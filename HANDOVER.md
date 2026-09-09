# Handover — syslab-server

Rewritten 8 September 2026. A portable copy of the session notes, so a new chat
(or a different tool, or you in three weeks) can pick this up without a recap.
The previous version described a Windows laptop with an RTX 3060 and is
superseded; the commit history has it if you want it.

## Checkpoint — 9 September 2026

Today is scoped to two things, both of which close claims this file currently
makes without proof. The tunnel and Step 2's sign-off are explicitly deferred;
they are not blocked by anything done today.

**1. Finish the local website test, through the app's own provider settings.**
Not `.env.local`. The box is added the way any other model provider is: Settings
→ Model provider → **Custom (OpenAI-compatible)**, then

| Field | Value |
|---|---|
| Base URL | `http://192.168.1.185:8080/v1` |
| API key | one of the box's `GATEWAY_TOKENS` |
| Model | `syslab-default` |

This is the better path and is now the intended one. The key lands in the
workspace secret store, referenced by handle and never returned by any
endpoint — responses carry a four-character hint and nothing more
(`lib/services/model-providers.ts`). It survives restarts, needs no dev-server
bounce, and is the same path a real deployment uses. The env variables are only
a fallback for env-only deployments, and are read **only when the workspace has
no provider of its own** (`lib/services/model-providers.ts:359`), so a
workspace provider wins regardless.

The `custom` preset exists for exactly this: `requiresBaseUrl: true`,
`keyOptional: false`, noted as "any endpoint implementing POST
/chat/completions with tool calling" (`lib/agent/providers/presets.ts:129`).

Press **Test** before chatting. It probes `GET /models` and never spends
generation tokens, and it separates the three failures that look alike from the
UI: wrong key (401/403 → "the provider rejected the API key"), unreachable host,
and a model the provider does not list. It will come back green — the gateway's
`/v1/models` returns the alias as its `id` (`app/gateway.py:171`), so
`syslab-default` matches the catalogue.

Leave `AGENT_MAX_TOKENS` unset so the agent keeps its 32000 default
(`lib/agent/index.ts:29`) and the run proves the server-side clamp through the
website's own code.

Verified before starting, so these are not the cause if it fails: the box
answers on the LAN (`:8080` 401, `:8000` 200), no orphan `node.exe` holds
Next's lock, and nothing listens on 3000/3001.

Order, because each step proves something the next assumes: Test, then chat
once (alias, token, clamp, streaming), then attach the Postgres connection and
ask something real (multi-turn tool calling).

**2. Prove the reboot — DONE, 9 September.** Both `:8000` and `:8080` came
back with nothing typed, and the units are `enabled` rather than merely
running, so it is configuration and not something started by hand:

| | |
|---|---|
| Kernel boot | 11:33:27 +04 |
| App (`syslab-server@syslab`, systemd) | active 11:33:42 — **15s after boot** |
| vLLM (container, `restart: unless-stopped`) | serving ~11:34:21 — **~54s after boot** |
| Serving on return | `Qwen/Qwen3-14B-AWQ`, `max_model_len` 16384 |

It proves more than the version planned this morning would have, because it
ran *after* the model swap: what returns unattended is the new config, not the
one that had been running for a day. Verified against `uptime -s` rather than
trusted — a watcher on the LAN saw the box drop at 07:33:19Z and the boot
timestamp is 07:33:27Z, which rules out the failure mode where a network blip
looks exactly like a reboot from outside.

**Blocked, and not on anything box-side.** The website needs its own Postgres
before it will render at all: users, workspaces and the model-provider secret
store all live there (`lib/db.ts`), reached through `PGHOST`/`PGPORT`/
`PGUSER`/`PGPASSWORD`/`PGDATABASE`, plus an `AUTH_SECRET`. None of it exists in
the checkout. Auth itself is not the problem — `API_AUTH_MODE` defaults to
`open` (`lib/api/auth.ts`), so no OAuth credentials are needed.

The decision waiting is where that database comes from: a throwaway Postgres in
Docker (isolated, nothing touches production, needs the Docker daemon started
on the laptop), or an existing Cloud SQL instance someone is willing to have a
dev server write session rows and possibly schema into. Everything on the
syslab-server side is ready and verified — the gateway answers, the alias
resolves, the clamp works against 16384, and the `custom` preset's Test probe
will come back green because `/v1/models` lists `syslab-default`
(`lib/agent/providers/openai-compatible.ts` takes the catalogue branch and
spends no generation tokens).

**The Access-vs-WAF decision is now settled, by this.** The box is integrated
as an API token — base URL, key, model — because that is the shape the provider
config has. Cloudflare Access authenticates with two extra HTTP headers, and
neither the settings form nor `ModelClientConfig` has anywhere to put one, so
Access in front of `/v1` would 403 the only caller this exists for. The tunnel
therefore uses the **WAF custom rule** instead, blocking everything that is not
`/v1` at the edge, with `/v1` defended by `GATEWAY_TOKENS` and the per-token
rate limit as designed. `requiresBaseUrl`/`keyOptional` on the `custom` preset
are the whole contract; a header field is not part of it.

**Still deferred:** the tunnel itself, and Step 2's five decisions.

## The ESG tool: the model calls it reliably and fills the wrong parameter

9 September, testing `generate_esg_report` — the first custom tool here that
is not `run_sql`, and the question was whether the 14B can drive one.

