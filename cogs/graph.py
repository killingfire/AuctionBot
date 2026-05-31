"""
cogs/graph.py – Price history graph for Pokémon auctions.
Uses the same filter system as auction search .
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
BG_DARK       = "#0f1117"   # deeper near-black for more contrast
BG_CARD       = "#1a1d27"   # dark navy card background
GRID_COLOR    = "#2a2d3a"
TEXT_COLOR    = "#e8eaf0"
MUTED_COLOR   = "#6c7086"

# Richer, more vibrant palette — each variant has a distinct visual identity
_PALETTE = {
    # Shiny: warm gold/amber gradient feel
    "shiny":  {
        "dot":  "#ffd166",  # warm golden yellow
        "line": "#f4a261",  # amber-orange avg line
        "fill": "#ffd16622",
        "tag":  "[Shiny]",
        "trend_up":   "#06d6a0",
        "trend_down": "#ef476f",
    },
    # Gmax: volcanic red/orange
    "gmax":   {
        "dot":  "#ff6b6b",  # coral red
        "line": "#ff4d6d",  # deep rose avg line
        "fill": "#ff6b6b22",
        "tag":  "[Gmax]",
        "trend_up":   "#06d6a0",
        "trend_down": "#ef476f",
    },
    # Normal: cool cyan/teal — distinct from Discord blurple
    "normal": {
        "dot":  "#4cc9f0",  # bright sky cyan
        "line": "#7b2fff",  # vivid purple avg line
        "fill": "#4cc9f022",
        "tag":  "",
        "trend_up":   "#06d6a0",
        "trend_down": "#ef476f",
    },
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

# ── View mode flags ────────────────────────────────────────────────────────────
# These are parsed out of the filter string before passing to build_query.
FLAG_ALLTIME      = "--alltime"       # bypass GRAPH_START_YEAR cutoff
FLAG_WITHOUTLIERS = "--withoutliers"  # include outlier points inline on the graph
FLAG_SINCE        = "--since"         # --since 2024-06-01 or --since 2023
FLAG_BEFORE       = "--before"        # --before 2025-01-01 or --before 2025
FLAG_COMPARE      = "--compare"       # --compare pokemon2 [pokemon3 ...]

# ── Overlay palette for multi-pokemon compare mode ────────────────────────────
# Each entry: (dot_color, line_color, fill_color)
_OVERLAY_PALETTE = [
    ("#4cc9f0", "#7b2fff", "#4cc9f015"),  # cyan / purple   (slot 0 — primary)
    ("#f72585", "#b5179e", "#f7258515"),  # hot pink / magenta
    ("#06d6a0", "#118ab2", "#06d6a015"),  # teal / ocean blue
    ("#ffd166", "#f4a261", "#ffd16615"),  # gold / amber
    ("#ff6b6b", "#ff4d6d", "#ff6b6b15"),  # coral / rose
]


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


def _parse_date_flag(value: str) -> datetime | None:
    """
    Parse --since / --before value into a UTC datetime.
    Accepts: YYYY, YYYY-MM, YYYY-MM-DD
    Returns None if unparseable.
    """
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _extract_flag_value(tokens: list[str], flag: str) -> tuple[str | None, list[str]]:
    """
    Pull the value immediately after `flag` from tokens.
    Returns (value_or_None, remaining_tokens).
    """
    try:
        idx = tokens.index(flag)
        value = tokens[idx + 1] if idx + 1 < len(tokens) else None
        remaining = tokens[:idx] + tokens[idx + 2 if value else idx + 1:]
        return value, remaining
    except ValueError:
        return None, tokens


def _extract_flag_values(tokens: list[str], flag: str) -> tuple[list[str], list[str]]:
    """
    Pull ALL consecutive values after `flag` (stops at next --flag).
    Supports comma-separated names (with or without spaces), allowing multi-word
    Pokémon names such as: --compare mewtwo, iron valiant, brute bonnet
    Returns (values_list, remaining_tokens).
    """
    try:
        idx = tokens.index(flag)
    except ValueError:
        return [], tokens

    # Collect raw tokens until the next --flag
    raw_values = []
    i = idx + 1
    while i < len(tokens) and not tokens[i].startswith("--"):
        raw_values.append(tokens[i])
        i += 1
    remaining = tokens[:idx] + tokens[i:]

    if not raw_values:
        return [], remaining

    # Re-join and split on commas so "iron valiant, brute bonnet" → ["iron valiant", "brute bonnet"]
    joined = " ".join(raw_values)
    values = [v.strip() for v in joined.split(",") if v.strip()]
    return values, remaining


# ─────────────────────────────────────────────────────────────────────────────
# COMPARE GRAPH BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_compare_graph(
    series: list[dict],   # list of {"name": str, "records": list[dict], "variant": str}
    query_str: str,
    *,
    alltime: bool = False,
    show_outliers: bool = False,
    since_dt: datetime | None = None,
    before_dt: datetime | None = None,
) -> io.BytesIO:
    """
    Overlay multiple Pokémon price histories on one chart.
    Each series uses a distinct colour from _OVERLAY_PALETTE.
    """
    fig = plt.figure(figsize=(13, 7.5), facecolor=BG_DARK)
    gs  = fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.10)
    ax  = fig.add_subplot(gs[0])
    axs = fig.add_subplot(gs[1])
    ax.set_facecolor(BG_CARD)
    axs.set_facecolor(BG_DARK)
    axs.axis("off")

    all_dates  = []
    all_prices = []
    stats_rows = []  # per-series summary for the bottom stats panel

    for slot, s in enumerate(series):
        dot_c, line_c, fill_c = _OVERLAY_PALETTE[slot % len(_OVERLAY_PALETTE)]
        records = sorted(s["records"], key=lambda r: r.get("ts", 0))

        if len(records) > MAX_POINTS:
            step    = len(records) // MAX_POINTS
            records = records[::step]

        dates  = [datetime.fromtimestamp(r["ts"], tz=timezone.utc) for r in records]
        prices = np.array([r["bid"] for r in records], dtype=float)

        if not show_outliers:
            q1, q3 = np.percentile(prices, 25), np.percentile(prices, 75)
            iqr    = q3 - q1
            upper     = q3 + 3.0 * iqr if iqr > 0 else prices.max()
            median_p  = float(np.median(prices))
            iqr_lower = q1 - 3.0 * iqr
            pct_lower = median_p * 0.20
            lower     = max(iqr_lower, pct_lower)  # same logic as build_graph
            mask      = ~((prices > upper) | ((lower > 0) & (prices < lower)))
            dates  = [d for d, m in zip(dates, mask) if m]
            prices = prices[mask]

        if len(prices) < 2:
            continue

        label = s["name"]
        all_dates.extend(dates)
        all_prices.extend(prices.tolist())

        ax.scatter(dates, prices, color=dot_c, s=16, alpha=0.45, zorder=3,
                   linewidths=0, edgecolors="none")

        window   = max(5, len(prices) // 10)
        roll_avg = _rolling_average(prices, window)
        ax.plot(dates, roll_avg, color=line_c, linewidth=2.2,
                label=label, zorder=4, solid_capstyle="round")

        # Trend line
        x_num = np.arange(len(prices), dtype=float)
        slope, intercept = np.polyfit(x_num, prices, 1)
        ax.plot(dates, slope * x_num + intercept, color=line_c, linewidth=1.0,
                linestyle="--", alpha=0.55, zorder=4)

        arrow = "▲" if slope > 0 else "▼"
        stats_rows.append({
            "name":   label,
            "color":  line_c,
            "count":  len(prices),
            "min":    prices.min(),
            "max":    prices.max(),
            "avg":    prices.mean(),
            "median": float(np.median(prices)),
            "trend":  f"{arrow} {_format_price(abs(slope))}/sale",
        })

    # ── X-axis: two-level labels — months on first row, year on second row ─────
    if all_dates:
        _span_days = (max(all_dates) - min(all_dates)).days if len(all_dates) > 1 else 1
    else:
        _span_days = 365
    if _span_days <= 60:
        _major_loc = mdates.WeekdayLocator(byweekday=mdates.MO)
        _minor_loc = mdates.DayLocator()

        def _fmt_short_c(x, _pos=None):
            dt = mdates.num2date(x)
            return f"{dt.day} {dt.strftime('%b')}\n{dt.year}"

        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_short_c))
    else:
        if _span_days <= 365:
            _major_loc = mdates.MonthLocator()
            _minor_loc = mdates.WeekdayLocator(byweekday=mdates.MO)
        elif _span_days <= 365 * 2:
            _major_loc = mdates.MonthLocator(bymonth=range(1, 13, 2))
            _minor_loc = mdates.MonthLocator()
        else:
            _major_loc = mdates.MonthLocator(bymonth=[1, 4, 7, 10])
            _minor_loc = mdates.MonthLocator()

        _first_tick_done_c = [False]

        def _fmt_month_c(x, _pos=None):
            dt = mdates.num2date(x)
            month_str = dt.strftime("%b")
            if dt.month == 1 or not _first_tick_done_c[0]:
                _first_tick_done_c[0] = True
                return f"{month_str}\n{dt.year}"
            return month_str

        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_month_c))

    ax.xaxis.set_major_locator(_major_loc)
    ax.xaxis.set_minor_locator(_minor_loc)
    ax.tick_params(axis="x", which="major", length=5, colors=TEXT_COLOR, labelsize=8.5, pad=3)
    ax.tick_params(axis="x", which="minor", length=3, color=GRID_COLOR)
    plt.setp(ax.get_xticklabels(), rotation=0, ha="center")

    # Year boundary lines
    if all_dates:
        _y0, _y1 = min(all_dates).year, max(all_dates).year
        for _yr in range(_y0, _y1 + 1):
            _jan1 = datetime(_yr, 1, 1, tzinfo=timezone.utc)
            if min(all_dates) < _jan1 < max(all_dates):
                ax.axvline(_jan1, color=MUTED_COLOR, linewidth=0.9,
                           linestyle="--", alpha=0.40, zorder=2)
    ax.tick_params(colors=TEXT_COLOR, labelsize=9)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_COLOR)
    ax.grid(color=GRID_COLOR, linestyle="-", linewidth=0.6, alpha=0.8)
    ax.set_ylabel("Winning Bid (pc)", color=TEXT_COLOR, fontsize=10)
    ax.yaxis.label.set_color(TEXT_COLOR)

    if all_prices:
        gmin, gmax = min(all_prices), max(all_prices)
        if show_outliers and gmax / max(gmin, 1) > 20:
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
            )
            ax.set_ylim(gmin * 0.85, gmax * 1.15)
        else:
            yticks = _smart_yticks(gmin, gmax)
            ax.set_yticks(yticks)
            ax.yaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
            )
            g_range = gmax - gmin or gmax or 1
            ax.set_ylim(max(0, gmin - g_range * 0.12), gmax + g_range * 0.22)

    if all_dates:
        ax.set_xlim(min(all_dates), max(all_dates))

    # ── Title ─────────────────────────────────────────────────────────────────
    names_str    = " vs ".join(s["name"] for s in series)
    alltime_note = "  •  All-time" if alltime else ""
    raw_note     = "  •  Raw data" if show_outliers else ""
    since_note   = f"  •  since {since_dt.strftime('%b %Y')}" if since_dt else ""
    before_note  = f"  •  before {before_dt.strftime('%b %Y')}" if before_dt else ""
    ax.set_title(
        f"{names_str}  •  Price Comparison{alltime_note}{raw_note}{since_note}{before_note}",
        color=TEXT_COLOR, fontsize=13, fontweight="bold", pad=10,
    )
    if query_str:
        ax.set_xlabel(f"Filters: {query_str}", color=MUTED_COLOR, fontsize=8)

    ax.legend(
        facecolor=BG_DARK, edgecolor=GRID_COLOR,
        labelcolor=TEXT_COLOR, fontsize=9,
        loc="upper left", borderpad=0.6, handlelength=1.8,
    )

    # ── Per-series stats bar ──────────────────────────────────────────────────
    cols      = ["Pokémon", "Sales", "Min", "Max", "Avg", "Median", "Trend"]
    col_xs    = [0.04, 0.18, 0.30, 0.42, 0.54, 0.66, 0.82]
    header_y  = 0.82
    value_y   = 0.35

    for col_x, col_label in zip(col_xs, cols):
        axs.text(col_x, header_y, col_label, ha="left", va="center",
                 color=MUTED_COLOR, fontsize=7, transform=axs.transAxes,
                 fontweight="bold")

    row_height = 0.55 / max(len(stats_rows), 1)
    for ri, row in enumerate(stats_rows):
        y = value_y - ri * row_height
        vals = [
            row["name"], f"{row['count']:,}",
            _format_price(row["min"]), _format_price(row["max"]),
            _format_price(row["avg"]), _format_price(row["median"]),
            row["trend"],
        ]
        for col_x, val in zip(col_xs, vals):
            color = row["color"] if col_x == col_xs[0] else TEXT_COLOR
            axs.text(col_x, y, val, ha="left", va="center",
                     color=color, fontsize=8, fontweight="bold",
                     transform=axs.transAxes)

    fig.add_artist(matplotlib.lines.Line2D(
        [0.03, 0.97], [0.16, 0.16],
        transform=fig.transFigure,
        color=GRID_COLOR, linewidth=0.8,
    ))

    # Ensure top margin is tall enough for the title and the top y-tick label,
    # and add a small left margin so the y-axis label isn't cropped.
    fig.subplots_adjust(top=0.91, left=0.09, right=0.97, bottom=0.02)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf



def build_graph(
    records: list[dict],
    query: dict,
    query_str: str,
    *,
    alltime: bool = False,
    show_outliers: bool = False,
    pokemon_name: str | None = None,
) -> io.BytesIO:
    """
    Build a dark-themed price history chart and return a PNG BytesIO buffer.
    Records use short field names: ts = unix_timestamp, bid = winning_bid, pn = pokemon_name.
    If pokemon_name is provided it is used as the chart title instead of the DB's pn field
    (which can be a form name like 'Snowman Pikachu' even when the user asked for 'pikachu').
    """
    records = sorted(records, key=lambda r: r.get("ts", 0))

    if len(records) > MAX_POINTS:
        step    = len(records) // MAX_POINTS
        records = records[::step]

    # Short field names: ts, bid, pn
    dates  = [datetime.fromtimestamp(r["ts"], tz=timezone.utc) for r in records]
    prices = np.array([r["bid"] for r in records], dtype=float)

    if show_outliers:
        # ── Raw mode: plot every single point, no filtering whatsoever ────────
        # Stats, rolling avg, trend, Y-axis all reflect the complete dataset.
        dates_plot      = dates
        prices_plot     = prices
        outlier_dates   = []
        outlier_prices  = np.array([])
        outlier_records = []
        outlier_kinds   = []
        n_outliers      = 0
    else:
        # ── Clean mode: detect and exclude outliers ──────────────────────────
        q1, q3  = np.percentile(prices, 25), np.percentile(prices, 75)
        median  = float(np.median(prices))
        iqr     = q3 - q1
        # High fence: standard 3×IQR above Q3.
        upper_fence = q3 + 3.0 * iqr if iqr > 0 else prices.max()
        # Low fence: the IQR fence (Q1 - 3×IQR) almost always goes negative for
        # high-value tightly-clustered prices (e.g. 80k–120k), so (fence > 0) is
        # never True and no low outliers are caught.  Instead use a percentage-of-
        # median floor: anything below 20 % of the median is a sniped / underpriced
        # sale.  We take the *more generous* of the two fences so genuine lowballs
        # are always caught regardless of price range.
        iqr_lower    = q1 - 3.0 * iqr
        pct_lower    = median * 0.20
        lower_fence  = max(iqr_lower, pct_lower)  # higher value = stricter cutoff

        high_outlier_mask = prices > upper_fence
        low_outlier_mask  = (lower_fence > 0) & (prices < lower_fence)
        outlier_mask      = high_outlier_mask | low_outlier_mask
        plot_mask         = ~outlier_mask

        outlier_dates   = [d for d, m in zip(dates, outlier_mask) if m]
        outlier_prices  = prices[outlier_mask]
        outlier_records = [r for r, m in zip(records, outlier_mask) if m]
        outlier_kinds   = [
            "high" if h else "low"
            for h, m in zip(high_outlier_mask, outlier_mask) if m
        ]

        dates_plot  = [d for d, m in zip(dates, plot_mask) if m]
        prices_plot = prices[plot_mask]

        if len(prices_plot) < 3:
            # Not enough clean points — fall back to full dataset
            dates_plot      = dates
            prices_plot     = prices
            outlier_dates   = []
            outlier_prices  = np.array([])
            outlier_records = []
            outlier_kinds   = []

        n_outliers = int(outlier_mask.sum())

    total  = len(prices)
    p_min  = prices_plot.min()
    p_max  = prices_plot.max()
    p_avg  = prices_plot.mean()
    p_med  = np.median(prices_plot)
    p_std  = prices_plot.std()

    if prices_plot.max() == prices_plot.min():
        prices_plot = prices_plot + np.linspace(-0.5, 0.5, len(prices_plot))

    variant = _detect_variant(query)
    pal     = _PALETTE[variant]

    x_num               = np.arange(len(prices_plot), dtype=float)
    slope, intercept    = np.polyfit(x_num, prices_plot, 1)
    trend_arrow         = "▲" if slope > 0 else "▼"
    trend_color         = pal.get("trend_up", "#43b581") if slope > 0 else pal.get("trend_down", "#f04747")

    window   = max(5, len(prices_plot) // 10)
    roll_avg = _rolling_average(prices_plot, window)

    do_band = len(prices_plot) >= 20
    if do_band:
        p25, p75 = _percentile_band(prices_plot, dates_plot, window=30)

    trend_line = slope * x_num + intercept

    fig = plt.figure(figsize=(12, 7.8), facecolor=BG_DARK)
    # height_ratios=[5.5, 1.2]: slightly taller chart, compact stats panel that
    # fits two-row paired stats + manually drawn legend side by side.
    gs  = fig.add_gridspec(2, 1, height_ratios=[5.5, 1.2], hspace=0.18)
    ax  = fig.add_subplot(gs[0])
    axs = fig.add_subplot(gs[1])

    ax.set_facecolor(BG_CARD)
    axs.set_facecolor(BG_DARK)
    axs.axis("off")

    if do_band:
        ax.fill_between(dates_plot, p25, p75, color=pal["fill"], linewidth=0, label="25–75th pct")

    ax.scatter(
        dates_plot, prices_plot,
        color=pal["dot"], s=22, alpha=0.65, zorder=3, linewidths=0, label="Sales",
        edgecolors="none",
    )

    # In raw mode (show_outliers=True) outlier_prices is empty — this block is skipped.
    # In clean mode, show hint carets at chart edges so user knows data was excluded.
    if len(outlier_prices) > 0:
        hi_dates = [d for d, k in zip(outlier_dates, outlier_kinds) if k == "high"]
        lo_dates = [d for d, k in zip(outlier_dates, outlier_kinds) if k == "low"]
        if hi_dates:
            ax.scatter(hi_dates, [prices_plot.max()] * len(hi_dates),
                       color="#ef476f", marker="^", s=44, zorder=5, linewidths=0,
                       label=f"High outlier(s) ({len(hi_dates)}) — hidden")
        if lo_dates:
            ax.scatter(lo_dates, [prices_plot.min()] * len(lo_dates),
                       color="#ffd166", marker="v", s=44, zorder=5, linewidths=0,
                       label=f"Low outlier(s) ({len(lo_dates)}) — hidden")

    ax.plot(dates_plot, roll_avg, color=pal["line"], linewidth=2.5,
            label=f"Avg (±{window})", zorder=4, solid_capstyle="round")

    ax.plot(dates_plot, trend_line, color=trend_color, linewidth=1.4,
            linestyle="--", alpha=0.85, label="Trend", zorder=4)

    idx_max = int(np.argmax(prices_plot))
    idx_min = int(np.argmin(prices_plot))

    _y_lo, _y_hi = prices_plot.min(), prices_plot.max()
    _y_span = _y_hi - _y_lo or 1

    def _annotate_point(idx, label, color, prefer_above: bool):
        """Place annotation above or below based on where the point sits in the chart."""
        val = prices_plot[idx]
        # Fraction of the way up the y-range (0 = bottom, 1 = top)
        rel = (val - _y_lo) / _y_span
        # If point is in top 30% of chart, label goes below to avoid title clip
        if rel > 0.70:
            yoff, va = -32, "top"
        # If point is in bottom 30%, label goes above
        elif rel < 0.30:
            yoff, va = 22, "bottom"
        else:
            yoff, va = (20, "bottom") if prefer_above else (-28, "top")
        ax.annotate(
            label,
            xy=(dates_plot[idx], val),
            xytext=(0, yoff),
            textcoords="offset points",
            ha="center", va=va,
            color=color, fontsize=8, fontweight="bold",
            arrowprops=dict(arrowstyle="-", color=color, lw=1.2),
        )

    _annotate_point(idx_max, f"Max\n{_format_price(prices_plot.max())}",
                    pal.get("trend_up",   "#06d6a0"), prefer_above=True)
    _annotate_point(idx_min, f"Min\n{_format_price(prices_plot.min())}",
                    pal.get("trend_down", "#ef476f"), prefer_above=False)

    # ── X-axis: two-level labels — months on first row, year on second row ─────
    # Major ticks show short month name. On January ticks (and the first visible
    # tick) the year is appended as a second line: "Jan\n2024". Minor ticks mark
    # every week/month between major labels for easy date estimation.
    _span_days = (dates_plot[-1] - dates_plot[0]).days if len(dates_plot) > 1 else 1
    if _span_days <= 60:
        _major_loc = mdates.WeekdayLocator(byweekday=mdates.MO)
        _minor_loc = mdates.DayLocator()

        def _fmt_short(x, _pos=None):
            dt = mdates.num2date(x)
            return f"{dt.day} {dt.strftime('%b')}\n{dt.year}"

        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_short))
    else:
        if _span_days <= 365:
            _major_loc = mdates.MonthLocator()
            _minor_loc = mdates.WeekdayLocator(byweekday=mdates.MO)
        elif _span_days <= 365 * 2:
            _major_loc = mdates.MonthLocator(bymonth=range(1, 13, 2))
            _minor_loc = mdates.MonthLocator()
        else:
            _major_loc = mdates.MonthLocator(bymonth=[1, 4, 7, 10])
            _minor_loc = mdates.MonthLocator()

        _first_tick_done = [False]

        def _fmt_month(x, _pos=None):
            dt = mdates.num2date(x)
            month_str = dt.strftime("%b")
            if dt.month == 1 or not _first_tick_done[0]:
                _first_tick_done[0] = True
                return f"{month_str}\n{dt.year}"
            return month_str

        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_month))

    ax.xaxis.set_major_locator(_major_loc)
    ax.xaxis.set_minor_locator(_minor_loc)
    ax.tick_params(axis="x", which="major", length=5, colors=TEXT_COLOR, labelsize=8.5, pad=3)
    ax.tick_params(axis="x", which="minor", length=3, color=GRID_COLOR)
    plt.setp(ax.get_xticklabels(), rotation=0, ha="center")

    # Year boundary lines — subtle dashed vertical rule at each Jan 1
    _y0, _y1 = dates_plot[0].year, dates_plot[-1].year
    for _yr in range(_y0, _y1 + 1):
        _jan1 = datetime(_yr, 1, 1, tzinfo=timezone.utc)
        if dates_plot[0] < _jan1 < dates_plot[-1]:
            ax.axvline(_jan1, color=MUTED_COLOR, linewidth=0.9, linestyle="--",
                       alpha=0.40, zorder=2)

    ax.tick_params(colors=TEXT_COLOR, labelsize=9)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_COLOR)
    ax.grid(color=GRID_COLOR, linestyle="-", linewidth=0.6, alpha=0.8)
    ax.set_xlim(dates_plot[0], dates_plot[-1])

    pm_clean = prices_plot.min()
    px_clean = prices_plot.max()
    y_range  = px_clean - pm_clean or px_clean or 1

    if show_outliers and px_clean > 0 and pm_clean > 0 and px_clean / max(pm_clean, 1) > 20:
        # When raw data spans more than 20× price range, log scale prevents
        # extreme outliers from flattening the rest of the chart.
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
        )
        ax.set_ylim(pm_clean * 0.85, px_clean * 1.15)
    else:
        yticks = _smart_yticks(pm_clean, px_clean)
        ax.set_yticks(yticks)
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
        )
        ax.set_ylim(
            max(0, pm_clean - y_range * 0.18),
            px_clean + y_range * 0.25,
        )

    ax.set_ylabel("Winning Bid (pc)", color=TEXT_COLOR, fontsize=10)
    ax.yaxis.label.set_color(TEXT_COLOR)

    tag        = pal["tag"]
    # Use the requested name if provided; fall back to DB pn only as a last resort.
    # This prevents form names (e.g. "Snowman Pikachu") from appearing when the
    # user explicitly asked for "pikachu".
    name       = pokemon_name or records[0].get("pn", "Unknown")
    full_title = f"[{tag}] {name}".strip() if tag else name
    date_first = dates[0].strftime("%-d %b %Y")
    date_last  = dates[-1].strftime("%-d %b %Y")
    span_days  = (dates[-1] - dates[0]).days
    alltime_note  = "  •  All-time" if alltime else ""
    raw_note      = "  •  Raw (all data)" if show_outliers else ""
    ax.set_title(
        f"{full_title}  •  Price History{alltime_note}{raw_note}  •  {date_first} → {date_last} ({span_days}d)",
        color=TEXT_COLOR, fontsize=14, fontweight="bold", pad=10,
    )
    # Filter string is already shown in the Discord message subtitle — no need
    # to repeat it as an xlabel which would collide with the x-axis tick labels.

    # ── Legend: placed inside axs on the left, using the real handles from ax ─
    legend_handles, legend_labels = ax.get_legend_handles_labels()
    if ax.get_legend():
        ax.get_legend().remove()

    axs_legend = axs.legend(
        legend_handles, legend_labels,
        loc="center left",
        bbox_to_anchor=(0.0, 0.5),
        facecolor=BG_CARD,
        edgecolor=GRID_COLOR,
        labelcolor=TEXT_COLOR,
        fontsize=7.5,
        borderpad=0.6,
        handlelength=1.5,
        handletextpad=0.5,
        framealpha=1.0,
        borderaxespad=0.0,
    )

    # ── Stats columns: right of the legend ───────────────────────────────────
    # We don't know the legend width in advance, so we fix it at 27% of axs.
    LEG_FRAC = 0.27
    S0    = LEG_FRAC + 0.015
    S1    = 1.0
    col_w = (S1 - S0) / 6

    paired_cols = [
        ("Min",      _format_price(p_min),  "Max",    _format_price(p_max)),
        ("Avg",      _format_price(p_avg),  "Median", _format_price(p_med)),
        ("Auctions", f"{total:,}",          None,     None),
        ("Std Dev",  _format_price(p_std),  None,     None),
        ("Trend",    f"{trend_arrow} {_format_price(abs(slope))}/sale", None, None),
        ("Outliers", "All shown" if show_outliers else (f"{n_outliers} hidden" if n_outliers else "None"), None, None),
    ]

    # y-coords for paired rows (two label+value pairs stacked)
    P_TOP_LBL, P_TOP_VAL = 0.80, 0.58
    P_BOT_LBL, P_BOT_VAL = 0.38, 0.12
    # y-coords for single rows (centred between top and bottom)
    S_LBL, S_VAL = 0.72, 0.28

    for ci, (tl, tv, bl, bv) in enumerate(paired_cols):
        cx = S0 + ci * col_w + col_w * 0.5
        paired = bl is not None

        if paired:
            axs.text(cx, P_TOP_LBL, tl, ha="center", va="center",
                     color=MUTED_COLOR, fontsize=7, transform=axs.transAxes)
            axs.text(cx, P_TOP_VAL, tv, ha="center", va="center",
                     color=TEXT_COLOR, fontsize=9, fontweight="bold",
                     transform=axs.transAxes)
            # subtle mid divider just for paired columns
            xmin_f = (S0 + ci * col_w) / 1.0
            xmax_f = (S0 + (ci + 1) * col_w) / 1.0
            axs.axhline(0.48, xmin=xmin_f, xmax=xmax_f,
                        color=GRID_COLOR, linewidth=0.5, alpha=0.5)
            axs.text(cx, P_BOT_LBL, bl, ha="center", va="center",
                     color=MUTED_COLOR, fontsize=7, transform=axs.transAxes)
            axs.text(cx, P_BOT_VAL, bv, ha="center", va="center",
                     color=TEXT_COLOR, fontsize=9, fontweight="bold",
                     transform=axs.transAxes)
        else:
            axs.text(cx, S_LBL, tl, ha="center", va="center",
                     color=MUTED_COLOR, fontsize=7, transform=axs.transAxes)
            axs.text(cx, S_VAL, tv, ha="center", va="center",
                     color=TEXT_COLOR, fontsize=9, fontweight="bold",
                     transform=axs.transAxes)

    fig.add_artist(matplotlib.lines.Line2D(
        [0.05, 0.95], [0.135, 0.135],
        transform=fig.transFigure,
        color=GRID_COLOR, linewidth=0.8,
    ))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    # Each outlier entry: (date, price, record, kind) — kind is "high" or "low"
    return buf, list(zip(outlier_dates, outlier_prices.tolist(), outlier_records, outlier_kinds))


def build_outlier_image(
    outliers: list[tuple],
    pokemon_name: str,
    variant: str,
) -> io.BytesIO:
    """
    Build a table image for outlier sales.
    Each entry in outliers is a (date, price, record, kind) tuple.
    kind is "high" or "low". Columns: #, Type, Auction ID, Date, Level, IV%, Winning Bid
    """
    n        = len(outliers)
    row_h_in = 0.38
    head_h   = 0.50
    fig_h    = head_h + n * row_h_in

    fig, ax = plt.subplots(figsize=(11, fig_h), facecolor=BG_DARK)
    ax.set_facecolor(BG_DARK)
    ax.axis("off")

    headers    = ["#", "Type", "Auction ID", "Date", "Level", "IV %", "Winning Bid"]
    col_widths = [0.04, 0.08, 0.16, 0.20, 0.09, 0.12, 0.20]

    rows = []
    for i, entry in enumerate(outliers):
        d, p, r, kind = entry if len(entry) == 4 else (*entry, "high")
        aid   = str(r.get("aid", "?"))
        date  = d.strftime("%-d %b %Y")
        level = str(r.get("lv", "???"))
        iv    = r.get("iv")
        iv_s  = f"{iv:.2f}%" if iv is not None else "???"
        kind_label = "▲ High" if kind == "high" else "▼ Low"
        rows.append([str(i + 1), kind_label, aid, date, level, iv_s, _format_price(p)])

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
            kind_val = rows[row - 1][1] if row <= len(rows) else ""
            if col == 6:
                # Winning bid — color by kind
                color = "#ef476f" if "High" in kind_val else "#ffd166"
                cell.get_text().set_color(color)
                cell.get_text().set_fontweight("bold")
            elif col == 1:
                # Type column — color by high/low
                color = "#ef476f" if "High" in kind_val else "#ffd166"
                cell.get_text().set_color(color)
                cell.get_text().set_fontweight("bold")
            elif col == 5:
                cell.get_text().set_color("#ffd166")  # IV % accent gold
            elif col == 0:
                cell.get_text().set_color(MUTED_COLOR)
            elif col == 2:
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
          j!g --name mewtwo --iv >90
          j!g --name goomy --limit 10
          j!g --name garchomp --since 2024-06-01
          j!g --name garchomp --before 2025
          j!g --name pikachu --compare charizard mewtwo
        """
        _raw_check = filters.split() if filters else []
        _has_name_flag    = any(t in _NAME_FLAGS for t in _raw_check)
        _has_compare_flag = FLAG_COMPARE in _raw_check
        if not _has_name_flag and not _has_compare_flag:
            await ctx.send(
                view=_error_view(
                    f"❌ Please specify a Pokémon name.\n"
                    f"{REPLY} Example: `j!g --name pikachu --shiny`\n"
                    f"{REPLY} Example: `j!g --compare mewtwo, iron valiant, brute bonnet --sh`"
                ),
                reference=ctx.message,
                mention_author=False,
            )
            return

        raw = filters.split() if filters else []

        # ── Extract graph-only flags before passing to build_query ─────────────
        use_alltime    = FLAG_ALLTIME      in raw
        use_outliers   = FLAG_WITHOUTLIERS in raw
        raw = [t for t in raw if t not in (FLAG_ALLTIME, FLAG_WITHOUTLIERS)]

        since_str, raw  = _extract_flag_value(raw, FLAG_SINCE)
        before_str, raw = _extract_flag_value(raw, FLAG_BEFORE)
        compare_names, raw = _extract_flag_values(raw, FLAG_COMPARE)

        since_dt  = _parse_date_flag(since_str)  if since_str  else None
        before_dt = _parse_date_flag(before_str) if before_str else None

        if since_str and since_dt is None:
            await ctx.send(
                view=_error_view(f"❌ Couldn't parse `--since {since_str}`. Use YYYY, YYYY-MM, or YYYY-MM-DD."),
                reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                mention_author=False,
            )
            return
        if before_str and before_dt is None:
            await ctx.send(
                view=_error_view(f"❌ Couldn't parse `--before {before_str}`. Use YYYY, YYYY-MM, or YYYY-MM-DD."),
                reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                mention_author=False,
            )
            return

        query, _, limit  = build_query(raw, expand_name_by_dex=True)
        display_str      = " ".join(raw).strip()

        # ── Capture the requested Pokémon name exactly as the user typed it ───
        # This ensures graph titles show "Pikachu" not "Snowman Pikachu" or
        # any other DB form name that happens to match the dex expansion.
        def _requested_name_from_tokens(tokens: list[str]) -> str | None:
            for flag in _NAME_FLAGS:
                if flag in tokens:
                    idx = tokens.index(flag)
                    if idx + 1 < len(tokens):
                        return tokens[idx + 1].title()
            return None

        _requested_name = _requested_name_from_tokens(raw)

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

        # ── Build the timestamp filter (alltime / since / before / default) ───
        def _build_ts_filter(use_alltime: bool, since_dt, before_dt) -> dict:
            ts_f: dict = {"$exists": True}
            conditions = []
            if not use_alltime:
                year_ts = int(datetime(GRAPH_START_YEAR, 1, 1, tzinfo=timezone.utc).timestamp())
                conditions.append({"ts": {"$gte": year_ts}})
            if since_dt:
                conditions.append({"ts": {"$gte": int(since_dt.timestamp())}})
            if before_dt:
                conditions.append({"ts": {"$lt": int(before_dt.timestamp())}})
            if conditions:
                # Merge all $gte / $lt into one ts expression
                merged: dict = {"$exists": True}
                for c in conditions:
                    merged.update(c.get("ts", {}))
                return merged
            return ts_f

        ts_filter = _build_ts_filter(use_alltime, since_dt, before_dt)

        # ── Helper: fetch records for one query dict ───────────────────────────
        def _fetch(q: dict) -> list[dict]:
            cur = _col.find(
                {**q, "ts": ts_filter, "bid": {"$exists": True}},
                projection,
            ).sort("ts", -1)
            if limit is not None:
                cur = cur.limit(limit)
            recs = list(cur)
            recs.sort(key=lambda r: r.get("ts", 0))
            return recs

        # ── COMPARE MODE ──────────────────────────────────────────────────────
        if compare_names:
            # Support two syntaxes:
            #   (A) j!g --name mewtwo --compare iron valiant, brute bonnet --sh
            #       → primary = mewtwo (from --name), compare list = [iron valiant, brute bonnet]
            #   (B) j!g --compare mewtwo, iron valiant, brute bonnet --sh
            #       → primary = mewtwo (first in list), compare list = rest
            # In syntax (B) there is no --name flag, so we promote compare_names[0] to primary.
            _no_name_flag = not any(t in _NAME_FLAGS for t in raw)
            if _no_name_flag:
                if len(compare_names) < 2:
                    await ctx.send(
                        view=_error_view(
                            "❌ `--compare` needs at least 2 Pokémon names.\n"
                            f"{REPLY} Example: `j!g --compare mewtwo, iron valiant, brute bonnet --sh`"
                        ),
                        reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                        mention_author=False,
                    )
                    return
                # Treat first name as primary, rest as compare targets.
                # Forward any variant flags (--sh, --shiny, --gmax) so the
                # primary query is filtered correctly (e.g. shiny-only).
                _primary_cname  = compare_names[0]
                compare_names   = compare_names[1:]
                _variant_flags  = [t for t in raw if t in ("--sh", "--shiny", "--gmax", "--noshiny")]
                praw = ["--name", _primary_cname] + _variant_flags
                query, _, limit = build_query(praw, expand_name_by_dex=True)
                _requested_name = _primary_cname.title()

            if len(compare_names) > 4:
                await ctx.send(
                    view=_error_view("❌ Maximum 4 Pokémon in compare mode (5 total including primary)."),
                    reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                    mention_author=False,
                )
                return

            primary_records = _fetch(query)
            if not primary_records:
                await ctx.send(
                    view=_error_view("❌ No auctions found for the primary Pokémon."),
                    reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                    mention_author=False,
                )
                return

            primary_name    = _requested_name or primary_records[0].get("pn", "Unknown")
            primary_variant = _detect_variant(query)
            series = [{"name": primary_name, "records": primary_records, "variant": primary_variant}]

            for cname in compare_names:
                # Forward variant flags (--sh/--shiny/--gmax) to each compared pokemon
                # so "j!g --compare mewtwo, iron valiant --sh" queries all as shiny.
                _variant_flags = [t for t in raw if t in ("--sh", "--shiny", "--gmax", "--noshiny")]
                craw = ["--name", cname] + _variant_flags
                cquery, _, _ = build_query(craw, expand_name_by_dex=True)
                crecs = _fetch(cquery)
                if not crecs:
                    await ctx.send(
                        view=_error_view(f"❌ No auctions found for `{cname}` — skipping."),
                        reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                        mention_author=False,
                    )
                    continue
                cname_real = cname.title()  # use the name as requested, not the DB form name
                series.append({"name": cname_real, "records": crecs, "variant": primary_variant})

            if len(series) < 2:
                await ctx.send(
                    view=_error_view("❌ Need at least 2 Pokémon with data to compare."),
                    reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                    mention_author=False,
                )
                return

            try:
                buf = build_compare_graph(
                    series, display_str,
                    alltime=use_alltime,
                    show_outliers=use_outliers,
                    since_dt=since_dt,
                    before_dt=before_dt,
                )
            except Exception as e:
                await ctx.send(
                    view=_error_view(f"❌ Failed to generate comparison graph: `{e}`"),
                    reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                    mention_author=False,
                )
                return

            names_heading  = " vs ".join(s["name"] for s in series)
            heading        = f"## {names_heading} — Price Comparison"
            since_badge    = f"  •  📅 Since {since_dt.strftime('%b %Y')}" if since_dt else ""
            before_badge   = f"  •  📅 Before {before_dt.strftime('%b %Y')}" if before_dt else ""
            alltime_badge  = "  •  🕐 All-time" if use_alltime else ""
            outliers_badge = "  •  ⚠️ Raw data" if use_outliers else ""
            sub = f"_Comparing {len(series)} Pokémon{alltime_badge}{since_badge}{before_badge}{outliers_badge}  •  filters: `{display_str}`_"

            file = discord.File(buf, filename="graph.png")
            ref  = ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None

            class CompareView(discord.ui.LayoutView):
                container = discord.ui.Container(
                    discord.ui.TextDisplay(content=heading),
                    discord.ui.TextDisplay(content=sub),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.MediaGallery(
                        discord.MediaGalleryItem(media="attachment://graph.png"),
                    ),
                    accent_colour=config.EMBED_COLOR,
                )
                def __init__(self):
                    super().__init__(timeout=300)

            await ctx.send(view=CompareView(), file=file, reference=ref, mention_author=False)
            return

        # ── SINGLE POKEMON MODE ───────────────────────────────────────────────
        records = _fetch(query)

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
            buf, outliers = build_graph(
                records, query, display_str,
                alltime=use_alltime,
                show_outliers=use_outliers,
                pokemon_name=_requested_name,
            )
        except Exception as e:
            await ctx.send(
                view=_error_view(f"❌ Failed to generate graph: `{e}`"),
                reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                mention_author=False,
            )
            return

        # Use the name the user actually asked for, not the DB's form name.
        # e.g. "--n pikachu" shows "Pikachu" not "Snowman Pikachu".
        name      = _requested_name or records[0].get("pn", "Unknown")
        total     = len(records)
        variant   = _detect_variant(query)
        pal       = _PALETTE[variant]
        disc_tag  = _DISCORD_TAG[variant]
        accent    = config.SHINY_EMBED_COLOR if variant == "shiny" else config.EMBED_COLOR

        heading    = f"## {disc_tag} {name} — Price History".strip()
        limit_note = f"  •  last {limit:,} auctions" if limit is not None else ""
        alltime_badge   = "  •  🕐 All-time" if use_alltime else ""
        since_badge     = f"  •  📅 Since {since_dt.strftime('%b %Y')}" if since_dt else ""
        before_badge    = f"  •  📅 Before {before_dt.strftime('%b %Y')}" if before_dt else ""
        outliers_badge  = "  •  ⚠️ Raw data (all outliers included)" if use_outliers else ""
        sub        = f"_{total:,} auction(s) plotted{limit_note}{alltime_badge}{since_badge}{before_badge}{outliers_badge}  •  filters: `{display_str}`_"

        file = discord.File(buf, filename="graph.png")
        ref  = ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None

        # ── Static text payloads for button ephemeral replies ─────────────────
        _legend_text = (
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
            f"{REPLY} **Outliers** — sales so far above the typical price range they squash everything else. Excluded from the graph and most stats\n"
            f"{REPLY} **Chart Max** — highest sale visible on the graph (outliers excluded)\n"
            f"{REPLY} **All-time Max** — the absolute highest sale ever recorded, including outliers"
        )

        _filters_body = (
            f"**🔍 Available Filters**\n"
            f"-# Use these with `j!g` — e.g. `j!g --name pikachu --shiny --iv >90`\n"
            f"{REPLY} `--name <value>` — Pokémon name  _(--n, -n, --pokemon)_\n"
            f"{REPLY} `--shiny` — Shiny only  _(--sh)_\n"
            f"{REPLY} `--gmax` — Gigantamax only  _(--gm, --giga)_\n"
            f"{REPLY} `--noshiny` — Exclude shinies  _(--nosh)_\n"
            f"{REPLY} `--nogmax` — Exclude Gigantamax  _(--nogm)_\n"
            f"{REPLY} `--iv <value>` — Total IV % e.g. `>90`, `>=85`, `90-100`  _(--totaliv)_\n"
            f"{REPLY} `--hpiv / --atkiv / --defiv / --spatkiv / --spdefiv / --spdiv <value>` — Individual IVs\n"
            f"{REPLY} `--level <value>` — Level e.g. `50`, `>50`, `30-100`  _(--lv, --lvl)_\n"
            f"{REPLY} `--nature <value>` — Nature e.g. `adamant`  _(--nat)_\n"
            f"{REPLY} `--move <value>` — Has this move, stackable  _(-m, --moves)_\n"
            f"{REPLY} `--gender <value>` — `male`, `female`, or `unknown`  _(--g)_\n"
            f"{REPLY} `--type <value>` — Type, stackable up to 2  _(--t)_\n"
            f"{REPLY} `--region <value>` — Region e.g. `kanto`, `galar`  _(--r)_\n"
            f"{REPLY} `--evo <value>` — Entire evo family  _(--family)_\n"
            f"{REPLY} `--category <value>` — Category e.g. `rares`, `starters`  _(--cat)_\n"
            f"{REPLY} `--exclude <value>` — Exclude by name/type/region/category  _(--ex)_\n"
            f"{REPLY} `--price <value>` — Price filter e.g. `>5000`, `500-5000`  _(--p, --bid)_\n"
            f"{REPLY} `--limit <value>` — Limit to N most recent matches  _(--lim, --top)_\n"
            f"{REPLY} `--sort <value>` — Sort by `iv`, `bid`, `level`, `date`, `id` (append `+`/`-`)  _(--order)_\n"
            f"{REPLY} `--alltime` — 🕐 Show all historical data instead of {GRAPH_START_YEAR}+ only\n"
            f"{REPLY} `--withoutliers` — ⚠️ Plot ALL data including outliers (raw mode, may use log scale)\n"
            f"{REPLY} `--since <date>` — Only show auctions from this date onwards (YYYY, YYYY-MM, or YYYY-MM-DD)\n"
            f"{REPLY} `--before <date>` — Only show auctions before this date (YYYY, YYYY-MM, or YYYY-MM-DD)\n"
            f"{REPLY} `--compare <name> [name2 ...]` — Overlay up to 4 other Pokémon on the same graph"
        )
        _protip_text = (
            f"-# 💡 **Pro tip:** Use `--limit` to focus on the most recent auctions — "
            f"e.g. `j!g --name garchomp --limit 50` graphs only the latest 50 sales, "
            f"giving you a much cleaner picture of where prices stand today."
        )

        # ── Build outlier BytesIO if needed — used only by the button callback ─
        out_buf = None
        if outliers:
            out_buf = build_outlier_image(outliers, name, variant)

        # ── Close-over state for toggle button callbacks ───────────────────────
        _outlier_count  = len(outliers)
        _has_outliers   = bool(outliers)
        _legend_capture = _legend_text
        _outlier_bytes  = out_buf  # BytesIO | None

        # Captured state needed to regenerate the graph on toggle
        _records_cap    = records
        _query_cap      = query
        _display_cap    = display_str
        _limit_cap      = limit
        _since_cap      = since_dt
        _before_cap     = before_dt

        async def _regenerate_graph(
            interaction: discord.Interaction,
            new_alltime: bool,
            new_outliers: bool,
        ):
            """Re-fetch, rebuild, and edit the message with toggled view flags."""
            await interaction.response.defer()

            new_ts = _build_ts_filter(new_alltime, _since_cap, _before_cap)
            new_cursor = _col.find(
                {**_query_cap, "ts": new_ts, "bid": {"$exists": True}},
                projection,
            ).sort("ts", -1)
            if _limit_cap is not None:
                new_cursor = new_cursor.limit(_limit_cap)
            new_records = list(new_cursor)
            new_records.sort(key=lambda r: r.get("ts", 0))

            if not new_records:
                await interaction.followup.send("❌ No data found.", ephemeral=True)
                return

            try:
                new_buf, new_out = build_graph(
                    new_records, _query_cap, _display_cap,
                    alltime=new_alltime,
                    show_outliers=new_outliers,
                )
            except Exception as exc:
                await interaction.followup.send(f"❌ Failed to regenerate: `{exc}`", ephemeral=True)
                return

            new_file = discord.File(new_buf, filename="graph.png")

            n_out_new       = len(new_out)
            has_out_new     = bool(new_out)
            out_buf_new     = build_outlier_image(new_out, name, variant) if has_out_new else None
            alltime_badge_n = "  •  🕐 All-time" if new_alltime else ""
            since_badge_n   = f"  •  📅 Since {_since_cap.strftime('%b %Y')}" if _since_cap else ""
            before_badge_n  = f"  •  📅 Before {_before_cap.strftime('%b %Y')}" if _before_cap else ""
            out_badge_n     = "  •  ⚠️ Raw data" if new_outliers else ""
            lim_note_n      = f"  •  last {_limit_cap:,} auctions" if _limit_cap is not None else ""
            new_sub         = (
                f"_{len(new_records):,} auction(s) plotted{lim_note_n}"
                f"{alltime_badge_n}{since_badge_n}{before_badge_n}{out_badge_n}  •  filters: `{_display_cap}`_"
            )

            # Rebuild buttons with updated toggle state
            new_btn_list = _build_btn_list(
                legend_text=_legend_capture,
                filters_text=_filters_body,
                has_outliers=has_out_new,
                outlier_count=n_out_new,
                outlier_bytes=out_buf_new,
                outlier_data=new_out,
                is_alltime=new_alltime,
                is_outliers=new_outliers,
                regenerate_fn=_regenerate_graph,
            )

            new_container_comps = [
                discord.ui.TextDisplay(content=heading),
                discord.ui.TextDisplay(content=new_sub),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media="attachment://graph.png"),
                ),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(content=_protip_text),
            ]

            class NewGraphView(discord.ui.LayoutView):
                container = discord.ui.Container(
                    *new_container_comps,
                    accent_colour=accent,
                )
                action_row = discord.ui.ActionRow(*new_btn_list)
                def __init__(self):
                    super().__init__(timeout=300)

            await interaction.edit_original_response(
                attachments=[new_file],
                view=NewGraphView(),
            )

        # ── Button factory (used both for initial view and after toggles) ──────
        def _build_btn_list(
            legend_text, filters_text, has_outliers, outlier_count, outlier_bytes,
            outlier_data, is_alltime, is_outliers, regenerate_fn,
        ):
            btn_list = []

            # ── 📖 How to Read This Graph ──────────────────────────────────────
            class _HowToReadBtn(discord.ui.Button):
                def __init__(self):
                    super().__init__(
                        style=discord.ButtonStyle.secondary,
                        label="📖 How to Read This Graph",
                        custom_id="g_legend",
                    )
                async def callback(self, interaction: discord.Interaction):
                    class LegendView(discord.ui.LayoutView):
                        c = discord.ui.Container(
                            discord.ui.TextDisplay(content=legend_text),
                            accent_colour=config.EMBED_COLOR,
                        )
                    await interaction.response.send_message(view=LegendView(), ephemeral=True)

            btn_list.append(_HowToReadBtn())

            # ── 🔍 Available Filters ───────────────────────────────────────────
            _ft = filters_text

            class _FiltersBtn(discord.ui.Button):
                def __init__(self):
                    super().__init__(
                        style=discord.ButtonStyle.secondary,
                        label="🔍 Available Filters",
                        custom_id="g_filters",
                    )
                async def callback(self, interaction: discord.Interaction):
                    class FiltersView(discord.ui.LayoutView):
                        c = discord.ui.Container(
                            discord.ui.TextDisplay(content=_ft),
                            accent_colour=config.EMBED_COLOR,
                        )
                    await interaction.response.send_message(view=FiltersView(), ephemeral=True)

            btn_list.append(_FiltersBtn())

            # ── 🕐 All-time / Since 2024 toggle ───────────────────────────────
            _is_alltime_cap  = is_alltime
            _is_outliers_cap = is_outliers

            class _AlltimeBtn(discord.ui.Button):
                def __init__(self):
                    if _is_alltime_cap:
                        # currently showing all-time → button offers to go back to 2024+
                        _style = discord.ButtonStyle.success
                        _label = f"📅 Since {GRAPH_START_YEAR} Only"
                    else:
                        # currently showing 2024+ → button offers to expand to all-time
                        _style = discord.ButtonStyle.secondary
                        _label = "🕐 Show All-time Data"
                    super().__init__(style=_style, label=_label, custom_id="g_alltime")
                async def callback(self, interaction: discord.Interaction):
                    await regenerate_fn(interaction, not _is_alltime_cap, _is_outliers_cap)

            btn_list.append(_AlltimeBtn())

            # ── ⚠️ Outliers toggle ─────────────────────────────────────────────
            class _OutliersToggleBtn(discord.ui.Button):
                def __init__(self):
                    if _is_outliers_cap:
                        # currently raw (outliers shown) → button offers clean view
                        _style = discord.ButtonStyle.danger
                        _label = "📊 Hide Outliers (Clean View)"
                    else:
                        # currently clean → button offers raw mode
                        _style = discord.ButtonStyle.secondary
                        _label = "⚠️ Show Outliers (Raw Data)"
                    super().__init__(style=_style, label=_label, custom_id="g_outliers_toggle")
                async def callback(self, interaction: discord.Interaction):
                    await regenerate_fn(interaction, _is_alltime_cap, not _is_outliers_cap)

            btn_list.append(_OutliersToggleBtn())

            # ── Outlier detail viewer (only when in clean mode with hidden outliers) ─
            if has_outliers and not is_outliers:
                _ob = outlier_bytes
                _oc = outlier_count
                n_high = sum(1 for e in outlier_data if (e[3] if len(e) == 4 else "high") == "high")
                n_low  = _oc - n_high
                parts  = []
                if n_high: parts.append(f"▲{n_high} overpriced")
                if n_low:  parts.append(f"▼{n_low} sniped")
                label  = f"📋 View {_oc} Excluded Sale(s) ({', '.join(parts)})"

                class _OutlierDetailBtn(discord.ui.Button):
                    def __init__(self):
                        super().__init__(
                            style=discord.ButtonStyle.secondary,
                            label=label,
                            custom_id="g_outlier_detail",
                        )
                    async def callback(self, interaction: discord.Interaction):
                        if _ob:
                            _ob.seek(0)
                        out_f = discord.File(_ob, filename="outliers.png")
                        class OutlierView(discord.ui.LayoutView):
                            c = discord.ui.Container(
                                discord.ui.TextDisplay(content=(
                                    f"📋 **{_oc} sale(s) excluded from the graph**\n"
                                    f"_▲ Overpriced outliers inflate the average; ▼ sniped/underpriced sales compress the Y-axis. Both are hidden by default for a cleaner chart._"
                                )),
                                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                                discord.ui.MediaGallery(
                                    discord.MediaGalleryItem(media="attachment://outliers.png"),
                                ),
                                accent_colour=discord.Colour(0xef476f),
                            )
                        await interaction.response.send_message(
                            view=OutlierView(), file=out_f, ephemeral=True,
                        )

                btn_list.append(_OutlierDetailBtn())

            return btn_list

        # ── Build initial button list and view ────────────────────────────────
        _btn_list = _build_btn_list(
            legend_text=_legend_capture,
            filters_text=_filters_body,
            has_outliers=_has_outliers,
            outlier_count=_outlier_count,
            outlier_bytes=_outlier_bytes,
            outlier_data=outliers,
            is_alltime=use_alltime,
            is_outliers=use_outliers,
            regenerate_fn=_regenerate_graph,
        )

        _container_comps = [
            discord.ui.TextDisplay(content=heading),
            discord.ui.TextDisplay(content=sub),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media="attachment://graph.png"),
            ),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content=_protip_text),
        ]

        _action_row = discord.ui.ActionRow(*_btn_list)

        class GraphView(discord.ui.LayoutView):
            container = discord.ui.Container(
                *_container_comps,
                accent_colour=accent,
            )
            action_row = _action_row
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
