"""
cogs/graph.py – Price history graph for Pokémon auctions.
Uses the same filter system as auction search.
Generates a dark-themed matplotlib chart and sends it as a Discord image.

Field mapping (DB short name → meaning):
  ts   = unix_timestamp      bid  = winning_bid
  pn   = pokemon_name        sh   = shiny
  gx   = gmax                iv   = total_iv_percent
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import discord
import matplotlib
matplotlib.use("Agg")  # non-interactive backend, must be set before pyplot import
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
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

# ─── Theme colours ────────────────────────────────────────────────────────────
BG_DARK       = "#1e1f22"
BG_CARD       = "#2b2d31"
GRID_COLOR    = "#3a3d44"
TEXT_COLOR    = "#dcddde"
MUTED_COLOR   = "#72767d"

_PALETTE = {
    "shiny":  {"dot": "#ffe066", "line": "#ffc300", "fill": "#ffc30033", "tag": "[Shiny]"},
    "gmax":   {"dot": "#ff7043", "line": "#ff5722", "fill": "#ff572233", "tag": "[Gmax]"},
    "normal": {"dot": "#7289da", "line": "#5865f2", "fill": "#5865f233", "tag": ""},
}

_DISCORD_TAG = {
    "shiny":  "✨ Shiny",
    "gmax":   "⚡ Gigantamax",
    "normal": "",
}

MAX_POINTS = 800

# Only include auctions from this year onwards when building graphs.
# Change this value to shift the global cutoff.
GRAPH_START_YEAR = 2024


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _detect_variant(query: dict) -> str:
    """
    Detect shiny/gmax variant from query.
    Must check for exactly True — --noshiny sets sh={"$ne": True} which is
    truthy but must NOT be treated as a shiny query.
    """
    if query.get("sh") is True:
        return "shiny"
    if query.get("gx") is True:
        return "gmax"
    return "normal"


def _format_price(val: float) -> str:
    if val >= 1_000_000:
        return f"{val/1_000_000:.2f}M"
    if val >= 100_000:
        return f"{val/1_000:.1f}k"
    if val >= 10_000:
        return f"{val/1_000:.1f}k"
    if val >= 1_000:
        return f"{val/1_000:.2f}k"
    return f"{int(val):,}"


def _smart_yticks(p_min: float, p_max: float) -> np.ndarray:
    price_range = p_max - p_min
    if price_range == 0:
        price_range = p_max or 1
    raw_step    = price_range / 6
    magnitude   = 10 ** np.floor(np.log10(raw_step)) if raw_step > 0 else 1
    clean_steps = [1, 2, 2.5, 5, 10]
    step  = min(clean_steps, key=lambda s: abs(s * magnitude - raw_step)) * magnitude
    start = np.floor(max(0, p_min - price_range * 0.1) / step) * step
    stop  = np.ceil((p_max + price_range * 0.1) / step) * step
    return np.arange(start, stop + step, step)


def _rolling_average(prices: np.ndarray, window: int) -> np.ndarray:
    if len(prices) < window:
        return prices.copy()
    kernel = np.ones(window) / window
    return np.convolve(prices, kernel, mode="same")


def _percentile_band(prices: np.ndarray, dates, window: int = 30):
    p25 = np.empty(len(prices))
    p75 = np.empty(len(prices))
    ts  = np.array([d.timestamp() for d in dates])
    day = 86_400
    for i, t in enumerate(ts):
        mask    = np.abs(ts - t) <= window * day / 2
        nearby  = prices[mask]
        p25[i]  = np.percentile(nearby, 25)
        p75[i]  = np.percentile(nearby, 75)
    return p25, p75


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_graph(records: list[dict], query: dict, query_str: str) -> io.BytesIO:
    """
    Build a dark-themed price history chart and return a PNG BytesIO buffer.
    Records use short field names: ts = unix_timestamp, bid = winning_bid, pn = pokemon_name.
    """
    records = sorted(records, key=lambda r: r.get("ts", 0))

    if len(records) > MAX_POINTS:
        step    = len(records) // MAX_POINTS
        records = records[::step]

    # Short field names: ts, bid, pn
    dates  = [datetime.fromtimestamp(r["ts"], tz=timezone.utc) for r in records]
    prices = np.array([r["bid"] for r in records], dtype=float)

    q1, q3  = np.percentile(prices, 25), np.percentile(prices, 75)
    iqr     = q3 - q1
    fence   = q3 + 3.0 * iqr if iqr > 0 else prices.max()

    outlier_mask   = prices > fence
    plot_mask      = ~outlier_mask
    outlier_dates   = [d for d, m in zip(dates, outlier_mask) if m]
    outlier_prices  = prices[outlier_mask]
    outlier_records = [r for r, m in zip(records, outlier_mask) if m]

    dates_plot  = [d for d, m in zip(dates, plot_mask) if m]
    prices_plot = prices[plot_mask]

    if len(prices_plot) < 3:
        dates_plot    = dates
        prices_plot   = prices
        outlier_dates   = []
        outlier_prices  = np.array([])
        outlier_records = []

    total      = len(prices)
    p_min      = prices.min()
    p_max      = prices.max()
    p_avg      = prices_plot.mean()
    p_med      = np.median(prices_plot)
    p_std      = prices_plot.std()
    n_outliers = int(outlier_mask.sum())

    if prices_plot.max() == prices_plot.min():
        prices_plot = prices_plot + np.linspace(-0.5, 0.5, len(prices_plot))

    x_num               = np.arange(len(prices_plot), dtype=float)
    slope, intercept    = np.polyfit(x_num, prices_plot, 1)
    trend_arrow         = "▲" if slope > 0 else "▼"
    trend_color         = "#43b581" if slope > 0 else "#f04747"

    window   = max(5, len(prices_plot) // 10)
    roll_avg = _rolling_average(prices_plot, window)

    do_band = len(prices_plot) >= 20
    if do_band:
        p25, p75 = _percentile_band(prices_plot, dates_plot, window=30)

    trend_line = slope * x_num + intercept

    variant = _detect_variant(query)
    pal     = _PALETTE[variant]

    fig = plt.figure(figsize=(12, 7), facecolor=BG_DARK)
    gs  = fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.08)
    ax  = fig.add_subplot(gs[0])
    axs = fig.add_subplot(gs[1])

    ax.set_facecolor(BG_CARD)
    axs.set_facecolor(BG_DARK)
    axs.axis("off")

    if do_band:
        ax.fill_between(dates_plot, p25, p75, color=pal["fill"], linewidth=0, label="25–75th pct")

    ax.scatter(
        dates_plot, prices_plot,
        color=pal["dot"], s=18, alpha=0.55, zorder=3, linewidths=0, label="Sales",
    )

    if len(outlier_prices) > 0:
        ax.scatter(
            outlier_dates, [prices_plot.max()] * len(outlier_dates),
            color="#f04747", marker="^", s=40, zorder=5,
            linewidths=0, label=f"Outlier(s) ({len(outlier_prices)})",
        )

    ax.plot(dates_plot, roll_avg, color=pal["line"], linewidth=2.2,
            label=f"Avg (±{window})", zorder=4)

    ax.plot(dates_plot, trend_line, color=trend_color, linewidth=1.2,
            linestyle="--", alpha=0.75, label="Trend", zorder=4)

    idx_max = int(np.argmax(prices_plot))
    idx_min = int(np.argmin(prices_plot))
    for idx, label, color in [
        (idx_max, f"Max\n{_format_price(prices_plot.max())}", "#43b581"),
        (idx_min, f"Min\n{_format_price(prices_plot.min())}", "#f04747"),
    ]:
        ax.annotate(
            label,
            xy=(dates_plot[idx], prices_plot[idx]),
            xytext=(20 if idx == idx_max else 20, 16 if idx == idx_max else -28),
            textcoords="offset points",
            ha="center", va="bottom",
            color=color, fontsize=8, fontweight="bold",
            arrowprops=dict(arrowstyle="-", color=color, lw=1),
        )

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
    fig.autofmt_xdate(rotation=30, ha="right")

    yticks = _smart_yticks(prices_plot.min(), prices_plot.max())
    ax.set_yticks(yticks)
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
    )
    ax.tick_params(colors=TEXT_COLOR, labelsize=9)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_COLOR)
    ax.grid(color=GRID_COLOR, linestyle="-", linewidth=0.6, alpha=0.8)
    ax.set_xlim(dates_plot[0], dates_plot[-1])

    pm_clean = prices_plot.min()
    px_clean = prices_plot.max()
    y_range  = px_clean - pm_clean or px_clean or 1
    ax.set_ylim(
        max(0, pm_clean - y_range * 0.18),
        px_clean + y_range * 0.25,
    )

    ax.set_ylabel("Winning Bid (pc)", color=TEXT_COLOR, fontsize=10)
    ax.yaxis.label.set_color(TEXT_COLOR)

    tag        = pal["tag"]
    # Short field name: pn = pokemon_name
    name       = records[0].get("pn", "Unknown")
    full_title = f"[{tag}] {name}".strip() if tag else name
    date_first = dates[0].strftime("%-d %b %Y")
    date_last  = dates[-1].strftime("%-d %b %Y")
    span_days  = (dates[-1] - dates[0]).days
    ax.set_title(
        f"{full_title}  •  Price History  •  {date_first} → {date_last} ({span_days}d)",
        color=TEXT_COLOR, fontsize=14, fontweight="bold", pad=10,
    )
    if query_str and query_str.lower() not in ("all auctions",):
        ax.set_xlabel(f"Filters: {query_str}", color=MUTED_COLOR, fontsize=8)

    ax.legend(
        facecolor=BG_DARK, edgecolor=GRID_COLOR,
        labelcolor=TEXT_COLOR, fontsize=8,
        loc="upper left",
        bbox_to_anchor=(-0.12, -0.03),
        bbox_transform=ax.transAxes,
        borderpad=0.6,
        handlelength=1.5,
    )

    stats_items = [
        ("Auctions",     f"{total:,}"),
        ("Min",          _format_price(p_min)),
        ("Chart Max",    _format_price(prices_plot.max())),
        ("All-time Max", _format_price(p_max)),
        ("Avg",          _format_price(p_avg)),
        ("Median",       _format_price(p_med)),
        ("Std Dev",      _format_price(p_std)),
        ("Trend",        f"{trend_arrow} {_format_price(abs(slope))}/sale"),
        ("Outliers",     f"{n_outliers} hidden" if n_outliers else "None"),
    ]

    step_x = 1.0 / len(stats_items)
    for i, (label, value) in enumerate(stats_items):
        x = i * step_x + step_x * 0.5
        axs.text(x, 0.72, label, ha="center", va="center",
                 color=MUTED_COLOR, fontsize=7.5, transform=axs.transAxes)
        axs.text(x, 0.22, value, ha="center", va="center",
                 color=TEXT_COLOR, fontsize=9, fontweight="bold",
                 transform=axs.transAxes)

    fig.add_artist(matplotlib.lines.Line2D(
        [0.05, 0.95], [0.175, 0.175],
        transform=fig.transFigure,
        color=GRID_COLOR, linewidth=0.8,
    ))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    # Each outlier entry: (date, price, record) so callers have full metadata
    return buf, list(zip(outlier_dates, outlier_prices.tolist(), outlier_records))


def build_outlier_image(
    outliers: list[tuple],
    pokemon_name: str,
    variant: str,
) -> io.BytesIO:
    """
    Build a table image for outlier sales.
    Each entry in outliers is a (date, price, record) tuple.
    Columns: #, Auction ID, Date, Level, IV%, Winning Bid
    """
    n        = len(outliers)
    row_h_in = 0.38
    head_h   = 0.50
    fig_h    = head_h + n * row_h_in

    # Wider figure to accommodate extra columns
    fig, ax = plt.subplots(figsize=(10, fig_h), facecolor=BG_DARK)
    ax.set_facecolor(BG_DARK)
    ax.axis("off")

    headers    = ["#", "Auction ID", "Date", "Level", "IV %", "Winning Bid"]
    col_widths = [0.05, 0.18, 0.22, 0.10, 0.13, 0.22]

    rows = []
    for i, (d, p, r) in enumerate(outliers):
        aid   = str(r.get("aid", "?"))
        date  = d.strftime("%-d %b %Y")
        level = str(r.get("lv", "???"))
        iv    = r.get("iv")
        iv_s  = f"{iv:.2f}%" if iv is not None else "???"
        rows.append([str(i + 1), aid, date, level, iv_s, _format_price(p)])

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
                # Winning bid — red + bold
                cell.get_text().set_color("#f04747")
                cell.get_text().set_fontweight("bold")
            elif col == 4:
                # IV % — highlight in accent gold
                cell.get_text().set_color("#ffe066")
            elif col == 0:
                # Row number — muted
                cell.get_text().set_color(MUTED_COLOR)
            elif col == 1:
                # Auction ID — muted but readable
                cell.get_text().set_color(MUTED_COLOR)
            else:
                cell.get_text().set_color(TEXT_COLOR)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
# ERROR VIEW
# ─────────────────────────────────────────────────────────────────────────────

def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class Graph(commands.Cog):
    """Price history graphs for Pokémon auctions"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="graph", aliases=["g", "chart"])
    @app_commands.describe(filters="Same filters as auction search e.g: --name pikachu --shiny --iv >80")
    async def graph_command(self, ctx: commands.Context, *, filters: str = ""):
        """
        Show a price history graph for a Pokémon.

        Uses the same filters as `j!a s`.
        Examples:
          j!g --name pikachu --shiny
          j!g --name charizard --gmax
          j!g --name mewtwo --iv >90 --sort price
          j!g --name goomy --limit 10
        """
        if not any(t in _NAME_FLAGS for t in (filters.split() if filters else [])):
            await ctx.send(
                view=_error_view(
                    f"❌ Please specify a Pokémon name.\n"
                    f"{REPLY} Example: `j!g --name pikachu --shiny`\n"
                    f"{REPLY} Example: `j!g --name charizard --gmax`"
                ),
                reference=ctx.message,
                mention_author=False,
            )
            return

        raw              = filters.split() if filters else []
        query, _, limit  = build_query(raw, expand_name_by_dex=True)
        display_str      = filters.strip()

        # Only pull fields we actually need (short names)
        projection = {
            "ts":  1,   # unix_timestamp
            "bid": 1,   # winning_bid
            "pn":  1,   # pokemon_name
            "sh":  1,   # shiny
            "gx":  1,   # gmax
            "iv":  1,   # total_iv_percent
            "aid": 1,   # auction_id  — needed for outlier table
            "lv":  1,   # level       — needed for outlier table
        }

        if hasattr(ctx, "interaction") and ctx.interaction:
            await ctx.defer()
        else:
            await ctx.typing()

        # Apply global year cutoff — only auctions from GRAPH_START_YEAR onwards
        year_ts = int(datetime(GRAPH_START_YEAR, 1, 1, tzinfo=timezone.utc).timestamp())

        # Sort newest-first so --limit keeps the most recent N records
        cursor = _col.find(
            {**query, "ts": {"$gte": year_ts, "$exists": True}, "bid": {"$exists": True}},
            projection,
        ).sort("ts", -1)

        if limit is not None:
            cursor = cursor.limit(limit)

        records = list(cursor)
        # Re-sort oldest→newest for the graph's X axis
        records.sort(key=lambda r: r.get("ts", 0))

        if not records:
            await ctx.send(
                view=_error_view("❌ No auctions found matching your filters."),
                reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                mention_author=False,
            )
            return

        if len(records) < 3:
            await ctx.send(
                view=_error_view(
                    f"❌ Only **{len(records)}** auction(s) found — need at least 3 to draw a meaningful graph.\n"
                    f"{REPLY} Try broadening your filters."
                ),
                reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                mention_author=False,
            )
            return

        try:
            buf, outliers = build_graph(records, query, display_str)
        except Exception as e:
            await ctx.send(
                view=_error_view(f"❌ Failed to generate graph: `{e}`"),
                reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                mention_author=False,
            )
            return

        # Short field name: pn = pokemon_name
        name      = records[0].get("pn", "Unknown")
        total     = len(records)
        variant   = _detect_variant(query)
        pal       = _PALETTE[variant]
        disc_tag  = _DISCORD_TAG[variant]
        accent    = config.SHINY_EMBED_COLOR if variant == "shiny" else config.EMBED_COLOR

        heading    = f"## {disc_tag} {name} — Price History".strip()
        limit_note = f"  •  last {limit:,} auctions" if limit is not None else ""
        sub        = f"_{total:,} auction(s) plotted{limit_note}  •  filters: `{display_str}`_"

        file = discord.File(buf, filename="graph.png")
        ref  = ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None

        legend_text = (
            f"**📖 Reading the Graph**\n"
            f"{REPLY} **Dots** — every individual auction sale, plotted by date and price\n"
            f"{REPLY} **Avg Line** — smoothed average price over time; shows the general price direction\n"
            f"{REPLY} **Trend** (dashed) — linear regression line; green means price rising over time, red means falling\n"
            f"{REPLY} **Shaded band** — the middle 50% of sales (25th–75th percentile); wide band = inconsistent prices, narrow = stable market\n"
            f"{REPLY} **Min / Max markers** — the single cheapest and most expensive sale ever recorded\n\n"
            f"**📊 Stats Bar**\n"
            f"{REPLY} **Auctions** — total number of sales plotted\n"
            f"{REPLY} **Min / Max** — lowest and highest winning bid\n"
            f"{REPLY} **Avg** — mean price across all auctions\n"
            f"{REPLY} **Median** — middle price (less affected by extreme outliers than avg)\n"
            f"{REPLY} **Std Dev** — how spread out prices are; high = big price swings, low = consistent\n"
            f"{REPLY} **Trend** — average price change per sale (▲ rising, ▼ falling)\n"
            f"{REPLY} **Outliers** — sales so far above the typical price range that they would squash all other data on the chart. Excluded from the graph and most stats, but listed separately below\n"
            f"{REPLY} **Chart Max** — highest sale visible on the graph (outliers excluded); what the market realistically peaks at\n"
            f"{REPLY} **All-time Max** — the absolute highest sale ever recorded, including outliers"
        )

        if outliers:
            out_buf  = build_outlier_image(outliers, name, variant)
            out_file = discord.File(out_buf, filename="outliers.png")

            class CombinedView(discord.ui.LayoutView):
                container1 = discord.ui.Container(
                    discord.ui.TextDisplay(content=heading),
                    discord.ui.TextDisplay(content=sub),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.MediaGallery(
                        discord.MediaGalleryItem(media="attachment://graph.png"),
                    ),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.TextDisplay(content=legend_text),
                    accent_colour=accent,
                )
                container2 = discord.ui.Container(
                    discord.ui.TextDisplay(
                        content=(
                            f"⚠️ **{len(outliers)} outlier sale(s) excluded from the graph**\n"
                            f"_These sales were far above typical prices and excluded to keep the Y-axis readable._"
                        )
                    ),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.MediaGallery(
                        discord.MediaGalleryItem(media="attachment://outliers.png"),
                    ),
                    accent_colour=discord.Colour(0xf04747),
                )
                def __init__(self):
                    super().__init__(timeout=300)

            await ctx.send(
                view=CombinedView(),
                files=[file, out_file],
                reference=ref,
                mention_author=False,
            )
        else:
            class GraphView(discord.ui.LayoutView):
                container1 = discord.ui.Container(
                    discord.ui.TextDisplay(content=heading),
                    discord.ui.TextDisplay(content=sub),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.MediaGallery(
                        discord.MediaGalleryItem(media="attachment://graph.png"),
                    ),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.TextDisplay(content=legend_text),
                    accent_colour=accent,
                )
                def __init__(self):
                    super().__init__(timeout=300)

            await ctx.send(
                view=GraphView(),
                file=file,
                reference=ref,
                mention_author=False,
            )


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Graph(bot))
