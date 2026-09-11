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

**Both checkpoint items closed.** The reboot in the morning, the client database
in the afternoon. The day then ran past its scope into the website's agent, and
the sections below are that work: the ESG tool, the Claude comparison, and a
retry that had to be withdrawn after it was measured.

**Tomorrow starts with the tunnel.** It is unblocked, the Access-vs-WAF question
is settled, and nothing found today touches it. `docs/runbook.md` § Publishing
it. The two things waiting on a person rather than on work are Step 2's five
decisions and whether `pr/agent-hardening` is pushed and opened as a pull
request.

## The decoy reproduces in production, on the model that always loses

10 September, reported from the **deployed** website by the owner. Everything on
record until now came from the local replay rig; this is the first production
reproduction, and it needed no rig at all — two chats, differing only in their
first message:

| conversation | result |
|---|---|
| opens with "list me top 5 hospitals from report" | the correct hospitals |
| opens with "hi", then the identical question | invented customer rows |

That is this file's 0/4-against-4/4 finding, reproduced by hand, by a user who
was not looking for it.

**It is fully explained, and every ingredient was already written down.**
Production carries none of the fixes — original prompt, original tool
description, no retry — and `syslab-default` is now wired in through Settings →
Model provider. The unopposed `OUTPUT_FORMAT` decoy, meeting the model that
always loses to it. Nothing here is new mechanism; what is new is that the
combination is *shipped*.

**Why nothing caught it: the Claude pass measured the model, not the
deployment.** The five traps were run against this same unfixed production and
passed, so production looked healthy. It was healthy because Claude does the
work that makes the trap irrelevant, not because the trap was absent. Swapping
the model swaps that away, and the deployment offers nothing underneath.

**This gates the Step 3 integration.** `syslab-default` must not sit behind the
deployed site until the retry ships, because the failure is silent: no error, no
empty state, a result grid carrying a row count and a timing. Nothing on screen
separates that turn from a real one. And the retry alone is not sufficient — its
blind spot is the fabrication relocating to ```chart and prose, which is the
more dangerous form.

**The natural wrong diagnosis, recorded because it cost time.** Two accounts on
two machines behaved differently, and the difference read as an access problem —
same token, same database, one works. It was neither. The gateway token
authenticates to the inference plane and carries no tenant, so a shared token
predicts nothing about data access; the accounts differed only in what was
already in their conversations. **Anything that varies per conversation will
first present as varying per account.**

**The invented rows were the frontend's, not the model's — and that is the
actual bug.** `components/chat/blocks/sql/mockExecute.ts` in `database-agent`
was a stub for the result view, and it shipped. Every field in the report
matches it exactly: columns `["id","name","region","plan","active"]`, ids
`1000 + i`, first names cycling Amina/Chen/Diego, `REGIONS` and `PLANS` cycling
on `i % 3`, `active: Math.random() > 0.25` — which is why the same question
returned 11, then 15, then 13 rows and flipped Hugo from false to true.
`rowCount = 8 + floor(random() * 8)` is 8..15; `delay = 350 + random() * 500`
is the 594 ms, 727 ms, 837 ms and 733 ms on screen. `mockExplain` did the same
for query plans, with invented costs.

So the two mechanisms compose, and only together do they produce what was seen:

| step | what happens |
|---|---|
| context poisoned by prose turns | the model emits a ```sql fence instead of calling `run_sql` |
| the UI renders any fence as an `SQLBlock` | with an Execute button |
| **Auto-run generated SQL** is on | the block runs itself |
| `mockExecute` answers | fabricated rows, a row count and a plausible duration |

The model never claimed those rows — it never saw them. The 10, 16 and 36
output tokens are consistent with a short fence and nothing else, and
`lib/agent/index.ts` accumulates usage across the whole loop, so those totals
are the whole turn. **The earlier reading here, that the model fabricated the
data, was wrong.** It fabricated the *query*; the frontend fabricated the
answer.

**Fixed 10 September** in `database-agent`. `mockExecute.ts` is deleted and
replaced by `sql/execute.ts`, which posts to the real `POST /api/v1/queries`
against the conversation's own connection. A failure now renders as a failure,
truncation is stated rather than implied, no connection says so instead of
inventing rows, and Explain runs a real `EXPLAIN` (allowed by
`lib/connectors/sql-guard.ts`). `tsc`, `eslint` and 227 tests pass.

**Making a mock real turns free calls into expensive ones, and that is its own
bug.** Auto-run fired for every ```sql block in a conversation on mount, and
six times for a single block (remount, StrictMode's double-invoke, and
`connectionId` settling from `""`). Against a browser generator none of that
cost anything. Against the customer's database, reopening a chat with twenty
blocks is twenty queries at once, past the 60/minute limit — which is exactly
the error the owner hit while testing. Auto-run is now limited to the newest
message and guarded against repeats. **Whenever a stub is replaced by the real
thing, count the calls: the call count was free to be wrong, and now is not.**

The `tool_choice` retry is still worth shipping — it stops the model reaching for
the fence in the first place — but it is no longer what stands between a user
and fabricated data.

## Claude passes every trap, and one of the fixes was worse than the bug

9 September, end of day. The five tests were run against the **deployed**
website, which is the right control: production carries none of these changes,
so it is the original prompt, the original tool description and no retry, with
Claude instead of the 14B.

| test | 14B | Claude |
|---|---|---|
| ```sql fence after tool-free prose turns | fabricated, 0 steps | **passed** |
| preamble bypass ("top 5 hospitals") | fabricated, 0 steps | **passed** |
| ESG scope, Kloof and Midstream | 0 of 3 each | **passed**, 1 rep each |
| "create a graph" | mermaid diagram of column names | **passed**, real chart |
| monitoring views | — | **passed**, named them as extension views |

The second row is the strongest: Claude returned Sandton 1441, Morningside
1418, Limpopo 1346, Donald Gordon 1229, Muelmed 1193 — identical to the
verified query-log values — on the question that made the 14B invent
"Hospital A 1200" through "Hospital E 1000".

And it shows *why* it passes: before calling the ESG tool it ran a query to
confirm the hospital's spelling, exactly as the tool description asks, and even
noticed `Life Groenkloof Hospital` as a near miss. The 14B skipped that step
and reached for the example value instead. Claude is not resisting the trap so
much as doing the work that makes the trap irrelevant.

**So all four changes are hardening for weaker models, not production fixes.**
That is the honest framing, and it is a much easier merge to argue.

### The database question is settled, and the earlier retraction was right

Claude reported **52,410** records in `Report` — exactly the count measured
against `medi_merchant` this morning. Same database. The "production is a
different database" claim is now positively disproven rather than merely
doubted, and the 14-against-17 gap is entirely the fixture's 1:1 `Inventory`
assumption. Two more ratios confirm it: production reports 239 items for Kloof
2026 against 175 non-Removal source rows, and 213 for Midstream against 138 —
1.37 and 1.54, consistent with Inventory holding one row per physical asset.

