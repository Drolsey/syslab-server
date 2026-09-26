"""Step 5.1: does an embedding candidate retrieve better, not just embed
faster -- recall@k and MRR against the SAME golden set and the SAME ground
truth scripts/check_retrieval.py measures the keyword baseline against
(overall MRR 0.768, 12 September).

    python scripts/bench_retrieval_candidates.py --base-url http://192.168.1.185:8001/v1 \\
        --model Qwen/Qwen3-Embedding-0.6B \\
        --query-instruction "Given a search query, retrieve relevant passages that answer the query." \\
        --context-limit 32768

    python scripts/bench_retrieval_candidates.py --base-url http://192.168.1.185:8002/v1 \\
        --model BAAI/bge-base-en-v1.5 \\
        --query-prefix "Represent this sentence for searching relevant passages: " \\
        --context-limit 512

WHY THIS EXISTS SEPARATELY FROM check_retrieval.py
    check_retrieval.py's fused/passage tables only exercise retrievers
    actually REGISTERED in app/retrieve.py, and the real Vector retriever
    (5.3) does not exist yet -- deliberately: the plan's own order puts
    model SELECTION (5.1) before the production producer (5.2-5.4), and
    building that producer for a candidate that then loses the comparison
    would be backwards. This script is the throwaway version of the same
    measurement: it chunks the SAME golden corpus, asks the SAME 42
    queries, and imports check_retrieval.py's own recall_at_k /
    reciprocal_rank / summarise / table -- not re-derived -- so a
    candidate's numbers are comparable to the keyword baseline on exactly
    the same terms, not a similar-looking metric computed a second way.

    Nothing here touches derived/, app/vectors.py's eventual embeddings
    table, or any tenant. Chunks live in memory for one process and are
    thrown away when it exits.

QUERY vs. DOCUMENT FORMATTING IS NOT THE SAME ACROSS CANDIDATES, AND
FAKING SAMENESS WOULD BE THE UNFAIR COMPARISON, NOT THE FAIR ONE
    Both Qwen3-Embedding and BGE recommend an instruction/prefix on the
    QUERY side only for retrieval, and document/passage text plain --
    confirmed 18 September 2026 against each model's own HuggingFace card,
    not assumed:
      - Qwen3-Embedding-0.6B: "Instruct: {task}\\nQuery: {text}" for
        queries; "No need to add instruction for retrieval documents."
      - BGE (base/small/large, en): "Represent this sentence for
        searching relevant passages: " prepended to queries; "no
        instruction needs to be added to passages."
    vLLM's OpenAI-compatible `/v1/embeddings` schema has no `input_type`
    field to apply this automatically (checked against the live schema,
    not guessed) -- the "prompt prefixes for input_type" line in Qwen's
    startup log belongs to `/v2/embed`, a different route this project
    does not use elsewhere. So both candidates go through the plain
    `/v1/embeddings` surface `app/llm.py` and the gateway already speak,
    and the query-side instruction is applied by THIS SCRIPT, per
    candidate, via `--query-prefix` / `--query-instruction` -- matched to
    what each model's own card recommends, not left off either one.

CONTEXT LIMIT IS A REAL, STRUCTURAL DIFFERENCE, NOT A KNOB TO HIDE
    `app/chunks.py` sizes a chunk by a CHARACTER estimate (7 chars / 2
    tokens), never a real tokenizer -- deliberate, see that module's own
    docstring. BGE-base's real limit is 512 tokens; a chunk estimated at
    512 tokenized to 513+ under BGE's real tokenizer on the first live
    run, 18 September 2026 -- CONFIRMED, not a theoretical risk: vLLM does
    not truncate silently by default, it hard-errors the whole batch with
    a 400. `--context-limit`, when given, is passed straight through as
    `truncate_prompt_tokens` (vLLM's own field for this), so an
    over-estimate gets truncated instead of crashing the run -- but
    truncated is not the same as embedded whole, and the report counts how
    many chunks were at risk by the character estimate rather than hiding
    that this happened.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import chunks as chunkmod  # noqa: E402
from app import parse  # noqa: E402

# Imported, not re-derived: the whole point is numbers comparable to the
# keyword baseline check_retrieval.py already measured, on the same metric
# code. See that script's own docstring for why PASSAGE_DEPTH etc. are
# shaped the way they are.
import check_retrieval as goldenset  # noqa: E402

LINE = "-" * 72
CORPUS = goldenset.CORPUS
KS = goldenset.KS


# --------------------------------------------------------------------------
# talking to the candidate
# --------------------------------------------------------------------------

def post_json(url: str, payload: dict, timeout: int = 300) -> tuple[int, dict | str]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def embed_batch(base_url: str, model: str, texts: list[str],
                 truncate_prompt_tokens: int | None = None) -> list[list[float]]:
    url = base_url.rstrip("/") + "/embeddings"
    payload = {"model": model, "input": texts}
    if truncate_prompt_tokens:
        # CONFIRMED LIVE, 18 September 2026, not assumed: vLLM does NOT
        # silently truncate an over-length input by default -- it hard-errors
        # the WHOLE batch with a 400, which is more disruptive than useful
        # for a chunk that is one token over a tight limit like BGE's 512.
        # This is the OpenAI-compatible field (present in vLLM's own
        # EmbeddingCompletionRequest schema) that opts into truncation
        # instead, applied identically for every candidate that sets
        # --context-limit rather than as a special case for the tight one.
        payload["truncate_prompt_tokens"] = truncate_prompt_tokens
    status, body = post_json(url, payload)
    if status != 200 or not isinstance(body, dict) or not body.get("data"):
        raise RuntimeError(f"embedding call failed ({status}): {str(body)[:300]}")
    # data[i].index says which input it answers -- vLLM has returned batches
    # out of order under load before now elsewhere in this project's own
    # testing (docs/models.md), so this sorts on it rather than trusting
    # response order to match request order.
    ordered = sorted(body["data"], key=lambda row: row["index"])
    return [row["embedding"] for row in ordered]


def embed_all(base_url: str, model: str, texts: list[str], batch_size: int,
              label: str, truncate_prompt_tokens: int | None = None) -> list[list[float]]:
    vectors: list[list[float]] = []
    started = time.time()
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        vectors.extend(embed_batch(base_url, model, batch, truncate_prompt_tokens))
        done = min(i + batch_size, len(texts))
        print(f"\r  embedding {label}: {done}/{len(texts)}", end="", flush=True)
    print(f"  ({time.time() - started:.1f}s)")
    return vectors


# --------------------------------------------------------------------------
# brute-force cosine similarity, pure python -- a few thousand chunks and
# 42 queries is not a scale that needs numpy, and this project's scripts
# stay stdlib-only on purpose (see e.g. bench_models.py, bench_gateway.py).
# --------------------------------------------------------------------------

def norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def cosine(a: list[float], a_norm: float, b: list[float], b_norm: float) -> float:
    if a_norm == 0 or b_norm == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (a_norm * b_norm)


# --------------------------------------------------------------------------
# building the golden corpus's chunk universe, once, in memory
# --------------------------------------------------------------------------

# Extraction and chunking are the SAME for every candidate -- neither depends
# on which embedding model is being measured -- and Docling's backend takes
# ~4.6s per contract (measured), ~4-5 minutes for the full 52-document
# corpus. Paying that twice, once per candidate run in the same session, is
# pure waste, so the chunk universe is cached to disk the first time and
# reused after. Not committed: it is a local scratch file, regenerated from
# the versioned PDFs whenever it is missing or --no-cache is passed.
CACHE_PATH = Path(__file__).resolve().parent / ".golden_chunk_cache.json"


def _load_cache() -> list[chunkmod.Chunk] | None:
    if not CACHE_PATH.is_file():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return [
        chunkmod.Chunk(source=row["source"], ordinal=row["ordinal"], text=row["text"],
                        start=row["start"], end=row["end"], tokens=row["tokens"])
        for row in data
    ]


def _save_cache(universe: list[chunkmod.Chunk]) -> None:
    payload = [
        {"source": c.source, "ordinal": c.ordinal, "text": c.text,
         "start": c.start, "end": c.end, "tokens": c.tokens}
        for c in universe
    ]
    CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")


def build_chunk_universe(context_limit: int | None, use_cache: bool = True) -> tuple[list[chunkmod.Chunk], int]:
    universe = _load_cache() if use_cache else None
    if universe is not None:
        print(f"  Loaded {len(universe)} chunks from {CACHE_PATH.name} "
              f"(delete it, or pass --no-cache, to re-parse the PDFs)")
    else:
        contracts = sorted((CORPUS / "contracts").glob("*.pdf"))
        if not contracts:
            raise RuntimeError(f"no contracts found under {CORPUS / 'contracts'}")
        universe = []
        started = time.time()
        for i, path in enumerate(contracts, start=1):
            found = parse.extract(path)
            print(f"\r  parsing corpus: {i}/{len(contracts)} ({time.time() - started:.0f}s)",
                  end="", flush=True)
            if not found.text or not found.text.strip():
                continue
            universe.extend(chunkmod.split(found.text, path.name))
        print(f"  ({time.time() - started:.0f}s total)")
        _save_cache(universe)

    at_risk = sum(1 for c in universe if context_limit and c.tokens > context_limit)
    return universe, at_risk


# --------------------------------------------------------------------------
# folding a ranked list of chunks into a ranked list of documents
# --------------------------------------------------------------------------

def documents_of_ranked_chunks(ranked: list[chunkmod.Chunk]) -> list[str]:
    """First appearance wins -- the same rule check_retrieval.documents_of
    uses, adapted to work directly off Chunk objects (which already carry
    `.source`) instead of resolving a chunk_id through the passages table,
    since this harness never writes one.
    """
    ordered, seen = [], set()
    for chunk in ranked:
        if chunk.source not in seen:
            seen.add(chunk.source)
            ordered.append(chunk.source)
    return ordered


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def run(base_url: str, model: str, batch_size: int, query_prefix: str,
        query_instruction: str, context_limit: int | None, use_cache: bool = True) -> dict:
    golden = json.loads((CORPUS / "golden.json").read_text(encoding="utf-8"))

    print(f"\n{LINE}\n  {model}  @  {base_url}\n{LINE}")
    print("  Building the golden corpus's chunk universe (52 contracts, "
          "app/parse.py + app/chunks.py)...")
    universe, at_risk = build_chunk_universe(context_limit, use_cache)
    print(f"  {len(universe)} chunks over {len({c.source for c in universe})} documents")
    if context_limit and at_risk:
        print(f"  WARNING: {at_risk} chunks estimate over {context_limit} tokens "
              f"(app/chunks.py's char-based estimate, not this model's real "
              f"tokenizer) -- --context-limit is passed as truncate_prompt_tokens "
              f"below, so these get truncated rather than erroring the whole "
              f"batch, but truncated is not the same as embedded whole.")

    chunk_texts = [c.text for c in universe]
    chunk_vectors = embed_all(base_url, model, chunk_texts, batch_size, "corpus chunks",
                               truncate_prompt_tokens=context_limit)
    chunk_norms = [norm(v) for v in chunk_vectors]
    dimension = len(chunk_vectors[0]) if chunk_vectors else 0

    if query_instruction:
        query_texts = [f"Instruct: {query_instruction}\nQuery: {q['query']}"
                        for q in golden["queries"]]
    elif query_prefix:
        query_texts = [f"{query_prefix}{q['query']}" for q in golden["queries"]]
    else:
        query_texts = [q["query"] for q in golden["queries"]]
    query_vectors = embed_all(base_url, model, query_texts, batch_size, "golden queries",
                               truncate_prompt_tokens=context_limit)

    results = []
    for query, qvec in zip(golden["queries"], query_vectors):
        relevant = set(query["relevant"])
        qnorm = norm(qvec)
        scored = sorted(
            zip(universe, chunk_vectors, chunk_norms),
            key=lambda row: -cosine(qvec, qnorm, row[1], row[2]),
        )
        ranked_chunks = [row[0] for row in scored]
        ranked_documents = documents_of_ranked_chunks(ranked_chunks)
        results.append({
            "id": query["id"],
            "kind": query["kind"],
            "relevant": sorted(relevant),
            "returned": ranked_documents[:max(KS)],
            "recall": {str(k): goldenset.recall_at_k(ranked_documents, relevant, k) for k in KS},
            "rr": goldenset.reciprocal_rank(ranked_documents, relevant),
        })

    return {
        "model": model,
        "base_url": base_url,
        "dimension": dimension,
        "chunks_embedded": len(universe),
        "documents": len({c.source for c in universe}),
        "chunks_at_risk_of_truncation": at_risk,
        "context_limit": context_limit,
        "query_formatting": (
            f"Instruct: {query_instruction}\\nQuery: <text>" if query_instruction
            else f"{query_prefix}<text>" if query_prefix
            else "<text> (no prefix)"
        ),
        "queries": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--query-prefix", default="",
                         help="Prepended verbatim to each query (BGE-style).")
    parser.add_argument("--query-instruction", default="",
                         help="Wrapped as 'Instruct: <this>\\nQuery: <text>' (Qwen3-Embedding-style). "
                              "Takes precedence over --query-prefix if both are given.")
    parser.add_argument("--context-limit", type=int, default=None,
                         help="This model's real max sequence length, to flag at-risk chunks. "
                              "Purely diagnostic -- does not truncate or skip anything itself.")
    parser.add_argument("--out", default=None,
                         help="Default: bench_results_retrieval_<model, sanitised>.json")
    parser.add_argument("--no-cache", action="store_true",
                         help="Re-parse the PDFs (~4-5 min) instead of reusing "
                              ".golden_chunk_cache.json from a prior run.")
    args = parser.parse_args()

    if not (CORPUS / "golden.json").is_file():
        print("  tests/fixtures/corpus/golden.json is missing; nothing to measure.")
        return 1

    report = run(args.base_url, args.model, args.batch_size,
                 args.query_prefix, args.query_instruction, args.context_limit,
                 use_cache=not args.no_cache)
    summary = goldenset.summarise(report["queries"])
    goldenset.table(f"Vector search: {args.model} over the golden corpus's chunks", summary)

    print(f"\n  dimension: {report['dimension']}")
    print(f"  query formatting: {report['query_formatting']}")
    print(f"  {report['chunks_embedded']} chunks over {report['documents']} documents")
    if report["context_limit"] and report["chunks_at_risk_of_truncation"]:
        print(f"  {report['chunks_at_risk_of_truncation']} chunks estimated over "
              f"{report['context_limit']} tokens -- truncation risk, not simulated here")
    print(f"\n  For comparison, the keyword baseline (scripts/check_retrieval.py, "
          f"12 September): overall MRR 0.768")

    out_path = args.out or f"bench_results_retrieval_{args.model.replace('/', '_')}.json"
    payload = {"summary": summary, **report}
    Path(out_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  Full numbers written to {out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
