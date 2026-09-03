"""What one tenant must not be able to reach.

These are the checks that only mean something once storage is per tenant. The
full adversarial gate arrives with sub-step 1.8, when jobs, auth and the
database are tenant-aware too; this file covers what 1.2 makes true: files,
paths, and the search index.

Every test here should fail loudly if the isolation is removed. Several were
written by removing it first.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from app import agent, config, context, search, tools
from tests.conftest import OTHER_TENANT, TEST_TENANT


@pytest.fixture()
def two_tenants(tenant_storage):
    """One document each, under the same filename, in two tenants."""
    tools.write_pdf("shared_name.pdf", title="Belongs to A",
                    body="Aardvark is a word only tenant A's file contains.")
    with context.use_tenant(OTHER_TENANT):
        config.ensure_data_dir()
        tools.write_pdf("shared_name.pdf", title="Belongs to B",
                        body="Bandicoot is a word only tenant B's file contains.")
        tools.write_pdf("b_only.pdf", title="B only", body="Nothing to see.")
    return tenant_storage


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def test_the_same_filename_is_two_different_files(two_tenants):
    mine = tools.read_pdf("shared_name.pdf")["text"]
    with context.use_tenant(OTHER_TENANT):
        theirs = tools.read_pdf("shared_name.pdf")["text"]
    assert "Aardvark" in mine and "Bandicoot" not in mine
    assert "Bandicoot" in theirs and "Aardvark" not in theirs


def test_a_listing_shows_only_this_tenants_files(two_tenants):
    mine = {f["name"] for f in tools.list_files()["files"]}
    assert "shared_name.pdf" in mine
    assert "b_only.pdf" not in mine, "that file belongs to the other tenant"


def test_the_other_tenants_file_is_simply_not_found(two_tenants):
    with pytest.raises(tools.ToolError):
        tools.read_pdf("b_only.pdf")


@pytest.mark.parametrize("escape", [
    "../{other}/b_only.pdf",
    "..\\{other}\\b_only.pdf",
    "./../{other}/b_only.pdf",
    "sub/../../{other}/b_only.pdf",
])
def test_walking_sideways_into_another_tenant_is_refused(two_tenants, escape):
    """Refused as unsafe, specifically.

    An earlier version of this test asserted only that read_pdf raised
    ToolError, which "no file named that" also satisfies. It therefore passed
    while pointing at a real file in another tenant's folder, for the wrong
    reason. Assert the refusal itself, at the function that makes it.
    """
    name = escape.format(other=OTHER_TENANT)
    with pytest.raises(config.UnsafePathError):
        config.resolve_in_data_dir(name)
    with pytest.raises(tools.ToolError) as caught:
        tools.read_pdf(name)
    assert "outside the data folder" in str(caught.value)


def test_a_symlink_out_of_the_tenant_folder_is_refused(two_tenants):
    """The containment check, which the `..` rule above never reaches.

    Every path in the test above is stopped by the explicit `..` rejection
    earlier in resolve_in_data_dir, so none of them exercises the final check
    that the resolved path is really inside this tenant's folder. A symlink
    does: it holds no `..`, and it resolves somewhere else entirely. Without
    this test, changing that check to compare against the ROOT instead of the
    tenant folder passes the whole suite, which is exactly what happened.

    Windows needs developer mode or admin rights to create a symlink, so this
    skips there rather than failing for a reason that is not about tenancy.
    """
    mine = config.data_dir()
    target = config.DATA_ROOT / OTHER_TENANT / "b_only.pdf"
    link = mine / "innocent.pdf"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")

    with pytest.raises(config.UnsafePathError):
        config.resolve_in_data_dir("innocent.pdf")
    with pytest.raises(tools.ToolError):
        tools.read_pdf("innocent.pdf")


def test_the_two_folders_are_actually_different_places(two_tenants):
    mine = config.data_dir()
    with context.use_tenant(OTHER_TENANT):
        theirs = config.data_dir()
    assert mine != theirs
    assert mine.parent == theirs.parent == config.DATA_ROOT


# --------------------------------------------------------------------------
# the search index
# --------------------------------------------------------------------------

def test_a_search_never_reaches_the_other_tenants_index(two_tenants):
    search.rebuild()
    with context.use_tenant(OTHER_TENANT):
        search.rebuild()

    assert search.search("Aardvark")["count"] == 1
    assert search.search("Bandicoot")["count"] == 0, "that word is only in B's document"

    with context.use_tenant(OTHER_TENANT):
        assert search.search("Bandicoot")["count"] == 1
        assert search.search("Aardvark")["count"] == 0


def test_each_tenant_has_its_own_index_file(two_tenants):
    mine = config.index_path()
    with context.use_tenant(OTHER_TENANT):
        theirs = config.index_path()
    assert mine != theirs
    assert mine.name == f"{TEST_TENANT}.sqlite3"
    assert theirs.name == f"{OTHER_TENANT}.sqlite3"


def test_deleting_one_tenants_index_leaves_the_other_alone(two_tenants):
    search.rebuild()
    with context.use_tenant(OTHER_TENANT):
        search.rebuild()
        assert search.status()["documents"] > 0

    config.index_path().unlink()
    assert search.status()["documents"] == 0
    with context.use_tenant(OTHER_TENANT):
        assert search.status()["documents"] > 0


# --------------------------------------------------------------------------
# no owner means no work
# --------------------------------------------------------------------------

def test_every_storage_entry_point_refuses_without_a_tenant(tenant_storage):
    with context.no_tenant():
        for call in (
            lambda: config.data_dir(),
            lambda: config.index_path(),
            lambda: config.ensure_data_dir(),
            lambda: config.resolve_in_data_dir("anything.pdf"),
            lambda: tools.list_files(),
            lambda: search.status(),
        ):
            with pytest.raises(context.NoTenantError):
                call()


# --------------------------------------------------------------------------
# the agent's own thread pool
# --------------------------------------------------------------------------

def test_parallel_tool_calls_stay_in_the_calling_tenant(two_tenants, monkeypatch):
    """agent._run_calls runs a turn's read-only tools in a ThreadPoolExecutor,
    and a thread does not inherit a context. Without the copy_context() in
    _run_calls every tool in a parallel turn runs for nobody and fails; with a
    naive fix that used one shared Context it would fail differently. This is
    the test that says which."""
    from app import llm

    two = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_pdf", "arguments": {"filename": "shared_name.pdf"}}},
                {"function": {"name": "list_files", "arguments": {}}},
            ],
        }
    }
    replies = [two, {"message": {"role": "assistant", "content": "done"}}]

    def fake_chat(messages, tools=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)

    outcome = agent.ask("read it and list the folder")
    assert len(outcome["steps"]) == 2
    assert all(step["ok"] for step in outcome["steps"]), outcome["steps"]

    read = next(s for s in outcome["steps"] if s["tool"] == "read_pdf")
    listed = next(s for s in outcome["steps"] if s["tool"] == "list_files")
    assert "Aardvark" in str(read["result"]), "read the wrong tenant's file"
    assert "b_only.pdf" not in str(listed["result"]), "listed the wrong tenant's folder"


# --------------------------------------------------------------------------
# the boundary must stay invisible to the model
# --------------------------------------------------------------------------

def test_no_tool_asks_the_model_who_the_tenant_is():
    """Decision 4.2 in the Step 1 plan, made checkable.

    If a tenant argument ever appears on a tool, agent._coerce_arguments will
    derive it from the signature, demand it from the model, and name it in the
    error text. The owner of the data would then be something a small model
    guesses at.
    """
    forbidden = {"tenant", "tenant_id", "owner", "customer", "account"}

    for name, function in agent.REGISTRY.items():
        parameters = set(inspect.signature(function).parameters)
        assert not (parameters & forbidden), f"{name} exposes {parameters & forbidden}"

    for schema in agent.TOOL_SCHEMAS:
        properties = schema.get("function", {}).get("parameters", {}).get("properties", {})
        assert not (set(properties) & forbidden), schema["function"]["name"]


def test_the_old_single_folder_constants_are_gone_from_the_storage_modules():
    """A source check, because the failure it prevents is a call site that still
    reads one global folder and quietly serves the wrong customer. Renaming the
    constants makes such a call site a NameError, and this keeps it that way."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for module in ("app/tools.py", "app/search.py", "app/db.py"):
        source = (root / module).read_text(encoding="utf-8")
        for gone in ("DATA_DIR", "INDEX_PATH"):
            # INDEX_DIR survives as the name of an environment variable in a
            # help string, which is a different thing from the old constant.
            hits = [line for line in source.splitlines()
                    if re.search(rf"\b{gone}\b", line)]
            assert not hits, f"{module} still mentions {gone}: {hits}"