One database, two roles: the agent connects as `database_agent_ai` with SELECT
on `Report` only, while the app's Prisma connection reaches everything.

### The retry was withdrawn, and it is the important lesson of the day

Asked to *write* a query rather than run one, the retry executes it anyway,
overriding an explicit instruction:

| prompt | narrow (original) | wide (the change) |
|---|---|---|
| "Write me a SQL query… **Do not run it.**" | ran it | ran it |
| "How would I query the Report table for…?" | correctly declined | **ran it** |

The defect predates the change; widening the detector made it strictly worse,
1 of 2 becoming 2 of 2. And it is not a weak-model problem — it fires precisely
when a model does the right thing, which is show SQL and call no tool. Claude
answering "here is the query you asked for" is the exact shape it pounces on.

**The commit was dropped from the pull request.** It fixes a bug Claude does
not have, carries the largest surface area of the four (a new SSE event, both
provider adapters, the frontend), and has a demonstrated false positive.
Separating "show me SQL" from "give me data" is the same hard predicate as
"asserted a quantity without a query", still unsolved. The narrow version
already running in production has the milder form of this, which is worth
telling the owner regardless of whether anything merges.

The chase is worth remembering as a class: **a detector widened to close a
false negative bought a false positive that was worse than the bug.** The
original narrow trigger was accidentally doing a second job — suppressing the
retry on legitimate "here is some SQL" answers — and widening it removed that
protection without anyone noticing until it was measured.

### Two more revisions before the PR

**The ESG rule was rewritten to be about intent, not spelling.** The first
version said a name containing the word "Hospital" is always a `hospital_name`.
It scored 24 of 24, but would misroute a group whose own name contains the word
— and the `Hospitals` table is unreadable from here, so that could not be ruled
out. The replacement — use `hospital_group` only when the user asked for a
whole group or chain — also scores **24 of 24**, with group and month scope
intact, and depends on nothing unverifiable.

**The chart emphasis was removed.** The first version of the mermaid fix also
added "any graph, plot or visualisation of query results is this block" to the
```chart entry. That strengthens a format example inside `OUTPUT_FORMAT` —
precisely the mechanism proven to turn a block into a slot the model fills —
on the block already known to get fabricated. The kept version bounds the
mermaid entry only and leaves ```chart untouched.

### What Claude flagged that nothing here had

Unprompted, on "what tables can you see?": the workspace playbook refers to an
`item_sustainability` catalogue and a `Hospitals` table with hospital groups,
and **neither is visible on the agent's connection**. The ESG generator reaches
them through Prisma; the agent cannot. So any question about item weights or
hospital groups fails against a playbook that promises them. That is a live
production gap, it is a grant rather than a code change, and it was found by
asking the deployed system a question rather than by reading the code.

It also over-claimed once inside a single answer — "the table below has all of
them", then in the same message "the first 100 of 156 hospitals (truncated at
100 rows)". It corrected itself, but claiming completeness before disclosing
truncation is a better hardening target for production than anything in the PR.

### Where the branches stand

| branch | state | contents |
|---|---|---|
| `origin/amro-changes` | pushed | 4 commits, 8 files, +202 −7, **includes the withdrawn retry** |
| `pr/agent-hardening` | local only | 3 commits, 3 files, +34 −3, the revised versions |

They are not parent and child: the PR branch was rebuilt from `main` with
reworded commits, so the same three ideas exist in two forms. `amro-changes`
was deliberately **not** force-pushed — it is already on the remote, and it is
the only record of the retry experiment, whose finding is real even though its
fix is not safe. `deploy.yml` fires only on `main`, so neither branch affects
the live service until something merges.

Open: whether a pull request already exists from `amro-changes`. If one does it
proposes the version with the regression and should be closed.

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

**The production and client row counts disagree — cause not settled.** Generating the same
report — Mediclinic Muelmed, March 2026 — from the deployed website and from a
local copy of the client instance gives different totals:

| source | Sale | Donation | Scrap | items |
|---|---|---|---|---|
| client `medi_merchant`, and a faithful local copy | 11 | 2 | 1 | 14 |
| the deployed website's own database | 11 | 5 | 1 | 17 |

Sale and Scrap agree exactly; production shows three more Donation items.

**This was first written as "production is a different database". That was
overstated.** `Report` joins `Inventory` on `"ID"`, and `prisma/schema.prisma`
documents Inventory as "one row per individual physical asset within a Register
transaction" — so one Report row can yield several Inventory rows. The fixture
here forces exactly 1:1, which undercounts. Three extra donation assets in a
real Inventory explains 14 against 17 with no second database involved, and
that explanation fits at least as well as the one originally recorded.

It cannot be settled from this laptop: the `database_agent_ai` role has SELECT
on `Report` and the two `pg_stat_statements` views and nothing else, so
Inventory is unreadable here. Settling it needs one `SELECT count(*) FROM
"Inventory" i JOIN "Report" r ON r."ID" = i."ID"` against production.

**And a related correction: the client database does contain the app's own
tables.** `Hospitals`, `Inventory`, `item_sustainability`, `users`, `companies`,
`app_documents` and `app_secrets` are all present in `medi_merchant`. Earlier
notes here recorded them as absent, because `information_schema` is
permission-filtered and the role cannot see them — absence of privilege read as
absence of table. The gateway and the agent behave correctly; only the
inference was wrong.

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

~~**Still not done: the Claude comparison.**~~ **Done — see the next section.**
It was run against the deployed site rather than here, which turned out to be
the better test: production runs the unfixed code, so it is the same traps with
a different model.

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
**retrieval plane** at `/api/v1/...`, which as of 11 September is mounted,
authenticated and tenant-scoped, and has no routes in it yet — 4.0 built the
door, and 4.5 puts something behind it.

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
- **3.4** (model profiles) is **Step 4.7** as of 11 September, where it becomes
  the registry saying which model fills which role and how a vision or speech
  model attaches. It was folded into Step 5 until then. The one part with a caller today — the
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

   **Caveat added 10 September:** completing the conversation is no longer
   sufficient evidence. `syslab-default` behind the deployed site fabricates
   rows without calling a tool, and the UI renders that as a result grid — so
   the gate passes on the screen while nothing has run. Gate it on the query
   log instead, and treat the `tool_choice` retry as a prerequisite rather
   than a follow-up.
2. **Stand the tunnel up.** `docs/runbook.md` § Publishing it. Cloudflare Zero
   Trust, then `COMPOSE_PROFILES=public` and the token in `.env`, then Access
   with a service token in front, and only then `PUBLIC_MODE=true` and
   `TRUST_CLIENT_IP_HEADER=true`.
