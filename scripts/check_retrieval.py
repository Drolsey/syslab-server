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

# How many PASSAGES to ask for before folding them back into a ranking of
# documents, and the number needs its reasoning attached because it is the one
# judgement call in this comparison.
#
# The baseline's Recall@10 means "the right document appeared among ten
# DOCUMENTS". A document's passages cluster -- the ten best-scoring passages
# for a clause query are routinely three contracts -- so asking the passage
# index for ten and folding them down would compare ten documents against
# three and call the difference a regression.
#
# Fifty is deep enough that ten distinct documents can be reached, and it is
# still far LESS text than the baseline returns: fifty 512-token passages is
# about 25,000 tokens against ten whole contracts. The comparison is not being
# made generous to passages; it is being made possible.
PASSAGE_DEPTH = 50


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

def documents_of(hits, resolve) -> tuple[list[str], list[str]]:
    """Fold a ranked list of passages into a ranked list of documents.

    First appearance wins: a contract whose best passage is third is ranked
    third, whatever its other passages do. Averaging a document's passage
    positions would reward a document for being verbose, which is the opposite
    of what a citation is for.

    Returns the documents and any chunk_ids that did not resolve. A chunk_id
    that cannot be looked up again is not a citation, so an unresolvable one is
    reported rather than skipped -- it would otherwise show up as a retrieval
    result that is merely slightly worse.
    """
    ordered, seen, unresolved = [], set(), []
    for hit in hits:
        found = resolve(hit.chunk_id)
        if found is None:
            unresolved.append(hit.chunk_id)
            continue
        if found["source"] not in seen:
            seen.add(found["source"])
            ordered.append(found["source"])
    return ordered, unresolved


