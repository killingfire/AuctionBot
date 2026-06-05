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

import asyncio
import functools
import io
from dataclasses import dataclass
from datetime import datetime, timezone

import discord
import matplotlib
matplotlib.use("Agg")  # non-interactive backend, must be set before pyplot import
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

import config
from config import REPLY
from utils import build_query, resolve_pokemon_name, shiny_prefix, get_names_by_spawnrate, get_spawnrate_db
from filters import FLAG_DEFINITIONS

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

# Hard cap on records pulled from MongoDB per query.
# Prevents OOM when no name/filter is given and the entire collection matches.
MAX_FETCH = 50_000

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

# Multi-name flags: --n meowth --n zorua  (repeatable; each is one pokemon)
# These are ALL aliases of --name as registered in FLAG_DEFINITIONS
_MULTI_NAME_FLAGS_ALL: frozenset[str] = frozenset(
    ["--name"] + FLAG_DEFINITIONS["--name"].get("aliases", [])
)

# Spawnrate flag aliases (for graph-level pre-extraction)
_SPAWNRATE_FLAGS: frozenset[str] = frozenset(
    ["--spawnrate"] + FLAG_DEFINITIONS.get("--spawnrate", {}).get("aliases", [])
)

# ── Graph-only flags (never passed to build_query) ────────────────────────────
# Defined here at module level so they're shared between the command and any
# future helpers, and not rebuilt on every invocation.
_GRAPH_ONLY_FLAGS: frozenset[str] = frozenset({
    FLAG_ALLTIME, FLAG_WITHOUTLIERS, FLAG_SINCE, FLAG_BEFORE, FLAG_COMPARE,
})

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
# STATIC TEXT CONSTANTS
# Hoisted to module level so they're built once, not on every command invocation.
# ─────────────────────────────────────────────────────────────────────────────

_LEGEND_TEXT = (
    f"**📖 Reading the Graph**\n"
    f"{REPLY} **Dots** — every individual auction sale, plotted by date and price\n"
    f"{REPLY} **Avg Line** — smoothed average price over time; shows the general price direction\n"
    f"{REPLY} **Trend** (dashed) — linear regression line; green means price rising over time, red means falling\n"
    f"{REPLY} **Shaded band** — the middle 50% of sales (25th–75th percentile); wide band = inconsistent prices, narrow = stable market\n"
    f"{REPLY} **Chart Min / Chart Max markers** — the cheapest and most expensive sale visible on the graph (outliers excluded)\n\n"
    f"**📊 Stats Bar**\n"
    f"{REPLY} **Auctions** — total number of sales plotted\n"
    f"{REPLY} **Chart Min / Chart Max** — lowest and highest winning bid visible on the graph (outliers excluded)\n"
    f"{REPLY} **All-time Min / All-time Max** — the absolute lowest and highest sale ever recorded, including any outliers\n"
    f"{REPLY} **Avg** — mean price across all auctions\n"
    f"{REPLY} **Median** — middle price (less affected by extreme outliers than avg)\n"
    f"{REPLY} **Std Dev** — how spread out prices are; high = big price swings, low = consistent\n"
    f"{REPLY} **Trend** — average price change per sale (▲ rising, ▼ falling)\n"
    f"{REPLY} **Outliers** — sales so far above the typical price range they squash everything else. Excluded from the graph and most stats"
)

_FILTERS_BODY = (
    f"**🔍 Available Filters**\n"
    f"-# Use these with `a!g` — e.g. `a!g --n pikachu --sh` or `a!g --sh` for all shinies\n"
    f"{REPLY} `--n <value>` — Pokémon name, **repeatable** for multi-plot  _(--name, --pokemon)_\n"
    f"{REPLY} `--evo <value>` — Entire evo family merged as one series  _(--family, --fam)_\n"
    f"{REPLY} `--sr <value>` — Spawn rate e.g. `--sr 1/225` or `--sr 225`  _(--spawnrate)_\n"
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
    f"{REPLY} `--category <value>` — Category e.g. `rares`, `starters`  _(--cat)_\n"
    f"{REPLY} `--exclude <kind> <value>` — Exclude by name/type/region/category  _(--ex)_\n"
    f"{REPLY} `--price <value>` — Price filter e.g. `>5000`, `500-5000`  _(--p, --bid)_\n"
    f"{REPLY} `--limit <value>` — Limit to N most recent matches  _(--lim, --top)_\n"
    f"{REPLY} `--sort <value>` — Sort by `iv`, `bid`/`price`, `level`, `date`, `id` (append `+`/`-`)  _(--order)_\n"
    f"{REPLY} `--alltime` — 🕐 Show all historical data instead of {GRAPH_START_YEAR}+ only\n"
    f"{REPLY} `--withoutliers` — ⚠️ Plot ALL data including outliers (raw mode, may use log scale)\n"
    f"{REPLY} `--since <date>` — Only show auctions from this date onwards (YYYY, YYYY-MM, or YYYY-MM-DD)\n"
    f"{REPLY} `--before <date>` — Only show auctions before this date (YYYY, YYYY-MM, or YYYY-MM-DD)\n"
    f"{REPLY} `--compare <name> [name2 ...]` — Overlay up to 4 other Pokémon on the same graph"
)

