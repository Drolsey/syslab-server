# Changelog

Notable changes to `syslab-server`, newest first.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/), with one project-specific rule:

> **A model change is a changelog entry.** Swapping the served model, its quantisation or its
> context length changes what the system does, even when no line of code moves. It goes under
> Changed, with the old and new values, the same as any other behavioural change.

Two other things belong here that a code-only changelog would miss: **licence findings**,
because a dependency's terms can change without its version changing, and **anything that
alters an on-disk layout**, because that is what a restore from backup has to match.

---

## [Unreleased]

### Fixed
- **`tenancy.new_id()` was generating tenant ids the rest of the application refuses,
  about one in four.** The id alphabet holds eight digits and the first character was
  drawn from all 31, while `context.VALID_TENANT_ID` requires an id to start with a letter
  — deliberately, so that an id can never look like a number, a flag or a dotfile. So
  roughly a quarter of generated tenants were stored happily by `create_tenant` and then
  refused by `context.set_tenant` on their first request, **permanently and with a 500**.
  Measured before the fix: 50 of 200. Nothing had noticed because every tenant that exists
  was created with an explicit id, and Step 4's retrieval plane is the first thing that
  would have created one without.
  - **The deeper fault was where the check lived.** `create_tenant` validated an id a
    caller chose and skipped the one it generated itself, so the single id in the system
    nobody checked was the one the system made. It validates both now. A generator is not
    more trustworthy than a caller; it is only closer to home.
  - Found by `tenancy.resolve_alias`, which validates the local id on the way out as
    defence against a hand-edited control plane, and started returning `None` for a tenant
    that was plainly there.
- **A bearer token holding one non-ASCII character was a 500, not a 401**, on both the
  inference plane and the local plane. `hmac.compare_digest` **raises** `TypeError` on a
  `str` with non-ASCII in it, so `Authorization: Bearer ünicode` reached a traceback from a
  caller who had not authenticated — and header values are bytes on the wire, latin-1
  decoded by Starlette, so it is something a real client can send even though httpx will
  not build one from a `str`. Every plane now compares through `config.tokens_equal`, which
  compares the UTF-8 bytes: the constant-time property is kept, and a malformed token is
  simply a wrong one.
- **`scripts/check_isolation.py` was writing into this install's own `derived/` folder**
  while its docstring promised it never touches anything of yours. It redirects the data,
  index and control roots into a temporary directory; Step 2 added a fourth it had never
  heard of, so it had been creating real `derived/alpha/` and `derived/beta/` since 2.1.
  All four are redirected now, and the assertion checks all four rather than checking one
  and trusting the rest. Second gate in two sub-steps found checking an incomplete list.
- **Derived artifacts outlived the files they came from.** Nothing swept them, so a deleted
  document kept a manifest row and a folder of bytes for ever; `scripts/check_search.py`
  had been leaving 41 text artifacts behind on every run. `ingest.rebuild()` now reconciles
  before it produces, and reconciles both the manifest and the folder — output written by a
  run that died before recording itself was the case that only sweeping rows would miss.
- **`scripts/check_gateway_isolation.py` was checking an incomplete list.** It names every
  module that can reach tenant storage, and Step 2 added three it had never heard of. A
  gate built from a list is only as good as the list, and nothing says when the list has
  fallen behind. 16 checks to 24.
- **A tenant delete left `derived/<tenant>/` behind**, holding text extracted from that
  customer's documents. `scripts/tenant.py` removes it now — deleted rather than moved
  aside even when the documents are only moved, because it can be made again from them.
  Found by the test suite's folder guard the moment Step 2.1 made an upload produce
  artifacts.
- **The ingestion pipeline's freshness check used a one-second mtime tolerance**, carried
  over from `search.stale()`, whose consequences are not comparable: that one produces a
  suggestion, this one decides whether derived data may be served for bytes that no longer
  exist. A file rewritten at the same size within the same second was served stale.
  Exact now.

