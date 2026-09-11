"""Step 4.1 gate: how good is retrieval, measured rather than asserted.

    py scripts\\check_retrieval.py
    py scripts\\check_retrieval.py --baseline     # write the numbers down

WHAT THIS IS FOR
    Every sub-step after this one changes how retrieval works: 4.3 splits
    documents into passages, 4.4 indexes those passages, 4.5 puts a fusion in
    front, and Step 5 adds a second retriever. Each of those is a claim that
    something got better. This is the only thing here that can say whether it
    did.

    So it runs FIRST, against the system as it is today -- whole-document FTS5
    keyword search, `app/search.py`, unchanged -- and records that. A baseline
    measured after the change is not a baseline.

WHY THE PARAPHRASE QUERIES ARE SUPPOSED TO FAIL
    Eight of the 42 queries describe a clause in words the contract does not
    use. Keyword search cannot match a paraphrase and says so in its own
    result payload. They are in the golden set precisely because they fail:
    a golden set the current system passes completely cannot show an
    improvement, and Step 5 exists to move exactly these.

WHY THE AGGREGATE QUESTIONS ARE HERE AT ALL
    They cannot be answered by retrieval and this gate does not pretend to
    answer them. What it reports is the SHORTFALL -- how many documents
    actually carry the thing, against how many a k-limited search can return.
    That number is decision 5.6's whole argument, and Step 9 is where it gets
    fixed.

IT RUNS ENTIRELY IN A TEMPORARY FOLDER, like every other gate here: your own
data/, index/, derived/ and control/ are redirected before anything is opened,
and that is checked and printed before any measurement runs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _deps  # noqa: E402

_deps.require("pymupdf", "openpyxl")

from app import config, context  # noqa: E402

LINE = "-" * 72
CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "corpus"
BASELINE = CORPUS / "baseline.json"
TENANT = "goldenset"
KS = (1, 3, 5, 10)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Of the documents that SHOULD come back, what fraction did, within k?

    Not precision: a query with three right answers that returns all three at
    ranks 1-3 scores 1.0, and so does a query with one right answer at rank 1.
    """
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    """1/rank of the FIRST correct answer, or 0 if none came back.

    Answers "how far down did the user have to read", which is the thing a
    person actually experiences. Averaged over queries this is MRR.
    """
    for position, name in enumerate(ranked, start=1):
        if name in relevant:
            return 1.0 / position
    return 0.0


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def measure(root: Path, golden: dict, manifest: dict) -> dict:
    from app import search, tools  # noqa: F401  (tools imported for its side effects)

    corpus_files = sorted((CORPUS / "contracts").glob("*.pdf"))
    target = config.DATA_ROOT / TENANT
    target.mkdir(parents=True, exist_ok=True)
    for source in corpus_files:
        shutil.copy2(source, target / source.name)

    with context.use_tenant(TENANT):
        indexed = search.rebuild()
        results = []
        for query in golden["queries"]:
            relevant = set(query["relevant"])
            try:
                found = search.search(query["query"], limit=max(KS))
                # search() returns its hits under "results". Read from the real
                # shape, never from what the name suggests -- guessing
                # "documents" here scored every query 0.000 and looked exactly
                # like a finding until the shape was printed.
                ranked = [row["name"] for row in found["results"]]
                matched_on = found.get("matched_on")
                error = None
            except Exception as exc:  # noqa: BLE001
                ranked, matched_on, error = [], None, f"{type(exc).__name__}: {exc}"

            results.append({
                "id": query["id"],
                "kind": query["kind"],
                "relevant": sorted(relevant),
                "returned": ranked[:max(KS)],
                "matched_on": matched_on,
                "error": error,
                "recall": {str(k): recall_at_k(ranked, relevant, k) for k in KS},
                "rr": reciprocal_rank(ranked, relevant),
            })

        # The aggregate questions. Retrieval cannot answer these; what is
        # recorded is how far short it falls, which is the point.
        aggregate_report = []
        for item in golden["aggregates"]:
            answer = item["true_answer"]
            carriers = answer.get("yes")
            if carriers is None:
                aggregate_report.append({
                    "id": item["id"], "question": item["question"],
                    "note": "not a count; no shortfall to compute",
                    "true_answer": answer,
                })
                continue
            aggregate_report.append({
                "id": item["id"],
                "question": item["question"],
                "documents_carrying_it": carriers,
                "of": answer["of"],
                "a_search_returning_k": {str(k): min(k, carriers) for k in KS},
                "shortfall_at_8": max(0, carriers - 8),
            })

    return {
        "documents_indexed": indexed.get("indexed", len(corpus_files)),
        "corpus_documents": len(corpus_files),
        "queries": results,
        "aggregates": aggregate_report,
    }