**It can. What it gets wrong is which argument.** Over 8 hospitals x 3 runs,
asking for a single hospital by its full name:

| | correct |
|---|---|
| tool description as written | **14 of 24** |
| example group values replaced with a rule | **24 of 24** |

The tool itself never failed — it was called every time, produced real PDF and
Excel, handled `March 2026` correctly, and handled group scope correctly. The
failure is entirely in parameter choice: asked for one hospital, it passed
`hospital_group` instead.

Failures were hospital-specific and stable, not sampling noise: Kloof,
Midstream and Muelmed missed 3 of 3; Nelspruit, Morningside and both Life
hospitals hit 3 of 3; Highveld 2 of 3. The pattern is familiarity — hospitals
whose distinguishing word is less recognisable collapse onto the example.

**The cause is the tool description, and it is lesson 3 again.** It read
"real groups include values like 'Life Healthcare' and **'Mediclinic'**, not
the hospital's own name". The model filled the parameter with the example it
was handed. Replacing that with a rule — *a name containing the word
"Hospital" is a hospital_name* — is 24 of 24, and group requests still route
correctly. Same shape as the ```sql entry in `OUTPUT_FORMAT`: a concrete
example reads as a slot to fill. That is now measured on two unrelated tools.

**Every failure is silent**, which is what makes it worse than a wrong answer.
The report generates, names itself `esg-report-Mediclinic-2026.pdf`, and the
model's prose agrees with the filename. A request for one hospital returns a
group report covering 854 items instead of 175, and nothing reports an error.

### Two things about the deployment that this turned up

**`Report` is not a Prisma model, and the app expects it to already exist.**
`prisma/schema.prisma` has Company, User, AppDocument, ItemSustainability,
Inventory, Hospital and AppSecret — no Report. `lib/services/esg-report.ts`
joins `"Report"` to `"Inventory"` on `"ID"` through `prisma.$queryRaw`, against
the app's own database, so ESG generation needs a table no migration creates.
On a fresh local database it fails with `relation "Report" does not exist`,
which is what "create an esg report" returns here. Not a bug — the table is
externally managed — but nothing in the repo says so, and there is no seed
script for it or for Inventory, Hospitals and item_sustainability, which exist
locally but empty.

**The production database is not the client database.** Generating the same
report — Mediclinic Muelmed, March 2026 — from the deployed website and from a
local copy of the client instance gives different totals:

| source | Sale | Donation | Scrap | items |
|---|---|---|---|---|
| client `medi_merchant`, and a faithful local copy | 11 | 2 | 1 | 14 |
| the deployed website's own database | 11 | 5 | 1 | 17 |

Sale and Scrap agree exactly; production carries three Donation rows the client
instance does not. So the deployment reads a fuller or newer dataset than the
credentials handed over for this work reach, and any figure reconciled between
the two will disagree for that reason before any other.

### What the local test rig is, so nobody mistakes it for real

A fixture: 2,436 real Report rows copied from the client instance (2026, eight
hospitals), with Inventory, Hospitals and item_sustainability synthesised
around them, since those exist in neither reachable database. The weights and
emission factors are invented.

The pipeline says so itself, which is worth recording as a point in its favour.
`weight_source` is only credited as `ewaste_sheet` or `ai_estimated`
(`lib/services/esg-report.ts:282`); the fixture's value falls through to
`unmatched`, so the report renders "Unmatched item / Unacceptable" and a Carbon
Data Quality Score of **0.0%** against production's 100%. It declined to vouch
for numbers built on data it could not match rather than presenting them as
sound.

**Still not done: the Claude comparison.** This laptop has one provider, the
box. The deployed site clearly has an Anthropic key, but that is the
deployment's, not one available here, so the two are not yet comparable on
equal footing. Comparing a report generated from production against one
generated from the fixture compares two databases, not two models — both
models made the same, correct tool call for that case. The 24-run harness is
built and repeatable the moment a key is added here.

## The client database is attached, and the retry's trigger is narrower than recorded

9 September. Checkpoint step 3 done: `public."Report"` attached as a real
`engine: postgres` connection, `allow_writes` false, and asked real questions.
Every claim below is from the query log in `app_documents`, not from the screen.

**The connection.** `conn_01M22WFDF54ZC2J2K1W5Y3K1HA`, probe `connected` at
1296 ms, introspection returning three tables — `public.Report` with 35 columns
and the two `pg_stat_statements` views, exactly as this file describes. **All 35
of the 35 columns need quoting**, confirmed programmatically rather than by eye.
Row count is now **52,410**, up from 52,190 on 3 September; ordinary growth in a
live table, not a discrepancy.

**Quoting costs two round trips, and the model does self-correct.** Turn 1
("how many records") took three attempts, all three in the query log:

| attempt | sql | result |
|---|---|---|
| 1 | `SELECT COUNT(*) FROM public.Report;` | `relation "public.report" does not exist` |
| 2 | `SELECT COUNT(*) FROM Report;` | `relation "report" does not exist` |
| 3 | `SELECT COUNT(*) FROM public."Report";` | 1 row in 258 ms |

Unquoted identifiers fold to lower case and the error says so, which is enough
signal for the model to fix it unaided. It is a latency cost on this schema, not
a correctness one.

**The fabrication reproduced against real data, on the second turn.** Asked
"which hospitals have the most assets? show me the top 5", the reply had **no
steps at all**, returned in 1.3 s, and contained both a ```sql fence and a
```table block:

```
Hospital A 1200 · Hospital B 1150 · Hospital C 1100 · Hospital D 1050 · Hospital E 1000
```

The query log has nothing between 10:48:13 and 10:50:07. Every one of those
numbers was invented, against a client database it was connected to and had
successfully queried one turn earlier.

Asked again — "actually run that query and give me the real numbers" — it ran
one query, 5 rows in 399 ms, and returned Mediclinic Sandton 1441, Morningside
1418, Limpopo 1346, Donald Gordon 1229, Muelmed 1193. Same question, same
connection, ninety seconds apart: one invented, one real.

**Why the retry did not fire, which is the finding.** This file said the
detector "detects a bare ```sql fence". It is narrower than that.
`bareSqlFence` (`lib/agent/index.ts:142`) strips leading whitespace and tests
`startsWith("```sql")`, and it is consumed by a **streaming hold**: the first
delta that cannot still become the fence sets `holding = false`
(`lib/agent/index.ts:262`), and the retry at line 316 requires `holding` to
still be true. So the fence must be **the first thing in the reply**.

