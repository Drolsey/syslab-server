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

import re
from typing import Any

from app.config import (
    DB_CONNECT_TIMEOUT,
    DB_DATABASE,
    DB_HOST,
    DB_MAX_ROWS,
    DB_PASSWORD,
    DB_PORT,
    DB_SSLMODE,
    DB_STATEMENT_TIMEOUT_MS,
    DB_USER,
)


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
    lowered = bare.lower()
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

def is_configured() -> bool:
    return bool(DB_HOST and DB_DATABASE and DB_USER)


def _connect():
    if not is_configured():
        raise DatabaseError(
            "No database is configured. Set DB_HOST, DB_DATABASE, DB_USER and "
            "DB_PASSWORD in .env, then restart the app."
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
        f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} "
        f"-c idle_in_transaction_session_timeout=30000"
    )
    try:
        return psycopg.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_DATABASE,
            user=DB_USER, password=DB_PASSWORD, sslmode=DB_SSLMODE,
            connect_timeout=DB_CONNECT_TIMEOUT, options=options,
            application_name="syslab-server",
        )
    except Exception as exc:  # noqa: BLE001
        # Never let a connection string with a password reach a log or the model.
        message = str(exc).replace(DB_PASSWORD or "\0", "***") if DB_PASSWORD else str(exc)
        raise DatabaseError(f"Could not connect to {DB_HOST}:{DB_PORT}/{DB_DATABASE}: {message}") from exc


def _rows(cursor, limit: int) -> tuple[list[str], list[list[Any]], bool]:
    columns = [d.name for d in cursor.description] if cursor.description else []
    fetched = cursor.fetchmany(limit + 1)
    truncated = len(fetched) > limit
    data = [[_json_safe(v) for v in row] for row in fetched[:limit]]
    return columns, data, truncated


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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
    return {
        "database": DB_DATABASE,
        "count": len(rows),
        "tables": [dict(zip(columns, row)) for row in rows],
        "note": "Row counts are the planner's estimate, not exact. "
                "Use SELECT count(*) if an exact figure matters.",
    }


# --------------------------------------------------------------------------
# tool 2: what is in a table
# --------------------------------------------------------------------------

def describe_table(table: str, schema: str | None = None) -> dict:
    """Column names, types and nullability for one table.

    Deliberately returns no sample rows. Knowing a table has a `diagnosis`
    column is what the model needs to write a query; reading somebody's
    diagnosis is not, and should happen only when a question actually asks for it.
    """
    if not table or not table.strip():
        raise DatabaseError("No table name given.")
    name = table.strip()
    if "." in name and schema is None:
        schema, name = name.split(".", 1)

    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_schema, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = %s AND (%s IS NULL OR table_schema = %s)
            ORDER BY table_schema, ordinal_position
            """,
            (name, schema, schema),
        )
        columns, rows, _ = _rows(cursor, 400)

    if not rows:
        raise DatabaseError(
            f"No table named {table!r} is visible to this account. "
            "Call list_tables to see what is there."
        )
    return {
        "table": name,
        "columns": [
            {"name": r[1], "type": r[2], "nullable": r[3] == "YES", "default": r[4]}
            for r in rows
        ],
        "schemas": sorted({r[0] for r in rows}),
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

    with _connect() as connection, connection.cursor() as cursor:
        try:
            cursor.execute(statement)
        except Exception as exc:  # noqa: BLE001
            # The database's own message is the most useful thing we can hand
            # back: it names the missing column or the syntax error directly.
            raise DatabaseError(f"The database refused that query: {exc}") from exc
        columns, rows, truncated = _rows(cursor, limit)

    result = {
        "sql": statement,
        "columns": columns,
        "row_count": len(rows),
        "rows": rows,
        "truncated": truncated,
    }
    if truncated:
        result["note"] = (
            f"Only the first {limit} rows are shown. Add a LIMIT, or aggregate "
            "with count/sum/avg, rather than asking for everything."
        )
    return result