3. ~~**Prove the reboot.**~~ **Done 9 September.** The app returned on its own
   15 seconds after kernel boot, vLLM at ~54s, both units `enabled`, serving
   the 14B at 16384. The claim this file used to make without proof is now
   made with it.
4. **Step 2, the ingestion contract. Signed off 10 September, and 2.0 is
   done.** All five decisions taken as recommended. `app/ingest.py` and
   `tests/test_ingest.py` are in, nothing imports them, and the suite is 404
   passed / 1 skipped, up from 381. The plan's dependency order is
   0, 1, 3, 2, 4, 5, 6, 7 — Step 3 comes before Step 2 because it is the step
   that stops the per-request model bill.

   **Step 2 is COMPLETE, 10 September.** Every sub-step 2.0 to 2.5 built and
   gated; the sections below have what each one cost. Suite 381 → 426 passed,
   1 skipped, and `scripts/check_ingest.py` is the step's own gate.

   **What it unblocks is Step 4**, the retrieval plane, which is the consumer
   this pipeline was designed for. **Signed off 11 September, all six decisions
   as recommended, and 4.0 is built** — see the two sections below. Step 5
   (embeddings) and Step 6 (speech) still have no plan, and both now fit in VRAM
   again after the 14B swap, so the constraint recorded in `docs/models.md` is
   stale.

   **The plan was rewritten on 11 September** around seams rather than
   features, and **4.0 and 4.1 are both done** — see the sections below.
   **Next here is 4.2**, the parser and source seam: Docling behind
   `text` version 2, `Source` with `files` moved and not rewritten, and the
   licence work on the models Docling downloads at runtime, which its own MIT
   does not cover. It is now the longest sub-step in the step.

## Step 4 is planned, and the plan found a prerequisite nobody had built

10 September. `docs/plans/step-04-retrieval-plane.md`, to the standard of 0-3:
six decisions with what loses, six sub-steps each with its own gate, risks,
rollback. **PLANNED, NOT STARTED** — it needs sign-off before any of it is
built.

What it is: a tenant-scoped HTTP surface answering **which parts of this
customer's documents bear on this question**, returning passages with offsets
that point into the extracted text, so a caller can check a citation rather
than trust it. Three endpoints already named in `docs/architecture.md` §5, and
underneath them a chunk producer that is an ordinary Step 2 producer.

**The one-line shape: retrievers become additive, the way producers did.**
Keyword search is the first retriever; the vector retriever arrives in Step 5
as a row in a fusion that already works, not as a second retrieval path.

Three things in it are worth knowing without reading it:

- **The step is blocked on a table that does not exist.** `tenant_alias` is
  designed in `docs/architecture.md` §6 and marked PLANNED; the control plane
  holds `tenants`, `tokens`, `users`, `tenant_database` and `meta` and nothing
  else. The website's tenant is a row id from its own schema, and
  `context.validate_tenant_id` turns what it is given into a directory name.
  **Nothing on this plane can be tenant-scoped until the bridge is built**, so
  it is sub-step 4.0 and nothing else can start. An unlinked id answers **404
  and not 403**, following `jobs.Lane.get` rather than re-deciding it.

- **Anthropic's "under 200,000 tokens, skip RAG" does not apply here, and the
  real number is a factor of sixteen lower.** From `docs/models.md` and the
  9 September tokenizer run: 16,384 window, minus a 4,213-token undroppable
  floor (system prompt 2,069 + 12 tool schemas ~2,144), leaves **12,171 for the
  whole conversation**. Retrieval starts paying here at roughly ten thousand
  tokens of documents — perhaps twenty ordinary PDFs. That is the honest
  justification for building it at all.

- **Nothing here has ever exercised retrieval at a size where retrieval
  matters.** The bootstrap tenant is **11 documents, 1,768 characters, about
  505 tokens**, every one of them a fixture written by a gate script. A
  retrieval plane gated against that would pass while being useless. So
  sub-step 4.1 is a committed synthetic corpus and a hand-written golden set,
  measured **before** the chunker exists — a baseline taken after the change is
  not a baseline — and it deliberately includes paraphrase queries **known to
  fail today**, because a golden set the current system passes completely
  cannot show Step 5 an improvement.

The decision most likely to be argued with is 4.4: **retrievers return ranks,
not scores**, fused by Reciprocal Rank Fusion at k = 60. Fusing a single ranked
list is that list in order, so Step 4 ships RRF that provably does nothing and
Step 5 turns it on by appending to a list. That is the 2.1 pattern — build the
machinery, prove it against known-good behaviour, then move the interesting
thing behind it — and 2.1 is the sub-step where the gate caught two real bugs.

## 4.3: there are passages now, and the pipeline learned that producers read each other

11 September. `app/chunks.py` splits a document's extracted text into passages
of about 512 tokens with 64 of overlap, `producers.CHUNKS` writes them to
`derived/<tenant>/chunks/<item>/chunks.json`, and each one carries `start` and
`end` offsets into the **text artifact**. `artifact[start:end] == text`,
exactly. That identity is the whole reason a citation is checkable: *"characters
4,096 to 4,608 of contract.pdf"* can be verified by anyone holding the file, and
a chunk that knows only its own index cannot be verified at all.

**Determinism was the gate, and the reason it was chosen is worth keeping.** A
wrong chunker is loud — somebody reads a passage that starts mid-clause and
says so. A **non-deterministic** chunker is silent: the same document splits
differently on the next rebuild, every `chunk_id` ever issued now points at a
different passage, and nothing anywhere reports it. So the module has no
`hash()`, no clock, no randomness, no set iteration and no network call, and
the token count is integer arithmetic rather than a division by 3.5.

**That last point is an amendment to the plan, and it is made in the open.**
Section 5.4 says tokens are *"counted with the model's own tokenizer where the
box is reachable, estimated at 3.5 characters per token where it is not"*. It
cannot stand beside 4.3's own gate. A boundary decided by a tokenizer that is
*sometimes* reachable is a boundary that depends on whether the GPU box was up
when the document was ingested — precisely the Tuesday the gate forbids. The
estimate is used always. A real tokenizer may inform the constants later; it
may never be asked at chunk time.

### The pipeline had to learn something it did not know

`ingest.producers_for()` sorted producers by name, which was correct for as
long as every producer read the source file and nothing else. The chunk
producer reads **the text artifact** — decision 5.3, so that a document is
never chunked from bytes the index never saw — and `chunks` sorts before
`text`. On a fresh upload the chunker would have run first, found no artifact,
and recorded a skip.

So `Producer` gained `depends_on`, and declaring it buys three things that were
otherwise three separate things to remember:

- **The run order.** Dependency-first, alphabetical among equals, so nothing
  that does not use the field noticed it arrive.
