"""Gate for the ingestion contract: is derived/ really disposable?

    .\\run.cmd scripts\\check_ingest.py
    .\\run.cmd scripts\\check_ingest.py --keep     leave the test files behind

Step 2.4's gate. Two properties, and both of them are promises the rest of the
design leans on:

  1. **derived/ deleted entirely reconstructs from data/.** Every decision in
     `docs/plans/step-02-ingestion-contract.md` about what may live under
     derived/ rests on this. A producer that ever makes something which cannot
     be regenerated breaks it silently, and this is what would notice.
  2. **A deleted source file leaves nothing behind.** Artifacts are written
     when a file arrives and never when one leaves, so without a sweep a
     deleted document owns a manifest row and a folder of bytes for ever.

Works on THIS INSTALL's own data folder, as the other gates do, and puts back
what it made. It runs against the real producers rather than a fake one --
tests/test_ingest.py covers the pipeline with a fake, and the thing worth
checking here is that the real one behaves the same way.
"""

from __future__ import annotations

import argparse
import random
import shutil
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deps import require  # noqa: E402

require("pymupdf", "openpyxl", "reportlab")

from app import context, ingest, intake, search, tools  # noqa: E402
from app.config import (  # noqa: E402
    BOOTSTRAP_TENANT,
    data_dir,
    derived_dir,
    ensure_data_dir,
)

LINE = "-" * 66
results: list[tuple[str, bool, str]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def found(phrase: str) -> list[str]:
    """Which documents the index offers for this phrase, by name.

    By NAME, not by count. search() falls back from "all terms" to "any term",
    so a phrase carrying the run's tag matches every file this script wrote --
    and a count assertion then fails for a reason that has nothing to do with
    what is being tested. It did.
    """
    return [row["name"] for row in search.search(phrase)["results"]]


def artifacts_for(name: str) -> list[str]:
    """Every producer directory holding output for this source file."""
    root = derived_dir()
    if not root.is_dir():
        return []
    return sorted(
        f"{folder.name}/{name}"
        for folder in root.iterdir()
        if folder.is_dir() and (folder / name).is_dir()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the test files behind")
    args = parser.parse_args()

    ensure_data_dir()
    tag = "".join(random.choices(string.digits, k=4))
    staying = f"ingest_{tag}_staying.pdf"
    leaving = f"ingest_{tag}_leaving.pdf"
    phrase = f"Quenched bracket {tag} torque revision"

    print("\nsyslab-server / ingestion contract check")
    print(f"  Data folder: {data_dir()}")
    print(f"  Derived:     {derived_dir()}")
    print(f"  Producers:   {', '.join(sorted(ingest.registered())) or 'none'}")

    made: list[str] = []

    # ---- 1. a file arrives ----------------------------------------------
    section("1. A file arrives")
    for name, body in ((staying, "Routine inspection, nothing unusual."), (leaving, phrase)):
        tools.write_pdf(name, title=name, body=body)
        made.append(name)
    print(f"  Wrote {staying} and {leaving} through the ordinary tool path.")

    for name in (staying, leaving):
        intake.arrived(data_dir() / name)

    state = ingest.status(leaving)
    record(
        "A new file is produced and recorded",
        state["ready"] and not state["failed"],
        f"{len(state['producers'])} producer(s), ready={state['ready']}",
    )
    record(
        "Its artifacts are on disk",
        artifacts_for(leaving) != [],
        ", ".join(artifacts_for(leaving)),
    )
    record(
        "And the index can find it by what is inside it",
        leaving in found(phrase),
    )

    # ---- 2. derived/ is disposable --------------------------------------
    section("2. derived/ deleted entirely, and rebuilt from data/")
    before = {name: artifacts_for(name) for name in (staying, leaving)}
    shutil.rmtree(derived_dir())
    record(
        "Deleting it leaves the documents alone",
        (data_dir() / staying).is_file() and (data_dir() / leaving).is_file(),
    )
    record(
        "And the pipeline says so, rather than claiming ready",
        ingest.status(leaving)["ready"] is False,
    )

    summary = ingest.rebuild()
    after = {name: artifacts_for(name) for name in (staying, leaving)}
    record(
        "A rebuild reconstructs every artifact",
        after == before and all(after.values()),
        f"{summary['ran']} produced from {summary['files_seen']} file(s) "
        f"in {summary['seconds']}s",
    )
    record(
        "Nothing failed on the way",
        summary["failed"] == 0 and not summary["unavailable"],
    )

    # ---- 3. a file leaves ------------------------------------------------
    section("3. A file leaves")
    (data_dir() / leaving).unlink()
    made.remove(leaving)

    swept = ingest.forget_missing()
    record(
        "The sweep names the file that went",
        swept["gone"] == [leaving],
        ", ".join(swept["gone"]) or "nothing",
    )
    record(
        "Its artifacts are gone",
        artifacts_for(leaving) == [],
    )
    record(
        "Its manifest rows are gone",
        swept["rows"] > 0 and ingest.status(staying)["ready"],
        f"{swept['rows']} row(s) dropped",
    )
    record(
        "The other document is untouched",
        artifacts_for(staying) == before[staying] and ingest.status(staying)["ready"],
    )

    search.rebuild()
    record(
        "And the index no longer offers it",
        leaving not in found(phrase),
    )

    # ---- 4. output with no row behind it ---------------------------------
    section("4. Output left by a run that never recorded itself")
    ghost = derived_dir() / "text" / f"ghost_{tag}.pdf"
    ghost.mkdir(parents=True, exist_ok=True)
    (ghost / "text.txt").write_text("from a run that died halfway", encoding="utf-8")

    swept = ingest.forget_missing()
    record(
        "Swept as well, though no row ever named it",
        not ghost.exists() and f"ghost_{tag}.pdf" in swept["gone"],
        "sweeping only the manifest would leave this with nothing pointing at it",
    )

    # ---- tidying up ------------------------------------------------------
    if not args.keep:
        section("Tidying up")
        removed = 0
        for name in made:
            try:
                (data_dir() / name).unlink()
                removed += 1
            except OSError:
                pass
        ingest.forget_missing()
        search.rebuild()
        print(f"  Removed {removed} of {len(made)} test file(s), swept and reindexed.")
        record(
            "The folder is as it was found",
            artifacts_for(staying) == [] and not (data_dir() / staying).exists(),
        )

    # ---- summary ---------------------------------------------------------
    section("Gate")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n  {passed} of {len(results)} checks passed")
    if passed != len(results):
        print("\n  Paste this output back into the chat.\n")
        return 1
    print("\n  The contract holds. Everything under derived/ can be thrown away")
    print("  and made again, and a document that leaves takes its own with it.\n")
    return 0


if __name__ == "__main__":
    with context.use_tenant(BOOTSTRAP_TENANT):
        raise SystemExit(main())
