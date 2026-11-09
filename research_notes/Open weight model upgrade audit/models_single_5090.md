# Open-weight chat models to replace Qwen3-14B-AWQ on one RTX 5090 (vLLM), as of 7 Nov 2026

Scope and conventions used in these notes:
- Budget assumed for the chat model: ~28 GB of the 5090's ~31.4 GiB usable (the vLLM recipe measured ~31.4 GiB usable on one 5090; a separate log showed 30.9 GiB free at start). The other ~3 GB goes to the Qwen3-Embedding-0.6B instance.
- "Vendor" means the model maker's own numbers. "Independent" means Artificial Analysis (AA), the Berkeley BFCL board, or third-party users. "Aggregator" means sites like BenchLM or benchmarklist that repost numbers and do not always say where they came from.
- Any number marked "est." is my own arithmetic, not a measurement. The method is shown under Inferences.

## Q1. Which candidates exist as of Nov 2026, and which fit today's box?

### Takeaway
The Qwen 27B dense line (Qwen3.6-27B from Apr 2026, Qwen3.8-27B from Aug 2026) and Qwen3.6-35B-A3B fit at 4-bit (NVFP4/AWQ) with lots of KV room. So do gpt-oss-20b (MXFP4), Gemma 4 31B and 26B-A4B (4-bit), GLM-4.7-Flash, Nemotron 3 Nano 30B-A3B, Devstral Small 2 24B and Granite 4.1/4.2 30B. **No ~30-35B model fits at FP8 in 28 GB.** Mistral Small 4 (119B) and every new flagship (Kimi K3.1, DeepSeek V4.x, Mistral Large 4) are far too big.

