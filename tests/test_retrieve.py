"""Reciprocal Rank Fusion, Step 4.5.

THE HEADLINE TEST IS THAT NOTHING HAPPENS. One retriever is registered today,
and fusion of a single ranked list is that list in the same order. That is
asserted directly here rather than reasoned about from the formula, because it
is the property every number in scripts/check_retrieval.py depends on for this
sub-step: 4.5's measurements must come out IDENTICAL to 4.4's, and an RRF that
quietly reorders a single list would show up as a small unexplained improvement
that looks like progress.

The rest of the file tests the machinery Step 5 will turn on, on fixtures made
of plain `Hit` lists rather than of documents. Fusion does not know what a
passage is, has no index behind it and opens no file -- so the tests about it
should not need one either, and a fusion test that needed a corpus would be
testing the corpus.

Verified the way 4.3's and 4.4's were: each property broken on purpose and the
test watched to fail. Noted below where the break found something reading had
not.
"""

from __future__ import annotations

import pytest

from app import retrieve


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is module-level state, so put it back.

    app/passages.py registers `keyword` at import, and a test that leaves a
    fake retriever behind changes what an unrelated test in another file sees
    from retrieve.names(). test_passages.py asserts on exactly that list.
    """
    before = retrieve.registered()
    yield
    for name in list(retrieve.registered()):
        retrieve.unregister(name)
    for retriever in before.values():
        retrieve.register(retriever)


def hits(*chunk_ids: str) -> list[retrieve.Hit]:
    """A ranked list, best first, ranks 1..n -- what a retriever returns."""
    return [
        retrieve.Hit(chunk_id=chunk_id, rank=position)
        for position, chunk_id in enumerate(chunk_ids, start=1)
    ]


class Fake:
    """A retriever that returns what it was told to, and remembers the asking."""

    def __init__(self, name: str, results: list[retrieve.Hit], fails: str = ""):
        self.name = name
        self.results = results
        self.fails = fails
        self.asked: list[tuple[str, int]] = []

    def search(self, query: str, limit: int) -> list[retrieve.Hit]:
        self.asked.append((query, limit))
        if self.fails:
            raise retrieve.RetrieverError(self.fails)
        return self.results[:limit]


# --------------------------------------------------------------------------
# fusing one list does nothing, which is this sub-step's whole gate
# --------------------------------------------------------------------------

def test_fusing_one_list_returns_that_list_in_that_order():
    """4.5's gate, asserted and not derived.

    Twelve entries rather than three: 1/(60 + rank) is a shallow curve, and a
    comparison bug that swapped adjacent entries would still be arithmetically
    ordered across a short list by luck.
    """
    ordered = [f"contract.pdf#{n}" for n in range(12)]

    fused = retrieve.fuse({"keyword": hits(*ordered)})

    assert [f.chunk_id for f in fused] == ordered
    assert [f.rank for f in fused] == list(range(1, 13))


def test_fusing_one_list_does_not_depend_on_the_chunk_ids():
    """The break that found something.

    The first tiebreak written here sorted by score and then by chunk_id, and
    every test above passed -- because `hits()` generated ids in the same order
    as the ranks, so the alphabet agreed with the answer. Reversed ids, where
    the two disagree, is what caught it. The rank decides; the id decides
    nothing unless the scores are genuinely equal.
    """
    ordered = [f"z.pdf#{n}" for n in range(9, 0, -1)]

    fused = retrieve.fuse({"keyword": hits(*ordered)})

    assert [f.chunk_id for f in fused] == ordered


def test_one_list_names_one_retriever_in_found_by():
    fused = retrieve.fuse({"keyword": hits("a.pdf#0", "b.pdf#3")})

    assert [f.found_by for f in fused] == [("keyword",), ("keyword",)]


def test_fusing_nothing_is_empty_and_not_an_error():
    """No passage matched is an answer. It comes back as an empty list."""
    assert retrieve.fuse({}) == []
    assert retrieve.fuse({"keyword": []}) == []


# --------------------------------------------------------------------------
# what fusion is actually for, and Step 5 turns on
# --------------------------------------------------------------------------

def test_a_passage_both_retrievers_found_beats_one_either_found_alone():
    """The case hybrid retrieval exists for, in one assertion.

    `shared` is SECOND on both lists and still wins, beating two passages that
    are first on one list each. That is the whole argument for fusion over
    "one wins, the other is a fallback": agreement is worth more than any
    single retriever's confidence.
    """
    fused = retrieve.fuse({
        "keyword": hits("keyword_best.pdf#0", "shared.pdf#1"),
        "vector": hits("vector_best.pdf#0", "shared.pdf#1"),
    })

    assert fused[0].chunk_id == "shared.pdf#1"
    assert fused[0].found_by == ("keyword", "vector")


def test_a_retriever_that_found_nothing_changes_nothing():
    """Absence is silence. The strongest form of it: silence is free.

    An empty second list must produce a byte-identical fused result, `found_by`
    included. The wrong implementation of RRF sums over EVERY retriever and
    substitutes a sentinel rank for a passage one of them missed, and this is
    where that starts being visible.
    """
    alone = retrieve.fuse({"keyword": hits("a.pdf#0", "b.pdf#0", "c.pdf#0")})
    with_silent = retrieve.fuse({
        "keyword": hits("a.pdf#0", "b.pdf#0", "c.pdf#0"),
        "vector": [],
    })

    assert alone == with_silent


def test_adding_a_retriever_cannot_reorder_the_passages_it_did_not_rank():
    """Because otherwise a second retriever is a regression nobody can explain.

    `vector` ranks one passage none of the others mention. Wherever that
    passage lands, the three `keyword` found must stay in the order `keyword`
    put them: they are all equally absent from `vector`, and equal absence
    cannot be a reason to reorder them.

    This is the assertion the first version of this test lacked. It checked
    that a passage was still "in the list somewhere" and that another was
    "within the first two", which a demotion survives comfortably.
    """
    alone = retrieve.fuse({"keyword": hits("a.pdf#0", "b.pdf#0", "c.pdf#0")})
    with_second = retrieve.fuse({
        "keyword": hits("a.pdf#0", "b.pdf#0", "c.pdf#0"),
        "vector": hits("d.pdf#0"),
    })

    kept = [f.chunk_id for f in with_second if f.chunk_id != "d.pdf#0"]
    assert kept == [f.chunk_id for f in alone]
    assert "d.pdf#0" in [f.chunk_id for f in with_second]


def test_found_by_reports_only_the_retrievers_that_ranked_it():
    fused = {
        f.chunk_id: f.found_by
        for f in retrieve.fuse({
            "keyword": hits("shared.pdf#0", "keyword_only.pdf#0"),
            "vector": hits("shared.pdf#0", "vector_only.pdf#0"),
        })
    }

    assert fused["shared.pdf#0"] == ("keyword", "vector")
    assert fused["keyword_only.pdf#0"] == ("keyword",)
    assert fused["vector_only.pdf#0"] == ("vector",)


def test_the_limit_cuts_the_fused_list_and_not_each_input():
    fused = retrieve.fuse(
        {"keyword": hits("a.pdf#0", "b.pdf#0"), "vector": hits("c.pdf#0", "d.pdf#0")},
        limit=3,
    )

    assert len(fused) == 3
    assert [f.rank for f in fused] == [1, 2, 3]


# --------------------------------------------------------------------------
# the order cannot depend on anything invisible
# --------------------------------------------------------------------------

def test_the_result_does_not_depend_on_which_retriever_was_registered_first():
    """Which module imported first must not change an answer.

    This is why the arithmetic is exact. Summing 1/(60 + rank) as floats makes
    the total depend on the order the terms were added, and that order is the
    registration order, so two tied passages would sort by whichever sum
    happened to round up -- and the answer would move the day an unrelated
    import moved.
    """
    one = {"keyword": hits("a.pdf#0", "b.pdf#0"), "vector": hits("b.pdf#0", "a.pdf#0")}
    other = {"vector": hits("b.pdf#0", "a.pdf#0"), "keyword": hits("a.pdf#0", "b.pdf#0")}

    assert retrieve.fuse(one) == retrieve.fuse(other)


def test_a_genuine_tie_is_broken_by_the_best_single_rank():
    """An EXACT tie, and the stronger single opinion takes it.

    Constructing one takes care, and the first version of this test did not:
    it used two plausible-looking rank patterns whose scores differed in the
    fourth decimal, asserted the ordering the scores already gave, and would
    have passed with the tiebreak deleted entirely. A tie has to be built, not
    hoped for.

    Built here from a weight that is exact in binary, so Fraction(0.5) is 1/2
    and not 1/2-ish: `z` scores 0.5/(60 + 1) and `a` scores 1/(60 + 62), and
    both are exactly 1/122. `z` was ranked FIRST by something and `a` was
    ranked sixty-second, so `z` wins -- and the ids are chosen to say the
    opposite, which is what makes this test about the rank rather than the
    alphabet.
    """
    fused = retrieve.fuse(
        {"sure": hits("z.pdf#0"), "deep": hits(*[f"x{n}.pdf#0" for n in range(61)], "a.pdf#0")},
        weights={"sure": 0.5},
    )
    order = [f.chunk_id for f in fused]

    assert order.index("z.pdf#0") < order.index("a.pdf#0")


def test_two_passages_with_identical_scores_and_ranks_sort_by_id_and_stay_there():
    """The last resort, and it exists so the order is total.

    Two passages that no retriever can tell apart must still come back in the
    same order every time, or the same query answers differently on Tuesday and
    every cached citation moves.
    """
    first = retrieve.fuse({"keyword": hits("b.pdf#0"), "vector": hits("a.pdf#0")})
    again = retrieve.fuse({"vector": hits("a.pdf#0"), "keyword": hits("b.pdf#0")})

    assert [f.chunk_id for f in first] == ["a.pdf#0", "b.pdf#0"]
    assert first == again


# --------------------------------------------------------------------------
# what fuse() refuses, and why each refusal is not fussiness
# --------------------------------------------------------------------------

def test_a_retriever_listing_a_passage_twice_is_refused():
    """Deduplicating it quietly is how the bug survives to the next retriever.

    A repeat counts twice and lands the passage at the top of the fused list,
    and nothing about the output looks wrong.
    """
    with pytest.raises(retrieve.RetrieverError, match="twice"):
        retrieve.fuse({"keyword": [
            retrieve.Hit(chunk_id="a.pdf#0", rank=1),
            retrieve.Hit(chunk_id="a.pdf#0", rank=2),
        ]})


def test_two_passages_at_the_same_rank_are_refused():
    with pytest.raises(retrieve.RetrieverError, match="rank"):
        retrieve.fuse({"keyword": [
            retrieve.Hit(chunk_id="a.pdf#0", rank=1),
            retrieve.Hit(chunk_id="b.pdf#0", rank=1),
        ]})


def test_a_k_below_one_is_refused():
    """At k = 0 one retriever's first place outranks every other combined."""
    with pytest.raises(retrieve.RetrieverError, match="smoothing"):
        retrieve.fuse({"keyword": hits("a.pdf#0")}, k=0)


