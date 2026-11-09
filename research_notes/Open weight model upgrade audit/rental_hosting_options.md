# Rental and hosting options for open-weight LLMs (alternative or complement to the self-hosted RTX 5090 box), November 2026

Research date: 2026-11-07. Prices are USD unless marked EUR. "24/7" = 730 h/month. "Business hours" = 10 h/day x 22 weekdays = 220 h/month. Every monthly figure in these notes is my arithmetic on the hourly price shown, not a figure from the provider. Most pricing pages show no effective date. Where a page does show one, it is noted. Lambda's new rates take effect 2026-11-16.

Model-landscape caveat: as of November 2026, Together and Fireworks serverless menus have moved on to newer generations: GLM-5.2/5.3, DeepSeek V4.x, Kimi K3, Qwen 3.8. The classes named in the brief (Qwen3-235B, GLM-4.x, DeepSeek V3.x, Kimi K2) are still served by DeepInfra and several OpenRouter providers, but no longer by those two. gpt-oss-120b is still on every major platform.

## 1. GPU rental: hourly prices and monthly cost (24/7 vs business hours)

### Takeaway
A single 80-96 GB GPU, enough for gpt-oss-120b, rents for about $1.7-4/h on specialist clouds. That is roughly $1,200-2,900/month 24/7, or $370-880/month for business hours only. Hyperscalers cost 2-3x more, and 3x+ in the UAE regions. RTX 5090s are cheap ($0.20-1.19/h) but have the same 32 GB as the user's own box, so renting one adds capacity, not a bigger model.

