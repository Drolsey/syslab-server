# Step 4: The Retrieval Plane

Status: PLANNED, NOT STARTED. Needs Amro's sign-off on the six decisions in section 4.
Written 10 September 2026, after Step 2 completed.

Step 2 built the pipeline that makes derived things out of a tenant's documents and can
say whether one is ready. This step is the first consumer that pipeline was designed for,
and the first thing here a website can call to get a passage out of a document.

---

## 1. What this step is

**A tenant-scoped HTTP surface that answers "which parts of this customer's documents bear
on this question", and returns those parts with enough citation to check them.**

Three endpoints, already named in `docs/architecture.md` §5:

| | |
|---|---|
| `POST /api/v1/retrieve` | a query in, ranked passages out |
| `GET /api/v1/documents` | what this tenant has, and whether it is ready |
| `POST /api/v1/ingest/{name}` | make it ready |

And, underneath them, the thing that makes the answer possible: **a chunk producer**, which
is an ordinary Step 2 producer turning the text artifact into retrievable passages.

**The one-line summary of the design: retrievers become additive, the way producers did.**
Step 2's payoff was that vision, embeddings and field extraction each become a new producer
rather than a fourth reader of the same PDF. Step 4's payoff is the same shape one level
up: keyword search is the first retriever, the vector retriever arrives in Step 5, and it
arrives as a row in a fusion that already works rather than as a second retrieval path.

## 2. What this step is not

- **Not embeddings, and not vector search.** That is Step 5. This step defines where a
  second retriever plugs in and proves the plug with one retriever in it.
- **Not a reranker.** A cross-encoder is a second model on the GPU and belongs with Step
  5's model work. The response contract leaves room for it; nothing in this step runs one.
- **Not an answer.** This plane returns passages, never prose. The boundary rule in
  `README.md` is the reason and it is not negotiable here: *this server never learns what a
  conversation is.* Query rewriting from conversation history, answer synthesis and
  citation formatting are the website's, because they need the turn before this one.
- **Not a new ingestion path.** Everything it serves comes out of `app/ingest.py`. If this
  step finds itself opening a PDF, it has gone wrong.
- **Not a change to `search_files`.** The local agent's tool keeps working exactly as it
  does. The retrieval plane is a second consumer of the same index, not a replacement.

---

## 3. What retrieval is today, and what it can and cannot do

`app/search.py` is SQLite FTS5 over the whole extracted text of each document, one row per
document, ranked by `bm25()`. It is genuinely good at what it does and two properties of it
decide most of this step:

**It returns documents, not passages.** A hit means "this file contains these words". The
model then calls `read_pdf` and reads the whole thing. That is fine for a 300-word invoice
and useless for a 90-page contract, because the whole thing does not fit in the window and
nothing says which page mattered.

**It cannot match a paraphrase.** `search_files` already says so out loud in its own
result payload — *"This is a text match, not a filter"* — and that honesty is the reason
the gap is worth closing rather than papering over.

**What the window allows, measured on this box rather than assumed.** This is the number
that decides how much retrieval is worth doing, and it comes from `docs/models.md` and the
9 September tokenizer run, not from a rule of thumb:

| | tokens |
|---|---|
| `--max-model-len` (Qwen3-14B-AWQ) | 16,384 |
| Undroppable floor (system prompt 2,069 + 12 tool schemas ~2,144) | 4,213 |
| Left for the whole conversation | **12,171** |
| A sane retrieval budget inside that | **~4,000** |

**So the interesting derivation: Anthropic's published advice is that under ~200,000 tokens
you should skip RAG and put the whole corpus in the prompt. This deployment cannot. Its
whole-corpus ceiling is about 12,000 tokens, not 200,000** — a factor of sixteen earlier.
Retrieval starts earning its keep here at roughly **ten thousand tokens of documents**,
which is perhaps twenty ordinary PDFs. That is a much lower bar than the industry number
and it is the honest justification for building this at all.