_PROTIP_TEXT = (
    f"-# 💡 **Pro tip:** Use `--limit` to focus on the most recent auctions — "
    f"e.g. `j!g --name garchomp --limit 50` graphs only the latest 50 sales, "
    f"giving you a much cleaner picture of where prices stand today. "
    f"Add `--nosh` to exclude shinies if you only want non-shiny data. "
    f"By default both shiny and non-shiny are plotted together (e.g. `j!g --n meowth --iv >70`). "
    f"Want only the base form with no regional/alternate variants? Use `--n normal meowth` — "
    f"this excludes forms like Alolan Meowth, Galarian Meowth, or Gmax variants from the graph."
)


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
    return pd.Series(prices).rolling(window, center=True, min_periods=1).mean().to_numpy()


def _percentile_band(prices: np.ndarray, dates, window: int = 30):
    # Use pandas rolling quantile — O(n log n) vs the previous O(n²) loop.
    # window is in calendar days; convert to a row-count window as an approximation
    # (data is roughly uniform in time after subsampling, so this is fine).
    s   = pd.Series(prices)
    win = max(5, window)  # minimum sensible window
    p25 = s.rolling(win, center=True, min_periods=1).quantile(0.25).to_numpy()
    p75 = s.rolling(win, center=True, min_periods=1).quantile(0.75).to_numpy()
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


def _extract_repeatable_flag_values(
    tokens: list[str], flag_set: frozenset[str]
) -> tuple[list[str], list[str]]:
    """
    Extract ALL occurrences of any flag in flag_set (e.g. all --name / --n aliases).
    Each occurrence contributes exactly one value (the next non-flag token(s) until
    the following flag).
    Returns (list_of_names, remaining_tokens_without_those_flags_and_values).

    e.g. tokens = ["--n", "meowth", "--n", "iron", "valiant", "--sh"]
         → names = ["meowth", "iron valiant"],  remaining = ["--sh"]
    """
    names: list[str]     = []
    out:   list[str]     = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.lower() in flag_set:
            # Consume value tokens until next flag
            i += 1
            parts: list[str] = []
            while i < len(tokens) and not tokens[i].startswith("-"):
                parts.append(tokens[i])
                i += 1
            name = " ".join(parts).strip()
            if name:
                names.append(name)
        else:
            out.append(tok)
            i += 1
    return names, out


