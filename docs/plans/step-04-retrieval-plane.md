# Step 4: The Retrieval Plane, and the Seams Everything Else Plugs Into

Status: **IN PROGRESS.** 4.0 is built and gated. **Rewritten 11 September 2026**, after the
requirements turned out to be wider than the first draft assumed. **All seven decisions
signed off the same day**, 5.1 to 5.6 as recommended and 5.7 amended by Amro — see below,
because the amendment is the best thing that happened to this plan.

4.0 stands unchanged and is done — the tenant bridge is needed under every version of this.
Everything from 4.1 onward is new.

---

## 1. Why this plan was rewritten

The first draft assumed text-only PDFs and spreadsheets, one way in, and questions answerable
from a handful of passages. The actual requirement is:

- Customer data in **SQL databases, Google Docs and other cloud services**, not only files
- **Many formats, including media**, and *"it seems stupid to hardcode all data types"*
- **OCR from images**
- Questions like *"which vendors has this client used across all their contracts, ranked by
  spend"* and *"connect the dots across 200 contracts"*
- *"find the chart that shows Q3 revenue"*
- Speech to text and text to speech, possibly Kokoro
- All of it **per customer, in isolation**

And one instruction that shapes the whole document more than any of the above:

> **"I want to build the skeleton that allows for all these systems to be added as features
> later on. Build this right, focused on scalability."**

So this step does not build vision, speech, connectors or graph reasoning. **It builds the
four places they attach**, and proves each one by putting the cheapest possible thing in it.
The measure of success is not what this step can do. It is **how little has to change when
the next thing arrives.**

That is the same bet Step 2 made and won: vision, embeddings and field extraction each
became a *producer* rather than a fourth reader of the same PDF.

---

## 2. What this step is

**A tenant-scoped HTTP surface that answers "which parts of this customer's material bear on
this question", returns them with enough citation to check, and is built so that a new
source, a new format, a new retriever or a new model is a registration rather than a
rewrite.**

## 3. What this step is not

- **Not vision, speech, OCR or cloud connectors.** Each gets a named slot in section 7 and
  a VRAM budget in section 8. None is built here.
- **Not an answer.** This plane returns passages and rows, never prose. The boundary rule in
  `README.md` holds: *this server never learns what a conversation is.* Query rewriting from
  conversation history and answer synthesis are the website's, because they need the turn
  before this one.
- **Not a change to `search_files`.** The local agent's tool keeps working exactly as it does.

---

## 4. The skeleton: four seams, and the two answer paths

### The four seams

```
        SOURCE  ──►  PRODUCER  ──►  RETRIEVER  ──►  /api/v1/retrieve
          │             │              │
      where data    what gets      how a question
      comes from    derived        finds material
          │             │              │
        files       text, chunks,   keyword
        (today)     fields          (today)
          │             │              │
        SQL,        OCR, page       vector, structured,
        Google      images,         graph
        Docs,       embeddings      (later)
        cloud       (later)
        (later)
                          MODEL ROLE
                    which model does what
                    chat (today) │ embed, vision, stt, tts (later)
```

| Seam | Exists? | What it is | What plugs in later |
|---|---|---|---|
| **Source** | **No — 4.2 builds it** | Where a tenant's material comes from | Customer SQL, Google Docs, Drive, cloud storage |
| **Producer** | **Yes — Step 2 built it** | What is derived from a source, at a declared version | OCR, page images, extracted fields, embeddings |
| **Retriever** | **No — 4.6 builds it** | How a question finds material, fused by rank | Vector search, structured lookup, graph |
| **Model role** | **No — 4.7 declares it** | Which model fills which job | Embedding, vision, STT, TTS |

The producer seam already works and is gated, which is the evidence that this shape is
worth repeating. `Producer` declares `name`, `version`, `handles` and `slow`; `ingest.py`
records what was asked for, whether it worked, and against which version; a version bump
re-derives everything; and a tenant delete takes it all with it.

### The two answer paths, and why one of them is not RAG

This is the most important thing in the document.

**Path A — passages.** *"What do the documents say about the Meridian payment terms?"*
Retrieval finds the passages that bear on it, returns them with citations, and the website
turns them into an answer. This is RAG and this step builds it.

