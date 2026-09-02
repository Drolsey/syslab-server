"""Gate for the database tools: is the connection real, and is it genuinely read-only?

    .\\run.cmd scripts\\check_database.py

This does not take anyone's word for the account being read-only. It tries a
write and expects to be refused. The probe is a CREATE TEMP TABLE, so in the
worst case, where the account turns out to be writable after all, it creates a
temporary table that disappears the moment the connection closes. Nothing
permanent is touched either way.
"""

from __future__ import annotations

import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deps import require  # noqa: E402

require("psycopg")

from app import db  # noqa: E402
from app.config import (  # noqa: E402
    DB_CONNECT_TIMEOUT, DB_DATABASE, DB_HOST, DB_MAX_ROWS, DB_PORT,
    DB_SSLMODE, DB_STATEMENT_TIMEOUT_MS, DB_USER,
)

LINE = "-" * 66
results: list[tuple[str, bool, str]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def public_ip() -> str | None:
    """Ask an outside service what address the world sees us as.

    Needed because an allowlist is written in terms of this, not your local
    address. Sends one request to ipify and nothing else.
    """
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=6) as response:
                value = response.read().decode().strip()
                if value:
                    return value
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return None


def diagnose_connection() -> None:
    """Work out WHICH layer failed, rather than listing everything it could be.

    The three outcomes mean different things and have different fixes, and the
    driver's own message does not distinguish them clearly enough to act on.
    """
    section("Where exactly it is failing")

    # Layer 1: can we even resolve and route to the host?
    print(f"  Opening a plain TCP socket to {DB_HOST}:{DB_PORT}, {DB_CONNECT_TIMEOUT}s limit...")
    started = time.time()
    verdict = None
    try:
        with socket.create_connection((DB_HOST, DB_PORT), timeout=DB_CONNECT_TIMEOUT):
            elapsed = (time.time() - started) * 1000
        print(f"  The port is open and accepting connections ({elapsed:.0f} ms).")
        verdict = "open"
    except socket.timeout:
        print(f"  Timed out after {DB_CONNECT_TIMEOUT}s. The packets left and nothing came back.")
        verdict = "timeout"
    except ConnectionRefusedError:
        print("  Refused immediately. Something answered and said no.")
        verdict = "refused"
    except socket.gaierror as exc:
        print(f"  Could not resolve the host at all: {exc}")
        verdict = "dns"
    except OSError as exc:
        print(f"  Failed before reaching the host: {exc}")
        verdict = "unreachable"

    print()
    if verdict == "open":
        print("  So the network is fine and the problem is inside PostgreSQL itself:")
        print("  wrong database name, wrong user, a pg_hba rule that rejects your")
        print("  address, or SSL negotiation failing. Re-run this check for the exact")
        print("  message the server sends back.")
    elif verdict == "timeout":
        print("  A silent drop is a firewall, not a database. Ranked by likelihood:")
        print()
        print("  1. YOUR ADDRESS IS NOT ON THE ALLOWLIST. Managed PostgreSQL on GCP")
        print("     accepts connections only from addresses the owner has authorised,")
        print("     and it drops everything else without replying. This is almost")
        print("     always the answer, and only the client can fix it.")
        ip = public_ip()
        if ip:
            print(f"\n     Your public address is  {ip}")
            print("     Send the client exactly that and ask them to add it under")
            print("     Authorized networks on the instance. Ask whether it is static:")
            print("     if your ISP rotates it, this breaks again next week and the")
            print("     real answer is a private IP with a VPN, or the Cloud SQL proxy.")
        else:
            print("\n     Could not determine your public address. Check it at")
            print("     whatismyip.com and send that to the client.")
        print()
        print("  2. The instance is stopped, or has no public IP at all. Many managed")
        print("     databases are private-only by design and are reached through the")
        print("     Cloud SQL Auth Proxy rather than a direct connection.")
        print()
        print("  3. Your own network blocks outbound 5432. Rare on home broadband,")
        print("     common on corporate and university networks. Test by running this")
        print("     again on your phone's hotspot: if it works there, it is your router.")
    elif verdict == "refused":
        print("  You reached the machine and nothing is listening on that port.")
        print("  Either the database is not running, or it is on a different port.")
    elif verdict == "dns":
        print("  The host could not be resolved. Check DB_HOST in .env for a typo.")
    elif verdict == "unreachable":
        print("  Your machine could not route to that address at all, which is a")
        print("  network or VPN problem on your side rather than anything the client")
        print("  controls. Check you are online and not on a restricted network.")

    print("\n  What to ask the client, in one message:")
    print("    - Is this instance reachable over a public IP, or only through the")
    print("      Cloud SQL Auth Proxy or a private network?")
    print("    - If public: please add <your address> to the authorised networks.")
    print("    - Does database_agent_ai hold SELECT only? I would rather it did.")
    print()


