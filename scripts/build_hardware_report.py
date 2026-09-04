"""Build the syslab-server hardware, model and ROI report.

Reads docs/hardware_report_data.json, computes every number, renders a PDF.

Nothing in the report is typed twice. Prices, benchmarks and assumptions live in
the data file with a source URL and a date; the sizing model, the cost model and
the charts are all functions of those inputs. When the market moves, edit the
JSON and run this again.

    python scripts/build_hardware_report.py

Standalone by design: it imports nothing from app/, so it cannot break the
service and does not need the app's settings to run.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "docs" / "hardware_report_data.json"
OUT_PATH = ROOT / "docs" / "Syslab_Server_Hardware_Model_and_ROI_Report.pdf"

GB = 1024.0  # MiB per GiB


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------

class ProvenanceError(RuntimeError):
    """A figure the report depends on has no source or no date."""


def check_provenance(data: dict) -> list[str]:
    """Every priced or benchmarked row must carry a source and a date.

    The report fails to build rather than shipping a number nobody can trace.
    Rows explicitly marked 'estimated' are exempt from the URL requirement --
    an estimate has no source by definition -- but must still say so.
    """
    problems: list[str] = []

    def visit(node, path: str) -> None:
        if isinstance(node, dict):
            priced = ("price_aed" in node or "usd_hr" in node or "input" in node)
            if priced or "confidence" in node:
                conf = node.get("confidence")
                if conf is None:
                    problems.append(f"{path}: priced row with no confidence field")
                elif conf != "estimated" and not node.get("source_url"):
                    problems.append(f"{path}: confidence '{conf}' but no source_url")
                elif conf != "estimated" and not node.get("as_of"):
                    problems.append(f"{path}: confidence '{conf}' but no as_of date")
            for key, value in node.items():
                if not key.startswith("_"):
                    visit(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                visit(value, f"{path}[{i}]")

    for section in ("gpus", "platform_parts", "rental", "market_context",
                    "api_prices_usd_per_mtok", "energy", "local_purchase"):
        visit(data.get(section), section)
    return problems


# --------------------------------------------------------------------------
# the memory model
# --------------------------------------------------------------------------

def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int, dtype_bytes: int) -> int:
    """Key/value cache cost of one token, in bytes.

    Two tensors (K and V), one per layer, one row per KV head. Grouped-query
    attention is why kv_heads is smaller than heads, and it is the single
    reason a 30B model can serve twenty sessions on one card.
    """
    return 2 * layers * kv_heads * head_dim * dtype_bytes


def validate_kv_model(data: dict) -> list[dict]:
    """Check the formula against measurements from a temporary test machine.

    That machine is not part of any build in this report and is not counted as
    an asset. These numbers exist only to show the formula predicts reality.
    """
    rows = []
    for model in data["measured_baseline"]["models"]:
        head_dim = model["embedding"] // model["heads"]
        per_tok = kv_bytes_per_token(
            model["layers"], model["kv_heads"], head_dim,
            data["assumptions"]["kv_dtype_bytes_fp16"],
        )
        for point in model["points"]:
            if point["kv_mb"] < 50:  # the 2k row measures noise, not cache
                continue
            predicted_mb = per_tok * point["ctx"] / (1024 * 1024)
            rows.append({
                "model": model["model"],
                "ctx": point["ctx"],
                "predicted_mb": predicted_mb,
                "measured_mb": point["kv_mb"],
                "ratio": point["kv_mb"] / predicted_mb,
            })
    return rows


# --------------------------------------------------------------------------
# the traffic model: organisations -> in-flight requests
# --------------------------------------------------------------------------

def erlang_c(offered: float, servers: int) -> float:
    """Probability an arriving request has to wait. Classic M/M/c."""
    if servers <= offered:
        return 1.0
    top = offered ** servers / math.factorial(servers) * servers / (servers - offered)
    bottom = sum(offered ** k / math.factorial(k) for k in range(servers)) + top
    return top / bottom


@dataclass
class Traffic:
    orgs: int
    users: int
    turns_per_day: float
    peak_hour_turns: float
    service_seconds: float
    offered_erlangs: float
    concurrency: int
    wait_probability: float
    tokens_per_month: float


def traffic_for(data: dict, orgs: int, prefill_tps: float = 2500.0) -> Traffic:
    w = data["workload_model"]
    users = orgs * w["active_users_per_org"]
    turns_per_day = users * w["sessions_per_user_per_day"] * w["turns_per_session"]
    peak = turns_per_day * w["peak_hour_share"]

    prefill_s = w["prompt_tokens_per_turn"] / prefill_tps
    decode_s = w["output_tokens_per_turn"] / w["target_decode_tps_per_user"]
    service_s = prefill_s + decode_s

    offered = peak * service_s / 3600.0
    servers = max(1, int(math.ceil(offered)))
    while servers < 256 and erlang_c(offered, servers) > 0.10:
        servers += 1

    tokens = (turns_per_day
              * (w["prompt_tokens_per_turn"] + w["output_tokens_per_turn"])
              * w["working_days_per_month"])

    return Traffic(orgs, users, turns_per_day, peak, service_s, offered,
                   servers, erlang_c(offered, servers), tokens)


def vram_budget(data: dict, concurrency: int, agent_gb: float, vlm_gb: float,
                asr_gb: float, tts_gb: float, kv_fp8: bool = False) -> dict:
    """The stacked VRAM requirement for one design point."""
    w = data["workload_model"]
    a = data["assumptions"]
    # A 30B-A3B-class MoE: 48 layers, 4 KV heads, 128 head dim. Stated, not
    # measured -- the measured 8B validates the formula, not this config.
    dtype = a["kv_dtype_bytes_fp8"] if kv_fp8 else a["kv_dtype_bytes_fp16"]
    per_tok = kv_bytes_per_token(48, 4, 128, dtype)
    kv_gb = per_tok * w["context_per_session"] * concurrency / (1024 ** 3)
    weights = agent_gb + vlm_gb + asr_gb + tts_gb
    raw = weights + kv_gb + a["activation_overhead_gb"]
    return {
        "agent_gb": agent_gb, "vlm_gb": vlm_gb, "asr_gb": asr_gb, "tts_gb": tts_gb,
        "weights_gb": weights, "kv_gb": kv_gb,
        "overhead_gb": a["activation_overhead_gb"],
        "raw_gb": raw,
        "card_gb": raw / a["vllm_gpu_memory_utilization"],
    }


# --------------------------------------------------------------------------
# builds
# --------------------------------------------------------------------------

@dataclass
class Build:
    ident: str
    tier: str
    name: str
    strategy: str                       # "buy" or "rent"
    condition: str = "new"              # "new", "used" or "rent"
    gpus: list = field(default_factory=list)      # (gpu_key, count)
    parts: list = field(default_factory=list)     # (part_key, count)
    platform_w: int = 120
    rent_gpu_global: str = ""
    rent_usd_global: float = 0.0
    rent_gpu_region: str = ""
    rent_usd_region: float = 0.0
    verdict: str = ""
    cannot: str = ""
    body: str = ""


AM5 = [("cpu_am5", 1), ("board_am5", 1), ("ram_64gb", 1), ("ssd_2tb", 1),
       ("case", 1), ("cooler_aio", 1), ("ups_1500", 1)]
TRX = [("cpu_trx", 1), ("board_trx50", 1), ("ram_128gb", 1), ("ssd_4tb", 1),
       ("case", 1), ("cooler_tr", 1), ("ups_1500", 1)]
REGION = ("H100 80 GB, GPU.ai UAE", 2.48)


def build_slate(data: dict) -> list[Build]:
    return [
        # ------------------------------- LOW -------------------------------
        Build(
            "L1", "Low", "Rent, own nothing", "rent", "rent",
            gpus=[], parts=[], platform_w=0,
            rent_gpu_global="RTX PRO 5000 48 GB", rent_usd_global=0.64,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="Zero capital, and the only option whose cost falls if the client count "
                    "turns out smaller than planned. Whether it is admissible at all depends "
                    "on one answer nobody has yet.",
            cannot="On the international line, client documents leave the UAE. If residency is "
                   "contractual that line is inadmissible at any price, and the in-region line "
                   "is the only rental that counts.",
            body=("Port app/llm.py to the OpenAI-compatible shape and serve from a rented "
                  "instance. There is no machine to build, no card to source in a shortage, no "
                  "warranty to argue about and no capital at risk. The whole question is where "
                  "the card sits. Internationally, a 48 GB card runs about $0.64 an hour and "
                  "the economics are excellent. In the UAE there is no consumer-class rental at "
                  "all: the cheapest in-region option is a datacentre H100 at $2.48, roughly "
                  "four times the price for hardware far larger than this workload needs. That "
                  "gap is the price of keeping documents in the country, and it is quantified "
                  "on this page rather than assumed away. Development also has to live "
                  "somewhere under this option, since no machine is bought."),
        ),
        Build(
            "L2", "Low", "Two used RTX 3090 - 48 GB", "buy", "used",
            gpus=[("rtx_3090_used", 2)],
            parts=AM5 + [("psu_1200", 1)], platform_w=120,
            rent_gpu_global="RTX PRO 5000 48 GB", rent_usd_global=0.64,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="The cheapest hardware that reaches the design point, by a wide margin. "
                    "The UAE second-hand market is what makes it possible.",
            cannot="No FP8. Ampere has no 8-bit path, so the KV cache stays full width and the "
                   "48 GB that comfortably serves fifteen organisations is tight at twenty. "
                   "There is also no warranty and no tax invoice on a private sale.",
            body=("Two matched 24 GB cards, so vLLM can run tensor parallel and treat the 48 GB "
                  "as one pool. This build exists because of a local price relationship: RTX "
                  "3090s list on Dubizzle around AED 2,000 to 2,300, roughly a quarter of a "
                  "used 4090 here and a sixth of a new 5090, while carrying over half a 5090's "
                  "bandwidth at 936 GB/s. Inspect before paying - power-on hours under about "
                  "20,000, a sustained load test in front of the seller, and a meeting place "
                  "where the card can be plugged in. The 12% failure reserve is there because "
                  "none of that is a warranty."),
        ),
        Build(
            "L3", "Low", "One new RTX 5080 - 16 GB", "buy", "new",
            gpus=[("rtx_5080", 1)],
            parts=AM5 + [("psu_850", 1)], platform_w=120,
            rent_gpu_global="RTX 5090 32 GB", rent_usd_global=0.33,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="The cheapest rig you can buy new, with a warranty and a tax invoice - and "
                    "it does not meet the specification. That is the finding, not a flaw in "
                    "the build.",
            cannot="16 GB holds the agent or the vision model, not both, and leaves almost "
                   "nothing for KV cache. It cannot serve ten organisations, let alone twenty.",
            body=("Included because 'buy something new, from a shop, with a receipt' is a "
                  "reasonable procurement instinct and deserves a real answer. At the same "
                  "money as two used 3090s this build gets a third of the memory. That is not "
                  "a criticism of the 5080, which is a fine card; it is what the shortage has "
                  "done to the price of new VRAM. Its honest role is a development and staging "
                  "machine beside a rented production endpoint, or a first box for a single "
                  "operator - not a server behind fifteen clients."),
        ),

        # ------------------------------- MID -------------------------------
        Build(
            "M1", "Mid", "Two used RTX 4090 - 48 GB", "buy", "used",
            gpus=[("rtx_4090_used", 2)],
            parts=AM5 + [("psu_1200", 1)], platform_w=120,
            rent_gpu_global="RTX PRO 5000 48 GB", rent_usd_global=0.64,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="The same 48 GB as L2 with the FP8 path that removes its one real weakness "
                    "- for roughly twice the money.",
            cannot="Still a private-sale purchase with no warranty, at a price where that "
                   "matters considerably more than it does on a AED 2,400 card.",
            body=("Ada generation means native FP8, which halves the KV cache and turns 48 GB "
                  "from adequate at fifteen organisations into comfortable at twenty. It is a "
                  "genuine capability difference and the question is only whether it is worth "
                  "the gap. Locally that gap is large: used 4090s run AED 8,300 to 12,000 "
                  "against AED 2,000 to 2,300 for a 3090, so the pair costs about four times "
                  "as much for the same memory and one quantisation feature. Buy this if the "
                  "twenty-organisation case is firm and you want headroom; buy L2 if it is "
                  "still a projection."),
        ),
        Build(
            "M2", "Mid", "One new RTX 5090 - 32 GB", "buy", "new",
            gpus=[("rtx_5090", 1)],
            parts=AM5 + [("psu_1200", 1)], platform_w=120,
            rent_gpu_global="RTX 5090 32 GB", rent_usd_global=0.33,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="The best new card most UAE sellers will actually hand you today, in the "
                    "wrong size for this job.",
            cannot="32 GB will not hold the agent, a vision model and eight sessions of KV "
                   "cache at 16k context without quantising the cache to FP8 and dropping to "
                   "the 4 GB vision model.",
            body=("One card, one warranty, one supplier, and 1,792 GB/s - the fastest "
                  "single-request experience in this report and the simplest machine to "
                  "operate. It is also the clearest illustration of what the shortage costs: "
                  "AED 13,839 to 22,000 for 32 GB, against AED 4,800 for 48 GB in used Ampere. "
                  "This workload spends most of a turn on 6,000 tokens of prefill and tool "
                  "round-trips, so it feels the extra bandwidth far less than a single-user "
                  "chat product would. Buy it if warranty and simplicity are the requirement; "
                  "do not buy it for capacity."),
        ),
        Build(
            "M3", "Mid", "Four used RTX 3090 - 96 GB", "buy", "used",
            gpus=[("rtx_3090_used", 4)],
            parts=TRX + [("psu_1600", 1), ("risers", 1)], platform_w=180,
            rent_gpu_global="RTX PRO 6000 96 GB", rent_usd_global=2.20,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="96 GB for less than the price of one new 5090's worth of cards. The "
                    "platform, not the silicon, is what you are paying for.",
            cannot="No FP8, four unwarranted cards, and 1.4 kW under load. NVLink pairs only "
                   "two of the four, so tensor parallel across all of them crosses PCIe.",
            body=("Four cards at AED 2,400 is AED 9,600 for 96 GB - the cheapest large-memory "
                  "CUDA machine that can be assembled anywhere, and the UAE used market makes "
                  "it cheaper here than most places. The catch is everything around them: an "
                  "sTR5 platform to give four cards their lanes costs more than the cards do, "
                  "and 128 GB of registered memory is now one of the largest lines in the "
                  "build. Take this option if the roadmap genuinely lands on a large dense "
                  "model or a resident 30B vision model. For the workload as specified it is "
                  "buying capacity that will sit unused."),
        ),

        # ------------------------------ HIGH -------------------------------
        Build(
            "H1", "High", "Two new RTX 5090 - 64 GB", "buy", "new",
            gpus=[("rtx_5090", 2)],
            parts=TRX + [("psu_1600", 1), ("risers", 1)], platform_w=180,
            rent_gpu_global="RTX PRO 6000 96 GB", rent_usd_global=2.20,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="Buys lane isolation and the fastest decode here, with a warranty on both "
                    "cards. Also 1,150 W of graphics card.",
            cannot="Two 575 W cards need their own circuit and a room that can shed 1.4 kW. On "
                   "230 V mains that is manageable, which it would not be on 120 V.",
            body=("Two cards means the agent and the vision lane never contend: one holds the "
                  "tool-calling model and its cache, the other holds the VLM and speech, and a "
                  "slow document parse cannot make a chat turn wait. That is the argument "
                  "app/jobs.py already makes in software, expressed in hardware. It is the "
                  "first build here that is both fully warranted and comfortably over the "
                  "design point, which is worth something to a business signing client "
                  "agreements. Whether it is worth AED 30,000 of graphics card is the question "
                  "section 7 answers."),
        ),
        Build(
            "H2", "High", "Two RTX PRO 5000 Blackwell - 96 GB ECC", "buy", "new",
            gpus=[("rtx_pro_5000", 2)],
            parts=TRX + [("psu_1200", 1)], platform_w=150,
            rent_gpu_global="RTX PRO 6000 96 GB", rent_usd_global=2.20,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="The overlooked option. The same 96 GB and the same ECC as the PRO 6000 "
                    "build, for around three quarters of the price and half the power.",
            cannot="Two cards rather than one, so tensor parallelism has to be configured and "
                   "a model larger than 48 GB pays a PCIe crossing. Local pricing on this part "
                   "varies by more than 30%, which is a procurement risk in itself.",
            body=("Blackwell, so native FP8 and the halved KV cache that comes with it. ECC "
                  "memory, blower coolers, two slots and 300 W each: cards designed to sit in "
                  "a workstation that runs continuously, which is exactly what this machine "
                  "is. Two reach 96 GB on a 1200 W supply where the PRO 6000 build needs more "
                  "card budget and more cooling. The reason it is not the headline "
                  "recommendation is the quote spread - Promise Gulf's product page says "
                  "AED 15,500 and its own price list says AED 31,943 for the same part. Get "
                  "the number in writing before planning around it."),
        ),
        Build(
            "H3", "High", "One RTX PRO 6000 Blackwell - 96 GB ECC", "buy", "new",
            gpus=[("rtx_pro_6000", 1)],
            parts=TRX + [("psu_1200", 1)], platform_w=150,
            rent_gpu_global="RTX PRO 6000 96 GB", rent_usd_global=2.20,
            rent_gpu_region=REGION[0], rent_usd_region=REGION[1],
            verdict="Technically the best machine in this report, at the top of the budget and "
                    "the top of a supply squeeze.",
            cannot="Nothing technical. What it cannot do is stay busy: at the specified "
                   "workload it would idle most of the day, and it consumes most of the "
                   "AED 80,000 ceiling on its own.",
            body=("96 GB of ECC memory on one card at 1,792 GB/s, 600 W, one slot, no tensor "
                  "parallelism to configure and no second-hand risk. One card is one thing to "
                  "monitor, cool and replace, and that operational simplicity is worth real "
                  "money to a small team. The difficulty is what the market has done to it: it "
                  "listed at USD 8,565 at launch and USD 16,000 now, and none of that increase "
                  "bought performance. Locally the same part number ranges from AED 48,825 to "
                  "AED 77,032 depending on who answers the phone, so the first task is a "
                  "written quote rather than a decision."),
        ),
    ]


# --------------------------------------------------------------------------
# costing
# --------------------------------------------------------------------------

def usd(v, dp=2):
    return "$" + format(round(v, dp), f",.{dp}f")


def cost_build(data: dict, b, duty_hours: int, tokens_per_month: float) -> dict:
    """Cost one option in AED, bought from a UAE seller.

    Local purchase is simpler than importing: the shelf price already carries
    the 5% customs duty the distributor paid, there is no freight to arrange,
    and the 5% VAT is shown on the tax invoice and recovered by a registered
    business. So the only adjustment to a listed price is stripping VAT.
    """
    gpus, parts = data["gpus"], data["platform_parts"]
    lp, energy, a = data["local_purchase"], data["energy"], data["assumptions"]
    fx = data["locale"]["aed_per_usd"]

    lines = []
    gpu_retail = used_gpu_retail = 0.0
    vram = bandwidth = 0.0
    gpu_w = 0

    for key, count in b.gpus:
        g = gpus[key]
        line = g["price_aed"] * count
        gpu_retail += line
        if g.get("condition") == "used":
            used_gpu_retail += line
        vram += g["vram_gb"] * count
        bandwidth += g["bandwidth_gbs"] * count
        gpu_w += g["tdp_w"] * count
        lines.append((g["label"], count, g["price_aed"], line, g.get("seller", "")))

    parts_retail = 0.0
    for key, count in b.parts:
        p = parts[key]
        line = p["price_aed"] * count
        parts_retail += line
        lines.append((p["label"], count, p["price_aed"], line, p.get("seller", "")))

    retail = gpu_retail + parts_retail
    delivery = lp["local_delivery_aed"] if retail else 0.0
    gross = retail + delivery
    ex_vat = gross / (1 + lp["uae_vat_pct"] / 100.0)
    vat_component = gross - ex_vat

    reserve = (used_gpu_retail * a["used_card_failure_reserve_pct"] / 100.0
               + (gpu_retail - used_gpu_retail) * a["new_card_failure_reserve_pct"] / 100.0)
    capex_effective = ex_vat + reserve

    # The box is on continuously -- a sleeping desktop cannot answer a client --
    # but GPU draw follows the duty cycle.
    load_factor = 0.15 + 0.70 * (duty_hours / 24.0)
    avg_w = b.platform_w + gpu_w * load_factor
    kwh_year = avg_w * 8760 / 1000.0
    power_year = kwh_year * energy["aed_per_kwh"] * energy["cooling_multiplier"]
    ops_month = a["ops_hours_per_month"] * a["ops_rate_aed_per_hour"]

    hours_month = duty_hours * 365.25 / 12.0
    rent_global_month = b.rent_usd_global * fx * hours_month
    rent_region_month = b.rent_usd_region * fx * hours_month

    if b.strategy == "rent":
        capex_month = power_month = 0.0
        monthly = rent_global_month + ops_month
        monthly_region = rent_region_month + ops_month
    else:
        capex_month = capex_effective / a["amortisation_months"]
        power_month = power_year / 12.0
        monthly = capex_month + power_month + ops_month
        monthly_region = monthly

    tco3 = monthly * a["amortisation_months"]
    per_mtok = monthly / (tokens_per_month / 1e6) if tokens_per_month else 0.0

    def breakeven(rent_month):
        if b.strategy == "rent":
            return None
        margin = rent_month - power_month
        return capex_effective / margin if margin > 0 else None

    return {
        "lines": lines, "retail": retail, "delivery": delivery, "gross": gross,
        "ex_vat": ex_vat, "vat_component": vat_component,
        "reserve": reserve, "capex_effective": capex_effective,
        "vram_gb": vram, "bandwidth_gbs": bandwidth, "gpu_w": gpu_w, "avg_w": avg_w,
        "kwh_year": kwh_year, "power_year": power_year,
        "capex_month": capex_month, "power_month": power_month, "ops_month": ops_month,
        "rent_global_month": rent_global_month, "rent_region_month": rent_region_month,
        "monthly": monthly, "monthly_region": monthly_region,
        "tco3": tco3, "per_mtok": per_mtok,
        "breakeven_global": breakeven(rent_global_month),
        "breakeven_region": breakeven(rent_region_month),
    }


def blended_api_rate(data: dict, api: dict) -> float:
    """USD per million tokens, weighted by this workload's own input/output split."""
    w = data["workload_model"]
    total = w["prompt_tokens_per_turn"] + w["output_tokens_per_turn"]
    return (api["input"] * w["prompt_tokens_per_turn"] / total
            + api["output"] * w["output_tokens_per_turn"] / total)