**Path B — facts.** *"Which vendors has this client used across all their contracts, ranked
by spend?"*

**Retrieval cannot answer this, and will not say so.** It finds the top-k relevant chunks —
eight of them — hands them to a model, and the model produces a confident, well-formatted
ranking **derived from 4% of the contracts**. No error is raised. It looks exactly like a
correct answer.

This project has already been bitten by this exact failure. `HANDOVER.md`, on the deployed
site: *"fabricates rows without calling a tool, and the UI renders that as a result grid —
so the gate passes on the screen while nothing has run."*

The answer to a question about *all* the data is not better retrieval. It is **structured
extraction at ingest time**: a producer pulls `(vendor, amount, date, document)` out of each
contract into a real table as it arrives, and the question is answered with **SQL** — exact,
complete, and checkable against the rows. `app/db.py` already runs read-only SQL with a
four-layer safety model, and `run_sql` is already a tool the model has.

**Both paths are built on the same seams.** The extraction producer is a producer. The fact
table is a retriever of a different shape. What this step must get right is that **a question
needing Path B never gets silently answered from Path A** — decision 5.6.

---

## 5. Seven decisions

### 5.1 What is the unit that comes back?

| Option | Verdict |
|---|---|
| a. Documents, as today | Cannot answer "where in the contract", which is the question a retrieval plane exists for |
| **b. Passages with a citation** | **Recommended.** `{source, chunk_id, text, offsets, score}` — quotable, showable, checkable |
| c. An answer | Crosses the boundary rule. Needs the conversation, which is not here |

**Recommendation: (b). SIGNED OFF**, with an instruction attached: *"just tell the customer
the information he needs only."*

That is a constraint on the response, not a comment on it, and it shows up in three places:
`k` defaults low rather than high, `budget_tokens` is respected rather than advisory, and a
passage is returned at chunk size rather than padded with its neighbours. **Returning more
than was asked for is not generosity here** — it is spending the caller's 12,171-token
conversation budget on their behalf, and it is how a retrieval plane starts crowding out the
conversation it exists to serve.

And the part that separates a citation from a decoration: **the offsets point into the
source's extracted text, not into the chunk.** "Characters 4,096–4,608 of `contract.pdf`" can
be verified by anyone holding the file. A chunk that knows only its own index cannot.

### 5.2 Where does format-agnostic parsing come from? *(new)*

The requirement is explicit: many formats, media included, and no hardcoded list. Today
`producers.py` has `SEARCHABLE = {".pdf", ".xlsx", ".xlsm"}` and hand-rolled readers.

| Option | Verdict |
|---|---|
| a. Keep adding readers per format | Does not scale to "I cannot list you all data types", and each new format is a code change |
| b. Write one abstraction over several parsers ourselves | Rebuilding a solved problem. Nobody should hand-write an `.odt` parser |
| **c. Adopt a document-parsing library behind ONE producer** | **Recommended.** Formats become a property of the library, not of our code |

**Recommendation: (c) with Docling. SIGNED OFF, and the licence is VERIFIED** — MIT, read
from the project's own LICENSE file by Amro on 11 September 2026 and pasted in full. That is
a primary source under `docs/licences.md`'s rule, unlike the fetch-and-summarise reading that
left `sqlite-vec` unverified. The row is green.

**One thing the MIT does not cover, and 4.2 must not skip it.** Docling **downloads models at
runtime** — layout, table structure, OCR — and *those carry their own terms*. The library
being clear is necessary and not sufficient. 4.2's gate includes listing the models Docling
actually pulls on this box and reading their terms the same way. An OCR engine is the one
most likely to surprise; some are GPL.

Three things follow:

- **It is a producer, not a framework.** It sits behind `Producer(name="text", version=2)`,
  the ingestion contract is unchanged, and a version bump re-derives every document.
- **`handles` stops being a hardcoded set** and starts being "what the library reports it can
  read", so a new format is a library upgrade.
- **It may retire PyMuPDF, which is AGPL-3.0** and the most serious licence risk in
  `docs/licences.md`. That is a real secondary win, not the reason.

### 5.3 Where do chunks come from?