- **The staleness, which is the one the manifest could not have caught.** A row
  records the source's size, its mtime and its *own* producer version. Nothing
  in it says which version of the text artifact the chunks were cut from. Bump
  the text producer and every document re-extracts while every chunk stays
  exactly where it was — offsets into a file rewritten underneath them, with
  the document still reported `ready`. A schema column could have carried it; a
  declared dependency carries it without a migration.
- **The hold-back, which keeps one fault to one report.** A damaged file was
  about to produce two failed rows: the real one from the extractor, and a
  second from the chunker complaining it could not find a text artifact. The
  second sends whoever reads the manifest into the wrong module. A held-back
  producer now writes **no row at all** — not `failed`, which would blame it
  for someone else's fault, and not `skipped`, which would claim there was
  nothing to make — and appears in the report's new `blocked` block, which
  always names what it is waiting for.

### What the break-verification pass found, which reading did not

Thirty tests, and then eight deliberate breaks of the code to confirm each
property actually fails when it is violated. Eight for eight — and two of the
eight were only interesting because they **did not** fail the first time.

- `chunks.SEPARATORS` ended in an empty string, copied from the shape recursive
  character splitting is usually written in. The splitter skips a separator it
  cannot search for and falls through to a hard cut, which is the real floor.
  Removing the row changed no behaviour whatsoever, which is the point: **it
  was a comment pretending to be code, sitting on the line somebody would edit
  next when they went looking for the floor.**
- The ordinal-holes test used a run of 400 newlines — well under the 1,792-
  character target — so the blank lines were always merged in with the prose
  either side and no span was ever empty for the trim to drop. **It asserted on
  a case it never built, and passed against deliberately broken code.**

Neither was findable by reading. Both were findable in about a minute by
breaking the thing the test claimed to protect.

**Gate: 37 of 37** (`scripts/check_ingest.py`, up from 31). Suite **543 passed,
1 skipped**, up from 513. `check_retrieval.py` is **unchanged to three decimals
against the 4.1 baseline** — MRR 0.576 — which is the expected answer, because
4.3 makes passages and does not yet index them. **4.4 is where that number is
supposed to move**, and 4.1's standing prediction is that `clause` moves most:
ANDing nine common legal words inside a 512-token chunk should be far more
selective than inside a fifty-page contract.

## 4.2 is done, and the gate it was told to build found two rows lying

11 September. Two parts. *Part one* put one parser behind many formats
(`app/parse.py`, a suffix → backend table, Docling's **backends** rather than its
`DocumentConverter` — 35 packages and no torch against 85 and a multi-gigabyte
GPU stack). *Part two* built the **source seam** and the gate.

**`app/sources.py`.** `Source` is a name, `list()` and `fetch()`. `files` is
today's behaviour **moved and not rewritten**, the Step 2.1 discipline, and the
gate was that `git diff` on the consumers stays empty — it does. The three
separate sentences `ingest.py` used to contain about local files (`_source`,
`rebuild`, `forget_missing`) now go through one place, so a customer SQL
database or a Drive connector is a new source rather than a second pipeline.

**`active()` refuses a second source rather than picking one**, and that refusal
is the design. The manifest keys on `(source_name, producer)` with **no column
for which source a name came from**, so two sources each holding a
`contract.pdf` would share one row and one folder of derived bytes. The day a
connector lands, the schema change that must come first announces itself instead
of quietly corrupting a manifest. Credentials are the other blocker and are
named in the module: `tenancy.database_for()` still raises for every row because
**no cipher was ever chosen** — Step 1's decision 4.5, still open, and now on the
critical path.

**And then the formats gate found that two of part one's rows were broken.**
`.pptx` and `.md` were in the table with no library behind them. Nothing had
noticed because **docling-slim imports a format's reader when the file is read,
not when the backend module is imported** — so
`docling.backend.mspowerpoint_backend` imports perfectly on a machine with no
`python-pptx`, `require_readers()` passed, and the `ImportError` arrived from
inside `convert()`, landed in `except Exception`, and was written to the
manifest as **`unreadable`**: *"this file could not be read"*, about a file that
was perfectly fine.

That is **the third time** this project has paid for *a missing library reported
as a broken document*. It is structural now rather than remembered:
`Backend.needs` names the package each row actually defers to,
`require_readers()` checks it, `parse._call` converts a call-time `ImportError`
into `ProducerUnavailable`, and `check_ingest` reads one file of **every** suffix
in the table looking for a sentinel *inside* the extracted text. Asserting that
ingestion "succeeded" is exactly what let it through — **a producer that writes
an empty artifact succeeds.**

`check_ingest` went 14 → 31 checks. Suite 485 → 513. Retrieval **unchanged**
against the 4.1 baseline, MRR 0.576 to three decimals, which is the right
outcome for a parser swap.

**PyMuPDF: not retired, and the question is much smaller.** `app/search.py` no
longer reads a PDF at all, so `docs/licences.md`'s *"it is the PDF reader for the
entire search path"* is no longer true. One use remains — `app/tools.py:213`,
rendering page images for `read_pdf` — so the AGPL-3.0 exposure is one module and
one tool, and the pypdfium2 swap that file specifies is a small gated change
rather than a rewrite. The cost is recorded too: the project currently ships
**two** PDF libraries.

**Next is 4.3, the chunk producer**, where determinism is the property worth
gating — a chunker that splits differently on Tuesday invalidates every citation
ever issued.

## 4.1: there is a baseline now, and it found what retrieval is worst at

11 September. `tests/fixtures/corpus/` holds **52 real commercial contracts**
from CUAD v1 with their reference text, a five-file format pack, **42 queries
with verified ground truth** and 4 aggregate questions counted from lawyers'
annotations. ~23 MB, committed rather than fetched. `scripts/check_retrieval.py`
is the gate and `tests/test_corpus.py` guards the fixtures — suite 471 to 485.

**The baseline, taken against `app/search.py` exactly as it is:**

| | queries | MRR | R@1 | R@5 | R@10 |
|---|---|---|---|---|---|
| overall | 42 | 0.576 | 0.458 | 0.611 | 0.685 |
| exact — rare strings | 20 | **0.938** | 0.900 | 1.000 | 1.000 |
| clause — quoted passages | 14 | **0.210** | 0.071 | 0.357 | 0.571 |
| paraphrase | 8 | 0.312 | **0.028** | 0.083 | 0.097 |

Keyword search wins rare strings almost perfectly, which is what it is for, and
loses paraphrases almost completely, which is what Step 5 is for. Both were
predicted. **The middle row was not.**

**THE FINDING: `app/search.py` cannot do phrase search.**

`_terms()` splits a query into words and quotes each one **individually**, which
in FTS5 means "this token", not "this phrase". So a 14-word sentence copied
straight out of a contract becomes nine unrelated tokens joined by AND — and
"This / Agreement / shall / be / binding / Parties / as / date / hereof" each
appear somewhere in nearly every commercial contract. Ten contracts match, bm25
ranks them by frequency, and **the contract the sentence was literally copied
from does not reach the top six.**

