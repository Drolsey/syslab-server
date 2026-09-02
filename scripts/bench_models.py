"""Measure what a model actually costs on this machine, then project upward.

    .\run.cmd scripts\bench_models.py
    .\run.cmd scripts\bench_models.py --models qwen3:8b,qwen3:14b --runs 3

Standard library only. Measures, per model:

  * VRAM for the weights, from Ollama's own accounting and from nvidia-smi
  * peak VRAM under load, sampled while it is generating
  * system RAM used, which is where a model spills when it does not fit
  * prompt processing speed and generation speed, at several context sizes
  * KV cache growth per 1k tokens of context, measured and predicted

Then it projects to a larger model. Read the caveats it prints. Two measured
points can pin down the parts that scale by arithmetic; they cannot tell you
about an architecture nobody has measured.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import platform
import random
import statistics
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.config import OLLAMA_HOST
except Exception:  # noqa: BLE001
    OLLAMA_HOST = "http://127.0.0.1:11434"

LINE = "-" * 74
MB = 1024 * 1024
GB = 1024 * 1024 * 1024

# Average bits per weight for the common GGUF quantisations. Used to project
# from the one you measured to the ones you did not, since scaling a 4-bit
# figure by a made-up factor is how you end up telling someone an 8-bit 120B
# fits in 77 GB.
BITS_PER_WEIGHT = {
    "Q3_K_M": 3.9, "Q4_0": 4.55, "Q4_K_S": 4.6, "Q4_K_M": 4.85,
    "Q5_K_M": 5.7, "Q6_K": 6.6, "Q8_0": 8.5, "F16": 16.0, "FP16": 16.0, "BF16": 16.0,
}
# One billion weights at one bit per weight, in GiB.
GIB_PER_B_PER_BIT = 1e9 / 8 / GB


# --------------------------------------------------------------------------
# machine measurements
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


def system_ram() -> dict:
    """Total and available RAM, without needing psutil."""
    if platform.system() == "Windows":
        class Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return {"total_mb": status.ullTotalPhys / MB, "available_mb": status.ullAvailPhys / MB}
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                info[key] = float(rest.strip().split()[0]) / 1024
        return {"total_mb": info.get("MemTotal", 0), "available_mb": info.get("MemAvailable", 0)}
    except OSError:
        return {"total_mb": None, "available_mb": None}


class VramSampler:
    """Poll VRAM in the background while something else is generating."""

    def __init__(self, interval: float = 0.25):
        self.interval = interval
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                value = vram_used_mb()
                if value is not None:
                    self.samples.append(value)
                self._stop.wait(self.interval)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def peak(self) -> float | None:
        return max(self.samples) if self.samples else None


# --------------------------------------------------------------------------
# talking to ollama
# --------------------------------------------------------------------------

def call(path: str, payload: dict | None = None, timeout: int = 600):
    request = urllib.request.Request(
        OLLAMA_HOST + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def unload_everything() -> None:
    """keep_alive 0 tells Ollama to drop a model from VRAM immediately."""
    try:
        loaded = call("/api/ps").get("models", [])
    except Exception:  # noqa: BLE001
        return
    for entry in loaded:
        name = entry.get("name") or entry.get("model")
        if not name:
            continue
        try:
            call("/api/generate", {"model": name, "prompt": "", "keep_alive": 0}, timeout=60)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(3)


def loaded_footprint(model: str) -> dict:
    """Ollama's own accounting: how much it thinks the model occupies."""
    try:
        for entry in call("/api/ps").get("models", []):
            if (entry.get("name") or entry.get("model", "")).startswith(model.split(":")[0]):
                total = entry.get("size", 0)
                in_vram = entry.get("size_vram", 0)
                return {
                    "total_mb": total / MB,
                    "vram_mb": in_vram / MB,
                    "ram_mb": max(0.0, (total - in_vram) / MB),
                    "fully_on_gpu": total > 0 and in_vram >= total * 0.99,
                }
    except Exception:  # noqa: BLE001
        pass
    return {}


def architecture(model: str) -> dict:
    """Layer count, widths and head counts, straight from the model file."""
    try:
        info = call("/api/show", {"model": model}, timeout=60)
    except Exception:  # noqa: BLE001
        return {}
    raw = info.get("model_info", {}) or {}
    details = info.get("details", {}) or {}
    family = details.get("family", "")
    def pick(suffix):
        for key, value in raw.items():
            if key.endswith(suffix):
                return value
        return None
    return {
        "parameter_size": details.get("parameter_size"),
        "quantisation": details.get("quantization_level"),
        "family": family,
        "layers": pick("block_count"),
        "embedding": pick("embedding_length"),
        "heads": pick("attention.head_count"),
        "kv_heads": pick("attention.head_count_kv"),
        "context_trained": pick("context_length"),
    }