### Added
- **The tenant bridge** (`app/plane.py`, `tenant_alias`), Step 4.0. A foreign tenant id
  becomes one of ours through an explicit table and nothing else — no regex over another
  system's primary key, no hash of it, no "sanitise it and hope". The rule it exists to
  keep is the sharpest one in the project: **a foreign id must never become a filesystem
  path.** It reaches exactly one function, `tenancy.resolve_alias`, as a bound lookup key;
  it never reaches `validate_tenant_id`; and what reaches `context.set_tenant` is the local
  id that came back out of the control plane.
  - **An unlinked id is 404, never 403**, and so are an id linked to a disabled tenant, an
    id linked to a tenant since deleted, and an id that could not be an id at all.
    `../../etc/passwd` is not refused as malformed — it is simply an id nothing linked, and
    a distinct error for a malformed one would tell a stranger which of their guesses had
    the right shape.
  - **The external system comes from the token, never from a header.** `RETRIEVAL_TOKENS`
    in `.env` is `system:token` pairs rather than the flat list `GATEWAY_TOKENS` is,
    because a foreign id only means anything inside one system's namespace: a caller that
    could name its own system could resolve ids in another's, and the
    `(external_system, external_id)` key would be decoration.
  - **On-disk layout, new:** `tenant_alias` in `control/control.sqlite3`. `SCHEMA_VERSION`
    stays at 1 on purpose — it guards against an older build opening a schema it would
    misread, and an additive table nothing older references is not that. Bumping it would
    turn a code rollback into a control plane that refuses to open, and the control plane
    is the one directory here that cannot be rebuilt.
  - `scripts/tenant.py alias link|unlink|list`. Nothing in the app writes to the table: a
    link decides whose documents an id can reach, so it is a deliberate act by a person.
    Re-pointing a live alias is unlink-then-link rather than an update, for the same reason.
  - The `/api/v1` router is mounted **with no routes yet**, which is the point of this
    order. `POST /api/v1/retrieve` arrives in 4.5 onto a router already wired and already
    authenticated, exactly as Step 2.0 landed its pipeline before its first producer.
- **`scripts/check_isolation.py` § 7b, the retrieval plane** — 38 checks to 54, and
  section 9 grew one more. Each was written by breaking the property first, and **two of
  them were decoration until that run**: one read the `ContextVar` in the wrong thread, so
  removing the teardown entirely did not move it, and one never sent the spoofed
  `X-Syslab-System` header it claimed to test. Both are real checks now, and the habit paid
  for itself in a single afternoon.
- **The ingestion contract** (`app/ingest.py`), Step 2.0. One place that decides what runs
  when a file enters the system, so that page images, extracted fields and embeddings each
  become a producer in an existing pipeline rather than a fourth reader of the same PDF.
  A producer declares a name, a version, the suffixes it handles and whether it is slow;
  the pipeline records what was asked for, whether it worked, and against which version.
  **Nothing imports it yet**, which is the point of doing it in this order: the pipeline is
  proven before the first real producer moves behind it (2.1).
  - **On-disk layout, new:** `derived/<tenant>/manifest.sqlite3` and
    `derived/<tenant>/<producer>/<source file>/`, under a new `DERIVED_DIR` (default
    `./derived`). Same rule as the search index and for the same reason — everything in it
    was made out of a file in `data/` and can be deleted and rebuilt. A producer that ever
    makes something which cannot be regenerated does not belong there.
  - **A producer failing is recorded, not raised.** One failed producer does not fail the
    upload, does not stop the other producers, and is not invisible. A missing library is
    the exception: it is an environment fault, reported once and written against nothing,
    because recording it per file is how nineteen good PDFs once came to look like a folder
    of broken documents.
  - Re-runs on a change of source size, mtime or producer version. Deliberately not a
    content hash.
- **`check_isolation` covers derived artifacts** (Step 2.5): a folder and manifest per
  tenant, one tenant's extracted text unreachable as another, a sweep that never crosses
  the boundary, and a tenant removal that takes its `derived/` with it — asserted through
  `scripts/tenant.py` itself rather than a copy of what it does.
- **`scripts/check_ingest.py`** (Step 2.4), the gate for the two properties the rest of the
  design leans on: `derived/` deleted entirely reconstructs from `data/`, and a deleted
  source file leaves nothing behind. `check_search` grew a matching check.
