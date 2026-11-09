# Hardware upgrade paths for larger open-weight LLMs (RTX 5090 box, as of November 2026)

Context for the report writer: the box today serves `Qwen/Qwen3-14B-AWQ` (AWQ 4-bit) on vLLM v0.28.0 in Docker, 32768 context, `--gpu-memory-utilization 0.70`, and shares the GPU with a `Qwen3-Embedding-0.6B` vLLM container on port 8001. Source: the repo's own `models.toml`, `README.md` and `CHANGELOG.md` (local files, `C:\Users\amro\Documents\Github\syslab-server`). So "the 14B baseline" below means Qwen3-14B (April 2025 generation), and any new configuration must still leave room on the 5090 for the embedder.

Search was US-only. All prices are US retail unless stated. No UAE retailer prices were found (see Gaps). The market in Oct-Nov 2026 is in a severe DRAM/GDDR shortage, which pushes every option here far above MSRP.

## Upgrade path table (synthesis of the questions below)

### Takeaway
The cheapest large quality jump is a software step: moving from Qwen3-14B to a 2026-generation 27B-35B Qwen model that already fits the 5090. After that, a RAM upgrade to 128 GB (two DIMMs, about $1,850-3,400) is the best-value hardware step: it unlocks the 120B-class MoE models at a measured 21-48 tok/s decode. A second 5090 is poor value at today's $6,000-9,400 street price. The RTX PRO 6000 (96 GB, about $10,500-18,000) is the only step that runs 120B-class models fully in VRAM under vLLM with multi-user throughput.

