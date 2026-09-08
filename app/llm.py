"""A thin client for an OpenAI-compatible /v1/chat/completions endpoint.

Step 3.1 of the architecture plan: this used to speak Ollama's native
/api/chat. It now speaks the OpenAI shape that vLLM (production) and Ollama's
own /v1 shim (dev laptop) both serve, so swapping backends is a base URL in
config, not a rewrite here.

The function signature and the shape of what chat() returns are unchanged on
purpose: app/agent.py reads response["message"]["content"] and
response["message"]["tool_calls"] and nothing else, and every existing test
mocks chat() directly (see tests/test_agent.py's `scripted()`), so neither
needed to change for this step. See docs/plans/step-03-model-gateway.md for
the capability probe this was built against.

Deliberately stdlib only, matching the reasoning that was already here: a
client library pinned against a fast-moving local server is a version
mismatch waiting to happen, and this is one POST to one endpoint.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator

from app.config import LLM_BASE_URL, LLM_MODEL, LLM_THINK, LLM_TIMEOUT


class LlmError(Exception):
    """The model server could not be reached, or refused the request."""


def _build_payload(
    messages: list[dict[str, Any]],
    tools: list[dict] | None,
    model: str | None,
    think: bool | None,
    temperature: float,
    stream: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        # Not a standard OpenAI field. vLLM forwards extra top-level fields
        # into the model's chat template, and Qwen3's template reads this one
        # to turn its <think> block off. Confirmed against this build in
        # Step 3.0 -- see docs/plans/step-03-model-gateway.md, Finding 2.
        "chat_template_kwargs": {"enable_thinking": LLM_THINK if think is None else think},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _post(payload: dict[str, Any], timeout: int) -> Any:
    request = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LlmError(f"{LLM_BASE_URL} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LlmError(
            f"Cannot reach the model server at {LLM_BASE_URL}. Is it running? ({exc.reason})"
        ) from exc
    except TimeoutError as exc:
        raise LlmError(f"The model server did not answer within {timeout}s.") from exc


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
    think: bool | None = None,
    temperature: float = 0.0,
    timeout: int | None = None,
) -> dict:
    """One round trip to the model. Returns {"message": ..., "usage": ...}.

    "message" is what app/agent.py actually reads: either a normal assistant
    reply with "content", or a request to run tools carrying "tool_calls".
    Tool call arguments arrive as a JSON string here (that is what vLLM
    sends, per Step 3.0's probe) -- app/agent.py's _coerce_arguments already
    parses either a string or a dict, so this is passed through unchanged
    rather than pre-parsed.
    """
    payload = _build_payload(messages, tools, model, think, temperature, stream=False)
    response = _post(payload, timeout or LLM_TIMEOUT)
    body = response.read().decode("utf-8")
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LlmError(f"The model server returned something that is not JSON: {body[:300]}") from exc

    choices = result.get("choices") or []
    if not choices:
        raise LlmError(f"The model server returned no choices: {body[:300]}")
    return {"message": choices[0].get("message") or {}, "usage": result.get("usage")}


def chat_stream(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
    think: bool | None = None,
    temperature: float = 0.0,
    timeout: int | None = None,
) -> Iterator[dict]:
    """Stream one round trip. Yields each parsed SSE data chunk as a dict.

    Not yet used by app/agent.py -- this exists for app/gateway.py's
    streaming endpoint (Step 3.2), added alongside chat() rather than
    inside it because the two have different callers and different
    failure shapes: a streamed response can fail mid-stream, after
    output has already reached the caller.
    """
    payload = _build_payload(messages, tools, model, think, temperature, stream=True)
    response = _post(payload, timeout or LLM_TIMEOUT)
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue
