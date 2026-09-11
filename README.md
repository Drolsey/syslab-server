# syslab-server

A local AI server. It runs open-weight models on hardware you own, reads and writes PDFs and
spreadsheets, searches documents, queries a customer's database read-only, and keeps every
customer's data separate from every other customer's.

No cloud model, no per-request bill, no public URL unless you deliberately open one.

```
website / browser  -->  [ FastAPI front door ]  token -> which customer is this?
                              |
                        [ Ollama / Qwen3 ]  decides which tool to call
                              |
                        [ tools ]  PDF, Excel, search, read-only SQL, jobs
                              |
                        [ data/<tenant>/ ]  one folder per customer
```

It began as a single-user assistant reached from a laptop over Tailscale, and that still
works. It is becoming the model, retrieval and speech backend for two websites that keep
their own agent and their own interface. See **Where this is going** below.

## Stack

| Piece | Choice | Why |
|---|---|---|
| Model runtime | Ollama | Loads the model on the GPU, exposes a local REST API, same commands on Windows and Linux |
| Model client | `app/llm.py`, standard library only | One POST to one endpoint. No client library to fall out of step with a fast-moving local server |
| Model | `qwen3:8b` (fallback `qwen3:4b`) | Solid tool calling at a size a 12 GB card can hold |
| Tools | PyMuPDF, openpyxl, reportlab | Ordinary libraries, testable without the model |
| Search | SQLite FTS5, one file per tenant | Keyword search over extracted text. No embeddings yet, on purpose |
| Control plane | SQLite, `control/control.sqlite3` | Tenants and hashed tokens. Portable SQL only, so the move to PostgreSQL is one file |
| Customer database | psycopg 3, read-only | Optional. Four layers between the model and any damage |
| Web app | FastAPI + one static HTML page | No build step, no framework |
| Remote access | Tailscale | Private tailnet, nothing exposed to the open internet |

## Where this is going

The direction changed after Step 1. This server is becoming a **capability server**: it
serves models, retrieval and speech, and the websites that call it keep their own agent,
their own conversations and their own interface. The rule that follows is worth knowing
before you add anything:

> **This server never learns what a conversation is.** No conversations, no run records, no
> user model, no charts, no reports. If a feature needs to remember what the user said last
> turn, it belongs in the website.

`app/agent.py` and `app/web/index.html` still work and are still useful for running this
standalone and for debugging the tool layer. They are frozen: new agent capability goes in
the website, not here.

Read [`docs/architecture.md`](docs/architecture.md) first, then [`docs/plans/`](docs/plans/)
for what was decided and why.

## Build phases

Each phase ends at a gate you can actually check. Do not skip one; the next phase assumes
the previous actually worked.

- [x] **00 Verify hardware and OS** — `python scripts/check_env.py`. Gate: you know your real VRAM number and your OS.
- [x] **01 Install Ollama, pull the model** — `python scripts/check_ollama.py`. Gate: the model answers over the local REST API.
- [x] **02 Write the tools, test them alone** — `python scripts/check_tools.py` and `pytest`. Gate: each works on a real file, called directly.
- [x] **03 Wire the model to the tools** — `python scripts/check_agent.py`. Gate: a question about a real file gets a correct, tool-backed answer from the terminal.
- [x] **04 Give it a face** — `python -m app.main`, then open http://127.0.0.1:8000.
- [x] **05 Make it survive a logout** — `scripts/service/install_windows.ps1`, then `python scripts/check_services.py`. Gate: reboot, and it is reachable again untouched.
- [x] **06 Open the door, carefully** — `python scripts/new_token.py`, then Tailscale, then `python scripts/check_remote.py`.
- [x] **07 Prove it end to end** — `python scripts/check_endtoend.py --url ... --token ...`, run from the laptop.

After the phases, work is tracked as numbered **steps** in `docs/plans/`, each with its own
plan document, sub-steps and gate:

| Step | What it is | Status |
|---|---|---|
| 0 | Clean baseline | Complete |
| 1 | The ownership boundary: every byte has an owner | Complete, `scripts/check_isolation.py` is its gate |
| 3 | The model gateway: serve `/v1` from our own hardware | Built. Its gate belongs to the website — see below |
| 2 | The ingestion contract | **Complete**, 10 Sep. `scripts/check_ingest.py` is its gate |
| 4 | The retrieval plane, and the seams the rest plugs into | **In progress**, 11 Sep. 4.0 (tenant bridge), 4.1 (corpus, golden set, measured baseline) and 4.2 (one parser for many formats, the source seam) done; `scripts/check_retrieval.py` is its gate. `docs/plans/step-04-retrieval-plane.md` |

Step 3 comes before Step 2 on purpose: that is the plan's own dependency order, and Step 3
is the one that stops the per-request model bill. Its gate is the website's own provider
code, unmodified, completing a multi-turn tool-calling conversation against `/v1` from Cloud
Run — not a curl. `HANDOVER.md` has the current state and what is next.

## Target machine

Current, as of 8 Sep 2026:

| | |
|---|---|
| OS | Ubuntu, on a dedicated box |
| GPU | NVIDIA GeForce RTX 5090, 32 GB VRAM |
| Model | `Qwen/Qwen3-14B-AWQ` at a 16384 window, served by vLLM `v0.28.0` in a container; image pinned by digest, model by revision |

The move from the original Windows laptop (RTX 3060, `qwen3:8b` under Ollama) was a
configuration change rather than a rewrite, which was the point of routing every path
through `pathlib` and every setting through `.env`. A laptop still runs the whole suite and
the offline gates; set `LLM_BASE_URL` at Ollama's own `/v1` shim and nothing else changes.
`docs/models.md` has the benchmark numbers behind the model choice and the digest it is
pinned to.

## Setup

```bash
git clone https://github.com/Drolsey/syslab-server.git
cd syslab-server

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
python scripts/new_token.py
```

Then edit `.env`. The settings that matter first are `OLLAMA_MODEL` and `DATA_DIR`.
`.env.example` documents every variable the code reads, and that is checkable: `app/config.py`
reads 27 and `.env.example` documents the same 27.

## Layout

```
app/               FastAPI app, tool functions, model loop
  config.py        every setting and path, read from .env
  context.py       whose request is this
  tenancy.py       the control plane: tenants and tokens
data/<tenant>/     customer documents, one folder each      (gitignored)
index/<tenant>.sqlite3   that tenant's search index         (gitignored, disposable)
control/           control.sqlite3: tenants, tokens         (gitignored, back this up)
logs/              server.log
docs/              architecture, licences, plans
scripts/           gate scripts and admin tools
tests/             the unit suite
```

**Three directories, three different guarantees.** They are separate because of the
guarantees, not for tidiness.

- `data/` is **customer content**. Losing it loses their documents.
- `index/` is **disposable**. Delete it at any moment and rebuild from `data/`. Nothing may
  live here that exists nowhere else.
- `control/` is **small and precious**. Losing it loses every tenant's identity and every
  token. It is the one directory that must be backed up, and the reason `tenancy.py` opens
  its database with `synchronous=FULL` while `search.py` uses `NORMAL`.

## Tenants and isolation

Every byte the system stores or returns has an owner, and the code is built so it cannot
return one owner's byte to another. This is treated as a security property, not a database
convenience.

A tenant is a customer organisation. Each has its own folder, its own search index, its own
jobs and its own database credentials. Ids are opaque and generated, not derived from the
customer's name, because a folder name is not a place to leak a client list.

**The rule the whole design rests on:** `context.current_tenant()` **raises when no tenant is
set. There is no default.** A default is how one customer's request quietly reads another
customer's files.

The tenant travels in a `ContextVar` rather than as a function argument. That is deliberate:
`agent._coerce_arguments` derives each tool's required arguments from its signature and
reports them to the model, so a `tenant` parameter would become a security boundary a small
model can see and try to guess.

Three places where that context does not travel by itself, each found by measurement rather
than by reasoning:

- A **sync FastAPI dependency** hands nothing to the endpoint, because they run in different
  worker threads. `require_auth` is an async generator for exactly this reason.
- A **plain `threading.Thread`** inherits nothing, so `jobs.py` re-enters
  `context.use_tenant(job.tenant_id)` inside the worker.
- A **thread pool** is the same problem, so `agent.py` submits parallel tool calls through
  `contextvars.copy_context()`.

Managing tenants:

```bash
python scripts/tenant.py new "Acme Ltd"       # create, prints a token once
python scripts/tenant.py list
python scripts/tenant.py show <id>
python scripts/tenant.py disable <id>         # instant, reversible, touches no files
python scripts/tenant.py enable <id>
python scripts/tenant.py delete <id>          # dry run by default
python scripts/tenant.py token new <id>
python scripts/tenant.py token list [<id>]
python scripts/tenant.py token revoke <fingerprint>
```

A token is shown once, when created, and never again. The store holds only its SHA-256, so
there is no command that could print it back to you.

**Deletion is deliberately hostile.** It runs dry unless you pass `--apply`, `--confirm` must
repeat the id exactly, it refuses a tenant that is still active (disable first, because the
first act is instant and reversible and the second is neither), it refuses the bootstrap
tenant outright, it refuses to run while the service is listening, and it *moves* documents
to `data/_removed/` rather than destroying them unless you pass `--purge-files`.

The gate for all of this:

```bash
python scripts/check_isolation.py
```

It runs entirely inside a temporary folder with two invented tenants, and its first check is
that it is not touching your real folders. It covers files, paths, the search index, jobs,
the database, the no-owner rule, HTTP with two real tokens, suspension and deletion. Each
check was written by breaking the thing first: pointing every tenant at one folder fails
eight of them.

## The tools

`app/tools.py`, `app/search.py`, `app/db.py` and `app/jobs.py`. Plain functions with no
knowledge that a model exists. Each returns a JSON-serialisable dict, fed straight back to
the model. `agent.REGISTRY` holds twelve.

| Function | Does |
|---|---|
| `list_files()` | What is in this tenant's folder, with sizes and dates |
| `search_files(query)` | Keyword search across every indexed PDF and spreadsheet |
| `read_pdf(filename, pages=None)` | Text of a PDF, optionally just pages `"1-3,7"` |
| `read_excel(filename, sheet=None, max_rows=200)` | Cells of a worksheet, plus the sheet names |
| `write_excel(filename, rows, ...)` | Creates a workbook or appends rows. A value starting `=` becomes a live formula |
| `write_pdf(filename, title, body)` | A simple text PDF. Blank lines split paragraphs, `# ` starts a heading |
| `list_tables()` | Tables in the customer's database the role may actually read |
| `describe_table(table)` | Columns and types. No sample rows, on purpose |
| `run_sql(sql)` | One read-only SELECT, row-capped |
| `query_to_excel(sql, filename)` | The same, written to a file with a far larger row ceiling |
| `queue_image(prompt)` | Queues slow work through the job lane |
| `job_status(job_id)` | Progress of a queued job |

Two behaviours worth knowing:

- Failures raise `ToolError` with a plain sentence, not a stack trace, because that sentence
  goes back to the model as the tool result and it can correct itself. Asking for a file
  that is not there returns the closest matching names.
- A formula cell reads back as `None` until Excel has opened the file once. openpyxl reads
  cached results, and a file Excel has never seen has no cache. That is the library behaving
  correctly, not a bug in the tool.

## The loop

`app/agent.py`. One question in, one answer out, with tool calls in between.

1. Send the conversation plus a description of every tool to the model.
2. It either answers, or asks for a tool with arguments.
3. Run the real Python function, append the result as a `tool` message.
4. Send the whole thing back. Repeat until it answers or hits `MAX_TOOL_STEPS`.

`agent.ask()` returns the answer, a `steps` trace of what it actually called, and the full
message list to pass back as `history` next turn.

Read-only tools called together in one turn run concurrently, so "read these five invoices"
costs one wait rather than five. Anything that writes stays sequential, because two writers
racing on one file is a bug nobody enjoys finding.

The model never touches a file. It only ever asks, and `run_tool` decides.

Four things the loop does that are not obvious, all added after a real session went wrong
rather than guessed at in advance:

- **Argument aliases.** Qwen3 reliably calls `write_excel` with `data` and `sheet_name`
  instead of `rows` and `sheet`. `ARGUMENT_ALIASES` remaps the names models actually reach
  for. A key that is already correct is never overwritten by an alias, and per-tool aliases
  beat global ones because `table` means something different to `write_excel` and
  `describe_table`.
- **Errors that teach.** When a required argument is missing, the message names what is
  missing, the full accepted signature, and which of the keys sent were not real.
- **A blank reply is not an answer, and neither is a promise.** Models return an empty turn
  after a tool failure, or narrate a plan ("let me start by reading the PDFs") and stop.
  Both get one nudge, on the loop's own budget rather than the user's.