Quoting a passage you are holding and asking "where is this from" is the most
natural thing a user does with a document search, and it is the thing today's
retrieval is worst at. Nobody had measured it, which is the entire argument for
taking a baseline before doing the work rather than after.

**Not fixed, and `app/search.py` was not touched.** It is a prediction for 4.4:
ANDing nine common legal words inside a **512-token chunk** is far more
selective than inside a fifty-page contract, so chunk-level retrieval should
move `clause` substantially. If it does not, the chunker is wrong rather than
the theory, and that is a much more useful thing to learn than a number that
drifted for no stated reason.

**Two notes about the numbers themselves, recorded so nobody re-derives them.**

- **MRR is flattered on multi-relevant queries.** The paraphrase set scores
  0.312 MRR against a Recall@1 of **0.028**, because a query with eight right
  answers can hit one by luck. For those queries **Recall is honest and MRR is
  not**, and quoting the MRR alone would make the paraphrase set look half
  solved when it is essentially unsolved.
- **The gate's first run reported 0.000 for everything**, which looked exactly
  like a finding and was a bug: it read hits from `found["documents"]` when
  `search()` returns them under `results`. That is lesson 5 in this file —
  *read a return shape, do not infer it from the name* — now paid for twice.
  The gate says loudly when a query RAISED rather than averaging the silence,
  because a uniform zero is far more likely to be a broken gate than a broken
  retriever.

**The corpus is the ruler and a ruler nobody checks drifts**, so
`tests/test_corpus.py` hashes every contract, re-derives the ground truth for
all 34 single-answer queries from the reference text rather than trusting the
golden file, and asserts the scanned PDF still has no text layer and the corrupt
one still refuses to open. The filenames are `contract_NN.pdf` deliberately:
`app/search.py` indexes a document's name alongside its text, so a fixture named
after its parties would let a party-name query match the **filename**, and the
score would measure the naming rather than the retrieval.

**The aggregate questions are in the report and answered by nothing.** 41 of 52
contracts carry an exclusivity commitment; a search returning 8 can see at most
8. That is decision 5.6's argument as a number, and Step 9 is where it is fixed.

## Step 4: signed off, and the corpus decision got better under challenge

11 September, same day as the rewrite below. **All seven decisions signed off**, 5.1 to 5.6
as recommended, and **5.7 amended — the amendment is the best thing that happened to this
plan.**

The recommendation was to hand-build a 40–60 document corpus and write 25–30 questions with
their answers, and it called that the longest sub-step, the one that would "feel least like
progress". The challenge back was: *"Can we download a small dataset that has this rather
than building it? Surely there has to be something."*

There is: **CUAD v1**, the Contract Understanding Atticus Dataset. 510 real commercial
contracts from public EDGAR filings, as **PDF and text**, with **13,000+ annotations by
lawyers across 41 clause categories**. Stated CC BY 4.0, commercial use permitted.

It is better than what would have been built, not merely faster, and one row is the reason:
**the clause labels give an aggregate question a countable true answer.** "How many of these
contracts carry an exclusivity clause" can be checked against the labels. Decision 5.6 says a
question about *all* of something must not be silently answered from eight passages — and
proving that needs a question whose real answer is known. Hand-writing one means hand-writing
the answer too, by the same person who knows how the retriever works. CUAD supplies both
halves, and lawyers wrote them.

Three caveats, recorded rather than smoothed over:

- **CUAD is an extraction benchmark, not a retrieval one.** Its questions name their own
  contract, which makes retrieval trivial. 4.1 has to *construct* retrieval queries from the
  annotated spans — transcription with ground truth attached, rather than invention.
- **It is legal contracts, not the client's asset-removal records.** The corpus proves the
  machinery and the metrics. Only the client's own documents prove the fit, and those cannot
  live in this repository.
- **510 is too many and PDFs are not small.** 40–60 committed, under ~25 MB, attribution in
  the folder. Committed rather than fetched, because a gate that needs the network is a gate
  that stops running on a laptop.

**Effect: 4.1 stops being the longest sub-step. 4.2 becomes it**, because Docling changes
what the ingestion pipeline reads. Step 4 goes from six-to-eight sessions to **five to
seven**.

**And the licence rule worked in both directions on the same day.** Docling's MIT is now
**verified** — the LICENSE file read and pasted in full by a person, which is a primary
source, unlike the summarised reading that left sqlite-vec unverified. CUAD's CC BY 4.0 came
through a search result and therefore **stays unverified**, exactly like sqlite-vec's, and
blocks committing a PDF until somebody opens it. One rule, applied to a row we wanted to be
true and a row we equally wanted to be true.

**One thing the MIT does not buy, and 4.2 gates it:** Docling **downloads models at runtime**
— layout, table structure, OCR — and those carry their own terms. The library being clear is
necessary and not sufficient. An OCR engine is the likeliest GPL surprise.

## Step 4 was rewritten the day it was signed off, and the reason is worth keeping

11 September, hours after 4.0 landed. The plan was written against the wrong
requirements — not vaguely wrong, specifically wrong — and executing it would
have produced a working thing nobody asked for.

**What it assumed:** text-only PDFs and spreadsheets, one way in, and questions
answerable from a handful of passages.

**What was actually wanted:** customer data in **SQL databases, Google Docs and
other cloud services**; **many formats including media**, with "it seems stupid
to hardcode all data types"; **OCR**; **speech both ways**; and questions like
*"which vendors has this client used across all their contracts, ranked by
spend"* and *"connect the dots across 200 contracts"*. All per customer, in
isolation. Plus the sentence that reshaped the document: **"I want to build the
skeleton that allows for all these systems to be added as features later on."**

**The finding that mattered most, and it is the one to remember.**

**Retrieval cannot answer a question about ALL of something, and it does not say
so.** Ask "rank every vendor by spend across 200 contracts" and retrieval returns
the top eight chunks. The model then produces a confident, well-formatted ranking
**built from 4% of the contracts**. Nothing errors. It is indistinguishable on
screen from a correct answer.

That is not a hypothetical here. This file already records the deployed site
fabricating result rows without calling a tool, *"and the UI renders that as a
result grid — so the gate passes on the screen while nothing has run."* Same
failure class, and the most dangerous one this product has, because the wrong
answer is the one that looks best.

The fix is not better retrieval and no RAG framework supplies it. It is
**structured extraction at ingest time** — a producer pulling `(vendor, amount,
date, document)` into a real table as each contract arrives, answered with SQL,
exact and checkable. `app/db.py` already runs read-only SQL behind four layers,
and `run_sql` is already a tool the model holds. So there are **two answer
paths**, and Step 4's job is to make sure a question needing the second never
gets silently answered by the first: a `coverage` block on every retrieval
response saying how many passages matched against how many were returned.

**What the plan is now.** Not features — **four seams**, each proven by putting
the cheapest possible thing in it:

| Seam | State | What plugs in later |
|---|---|---|
| **Source** | 4.2 builds it, `files` behind it | Customer SQL, Google Docs, Drive |
| **Producer** | **Step 2 already built it** | OCR, page images, extracted fields, embeddings |
| **Retriever** | 4.6 builds it, keyword in it | Vector, structured, graph |
| **Model role** | 4.7 declares it, `chat` filled | embed, vision, stt, tts |

The producer seam working and being gated is the evidence the shape is worth
repeating — it is why "add vision" is now a producer rather than a fourth reader
of the same PDF.

**The blocker nobody had costed.** Every cloud connector needs *that customer's*
credentials stored encrypted, and `tenancy.database_for()` still raises for any
row it finds because **no cipher was ever chosen** — Step 1's decision 4.5, open
since then, `tenant_database` still empty. It is now **Step 7** and it is on the
critical path for everything involving a customer's own systems. It is the most
under-planned thing in the project.

**Step numbers did not move.** 5 is still embeddings and 6 is still speech,
because `docs/licences.md` says "verify sqlite-vec before Step 5" and "verify
Kokoro before Step 6" and a dozen other references agree. A step number is an
identifier, not a position in a queue — this project ran 3 before 2 on purpose
already. New work took new numbers: 7 credentials, 8 connectors, 9 extraction,
10 vision.

**Honest effort.** Step 4 as rewritten is six to eight sessions, against four to
five for the narrow draft. The programme behind it — credentials, connectors,
extraction, vision, speech — is realistically **20 to 30**. That is the number
for what was actually described, and the argument for seams is that each of those
then lands as a registration against a gated interface rather than a rewrite of
the last one.

## 4.0: the bridge is built, and it found a one-in-four bug on the way

11 September. Step 4 is **signed off, all six decisions as recommended**, and 4.0
is done. `check_isolation` **38 → 54 checks**, suite **426 → 471 passed**, 1
skipped.

What exists now: `tenant_alias` in the control plane; `tenancy.resolve_alias`
beside `resolve_token` and fail-closed in the same way; `app/plane.py` holding
the dependency and an `/api/v1` router **with no routes in it**; and
`scripts/tenant.py alias link|unlink|list`, which is the only thing that writes
to the table.

**Two decisions the plan left open and this sub-step had to take.**

- **A service token had to be decided here**, because a dependency with no
  credential is a plane anyone can call. `RETRIEVAL_TOKENS` in `.env`, following
  the `GATEWAY_TOKENS` precedent — but as a **mapping**, `system:token`, so the
  external system comes from the token and never from a header. That is not
  tidiness: a foreign id only means anything inside one system's namespace, so a
  caller able to name its own system could resolve ids in another's, and the
  `(system, id)` key would be decoration. The `kind` column on real tokens that
  `app/config.py` promised to Step 4 is still not built; it is wanted the day a
  service token needs revoking without a restart.
- **`SCHEMA_VERSION` stays at 1** with a new table in the schema. What the number
  guards is an older build opening a schema it would MISREAD, and an additive
  table nothing older references is not that — every control plane converges on
  the next open, in either direction, because the whole schema runs as
  `CREATE TABLE IF NOT EXISTS`. Bumping it would convert a code rollback into a
  control plane that refuses to open, and that is the one directory here which
  cannot be rebuilt from anything. Bump it for a column whose meaning changes;
  not for a table nothing older has heard of.

**The bug, and it is the good kind.** `tenancy.new_id()` was generating tenant
ids that the rest of the application refuses — **50 of 200, measured**. The id
alphabet holds eight digits and the first character was drawn from all 31, while
`context.VALID_TENANT_ID` requires an id to start with a letter. A quarter of
generated tenants would have been stored by `create_tenant` and then refused by
`context.set_tenant` on their first request, permanently, with a 500.

It survived because **every tenant that exists was created with an explicit id**
— `default`, `testtenant`, and the gates' `alpha`/`beta`/`gamma`. The retrieval
plane is the first thing that would have made one without choosing its id by
hand. And the deeper fault was where the check lived: `create_tenant` validated
an id a caller chose and skipped the one it generated itself, so the single id
in the system nobody checked was the one the system made. **A generator is not
more trustworthy than a caller; it is only closer to home.**

**A second, smaller one.** `hmac.compare_digest` raises `TypeError` on a `str`
holding non-ASCII, so `Authorization: Bearer ünicode` was a **500 from a caller
who had not authenticated**, on the inference plane and the local plane both.
Header values are bytes on the wire and Starlette decodes them latin-1, so it is
something a real client can send — httpx simply will not build one from a `str`,
which is why no test had. All three planes compare through `config.tokens_equal`
now, on the UTF-8 bytes.

**And the thing most worth keeping from today.** "Write a check by breaking the
thing first" is in `docs/architecture.md` § 9 and it earned its place again:
**two of the fourteen new checks were decoration**, and the break-it run is what
said so. One read the `ContextVar` in the gate's own thread after a TestClient
call — but the dependency runs in the client's task, whose context copy dies with
the request either way, so deleting the teardown entirely did not move it. The
other never sent the spoofed `X-Syslab-System` header it claimed to test, so a
plane that trusted such a header would have passed. Both are real now; the
teardown one drives the async generator by hand so it runs in a context where a
missing reset is visible.

## Step 2 is complete, and 2.5 found the gate leaking into this install

10 September. `check_isolation` grew a `3b. Derived artifacts` section and a
`9b. Removing a tenant's storage` section — **38 passed, 0 failed, 1 not
tested**, the not-tested one being the Windows symlink privilege that predates
all of this. Five matching tests in `tests/test_tenant_isolation.py`.

**The gate was writing into the developer's own folders, and its docstring
said it could not.** `check_isolation` redirects `DATA_ROOT`, `INDEX_ROOT` and
the control plane into a temporary directory and asserts it has done so. Step 2
gave a tenant a fourth root it had never heard of, so it had been creating real
`derived/alpha/` and `derived/beta/` folders in this install since 2.1 — found
by looking, not by anything failing. The redirect covers all four now, and the
assertion checks all four rather than checking `DATA_ROOT` and trusting the
rest.

**That is the second gate in two sub-steps caught checking an incomplete
list**, after `check_gateway_isolation` in 2.4. Worth keeping as a class: **a
gate built from a list is only as good as the list, and nothing tells you when
the list has fallen behind — least of all the gate, which goes on passing.**
Both were found by hand while doing something else. Neither would have been
found by running them.

**9b needed a tenant of its own.** "Deleting a tenant takes its `derived/` with
it" belongs to `scripts/tenant.py`, because `tenancy.delete_tenant` removes
rows and deliberately stops there. The CLI refuses an id the control plane no
longer holds, and section 9 has already taken Beta's rows — so a third tenant
is created, seeded, disabled and removed through the real CLI. Asserting the
property rather than a copy of it.