Turn 1 opened with the fence and the retry fired — the step
"Query was shown but not run — running it" is in the run. Turn 2 opened with
"To determine which hospitals have the most assets, we can count…" and the hold
was released on the first delta, so the fence and the fabricated table that
followed were never examined.

This sharpens yesterday's entry rather than confirming it. The conclusion there
was that the dangerous form "does not involve SQL at all". Today's turn 2
contained a perfectly ordinary ```sql fence and still slipped through — **one
sentence of preamble is enough to defeat the detector**. That is a much cheaper
failure than the chart-only case, and it means the retry's real coverage is
"replies that open with a fence", which is a small subset of the replies it was
believed to cover.

It also narrows the open decision. Extending the detector to "asserted a
quantity without a query" is still the general fix and still carries the
false-positive cost. But moving the check off the streaming hold — scan the
completed reply for a fence anywhere, instead of requiring it at position zero —
is a strictly smaller change with no new false positives, since a reply that
contains a ```sql fence and called no tool is exactly the case the retry was
already written for. Worth doing regardless of how the larger question lands.

**Two things learned about the harness, both of which cost time here.** The API
resolves an unauthenticated caller to tenant `ten_local` while the browser
workspace is `cmp_fb6a6edde0af2c3c38d0` (`lib/api/auth.ts:137`), so a connection
created by plain curl lands in a tenant with no model provider and every run
returns the "No model provider is configured" rendering demo — which itself
emits a fake ```sql block and a fake ```table, and looks exactly like the bug
being investigated. Authenticate first: the `auto-link` provider takes an email
and the company name and yields a session cookie. And `/api/v1/queries` is
POST-only; the query log is read from the `queries` collection in
`app_documents`, not over HTTP.

## Session log — 8 September 2026, evening

Stopped mid-way through a local end-to-end test. Everything on the server side
is done and verified; what is unfinished is a test harness on the laptop.

**Shipped and verified today** (`022761a`, `ac88908`, `837e22e`, `1f3fab4`):

- Step 3.5 built: frozen `/v1` contract + `check_api_compat.py`, `cloudflared`
  in compose behind an `.env` profile, `TRUST_CLIENT_IP_HEADER`,
  `docs/runbook.md`, the systemd template unit.
- **The `max_tokens` clamp**, which was a hard blocker nobody had seen. The
  website sends `max_tokens: 32000`; the server runs an 8192 window; vLLM
  rejects that outright. Every message from the website would have been an
  HTTP 400. Verified through the whole path on the box: alias preserved, real
  model name never returned, `finish_reason: stop`.
- **`--gpu-memory-utilization` 0.70 → 0.85.** Concurrency 1.50x → **2.58x**,
  KV cache 3.0 → 5.16 GiB. Less than projected because vLLM's overhead scales
  with the budget; see `docs/models.md` for the numbers and what it means for
  Step 5's headroom.
