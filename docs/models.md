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

### Raised to 0.85 on 8 September 2026, and what that actually bought

Measured after the change, from the same log line:

| | At 0.70 | At 0.85 |
|---|---|---|
| Budget | 21.95 GiB | 26.65 GiB |
| Weights and non-torch | 18.62 GiB | **20.04 GiB** |
| Peak activation | 0.33 GiB | **1.46 GiB** |
| CUDA graphs | 0.81 GiB | 0.81 GiB |
| KV cache | 3.0 GiB | **5.16 GiB** |
| KV cache in tokens | 12,272 | **21,120** |
| Concurrency at 8192 per request | 1.50x | **2.58x** |

Concurrency up 72%. But the projection before the change was ~3.4x, and the
gap is the thing worth writing down: **vLLM's own overhead scales with the
budget you give it.** Weights and non-torch grew 1.42 GiB and peak activation
grew 1.13 GiB, purely because the engine sized its batching to the larger
allowance. Of 4.70 GiB of extra budget, only 2.16 GiB — about 46% — became
cache. The estimate assumed the overhead was fixed. It is not, and this is the
correction: **on this engine, budget a little under half of any increase to
actually arrive as KV cache.**

The same effect moved the ceiling. At 0.70 vLLM said the full card would give
10.95 GiB of cache; at 0.85 it says 8.41 GiB, because the overhead it is
measuring against is now larger. The "5.5x if you spent everything" figure from
before was optimistic for the same reason — roughly 4.2x is the real ceiling,
and it costs every gigabyte of headroom to reach.

**Headroom is now the constraint, and it is tighter than planned.** Actual
usage is 20.04 + 1.46 + 0.81 + 5.16 = **27.47 GiB**, leaving about **3.4 GiB**
free, not the 4.7 projected. Section 13 budgets ~3.5 GiB for embeddings, STT
and TTS together. That no longer fits with anything to spare. When Step 5
lands, either this comes back down, or speech goes on the CPU — which Section
13 already allows ("the 9950X has cores to spare") and which is now the
expected answer rather than the fallback.

Also visible: vLLM again allocated slightly more cache than the budget asked
for (5.16 GiB where 4.2 would have fit inside 26.65), the same overshoot as at
0.70. `--kv-cache-memory` sets the cache directly instead of inferring it from
a fraction, and vLLM names it in that log line; it is the more precise flag if
this ever needs to be exact.

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

### Down to 14B on 9 September 2026, and up to a 16384 window

The window, not the weights, was the thing that broke. Measured on this box's
own `/tokenize` endpoint rather than estimated:

| | tokens |
|---|---|
| System prompt | 2069 |
| 12 tool schemas | ~2144 |
| **Floor, before anything is asked** | **4213** |
| Window then (`--max-model-len 8192`) | 8192 |
| Left for the whole conversation | 3979 |
| One tool result at `TOOL_RESULT_BUDGET` | **4506** |

A single maximum-size tool result did not fit in what the window had left —
4213 + 4506 = 8719 against 8192, before the user's question, the assistant's
tool-call turn, or one token of reply. History trimming (`llm.trim_to_window`,
added the same day) stops that being *terminal* for a conversation, but it
cannot make one oversized turn fit.

Every fix inside the 32B was a trade: cut `TOOL_RESULT_BUDGET` and the model
sees fewer rows per call; raise `--max-model-len` and it costs the 2.58x
concurrency bought the day before; cut the system prompt and 2069 tokens is not
enough to matter. Changing the model is the only move that buys back both,
because the 32B's weights were what made a larger window unaffordable.

| | 32B-AWQ | 14B-AWQ |
|---|---|---|
| Weights on disk | 18.00 GiB | 9.29 GiB |
| Layers | 64 | 40 |
| KV per token | 256 KiB | 160 KiB |
| `--gpu-memory-utilization` | 0.85 | 0.70 |
| KV cache | 5.16 GiB | **9.27 GiB** |
| KV cache in tokens | 21,135 | **60,768** |
| `--max-model-len` | 8192 | 16384 |
| Conversation room after the floor | 3,979 | **12,171** |
| Concurrency at that window | 2.58x | **3.71x** |
| Free VRAM for Steps 5 and 6 | ~3.4 GiB | **~9.4 GiB** |