- **Wrong-tool mistakes are answered, not escalated.** Asking to read `invoice.xlsx` when
  only `invoice.pdf` exists returns "that exists, read it with read_pdf", because the file
  was found and only the tool was wrong. Clarifying questions are reserved for real
  ambiguity, not for things the program can work out.

## Running it

```bash
python -m app.main
# then open http://127.0.0.1:8000
```

`app/main.py` serves one page and twelve routes:

| | |
|---|---|
| `GET /` | the page, `app/web/index.html`, one file, no build step |
| `POST /api/login` | exchange a token for a cookie |
| `POST /api/logout` | drop the cookie |
| `GET /api/health` | model, data folder and a fingerprint of the running code |
| `GET /api/files` | what is in this tenant's folder |
| `POST /api/upload` | one .pdf, .xlsx or .xlsm, size-capped |
| `GET /api/files/{name}` | download |
| `POST /api/jobs` | queue slow work, returns **202** and an id immediately |
| `GET /api/jobs` | this tenant's queued, running and recently finished jobs |
| `GET /api/jobs/{id}` | status, progress, queue position, result |
| `DELETE /api/jobs/{id}` | cancel |
| `POST /api/chat` | message plus history in, answer plus tool trace out |

Every `/api/*` route carries `Depends(require_auth)` individually rather than relying on
middleware, so a new route is unauthenticated only if someone actively forgets.

The page shows every tool call the model made, so you can see whether an answer came from
reading your file or from thin air. A failed call opens the trace automatically.

Uploads are treated as hostile input: the browser-supplied name is reduced to its last path
component, stripped of anything exotic, and checked against a list of extensions we handle.
Uploading a name that already exists writes `report (2).pdf` rather than overwriting. The
size cap is enforced **during** the read, in 1 MiB chunks, because checking the length after
reading the whole body protects the disk and nothing else.

## The door

Two locks, and the order they went on matters.

Tailscale decides which *machines* can reach the app: only devices signed into your account,
over a WireGuard tunnel, with no public address for anyone to find. The token decides which
*people* get in once a machine can, and **which tenant they are**. One deliberate check in
the app is worth more than trusting a network setting to stay the way you left it, and this
app writes real files.

A presented token resolves in two steps. The control plane is asked first; if it has no
answer, `APP_TOKEN` from `.env` maps to `BOOTSTRAP_TENANT`. That order matters: a control
plane that will not open authenticates nobody through the first path and falls through to the
second, so a broken control plane leaves the operator able to get in and fix it, and nobody
else able to get in at all.

- Every `/api/*` route needs the token. `/` is public so the sign-in form can load.
- Browsers get an HttpOnly cookie from `POST /api/login`. The cookie is set to **the token
  they signed in with**, never `APP_TOKEN`; setting it to the operator's token handed every
  tenant the operator's credential, which with one tenant looked tidy and with two was a
  privilege escalation.
- Scripts use `X-Syslab-Token` or `Authorization: Bearer`.
- Eight wrong tokens from one address and it stops answering for fifteen minutes.
- Comparison is `hmac.compare_digest`, so a wrong guess takes the same time as a right one.
- An unknown token, a revoked token and a disabled tenant all fail identically, because
  "that token exists but is revoked" is itself a fact worth not disclosing.

**The guard that matters most:** if `APP_HOST` is anything other than loopback and
`APP_TOKEN` is missing or weak, the app refuses to start and says why. There is no
configuration in which this listens on a network without a real token.

Traffic inside the tailnet is plain HTTP, which is fine: Tailscale encrypts every packet
between your devices with WireGuard. The token is never sent over the open internet.

## Known limitations

Real, current, and none of them a bug. They shape what is planned next.

- **No streaming.** `llm.py` sets `"stream": False`. `/api/chat` blocks for the whole tool
  loop and returns one JSON body.
- **No vector search.** Search is FTS5 keyword matching. A deliberate choice, not an
  oversight, and the seam for changing it is `docs/plans/step-02-ingestion-contract.md`.
- **Jobs do not survive a restart.** The queue is in memory and the status endpoint says so
  rather than pretending otherwise.
- **Per-tenant database credentials do not work.** `tenancy.database_for()` raises for any
  row it finds, because no cipher has been chosen. Only the bootstrap tenant reaches a
  database, through `.env`. Recorded as decision 4.5 in the Step 1 plan.
