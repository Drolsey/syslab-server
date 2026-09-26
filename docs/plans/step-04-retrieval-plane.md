# Step 4: The Retrieval Plane, and the Seams Everything Else Plugs Into

Status: **COMPLETE. 4.0 through 4.8 are done — the plane answers, and the seams are all in place.**
**Rewritten 11 September 2026**,
after the requirements turned out to be wider than the first draft assumed. **All seven
decisions signed off the same day**, 5.1 to 5.6 as recommended and 5.7 amended by Amro — see
below, because the amendment is the best thing that happened to this plan.

**The number moved, as of 12 September: overall MRR 0.576 → 0.768.** The 4.1 baseline was
whole-document keyword search; 4.4 indexed the passages and retrieval got substantially
better at the thing 4.1 found it was worst at. **4.1's standing prediction was right and by
more than it claimed** — `clause` went **0.210 → 0.686 MRR**, Recall@10 **0.571 → 1.000**,
and **every one of the 42 queries now finds its contract in the top ten**, where
document-level search missed eleven of them and six of those eleven were not paraphrases.
`scripts/check_retrieval.py` is the gate and it prints both rows.

**4.5 landed the same day and changed nothing, which was the requirement.** `retrieve.fuse()`
is RRF at k = 60 over the registered retrievers; one is registered, and fusing one ranked
list is that list in that order — asserted on ~2,100 chunk positions, not on the metrics, and
the gate was broken on purpose to watch it fail.

**4.6 shipped the same day and the plane now answers**: `POST /api/v1/retrieve` returns
passages with citations, a respected token budget, and a `coverage` block that says "2 of 14"
out loud. `/api/v1` is frozen. It was a mapping onto the wire, as intended — the one design
change it forced was pushing the source filter **into** the retriever seam, because filtering
afterwards would have made `k` and `matched` both lie.

**4.7 and 4.8 shipped 14 September, and Step 4 is now complete.** `models.toml` declares five
roles, one filled, and an empty role is *unavailable* and never a silent fallback — asserted by
asking for `embed` and getting a refusal with a reason, the same rule as `current_tenant()`
raising rather than defaulting. `app/llm.py` was deliberately left untouched: the registry
documents today's deployment rather than replacing the config path that already works, because
nothing yet needs two chat configurations to agree. And `GET /api/v1/documents`,
`GET /api/v1/documents/{name}`, `POST /api/v1/ingest/{name}` round out the plane as thin
tenant-scoped wrappers over what Step 2.3 already built — genuinely thin, since `require_tenant`
already sets the tenant before any of them run. `/api/v1` is frozen at four routes.

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
| **Source** | **Yes — 4.2 built it** | Where a tenant's material comes from | Customer SQL, Google Docs, Drive, cloud storage |
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

> **AMENDED AT 4.3, 11 September 2026. The tokenizer is never asked.** This sentence could
> not stand beside 4.3's own gate. A boundary decided by a tokenizer that is *sometimes*
> reachable is a boundary that depends on whether the GPU box was up when the document was
> ingested — exactly the Tuesday the gate was written to forbid. The 3.5-character estimate
> is used **always**, expressed as integer arithmetic (seven characters to two tokens) so
> that not even a float can vary. A real tokenizer may inform the *constants* later; it may
> never be asked at chunk time.
>
> `llm.estimate_prompt_tokens` turned out to be the wrong function too — it serialises a
> *message list* to JSON and adds 256 tokens of chat-template overhead, so it would have
> reported every passage as 256 tokens larger than it is. The prose estimator lives in
> `app/chunks.py` with the reason for the different ratio written beside it: `llm`'s 3
> characters per token is deliberately pessimistic for JSON payloads, and being pessimistic
> here would simply make every chunk 15% shorter than the size that was benchmarked.

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

**DONE, 11 September 2026.** 52 CUAD contracts (~16 MB) with their reference text, a
five-file format pack, 42 verified queries, 4 aggregate questions,
`scripts/check_retrieval.py`, and `tests/test_corpus.py` — 14 tests that hash every contract,
because the corpus is the ruler and a ruler nobody checks drifts. Suite 471 → 485.

**The baseline, `tests/fixtures/corpus/baseline.json`:**

| | queries | MRR | R@1 | R@5 | R@10 |
|---|---|---|---|---|---|
| overall | 42 | 0.576 | 0.458 | 0.611 | 0.685 |
| exact (rare strings) | 20 | **0.938** | 0.900 | 1.000 | 1.000 |
| clause (quoted passages) | 14 | **0.210** | 0.071 | 0.357 | 0.571 |
| paraphrase | 8 | 0.312 | **0.028** | 0.083 | 0.097 |