| Option | Verdict |
|---|---|
| a. Chunk at query time | The same document re-split on every question, with boundaries that move under a cache nobody invalidated |
| **b. A Step 2 producer** | **Recommended.** Exactly what the producer seam is for |
| c. Inside `search.py` | The arrangement Step 2 spent five sub-steps ending |

**Recommendation: (b). SIGNED OFF.** `name="chunks"`, consuming the **text artifact** rather than the
source, so it splits exactly what was indexed and a document is never chunked from bytes the
index never saw.

### 5.4 How is a chunk made?

**Recursive character splitting, target 512 tokens, 64 of overlap. SIGNED OFF.** 512 benchmarked best of
seven strategies over 50 academic papers (Feb 2026) and divides the ~4,000-token retrieval
budget into **eight passages**. Overlap at 12.5% is at the bottom of the industry range
deliberately — the evidence for it is weak, the cost is small, and a sentence cut in half at
a boundary is a real failure. **If a measurement shows it buying nothing, it is a version
bump to remove.**

Tokens counted with the model's own tokenizer where the box is reachable, estimated at 3.5
characters per token where it is not, using the shared `llm.estimate_prompt_tokens`.

**Contextual retrieval is deliberately deferred and designed for.** Anthropic's method — an
LLM writing 50–100 tokens of "where this chunk sits" before indexing — is the best-evidenced
improvement in the field: failure rates 5.7% → 3.7% → 2.9% → 1.9%. It needs a model call per
chunk and lands as `chunks` version 2. Building the producer now with that in mind is most
of the work of adopting it later.

### 5.5 How do several retrievers combine? *(the retriever seam)*

| Option | Verdict |
|---|---|
| a. Blend the scores | Wrong, confidently. BM25 is unbounded; cosine is −1 to 1. Averaging them is arithmetic on incompatible units |
| b. One wins, the other is a fallback | Loses the case hybrid exists for: half a product code, half a paraphrase |
| **c. Reciprocal Rank Fusion** | **Recommended.** Throw the scores away, keep the positions |

**Recommendation: (c), k = 60, per-retriever weight defaulting to 1.0. SIGNED OFF.**

```
score(chunk) = Σ  weight[retriever] / (60 + rank[retriever][chunk])
```

**A retriever returns ranks, not scores** — the decision expressed in the type. A retriever
that cannot leak its scale into the fusion cannot break it, and the next person cannot
"improve" it by blending.

**Fusion of a single ranked list is that list, in order.** So this step ships RRF that
provably does nothing, and the vector retriever turns it on by appending to a list. That is
the 2.1 pattern — build the machinery, prove it against known-good behaviour, then move the
interesting thing behind it — and 2.1 is where the gate caught two real bugs.

### 5.6 How is a question that needs all the data kept out of the passage path? *(new)*

The failure in section 4 is not hypothetical and it is not detectable by the caller.

| Option | Verdict |
|---|---|
| a. Trust the model to notice | It will not. A confident ranking from 8 of 200 contracts is indistinguishable from a correct one |
| b. Raise `k` until it fits | 200 contracts do not fit in 12,171 tokens. This fails silently at the exact moment it matters |
| c. Classify the question with a model call | Another model call, another thing to be wrong, and it needs the conversation |
| **d. Make the two paths separate tools, and make the passage path declare its own coverage** | **Recommended** |

**Recommendation: (d). SIGNED OFF, in two halves.**

- **Separate tools.** Path B is `run_sql` against an extracted fact table — a tool the model
  already has, returning exact rows. Path A is `retrieve`, returning passages. The model
  chooses, the same way it already chooses between `search_files` and `read_pdf`.
- **The passage response states its own coverage**, and this is the load-bearing half:
  `searched`, `matched` and `returned` counts, plus `truncated`. A caller — or a reader of
  the trace — can see that a question about 200 contracts was answered from 8 passages. The
  response also carries `what_this_means`, carried over in spirit from `search_files`, which
  already says out loud that a text match is not a filter.

**This does not fully solve it and the plan should not claim it does.** It makes the shortfall
*visible* rather than invisible, which is the difference between a bug someone can find and
one nobody can. Properly routing aggregate questions is its own step, named in section 9.

### 5.7 What corpus is any of this measured against?