### Cited Findings
**Qwen**
- Qwen3.6-35B-A3B (MoE, 35B total / 3B active) and Qwen3.6-27B (dense, multimodal) are open-weight under Apache 2.0, released April 2026. One tracker dates the 35B-A3B to Apr 16 and the 27B to Apr 22. — [Qwen blog 27B](https://qwen.ai/blog?id=qwen3.6-27b); [Qwen blog 35B-A3B](https://qwen.ai/blog?id=qwen3.6-35b-a3b); [willitrunai timeline](https://willitrunai.com/blog/qwen3-6-35b-a3b-release-date)
- Qwen3.8-27B is on Hugging Face under Apache-2.0. The citation says August 2026 and the collection was updated Aug 13. A third party dates the release to Aug 14, 2026. Specs: 27B (Hub metadata shows 28B), dense, with a vision encoder. — [HF Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B); [Codersera](https://codersera.com/blog/qwen-3-5-complete-guide-2026/)
- I found no Qwen3.8 MoE (no "Qwen3.8-35B-A3B"). The search returned only Qwen3.5/3.6-35B-A3B quants. — [search result set incl. NGC listing](https://catalog.ngc.nvidia.com/orgs/nim/teams/qwen/models/qwen3.6-35b-a3b)
- One analysis says Qwen flagship releases since July 2026 have been closed-weight (e.g. Qwen3.7 Max/Plus). It calls the Qwen3.6 pair "the last open general-purpose checkpoints", which contradicts the existence of the Aug 2026 Qwen3.8-27B open release. — [digitalapplied](https://www.digitalapplied.com/blog/qwen-closed-flagship-pivot-open-weight-retreat-2026); contradicted by [HF Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)

**OpenAI**
- gpt-oss-20b (Aug 2025) is still OpenAI's latest open-weight small model. Talk of a "gpt-oss-2" was only rumour as of Oct 2026. — [aiera](https://aiera.blog/?p=1948); [OpenAI announcement](https://openai.com/index/introducing-gpt-oss/)

**Google**
- Gemma 4 comes in E2B, E4B, 26B-A4B MoE (128 experts, top-8) and 31B dense sizes. It has been in vLLM since early April 2026. — [vLLM blog Gemma 4](https://vllm.ai/blog/2026-04-02-gemma4); [vLLM Gemma4 recipe](https://docs.vllm.ai/projects/recipes/en/latest/Google/Gemma4.html)

**Mistral**
- Mistral Small 4 (Mar 2026) is a 119B MoE with ~6.5B active, native FP8 and 256K context. It does not fit one 32 GB card. — [vLLM recipe Mistral Small 4](https://recipes.vllm.ai/mistralai/Mistral-Small-4-119B-2603); [Mistral blog](https://mistral.ai/en/news/mistral-small-4/)
- Devstral Small 2 (24B, Dec 2025) scores 68.0% on SWE-bench Verified and uses the `mistral` tool parser. Ministral 3 (3B/8B/14B) shipped Dec 2025. — [HF Devstral-Small-2-24B-Instruct-2512](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512); [Mistral docs](https://docs.mistral.ai/models/overview)

**Others**
- GLM-4.7-Flash: 30B-A3B MoE, MIT licence. vLLM flags are `--tool-call-parser glm47 --reasoning-parser glm45`. At launch the model card said it needed vLLM main/nightly. — [HF zai-org/GLM-4.7-Flash](https://huggingface.co/zai-org/GLM-4.7-Flash)
- NVIDIA Nemotron 3 Nano 30B-A3B (Dec 2025) and Nemotron 3 Nano Omni 30B-A3B both use `--tool-call-parser qwen3_coder` in vLLM. They ship as BF16, FP8 and NVFP4. — [vLLM blog Nemotron 3 Nano](https://vllm.ai/blog/2025-12-15-run-nvidia-nemotron-3-nano); [HF Nemotron-3-Nano-Omni NVFP4](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4)
- IBM Granite 4.1 (Apr 29, 2026, Apache 2.0) comes in dense 3B, 8B and 30B, with 512K context on the 8B and 30B. A secondary source says Granite 4.2 8B/30B also exist and use the `qwen3_coder` parser per IBM. — [IBM Granite 4.1 docs](https://www.ibm.com/granite/docs/models/granite4-1); [DataNorth](https://datanorth.ai/news/ibm-releases-granite-4-1); [vdf.ai](https://vdf.ai/blog/best-local-llm-for-agents-tool-calling/)
- Nanbeige4.3-33B, an open-weights model listed Nov 5, 2026, scores 20 on AA Intelligence Index v5, the same as Qwen3.8 27B (xhigh). It is described as "stronger terminal use but weaker knowledge". AA gives no licence or architecture. — [AA recent launches](https://artificialanalysis.ai/articles/recent-open-weights-model-launches)
- Thinking Machines Inkling 2 Small (Nov 5, 2026) scores 31 on AA, but its weights were not yet released, so it is not open-weight yet. — [AA recent launches](https://artificialanalysis.ai/articles/recent-open-weights-model-launches)
- No Phi-5 or Granite 5 release turned up in search. — [search for Granite/Phi 2026](https://www.llmreference.com/model-family/granite-4.1)

### Inferences
- **Fits at 4-bit:**
  - Qwen3.8-27B and Qwen3.6-27B
  - Qwen3.6-35B-A3B
  - Gemma 4 31B and 26B-A4B
  - GLM-4.7-Flash
  - Nemotron 3 Nano 30B-A3B
  - Devstral Small 2 24B
  - Granite 4.1/4.2 30B
  - Ministral 3 14B
- **Fits natively:** gpt-oss-20b (MXFP4).
- **Does not fit at FP8 within 28 GB:** any 30-35B model at FP8. At ~1 byte/param a 35B model needs ~35 GB for weights alone. Gemma 4 26B-A4B at FP8 (~26-27 GB est.) would leave almost no KV room.
- **Does not fit at all:** Mistral Small 4 (119B), Kimi K3.1 Pro, DeepSeek V4.x, Mistral Large 4.

### Gaps
- I could not confirm Gemma 4's licence text in this session; check the HF card. I also did not confirm the licences for Devstral Small 2, Nemotron 3 Nano (likely NVIDIA Open Model License) or Nanbeige4.3-33B.
- I did not verify Granite 4.2's release date or model card directly; it is known only through vdf.ai.
- I could not confirm whether a newer Ministral, Magistral or Phi checkpoint than those listed exists.

## Q2. Per-candidate specs: params, licence, Blackwell quant, weight size, KV at 32K, vLLM parsers

### Takeaway
The Qwen3.6/3.8 hybrid Gated-DeltaNet models are unusually cheap on KV cache: 64 KiB/token on the 27B and 20 KiB/token on the 35B-A3B, against ~160 KiB/token for today's Qwen3-14B. So 32K context costs only ~0.6-2 GiB, even though the weights are larger. On the 5090 the practical formats are NVFP4 (native W4A4 on SM120 from vLLM 0.30) or AWQ/GPTQ-int4 through Marlin. gpt-oss-20b stays in its native MXFP4.

### Cited Findings
**Qwen3.8-27B: architecture and settings**
- Layout: 64 layers in a 3:1 mix, 48 Gated DeltaNet (linear) layers and 16 Gated Attention layers. The full-attention layers have 24 Q heads, **4 KV heads** and head_dim 256. Context is 262,144 native, up to 1M with YaRN.
- Thinking is on by default. `reasoning_effort` accepts xhigh (default), medium or low. `preserve_thinking` is on by default.
- Non-thinking sampling: temperature 0.7, top_p 0.8, top_k 20, presence_penalty 1.5.
- Source: [HF Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)

**Qwen3.8-27B: vLLM recipe**
- Flags: `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`. Prerequisites are vLLM ≥0.17.0 and transformers ≥5.8.0.
- Variants listed: NVFP4 (`Inferact/Qwen3.8-27B-NVFP4`, `unsloth/Qwen3.8-27B-NVFP4`), MXFP4 W4A4 (`RedHatAI/Qwen3.8-27B-MXFP4`) and FP8 (`Qwen/Qwen3.8-27B-FP8`, shown only at TP4 or on Ascend). No AWQ is listed.
- For a single 5090 the recipe uses `--max-model-len 32768 --kv-cache-dtype fp8 --enforce-eager`. It warns that startup without `--enforce-eager` OOMs during CUDA-graph capture, and that lowering `--gpu-memory-utilization` does not fix it.
- The 5090 runs were verified on `0.26.1rc1.dev608`. The DFlash2 drafter (`incoai/Qwen3.8-27B-DFlash2`) needs vLLM ≥0.28.0. The built-in MTP is `--speculative-config '{"method":"mtp","num_speculative_tokens":3}'`.
- The gpt-oss-style native `Mxfp4Config` path does not work for this model; use the RedHatAI compressed-tensors MXFP4 build instead.
- Source: [vLLM recipe Qwen3.8-27B](https://recipes.vllm.ai/Qwen/Qwen3.8-27B)
- `Inferact/Qwen3.8-27B-NVFP4` is Apache-2.0. The Hub shows "18B params" (packed U8 plus BF16) and publishes no accuracy-recovery numbers. — [HF Inferact NVFP4](https://huggingface.co/Inferact/Qwen3.8-27B-NVFP4)

**Qwen3.6-27B and Qwen3.6-35B-A3B**
- Qwen3.6-27B has the same 64-layer, 48/16 layout, 4 KV heads and head_dim 256 as Qwen3.8-27B, with 262K native context. Its card names no official FP8 checkpoint. — [HF Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B)
- Qwen3.6-35B-A3B architecture: 40 layers (30 Gated DeltaNet, 10 Gated Attention). The attention layers have **2 KV heads** and head_dim 256. It uses 256 experts (8 routed + 1 shared) and has 262K native context.
- Qwen3.6-35B-A3B flags: `--language-model-only` frees memory for KV; MTP is `{"method":"qwen3_next_mtp","num_speculative_tokens":2}`; the parsers are `qwen3_coder` and `qwen3`.
- Source: [HF Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- The vLLM recipe for Qwen3.6-35B-A3B adds `--trust-remote-code --mm-encoder-tp-mode data` and needs vLLM ≥0.17.0. — [vLLM recipe Qwen3.6-35B-A3B](https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B.md)
- **Measured on one 5090, Qwen3.6-27B NVFP4** (community build `Qwen3.6-27B-NVFP4-GPTQ`):
  - 19.15 GB on disk and 18.48 GB in VRAM.
  - With `--kv-cache-memory-bytes 10000000000` (9.31 GiB) and `--kv-cache-dtype fp8_e4m3`, the KV cache held 292,275 tokens.
  - Other flags: `--max-num-seqs 16`, `--max-num-batched-tokens 8192`, `--max-cudagraph-capture-size 16` (graphs cost 0.91 GB, so no `--enforce-eager` was needed), `--reasoning-parser qwen3 --tool-call-parser qwen3_coder`.
  - Total use was ~28.7 GB of 32 GB. A patched chat template was needed for correct tool calling and reasoning parsing. `--kv-cache-memory-bytes` overrides `--gpu-memory-utilization`.
  - Source: [jpmrblood codeberg, Aug 2026](https://jpmrblood.codeberg.page/2026/08/07-vllm-docker-nvfp4-27b-rtx5090-command-line-breakdown/)
- **Measured on one 5090, Qwen3.6-35B-A3B NVFP4** ("heretic" variant, 22.84 GiB):
  - Run at util 0.94 and `max-model-len 32768`, with BF16 KV (FP8 KV was incompatible with the vision attention). A 31.8K-token prefill was tested.
  - Throughput was ~190-300 t/s with a DFlash drafter.
  - Official vLLM 0.22 lacked an SM120 CUTLASS NVFP4 grouped-GEMM, so the MoE layers fell back to Marlin INT4 while the linear layers used FlashInfer NVFP4.
  - Source: [note.com, Jun 2026](https://note.com/unco3/n/n8212ee12811b?hl=en)
- **Other Qwen3.6-35B-A3B quants:** NVIDIA NGC lists FP8 and NVFP4 builds tested with vLLM. Community NVFP4 builds include `kyaky/Qwen3.6-35B-A3B-NVFP4` (FP8 attention and GDN, NVFP4 experts) and `Infatoshi/Qwen3.6-35B-A3B-NVFP4-FP8` (~24 GB). — [NGC](https://catalog.ngc.nvidia.com/orgs/nim/teams/qwen/models/qwen3.6-35b-a3b); [kyaky](https://huggingface.co/kyaky/Qwen3.6-35B-A3B-NVFP4); [Infatoshi](https://huggingface.co/Infatoshi/Qwen3.6-35B-A3B-NVFP4-FP8)

**SM120 (5090) kernel status by vLLM version** (secondary source dated Oct 26 / Nov 5, 2026):
- 0.30.0 (Sep 22): native W4A4 NVFP4 became the default on SM120/121.
- 0.31.0 (Oct 5): fixed a **High-rated RCE for servers started with `--trust-remote-code`**.
- 0.32.0 (Oct 18): NVFP4 KV cache and Triton MXFP4 MoE (the gpt-oss path) on SM12x. Default builds moved to CUDA 13.0, needing driver R580+.
- 0.33.0 (Nov 2): CUDA 13 only.
- Without a native kernel, vLLM falls back to Marlin W4A16 and logs a warning.
- Source: [eurokommerz](https://eurokommerz.at/blog/blackwell-rtx-sm120-software-support/). Corroborating history: [vLLM PR #24968, NVFP4 MoE on SM120](https://github.com/vllm-project/vllm/pull/24968); [CUTLASS issue #3096, SM120 grouped GEMM garbage output](https://github.com/NVIDIA/cutlass/issues/3096)

**gpt-oss-20b**
- 21B total / 3.6B active, Apache 2.0, runs within 16 GB. — [OpenAI](https://openai.com/index/introducing-gpt-oss/)
- vLLM flags are `--tool-call-parser openai --enable-auto-tool-choice`. On Chat Completions, function calling supports only `tool_choice="auto"`. The recipe says RTX 5090 (sm_120) support is "in progress". — [vLLM GPT-OSS recipe](https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html)

**Gemma 4 31B**
- vLLM flags: `--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 --chat-template examples/tool_chat_template_gemma4.jinja`. Needs vLLM ≥0.19.1.
- Variants: FP8 `RedHatAI/gemma-4-31B-it-FP8-dynamic`; NVFP4 `nvidia/gemma-4-31B-it-NVFP4`, which the recipe labels "Blackwell only (B200/B300)", with SM120 not mentioned.
- The architecture alternates sliding-window and global attention.
- Structured-output caveat: the model does not see JSON-schema descriptions, so meaning has to go in the system prompt.
- Source: [vLLM recipe Gemma 4 31B](https://recipes.vllm.ai/Google/gemma-4-31B-it?variant=w4a16)

**Qwen3-14B (baseline)**
- 40 layers, 40 Q / 8 KV heads, 32,768 native context (131,072 with YaRN), Apache 2.0. — [HF Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B)

### Inferences
**Fit table for a ~28 GB budget.** KV figures are BF16 unless marked. Per-token KV = 2 × full-attention layers × KV heads × head_dim × bytes.

| Model (format) | Weights in VRAM | KV/token | KV for one 32K request | Room left for KV in 28 GB (after ~2 GB graphs/activations) | Verdict |
|---|---|---|---|---|---|
| Qwen3-14B-AWQ (today) | ~10 GB est. | 160 KiB (40×8×128; head_dim 128 from config, not re-fetched) | 5.0 GiB | ~16 GiB, about 3 × 32K | baseline |
| Qwen3.8-27B / Qwen3.6-27B NVFP4 | 18.5 GB (measured, 3.6-27B) | 64 KiB (16×4×256); FP8 ≈ 33 KiB measured | 2.0 GiB (1.0 GiB FP8) | ~7 GiB, about 3 × 32K BF16 or ~200K tokens FP8 | fits comfortably |
| Qwen3.6-35B-A3B NVFP4/AWQ | 21-24 GB (22.84 GiB measured heretic; ~24 GB mixed) | 20 KiB (10×2×256) | 0.63 GiB | ~2-5 GiB, about 3-8 × 32K | fits, tighter on weights; use `--language-model-only` |
| Qwen3.6-35B-A3B FP8 | ~35+ GB est. | — | — | negative | **does not fit** |
| gpt-oss-20b MXFP4 | ~13 GB est. (OpenAI: "within 16 GB") | ~24 KiB est. (12 full layers × 8 KV × 64; sliding layers capped at 128 tokens) | ~0.75 GiB | >10 GiB, so the full 131K context plus concurrency | fits easily |
| Gemma 4 31B 4-bit | ~19-20 GB est. | unknown (mixed sliding/global) | unknown | likely fine at 32K | fits at 4-bit; FP8 (~31 GB) does not |
| GLM-4.7-Flash / Nemotron 3 Nano 30B-A3B 4-bit | ~17-20 GB est. | not checked | — | likely fine | fits at 4-bit only |
| Devstral Small 2 24B 4-bit | ~13-14 GB est. | not checked | — | fine | fits (FP8 ~24 GB + KV is too tight) |

- The measured run confirms that a 27B NVFP4 model fits in ~28.7 GB total with a 9.3 GiB KV pool. Our budget is ~0.7 GB smaller, so shrinking `--kv-cache-memory-bytes` to ~6-7 GB still covers 32K-64K comfortably.
- Because KV is cheap on the Qwen3.6/3.8 hybrids, there is no need to drop to FP8 KV at 32K. BF16 KV avoids the "FP8 KV without scaling factor may lose accuracy" warning seen in the codeberg log, which matters for tool-argument exactness.
- The user is on vLLM 0.28.0. Upgrading to ≥0.31 is advisable for native SM120 W4A4 NVFP4 (0.30) and the `--trust-remote-code` RCE fix (0.31). Going to 0.32+ means moving the Docker host to CUDA 13 / driver R580+.

### Gaps
- No official FP8 or AWQ checkpoint for Qwen3.8-27B fits 28 GB. No NVFP4 accuracy-recovery data was published for `Inferact/Qwen3.8-27B-NVFP4`.
- I found no source confirming that NVFP4 *MoE* (as opposed to dense) runs on native SM120 kernels in vLLM 0.30-0.33 rather than the Marlin fallback.
- Gemma 4 31B KV-per-token and real NVFP4/w4a16 size on a 5090 were not found.
- Exact weight sizes for gpt-oss-20b, GLM-4.7-Flash, Nemotron 3 Nano and Devstral Small 2 quants were not fetched, so those are estimates.

## Q3. Benchmark evidence (BFCL, tau2, text-to-SQL, IFEval/IFBench, long context) vs the Qwen3-14B baseline

### Takeaway
Independent numbers for the 2026 small models are thin. The official BFCL v4 data file still only has 2025-era models: **Qwen3-14B scores 41.0% overall and 34.8% multi-turn**. None of Qwen3.5/3.6/3.8, gpt-oss-20b or Gemma 4 appear in it. AA and tau2 data (largely AA's telecom harness via an aggregator) put Qwen3.6-27B and 35B-A3B at ~94-95%, far ahead of gpt-oss-20b and Gemma 4 31B (~60%). There is **no published BIRD or Spider 2.0 score for any candidate**.

### Cited Findings
**Function calling: BFCL v4**
- Official data file (page says last updated 2026-04-12), FC mode: Qwen3-14B 41.03% overall, non-live 84.94%, live 80.01%, **multi-turn 34.75%**, web-search 10.0%, memory 19.57%, irrelevance 81.94%. For comparison: Qwen3-32B 48.71% (multi-turn 47.87%), GLM-4.6 72.38% (multi-turn 68.0%), Mistral-small-2506 37.15% (multi-turn 11.5%), Gemma-3-27b (Prompt) 29.47% (multi-turn 10.75%). No Qwen3.5/3.6/3.8, gpt-oss or Gemma 4 rows. — [BFCL data_overall.csv](https://gorilla.cs.berkeley.edu/data_overall.csv); [BFCL leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html)
- Vendor or self-reported BFCL v4: Qwen3.6-35B-A3B 65.0, Gemma 4 26B-A4B 57.09, Qwen3.5-9B 66.1. Aggregator; launch-post numbers. — [benchmarklist BFCL v4](https://benchmarklist.com/benchmarks/bfcl_v4/); [vdf.ai](https://vdf.ai/blog/best-local-llm-for-agents-tool-calling/)
- IBM self-reported: Granite-4.1-30B BFCL v3 73.7 vs Gemma-4-31B 72.7 on IBM's chart. Granite 4.2 8B/30B BFCL v4 52.39 / 61.39 and τ³-bench 66.34 / 68.05. — [Firethering via reader](https://hn.jakechampion.name/reader/https://firethering.com/granite-4-1-ibm-open-source-model-family/); [vdf.ai](https://vdf.ai/blog/best-local-llm-for-agents-tool-calling/)

**Agentic tool use: tau2 / tau3**
- τ² rows, aggregator, page updated Nov 6, 2026. The page says "most current rows come from Artificial Analysis's telecom implementation" but labels no individual row. Scores:

  | Model | τ² |
  |---|---|
  | Qwen3.6-35B-A3B | 95.3% |
  | Qwen3.6-27B | 94.2% |
  | Qwen3.5-27B | 93.9% |
  | Qwen3.5-35B-A3B | 89.2% |
  | GPT-OSS 20B | 60.2% |
  | Gemma 4 31B | 59.9% |
  | Nemotron 3 Nano Omni | 45.3% |
  | Gemma 4 26B-A4B | 43.6% |
  | "Devstral Small" (version unclear) | 28.4% |
  | Nemotron 3 Nano 30B | 25.4% |
  | Phi-4 | 0% |

  — [BenchLM τ²](https://benchlm.ai/benchmarks/tau2-bench)
- Vendor tau2 figures differ: Gemma 4 31B 76.9% (Google, average of 3), Gemma 4 26B-A4B 68.2%, GLM-4.7-Flash 79.5% (Z.ai), Qwen3.5-9B 79.1. — [vdf.ai](https://vdf.ai/blog/best-local-llm-for-agents-tool-calling/); [regolo comparison](https://regolo.ai/gemma-4-31b-vs-qwen3-6-35b-a3b-when-to-use-which/)
- Vendor agentic table, from Qwen's card:

  | Benchmark | Qwen3.6-35B-A3B | Qwen3.5-27B | Gemma4-31B | Gemma4-26B-A4B |
  |---|---|---|---|---|
  | TAU3-Bench | 67.2 | 68.4 | 67.5 | 59.0 |
  | MCP-Atlas | 62.8 | 68.4 | 57.2 | 50.0 |
  | MCPMark | 37.0 | 36.3 | 18.1 | 14.2 |
  | Tool Decathlon | 26.9 | 31.5 | 21.2 | 12.0 |
  | Claw-Eval Pass^3 | 50.0 | 46.2 | 25.0 | 28.0 |

  — [HF Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- Qwen3.6-27B vendor: Claw-Eval Avg 72.4 / **Pass^3 60.6**, Terminal-Bench 2.0 59.3, SWE-bench Verified 77.2. — [HF Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B)
- Qwen3.8-27B vendor: Terminal-Bench 2.1 73.0, SWE-bench Pro 61.7, CoWorkBench 70.7, **IFBench 79.5**, LiveCodeBench v6 90.3, OmniDocBench 1.5 91.1. **No BFCL, tau2, IFEval or SQL rows.** — [HF Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)

**Independent composite: Artificial Analysis**
- Intelligence Index v4, Apr 13, 2026 article:

  | Model | Index | Output tokens to run the index |
  |---|---|---|
  | Qwen3.5 27B (reasoning) | 42 | 98M |
  | Qwen3.5 27B (non-reasoning) | 37 | 25M |
  | Gemma 4 31B (reasoning) | 39 | 39M |
  | Gemma 4 31B (non-reasoning) | 32 | 7.1M |
  | Qwen3.5 35B-A3B | 37 | — |
  | Gemma 4 26B-A4B | 31 | — |
  | GLM-4.7-Flash | 30 | — |
  | Nemotron Cascade 2 30B-A3B | 28 | — |
  | gpt-oss-20B (high) | ~24 | — |
  | Nemotron 3 Nano 30B-A3B | ~24 | — |

  Qwen3.5 27B IFBench was 76%. — [AA sub-32B article](https://artificialanalysis.ai/articles/sub-32b-open-weights)
- AA on X: Qwen3.6 27B scored 46, "new open weights leader under 150B", but used ~3.7x the output tokens and cost ~21x more than Gemma 4 31B (39) to run the index. — [AA on X](https://x.com/ArtificialAnlys/status/2049881951260283097)
- AA Index v5 (Nov 2026, rescaled): Qwen3.8 27B (xhigh) = 20 = Nanbeige4.3-33B. This is not comparable with v4 numbers. — [AA recent launches](https://artificialanalysis.ai/articles/recent-open-weights-model-launches)
- Instruction following: Qwen3.8-27B IFBench 79.5 vs Qwen3.6-27B 69.1, per Qwen's table. A third-party tracker lists the best verified IFBench at 89.2. — [regolo Qwen3.8 benchmarks](https://regolo.ai/qwen3-8-27b-benchmarks-every-test-where-alibabas-27b-model-beats-claude-opus-4-6-max/); [benchlm Qwen3.8](https://benchlm.ai/models/qwen3-8-27b)

**Text-to-SQL**
- No BIRD EX or Spider 2.0 result was found for Qwen3.5/3.6/3.8, gpt-oss-20b or Gemma 4. — [search results incl. SIRIUS-SQL](https://arxiv.org/pdf/2606.01246)
- LiveSQLBench Base-Lite: Qwen3-235B-A22B 26.90%, Qwen3-Coder-480B 26.21%, top o3-mini 31.15%. — [LiveSQLBench (mirror)](https://kelaenderkoe.pages.dev/float-https-livesqlbench.ai)
- Spider 2.0-Snow: the best open-weight method (ReViSQL-235B-A22B with 129 candidates) reaches 55.58%. — [ReViSQL](https://arxiv.org/html/2603.20004v1)
- Informal 2025 test: gpt-oss-20b ~85% vs Qwen3-30B-A3B 75%, using a custom value-match check, not BIRD EX. — [datamonkeysite](https://datamonkeysite.com/2025/08/07/using-gpt-oss-20b-for-text-to-sql/)
- Benchmark reliability: a 2026 CIDR/VLDB paper finds annotation error rates of 52.8% on BIRD Mini-Dev and 66.1% on Spider 2.0-Snow. — [Jin et al. 2026](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf)

**Long context**
- All Qwen3.6/3.8 models are 262K native. Gemma 4 headers list 262K. Granite 4.1 30B lists 512K. No AA-LCR or RULER numbers were found for the candidates. — [HF Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B); [Gemma 4 recipe](https://recipes.vllm.ai/Google/gemma-4-31B-it?variant=w4a16); [DataNorth Granite 4.1](https://datanorth.ai/news/ibm-releases-granite-4-1)

### Inferences
- Treat the τ² rows as roughly independent (AA telecom), since that is what BenchLM says about most rows, but unverified per row.
  - On that basis, the Qwen 3.5/3.6 family is in a different league from gpt-oss-20b and Gemma 4 for multi-turn customer-service-style tool use.
  - Qwen3-14B has no τ² row here. Its old BFCL v4 multi-turn of 34.75% is the clearest evidence of why it fabricates or stalls in multi-turn tool chains.
- For SQL, the best available proxies are coding strength (LiveCodeBench, SWE-bench) and instruction following. Those favour Qwen3.8-27B, then Qwen3.6-27B, then Qwen3.6-35B-A3B.
  - The user should build a small in-house Postgres eval (e.g. 50-100 NL→SQL pairs over the syslab schema, scored by execution-result match). Public SQL leaderboards neither cover these models nor are reliable (>50% annotation errors).
- For extraction, schema conformance is best guaranteed by vLLM structured outputs (`response_format: json_schema`) rather than by the model. Semantic accuracy then tracks IFBench and OmniDocBench, where Qwen3.8-27B leads (vendor).
- AA token counts imply that Qwen thinking at default effort is 2.5-4x more verbose than Gemma 4. On a single GPU, use non-thinking mode or `reasoning_effort: low/medium` for extraction and SQL to keep latency down.

### Gaps
- No independent BFCL v4 numbers for any 2026 candidate; the Berkeley CSV has not added them.
- No BIRD or Spider 2.0 numbers for any candidate.
- No AA long-context (AA-LCR) numbers were retrieved.
- No τ² or BFCL numbers for Qwen3.8-27B anywhere.
- IFEval, as distinct from IFBench, was not found for these models.

## Q4. Dense ~27-32B at 4-bit vs 30B-A3B MoE vs gpt-oss-20b: which is more reliable at tool calling on vLLM?

### Takeaway
The premise "30B-A3B at FP8" does not fit 28 GB, so the real choice is a dense 27B at NVFP4, a 35B-A3B at NVFP4/AWQ, or gpt-oss-20b at MXFP4. What evidence exists favours the **dense 27B for reliability**. It wins on vendor pass^3 consistency (60.6 vs 50.0), and a community report found the 35B-A3B more prone to loops and malformed calls in long agent runs. The MoE is the throughput pick (~3-5x faster decode). gpt-oss-20b is the weakest on agentic scores and has an open vLLM Harmony bug that produces phantom tool calls.

### Cited Findings
- Claw-Eval Pass^3 (all 3 attempts succeed), vendor: Qwen3.6-27B 60.6 vs Qwen3.6-35B-A3B 50.0 vs Gemma4-31B 25.0. — [HF Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B)
- τ² (aggregator, likely AA): Qwen3.6-35B-A3B 95.3% and Qwen3.6-27B 94.2%, essentially tied, while GPT-OSS 20B scores 60.2%. — [BenchLM τ²](https://benchlm.ai/benchmarks/tau2-bench)
- Old independent BFCL v4 shows dense size helping multi-turn: Qwen3-32B 47.87% vs Qwen3-14B 34.75%. — [BFCL CSV](https://gorilla.cs.berkeley.edu/data_overall.csv)
- Community blog (Apr 19, 2026; seen only as a search snippet because the page returned 404 on fetch):
  - The author recommends the `qwen3_xml` parser over `qwen3_coder` "for demanding agentic traces".
  - The author found Qwen3.6-35B-A3B "more prone to reasoning loops and to malformed tool calls that interrupt long agent runs than Qwen3.5-27B". This is a single author's testing.
  - Source: [allanchan339 blog](https://allanchan339.github.io/bug-fixes/2026/04/19/Qwen36-35B-A3B-tool-calling.html)
- Without `--tool-call-parser`, Qwen3.6 emits tool-call JSON in the text but vLLM does not turn it into structured `tool_calls`. This is a classic cause of "the model said it called the tool". — [DEV guide](https://dev.to/lavellehatcherjr/serving-qwen36-35b-a3b-with-vllm-and-building-a-coding-agent-with-tool-calling-2kob)
- Qwen3.6 needs a patched chat template for correct tool calling and reasoning parsing on vLLM. — [codeberg 5090 log](https://jpmrblood.codeberg.page/2026/08/07-vllm-docker-nvfp4-27b-rtx5090-command-line-breakdown/)
- **gpt-oss-20b Harmony parser bug (#58825):**
  - Opened Sep 26, 2026 on vLLM 0.30.0 and main; still open, with fix PR #59225 also open.
  - A stray `<|channel|>commentary to=assistant` header becomes a tool call named `assistant<|channel|>analysis`, which is not one of the request's tools. Related: #32587 (`<|channel|>` leaking into tool names in multi-turn).
  - Source: [vLLM issue #58825](https://github.com/vllm-project/vllm/issues/58825)
- Older practitioner report: on vLLM, gpt-oss tool results were "often ignored, or cut short. Sometimes replaced with a hallucinated answer". The author blamed Harmony handling. The post is from Sep 2025 and may predate fixes. — [Visokio](https://visokio.com/2025/09/19/lessons-from-the-trenches-why-llama-cpp-works-best-today/)
- NVFP4 Nemotron 3 Nano: one user reports repetition and failed tool calls in Opencode; another says NVIDIA's tool-calling notebook works. — [NVIDIA forum](https://forums.developer.nvidia.com/t/nemotron-3-nano-30b-a3b-nvfp4-ultra-efficient-nvfp4-precision-version-of-nemotron-3-nano/359074?page=3)
- vLLM docs (per vdf.ai): with `tool_choice="auto"` and no strict schemas, arguments can occasionally be malformed. Required or named tool choice, or a stricter level, constrains output. — [vdf.ai](https://vdf.ai/blog/best-local-llm-for-agents-tool-calling/)
- Speed: Qwen3.6-35B-A3B NVFP4 runs ~190-300 t/s on a 5090 with a drafter. No measured stock-vLLM tok/s was found for the 27B dense on a 5090. — [note.com](https://note.com/unco3/n/n8212ee12811b?hl=en); [search summary](https://modelfit.io/gpu/rtx-5090/) (estimates only)

### Inferences
- **For "must actually call tools rather than fabricate results"**, rank them as follows:
  1. Dense Qwen3.8-27B or Qwen3.6-27B (best pass^3, no MoE loop reports).
  2. Qwen3.6-35B-A3B (equal τ², lower pass^3, community loop and malformed-call reports).
  3. gpt-oss-20b (much lower τ², open phantom-tool-call parser bug).
- **Quantization effect:** I found no study of NVFP4 vs BF16 tool-calling accuracy for these models. The dense 27B is likely more robust to 4-bit than a 3B-active MoE whose experts are quantized, but this is untested.
- **Fabrication guards** that are independent of the model:
  - Always set the correct parser and the patched template.
  - Keep BF16 KV.
  - For Qwen, keep thinking on with `preserve_thinking` and pass the reasoning back across turns, so the model remembers why it called a tool.
  - Use `tool_choice: "required"` or a named tool on turns where a tool call is mandatory.
  - In the app, reject any assistant turn that states a tool result without a matching `tool` message.

### Gaps
- No independent head-to-head of tool-call fabrication rates on vLLM for these three model types.
- The allanchan blog body could not be fetched (404), so its quantitative results are unknown.
- No measured Qwen3.8-27B NVFP4 decode tok/s on a 5090.
- Whether vLLM 0.28 has a working `qwen3_xml` parser for Qwen3.6/3.8 was not verified.

## Q5. Ranked shortlist and the top 2-3 to trial, with vLLM launch flags

### Takeaway
Trial in this order:
1. **Qwen3.8-27B NVFP4**: best quality per GB, Apache-2.0, fits with ~3 × 32K BF16 KV.
2. **Qwen3.6-35B-A3B 4-bit**: fastest; good for bulk extraction and SQL, with somewhat weaker long-run tool reliability.
3. **gpt-oss-20b**: low-VRAM, Apache-2.0 control and fallback. Clean JSON output, but weaker agentic scores and a known vLLM Harmony bug.

Gemma 4 31B is the non-Qwen alternative if vendor diversity matters. All three Qwen-family picks beat Qwen3-14B by a wide margin on every agentic metric found.

### Cited Findings
- Per-workload ranking table:

  | Workload | 1st | 2nd | 3rd | Evidence |
  |---|---|---|---|---|
  | Agentic tool calling | Qwen3.8-27B NVFP4 (or Qwen3.6-27B) | Qwen3.6-35B-A3B NVFP4/AWQ | Gemma 4 31B 4-bit / gpt-oss-20b | τ², Pass^3, MCP-Atlas above |
  | Text-to-SQL (Postgres) | Qwen3.8-27B | Qwen3.6-35B-A3B | gpt-oss-20b | proxies only: LiveCodeBench 90.3 (Qwen3.8), informal gpt-oss SQL test |
  | Structured extraction / doc review | Qwen3.8-27B | Qwen3.6-35B-A3B (throughput) | Gemma 4 31B | IFBench 79.5 / OmniDocBench 91.1 (vendor); Gemma schema-description caveat |

  — [HF Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B); [BenchLM τ²](https://benchlm.ai/benchmarks/tau2-bench); [vLLM Gemma 4 recipe](https://recipes.vllm.ai/Google/gemma-4-31B-it?variant=w4a16)
- Sources for the launch flags below: [vLLM recipe Qwen3.8-27B](https://recipes.vllm.ai/Qwen/Qwen3.8-27B); [codeberg 5090 run](https://jpmrblood.codeberg.page/2026/08/07-vllm-docker-nvfp4-27b-rtx5090-command-line-breakdown/); [HF Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B); [vLLM GPT-OSS recipe](https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html)
- Community consensus for a 32 GB 5090 in 2026 names the Qwen3.6 35B-A3B and 27B-class models as the top picks. — [llamabuilds](https://www.llamabuilds.ai/gpus/rtx-5090/best-models)

### Inferences
These launch flags are synthesised from the recipes and the measured 5090 run, then fitted to the 28 GB budget. None has been tested on this box.

Start the embedding instance first (e.g. `--gpu-memory-utilization 0.08`). Then cap the chat instance with `--kv-cache-memory-bytes`, which overrides `--gpu-memory-utilization` for KV sizing, so the two instances do not fight over the split.

**Trial A: Qwen3.8-27B NVFP4** (primary)
```
vllm serve Inferact/Qwen3.8-27B-NVFP4 \
  --served-model-name chat \
  --max-model-len 32768 \
  --kv-cache-memory-bytes 6500000000 \
  --max-num-seqs 8 --max-num-batched-tokens 8192 \
  --max-cudagraph-capture-size 8 \
  --enable-prefix-caching \
  --language-model-only \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
# If CUDA-graph capture OOMs, add --enforce-eager (recipe's single-5090 setting).
# Optional speed-up: --speculative-config '{"method":"mtp","num_speculative_tokens":3}'
#   Check the NVFP4 repo keeps the MTP head, and watch the vllm:spec_decode_num_accepted_tokens_total metric.
# Optional: --kv-cache-dtype fp8 to reach 64K+, but BF16 KV is affordable at 32K and safer for exact tool arguments.
# Per-request: use reasoning_effort "low"/"medium" or enable_thinking=false for extraction/SQL.
#   Non-thinking sampling: T=0.7, top_p=0.8, top_k=20, presence_penalty=1.5.
```
- 6.5 GB of KV at 64 KiB/token is ~100K BF16 tokens, about 3 concurrent 32K requests.
- `--language-model-only` comes from the Qwen3.6 card; check that it applies to Qwen3.8.
- Fallback checkpoint if the Inferact build misbehaves: `unsloth/Qwen3.8-27B-NVFP4` or `RedHatAI/Qwen3.8-27B-MXFP4`. Swapping to Qwen3.6-27B is a drop-in with the same flags.

**Trial B: Qwen3.6-35B-A3B 4-bit** (speed)
```
vllm serve <kyaky/Qwen3.6-35B-A3B-NVFP4 | NGC NVFP4 build | an AWQ-int4 build> \
  --served-model-name chat \
  --max-model-len 32768 \
  --kv-cache-memory-bytes 3000000000 \
  --max-num-seqs 8 --max-cudagraph-capture-size 8 \
  --language-model-only \
  --enable-prefix-caching \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
# Try --tool-call-parser qwen3_xml if long agent runs show malformed calls.
# MTP: --speculative-config '{"method":"qwen3_next_mtp","num_speculative_tokens":2}'
```
- 3 GB of KV at 20 KiB/token is ~150K tokens.
- Do not add `--trust-remote-code` on vLLM <0.31 because of the RCE fix in 0.31.
- If NVFP4 MoE falls back to Marlin (watch the startup warning), an AWQ build gives similar speed with more mature kernels.

**Trial C: gpt-oss-20b** (control and fallback)
```
vllm serve openai/gpt-oss-20b \
  --served-model-name chat \
  --max-model-len 65536 \
  --kv-cache-memory-bytes 6000000000 \
  --enable-auto-tool-choice --tool-call-parser openai
# Add the gpt-oss reasoning parser your vLLM build ships (named openai_gptoss in recent builds; check `vllm serve --help`).
# Chat Completions supports only tool_choice="auto" for gpt-oss.
# Watch for phantom tool names containing "<|channel|>" (issue #58825).
```

**Alternative: Gemma 4 31B 4-bit**
```
... --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 \
    --chat-template examples/tool_chat_template_gemma4.jinja
```
- Put schema semantics in the system prompt, since the model does not see JSON-schema descriptions.
- NVFP4 support on SM120 is unconfirmed.

**Upgrade path:** move from vLLM 0.28.0 to ≥0.31 (ideally 0.32/0.33 on a CUDA-13 image with driver R580+). This gets native SM120 NVFP4 W4A4, the `--trust-remote-code` RCE fix and NVFP4 KV support.

**Evaluation plan:** before switching, run the same three in-house test sets on Qwen3-14B-AWQ and each trial:
- a multi-turn tool test that counts "claimed result without a tool call" fabrications;
- Postgres NL→SQL scored by execution match;
- JSON-schema extraction scored by field accuracy.

### Gaps
- None of these flag sets has been run on this exact box or on vLLM 0.28.0. The `--language-model-only` and MTP-on-NVFP4 combinations are untested.
- No measured tok/s for Qwen3.8-27B NVFP4 on a single 5090.
- Licence verification is still needed for Gemma 4, Devstral Small 2 and Nemotron 3 Nano before using them as alternatives.