def generate(model: str, prompt: str, predict: int, context: int) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": predict, "num_ctx": context},
        "keep_alive": "5m",
    }
    started = time.time()
    result = call("/api/generate", payload)
    result["_wall"] = time.time() - started
    return result


def rate(count, nanoseconds) -> float | None:
    if not count or not nanoseconds:
        return None
    return count / (nanoseconds / 1e9)


# --------------------------------------------------------------------------
# the benchmark
# --------------------------------------------------------------------------

FILLER = (
    "Invoice line item: on-site calibration of sensor arrays, three days, "
    "labour and parts itemised separately, payable within thirty days. "
)


def bench_model(model: str, runs: int, contexts: list[int], vram_total: float | None = None) -> dict | None:
    print(f"\n{LINE}\n  {model}\n{LINE}")

    arch = architecture(model)
    if not arch.get("layers"):
        print(f"  Could not read {model}. Is it pulled?   ollama pull {model}")
        return None
    print(f"  {arch['parameter_size']} parameters, {arch['quantisation']}, "
          f"{arch['layers']} layers, width {arch['embedding']}, "
          f"{arch['heads']} heads / {arch['kv_heads']} kv heads")

    unload_everything()
    baseline_vram = vram_used_mb()
    baseline_ram = system_ram()
    print(f"  VRAM before loading: {baseline_vram:.0f} MB" if baseline_vram is not None
          else "  VRAM before loading: unknown")

    # ---- load ------------------------------------------------------------
    load_started = time.time()
    try:
        generate(model, "hi", predict=1, context=2048)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  Could not load: {exc}")
        return None
    load_seconds = time.time() - load_started

    time.sleep(2)
    after_load_vram = vram_used_mb()
    footprint = loaded_footprint(model)
    after_load_ram = system_ram()

    weights_vram = (after_load_vram - baseline_vram) if None not in (after_load_vram, baseline_vram) else None
    print(f"  Loaded in {load_seconds:.1f}s")
    if footprint:
        where = "entirely on the GPU" if footprint.get("fully_on_gpu") else \
                f"SPILLING {footprint['ram_mb']:.0f} MB into system RAM"
        print(f"  Ollama reports {footprint['total_mb'] / 1024:.2f} GB, {where}")
    if weights_vram is not None:
        print(f"  nvidia-smi delta after load: {weights_vram:.0f} MB")

    ram_used = None
    if None not in (baseline_ram.get("available_mb"), after_load_ram.get("available_mb")):
        ram_used = baseline_ram["available_mb"] - after_load_ram["available_mb"]
        print(f"  System RAM consumed by loading: {ram_used:.0f} MB")

    # ---- generate at several context sizes -------------------------------
    measurements = []
    for context in contexts:
        # Roughly fill the context so the KV cache is actually allocated.
        target_words = max(8, int(context * 0.55))
        base = (FILLER * (target_words // len(FILLER.split()) + 1))
        base = " ".join(base.split()[:target_words])

        speeds, prompt_speeds, peaks, cached = [], [], [], 0
        for attempt in range(runs):
            # A unique preamble every time. Without it Ollama reuses the KV
            # cache from the previous run, prompt_eval_count collapses to a
            # handful of tokens, and the prompt rate reads as hundreds of
            # thousands of tokens per second, which is nonsense.
            nonce = "".join(random.choices(string.ascii_lowercase, k=24))
            prompt = (f"Reference {nonce}. " + base +
                      "\n\nIn one sentence, what is this text about?")
            with VramSampler() as sampler:
                try:
                    result = generate(model, prompt, predict=128, context=context)
                except (urllib.error.URLError, TimeoutError) as exc:
                    print(f"  {context:>6} ctx  failed: {exc}")
                    break
            speed = rate(result.get("eval_count"), result.get("eval_duration"))
            counted = result.get("prompt_eval_count") or 0
            prompt_speed = rate(counted, result.get("prompt_eval_duration"))
            if speed:
                speeds.append(speed)
            # If the server only had to process a fraction of the prompt, the
            # rest came from cache and the rate is meaningless. Throw it away
            # rather than reporting it.
            if prompt_speed and counted >= target_words * 0.5:
                prompt_speeds.append(prompt_speed)
            elif prompt_speed:
                cached += 1
            if sampler.peak:
                peaks.append(sampler.peak)

        if not speeds:
            continue
        peak = max(peaks) if peaks else None
        # Within a whisker of the card's capacity means the runtime started
        # pushing layers or cache into system RAM. Past that point the numbers
        # describe the PCIe bus, not the model.
        at_ceiling = bool(peak and vram_total and peak >= vram_total * 0.93)
        entry = {
            "context": context,
            "prompt_tokens": result.get("prompt_eval_count"),
            "generation_tps": statistics.median(speeds),
            "prompt_tps": statistics.median(prompt_speeds) if prompt_speeds else None,
            "prompt_cached_runs": cached,
            "peak_vram_mb": peak,
            "kv_vram_mb": (peak - after_load_vram) if peak and after_load_vram else None,
            "at_vram_ceiling": at_ceiling,
        }
        measurements.append(entry)
        line = f"  {context:>6} ctx   {entry['generation_tps']:6.1f} tok/s out"
        line += f"   {entry['prompt_tps']:7.0f} tok/s in" if entry["prompt_tps"] else "        cached in"
        line += f"   peak {peak:.0f} MB" if peak else ""
        print(line)
        if at_ceiling:
            print(f"           ^ AT THE VRAM CEILING ({peak:.0f} of {vram_total:.0f} MB). Everything")
            print(f"             on this line measures spill into system RAM, not the model.")

    unload_everything()

    return {
        "model": model,
        "architecture": arch,
        "load_seconds": round(load_seconds, 1),
        "baseline_vram_mb": baseline_vram,
        "after_load_vram_mb": after_load_vram,
        "weights_vram_mb": weights_vram,
        "ollama_footprint": footprint,
        "system_ram_used_mb": ram_used,
        "measurements": measurements,
    }


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------

def parse_params(text: str | None) -> float | None:
    """'8.2B' -> 8.2"""
    if not text:
        return None
    cleaned = text.strip().upper().rstrip("B")
    try:
        return float(cleaned)
    except ValueError:
        return None


def project(results: list[dict], target_b: float, gpu: dict) -> None:
    usable = [r for r in results if r and r.get("measurements")]
    if not usable:
        return

    print(f"\n{LINE}\n  What this implies for a {target_b:.0f}B model\n{LINE}")

    points = []
    for r in usable:
        params = parse_params(r["architecture"].get("parameter_size"))
        vram = (r["ollama_footprint"].get("total_mb") or r.get("weights_vram_mb"))
        best = max((m["generation_tps"] for m in r["measurements"]), default=None)
        if params and vram and best:
            points.append((params, vram, best, r["model"], r["architecture"].get("quantisation")))

    if not points:
        print("  Not enough clean measurements to extrapolate from.")
        return

    print("\n  Measured")
    print(f"  {'model':<14}{'params':>8}{'GB in VRAM':>13}{'GB per B':>11}{'best tok/s':>12}")
    for params, vram, speed, name, quant in points:
        print(f"  {name:<14}{params:>8.1f}{vram / 1024:>13.2f}{vram / 1024 / params:>11.3f}{speed:>12.1f}")

    gb_per_b = statistics.mean(v / 1024 / p for p, v, _s, _n, _q in points)
    quant = (points[0][4] or "").upper()

    print(f"\n  Memory: arithmetic, and therefore trustworthy")
    print(f"  Weights occupy a fixed number of bits each. Yours measure")
    print(f"  {gb_per_b:.3f} GB per billion parameters at {quant or 'the quantisation you used'}.")

    theoretical = BITS_PER_WEIGHT.get(quant)
    overhead = 1.0
    if theoretical:
        expected = theoretical * GIB_PER_B_PER_BIT
        overhead = gb_per_b / expected
        print(f"  Bits alone predict {expected:.3f}, so runtime overhead is {(overhead - 1) * 100:+.0f}%.")
        print(f"  That overhead is applied to the other quantisations below.\n")
    else:
        print(f"  Unrecognised quantisation, so only the measured row below is reliable.\n")

    rows = [(quant + " (as measured)", gb_per_b)]
    if theoretical:
        for label in ("Q5_K_M", "Q8_0", "F16"):
            if label != quant:
                rows.append((label, BITS_PER_WEIGHT[label] * GIB_PER_B_PER_BIT * overhead))
    for label, factor in rows:
        need = target_b * factor
        cards = need / (gpu.get("vram_total_mb") / 1024) if gpu.get("vram_total_mb") else None
        suffix = f"   ({cards:.0f} cards this size)" if cards else ""
        print(f"    {label:<24} {need:>6.0f} GB of weights{suffix}")

    print(f"\n  Context on top of that. KV cache is also arithmetic:")
    print(f"    2 (keys and values) x layers x kv_heads x head_dim x 2 bytes, per token.\n")
    for r in usable:
        arch = r["architecture"]
        clean = [m for m in r["measurements"] if m.get("kv_vram_mb") and not m.get("at_vram_ceiling")]
        predicted = None
        if arch.get("layers") and arch.get("kv_heads") and arch.get("embedding") and arch.get("heads"):
            head_dim = arch["embedding"] / arch["heads"]
            per_token = 2 * arch["layers"] * arch["kv_heads"] * head_dim * 2
            predicted = per_token * 1000 / MB
        measured = None
        if len(clean) >= 2:
            first, last = clean[0], clean[-1]
            span = last["context"] - first["context"]
            if span > 0:
                measured = (last["kv_vram_mb"] - first["kv_vram_mb"]) / span * 1000
        parts = [f"    {r['model']:<14}"]
        parts.append(f"predicted {predicted:6.0f} MB/1k" if predicted else "predicted     ?     ")
        if measured:
            error = abs(measured - predicted) / predicted * 100 if predicted else 0
            parts.append(f"measured {measured:6.0f} MB/1k   ({error:.0f}% off)")
        else:
            parts.append("measured   n/a   (no usable points below the VRAM ceiling)")
        print("   ".join(parts))

    print("\n  Speed: bandwidth, not FLOPS, and the surprise for most people")
    print("  Generating one token reads every active weight from memory once. So")
    print("  tokens per second is roughly memory bandwidth divided by model size,")
    print("  and a model twice the size runs about half as fast on the same card.")
    if len(points) >= 2:
        (p1, v1, s1, n1, _), (p2, v2, s2, n2, _) = points[0], points[1]
        predicted = s1 * (v1 / v2)
        error = abs(predicted - s2) / s2 * 100
        print(f"\n    Your own data tests that claim:")
        print(f"      {n1} runs at {s1:.1f} tok/s")
        print(f"      predicting {n2} from size ratio alone gives {predicted:.1f} tok/s")
        print(f"      actual was {s2:.1f} tok/s, so the rule is off by {error:.0f}%")
        slowest = min(points, key=lambda x: x[2])
        scale = slowest[1] / (target_b * gb_per_b * 1024)
        print(f"\n    Applying it to {target_b:.0f}B on THIS card, if it fit, which it does not:")
        print(f"      roughly {slowest[2] * scale:.2f} tok/s. Unusable, as expected.")

    print("\n  What this cannot tell you")
    print("  1. Whether the 120B is dense or mixture-of-experts. An MoE holds all")
    print("     its parameters in memory but only runs a fraction per token, so it")
    print("     needs the VRAM of a giant model and the speed of a small one. That")
    print("     single fact changes the answer more than everything measured above.")
    print("  2. Batching. These numbers are one request at a time. Serving several")
    print("     users at once raises total throughput substantially without needing")
    print("     proportionally more memory, so per-user speed is a pessimistic floor.")
    print("  3. Two points make a line whatever the truth is. 8B and 14B are close")
    print("     together and the same family; a third, further point would test the")
    print("     rule properly rather than just fitting it.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure model cost on this machine.")
    parser.add_argument("--models", default="qwen3:8b,qwen3:14b")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--contexts", default="2048,8192,16384")
    parser.add_argument("--project", type=float, default=120.0, help="Target size in billions.")
    parser.add_argument("--out", default="bench_results.json")
    args = parser.parse_args()

    gpu = gpu_facts()
    ram = system_ram()
    print("\nsyslab-server / model benchmark")
    print(f"  GPU: {gpu['name']}"
          + (f", {gpu['vram_total_mb'] / 1024:.0f} GB VRAM" if gpu["vram_total_mb"] else ""))
    if ram.get("total_mb"):
        print(f"  RAM: {ram['total_mb'] / 1024:.0f} GB total, {ram['available_mb'] / 1024:.0f} GB free")
    print(f"  Ollama: {OLLAMA_HOST}")
    print("\n  This takes a few minutes and loads each model in turn. Close other")
    print("  GPU-heavy programs first, or the numbers will be someone else's.")

    contexts = [int(c) for c in args.contexts.split(",") if c.strip()]
    results = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        try:
            results.append(bench_model(model, args.runs, contexts, gpu.get("vram_total_mb")))
        except KeyboardInterrupt:
            print("\n  Interrupted.")
            break

    project([r for r in results if r], args.project, gpu)

    payload = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "gpu": gpu,
        "ram": ram,
        "results": [r for r in results if r],
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  Full numbers written to {args.out}")
    print("  Paste this output into the chat and we can turn it into hardware options.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
