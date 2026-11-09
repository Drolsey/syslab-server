# Current AI model setup of syslab-server, and why it performs poorly

Scope: read-only audit of the local repo (C:/Users/amro/Documents/Github/syslab-server, HEAD 60de379) plus primary web sources (Qwen model card, Qwen docs, vLLM docs, Qwen3 technical report, arXiv). No server was contacted, so nothing about the live box is verified beyond what the repo records. Repo citations are written as `file:line`. Line numbers are from the working copy on 2026-11-07.

Architecture note that frames everything below. syslab-server only *serves* the model. The agent loop, prompts, tool schemas and SQL rendering that the user actually hits live in a separate Next.js repo, `database-agent`, which is not in this checkout ([docs/architecture.md:42-50](docs/architecture.md)). syslab-server has its own smaller agent (`app/agent.py`, the operator UI at `/api/chat`), but the reported SQL failures come from database-agent talking to the `/v1` gateway ([HANDOVER.md:114-155](HANDOVER.md)). So many "model" failures are failures of a harness this repo cannot see.

---

## 1. What exactly is deployed

### Takeaway
One RTX 5090 (32 GB) runs vLLM v0.28.0 (pinned by digest) serving `Qwen/Qwen3-14B-AWQ` (pinned revision) at a 32,768-token window and 0.70 GPU memory utilization. Tool calling uses the `hermes` parser with `tool_choice` always `"auto"`. The `qwen3` reasoning parser is on, but thinking is turned **off** by default on every path. The app sends temperature 0.0 and no other sampling parameters. A second vLLM container serves Qwen3-Embedding-0.6B at 0.10 utilization. The 32B→14B swap on 9 Sept was a VRAM/context decision, not a quality one.

### Cited Findings
**Serving flags ([docker-compose.yml](docker-compose.yml))**
- Image `vllm/vllm-openai:v0.28.0@sha256:61fc8a89…` (digest-pinned) — [docker-compose.yml:29](docker-compose.yml)
- `--model Qwen/Qwen3-14B-AWQ --revision 31c69efc29464b6bb0aee1398b5a7b50a99340c3` — [docker-compose.yml:49-55](docker-compose.yml)
- `--max-model-len 32768`, raised 8192→16384 on 9 Sept and 16384→32768 on 14 Sept — [docker-compose.yml:56-91](docker-compose.yml)
- `--gpu-memory-utilization 0.70`. It was 0.85 for one day (8 Sept) and went back to 0.70 with the 14B — [docker-compose.yml:92-123](docker-compose.yml)
- `--enable-auto-tool-choice --tool-call-parser hermes` — [docker-compose.yml:124-126](docker-compose.yml)
- `--reasoning-parser qwen3`, added 15 Sept because `<think>…</think>` text had been landing inline in `content` for database-agent — [docker-compose.yml:127-139](docker-compose.yml); [HANDOVER.md:1448-1457](HANDOVER.md)
- No quantization flag, no `--kv-cache-dtype`, no `--generation-config`, no `--tool-strict-level`, no structured-output backend flag. AWQ is picked up from the checkpoint — [docker-compose.yml:36-139](docker-compose.yml)
- Embedding container: `Qwen/Qwen3-Embedding-0.6B`, `--runner pooling`, `--max-model-len 1024`, `--gpu-memory-utilization 0.10` (~3.3 GiB of the 32,607 MiB card), port 8001 — [docker-compose.yml:157-209](docker-compose.yml)
- `models.toml` declares chat = Qwen3-14B-AWQ / vllm / `context_tokens = 32768`. It is documentation only: nothing reads it into `app/llm.py` — [models.toml:1-12](models.toml)

**Request-side settings**
- `.env.example`: `LLM_MODEL=Qwen/Qwen3-14B-AWQ`, `LLM_TIMEOUT=300`, and `LLM_THINK=false` with the comment "Slower, rarely worth it here" — [.env.example:6-11](.env.example); [app/config.py:37-40](app/config.py)
- `app/llm.py` sends `temperature` (default **0.0**) and `chat_template_kwargs: {"enable_thinking": LLM_THINK}`. When tools are present it always sends `tool_choice: "auto"`. It never sends `top_p`, `top_k`, `min_p`, `presence_penalty` or `repetition_penalty` — [app/llm.py:34-58](app/llm.py); [app/llm.py:223-230](app/llm.py)
- The gateway (`/v1`, used by database-agent) passes the client's body through unchanged. It adds `enable_thinking: False` only if the caller did not set `chat_template_kwargs`, and clamps `max_tokens` to what is left of the window — [app/gateway.py:197-211](app/gateway.py); [app/gateway.py:93-139](app/gateway.py)
- Internal agent limits: `MAX_TOOL_STEPS=10`, `DB_MAX_ROWS=200`, `TOOL_RESULT_BUDGET=12,000` chars, a 12-tool schema, and a system prompt of about 8.8k characters with ~35 "never/always" rules — [app/config.py:42,181,188](app/config.py); [app/agent.py:373-497](app/agent.py); [app/agent.py:703](app/agent.py)