# --------------------------------------------------------------------------
# the job lane
# --------------------------------------------------------------------------

def _finish(lane, job_id, seconds=3.0):
    from app import jobs as jobs_module
    import time
    deadline = time.time() + seconds
    while time.time() < deadline:
        with lane._lock:
            job = lane._jobs.get(job_id)
        if job and job.status in jobs_module.FINISHED:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished")


@pytest.fixture()
def writing_lane(tenant_storage):
    """A lane whose handler writes a file, so where it lands is visible."""
    from app import jobs as jobs_module

    made = jobs_module.Lane(workers=1, max_queued=10)
    made.handler("write", lambda report, name="x.pdf", body="hello":
                 tools.write_pdf(name, title="from a job", body=body))
    made.handler("whoami", lambda report: {"tenant": context.current_tenant()})
    return made


def test_a_job_runs_as_the_tenant_that_submitted_it(writing_lane):
    """The line 1.4 exists for.

    A worker thread does not inherit the context of whoever submitted the job.
    Without the use_tenant() in Lane._work the handler runs for nobody and
    every tool it calls raises; worse, a worker that had run for someone else
    would be the obvious place for a wrong-tenant write to appear.
    """
    mine = writing_lane.submit("write", {"name": "mine.pdf", "body": "Aardvark"})
    with context.use_tenant(OTHER_TENANT):
        config.ensure_data_dir()
        theirs = writing_lane.submit("write", {"name": "theirs.pdf", "body": "Bandicoot"})

    assert _finish(writing_lane, mine.id).status == "done"
    assert _finish(writing_lane, theirs.id).status == "done"

    assert (config.DATA_ROOT / TEST_TENANT / "mine.pdf").is_file()
    assert (config.DATA_ROOT / OTHER_TENANT / "theirs.pdf").is_file()
    assert not (config.DATA_ROOT / TEST_TENANT / "theirs.pdf").exists()
    assert not (config.DATA_ROOT / OTHER_TENANT / "mine.pdf").exists()