**And the finding that makes the baseline worth having already: `app/search.py` cannot do
phrase search.** `_terms()` splits a query into words and quotes each one *individually*, so
a 14-word quotation from a contract becomes nine unrelated tokens joined by AND. Every one of
"This / Agreement / shall / be / binding / Parties / as / date / hereof" appears somewhere in
nearly every commercial contract, so ten of them match and the contract the sentence was
literally copied from does not reach the top six. **Quoting a passage you are holding is the
single most natural thing a user does, and it is what today's retrieval is worst at.**

That is not a bug to fix in 4.1 and `app/search.py` was not touched. It is a **prediction for
4.4**: ANDing nine common legal words inside a 512-token chunk is far more selective than
inside a fifty-page contract, so chunk-level retrieval should move `clause` substantially.
If it does not, something is wrong with the chunker rather than with the theory.

**Two methodological notes recorded with the numbers, because they change how to read them.**
MRR is **flattered on multi-relevant queries**: the paraphrase set scores 0.312 MRR while
Recall@1 is 0.028, because a query with eight right answers can hit one by luck. For
paraphrase, **Recall is the honest metric and MRR is not**. And the gate's first run reported
**0.000 for everything**, which looked like a finding and was a bug — it read the hits from
`found["documents"]` when `search()` returns them under `results`. Lesson 5 in `HANDOVER.md`,
paid for twice now. The gate now says loudly when queries raised, rather than averaging the
silence.

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

**DONE, 11 September 2026, in two parts.**

*Part one* replaced the two hardcoded branches in `producers.extract` with `app/parse.py`'s
suffix → backend table, adopted Docling's **backends** rather than its `DocumentConverter`
(35 packages and no torch, against 85 and a multi-gigabyte GPU stack), bumped `text` to
version 2, and separated `ok` / `empty` / `unreadable` / `unsupported` so that a corrupt file
and a scan stopped producing the same answer.

*Part two* built the **source seam** (`app/sources.py`) — `files` moved and not rewritten,
`git diff` on the consumers empty — and grew `check_ingest` from 14 checks to **31**.

**And the formats section immediately found that two of part one's rows were broken.**
`.pptx` and `.md` were in the table with no library behind them, and the reason nothing had
noticed is worth the space: **docling-slim imports a format's reader when the file is read,
not when the backend module is imported.** So `docling.backend.mspowerpoint_backend` imports
perfectly on a machine with no `python-pptx`, `require_readers()` passed, and the
`ImportError` arrived from inside `convert()` — where it landed in `except Exception` and was
written to the manifest as **`unreadable`**, against a document that was perfectly fine.

That is the third time this project has paid for *a missing library reported as a broken
document*. It is now structural rather than remembered: `Backend.needs` names the package
each row actually defers to, `require_readers()` checks it, `parse._call` converts a
call-time `ImportError` into `ProducerUnavailable`, and the gate reads one file of **every**
suffix in the table looking for a sentinel *inside* the extracted text. Asserting that
ingestion "succeeded" is what let it through — a producer that writes an empty artifact
succeeds.

**PyMuPDF: not retired, and the exposure is much smaller.** `app/search.py` no longer reads a
PDF at all, so `docs/licences.md`'s *"it is the PDF reader for the entire search path"* is no
longer true. One use remains — `app/tools.py:213`, rendering page images for `read_pdf` — so
the AGPL-3.0 question is now one module and one tool, and the pypdfium2 swap that file
specifies is a small gated change rather than a rewrite. Recorded with its cost: the project
currently ships two PDF libraries. Retrieval is **unchanged against the 4.1 baseline**, MRR
0.576 to three decimals, which is the right outcome for a parser swap. Suite 485 → 513.

**4.3 The chunk producer.** `producers.CHUNKS`, writing `chunks.json`. Gate: offsets resolve
back to the real text; a version bump re-chunks; deleting `derived/` and rebuilding gives
**byte-identical** chunks. Determinism is the property worth gating — a chunker that splits
differently on Tuesday invalidates every citation ever issued.