def _embed_reachable() -> bool:
    """Same probe as app/main.py's own startup registration gate -- see that
    file's comment for why config presence alone is not enough. Duplicated
    rather than imported because importing app.main here would run the
    whole FastAPI app's route setup for one boolean; this is six lines."""
    import urllib.error
    import urllib.request

    from app import config as app_config

    try:
        urllib.request.urlopen(f"{app_config.EMBED_BASE_URL}/models", timeout=3)
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def measure(root: Path, golden: dict, manifest: dict) -> dict:
    from app import ingest, models, passages, retrieve, search, tools, vectors  # noqa: F401  (tools for side effects)

    # Step 5's own comparison: keyword vs keyword+vector+RRF. Registered here,
    # exactly the way app/main.py registers it for a real deployment (role
    # declared AND a server actually answering) rather than unconditionally --
    # a checkout with no embed role filled, or no reachable server, measures
    # keyword-only exactly as it always has, with an explicit note rather than
    # a silent gap.
    vector_available = False
    try:
        models.model_for("embed")
    except models.RoleUnavailable:
        pass
    else:
        if _embed_reachable():
            ingest.register(vectors.EMBEDDINGS)
            retrieve.register(vectors.VECTOR)
            vector_available = True

    corpus_files = sorted((CORPUS / "contracts").glob("*.pdf"))
    target = config.DATA_ROOT / TENANT
    target.mkdir(parents=True, exist_ok=True)
    for source in corpus_files:
        shutil.copy2(source, target / source.name)

    with context.use_tenant(TENANT):
        indexed = search.rebuild()
        chunked = passages.rebuild()
        keyword = retrieve.registered()["keyword"]
        vector = retrieve.registered().get("vector")
        results = []
        passage_results = []
        vector_results = []
        fused_results = []
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

            # The same query, through the retriever seam. It goes through
            # retrieve.registered() rather than calling passages directly,
            # because the seam is what 4.5 fuses and 4.6 serves -- measuring
            # the module behind it would measure something nothing calls.
            try:
                hits = keyword.search(query["query"], PASSAGE_DEPTH)
                by_document, unresolved = documents_of(hits, passages.passage)
                shallow, _ = documents_of(hits[:max(KS)], passages.passage)
                perror = None
            except Exception as exc:  # noqa: BLE001
                hits, by_document, unresolved, shallow = [], [], [], []
                perror = f"{type(exc).__name__}: {exc}"

            passage_results.append({
                "id": query["id"],
                "kind": query["kind"],
                "relevant": sorted(relevant),
                "returned": by_document[:max(KS)],
                "passages_returned": len(hits),
                "documents_in_top_10_passages": len(shallow),
                "unresolved_chunk_ids": unresolved,
                "error": perror,
                "recall": {str(k): recall_at_k(by_document, relevant, k) for k in KS},
                "rr": reciprocal_rank(by_document, relevant),
            })

            # The vector retriever alone, same shape as the keyword pass
            # above -- only computed when Step 5 is actually available, so a
            # checkout without it simply has an empty vector_results (main()
            # skips the table rather than printing zeroes that would read as
            # "vector search returned nothing" instead of "not measured".
            if vector is not None:
                try:
                    vhits = vector.search(query["query"], PASSAGE_DEPTH)
                    vby_document, vunresolved = documents_of(vhits, passages.passage)
                    verror = None
                except Exception as exc:  # noqa: BLE001
                    vhits, vby_document, vunresolved = [], [], []
                    verror = f"{type(exc).__name__}: {exc}"

                vector_results.append({
                    "id": query["id"],
                    "kind": query["kind"],
                    "relevant": sorted(relevant),
                    "returned": vby_document[:max(KS)],
                    "passages_returned": len(vhits),
                    "unresolved_chunk_ids": vunresolved,
                    "error": verror,
                    "recall": {str(k): recall_at_k(vby_document, relevant, k) for k in KS},
                    "rr": reciprocal_rank(vby_document, relevant),
                })

            # The same query AGAIN, through the fusion this time -- Step 4.5.
            #
            # One retriever is registered, so fusing its list must give that
            # list back in that order, and the assertion is made on the
            # CHUNK_IDS position by position rather than on the metrics. Equal
            # MRR is a much weaker claim: a fusion that swapped two passages of
            # the same contract, or two passages neither of which is relevant,
            # would score identically and be just as broken. Forty-two queries
            # at PASSAGE_DEPTH is a couple of thousand positions that have to
            # agree exactly.
            try:
                fused = retrieve.search(query["query"], PASSAGE_DEPTH)
                fused_ids = [f.chunk_id for f in fused["passages"]]
                fused_by = sorted({r for f in fused["passages"] for r in f.found_by})
                fused_documents, _ = documents_of(fused["passages"], passages.passage)
                ferror = None
            except Exception as exc:  # noqa: BLE001
                fused, fused_ids, fused_by, fused_documents = None, [], [], []
                ferror = f"{type(exc).__name__}: {exc}"

            fused_results.append({
                "id": query["id"],
                "kind": query["kind"],
                "relevant": sorted(relevant),
                "returned": fused_documents[:max(KS)],
                "retrievers": fused["retrievers"] if fused else [],
                "failed": fused["failed"] if fused else {},
                "found_by": fused_by,
                "same_order_as_the_retriever": fused_ids == [h.chunk_id for h in hits],
                "error": ferror,
                "recall": {str(k): recall_at_k(fused_documents, relevant, k) for k in KS},
                "rr": reciprocal_rank(fused_documents, relevant),
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
        "passages_indexed": chunked.get("passages", 0),
        "corpus_documents": len(corpus_files),
        "registered_retrievers": sorted(retrieve.registered()),
        "vector_available": vector_available,
        # Carried out of here rather than read in main(): app is imported
        # inside this function, after the throwaway root has been set, and
        # reaching for app.retrieve from main() is how a gate ends up importing
        # the application against the real config it just went to the trouble
        # of replacing.
        "rrf_k": retrieve.RRF_K,
        "queries": results,
        "passage_queries": passage_results,
        "vector_queries": vector_results,
        "fused_queries": fused_results,
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


def table(title: str, summary: dict) -> None:
    print(f"\n{title}")
    print(LINE)
    print(f"  {'':14s}{'queries':>9s}{'MRR':>8s}" + "".join(f"{'R@'+str(k):>9s}" for k in KS))
    for key in ["overall"] + [k for k in summary if k != "overall"]:
        row = summary[key]
        print(f"  {key:14s}{row['queries']:>9d}{row['mrr']:>8.3f}"
              + "".join(f"{row['recall@'+str(k)]:>9.3f}" for k in KS))


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
    passage_summary = summarise(run["passage_queries"])
    fused_summary = summarise(run["fused_queries"])
    vector_summary = summarise(run["vector_queries"]) if run["vector_queries"] else None

    table("Documents: whole-document FTS5 keyword search, app/search.py", summary)
    table("Passages: chunk-level FTS5 via the keyword retriever, app/passages.py",
          passage_summary)
    if vector_summary is not None:
        table("Vector: chunk-level cosine similarity via the Vector retriever, app/vectors.py",
              vector_summary)
        table(f"Fused: RRF over {', '.join(run['registered_retrievers'])}, app/retrieve.py",
              fused_summary)
    elif run["vector_available"]:
        print("\n  Vector: [roles.embed] is filled and the server answered, but no "
              "vector results were produced -- investigate.")
    else:
        print("\n  Vector: not measured -- [roles.embed] is empty or the embed server "
              "did not answer the reachability probe.")
    print(f"  {run['passages_indexed']} passages over {run['corpus_documents']} "
          f"documents; each query asked for {PASSAGE_DEPTH} passages and they were")
    print("  folded into a document ranking by first appearance. See PASSAGE_DEPTH")
    print("  for why that number and not ten.")
    spread = [r["documents_in_top_10_passages"] for r in run["passage_queries"]]
    print(f"  The ten best passages of a query are {sum(spread) / max(1, len(spread)):.1f} "
          f"distinct documents on average, which is")
    print("  why folding ten passages down would have compared ten documents with "
          "three.")
    print("  Rank 1 and MRR are unaffected by the depth and are the honest headline;")
    print("  R@10 is the one the deeper pool helps.")

    errored = [r for r in run["queries"] if r["error"]]
    if errored:
        print(f"\n  {len(errored)} of {len(run['queries'])} queries RAISED. That is a "
              f"fault in the gate or the search, not a measurement:")
        for row in errored[:3]:
            print(f"    {row['id']}: {row['error']}")
        print("  Every number above is meaningless until that is fixed.")

    # Per path, and labelled. Both numbers used to be one number, because
    # there was only one path; printing the document figure under two tables
    # would read as the verdict on whichever was nearer.
    for label, rows in (("documents", run["queries"]),
                        ("passages", run["passage_queries"])):
        failing = [r for r in rows if r["rr"] == 0.0]
        paraphrase = [r for r in failing if r["kind"] == "paraphrase"]
        unexpected = [r for r in failing if r["kind"] != "paraphrase"]
        print()
        print(f"  {label:10s} {len(failing)} of {len(rows)} queries returned nothing "
              f"relevant in the top {max(KS)}.")
        print(f"  {'':10s} {len(paraphrase)} are paraphrase queries, which keyword "
              f"search is EXPECTED to fail.")
        if unexpected:
            print(f"  {'':10s} {len(unexpected)} are NOT: "
                  f"{', '.join(r['id'] for r in unexpected)}")

    # ----------------------------------------------------------------------
    # Step 4.5's gate, and it is a gate that asserts NOTHING HAPPENED.
    # ----------------------------------------------------------------------
    print("\nFusion: RRF over one ranked list, app/retrieve.py")
    print(LINE)
    fusion_ok = True

    reordered = [r for r in run["fused_queries"] if not r["same_order_as_the_retriever"]]
    ferrored = [r for r in run["fused_queries"] if r["error"]]
    print(f"  Registered retrievers: {', '.join(run['registered_retrievers']) or 'none'}")
    print("  Fusion of a single ranked list is that list in that order, and it is")
    print(f"  asserted on the chunk_ids position by position -- {len(run['fused_queries'])} "
          f"queries at a depth of")
    print(f"  {PASSAGE_DEPTH}, so roughly {len(run['fused_queries']) * PASSAGE_DEPTH:,} "
          f"positions that have to agree exactly. Equal MRR")
    print("  is a far weaker claim: a fusion that swapped two passages of the same")
    print("  contract would score identically and be just as broken.")
    print()

    if ferrored:
        fusion_ok = False
        print(f"  FAIL  {len(ferrored)} of {len(run['fused_queries'])} queries raised "
              f"inside the fusion:")
        for row in ferrored[:3]:
            print(f"          {row['id']}: {row['error']}")
    elif reordered:
        fusion_ok = False
        print(f"  FAIL  the fusion REORDERED {len(reordered)} of "
              f"{len(run['fused_queries'])} queries: "
              f"{', '.join(r['id'] for r in reordered[:6])}")
        print("        An RRF that moves a single list is broken, and this is the only")
        print("        moment it is cheap to notice -- once a second retriever is")
        print("        registered there is nothing left to compare it against.")
    else:
        print(f"  PASS  all {len(run['fused_queries'])} queries came back in exactly the "
              f"order the retriever gave")

    # The metrics have to agree too, and they are checked separately from the
    # order because they are a different claim: the order being identical is
    # about the fusion, and the metrics being identical is about the measurement
    # having actually gone through the fusion rather than around it.
    metric_drift = [
        metric for metric in ["mrr"] + [f"recall@{k}" for k in KS]
        if abs(fused_summary["overall"][metric] - passage_summary["overall"][metric]) > 1e-12
    ]
    if metric_drift:
        fusion_ok = False
        print(f"  FAIL  the fused metrics differ from the retriever's: "
              f"{', '.join(metric_drift)}")
    else:
        print(f"  PASS  every metric is identical to the retriever's, MRR "
              f"{fused_summary['overall']['mrr']:.3f}")

    credited = sorted({r for row in run["fused_queries"] for r in row["found_by"]})
    if credited == run["registered_retrievers"]:
        print(f"  PASS  found_by credits exactly the retrievers that ran: "
              f"{', '.join(credited)}")
    else:
        fusion_ok = False
        print(f"  FAIL  found_by says {credited or 'nothing'} and the retrievers that "
              f"ran were {run['registered_retrievers']}")

    print("\n  This section is expected to stay a PASS and stop being interesting.")
    print("  It becomes interesting again in Step 5: the day a second retriever is")
    print("  registered, the ORDER assertion above must start FAILING, because a")
    print("  fusion of two lists that still returns the first one unchanged means")
    print("  the second one is not reaching it.")

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
        # Added at 4.4, ALONGSIDE the 4.1 numbers rather than replacing them.
        # The committed baseline is the thing every later step is measured
        # against, and a baseline rewritten by the step it is measuring is not
        # a baseline. `summary` above stays what it has always been.
        "passages": {
            "measured": "chunk-level FTS5 via the keyword retriever (app/passages.py), Step 4.4",
            "passage_depth": PASSAGE_DEPTH,
            "summary": passage_summary,
            "queries": run["passage_queries"],
        },
        # Step 5's own row, alongside (never replacing) 4.4's. Present only
        # when Step 5 was actually measurable this run -- see vector_available.
        "vector": {
            "measured": "chunk-level cosine similarity via the Vector retriever (app/vectors.py), Step 5",
            "available": run["vector_available"],
            "passage_depth": PASSAGE_DEPTH,
            "summary": vector_summary,
            "queries": run["vector_queries"],
        },
        # Added at 4.5, and it is deliberately a duplicate of the block above.
        # A row that is supposed to be identical is only useful if it is
        # recorded separately: the day these two disagree, the committed file
        # is what says which of them moved.
        "fusion": {
            "measured": "RRF over the registered retrievers (app/retrieve.py), Step 4.5",
            "retrievers": run["registered_retrievers"],
            "rrf_k": run["rrf_k"],
            "summary": fused_summary,
            "queries": run["fused_queries"],
        },
    }

    if args.baseline:
        BASELINE.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  Baseline written to {BASELINE.relative_to(BASELINE.parents[3])}")
    elif BASELINE.is_file():
        previous = json.loads(BASELINE.read_text(encoding="utf-8"))
        before = previous["summary"]["overall"]
        now = summary["overall"]
        later = passage_summary["overall"]
        print(f"\nAgainst the committed 4.1 baseline")
        print(LINE)
        print("  Two rows per metric. The FIRST is the same whole-document search the",)
        print("  baseline measured and must not move -- if it has, something changed")
        print("  that was not supposed to, and the passage numbers below mean nothing")
        print("  until that is explained. The SECOND is what 4.4 added.")
        print()
        drifted = []
        for metric in ["mrr"] + [f"recall@{k}" for k in KS]:
            for label, current in (("documents", now), ("passages", later)):
                delta = current[metric] - before[metric]
                arrow = "same" if abs(delta) < 1e-9 else ("better" if delta > 0 else "WORSE")
                if label == "documents" and arrow != "same":
                    drifted.append(metric)
                print(f"  {metric:12s}{label:11s}{before[metric]:>8.3f} -> "
                      f"{current[metric]:>6.3f}   {delta:+.3f}  {arrow}")
            print()
        if drifted:
            print(f"  THE DOCUMENT NUMBERS MOVED: {', '.join(sorted(set(drifted)))}.")
            print("  4.4 adds a second index and changes nothing about the first one,")
            print("  so this is a regression to explain before reading anything else.")
        print("  A change in the passage row is information, not a verdict. Passage")
        print("  retrieval may be worse on some queries than whole-document retrieval,")
        print("  because a 512-token chunk gives BM25 less to score. Record it either way.")

        # 4.5 is measured against 4.4 rather than against 4.1, because "the
        # fusion changed nothing" is a claim about the sub-step before it.
        recorded = previous.get("passages", {}).get("summary", {}).get("overall")
        if recorded:
            moved = [
                metric for metric in ["mrr"] + [f"recall@{k}" for k in KS]
                if abs(fused_summary["overall"][metric] - recorded[metric]) > 5e-4
            ]
            print()
            if moved:
                fusion_ok = False
                print(f"  FAIL  the fused numbers moved against the committed 4.4 "
                      f"passage row: {', '.join(moved)}.")
                print("        4.5 adds a fusion over ONE list and must change nothing.")
            else:
                print(f"  PASS  the fused numbers match the committed 4.4 passage row "
                      f"exactly, MRR {fused_summary['overall']['mrr']:.3f}")
    else:
        print("\n  No committed baseline yet. Run with --baseline to write one.")

    if not fusion_ok:
        print("\n  The fusion gate FAILED. See the fusion section above.\n")
        return 1

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
