"""Tests for the search index.

The index is a cache. Most of these tests are about it staying one: rebuildable
from scratch, holding nothing that is not derived, and never being the reason an
upload fails.
"""

from __future__ import annotations

import pytest

from app import config, search, tools


@pytest.fixture(autouse=True)
def temp_dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    index = tmp_path / "index"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "INDEX_DIR", index)
    monkeypatch.setattr(config, "INDEX_PATH", index / "documents.sqlite3")
    monkeypatch.setattr(search, "INDEX_PATH", index / "documents.sqlite3")
    yield data


@pytest.fixture
def documents(temp_dirs):
    tools.write_pdf("meridian_invoice.pdf", title="Invoice QT-4417",
                    body="On-site calibration of four sensor arrays by Rania Haddad. "
                         "Total due 18,640 AED.")
    tools.write_pdf("staffing_note.pdf", title="Staffing",
                    body="Farah Nasser covers the northern region in October.")
    tools.write_excel("q1_sales.xlsx", rows=[["North", 21400], ["East", 9300]],
                      headers=["Region", "Revenue"])
    return search.rebuild()


# --- finding things -------------------------------------------------------

def test_a_document_is_found_by_words_inside_it(documents):
    result = search.search("sensor calibration")
    assert result["count"] >= 1
    assert result["results"][0]["name"] == "meridian_invoice.pdf"
    assert "calibration" in result["results"][0]["snippet"].lower()


def test_spreadsheets_are_searchable_too(documents):
    result = search.search("East revenue")
    names = [r["name"] for r in result["results"]]
    assert "q1_sales.xlsx" in names


def test_the_result_says_which_tool_opens_each_hit(documents):
    """So the model never has to guess read_pdf versus read_excel."""
    by_name = {r["name"]: r["read_with"] for r in search.search("region OR calibration")["results"]}
    assert by_name.get("q1_sales.xlsx") == "read_excel"
    assert by_name.get("meridian_invoice.pdf") == "read_pdf"


def test_all_terms_is_preferred_and_any_term_is_the_fallback(documents):
    both = search.search("calibration Rania")
    assert both["matched_on"] == "all terms"
    # "calibration" and "October" appear in different documents, so AND finds none
    either = search.search("calibration October")
    assert either["matched_on"] == "any term"
    assert either["count"] >= 2


def test_nothing_found_explains_itself(documents):
    result = search.search("aardvark zeppelin")
    assert result["count"] == 0
    assert "list_files" in result["note"]


def test_a_query_with_fts_syntax_in_it_does_not_blow_up(documents):
    """The model writes these strings, and English contains quotes and brackets."""
    for awkward in ['the "invoice" (calibration)', "Rania's report -- urgent", "NEAR OR AND"]:
        search.search(awkward)


def test_an_empty_query_is_refused(documents):
    with pytest.raises(search.SearchError):
        search.search("   ")


# --- the index as a cache -------------------------------------------------

def test_rebuild_reconstructs_everything_from_the_files(documents):
    """If it can always be rebuilt, nothing irreplaceable can accumulate in it."""
    before = search.status()["documents"]
    again = search.rebuild()
    assert again["indexed"] == before
    assert search.search("sensor calibration")["count"] >= 1


def test_deleting_the_index_file_loses_nothing(documents, temp_dirs):
    config.INDEX_PATH.unlink()
    assert search.status()["documents"] == 0
    search.rebuild()
    assert search.search("sensor calibration")["count"] >= 1


def test_the_extracted_text_is_stored_not_only_the_index(documents):
    """This is what makes moving to PostgreSQL an insert rather than a re-parse."""
    connection = search.connect()
    try:
        row = connection.execute(
            "SELECT text FROM documents WHERE name = 'meridian_invoice.pdf'"
        ).fetchone()
    finally:
        connection.close()
    assert "Rania Haddad" in row["text"]


def test_the_index_lives_outside_the_data_folder(temp_dirs):
    """So that deleting it is obviously safe, and it is never mistaken for user data."""
    assert config.DATA_DIR not in config.INDEX_PATH.parents


# --- keeping up with the folder -------------------------------------------

def test_a_new_file_is_reported_as_not_yet_indexed(documents):
    tools.write_pdf("late_arrival.pdf", title="Late", body="A brand new document.")
    assert "late_arrival.pdf" in search.status()["not_yet_indexed"]


def test_indexing_one_file_makes_it_findable_without_a_full_rebuild(documents, temp_dirs):
    tools.write_pdf("late_arrival.pdf", title="Late", body="Contains the word pomegranate.")
    assert search.search("pomegranate")["count"] == 0
    search.index_file(temp_dirs / "late_arrival.pdf")
    assert search.search("pomegranate")["count"] == 1
    assert search.status()["not_yet_indexed"] == []


def test_reindexing_a_changed_file_replaces_it_rather_than_duplicating(documents, temp_dirs):
    tools.write_pdf("changing.pdf", title="V1", body="The word is alpha.")
    search.index_file(temp_dirs / "changing.pdf")
    tools.write_pdf("changing.pdf", title="V2", body="The word is beta.")
    search.index_file(temp_dirs / "changing.pdf")
    assert search.search("alpha")["count"] == 0
    assert search.search("beta")["count"] == 1


def test_a_file_with_no_text_is_skipped_not_failed(temp_dirs):
    (temp_dirs / "scanned.pdf").write_bytes(b"%PDF-1.4 no text layer here")
    result = search.rebuild()
    assert "scanned.pdf" in result["skipped"]


def test_an_unsupported_file_type_is_ignored(temp_dirs):
    (temp_dirs / "notes.txt").write_text("this is not searchable")
    assert search.index_file(temp_dirs / "notes.txt")["indexed"] is False
