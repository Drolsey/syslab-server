"""Step 4.4: the chunk index, and the one retriever that reads it.

`app/search.py` finds DOCUMENTS. This finds PASSAGES, and the difference is the
whole reason Step 4 exists: an index of whole documents can say "the answer is
somewhere in this fifty-page contract", and a retrieval plane has to say where.

WHAT IS DIFFERENT FROM app/search.py, AND WHAT DELIBERATELY IS NOT
    Not different: the storage rules. Everything here is a CACHE, derived from
    chunks.json which is itself derived from data/. Deleting the table costs a
    rebuild and nothing else, the table lives in the tenant's own index file
    rather than in a shared one with an owner column, and nothing durable is
    ever written here. Those four rules are at the top of app/search.py and
    they are inherited on purpose rather than restated and allowed to drift.

    Different: this is a CONSUMER OF AN ARTIFACT, not of a file. It never opens
    a PDF, never calls a parser, and never runs the pipeline. It reads
    `derived/<tenant>/chunks/<item>/chunks.json` and puts what is in it into an
    FTS5 table. `app/intake.py` owns the order -- it always has -- and by the
    time this is called the fast producers have already run.

    That is why `index_file` here does NOT call `ingest.ingest` the way
    `search.index_file` does. Two consumers each bringing the pipeline up to
    date for themselves is how "what happens to a new file" ends up decided in
    two places that drift, which is the fault intake exists to prevent.

THE COVERAGE COUNTS ARE MEASURED HERE AND NOT INVENTED LATER
    `search()` returns `matched` -- how many passages matched the query, NOT
    how many were returned. Decision 5.6 rests on it: a question about 200
    contracts answered from 8 passages must SAY that it was, and a count
    computed from the returned rows would always equal the limit and would
    always agree with itself. It costs one extra COUNT against an index that
    has already done the matching.

WHY THE PASSAGE TEXT IS STORED IN THE TABLE
    Rule 2 at the top of app/search.py, for the same reason: the extracted text
    is stored, not only the inverted index, so moving to PostgreSQL later is an
    insert rather than a re-derive of everything. It is also what lets a hit
    come back with its passage in one query rather than one query plus a read
    of chunks.json per hit.

    The artifact stays the source of truth. If the two ever disagree, the table
    is the one that is wrong, and the fix is a rebuild.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Sequence

from app import ingest, producers, retrieve, search
from app.config import ensure_data_dir, index_path

# Asked of the chunk producer rather than kept here, the way the text producer
# asks app/parse. `search.SEARCHABLE` is three suffixes and is a Step 2 legacy
# of what the DOCUMENT index was willing to read; the chunk index covers
# everything that produces chunks, which today is all ten formats in
# app/parse.py's table. The two differing is a real gap and it is named in
# docs/plans/step-04-retrieval-plane.md rather than quietly closed here --
# widening the document index would move the 4.1 baseline, which is the one
# number 4.4 has to be measured against.
def handles() -> frozenset[str]:
    return producers.CHUNKS.handles


class PassageError(Exception):
    """Something a caller can read and act on."""


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

# In the tenant's EXISTING index file, as a second table. Section 6 of the plan:
# "index/<tenant>.sqlite3 -- FTS5, gains a `chunks` table". One file per tenant
# stays one file per tenant; a second database would be a second thing to
# delete, a second thing to back up, and a second chance to open the wrong one.
#
# The offset columns are `start_char` and `end_char` rather than `start` and
# `end` because `end` is a keyword in SQLite and a quoted column name in every
# statement that touches it is a quoting mistake waiting to happen. The API
# shape in section 6 says `start` and `end`; 4.6 maps them.
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    chunk_id UNINDEXED,
    source UNINDEXED,
    ordinal UNINDEXED,
    text,
    start_char UNINDEXED,
    end_char UNINDEXED,
    tokens UNINDEXED,
    indexed_at UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def connect() -> sqlite3.Connection:
    """Open this tenant's index, with the chunks table present.

    Built on search.connect() rather than beside it. Both tables live in one
    file, and two functions that each open it with their own PRAGMAs and their
    own fallback behaviour would be two answers to "can this mount host a
    SQLite database" -- a question that already has one.
    """
    try:
        connection = search.connect()
    except search.SearchError as exc:
        raise PassageError(str(exc)) from exc
    try:
        connection.executescript(SCHEMA)
    except sqlite3.OperationalError as exc:
        connection.close()
        raise PassageError(
            f"Could not create the passage index at {index_path()}: {exc}."
        ) from exc
    return connection


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def index_source(name: str, connection: sqlite3.Connection | None = None) -> dict:
    """Put this document's passages into the index, replacing what was there.

    Takes a NAME rather than a Path, because what it indexes is an artifact and
    not a file -- there is nothing here that needs to open the source, and
    asking for a path would suggest otherwise.

    Safe to call repeatedly, and safe to call for a document that has no
    passages: a scan has no text layer, so it has no chunks, and the honest
    outcome is zero rows rather than a failure.
    """
    own = connection is None
    connection = connection or connect()
    try:
        passages = producers.chunks_of(name)
        connection.execute("DELETE FROM chunks WHERE source = ?", (name,))
        now = time.time()
        connection.executemany(
            """
            INSERT INTO chunks (
                chunk_id, source, ordinal, text, start_char, end_char, tokens, indexed_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (c.chunk_id, name, c.ordinal, c.text, c.start, c.end, c.tokens, now)
                for c in passages
            ],
        )
        connection.commit()
        return {
            "name": name,
            "passages": len(passages),
            # Does not name a cause it has not established. "No passages" is a
            # scan, an empty file, or a document nobody has produced yet, and
            # ingest.status() is where those are told apart.
            "reason": "" if passages else "no passages have been produced for it",
        }
    finally:
        if own:
            connection.close()


