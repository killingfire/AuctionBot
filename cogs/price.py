"""
cogs/price.py – Smart price lookup for Pokémon auctions.

Uses the same filter system as auction search (expand_name_by_dex=True).
Outliers are excluded from stats using the same 3×IQR fence as graph.py,
and listed separately as a table image if present.

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

import io
from datetime import datetime, timezone

import discord
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

import config
from config import REPLY
from utils import build_query, resolve_pokemon_name
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

# Outlier fence multiplier — same as graph.py
OUTLIER_FENCE = 3.0

# Theme — matches graph.py
BG_DARK     = "#1e1f22"
BG_CARD     = "#2b2d31"
GRID_COLOR  = "#3a3d44"
TEXT_COLOR  = "#dcddde"
MUTED_COLOR = "#72767d"


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    if val >= 1_000_000:
        return f"{val/1_000_000:.2f}M"
    if val >= 100_000:
        return f"{val/1_000:.1f}k"
    if val >= 10_000:
        return f"{val/1_000:.1f}k"
    if val >= 1_000:
        return f"{val/1_000:.2f}k"
    return f"{int(val):,}"


def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


def _confidence(n: int) -> str:
    if n >= 30:
        return "🟢 High confidence"
    if n >= 10:
        return "🟡 Moderate confidence"
    return "🔴 Low confidence — small sample"


def _prices(records: list[dict]) -> list[float]:
    return [r["bid"] for r in records if r.get("bid") is not None]


def _median(vals: list[float]) -> float:
    return float(np.median(vals))


def _remove_outliers(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split records into (clean, outliers) using the same 3xIQR upper fence
    as graph.py. Falls back to returning all records as clean if fewer than
    3 would remain after filtering.
    """
    if len(records) < 4:
        return records, []

    prices = np.array(_prices(records), dtype=float)
    q1, q3 = np.percentile(prices, 25), np.percentile(prices, 75)
    iqr    = q3 - q1
    fence  = q3 + OUTLIER_FENCE * iqr if iqr > 0 else prices.max()

    clean    = [r for r in records if (r.get("bid") or 0) <= fence]
    outliers = [r for r in records if (r.get("bid") or 0) > fence]

    if len(clean) < 3:
        return records, []

    return clean, outliers


def _premium_line(
    label: str,
    with_prices: list[float],
    without_prices: list[float],
) -> str | None:
    if len(with_prices) < MIN_PREMIUM_SAMPLE or len(without_prices) < MIN_PREMIUM_SAMPLE:
        return None
    diff = _median(with_prices) - _median(without_prices)
    if abs(diff) < 100:
        return None
    sign  = "+" if diff > 0 else "-"
    arrow = "📈" if diff > 0 else "📉"
    return f"{arrow} **{label}**: `{sign}{_fmt(abs(diff))}`"


def _build_outlier_image(outliers: list[dict]) -> io.BytesIO:
    """
    Table image for outlier sales — matches graph.py style exactly.
    Columns: #, Auction ID, Date, Level, IV%, Winning Bid
    """
    n        = len(outliers)
    row_h_in = 0.38
    head_h   = 0.50
    fig_h    = head_h + n * row_h_in

    fig, ax = plt.subplots(figsize=(10, fig_h), facecolor=BG_DARK)
    ax.set_facecolor(BG_DARK)
    ax.axis("off")

    headers    = ["#", "Auction ID", "Date", "Level", "IV %", "Winning Bid"]
    col_widths = [0.05, 0.18, 0.22, 0.10, 0.13, 0.22]

    rows = []
    for i, r in enumerate(outliers):
        ts    = r.get("ts")
        date  = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %b %Y") if ts else "?"
        aid   = str(r.get("aid", "?"))
        level = str(r.get("lv", "???"))
        iv    = r.get("iv")
        iv_s  = f"{iv:.2f}%" if iv is not None else "???"
        bid   = r.get("bid", 0)
        rows.append([str(i + 1), aid, date, level, iv_s, _fmt(bid)])

    tbl = ax.table(
        cellText=rows,
        colLabels=headers,
        colWidths=col_widths,
        loc="center",
        cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.5)

    cell_h = row_h_in / fig_h
    for (row, col), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID_COLOR)
        cell.set_linewidth(0.5)
        cell.set_height(cell_h)
        if row == 0:
            cell.set_facecolor(BG_DARK)
            cell.get_text().set_color(TEXT_COLOR)
            cell.get_text().set_fontweight("bold")
        else:
            cell.set_facecolor(BG_CARD if row % 2 == 0 else BG_DARK)
            if col == 5:
                cell.get_text().set_color("#f04747")
                cell.get_text().set_fontweight("bold")
            elif col == 4:
                cell.get_text().set_color("#ffe066")
            elif col in (0, 1):
                cell.get_text().set_color(MUTED_COLOR)
            else:
                cell.get_text().set_color(TEXT_COLOR)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


