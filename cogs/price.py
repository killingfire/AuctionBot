"""
cogs/price.py – Smart price lookup for Pokémon auctions.

Redesigned to mirror graph.py capabilities:
  • Last 20-30 auctions limit (user configurable, with warnings if old)
  • Remove low outliers, display as range + button
  • Show price range instead of single values
  • Add disclaimer about price variance
  • "View Graph" button linking to graph.py
  • All filter support from graph.py
  • Configurable limit with tips

Uses the same filter system as auction search (expand_name_by_dex=True).
Outliers are excluded from stats using the same 3×IQR fence as graph.py.

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

# ─── DB ─────────────────────────────────────────────────────────────
_mongo = MongoClient(config.MONGO_URI)
_db    = _mongo[config.MONGO_DB_NAME]
_col   = _db[config.MONGO_COLLECTION]

# ─── Name flag aliases (derived from filters.py — stays in sync automatically) ─
_NAME_FLAGS: frozenset[str] = frozenset(
    ["--name"] + FLAG_DEFINITIONS["--name"].get("aliases", [])
)

# Price cog configuration
DEFAULT_LIMIT = 25          # Default: last 20-30 auctions
MAX_LIMIT = 200             # Don't allow more than 200
DATA_AGE_WARNING = 30       # Days — warn if oldest data is older than this

# Outlier fence multiplier — same as graph.py
OUTLIER_FENCE = 3.0

# Theme — matches graph.py
BG_DARK     = "#0f1117"
BG_CARD     = "#1a1d27"
GRID_COLOR  = "#2a2d3a"
TEXT_COLOR  = "#e8eaf0"
MUTED_COLOR = "#6c7086"


# ──────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    """Format price with appropriate suffix (M, k, etc)."""
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
    """Render an error message as a view."""
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


def _confidence(n: int) -> str:
    """Return confidence indicator based on sample size."""
    if n >= 30:
        return "🟢 High confidence"
    if n >= 15:
        return "🟡 Moderate confidence"
    if n >= 5:
        return "🟠 Low-moderate confidence"
    return "🔴 Very low confidence — tiny sample"


def _prices(records: list[dict]) -> list[float]:
    """Extract prices from records."""
    return [r["bid"] for r in records if r.get("bid") is not None]


def _remove_outliers(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split records into (clean, outliers) using the same 3×IQR upper fence
    as graph.py, but ALSO remove low outliers using the percentage-of-median approach.
    Falls back to returning all records as clean if fewer than 3 would remain.
    """
    if len(records) < 4:
        return records, []

    prices = np.array(_prices(records), dtype=float)
    q1, q3 = np.percentile(prices, 25), np.percentile(prices, 75)
    median = float(np.median(prices))
    iqr = q3 - q1

    # High fence: standard 3×IQR above Q3
    upper_fence = q3 + OUTLIER_FENCE * iqr if iqr > 0 else prices.max()

    # Low fence: the higher of IQR fence or percentage-of-median
    iqr_lower = q1 - OUTLIER_FENCE * iqr
    pct_lower = median * 0.20
    lower_fence = max(iqr_lower, pct_lower)

    high_outlier_mask = prices > upper_fence
    low_outlier_mask = (lower_fence > 0) & (prices < lower_fence)
    outlier_mask = high_outlier_mask | low_outlier_mask
    plot_mask = ~outlier_mask

    clean = [r for r, m in zip(records, plot_mask) if m]
    outliers = [r for r, m in zip(records, outlier_mask) if m]

    if len(clean) < 3:
        return records, []

    return clean, outliers


def _build_outlier_image(outliers: list[dict]) -> io.BytesIO:
    """
    Table image for outlier sales — matches graph.py style exactly.
    Columns: #, Type, Auction ID, Date, Level, IV%, Winning Bid
    """
    n = len(outliers)
    row_h_in = 0.38
    head_h = 0.50
    fig_h = head_h + n * row_h_in

    fig, ax = plt.subplots(figsize=(11, fig_h), facecolor=BG_DARK)
    ax.set_facecolor(BG_DARK)
    ax.axis("off")

    headers = ["#", "Type", "Auction ID", "Date", "Level", "IV %", "Winning Bid"]
    col_widths = [0.04, 0.08, 0.16, 0.20, 0.09, 0.12, 0.20]

    rows = []
    for i, r in enumerate(outliers):
        ts = r.get("ts")
        date = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %b %Y")
            if ts
            else "?"
        )
        aid = str(r.get("aid", "?"))
        level = str(r.get("lv", "???"))
        iv = r.get("iv")
        iv_s = f"{iv:.2f}%" if iv is not None else "???"
        bid = r.get("bid", 0)
        
        # Determine if high or low outlier
        kind = r.get("_outlier_kind", "high")
        kind_label = "▲ High" if kind == "high" else "▼ Low"
        
        rows.append([str(i + 1), kind_label, aid, date, level, iv_s, _fmt(bid)])

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
            if col == 6:  # Winning bid
                kind_val = rows[row - 1][1] if row <= len(rows) else ""
                color = "#ef476f" if "High" in kind_val else "#ffd166"
                cell.get_text().set_color(color)
                cell.get_text().set_fontweight("bold")
            elif col == 1:  # Type column
                kind_val = rows[row - 1][1] if row <= len(rows) else ""
                color = "#ef476f" if "High" in kind_val else "#ffd166"
                cell.get_text().set_color(color)
                cell.get_text().set_fontweight("bold")
            elif col == 5:
                cell.get_text().set_color("#ffd166")  # IV % accent gold
            elif col in (0, 2):
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