- **`GET /` and `GET /api/docs` are unauthenticated.** `/` serves only the sign-in shell, but
  `/api/docs` publishes the whole API surface. Safe behind Tailscale. Not safe the moment
  this is publicly reachable.
- **The login throttle grows without bound**, keyed by client IP and pruned only within a
  key. Bounded on a private tailnet, unbounded behind a public tunnel.
- **PyMuPDF is AGPL-3.0.** It is the PDF reader for the whole search path, and no decision
  has been made about it. See [`docs/licences.md`](docs/licences.md).

## Running it as a service

```powershell
# Administrator PowerShell, once
powershell -ExecutionPolicy Bypass -File scripts\service\install_windows.ps1
python scripts\check_services.py
```

Two scheduled tasks, `syslab-ollama` and `syslab-server`, both starting at boot and
restarting up to three times on failure. Sleep and hibernate are turned off on mains power,
because a sleeping desktop cannot answer.

**Why scheduled tasks and not a real Windows service.** A service runs as LocalSystem, which
has its own user profile. Ollama would look for your pulled models under the wrong
`%LOCALAPPDATA%` and find none, and the virtualenv would not be where the service expects.
These tasks run as *you*, using a logon type called S4U, meaning "as this user, without a
login session and without storing their password". Same profile, same models, and it
survives a logout.

**A running service is a snapshot of the code as it was when it started.** Editing a file
changes nothing until you restart the task, and that gap is genuinely confusing: every check
passes and the behaviour is still old. So `/api/health` reports a fingerprint of the code the
running process loaded, and `check_services.py` compares it against what is on disk and says
plainly when they differ. After changing anything in `app/`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\service\restart_windows.ps1
```

That restarts the web app only. Ollama is left alone unless you pass `-IncludeOllama`,
because restarting it drops the model out of VRAM and the next question waits for it to load
again.

### Turning it off

```powershell
$c = "scripts\service\control_windows.ps1"
powershell -ExecutionPolicy Bypass -File $c -Action status    # what is running
powershell -ExecutionPolicy Bypass -File $c -Action stop      # off until the next restart
powershell -ExecutionPolicy Bypass -File $c -Action disable   # off, and stays off (needs admin)
powershell -ExecutionPolicy Bypass -File $c -Action enable    # back on (needs admin)
```

`disable` is almost always the right one. Nothing is deleted: the tasks, your token, your
files and your settings all stay exactly as they are, and `enable` puts it back in one
command. `stop` only lasts until the machine restarts, because the tasks still run at boot.

Two things it deliberately does not touch. Sleep stays disabled until you say otherwise
(`powercfg /change standby-timeout-ac 30` restores it), and Ollama's own tray app still
starts with Windows.

`scripts/service/uninstall_windows.ps1` removes both tasks and leaves the power settings
alone, because those are a preference for the machine rather than ours to guess at.

On Linux, `scripts/service/syslab-server.service` is the equivalent, and Ollama's installer
already provides `ollama.service`. That one file is the entire Windows to Linux difference.

## Running things on Windows

PowerShell refuses to run `.ps1` files by default, so `.venv\Scripts\activate` fails with a
`PSSecurityException` on a stock machine. Nothing is broken, and you do not have to change
that setting. `run.cmd` uses the project's Python directly, and batch files are not covered
by the policy:

```powershell
.\run.cmd scripts\check_remote.py
.\run.cmd -m pytest -q
.\run.cmd -m app.main
```

The `.ps1` scripts in `scripts/service/` are invoked with `-ExecutionPolicy Bypass`, which
allows that one command without changing anything permanently. If you would rather have
`activate` work, this is the usual one-time change and it applies to your account only:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Either way, `py` on its own is the *system* Python launcher and will not have this project's
packages.

`check_remote.py` and `check_services.py` are the exceptions: they import only
`app/config.py`, which has no third-party dependencies, so they run from any Python. That is
deliberate. A script whose job is to tell you why the app is broken should not need the app's
packages to run.

## Proving it works end to end

```bash
# on the desktop, against itself
python scripts/check_endtoend.py