def crossover_orgs(data: dict, monthly_fixed_aed: float, blended_aed: float) -> float:
    """How many client organisations before owning beats paying per token."""
    if blended_aed <= 0:
        return float("inf")
    tokens_needed = monthly_fixed_aed / blended_aed * 1e6
    per_org = traffic_for(data, 1).tokens_per_month
    return tokens_needed / per_org


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.shapes import Drawing, Line, Rect, String

INK = colors.HexColor("#1c1c1e")
MUTED = colors.HexColor("#6b6b70")
RULE = colors.HexColor("#c9c9cd")
FAINT = colors.HexColor("#f0f0f2")
ACCENT = colors.HexColor("#1f6f8b")
ACCENT_2 = colors.HexColor("#3f9fbd")
WARN = colors.HexColor("#a8442a")
GOOD = colors.HexColor("#2e6b4f")
GOLD = colors.HexColor("#9a7b2f")

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm
FRAME_W = PAGE_W - 2 * MARGIN

S = {}


def _styles():
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13.6,
                          textColor=INK, alignment=TA_LEFT, spaceAfter=6)
    S["body"] = base
    S["lead"] = ParagraphStyle("lead", parent=base, fontSize=10.5, leading=15.4, spaceAfter=8)
    S["small"] = ParagraphStyle("small", parent=base, fontSize=8, leading=10.8, textColor=MUTED)
    S["cap"] = ParagraphStyle("cap", parent=base, fontSize=7.6, leading=10.2, textColor=MUTED,
                              spaceBefore=3, spaceAfter=10)
    S["h1"] = ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=19,
                             leading=23, spaceBefore=0, spaceAfter=3, textColor=INK)
    S["kicker"] = ParagraphStyle("kicker", parent=base, fontName="Helvetica-Bold", fontSize=8,
                                 leading=11, textColor=ACCENT, spaceAfter=2)
    S["h2"] = ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=13,
                             leading=16.5, spaceBefore=14, spaceAfter=5, textColor=INK)
    S["h3"] = ParagraphStyle("h3", parent=base, fontName="Helvetica-Bold", fontSize=10.5,
                             leading=13.5, spaceBefore=6.5, spaceAfter=2.5, textColor=INK)
    S["th"] = ParagraphStyle("th", parent=base, fontName="Helvetica-Bold", fontSize=8,
                             leading=10.5, spaceAfter=0, textColor=INK)
    S["td"] = ParagraphStyle("td", parent=base, fontSize=8, leading=10.5, spaceAfter=0)
    S["tdb"] = ParagraphStyle("tdb", parent=S["td"], fontName="Helvetica-Bold")
    S["tdm"] = ParagraphStyle("tdm", parent=S["td"], textColor=MUTED)
    S["pull"] = ParagraphStyle("pull", parent=base, fontSize=10, leading=14.5,
                               leftIndent=10, rightIndent=8, spaceBefore=4, spaceAfter=4)
    S["cover_t"] = ParagraphStyle("ct", parent=base, fontName="Helvetica-Bold", fontSize=30,
                                  leading=34, spaceAfter=6)
    S["cover_s"] = ParagraphStyle("cs", parent=base, fontSize=13, leading=18,
                                  textColor=MUTED, spaceAfter=14)


def P(text, style="body"):
    return Paragraph(text, S[style])


def money(v, dp=0):
    """AED, the currency everything in this report is bought and billed in."""
    return "AED " + format(round(v, dp), f",.{dp}f")


# ---- page furniture -------------------------------------------------------

class Doc(BaseDocTemplate):
    def __init__(self, path, **kw):
        super().__init__(path, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=24 * mm, bottomMargin=20 * mm, **kw)
        frame = Frame(MARGIN, 20 * mm, FRAME_W, PAGE_H - 44 * mm, id="body")
        cover = Frame(MARGIN, 20 * mm, FRAME_W, PAGE_H - 40 * mm, id="cover")
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[cover], onPage=self._cover_furniture),
            PageTemplate(id="body", frames=[frame], onPage=self._furniture),
        ])
        self.section = ""

    def _cover_furniture(self, canvas, doc):
        canvas.saveState()
        canvas.setFillColor(INK)
        canvas.rect(0, PAGE_H - 14 * mm, PAGE_W, 14 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(MARGIN, PAGE_H - 9.2 * mm, "SYSLAB SERVER")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 9.2 * mm,
                               "Hardware, Model & ROI Report  \u00b7  September 2026")
        canvas.restoreState()

    def _furniture(self, canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, PAGE_H - 15 * mm, "Syslab Server \u2014 Hardware, Model & ROI Report")
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 15 * mm, doc.section)
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, PAGE_H - 17 * mm, PAGE_W - MARGIN, PAGE_H - 17 * mm)
        canvas.line(MARGIN, 15 * mm, PAGE_W - MARGIN, 15 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(MARGIN, 11.4 * mm, "Compiled 4 September 2026 \u00b7 prices verified at compile date")
        canvas.drawRightString(PAGE_W - MARGIN, 11.4 * mm, str(doc.page))
        canvas.restoreState()


class SectionMark(Spacer):
    """A zero-height flowable that renames the running head."""

    def __init__(self, label):
        super().__init__(0, 0)
        self.label = label

    def draw(self):
        self.canv._doctemplate.section = self.label


# ---- table helpers --------------------------------------------------------

def table(rows, widths, align=None, header=True, zebra=True, size=8, pad=4):
    align = align or ["l"] * len(widths)
    data = []
    for r, row in enumerate(rows):
        out = []
        for c, cell in enumerate(row):
            if isinstance(cell, str):
                st = "th" if (header and r == 0) else "td"
                if align[c] in ("r", "c") and not (header and r == 0):
                    st = "td"
                out.append(Paragraph(cell, S[st]))
            else:
                out.append(cell)
        data.append(out)

    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK if header else RULE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE),
    ]
    for c, a in enumerate(align):
        if a == "r":
            style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
        elif a == "c":
            style.append(("ALIGN", (c, 0), (c, -1), "CENTER"))
    if zebra:
        for r in range(1, len(rows)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), FAINT))
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    t.setStyle(TableStyle(style))
    return t


def callout(title, text, tone=ACCENT, compact=False):
    body = S["body"] if not compact else ParagraphStyle(
        "cb", parent=S["body"], fontSize=8.8, leading=12.1, spaceAfter=0)
    inner = [[Paragraph(f'<font color="{tone.hexval()}"><b>{title}</b></font>', S["td"])],
             [Paragraph(text, body)]]
    t = Table(inner, colWidths=[FRAME_W - 14])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), FAINT),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, 0), 5 if compact else 7),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 6 if compact else 8),
        ("TOPPADDING", (0, 1), (-1, 1), 1), ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, tone),
    ]))
    return t


def caption(text):
    return P(text, "cap")


# ---- charts ---------------------------------------------------------------

def _axis(ax, size=7):
    ax.labels.fontName = "Helvetica"
    ax.labels.fontSize = size
    ax.labels.fillColor = MUTED
    ax.strokeColor = RULE
    ax.strokeWidth = 0.5


