"""What happens when a file enters this system.

Step 2, the ingestion contract. `docs/plans/step-02-ingestion-contract.md` is
the design and the five decisions behind it; this is the implementation of
sub-step 2.0, and nothing imports it yet on purpose.

WHY THIS EXISTS AT ALL
    Today ingestion is one thing: text, extracted once, into an FTS5 row.
    Vision wants page images. Numeric filtering wants extracted fields.
    Semantic search wants chunks and embeddings. Bolted on separately those are
    four readers of the same PDF, four places a file can be half-processed, and
    no way to answer "is this document ready". This module is the one place
    that decides what runs, so that each of those becomes a new producer in an
    existing pipeline rather than a second pipeline.

    It adds no capability a user would notice. That is the point.

WHAT A PRODUCER IS
    A name, a version, the suffixes it handles, whether it is slow, and a
    function. The pipeline knows nothing about what a producer makes. It knows
    that something was asked for, whether it succeeded, when, and against which
    version of the producer -- which is exactly what lets a later step add page
    images without touching this file.

THE RULE THAT KEEPS derived/ DISPOSABLE
    Everything under derived/ can be deleted and rebuilt from data/. It is the
    rule that made the search index safe and it is inherited deliberately. If a
    producer ever makes something that cannot be regenerated, it is not
    derived, and it does not belong here.

    The manifest lives INSIDE derived/<tenant>/ for the same reason: a record
    that outlived the artifacts it describes would report a document ready and
    point at files that are gone.

THREE OUTCOMES, AND ONLY ONE OF THEM IS AN EXCEPTION
    A producer returns `ok` (it made something) or `skipped` (there was nothing
    here to make). It never has to think about failure: anything it raises
    becomes a `failed` row. That is decision 4.5 -- a producer failing is
    RECORDED, NOT RAISED. One failed producer must not fail the upload, must
    not stop the other producers, and must not be invisible.

    The exception is ProducerUnavailable, and the distinction is the one
    app/search.py already learned the hard way: a missing library is not a
    broken document. An unreadable file affects one document; a missing parser
    affects every document of that type, and it is an environment fault. It is
    reported once per call and written against nothing, because recording it as
    a failure against every file in the folder is how a fine folder comes to
    look like a folder of broken documents.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import (
    UnsafePathError,
    ensure_derived_dir,
    manifest_path,
    resolve_in_data_dir,
)

# A recorded error is read by a person scanning a manifest and, later, by a
# model deciding whether a document is usable. A stack trace pasted whole
# serves neither. The lesson is already written down in this project: an error
# that dumps eighteen filenames drowns everything around it.
MAX_DETAIL = 2000

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


class IngestError(Exception):
    """The caller asked for something that does not exist or is not allowed.

    Distinct from a producer failing, which is recorded rather than raised.
    This one is a bug in the caller: a filename that is not in the tenant's
    folder, or a path trying to leave it.
    """


class ProducerUnavailable(Exception):
    """This producer cannot run at all, for reasons nothing to do with the file.

    A missing parser library is the case that matters. Raise this and the
    pipeline reports it once and writes no manifest row, so that installing the
    package is all it takes to fix -- rather than installing the package and
    then working out which several hundred `failed` rows were lies.
    """


# --------------------------------------------------------------------------
# what a producer is
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Result:
    """What a producer says it did. Never how it failed -- that is an exception.

    `output_path` is relative to derived/<tenant>/ and is a convenience for
    whoever reads the manifest later; a producer that wrote several files names
    the one that matters, or none at all. Nothing here depends on it, because
    forget() removes the producer's whole directory for this source rather than
    the one path it happened to report.
    """

    status: str
    output_path: str | None = None
    detail: str = ""

    @classmethod
    def made(cls, output_path: str | Path | None = None, detail: str = "") -> "Result":
        return cls(OK, str(output_path) if output_path is not None else None, detail)

    @classmethod
    def nothing(cls, detail: str) -> "Result":
        """Ran fine, and there was nothing in this file to produce.

        A PDF with no extractable text is this, not a failure. Saying so is the
        difference between "nobody has looked at this" and "we looked, and
        there is nothing here", which the index today cannot tell apart.
        """
        return cls(SKIPPED, None, detail)


@dataclass(frozen=True)
class Producer:
    """One thing that can be made out of a source file.

    `run` takes the source path AND the directory to write into, which is a
    deliberate departure from the one-argument signature sketched in the plan.
    A producer that chooses its own output location is a producer whose output
    nothing else can find or delete, and decision 4.2's promise -- that
    derived/ can be deleted and rebuilt -- would then depend on every producer
    remembering to keep it. Handing the directory down makes it structural: the
    pipeline knows where everything is because it decided.

    `version` is the invalidation lever. Bumping it invalidates everything this
    producer ever made, which is how a fixed OCR bug reaches documents that
    were processed before the fix.
    """

    name: str
    version: int
    handles: frozenset[str]
    slow: bool
    run: Callable[[Path, Path], Result]

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError(
                f"{self.name!r} is not a usable producer name: it becomes a folder "
                "name under derived/<tenant>/, so it cannot be empty or hold a "
                "path separator."
            )
        if self.version < 1:
            raise ValueError("A producer version starts at 1 and only goes up.")


_PRODUCERS: dict[str, Producer] = {}


def register(producer: Producer) -> Producer:
    """Add a producer to the pipeline. A module registers itself at import."""
    _PRODUCERS[producer.name] = producer
    return producer


def unregister(name: str) -> None:
    _PRODUCERS.pop(name, None)


def registered() -> dict[str, Producer]:
    """A copy, so that a caller holding this cannot quietly change the pipeline."""
    return dict(_PRODUCERS)


def producers_for(suffix: str) -> list[Producer]:
    """Which producers handle this file type, in a stable order.

    A producer that does not handle the suffix gets no row. The alternative --
    a `skipped` row per producer per file -- fills the manifest with the
    absence of work nobody asked for, and buries the skips that mean something.
    """
    lowered = suffix.lower()
    return sorted(
        (p for p in _PRODUCERS.values() if lowered in p.handles),
        key=lambda p: p.name,
    )


# --------------------------------------------------------------------------
# the manifest
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    source_name      TEXT NOT NULL,
    producer         TEXT NOT NULL,
    producer_version INTEGER NOT NULL,
    status           TEXT NOT NULL,
    source_size      INTEGER NOT NULL,
    source_mtime     REAL NOT NULL,
    output_path      TEXT,
    detail           TEXT,
    attempts         INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (source_name, producer)
);
"""


