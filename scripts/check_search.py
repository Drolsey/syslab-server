"""Gate for document search: can it find a file by its contents in one call?

    .\\run.cmd scripts\\check_search.py
    .\\run.cmd scripts\\check_search.py --rebuild        rebuild from scratch first
    .\\run.cmd scripts\\check_search.py --haystack 60    make a bigger pile to search

Builds a pile of near-identical decoy invoices with one distinctive document
hidden among them, then finds it. That is the case the index exists for: before
it, the model had to open documents one at a time and would run out of steps
after about nine.
"""

from __future__ import annotations

import argparse
import random
import string
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deps import require  # noqa: E402

require("pymupdf", "openpyxl", "reportlab")

from app import search, tools  # noqa: E402
from app import context  # noqa: E402
from app.config import (  # noqa: E402
    BOOTSTRAP_TENANT,
    MAX_TOOL_STEPS,
    data_dir,
    ensure_data_dir,
    index_path,
)

LINE = "-" * 66
results: list[tuple[str, bool, str]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--haystack", type=int, default=40)
    parser.add_argument("--keep", action="store_true", help="leave the decoy files behind")
    args = parser.parse_args()

    ensure_data_dir()
    tag = "".join(random.choices(string.digits, k=4))
    needle_ref = f"ZQ-{tag}-RARE"
    engineer = "Yusra Bekele"

    print("\nsyslab-server / document search check")
    print(f"  Data folder: {data_dir()}")
    print(f"  Index:       {index_path()}")

    # ---- 1. the index itself -------------------------------------------
    section("1. The index")
    try:
        before = search.status()
        record("Index opens", True, f"{before['documents']} document(s) already in it")
    except search.SearchError as exc:
        record("Index opens", False, str(exc))
        return 1

    if args.rebuild or before["not_yet_indexed"]:
        pending = len(before["not_yet_indexed"])
        print(f"  {'Rebuilding from scratch' if args.rebuild else f'{pending} file(s) not yet indexed, rebuilding'}...")
        outcome = search.rebuild()
        record("Rebuilt from the files on disk", True,
               f"{outcome['indexed']} of {outcome['files_seen']} in {outcome['seconds']}s")
        if outcome["skipped"]:
            print(f"        skipped (no text layer): {', '.join(outcome['skipped'][:5])}")

    # ---- 2. build a haystack -------------------------------------------
    section(f"2. A pile of {args.haystack} near-identical documents, one of them different")
    made: list[str] = []
    started = time.time()
    for n in range(args.haystack):
        name = f"decoy_{tag}_{n:03d}.pdf"
        tools.write_pdf(
            name,
            title=f"Invoice AA-{tag}-{n:03d}",
            body="Routine monthly maintenance visit. Standard rate applied. "
                 "Issued to Northwind Trading. Payable within thirty days.",
        )
        made.append(name)
    needle = f"needle_{tag}.pdf"
    tools.write_pdf(
        needle,
        title=f"Invoice {needle_ref}",
        body=f"Emergency recalibration of the cryogenic manifold, attended by {engineer}. "
             "This is the only document mentioning a manifold.",
    )
    made.append(needle)
    print(f"  Wrote {len(made)} files in {time.time() - started:.1f}s")

    started = time.time()
    outcome = search.rebuild()
    record("Indexed the whole folder", outcome["indexed"] >= len(made),
           f"{outcome['indexed']} documents in {outcome['seconds']}s")

    # ---- 3. the point ---------------------------------------------------
    section("3. Finding the one that is different")
    for description, query in [
        ("a phrase only it contains", "cryogenic manifold"),
        ("a person's name", engineer),
        ("its reference number", needle_ref),
    ]:
        started = time.time()
        found = search.search(query, limit=3)
        elapsed = (time.time() - started) * 1000
        top = found["results"][0]["name"] if found["results"] else "(nothing)"
        record(f"Found it by {description}", top == needle,
               f"{elapsed:.0f} ms, top hit {top}")
        if found["results"]:
            print(f"        {found['results'][0]['snippet'][:90]}")

    section("4. What that replaces")
    print(f"  Files in the folder:        {outcome['files_seen']}")
    print(f"  Calls to find one by hand:  up to {outcome['files_seen']} reads")
    print(f"  MAX_TOOL_STEPS allows:      {MAX_TOOL_STEPS}")
    print(f"  Calls with search_files:    1, then 1 read")
    record("One call replaces a folder-sized scan", outcome["files_seen"] > MAX_TOOL_STEPS,
           f"the old approach gives up after {MAX_TOOL_STEPS} of {outcome['files_seen']} files")

    # ---- 5. it stays a cache -------------------------------------------
    section("5. It is still a cache")
    index_path().unlink(missing_ok=True)
    record("Deleting the index loses nothing", search.status()["documents"] == 0,
           "index removed")
    again = search.rebuild()
    record("A rebuild restores it completely", search.search("cryogenic manifold")["count"] == 1,
           f"{again['indexed']} documents back in {again['seconds']}s")

    # ---- clean up -------------------------------------------------------
    if not args.keep:
        section("Tidying up")
        removed = 0
        for name in made:
            path = data_dir() / name
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        search.rebuild()
        print(f"  Removed {removed} of {len(made)} test files and reindexed.")
        if removed < len(made):
            print(f"  {len(made) - removed} could not be deleted; remove them by hand.")

    # ---- summary --------------------------------------------------------
    section("Gate")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n  {passed} of {len(results)} checks passed")
    if passed != len(results):
        print("\n  Paste this output back into the chat.\n")
        return 1
    print("\n  Search holds. Ask the assistant for a document by what is in it")
    print("  rather than by its name and it will find it in one call.\n")
    return 0


if __name__ == "__main__":
# Gate scripts call the tool functions directly, with no HTTP request to say
# whose data this is, so they name a tenant themselves. BOOTSTRAP_TENANT is the
# one holding the data this install started with.
    with context.use_tenant(BOOTSTRAP_TENANT):
        sys.exit(main())
