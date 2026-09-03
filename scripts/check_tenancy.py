"""Step 1.0 gate: does the control plane know who is who, and refuse everyone else?

    py scripts\\check_tenancy.py

Runs entirely against a throwaway control file in a temporary folder. It never
opens, reads or writes control/control.sqlite3, so running it cannot disturb a
real tenant.

Two design rules this file follows, both learned the hard way in this project:

- NOTHING outside a wrapper may raise. An earlier version called revoke_token
  directly during setup. Under a deliberately broken build that call threw, the
  run stopped, and the most important check in the file, the one that looks for
  plaintext tokens on disk, was never reached. A crash tells you nothing about
  the checks it never got to.
- THREE OUTCOMES, NEVER TWO. passed, failed, and not tested. A check whose setup
  did not happen is not a pass and is not a failure.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tenancy  # noqa: E402
from app.config import CONTROL_PATH  # noqa: E402

LINE = "-" * 62
PASSED, FAILED, NOT_TESTED = "PASS", "FAIL", "----"

results: list[tuple[str, str, str]] = []

# Every plaintext token this run has ever created, so the disk scan at the end
# can look for all of them whatever else went wrong.
issued: list[tuple[str, str]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, outcome: str, detail: str = "") -> None:
    """Print one line. The detail is printed exactly as given, so whoever wrote
    it owns whether it reads correctly next to its own verdict."""
    results.append((name, outcome, detail))
    print(f"  {outcome}  {name}{'  ' + detail if detail else ''}")


def check(name: str, function) -> None:
    """One assertion. A leaked exception is a FAIL naming the type, never a
    traceback that stops the checks below it from running.

    The function returns (ok, detail) or (ok, detail_when_it_passes,
    detail_when_it_fails). With two values the failing line is prefixed with
    "expected:", because a bare success phrase under a FAIL is a sentence that
    argues with itself and an earlier version of this file printed exactly
    that. With three, the check has an observation of its own to report and it
    is printed untouched.
    """
    try:
        ok, *details = function()
        if ok:
            record(name, PASSED, details[0])
        elif len(details) > 1:
            record(name, FAILED, details[1])
        else:
            record(name, FAILED, f"expected: {details[0]}")
    except Exception as exc:  # noqa: BLE001
        record(name, FAILED, f"leaked a raw {type(exc).__name__}: {exc}")


def step(name: str, action):
    """Setup. Returns the result, or None having recorded a failure.

    Everything the checks depend on goes through here, so that a broken build
    produces a report rather than a traceback.
    """
    try:
        return action()
    except Exception as exc:  # noqa: BLE001
        record(name, FAILED, f"setup raised {type(exc).__name__}: {exc}")
        return None


def skip(name: str, why: str) -> None:
    record(name, NOT_TESTED, why)


def refuses(action) -> bool:
    """True if the action refused deliberately, by raising TenancyError.

    A refusal that instead reaches the database and fails there is not a
    deliberate refusal and must not read as one.
    """
    try:
        action()
    except tenancy.TenancyError:
        return True
    except sqlite3.Error:
        return False
    return False


def issue(tenant_id: str, label: str, connection) -> str | None:
    token = tenancy.issue_token(tenant_id, label=label, connection=connection)
    issued.append((label, token))
    return token


# --------------------------------------------------------------------------

def run(path: Path) -> int:
    connection = step("Control plane opens", lambda: tenancy.connect(path))
    if connection is None:
        section("Gate")
        print("\n  The control plane would not open, so nothing else could be tested.\n")
        return 1
    record("Control plane opens", PASSED, str(path.name))

    # ---- two tenants -----------------------------------------------------
    section("Two tenants")
    acme = step("Create tenant A",
                lambda: tenancy.create_tenant("Acme Ltd", connection=connection))
    globex = step("Create tenant B",
                  lambda: tenancy.create_tenant("Globex Corporation", connection=connection))

    if acme and globex:
        check("Two tenants exist with different ids",
              lambda: (acme["id"] != globex["id"], f"{acme['id']} and {globex['id']}"))
        check("An id gives nothing away about the customer",
              lambda: (
                  "acme" not in acme["id"].lower()
                  and "globex" not in globex["id"].lower()
                  and len(acme["id"]) >= 8,
                  "generated, not derived from the name",
              ))
        check("A duplicate id is refused",
              lambda: (refuses(lambda: tenancy.create_tenant(
                  "Acme again", tenant_id=acme["id"], connection=connection)),
                  "the second use of an id is rejected"))
    else:
        for name in ("Two tenants exist with different ids",
                     "An id gives nothing away about the customer",
                     "A duplicate id is refused"):
            skip(name, "a tenant could not be created")

    check("A hand-given id that could escape a folder is refused",
          lambda: (refuses(lambda: tenancy.create_tenant(
              "Evil", tenant_id="../secrets", connection=connection)),
              "'../secrets' rejected before it could become a path"))

    # ---- tokens ----------------------------------------------------------
    section("Tokens")
    acme_token = step("Issue a token to A",
                      lambda: issue(acme["id"], "acme laptop", connection)) if acme else None
    globex_token = step("Issue a token to B",
                        lambda: issue(globex["id"], "globex server", connection)) if globex else None

    if acme_token and globex_token:
        check("A token resolves to its own tenant",
              lambda: (
                  tenancy.resolve_token(acme_token, connection=connection)["id"] == acme["id"]
                  and tenancy.resolve_token(globex_token, connection=connection)["id"] == globex["id"],
                  "each token returned the tenant it was issued for",
              ))
        check("A token NEVER resolves to the other tenant",
              lambda: (
                  tenancy.resolve_token(acme_token, connection=connection)["id"] != globex["id"],
                  "this is the whole point of the file",
              ))
        check("A truncated or padded token is refused",
              lambda: (
                  tenancy.resolve_token(acme_token[:-1], connection=connection) is None
                  and tenancy.resolve_token(acme_token + "x", connection=connection) is None,
                  "the whole token must match, not a prefix of it",
              ))
    else:
        for name in ("A token resolves to its own tenant",
                     "A token NEVER resolves to the other tenant",
                     "A truncated or padded token is refused"):
            skip(name, "a token could not be issued")

    check("An unknown token is refused",
          lambda: (tenancy.resolve_token("not-a-real-token", connection=connection) is None,
                   "no tenant returned"))
    check("An empty or malformed token is refused",
          lambda: (
              tenancy.resolve_token("", connection=connection) is None
              and tenancy.resolve_token(None, connection=connection) is None
              and tenancy.resolve_token(12345, connection=connection) is None,
              "empty string, None and a non-string all returned no tenant",
          ))

    # ---- revocation ------------------------------------------------------
    section("Revocation and suspension")
    if acme_token:
        fingerprint = tenancy.token_hash(acme_token)[:12]
        revoked = step("Revoke A's token",
                       lambda: tenancy.revoke_token(fingerprint, connection=connection))
        if revoked is not None:
            check("A revoked token stops working",
                  lambda: (tenancy.resolve_token(acme_token, connection=connection) is None,
                           f"{fingerprint} no longer resolves"))
        else:
            skip("A revoked token stops working", "the revoke call did not complete")
    else:
        skip("A revoked token stops working", "no token to revoke")

    if globex_token:
        check("Revoking one token leaves the other alone",
              lambda: (tenancy.resolve_token(globex_token, connection=connection) is not None,
                       "B is still signed in"))
    else:
        skip("Revoking one token leaves the other alone", "no second token")

    check("An ambiguous revoke is refused rather than guessed",
          lambda: (refuses(lambda: tenancy.revoke_token("a", connection=connection)),
                   "a too-short fingerprint is rejected, not resolved to the first match"))

    if globex and globex_token:
        suspended = step("Disable tenant B",
                         lambda: tenancy.set_disabled(globex["id"], True, connection=connection))
        if suspended is not None:
            check("A disabled tenant's live token stops working",
                  lambda: (tenancy.resolve_token(globex_token, connection=connection) is None,
                           "suspension bites without revoking anything"))
            check("Issuing a token for a disabled tenant is refused",
                  lambda: (refuses(lambda: tenancy.issue_token(
                      globex["id"], connection=connection)),
                      "no token is handed out for an account that cannot be used"))
            restored = step("Enable tenant B",
                            lambda: tenancy.set_disabled(globex["id"], False,
                                                         connection=connection))
            if restored is not None:
                check("Enabling restores the same token",
                      lambda: (
                          tenancy.resolve_token(globex_token, connection=connection)["id"]
                          == globex["id"],
                          "suspension is reversible and destroys nothing",
                      ))
            else:
                skip("Enabling restores the same token", "could not re-enable")
        else:
            for name in ("A disabled tenant's live token stops working",
                         "Issuing a token for a disabled tenant is refused",
                         "Enabling restores the same token"):
                skip(name, "could not disable the tenant")
    else:
        for name in ("A disabled tenant's live token stops working",
                     "Issuing a token for a disabled tenant is refused",
                     "Enabling restores the same token"):
            skip(name, "no second tenant with a token")

    # ---- the one that matters most --------------------------------------
    # Deliberately last in the report and deliberately independent of every
    # check above: it looks for whatever tokens actually got issued, so it
    # still runs when the rest of the file has fallen over.
    section("Is the file safe to lose?")
    if acme:
        step("Issue one more token", lambda: issue(acme["id"], "freshly issued", connection))
    try:
        connection.commit()
    except sqlite3.Error:
        pass

    def no_plaintext():
        if not issued:
            return (False, "", "no token was issued, so this proves nothing either way")
        blob = b""
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.exists():
                blob += candidate.read_bytes()
        found = [label for label, value in issued if value.encode("utf-8") in blob]
        if found:
            return (False, "",
                    f"FOUND IN PLAINTEXT ON DISK: {', '.join(found)}. This file is "
                    "a set of keys, not a list of hashes.")
        return (True, f"none of the {len(issued)} issued token(s) appear in the file")

    check("No token is stored in plaintext", no_plaintext)

    def only_the_hash():
        if not issued:
            return (False, "", "no token was issued")
        blob = b""
        for candidate in (path, Path(str(path) + "-wal")):
            if candidate.exists():
                blob += candidate.read_bytes()
        hashes = [tenancy.token_hash(value) for _, value in issued]
        present = [h for h in hashes if h.encode("ascii") in blob]
        if len(present) == len(hashes):
            return (True, f"all {len(hashes)} token(s) are on disk as their SHA-256")
        return (False, "",
                f"only {len(present)} of {len(hashes)} token(s) are on disk as a "
                "SHA-256, so something else is being stored instead")

    check("Only the hash is stored", only_the_hash)

    check("Nothing can print a token back",
          lambda: (
              all(key in {"fingerprint", "tenant_id", "label", "created_at",
                          "last_seen_at", "revoked_at", "active"}
                  for row in tenancy.list_tokens(connection=connection) for key in row),
              "list_tokens returns metadata and a fingerprint, never a value",
          ))

    # ---- fail-closed on a future schema ----------------------------------
    section("Fail-closed")

    def refuses_newer_schema():
        connection.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                           (str(tenancy.SCHEMA_VERSION + 1),))
        connection.commit()
        second = None
        try:
            second = tenancy.connect(path)
            return False, "refused rather than risk half-migrating tenants"
        except tenancy.TenancyError:
            return True, "refused rather than risk half-migrating tenants"
        finally:
            if second is not None:
                second.close()
            connection.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                               (str(tenancy.SCHEMA_VERSION),))
            connection.commit()

    check("A control plane from a newer build is refused", refuses_newer_schema)

    try:
        connection.close()
    except sqlite3.Error:
        pass

    # ---- summary ---------------------------------------------------------
    section("Gate")
    for name, outcome, _ in results:
        print(f"  {outcome}  {name}")
    passed = sum(1 for _, o, _ in results if o == PASSED)
    failed = sum(1 for _, o, _ in results if o == FAILED)
    untested = sum(1 for _, o, _ in results if o == NOT_TESTED)

    print(f"\n  {passed} passed, {failed} failed, {untested} not tested")
    if untested:
        print("  Not tested is not a pass. Something earlier did not happen:")
        for name, outcome, detail in results:
            if outcome == NOT_TESTED:
                print(f"    - {name}: {detail}")

    if failed or untested:
        print("\n  The control plane does not hold yet. Paste this output back into the chat.\n")
        return 1
    print("\n  The control plane holds: tenants are distinct, a token reaches exactly")
    print("  one of them, revocation and suspension both bite, and the file on disk")
    print("  is a list of hashes rather than a set of keys. Nothing further to do here.\n")
    return 0


def main() -> int:
    print("\nsyslab-server / control plane check")
    with tempfile.TemporaryDirectory(prefix="syslab-tenancy-") as tmp:
        path = Path(tmp) / "control.sqlite3"
        print(f"  Throwaway control plane: {path}")
        print(f"  Your real one at {CONTROL_PATH} is not opened by this check.")
        return run(path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"\n  This check crashed, which is a bug in the check itself: "
              f"{type(exc).__name__}: {exc}\n")
        traceback.print_exc()
        raise SystemExit(2)
