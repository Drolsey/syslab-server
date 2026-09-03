# syslab-server

A personal assistant that runs on your own desktop, reads and writes PDFs and
Excel files, and answers from your laptop wherever you are. No cloud model, no
public URL.

The desktop does all the work and never leaves the house. The laptop reaches it
over a private network that only your own devices can see.

```
laptop  --Tailscale-->  [ FastAPI front door ]
                              |
                        [ Ollama / Qwen3 ]  decides which tool to call
                              |
                        [ PDF / Excel tools ]  plain Python functions
                              |
                        [ your files, one folder on disk ]
```

## Stack

| Piece | Choice | Why |
|---|---|---|
| Model runtime | Ollama | Loads the model on the GPU, exposes a local REST API, same commands on Windows and Linux |
| Model client | `app/llm.py`, standard library only | One POST to one endpoint. No client library to fall out of step with a fast-moving local server |
| Model | `qwen3:8b` (fallback `qwen3:4b`) | Solid tool calling at a size a 3060 can hold |
| Tools | PyMuPDF, openpyxl, reportlab | Ordinary libraries, testable without the model |
| Web app | FastAPI + one static HTML page | No build step, no framework |
| Remote access | Tailscale | Private tailnet, nothing exposed to the open internet |

## Build phases

Each phase ends at a gate you can actually check. Do not skip one; the next
phase assumes the previous actually worked.

- [x] **00 Verify hardware and OS** - `python scripts/check_env.py`. Gate: you know your real VRAM number and your OS.
- [ ] **01 Install Ollama, pull the model** - `python scripts/check_ollama.py`. Gate: the model answers over the local REST API.
- [x] **02 Write the tools, test them alone** - `python scripts/check_tools.py` and `pytest`. Gate: each works on a real file, called directly.
- [x] **03 Wire the model to the tools** - `python scripts/check_agent.py`. Gate: a question about a real file gets a correct, tool-backed answer from the terminal.
- [x] **04 Give it a face** - `python -m app.main`, then open http://127.0.0.1:8000. Gate: browse from the desktop, upload a file, chat.
- [x] **05 Make it survive a logout** - `scripts/service/install_windows.ps1`, then `python scripts/check_services.py`. Gate: reboot, and it is reachable again untouched.
- [x] **06 Open the door, carefully** - `python scripts/new_token.py`, then Tailscale, then `python scripts/check_remote.py`. Gate: reachable from a phone hotspot, invisible to everyone else.
- [x] **07 Prove it end to end** - `python scripts/check_endtoend.py --url ... --token ...`, run from the laptop. Gate: both round-trips correct, from somewhere else.

## Target machine

Verified 31 Aug 2026 by `scripts/check_env.py`:

| | |
|---|---|
| OS | Windows 11 (x64) |
| GPU | NVIDIA GeForce RTX 3060, 12 GB VRAM, driver 595.71 |
| Model | `qwen3:8b`, with headroom for a 14b later |

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
```

Then edit `.env`. The two settings that matter first are `OLLAMA_MODEL` and
`DATA_DIR`.

## Layout

```
app/           FastAPI app, tool functions, model loop
  config.py    every setting and path, read from .env
data/          the one folder the assistant may read and write (gitignored)
scripts/       one-off helpers, starting with the Phase 00 environment check
tests/         standalone tests for the tool functions
```

## The tools

`app/tools.py`. Plain functions, no knowledge that a model exists. Each returns a
JSON-serialisable dict, which in Phase 03 is fed straight back to the model.

| Function | Does |
|---|---|
| `list_files()` | What is in the data folder, with sizes and dates |
| `read_pdf(filename, pages=None)` | Text of a PDF, optionally just pages `"1-3,7"` |
| `read_excel(filename, sheet=None, max_rows=200)` | Cells of a worksheet, plus the sheet names |
| `write_excel(filename, rows, sheet=None, mode, headers=None)` | Creates a workbook or appends rows. A value starting `=` is written as a live formula |
| `write_pdf(filename, title, body)` | A simple text PDF. Blank lines split paragraphs, `# ` starts a heading |

`list_files` is an addition to the original plan. Without it the model has to be
handed a filename before it can do anything.

Two behaviours worth knowing:

- Failures raise `ToolError` with a plain sentence, not a stack trace, because in
  Phase 03 that sentence goes back to the model as the tool result and it can
  correct itself. Asking for a file that is not there returns the list of files
  that are.
- A formula cell reads back as `None` until Excel has opened the file once.
  openpyxl reads cached results, and a file Excel has never seen has no cache.
  That is the library behaving correctly, not a bug in the tool.

## The loop

`app/agent.py`. One question in, one answer out, with tool calls in between.