**The decision with the least interesting content and the most consequence — and the one
Amro amended, correctly.** The recommendation was to hand-build 40–60 documents and 25–30
queries. The amendment: *"Can we download a small dataset that has this rather than building
it? Surely there has to be something."*

There is, and it is better than what would have been built by hand.

The problem stands: the bootstrap tenant holds **11 documents, 1,768 characters, about 505
tokens** — re-measured 11 September, every one a fixture written by a gate script. A
retrieval plane gated against that proves nothing and would pass while being useless.

| Option | Verdict |
|---|---|
| a. The client's real documents | A customer's. Not in the repository, and a gate that cannot run on a laptop stops being run |
| b. Generate a corpus with the model | Reproducible only if the model is pinned, and the model is under test in half these checks |
| c. Hand-build 40–60 documents and a golden set | What the first draft said. Slow, and the hand-written questions would be written by the same person who knows how the retriever works |
| **d. Adopt CUAD v1, subsampled, and derive the golden set from its labels** | **Recommended, and SIGNED OFF** |

**Recommendation: (d). CUAD v1 — the Contract Understanding Atticus Dataset.** 510 real
commercial contracts, drawn from public EDGAR filings, with **13,000+ annotations by lawyers
across 41 clause categories**. Stated CC BY 4.0, commercial use permitted.

Why it beats what 4.1 was going to build, point by point against the requirements the first
draft set for itself:

| What the corpus must hold | CUAD gives it |
|---|---|
| Real documents, not fixtures | **510 PDFs of genuine commercial contracts**, plus a TXT of each |
| Several formats | PDF and TXT of every contract, plus 28 Excel files of labels — the parsing seam gets exercised by the thing that grades it |
| Near-duplicates, so precision means something | Commercial contracts of the same type share boilerplate heavily. This is free and realistic |
| Rare exact strings | Party names, dates, dollar amounts, section numbers, throughout |
| A golden set | **Expert-annotated spans**, not questions invented by the person who built the retriever |
| **Ground truth for an aggregate question** | **The 41 clause categories across 510 contracts.** "How many of these contracts have an exclusivity clause" has a *known, countable* answer. This is the Path B test case with real ground truth, and it is the thing I did not expect to get |

That last row is the reason this is a better decision and not merely a faster one. Decision
5.6 says a question about all of something must not be silently answered from eight passages.
**Proving that requires a question whose true answer is known.** Hand-writing one means
hand-writing the answer too. CUAD's labels supply both.

**Three honest caveats, none fatal.**

- **CUAD is an extraction benchmark, not a retrieval one.** Its questions are per-contract
  ("highlight the parts related to exclusivity in *this* contract"), so used as-is they name
  the document and make retrieval trivial. **4.1 must construct retrieval queries from the
  annotated spans** — take a clause, ask a question it answers, and the correct result is
  that contract and that span. That is real work, but it is *transcription with ground truth
  attached* rather than invention.
- **It is legal contracts, not the client's asset-removal records.** Retrieval quality on
  contracts is not a promise about quality on their data. The corpus proves the *machinery*
  and the *metrics*; only the client's own documents prove the fit, and those cannot live
  here.
- **510 is too many and the PDFs are not small.** 4.1 commits a **subsample of 40–60**, kept
  under roughly 25 MB, with the CC BY 4.0 attribution in the corpus folder. Committed rather
  than downloaded on demand, because a gate that needs the network is a gate that stops
  running on a laptop — the same reasoning that keeps every other fixture in the repository.

**Still hand-made, and deliberately: a small format pack.** CUAD is PDF, TXT and XLSX. The
format seam in 5.2 claims more than that, so 4.1 adds perhaps five files — a `.docx`, a
`.pptx`, an `.html`, a scanned image, and one deliberately corrupt file — to prove the parser
handles them *and* fails honestly on the last one. Five files is an afternoon, against the
forty the first draft asked for.

**Paraphrase queries are still written by hand**, perhaps eight of them, and still expected
to **fail** in this step. They are how Step 5's benefit gets measured rather than asserted. A
golden set the current system passes completely cannot show an improvement.

**The licence is not yet verified and that blocks nothing yet.** CC BY 4.0 is what the
Atticus Project states, but that reading came through a search result, which
`docs/licences.md` excludes exactly as it excluded sqlite-vec's. Somebody opens the licence
before 4.1 commits a single PDF. The row is recorded.

