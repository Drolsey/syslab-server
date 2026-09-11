"""The chunk producer, Step 4.3.

`docs/plans/step-04-retrieval-plane.md`: "Determinism is the property worth
gating -- a chunker that splits differently on Tuesday invalidates every
citation ever issued."

So the tests here are mostly one property said several ways:

  * `artifact[chunk.start:chunk.end] == chunk.text`, exactly;
  * the same text splits the same way, every time;
  * deleting derived/ and rebuilding produces byte-identical chunks.json;
  * a version bump -- of the chunker OR of the text producer above it --
    re-chunks, and a citation deliberately does not survive it.

Every property here was verified by breaking the code and watching the test
fail: eight deliberate breaks, eight failures, all restored. That pass is what
found the dead row in `chunks.SEPARATORS` -- an empty string at the end of the
table, copied from the shape the technique is usually written in, which the
splitter skipped and which therefore protected nothing. Removing it changed no
behaviour at all, which is the point: it was a comment pretending to be code,
sitting on the line somebody would edit next.
"""

from __future__ import annotations

import json
import shutil

import pytest

from app import chunks, config, ingest, producers

SENTENCE = "Payment falls due thirty days from invoice date. "

# Spelled as numbers so that the newline test cannot quietly pass because an
# escape was lost between here and the assertion. \r\n written in a string
# literal is exactly the sort of thing that survives a bad edit looking fine.
CARRIAGE_RETURN = bytes([13])
LINE_FEED = bytes([10])
BLANK_LINE = LINE_FEED.decode()


def prose(paragraphs: int = 8, sentences: int = 40) -> str:
    """Text long enough to be several chunks, in ordinary document shape."""
    return "\n\n".join(SENTENCE * sentences for _ in range(paragraphs))


def a_document(folder, name: str = "contract.txt", **kwargs) -> tuple[str, str]:
    text = prose(**kwargs)
    (folder / name).write_text(text, encoding="utf-8")
    return name, text


def artifact(name: str) -> str:
    """The raw bytes of chunks.json, which is what "byte-identical" is about."""
    written = config.derived_dir() / "chunks" / name / producers.CHUNKS_ARTIFACT
    return written.read_bytes().decode("utf-8")


# --------------------------------------------------------------------------
# the offsets, which are the whole citation
# --------------------------------------------------------------------------

def test_every_offset_resolves_back_to_the_text_it_came_from():
    """The gate, stated directly. `start` and `end` are what a customer checks.

    Verified by breaking it: with the trim calling `.strip()` on the chunk
    text and leaving the offsets where they were, every passage preceded by a
    blank line is reported at an offset a few characters before where it
    actually begins. A citation that looks right, reads right, and points at
    the wrong place is the failure this whole sub-step is built against.
    """
    text = prose(paragraphs=6, sentences=30)

    made = chunks.split(text, "contract.txt")

    assert made
    for chunk in made:
        assert text[chunk.start:chunk.end] == chunk.text


def test_a_passage_is_never_padded_or_tidied_on_its_way_out():
    """No normalising, no re-wrapping, no case folding. It is the slice."""
    text = "First line.\n\tIndented  with   odd spacing.\n\nAnd a second paragraph."

    only = chunks.split(text, "notes.txt")

    assert len(only) == 1
    assert only[0].text == text
    assert text[only[0].start:only[0].end] == only[0].text


def test_leading_and_trailing_whitespace_moves_the_offset_not_the_string():
    text = "\n\n\n   Payment terms are thirty days.   \n\n\n"

    only = chunks.split(text, "terms.txt")[0]

    assert only.text == "Payment terms are thirty days."
    assert only.start == text.index("Payment")
    assert text[only.start:only.end] == only.text