def _extract_flag_value_multi_alias(
    tokens: list[str], flag_set: frozenset[str]
) -> tuple[str | None, list[str]]:
    """
    Like _extract_flag_value but matches any token in flag_set (e.g. all --sr aliases).
    Returns the first match's value and the remaining tokens.
    """
    for flag in flag_set:
        if flag in tokens:
            return _extract_flag_value(tokens, flag)
    return None, tokens


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

        # Use a mutable container instead of nonlocal so the formatter is safe if
        # matplotlib ever calls it from multiple threads (unlikely with Agg, but clean).
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
    cols      = ["Pokémon", "Sales", "Chart Min", "Chart Max", "Avg", "Median", "Trend"]
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
) -> tuple[io.BytesIO, list, int, int]:
    """
    Build a dark-themed price history chart and return (buf, outliers, fetched_count, plotted_count).
    fetched_count is the number of records received before subsampling.
    plotted_count is the number of records after subsampling (actual dots on the graph).
    Records use short field names: ts = unix_timestamp, bid = winning_bid, pn = pokemon_name.
    If pokemon_name is provided it is used as the chart title instead of the DB's pn field
    (which can be a form name like 'Snowman Pikachu' even when the user asked for 'pikachu').
    """
    records = sorted(records, key=lambda r: r.get("ts", 0))

    # ── Capture fetched count BEFORE subsampling ───────────────────────────────
    fetched_count = len(records)

    # ── Capture true all-time min/max from the FULL dataset BEFORE subsampling ──
    # Subsampling with a step can silently skip the highest/lowest bid record.
    _all_prices_full = np.array([r["bid"] for r in records], dtype=float)
    _at_min_true     = float(_all_prices_full.min()) if len(_all_prices_full) else 0.0
    _at_max_true     = float(_all_prices_full.max()) if len(_all_prices_full) else 0.0

    if len(records) > MAX_POINTS:
        step    = len(records) // MAX_POINTS
        records = records[::step]

    # ── Capture plotted count AFTER subsampling ────────────────────────────────
    plotted_count = len(records)

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

    _annotate_point(idx_max, f"Chart Max\n{_format_price(prices_plot.max())}",
                    pal.get("trend_up",   "#06d6a0"), prefer_above=True)
    _annotate_point(idx_min, f"Chart Min\n{_format_price(prices_plot.min())}",
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

        # Use a mutable container instead of nonlocal — thread-safe if matplotlib
        # ever calls the formatter from multiple threads (unlikely with Agg, but clean).
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
    # Use the requested name if provided; otherwise build a summary from unique species.
    # Never fall back to records[0].pn — that would show a random Pokémon name.
    if pokemon_name:
        name = pokemon_name
    else:
        unique_pn = sorted({r.get("pn", "") for r in records if r.get("pn")})
        if len(unique_pn) == 1:
            name = unique_pn[0]
        elif len(unique_pn) <= 4:
            name = " / ".join(unique_pn)
        else:
            name = f"{len(unique_pn)} Pokémon"
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
    col_w = (S1 - S0) / 7

    # Use true all-time min/max captured before subsampling so large datasets
    # (e.g. 550M max) are never silently dropped by the step-sample.
    at_min = _at_min_true
    at_max = _at_max_true

    paired_cols = [
        ("Chart Min",    _format_price(p_min),  "Chart Max",    _format_price(p_max)),
        ("All-time Min", _format_price(at_min),  "All-time Max", _format_price(at_max)),
        ("Avg",          _format_price(p_avg),  "Median",       _format_price(p_med)),
        ("Auctions",     f"{total:,}",          None,           None),
        ("Std Dev",      _format_price(p_std),  None,           None),
        ("Trend",        f"{trend_arrow} {_format_price(abs(slope))}/sale", None, None),
        ("Outliers",     "All Included" if show_outliers else (f"{n_outliers} hidden" if n_outliers else "None"), None, None),
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

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    # Each outlier entry: (date, price, record, kind) — kind is "high" or "low"
    return buf, list(zip(outlier_dates, outlier_prices.tolist(), outlier_records, outlier_kinds)), fetched_count, plotted_count


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


@dataclass
class _RegenState:
    """Bundles the immutable state needed to regenerate a graph on toggle button press.

    Using a dataclass instead of a raw closure makes the captured state explicit,
    testable, and safe across concurrent requests (no shared mutable module-level vars).
    """
    records:      list
    query:        dict
    display_str:  str
    limit:        int | None
    since_dt:     datetime | None
    before_dt:    datetime | None
    pokemon_name: str
    variant:      str
    accent:       int
    heading:      str
    legend_text:  str
    filters_body: str
    protip_text:  str
    found_names:  list | None = None   # set for multi-name mode only
    variant_flags: list | None = None  # set for multi-name mode only


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class Graph(commands.Cog):
    """Price history graphs for Pokémon auctions"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Connect here so a bad URI raises at cog-load time, not at import time,
        # giving a clear startup error rather than a silent crash.
        # Store the client so cog_unload can close it and avoid leaking connections
        # on hot-reloads (common in dev).
        self._mongo = MongoClient(config.MONGO_URI)
        self._col   = self._mongo[config.MONGO_DB_NAME][config.MONGO_COLLECTION]

    def cog_unload(self):
        """Close the MongoDB connection when the cog is unloaded or reloaded."""
        self._mongo.close()

    @commands.hybrid_command(name="graph", aliases=["g", "chart"])
    @app_commands.describe(filters="Same filters as auction search e.g: --sh --iv >80, or --n pikachu --n meowth --sh")
    async def graph_command(self, ctx: commands.Context, *, filters: str = ""):
        """
        Show a price history graph for Pokémon auctions.

        No name needed — defaults to ALL Pokémon matching your filters.

        Examples:
          a!g --sh                            → shiny graph for ALL Pokémon
          a!g --n pikachu --sh               → shiny pikachu
          a!g --n meowth --n zorua --n ralts --sh  → plot 3 shinies on one graph
          a!g --evo pikachu --sh             → whole pikachu evo family merged as one line
          a!g --sr 1/225 --sh                → shiny graph for all 1/225 spawn-rate mons
          a!g --sr 225                       → all mons with 1/225 spawn rate
          a!g --n garchomp --since 2024-06-01
          a!g --compare mewtwo, iron valiant, brute bonnet --sh
        """
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

        # ── Extract multi-name flags (--n / --name, repeatable) ───────────────
        # e.g. --n meowth --n zorua --n ralts  → ["meowth", "zorua", "ralts"]
        # After extraction, raw no longer contains --n flags.
        multi_names, raw_no_names = _extract_repeatable_flag_values(raw, _MULTI_NAME_FLAGS_ALL)

        # ── Extract --sr / --spawnrate value ──────────────────────────────────
        sr_val, raw_no_names = _extract_flag_value_multi_alias(raw_no_names, _SPAWNRATE_FLAGS)

        # ── Extract --evo value from the remaining tokens ─────────────────────
        _evo_flags = frozenset(["--evo"] + FLAG_DEFINITIONS.get("--evo", {}).get("aliases", []))
        evo_val, raw_no_names = _extract_flag_value_multi_alias(raw_no_names, _evo_flags)

        # raw_no_names now has only modifier flags (--sh, --gmax, --iv, etc.)
        # We use these as the "variant/filter" tokens for every pokemon query.

        # ── Combined validation: invalid Pokémon names + unknown flags ─────────
        from filters import is_flag, is_category_shortcut, resolve_flag
        from utils import get_forms_db

        _EXTRACTED_FLAGS = _MULTI_NAME_FLAGS_ALL | _SPAWNRATE_FLAGS | _evo_flags | _GRAPH_ONLY_FLAGS

        _invalid_names: list[str] = []
        _unknown_flags: list[str] = []

        for mname in multi_names:
            check = mname
            if check.lower().endswith(" only"): check = check[:-5].strip()
            elif check.lower().startswith("normal "): check = check[7:].strip()
            if check and not get_forms_db().resolve_name_to_forms(check) and not resolve_pokemon_name(check):
                _invalid_names.append(check)

        if evo_val:
            _ec = evo_val.strip()
            if _ec and not get_forms_db().resolve_name_to_forms(_ec) and not resolve_pokemon_name(_ec):
                _invalid_names.append(_ec)

        for cname in compare_names:
            _cc = cname.strip()
            if _cc and not get_forms_db().resolve_name_to_forms(_cc) and not resolve_pokemon_name(_cc):
                _invalid_names.append(_cc)

        _j = 0
        while _j < len(raw_no_names):
            _tok = raw_no_names[_j]
            if _tok.startswith("-"):
                if not is_flag(_tok) and not is_category_shortcut(_tok) and _tok not in _EXTRACTED_FLAGS:
                    _unknown_flags.append(_tok)
                _canon = resolve_flag(_tok)
                _info  = FLAG_DEFINITIONS.get(_canon, {}) if _canon else {}
                _j += 1
                if _info.get("takes_arg"):
                    while _j < len(raw_no_names) and not raw_no_names[_j].startswith("-"):
                        _j += 1
            else:
                _j += 1

        if _invalid_names or _unknown_flags:
            _lines: list[str] = []
            for bad in _invalid_names:
                _lines.append(f"❌ **{bad}** is not a valid Pokémon name.")
            for uf in _unknown_flags:
                _lines.append(f"❌ Unknown filter: `{uf}`")
            _lines.append(f"{REPLY} Check your spelling or use `a!a h` to see all available filters.")
            await ctx.send(
                view=_error_view("\n".join(_lines)),
                reference=ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None,
                mention_author=False,
            )
            return

        # ── Determine display string (for subtitle) ────────────────────────────
        display_str = filters.strip() or "All auctions"

        if hasattr(ctx, "interaction") and ctx.interaction:
            await ctx.defer()
        else:
            await ctx.typing()

        # ── Build timestamp filter ─────────────────────────────────────────────
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
                merged: dict = {"$exists": True}
                for c in conditions:
                    merged.update(c.get("ts", {}))
                return merged
            return ts_f

        ts_filter = _build_ts_filter(use_alltime, since_dt, before_dt)

        # ── Helper: fetch records for one query dict ───────────────────────────
        projection = {
            "ts":  1, "bid": 1, "pn":  1,
            "sh":  1, "gx":  1, "iv":  1,
            "aid": 1, "lv":  1,
        }

        _col = self._col  # capture for the nested closures below

        def _fetch_sync(q: dict, lim: int | None = None) -> tuple[list[dict], bool]:
            """
            Synchronous MongoDB fetch — always called via _fetch() so it
            never blocks the asyncio event loop directly.
            """
            fetch_n = min(lim, MAX_FETCH) if lim is not None else MAX_FETCH
            # +1 lets us detect whether more records exist beyond the cap
            cur = _col.find(
                {**q, "ts": ts_filter, "bid": {"$exists": True}},
                projection,
            ).sort("ts", -1).limit(fetch_n + 1)
            recs = list(cur)
            capped = len(recs) > fetch_n
            if capped:
                recs = recs[:fetch_n]
            recs.sort(key=lambda r: r.get("ts", 0))
            return recs, capped

        async def _fetch(q: dict, lim: int | None = None) -> tuple[list[dict], bool]:
            """
            Async wrapper: offloads the blocking PyMongo call to a thread-pool
            executor so the Discord event loop (and its heartbeat) are never blocked.
            """
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, functools.partial(_fetch_sync, q, lim))

        # Use a mutable container so inner helpers (e.g. the multi-name loop) can
        # update the flag without needing `nonlocal`, and the intent is clear if the
        # code is later refactored into a separate helper function.
        _capped = [False]  # _capped[0] is True if any _fetch call hits MAX_FETCH

        ref = ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None

        # Use module-level constants — no need to rebuild these strings on every invocation.
        _legend_text  = _LEGEND_TEXT
        _filters_body = _FILTERS_BODY
        _protip_text  = _PROTIP_TEXT

        # ── Button factory (hoisted so all branches can reference it) ──────────
        def _build_btn_list(
            legend_text, filters_text, has_outliers, outlier_count, outlier_bytes,
            outlier_data, is_alltime, is_outliers, regenerate_fn,
        ):
            """
            Build the list of discord.ui.Button instances for the graph message.

            Previously used five nested classes defined inside this function, each
            closing over different variables.  That worked but was hard to test and
            debug.  Now each button is a small inner class that receives all its
            state through __init__ parameters, which is how discord.py intends it.
            """
            btn_list = []

            # ── 📖 How to Read This Graph ──────────────────────────────────────
            class _HowToReadBtn(discord.ui.Button):
                def __init__(self, _legend_text):
                    super().__init__(
                        style=discord.ButtonStyle.secondary,
                        label="📖 How to Read This Graph",
                        custom_id="g_legend",
                    )
                    self._legend_text = _legend_text
                async def callback(self, interaction: discord.Interaction):
                    class LegendView(discord.ui.LayoutView):
                        c = discord.ui.Container(
                            discord.ui.TextDisplay(content=legend_text),
                            accent_colour=config.EMBED_COLOR,
                        )
                    await interaction.response.send_message(view=LegendView(), ephemeral=True)

            btn_list.append(_HowToReadBtn(legend_text))

            # ── 🔍 Available Filters ───────────────────────────────────────────
            class _FiltersBtn(discord.ui.Button):
                def __init__(self, _filters_text):
                    super().__init__(
                        style=discord.ButtonStyle.secondary,
                        label="🔍 Available Filters",
                        custom_id="g_filters",
                    )
                    self._filters_text = _filters_text
                async def callback(self, interaction: discord.Interaction):
                    class FiltersView(discord.ui.LayoutView):
                        c = discord.ui.Container(
                            discord.ui.TextDisplay(content=self._filters_text),
                            accent_colour=config.EMBED_COLOR,
                        )
                    await interaction.response.send_message(view=FiltersView(), ephemeral=True)

            btn_list.append(_FiltersBtn(filters_text))

            # ── 🕐 All-time / Since 2024 toggle ───────────────────────────────
            class _AlltimeBtn(discord.ui.Button):
                def __init__(self, _is_alltime, _is_outliers, _regenerate_fn):
                    if _is_alltime:
                        _style = discord.ButtonStyle.success
                        _label = f"📅 Since {GRAPH_START_YEAR} Only"
                    else:
                        _style = discord.ButtonStyle.secondary
                        _label = "🕐 Show All-time Data"
                    super().__init__(style=_style, label=_label, custom_id="g_alltime")
                    self._is_alltime    = _is_alltime
                    self._is_outliers   = _is_outliers
                    self._regenerate_fn = _regenerate_fn
                async def callback(self, interaction: discord.Interaction):
                    await self._regenerate_fn(interaction, not self._is_alltime, self._is_outliers)

            btn_list.append(_AlltimeBtn(is_alltime, is_outliers, regenerate_fn))

            # ── ⚠️ Outliers toggle ─────────────────────────────────────────────
            class _OutliersToggleBtn(discord.ui.Button):
                def __init__(self, _is_alltime, _is_outliers, _regenerate_fn):
                    if _is_outliers:
                        _style = discord.ButtonStyle.danger
                        _label = "📊 Hide Outliers (Clean View)"
                    else:
                        _style = discord.ButtonStyle.secondary
                        _label = "⚠️ Include Outliers too"
                    super().__init__(style=_style, label=_label, custom_id="g_outliers_toggle")
                    self._is_alltime    = _is_alltime
                    self._is_outliers   = _is_outliers
                    self._regenerate_fn = _regenerate_fn
                async def callback(self, interaction: discord.Interaction):
                    await self._regenerate_fn(interaction, self._is_alltime, not self._is_outliers)

            btn_list.append(_OutliersToggleBtn(is_alltime, is_outliers, regenerate_fn))

            # ── Outlier detail viewer ──────────────────────────────────────────
            if has_outliers and not is_outliers:
                n_high = sum(1 for e in outlier_data if (e[3] if len(e) == 4 else "high") == "high")
                n_low  = outlier_count - n_high
                parts  = []
                if n_high: parts.append(f"▲{n_high} overpriced")
                if n_low:  parts.append(f"▼{n_low} sniped")
                detail_label = f"📋 View {outlier_count} Excluded Sale(s) ({', '.join(parts)})"

                class _OutlierDetailBtn(discord.ui.Button):
                    def __init__(self, _label, _ob, _oc):
                        super().__init__(
                            style=discord.ButtonStyle.secondary,
                            label=_label,
                            custom_id="g_outlier_detail",
                        )
                        # Store raw bytes so the button remains usable even after
                        # the original BytesIO is closed post-send.
                        self._ob_bytes = _ob.getvalue() if _ob is not None else None
                        self._oc = _oc
                    async def callback(self, interaction: discord.Interaction):
                        if not self._ob_bytes:
                            await interaction.response.send_message(
                                "❌ Outlier image unavailable.", ephemeral=True
                            )
                            return
                        # Reconstruct a fresh BytesIO on every click — always seekable.
                        out_f = discord.File(io.BytesIO(self._ob_bytes), filename="outliers.png")
                        _oc   = self._oc
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

                btn_list.append(_OutlierDetailBtn(detail_label, outlier_bytes, outlier_count))

            return btn_list

        # ── COMPARE MODE ──────────────────────────────────────────────────────
        if compare_names:
            _no_name_flag = not multi_names
            if _no_name_flag:
                if len(compare_names) < 2:
                    await ctx.send(
                        view=_error_view(
                            "❌ `--compare` needs at least 2 Pokémon names.\n"
                            f"{REPLY} Example: `a!g --compare mewtwo, iron valiant, brute bonnet --sh`"
                        ),
                        reference=ref, mention_author=False,
                    )
                    return
                _primary_cname = compare_names[0]
                compare_names  = compare_names[1:]
                _variant_flags = [t for t in raw_no_names if t in ("--sh", "--shiny", "--gmax", "--noshiny")]
                praw   = ["--name", _primary_cname] + _variant_flags
                query, _, limit = build_query(praw, expand_name_by_dex=True)
                _requested_name = _primary_cname.title()
            else:
                _variant_flags  = [t for t in raw_no_names if t in ("--sh", "--shiny", "--gmax", "--noshiny")]
                praw = ["--name", multi_names[0]] + _variant_flags
                query, _, limit = build_query(praw, expand_name_by_dex=True)
                _requested_name = multi_names[0].title()
                compare_names = (multi_names[1:] if len(multi_names) > 1 else []) + compare_names

            if len(compare_names) > 4:
                await ctx.send(
                    view=_error_view("❌ Maximum 4 Pokémon in compare mode (5 total including primary)."),
                    reference=ref, mention_author=False,
                )
                return

            primary_records, _primary_capped = await _fetch(query)
            if not primary_records:
                await ctx.send(
                    view=_error_view("❌ No auctions found for the primary Pokémon."),
                    reference=ref, mention_author=False,
                )
                return

            primary_name    = _requested_name or primary_records[0].get("pn", "Unknown")
            primary_variant = _detect_variant(query)
            series = [{"name": primary_name, "records": primary_records, "variant": primary_variant}]

            for cname in compare_names:
                _variant_flags = [t for t in raw_no_names if t in ("--sh", "--shiny", "--gmax", "--noshiny")]
                craw = ["--name", cname] + _variant_flags
                cquery, _, _ = build_query(craw, expand_name_by_dex=True)
                crecs, _ = await _fetch(cquery)
                if not crecs:
                    await ctx.send(
                        view=_error_view(f"❌ No auctions found for `{cname}` — skipping."),
                        reference=ref, mention_author=False,
                    )
                    continue
                series.append({"name": cname.title(), "records": crecs, "variant": primary_variant})

            if len(series) < 2:
                await ctx.send(
                    view=_error_view("❌ Need at least 2 Pokémon with data to compare."),
                    reference=ref, mention_author=False,
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
                    reference=ref, mention_author=False,
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

        # ── MULTI-NAME MODE  (--n meowth --n zorua --n ralts) ─────────────────
        # Fetch all named pokemon and merge into ONE pool → single build_graph call.
        # This is NOT compare mode. Use --compare for separate overlaid series.
        if len(multi_names) > 1:
            _variant_flags = list(raw_no_names)
            _merged_records: list[dict] = []
            _found_names:    list[str]  = []
            for mname in multi_names:
                mraw              = ["--name", mname] + _variant_flags
                mquery, _, mlimit = build_query(mraw, expand_name_by_dex=True)
                mrecs, _mc        = await _fetch(mquery, mlimit)
                if _mc: _capped[0] = True
                if mrecs:
                    _merged_records.extend(mrecs)
                    _found_names.append(mname.title())

            if not _merged_records:
                await ctx.send(
                    view=_error_view("❌ No auctions found for any of the specified Pokémon."),
                    reference=ref, mention_author=False,
                )
                return

            if len(_merged_records) < 3:
                await ctx.send(
                    view=_error_view(
                        f"❌ Only **{len(_merged_records)}** auction(s) found across all specified Pokémon — need at least 3.\n"
                        f"{REPLY} Try broadening your filters."
                    ),
                    reference=ref, mention_author=False,
                )
                return

            # Build a display name: list up to 4 names, then "+ N more"
            if len(_found_names) <= 4:
                _multi_display_name = " / ".join(_found_names)
            else:
                _multi_display_name = f"{', '.join(_found_names[:4])} + {len(_found_names) - 4} more"

            # Use the variant/query from the first successful name for palette detection
            _first_mraw              = ["--name", multi_names[0]] + _variant_flags
            _first_mquery, _, _      = build_query(_first_mraw, expand_name_by_dex=True)

            try:
                buf, outliers, _fetched_count, _plotted_count = build_graph(
                    _merged_records, _first_mquery, display_str,
                    alltime=use_alltime,
                    show_outliers=use_outliers,
                    pokemon_name=_multi_display_name,
                )
            except Exception as e:
                await ctx.send(
                    view=_error_view(f"❌ Failed to generate graph: `{e}`"),
                    reference=ref, mention_author=False,
                )
                return

            # Re-use the standard single-graph response path from here
            multi_name      = _multi_display_name
            total           = len(_merged_records)
            variant         = _detect_variant(_first_mquery)
            pal             = _PALETTE[variant]
            disc_tag        = _DISCORD_TAG[variant]
            accent          = config.SHINY_EMBED_COLOR if variant == "shiny" else config.EMBED_COLOR
            heading         = f"## {disc_tag} {multi_name} — Price History".strip()
            _cap_note       = " (capped)" if _capped[0] else ""
            alltime_badge   = "  •  🕐 All-time" if use_alltime else ""
            since_badge     = f"  •  📅 Since {since_dt.strftime('%b %Y')}" if since_dt else ""
            before_badge    = f"  •  📅 Before {before_dt.strftime('%b %Y')}" if before_dt else ""
            outliers_badge  = "  •  ⚠️ Raw data (all outliers included)" if use_outliers else ""
            sub             = f"_{_fetched_count:,} fetched{_cap_note}  •  {_plotted_count:,} plotted  •  {len(_found_names)} Pokémon{alltime_badge}{since_badge}{before_badge}{outliers_badge}  •  filters: `{display_str}`_"
            file            = discord.File(buf, filename="graph.png")

            _has_outliers   = len(outliers) > 0
            _outlier_count  = len(outliers)
            _outlier_bytes  = build_outlier_image(outliers, multi_name, variant) if outliers else None
            _legend_capture = _legend_text  # defined below — forward ref safe at runtime

            async def _regenerate_graph_multi(interaction: discord.Interaction, new_alltime: bool, new_outliers: bool):
                await interaction.response.defer()
                new_ts    = _build_ts_filter(new_alltime, since_dt, before_dt)
                new_recs: list[dict] = []
                for mname in multi_names:
                    mraw2             = ["--name", mname] + _variant_flags
                    mq2, _, ml2       = build_query(mraw2, expand_name_by_dex=True)
                    _mr, _ = await _fetch(mq2, ml2)
                    new_recs.extend(_mr)
                    del _mr
                if not new_recs:
                    await interaction.followup.send("❌ No data found.", ephemeral=True)
                    return
                try:
                    new_buf, new_outlier_data, _new_fetched, _new_plotted = build_graph(
                        new_recs, _first_mquery, display_str,
                        alltime=new_alltime,
                        show_outliers=new_outliers,
                        pokemon_name=_multi_display_name,
                    )
                except Exception as exc:
                    await interaction.followup.send(f"❌ `{exc}`", ephemeral=True)
                    return
                new_file          = discord.File(new_buf, filename="graph.png")
                new_ob_bytes      = build_outlier_image(new_outlier_data, multi_name, variant) if new_outlier_data else None
                new_alltime_b     = "  •  🕐 All-time" if new_alltime else ""
                new_outliers_b    = "  •  ⚠️ Raw data (all outliers included)" if new_outliers else ""
                new_sub           = f"_{_new_fetched:,} fetched  •  {_new_plotted:,} plotted  •  {len(_found_names)} Pokémon{new_alltime_b}{since_badge}{before_badge}{new_outliers_b}  •  filters: `{display_str}`_"
                new_btn_list      = _build_btn_list(
                    legend_text=_legend_text,
                    filters_text=_filters_body,
                    has_outliers=bool(new_outlier_data),
                    outlier_count=len(new_outlier_data),
                    outlier_bytes=new_ob_bytes,
                    outlier_data=new_outlier_data,
                    is_alltime=new_alltime,
                    is_outliers=new_outliers,
                    regenerate_fn=_regenerate_graph_multi,
                )
                new_container_comps = [
                    discord.ui.TextDisplay(content=heading),
                    discord.ui.TextDisplay(content=new_sub),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://graph.png")),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.TextDisplay(content=_protip_text),
                ]
                class _NewMultiView(discord.ui.LayoutView):
                    container  = discord.ui.Container(*new_container_comps, accent_colour=accent)
                    action_row = discord.ui.ActionRow(*new_btn_list)
                    def __init__(self): super().__init__(timeout=300)
                await interaction.edit_original_response(attachments=[new_file], view=_NewMultiView())

            _btn_list_multi = _build_btn_list(
                legend_text=_legend_text,
                filters_text=_filters_body,
                has_outliers=_has_outliers,
                outlier_count=_outlier_count,
                outlier_bytes=_outlier_bytes,
                outlier_data=outliers,
                is_alltime=use_alltime,
                is_outliers=use_outliers,
                regenerate_fn=_regenerate_graph_multi,
            )

            _container_comps_multi = [
                discord.ui.TextDisplay(content=heading),
                discord.ui.TextDisplay(content=sub),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://graph.png")),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(content=_protip_text),
            ]

            class MultiView(discord.ui.LayoutView):
                container  = discord.ui.Container(*_container_comps_multi, accent_colour=accent)
                action_row = discord.ui.ActionRow(*_btn_list_multi)
                def __init__(self): super().__init__(timeout=300)

            await ctx.send(view=MultiView(), file=file, reference=ref, mention_author=False)
            return

        # ── SINGLE / ALL MODE ─────────────────────────────────────────────────
        # Determine the query.  Priority:
        #   1. --evo <name>    → whole evo family merged into one series
        #   2. --n <name>      → single named Pokémon (with form expansion)
        #   3. --sr <rate>     → all Pokémon at that spawn rate
        #   4. (nothing)       → all Pokémon matching modifier flags

        _requested_name: str | None = None

        if evo_val:
            # Build query that includes the whole evo family
            evo_tokens = ["--evo", evo_val] + list(raw_no_names)
            query, _, limit = build_query(evo_tokens, expand_name_by_dex=True)
            _requested_name = f"{evo_val.title()} family"

        elif multi_names:
            # Single name (len == 1, already handled multi above)
            single_name = multi_names[0]
            name_tokens = ["--name", single_name] + list(raw_no_names)
            query, _, limit = build_query(name_tokens, expand_name_by_dex=True)
            _requested_name = single_name.title()

        elif sr_val:
            # Spawn-rate filter: resolve names and intersect
            sr_tokens = ["--spawnrate", sr_val] + list(raw_no_names)
            query, _, limit = build_query(sr_tokens, expand_name_by_dex=True)
            # Validate that we actually resolved any names
            sr_names = get_names_by_spawnrate(sr_val)
            if not sr_names:
                db = get_spawnrate_db()
                valid = ", ".join(f"1/{d}" for d in sorted(db.all_denominators())[:12])
                await ctx.send(
                    view=_error_view(
                        f"❌ No Pokémon found for spawn rate `{sr_val}`.\n"
                        f"{REPLY} Valid rates include: {valid} …\n"
                        f"{REPLY} Try `--sr 225` or `--sr 1/225`."
                    ),
                    reference=ref, mention_author=False,
                )
                return
            _requested_name = f"1/{sr_val.split('/')[-1]} spawn rate"

        else:
            # No name, no evo, no sr — all Pokémon (filtered by modifier flags only)
            query, _, limit = build_query(list(raw_no_names), expand_name_by_dex=True)
            _requested_name = None   # computed after fetch from actual data

        records, _mc = await _fetch(query, limit)
        if _mc:
            _capped[0] = True

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

        # ── Resolve the display name AFTER fetch so we know exactly what's in the data ──
        # For named queries (_requested_name already set) just use it.
        # For all-pokemon / broad queries, compute from unique species in results.
        if _requested_name is None:
            unique_pn = sorted({r.get("pn", "") for r in records if r.get("pn")})
            n_unique  = len(unique_pn)
            if n_unique == 1:
                _requested_name = unique_pn[0]
            elif n_unique <= 3:
                _requested_name = " / ".join(unique_pn)
            else:
                _requested_name = f"{n_unique} Pokémon"

        try:
            buf, outliers, _fetched_count, _plotted_count = build_graph(
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

        # _requested_name is always set by this point (either from flags or computed above).
        name      = _requested_name
        total     = len(records)
        variant   = _detect_variant(query)
        pal       = _PALETTE[variant]
        disc_tag  = _DISCORD_TAG[variant]
        accent    = config.SHINY_EMBED_COLOR if variant == "shiny" else config.EMBED_COLOR

        heading         = f"## {disc_tag} {name} — Price History".strip()
        _cap_note       = " (capped)" if _capped[0] else ""
        alltime_badge   = "  •  🕐 All-time" if use_alltime else ""
        since_badge     = f"  •  📅 Since {since_dt.strftime('%b %Y')}" if since_dt else ""
        before_badge    = f"  •  📅 Before {before_dt.strftime('%b %Y')}" if before_dt else ""
        outliers_badge  = "  •  ⚠️ Raw data (all outliers included)" if use_outliers else ""
        sub        = f"_{_fetched_count:,} fetched{_cap_note}  •  {_plotted_count:,} plotted{alltime_badge}{since_badge}{before_badge}{outliers_badge}  •  filters: `{display_str}`_"

        file = discord.File(buf, filename="graph.png")

        # ── Build outlier BytesIO if needed — used only by the button callback ─
        out_buf = None
        if outliers:
            out_buf = build_outlier_image(outliers, name, variant)

        # ── Close-over state for toggle button callbacks ───────────────────────
        _regen_state = _RegenState(
            records      = records,
            query        = query,
            display_str  = display_str,
            limit        = limit,
            since_dt     = since_dt,
            before_dt    = before_dt,
            pokemon_name = name,
            variant      = variant,
            accent       = accent,
            heading      = heading,
            legend_text  = _legend_text,
            filters_body = _filters_body,
            protip_text  = _protip_text,
        )

        # Captured state needed to regenerate the graph on toggle
        _outlier_count  = len(outliers)
        _has_outliers   = bool(outliers)
        _legend_capture = _legend_text
        _outlier_bytes  = out_buf  # BytesIO | None

        async def _regenerate_graph(
            interaction: discord.Interaction,
            new_alltime: bool,
            new_outliers: bool,
        ):
            """Rebuild and edit the message with toggled view flags.

            For the alltime toggle we already have all the records in
            _regen_state.records — we just apply a different timestamp filter in
            memory instead of making an extra DB round-trip, which feels noticeably
            faster.  A new DB fetch is only needed if some other state changes
            (which currently never happens from the buttons).
            """
            await interaction.response.defer()
            st = _regen_state

            # Apply the timestamp filter in-memory — no DB round-trip needed.
            new_ts_filter = _build_ts_filter(new_alltime, st.since_dt, st.before_dt)
            _gte = new_ts_filter.get("$gte")
            _lt  = new_ts_filter.get("$lt")
            new_records = [
                r for r in st.records
                if (_gte is None or r.get("ts", 0) >= _gte)
                and (_lt  is None or r.get("ts", 0) <  _lt)
            ]
            _regen_capped = False  # no new fetch, so no new cap

            if not new_records:
                await interaction.followup.send("❌ No data found.", ephemeral=True)
                return

            try:
                new_buf, new_out, _new_fetched, _new_plotted = build_graph(
                    new_records, st.query, st.display_str,
                    alltime=new_alltime,
                    show_outliers=new_outliers,
                    pokemon_name=st.pokemon_name,
                )
            except Exception as exc:
                await interaction.followup.send(f"❌ Failed to regenerate: `{exc}`", ephemeral=True)
                return

            new_file = discord.File(new_buf, filename="graph.png")

            n_out_new       = len(new_out)
            has_out_new     = bool(new_out)
            out_buf_new     = build_outlier_image(new_out, st.pokemon_name, st.variant) if has_out_new else None
            alltime_badge_n = "  •  🕐 All-time" if new_alltime else ""
            since_badge_n   = f"  •  📅 Since {st.since_dt.strftime('%b %Y')}" if st.since_dt else ""
            before_badge_n  = f"  •  📅 Before {st.before_dt.strftime('%b %Y')}" if st.before_dt else ""
            out_badge_n     = "  •  ⚠️ Raw data" if new_outliers else ""
            _regen_cap_note = " (capped)" if _regen_capped else ""
            new_sub         = (
                f"_{_new_fetched:,} fetched{_regen_cap_note}  •  {_new_plotted:,} plotted"
                f"{alltime_badge_n}{since_badge_n}{before_badge_n}{out_badge_n}  •  filters: `{st.display_str}`_"
            )

            # Rebuild buttons with updated toggle state
            new_btn_list = _build_btn_list(
                legend_text=st.legend_text,
                filters_text=st.filters_body,
                has_outliers=has_out_new,
                outlier_count=n_out_new,
                outlier_bytes=out_buf_new,
                outlier_data=new_out,
                is_alltime=new_alltime,
                is_outliers=new_outliers,
                regenerate_fn=_regenerate_graph,
            )

            new_container_comps = [
                discord.ui.TextDisplay(content=st.heading),
                discord.ui.TextDisplay(content=new_sub),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media="attachment://graph.png"),
                ),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(content=st.protip_text),
            ]

            class NewGraphView(discord.ui.LayoutView):
                container = discord.ui.Container(
                    *new_container_comps,
                    accent_colour=st.accent,
                )
                action_row = discord.ui.ActionRow(*new_btn_list)
                def __init__(self):
                    super().__init__(timeout=300)

            await interaction.edit_original_response(
                attachments=[new_file],
                view=NewGraphView(),
            )

        # ── Build initial button list and view ────────────────────────────────
        _btn_list = _build_btn_list(
            legend_text=_regen_state.legend_text,
            filters_text=_regen_state.filters_body,
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

        # ── Free large objects from memory now that the response is sent ──────
        # NOTE: do NOT call records.clear() here — _regen_state.records points to
        # the same list and is needed by the toggle buttons for in-memory filtering.
        # The list will be garbage-collected when the view times out (300 s) and
        # _regen_state goes out of scope.
        outliers.clear()
        buf.close()
        if out_buf is not None:
            out_buf.close()

# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Graph(bot))