# from your laptop, which is the run that means something
python scripts/check_endtoend.py --url http://<machine>.ts.net:8000 --token <token>
```

It builds a PDF and a spreadsheet containing facts invented for that run, uploads them, asks
a question only the file can answer, has a row appended, then **downloads the workbook and
looks inside it**, so the check does not depend on anything the model said about its own
work. Then it has a PDF written and confirms the bytes that come back really are a PDF.

`scripts/_fixtures.py` builds those files with nothing but the standard library, and the
checker speaks HTTP with `urllib`, so both files can be copied onto any machine with Python
and run there. The test worth running is the one from somewhere else, so it must not need the
project installed there.

A call is not a success: every check that matters looks for a tool step that returned `ok`,
not merely a tool that was attempted.

## Finding a document by what is in it

Without an index, "find the invoice for the calibration job" forces the model to open
documents one at a time looking for the words. Each one is a round trip, and `MAX_TOOL_STEPS`
caps that at about nine files. Fine for a test folder, useless at two hundred.

`search_files` searches every PDF and spreadsheet in one call and returns the best matches
with a snippet and the tool that opens each one. Text is extracted at upload, so a file is
searchable the moment it arrives.

```bash
python scripts/check_search.py              # find one document among 40 decoys
python scripts/check_search.py --rebuild    # rebuild the index from scratch
```

It is keyword search, and the result payload says so in a field the model reads: these
documents *contain* these words. It cannot compare numbers, amounts or dates. Saying that
plainly is cheaper than letting the model assume otherwise.

**The index is a cache, and four rules keep it one.** This is what makes moving to PostgreSQL
later a change of storage rather than a migration:

1. `search()`, `index_file()` and `rebuild()` are the only interface. Nothing else in the app
   knows SQLite exists.
2. The extracted **text** is stored, not only the inverted index, so migrating is an insert
   rather than a re-parse of every document.
3. The database lives in `index/`, outside `DATA_DIR`, so it is obviously not user data and
   deleting it is obviously safe.
4. `rebuild()` reconstructs it from the files on disk, which makes it disposable by
   construction rather than by intention.

Nothing durable is ever stored there. No tags, no notes, no annotations. The moment something
exists only in that file it stops being a cache.

## Customer database access

Optional and off unless a database is configured. Adds four tools: `list_tables`,
`describe_table`, `run_sql` and `query_to_excel`. The model writes the SQL, which is the
useful part and also the dangerous part, so there are four layers between it and any damage:

1. **The database role should hold `SELECT` and nothing else.** The only layer that cannot be
   argued with, and the only one that is not ours to set.
2. **Every connection sets `default_transaction_read_only`**, plus a statement timeout and an
   idle-in-transaction timeout. PostgreSQL then refuses writes itself, whatever gets sent.
3. **`app/db.py` rejects anything that is not a single SELECT or WITH**, including a second
   statement after a semicolon and a write hidden inside a CTE, which PostgreSQL really does
   allow.
4. **A row cap**, so one careless query cannot drag back a million rows.

**Layer 3 alone would be theatre. Layer 2 is what actually holds.** Layer 1 is what you
should ask the client for. Keep that order in mind before adding a fifth string check.

Two ceilings, not one: `DB_MAX_ROWS` for what the model sees, `DB_EXPORT_MAX_ROWS` for what
goes into a file. Conflating them is how "save that query as a spreadsheet" silently wrote
200 rows of a 50,000-row table.

Connections are made fresh per call and never pooled. A pool would have to be keyed by
tenant, and a pool key that is wrong once is one customer running queries on another
customer's database.

```bash
python scripts/check_database.py
```

It does not take the account's word for being read-only: it attempts a `CREATE TEMP TABLE`
and expects to be refused, reports which privileges the role actually holds, and confirms the
connection is encrypted.

Credentials live in `.env`, which is gitignored, and nowhere else. `describe_table`
deliberately returns no sample rows: knowing a table has a `diagnosis` column is what the
model needs to write a query, and reading somebody's diagnosis is not.

## The job lane

Chat and image generation behave in opposite ways under load, and that is the whole reason
this exists.

Generating a token reads the model's weights out of memory, so several conversations at once
**share that read** and cost almost nothing extra. Diffusion is the other way round: it does
real arithmetic per denoising step, so two images cost close to twice the time. **One
batches, one queues.**

Put both through the same blocking endpoint and the person asking a question waits behind
somebody's picture. So slow work goes through `app/jobs.py` and the four `/api/jobs` routes.

One worker by default, because the GPU serialises this work anyway and extra workers only
contend for it. The queue is bounded, so a flood is refused with a readable message rather
than accepted and forgotten.

Every job carries a tenant, and it has no default: a job with no owner cannot be constructed.
Another tenant's job is **not found**, not forbidden, because "that job exists but is not
yours" confirms an id someone guessed.

Cancelling a queued job is instant. Cancelling a running one is **cooperative**: handlers
call `report(progress, note)` between steps and that is where the cancellation lands, because
a GPU step cannot safely be interrupted half way through.

```bash
python scripts/check_jobs.py
```

The check that matters is the second one: it starts a three second job, then asks a question
while it runs, and fails if the answer waited.

## Testing

Two layers, answering different questions.

```bash
pytest -q                          # the unit suite
python scripts/check_tools.py      # end to end on real files you can open yourself
```

The suite is **252 test functions across 11 files**, counted 7 Sep 2026; parametrised cases
make the number pytest collects higher. `tests/conftest.py` carries an autouse fixture that
snapshots the real `data/`, `index/` and `control/` directories around **every** test and
fails any test that added something to them. It is per-test rather than per-session because a
session-scoped check reports its failure against whichever test happened to run last, which
names the wrong one.

The gate scripts exercise the real thing: real files, a real model, a real database, real
HTTP. They print `N passed, N failed, N not tested` and return non-zero on any failure *or
any untested check*.

| Script | Proves |
|---|---|
| `check_env.py` | Your OS, GPU and real VRAM number |
| `check_ollama.py` | The model answers over the local REST API |
| `check_tools.py` | Every file tool, on real files, no model |
| `check_search.py` | The index finds one document among decoys, and is disposable |
| `check_agent.py` | The model picks the right tool and the answer is tool-backed |
| `check_jobs.py` | Slow work does not block a conversation |
| `check_database.py` | The role really is read-only, and the caps really cap |
| `check_tenancy.py` | Tokens, revocation, suspension, and no plaintext token anywhere |
| `check_isolation.py` | One tenant cannot reach another's anything |
| `check_services.py` | It came back on its own, running the code that is on disk |
| `check_remote.py` | The door is shut to everyone without a token |
| `check_endtoend.py` | The whole thing, over HTTP, from another machine |
| `check_gateway.py` | What vLLM will really do with `tool_choice` and streaming |
| `check_gateway_isolation.py` | The inference plane cannot reach any tenant's files |
| `check_api_compat.py` | The `/v1` contract the website deploys against has not broken |

Three habits run through all of it and are worth keeping:

- **Three outcomes, never two.** Passed, failed, and not tested. A check that could not run
  is not a check that passed.
- **Never loosen a gate to make it pass.**
- **Write a check by breaking the thing first.** A check that only proves the good path works
  is not a check.

## Documentation

| File | What it is for |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | The system, for someone who did not build it. Start here |
| [`docs/runbook.md`](docs/runbook.md) | Start, stop, roll back, read logs, publish it, and what breaks |
| [`docs/models.md`](docs/models.md) | What is served, what it was measured against, and what it is pinned to |
| [`docs/api/gateway-v1.released.json`](docs/api/gateway-v1.released.json) | The frozen `/v1` contract. Do not edit by hand |
| [`docs/licences.md`](docs/licences.md) | Every dependency's licence, its source, and the date it was read |
| [`docs/plans/`](docs/plans/) | What was decided, what was rejected, and what it cost |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed, including model changes and licence findings |
| [`HANDOVER.md`](HANDOVER.md) | Session notes, so a new context can pick this up |

The commit history is unusually informative here. Messages record what was rejected and why,
not only what changed. When a line looks arbitrary, `git log -S` on it is usually faster than
reasoning about it.

## Design rules

Two habits that cost nothing now and keep the Ubuntu move a copy rather than a rewrite:

1. Every filesystem path goes through `pathlib`, never a hardcoded `D:\...`.
2. Every setting lives in `.env`, never baked into the code.

Two that matter more than usual here:

3. This app writes real files. Paths supplied by the model go through
   `config.resolve_in_data_dir`, which rejects anything outside the **current tenant's**
   folder instead of trusting it.
4. **An unset value is an error, never a fallback.** No default tenant, no default database,
   no guessing which customer a request belongs to. Everything else in the isolation design
   is downstream of this one.
