# Models

Benchmarked on the production box (RTX 5090, 32 GB VRAM, Ryzen 9950X, 60 GB RAM)
on 2026-09-08, via `scripts/bench_models.py` against Ollama. Numbers are a
relative-sizing reference, not the production figure: vLLM's PagedAttention
manages KV cache more efficiently than Ollama's allocator, so real footprint
under vLLM should be re-measured against concurrent load before anyone plans a
second service onto the same card.

## Candidates measured

| Model | Params | Quant | VRAM @8k ctx | VRAM @32k ctx | tok/s @8k | tok/s @32k |
|---|---|---|---|---|---|---|
| qwen3:32b | 32.8B | Q4_K_M | 21.9 GB | 27.9 GB | 63.9 | 56.3 |
| gemma3:27b | 27.4B | Q4_K_M | 19.1 GB | 20.7 GB | 76.1 | 71.7 |
| qwen3:14b | 14.8B | Q4_K_M | 10.6 GB | 14.5 GB | 136.6 | 110.6 |

Full raw output: `bench_results.json` on the server (not committed — regenerate
with `scripts/bench_models.py` any time).

## Decision: qwen3:32b, 8192 context

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

Confirmed under vLLM, not inferred: `scripts/check_agent.py` runs real
multi-turn tool-calling transcripts against the live server, and the 32B model
passes 6 of 8 where the 8B smoke-test model passed 5 of 8 — including a
scenario the 8B model failed by hallucinating SQL. That is the "realistic
transcript, not a benchmark prompt" the plan asks for. See
`docs/plans/step-03-model-gateway.md` §3.1 for the two remaining failures and
why they are test strictness rather than model quality.

## What the card is actually doing

Measured 8 September 2026, read out of vLLM's own startup log rather than
estimated. This replaces the Ollama-derived guesses above for anything to do
with capacity.

```
docker compose logs vllm | grep -i "kv cache\|maximum concurrency"
```

| | |
|---|---|
| Card, free at startup | 30.86 of 31.36 GiB |
| Budget at `--gpu-memory-utilization 0.70` | 21.95 GiB |
| Weights and non-torch | 18.62 GiB |
| Peak activation + CUDA graphs | 1.14 GiB |
| **KV cache** | **3.0 GiB = 12,272 tokens** |
| **Concurrency at 8192 tokens per request** | **1.50x** |

**1.5x was the number to argue with.** It means one conversation at a time and a
second one queueing, on a machine bought for concurrency — which is the reason
the plan chose vLLM over Ollama in the first place. Everything else about the
card is fine; the KV cache is the whole constraint, and it was small because the
weights take 18.62 GiB of a 21.95 GiB budget.

Two things stop 1.50x being as bad as it reads. A request occupies what it
actually uses, not `max_model_len` — one observed request held 38% of the pool,
about 4,660 tokens, so roughly 2.6 of that size fit. And the prefix cache ran at
**92.9% hit rate**: every conversation opens with the same system prompt, schema
and tool definitions, and vLLM stores that once rather than per request. When
the pool does fill, vLLM queues (`Waiting: N reqs`) rather than failing, so the
symptom is latency, not errors.

### Raised to 0.85 on 8 September 2026

vLLM printed the alternative in the same log line: the full card would give
**10.95 GiB of KV cache**, roughly 44,800 tokens, about 5.5x. `0.85` takes most
of that while leaving about 4.7 GiB — which still covers the ~3.5 GiB Section 13
budgets for embeddings, STT and TTS combined. The 0.70 was reserving nine
gigabytes for six weeks on behalf of three services that do not exist, while the
one that does exist ran at 1.50x.

`--max-model-len` deliberately stayed at 8192 in the same change. Context length
and concurrency spend the same cache, so doubling the window would have given
back most of what the utilization gained. The window is not what hurts: callers
never have to know it, because `app/gateway.py` clamps `max_tokens` to what is
left after the prompt.

**Re-read the startup log after any change to either flag** — the numbers above
are the whole basis for both, and they are one `grep` away:

```
docker compose logs vllm | grep -i "kv cache\|maximum concurrency"
```

Not free, though, and the same log shows why. **One boot of this container
failed outright**:

```
Available KV cache memory: 0.45 GiB
ValueError: To serve at least one request with the model's max seq len (8192),
2.0 GiB KV cache is needed ... estimated maximum model length is 1856.
```

That was a `docker compose up -d` recreating the container while the previous
one was still releasing VRAM — 28 seconds later the next attempt succeeded with
the full 3.0 GiB. `restart: unless-stopped` is what made that self-healing
rather than an outage, and it is worth knowing before raising utilization: the
higher it goes, the less slack there is for exactly that race.

Still not measured: sustained throughput with several conversations in flight.
The concurrency figure above is what vLLM will *allow*, not what it delivers.

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
- `--max-model-len 8192` — the VRAM budget above. Callers do not have to know
  this number: `app/gateway.py` reads it from `/v1/models` and clamps
  `max_tokens` to what is left after the prompt. Without that the website's
  `max_tokens: 32000` is an HTTP 400 on every single request.
- `--gpu-memory-utilization 0.85` — everything left after the weights becomes
  KV cache, which is what concurrency is made of. Raised from 0.70; the
  measurements and the trade are above.

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

Section 13's `models.toml` profile file does not exist and is not missing: it
is folded into Step 5, where a second model finally gives it a second row.
Until then the alias table is `MODEL_ALIASES` in `app/config.py` and the served
model is the `--model` flag in `docker-compose.yml`. Reasoning in
`docs/plans/step-03-model-gateway.md` §3.4.

## Licence

Qwen3 family: Apache 2.0, verified per `docs/licences.md`.
