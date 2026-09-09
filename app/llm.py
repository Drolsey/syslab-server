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


# Answered once per model and kept, because it cannot change without the
# server restarting -- max_model_len is an engine launch flag. Only successful
# answers are cached: a lookup that failed because the model server was still
# starting must be asked again, not remembered as "unknown" forever.
_windows: dict[str, int] = {}


def model_window(model: str) -> int | None:
    """How many tokens this model holds for one request, or None if unknown.

    Asked of the server rather than configured here, and that is the point.
    The window is set by --max-model-len in docker-compose.yml; a copy of it
    in .env would be a second place to remember, and the two would disagree
    on the first day somebody changed one. The server already publishes it on
    /v1/models, so this reads it from the thing that decides it.

    None means genuinely unknown -- the server is down, or does not publish
    max_model_len. Callers must not invent a number in that case: an assumed
    window is worse than no window, because it silently truncates answers on
    a server that would have accepted them.
    """
    cached = _windows.get(model)
    if cached is not None:
        return cached
    try:
        with urllib.request.urlopen(f"{LLM_BASE_URL}/models", timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    for entry in body.get("data") or []:
        length = entry.get("max_model_len")
        if entry.get("id") == model and isinstance(length, int) and length > 0:
            _windows[model] = length
            return length
    return None


# Estimating a token count without a tokenizer. Three characters per token is
# deliberately pessimistic -- English prose runs closer to four, but what
# actually fills these prompts is JSON: tool definitions, tool results, and
# rows out of a database, where punctuation and quoting push the ratio down.
# Over-estimating the prompt costs a slightly shorter answer. Under-estimating
# costs an HTTP 400 and no answer at all, so the error is taken in the
# direction that still works.
CHARS_PER_TOKEN = 3
# The chat template wraps every message in role markers the payload does not
# contain, and the estimate above cannot see them.
TEMPLATE_OVERHEAD_TOKENS = 256

# A reply needs somewhere to happen. Trimming only until the prompt fits
# leaves a window with nothing left to answer into, which is the exact failure
# this exists to stop: vLLM reports it as "you requested 0 output tokens and
# your prompt contains at least 8193 input tokens".
MIN_REPLY_TOKENS = 1024


def estimate_prompt_tokens(
    messages: list[dict[str, Any]], tools: list[dict] | None = None
) -> int:
    """Roughly how many tokens this prompt will occupy. Never an exact count.

    Shared by app/agent.py, which trims history against it, and app/gateway.py,
    which clamps max_tokens against it. One estimator on purpose: two would
    drift, and the two callers would then disagree about how full one window is.
    """
    material = json.dumps({"messages": messages or [], "tools": tools or []}, default=str)
    return len(material) // CHARS_PER_TOKEN + TEMPLATE_OVERHEAD_TOKENS


def _oldest_exchange_end(messages: list[dict[str, Any]]) -> int:
    """Index one past the oldest droppable exchange, or 0 if there is none.

    An exchange is one non-system message plus any tool results answering it.
    They move as a unit because a `tool` message whose assistant turn has been
    dropped is an orphan, and a conversation opening with an orphan tool result
    is rejected outright -- which would trade one HTTP 400 for a different one.

    Returns 0 when nothing can be dropped: messages[0] is the system prompt and
    stays, and the newest message is what is being asked right now, so at least
    one message must survive after the cut.
    """
    end = 2
    while end < len(messages) and messages[end].get("role") == "tool":
        end += 1
    return end if end < len(messages) else 0


def trim_to_window(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
) -> int:
    """Drop oldest exchanges, in place, until the prompt leaves room to reply.

    Returns how many messages were dropped. Zero means it already fitted, or
    that nothing could be done about it.

    Nothing trimmed conversation history before this, and a full window was
    terminal: the failure repeats on every following message, because each one
    is larger than the one that just failed, so the only recovery was starting
    a new conversation. Dropping the oldest turns keeps the newest ones, which
    are the ones the next answer is actually built on.

    Two cases are deliberately left alone, matching the reasoning the output
    clamp in app/gateway.py already uses:

    - No window known. An assumed window silently discards conversation on a
      server that would have accepted all of it.
    - It still does not fit with nothing left to drop -- one enormous message,
      or tool schemas that fill the window on their own. That is a real
      failure and the model server states it exactly; guessing here would
      replace a precise error with a vaguer one.

    The caller is expected to say that this happened. Trimming silently is the
    one option this file's callers have consistently argued against: the
    conversation would keep working while quietly forgetting, and nobody would
    know which answer was the first one built on less than it appeared to be.
    """
    window = model_window(model or LLM_MODEL)
    if window is None:
        return 0
    dropped = 0
    while estimate_prompt_tokens(messages, tools) + MIN_REPLY_TOKENS > window:
        end = _oldest_exchange_end(messages)
        if not end:
            break
        del messages[1:end]
        dropped += end - 1
    return dropped


def raw_request(payload: dict[str, Any], timeout: int | None = None):
    """POST an arbitrary OpenAI-shaped payload, unmodified, and return the open response.

    For app/gateway.py, which forwards a caller's own request body (built by
    a real OpenAI client, carrying fields chat() never needed to know about)
    rather than app/agent.py's narrower, reshaped {"message": ...} contract.
    The caller reads and closes the response; this only opens the connection.
    """
    return _post(payload, timeout or LLM_TIMEOUT)


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
