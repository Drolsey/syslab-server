"""A company's corpus on disk, for the operator dashboard (Step 11.5).

Filesystem work only: what is in a company's folder, putting a file into it,
taking one out. Everything here runs inside `context.use_tenant(...)`, so every
path goes through `config.resolve_in_data_dir` -- the same traversal and
sideways-tenant check the rest of the app is gated on -- rather than a second
copy of it written here.

Nothing is ever deleted from a company's folder by this module. `trash` moves
things to TRASH_ROOT, outside data/, where the ingestion pipeline cannot see
them and an operator can still get them back.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from app import config, ingest, parse, passages, retrieve, search, tenancy
from app.config import UnsafePathError, data_dir, data_files, doc_id, resolve_in_data_dir

MAX_DEPTH = 16
MAX_PATH = 255
# Letters and digits in ANY script -- a client's corpus may well be named in
# Arabic -- plus a few characters people actually put in filenames. Everything
# else, including every character Windows or a shell treats specially, becomes _.
_UNSAFE = re.compile(r"[^\w .,()&'+\-]", re.UNICODE)
# Moved here from scripts/tenant.py so the CLI and the dashboard retire a
# company the same way.
REMOVED = "_removed"


class CorpusError(Exception):
    """Something about a corpus operation a person can read and act on."""


def _clean_segment(raw: str) -> str | None:
    """One path segment made safe to write, or None if it should not be written.

    A hidden segment (.git, .DS_Store, ._foo) is None rather than renamed:
    a folder upload drags those along and nobody meant them to be documents.
    """
    segment = unicodedata.normalize("NFKC", raw or "").strip()
    if not segment or segment.startswith("."):
        return None
    segment = _UNSAFE.sub("_", segment).strip(". ")
    return segment or None


def clean_relative(raw: str) -> str | None:
    """A browser-supplied relative path, cleaned segment by segment. None means skip it."""
    parts = [p for p in str(raw or "").replace("\\", "/").split("/") if p not in {"", "."}]
    if any(p == ".." for p in parts):
        raise CorpusError(f"Refusing a path that climbs out of the folder: {raw!r}")
    cleaned = [_clean_segment(p) for p in parts]
    if any(c is None for c in cleaned):
        return None
    joined = "/".join(cleaned)
    if len(cleaned) > MAX_DEPTH:
        raise CorpusError(f"{raw!r} is nested more than {MAX_DEPTH} folders deep.")
    if len(joined) > MAX_PATH:
        raise CorpusError(f"{raw!r} is longer than {MAX_PATH} characters.")
    return joined


def _folder(sub: str) -> Path:
    if not sub or sub.strip("/") == "":
        return config.ensure_data_dir()
    try:
        path = resolve_in_data_dir(sub)
    except UnsafePathError as exc:
        raise CorpusError(str(exc)) from exc
    if not path.is_dir():
        raise CorpusError(f"No folder called {sub!r}.")
    return path


def _state(name: str, suffix: str, manifest) -> dict:
    """A document's ingestion state as the dashboard shows it."""
    if not ingest.producers_for(suffix):
        return {"state": "unsupported"}
    try:
        status = ingest.status(name, connection=manifest)
    except ingest.IngestError as exc:
        return {"state": "error", "detail": str(exc)}
    if status["failed"]:
        detail = "; ".join(
            f"{p}: {status['producers'][p].get('detail') or 'failed'}" for p in status["failed"]
        )
        return {"state": "failed", "detail": detail[:600]}
    return {"state": "ready" if status["ready"] else "outstanding"}


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def summary() -> dict:
    """Counts for the current tenant's corpus.

    ponytail: one ingest.status() per document over ONE manifest connection --
    about 2 ms a document. A few thousand is a second or two; a single pass over
    the manifest rows if a real corpus makes that too slow.
    """
    counts = {"documents": 0, "ready": 0, "outstanding": 0, "failed": 0, "unsupported": 0,
              "bytes": 0}
    manifest = ingest.connect()
    try:
        for path in data_files():
            counts["bytes"] += path.stat().st_size
            state = _state(doc_id(path), path.suffix, manifest)["state"]
            if state == "unsupported":
                counts["unsupported"] += 1
                continue
            counts["documents"] += 1
            counts["failed" if state in {"failed", "error"} else state] += 1
    finally:
        manifest.close()
    return counts


def listing(sub: str = "") -> dict:
    """One folder's direct contents: subfolders, then files with their state."""
    folder = _folder(sub)
    root = data_dir().resolve()
    folders, files = [], []
    manifest = ingest.connect()
    try:
        for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            rel = child.resolve().relative_to(root).as_posix()
            if child.is_dir():
                folders.append({"name": child.name, "path": rel})
            elif child.is_file():
                stat = child.stat()
                files.append({
                    "name": child.name, "path": rel, "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    **_state(rel, child.suffix, manifest),
                })
    finally:
        manifest.close()
    here = "" if folder.resolve() == root else folder.resolve().relative_to(root).as_posix()
    return {"path": here, "folders": folders, "files": files}