- **`ingest.forget_missing()`** and a folder-level **`POST /api/ingest`** — the rebuild
  affordance the app never had, since `search.rebuild()` was reachable only from a script.
- **`app/intake.py`** (Steps 2.2 and 2.3), the one place that decides what happens to a file
  that has just been written: fast producers in the request, then the index, then anything
  slow to the job lane. Uploads, tool writes and database exports all go through it.
- **Slow producers run in the job lane** (Step 2.3), so an upload is not held open while a
  200 page scan is processed. One job kind for all of them, because a file with three slow
  producers outstanding wants one queue entry that finishes when the document is ready.
- **`GET /api/ingest`**, **`GET /api/ingest/{name}`** and **`POST /api/ingest/{name}`** —
  what has been produced from a document, what is outstanding, and a way to ask for the
  outstanding work without waiting for it. Before this, "is this document ready" had no
  answer, only a search index row that either existed or did not.
- **The upload response gained `outstanding` and `job`.** `searchable` says the text is in;
  it says nothing about producers still queued behind it. Additive — only `/v1` is frozen.
- **`app/producers.py`** (Step 2.1). Text extraction moved out of `app/search.py` and
  became the pipeline's first producer; `search.index_file` is a consumer of it now, so a
  re-index of an unchanged file no longer re-parses the PDF, and the next thing that wants
  that text reads the same bytes rather than opening the file again for itself.
  `search.SEARCHABLE`, `MAX_TEXT_PER_FILE` and `extract` remain as names on `search`.
- **The inference plane** (`app/gateway.py`), mounted on the same process: an
  OpenAI-compatible `/v1/chat/completions` and `/v1/models`, authenticated by its own
  `GATEWAY_TOKENS` and carrying no tenant at all, by design. A leaked gateway token costs
  GPU time, not documents — asserted on the source by
  `scripts/check_gateway_isolation.py`, which was verified by breaking the property first.
- **Model aliasing.** Callers ask for `syslab-default`; the real model name never leaves
  this machine, in the response or in any streamed chunk. Changing the served model is now
  a one-line edit here and no change at all in the website's Secret Manager.
- `PUBLIC_MODE` (default off), which withdraws the admin page, the interactive docs and
  the OpenAPI schema behind them. The admin page returns 404 rather than 401: "there is
  nothing here" discloses less than "there is something here that needs a password".
- `scripts/check_gateway.py` — probes what vLLM will actually do with `tool_choice`,
  streaming and usage before any code depends on it. It earned itself immediately; see
  Fixed.
- **A frozen `/v1` contract** (`docs/api/gateway-v1.released.json`) and
  `scripts/check_api_compat.py` to enforce it. The website deploys straight to production
  on a push, so a narrowing here would be found by a customer rather than by a test. Only
  `/v1` is frozen: it is the one surface whose caller deploys separately.
- **Cloudflare Tunnel** as a `cloudflared` service, pinned by digest, behind a compose
  profile activated from `.env` rather than a `--profile` flag — the machine that is meant
  to be public is the one that brings up the tunnel, including on the reboot nobody typed a
  flag on. Outbound only: no port forwarded, no inbound firewall rule, no static address.
- `TRUST_CLIENT_IP_HEADER`, and `docs/runbook.md` — start, stop, roll back, read logs,
  publish it, and the failures that have actually happened on this box.
- `docs/models.md`, the benchmark numbers and the reasoning behind the model choice.
- `docker-compose.yml`, pinning vLLM **by digest**, not by tag.
- A test fixture pinning `PUBLIC_MODE` off for the whole suite. Two tests had started
  failing on one machine and not another, because config reads `.env` at import and `.env`
  is not in the repository. The defect was never those two tests; it was that a test
  result depended on an untracked file.
- `docs/architecture.md` — system overview for an engineer who did not build this. Keeps
  what exists and what is planned deliberately separate.
- `docs/licences.md` — the licence inventory. Every component carries either a primary
  source and the date it was read, or the word `unverified`.
- This changelog.
- `BOOTSTRAP_TENANT` documented in `.env.example`. It was read by `app/config.py` and
  documented nowhere, despite deciding which tenant owns this install's data.