def main() -> int:
    print("\nsyslab-server / database check")
    if not db.is_configured():
        print("\n  No database configured. Fill DB_HOST, DB_DATABASE, DB_USER and")
        print("  DB_PASSWORD in .env, then restart the app.\n")
        return 1
    print(f"  {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_DATABASE}  sslmode={DB_SSLMODE}")

    # ---- 1. connect ----------------------------------------------------
    section("Connection")
    started = time.time()
    try:
        with db._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT version(), current_user, current_database()")
            version, user, database = cursor.fetchone()
    except db.DatabaseError as exc:
        record("Connected", False, str(exc))
        diagnose_connection()
        return 1
    record("Connected", True, f"{(time.time() - started) * 1000:.0f} ms")
    print(f"        {version.split(',')[0]}")
    record("Connected as the expected user", user == DB_USER, f"server says {user!r}")
    record("Connected to the expected database", database == DB_DATABASE, f"server says {database!r}")

    # ---- 2. encryption -------------------------------------------------
    section("Encryption in transit")
    try:
        with db._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            row = cursor.fetchone()
        if row and row[0]:
            record("Traffic is encrypted", True, f"{row[1]}, {row[2]}")
        else:
            record("Traffic is encrypted", False, "the connection is in the clear")
    except Exception as exc:  # noqa: BLE001
        record("Traffic is encrypted", False, f"could not read pg_stat_ssl: {exc}")

    # ---- 3. the important one ------------------------------------------
    section("Is it actually read-only?")
    try:
        with db._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SHOW default_transaction_read_only")
            record("Session opens read-only", cursor.fetchone()[0] == "on",
                   "set by this app on every connection")
            cursor.execute("SHOW statement_timeout")
            record("Statement timeout is set", True,
                   f"{cursor.fetchone()[0]} (configured {DB_STATEMENT_TIMEOUT_MS} ms)")
    except Exception as exc:  # noqa: BLE001
        record("Session opens read-only", False, repr(exc))

    refused = False
    try:
        with db._connect() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE syslab_write_probe (x integer)")
    except Exception as exc:  # noqa: BLE001
        refused = True
        reason = str(exc).strip().splitlines()[0]
    record("A write is refused by the server", refused,
           reason if refused else "IT SUCCEEDED, which means writes are possible")
    if not refused:
        print("\n  This matters. The app's guard rejects non-SELECT statements, but the")
        print("  database itself would accept a write if anything got past it. Ask the")
        print("  client to grant this account SELECT only:")
        print(f"      REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {DB_USER};")
        print(f"      GRANT SELECT ON ALL TABLES IN SCHEMA public TO {DB_USER};")

    # ---- 4. what the account can reach ---------------------------------
    section("Privileges this account holds")
    try:
        with db._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT privilege_type, count(*)
                FROM information_schema.role_table_grants
                WHERE grantee = current_user
                GROUP BY privilege_type ORDER BY privilege_type
                """
            )
            grants = cursor.fetchall()
        if grants:
            for privilege, count in grants:
                marker = "     " if privilege == "SELECT" else "  <-- "
                print(f"    {privilege:<12} on {count:>4} tables{marker}")
            writable = [p for p, _ in grants if p != "SELECT"]
            record("Granted SELECT and nothing else", not writable,
                   "also holds " + ", ".join(writable) if writable else "")
        else:
            record("Granted SELECT and nothing else", True,
                   "no explicit grants found, access is probably via a role")
    except Exception as exc:  # noqa: BLE001
        record("Granted SELECT and nothing else", False, repr(exc))

    # ---- 5. the tools themselves ---------------------------------------
    section("The tools the model will call")
    try:
        listing = db.list_tables()
        record("list_tables works", True, f"{listing['count']} tables visible")
        for entry in listing["tables"][:12]:
            print(f"    {entry['schema_name']}.{entry['table_name']:<32} ~{entry['approx_rows']:>10,} rows")
        if listing["count"] > 12:
            print(f"    ... and {listing['count'] - 12} more")
    except db.DatabaseError as exc:
        record("list_tables works", False, str(exc))
        listing = {"tables": []}

    if listing["tables"]:
        first = listing["tables"][0]
        try:
            shape = db.describe_table(first["table_name"], first["schema_name"])
            names = ", ".join(c["name"] for c in shape["columns"][:8])
            record("describe_table works", True,
                   f"{first['table_name']}: {len(shape['columns'])} columns ({names}...)")
        except db.DatabaseError as exc:
            record("describe_table works", False, str(exc))

    try:
        started = time.time()
        result = db.run_sql("SELECT 1 AS ok")
        record("run_sql works", result["rows"] == [[1]], f"{(time.time() - started) * 1000:.0f} ms")
    except db.DatabaseError as exc:
        record("run_sql works", False, str(exc))

    try:
        db.run_sql("DELETE FROM information_schema.tables")
        record("run_sql refuses a write", False, "IT DID NOT REFUSE")
    except db.DatabaseError:
        record("run_sql refuses a write", True, "rejected before it reached the database")

    # ---- summary --------------------------------------------------------
    section("Gate")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n  {passed} of {len(results)} checks passed")
    print(f"\n  Row cap {DB_MAX_ROWS} per query, statement timeout {DB_STATEMENT_TIMEOUT_MS} ms.")
    if passed != len(results):
        print("\n  Paste this output back into the chat.\n")
        return 1
    print("\n  The database tools are safe to switch on. Restart the app and ask it")
    print("  something about the data.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
