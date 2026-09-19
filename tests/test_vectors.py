"""Step 5.2/5.3: the embeddings producer, its table, and the Vector retriever.

The freshness design under test here is `docs/plans/step-05-embeddings.md`
§5.4's, not re-argued here: two mechanisms, matched to two different
things that must invalidate an embedding. `depends_on` (proven already, in
test_chunks.py) decides whether the producer runs at all; what is new and
tested here is the per-chunk content hash (decides WHICH chunks within a
run need a new vector) and the config fingerprint (decides whether an
UNCHANGED chunk's stored vector is still valid for the CURRENT embed role).

No live model anywhere in this file -- app.embed.embed and
app.models.model_for are both monkeypatched, the same shape
tests/test_agent.py already mocks app.llm.chat and
tests/test_passages.py already mocks passages.index_file.
"""

from __future__ import annotations

import pytest

from app import config, ingest, producers, retrieve, vectors

FAKE_DIMENSION = 4


@pytest.fixture(autouse=True)
def registered_only_here(monkeypatch):
    """app/vectors.py deliberately does NOT self-register EMBEDDINGS at
    import, unlike app/producers.py's TEXT/CHUNKS -- see the long comment
    at that line for why (importing this module used to make every
    document in the whole suite report not-ready, the moment any test file
    imported it). Registering it here, scoped to this file's tests only via
    monkeypatch, is what "the caller's decision" means in practice.
    """
    monkeypatch.setitem(ingest._PRODUCERS, vectors.EMBEDDINGS.name, vectors.EMBEDDINGS)


def a_document(folder, name: str = "contract.txt", body: str | None = None) -> str:
    text = body if body is not None else ("Payment falls due thirty days from invoice date. " * 80)
    (folder / name).write_text(text, encoding="utf-8")
    return name


def fake_config(model: str = "fake-embed-model") -> dict:
    return {"model": model, "provider": "fake"}


def install_role(monkeypatch, config_dict: dict) -> None:
    """Makes model_for("embed") answer, and re-registers EMBEDDINGS with the
    version that config would derive -- mirroring test_chunks.py's own
    pattern for testing a version bump (monkeypatch.setitem(ingest._PRODUCERS, ...)),
    since EMBEDDINGS.version is otherwise fixed at real import time, before
    any test has a chance to fill the role.
    """
    monkeypatch.setattr(vectors.models, "model_for",
                         lambda role: config_dict if role == "embed" else {})
    bumped = ingest.Producer(
        name=vectors.EMBEDDINGS.name,
        version=vectors._embed_producer_version(),
        handles=vectors.EMBEDDINGS.handles,
        slow=vectors.EMBEDDINGS.slow,
        depends_on=vectors.EMBEDDINGS.depends_on,
        run=vectors.EMBEDDINGS.run,
    )
    monkeypatch.setitem(ingest._PRODUCERS, bumped.name, bumped)


def install_fake_embed(monkeypatch) -> list[list[str]]:
    """A deterministic, content-derived fake vector: the same text always
    gives the same vector and different text gives a different one, so
    "was this chunk actually re-embedded" is observable through what was
    asked for, not just through what came back.
    """
    calls: list[list[str]] = []

    def _embed(texts, model=None, timeout=None, truncate_prompt_tokens=None):
        calls.append(list(texts))
        return [[float((hash(t) >> (8 * i)) % 97) for i in range(FAKE_DIMENSION)] for t in texts]

    monkeypatch.setattr(vectors.embed, "embed", _embed)
    return calls


def stored_rows(name: str) -> dict[str, tuple[str, str]]:
    """chunk_id -> (content_hash, embed_config) read straight from the table."""
    connection = vectors.connect()
    try:
        return {
            row["chunk_id"]: (row["content_hash"], row["embed_config"])
            for row in connection.execute(
                "SELECT chunk_id, content_hash, embed_config FROM embeddings WHERE source = ?",
                (name,),
            )
        }
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the role gate
# --------------------------------------------------------------------------

def test_an_unfilled_role_is_an_environment_fault_not_a_data_fault(tenant_storage, monkeypatch):
    """[roles.embed] is filled in the real models.toml as of 18 September
    2026 (Step 5.4) -- this test used to rely on the real file being empty
    and now explicitly empties the role instead, so the property it checks
    does not depend on incidental config state. Must fail the same way a
    missing parser library already does: reported once, no row written
    blaming the document for it.
    """
    def unfilled(role):
        raise vectors.models.RoleUnavailable(f"No model fills the {role!r} role.")
    monkeypatch.setattr(vectors.models, "model_for", unfilled)
    name = a_document(tenant_storage)

    report = ingest.ingest(name)

    assert "embeddings" in report["unavailable"]
    assert "embeddings" not in report["failed"]
    # No row written blaming the document -- status() reports it the same
    # way a producer that has genuinely never run reads, not as a failure.
    assert ingest.status(name)["producers"]["embeddings"] == {"status": "never run", "stale": True}