Those are measured, from the startup log on 9 September 2026:

```
Available KV cache memory: 9.27 GiB
GPU KV cache size: 60,768 tokens,
Maximum concurrency for 16,384 tokens per request: 3.71x
```

Same family, so the tokenizer is the same and the 4213-token floor does not
move. 16384 rather than 32768 because context and concurrency still spend the
same cache, and 16384 is the point where both numbers beat what the 32B gave.

**Why the utilization went back to 0.70 the day after it went up to 0.85.** Not
a reversal. 0.85 existed to buy concurrency out of the budget, because the 32B's
weights left nowhere else to get it, and the section above records what it cost:
3.4 GiB of headroom against the ~3.5 GiB Section 13 budgets for embeddings and
speech, with speech-on-CPU becoming "the expected answer rather than the
fallback". The 14B returns that concurrency for free, so the 4.7 GiB has no case
left. Concurrency, window and headroom all improve at once, which is not a
trade and did not need one.

### The projection missed by 21%, for a new reason — read this before projecting again

Predicted 11.71 GiB of cache, 76,743 tokens, 4.68x. Measured 9.27 GiB, 60,768
tokens, **3.71x**. The shortfall is 2.44 GiB, and the ratio (0.792) is
uncomfortably close to the 0.759 of the previous miss.

The KV arithmetic was not the problem and has now been right three times:
9.27 GiB ÷ 160 KiB per token = 60,752 against 60,768 logged, after 0.70 →
12,288 against 12,272 and 0.85 → 2.58x against 2.58x. What was wrong, both
times, is the estimate of how much memory would be *left* for cache.

The first miss came from assuming vLLM's overhead was fixed while the
**budget** grew. This one holds the budget still and changes two things at
once: the model *and* the window. The projection carried the overhead measured
during an 8192 run straight into a 16384 one, and **`--max-model-len` costs
non-KV memory of its own** — attention workspace, chunked-prefill buffers and
peak activation all scale with sequence length, not just the cache does.

So the lesson generalises rather than repeats: **vLLM's non-KV overhead scales
with anything you give it — the budget and the window both.** The only number
worth quoting is the one in the startup log:

```
docker compose logs vllm | grep -i "kv cache\|maximum concurrency"
```

**The decision holds comfortably even so.** Against the 32B it replaced: KV
tokens 21,135 → 60,768 (2.88x), concurrency 2.58x → 3.71x (1.44x), window 8192
→ 16384 (2x), and free VRAM ~3.4 → ~9.4 GiB. The failure that started this —
one 4,506-token tool result not fitting in 3,979 tokens of room — is now a
4,506-token result inside 12,171, with 7,665 to spare.

**Capability was the real risk, and it held.** This document already had the
evidence that model size matters here: the 8B failed 3 of 8 `check_agent`
scenarios, including inventing a SQL query against a database that was never
configured, and that is precisely why the 32B was chosen. A 14B sits between
them, and nothing but a measurement could say where.

Run against the live 14B on 9 September 2026, immediately after the swap:

| | 32B | 14B |
|---|---|---|
| `check_agent` | 6 of 8 | **6 of 8** |
| `check_search` | 8 of 8 | **8 of 8** |

Not merely the same totals — the **same two scenarios** fail (3 and 7), for the
same reason they failed on the 32B: both reach the correct, verified answer by
a valid tool path the assertion does not accept. Scenario 7 found the invoice
with `search_files` instead of `list_files` and built the right spreadsheet
from it. That is the test being strict about route rather than result, which is
what this file has said about those two since the 32B, and it is unchanged.