def test_the_ordinals_run_from_zero_with_no_holes():
    """A hole is a chunk_id in an API response that resolves to nothing.

    Verified by breaking it: numbering before the whitespace-only spans are
    dropped gives a document with a long run of blank lines the ordinals #0,
    #1, #3 -- and #2 is a passage a caller can ask for and never get.

    THE FIRST VERSION OF THIS TEST WAS THE THING THAT WAS WRONG. It used a run
    of 400 newlines, well under the target size, so the blank lines were always
    merged in with the prose either side and no span was ever empty for the
    trim to drop. It asserted on a case it never built, and passed against the
    broken code.
    """
    # Long enough to BE a span on its own: a shorter run of blank lines gets
    # merged in with the prose either side and never becomes a chunk that the
    # trim can empty. The first version of this test used 400 newlines, which
    # is well under the target size, so it asserted on a case it never built.
    text = (BLANK_LINE * 4000).join(SENTENCE * 40 for _ in range(4))

    made = chunks.split(text, "gappy.txt")

    assert [c.ordinal for c in made] == list(range(len(made)))
    assert [c.chunk_id for c in made] == [f"gappy.txt#{n}" for n in range(len(made))]


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------

def test_the_same_text_splits_the_same_way_every_time():
    text = prose(paragraphs=9, sentences=37)

    first = chunks.split(text, "contract.txt")
    second = chunks.split(text, "contract.txt")

    assert first == second
    assert chunks.dumps("contract.txt", len(text), first) == \
        chunks.dumps("contract.txt", len(text), second)


def test_the_artifact_on_disk_is_written_with_one_kind_of_newline(tenant_storage):
    """Byte-identical has to mean between machines, not only between runs.

    Reads the BYTES of the file the producer wrote, not the string it handed
    to write_text. Python's text mode rewrites every newline on the way out
    under Windows, so without newline="\n" the same document, the same text
    and the same chunker produce a different chunks.json here and on the
    Ubuntu box -- and the gate would pass on both while they disagreed.
    """
    name, _ = a_document(tenant_storage)
    ingest.ingest(name)

    raw = (config.derived_dir() / "chunks" / name / producers.CHUNKS_ARTIFACT).read_bytes()

    assert CARRIAGE_RETURN not in raw
    assert raw.endswith(LINE_FEED)


def test_nothing_in_a_chunk_depends_on_where_it_is_being_read():
    """No hash(), no clock, no locale, no network. Said as an assertion.

    A weaker version of this test read the module source for the banned names.
    This runs the split under a different hash seed's worth of dict churn and
    a different working directory instead, because what matters is the answer,
    not the spelling.
    """
    text = prose(paragraphs=5, sentences=44)
    expected = chunks.split(text, "contract.txt")

    for _ in range(5):
        # Enough allocation and dict activity between runs to move any
        # identity-derived ordering, if there were one.
        noise = {str(n): object() for n in range(500)}
        assert chunks.split(text, "contract.txt") == expected
        del noise


# --------------------------------------------------------------------------
# the shape of the split
# --------------------------------------------------------------------------

def test_no_passage_is_larger_than_the_budget_it_was_sized_for():
    text = prose(paragraphs=12, sentences=60)

    for chunk in chunks.split(text, "contract.txt"):
        assert chunk.tokens <= chunks.TARGET_TOKENS
        assert chunk.tokens == chunks.tokens_in(chunk.text)


def test_an_unbroken_run_is_cut_rather_than_becoming_one_giant_passage():
    """A base64 attachment in an extracted document is a real thing to find.

    Without the empty separator at the end of the table this is one chunk of
    40,000 characters, which spends the entire retrieval budget on one hit.
    """
    text = "y" * 40_000

    made = chunks.split(text, "blob.txt")

    assert len(made) > 1
    assert all(c.tokens <= chunks.TARGET_TOKENS for c in made)
    assert "".join(c.text for c in made) == text


def test_consecutive_passages_overlap_so_a_sentence_is_not_lost_at_a_seam():
    text = prose(paragraphs=8, sentences=40)

    made = chunks.split(text, "contract.txt")

    assert len(made) > 2
    overlapping = sum(1 for a, b in zip(made, made[1:]) if b.start < a.end)
    assert overlapping == len(made) - 1


def test_overlap_carries_whole_sentences_and_not_half_of_one():
    """Backing up a fixed number of CHARACTERS would reintroduce, at the start
    of the next passage, exactly the mid-sentence cut that overlap exists to
    prevent at the end of this one."""
    text = prose(paragraphs=6, sentences=40)

    made = chunks.split(text, "contract.txt")

    for chunk in made[1:]:
        assert chunk.text.startswith("Payment falls due")