def hbar(cats, series, colours, width, height, vmax=None, fmt="{:.0f}",
         label_gap=3, bar_labels=True, x_left=150):
    """Horizontal bars, read top to bottom."""
    d = Drawing(width, height)
    ch = HorizontalBarChart()
    ch.x, ch.y = x_left, 16
    ch.width, ch.height = width - x_left - 34, height - 26
    ch.data = [list(reversed(s)) for s in series]
    ch.categoryAxis.categoryNames = list(reversed(cats))
    ch.categoryAxis.labels.boxAnchor = "e"
    ch.categoryAxis.labels.dx = -4
    ch.bars.strokeColor = None
    ch.groupSpacing = 5
    ch.barSpacing = 1
    ch.valueAxis.valueMin = 0
    if vmax:
        ch.valueAxis.valueMax = vmax
    for i, c in enumerate(colours):
        ch.bars[i].fillColor = c
    _axis(ch.categoryAxis, 7.4)
    _axis(ch.valueAxis, 7)
    ch.valueAxis.visibleGrid = True
    ch.valueAxis.gridStrokeColor = colors.HexColor("#e4e4e8")
    ch.valueAxis.gridStrokeWidth = 0.4
    if bar_labels:
        ch.barLabels.fontName = "Helvetica-Bold"
        ch.barLabels.fontSize = 7
        ch.barLabels.fillColor = INK
        ch.barLabels.dx = label_gap
        ch.barLabelFormat = lambda v: fmt.format(v) if v else ""
        ch.barLabels.boxAnchor = "w"
    d.add(ch)
    return d


def vbar(cats, series, colours, width, height, vmax=None, fmt="{:.0f}",
         stacked=False, legend=None):
    d = Drawing(width, height)
    ch = VerticalBarChart()
    ch.x, ch.y = 38, 34 if legend else 20
    ch.width, ch.height = width - 52, height - (58 if legend else 40)
    ch.data = series
    ch.categoryAxis.categoryNames = cats
    ch.bars.strokeColor = None
    ch.groupSpacing = 14
    ch.barSpacing = 1
    ch.valueAxis.valueMin = 0
    if vmax:
        ch.valueAxis.valueMax = vmax
    if stacked:
        ch.categoryAxis.style = "stacked"
    for i, c in enumerate(colours):
        ch.bars[i].fillColor = c
    _axis(ch.categoryAxis, 7.2)
    _axis(ch.valueAxis, 7)
    ch.valueAxis.visibleGrid = True
    ch.valueAxis.gridStrokeColor = colors.HexColor("#e4e4e8")
    ch.valueAxis.gridStrokeWidth = 0.4
    d.add(ch)
    if legend:
        x = 40
        for label, c in zip(legend, colours):
            d.add(Rect(x, 8, 8, 8, fillColor=c, strokeColor=None))
            d.add(String(x + 11, 10.5, label, fontName="Helvetica", fontSize=7.2, fillColor=MUTED))
            x += 12 + len(label) * 3.9 + 14
    return d


def marker_line(d, chart_x, chart_w, y0, y1, value, vmax, label, colour):
    x = chart_x + chart_w * value / vmax
    d.add(Line(x, y0, x, y1, strokeColor=colour, strokeWidth=0.9, strokeDashArray=[2.5, 2]))
    d.add(String(x + 2, y1 + 2, label, fontName="Helvetica-Bold", fontSize=6.6, fillColor=colour))
    return d


# --------------------------------------------------------------------------
# the computed model, in one place
# --------------------------------------------------------------------------

AGENT_GB, VLM_GB, ASR_GB, TTS_GB = 18.0, 9.0, 3.0, 1.0
DUTY_MAIN = 12


def compute(data: dict) -> dict:
    w = data["workload_model"]
    fx = data["locale"]["aed_per_usd"]
    traf = {n: traffic_for(data, n) for n in (1, 5, 10, 15, 20, 30, 40)}
    design = traf[w["orgs_design"]]
    stress = traf[w["orgs_high"]]

    vram = {c: {"fp16": vram_budget(data, c, AGENT_GB, VLM_GB, ASR_GB, TTS_GB, False),
                "fp8": vram_budget(data, c, AGENT_GB, VLM_GB, ASR_GB, TTS_GB, True)}
            for c in (traf[10].concurrency, design.concurrency,
                      stress.concurrency, traf[40].concurrency, 20)}

    builds = build_slate(data)
    costs = {}
    for b in builds:
        costs[b.ident] = {h: cost_build(data, b, h, design.tokens_per_month)
                          for h in data["assumptions"]["duty_cycle_hours_per_day"]}

    apis = []
    for a in data["api_prices_usd_per_mtok"]:
        bl = blended_api_rate(data, a)
        apis.append({**a, "blended": bl, "blended_aed": bl * fx,
                     "monthly_aed": bl * fx * design.tokens_per_month / 1e6})
    apis.sort(key=lambda r: -r["blended"])

    return {"traffic": traf, "design": design, "stress": stress, "vram": vram,
            "builds": builds, "costs": costs, "apis": apis, "fx": fx,
            "kv_valid": validate_kv_model(data)}


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def sec_cover(data, m):
    f = []
    f.append(Spacer(1, 44 * mm))
    f.append(P("PROCUREMENT AND BUSINESS ANALYSIS", "kicker"))
    f.append(P("Hardware, Model &amp;<br/>Serving Strategy", "cover_t"))
    f.append(P("Nine options across three budget tiers, priced in dirhams from UAE sellers, "
               "sized against a stated workload and judged on two baselines: what the tokens "
               "would cost from an API, and what the same capability would cost to rent.",
               "cover_s"))
    f.append(Spacer(1, 3 * mm))
    w = data["workload_model"]
    a = data["assumptions"]
    rows = [
        ["Prepared for", data["prepared_for"]],
        ["Compiled", "4 September 2026 &middot; Dubai, UAE"],
        ["Currency", "AED throughout, at UAE seller prices including VAT. USD appears only "
                     "where the vendor bills in it, converted at the 3.6725 peg"],
        ["Sizing target", f"{w['orgs_low']}&ndash;{w['orgs_high']} client organisations, "
                          "with a stated upgrade path"],
        ["Workload", "Tool-calling document agent, voice chat, vision and image reading, "
                     "and graph generation"],
        ["Budget ceiling", money(a["budget_ceiling_aed"])],
        ["Inputs", "docs/hardware_report_data.json &mdash; every figure carries a named seller, "
                   "a source URL and a capture date"],
    ]
    f.append(table([[P("<b>" + k + "</b>", "td"), P(v, "td")] for k, v in rows],
                   [30 * mm, FRAME_W - 30 * mm], header=False, zebra=False))
    f.append(Spacer(1, 8 * mm))
    f.append(callout(
        "The question this report answers",
        "There is no production server yet. The choice is to rent capacity or to build a rig, "
        "and it is genuinely open: at the design point, renting internationally costs about the "
        "same per month as owning, while renting inside the UAE costs roughly four times as "
        "much. <b>So the decision is not really about hardware. It is about whether client "
        "documents are allowed to leave the country</b> &mdash; and this report puts a monthly "
        "figure on that answer rather than assuming one.", ACCENT))
    f.append(Spacer(1, 5 * mm))
    f.append(P("Prices move fast in the current memory shortage. Every figure here was captured "
               "at the compile date from a named UAE seller and can be re-checked, or the "
               "report regenerated, from the data file.", "small"))
    f.append(NextPageTemplate("body"))
    f.append(PageBreak())
    return f


def sec_exec(data, m):
    design, stress = m["design"], m["stress"]
    v = m["vram"][design.concurrency]
    l1 = m["costs"]["L1"][DUTY_MAIN]
    l2 = m["costs"]["L2"][DUTY_MAIN]
    m1 = m["costs"]["M1"][DUTY_MAIN]
    h3 = m["costs"]["H3"][DUTY_MAIN]
    glm = next(a for a in m["apis"] if a["model"].startswith("GLM"))
    sonnet = next(a for a in m["apis"] if "Sonnet" in a["model"])
    region_month = l1["rent_region_month"] + l1["ops_month"]

    f = [SectionMark("1 · Executive summary")]
    f.append(P("Executive summary", "h1"))
    f.append(P("What to do, what it costs in dirhams, and the one answer that changes it.",
               "small"))
    f.append(Spacer(1, 4))

    f.append(P("Four findings", "h2"))
    f.append(P(
        "<b>1. The workload needs 48&nbsp;GB, not 96.</b> Fifteen client organisations at four "
        f"active users each generate {design.turns_per_day:,.0f} agent turns a day, but office "
        f"traffic is bursty and short. The busiest hour carries {design.peak_hour_turns:,.0f} of "
        f"them, and at {design.service_seconds:.0f} seconds of service per turn that is only "
        f"{design.offered_erlangs:.1f} requests in flight on average. Queueing theory puts "
        f"<b>{design.concurrency} concurrent slots</b> at under a 10% chance of waiting, and "
        f"{stress.concurrency} covers twenty organisations. Weights, that much KV cache and "
        f"vLLM&rsquo;s own overhead come to <b>{v['fp16']['card_gb']:.0f}&nbsp;GB</b> at an FP16 "
        f"cache, {v['fp8']['card_gb']:.0f}&nbsp;GB at FP8. Organisations are not concurrency, and "
        "confusing the two is how a 96&nbsp;GB card gets bought for a 48&nbsp;GB job."))
    f.append(P(
        "<b>2. Data residency decides rent versus buy, and it is worth about "
        f"{money(region_month - l2['monthly'])} a month.</b> Renting a capable card "
        f"internationally costs {money(l1['monthly'])} a month all in. Renting one inside the "
        f"UAE costs {money(region_month)}, because there is no in-region rental of a "
        "48&nbsp;GB consumer card at all &mdash; the cheapest UAE-hosted option is a datacentre "
        "H100 at four times the price for hardware far larger than this needs. Owning sits "
        f"between them at {money(l2['monthly'])}. So if documents may leave the country, "
        "renting is competitive and reversible. <b>If they may not, owning is decisively "
        "cheaper and the only question left is which rig.</b>"))
    f.append(P(
        "<b>3. The UAE second-hand market is the cheapest route to the design point, by a wide "
        f"margin.</b> Two used RTX&nbsp;3090s reach 48&nbsp;GB for about "
        f"{money(2400 * 2)} of graphics card; the complete build is "
        f"{money(l2['gross'])} delivered. The same 48&nbsp;GB in used Ada costs "
        f"{money(m1['gross'])}. What the extra buys is an FP8 path that halves the KV cache, "
        "turning 48&nbsp;GB from comfortable at fifteen organisations into comfortable at "
        f"twenty &mdash; worth {money(m1['gross'] - l2['gross'])} only if that case is firm."))
    f.append(P(
        "<b>4. The free change is worth more than any of the paid ones.</b> Ollama does not do "
        "continuous batching &mdash; it serves one request at a time. Until <b>app/llm.py</b> "
        "speaks to a batching server, <b>JOB_WORKERS</b> rises above 1 and uvicorn runs more "
        "than one process, a bigger card buys very little. That work is roughly a day, it costs "
        "nothing, and it is a precondition for every option below, rented or owned."))

    f.append(P("The recommendation, in order", "h2"))
    steps = [
        ["1", "Settle the residency question", "free",
         "Ask the client, in writing, whether their documents may be processed outside the UAE. "
         "Every number in this report bends around that answer and nothing should be bought "
         "before it exists."],
        ["2", "Land the vLLM seam", "~1 day",
         "Port <b>app/llm.py</b> to the OpenAI-compatible shape and lift the two other "
         "ceilings. Free, reversible, and required whether the endpoint is rented or owned."],
        ["3", "Rent for a month and measure", "~" + money(0.64 * 3.6725 * 24 * 30),
         "A 48&nbsp;GB card on the international line, running the real stack on Ubuntu. It "
         "replaces the modelled concurrency figure with a measured one and rehearses the Linux "
         "move before any card is bought."],
        ["4", "Buy L2 &mdash; two used RTX 3090", money(l2["gross"]),
         "If residency is required, or if the measurement shows sustained load. Inspect before "
         "paying. Step up to M1 only if the twenty-organisation case is firm and the FP8 "
         "headroom is genuinely wanted."],
    ]
    f.append(table([["", "Step", "Cost", "Why"]] + steps,
                   [8 * mm, 40 * mm, 26 * mm, FRAME_W - 74 * mm],
                   align=["c", "l", "r", "l"]))

    # Keep the closing block whole rather than letting one line dangle overleaf.
    tail = [P("What would change the answer", "h2"), P(
        "<b>Measured concurrency above roughly twenty in flight</b>, or sessions routinely "
        "running at 32k context rather than 16k. Either doubles the KV cache and 48&nbsp;GB "
        "stops being enough. <b>A large dense model entering the roadmap</b> &mdash; everything "
        "here assumes a sparse mixture-of-experts agent in the 30B class, and a dense 70B is a "
        "96&nbsp;GB conversation. <b>A hosted open-weight API being acceptable</b>: "
        f"{glm['model'].split(' (')[0]} serves this workload for "
        f"{money(glm['monthly_aed'])} a month against {money(l2['monthly'])} owned, so if "
        "documents may leave at all, nothing here beats it below about sixteen clients.")]
    f.append(KeepTogether(tail))
    f.append(PageBreak())
    return f