Metrics unchanged: **Recall@k and MRR**, both standard, both computable without a judge model.

**Effect on effort: 4.1 stops being the longest sub-step.** The first draft called it the one
that "will take longest and feel least like progress". Adopting CUAD removes the document
authoring entirely and replaces question invention with question construction against
existing labels. Section 11 is revised down.

---

## 6. The design, concretely

```
data/<tenant>/                              source material, unchanged
derived/<tenant>/manifest.sqlite3           what has been produced, unchanged
derived/<tenant>/text/<item>/text.txt       Step 2.1; producer swapped in 4.2
derived/<tenant>/chunks/<item>/chunks.json  NEW: the passages
index/<tenant>.sqlite3                      FTS5, gains a `chunks` table
control/control.sqlite3                     tenant_alias (4.0, done)
models.toml                                 NEW: which model fills which role
```

### The source seam

```python
@dataclass(frozen=True)
class Source:
    name: str                   # "files", later "postgres", "gdrive"
    def list(self) -> list[Item]: ...
    def fetch(self, item_id: str) -> Path: ...
```

`files` is the only one implemented, and it is today's behaviour moved behind the interface
and **not rewritten** — the same discipline as Step 2.1, whose gate was that `git diff` on
the consumer stayed empty. A connector arriving later registers a `Source` and produces into
the same pipeline, with the same manifest and the same tenant delete.

**Credentials are the real blocker and are named, not hidden.** Any source that is not local
files needs *that customer's* credentials stored encrypted. `tenancy.database_for()` raises
for any row it finds because **no cipher was ever chosen** — Step 1's decision 4.5, still
open, `tenant_database` still empty. **Every cloud connector sits behind it**, and it is now
on the critical path rather than a footnote. Section 9 gives it a step of its own.

### The chunk and the retriever

```python
@dataclass(frozen=True)
class Chunk:
    source: str         # the item in data/<tenant>/
    ordinal: int
    text: str
    start: int          # character offset into the text artifact
    end: int
    tokens: int

@dataclass(frozen=True)
class Hit:
    chunk_id: str
    rank: int           # 1-based, within this retriever

class Retriever(Protocol):
    name: str
    def search(self, query: str, limit: int) -> list[Hit]: ...
```

`chunk_id` is `f"{source}#{ordinal}"`, stable across a re-chunk at the same version and
deliberately **not** across a version bump — it is not the same passage any more, and a
citation that survives its own text changing is worse than one that breaks.

### The wire contract

```
POST /api/v1/retrieve
X-Syslab-Tenant: <external id>
Authorization: Bearer <service token>

{ "query": "what were the payment terms on the Meridian contract",
  "k": 8, "budget_tokens": 4000, "sources": ["meridian_contract.pdf"] }
```

```json
{ "query": "...",
  "retrievers": ["keyword"],
  "passages": [
    { "chunk_id": "meridian_contract.pdf#12",
      "source": "meridian_contract.pdf",
      "text": "Payment falls due thirty days from invoice date ...",
      "start": 6144, "end": 6698, "tokens": 138,
      "rank": 1, "found_by": ["keyword"] }
  ],
  "coverage": { "searched": 214, "matched": 31, "returned": 8 },
  "tokens_returned": 138,
  "truncated": false,
  "what_this_means": "These passages CONTAIN or RESEMBLE the words asked about. This is a
                      sample, not a census: 8 of 31 matching passages were returned. Do not
                      answer a question about ALL of something from this." }
```

Five things are load-bearing:

- **`found_by`** names which retrievers ranked it — how anyone sees whether the vector side
  contributes, without instrumenting the box.
- **`coverage`** is decision 5.6 made visible. It is the difference between a wrong aggregate
  answer nobody can detect and one anybody can.
- **`tokens_returned` / `truncated`** — the caller has a 12,171-token conversation budget and
  needs to know what it just spent. `truncated` says "the budget stopped this", never "that
  was all there was".
- **`what_this_means`** — a retrieval result reads like an answer and is not one.
- **No score.** A float labelled "score" invites thresholding, and it would mean something
  different the day a second retriever joins.

