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

**2. Prove the reboot.** `sudo reboot` on the box, then both `:8000` and
`:8080` answer with nothing typed. The box being up right now proves the
process runs; it does not prove it returns on its own, which is the open claim.
Do this second — it costs the box's uptime, and the website test needs the box.

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
is not.

### The local website test — what it is and where it stopped

The point: `database-agent` can run on the laptop and talk to the box over the
LAN at `http://192.168.1.185:8080/v1`. That exercises the real UI, real
multi-turn tool calling and the real clamp — everything the Step 3 gate asks
for except "from Cloud Run", which needs the tunnel. Worth doing first because
it separates "does the integration work" from "does the network path work".

Done on the laptop: `npm install`, `npx prisma generate`. The dev server is up
and serving (`GET / -> 200`).

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

**Next action, and it needs a value only the operator has:** create
`database-agent/.env.local` —

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

## Where it stands

The 5090 box is real and serving. `syslab-server` is now three planes in one
FastAPI process: the **inference plane** at `/v1` (no tenant, its own tokens),
the **local plane** at `/api/...` (this install's own admin surface), and the
retrieval plane, which is Step 4 and does not exist yet.

Gated 8 September: **pytest 368 passed, 1 skipped**, `check_gateway_isolation`
pass, `check_api_compat` pass, `check_remote` 13 of 16 with the public-surface
section all passing, `check_agent` 6 of 8 against the live 32B model.

## The machine

| | |
|---|---|
| Box | Ubuntu, RTX 5090 32 GB, Ryzen 9950X, 60 GB RAM, `192.168.1.185` on the LAN |
| Model | `Qwen/Qwen3-32B-AWQ` under vLLM `v0.28.0`, pinned by digest in `docker-compose.yml` |
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
3. **Prove the reboot.** The systemd unit is installed and running; nothing has
   yet confirmed the app actually returns on its own. `sudo reboot`, then both
   ports answer untouched.
4. **Step 2, the ingestion contract.** Designed in full in
   `docs/plans/step-02-ingestion-contract.md` and **waiting on your sign-off of
   its five decisions**, which is the thing actually blocking it. The plan's
   dependency order is 0, 1, 3, 2, 4, 5, 6, 7 — Step 3 comes before Step 2
   because it is the step that stops the per-request model bill.

## Known and deliberately not fixed yet

- **Nothing trims conversation history, and a full window is unrecoverable.**
  Found 9 September in the operator UI: vLLM returned HTTP 400 with "you
  requested 0 output tokens and your prompt contains at least 8193 input
  tokens" against the 8192 window. The 0 is not a caller bug -- the internal
  path sends no `max_tokens` (`app/llm.py:42`) so vLLM fills what remains, and
  what remained was nothing. Yesterday's clamp does not cover it: it is on the
  `/v1` path only (`app/gateway.py:139`), and it deliberately bails when
  `room <= 0` rather than truncate a prompt (`app/gateway.py:142`). The history
  itself is unbounded (`app/agent.py:754`). The failure is terminal for that
  conversation -- every following message is larger than the one that just
  failed -- and the only recovery is starting a new one. **The website will hit
  this too**, on any long conversation, which puts it directly in front of the
  Step 3 gate. The fix is a decision, not a patch: drop oldest turns, summarise
  them, or refuse early with a clear message. Trimming silently is the one
  option the gateway's existing reasoning already argues against.

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
