# How to use this system

A hands-on guide for the person operating the syslab box: get a shell that works, add
documents, check that search finds them, and run the benchmark. For restarting services and
fixing things that broke, see [runbook.md](runbook.md). For why it is built this way, see
[architecture.md](architecture.md).

**Status of this guide.** Every command below was checked against the code in this repo, but
not every one has been run end to end on the box. Steps marked *(not yet run)* are the ones to
confirm the first time you use them, and to correct here if they differ.

---

## 0. Where you are matters

All commands assume you are in the repo root on the box:

```bash
cd ~/syslab-server
```

Not in `scripts/`. Running from the wrong folder is the most common way the commands below fail:

- Paths like `scripts/check_retrieval.py` only resolve from the repo root.
- `python3 -m venv .venv` run inside `scripts/` creates a second, empty venv at
  `scripts/.venv` (delete it with `rm -rf scripts/.venv`); the real one is
  `~/syslab-server/.venv`.
- `requirements.txt` lives in the repo root.

## 1. Activate the virtualenv

```bash
cd ~/syslab-server
source .venv/bin/activate        # prompt now starts with (.venv)
deactivate                       # to leave it
```

Or skip activating and call it directly: `.venv/bin/python scripts/check_retrieval.py`.

Use `python`, not `py`. On the box, `/usr/bin/py` is an unrelated tool that evaluates a Python
expression; the `py scripts\...` commands in older docs are the Windows launcher.

If a script reports `Missing: pymupdf, openpyxl` (or docling packages), the venv has drifted.
Repair it from the repo root:

```bash
.venv/bin/pip install -r requirements.txt
```

If `.venv` does not exist at all: `python3 -m venv .venv`, activate it, then the line above.

## 2. Services: what should be running

Three things run on the box, started three different ways (details in
[runbook.md](runbook.md)):

| What | Port | Check |
|---|---|---|
| vLLM chat model | 8000 | `docker compose ps` |
| syslab-server app | 8080 | `sudo systemctl status syslab-server@syslab` |
| vLLM embeddings (`vllm-embed`) | 8001 | `curl -s http://127.0.0.1:8001/v1/models` |

Embeddings are optional and detected at startup. If the embed container is down when the app
starts, the app runs with keyword search only and does not register the vector retriever;
restart the app after bringing the embed container back.

## 3. Tenants and tokens

Each customer is a tenant with its own folder, index and tokens. Nothing is shared between
tenants.

```bash
python scripts/tenant.py new "My Corpus"       # creates the tenant, prints a token ONCE
python scripts/tenant.py list
python scripts/tenant.py show <id>
python scripts/tenant.py token new <id> --label "amro laptop"
python scripts/tenant.py token revoke <fingerprint>
python scripts/tenant.py disable <id>          # instant and reversible; touches no files
```

Copy the token when it is printed. Only its hash is stored, so it cannot be shown again; if
lost, revoke it and issue another.

Send the token on API requests as `Authorization: Bearer <token>` or `X-Syslab-Token: <token>`.
The web page uses a cookie set by `POST /api/login`.

## 4. Ingesting a corpus

A tenant's documents live in `data/<tenant-id>/`. Formats: `.pdf`, `.xlsx`, `.xlsm`, `.docx`,
`.pptx`, `.md` (the non-PDF/Excel types are read through docling). Uploads are capped at
`MAX_UPLOAD_MB` (default 50).

**Step 1: get the files in.** Either way works:

- **Upload** through the web page or `POST /api/upload`, one file at a time. It also starts
  ingesting that file.
- **Copy** the files straight into `data/<tenant-id>/`. Easier for a whole corpus, but nothing
  is processed until you trigger step 2. Copy as the user the app runs as (`syslab`) so the
  app can read them.

**Step 2: trigger ingest.** *(not yet run against a hand-copied folder)*

```bash
TOKEN=<the tenant's token>
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/ingest
```

This reconciles the whole folder as a background job and returns `202` with a job id right
away. For a single file: `POST /api/ingest/<filename>`.

Order of work per document: text extraction, then chunking, then embeddings. Embeddings only
run if the embed container was up when the app started.

