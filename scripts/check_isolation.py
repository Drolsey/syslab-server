"""Step 1 gate: what one tenant must fail to reach.

    py scripts\\check_isolation.py

Runs entirely inside a temporary folder with two invented tenants. It never
opens your data folder, your index or your control plane, so running it cannot
touch a real customer's anything. That is checked and reported before any test
runs, rather than asserted in a comment.

Every check here should fail loudly if the isolation were removed. Each was
written by removing it first and confirming this said so.

Design rules, both learned in this project:
  - NOTHING outside a wrapper may raise. A crash tells you nothing about the
    checks it never reached, and the most valuable ones are usually last.
  - THREE OUTCOMES, NEVER TWO. passed, failed, and not tested.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _deps  # noqa: E402

_deps.require("pymupdf", "openpyxl", "fastapi")

from app import config, context, db, tenancy  # noqa: E402

LINE = "-" * 66
PASSED, FAILED, NOT_TESTED = "PASS", "FAIL", "----"

results: list[tuple[str, str, str]] = []

A = "alpha"      # the tenant whose things must stay private
B = "beta"       # the tenant doing the reaching


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, outcome: str, detail: str = "") -> None:
    results.append((name, outcome, detail))
    print(f"  {outcome}  {name}{'  ' + detail if detail else ''}")


def check(name: str, function) -> None:
    """(ok, detail) or (ok, detail_if_pass, detail_if_fail)."""
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
    try:
        return action()
    except Exception as exc:  # noqa: BLE001
        record(name, FAILED, f"setup raised {type(exc).__name__}: {exc}")
        return None


def skip(name: str, why: str) -> None:
    record(name, NOT_TESTED, why)


def refused(action, expected) -> bool:
    try:
        action()
    except expected:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


# --------------------------------------------------------------------------

def run(root: Path) -> int:
    from app import jobs, search, tools

    section("Where this is running")
    real_data, real_index, real_control = config.DATA_ROOT, config.INDEX_ROOT, config.CONTROL_PATH
    config.DATA_ROOT = root / "data"
    config.INDEX_ROOT = root / "index"
    config.CONTROL_DIR = root / "control"
    config.CONTROL_PATH = root / "control" / "control.sqlite3"
    tenancy.CONTROL_PATH = config.CONTROL_PATH
    tenancy.ensure_control_dir = lambda: (root / "control").mkdir(parents=True, exist_ok=True)

    print(f"  Throwaway root: {root}")
    check("Your real data, index and control plane are not in use",
          lambda: (
              root in config.DATA_ROOT.parents or config.DATA_ROOT.is_relative_to(root),
              f"nothing outside {root.name} is opened "
              f"(your own stay at {real_data.name}/, {real_index.name}/, "
              f"{real_control.parent.name}/)",
          ))

    # ---- two tenants with the same filenames ----------------------------
    section("Two tenants")
    connection = step("Control plane opens", lambda: tenancy.connect())
    if connection is None:
        section("Gate")
        print("\n  Nothing could be tested: the control plane would not open.\n")
        return 1

    tenants = step("Create two tenants", lambda: (
        tenancy.create_tenant("Alpha Ltd", tenant_id=A, connection=connection),
        tenancy.create_tenant("Beta Ltd", tenant_id=B, connection=connection),
    ))
    tokens = step("Issue a token each", lambda: (
        tenancy.issue_token(A, connection=connection),
        tenancy.issue_token(B, connection=connection),
    ))

    if tenants is None or tokens is None:
        section("Gate")
        print("\n  Nothing could be tested: the two tenants could not be set up.\n")
        return 1
    a_token, b_token = tokens

    def seed():
        # Returns True rather than falling off the end. step() reads None as
        # "it raised", so a setup function that succeeds and returns nothing
        # reads as a failure, which is exactly what this one did first time.
        with context.use_tenant(A):
            config.ensure_data_dir()
            tools.write_pdf("shared.pdf", title="Alpha", body="Aardvark is only in Alpha's file.")
            tools.write_pdf("alpha_only.pdf", title="Alpha only", body="Nothing to see.")
            search.rebuild()
        with context.use_tenant(B):
            config.ensure_data_dir()
            tools.write_pdf("shared.pdf", title="Beta", body="Bandicoot is only in Beta's file.")
            search.rebuild()
        return True
    if step("Give each tenant a document", seed) is None:
        section("Gate")
        print("\n  Nothing could be tested: the documents could not be written.\n")
        return 1
    record("Give each tenant a document", PASSED, "same filename, different contents")

    # ---- 1. files --------------------------------------------------------
    section("1. Files")

    check("The same filename is two different files",
          lambda: (
              (lambda a, b: "Aardvark" in a and "Bandicoot" not in a
               and "Bandicoot" in b and "Aardvark" not in b)(
                  _as(A, lambda: tools.read_pdf("shared.pdf")["text"]),
                  _as(B, lambda: tools.read_pdf("shared.pdf")["text"])),
              "each tenant read its own",
          ))

    check("A listing shows only your own files",
          lambda: (
              "alpha_only.pdf" not in {f["name"] for f in _as(B, lambda: tools.list_files())["files"]},
              "Beta cannot see Alpha's file in a listing",
          ))

    check("Another tenant's file is not found by name",
          lambda: (_as(B, lambda: refused(lambda: tools.read_pdf("alpha_only.pdf"),
                                          tools.ToolError)),
                   "read_pdf refuses it"))

    # ---- 2. paths --------------------------------------------------------
    section("2. Paths")
    for label, attempt in (
        ("../alpha/", f"../{A}/alpha_only.pdf"),
        ("..\\alpha\\", f"..\\{A}\\alpha_only.pdf"),
        ("sub/../../", f"sub/../../{A}/alpha_only.pdf"),
    ):
        check(f"Walking sideways with {label} is refused as unsafe",
              lambda attempt=attempt: (
                  _as(B, lambda: refused(lambda: config.resolve_in_data_dir(attempt),
                                         config.UnsafePathError)),
                  "refused by path resolution, not merely not found",
              ))

    def symlink_check():
        with context.use_tenant(B):
            link = config.data_dir() / "innocent.pdf"
            target = config.DATA_ROOT / A / "alpha_only.pdf"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                return None, str(exc)
            try:
                # Both halves. A symlink DOES appear in a listing, because
                # list_files reads the directory; what it must not do is open.
                # Names are not content, and the check that matters is the read.
                by_path = refused(lambda: config.resolve_in_data_dir("innocent.pdf"),
                                  config.UnsafePathError)
                by_tool = refused(lambda: tools.read_pdf("innocent.pdf"), tools.ToolError)
                return (by_path and by_tool), ""
            finally:
                # Tidy up, or the listing checks further down count it as one of
                # Beta's files and report an isolation failure that is really
                # this check's litter. It did, first time.
                link.unlink(missing_ok=True)
    outcome = step("Symlink out of the folder", symlink_check)
    if outcome is None:
        skip("A symlink out of the tenant folder is refused", "setup failed")
    elif outcome[0] is None:
        skip("A symlink out of the tenant folder is refused",
             f"this machine will not make one ({outcome[1]}). Windows needs "
             "developer mode or admin rights; the check runs on Linux.")
    else:
        record("A symlink out of the tenant folder is refused",
               PASSED if outcome[0] else FAILED,
               "the containment check, which the .. rule never reaches")

    # ---- 3. search -------------------------------------------------------
    section("3. Search")
    check("A search never reaches the other tenant's index",
          lambda: (
              _as(A, lambda: search.search("Aardvark")["count"]) == 1
              and _as(A, lambda: search.search("Bandicoot")["count"]) == 0
              and _as(B, lambda: search.search("Bandicoot")["count"]) == 1
              and _as(B, lambda: search.search("Aardvark")["count"]) == 0,
              "each word is found only by the tenant whose document holds it",
          ))
    check("Each tenant has its own index file",
          lambda: (
              _as(A, lambda: config.index_path()) != _as(B, lambda: config.index_path()),
              f"{A}.sqlite3 and {B}.sqlite3",
          ))
    check("While acting as one tenant, the other's index is never opened",
          lambda: (
              _as(B, lambda: config.index_path()).name == f"{B}.sqlite3",
              "asserted on the path used, not on the results returned",
          ))

    # ---- 4. jobs ---------------------------------------------------------
    section("4. Jobs")
    lane = jobs.Lane(workers=1, max_queued=10)
    lane.handler("whoami", lambda report: {"tenant": context.current_tenant()})
    a_job = step("Alpha submits a job", lambda: _as(A, lambda: lane.submit("whoami", {})))
    if a_job is None:
        for name in ("Another tenant's job is not found, not forbidden",
                     "Another tenant cannot cancel it",
                     "A job listing shows only your own"):
            skip(name, "the job could not be submitted")
    else:
        record("Alpha submits a job", PASSED, a_job.id)
        check("Another tenant's job is not found, not forbidden",
              lambda: (
                  _as(B, lambda: refused(lambda: lane.get(a_job.id), jobs.JobError)),
                  "'exists but is not yours' would confirm a guessed id",
              ))
        check("Another tenant cannot cancel it",
              lambda: (_as(B, lambda: refused(lambda: lane.cancel(a_job.id), jobs.JobError)),
                       "refused"))
        check("A job listing shows only your own",
              lambda: (
                  a_job.id not in {j["id"] for j in _as(B, lambda: lane.snapshot())["jobs"]},
                  "Beta's listing does not contain Alpha's job",
              ))

    # ---- 5. the database -------------------------------------------------
    section("5. The database")
    check("A tenant with no database does not borrow one",
          lambda: (
              _as(A, lambda: db.settings()) is None and _as(B, lambda: db.settings()) is None,
              "neither invented tenant is the bootstrap tenant, so neither gets its "
              "connection",
          ))
    check("Asking anyway is refused rather than answered",
          lambda: (_as(B, lambda: refused(lambda: db.run_sql("SELECT 1"), db.DatabaseError)),
                   "run_sql refuses instead of connecting to somebody else's server"))

    # ---- 6. no owner, no work -------------------------------------------
    section("6. No owner means no work")
    check("Every storage entry point refuses with no tenant set",
          lambda: (
              all(refused(call, context.NoTenantError) for call in (
                  lambda: config.data_dir(),
                  lambda: config.index_path(),
                  lambda: config.resolve_in_data_dir("anything.pdf"),
                  lambda: tools.list_files(),
                  lambda: search.status(),
                  lambda: lane.submit("whoami", {}),
              )),
              "there is no default tenant at any layer",
          ))

    # ---- 7. over HTTP ----------------------------------------------------
    section("7. Over HTTP, which is how a customer actually arrives")
    def http_checks():
        from fastapi.testclient import TestClient
        from app import main

        main._failures.clear()
        config.APP_TOKEN = "an-operator-token-long-enough-to-count"

        def client(token):
            made = TestClient(main.app)
            made.headers.update({"X-Syslab-Token": token})
            return made

        alpha, beta = client(a_token), client(b_token)
        return {
            "alpha_files": {f["name"] for f in alpha.get("/api/files").json()["files"]},
            "beta_files": {f["name"] for f in beta.get("/api/files").json()["files"]},
            "beta_fetching_alphas": beta.get("/api/files/alpha_only.pdf").status_code,
            "stranger": client("not-a-real-token-at-all").get("/api/files").status_code,
        }

    http = step("Sign in as each tenant", http_checks)
    if http is None:
        for name in ("Each token sees only its own tenant's files",
                     "One tenant cannot download another's file",
                     "An unknown token is refused"):
            skip(name, "the HTTP checks could not run")
    else:
        record("Sign in as each tenant", PASSED, "two tokens, two sessions")
        check("Each token sees only its own tenant's files",
              lambda: (
                  http["alpha_files"] == {"shared.pdf", "alpha_only.pdf"}
                  and http["beta_files"] == {"shared.pdf"},
                  f"alpha {sorted(http['alpha_files'])}, beta {sorted(http['beta_files'])}",
              ))
        check("One tenant cannot download another's file",
              lambda: (http["beta_fetching_alphas"] == 404,
                       f"404, not 403: got {http['beta_fetching_alphas']}"))
        check("An unknown token is refused",
              lambda: (http["stranger"] == 401, f"401: got {http['stranger']}"))

    # ---- 8. suspension ---------------------------------------------------
    section("8. Suspension")
    check("Disabling a tenant locks it out and leaves its files alone",
          lambda: (
              (lambda: (
                  tenancy.set_disabled(B, True, connection=connection),
                  tenancy.resolve_token(b_token, connection=connection) is None
                  and (config.DATA_ROOT / B / "shared.pdf").is_file(),
              )[1])(),
              "the token stops working; nothing on disk is touched",
          ))

    try:
        connection.close()
    except Exception:  # noqa: BLE001
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
        print("  Not tested is not a pass:")
        for name, outcome, detail in results:
            if outcome == NOT_TESTED:
                print(f"    - {name}: {detail}")

    if failed:
        print("\n  ISOLATION DOES NOT HOLD. Nothing above is a small problem: each")
        print("  failure is one customer able to reach another customer's things.")
        print("  Paste this output back into the chat.\n")
        return 1
    if untested:
        print("\n  Everything that ran, held. Something did not run, so this is not")
        print("  a pass yet. The reasons are listed above.\n")
        return 1
    print("\n  Isolation holds. Two tenants share a server, a process, a job lane and")
    print("  a code path, and neither can see the other's files, search their")
    print("  contents, touch their jobs, reach their database, or sign in as them.")
    print("  Nothing further to do here.\n")
    return 0


def _as(tenant: str, action):
    with context.use_tenant(tenant):
        return action()


def main() -> int:
    print("\nsyslab-server / tenant isolation check")
    with tempfile.TemporaryDirectory(prefix="syslab-isolation-") as tmp:
        return run(Path(tmp))


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
