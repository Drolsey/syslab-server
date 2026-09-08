# Step 3 — The model gateway

Status: 3.0, 3.1, 3.2 and 3.3 complete. 3.4 is folded into Step 5 (below). 3.5
is built on this side; its gate is the website's, and is still open.

## 3.5 — the network path and the frozen contract

Three pieces, of which only the first is code the website can see.

### The frozen contract

`docs/api/gateway-v1.released.json`, taken from FastAPI's own schema by
`scripts/check_api_compat.py --freeze`, and compared against on every run.

**Only `/v1` is frozen.** The local plane (`/api/...`) has exactly one client
and it ships in the same commit as the server; freezing it would produce a
stream of failures that mean nothing, and a gate people learn to ignore is
worse than no gate. `/v1` is the one surface whose caller deploys separately,
from a repository that pushes straight to production.

**Three outcomes, not two:** compatible (0), breaking (1), and *cannot judge*
(2) when there is no frozen file or the app will not import. A contract check
that could not load the app has not passed.

**What it cannot see, stated rather than implied.** It compares the request
contract — routes, methods, required fields, types, and whether unknown fields
are still accepted. It cannot compare response bodies, because the gateway
returns vLLM's JSON unmodified and FastAPI has no response model to declare.
That shape is OpenAI's, not ours, and `scripts/check_gateway.py` checks it
against the running server instead. Two gates, two questions: this one asks
"did we narrow the door", that one asks "does what comes back still look
right".

Verified by breaking it, three ways, each caught with exit 1:

| Break | Reported as |
|---|---|
| A new required field on `ChatCompletionRequest` | `` `workspace_id` is now required. Existing callers do not send it.`` |
| `extra="allow"` → `extra="forbid"` | unknown fields no longer accepted — which silently rejects `tools` and `tool_choice`, neither of which the schema ever named |
| `/v1/models` renamed | `/v1/models` was removed. A caller pinned to it now gets 404 |

The second is the one worth having. `extra="allow"` is what makes an ordinary
OpenAI client work untouched, and nothing about reading the field list would
tell you that turning it off breaks tool calling.

### The tunnel

`cloudflared` in `docker-compose.yml`, pinned by digest like vLLM, behind a
compose profile.

The profile is activated by `COMPOSE_PROFILES=public` **in `.env`**, not by a
`--profile` flag. A flag has to be remembered on every invocation including the
one after a reboot at 2am; a rule enforced by remembering is not enforced.
`.env` makes the machine that is meant to be public the machine that brings up
the tunnel, and leaves a laptop that clones this repo unable to publish
anything by accident.

One trap, found by testing rather than by reasoning: the obvious way to write
the token line is `${CLOUDFLARE_TUNNEL_TOKEN:?...}`, so a missing token is a
clear error instead of a restart loop. **Compose interpolates the whole file
before it filters by profile**, so that form makes `docker compose up -d vllm`
fail on every machine without a tunnel token — including this box before the
tunnel existed. Confirmed at `docker compose config`. It uses `:-` instead, and
`docs/runbook.md` names the restart loop as the symptom of an empty token.

### The real client address

The tunnel changes something no test was watching. Every request now arrives
from the cloudflared container, so `request.client.host` is one address for the
entire internet, and 3.3's login throttle keys on it: eight wrong tokens from
anyone would lock out everyone, turning a rate limit into a denial of service
against the operator. It would have happened silently, on the day the tunnel
went up, with nothing in this repository failing.

Cloudflare puts the real address in `CF-Connecting-IP` and sets that header
itself, discarding whatever the caller sent — but only while the app is
unreachable except through the tunnel, which is a condition a header cannot
state. So it is a setting, `TRUST_CLIENT_IP_HEADER`, off by default, and both
halves are asserted in `tests/test_network_path.py`: that it works when on, and
that it is **ignored** when off. The second is the one that matters — a trusted
`CF-Connecting-IP` with the port also open is a throttle anyone evades by
varying one string, which is worse than the bug it fixes.

`X-Forwarded-For` is deliberately not read: it is caller-appended, and its
left-most entry is whatever an attacker typed.

Verified by breaking it, three ways: guard removed (2 tests fail), header
ignored entirely — the pre-3.5 behaviour (2 fail), truncation removed (1 fail).

### Also in this sub-step

`scripts/service/syslab-server.service` was renamed to
`syslab-server@.service`, which fixes a real bug rather than tidying a name: it
used `%i` for the user and the home directory, and `%i` is the *instance* name,
which a non-template unit does not have. Installed under the old name every one
of those paths expanded to nothing. Its `After=ollama.service` was also stale —
the model is a container now — and is `After=docker.service`.

`docs/runbook.md`, the other Section 16 deliverable for this sub-step: start,
stop, roll back, read logs, publish through the tunnel, and the failures that
have actually happened on this box.

**Not yet done — this is the step's gate, and it needs the website.** The
website's own provider code, unmodified, completing a multi-turn tool-calling
conversation against `/v1` from Cloud Run. Not a curl: their code, their
deployment, their network. Everything on this side is ready for it.

## 3.4 — model profiles, folded into Step 5

