"""Step 4.2: where a tenant's material comes from.

WHY THIS EXISTS
    Today there is one answer and it is hardcoded in three places:
    `ingest._source` resolves a name inside `data/<tenant>/`, `ingest.rebuild`
    walks that folder, and `ingest.forget_missing` asks that folder whether a
    name is still there. Each of those is a sentence about local files written
    into a module that is otherwise about producing artifacts.

    The requirement this step has to survive is that customer material lives in
    SQL databases, Google Docs and cloud storage as well as on disk. Bolted on
    separately those are three more folder-walks, three more places a document
    can be half-processed, and no single answer to "what does this tenant
    have". This module is the one place that decides where things come from, so
    that a connector becomes a new SOURCE in an existing pipeline rather than a
    second pipeline.

    It adds no capability a user would notice. That is the point, and it is the
    same bargain Step 2 made for producers -- which is the evidence this shape
    is worth repeating, because that seam is now four sub-steps old and has not
    had to change.

WHAT A SOURCE IS
    A name, a way to list what it holds, and a way to put one item on local
    disk. The pipeline knows nothing about where the bytes were a moment ago.
    It knows it has a Path it can stat and hand to a parser -- which is exactly
    what lets a Google Drive connector arrive later without producers, the
    manifest, the index or the tenant delete learning what Drive is.

    `fetch` returns a Path and not a stream ON PURPOSE. `ingest._is_current`
    compares size and mtime to decide whether derived data may be served for
    bytes that no longer exist, and parsers want to seek. A remote source
    downloads to a local file and returns it; that cost is real, and it is
    better paid visibly here than smuggled into every consumer.

WHAT IS NOT SOLVED HERE, AND IS NOT PRETENDED TO BE
    ROUTING. The manifest keys an artifact on (source_name, producer) with no
    column saying which source that name came from, so two sources each holding
    an item called contract.pdf would silently share one row and one folder of
    derived bytes. That is a schema change and a migration, and it is not worth
    paying for while one source exists.

    So `active()` RAISES when a second source is registered rather than picking
    one. The day a connector lands, the work that has to happen first announces
    itself instead of quietly corrupting a manifest.

    CREDENTIALS are the other blocker and are also named rather than hidden.
    Any source that is not local files needs THAT CUSTOMER's credentials stored
    encrypted, and `tenancy.database_for()` still raises for every row it finds
    because no cipher was ever chosen -- Step 1's decision 4.5, still open.
    Every cloud connector sits behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class SourceError(Exception):
    """The caller asked a source for something it does not have.

    Deliberately NOT ingest.IngestError, which is what callers still see:
    `app/ingest.py` imports this module, so this module importing it back would
    be a cycle. `ingest._source` translates, and the message a caller reads is
    unchanged from before this seam existed.
    """


@dataclass(frozen=True)
class Item:
    """One thing a source holds, named and typed WITHOUT fetching it.

    Two fields, and the restraint is the design. `id` is what becomes
    `source_name` in the manifest. `suffix` is here because the pipeline has to
    decide which producers handle an item, and a source that could only answer
    that by downloading would make `rebuild` pull ten thousand Google Docs to
    find the four spreadsheets.

    Size and mtime are deliberately ABSENT. The pipeline gets those by
    stat()ing the fetched path -- the same bytes it is about to parse. A source
    reporting them separately would be reporting them about something it had
    not fetched, and `_is_current` is the one comparison in this project that
    decides whether stale derived data may be served.
    """

    id: str
    suffix: str


@runtime_checkable
class Source(Protocol):
    """Where material comes from. `files` is the only one today."""

    name: str

    def list(self) -> list[Item]:
        """Everything this source holds, UNFILTERED.

        Unfiltered matters. A caller that wants only the suffixes some producer
        handles does that filtering itself; a source that filtered would make
        an item with no producer look DELETED to `forget_missing`, which would
        then sweep artifacts belonging to a file sitting right there.
        """
        ...

    def fetch(self, item_id: str) -> Path:
        """A local path holding this item's bytes, or SourceError."""
        ...


@dataclass(frozen=True)
class Files:
    """The tenant's own data folder. Today's behaviour, moved and not rewritten.

    Every path still goes through `config.resolve_in_data_dir`, so the
    traversal defence and the per-tenant redirection are the ones already gated
    by `scripts/check_isolation.py`, rather than a second copy written here
    that would have to be found and fixed twice.
    """

    name: str = "files"

    def list(self) -> list[Item]:
        from app.config import ensure_data_dir  # local: keeps the import graph flat

        return sorted(
            (
                Item(id=p.name, suffix=p.suffix.lower())
                for p in ensure_data_dir().iterdir()
                if p.is_file()
            ),
            key=lambda item: item.id,
        )

    def fetch(self, item_id: str) -> Path:
        from app.config import UnsafePathError, resolve_in_data_dir

        try:
            path = resolve_in_data_dir(item_id)
        except UnsafePathError as exc:
            raise SourceError(str(exc)) from exc
        if not path.is_file():
            raise SourceError(f"No file named {item_id!r} in this tenant's folder.")
        return path


_SOURCES: dict[str, Source] = {}


def register(source: Source) -> Source:
    """Add a source to the pipeline. A module registers itself at import."""
    _SOURCES[source.name] = source
    return source


def unregister(name: str) -> None:
    _SOURCES.pop(name, None)


def registered() -> dict[str, Source]:
    """A copy, so a caller holding this cannot quietly change the pipeline."""
    return dict(_SOURCES)


def active() -> Source:
    """The source this tenant's material comes from.

    Raises when there is more than one, and the refusal is the useful part.
    See the module docstring: the manifest has no column for which source a
    name came from, so the moment two exist, one of them is silently writing
    over the other's rows. A function that picked the first would turn a
    missing schema change into corrupted derived data discovered months later.
    """
    if not _SOURCES:
        raise SourceError(
            "No source is registered, so there is nowhere for this tenant's "
            "material to come from. This module registers `files` at import, "
            "so something has unregistered it."
        )
    if len(_SOURCES) > 1:
        raise SourceError(
            f"{len(_SOURCES)} sources are registered ({', '.join(sorted(_SOURCES))}) "
            "and the pipeline cannot yet tell their items apart: the manifest "
            "keys on (source_name, producer) with no column for which source "
            "that name came from, so two sources each holding a contract.pdf "
            "would share one row and one folder of derived bytes. That column, "
            "and the migration for it, comes before the second source does."
        )
    return next(iter(_SOURCES.values()))


register(Files())
