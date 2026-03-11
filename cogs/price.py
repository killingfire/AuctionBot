"""
cogs/price.py – Smart price lookup for Pokémon auctions.

Uses the same filter system as auction search.
Finds comparable past sales and surfaces useful price intelligence
for buyers and sellers.

Field mapping (DB short name → meaning):
  ts   = unix_timestamp      bid  = winning_bid
  pn   = pokemon_name        sh   = shiny
  gx   = gmax                iv   = total_iv_percent
  lv   = level               mv   = moves
  spe  = iv_speed            atk  = iv_attack
  hp/def/spa/spd = other IVs nat  = nature
  gen  = gender              aid  = auction_id
"""
from __future__ import annotations

import numpy as np
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

import config
from config import REPLY
from utils import build_query, resolve_pokemon_name, shiny_prefix
from filters import FLAG_DEFINITIONS

# ─── DB ───────────────────────────────────────────────────────────────────────
_mongo = MongoClient(config.MONGO_URI)
_db    = _mongo[config.MONGO_DB_NAME]
_col   = _db[config.MONGO_COLLECTION]

# ─── Name flag aliases (derived from filters.py — stays in sync automatically) ─
_NAME_FLAGS: frozenset[str] = frozenset(
    ["--name"] + FLAG_DEFINITIONS["--name"].get("aliases", [])
)

# How many IV percent points either side to use for "comparable" sales
IV_BAND = 5.0

# Minimum sales needed before we show a premium estimate
MIN_PREMIUM_SAMPLE = 5


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    if val >= 1_000_000:
        return f"{val/1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val/1_000:.1f}k"
    return f"{int(val):,}"


def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


def _median(vals: list[float]) -> float:
    return float(np.median(vals))


def _confidence(n: int) -> str:
    if n >= 30:
        return "🟢 High confidence"
    if n >= 10:
        return "🟡 Moderate confidence"
    return "🔴 Low confidence — small sample"


def _prices(records: list[dict]) -> list[float]:
    return [r["bid"] for r in records if r.get("bid") is not None]


def _premium_line(label: str, with_prices: list[float], without_prices: list[float]) -> str | None:
    """
    Compare median price of two groups and return a formatted premium line.
    Returns None if either group is too small.
    """
    if len(with_prices) < MIN_PREMIUM_SAMPLE or len(without_prices) < MIN_PREMIUM_SAMPLE:
        return None
    diff = _median(with_prices) - _median(without_prices)
    if abs(diff) < 100:
        return None
    sign  = "+" if diff > 0 else "-"
    arrow = "📈" if diff > 0 else "📉"
    return f"{arrow} **{label}** premium: `{sign}{_fmt(abs(diff))}` vs without"