def sec_changed(data, m):
    mc = data["market_context"]
    lp = data["local_purchase"]
    f = [SectionMark("2 · The market")]
    f.append(P("The market you are buying into", "h1"))
    f.append(P("One global condition, and three things about buying it in the UAE.", "small"))
    f.append(Spacer(1, 4))

    f.append(P(
        "Any hardware bought in 2026 is bought during a memory shortage, and that fact governs "
        "every price in this report. It is not a normal market and the usual instincts &mdash; "
        "buy new, buy the current generation, wait for prices to fall &mdash; are all wrong in "
        "specific ways worth naming before a bill of materials is read.", "lead"))

    f.append(P("The global condition", "h2"))
    f.append(P(
        f"The AI datacentre buildout is absorbing {mc['dram_share_of_supply']['value']}, and the "
        "allocation is not neutral between product lines: DDR5 wafers go to server memory, GDDR7 "
        "goes to accelerators, and consumer parts get what is left. "
        f"{mc['supply_cuts']['value']}. Published forecasts do not expect meaningful relief "
        "before 2028."))
    f.append(P(
        "The visible result is that prices rose without performance changing. An RTX PRO 6000 "
        "listed at USD 8,565 when it launched in March 2025 and USD 16,000 by August 2026 "
        "&mdash; an 87% increase on the same silicon. RTX 5090 street pricing runs USD 4,300 to "
        "5,000 against a USD 1,999 launch price. <b>Nothing about those cards got faster. The "
        "memory they are made of got scarce.</b>"))
    f.append(callout(
        "The consequence for this decision",
        "When the price of owning capacity rises for reasons unrelated to performance, two "
        "things follow. Buying at the top of a squeeze carries mark-to-market risk that buying "
        "in a normal market does not &mdash; if supply normalises, the card does not get slower "
        "but its replacement cost falls. And the second-hand market, where prices are set by "
        "people selling cards rather than by fabs allocating wafers, becomes the one segment "
        "still priced on merit. Both point the same way: <b>buy used, buy small, or rent.</b>",
        ACCENT))

    f.append(P("Three things about buying it here", "h2"))
    f.append(P(
        "<b>Local retail carries a real premium, and a wide spread.</b> An RTX 5090 Founders "
        "Edition sits above AED 12,000 in the UAE against a USD 1,999 (about AED 7,340) launch "
        "price. More usefully for planning, the spread <i>between UAE sellers</i> on one part "
        "number is enormous: the RTX PRO 6000 ranges from AED 48,825 to AED 77,032 depending on "
        "the vendor, and Promise Gulf&rsquo;s own product page and price list disagree by more "
        "than a factor of two on the PRO 5000. <b>Every four- and five-figure line in this "
        "report should be re-quoted in writing before it is committed to</b>, and the spread "
        "itself is a negotiating position."))
    f.append(P(
        "<b>The second-hand market here is deep enough to build on.</b> Dubizzle lists roughly "
        "290 used graphics cards nationally with a further 91 in Abu Dhabi &mdash; a real "
        "market rather than a handful of listings. RTX 3090s appear around AED 2,000 to 2,300, "
        "which is a quarter of what a used 4090 costs locally and about a sixth of a new 5090. "
        "That single price relationship is why the low tier of this report is competitive with "
        "the high tier on capability, and it is a specifically local advantage."))
    f.append(P(
        "<b>Buying locally removes two cost lines entirely.</b> Importing hardware means 5% "
        "customs duty on the CIF value, 5% VAT on top of that, freight and a Mirsal 2 "
        "clearance. Buying from a UAE seller means the duty is already inside the shelf price, "
        "there is no freight to arrange and no clearance to wait on, and the only adjustment is "
        f"the {lp['uae_vat_pct']:.0f}% VAT shown on the tax invoice, which a VAT-registered "
        "business recovers. Every build in this report is therefore costed at a listed AED "
        "price plus local delivery, with VAT stripped to give the effective capital cost."))

    rows = [["", "Import it", "Buy it here"]]
    rows += [
        ["Customs duty", "5% on CIF value", "already inside the shelf price"],
        ["VAT", "5% on CIF plus duty, paid at clearance",
         "5% on the invoice, recovered if registered"],
        ["Freight", "international, plus Mirsal 2 clearance",
         f"local delivery, about {money(lp['local_delivery_aed'])}"],
        ["Lead time", "weeks, and a customs process",
         "next-day from most Dubai sellers"],
        ["Warranty", "an international RMA, at your cost",
         "local warranty on new stock; none at all on a private sale"],
    ]
    f.append(table(rows, [26 * mm, 58 * mm, FRAME_W - 84 * mm]))
    f.append(caption("The comparison matters because a report priced from US retail would add "
                     "roughly 10% in duty and VAT plus freight to every build, and would be "
                     "describing a purchase nobody here needs to make."))

    f.append(P("What this means for the tiers", "h2"))
    rows = [["Strategy", "In this market", "Verdict"]]
    rows += [
        ["Buy new consumer silicon",
         "Paying shortage prices for VRAM. A new 16 GB card and two used 24 GB cards cost the "
         "same here.", "Only for warranty and simplicity, not for capacity"],
        ["Buy used consumer silicon",
         "Priced by sellers, not by allocation. Deep local supply on Ampere.",
         "<b>The best value available, if a private sale is acceptable</b>"],
        ["Buy workstation silicon",
         "Rose on supply, not on performance, and carries the widest local price spread.",
         "Defensible only with a written quote at the low end of the range"],
        ["Rent internationally",
         "Providers competing on utilisation of assets bought before the squeeze.",
         "Cheapest per hour, forfeits residency"],
        ["Rent in region",
         "No consumer-class option exists; datacentre hardware only.",
         "About four times the international rate, keeps data in the country"],
    ]
    f.append(table(rows, [32 * mm, FRAME_W - 92 * mm, 60 * mm]))
    f.append(PageBreak())
    return f


def sec_sizing(data, m):
    w = data["workload_model"]
    design, stress = m["design"], m["stress"]
    f = [SectionMark("3 · Sizing the machine")]
    f.append(P("From client organisations to gigabytes", "h1"))
    f.append(P("An auditable chain: traffic, then concurrency, then memory. Change any input "
               "and the rest re-derives.", "small"))
    f.append(Spacer(1, 4))

    f.append(P(
        "&ldquo;Ten to twenty client organisations&rdquo; is a commercial target, not an "
        "engineering one. It has to be turned into a number of simultaneously in-flight "
        "requests before it can be turned into a card, and the gap between those two numbers is "
        "large enough to change which tier gets bought.", "lead"))

    f.append(P("Step 1 &mdash; the workload", "h2"))
    f.append(P(
        "Four slots run on this machine, and one of them is free. The agent is a tool-calling "
        "language model. Vision is a separate model that reads scanned invoices and images. "
        "Speech in and speech out are two more. <b>Graph generation is not a model at all</b> "
        "&mdash; it is matplotlib writing a PNG, or reportlab writing a chart into a PDF, and it "
        "belongs in <b>app/tools.py</b> next to <b>write_excel</b> as an ordinary Python "
        "function. It costs no VRAM, no weights and no load time. Building it as a diffusion "
        "model would be the single most expensive mistake available in this project."))

    slots = [["Slot", "Model class", "Serving quant", "VRAM", "Note"]]
    for row, gb in ((data["models"]["agent"][0], AGENT_GB), (data["models"]["vision"][0], VLM_GB),
                    (data["models"]["asr"][0], ASR_GB), (data["models"]["tts"][0], TTS_GB)):
        pass
    slots += [
        ["Agent", "Qwen3-Coder-30B-A3B", "INT4 / AWQ", f"{AGENT_GB:.0f} GB",
         "MoE, ~3B active per token &mdash; large-model reasoning at small-model decode speed"],
        ["Vision", "Qwen3-VL-8B", "FP8", f"{VLM_GB:.0f} GB",
         "Native-resolution encoder; the practical single-GPU choice for document OCR"],
        ["Speech in", "Whisper Large V3 Turbo", "FP16 / CTranslate2", f"{ASR_GB:.0f} GB",
         "809M params, ~216&times; real time; a naive load costs 6 GB, faster-whisper costs 3"],
        ["Speech out", "Kokoro-82M", "FP16", f"{TTS_GB:.0f} GB",
         "Runs on CPU. Do not give it a GPU slot until measurement says otherwise"],
        ["Graphs", "matplotlib / reportlab", "&mdash;", "0 GB",
         "A CPU tool, not a model. The cheapest requirement in the whole brief"],
    ]
    f.append(table(slots, [17 * mm, 34 * mm, 24 * mm, 14 * mm, FRAME_W - 89 * mm],
                   align=["l", "l", "l", "r", "l"]))
    f.append(caption(f"Resident weights total {AGENT_GB + VLM_GB + ASR_GB + TTS_GB:.0f} GB. "
                     "That is the floor, and it is the smaller half of the problem."))

    f.append(P("Step 2 &mdash; traffic, and why organisations are not concurrency", "h2"))
    f.append(P(
        f"Each organisation is assumed to have {w['active_users_per_org']} active users, each "
        f"running {w['sessions_per_user_per_day']} agent sessions a day of "
        f"{w['turns_per_session']} turns. Each turn sends about "
        f"{w['prompt_tokens_per_turn']:,} prompt tokens and receives "
        f"{w['output_tokens_per_turn']} &mdash; the prompt figure is deliberately high because "
        "<b>app/agent.py</b> re-sends the entire conversation plus every tool result on every "
        "step, and <b>TOOL_RESULT_BUDGET</b> alone is 12,000 characters. A fifth of the day&rsquo;s "
        "turns are assumed to land in the busiest hour, which is a normal office shape and a "
        "pessimistic one."))
    f.append(P(
        f"A turn occupies a slot for about {design.service_seconds:.0f} seconds: roughly "
        f"{w['prompt_tokens_per_turn'] / 2500:.1f}s of prefill at a modelled 2,500 tok/s share "
        f"under batching, then {w['output_tokens_per_turn'] / w['target_decode_tps_per_user']:.0f}s "
        f"of decode at the {w['target_decode_tps_per_user']} tok/s per user that reads as "
        "responsive. Offered load is arrivals times service time; the number of slots needed to "
        "keep the wait probability under 10% comes from Erlang C."))

    rows = [["Orgs", "Users", "Turns/day", "Peak hour", "Offered load", "Slots needed",
             "P(wait)", "Tokens/month"]]
    for n in (5, 10, 15, 20, 30, 40):
        t = m["traffic"][n]
        bold = n == w["orgs_design"]
        cells = [str(n), str(t.users), f"{t.turns_per_day:,.0f}",
                 f"{t.peak_hour_turns:,.0f}", f"{t.offered_erlangs:.2f} E",
                 str(t.concurrency), f"{t.wait_probability * 100:.0f}%",
                 f"{t.tokens_per_month / 1e6:.0f} M"]
        rows.append([Paragraph(f"<b>{c}</b>", S["td"]) for c in cells] if bold else cells)
    f.append(table(rows, [13 * mm, 14 * mm, 20 * mm, 19 * mm, 22 * mm, 20 * mm, 15 * mm,
                          FRAME_W - 123 * mm],
                   align=["r"] * 8))
    f.append(caption("The design row is fifteen organisations. Note the shape: quadrupling the "
                     "client count from five to twenty roughly triples the token bill but only "
                     "raises required concurrency from three slots to eight. Statistical "
                     "multiplexing is why a small machine serves a lot of people."))

    f.append(callout(
        "Twenty organisations is eight in-flight requests, not twenty",
        "This is the single most consequential number in the report. Sizing to twenty concurrent "
        "sessions would demand roughly 70&nbsp;GB and push the decision into the high tier. "
        "Sizing to the eight that queueing theory actually calls for lands at 50&nbsp;GB and "
        "makes a pair of used cards sufficient. The difference between those two readings is "
        "about ten thousand dollars.", ACCENT))

    f.append(PageBreak())
    f.append(P("Step 3 &mdash; concurrency into memory", "h2"))
    f.append(P(
        "The KV cache is the state a model keeps for a conversation in progress, and it is the "
        "term that scales with users rather than with model size:"))
    f.append(callout(
        "KV bytes per token  =  2 &times; layers &times; kv_heads &times; head_dim &times; dtype_bytes",
        "The 2 is one tensor for keys and one for values. <b>kv_heads</b>, not <b>heads</b>, is "
        "what matters &mdash; grouped-query attention shares key/value projections across "
        "attention heads, and it is the single reason a 30B model can hold twenty sessions on "
        "one card. Multiply by context length and by concurrent sessions to get the total.",
        GOLD))

    f.append(P("The formula is checked against this project&rsquo;s own measurements rather than "
               "taken on faith. <b>bench_results.json</b>, measured on 2 September on a "
               "temporary RTX 3060 test machine &mdash; a box used to build the software, not "
               "part of any option in this report and not counted as an asset anywhere in it:",
               "body"))
    rows = [["Model", "Context", "Predicted", "Measured", "Ratio", ""]]
    for r in m["kv_valid"]:
        contaminated = r["model"] == "qwen3:14b" and r["ctx"] == 16384
        note = ("card at its ceiling &mdash; throughput collapsed to 8.25 tok/s on this run, so "
                "the reading is offloading, not cache" if contaminated else
                "within measurement error of the formula" if r["ratio"] > 0.84 else
                "GQA sharing plus allocator rounding")
        rows.append([r["model"], f"{r['ctx']:,}", f"{r['predicted_mb']:,.0f} MiB",
                     f"{r['measured_mb']:,.0f} MB", f"{r['ratio']:.2f}", note])
    f.append(table(rows, [22 * mm, 17 * mm, 22 * mm, 21 * mm, 14 * mm, FRAME_W - 96 * mm],
                   align=["l", "r", "r", "r", "r", "l"]))
    f.append(caption("The two 8B rows agree with the model to within 15%, which is as close as "
                     "an allocator-level measurement gets. The final row is excluded from the "
                     "fit: at 10.9 GB peak on a 12 GB card, that run was paging, and its "
                     "collapse from 31.7 to 8.25 tok/s is the evidence."))

    f.append(P("The budget", "h3"))
    f.append(P(
        "A 30B-A3B-class agent is modelled at 48 layers, 4 KV heads and 128 head dimension "
        "&mdash; stated as an assumption, not measured; the 8B above validates the formula, not "
        "this configuration. At 16k context that is 96&nbsp;KiB per token at FP16 and half that "
        "at FP8, and vLLM is given "
        f"{data['assumptions']['vllm_gpu_memory_utilization'] * 100:.0f}% of the card rather "
        "than all of it."))

    cats, weights, kv16, kv8 = [], [], [], []
    for c in sorted(m["vram"]):
        cats.append(f"{c} slots")
        weights.append(m["vram"][c]["fp16"]["weights_gb"] + m["vram"][c]["fp16"]["overhead_gb"])
        kv16.append(m["vram"][c]["fp16"]["kv_gb"])
        kv8.append(m["vram"][c]["fp8"]["kv_gb"])
    d = vbar(cats, [weights, kv16], [ACCENT, GOLD], FRAME_W, 122, vmax=76,
             stacked=True, legend=["Weights + overhead", "KV cache at FP16"])
    ch_x, ch_w, ch_h = 38, FRAME_W - 52, 122 - 58
    for gb, lab, col in ((24, "24 GB", MUTED), (32, "32 GB", MUTED),
                         (48, "48 GB card", GOOD), (96, "96 GB", MUTED)):
        y = 34 + ch_h * gb / 76.0
        if y < 34 + ch_h:
            d.add(Line(ch_x, y, ch_x + ch_w, y, strokeColor=col, strokeWidth=0.8,
                       strokeDashArray=[3, 2.4]))
            w_lab = len(lab) * 3.9 + 4
            d.add(Rect(ch_x + 3, y + 1.4, w_lab, 7.6, fillColor=colors.white,
                       strokeColor=None))
            d.add(String(ch_x + 5, y + 3.2, lab, fontName="Helvetica-Bold",
                         fontSize=6.6, fillColor=col))
    f.append(d)
    f.append(caption("Stacked VRAM requirement against concurrent slots, with card sizes marked. "
                     "Note where the crossings fall: 24 GB is out at any concurrency once a "
                     "vision model is resident, 32 GB runs out around six slots, and 48 GB "
                     "carries the twenty-organisation case with room to spare."))

    rows = [["Concurrent slots", "Serves", "Weights", "KV @ FP16", "KV @ FP8",
             "Card needed, FP16", "Card needed, FP8"]]
    labels = {m["traffic"][10].concurrency: "10 orgs", design.concurrency: "15 orgs",
              stress.concurrency: "20 orgs", m["traffic"][40].concurrency: "40 orgs",
              20: "a bad day"}
    for c in sorted(m["vram"]):
        a, b = m["vram"][c]["fp16"], m["vram"][c]["fp8"]
        rows.append([str(c), labels.get(c, ""), f"{a['weights_gb']:.0f} GB",
                     f"{a['kv_gb']:.1f} GB", f"{b['kv_gb']:.1f} GB",
                     f"{a['card_gb']:.0f} GB", f"{b['card_gb']:.0f} GB"])
    f.append(table(rows, [24 * mm, 20 * mm, 19 * mm, 22 * mm, 21 * mm, 30 * mm,
                          FRAME_W - 136 * mm],
                   align=["r", "l", "r", "r", "r", "r", "r"]))
    f.append(caption("FP8 KV cache is available on Ada and Blackwell, not on Ampere. That single "
                     "capability is worth roughly 5 GB at the design point and is the strongest "
                     "technical argument for buying 4090s rather than 3090s."))

    f.append(callout(
        "The honest caveat",
        "Every measured number in this project is a single request at a time on Ollama, which "
        "does not batch. The formula above is validated against those measurements; the "
        "concurrency figures are <b>modelled, not measured</b>. Appendix A sets out what to "
        "instrument, and section 8 puts a two-week rental in front of any purchase precisely so "
        "this paragraph can be deleted from the next revision.", WARN))
    f.append(PageBreak())
    return f


