"""Tests for the SQL guard. No database required.

The guard is the layer that stops a model turning a read-only tool into a
write tool by being creative. It is worth more tests than it has lines.
"""

from __future__ import annotations

import pytest

from app import db


# --- things that must be allowed ------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from patients limit 10",
    "  SELECT count(*) FROM orders WHERE total > 100  ",
    "WITH recent AS (SELECT * FROM orders WHERE created_at > now() - interval '7 days') "
    "SELECT count(*) FROM recent",
    "SELECT * FROM merchants;",                     # one trailing semicolon is fine
    "-- how many merchants\nSELECT count(*) FROM merchants",
    "/* a comment */ SELECT name FROM merchants",
    "SELECT a.name, b.total FROM merchants a JOIN orders b ON b.merchant_id = a.id",
])
def test_ordinary_selects_are_allowed(sql):
    assert db.check_statement(sql)


# --- things that must not be ----------------------------------------------

@pytest.mark.parametrize("sql", [
    "DELETE FROM patients",
    "delete from patients where id = 1",
    "UPDATE merchants SET name = 'x'",
    "INSERT INTO orders (id) VALUES (1)",
    "DROP TABLE patients",
    "TRUNCATE orders",
    "ALTER TABLE orders ADD COLUMN x int",
    "CREATE TABLE evil (id int)",
    "GRANT ALL ON orders TO public",
    "COPY orders TO '/tmp/out.csv'",
    "VACUUM FULL",
    "SET default_transaction_read_only = off",
])
def test_writes_are_refused(sql):
    with pytest.raises(db.DatabaseError):
        db.check_statement(sql)


def test_a_second_statement_after_a_semicolon_is_refused():
    """The classic way a read-only tool becomes a write tool."""
    with pytest.raises(db.DatabaseError, match="one statement"):
        db.check_statement("SELECT 1; DROP TABLE patients")


def test_a_write_hidden_inside_a_cte_is_refused():
    """PostgreSQL really will let you DELETE inside a WITH clause."""
    with pytest.raises(db.DatabaseError, match="DELETE"):
        db.check_statement(
            "WITH gone AS (DELETE FROM patients RETURNING *) SELECT count(*) FROM gone"
        )


def test_a_write_hidden_behind_a_comment_is_refused():
    with pytest.raises(db.DatabaseError):
        db.check_statement("SELECT 1 /* harmless */ ; DELETE FROM orders")


def test_a_comment_that_makes_it_look_like_a_select_is_refused():
    with pytest.raises(db.DatabaseError):
        db.check_statement("-- SELECT\nDELETE FROM patients")


@pytest.mark.parametrize("sql", ["", "   ", "-- nothing here", "/* just a comment */"])
def test_empty_input_is_refused(sql):
    with pytest.raises(db.DatabaseError):
        db.check_statement(sql)


def test_the_error_says_what_would_have_worked():
    """A model that cannot read the refusal cannot correct itself."""
    with pytest.raises(db.DatabaseError) as caught:
        db.check_statement("UPDATE merchants SET name = 'x'")
    message = str(caught.value)
    assert "SELECT" in message and "read-only" in message


# --- configuration --------------------------------------------------------

def test_the_tools_report_clearly_when_no_database_is_configured(monkeypatch):
    monkeypatch.setattr(db, "DB_HOST", "")
    monkeypatch.setattr(db, "DB_DATABASE", "")
    monkeypatch.setattr(db, "DB_USER", "")
    assert db.is_configured() is False
    with pytest.raises(db.DatabaseError, match="DB_HOST"):
        db.run_sql("SELECT 1")


def test_the_password_never_appears_in_a_connection_error(monkeypatch):
    """An error message goes to the model, the browser and the log."""
    secret = "hunter2-do-not-leak"
    monkeypatch.setattr(db, "DB_HOST", "203.0.113.1")
    monkeypatch.setattr(db, "DB_DATABASE", "nope")
    monkeypatch.setattr(db, "DB_USER", "nobody")
    monkeypatch.setattr(db, "DB_PASSWORD", secret)
    monkeypatch.setattr(db, "DB_CONNECT_TIMEOUT", 1)

    class Boom(Exception):
        pass

    import psycopg

    def explode(**kwargs):
        raise Boom(f"connection failed: password={kwargs.get('password')}")

    monkeypatch.setattr(psycopg, "connect", explode)
    with pytest.raises(db.DatabaseError) as caught:
        db.run_sql("SELECT 1")
    assert secret not in str(caught.value)
    assert "***" in str(caught.value)


