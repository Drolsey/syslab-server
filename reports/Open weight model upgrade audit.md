# Fix the harness, then move to 27B

Most of what makes Qwen3-14B look broken on syslab-server is the harness around it, and part of the rest is configuration. Some of it really is the model. The worst failures on record are SQL fabrications. Each one traces first to scaffolding. A ```` ```sql ```` format template in database-agent's prompt imitates a tool call: as shipped, the model called `run_sql` in **0 of 18** runs, and with `tool_choice:"required"` it called it in **18 of 18**. A shipped frontend mock invented the "fabricated rows". Tool descriptions carried example values that the model copied into arguments. On top of that sit four configuration defects that cost measurable quality: greedy decoding at temperature 0.0, no presence penalty on a quantized model, thinking turned off everywhere, and unconstrained tool arguments. Underneath both is a real capability gap. On the same unfixed harness, the 14B fell into every production trap it was recorded on (four of five), while Claude avoided all five, and Berkeley's BFCL v4 puts it at **34.75% on multi-turn tool use**.

Meeting-minutes extraction, the second workload with recorded evidence, splits the same way. A one-pass prompt recovered **2 of 10** reference decisions even from AMI's perfect manual transcript. Splitting the transcript into chunks and asking for every decision recovered **9 of 9** on one meeting and **about 6 of 10** on the other, but buried them among 60-90 candidates. The harness caused the misses. What is left is a model limit: the 14B cannot tell a decision the group agreed from a proposal it discussed.

The recommended path costs nothing for its first two stages:

1. This week, fix the configuration and remove the decoys. Turn the recorded traps into a regression eval.
2. Then trial Qwen3.8-27B, or the Qwen3.6-27B that has already been run on a 5090, in NVFP4. It fits in roughly 26 GiB beside the embedder. In Qwen's own tables its generation of 27B models beats the old Qwen3-235B by 14 points on BFCL-V4. The swap takes the GPU headroom that was set aside for speech, and decode speed roughly halves.

Extra hardware buys modest gains at shortage prices, so wait until the eval shows a 27B is not enough:

- A second 5090 unlocks no new class of model.
- 128 GB of RAM runs 120B-class models for one user at a time, at 21-48 tok/s.
- An RTX PRO 6000 is selling at nearly double its launch price.

Hosted open-weight APIs are one to two orders of magnitude cheaper than renting or buying a GPU, but no specialist provider runs in the UAE. Client data should therefore be routed by sensitivity. Regulated data stays on the box or in the country. Synthetic eval traffic and non-sensitive work can go to providers that keep no copy of requests (zero data retention, or ZDR).

## Most failures came from the harness, but a real 14B gap remains

The box runs vLLM v0.28.0, pinned by image digest. It serves `Qwen/Qwen3-14B-AWQ` at a pinned revision with a 32,768-token window and `--gpu-memory-utilization 0.70`. Tool calls use the `hermes` parser and the `qwen3` reasoning parser. Thinking is disabled on every path, and the app sends temperature 0.0 and no other sampling parameters ([docker-compose.yml](../docker-compose.yml); [app/llm.py:34-58](../app/llm.py)). A second vLLM container serves Qwen3-Embedding-0.6B at 0.10 utilization. The startup log measured **9.27 GiB of KV cache, or 60,768 tokens: 1.85 concurrent requests at the full window** ([docs/models.md:274-326](../docs/models.md)).

The September move from 32B to 14B was about VRAM, not quality. The 32B left 5.16 GiB of cache, which forced an 8,192-token window. That window could not hold the agent's 4,213-token floor of system prompt and tool schemas plus one maximum-size 4,506-token tool result ([HANDOVER.md:2408-2446](../HANDOVER.md)). On the internal `check_agent` suite the 14B matched the 32B at 6 of 8 and was faster (2.4 s vs 3.9 s per trace). That suite cannot see the failures the user complains about, though. Every scenario opens with a question that needs a tool, and none builds up tool-free turns first ([HANDOVER.md:805-808](../HANDOVER.md)).

Most of the SQL failures the user hits come from **database-agent**, a separate Next.js repo. It sits beside this checkout on the user's machine (`../database-agent`), but this audit did not read it. It talks to the `/v1` gateway ([docs/architecture.md:42-50](../docs/architecture.md)).

The decisive evidence is a controlled replay of the SQL fabrication.

| Condition | Runs that called `run_sql` |
|---|---|
| Clean context | 4/4 |
| Same question after six prose turns | 0/4 |
| `OUTPUT_FORMAT` removed | 4/4 |
| Prohibition added next to the example | still 0/4 |
| Thinking on or off | 0/4 either way |
| Temperature 0.0 or 0.7 | 0/4 either way |

Across three questions and six runs each, the shipped prompt scored 0/18. Moving the bullet to the end scored 11/18, deleting the line 12/18, and forcing a tool call 18/18 ([HANDOVER.md:657-819](../HANDOVER.md)). The "fabricated rows" in production were not the model's. A shipped `mockExecute.ts` ran the model's un-executed fence against a random generator that cycled the names Amina, Chen and Diego. In HANDOVER's words, "the model fabricated the *query*; the frontend fabricated the answer" ([HANDOVER.md:157-189](../HANDOVER.md)). Against the real client database, a zero-tool reply listed "Hospital A 1200 … Hospital E 1000", while the actual query returned Sandton 1441 and Morningside 1418 ([HANDOVER.md:449-468](../HANDOVER.md)).

| Failure | Measured evidence | Configuration | Harness | Model |
|---|---|---|---|---|
| ```` ```sql ```` fence instead of `run_sql` after prose turns | 0/18 shipped → 18/18 forced | — | **Primary**: format template, no forced call | Secondary |
| Invented rows in the UI | `mockExecute.ts` shipped | — | **Entirely** | — |
| Retry misses one sentence of preamble; widened retry ran a "do not run it" query | 1/2 → 2/2 false positive | — | **Entirely**: detector design | — |
| Chart and prose figures with no query (summing to 45 against a 100-row table) | [HANDOVER.md:620-640](../HANDOVER.md) | — | **Primary**: no provenance check | Persists at 14B |
| ESG tool fills `hospital_group` with the description's example "Mediclinic" | 14/24 → 24/24 after rewrite | — | **Primary**: examples in description | Secondary |
| Mermaid diagram for "create a graph" | [HANDOVER.md:641-648](../HANDOVER.md) | — | **Primary**: format wording | Minor |
| Unquoted identifiers, 3 attempts per query | [HANDOVER.md:436-452](../HANDOVER.md) | — | **Primary**: schema presentation | Minor, self-corrects |
| Wrong argument names (`data` for `rows`), alias tables | [app/agent.py:506-566](../app/agent.py) | **Partly**: `auto` with no `strict` | Partly: wording | Partly: 8B worse |
| Thinking ran out of budget, then leaked inline | 200-token probe | **Entirely** (parser now fixed) | — | — |
| 16K window overflow | 16,334-token prompt | **Entirely** (fixed at 32K) | Schema dump size | — |
| Greedy decoding, no presence penalty, thinking off | open | **Entirely**, cheap to change | — | Thinking off costs ~9 BFCL points |
| Minutes: one pass misses most decisions (2/10 on a perfect transcript) | IB4005, ES2003b | Partly: 1,500-token cap truncated 4 of 15 chunk calls | **Primary**: one "concise" pass; chunked exhaustive prompt reached 9/9 and ~6/10 | Secondary |
| Minutes: proposals promoted to decisions, 60-90 candidates per meeting | IB4005, ES2003b | — | Partly: no filter pass yet | **Primary**: agreed vs proposed |
| Minutes: citation points to a segment that does not support the claim | ES2003b | — | Partly: ID check only | **Primary** |
| Document review | no transcripts on file | Likely: no `json_schema`, thinking off | Unknown | Likely partial |

