# Architecture

For an engineer who did not build this and needs to change it safely.

Two things are described below: **what exists today**, and **what it is becoming**. They are
kept apart on purpose, because a document that blends the two leaves you unable to tell
whether a thing you cannot find is missing or was never built. Anything marked PLANNED is a
design, not code.

---

## 1. What this system is, in one paragraph

`syslab-server` is a local AI capability server. It runs open-weight models on hardware you
own, and exposes them over HTTP to applications that need inference, document retrieval and
speech. It also enforces which customer a request belongs to, and keeps each customer's
documents, search index and derived data physically separate.

It is **not** the product. The product is a pair of Next.js websites that talk to it.

---

## 2. The boundary rule

> **`syslab-server` never learns what a conversation is.**

No conversations, no run records, no user model, no charts, no reports. If a feature needs
to remember what the user said last turn, it belongs in the website.

This is the rule everything else falls out of, and the one most likely to be broken by
accident, because adding "just one conversation table" is always the shortest path to the
feature in front of you. The consequence of breaking it is two half-products that both need
maintaining and neither of which is complete.

The websites already have conversations, runs, an agent loop, tool rendering, a role and
scope model, and a versioned public API. Duplicating that here would mean rebuilding all of
it, in a second language, worse.

---

## 3. Who does what

| Concern | Where it lives | Why there |
|---|---|---|
| Conversations, runs, history | The website | It already has them, with a versioned contract |
| The agent loop and tool orchestration | The website | Same, plus its UI renders the tool steps |
| Client database connections | The website | It owns the connectors and stores credentials in a secret manager |
| Rendering: charts, tables, reports | The website | Contract-bound to its own UI |
| Users, roles, sign-in | The website | It is where people log in |
| Running models | **Here** | The GPU is here |
| Embeddings and retrieval | **Here** | PLANNED. Retrieval needs the documents, which are here |
| Speech to text, text to speech | **Here** | PLANNED. Same GPU, same budget |
| Which customer owns a byte | **Here** | The isolation gates that prove it are here |

---

## 4. Today: modules and their dependencies

Twenty modules in `app/`. The import graph is acyclic and deliberately so, and it is listed
bottom-up: nothing on a line imports anything below it.

```
context.py      -> nothing          the tenant, in a ContextVar
chunks.py       -> nothing          splits a string into passages; opens no file, asks no clock
retrieve.py     -> nothing          the retriever seam: Hit, Retriever, the registry
sources.py      -> nothing          where a tenant's material comes from; `files` today
config.py       -> context          every env var and every per-tenant path
llm.py          -> config           one POST to the model server
gateway.py      -> config, llm      the inference plane: /v1, no tenant, no storage
tenancy.py      -> config, context  the control plane: tenants, tokens, aliases
jobs.py         -> config, context  in-memory queue and worker threads
plane.py        -> config, context, tenancy    the /api/v1 tenant bridge
db.py           -> config, context, tenancy    read-only PostgreSQL
tools.py        -> config           file tools, sandboxed to the tenant folder
ingest.py       -> config, sources  the producer pipeline and the manifest
parse.py        -> ingest           suffix -> backend table, Docling behind it
producers.py    -> chunks, ingest, parse       text and chunks: what gets derived
search.py       -> config, ingest, producers   FTS5 over whole DOCUMENTS
passages.py     -> config, ingest, producers, retrieve, search    FTS5 over PASSAGES
intake.py       -> ingest, jobs, passages, search    what happens to a new file, in one place
agent.py        -> config, db, jobs, llm, search, tools
main.py         -> everything       FastAPI, the auth dependency, the three planes
```

`context.py` imports nothing because `config` imports it and `tenancy` imports `config`, so
it has to sit at the bottom. Three edges are function-local imports rather than module-level
ones (`tools -> db`, `tools -> intake`, `db -> intake`) purely to keep it that way. If you
add an import and something starts failing at startup, that is why.

**`intake.py` is the one to read before adding anything to the read path.** Both indexes are
brought up to date there, because before it existed three call sites each decided what
happens to a new file and they had already drifted once. `search.py` and `passages.py` are
siblings that never call each other's write path, and `passages.index_file` deliberately
does **not** bring the pipeline up to date the way `search.index_file` still does: two
consumers each running the pipeline for themselves is that same drift, arriving again.

### The one rule inside the code that matters most

`context.current_tenant()` **raises when no tenant is set. There is no default.** A default
is how one customer's request quietly reads another customer's files. The same idea shows up
everywhere: an unset value is an error, never a fallback.

Storage follows from it:

```
data/<tenant>/                 customer documents          DATA_ROOT
index/<tenant>.sqlite3         FTS5 indexes, disposable    INDEX_ROOT
  documents                    whole documents, for search_files     Step 1
  chunks                       passages, for retrieval               Step 4.4
control/control.sqlite3        tenants, tokens             CONTROL_DIR
logs/server.log
derived/<tenant>/              derived artifacts, disposable   DERIVED_ROOT
  manifest.sqlite3             what has been produced, and against which version
  text/<item>/text.txt         extracted text                  Step 2.1, parser swapped at 4.2
  chunks/<item>/chunks.json    passages, with offsets into text.txt   Step 4.3
```