1. Send the conversation plus a description of every tool to the model.
2. It either answers, or asks for a tool with arguments.
3. Run the real Python function, append the result as a `tool` message.
4. Send the whole thing back. Repeat until it answers or hits `MAX_TOOL_STEPS`.

`agent.ask()` returns the answer, a `steps` trace of what it actually called,
and the full message list to pass back as `history` next turn.

Read-only tools called together in one turn run concurrently, so "read these
five invoices" costs one wait rather than five. Anything that writes stays
sequential, because two writers racing on one file is a bug nobody enjoys
finding.

The model never touches a file. It only ever asks, and `run_tool` decides.
Every failure inside a tool becomes a sentence in the result rather than an
exception, so the model can read what went wrong and correct itself. That is
why asking for a file that is not there returns the closest matching names.

Three things the loop does that are not obvious, all of them added after a real
session went wrong rather than guessed at in advance:

- **Argument aliases.** Qwen3 reliably calls `write_excel` with `data` and
  `sheet_name` instead of `rows` and `sheet`. `ARGUMENT_ALIASES` remaps the
  names models actually reach for. A key that is already correct is never
  overwritten by an alias.
- **Errors that teach.** When a required argument is still missing, the message
  names what is missing, the full accepted signature, and which of the keys sent
  were not real. A retry then has everything it needs to succeed.