**Step 3: confirm it finished.**

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/ingest
# -> {"ready": [...], "outstanding": [...], "failed": [...], "producers": [...]}
```

- `ready` means every registered producer holds a current result for the file.
- `outstanding` means work is still queued or running. Check `GET /api/jobs`.
- `failed` means a producer errored. `GET /api/ingest/<filename>` says which and why.
- `producers` should include `embeddings` when the embed container is up. If it is missing,
  documents are getting keyword search only.

**Editing or deleting files.** Editing a file re-embeds only the chunks whose text changed.
Deleting one is swept from the chunk and embedding tables on the next search.

## 5. Searching

The retrieval API, tenant-scoped by the token:

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/retrieve \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "who pays if the supplier delivers late", "k": 8}'
```

`k` is 1 to 50 (default 8); `budget_tokens` caps returned text (default 4000). Other routes:
`GET /api/v1/documents`, `GET /api/v1/documents/{name}`, `POST /api/v1/ingest/{name}`.

With both retrievers registered, results are fused (reciprocal-rank fusion) from keyword and
vector rankings. A paraphrased query ("who pays if the supplier is late") should find the
clause even when it shares no words with it; that is what the vector retriever adds.

## 6. Measuring retrieval quality

```bash
cd ~/syslab-server
source .venv/bin/activate
python scripts/check_retrieval.py
```

It runs entirely in a temporary folder (your real `data/`, `index/` and `derived/` are not
touched), staging `tests/fixtures/corpus/contracts/*.pdf` under a tenant called `goldenset` and
scoring 42 queries from `tests/fixtures/corpus/golden.json`. `EMBED_BASE_URL` defaults to
`http://127.0.0.1:8001/v1`, which is right on the box; set it only to point elsewhere.

Recorded result from the 19 September deployment ([models.md](models.md),
[HANDOVER.md](../HANDOVER.md)) to compare against:

| MRR | Keyword | Vector | Fused |
|---|---|---|---|
| Overall | 0.768 | 0.402 | 0.779 |
| Exact | 0.960 | 0.517 | 0.938 |
| Clause | 0.686 | 0.230 | 0.646 |
| Paraphrase | 0.430 | 0.419 | 0.616 |

Reading it honestly: the gain is modest overall (+0.011 MRR) and the real win is paraphrase
(0.430 -> 0.616, on only 8 queries). Fused is *worse* than passages-only on recall@10
(0.871 -> 0.807) and recall@1 (0.653 -> 0.611), and slightly worse on exact and clause MRR. The script now prints those
regressions next to the win, unscored.

The Fusion section should end with PASS lines and exit code 0: `fuse()` returns a single list
unchanged, the vector list reaches the fusion, and overall and paraphrase MRR both beat
passages-only. (Before 19 September's correction the script printed FAIL here, because its
one-retriever assertions cannot hold with two retrievers; see HANDOVER.md.) If the vector
table is missing entirely, the embed container was not reachable and only keyword was
measured; the script says so.

The script is fixed to the bundled contracts corpus. To benchmark a different corpus you need
your own golden set (queries with the expected document for each) and a copy of the script
pointed at it; ingesting documents does not create one.

## 7. Other checks

| Command | What it verifies |
|---|---|
| `python -m pytest tests/test_vectors.py -q` | embeddings producer: reuse, invalidation, stale rows (14 tests recorded) |
| `python -m pytest -q` | whole suite (677 passed, 1 skipped recorded on 19 Sep) |
| `python scripts/check_ingest.py` | Step 2's ingestion gate |
| `python scripts/check_search.py` | search index |
| `python scripts/check_isolation.py` | tenants cannot see each other |
| `python scripts/bench_gateway.py` | concurrency against the gateway on :8080 |
| `python scripts/bench_embeddings.py` | embed latency, dimension, VRAM |

Gate scripts redirect `data/`, `index/`, `derived/` and `control/` to a temp folder before
opening anything, so they are safe to run against a live box.

## 8. Quick failure lookup

| Symptom | Cause | Fix |
|---|---|---|
| `NameError: name 'scriptscheck_...'` | ran with `py`, backslashes | use `python` and `/` |
| `can't open file .../scripts/scripts/...` | wrong directory | `cd ~/syslab-server` |
| `Missing: pymupdf, openpyxl` | not in the venv | `source .venv/bin/activate` |
| `requirements.txt` not found | wrong directory, or stray `scripts/.venv` | `cd ~/syslab-server`; `rm -rf scripts/.venv` |
| No `embeddings` in `producers` | embed container was down at app start, so the app skipped registering it | start `vllm-embed`, restart the app |
| `401 Not signed in` | missing or wrong token | send `Authorization: Bearer ...` |
| Login `429` | too many wrong tokens | wait fifteen minutes |

More failures and their fixes: [runbook.md](runbook.md) § Common failures.