### Changed
- **The served model is now `Qwen/Qwen3-14B-AWQ` at a 16384 context**, replacing
  `Qwen/Qwen3-32B-AWQ` at 8192. Per this changelog's own rule, the values: model
  `Qwen/Qwen3-32B-AWQ` → `Qwen/Qwen3-14B-AWQ` (now pinned by revision `31c69efc`, not just
  by repo name), quantisation AWQ 4-bit unchanged, context `--max-model-len` 8192 → 16384,
  `--gpu-memory-utilization` 0.85 → 0.70.
  **Why a smaller model is an upgrade here:** measured on the box's own tokenizer, the
  system prompt and tool schemas occupy 4,213 tokens before anything is asked, leaving
  3,979 of an 8192 window — less than the 4,506 tokens of a single maximum-size tool
  result. Every fix inside the 32B was a trade; halving the weights was the only one that
  bought back the window *and* the concurrency. Measured on the box: KV cache 5.16 →
  **9.27 GiB**, KV tokens 21,135 → **60,768**, concurrency 2.58x → **3.71x**, free VRAM
  for Steps 5 and 6 ~3.4 → ~9.4 GiB, which un-does the headroom squeeze the 0.85 change
  had created and takes speech back off the CPU. The 4,506-token tool result that started
  this now fits inside 12,171 tokens of room with 7,665 to spare. (The pre-flight
  projection said 4.68x and missed by 21%: `--max-model-len` costs non-KV memory too, not
  just cache — see `docs/models.md`.)
  **Not yet measured: capability.** The 8B failed 3 of 8 `check_agent` scenarios and that
  is why the 32B was chosen; the 32B scores 6 of 8 and `check_search` 8 of 8. Those are
  the gate for this swap and it has not been run against the 14B yet. `docs/models.md`
  § "Down to 14B" has the full table and the `--kv-cache-dtype fp8` fallback.
- **The served model is now `Qwen/Qwen3-32B-AWQ` under vLLM**, replacing `qwen3:8b` under
  Ollama. Per this changelog's own rule, the values: backend Ollama → vLLM v0.28.0
  (pinned `sha256:61fc8a89…`), model `qwen3:8b` → `Qwen/Qwen3-32B-AWQ`, context 8192
  unchanged, `--gpu-memory-utilization 0.70`. On `scripts/check_agent.py`'s real
  tool-calling transcripts this moved 5 of 8 scenarios to 6 of 8, including one the small
  model failed by inventing a SQL query against a database that was never configured.
- **`--gpu-memory-utilization` 0.70 → 0.85.** A behavioural change, so it is recorded here
  with its numbers. At 0.70 vLLM reported 3.0 GiB of KV cache, 12,272 tokens, and maximum
  concurrency of **1.50x** at 8192 tokens per request — one conversation at a time, on a
  machine bought for concurrency. The weights take 18.62 GiB of the 21.95 GiB that 0.70
  allowed. Measured after the change: **5.16 GiB of cache, 21,120 tokens, 2.58x** — 72%
  more concurrency. Less than the ~3.4x projected, because vLLM's own overhead scales with
  the budget (weights and non-torch +1.42 GiB, peak activation +1.13 GiB), so only about
  46% of an increase arrives as cache. `--max-model-len` stayed at 8192 deliberately:
  context and concurrency spend the same cache, and the window is not what hurts.
  Consequence to carry into Step 5: about 3.4 GiB is now free, against the ~3.5 GiB
  Section 13 budgets for embeddings, STT and TTS together. Speech on the CPU is now the
  expected answer rather than the fallback.
- `app/llm.py` speaks OpenAI rather than Ollama's native API. `app/agent.py` was not
  touched: the call signature and return shape were preserved deliberately, and an empty
  `git diff app/agent.py` was the gate for that sub-step.
- `config.OLLAMA_*` split into `LLM_*` (what the app talks to) and `OLLAMA_*` (dev tooling
  only). They had been one setting doing two jobs.
- **Model profiles (`models.toml`) moved from Step 3.4 to Step 5.** The file names five
  model roles and only one exists until embeddings land, so the rule that justifies it —
  an empty value means unavailable, never a silent fallback — has nothing to guard yet.
  Reasoning in `docs/plans/step-03-model-gateway.md` §3.4.