**And the uncomfortable fact, recorded rather than hidden.** The bootstrap tenant's corpus
today is **11 documents totalling 1,768 characters — about 505 tokens.** Every one of them
is a test fixture written by a gate script. Nothing in this project has ever exercised
retrieval at a size where retrieval matters, and no amount of careful design substitutes
for that. **Decision 4.6 is about fixing this before anything else in the step is
believed**, and it is deliberately the decision with the least interesting content and the
most consequence.

---

## 4. Six decisions

### 4.1 What is the unit that comes back?

| Option | Shape | Verdict |
|---|---|---|
| a. Documents, as today | `{name, snippet, score}` | The status quo. It cannot answer "where in the contract", which is the question a retrieval plane exists for. |
| **b. Passages with a citation** | `{document, chunk_id, text, offsets, score}` | **Recommended.** The caller can quote it, show it, and check it. The offsets are what let a website highlight the source rather than assert it. |
| c. An answer | `{answer, sources}` | Crosses the boundary rule. It needs the conversation, and the conversation is not here. |

**Recommendation: (b).** And one thing that follows from it and is worth stating separately,
because it is the difference between a citation and a decoration: **the offsets must point
into the source document's extracted text, not into the chunk.** A chunk that says "chars
4,096–4,608 of `contract.pdf`" can be checked by anyone holding the file. A chunk that
knows only its own index cannot.

### 4.2 Where do chunks come from?

| Option | Cost |
|---|---|
| a. Chunk at query time | The same PDF re-split on every question, and a chunk boundary that moves when the code changes under a cache nobody invalidated. |
| **b. A Step 2 producer** | **Recommended.** Chunks are exactly what decision 4.1 of the Step 2 plan describes: something derived from a source file, by a named producer, at a declared version, which can be thrown away and made again. |
| c. Inside `search.py` | The arrangement Step 2 spent five sub-steps ending. |

**Recommendation: (b), and it is the reason Step 2 came first.** The chunker declares
`name="chunks"`, `version=1`, `handles={".pdf", ".xlsx", ".xlsm"}`, `slow=False`, and
consumes the **text artifact** rather than the source file — so it splits exactly what the
index indexed, and a document is never chunked from bytes the index never saw.

Three things fall out for free, and they are the whole argument:

- **Re-chunking is a version bump.** Changing the chunk size from 512 to 384 is
  `version=2`, and every document re-chunks on next ingest. That is the machinery from
  Step 2.4, already built and gated.
- **`status(name)` already answers "is this retrievable".** A document whose `chunks` row
  is `ok` is; one whose row is missing or failed is not, and says which.
- **A tenant delete already takes the chunks with it**, because `scripts/tenant.py` removes
  `derived/<tenant>/` wholesale. Step 2.5 paid for that.

### 4.3 How is a chunk made?

The evidence, because this is the one place in the step where the wider field has actually
measured things and the answer is not obvious:

| Strategy | Finding |
|---|---|
| Recursive character splitting, 512 tokens | Best end-to-end accuracy (69%) of seven strategies benchmarked over 50 academic papers, Feb 2026. Fast and cheap. |
| Semantic chunking | Sometimes better recall for dense retrieval, but produces chunks too small or too large for BM25 to score well — and it needs an embedding model at ingest time, which this project does not have until Step 5. |
| Overlap | Contested. Industry practice says 10–25%; a Jan 2026 systematic analysis found no measurable benefit and only added indexing cost. |

**Recommendation: recursive splitting, target 512 tokens, 64 tokens of overlap.**

- **512** because it is the benchmarked default and because it divides the ~4,000-token
  retrieval budget derived in section 3 into **eight passages**, which is a number a
  reader can actually look at.
- **Recursive**, meaning: split on paragraph breaks; if a piece is still too long, split on
  sentence ends; if still too long, split on whitespace; only then split mid-word. Structure
  is preferred to size, and size is the fallback.
- **64 tokens of overlap (12.5%)**, at the bottom of the industry range and deliberately so.
  The evidence for overlap is weak; the cost is small; a sentence cut in half at a boundary
  is a real failure and this is the cheapest insurance against it. **If a later measurement
  shows it buying nothing, it is a `version` bump to remove.**