def connect() -> sqlite3.Connection:
    """Open THIS TENANT's manifest, creating it if this is the first file.

    One file per tenant, never one file with an owner column. Same reasoning as
    the search index: a forgotten WHERE returns another customer's rows
    silently, while a wrong path returns nothing.
    """
    folder = ensure_derived_dir()
    path = manifest_path()
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        # Some container and network mounts cannot do WAL. The manifest still
        # works without it; only concurrent readers suffer.
        pass
    try:
        connection.executescript(SCHEMA)
    except sqlite3.OperationalError as exc:
        connection.close()
        raise IngestError(
            f"Could not open the ingestion manifest at {path}: {exc}. "
            "Set DERIVED_DIR in .env to a local disk; network shares and some "
            "container mounts cannot host a SQLite database."
        ) from exc
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(connection: sqlite3.Connection, source_name: str) -> dict[str, sqlite3.Row]:
    return {
        row["producer"]: row
        for row in connection.execute(
            "SELECT * FROM artifacts WHERE source_name = ?", (source_name,)
        )
    }


def _is_current(row: sqlite3.Row, stat, producer: Producer) -> bool:
    """Has anything changed that this producer's output depended on?

    Size, mtime, producer version. Deliberately NOT a content hash: hashing a
    200 MB workbook on every upload, to notice a change that size and mtime
    already caught, is a cost paid every time for a case that is both rare and
    not silent.

    THE MTIME COMPARISON IS EXACT, AND THE FIRST VERSION OF THIS WAS NOT.
    It carried search.stale()'s one-second tolerance across, on the reasoning
    that the two ought to agree about the same file. They serve different
    purposes and the consequences are not comparable. stale() produces a
    SUGGESTION -- a list of files somebody might want to reindex -- so being
    loose there costs a rebuild nobody needed. This decides whether DERIVED
    DATA MAY BE SERVED FOR BYTES THAT NO LONGER EXIST, and being loose here is
    silently wrong.

    Step 2.1 caught it the moment the index started consuming this: a test
    rewrote a PDF with a body of the same length, within the same second, and
    the pipeline served the old text to the index. Nothing had been wrong
    before, because index_file re-extracted every time and had no cache to be
    stale. A float survives SQLite's REAL unchanged, so exact is exact.

    What exact still cannot see is a rewrite inside one filesystem mtime tick
    that leaves the size identical -- sub-microsecond on NTFS and ext4, two
    seconds on a FAT-derived mount. Bumping a producer's version, or forget(),
    is the answer to that; hashing is not, for the reason above.
    """
    return (
        int(row["producer_version"]) == producer.version
        and int(row["source_size"]) == stat.st_size
        and float(row["source_mtime"]) == stat.st_mtime
    )


