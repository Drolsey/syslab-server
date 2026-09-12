"""Step 4.4: what a retriever is, and how one registers.

WHAT IS HERE AND WHAT IS NOT
    The SEAM only: a `Hit`, a `Retriever` protocol, and a registry. There is no
    fusion in this file yet. Reciprocal Rank Fusion is 4.5, and it arrives as a
    function added here rather than as a module that has to be plumbed in.

    The file exists one sub-step early on purpose. `Hit` is the type every
    retriever returns, and a seam type that lives inside the first
    implementation is not a seam -- the day a vector retriever lands it would
    be importing its result type from the keyword module, which is the shape
    of the arrangement Step 2 spent five sub-steps ending.

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
from typing import Protocol, runtime_checkable


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