### Fixed
- **A conversation that filled the context window was unrecoverable.** Nothing trimmed
  history, so a long chat eventually sent a prompt larger than the window and vLLM
  refused it — and every following message was larger than the one that had just failed,
  so the only recovery was starting over. The `max_tokens` clamp did not cover this: it
  is on the `/v1` path only and bounds *output*, while this is *input*. `llm.trim_to_window`
  now drops the oldest whole exchanges before every model call until the prompt leaves
  `MIN_REPLY_TOKENS` of room, and `app/agent.py` records each trim as a `trim_history`
  step so a conversation never silently forgets. Exchanges move as a unit — an assistant
  turn with the `tool` messages answering it — because an orphaned tool result is rejected
  as hard as an overlong prompt. An unknown window still changes nothing, and a prompt
  that cannot be trimmed is passed through for the server to reject precisely.
  The token estimator moved from `app/gateway.py` to `app/llm.py` so the trim and the
  clamp cannot drift apart about how full one window is.
- **Every request from the website would have returned HTTP 400.** It sends
  `max_tokens: 32000`; this server runs `--max-model-len 8192`, and vLLM rejects that
  outright before generating anything. `max_tokens` is a reservation out of a budget the
  prompt shares, and a generic OpenAI client has no way to know what this machine serves.
  The gateway now reads `max_model_len` from `/v1/models` and clamps `max_tokens` to what
  is left after the prompt — the same argument as the model alias beside it: the caller
  pins a name so it does not have to track what is behind it. Nothing is clamped when the
  window is unknown or the prompt alone does not fit; both are errors the caller needs to
  see rather than have papered over.
- **The vLLM container could not call a tool at all.** It was started without
  `--enable-auto-tool-choice --tool-call-parser hermes`, and every `tool_choice` value
  except `"none"` returned HTTP 400 — which is the entire feature the website's agent
  depends on. Found by `scripts/check_gateway.py` before any application code trusted it.
- **Qwen3 thinks by default and will spend a whole token budget doing it**, returning
  `finish_reason: "length"` with no answer. Requests now send
  `chat_template_kwargs: {"enable_thinking": false}` unless a caller asks otherwise.
- **A test was writing into this install's real search index**, intermittently.
  `test_another_tenant_cannot_cancel_the_job` submits a job and never waits for it, because
  what it asserts is about the cancel refusal — but a `Lane`'s workers are daemon threads
  with no `stop()`, so the handler ran on after the test body returned and sometimes landed
  after the tenant redirects had unwound. `write_pdf` indexes what it writes, so
  `index/testtenant.sqlite3` appeared in the repository. The conftest guard caught it and
  blamed the *next* test in the file, which is the one thing its docstring says it is
  designed not to do: it cannot see across a thread boundary. The lane fixture now drains
  before teardown.
- **The systemd unit could not have started.** `scripts/service/syslab-server.service` used
  `%i` for the user and the home directory, and `%i` is the *instance* name, which a
  non-template unit does not have — every one of those paths expanded to nothing. It is now
  `syslab-server@.service`, installed as `syslab-server@syslab`. Its `After=ollama.service`
  was stale too; the model is a container now.
- `query_to_excel`'s tool description claimed to be "THE ONLY WAY" to turn anything into a
  file, with nothing saying that a file already in the data folder is not a database
  table. Both models tested hit it. Pre-existing, not introduced by this work.

### Security
- **Fixed:** the two exposures this section previously carried as "recorded, not yet fixed"
  are both closed — unauthenticated `GET /` and `GET /api/docs`, and the unbounded login
  throttle. The page and the docs are withdrawn under `PUBLIC_MODE`, and the throttle is no
  longer a `defaultdict` — merely *reading* an entry created one, so every address that
  ever attempted a sign-in left a key behind forever. It is now a plain dict, swept of
  aged-out clients on each use and capped at `MAX_TRACKED_CLIENTS`, so varying the source
  address cannot grow it without limit.