- **The app now runs under systemd**, `syslab-server@syslab`. Confirmed
  serving from another machine: `/v1/models` 401, `/api/health` 401, `/` 404
  (`PUBLIC_MODE=true` is already set in the box's `.env`).

**Not yet done: the reboot test.** `sudo reboot`, then both `:8000` and `:8080`
should answer with nothing typed. vLLM's persistence is proven; the app's
is not. *(Done 9 September — see the checkpoint above. The app returned in 15
seconds.)*

### The local website test — what it is and where it stopped

The point: `database-agent` can run on the laptop and talk to the box over the
LAN at `http://192.168.1.185:8080/v1`. That exercises the real UI, real
multi-turn tool calling and the real clamp — everything the Step 3 gate asks
for except "from Cloud Run", which needs the tunnel. Worth doing first because
it separates "does the integration work" from "does the network path work".

Done on the laptop: `npm install`, `npx prisma generate`. The dev server is up
and serving (`GET / -> 200`).

> **Corrected 9 September: that `200` was not a working page.** There is no
> `.env` or `.env.local` in the `database-agent` checkout — only `.env.example`
> — so the app has no `AUTH_SECRET` and no Postgres connection, and NextAuth
> renders its "problem with the server configuration" page. That page returns
> **HTTP 200**, so a status-code check passes while nothing works. The website
> has therefore never actually run on this laptop, and the note below that the
> only missing input is a gateway token is wrong: see the checkpoint entry for
> what it really needs.

Two traps hit, both worth knowing before repeating this:

1. **npm 11 blocks package install scripts by default.** `npm install`
   succeeds with exit 0 and a warning, then `next dev` dies with
   `Cannot find module '.prisma/client/default'`. The fix is
   `npx prisma generate`. The warning names five packages; Prisma is the one
   that matters.
2. **`next dev` survives having its shell killed.** It leaves an orphan node
   process holding Next's lock file, so the next start says "Another next dev
   server is already running" and picks port 3001 while nothing listens on
   3000. `taskkill /PID <pid> /F`, and delete `.next/dev`.

**Next action** *(superseded — the workspace-provider path replaced this, and
the real blocker is the database below, not this file)* —

```
MODEL_PROVIDER=custom
MODEL_BASE_URL=http://192.168.1.185:8080/v1
MODEL_API_KEY=<one of the box's GATEWAY_TOKENS>
AGENT_MODEL=syslab-default
```

Deliberately no `AGENT_MAX_TOKENS`, so it keeps its 32000 default and the run
proves the clamp through the website's own code. Restart `npm run dev` after
writing it — Next reads env at boot. Then chat (proves alias, token, clamp,
streaming), then attach the Postgres connection and ask something real
(proves multi-turn tool calling).

### One decision still open, before the tunnel

**Cloudflare Access cannot be used in front of `/v1` as the plan assumed.**
Access service tokens travel as `CF-Access-Client-Id` / `CF-Access-Client-Secret`
headers, and the website's `ModelClientConfig` is `{apiKey, baseUrl, model}` —
three fields, no way to add a header (`lib/agent/providers/types.ts`). Access
would 403 every request before it reached the box.

Recommended instead: no Access on the AI hostname, and a **WAF custom rule**
blocking everything that is not `/v1` —

```
(http.host eq "ai.<domain>" and not starts_with(http.request.uri.path, "/v1"))
```

That gives the property Section 8 wanted from Access — a scan never reaches the
app — without a header the website cannot send. `/v1` stays defended by
`GATEWAY_TOKENS` and the per-token rate limit, as designed. A second hostname
with real Access on it covers browser admin access if that is ever wanted;
Tailscale already covers it today.

The alternative is adding a headers field to the website's provider config.
About twenty lines, but it ends "unmodified provider code" as the step gate and
puts an Access secret into a config surface that holds one credential today.

### Also corrected today

An earlier instruction in this session said to bind the app to `127.0.0.1` to
protect port 8080. **That would break the tunnel** — `host-gateway` resolves to
the Docker bridge address, not loopback, so cloudflared could not reach a
loopback-only app. The right move is a firewall rule scoped to the bridge
subnet, and it belongs after the tunnel works, not before.

## The retry works, and the fabrication moved somewhere it does not look

9 September, after the `tool_choice` retry went into `database-agent`. The
original failing turn now runs a real query and returns 100 real rows —
verified in the query log, not by reading the screen. The next two turns then
did this:

```
chart block   "data": [15, 20, 10]
report        Free 15 · Pro 20 · Enterprise 10
query log     no query ran for either
```

Invented, and they sum to 45 against the 100-row table the model had just
displayed. The retry did not fire because it detects a bare ```sql fence, and
these turns emitted a ```chart block and prose. **The dangerous form of this
bug does not involve SQL at all** — a chart and a formatted report read as far
more authoritative than a code block, and nothing in the app distinguishes a
figure that came from a query result from one that did not.

The general shape of the defect is "asserted a quantity without a query in this
turn". `bareSqlFence` only covers the narrowest instance of it. Extending the
detector to "no tool call, tools available, and the reply asserts figures" would
cover the chart and the report, at the cost of false positives on answers that
legitimately cite a result from earlier in the same conversation — which the
prompt explicitly allows. That trade has not been made yet.

Two smaller things from the same session: "create a graph" makes the model reach
for ```mermaid `graph TD` and draw a diagram of the *column names*, because
`OUTPUT_FORMAT` describes mermaid as "a diagram, for relationships and flows"
and never says "not for data" — the following turn produced a correct ```chart,
so the word "graph" is doing the steering. And `generate_esg_report` is wired
unconditionally in `runs.ts` but is hard-bound to the ESG pipeline over this
app's own Report/Inventory tables, so declining to build a PDF about customers
is correct; only the model's stated reason ("I don't have the capability") is
wrong.

## The website talks to the box — and did not call a single tool

9 September. The local website test ran. Both halves matter.

**What worked, and it is most of the gate.** `database-agent` on the laptop,
unmodified, reached the box over the LAN and held an eleven-turn conversation:
provider added through Settings → Model provider (not an env file), `Test`
green, alias `syslab-default` throughout, streaming, 0.3–1.4s per turn, and
everything persisted to Postgres rather than memory. The key landed in the
workspace secret store **encrypted** (`app_secrets.valueEnc`, 96 bytes) with
only `"key_hint": "••••Z0PQ"` in the provider document — the property this
design claimed, now observed.

**What did not happen: any tool call at all.** Zero of ten runs have steps; no
message contains a tool call in any spelling. Asked to "show me all public
customers", the model replied with a markdown ```sql fence as *prose*, which
the UI renders as a "Show query" widget — so it looks like a query ran. Asked
for the data in text, it produced ten names, "Enterprise Customer A" through
"J". **Those were invented.** No query ever executed.

**syslab-server is not the cause, and this was checked rather than assumed:**

| tested | result |
|---|---|
| 14B calls `run_sql`, non-streaming, direct to `:8000` | 4/4 |
| 14B calls `run_sql`, **streaming** | 28 deltas, `finish_reason: tool_calls` |
| Through the **gateway** (`:8080`), non-streaming, `max_tokens: 32000` | `tool_calls: 1` |
| Through the **gateway**, streaming | 28 deltas, `finish_reason: tool_calls` |
| With the website's full system prompt | 4/4 |

Two hypotheses were tested and **both were wrong**: the prompt's "always show
the SQL" instruction does not suppress tool calls (4/4 with it), and neither
does the full `OUTPUT_FORMAT` block.

**The cause is the app's own output contract, and history only loads the gun.**
Replaying the real transcript from the website's own database:

| context for "show me all public customers" | `run_sql` |
|---|---|
| that question alone | **4/4** |
| with the real six prior turns | **0/4** — emits the identical ```sql fence |
| same, minus the two "files" exchanges | 1/4 |
| same, with `tool_choice: "required"` | **4/4** |

The first turns are legitimately tool-free — a greeting, and two questions
about *files*, which this product does not do, so it correctly answered in
prose (the prompt even instructs this: "small talk is not a question ... do
not run a query"). But once several prose turns are in the context the model
imitates itself, and the tool stops being reached for. The degradation is
gradual, not caused by one turn: removing the file exchanges recovers only
1 of 4.

Holding that history fixed and changing only the system prompt:

| system prompt | `run_sql` |
|---|---|
| with "Show the SQL you ran" + "```sql — the query you ran. Always show it." | **0/4** |
| **without those two lines** | **4/4** |
| with them, plus "writing SQL is not running it, never present SQL you have not run" | **0/4** |

So the mechanism is an interaction, and both halves are needed. The prompt's
rendering contract gives the model a **text channel that imitates the tool** —
a ```sql block is what a successful query *looks like* to this UI. In a clean
context the model still calls the tool; once several prose turns are behind it,
it reaches for the cheaper channel that also satisfies the brief. Remove the
contract and it calls the tool again even with the full poisoned history.

**An explicit correction in the prompt does not help** (0/4). A worked format
example outweighs a prohibition sitting next to it.

**Which section, exactly — because the obvious suspect is innocent.** The
workspace playbook also says "Show the SQL you ran", and the workspace has a
"SQL style guide" skill, so the tenant-editable content looks guilty. It is
not. Building the prompt the way `buildSystemPrompt` actually does (core,
output format, detail, connection, playbook, schema):

| prompt | `run_sql` |
|---|---|
| everything, as the app builds it today | 0/4 |
| **minus `OUTPUT_FORMAT`** (playbook still says "show the SQL") | **4/4** |
| minus the playbook only | 0/4 |
| minus both | 4/4 |

Only `OUTPUT_FORMAT` is load-bearing, and the reason is the *kind* of
instruction, not the topic. The playbook says "Show the SQL you ran" — prose,
no template. `OUTPUT_FORMAT` says "```sql — the query you ran. Always show it."
— a worked format specification, which is a slot the model can fill. **A prose
instruction does not create the decoy; a format example does.** The model is
not disobeying "call the tool"; it is filling in the most concrete template it
was handed.

That is lesson 3 of this file one level up: an example in a tool description is
indistinguishable from an argument, and an example in a format spec is
indistinguishable from a deliverable.

**No prompt edit fixes this reliably. Measured across three questions, six
runs each, same poisoned history:**

| change | `run_sql` |
|---|---|
| as shipped | **0/18** |
| ```sql bullet moved to last in the list (text unchanged) | 11/18 |
| ```sql line removed entirely | 12/18 |
| as shipped + `tool_choice: "required"` | **18/18** |

The one-line removal looked like a complete fix at 5/5 on a single question and
is not: "show me the enterprise names" stays at 0/6 with the line gone. Position
in the list matters too — moving the bullet last, changing no words at all,
recovers most of it — which confirms the decoy is about salience rather than
wording, and also shows why no wording is dependable.

**The reliable fix is `tool_choice`, applied conditionally.** Blanket
`"required"` is wrong: it would force a query on "hi", which `CORE_BEHAVIOR`
deliberately prevents. The shape that works is a **retry on detection** — if a
turn produced a ```sql fence and called no tool, re-issue that same turn with
`tool_choice: "required"`. It costs nothing on the happy path, never fires on
small talk (a greeting produces no SQL fence), is model-agnostic, and is 18/18
when it does fire. `lib/agent/index.ts` already loops per turn, so it has the
right shape for this.

It needs a second, smaller change to not lose the query display, and that
change is worth making on its own account. `Markdown.tsx` renders `SQLBlock`
only from a model-authored fence, so today the SQL on screen is the model's
*recollection* of the query, re-typed after the fact — it is not necessarily
what executed. `lib/agent/index.ts` already holds the real statement and a
`query_id` at the point it emits the step. Rendering from that makes the widget
provably the query that ran, for every provider, and removes the model's reason
to reproduce it.

Scoping this to one provider is possible — `app/api/chat/route.ts` resolves the
client and knows which it is — but probably not wanted. The decoy misleads any
model; a stronger one just resists it longer, and the trustworthy-SQL change is
an improvement everywhere.

It also means **the fix is not available to the operator.** `OUTPUT_FORMAT` is
a hardcoded constant in `lib/agent/prompt.ts`, not tenant-editable, so no
playbook edit reaches it. The narrow change is to drop the `sql` line from that
block and render the query from the tool call's own `sql` argument, which the
UI already receives.

**This is not a 14B tool-calling weakness, and it was wrong to file it as one.**
The 14B calls `run_sql` 4/4 direct, 4/4 streamed, 4/4 through the gateway, 4/4
under the full website prompt, and 4/4 with the poisoned history once the SQL
contract is removed. Three other hypotheses were tested and all failed:
`enable_thinking` on vs off (0/4 either way), temperature 0.0 vs 0.7 (0/4
either way), and the prompt wording in a clean context (4/4 either way).

Whether a 32B resolves the ambiguity better is plausible and **still untested**
— but it is now a secondary hypothesis, not the explanation. `check_agent`
cannot see any of this regardless: every scenario opens with a question that
demands a tool and none accumulates tool-free turns, and none of its tools has
a text format that imitates it.

**What to do about it is a website decision, not a gateway one.**
`tool_choice: "required"` fixes it completely but cannot be applied blanket —
it would force a query for "hi", which the prompt deliberately prevents.
Something adaptive is needed, and it lives in `lib/agent/index.ts`, not here.
Worth measuring the 32B against the same replay before concluding the model
swap caused it.

~~**Also still not done:**~~ **Done 9 September** — the client Postgres is
attached and the run is below. It reproduced the fabrication against real data,
and showed the retry's trigger is narrower than this file recorded.

## Deploy verified end to end — 9 September 2026

The 14B swap is live on the box and proven through the app, not just through
vLLM. One request carries the whole proof:

```
GET  /v1/models        -> {"id":"syslab-default"}
POST /v1/chat/completions with max_tokens: 32000
                       -> "content":"ok", "finish_reason":"stop",
                          "model":"syslab-default"
```

That establishes four things that had been claimed separately: the alias table
is current (so the app is running on the new `LLM_MODEL`), gateway token auth
works, the real model name still never leaves the box, and **the clamp is now
clamping against 16384 rather than 8192** — `max_tokens: 32000` is the
website's exact request shape and is an outright vLLM 400 without it.

Startup log, measured: `Available KV cache memory: 9.27 GiB`, `GPU KV cache
size: 60,768 tokens`, `Maximum concurrency for 16,384 tokens per request:
3.71x`.

One process note worth keeping, because it cost several rounds: `systemctl
restart` prints nothing on success, and the app's endpoints all answer 401
without a `GATEWAY_TOKENS` value, so "did the restart take effect" is not
answerable by looking from outside. The two curls above are the answer, and
they belong in `docs/runbook.md` as the after-any-model-change check rather
than being re-derived next time.

## Where it stands

The 5090 box is real and serving. `syslab-server` is now three planes in one
FastAPI process: the **inference plane** at `/v1` (no tenant, its own tokens),
the **local plane** at `/api/...` (this install's own admin surface), and the
retrieval plane, which is Step 4 and does not exist yet.

Gated 9 September: **pytest 381 passed, 1 skipped**, `check_gateway_isolation`
pass, `check_api_compat` pass, `check_remote` 13 of 16 with the public-surface
section all passing, `check_agent` 6 of 8 against the live 32B model.

## The machine

| | |
|---|---|
| Box | Ubuntu, RTX 5090 32 GB, Ryzen 9950X, 60 GB RAM, `192.168.1.185` on the LAN |
| Model | `Qwen/Qwen3-14B-AWQ` under vLLM `v0.28.0`; image pinned by digest, model by revision, both in `docker-compose.yml`. Was the 32B until 9 September — see the context-window entry below for why it changed |
| vLLM | container, port 8000, `restart: unless-stopped`, survives reboot |
| The app | port 8080, from `.venv` on the host, under systemd as `syslab-server@syslab` |
| Alias | callers ask for `syslab-default`; the real model name never leaves the box |

`docs/runbook.md` is the operational half of this file: start, stop, roll back,
read logs, publish through the tunnel, and the failures that have actually
happened here.

## Step 3, the model gateway

Substantially done. `docs/plans/step-03-model-gateway.md` is the detailed log;
the short version:

- **3.0** probed vLLM before trusting it, and immediately found the container
  had been started without `--enable-auto-tool-choice --tool-call-parser
  hermes`, which made every `tool_choice` value except `"none"` return 400.
  That is the whole feature the website depends on.
- **3.1** rewrote `app/llm.py` to speak OpenAI. `git diff app/agent.py` stayed
  empty, which was the gate.
- **3.2** built `app/gateway.py`. Verified live: alias round trip, streamed
  tool calls, multi-turn tool results.
- **3.3** added `PUBLIC_MODE` and bounded the login throttle.
- **3.4** (model profiles) is **folded into Step 5**, where embeddings give the
  profile file a second row to hold. The one part with a caller today — the
  website pins an alias, never a model name — already shipped in 3.2.
- **3.5** is built on this side: the frozen `/v1` contract, `cloudflared` in
  compose behind an `.env`-activated profile, and `TRUST_CLIENT_IP_HEADER`.

## Next, in order

1. **The Step 3 gate, and it is not ours to run.** The website's own provider
   code, unmodified, completing a multi-turn tool-calling conversation against
   `/v1` from Cloud Run. Not a curl — their code, their deployment, their
   network. Everything on this side is ready. The website change is a secret
   value (base URL, gateway token, Access service-token pair), not a container
   change.
2. **Stand the tunnel up.** `docs/runbook.md` § Publishing it. Cloudflare Zero
   Trust, then `COMPOSE_PROFILES=public` and the token in `.env`, then Access
   with a service token in front, and only then `PUBLIC_MODE=true` and
   `TRUST_CLIENT_IP_HEADER=true`.
3. ~~**Prove the reboot.**~~ **Done 9 September.** The app returned on its own
   15 seconds after kernel boot, vLLM at ~54s, both units `enabled`, serving
   the 14B at 16384. The claim this file used to make without proof is now
   made with it.
4. **Step 2, the ingestion contract.** Designed in full in
   `docs/plans/step-02-ingestion-contract.md` and **waiting on your sign-off of
   its five decisions**, which is the thing actually blocking it. The plan's
   dependency order is 0, 1, 3, 2, 4, 5, 6, 7 — Step 3 comes before Step 2
   because it is the step that stops the per-request model bill.

## Known and deliberately not fixed yet

- **History trimming exists now; the tool-result budget is the part that does
  not fit.** Fixed 9 September. `llm.trim_to_window` (`app/llm.py`) drops the
  oldest whole exchanges before every model call until the prompt leaves
  `MIN_REPLY_TOKENS` of room, and `app/agent.py` records each trim as a
  `trim_history` step so a conversation never quietly forgets. Exchanges move
  as a unit — an assistant turn and the `tool` messages answering it — because
  an orphan tool result is rejected as hard as an overlong prompt. Unknown
  window still changes nothing, and a prompt that cannot be trimmed is passed
  through for vLLM to reject precisely, both matching the output clamp's
  existing reasoning. One estimator now serves both paths
  (`llm.estimate_prompt_tokens`), because two would drift apart about how full
  the same window is.

  **What that does not fix, measured on the box's own tokenizer (`/tokenize`,
  9 September), not estimated:**

  | | tokens |
  |---|---|
  | System prompt | 2069 |
  | 12 tool schemas | ~2144 |
  | **Undroppable floor** | **4213** |
  | Window (`--max-model-len`) | 8192 |
  | **Left for the whole conversation** | **3979** |
  | One tool result clipped to `TOOL_RESULT_BUDGET` | **4506** |

  A single maximum-size tool result is larger than everything the window has
  left after the floor — 4213 + 4506 = 8719 against 8192, before the user's
  question, the assistant's tool-call turn, or one token of reply. So
  `TOOL_RESULT_BUDGET` (12,000 characters, `app/agent.py`) was chosen against a
  window it cannot fit in. Trimming contains the damage — the failure is no
  longer terminal for the conversation, because the oldest turns go and the
  next question still works — but that one turn still fails.

  **Decided the same day: serve a 14B instead, and spend the freed VRAM on the
  window.** The three obvious fixes were all trades — cut `TOOL_RESULT_BUDGET`
  and the model sees fewer rows; raise `--max-model-len` and it costs the 2.58x
  concurrency bought on 8 September; cut the 2069-token system prompt and there
  is not enough there to matter. Changing the model is the only one that buys
  back both, because the 32B's weights were what made the window unaffordable
  in the first place:

  | | 32B-AWQ (was) | 14B-AWQ (now) |
  |---|---|---|
  | Weights | 18.00 GiB | 9.29 GiB |
  | Layers → KV per token | 64 → 256 KiB | 40 → 160 KiB |
  | `--gpu-memory-utilization` | 0.85 | **0.70** |
  | KV cache budget | 5.16 GiB = 21,135 tokens | **9.27 GiB = 60,768 tokens** |
  | `--max-model-len` | 8192 | **16384** |
  | Conversation room after the 4,213 floor | 3,979 | **12,171** |
  | Concurrency at that window | 2.58x @ 8192 | **3.71x @ 16384** |
  | Free VRAM for Steps 5 and 6 | ~3.4 GiB (did not fit) | **~9.4 GiB** |

  **Measured on the box 9 September, not projected — and the projection missed
  by 21% (4.68x predicted, 3.71x delivered).** The KV arithmetic was right for
  the third time; the estimate of how much memory would be *left* was not.
  `--max-model-len` costs non-KV memory of its own, and this projection carried
  overhead measured at 8192 into a 16384 run. `docs/models.md` has the full
  post-mortem. The decision still holds comfortably: 2.88x the KV tokens, 1.44x
  the concurrency, 2x the window, and the 4,506-token tool result that started
  all this now sits inside 12,171 tokens of room with 7,665 to spare.

  **The utilization went back down in the same change, and that is not a
  reversal of 8 September.** 0.85 existed to buy concurrency out of the budget,
  because the 32B's weights left nowhere else to get it — and it cost the
  headroom, which `docs/models.md` then recorded as the new constraint, with
  speech on the CPU as "the expected answer rather than the fallback". The 14B
  gives that concurrency back for free, so the 4.7 GiB has no case left. Every
  axis improves at once: concurrency 2.58x → ~4.68x, window 8192 → 16384,
  free VRAM ~3.4 → ~8.6 GiB. Step 5 and Step 6 fit again, and speech does not
  have to leave the GPU.

  Same family, so the tokenizer and the 4,213-token floor do not move. 16384
  rather than 32768 because concurrency and context still spend the same cache,
  and 16384 is where both numbers beat what the 32B gave; 32768 would buy more
  context than anything here asks for and hand back the concurrency for it. Going back to 0.70 also re-widens the boot race
  behind "Available KV cache memory: 0.45 GiB", which 0.85 had narrowed.

  **Projected, but not the way 0.85 was projected and missed.** That estimate
  assumed vLLM's overhead stayed fixed while the budget grew, and it does not —
  3.4x predicted, 2.58x delivered. This projection does not move the budget at
  all: the 8.71 GiB the smaller weights hand back becomes cache against the
  *same measured* overhead already in `docs/models.md` (weights-and-non-torch
  18.62, peak activation 0.33, CUDA graphs 0.81 GiB at 0.70). The KV arithmetic behind
  it reproduces both recorded measurements — 0.70 → 12,288 tokens against
  12,272 recorded, 0.85 → 2.58x against 2.58x recorded. **Still read the
  startup log and record the real numbers**, the way 8 September did.

  **Capability was the open question, and it is now answered: the 14B matches
  the 32B.** Run against the live model straight after the swap, `check_agent`
  **6 of 8** and `check_search` **8 of 8** — the same totals as the 32B, and the
  same two scenarios failing (3 and 7) for the same documented reason, that both
  reach the correct answer by a tool path the assertion does not accept.
  Scenario 7 used `search_files` rather than `list_files` and produced the right
  spreadsheet. Nothing regressed, and the traces are quicker (2.4s against
  3.9s). The `--kv-cache-dtype fp8` fallback on the 32B was not needed.

  Note the numbers above are the **internal** agent path (`/api/chat`, the
  operator UI). The website reaches the model through `/v1` with its own system
  prompt and its own tool schemas, so its floor is its own — but the same
  arithmetic decides it.

- **`check_agent` is 6 of 8.** Both remaining failures reach the correct,
  verified answer by a different valid tool path than the test requires, and
  scenario 7 compares against a fixture left over from an earlier phase that
  has nothing to do with invoices. Worth tightening the assertions; not a model
  or gateway regression.
- **`check_search` reports 8 of 8** where this file used to claim 9 of 9. The
  discrepancy is unexplained and predates this work; find out which is right
  before quoting either.
- **Gateway usage accounting is pass-through only.** The `usage` block reaches
  the caller; nothing here records per-token spend. Wanted before Step 7's
  measurement window, which is when someone asks what the hardware served.
- **Concurrency is unmeasured.** Every number in `docs/models.md` is
  one-request-at-a-time. Continuous batching is the reason vLLM was chosen and
  nothing has yet exercised it.
- **Jobs are in memory** and do not survive an app restart, silently. A named
  deferred step in the architecture plan.

## Four lessons that cost the most, as classes

1. **A tool must accept back exactly what it hands out.** `describe_table`
   handed the model a quoted table name and then rejected that same string.
   Likewise, anything a result *names* — a `query_as` field — will be echoed
   back as an *argument name*. Accept it rather than lecture.
2. **An ordinary psycopg cursor materialises the whole result during
   `execute()`.** A 200-row cap applied afterwards did nothing; the server
   still produced every row and timed out. Fixed with a server-side
   cursor — which moves the error from `execute()` to the first `FETCH`, so
   guard both as one unit.
3. **An example in a tool description is indistinguishable from an
   argument.** "whichever file mentions Meridian" was an illustration; the
   model searched for Meridian unprompted.
4. **An error that dumps 18 filenames drowns the context.** After two such
   errors the model said it had no database access while holding four
   database tools. It had not lost the tools; it had lost sight of them.
5. **Read a return shape, do not infer it from the name.** `run_sql` returns
   rows as dicts keyed by unique column label, not as lists, and a helper
   written against the assumed shape died with `KeyError: 0`. The deliberate
   design is documented in `app/db.py`; the assumption was not checked against
   it.

Three more from Step 3, same class of thing:

6. **A tag is not a pin.** `:latest` and `:v0.28.0` both mean "whatever that
   name pointed at last time somebody ran `up`". Only a digest is a version.
7. **Ask the server what it does before writing code that assumes it.** Every
   Step 3.0 finding — broken tool calling, thinking mode eating the token
   budget, arguments arriving as a JSON string — would otherwise have been
   found by the website, in production.
8. **Test the config, not just the code.** `${VAR:?}` in a compose file behind
   an inactive profile still breaks every other service, because compose
   interpolates before it filters. Reasoning said otherwise; `docker compose
   config` said this.

## The client database

A client PostgreSQL instance on GCP; host, name and credentials live in
`.env`, which is not tracked. One real table, `public."Report"`, holding
**52,190 rows** by `count(*)` on 3 September, with 35 columns of
asset-removal records. `list_tables` shows roughly 49,795, which is
`pg_class.reltuples`, a planner estimate refreshed by ANALYZE. A 4.6 per cent
gap between the two is ordinary staleness and is not a finding. **Every one of
those 35 column names needs SQL quoting** — capitals and spaces throughout. Two
`pg_stat_statements*` entries also appear; those are PostgreSQL monitoring
views, not client data.

## Run these to confirm the state

    py -m pytest -q
    py scripts/check_api_compat.py
    py scripts/check_gateway_isolation.py
    py scripts/check_agent.py                 # needs the model up
    py scripts/check_remote.py