**VRAM budget (measured from the startup log, 16 Sept)**
- `Available KV cache memory: 9.27 GiB`, `GPU KV cache size: 60,768 tokens`, `Maximum concurrency for 32,768 tokens per request: 1.85x` (3.71x at 16,384). Weights plus non-torch 11.22 GiB, activation 1.46, CUDA graphs 0.52. That leaves about 8.4 GiB of the card outside vLLM's budget — [docs/models.md:274-326](docs/models.md)
- Sustained throughput under concurrent load has never been measured. `scripts/bench_gateway.py` exists but has not been run — [HANDOVER.md:1459-1466](HANDOVER.md); [docs/models.md:328-333](docs/models.md)

**Model history**
- Qwen3-8B-AWQ was the smoke-test model and scored 5/8 on `check_agent`. It hallucinated SQL when asked to sum a spreadsheet — [docs/models.md:537-543](docs/models.md); [docs/plans/step-03-model-gateway.md:255-268](docs/plans/step-03-model-gateway.md)
- Qwen3-32B-AWQ ran at 8192 context and 0.85 utilization and scored 6/8 — [docs/models.md:21-46](docs/models.md)
- **Swap to 14B on 9 Sept, and why.** The 32B left 5.16 GiB of KV cache (21,135 tokens), so the window had to stay at 8192. The internal agent's undroppable floor is 4,213 tokens (system prompt 2,069 plus 12 tool schemas ~2,144), which left 3,979 tokens. One maximum-size tool result is 4,506 tokens, so it could not fit. The 14B's 9.29 GiB of weights (vs 18.00) bought both window and concurrency — [HANDOVER.md:2408-2446](HANDOVER.md); [docker-compose.yml:37-48](docker-compose.yml)
- The 14B scored 6/8 `check_agent` and 8/8 `check_search`, the same totals and the same two failing scenarios (3 and 7) as the 32B, with faster traces (2.4 s vs 3.9 s) — [docs/models.md:253-268](docs/models.md); [HANDOVER.md:2482-2491](HANDOVER.md)
- **Stale line:** the "Where it stands" section still says "`check_agent` 6 of 8 against the live 32B model" — [HANDOVER.md:861](HANDOVER.md)
- The original Ollama benchmark chose the Qwen3 family "specifically for tool-calling strength" over gemma3:27b — [docs/models.md:21-28](docs/models.md)

### Inferences
- The 14B was chosen because its *infrastructure* trade (window plus concurrency) was clearly better and `check_agent` did not regress. But `check_agent` cannot see the failure class the user complains about. Every scenario opens with a question that demands a tool, none builds up tool-free turns, and none has a text format that imitates a tool ([HANDOVER.md:805-808](HANDOVER.md)). "14B matches 32B" is therefore proven only on an easy benchmark.
- 0.70 + 0.10 = 0.80 of the card is reserved by the two vLLM processes. Roughly 6–8 GiB is nominally free for the planned speech step. A bigger chat model would compete with that budget.

### Gaps
- What database-agent sends: temperature, `top_p`, `chat_template_kwargs`, whether it trims history, its tool schemas and `OUTPUT_FORMAT`. That repo is not in this checkout.
- **Possible contradiction to check on the box.** The 15 Sept note says Qwen3's thinking text was reaching database-agent inline ([HANDOVER.md:1448-1453](HANDOVER.md)). But the gateway defaults `enable_thinking` to False ([app/gateway.py:208](app/gateway.py)). Either database-agent sets `chat_template_kwargs` itself, or it reaches vLLM directly (both now share one compose network) and so bypasses the gateway's defaults and clamp. The 14 Sept "max_tokens=32000 rejected outright against 16384" event ([docker-compose.yml:73-79](docker-compose.yml)) is also odd, since the gateway's clamp exists to prevent exactly that. It can, however, happen through the gateway when the prompt estimate alone exceeds the window, because the clamp then deliberately does nothing ([app/gateway.py:131-132](app/gateway.py)). Unresolved.

---

## 2. What failures are recorded