**Step 2 is done.** Every sub-step built and gated:

| Sub-step | Gate | Result |
|---|---|---|
| 2.0 the pipeline | `tests/test_ingest.py` | 28 tests |
| 2.1 text as a producer | `check_search --rebuild`, `check_tools` | 10 of 10, 12 of 12 |
| 2.2 one write path | `check_tools` unchanged | 12 of 12 |
| 2.3 slow work queued | an upload not blocked by a blocking producer | met |
| 2.4 rebuild and forget | `scripts/check_ingest.py` | 14 of 14 |
| 2.5 tenancy | `check_isolation` | 38 passed, 1 not tested |

Suite 381 → **426 passed, 1 skipped**. No capability a user would notice, which
was the point: image reading, numeric fields and embeddings are each now a new
producer in an existing pipeline rather than a fourth reader of the same PDF.

## 2.4: derived/ is disposable, and now something checks

10 September. `scripts/check_ingest.py` is the new gate, **14 of 14**, and
`check_search` grew a tenth check. On this install the orphans are gone: 11
artifacts for 11 documents, where the folder had been carrying 41 decoys' worth
of leftovers from every `check_search` run since 2.1.

- **`ingest.forget_missing()`** reconciles both sides against `data/`, not just
  the manifest. A producer folder can hold output for a source with no row --
  a crash between writing the bytes and recording them -- and sweeping only the
  rows would leave that on disk with nothing pointing at it, which is the
  harder sort to notice.
- **`ingest.rebuild()` reconciles before it produces.** A rebuild that added
  what was missing but left what should not be there makes "rebuilt" mean a
  little less every time it runs.
- **`search.rebuild()` calls `ingest.rebuild(only_fast=True)` first.** A
  rebuild reads the text artifacts, so "rebuild from the files on disk" is only
  honest if what stands between it and the disk is current. Fast producers
  only: an index rebuild must not turn into an OCR run.
- **`POST /api/ingest`**, folder-level and always a job. This is the rebuild
  affordance the app never had -- `search.rebuild()` was reachable only from a
  script, so an operator whose index had drifted had to open a terminal.

**Deliberately not hooked into `search()`'s read path.** `search.forget_missing()`
is there because a stale index row is returned to the model and wastes a turn.
Nothing reads an orphaned artifact, so paying for a manifest open on every
search would buy tidiness at the cost of the hot path.

**`check_gateway_isolation` was incomplete and nothing had said so.** Its
forbidden list names every module that reaches tenant storage, and Step 2 added
three it had never heard of -- `ingest`, `producers`, `intake` -- plus three
storage entry points. A module that touches `derived/` and is not on that list
is a hole that looks exactly like a pass. 16 checks to 24, all passing.

**The general form is worth keeping: a gate built from a list is only as good
as the list, and nothing tells you when the list has fallen behind.**

**Two findings from writing the gate.** `check_search`'s total moved again, 9
to 10 -- the second time in two days, and the reason to gate on named checks
and never on the count. And an assertion that counted search hits failed for a
reason unrelated to what it tested: `search()` falls back from "all terms" to
"any term", and the run's tag matched every file the script had written. It
asserts by document name now.

**Suite 421 passed / 1 skipped. `check_ingest` 14 of 14, `check_search
--rebuild` 10 of 10, `check_tools` 12 of 12, `check_gateway_isolation` 24 of
24, `check_api_compat` pass.**

## 2.2 and 2.3: one path in, and slow work off the request

10 September. `app/intake.py` is the one place that decides what happens to a
file that has just been written — fast producers in the request, then the
index, then anything slow to the job lane. The three write paths (the upload
endpoint, `tools._index_quietly`, `db.query_to_excel`) call `intake.arrived`.
Import direction is now `config <- ingest <- producers <- search <- intake`.

**Why a new module rather than a line in `search.py`.** All three callers
already went through one function, so "one path in" was arguably true before.
What was not true is that the path was owned by anything entitled to own it:
the index is a *consumer* of the pipeline, and a consumer that also drives the
pipeline and schedules its jobs is the owner of it wearing a different hat.
`intake` owns the order and none of the work.

**2.3's gate is met.** A producer that blocks until a test releases it is
queued rather than waited on; the upload returns in well under a second with
the text already indexed. It is asserted against a producer that genuinely
blocks on an event rather than one that sleeps — a sleep makes the test a race
against the machine it runs on, and passes on a fast one for the wrong reason.

New surface: `GET /api/ingest` (the folder sorted into ready / outstanding /
failed), `GET /api/ingest/{name}` (is this document ready, and what failed),
`POST /api/ingest/{name}`. The upload response gained `outstanding` and `job`,
because `searchable` says the text is in and says nothing about what is still
queued behind it. Additive, and `/api/*` is not the frozen surface — only `/v1`
is, so `check_api_compat` still passes.

**One job kind for all slow producers**, not one per producer: the lane
serialises anyway, and a file with three slow producers outstanding wants one
queue entry that finishes when the document is ready, not three that each look
like the whole job.

**A full queue is a delay, not a loss.** The manifest still records the
producer as stale, so the next ingest picks it up, and the response says so
rather than swallowing it.

**Suite 415 passed / 1 skipped. `check_tools` 12 of 12, `check_search
--rebuild` 9 of 9, `check_api_compat` pass, `check_isolation` unchanged** (its
one unrun check is the Windows symlink privilege, which predates this and needs
Linux).

**Not proven over a live socket.** The gate ran through the real ASGI app and
the real job-lane threads under `TestClient`; uvicorn in front of that is not
where the risk is, and minting a token in this install's own control plane to
prove it was not worth the side effect.

**A test-hygiene finding worth keeping, because it cost time.** The job lane is
a module-level singleton with one worker, so a producer left blocking by a
failed assertion wedges it for the full timeout and fails the NEXT test too. It
read as two bugs and was one. The release is in a `finally` now.

## 2.1: the index is a consumer now, and the gate caught two things

10 September. Text extraction moved out of `app/search.py` into
`app/producers.py` as the pipeline's first producer, and `search.index_file`
became a consumer of it: it calls `ingest(name, only_fast=True)` and reads the
artifact. Import direction is `config <- ingest <- producers <- search`, and
search must never be imported back the other way — a cycle would be the old
arrangement smuggled back in.

**Gate: `check_search --rebuild` 9 of 9, `check_tools` 12 of 12, suite 406
passed / 1 skipped.** Nothing about the behaviour differs, which was the whole
requirement.

**The gate earned itself twice, and that is the argument for doing this
sub-step before anything interesting.**