Section 13 replaces one model string with a `models.toml` naming five roles —
chat, embed, rerank, stt, tts — so any of them can change without a code edit.
That is the right design. It is the wrong week.

Today exactly one of those five roles exists. Embeddings arrive in Step 5,
speech in Step 6. Building the profile file now produces a table with one row
in it, and the rule that makes it worth having — **an empty value means
unavailable, never a silent fallback** — has nothing to be empty about yet. A
mechanism with no second case is not a mechanism, it is an indirection, and
this repo would carry it, document it and test it for eight weeks before it
did anything.

The two things 3.4 was actually protecting are already true without it:

- **The website pins an alias, never a model name.** Done in 3.2.
  `MODEL_ALIASES` in `app/config.py` maps `syslab-default` to whatever is
  served, and the alias round trip is verified on the live server (see 3.2's
  table). Changing the served model is a one-line edit on one machine and
  changes nothing in Secret Manager. That was rule 1 of Section 13's four, and
  it is the only one with a caller depending on it today.
- **The served model and its digest are written down.** `docs/models.md` and
  the digest pin in `docker-compose.yml`, with the changelog rule that a model
  change is a changelog entry.

What moves to Step 5, where the second model appears and the file finally has
two rows to hold: `models.toml` with `SYSLAB_PROFILE`, the `dev-laptop` vs
`prod-5090` split, `embed_dim` feeding Step 2's producer version so a model
swap re-embeds everything, and `scripts/check_models.py` proving every model in
the active profile loads and answers. Section 13's rules 2, 3 and 4 all name
embeddings or a second role; none of them can be tested before Step 5 exists.

Explicitly **not** deferred: the alias indirection itself. If 3.5 or Step 4
ever needs a second alias before Step 5 lands, it goes in `MODEL_ALIASES` as a
second dict entry, which costs a line — the profile file is not on the critical
path for that.

## 3.3 — public exposure

Finding 4.6, both halves.

`PUBLIC_MODE` (off by default, because the wrong default here is
one-directional) hides three things: the interactive docs, the OpenAPI schema
behind them, and the admin page at `/`. Hiding the docs page alone would have
been theatre — it is only a reader for `/openapi.json`, and serving that
publishes every route by name, including the ones that write files. The admin
page returns 404 rather than 401 in public mode: "there is nothing here"
discloses less than "there is something here that needs a password".

The login throttle was a `defaultdict` keyed by client address, pruned by
timestamp within a key and never by key. The `defaultdict` was the sharp edge:
merely *reading* an entry creates it, so every address that ever attempted a
sign-in left a key behind permanently. It is now a plain dict, swept of
aged-out clients on each use, and capped at `MAX_TRACKED_CLIENTS` so an
attacker who varies their source address cannot grow it without limit.
Forgetting an entry is always the safe direction: it gives an attacker nothing
they could not get by waiting out the window, and can never lock out someone
who belongs.

`check_remote.py` gains a "public surface, with no token" section, which is
this sub-step's gate.

**Deferred to Step 4: token kinds.** Section 8's `kind` column on the `tokens`
table is listed under 3.3, but nothing reads it until Step 4's service-token
branch in `require_auth`, and adding a column to an existing table needs
schema-migration machinery this repo does not have yet (`tenancy.py` runs
`CREATE TABLE IF NOT EXISTS` and refuses only *newer* schema versions; there is
no upgrade path). Step 4 needs that machinery anyway and is where the column is
first used, so it lands there. The gateway's own credentials are `GATEWAY_TOKENS`
in the environment, deliberately outside the control plane entirely, so nothing
in Step 3 is waiting on this.

## 3.2 — the inference plane

`app/gateway.py`, mounted on the same FastAPI process, serving
`/v1/chat/completions` and `/v1/models`. Verified against the live server on
2026-09-08 (app on :8080, vLLM on :8000):

| Check | Result |
|---|---|
| No token / wrong token | 401 both |
| `/v1/models` | advertises `syslab-default` only, never the real model name |
| Unknown model | 404 in OpenAI's error shape |
| Non-streamed tool call | `finish_reason: tool_calls`, arguments as a JSON string |
| Multi-turn: tool result fed back | correct final answer, `finish_reason: stop` |
| Streamed tool call | 12 chunks, stable delta index `0`, arguments reassemble to valid JSON, `[DONE]` terminator, usage chunk present |
| Model name in every streamed chunk | `syslab-default`; zero occurrences of the real name |

The alias round trip is the part worth restating: the caller asks for
`syslab-default`, vLLM is asked for `Qwen/Qwen3-32B-AWQ`, and every response
and every streamed chunk says `syslab-default` on the way back. That is what
makes "changing the served model changes nothing in Secret Manager" true.

**Open follow-up, not blocking:** usage accounting is currently pass-through
only — the `usage` block reaches the caller, but nothing on this side records
per-token spend. Real accounting is worth having before Step 7's measurement
window, which is when someone will actually ask what the hardware served.

## 3.1 — app/llm.py speaks OpenAI