def test_text_with_nothing_in_it_makes_no_passages_at_all():
    assert chunks.split("", "empty.txt") == []
    assert chunks.split("   \n\n\t  \n ", "blank.txt") == []


def test_a_short_document_is_one_passage_and_keeps_its_offsets():
    text = "A single short clause."

    made = chunks.split(text, "short.txt")

    assert len(made) == 1
    assert (made[0].start, made[0].end) == (0, len(text))


# --------------------------------------------------------------------------
# the artifact
# --------------------------------------------------------------------------

def test_the_artifact_round_trips_without_losing_a_character():
    text = prose(paragraphs=4, sentences=30)
    made = chunks.split(text, "contract.txt")

    assert chunks.loads(chunks.dumps("contract.txt", len(text), made)) == made


def test_the_artifact_records_how_long_the_text_it_measured_was():
    """The one cheap way to notice offsets pointing into a re-extraction."""
    text = prose(paragraphs=3, sentences=20)

    payload = json.loads(chunks.dumps("contract.txt", len(text), chunks.split(text, "contract.txt")))

    assert payload["text_chars"] == len(text)
    assert payload["target_tokens"] == chunks.TARGET_TOKENS
    assert payload["overlap_tokens"] == chunks.OVERLAP_TOKENS


def test_an_artifact_from_a_shape_this_reader_does_not_know_is_refused():
    payload = json.dumps({"format": chunks.FORMAT + 1, "source": "a.txt", "chunks": []})

    with pytest.raises(ValueError) as caught:
        chunks.loads(payload)

    assert "rebuild" in str(caught.value)


def test_something_that_is_not_an_artifact_at_all_is_refused():
    with pytest.raises(ValueError):
        chunks.loads("not json, just words")


# --------------------------------------------------------------------------
# the producer, in the pipeline
# --------------------------------------------------------------------------

def test_chunks_are_produced_and_resolve_against_the_text_artifact(tenant_storage):
    """End to end, and the assertion is the citation property again.

    Against `producers.text_of` rather than against the file's own bytes,
    deliberately: the offsets belong to the EXTRACTED text, and for a .pdf
    those are not the same thing at all.
    """
    name, _ = a_document(tenant_storage)

    report = ingest.ingest(name)

    assert sorted(report["ran"]) == ["chunks", "text"]
    assert not report["failed"] and not report["blocked"]

    text = producers.text_of(name)
    made = producers.chunks_of(name)
    assert made
    for chunk in made:
        assert text[chunk.start:chunk.end] == chunk.text


def test_the_chunker_runs_after_the_extractor_it_reads_from(tenant_storage):
    """The reason `depends_on` exists, and it is not a hypothetical.

    `producers_for` sorted by name, and "chunks" sorts before "text". Without
    the declaration the chunk producer runs first on a file's first ingest,
    finds no text artifact, and records a skip -- so a freshly uploaded
    document has no passages until somebody ingests it a second time. Taking
    the declaration away fails this and two more, which is where that sentence
    comes from.
    """
    a_document(tenant_storage)

    order = [p.name for p in ingest.producers_for(".txt")]

    assert order.index("text") < order.index("chunks")


def test_a_document_with_no_text_layer_has_no_passages_and_that_is_not_a_failure(tenant_storage):
    """A scan. The text producer skips it, and so must this one."""
    (tenant_storage / "scan.txt").write_text("   \n  \n", encoding="utf-8")

    report = ingest.ingest("scan.txt")

    assert not report["failed"]
    rows = ingest.status("scan.txt")["producers"]
    assert rows["text"]["status"] == ingest.SKIPPED
    assert rows["chunks"]["status"] == ingest.SKIPPED
    assert "no extracted text" in rows["chunks"]["detail"]
    assert producers.chunks_of("scan.txt") == []


