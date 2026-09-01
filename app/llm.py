"""A thin client for Ollama's /api/chat endpoint.

Deliberately stdlib only. The official `ollama` package is fine, but pinning a
client library against a fast-moving local server is how you end up with a
version mismatch you cannot debug. This is one POST to one endpoint, and the
payload shape is visible right here.

Docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.config import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_THINK, OLLAMA_TIMEOUT


class LlmError(Exception):
    """The model server could not be reached, or refused the request."""


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
    think: bool | None = None,
    temperature: float = 0.0,
    timeout: int | None = None,
) -> dict:
    """One round trip to the model. Returns the raw response dict.

    The interesting part of the response is response["message"], which is either
    a normal assistant reply with "content", or a request to run tools carrying
    "tool_calls".
    """
    payload: dict[str, Any] = {
        "model": model or OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": OLLAMA_THINK if think is None else think,
        "options": {"temperature": temperature},
    }
    if tools:
        payload["tools"] = tools

    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout or OLLAMA_TIMEOUT) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LlmError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LlmError(
            f"Cannot reach Ollama at {OLLAMA_HOST}. Is it running? ({exc.reason})"
        ) from exc
    except TimeoutError as exc:
        raise LlmError(
            f"Ollama did not answer within {timeout or OLLAMA_TIMEOUT}s."
        ) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LlmError(f"Ollama returned something that is not JSON: {body[:300]}") from exc
