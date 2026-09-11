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

import _fixtures  # noqa: E402

from app import context, ingest, intake, parse, search, sources, tools  # noqa: E402
from app.config import (  # noqa: E402
    BOOTSTRAP_TENANT,
    data_dir,
    derived_dir,
    ensure_data_dir,
)

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "corpus"

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

    # ---- 5. the formats table --------------------------------------------
    #
    # Step 4.2's gate. app/parse.py's claim is that a new format is a ROW, and
    # a row nothing ever reads is a claim rather than a fact. Two of them
    # shipped broken in 4.2 part one for exactly that reason: docling-slim
    # imports a format's reader when the FILE is read rather than when the
    # backend module is imported, so .pptx and .md passed every import check
    # this project had and then recorded every such document as damaged.
    #
    # So this reads one file of every suffix in the table and looks for a
    # sentinel string INSIDE the extracted text. Asserting only that ingestion
    # "succeeded" is what let that through: a producer that writes an empty
    # artifact succeeds.
    section("5. Every format the parser claims, read end to end")

    samples: dict[str, bytes] = {
        ".pdf":  _fixtures.build_pdf("Formats", [f"ZARAFMT{tag}PDF in the body"]),
        ".xlsx": _fixtures.build_xlsx([[f"ZARAFMT{tag}XLSX", 42]]),
        ".xlsm": _fixtures.build_xlsx([[f"ZARAFMT{tag}XLSM", 42]]),
        ".pptx": _fixtures.build_pptx([f"ZARAFMT{tag}PPTX", "a second line"]),
        ".docx": (CORPUS / "formats" / "format_docx.docx").read_bytes(),
        ".html": f"<html><body><p>ZARAFMT{tag}HTML here</p></body></html>".encode(),
        ".htm":  f"<html><body><p>ZARAFMT{tag}HTM here</p></body></html>".encode(),
        ".csv":  f"col_a,col_b\nZARAFMT{tag}CSV,7\n".encode(),
        ".md":   f"# Heading\n\nZARAFMT{tag}MD in a paragraph.\n".encode(),
        ".txt":  f"ZARAFMT{tag}TXT plain words.\n".encode(),
    }
    missing = sorted(parse.handles() - set(samples))
    record(
        "Every suffix in the table has a sample to read",
        not missing,
        f"no sample for {', '.join(missing)}" if missing else f"{len(samples)} formats",
    )

    # The .docx comes from the corpus rather than being generated, so its
    # sentinel is the corpus's own and not this run's tag.
    docx_sentinel = "ZARAMANDA-DOCX-9028"

    for suffix in sorted(samples):
        name = f"formats_{tag}{suffix}"
        (data_dir() / name).write_bytes(samples[suffix])
        made.append(name)
        report = ingest.ingest(name)
        wanted = docx_sentinel if suffix == ".docx" else f"ZARAFMT{tag}{suffix[1:].upper()}"

        artifact = derived_dir() / "text" / name / "text.txt"
        text = artifact.read_text(encoding="utf-8") if artifact.is_file() else ""
        detail = ""
        if report["failed"]:
            detail = f"failed: {list(report['failed'].values())[0][:70]}"
        elif report["unavailable"]:
            detail = f"unavailable: {list(report['unavailable'].values())[0][:70]}"
        elif not text:
            detail = "ingested, and the text artifact is empty or absent"
        record(f"{suffix:6} is read, and the words inside it arrive", wanted in text, detail)

    # ---- 6. the two files that must NOT behave the same -------------------
    #
    # The distinction 4.2 was built to make. Before it, a corrupt file and a
    # scan both produced "" and both were recorded as a success, so a folder
    # with one damaged document looked exactly like a folder with one scan.
    section("6. A damaged file and a scan are told apart")

    pack = {
        "corrupt": CORPUS / "formats" / "format_corrupt.pdf",
        "scan": CORPUS / "formats" / "format_scanned.pdf",
    }
    outcomes = {}
    for kind, source in pack.items():
        name = f"formats_{tag}_{kind}.pdf"
        shutil.copy2(source, data_dir() / name)
        made.append(name)
        outcomes[kind] = ingest.ingest(name)

    record(
        "The damaged file fails, loudly and against itself",
        "text" in outcomes["corrupt"]["failed"],
        (list(outcomes["corrupt"]["failed"].values())[0][:70]
         if outcomes["corrupt"]["failed"] else "it was recorded as a success"),
    )
    record(
        "The scan does NOT fail -- it is a good file with no text layer",
        not outcomes["scan"]["failed"] and not outcomes["scan"]["unavailable"],
        "an OCR producer is what this is waiting for, not a repair",
    )
    # status()["producers"] is a dict KEYED by producer name, not a list of
    # rows. Read from the real shape rather than from what the name suggests --
    # the lesson check_retrieval paid for by scoring every query 0.000.
    # The corrupt file goes NOW, not in the tidy-up at the end, and this is a
    # trap that was sprung rather than foreseen. It is the one file here whose
    # whole purpose is to fail, it lives in the install's real data folder, and
    # a run that dies after writing it leaves it there -- where section 2's
    # "Nothing failed on the way" then fails on every later run, for a reason
    # that has nothing to do with what that section tests. Removing it the
    # moment it has been asserted on means the window is two statements wide
    # instead of the rest of the script.
    corrupt_name = f"formats_{tag}_corrupt.pdf"
    (data_dir() / corrupt_name).unlink(missing_ok=True)
    made.remove(corrupt_name)
    ingest.forget_missing()

    scan_rows = ingest.status(f"formats_{tag}_scan.pdf")["producers"]
    scan_detail = scan_rows.get("text", {}).get("detail") or ""
    record(
        "And the pipeline says which of the two it is, rather than 'empty'",
        "no text layer" in scan_detail,
        scan_detail[:70] or "no detail recorded",
    )

    # ---- 7. the source seam ----------------------------------------------
    #
    # Step 4.2's other half. The seam's whole claim is that the pipeline no
    # longer knows where material comes from, and the cheap honest way to check
    # that is to confirm the pipeline agrees with the source about what exists.
    section("7. The pipeline reaches its material through the source seam")

    source = sources.active()
    record(
        "One source is registered, and it is the tenant's own folder",
        source.name == "files",
        f"{', '.join(sorted(sources.registered()))}",
    )
    listed = {item.id for item in source.list()}
    on_disk = {p.name for p in data_dir().iterdir() if p.is_file()}
    record(
        "It lists exactly what is in the folder, unfiltered",
        listed == on_disk,
        f"{len(listed)} item(s)" if listed == on_disk
        else f"source {sorted(listed - on_disk)}, disk {sorted(on_disk - listed)}",
    )
    record(
        "And fetching one gives back the bytes that are there",
        source.fetch(staying).read_bytes() == (data_dir() / staying).read_bytes(),
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
