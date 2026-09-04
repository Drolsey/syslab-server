# Four Months: local inference behind database-agent

Written 4 September 2026. Covers week 1 (Mon 7 Sep 2026) to week 18 (w/c 4 Jan 2027).

Assumptions this is built on, all stated by Amro:
- **40 hours a week.** Roughly 680 hours, minus the holiday dip in weeks 16 and 17.
- **Two RTX 4090s, 48 GB total.** Section 8 has the consequences, and one of them is not
  what "48 GB" sounds like.
- **Success is a full working pipeline integrated with the web app.** Not billing, not a
  marketplace. That target shapes every priority below: quality and integration beat
  breadth.

---

## 1. The one sentence version

Turn syslab-server into an OpenAI-compatible inference endpoint that database-agent treats
as one more model provider, serve it from a two-card Linux box running vLLM, and prove at
every step against the same fixed set of real questions that the local model answers them as
well as a hosted one would.

## 2. What exists at the end

```
  Customer's browser
        |
  database-agent (Next.js, his fork)          runs the agent loop, owns the SQL,
        |  Settings -> Model provider          holds connections and the playbook
        |  custom, base URL + API key
        v
  syslab inference API            /v1/chat/completions (SSE, tools), /v1/models
        |                         subscribers, keys, quotas, metering, admission control
        v
  vLLM x2, one per 4090           Qwen3-Coder-30B-A3B, two independent replicas
        |
  Ubuntu box on the tailnet
```

syslab-server keeps: the control plane, the request-identity work, the job lane repurposed
as admission control, `app/llm.py`, the gates. It stops carrying documents, tools, search,
its own agent loop and its own web page.

## 3. The measuring stick, and it is the most important thing in this plan

**Week 1 builds a fixed set of about 40 real questions against `medi_merchant`, with
known-correct answers.** Every phase gate re-runs it. Nothing else in this plan is allowed
to be the reason a phase passes.

Composition:
- 15 simple: counts, filters, single-table aggregates over the `Report` table.
- 15 hard: joins, date ranges, and the numeric filtering that has always been the weak
  spot ("labour more than 12,000"), against 35 columns that all need SQL quoting.
- 5 that require the ESG report tool rather than a query.
- 5 that **cannot be answered from the data at all**, and where the only correct behaviour
  is to say so.

Scoring, three numbers, every time:
1. **Correct**: the figure matches the known answer.
2. **Honest**: it refused the five unanswerable ones. A fabricated number is not a lower
   score, it is a **failure of the whole run**. This is the number that decides whether the
   thing is sellable.
3. **Cost**: seconds to first token, seconds to complete, tokens in and out.

Build it once, in week 1, by hand, with the answers verified against the database. It is
about a day of tedious work and it is the difference between a project that knows whether it
is improving and one that argues about it.

---

## 4. Phase 0 — Week 1 (7 Sep). Does the model work at all, with no code written

The pivot rests on an untested assumption: that a local model can drive **somebody else's**
agent loop, with none of the guards we wrote in `app/agent.py`, and produce correct SQL.
Answer that before building anything to serve it.

| Day | Work |
|---|---|
| 1-2 | Build the eval set. Verify every answer against the database by hand. |
| 2 | Point a database-agent workspace at Ollama over Tailscale: `custom` provider, base URL `http://desktop-r0g7ikh.tail85a02b.ts.net:11434/v1`, model `qwen3:8b`. Attach `medi_merchant` read-only. |
| 3 | Run the eval set against qwen3:8b, then qwen3:14b. Record all three numbers. |
| 4 | **Rent a 4090 for a day**, roughly $10 at the recorded $0.39/hr, and run the same eval against Qwen3-Coder-30B-A3B on vLLM. |
| 5 | Write up the comparison. Decide. |

**Gate.** At least one model reaches: 80% on simple, 50% on hard, and **zero fabricated
figures**. Fabrication is disqualifying at any score.

**If nothing clears it**, stop and replan rather than proceeding. The options in that case
are a larger model on rented hardware, or a hybrid where a hosted frontier model handles the
hard questions and the local one handles the rest, which is a different product and needs
saying out loud rather than drifting into.

**Why day 4 matters more than it looks.** Ten dollars of rental answers the question a
four-thousand-dollar purchase is betting on. Do not buy two 4090s before this number exists.

---

## 5. Phase 1 — Weeks 2-3 (14, 21 Sep). The provider API

**Week 2.** The contract, and it is small but exacting.

- `POST /v1/chat/completions`: SSE only, because the adapter always sets `stream: true`.
  Text deltas, tool-call deltas with fragmented `function.arguments` keyed by index,
  `finish_reason`, and `usage` on the final chunk under `stream_options.include_usage`.
- `GET /v1/models` returning `{data: [{id}]}`, so their Test button works.
- Bearer auth, absent header tolerated.
- Errors: HTTP status where possible, and the mid-stream `{error:{message}}` shape where a
  failure happens after the stream has opened.

**Week 3.** The conformance suite, and the strip-down.