def _resolve_display_name(query: dict) -> str:
    """Extract a clean display name from the built query's pn field."""
    pn_val = query.get("pn", {})
    if isinstance(pn_val, dict) and "$in" in pn_val:
        candidates = pn_val["$in"]
        return min(candidates, key=len) if candidates else "Unknown"
    if isinstance(pn_val, dict) and "$regex" in pn_val:
        raw = pn_val["$regex"].strip("^$")
        return resolve_pokemon_name(raw) or raw or "Unknown"
    return str(pn_val) or "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# CORE PRICE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def _analyse(
    query: dict,
    filters_str: str,
    limit: int | None = None,
) -> tuple[discord.ui.LayoutView, io.BytesIO | None]:
    """
    Returns (view, outlier_image_buf | None).
    """
    name     = _resolve_display_name(query)
    is_shiny = query.get("sh") is True
    is_gmax  = query.get("gx") is True

    # Base query: name + shiny + gmax only (wider pool for premium comparisons)
    base_query: dict = {k: query[k] for k in ("pn", "sh", "gx") if k in query}

    projection = {
        "bid": 1, "iv": 1, "lv": 1,
        "spe": 1, "atk": 1, "mv": 1,
        "gen": 1, "sh": 1, "gx": 1,
        "ts": 1, "aid": 1,
    }

    def _fetch(q: dict, lim: int | None = limit) -> list[dict]:
        cur = _col.find(q, projection).sort("ts", -1)
        if lim is not None:
            cur = cur.limit(lim)
        return list(cur)

    # Exact match — the full user query (all filters applied)
    exact_raw = _fetch(query)
    if not exact_raw:
        return _error_view("❌ No past sales found matching your filters."), None

    # Base records — used for premium estimates (no IV/level/move/etc filters)
    base_raw = _fetch(base_query)

    # ── Outlier detection ─────────────────────────────────────────────────────
    exact_clean, exact_outliers = _remove_outliers(exact_raw)
    base_clean,  _              = _remove_outliers(base_raw)

    # ── IV-comparable band ────────────────────────────────────────────────────
    iv_cond   = query.get("iv")
    iv_target = None
    if isinstance(iv_cond, dict):
        if "$gte" in iv_cond and "$lte" in iv_cond:
            iv_target = (iv_cond["$gte"] + iv_cond["$lte"]) / 2
        elif "$gte" in iv_cond:
            iv_target = iv_cond["$gte"]
        elif "$eq" in iv_cond:
            iv_target = iv_cond["$eq"]

    # Choose stat pool: exact clean → IV-band → all base
    if len(exact_clean) >= 3:
        stat_records = exact_clean
        stat_label   = "exact match"
    elif iv_target is not None:
        lo = iv_target - IV_BAND
        hi = iv_target + IV_BAND
        comp_raw          = _fetch({**base_query, "iv": {"$gte": lo, "$lte": hi}})
        comp_clean, _     = _remove_outliers(comp_raw)
        if len(comp_clean) >= 3:
            stat_records = comp_clean
            stat_label   = f"comparable ±{IV_BAND:.0f}% IV"
        else:
            stat_records = base_clean
            stat_label   = "all sales (IV band too narrow)"
    else:
        stat_records = base_clean
        stat_label   = "all sales"

    if not stat_records:
        return _error_view("❌ Not enough sales data to analyse."), None

    stat_prices = np.array(_prices(stat_records), dtype=float)
    n           = len(stat_prices)
    p_median    = float(np.median(stat_prices))
    p_avg       = float(np.mean(stat_prices))
    p_min       = float(stat_prices.min())
    p_max       = float(stat_prices.max())
    p_std       = float(stat_prices.std())
    p25         = float(np.percentile(stat_prices, 25))
    p75         = float(np.percentile(stat_prices, 75))

    # ── Recent 5 sales ────────────────────────────────────────────────────────
    recent_five  = sorted(exact_raw, key=lambda r: r.get("ts", 0), reverse=True)[:5]
    outlier_aids = {r.get("aid") for r in exact_outliers}
    recent_lines = []
    for r in recent_five:
        ts   = r.get("ts")
        dt   = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %b %Y") if ts else "?"
        iv   = r.get("iv")
        iv_s = f"{iv:.1f}%" if iv is not None else "?"
        bid  = r.get("bid", 0)
        flag = " ⚠️" if r.get("aid") in outlier_aids else ""
        recent_lines.append(
            f"{REPLY} `{_fmt(bid)}` — {iv_s} IV — {dt} — `#{r.get('aid', '?')}`{flag}"
        )

    # ── Attribute premiums ────────────────────────────────────────────────────
    premiums: list[str] = []

    def _add(label: str, with_r: list[dict], without_r: list[dict]) -> None:
        line = _premium_line(label, _prices(with_r), _prices(without_r))
        if line:
            premiums.append(line)

    if not is_shiny and not is_gmax:
        _add("Shiny",
             [r for r in base_clean if r.get("sh")],
             [r for r in base_clean if not r.get("sh")])

    _add("Max Speed (31)",
         [r for r in stat_records if r.get("spe") == 31],
         [r for r in stat_records if r.get("spe") != 31])

    _add("Max Attack (31)",
         [r for r in stat_records if r.get("atk") == 31],
         [r for r in stat_records if r.get("atk") != 31])

    _add("0 Attack",
         [r for r in stat_records if r.get("atk") == 0],
         [r for r in stat_records if r.get("atk") not in (0, None)])

    _add("Split IV (50%)",
         [r for r in base_clean if r.get("iv") == 50.0],
         [r for r in base_clean if r.get("iv") != 50.0])

    _add("Low Level (<15)",
         [r for r in base_clean if (r.get("lv") or 100) < 15],
         [r for r in base_clean if (r.get("lv") or 100) >= 15])

    _add("Female",
         [r for r in base_clean if r.get("gen") == "Female"],
         [r for r in base_clean if r.get("gen") == "Male"])

    for clause in query.get("$and", []):
        mv = clause.get("mv", {})
        if isinstance(mv, dict) and "$elemMatch" in mv:
            regex = mv["$elemMatch"].get("$regex", "")
            if regex:
                _add(f"Move: {regex}",
                     [r for r in stat_records
                      if any(regex.lower() in str(m).lower() for m in (r.get("mv") or []))],
                     [r for r in stat_records
                      if not any(regex.lower() in str(m).lower() for m in (r.get("mv") or []))])

    # ── Outlier image ─────────────────────────────────────────────────────────
    outlier_buf: io.BytesIO | None = None
    if exact_outliers:
        outlier_buf = _build_outlier_image(
            sorted(exact_outliers, key=lambda r: r.get("bid", 0), reverse=True)
        )

    # ── Build text blocks ─────────────────────────────────────────────────────
    shiny_tag = "✨ Shiny " if is_shiny else ""
    gmax_tag  = "⚡ Gmax "  if is_gmax  else ""
    title     = f"## 💰 {shiny_tag}{gmax_tag}{name} — Price Check"

    limit_note = f"  •  last {limit:,} sales" if limit is not None else ""
    iv_note    = f"  •  IV ~{iv_target:.1f}% (±{IV_BAND:.0f}%)" if iv_target is not None else ""
    sub        = f"-# {n} sales analysed ({stat_label}{iv_note}{limit_note})"

    market_text = (
        f"**💵 What to sell / bid for**\n"
        f"{REPLY} **Target price:** `{_fmt(p_median)}` ← median\n"
        f"{REPLY} **Typical range:** `{_fmt(p25)}` – `{_fmt(p75)}`  "
        f"_({_confidence(n)})_"
    )

    stats_text = (
        f"**📊 Stats** _(outliers excluded)_\n"
        f"{REPLY} Avg `{_fmt(p_avg)}`  •  "
        f"Low `{_fmt(p_min)}`  •  "
        f"High `{_fmt(p_max)}`  •  "
        f"Std Dev `{_fmt(p_std)}`\n"
        f"{REPLY} Total sales: `{len(exact_raw):,}`"
        + (f"  •  Outliers excluded: `{len(exact_outliers)}`" if exact_outliers else "")
    )

    recent_text = (
        f"**🕐 Recent Sales**"
        + (" _(⚠️ = outlier)_" if any("⚠️" in l for l in recent_lines) else "")
        + "\n"
        + ("\n".join(recent_lines) if recent_lines else f"{REPLY} _No recent sales_")
    )

    premium_text = (
        f"**⚡ Attribute Premiums**  "
        f"_-# median difference vs without  •  min {MIN_PREMIUM_SAMPLE} sales each_\n"
        + ("\n".join(premiums) if premiums else f"{REPLY} _Not enough data_")
    )

    filters_display = filters_str.strip() or "no filters"
    accent = config.SHINY_EMBED_COLOR if is_shiny else config.EMBED_COLOR

    main_comps = [
        discord.ui.TextDisplay(content=title),
        discord.ui.TextDisplay(content=sub),
        discord.ui.TextDisplay(content=f"-# Filters: `{filters_display}`"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=market_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=stats_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=recent_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=premium_text),
    ]

    if outlier_buf:
        class PriceViewWithOutliers(discord.ui.LayoutView):
            container1 = discord.ui.Container(*main_comps, accent_colour=accent)
            container2 = discord.ui.Container(
                discord.ui.TextDisplay(content=(
                    f"⚠️ **{len(exact_outliers)} outlier sale(s) excluded from stats**\n"
                    f"_These sales were far above the typical price range and would skew the numbers._"
                )),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media="attachment://outliers.png"),
                ),
                accent_colour=discord.Colour(0xf04747),
            )
            def __init__(self):
                super().__init__(timeout=None)
        return PriceViewWithOutliers(), outlier_buf
    else:
        class PriceView(discord.ui.LayoutView):
            container = discord.ui.Container(*main_comps, accent_colour=accent)
            def __init__(self):
                super().__init__(timeout=None)
        return PriceView(), None


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
          j!price --name dragonite --limit 50
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

        raw             = filters.split() if filters else []
        query, _, limit = build_query(raw, expand_name_by_dex=True)

        if hasattr(ctx, "interaction") and ctx.interaction:
            await ctx.defer()
        else:
            await ctx.typing()

        view, outlier_buf = _analyse(query, filters, limit=limit)

        ref = ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None

        if outlier_buf:
            await ctx.send(
                view=view,
                file=discord.File(outlier_buf, filename="outliers.png"),
                reference=ref,
                mention_author=False,
            )
        else:
            await ctx.send(
                view=view,
                reference=ref,
                mention_author=False,
            )


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Price(bot))
