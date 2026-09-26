"""Step 3.0: measure what this vLLM build actually does before designing app/llm.py.

The architecture plan is explicit that this comes before any code: vLLM's
tool_choice support, streaming tool-call shape and usage reporting are
asserted by the vLLM project in general, but the specific build, model and
chat template running on this box are what app/gateway.py has to work
against. Guessing wastes the step; probing it costs one script.

Stdlib only, matching app/llm.py's own reasoning: a client library pinned
against a fast-moving local server is a version mismatch waiting to happen,
and this is a handful of POSTs to one endpoint.

    python scripts/check_gateway.py --probe
    python scripts/check_gateway.py --probe --base-url http://192.168.1.185:8000/v1
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
LINE = "-" * 74

DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

WEATHER_PROMPT = "What is the weather in Cairo right now?"
NEUTRAL_PROMPT = "Say hello in exactly three words."


def post(base_url: str, payload: dict, timeout: int = 60) -> dict | list:
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    if payload.get("stream"):
        return list(_iter_sse(body))
    return json.loads(body)


def _iter_sse(body: str):
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def get_model(base_url: str) -> str:
    request = urllib.request.Request(f"{base_url}/models", method="GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["data"][0]["id"]


def probe_tool_choice(base_url: str, model: str, value: Any, prompt: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [DUMMY_TOOL],
        "tool_choice": value,
        "temperature": 0,
        "max_tokens": 200,
    }
    try:
        result = post(base_url, payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}

    choice = result.get("choices", [{}])[0]
    message = choice.get("message", {})
    calls = message.get("tool_calls") or []
    return {
        "ok": True,
        "called_tool": bool(calls),
        "tool_name": calls[0]["function"]["name"] if calls else None,
        "arguments_type": type(calls[0]["function"]["arguments"]).__name__ if calls else None,
        "finish_reason": choice.get("finish_reason"),
        "content": (message.get("content") or "")[:80],
    }


def probe_streaming_tool_calls(base_url: str, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": WEATHER_PROMPT}],
        "tools": [DUMMY_TOOL],
        "tool_choice": "required",
        "temperature": 0,
        "max_tokens": 200,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    try:
        chunks = post(base_url, payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}

    indices_seen: list[int] = []
    saw_usage = False
    reassembled_name = None
    reassembled_args = ""
    for chunk in chunks:
        if chunk.get("usage"):
            saw_usage = True
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        for call in delta.get("tool_calls") or []:
            if "index" in call:
                indices_seen.append(call["index"])
            fn = call.get("function") or {}
            if fn.get("name"):
                reassembled_name = fn["name"]
            if fn.get("arguments"):
                reassembled_args += fn["arguments"]

    return {
        "ok": True,
        "chunk_count": len(chunks),
        "saw_usage_chunk": saw_usage,
        "stable_indices": indices_seen,
        "reassembled_name": reassembled_name,
        "reassembled_arguments": reassembled_args,
        "arguments_parse_ok": _try_parse(reassembled_args),
    }


def _try_parse(text: str) -> bool:
    try:
        json.loads(text or "{}")
        return True
    except json.JSONDecodeError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="run the capability probe")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    if not args.probe:
        parser.print_help()
        return

    print(f"\nsyslab-server / Step 3.0 gateway capability probe")
    print(f"  target: {args.base_url}\n")

    try:
        model = get_model(args.base_url)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError) as exc:
        print(f"Could not reach {args.base_url}: {exc}")
        raise SystemExit(1)
    print(f"  model: {model}\n")

    print("tool_choice matrix")
    print(LINE)
    for label, value, prompt in [
        ("auto, tool-relevant prompt", "auto", WEATHER_PROMPT),
        ("auto, unrelated prompt", "auto", NEUTRAL_PROMPT),
        ("none, tool-relevant prompt", "none", WEATHER_PROMPT),
        ("required, unrelated prompt", "required", NEUTRAL_PROMPT),
        ("named function", {"type": "function", "function": {"name": "get_weather"}}, NEUTRAL_PROMPT),
    ]:
        result = probe_tool_choice(args.base_url, model, value, prompt)
        print(f"  {label}:")
        print(f"    {json.dumps(result)}")

    print(f"\nstreaming tool-call shape")
    print(LINE)
    result = probe_streaming_tool_calls(args.base_url, model)
    print(f"  {json.dumps(result, indent=2)}")

    print(f"\nGate")
    print(LINE)
    print("  This prints a capability matrix; it does not pass or fail on its own.")
    print("  Read the results and commit them into the Step 3 plan before writing")
    print("  app/gateway.py against them.\n")


if __name__ == "__main__":
    main()