- **Tokens are counted with the model's own tokenizer where the box is reachable**
  (`/tokenize`, already used on 9 September) and estimated at 3.5 characters per token where
  it is not. The estimator is already shared (`llm.estimate_prompt_tokens`) and a second one
  would drift, which is the reasoning Step 3 recorded when it made one estimator serve two
  paths.

**Deliberately not doing contextual retrieval yet, and designed so it is a version bump.**
Anthropic's method — an LLM writing 50–100 tokens of "where this chunk sits in the document"
and prepending it before indexing — is the single best-evidenced improvement in the field:
failure rates 5.7% → 3.7% with contextual embeddings, → 2.9% adding contextual BM25, → 1.9%
adding reranking. It costs about $1.02 per million document tokens against a hosted model,
and near-nothing against a GPU already sitting here. **It is the obvious Step 5.x upgrade,
it needs a model call per chunk, and it lands as `chunks` version 2.** Building the producer
now with that in mind is most of the work of adopting it later.

### 4.4 How do several retrievers combine?

They will not for a while — there is one — but the shape decided now is what Step 5 plugs
into, and getting it wrong means Step 5 rewrites this instead of extending it.

| Option | Verdict |
|---|---|
| a. Blend the scores | Wrong, and confidently so. BM25 is an unbounded positive number; cosine similarity is −1 to 1. Averaging them is arithmetic on incompatible units, and the result is dominated by whichever scale happens to be larger. |
| b. One retriever wins, the other is a fallback | Loses the case hybrid exists for: a query that is half a product code and half a paraphrase. |
| **c. Reciprocal Rank Fusion** | **Recommended.** Throw the scores away, keep the positions. A document at rank *r* contributes `1/(k + r)`. Something both retrievers like accumulates from both and rises above anything only one liked. |

**Recommendation: (c), with k = 60 and a per-retriever weight defaulting to 1.0.**

```
score(chunk) = Σ  weight[retriever] / (60 + rank[retriever][chunk])
```

Two properties make this the right thing to build with one retriever in the list:

- **Fusion of a single ranked list is that list, in order.** So Step 4 ships RRF that
  provably does nothing, and Step 5 turns it on by appending to a list. This is exactly the
  2.1 pattern — build the machinery, prove it against known-good behaviour, then move the
  interesting thing behind it — and 2.1 is the sub-step where the gate caught two real bugs.
- **k = 60 is the value from the original RRF paper and every implementation since.** It is
  not tuned here and should not be, until something measures it.

Expected payoff, for the record, so Step 5 can be checked against it rather than assumed:
hybrid retrieval plus reranking measured **Recall@5 = 0.816 and MRR@3 = 0.605** against
single-stage methods in a 2026 benchmark, roughly nine points of MRR over semantic-only.
**This step will not see any of that**, because it has one retriever. Its job is to make
those numbers measurable when the second one arrives.

### 4.5 How does a foreign tenant id become a local one?

**And this is a prerequisite, not a detail: the bridge does not exist.** `docs/architecture.md`
§6 designs a `tenant_alias` table and marks it PLANNED; the control plane holds `tenants`,
`tokens`, `users`, `tenant_database` and `meta`, and nothing else. **Nothing on this plane
can be tenant-scoped until it is built**, and the retrieval plane is meaningless untenanted.

The rule it exists to enforce is already written down and is the sharpest one in the
project: **a foreign id must never become a filesystem path.** `context.validate_tenant_id`
requires `^[a-z][a-z0-9_-]{0,31}$` and that value becomes a directory name. The website's
tenant is a row id from its own schema, chosen by a system that has never heard of this
constraint.

| Option | Verdict |
|---|---|
| a. Validate the foreign id and use it directly | One regex between another system's primary key and this filesystem. It would hold until the day their ids change shape. |
| b. Hash the foreign id into a local one | No lookup table to keep, and no way to answer "whose folder is this" from the folder name. |
| **c. An explicit `tenant_alias` table** | **Recommended, and already designed.** `(external_system, external_id) -> local tenant id`, the local id generated here by `tenancy.new_id()`. |