def test_the_row_cap_cannot_be_raised_by_the_model(monkeypatch):
    """max_rows is a request, not an instruction."""
    monkeypatch.setattr(db, "DB_MAX_ROWS", 50)
    captured = {}

    class FakeCursor:
        description = [type("D", (), {"name": "n"})()]
        def execute(self, sql): captured["sql"] = sql
        def fetchmany(self, n): captured["limit"] = n; return []
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeConn:
        def cursor(self): return FakeCursor()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(db, "_connect", lambda: FakeConn())
    db.run_sql("SELECT 1", max_rows=100000)
    assert captured["limit"] == 51  # the cap, plus one to detect truncation


# --------------------------------------------------------------------------
# Things that must hold without a database in front of them.
# These are the parts that broke in real use, so they get regression tests
# that run in CI where a live connection never will.
# --------------------------------------------------------------------------

import datetime
import decimal

import pytest

from app import db


@pytest.mark.parametrize(
    "given, expected",
    [
        ("Report", (None, "Report")),
        ('"Report"', (None, "Report")),
        ("public.Report", ("public", "Report")),
        ('public."Report"', ("public", "Report")),
        ('"public"."Report"', ("public", "Report")),
        ('  public . "Report" ', ("public", "Report")),
        ('"odd.name"', (None, "odd.name")),
        ('public."say ""hi"""', ("public", 'say "hi"')),
    ],
)
def test_describe_table_accepts_every_spelling_it_hands_out(given, expected):
    # describe_table tells the model to use public."Report". Refusing that
    # string back is the tool contradicting its own instructions, which is
    # exactly what happened in practice.
    assert db._split_identifier(given) == expected


def test_a_name_is_quoted_only_when_it_has_to_be():
    assert db._quote_one("invoices") == "invoices"
    assert db._quote_one("Report") == '"Report"'
    assert db._quote_one("Hospital Name") == '"Hospital Name"'
    assert db._quote_one("2024") == '"2024"'
    assert db._quote_one('a"b') == '"a""b"'


def test_excel_values_keep_their_type():
    # A date written as a string sorts alphabetically in Excel and cannot be
    # filtered by month, so the export converter must not stringify like the
    # JSON one does.
    when = datetime.datetime(2023, 4, 1, 12, 0)
    assert db._excel_safe(when) == when
    assert db._excel_safe(decimal.Decimal("11200.50")) == 11200.5
    assert db._excel_safe(None) is None
    assert db._excel_safe("text") == "text"


def test_a_huge_decimal_stays_text_rather_than_being_rounded():
    # Past 15 digits this is an identifier, not an amount. Rounding it would
    # corrupt it silently, which is worse than showing it as text.
    big = decimal.Decimal("123456789012345678901")
    assert db._excel_safe(big) == "123456789012345678901"


def test_a_timezone_is_dropped_not_stringified():
    aware = datetime.datetime(2023, 4, 1, 12, 0, tzinfo=datetime.timezone.utc)
    got = db._excel_safe(aware)
    assert isinstance(got, datetime.datetime) and got.tzinfo is None


def test_a_keyword_inside_a_string_literal_is_not_a_command():
    # "comment", "do", "grant" and "set" are ordinary English. Scanning the raw
    # statement refused perfectly good queries whose WHERE clause happened to
    # contain one, and the model had no way to tell why.
    assert db.check_statement("SELECT * FROM t WHERE note = 'do not use'")
    assert db.check_statement("SELECT * FROM t WHERE name = 'Grant Hospital'")
    assert db.check_statement('''SELECT "Comment" FROM public."Report"''')


def test_a_write_outside_quotes_is_still_refused():
    with pytest.raises(db.DatabaseError):
        db.check_statement("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x")
    with pytest.raises(db.DatabaseError):
        db.check_statement("SELECT * FROM t WHERE a = 'ok' AND (DROP TABLE t)")


def test_repeated_column_names_are_kept_apart():
    # SELECT a."ID", b."ID" returns two columns called ID. Keyed into a dict
    # the second silently replaced the first, so the model was shown one
    # column's name above another column's values.
    assert db._unique_labels(["ID", "ID", "name"]) == ["ID", "ID (2)", "name"]
    assert db._unique_labels(["a", "b"]) == ["a", "b"]