**`/api/v1/*` is frozen** the way `/v1` is, through `docs/api/gateway-v1.released.json` and
`scripts/check_api_compat.py`. The website deploys separately, so a narrowing is found by a
customer rather than by a test. A field may be **added**; the freeze forbids narrowing.

---

## 7. Where the extra models fit

Not built here. **Declared here**, so adding one is filling a row rather than designing a
mechanism. This is `models.toml`, deferred from Step 3.4 because one model gave it nothing to
hold — it now has five roles to hold.

```toml
[roles.chat]      # filled: Qwen3-14B-AWQ, 16384, via vLLM
[roles.embed]     # empty
[roles.vision]    # empty
[roles.stt]       # empty
[roles.tts]       # empty
```

**An empty value means unavailable, never a silent fallback.** That is the rule the file
exists for, and it is the same rule as `current_tenant()` raising rather than defaulting.

**This moves `models.toml` back out of Step 5**, where Step 3.4 deferred it — recorded in
`CHANGELOG.md` and `HANDOVER.md`, both updated. The reason for deferring was that one model
gave the file nothing to hold. That reason is gone: the file now holds the *shape* of four
models that have been asked for, and declaring the slot before filling it is the whole
request this plan exists to answer. It is the same order Step 2.0 used — the pipeline
existed, gated and unimported, before its first producer moved behind it.

| Role | What it unlocks | How it attaches | Rough VRAM |
|---|---|---|---|
| **embed** | Paraphrase matching — the second retriever | `Retriever` + an `embeddings` producer | ~2 GiB |
| **vision** | "Find the chart showing Q3 revenue"; OCR of scans | A `page_images` producer, then a captioning producer whose output is indexed as text | ~3–4 GiB |
| **stt** | Speech in | A producer over audio items | ~1–2 GiB, or CPU |
| **tts** | Speech out | Not a producer — a response path | ~0.3 GiB, CPU is fine |

**The VRAM arithmetic, and it is arithmetic, not a measurement.** 32 GiB card; the 14B at
`--gpu-memory-utilization 0.70` takes ~22 GiB; **~9.4 GiB free**, recorded in
`docs/models.md`. The four roles above come to roughly **6.5–8 GiB**. It fits, narrowly.

Two things that must not be glossed:

- **Each is a separate container**, not a flag. `--runner pooling` is not a mode the
  generative server can also be in, so the embedding model is a second vLLM instance — a
  compose change and a boot-order question.
- **`docs/models.md` already records a projection of this kind missing.** At
  `--gpu-memory-utilization 0.85` the estimate was 3.4x and the delivery was 2.58x, because
  vLLM's overhead grows with the budget. **Read the startup log and record the real numbers**
  before believing this table.

**The honest summary: they fit on paper, with no room for a bad estimate.** If vision turns
out to need a larger model, something moves to CPU or the chat model gets quantised further —
and that is a trade to make with measurements, not now.

---

## 8. Step by step

**4.0 The tenant bridge. DONE, 11 September 2026.** `tenant_alias`, `tenancy.resolve_alias`,
`app/plane.py`, `RETRIEVAL_TOKENS`, the CLI. Gate: `check_isolation` § 7b, 38 → 54 checks,
each written by breaking the property first — two were decoration until that run. Suite
426 → 471. Found and fixed two pre-existing bugs: `new_id()` generating ids the application
refuses one time in four, and a non-ASCII bearer token being a 500 rather than a 401.

**4.1 The corpus and the golden set.** A **40–60 contract subsample of CUAD v1** under
`tests/fixtures/corpus/`, under ~25 MB, with its CC BY 4.0 attribution beside it and its
licence verified first. A hand-made **format pack** of about five files — `.docx`, `.pptx`,
`.html`, a scanned image, and one deliberately corrupt file. A `golden.json` built by
**constructing retrieval queries from CUAD's annotated spans** (the dataset's own questions
name their contract, which would make retrieval trivial), plus about eight hand-written
paraphrase queries expected to fail. And **at least one aggregate question whose true answer
is countable from the clause labels.** Plus `scripts/check_retrieval.py` reporting Recall@k
and MRR.