Three directories, three different guarantees, and they are kept apart because of those
guarantees rather than for tidiness:

- `data/` is **customer content**. Losing it loses their documents.
- `index/` is **disposable**. It can be deleted at any moment and rebuilt from `data/`.
  Nothing may be stored here that exists nowhere else.
- `derived/` is **disposable on the same terms**, and `scripts/check_ingest.py` deletes it
  entirely on every run to prove it. The manifest lives *inside* it rather than beside it for
  that reason: a record that outlived the artifacts it describes would report a document
  ready and point at files that are gone. Since Step 4.3 the check is stricter than
  "everything came back" — the passages must come back **byte for byte identical**, because
  every citation the retrieval plane issues is a pair of offsets into these files.
- `control/` is **precious and small**. Losing it loses every tenant's identity and every
  token. It is the one directory that must be backed up, and it is the reason `tenancy.py`
  opens its database with `synchronous=FULL` while `search.py` uses `NORMAL`.

`config.resolve_in_data_dir()` is the gate for every filename that arrives from outside. It
rejects `..`, rejects drive letters, resolves symlinks, and then checks the result is inside
*this tenant's* folder. Because every tenant's folder is a subdirectory of one root, the same
check that stops an escape to the system drive also stops a walk sideways into another
customer's data.

### Today's limitations, stated plainly

These are real and they shape the plan. None is a bug.

- **No streaming.** `llm.py` hardcodes `"stream": False`. `/api/chat` blocks for the whole
  tool loop and returns one JSON body.
- **No vector search.** `search.py` and `passages.py` are FTS5 keyword matching only. This
  was a deliberate choice, not an oversight: the `Retriever` seam in `retrieve.py` is the
  place a vector retriever registers, and Step 5 is where it does. **A paraphrase of words
  the document does not use will not be found today** — measured, not assumed:
  `tests/fixtures/corpus/baseline.json` puts paraphrase queries at Recall@1 of 0.056.
- **The two indexes do not cover the same formats.** `search.SEARCHABLE` is three suffixes;
  the chunk index covers everything that produces chunks, which is all ten formats in
  `parse.py`'s table. So a `.docx` has passages and is not in the document index. Named in
  `docs/plans/step-04-retrieval-plane.md` § 4.4 rather than closed quietly, because widening
  the document index moves the 4.1 baseline that Step 4 is measured against.
- **Jobs do not survive a restart.** The queue is in memory.
- **Per-tenant database credentials do not work.** `tenancy.database_for()` raises for any
  row it finds, because no cipher was chosen. Only the bootstrap tenant reaches a database,
  through `.env`. See Step 4.5 in `docs/plans/step-01-ownership-boundary.md`.
- **`GET /` and `GET /api/docs` are unauthenticated.** Safe behind Tailscale, not safe on
  the public internet.
- **The login throttle grows without bound**, keyed by client IP and pruned only within a
  key.

---

## 5. Target: three planes

One FastAPI process, three groups of endpoints separated by what they may touch. All three
are mounted as of 11 September; the retrieval plane's own endpoints are the part still to
come, and the table marks which.

