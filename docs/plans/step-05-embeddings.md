# Step 5: Embeddings, and Turning On the Fusion That Provably Does Nothing

Status: **PLANNED. Drafted 16 September 2026.** 5.0-5.3 are DONE (18 September): the model is
chosen, the producer and retriever are built and tested. **5.2's freshness design is SIGNED
OFF, 18 September** — reviewed against the running code (not re-derived), confirmed by
re-running `tests/test_vectors.py` on demand: **14 passed, 0 failed**, covering per-chunk
content changes, embed-config changes, the derived `EMBEDDINGS.version`, stale-row removal,
and the unchanged-document no-op. 5.4's deployment architecture (§ "3a") was reviewed and
**approved** the same day; `models.toml` and `docker-compose.yml` are applied, plus the
`EmbedError`/`EmbedUnavailable` distinction the review's own §3 found and fixed before
deployment (`tests/test_embed.py`, 9 more tests). Full suite: **677 passed, 1 skipped.**
Neither the producer nor the retriever is registered into the running app yet — that is 5.4's
remaining `retrieve.register`/`ingest.register` step, blocked on the embed container actually
being deployed on the box. Written to the standard of Steps 0-4: decisions named with what
loses, sub-steps each with its own gate, risks, rollback.

**This step has one job stated in Step 4's own plan**: `retrieve.fuse()` is Reciprocal Rank
Fusion over a registry that holds exactly one retriever, and fusing a single ranked list is
that list in the same order — asserted directly in `tests/test_retrieve.py` on purpose,
because an RRF that moves a single list is broken and 4.5 was the only moment it was cheap
to notice. **Step 5 is a second retriever landing in that registry.** Everything else below
is what has to be true first for that to be a real second opinion and not a second copy of
the first.

The measurement this step exists to move is already built and already has a number to beat:
`scripts/check_retrieval.py` against `tests/fixtures/corpus/golden.json` scored keyword
search over passages at **overall MRR 0.768** (12 September), up from 0.576 for whole-document
search. The golden set's own queries were written expecting some of them to fail — paraphrase
queries where the right words never appear in the contract, only the right meaning does. That
is the failure mode keyword search cannot fix by construction, and it is the one Step 5 is
for. If the fused MRR does not clear 0.768, or clears it only on queries keyword search
already won, this step has not paid for itself.

---

## 1. What this step is

**One additional retriever — vector search over the passages Step 4 already chunked and
offset — registered into the seam Step 4 built, measured against the same golden set Step 4
already has, with a decision recorded either way.** "Either way" is not a hedge: a fused MRR
that does not beat 0.768 is a real result, and shipping a second retriever that adds latency
and VRAM for a number that does not move is the wrong call, made visible instead of made by
default.

## 2. What this step is not

- **Not a rewrite of `retrieve.py`.** `Hit`, `Retriever`, `register()` and `fuse()` are
  built and gated; a vector retriever is a new module that imports them, the same relationship
  `app/passages.py`'s `Keyword` class already has to them (`app/passages.py:493`).
- **Not vision, STT or TTS.** Those are Steps 6 and later, named in `docs/architecture.md`
  §7 and budgeted in `docs/models.md`'s VRAM tables. Filling `[roles.embed]` in `models.toml`
  is this step's job; `[roles.vision]`, `[roles.stt]`, `[roles.tts]` stay empty.
- **Not a new chunking strategy.** `app/chunks.py`'s chunks, offsets and token counts from
  Step 4.3 are the unit indexed. If they turn out to be the wrong size for embeddings —
  section 5.5 below names the test — that is a finding this step reports, not a rewrite it
  does unasked.
- **Not ANN, and not pretending otherwise.** `docs/licences.md` already flags the leading
  candidate, `sqlite-vec`, as **brute-force rather than approximate nearest neighbour**, "which
  is fine at this project's scale and is a fact to design around rather than discover." At a
  few thousand chunks per tenant, brute-force cosine similarity is a table scan, not an
  algorithm problem. This step does not go looking for ANN it does not need yet.

---

## 3. Five decisions

### 5.1 Which embedding model, and where does it run

**DECIDED, 18 September 2026: `Qwen/Qwen3-Embedding-0.6B`.** Benchmarked, the way the chat
model was — `docs/models.md`'s own method is the template: `scripts/bench_models.py` measured
three chat candidates on VRAM and throughput before anything was chosen, and the decision
section named what lost and why. Two candidates measured here the same way — Qwen3-Embedding-0.6B
against `BAAI/bge-base-en-v1.5`, the plan's own "small BGE-class" suggestion — on latency,
throughput, dimension, real VRAM cost, licence, AND retrieval quality against the golden set
(§5.5's metric, run early rather than deferred, specifically so the choice is not made on
latency alone). Full numbers, the comparison table, and the reasoning: `docs/models.md` §
"Second candidate: BAAI/bge-base-en-v1.5" and § "Decision: Qwen3-Embedding-0.6B".