def test_an_unreachable_embedding_server_is_unavailable_not_a_document_failure(tenant_storage, monkeypatch):
    """Step 5.4's own finding: before this, a real outage would have recorded
    one FAILED row per document ingested during it (retried on every future
    attempt), instead of the single clean report a missing parser library
    already gets. embed.EmbedUnavailable is what the producer now catches to
    tell the two apart -- verified here at the producer boundary, with the
    HTTP-level distinction itself covered in tests/test_embed.py.
    """
    name = a_document(tenant_storage)
    install_role(monkeypatch, fake_config())

    def unreachable(texts, model=None, **_):
        raise vectors.embed.EmbedUnavailable("connection refused")
    monkeypatch.setattr(vectors.embed, "embed", unreachable)

    report = ingest.ingest(name)

    assert "embeddings" in report["unavailable"]
    assert "embeddings" not in report["failed"]
    assert ingest.status(name)["producers"]["embeddings"] == {"status": "never run", "stale": True}


def test_a_refused_embedding_request_is_a_document_failure_not_an_outage(tenant_storage, monkeypatch):
    """The other half of the same distinction: the server was reached and
    refused this specific call (a plain EmbedError, not the Unavailable
    subclass) -- that is this document's problem, recorded against it, not
    escalated into "the whole service is down".
    """
    name = a_document(tenant_storage)
    install_role(monkeypatch, fake_config())

    def refused(texts, model=None, **_):
        raise vectors.embed.EmbedError("400: bad input")
    monkeypatch.setattr(vectors.embed, "embed", refused)

    report = ingest.ingest(name)

    assert "embeddings" in report["failed"]
    assert "embeddings" not in report["unavailable"]


# --------------------------------------------------------------------------
# the outer gate: depends_on decides whether to look at all
# --------------------------------------------------------------------------

def test_chunks_are_embedded_and_stored(tenant_storage, monkeypatch):
    name = a_document(tenant_storage)
    calls = install_fake_embed(monkeypatch)
    install_role(monkeypatch, fake_config())

    report = ingest.ingest(name)

    assert sorted(report["ran"]) == ["chunks", "embeddings", "text"]
    made = producers.chunks_of(name)
    assert made  # the fixture text is long enough to be several chunks
    rows = stored_rows(name)
    assert set(rows) == {c.chunk_id for c in made}
    assert sum(len(batch) for batch in calls) == len(made)


def test_an_unchanged_document_is_never_looked_at_again(tenant_storage, monkeypatch):
    """The plan's own §5.2 gate: re-running ingest over an unchanged corpus
    embeds nothing -- because the producer is not even invoked, not because
    it ran and decided there was nothing to do.
    """
    name = a_document(tenant_storage)
    calls = install_fake_embed(monkeypatch)
    install_role(monkeypatch, fake_config())
    ingest.ingest(name)
    calls.clear()

    report = ingest.ingest(name)

    assert report["ran"] == []
    assert sorted(report["current"]) == ["chunks", "embeddings", "text"]
    assert calls == []


