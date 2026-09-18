"""Step 5.2/5.3: the embeddings producer, its table, and the Vector retriever.

WHY A BLOB COLUMN AND PYTHON COSINE SIMILARITY, NOT sqlite-vec
    `docs/plans/step-05-embeddings.md` §5.2 decided sqlite-vec, provisionally,
    with this as the named fallback -- "if sqlite-vec's pre-v1 breaking-changes
    warning turns into an actual break". It is used here from the start
    instead, not because that decision reversed: sqlite-vec is not an
    installed dependency (checked 18 September -- not in requirements.txt,
    not importable), and adding a new compiled binary dependency is a
    decision for a person to make deliberately, not one a producer
    implementation should make for itself along the way. Swapping the
    storage/similarity layer for sqlite-vec later is a change inside this
    module's search() and schema, not a redesign of anything that calls it --
    `retrieve.Hit` carries no score (decision 5.3), so nothing outside this
    file can tell which implementation answered a query.

WHY THE FRESHNESS LOGIC BELOW IS SHAPED THE WAY IT IS
    Full reasoning in `docs/plans/step-05-embeddings.md` §5.4, not repeated
    here in full. The short version: TWO separate questions, two separate
    mechanisms. Whether the producer runs AT ALL for a document is decided
    by `Producer.depends_on={"chunks"}` -- machinery Step 4.3 already built,
    nothing new. Which of that document's chunks actually need a new vector
    is decided by a hash of each chunk's own text (cheap -- a chunk is
    capped at 512 tokens, nothing like the file-level cost
    `app/ingest.py`'s `_is_current()` is titled "Deliberately NOT a content
    hash" to avoid). And whether an unchanged chunk's STORED vector is still
    valid is decided by `EMBEDDINGS.version`, derived from `[roles.embed]`'s
    config rather than hand-maintained, so a model swap invalidates every
    document automatically on the next process start.

WHY THIS PRODUCER WRITES NOTHING TO derived/
    Every other producer's output is a citable artifact -- chunks.json is
    what a passage's offsets point into. A vector is not a citation; it is
    an index entry. It lives in the tenant's own index file (see connect()),
    the same file `app/passages.py`'s `chunks` FTS5 table already lives in,
    for the same reason that one is there and not a fourth file to delete
    when a tenant leaves.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import time
from pathlib import Path
from typing import Sequence

from app import embed, ingest, models, producers, retrieve, search

# scripts/bench_embeddings.py measured batching's throughput gain mostly
# captured by 32 chunks per call (docs/models.md, Step 5.1's candidate
# comparison) -- more than this bought little and risks a larger single
# HTTP payload for no real return.
EMBED_BATCH_SIZE = 32


class VectorError(Exception):
    """Something a caller can read and act on."""


# --------------------------------------------------------------------------
# storage -- a third table in the tenant's existing index file
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id      TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    vector        BLOB NOT NULL,
    dimension     INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    embed_config  TEXT NOT NULL,
    indexed_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS embeddings_source ON embeddings(source);
"""


