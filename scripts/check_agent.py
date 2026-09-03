"""Phase 03 gate: does the model actually use the tools, or does it guess?

Builds two files with facts invented for this test, so a correct answer can
only have come from reading them. Then asks real questions and checks both the
answer and the tools the model chose along the way.

    py scripts/check_agent.py
    py scripts/check_agent.py --verbose    # show the full trace
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deps import require  # noqa: E402

require("pymupdf", "openpyxl", "reportlab")

from app import agent, tools  # noqa: E402
from app.config import DATA_DIR, OLLAMA_MODEL, ensure_data_dir  # noqa: E402
from app.llm import LlmError  # noqa: E402

LINE = "-" * 62
PDF = "phase03_invoice.pdf"
XLSX = "phase03_sales.xlsx"

# Facts that exist nowhere else, so the model cannot know them without reading.
INVOICE_REF = "SL-4417-B"
INVOICE_TOTAL = "18,640"
ENGINEER = "Rania Haddad"

results: list[tuple[str, bool, str]] = []
VERBOSE = False


def build_fixtures() -> None:
    ensure_data_dir()
    tools.write_pdf(
        PDF,
        title=f"Invoice {INVOICE_REF}",
        body=(
            f"Issued to Meridian Logistics on 14 August 2026.\n\n"
            "# Work performed\n\n"
            f"On-site calibration of four sensor arrays, carried out by {ENGINEER} "
            "over three days.\n\n"
            "# Amounts\n\n"
            "Labour: 11,200 AED\n"
            "Parts: 5,900 AED\n"
            "Transport: 1,540 AED\n\n"
            f"Total due: {INVOICE_TOTAL} AED, payable within 30 days."
        ),
    )
    tools.write_excel(
        XLSX,
        rows=[
            ["North", 26800, 3],
            ["Central", 18450, 2],
            ["South", 13900, 4],
        ],
        headers=["Region", "Revenue", "Engineers"],
        sheet="Q1",
        mode="overwrite",
    )


def show_trace(outcome: dict) -> None:
    for step in outcome["steps"]:
        mark = "ok  " if step["ok"] else "FAIL"
        print(f"      [{mark}] {step['tool']}({step['arguments']})")
        if not step["ok"]:
            print(f"             -> {step['error']}")
        elif VERBOSE:
            print(f"             -> {str(step['result'])[:200]}")


def scenario(name: str, question: str, check, expect_tools: set[str] | None = None) -> None:
    print(f"\n{name}\n{LINE}")
    print(f'  Q: "{question}"')
    started = time.time()
    try:
        outcome = agent.ask(question)
    except LlmError as exc:
        print(f"  {exc}")
        results.append((name, False, str(exc)))
        return
    elapsed = time.time() - started

    print(f"  Tools used ({elapsed:.1f}s):")
    if not outcome["steps"]:
        print("      (none)")
    show_trace(outcome)
    answer = outcome["answer"]
    print(f"  A: {answer[:400]}{'...' if len(answer) > 400 else ''}")

    used = {s["tool"] for s in outcome["steps"]}
    if expect_tools and not expect_tools <= used:
        missing = ", ".join(sorted(expect_tools - used))
        print(f"  FAIL  expected it to call: {missing}")
        results.append((name, False, f"did not call {missing}"))
        return

    ok, detail = check(answer, outcome)
    print(f"  {'PASS' if ok else 'FAIL'}  {detail}")
    results.append((name, ok, detail))


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    print("\nsyslab-server / Phase 03 tool-calling check")
    print(f"Model: {OLLAMA_MODEL}   Data folder: {DATA_DIR}")
    try:
        from app import llm

        llm.chat([{"role": "user", "content": "ping"}], timeout=20)
    except LlmError as exc:
        print(f"\n  {exc}")
        print("  Start Ollama and re-run scripts/check_ollama.py first.\n")
        return 1

    print("\nBuilding test files with facts invented for this run...")
    build_fixtures()
    print(f"  {PDF}  and  {XLSX}")

    scenario(
        "1. Reads a PDF rather than guessing",
        f"What is the total due on {PDF}, and who did the work?",
        lambda answer, _: (
            ("18640" in answer.replace(",", "") and "Rania" in answer),
            "quoted the real total and the real engineer",
        ),
        expect_tools={"read_pdf"},
    )

    scenario(
        "2. Reads a spreadsheet and does arithmetic on it",
        f"In {XLSX}, which region has the most revenue, and what do the three regions add up to?",
        lambda answer, _: (
            ("North" in answer and "59150" in answer.replace(",", "")),
            "identified North and totalled 59,150 correctly",
        ),
        expect_tools={"read_excel"},
    )

    scenario(
        "3. Finds a file from a loose description",
        "I have an invoice somewhere in my folder. What is its reference number?",
        lambda answer, _: (INVOICE_REF in answer, f"found {INVOICE_REF} without being given the filename"),
        expect_tools={"list_files"},
    )

    def wrote_the_row(_answer, _outcome):
        rows = tools.read_excel(XLSX)["rows"]
        east = [r for r in rows if r and str(r[0]).strip().lower() == "east"]
        if not east:
            return False, "no East row found in the file afterwards"
        if 9300 not in east[0]:
            return False, f"East row written but the value is wrong: {east[0]}"
        return True, f"East row is really in the file: {east[0]}"

    scenario(
        "4. Writes to a real file, correctly",
        f"Add a row to {XLSX} for the East region with revenue 9300 and 2 engineers.",
        wrote_the_row,
        expect_tools={"write_excel"},
    )

    def admits_ignorance(answer, outcome):
        lowered = answer.lower()
        admitted = any(
            phrase in lowered
            for phrase in ("not", "no ", "cannot", "can't", "does not exist", "unable", "couldn't")
        )
        invented = "2026" in answer and "budget" in lowered and not admitted
        if invented:
            return False, "it invented contents for a file that does not exist"
        return admitted, "said it could not find the file instead of inventing contents"

    scenario(
        "5. Refuses to invent a file that is not there",
        "Summarise the contents of budget_2026.xlsx for me.",
        admits_ignorance,
    )

    # ---- 6: the multi-file build that failed in the first real session ----
    def built_the_summary_sheet(_answer, _outcome):
        try:
            data = tools.read_excel("phase03_summary.xlsx")
        except tools.ToolError as exc:
            return False, f"the file was not created: {exc}"
        flat = " ".join(str(cell) for row in data["rows"] for cell in row)
        if INVOICE_REF not in flat and "18" not in flat:
            return False, f"the sheet exists but has nothing from the invoice: {data['rows']}"
        return True, f"{data['total_rows']} rows, built from both files"

    scenario(
        "6. Builds a spreadsheet from more than one file",
        f"Read {PDF} and {XLSX}, then make a new spreadsheet called "
        "phase03_summary.xlsx with one row per source file: the file name, and "
        "the most important number in it.",
        built_the_summary_sheet,
        expect_tools={"read_pdf", "read_excel", "write_excel"},
    )

    # ---- 7: ambiguity should produce a question, not a guess ----
    def asked_instead_of_guessing(answer, outcome):
        wrote = [s for s in outcome["steps"] if s["tool"].startswith("write_")]
        if wrote:
            return False, "it wrote a file rather than asking which invoice was meant"
        named = sum(1 for name in (PDF, "phase02_sample.pdf") if name in answer)
        if "?" in answer and named >= 2:
            return True, "listed the candidates and asked which one"
        if "?" in answer:
            return True, "asked a clarifying question"
        return False, "it neither asked nor named the candidates"

    scenario(
        "7. Asks rather than guessing when the request is vague",
        "Turn the invoice into a spreadsheet for me.",
        asked_instead_of_guessing,
        expect_tools={"list_files"},
    )

    # ---- 8: the mixed-format combine that failed in the second session ----
    tools.write_excel(
        "phase08_a.xlsx", rows=[["Alpha", 4100]], headers=["Item", "Amount"], sheet="Q1"
    )
    tools.write_pdf(
        "phase08_b.pdf", title="Phase 08 B", body="The amount for phase B is 7,250 AED."
    )
    tools.write_pdf(
        "phase08_c.pdf", title="Phase 08 C", body="The amount for phase C is 3,900 AED."
    )

    def combined_all_three(answer, outcome):
        try:
            rows = tools.read_excel("phase08_combined.xlsx")["rows"]
        except tools.ToolError as exc:
            return False, f"the combined file was not created: {exc}"
        flat = " ".join(str(cell) for row in rows for cell in row).replace(",", "")
        found = [name for name, value in
                 (("a", "4100"), ("b", "7250"), ("c", "3900")) if value in flat]
        if len(found) < 3:
            missing = {"a", "b", "c"} - set(found)
            return False, f"only covered {found}, missing {sorted(missing)}: {rows}"
        return True, f"all three amounts present across {len(rows)} rows"

    scenario(
        "8. Combines files of different types without stalling",
        "Combine the amounts from phase08_a, phase08_b and phase08_c into one new "
        "spreadsheet called phase08_combined.xlsx, one row per phase.",
        combined_all_three,
        expect_tools={"read_excel", "read_pdf", "write_excel"},
    )

    # ---- summary --------------------------------------------------------
    print(f"\nGate\n{LINE}")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    print(f"\n  {passed} of {len(results)} scenarios passed")

    if passed == len(results):
        print("\n  Phase 03 passes. The model reads before it answers and writes what")
        print("  it is asked to. Nothing further to do here.\n")
        return 0
    print("\n  Phase 03 does not pass yet. Paste this whole output back into the chat,")
    print("  including the tool traces, and we will work out whether it is the prompt,")
    print("  the tool descriptions, or the model.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
