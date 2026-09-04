# Pivot: syslab-server becomes an inference provider

Status: PROPOSED. Written 4 September 2026 after reading github.com/Drolsey/database-agent-amro
at depth (203 files, the OpenAPI contract, and the provider, agent and connector code).
Supersedes `step-02-ingestion-contract.md`, which is shelved rather than deleted.

---

## 1. What changed

The product is no longer "an assistant with tools". It is: **customers subscribe to a local
model, and consume it through database-agent as one more provider option.**

That inverts the architecture. syslab-server stops being the thing that reasons, calls tools
and holds documents. It becomes the thing on the other end of one HTTP contract.

## 2. What the fork actually is

Not a prototype. Evidence, from the code rather than the README:

- 34 endpoints under `/api/v1`, contract-first in `openapi/v1.yaml`, bearer auth with
  `dak_live_…` keys and per-resource scopes.
- Multi-tenant throughout: every service takes a `tenantId`, and tenancy hangs off Prisma
  `Company.id` with NextAuth sign-in. **Model providers are per tenant**
  (`lib/services/model-providers.ts`), which is the single most important fact below.
- Ten model presets in `lib/agent/providers/presets.ts`, including `ollama` and `custom`,
  behind two adapters: Anthropic Messages and OpenAI Chat Completions.
- Its own agent loop in `lib/agent/index.ts` with `MAX_ITERATIONS`, exposing exactly **two**
  tools: `run_sql` and `generate_esg_report` (hospital ESG, waste and GHG, which is the same
  domain as the `medi_merchant` database).
- Postgres, MySQL and BigQuery connectors, a conservative SQL guard, row ceilings, schema
  introspection with caching.

**Because model providers are per tenant, no change to that codebase is required for a
customer to use a syslab endpoint.** They add a provider, pick `custom`, paste a base URL
and a key. The subscription is a URL and a key, not a feature.

## 3. The contract syslab-server has to implement

From `lib/agent/providers/openai-compatible.ts`, exactly:

| Requirement | Detail |
|---|---|
| `POST {base}/chat/completions` | The only endpoint that must work |
| **Always streaming** | The adapter sets `stream: true` on every call. There is no non-streaming path. SSE, terminated by blank lines. |
| Request body | `model`, `max_tokens`, `stream_options: {include_usage: true}`, `messages`, and when tools are in play `tools: [...]` plus `tool_choice: "auto"` |
| Response chunks | `choices[0].delta.content` for text; `choices[0].delta.tool_calls[]` carrying `index`, `id`, `function.name`, and `function.arguments` **as fragments to be concatenated per index** |
| Terminators | `choices[0].finish_reason`, and `usage.prompt_tokens` / `usage.completion_tokens` on the final chunk |
| Errors | May be delivered mid-stream with HTTP 200 as `{error: {message}}` |
| `GET {base}/models` | Optional. Used by the "Test" button; returns `{data: [{id}]}`. Without it the probe falls back to a 1-token completion. |
| Auth | `Authorization: Bearer <key>`, omitted entirely when no key is set |

Timeout is `MODEL_REQUEST_TIMEOUT_MS`, default 300000, described in their code as "an agent
turn on a slow local model legitimately takes minutes". That is generous and in our favour.

## 4. What survives, and what stops being developed

| Piece | Fate |
|---|---|
| `app/tenancy.py`, the control plane, SHA-256 tokens, revocation, disable | **Survives and becomes the product.** Tenants become subscribers, tokens become API keys. This is the subscription layer. |
| `app/context.py`, the no-default rule, the isolation discipline | **Survives.** Still how a request knows whose quota it spends. |
| `app/jobs.py` | **Repurposed** as admission control: concurrency per subscriber, queueing under load. |
| `app/llm.py`, the vLLM seam, benchmarks, hardware plan | **Survives, and becomes the whole business.** |
| The gate-script discipline, mutation testing, the tmp_path guard | **Survives.** |
| Per-tenant `data/` and `index/` folders, the 1.3 migration | **Not on this path.** A model provider never sees a document. |
| `app/tools.py`, `app/search.py`, `app/db.py` | **Not on this path.** database-agent owns SQL and its own connectors. |
| `app/agent.py`, the tool-calling loop and its hardening | **Not on this path.** Their loop, their playbook, their skills. |
| `app/main.py` file endpoints, `app/web/index.html` | **Superseded** by their UI. |
| `step-02-ingestion-contract.md` | **Shelved.** |