- **A blank reply is not an answer, and neither is a promise.** Models return
  an empty turn after a tool failure, or narrate a plan ("let me start by
  reading the PDFs") and stop. Both get one nudge, on the loop's own budget.
- **Wrong-tool mistakes are answered, not escalated.** Asking to read
  `invoice.xlsx` when only `invoice.pdf` exists returns "that exists, read it
  with read_pdf", because the file was found and only the tool was wrong. A
  wrong sheet name on a single-sheet workbook reads the one sheet and says so.
  Clarifying questions are reserved for real ambiguity, not for things the
  program can work out.

## Running it

```bash
python -m app.main
# then open http://127.0.0.1:8000
```

`app/main.py` serves one page and five endpoints:

| | |
|---|---|
| `GET /` | the page, `app/web/index.html`, one file, no build step |
| `GET /api/health` | model and data folder, for the status dot |
| `GET /api/files` | what is in the folder |
| `POST /api/upload` | one .pdf or .xlsx, size-capped |
| `GET /api/files/{name}` | download |
| `POST /api/chat` | message plus history in, answer plus tool trace out |

The page shows every tool call the model made, so you can see whether an answer
came from reading your file or from thin air. A failed call opens the trace
automatically.

Uploads are treated as hostile input: the browser-supplied name is reduced to
its last path component, stripped of anything exotic, and checked against a
list of extensions we handle. Uploading a name that already exists writes
`report (2).pdf` rather than overwriting `report.pdf`.

## The door

Two locks, and the order they went on matters.

Tailscale decides which *machines* can reach the app: only devices signed into
your account, over a WireGuard tunnel, with no public address for anyone to find.
The token decides which *people* get in once a machine can. One deliberate check
in the app is worth more than trusting a network setting to stay the way you left
it, and this app writes real files.

```bash
python scripts/new_token.py     # writes a strong APP_TOKEN into .env
```

- Every `/api/*` route needs the token. `/` is public so the sign-in form can load.
- Browsers get an HttpOnly cookie from `POST /api/login`, so page javascript
  cannot read it back and plain `<a href>` download links carry it too.
- Scripts use `X-Syslab-Token` or `Authorization: Bearer`.
- Eight wrong tokens from one address and it stops answering for fifteen minutes.
- Comparison is `hmac.compare_digest`, so a wrong guess takes the same time as
  a right one.
- Changing `APP_TOKEN` and restarting signs out every device at once.

**The guard that matters most:** if `APP_HOST` is anything other than loopback
and `APP_TOKEN` is missing or weak, the app refuses to start and says why. There
is no configuration in which this listens on a network without a real token.

Traffic inside the tailnet is plain HTTP, which is fine: Tailscale encrypts
every packet between your devices with WireGuard. The token is never sent over
the open internet.

## Running it as a service

```powershell
# Administrator PowerShell, once
powershell -ExecutionPolicy Bypass -File scripts\service\install_windows.ps1
python scripts\check_services.py
```

Two scheduled tasks, `syslab-ollama` and `syslab-server`, both starting at boot
and restarting up to three times on failure. Sleep and hibernate are turned off
on mains power, because a sleeping desktop cannot answer your laptop.

**Why scheduled tasks and not a real Windows service.** A service runs as
LocalSystem, which has its own user profile. Ollama would look for your pulled
models under the wrong `%LOCALAPPDATA%` and find none, and the virtualenv would
not be where the service expects. These tasks run as *you*, using a logon type
called S4U, which means "as this user, without a login session and without
storing their password". Same profile, same models, and it survives a logout.

`python scripts/check_services.py` reads the machine's boot time and the task's
last run time and tells you plainly whether the app started itself or whether
you started it. Run it after a reboot and it says PHASE 05 PASSES rather than
asking you to reboot again.

**A running service is a snapshot of the code as it was when it started.**
Editing a file changes nothing until you restart the task, and that gap is
genuinely confusing: every check passes and the behaviour is still old. So
`/api/health` reports a fingerprint of the code the running process loaded, and
`check_services.py` compares it against what is on disk and says plainly when
they differ. After changing anything in `app/`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\service\restart_windows.ps1
```

That restarts the web app only. Ollama is left alone unless you pass
`-IncludeOllama`, because restarting it drops the model out of VRAM and the next
question waits for it to load again.

### Turning it off

```powershell
$c = "scripts\service\control_windows.ps1"
powershell -ExecutionPolicy Bypass -File $c -Action status    # what is running
powershell -ExecutionPolicy Bypass -File $c -Action stop      # off until the next restart
powershell -ExecutionPolicy Bypass -File $c -Action disable   # off, and stays off (needs admin)
powershell -ExecutionPolicy Bypass -File $c -Action enable    # back on (needs admin)
```

`disable` is almost always the right one. Nothing is deleted: the tasks, your
token, your files and your settings all stay exactly as they are, and `enable`
puts it back in one command rather than redoing Phase 05. `stop` only lasts
until the machine restarts, because the tasks are still set to run at boot.

While it is disabled your laptop cannot reach the assistant. That is the whole
trade.

Two things it deliberately does not touch. Sleep stays disabled until you say
otherwise (`powercfg /change standby-timeout-ac 30` restores it), and Ollama's
own tray app, installed separately by Ollama, still starts with Windows. Turn
that off in Task Manager, Startup apps, if you want the GPU completely idle.

`scripts/service/uninstall_windows.ps1` removes both tasks. It deliberately
leaves the power settings alone: those are a preference for the machine, not
ours to guess at.

On Linux, `scripts/service/syslab-server.service` is the equivalent, and Ollama's
own installer already provides `ollama.service`. That one file is the entire
Windows to Linux difference the plan warned about.

## Running things on Windows

PowerShell refuses to run `.ps1` files by default, so `.venv\Scripts\activate`
fails with a `PSSecurityException` on a stock machine. Nothing is broken, and
you do not have to change that setting. `run.cmd` uses the project's Python
directly, and batch files are not covered by the policy:

```powershell
.\run.cmd scripts\check_remote.py
.\run.cmd -m pytest -q
.\run.cmd -m app.main
```

The `.ps1` scripts in `scripts/service/` are invoked with
`-ExecutionPolicy Bypass`, which allows that one command without changing
anything permanently. That is why those worked while `activate` did not.

If you would rather have `activate` work, this is the usual one-time change,
and it applies to your account only:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Either way, `py` on its own is the *system* Python launcher and will not have
this project's packages.

`check_remote.py` and `check_services.py` are the exceptions: they import only
`app/config.py`, which has no third-party dependencies, so they run from any
Python. That is deliberate. A script whose job is to tell you why the app is
broken should not need the app's packages to run.

## Proving it works end to end

```bash
# on the desktop, against itself
python scripts/check_endtoend.py

# from your laptop, which is the run that means something
python scripts/check_endtoend.py --url http://<machine>.ts.net:8000 --token <token>
```

It builds a PDF and a spreadsheet containing facts invented for that run, uploads
them, asks a question only the file can answer, has a row appended, then
**downloads the workbook and looks inside it**, so the check does not depend on
anything the model said about its own work. Then it has a PDF written and
confirms the bytes that come back really are a PDF.

`scripts/_fixtures.py` builds those files with nothing but the standard library,
and the checker speaks HTTP with `urllib`, so both files can be copied onto any
machine with Python and run there. The test worth running is the one from
somewhere else, so it must not need the project installed there.

A call is not a success: every check that matters looks for a tool step that
returned `ok`, not merely a tool that was attempted.

## Finding a document by what is in it

Without an index, "find the invoice for the calibration job" forces the model to
open documents one at a time looking for the words. Each one is a round trip, and
`MAX_TOOL_STEPS` caps that at about nine files. Fine for a test folder, useless
at two hundred.

`search_files` searches every PDF and spreadsheet in one call and returns the
best matches with a snippet and the tool that opens each one. Text is extracted
at upload, so a file is searchable the moment it arrives.

```bash
python scripts/check_search.py              # find one document among 40 decoys
python scripts/check_search.py --rebuild    # rebuild the index from scratch
```

**The index is a cache, and four rules keep it one.** This is what makes moving
to PostgreSQL later a change of storage rather than a migration:

1. `search()`, `index_file()` and `rebuild()` are the only interface. Nothing
   else in the app knows SQLite exists.
2. The extracted **text** is stored, not only the inverted index, so migrating is
   an insert rather than a re-parse of every document.
3. The database lives in `index/`, outside `DATA_DIR`, so it is obviously not
   user data and deleting it is obviously safe.
4. `rebuild()` reconstructs it from the files on disk, which makes it disposable
   by construction rather than by intention.

Nothing durable is ever stored there. No tags, no notes, no annotations. The
moment something exists only in that file it stops being a cache.

**What would make PostgreSQL the right answer instead:** a second machine (a
SQLite file cannot serve two hosts, though any number of GPUs in one box is
fine), a rebuild slow enough that losing the index is an outage, or wanting one
query that ranks document matches and database rows together. None of those is
about how many cards are in the server.

## Customer database access

Optional and off unless `DB_HOST` is set. Adds three tools: `list_tables`,
`describe_table` and `run_sql`. The model writes the SQL, which is the useful
part and also the dangerous part, so there are four layers between it and any
damage:

1. **The database role should hold `SELECT` and nothing else.** The only layer
   that cannot be argued with, and the only one that is not ours to set.
2. **Every connection sets `default_transaction_read_only`.** PostgreSQL then
   refuses writes itself, whatever gets sent.
3. **`app/db.py` rejects anything that is not a single SELECT or WITH**,
   including a second statement after a semicolon and a write hidden inside a
   CTE, which PostgreSQL really does allow.
4. **A statement timeout and a row cap**, so one careless query cannot sit on a
   production database or drag back a million rows.

Layer 3 alone would be theatre. Layer 2 is what actually holds. Layer 1 is what
you should ask the client for.

```bash
python scripts/check_database.py
```

It does not take the account's word for being read-only: it attempts a
`CREATE TEMP TABLE` and expects to be refused, reports which privileges the
role actually holds, and confirms the connection is encrypted.

Credentials live in `.env`, which is gitignored, and nowhere else. `describe_table`
deliberately returns no sample rows: the model needs to know a column exists,
not what is in it.

## The job lane

Chat and image generation behave in opposite ways under load, and that is the
whole reason this exists.

Generating a token reads the model's weights out of memory, so several
conversations at once **share that read** and cost almost nothing extra. A
batching server gets four to eight times the throughput with barely any latency
penalty per user. Diffusion is the other way round: it does real arithmetic per
denoising step, so two images cost close to twice the time. **One batches, one
queues.**

Put both through the same blocking endpoint and the person asking a question
waits behind somebody's picture. So slow work goes through `app/jobs.py`:

| | |
|---|---|
| `POST /api/jobs` | `{kind, params}`, returns **202** and an id, immediately |
| `GET /api/jobs` | everything queued, running and recently finished |
| `GET /api/jobs/{id}` | status, progress, queue position, result |
| `DELETE /api/jobs/{id}` | cancel |

One worker by default, because the GPU serialises this work anyway and extra
workers only contend for it. Raise `JOB_WORKERS` only if you pin jobs to a
second card. The queue is bounded, so a flood is refused with a readable
message rather than accepted and forgotten.

Cancelling a queued job is instant. Cancelling a running one is **cooperative**:
handlers call `report(progress, note)` between steps and that is where the
cancellation lands, because a GPU step cannot safely be interrupted half way
through. A denoising loop has exactly that shape, which is why `selftest` is
written the same way and can stand in for one.

**The queue is in memory. Jobs do not survive a restart**, and the status
endpoint says so rather than pretending. Moving this table into PostgreSQL is a
change of storage, not of shape.

```bash
python scripts/check_jobs.py
```

The check that matters is the second one: it starts a three second job, then
asks a question while it runs, and fails if the answer waited.

## Testing

```bash
pytest                        # 21 unit tests, throwaway data folder
python scripts/check_tools.py # end to end on real files you can open yourself
```

## Design rules

Two habits that cost nothing now and make the eventual Linux move a copy of the
folder rather than a rewrite:

1. Every filesystem path goes through `pathlib`, never a hardcoded `D:\...`.
2. Every setting lives in `.env`, never baked into the code.

One rule that matters more than usual here: this app can write real files on
your machine. Paths supplied by the model are resolved through
`app.config.resolve_in_data_dir`, which rejects anything outside `DATA_DIR`
instead of trusting it.