**Recommendation: (c), with an unlinked id answering 404 and not 403.** "That tenant exists
but is not yours" confirms an id someone guessed, and over enough guesses it counts another
customer's tenants. The job lane already set this precedent (`jobs.Lane.get`) and the
retrieval plane inherits it rather than re-deciding it.

### 4.6 What corpus is any of this measured against?

**The decision with the least interesting content and the most consequence.** Section 3's
number: the bootstrap tenant holds 505 tokens of test fixtures. A retrieval plane gated
against that proves nothing, and would pass while being useless.

| Option | Verdict |
|---|---|
| a. The client's real documents | They are a customer's. They are not in the repository and must not be, and a gate that cannot run on a laptop is a gate that stops being run. |
| b. Generate a corpus with the model | Reproducible only if the model is pinned, and the model is the thing under test in half of these checks. |
| **c. A committed synthetic corpus with a hand-written golden set** | **Recommended.** Perhaps 40–60 documents of a few pages each, deliberately containing near-duplicates, shared vocabulary and a few rare exact strings; plus 25–30 queries whose correct document is written down. |

**Recommendation: (c), and it is sub-step 4.1 rather than an afterthought.** It has to hold
the three things that break retrieval, because a corpus that lacks them makes any method
look good:

- **Near-duplicates**, so precision means something. `check_search.py` already does this
  with its 40 decoy invoices and it is the reason that gate is worth running.
- **Rare exact strings** — reference numbers, part codes, surnames — which is what keyword
  retrieval wins and dense retrieval loses.
- **Paraphrase targets**, where the query shares no content word with the document. These
  will **fail** in Step 4 and are written now anyway, because they are how Step 5's benefit
  gets measured rather than asserted. A golden set that the current system passes completely
  is a golden set that cannot show an improvement.

Metrics: **Recall@k and MRR**, both standard, both computable without a judge model.

---

## 5. The design, concretely

```
data/<tenant>/                              source documents, unchanged
derived/<tenant>/manifest.sqlite3           what has been produced, unchanged
derived/<tenant>/text/<file>/text.txt       Step 2.1, unchanged
derived/<tenant>/chunks/<file>/chunks.json  NEW: the passages
index/<tenant>.sqlite3                      FTS5, gains a `chunks` table
control/control.sqlite3                     gains `tenant_alias`
```

### The chunk

```python
@dataclass(frozen=True)
class Chunk:
    document: str       # the filename in data/<tenant>/
    ordinal: int        # 0, 1, 2 ... within the document
    text: str
    start: int          # character offset into derived/<tenant>/text/<file>/text.txt
    end: int
    tokens: int         # counted, or estimated, and which is recorded
```

`chunk_id` is `f"{document}#{ordinal}"`. Stable across a re-chunk at the same producer
version, and deliberately **not** stable across a version bump — because it is not the same
passage any more, and a citation that survives its own text changing is worse than one that
breaks.

### The FTS5 side

A second virtual table in the same per-tenant index file, beside `documents`:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    chunk_id UNINDEXED,
    document,
    text,
    ordinal UNINDEXED,
    start UNINDEXED,
    end UNINDEXED,
    tokens UNINDEXED,
    tokenize = 'porter unicode61'
);
```

`document` is indexed as well as `text` so a filename term still matches, which is what
users type. Same file as the document index because it is the same cache with the same
disposability rule, and a second file would be a second thing to forget to delete.

### The retriever interface

```python
@dataclass(frozen=True)
class Hit:
    chunk_id: str
    rank: int           # 1-based, within this retriever

class Retriever(Protocol):
    name: str
    def search(self, query: str, limit: int) -> list[Hit]: ...
