"""Step 3.2: the inference plane. Auth, model aliases, rate limits -- no tenant.

Section 2 of the architecture plan draws the boundary this file lives inside:
"syslab-server never learns what a conversation is." This module must never
import app.context, app.tenancy, or call app.config.data_dir(), index_path()
or db.settings() -- scripts/check_gateway_isolation.py asserts that by
reading this file's source, the way scripts/check_isolation.py already
asserts the equivalent for tenant storage. A leaked GATEWAY_TOKEN should only
ever mean "someone used the GPU", never "someone read a customer's files".

Auth is deliberately its own token set (GATEWAY_TOKENS), separate from
APP_TOKEN and the tenant token system in app/tenancy.py: this plane has no
tenant to authenticate INTO. The service-token-linked-to-a-tenant model in
Section 8 of the plan is Step 4's retrieval plane, not this one.

Mostly pass-through on purpose: vLLM already speaks the OpenAI shape (Step
3.0/3.1), so there is little to translate. What this file adds that vLLM
does not have on its own: a token that is not vLLM's single static key,
per-token rate limits, and model aliasing so the website pins a name that
never has to change when the served model does (Section 13).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from app import llm
from app.config import (GATEWAY_RATE_LIMIT_PER_MINUTE, GATEWAY_TOKENS,
                        MODEL_ALIASES, tokens_equal)

router = APIRouter()

_requests: dict[str, list[float]] = defaultdict(list)
RATE_WINDOW_SECONDS = 60


class ChatCompletionRequest(BaseModel):
    """Only the two fields this plane needs to read are named; everything
    else an OpenAI client sends (tools, tool_choice, temperature, max_tokens,
    stream_options, ...) passes through untouched via `extra="allow"`."""

    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False


def _enforce_rate_limit(token: str) -> None:
    now = time.time()
    recent = [t for t in _requests[token] if now - t < RATE_WINDOW_SECONDS]
    if len(recent) >= GATEWAY_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            429,
            f"Rate limit exceeded: {GATEWAY_RATE_LIMIT_PER_MINUTE} requests "
            f"per {RATE_WINDOW_SECONDS}s per token.",
        )
    recent.append(now)
    _requests[token] = recent


def require_gateway_token(request: Request) -> str:
    """Applied to every route in this router. Returns the token, for rate limiting.

    Deliberately does not call context.set_tenant or anything like it: there
    is no tenant here, by design, and there must never be one.
    """
    if not GATEWAY_TOKENS:
        raise HTTPException(
            503,
            "The inference plane has no tokens configured. Set GATEWAY_TOKENS in .env.",
        )
    header = request.headers.get("authorization", "")
    candidate = header[7:].strip() if header.lower().startswith("bearer ") else ""
    # Constant-time against every configured token, not ==, so a wrong guess
    # takes the same time as a right one and no early return leaks which token
    # index it was compared against. Through config.tokens_equal rather than
    # hmac.compare_digest directly: compare_digest RAISES on a str holding
    # non-ASCII, so one accented letter in a bearer token used to reach a 500
    # from a caller who had not authenticated. Found in Step 4.0.
    if not candidate or not any(tokens_equal(candidate, t) for t in GATEWAY_TOKENS):
        raise HTTPException(401, "Invalid or missing bearer token.")
    _enforce_rate_limit(candidate)
    return candidate


def _clamp_output_budget(payload: dict[str, Any], real_model: str) -> None:
    """Cap max_tokens at what is actually left of the model's window.

    max_tokens is a RESERVATION, made before generation starts, out of a
    budget the prompt shares. An OpenAI client sets it from what the hosted
    models allow and has no way to know what this machine serves -- the
    website sends 32000 against a window of 8192, and vLLM rejects that
    outright: "max_tokens=32000 cannot be greater than max_model_len=8192".
    Every request, before a token is generated.

    Clamping here rather than asking the website to change is the same
    argument as the model alias next to it. The caller pins `syslab-default`
    precisely so it does not have to know what is behind it; a context window
    it must track is that knowledge coming back in through another door, and
    it would have to change again the day --max-model-len does.

    Two cases are deliberately left alone:

    - No window known (the server is down, or does not publish it). Passing
      the request through unchanged lets vLLM give its own precise error
      rather than this guessing at a limit and truncating answers a working
      server would have finished.
    - The prompt alone does not fit. That is a real failure and the caller
      needs to see it, not a silently empty answer. vLLM says so exactly.

    A clamped request is not a degraded one in the way a truncated PROMPT
    would be: nothing the caller sent is dropped. Only the ceiling on the
    reply comes down, and it comes down to the largest value that can work.
    """
    window = llm.model_window(real_model)
    if window is None:
        return
    # llm.estimate_prompt_tokens, not a local copy: app/agent.py trims history
    # against the same estimate this clamps against, and two of them would drift
    # into disagreeing about how full one window is.
    room = window - llm.estimate_prompt_tokens(
        payload.get("messages"), payload.get("tools")
    )
    if room <= 0:
        return
    # max_completion_tokens is the newer OpenAI spelling of the same field.
    # Both are honoured because a client may send either, and a clamp that
    # only knew one name would leave the other to fail exactly as before.
    for field in ("max_tokens", "max_completion_tokens"):
        requested = payload.get(field)
        if isinstance(requested, int) and requested > room:
            payload[field] = room


def _resolve_model(alias: str) -> str:
    real = MODEL_ALIASES.get(alias)
    if real is None:
        raise HTTPException(
            404,
            {
                "error": {
                    "message": f"The model `{alias}` does not exist.",
                    "type": "NotFoundError",
                    "param": "model",
                    "code": 404,
                }
            },
        )
    return real


@router.get("/v1/models", dependencies=[Depends(require_gateway_token)])
def list_models() -> dict:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": alias, "object": "model", "created": now, "owned_by": "syslab"}
            for alias in MODEL_ALIASES
        ],
    }


def _relabel_stream(response, alias: str) -> Iterator[str]:
    """Re-emit vLLM's SSE stream, except the model field: the caller asked for
    an alias and must see that alias in every chunk, not the real model name
    behind it. Section 13's "changing the model edits one file on one machine
    and changes nothing" only holds if the caller never sees the real name.
    """
    for chunk in _iter_sse(response):
        chunk["model"] = alias
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


def _iter_sse(response) -> Iterator[dict]:
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


@router.post("/v1/chat/completions", dependencies=[Depends(require_gateway_token)])
def chat_completions(body: ChatCompletionRequest):
    alias = body.model
    real_model = _resolve_model(alias)

    payload = body.model_dump()
    payload["model"] = real_model
    # Same default as app/llm.py's agent-facing chat(): Qwen3 thinks by
    # default and will burn a token budget on it (Step 3.0, Finding 2). A
    # caller that wants thinking on can still set chat_template_kwargs
    # itself; this only supplies the default when they did not.
    payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
    _clamp_output_budget(payload, real_model)
    if payload.get("stream"):
        payload.setdefault("stream_options", {"include_usage": True})

    try:
        response = llm.raw_request(payload)
    except llm.LlmError as exc:
        raise HTTPException(503, str(exc)) from exc

    if not payload.get("stream"):
        raw_body = response.read().decode("utf-8")
        try:
            result = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(502, "The model server returned something that is not JSON.") from exc
        result["model"] = alias
        return JSONResponse(result)

    return StreamingResponse(_relabel_stream(response, alias), media_type="text/event-stream")