def summarise(results: list[dict]) -> dict:
    """Averages overall and per kind. Per kind is the useful half."""
    def average(rows, key, k=None):
        if not rows:
            return 0.0
        values = [r["recall"][str(k)] if k else r["rr"] for r in rows]
        return sum(values) / len(values)

    kinds = sorted({r["kind"] for r in results})
    out = {"overall": {"queries": len(results),
                       "mrr": average(results, "rr"),
                       **{f"recall@{k}": average(results, "recall", k) for k in KS}}}
    for kind in kinds:
        rows = [r for r in results if r["kind"] == kind]
        out[kind] = {"queries": len(rows),
                     "mrr": average(rows, "rr"),
                     **{f"recall@{k}": average(rows, "recall", k) for k in KS}}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true",
                        help="write these numbers to the committed baseline file")
    args = parser.parse_args()

    print("\nsyslab-server / retrieval quality")
    print(LINE)

    if not (CORPUS / "golden.json").is_file():
        print("  The golden set is not there. tests/fixtures/corpus/golden.json is")
        print("  built by Step 4.1 and nothing can be measured without it.\n")
        return 1

    golden = json.loads((CORPUS / "golden.json").read_text(encoding="utf-8"))
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))

    real = (config.DATA_ROOT, config.INDEX_ROOT, config.DERIVED_ROOT, config.CONTROL_PATH)
    with tempfile.TemporaryDirectory(prefix="syslab-retrieval-") as raw:
        root = Path(raw)
        config.DATA_ROOT = root / "data"
        config.INDEX_ROOT = root / "index"
        config.DERIVED_ROOT = root / "derived"
        config.CONTROL_DIR = root / "control"
        config.CONTROL_PATH = root / "control" / "control.sqlite3"

        safe = all(p.is_relative_to(root) for p in
                   (config.DATA_ROOT, config.INDEX_ROOT,
                    config.DERIVED_ROOT, config.CONTROL_PATH))
        print(f"  Throwaway root: {root}")
        print(f"  {'PASS' if safe else 'FAIL'}  your own data, index, derived and "
              f"control plane are untouched")
        if not safe:
            return 1
        print(f"  Corpus: {manifest['documents']} contracts, "
              f"{manifest['source']}, {manifest['licence']}")
        print(f"  Golden set: {len(golden['queries'])} queries, "
              f"{len(golden['aggregates'])} aggregate questions")

        try:
            run = measure(root, golden, manifest)
        except Exception as exc:  # noqa: BLE001
            print(f"\n  FAIL  the measurement did not complete: "
                  f"{type(exc).__name__}: {exc}\n")
            return 1
        finally:
            (config.DATA_ROOT, config.INDEX_ROOT,
             config.DERIVED_ROOT, config.CONTROL_PATH) = real

    summary = summarise(run["queries"])

    print(f"\nRetrieval today: whole-document FTS5 keyword search, app/search.py")
    print(LINE)
    print(f"  {'':14s}{'queries':>9s}{'MRR':>8s}" + "".join(f"{'R@'+str(k):>9s}" for k in KS))
    for key in ["overall"] + [k for k in summary if k != "overall"]:
        row = summary[key]
        print(f"  {key:14s}{row['queries']:>9d}{row['mrr']:>8.3f}"
              + "".join(f"{row['recall@'+str(k)]:>9.3f}" for k in KS))

    errored = [r for r in run["queries"] if r["error"]]
    if errored:
        print(f"\n  {len(errored)} of {len(run['queries'])} queries RAISED. That is a "
              f"fault in the gate or the search, not a measurement:")
        for row in errored[:3]:
            print(f"    {row['id']}: {row['error']}")
        print("  Every number above is meaningless until that is fixed.")

    failing = [r for r in run["queries"] if r["rr"] == 0.0]
    paraphrase = [r for r in failing if r["kind"] == "paraphrase"]
    print(f"\n  {len(failing)} of {len(run['queries'])} queries returned nothing relevant "
          f"in the top {max(KS)}.")
    print(f"  {len(paraphrase)} of those are paraphrase queries, which are EXPECTED to "
          f"fail today.")
    unexpected = [r for r in failing if r["kind"] != "paraphrase"]
    if unexpected:
        print(f"  {len(unexpected)} are NOT: {', '.join(r['id'] for r in unexpected)}")

    print(f"\nThe aggregate questions, which retrieval cannot answer")
    print(LINE)
    for item in run["aggregates"]:
        if "documents_carrying_it" in item:
            print(f"  {item['id']}  {item['question']}")
            print(f"        true answer: {item['documents_carrying_it']} of {item['of']} "
                  f"contracts. A search returning 8 can see at most 8, so it is short by "
                  f"{item['shortfall_at_8']}.")
        else:
            print(f"  {item['id']}  {item['question']}")
            print(f"        {item['note']}: {item['true_answer']}")
    print("\n  This is decision 5.6's argument as a number. Step 9 builds the fact")
    print("  table that answers these; until then the shortfall must be VISIBLE,")
    print("  which is what the `coverage` block in 4.6 is for.")

    report = {
        "measured": "whole-document FTS5 keyword search (app/search.py), before Step 4.2",
        "corpus": {"documents": manifest["documents"], "source": manifest["source"]},
        "summary": summary,
        "queries": run["queries"],
        "aggregates": run["aggregates"],
    }

    if args.baseline:
        BASELINE.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  Baseline written to {BASELINE.relative_to(BASELINE.parents[3])}")
    elif BASELINE.is_file():
        previous = json.loads(BASELINE.read_text(encoding="utf-8"))
        before = previous["summary"]["overall"]
        now = summary["overall"]
        print(f"\nAgainst the committed baseline")
        print(LINE)
        for metric in ["mrr"] + [f"recall@{k}" for k in KS]:
            delta = now[metric] - before[metric]
            arrow = "same" if abs(delta) < 1e-9 else ("better" if delta > 0 else "WORSE")
            print(f"  {metric:12s}{before[metric]:>8.3f} -> {now[metric]:>6.3f}   "
                  f"{delta:+.3f}  {arrow}")
        print("\n  A change here is information, not a verdict. Passage retrieval may")
        print("  be worse on some queries than whole-document retrieval, because a")
        print("  512-token chunk gives BM25 less to score. Record it either way.")
    else:
        print("\n  No committed baseline yet. Run with --baseline to write one.")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