def _data_age_warning(records: list[dict]) -> str | None:
    """
    Return warning text if data is too old (default: >30 days).
    Returns None if data is recent enough.
    """
    if not records:
        return None
    
    # Get oldest record
    oldest_ts = min(r.get("ts", 0) for r in records)
    oldest_dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    age_days = (now - oldest_dt).days
    
    if age_days > DATA_AGE_WARNING:
        return (
            f"⚠️ **Data is {age_days} days old** — market prices may have changed. "
            f"Consider increasing `--limit` to get more recent auctions."
        )
    return None


# ──────────────────────────────────────────────────────────────────
# CORE PRICE ANALYSIS
# ──────────────────────────────────────────────────────────────────

def _analyse(
    query: dict,
    filters_str: str,
    limit: int | None = None,
) -> tuple[discord.ui.LayoutView, io.BytesIO | None, str]:
    """
    Returns (view, outlier_image_buf | None, filter_string_for_graph_cmd).
    """
    name = _resolve_display_name(query)
    is_shiny = query.get("sh") is True
    is_gmax = query.get("gx") is True

    projection = {
        "bid": 1, "iv": 1, "lv": 1,
        "spe": 1, "atk": 1, "mv": 1,
        "gen": 1, "sh": 1, "gx": 1,
        "ts": 1, "aid": 1,
    }

    # Apply default limit if not specified
    if limit is None:
        limit = DEFAULT_LIMIT

    # Fetch records — most recent first
    cur = _col.find(query, projection).sort("ts", -1).limit(limit)
    exact_raw = list(cur)

    if not exact_raw:
        return (
            _error_view("❌ No past sales found matching your filters."),
            None,
            filters_str,
        )

    # ── Outlier detection ─────────────────────────────────────────────────────
    exact_clean, exact_outliers = _remove_outliers(exact_raw)

    if not exact_clean:
        return (
            _error_view("❌ Not enough non-outlier sales data to analyse."),
            None,
            filters_str,
        )

    # ── Mark outlier kind (high or low) ───────────────────────────────────────
    prices = np.array(_prices(exact_raw), dtype=float)
    q1, q3 = np.percentile(prices, 25), np.percentile(prices, 75)
    median = float(np.median(prices))
    iqr = q3 - q1
    
    upper_fence = q3 + OUTLIER_FENCE * iqr if iqr > 0 else prices.max()
    iqr_lower = q1 - OUTLIER_FENCE * iqr
    pct_lower = median * 0.20
    lower_fence = max(iqr_lower, pct_lower)

    for r in exact_outliers:
        bid = r.get("bid", 0)
        if bid > upper_fence:
            r["_outlier_kind"] = "high"
        else:
            r["_outlier_kind"] = "low"

    # ── Statistics ────────────────────────────────────────────────────────────
    stat_prices = np.array(_prices(exact_clean), dtype=float)
    n_clean = len(stat_prices)
    n_total = len(exact_raw)

    p_min = float(stat_prices.min())
    p_max = float(stat_prices.max())
    p_avg = float(stat_prices.mean())
    p_median = float(np.median(stat_prices))
    p_std = float(stat_prices.std())
    p25 = float(np.percentile(stat_prices, 25))
    p75 = float(np.percentile(stat_prices, 75))

    # ── Recent 5 sales ───────────────────────────────────────────────────────
    recent_five = sorted(exact_raw, key=lambda r: r.get("ts", 0), reverse=True)[:5]
    outlier_aids = {r.get("aid") for r in exact_outliers}
    recent_lines = []
    for r in recent_five:
        ts = r.get("ts")
        dt = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %b %Y")
            if ts
            else "?"
        )
        iv = r.get("iv")
        iv_s = f"{iv:.1f}%" if iv is not None else "?"
        bid = r.get("bid", 0)
        flag = " ⚠️" if r.get("aid") in outlier_aids else ""
        recent_lines.append(
            f"{REPLY} `{_fmt(bid)}` — {iv_s} IV — {dt} — `#{r.get('aid', '?')}`{flag}"
        )

    # ── Outlier image ─────────────────────────────────────────────────────────
    outlier_buf: io.BytesIO | None = None
    if exact_outliers:
        outlier_buf = _build_outlier_image(
            sorted(exact_outliers, key=lambda r: r.get("bid", 0), reverse=True)
        )

    # ── Build text blocks ─────────────────────────────────────────────────────
    shiny_tag = "✨ Shiny " if is_shiny else ""
    gmax_tag = "⚡ Gmax " if is_gmax else ""
    title = f"## 💰 {shiny_tag}{gmax_tag}{name} — Price Check"

    # Data age and limits
    oldest_ts = min(r.get("ts", 0) for r in exact_raw) if exact_raw else 0
    oldest_dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
    newest_dt = datetime.fromtimestamp(max(r.get("ts", 0) for r in exact_raw), tz=timezone.utc)
    date_span = (newest_dt - oldest_dt).days

    age_warning = _data_age_warning(exact_raw)
    limit_note = f"  •  last {limit} sales" if limit is not None else ""
    date_note = f"  •  {date_span}d span ({oldest_dt.strftime('%d %b')} → {newest_dt.strftime('%d %b')})"
    sub = f"-# {n_clean} sales analysed (outliers excluded){limit_note}{date_note}\n-# ⚠️ Prices may vary — we don't guarantee accuracy"

    # ── Main stats ────────────────────────────────────────────────────────────
    market_text = (
        f"**💵 Typical Price Range**\n"
        f"{REPLY} **25th–75th percentile:** `{_fmt(p25)}` – `{_fmt(p75)}`  "
        f"_({_confidence(n_clean)})_\n"
        f"{REPLY} **Median (midpoint):** `{_fmt(p_median)}`  ← suggested start\n"
        f"{REPLY} **Mean (average):** `{_fmt(p_avg)}`"
    )

    stats_text = (
        f"**📊 Full Stats** _(outliers excluded, n={n_clean})_\n"
        f"{REPLY} Low: `{_fmt(p_min)}`  •  High: `{_fmt(p_max)}`  •  Spread: `{_fmt(p_max - p_min)}`\n"
        f"{REPLY} Std Dev: `{_fmt(p_std)}`  _(higher = less stable)_\n"
        f"{REPLY} Total sales processed: `{n_total:,}`"
        + (f"  •  Excluded: `{len(exact_outliers)}` outlier(s)" if exact_outliers else "")
    )

    recent_text = (
        f"**🕐 Recent Sales**"
        + (" _(⚠️ = outlier, excluded from stats)_" if any("⚠️" in l for l in recent_lines) else "")
        + "\n"
        + ("\n".join(recent_lines) if recent_lines else f"{REPLY} _No recent sales_")
    )

    filters_display = filters_str.strip() or "no filters"
    accent = config.SHINY_EMBED_COLOR if is_shiny else config.EMBED_COLOR

    main_comps = [
        discord.ui.TextDisplay(content=title),
        discord.ui.TextDisplay(content=sub),
        discord.ui.TextDisplay(content=f"-# Filters: `{filters_display}`"),
    ]

    if age_warning:
        main_comps.append(discord.ui.TextDisplay(content=age_warning))

    main_comps.extend([
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=market_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=stats_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=recent_text),
    ])

    if outlier_buf:
        class PriceViewWithOutliers(discord.ui.LayoutView):
            container1 = discord.ui.Container(*main_comps, accent_colour=accent)
            container2 = discord.ui.Container(
                discord.ui.TextDisplay(content=(
                    f"⚠️ **{len(exact_outliers)} outlier sale(s) excluded from stats**\n"
                    f"_▲ Overpriced — paid way more than typical. ▼ Sniped — got a bargain. "
                    f"Both skew the numbers, so they're hidden by default._"
                )),
                discord.ui.Separator(
                    visible=True, spacing=discord.SeparatorSpacing.small
                ),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media="attachment://outliers.png"),
                ),
                accent_colour=discord.Colour(0xef476f),
            )

            def __init__(self):
                super().__init__(timeout=300)

        return PriceViewWithOutliers(), outlier_buf, filters_str
    else:
        class PriceView(discord.ui.LayoutView):
            container = discord.ui.Container(*main_comps, accent_colour=accent)

            def __init__(self):
                super().__init__(timeout=300)

        return PriceView(), None, filters_str


