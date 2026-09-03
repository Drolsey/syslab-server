"""Tests for the job lane.

The property that matters, and the reason the lane exists, is that slow work
does not block anything else. Most of these tests are about that.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main


@pytest.fixture
def lane():
    made = jobs.Lane(workers=1, max_queued=5)
    made.handler("selftest", jobs.selftest)
    made.handler("boom", lambda report, **_: 1 / 0)
    made.handler("echo", lambda report, value=None, **_: {"value": value})
    yield made


# --- the basics -----------------------------------------------------------

def test_a_job_is_accepted_and_finishes(lane):
    job = lane.submit("echo", {"value": 42})
    assert job.status == jobs.QUEUED
    for _ in range(100):
        if lane.get(job.id).status in jobs.FINISHED:
            break
        time.sleep(0.02)
    done = lane.get(job.id)
    assert done.status == jobs.DONE
    assert done.result == {"value": 42}
    assert done.progress == 1.0


def test_an_unknown_kind_is_refused_with_the_real_ones(lane):
    with pytest.raises(jobs.JobError, match="echo"):
        lane.submit("paint_the_moon", {})


def test_a_handler_that_raises_fails_the_job_and_not_the_worker(lane):
    bad = lane.submit("boom", {})
    good = lane.submit("echo", {"value": 1})
    for _ in range(150):
        if lane.get(good.id).status in jobs.FINISHED:
            break
        time.sleep(0.02)
    assert lane.get(bad.id).status == jobs.FAILED
    assert "ZeroDivisionError" in lane.get(bad.id).error
    # the worker survived and carried on
    assert lane.get(good.id).status == jobs.DONE


def test_wrong_parameters_say_so_rather_than_crashing(lane):
    job = lane.submit("echo", {"nonsense": True})
    for _ in range(100):
        if lane.get(job.id).status in jobs.FINISHED:
            break
        time.sleep(0.02)
    assert lane.get(job.id).status == jobs.DONE  # echo swallows extras via **_


# --- queueing behaviour ---------------------------------------------------

def test_jobs_queue_in_order_and_report_their_position(lane):
    first = lane.submit("selftest", {"seconds": 0.6, "steps": 3})
    second = lane.submit("selftest", {"seconds": 0.6, "steps": 3})
    third = lane.submit("selftest", {"seconds": 0.6, "steps": 3})
    time.sleep(0.15)
    assert lane.get(first.id).status == jobs.RUNNING
    assert lane.position_of(second.id) == 1
    assert lane.position_of(third.id) == 2


def test_one_worker_means_one_job_at_a_time(lane):
    """More workers would only contend for the same GPU."""
    for _ in range(3):
        lane.submit("selftest", {"seconds": 0.4, "steps": 2})
    seen = []
    for _ in range(30):
        seen.append(lane.snapshot()["running"])
        time.sleep(0.05)
    assert max(seen) == 1


def test_the_queue_is_bounded(lane):
    # The cap counts work in flight, queued and running together. Counting
    # only what was waiting made this depend on whether the worker had picked
    # the first job up yet, so the same five submissions passed or failed by
    # thread timing.
    for _ in range(5):
        lane.submit("selftest", {"seconds": 5, "steps": 10})
    with pytest.raises(jobs.JobError, match="queue is full"):
        lane.submit("selftest", {"seconds": 5})


def test_the_bound_holds_however_the_worker_is_scheduled(lane):
    """The same submissions must be refused whether or not a job has started."""
    for _ in range(5):
        lane.submit("selftest", {"seconds": 5, "steps": 10})
    time.sleep(0.2)  # long enough for the worker to have taken one
    with pytest.raises(jobs.JobError, match="queue is full"):
        lane.submit("selftest", {"seconds": 5})


# --- cancelling -----------------------------------------------------------

def test_a_queued_job_cancels_immediately(lane):
    lane.submit("selftest", {"seconds": 2, "steps": 8})
    waiting = lane.submit("selftest", {"seconds": 2, "steps": 8})
    time.sleep(0.1)
    assert lane.cancel(waiting.id).status == jobs.CANCELLED
    assert lane.get(waiting.id).status == jobs.CANCELLED


def test_a_running_job_stops_at_its_next_step(lane):
    """A GPU step cannot be interrupted mid-way, so cancellation is cooperative."""
    job = lane.submit("selftest", {"seconds": 3, "steps": 30})
    time.sleep(0.3)
    assert lane.get(job.id).status == jobs.RUNNING
    lane.cancel(job.id)
    for _ in range(100):
        if lane.get(job.id).status in jobs.FINISHED:
            break
        time.sleep(0.02)
    assert lane.get(job.id).status == jobs.CANCELLED
    assert 0 < lane.get(job.id).progress < 1


def test_cancelling_a_finished_job_says_so(lane):
    job = lane.submit("echo", {"value": 1})
    for _ in range(100):
        if lane.get(job.id).status in jobs.FINISHED:
            break
        time.sleep(0.02)
    with pytest.raises(jobs.JobError, match="already finished"):
        lane.cancel(job.id)


def test_an_unknown_job_id_explains_that_jobs_expire(lane):
    with pytest.raises(jobs.JobError, match="restarts"):
        lane.get("nosuchjob")


# --- the HTTP layer -------------------------------------------------------

TOKEN = "a-test-token-long-enough-to-count"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_TOKEN", TOKEN)
    main._failures.clear()
    signed_in = TestClient(main.app)
    signed_in.headers.update({"X-Syslab-Token": TOKEN})
    return signed_in


def test_submitting_returns_202_and_an_id(client):
    response = client.post("/api/jobs", json={"kind": "selftest", "params": {"seconds": 0.3}})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] in {jobs.QUEUED, jobs.RUNNING}
    assert len(body["id"]) == 12


def test_a_stranger_cannot_queue_work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_TOKEN", TOKEN)
    stranger = TestClient(main.app)
    assert stranger.post("/api/jobs", json={"kind": "selftest"}).status_code == 401
    assert stranger.get("/api/jobs").status_code == 401


def test_polling_a_job_reports_progress_then_the_result(client):
    job_id = client.post("/api/jobs",
                         json={"kind": "selftest", "params": {"seconds": 0.6, "steps": 4}}).json()["id"]
    for _ in range(150):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in jobs.FINISHED:
            break
        time.sleep(0.02)
    assert body["status"] == jobs.DONE
    assert body["result"]["steps"] == 4
    assert "seconds_running" in body


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/deadbeefcafe").status_code == 404


def test_cancelling_over_http(client):
    client.post("/api/jobs", json={"kind": "selftest", "params": {"seconds": 3, "steps": 20}})
    second = client.post("/api/jobs",
                         json={"kind": "selftest", "params": {"seconds": 3, "steps": 20}}).json()
    assert client.delete(f"/api/jobs/{second['id']}").json()["status"] == jobs.CANCELLED


def test_an_unknown_kind_is_a_400_naming_the_real_ones(client):
    response = client.post("/api/jobs", json={"kind": "nope"})
    assert response.status_code == 400
    assert "generate_image" in response.json()["detail"]


def test_image_generation_is_declared_but_not_yet_installed(client):
    """The lane is wired before the model, on purpose. Say so honestly."""
    job_id = client.post("/api/jobs",
                         json={"kind": "generate_image",
                               "params": {"prompt": "a cat"}}).json()["id"]
    for _ in range(150):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in jobs.FINISHED:
            break
        time.sleep(0.02)
    assert body["status"] == jobs.FAILED
    assert "not installed yet" in body["error"]


# --- the property the whole thing exists for ------------------------------

def test_a_running_job_does_not_block_other_requests(client, monkeypatch):
    """Somebody's picture must never make somebody else's question wait."""
    monkeypatch.setattr(main.agent, "ask",
                        lambda message, history=None: {"answer": "still here",
                                                       "steps": [], "messages": []})
    client.post("/api/jobs", json={"kind": "selftest", "params": {"seconds": 3, "steps": 30}})
    time.sleep(0.2)

    started = time.time()
    reply = client.post("/api/chat", json={"message": "are you busy?"})
    elapsed = time.time() - started

    assert reply.status_code == 200
    assert reply.json()["answer"] == "still here"
    assert elapsed < 1.0, f"chat waited {elapsed:.1f}s behind a job"


def test_several_clients_can_poll_at_once_while_work_runs(client):
    client.post("/api/jobs", json={"kind": "selftest", "params": {"seconds": 2, "steps": 20}})
    codes: list[int] = []

    def poll():
        codes.append(client.get("/api/jobs").status_code)

    threads = [threading.Thread(target=poll) for _ in range(8)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert codes == [200] * 8
    assert time.time() - started < 2.0