Gate: it runs, and **it reports today's document-level keyword numbers before any of this
step's code exists.** A baseline measured after the change is not a baseline. The aggregate
question is recorded as **answered incompletely**, which is the number decision 5.6 exists to
make visible and Step 9 exists to fix.

**4.2 The parser and the source seam.** The `Source` protocol with `files` behind it, moved
and not rewritten; **Docling** adopted behind `text` version 2.

Gate: `check_ingest` grows a formats section; **every file in 4.1's corpus and format pack
ingests, and the corrupt one fails honestly rather than silently producing empty text** — the
distinction Step 2 paid for, where a missing parser and an unreadable file were told apart;
`git diff` on the consumers stays empty for the `files` move; a version bump re-derives
everything.

**And one gate that is not about code: the models Docling downloads are listed and their
terms read.** The library is MIT and verified; the layout, table-structure and OCR models it
pulls at runtime are not covered by that, and an OCR engine is the one most likely to be GPL.
`docs/licences.md` gets a row for each.

**Watch for, and record either way: does this retire PyMuPDF?** If Docling reads every format
the project needs, the AGPL-3.0 dependency that `docs/licences.md` calls the largest licence
risk here can go. That is a real prize and it is *not* a reason to declare it true — it has to
be demonstrated on the same documents, `check_search` and `check_tools` unchanged, the way
that file already specifies for a pypdfium2 swap.

**4.3 The chunk producer.** `producers.CHUNKS`, writing `chunks.json`. Gate: offsets resolve
back to the real text; a version bump re-chunks; deleting `derived/` and rebuilding gives
**byte-identical** chunks. Determinism is the property worth gating — a chunker that splits
differently on Tuesday invalidates every citation ever issued.

**4.4 The chunk index and the keyword retriever.** The `chunks` FTS5 table, populated by
`intake` as a second consumer beside the document index. Gate: `check_retrieval` shows
passage-level numbers against 4.1's baseline. **They may be worse on some queries** — a
512-token chunk has less context than a whole document for BM25 to score — and that is
information, not a failure. Record it.

**4.5 Fusion.** `app/retrieve.py`, RRF, one retriever registered. Gate: fusing one list
returns that list in that order, asserted directly; `check_retrieval`'s numbers are
**identical** to 4.4's, because nothing has changed yet. An RRF that moves a single list is
broken, and this is the only moment it is cheap to notice.

**4.6 `POST /api/v1/retrieve`.** The contract in section 6, the token budget, the filter, and
`coverage`. Gate: the budget is respected and `truncated` is honest; **`coverage` is correct
and provably so** — a query matching 31 passages and returning 8 says so; a request for
another tenant's source filters to nothing rather than erroring informatively.

**4.7 The model role registry.** `models.toml` with five roles, one filled. Gate: an empty
role is *unavailable* and never a silent fallback, asserted by asking for `embed` and getting
a refusal with a reason; `check_api_compat` freezes `/api/v1/*`.

**4.8 The rest of the plane.** `GET /api/v1/documents`, `GET /api/v1/documents/{name}`,
`POST /api/v1/ingest/{name}` — thin tenant-scoped wrappers over what Step 2.3 already built.

---

## 9. What comes after, and where it attaches

Named here so the sequencing is visible, and because two of these are prerequisites for
things already asked for.

**Steps 5 and 6 keep the meanings every other document already gives them** — embeddings and
speech. `docs/licences.md` says "verify sqlite-vec before Step 5" and "verify Kokoro before
Step 6", and renumbering to suit this plan would silently invalidate those and a dozen
references besides. **A step number is an identifier, not a position in a queue**; this
project has run Step 3 before Step 2 on purpose once already. New work takes new numbers,
and the *Do first* column says what actually blocks what.