def index_file(path: Path, connection: sqlite3.Connection | None = None) -> dict:
    """The same thing, for a caller that is holding a path.

    Exists so that `intake.arrived(path)` reads as one sentence beside
    `search.index_file(path)` rather than reaching into the path for a name.
    """
    if path.suffix.lower() not in handles():
        return {"name": path.name, "passages": 0, "reason": "nothing chunks this file type"}
    return index_source(path.name, connection)


def remove(name: str, connection: sqlite3.Connection | None = None) -> None:
    own = connection is None
    connection = connection or connect()
    try:
        connection.execute("DELETE FROM chunks WHERE source = ?", (name,))
        connection.commit()
    finally:
        if own:
            connection.close()


def forget_missing(connection: sqlite3.Connection | None = None) -> list[str]:
    """Drop passages belonging to source files that are no longer on disk.

    The counterpart to search.forget_missing(), and it runs on the read path
    for the same reason: a hit that cites a document which is not there costs
    the caller a turn finding that out, and a passage is worse than a document
    row because it comes back quoted. A deleted contract must not be able to
    answer a question with a paragraph out of itself.
    """
    own = connection is None
    connection = connection or connect()
    try:
        folder = ensure_data_dir()
        gone = sorted({
            row["source"]
            for row in connection.execute("SELECT DISTINCT source FROM chunks")
            if not (folder / row["source"]).is_file()
        })
        for name in gone:
            connection.execute("DELETE FROM chunks WHERE source = ?", (name,))
        if gone:
            connection.commit()
        return gone
    except sqlite3.Error:  # a tidy-up is never worth failing a search over
        return []
    finally:
        if own:
            connection.close()


def rebuild(report=None) -> dict:
    """Throw the passage index away and build it again from the artifacts.

    Brings the pipeline up to date first, exactly as search.rebuild() does, so
    that "rebuild from what is on disk" is honest when this is called on its
    own. When intake calls both in a row the second call is nearly free: every
    producer is current by then and the walk finds nothing to do.
    """
    ingest.rebuild(only_fast=True)

    folder = ensure_data_dir()
    names = sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in handles() and not p.name.startswith(".")
    )
    connection = connect()
    started = time.time()
    indexed, empty = [], []
    try:
        connection.execute("DELETE FROM chunks")
        connection.commit()
        for number, name in enumerate(names, start=1):
            result = index_source(name, connection)
            (indexed if result["passages"] else empty).append(result)
            if report:
                report(number / max(1, len(names)), f"{number} of {len(names)}: {name}")
    finally:
        connection.close()
    return {
        "files_seen": len(names),
        "documents_indexed": len(indexed),
        "passages": sum(r["passages"] for r in indexed),
        "no_passages": [r["name"] for r in empty],
        "seconds": round(time.time() - started, 2),
    }


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