`git diff app/agent.py` was empty for the rewrite itself; all 338 tests pass
unchanged (they mock `llm.chat` directly). Verified against live vLLM with
`scripts/check_agent.py`, first on the Step 1 smoke-test model
(`Qwen3-8B-AWQ`, 5 of 8 scenarios), then on the production candidate
(`Qwen3-32B-AWQ`, 6 of 8).

One real bug surfaced and was fixed: `query_to_excel`'s tool description said
"THE ONLY WAY to turn a database table into a file" and listed "turn X into
a file" as a trigger phrase, with nothing telling the model that a file
already in the data folder is not a database table. Both models hit this —
the 8B model hallucinated a SQL query when asked to sum a spreadsheet it had
already read; the 32B model hallucinated one when asked to turn an invoice
PDF into a spreadsheet. Fixed by adding an explicit exclusion to the tool
description (`app/agent.py`). Pre-existing gap, not introduced by this step.

The remaining 2 of 8 failures on `Qwen3-32B-AWQ` (scenarios 3 and 7) are not
treated as bugs: both times the model reached the correct, verified answer
by a different valid tool path than the test's `expect_tools` requires
(`search_files` + `read_pdf` instead of `list_files`), and scenario 7's
"ambiguity" compares against a fixture (`phase02_sample.pdf`) that has
nothing to do with invoices, left over from an earlier phase's tests. Worth
tightening the test's assertions at some point, but it is not a gateway or
model-quality regression and does not block Step 3.

## 3.0 — vLLM capability matrix

Measured 2026-09-08 against `Qwen/Qwen3-8B-AWQ` on the production box, via
`scripts/check_gateway.py --probe`. This is the model and build that were
running; re-run the probe if either changes before relying on this matrix.

**Finding 1 — tool_choice needed launch flags it wasn't given.** The
container was started with no tool-calling flags at all. Every `tool_choice`
value except `"none"` returned HTTP 400: `"auto" tool choice requires
--enable-auto-tool-choice and --tool-call-parser to be set`. Fixed in
`docker-compose.yml` by adding `--enable-auto-tool-choice` and
`--tool-call-parser hermes` (the parser the architecture plan names as
covering Qwen models). After the fix, the full matrix passes:

| `tool_choice` | Prompt | Result |
|---|---|---|
| `auto` | tool-relevant | Called `get_weather`, `finish_reason: tool_calls` |
| `auto` | unrelated | Did not call it — see Finding 2 below |
| `none` | tool-relevant | Never calls, `finish_reason: stop` |
| `required` | unrelated | Forced a call anyway, `finish_reason: tool_calls` |
| named (`{"name": "get_weather"}`) | unrelated | Forced exactly that function |

**Finding 2 — Qwen3's thinking mode is on by default and will exhaust a
token budget.** The `auto, unrelated prompt` case returned `finish_reason:
"length"` at `max_tokens: 200` — the model spent the whole budget on a
`<think>...</think>` block and never produced an answer. This is the same
behaviour `app/config.py` already documented for Ollama (`OLLAMA_THINK`
defaults to `false` because thinking "roughly triples the wait... keeps the
transcript readable"). vLLM exposes the same control as a non-standard
top-level request field: `chat_template_kwargs: {"enable_thinking": false}`.
Confirmed directly: with it set, the same prompt returns a 7-token answer,
no `<think>` block, `finish_reason: stop`. `app/llm.py`'s `think` parameter
must keep setting this so default behaviour doesn't silently change from
"fast, no reasoning" to "slow, verbose" on the same setting.

**Finding 3 — streaming tool calls are well-formed.** With
`stream_options: {"include_usage": true}`, a streamed tool-calling response:
one tool call, index `0` on every delta chunk (stable, matches the plan's
concern about "whether tool-call deltas carry stable indices"), arguments
arrive across a handful of chunks that concatenate into valid JSON
(`{"city": "Cairo"}`), and a final chunk carries `usage`. No index-tracking
workaround is needed for the single-tool-call case; multi-tool-call-in-one-turn
streaming is untested and should be checked before `app/gateway.py` relies on it.

**Finding 4 — tool_calls arguments arrive as a JSON string, not a dict**
(`arguments_type: "str"` in every probe result). `app/agent.py`'s
`_coerce_arguments` already handles both a string and a dict transparently
(it was written defensively — "Models sometimes send a JSON string"), so
this needs no change on the agent side. It matters only for how
`app/llm.py`'s `chat()` return shape is built: the dict handed back to
`app/agent.py` must keep `message["tool_calls"][i]["function"]["arguments"]`
as whatever vLLM sent (a string), not pre-parse it.

## What this means for 3.1

- `docker-compose.yml` is fixed and deployed with the tool-call parser.
- `app/llm.py`'s new `chat()` must pass `chat_template_kwargs:
  {"enable_thinking": think}` on every request, defaulting `think` to
  `False` exactly as today.
- The response reshape is: take `response["choices"][0]["message"]` and
  return it under the key `"message"`, unchanged otherwise — this preserves
  `app/agent.py`'s `response.get("message")` access and the tool_calls shape
  it already tolerates. No change to `app/agent.py` is needed or expected;
  the sub-step's gate is `git diff app/agent.py` staying empty.