| | What | Attaches at | Do first? |
|---|---|---|---|
| **Step 5** | **The second retriever** — embeddings, hybrid search | `Retriever` registry, `embed` role | After Step 4. The fusion must exist and provably do nothing first |
| **Step 6** | **Speech** — STT and TTS | `stt` / `tts` roles | Any time after 4.7. Licences already listed in `docs/licences.md` |
| **Step 7** | **Per-tenant credentials.** Choose a cipher, encrypt, key management | `tenancy.database_for()` | **The real blocker.** Nothing customer-cloud can start until it is done, and it is the most under-planned thing in the project. Worth doing before Step 5 if connectors matter more than paraphrase matching |
| **Step 8** | **Sources beyond files** — customer SQL, Google Docs, Drive | `Source` registry | Blocked on Step 7 |
| **Step 9** | **Structured extraction and Path B** — facts table, aggregate answers | A producer + `run_sql` | Needs 4.1's corpus to grade it. **The answer to the question RAG cannot answer** |
| **Step 10** | **Vision and OCR** | `vision` role + producers | Needs measured VRAM. OCR alone may arrive earlier, inside 4.2's parsing library |
| **Any time** | **Contextual retrieval** | `chunks` version 2 | Best-evidenced single improvement available; machinery fully built by then |

**Two scalability limits that are not on this list and should be.** Jobs are **in memory and
do not survive a restart** — ingesting a customer's Google Drive is exactly the long job that
makes that unacceptable. And the control plane is SQLite, which is right until there are
genuinely concurrent writers. Both are recorded in `HANDOVER.md`; neither has a step.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| **An aggregate question is answered from 8 passages and looks right** | The sharpest risk in the project. Decision 5.6 makes it visible via `coverage`; Step 8 makes it answerable. **It is not fully solved by this step and must not be described as if it were** |
| The golden set is written to make the current system look good | Largely removed by 5.7: the labels are **lawyers' annotations**, not questions invented by whoever built the retriever. What remains hand-made — the paraphrase queries — is written **before** the chunker and expected to fail |
| CUAD's licence is not what a search result said | Same rule as everything else: somebody opens it before a single PDF is committed. Recorded in `docs/licences.md` as unverified until then |
| **Docling's MIT does not cover the models it downloads** | The library is verified; the runtime models are not. A 4.2 gate lists them and reads their terms. An OCR engine is the likely surprise |
| Retrieval measures well on contracts and badly on the client's actual documents | Stated in 5.7 rather than hidden. The corpus proves the machinery and the metrics; only the client's own documents prove the fit, and they cannot live here |
| Adopting a library means adopting its dependency tree | It goes behind one producer with our interface on both sides. If it has to be replaced, the blast radius is one file and a version bump |
| Chunk-level retrieval is worse than document-level for some queries | Expected. 4.4's gate records both rather than asserting an improvement |
| The chunker is not deterministic | Gated in 4.3 explicitly, because every citation depends on it |
| `/api/v1/*` frozen too early | `found_by`, `coverage` and the absent score are where later steps will push. All three were chosen for that. A field may be added |
| The extra models do not fit in VRAM | Section 7 is arithmetic, and `docs/models.md` records a projection of this kind missing by 25%. Measured before promised |
| Scope creeps into building the features rather than the seams | Every sub-step's gate is about a seam. The word "vision" appears in no gate in section 8 |

## 11. Effort

**Five to seven sessions for Step 4 as signed off.** The narrow first draft said four to
five; the seams added 4.2 and 4.7 and `coverage` in 4.6, taking it to six to eight; **the
5.7 amendment took roughly a session back off.**

**4.1 is no longer the longest sub-step, and that is entirely down to adopting CUAD.** The
first draft had it authoring 40–60 documents and inventing 25–30 questions with their
answers. What is left is subsampling a dataset, five hand-made format files, and
*constructing* queries against annotations that already exist. It is still the sub-step that
decides whether anything after it can be believed — it just no longer costs the most.

**The longest is now 4.2**, because Docling changes what the ingestion pipeline reads and the
licence work on its runtime models has to happen before it ships.

The wider programme in section 9 is realistically **20–30 sessions**. That is the honest
number for what has been described, and the reason for doing it as seams: each of those steps
is then additive, gated, and independently rollback-able, rather than a rewrite of the last.

## 12. Rollback

Every sub-step is additive. `derived/<tenant>/chunks/` deletes and rebuilds like everything
else under `derived/`; the `chunks` FTS5 table drops without touching `documents`;
`/api/v1/*` unmounts without affecting `/v1` or `/api/*`; `models.toml` absent means the one
model behaves as it does today; the `files` source is today's code path unchanged. The local
agent's `search_files` is untouched throughout, so the offline path keeps working even if the
whole plane is withdrawn.