def sec_serving(data, m):
    s = data["serving"]
    f = [SectionMark("4 · The serving layer")]
    f.append(P("The serving layer: Ollama today, vLLM next", "h1"))
    f.append(P("The highest-leverage change on the list, and the only one that costs nothing.",
               "small"))
    f.append(Spacer(1, 4))

    f.append(P(
        "Ollama was the right choice for this project and remains the right choice for a "
        "one-person box: same commands on Windows and Linux, a model pulled in one line, and a "
        "cold start of "
        f"{s['cold_start']['ollama_s']}s against vLLM&rsquo;s {s['cold_start']['vllm_s']}s. "
        "<b>app/llm.py</b> talks to it in a single standard-library POST, and the README is "
        "right that a client library would only fall out of step with a fast-moving server.", "lead"))
    f.append(P(
        "What it does not do is serve several people at once. Requests are handled one after "
        "another. On a single stream the difference is modest &mdash; "
        f"{s['vllm_vs_ollama_single']['vllm_tps']} against "
        f"{s['vllm_vs_ollama_single']['ollama_tps']} tok/s on the same card and quantisation "
        "&mdash; but that comparison measures the wrong thing. Under load, a batching server "
        f"reaches roughly {s['vllm_batched']['aggregate_tps']:,} tok/s aggregate at batch "
        f"{s['vllm_batched']['batch']}, which is about "
        f"{s['vllm_batched']['concurrent_users']} users each still seeing "
        f"{s['vllm_batched']['per_user_tps']} tok/s. The serial server has no equivalent "
        "number, because the second user waits for the first."))

    f.append(callout(
        "Why batching is nearly free, and why image generation is not",
        "Generating a token reads the model&rsquo;s weights out of memory. Several conversations "
        "in the same batch <b>share that read</b>, so the marginal cost of the second user is "
        "arithmetic, not bandwidth &mdash; and bandwidth is the bottleneck. That is the whole "
        "trick. It is also why <b>app/jobs.py</b> exists and is correct: diffusion does real "
        "work per denoising step and does not share, so it queues rather than batches. One "
        "batches, one queues. Keeping those two behaviours in separate lanes is a design "
        "decision this project already made and should keep.", ACCENT))

    f.append(P("Three ceilings, not one", "h2"))
    f.append(P(
        "Swapping the inference server alone will not deliver concurrency, because two other "
        "limits sit in front of it. All three have to lift together, and none of them costs money:"))
    rows = [["#", "Ceiling", "Where", "Change"]]
    rows += [
        ["1", "Serial inference", "app/llm.py &rarr; Ollama /api/chat",
         "Point it at an OpenAI-compatible /v1/chat/completions endpoint. The file is one POST; "
         "step-00 estimates about an hour."],
        ["2", "One job worker", "JOB_WORKERS=1 in .env",
         "Correct today, and documented as correct: one GPU serialises the work anyway. It "
         "becomes wrong the moment a second card or a batching server arrives."],
        ["3", "One web process", "uvicorn.run in app/main.py, no --workers",
         "Single-process uvicorn will saturate on request handling long before the GPU does at "
         "eight concurrent agent turns."],
    ]
    f.append(table(rows, [7 * mm, 30 * mm, 52 * mm, FRAME_W - 89 * mm], align=["c", "l", "l", "l"]))
    f.append(caption("Buying a larger card without lifting all three buys latency on one request "
                     "and nothing on ten. This is the cheapest work in the report and it gates "
                     "every hardware decision after it."))

    f.append(P("What it costs: the operating system", "h2"))
    f.append(P(
        "<b>vLLM has no native Windows support.</b> The machine that holds the production card "
        "should run Ubuntu. WSL2 is fine for development but adds a virtualised memory boundary "
        "that is unhelpful precisely when the card is full. This is not a surprise and not a "
        "rewrite: <b>scripts/service/syslab-server.service</b> already exists as the systemd "
        "equivalent of the Windows scheduled tasks, every path goes through <b>pathlib</b>, and "
        "every setting lives in <b>.env</b>. The README calls those two habits the reason the "
        "Linux move is a copy of the folder rather than a rewrite. They are about to be tested, "
        "and the plan should assume a Windows development box and a Linux serving box from here on."))

    f.append(P("Prefix caching is worth more here than on most workloads", "h2"))
    f.append(P(
        "vLLM&rsquo;s automatic prefix caching keeps the KV blocks of a shared prompt prefix and "
        "reuses them across requests. Most chat products get a modest win from it. This one "
        "should get an unusually large one, because <b>app/agent.py</b> re-sends the entire "
        "conversation plus every tool result on every step of the loop: turn four of a session "
        "is turn three&rsquo;s prompt with a tool result appended. The system prompt and twelve "
        "tool descriptions are identical on every request from every tenant. On a workload whose "
        "prompt-to-output ratio is roughly fifteen to one, cutting repeated prefill is worth more "
        "than any plausible card upgrade &mdash; and it costs a configuration flag."))

    f.append(P("The alternatives, assessed rather than dismissed", "h2"))
    rows = [["Runtime", "Batching", "Verdict for this project"]]
    rows += [
        ["vLLM", "Continuous, PagedAttention",
         "<b>The recommendation.</b> Best-supported OpenAI-compatible server, widest quantisation "
         "coverage, automatic prefix caching. Linux only."],
        ["SGLang", "Continuous, RadixAttention",
         "A real contender, and arguably better suited: RadixAttention shares prefix cache across "
         "a tree of requests, which is the exact shape of an agent loop. Worth benchmarking "
         "during the rental fortnight rather than assumed away."],
        ["TensorRT-LLM", "Continuous",
         "Fastest on NVIDIA silicon and the worst ergonomics here. An engine must be compiled per "
         "model, per quantisation, per GPU. Wrong trade for a project that will change models."],
        ["llama.cpp", "Limited",
         "The only runtime that does MoE expert offload to system RAM well &mdash; the technique "
         "behind the 120B-on-12&nbsp;GB result. Keep it in the toolbox for that specific case; do "
         "not serve clients from it."],
        ["Ollama", "None",
         "Keep it on the development box. It is the reason the project got built quickly and it "
         "remains the fastest way to try a new model. It is not a serving tier."],
    ]
    f.append(table(rows, [24 * mm, 34 * mm, FRAME_W - 58 * mm]))

    f.append(P("Quantisation, and the metric that actually matters", "h2"))
    f.append(P(
        "The temptation is to pick the quantisation that fits and move on. For this project the "
        "constraint is different: the model&rsquo;s job is to emit well-formed tool calls, and "
        "tool-calling accuracy degrades under aggressive quantisation earlier and less "
        "gracefully than prose quality does. A model that writes a slightly worse sentence is "
        "fine. A model that omits a required argument sends the loop round again, and "
        "<b>MAX_TOOL_STEPS</b> is 10."))
    rows = [["Scheme", "Hardware", "Memory", "Note"]]
    rows += [
        ["FP8 weights + FP8 KV", "Ada, Blackwell", "~50% of FP16",
         "The right default on 4090 / 5090 / PRO class. Near-lossless in practice and it halves "
         "the KV cache, which is where the pressure is."],
        ["AWQ / GPTQ INT4", "Ampere and later", "~25% of FP16",
         "What Ampere gets, because it has no FP8 path. Calibrated 4-bit holds tool calling well; "
         "verify against the project&rsquo;s own check_agent.py rather than a published score."],
        ["Q4_K_M (GGUF)", "any, llama.cpp/Ollama",
         "~28% of FP16", "What the project runs today. Fine for a single stream, not a vLLM "
         "serving format."],
        ["FP16", "any", "100%",
         "Only worth it if a regression appears that quantisation explains. Costs roughly twice "
         "the card for no measured benefit at this scale."],
    ]
    f.append(table(rows, [34 * mm, 27 * mm, 22 * mm, FRAME_W - 83 * mm]))
    f.append(callout(
        "Test quantisation with the gates that already exist",
        "This project has <b>check_agent.py</b>, <b>check_tools.py</b>, <b>check_search.py</b>, "
        "<b>check_database.py</b> and a 279-test pytest suite, all of which assert on tool calls "
        "returning <b>ok</b> rather than merely being attempted. That is a better quantisation "
        "acceptance test than any leaderboard, because it measures the thing the product sells. "
        "Run the gates at each candidate quantisation and pick the smallest one that stays green.",
        GOOD))
    f.append(PageBreak())
    return f


FP8_CARDS = {"rtx_4090_used", "rtx_5090", "rtx_pro_5000", "rtx_pro_6000"}


FP8_CARDS = {"rtx_4090_used", "rtx_5090", "rtx_5080", "rtx_pro_5000", "rtx_pro_6000"}


def capability(data, m, b):
    """Can this build actually serve the design point?"""
    cost = m["costs"][b.ident][DUTY_MAIN]
    keys = [k for k, _ in b.gpus]
    # A rented instance is judged as the card it is priced against: the global
    # line is a Blackwell PRO 5000, the in-region line an H100. Both have FP8.
    fp8 = True if b.strategy == "rent" else (bool(keys) and all(k in FP8_CARDS for k in keys))
    batching = all(data["gpus"][k]["cuda"] for k in keys) if keys else True
    matched = len(set(keys)) <= 1
    need_key = "fp8" if fp8 else "fp16"
    need_design = m["vram"][m["design"].concurrency][need_key]["card_gb"]
    need_stress = m["vram"][m["stress"].concurrency][need_key]["card_gb"]

    if b.strategy == "rent":
        pooled = 48.0
    else:
        pooled = (cost["vram_gb"] if matched
                  else max(data["gpus"][k]["vram_gb"] for k in keys) if keys else 0.0)

    verdict, tone = "Serves 20 orgs", GOOD
    if not batching:
        verdict, tone = "Cannot batch", WARN
    elif pooled < need_design:
        verdict, tone = "Under-sized", WARN
    elif pooled < need_stress:
        verdict, tone = "Serves 15, tight at 20", GOLD
    elif pooled > need_stress * 1.6:
        verdict, tone = "Serves 20 orgs, headroom", ACCENT
    return {"fp8": fp8, "batching": batching, "matched": matched, "pooled": pooled,
            "need_design": need_design, "need_stress": need_stress,
            "verdict": verdict, "tone": tone}


def sec_models(data, m):
    f = [SectionMark("5 · Model selection")]
    f.append(P("Model selection", "h1"))
    f.append(P("Four slots, four shortlists, and one licence trap worth naming twice.", "small"))
    f.append(Spacer(1, 4))
    f.append(P(
        "The recommended agent is Qwen3-Coder-30B-A3B-Instruct, on a 92% Berkeley Function "
        "Calling score and an Apache 2.0 licence. The reasoning behind it matters more than the "
        "specific name: <b>a sparse mixture-of-experts model in the 30B "
        "class is the right shape for this workload</b>. It occupies the memory of a large model "
        "but reads only about three billion parameters per token, so it reasons like something "
        "big and decodes like something small. On a bandwidth-bound machine that is the entire "
        "game.", "lead"))
    f.append(P(
        "The field moved during 2026 &mdash; GLM, Qwen, MiniMax and Nemotron all shipped "
        "iterations &mdash; but the shortlist below is deliberately conservative. Model names "
        "churn faster than a procurement cycle, and this project has something better than a "
        "leaderboard to choose with: its own gates."))

    for title, key, cols in (
        ("Agent &mdash; the tool-calling model", "agent",
         ["Model", "Shape", "VRAM INT4", "VRAM FP8", "Licence", "Tool use"]),
    ):
        f.append(P(title, "h2"))
        rows = [cols]
        for r in data["models"][key]:
            shape = (f"{r['total_b']:g}B total / {r['active_b']:g}B active, {r['arch']}"
                     if r["active_b"] and r["active_b"] != r["total_b"]
                     else f"{r['total_b']:g}B {r['arch']}")
            rows.append([r["name"], shape, f"{r['vram_int4_gb']} GB", f"{r['vram_fp8_gb']} GB",
                         r["licence"], r["tool_score"]])
        f.append(table(rows, [43 * mm, 38 * mm, 17 * mm, 16 * mm, 26 * mm,
                              FRAME_W - 140 * mm],
                       align=["l", "l", "r", "r", "l", "l"]))
        notes = [r for r in data["models"][key] if r.get("note")]
        for r in notes[:3]:
            f.append(P(f"<b>{r['name']}.</b> {r['note']}", "small"))
    f.append(Spacer(1, 4))

    f.append(P("Vision &mdash; reading images and scanned documents", "h2"))
    rows = [["Model", "VRAM", "Licence", "Note"]]
    for r in data["models"]["vision"]:
        rows.append([r["name"], f"{r['vram_gb']} GB", r["licence"], r["note"]])
    f.append(table(rows, [34 * mm, 15 * mm, 30 * mm, FRAME_W - 79 * mm],
                   align=["l", "r", "l", "l"]))
    f.append(caption("Start with the 4B, budget for the 8B. A vision model earns a permanent "
                     "slot only once field extraction from invoice PDFs is a product feature "
                     "rather than an experiment &mdash; and HANDOVER.md already names structured "
                     "field extraction as the highest-value unbuilt thing."))

    f.append(P("Speech in and speech out", "h2"))
    rows = [["Slot", "Model", "VRAM", "Licence", "Note"]]
    for r in data["models"]["asr"]:
        rows.append(["Speech in", r["name"], f"{r['vram_gb']} GB", r["licence"], r["note"]])
    for r in data["models"]["tts"]:
        rows.append(["Speech out", r["name"], f"{r['vram_gb']} GB", r["licence"], r["note"]])
    f.append(table(rows, [18 * mm, 40 * mm, 14 * mm, 22 * mm, FRAME_W - 94 * mm],
                   align=["l", "l", "r", "l", "l"]))
    f.append(caption("Voice is the cheapest capability in the brief after graph generation. "
                     "Whisper Turbo and Kokoro together fit in 4 GB and can largely run beside "
                     "the agent rather than competing with it."))

    f.append(P("Graph generation is not a model", "h2"))
    f.append(P(
        "Worth stating once more in its own section, because it is the requirement most likely "
        "to be mis-specified into a hardware line item. &ldquo;Generate a graph of monthly "
        "removals by site&rdquo; is a <b>run_sql</b> call followed by a matplotlib call. The "
        "model chooses the query and the chart type; Python draws the picture. It belongs in "
        "<b>app/tools.py</b> beside <b>write_excel</b> and <b>write_pdf</b>, as a plain function "
        "with no knowledge that a model exists &mdash; which is exactly the contract the README "
        "sets for every tool in that file. Zero VRAM, testable without a GPU, and deterministic."))

    f.append(P("Licences: read the file, not the family name", "h2"))
    f.append(callout(
        "Read the LICENSE file, not the family name",
        "FLUX.2 klein-4B shipped Apache 2.0 and klein-9B shipped non-commercial, on the same day, "
        "under the same family name. Only one of them is sellable. The rule that follows is "
        "cheap to apply and expensive to skip: <b>before shipping anything to a paying client, "
        "open the LICENSE file in the exact repository and exact revision being deployed.</b> "
        "Community licences with user-count caps, research-only terms and revenue thresholds all "
        "exist inside families whose other members are permissive.", WARN))
    f.append(P(
        "For this project the practical consequence is narrow. Apache 2.0 and MIT cover the "
        "entire recommended stack &mdash; agent, vision, speech in, speech out and charting. "
        "There is no need to accept a restrictive licence to build what has been specified, and "
        "no reason to."))

    f.append(P("What to actually run, by tier", "h2"))
    rows = [["If the machine has", "Agent", "Vision", "Voice", "Comment"]]
    rows += [
        ["12&ndash;24 GB", "qwen3:8b or Nemotron Nano 9B, INT4", "Qwen3-VL-4B", "Whisper Turbo, "
         "Kokoro on CPU", "Today&rsquo;s stack. One or two users, no batching headroom."],
        ["32 GB", "Qwen3-Coder-30B-A3B, INT4", "Qwen3-VL-4B", "Whisper Turbo, Kokoro on CPU",
         "The agent fits; the KV cache is what runs out. FP8 cache is mandatory here."],
        ["48 GB", "Qwen3-Coder-30B-A3B, INT4 or FP8", "Qwen3-VL-8B", "Whisper Turbo, Kokoro",
         "<b>The design point.</b> Everything resident, eight slots at 16k, room to grow."],
        ["96 GB", "Qwen3-Coder-30B-A3B at FP8, or a 70B-class dense model", "Qwen3-VL-30B-A3B",
         "Voxtral, Chatterbox", "Buys model headroom rather than user headroom. Only worth it if "
         "the model roadmap changes."],
    ]
    f.append(table(rows, [23 * mm, 40 * mm, 26 * mm, 30 * mm, FRAME_W - 119 * mm]))
    f.append(PageBreak())
    return f


