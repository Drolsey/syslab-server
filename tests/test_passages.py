"""The chunk index and the keyword retriever, Step 4.4.

Two things are being tested and they are not the same thing.

The INDEX is a cache, and the tests about it are the ones app/search.py's have
always been: rebuildable from nothing, holding nothing that is not derived,
never the reason an upload fails, and never showing one tenant another's words.

The RETRIEVER is a seam, and the tests about it are about the shape of what
crosses it: ranks and not scores, positions that start at one, and a `chunk_id`
that can be looked up again. A citation nobody can resolve is a decoration.

Verified the way 4.3's were: by breaking the code and watching the test fail,
noted where the break found something the reading had not.
"""

from __future__ import annotations

import pytest

from app import config, context, ingest, intake, passages, producers, retrieve, search, tools

CALIBRATION = (
    "On-site calibration of four sensor arrays by Rania Haddad. "
    "Total due 18,640 AED. Payment falls due thirty days from invoice date. "
)
STAFFING = "Farah Nasser covers the northern region in October. "


@pytest.fixture(autouse=True)
def temp_dirs(tenant_storage):
    yield tenant_storage


@pytest.fixture
def documents(temp_dirs):
    """Two documents, each long enough to be SEVERAL passages.

    The multiplier is load-bearing and the first version of it was too small.
    CALIBRATION * 12 is about 1,400 characters, under the 1,792-character
    target, so every document came out as exactly one passage and the fixture
    was testing whole-document search through a second table. Caught by
    asserting on the passage count rather than on "it worked".
    """
    tools.write_pdf("meridian_invoice.pdf", title="Invoice QT-4417",
                    body=CALIBRATION * 40)
    tools.write_pdf("staffing_note.pdf", title="Staffing", body=STAFFING * 40)
    search.rebuild()
    return passages.rebuild()


# --------------------------------------------------------------------------
# the index holds what the artifact holds
# --------------------------------------------------------------------------

def test_a_passage_is_found_by_words_inside_it(documents):
    found = passages.search_passages("sensor calibration")

    assert found["count"] >= 1
    assert found["results"][0]["source"] == "meridian_invoice.pdf"
    assert "calibration" in found["results"][0]["text"].lower()


def test_a_document_becomes_several_passages_and_not_one_row(documents):
    """The point of the whole sub-step. If this is 1, nothing was chunked."""
    assert documents["passages"] > 2
    assert documents["documents_indexed"] == 2


def test_the_indexed_text_is_the_artifact_text_and_not_a_re_derivation(documents):
    """The artifact is the source of truth; the table is a cache of it.

    Checked passage by passage rather than by count, because "the same number
    of rows" is exactly what a table built from a second, disagreeing
    extraction would also report.
    """
    artifact = {c.chunk_id: c for c in producers.chunks_of("meridian_invoice.pdf")}
    assert artifact

    for chunk_id, chunk in artifact.items():
        row = passages.passage(chunk_id)
        assert row is not None, f"{chunk_id} is in the artifact and not in the index"
        assert row["text"] == chunk.text
        assert (row["start"], row["end"]) == (chunk.start, chunk.end)


def test_an_offset_in_the_index_still_resolves_against_the_extracted_text(documents):
    """4.3's property, asserted again from the other side of the index.

    The offsets survive a round trip through SQLite as integers, which is the
    sort of thing that is obviously true until a column is declared TEXT.
    """
    text = producers.text_of("meridian_invoice.pdf")

    for row in passages.search_passages("calibration", limit=20)["results"]:
        assert text[row["start"]:row["end"]] == row["text"]


# --------------------------------------------------------------------------
# the counts decision 5.6 rests on
# --------------------------------------------------------------------------

def test_matched_counts_every_passage_and_not_the_ones_returned(documents):
    """The load-bearing half of decision 5.6.

    A count derived from the returned rows equals the limit every time and
    agrees with itself every time, which is precisely the shape of a coverage
    number nobody can use. This one is asked of the index.
    """
    everything = passages.search_passages("calibration", limit=100)
    just_two = passages.search_passages("calibration", limit=2)

    assert everything["count"] > 2
    assert just_two["count"] == 2
    assert just_two["matched"] == everything["matched"] == everything["count"]


def test_a_query_that_matches_nothing_says_so_without_raising(documents):
    found = passages.search_passages("xylophone quokka")

    assert found["count"] == 0
    assert found["matched"] == 0
    assert found["matched_on"] is None
    assert found["passages_indexed"] > 0


def test_all_terms_is_preferred_and_any_term_is_the_fallback(documents):
    both = passages.search_passages("calibration Rania")
    assert both["matched_on"] == "all terms"

    # "calibration" and "October" are in different documents, so no single
    # passage holds both and the AND pass finds nothing.
    either = passages.search_passages("calibration October")
    assert either["matched_on"] == "any term"


def test_the_two_indexes_agree_about_what_a_query_means(documents):
    """They share app/search.terms(), and this is why it was made public.

    Two tokenizers would drift, and every comparison between document and
    passage retrieval -- which is the entire output of check_retrieval.py --
    would then be measuring the difference between two query parsers as much
    as between two indexes.

    THE FIRST VERSION OF THIS ASSERTED `search.terms is passages.search.terms`,
    which is the same module attribute read twice and cannot fail. Asked
    behaviourally instead: a real question, with the punctuation and stop words
    a model actually writes, has to reach the same document by both routes and
    be matched with the same precision.
    """
    question = "What is the on-site calibration, and who did it?"

    by_document = search.search(question)
    by_passage = passages.search_passages(question)

    assert by_document["count"] and by_passage["count"]
    assert by_document["results"][0]["name"] == by_passage["results"][0]["source"]
    assert by_document["matched_on"] == by_passage["matched_on"]


