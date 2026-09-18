"""A thin client for an OpenAI-compatible /v1/embeddings endpoint.

The embedding-role counterpart to app/llm.py, and deliberately the same
shape: stdlib only, one function, one exception type, no retry or batching
logic of its own. A client library pinned against a fast-moving local
server is a version mismatch waiting to happen, and this is one POST to
one endpoint -- the same reasoning app/llm.py's own docstring already gives.

BATCHING IS THE CALLER'S JOB, NOT THIS MODULE'S. `embed()` sends exactly the
list it is given in one request. `scripts/bench_embeddings.py` measured
batches of 32 capturing most of the throughput gain a 512-token chunk gets
from batching at all (docs/models.md, Step 5.1) -- app/vectors.py's producer
is what knows it is embedding chunks and picks that number; this module has
no opinion about what a caller is embedding.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.config import EMBED_BASE_URL, EMBED_MODEL, EMBED_TIMEOUT


class EmbedError(Exception):
    """The embedding server was reached and refused the request, or answered
    with something unusable -- a per-call fault: this specific input, this
    specific response. Not raised for an unreachable server; see
    EmbedUnavailable for that."""


class EmbedUnavailable(EmbedError):
    """The embedding server could not be reached at all -- connection
    refused, DNS failure, timeout. An ENVIRONMENT fault, not a data fault:
    the same distinction app/producers.py's _reader() already draws between
    a missing parser library and one damaged file. A subclass of EmbedError
    rather than a sibling, so a caller that only wants "something went
    wrong" can still catch EmbedError and get both; app/vectors.py's
    producer is the caller that needs the finer distinction, to avoid
    recording a whole outage as one FAILED row per document rather than a
    single ProducerUnavailable report."""


def embed(
    texts: list[str],
    model: str | None = None,
    timeout: int | None = None,
    truncate_prompt_tokens: int | None = None,
) -> list[list[float]]:
    """One vector per text, in the same order texts were given.

    `truncate_prompt_tokens` exists because vLLM does NOT truncate an
    over-length input silently by default -- it hard-errors the whole batch
    with a 400 (confirmed live against BAAI/bge-base-en-v1.5's 512-token
    limit during the Step 5.1 comparison, `scripts/bench_retrieval_candidates.py`).
    A caller whose embed role has a real ceiling tighter than
    `app/chunks.py`'s TARGET_TOKENS estimate should pass it; the default,
    None, sends no such field and lets an over-length input fail loudly,
    which is correct for a candidate assumed to have headroom.
    """
    if not texts:
        return []
    payload: dict[str, Any] = {"model": model or EMBED_MODEL, "input": texts}
    if truncate_prompt_tokens:
        payload["truncate_prompt_tokens"] = truncate_prompt_tokens
    request = urllib.request.Request(
        f"{EMBED_BASE_URL}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or EMBED_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # The server was reached and answered -- a refused request, not an
        # outage. EmbedError, not EmbedUnavailable: app/vectors.py's producer
        # must record this per document, the same way a malformed file
        # fails only itself and not the whole producer.
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise EmbedError(f"{EMBED_BASE_URL} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        # HTTPError is also a URLError in Python's hierarchy, so this must
        # come second -- it is the case that did NOT match HTTPError above,
        # i.e. the connection itself failed (refused, DNS, unreachable host).
        raise EmbedUnavailable(
            f"Cannot reach the embedding server at {EMBED_BASE_URL}. Is it running? ({exc.reason})"
        ) from exc
    except TimeoutError as exc:
        raise EmbedUnavailable(
            f"The embedding server did not answer within {timeout or EMBED_TIMEOUT}s."
        ) from exc

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EmbedError(f"The embedding server returned something that is not JSON: {raw[:300]}") from exc

    data = body.get("data")
    if not data:
        raise EmbedError(f"The embedding server returned no data: {str(body)[:300]}")
    # data[i].index says which input it answers. Not assumed to match request
    # order -- vLLM has returned batches out of order under load before now
    # (docs/models.md), so this sorts on it rather than trusting response order.
    ordered = sorted(data, key=lambda row: row["index"])
    return [row["embedding"] for row in ordered]