def download_path(rel: str) -> Path:
    try:
        path = resolve_in_data_dir(rel)
    except UnsafePathError as exc:
        raise CorpusError(str(exc)) from exc
    if not path.is_file():
        raise CorpusError(f"No file called {rel!r}.")
    return path


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def accepted_suffixes() -> list[str]:
    return sorted(parse.handles())


def store(directory: str, filename: str, stream: BinaryIO, *, replace: bool = False,
          max_bytes: int | None = None) -> dict:
    """Write one uploaded file into the current tenant's folder. Never ingests.

    Returns {"path", "status"}: stored, replaced, exists (left alone), skipped
    (hidden or a type nothing reads). Written to a temporary file beside the
    target and renamed into place, so a failed or oversized upload never leaves
    half a document for the pipeline to find.
    """
    max_bytes = max_bytes or config.MAX_UPLOAD_BYTES
    relative = clean_relative(f"{directory}/{filename}" if directory else filename)
    if relative is None:
        return {"path": filename, "status": "skipped", "reason": "hidden file or folder"}
    suffix = Path(relative).suffix.lower()
    if suffix not in parse.handles():
        return {"path": relative, "status": "skipped",
                "reason": f"{suffix or 'no extension'} is not a type the pipeline reads"}
    try:
        target = resolve_in_data_dir(relative)
    except UnsafePathError as exc:
        raise CorpusError(str(exc)) from exc
    existed = target.exists()
    if existed and not target.is_file():
        raise CorpusError(f"{relative!r} is a folder.")
    if existed and not replace:
        return {"path": relative, "status": "exists"}

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
    total = 0
    try:
        with os.fdopen(handle, "wb") as out:
            while chunk := stream.read(1_048_576):
                total += len(chunk)
                if total > max_bytes:
                    raise CorpusError(
                        f"{relative!r} is larger than the {max_bytes // 1_048_576} MB limit "
                        "(MAX_UPLOAD_MB)."
                    )
                out.write(chunk)
        if total == 0:
            raise CorpusError(f"{relative!r} is empty.")
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return {"path": relative, "status": "replaced" if existed else "stored", "bytes": total}


def make_folder(sub: str) -> str:
    relative = clean_relative(sub)
    if not relative:
        raise CorpusError("That is not a usable folder name.")
    try:
        path = resolve_in_data_dir(relative)
    except UnsafePathError as exc:
        raise CorpusError(str(exc)) from exc
    if path.exists() and not path.is_dir():
        raise CorpusError(f"{relative!r} is a file.")
    path.mkdir(parents=True, exist_ok=True)
    return relative


def sweep() -> dict:
    """Drop everything derived from files that have left. The existing sweeps, in order."""
    swept = {"artifacts": ingest.forget_missing()["gone"], "index": search.forget_missing(),
             "passages": passages.forget_missing()}
    if "vector" in retrieve.registered():
        from app import vectors

        swept["vectors"] = vectors.forget_missing()
    return swept


def trash(rel: str) -> dict:
    """Move a file or folder out of the corpus into TRASH_ROOT, then sweep."""
    try:
        path = resolve_in_data_dir(rel)
    except UnsafePathError as exc:
        raise CorpusError(str(exc)) from exc
    root = data_dir().resolve()
    if path == root or not path.exists():
        raise CorpusError(f"Nothing called {rel!r} to remove.")
    relative = path.relative_to(root).as_posix()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = config.TRASH_ROOT / root.name / stamp / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(destination))
    return {"path": relative, "moved_to": str(destination), "swept": sweep()}


# --------------------------------------------------------------------------
# retiring a company
# --------------------------------------------------------------------------

def retire_company(tenant_id: str, purge_files: bool = False) -> dict:
    """Remove a disabled company: control-plane rows, index, derived, and its files.

    The files are MOVED to data/_removed/ unless purge_files -- a customer's
    documents are not something to lose to a misclick. The index and derived
    artifacts are deleted outright: every byte of them can be made again from
    the files, and keeping them would keep a second copy of the customer's text.
    tenancy.delete_tenant() refuses an active tenant, so disabling first is
    enforced there and not only by the callers.
    """
    removed = tenancy.delete_tenant(tenant_id)
    result = {"control": removed, "index": False, "derived": False, "files": "none on disk"}

    for suffix in ("", "-wal", "-shm"):
        index = config.INDEX_ROOT / f"{tenant_id}.sqlite3{suffix}"
        if index.exists():
            index.unlink()
            result["index"] = True
    derived = config.DERIVED_ROOT / tenant_id
    if derived.exists():
        shutil.rmtree(derived)
        result["derived"] = True

    folder = config.DATA_ROOT / tenant_id
    if folder.exists():
        if purge_files:
            shutil.rmtree(folder)
            result["files"] = "deleted"
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = config.DATA_ROOT / REMOVED / f"{tenant_id}-{stamp}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(destination))
            result["files"] = f"moved to {destination}"
    return result
