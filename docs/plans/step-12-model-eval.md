# Step 12: the model eval — scope

Status: **SCOPED, 9 November 2026. Decisions 1-3 taken as recommended the same day; 4 and 5
still open. 12.1 started.**

Why: `reports/Open weight model upgrade audit.md` recommends trialling Qwen3.8-27B, but nothing
can say whether a new model is better until the same questions are scored on both. This step
builds that yardstick, scores today's 14B on it (with the 7 November sampling defaults), and
fixes the bar a candidate must clear.

## What already exists, and is reused rather than rebuilt

**database-agent's eval suite** (`database-agent/evals/`, `npm run eval`) is most of this step
already. It drives the real agent loop against a configured model, repeats each case (default
5, A3 used 10), separates model failures from rate limits and infrastructure, classifies a
failure as routing, answer, retrieval, provenance or rendering, and `evals/compare.ts` compares
two reports with 95% intervals and an exact test. Cases can carry earlier turns (`history`).

It has already measured `syslab-default` (the 14B), on 21 September, 980 valid runs
(`evals/results/a3-assessment.md`):

| Area | 14B result, 21 Sept |
|---|---|
| The 11 original SQL cases (naming, decoy table, dates, multi-database) | 110/110, three times |
| Document routing (media families) | 144/240 and 141/240: whether it searches at all is question-specific |
| Asking first when the source is materially ambiguous | 0 of 40 |
| Live retrieval, specific questions | 30/30 |
| Known-answer citation precision (`known-answer-baseline.md`) | 1/5 on one case |

Two limits matter for a model comparison. Database `execute` is **stubbed** per case, so the
suite checks which table and query shape the model chose, not whether the SQL returns the right
answer. And **none of the five production traps** from 9 September is a case.

**minutes-of-meeting-ai** has the AMI pipeline, `score_ami.py` (WER only), a written pass bar
(at least 6 of 10 reference decisions, at most 1 wrong item) and results for ES2003b and
IB4005. Decisions are scored by hand.

**Document review / field extraction** has no recorded failures and no cases anywhere.

## What this step adds

### 12.1 Re-baseline the 14B (no new code)

Re-run the A3 suites (`--suite=legacy` and `--suite=media`, `--repeat=10`) against
`syslab-default` now that the gateway sends Qwen's sampling. That answers whether the sampling
change moved anything, and it is the 14B's number in every later comparison. About 500 runs at
the suite's 40 requests a minute: roughly an hour on the shared gateway, so in a quiet window.

**Done, 9 November.** 460 runs, all valid (2 requests answered 429 and were retried). The
sampling defaults changed nothing measurable:

| Suite | 21 Sept (A3) | 9 Nov |
|---|---|---|
| Original 11 SQL cases | 110/110 | 110/110 |
| Same, with a library attached | 110/110 | 110/110 |
| Media families | 144/240 and 141/240 | 145/240 |

Per case, the media numbers sit inside A3's run-to-run spread. The largest moves are
`media-multi-leave-policy` 5/10 against 1/10 and 2/10 (Fisher p 0.08 against the pooled 3/20)
and `media-only-paraphrase` 2/10 against 6/10 and 3/10 (p 0.25). Neither is distinguishable from
chance. The same seven cases are 0/10 in all three runs: `media-only-no-database`,
`media-cross-register-and-terms`, `media-multi-two-relevant`, `media-lowrisk-expiry-date`,
`media-material-penalty-which`, `media-material-invoice-rate` and `media-vague-library-only`.
They fail the same way every time, which makes them the clearest test for a new model.

Reports: `database-agent/evals/results/rebaseline-2026-11-09-{legacy,legacy-library,media}.json`.
They are git-ignored and exist only on Amro's laptop. A3's own JSON files are already gone, so
keep these: they are what `evals/compare.ts` will read when a candidate model is run.

### 12.2 The five production traps as cases (database-agent `evals/cases/`)

