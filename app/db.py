"""Read-only access to a customer's PostgreSQL database, exposed as three tools.

The model writes SQL. That is the point and also the danger, so this module is
mostly about what the model is NOT allowed to do. Four layers, outermost first:

  1. The database role itself should hold SELECT and nothing else. That is the
     only layer that cannot be talked around, and it is not ours to set. Verify
     it with scripts/check_database.py rather than assuming.
  2. Every connection sets default_transaction_read_only. PostgreSQL then
     refuses INSERT, UPDATE, DELETE, DROP and friends itself, whatever we send.
  3. This module refuses anything that is not a single SELECT or WITH.
  4. A statement timeout and a row cap, so one careless query cannot sit on the
     client's production database for ten minutes or drag back a million rows.

Layer 3 alone would be theatre. Layer 2 is what actually holds.
"""

from __future__ import annotations

import datetime
import decimal
import re
import time
from typing import Any

from app import config, tenancy
from app.config import (
    DB_CONNECT_TIMEOUT,
    DB_EXPORT_MAX_ROWS,
    DB_EXPORT_TIMEOUT_MS,
    DB_MAX_ROWS,
    DB_STATEMENT_TIMEOUT_MS,
    UnsafePathError,
    resolve_in_data_dir,
)
from app.context import current_tenant


class DatabaseError(Exception):
    """Something the model can read and act on."""


# --------------------------------------------------------------------------
# what counts as a safe statement
# --------------------------------------------------------------------------