```

**A retriever returns ranks, not scores.** Not a simplification — it is decision 4.4
expressed in the type. A retriever that cannot leak its scores into the fusion cannot break
the fusion by having a different scale, and the next person cannot "improve" it by blending.

`app/retrieve.py`:

```python
def register(retriever: Retriever, weight: float = 1.0) -> None
def fuse(results: dict[str, list[Hit]], weights: dict[str, float]) -> list[str]
def retrieve(query: str, *, k: int = 8, budget_tokens: int | None = None) -> dict
```

### The wire contract

```
POST /api/v1/retrieve
X-Syslab-Tenant: <external id>
Authorization: Bearer <service token>

{ "query": "what were the payment terms on the Meridian contract",
  "k": 8,
  "budget_tokens": 4000,
  "documents": ["meridian_contract.pdf"] }      # optional filter
```

```json
{ "query": "...",
  "retrievers": ["keyword"],
  "passages": [
    { "chunk_id": "meridian_contract.pdf#12",
      "document": "meridian_contract.pdf",
      "ordinal": 12,
      "text": "Payment falls due thirty days from invoice date ...",
      "start": 6144, "end": 6698, "tokens": 138,
      "rank": 1,
      "found_by": ["keyword"] }
  ],
  "tokens_returned": 138,
  "truncated": false,
  "what_this_means": "These passages CONTAIN or RESEMBLE the words asked about..." }