def test_a_negative_weight_is_refused():
    """It would rank a passage below one that nothing found at all."""
    with pytest.raises(retrieve.RetrieverError, match="negative"):
        retrieve.fuse({"keyword": hits("a.pdf#0")}, weights={"keyword": -1})


def test_a_weight_changes_the_order_and_one_is_the_default():
    heavier = retrieve.fuse(
        {"keyword": hits("k.pdf#0"), "vector": hits("v.pdf#0")},
        weights={"vector": 3},
    )
    default = retrieve.fuse({"keyword": hits("k.pdf#0"), "vector": hits("v.pdf#0")})

    assert heavier[0].chunk_id == "v.pdf#0"
    # Unweighted, the two are tied and the id decides -- so k wins, and that is
    # the evidence the weight did the work above rather than the alphabet.
    assert default[0].chunk_id == "k.pdf#0"


def test_a_weight_of_zero_contributes_nothing_rather_than_removing_the_hits():
    """Documented because it surprises: zero is not "do not ask".

    A passage only a zero-weighted retriever found still appears, at the bottom.
    Taking a retriever out of an answer is done by not asking it.
    """
    fused = retrieve.fuse(
        {"keyword": hits("k.pdf#0"), "muted": hits("m.pdf#0")},
        weights={"muted": 0},
    )

    assert [f.chunk_id for f in fused] == ["k.pdf#0", "m.pdf#0"]