def connect() -> sqlite3.Connection:
    """Open this tenant's index, with the embeddings table present.

    Built on search.connect(), the same way app/passages.py's chunks table
    is: one file per tenant stays one file per tenant, rather than a second
    function with its own PRAGMAs answering a question search.py already
    answers once.
    """
    try:
        connection = search.connect()
    except search.SearchError as exc:
        raise VectorError(str(exc)) from exc
    try:
        connection.executescript(SCHEMA)
    except sqlite3.OperationalError as exc:
        connection.close()
        raise VectorError(f"Could not create the embeddings table: {exc}.") from exc
    return connection


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def _content_hash(text: str) -> str:
    """A hash of ONE CHUNK's text, not a source file's.

    Deliberately unlike `_is_current()`'s own reasoning in app/ingest.py,
    which avoids hashing a whole source file because that cost is paid on
    every upload for a change size and mtime already caught. A chunk is
    capped at TARGET_TOKENS (app/chunks.py, 512), so hashing one is
    microseconds -- the cost that reasoning was written to avoid does not
    apply here. See docs/plans/step-05-embeddings.md §5.4.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _config_fingerprint(config: dict) -> str:
    """A stable string identifying the embed model/provider that produced a
    vector, so a config change is detectable even when a chunk's text did
    not change at all -- the second of §5.4's two guards, kept alongside
    EMBEDDINGS.version rather than instead of it, because the version is
    what gets run() invoked again in the first place."""
    return json.dumps(config, sort_keys=True)


# --------------------------------------------------------------------------
# the embeddings producer
# --------------------------------------------------------------------------

def _embed_producer_version() -> int:
    """Derived from the CURRENT embed config, not hand-maintained.

    A config change in [roles.embed] must invalidate every document's
    embeddings automatically, on the next process start, with nobody
    remembering to bump a version constant -- the same "process-start
    effect" caveat app/models.py's own docstring already documents for
    models.toml changes in general. `_is_current()` (app/ingest.py)
    compares producer_version by EQUALITY, not by order, so a derived value
    moving in either direction on a config change still invalidates
    correctly; "a producer version... only goes up" is a convention for
    hand-maintained versions, not a constraint this equality check enforces.

    Returns 1 when the role is unfilled: model_for("embed") raises
    RoleUnavailable inside run() before this value is ever compared against
    anything, so its exact value is moot there -- it exists only so
    Producer's own __post_init__ (version >= 1) does not refuse to register
    this producer at import, before any tenant has even been chosen.
    """
    try:
        config = models.model_for("embed")
    except models.RoleUnavailable:
        return 1
    digest = hashlib.sha256(_config_fingerprint(config).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) or 1  # never 0: Producer requires version >= 1


def _existing_rows(connection: sqlite3.Connection, source_name: str) -> dict[str, tuple[str, str]]:
    """chunk_id -> (content_hash, embed_config) for everything already stored."""
    return {
        row["chunk_id"]: (row["content_hash"], row["embed_config"])
        for row in connection.execute(
            "SELECT chunk_id, content_hash, embed_config FROM embeddings WHERE source = ?",
            (source_name,),
        )
    }


def _run_embeddings(source: Path, out_dir: Path) -> ingest.Result:
    """Chunks in, vectors out -- keyed by chunk_id, reusing what has not changed.

    READS THE CHUNKS ARTIFACT, NOT THE TEXT ARTIFACT, the same reasoning
    Step 4.3's chunk producer reads the text artifact and not the source
    file: this embeds exactly what was split, or it does not embed.
    `source` is otherwise unused except for its name, the same deliberate
    shape app/producers.py's own chunk producer already uses -- a producer
    signature is a contract, not every producer opens the file it is about.

    `out_dir` is unused: nothing here writes to derived/. See this module's
    own docstring for why an embedding is not a citable artifact the way
    chunks.json is.
    """
    try:
        config = models.model_for("embed")
    except models.RoleUnavailable as exc:
        raise ingest.ProducerUnavailable(
            f"[roles.embed] is not filled in models.toml, so nothing can embed "
            f"this file: {exc}"
        ) from exc

    current = producers.chunks_of(source.name)
    current_hashes = {c.chunk_id: _content_hash(c.text) for c in current}
    fingerprint = _config_fingerprint(config)

    connection = connect()
    try:
        existing = _existing_rows(connection, source.name)

        if not current:
            gone = set(existing)
            if gone:
                connection.executemany(
                    "DELETE FROM embeddings WHERE chunk_id = ?", [(g,) for g in gone]
                )
                connection.commit()
            return ingest.Result.nothing("there are no chunks for this file to embed")

        to_embed = [
            c for c in current
            if existing.get(c.chunk_id) != (current_hashes[c.chunk_id], fingerprint)
        ]
        reused = len(current) - len(to_embed)

        for i in range(0, len(to_embed), EMBED_BATCH_SIZE):
            batch = to_embed[i:i + EMBED_BATCH_SIZE]
            try:
                vectors = embed.embed([c.text for c in batch], model=config["model"])
            except embed.EmbedUnavailable as exc:
                # The server is unreachable -- an environment fault, not a
                # per-document one. Translated to ProducerUnavailable so the
                # pipeline reports ONE outage, not one FAILED row per
                # document ingested while it was down (each retried on every
                # future attempt). A plain EmbedError (the server was
                # reached and refused this request) is NOT caught here and
                # falls through to ingest.py's ordinary per-document FAILED
                # handling, which is the correct shape for that case.
                raise ingest.ProducerUnavailable(
                    f"The embedding server is unreachable: {exc}"
                ) from exc
            now = time.time()
            connection.executemany(
                """
                INSERT INTO embeddings (
                    chunk_id, source, vector, dimension, content_hash, embed_config, indexed_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    vector       = excluded.vector,
                    dimension    = excluded.dimension,
                    content_hash = excluded.content_hash,
                    embed_config = excluded.embed_config,
                    indexed_at   = excluded.indexed_at
                """,
                [
                    (c.chunk_id, source.name, _pack(v), len(v),
                     current_hashes[c.chunk_id], fingerprint, now)
                    for c, v in zip(batch, vectors)
                ],
            )

        # Rows for a chunk_id no longer in the current split -- a shorter
        # re-chunk, or a chunk whose ordinal now holds different text under
        # the same source. The same "nothing orphaned" rule forget_missing()
        # already enforces elsewhere (app/ingest.py, app/passages.py).
        gone = set(existing) - set(current_hashes)
        if gone:
            connection.executemany(
                "DELETE FROM embeddings WHERE chunk_id = ?", [(g,) for g in gone]
            )

        connection.commit()
    finally:
        connection.close()

    return ingest.Result.made(
        detail=f"{len(to_embed)} embedded, {reused} reused, {len(gone)} dropped"
    )


# NOT registered at import, unlike app/producers.py's TEXT and CHUNKS -- and
# that is a deliberate difference, not an oversight matching their pattern
# halfway. TEXT and CHUNKS are foundational: every document needs them, and
# "unavailable" for either is a genuine environment fault (a missing parser
# library) that SHOULD hold a document back from ready. Embeddings is
# additive and role-gated (docs/plans/step-05-embeddings.md's whole framing:
# "a second retriever," rollback is "nothing in Steps 0-4 reads anything
# this step writes") and [roles.embed] is expected to stay empty for a real
# stretch of time -- this checkout's models.toml has it empty right now.
# `ingest.status()`'s `ready` is "every HANDLING producer holding a current
# row" -- if this self-registered the way TEXT/CHUNKS do, importing this
# module anywhere would make EVERY existing document in the system
# permanently "not ready" the moment [roles.embed] is unfilled, silently
# redefining what "ready" has meant for Steps 0-4. Caught by the existing
# suite, not reasoned out in advance: registering this at import broke
# `ready` assertions across test_chunks.py, test_intake.py,
# test_plane_documents.py and test_tenant_isolation.py the moment
# tests/test_vectors.py imported this module in the same pytest session.
# Registration is therefore the caller's decision: `ingest.register(EMBEDDINGS)`
# wherever a deployment turns Step 5 on, not a side effect of importing this
# file. tests/test_vectors.py registers it itself, scoped to that file only.
EMBEDDINGS = ingest.Producer(
    name="embeddings",
    version=_embed_producer_version(),
    handles=producers.CHUNKS.handles,
    # Fast or slow is 5.6's decision, using real batch latency at production
    # chunk volumes rather than the guess this defaults to today. See §5.4's
    # "What is not decided".
    slow=False,
    depends_on=frozenset({producers.CHUNKS.name}),
    run=_run_embeddings,
)


def remove(source_name: str, connection: sqlite3.Connection | None = None) -> None:
    """Drop this source's embeddings. The counterpart to passages.remove()."""
    own = connection is None
    connection = connection or connect()
    try:
        connection.execute("DELETE FROM embeddings WHERE source = ?", (source_name,))
        connection.commit()
    finally:
        if own:
            connection.close()


# --------------------------------------------------------------------------
# the Vector retriever. 5.3 builds it; 5.4 is `retrieve.register(VECTOR)`,
# deliberately a separate sub-step and NOT done here -- unlike
# app/passages.py's Keyword, which self-registers at import, this is left
# unregistered so 5.4's gate (the single-retriever fusion test surviving
# contact with a real second retriever) is checked at the moment it starts
# being true, not folded silently into importing this module.
# --------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class Vector:
    """Brute-force cosine similarity over the embeddings table.

    The same shape as app/passages.py's Keyword (app/passages.py:493): hand
    back ranks and nothing else, the score computed below and thrown away
    on the way out -- decision 5.3, so a vector retriever cannot leak its
    scale into retrieve.fuse() any more than BM25 already does.

    Brute-force is the decision docs/licences.md and the Step 5 plan already
    named, not an oversight here: "a few thousand chunks per tenant,
    brute-force cosine similarity is a table scan, not an algorithm problem."
    """

    name = "vector"

    def search(
        self, query: str, limit: int, sources: Sequence[str] | None = None
    ) -> list[retrieve.Hit]:
        try:
            config = models.model_for("embed")
        except models.RoleUnavailable as exc:
            raise retrieve.RetrieverError(str(exc)) from exc

        try:
            [query_vector] = embed.embed([query], model=config["model"])
        except embed.EmbedError as exc:
            raise retrieve.RetrieverError(f"could not embed the query: {exc}") from exc

        connection = connect()
        try:
            if sources is None:
                rows = connection.execute("SELECT chunk_id, vector FROM embeddings").fetchall()
            elif not sources:
                # An explicitly empty list means nothing, per Retriever's own
                # contract -- not "no filter", which None means.
                rows = []
            else:
                placeholders = ",".join("?" for _ in sources)
                rows = connection.execute(
                    f"SELECT chunk_id, vector FROM embeddings WHERE source IN ({placeholders})",
                    tuple(sources),
                ).fetchall()
        finally:
            connection.close()

        scored = sorted(
            ((row["chunk_id"], _cosine(query_vector, _unpack(row["vector"]))) for row in rows),
            key=lambda pair: -pair[1],
        )[:limit]
        return [
            retrieve.Hit(chunk_id=chunk_id, rank=position)
            for position, (chunk_id, _score) in enumerate(scored, start=1)
        ]


VECTOR = Vector()
