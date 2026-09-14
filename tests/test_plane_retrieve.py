"""POST /api/v1/retrieve, Step 4.6.

The three properties this sub-step exists to prove, from the plan's own gate:

  1. THE BUDGET IS RESPECTED AND `truncated` IS HONEST. Respected means a
     passage larger than what is left does not go in, not that the total is
     approximately right. Honest means `truncated` says "the budget stopped
     this" and never "that was all there was".

  2. `coverage` IS CORRECT AND PROVABLY SO. A query matching 31 passages and
     returning 8 has to say so, because decision 5.6's whole argument is that
     the difference between a wrong aggregate answer nobody can detect and one
     anybody can is those two numbers sitting beside each other. Proving it
     means asserting `matched` is NOT the returned count -- a coverage block
     that agrees with itself is what an invented one looks like.

  3. A REQUEST FOR ANOTHER TENANT'S SOURCE FILTERS TO NOTHING rather than
     erroring informatively. There is no code for this case, which is an
     argument and not a check, so it is checked.

The citation property is asserted end to end here as well: `start` and `end`
from the wire must index back into the text artifact and produce the `text`
that was returned. 4.3 gated that inside the producer; this is the same claim
after it has been through an index, a fusion, a token budget and a JSON
encoder.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, context, passages, plane, producers, search, tenancy, tools

TOKEN = "a-service-token-for-the-website-long-enough"

# Long enough to be several passages, and made of sentences a contract would
# have. The rare strings ("Meridian", "QT-4417") are what the source filter and
# the per-tenant tests key on, because a word that appears in both documents
# would make a leak look like a match.
CALIBRATION = (
    "On-site calibration of four sensor arrays by Rania Haddad. "
    "Total due 18,640 AED under purchase order QT-4417. "
    "Payment falls due thirty days from invoice date. "
)
STAFFING = (
    "Farah Nasser covers the northern region in October. "
    "Payment falls due thirty days from invoice date. "
)
OTHER_TENANT_TEXT = (
    "Confidential Thornbury settlement terms, payment within thirty days. "
)


@pytest.fixture()
def linked(tenant_storage, monkeypatch):
    """A real tenant with real passages, reachable as website:42.

    The tenant id is GENERATED rather than chosen, because that is what the
    plane resolves an alias to and what `data/<tenant>/` is then named. A
    fixture that picked its own id would be testing a path the application
    never builds.
    """
    monkeypatch.setattr(config, "RETRIEVAL_TOKENS", {TOKEN: "website"})
    connection = tenancy.connect()
    tenant = tenancy.create_tenant("Acme Ltd", connection=connection)
    tenancy.link_alias("website", "42", tenant["id"], connection=connection)
    connection.close()

    with context.use_tenant(tenant["id"]):
        tools.write_pdf("meridian_invoice.pdf", title="Invoice QT-4417",
                        body=CALIBRATION * 40)
        tools.write_pdf("staffing_note.pdf", title="Staffing", body=STAFFING * 40)
        search.rebuild()
        built = passages.rebuild()
    assert built["passages"] > 4, "the fixture must be several passages, not one"
    return tenant


@pytest.fixture()
def other_tenant(linked):
    """A second tenant, with a document whose words appear nowhere in the first."""
    connection = tenancy.connect()
    tenant = tenancy.create_tenant("Thornbury LLC", connection=connection)
    tenancy.link_alias("website", "99", tenant["id"], connection=connection)
    connection.close()

    with context.use_tenant(tenant["id"]):
        tools.write_pdf("thornbury_deed.pdf", title="Deed",
                        body=OTHER_TENANT_TEXT * 40)
        search.rebuild()
        passages.rebuild()
    return tenant


@pytest.fixture()
def client():
    """The real router, mounted on a bare app.

    Not app.main's application: this route's own dependency is what is under
    test, and main.py adds a session cookie check and middleware that would
    make a 401 here ambiguous between two different refusals.
    """
    app = FastAPI()
    app.include_router(plane.router)
    return TestClient(app, raise_server_exceptions=False)


def ask(client, tenant="42", token=TOKEN, **body):
    return client.post(
        "/api/v1/retrieve",
        json=body,
        headers={"Authorization": f"Bearer {token}", "X-Syslab-Tenant": tenant},
    )


# --------------------------------------------------------------------------
# a passage comes back, and it is citable
# --------------------------------------------------------------------------

def test_a_passage_comes_back_with_everything_needed_to_check_it(linked, client):
    answer = ask(client, query="sensor calibration Haddad", k=3)

    assert answer.status_code == 200
    body = answer.json()
    assert body["passages"], body
    first = body["passages"][0]
    assert first["source"] == "meridian_invoice.pdf"
    assert first["chunk_id"].startswith("meridian_invoice.pdf#")
    assert first["rank"] == 1
    assert first["found_by"] == ["keyword"]
    assert first["tokens"] > 0
    assert "calibration" in first["text"].lower()


def test_the_offsets_index_back_into_the_text_artifact(linked, client):
    """The whole reason a citation is checkable, asserted after the round trip.

    "Characters 4,096 to 4,608 of contract.pdf" can be verified by anyone
    holding the file. This asserts the numbers that left the server still do
    that -- through the index, the fusion, the budget and a JSON encoder.
    """
    body = ask(client, query="purchase order QT-4417", k=5).json()
    assert body["passages"]

    with context.use_tenant(linked["id"]):
        artifact = producers.text_of("meridian_invoice.pdf")

    assert artifact is not None
    for passage in body["passages"]:
        if passage["source"] != "meridian_invoice.pdf":
            continue
        assert artifact[passage["start"]:passage["end"]] == passage["text"]


def test_no_passage_carries_a_score(linked, client):
    """Section 6: a float labelled "score" invites thresholding.

    And it would mean something different the day a second retriever joins --
    1/61 is the best one retriever can give and 2/61 the best two can.
    """
    body = ask(client, query="payment falls due", k=5).json()

    assert body["passages"]
    for passage in body["passages"]:
        assert "score" not in passage


def test_the_retrievers_that_ran_are_named_and_none_are_unavailable(linked, client):
    body = ask(client, query="sensor calibration", k=2).json()

    assert body["retrievers"] == ["keyword"]
    assert body["retrievers_unavailable"] == {}


# --------------------------------------------------------------------------
# coverage: decision 5.6 made visible
# --------------------------------------------------------------------------

def test_coverage_reports_more_matched_than_returned_and_that_is_the_point(linked, client):
    """The assertion that makes the block worth having.

    `matched` must NOT equal `returned`. A coverage block computed from the
    rows that came back would agree with itself every time, which is exactly
    what an invented one looks like -- so the fixture asks for two passages out
    of a query that matches many.
    """
    body = ask(client, query="payment falls due thirty days", k=2).json()

    coverage = body["coverage"]
    assert coverage["returned"] == 2
    assert coverage["matched"] > coverage["returned"], coverage
    assert coverage["searched"] >= coverage["matched"]
    assert coverage["returned"] == len(body["passages"])


def test_coverage_searched_is_every_passage_in_scope(linked, client):
    with context.use_tenant(linked["id"]):
        indexed = passages.status()["passages"]

    body = ask(client, query="payment", k=1).json()

    assert body["coverage"]["searched"] == indexed


def test_coverage_follows_the_same_and_then_or_fallback_as_the_ranking(linked, client):
    """Otherwise coverage contradicts the list printed underneath it.

    Both the ranking and the counting try every term ANDed first and fall back
    to any term ORed. This query pairs a word the corpus has with one it does
    not, so the AND pass matches NOTHING and the OR pass answers. A coverage
    figure taken from the AND pass would report `matched: 0` above a response
    carrying real passages -- self-contradicting, and in the direction that
    makes an aggregate shortfall look like no shortfall at all.

    The gap this closes was found by breaking the fallback and watching every
    other coverage test carry on passing: they all use queries where the AND
    pass matches, so the fallback never ran.
    """
    body = ask(client, query="calibration zygomorphic", k=8).json()

    assert body["passages"], "the OR fallback should have found the calibration text"
    assert body["coverage"]["matched"] >= len(body["passages"])
    assert body["coverage"]["matched"] > 0


def test_what_this_means_says_sample_not_census_with_the_numbers(linked, client):
    body = ask(client, query="payment falls due thirty days", k=2).json()

    said = body["what_this_means"]
    assert "sample, not a census" in said
    assert f"{body['coverage']['returned']} of {body['coverage']['matched']}" in said
    assert "ALL of something" in said


def test_when_everything_matching_came_back_it_does_not_claim_a_shortfall(linked, client):
    """A response that cried "sample" while returning everything would teach a
    reader to ignore the sentence, which costs the sentence its only job."""
    body = ask(client, query="Haddad", k=50).json()

    assert body["coverage"]["returned"] == body["coverage"]["matched"]
    assert "All" in body["what_this_means"]
    assert "sample, not a census" not in body["what_this_means"]


def test_nothing_matching_is_a_200_that_says_why_rather_than_an_error(linked, client):
    answer = ask(client, query="zygomorphic bryophyte", k=8)

    assert answer.status_code == 200
    body = answer.json()
    assert body["passages"] == []
    assert body["coverage"]["matched"] == 0
    assert body["truncated"] is False
    # The honest caveat: keyword search failing is not evidence of absence.
    assert "paraphrase" in body["what_this_means"]


# --------------------------------------------------------------------------
# the token budget, respected rather than advisory
# --------------------------------------------------------------------------

def test_the_budget_is_respected_and_truncated_says_the_budget_did_it(linked, client):
    generous = ask(client, query="payment falls due thirty days", k=8).json()
    assert len(generous["passages"]) > 1, "need more than one passage to cut"

    budget = generous["passages"][0]["tokens"] + 1
    tight = ask(client, query="payment falls due thirty days", k=8,
                budget_tokens=budget).json()

    assert tight["tokens_returned"] <= budget
    assert len(tight["passages"]) < len(generous["passages"])
    assert tight["truncated"] is True
    assert "token budget" in tight["what_this_means"]


def test_tokens_returned_is_what_the_passages_actually_cost(linked, client):
    body = ask(client, query="payment falls due", k=5).json()

    assert body["passages"]
    assert body["tokens_returned"] == sum(p["tokens"] for p in body["passages"])


def test_tokens_returned_counts_what_shipped_and_not_what_was_considered(linked, client):
    """The gap the first version of the test above left open.

    Asking for five passages inside a 4,000-token budget fits all five, so
    "the cost of what shipped" and "the cost of everything considered" are the
    same number and a bug between them is invisible. This asks with a budget
    that cuts, where the two differ.
    """
    generous = ask(client, query="payment falls due thirty days", k=8).json()
    assert len(generous["passages"]) > 2

    budget = sum(p["tokens"] for p in generous["passages"][:2])
    tight = ask(client, query="payment falls due thirty days", k=8,
                budget_tokens=budget).json()

    assert tight["tokens_returned"] == sum(p["tokens"] for p in tight["passages"])
    assert tight["tokens_returned"] < generous["tokens_returned"]


def test_a_budget_that_fits_exactly_is_not_off_by_one(linked, client):
    """The boundary, which nothing else in this file touches.

    `>` and `>=` in fit_budget are indistinguishable until a passage fits the
    remaining budget EXACTLY, and then one of them silently drops a passage the
    caller paid for. So the budget here is the exact cost of the first two.
    """
    generous = ask(client, query="payment falls due thirty days", k=8).json()
    assert len(generous["passages"]) > 2

    budget = sum(p["tokens"] for p in generous["passages"][:2])
    body = ask(client, query="payment falls due thirty days", k=8,
               budget_tokens=budget).json()

    assert len(body["passages"]) == 2
    assert body["tokens_returned"] == budget
    # A third passage was available and did not fit, so the budget is what
    # stopped it -- which is the other half of the boundary.
    assert body["truncated"] is True


def test_truncated_is_false_when_k_and_not_the_budget_stopped_it(linked, client):
    """`truncated` must never mean "that was all there was".

    Here k is what limited the answer and the budget was never close, so a
    `true` would send a caller looking for a budget they do not need.
    """
    body = ask(client, query="payment falls due thirty days", k=1,
               budget_tokens=16384).json()

    assert len(body["passages"]) == 1
    assert body["truncated"] is False
    assert body["coverage"]["matched"] > 1


def test_a_passage_bigger_than_the_whole_budget_returns_nothing_and_explains(linked, client):
    """Strict, and the one case where `returned` is 0 while `matched` is not.

    Handing the passage over anyway would blow the budget of a caller who
    asked precisely so that would not happen. The response has to be
    distinguishable from "nothing matched", which is what `truncated` and the
    sentence are for.
    """
    body = ask(client, query="payment falls due thirty days", k=8,
               budget_tokens=1).json()

    assert body["passages"] == []
    assert body["tokens_returned"] == 0
    assert body["truncated"] is True
    assert body["coverage"]["matched"] > 0
    # `returned` is the count that SHIPPED, and this is the case that tells it
    # apart from `k`: eight were asked for and none came back. A block that
    # reported the request rather than the response would say 8 here.
    assert body["coverage"]["returned"] == 0
    assert "budget_tokens" in body["what_this_means"]


def test_ranks_are_contiguous_from_one_after_the_budget_has_cut(linked, client):
    generous = ask(client, query="payment falls due thirty days", k=8).json()
    budget = sum(p["tokens"] for p in generous["passages"][:2])

    body = ask(client, query="payment falls due thirty days", k=8,
               budget_tokens=budget).json()

    assert [p["rank"] for p in body["passages"]] == list(
        range(1, len(body["passages"]) + 1))


# --------------------------------------------------------------------------
# the source filter
# --------------------------------------------------------------------------

def test_a_source_filter_narrows_to_that_document(linked, client):
    body = ask(client, query="payment falls due thirty days", k=8,
               sources=["staffing_note.pdf"]).json()

    assert body["passages"]
    assert {p["source"] for p in body["passages"]} == {"staffing_note.pdf"}


def test_the_filter_narrows_the_coverage_counts_and_not_just_the_list(linked, client):
    """The reason the filter is SQL and not a list comprehension afterwards.

    Filtering after the fact leaves `matched` counting passages the caller
    excluded, so `coverage` would report a census of the wrong corpus -- and
    coverage is the one field decision 5.6 rests on.
    """
    everywhere = ask(client, query="payment falls due thirty days", k=8).json()
    narrowed = ask(client, query="payment falls due thirty days", k=8,
                   sources=["staffing_note.pdf"]).json()

    assert narrowed["coverage"]["searched"] < everywhere["coverage"]["searched"]
    assert narrowed["coverage"]["matched"] < everywhere["coverage"]["matched"]


def test_another_tenants_source_filters_to_nothing_rather_than_erroring(
        other_tenant, client):
    """4.6's third gated property, and the wording of the gate matters.

    NOT an informative error. "No such document" would confirm a filename to
    whoever guessed it, and this module's own 404 rule is the same rule. The
    filter runs inside the asking tenant's index, so another tenant's filename
    simply matches no row -- and the words of that document must not appear
    either.
    """
    answer = ask(client, query="payment within thirty days", k=8,
                 sources=["thornbury_deed.pdf"])

    assert answer.status_code == 200
    body = answer.json()
    assert body["passages"] == []
    assert body["coverage"]["searched"] == 0
    assert body["coverage"]["matched"] == 0
    assert "Thornbury" not in answer.text
    assert "settlement" not in answer.text


def test_each_tenant_sees_only_its_own_passages_for_the_same_query(
        other_tenant, client):
    """A leaked search result is a filename. A leaked passage is a paragraph.

    Asserted on the TEXT returned rather than on a count, for that reason.
    """
    ours = ask(client, tenant="42", query="payment thirty days", k=8).json()
    theirs = ask(client, tenant="99", query="payment thirty days", k=8).json()

    assert ours["passages"] and theirs["passages"]
    assert {p["source"] for p in ours["passages"]} <= {
        "meridian_invoice.pdf", "staffing_note.pdf"}
    assert {p["source"] for p in theirs["passages"]} == {"thornbury_deed.pdf"}
    assert all("Thornbury" not in p["text"] for p in ours["passages"])
    assert all("Haddad" not in p["text"] for p in theirs["passages"])


def test_an_empty_source_list_is_nothing_and_not_everything(linked, client):
    """The decision in passages._scope, at the wire.

    A caller that computed a filter and computed an empty one must not be
    answered with the whole corpus: of the two surprises, "nothing came back"
    costs a retry and "everything came back" spends a token budget on material
    the caller excluded.
    """
    body = ask(client, query="payment falls due", k=8, sources=[]).json()

    assert body["passages"] == []
    assert body["coverage"]["searched"] == 0


# --------------------------------------------------------------------------
# what the endpoint refuses, and what it tolerates
# --------------------------------------------------------------------------

def test_a_query_with_nothing_searchable_in_it_is_400(linked, client):
    """Distinct from nothing matching, which is a 200 with an empty list."""
    answer = ask(client, query="!!! ...", k=8)

    assert answer.status_code == 400
    assert "searchable" in answer.json()["detail"].lower()


@pytest.mark.parametrize("body", [
    {"k": 0},
    {"k": 999},
    {"budget_tokens": 0},
    {"budget_tokens": 10 ** 9},
    {"query": ""},
])
def test_the_bounds_are_enforced_rather_than_clamped_silently(linked, client, body):
    """422 rather than a quiet clamp.

    A caller who asked for 999 passages has misunderstood something, and
    answering 50 as though that were the question hides it. The ceilings exist
    because the caller has ~12,171 tokens for the whole conversation.
    """
    answer = ask(client, **{"query": "payment", **body})

    assert answer.status_code == 422


def test_an_unknown_field_is_ignored_rather_than_rejected(linked, client):
    """`extra="forbid"` would be a narrowing, and /api/v1 is frozen.

    The website deploys separately; the day it sends a field this version has
    not heard of, it must not start getting 422s from a server that could
    simply have answered.
    """
    answer = ask(client, query="sensor calibration", k=2, rerank=True)

    assert answer.status_code == 200
    assert answer.json()["passages"]


# --------------------------------------------------------------------------
# the response shape, which the frozen contract does NOT cover
# --------------------------------------------------------------------------
#
# docs/api/retrieval-v1.released.json freezes the REQUEST: which routes exist,
# which fields are required, their types, and that unknown fields are still
# accepted. It cannot freeze the response, because this endpoint returns a
# `dict` and FastAPI therefore emits an open object for the 200 -- and a
# response model that closed it would fight the freeze's own rule that a field
# may be ADDED, since pydantic strips what a model does not name.
#
# So the response shape is pinned HERE instead, and it has to be pinned
# somewhere: `found_by`, `coverage`, `truncated` and `what_this_means` are what
# section 6 of the plan calls load-bearing, and every one of them lives in the
# response. A freeze that quietly covered none of them would be worse than no
# freeze, because the file would look like it did.

RESPONSE_KEYS = {
    "query", "retrievers", "retrievers_unavailable", "passages", "coverage",
    "tokens_returned", "truncated", "what_this_means",
}
PASSAGE_KEYS = {
    "chunk_id", "source", "text", "start", "end", "tokens", "rank", "found_by",
}


def test_the_response_has_exactly_the_documented_fields(linked, client):
    """Exactly, not "at least". A removal is what breaks the website."""
    body = ask(client, query="sensor calibration", k=2).json()

    assert set(body) == RESPONSE_KEYS
    assert set(body["coverage"]) == {"searched", "matched", "returned"}
    assert body["passages"]
    for passage in body["passages"]:
        assert set(passage) == PASSAGE_KEYS


def test_the_shape_is_the_same_when_nothing_matched(linked, client):
    """Every field present on the empty path too.

    A caller reading `coverage` or `truncated` must not have to branch on
    whether anything came back -- an absent key and a zero are the same value
    to a careless reader, and only one of them is true.
    """
    body = ask(client, query="zygomorphic bryophyte", k=8).json()

    assert set(body) == RESPONSE_KEYS
    assert set(body["coverage"]) == {"searched", "matched", "returned"}


def test_a_query_needs_a_tenant_even_with_a_valid_token(linked, client):
    answer = client.post(
        "/api/v1/retrieve",
        json={"query": "payment"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert answer.status_code == 400
    assert "X-Syslab-Tenant" in answer.json()["detail"]


def test_an_unlinked_tenant_is_404_and_never_403(linked, client):
    answer = ask(client, tenant="404404", query="payment")

    assert answer.status_code == 404


def test_the_tenant_does_not_survive_the_request(linked, client):
    """The dependency's teardown, asserted through the real route.

    A tenant left in the ContextVar is read by whatever reuses the thread, and
    the next request would answer out of the wrong customer's folder.

    Asserted as "put back to what it was" rather than as "raises", which is
    what the first version of this test claimed and why it failed: the storage
    fixture is itself inside a tenant, so an empty ContextVar afterwards would
    have meant the teardown had cleared something it did not set. Restoring the
    previous value is the property, and it is the stronger one -- it fails both
    when the tenant leaks and when the teardown overreaches.
    """
    before = context.current_tenant()
    assert before != linked["id"]

    assert ask(client, query="sensor calibration").status_code == 200

    assert context.current_tenant() == before