def test_a_second_ingest_changes_nothing_and_says_so(tenant_storage):
    name, _ = a_document(tenant_storage)
    ingest.ingest(name)
    before = artifact(name)

    report = ingest.ingest(name)

    assert sorted(report["current"]) == ["chunks", "text"]
    assert report["ran"] == []
    assert artifact(name) == before


# --------------------------------------------------------------------------
# rebuilding, which is what the gate is really about
# --------------------------------------------------------------------------

def test_deleting_derived_and_rebuilding_gives_byte_identical_chunks(tenant_storage):
    """4.3's gate, in the suite as well as in the gate script.

    Byte-identical rather than equal-when-parsed. A JSON file that holds the
    same passages in a different order, or with the same numbers spelled
    differently, is a file whose diff nobody can read -- and a chunker that
    reorders is one step from a chunker that renumbers.
    """
    name, _ = a_document(tenant_storage)
    ingest.ingest(name)
    before = artifact(name)

    shutil.rmtree(config.derived_dir())
    summary = ingest.rebuild()

    assert summary["failed"] == 0
    assert artifact(name) == before


def test_re_extracting_the_text_re_chunks_the_document(tenant_storage, monkeypatch):
    """The hole the manifest cannot see for itself.

    A row records the source's size, its mtime and its OWN producer version.
    Nothing in it says which version of the text artifact the chunks were cut
    from. Without the staleness propagated along `depends_on`, bumping the text
    producer re-extracts every document and leaves every chunk exactly where it
    was: offsets into a file that has just been rewritten underneath them, and
    the document reported as ready.
    """
    name, _ = a_document(tenant_storage)
    ingest.ingest(name)

    bumped = ingest.Producer(
        name=producers.TEXT.name,
        version=producers.TEXT.version + 1,
        handles=producers.TEXT.handles,
        slow=producers.TEXT.slow,
        run=producers.TEXT.run,
    )
    monkeypatch.setitem(ingest._PRODUCERS, bumped.name, bumped)

    assert ingest.needs(name) == ["text", "chunks"]
    assert ingest.status(name)["ready"] is False

    report = ingest.ingest(name)
    assert sorted(report["ran"]) == ["chunks", "text"]


def test_bumping_the_chunker_alone_re_chunks_and_leaves_the_text_alone(tenant_storage, monkeypatch):
    name, _ = a_document(tenant_storage)
    ingest.ingest(name)

    bumped = ingest.Producer(
        name=producers.CHUNKS.name,
        version=producers.CHUNKS.version + 1,
        handles=producers.CHUNKS.handles,
        slow=producers.CHUNKS.slow,
        depends_on=producers.CHUNKS.depends_on,
        run=producers.CHUNKS.run,
    )
    monkeypatch.setitem(ingest._PRODUCERS, bumped.name, bumped)

    report = ingest.ingest(name)

    assert report["ran"] == ["chunks"]
    assert report["current"] == ["text"]


# --------------------------------------------------------------------------
# what happens when the thing above fails
# --------------------------------------------------------------------------

def test_a_document_the_extractor_could_not_read_does_not_get_chunks_blamed_on_it(tenant_storage):
    """One fault, one report. The chunker is not broken; the file is.

    Verified by breaking it: without the hold-back a damaged document gets TWO
    failed rows -- the real one from the extractor, and a second from the
    chunker complaining it cannot find a text artifact. The second is noise
    that sends whoever reads the manifest looking in the wrong module, and it
    is the class of fault 4.2 spent a sub-step on: one problem wearing the
    costume of several.
    """
    saved = dict(ingest._PRODUCERS)

    def refuse(source, out_dir):
        raise ValueError("this file is damaged")

    ingest._PRODUCERS[producers.TEXT.name] = ingest.Producer(
        name=producers.TEXT.name,
        version=producers.TEXT.version,
        handles=producers.TEXT.handles,
        slow=False,
        run=refuse,
    )
    try:
        name, _ = a_document(tenant_storage)

        report = ingest.ingest(name)

        assert "text" in report["failed"]
        assert "chunks" in report["blocked"]
        assert "text" in report["blocked"]["chunks"]
        assert report["failed"].keys() == {"text"}

        state = ingest.status(name)
        assert state["ready"] is False
        assert state["failed"] == ["text"]
        assert state["missing"] == ["chunks"]
        assert producers.chunks_of(name) == []
    finally:
        ingest._PRODUCERS.clear()
        ingest._PRODUCERS.update(saved)