| Plane | Endpoints | Tenant | May touch tenant storage |
|---|---|---|---|
| **Inference** | `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, `/v1/audio/*` | none | **No, enforced by a source check** |
| **Retrieval** | mounted and authenticated (Step 4.0); `/api/v1/retrieve` is Step 4.5, `/api/v1/documents` and `/api/v1/ingest/{name}` Step 4.6 | required, resolved through `tenant_alias` | Yes |
| **Local** | the existing `/api/*` and `app/agent.py` | required | Yes, unchanged |

The inference plane being unable to reach tenant storage is what bounds the damage from a
leaked service token to "someone used your GPU" rather than "someone read every customer's
documents". That is worth more than it costs, and a source check asserts it rather than a
comment claiming it.

**`app/agent.py` is frozen.** It keeps working as the offline path and as the way to debug
the tool layer when the website is down. New agent capability goes in the website. For the
same reason, `app/web/index.html` is an admin and debug page now, not a product surface.

---

## 6. How a request arrives

Today, from a person:

```
browser -> Tailscale -> FastAPI -> require_auth -> token -> tenant -> ContextVar
                                                                   -> agent loop
                                                                   -> Ollama + tools
```

PLANNED, from a website:

```
Cloud Run -> Cloudflare Tunnel -> FastAPI -> service token
                                          -> X-Syslab-Tenant header
                                          -> tenant_alias lookup -> local tenant id
                                          -> ContextVar
```

`require_auth` is an **async generator dependency**, and that is not cosmetic. FastAPI runs a
`def` dependency in one worker thread and a `def` endpoint in another. A context set in a
sync dependency is gone before the endpoint runs. This was measured, and the result is
recorded in sub-step 1.5 of the Step 1 plan. If you make it sync, tenancy silently stops
working and the tests that catch it are in `tests/test_context.py`.

The same class of problem appears twice more. A plain `threading.Thread` does not inherit a
context, which is why `jobs.py` re-enters `context.use_tenant(job.tenant_id)` inside the
worker, and why `agent.py` submits parallel tool calls through `contextvars.copy_context()`.

### The tenant id collision

**BUILT, Step 4.0, 11 September 2026.** The website's tenant is a database id from its own
schema.
`context.py` requires `^[a-z][a-z0-9_-]{0,31}$` **and that value becomes a directory name.**

**A foreign id must never become a filesystem path.** The bridge is an explicit
`tenant_alias` table mapping (external system, external id) to a local tenant id generated
here. An unlinked id is a **404, not a 403**, because "that tenant exists but is not yours"
confirms an id someone guessed. The job lane already sets this precedent.

`app/plane.py` is where that rule is kept, and it is kept by the foreign id reaching exactly
one function — `tenancy.resolve_alias`, as a bound lookup key. It never reaches
`validate_tenant_id`, and what reaches `context.set_tenant` is the local id that came back
out of the control plane. `../../etc/passwd` is not rejected as malformed; it is an id
nothing linked, and it gets the same 404 as `12345`, because a distinct error for a
malformed id tells a stranger which of their guesses had the right shape.

**The system comes from the token, never from a header.** `RETRIEVAL_TOKENS` in `.env` is
`system:token` pairs rather than the flat list `GATEWAY_TOKENS` is, because ids only mean
anything inside one system's namespace: a caller that could name its own system could
resolve ids in another's, and the `(external_system, external_id)` key would be decoration.
`scripts/check_isolation.py` § 7b asserts all of this, each check written by breaking the
property first.

---

## 7. Model serving

Today: Ollama, over its REST API, using only the standard library. There is no client
library on purpose, because pinning one against a fast-moving local server produces version
mismatches that are hard to debug.

PLANNED: vLLM on the production machine, for two reasons. Continuous batching, which is the
difference between one user at a time and several; and native support for the full
`tool_choice` range, which the website sends on every request carrying tools and which
Ollama documents as unsupported.

Both speak the OpenAI chat completions shape, so `llm.py` will target that shape and the
serving layer becomes a base URL in configuration. That is the whole anti-lock-in mechanism:
swapping vLLM for Ollama, llama.cpp or anything else is a config change, not a rewrite.

Model choice moves out of a single `OLLAMA_MODEL` string into a profile file, because five
roles need five values and the development machine needs different ones from production. The
website pins a stable alias such as `syslab-default` and never a real model name, so changing
the model touches one file on one machine.

---

## 8. Safety layers on the database path

`app/db.py` ranks its four layers, and the ranking is the useful part:

1. The database role holds `SELECT` and nothing else.
2. Every connection sets `default_transaction_read_only`, a statement timeout and an
   idle-in-transaction timeout.
3. The module refuses anything that is not a single `SELECT` or `WITH`.
4. A row cap and an export cap.

**Layer 3 alone would be theatre. Layer 2 is what actually holds.** A string check can be
defeated by a dialect quirk nobody thought of; a connection that cannot write cannot write.
Keep that order in mind before adding a fifth string check.

Two details worth knowing before touching this file. A CTE can hide a write in PostgreSQL,
which is why literals are blanked before keywords are scanned. And an ordinary cursor pulls
the entire result into memory during `execute()`, so a row cap applied afterwards does
nothing at all; the server-side cursor is what makes the cap real.

---

## 9. Testing and gates

Two layers, and they answer different questions.

`tests/` is 338 tests run with `pytest`. `tests/conftest.py` carries an autouse fixture that
snapshots the real `data/`, `index/` and `control/` directories around **every** test and
fails any test that added something to them. It is per-test rather than per-session because a
session-scoped check reports its failure against whichever test happened to run last, which
names the wrong one.

`scripts/check_*.py` are gate scripts that exercise the real thing: real files, a real model,
a real database, real HTTP. They print `N passed, N failed, N not tested` and return non-zero
on any failure *or any untested check.*

Three habits run through all of it and should be kept:

- **Three outcomes, never two.** Passed, failed, and not tested. A check that could not run is
  not a check that passed.
- **Never loosen a gate to make it pass.**
- **Write a check by breaking the thing first.** A check that only proves the good path works
  is not a check. `scripts/check_isolation.py` was verified by pointing every tenant at one
  folder and confirming 8 of its checks failed.

---

## 10. Where to look first

| To understand | Read |
|---|---|
| Why there is no default tenant | `app/context.py`, the docstring on `current_tenant` |
| How a token becomes a tenant | `app/tenancy.py:resolve_token`, then `app/main.py:require_auth` |
| Why paths are safe | `app/config.py:resolve_in_data_dir` |
| Why the SQL guard is shaped that way | the header of `app/db.py` |
| What isolation actually means here | `scripts/check_isolation.py` |
| What was decided and why | `docs/plans/step-*.md`, then `docs/adr/` |
| What may not be used, and why | `docs/licences.md` |

The commit history is unusually informative in this repository. Commit messages record what
was rejected and what it cost, not only what changed. When something looks arbitrary,
`git log -S` on the surprising line is usually faster than reasoning about it.
