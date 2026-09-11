"""The format table, and the distinction it keeps getting wrong.

Step 4.2. `app/parse.py`'s claim is that a new format is a ROW. What these
assert is the part of that claim which is not free: a row is only true if the
thing behind it is actually installed, and the pipeline has to be able to tell
"this package is missing" from "this document is damaged".

That distinction has now cost this project three times -- nineteen good PDFs
indexed as empty when pymupdf was absent, then the same shape again when 4.2
part one shipped .pptx and .md rows whose libraries were not in requirements.
The second one got through because the check was at IMPORT time and docling
raises at READ time. These are the tests that make it structural.
"""

from __future__ import annotations

import pytest

from app import ingest, parse, producers  # noqa: F401 - registers the text producer
from scripts import _fixtures


# --------------------------------------------------------------------------
# every row of the table is a fact, not a claim
# --------------------------------------------------------------------------

def a_sample(suffix: str, sentinel: str) -> bytes:
    """One small file of each kind the table claims to read."""
    if suffix == ".pdf":
        return _fixtures.build_pdf("Sample", [f"{sentinel} in the body"])
    if suffix in (".xlsx", ".xlsm"):
        return _fixtures.build_xlsx([[sentinel, 42]])
    if suffix == ".pptx":
        return _fixtures.build_pptx([sentinel, "a second line"])
    if suffix in (".html", ".htm"):
        return f"<html><body><p>{sentinel} here</p></body></html>".encode()
    if suffix == ".csv":
        return f"col_a,col_b\n{sentinel},7\n".encode()
    if suffix == ".md":
        return f"# Heading\n\n{sentinel} in a paragraph.\n".encode()
    if suffix == ".txt":
        return f"{sentinel} plain words.\n".encode()
    raise AssertionError(
        f"{suffix} is in parse.handles() and this test does not know how to "
        "make one. Add it here rather than narrowing the loop: a row nothing "
        "reads is how .pptx and .md shipped broken."
    )


DOCX = "tests/fixtures/corpus/formats/format_docx.docx"


@pytest.mark.parametrize("suffix", sorted(parse.handles()))
def test_every_claimed_format_is_read_and_its_words_arrive(suffix, tmp_path):
    """Asserting on the TEXT, not on the outcome.

    A backend that silently produced nothing would pass an outcome check --
    `.pptx` did, for a while, in the sense that nobody had ever looked.
    """
    sentinel = "ZARATEST" + suffix[1:].upper()
    path = tmp_path / f"sample{suffix}"

    if suffix == ".docx":
        from pathlib import Path
        path.write_bytes((Path(DOCX).resolve()).read_bytes())
        sentinel = "ZARAMANDA-DOCX-9028"
    else:
        path.write_bytes(a_sample(suffix, sentinel))

    found = parse.extract(path)

    assert found.outcome is parse.Outcome.OK, found.detail
    assert sentinel in found.text


def test_every_row_names_the_package_it_actually_needs():
    """The `needs` column, asserted against reality rather than against itself.

    docling-slim imports a format's reader at READ time, so importing the
    backend module proves nothing. This walks the table and imports what each
    row says it needs -- which is the check that would have failed on the day
    .pptx and .md were added without their libraries.
    """
    import importlib

    missing = []
    for suffix, spec in sorted(parse._BACKENDS.items()):
        if spec is parse.PLAIN:
            continue
        for module in (spec.module, spec.needs):
            if not module:
                continue
            try:
                importlib.import_module(module)
            except ImportError:
                missing.append(f"{suffix} needs {module}")
    assert not missing, (
        "These rows claim a format this interpreter cannot read: "
        + "; ".join(missing)
    )


def test_an_unknown_suffix_is_unsupported_rather_than_broken(tmp_path):
    path = tmp_path / "holiday.jpeg"
    path.write_bytes(b"pixels")

    found = parse.extract(path)

    assert found.outcome is parse.Outcome.UNSUPPORTED
    assert ".jpeg" in found.detail


# --------------------------------------------------------------------------
# a missing package is an environment fault, never a damaged document
# --------------------------------------------------------------------------

def test_a_reader_missing_at_READ_time_is_unavailable_not_unreadable(tmp_path, monkeypatch):
    """THE regression test for the bug 4.2 part one shipped.

    Written by reproducing it first: with `_call` removed, this returns
    Outcome.UNREADABLE and the assertion below fails -- which is exactly what
    every .pptx and every .md was getting, recorded against the document as
    "this file could not be read" while the file was perfectly fine.

    The difference is not cosmetic. UNREADABLE is written into the manifest
    against that one document, so installing the package afterwards fixes
    nothing until somebody works out which several hundred failed rows were
    lies. ProducerUnavailable is reported once, against nothing, and the
    document is simply retried.
    """
    path = tmp_path / "deck.pptx"
    path.write_bytes(_fixtures.build_pptx(["ZARATESTPPTX"]))

    real = parse._open_backend

    def import_error_from_inside_the_read(p, spec):
        backend = real(p, spec)

        class Refusing:
            def convert(self):
                raise ImportError(
                    "The 'python-pptx' package is required to process "
                    "PowerPoint files."
                )

            def unload(self):
                backend.unload()

        return Refusing()

    monkeypatch.setattr(parse, "_open_backend", import_error_from_inside_the_read)

    with pytest.raises(ingest.ProducerUnavailable) as caught:
        parse.extract(path)

    message = str(caught.value)
    assert "python-pptx" in message
    assert ".pptx" in message, "it has to say which format is affected, not just which file"


def test_that_unavailability_reaches_the_pipeline_as_unavailable(tmp_path, monkeypatch, tenant_storage):
    """And the pipeline writes no failure row for it.

    The half that matters operationally: `unavailable` is reported once per
    call and written against nothing, so a folder of good decks does not
    become a folder of rows saying every one of them is damaged.
    """
    (tenant_storage / "deck.pptx").write_bytes(_fixtures.build_pptx(["ZARATESTPPTX"]))

    def refuse(path):
        raise ingest.ProducerUnavailable("python-pptx is not installed")

    monkeypatch.setattr(parse, "extract", refuse)

    report = ingest.ingest("deck.pptx")

    assert "text" in report["unavailable"]
    assert report["failed"] == {}
    assert ingest.status("deck.pptx")["failed"] == [], "no row blames the document"


def test_require_readers_checks_the_deferred_package_too(monkeypatch):
    """Why `require_readers` grew the `needs` column.

    It exists so a rebuild does not delete the index and THEN discover the
    missing parser. Checking only the docling backend module let it pass for
    .pptx on an interpreter that could not read one, because that module
    imports fine without python-pptx.
    """
    real_import = parse.importlib.import_module

    def without_pptx(name, *args, **kwargs):
        if name == "pptx":
            raise ImportError("No module named 'pptx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(parse.importlib, "import_module", without_pptx)

    with pytest.raises(ingest.ProducerUnavailable):
        parse.require_readers([".pptx"])

    # And it stays quiet about formats nobody asked for.
    parse.require_readers([".txt"])