The one-paragraph version: overall MRR on the golden set is a statistical tie between the two
(0.654 Qwen, 0.657 BGE, on 42 queries), but splits in opposite directions underneath — BGE
wins clearly on clause queries, Qwen wins clearly on paraphrase queries, and paraphrase is the
specific failure mode this whole step exists to fix (§0's opening paragraph: "the failure mode
keyword search cannot fix by construction"). Combined with Qwen's already-verified licence
against BGE's still-unverified one, Qwen wins the comparison that actually matters for this
step's purpose. BGE's clause-query strength is recorded as a real limitation of the choice, not
hidden — see `docs/models.md` for the number and a candidate mitigation (`Qwen3-Reranker-0.6B`,
already a licence row, unused).

Whether vLLM v0.28.0 itself exposes an embedding endpoint without a second container was also
checked directly rather than assumed: **no** — `POST /v1/embeddings` against the running chat
container returns a live `HTTP 404`. A vLLM server serves one `--runner` per process; the
embed role needs its own container, `[roles.embed]`'s deployment shape for 5.2 onward.

**What is decided:** embeddings run through the `embed` role in `models.toml`
(`app/models.py:model_for("embed")`), the same seam Step 4.7 declared and left empty on
purpose. `model_for()` raising `RoleUnavailable` for an empty role is the mechanism that
keeps this honest — a producer that calls it before the role is filled fails loudly instead
of silently skipping embedding, the same rule `context.current_tenant()` enforces and for the
same reason.

**What loses:** running the embedding model on CPU by default and calling that "good enough
without measuring." The 9950X has cores to spare, per Section 13's own reasoning for speech —
but that reasoning was earned by a measurement, not assumed, and embeddings sit on the write
path (every ingested chunk) as well as the read path (every query), where speech does not.

### 5.2 Which vector store

**`sqlite-vec` — licence VERIFIED 18 September 2026.** `docs/licences.md` now records a
person's own reading of `LICENSE-APACHE` and `LICENSE-MIT` in the sqlite-vec repository: dual
Apache-2.0/MIT, both standard text, both copyright Alex Garcia 2024, no additional terms. That
was the one blocker 5.0 named, the same discipline that held for Docling in Step 4.2, and it is
now cleared — this step can build against sqlite-vec.

Chosen over standing up a separate vector database (Qdrant, pgvector, etc.)
for the same reason SQLite already won for the keyword and passage indexes: one file per
tenant, disposable by construction, no second service to run, back up or isolate between
tenants. `sqlite-vec` is an extension of the same SQLite file `search.py` and `passages.py`
already open — a third table (`embeddings`) in the file the tenant's index already is, not a
fourth thing to delete when a tenant leaves.

**What loses:** a hosted vector database. It would be the right call at a scale where
brute-force cosine similarity stops being a table scan — nothing here is near that scale, and
Section 6 of the architecture plan already committed to "storage as a config change, not a
migration" for exactly this kind of decision. If `sqlite-vec`'s pre-v1 breaking-changes
warning turns into an actual break, the fallback is a `BLOB` column of raw float bytes and
cosine similarity computed in Python — slower, but with no new dependency and no licence to
verify, and a real option because nothing here is measured in tens of milliseconds yet.

### 5.3 What a vector retriever returns, and how it fits the existing seam

**Decided by the seam already built, not by this step.** `retrieve.Retriever` is a `name` and
a `search(query, limit, sources)` returning `list[Hit]`, ranks 1-based, no scores — Step 4.4's
decision that a retriever "cannot leak its scale into the fusion." A vector retriever
computes cosine similarities, a number that means something on its own, and must still hand
back ranks and nothing else: the type enforces this rather than a reviewer having to catch a
`score` field creeping back in.

Concretely: a new module, `app/vectors.py`, exporting a `Vector` class analogous to
`app/passages.py`'s `Keyword` (`app/passages.py:493`) — the same shape as the existing
keyword-over-passages retriever, so the two are read side by side rather than one being the
odd one out. `retrieve.register(Vector())` at import, the same as every retriever and every
Step 2 producer registers itself.

**What loses:** computing a query embedding inside `retrieve.search()` or `app/plane.py`.
Either would make the retrieval seam know that embeddings exist, which is exactly the
coupling Step 4.4's seam was built to prevent — a vector retriever is a plugin, not a special
case the orchestrator knows about.

### 5.4 What triggers re-embedding, and what it costs on the write path

**Corrected 18 September 2026 — the mechanism this section originally named does not exist.**
The first draft said skipping an unchanged chunk "needs the same content hash Step 2.1 already
uses to decide whether a chunk changed at all, read once rather than re-derived here." There is
no such hash. `app/ingest.py:358-390`'s `_is_current()` is titled, in its own comment,
"Deliberately NOT a content hash" — size, mtime and producer version only, because hashing a
200 MB workbook on every upload to catch a change size and mtime already caught is a cost paid
every time for a case that is rare and not silent. That reasoning is sound for a whole source
file. It does not transfer to a 512-token chunk, and conflating the two was the error, not a
reason to avoid hashing altogether.

**Two different questions were tangled together in the first draft, and they need two
different answers.**

1. *Should the `embeddings` producer even be asked to look at this document right now?* — a
   per-document question, already answered by machinery Step 4.3 built:
   `Producer.depends_on` (`app/ingest.py:149-161`). Declaring `embeddings` with
   `depends_on=frozenset({"chunks"})` means `_must_run()`'s second clause
   (`app/ingest.py:289-290`, `elif producer.depends_on & stale: stale.add(...)`) marks it stale
   exactly when `chunks` reran — the same seam `chunks` already rides on `text`. Nothing new
   here; this is the mechanism, used the way it is already used.
2. *Given that the producer is running for this document, which of ITS chunks actually need a
   new vector?* — a per-chunk question `depends_on` cannot answer, because it operates at
   producer granularity and `chunks.split()` re-cuts a whole document's text in one pass
   (`app/chunks.py:242-283`). A single edit shifts `_merge()`'s running size total
   (`app/chunks.py:201-239`) for everything after it, so most `chunk_id`s downstream of an edit
   change anyway — but not all of them, and an edit near the end of a long document leaves most
   of it untouched. This is where a hash earns its keep, and it is nothing like the file-level
   case `_is_current()` was written to avoid: a chunk is capped at `TARGET_TOKENS` = 512
   (`app/chunks.py:75`), so hashing one is microseconds, not the cost of hashing a 200 MB
   workbook on every upload.

**A third question hides inside question 2, and missing it is worse than missing the first
two: does the vector still mean anything for the model about to compare it to a query?** A
chunk whose text has not changed at all still needs a new embedding if `[roles.embed]` now
names a different model — cosine similarity across two models' embedding spaces is not merely
worse, it is meaningless, and a text hash would never notice, because the text did not change.
`models.toml` is read once per process (`app/models.py:39-42`, "takes effect on the next
process start, not mid-request"), so nothing mid-request can catch this either; it has to be
caught by the producer machinery itself.

**Decided: two mechanisms, matched to the two things that must invalidate an embedding.**

- **Chunk content.** Each stored row carries a short hash of the chunk's own `text` (e.g. the
  first 16 hex characters of `sha256`) alongside its vector, keyed by `chunk_id`. On a run, a
  chunk is reused only when the CURRENT `chunks_of(source)` entry at that `chunk_id` hashes to
  the value already stored for it; a new `chunk_id`, or the same `chunk_id` with a different
  hash, gets a fresh embedding. Rows whose `chunk_id` no longer appears in the current chunk
  list are deleted, not left behind — the same "nothing orphaned" rule `forget_missing()`
  already enforces elsewhere (`app/ingest.py:736-784`, `app/passages.py:189-217`).
- **Embed configuration.** `EMBEDDINGS.version` (the `Producer.version` int,
  `app/ingest.py:145,178-179`) is not a hand-maintained constant here — it is derived at import
  from `model_for("embed")` (a short hash of the sorted config dict), so changing
  `[roles.embed]` changes the producer's version automatically on the next process start, with
  no line for a person to remember to touch. `_is_current()` compares `producer_version` by
  equality, not by order (`app/ingest.py:387`), so a version that moves in either direction on
  a config change — not only upward — still invalidates correctly; "a producer version… only
  goes up" (`app/ingest.py:179`) is a convention for hand-maintained versions, not a constraint
  this equality check enforces. This is what flips `_must_run()` for `embeddings` on EVERY
  document the next time anything ingests, which is what actually gets `run()` invoked at all —
  the chunk-hash check above never fires if `depends_on` and `producer_version` both say
  nothing changed, so this is not decoration on top of the hash; it is the only thing that
  makes the hash check reachable when the model itself is what moved. As a second guard inside
  a single run — belt on top of the version having already changed, not a replacement for it —
  each row also stores the config fingerprint that produced it, and a chunk is reused only when
  BOTH the content hash and the fingerprint match. That is what stops a version-derivation
  collision (astronomically unlikely with a real hash, but free to guard against anyway) from
  silently serving a stale vector.

**What loses:**

- **A hand-maintained `EMBEDDINGS.version` bump**, the same discipline already trusted for the
  `text` producer's 1→2 bump when Docling replaced PyMuPDF (Step 4.2). Rejected specifically
  here, not as a general objection to that pattern: an embed-role swap is exactly the kind of
  change `docs/models.md` already records happening more than once without the number attached
  to it turning out right the first time (the VRAM projection was wrong twice), and this step's
  own risk list already worries about the embed role changing something silently (§5, next
  section). Deriving the version removes one more place that requires a person to remember, for
  the cost of one small pure function.
- **A single tenant-wide "last embed config used" marker** instead of a fingerprint per row.
  Smaller on disk, but it adds a second read-then-branch step to every run (read the marker,
  decide whether to bypass the hash check entirely, then run the per-chunk loop) where a
  per-row fingerprint keeps the decision local to one row and one comparison. Storage is cheap
  here; a second code path that can disagree with the first is not.
- **Skipping the fingerprint and trusting the derived version alone.** Would work if `run()`
  could learn from `ingest.py` WHY it was invoked (config changed vs. this document's chunks
  changed) — it cannot; `Producer.run` takes only `(source, out_dir)` (`app/ingest.py:168`) —
  so a content-hash-only check would find every existing chunk's text unchanged after a pure
  config swap and skip recomputing all of them, which is the one failure this section exists to
  prevent.

**What is not decided:** batch size for embedding calls, and whether embedding runs inline
during ingest (as fast producers do today) or is deferred to the job queue Step 2.2 built for
slow work. Docling's OCR is the precedent for "slow, so it goes through jobs.py, not the
request." Whether embedding a chunk is fast enough to stay inline is an empirical question —
section 5 below names the sub-step that measures it before deciding, rather than assuming
either answer.

### 5.5 How this is measured, and what "done" means

**`scripts/check_retrieval.py`, unmodified in its metric, run with the vector retriever
registered.** The golden set at `tests/fixtures/corpus/golden.json` already includes
paraphrase queries "known to fail today" — written that way on purpose in Step 4.1, because a
golden set the current system passes completely cannot show this step an improvement. Three
outcomes, all of them a real answer:

1. **Fused MRR clears 0.768 on the paraphrase queries specifically**, not just on average —
   the case this step is for, and worth naming honestly if the overall number moves less than
   the paraphrase-only number, because that is fusion doing exactly its job: not hurting the
   queries keyword search already won.
2. **Fused MRR is flat.** The embedding model, chunk size, or both are wrong for this corpus,
   and the fix is in 5.1 or in `app/chunks.py`'s chunk size — not a reason to ship anyway.
3. **Fused MRR drops.** RRF should make this structurally hard (a chunk absent from one list
   contributes nothing, never a penalty), so a drop points at a bug in the vector retriever's
   ranking, not at fusion — the same diagnostic split Step 4.4's gate used when it "caught two
   real rows lying."

**Gate for this step being called done:** `check_retrieval.py`'s fused row beats the
passages-only row on the golden set, `retrieve.fuse()`'s existing single-retriever assertion
still passes unmodified (a real second retriever must not break the property proven when there
was only one), and `docs/models.md` carries a real measurement — not a projection — of what
the embedding role costs in VRAM and per-chunk latency.

---

## 3a. Deployment architecture for `[roles.embed]` (5.4)

**PROPOSED, 18 September 2026 — needs sign-off before `docker-compose.yml` or `models.toml`
changes. Nothing in this section has been applied.** Ten questions, in the order they were
asked, each with a recommendation, what it costs to be wrong, and what loses.

### 1. Where the service runs

**A second vLLM container, on the same box, on its own port.** No second GPU exists anywhere
in this project, and the box already carries this pattern for everything else additive —
Postgres, MinIO and the database-agent app all joined `docker-compose.yml` as new services on
the same box rather than new hardware. Port 8001, matching what `EMBED_BASE_URL`'s default
(`app/config.py`) and the Step 5.1 benchmark both already used.

**What loses:** CPU-only serving. Named as "CPU-viable" in §5.1's original candidate list, but
never benchmarked that way — every number in `docs/models.md`'s comparison is a GPU number,
and recommending CPU now would be estimating on the write path, the exact thing this project's
whole `docs/models.md` history argues against doing without measuring first.

### 2. How its endpoint is configured

**Already built, not a 5.4 decision:** `EMBED_BASE_URL` / `EMBED_MODEL` / `EMBED_TIMEOUT` in
`app/config.py`, added alongside the freshness work, mirroring `LLM_BASE_URL` exactly. The one
genuinely useful fact this surfaces: **because the app runs on the box itself (systemd, not a
container — `docker-compose.yml`'s own top comment), the production default
(`http://127.0.0.1:8001/v1`) is already correct for a container on port 8001, the same way
`LLM_BASE_URL`'s `127.0.0.1:8000` default already is.** Deploying to port 8001 needs no `.env`
change at all. `.env.example` is missing the documenting entry for these three variables —
harmless to add now (it documents code already merged, not a running service), proposed below
alongside the other file changes.

### 3. How the application detects service availability vs. failure

**Three layers, and only one of them has a real gap.**

- **The retriever, at query time — already correct, no change needed.** `Vector.search()`
  turns `embed.EmbedError` into `retrieve.RetrieverError`, and `retrieve.fuse()`'s caller
  already tolerates one retriever failing (`check_retrieval.py` reads `fused["failed"]`
  without treating it as fatal). An embed outage at query time degrades to keyword-only, not a
  crash — this was true the moment `Vector` was written and needs nothing from 5.4.
- **The producer, at ingest time — a real gap, found by this review rather than assumed away.**
  `_run_embeddings` only raises `ingest.ProducerUnavailable` (the "environment fault, not a
  data fault" signal every other producer's missing-library case already uses) when
  `model_for("embed")` itself raises `RoleUnavailable` — i.e., the CONFIG is missing. If the
  config is filled but the CONTAINER is down or unreachable, `embed.embed()` raises
  `EmbedError`, which is not specially handled and becomes an ordinary per-document `FAILED` —
  the wrong shape for "the whole embedding service is down," which is exactly the
  missing-parser-library case `app/producers.py`'s `_reader()` already exists to distinguish
  from a per-document fault. An outage today would record one `FAILED` row per document
  ingested during it, retried on every attempt, rather than one clean "unavailable" report.
  **Concrete fix, in `app/embed.py`:** `EmbedError` already wraps both `URLError`/`TimeoutError`
  (server unreachable — an environment fault) and `HTTPError` (server reached, this request
  refused — a real per-call fault) identically. Distinguishing them lets `_run_embeddings`
  translate only the first kind into `ProducerUnavailable`, the same distinction `_reader()`
  already draws. This is the "concrete interface problem" 5.4 was asked to look for — proposed
  here, not yet applied, since it is a real code change and the instruction was to present
  before applying.
- **Startup/diagnostic — not built, and not required by 5.4.** `app/llm.py`'s `model_window()`
  pattern (ask `/v1/models`, cache only a successful answer) could be mirrored for an operator
  health check, or folded into `scripts/check_services.py`. Left for whoever writes the runbook
  entry for bringing the embed container up, the same way `docs/runbook.md`'s "after any
  model change" check exists for the chat container today rather than being built into the app.

### 4. Development vs. production configuration

**Production needs no `.env` change** (see §2). **A dev laptop has two honest options, and
`.env.example` should say both rather than pick one:** point `EMBED_BASE_URL` at the shared
box's LAN address (`http://192.168.1.185:8001/v1`, the same pattern `scripts/bench_gateway.py`
already uses to reach the chat role from off-box), or leave it unset and accept that the
embeddings producer reports `ProducerUnavailable` locally — exactly how a laptop without a
local Ollama install already cannot exercise every chat-adjacent script today. Neither is
wrong; a laptop clone should not be forced to run a GPU container to run the test suite, and
`tests/test_vectors.py` already proves the code works with nothing live.

### 5. Shared or dedicated service

**Two different questions hid under one word, and they have different answers.** Shared
*across tenants*: yes, trivially — one embedding instance serves every tenant, the same
tenant-blind shape the inference plane's chat gateway already has (`GATEWAY_TOKENS` carry no
tenant). Shared *with the chat container, one process doing both jobs*: **not possible** —
confirmed live in 5.1, not assumed: `POST /v1/embeddings` against the running chat container
returns `HTTP 404`, because vLLM serves exactly one `--runner` per process. Dedicated container
is not a preference here, it is the only shape vLLM offers.

### 6. Model/version configuration and reproducibility

**Mirror the chat model's own discipline exactly — image pinned by digest, model pinned by
revision — because Step 3's docker-compose.yml comments already record why a moving tag or a
moving repo ref is a handover document that cannot describe itself.**

- **Image:** `vllm/vllm-openai:v0.28.0@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14`
  — the SAME image already pinned for chat and already proven against this exact model family
  during the Step 5.1 comparison. No new image to source or verify.
- **Revision:** `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` — fetched live from
  `huggingface.co/api/models/Qwen/Qwen3-Embedding-0.6B` on 18 September 2026 (`lastModified`
  2026-04-20), not guessed. The commit the chat model's own `--revision` pins to a specific
  date the same way; this is that same discipline applied to the second model.
- **A gap this review found, not one the existing design already closed:** `[roles.embed]`'s
  schema, if it copies `[roles.chat]`'s shape (`model`, `provider`, `context_tokens`), does
  **not** capture `revision` or the image digest. `_config_fingerprint()` (`app/vectors.py`)
  hashes whatever dict `model_for("embed")` returns — it is already correct and needs no code
  change — but it can only invalidate on a change it can see. A "same model name, quietly
  bumped revision" swap (exactly the kind of change §8/§9 below need to catch) would not
  change the fingerprint if `revision` is not a field in it, and embeddings are far more
  sensitive to exact-weights identity than chat is: a revision bump that is a harmless
  wording fix for a chat model can shift an embedding model's vector space in a way nothing
  reports as an error. **Recommendation:** `[roles.embed]` carries `model`, `provider` AND
  `revision` at minimum from the day it is first filled in — a schema decision, not an
  implementation one, and the freshness mechanism already handles whatever the schema gives it.

### 7. Resource / VRAM implications

**Real fixed cost, measured in 5.1: ~2.33 GiB** (weights + non-torch overhead + CUDA graph
memory, the clean breakdown from the isolated 0.3-utilization run, `docs/models.md`). **Free
headroom on the box today: ~8.4 GiB** (`docs/models.md` § "16384 to 32768", 16 September,
re-confirmed 18 September). Two sizing choices, both lessons the 5.1 benchmark already paid
for once:

- **`--max-model-len` sized to real usage, not the model's native ceiling.** Qwen3-Embedding's
  native context is 32,768 tokens; nothing this system embeds is anywhere near that
  (`TARGET_TOKENS` + `OVERLAP_TOKENS` in `app/chunks.py` is 576). The 5.1 benchmark crashed
  once at a low utilization budget specifically because it had not yet capped this — proposed:
  **1024** (576 with real headroom, matching the same "generous rather than cut to the
  minimum" reasoning the first working benchmark run used).
- **`--gpu-memory-utilization`, sized for the batch sizes actually measured, not left wide
  open.** 5.1's own benchmark showed the 9.9 GiB first reading was mostly an arbitrary KV-cache
  budget, not a requirement. Batches up to 64 chunks (the sweep 5.1 measured) need nowhere near
  8.4 GiB of cache. **Proposed starting point: 0.10** (~3.3 GiB of the 32,607 MiB card) —
  comfortably above the ~2.33 GiB fixed cost with room for concurrent ingest-time and
  query-time calls, and leaves headroom for Step 6 (speech), which Section 13's original
  budget sized at ~3.5 GiB. **Re-measure against the real startup log once deployed** — every
  number on this line is a proposal, not yet a measurement of the actual container, the same
  distinction `docker-compose.yml`'s own comments draw for every chat-model value on that file.

### 8 & 9. Upgrading the service without silently invalidating embeddings, and how a config
change reaches the freshness mechanism

**These are one answer, already built in 5.2, not a new 5.4 decision** — restated here because
the question was asked directly. `EMBEDDINGS.version` (`app/vectors.py`) derives from
`model_for("embed")`'s current config at process start; changing `[roles.embed]` in
`models.toml` and restarting the app (the same "takes effect on the next process start, not
mid-request" rule `app/models.py`'s own docstring already states) is the entire upgrade
procedure — no manual version bump, no script, no migration. Its correctness depends entirely
on §6's finding: the config must contain what actually identifies the deployed weights
(`revision`, not just `model`), or a change that should invalidate everything will not.

### 10. Logging / audit requirements for model and configuration changes

**`CHANGELOG.md` already states the rule this question is asking for**, word for word: "A
model change is a changelog entry... Swapping the served model, its quantisation or its
context length changes what the system does, even when no line of code moves." Proposed as the
standing requirement for `[roles.embed]`, not a one-off for this deployment: every change to
it — first deployment, a later revision bump, a quantisation change — gets **all four** of
what the chat model's own history already demonstrates doing: a `CHANGELOG.md` entry under
Changed; a `docs/models.md` update carrying the real startup-log numbers, not a projection; a
`docker-compose.yml` comment on the changed line explaining why, in the same density the chat
service's own flags already carry; and a `HANDOVER.md` entry the day it happens. This is not
new process — it is the existing one, named explicitly so "embed" is not quietly held to a
lower bar than "chat" has been since Step 1.

### The exact changes proposed, not yet applied

**`.env.example`**, additive only, documents code already merged:

```
# --- embeddings (Step 5.1/5.2) ---
# The OpenAI-compatible endpoint app/embed.py talks to. Mirrors LLM_BASE_URL:
# a separate variable because vLLM serves one --runner per process, so chat
# and embed are always two containers even on the same box (confirmed live,
# docs/models.md -- the chat container answers /v1/embeddings with a 404).
# Production default (127.0.0.1:8001) needs no override if the embed
# container is deployed on port 8001, the same way LLM_BASE_URL's default
# already matches the chat container's port with nothing set here.
EMBED_BASE_URL=http://127.0.0.1:8001/v1
# A dev laptop without a local embed container may point this at the shared
# box instead: http://192.168.1.185:8001/v1 -- the same pattern
# scripts/bench_gateway.py already uses to reach the chat role off-box.
EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBED_TIMEOUT=60
```

**`docker-compose.yml`**, a new service, not yet added:

```yaml
  vllm-embed:
    image: vllm/vllm-openai:v0.28.0@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14
    restart: unless-stopped
    ipc: host
    ports:
      - "8001:8000"
    volumes:
      - huggingface-cache:/root/.cache/huggingface
    command:
      - --model
      - Qwen/Qwen3-Embedding-0.6B
      - --revision
      - 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
      - --runner
      - pooling
      - --max-model-len
      - "1024"
      - --gpu-memory-utilization
      - "0.10"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

**`models.toml`**, filled per §6's schema finding:

```toml
[roles.embed]
model = "Qwen/Qwen3-Embedding-0.6B"
provider = "vllm"
revision = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
```

**Not proposed for change:** `app/vectors.py`, `app/embed.py`, `app/retrieve.py`. The one
concrete interface gap found (§3, `EmbedError` not distinguishing an unreachable server from a
refused request) is named but not fixed here, per the instruction to present before applying.

---

## 4. Step by step

Numbered like Step 4's sub-steps: each is small enough to gate on its own, and the order is
load-bearing — nothing after 5.0 can be built before it, and nothing here should be built out
of order just because it looks independent, the same lesson Step 4.0's tenant-bridge
prerequisite taught the hard way.

- **5.0 — DONE, 18 September 2026.** `LICENSE-APACHE` and `LICENSE-MIT` read in full in the
  `sqlite-vec` repository, by Amro; `docs/licences.md`'s row updated from `unverified` to
  `verified` — dual Apache-2.0/MIT confirmed, no additional terms. The free-VRAM figure this
  step spends against was already measured (`docs/models.md` § "16384 to 32768", 16 September)
  — it turned out not to depend on the context-length change that prompted this sub-step in the
  first draft of this plan.
- **5.1 — DONE, 18 September 2026.** `scripts/bench_embeddings.py` (latency/VRAM/dimension,
  not `bench_models.py` reused — it only speaks Ollama's API, which this box does not run) and
  `scripts/bench_retrieval_candidates.py` (a throwaway golden-set MRR harness, so the choice
  is not made on latency alone — imports `check_retrieval.py`'s own metric functions rather
  than re-deriving them, so the numbers are directly comparable to the 0.768 keyword baseline).
  Two candidates compared — `Qwen/Qwen3-Embedding-0.6B` and `BAAI/bge-base-en-v1.5` — on
  latency, VRAM, dimension, licence, and golden-set recall/MRR. **Decided: Qwen3-Embedding-0.6B**
  (overall MRR ties BGE, but wins decisively on paraphrase queries — the specific failure mode
  this step exists to fix — while BGE wins on clause queries, recorded as a real limitation
  rather than hidden). Full reasoning and numbers: `docs/models.md` §§ "Embedding candidates",
  "Second candidate: BAAI/bge-base-en-v1.5", "Decision: Qwen3-Embedding-0.6B". The running chat
  container does not serve embeddings either (confirmed live, `HTTP 404`) — `[roles.embed]`
  needs its own container for 5.2.
- **5.2 — DONE and SIGNED OFF, 18 September 2026. `app/vectors.py`: the embeddings producer
  and the `embeddings` table.** Sign-off confirmed by re-running `tests/test_vectors.py` on
  request against the running code, not by re-deriving the design: **14 passed, 0 failed**,
  covering every freshness scenario below by name. `depends_on=frozenset({"chunks"})` and a version derived from
  `[roles.embed]`'s config rather than hand-maintained (§5.4). Each row is keyed by `chunk_id`
  and carries a hash of the chunk's own text plus the config fingerprint that produced its
  vector; a chunk is reused only when both still match, everything else is recomputed, and
  rows for a `chunk_id` no longer in the document are dropped. Storage is a `BLOB` column and
  Python cosine similarity, not sqlite-vec — §5.2's own named fallback, used from the start
  because sqlite-vec is not an installed dependency and adding one is a decision for a person,
  not something to fold into an implementation turn; swapping it in later is a change inside
  `Vector.search()`, not a redesign. All three gates from §5.4 hold, tested in
  `tests/test_vectors.py`: an unchanged corpus embeds nothing (the producer is never even
  invoked); an edit near the end of one document re-embeds only the chunks whose text actually
  changed (and the test that checks this caught its own bug first — comparing chunk_id
  membership is not the same claim as comparing text, which is the exact gap the hash exists
  to close); a `[roles.embed]` config change re-embeds every chunk of every document with
  nobody bumping a version by hand.

  **A real design bug, caught by the existing suite rather than reasoned out in advance:**
  `EMBEDDINGS` was first written self-registering at import, mirroring `app/producers.py`'s
  `TEXT`/`CHUNKS`. Unlike those two, `embeddings` is optional and role-gated, and
  `ingest.status()`'s `ready` is "every HANDLING producer holding a current row" — so
  self-registering made every document in the WHOLE SYSTEM permanently not-ready the moment
  `app/vectors.py` was imported anywhere, `[roles.embed]` being empty. Broke `ready` assertions
  in `test_chunks.py`, `test_intake.py`, `test_plane_documents.py` and `test_tenant_isolation.py`
  the moment `tests/test_vectors.py` imported the module in the same pytest session — thirteen
  failures across four files nothing about this step should have touched. Fixed: `EMBEDDINGS`
  is constructed but not registered by `app/vectors.py` itself; registration is the caller's
  decision (`ingest.register(vectors.EMBEDDINGS)`), the same way 5.4 below was already going to
  be a deliberate separate step for the retriever. Full suite: 666 passed, 1 skipped.

- **5.3 — DONE, 18 September 2026. The `Vector` retriever**, implementing `retrieve.Retriever`
  against the table 5.2 built: embed the query through the same `embed` role, brute-force
  cosine similarity, ranks out — no score leaked, the same discipline `app/passages.py`'s
  `Keyword` already holds to. NOT self-registered at import, matching the same reasoning 5.2's
  fix above landed on: `retrieve.register(VECTOR)` is 5.4's job, not a module import's side
  effect. Gated with hand-picked vectors where the nearest neighbour is known by construction
  (two candidates close to the query AND to each other, not trivially different — the exact
  discipline this sub-step's own text already named) rather than the hash-derived fake vectors
  the producer tests use, since those give no control over which one should rank first.
- **5.4 — PROPOSED, 18 September 2026; not applied.** Register it, and watch the "provably
  does nothing" property survive contact with a real second retriever: `retrieve.register(
  vectors.VECTOR)` and `ingest.register(vectors.EMBEDDINGS)` (5.2's fix made this the
  producer's registration too, not automatic at import), then re-run the fusion's existing
  single-retriever test — it must still pass, because it is asserting a property of `fuse()`
  itself, not of how many retrievers happen to be registered when it runs. Blocked on a real
  embedding container existing first — full deployment architecture (where it runs, endpoint
  config, availability detection, dev/prod, shared-vs-dedicated, model pinning, VRAM sizing,
  upgrade safety, audit requirements) reviewed and proposed in § "3a. Deployment architecture
  for `[roles.embed]`" above, including the exact `docker-compose.yml`, `models.toml` and
  `.env.example` changes. **Awaiting sign-off before `docker-compose.yml` or `models.toml` are
  touched** — `.env.example` alone was updated, since it only documents code already merged.
- **5.5 — Run `check_retrieval.py` against the golden set with both retrievers live**, and
  record the result in `docs/models.md` and this plan, honestly, per section 3.5's three
  outcomes. This is the step's actual gate; 5.0 through 5.4 are what make it possible to run.
- **5.6 — Decide whether embedding runs inline or through `jobs.py`**, using 5.1's measured
  latency rather than the guess in section 3.4. If it is slow enough to matter, this sub-step
  wires it through the queue Step 2.2 already built rather than inventing a second one.

---

## 5. Risks

- **The embedding model is wrong for this corpus and nobody notices**, because MRR moving up
  by a little looks like success. Mitigated by 5.5's per-query-class breakdown (paraphrase
  queries specifically, not only the overall average) rather than trusting one number, the
  same reasoning `docs/models.md` used when it distrusted "5/5 on one question" as a complete
  fix during the `tool_choice` retry work.
- **`sqlite-vec`'s pre-v1 status breaks something after this step ships.** Named already in
  `docs/licences.md`; the fallback in 5.2 (a `BLOB` column and Python-side cosine similarity)
  is slower but has no new moving part, and is worth keeping as a documented escape rather
  than discovering it under pressure.
- **Embedding cost on the write path is worse than projected**, the same shape of miss
  `docs/models.md` has recorded twice for VRAM projections (21% and worse). 5.1's benchmark
  is what stands between this step and a third one — measured before committing, not
  estimated and corrected after the fact.
- **The `embed` role gets filled and nothing enforces that filling it does not silently
  change chat behaviour.** Not expected to be possible — `app/llm.py` reads `LLM_BASE_URL` /
  `LLM_MODEL` directly and `models.toml`'s `chat` entry only documents that deployment, per
  Step 4.7 — but worth a test asserting it explicitly, the same shape as the tenancy
  isolation tests that assert what SQLite actually opened rather than trusting config.
- **`[roles.embed]` changes and old vectors keep being served against the new model's space.**
  The failure mode §5.4 was rewritten to close: a chunk whose text never changed gives no
  signal that its embedding is now meaningless. Mitigated by deriving `EMBEDDINGS.version`
  from the embed config at import (forces every document's embeddings to be reconsidered on
  the next process start after a swap) and a per-row config fingerprint checked alongside the
  content hash (stops a stale vector being served even if the derived version ever collided).
  Worth a test asserting both halves explicitly before 5.2 ships, not just the golden-set MRR
  in 5.5 — a wrong-model vector can still resemble a real one well enough to rank plausibly.

## 6. Rollback

Every piece here is additive, the same property Step 4 protected throughout. `unregister
("vector")` removes the retriever from `retrieve.py`'s registry with no caller needing to
know; the `embeddings` producer and table can be dropped without touching `chunks` or
`documents`; `[roles.embed]` goes back to empty in `models.toml` and every existing caller of
`model_for("embed")` — there are none yet outside this step — gets the same `RoleUnavailable`
it already gets today. Nothing in Steps 0-4 reads anything this step writes, so rollback is
deleting what 5.0-5.6 added, not un-doing a change to something else.

## 7. What comes after

A second retriever existing at all is what makes a third one — a connector beyond `files`, a
SQL-aware retriever, whatever "connect the dots across 200 contracts" turns into — a
registration instead of a redesign, which was Step 4's whole bet. Vision (image chunks) and
speech (Step 6) each want their own producer feeding the same `chunks` table this step reads
from, not a parallel pipeline. None of that is scoped here; it is named so the next plan does
not have to re-discover that the seam already generalises.