# ──────────────────────────────────────────────────────────────────
# COG
# ──────────────────────────────────────────────────────────────────

class Price(commands.Cog):
    """Smart price lookup using historical auction data"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="price", aliases=["pc", "pricecheck"])
    @app_commands.describe(
        filters="Same filters as auction search e.g: --name eevee --shiny --iv >85 --limit 50"
    )
    async def price_cmd(self, ctx: commands.Context, *, filters: str = ""):
        """
        Price check a Pokémon using historical auction data.

        **Features:**
          • Shows price range (25th–75th percentile) + median
          • Analyzes last 20-30 auctions by default
          • Removes outliers (overpriced/sniped sales)
          • Warns if data is too old
          • Full filter support — same as /graph and /auction search
          • Link to /graph for detailed price history

        **Examples:**
          j!price --name garchomp --iv 90
          j!price --name eevee --shiny
          j!price --name charizard --gmax --iv >85 --limit 50
          j!price --name umbreon --move wish
          j!price --name dragonite --type flying

        **Tips:**
          • Use `--limit 50` to check more (or fewer) recent auctions
          • `--limit 100` for deeper analysis, `--limit 10` for quick check
          • Default is 25 auctions; raises warning if data is >30 days old
          • All `/graph` and `/auction search` filters supported
        """
        if not any(t in _NAME_FLAGS for t in (filters.split() if filters else [])):
            await ctx.send(
                view=_error_view(
                    f"❌ Please specify a Pokémon name.\n"
                    f"{REPLY} Example: `j!price --name garchomp --iv 90`\n"
                    f"{REPLY} Example: `j!price --name eevee --shiny --limit 50`\n"
                    f"{REPLY} Use `j!help price` for all options."
                ),
                reference=ctx.message,
                mention_author=False,
            )
            return

        raw = filters.split() if filters else []
        query, _, limit = build_query(raw, expand_name_by_dex=True)

        # Validate and cap limit
        if limit is None:
            limit = DEFAULT_LIMIT
        else:
            limit = min(limit, MAX_LIMIT)

        if hasattr(ctx, "interaction") and ctx.interaction:
            await ctx.defer()
        else:
            await ctx.typing()

        view, outlier_buf, graph_filters = _analyse(query, filters, limit=limit)

        ref = (
            ctx.message
            if not (hasattr(ctx, "interaction") and ctx.interaction)
            else None
        )

        # Build button row
        class PriceViewWithButtons(discord.ui.LayoutView):
            def __init__(self, base_view: discord.ui.LayoutView):
                super().__init__(timeout=300)
                # Copy containers from base view
                for attr in dir(base_view):
                    if attr.startswith("container") or attr.startswith("action_row"):
                        setattr(self, attr, getattr(base_view, attr))

            action_row = discord.ui.ActionRow()

            async def on_ready(self):
                """Populate action row after containers are set."""
                pass

        # Create button for graph
        class ViewGraphBtn(discord.ui.Button):
            def __init__(self, graph_filters_str: str):
                super().__init__(
                    style=discord.ButtonStyle.primary,
                    label="📈 View Detailed Graph",
                    custom_id="pc_view_graph",
                )
                self.graph_filters = graph_filters_str

            async def callback(self, interaction: discord.Interaction):
                # Use the same filters but direct them to /graph
                await interaction.response.send_message(
                    content=(
                        f"Use this command to see the detailed price history graph:\n"
                        f"`j!graph {self.graph_filters}`"
                    ),
                    ephemeral=True,
                )

        btn_list = [ViewGraphBtn(graph_filters)]

        # If we have outliers, add a button to view them
        if outlier_buf:
            outlier_buf.seek(0)

        # Create custom view with buttons
        base_view_class = type(view)
        
        class EnhancedPriceView(base_view_class):
            def __init__(self):
                super().__init__(timeout=300)
                # Add action row with button
                self.action_row = discord.ui.ActionRow(*btn_list)

        enhanced_view = EnhancedPriceView()

        if outlier_buf:
            outlier_buf.seek(0)
            await ctx.send(
                view=enhanced_view,
                file=discord.File(outlier_buf, filename="outliers.png"),
                reference=ref,
                mention_author=False,
            )
        else:
            await ctx.send(
                view=enhanced_view,
                reference=ref,
                mention_author=False,
            )


# ──────────────────────────────────────────────────────────────────
# SETUP
# ──────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Price(bot))