# --------------------------------------------------------------------------
# search(): the function 4.6 serves
# --------------------------------------------------------------------------

def test_search_fuses_every_registered_retriever():
    retrieve.register(Fake("keyword", hits("shared.pdf#0", "k.pdf#0")))
    retrieve.register(Fake("vector", hits("shared.pdf#0", "v.pdf#0")))

    found = retrieve.search("payment terms", limit=5)

    assert found["retrievers"] == ["keyword", "vector"]
    assert found["failed"] == {}
    assert found["passages"][0].chunk_id == "shared.pdf#0"
    assert found["passages"][0].found_by == ("keyword", "vector")


def test_each_retriever_is_asked_for_the_whole_limit_and_not_a_share_of_it():
    """Fusion needs depth or it has nothing to agree about.

    Two retrievers asked for two each, agreeing on nothing, produce four
    passages fused from two lists of two -- and a passage ranked third by both,
    which is a strong signal, is not even visible.
    """
    keyword = Fake("keyword", hits("a.pdf#0", "b.pdf#0"))
    vector = Fake("vector", hits("c.pdf#0", "d.pdf#0"))
    retrieve.register(keyword)
    retrieve.register(vector)

    retrieve.search("anything", limit=8)

    assert keyword.asked == [("anything", 8)]
    assert vector.asked == [("anything", 8)]