One case per row of HANDOVER's 9 September table, on a **synthetic hospital schema** shaped like
the client database (decision 1). Graders are the suite's property checks, not an LLM judge:

| Case | Setup | Passes when |
|---|---|---|
| `trap-fence-after-prose` | three to six tool-free prose turns in `history`, then a data question | `run_sql` is called; no ```` ```sql ```` block appears without a query behind it |
| `trap-preamble-bypass` | "top 5 hospitals by …" | a query runs and every figure in the answer appears in its rows |
| `trap-esg-unfamiliar-name` | ESG question naming a hospital that is not the description's old example | the tool gets that hospital, after a lookup when the spelling is uncertain |
| `trap-create-a-graph` | "create a graph of …" | a ```` ```chart ```` block built from queried rows, not a mermaid diagram |
| `trap-monitoring-views` | question touching extension/monitoring views | they are named as such, not queried as business tables |

Each also needs the ideal and wrong scripted runs in `tests/evals-cases.test.ts`, which the
suite already requires before a case may spend model time.

**Done, 9 November.** `database-agent/evals/cases/traps.eval.ts`, run with `--suite=traps`.
The harness gained one hook, `EvalCase.reportGenerator`, so the ESG case can offer
`generate_esg_report` and record its arguments. Scripted ideal and wrong runs pass and fail as
intended, and database-agent's full test run shows 1,040 passed, 0 failed, 5 skipped. **The 14B
on today's database-agent: 10/10 on all five**, with no backstop retry firing in any run. The
fence case uses six prose turns, the condition HANDOVER measured at 0/4.

So the traps were the prompt's doing, and the prompt fixes already on `main` closed them. That
makes these five **regression guards, not a way to tell models apart**: both the 14B and Claude
pass them now. Two grader fixes were made after looking at real answers, not to raise the score.
The monitoring case first required the answer to name the `Report` table, which failed a
correct "medical waste removal records" answer (4/10, then 10/10 once it checked the
description instead). The fence case went from four prose turns to six to match the measured
condition. The fixture is also gentler than production: 7 columns against 35. A candidate
model still has to keep all five at 9/10 or better; the comparison itself rests on 12.3 and on
the seven media cases that fail every time.

### 12.3 SQL that is checked by its result

30-50 natural-language questions against a **real, seeded Postgres database**, each with a
reference query; a run passes when its result set matches the reference's (row order ignored
unless the question asks for one). This is the only SQL evidence worth having: public BIRD and
Spider scores carry 50-66% annotation errors and none of the candidate models has one. Mix:
joins, aggregation, dates and ranges, `NULL` handling, quoted identifiers, a question with no
answer in the data, and five asked after unrelated turns. Needs a small executor in the harness
that runs read-only against that database instead of the per-case stub (decision 2).

**Done, 9 November: 28 questions, and the 14B gets 127 of 140 runs (91%).**

- **The database.** `eval_company` lives in the compose `postgres` on the box. It is seeded
  from `database-agent/evals/sql/eval_company.sql`, which builds every row from a formula, so a
  rebuild gives identical answers: 60 customers, 1,240 orders, 2,479 order lines, 12 products,
  8 sales reps in a `"Sales Reps"` table with quoted column names, and 14 supplier contracts.
- **Access.** The login role `eval_reader` can only read: `default_transaction_read_only` is
  on, and a `DELETE` was tried and refused by the database itself. Remote connections must use
  SSL, which `pg_hba` already requires. The connection string sits in `~/.eval_company_dsn` on
  the box (mode 600) and is passed to an eval run through `EVAL_COMPANY_DSN`.
- **Running it.** `npm run eval -- --suite=sql`. The model's SQL runs through the app's own
  `PostgresConnector`. A run passes when any query it ran returns what the reference returns
  (`evals/sql-match.ts`): extra columns, column names and unrounded numbers are allowed, and
  row order matters only where the question asks for one. "Top" questions accept a longer
  ranked list whose first rows match.