- **The login throttle would have collapsed into a single bucket behind the tunnel.** Every
  request arrives from the cloudflared container, so eight wrong tokens from anyone would
  have locked out everyone — a rate limit turned into a denial of service against the
  operator, silently, on the day the tunnel went up. `CF-Connecting-IP` is now read, but
  only when `TRUST_CLIENT_IP_HEADER` is on, because trusting it while the port is also open
  lets anyone evade the throttle by varying one header. `X-Forwarded-For` is deliberately
  not read: it is caller-appended.
- `GATEWAY_TOKENS` are deliberately separate from `APP_TOKEN` and from the tenant token
  system: the inference plane is meant to be the cheap credential, and it can only be
  cheap if it is a different value.

### Licence findings
- **PyMuPDF 1.24.10 is AGPL-3.0** or a commercial licence from Artifex. It is the PDF reader
  for the whole search path. The AGPL's network clause can reach a hosted service. No
  decision has been made; see `docs/licences.md`. This predates the current work.
- **Piper's licence changed under the project.** The original MIT repository is archived and
  development moved to a GPL-3.0 fork. Kokoro at Apache-2.0 is therefore the planned
  text-to-speech default.
- **Gemma 4 is Apache-2.0; Gemma 1 to 3 are not.** They use Google's custom terms, which
  require passing use restrictions to anyone you distribute to. The family name does not tell
  you the licence.
- **Tailscale's free plan is non-commercial.** The client is BSD-3-Clause, but the plan terms
  restrict it. Tailscale stays for operator access and will not be the production request
  path.
- **sqlite-vec reads as dual Apache-2.0 / MIT, and the row stays `unverified` anyway.** Its
  repository front page states both licences, but that reading arrived through a
  fetch-and-summarise tool, which is exactly what `docs/licences.md` refuses as a primary
  source. Recording it as verified would make the rule decorative the first time it was
  inconvenient. The licence column now says what to expect; clearing the row is one minute
  of a person opening `LICENSE-APACHE` and `LICENSE-MIT`, and it is wanted before Step 5.
  Two non-licence facts found in the same place and worth having before Step 5 depends on
  it: it is **pre-v1 and says to expect breaking changes**, and it is **brute-force rather
  than ANN** — fine at this project's scale, and a thing to design around rather than
  discover.

---

## Before this changelog

Versioning starts here. Work before this point is recorded in the commit history, which is
unusually detailed, and in `docs/plans/`. The milestones, for orientation:

**Step 1, the ownership boundary — complete.** Every byte the system stores or returns has an
owner, and the code cannot return one owner's byte to another. Added `app/context.py` (the
tenant in a `ContextVar`, with no default) and `app/tenancy.py` (the control plane: tenants,
SHA-256 hashed tokens, per-tenant database rows). Storage moved to one folder and one search
index per tenant, with a migration that runs dry by default and ships an undo manifest.
Ends with `scripts/check_isolation.py`, verified by removing the isolation and confirming 8
of its checks fail. Tenant deletion refuses an active tenant, requires the id typed twice,
refuses the bootstrap tenant, and moves documents aside rather than destroying them.

**The conftest guard.** Twice during Step 1 a test wrote into this install's real data
folders and nothing said so. Every test is now snapshotted around the real `data/`, `index/`
and `control/` directories, and a test that adds anything to them fails by name.

**Step 0, clean baseline.** Fifteen files that had been changed but never committed, gated on
two machines before being committed.

**Earlier.** Document search over PDFs and spreadsheets via SQLite FTS5; four read-only
PostgreSQL tools with a four-layer safety model; an in-memory job lane so slow work never
blocks a conversation; token authentication; the file tools; and the model benchmark harness.

### Not yet started

- ~~**Step 4, the retrieval plane.**~~ **Signed off and started, 11 September**, all six
  decisions as recommended. 4.0, the tenant bridge that blocked it, is built and is above
  under Added. What is left is 4.1 to 4.6, and 4.1 — a committed synthetic corpus and a
  hand-written golden set — is the long one.
- **Per-tenant database credentials.** `tenancy.database_for()` raises for any row it finds
  because no cipher was chosen. The table exists and nothing writes to it. Recorded as
  decision 4.5 in the Step 1 plan.
