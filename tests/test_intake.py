"""One path in, and slow work off the request. Steps 2.2 and 2.3.

The gate for 2.3, from `docs/plans/step-02-ingestion-contract.md`: a slow
producer on a large file does not block the upload response.

That is asserted here against a producer that genuinely blocks -- it waits on
an event this test controls -- rather than one that merely takes a while. A
sleep would make the test a race against the machine it runs on, and would
pass on a fast one for the wrong reason.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import config, ingest, intake, jobs, main

TEST_TOKEN = "a-test-token-long-enough-to-count"


@pytest.fixture(autouse=True)
def a_pipeline_holding_only_the_real_producers():
    """A producer registered by one test is in the pipeline for every test after
    it, which shows up as a manifest row nobody wrote."""
    saved = ingest.registered()
    yield
    ingest._PRODUCERS.clear()
    ingest._PRODUCERS.update(saved)


@pytest.fixture
def client(tenant_storage, monkeypatch):
    """A signed-in client, the same way tests/test_api.py makes one."""
    monkeypatch.setattr(config, "APP_TOKEN", TEST_TOKEN)
    main._failures.clear()
    made = TestClient(main.app)
    made.headers.update({"X-Syslab-Token": TEST_TOKEN})
    return made


def a_pdf(name="notes.pdf", body="Cryogenic manifold inspection."):
    """A real PDF on disk, written WITHOUT going through a write path.

    `tools.write_pdf` calls intake itself, which is the correct behaviour and
    made these tests measure the wrong call: the producer had already been
    queued and finished before the test asked intake to do anything, so
    `deferred` came back empty and the assertion read like a bug in the code.
    """
    path = config.ensure_data_dir() / name
    path.write_bytes(_a_real_pdf(body))
    return path


def _a_real_pdf(body: str = "Cryogenic manifold inspection.") -> bytes:
    """A PDF with text in it, built the way the tools build one."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    page.drawString(72, 720, body)
    page.save()
    return buffer.getvalue()