### Takeaway
Most recorded failures were caused or amplified by the harness rather than by raw model ability:
- a ```sql format example that acts as a decoy (0/18 vs 18/18 with `tool_choice:"required"`);
- prose history poisoning;
- a shipped frontend mock that invented the "fabricated rows";
- a tool description whose example values the model copied into the wrong parameter;
- a retry detector that one sentence of preamble defeats.

The decisive comparison: on the same unfixed production harness, the 14B failed all five traps and Claude passed all five. That isolates a real robustness gap in the model, on top of the harness defects that create the traps.

### Cited Findings
- **F1. No tool call at all in 10 website runs; invented "Enterprise Customer A…J".** The model emitted a ```sql fence as prose, and the UI rendered it as though a query had run. The cause was database-agent's `OUTPUT_FORMAT`, which says "```sql — the query you ran. Always show it." That line is a worked format template that imitates the tool.
  - Clean context: 4/4 `run_sql`. With six real prose turns before it: 0/4. Removing `OUTPUT_FORMAT`: 4/4. A prohibition added next to the example: still 0/4.
  - Across 3 questions × 6 runs: as shipped 0/18; bullet moved last 11/18; line removed 12/18; `tool_choice:"required"` 18/18.
  - Ruled out explicitly: `enable_thinking` on vs off (0/4 either way) and temperature 0.0 vs 0.7 (0/4 either way). HANDOVER concludes "This is not a 14B tool-calling weakness."
  - Sources: [HANDOVER.md:657-819](HANDOVER.md), tables at 694-700, 718-724, 731-737, 755-760; conclusion at 797-805.
- **F2. "Fabricated rows" in production were the frontend's.** `components/chat/blocks/sql/mockExecute.ts` shipped. Auto-run executed the model's un-run ```sql fence against a random generator: rows cycling Amina/Chen/Diego, `rowCount = 8 + floor(random()*8)`. "The model fabricated the *query*; the frontend fabricated the answer." Fixed 10 Sept — [HANDOVER.md:114-200](HANDOVER.md), esp. 157-189.
- **F3. Decoy reproduced in production.** A chat that opened with "hi" and then asked "top 5 hospitals" gave invented rows. Asking the same question as the first message gave correct rows — [HANDOVER.md:114-155](HANDOVER.md)
- **F4. Fabrication against the real client DB, with zero tool steps.** The reply listed "Hospital A 1200 … Hospital E 1000". After "actually run that query", the real answer came back: Sandton 1441, Morningside 1418 … — [HANDOVER.md:449-468](HANDOVER.md)
- **F5. The retry detector is narrower than believed.** `bareSqlFence` only fires when the fence is the very first thing in the reply, so "one sentence of preamble is enough to defeat the detector" — [HANDOVER.md:470-500](HANDOVER.md)
- **F6. Widening the retry made things worse.** On "Write me a SQL query… Do not run it", the widened retry ran the query anyway (1 of 2 became 2 of 2). It was withdrawn. "A detector widened to close a false negative bought a false positive that was worse than the bug" — [HANDOVER.md:247-274](HANDOVER.md)
- **F7. Fabrication moved to charts and prose.** After the retry shipped, later turns produced chart `"data": [15, 20, 10]` and "Free 15 · Pro 20 · Enterprise 10" with no query in the log. Those sum to 45, against the 100-row table just shown. "The dangerous form of this bug does not involve SQL at all" — [HANDOVER.md:620-640](HANDOVER.md)
- **F8. ESG tool: called every time, wrong parameter.** With the tool description as written, 14/24 correct: for a single hospital the model filled `hospital_group` with the description's example ("Mediclinic"). Failures were hospital-specific and stable (Kloof, Midstream, Muelmed 0/3), which reads as unfamiliar names collapsing onto the example rather than sampling noise. Rewriting the description as a rule gave 24/24. Every failure was silent: the report was named `esg-report-Mediclinic-2026.pdf` — [HANDOVER.md:325-358](HANDOVER.md); revision at [HANDOVER.md:278-285](HANDOVER.md)
- **F9. "Create a graph" produced a mermaid diagram of column names.** `OUTPUT_FORMAT` wording steered this — [HANDOVER.md:641-648](HANDOVER.md)
- **F10. Identifier quoting costs round trips.** "How many records" took 3 attempts (`public.Report` → `Report` → `public."Report"`). All 35 columns need quoting. The model self-corrected from the error text — [HANDOVER.md:436-452](HANDOVER.md); [HANDOVER.md:2620-2630](HANDOVER.md)
- **F11. Claude comparison: 14B failed 5 of 5 traps, Claude passed 5 of 5,** on the same unfixed production harness:

  | Trap | 14B | Claude |
  |---|---|---|
  | ```sql fence after prose turns | 0 steps | passed |
  | Preamble bypass | 0 steps | passed |
  | ESG Kloof/Midstream | 0/3 | passed |
  | "create a graph" | mermaid | passed, real chart |
  | Monitoring views | not recorded | named them correctly |

  Claude first ran a query to confirm the hospital's spelling, as the tool description asked; the 14B skipped that and used the example value. HANDOVER: "Claude is not resisting the trap so much as doing the work that makes the trap irrelevant… all four changes are hardening for weaker models" — [HANDOVER.md:205-232](HANDOVER.md). Claude also over-claimed completeness once before disclosing truncation — [HANDOVER.md:303-306](HANDOVER.md)
