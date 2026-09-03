"""Phase 01 gate: prove Ollama is up, the model is pulled, and it answers.

Stdlib only, so it runs before the project's virtualenv exists.

    py scripts/check_ollama.py
    py scripts/check_ollama.py --model qwen3:4b
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

LINE = "-" * 62
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def get_json(url: str, timeout: int = 10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: int = 300):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_server(host: str) -> bool:
    section("Ollama server")
    try:
        tags = get_json(f"{host}/api/tags")
    except urllib.error.URLError as exc:
        print(f"  Cannot reach Ollama at {host}")
        print(f"  {exc}")
        print("\n  Ollama is not running, or it is listening somewhere else.")
        print("  Start it (the Windows installer runs it in the tray) and retry.")
        return False
    print(f"  Reachable at {host}")
    models = [m.get("name", "?") for m in tags.get("models", [])]
    if models:
        print("  Models pulled:")
        for name in models:
            print(f"    {name}")
    else:
        print("  No models pulled yet.")
    return True


def check_model(host: str, model: str) -> bool:
    section(f"Model: {model}")
    try:
        info = post_json(f"{host}/api/show", {"model": model}, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"  {model} is not pulled. Run:  ollama pull {model}")
        else:
            print(f"  /api/show failed: {exc}")
        return False
    except urllib.error.URLError as exc:
        print(f"  /api/show failed: {exc}")
        return False

    details = info.get("details", {}) or {}
    print(f"  Parameters:   {details.get('parameter_size', 'unknown')}")
    print(f"  Quantisation: {details.get('quantization_level', 'unknown')}")
    print(f"  Family:       {details.get('family', 'unknown')}")

    caps = info.get("capabilities") or []
    if caps:
        print(f"  Capabilities: {', '.join(caps)}")
    if caps and "tools" not in caps:
        print("\n  WARNING: this build does not advertise tool support.")
        print("  Phase 03 needs it. Pick a tool-capable tag before continuing.")
        return False
    return True


def check_answer(host: str, model: str) -> bool:
    section("Round trip")
    print("  Asking a question that needs no tools. First load can take a minute.")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Reply with exactly one word: the capital city of Japan.",
            }
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    started = time.time()
    try:
        result = post_json(f"{host}/api/chat", payload)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  Request failed: {exc}")
        return False
    elapsed = time.time() - started

    answer = (result.get("message", {}) or {}).get("content", "").strip()
    print(f"  Answer:  {answer!r}")
    print(f"  Latency: {elapsed:.1f}s (includes loading the model into VRAM)")

    eval_count = result.get("eval_count")
    eval_ns = result.get("eval_duration")
    if eval_count and eval_ns:
        print(f"  Speed:   {eval_count / (eval_ns / 1e9):.1f} tokens/sec")

    if "tokyo" not in answer.lower():
        print("\n  The model replied, but not with the expected answer.")
        print("  Not fatal. It proves the pipe works; judge the quality yourself.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 01 gate check for Ollama.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    print("\nsyslab-server / Phase 01 Ollama check")
    if not check_server(args.host):
        return 1
    if not check_model(args.host, args.model):
        return 1
    if not check_answer(args.host, args.model):
        return 1

    section("Gate")
    print("  Phase 01 passes: Ollama is running, the model is pulled, and it")
    print("  answers over the REST API the agent uses. Nothing further to do here.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