# The source filter, as a SQL condition rather than as a list comprehension
# afterwards, and that is the whole point of it being here.
#
# Filtering AFTER retrieval would make two numbers lie at once: `k` would come
# back short with no explanation, and `matched` would count passages the caller
# had excluded -- so `coverage` would report a census of the wrong corpus, which
# is exactly the field decision 5.6 rests on.
def _scope(sources: Sequence[str] | None) -> tuple[str, list[str]]:
    """A WHERE condition and its parameters. `None` is every source.

    AN EMPTY LIST IS NOTHING, NOT EVERYTHING, and the choice needs its reason
    attached because the other reading is the common one. A caller that
    computed a filter -- the documents a user ticked, say -- and computed an
    empty one must not be answered with the whole corpus. Returning MORE than
    was asked for is the failure this plane is built to avoid, and of the two
    surprises, "nothing came back" costs a retry while "everything came back"
    spends the caller's token budget on material they excluded.
    """
    if sources is None:
        return "1", []
    names = [str(name) for name in sources]
    if not names:
        return "0", []
    return f"source IN ({','.join('?' for _ in names)})", names


def search_passages(
    query: str, limit: int = 8, sources: Sequence[str] | None = None
) -> dict:
    """Find passages by what is inside them.

    All terms first, any term as a fallback, and it says which one answered --
    the same two-pass shape as the document index, using THE SAME TOKENIZER.
    `search.terms()` was private until this file existed; it is shared rather
    than copied because two indexes that disagree about what a query means
    would make every comparison between them meaningless, including the one
    scripts/check_retrieval.py exists to make.

    `matched` is the count of passages the expression matched, not the count
    returned. That is decision 5.6's load-bearing half and the reason it is
    measured here: a count derived from the returned rows would equal the limit
    every time and would agree with itself every time.
    """
    terms = search.terms(query)
    if not terms:
        raise PassageError("Nothing searchable in that query. Give me some words to look for.")

    limit = max(1, min(int(limit), 100))
    scope, scoped = _scope(sources)
    connection = connect()
    try:
        forget_missing(connection)
        total = connection.execute(
            f"SELECT count(*) AS n FROM chunks WHERE {scope}", scoped
        ).fetchone()["n"]
        for joiner, precision in ((" AND ", "all terms"), (" OR ", "any term")):
            expression = joiner.join(terms)
            try:
                matched = connection.execute(
                    f"SELECT count(*) AS n FROM chunks WHERE chunks MATCH ? AND {scope}",
                    (expression, *scoped),
                ).fetchone()["n"]
                rows = connection.execute(
                    f"""
                    SELECT chunk_id, source, ordinal, text, start_char, end_char, tokens,
                           snippet(chunks, 3, '[', ']', ' ... ', 18) AS snippet,
                           bm25(chunks) AS score
                    FROM chunks
                    WHERE chunks MATCH ? AND {scope}
                    ORDER BY score
                    LIMIT ?
                    """,
                    (expression, *scoped, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise PassageError(f"Could not run that search: {exc}") from exc
            if rows:
                return {
                    "query": query,
                    "matched_on": precision,
                    "passages_indexed": total,
                    "matched": matched,
                    "count": len(rows),
                    "results": [
                        {
                            "chunk_id": row["chunk_id"],
                            "source": row["source"],
                            "ordinal": int(row["ordinal"]),
                            "text": row["text"],
                            "start": int(row["start_char"]),
                            "end": int(row["end_char"]),
                            "tokens": int(row["tokens"]),
                            "snippet": " ".join((row["snippet"] or "").split()),
                        }
                        for row in rows
                    ],
                }
        return {
            "query": query,
            "matched_on": None,
            "passages_indexed": total,
            "matched": 0,
            "count": 0,
            "results": [],
        }
    finally:
        connection.close()


def coverage(query: str, sources: Sequence[str] | None = None) -> dict:
    """How many passages were in scope, and how many the query matched.

    Decision 5.6, and the reason it is a function of its own rather than a
    by-product of `search_passages`: the plane asks the RETRIEVER SEAM for its
    ranking, and the seam carries ranks and nothing else. A coverage count
    assembled from what the seam handed back would equal the limit every time.

    COUNTS ONLY -- no rows, no bm25, no ORDER BY. It is two COUNT(*) queries
    against an index that has already done the matching.

    WHAT `matched` WILL MEAN WHEN THERE ARE TWO RETRIEVERS IS NOT SETTLED, and
    pretending otherwise here would be the dishonest part. This is the KEYWORD
    index's count: how many passages the FTS5 expression matched. That is
    well-defined today because keyword is the only retriever. A vector
    retriever matches EVERYTHING at some distance, so "matched" stops having an
    obvious meaning the day Step 5 lands, and it is named in
    docs/plans/step-04-retrieval-plane.md as a thing to re-decide rather than
    left to be discovered. `searched` is unaffected: it is how many passages
    were in scope, which is true regardless of who does the searching.
    """
    terms = search.terms(query)
    if not terms:
        raise PassageError("Nothing searchable in that query. Give me some words to look for.")

    scope, scoped = _scope(sources)
    connection = connect()
    try:
        searched = connection.execute(
            f"SELECT count(*) AS n FROM chunks WHERE {scope}", scoped
        ).fetchone()["n"]
        for joiner, precision in ((" AND ", "all terms"), (" OR ", "any term")):
            try:
                matched = connection.execute(
                    f"SELECT count(*) AS n FROM chunks WHERE chunks MATCH ? AND {scope}",
                    (joiner.join(terms), *scoped),
                ).fetchone()["n"]
            except sqlite3.OperationalError as exc:
                raise PassageError(f"Could not run that search: {exc}") from exc
            # The same two-pass rule as search_passages, in the same order, so
            # the count agrees with the list. If the AND pass matched nothing
            # the ranking fell through to OR, and a coverage figure taken from
            # the AND pass would report zero beside eight returned passages.
            if matched:
                return {"searched": searched, "matched": matched, "matched_on": precision}
        return {"searched": searched, "matched": 0, "matched_on": None}
    finally:
        connection.close()


def passage(chunk_id: str) -> dict | None:
    """One passage by its citation, or None.

    What makes a chunk_id worth putting in an API response: something can be
    asked for again. Reads the index rather than the artifact because the
    index is what issued the id in the first place.
    """
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT chunk_id, source, ordinal, text, start_char, end_char, tokens
            FROM chunks WHERE chunk_id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "chunk_id": row["chunk_id"],
            "source": row["source"],
            "ordinal": int(row["ordinal"]),
            "text": row["text"],
            "start": int(row["start_char"]),
            "end": int(row["end_char"]),
            "tokens": int(row["tokens"]),
        }
    finally:
        connection.close()


def opened_path() -> str:
    """Which database file connect() actually opened, asked of SQLite itself.

    Not `index_path()`. The two agree, and a check that reads the second to
    prove the first is a check that passes when connect() has been changed to
    open something else -- which is precisely the bug it exists to catch.
    scripts/check_isolation.py broke tenant scoping on purpose and this was the
    one assertion of its four that carried on saying PASS.
    """
    connection = connect()
    try:
        for row in connection.execute("PRAGMA database_list"):
            if row[1] == "main":
                return str(row[2])
        return ""
    finally:
        connection.close()


def status() -> dict:
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT count(*) AS n, count(DISTINCT source) AS docs, sum(tokens) AS tokens
            FROM chunks
            """
        ).fetchone()
        return {
            "index_path": str(index_path()),
            "passages": row["n"] or 0,
            "documents": row["docs"] or 0,
            "tokens": row["tokens"] or 0,
            "disposable": True,
        }
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the retriever
# --------------------------------------------------------------------------

class Keyword:
    """BM25 over the passage index. The first thing to implement the seam.

    It hands back POSITIONS and nothing else. BM25's own score is right there
    in the query above and is deliberately dropped on the floor here: decision
    5.5, and the point of it is that an unbounded negative number cannot be
    fused with a cosine similarity, so the only safe thing to carry across the
    seam is where each hit came in the list.
    """

    name = "keyword"

    def search(
        self, query: str, limit: int, sources: Sequence[str] | None = None
    ) -> list[retrieve.Hit]:
        try:
            found = search_passages(query, limit, sources=sources)
        except PassageError as exc:
            raise retrieve.RetrieverError(str(exc)) from exc
        return [
            retrieve.Hit(chunk_id=row["chunk_id"], rank=position)
            for position, row in enumerate(found["results"], start=1)
        ]


KEYWORD = retrieve.register(Keyword())
