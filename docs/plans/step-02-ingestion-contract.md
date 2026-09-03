# Step 2: The Ingestion Contract

Status: PLANNED, NOT STARTED. Needs Amro's sign-off on the five decisions in section 4.
Written 3 September 2026, after Step 1 completed.

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

**2.1** Move text extraction into a producer. `search.index_file` becomes a consumer of it.
Gate: `check_search` unchanged at 9 of 9, and the suite unchanged, because nothing about the
behaviour should differ.

**2.2** Wire the write paths. `_index_quietly` becomes `ingest(name, only_fast=True)`.
Uploads and tool writes go through one path. Gate: `check_tools` unchanged.

**2.3** Slow producers through the job lane, and `POST /api/ingest` plus a status the UI can
show. Gate: a slow producer on a large file does not block the upload response.

**2.4** Rebuild and forget: `derived/` deleted entirely must reconstruct from `data/`, and a
deleted source file must leave nothing behind. Gate: extend `check_search`, and add
`scripts/check_ingest.py`.

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
| Another sub-step forgets tenancy | 2.5, and the conftest guard that fails any test writing outside `tmp_path` |

## 8. Effort

Three to four sessions. 2.1 is the delicate one, because moving working code must change no
behaviour at all, and the gate for it is that every existing number stays the same.