def wait_for(condition, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------
# 2.2: one path in
# --------------------------------------------------------------------------

def test_a_written_file_is_ingested_and_indexed_by_one_call(tenant_storage):
    from app import search

    path = a_pdf()
    outcome = intake.arrived(path)

    assert outcome["indexed"] is True
    assert outcome["queued"] is None, "nothing slow is registered, so nothing to queue"
    assert ingest.status("notes.pdf")["ready"] is True
    assert search.search("cryogenic")["count"] == 1


def test_intake_never_raises_however_badly_it_goes(tenant_storage, monkeypatch):
    """Three callers wrap this in `except Exception: pass` and one no longer does.

    The file is already on disk by the time any of them call. Losing a write
    because the index or the queue had a bad day is the one outcome that is
    never acceptable here.
    """
    from app import search

    def explode(*_args, **_kwargs):
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr(search, "index_file", explode)
    outcome = intake.arrived(a_pdf())

    assert outcome["indexed"] is False
    assert "on fire" in outcome["reason"]


# --------------------------------------------------------------------------
# 2.3: slow work does not block the request
# --------------------------------------------------------------------------

def test_a_slow_producer_does_not_block_the_upload(client):
    """The gate for this sub-step.

    A 200 page scan being OCR'd is the case. The upload must come back with the
    document indexed for text and the slow work queued, not with the caller
    holding the connection open until the OCR finishes.
    """
    release = threading.Event()
    started = threading.Event()

    def blocks_until_told(source, out_dir):
        started.set()
        if not release.wait(timeout=10):
            raise RuntimeError("nobody released it")
        (out_dir / "pages.txt").write_text("page 1", encoding="utf-8")
        return ingest.Result.made("pages")

    ingest.register(ingest.Producer(
        name="pages", version=1, handles=frozenset({".pdf"}), slow=True,
        run=blocks_until_told,
    ))

    # try/finally, and not for tidiness. The lane is a module-level singleton
    # with one worker, so a producer left blocking by a failed assertion wedges
    # it for the full timeout and fails the NEXT test too -- which is what
    # happened, and it read like two bugs instead of one.
    try:
        began = time.time()
        response = client.post(
            "/api/upload",
            files={"file": ("scan.pdf", _a_real_pdf(), "application/pdf")},
        )
        took = time.time() - began

        assert response.status_code == 200, response.text
        assert took < 5, f"the upload waited {took:.1f}s for work meant to be queued"

        body = response.json()
        assert body["searchable"] is True, "the fast producer still ran in the request"
        assert body["outstanding"] == ["pages"], "the caller is told what is not done"
        assert body["job"], "and given something to follow it with"

        assert wait_for(started.is_set), "the slow producer never started in the lane"
        assert ingest.status("scan.pdf")["ready"] is False, "not ready until the slow work is"
        assert ingest.deferred("scan.pdf") == ["pages"]
    finally:
        release.set()

    assert wait_for(lambda: ingest.status("scan.pdf")["ready"]), "the queued work never landed"


def test_the_queued_job_is_reported_so_a_caller_can_follow_it(tenant_storage):
    def quick(source, out_dir):
        (out_dir / "done.txt").write_text("x", encoding="utf-8")
        return ingest.Result.made("done.txt")

    ingest.register(ingest.Producer(
        name="pages", version=1, handles=frozenset({".pdf"}), slow=True, run=quick,
    ))

    outcome = intake.arrived(a_pdf())

    assert outcome["deferred"] == ["pages"]
    assert outcome["queued"], "no job id came back, so nothing can follow the work"
    assert wait_for(lambda: jobs.lane.get(outcome["queued"]).status in jobs.FINISHED)
    assert jobs.lane.get(outcome["queued"]).status == jobs.DONE
    assert ingest.status("notes.pdf")["ready"] is True


def test_a_full_queue_delays_the_work_and_does_not_lose_the_file(tenant_storage, monkeypatch):
    """A queue that is full is a reason to leave work outstanding, not to fail a write.

    The manifest still says the producer is stale, so the next ingest picks it
    up. Said out loud in the response rather than swallowed: "nothing happened
    and nothing said so" is the failure mode decision 4.5 exists to prevent.
    """
    def never_runs(source, out_dir):
        raise AssertionError("this should not have been reached")

    ingest.register(ingest.Producer(
        name="pages", version=1, handles=frozenset({".pdf"}), slow=True, run=never_runs,
    ))

    def full(*_args, **_kwargs):
        raise jobs.JobError("The queue is full (5 waiting, 1 running).")

    monkeypatch.setattr(jobs.lane, "submit", full)

    outcome = intake.arrived(a_pdf())

    assert outcome["indexed"] is True, "the file is still indexed for text"
    assert outcome["queued"] is None
    assert "queue is full" in outcome["queued_error"]
    assert ingest.deferred("notes.pdf") == ["pages"], "still outstanding, so still findable"


# --------------------------------------------------------------------------
# 2.3: a status the UI can show
# --------------------------------------------------------------------------

def test_the_status_endpoint_says_whether_a_document_is_ready(client):
    a_pdf()
    client.post("/api/ingest/notes.pdf")

    body = client.get("/api/ingest/notes.pdf").json()
    assert body["ready"] is True
    assert body["producers"]["text"]["status"] == ingest.OK
    assert body["producers"]["text"]["attempts"] == 1


def test_the_status_endpoint_names_what_failed(client):
    def explode(source, out_dir):
        raise ValueError("the page images did not come out")

    ingest.register(ingest.Producer(
        name="pages", version=1, handles=frozenset({".pdf"}), slow=False, run=explode,
    ))
    a_pdf()
    client.post("/api/ingest/notes.pdf")

    body = client.get("/api/ingest/notes.pdf").json()
    assert body["ready"] is False
    assert body["failed"] == ["pages"]
    assert "did not come out" in body["producers"]["pages"]["detail"]
    assert body["producers"]["text"]["status"] == ingest.OK, "the other producer still ran"


def test_the_summary_endpoint_sorts_the_folder_three_ways(client):
    def explode(source, out_dir):
        raise ValueError("no")

    a_pdf("ready.pdf")
    a_pdf("broken.pdf")
    client.post("/api/ingest/ready.pdf")

    ingest.register(ingest.Producer(
        name="pages", version=1, handles=frozenset({".pdf"}), slow=False, run=explode,
    ))
    client.post("/api/ingest/broken.pdf")

    body = client.get("/api/ingest").json()
    assert "broken.pdf" in body["failed"]
    assert "ready.pdf" in body["outstanding"], "the new producer made it stale, which is right"
    assert "text" in body["producers"] and "pages" in body["producers"]


def test_asking_about_a_file_that_is_not_there_is_a_404(client):
    assert client.get("/api/ingest/nothing.pdf").status_code == 404
    assert client.post("/api/ingest/nothing.pdf").status_code == 404
    assert client.post("/api/ingest/../../etc/passwd").status_code in {400, 404}
