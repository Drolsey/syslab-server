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


def _teardown_puts_the_tenant_back():
    """(tenant during the request, tenant after it), in THIS context.

    Driven by hand rather than through TestClient, and that is the point. A
    dependency running inside the client runs in its own task, whose context
    copy is discarded when the request ends -- so a require_tenant that never
    calls reset_tenant looks identical from outside to one that does, and a
    check written that way passes whether the teardown exists or not. This one
    was, and removing the reset entirely did not move it.

    require_tenant awaits nothing, so stepping its coroutine with send(None)
    runs the body here, in the caller's context, where a missing reset shows.
    """
    from starlette.requests import Request
    from app import plane

    scope = {
        "type": "http", "method": "GET", "path": "/api/v1/probe",
        "query_string": b"", "client": ("127.0.0.1", 1), "headers": [
            (b"authorization", b"Bearer a-service-token-for-the-website-long-enough"),
            (b"x-syslab-tenant", b"42"),
        ],
    }
    generator = plane.require_tenant(Request(scope))

    def step(coroutine):
        # StopIteration carries the yielded value on the way in; the generator
        # ending after its finally raises StopAsyncIteration on the way out.
        # Both mean "that step completed".
        try:
            coroutine.send(None)
        except (StopIteration, StopAsyncIteration):
            return
        raise AssertionError("require_tenant awaited something; drive it properly")

    # Returned, never raised. This runs inside the setup function that twelve
    # checks depend on, and this script's own rule is that nothing outside a
    # wrapper may raise: a crash here would report "could not run" against
    # every check below rather than naming the one thing that broke.
    try:
        step(generator.__anext__())
        during = context.tenant_if_set()
        step(generator.__anext__())      # runs the finally
        return during, context.tenant_if_set()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}", context.tenant_if_set()


def _load_tenant_cli():
    """scripts/tenant.py as a module, the way tests/test_tenant_delete.py loads it.

    It is a script rather than a package member, so it cannot simply be
    imported. Section 9b needs it because it is the only thing in this project
    that removes a tenant's storage from disk -- tenancy.delete_tenant removes
    rows and deliberately stops there.
    """
    import importlib.util

    path = Path(__file__).resolve().parent / "tenant.py"
    spec = importlib.util.spec_from_file_location("tenant_cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tenant_cli"] = module
    spec.loader.exec_module(module)
    return module


def _seed(tenant: str, tools, search) -> bool:
    """One tenant, one document, produced and indexed.

    Returns True rather than falling off the end. step() reads None as "it
    raised", so a setup function that succeeds and returns nothing reads as a
    failure, which is exactly what this one did first time.
    """
    with context.use_tenant(tenant):
        config.ensure_data_dir()
        tools.write_pdf(f"{tenant}_only.pdf", title=tenant, body="Nothing to see.")
        search.rebuild()
    return True


# --------------------------------------------------------------------------