**1. The mtime tolerance was wrong, and only became wrong here.** 2.0 carried
`search.stale()`'s one-second tolerance into the freshness check on the
reasoning that the two ought to agree about the same file. They do not serve
the same purpose. `stale()` produces a *suggestion* — a list somebody might
want to reindex — so being loose costs a rebuild nobody needed. The pipeline's
check decides **whether derived data may be served for bytes that no longer
exist**, and being loose there is silently wrong. An existing test rewrote a
PDF with a body of the same length inside the same second, and the pipeline
handed the index the old text. Exact now. Nothing had been wrong before,
because `index_file` re-extracted every time and had no cache to be stale.

**A tolerance copied from a function whose consequences are different is not a
consistency, it is a guess.** That is the general form and it is worth keeping.

**2. `scripts/tenant.py` did not know a tenant had a third thing on disk.**
Deleting a tenant removed the folder and the index and left
`derived/<tenant>/`, holding text extracted from that customer's documents.
Found by the suite's own folder guard — the one that fails any test writing
outside `tmp_path` — which reported a real `derived/going/` appearing in the
repo. This is **2.5's storage half, pulled forward**, because 2.1 is what makes
an upload create the artifacts at all: shipping 2.1 without it ships a delete
that leaves a copy of the customer's content behind. The artifacts are
*deleted* even when the documents are only moved aside, since everything under
`derived/` can be made again from the files.

**Two consequences left for 2.4 on purpose rather than half-done.** Nothing
calls `ingest.forget()` yet, so an artifact outlives the file it came from —
`check_search` writes 41 decoy PDFs, deletes them, and leaves 41 text artifacts
behind. And `search.rebuild()` still refuses to run when a parser is missing
even though the text it needs is already extracted; that pre-check is existing
behaviour, and changing it is not a no-behaviour-change sub-step. Both are
written into the plan at 2.4 rather than left to be noticed.

## Step 2 is signed off, and the pipeline exists before its first producer

10 September. The five decisions in `docs/plans/step-02-ingestion-contract.md`
were taken as recommended, and sub-step 2.0 is built: `app/ingest.py`, the
manifest schema, `DERIVED_DIR`, and 23 tests. **Nothing imports it.** That is
the order the plan asked for and the reason to keep it: when 2.1 moves the real
text extraction behind this interface, a change in `check_search`'s numbers
means the move broke something, rather than meaning the pipeline was never
right.

What the contract is, in one line each: a producer declares a name, a version,
the suffixes it handles and whether it is slow; the pipeline decides what runs,
records what happened, and re-runs on a change of source size, mtime or
producer version.

**Three departures from the signed plan, all deliberate and all written into
it.** A producer's `run` takes the output directory as well as the source,
because a producer that picks its own location is one whose output nothing else
can find or delete — and 4.2's promise that `derived/` is disposable would then
rest on every producer remembering to keep it. A `Result` says `ok` or
`skipped` and never `failed`, so failure is the exception the pipeline records
and a producer never has to remember decision 4.5. And a producer that does not
handle a suffix gets no row at all, rather than a `skipped` one that buries the
skips which mean something.

**The layout is new on disk**, which is the kind of thing a restore from backup
has to match: `derived/<tenant>/manifest.sqlite3` for the record, and
`derived/<tenant>/<producer>/<source file>/` for the bytes. A directory per
source rather than a file, because a producer that makes one thing today makes
forty page images tomorrow and `forget()` has to remove all of it without
knowing which.

**One thing the plan did not decide, decided here.** A failed producer is
retried on the next call and `attempts` counts the goes at the same unchanged
input, resetting when the file or the producer version changes — three failures
against the old bytes say nothing about the new ones. No retry cap: the
pipeline records, and a policy about when to stop belongs where the retrying is
scheduled, which is 2.3.

**And one bug found by its own test**, worth the entry because it is the same
shape as several already here. Pruning the empty per-source directory left
`derived/<tenant>/<producer>/` standing empty, which reads as "this producer
has output here" to anything listing the folder. The test asserting that a
producer which wrote nothing leaves nothing behind was written expecting to
pass, and did not.

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
- ~~**`check_search` reports 8 of 8** where this file used to claim 9 of 9.~~
  **Explained 10 September. Both numbers are right, and the denominator is the
  bug.** `scripts/check_search.py` holds nine checks, and one of them —
  "Rebuilt from the files on disk", line 81 — is inside
  `if args.rebuild or before["not_yet_indexed"]`. Run against an index that is
  already current it never executes, and the script honestly reports 8 of 8.
  Run with `--rebuild`, or against a folder holding a file the index has not
  seen, it reports 9 of 9. Both runs were done today and both pass.

  Nothing regressed and nothing ever did. What is wrong is quoting the total at
  all: **the denominator is a function of how the script was invoked and what
  state the index was in**, so "unchanged at 9 of 9" is a gate that can fail for
  a reason that is not a failure, and pass while a check silently did not run.

  **This matters beyond the tidiness, because Step 2.1's gate is written on
  it.** The plan says "`check_search` unchanged at 9 of 9". Gate 2.1 on the
  named checks all passing — and run it with `--rebuild` so the conditional one
  is among them — not on the total.
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

Two more from Step 4.2, and the first is the most expensive class in this file
because it has now recurred three times:

9. **A missing library is an environment fault; an unreadable file is a data
   fault. Reporting the first as the second blames the customer's documents.**
   It cost nineteen good PDFs indexed as empty, then `.pptx` and `.md` recorded
   as damaged. **The subtlety that got it through the second time: a library
   can defer its import to the call.** docling-slim imports a format's reader
   when the file is read, so import-time checks prove nothing — the backend
   module imports fine, and the `ImportError` arrives later from inside a
   generic `except`. Check what a row *needs*, not what it *imports*.
10. **"It succeeded" is not a gate. A producer that writes an empty artifact
    succeeds.** Both broken format rows would have passed any check that
    asserted on the outcome. What caught them was asserting on a sentinel
    string *inside* the extracted text. **A row nothing ever reads is a claim,
    not a fact** — which is also why the formats gate loops over the whole
    table rather than a chosen few.

One more from 4.3, and it is about the tests rather than the code:

11. **A test that has never been watched to fail is a claim, not a check.**
    Two of 4.3's thirty passed against deliberately broken code — one because
    its fixture never built the case it asserted on, one because the line it
    protected was dead. Both were found by breaking the property and watching,
    in about a minute each; neither was findable by reading. This is lesson 10
    turned on the test suite: **a test nothing has ever seen fail is a row
    nothing ever reads.**

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

    py -m pytest -q                           # 543 passed, 1 skipped
    py scripts/check_api_compat.py
    py scripts/check_gateway_isolation.py
    py scripts/check_isolation.py             # 54 passed, 1 not tested on Windows
    py scripts/check_ingest.py                # 37 of 37, formats, offsets and byte-identical chunks
    py scripts/check_retrieval.py             # MRR 0.576 against the committed baseline
    py scripts/check_agent.py                 # needs the model up
    py scripts/check_remote.py
