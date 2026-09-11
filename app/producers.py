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

from app import ingest, parse

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

    Kept as the name every caller already uses; app/parse owns the answer now.
    """
    parse.require_readers(suffixes)


def extract(path: Path) -> str:
    """Text out of one file, or "" -- the version 1 signature, kept for callers.

    DEPRECATED in favour of parse.extract, which returns an OUTCOME as well as
    the text. This shape cannot tell a damaged file from a scan, and the whole
    point of 4.2 was that those are different. Nothing in the app calls it;
    it stays because tests and gates written against version 1 do.
    """
    return parse.extract(path).text


# --------------------------------------------------------------------------
# the text producer
# --------------------------------------------------------------------------

def _run_text(source: Path, out_dir: Path) -> ingest.Result:
    found = parse.extract(source)

    if found.outcome is parse.Outcome.UNREADABLE:
        # RAISED, not returned. ingest.Result says it plainly -- "what a
        # producer says it did. Never how it failed -- that is an exception" --
        # and the pipeline catches this, records FAILED against this one
        # document, and carries on with the folder.
        #
        # New in version 2. Until now a damaged file returned "nothing
        # extractable", which is the same answer a SCAN gets, so a folder with
        # one corrupt document was indistinguishable from a folder with one
        # scanned document. tests/fixtures/corpus/formats/ holds one of each.
        raise ValueError(f"{source.name} could not be read: {found.detail}")

    if found.outcome is parse.Outcome.UNSUPPORTED:
        return ingest.Result.nothing(found.detail)

    if found.outcome is parse.Outcome.EMPTY:
        # Not a failure. "Nobody has looked at this" and "we looked, and there
        # is nothing here" are different states, and the reason deliberately
        # does not name a cause it has not established -- though version 2 CAN
        # now say the file itself was fine, which is what an OCR producer will
        # act on when Step 10 gives us one.
        return ingest.Result.nothing(found.detail or "no text could be extracted from it")

    written = out_dir / TEXT_ARTIFACT
    written.write_text(found.text, encoding="utf-8")
    return ingest.Result.made(
        written.relative_to(out_dir.parent.parent),
        f"{len(found.text)} characters via {found.backend}",
    )


TEXT = ingest.register(ingest.Producer(
    name="text",
    # VERSION 2, Step 4.2: the same artifact, produced by a different reader.
    # Every document re-extracts on next ingest, which is the machinery Step
    # 2.4 built and gated, and it is why swapping a parser is a one-line
    # change rather than a migration.
    version=2,
    # Asked of app/parse rather than kept here, so the list of formats this
    # project claims and the list it can actually read cannot drift apart.
    # SEARCHABLE survives as what the INDEX considers worth indexing.
    handles=parse.handles(),
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
