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
    A name, a version, the suffixes it handles, whether it is slow, what it
    reads from other producers, and a function. The pipeline knows nothing
    about what a producer makes. It knows that something was asked for, whether
    it succeeded, when, and against which version of the producer -- which is
    exactly what lets a later step add page images without touching this file.

    `depends_on` is Step 4.3 and is the only one of those that took a
    behavioural change to add. Everything before it read the source file and
    nothing else; the chunk producer reads the TEXT ARTIFACT, and a pipeline
    that runs its producers in alphabetical order runs `chunks` before `text`.

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

from app import sources
from app.config import ensure_derived_dir, manifest_path

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

    `depends_on` NAMES THE PRODUCERS WHOSE OUTPUT THIS ONE READS, and it is new
    in Step 4.3. Until then every producer read the source file and nothing
    else, so the pipeline could run them in any order it liked and chose
    alphabetical. The chunk producer consumes the TEXT ARTIFACT -- decision
    5.3, so that a document is never chunked from bytes the index never saw --
    and alphabetical order runs `chunks` before `text`. On a fresh file that is
    a producer reading an artifact that does not exist yet.

    Declaring it buys three things that were otherwise three separate bits of
    remembering: the run order, the staleness (re-extracting the text must
    re-chunk, and the manifest has no column that would have noticed), and the
    hold-back (text failed on a damaged file means chunks must not run and
    record a second failure blaming itself for the first one's problem).
    """

    name: str
    version: int
    handles: frozenset[str]
    slow: bool
    run: Callable[[Path, Path], Result]
    depends_on: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError(
                f"{self.name!r} is not a usable producer name: it becomes a folder "
                "name under derived/<tenant>/, so it cannot be empty or hold a "
                "path separator."
            )
        if self.version < 1:
            raise ValueError("A producer version starts at 1 and only goes up.")
        if self.name in self.depends_on:
            raise ValueError(
                f"{self.name!r} cannot depend on itself: it would never be in a "
                "state where it was allowed to run."
            )


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


def _in_dependency_order(handling: list[Producer]) -> list[Producer]:
    """The same producers, with every one placed after what it reads.

    Takes a list that is ALREADY SORTED BY NAME and keeps that as the
    tie-break, so the order is a function of the registry and nothing else.
    Two producers that depend on nothing come out alphabetically, as they
    always did; a producer that depends on something comes out after it.

    A dependency that is not in this list -- unregistered, or handling other
    suffixes -- is ignored HERE and left to the producer's own `run` to notice.
    Ordering cannot fix an absence, and refusing to run the whole file because
    one producer named something optional would be the pipeline deciding a
    question that belongs to the producer.
    """
    by_name = {p.name: p for p in handling}
    ordered: list[Producer] = []
    placed: set[str] = set()
    visiting: list[str] = []

    def place(producer: Producer) -> None:
        if producer.name in placed:
            return
        if producer.name in visiting:
            # Registered at import time, so this would otherwise surface as a
            # RecursionError on the first upload after somebody wired two
            # producers into each other.
            circle = visiting[visiting.index(producer.name):] + [producer.name]
            raise IngestError(
                "These producers depend on each other in a circle and none of "
                f"them can ever run: {' -> '.join(circle)}."
            )
        visiting.append(producer.name)
        for needed in sorted(producer.depends_on):
            upstream = by_name.get(needed)
            if upstream is not None:
                place(upstream)
        visiting.pop()
        placed.add(producer.name)
        ordered.append(producer)

    for producer in handling:
        place(producer)
    return ordered


def producers_for(suffix: str) -> list[Producer]:
    """Which producers handle this file type, in an order they can run in.

    A producer that does not handle the suffix gets no row. The alternative --
    a `skipped` row per producer per file -- fills the manifest with the
    absence of work nobody asked for, and buries the skips that mean something.

    The order was alphabetical until Step 4.3 and is now dependency-first,
    still alphabetical among equals. See Producer.depends_on for why a name
    sort stopped being enough.
    """
    lowered = suffix.lower()
    return _in_dependency_order(sorted(
        (p for p in _PRODUCERS.values() if lowered in p.handles),
        key=lambda p: p.name,
    ))


def _must_run(existing: dict, stat, handling: list[Producer]) -> set[str]:
    """Which of these producers are not up to date, dependencies included.

    `handling` must already be in dependency order, which is what makes one
    pass enough: by the time a producer is looked at, everything it reads has
    already been decided.

    THE SECOND CLAUSE IS THE ONE THE MANIFEST CANNOT SEE FOR ITSELF. A row
    records the source's size and mtime and its OWN producer version. Nothing
    in it records which version of the text artifact the chunks were cut from,
    so bumping the text producer would re-extract every document and leave
    every chunk exactly where it was -- offsets into a file that had been
    rewritten underneath them. A schema column could have carried it; a
    declared dependency carries it without a migration, and is also what the
    run order needs anyway.
    """
    stale: set[str] = set()
    for producer in handling:
        row = existing.get(producer.name)
        if row is None or row["status"] == FAILED or not _is_current(row, stat, producer):
            stale.add(producer.name)
        elif producer.depends_on & stale:
            stale.add(producer.name)
    return stale


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
    """One item, on local disk, whatever kind of source it came from.

    Step 4.2's source seam. The resolution itself moved to app/sources.py and
    was not rewritten; what stays here is the translation, so that every caller
    of this module still sees IngestError and nothing downstream learned a new
    exception type the day sources became a thing.
    """
    try:
        return sources.active().fetch(name)
    except sources.SourceError as exc:
        raise IngestError(str(exc)) from exc


def _items() -> list[sources.Item]:
    """Everything this tenant has, translated at the same boundary as _source.

    A separate function because the first version of this seam translated only
    `fetch` and let `list` raise SourceError straight through rebuild() and
    forget_missing(). Callers of those catch IngestError -- so a source that
    could not answer would have been a 500 in the two paths that walk the
    WHOLE folder, and a clean 404 in the one that opens a single file. One
    boundary, or it is not a boundary.
    """
    try:
        return sources.active().list()
    except sources.SourceError as exc:
        raise IngestError(str(exc)) from exc


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
        # Step 4.3. A producer that was ready to run and was not allowed to,
        # because something it reads did not finish in this pass. Reported
        # rather than silently absent: "chunks is missing and nothing said why"
        # is the shape of failure decision 4.5 exists to prevent, and the
        # answer here is always somewhere else in this same report.
        "blocked": {},
    }

    handling = producers_for(source.suffix)
    if not handling:
        return report

    connection = connect()
    try:
        existing = _rows(connection, key)
        must_run = _must_run(existing, stat, handling)
        # Everything that did not finish in this pass, for whatever reason.
        # Whether that was a failure, a missing library or a deferral does not
        # change what it means to a producer downstream of it.
        held_back: set[str] = set()
        for producer in handling:
            row = existing.get(producer.name)
            if producer.name not in must_run:
                report["current"].append(producer.name)
                continue

            waiting_for = sorted(producer.depends_on & held_back)
            if waiting_for:
                # NO ROW IS WRITTEN, deliberately. A `failed` row here would
                # blame this producer for a fault upstream of it, and a
                # `skipped` row would claim there was nothing to make when
                # there was. Leaving the row absent leaves status() saying
                # "never run" and ready=False, which is the truth, and the next
                # ingest picks it up the moment the upstream one works.
                report["blocked"][producer.name] = (
                    f"{', '.join(waiting_for)} did not finish, and this reads what it makes"
                )
                held_back.add(producer.name)
                continue

            if only_fast and producer.slow:
                report["deferred"].append(producer.name)
                held_back.add(producer.name)
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
                held_back.add(producer.name)
                continue
            except Exception as exc:  # noqa: BLE001 - recorded, never raised
                _prune(folder)
                detail = f"{type(exc).__name__}: {exc}"
                _record(connection, key, producer, FAILED, stat, None, detail, attempts)
                report["failed"][producer.name] = detail[:MAX_DETAIL]
                held_back.add(producer.name)
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
    """Which producers are stale for this file, including ones never run.

    In dependency order, and a producer downstream of a stale one is stale
    too -- so this and `ingest()` cannot disagree about what is outstanding.
    They used to compute it separately, which was fine while the answer was one
    line long in both places.
    """
    source = _source(name)
    stat = source.stat()
    connection = connect()
    try:
        handling = producers_for(source.suffix)
        stale = _must_run(_rows(connection, source.name), stat, handling)
        return [p.name for p in handling if p.name in stale]
    finally:
        connection.close()


def deferred(name: str) -> list[str]:
    """The SLOW producers still outstanding for this file.

    What `ingest(only_fast=True)` left behind, asked as a question rather than
    remembered from a return value -- so the caller that schedules the work
    does not have to be the same call that skipped it, and a file left
    half-produced by a restart is still answerable.
    """
    return [n for n in needs(name) if n in _PRODUCERS and _PRODUCERS[n].slow]


def status(name: str) -> dict:
    """Is this document ready, and what failed?

    The question the index cannot answer today, and the reason this step
    exists. `ready` is every handling producer holding a current row that is
    not a failure -- so a document whose page images failed is not ready, and
    says which half is missing rather than quietly returning less.

    Since Step 4.3 a producer is ALSO stale when something it reads is about to
    be remade, which is how a text re-extraction stops a document reporting
    ready while its chunks still hold offsets into the previous extraction.
    """
    source = _source(name)
    stat = source.stat()
    connection = connect()
    try:
        handling = producers_for(source.suffix)
        existing = _rows(connection, source.name)
        must_run = _must_run(existing, stat, handling)
        detail: dict[str, dict] = {}
        missing, stale_now, failed = [], [], []
        for producer in handling:
            row = existing.get(producer.name)
            if row is None:
                missing.append(producer.name)
                detail[producer.name] = {"status": "never run", "stale": True}
                continue
            # A row that is out of date on its own terms, or one whose upstream
            # is. A FAILED row that is otherwise current is reported as failed
            # and not as stale, which is the distinction this already drew.
            is_stale = not _is_current(row, stat, producer) or bool(producer.depends_on & must_run)
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


def forget_missing() -> dict:
    """Drop everything belonging to source files that are no longer on disk.

    The counterpart to search.forget_missing(), and it exists for the same
    reason: artifacts are written when a file arrives and never when one
    leaves, so a deleted document went on owning a manifest row and a folder of
    derived bytes for ever. `scripts/check_search.py` writes 41 decoy PDFs,
    deletes them, and used to leave 41 text artifacts behind every run.

    Reconciles BOTH sides against data/, not just the manifest. A producer
    folder can hold output for a source with no row -- a crash between writing
    the bytes and recording them -- and a row can name a source whose folder was
    never made. Sweeping only the rows would leave the first kind on disk with
    nothing pointing at it, which is the harder sort to notice.

    Deliberately NOT called from search()'s read path. search.forget_missing()
    is there because a stale index row is returned to the model and wastes a
    turn; nothing reads an orphaned artifact, so paying for a manifest open on
    every search would buy tidiness at the cost of the hot path. It runs at
    rebuild, and when someone asks.
    """
    root = ensure_derived_dir()
    present = {item.id for item in _items()}

    known: set[str] = set()
    connection = connect()
    try:
        known.update(
            row["source_name"]
            for row in connection.execute("SELECT DISTINCT source_name FROM artifacts")
        )
    finally:
        connection.close()
    for producer_folder in (p for p in root.iterdir() if p.is_dir()):
        known.update(p.name for p in producer_folder.iterdir() if p.is_dir())

    # Set membership rather than `(folder / name).is_file()`. Same answer for
    # every name the pipeline can produce -- source_name is always a bare
    # filename -- and it no longer turns a manifest row into a path lookup,
    # which is the sort of thing a hand-edited control plane gets to exploit.
    gone = sorted(known - present)
    removed = []
    for name in gone:
        removed.append(forget(name))
    return {
        "gone": gone,
        "rows": sum(r["rows"] for r in removed),
        "removed": sorted(p for r in removed for p in r["removed"]),
    }


def rebuild(only_fast: bool = False, report: Callable[[float, str], None] | None = None) -> dict:
    """Run every registered producer over every file in the tenant's folder.

    The proof that derived/ is disposable, and the same shape as
    search.rebuild() for the same reason: something that can always be rebuilt
    cannot accumulate anything irreplaceable.
    """
    # Reconcile before producing. A rebuild that added what is missing but left
    # what should not be there would make "rebuilt" mean less every time it
    # ran, and this is the one moment the whole folder is already being walked.
    swept = forget_missing()

    # The filtering is HERE and not in the source, deliberately: a source that
    # returned only what some producer handles would make an unhandled item
    # look deleted to forget_missing() above, which would then sweep the
    # artifacts of a file sitting right there.
    suffixes = {s for p in _PRODUCERS.values() for s in p.handles}
    items = [
        item for item in _items()
        if item.suffix in suffixes and not item.id.startswith(".")
    ]
    started = time.time()
    reports = []
    for number, item in enumerate(items, start=1):
        reports.append(ingest(item.id, only_fast=only_fast))
        if report:
            report(number / max(1, len(items)), f"{number} of {len(items)}: {item.id}")
    return {
        "files_seen": len(items),
        "ran": sum(len(r["ran"]) for r in reports),
        "failed": sum(len(r["failed"]) for r in reports),
        "deferred": sum(len(r["deferred"]) for r in reports),
        "unavailable": sorted({n for r in reports for n in r["unavailable"]}),
        "forgotten": swept["gone"],
        "seconds": round(time.time() - started, 2),
    }