- A test suite that speaks the adapter's exact expectations, including a tool call whose
  arguments are deliberately split across chunk boundaries, a mid-stream error, and a
  provider that omits tool-call ids.
- Take the tool loop, file tools, search and database tools **out of the serving path**.
  Leave the code in the repository; delete nothing this month.

**Gate.** database-agent pointed at syslab-server scores **identically** to database-agent
pointed at Ollama directly, on the same eval set. Identical, not similar: the wrapper must
add nothing and lose nothing.

**In parallel, and deliberately early: the first two PRs to the fork.** The
`SET LOCAL statement_timeout` no-op and the missing `default_transaction_read_only`, both in
`lib/connectors/postgres.ts`. They are small, they are real bugs, and they are the cheapest
way to learn that codebase and earn reviewer trust before asking for anything larger. Doing
this in week 2 rather than week 10 is the whole reason it is listed here.

---

## 6. Phase 2 — Weeks 4-6 (28 Sep, 5, 12 Oct). Linux, vLLM, and the hardware

The largest and most underestimated block. vLLM has no native Windows support, so this is
not a swap, it is a platform move.

**Week 4. Buy and build.** Cards, a board with two spaced x16 slots, a 1600 W PSU, cooling.
Two 4090s draw around 900 W between them, and a Dubai 230 V circuit handles that comfortably
where a 110 V one would not. Ubuntu 24.04, drivers, CUDA, Tailscale.

**Week 5. vLLM.** Qwen3-Coder-30B-A3B in a Marlin-kernel AWQ build, because GGUF under vLLM
is slow enough to defeat the point. **Two independent replicas, one per card, not one model
split across both** (section 8). A small router in syslab-server picks a replica.

**Week 6. Move the operational story.** systemd instead of Task Scheduler, and the unit file
already exists in `scripts/service/`. Every check script needs a Linux path. Restart,
reboot-survival, and log handling all get proven again on the new platform.

**Gate.** The eval set scores the same or better on the new box; a measured tokens-per-second
figure under concurrency; and it comes back on its own after a power cut.

**Risk.** Three weeks is honest but not generous. If the build slips, Phase 3 can start on
the old machine, because subscribers and metering do not depend on which GPU answers.

---

## 7. Phase 3 — Weeks 7-9 (19, 26 Oct, 2 Nov). Subscribers

This is where Step 1's control plane earns its keep, renamed rather than rewritten.

**Week 7.** Tenants become subscribers, tokens become API keys with a plan attached. Key
issuance, rotation and revocation through `scripts/tenant.py`.

**Week 8.** Metering: input and output tokens per subscriber per request, stored durably,
queryable. Quotas: monthly ceiling, refusal with a clear message and a documented error code
rather than a silent truncation.

**Week 9.** Admission control, which is the job lane repurposed. A concurrency cap per
subscriber and a global one, with queueing rather than refusal under short bursts. The
property to prove is that **one subscriber cannot starve another**, which is a load test, not
an opinion.

**Gate.** Two subscribers under simultaneous load; correct metering to the token; a
demonstrated fair-queueing outcome; an over-quota call refused clearly.

---

## 8. The hardware, in detail, because "48 GB" is misleading

Two 4090s are **not** a 48 GB pool. They are two 24 GB pools, and the 40-series has no
NVLink, so splitting one model across both goes over PCIe.

**The recommended shape: two independent replicas, one model per card.**
- Qwen3-Coder-30B-A3B at 4-bit is roughly 18 GB, which fits one card with room for KV cache.
- Aggregate throughput is additive: the recorded figure is about 2,259 tok/s per 4090 serving
  this model with batching, so roughly 4,500 tok/s across the pair.
- A card failing takes out half the capacity rather than all of it.
- One card can later run something different, a vision model or a re-ranker, without
  disturbing the other.

**Tensor-parallel across the two** is the alternative, and it is the right answer only if a
model that does not fit in 24 GB becomes necessary. On PCIe without NVLink it costs
interconnect overhead on every token. This is the same distinction already recorded in the
project's hardware notes: splitting one model across consumer cards is where PCIe bites, and
running different models on different cards is where it does not.

**Watch for**: 4090s are three slots or more, so board spacing is a real constraint; 1600 W
of PSU; and case airflow, since two cards this size in one box is a thermal problem before it
is a compute one.

---

## 9. Phase 4 — Weeks 10-12 (9, 16, 23 Nov). The web app, which is the actual goal

**Week 10. The preset PR.** About fifteen lines in `lib/agent/providers/presets.ts` adding a
`syslab` entry with the base URL and `keyOptional: false`, so subscribing is a click rather
than a typed URL. Small, and by now the third PR rather than the first.

**Week 11. Local-model UX.** A local 30B has a slower first token than a hosted API and a
long tail on complex turns. Look at how the fork surfaces waiting, whether
`MODEL_REQUEST_TIMEOUT_MS` at 300 s is right for this deployment, and whether a mid-stream
error reads as a useful message or a dead spinner. Fixes here go upstream as PRs.

**Week 12. The ESG report path.** `generate_esg_report` is the second of the two tools and it
is the hospital client's actual use. Prove the local model calls it correctly: right hospital,
right period, and the "exactly one of hospital_name or hospital_group" rule respected.