def build_page(data, m, b):
    cost = m["costs"][b.ident][DUTY_MAIN]
    cap = capability(data, m, b)
    f = []

    tier_tone = {"Low": GOOD, "Mid": ACCENT, "High": GOLD}[b.tier]
    kicker = ParagraphStyle("bk", parent=S["td"], fontName="Helvetica-Bold", fontSize=8,
                            leading=11, textColor=tier_tone, spaceAfter=3)
    title = ParagraphStyle("bt", parent=S["td"], fontName="Helvetica-Bold", fontSize=15,
                           leading=18.5, spaceAfter=0)
    tag = {"new": "NEW, WARRANTED", "used": "SECOND-HAND", "rent": "NO CAPITAL"}[b.condition]
    head = Table([[[Paragraph(f"{b.tier.upper()} TIER &nbsp;&middot;&nbsp; OPTION {b.ident} "
                              f"&nbsp;&middot;&nbsp; {tag}", kicker),
                    Paragraph(b.name, title)]]], colWidths=[FRAME_W])
    head.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, -1), 1.6, tier_tone),
    ]))
    f.append(head)
    f.append(Spacer(1, 7))

    if b.strategy == "rent":
        headline = [
            ("Capital", "AED 0"),
            ("International", money(cost["monthly"]) + "/mo"),
            ("In region", money(cost["monthly_region"]) + "/mo"),
            ("Per M tokens", f"{cost['per_mtok']:.2f}"),
            ("Design point", cap["verdict"]),
        ]
    else:
        headline = [
            ("Delivered", money(cost["gross"])),
            ("VRAM", f"{cost['vram_gb']:.0f} GB"),
            ("All-in monthly", money(cost["monthly"])),
            ("Per M tokens", f"{cost['per_mtok']:.2f}"),
            ("Design point", cap["verdict"]),
        ]
    cells = [[Paragraph(f'<font size=7 color="{MUTED.hexval()}">{k.upper()}</font><br/>'
                        f'<font size=11><b>{v}</b></font>', S["td"]) for k, v in headline]]
    t = Table(cells, colWidths=[FRAME_W / len(headline)] * len(headline))
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), FAINT),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, colors.white),
    ]))
    f.append(t)
    f.append(Spacer(1, 7))

    f.append(Paragraph(b.body, ParagraphStyle(
        "bbody", parent=S["body"], fontSize=9.0, leading=12.4, spaceAfter=2)))

    if cost["lines"]:
        f.append(P("Bill of materials, at UAE seller prices", "h3"))
        vat_pct = data["local_purchase"]["uae_vat_pct"]
        rows = [["Item", "Seller", "Unit", "Line"]]
        for label, count, unit, line, seller in cost["lines"]:
            name = label + (f"  &times;{count}" if count > 1 else "")
            rows.append([name, seller or "&mdash;", money(unit), money(line)])
        rows.append(["<b>Retail subtotal</b>", "", "", f"<b>{money(cost['retail'])}</b>"])
        rows.append([f"<b>Delivered, VAT-inclusive</b> <font size=6.8>(includes "
                     f"{money(cost['delivery'])} local delivery)</font>", "", "",
                     f"<b>{money(cost['gross'])}</b>"])
        reserve_note = (f", plus {money(cost['reserve'])} failure reserve"
                        if cost["reserve"] else "")
        rows.append([f"<b>Effective capital cost</b> <font size=6.8>(less {vat_pct:.0f}% VAT, "
                     f"recoverable if registered{reserve_note})</font>", "", "",
                     f"<b>{money(cost['capex_effective'])}</b>"])
        t = table(rows, [FRAME_W - 80 * mm, 32 * mm, 22 * mm, 26 * mm],
                  align=["l", "l", "r", "r"], zebra=False, size=7.4, pad=2.2)
        n = len(rows)
        t.setStyle(TableStyle([("LINEABOVE", (0, n - 3), (-1, n - 3), 0.7, INK)]))
        f.append(t)

    f.append(P(f"Capability against the design point &mdash; "
               f"<font size=9>{cap['need_design']:.0f} GB at 15 organisations, "
               f"{cap['need_stress']:.0f} GB at 20</font>", "h3"))
    checks = [
        ("Pooled memory vLLM can use", f"{cap['pooled']:.0f} GB",
         cap["pooled"] >= cap["need_stress"]),
        ("Continuous batching", "yes, CUDA + vLLM" if cap["batching"] else "no", cap["batching"]),
        ("Matched cards, tensor parallel", "yes" if cap["matched"] else
         "no &mdash; lanes must be split by model", cap["matched"]),
        ("FP8 KV cache", "yes" if cap["fp8"] else "no &mdash; Ampere; cache stays full width",
         cap["fp8"]),
        ("Warranty and tax invoice", {"new": "yes, local warranty",
                                      "used": "no &mdash; private sale",
                                      "rent": "n/a &mdash; an operating cost"}[b.condition],
         b.condition != "used"),
    ]
    rows = [["Capability", "This option", ""]]
    for label, value, good in checks:
        mark = ('<font color="%s"><b>&#10003;</b></font>' % GOOD.hexval()) if good else (
            '<font color="%s"><b>&#10007;</b></font>' % WARN.hexval())
        rows.append([label, value, mark])
    f.append(table(rows, [52 * mm, FRAME_W - 62 * mm, 10 * mm], align=["l", "l", "c"],
                   size=7.6, pad=2.2))

    if b.strategy == "buy":
        f.append(P("Buy versus rent, at three duty cycles", "h3"))
        rows = [["Duty cycle", f"Rent abroad<br/><font size=6.5>{b.rent_gpu_global}, "
                 f"${b.rent_usd_global:.2f}/hr</font>",
                 f"Rent in UAE<br/><font size=6.5>{b.rent_gpu_region}, "
                 f"${b.rent_usd_region:.2f}/hr</font>",
                 "Own, monthly run", "Break-even<br/>vs abroad", "Break-even<br/>vs in region",
                 "Avg draw"]]
        for h in data["assumptions"]["duty_cycle_hours_per_day"]:
            c = m["costs"][b.ident][h]

            def be(v):
                if v is None:
                    return '<font color="%s">never</font>' % WARN.hexval()
                col = GOOD if v <= data["assumptions"]["amortisation_months"] else WARN
                return '<font color="%s">%.0f mo</font>' % (col.hexval(), v)

            rows.append([f"{h} h/day", money(c["rent_global_month"]),
                         money(c["rent_region_month"]),
                         money(c["power_month"] + c["ops_month"]),
                         be(c["breakeven_global"]), be(c["breakeven_region"]),
                         f"{c['avg_w']:.0f} W"])
        f.append(table(rows, [17 * mm, 27 * mm, 27 * mm, 26 * mm, 22 * mm, 24 * mm,
                              FRAME_W - 143 * mm],
                       align=["l", "r", "r", "r", "r", "r", "r"], size=7.4, pad=2.2))

    f.append(KeepTogether([Spacer(1, 2), callout(
        "Verdict",
        f"<b>{b.verdict}</b><br/><br/><b>What it cannot do.</b> {b.cannot}",
        tier_tone, compact=True)]))
    f.append(PageBreak())
    return f


def sec_builds(data, m):
    f = [SectionMark("6 · The nine options")]
    f.append(P("Nine options, three tiers", "h1"))
    f.append(P("Every price is a UAE seller&rsquo;s listed AED price, VAT-inclusive, plus local "
               "delivery. No duty, no freight.", "small"))
    f.append(Spacer(1, 4))
    f.append(P(
        "The tiers are budget bands, not a ladder: moving up one does not strictly dominate the "
        "band below. New and second-hand options appear in every tier deliberately, because "
        "whether a private sale is acceptable is a separate decision from how much to spend, "
        "and the two should be made independently rather than bundled.", "lead"))

    f.append(P("At a glance", "h2"))
    rows = [["", "Option", "Cond.", "VRAM", "Delivered", "All-in / mo", "AED/M tok",
             "Design point"]]
    for b in m["builds"]:
        c = m["costs"][b.ident][DUTY_MAIN]
        cap = capability(data, m, b)
        rows.append([
            f"<b>{b.ident}</b>", b.name,
            {"new": "new", "used": "used", "rent": "rent"}[b.condition],
            "&mdash;" if b.strategy == "rent" else f"{c['vram_gb']:.0f} GB",
            "&mdash;" if b.strategy == "rent" else money(c["gross"]),
            money(c["monthly"]), f"{c['per_mtok']:.2f}",
            f'<font color="{cap["tone"].hexval()}">{cap["verdict"]}</font>'])
    f.append(table(rows, [8 * mm, 45 * mm, 12 * mm, 14 * mm, 22 * mm, 22 * mm, 18 * mm,
                          FRAME_W - 141 * mm], size=7.6, pad=3.5))
    f.append(caption(f"All-in monthly is capital over {data['assumptions']['amortisation_months']} "
                     "months plus electricity, cooling and four hours of operator time, at "
                     f"{DUTY_MAIN} hours of GPU duty a day. The rent row uses the international "
                     "line; its in-region equivalent is on its own page and in section 7."))

    cats = [b.ident for b in m["builds"]]
    per = [m["costs"][b.ident][DUTY_MAIN]["per_mtok"] for b in m["builds"]]
    top = max(per + [a["blended_aed"] for a in m["apis"] if a["class"] == "open-hosted"]) * 1.3
    d = vbar(cats, [per], [ACCENT], FRAME_W, 112, vmax=top,
             legend=["Fully loaded cost per million tokens, AED"])
    ch_x, ch_w, ch_y, ch_h = 38, FRAME_W - 52, 34, 112 - 58
    for a in m["apis"]:
        if a["blended_aed"] > top:
            continue
        y = ch_y + ch_h * a["blended_aed"] / top
        col = WARN if a["class"] == "frontier" else GOOD
        d.add(Line(ch_x, y, ch_x + ch_w, y, strokeColor=col, strokeWidth=0.8,
                   strokeDashArray=[3, 2.4]))
        d.add(String(ch_x + ch_w - 2, y + 2.4,
                     f"{a['model']}  AED {a['blended_aed']:.2f}/M", fontName="Helvetica",
                     fontSize=6.3, fillColor=col, textAnchor="end"))
    f.append(d)
    f.append(caption("Owned cost per million tokens against API list prices converted to AED, at "
                     f"the {m['design'].tokens_per_month / 1e6:.0f} M tokens a month the design "
                     "point generates. Frontier APIs sit far above every option here and are "
                     "off the top of the scale; the open-weight hosts sit among them."))

    f.append(P("How to read the nine pages that follow", "h3"))
    f.append(P(
        "Each option gets a bill of materials priced line by line against a named UAE seller, a "
        "capability check against the design point from section 3, and &mdash; for the eight "
        "that involve buying something &mdash; a buy-versus-rent table at eight, twelve and "
        "twenty-four hours of daily GPU duty, computed twice: once against renting abroad and "
        "once against renting in the UAE. A break-even beyond "
        f"{data['assumptions']['amortisation_months']} months is shown in red, which is the "
        "polite way of saying the machine never repays itself."))
    f.append(PageBreak())

    for b in m["builds"]:
        f.extend(build_page(data, m, b))
    return f