- **F12. Internal agent (`app/agent.py`) failures.**
  - Both the 8B and the 32B hallucinated SQL because of the `query_to_excel` description; fixed with an explicit exclusion — [docs/plans/step-03-model-gateway.md:261-268](docs/plans/step-03-model-gateway.md)
  - The `check_agent` 6/8 failures are judged to be test strictness: the correct answer reached by a different tool path — [docs/models.md:259-265](docs/models.md); [HANDOVER.md:2497-2501](HANDOVER.md)
  - Recorded lessons: an example in a tool description is indistinguishable from an argument (searched "Meridian" unprompted); an error dumping 18 filenames made the model claim it had no DB access — [HANDOVER.md:2538-2549](HANDOVER.md)
- **F13. Workarounds coded for model quirks** (evidence that the model often fills tool parameters wrongly):
  - `ARGUMENT_ALIASES`: "qwen3 reliably calls write_excel with `data` and `sheet_name` rather than `rows` and `sheet`" — [app/agent.py:506-528](app/agent.py)
  - `TOOL_ARGUMENT_ALIASES` for `query_as`, `query`, `statement` and so on — [app/agent.py:530-566](app/agent.py)
  - A promise-detector nudge for "let me start…" stalls — [app/agent.py:650-666](app/agent.py)
  - A repeated-identical-failing-call guard — [app/agent.py:861-880](app/agent.py)
- **F14. Thinking exhausted the token budget at `max_tokens: 200`** in the Step 3.0 probe, which is why it was turned off everywhere — [docs/plans/step-03-model-gateway.md:301-312](docs/plans/step-03-model-gateway.md)
- **F15. Context-window failures:** `max_tokens=32000` rejected at 16384, and a schema-plus-history prompt measured at 16,334 tokens. Both were addressed by moving to 32K — [HANDOVER.md:1410-1423](HANDOVER.md)
- **F16. Permission gap:** the playbook promises an `item_sustainability` table and a `Hospitals` table that the agent's DB role cannot see — [HANDOVER.md:293-301](HANDOVER.md)

### Inferences
- F1–F7 and F9 share one root cause: a harness that gives the model a cheaper text channel that looks like success (format examples, a UI that renders any fence as a query), plus no check that a number in the reply came from a query in this turn.
- F8 and F11 show where model capability is the real factor. Given the same bad scaffolding, the 14B copied example values and skipped verification steps that Claude carried out.
- The user's "fails the most basic tasks" is consistent with this record: most failures are silent (no error, plausible output), which is why debugging takes so long.

### Gaps
- No recorded transcripts in this repo of document "review" or "data extraction" failures specifically: PDF/Excel review, field extraction, structured output. The user's complaint covers them, but the evidence on file is almost all SQL and tool-routing.
- The 32B was never run against the poisoned-history replay ("plausible and still untested", [HANDOVER.md:804](HANDOVER.md)). Thinking on was tested only on the decoy, not on SQL correctness or extraction.

---

## 3. Thinking mode, sampling, parser, quantization, context

### Takeaway
Thinking is off everywhere. The app samples greedily at temperature 0.0, which Qwen explicitly advises against, and sends no presence penalty, which Qwen "strongly recommends" at 1.5 for quantized models. `hermes` is the correct tool parser for Qwen3. AWQ-INT4 costs about 1–3 benchmark points. Context truncation was a real cause at 8K/16K and is largely solved at 32K for the internal agent.

The biggest untested configuration lever is thinking mode: Qwen3-14B scores **70.4 on BFCL v3 with thinking vs 61.5 without**.

### Cited Findings
**Thinking**
- Off by default: `LLM_THINK=false` ([.env.example:10-11](.env.example)), and the gateway defaults `enable_thinking` to False ([app/gateway.py:204-208](app/gateway.py)).
- Turned off originally because a 200-token probe ran out inside `<think>` ([docs/plans/step-03-model-gateway.md:301-312](docs/plans/step-03-model-gateway.md)).