### Cited Findings
- Qwen3.5-27B (dense) scores BFCL-V4 68.5, TAU2-Bench 79.0, IFEval 95.0. Qwen3.5-35B-A3B scores BFCL-V4 67.3, TAU2 81.2. Qwen3.5-122B-A10B scores BFCL-V4 72.2, TAU2 79.5, IFEval 93.4. On BFCL-V4 the older Qwen3-235B-A22B scores only 54.8, and GPT-5-mini 55.5 — [Qwen3.5-122B-A10B model card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)
- Qwen3-14B (the current model) scores BFCL v3 70.4 thinking / 61.5 non-thinking. Qwen3-235B-A22B scores BFCL v3 70.8 / 68.0 on the same version, so it is roughly level with the 14B — [Qwen3 Technical Report](https://arxiv.org/html/2505.09388)
- Qwen3.8-Flash-Next (125B total / 6B active, MoE) runs on an RTX 5090 + 128 GB DDR5 under llama.cpp at about 48 tok/s short decode, about 43 tok/s after 16k input, and about 1,429 tok/s prefill (UD-Q2_K_XL, 78.9 GB) — [zenn.dev, Aug 2026](https://zenn.dev/holy_fox/articles/04887ff8177b87)

### Inferences

| Step | Approx. cost (US, Oct-Nov 2026) | Memory after | Best open-weight models unlocked (newest found) | Expected quality vs Qwen3-14B | Expected throughput (single user) | Serving stack | Verdict |
|---|---|---|---|---|---|---|---|
| 0. Software only | $0 | 32 GB VRAM, 60 GB RAM | Qwen3.5-27B, Qwen3.6-27B (dense, Apr 2026), Qwen3.6-35B-A3B (MoE, Apr 2026), possibly Qwen3.8-27B (unverified) at 4-bit AWQ or FP8 | Largest gain per dollar. Qwen3.5-27B/35B-A3B reach BFCL-V4 67-68 and TAU2 79-81, where Qwen3-235B (≈ 14B on BFCL v3) scores 54.8 / 58.5 | Fully in VRAM; the 5090 sustains prefill in the thousands of tok/s (10K tok/s measured on Qwen in llama-bench) | vLLM as today (`qwen3_coder` tool parser for Qwen3.5) | Do first |
| A1. RAM to 128 GB (2x64 GB, keeps 2-DIMM speed) | $1,852 cheapest tracked kit (Sep 2026); most $2,350-3,700; one list price $3,399 | 32 GB VRAM + 128 GB RAM | GPT-OSS-120B; Qwen3.5-122B-A10B at Q4-Q6; Qwen3.8-Flash-Next (experimental) at Q2-Q4 | Over the 27B step: about +4 BFCL-V4, about 0 TAU2, +8 Terminal Bench 2 (Qwen3.5-122B vs 27B) | Measured: 21-48 tok/s decode. Prefill 150-2,000 tok/s depending on tuning (`-ub 4096`, `--no-mmap`) | llama.cpp / ik_llama.cpp `--n-cpu-moe`, or KTransformers+SGLang; not vLLM | Best-value hardware step, but low concurrency |
| A2. RAM to 192-256 GB (4 DIMMs) | 256 GB kit price not found; probably roughly 2x the 128 GB kit | 32 GB VRAM + 256 GB RAM | DeepSeek-V4-Flash (284B/13B, 156 GB GGUF); Qwen3.5-397B-A17B; MiniMax-M3 (428B/23B) at about Q3; GLM-4.7 (355B/32B); GLM-5.x at 2-bit (about 238 GB) | Reaches the open frontier on agentic coding (V4-Flash: Terminal Bench 2.1 82.7, Toolathlon-Verified 70.3, self-reported). No BFCL or TAU2 numbers found for these | Not measured. Back-of-envelope estimate: about 4-10 tok/s decode, and less if the 4 DIMMs fall to the guaranteed DDR5-3600 | KTransformers+SGLang (Day-0 support for V4-Flash, GLM-5.2, MiniMax-M3) or llama.cpp | Only for batch or offline review jobs; too slow for interactive agent loops |
| B. Second RTX 5090 | $6,033-9,400 US (Oct 2026); EU above €5,000 | 64 GB VRAM | No new model class under vLLM: GPT-OSS-120B and the 122B at 4-bit do not leave room for KV cache in 64 GB. It enables 27B at BF16/FP8 with long context, and higher concurrency | Small (precision: FP8/BF16 instead of AWQ) | TP=2 over PCIe 5.0 x8/x8; P2P vs SHM changes throughput by about +2-5%; gains mostly under 16-64 concurrent requests (vendor claim) | vLLM TP=2 | Poor value at current prices |
| C. RTX PRO 6000 Blackwell 96 GB | $8,565 launch; $10,458 12-month US median; $13,250 (June 2026) and $16,000 on NVIDIA's marketplace; US low $17,987 (Oct 22, 2026) | 96 GB (+32 GB if the 5090 is kept) | GPT-OSS-120B and Qwen3.5-122B-A10B fully in VRAM; with 128-256 GB RAM, DeepSeek-V4-Flash mostly in VRAM | Same model tier as A1/A2, but at full GPU speed and multi-user | Full-VRAM prefill/decode for 120B-class models; batching works | vLLM single-GPU (no TP needed); llama.cpp for >96 GB | Best experience, worst price; resale of the 5090 at inflated used asks (median $8,000) could offset it |
| D. Separate 128 GB box | DGX Spark $4,699 FE (one report $6,950 for 128 GB on NVIDIA's marketplace); Strix Halo $3,449-3,999; Mac Studio M5 Max from $2,499, M5 Ultra from $5,499 (base configs) | 128 GB unified | Same tier as A1 (GPT-OSS-120B, 122B-class) | Same as A1 | gpt-oss-120b: Spark 38.6 tok/s decode, about 1,723 tok/s prefill; Strix Halo 34.1 tok/s, about 340 tok/s prefill | Spark: CUDA (vLLM/llama.cpp); Strix: ROCm/Vulkan llama.cpp; Mac: MLX/llama.cpp | Only if a second server is wanted; Spark is the agent-loop pick because of its prefill speed |

- Prices and throughput in the table are from the cited findings in the sections below. The "expected quality" deltas come from Qwen's own model-card table, which is vendor-reported, not independently verified.

### Gaps
- No single independent benchmark compares Qwen3-14B against the larger 2026 models on the same BFCL version. The 14B-vs-new deltas are inferred via Qwen3-235B as a bridge (see the quality section).

## (a)-(d) Hardware steps and current street prices (USD, Nov 2026)

### Takeaway
Every option is inflated by the 2026 memory shortage. 128 GB of DDR5 costs about $1,850-3,400. A second 5090 costs $6,000-9,400 in the US (about 3-4.7x its $1,999 MSRP). An RTX PRO 6000 costs about $16,000-18,000 at retail in Oct 2026, against $8,565 at launch. No November-dated price was found for any item.

### Cited Findings
**(a) RAM**
- The cheapest new 128 GB (2x64 GB) DDR5-5600 kit was $1,852 ($14.47/GB) as of Sep 22, 2026; most tracked 128 GB kits are $2,350-3,700 — [datacenterdisk.com tracker](https://datacenterdisk.com/ram/ddr5/128gb)
- A DDR5-6400 128 GB kit that bottomed at $329 now lists at $3,399; HWBusters notes that a list price is not a transaction price — [HWBusters](https://hwbusters.com/news/ddr5-memory-prices-are-up-500-in-a-year-and-a-128gb-kit-now-lists-at-3399/)
- An April 2026 outlier: a 128 GB kit at $4,199 — [Notebookcheck](https://www.notebookcheck.net/Store-prices-stockpile-of-128GB-DDR5-RAM-at-4199-raising-doubt-over-memory-shortage.1271226.0.html)
- AMD's guaranteed Zen 5 memory speed is reported as DDR5-5600 with 2 DIMMs and DDR5-3600 with 4 DIMMs; anything faster is an overclock (forum post, not verified against AMD documentation) — [TechPowerUp forum](https://www.techpowerup.com/forums/threads/g-skill-reveals-worlds-first-large-capacity-256-gb-64-gb-x4-ddr5-u-dimm-memory-at-ddr5-6000-cl32-overclocked-speed.335752)
- 4x64 GB = 256 GB DDR5-6000 CL32 EXPO UDIMM kits exist for AM5 (G.Skill, TeamGroup); G.Skill also showed DDR5-6400 on a 9900X3D — [Tom's Hardware](https://www.tomshardware.com/pc-components/ram/g-skill-debuts-worlds-first-256gb-64gbx4-ddr5-6000-cl32-udimm-memory-kit-with-expo-support); a builder reported 256 GB working at high speed on Zen 5 — [AnandTech forum](https://forums.anandtech.com/threads/running-256gb-of-high-speed-ddr5-on-am5-zen-5.2631480/post-41492367)

**(b) Second RTX 5090**
- The lowest available US price reached $9,400 in October 2026, about 370% over MSRP (reported via search snippet; the page returned 403 when fetched) — [TechSpot Q4 2026 GPU pricing](https://techspot.com/article/3192-gpu-pricing-q4-2026)
- Another tracker showed a current low of $7,199 (Oct 22) — [gpuprix](https://gpuprix.com/us/gpus/geforce-rtx-5090?show=listings); deals from $6,033 — [GPUPoet Oct 2026 report](https://gpupoet.com/gpu/market-report/gpu-market-report-october-2026)
- The median used eBay ask rose to about $8,000-8,500 between Oct 13 and 23, 2026 — [gpudojo](https://gpudojo.com/market-notes/2026-10-23-rtx-5090-asks-jump)
- EU street prices exceed €5,000 (about $5,700) — [Guru3D](https://www.guru3d.com/story/geforce-rtx-5090-street-prices-in-eu-now-exceed/)
- Several of these price sites are low-authority or affiliate pages, and their figures disagree (search-result assessment) — sources as above

**(c) RTX PRO 6000 Blackwell 96 GB**
- Launch listing $8,565 at a US retailer — [Tom's Hardware](https://www.tomshardware.com/pc-components/gpus/nvidia-rtx-pro-6000-blackwell-gpu-is-listed-for-usd8-565-at-us-retailer-26-percent-more-expensive-than-the-last-gen-rtx-6000-ada)
- NVIDIA raised pricing to $13,250 by June 2026 — [pulse.bot](https://www.pulse.bot/hardware/news/nvidia-raises-rtx-pro-6000-blackwell-gpu-pricing-to-13250-55-increase-over-msrp-in-a-years-time-1a0c8cf3-8457-4e55-bb2a-c4427ad8efc2/)
- $16,000 on NVIDIA's marketplace, 87% over launch MSRP; the cause is the shortage of its 32 x 3 GB GDDR7 modules — [TweakTown](https://www.tweaktown.com/news/113098/nvidias-rtx-pro-6000-blackwell-gpu-price-almost-doubles-reaching-dollars16000/index.html); [Digital Citizen](https://www.digitalcitizen.life/nvidia-rtx-pro-6000-blackwell-prices-climb-past-10000-as-ai-demand-keeps-rising/)
- US low about $17,987 (Oct 22, 2026); 12-month US median $10,458 — [gpuprix](https://gpuprix.com/us/gpus/rtx-pro-6000-blackwell)
- Cloud rental averages about $3.27/GPU-hour across nine providers (Oct 2026) — [Thunder Compute](https://www.thundercompute.com/blog/nvidia-rtx-pro-6000-pricing)
- Conflict: one undated article lists the NVIDIA store price at $8,900 (out of stock) — search result set above; treat it as stale

**(d) Other boxes**
- DGX Spark Founders Edition $4,699, up from $3,999 in February 2026; a late-October update says NVIDIA sells the 128 GB box at $6,950 on its marketplace and partner 64 GB models from $4,999 (the two reports conflict) — [shattered.io](https://shattered.io/dgx-spark-vs-mac-studio-ryzen-ai-halo-local-ai-2026/); [compute-market](https://www.compute-market.com/blog/dgx-spark-vs-strix-halo-local-ai-2026); [Spheron](https://www.spheron.network/blog/dgx-spark-vs-mac-studio-for-local-llms-which-desk-side-box-w/)
- Strix Halo (Ryzen AI Max+ 395) 128 GB: $3,449 Framework (no SSD), $3,499.99 GMKtec EVO-X2, about $3,999 other vendors — [runaihome](https://runaihome.com/blog/best-local-llm-128gb-unified-memory-2026/); [localaimaster](https://localaimaster.com/blog/dgx-spark-vs-strix-halo-vs-mac-studio)
- Mac Studio M5 Max from $2,499 and M5 Ultra from $5,499 (announced Aug 25, 2026); the M5 Ultra is claimed at 1.2 TB/s, against 273 GB/s (Spark) and 256 GB/s (Strix) — [threatfrontier](https://threatfrontier.com/articles/dgx-spark-vs-mac-studio-m5-ultra-vs-ryzen-ai-max-395-local-llm); [contextstudios](https://www.contextstudios.ai/blog/local-ai-hardware-guide-2026)

### Inferences
- The current "60 GB RAM" is probably 2x32 GB. A 128 GB upgrade means replacing both DIMMs with 2x64 GB, which keeps the 2-DIMM speed (DDR5-5600 guaranteed). 192/256 GB needs 4 DIMMs and risks a drop toward DDR5-3600. That cuts theoretical dual-channel bandwidth from about 89.6 GB/s (5600) or 96 GB/s (6000) to about 57.6 GB/s (3600), calculated as MT/s x 8 bytes x 2 channels. CPU-offloaded MoE decode is bandwidth-bound, so the larger-capacity step also slows the CPU side.
- 2x5090 power: from NVIDIA's spec (575 W per 5090, not re-verified this session), two cards plus a 9950X need a 1600 W+ PSU or power-limiting each card to about 400-450 W. Two open-air cards in one case need careful airflow, or a riser.
- The 5090's resale value is high right now (median used ask about $8,000). Selling it toward a PRO 6000 cuts the PRO 6000's net cost a lot, but asks are not sale prices.
- Region: EU 5090 prices (about €5,000) run lower than US prices, so UAE pricing could differ substantially in either direction. Local Dubai/Abu Dhabi retailer checks are needed.

### Gaps
- No November 2026-dated price for any item; all figures are Aug-Oct 2026 snapshots.
- No 192 GB (4x48) or 256 GB (4x64) DDR5 kit price found for 2026.
- No UAE prices (the search tool is US-only).
- Whether the user's motherboard supports x8/x8 bifurcation for two GPUs is unknown (it depends on the board model, which is not in the repo notes).

## Models unlocked at each step (newest as of Nov 2026)

### Takeaway
The 2026 open-weight landscape is Qwen3.5/3.6/3.8, DeepSeek V4/V4.1-Flash, GLM-4.7/5.x, MiniMax-M3, Kimi K3/K3.1, Gemma 4 and gpt-oss. Most of the true frontier models (GLM-5.x, Kimi K3, DeepSeek V4 Pro) are hundreds of billions to more than a trillion parameters and stay out of reach. At 128 GB the realistic picks are the 120B-class MoE models. At 256 GB they are DeepSeek-V4-Flash and Qwen3.5-397B.

### Cited Findings
**Fits the current 5090 (Step 0)**
- Qwen3.6-35B-A3B (MoE, 35B/3B) was released Apr 16, 2026; Qwen3.6-27B (dense) Apr 22, 2026 — [QwenLM/Qwen3.6 GitHub (via mirror)](https://github.com/QwenLM/Qwen3.6); [SiliconFlow model page](https://www.siliconflow.com/zh/models/qwen3-6-35b-a3b)
- The Qwen3.5 family (Feb 2026) includes 397B-A17B, 122B-A10B, 35B-A3B and 27B. No Qwen3.6 variant at 122B or 397B was found — same Qwen3.6 repo listing (search summary); [Qwen3.5-122B-A10B card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)
- Local-model roundups for Nov 2026 name Qwen3.6-27B, Qwen3.8-27B, Gemma 4 and gpt-oss-20b as the single-GPU picks — [benchlm local LLM (updated Nov 6, 2026)](https://benchlm.ai/best/local-llm); [geotoolbox](https://geotoolbox.ai/blog/best-open-source-llms) (SEO-style sources; treat with caution)

**128 GB RAM tier (A1) / 96 GB VRAM tier (C)**
- Qwen3.5-122B-A10B: 122B total / 10B active, 256 experts (8 routed + 1 shared), hybrid Gated DeltaNet + Gated Attention, 262K native context, Apache-2.0 — [HF card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)
- Qwen3.8-Flash-Next: 125B MoE / 6B active (plus a 51B N-gram embedding and 4B MTP; llama.cpp counts 176.94B), 512 experts, 262K context. Released Aug 26, 2026 as "an experimental model" and an early release of the Qwen4 architecture. It needed an unmerged llama.cpp PR (#27742) at the time, and MTP was not supported — [zenn.dev](https://zenn.dev/holy_fox/articles/04887ff8177b87)
- GPT-OSS-120B is still the reference in Qwen's comparisons: IFEval 88.9, IFBench 69.0, Terminal Bench 2 18.7 — [Qwen3.5-122B card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)

**192-256 GB RAM tier (A2)**
- DeepSeek-V4-Flash: 284B total / 13B active, 1M context, released 2026-04-24 under MIT — [DataLearner](https://www.datalearner.com/en/ai-models/pretrained-models/deepseek-v4-flash). The "0731" revision has a 156.40 GB GGUF; tool use and reasoning levels low/high/max — [LM Studio](https://lmstudio.ai/models/deepseek-v4-flash)
- DeepSeek-V4.1-Flash exists (official HF repo; paper on KV-cache compression) — [HF](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash); [arXiv 2609.19969](https://arxiv.org/pdf/2609.19969)
- MiniMax-M3: 428B total / about 23B active, 1M context; Unsloth shipped local quants and it runs through llama.cpp — [codersera](https://codersera.com/blog/glm-5-2-vs-minimax-m3-coding-2026/); [dev.to](https://dev.to/softwarebuilding/glm-5-vs-minimax-m3-open-models-got-serious-1hfd)
- GLM-4.7: 355B total / 32B active; one source says Q4_K_M needs "about 130GB" (this looks low for 355B at 4-bit; see Inferences) — [Agentic Coding Weekly](https://www.agenticcodingweekly.com/p/acw-6-glm-4-7-minimax-m2-1-and-guide-to-local-coding-models)
- GLM-5: 744B / 40B active, MIT. GLM-5.2 full weights are about 1.5 TB; a 2-bit quant of about 238 GB "fits a 256 GB Mac or a 24 GB GPU with offload" — [Latent Space](https://www.latent.space/p/ainews-zai-glm-5-new-sota-open-weights); [codersera](https://codersera.com/blog/glm-5-2-vs-minimax-m3-coding-2026/)
- No GLM-5 "Air" has been released; users on HF are asking for a 100-150B variant — [HF discussion](https://huggingface.co/zai-org/GLM-5/discussions/3)

**Out of reach**
- Kimi K3.1 Pro leads one open-weight index (75.1), ahead of Kimi K3 (69.5) and Ling 3.1 Flash (65.5), as of Nov 6, 2026; another names DeepSeek-V4-Flash-0731 best for coding — [benchlm open-source](https://benchlm.ai/best/open-source); [geotoolbox](https://geotoolbox.ai/blog/best-open-source-llms)

### Inferences
- RAM needed for the CPU-offload tiers is about the model file size minus about 24-28 GB on the GPU (the embedder and KV cache take the rest of the 32 GB), plus about 8-12 GB for the OS, Docker and the rest of the stack.
  - 60 GB (today): GPT-OSS-120B is borderline possible (a known roughly 60+ GB MXFP4 file, from training knowledge, not re-verified).
  - 128 GB: up to about 140-150 GB files, which covers GPT-OSS-120B, Qwen3.5-122B Q4-Q6 and Qwen3.8-Flash-Next Q4 (111 GB). DeepSeek-V4-Flash at 156 GB is just over and would need a smaller quant.
  - 256 GB: V4-Flash Q4, Qwen3.5-397B-A17B Q4 (estimated 210-230 GB), MiniMax-M3 at Q3, and GLM-5.x at 2-bit (tight).
- A 64 GB (2x5090) vLLM setup cannot hold GPT-OSS-120B or Qwen3.5-122B at 4-bit plus useful KV cache: roughly 61-65 GB of weights alone (estimated from params x 4.25 bits). So step B does not unlock a new model class under vLLM.
- The GLM-4.7 "130 GB at Q4_K_M" claim is inconsistent with 355B x about 4.5 bits (about 200 GB). Treat the 355B GLM as a 256 GB-tier model.

### Gaps
- Exact parameter counts and local availability for Kimi K3/K3.1, GLM-5.3, Ling 3.1 Flash, Gemma 4 sizes and Qwen3.8-27B were not verified. A search snippet (source page not identified) said Kimi K3, GLM-5.3, MiniMax-M3 and DeepSeek-V4.1-Flash appear only as cloud tags on Ollama; this is unverified.
- vLLM support status for Qwen3.8-Flash-Next and DeepSeek-V4-Flash on consumer Blackwell was not checked.
- No newer gpt-oss release (successor to gpt-oss-120b) was found; absence not confirmed.

## Quality evidence vs the Qwen3-14B baseline (BFCL, tau-bench, BIRD/Spider, IFEval)

### Takeaway
Within the Qwen3 generation, model size barely moved tool-calling scores (235B ≈ 14B on BFCL v3). The 2026-generation models moved them a lot: on BFCL-V4 and TAU2 even the 27B/35B-A3B models beat the old 235B by about 13-23 points. The 122B adds only about 4 BFCL-V4 points over the 27B. Text-to-SQL leaderboards show raw BIRD scores clustered at about 56-68% across large open models, with no usable 14B-vs-large comparison.

### Cited Findings
- Qwen3-14B: BFCL v3 70.4 (thinking) / 61.5 (non-thinking); IFEval strict-prompt 85.4 / 84.8; Arena-Hard 91.7 / 86.3. Qwen3-30B-A3B BFCL v3 69.1 / 58.6. Qwen3-235B-A22B BFCL v3 70.8 / 68.0 — [Qwen3 Technical Report](https://arxiv.org/html/2505.09388)
- From Qwen's own Qwen3.5 table, in the order Qwen3-235B-A22B / Qwen3.5-35B-A3B / Qwen3.5-27B / Qwen3.5-122B-A10B / GPT-OSS-120B — [Qwen3.5-122B-A10B card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B):
  - BFCL-V4: 54.8 / 67.3 / 68.5 / 72.2 / —
  - TAU2-Bench: 58.5 / 81.2 / 79.0 / 79.5 / —
  - IFEval: 87.8 / 91.9 / 95.0 / 93.4 / 88.9
  - IFBench: 51.7 / 70.2 / 76.5 / 76.1 / 69.0
  - Terminal Bench 2: — / 40.5 / 41.6 / 49.4 / 18.7
  - SWE-bench Verified: — / 69.2 / 72.4 / 72.0 / 62.0
  - The TAU2 airline domain uses fixes from the Claude Opus 4.5 system card
- Tau-style results for small models elsewhere: Qwen3-8B (thinking) BFCL v3 68.2, tau-bench Retail 45.2, Airline 25.0 — [Klear-AgentForge, arXiv 2511.05951](https://arxiv.org/pdf/2511.05951). Qwen3-14B non-reasoning scored 32.2% on τ²-Bench Telecom (Artificial Analysis, via search snippet) — same search result set
- DeepSeek-V4-Flash-0731 (self-reported, DeepSeek harness): Terminal Bench 2.1 82.7, Toolathlon-Verified 70.3, DeepSWE 54.4, Agents' Last Exam 25.2. GLM-5.2 on the same table: 81.0 / 59.9 / 46.2 / 23.8. No BFCL, TAU2 or IFEval reported — [LM Studio model page](https://lmstudio.ai/models/deepseek-v4-flash)
- DeepSeek-V4.1-Flash: Deep-SWE v1.1 74.2% vs V4-Flash 54.4% — [arXiv 2609.19969](https://arxiv.org/pdf/2609.19969)
- BIRD dev (raw LLM, aggregator): GLM-4.7 63.82%, DeepSeek-R1 61.67%, Kimi-K2-Thinking 60.63%; no independent BIRD scores for DeepSeek V4 Pro or Qwen 3.7 Max — [besthub.dev](https://www.besthub.dev/articles/2026-text2sql-model-showdown-which-one-performs-best-fbb17d2f12ae). Qwen3-Coder-480B-A35B about 66% dev / 68% test; DeepSeek V3 about 56.1% dev — [codingfleet](https://codingfleet.com/blog/best-ai-models-for-sql-database-coding-in-2026-text-to-sql-orms-and-beyond/)
- Spider 2.0-AIFunc (465 Snowflake AI-function tasks): best proprietary 67-70% execution accuracy, best open-source 58.1%; GLM-5 about 52.9%, MiniMax M2.5 about 44.9% (self-reported, garbled page) — [benchmarklist Spider 2.0-AIFunc](https://benchmarklist.com/benchmarks/spider_2_0_aifunc/); Spider 2.0 agent-pipeline entries (e.g. PGV-Agent + GLM-5 about 50.3%) have unclear provenance — [benchmarklist Spider 2.0](https://benchmarklist.com/benchmarks/spider_2_0/)
- A June 2026 paper benchmarking on-prem open models on BIRD covers Qwen2.5-Coder, CodeLlama and Llama-3.x, not Qwen3.5 — [arXiv 2606.29733](https://arxiv.org/abs/2606.29733)

### Inferences
- Bridge estimate: Qwen3-14B ≈ Qwen3-235B on BFCL v3 (70.4 vs 70.8 thinking; 61.5 vs 68.0 non-thinking). The 235B scores 54.8 on BFCL-V4, so the 14B probably scores at or below about 55 on BFCL-V4, and below the 235B's 58.5 on TAU2.
  - Moving to Qwen3.5/3.6-27B or 35B-A3B (no hardware change) should then be worth about +12-14 BFCL-V4 and about +20 TAU2 points.
  - Moving further to the 122B adds about +4 BFCL-V4, about 0 TAU2, +8 Terminal Bench 2 and roughly equal IFEval/IFBench.
  - The data-extraction/review proxies (IFEval 85→93-95, IFBench 52→70-76) follow the same pattern.
  - These are vendor numbers and a bridge estimate, not a measured 14B-on-V4 result.
- GPT-OSS-120B is weaker than Qwen3.5-27B on the instruction-following and agentic rows Qwen reports (IFBench 69.0 vs 76.5; Terminal Bench 18.7 vs 41.6). On this evidence it is not the obvious 128 GB pick for this workload; Qwen3.5-122B-A10B is.
- For text-to-SQL, raw-model BIRD scores span only about 56-68% from DeepSeek V3 to Qwen3-Coder-480B. Agent pipeline design (schema linking, execution feedback) moves Spider 2.0 more than model size. A domain eval on the repo's own SQL tasks would be more decisive than any leaderboard.

### Gaps
- No BFCL-V4/TAU2 results found for DeepSeek-V4-Flash, MiniMax-M3, GLM-5.x, Qwen3.6 or Qwen3.8 models.
- No Qwen3-14B BIRD or Spider 2.0 result, and no BIRD result for any Qwen3.5/3.6 model.
- No independent (non-vendor) BFCL leaderboard snapshot for Nov 2026 was fetched.

## Throughput on comparable hardware (especially CPU offload; prefill for long-schema agent loops)

### Takeaway
On a 5090 + 128 GB DDR5 with llama.cpp `--n-cpu-moe`, 120B-class MoE models decode at about 20-48 tok/s. Prefill is the weak point: it ranges from about 150 to 2,000 tok/s and depends heavily on batch size (`-ub`), `--no-mmap` and the OS. Agent loops that resend long schemas will see seconds-long time-to-first-token (about 11 s at 16k input in the best measured config). No measured numbers exist for the 256 GB-tier models on this class of machine.

### Cited Findings
- Qwen3.8-Flash-Next UD-Q2_K_XL (78.9 GB), RTX 5090 + 128 GB DDR5, Ryzen 7 9700X, Ubuntu 24.04. Flags `-c 65536 -ngl 999 -ncmoe 24 -b 4096 -ub 4096 -t 8 --jinja`. Results: about 48 tok/s short decode, about 43 tok/s after 15.7k input, prefill about 1,429 tok/s, TTFT about 11 s at 16k, about 30.9 GB VRAM used — [zenn.dev, Aug 27, 2026](https://zenn.dev/holy_fox/articles/04887ff8177b87)
- Qwen3.8-Flash-Next UD-Q4_K_XL (about 111 GB), RTX 5090 + 128 GB (4x32 GB), 9950X3D, Windows, llama.cpp build 10731, 64K context with `-ncmoe 36`. Results: decode 21-22 tok/s, holding 21.3 tok/s at 48k input; prefill 152-163 tok/s at 15-48k inputs, but only 14-17 tok/s on short prompts — [note.com, Sep 2, 2026](https://note.com/gb10_tsurumitsu/n/n180cba8a21ae)
- GPT-OSS-120B on a 5090 + DDR5 with `--n-cpu-moe 24`: 23 tok/s generation at about 45k context, 1,600-1,800 tok/s prefill; `--no-mmap` "~doubled" prefill; 1,974.9 tok/s prefill on a 13,989-token prompt (13600K). With 96 GB DDR5-6000 + 32 GB VRAM: 863 tok/s prefill on 18.4k input, about 28 tok/s generation (r/LocalLLaMA, Sep 2025 thread) — [r/LocalLLaMA mirror](https://libreddit.winscloud.net/r/LocalLLaMA/comments/1ndit0a/1631_toksec_on_gpt_oss_120b)
- Hardware-Corner GPT-OSS-120B on a 5090 with `--n-cpu-moe 21`: 448 tok/s prefill at 5,650 tokens, 9.6 tok/s generation at 87 tokens (RAM setup unclear) — [Hardware-Corner](https://www.hardware-corner.net/gpt-oss-offloading-moe-layers/)
- `--n-cpu-moe` has a cliff: in one 5090 sweep, 20 gave 54 tok/s, 16 gave 64.5, 12 gave 69.4, and one step below 12 collapsed to 27.5. A larger `-c` shifts the optimum (search snippet; page not fetched) — [llama.cpp discussion #21112](https://github.com/ggml-org/llama.cpp/discussions/21112); [openclawdc](https://openclawdc.com/blog/llama-cpp-moe-offload-flags-explained/)
- A 5090 fully in VRAM reaches about 10K tok/s prefill on Qwen models in llama-bench — [Hardware-Corner RTX 5090 benchmarks](https://www.hardware-corner.net/rtx-5090-llm-benchmarks/)
- gpt-oss-120b on a DGX Spark: 38.55 tok/s decode, about 1,723 tok/s prefill. On Strix Halo: 34.13 tok/s, about 340 tok/s prefill (about 5x slower prefill). In one Ollama test a Mac Studio did 34 tok/s but dropped to 6 tok/s at long context (single test) — [localaimaster](https://localaimaster.com/blog/dgx-spark-vs-strix-halo-vs-mac-studio); [Spheron](https://www.spheron.network/blog/dgx-spark-vs-mac-studio-for-local-llms-which-desk-side-box-w/); [codersera June 2026](https://codersera.com/blog/local-llm-hardware-showdown-june-2026/)
- Dual 5090 TP=2 in vLLM: NCCL P2P vs SHM changed throughput by about +2-5%, sometimes negative (Aug 2026 thread) — [vLLM forum](https://discuss.vllm.ai/t/dual-rtx-5090-tp-2-shm-vs-patched-bar1-p2p-cumem-single-pass-2-7-mean-point-estimate/2870/2). PCIe 5.0 x8 is about 31.5 GB/s per direction, about 1/57 of the 5090's 1,792 GB/s VRAM bandwidth; consumer boards split two cards x8/x8 — [runaihome via dev.to](https://dev.to/jovan_chan_9500711396d4e6/multi-gpu-for-local-ai-in-2026-nvlink-vs-pcie-and-when-a-second-card-actually-helps-1egf). Whether P2P works on RTX 50-series was unresolved on NVIDIA's forum — [NVIDIA dev forum](https://forums.developer.nvidia.com/t/p2p-issue-using-two-rtx-5090-gpus/326776). A hosted-GPU vendor claims dual 5090 wins at 16-64 concurrent requests (vendor claim) — [gigagpu](https://gigagpu.com/?p=14030)
- An SGLang "paged experts" write-up on running an MoE larger than VRAM reports a "4-request cliff" (title only; not fetched) — [dev.to](https://dev.to/wolfsea2357/running-an-moe-model-larger-than-your-vram-sglang-paged-experts-the-pcie-math-and-the-4-request-1g9o)

### Inferences
- The 10x prefill gap between the two Qwen3.8-Flash-Next reports (about 1,429 vs about 150 tok/s) most likely comes from `-b/-ub 4096` (zenn) versus defaults (note.com), plus Windows and 4 DIMMs. Large ubatches let llama.cpp stream expert weights to the GPU for prefill, so PCIe bandwidth, not CPU, sets prefill speed. For agent loops, tune `-ub` and `--no-mmap` first.
- Decode estimates for the 256 GB tier, assuming CPU-resident expert bytes per token divided by effective RAM bandwidth of about 60-75 GB/s:
  - DeepSeek-V4-Flash (13B active, 156 GB file) ≈ 5-6 GB/token from RAM → ceiling about 11-14 tok/s, realistically about 6-10.
  - Qwen3.5-397B-A17B → realistically about 4-6.
  - MiniMax-M3 (23B active) → realistically about 3-5.
  - 4 DIMMs at DDR5-3600 would cut these by about a third.
  - Cross-check: the same method gives about 20-25 tok/s for Qwen3.8-Flash-Next Q4, which matches the measured 21-22.
  - These are unmeasured estimates.
- CPU offload in llama.cpp is effectively single-stream. It suits one operator plus batch jobs, not several concurrent agent sessions, whereas vLLM on an all-VRAM step (C) batches well.

### Gaps
- No measured 5090 + DDR5 numbers for Qwen3.5-122B-A10B, DeepSeek-V4-Flash, Qwen3.5-397B, MiniMax-M3 or GLM-4.7/5.x, and no KTransformers benchmark on Zen 5.
- No measured dual-5090 vLLM tokens/s for a specific model on AM5 x8/x8.
- No measured RTX PRO 6000 numbers for GPT-OSS-120B or Qwen3.5-122B were fetched.

## Practical caveats (vLLM vs llama.cpp offload, tool calling under llama.cpp, power/cooling)

### Takeaway
The CPU-offload steps (A1/A2) mean leaving vLLM for llama.cpp/ik_llama.cpp or KTransformers+SGLang. vLLM's CPU offload is not MoE-aware. KTransformers has Day-0 support for the 2026 big MoE models, but its fast kernels target AVX-512/AMX. Tool-calling robustness under llama.cpp could not be verified from sources. The all-VRAM steps (B, C) keep the current vLLM workflow and its `qwen3_coder` tool parser.

### Cited Findings
- vLLM can offload weights to CPU, but it is not built around MoE sparsity. KTransformers places hot experts on the GPU and cold experts on the CPU, integrates with SGLang (`pip install "ktransformers[sglang]"`, `--kt-num-gpu-experts`), and has CPU kernels targeting Intel AMX/AVX-512 on INT4/INT8. llama.cpp achieves similar placement with `--override-tensor`/`--n-cpu-moe` — [DataCamp KTransformers tutorial](https://www.datacamp.com/nl/tutorial/ktransformers-tutorial); [KTransformers GitHub](https://github.com/kvcache-ai/ktransformers)
- KTransformers dates: AVX2-only CPU backend (Mar 2026), DeepSeek-V4-Flash support (May 2026), GLM-5.2 Day-0 (Jun 17, 2026), MiniMax-M3 Day-0 (Jun 21, 2026) — [KTransformers GitHub](https://github.com/kvcache-ai/ktransformers); [noze.it](https://www.noze.it/en/insights/ktransformers-hybrid-cpu-gpu-inference/)
- Qwen3.5 serving: vLLM nightly with `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`, optional MTP speculative decoding; SGLang equivalent `--tool-call-parser qwen3_coder`. The card recommends at least 128K context to preserve thinking — [Qwen3.5-122B card](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)
- llama.cpp tool templates require `--jinja` (used in the zenn config). New architectures can need unmerged PRs (Qwen3.8-Flash-Next needed PR #27742; MTP unsupported) — [zenn.dev](https://zenn.dev/holy_fox/articles/04887ff8177b87)
- Four 64 GB DIMMs on Zen 5 need "exhaustive tweaking" for DDR5-6000 CL30, and the IMC may not keep up even when the board vendor validates it — [TechPowerUp forum](https://www.techpowerup.com/forums/threads/g-skill-reveals-worlds-first-large-capacity-256-gb-64-gb-x4-ddr5-u-dimm-memory-at-ddr5-6000-cl32-overclocked-speed.335752); [Tom's Hardware](https://www.tomshardware.com/pc-components/ram/g-skill-debuts-worlds-first-256gb-64gbx4-ddr5-6000-cl32-udimm-memory-kit-with-expo-support)
- On a 5090 + offload setup (Windows), going one `-ncmoe` step too low stalled the GPU at 0%, and Q8 KV cache caused CUDA OOM at 32K — [note.com](https://note.com/gb10_tsurumitsu/n/n180cba8a21ae)

### Inferences
- Ryzen 9950X (Zen 5) has AVX-512 but no AMX (from training knowledge, not re-verified). KTransformers' AVX-512 path should apply, but its headline numbers come from Xeon AMX systems and will not transfer directly.
- Switching from vLLM to llama-server changes the tool-call parsing path from vLLM's `qwen3_coder` parser to llama.cpp's Jinja template plus its own parser. The repo's `scripts/check_agent.py` gate (referenced in CHANGELOG.md) should be re-run on any new engine before trusting tool-calling quality.
- The embedder container on the same GPU (today 0.30 of VRAM is left outside the LLM) reduces how many expert layers can stay on the GPU under offload. Moving the 0.6B embedder to CPU, or onto a second device, is worth considering for steps A1/A2.
- Power and cooling: step A adds almost no power. Step B adds about 575 W of GPU (NVIDIA spec, not re-verified). Step C's 600 W Workstation card (a 300 W Max-Q variant exists, from training knowledge) combined with the kept 5090 also needs a 1600 W-class PSU. UAE ambient temperatures make case airflow and room cooling a real constraint for 1 kW+ sustained loads.

### Gaps
- No source found on tool-calling accuracy under llama.cpp vs vLLM for the same model (e.g. BFCL run through llama-server).
- vLLM's own MoE expert-offload roadmap in late 2026 was not checked.
- ik_llama.cpp-specific performance on Zen 5 was not found.