Sources: [HANDOVER.md](../HANDOVER.md) and [docs/plans/step-03-model-gateway.md](../docs/plans/step-03-model-gateway.md).

The model's share is real. On the same unfixed production harness, **the 14B failed all four traps it was recorded on, and Claude passed all five**; the 14B's result on the fifth, monitoring views, was not recorded ([HANDOVER.md:205-232](../HANDOVER.md)). The difference was diligence, not trap resistance. Claude ran a query to confirm a hospital's spelling, as the tool description asked. The 14B skipped that step and used the example value. Its ESG failures were stable for unfamiliar names (Kloof, Midstream and Muelmed each scored 0/3), which looks like unfamiliar entities collapsing onto the example rather than random noise ([HANDOVER.md:325-358](../HANDOVER.md)). Wording fixes alone reached only 11-12 of 18 on the decoy. Closing it took an architectural change.

The independent BFCL v4 data agrees. Qwen3-14B scores **41.03% overall and 34.75% multi-turn**, against 48.71% and 47.87% for Qwen3-32B ([BFCL data](https://gorilla.cs.berkeley.edu/data_overall.csv)). An ICML 2026 paper, seen only through a search summary, reports a 1.87% tool-existence hallucination rate at 14B ([ICML 2026](https://icml.cc/virtual/2026/67404)).

Going back to the 32B is not the answer. Within the Qwen3 generation, size barely moves tool calling: BFCL v3 in thinking mode is 70.4 for the 14B and 70.3 for the 32B ([Qwen3 Technical Report](https://arxiv.org/html/2505.09388v1)). The lever is the model generation, not the parameter count.

### Minutes extraction: the decisions can be found, but not sorted

The minutes-of-meeting-ai project benchmarked the same 14B on public AMI meetings. Each run was scored by hand against AMI's reference decisions, against a pass bar written down before the run: at least 6 of 10 decisions and at most 1 wrong item ([RESULTS.md](../../minutes-of-meeting-ai/benchmarks/RESULTS.md); [EVALUATION_MANIFEST.md](../../minutes-of-meeting-ai/benchmarks/EVALUATION_MANIFEST.md)).

| Meeting and method | Decisions recovered | Wrong items |
|---|---|---|
| ES2003b, one pass | 0 of 9 in the Decisions section | 3 invented actions |
| ES2003b, four chunks plus a consolidation pass | about 4 of 9 | several proposals listed as decisions |
| IB4005, one pass, whisper transcript (23% WER) | 1 of 10 (loosely) | 3, including the opposite of the Gisella decision |
| IB4005, one pass, AMI manual transcript | 2 of 10 | 0 |
| Either meeting, one pass, thinking on | 1 of 9 and about 2 of 10 | new errors added |
| ES2003b, exhaustive 10-minute chunks | 9 of 9 among ~60 candidates | unadopted ideas, a contradicting detail |
| IB4005, exhaustive chunks, whisper / manual | about 6 / about 5 of 10, among ~90 candidates | presentation facts, trivia, dropped proposals |

Four conclusions follow:

- **The misses were the prompt's fault.** The one-pass prompt asks for a concise result and stops at 2-3 decisions. With the perfect transcript it still found only 2 of 10, so transcription errors cannot explain the misses. Context was not the limit either: the prompts were 11-15K tokens in a 32K window. A different prompt shape, with no meeting-specific tuning, took recall from about 4 of 9 to 9 of 9.
- **Thinking did not help here**, which qualifies the thinking lever below. It was tried only with the one-pass prompt, whose "concise" format capped output in both modes. It has not been tried on the exhaustive or filter passes.
- **The model's real share is precision.** Across every run the 14B listed proposals as decisions, presented an idea that was later replaced as settled, and cited segments that do not support the claim. That is the judgement step, and it is where model generation should matter most. The planned filter pass checks every candidate against the full transcript, so it is the right place to compare the 14B with a 27B.
- **These runs bypassed the gateway.** The minutes scripts call vLLM directly at `127.0.0.1:8000` with temperature 0.2 and thinking off. That proves direct callers exist, so the gateway's sampling defaults will not reach them. Each caller needs its own settings, or the routing has to change.

Limits: each draft was scored once, by one person, against 9-10 reference decisions per meeting, and the "about" figures are rough. IB4005's answers have now been seen, so it is no longer held out. The next comparison needs an AMI meeting nobody has read.

Document review still has no failing transcripts on file, so its row in the table is inference. Stage 0 should capture a dozen.

## Four configuration defects are costing quality on the current box

**Greedy decoding.** The app samples at temperature 0.0. For thinking mode, Qwen's model card says "DO NOT use greedy decoding, as it can lead to performance degradation and endless repetitions". It recommends 0.6 / 0.95 / 20 / 0 for temperature / top_p / top_k / min_p in thinking mode, and 0.7 / 0.8 / 20 / 0 in non-thinking mode ([Qwen3-14B-AWQ card](https://huggingface.co/Qwen/Qwen3-14B-AWQ)).

**No presence penalty.** The same card "strongly recommend[s]" `presence_penalty` 1.5 for quantized models, with a warning that it can cause language mixing. The app sends none.

**Thinking off.** This is the largest single lever. Qwen3-14B scores **70.4 on BFCL v3 with thinking and 61.5 without**, and 71.3 vs 59.6 on LiveBench ([Qwen3 Technical Report](https://arxiv.org/html/2505.09388v1)). Thinking was disabled after a single probe ran out of tokens inside `<think>` at `max_tokens: 200` ([docs/plans/step-03-model-gateway.md:301-312](../docs/plans/step-03-model-gateway.md)). It has never been measured on SQL correctness. On minutes extraction it gave no gain, but only with a one-pass prompt that capped output whatever the mode (see above), so treat it as a lever to measure per task rather than a default.

**Unconstrained tool arguments.** Under `tool_choice:"auto"`, vLLM extracts calls from raw text unless a tool is marked `strict: true` or the server sets `--tool-strict-level`, "so arguments may occasionally be malformed" ([vLLM tool calling](https://docs.vllm.ai/en/latest/features/tool_calling.html)). The alias tables in `app/agent.py` exist to catch exactly that.

Several suspects can be cleared:

- **The parser.** `hermes` is the parser Qwen recommends for Qwen3 ([Qwen function calling](https://qwen.readthedocs.io/en/latest/framework/function_call.html)). The repo's probes parsed tool calls 4/4 directly, streamed and through the gateway.
- **AWQ quantization.** It costs 1-3 points; non-thinking LiveBench drops from 59.6 to 57.4 ([Qwen3-14B-AWQ card](https://huggingface.co/Qwen/Qwen3-14B-AWQ)).
- **Context truncation.** It was real at 8K and 16K and is solved at the native 32K.

None of these fixes will take effect unless every request goes through the gateway. HANDOVER records Qwen3's thinking text reaching database-agent inline ([HANDOVER.md:1448-1457](../HANDOVER.md)). That should have been impossible, because the gateway defaults `enable_thinking` to false ([app/gateway.py:204-208](../app/gateway.py)). Either database-agent sets `chat_template_kwargs` itself, or it calls vLLM directly; vLLM publishes port 8000 to the host. Settle that first. Then add the sampling defaults in the gateway, filled in only when the caller omits them, the way it already handles `enable_thinking`. Also check the startup log for vLLM's "Default sampling parameters have been overridden" line. It shows whether the model repo's `generation_config.json` is quietly applying thinking-mode values to non-thinking turns.

The request settings to trial on the current 14B, before any model swap:

```jsonc
// Non-thinking turns: routing, small talk, summarising a tool result
{"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0,
 "presence_penalty": 1.5,
 "chat_template_kwargs": {"enable_thinking": false}}

// Thinking turns: writing SQL, extraction, document review
{"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0,
 "presence_penalty": 1.5,
 "chat_template_kwargs": {"enable_thinking": true},
 "max_tokens": 8192}            // thousands, not the 200 that failed

// Extraction / review output: constrain the shape
"response_format": {"type": "json_schema", "json_schema": {...}}

// Turns a classifier marks as data-seeking: force the call
"tool_choice": "required"        // or {"type":"function","function":{"name":"run_sql"}}
```

One hypothesis needs testing rather than assuming. A presence penalty pushes against tokens the model has already produced, and SQL legitimately repeats quoted identifiers. So run the SQL set at both 0 and 1.5.

Whether `strict: true` and `--tool-strict-level` exist in v0.28.0 specifically is unverified; the docs read were for the latest version. Check with `vllm serve --help` before relying on them. Named or required tool choice does constrain arguments through structured outputs today. vLLM is explicit that this guarantees "a validly-parsable function call - not a high-quality one" ([vLLM tool calling](https://docs.vllm.ai/en/latest/features/tool_calling.html)).

The harness fixes matter more than the sampling, and the strongest-evidenced ones belong in database-agent:

1. **Render displayed SQL and figures from the tool call's real `sql` argument and `query_id`**, never from model-written text. Removing the template took the model from 0/4 to 4/4, and the change works for every model ([HANDOVER.md:776-786](../HANDOVER.md)).
2. **Force a tool call on data-seeking turns.** Forcing it scored 18/18 on the decoy. Decide which turns are data-seeking with a small `structured_outputs.choice` classifier, not a regex. A blanket `"required"` would force a query on "hi".
3. **Write tool descriptions as rules, with no example values.** This took the ESG tool from 14/24 to 24/24.
4. **Add a provenance verifier.** It blocks or re-prompts any reply whose number, table or chart does not trace to a tool result from the same turn. This is the fix that turns silent failures into loud ones, and silent failures are why debugging eats so much time.
5. **Run SQL through a loop.** Run `EXPLAIN` before executing, feed errors back, and enforce a read-only transaction with a statement timeout that actually applies. The current `SET LOCAL statement_timeout` is likely a no-op outside a transaction ([docs/plans/pivot-inference-provider.md:122-133](../docs/plans/pivot-inference-provider.md)).
6. **Prune the schema.** Send only the relevant tables, with names already quoted as `query_as`, instead of the full 35-column dump.

Execution-guided SQL generation, which samples several candidates and picks by agreement on results, lets "smaller, cost-effective models" rival reasoning models at much lower cost ([arXiv 2503.24364](https://arxiv.org/abs/2503.24364)).

## A 2026-generation 27B fits beside the embedder and outclasses the 14B

The open-weight field has moved two generations past Qwen3-14B. The candidates that fit a 5090 at 4-bit are:

- Qwen3.8-27B, dense, Apache-2.0, released August 2026 ([HF](https://huggingface.co/Qwen/Qwen3.8-27B))
- Qwen3.6-27B and Qwen3.6-35B-A3B, April 2026 ([Qwen blog](https://qwen.ai/blog?id=qwen3.6-27b))
- gpt-oss-20b
- Gemma 4 31B and 26B-A4B
- GLM-4.7-Flash
- Nemotron 3 Nano 30B-A3B
- Devstral Small 2
- Granite 4.1 30B

**No 30-35B model fits at FP8** next to the embedder. Mistral Small 4 (119B) and every 2026 flagship are far out of reach.

The Qwen 27B and 35B models are hybrid Gated-DeltaNet designs. Only 16 of the 27B's 64 layers are full attention, with 4 KV heads, so **KV cache costs 64 KiB per token against 160 KiB for today's 14B**. A full 32K request needs about 2 GiB of cache even though the weights are twice the size ([HF Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)).

There is no independent benchmark that compares the 14B with the 2026 models on the same version, so the case rests on a bridge. In Qwen's own Qwen3.5 table, the April-2025 Qwen3-235B scores 54.8 on BFCL-V4, 58.5 on TAU2 and 51.7 on IFBench. Qwen3.5-27B scores **68.5, 79.0 and 76.5** ([Qwen3.5-122B-A10B card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)). The 235B was itself only level with the 14B on BFCL v3 (70.8 vs 70.4 thinking), so the 14B probably sits at or below the 235B's numbers. A 27B from the new generation should then be worth something like +13 BFCL-V4 and +20 TAU2 points. That is a vendor-number estimate, not a measurement.

Aggregated τ² scores, mostly from Artificial Analysis's telecom harness, put Qwen3.6-35B-A3B at **95.3%** and Qwen3.6-27B at **94.2%**. gpt-oss-20b scores 60.2%, Gemma 4 31B 59.9% and Nemotron 3 Nano 30B 25.4% ([BenchLM τ²](https://benchlm.ai/benchmarks/tau2-bench)). Qwen3.8-27B adds IFBench 79.5 (against 69.1 for Qwen3.6-27B), OmniDocBench 91.1 and LiveCodeBench v6 90.3. It publishes no BFCL, τ² or SQL numbers ([HF Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)).

None of the candidates has a published BIRD or Spider 2.0 score. Those leaderboards also carry annotation-error rates of 52.8% and 66.1% ([Jin et al., CIDR 2026](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf)). An in-house NL→SQL set scored by execution match is the only SQL evidence worth having.

For "must actually call the tool", the dense 27B is more reliable than the MoE, and the MoE is faster. On Claw-Eval, which counts a task only when all three attempts succeed (Pass^3), Qwen3.6-27B scores **60.6, Qwen3.6-35B-A3B 50.0 and Gemma 4 31B 25.0** ([HF Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B)). One practitioner found the 35B-A3B more prone to reasoning loops and malformed calls in long agent runs. That comes from a single author, seen only as a snippet ([allanchan339](https://allanchan339.github.io/bug-fixes/2026/04/19/Qwen36-35B-A3B-tool-calling.html)). gpt-oss-20b is the weakest agent of the three, and its vLLM Harmony parser has an open bug. A stray header becomes a phantom tool named `assistant<|channel|>analysis` ([vLLM #58825](https://github.com/vllm-project/vllm/issues/58825)), the same silent-failure class the user is already fighting.

| Model | Weights in VRAM | KV per token | Tool-use evidence | vLLM parsers | Role |
|---|---|---|---|---|---|
| Qwen3-14B-AWQ (today) | 9.29 GiB | 160 KiB | BFCL v4 41.0, multi-turn 34.8 | `hermes`, `qwen3` | Baseline |
| **Qwen3.8-27B NVFP4** | ~18.5 GB (est. from the 3.6-27B) | 64 KiB | IFBench 79.5, OmniDocBench 91.1 (vendor) | `qwen3_coder`, `qwen3` | **Primary trial** |
| Qwen3.6-27B NVFP4 | 18.48 GB measured on a 5090 | 64 KiB | τ² 94.2%, Pass^3 60.6 | `qwen3_coder`, `qwen3` + patched template | Proven fallback |
| Qwen3.6-35B-A3B NVFP4 | 21-24 GB | 20 KiB | τ² 95.3%, Pass^3 50.0 | `qwen3_coder` (or `qwen3_xml`) | Speed, bulk extraction |
| gpt-oss-20b MXFP4 | ~13 GB (est.) | ~24 KiB (est.) | τ² 60.2%, open phantom-tool bug | `openai` | Control only |
| Gemma 4 31B 4-bit | ~19-20 GB (est.) | unknown | τ² 59.9% (aggregator) | `gemma4` | Non-Qwen fallback |

Sources: [codeberg 5090 run](https://jpmrblood.codeberg.page/2026/08/07-vllm-docker-nvfp4-27b-rtx5090-command-line-breakdown/); [HF Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B); [vLLM Gemma 4 recipe](https://recipes.vllm.ai/Google/gemma-4-31B-it?variant=w4a16).

The swap has four costs.

**VRAM headroom.** One measured 5090 run of Qwen3.6-27B NVFP4 used ~28.7 GB in total with a 9.3 GiB KV pool and 0.91 GB of CUDA graphs ([codeberg](https://jpmrblood.codeberg.page/2026/08/07-vllm-docker-nvfp4-27b-rtx5090-command-line-breakdown/)). With KV capped at 6.5 GB, chat should take about 26-27 GiB. Add the embedder's ~3.3 GiB and almost none of the ~8.6 GiB that the 14B freed for Step 6 speech is left ([HANDOVER.md:2461-2464](../HANDOVER.md)). Speech goes back to the CPU, or KV drops to FP8, or the 0.6B embedder moves to the CPU.

**Decode speed.** The 5090 has 1,792 GB/s of VRAM bandwidth ([runaihome via dev.to](https://dev.to/jovan_chan_9500711396d4e6/multi-gpu-for-local-ai-in-2026-nvlink-vs-pcie-and-when-a-second-card-actually-helps-1egf)). That puts the single-stream ceiling at roughly 97 tok/s for 18.5 GB of weights, against roughly 180 for today's 14B. These are estimates. No stock-vLLM tok/s has been measured for the 27B on a 5090.

**Verbosity.** Running Artificial Analysis's index, Qwen3.6 27B used ~3.7x the output tokens of Gemma 4 31B ([AA on X](https://x.com/ArtificialAnlys/status/2049881951260283097)). Qwen3.5 27B used 98M tokens reasoning against 25M without ([AA](https://artificialanalysis.ai/articles/sub-32b-open-weights)). A thinking turn on the 27B could therefore run several times slower than a non-thinking 14B turn. Use `reasoning_effort` low or medium for SQL and extraction, not the default xhigh.

**Harness assumptions.** The parser changes from `hermes` to `qwen3_coder`. Qwen3.6 needed a patched chat template for correct tool and reasoning parsing. Qwen3.8 turns `preserve_thinking` on by default, which conflicts with `app/agent.py` dropping reasoning from replayed turns. The gateway's `enable_thinking:false` default needs re-checking against the 3.8 template.

Launch settings to trial, derived from the [vLLM Qwen3.8-27B recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B) and the measured 5090 run. None has run on this box. In `docker-compose.yml` they become `command:` list entries, and the model is pinned with `--revision` the way the 14B is today.

```bash
# Trial A (primary): Qwen3.8-27B NVFP4
vllm serve Inferact/Qwen3.8-27B-NVFP4 --revision <pin> \
  --served-model-name qwen3.8-27b \            # set LLM_MODEL to match
  --max-model-len 32768 \                       # keep parity with the 14B eval
  --kv-cache-memory-bytes 6500000000 \          # ~99K BF16 tokens = 3 x 32K; overrides --gpu-memory-utilization
  --max-num-seqs 8 --max-num-batched-tokens 8192 \
  --max-cudagraph-capture-size 8 \              # if graph capture still OOMs: --enforce-eager
  --enable-prefix-caching \
  --language-model-only \                       # from the Qwen3.6 card; verify on 3.8
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
# Leave KV at BF16 (no --kv-cache-dtype) to keep tool arguments exact.
# Optional speed-up: --speculative-config '{"method":"mtp","num_speculative_tokens":3}'
# Fallback checkpoints: unsloth/Qwen3.8-27B-NVFP4, RedHatAI/Qwen3.8-27B-MXFP4
# Non-thinking requests: temperature 0.7, top_p 0.8, top_k 20, presence_penalty 1.5

# Trial B (5090-proven): same flags with a Qwen3.6-27B NVFP4 build
#   plus the patched chat template the codeberg run needed.

# Trial C (speed): Qwen3.6-35B-A3B NVFP4 (kyaky/Qwen3.6-35B-A3B-NVFP4 or the NGC build)
  --kv-cache-memory-bytes 3000000000 \          # ~150K tokens at 20 KiB/token
  --language-model-only --enable-prefix-caching \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
# Switch to --tool-call-parser qwen3_xml if long runs show malformed calls.
# No --trust-remote-code below vLLM 0.31 (RCE fix). Watch for a Marlin-fallback warning on the MoE layers.

# Control: openai/gpt-oss-20b --tool-call-parser openai --enable-auto-tool-choice
#   (Chat Completions supports only tool_choice="auto"; watch for "<|channel|>" in tool names)
```

**The vLLM version can wait until after the quality verdict.** The Qwen3.8 recipe needs only vLLM ≥0.17, so the current 0.28.0 image should load the model, falling back to Marlin W4A16 kernels with a warning. The recipe's single-5090 runs were verified on a 0.26.1 build. Native W4A4 NVFP4 on the 5090's SM120 arrived in 0.30.0, and 0.31.0 fixed a high-rated RCE affecting servers started with `--trust-remote-code`. 0.32.0 added an NVFP4 KV cache but moved default builds to CUDA 13, which needs host driver R580+ ([eurokommerz](https://eurokommerz.at/blog/blackwell-rtx-sm120-software-support/)). Run the quality comparison first. Then repin to a 0.31 digest for speed and the security fix, and treat 0.32+ as a separate driver change.

## More hardware buys modest gains at shortage prices

The October-November 2026 memory shortage has inflated every hardware option. The ranking still comes out clearly.

**128 GB of DDR5** is the cheapest real step. A 2x64 GB kit starts at **$1,852**, and most kits sit between $2,350 and $3,700 ([datacenterdisk](https://datacenterdisk.com/ram/ddr5/128gb)). It unlocks the 120B-class MoE models through llama.cpp, which keeps the attention layers on the GPU and moves the expert layers to system RAM (`--n-cpu-moe`):

- Qwen3.8-Flash-Next at Q2 decoded at ~48 tok/s on a 5090 with 128 GB, with ~1,429 tok/s prefill and ~11 s to first token at 16K input ([zenn.dev](https://zenn.dev/holy_fox/articles/04887ff8177b87)).
- The same model at Q4 managed 21-22 tok/s, with prefill of only 152-163 tok/s on default batch settings ([note.com](https://note.com/gb10_tsurumitsu/n/n180cba8a21ae)).

The quality gain over the 27B is small. Qwen3.5-122B-A10B adds **+3.7 BFCL-V4, +0.5 TAU2 and +7.8 Terminal Bench 2** over Qwen3.5-27B, and gpt-oss-120b trails the 27B on IFBench (69.0 vs 76.5) ([Qwen3.5-122B card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)). The costs are large. The step leaves vLLM and its `qwen3_coder` parser, it runs one stream at a time, and it reaches 192-256 GB only with four DIMMs. On Zen 5, four DIMMs reportedly drop the guaranteed memory speed to DDR5-3600 ([TechPowerUp forum](https://www.techpowerup.com/forums/threads/g-skill-reveals-worlds-first-large-capacity-256-gb-64-gb-x4-ddr5-u-dimm-memory-at-ddr5-6000-cl32-overclocked-speed.335752)).

**A second RTX 5090** is poor value. It costs **$6,033-9,400** in the US ([GPUPoet](https://gpupoet.com/gpu/market-report/gpu-market-report-october-2026); [TechSpot](https://techspot.com/article/3192-gpu-pricing-q4-2026)) and unlocks no new model class under vLLM. 120B-class models at 4-bit need an estimated 61-65 GB for weights alone, which leaves no KV room in 64 GB. The two cards would also talk over PCIe 5.0 at x8 each, where enabling GPU-to-GPU peer access moved throughput only 2-5% ([vLLM forum](https://discuss.vllm.ai/t/dual-rtx-5090-tp-2-shm-vs-patched-bar1-p2p-cumem-single-pass-2-7-mean-point-estimate/2870/2)).

**An RTX PRO 6000 (96 GB)** is the only step that runs 120B-class models fully in VRAM, under vLLM, for multiple users. It launched at **$8,565** and now sells for $16,000-17,987 ([Tom's Hardware](https://www.tomshardware.com/pc-components/gpus/nvidia-rtx-pro-6000-blackwell-gpu-is-listed-for-usd8-565-at-us-retailer-26-percent-more-expensive-than-the-last-gen-rtx-6000-ada); [gpuprix](https://gpuprix.com/us/gpus/rtx-pro-6000-blackwell)). Used 5090 asks of around $8,000 could offset part of the cost, but an ask is not a sale price ([gpudojo](https://gpudojo.com/market-notes/2026-10-23-rtx-5090-asks-jump)).

| Step | Cost (US, Oct-Nov 2026) | Unlocks | Gain over a 27B | Throughput | Stack | Verdict |
|---|---|---|---|---|---|---|
| 0. Software | $0 | Qwen3.8/3.6-27B, 35B-A3B | (the big jump) | Full VRAM speed | vLLM | **Do first** |
| A1. RAM to 128 GB | $1,852-3,400 | Qwen3.5-122B-A10B, gpt-oss-120b, Qwen3.8-Flash-Next | ~+4 BFCL-V4, ~0 TAU2 | 21-48 tok/s, one stream | llama.cpp / KTransformers | Batch review jobs only |
| A2. RAM to 256 GB | price not found | DeepSeek-V4-Flash (284B/13B), Qwen3.5-397B | unknown on BFCL/TAU2 | ~4-10 tok/s (est.) | KTransformers / llama.cpp | Offline only |
| B. Second 5090 | $6,033-9,400 | no new class; FP8 27B, more concurrency | small | TP=2 over PCIe x8 | vLLM | Skip |
| C. RTX PRO 6000 | $16,000-17,987 | 120B-class fully in VRAM | as A1, at GPU speed | multi-user batching | vLLM | Wait for prices |
| D. DGX Spark | $4,699 (reports conflict) | as A1 | as A1 | gpt-oss-120b 38.55 tok/s, ~1,723 prefill | CUDA | Only if a second server is wanted |

Sources for the separate-box figures: [localaimaster](https://localaimaster.com/blog/dgx-spark-vs-strix-halo-vs-mac-studio); [shattered.io](https://shattered.io/dgx-spark-vs-mac-studio-ryzen-ai-halo-local-ai-2026/). Strix Halo matches the Spark's decode speed but has roughly a fifth of its prefill speed (~340 tok/s). Every agent turn re-sends a long schema, so that prefill gap rules it out for agent loops.

## Hosted open models are cheap, but none run in the UAE

Per-token APIs win on cost by a wide margin:

| Model | Provider | $/M input | $/M output |
|---|---|---|---|
| gpt-oss-120b | Together, Fireworks, Groq | 0.15 | 0.60 |
| Qwen3-235B-A22B-2507 | DeepInfra | 0.09 | 0.55 |
| DeepSeek-V4-Flash | DeepInfra | 0.09 | 0.18 |
| GLM-5.3 | DeepInfra | 0.5625 | 2.50 |
| Qwen3.8-27B (preview) | Groq, ~450 tok/s | 0.80 | 4.00 |

Sources: [Together](https://www.together.ai/pricing); [DeepInfra](https://deepinfra.com/pricing); [Groq](https://console.groq.com/docs/models).

At a 3:1 input-to-output mix, gpt-oss-120b blends to about $0.26 per million tokens, or roughly $263 for a billion tokens a month. Matching the cost of a 24/7 RunPod Secure H100 at $3.99/h would take about 11 billion tokens a month ([RunPod](https://www.runpod.io/pricing)). Dedicated rentals make sense only for single tenancy, custom weights or guaranteed latency. In that case the cheapest serious options are:

- DeepInfra dedicated H100 at $2.90/h
- RunPod Secure RTX PRO 6000 at $2.49/h: about $1,818 a month 24/7, or about $548 for business hours only
- Hetzner GEX63, a PRO 6000 Max-Q for €999 a month plus a €999 setup fee, hosted in Germany or Finland ([Hetzner](https://www.hetzner.com/dedicated-rootserver/matrix-gpu/))

RunPod Community Cloud and Vast.ai run on third-party hosts and should not see client data.

Privacy terms differ by provider:

- **Fireworks** keeps no copy by default. Prompts and outputs stay in volatile memory, except on the Responses API with `store=True` ([Fireworks data handling](https://docs.fireworks.ai/guides/security_compliance/data_handling)).
- **DeepInfra** holds inputs only in memory and does not train on them ([DeepInfra privacy](https://docs.deepinfra.com/account/data-privacy)).
- **Together** stores prompts until zero data retention is switched on in organisation settings, and the setting applies only to later requests ([Together ZDR](https://docs.together.ai/docs/zero-data-retention)).
- **OpenRouter** can enforce `zdr: true`, `data_collection: "deny"` and a pinned provider list, account-wide or per request ([OpenRouter routing](https://openrouter.ai/docs/features/provider-routing)).

All of these still send data out of the UAE. The country's personal data protection law (PDPL, Federal Decree-Law 45 of 2021) requires adequate protection for cross-border transfers, and sector regulators such as CBUAE for finance and DOH for health add stricter in-country rules. That summary comes from a vendor blog that disclaims legal advice ([Spheron](https://www.spheron.network/blog/gpu-cloud-middle-east-2026/)).

The in-country options are thin and expensive:

- **AWS me-central-1** has had L40S (G6e) instances since February 2026 ([AWS](https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-ec2-g6e-instances-uae-region)). Its H100 capacity is unconfirmed.
- **Azure UAE North** lists an H100 v5 SKU that was last observed in July 2026 ([Sparecores](https://sparecores.com/server/azure/Standard_ND96isr_H100_v5)).
- **In-region H100s** cost about $12.30-14.00 per GPU-hour ([Spheron](https://www.spheron.network/blog/gpu-cloud-middle-east-2026/)).
- **Core42** offers AMD MI355X from $3.50/h ([Core42](https://www.core42.ai/ai-cloud)) and has served gpt-oss-120b and 20b through its Compass API under UAE jurisdiction since August 2025. Pricing and OpenAI compatibility are unpublished ([Middle East AI News](https://www.middleeastainews.com/p/core42-launches-gpt-oss-access-with)).

Pointing the gateway at a remote backend is mostly configuration, with three code gaps. `_post()` sends no `Authorization` header ([app/llm.py:61-67](../app/llm.py)). Every request gets a Qwen-only `chat_template_kwargs` ([app/gateway.py:208](../app/gateway.py)). The output clamp reads `max_model_len`, which only vLLM publishes ([app/llm.py:88-115](../app/llm.py)). A rented vLLM pod needs only the auth header. A hosted API needs all three fixes, plus a per-alias upstream so `syslab-default` can stay local while a second alias routes elsewhere. The gateway, not its callers, should inject the OpenRouter privacy policy. Embeddings have their own URL ([app/config.py](../app/config.py)), so document text never has to leave the box for embedding.

On break-even, a $16,000 PRO 6000 pays for itself in 8.8 months at 24/7 against RunPod Secure, and in 29 months at business hours only. Against per-token APIs at a billion tokens a month it takes about five years. Against in-country hyperscaler H100s at roughly $9,000-10,000 a month, it pays back in under two months. **Owning hardware is justified by data residency, not by cost or quality.**

## The recommended path: fix, measure, swap, then decide on hardware

| Stage | When | Cost | Actions | Gate to the next stage |
|---|---|---|---|---|
| 0. Repair | This week | $0 | Confirm all traffic goes through the gateway. Add the sampling defaults and per-turn thinking. Use `json_schema` for extraction. Force tool calls on data-seeking turns. Remove the SQL fence template. Render SQL from tool arguments. Add the provenance verifier. Build and measure the minutes filter pass. Capture document-review transcripts. | A regression eval exists: the 5 production traps, poisoned-history replays, 50-100 NL→SQL pairs scored by execution match, JSON field accuracy, and the minutes pass bar on an unseen AMI meeting with a second scorer. The 14B has a baseline. |
| 1. Swap | Weeks 2-3 | $0, or about $10 to pre-screen on Groq | Pre-screen Qwen3.8-27B on Groq using the synthetic test-corpus company (Step 11) as data; Groq does not state tool support for this preview, so check that first. Then run Trials A, B and C on the box under 0.28.0. Pick by eval. Repin to vLLM 0.31. Re-run `check_agent` with the new parser. | The 27B beats the 14B on traps, SQL and extraction at acceptable latency. Decide where speech goes. |
| 2. Hybrid | Months 1-2 | Tens of dollars a month | Fix the three gateway gaps. Add a second alias to a ZDR provider pinned with `only` and `allow_fallbacks:false`, for non-sensitive and bulk work. Price Core42 or AWS UAE for regulated clients. | A written data classification says which clients can leave the box. |
| 3. Hardware | Only if the eval shows the 27B is short and a bigger hosted model fixes it | $1,850-3,400 (RAM) or $16-18k (PRO 6000) | 128 GB RAM for batch review with a 122B model. A PRO 6000 only when residency needs multi-user 120B-class serving and prices ease. Never a second 5090. | — |

The Stage 1 pre-screen cost is an estimate: about 4.5M tokens at Groq's $0.80/$4.00 comes to roughly $7.

The order matters because it separates the three causes. Stage 0 removes the harness and configuration share, so whatever still fails on the 14B is the model's share. Stage 1 then shows how much of that share a 2026 27B closes. Hosted endpoints, run on synthetic data, can answer the expensive question cheaply: whether a 122B or 235B-class model would close the rest. That makes it possible to decide on hardware before spending $2,000-18,000 on it.

## Conclusion

The model generation now matters more than size or hardware. A 2026 27B outscores the 2025 235B on tool-use benchmarks and fits on the existing card. So the question of what fits on the box has largely stopped being a quality question; it is now about concurrency, speech headroom and data residency. The most durable investment is not a model at all. It is the provenance verifier and the trap-based eval, because the user's real cost is debugging time. That cost comes from failures that look like success, and every model the user ever runs will produce some of them.

Two risks shape the roadmap. First, the strongest small candidates all come from one vendor. Reports say Qwen's flagships have been closed-weight since July 2026, although the August Qwen3.8-27B release shows the small line is still open ([digitalapplied](https://www.digitalapplied.com/blog/qwen-closed-flagship-pivot-open-weight-retreat-2026)). Keeping Gemma 4 or gpt-oss in the eval as a standing fallback costs nothing. Second, buying hardware is justified by UAE data residency, not by quality per dollar. The decision to buy should therefore follow a written data classification of the user's clients, not a benchmark table.