def run(root: Path) -> int:
    from app import ingest, jobs, passages, producers, search, tools

    section("Where this is running")
    real = (config.DATA_ROOT, config.INDEX_ROOT, config.DERIVED_ROOT, config.CONTROL_PATH)
    real_data, real_index, real_derived, real_control = real
    config.DATA_ROOT = root / "data"
    config.INDEX_ROOT = root / "index"
    # Step 2 gave a tenant a fourth root, and this script did not know. It ran
    # for two days writing alpha/ and beta/ into the developer's real derived/
    # folder while its own docstring promised it never touches anything of
    # yours. A sandbox is a list of roots, and a list falls behind.
    config.DERIVED_ROOT = root / "derived"
    config.CONTROL_DIR = root / "control"
    config.CONTROL_PATH = root / "control" / "control.sqlite3"
    tenancy.CONTROL_PATH = config.CONTROL_PATH
    tenancy.ensure_control_dir = lambda: (root / "control").mkdir(parents=True, exist_ok=True)

    print(f"  Throwaway root: {root}")
    check("Your real data, index, derived and control plane are not in use",
          lambda: (
              all(
                  redirected.is_relative_to(root)
                  for redirected in (config.DATA_ROOT, config.INDEX_ROOT,
                                     config.DERIVED_ROOT, config.CONTROL_PATH)
              ),
              f"nothing outside {root.name} is opened "
              f"(your own stay at {real_data.name}/, {real_index.name}/, "
              f"{real_derived.name}/, {real_control.parent.name}/)",
              "one of the four roots is still pointing at this install",
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

    # ---- 3b. derived artifacts, Step 2.5 ---------------------------------
    #
    # The index has held one tenant's words since Step 1. Since Step 2 there is
    # a second thing on disk made out of a customer's documents -- the
    # extracted text itself, in full -- and it needs the same answers.
    section("3b. Derived artifacts")
    check("Each tenant has its own derived folder and manifest",
          lambda: (
              _as(A, lambda: config.manifest_path()) != _as(B, lambda: config.manifest_path())
              and _as(A, lambda: config.derived_dir()).name == A,
              "a folder each, never one folder with an owner column",
          ))
    check("The same filename holds each tenant's own text, not the other's",
          lambda: (
              (lambda a, b: a and b and "Aardvark" in a and "Bandicoot" not in a
               and "Bandicoot" in b and "Aardvark" not in b)(
                  _as(A, lambda: producers.text_of("shared.pdf")),
                  _as(B, lambda: producers.text_of("shared.pdf")),
              ),
              "shared.pdf produced twice, and neither artifact carries the other's words",
              "one tenant's extracted text was reachable as the other",
          ))
    check("A document only one tenant has is unknown to the other",
          lambda: (
              _as(A, lambda: ingest.status("alpha_only.pdf"))["ready"] is True
              and refused(lambda: _as(B, lambda: ingest.status("alpha_only.pdf")),
                          ingest.IngestError),
              "not found rather than forbidden: 'it exists but is not yours' "
              "confirms a filename someone guessed",
          ))
    check("Sweeping as one tenant never reaches the other's artifacts",
          lambda: (
              _as(B, lambda: ingest.forget_missing())["gone"] == []
              and _as(A, lambda: producers.text_of("alpha_only.pdf")) is not None,
              "forget_missing walks one tenant's folder and no other",
          ))
    check("A rebuild as one tenant produces nothing for the other",
          lambda: (
              _as(B, lambda: ingest.rebuild())["files_seen"] == 1
              and len(_as(A, lambda: list((config.derived_dir() / "text").iterdir()))) == 2,
              "Beta has one document, Alpha still has its two",
          ))

    # ---- 3c. passages, Step 4.4 ------------------------------------------
    #
    # A third thing on disk made out of a customer's documents, and the one
    # with the least distance between "a bug" and "a customer reads another
    # customer's contract". A leaked search result is a FILENAME. A leaked
    # passage is a paragraph of the document, quoted, already formatted to be
    # dropped into an answer.
    section("3c. Passages")
    for tenant in (A, B):
        _as(tenant, lambda: passages.rebuild())
    check("Each word is found only by the tenant whose passages hold it",
          lambda: (
              _as(A, lambda: passages.search_passages("Aardvark")["count"]) == 1
              and _as(A, lambda: passages.search_passages("Bandicoot")["count"]) == 0
              and _as(B, lambda: passages.search_passages("Bandicoot")["count"]) == 1
              and _as(B, lambda: passages.search_passages("Aardvark")["count"]) == 0,
              "the same query, run as each tenant, over the same filename",
              "one tenant's passages were readable as the other",
          ))
    check("A passage never comes back carrying the other tenant's words",
          lambda: (
              (lambda rows: bool(rows) and all(
                  "Bandicoot" not in row["text"] for row in rows))(
                  _as(A, lambda: passages.search_passages("Aardvark")["results"])),
              "asserted on the TEXT returned, not only on the count",
          ))
    # A CHUNK ID IS NOT GLOBALLY UNIQUE, and this check was written the wrong
    # way round first. It asserted that A's chunk_id does not resolve for B,
    # which failed -- and the failure was the gate being wrong, not the code.
    #
    # chunk_id is `source#ordinal`. Both tenants here have a shared.pdf, so
    # both indexes hold a row called `shared.pdf#0`, deliberately: there is one
    # index per tenant and the id is scoped to it. The property that matters is
    # not that the id is unrecognised, it is that resolving it gives the caller
    # THEIR OWN passage and never the other tenant's text -- which is the
    # stronger of the two claims and the one worth gating.
    #
    # Worth knowing at 4.6: a chunk_id in a log line or a cache key means
    # nothing without the tenant beside it.
    check("The same citation resolves to each tenant's own passage, never the other's",
          lambda: (
              (lambda cid, mine, theirs: bool(cid) and mine and theirs
               and "Aardvark" in mine["text"] and "Bandicoot" not in mine["text"]
               and "Bandicoot" in theirs["text"] and "Aardvark" not in theirs["text"])(
                  (cid := _as(A, lambda: (passages.search_passages("Aardvark")["results"]
                                          or [{}])[0].get("chunk_id"))),
                  _as(A, lambda: passages.passage(cid)),
                  _as(B, lambda: passages.passage(cid))),
              "shared.pdf#0 exists in both indexes on purpose; the id is scoped "
              "to a tenant, and 4.6 must not treat it as globally unique",
              "one tenant's passage was fetchable by the other with a guessable id",
          ))
    check("Both tables live in the tenant's own index file",
          lambda: (
              (lambda a, b: a.endswith(f"{A}.sqlite3") and b.endswith(f"{B}.sqlite3"))(
                  _as(A, lambda: passages.opened_path()),
                  _as(B, lambda: passages.opened_path())),
              "asked of SQLite via PRAGMA database_list, not of config -- reading "
              "index_path() to prove what connect() opened is how this check "
              "carried on passing with tenant scoping deliberately broken",
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

    # ---- 7b. the retrieval plane, which is how a WEBSITE arrives ---------
    #
    # Step 4.0. Section 7 above signs in with a token that already names a
    # tenant. This is the other door: a service token that names a SYSTEM, and
    # a header carrying that system's own id for a customer, which has to
    # become one of ours without ever becoming a path.
    #
    # The plane has no routes until 4.5, so this drives the real dependency on
    # a probe route of its own. What is under test is app/plane.require_tenant
    # and the tenant_alias bridge beneath it, which is all of 4.0 that exists.
    section("7b. The retrieval plane, which is how a website arrives")

    WEBSITE_TOKEN = "a-service-token-for-the-website-long-enough"
    PARTNER_TOKEN = "a-service-token-for-somebody-else-entirely"

    def plane_checks():
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient
        from app import plane

        # Two systems, so "their id 42" can be shown to mean two different
        # things. Only the website has anything linked.
        config.RETRIEVAL_TOKENS = {WEBSITE_TOKEN: "website", PARTNER_TOKEN: "partner"}

        tenancy.link_alias("website", "42", A, connection=connection)
        tenancy.link_alias("website", "77", B, connection=connection)

        probe = FastAPI()

        @probe.get("/api/v1/probe", dependencies=[Depends(plane.require_tenant)])
        def probe_route():
            return {
                "tenant": context.current_tenant(),
                "files": sorted(p.name for p in config.data_dir().iterdir()),
            }

        # raise_server_exceptions=False so a 500 arrives AS a 500. With the
        # default, one unhandled exception becomes a Python traceback out of
        # this setup function and takes all twelve checks below with it,
        # reporting "could not run" where the honest answer is "that request
        # returned 500 and here is which one".
        client = TestClient(probe, raise_server_exceptions=False)

        def ask(external_id, token=WEBSITE_TOKEN, send_header=True, auth=None,
                claim_system=None):
            # `auth` takes a RAW BYTES header value. Header values are bytes on
            # the wire and Starlette decodes them as latin-1, so a non-ASCII
            # token is something a real client can send and httpx will not
            # build from a str. Sending the bytes is the only honest way to
            # reach that path from here.
            headers = {"Authorization": auth if auth else f"Bearer {token}"}
            if send_header:
                headers["X-Syslab-Tenant"] = external_id
            if claim_system:
                # A header the plane must have no use for. If a future edit
                # ever reads one, this is what notices.
                headers["X-Syslab-System"] = claim_system
            return client.get("/api/v1/probe", headers=headers)

        before = {p.name for p in config.DATA_ROOT.iterdir()}
        traversal = ask("../../etc/passwd")
        after = {p.name for p in config.DATA_ROOT.iterdir()}

        config.RETRIEVAL_TOKENS = {}
        unconfigured = ask("42").status_code
        config.RETRIEVAL_TOKENS = {WEBSITE_TOKEN: "website", PARTNER_TOKEN: "partner"}

        tenancy.set_disabled(B, True, connection=connection)
        while_disabled = ask("77").status_code
        tenancy.set_disabled(B, False, connection=connection)

        return {
            "alpha": ask("42"),
            "beta": ask("77"),
            "unlinked": ask("9999").status_code,
            "traversal": traversal.status_code,
            "roots_unchanged": before == after,
            "spaces_and_capitals": ask("Acme Ltd").status_code,
            "our_own_id": ask(A).status_code,
            "wrong_system": ask("42", token=PARTNER_TOKEN).status_code,
            "claimed_system": ask("42", token=PARTNER_TOKEN,
                                  claim_system="website").status_code,
            "stranger": ask("42", token="not-a-service-token").status_code,
            "non_ascii_token": ask(
                "42", auth=b"Bearer \xfcnicode-token").status_code,
            "no_header": ask("42", send_header=False).status_code,
            "unconfigured": unconfigured,
            "while_disabled": while_disabled,
            "teardown": _teardown_puts_the_tenant_back(),
        }

    plane_result = step("Reach the plane as a website would", plane_checks)
    if plane_result is None:
        for name in ("A foreign id resolves to exactly one tenant's files",
                     "A second customer of the same system gets only their own",
                     "An unlinked foreign id is 404, not 403",
                     "A foreign id shaped like a path never becomes one",
                     "A foreign id with spaces and capitals is refused the same way",
                     "A foreign id that IS one of our tenant ids is still nothing",
                     "One system's token cannot resolve another system's id",
                     "A caller cannot name its own system",
                     "An unknown service token is refused",
                     "A bearer token holding non-ASCII is refused, not a crash",
                     "A request with no tenant header is 400, not a guess",
                     "With no service tokens configured the plane serves nobody",
                     "Disabling a tenant stops its alias resolving",
                     "No tenant survives the request"):
            skip(name, "the retrieval plane checks could not run")
    else:
        record("Reach the plane as a website would", PASSED,
               "two systems, two linked ids, one probe route")
        check("A foreign id resolves to exactly one tenant's files",
              lambda: (
                  plane_result["alpha"].status_code == 200
                  and plane_result["alpha"].json()["tenant"] == A
                  and set(plane_result["alpha"].json()["files"])
                  == {"shared.pdf", "alpha_only.pdf"},
                  f"website:42 -> {A}, and Alpha's two files",
                  f"got {plane_result['alpha'].status_code}: "
                  f"{plane_result['alpha'].text[:120]}",
              ))
        check("A second customer of the same system gets only their own",
              lambda: (
                  plane_result["beta"].status_code == 200
                  and plane_result["beta"].json()["tenant"] == B
                  and set(plane_result["beta"].json()["files"]) == {"shared.pdf"},
                  f"website:77 -> {B}, and none of Alpha's",
                  f"got {plane_result['beta'].status_code}: "
                  f"{plane_result['beta'].text[:120]}",
              ))
        check("An unlinked foreign id is 404, not 403",
              lambda: (plane_result["unlinked"] == 404,
                       "404: a 403 would confirm an id someone guessed",
                       f"got {plane_result['unlinked']}"))
        check("A foreign id shaped like a path never becomes one",
              lambda: (
                  plane_result["traversal"] == 404 and plane_result["roots_unchanged"],
                  "'../../etc/passwd' is just an id nothing linked, and nothing "
                  "was created on the way to saying so",
                  f"status {plane_result['traversal']}, "
                  f"roots unchanged: {plane_result['roots_unchanged']}",
              ))
        check("A foreign id with spaces and capitals is refused the same way",
              lambda: (plane_result["spaces_and_capitals"] == 404,
                       "'Acme Ltd' never reaches validate_tenant_id; it is a "
                       "lookup key that matched nothing",
                       f"got {plane_result['spaces_and_capitals']}"))
        check("A foreign id that IS one of our tenant ids is still nothing",
              lambda: (plane_result["our_own_id"] == 404,
                       f"{A!r} arriving in the header reaches no tenant: the bridge "
                       "is the only way across, and nothing linked it",
                       f"got {plane_result['our_own_id']} -- a foreign id was used "
                       "as a local one"))
        check("One system's token cannot resolve another system's id",
              lambda: (plane_result["wrong_system"] == 404,
                       "the partner's token asking for website:42 gets nothing",
                       f"got {plane_result['wrong_system']}"))
        check("A caller cannot name its own system",
              lambda: (plane_result["claimed_system"] == 404,
                       "the partner's token plus X-Syslab-System: website is still "
                       "the partner. The system comes from the token, and a header "
                       "that could override it would make the alias key decoration",
                       f"got {plane_result['claimed_system']}"))
        check("An unknown service token is refused",
              lambda: (plane_result["stranger"] == 401, "401",
                       f"got {plane_result['stranger']}"))
        check("A bearer token holding non-ASCII is refused, not a crash",
              lambda: (plane_result["non_ascii_token"] == 401,
                       "401: hmac.compare_digest raises on a non-ASCII str, which "
                       "made one accented letter a 500 from an unauthenticated caller",
                       f"got {plane_result['non_ascii_token']}"))
        check("A request with no tenant header is 400, not a guess",
              lambda: (plane_result["no_header"] == 400,
                       "400: there is no default tenant at this layer either",
                       f"got {plane_result['no_header']}"))
        check("With no service tokens configured the plane serves nobody",
              lambda: (plane_result["unconfigured"] == 503,
                       "503: it refuses to serve rather than serve without "
                       "knowing who is calling",
                       f"got {plane_result['unconfigured']}"))
        check("Disabling a tenant stops its alias resolving",
              lambda: (plane_result["while_disabled"] == 404,
                       "404, the same answer as never having been linked",
                       f"got {plane_result['while_disabled']}"))
        check("No tenant survives the request",
              lambda: (
                  plane_result["teardown"] == (A, None),
                  "set for the request, and put back after it",
                  f"expected (during, after) == ({A!r}, None), got "
                  f"{plane_result['teardown']!r}",
              ))

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

    # ---- 9. removing one ------------------------------------------------
    section("9. Removing a tenant")
    check("An active tenant cannot be deleted",
          lambda: (
              refused(lambda: tenancy.delete_tenant(A, connection=connection),
                      tenancy.TenancyError),
              "disable first is a deliberate two-step: one is reversible, this is not",
          ))

    removed = step("Delete the disabled tenant",
                   lambda: tenancy.delete_tenant(B, connection=connection))
    if removed is None:
        for name in ("Deleting one tenant takes its access with it",
                     "Deleting one tenant leaves the other entirely alone"):
            skip(name, "the delete did not complete")
    else:
        record("Delete the disabled tenant", PASSED,
               f"{removed['tokens']} token(s) and the tenant row")
        check("Deleting one tenant takes its access with it",
              lambda: (
                  tenancy.get_tenant(B, connection=connection) is None
                  and tenancy.resolve_token(b_token, connection=connection) is None,
                  "no tenant, no token",
              ))
        check("Deleting one tenant leaves the other entirely alone",
              lambda: (
                  tenancy.get_tenant(A, connection=connection) is not None
                  and tenancy.resolve_token(a_token, connection=connection) is not None
                  and (config.DATA_ROOT / A / "alpha_only.pdf").is_file()
                  and (config.INDEX_ROOT / f"{A}.sqlite3").exists()
                  and (config.DERIVED_ROOT / A / "manifest.sqlite3").exists(),
                  "Alpha's rows, token, files, index and artifacts are all still there",
              ))
        check("Deleting one tenant takes its aliases with it",
              lambda: (
                  tenancy.list_aliases(B, connection=connection) == []
                  and tenancy.resolve_alias("website", "77",
                                            connection=connection) is None,
                  "no orphaned row: a foreign id pointing at a tenant that no "
                  "longer exists is the one failure here that looks like a "
                  "working system",
              ))
        check("The deleted tenant's documents are still on disk",
              lambda: (
                  (config.DATA_ROOT / B / "shared.pdf").is_file(),
                  "delete_tenant removes ROWS. What happens to a customer's documents "
                  "is a separate judgement, made by scripts/tenant.py",
              ))
        check("And so are its derived artifacts, for the same reason",
              lambda: (
                  (config.DERIVED_ROOT / B).is_dir(),
                  "they hold the customer's own text, so they go when the documents "
                  "are dealt with, not when the rows are",
              ))

    # ---- 9b. and what scripts/tenant.py does with them -------------------
    #
    # The property Step 2.5 asks for -- deleting a tenant takes its derived/
    # with it -- belongs to the CLI, because that is the only thing here that
    # removes anything from disk. It needs a tenant of its own: the CLI refuses
    # an id the control plane no longer has, and section 9 has already taken
    # Beta's rows. Loaded the way tests/test_tenant_delete.py loads it, and
    # wrapped, because a gate must not crash on the way to its last check.
    section("9b. Removing a tenant's storage")
    C = "gamma"
    cli = step("Load scripts/tenant.py", _load_tenant_cli)
    ready = cli is not None and step("Give it a third tenant to remove", lambda: (
        tenancy.create_tenant("Gamma Ltd", tenant_id=C, connection=connection),
        _seed(C, tools, search),
        tenancy.set_disabled(C, True, connection=connection),
        True,
    )[-1])

    if not ready:
        for name in ("Removing a tenant takes its derived artifacts with it",
                     "And leaves the surviving tenant's alone"):
            skip(name, "the CLI or its tenant could not be set up")
    else:
        cli.DATA_ROOT = config.DATA_ROOT
        cli.INDEX_ROOT = config.INDEX_ROOT
        cli.DERIVED_ROOT = config.DERIVED_ROOT
        cli._app_is_running = lambda: False
        record("Load scripts/tenant.py", PASSED, "pointed at the throwaway roots")
        record("Give it a third tenant to remove", PASSED,
               f"{C}, disabled, with a document and its artifacts")

        had = (config.DERIVED_ROOT / C).is_dir()
        code = step("Remove it", lambda: cli.main(
            ["delete", C, "--apply", "--confirm", C]))
        check("Removing a tenant takes its derived artifacts with it",
              lambda: (
                  had and code == 0 and not (config.DERIVED_ROOT / C).exists(),
                  "deleted outright even though the documents are only moved aside: "
                  "everything in derived/ can be made again from them",
                  "the artifacts outlived the tenant" if had
                  else "there were none to remove, so this proved nothing",
              ))
        check("And leaves the surviving tenant's alone",
              lambda: (
                  (config.DERIVED_ROOT / A / "manifest.sqlite3").exists()
                  and (config.DERIVED_ROOT / A / "text" / "alpha_only.pdf").is_dir(),
                  f"Alpha's manifest and artifacts survived {C}'s removal",
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