def _record(
    connection: sqlite3.Connection,
    source_name: str,
    producer: Producer,
    status: str,
    stat,
    output_path: str | None,
    detail: str,
    attempts: int,
) -> None:
    connection.execute(
        """
        INSERT INTO artifacts (
            source_name, producer, producer_version, status,
            source_size, source_mtime, output_path, detail, attempts, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_name, producer) DO UPDATE SET
            producer_version = excluded.producer_version,
            status           = excluded.status,
            source_size      = excluded.source_size,
            source_mtime     = excluded.source_mtime,
            output_path      = excluded.output_path,
            detail           = excluded.detail,
            attempts         = excluded.attempts,
            updated_at       = excluded.updated_at
        """,
        (
            source_name,
            producer.name,
            producer.version,
            status,
            stat.st_size,
            stat.st_mtime,
            # Recorded with forward slashes whatever wrote it. A path is the
            # one field here another machine might read, and this project
            # already runs the same code on Windows and on the Ubuntu box.
            None if output_path is None else str(output_path).replace("\\", "/"),
            (detail or "")[:MAX_DETAIL],
            attempts,
            _now(),
        ),
    )
    connection.commit()


# --------------------------------------------------------------------------
# where a producer's output goes
# --------------------------------------------------------------------------

def output_dir(producer_name: str, source_name: str) -> Path:
    """derived/<tenant>/<producer>/<source file>/

    A directory per source rather than a file per source, because a producer
    that makes one thing today makes forty page images tomorrow, and forget()
    has to be able to remove all of it without knowing which.

    source_name is a real filename read off disk, so it is already legal as a
    directory name; nothing model-supplied reaches here unresolved.
    """
    return ensure_derived_dir() / producer_name / source_name


def _prune_if_empty(folder: Path) -> None:
    try:
        next(folder.iterdir())
    except StopIteration:
        folder.rmdir()
    except OSError:
        pass


def _prune(folder: Path) -> None:
    """A producer that made no files leaves no folder behind -- neither of them.

    Pruning only the per-source directory left derived/<tenant>/<producer>/
    standing empty, which reads as "this producer has output here" to anyone
    listing the folder, and gives forget() a directory to walk for every
    producer that has never made anything.
    """
    _prune_if_empty(folder)
    _prune_if_empty(folder.parent)


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------

def _source(name: str) -> Path:
    try:
        path = resolve_in_data_dir(name)
    except UnsafePathError as exc:
        raise IngestError(str(exc)) from exc
    if not path.is_file():
        raise IngestError(f"No file named {name!r} in this tenant's folder.")
    return path


def ingest(name: str, *, only_fast: bool = False) -> dict:
    """Bring one file up to date, and record what happened.

    `only_fast` runs the producers that declared themselves cheap and leaves
    the rest stale, reported as `deferred`. That is decision 4.3: text
    extraction is already fast enough to be in-request and stays there, so
    "upload then immediately ask about it" keeps working, while OCR-ing a 200
    page scan does not become a request that times out. Sub-step 2.3 gives the
    deferred list to the job lane; until then it is a promise this returns and
    nobody keeps.

    A failed producer is retried on the next call, and `attempts` counts the
    goes at the same unchanged input. There is no retry cap here on purpose:
    the pipeline records, and a policy about when to stop belongs where the
    retrying is scheduled, not where it is written down.
    """
    source = _source(name)
    stat = source.stat()
    key = source.name

    report = {
        "name": key,
        "ran": [],
        "current": [],
        "deferred": [],
        "failed": {},
        "unavailable": {},
    }

    handling = producers_for(source.suffix)
    if not handling:
        return report

    connection = connect()
    try:
        existing = _rows(connection, key)
        for producer in handling:
            row = existing.get(producer.name)
            if row is not None and _is_current(row, stat, producer) and row["status"] != FAILED:
                report["current"].append(producer.name)
                continue
            if only_fast and producer.slow:
                report["deferred"].append(producer.name)
                continue

            # A run against unchanged input is another go at the same thing; a
            # run because the file or the producer changed is a fresh start,
            # and carrying the old count forward would read as if the new
            # version had already failed three times.
            retry = row is not None and _is_current(row, stat, producer)
            attempts = int(row["attempts"]) + 1 if retry else 1

            folder = output_dir(producer.name, key)
            folder.mkdir(parents=True, exist_ok=True)
            try:
                result = producer.run(source, folder)
            except ProducerUnavailable as exc:
                _prune(folder)
                report["unavailable"][producer.name] = str(exc)[:MAX_DETAIL]
                continue
            except Exception as exc:  # noqa: BLE001 - recorded, never raised
                _prune(folder)
                detail = f"{type(exc).__name__}: {exc}"
                _record(connection, key, producer, FAILED, stat, None, detail, attempts)
                report["failed"][producer.name] = detail[:MAX_DETAIL]
                continue

            _prune(folder)
            _record(
                connection, key, producer, result.status, stat,
                result.output_path, result.detail, attempts,
            )
            report["ran"].append(producer.name)
        return report
    finally:
        connection.close()


