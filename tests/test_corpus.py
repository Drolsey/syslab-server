"""Step 4.1: the corpus and the golden set are what they claim to be.

WHY THIS EXISTS
    Everything measured from 4.2 onward is measured against these files. A
    corpus that changes quietly makes every later number meaningless while
    every gate still passes -- the corpus is the ruler, and a ruler nobody
    checks is a ruler that drifts.

    So: hashes, not "looks fine". Ground truth re-derived, not trusted.

WHAT IS NOT TESTED HERE
    Whether retrieval is any GOOD. That is scripts/check_retrieval.py, which
    needs to index 52 PDFs and is far too slow for the suite.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

CORPUS = Path(__file__).resolve().parent / "fixtures" / "corpus"


@pytest.fixture(scope="module")
def manifest():
    return json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden():
    return json.loads((CORPUS / "golden.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# the corpus
# --------------------------------------------------------------------------

def test_every_contract_is_present_and_unmodified(manifest):
    """sha256 per contract. The ruler does not get to drift silently."""
    for entry in manifest["contracts"]:
        pdf = CORPUS / "contracts" / entry["name"]
        assert pdf.is_file(), f"{entry['name']} is missing"
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"{entry['name']} has changed since it was committed"


def test_nothing_extra_crept_into_the_corpus(manifest):
    on_disk = {p.name for p in (CORPUS / "contracts").glob("*")}
    recorded = {e["name"] for e in manifest["contracts"]}
    assert on_disk == recorded


def test_the_attribution_travels_with_the_files():
    """CC BY 4.0 requires it, and requires it beside the corpus, not only in
    docs/licences.md, because a copy of this folder is a copy of the data."""
    attribution = (CORPUS / "ATTRIBUTION.md").read_text(encoding="utf-8")
    for required in ("CC BY 4.0", "Atticus Project", "zenodo.org/records/4595826"):
        assert required in attribution


def test_filenames_carry_no_content(manifest):
    """A filename that names its parties would let a party-name query match the
    NAME rather than the text, and app/search.py indexes both. That would
    measure the fixture's naming and call it retrieval."""
    for entry in manifest["contracts"]:
        assert re.fullmatch(r"contract_\d{2}\.pdf", entry["name"])


# --------------------------------------------------------------------------
# the golden set
# --------------------------------------------------------------------------

def test_every_relevant_document_exists(golden, manifest):
    known = {e["name"] for e in manifest["contracts"]}
    for query in golden["queries"]:
        assert query["relevant"], f"{query['id']} has no relevant document"
        unknown = set(query["relevant"]) - known
        assert not unknown, f"{query['id']} points at {unknown}, which is not in the corpus"


def test_the_single_answer_queries_really_are_unique(golden, manifest):
    """exact and clause queries claim their string occurs in exactly ONE
    contract. Re-derived here from the reference text rather than trusted,
    because that claim is the entire ground truth for 34 of the 42 queries."""
    texts = {}
    for entry in manifest["contracts"]:
        raw = (CORPUS / "reference_text" / entry["reference_text"]).read_text(
            encoding="utf-8", errors="replace")
        texts[entry["name"]] = re.sub(r"\s+", " ", raw).lower()

    for query in golden["queries"]:
        if query["kind"] not in ("exact", "clause"):
            continue
        needle = re.sub(r"\s+", " ", query["query"]).lower().strip()
        holders = [name for name, body in texts.items() if needle in body]
        assert holders == query["relevant"], (
            f"{query['id']} claims {query['relevant']} but the string occurs in {holders}")


def test_paraphrase_queries_share_little_vocabulary_with_their_targets(golden):
    """They exist to FAIL against keyword search. One that happens to quote the
    contract would pass today and quietly stop being evidence for Step 5."""
    for query in golden["queries"]:
        if query["kind"] != "paraphrase":
            continue
        words = [w for w in re.findall(r"[a-z]{4,}", query["query"].lower())]
        assert len(words) >= 4, f"{query['id']} is too short to be a paraphrase"


def test_the_golden_set_is_not_one_the_system_already_passes(golden):
    """A golden set with no failures cannot show an improvement, which is the
    only reason to have one before the work rather than after it."""
    kinds = {q["kind"] for q in golden["queries"]}
    assert "paraphrase" in kinds
    hard = [q for q in golden["queries"] if q["kind"] == "paraphrase"]
    assert len(hard) >= 5, "too few queries that today's retrieval should fail"


def test_aggregate_answers_are_counted_not_asserted(golden):
    for item in golden["aggregates"]:
        assert "true_answer" in item and item["true_answer"]
        assert "counted from CUAD" in item["source"]


# --------------------------------------------------------------------------
# the format pack
# --------------------------------------------------------------------------

def test_the_format_pack_carries_findable_sentinels():
    pack = CORPUS / "formats"
    sentinels = json.loads((pack / "sentinels.json").read_text(encoding="utf-8"))
    for name, sentinel in sentinels["sentinels"].items():
        assert (pack / name).is_file(), f"{name} is missing"
        if name.endswith((".html", ".csv")):
            assert sentinel in (pack / name).read_text(encoding="utf-8")


def test_the_scanned_pdf_still_has_no_text_layer():
    """It is the OCR case. The day this starts extracting text is the day it
    stops being the OCR case, and something should say so."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(CORPUS / "formats" / "format_scanned.pdf")
    extracted = "".join(page.get_text() for page in doc).strip()
    doc.close()
    assert extracted == "", "the scan grew a text layer; it no longer tests OCR"


def test_the_corrupt_pdf_is_still_genuinely_unreadable():
    """A missing xref alone is RECOVERED by pymupdf and opens with zero pages,
    which reads as an empty document rather than a broken one -- the exact
    confusion this fixture exists to catch."""
    fitz = pytest.importorskip("fitz")
    with pytest.raises(Exception):
        fitz.open(CORPUS / "formats" / "format_corrupt.pdf").close()


# --------------------------------------------------------------------------
# the baseline
# --------------------------------------------------------------------------

def test_the_baseline_measures_the_same_queries_it_was_written_against(golden):
    """If a query is added or removed, the committed baseline stops being
    comparable and has to be re-taken. Nothing else would say so."""
    baseline = json.loads((CORPUS / "baseline.json").read_text(encoding="utf-8"))
    assert {row["id"] for row in baseline["queries"]} == {q["id"] for q in golden["queries"]}


def test_the_baseline_records_what_it_measured():
    baseline = json.loads((CORPUS / "baseline.json").read_text(encoding="utf-8"))
    assert "app/search.py" in baseline["measured"]
    assert baseline["summary"]["overall"]["queries"] > 0
    # Not a target. A record. If retrieval changes, this number should move,
    # and 4.4's gate is where the movement gets explained.
    assert 0.0 < baseline["summary"]["overall"]["mrr"] <= 1.0