> **DONE, 11 September 2026.** All three properties gated in `scripts/check_ingest.py`
> (31 → **37 checks**), plus `tests/test_chunks.py` (30 tests, suite 513 → **543**). The
> splitting rule itself is `app/chunks.py` and it never touches the filesystem: it takes a
> string and returns passages, the way `app/parse.py` takes a path and returns an outcome.
>
> **It needed a change to the ingestion contract, and that was not foreseen here.** Decision
> 5.3 says the chunker consumes the text artifact rather than the source — and
> `ingest.producers_for()` sorted producers by name, where `chunks` sorts before `text`. On a
> fresh upload the chunker would have run first and found nothing. `Producer` gained
> `depends_on`, which buys three things at once: the **run order**, the **staleness** (a text
> re-extraction must re-chunk, and no column in the manifest would have noticed — it records
> a producer's *own* version and nothing about the artifact it read), and the **hold-back**
> (a damaged file produced two failed rows, the second of which blamed the chunker for the
> extractor's problem). A dependency circle is refused by name rather than arriving as a
> `RecursionError` on somebody's first upload.
>
> **Section 5.4's tokenizer sentence was amended**, in the open, above.
>
> **What the break-verification found that reading did not.** Eight deliberate breaks of the
> code, to confirm each test fails when its property is violated. Two passed at first, and
> both were worth the pass: `chunks.SEPARATORS` ended in an empty string that the splitter
> skips — a comment pretending to be code, sitting on the line somebody would edit when they
> went looking for the floor — and the ordinal-holes test used a run of newlines shorter than
> the target size, so it asserted on a case it never built and passed against broken code.
>
> **Retrieval is unchanged against the 4.1 baseline**, MRR 0.576 to three decimals, and that
> is the expected answer rather than a disappointment: 4.3 makes passages and 4.4 indexes
> them. If 4.4 does not move `clause`, the chunker is where to look.

**4.4 The chunk index and the keyword retriever.** The `chunks` FTS5 table, populated by
`intake` as a second consumer beside the document index. Gate: `check_retrieval` shows
passage-level numbers against 4.1's baseline. **They may be worse on some queries** — a
512-token chunk has less context than a whole document for BM25 to score — and that is
information, not a failure. Record it.

> **DONE, 12 September 2026.** `app/passages.py` — the `chunks` table inside the tenant's
> existing `index/<tenant>.sqlite3`, `search_passages()`, and the `Keyword` retriever. Plus
> `app/retrieve.py`, the seam, one sub-step early. Suite 543 → **566**;
> `check_isolation` 54 → **58**.
>
> | | MRR | R@1 | R@3 | R@5 | R@10 |
> |---|---|---|---|---|---|
> | documents (4.1 baseline) | 0.576 | 0.458 | 0.563 | 0.611 | 0.685 |
> | **passages (4.4)** | **0.768** | **0.653** | **0.704** | **0.772** | **0.871** |
> | clause, documents | 0.210 | 0.071 | 0.286 | 0.357 | 0.571 |
> | **clause, passages** | **0.686** | **0.571** | **0.714** | **0.786** | **1.000** |
> | paraphrase, documents | 0.312 | 0.028 | 0.083 | 0.083 | 0.097 |
> | **paraphrase, passages** | **0.430** | **0.056** | 0.071 | **0.176** | **0.322** |
>
> **They were not worse anywhere, and the gate was written expecting they might be.** The
> prediction held: ANDing nine common legal words inside a 512-token chunk is far more
> selective than inside a fifty-page contract. `exact` was already 0.938 and barely moved to
> 0.960, which is the right shape — a rare string was never the problem. Paraphrase moved and
> is **still the worst kind by a wide margin**, which is the point of having it in the golden
> set: Step 5 is what those queries are for, and they are now a measured gap rather than an
> asserted one.
>
> **The document row did not move, to three decimals**, and the gate prints two rows per
> metric so that stays visible. 4.4 adds a second index and changes nothing about the first;
> drift in that row is a regression to explain before the passage row means anything.
>
> **One judgement call, written down where it is made.** The baseline's Recall@10 means "the
> right document was among ten **documents**", and passages of one document cluster — the ten
> best passages of a query are **3.3 distinct documents on average**, measured. Folding ten
> passages down would have compared ten documents against three and called the difference a
> regression. So each query asks for fifty passages, folded to documents by first appearance
> (`PASSAGE_DEPTH`). Rank 1 and MRR are unaffected by the depth and are the honest headline;
> R@10 is the one the deeper pool helps.
>
> **A gap this opened and did not close, named here because `app/passages.py` points at
> this paragraph.** `search.SEARCHABLE` is three suffixes — a Step 2 legacy of what the
> *document* index was willing to read — while the chunk index covers everything that
> produces chunks, which is all ten formats in `app/parse.py`'s table. **So a `.docx` has
> passages and is not in the document index.** Widening the document index is a two-line
> change and it is deliberately not made here: it would move the 4.1 baseline, which is the
> one number every sub-step of Step 4 is measured against. It is a change to make on its own,
> with its own before-and-after.
>
> **And it found that a `chunk_id` is not globally unique**, before 4.6 could assume it was.
> It is `source#ordinal` and there is one index per tenant, so two tenants who both hold a
> `contract.pdf` both hold a `contract.pdf#0`. Resolving it gives each of them their own
> passage and never the other's — the stronger property, and now the gated one — but a
> `chunk_id` in a log line or a cache key means nothing without the tenant beside it.
>
> **Two gate bugs found, and both were checks that had never failed.** Detailed in
> `CHANGELOG.md`: a check in `check_gateway_isolation.py` whose regex contained a literal
> backspace character, so it matched nothing and printed PASS; and a check in
> `check_isolation.py` that read `index_path()` to prove what `connect()` had opened, so it
> carried on passing with tenant scoping deliberately broken. The gate now proves its own
> patterns are alive before it trusts them, and asks SQLite via `passages.opened_path()`
> rather than asking the config. **A check nothing has ever seen fail is a claim, not a
> check** — 4.3's lesson about tests, arriving at the gates.

**4.5 Fusion.** `app/retrieve.py`, RRF, one retriever registered. Gate: fusing one list
returns that list in that order, asserted directly; `check_retrieval`'s numbers are
**identical** to 4.4's, because nothing has changed yet. An RRF that moves a single list is
broken, and this is the only moment it is cheap to notice.

> **DONE, 12 September 2026.** `retrieve.fuse()` and `retrieve.search()`, k = 60, per-retriever
> weight defaulting to 1.0. `tests/test_retrieve.py` — 25 tests, suite 566 → **591** — and
> `check_retrieval` grows a fusion section that **asserts nothing happened**: all 42 queries
> come back in exactly the order the retriever gave, every metric identical, MRR 0.768
> unmoved against the committed 4.4 row.
>
> **The gate asserts on the chunk_ids position by position, not on the metrics**, and the
> difference matters more than it looks: 42 queries at a depth of 50 is ~2,100 positions that
> have to agree exactly, where **equal MRR is a far weaker claim** — a fusion that swapped two
> passages of the same contract, or two passages neither of which is relevant, scores
> identically and is just as broken. **And the gate was itself broken on purpose**, `fuse`
> made to sort by `chunk_id`: it failed 33 of 42 on order, all five metrics, and returned 1.
> That check is not a claim.
>
> **The section says out loud when it is supposed to start failing.** The day Step 5 registers
> a second retriever, the order assertion **must** break — a fusion of two lists that still
> returns the first one unchanged means the second one is not reaching it. A gate that would
> silently keep passing through the change it exists to observe is the inert-check problem
> from 4.4 wearing a different hat.
>
> **The arithmetic is exact, `Fraction` rather than float, and that is 4.3's reasoning
> reused.** Ranks summed as floats make the total depend on the order the terms were added,
> which is the registration order, which is which module imported first — so two
> mathematically tied passages would sort by whichever sum happened to round up, and the
> answer would move the day an unrelated import moved. Ties are then real ties, broken by the
> best single rank and finally by `chunk_id`, so the order is total and nothing is left to
> chance. There is no score on `Fused` for a reason `Hit` does not have: **1/61 is the best
> a passage can score with one retriever and 2/61 with two**, so the same passage, equally
> well retrieved, would double the day Step 5 ships and anybody who had thresholded on it
> would change behaviour silently.
>
> **Four refusals, each because the silent version is worse.** A retriever listing a passage
> twice is refused rather than deduplicated (a repeat counts twice and lands it at the top,
> and nothing in the output looks wrong); two hits at one rank; `k < 1` (at k = 0 one
> retriever's first place outranks every other combined); and a **negative weight**, which is
> incoherent rather than merely odd — absence contributes zero, so a negative weight ranks a
> passage below one nothing found at all. Weight 0 is allowed, contributes nothing, and its
> hits sort last rather than vanishing: to take a retriever out of an answer, do not ask it.
>
> **Two things decided here that section 6 did not cover.** **Each retriever is asked for the
> full `limit`, not for `limit/n`** — two retrievers asked for four, agreeing on nothing, give
> eight passages fused from two lists of four, and a passage ranked fifth by both, which is a
> strong signal, is invisible. And **a failed retriever is named in `failed` rather than
> swallowed**, because a fused list missing the vector side is a worse answer that looks
> exactly like a normal one; when *every* retriever fails it raises, since an empty list
> already means "nothing matched". 4.6 puts both on the wire.
>
> **One test was wrong and passed anyway, found by breaking rather than reading.** The tie
> test used two plausible rank patterns whose scores differed in the fourth decimal, asserted
> the ordering the scores already gave, and would have passed with the tiebreak deleted
> entirely. A tie has to be **constructed** — here from a weight exact in binary, so two
> passages score exactly 1/122 — not hoped for. Twelve deliberate breaks, twelve caught, and
> a thirteenth attempt that turned out not to be a bug at all: a penalty subtracted equally
> from every passage changes no ordering, and since `Fused` carries no score, nothing
> observable had changed.

**4.6 `POST /api/v1/retrieve`.** The contract in section 6, the token budget, the filter, and
`coverage`. Gate: the budget is respected and `truncated` is honest; **`coverage` is correct
and provably so** — a query matching 31 passages and returning 8 says so; a request for
another tenant's source filters to nothing rather than erroring informatively.

> **DONE, 12 September 2026.** The route is in `app/plane.py` on the router 4.0 mounted, and
> it is a **mapping onto the wire**: `retrieve.search()` ranks, `passages.coverage()` counts,
> and what is left here is the shape, the budget and saying out loud what the answer is not.
> `tests/test_plane_retrieve.py` — 34 tests, suite 593 → **627**. `/api/v1` is frozen at
> `docs/api/retrieval-v1.released.json`. All three gated properties hold, and **21 deliberate
> breaks were all caught**.
>
> **The budget is strict, and "strict" needed deciding rather than assuming.** A 600-token
> passage does not go into a 500-token budget — handing it over anyway would blow the budget
> of a caller who asked precisely so that would not happen, which makes the field a
> decoration. It **stops rather than skipping ahead** to a smaller passage further down:
> skipping would fill the budget more completely and quietly return a worse-ranked set as
> though it were the best one. And the one case where `returned` is 0 while `matched` is not
> — a first passage larger than the whole budget — says so in `what_this_means` rather than
> looking like an empty result.
>
> **`coverage` needed a function of its own, and that is the interesting find.** The plane
> asks the *seam* for its ranking, and the seam carries ranks and nothing else, so a coverage
> count assembled from what came back **would have equalled `k` every time and agreed with
> itself every time** — which is exactly what an invented number looks like. So
> `passages.coverage()` asks the index: two `COUNT(*)`s against a match it has already done.
> The gate is that `matched` is **not** the returned count, which is the assertion that makes
> the block worth having.
>
> **And `matched` will need re-deciding at Step 5, which is written down rather than left to
> be discovered.** It is the keyword index's count — how many passages the FTS5 expression
> matched. A vector retriever matches *everything* at some distance, so the word stops having
> an obvious meaning the day the second retriever lands. `searched` is unaffected: it is how
> many passages were in scope, which is true regardless of who searches.
>
> **The source filter went into the seam, and that changed `Retriever.search`.** It takes
> `sources` now. Filtering after retrieval was the alternative and it makes two numbers lie
> at once: `k` comes back short with no explanation, and `matched` counts passages the caller
> excluded — so `coverage` would report a census of the wrong corpus, which is the one field
> decision 5.6 rests on. Changing the seam one sub-step after shipping it is awkward and it
> is still right: better now than after a second retriever exists. **An empty list is
> nothing, not everything**, because of the two surprises "nothing came back" costs a retry
> while "everything came back" spends the caller's budget on material they excluded.
>
> **Another tenant's source filters to nothing and is not an error**, and there is no code for
> that case — the filter runs inside the asking tenant's own index, so a foreign filename
> matches no row. "There is no code for it" is an argument and not a check, so it is gated:
> 200, zero passages, `searched: 0`, and the other tenant's words absent from the whole
> response body.
>
> **One honest limit of the freeze, stated where somebody would otherwise trust it.** The
> frozen file covers the **request**. This endpoint returns a `dict`, so FastAPI emits an open
> object for its 200, and declaring a response model to close it would fight the freeze's own
> rule that a field may be added, since pydantic strips what a model does not name. Every
> load-bearing field in section 6 — `found_by`, `coverage`, `truncated`, `what_this_means` —
> lives in the **response**, so the response shape is pinned by exact-key-set assertions in
> `tests/test_plane_retrieve.py`, and `check_api_compat.py`'s own header says so. A freeze
> that looked like it covered them and did not would be worse than no freeze.
>
> **Four gaps in the tests, found by breaking rather than reading, and all four were the same
> mistake**: a test whose fixture never built the case it claimed to assert on. The budget
> tests used a budget nothing hit, so `>` and `>=` were indistinguishable; `tokens_returned`
> was asserted where nothing was ever cut, so "what shipped" and "what was considered" were
> the same number; `coverage.returned` was asserted where it happened to equal `k`; and every
> coverage test used a query whose AND pass matched, so **the AND-then-OR fallback was never
> exercised** — a coverage figure taken from the AND pass would have reported `matched: 0`
> above a response carrying real passages. That is the 4.3 lesson for the third sub-step
> running.

**4.7 The model role registry.** `models.toml` with five roles, one filled. Gate: an empty
role is *unavailable* and never a silent fallback, asserted by asking for `embed` and getting
a refusal with a reason; `check_api_compat` freezes `/api/v1/*`.

> **DONE, 14 September 2026.** `models.toml` (project root) and `app/models.py`.
> `model_for(role)` raises `RoleUnavailable` for an empty or unknown role — mirroring
> `app.context.current_tenant()` / `NoTenantError` deliberately rather than inventing a new
> shape for the same rule. `_ROLES` loads once at import, a module-level dict in the style of
> `config.py`'s own constants, and tests monkeypatch it exactly as `tests/test_plane_retrieve.py`
> already monkeypatches `config.RETRIEVAL_TOKENS`. `tests/test_models.py`, 8 tests. Suite
> 646 → **654**.
>
> **Returns a plain `dict`, not a per-role dataclass, deliberately.** `chat`, `embed`, `vision`,
> `stt` and `tts` will need different fields as each is filled — a context window is not an
> embedding dimension is not a sample rate — and there is nothing to validate yet for four of
> the five roles. A schema is for when a second role is actually filled, not before.
>
> **`app/llm.py` is untouched, and that was a decision rather than an oversight.** `LLM_BASE_URL`
> / `LLM_MODEL` still come from `.env` via `app/config.py` exactly as before this sub-step;
> `models.toml`'s `chat` entry documents that same deployment rather than becoming a second
> source of truth for it. Nothing today calls `model_for("chat")` in production code — the
> seam exists for the day a producer needs to ask whether a role is available, the same
> discipline 4.5's fusion used to ship something that provably does nothing yet.

**4.8 The rest of the plane.** `GET /api/v1/documents`, `GET /api/v1/documents/{name}`,
`POST /api/v1/ingest/{name}` — thin tenant-scoped wrappers over what Step 2.3 already built.

> **DONE, 14 September 2026.** All three routes in `app/plane.py`, on the router 4.0 mounted,
> each gated by the same `require_tenant` dependency as `/retrieve`. `tests/test_plane_documents.py`,
> 19 tests. Suite 627 → **646**.
>
> **Genuinely thin, because `require_tenant` already did the only hard part.** It calls
> `context.set_tenant()` before any handler runs, and `tools.list_files()`, `ingest.status()`
> and `config.resolve_in_data_dir()` already resolve everything through `config.data_dir()` →
> `current_tenant()`. There was no tenant-scoping logic left to write in this sub-step — only
> the mapping onto the wire, the same shape 4.6 took for `/retrieve`.
>
> **One deliberate departure from a straight passthrough, found by asking what each field
> means to an external caller rather than an internal one.** `tools.list_files()` also
> reports `folder`, an absolute path on this server's own disk. That is fine for `/api/files`,
> same-process admin surface; `/api/v1` is the frozen surface a customer's own website relies
> on, and handing it this host's filesystem layout is the same class of mistake this module's
> own header names for tenant ids. `GET /api/v1/documents` drops `folder` and renames `files`
> to `documents`. The other two routes return Step 2.3's shapes unmodified: everything in
> `ingest.status()` and `intake.arrived()`'s output already describes the calling tenant's own
> document, so there is no host-path or cross-tenant leak to filter out of them.
>
> **`docs/api/retrieval-v1.released.json` re-frozen additively**, 1 route to 4,
> `check_api_compat.py` reporting the three additions and no breaking change on either frozen
> contract.
>
> **Step 4 is complete.** Source, producer, retriever and model-role seams all exist and are
> gated. What attaches to them next — a second retriever, a filled `embed` role, a source
> beyond `files` — is Step 5 or later, section 9, and none of it requires touching what is
> built here.

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
