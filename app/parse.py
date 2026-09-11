"""Step 4.2: one way to get text out of a file, whatever kind of file it is.

WHY THIS EXISTS
    `producers.extract` knew two formats, `.pdf` and `.xlsx`, by writing a
    branch for each. That does not survive the requirement it now has to meet:
    customer material arrives in many formats and "it seems stupid to hardcode
    all data types". A branch per format means every new format is a code
    change, a test, and a release.

    So the formats become a property of a LIBRARY rather than of this project,
    and this module is the one place that knows which library reads what.

WHAT WAS ADOPTED, AND THE PART THAT IS NOT OBVIOUS
    Docling (MIT, verified 11 September 2026 from its own LICENSE file).

    But NOT `docling.document_converter.DocumentConverter`, which is its
    headline API. That import pulls the whole pipeline -- ASR, OCR, picture
    description -- and through it scipy, transformers and torch: 85 packages
    and a multi-gigabyte GPU stack, installed on the HOST, on a machine whose
    GPU stack already lives in a container.

    Docling's BACKENDS are usable on their own, and they are the part that
    actually reads files. Measured on this project's own fixtures: 35 packages,
    211 MB, no torch, and **no models downloaded at all** -- which also means
    the runtime-model licence question that Step 4.2 was told to answer does
    not arise yet. It arrives with OCR, in Step 10, and it arrives as a
    `version` bump here because everything under derived/ is rebuildable.

    What is given up: ML layout analysis, table-structure recognition, and OCR.
    All three are Step 10 and none is needed to put text in an index.

THREE OUTCOMES, NOT TWO -- and this is a real bug fixed rather than a tidy-up.
    The old `extract()` caught every exception and returned "". So a corrupt
    PDF and a scanned PDF produced the SAME answer: empty text, recorded as a
    success. They are not the same thing. One is a file nobody can read; the
    other is a file that is perfectly fine and simply has no text layer,
    waiting for an OCR producer that does not exist yet.

    `tests/fixtures/corpus/formats/` holds one of each, on purpose.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app import ingest

MAX_TEXT_PER_FILE = 400_000


class Outcome(str, Enum):
    """What happened, in a form a manifest row can hold."""

    OK = "ok"                      # text came out
    EMPTY = "empty"                # read cleanly, and there is genuinely no text
    UNREADABLE = "unreadable"      # this file is damaged; one document affected
    UNSUPPORTED = "unsupported"    # nothing here reads this kind of file


@dataclass(frozen=True)
class Extraction:
    text: str
    outcome: Outcome
    backend: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


# --------------------------------------------------------------------------
# which backend reads what
# --------------------------------------------------------------------------
#
# Adding a format is a ROW, which is the whole point of the module. The value
# is (module path, class name, InputFormat member, needs_pagination).
_BACKENDS: dict[str, tuple[str, str, str, bool]] = {
    ".pdf":  ("docling.backend.docling_parse_backend", "DoclingParseDocumentBackend", "PDF", True),
    ".docx": ("docling.backend.msword_backend", "MsWordDocumentBackend", "DOCX", False),
    ".xlsx": ("docling.backend.msexcel_backend", "MsExcelDocumentBackend", "XLSX", False),
    ".xlsm": ("docling.backend.msexcel_backend", "MsExcelDocumentBackend", "XLSX", False),
    ".pptx": ("docling.backend.mspowerpoint_backend", "MsPowerpointDocumentBackend", "PPTX", False),
    ".html": ("docling.backend.html_backend", "HTMLDocumentBackend", "HTML", False),
    ".htm":  ("docling.backend.html_backend", "HTMLDocumentBackend", "HTML", False),
    ".csv":  ("docling.backend.csv_backend", "CsvDocumentBackend", "CSV", False),
    ".md":   ("docling.backend.md_backend", "MarkdownDocumentBackend", "MD", False),
    ".txt":  ("", "", "", False),   # read directly; no parser earns its keep here
}


def handles() -> frozenset[str]:
    """Every suffix this module can attempt. The producer declares this rather
    than keeping its own list, so the two cannot drift apart."""
    return frozenset(_BACKENDS)


def _module(name: str, what: str):
    """Import a parser, or say plainly that it is missing.

    Carried over verbatim in spirit from producers._reader, and for the reason
    recorded there: a missing library affects EVERY document of that type and
    is an environment fault, while an unreadable file affects one document and
    is a data fault. Reporting the first as the second indexed nineteen good
    PDFs as empty and blamed the documents.
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ingest.ProducerUnavailable(
            f"{name} is not installed in the interpreter running this "
            f"({sys.executable}), so {what}. Every file of that type would be "
            "recorded as empty, which reads like a folder of unreadable "
            "documents rather than a missing package. Install the project's "
            "requirements, or run this with the virtualenv's python."
        ) from exc


