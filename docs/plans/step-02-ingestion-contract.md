# Step 2: The Ingestion Contract

Status: **SIGNED OFF 10 September 2026**, all five decisions in section 4 taken as
recommended. **2.0 to 2.4 are done**; 2.5 remains.
Written 3 September 2026, after Step 1 completed.

Two things this document says that were true when it was written and are not now, both
recorded rather than edited away:

- **The step numbering in section 2 is the old one.** "No vision model. Step 3" was written
  when Step 3 was image reading. Step 3 is the model gateway and is built; vision is not
  currently a numbered step. What section 2 means is unchanged: this step designs the
  contract and adds no producer beyond the one that already exists.
- **2.1's gate cites `check_search` at 9 of 9, and the total is not a thing to gate on.**
  Settled 10 September: both numbers are right. One of the nine checks, "Rebuilt from the
  files on disk", is inside `if args.rebuild or before["not_yet_indexed"]`
  (`scripts/check_search.py:81`), so a run against an already-current index reports 8 of 8
  and a run with `--rebuild` reports 9 of 9. The denominator is a function of invocation,
  not of behaviour. **Gate 2.1 on the named checks all passing, with `--rebuild` so the
  conditional one is among them.**

---

## 1. What this step is

One answer to the question "what happens when a file enters this system", so that
everything which will later want something out of a document asks the same pipeline
instead of building its own.

Today there is one thing: text, extracted once, into an FTS5 row. Vision wants page
images. Numeric filtering wants extracted fields. A future semantic search wants chunks
and embeddings. Each of those could be bolted on separately, and if they are, the system
ends up with four parallel readers of the same PDF, four places where a file can be
half-processed, and no way to say whether a document is ready.

**This step adds no capability a user would notice.** What it buys is that image reading,
Step 3, is a new producer in an existing pipeline rather than a second pipeline.

## 2. What this step is not

- No vision model. Step 3.
- No embeddings or vector search. That is a producer this contract makes possible, not
  something to build while designing the contract.
- No change to how a document is searched today. FTS5 stays exactly as it is; it just
  stops being the only consumer.
- No new UI.

---

## 3. What happens to a file today

| Path in | What happens |
|---|---|
| `POST /api/upload` | Bytes written into the tenant's folder, then `search.index_file` |
| `tools.write_excel` / `write_pdf` / `db.query_to_excel` | File written, then `tools._index_quietly` |
| A file copied into the folder by hand | Nothing, until something calls `search.rebuild` or notices it is stale |

`index_file` extracts text and writes one FTS5 row keyed by **filename alone**. That is the
whole of ingestion. Three consequences worth naming before designing anything:

1. **There is no record that a file was processed**, only a row that exists or does not.
   "Not indexed" and "indexed, no text in it" are told apart by a `reason` string, and a
   file that failed halfway is indistinguishable from one nobody has looked at.
2. **Indexing is deliberately silent.** `_index_quietly` swallows every failure so that a
   write never fails because of the index. That is right for a cache and wrong for
   anything a user will later be told is "ready".
3. **It is synchronous and in-request.** Fine for extracting text from a 2 MB PDF, not fine
   for OCR-ing a 200 page scan, which is exactly what Step 3 brings.

---

## 4. Five decisions

### 4.1 What is the unit the pipeline produces?

| Option | Shape | Verdict |
|---|---|---|
| a. Keep it implicit | Each consumer stores its own thing its own way, as FTS5 does now | This is the status quo, and the thing this step exists to stop. |
| **b. A derived artifact** | `(tenant, source file, producer, producer version) -> bytes or rows`, with a status | **Recommended.** One table can then answer "is this document ready", "what failed", and "what needs redoing", for producers that do not exist yet. |
| c. A document object with typed fields | A rich model: `Document.text`, `.pages`, `.fields` | Reads better until the fifth producer, then every new capability edits a shared class. |

**Recommendation: (b).** The pipeline knows nothing about what a producer makes. It knows
that something was asked for, whether it succeeded, when, and against which version of the
producer. That is what lets Step 3 add vision without touching Step 2's code.