So the 14B costs nothing measurable on the workload this box exists for, and
the traces run noticeably quicker (2.4s where the 32B took 3.9s on the same
scenario). Had it regressed, the fallback keeping the larger model was
`--kv-cache-dtype fp8` on the 32B — roughly halves KV memory and would reach
16384 at about the old concurrency. It was not needed, and is recorded here
only so the next person does not have to re-derive it.

### 16384 to 32768, 14 September, confirmed measured 16 September

Not a repeat of the 8192→16384 move, and not the model-swap projection either
— those changed the weights or the budget, and both had to re-derive the
overhead from scratch. This changes only `--max-model-len`, with the model
and `--gpu-memory-utilization` (0.70) held fixed, so the KV cache pool itself
does not move — only how many requests' worth of it a full-length request
now costs.

```
Available KV cache memory: 9.27 GiB
GPU KV cache size: 60,768 tokens, Maximum concurrency for 32,768 tokens per
  request: 1.85x
```

**KV cache is unchanged — 9.27 GiB, 60,768 tokens, the same pool 16384 had.**
Concurrency is pure division against a fixed pool: 60,768 ÷ 16,384 = 3.71x
(recorded 9 September) and 60,768 ÷ 32,768 = 1.85x (this measurement) —
doubling the window exactly halves the ceiling, with no overhead surprise
this time, because nothing about the budget or the weights changed for vLLM
to mis-estimate.