def require_readers(suffixes) -> None:
    """Check the parsers are present BEFORE doing work.

    Exists because rebuild() used to delete the index and discover the missing
    parser afterwards, so a run under the wrong interpreter cost the whole
    index and put nothing in its place.
    """
    wanted = {str(s).lower() for s in suffixes}
    if not wanted & set(_BACKENDS):
        return
    _module("docling.datamodel.document", "no document of any kind can be read")
    if ".pdf" in wanted:
        _module("docling.backend.docling_parse_backend", "no PDF can be read")


def _open_backend(path: Path, spec: tuple[str, str, str, bool]):
    module_path, class_name, format_name, _ = spec
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import InputDocument

    backend_cls = getattr(_module(module_path, f"no {path.suffix} can be read"), class_name)
    document = InputDocument(
        path_or_stream=path,
        format=getattr(InputFormat, format_name),
        backend=backend_cls,
        filename=path.name,
    )
    # Docling refuses a document it cannot open by never attaching a backend.
    # Reading that as an AttributeError and letting it escape as a crash is
    # how a damaged file would become a 500 rather than a recorded fact.
    backend = getattr(document, "_backend", None)
    if backend is None or not backend.is_valid():
        raise ValueError("the parser refused this file")
    return backend


def _pdf_text(backend) -> str:
    from docling_core.types.doc import BoundingBox

    pages = []
    for number in range(backend.page_count()):
        page = backend.load_page(number)
        size = page.get_size()
        pages.append(page.get_text_in_rect(
            BoundingBox(l=0, t=0, r=size.width, b=size.height)))
        if sum(len(p) for p in pages) > MAX_TEXT_PER_FILE:
            break
    return "\n".join(pages)


def extract(path: Path) -> Extraction:
    """Text out of one file, and an honest account of what happened.

    Raises ProducerUnavailable if the PARSER is missing, which is an
    environment fault and not this file's problem. Everything else is
    reported, never raised: one damaged document must not stop a folder.
    """
    suffix = path.suffix.lower()
    spec = _BACKENDS.get(suffix)
    if spec is None:
        return Extraction("", Outcome.UNSUPPORTED, "none",
                          f"nothing here reads {suffix or 'a file with no suffix'}")

    if suffix == ".txt":
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_PER_FILE]
        except OSError as exc:
            return Extraction("", Outcome.UNREADABLE, "plain", str(exc))
        return Extraction(text, Outcome.OK if text.strip() else Outcome.EMPTY, "plain")

    backend_name = f"docling:{spec[1]}"
    try:
        backend = _open_backend(path, spec)
    except ingest.ProducerUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - one file, never fatal
        return Extraction("", Outcome.UNREADABLE, backend_name,
                          f"{type(exc).__name__}: {exc}")

    try:
        if spec[3]:
            text = _pdf_text(backend)
        else:
            text = backend.convert().export_to_markdown()
    except Exception as exc:  # noqa: BLE001
        return Extraction("", Outcome.UNREADABLE, backend_name,
                          f"{type(exc).__name__}: {exc}")
    finally:
        try:
            backend.unload()
        except Exception:  # noqa: BLE001 - unloading must never be the failure
            pass

    text = (text or "")[:MAX_TEXT_PER_FILE]
    if not text.strip():
        # NOT a failure. A scan is a perfectly good file with no text layer,
        # and saying "unreadable" would send someone looking for damage that
        # is not there. It is what an OCR producer is for -- Step 10.
        return Extraction("", Outcome.EMPTY, backend_name,
                          "read cleanly; there is no text layer in this file")
    return Extraction(text, Outcome.OK, backend_name)