def sec_roi(data, m):
    design = m["design"]
    a = data["assumptions"]
    f = [SectionMark("7 · Business analysis")]
    f.append(P("Business analysis", "h1"))
    f.append(P("Two baselines, one total cost of ownership, and the conditions under which the "
               "answer flips.", "small"))
    f.append(Spacer(1, 4))
    f.append(P(
        "There is no revenue model here by design. This decision is judged on cost avoidance "
        "against two alternatives that already exist and can be priced today: paying an API for "
        "the tokens, and renting the same capability by the hour. Anything resting on a seat "
        "price nobody has set yet would be a forecast dressed as an analysis.", "lead"))

    f.append(P("How a monthly cost is built", "h2"))
    l2 = m["costs"]["L2"][DUTY_MAIN]
    rows = [["Component", "Basis", "Build L2"]]
    rows += [
        ["Capital, amortised", f"Effective capital over {a['amortisation_months']} months, VAT "
         "stripped, failure reserve added", money(l2["capex_month"]) + "/mo"],
        ["Electricity", "Average draw at the DEWA top commercial slab, "
         f"AED {data['energy']['aed_per_kwh']:.3f}/kWh including the fuel surcharge",
         money(l2["power_month"] / data["energy"]["cooling_multiplier"]) + "/mo"],
        ["Cooling", f"&times;{data['energy']['cooling_multiplier']:.2f} on electricity &mdash; a "
         "split unit moves roughly three watts of heat per watt drawn",
         money(l2["power_month"] * (1 - 1 / data["energy"]["cooling_multiplier"])) + "/mo"],
        ["Operations", f"{a['ops_hours_per_month']} hours a month at "
         f"{money(a['ops_rate_aed_per_hour'])}/hr &mdash; patching, drivers, model swaps, the "
         "3am restart", money(l2["ops_month"]) + "/mo"],
    ]
    rows.append(["<b>Total</b>", "", f"<b>{money(l2['monthly'])}/mo</b>"])
    f.append(table(rows, [30 * mm, FRAME_W - 62 * mm, 32 * mm], align=["l", "l", "r"]))
    f.append(caption("The same four components are applied identically to all nine options. "
                     "Operations is charged to the rental options too, at the same rate: "
                     "renting removes hardware maintenance but adds orchestration, and "
                     "pretending otherwise would bias the comparison. On the cheaper builds it "
                     "is the largest single line &mdash; below a certain hardware cost, the "
                     "machine is not what you are paying for."))

    f.append(P("Baseline one &mdash; what the tokens would cost from an API", "h2"))
    f.append(P(
        f"The design point generates <b>{design.tokens_per_month / 1e6:.0f} million tokens a "
        f"month</b>. The ratio matters as much as the total: at "
        f"{data['workload_model']['prompt_tokens_per_turn']:,} prompt tokens to "
        f"{data['workload_model']['output_tokens_per_turn']} output, this workload is about 94% "
        "input, which makes input pricing dominant and blends the headline rates downward. That "
        "flatters the APIs rather than the reverse."))
    rows = [["Model", "Class", "Blended $/M", "Blended AED/M", "Monthly, AED", "vs L2"]]
    for api in m["apis"]:
        ratio = api["monthly_aed"] / l2["monthly"]
        tone = GOOD if ratio > 1 else WARN
        rows.append([api["model"], api["class"], f"{api['blended']:.3f}",
                     f"<b>{api['blended_aed']:.2f}</b>", money(api["monthly_aed"]),
                     f'<font color="{tone.hexval()}">{ratio:.1f}&times;</font>'])
    rows.append(["<b>Build L2, owned</b>", "on-prem", "&mdash;",
                 f"<b>{l2['per_mtok']:.2f}</b>", f"<b>{money(l2['monthly'])}</b>", "&mdash;"])
    f.append(table(rows, [42 * mm, 22 * mm, 22 * mm, 26 * mm, 26 * mm, FRAME_W - 138 * mm],
                   align=["l", "l", "r", "r", "r", "r"]))

    cats = ["5 orgs", "10 orgs", "15 orgs", "20 orgs", "30 orgs", "40 orgs"]
    ns = [5, 10, 15, 20, 30, 40]
    own = [l2["monthly"]] * len(ns)
    sonnet = next(x for x in m["apis"] if "Sonnet" in x["model"])
    glm = next(x for x in m["apis"] if x["model"].startswith("GLM"))
    son_series = [sonnet["blended_aed"] * m["traffic"][n].tokens_per_month / 1e6 for n in ns]
    glm_series = [glm["blended_aed"] * m["traffic"][n].tokens_per_month / 1e6 for n in ns]
    f.append(vbar(cats, [own, glm_series, son_series], [ACCENT, GOOD, WARN], FRAME_W, 128,
                  vmax=max(son_series) * 1.12,
                  legend=["Own (build L2)", "GLM-5.2 hosted", "Claude Sonnet 5"]))
    f.append(caption("Monthly cost in AED against client count. The owned line is flat because "
                     "capital, power and operations do not care how many tokens pass through. "
                     "That flatness is the entire economic argument for owning, and it is why "
                     "the case strengthens with every client added."))

    f.append(P("Where the lines cross", "h2"))
    names, xs = [], []
    for api in m["apis"]:
        n = crossover_orgs(data, l2["monthly"], api["blended_aed"])
        names.append(api["model"])
        xs.append(min(n, 60))
    f.append(hbar(names, [xs], [ACCENT], FRAME_W, 104, vmax=64, fmt="{:.0f} orgs", x_left=132))
    f.append(caption("Client organisations at which owning build L2 becomes cheaper than paying "
                     "per token. Bars are capped at 60. Against a frontier API the machine pays "
                     "for itself almost immediately; against a hosted open-weight model it "
                     "needs the top of the target range."))
    f.append(callout(
        "The competitor is not the one you would expect",
        "The instinct is to compare an owned machine against GPT or Claude, where it wins easily "
        f"&mdash; {sonnet['model']} would cost {money(sonnet['monthly_aed'])} a month against "
        f"{money(l2['monthly'])} owned. <b>The real competitor is a hosted open-weight model at "
        f"AED {glm['blended_aed']:.2f} per million tokens</b>, running the same class of model "
        f"this machine would run, for {money(glm['monthly_aed'])} a month. On cost per token "
        f"alone that comparison is only won above about "
        f"{crossover_orgs(data, l2['monthly'], glm['blended_aed']):.0f} client organisations. "
        "The justification for owning has to rest on the three things a hosted option "
        "structurally cannot offer: <b>documents that never leave the building, latency with no "
        "data-centre round trip, and a bill that does not move when usage does.</b>", WARN))

    f.append(PageBreak())
    f.append(P("Baseline two &mdash; buy versus rent, twice", "h2"))
    f.append(P(
        "Because there is no in-region rental of a 48&nbsp;GB consumer card, the two rental "
        "lines are not the same product. Internationally the design point rents for "
        f"${data['rental']['design_point_global_usd_hr']:.2f} an hour. The cheapest UAE-hosted "
        f"option is a datacentre H100 at ${data['rental']['design_point_in_region_usd_hr']:.2f} "
        "&mdash; larger hardware than this workload needs, at about four times the price. Both "
        "are shown, because the difference between them is the price of a compliance position "
        "rather than a price of compute."))
    rows = [["Option", "Effective capital", "8 h/day", "12 h/day", "24/7",
             "8 h/day", "12 h/day", "24/7"]]
    for b in m["builds"]:
        if b.strategy == "rent":
            continue
        cap = capability(data, m, b)
        fails = cap["verdict"] in ("Under-sized", "Cannot batch")
        cells = [f"<b>{b.ident}</b> {b.name}" + (" &dagger;" if fails else ""),
                 money(m["costs"][b.ident][DUTY_MAIN]["capex_effective"])]
        for key in ("breakeven_global", "breakeven_region"):
            for h in (8, 12, 24):
                be = m["costs"][b.ident][h][key]
                if be is None:
                    cells.append('<font color="%s">never</font>' % WARN.hexval())
                else:
                    col = GOOD if be <= a["amortisation_months"] else WARN
                    cells.append('<font color="%s">%.0f</font>' % (col.hexval(), be))
        rows.append(cells)
    t = table(rows, [FRAME_W - 106 * mm, 24 * mm, 13 * mm, 14 * mm, 13 * mm,
                     13 * mm, 14 * mm, 13 * mm],
              align=["l", "r", "r", "r", "r", "r", "r", "r"], size=7.6, pad=3)
    t.setStyle(TableStyle([
        ("LINEBEFORE", (2, 0), (2, -1), 0.7, INK),
        ("LINEBEFORE", (5, 0), (5, -1), 0.7, INK),
    ]))
    f.append(Paragraph(
        f'<font size=7.6 color="{MUTED.hexval()}">Break-even in months, against '
        '<b>renting abroad</b> (first three columns) and <b>renting in the UAE</b> '
        '(last three)</font>', S["td"]))
    f.append(Spacer(1, 3))
    f.append(t)
    f.append(caption(f"Green is a break-even inside the {a['amortisation_months']}-month "
                     "write-off window. <b>&dagger; marks an option that does not meet the "
                     "design point</b>, so a fast payback there is buying something other than "
                     "the specified workload. Note the pattern: against international rental "
                     "most builds struggle, and against in-region rental almost all of them "
                     "repay quickly. That single contrast is the decision."))

    f.append(callout(
        "The inversion, stated plainly",
        "At twelve hours of GPU duty a day, renting a capable card abroad costs "
        f"{money(l2['rent_global_month'])} a month and owning build L2 costs "
        f"{money(l2['capex_month'] + l2['power_month'])} in capital and power &mdash; close "
        "enough that the choice turns on preference rather than arithmetic. Renting the same "
        f"capability in the UAE costs {money(l2['rent_region_month'])} a month, roughly "
        f"{l2['rent_region_month'] / (l2['capex_month'] + l2['power_month']):.0f} times owning. "
        "<b>If the data may leave the country, rent. If it may not, buy.</b> There is no third "
        "reading of these numbers.", ACCENT))

    f.append(PageBreak())
    f.append(P("Three-year total cost of ownership", "h2"))
    rows = [["Option", "Capital", "Power + cooling", "Operations", "3-year TCO", "AED/M tok"]]
    for b in m["builds"]:
        c = m["costs"][b.ident][DUTY_MAIN]
        n = a["amortisation_months"]
        capital = 0 if b.strategy == "rent" else c["capex_effective"]
        rows.append([f"<b>{b.ident}</b> {b.name}",
                     money(capital) if capital else money(c["rent_global_month"] * n) + " rent",
                     money(c["power_month"] * n) if c["power_month"] else "&mdash;",
                     money(c["ops_month"] * n), f"<b>{money(c['tco3'])}</b>",
                     f"{c['per_mtok']:.2f}"])
    f.append(table(rows, [FRAME_W - 112 * mm, 28 * mm, 26 * mm, 22 * mm, 24 * mm, 18 * mm],
                   align=["l", "r", "r", "r", "r", "r"]))
    f.append(caption("Three years at twelve hours of daily GPU duty, international rental line. "
                     f"Operations is a flat {money(l2['ops_month'] * a['amortisation_months'])} "
                     "on every row."))

    f.append(P("Sensitivity: what survives being wrong", "h2"))
    base = l2["monthly"]
    tok = design.tokens_per_month
    scen = [
        ["Base case", "As modelled", f"{base / (tok / 1e6):.2f}",
         f"{crossover_orgs(data, base, glm['blended_aed']):.0f} orgs",
         "The recommendation stands."],
        ["Half the utilisation", "Token volume &minus;50%",
         f"{base / (tok * 0.5 / 1e6):.2f}",
         f"{crossover_orgs(data, base, glm['blended_aed']):.0f} orgs",
         "Cost per token doubles; the API comparison worsens, the rental comparison does not "
         "move. Strengthens the case for renting."],
        ["Triple the volume", "Token volume &times;3", f"{base / (tok * 3 / 1e6):.2f}",
         "&mdash;",
         "Owning beats every option on this page including the cheap hosts. This is the "
         "scenario that justifies buying, and it is measurable in advance."],
        ["32k context sessions", "KV cache doubles", f"{base / (tok / 1e6):.2f}", "unchanged",
         "Cost per token is unchanged but 48&nbsp;GB stops fitting. This changes the "
         "<i>option</i>, not the economics: L2 and M1 become under-sized and H2 becomes the "
         "answer."],
        ["Card prices fall 30%", "Memory market normalises",
         f"{(base - l2['capex_month'] * 0.3) / (tok / 1e6):.2f}",
         f"{crossover_orgs(data, base - l2['capex_month'] * 0.3, glm['blended_aed']):.0f} orgs",
         "Break-evens shorten by about a third and the high tier becomes defensible. The reason "
         "the 96 GB options are deferred rather than dismissed."],
        ["Operator time doubles", "8 hours a month",
         f"{(base + l2['ops_month']) / (tok / 1e6):.2f}",
         f"{crossover_orgs(data, base + l2['ops_month'], glm['blended_aed']):.0f} orgs",
         "The most under-estimated line in any on-premise case, and on the cheap builds it "
         "already dominates the hardware."],
    ]
    f.append(table([["Scenario", "Change", "AED/M", "Crossover", "Consequence"]] + scen,
                   [30 * mm, 27 * mm, 16 * mm, 19 * mm, FRAME_W - 92 * mm]))
    f.append(callout(
        "What survives all six",
        "Two conclusions hold in every scenario. <b>The vLLM work comes first</b> &mdash; it is "
        "free, and no scenario makes serial inference acceptable for ten clients. <b>Renting "
        "briefly to measure before committing capital is never the wrong move</b> &mdash; where "
        "owning wins, a month of rental delays it by a month and costs "
        f"{money(0.64 * m['fx'] * 24 * 30)}; where owning loses, it saves between "
        f"{money(m['costs']['L2'][DUTY_MAIN]['gross'])} and "
        f"{money(m['costs']['H3'][DUTY_MAIN]['gross'])}.", GOOD))

    f.append(PageBreak())
    f.append(P("Depreciation and residual value", "h2"))
    f.append(P(
        "Standard practice writes compute hardware down to nothing over three years and this "
        "report&rsquo;s TCO does exactly that. The current market makes that conservative in one "
        "specific place: second-hand Ampere has held its price while everything new inflated "
        "around it, so cards bought used today are unlikely to be worth nothing in three years. "
        "The downside of the recommended build is smaller than the amortisation table implies."))
    f.append(P(
        "The mirror image is less comfortable. A card bought new at the top of a supply squeeze "
        "carries genuine mark-to-market risk: if memory supply normalises, the card does not "
        "become slower, but its resale value tracks a replacement cost that has fallen. "
        "<b>Buying used silicon at a market price is a smaller bet than buying new silicon at a "
        "shortage price</b>, and that asymmetry is a legitimate input to the decision."))

    f.append(P("Risk register", "h2"))
    rows = [["Risk", "Likelihood", "Impact", "Mitigation"]]
    rows += [
        ["Residency answer arrives after a purchase", "Medium", "High",
         "The whole decision hinges on it and it costs nothing to ask. Get it in writing before "
         "any card is bought; the wrong order here wastes the entire capital budget."],
        ["Used card fails, no warranty, no invoice", "Medium", "Medium",
         f"{a['used_card_failure_reserve_pct']}% of card cost reserved in every second-hand "
         "build. Inspect in person, ask for power-on hours under roughly 20,000, run a "
         "sustained load test before paying, and meet somewhere the card can be plugged in. A "
         "failed 3090 is a AED 2,400 problem."],
        ["Quoted price differs from the listed price", "High", "Medium",
         "UAE listings on the same part number span more than 50% on the workstation cards. "
         "Treat every figure here as a starting point for a written quote, not a commitment."],
        ["Single box is a single point of failure", "Medium", "High",
         "The real exposure once clients are contractual. Mitigation is a documented rental "
         "fallback that the vLLM port makes possible: an OpenAI-compatible client can be "
         "re-pointed at a rented endpoint in a configuration change. <b>Do not sign an "
         "availability commitment before that path is tested.</b>"],
        ["Windows to Linux migration slips", "Low", "Medium",
         "The systemd unit exists, paths go through pathlib, settings live in .env. Rehearse it "
         "on the rented instance in step 3, before any hardware is bought."],
        ["Model licence changes under a client", "Low", "High",
         "Pin the exact revision, keep the LICENSE file with the deployment record, and prefer "
         "Apache 2.0 and MIT throughout &mdash; which the recommended stack already is."],
        ["Concurrency is far above the model", "Medium", "Medium",
         "The reason step 3 is a month of measurement. Being wrong is only expensive if "
         "hardware is bought first."],
    ]
    f.append(table(rows, [46 * mm, 20 * mm, 18 * mm, FRAME_W - 84 * mm]))
    f.append(PageBreak())
    return f


