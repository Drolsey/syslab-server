# Models

Benchmarked on the production box (RTX 5090, 32 GB VRAM, Ryzen 9950X, 60 GB RAM)
on 2026-09-08, via `scripts/bench_models.py` against Ollama. Numbers are a
relative-sizing reference, not the production figure: vLLM's PagedAttention
manages KV cache more efficiently than Ollama's allocator, so real footprint
under vLLM should be re-measured once a model is chosen for the profile in
Section 13 of the architecture plan.

## Candidates measured

| Model | Params | Quant | VRAM @8k ctx | VRAM @32k ctx | tok/s @8k | tok/s @32k |
|---|---|---|---|---|---|---|
| qwen3:32b | 32.8B | Q4_K_M | 21.9 GB | 27.9 GB | 63.9 | 56.3 |
| gemma3:27b | 27.4B | Q4_K_M | 19.1 GB | 20.7 GB | 76.1 | 71.7 |
| qwen3:14b | 14.8B | Q4_K_M | 10.6 GB | 14.5 GB | 136.6 | 110.6 |

Full raw output: `bench_results.json` on the server (not committed — regenerate
with `scripts/bench_models.py` any time).

## Decision: qwen3:32b, 8192 context, pending Step 3.4 confirmation

Chosen over gemma3:27b despite being slower, because Section 14 of the
architecture plan picked the Qwen3 family specifically for tool-calling
strength, which is what the website's agent depends on — a 20% speed
advantage doesn't matter if the model is worse at the one thing the
integration needs. gemma3:27b remains the fallback if qwen3:32b's tool-calling
quality doesn't hold up under a real multi-turn transcript.

Context capped at 8192 rather than the 32768 in the plan's example profile
(Section 13): at 32k context qwen3:32b measured 27.9 GB, which exceeds the
~22 GB chat budget (`--gpu-memory-utilization 0.70`) and leaves nothing for
embeddings, STT or TTS on the same card. At 8192 context it measured 21.9 GB,
which fits the budget.

qwen3:14b stays the documented fallback for concurrency: 14.5 GB even at 32k
context, more than double the tok/s, and much more headroom for multiple
simultaneous users once Step 3's continuous batching is in play.

**Open, blocking Step 3.4:** re-measure the chosen model under vLLM directly
(not Ollama) with a realistic multi-turn tool-calling transcript, per the
plan's own instruction not to trust a benchmark prompt over a real one.

## Serving stack, pinned

| Component | Version | Pin |
|---|---|---|
| vLLM | v0.28.0 | `sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14` |

That digest is the multi-arch index digest published for `vllm/vllm-openai:v0.28.0`,
verified against the registry on 2026-09-08 and against the image actually running
on the box. `docker-compose.yml` pins the digest and carries the tag alongside it
for readability; the digest is what actually resolves, so a re-pushed tag cannot
change what runs.

Launch flags that are not optional, and why (see
`docs/plans/step-03-model-gateway.md` for the measurements behind them):

- `--enable-auto-tool-choice --tool-call-parser hermes` — without both, every
  `tool_choice` value except `"none"` returns HTTP 400 and the website's agent
  cannot call a tool at all.
- `--max-model-len 8192` — the VRAM budget above.
- `--gpu-memory-utilization 0.70` — leaves room for embeddings, STT and TTS.

## Currently running

`Qwen/Qwen3-32B-AWQ`, served by the `vllm` service in `docker-compose.yml` and
advertised to callers only as the alias `syslab-default` (Step 3.2).

It replaced `Qwen/Qwen3-8B-AWQ`, which was the Step 1 smoke-test model and
proved the container and the reboot-persistence gate. The swap was not
cosmetic: on `scripts/check_agent.py` the 8B model failed 3 of 8 real
tool-calling scenarios, including hallucinating a SQL query against a database
that was never configured when asked to sum a spreadsheet it had already read.
The 32B model does that scenario correctly. See
`docs/plans/step-03-model-gateway.md`.

Still to confirm under vLLM rather than inferred from the Ollama benchmark
above: actual VRAM headroom at `--max-model-len 8192` with
`--gpu-memory-utilization 0.70`, and throughput under concurrent requests,
which is the number continuous batching exists for and which a
one-request-at-a-time benchmark cannot show.

## Licence

Qwen3 family: Apache 2.0, verified per `docs/licences.md`.