**Gate.** The hospital client's real questions answered end to end through the web app, on
the local model, including at least one report generated and downloaded.

**This is the point at which the stated four-month goal is met.** Everything after is
consolidation, and if the project has slipped, it should slip into weeks 13 onward rather
than into this phase.

---

## 10. Phase 5 — Weeks 13-15 (30 Nov, 7, 14 Dec). Database hosting

Three modes were asked for, and none of them is model-provider work; they are
database-agent connections plus infrastructure.

**Week 13. Postgres for customers with no database.** An instance, a role and schema per
customer, backups with a **restore actually tested**, and connection details issued into
database-agent. The read-only role design already proven on the client instance applies
directly.

**Week 14. A mirror of a customer's database.** Logical replication where the customer's
Postgres allows it, a scheduled extract where it does not. The honest part is staleness: the
agent must be able to say how old its copy is, because an answer that is silently a day out
of date is worse than no answer.

**Week 15. Cloud databases.** BigQuery already works in the fork. Snowflake is in progress
and Azure Blob is planned, so those are contributions rather than configuration, and should
only be started if a real customer needs one.

**And the decision that has been deferred twice:** credential encryption. Under this
architecture the credentials live in database-agent's secret store rather than in
syslab-server, which reduces but does not remove the question. Settle it here.

---

## 11. Phase 6 — Weeks 16-17 (21, 28 Dec). Make it survivable, at half pace

Treat these two weeks as roughly half capacity for the holidays, and plan accordingly rather
than discovering it.

- Monitoring: is the model up, what is the queue depth, what is the p95 first-token latency,
  what did each subscriber consume.
- Alerting on the two failures that matter: the model server down, and the disk full.
- Backups with a **restore drill**, not a backup job that has never been read back.
- A security pass over the exposed surface, since this is now an internet-adjacent API rather
  than a tailnet toy.
- An incident runbook in the style of the existing `runbook_syslab` notes.

**Week 18 (4 Jan) is buffer.** If it is not needed, spend it on the eval set: more questions,
harder ones, and a second customer's schema.

---

## 12. Decision points

| When | Decision | What decides it |
|---|---|---|
| End of week 1 | Does a local model clear the bar? | The eval numbers. Fabrication rate is the veto. |
| End of week 1 | Buy the cards, or rent longer? | The rented-4090 result, not enthusiasm. |
| End of week 3 | Is the provider API sound? | Identical eval scores through the wrapper. |
| End of week 6 | Is the Linux box production-worthy? | Eval parity, throughput, reboot survival. |
| End of week 9 | Can it take more than one customer? | The fairness load test. |
| End of week 12 | **Is the stated goal met?** | The client's real questions, end to end. |
| End of week 15 | Which hosting modes are real? | Whether a customer actually asked. |

## 13. What to cut if it slips, in this order

1. **Snowflake and Azure connectors.** Nothing needs them yet.
2. **The mirror mode.** Direct connections cover the same customers with less machinery.
3. **Quotas and billing-grade metering.** Keep admission control, which protects the box;
   defer accounting, which protects revenue that does not exist yet.
4. **The second replica.** One 4090 serving is a smaller product, not a broken one.
5. **The upstream PRs.** Carry them as a fork-local patch and contribute later.

**Never cut:** the eval set, the conformance suite, or the fairness test. Those three are how
anyone can tell whether the rest works.

## 14. What could kill this

| Risk | Signal | Response |
|---|---|---|
| The model cannot write correct SQL on this schema | Week 1 eval | Larger model, or a hybrid with a hosted model for hard questions. Decide in week 1, not week 10. |
| The model fabricates figures | Any eval run | Stop. An analyst that invents numbers is worse than no analyst, and no amount of UI hides it. |
| Linux migration eats three weeks and wants five | Week 6 gate | Serve from rented Linux while the box is finished. The provider API does not care. |
| The TypeScript codebase is unfamiliar and slow going | Week 2-3 PR pace | The two connector PRs in week 2 are the early warning. If they take a fortnight, rescope Phase 4. |
| Two 4090s cannot be cooled or powered in the chosen case | Week 4 | Found by building it, which is why the build is week 4 and not week 12. |
| The upstream maintainers do not want the changes | First PR response | Fork-local patches. It costs merge maintenance, not the product. |

## 15. Money

| Item | Estimate |
|---|---|
| Rented benchmarking, week 1 and occasionally after | $10 to $100 total |
| Two RTX 4090s | Check current pricing; the used market moved after the 50-series launch |
| Board, 1600 W PSU, case, cooling | roughly $600 to $1,000 |
| Ubuntu, vLLM, Tailscale | free |
| Electricity, two cards under load | around 900 W plus the system; at DEWA commercial rates this is a real monthly line, worth calculating before quoting a customer a price |

The recorded rental comparison is worth keeping in view throughout: a 4090 at $0.39/hr is
about $280 a month of continuous use. Owning wins on a long enough horizon and on the
data-never-leaves-the-building argument, which is the actual product claim, and not on price
alone in month one.
