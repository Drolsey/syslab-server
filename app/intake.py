"""A file just landed. Step 2.2, and the queueing half of 2.3.

There are three ways a file arrives in a tenant's folder -- an upload, a tool
that wrote one, a spreadsheet exported from a database query -- and before this
they each called `search.index_file` and each decided for themselves what to do
about the result. That worked while indexing was the only thing that happened
to a new file. It stops working the moment a second producer exists, because
"what happens to a new file" then has to be decided in three places that will
drift.

WHAT THIS OWNS, AND WHAT IT DOES NOT
    It owns the ORDER: fast producers run in the request, the index is brought
    up to date, and anything slow is handed to the job lane. It does not own
    the work. `ingest` decides what needs producing, `search` decides what
    being indexed means, and this module knows only that one comes before the
    other.

WHY NOT PUT IT IN search.py
    Because that is the arrangement Step 2 exists to end. The index is one
    consumer of the pipeline, and a consumer that also drives the pipeline and
    schedules its jobs is the owner of it wearing a different hat. Import
    direction stays one-way: config <- ingest <- producers <- search <- intake.

WHY NOT PUT IT IN ingest.py
    The pipeline must not know that a search index exists, or that there is a
    job lane. A producer that needed either would be a producer this module
    could not schedule, and the contract would quietly become "whatever
    ingest.py happens to import".
"""

from __future__ import annotations

from pathlib import Path

from app import ingest, jobs, search

# The job kind the lane knows this by. One kind for all slow producers rather
# than one per producer: the lane serialises anyway, and a file with three slow
# producers outstanding wants one queue entry that finishes when the document
# is ready, not three that each look like the whole job.
SLOW_INGEST = "ingest_slow"


def arrived(path: Path) -> dict:
    """Everything that should happen to a file that has just been written.

    Returns what the old callers of `search.index_file` returned, plus what was
    queued, because two of the three callers already look at `indexed` and the
    third ignores the result entirely.

    Never raises. Indexing was always a convenience that must not fail a write
    which already succeeded -- the file is on disk, and a rebuild can put the
    index right at any time -- and queueing inherits that: a full job lane is a
    reason to leave the slow work outstanding, not a reason to lose the upload.
    """
    outcome = {"name": path.name, "indexed": False, "queued": None, "deferred": []}
    try:
        outcome.update(search.index_file(path))
    except Exception as exc:  # noqa: BLE001 - the file is already written
        outcome["reason"] = f"{type(exc).__name__}: {exc}"
        return outcome

    try:
        deferred = ingest.deferred(path.name)
    except ingest.IngestError:
        return outcome
    if not deferred:
        return outcome

    outcome["deferred"] = deferred
    try:
        job = jobs.lane.submit(SLOW_INGEST, {"name": path.name})
        outcome["queued"] = job.id
    except jobs.JobError as exc:
        # The queue is full, or the lane does not know this kind. The work is
        # still recorded as outstanding in the manifest and the next ingest()
        # will pick it up, so this is a delay and not a loss. Said out loud
        # rather than swallowed, because "nothing happened and nothing said so"
        # is the failure mode decision 4.5 exists to prevent.
        outcome["queued_error"] = str(exc)
    return outcome


def run_slow(report, name: str) -> dict:
    """The job lane's handler: finish whatever is still outstanding for a file.

    Runs EVERY stale producer, not only the slow ones. A fast producer still
    outstanding at this point failed or was never reached, and the cheap thing
    to do about it is the cheap thing.

    The index is refreshed afterwards because a slow producer may be why a
    document had no text -- OCR is the case this is waiting for. Cheap when it
    changes nothing, and the alternative is a document that is ready and not
    findable until somebody rebuilds.
    """
    report(0.05, f"producing {name}")
    outcome = ingest.ingest(name)
    report(0.8, "indexing")
    try:
        from app.config import resolve_in_data_dir

        search.index_file(resolve_in_data_dir(name))
    except Exception:  # noqa: BLE001 - the artifacts are made either way
        pass
    report(1.0, "done")
    return outcome


jobs.lane.handler(SLOW_INGEST, run_slow)