def needs(name: str) -> list[str]:
    """Which producers are stale for this file, including ones never run."""
    source = _source(name)
    stat = source.stat()
    connection = connect()
    try:
        existing = _rows(connection, source.name)
        out = []
        for producer in producers_for(source.suffix):
            row = existing.get(producer.name)
            if row is None or row["status"] == FAILED or not _is_current(row, stat, producer):
                out.append(producer.name)
        return out
    finally:
        connection.close()


def status(name: str) -> dict:
    """Is this document ready, and what failed?

    The question the index cannot answer today, and the reason this step
    exists. `ready` is every handling producer holding a current row that is
    not a failure -- so a document whose page images failed is not ready, and
    says which half is missing rather than quietly returning less.
    """
    source = _source(name)
    stat = source.stat()
    connection = connect()
    try:
        existing = _rows(connection, source.name)
        detail: dict[str, dict] = {}
        missing, stale_now, failed = [], [], []
        for producer in producers_for(source.suffix):
            row = existing.get(producer.name)
            if row is None:
                missing.append(producer.name)
                detail[producer.name] = {"status": "never run", "stale": True}
                continue
            is_stale = not _is_current(row, stat, producer)
            if is_stale:
                stale_now.append(producer.name)
            if row["status"] == FAILED:
                failed.append(producer.name)
            detail[producer.name] = {
                "status": row["status"],
                "producer_version": int(row["producer_version"]),
                "stale": is_stale,
                "output_path": row["output_path"],
                "detail": row["detail"],
                "attempts": int(row["attempts"]),
                "updated_at": row["updated_at"],
            }
        return {
            "name": source.name,
            "ready": not missing and not stale_now and not failed,
            "producers": detail,
            "missing": missing,
            "stale": stale_now,
            "failed": failed,
        }
    finally:
        connection.close()


def forget(name: str) -> dict:
    """A file left. Drop its rows and everything produced from it.

    Takes a name rather than a path, and does NOT require the source to still
    exist -- this is called precisely when it does not. Every producer's
    directory for this source goes, whether or not that producer is still
    registered, because a producer removed from the code does not take its old
    output with it.
    """
    key = Path(str(name).replace("\\", "/")).name
    if not key or key in {".", ".."}:
        raise IngestError(f"{name!r} does not name a file.")

    removed = []
    root = ensure_derived_dir()
    for producer_folder in sorted(p for p in root.iterdir() if p.is_dir()):
        target = producer_folder / key
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(f"{producer_folder.name}/{key}")
        _prune_if_empty(producer_folder)

    connection = connect()
    try:
        dropped = connection.execute(
            "DELETE FROM artifacts WHERE source_name = ?", (key,)
        ).rowcount
        connection.commit()
    finally:
        connection.close()
    return {"name": key, "rows": dropped, "removed": removed}


def rebuild(only_fast: bool = False, report: Callable[[float, str], None] | None = None) -> dict:
    """Run every registered producer over every file in the tenant's folder.

    The proof that derived/ is disposable, and the same shape as
    search.rebuild() for the same reason: something that can always be rebuilt
    cannot accumulate anything irreplaceable.
    """
    from app.config import ensure_data_dir  # local: keeps the import graph flat

    suffixes = {s for p in _PRODUCERS.values() for s in p.handles}
    files = sorted(
        p for p in ensure_data_dir().iterdir()
        if p.is_file() and p.suffix.lower() in suffixes and not p.name.startswith(".")
    )
    started = time.time()
    reports = []
    for number, path in enumerate(files, start=1):
        reports.append(ingest(path.name, only_fast=only_fast))
        if report:
            report(number / max(1, len(files)), f"{number} of {len(files)}: {path.name}")
    return {
        "files_seen": len(files),
        "ran": sum(len(r["ran"]) for r in reports),
        "failed": sum(len(r["failed"]) for r in reports),
        "deferred": sum(len(r["deferred"]) for r in reports),
        "unavailable": sorted({n for r in reports for n in r["unavailable"]}),
        "seconds": round(time.time() - started, 2),
    }
