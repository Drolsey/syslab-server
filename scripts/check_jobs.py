"""Gate for the job lane: does slow work actually stay out of the way?

    .\\run.cmd scripts\\check_jobs.py
    .\\run.cmd scripts\\check_jobs.py --url http://<machine>.ts.net:8000 --token <token>

Runs against the live server over HTTP, which is the only way to test the thing
that matters. Uses the built-in `selftest` job, which sleeps and reports
progress exactly the way a diffusion run does, so this works before any image
model is installed.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LINE = "-" * 66
results: list[tuple[str, bool, str]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


class Client:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, payload: dict | None = None, timeout: int = 30):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", "X-Syslab-Token": self.token},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode()
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"detail": raw[:200]}
        except (urllib.error.URLError, TimeoutError) as exc:
            return 0, {"detail": str(exc)}


def wait_for(client: Client, job_id: str, limit: float = 60.0) -> dict:
    deadline = time.time() + limit
    body: dict = {}
    while time.time() < deadline:
        _, body = client.call("GET", f"/api/jobs/{job_id}")
        if body.get("status") in {"done", "failed", "cancelled"}:
            return body
        time.sleep(0.3)
    return body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    url, token = args.url, args.token
    if not url or not token:
        from app.config import APP_PORT, APP_TOKEN
        url = url or f"http://127.0.0.1:{APP_PORT}"
        token = token or APP_TOKEN
    client = Client(url, token)

    print("\nsyslab-server / job lane check")
    print(f"Target: {url}")

    status, health = client.call("GET", "/api/health")
    if status != 200:
        print(f"\n  Cannot reach the app: HTTP {status} {health.get('detail', '')}\n")
        return 1
    print(f"  Job types available: {', '.join(health.get('job_kinds', [])) or 'none'}")

    # ---- 1. a job runs -------------------------------------------------
    section("1. Work is accepted and completed")
    status, job = client.call("POST", "/api/jobs",
                              {"kind": "selftest", "params": {"seconds": 3, "steps": 10}})
    record("Accepted with 202 and an id", status == 202 and bool(job.get("id")), f"HTTP {status}")
    if status != 202:
        print(f"  {job}\n")
        return 1
    first = job["id"]

    time.sleep(1.0)
    _, mid = client.call("GET", f"/api/jobs/{first}")
    record("Reports progress while running",
           mid.get("status") == "running" and 0 < mid.get("progress", 0) < 1,
           f"{mid.get('progress', 0) * 100:.0f}% after 1s")

    # ---- 2. the point of the whole thing -------------------------------
    section("2. A running job does not block anything else")
    print("  Asking a question while that job is still going...")
    started = time.time()
    status, reply = client.call("POST", "/api/chat",
                                {"message": "Reply with one word: ready.", "history": []},
                                timeout=120)
    elapsed = time.time() - started
    remaining = wait_for(client, first, limit=1)
    record("Chat answered while a job was running", status == 200, f"HTTP {status}, {elapsed:.1f}s")
    if status == 200:
        print(f"        the model said: {reply.get('answer', '')[:80]!r}")
    record("It did not wait for the job to finish",
           remaining.get("status") == "running" or elapsed < 3.0,
           "the job was still going when chat replied" if remaining.get("status") == "running"
           else f"chat took {elapsed:.1f}s")

    # ---- 3. queueing ---------------------------------------------------
    section("3. Several requests queue rather than fighting for the GPU")
    ids = []
    for _ in range(3):
        _, body = client.call("POST", "/api/jobs",
                              {"kind": "selftest", "params": {"seconds": 2, "steps": 8}})
        ids.append(body["id"])
    time.sleep(0.6)
    _, snapshot = client.call("GET", "/api/jobs")
    record("Only one runs at a time", snapshot.get("running", 0) <= snapshot.get("workers", 1),
           f"{snapshot.get('running')} running, {snapshot.get('queued')} waiting")
    positions = [client.call("GET", f"/api/jobs/{i}")[1].get("position_in_queue") for i in ids]
    ordered = [p for p in positions if p is not None]
    record("Waiting jobs know their place in the queue", ordered == sorted(ordered),
           f"positions {positions}")

    # ---- 4. cancelling --------------------------------------------------
    section("4. Cancelling")
    last = ids[-1]
    status, cancelled = client.call("DELETE", f"/api/jobs/{last}")
    record("A queued job can be cancelled", status == 200 and cancelled.get("status") == "cancelled",
           f"HTTP {status}")

    # ---- 5. many clients at once ---------------------------------------
    section("5. Several clients polling at once")
    codes: list[int] = []
    def poll():
        codes.append(client.call("GET", "/api/jobs")[0])
    threads = [threading.Thread(target=poll) for _ in range(8)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    record("Eight simultaneous pollers all served", codes == [200] * 8,
           f"{len(codes)} replies in {time.time() - started:.2f}s")

    # ---- 6. honesty about what is not built yet ------------------------
    section("6. Image generation, which is wired but not installed")
    _, body = client.call("POST", "/api/jobs",
                          {"kind": "generate_image", "params": {"prompt": "a red bicycle"}})
    final = wait_for(client, body.get("id", ""), limit=20)
    record("Says plainly that the model is missing",
           final.get("status") == "failed" and "not installed" in (final.get("error") or ""),
           final.get("error", "")[:70])

    for job_id in ids:
        client.call("DELETE", f"/api/jobs/{job_id}")

    # ---- summary --------------------------------------------------------
    section("Gate")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n  {passed} of {len(results)} checks passed")
    if passed != len(results):
        print("\n  Paste this output back into the chat.\n")
        return 1
    print("\n  The job lane holds. Two clients can use this at once: conversations")
    print("  are answered while generation runs, and generation queues instead of")
    print("  competing for the card.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