# ─────────────────────────────────────────────────────────────────────────────
# CORE PRICE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def _analyse(query: dict, filters_str: str, limit: int | None = None) -> discord.ui.LayoutView:
    """
    Run price analysis for the given query.
    Steps:
      1. Base query  — name + shiny + gmax (hard filters)
      2. Comparable  — base + IV band around the queried IV (if iv in query)
      3. Exact match — the full user query
      4. Premium estimates — compare subsets
    """

    # ── Determine hard base (name, shiny, gmax) ───────────────────────────────
    base_query: dict = {}
    if "pn" in query:
        base_query["pn"] = query["pn"]
    if "sh" in query:
        base_query["sh"] = query["sh"]
    if "gx" in query:
        base_query["gx"] = query["gx"]

    # Resolve a display name
    pn_val = query.get("pn", {})
    if isinstance(pn_val, dict) and "$in" in pn_val:
        # expand_name_by_dex path: pn = {"$in": ["Sentret", "Furret", ...]}
        # Pick the shortest name — base forms are typically shorter than form names
        candidates = pn_val["$in"]
        name = min(candidates, key=len) if candidates else "Unknown"
    elif isinstance(pn_val, dict) and "$regex" in pn_val:
        raw_name = pn_val["$regex"].strip("^$")
        name = resolve_pokemon_name(raw_name) or raw_name or "Unknown"
    else:
        name = str(pn_val) or "Unknown"

    is_shiny = query.get("sh") is True
    is_gmax  = query.get("gx") is True

    # ── Pull all base records (for premium calculations) ──────────────────────
    _base_cur = _col.find(base_query, {
        "bid": 1, "iv": 1, "lv": 1, "spe": 1, "atk": 1,
        "mv": 1, "gen": 1, "sh": 1, "gx": 1, "ts": 1, "aid": 1,
    }).sort("ts", -1)
    if limit is not None:
        _base_cur = _base_cur.limit(limit)
    base_records = list(_base_cur)

    if not base_records:
        return _error_view(f"❌ No past sales found for **{name}**.")

    base_prices = _prices(base_records)

    # ── Comparable records (IV-banded if IV filter present) ───────────────────
    iv_cond      = query.get("iv")
    iv_target    = None
    comp_records = base_records  # fallback: all base records

    if isinstance(iv_cond, dict):
        # Try to extract a centre value from the IV condition
        if "$gte" in iv_cond and "$lte" in iv_cond:
            iv_target = (iv_cond["$gte"] + iv_cond["$lte"]) / 2
        elif "$gte" in iv_cond:
            iv_target = iv_cond["$gte"]
        elif "$eq" in iv_cond:
            iv_target = iv_cond["$eq"]

    if iv_target is not None:
        lo = iv_target - IV_BAND
        hi = iv_target + IV_BAND
        comp_query = {**base_query, "iv": {"$gte": lo, "$lte": hi}}
        _comp_cur = _col.find(comp_query, {
            "bid": 1, "iv": 1, "lv": 1, "spe": 1, "atk": 1,
            "mv": 1, "gen": 1, "sh": 1, "gx": 1, "ts": 1, "aid": 1,
        }).sort("ts", -1)
        if limit is not None:
            _comp_cur = _comp_cur.limit(limit)
        comp_records = list(_comp_cur)
        if len(comp_records) < 3:
            comp_records = base_records  # fall back if band is too narrow

    comp_prices = _prices(comp_records)

    # ── Exact-match records (full user query) ─────────────────────────────────
    _exact_cur = _col.find(query, {"bid": 1, "ts": 1, "aid": 1, "iv": 1}).sort("ts", -1)
    if limit is not None:
        _exact_cur = _exact_cur.limit(limit)
    exact_records = list(_exact_cur)
    exact_prices  = _prices(exact_records)

    # ── Stats ─────────────────────────────────────────────────────────────────
    use_prices = exact_prices if len(exact_prices) >= 3 else comp_prices
    use_label  = "exact match" if len(exact_prices) >= 3 else f"comparable (±{IV_BAND}% IV)"
    n          = len(use_prices)

    if n == 0:
        return _error_view("❌ Not enough sales data to analyse.")

    p_median = _median(use_prices)
    p_avg    = float(np.mean(use_prices))
    p_min    = float(np.min(use_prices))
    p_max    = float(np.max(use_prices))
    p_std    = float(np.std(use_prices))

    # Most recent 5 sales
    recent = sorted(exact_records or comp_records, key=lambda r: r.get("ts", 0), reverse=True)[:5]
    recent_lines = []
    for r in recent:
        ts  = r.get("ts")
        dt  = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %b %Y") if ts else "?"
        iv  = r.get("iv")
        ivs = f"{iv:.1f}%" if iv is not None else "?"
        recent_lines.append(
            f"{REPLY} `{_fmt(r['bid'])}` — {ivs} IV — {dt} — `#{r.get('aid','?')}`"
        )

    # ── Premium estimates ─────────────────────────────────────────────────────
    premiums: list[str] = []

    # Shiny premium (only if not already filtering shiny)
    if not is_shiny and not is_gmax:
        shiny_recs   = [r for r in base_records if r.get("sh")]
        noshiny_recs = [r for r in base_records if not r.get("sh")]
        line = _premium_line("Shiny", _prices(shiny_recs), _prices(noshiny_recs))
        if line:
            premiums.append(line)

    # Max speed premium
    maxspe_recs   = [r for r in comp_records if r.get("spe") == 31]
    nospe_recs    = [r for r in comp_records if r.get("spe") != 31]
    line = _premium_line("Max Speed (31)", _prices(maxspe_recs), _prices(nospe_recs))
    if line:
        premiums.append(line)

    # Max attack premium
    maxatk_recs  = [r for r in comp_records if r.get("atk") == 31]
    noatk_recs   = [r for r in comp_records if r.get("atk") != 31]
    line = _premium_line("Max Attack (31)", _prices(maxatk_recs), _prices(noatk_recs))
    if line:
        premiums.append(line)

    # Zero attack (for special attackers) — 0 atk can be desirable
    zeroatk_recs = [r for r in comp_records if r.get("atk") == 0]
    line = _premium_line("0 Attack", _prices(zeroatk_recs), _prices(noatk_recs))
    if line:
        premiums.append(line)

    # Split IV premium (iv == 50.00 exactly)
    split_recs  = [r for r in base_records if r.get("iv") == 50.0]
    nosplit_recs = [r for r in base_records if r.get("iv") != 50.0]
    line = _premium_line("Split (50.00% IV)", _prices(split_recs), _prices(nosplit_recs))
    if line:
        premiums.append(line)

    # Low level premium (<15)
    lowlv_recs  = [r for r in base_records if (r.get("lv") or 100) < 15]
    normlv_recs = [r for r in base_records if (r.get("lv") or 100) >= 15]
    line = _premium_line("Low Level (<15)", _prices(lowlv_recs), _prices(normlv_recs))
    if line:
        premiums.append(line)

    # Gender premium — gen is stored as "Male" / "Female" / "Unknown"
    female_recs = [r for r in base_records if r.get("gen") == "Female"]
    male_recs   = [r for r in base_records if r.get("gen") == "Male"]
    line = _premium_line("Female", _prices(female_recs), _prices(male_recs))
    if line:
        premiums.append(line)

    # Move premiums — check if any moves appear in the query's $and clauses
    # (build_query puts moves in $and as $elemMatch)
    queried_moves: list[str] = []
    for clause in query.get("$and", []):
        mv = clause.get("mv", {})
        if isinstance(mv, dict) and "$elemMatch" in mv:
            regex = mv["$elemMatch"].get("$regex", "")
            if regex:
                queried_moves.append(regex)

    for move_regex in queried_moves:
        with_move    = [r for r in comp_records
                        if any(move_regex.lower() in str(m).lower() for m in (r.get("mv") or []))]
        without_move = [r for r in comp_records
                        if not any(move_regex.lower() in str(m).lower() for m in (r.get("mv") or []))]
        line = _premium_line(f'Move: {move_regex}', _prices(with_move), _prices(without_move))
        if line:
            premiums.append(line)

    # ── Build display ─────────────────────────────────────────────────────────
    shiny_tag = "✨ Shiny " if is_shiny else ""
    gmax_tag  = "⚡ Gmax " if is_gmax else ""
    title     = f"## 💰 Price Check — {shiny_tag}{gmax_tag}{name}"

    iv_range_s  = ""
    if iv_target is not None:
        iv_range_s = f"  •  IV ~{iv_target:.1f}% (±{IV_BAND}%)"
    limit_note  = f"  •  last {limit:,}" if limit is not None else ""

    stats_text = (
        f"**📊 Price Stats** — _{use_label}, {n} sales{iv_range_s}{limit_note}_\n"
        f"{REPLY} **Median:** `{_fmt(p_median)}`  ← best single reference\n"
        f"{REPLY} **Average:** `{_fmt(p_avg)}`\n"
        f"{REPLY} **Range:** `{_fmt(p_min)}` – `{_fmt(p_max)}`\n"
        f"{REPLY} **Std Dev:** `{_fmt(p_std)}`  {'(stable market)' if p_std < p_avg * 0.25 else '(volatile — prices vary a lot)'}\n"
        f"{REPLY} {_confidence(n)}"
    )

    # Broad base stats (all sales of this Pokémon in this variant)
    all_n = len(base_prices)
    broad_text = (
        f"**📦 All-time ({name}{' shiny' if is_shiny else ''}{' gmax' if is_gmax else ''})** — {all_n} total sales\n"
        f"{REPLY} Median `{_fmt(_median(base_prices))}` • "
        f"Min `{_fmt(float(np.min(base_prices)))}` • "
        f"Max `{_fmt(float(np.max(base_prices)))}`"
    )

    recent_text = (
        f"**🕐 Recent Sales**\n"
        + ("\n".join(recent_lines) if recent_lines else f"{REPLY} _No recent sales_")
    )

    premium_text = (
        "**⚡ Attribute Premiums** _(based on past sales)_\n"
        + ("\n".join(premiums) if premiums else f"{REPLY} _Not enough data to estimate premiums_")
    )

    filters_display = filters_str.strip() or "no filters"

    comps = [
        discord.ui.TextDisplay(content=title),
        discord.ui.TextDisplay(content=f"-# Filters: `{filters_display}`"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=stats_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=broad_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=premium_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=recent_text),
    ]

    accent = config.SHINY_EMBED_COLOR if is_shiny else config.EMBED_COLOR

    class PriceView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=accent)
        def __init__(self):
            super().__init__(timeout=180)

    return PriceView()


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class Price(commands.Cog):
    """Smart price lookup using historical auction data"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="price", aliases=["pc", "pricecheck"])
    @app_commands.describe(filters="Same filters as auction search e.g: --name eevee --shiny --iv >85")
    async def price_cmd(self, ctx: commands.Context, *, filters: str = ""):
        """
        Price check a Pokémon using historical auction data.

        Examples:
          j!price --name garchomp --iv 90
          j!price --name eevee --shiny
          j!price --name charizard --gmax --iv >85
          j!price --name umbreon --move wish
        """
        if not any(t in _NAME_FLAGS for t in (filters.split() if filters else [])):
            await ctx.send(
                view=_error_view(
                    f"❌ Please specify a Pokémon name.\n"
                    f"{REPLY} Example: `j!price --name garchomp --iv 90`\n"
                    f"{REPLY} Example: `j!price --name eevee --shiny`"
                ),
                reference=ctx.message,
                mention_author=False,
            )
            return

        raw              = filters.split() if filters else []
        query, _, limit  = build_query(raw, expand_name_by_dex=True)

        async with ctx.typing():
            view = _analyse(query, filters, limit=limit)

        await ctx.send(view=view, reference=ctx.message, mention_author=False)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Price(bot))