### 4.2 Where do derived artifacts live?

Per-tenant, always, since Step 1. Within that:

| Option | Cost |
|---|---|
| a. Inside the FTS5 index file | It stops being disposable. That index is currently deletable at any moment and rebuilt from `data/`, which is a property worth more than the convenience. |
| **b. `derived/<tenant>/` for bytes, plus a manifest table** | **Recommended.** Page images and OCR output are files; their status is rows. Both stay rebuildable from `data/`. |
| c. All of it in the control plane | The control plane is small, precious and backed up. Page images are none of those. |

**Recommendation: (b),** and keep the rule that made the search index safe: **everything under
`derived/` can be deleted and rebuilt from `data/`.** If a producer ever makes something that
cannot be regenerated, it is not derived and does not belong there.

The manifest is a SQLite database per tenant, `derived/<tenant>/manifest.sqlite3`, for the
same reason the index is per tenant: a forgotten `WHERE` is silent, a wrong path is loud.

### 4.3 Synchronous or through the job lane?

| Option | Verdict |
|---|---|
| a. Everything in the request, as now | An upload that OCRs a 200 page scan is a request that times out. |
| b. Everything through the job lane | An upload no longer produces a searchable file for several seconds, which breaks "upload then immediately ask about it". |
| **c. Fast producers in the request, slow ones queued** | **Recommended.** Text extraction is already fast enough to be in-request and stays there. A producer declares its own cost, and anything expensive becomes a job. |

The job lane already carries a tenant and runs the handler as its owner, so this costs
nothing new. Each producer declares `slow = True/False`, and that is the whole interface.

### 4.4 When does a producer re-run?

The thing that makes a pipeline trustworthy, and the thing most likely to be got wrong.

Re-run when any of these changed: the source file's **size or mtime**, or the producer's
declared **version**. Store both in the manifest row. A producer bumping its version
invalidates everything it made, which is how a fixed OCR bug gets applied to documents that
were processed before the fix.

Deliberately NOT content-hashing the file. Hashing a 200 MB workbook on every upload to
notice a change that size and mtime already caught is a cost paid every time to catch a case
that is rare and not silent.

### 4.5 What does failure mean?

Today it means nothing: `_index_quietly` swallows it.

**Recommendation: a producer failing is recorded, not raised.** One failed producer must not
fail the upload, must not stop the other producers, and must not be invisible. The manifest
row gets `failed`, the error, and the attempt count, and `search_files` can say "this
document is text-only because its page images failed" instead of quietly returning less.

The distinction Step 1 already taught this project applies here too: **a missing library is
not a broken document.** A producer that cannot run at all is an environment fault and is
reported once, not recorded as a failure against every file in the folder.

---

## 5. The design, concretely

```
data/<tenant>/                     source documents, unchanged
index/<tenant>.sqlite3             FTS5, unchanged, still disposable
derived/<tenant>/manifest.sqlite3  what has been produced, and what failed
derived/<tenant>/<producer>/...    the bytes a producer made
control/control.sqlite3            unchanged
```

```sql
CREATE TABLE artifacts (
    source_name      TEXT NOT NULL,     -- the filename in data/<tenant>/
    producer         TEXT NOT NULL,
    producer_version INTEGER NOT NULL,
    status           TEXT NOT NULL,     -- ok | failed | skipped
    source_size      INTEGER NOT NULL,  -- invalidation, with mtime
    source_mtime     REAL NOT NULL,
    output_path      TEXT,              -- relative to derived/<tenant>/, when it made a file
    detail           TEXT,              -- the error, or why it was skipped
    attempts         INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (source_name, producer)
);
```

**`app/ingest.py`**, the only module that decides what runs:

```python
@dataclass
class Producer:
    name: str
    version: int
    handles: set[str]      # suffixes
    slow: bool
    run: Callable[[Path], Result]

def ingest(name: str, *, only_fast: bool = False) -> dict
def needs(name: str) -> list[str]        # which producers are stale for this file
def status(name: str) -> dict            # is this document ready, and what failed
def forget(name: str) -> None            # a file left; drop its rows and outputs
```

The first producer is the text extraction that already exists, moved rather than rewritten,
so that Step 2 lands with the pipeline proven against known-good behaviour and nothing new
in it.

---

## 6. Step by step

**2.0** `app/ingest.py` and the manifest schema, imported by nothing. Gate: a fake producer
runs, records, invalidates on size, mtime and version change, and its failure is recorded
rather than raised.

> **DONE, 10 September 2026.** `app/ingest.py`, `tests/test_ingest.py` (23 tests), and
> `DERIVED_DIR` in `app/config.py`. Suite 404 passed, 1 skipped, up from 381. Nothing
> imports it, which was the point: the pipeline is proven before the first real producer
> moves behind it.
>
> **Three departures from this document, all deliberate.**
>
> 1. **`run` takes the source path AND the directory to write into**, rather than the
>    one-argument signature in section 5. A producer that chooses its own output location
>    is a producer whose output nothing else can find or delete, and 4.2's promise — that
>    `derived/` can be deleted and rebuilt — would then rest on every producer remembering
>    to keep it. Handing the directory down makes it structural. The layout is
>    `derived/<tenant>/<producer>/<source file>/`, a directory per source rather than a
>    file, because a producer that makes one thing today makes forty page images tomorrow
>    and `forget()` has to remove all of it without knowing which.
> 2. **A `Result` says `ok` or `skipped`, never `failed`.** Failure is the exception the
>    producer raises and the pipeline records, so a producer never has to remember 4.5.
> 3. **A producer that does not handle a suffix gets no row at all**, rather than a
>    `skipped` one. The alternative fills the manifest with the absence of work nobody
>    asked for and buries the skips that mean something.
>
> **One thing the plan did not specify, decided here.** A `failed` row is retried on the
> next `ingest()` call and `attempts` counts the goes at the same unchanged input; a
> change to the file or the producer version resets it to 1, because three failures
> against the old bytes say nothing about the new ones. There is no retry cap: the
> pipeline records, and a policy about when to stop belongs where the retrying is
> scheduled, which is 2.3.
>
> **One bug found by its own test.** Pruning the empty per-source directory left
> `derived/<tenant>/<producer>/` standing, which reads as "this producer has output here"
> to anyone listing the folder. Both are pruned now.

**2.1** Move text extraction into a producer. `search.index_file` becomes a consumer of it.
Gate: `check_search` unchanged at 9 of 9, and the suite unchanged, because nothing about the
behaviour should differ.

> **DONE, 10 September 2026.** `app/producers.py` holds `_reader`, `extract` and the `text`
> producer, moved verbatim out of `app/search.py`; `search.index_file` now calls
> `ingest(name, only_fast=True)` and reads the artifact. Import direction is
> `config <- ingest <- producers <- search`, and search must never be imported back the
> other way. Gate: `check_search --rebuild` 9 of 9, `check_tools` 12 of 12, suite 406
> passed / 1 skipped. `search.SEARCHABLE`, `MAX_TEXT_PER_FILE` and `extract` stay as names
> on `search` so nothing that imported them has to care that they moved.
>
> **The gate earned itself twice, which is the argument for doing 2.1 before anything
> interesting.**
>
> 1. **The mtime tolerance was wrong, and only became wrong here.** 2.0 carried
>    `search.stale()`'s one-second tolerance into `_is_current()` on the reasoning that the
>    two ought to agree about the same file. They serve different purposes: `stale()`
>    produces a *suggestion*, so being loose costs a rebuild nobody needed, while
>    `_is_current()` decides whether **derived data may be served for bytes that no longer
>    exist**. An existing test rewrote a PDF with a body of the same length inside the same
>    second and the pipeline handed the index the old text. The comparison is exact now.
>    Nothing had been wrong before 2.1 because `index_file` re-extracted every time and had
>    no cache to be stale.
> 2. **`scripts/tenant.py` did not know a tenant had a third thing on disk.** Deleting a
>    tenant removed the folder and the index and left `derived/<tenant>/`, which holds text
>    extracted from that customer's documents. **This is 2.5's storage half and it was
>    pulled forward**, because 2.1 is what makes an upload create the artifacts: shipping
>    2.1 without it ships a delete that leaves a copy of the customer's content behind. The
>    artifacts are *deleted* even when the documents are only moved aside, since everything
>    in `derived/` can be made again from the files.
>
> **Two consequences, both left for 2.4 on purpose rather than half-done here.** Nothing
> calls `ingest.forget()` yet, so an artifact outlives the file it came from — `check_search`
> writes 41 decoy PDFs, deletes them, and leaves 41 text artifacts behind. And
> `search.rebuild()` still refuses to run at all when a parser is missing, even though the
> text it needs is now already extracted; that pre-check is existing behaviour and changing
> it is not a no-behaviour-change sub-step.