def test_one_failed_retriever_is_named_in_the_answer_rather_than_swallowed():
    """A fused list missing a retriever looks exactly like a normal one.

    So it is reported. 4.6 puts it on the wire, and this project has already
    paid once for a result that looked right because nothing had run.
    """
    retrieve.register(Fake("keyword", hits("a.pdf#0")))
    retrieve.register(Fake("vector", [], fails="the embedding model is not loaded"))

    found = retrieve.search("anything", limit=4)

    assert found["retrievers"] == ["keyword"]
    assert found["failed"] == {"vector": "the embedding model is not loaded"}
    assert [f.chunk_id for f in found["passages"]] == ["a.pdf#0"]


def test_every_retriever_failing_raises_rather_than_returning_nothing():
    """Because an empty list already means something else: nothing matched."""
    retrieve.register(Fake("keyword", [], fails="the index is missing"))

    with pytest.raises(retrieve.RetrieverError, match="Every retriever failed"):
        retrieve.search("anything", limit=4)


def test_asking_for_a_retriever_that_does_not_exist_says_which_ones_do():
    retrieve.register(Fake("keyword", hits("a.pdf#0")))

    with pytest.raises(retrieve.RetrieverError, match="vector"):
        retrieve.search("anything", limit=4, retrievers=["keyword", "vector"])


def test_asking_for_a_subset_runs_only_that_subset():
    keyword = Fake("keyword", hits("a.pdf#0"))
    vector = Fake("vector", hits("b.pdf#0"))
    retrieve.register(keyword)
    retrieve.register(vector)

    found = retrieve.search("anything", limit=4, retrievers=["keyword"])

    assert found["retrievers"] == ["keyword"]
    assert vector.asked == []


def test_searching_with_nothing_registered_refuses_rather_than_answering_empty():
    for name in list(retrieve.registered()):
        retrieve.unregister(name)

    with pytest.raises(retrieve.RetrieverError, match="No retriever is registered"):
        retrieve.search("anything", limit=4)