def test_dimension_and_vector_round_trip_through_the_blob_column(tenant_storage, monkeypatch):
    name = a_document(tenant_storage)
    install_fake_embed(monkeypatch)
    install_role(monkeypatch, fake_config())
    ingest.ingest(name)

    made = producers.chunks_of(name)
    connection = vectors.connect()
    try:
        row = connection.execute(
            "SELECT vector, dimension FROM embeddings WHERE chunk_id = ?",
            (made[0].chunk_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row["dimension"] == FAKE_DIMENSION
    assert len(vectors._unpack(row["vector"])) == FAKE_DIMENSION


# --------------------------------------------------------------------------
# the inner gate: a per-chunk hash decides which chunks need a new vector
# --------------------------------------------------------------------------

def test_an_edit_near_the_end_only_re_embeds_the_chunks_that_actually_changed(tenant_storage, monkeypatch):
    """The property §5.4 was rewritten to deliver: `depends_on` gets the
    producer to look again (the text changed, so `chunks` reran), but the
    per-chunk hash is what stops it recomputing chunks whose text did not
    move. An edit appended at the very end cannot affect anything before
    it -- `_merge()` (app/chunks.py) accumulates left to right -- so every
    earlier chunk_id must come back with an identical hash and must not be
    re-embedded.
    """
    base = "Payment falls due thirty days from invoice date. " * 200
    name = a_document(tenant_storage, body=base)
    calls = install_fake_embed(monkeypatch)
    install_role(monkeypatch, fake_config())
    ingest.ingest(name)
    before = producers.chunks_of(name)
    assert len(before) >= 3, "the fixture needs to be several chunks for this test to mean anything"
    calls.clear()

    # A change with its own separator, appended after everything else --
    # confirmed to add at least one new chunk without touching earlier ones.
    edited = base + "\n\nAddendum, signed separately: a new clause follows here. " * 5
    (tenant_storage / name).write_text(edited, encoding="utf-8")

    report = ingest.ingest(name)
    after = producers.chunks_of(name)

    # text and chunks rerun too -- the source itself changed -- but the
    # property under test is what embeddings did with the chunks it got.
    # Whether the edit adds a whole new chunk or just grows the last one is
    # app/chunks.py's business, not this test's -- either way, something
    # must actually have changed for the test to mean anything.
    assert "embeddings" in report["ran"]
    assert after != before
    # A chunk_id existing both before and after is NOT the same claim as its
    # text being unchanged -- that is exactly the gap a hash catches and a
    # chunk_id-only check would miss, so this compares the pair, not just
    # the id, before calling something "untouched".
    before_by_id = {c.chunk_id: c.text for c in before}
    reembedded = {t for batch in calls for t in batch}
    unchanged_chunk_texts = {c.text for c in after if before_by_id.get(c.chunk_id) == c.text}
    assert unchanged_chunk_texts, "the edit should have left at least the first chunk untouched"
    assert not (unchanged_chunk_texts & reembedded), (
        "a chunk whose text did not change was sent to embed() again"
    )
    assert sum(len(b) for b in calls) < len(after), "nothing was reused at all"


def test_a_shrinking_document_drops_the_vanished_chunk_ids(tenant_storage, monkeypatch):
    long_body = "Payment falls due thirty days from invoice date. " * 200
    name = a_document(tenant_storage, body=long_body)
    install_fake_embed(monkeypatch)
    install_role(monkeypatch, fake_config())
    ingest.ingest(name)
    before = {c.chunk_id for c in producers.chunks_of(name)}
    assert len(before) >= 3

    (tenant_storage / name).write_text("One short paragraph now.", encoding="utf-8")
    ingest.ingest(name)
    after = {c.chunk_id for c in producers.chunks_of(name)}

    rows = stored_rows(name)
    assert set(rows) == after
    assert not (before - after) & set(rows), "a chunk_id that no longer exists is still stored"


# --------------------------------------------------------------------------
# the config guard: an embed-role change invalidates everything, even
# chunks whose text never moved
# --------------------------------------------------------------------------

def test_a_config_change_re_embeds_every_chunk_even_though_the_text_did_not_move(tenant_storage, monkeypatch):
    name = a_document(tenant_storage)
    calls = install_fake_embed(monkeypatch)
    install_role(monkeypatch, fake_config("model-a"))
    ingest.ingest(name)
    made = producers.chunks_of(name)
    first_pass_calls = sum(len(b) for b in calls)
    assert first_pass_calls == len(made)
    calls.clear()

    install_role(monkeypatch, fake_config("model-b"))
    report = ingest.ingest(name)

    assert report["ran"] == ["embeddings"], (
        "a config change with no document change must still get run() invoked -- "
        "this is EMBEDDINGS.version being derived from the config, not hand-maintained"
    )
    assert sum(len(b) for b in calls) == len(made), (
        "every chunk should have been recomputed: the stored fingerprint no "
        "longer matches the current embed config, even though every chunk's "
        "own text hash still does"
    )


def test_the_derived_version_moves_when_the_config_does(monkeypatch):
    """Not "only goes up" -- app/ingest.py's _is_current() compares by
    equality, so any change in either direction must invalidate. Asserted
    directly on the derivation function, the cheapest place to catch a
    regression that made it stop depending on the config at all.
    """
    monkeypatch.setattr(vectors.models, "model_for", lambda role: fake_config("model-a"))
    version_a = vectors._embed_producer_version()
    monkeypatch.setattr(vectors.models, "model_for", lambda role: fake_config("model-b"))
    version_b = vectors._embed_producer_version()
    monkeypatch.setattr(vectors.models, "model_for", lambda role: fake_config("model-a"))
    version_a_again = vectors._embed_producer_version()

    assert version_a != version_b
    assert version_a == version_a_again, "the same config must derive the same version, deterministically"


# --------------------------------------------------------------------------
# the Vector retriever -- 5.3's gate: a similarity test needs two chunks
# close enough to tell `>` from `>=`, not two trivially different ones
# (the same lesson Step 4's own gate names for exactly this failure mode).
# --------------------------------------------------------------------------

def _insert_vector(connection, chunk_id: str, source: str, vector: list[float]) -> None:
    # vectors.forget_missing() (Vector.search()'s own read-path sweep, added
    # after Phase F of the Step 5 deployment found real orphaned rows) drops
    # any embeddings row whose source is not a real file in the tenant's data
    # folder -- correct against real ingestion, but these tests insert a
    # vector directly, bypassing the producer entirely, so nothing has ever
    # written this name to disk. A placeholder file is enough: forget_missing()
    # only checks existence, never content.
    (config.ensure_data_dir() / source).touch(exist_ok=True)
    connection.execute(
        """
        INSERT INTO embeddings (chunk_id, source, vector, dimension, content_hash, embed_config, indexed_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (chunk_id, source, vectors._pack(vector), len(vector), "irrelevant", "irrelevant", 0.0),
    )


def test_the_nearest_neighbour_ranks_first_even_when_two_candidates_are_close(tenant_storage, monkeypatch):
    query_vector = [1.0, 0.0, 0.0, 0.0]
    monkeypatch.setattr(vectors.embed, "embed", lambda texts, **_: [query_vector])
    monkeypatch.setattr(vectors.models, "model_for", lambda role: fake_config())

    connection = vectors.connect()
    try:
        # Both close to the query and to each other -- the whole point being
        # that a comparator bug (> vs >=, or a sign error) would still often
        # get this right on two vectors that are not close at all.
        _insert_vector(connection, "doc.pdf#0", "doc.pdf", [0.95, 0.05, 0.0, 0.0])   # closer
        _insert_vector(connection, "doc.pdf#1", "doc.pdf", [0.90, 0.10, 0.0, 0.0])   # second
        _insert_vector(connection, "doc.pdf#2", "doc.pdf", [0.0, 0.0, 1.0, 0.0])     # far
        connection.commit()
    finally:
        connection.close()

    hits = vectors.VECTOR.search("query text", limit=3)

    assert [h.chunk_id for h in hits] == ["doc.pdf#0", "doc.pdf#1", "doc.pdf#2"]
    assert [h.rank for h in hits] == [1, 2, 3]


def test_sources_none_means_everything_and_empty_list_means_nothing(tenant_storage, monkeypatch):
    monkeypatch.setattr(vectors.embed, "embed", lambda texts, **_: [[1.0, 0.0, 0.0, 0.0]])
    monkeypatch.setattr(vectors.models, "model_for", lambda role: fake_config())

    connection = vectors.connect()
    try:
        _insert_vector(connection, "a.pdf#0", "a.pdf", [1.0, 0.0, 0.0, 0.0])
        _insert_vector(connection, "b.pdf#0", "b.pdf", [1.0, 0.0, 0.0, 0.0])
        connection.commit()
    finally:
        connection.close()

    assert {h.chunk_id for h in vectors.VECTOR.search("q", limit=10)} == {"a.pdf#0", "b.pdf#0"}
    assert {h.chunk_id for h in vectors.VECTOR.search("q", limit=10, sources=["a.pdf"])} == {"a.pdf#0"}
    assert vectors.VECTOR.search("q", limit=10, sources=[]) == []


def test_vector_is_not_registered_by_importing_this_module(tenant_storage):
    """5.3 builds it; 5.4 is retrieve.register(VECTOR), and NOT here --
    unlike app/passages.py's Keyword, which self-registers on import. If
    this ever starts passing for the wrong reason (something else in the
    suite already registered "vector"), that is itself worth knowing.
    """
    assert "vector" not in retrieve.registered()


def test_a_raised_role_unavailable_becomes_a_retriever_error(tenant_storage, monkeypatch):
    """[roles.embed] is filled in the real models.toml since Step 5.4, so
    this explicitly empties it rather than relying on incidental config
    state (the same fix `test_an_unfilled_role_is_an_environment_fault_not_a_data_fault`
    needed above) -- the RoleUnavailable path, translated to the exception
    retrieve.fuse()'s caller already knows how to handle, the same
    translation Keyword does for PassageError.
    """
    def unfilled(role):
        raise vectors.models.RoleUnavailable(f"No model fills the {role!r} role.")
    monkeypatch.setattr(vectors.models, "model_for", unfilled)

    with pytest.raises(retrieve.RetrieverError):
        vectors.VECTOR.search("anything", limit=5)
