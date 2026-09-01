"""Phase 07 gate: prove the whole chain, ideally from another machine.

Standard library only, and it builds its own test files, so you can copy this
file plus scripts/_fixtures.py onto your laptop and run it there. That is the
point: the thing worth proving is the path from a machine that is not the one
doing the work.

    python scripts/check_endtoend.py
    python scripts/check_endtoend.py --url http://desktop-r0g7ikh.tail85a02b.ts.net:8000 --token <token>

Every fact in the test files is invented for this run, so a correct answer
cannot have come from anywhere but the file.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _fixtures import build_pdf, build_xlsx, xlsx_contains  # noqa: E402

LINE = "-" * 62
results: list[tuple[str, bool, str]] = []
timings: list[tuple[str, float]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


# --------------------------------------------------------------------------
# a very small http client, so this file needs nothing installed
# --------------------------------------------------------------------------

class Client:
    def __init__(self, base: str, token: str, timeout: int = 300):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _open(self, request: urllib.request.Request):
        request.add_header("X-Syslab-Token", self.token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            return 0, str(exc).encode()

    def get(self, path: str):
        return self._open(urllib.request.Request(self.base + path))

    def post_json(self, path: str, payload: dict):
        return self._open(urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ))

    def upload(self, path: str, filename: str, content: bytes):
        boundary = "----syslab" + "".join(random.choices(string.ascii_letters, k=16))
        kind = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {kind}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        return self._open(urllib.request.Request(
            self.base + path,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        ))


def succeeded(steps: list[dict], tool: str) -> bool:
    """Called is not the same as worked. A failed call must never read as a pass."""
    return any(step["tool"] == tool and step["ok"] for step in steps)


def ask(client: Client, question: str) -> tuple[str, list[dict], float]:
    print(f'\n  Q: "{question}"')
    started = time.time()
    status, raw = client.post_json("/api/chat", {"message": question, "history": []})
    elapsed = time.time() - started
    if status != 200:
        print(f"  HTTP {status}: {raw.decode('utf-8', errors='replace')[:200]}")
        return "", [], elapsed
    data = json.loads(raw)
    steps = data.get("steps", [])
    for step in steps:
        mark = "ok  " if step["ok"] else "FAIL"
        print(f"      [{mark}] {step['tool']}({json.dumps(step['arguments'])[:110]})")
    answer = data.get("answer", "")
    print(f"  A: {answer[:300]}{'...' if len(answer) > 300 else ''}   ({elapsed:.1f}s)")
    return answer, steps, elapsed


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 07 end-to-end proof.")
    parser.add_argument("--url", default=None, help="e.g. http://your-machine.ts.net:8000")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    url, token = args.url, args.token
    if not url or not token:
        try:
            from app.config import APP_PORT, APP_TOKEN
            url = url or f"http://127.0.0.1:{APP_PORT}"
            token = token or APP_TOKEN
        except Exception:  # noqa: BLE001 - running as a lone file elsewhere
            print("\n  Pass --url and --token when running this outside the project folder.\n")
            return 2
    if not token:
        print("\n  No token. Pass --token, or run scripts/new_token.py first.\n")
        return 2

    client = Client(url, token)
    tag = "".join(random.choices(string.digits, k=4))
    pdf_name = f"e2e_invoice_{tag}.pdf"
    xlsx_name = f"e2e_sales_{tag}.xlsx"
    reference = f"QT-{tag}-K"
    engineer = "Farah Nasser"
    total = f"{random.randint(20, 40)},{random.randint(100, 999)}"

    print("\nsyslab-server / Phase 07 end-to-end check")
    print(f"Target: {url}")
    print(f"Facts invented for this run: {reference}, {engineer}, {total} AED")

    # ---- 1. the connection itself -------------------------------------
    section("1. The server, over this address")
    status, raw = client.get("/api/health")
    if status != 200:
        record("Reachable and signed in", False,
               f"HTTP {status}: {raw.decode('utf-8', errors='replace')[:150]}")
        print("\n  Nothing else can be tested until this works.\n")
        return 1
    health = json.loads(raw)
    record("Reachable and signed in", True, f"model {health.get('model')}")

    # ---- 2. upload a PDF and ask about it -----------------------------
    section("2. A PDF it has never seen")
    pdf = build_pdf(
        f"Invoice {reference}",
        [
            "Issued to Meridian Logistics.",
            f"Engineer on site: {engineer}",
            "Labour: 14,900 AED",
            "Parts: 6,120 AED",
            f"Total due: {total} AED",
        ],
    )
    status, raw = client.upload("/api/upload", pdf_name, pdf)
    record("PDF uploaded", status == 200, f"HTTP {status}, {len(pdf) / 1024:.1f} KB")
    if status != 200:
        return 1

    answer, steps, elapsed = ask(
        client, f"What is the total due on {pdf_name}, and who was the engineer on site?"
    )
    timings.append(("read a PDF and answer", elapsed))
    used = {s["tool"] for s in steps}
    record("It read the file rather than guessing", succeeded(steps, "read_pdf"),
           f"tools: {', '.join(used) or 'none'}")
    record("It quoted the real total", total.replace(",", "") in answer.replace(",", ""), f"expected {total}")
    record("It named the real engineer", "Farah" in answer, f"expected {engineer}")

    # ---- 3. upload a spreadsheet and have it edited --------------------
    section("3. A spreadsheet it must change")
    xlsx = build_xlsx(
        [["Region", "Revenue", "Engineers"],
         ["North", 21400, 3],
         ["Central", 15200, 2],
         ["South", 11750, 4]],
        sheet_name="Q1",
    )
    status, raw = client.upload("/api/upload", xlsx_name, xlsx)
    record("Spreadsheet uploaded", status == 200, f"HTTP {status}")
    if status != 200:
        return 1

    answer, steps, elapsed = ask(
        client,
        f"Add a row to {xlsx_name} for the East region with revenue 9300 and 2 engineers.",
    )
    timings.append(("read and write a spreadsheet", elapsed))
    used = {s["tool"] for s in steps}
    record("It wrote to the spreadsheet", succeeded(steps, "write_excel"),
           f"tools: {', '.join(used) or 'none'}")

    # ---- 4. download it back and check the bytes ----------------------
    section("4. The file on disk, not the model's word for it")
    status, downloaded = client.get(f"/api/files/{xlsx_name}")
    record("Downloaded the edited file", status == 200, f"HTTP {status}, {len(downloaded)} bytes")
    if status == 200:
        record("The new row is really in the file", xlsx_contains(downloaded, "East") and
               xlsx_contains(downloaded, "9300"),
               "checked inside the workbook, independent of anything the model said")

    # ---- 5. have it produce a PDF --------------------------------------
    section("5. A document it writes itself")
    summary_name = f"e2e_summary_{tag}.pdf"
    answer, steps, elapsed = ask(
        client,
        f"Write a short PDF called {summary_name} summarising the invoice {pdf_name}. "
        "Include the reference number and the total.",
    )
    timings.append(("write a PDF", elapsed))
    used = {s["tool"] for s in steps}
    record("It created a PDF", succeeded(steps, "write_pdf"),
           f"tools: {', '.join(used) or 'none'}")
    status, produced = client.get(f"/api/files/{summary_name}")
    record("The PDF downloads and is a real PDF", status == 200 and produced[:5] == b"%PDF-",
           f"HTTP {status}, {len(produced)} bytes")

    # ---- summary -------------------------------------------------------
    section("Timings")
    for label, seconds in timings:
        print(f"  {seconds:6.1f}s   {label}")

    section("Gate")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n  {passed} of {len(results)} checks passed")
    print(f"\n  Test files left in your data folder: {pdf_name}, {xlsx_name}, {summary_name}")
    print("  Delete them whenever; they are only useful for reading this output back.")

    if passed != len(results):
        print("\n  Phase 07 does not pass yet. Paste this output back into the chat.\n")
        return 1

    print("\n  The machine half of Phase 07 passes.")
    if "127.0.0.1" in url or "localhost" in url:
        print("\n  But this ran against 127.0.0.1, which proves the software and not the")
        print("  point of the whole build. Run it again from your laptop, on a network")
        print("  that is not your home one:")
        print("    python scripts/check_endtoend.py --url http://<your-machine>.ts.net:8000 --token <token>")
    else:
        print(f"\n  And it ran against {url}, which is the real thing.")
    print("\n  The other half is yours: your own PDF, your own spreadsheet, a question")
    print("  you actually need answered. That is the only test that tells you whether")
    print("  this is useful rather than merely working.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
