"""Step 5.1: benchmark an embedding candidate, and check whether the chat
deployment already answers embedding requests before assuming a second
container is needed.

    python scripts/bench_embeddings.py --check-production
    python scripts/bench_embeddings.py --base-url http://192.168.1.185:8001/v1 \\
        --model Qwen/Qwen3-Embedding-0.6B

WHY THIS IS A NEW SCRIPT AND NOT scripts/bench_models.py APPLIED AS-IS
    bench_models.py does not fit, for two independent reasons, not one:

    1. It only speaks Ollama's API (`/api/generate`, `/api/show`, `/api/ps`,
       OLLAMA_HOST). Ollama is not what this box runs. `docs/models.md`'s own
       words: bench_models.py's 8 September numbers were "Ollama-derived
       guesses", explicitly superseded the moment vLLM's own startup log was
       read, and vLLM was chosen over Ollama specifically for concurrency
       (`docker-compose.yml`, the comment on `--gpu-memory-utilization`).
       There is no Ollama service in `docker-compose.yml`. Benchmarking an
       embedding candidate through Ollama would reproduce the exact category
       of number this project already threw away once.
    2. Even against the right backend, it measures the wrong thing.
       `/api/generate`'s `eval_count` / `eval_duration` is autoregressive
       generation speed -- tokens produced one at a time, with a growing KV
       cache. An embedding call is a single forward pass over the input and
       back: one number, not a stream, and "tokens per second" is not the
       question. The question is latency for one chunk and for a batch,
       which is what this script measures directly.

WHAT IT TALKS TO
    The OpenAI-compatible `/v1/embeddings` surface, the same wire format
    `app/llm.py` and the gateway already speak for chat -- so the numbers
    here describe the same kind of deployment the chat model actually runs
    under, not a stand-in. `--production-url` (default: the box's vLLM port
    directly, unauthenticated on the LAN the same way HANDOVER.md already
    records ":8000 200") answers "does the current container already serve
    embeddings" with a real response instead of an assumption -- expected to
    fail, since vLLM serves one task per process, but the plan itself says
    this is "worth fifteen minutes of checking" rather than trusting that.

    `--base-url` is a SEPARATE candidate embedding deployment the operator
    starts themselves, the same way `docker-compose.yml`'s own vLLM service
    is started deliberately rather than orchestrated by a script. This tool
    measures; it does not launch containers. A plausible way to bring one up
    for Qwen3-Embedding-0.6B, already the one embedding-adjacent entry
    `docs/licences.md` verified on the same day as vLLM itself:

        docker run -d --name bench-embed-candidate --gpus all -p 8001:8000 \\
          -v syslab-server_huggingface-cache:/root/.cache/huggingface \\
          vllm/vllm-openai:v0.28.0@<digest, see docker-compose.yml> \\
          --model Qwen/Qwen3-Embedding-0.6B --runner pooling \\
          --max-model-len 2048 --gpu-memory-utilization 0.3

    BOTH FLAGS ON THE LAST LINE WERE WRONG ONCE, CONFIRMED AGAINST THE
    RUNNING v0.28.0 IMAGE ON 18 SEPTEMBER 2026, not trusted from memory:
    (1) an earlier draft guessed `--task embed`; this version has no
    `--task` flag at all, and a container started with it crashed on an
    argparse error and self-removed via `--rm` before the logs could be
    read. `vllm serve --help=all` shows `--runner
    {auto,draft,generate,pooling}` and `--convert {auto,classify,embed,none}`
    instead -- `--convert` adapts a TEXT-GENERATION model into a pooling
    one, which does not apply here since Qwen3-Embedding-0.6B is already
    natively a pooling model, so `--runner pooling` alone is right.
    (2) `--gpu-memory-utilization 0.15` alone then failed too: this model
    inherits a 32768-token max context from its architecture, and vLLM
    refuses to start unless it can fit a KV cache for at least one
    full-length request -- 3.5 GiB, more than 0.15's ~4.9 GiB budget left
    after weights. `--max-model-len 2048` is the actual fix, not just a
    bigger utilization number: nothing this project embeds is anywhere
    near 32768 tokens (`TARGET_TOKENS` in `app/chunks.py` is 512), so
    capping the context vLLM reserves KV cache for is honest sizing, not a
    workaround.

    Run WITHOUT `--rm` the first time against any new candidate,
    specifically so a crash leaves `docker logs <name>` behind to read
    instead of taking the evidence with it.

VRAM IS READ, NOT DIFFED, ON PURPOSE
    Ollama could be told to load and unload a model on command, so
    bench_models.py could measure a clean before/after delta itself. vLLM
    cannot be remote-controlled that way -- a container is started with one
    model and stays that model. So this prints `nvidia-smi`'s CURRENT
    reading and leaves the before/after to the operator: run once with the
    candidate not yet started, run again once it is, diff the two numbers --
    the same discipline `docker-compose.yml`'s own comments already ask for
    ("read the startup log and record the real numbers... before trusting
    this one"). `--baseline-vram-mb` does the subtraction for you if you
    hand it the first reading.

Stdlib only, matching every other script here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.config import LLM_BASE_URL, LLM_MODEL
except Exception:  # noqa: BLE001
    LLM_BASE_URL = "http://127.0.0.1:8000/v1"
    LLM_MODEL = "Qwen/Qwen3-14B-AWQ"

try:
    # The real split size the system will actually embed, not a round number
    # picked for the benchmark. TARGET_CHARS is 512 tokens at 7/2 chars/token.
    from app.chunks import TARGET_CHARS
except Exception:  # noqa: BLE001
    TARGET_CHARS = 1792

LINE = "-" * 74
MB = 1024 * 1024

DEFAULT_PRODUCTION_URL = "http://192.168.1.185:8000/v1"
DEFAULT_CANDIDATE_MODEL = "Qwen/Qwen3-Embedding-0.6B"

FILLER = (
    "Section 4.2, on-site calibration: sensor arrays were verified against "
    "the reference standard before and after the maintenance window, with "
    "deviations logged per unit and re-checked the following business day. "
)


# --------------------------------------------------------------------------
# machine measurements -- same shape as scripts/bench_models.py, duplicated
# rather than imported: every script here is standalone stdlib, on purpose.
# --------------------------------------------------------------------------

def nvidia(query: str) -> list[str]:
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def vram_used_mb() -> float | None:
    values = nvidia("memory.used")
    return float(values[0]) if values else None


def gpu_facts() -> dict:
    name = nvidia("name")
    total = nvidia("memory.total")
    return {
        "name": name[0] if name else "unknown",
        "vram_total_mb": float(total[0]) if total else None,
    }


# --------------------------------------------------------------------------
# talking to an OpenAI-compatible embeddings endpoint
# --------------------------------------------------------------------------

def post_json(url: str, payload: dict, timeout: int = 120) -> tuple[int, dict | str]:
    """(status, body). Body is the parsed JSON, or raw text if it was not JSON."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def get_json(url: str, timeout: int = 15):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def chunk_text(nonce: str) -> str:
    """A string the size of a real chunk (TARGET_CHARS), not an arbitrary
    benchmark size, so the latency measured is the latency the embeddings
    producer will actually pay per chunk_id.

    Nonced per call, the same reasoning `bench_gateway.py` and
    `bench_models.py` already use: an unchanging input risks being served
    from a cache somewhere in the stack, which would measure the cache and
    not the model.
    """
    base = (FILLER * (TARGET_CHARS // len(FILLER) + 1))[:TARGET_CHARS]
    return f"[{nonce}] {base}"


def _nonce() -> str:
    return hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# the two checks 5.1 actually asks for
# --------------------------------------------------------------------------

def probe_production(base_url: str, model: str) -> dict:
    """Does the currently-deployed chat vLLM already answer /v1/embeddings?

    Expected to fail -- vLLM serves one --task per process, and the running
    container was started with --model Qwen/Qwen3-14B-AWQ for chat, no
    --task embed -- but checked rather than assumed, per the plan's own
    "worth fifteen minutes of checking before assuming a second container
    is needed."
    """
    url = base_url.rstrip("/") + "/embeddings"
    started = time.time()
    try:
        status, body = post_json(url, {"model": model, "input": "probe"}, timeout=30)
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"base_url": base_url, "model": model, "reachable": False, "error": str(exc)}
    wall = time.time() - started
    served = status == 200 and isinstance(body, dict) and bool(body.get("data"))
    return {
        "base_url": base_url,
        "model": model,
        "reachable": True,
        "status": status,
        "serves_embeddings": served,
        "response": body if served else (body if isinstance(body, str) else json.dumps(body)[:500]),
        "wall_seconds": round(wall, 3),
    }


def bench_candidate(base_url: str, model: str, runs: int, batch_sizes: list[int]) -> dict | None:
    print(f"\n{LINE}\n  {model}  @  {base_url}\n{LINE}")

    models_url = base_url.rstrip("/") + "/models"
    try:
        listing = get_json(models_url)
        ids = [m.get("id") for m in listing.get("data", [])]
        print(f"  /v1/models reports: {ids}")
        if model not in ids and ids:
            print(f"  WARNING: {model!r} is not in that list; requests may 404.")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  Could not reach {models_url}: {exc}")
        return None

    vram = vram_used_mb()
    print(f"  Current GPU VRAM in use: {vram:.0f} MB" if vram is not None else
          "  Current GPU VRAM in use: unknown (no nvidia-smi)")

    embeddings_url = base_url.rstrip("/") + "/embeddings"

    # ---- one chunk, several times, for a latency distribution -------------
    single_latencies = []
    dimension = None
    for _ in range(runs):
        started = time.time()
        status, body = post_json(embeddings_url, {"model": model, "input": chunk_text(_nonce())})
        wall = time.time() - started
        if status != 200 or not isinstance(body, dict) or not body.get("data"):
            print(f"  single-chunk call failed ({status}): {str(body)[:200]}")
            continue
        single_latencies.append(wall)
        if dimension is None:
            dimension = len(body["data"][0]["embedding"])

    if not single_latencies:
        print("  No successful single-chunk calls; nothing to report.")
        return None

    print(f"  dimension: {dimension}  ({dimension * 4} bytes/chunk as float32, "
          f"{dimension * 2} as float16)")
    print(f"  single chunk ({TARGET_CHARS} chars): "
          f"median {statistics.median(single_latencies) * 1000:.0f} ms, "
          f"p95 {sorted(single_latencies)[max(0, int(len(single_latencies) * 0.95) - 1)] * 1000:.0f} ms "
          f"over {len(single_latencies)} runs")

    # ---- batches of several chunks in one call -----------------------------
    batches = []
    for size in batch_sizes:
        latencies = []
        for _ in range(max(1, runs // 2 or 1)):
            inputs = [chunk_text(_nonce()) for _ in range(size)]
            started = time.time()
            status, body = post_json(embeddings_url, {"model": model, "input": inputs}, timeout=300)
            wall = time.time() - started
            if status != 200 or not isinstance(body, dict) or not body.get("data"):
                print(f"  batch={size} call failed ({status}): {str(body)[:200]}")
                continue
            latencies.append(wall)
        if not latencies:
            continue
        median = statistics.median(latencies)
        per_chunk = median / size
        batches.append({
            "batch_size": size,
            "wall_seconds_median": round(median, 3),
            "per_chunk_ms": round(per_chunk * 1000, 1),
        })
        print(f"  batch of {size:>4}: {median * 1000:7.0f} ms total, "
              f"{per_chunk * 1000:6.1f} ms/chunk "
              f"({single_latencies and statistics.median(single_latencies) / per_chunk:.1f}x "
              f"vs. one at a time)")

    return {
        "model": model,
        "base_url": base_url,
        "dimension": dimension,
        "current_vram_mb": vram,
        "single_chunk": {
            "chars": TARGET_CHARS,
            "runs": len(single_latencies),
            "median_ms": round(statistics.median(single_latencies) * 1000, 1),
            "p95_ms": round(
                sorted(single_latencies)[max(0, int(len(single_latencies) * 0.95) - 1)] * 1000, 1
            ),
        },
        "batches": batches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=None,
                         help="Candidate embedding endpoint, e.g. http://192.168.1.185:8001/v1")
    parser.add_argument("--model", default=DEFAULT_CANDIDATE_MODEL)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--batch-sizes", default="8,32,64")
    parser.add_argument("--baseline-vram-mb", type=float, default=None,
                         help="VRAM reading (MB) from before the candidate was started, "
                              "to print a weights delta instead of just the current total.")
    parser.add_argument("--check-production", action="store_true",
                         help="Also (or only) probe the existing chat deployment for /v1/embeddings.")
    parser.add_argument("--production-url", default=DEFAULT_PRODUCTION_URL)
    parser.add_argument("--production-model", default=LLM_MODEL)
    parser.add_argument("--out", default="bench_results_embeddings.json")
    args = parser.parse_args()

    if not args.base_url and not args.check_production:
        parser.error("pass --base-url for a candidate, or --check-production, or both.")

    gpu = gpu_facts()
    print("\nsyslab-server / embedding benchmark (Step 5.1)")
    print(f"  GPU: {gpu['name']}"
          + (f", {gpu['vram_total_mb'] / 1024:.0f} GB VRAM" if gpu["vram_total_mb"] else ""))

    production = None
    if args.check_production:
        print(f"\n{LINE}\n  Does {args.production_url} already serve embeddings?\n{LINE}")
        production = probe_production(args.production_url, args.production_model)
        if production["reachable"] and production.get("serves_embeddings"):
            print(f"  YES - {args.production_model} answered /v1/embeddings directly. "
                  f"No second container needed.")
        elif production["reachable"]:
            print(f"  NO - HTTP {production['status']}: {str(production['response'])[:300]}")
            print("  Expected: this process was started with --model "
                  f"{args.production_model} for chat, not --task embed.")
        else:
            print(f"  Could not reach it: {production['error']}")

    candidate = None
    if args.base_url:
        batch_sizes = [int(b) for b in args.batch_sizes.split(",") if b.strip()]
        candidate = bench_candidate(args.base_url, args.model, args.runs, batch_sizes)
        if candidate and args.baseline_vram_mb is not None and candidate["current_vram_mb"] is not None:
            delta = candidate["current_vram_mb"] - args.baseline_vram_mb
            candidate["weights_vram_mb"] = round(delta, 0)
            print(f"\n  VRAM delta vs. baseline {args.baseline_vram_mb:.0f} MB: {delta:.0f} MB")

    payload = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "gpu": gpu,
        "production_probe": production,
        "candidate": candidate,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  Full numbers written to {args.out}")
    print("  Paste this output into docs/models.md's embedding candidate table (5.1).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