**2.2** Wire the write paths. `_index_quietly` becomes `ingest(name, only_fast=True)`.
Uploads and tool writes go through one path. Gate: `check_tools` unchanged.

> **DONE, 10 September 2026.** `app/intake.py`, and the three write paths — the upload
> endpoint, `tools._index_quietly`, and `db.query_to_excel` — now call `intake.arrived`.
> Gate: `check_tools` 12 of 12 unchanged.
>
> **It is a new module rather than a line in `search.py`, and that is the point of the
> sub-step.** All three callers already went through one function, `search.index_file`, so
> "one path" was arguably true before. What was not true is that the path was owned by
> anything entitled to own it: the index is a *consumer* of the pipeline, and a consumer
> that also drives the pipeline and schedules its jobs is the owner of it wearing a
> different hat. `intake` owns the ORDER — fast producers, then the index, then the queue —
> and owns none of the work. Import direction extends to
> `config <- ingest <- producers <- search <- intake`.
>
> `intake.arrived` never raises. Two of the three callers already wrapped their call in
> `except Exception: pass` because a write that already succeeded must not be lost to a bad
> day in the index; that guarantee now lives in one place instead of three.

**2.3** Slow producers through the job lane, and `POST /api/ingest` plus a status the UI can
show. Gate: a slow producer on a large file does not block the upload response.

> **DONE, 10 September 2026.** Gate met: a producer that blocks until a test releases it is
> queued rather than waited on, and the upload returns in well under a second with the text
> indexed. Asserted against a producer that genuinely blocks on an event, not one that
> sleeps — a sleep makes the test a race against the machine it runs on and passes on a
> fast one for the wrong reason.
>
> - **`ingest.deferred(name)`** — the slow producers still outstanding, asked as a question
>   rather than remembered from `ingest(only_fast=True)`'s return value, so the caller that
>   schedules the work need not be the call that skipped it and a file left half-produced by
>   a restart is still answerable.
> - **One job kind, `ingest_slow`**, not one per producer. The lane serialises anyway, and a
>   file with three slow producers outstanding wants one queue entry that finishes when the
>   document is ready, not three that each look like the whole job.
> - **`GET /api/ingest`** (the folder sorted into ready / outstanding / failed),
>   **`GET /api/ingest/{name}`** (is this document ready, and what failed),
>   **`POST /api/ingest/{name}`** (bring it up to date, returning as soon as the fast
>   producers are done).
> - **The upload response gained `outstanding` and `job`.** Without them an uploader has no
>   way to know the document is unfinished: `searchable` says the text is in and says
>   nothing about what is still queued behind it. Additive, and `/api/*` is not the frozen
>   surface — only `/v1` is.
> - **A full queue is a delay, not a loss.** The manifest still records the producer as
>   stale so the next ingest picks it up, and the response says so rather than swallowing
>   it, because "nothing happened and nothing said so" is the failure mode 4.5 exists to
>   prevent.
>
> **Not done live over HTTP.** The gate ran through the real ASGI app and the real job-lane
> threads under `TestClient`; a uvicorn socket in front of that is not where the risk is,
> and minting a token in the developer's own control plane to prove it was not worth the
> side effect.
>
> **A test-hygiene finding worth keeping.** The lane is a module-level singleton with one
> worker, so a producer left blocking by a failed assertion wedges it for the full timeout
> and fails the NEXT test too. It read as two bugs and was one. The release is in a
> `finally` now.