Honest accounting: roughly half of Step 1's code keeps its job and the document half does
not. Step 0 was a clean baseline and eight real bug fixes, which was worth doing regardless.
Nothing needs deleting today; it stops being extended.

## 5. The problem this creates, and it is the big one

**All of the agent hardening moves out of reach.** Every guard for qwen3's failure modes
lived in `app/agent.py`. In the new architecture their loop drives, and we supply a model
that must emit correct OpenAI-format tool calls and write correct SQL against a 35-column
schema with quoted identifiers.

Their tool surface is small, two tools, which helps. The SQL is the hard part, and it is
exactly where an 8B fails. So the model decision stops being "phase 04, later" and becomes
the thing that decides whether a demo works at all:

- qwen3:8b: what we have. Tool-calling failure modes already recorded in this project.
- qwen3:14b: already pulled, never benchmarked against this workload.
- Qwen3-Coder-30B-A3B: 92% on Berkeley Function Calling, Apache 2.0, ~18 GB at Q4. **Does
  not fit the 3060.**

That is the pivot's real cost: it makes the hardware conversation urgent rather than
theoretical.

## 6. Sub-steps

**P0. Prove it before building anything.** Point a database-agent workspace at Ollama over
Tailscale (`custom` provider, base URL `http://<tailnet>:11434/v1`, model `qwen3:8b`), attach
the client database read-only, and ask three real questions. Zero code. **Gate:** does the
model produce usable SQL through someone else's loop? If not, no amount of API work helps,
and the answer is the model, not the plumbing.

**P1. The OpenAI-compatible surface.** `POST /v1/chat/completions` with SSE, tool calls and
usage, plus `GET /v1/models`. Wraps `app/llm.py`. **Gate:** a script that speaks the adapter's
exact expectations, including a tool call split across chunk boundaries.

**P2. Subscribers.** Step 1's control plane, with tokens issued as API keys carrying a plan
and a quota. **Gate:** two subscribers, isolation, revocation, and a refused over-quota call.

**P3. Metering and admission control.** Token accounting per subscriber, concurrency caps,
queueing under load. **Gate:** one subscriber cannot starve another.

**P4. vLLM and a model that can actually do the job.** Measured, not assumed.

**P5. A `syslab` preset PR to the fork.** Fifteen lines in `presets.ts` so subscribing is one
click rather than a typed URL. Small, and the natural first contribution.

## 7. Two findings to take to the fork as PRs

Both are in `lib/connectors/postgres.ts`, and both are the same class of bug this project has
already paid to learn: a protection that reads as present and is not.

**`SET LOCAL statement_timeout` is almost certainly a no-op.** It is issued outside any
transaction block; there is no `BEGIN` anywhere in the connector. Postgres applies `SET LOCAL`
only within a transaction and otherwise warns and does nothing. The comment above it says it
exists so "a runaway query keeps burning database CPU after we have given up on it", which is
precisely what it does not currently prevent. Fix: `SET` rather than `SET LOCAL`, or wrap the
query in a transaction, or set it in connection options.

**No `default_transaction_read_only`.** Their `sql-guard.ts` is honest that it "is defense in
depth, not the defense" and that the real protection is a read-only role. True, and it leaves
the guarantee resting on whoever configured the database. Passing
`-c default_transaction_read_only=on` in connection options makes the **server** refuse a
write even when the role is over-permissive and the text guard has been fooled. It costs one
line and it is the layer we already proved on the live client instance.

Lower priority, worth raising: connections use `ssl: {rejectUnauthorized: false}`, which
encrypts without authenticating the server, so it does not stop an active MITM.

## 8. The database hosting question

Three modes are wanted, and in this architecture all three are database-agent's `connections`
plus infrastructure, not model-provider work:

- **Postgres for customers with no database.** New: an instance, per-customer roles and
  schemas, backups, and the credential story. Their secret store abstraction already exists.
- **A mirror of a customer's database.** Adds replication or ETL, and a staleness question the
  agent's answers have to be honest about.
- **Cloud databases.** BigQuery works today; Snowflake is in progress and Azure Blob planned,
  so those are contributions rather than configuration.

None of it blocks P0 through P3, and none of it should start before P0 answers its question.