**Sampling**
- The app's default temperature is 0.0 (greedy). No top_p/top_k/min_p/presence_penalty is sent — [app/llm.py:34-58](app/llm.py); [app/llm.py:228](app/llm.py)
- Qwen's official recommendations:
  - Thinking mode: Temperature 0.6, TopP 0.95, TopK 20, MinP 0, with the warning "DO NOT use greedy decoding, as it can lead to performance degradation and endless repetitions."
  - Non-thinking mode: Temperature 0.7, TopP 0.8, TopK 20, MinP 0.
  - presence_penalty 0–2, "strongly recommend setting this value to 1.5 for quantized models" (may cause language mixing).
  - [Qwen3-14B-AWQ model card](https://huggingface.co/Qwen/Qwen3-14B-AWQ)
- Qwen's own function-calling examples use `temperature=0.7, top_p=0.8, repetition_penalty=1.05` — [Qwen function calling docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
- In the decoy scenario only, temperature 0.0 vs 0.7 made no difference (0/4 both) — [HANDOVER.md:801-802](HANDOVER.md)

**Thinking vs non-thinking scores, Qwen3-14B** ([Qwen3 Technical Report, Tables 15/16](https://arxiv.org/html/2505.09388v1))

| Benchmark | Thinking | Non-thinking |
|---|---|---|
| BFCL v3 | 70.4 | 61.5 |
| LiveBench | 71.3 | 59.6 |
| IFEval strict | 85.4 | 84.8 |
| Arena-Hard | 91.7 | 86.3 |
| Multi-IF | 74.8 | 72.9 |

For comparison, Qwen3-32B: BFCL 70.3 thinking / 63.0 non-thinking. Qwen3-30B-A3B: 69.1 / 58.6.

**Tool parser**
- Qwen says: "We recommend using Hermes-style tool use for Qwen3 to maximize function calling performance." Its example is `vllm serve Qwen/Qwen3-8B --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser deepseek_r1`. It warns against stopword/ReAct templates for reasoning models, and that "there are corner cases where tool calls are generated but they are malformed and cannot be parsed. For production code, we should try parsing by ourselves." — [Qwen function calling docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
- vLLM's current docs list `hermes` for Qwen2.5/QwQ and `qwen3_xml` for Qwen3-Coder only — [vLLM tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling.html)

**Unconstrained tool arguments under `"auto"`**
- Under `tool_choice:"auto"`, vLLM does not constrain output unless a tool has `strict: true` or `--tool-strict-level` is set: "vLLM extracts tool calls from raw text, so arguments may occasionally be malformed."
- Under `"required"` or a named tool, structured outputs are used. The model "is guaranteed to generate one or more tool calls", but that is "a validly-parsable function call - not a high-quality one."
- Source: [vLLM tool calling docs (latest)](https://docs.vllm.ai/en/latest/features/tool_calling.html). The app always sends `"auto"` and never `strict` ([app/llm.py:53-55](app/llm.py)).

**AWQ quantization**
- Official Qwen3-14B card, BF16 → AWQ-INT4:

  | Mode | LiveBench | GPQA | MMLU-Redux | AIME24 |
  |---|---|---|---|---|
  | Thinking | 71.3 → 70.0 | 64.0 → 62.1 | 88.6 → 88.5 | 79.3 → 77.0 |
  | Non-thinking | 59.6 → 57.4 | 54.8 → 53.8 | 82.0 → 81.5 | n/a |

  [Qwen3-14B-AWQ model card](https://huggingface.co/Qwen/Qwen3-14B-AWQ)
- Independent study, Qwen3-14B with its own AWQ w4 g128 (not Qwen's checkpoint): MMLU 78.5 → 75.9, zero-shot average 74.4 → 70.7, C4 PPL 12.0 → 13.3. w8 is near-lossless. The paper says Qwen3 "maintains competitive performance at higher bit-widths (4-bit and above)" — [Zheng et al., An Empirical Study of Qwen3 Quantization, arXiv 2505.02214](https://arxiv.org/html/2505.02214v1)

**Context**
- Native context is 32,768 tokens. YaRN up to 131K is possible but "potentially impact[s] performance on shorter texts", so it is not advised under 32K — [Qwen3-14B-AWQ model card](https://huggingface.co/Qwen/Qwen3-14B-AWQ). The deployment uses exactly the native 32,768 with no YaRN — [docker-compose.yml:79-82](docker-compose.yml)
- Internal-agent floor: 4,213 tokens. At 8192 that left 3,979, less than one 4,506-token tool result. At 32,768 it leaves ~28.5K — [HANDOVER.md:2408-2425](HANDOVER.md)
- History trimming drops the oldest exchanges and records a `trim_history` step. The estimator uses chars/3 + 256, which is deliberately pessimistic — [app/llm.py:118-209](app/llm.py); [app/agent.py:769-795](app/agent.py)
- database-agent hit a 16,334-token schema-plus-history prompt at 16K — [HANDOVER.md:1410-1416](HANDOVER.md)

**History handling**
- Qwen says multi-turn history should hold only final outputs, not thinking content — [Qwen3-14B-AWQ model card](https://huggingface.co/Qwen/Qwen3-14B-AWQ). `app/agent.py` keeps only `content` and `tool_calls` in the assistant turns it replays, which complies — [app/agent.py:800-808](app/agent.py)

### Inferences
- **Config defects worth fixing regardless of model:**
  1. Temperature 0.0 is greedy decoding, contrary to Qwen's guidance.
  2. No presence penalty on a quantized model.
  3. `tool_choice:"auto"` with no `strict`, so tool arguments are free text. The alias tables in F13 are the symptom.
  4. Any database-agent request that sends no temperature gets vLLM's defaults. **To verify:** vLLM's `--generation-config auto` typically loads the model repo's generation_config.json. For Qwen3 that holds the thinking-mode values 0.6/0.95/20, which would then apply even with thinking off. Check the box's startup log for the "Default sampling parameters have been overridden" line.
- **Thinking mode is the single largest capability lever for tools** (+8.9 BFCL points for the 14B). It was disabled for latency, and on the evidence of one 200-token probe. It was never measured on SQL correctness or extraction. Per-turn control is possible: `/think` / `/no_think` soft switches per the model card, or `chat_template_kwargs` per request.
- AWQ is unlikely to be the main cause: it costs 1–3 points against the 9-point thinking gap. An FP8 or BF16 14B would not fit as comfortably alongside the planned speech budget.
- Context truncation is no longer a likely cause for the internal agent at 32K. For database-agent it depends on its schema dump, since 35 quoted columns plus history reached 16K.
- `hermes` + `qwen3` are the right parsers. The parser is not the cause of the recorded failures: tool calls parsed 4/4 direct, streamed and through the gateway ([HANDOVER.md:680-686](HANDOVER.md)).

### Gaps
- Whether `strict: true` / `--tool-strict-level` exists in vLLM **v0.28.0** specifically. The docs read were "latest". Check before relying on it.
- No measurement anywhere of this deployment with Qwen-recommended sampling, presence_penalty 1.5, or thinking on, on the real SQL/extraction workload.

---

## 4. Known issues of Qwen3-14B with tool calling and vLLM parsers

### Takeaway
No published issue explains the recorded failures as a parser or model bug. Known issues are edge cases:
- Hermes JSON-decode errors on extra text;
- streaming token mishandling;
- reasoning parsers mis-splitting when `<think>` is missing;
- a measured, small tool-name hallucination rate at the 14B size.

The recorded failures (decoy, history imitation, example-value copying) are behavioural, not parsing.

### Cited Findings
- vLLM issue #17790 (v0.8.5, Qwen3-32B): the Hermes parser logs JSON decode errors when the output holds multiple JSON blocks or extra text — [vllm#17790](https://github.com/vllm-project/vllm/issues/17790) (seen via search snippet, not opened)
- vLLM issue #19056: the Hermes streaming path mishandled a specific token Qwen3 emitted — [vllm#19056](https://github.com/vllm-project/vllm/issues/19056) (search snippet, status unknown)
- vLLM issue #19001: a DeepSeek-R1-0528-Qwen3-8B variant with hermes leaked `<think>` text into content — [vllm#19001](https://github.com/vllm-project/vllm/issues/19001) (snippet)
- If a model's output omits the opening `<think>`, the qwen3 reasoning parser cannot split it, and `reasoning_content` stays null — [Tencent Cloud write-up](https://developer.cloud.tencent.cn/article/2581649) (secondary, Qwen3-VL)
- Qwen acknowledges malformed, unparseable tool calls as "corner cases" and recommends parsing them yourself in production — [Qwen function calling docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
- An ICML 2026 paper on BFCL multi-turn reports tool-existence hallucination for Qwen3 that is non-monotone in size: 0% at 0.6B, **1.87% at 14B**, 0% at 32B. A registry-check-and-reprompt middleware removed all 14 pooled Qwen3 fabrications — [ICML 2026: Tool-Existence Hallucination Is Tier-Dependent…](https://icml.cc/virtual/2026/67404) (via search summary; paper not opened)
- Community reports (anecdotal, small samples): Qwen3 invented a non-existent tool and a stock price, and ignored a system prompt forbidding search — [LessWrong](https://www.lesswrong.com/posts/Aqfd3peQmtxJsgq6d/how-do-you-know-an-llm-actually-used-its-tools)
- No Qwen3-14B-2507 refresh is referenced anywhere. The model card still points to the original hybrid-thinking release, with Qwen-Agent recommended for tool calling — [Qwen3-14B-AWQ model card](https://huggingface.co/Qwen/Qwen3-14B-AWQ)

### Inferences
- The repo's own probes show clean parsing: streaming deltas with a stable index and valid concatenated JSON, and string arguments handled ([docs/plans/step-03-model-gateway.md:314-332](docs/plans/step-03-model-gateway.md)). So known parser issues are not a primary explanation. Keep the defensive parsing in `_coerce_arguments` regardless.

### Gaps
- I did not open the GitHub issues or the ICML paper. Their status and fixes in vLLM v0.28.0 are unverified.
- I found no published Qwen3-14B text-to-SQL (BIRD/Spider) execution-accuracy figure. Search returned only coder or fine-tuned 14B variants, such as Arctic-Text2SQL-R1-14B at 72.22% on BIRD, per a secondary blog ([besthub.dev](https://www.besthub.dev/articles/2026-text2sql-model-showdown-which-one-performs-best-fbb17d2f12ae)).

---

## 5. What persists with any model of this size, and harness techniques that help regardless

### Takeaway
About half the recorded failures are fixable in config or harness without changing the model, and several of those fixes are already measured: `tool_choice:"required"` gave 18/18, a rule instead of example values gave 24/24, and removing the mock fixed fabricated rows. What persists for a ~14B model:
- weaker resistance to decoys and self-imitation over long prose histories;
- skipping verification steps;
- copying example values for unfamiliar entities;
- asserting numbers without a query.

Claude passing all five traps marks that gap. Harness techniques that close most of it regardless of model: constrained decoding, conditional forced tool calls, a "numbers must come from a query in this turn" verifier, SQL EXPLAIN/repair loops, and schema pruning.

### Cited Findings
- `tool_choice:"required"` fixed the decoy completely (18/18) but cannot be blanket: it would force a query on "hi". The proposed shape is a retry on detection, which is model-agnostic — [HANDOVER.md:755-779](HANDOVER.md)
- Render the displayed SQL from the tool call's real `sql` argument and `query_id`, not from a model-authored fence. That removes the model's reason to re-type it, for every provider — [HANDOVER.md:776-786](HANDOVER.md); [HANDOVER.md:780-786](HANDOVER.md)
- "The decoy misleads any model; a stronger one just resists it longer" — [HANDOVER.md:787-789](HANDOVER.md)
- vLLM structured outputs:
  - available as `structured_outputs` (`choice`, `regex`, `json`, `grammar`, `structural_tag`) and as `response_format: {type: "json_schema"}`;
  - backends xgrammar and guidance, default `auto`;
  - includes a simplified SQL EBNF grammar example;
  - grammars touching think or tool-call boundaries must use token IDs;
  - the `guided_*` parameters were removed in v0.12.0.
  - [vLLM structured outputs docs](https://docs.vllm.ai/en/latest/features/structured_outputs.html)
- Named or required tool choice gives schema-valid arguments through structured outputs. `strict: true` tools pin argument schemas even under `auto` — [vLLM tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling.html)
- Execution-guided SQL generation (sample several candidates, pick the most consistent by execution result) lets "smaller, cost-effective models… surpass" o1/o3-mini/R1-style reasoning at up to 30x lower inference cost, per the abstract; no accuracy numbers are in the abstract — [arXiv 2503.24364](https://arxiv.org/abs/2503.24364)
- A registry-check middleware removed all observed Qwen3 tool-name fabrications on BFCL multi-turn — [ICML 2026](https://icml.cc/virtual/2026/67404)
- database-agent's `sql-guard.ts` already allows `EXPLAIN` — [HANDOVER.md:186-189](HANDOVER.md)
- The connector's `SET LOCAL statement_timeout` is likely a no-op outside a transaction, and there is no `default_transaction_read_only` — [docs/plans/pivot-inference-provider.md:122-133](docs/plans/pivot-inference-provider.md)
- The internal agent already hands the model `query_as` (pre-quoted identifiers) to paste into SQL. That fixes the quoting class F10 at the source — [app/agent.py:416-420](app/agent.py)

### Inferences
**Classification of recorded failures**

| # | Failure | (a) Config/serving | (b) Prompt/harness | (c) Model capability |
|---|---|---|---|---|
| F1/F3/F4 | ```sql fence instead of `run_sql`, after prose turns | — | **Primary**: `OUTPUT_FORMAT` decoy, no forced tool call | Secondary: Claude resists, 14B does not |
| F2 | Invented rows | — | **Entirely**: shipped `mockExecute` | — |
| F5/F6 | Retry misses on preamble; widened retry over-fires | — | **Entirely**: detector design | — |
| F7 | Chart/prose figures with no query | — | **Primary**: no "figure must cite a query this turn" check | Likely persists in the 14B class |
| F8 | ESG wrong parameter | — | **Primary**: example values in the description (fixed 24/24) | Secondary: copies examples for unfamiliar names; skips the spelling check Claude did |
| F9 | Mermaid for "graph" | — | **Primary**: format wording | Minor |
| F10 | Unquoted identifiers, 3 attempts | — | **Primary**: schema presentation (use `query_as`, quoted names) | Minor; self-corrects |
| F12/F13 | Wrong argument names, hallucinated SQL from a tool description | **Partly**: `auto` with no `strict` / structured outputs | **Partly**: description wording | Partly: 8B worse than 14B/32B |
| F14 | Thinking exhausts budget; thinking inline | **Entirely**: max_tokens 200 probe, missing reasoning parser (fixed) | — | — |
| F15 | 16K window overflow, max_tokens 400 | **Entirely**: window, clamp (fixed at 32K) | Schema-plus-history size | — |
| F16 | Playbook promises invisible tables | DB grant | Playbook text | — |
| Open | Greedy temperature 0, no presence penalty, thinking off | **Config**, cheap to change | — | Thinking off costs ~9 BFCL points |
| Open | `check_agent` 6/8 | — | Test strictness | — |

**What persists with any model of ~14B size, even with a clean harness**
- Lower robustness to salient distractors and long prose histories: the 0/18 needed an architectural fix, and wording only reached 11–12/18.
- Skipping multi-step verification the instructions ask for.
- Entity-name confusion for unfamiliar names.
- Asserting quantities without evidence.
- A small rate of tool-name hallucination (1.87% at 14B).
- Lower tool-use scores without thinking (61.5 BFCL).

The record already shows 14B ≈ 32B: 6/8 vs 6/8 on `check_agent`, BFCL 70.4 vs 70.3 thinking. So a same-family 32B is unlikely to close the Claude gap on its own. That last part is an inference: the 32B was never tested on the traps.

**Harness techniques that help regardless of model**, roughly ordered by evidence and cost:
1. Remove format templates that imitate tool output. Render SQL and figures from tool-call data, never from model text (measured: 0/4 → 4/4).
2. Conditional `tool_choice:"required"` or a named tool when the turn is data-seeking (measured 18/18). Decide "data-seeking or not" with a small structured-output classifier (`structured_outputs.choice`) rather than a regex on the reply.
3. Mark all tool schemas `strict: true` (or `--tool-strict-level`, if v0.28 supports it) so arguments are schema-constrained under `auto`. Use `response_format json_schema` for any extraction or review output. Replace example values in tool descriptions with rules.
4. A post-hoc verifier: any number, table or chart in a reply must trace to a `query_id` or tool result from this turn, otherwise re-prompt or block. Also add a registry check for tool names.
5. A SQL loop: run `EXPLAIN` before execution, feed errors back, cap retries, and possibly sample N candidates and pick by execution consistency. Enforce a read-only transaction and a real statement timeout.
6. Schema pruning: send only the relevant tables and columns with pre-quoted `query_as` names, not the full 35-column dump. This also shrinks the 16K-class prompts.
7. Sampling and mode: Qwen's recommended non-thinking values (0.7 / 0.8 / 20 / 0, presence 1.5), and thinking on (0.6 / 0.95 / 20) for SQL-writing and extraction turns only. A large `max_tokens` for thinking turns: the 200-token probe was the wrong test.
8. A regression eval that includes the five production traps and poisoned-history replays. `check_agent` cannot see them. Run it across configs (think on/off, sampling) and candidate models before any swap.

### Gaps
- No measurement exists of fixes 3–7 on this deployment. The only measured harness fixes are the `OUTPUT_FORMAT` removal, `tool_choice:"required"` and the ESG description rewrite.
- No access to database-agent's current code, to confirm which of the 9–10 Sept fixes (retry, mock removal, auto-run guard, `pr/agent-hardening`) are merged and deployed ([HANDOVER.md:308-323](HANDOVER.md)).
- Whether the user's newer failures (reviewing, extraction) occur in the internal agent or in database-agent. No transcripts are on file.
