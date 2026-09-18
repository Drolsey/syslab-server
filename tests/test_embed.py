"""app/embed.py: the OpenAI-compatible /v1/embeddings client, and specifically
the distinction added for Step 5.4 between a server that refused a request
(EmbedError) and a server that could not be reached at all (EmbedUnavailable,
a subclass). app/vectors.py's producer depends on telling these apart --
tested separately, in tests/test_vectors.py -- so a bug here would silently
break the fault-isolation that exists to prevent one embedding outage from
becoming one FAILED manifest row per document.

No live server anywhere in this file: urllib.request.urlopen is replaced
directly, one layer below where every other test in this project mocks
(app.embed.embed itself, not urlopen underneath it).
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from app import embed


class FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def embeddings_body(count: int = 1, dimension: int = 3) -> dict:
    return {
        "data": [
            {"index": i, "embedding": [float(i)] * dimension}
            for i in range(count)
        ]
    }


# --------------------------------------------------------------------------
# the happy path, so the mocking seam itself is trusted before testing failures
# --------------------------------------------------------------------------

def test_embed_returns_vectors_in_request_order(monkeypatch):
    monkeypatch.setattr(
        embed.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(embeddings_body(count=2)),
    )

    result = embed.embed(["a", "b"])

    assert result == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]


def test_embed_sorts_by_index_even_if_the_server_answers_out_of_order(monkeypatch):
    """docs/models.md records vLLM returning batches out of order under load;
    this is the same defence app/embed.py already carries, checked directly."""
    out_of_order = {"data": [
        {"index": 1, "embedding": [1.0]},
        {"index": 0, "embedding": [0.0]},
    ]}
    monkeypatch.setattr(
        embed.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(out_of_order),
    )

    assert embed.embed(["a", "b"]) == [[0.0], [1.0]]


def test_no_texts_makes_no_request(monkeypatch):
    def explode(request, timeout=None):
        raise AssertionError("embed() with no texts should never call urlopen")
    monkeypatch.setattr(embed.urllib.request, "urlopen", explode)

    assert embed.embed([]) == []


# --------------------------------------------------------------------------
# the distinction Step 5.4 added: unreachable server vs. refused request
# --------------------------------------------------------------------------

def test_a_refused_request_is_an_embederror_not_unavailable(monkeypatch):
    """The server was reached and said no -- e.g. a malformed or over-length
    input. A per-call fault: app/vectors.py's producer must let this fail
    only the document being embedded, not report a whole-service outage.
    """
    def raise_http_error(request, timeout=None):
        raise urllib.error.HTTPError(
            url="http://embed.example/v1/embeddings", code=400,
            msg="Bad Request", hdrs=None,
            fp=__import__("io").BytesIO(b'{"error": "bad input"}'),
        )
    monkeypatch.setattr(embed.urllib.request, "urlopen", raise_http_error)

    with pytest.raises(embed.EmbedError) as excinfo:
        embed.embed(["too long"])

    assert not isinstance(excinfo.value, embed.EmbedUnavailable)
    assert "400" in str(excinfo.value)


def test_a_connection_refusal_is_embedunavailable(monkeypatch):
    """The server itself could not be reached -- an environment fault, the
    same shape as a missing parser library. Must be the SUBCLASS so a
    caller that only checks EmbedError still catches it too.
    """
    def raise_url_error(request, timeout=None):
        raise urllib.error.URLError("Connection refused")
    monkeypatch.setattr(embed.urllib.request, "urlopen", raise_url_error)

    with pytest.raises(embed.EmbedUnavailable):
        embed.embed(["anything"])


def test_a_timeout_is_also_embedunavailable(monkeypatch):
    def raise_timeout(request, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(embed.urllib.request, "urlopen", raise_timeout)

    with pytest.raises(embed.EmbedUnavailable):
        embed.embed(["anything"])


def test_embedunavailable_is_still_an_embederror(monkeypatch):
    """A caller that only wants 'something went wrong' (app/vectors.py's
    Vector.search(), which turns any EmbedError into RetrieverError
    uniformly -- see docs/plans/step-05-embeddings.md's 5.4 review, "already
    correct, no change needed") must not have to know the subclass exists.
    """
    assert issubclass(embed.EmbedUnavailable, embed.EmbedError)


def test_a_non_json_response_is_an_embederror_not_unavailable(monkeypatch):
    """The server answered -- badly -- which is not the same fault as not
    answering at all."""
    class BadResponse(FakeResponse):
        def read(self):
            return b"not json"
    monkeypatch.setattr(
        embed.urllib.request, "urlopen",
        lambda request, timeout=None: BadResponse({}),
    )

    with pytest.raises(embed.EmbedError) as excinfo:
        embed.embed(["a"])
    assert not isinstance(excinfo.value, embed.EmbedUnavailable)


def test_no_data_in_the_response_is_an_embederror(monkeypatch):
    monkeypatch.setattr(
        embed.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse({"data": []}),
    )

    with pytest.raises(embed.EmbedError):
        embed.embed(["a"])