### Cited Findings
**RunPod pods** (page says "Updated November 4, 2026"), Secure Cloud / Community Cloud $/h — [RunPod pricing](https://www.runpod.io/pricing):
- RTX 5090: $1.19 / $0.69. 24/7 is about $869 / $504 per month. Business hours is about $262 / $152.
- RTX PRO 6000 (96 GB): $2.49 / $1.69. 24/7 is about $1,818 / $1,234. Business hours is about $548 / $372.
- L40S: $1.09 / $0.79. 24/7 is about $796 / $577.
- A100 80GB PCIe: $1.79 / $1.19. A100 80GB SXM: $1.79 / $1.39. 24/7 Secure is about $1,307.
- H100 PCIe: $2.89 / $1.99. H100 SXM: $3.99 / $2.69. H100 NVL: $3.19 / $2.59. H100 SXM 24/7 is about $2,913 / $1,964. Business hours is about $878 / $592.
- H200: $5.29 / $3.59. 24/7 is about $3,862 / $2,621.
- B200: $7.99 / $5.98. 24/7 is about $5,833 / $4,365.
- The Community Cloud prices came from the page's structured data. The visible table shows Secure Cloud.

**RunPod Serverless** (billed per second while workers run; the page lists hourly equivalents only) — [RunPod pricing](https://www.runpod.io/pricing):
- RTX 5090 $1.79/h, RTX PRO 6000 $3.49/h, L40S/6000 Ada $1.75/h, A100 80GB $2.72/h, H100 $5.39/h, H200 $6.99/h, B200 $10.79/h, B300 $11.99/h.
- The page did not separate "flex" and "active" worker rates.

**RunPod storage** — [RunPod pricing](https://www.runpod.io/pricing):
- Network volume: $0.07/GB/month under 1 TB, $0.05/GB/month over 1 TB. High-performance network volume: $0.14/GB/month.
- Pod volume disk: $0.10/GB/month while running, $0.20/GB/month while idle.

**Lambda** on-demand, per GPU-hour, rates "take effect November 16, 2026, at 00:00 UTC". The page has four tables, apparently by GPU count, but does not label them — [Lambda pricing](https://lambda.com/pricing):
- B200: $7.69-$7.99. H100 SXM: $4.59-$4.89. H100 PCIe: $3.79. A100 80GB (HGX): $3.19. A100 40GB: $1.99. GH200 96GB: $2.59. A6000: $1.09.
- No H200 and no RTX PRO 6000 on-demand. Reserved pricing requires contacting sales.
- H100 SXM 24/7 is about $3,350-3,570/month.

**CoreWeave**, North America on-demand. The listed instances are 8-GPU only (GB200 is 4 GPUs) — [CoreWeave pricing](https://www.coreweave.com/pricing):
- 8x B200 $68.80/h ($8.60/GPU). 8x H200 $50.44/h ($6.31/GPU). 8x H100 $49.24/h ($6.16/GPU). 8x A100 $21.60/h. 8x L40S $18.00/h ($2.25/GPU). 8x RTX PRO 6000 High-Memory $20.00/h ($2.50/GPU). GB200 NVL72 $42/h per 4-GPU instance.
- Reserved capacity: "up to 60% off".
- The smallest instance (8x L40S) is about $13,140/month 24/7.

**Vast.ai** (marketplace, prices float):
- The official pricing page showed no numbers in the fetch. It lists On-Demand, Interruptible ("50%+ cheaper") and Reserved ("Up to 50% Off") options — [Vast.ai pricing](https://vast.ai/pricing).
- Third-party trackers (July-August 2026, seen in search snippets, not fetched): RTX 5090 about $0.20-$0.41/h. H100 about $1.05-$2.75/h. One Czech H100 SXM listing at $2.34/GPU-h — [computeprices.com](https://computeprices.com/providers/vast/gpus/rtx5090); [getdeploying.com](https://getdeploying.com/reference/cloud-gpu/nvidia-rtx-5090); [deploybase.ai](https://deploybase.ai/articles/rtx-5090-on-vastai-pricing-specs-how-to-rent).

**Hetzner dedicated GPU servers** (EUR, excl. VAT, monthly, plus a one-time setup fee equal to one month; locations Falkenstein/Helsinki, Germany/Finland) — [Hetzner GPU matrix](https://www.hetzner.com/dedicated-rootserver/matrix-gpu/):
- GEX63: 1x RTX PRO 6000 Blackwell Max-Q 96 GB, Core Ultra 7 265, 128 GB RAM. €999/month + €999 setup, or €1.6009/h. HEL1.
- GEX131: 1x RTX PRO 6000 Max-Q, Xeon Gold 5412U, 256 GB+ ECC. From €1,499/month + €1,499 setup, or €2.4022/h. FSN1, HEL1.
- GEX430: 4x RTX PRO 6000 Max-Q (384 GB total). €3,599/month + €3,599 setup, or €5.7676/h. HEL1.
- GEX45: RTX PRO 4000 Blackwell SFF 24 GB. €214/month.
- GEX44 and GEX130 are no longer listed.

**Modal** (serverless containers, per-second billing; my hourly conversions) — [Modal pricing](https://modal.com/pricing):
- Prices: B200 $6.25/h, H200 $4.54/h, H100 $3.95/h, RTX PRO 6000 $3.03/h, A100 80GB $2.50/h, A100 40GB $2.10/h, L40S $1.95/h, L4 $0.80/h.
- Multipliers: region selection 1.15-1.75x. Non-preemptible execution 3x.
- Plans: Starter $0 with $30/month credits. Team $250/month with $100 credits.
- Volumes: $0.09/GiB/month, 1 TiB free.

**Hugging Face Inference Endpoints** (billed per minute, no date on the page) — [HF Inference Endpoints pricing](https://huggingface.co/docs/inference-endpoints/pricing):
- AWS: L4 $0.80/h, A10G $1.00/h, L40S $1.80/h, A100 80GB $2.50/h, H200 $5.00/h (x8 $40/h).
- GCP: L4 $0.70/h, A100 $3.60/h, H100 $10.00/h.
- No B200. No Azure GPUs.

**AWS** (third-party aggregators; I did not fetch the AWS page):
- p5.4xlarge (1x H100) is $6.88/h on-demand in us-east-1, about $5,022/month 24/7. Spot was as low as $1.34/h — [Holori](https://calculator.holori.com/aws/ec2/p5.4xlarge/us-east-1); [DoiT](https://compute.doit.com/spot/us-east-1/p5.4xlarge).
- EC2 Capacity Blocks for P5 rose to about $5.19/GPU-h in US regions after increases in January and July 2026 — [eCorpIT](https://ecorpit.com/aws-ec2-capacity-blocks-gpu-price-rise-finops-2026/).

**Rental market average:**
- RTX PRO 6000 rental averaged about $3.27/GPU-h across nine providers as of 2026-10-16 — [Thunder Compute](https://www.thundercompute.com/blog/nvidia-rtx-pro-6000-pricing).

**Spheron** (vendor blog, prices dated 2026-11-07):
- H100 SXM $3.96/h on-demand, $2.20/h spot. H200 $5.52/h. B200 $10.20/h — [Spheron](https://www.spheron.network/blog/gpu-cloud-middle-east-2026/).
- The same page cites $5.07/h for H100 in a July comparison, which is internally inconsistent.

### Inferences
**What fits on which hardware.** Approximate checkpoint sizes from general model knowledge, not fetched this session:
- 30B-class models (Qwen3-30B-A3B, gpt-oss-20b) run on the existing 5090.
- gpt-oss-120b (MXFP4 checkpoint, about 60-65 GB) needs one 80 GB H100/A100 or one 96 GB RTX PRO 6000. It does not fit on a 5090.
- Qwen3-235B-A22B needs about 2x RTX PRO 6000 or 2x H200 at 4-bit, and 4x H100 or 2x H200 at FP8.
- GLM-4.6/4.7 (355B total) needs about 4x H200 or 8x H100 at FP8.
- DeepSeek V3.x (671B) needs a full 8x H200 or 8x B200 node.
- At the rental rates above, the 235B+ classes cost roughly $5k-$40k/month to run 24/7 on dedicated hardware.

**Picks for one 96 GB card (gpt-oss-120b):**
- Cheapest flexible: RunPod Community PRO 6000 ($1.69/h).
- Cheapest flat-rate: Hetzner GEX63 (€999/month), but it is in Germany/Finland.
- Most reputable on-demand single H100: RunPod Secure ($2.89-3.99/h) or Lambda ($3.79-4.89/h from 2026-11-16).

**Business-hours-only operation** cuts cost by about 70% (220 vs 730 h). This only works where pods can be stopped and started, or on scale-to-zero serverless. Hetzner's monthly contract gets no such saving.

**RunPod Community Cloud and Vast.ai** run on third-party host machines. For client company data, prefer RunPod Secure Cloud, Lambda, Hetzner or a hyperscaler.

### Gaps
- Not found or not fetched: TensorDock (404), Hyperbolic (404), Scaleway (403), OVHcloud (page truncated, no prices), GCP and Azure US on-demand H100 list prices.
- The Vast.ai official live prices did not render. The figures above are from third-party trackers dated July-August 2026.
- RunPod "flex vs active" serverless discount rates were not on the fetched page.
- Lambda's four tables are unlabeled, so it is unclear which price applies to a 1x vs 8x instance.
- The EUR/USD rate for converting Hetzner prices was not verified.

## 2. Dedicated and managed open-model endpoints: per-token prices, tool calling, structured output

### Takeaway
Per-token serverless APIs are far cheaper than any dedicated GPU at small-company volumes. gpt-oss-120b costs $0.15/$0.60 per M tokens on Together, Fireworks and Groq, and as little as $0.017/$0.062 on OpenRouter's cheapest FP4 provider. Qwen3-235B-2507 costs about $0.09/$0.55. Nearly every OpenRouter endpoint for these models advertises `tools`, `tool_choice` and `response_format` support. A few do not, so filter with `require_parameters: true`.

### Cited Findings
**Together AI:**
- Serverless gpt-oss-120b: $0.15 in / $0.60 out per M. GLM-5.2 and GLM-5.3: $1.40/$4.40. GLM-5.3-Flash: $0.15/$0.50. Kimi K3: $3.00/$15.00 — [Together pricing](https://www.together.ai/pricing).
- Not on the serverless menu: gpt-oss-20b, Qwen3-235B, Qwen3-30B/32B, GLM-4.x, DeepSeek V3.x, Kimi K2 — [Together pricing](https://www.together.ai/pricing).
- Dedicated endpoints: H100 $5.49/GPU-h, B200 $8.99/GPU-h, H200 contact sales — [Together pricing](https://www.together.ai/pricing).
- Dedicated endpoints bill per GPU-minute while running, whatever the request volume. They use the same inference API as serverless — [Together dedicated inference docs](https://docs.together.ai/docs/dedicated-inference).

**Fireworks:**
- gpt-oss-120b: $0.15 in / $0.015 cached / $0.60 out. GLM-5.3: $1.40/$4.40. GLM-5.3-Flash: $0.15/$0.50. DeepSeek V4.1 Flash: $0.30/$1.20. Qwen 3.8 Max: $2.00/$6.00. Kimi K3: $3.00/$15.00. A spot tier is about 30% off for some models. Not listed: gpt-oss-20b, Qwen3-235B, GLM-4.x, DeepSeek V3.x — [Fireworks pricing](https://fireworks.ai/pricing).
- On-demand deployments: H100 $8.00/h, H200 $8.00/h, B200 $13.00/h. Idle deployments scale to zero after 1 h by default, and start-up time is not billed. Region-restricted deployments cost 1.5x — [Fireworks pricing](https://fireworks.ai/pricing).

**DeepInfra** (no date on page):
- Qwen3-235B-A22B-Instruct-2507 $0.09/$0.55. Qwen3-30B-A3B $0.12/$0.50. Qwen3-32B $0.08/$0.28. Qwen3-Coder-480B Turbo $0.30/$1.00.
- GLM-4.6 $0.50/$2.00. GLM-4.7 $0.40/$1.75. GLM-5.3 $0.5625/$2.50 (discounted).
- DeepSeek-V3.1 $0.25/$0.95. DeepSeek-V3.2 $0.26/$0.38. DeepSeek-V4-Flash $0.09/$0.18.
- Kimi-K2.6 $0.75/$3.50.
- Dedicated GPUs (per minute): A100 80GB $1.49/h, H100 $2.90/h, H200 $3.49/h, B200 $4.89/h — the cheapest dedicated GPU rates found. All from [DeepInfra pricing](https://deepinfra.com/pricing).

**Groq** — [Groq models](https://console.groq.com/docs/models):
- gpt-oss-120b: $0.15/$0.60, 500 tok/s, 131k context.
- gpt-oss-20b: $0.075/$0.30, 1,000 tok/s.
- qwen/qwen3.8-27b (preview): $0.80/$4.00, 450 tok/s.
- Llama 3.x and MiniMax M2.7 are "contact sales".

**Cerebras:**
- The pricing page did not render prices — [Cerebras pricing](https://www.cerebras.ai/pricing).
- Via OpenRouter, Cerebras serves gpt-oss-120b at fp16 for $0.35/$0.75, with tools and structured outputs — [OpenRouter gpt-oss-120b endpoints](https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints).

**OpenRouter, gpt-oss-120b** (live endpoints API) — [OpenRouter endpoints](https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints):

| Provider (quantization) | $/M in | $/M out | Tools | Structured outputs |
|---|---|---|---|---|
| PrimeIntellect (fp4) | 0.017 | 0.062 | yes | yes |
| CoreWeave (fp4) | 0.03 | 0.17 | yes | yes |
| DeepInfra (bf16) | 0.037 | 0.17 | yes | yes |
| Crusoe (bf16) | 0.05 | 0.25 | yes | yes |
| Baseten (fp4) | 0.10 | 0.50 | yes | yes |
| Nebius (fp4) | 0.15 | 0.60 | listed | listed, but tool_choice flags all false |
| Together | 0.15 | 0.60 | yes | yes |
| Groq | 0.15 | 0.60 | yes | yes |
| Cerebras (fp16) | 0.35 | 0.75 | yes | yes |
| Amazon Bedrock | 0.15 | 0.60 | no | no |
| Google Vertex | 0.09 | 0.36 | no | yes (status down at fetch) |
| DigitalOcean | 0.06 | 0.42 | no | no |
| SiliconFlow | 0.15 | 0.60 | no | yes |

**OpenRouter, Qwen3-235B-A22B-2507**, all with tools and structured outputs — [OpenRouter endpoints](https://openrouter.ai/api/v1/models/qwen/qwen3-235b-a22b-2507/endpoints):
- GMICloud fp8 $0.0875/$0.35, 262k context.
- DeepInfra fp8 $0.09/$0.55.
- Parasail fp8 $0.14/$0.80.
- Venice fp8 $0.15/$0.75.

**OpenRouter, GLM-4.7** — [OpenRouter endpoints](https://openrouter.ai/api/v1/models/z-ai/glm-4.7/endpoints):
- DeepInfra fp4 $0.40/$1.75 (flex $0.32/$1.40), with tools and structured outputs.
- Z.AI fp4 $0.60/$2.20 and Novita fp8 $0.60/$2.20, with tools but no `structured_outputs`.
- Mancer: no tools.

**OpenRouter provider routing** — [OpenRouter provider routing](https://openrouter.ai/docs/features/provider-routing):
- Requests take a `provider` object with `order`, `only`, `ignore`, `allow_fallbacks`, `require_parameters`, `data_collection: "deny"`, `zdr: true`, `sort`, `quantizations` and `max_price`.
- `require_parameters: true` routes only to providers that support every parameter in the request, such as `tools` and `response_format`.
- Shortcuts: `:nitro` sorts by throughput, `:floor` by price.

**Modal** — [Modal pricing](https://modal.com/pricing):
- Token billing for Shared Endpoints took effect on 2026-09-01 for Starter users. Per-token rates were not shown.

**Baseten:**
- The pricing page did not render — [Baseten pricing](https://www.baseten.co/pricing/).
- Via OpenRouter, Baseten serves gpt-oss-120b fp4 at $0.10/$0.50 with tools and structured outputs, including a "us" endpoint — [OpenRouter endpoints](https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints).

### Inferences
**Token costs at small-company volume.** At 3:1 input:output, gpt-oss-120b at $0.15/$0.60 blends to about $0.26/M tokens.
- 100M tokens/month costs about $26.
- 1B tokens/month costs about $263.
- Breaking even with a 24/7 RunPod Secure H100 ($2,913/month) would take about 11B tokens/month.
- So for gpt-oss-120b, Qwen3-235B and GLM-4.x class models, per-token APIs win on cost by one to two orders of magnitude. The only reasons to rent dedicated GPUs are residency or single-tenancy requirements, custom or fine-tuned weights, or guaranteed latency.

**Quantization.** The cheapest OpenRouter endpoints are FP4/INT4 quantized. For tool-calling reliability, pin bf16 or fp8 providers with `quantizations`, or pin named providers with `only`. Use `require_parameters: true` to exclude the endpoints without tool support (Bedrock, DigitalOcean, Vertex).

**DeepInfra's dedicated rates** ($2.90/h H100, $3.49/h H200) undercut RunPod Secure pods. DeepInfra also handles serving, so it is a strong managed single-tenant option if its privacy terms are acceptable (see section 3).

### Gaps
- Cerebras and Baseten direct rate cards did not render.
- Groq's model page does not state which models support tool use or structured outputs.
- Hugging Face Inference Providers per-token routing was not researched.
- Together dedicated H200 and L40S/A100 prices are "contact sales".
- Fireworks prices for gpt-oss-20b and 30B-class models are not listed.

## 3. Data privacy and residency (ZDR, no-training, SOC 2, UAE / Middle East)

### Takeaway
Fireworks is zero-retention by default. DeepInfra keeps request data in memory only and does not train on it. Together needs ZDR switched on in org settings. OpenRouter can enforce ZDR per request or account-wide. None of the specialist providers found here runs GPUs in the UAE.

In the UAE itself, the options are:
- AWS me-central-1: L40S (G6e) confirmed. H100 capacity is unconfirmed or constrained.
- Azure UAE North: an H100 v5 SKU is listed by aggregators, with no capacity confirmation.
- Core42: sovereign cloud, MI355X from $3.50/h, and a Compass API serving gpt-oss.
- In-region hyperscaler H100 runs about $12-14/GPU-h, roughly 3x specialist-cloud rates.

### Cited Findings
**Fireworks** — [Fireworks data handling](https://docs.fireworks.ai/guides/security_compliance/data_handling):
- Zero data retention is the default for open models. Prompts and generations live only in volatile memory, and only metadata such as token counts is logged.
- Enterprise admins can enforce a ZDR policy that rejects requests that would persist data.
- Exception: the Responses API with `store=True` (the default) keeps conversations up to 30 days. Set `store=False`.
- The pricing page repeats "Zero data retention by default" — [Fireworks pricing](https://fireworks.ai/pricing).

**Together AI:**
- ZDR is off by default. Without it, Together stores prompts and responses and may use them for product improvement. Enable ZDR under Organization Settings, Privacy, by turning prompt storage off. It applies only to requests made after it is enabled — [Together ZDR docs](https://docs.together.ai/docs/zero-data-retention).
- Training on customer data is a separate opt-in, off by default — [Together ZDR docs](https://docs.together.ai/docs/zero-data-retention).
- The ZDR page does not say whether ZDR covers dedicated endpoints — [Together ZDR docs](https://docs.together.ai/docs/zero-data-retention).
- Together reports completing a SOC 2 Type 2 examination and says it signs HIPAA BAAs — [Together SOC 2 blog](https://www.together.ai/blog/soc-2-compliance). A third-party guide says a BAA requires an enterprise contract (see the search results summarized under that blog).

**DeepInfra** — [DeepInfra data privacy](https://docs.deepinfra.com/account/data-privacy):
- Inputs are held only in memory, not written to disk, and deleted after inference. Only debugging metadata is logged.
- No training on submitted data, except for Google and Anthropic models.
- Bulk inference data may be stored encrypted on disk for an unspecified "short retention period".
- No SOC 2 statement or data-center location on that page.

**OpenRouter** — [OpenRouter provider routing](https://openrouter.ai/docs/features/provider-routing):
- `provider.zdr: true` restricts a request to Zero Data Retention endpoints.
- `data_collection: "deny"` excludes providers that collect data.
- An account-wide ZDR setting at openrouter.ai/settings/privacy is OR-combined with per-request settings, and a request cannot switch it off.
- Account-level allowed providers act as a ceiling.
- "Safety Retention" endpoints are also eligible under `zdr: true` unless the account is set to ZDR-only.

**Modal** — [Modal pricing](https://modal.com/pricing):
- SOC 2 on all plans. HIPAA on Enterprise only.

**AWS me-central-1 (UAE):**
- G6e instances (NVIDIA L40S) became available in the UAE region on 2026-02-05, with On-Demand, Reserved and Spot options — [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-ec2-g6e-instances-uae-region).
- No AWS page was found confirming P5 (H100) in me-central-1.
- A vendor blog says P5 is "available, constrained capacity" in one place and "Not yet" in its table — [Spheron Middle East GPU guide](https://www.spheron.network/blog/gpu-cloud-middle-east-2026/).
- An undated status page reports a power-related me-central-1 regional outage that blocked new EC2 launches — [status.cast.ai](https://status.cast.ai/). The date and whether it is current are unknown.

**Azure UAE North:**
- Aggregators list the ND96isr H100 v5 SKU in uaenorth. Sparecores last observed it 2026-07-12 — [Northflank](https://northflank.com/cloud/azure/instances/Standard_ND96isr_H100_v5); [Sparecores](https://sparecores.com/server/azure/Standard_ND96isr_H100_v5).
- Spheron instead says "ND H100 v4" with "Limited" availability — [Spheron](https://www.spheron.network/blog/gpu-cloud-middle-east-2026/). These conflict, and "ND H100 v4" does not match Microsoft's naming.

**In-region H100 prices** (Spheron, dated 2026-07-01, approximate):
- AWS me-central-1 about $12.30/GPU-h, GCP "me-central1" about $12.49, Azure UAE North about $14.00.
- None of the Gulf hyperscaler regions offered H200 or B200 as of mid-2026.
- All from [Spheron](https://www.spheron.network/blog/gpu-cloud-middle-east-2026/).

**Core42 (G42):**
- The Core42 AI Cloud page lists an AMD MI355X platform "FROM $3.50/HR". It offers pay-as-you-go on-demand instances and short- and long-term commitments — [Core42 AI Cloud](https://www.core42.ai/ai-cloud).
- The self-service platform launched at GITEX in October 2025 — [Gulf News](https://gulfnews.com/uae/gitex-2025-core42-launches-self-service-ai-cloud-platform-1.500306352).
- Since 2025-08-13, Core42 has offered gpt-oss-120b and gpt-oss-20b through its Compass API. Hardware choices are AMD, Cerebras (up to 3,000 tok/s), NVIDIA or Azure. UAE-hosted deployments run under UAE jurisdiction. The article gives no pricing and does not say whether the API is OpenAI-compatible — [Middle East AI News](https://www.middleeastainews.com/p/core42-launches-gpt-oss-access-with).
- e& UAE and Core42 launched a "Sovereign AI Compute" platform in July 2026 — [Middle East AI News](https://www.middleeastainews.com/p/e-and-uae-core42-sovereign-ai-compute).

**UAE PDPL** (Federal Decree-Law 45 of 2021, as summarized by a vendor blog that says it is not legal advice):
- Cross-border personal-data transfers need adequate protection, via contract or an adequacy decision.
- Sector regulators add stricter in-country rules: CBUAE for financial data, DOH for health data.
- Source: [Spheron](https://www.spheron.network/blog/gpu-cloud-middle-east-2026/).

### Inferences
**Recommended privacy setup for client data on a per-token API:**
- OpenRouter with account-wide ZDR plus `only` pinned to providers you have checked, and `allow_fallbacks: false`.
- Or Fireworks directly (ZDR by default; avoid `store=True` on the Responses API).
- Or Together with ZDR enabled before the first request.
- Any of these still sends data out of the UAE. Clients under CBUAE or DOH rules, or with UAE-residency contract clauses, need an in-country option instead: Core42, AWS me-central-1 G6e, or Azure UAE North. Alternatively, keep those workloads on the local 5090 box.

**Spheron's GCP claim.** Spheron's "GCP me-central1 (UAE)" is questionable. To my knowledge, GCP me-central1 is Doha, Qatar, and me-central2 is Dammam, and GCP has no UAE region. This was not verified this session, so treat Spheron's Gulf details with caution.

**AWS UAE GPU fit.** An L40S (48 GB) G6e in the UAE can serve 30B-class models and gpt-oss-20b, not gpt-oss-120b (needs at least 80 GB). Multi-GPU g6e sizes, such as g6e.12xlarge with 4x L40S, could serve gpt-oss-120b with tensor parallelism. Price not verified.

### Gaps
- No AWS me-central-1 G6e or P5 on-demand price was fetched.
- No official Azure UAE North GPU capacity confirmation.
- Core42 Compass per-token pricing and API compatibility are unknown.
- RunPod, Lambda, Hetzner, DeepInfra and Groq SOC 2 and retention terms were not fetched.
- OpenRouter's own prompt-logging policy and SOC 2 status: the ZDR guide URL returned 404.
- DIFC and ADGM data-protection regimes were not covered.
- Whether Together ZDR applies to dedicated endpoints is not stated.

## 4. Practical setup: vLLM on a rented pod, cold starts, weight storage, keeping `syslab-default` as the single front door

### Takeaway
The cleanest path is to point the existing gateway's `LLM_BASE_URL` at the remote OpenAI-compatible `/v1` and set `LLM_MODEL` to the remote model name. The `syslab-default` alias then keeps hiding the real name. Three gaps in the current code need work first:
1. No upstream `Authorization` header is sent.
2. A vLLM/Qwen-only `chat_template_kwargs` field is always injected.
3. The output-budget clamp depends on the vLLM-only `max_model_len` field from `/v1/models`.

A vLLM pod on RunPod keeps working with gaps 2 and 3, because it is vLLM. Hosted APIs like OpenRouter or Together may not.

### Cited Findings
**RunPod Serverless vLLM** — [RunPod vLLM overview](https://docs.runpod.io/serverless/vllm/overview):
- Deploy from the RunPod Hub or fork `runpod-workers/worker-vllm`.
- Model delivery is either "cached models" (the recommended default) or baked into a custom image, which "brings cold starts down to seconds".
- Endpoints scale from zero and bill per second. They accept OpenAI-compatible tooling.

**RunPod vLLM worker environment variables** — [RunPod vLLM env vars](https://docs.runpod.io/serverless/vllm/environment-variables):
- `MODEL_NAME`, `MAX_MODEL_LEN`, `TENSOR_PARALLEL_SIZE`, `QUANTIZATION`, `GPU_MEMORY_UTILIZATION` (0.95).
- `ENABLE_AUTO_TOOL_CHOICE`, `TOOL_CALL_PARSER`, `REASONING_PARSER`.
- `OPENAI_SERVED_MODEL_NAME_OVERRIDE` sets a custom model ID in `/v1/models`.
- `BASE_PATH` defaults to `/runpod-volume`, the network-volume cache.
- `MAX_CONCURRENCY` defaults to 30 per worker.
- Any `vllm serve` flag can be set as an upper-snake-case env var, or passed through `VLLM_EXTRA_ARGS`.

**RunPod network volume:** $0.07/GB/month — [RunPod pricing](https://www.runpod.io/pricing).

**vLLM tool-call parsers** — [vLLM tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling.html):

| Model family | `--tool-call-parser` |
|---|---|
| gpt-oss | `openai` |
| Qwen3-Coder | `qwen3_xml` |
| GLM-4.5/4.6 | `glm45` |
| GLM-4.7 | `glm47` |
| DeepSeek V3 | `deepseek_v3` (chat template recommended) |
| DeepSeek V3.1 | `deepseek_v31` (chat template recommended) |
| Kimi K2 | `kimi_k2` |

- `--enable-auto-tool-choice` is required for `tool_choice="auto"`.
- `tool_choice="required"` and named function calling are supported by default.

**Cold starts and idle billing elsewhere:**
- Fireworks on-demand deployments scale to zero after 1 h idle by default, and start-up time is not billed — [Fireworks pricing](https://fireworks.ai/pricing).
- HF Inference Endpoints bill per minute. Scaled-to-zero endpoints still count against instance quota; paused ones do not — [HF pricing](https://huggingface.co/docs/inference-endpoints/pricing).

**Current gateway code in this repo:**
- `C:/Users/amro/Documents/Github/syslab-server/app/config.py` lines 35-36: `LLM_BASE_URL` (default `http://127.0.0.1:8000/v1`) and `LLM_MODEL` come from env. Line 216 is `MODEL_ALIASES = {"syslab-default": LLM_MODEL}`.
- `C:/Users/amro/Documents/Github/syslab-server/app/llm.py` `_post()` (lines 61-67) sends only `Content-Type`, with no `Authorization: Bearer`. Any hosted backend (RunPod serverless, Together, Fireworks, OpenRouter, DeepInfra) requires a key, so an `LLM_API_KEY` env plus header is needed.
- `app/gateway.py` line 208 and `app/llm.py` line 51 always set `chat_template_kwargs: {"enable_thinking": False}`. This is a vLLM extension read by Qwen3's template. Hosted APIs may ignore or reject it, and gpt-oss uses `reasoning_effort` instead.
- `app/llm.py` `model_window()` (lines 88-115) reads `max_model_len` from `/v1/models`. Only vLLM publishes that field. Hosted APIs return `None`, so the output-budget clamp cannot size the window.
- `ChatCompletionRequest` uses `extra="allow"` (`app/gateway.py` line 49). OpenAI fields, and an OpenRouter `provider` object if a caller sends one, pass through untouched.

### Inferences
**Option A: rented pod or serverless running vLLM** (RunPod, Lambda, Hetzner, Modal).
- Set `LLM_BASE_URL` to the pod's `/v1` and `LLM_MODEL` to the served name.
- Start vLLM with `--api-key` (or use the RunPod key) plus the tool parser above. Add `REASONING_PARSER` for Qwen3 or DeepSeek.
- Because the backend is still vLLM, `chat_template_kwargs` and `max_model_len` keep working. Only the auth header is needed.
- Weights belong on a network volume (a roughly 65 GB gpt-oss-120b is about $4.50/month on RunPod) or baked into the image, to avoid re-downloading on every cold start.
- For a dedicated pod, a private tunnel (Tailscale or WireGuard) between the box and the pod avoids exposing vLLM publicly. This is a suggestion, not something I verified on a provider.

**Option B: managed per-token API** (OpenRouter, Fireworks, Together, DeepInfra).
- Same two env vars plus an auth header.
- Also make `chat_template_kwargs` conditional, for example only when the backend is local vLLM.
- Also give the clamp a configured fallback window, or read `context_length`, which OpenRouter returns, when `max_model_len` is absent.
- For OpenRouter, the gateway, not callers, should inject `provider: {zdr: true, only: [...], allow_fallbacks: false, require_parameters: true}` so the privacy policy cannot be bypassed.

**Keep embeddings local.** The embedding URL is a separate variable (`app/config.py` around line 45), so moving chat to a remote backend does not have to move embeddings. Document text then never leaves the box for embedding, only for the chat turns that include retrieved passages.

**Hybrid with failover.** A cheap pattern is two upstreams: the local 5090 for 30B-class requests and a remote backend for a bigger model under a second alias. The current gateway maps a single alias to a single `LLM_BASE_URL`. Supporting this means a per-alias base URL, a small change, but not one that exists today.

### Gaps
- The exact RunPod serverless OpenAI base-URL format and FlashBoot cold-start timings were not on the fetched pages. From memory it is `https://api.runpod.ai/v2/<endpoint_id>/openai/v1`, unverified.
- Measured cold-start times for loading a 65 GB model from a network volume vs a baked image were not found.
- Whether Together, Fireworks and DeepInfra reject or ignore an unknown `chat_template_kwargs` field is untested.
- No vLLM parser for DeepSeek V3.2 or generic Qwen3 is listed on the vLLM page. Qwen3 commonly uses `hermes`, which is unverified here.

## 5. Break-even: renting vs buying a second GPU or an RTX PRO 6000

### Takeaway
RTX PRO 6000 street prices have nearly doubled to about $16,000-18,000 (October 2026). Against renting the same card, buying pays back in about 9-13 months at 24/7 use and 2.5-3.5+ years at business-hours use. Against per-token APIs for gpt-oss-120b, buying almost never pays back on cost. Buying only makes sense for data-residency or single-tenancy reasons, or for very heavy sustained use.

### Cited Findings
- RTX PRO 6000 Blackwell launched at $8,565 at US retail — [Tom's Hardware](https://www.tomshardware.com/pc-components/gpus/nvidia-rtx-pro-6000-blackwell-gpu-is-listed-for-usd8-565-at-us-retailer-26-percent-more-expensive-than-the-last-gen-rtx-6000-ada).
- By October 2026: NVIDIA marketplace about $16,000, and Newegg and B&H $15,999+ — [Thunder Compute](https://www.thundercompute.com/blog/nvidia-rtx-pro-6000-pricing); [TweakTown](https://www.tweaktown.com/news/113098/nvidias-rtx-pro-6000-blackwell-gpu-price-almost-doubles-reaching-dollars16000/).
- A tracker showed a US low of $17,987 on 2026-10-22, 20% above its 3-month typical of $14,999. UK low £14,999, France €14,299 — [gpuprix](https://gpuprix.com/us/gpus/rtx-pro-6000-blackwell). Coverage attributes the rise to GDDR7 memory costs (32 modules per card).
- Average rental is $3.27/GPU-h across nine providers (2026-10-16) — [Thunder Compute](https://www.thundercompute.com/blog/nvidia-rtx-pro-6000-pricing).
- RunPod PRO 6000: $2.49/h Secure, $1.69/h Community — [RunPod pricing](https://www.runpod.io/pricing).
- Hetzner GEX63 (PRO 6000 Max-Q): €999/month + €999 setup — [Hetzner](https://www.hetzner.com/dedicated-rootserver/matrix-gpu/).
- gpt-oss-120b per-token: $0.15/$0.60 on Together, Fireworks and Groq; from $0.017/$0.062 on OpenRouter — [Together](https://www.together.ai/pricing); [Fireworks](https://fireworks.ai/pricing); [Groq](https://console.groq.com/docs/models); [OpenRouter](https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints).

### Inferences
**Payback on a $16,000 card vs renting a PRO 6000** (GPU only; excludes host and PSU upgrade, power, cooling, resale):

| Rental compared against | Hours to break even | 24/7 (730 h/mo) | Business hours (220 h/mo) |
|---|---|---|---|
| RunPod Secure ($2.49/h) | 6,426 h | 8.8 months | 29 months |
| RunPod Community ($1.69/h) | 9,467 h | 13 months | 43 months |
| Market average ($3.27/h) | 4,893 h | 6.7 months | 22 months |
| Hetzner GEX63 (€999/mo) | — | about 16 months, plus setup fee and EUR/USD | — |

**Vs per-token API.** $16,000 buys about 61B blended gpt-oss-120b tokens at $0.26/M on Together, Fireworks or Groq. At 1B tokens/month, that is about 5 years of API use before a purchased card pays off. A 96 GB card also does not run the Qwen3-235B, GLM-4.x or DeepSeek-class models at usable precision, though their APIs are similarly cheap ($0.09-0.55/M for Qwen3-235B).

**Power.** Power is not included above. A 600 W workstation card at 24/7 draws about 438 kWh/month. The UAE commercial tariff was not researched, but at an assumed $0.10/kWh that is about $44/month, small next to the payback math.

**Strongest case for buying.** Client data must stay on-premises in the UAE, and the hosted in-country alternatives (AWS UAE at about $12.30/GPU-h for H100, Azure UAE North at about $14) are ruinously expensive: about $9,000-10,000/month 24/7. Against those, a $16-18k card pays back in under 2 months of equivalent 24/7 use.

**Middle path.**
- Keep the 5090 for 30B-class and private work.
- Route bigger-model traffic to a ZDR per-token API for non-sensitive data.
- Rent a RunPod Secure PRO 6000 or H100 by the hour only for bursts or experiments.
- Defer buying a PRO 6000 while prices are about 87% above MSRP.

### Gaps
- Second RTX 5090 street price in November 2026 was not researched.
- Exact UAE (DEWA/ADDC) commercial electricity tariff was not researched.
- Whether the existing box's PSU, slot and chassis can take a second GPU (5090 or 600 W PRO 6000) was not checked.
- Resale value and depreciation assumptions are not sourced.
- No November 2026 retail price was found for the RTX PRO 6000. The latest figures are from October 2026.
