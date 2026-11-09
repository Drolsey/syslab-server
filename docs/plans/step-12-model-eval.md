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

### 12.3 SQL that is checked by its result

30-50 natural-language questions against a **real, seeded Postgres database**, each with a
reference query; a run passes when its result set matches the reference's (row order ignored
unless the question asks for one). This is the only SQL evidence worth having: public BIRD and
Spider scores carry 50-66% annotation errors and none of the candidate models has one. Mix:
joins, aggregation, dates and ranges, `NULL` handling, quoted identifiers, a question with no
answer in the data, and five asked after unrelated turns. Needs a small executor in the harness
that runs read-only against that database instead of the per-case stub (decision 2).

### 12.4 Minutes on an unseen meeting

In minutes-of-meeting-ai: pick one AMI meeting nobody has read, write its reference decision
list from AMI's annotations **before** any model output exists, then run the current best method
(exhaustive chunks, then the filter pass once it is built) and score it against the existing bar
with two scorers (decision 4). ES2003b and IB4005 stay as development meetings.

### 12.5 Document extraction (decision 5)

Either a small set of field-extraction questions on the CUAD contracts already in
`tests/fixtures/corpus/` (lawyer-annotated ground truth, `response_format: json_schema`, scored
field by field), or nothing until a dozen real failing review transcripts are captured.

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