```

Four things in that payload are load-bearing:

- **`found_by`** names which retrievers ranked it. When Step 5 lands, this is how anyone
  can see whether the vector side is contributing anything, without instrumenting the box.
- **`tokens_returned` and `truncated`** exist because the caller has a 12,171-token
  conversation budget and needs to know what it just spent. `truncated` says "the budget
  stopped this", never "that was all there was" — the distinction Step 2 learned when a
  skipped file and an unreadable file were told apart.
- **`what_this_means`** is carried over verbatim in spirit from `search_files`, which
  already says a text match is not a filter. A retrieval result reads like an answer, and
  it is not one.
- **No score.** Ranks and `found_by`, nothing else. A float labelled "score" invites a
  caller to threshold on it, and it would mean something different the day a second
  retriever joins.

### The frozen surface

**`/api/v1/*` gets frozen the way `/v1` is, and for the same reason.** Step 3.5 froze the
inference plane because the website deploys separately, so a narrowing here is found by a
customer rather than by a test. Every word of that applies to the retrieval plane. It
extends `docs/api/gateway-v1.released.json` and `scripts/check_api_compat.py` rather than
inventing a second mechanism.

The local `/api/*` surface stays unfrozen. It is this install's own admin surface and its
caller ships with it.

---

## 6. Step by step

**4.0 The tenant bridge.** `tenant_alias` in the control plane, `tenancy.resolve_alias`,
and a dependency that sets the tenant from `X-Syslab-Tenant`. Nothing else in this step can
begin. Gate: an unlinked external id is **404 and not 403**; a foreign id shaped like
`../../etc` or `Acme Ltd` never reaches `validate_tenant_id` as a path; `check_isolation`
grows a section reaching the retrieval plane as one tenant and getting nothing of another's.

**4.1 The corpus and the golden set.** `tests/fixtures/corpus/` and a committed
`golden.json`: 25–30 queries, each with the document that answers it and, where it is
unambiguous, the passage. Plus `scripts/check_retrieval.py` reporting Recall@k and MRR.
Gate: it runs, and **it reports today's document-level keyword numbers before any of this
step's code exists.** A baseline measured after the change is not a baseline.

**4.2 The chunk producer.** `producers.CHUNKS`, consuming the text artifact, writing
`chunks.json`. Gate: `check_ingest` grows a section — chunks are produced, offsets resolve
back to the real text, a version bump re-chunks everything, and deleting `derived/` and
rebuilding gives byte-identical chunks. Determinism is the property worth gating: a chunker
that splits differently on Tuesday invalidates every citation ever issued.

**4.3 The chunk index and the keyword retriever.** The `chunks` FTS5 table, populated by
`intake` as a second consumer beside the document index. Gate: `check_retrieval` shows
passage-level numbers against 4.1's baseline. **They may be worse on some queries** — a
512-token chunk has less context than a whole document for BM25 to score — and that is
information, not a failure. Record it.

**4.4 Fusion.** `app/retrieve.py`, RRF, one retriever registered. Gate: fusing one list
returns that list in that order, asserted directly; `check_retrieval`'s numbers are
**identical** to 4.3's, because nothing has changed yet. An RRF that moves a single list is
broken and this is the only moment it is cheap to notice.

**4.5 `POST /api/v1/retrieve`.** The contract in section 5, the token budget, the filter.
Gate: the budget is respected and `truncated` is honest; a request for another tenant's
document filters to nothing rather than erroring informatively; `check_remote` grows a
retrieval section.

**4.6 The rest of the plane.** `GET /api/v1/documents`, `GET /api/v1/documents/{name}`,
`POST /api/v1/ingest/{name}` — thin tenant-scoped wrappers over what Step 2.3 already
built. Gate: `check_api_compat` freezes `/api/v1/*`, and the frozen file is committed.

---

## 7. Risks, named

| Risk | Mitigation |
|---|---|
| The golden set is written to make the current system look good | 4.1 writes it **before** the chunker, and deliberately includes paraphrase queries known to fail today |
| Chunk-level retrieval is worse than document-level for some queries | Expected. 4.3's gate records both rather than asserting an improvement. If it is worse overall, that is a finding and the step stops to think |
| The chunker is not deterministic | Gated in 4.2 explicitly, because every citation depends on it |
| `/api/v1/*` is frozen too early, around a shape Step 5 needs to change | `found_by` and the absent score are the two places Step 5 will push. Both were chosen for that. A field may be **added**; the freeze forbids narrowing, not growth |
| The tenant bridge becomes a path | 4.0's gate, and the local id is generated here by `tenancy.new_id()` and never derived from theirs |
| Scope creeps into embeddings | The word "embedding" does not appear in any sub-step's gate. Step 5 exists |
| sqlite-vec is pre-v1 with expected breaking changes | Step 5's problem, but worth knowing now. It is **brute-force rather than ANN** — fine at this scale, and a fact to design around rather than discover. Its repository states dual Apache-2.0/MIT, but that reading came through a summarising fetch, which `docs/licences.md` explicitly refuses as a primary source; the row there stays `unverified` and now says what to expect |

## 8. Effort

Four to five sessions. **4.1 is the one that will take longest and feel least like
progress**, and it is the one that decides whether anything after it can be believed. 4.0
is small but blocking and touches the control plane, which is the one directory in this
project worth backing up. 4.4 is an afternoon.

## 9. Rollback

Every sub-step is additive. `derived/<tenant>/chunks/` deletes and rebuilds like everything
else under `derived/`; the `chunks` FTS5 table drops without touching `documents`;
`/api/v1/*` unmounts without affecting `/v1` or `/api/*`; `tenant_alias` is a table nothing
else reads. The local agent's `search_files` is untouched throughout, so the offline path
keeps working even if the whole plane is withdrawn.

## 10. What comes after

**Step 5** registers a second retriever and the fusion starts doing something. The
measurable claim to check it against is in decision 4.4: roughly nine points of MRR from
hybrid over semantic-only, and Recall@5 around 0.82 with reranking. `models.toml` — deferred
from Step 3.4 — finally gets its second row, because there is finally a second model.

**Note for Step 5, from the research and worth writing down now:** the embedding model
needs its own vLLM instance. `--runner pooling` is not a mode the generative server can
also be in, so `Qwen3-Embedding-0.6B` is a second container, not a second flag. At 1024
dimensions and Matryoshka-truncatable down to 32, and with about 9.4 GiB of VRAM free since
the 14B swap, it fits — but it is a compose change and a boot-order question, not a
one-liner.

**Step 5.x, and the best-evidenced single improvement available:** contextual retrieval, as
`chunks` version 2. The machinery to adopt it is entirely built by then — bump the version,
every document re-chunks, the manifest tracks it, and nothing else changes.