**2.4** Rebuild and forget: `derived/` deleted entirely must reconstruct from `data/`, and a
deleted source file must leave nothing behind. Gate: extend `check_search`, and add
`scripts/check_ingest.py`.

> **DONE, 10 September 2026.** `scripts/check_ingest.py` is the new gate, **14 of 14**, and
> `check_search` grew a tenth check. On this install the orphans are gone: 11 artifacts for
> 11 documents, where the folder had been carrying 41 decoys' worth of leftovers.
>
> - **`ingest.forget_missing()`** reconciles BOTH sides against `data/`, not just the
>   manifest. A producer folder can hold output for a source with no row -- a crash between
>   writing the bytes and recording them -- and sweeping only the rows would leave that on
>   disk with nothing pointing at it, which is the harder sort to notice. The gate tests it.
> - **`ingest.rebuild()` reconciles before it produces.** A rebuild that added what was
>   missing but left what should not be there would make "rebuilt" mean a little less every
>   time it ran.
> - **`search.rebuild()` now calls `ingest.rebuild(only_fast=True)` first**, rather than
>   relying on per-file ingestion. A rebuild reads the text artifacts, so "rebuild from the
>   files on disk" is only honest if what stands between it and the disk is current. Fast
>   producers only: an index rebuild must not turn into an OCR run.
> - **`POST /api/ingest`** (folder-level, always a job) is the rebuild affordance the app
>   never had -- `search.rebuild()` was reachable only from a script, so an operator whose
>   index had drifted had to open a terminal. It is also the only thing that notices a
>   document removed by hand, which for now is every document that ever leaves.
>
> **Deliberately NOT hooked into `search()`'s read path.** `search.forget_missing()` is
> there because a stale index row is returned to the model and wastes a turn; nothing reads
> an orphaned artifact, so paying for a manifest open on every search would buy tidiness at
> the cost of the hot path.
>
> **`check_gateway_isolation` was incomplete and is now not.** Its forbidden list names
> every module that reaches tenant storage, and Step 2 added three it had never heard of.
> A module that touches `derived/` and is not on that list is a hole that looks exactly
> like a pass. 16 checks to 24.
>
> **Two findings from writing the gate.** `check_search`'s totals moved again, 9 to 10,
> which is the second time in two days -- gate on the named checks, never the count. And an
> assertion that counted search hits failed for a reason unrelated to what it tested,
> because `search()` falls back from "all terms" to "any term" and the run's tag matched
> every file the script had written; it asserts by document name now.

**2.5** Tenancy still holds. Gate: `check_isolation` grows a section: one tenant's derived
artifacts are invisible and unreachable from another, and deleting a tenant takes its
`derived/` with it.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| The manifest becomes the thing that cannot be rebuilt | The rule in 4.2, enforced by the 2.4 gate that deletes `derived/` wholesale and reconstructs |
| A producer that is slow AND in-request slips in | `slow` is declared per producer and 2.3's gate asserts an upload stays fast |
| Silent failure returns, because it is convenient | 4.5, and a status call that names what failed |
| Scope creep into vision or embeddings | 2.1's first producer is the existing text extraction, moved and not improved |
| Another sub-step forgets tenancy | 2.5, and the conftest guard that fails any test writing outside `tmp_path` — which is exactly how 2.1 found that `scripts/tenant.py` had never heard of `derived/` |

## 8. Effort

Three to four sessions. 2.1 is the delicate one, because moving working code must change no
behaviour at all, and the gate for it is that every existing number stays the same.