def test_the_handler_sees_the_jobs_owner_not_the_workers_last_one(writing_lane):
    first = writing_lane.submit("whoami", {})
    with context.use_tenant(OTHER_TENANT):
        second = writing_lane.submit("whoami", {})

    assert _finish(writing_lane, first.id).result == {"tenant": TEST_TENANT}
    # The same worker thread, having just run as TEST_TENANT.
    assert _finish(writing_lane, second.id).result == {"tenant": OTHER_TENANT}


def test_a_job_cannot_be_submitted_without_an_owner(writing_lane):
    with context.no_tenant():
        with pytest.raises(context.NoTenantError):
            writing_lane.submit("whoami", {})


def test_another_tenants_job_is_not_found_rather_than_forbidden(writing_lane):
    from app import jobs as jobs_module

    mine = writing_lane.submit("whoami", {})
    with context.use_tenant(OTHER_TENANT):
        with pytest.raises(jobs_module.JobError) as caught:
            writing_lane.get(mine.id)
    message = str(caught.value)
    assert "No job with id" in message
    assert "forbidden" not in message.lower() and "not yours" not in message.lower()


def test_another_tenant_cannot_cancel_the_job(writing_lane):
    from app import jobs as jobs_module

    mine = writing_lane.submit("write", {"name": "slow.pdf"})
    with context.use_tenant(OTHER_TENANT):
        with pytest.raises(jobs_module.JobError):
            writing_lane.cancel(mine.id)


def test_a_listing_shows_only_this_tenants_jobs(writing_lane):
    mine = writing_lane.submit("whoami", {})
    with context.use_tenant(OTHER_TENANT):
        theirs = writing_lane.submit("whoami", {})
        their_ids = {j["id"] for j in writing_lane.snapshot()["jobs"]}

    my_ids = {j["id"] for j in writing_lane.snapshot()["jobs"]}
    assert mine.id in my_ids and theirs.id not in my_ids
    assert theirs.id in their_ids and mine.id not in their_ids


def test_a_job_payload_never_carries_an_owner(writing_lane):
    job = writing_lane.submit("whoami", {})
    assert "tenant_id" not in job.public()
    assert all("tenant_id" not in entry for entry in writing_lane.snapshot()["jobs"])


def test_a_worker_takes_the_owner_from_the_job_and_not_from_its_own_residue(tenant_storage):
    """Unconditionally from the job, even if the thread already holds a tenant.

    Reading the worker's own context first and falling back to the job looks
    equivalent, because a fresh worker thread holds nothing. It stops being
    equivalent the moment anything leaves a tenant set in that thread: a
    handler that sets one and does not reset, a library that does, a future
    edit that hoists the context outside the loop. Then the next job for a
    different customer inherits it, and the wrong folder is written silently.

    This handler leaks on purpose. The job after it must be unaffected.
    """
    from app import jobs as jobs_module

    lane = jobs_module.Lane(workers=1, max_queued=10)

    def leaky(report):
        context.set_tenant(OTHER_TENANT)   # deliberately never reset
        return {"tenant": context.current_tenant()}

    lane.handler("leaky", leaky)
    lane.handler("whoami", lambda report: {"tenant": context.current_tenant()})

    with context.use_tenant(OTHER_TENANT):
        config.ensure_data_dir()
        first = lane.submit("leaky", {})
    assert _finish(lane, first.id).status == "done"

    second = lane.submit("whoami", {})
    assert _finish(lane, second.id).result == {"tenant": TEST_TENANT}, \
        "the worker inherited the previous job's tenant"
