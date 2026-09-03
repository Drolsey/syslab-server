"""A job lane, for work too slow to answer a request with.

Why this exists: chat and image generation behave in opposite ways under load.
Generating a token reads the model's weights from memory, so several
conversations at once share that read and cost almost nothing extra. Diffusion
does real arithmetic per step, so two images cost twice the time. One batches,
one queues. Trying to serve both through the same blocking endpoint means the
person asking a question waits behind somebody's picture.

So: slow work is submitted, gets an id, and is polled. One worker per lane by
default, because the GPU serialises anyway and extra workers only contend for it.

Deliberately in memory. Jobs do not survive a restart, and there is no attempt
to pretend otherwise: the status endpoint says so, and a real deployment wants
this table in PostgreSQL. That is a change of storage, not of shape.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app import context
from app.config import JOB_MAX_QUEUED, JOB_RETENTION_SECONDS, JOB_WORKERS

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINISHED = {DONE, FAILED, CANCELLED}


class JobError(Exception):
    """Something the caller, or the model, can read and act on."""


class Cancelled(Exception):
    """Raised inside a handler when the job was cancelled while running."""


@dataclass
class Job:
    id: str
    kind: str
    params: dict
    # Deliberately no default. A job with no owner cannot be constructed, so
    # there is no path where one gets made and the owner is filled in later,
    # or not at all.
    tenant_id: str
    status: str = QUEUED
    progress: float = 0.0
    note: str = ""
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self, position: int | None = None) -> dict:
        """What the browser and the model are allowed to see.

        No tenant_id. The caller already knows whose jobs these are, because
        they only ever receive their own, and putting an owner in the payload
        would be one more place for it to leak.
        """
        body = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": round(self.progress, 3),
            "note": self.note,
            "created_at": datetime.fromtimestamp(self.created_at).isoformat(timespec="seconds"),
            "seconds_waiting": round((self.started_at or time.time()) - self.created_at, 1),
        }
        if self.started_at:
            end = self.finished_at or time.time()
            body["seconds_running"] = round(end - self.started_at, 1)
        if position is not None and self.status == QUEUED:
            body["position_in_queue"] = position
        if self.status == DONE:
            body["result"] = self.result
        if self.error:
            body["error"] = self.error
        return body


class Lane:
    """One queue, one or more workers, and the jobs they have handled."""

    def __init__(self, workers: int = 1, max_queued: int = 20):
        self.max_queued = max_queued
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._worker_count = max(1, workers)

    # ---- registration --------------------------------------------------
    def handler(self, kind: str, function: Callable[..., Any]) -> None:
        self._handlers[kind] = function

    @property
    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for n in range(self._worker_count):
            thread = threading.Thread(target=self._work, name=f"syslab-job-{n}", daemon=True)
            thread.start()
            self._threads.append(thread)

    # ---- submitting ----------------------------------------------------
    def submit(self, kind: str, params: dict | None = None) -> Job:
        if kind not in self._handlers:
            raise JobError(
                f"No job type named {kind!r}. Available: {', '.join(self.kinds) or 'none'}"
            )
        with self._lock:
            # Count RUNNING as well as QUEUED. Counting only what is waiting
            # makes the cap depend on thread scheduling: with one worker the
            # first submission is picked up somewhere between the next two
            # calls, so the same five submissions are sometimes five waiting
            # and sometimes four, and the sixth is accepted or refused by
            # timing. In-flight work is what the lane actually owes the user,
            # so that is what is capped.
            waiting = sum(1 for j in self._jobs.values() if j.status == QUEUED)
            running = sum(1 for j in self._jobs.values() if j.status == RUNNING)
            if waiting + running >= self.max_queued:
                raise JobError(
                    f"The queue is full ({waiting} waiting, {running} running). "
                    "Try again in a minute, or cancel something."
                )
            # current_tenant() raises if nothing said whose job this is, so an
            # unowned job cannot enter the lane at all.
            job = Job(id=uuid.uuid4().hex[:12], kind=kind,
                      params=dict(params or {}), tenant_id=context.current_tenant())
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune()
        self.start()
        self._queue.put(job.id)
        return job

    # ---- reading -------------------------------------------------------
    def get(self, job_id: str) -> Job:
        """This tenant's job with that id, or the ordinary not-found error.

        Another tenant's job is NOT FORBIDDEN, it is not found. "That job exists
        but is not yours" is itself a disclosure: it confirms an id someone
        guessed, and over enough guesses it counts another customer's work.
        """
        tenant = context.current_tenant()
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None and job.tenant_id != tenant:
            job = None
        if job is None:
            raise JobError(
                f"No job with id {job_id!r}. Jobs are kept for "
                f"{JOB_RETENTION_SECONDS // 60} minutes after finishing, and are lost "
                "if the server restarts."
            )
        return job

    def position_of(self, job_id: str) -> int | None:
        with self._lock:
            waiting = [i for i in self._order
                       if i in self._jobs and self._jobs[i].status == QUEUED]
        return waiting.index(job_id) + 1 if job_id in waiting else None

    def snapshot(self, limit: int = 40) -> dict:
        tenant = context.current_tenant()
        with self._lock:
            self._prune()
            jobs = [self._jobs[i] for i in self._order if i in self._jobs]
        waiting = [j for j in jobs if j.status == QUEUED]
        running = [j for j in jobs if j.status == RUNNING]

        # WHICH jobs are running is private. HOW BUSY the machine is, is not,
        # and hiding it would make position_in_queue a lie: telling someone they
        # are first while ten jobs sit ahead of them is a wrong answer that
        # looks like a right one. So the counts and the queue position are
        # server-wide and truthful, and only the list is filtered.
        mine = [j for j in jobs if j.tenant_id == tenant]
        recent = list(reversed(mine))[:limit]
        return {
            "queued": len(waiting),
            "running": len(running),
            "workers": self._worker_count,
            "kinds": self.kinds,
            "persistent": False,
            "jobs": [
                j.public(waiting.index(j) + 1 if j in waiting else None) for j in recent
            ],
        }

    # ---- cancelling ----------------------------------------------------
    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in FINISHED:
            raise JobError(f"That job already finished with status {job.status!r}.")
        job._cancel.set()
        if job.status == QUEUED:
            # Never started, so it can just be marked. The worker skips it.
            job.status = CANCELLED
            job.finished_at = time.time()
            job.note = "cancelled before it started"
        else:
            job.note = "cancelling, waiting for the current step to end"
        return job

    # ---- the worker ----------------------------------------------------
    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                job = self._jobs.get(job_id)
                if job is None or job._cancel.is_set():
                    if job and job.status == QUEUED:
                        job.status = CANCELLED
                        job.finished_at = time.time()
                    continue

                job.status = RUNNING
                job.started_at = time.time()
                job.note = ""
                handler = self._handlers.get(job.kind)

                def report(fraction: float, note: str = "") -> None:
                    """Handlers call this. It also raises if the job was cancelled."""
                    if job._cancel.is_set():
                        raise Cancelled()
                    job.progress = max(0.0, min(1.0, float(fraction)))
                    if note:
                        job.note = note

                try:
                    # THE LINE THIS SUB-STEP EXISTS FOR. A worker thread does
                    # not inherit the context of whoever submitted the job, so
                    # without this the handler runs for nobody: every tool it
                    # calls raises NoTenantError, and a worker that had run for
                    # someone else earlier would still be holding nothing,
                    # because a thread's context does not carry over either.
                    # The owner is read from the job, which is the only place it
                    # is still true by the time the work happens.
                    with context.use_tenant(job.tenant_id):
                        job.result = handler(report=report, **job.params)
                    job.status = DONE
                    job.progress = 1.0
                except Cancelled:
                    job.status = CANCELLED
                    job.note = "cancelled while running"
                except TypeError as exc:
                    job.status = FAILED
                    job.error = f"Wrong parameters for {job.kind}: {exc}"
                except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
                    job.status = FAILED
                    job.error = f"{type(exc).__name__}: {exc}"
                finally:
                    job.finished_at = time.time()
            finally:
                self._queue.task_done()

    # ---- housekeeping --------------------------------------------------
    def _prune(self) -> None:
        """Called with the lock held. Forget finished jobs after a while."""
        cutoff = time.time() - JOB_RETENTION_SECONDS
        stale = [
            i for i in self._order
            if i in self._jobs
            and self._jobs[i].status in FINISHED
            and (self._jobs[i].finished_at or 0) < cutoff
        ]
        for job_id in stale:
            self._jobs.pop(job_id, None)
        if stale:
            self._order = [i for i in self._order if i in self._jobs]


# --------------------------------------------------------------------------
# the lane this app uses
# --------------------------------------------------------------------------

lane = Lane(workers=JOB_WORKERS, max_queued=JOB_MAX_QUEUED)


def generate_image(report: Callable[..., None], prompt: str, filename: str | None = None,
                   width: int = 1024, height: int = 1024) -> dict:
    """Placeholder for the image model, so the lane is wired before the GPU is.

    When FLUX.2 klein-4B is installed this becomes the real call, and nothing
    else in the stack changes: the endpoint, the queue, the polling and the UI
    are all already correct.
    """
    raise JobError(
        "Image generation is not installed yet. This job type exists so the queue, "
        "the endpoints and the interface can be tested before the model arrives. "
        "When FLUX.2 klein-4B is in place, wire it into app/jobs.generate_image."
    )


def selftest(report: Callable[..., None], seconds: float = 6.0, steps: int = 12) -> dict:
    """A deliberately slow job, so the lane can be proven without a GPU.

    Stands in for a diffusion run: takes real time, reports progress in steps,
    and checks for cancellation between them, which is exactly how a denoising
    loop behaves.
    """
    seconds = max(0.1, min(float(seconds), 120.0))
    steps = max(1, min(int(steps), 200))
    for step in range(steps):
        time.sleep(seconds / steps)
        report((step + 1) / steps, f"step {step + 1} of {steps}")
    return {"slept_for": round(seconds, 2), "steps": steps}


lane.handler("generate_image", generate_image)
lane.handler("selftest", selftest)