FORBIDDEN = (
    "insert", "update", "delete", "drop", "truncate", "alter", "create",
    "grant", "revoke", "comment", "copy", "call", "do", "vacuum", "analyze",
    "reindex", "refresh", "set", "reset", "begin", "commit", "rollback",
    "listen", "notify", "lock", "prepare", "execute", "discard", "cluster",
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def _without_literals(sql: str) -> str:
    """The statement with the inside of every quoted string replaced by spaces.

    Length is preserved so positions still line up, and identifiers in double
    quotes are kept: a column really can be called "comment", and the keyword
    scan has to see it as a name rather than a command.
    """
    out = []
    quote: str | None = None
    for ch in sql:
        if quote:
            out.append(ch if ch == quote else " ")
            if ch == quote:
                quote = None
            continue
        if ch == "'":
            quote = ch
            out.append(ch)
            continue
        out.append(ch)
    return "".join(out)


def check_statement(sql: str) -> str:
    """Return the statement, or raise with a message the model can act on."""
    if not sql or not sql.strip():
        raise DatabaseError("No SQL given.")

    bare = _strip_comments(sql).rstrip().rstrip(";").strip()
    if not bare:
        raise DatabaseError("That was only a comment, with no statement in it.")

    # One statement. A second one after a semicolon is the classic way a
    # read-only tool becomes a write tool.
    if ";" in bare:
        raise DatabaseError(
            "Send one statement at a time. Semicolons separating multiple "
            "statements are refused."
        )

    first = re.split(r"\s|\(", bare.lower(), maxsplit=1)[0]
    if first not in {"select", "with"}:
        raise DatabaseError(
            f"Only SELECT queries are allowed here, and this one starts with "
            f"{first.upper()!r}. This connection is read-only: the database "
            "will refuse to change anything even if asked."
        )

    # A CTE can hide a write in PostgreSQL: WITH x AS (DELETE ... RETURNING *)
    # Scan the statement with its string literals blanked out first: the words
    # below are ordinary English, and WHERE "Comment" LIKE '%do not use%' was
    # being refused as an attempt to run DO. Only the text outside quotes can
    # be a keyword.
    lowered = _without_literals(bare).lower()
    for word in FORBIDDEN:
        if re.search(rf"(^|[\s(]){word}\s", lowered):
            raise DatabaseError(
                f"This query contains {word.upper()!r}, which is not allowed on a "
                "read-only connection. Rewrite it as a plain SELECT."
            )
    return bare


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------

def _dbname() -> str:
    """The current tenant's database name, for messages. Never another's."""
    try:
        where = settings()
    except Exception:  # noqa: BLE001
        where = None
    return where["dbname"] if where else "the customer"


def settings() -> dict | None:
    """Which database belongs to the tenant making this request?

    Two sources, and no third:

    1. What the control plane stores for this tenant.
    2. The DB_* values in .env, which belong to the BOOTSTRAP TENANT and to
       nobody else. This is the same bridge as APP_TOKEN in app/main.py: it
       keeps the install that existed before tenancy working, and it is the
       reason a second tenant does not silently inherit the first one's
       database.

    Returns None when this tenant has no database. None is not a licence to use
    someone else's.
    """
    tenant = current_tenant()

    stored = tenancy.database_for(tenant)
    if stored:
        return stored

    if tenant == config.BOOTSTRAP_TENANT and config.DB_HOST and config.DB_DATABASE \
            and config.DB_USER:
        return {
            "host": config.DB_HOST,
            "port": config.DB_PORT,
            "dbname": config.DB_DATABASE,
            "username": config.DB_USER,
            "password": config.DB_PASSWORD,
            "sslmode": config.DB_SSLMODE,
        }
    return None


def is_configured() -> bool:
    """Does the CURRENT TENANT have a database? Not: does this server have one."""
    try:
        return settings() is not None
    except Exception:  # noqa: BLE001 - a broken control plane is not a database
        return False


def _connect(statement_timeout_ms: int | None = None):
    where = settings()
    if where is None:
        if current_tenant() == config.BOOTSTRAP_TENANT:
            # The operator's own install. Name the setting, because they can fix it.
            raise DatabaseError(
                "No database is configured. Set DB_HOST, DB_DATABASE, DB_USER and "
                "DB_PASSWORD in .env, then restart the app."
            )
        # A customer. Naming this machine's .env would be both useless to them
        # and a detail of somebody else's server.
        raise DatabaseError(
            "No database is configured for this account. Nothing was read, and no "
            "other account's database was used instead. If this account should "
            "have one, it has to be set up for this account specifically."
        )
    try:
        import psycopg
    except ImportError as exc:
        raise DatabaseError(
            "The psycopg driver is not installed. Run: pip install -r requirements.txt"
        ) from exc

    # These options are applied by the server on connect, so they hold for
    # everything sent afterwards, including anything this module failed to catch.
    options = (
        f"-c default_transaction_read_only=on "
        f"-c statement_timeout={statement_timeout_ms or DB_STATEMENT_TIMEOUT_MS} "
        f"-c idle_in_transaction_session_timeout=30000"
    )
    password = where.get("password") or ""
    try:
        # A fresh connection per call, deliberately. A pooled one would have to
        # be keyed by tenant, and a pool key that is wrong once is one customer
        # running queries on another customer's database.
        return psycopg.connect(
            host=where["host"], port=where["port"], dbname=where["dbname"],
            user=where["username"], password=password,
            sslmode=where.get("sslmode") or "require",
            connect_timeout=DB_CONNECT_TIMEOUT, options=options,
            application_name="syslab-server",
        )
    except Exception as exc:  # noqa: BLE001
        # Never let a connection string with a password reach a log or the model.
        message = str(exc).replace(password, "***") if password else str(exc)
        raise DatabaseError(
            f"Could not connect to {where['host']}:{where['port']}/{where['dbname']}: "
            f"{message}"
        ) from exc


def _explain(exc: Exception, statement: str, exporting: bool = False) -> str:
    """Turn a database error into something the model can act on.

    A bare "canceling statement due to statement timeout" tells the model only
    that it failed, so it retries the identical query, or guesses at a smaller
    max_rows -- which in the old code changed nothing, because the limit was
    applied after the server had already done all the work.
    """
    text = str(exc).strip()
    if "statement timeout" in text.lower():
        has_limit = "limit" in statement.lower()
        advice = (
            "The query took too long and the server stopped it. "
            "This is about how much work the query asks for, not about how "
            "many rows you want back."
        )
        if exporting:
            advice += (
                " This export already had the long time budget and still ran "
                "out, so the query itself is too expensive. Narrow it with a "
                "WHERE clause, or export fewer columns by naming them instead "
                "of using *. Do not simply retry it."
            )
        elif not has_limit:
            advice += (
                " Add a WHERE clause to narrow it, or an aggregate "
                "(count, sum, group by) so the server returns a summary "
                "instead of every row. If you truly need the whole table in a "
                "file, call query_to_excel, which is allowed far longer."
            )
        else:
            advice += (
                " It already has a LIMIT, so the cost is in the scan itself: "
                "filter with WHERE on an indexed column, or select fewer "
                "columns by naming them instead of using *."
            )
        return advice
    return f"The database refused that query: {text}"


def _streaming_cursor(connection, name: str):
    """A server-side cursor, so rows are fetched in batches and not all at once.

    This is the difference between a query that works and one that times out.
    An ordinary psycopg cursor pulls the ENTIRE result into client memory
    during execute(), before a single fetchmany() runs -- so asking for 200
    rows of a 49,795-row table still made the server produce all 49,795 and
    ship them over the wire. The row limit was applied after the expensive
    part, which is to say it did nothing at all.

    A named cursor issues DECLARE ... FETCH instead: the server keeps the
    result and hands over only what is asked for. The statement timeout then
    applies to each FETCH rather than to the whole table.
    """
    try:
        return connection.cursor(name=name)
    except TypeError:
        # A driver without server-side cursor support: correctness over speed.
        return connection.cursor()


def _rows(cursor, limit: int) -> tuple[list[str], list[list[Any]], bool]:
    columns = [d.name for d in cursor.description] if cursor.description else []
    fetched = cursor.fetchmany(limit + 1)
    truncated = len(fetched) > limit
    data = [[_json_safe(v) for v in row] for row in fetched[:limit]]
    return columns, data, truncated


def _unique_labels(columns: list[str]) -> list[str]:
    """Column names made unique, so a row can be keyed by them without loss."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for index, name in enumerate(columns):
        label = name or f"column_{index + 1}"
        if label in seen:
            seen[label] += 1
            label = f"{label} ({seen[label]})"
        seen.setdefault(label, 1)
        out.append(label)
    return out


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _quoted(schema: str, name: str) -> str:
    """The exact text to put in a query.

    PostgreSQL folds an unquoted identifier to lower case, so a table created
    as "Report" cannot be reached by writing Report -- that becomes report and
    the server says the relation does not exist. The model has no way to know
    which names were created with capitals, so we work it out here and hand it
    a name that is always safe to paste.
    """
    return f"{_quote_one(schema)}.{_quote_one(name)}"


def _quote_one(piece: str) -> str:
    """Quote a single identifier if it needs it.

    A name is safe bare only if it is all lower case, starts with a letter or
    underscore, and holds nothing but letters, digits and underscores. Anything
    else -- capitals, spaces, dashes -- must be quoted or the server either
    folds it to something that does not exist or fails to parse it at all.
    """
    safe = (
        piece
        and piece.islower()
        and not piece[:1].isdigit()
        and all(ch.isalnum() or ch == "_" for ch in piece)
    )
    return piece if safe else '"' + piece.replace('"', '""') + '"'


# --------------------------------------------------------------------------
# tool 1: what tables are there
# --------------------------------------------------------------------------

def list_tables() -> dict:
    """List the tables the assistant can read, with approximate row counts.

    Call this before writing any query. Guessing a table name wastes a turn.
    """
    sql = """
        SELECT c.relname AS table_name,
               n.nspname AS schema_name,
               GREATEST(c.reltuples::bigint, 0) AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'v', 'm', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND has_table_privilege(c.oid, 'SELECT')
        ORDER BY n.nspname, c.relname
    """
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql)
        columns, rows, _ = _rows(cursor, 500)
    tables = []
    for row in rows:
        entry = dict(zip(columns, row))
        entry["query_as"] = _quoted(entry["schema_name"], entry["table_name"])
        tables.append(entry)
    return {
        "database": _dbname(),
        "count": len(tables),
        "tables": tables,
        "note": "Write the table into your SQL exactly as query_as gives it, "
                "quotes and all. approx_rows is the query planner's ESTIMATE "
                "from the last analyse, not a count -- it can be out by "
                "thousands on a table that has grown since. Always call it "
                "approximate when you say it, and run SELECT count(*) if the "
                "user needs the real figure.",
    }


# --------------------------------------------------------------------------
# tool 2: what is in a table
# --------------------------------------------------------------------------

def _split_identifier(raw: str) -> tuple[str | None, str]:
    """Turn whatever the model sends into (schema, table).

    It may send any of these, and all of them are reasonable:
        Report              public.Report
        "Report"            public."Report"        "public"."Report"
    The last two are what describe_table itself told the model to use, so
    refusing them would be the tool contradicting its own instructions. A
    plain split on "." is not enough: a quoted name may legally contain one.
    """
    out: list[str] = []
    current: list[str] = []
    in_quotes = False
    i = 0
    text = str(raw).strip()
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if in_quotes and i + 1 < len(text) and text[i + 1] == '"':
                current.append('"')
                i += 2
                continue
            in_quotes = not in_quotes
        elif ch == "." and not in_quotes:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    out.append("".join(current))
    parts = [p.strip() for p in out if p.strip()]
    if not parts:
        return None, ""
    if len(parts) == 1:
        return None, parts[0]
    return parts[-2], parts[-1]


def describe_table(table: str, schema: str | None = None) -> dict:
    """Column names, types and nullability for one table.

    Deliberately returns no sample rows. Knowing a table has a `diagnosis`
    column is what the model needs to write a query; reading somebody's
    diagnosis is not, and should happen only when a question actually asks for it.
    """
    if not table or not table.strip():
        raise DatabaseError("No table name given.")
    parsed_schema, name = _split_identifier(table)
    if schema is None:
        schema = parsed_schema
    if schema:
        schema = _split_identifier(schema)[1]
    if not name:
        raise DatabaseError("No table name given.")

    # Build the filter in Python rather than passing the schema as a
    # parameter that may be NULL. `%s IS NULL` gives the server a parameter
    # with no type to infer from, and it refuses with
    # "could not determine data type of parameter".
    sql = """
        SELECT table_schema, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = %s
    """
    values: list[Any] = [name]
    if schema:
        sql += " AND table_schema = %s"
        values.append(schema)
    sql += " ORDER BY table_schema, ordinal_position"

    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql, tuple(values))
        columns, rows, _ = _rows(cursor, 400)

    if not rows:
        raise DatabaseError(
            f"No table named {table!r} is visible to this account. "
            "Call list_tables to see what is there."
        )
    schemas = sorted({r[0] for r in rows})
    return {
        "table": name,
        "query_as": _quoted(schemas[0], name),
        "columns": [
            {
                "name": r[1],
                "query_as": _quote_one(r[1]),
                "type": r[2],
                "nullable": r[3] == "YES",
                "default": r[4],
            }
            for r in rows
        ],
        "schemas": schemas,
        "needs_quoting": sorted(
            {r[1] for r in rows if _quote_one(r[1]) != r[1]}
        ),
        "note": "Write the table and every column into your SQL using their "
                "query_as text, quotes and all. Column names here contain "
                "capitals and spaces; unquoted they are a syntax error or "
                "silently the wrong column.",
    }


# --------------------------------------------------------------------------
# tool 3: run a query
# --------------------------------------------------------------------------

def run_sql(sql: str, max_rows: int | None = None) -> dict:
    """Run one read-only SELECT and return the rows.

    The connection cannot write, whatever the query says. Long-running queries
    are cut off by the server rather than left to sit on a production database.
    """
    statement = check_statement(sql)
    limit = min(int(max_rows or DB_MAX_ROWS), DB_MAX_ROWS)

    with _connect() as connection:
        with _streaming_cursor(connection, "syslab_read") as cursor:
            # execute() and the fetch are one guarded unit on purpose. A
            # server-side cursor does almost no work during execute -- it just
            # DECLAREs -- so a slow query does not fail there any more, it
            # fails on the first FETCH. Guarding only execute() looked correct
            # and let a timeout escape as a raw psycopg traceback.
            try:
                cursor.execute(statement)
                columns, rows, truncated = _rows(cursor, limit)
            except DatabaseError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DatabaseError(_explain(exc, statement)) from exc

    # Rows go back as objects keyed by column name, not as bare lists.
    # A list of 35 unlabelled values next to a separate list of 35 headers is
    # something the model has to zip together in its head, and with a wide
    # table it silently gets it wrong -- inventing "Field 1 ... Field 28"
    # instead of reading the real names. Repeating the key on every row costs
    # tokens; getting the wrong column costs the user a wrong answer.
    # SELECT a."ID", b."ID" hands back two columns called ID. Keyed straight
    # into a dict the second overwrites the first, and the model is told about
    # one column while looking at the values of another -- a wrong answer that
    # looks like a right one. Make the labels unique instead.
    labels = _unique_labels(columns)
    labelled = [dict(zip(labels, row)) for row in rows]

    result = {
        "sql": statement,
        "columns": labels,
        "column_count": len(columns),
        "row_count": len(labelled),
        "rows": labelled,
        "truncated": truncated,
    }
    notes = []
    if truncated:
        notes.append(
            f"You are seeing the first {limit} rows and there are more. Any "
            "total, count or average you work out from these rows will be "
            "wrong. Ask the server instead: count(*), sum(...), GROUP BY."
        )
    if labels != columns:
        notes.append(
            "Two or more columns came back with the same name, so the repeats "
            "have been numbered to keep them apart. Name the columns in your "
            "SELECT, or alias them, if you need to tell them apart properly."
        )
    if len(columns) > 12:
        notes.append(
            f"This result is {len(columns)} columns wide. If the question only "
            "needs a few of them, name those columns instead of SELECT * -- it "
            "is easier to read and far less likely to go wrong."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


# --------------------------------------------------------------------------
# tool 4: a query straight into a file
# --------------------------------------------------------------------------

def _excel_safe(value: Any) -> Any:
    """Convert one database value into something openpyxl will accept.

    Deliberately different from _json_safe, which stringifies everything it
    does not recognise. That is right for the model -- it reads text -- and
    wrong for a spreadsheet, where a date written as a string sorts
    alphabetically and cannot be filtered by month.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        # openpyxl rejects Decimal. Float loses precision beyond 15 digits,
        # which no invoice total reaches; a value that large is more likely an
        # identifier, so keep those as text rather than rounding them silently.
        return float(value) if abs(value) < decimal.Decimal("1e15") else str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            # Excel has no concept of a timezone. Store the wall-clock time and
            # let the offset go, rather than writing a string.
            return value.replace(tzinfo=None)
        return value
    return str(value)


def query_to_excel(sql: str, filename: str, sheet: str | None = None,
                   max_rows: int | None = None) -> dict:
    """Run a SELECT and write every row to a spreadsheet.

    The point of this tool is what it does NOT do: the rows never pass through
    the model. It reports how many were written and what the columns are, and
    that is all the model needs in order to tell the user the file is ready.
    A 50,000-row answer costs the same handful of tokens as a 5-row one.
    """
    from openpyxl import Workbook

    statement = check_statement(sql)
    limit = min(int(max_rows or DB_EXPORT_MAX_ROWS), DB_EXPORT_MAX_ROWS)

    name = str(filename).strip()
    if not name:
        raise DatabaseError("No filename given for the spreadsheet.")
    if not name.lower().endswith(".xlsx"):
        name = f"{name}.xlsx"

    try:
        path = resolve_in_data_dir(name)
    except UnsafePathError as exc:
        raise DatabaseError(str(exc)) from exc

    book = Workbook(write_only=True)
    worksheet = book.create_sheet(title=(sheet or "Query")[:31])

    written = 0
    truncated = False
    columns: list[str] = []
    with _connect(DB_EXPORT_TIMEOUT_MS) as connection, \
            _streaming_cursor(connection, "syslab_export") as cursor:
        try:
            cursor.execute(statement)
            if cursor.description is None:
                raise DatabaseError(
                    "That statement returned no columns, so there is nothing to write."
                )
            columns = [d.name for d in cursor.description]
            worksheet.append(columns)

            # Fetched in batches so a large export does not sit in memory
            # twice, and written straight out: write_only mode keeps one row
            # at a time. The batches are also where the time goes, so this is
            # inside the guard.
            while written < limit:
                batch = cursor.fetchmany(min(1000, limit - written))
                if not batch:
                    break
                for row in batch:
                    worksheet.append([_excel_safe(v) for v in row])
                    written += 1
            truncated = bool(cursor.fetchmany(1))
        except DatabaseError:
            book.close()
            raise
        except Exception as exc:  # noqa: BLE001
            book.close()
            raise DatabaseError(_explain(exc, statement, exporting=True)) from exc

    try:
        book.save(path)
    except PermissionError as exc:
        raise DatabaseError(
            f"Could not save {path.name}: the file is open in Excel. Close it and retry."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise DatabaseError(f"Could not write {path.name}: {exc}") from exc
    finally:
        book.close()

    # The export is a file in the data folder like any other, so it goes
    # through the same intake as an upload. Never fatal: the spreadsheet is
    # already written.
    try:
        from app import intake

        intake.arrived(path)
    except Exception:  # noqa: BLE001
        pass

    result = {
        "file": path.name,
        "path": str(path),
        "sheet": worksheet.title,
        "rows_written": written,
        "columns": columns,
        "column_count": len(columns),
        "size_kb": round(path.stat().st_size / 1024, 1),
        "sql": statement,
        "truncated": truncated,
        "note": (
            f"{written:,} rows are in the file. This is an EXACT count of rows "
            "written. If it differs from the approximate count list_tables "
            "gave you, this one is right: that one is the query planner's "
            "estimate from the last time the table was analysed, and it drifts "
            "as rows are added. Say the exact number and do not treat the "
            "difference as an error. "
            f"You have not seen these rows and you "
            "do not need to. Tell the user the filename and the row count. Do "
            "not describe the contents, do not summarise the data, and do not "
            "run the query again to look at it."
        ),
    }
    if truncated:
        result["note"] += (
            f" The query had more than {limit:,} rows and the file stops there. "
            "Say so, and offer to narrow it with a WHERE clause."
        )
    if written == 0:
        result["note"] = (
            "The query ran but matched no rows, so the file has only a header. "
            "Tell the user it came back empty rather than saying it worked."
        )
    return result


# --------------------------------------------------------------------------
# a cheap way for the file tools to notice they are in the wrong world
# --------------------------------------------------------------------------

_TABLE_CACHE: dict[str, Any] = {"at": 0.0, "names": {}}
_TABLE_CACHE_SECONDS = 300.0


def known_table_names(max_age: float = _TABLE_CACHE_SECONDS) -> dict[str, str]:
    """Lower-cased table name -> the text to put in SQL. Never raises.

    Used by the file tools so that a request for "Report.xlsx", when Report is
    a table and not a file, can say so instead of listing eighteen unrelated
    filenames. Cached because it is called on an error path, and an error path
    must not become slow or fragile: any failure here returns nothing at all
    and the caller carries on with the plain message.
    """
    if not is_configured():
        return {}
    now = time.time()
    if _TABLE_CACHE["names"] and now - _TABLE_CACHE["at"] < max_age:
        return _TABLE_CACHE["names"]
    try:
        found = {
            entry["table_name"].lower(): entry["query_as"]
            for entry in list_tables()["tables"]
        }
    except Exception:  # noqa: BLE001 - a hint is never worth an exception
        return _TABLE_CACHE["names"]
    _TABLE_CACHE.update({"at": now, "names": found})
    return found


def table_hint(name: str) -> str | None:
    """If `name` looks like a database table, say what to do about it."""
    stem = str(name).strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = stem.strip().strip('"').lower()
    if not stem:
        return None
    tables = known_table_names()
    match = tables.get(stem)
    if not match:
        return None
    return (
        f"{stem!r} is not a file -- it is a TABLE in the {_dbname()} database, "
        f"written in SQL as {match}. Tables and files are two separate places "
        "and no file tool can reach a table. To put this table into a "
        f"spreadsheet call query_to_excel with sql='SELECT * FROM {match}'. "
        "To look at a few rows instead, call run_sql. Do not call list_files "
        "or search_files: they only ever see the data folder, and this is not "
        "in the data folder."
    )