# --------------------------------------------------------------------------
# the cache stays a cache
# --------------------------------------------------------------------------

def test_the_index_rebuilds_from_nothing(documents):
    before = passages.status()

    connection = passages.connect()
    try:
        connection.execute("DELETE FROM chunks")
        connection.commit()
    finally:
        connection.close()
    assert passages.status()["passages"] == 0

    passages.rebuild()

    assert passages.status()["passages"] == before["passages"]


def test_indexing_the_same_document_twice_replaces_rather_than_duplicates(documents):
    before = passages.status()["passages"]

    passages.index_source("meridian_invoice.pdf")
    passages.index_source("meridian_invoice.pdf")

    assert passages.status()["passages"] == before


def test_a_deleted_document_cannot_answer_with_a_paragraph_of_itself(documents):
    """Worse than a stale document hit, which is only a filename.

    A stale PASSAGE comes back quoted, so a contract that was deleted goes on
    supplying text to answers. Swept on the read path for that reason.
    """
    (config.data_dir() / "meridian_invoice.pdf").unlink()

    found = passages.search_passages("calibration")

    assert found["count"] == 0
    assert "meridian_invoice.pdf" not in {r["source"] for r in found["results"]}


def test_a_document_with_no_passages_is_not_a_failure(temp_dirs):
    """A scan has no text layer, so it has no passages. Zero rows, no raise."""
    (temp_dirs / "blank.txt").write_text("   \n  \n", encoding="utf-8")
    ingest.ingest("blank.txt")

    result = passages.index_source("blank.txt")

    assert result["passages"] == 0
    assert "no passages" in result["reason"]


def test_a_file_type_nothing_chunks_is_declined_rather_than_indexed(temp_dirs):
    (temp_dirs / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    result = passages.index_file(temp_dirs / "photo.png")

    assert result["passages"] == 0
    assert "nothing chunks" in result["reason"]


def test_one_tenant_never_sees_another_tenants_passages(temp_dirs):
    tools.write_pdf("mine.pdf", title="Mine", body=CALIBRATION * 8)
    search.rebuild()
    passages.rebuild()
    assert passages.search_passages("calibration")["count"] > 0

    with context.use_tenant("othertenant"):
        config.ensure_data_dir()
        assert passages.search_passages("calibration")["count"] == 0


# --------------------------------------------------------------------------
# intake owns the order
# --------------------------------------------------------------------------

def test_an_arriving_file_reaches_both_indexes_in_one_call(temp_dirs):
    """The reason this went into intake and not into three call sites."""
    tools.write_pdf("arriving.pdf", title="Arriving", body=CALIBRATION * 10)

    outcome = intake.arrived(temp_dirs / "arriving.pdf")

    assert outcome["indexed"] is True
    assert outcome["passages"] > 0
    assert "passages_error" not in outcome
    assert passages.search_passages("calibration")["count"] > 0


def test_a_passage_index_failure_does_not_lose_the_document_index(temp_dirs, monkeypatch):
    """Neither index may fail a write that is already on disk."""
    def explode(path, connection=None):
        raise RuntimeError("the passage index is having a bad day")

    monkeypatch.setattr(passages, "index_file", explode)
    tools.write_pdf("arriving.pdf", title="Arriving", body=CALIBRATION * 4)

    outcome = intake.arrived(temp_dirs / "arriving.pdf")

    assert outcome["indexed"] is True
    assert "bad day" in outcome["passages_error"]
    assert search.search("calibration")["count"] > 0


# --------------------------------------------------------------------------
# the retriever seam
# --------------------------------------------------------------------------

def test_the_keyword_retriever_is_registered_under_the_name_it_reports(documents):
    assert retrieve.names() == ["keyword"]
    assert retrieve.registered()["keyword"] is passages.KEYWORD
    assert isinstance(passages.KEYWORD, retrieve.Retriever)


def test_it_returns_positions_starting_at_one_and_no_scores(documents):
    hits = passages.KEYWORD.search("calibration", 5)

    assert hits
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
    assert not hasattr(hits[0], "score")


def test_every_chunk_id_a_retriever_returns_can_be_looked_up_again(documents):
    """A citation nobody can resolve is a decoration.

    This is the property the retrieval gate folds passages back into documents
    with, so an unresolvable id there would show up as slightly worse retrieval
    rather than as the broken thing it is.
    """
    hits = passages.KEYWORD.search("calibration", 20)

    assert hits
    for hit in hits:
        found = passages.passage(hit.chunk_id)
        assert found is not None, f"{hit.chunk_id} came back and cannot be fetched"
        assert found["chunk_id"] == hit.chunk_id


def test_an_unknown_chunk_id_is_None_rather_than_an_error(documents):
    assert passages.passage("no_such_document.pdf#99") is None


def test_a_rank_of_zero_is_refused(documents):
    """1/(60 + rank) is quietly wrong for the best hit of every query if a
    retriever starts counting at zero, and nothing about the output looks
    wrong when it does."""
    with pytest.raises(ValueError) as caught:
        retrieve.Hit(chunk_id="a.pdf#0", rank=0)

    assert "1-based" in str(caught.value)


def test_an_unaskable_question_raises_and_an_unanswerable_one_does_not(documents):
    """Two different things. Returning [] for both would hide the first."""
    assert passages.KEYWORD.search("xylophone quokka", 5) == []

    with pytest.raises(retrieve.RetrieverError):
        passages.KEYWORD.search("!!! ???", 5)


def test_a_retriever_without_a_name_is_refused(documents):
    """`found_by` reports the name, so an unnamed retriever is a hit nobody
    can attribute."""
    class Anonymous:
        name = ""

        def search(self, query, limit):
            return []

    with pytest.raises(retrieve.RetrieverError):
        retrieve.register(Anonymous())
