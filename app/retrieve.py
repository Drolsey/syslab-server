"""Steps 4.4 and 4.5: what a retriever is, how one registers, and how several combine.

WHAT IS HERE
    The SEAM -- a `Hit`, a `Retriever` protocol, a registry -- and Reciprocal
    Rank Fusion over it. 4.4 built the first three; 4.5 added `fuse()` and
    `search()` as functions in this same file rather than as a module that has
    to be plumbed in.

    The seam landed one sub-step before the fusion on purpose. `Hit` is the type
    every retriever returns, and a seam type that lives inside the first
    implementation is not a seam -- the day a vector retriever lands it would
    be importing its result type from the keyword module, which is the shape
    of the arrangement Step 2 spent five sub-steps ending.

    TODAY'S FUSION PROVABLY DOES NOTHING, AND THAT IS THE POINT. One retriever
    is registered, and fusion of a single ranked list is that list in the same
    order -- asserted directly in tests/test_retrieve.py rather than reasoned
    about, because an RRF that moves a single list is broken and this is the
    only moment it is cheap to notice. Step 5 turns it on by appending to a
    list. That is the 2.1 pattern, and 2.1 is where the gate caught two real
    bugs.

A RETRIEVER RETURNS RANKS, NOT SCORES, AND THE TYPE IS WHERE THAT IS DECIDED
    Decision 5.5. BM25 is unbounded and negative; cosine similarity is -1 to 1.
    Averaging them is arithmetic on incompatible units, and it produces a
    number that looks like a score and means nothing. Reciprocal Rank Fusion
    throws the scores away and keeps the positions:

        score(chunk) = SUM  weight[retriever] / (60 + rank[retriever][chunk])

    `Hit` therefore carries a rank and NOT a score. A retriever that cannot
    leak its scale into the fusion cannot break the fusion, and the next person
    cannot "improve" it by blending. That is the decision expressed in the type
    rather than written in a comment somebody has to find.

    It is also why nothing in the API response carries a score -- section 6: a
    float labelled "score" invites thresholding, and it would mean something
    different the day a second retriever joins.

WHY A REGISTRY RATHER THAN A LIST IN A FUNCTION SOMEWHERE
    The same reason producers and sources have one. A vector retriever in Step
    5 is a module that registers itself at import, and turning it on is
    appending to a list rather than editing whatever function does the asking.
    Fusion of a single ranked list is that list in the same order, so the
    machinery ships doing provably nothing and the interesting thing moves
    behind it later -- the 2.1 pattern, and 2.1 is the sub-step where the gate
    caught two real bugs.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable


class RetrieverError(Exception):
    """A retriever could not answer, for reasons to do with the request.

    Distinct from returning nothing. "No passage matched" is an answer and
    comes back as an empty list; this is "that question cannot be asked".
    """


@dataclass(frozen=True)
class Hit:
    """One passage, and where this retriever put it.

    `rank` is 1-BASED and is a position within THIS retriever's own results.
    Rank 1 from the keyword retriever and rank 1 from a vector retriever are
    not the same claim and are not comparable -- which is exactly why fusion
    combines positions rather than trusting either of them.

    There is no score field and that is not an oversight. See the note above.
    """

    chunk_id: str
    rank: int

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError(
                f"A rank is 1-based and {self.rank} is not a position. Rank 0 is "
                "the off-by-one that makes 1/(60 + rank) quietly wrong for the "
                "best hit of every query."
            )


@runtime_checkable
class Retriever(Protocol):
    """A name, and one way of finding passages.

    `search` returns hits in its own rank order, best first, at most `limit` of
    them. It returns [] when nothing matched; it raises only when the question
    itself could not be asked.
    """

    name: str

    def search(self, query: str, limit: int) -> list[Hit]:
        ...


_RETRIEVERS: dict[str, Retriever] = {}


def register(retriever: Retriever) -> Retriever:
    """Add a retriever. A module registers itself at import, as producers do."""
    if not getattr(retriever, "name", ""):
        raise RetrieverError("A retriever needs a name: it is what `found_by` reports.")
    _RETRIEVERS[retriever.name] = retriever
    return retriever


def unregister(name: str) -> None:
    _RETRIEVERS.pop(name, None)


def registered() -> dict[str, Retriever]:
    """A copy, so a caller holding this cannot quietly change what runs."""
    return dict(_RETRIEVERS)


def names() -> list[str]:
    """Which retrievers are available, in a stable order.

    Sorted rather than insertion-ordered: this is what the `retrievers` field
    of the API response reports, and a list whose order depends on which
    module imported first is a response that changes for no reason anybody can
    see.
    """
    return sorted(_RETRIEVERS)


# --------------------------------------------------------------------------
# fusion (4.5)
# --------------------------------------------------------------------------

# Decision 5.5, and the value is the one the RRF literature uses. It is a
# smoothing constant, not a tuning knob: at k = 60 the gap between rank 1 and
# rank 2 is small (1/61 vs 1/62), so one retriever being confidently wrong
# costs less than one retriever being confidently right gains. Lowering it
# sharpens the top of the list and makes a single retriever's opinion dominate,
# which is the thing fusion exists to avoid.
RRF_K = 60

DEFAULT_WEIGHT = Fraction(1)


@dataclass(frozen=True)
class Fused:
    """One passage, where the fusion put it, and who put it there.

    `found_by` is the field that makes a second retriever visible without
    instrumenting the box -- section 6 of the plan. It is how anyone sees
    whether the vector side actually contributes, or whether every hit in the
    response came from keyword search alone.

    NO SCORE, for the third time in this file and for one more reason than
    `Hit` has: the fused number is not on a scale anybody outside this function
    could interpret. 1/61 is the best score achievable by one retriever and
    2/61 by two, so the same passage, equally well retrieved, scores twice as
    much on the day Step 5 ships. A caller who had thresholded on it would
    silently change behaviour. The order is the output.
    """

    chunk_id: str
    rank: int
    found_by: tuple[str, ...]


def fuse(
    ranked: Mapping[str, Sequence[Hit]],
    weights: Mapping[str, float] | None = None,
    limit: int | None = None,
    k: int = RRF_K,
) -> list[Fused]:
    """Combine several ranked lists into one, by position and never by score.

        score(chunk) = SUM  weight[retriever] / (k + rank[retriever][chunk])

    `ranked` maps a retriever's name to the hits it returned. A chunk missing
    from a list contributes nothing from it rather than contributing a penalty:
    RRF's whole shape is that absence is silence, not disagreement.

    FUSING ONE LIST RETURNS THAT LIST IN THAT ORDER, which is the property 4.5
    is gated on. It follows from 1/(k + rank) being strictly decreasing in
    rank, and it is asserted directly anyway.

    THE ARITHMETIC IS EXACT, AND THAT IS DELIBERATE -- the same reasoning as
    app/chunks.py counting tokens in integers. Ranks fused as floats make the
    order depend on the order the terms were added, which is the order the
    retrievers were registered in, which is which module imported first. Two
    chunks that are mathematically tied would then sort by whichever sum
    happened to round up, and the answer would change when an unrelated import
    moved. `Fraction` makes a tie a real tie, so the tiebreak below is the only
    thing that decides it.
    """
    if k < 1:
        raise RetrieverError(
            f"RRF's k is a smoothing constant and must be at least 1; {k} is not. "
            "At k = 0 a rank-1 hit scores 1/1 and a rank-2 hit 1/2, so one "
            "retriever's first place outranks every other retriever combined."
        )

    # A negative weight is incoherent with the model rather than merely odd.
    # A chunk no retriever found contributes 0, so a negative weight says "being
    # found by this retriever is worse than not being found at all", and the
    # chunk sorts below passages nobody retrieved. Weight 0 is allowed and means
    # "contribute nothing"; its hits then sort last rather than vanishing, so to
    # take a retriever out of an answer, do not ask it.
    weight_for = {}
    for name, value in (weights or {}).items():
        if value < 0:
            raise RetrieverError(
                f"Weight {value} for retriever {name!r} is negative. Absence "
                "contributes zero, so a negative weight ranks a passage below one "
                "that nothing found. To leave a retriever out, do not ask it."
            )
        weight_for[name] = Fraction(value)
    scores: dict[str, Fraction] = {}
    best_rank: dict[str, int] = {}
    found_by: dict[str, list[str]] = {}

    # Sorted by retriever name, so nothing downstream depends on registration
    # order -- not the arithmetic, not `found_by`, not the tiebreak.
    for name in sorted(ranked):
        hits = ranked[name]
        weight = weight_for.get(name, DEFAULT_WEIGHT)
        seen: set[str] = set()
        used: set[int] = set()
        for hit in hits:
            # A retriever that lists a chunk twice would count it twice and
            # push it to the top of the fused list, and nothing about the
            # output would look wrong. Refused by name rather than deduplicated
            # quietly: it is a bug in that retriever, and silently repairing it
            # here is how it survives to the next one.
            if hit.chunk_id in seen:
                raise RetrieverError(
                    f"Retriever {name!r} returned {hit.chunk_id!r} twice. A ranked "
                    "list is a list of distinct passages; fusing a repeat counts "
                    "it twice and nothing in the result looks wrong."
                )
            if hit.rank in used:
                raise RetrieverError(
                    f"Retriever {name!r} put two passages at rank {hit.rank}. A rank "
                    "is a position, and two things cannot share one."
                )
            seen.add(hit.chunk_id)
            used.add(hit.rank)
            scores[hit.chunk_id] = scores.get(hit.chunk_id, Fraction(0)) + weight / (
                k + hit.rank
            )
            found_by.setdefault(hit.chunk_id, []).append(name)
            if hit.chunk_id not in best_rank or hit.rank < best_rank[hit.chunk_id]:
                best_rank[hit.chunk_id] = hit.rank

    # Highest score first; then the best single position any retriever gave it,
    # because a genuine tie between "third for both" and "first for one, fifth
    # for the other" is better broken by the strongest single opinion than by
    # the alphabet; then the chunk_id, so that the order is total and nothing
    # is left to chance.
    order = sorted(
        scores,
        key=lambda chunk_id: (-scores[chunk_id], best_rank[chunk_id], chunk_id),
    )
    if limit is not None:
        order = order[: max(0, limit)]
    return [
        Fused(chunk_id=chunk_id, rank=position, found_by=tuple(found_by[chunk_id]))
        for position, chunk_id in enumerate(order, start=1)
    ]


def search(
    query: str,
    limit: int,
    retrievers: Iterable[str] | None = None,
    weights: Mapping[str, float] | None = None,
) -> dict:
    """Ask every registered retriever, then fuse what comes back.

    The function 4.6 serves, so that the endpoint is a mapping onto the wire
    and not the place retrieval is orchestrated.

    EACH RETRIEVER IS ASKED FOR `limit`, NOT FOR limit/n. Fusion needs depth to
    do anything: two retrievers each asked for four, agreeing on nothing, give
    eight passages fused from two lists of four, and a passage ranked fifth by
    both -- the strongest possible signal short of unanimity at the top -- is
    invisible. Asking each for the full limit costs one query per retriever and
    is what makes the fused order mean something.

    A FAILED RETRIEVER IS NAMED, NEVER SWALLOWED. `failed` carries the reason
    for each one that raised, so 4.6 can say so in the response: a fused list
    missing the vector side is a worse answer that looks exactly like a normal
    one, and this project has already paid for a result that looked right
    because nothing had run. When EVERY retriever fails there is no answer at
    all and it raises, because an empty list would mean "nothing matched".
    """
    available = registered()
    if retrievers is None:
        wanted = sorted(available)
    else:
        wanted = sorted(set(retrievers))
        unknown = [name for name in wanted if name not in available]
        if unknown:
            raise RetrieverError(
                f"No retriever named {', '.join(repr(n) for n in unknown)}. "
                f"Registered: {', '.join(sorted(available)) or 'none'}."
            )
    if not wanted:
        raise RetrieverError("No retriever is registered, so nothing can be searched.")

    ranked: dict[str, Sequence[Hit]] = {}
    failed: dict[str, str] = {}
    for name in wanted:
        try:
            ranked[name] = available[name].search(query, limit)
        except RetrieverError as exc:
            failed[name] = str(exc)

    if not ranked:
        raise RetrieverError(
            "Every retriever failed, so this is not an empty result: "
            + "; ".join(f"{name}: {reason}" for name, reason in sorted(failed.items()))
        )

    return {
        "query": query,
        "retrievers": sorted(ranked),
        "failed": failed,
        "passages": fuse(ranked, weights=weights, limit=limit),
    }