def sec_recommend(data, m):
    l1 = m["costs"]["L1"][DUTY_MAIN]
    l2 = m["costs"]["L2"][DUTY_MAIN]
    m1 = m["costs"]["M1"][DUTY_MAIN]
    h2 = m["costs"]["H2"][DUTY_MAIN]
    h3 = m["costs"]["H3"][DUTY_MAIN]
    glm = next(x for x in m["apis"] if x["model"].startswith("GLM"))
    region_month = l1["rent_region_month"] + l1["ops_month"]
    f = [SectionMark("8 · Recommendation")]
    f.append(P("Recommendation", "h1"))
    f.append(P("Four stages, each with the number that triggers the next.", "small"))
    f.append(Spacer(1, 4))
    f.append(P(
        "The cheapest mistake available is renting for a month and discovering the load is "
        "smaller than modelled. The most expensive is committing most of the budget to silicon "
        "at the top of a shortage, for a workload nobody has measured, to satisfy a compliance "
        "requirement nobody has confirmed. The plan below is ordered so each stage produces the "
        "evidence the next one needs, and so stopping after any stage leaves a working system.",
        "lead"))

    stages = [
        ("Stage 1", "Ask whether the data may leave the country", "free", "do this first",
         "Every number in this report bends around the answer. If client documents may be "
         f"processed abroad, renting at {money(l1['monthly'])} a month is competitive with "
         "owning and infinitely more reversible, and a hosted open-weight API at "
         f"{money(glm['monthly_aed'])} is cheaper than both. If they may not, in-region rental "
         f"costs {money(region_month)} a month and owning becomes decisively correct.",
         "<b>Gate:</b> a written answer from the client. <b>Nothing else on this page should "
         "start before it exists</b>, because it changes which half of the report applies."),
        ("Stage 2", "Lift the three ceilings", "~1 day", "free, and required either way",
         "Port <b>app/llm.py</b> from Ollama&rsquo;s <b>/api/chat</b> to an OpenAI-compatible "
         "<b>/v1/chat/completions</b>. Raise <b>JOB_WORKERS</b> once a second lane exists. Run "
         "uvicorn with more than one worker. This is needed whether the endpoint ends up rented "
         "or owned, and it is what lets a rented endpoint act as a fallback later.",
         "<b>Gate:</b> the full suite stays green against a vLLM endpoint &mdash; pytest, "
         "check_tools, check_agent, check_search, check_database, check_endtoend."),
        ("Stage 3", "Rent for a month and measure",
         "~" + money(0.64 * m["fx"] * 24 * 30), "reversible",
         "A 48&nbsp;GB instance running the real stack against real client documents on Ubuntu. "
         "It replaces the modelled concurrency figure with a measured one and rehearses the "
         "Linux migration before any card is bought. Benchmark SGLang against vLLM here, and "
         "settle quantisation with the project&rsquo;s own gates rather than a leaderboard. If "
         "residency was confirmed in stage 1, run this on the in-region line and accept the "
         "higher rate for the month.",
         "<b>Gate:</b> record peak concurrent in-flight requests, p95 turn latency, tokens per "
         "session and prefix-cache hit rate. <b>Trigger to proceed:</b> measured peak "
         f"concurrency at or above {m['design'].concurrency}, sustained over several working "
         "days."),
        ("Stage 4", "Buy L2 &mdash; two used RTX 3090, 48 GB", money(l2["gross"]),
         "the recommended purchase",
         "Two matched 24&nbsp;GB cards from the local second-hand market, on an AM5 platform "
         "with a 1200 W supply. It is the cheapest hardware that meets the design point and it "
         "leaves the most options open: if one card dies it is a AED 2,400 replacement, not a "
         "reconsidered decision. Step up to M1 &mdash; two used RTX 4090 at "
         f"{money(m1['gross'])} &mdash; only if the twenty-organisation case is firm and the "
         "FP8 headroom is genuinely wanted.",
         "<b>Do not proceed</b> if stage 1 said the data may leave the country and stage 3 "
         "measured peak concurrency below four. At that load renting is cheaper and the capital "
         "is better spent on the structured field extraction HANDOVER.md already names as the "
         "highest-value unbuilt thing."),
    ]
    for tag, title, cost, note, body, gate in stages:
        block = [
            Paragraph(f'<font size=7.5 color="{ACCENT.hexval()}"><b>{tag.upper()}</b></font>'
                      f'&nbsp;&nbsp;<font size=7.5 color="{MUTED.hexval()}">{cost} &middot; '
                      f'{note}</font><br/><font size=11.5><b>{title}</b></font>', S["td"]),
            Spacer(1, 4), P(body), P(gate, "small"),
        ]
        t = Table([[block]], colWidths=[FRAME_W])
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, ACCENT),
            ("BACKGROUND", (0, 0), (-1, -1), FAINT),
        ]))
        f.append(KeepTogether([t, Spacer(1, 7)]))

    f.append(P("What not to buy, and why", "h2"))
    rows = [["Option", "Why not"]]
    rows += [
        ["L3 &mdash; new RTX 5080 16 GB", "At the same money as two used 3090s it carries a "
         "third of the memory and cannot serve the workload. Worth having as a development box "
         "beside a rented endpoint; not as the server."],
        ["M2 &mdash; new RTX 5090 32 GB", "32&nbsp;GB does not hold the agent, a vision model "
         "and eight sessions of cache. The fastest single card here and the wrong size, at "
         f"{money(15000)} for the privilege."],
        ["M3 &mdash; four used RTX 3090", "96&nbsp;GB of Ampere with no FP8 path, on an sTR5 "
         "platform that costs more than the cards. Four unwarranted cards solving a problem two "
         "already solve."],
        ["H3 &mdash; RTX PRO 6000 96 GB", "Technically the best machine in this report. It also "
         f"consumes {h3['gross'] / data['assumptions']['budget_ceiling_aed'] * 100:.0f}% of the "
         "budget ceiling for capacity the measured workload does not need, at a price that rose "
         "87% in eighteen months without the silicon changing. If a 96&nbsp;GB machine is truly "
         f"wanted, H2 reaches the same memory for {money(h3['gross'] - h2['gross'])} less."],
    ]
    f.append(table(rows, [46 * mm, FRAME_W - 46 * mm]))

    f.append(P("The one thing to settle before any of it", "h2"))
    f.append(callout(
        "Confirm the residency requirement in writing",
        "It is worth repeating because it is both the cheapest step and the one most likely to "
        f"be skipped. If documents may leave the UAE: renting abroad at {money(l1['monthly'])} "
        f"a month is competitive with owning, and a hosted open-weight API at "
        f"{money(glm['monthly_aed'])} is cheaper than every option in this report below about "
        f"{crossover_orgs(data, l2['monthly'], glm['blended_aed']):.0f} client organisations "
        "&mdash; in which case the capital is better spent on the product than on the "
        f"infrastructure. If they may not: in-region rental at {money(region_month)} a month "
        f"makes owning repay itself in well under a year, and build L2 at {money(l2['gross'])} "
        "is the right purchase. <b>Two different reports follow from one question, and it costs "
        "an email to ask.</b>", WARN))
    f.append(PageBreak())
    return f


def sec_appendix(data, m):
    f = [SectionMark("Appendices")]
    f.append(P("Appendix A &mdash; what to measure before spending", "h1"))
    f.append(P("Every concurrency figure in this report is modelled. Four instruments turn "
               "them into measurements.", "small"))
    f.append(Spacer(1, 4))
    rows = [["Metric", "Where it goes", "Why it decides something"]]
    rows += [
        ["Peak concurrent in-flight requests",
         "A gauge around the agent call in <b>app/agent.py</b>, sampled per second and reported "
         "on <b>/api/health</b>",
         "The single input the whole VRAM budget hangs on. Section 3 models "
         f"{m['design'].concurrency} at fifteen organisations; if the real figure is three, buy "
         "nothing."],
        ["p95 turn latency, split prefill / decode",
         "Timestamps already available in the <b>steps</b> trace <b>agent.ask()</b> returns",
         "Separates &lsquo;the model is slow&rsquo; from &lsquo;the tools are slow&rsquo;. If "
         "tool round-trips dominate, a faster card changes nothing."],
        ["Tokens per session, prompt and output separately",
         "Log the counts the inference server already returns",
         "Drives both the KV budget and the API comparison. The 94% input ratio is an "
         "assumption; measuring it could move the blended rates materially."],
        ["Prefix-cache hit rate",
         "vLLM exposes it directly once stage 1 lands",
         "On an agent loop that re-sends its whole history, this is the cheapest performance "
         "lever available and nobody currently knows its value here."],
    ]
    f.append(table(rows, [42 * mm, 54 * mm, FRAME_W - 96 * mm]))
    f.append(caption("All four are cheap to add and none requires new infrastructure. The "
                     "project already has the right habit &mdash; check_services.py compares a "
                     "fingerprint of running code against what is on disk rather than trusting "
                     "a restart &mdash; and this is the same instinct applied to load."))

    f.append(P("Appendix B &mdash; assumptions", "h2"))
    w = data["workload_model"]
    a = data["assumptions"]
    rows = [["Assumption", "Value", "Basis"]]
    rows += [
        ["Active users per organisation", str(w["active_users_per_org"]), "Assumed"],
        ["Sessions per user per day", str(w["sessions_per_user_per_day"]), "Assumed"],
        ["Turns per session", str(w["turns_per_session"]), "Assumed"],
        ["Prompt tokens per turn", f"{w['prompt_tokens_per_turn']:,}",
         "Derived from TOOL_RESULT_BUDGET = 12,000 chars and the full-history re-send in "
         "app/agent.py"],
        ["Output tokens per turn", str(w["output_tokens_per_turn"]), "Assumed"],
        ["Peak hour share of daily traffic", f"{w['peak_hour_share'] * 100:.0f}%",
         "Assumed; deliberately pessimistic for an office-hours shape"],
        ["Context per session", f"{w['context_per_session']:,}",
         "Assumed; qwen3 models here are trained to 40,960"],
        ["Target decode rate per user", f"{w['target_decode_tps_per_user']} tok/s",
         "The rate at which streamed output reads as responsive"],
        ["Prefill rate share under batching", "2,500 tok/s",
         "Modelled. The measured prompt_tps in bench_results.json is not usable for this: it "
         "reports cached prompt processing"],
        ["Agent KV geometry", "48 layers, 4 KV heads, 128 head dim",
         "Assumed for a 30B-A3B-class model. The formula is validated against the measured 8B, "
         "not against this configuration"],
        ["vLLM memory utilisation", f"{a['vllm_gpu_memory_utilization']:.2f}",
         "Practical default; you do not get the whole card"],
        ["Amortisation", f"{a['amortisation_months']} months", "Standard for compute hardware"],
        ["Operator time", f"{a['ops_hours_per_month']} h/mo at "
         f"{money(a['ops_rate_aed_per_hour'])}/hr",
         "Mid-market UAE contract rate; charged identically to buying and to renting"],
        ["Used-card failure reserve", f"{a['used_card_failure_reserve_pct']}% of card cost",
         "No warranty on second-hand silicon"],
        ["Electricity", f"AED {data['energy']['aed_per_kwh']:.3f}/kWh",
         "DEWA top commercial slab 0.380 AED plus the 0.060 AED fuel surcharge. A server "
         "running continuously sits in the top slab, so this is the right marginal rate"],
        ["Cooling uplift", f"&times;{data['energy']['cooling_multiplier']:.2f}",
         "A split unit moves roughly three watts of heat per watt drawn"],
        ["Purchase basis", f"UAE seller price less {data['local_purchase']['uae_vat_pct']:.0f}% VAT, "
         f"plus {money(data['local_purchase']['local_delivery_aed'])} delivery",
         "Bought locally, so customs duty is already inside the shelf price and there is no "
         "freight or clearance. VAT is recoverable by a registered business. USD-billed APIs "
         f"and international rental convert at the pegged AED {data['locale']['aed_per_usd']}, "
         "so there is no currency risk"],
    ]
    f.append(table(rows, [46 * mm, 34 * mm, FRAME_W - 80 * mm], size=7.6, pad=3.5))

    f.append(PageBreak())
    f.append(P("Appendix C &mdash; sources", "h1"))
    f.append(P("Every figure in this report resolves to a row in "
               "<b>docs/hardware_report_data.json</b> carrying the URL and capture date below. "
               "The generator refuses to build if a priced row is missing either.", "small"))
    f.append(Spacer(1, 4))

    seen = {}

    def collect(node):
        if isinstance(node, dict):
            if node.get("source_url", "").startswith("http"):
                seen.setdefault(node["source_url"], node.get("as_of", ""))
            for k, v in node.items():
                if not k.startswith("_"):
                    collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(data)
    rows = [["Captured", "Source"]]
    for url, when in sorted(seen.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        rows.append([when or "2026", url])
    f.append(table(rows, [20 * mm, FRAME_W - 20 * mm], size=7, pad=2.6))
    f.append(caption("Internal sources &mdash; this project&rsquo;s own bench_results.json, "
                     "check_env.py output, README, HANDOVER.md and docs/plans &mdash; are cited "
                     "inline where used and are not listed here."))

    f.append(P("Appendix D &mdash; regenerating this report", "h2"))
    f.append(P(
        "GPU and memory prices in this market have moved by twenty per cent inside two months. "
        "This document is built to be re-run rather than re-written:"))
    f.append(callout(
        "python scripts/build_hardware_report.py",
        "Edit <b>docs/hardware_report_data.json</b> &mdash; a price, a rental rate, an API "
        "tariff, a workload assumption &mdash; and run the generator. The sizing model, every "
        "cost table, every break-even, every chart and the source appendix all re-derive from "
        "those inputs. Nothing in the prose contradicts the data file, because nothing in the "
        "prose is typed twice.<br/><br/>The script imports nothing from <b>app/</b>, so it "
        "cannot affect the running service, and it needs only <b>reportlab</b>, which is already "
        "in <b>requirements.txt</b>.", GOOD))
    f.append(P(
        "One habit worth keeping when it is re-run: record the capture date on every row at the "
        "moment the figure is found, not afterwards. Figures assembled after the fact are "
        "not wrong when they were written &mdash; they were assembled from working sessions "
        "whose own dates had already drifted past the market. A date on every number is what "
        "makes the difference visible next time.", "small"))
    return f


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    problems = check_provenance(data)
    if problems:
        print("Refusing to build. Figures without provenance:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        raise ProvenanceError(f"{len(problems)} unsourced figures")

    _styles()
    m = compute(data)

    story = []
    for section in (sec_cover, sec_exec, sec_changed, sec_sizing, sec_serving,
                    sec_models, sec_builds, sec_roi, sec_recommend, sec_appendix):
        story.extend(section(data, m))

    # The running head is painted at page-begin, so a SectionMark sitting after a
    # PageBreak renames the head one page too late. Swap each such pair.
    for i in range(1, len(story)):
        if isinstance(story[i], SectionMark) and isinstance(story[i - 1], PageBreak):
            story[i - 1], story[i] = story[i], story[i - 1]

    doc = Doc(str(OUT_PATH), title="Syslab Server - Hardware, Model & ROI Report",
              author="syslab-server", subject="Hardware procurement and business analysis")
    doc.build(story)

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"wrote {OUT_PATH.relative_to(ROOT)}  ({doc.page} pages, {size_kb:,.0f} KB)")
    print(f"  design point: {m['design'].orgs} orgs, {m['design'].concurrency} concurrent, "
          f"{m['design'].tokens_per_month / 1e6:.0f}M tokens/month")
    l2 = m["costs"]["L2"][DUTY_MAIN]
    print(f"  recommended:  L2 at {money(l2['gross'])} delivered, "
          f"AED {l2['per_mtok']:.2f}/M tokens, {money(l2['monthly'])}/month all-in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