**Correction to this file's own record:** the "Down to 14B" section above
cites "~76,700-token cache measured 9 September" while reasoning about what
32768 would cost — that number was never measured. It is the *projected*
figure from the paragraph just above it ("Predicted 11.71 GiB … 76,743
tokens … Measured 9.27 GiB … 60,768 tokens"), and using it produced a
projected concurrency of ~2.34x for this change, in `HANDOVER.md` and
`CHANGELOG.md`, before this section existed. The real number, measured, is
**1.85x**. Both documents are corrected alongside this entry.

**Free VRAM for Steps 5 and 6 does not need re-measuring for this reason —
it was never a function of `--max-model-len`.** The same startup log:

```
Free memory on device (30.86/31.36 GiB) on startup. Desired GPU memory
utilization is (0.7, 21.95 GiB). Actual usage is 11.22 GiB for consumed
memory (weights + non-torch), 1.46 GiB for peak activation, and 0.52 GiB
for CUDAGraph memory. Current kv cache memory in use is 9.27 GiB.
```

11.22 + 1.46 + 0.52 + 9.27 = 22.47 GiB against the 21.95 GiB budget — the
same small overshoot this log has shown at every prior setting — leaving
**30.86 − 22.47 ≈ 8.4 GiB** free outside vLLM's budget, against the ~9.4 GiB
recorded at 16384. Consistent within the log's own run-to-run noise, not a
regression: `--gpu-memory-utilization` and the model's weights are what set
this number, and neither changed. `HANDOVER.md`'s "Next, in order" item 5
and `docs/plans/step-05-embeddings.md`'s sub-step 5.0 both flagged this
figure as needing re-measurement before Step 5 spends against it — it has
now been re-measured, and the flag was more caution than the arithmetic
turned out to need.

**Still not measured by this: sustained throughput under real concurrent
load.** 1.85x is vLLM's own worst-case accounting — every concurrent request
assumed to fill the full 32768 window at once — not what the box delivers
against traffic shaped like this project's own transcripts (700–23,283
input tokens, per `scripts/bench_gateway.py`'s own docstring). That bench
exists now and has not been run; this measurement answers a different
question than it will.

## Embedding candidates (Step 5.1), measured 18 September 2026

`scripts/bench_embeddings.py` against the production box, not Ollama and not a
projection — `docs/plans/step-05-embeddings.md` §5.1's own two questions, both
answered with a real number.

**Does the running chat container already serve embeddings? No, confirmed
live.** `POST http://192.168.1.185:8000/v1/embeddings` against the deployed
`Qwen/Qwen3-14B-AWQ` container returns `HTTP 404 {"detail": "Not Found"}`. A
vLLM server serves one `--runner` per process — this one was started for
generation, not pooling — so an embedding role needs its own container. Not
assumed; this project has already thrown away one number that was assumed
instead of measured (see the Ollama disclaimer at the top of this file), and
was not going to do that twice in the same step.

**One candidate measured so far: `Qwen/Qwen3-Embedding-0.6B`** — the one
embedding-adjacent row `docs/licences.md` verified the same day as vLLM
itself. `--runner pooling` (this vLLM version has no `--task` flag; see
`scripts/bench_embeddings.py`'s docstring for the two wrong flags tried
before this one worked), `--max-model-len 2048` (still 4x this system's real
per-chunk ceiling — `TARGET_TOKENS` + `OVERLAP_TOKENS` in `app/chunks.py` is
576 — chosen with headroom rather than cut to the minimum for a first run).

| | |
|---|---|
| dimension | 1024 (4 KiB/chunk as float32, 2 KiB as float16) |
| single chunk (1792 chars, `TARGET_CHARS`) | median 7 ms, p95 8 ms |
| batch of 8 | 3.8 ms/chunk (1.7x vs. one at a time) |
| batch of 32 | 3.3 ms/chunk (2.0x) |
| batch of 64 | 3.2 ms/chunk (2.0x — batching's gain is mostly captured by 32) |

**The VRAM delta is 9.9 GB, and reporting that number alone would repeat this
file's own recorded mistake.** The startup log, the same shape already read
twice above for the chat model:

```
Free memory on device (10.21/31.36 GiB) on startup. Desired GPU memory
utilization is (0.3, 9.41 GiB). Actual usage is 1.57 GiB for consumed
memory (weights + non-torch), 0.52 GiB for peak activation, and 0.24 GiB
for CUDAGraph memory. Current kv cache memory in use is 7.31 GiB.
```

1.57 + 0.52 + 0.24 = **2.33 GiB is what this model actually costs to have
loaded and ready** — weights (`Model loading took 1.12 GiB`), overhead and
CUDA graphs. The other **7.31 GiB is KV cache, and KV cache here is a dial
this benchmark happened to leave wide open, not a requirement**:
`--gpu-memory-utilization 0.3` was chosen to clear the earlier crash (a
0.15 budget left too little room for even one 2048-token request), not
because a 0.6B embedding model needs 9.9 GiB. 68,432 cached tokens against
576-token real chunks is concurrency this write path will not use — the
embeddings producer calls this role in batches of a few dozen chunks
(§5.1's own batch sweep above), never dozens of documents at once. A
production deployment should size `--gpu-memory-utilization` or
`--kv-cache-memory` (the log names this flag directly) to a few GiB of
cache, not inherit this benchmark's number, and re-measure once 5.6 decides
real batch sizes.

### Second candidate: `BAAI/bge-base-en-v1.5`, and a retrieval-quality harness, 18 September

The plan's own "small BGE- or GTE-class" alternative — real BERT-family
encoder (768-dim, 512-token native limit, 109M params against Qwen's 600M),
MIT-licensed per its own model card (**unverified** in `docs/licences.md`
per this file's own primary-source rule — not read from source, so it stays
that way until someone does, same as sqlite-vec was before Step 5.0).

| | Qwen3-Embedding-0.6B | BAAI/bge-base-en-v1.5 |
|---|---|---|
| dimension | 1024 | 768 |
| single chunk (1792 chars) | 7 ms median, 8 ms p95 | 4 ms median, 4 ms p95 |
| batch of 32 | 3.3 ms/chunk | 1.5 ms/chunk |
| batch of 64 | 3.2 ms/chunk | 1.5 ms/chunk |
| real max context | 32,768 (inherited, unused) | 512 (native) |
| licence | **verified**, see first table | unverified, believed MIT |

**BGE is faster per chunk, consistent with being ~1/5th the parameters — and
the VRAM comparison needed a second correction in the same session that
already corrected one.** The first reading (5.09 GiB) was wrong for a new
reason: it was taken while Qwen's own candidate container was ALSO still
running, so the delta against the pre-Step-5.1 baseline was Qwen's footprint
plus BGE's, not BGE's alone — the mistake this file's own Ollama disclaimer
and the 9.9 GiB entry above both already warn about, arrived at a third way.
`nvidia-smi --query-compute-apps` (per-process, immune to what else is
running) isolates it: **BGE alone is 1.06 GiB**, Qwen alone (this run, a
tighter 0.15 utilization budget rather than the 2.33 GiB run above) was
3.90 GiB. BGE's own startup log reports `Model loading took 0.21 GiB` and
`Graph capturing finished... took 1.36 GiB`, which sum to more than the
1.06 GiB isolated measurement — **this does not fully reconcile, and is
recorded rather than smoothed over**, the way Qwen's log broke down clean
and BGE's does not. Both numbers agree on the direction: BGE's real footprint
is small, roughly a third to a fifth of Qwen's, tracking its parameter count.

**A real architectural difference, not just a smaller number: BGE has no
autoregressive KV cache to size at all.** Its runtime log reports `GPU KV
cache usage: 0.0%` throughout — an encoder-only forward pass has no
multi-step decode to cache. Qwen3-Embedding is a causal-LM backbone
(`Resolved architecture: Qwen3ForCausalLM`) repurposed for embedding via
pooling, so it inherits vLLM's KV-cache allocation machinery even though
single-pass embedding never needs it — which is the entire reason the 9.9
GiB / 2.33 GiB split above exists for Qwen and has no BGE equivalent to
draw.

**BGE's 512-token limit is real, and the char-based chunk-size estimate
underestimated it live, not just in theory.** `app/chunks.py`'s own
character estimate said 0 of 4,660 golden-corpus chunks exceeded 512
tokens; BGE's real tokenizer disagreed on the first run and hard-errored a
whole batch with a 400 (`This model's maximum context length is 512
tokens... requested... at least 513`) — vLLM does not truncate silently by
default. Fixed by passing `truncate_prompt_tokens`, vLLM's own field for
this, not by re-tuning the estimate.

**Retrieval quality: `scripts/bench_retrieval_candidates.py`, the same 42
golden queries and ground truth `scripts/check_retrieval.py` measures the
0.768 keyword baseline against, imported metric code so the numbers are
comparable, not merely similar.** Query-side instruction per each model's
own card (confirmed against both, not assumed): Qwen gets
`"Instruct: {task}\nQuery: {text}"`, BGE gets `"Represent this sentence for
searching relevant passages: "` — documents plain for both, since neither
card asks for a document-side prefix.

| | queries | MRR | R@1 | R@3 | R@5 | R@10 |
|---|---|---|---|---|---|---|
| **Qwen3-Embedding-0.6B**, overall | 42 | 0.654 | 0.516 | 0.580 | 0.588 | 0.621 |
| — clause | 14 | 0.205 | 0.143 | 0.214 | 0.214 | 0.286 |
| — exact | 20 | 0.967 | 0.950 | 1.000 | 1.000 | 1.000 |
| — paraphrase | 8 | 0.661 | 0.084 | 0.169 | 0.210 | 0.263 |
| **bge-base-en-v1.5**, overall | 42 | 0.657 | 0.537 | 0.566 | 0.614 | 0.665 |
| — clause | 14 | 0.335 | 0.286 | 0.286 | 0.357 | 0.429 |
| — exact | 20 | 0.935 | 0.900 | 0.950 | 1.000 | 1.000 |
| — paraphrase | 8 | 0.524 | 0.070 | 0.096 | 0.096 | 0.244 |

**Overall MRR is a tie (0.654 vs. 0.657, a 0.003 gap on 42 queries — well
under the ~0.024 one query flipping would move it) and hides two real,
opposite-direction differences underneath.** BGE is clearly stronger on
clause queries (0.335 vs. 0.205 MRR); Qwen is clearly stronger on
paraphrase queries (0.661 vs. 0.524 MRR). Both vector candidates alone
score below the 0.768 keyword baseline overall — expected and not a
concern by itself, since RRF fusion (§5.4) is what is supposed to combine a
retriever that wins on paraphrase with one that wins on exact-string
matches, not either replacing the other.

### Decision: Qwen3-Embedding-0.6B, 18 September 2026

Per the standard set for this comparison: not on latency alone, and BGE
would need a material retrieval-quality advantage to displace an
already-license-verified candidate. It does not have one where it counts
for this step. **Paraphrase is the specific failure mode Step 5 exists to
fix** — stated at the top of `docs/plans/step-05-embeddings.md`, "the
failure mode keyword search cannot fix by construction" — and Qwen beats
BGE on it by a wide, real margin (0.661 vs. 0.524 MRR), while BGE's own
advantage falls on clause queries, a category this step was not specifically
built to move. Combined with Qwen's already-verified licence against BGE's
still-unverified one, and both candidates' absolute latency being fast
enough in absolute terms (single-digit milliseconds) that BGE's speed edge
does not change what this write path can afford, Qwen3-Embedding-0.6B is
the selected candidate for `[roles.embed]`.

**Recorded as a real limitation, not swept aside because Qwen won overall:
BGE's clause-query strength (0.335 vs. 0.205 MRR, roughly 60% relative)
is a genuine gap in Qwen's coverage.** Worth revisiting if clause-type
queries turn out to matter more in production than the golden set's 14-of-42
share suggests, and `Qwen3-Reranker-0.6B` — already a licence row in
`docs/licences.md`, unverified, unused — is a plausible second-stage answer
if so, rather than re-running this comparison from scratch.

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
- `--max-model-len 32768` — the VRAM budget above. Callers do not have to know
  this number: `app/gateway.py` reads it from `/v1/models` and clamps
  `max_tokens` to what is left after the prompt. Without that the website's
  `max_tokens: 32000` is an HTTP 400 on every single request. Raised from
  8192 to 16384 on 9 September (see the section above for why 8192 turned out
  to be too small for a different reason than concurrency), then to 32768 on
  14 September when the same `max_tokens: 32000` request cleared 16384 but a
  real schema-plus-history prompt still measured within 50 tokens of it.
- `--gpu-memory-utilization 0.70` — everything left after the weights becomes
  KV cache, which is what concurrency is made of. Was 0.85 for one day; the 14B
  made that spending unnecessary, and the section below has both trades.
- `--revision <sha>` — a model repo moves like an image tag does. Pinned for
  the same reason the digest above is pinned.

## Currently running

`Qwen/Qwen3-14B-AWQ`, pinned to revision `31c69efc`, served by the `vllm`
service in `docker-compose.yml` and advertised to callers only as the alias
`syslab-default` (Step 3.2), at a 32768-token window — see "Down to 14B" and
"16384 to 32768" above. It replaced `Qwen/Qwen3-32B-AWQ` on 9 September.

The 32B replaced `Qwen/Qwen3-8B-AWQ`, which was the Step 1 smoke-test model and
proved the container and the reboot-persistence gate. The swap was not
cosmetic: on `scripts/check_agent.py` the 8B model failed 3 of 8 real
tool-calling scenarios, including hallucinating a SQL query against a database
that was never configured when asked to sum a spreadsheet it had already read.
The 32B model does that scenario correctly. See
`docs/plans/step-03-model-gateway.md`.

Section 13's `models.toml` profile file does not exist and is not missing: it
is **Step 4.7**, where it becomes the registry that says which model fills
which role — chat, embed, vision, stt, tts — with four of the five empty and an
empty role meaning *unavailable*, never a silent fallback. It was folded into
Step 5 on the reasoning that one model gave the file nothing to hold; Step 4's
rewrite on 11 September moved it back, because the file now holds the SHAPE of
four models that have been asked for, and declaring a slot before filling it is
the point of it.
Until then the alias table is `MODEL_ALIASES` in `app/config.py` and the served
model is the `--model` flag in `docker-compose.yml`. Reasoning in
`docs/plans/step-03-model-gateway.md` §3.4.

## Licence

Qwen3 family: Apache 2.0, verified per `docs/licences.md`.
