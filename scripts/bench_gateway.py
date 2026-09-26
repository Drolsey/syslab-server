"""Measure what the gateway actually does under concurrent load, before
tuning anything on the vLLM box for it.

    python scripts/bench_gateway.py --token $GATEWAY_TOKEN
    python scripts/bench_gateway.py --token $GATEWAY_TOKEN --concurrency 10 --rounds 3
    python scripts/bench_gateway.py --token $GATEWAY_TOKEN --base-url http://192.168.1.185:8080/v1

vLLM's own startup log prints a worst-case concurrency bound: it assumes
every concurrent request fills the full --max-model-len window at once.
Production traffic does not look like that (see the project memory on
KV-cache/concurrency: real transcripts ran 700-23,283 input tokens). The only
way to know the real number is to fire real concurrent requests shaped like
real traffic and measure what happens -- not trust the pessimistic log line.

Stdlib only, matching the other scripts here: urllib + a thread pool is
plenty for N POSTs to one endpoint.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import string
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.config import GATEWAY_TOKENS
except Exception:  # noqa: BLE001
    GATEWAY_TOKENS: set[str] = set()

LINE = "-" * 74
DEFAULT_BASE_URL = "http://192.168.1.185:8080/v1"
DEFAULT_MODEL = "syslab-default"

# Mirrors real database-agent traffic (project memory: production transcripts
# measured between ~700 and ~23,283 input tokens), weighted toward the
# small/medium turns that are most common, with a couple of large ones to
# also exercise the case that actually pressures the KV cache.
DEFAULT_SIZES = [700, 1500, 2500, 4000, 6000, 8000, 9000, 11000, 13000, 23000]

FILLER = (
    "Invoice line item: on-site calibration of sensor arrays, three days, "
    "labour and parts itemised separately, payable within thirty days. "
)


def build_prompt(target_tokens: int) -> str:
    """~1.3 tokens per word for English filler text -- close enough for a
    load test; actual usage is read back from the response either way. A
    per-call nonce keeps vLLM's prefix caching from turning every request
    after the first into a near-free cache hit, which would understate the
    concurrent load this is trying to measure."""
    target_words = max(8, int(target_tokens / 1.3))
    base = FILLER * (target_words // len(FILLER.split()) + 1)
    base = " ".join(base.split()[:target_words])
    nonce = "".join(random.choices(string.ascii_lowercase, k=24))
    return f"Reference {nonce}. {base}\n\nIn one sentence, what is this text about?"


@dataclass
class RequestResult:
    index: int
    target_tokens: int
    ok: bool
    http_code: int
    started: float
    ended: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    error: str | None = None

    @property
    def duration(self) -> float:
        return self.ended - self.started


def fire_one(
    base_url: str, token: str, model: str, index: int, target_tokens: int,
    max_tokens: int, timeout: int,
) -> RequestResult:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(target_tokens)}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        ended = time.time()
        usage = body.get("usage", {})
        choice = (body.get("choices") or [{}])[0]
        return RequestResult(
            index=index, target_tokens=target_tokens, ok=True, http_code=response.status,
            started=started, ended=ended,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            finish_reason=choice.get("finish_reason"),
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return RequestResult(
            index=index, target_tokens=target_tokens, ok=False, http_code=exc.code,
            started=started, ended=time.time(), error=detail,
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        return RequestResult(
            index=index, target_tokens=target_tokens, ok=False, http_code=0,
            started=started, ended=time.time(), error=str(exc),
        )


def run_round(
    base_url: str, token: str, model: str, sizes: list[int], max_tokens: int, timeout: int,
) -> tuple[list[RequestResult], float]:
    results: list[RequestResult] = []
    batch_started = time.time()
    with ThreadPoolExecutor(max_workers=len(sizes)) as pool:
        futures = [
            pool.submit(fire_one, base_url, token, model, i, size, max_tokens, timeout)
            for i, size in enumerate(sizes)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    batch_wall = time.time() - batch_started
    results.sort(key=lambda r: r.index)
    return results, batch_wall


def summarize(results: list[RequestResult], batch_wall: float) -> None:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print(f"\n  {len(ok)}/{len(results)} succeeded, wall time for the whole batch: {batch_wall:.1f}s\n")

    for r in results:
        if r.ok:
            print(f"  req {r.index:>2}  target~{r.target_tokens:>6}tok  "
                  f"actual {r.prompt_tokens if r.prompt_tokens is not None else '?':>6}in/"
                  f"{r.completion_tokens if r.completion_tokens is not None else '?':>4}out  "
                  f"{r.duration:6.1f}s  finish={r.finish_reason}")
        else:
            print(f"  req {r.index:>2}  target~{r.target_tokens:>6}tok  "
                  f"FAILED  HTTP {r.http_code}  {r.duration:6.1f}s  {r.error}")

    if ok:
        durations = [r.duration for r in ok]
        sequential_estimate = sum(durations)
        p95_index = max(0, int(len(durations) * 0.95) - 1)
        print(f"\n  latency (successful requests): "
              f"min {min(durations):.1f}s   median {statistics.median(durations):.1f}s   "
              f"p95 {sorted(durations)[p95_index]:.1f}s   max {max(durations):.1f}s")
        if batch_wall > 0:
            speedup = sequential_estimate / batch_wall
            print(f"  sum of individual latencies: {sequential_estimate:.1f}s   "
                  f"actual wall time: {batch_wall:.1f}s   "
                  f"=> effective concurrency ~{speedup:.2f}x")
            print("    (close to the request count means real parallelism; close to 1x means")
            print("     requests were serialized/queued behind each other)")

    if failed:
        print(f"\n  {len(failed)} request(s) failed:")
        by_code: dict[int, int] = {}
        for r in failed:
            by_code[r.http_code] = by_code.get(r.http_code, 0) + 1
        labels = {0: "connection/timeout", 429: "rate limited", 400: "bad request (likely context too long)"}
        for code, count in sorted(by_code.items()):
            print(f"    {count}x  {labels.get(code, f'HTTP {code}')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fire concurrent chat completions at the gateway and measure what actually happens."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token", default=next(iter(GATEWAY_TOKENS), None),
                         help="gateway bearer token; falls back to the first entry in GATEWAY_TOKENS (.env)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--concurrency", type=int, default=len(DEFAULT_SIZES),
                         help="number of simultaneous requests to fire per round")
    parser.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES),
                         help="comma-separated target input-token sizes; cycled/truncated to --concurrency")
    parser.add_argument("--max-tokens", type=int, default=500, help="requested output budget per request")
    parser.add_argument("--rounds", type=int, default=1, help="repeat the whole concurrent batch this many times")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    if not args.token:
        print("No gateway token. Pass --token <value>, or run this where GATEWAY_TOKENS is set in .env.")
        return 1

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if not sizes:
        print("No prompt sizes given.")
        return 1
    if len(sizes) < args.concurrency:
        sizes = (sizes * (args.concurrency // len(sizes) + 1))[: args.concurrency]
    else:
        sizes = sizes[: args.concurrency]

    print("\nsyslab-server / gateway concurrency load test")
    print(f"  target: {args.base_url}")
    print(f"  model: {args.model}")
    print(f"  concurrency: {len(sizes)} simultaneous requests, sizes (input tokens, approx): {sizes}")
    print(f"  max_tokens per request: {args.max_tokens}")

    for round_num in range(1, args.rounds + 1):
        print(f"\n{LINE}\n  round {round_num}/{args.rounds}\n{LINE}")
        results, batch_wall = run_round(args.base_url, args.token, args.model, sizes, args.max_tokens, args.timeout)
        summarize(results, batch_wall)

    print(f"\n{LINE}")
    print("  Read this, don't just trust the vLLM startup log's number: that number")
    print("  assumes every request fills the full context window at once. This measured")
    print("  what your actual traffic shape does. If requests queued (effective")
    print("  concurrency well under the request count) but nothing failed outright, that")
    print("  queuing is your real ceiling -- raise it with --kv-cache-dtype fp8 on the")
    print("  vLLM container before touching --gpu-memory-utilization (documented boot")
    print("  failure history on this box at 0.85).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
