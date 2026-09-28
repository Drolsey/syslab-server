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

Send the token on `/api/*` requests (upload, ingest, files, jobs, chat) as
`Authorization: Bearer <token>` or `X-Syslab-Token: <token>`. The web page uses a cookie set by
`POST /api/login`.

**These tenant tokens do not work on `/api/v1/*`.** The retrieval plane has its own service
tokens and tenant mapping; see section 5.

## 3a. The operator dashboard (`/admin`)

A web page for Syslab staff on the office LAN: companies, their document folders, ingestion,
and a retrieval tester. It is **not** reachable from outside the LAN (see `ADMIN_ALLOWED_NETWORKS`
in `.env.example`) and is switched off entirely when `PUBLIC_MODE=true`.

**Create your account on the box** (once per person; there is no sign-up page):

```bash
python scripts/operator_account.py new amro --name "Amro Taha"     # asks for a password twice
python scripts/operator_account.py new sara --generate             # or print a strong one once
python scripts/operator_account.py list
python scripts/operator_account.py reset-password sara             # also signs them out
python scripts/operator_account.py disable sara                    # instant, reversible
```

Then open `http://192.168.1.185:8080/admin` from any machine on the office network.

| Page | What it does |
|---|---|
| Overview | App uptime, whether the chat and embedding models answer, jobs, recent activity |
| Companies | Create, disable, enable, delete (disable first, then type the id to confirm) |
| Corpus | Browse a company's folders; upload files or a whole folder (or drag them in); new folder; download; re-ingest one file; move to trash; **Ingest now**; watch the job |
| Retrieval tester | Ask a question as a company and see exactly what `/api/v1/retrieve` returns to database-agent |

- A company is a tenant. Creating one here is the same as `scripts/tenant.py new`, with an id you
  choose. Tokens and aliases for database-agent are still made with `scripts/tenant.py`.
- Uploading a folder keeps its structure: `client-docs/contracts/lease.pdf` stays exactly that,
  and that path is the document's name everywhere, including in retrieval citations.
- Hidden files (`.DS_Store`, `.git/...`) and file types nothing reads are skipped and listed in
  the upload summary. A file that already exists is left alone unless **Replace files that
  already exist** is ticked.
- An upload never ingests on its own request; the page starts one ingestion job when the batch
  finishes. If the app restarts mid-job the job is lost (jobs live in memory), but the documents
  still show as **Outstanding**; press **Ingest now**.
- **Move to trash** moves the file or folder to `trash/<company>/<time>/` and removes it from
  the index. Nothing is deleted; empty `trash/` by hand when you are sure.
- Deleting a company moves its documents to `data/_removed/`, never deletes them.
- Every change made through the page is recorded in `audit_log` in `control/control.sqlite3`,
  under the operator who made it.

**The test corpus as a company.** `python scripts/seed_test_corpus.py` creates
`syslab-test-corpus` from `tests/fixtures/corpus` (52 contracts under `contracts/`, 5 format
samples under `formats/`) and ingests it. Safe to run again. `formats/format_corrupt.pdf` fails
on purpose, so there is always a failed document to look at.

## 4. Ingesting a corpus

A tenant's documents live in `data/<tenant-id>/`. Formats: `.pdf`, `.xlsx`, `.xlsm`, `.docx`,
`.pptx`, `.md` (the non-PDF/Excel types are read through docling). Uploads are capped at
`MAX_UPLOAD_MB` (default 50).

**Step 1: get the files in.** Any of these works:

- **Upload** through the web page or `POST /api/upload`, one file at a time. It also starts
  ingesting that file.
- **Copy** the files straight into `data/<tenant-id>/`. Easier for a whole corpus, but nothing
  is processed until you trigger step 2. Copy as the user the app runs as (`syslab`) so the
  app can read them.
- **The dashboard** (section 3a): upload files or whole folders and it ingests them for you.

Subfolders are fine (since Step 11.1). A document's name is its path inside the tenant's folder,
e.g. `contracts/lease.pdf`, and that is the name every API and citation uses. A file at the top
of the folder keeps its bare name.

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

## 5. Searching (the retrieval plane, `/api/v1`)

This is what other systems (such as database-agent) call. It authenticates differently from the
web app, with two credentials that answer two different questions:

- **A service token** says which *system* is calling. Set `RETRIEVAL_TOKENS=<system>:<token>` in
  the box's `.env` (comma-separated for several systems; generate a token with
  `python scripts/new_token.py`; the system name is lowercase, e.g. `database-agent`). Restart
  the app after editing `.env`. If it is empty, `/api/v1/*` answers `503`.
- **An `X-Syslab-Tenant` header** names *that system's own* tenant id. An operator links it to one
  of ours: `python scripts/tenant.py alias link <system> <their tenant id> <our tenant id>`.
  An id nothing has linked answers `404`, never `403`. A missing header is `400`.

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/retrieve \
  -H "Authorization: Bearer $RETRIEVAL_TOKEN" \
  -H "X-Syslab-Tenant: <their tenant id>" \
  -H "Content-Type: application/json" \
  -d '{"query": "who pays if the supplier delivers late", "k": 8}'
