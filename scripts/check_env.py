"""Phase 00: report what this machine actually has, and pick a starting model.

Stdlib only, so it runs on a fresh machine before anything is installed.

    python scripts/check_env.py
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

LINE = "-" * 62


def run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def check_os() -> None:
    section("Operating system")
    print(f"  {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  Python {platform.python_version()} at {sys.executable}")
    if sys.version_info < (3, 10):
        print("  WARNING: this project targets Python 3.10 or newer.")


def check_gpu() -> int | None:
    section("GPU")
    if shutil.which("nvidia-smi") is None:
        print("  nvidia-smi not found on PATH.")
        print("  On Windows it usually lives in C:\\Windows\\System32.")
        print("  If it is genuinely missing, install the NVIDIA driver first.")
        return None

    name = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    total = run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    free = run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    driver = run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])

    if not total:
        print("  nvidia-smi ran but reported no GPU memory. Full output:")
        print(run(["nvidia-smi"]) or "  (no output)")
        return None

    total_mb = int(total.splitlines()[0])
    print(f"  Card:     {name.splitlines()[0] if name else 'unknown'}")
    print(f"  Driver:   {driver.splitlines()[0] if driver else 'unknown'}")
    print(f"  VRAM:     {total_mb} MiB total ({total_mb / 1024:.1f} GB)")
    if free:
        print(f"  Free now: {int(free.splitlines()[0])} MiB")
    return total_mb


def recommend(total_mb: int | None) -> None:
    section("Recommended starting model")
    if total_mb is None:
        print("  Cannot tell without a VRAM reading.")
        print("  Safe choice until you know: qwen3:4b")
        return
    gb = total_mb / 1024
    if gb >= 11:
        print(f"  {gb:.1f} GB VRAM. Start with qwen3:8b; you have headroom to try")
        print("  a 14b model later if 8b feels weak at multi-step tool calls.")
        print("  OLLAMA_MODEL=qwen3:8b")
    elif gb >= 7.5:
        print(f"  {gb:.1f} GB VRAM. qwen3:8b fits at 4-bit quantisation with a")
        print("  modest context window. This is the plan's default.")
        print("  OLLAMA_MODEL=qwen3:8b")
    else:
        print(f"  {gb:.1f} GB VRAM. Too tight for 8b once context is added.")
        print("  Start on the 4b and revisit if you upgrade the card.")
        print("  OLLAMA_MODEL=qwen3:4b")


def check_tools() -> None:
    section("Tools already installed")
    for tool, note in [
        ("ollama", "Phase 01"),
        ("git", "already in use"),
        ("tailscale", "Phase 06, not needed yet"),
    ]:
        path = shutil.which(tool)
        print(f"  {tool:<10} {'found: ' + path if path else 'not installed (' + note + ')'}")


def main() -> None:
    print("\nsyslab-server / Phase 00 environment check")
    check_os()
    total_mb = check_gpu()
    recommend(total_mb)
    check_tools()
    section("Gate")
    print("  Phase 00 passes once you know your real VRAM number and your OS.")
    print("  Paste this whole output back into the chat and we move to Phase 01.\n")


if __name__ == "__main__":
    main()