- **Checks.** `tests/evals-sql-match.test.ts` covers the matcher. database-agent's full test
  run shows 1,051 passed, 0 failed, 5 skipped.
- **Cases removed or changed while building:**
  - "Which region has the most Enterprise customers" was dropped: all five regions tie at 4.
  - The first seed gave every customer the same products on every order, so "how many
    different products" answered 1. The seed was fixed and two questions moved with it.
  - Two grader mistakes were fixed after reading real answers. A full ranked list was being
    failed for not being one row, and "the rating for 2025" was read as a rating of 2025.

Every remaining failure was checked by reading the SQL the model wrote:

| Case | 14B | What it gets wrong |
|---|---|---|
| `sql-follow-up-top-customer` | 0/5 | ignores the customer named in the previous turn and writes `ORDER BY total` on `customers`, which has no total column, so Postgres silently uses the orders' total and the query means nothing |
| `sql-distinct-products-juniper` | 0/5 (3/5 in an earlier run) | reads "since the start of July" as "in July" |
| `sql-gloves-units-2025` | 2/5 | guesses the product name, gets nothing, looks the name up, then answers without re-running the query |
| the other 25 | 5/5 each | |

These three are the SQL cases a candidate model has to improve on. That an earlier run scored
3/5 on the Juniper question and this one 0/5 is a reminder that 5 repeats is a small sample:
run 10 when comparing models.

### 12.4 Minutes on an unseen meeting

In minutes-of-meeting-ai: pick one AMI meeting nobody has read, write its reference decision
list from AMI's annotations **before** any model output exists, then run the current best method
(exhaustive chunks, then the filter pass once it is built) and score it against the existing bar
with two scorers (decision 4). ES2003b and IB4005 stay as development meetings.

### 12.5 Document extraction (decision 5)

Either a small set of field-extraction questions on the CUAD contracts already in
`tests/fixtures/corpus/` (lawyer-annotated ground truth, `response_format: json_schema`, scored
field by field), or nothing until a dozen real failing review transcripts are captured.

## First trial: Qwen3.8-27B against the 14B, 9 November

Both ran on **vLLM 0.33.0**, which went live the same day (`682292a`; it had been 0.28.0). On the
new vLLM the 14B scored the same as on the old one, so the comparison below measures the model
alone. The candidate was `nvidia/Qwen3.8-27B-NVFP4`, revision `482ca0f3`, set up by
`docker-compose.trial-27b.yml` on the box (not committed): text only, 32768 window,
`--kv-cache-memory-bytes 6500000000`, `--gpu-memory-utilization 0.84`, `qwen3_coder` tool
parser, thinking off through the gateway's default.

- **Memory.** 18.96 GiB of weights on the native NVFP4 kernel. KV cache 79,579 tokens, 2.43
  full-size requests at once, against the 14B's 69,408 tokens and 2.12. 26.8 GB beside the
  embedder's 3.8 GB, so about 30.6 of 32.6 GB in use and **no room left for speech**.
- **Outage.** The first start looped for about 10 minutes, around 13:21-13:31. With the
  override replacing the whole command, vLLM 0.33 used its default `--gpu-memory-utilization`
  of 0.92, which does not fit beside the embedder.

10 repeats per case:

| Suite | 14B | 27B | Seconds per run, 14B / 27B |
|---|---|---|---|
| Original SQL cases | 110/110 | 107/110 | 3.5 / 3.5 |
| Document routing | 149/240 | **183/240** | 3.0 / 7.2 |
| Result-checked SQL | 262/280 | **270/280** | 3.1 / 3.9 |
| Traps | 50/50 | 34-37/50 | 2.8 / 8.5 |

What the numbers mean, after reading the 27B's actual answers:

- **Document routing.** It fixes five of the seven cases the 14B fails every time:
  - `no-database` 0 → 9;
  - `cross-register-and-terms` 1 → 8;
  - `lowrisk-expiry-date` 0 → 10;
  - `leave-policy` 2 → 10;
  - `paraphrase` 6 → 10.

  It newly fails `vague-show-contracts` (10 → 0): it answers "show me the contracts" from the
  `supplier_contracts` table instead of the library. That is a defensible reading.
- **Result-checked SQL.** It fixes `sql-follow-up-top-customer` (0 → 9), the follow-up the
  14B answered with a wrong number from a query that ran, and the gloves question (6 → 10).
  Its only remaining failure is `sql-distinct-products-juniper` (0/10), the same "since July"
  misreading as the 14B.
- **Original SQL cases.** It sometimes queries the archive database it should leave alone
  (8/10), and once answered a date question without a query.
- **Graders corrected for this trial**, each tested to still reject a wrong answer:
  - stated sums and gaps of returned figures now count as backed;
  - a one-number answer may come from a wider breakdown, provided the reply states the number;
  - wording like "there's no delivery rating" now counts as saying the data is missing.

  The first 27B SQL run, before these, scored 258/280. Every change only accepts more answers,
  and none of the 14B's failures was of a kind they accept, so the 14B's scores stand without
  a re-run.
- **Traps, two real problems and one fixture problem:**
  - The 27B checks its results. The trap stub answers only a few query shapes, so on "what
    data is in this database" it noticed results that made no sense, kept checking, and hit
    the 12-round limit (`AGENT_MAX_ITERATIONS`). That is a limit of the stub. These cases
    need a real database before they can compare models.
  - On "create a graph" it repeated the identical, correct query up to 11 times. That is a
    real repetition loop. Qwen's card recommends `presence_penalty` 1.5 in non-thinking mode,
    and the gateway does not set it yet.
  - It is about 2.5× slower on agent-style turns, because it runs more queries and writes
    longer answers. Plain SQL is close: 3.9 s against 3.1 s.

Reports: `database-agent/evals/results/v033-{14b,27b}-*.json`, on Amro's laptop only.

## The bar a candidate model must clear

Against the 14B's 12.1-12.5 numbers, on the same suites and repeat counts:

- **No regression** on the 11 original SQL cases (stays at or near 110/110).
- **Every trap at 9/10 or better**, up from the 14B's recorded 0.
- **Higher result-match rate** on 12.3, beyond what `compare.ts` calls chance.
- **Media routing up** on the families the 14B fails question-specifically.
- **Minutes pass bar met** on the unseen meeting.
- Latency recorded, not gated, except that a turn over ~30 s is a usability failure.

## Decisions waiting

1. **Trap schema.** Synthetic hospital-shaped schema and rows invented for the test
   (recommended: no client data in a repo or a hosted model), or replay against the real client
   database read-only.
2. **The result-checked SQL database.** A new database `eval_company` in the compose `postgres`,
   seeded from a script in the repo and extending the media fixtures' Sales company
   (recommended), or a separate throwaway container per run.
3. **Where 12.3 lives.** database-agent's harness, so the questions go through the real agent
   and prompt (recommended), or a standalone script here that calls `/v1` directly (simpler,
   but measures the model without the harness the user actually sees).
4. **Second minutes scorer.** Who scores the unseen meeting independently of the first.
5. **Extraction.** CUAD stand-in now, or wait for real transcripts.

## Order and size

12.1 first (no code, gives the baseline), then 12.2 and 12.3 together (they share the fixture
work), 12.4 in parallel in its own repo, 12.5 last. Rough size: 12.2 is five cases plus their
scripted tests; 12.3 is the seed script, 30-50 question/reference pairs, and one executor.

Out of scope: the model trial itself (that is Step 1 of the report's roadmap, after this),
`presence_penalty` (an A/B on 12.3 once it exists), and hosted-model pre-screening.
