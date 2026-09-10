"""The producers themselves. Step 2.1.

`app/ingest.py` is the pipeline and knows nothing about what a producer makes.
This is where the things that read a file actually live, and today there is
exactly one of them: the text extraction that used to sit inside
`app/search.py`, MOVED AND NOT REWRITTEN.

WHY IT MOVED, GIVEN THAT NOTHING ABOUT IT CHANGED
    It was search's private code and every future consumer would have had to
    reach into search to get at it -- or, more likely, extract the file again
    for itself. Chunking for embeddings, numeric field extraction and vision
    all start by opening the same PDF. One extraction, recorded once, is the
    whole point of Step 2, and it only pays off if the first producer is the
    one that already exists.

    `search.index_file` is now a CONSUMER of this rather than the owner of it.
    That is the entire behavioural change, and the gate for this sub-step is
    that there isn't one.

WHY THE TEXT IS WRITTEN TO A FILE AND NOT JUST RETURNED
    The next consumer is not the index. An embeddings producer wants the text
    to chunk, and having it on disk means it chunks what the index indexed
    rather than re-parsing the PDF and hoping the two agree. The FTS5 row
    already keeps its own copy of the text for its own reasons (see the rules
    at the top of app/search.py); this is not that copy, and neither is
    derived from the other.

THE IMPORT DIRECTION MATTERS
    config <- ingest <- producers <- search. This module must never import
    search: search consumes producers, and a cycle would be the shape of the
    old arrangement smuggled back in.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from app import ingest

# Moved verbatim from app/search.py. The suffixes a producer handles are now a
# property of the producer rather than of the index, which is what lets a later
# producer handle .png without the index having an opinion about it.
SEARCHABLE = {".pdf", ".xlsx", ".xlsm"}
MAX_TEXT_PER_FILE = 400_000

TEXT_ARTIFACT = "text.txt"


def _reader(module: str, what: str):
    """Import a parser, or say plainly that it is missing.

    A missing library is NOT an unreadable file, and catching both with one
    `except Exception` made them indistinguishable. Running a rebuild under an
    interpreter without pymupdf indexed nineteen perfectly good PDFs as empty
    and reported each as "no extractable text, probably a scan": a cause the
    code had not established, for a folder that was entirely fine.

    An unreadable file affects one document. A missing parser affects every
    document of that type, and it is an environment fault, not a data fault.

    It raises ProducerUnavailable now rather than SearchError, which is the
    same distinction the pipeline already draws for its own reasons: an
    environment fault is reported once and written against nothing, so that
    installing the package is the whole fix rather than the start of one.
    `search.index_file` translates it back into a SearchError for its own
    callers, because their contract has not changed.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ingest.ProducerUnavailable(
            f"{module} is not installed in the interpreter running this "
            f"({sys.executable}), so {what}. Every file of that type would be "
            "indexed as empty, which reads like a folder of unreadable documents "
            "rather than a missing package. Install the project's requirements, "
            "or run this with the virtualenv's python."
        ) from exc


def require_readers(suffixes) -> None:
    """Check the parsers for these file types are present, before doing work.

    Exists because rebuild() used to DELETE the index and discover the missing
    parser afterwards, so a run under the wrong interpreter cost the whole
    index and put nothing in its place. Same shape as the row cap that was
    applied after the fetch: a limit enforced after the cost is paid is not a
    limit.
    """
    for suffix in sorted({str(s).lower() for s in suffixes}):
        if suffix == ".pdf":
            _reader("pymupdf", "no PDF can be read")
        elif suffix in {".xlsx", ".xlsm"}:
            _reader("openpyxl", "no spreadsheet can be read")


def extract(path: Path) -> str:
    """Pull readable text out of one file. Empty string if there is none.

    Raises ProducerUnavailable if the parser for this file type is not
    installed. That is deliberately not the same outcome as a file that cannot
    be read.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        pymupdf = _reader("pymupdf", "no PDF can be read")
        try:
            with pymupdf.open(path) as document:
                pages = [document.load_page(i).get_text("text") for i in range(document.page_count)]
            return "\n".join(pages)[:MAX_TEXT_PER_FILE]
        except Exception:  # noqa: BLE001 - this one file is unreadable, never fatal
            return ""

    if suffix in {".xlsx", ".xlsm"}:
        openpyxl = _reader("openpyxl", "no spreadsheet can be read")
        try:
            book = openpyxl.load_workbook(path, data_only=True, read_only=True)
            try:
                parts: list[str] = []
                for sheet in book.worksheets:
                    parts.append(sheet.title)
                    for row in sheet.iter_rows(values_only=True):
                        cells = [str(v) for v in row if v is not None]
                        if cells:
                            parts.append(" ".join(cells))
                        if sum(len(p) for p in parts) > MAX_TEXT_PER_FILE:
                            break
            finally:
                book.close()
            return "\n".join(parts)[:MAX_TEXT_PER_FILE]
        except Exception:  # noqa: BLE001
            return ""

    return ""


# --------------------------------------------------------------------------
# the text producer
# --------------------------------------------------------------------------

def _run_text(source: Path, out_dir: Path) -> ingest.Result:
    text = extract(source)
    if not text.strip():
        # Not a failure. "Nobody has looked at this" and "we looked, and there
        # is nothing here" are different states, and the reason deliberately
        # does not name a cause it has not established -- a scan is ONE
        # explanation for a file with no text in it.
        return ingest.Result.nothing("no text could be extracted from it")
    written = out_dir / TEXT_ARTIFACT
    written.write_text(text, encoding="utf-8")
    return ingest.Result.made(
        written.relative_to(out_dir.parent.parent),
        f"{len(text)} characters",
    )


TEXT = ingest.register(ingest.Producer(
    name="text",
    version=1,
    handles=frozenset(SEARCHABLE),
    # Fast, and it stays in the request. Decision 4.3: extracting text from a
    # 2 MB PDF is already fast enough that "upload then immediately ask about
    # it" works, and that must go on working. The slow producers that arrive
    # later are the ones the job lane exists for.
    slow=False,
    run=_run_text,
))


def text_of(source_name: str) -> str | None:
    """The extracted text for this file, or None if there is none.

    Reads the artifact rather than the manifest, so a consumer gets the same
    bytes the producer wrote. None covers both "nothing was extractable" and
    "this has not been produced yet"; a caller that needs to tell those apart
    asks ingest.status().
    """
    from app.config import derived_dir  # local: this module is imported early

    written = derived_dir() / TEXT.name / source_name / TEXT_ARTIFACT
    try:
        return written.read_text(encoding="utf-8")
    except OSError:
        return None