```

`k` is 1 to 50 (default 8); `budget_tokens` caps returned text (default 4000). Other routes, same
auth: `GET /api/v1/documents`, `GET /api/v1/documents/{name}`, `POST /api/v1/ingest/{name}`.

With both retrievers registered, results are fused (reciprocal-rank fusion) from keyword and
vector rankings. Each passage's `found_by` says which retrievers found it, and the response's
`retrievers` lists which ran. A paraphrased query should find a clause even when it shares no
words with it; `found_by` containing `vector` is the visible proof the embeddings are wired in.

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

## 9. Testing database-agent's RAG wiring and tool calling

database-agent (the Next.js app in `../database-agent`) uses this box two ways: chat and tool
calling through the gateway (`:8080/v1`, model alias `syslab-default`), and document search
through the retrieval plane (`:8080/api/v1/retrieve`). Test the box side first with `curl`, so
that when the app misbehaves you know which half to blame.

**A. Retrieval plane, from the box.** Do sections 4 and 5 first: a tenant with documents ingested,
`RETRIEVAL_TOKENS=database-agent:<token>` in `.env` with the app restarted, and an alias linking
database-agent's tenant id to that tenant. Then run the section 5 `curl`. A good answer has:
- `retrievers` listing both `keyword` and `vector`, and `retrievers_unavailable` empty;
- passages whose `found_by` includes `vector` for a paraphrased query;
- a `coverage` block (`searched`, `matched`, `returned`).

Errors mean: `503` no `RETRIEVAL_TOKENS` set, `401` wrong token, `400` no `X-Syslab-Tenant`
header, `404` that tenant id is not linked.

**B. Tool calling, straight at the gateway.** Send a request that should make the model call a
tool, and check that the reply contains `tool_calls` instead of prose (the token is one of the
box's `GATEWAY_TOKENS`):

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"model": "syslab-default",
       "messages": [{"role": "user", "content": "What is the weather in Cairo? Use the tool."}],
       "tools": [{"type": "function", "function": {"name": "get_weather",
         "description": "Current weather for a city",
         "parameters": {"type": "object",
           "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]}'
```

Expect `choices[0].message.tool_calls[0].function.name` to be `get_weather` with
`{"city": "Cairo"}`. The model's `<think>` text should be in a separate `reasoning` field, not in
`content`. *(request shape not yet run; adjust if the gateway wants different fields)*

**C. Wire database-agent to the box.** In `database-agent/.env.local` (never commit it):

```
MODEL_BASE_URL=http://192.168.1.185:8080/v1
MODEL_API_KEY=<a GATEWAY_TOKENS value>
RETRIEVAL_BASE_URL=http://192.168.1.185:8080/api/v1
RETRIEVAL_TOKEN=<the database-agent value from RETRIEVAL_TOKENS>
```

Restart `npm run dev`. If `RETRIEVAL_BASE_URL` or `RETRIEVAL_TOKEN` is unset, the
`search_documents` tool is simply not offered, so a missing tool usually means missing config.

The app sends its own workspace tenant id as `X-Syslab-Tenant`. That exact id has to be linked on
the box with `python scripts/tenant.py alias link database-agent <that workspace id> <our tenant id>`.
Check with `python scripts/tenant.py alias list`.

The workspace id is whatever database-agent uses as its own tenant id (`lib/services/tenancy.ts`):
- **Local dev with no signed-in company** (open access, the dev default): the constant
  `ten_local`.
- **A signed-in user who belongs to a company:** that user's `companyId`, the `id` of their row in
  database-agent's `Company` table. Look it up with `npx prisma studio` in `database-agent`, or
  with SQL against its Postgres.
- **A user with no company** falls back to `ten_local`.

The `404` from step A does *not* tell you which id was sent; it only says the id isn't linked.

**D. Automated live test, from `database-agent`.** It skips itself unless all three variables are
set, and it queries contract text, so the linked tenant must hold the benchmark contracts
(`tests/fixtures/corpus/contracts/` in this repo):

```powershell
$env:RETRIEVAL_BASE_URL = "http://192.168.1.185:8080/api/v1"
$env:RETRIEVAL_TOKEN = "<token>"
$env:RETRIEVAL_TEST_TENANT = "<the workspace id you linked>"
npm test
```

`tests/retrieval-integration-live.test.ts` should then show a paraphrase query returning passages
found by `vector`, a bad token rejected with 401, and an unreachable server turned into an error.

**E. By hand in the app.** Ask a question that needs the documents, for example (from the
benchmark's paraphrase queries) *"if somebody else gets a sweeter deal later do we automatically
get it too"*. In the step trace you should see `Searched documents: ...` with a detail like
`5 of 40 matching passages`, then an answer that cites a source file. A failed retrieval shows as
a failed step with the reason, and the model is told retrieval failed rather than being handed
invented context. To test SQL tool calling, ask a data question and confirm a `run_sql` step
appears, and check the query really ran rather than trusting a table on screen (HANDOVER.md
explains why the screen alone is not proof).