def test_when_the_extractor_can_run_again_the_chunks_appear_with_no_prompting(tenant_storage):
    saved = dict(ingest._PRODUCERS)

    def refuse(source, out_dir):
        raise ValueError("this file is damaged")

    broken = ingest.Producer(
        name=producers.TEXT.name, version=producers.TEXT.version,
        handles=producers.TEXT.handles, slow=False, run=refuse,
    )
    ingest._PRODUCERS[broken.name] = broken
    try:
        name, _ = a_document(tenant_storage)
        ingest.ingest(name)
        ingest._PRODUCERS[producers.TEXT.name] = producers.TEXT

        report = ingest.ingest(name)

        assert sorted(report["ran"]) == ["chunks", "text"]
        assert ingest.status(name)["ready"] is True
        assert producers.chunks_of(name)
    finally:
        ingest._PRODUCERS.clear()
        ingest._PRODUCERS.update(saved)


def test_a_deferred_producer_holds_back_what_reads_it(tenant_storage):
    """only_fast leaves a slow producer stale; nothing downstream may run on
    an artifact that is about to be replaced."""
    saved = dict(ingest._PRODUCERS)
    ingest._PRODUCERS[producers.TEXT.name] = ingest.Producer(
        name=producers.TEXT.name,
        version=producers.TEXT.version,
        handles=producers.TEXT.handles,
        slow=True,
        run=producers.TEXT.run,
    )
    try:
        name, _ = a_document(tenant_storage)

        report = ingest.ingest(name, only_fast=True)

        assert report["deferred"] == ["text"]
        assert "chunks" in report["blocked"]
        assert report["ran"] == []
    finally:
        ingest._PRODUCERS.clear()
        ingest._PRODUCERS.update(saved)


# --------------------------------------------------------------------------
# the declaration itself
# --------------------------------------------------------------------------

def test_two_producers_that_read_each_other_are_refused_rather_than_recursed(tenant_storage):
    """A RecursionError on the first upload after somebody wires a loop is a
    stack trace nobody can read. This is a sentence naming both of them."""
    saved = dict(ingest._PRODUCERS)
    ingest._PRODUCERS.clear()

    def run(source, out_dir):
        return ingest.Result.nothing("nothing")

    for first, second in (("alpha", "beta"), ("beta", "alpha")):
        ingest.register(ingest.Producer(
            name=first, version=1, handles=frozenset({".txt"}), slow=False,
            depends_on=frozenset({second}), run=run,
        ))
    try:
        with pytest.raises(ingest.IngestError) as caught:
            ingest.producers_for(".txt")

        assert "alpha" in str(caught.value) and "beta" in str(caught.value)
    finally:
        ingest._PRODUCERS.clear()
        ingest._PRODUCERS.update(saved)


def test_a_producer_cannot_depend_on_itself():
    with pytest.raises(ValueError) as caught:
        ingest.Producer(
            name="ouroboros", version=1, handles=frozenset({".txt"}), slow=False,
            depends_on=frozenset({"ouroboros"}), run=lambda s, o: None,
        )

    assert "itself" in str(caught.value)


def test_producers_with_no_dependencies_still_come_out_alphabetically():
    """The order was alphabetical before 4.3 and stays that way among equals,
    so that adding the field changed nothing for anything that does not use
    it."""
    saved = dict(ingest._PRODUCERS)
    ingest._PRODUCERS.clear()

    def run(source, out_dir):
        return ingest.Result.nothing("nothing")

    for name in ("zulu", "alpha", "mike"):
        ingest.register(ingest.Producer(
            name=name, version=1, handles=frozenset({".txt"}), slow=False, run=run,
        ))
    try:
        assert [p.name for p in ingest.producers_for(".txt")] == ["alpha", "mike", "zulu"]
    finally:
        ingest._PRODUCERS.clear()
        ingest._PRODUCERS.update(saved)
