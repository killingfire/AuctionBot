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

import asyncio
import functools
import gc
import io
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import discord
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.lines
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np
import pandas as pd
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None
    _HAS_PSUTIL = False

import config
from config import REPLY
from filters import FLAG_DEFINITIONS
from utils import (
    build_query,
    get_forms_db,
    get_names_by_spawnrate,
    get_spawnrate_db,
    resolve_pokemon_name,
    shiny_prefix,
)

log = logging.getLogger(__name__)

# ─── Derived flag sets (stay in sync with filters.py automatically) ───────────

_NAME_FLAGS: frozenset[str] = frozenset(
    ["--name"] + FLAG_DEFINITIONS["--name"].get("aliases", [])
)
_SPAWNRATE_FLAGS: frozenset[str] = frozenset(
    ["--spawnrate"] + FLAG_DEFINITIONS.get("--spawnrate", {}).get("aliases", [])
)
_EVO_FLAGS: frozenset[str] = frozenset(
    ["--evo"] + FLAG_DEFINITIONS.get("--evo", {}).get("aliases", [])
)

# ─── Graph-only CLI flags (stripped before build_query) ───────────────────────

FLAG_ALLTIME      = "--alltime"
FLAG_WITHOUTLIERS = "--withoutliers"
FLAG_SINCE        = "--since"
FLAG_BEFORE       = "--before"
FLAG_COMPARE      = "--compare"

_GRAPH_ONLY_FLAGS: frozenset[str] = frozenset({
    FLAG_ALLTIME, FLAG_WITHOUTLIERS, FLAG_SINCE, FLAG_BEFORE, FLAG_COMPARE,
})

# ─── Theme ────────────────────────────────────────────────────────────────────

BG_DARK    = "#0f1117"
BG_CARD    = "#1a1d27"
GRID_COLOR = "#2a2d3a"
TEXT_COLOR = "#e8eaf0"
MUTED_COLOR = "#6c7086"

_PALETTE = {
    "shiny": {
        "dot": "#ffd166", "line": "#f4a261", "fill": "#ffd16622",
        "tag": "[Shiny]", "trend_up": "#06d6a0", "trend_down": "#ef476f",
    },
    "gmax": {
        "dot": "#ff6b6b", "line": "#ff4d6d", "fill": "#ff6b6b22",
        "tag": "[Gmax]", "trend_up": "#06d6a0", "trend_down": "#ef476f",
    },
    "normal": {
        "dot": "#4cc9f0", "line": "#7b2fff", "fill": "#4cc9f022",
        "tag": "", "trend_up": "#06d6a0", "trend_down": "#ef476f",
    },
}

_DISCORD_TAG = {"shiny": "✨ Shiny", "gmax": "⚡ Gigantamax", "normal": ""}

# Overlay palette for compare mode (dot, line, fill)
_OVERLAY_PALETTE = [
    ("#4cc9f0", "#7b2fff", "#4cc9f015"),
    ("#f72585", "#b5179e", "#f7258515"),
    ("#06d6a0", "#118ab2", "#06d6a015"),
    ("#ffd166", "#f4a261", "#ffd16615"),
    ("#ff6b6b", "#ff4d6d", "#ff6b6b15"),
]

# ─── Limits ───────────────────────────────────────────────────────────────────

MAX_POINTS          = 4_000   # dots rendered on chart (subsampled if more)
MAX_FETCH           = 20_000  # hard cap on DB records per query
GRAPH_START_YEAR    = 2024    # default year cutoff for the initial view
COMPARE_MODAL_SLOTS = 5       # inputs in the Compare modal (Discord max: 5)
OUTLIER_PAGE_SIZE   = 70      # rows per outlier table image

# ─── Memory safety ────────────────────────────────────────────────────────────

MEMORY_FREE_PCT_MIN = 30  # refuse new graphs below this % free RAM
MEMORY_WARN_PCT     = 45  # log warning but proceed below this %


class _LowMemoryError(RuntimeError):
    """Raised when available RAM is critically low."""


def _free_memory() -> None:
    """Close cached matplotlib figures and run GC."""
    plt.close("all")
    gc.collect()


def _check_memory() -> None:
    """Raise _LowMemoryError if free RAM is below MEMORY_FREE_PCT_MIN."""
    if not _HAS_PSUTIL or MEMORY_FREE_PCT_MIN <= 0:
        return
    vm = _psutil.virtual_memory()
    free_pct = vm.available * 100 / vm.total
    if free_pct < MEMORY_WARN_PCT:
        _free_memory()
        vm = _psutil.virtual_memory()
        free_pct = vm.available * 100 / vm.total
        log.warning(
            "graph.py: low RAM — %.1f%% free (%d MB); matplotlib figures cleared",
            free_pct, vm.available // 1024 // 1024,
        )
    if free_pct < MEMORY_FREE_PCT_MIN:
        _free_memory()
        vm2 = _psutil.virtual_memory()
        free_pct2 = vm2.available * 100 / vm2.total
        if free_pct2 < MEMORY_FREE_PCT_MIN:
            raise _LowMemoryError(
                f"Only {free_pct2:.1f}% RAM free ({vm2.available // 1024 // 1024} MB)"
            )


# ─── Static text (built once at import time) ──────────────────────────────────

_LEGEND_TEXT = (
    f"**📖 Reading the Graph**\n"
    f"{REPLY} **Dots** — every individual auction sale, plotted by date and price\n"
    f"{REPLY} **Avg Line** — smoothed average price over time\n"
    f"{REPLY} **Trend** (dashed) — linear regression; green = rising, red = falling\n"
    f"{REPLY} **Shaded band** — middle 50% of sales (25th–75th percentile)\n"
    f"{REPLY} **Chart Min / Chart Max** — cheapest and most expensive visible sale\n\n"
    f"**📊 Stats Bar**\n"
    f"{REPLY} **Auctions** — total sales plotted\n"
    f"{REPLY} **Chart Min / Chart Max** — lowest/highest bid on the graph (outliers excluded)\n"
    f"{REPLY} **All-time Min / All-time Max** — lowest/highest in the fetched sample\n"
    f"{REPLY} **Avg** — mean price\n"
    f"{REPLY} **Median** — middle price (less affected by extremes than avg)\n"
    f"{REPLY} **Std Dev** — price spread; high = volatile, low = stable\n"
    f"{REPLY} **Trend** — average price change per sale (▲ rising, ▼ falling)\n"
    f"{REPLY} **Outliers** — extreme sales excluded from the graph\n\n"
    f"**🔢 Subtitle Numbers**\n"
    f"{REPLY} **sampled from DB** — records pulled from MongoDB\n"
    f"{REPLY} **db has more** — fetch cap hit; more records exist\n"
    f"{REPLY} **dots on graph** — records actually rendered (after subsampling)"
)

_FILTERS_BODY = (
    f"**🔍 Available Filters**\n"
    f"-# Use these with `a!g` — e.g. `a!g --n pikachu --sh`\n"
    f"{REPLY} `--n <value>` — Pokémon name, **repeatable**  _(--name, --pokemon)_\n"
    f"{REPLY} `--evo <value>` — Entire evo family  _(--family, --fam)_\n"
    f"{REPLY} `--sr <value>` — Spawn rate e.g. `--sr 1/225`  _(--spawnrate)_\n"
    f"{REPLY} `--shiny` — Shiny only  _(--sh)_\n"
    f"{REPLY} `--gmax` — Gigantamax only  _(--gm, --giga)_\n"
    f"{REPLY} `--noshiny` — Exclude shinies  _(--nosh)_\n"
    f"{REPLY} `--nogmax` — Exclude Gigantamax  _(--nogm)_\n"
    f"{REPLY} `--iv <value>` — Total IV % e.g. `>90`, `90-100`  _(--totaliv)_\n"
    f"{REPLY} `--hpiv / --atkiv / --defiv / --spatkiv / --spdefiv / --spdiv <value>` — Individual IVs\n"
    f"{REPLY} `--level <value>` — Level  _(--lv, --lvl)_\n"
    f"{REPLY} `--nature <value>` — Nature  _(--nat)_\n"
    f"{REPLY} `--move <value>` — Stackable  _(-m, --moves)_\n"
    f"{REPLY} `--gender <value>` — `male`, `female`, or `unknown`  _(--g)_\n"
    f"{REPLY} `--type <value>` — Stackable up to 2  _(--t)_\n"
    f"{REPLY} `--region <value>` — e.g. `kanto`  _(--r)_\n"
    f"{REPLY} `--category <value>` — e.g. `rares`  _(--cat)_\n"
    f"{REPLY} `--exclude <kind> <value>` — Exclude by name/type/region/category  _(--ex)_\n"
    f"{REPLY} `--price <value>` — e.g. `>5000`  _(--p, --bid)_\n"
    f"{REPLY} `--limit <value>` — Limit to N most recent  _(--lim, --top)_\n"
    f"{REPLY} `--sort <value>` — Sort by `iv`, `bid`, `level`, `date`, `id`  _(--order)_\n"
    f"{REPLY} `--alltime` — 🕐 Show all historical data\n"
    f"{REPLY} `--withoutliers` — ⚠️ Plot raw data including outliers\n"
    f"{REPLY} `--since <date>` — From date onwards (YYYY, YYYY-MM, or YYYY-MM-DD)\n"
    f"{REPLY} `--before <date>` — Before date (YYYY, YYYY-MM, or YYYY-MM-DD)\n"
    f"{REPLY} `--compare <name> [name2 ...]` — Overlay up to 4 Pokémon on one graph"
)

_PROTIP_TEXT = (
    f"-# 💡 **Pro tip:** Use `--limit` to focus on recent auctions — "
    f"e.g. `j!g --name garchomp --limit 50`. "
    f"Add `--nosh` to exclude shinies. "
    f"Use `--n normal meowth` to exclude regional/alternate forms."
)


# ─────────────────────────────────────────────────────────────────────────────
# PURE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _detect_variant(query: dict) -> str:
    """Return 'shiny', 'gmax', or 'normal' based on the query dict."""
    if query.get("sh") is True:
        return "shiny"
    if query.get("gx") is True:
        return "gmax"
    return "normal"


def _format_price(val: float) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 10_000:
        return f"{val / 1_000:.1f}k"
    if val >= 1_000:
        return f"{val / 1_000:.2f}k"
    return f"{int(val):,}"


def _smart_yticks(p_min: float, p_max: float) -> np.ndarray:
    price_range = p_max - p_min or p_max or 1
    raw_step    = price_range / 6
    magnitude   = 10 ** np.floor(np.log10(raw_step)) if raw_step > 0 else 1
    step = min([1, 2, 2.5, 5, 10], key=lambda s: abs(s * magnitude - raw_step)) * magnitude
    start = np.floor(max(0, p_min - price_range * 0.1) / step) * step
    stop  = np.ceil((p_max + price_range * 0.1) / step) * step
    return np.arange(start, stop + step, step)


def _rolling_average(prices: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(prices).rolling(window, center=True, min_periods=1).mean().to_numpy()


def _percentile_band(prices: np.ndarray, window: int = 30):
    s   = pd.Series(prices)
    win = max(5, window)
    p25 = s.rolling(win, center=True, min_periods=1).quantile(0.25).to_numpy()
    p75 = s.rolling(win, center=True, min_periods=1).quantile(0.75).to_numpy()
    return p25, p75


def _parse_date_flag(value: str) -> datetime | None:
    """Parse YYYY, YYYY-MM, or YYYY-MM-DD into a UTC datetime."""
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _extract_flag_value(tokens: list[str], flag: str) -> tuple[str | None, list[str]]:
    """Pull the single value after *flag* and return (value, remaining_tokens)."""
    try:
        idx   = tokens.index(flag)
        value = tokens[idx + 1] if idx + 1 < len(tokens) else None
        remaining = tokens[:idx] + tokens[idx + (2 if value else 1):]
        return value, remaining
    except ValueError:
        return None, tokens


def _extract_flag_value_multi_alias(
    tokens: list[str], flag_set: frozenset[str]
) -> tuple[str | None, list[str]]:
    """Like _extract_flag_value but matches any alias in *flag_set*."""
    for flag in flag_set:
        if flag in tokens:
            return _extract_flag_value(tokens, flag)
    return None, tokens


def _extract_flag_values(tokens: list[str], flag: str) -> tuple[list[str], list[str]]:
    """
    Pull ALL consecutive values after *flag* (stops at the next --flag).
    Supports comma-separated multi-word names, e.g.
      --compare mewtwo, iron valiant, brute bonnet
    Returns (values_list, remaining_tokens).
    """
    try:
        idx = tokens.index(flag)
    except ValueError:
        return [], tokens

    raw: list[str] = []
    i = idx + 1
    while i < len(tokens) and not tokens[i].startswith("--"):
        raw.append(tokens[i])
        i += 1
    remaining = tokens[:idx] + tokens[i:]
    if not raw:
        return [], remaining

    joined = " ".join(raw)
    values = [v.strip() for v in joined.split(",") if v.strip()]
    return values, remaining


def _extract_repeatable_flag_values(
    tokens: list[str], flag_set: frozenset[str]
) -> tuple[list[str], list[str]]:
    """
    Extract ALL occurrences of any flag in *flag_set*.
    Each occurrence contributes one name (possibly multi-word until next --flag).
    Returns (names_list, remaining_tokens).
    """
    names: list[str] = []
    out:   list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.lower() in flag_set:
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


def _build_ts_filter(
    use_alltime: bool,
    since_dt: datetime | None,
    before_dt: datetime | None,
) -> dict:
    """Build a MongoDB timestamp filter dict from the given flags."""
    merged: dict = {"$exists": True}
    if not use_alltime:
        year_ts = int(datetime(GRAPH_START_YEAR, 1, 1, tzinfo=timezone.utc).timestamp())
        merged["$gte"] = year_ts
    if since_dt:
        since_ts = int(since_dt.timestamp())
        merged["$gte"] = max(merged.get("$gte", 0), since_ts)
    if before_dt:
        merged["$lt"] = int(before_dt.timestamp())
    return merged


def _apply_ts_filter(
    records: list[dict],
    ts_filter: dict,
) -> list[dict]:
    """Filter a list of records in-memory using a MongoDB-style ts filter dict."""
    gte = ts_filter.get("$gte")
    lt  = ts_filter.get("$lt")
    return [
        r for r in records
        if (gte is None or r.get("ts", 0) >= gte)
        and (lt  is None or r.get("ts", 0) <  lt)
    ]


def _detect_outliers(
    prices: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Return (high_mask, low_mask) boolean arrays using 3×IQR + 20%-of-median fences.
    """
    q1, q3 = np.percentile(prices, 25), np.percentile(prices, 75)
    iqr    = q3 - q1
    upper  = q3 + 3.0 * iqr if iqr > 0 else prices.max()
    lower  = max(q1 - 3.0 * iqr, float(np.median(prices)) * 0.20)
    high   = prices > upper
    low    = (lower > 0) & (prices < lower)
    return high, low


# ─────────────────────────────────────────────────────────────────────────────
# X-AXIS FORMATTER (shared by build_graph and build_compare_graph)
# ─────────────────────────────────────────────────────────────────────────────

def _configure_xaxis(ax, span_days: int) -> None:
    """Set smart date locators/formatters depending on the visible time span."""
    if span_days <= 60:
        major_loc = mdates.WeekdayLocator(byweekday=mdates.MO)
        minor_loc = mdates.DayLocator()

        def _fmt(x, _pos=None):
            dt = mdates.num2date(x)
            return f"{dt.day} {dt.strftime('%b')}\n{dt.year}"

        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt))
    else:
        if span_days <= 365:
            major_loc = mdates.MonthLocator()
            minor_loc = mdates.WeekdayLocator(byweekday=mdates.MO)
        elif span_days <= 365 * 2:
            major_loc = mdates.MonthLocator(bymonth=range(1, 13, 2))
            minor_loc = mdates.MonthLocator()
        else:
            major_loc = mdates.MonthLocator(bymonth=[1, 4, 7, 10])
            minor_loc = mdates.MonthLocator()

        first_done = [False]

        def _fmt(x, _pos=None):  # noqa: F811
            dt = mdates.num2date(x)
            label = dt.strftime("%b")
            if dt.month == 1 or not first_done[0]:
                first_done[0] = True
                return f"{label}\n{dt.year}"
            return label

        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt))

    ax.xaxis.set_major_locator(major_loc)
    ax.xaxis.set_minor_locator(minor_loc)
    ax.tick_params(axis="x", which="major", length=5, colors=TEXT_COLOR, labelsize=8.5, pad=3)
    ax.tick_params(axis="x", which="minor", length=3, color=GRID_COLOR)
    plt.setp(ax.get_xticklabels(), rotation=0, ha="center")


def _draw_year_boundaries(ax, dates: list[datetime]) -> None:
    """Draw subtle dashed vertical lines at January 1 of each visible year."""
    if len(dates) < 2:
        return
    for yr in range(dates[0].year, dates[-1].year + 1):
        jan1 = datetime(yr, 1, 1, tzinfo=timezone.utc)
        if dates[0] < jan1 < dates[-1]:
            ax.axvline(jan1, color=MUTED_COLOR, linewidth=0.9, linestyle="--", alpha=0.40, zorder=2)


def _style_axes(ax) -> None:
    """Apply consistent dark-theme styling to a chart axes."""
    ax.tick_params(colors=TEXT_COLOR, labelsize=9)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_COLOR)
    ax.grid(color=GRID_COLOR, linestyle="-", linewidth=0.6, alpha=0.8)


# ─────────────────────────────────────────────────────────────────────────────
# CHART BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

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
    Build a single-series price history chart.

    Returns (image_buf, outliers, fetched_count, plotted_count).
    outliers is a list of (date, price, record, kind) tuples.
    """
    records = sorted(records, key=lambda r: r.get("ts", 0))
    fetched_count = len(records)

    # Capture true all-time min/max BEFORE subsampling
    all_prices_full = np.array([r["bid"] for r in records], dtype=float)
    at_min = float(all_prices_full.min()) if len(all_prices_full) else 0.0
    at_max = float(all_prices_full.max()) if len(all_prices_full) else 0.0

    if len(records) > MAX_POINTS:
        step    = len(records) // MAX_POINTS
        records = records[::step]

    plotted_count = len(records)
    dates  = [datetime.fromtimestamp(r["ts"], tz=timezone.utc) for r in records]
    prices = np.array([r["bid"] for r in records], dtype=float)

    # ── Outlier handling ──────────────────────────────────────────────────────
    if show_outliers:
        dates_plot, prices_plot = dates, prices
        outlier_records = outlier_dates = outlier_kinds = []
        outlier_prices  = np.array([])
        n_outliers      = 0
    else:
        high_mask, low_mask = _detect_outliers(prices)
        outlier_mask = high_mask | low_mask
        plot_mask    = ~outlier_mask

        outlier_dates   = [d for d, m in zip(dates,   outlier_mask) if m]
        outlier_prices  = prices[outlier_mask]
        outlier_records = [r for r, m in zip(records, outlier_mask) if m]
        outlier_kinds   = [
            "high" if h else "low"
            for h, m in zip(high_mask, outlier_mask) if m
        ]
        dates_plot  = [d for d, m in zip(dates, plot_mask) if m]
        prices_plot = prices[plot_mask]

        # Fall back to full dataset if too few clean points
        if len(prices_plot) < 3:
            dates_plot, prices_plot = dates, prices
            outlier_records = outlier_dates = outlier_kinds = []
            outlier_prices  = np.array([])

        n_outliers = int(outlier_mask.sum())

    # ── Stats ─────────────────────────────────────────────────────────────────
    total  = len(prices)
    p_min  = prices_plot.min()
    p_max  = prices_plot.max()
    p_avg  = prices_plot.mean()
    p_med  = float(np.median(prices_plot))
    p_std  = prices_plot.std()

    # Jitter flat price series so polyfit doesn't divide by zero
    if p_max == p_min:
        prices_plot = prices_plot + np.linspace(-0.5, 0.5, len(prices_plot))

    x_num            = np.arange(len(prices_plot), dtype=float)
    slope, intercept = np.polyfit(x_num, prices_plot, 1)

    variant     = _detect_variant(query)
    pal         = _PALETTE[variant]
    trend_color = pal["trend_up"] if slope > 0 else pal["trend_down"]
    trend_arrow = "▲" if slope > 0 else "▼"

    window   = max(5, len(prices_plot) // 10)
    roll_avg = _rolling_average(prices_plot, window)
    trend_ln = slope * x_num + intercept

    do_band = len(prices_plot) >= 20
    if do_band:
        p25, p75 = _percentile_band(prices_plot, window=30)

    # ── Figure setup ──────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(12, 7.8), facecolor=BG_DARK)
    gs  = fig.add_gridspec(2, 1, height_ratios=[5.5, 1.2], hspace=0.18)
    ax  = fig.add_subplot(gs[0])
    axs = fig.add_subplot(gs[1])
    ax.set_facecolor(BG_CARD)
    axs.set_facecolor(BG_DARK)
    axs.axis("off")

    # ── Plot data ─────────────────────────────────────────────────────────────
    if do_band:
        ax.fill_between(dates_plot, p25, p75, color=pal["fill"], linewidth=0, label="25–75th pct")

    ax.scatter(
        dates_plot, prices_plot,
        color=pal["dot"], s=22, alpha=0.65, zorder=3,
        linewidths=0, edgecolors="none", label="Sales",
    )

    # Outlier hint markers at chart edges
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
    ax.plot(dates_plot, trend_ln, color=trend_color, linewidth=1.4,
            linestyle="--", alpha=0.85, label="Trend", zorder=4)

    # ── Min/Max annotations ───────────────────────────────────────────────────
    y_lo, y_hi = prices_plot.min(), prices_plot.max()
    y_span     = y_hi - y_lo or 1

    def _annotate(idx: int, label: str, color: str, prefer_above: bool) -> None:
        val = prices_plot[idx]
        rel = (val - y_lo) / y_span
        if rel > 0.70:
            yoff, va = -32, "top"
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

    _annotate(int(np.argmax(prices_plot)),
              f"Chart Max\n{_format_price(prices_plot.max())}",
              pal["trend_up"],   prefer_above=True)
    _annotate(int(np.argmin(prices_plot)),
              f"Chart Min\n{_format_price(prices_plot.min())}",
              pal["trend_down"], prefer_above=False)

    # ── X-axis ────────────────────────────────────────────────────────────────
    span_days = (dates_plot[-1] - dates_plot[0]).days if len(dates_plot) > 1 else 1
    _configure_xaxis(ax, span_days)
    _draw_year_boundaries(ax, dates_plot)
    _style_axes(ax)
    ax.set_xlim(dates_plot[0], dates_plot[-1])

    # ── Y-axis ────────────────────────────────────────────────────────────────
    y_range = p_max - p_min or p_max or 1
    if show_outliers and p_max > 0 and p_min > 0 and p_max / max(p_min, 1) > 20:
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
        )
        ax.set_ylim(p_min * 0.85, p_max * 1.15)
    else:
        ax.set_yticks(_smart_yticks(p_min, p_max))
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
        )
        ax.set_ylim(max(0, p_min - y_range * 0.18), p_max + y_range * 0.25)

    ax.set_ylabel("Winning Bid (pc)", color=TEXT_COLOR, fontsize=10)
    ax.yaxis.label.set_color(TEXT_COLOR)

    # ── Title ─────────────────────────────────────────────────────────────────
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

    tag        = pal["tag"]
    full_title = f"[{tag}] {name}".strip() if tag else name
    date_first = dates[0].strftime("%-d %b %Y")
    date_last  = dates[-1].strftime("%-d %b %Y")
    span_d     = (dates[-1] - dates[0]).days
    alltime_n  = "  •  All-time" if alltime else ""
    raw_n      = "  •  Raw (all data)" if show_outliers else ""
    ax.set_title(
        f"{full_title}  •  Price History{alltime_n}{raw_n}  •  {date_first} → {date_last} ({span_d}d)",
        color=TEXT_COLOR, fontsize=14, fontweight="bold", pad=10,
    )

    # ── Legend (moved into stats panel) ──────────────────────────────────────
    handles, labels = ax.get_legend_handles_labels()
    if ax.get_legend():
        ax.get_legend().remove()
    axs.legend(
        handles, labels,
        loc="center left", bbox_to_anchor=(0.0, 0.5),
        facecolor=BG_CARD, edgecolor=GRID_COLOR,
        labelcolor=TEXT_COLOR, fontsize=7.5,
        borderpad=0.6, handlelength=1.5, handletextpad=0.5,
        framealpha=1.0, borderaxespad=0.0,
    )

    # ── Stats panel ───────────────────────────────────────────────────────────
    LEG_FRAC = 0.27
    S0, S1   = LEG_FRAC + 0.015, 1.0
    col_w    = (S1 - S0) / 7

    paired_cols = [
        ("Chart Min",    _format_price(p_min),  "Chart Max",    _format_price(p_max)),
        ("All-time Min", _format_price(at_min),  "All-time Max", _format_price(at_max)),
        ("Avg",          _format_price(p_avg),  "Median",       _format_price(p_med)),
        ("Auctions",     f"{total:,}",          None,           None),
        ("Std Dev",      _format_price(p_std),  None,           None),
        ("Trend",        f"{trend_arrow} {_format_price(abs(slope))}/sale", None, None),
        ("Outliers",     "All Included" if show_outliers else (f"{n_outliers} hidden" if n_outliers else "None"), None, None),
    ]

    P_TOP_LBL, P_TOP_VAL = 0.80, 0.58
    P_BOT_LBL, P_BOT_VAL = 0.38, 0.12
    S_LBL,     S_VAL     = 0.72, 0.28

    for ci, (tl, tv, bl, bv) in enumerate(paired_cols):
        cx     = S0 + ci * col_w + col_w * 0.5
        paired = bl is not None
        if paired:
            axs.text(cx, P_TOP_LBL, tl, ha="center", va="center",
                     color=MUTED_COLOR, fontsize=7, transform=axs.transAxes)
            axs.text(cx, P_TOP_VAL, tv, ha="center", va="center",
                     color=TEXT_COLOR, fontsize=9, fontweight="bold", transform=axs.transAxes)
            xmin_f = (S0 + ci * col_w)
            xmax_f = (S0 + (ci + 1) * col_w)
            axs.axhline(0.48, xmin=xmin_f, xmax=xmax_f,
                        color=GRID_COLOR, linewidth=0.5, alpha=0.5)
            axs.text(cx, P_BOT_LBL, bl, ha="center", va="center",
                     color=MUTED_COLOR, fontsize=7, transform=axs.transAxes)
            axs.text(cx, P_BOT_VAL, bv, ha="center", va="center",
                     color=TEXT_COLOR, fontsize=9, fontweight="bold", transform=axs.transAxes)
        else:
            axs.text(cx, S_LBL, tl, ha="center", va="center",
                     color=MUTED_COLOR, fontsize=7, transform=axs.transAxes)
            axs.text(cx, S_VAL, tv, ha="center", va="center",
                     color=TEXT_COLOR, fontsize=9, fontweight="bold", transform=axs.transAxes)

    fig.add_artist(matplotlib.lines.Line2D(
        [0.03, 0.97], [0.16, 0.16],
        transform=fig.transFigure,
        color=GRID_COLOR, linewidth=0.8,
    ))
    fig.subplots_adjust(top=0.91, left=0.09, right=0.97, bottom=0.02)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)

    outlier_list = list(zip(outlier_dates, outlier_prices.tolist(), outlier_records, outlier_kinds))
    return buf, outlier_list, fetched_count, plotted_count


def build_compare_graph(
    series: list[dict],
    query_str: str,
    *,
    alltime: bool = False,
    show_outliers: bool = False,
    since_dt: datetime | None = None,
    before_dt: datetime | None = None,
) -> io.BytesIO:
    """
    Overlay multiple Pokémon price histories on one chart.
    Each series dict: {"name": str, "records": list[dict], "variant": str}
    """
    fig = plt.figure(figsize=(13, 7.5), facecolor=BG_DARK)
    gs  = fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.10)
    ax  = fig.add_subplot(gs[0])
    axs = fig.add_subplot(gs[1])
    ax.set_facecolor(BG_CARD)
    axs.set_facecolor(BG_DARK)
    axs.axis("off")

    all_dates:  list[datetime] = []
    all_prices: list[float]    = []
    stats_rows: list[dict]     = []

    for slot, s in enumerate(series):
        dot_c, line_c, _ = _OVERLAY_PALETTE[slot % len(_OVERLAY_PALETTE)]
        records = sorted(s["records"], key=lambda r: r.get("ts", 0))

        if len(records) > MAX_POINTS:
            step    = len(records) // MAX_POINTS
            records = records[::step]

        dates  = [datetime.fromtimestamp(r["ts"], tz=timezone.utc) for r in records]
        prices = np.array([r["bid"] for r in records], dtype=float)

        if not show_outliers and len(prices) >= 2:
            high_mask, low_mask = _detect_outliers(prices)
            clean = ~(high_mask | low_mask)
            dates  = [d for d, m in zip(dates,  clean) if m]
            prices = prices[clean]

        if len(prices) < 2:
            continue

        all_dates.extend(dates)
        all_prices.extend(prices.tolist())

        ax.scatter(dates, prices, color=dot_c, s=16, alpha=0.45, zorder=3,
                   linewidths=0, edgecolors="none")

        window   = max(5, len(prices) // 10)
        roll_avg = _rolling_average(prices, window)
        ax.plot(dates, roll_avg, color=line_c, linewidth=2.2,
                label=s["name"], zorder=4, solid_capstyle="round")

        x_num            = np.arange(len(prices), dtype=float)
        slope, intercept = np.polyfit(x_num, prices, 1)
        ax.plot(dates, slope * x_num + intercept, color=line_c,
                linewidth=1.0, linestyle="--", alpha=0.55, zorder=4)

        arrow = "▲" if slope > 0 else "▼"
        stats_rows.append({
            "name": s["name"], "color": line_c,
            "count": len(prices),
            "min":   prices.min(),   "max":    prices.max(),
            "avg":   prices.mean(),  "median": float(np.median(prices)),
            "trend": f"{arrow} {_format_price(abs(slope))}/sale",
        })

    # ── Axes styling ──────────────────────────────────────────────────────────
    span_days = (max(all_dates) - min(all_dates)).days if len(all_dates) > 1 else 365
    _configure_xaxis(ax, span_days)
    if all_dates:
        _draw_year_boundaries(ax, sorted(all_dates))
    _style_axes(ax)

    if all_prices:
        gmin, gmax = min(all_prices), max(all_prices)
        if show_outliers and gmax / max(gmin, 1) > 20:
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
            )
            ax.set_ylim(gmin * 0.85, gmax * 1.15)
        else:
            ax.set_yticks(_smart_yticks(gmin, gmax))
            ax.yaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: _format_price(v))
            )
            g_range = gmax - gmin or gmax or 1
            ax.set_ylim(max(0, gmin - g_range * 0.12), gmax + g_range * 0.22)

    if all_dates:
        ax.set_xlim(min(all_dates), max(all_dates))

    ax.set_ylabel("Winning Bid (pc)", color=TEXT_COLOR, fontsize=10)
    ax.yaxis.label.set_color(TEXT_COLOR)

    # ── Title ─────────────────────────────────────────────────────────────────
    names_str   = " vs ".join(s["name"] for s in series)
    at_note     = "  •  All-time" if alltime else ""
    raw_note    = "  •  Raw data" if show_outliers else ""
    since_note  = f"  •  since {since_dt.strftime('%b %Y')}" if since_dt else ""
    before_note = f"  •  before {before_dt.strftime('%b %Y')}" if before_dt else ""
    ax.set_title(
        f"{names_str}  •  Price Comparison{at_note}{raw_note}{since_note}{before_note}",
        color=TEXT_COLOR, fontsize=13, fontweight="bold", pad=10,
    )

    ax.legend(facecolor=BG_DARK, edgecolor=GRID_COLOR, labelcolor=TEXT_COLOR,
              fontsize=9, loc="upper left", borderpad=0.6, handlelength=1.8)

    # ── Per-series stats bar ──────────────────────────────────────────────────
    cols    = ["Pokémon", "Sales", "Chart Min", "Chart Max", "Avg", "Median", "Trend"]
    col_xs  = [0.04, 0.18, 0.30, 0.42, 0.54, 0.66, 0.82]

    for cx, label in zip(col_xs, cols):
        axs.text(cx, 0.82, label, ha="left", va="center",
                 color=MUTED_COLOR, fontsize=7, transform=axs.transAxes, fontweight="bold")

    row_h = 0.55 / max(len(stats_rows), 1)
    for ri, row in enumerate(stats_rows):
        y = 0.35 - ri * row_h
        vals = [
            row["name"], f"{row['count']:,}",
            _format_price(row["min"]), _format_price(row["max"]),
            _format_price(row["avg"]), _format_price(row["median"]),
            row["trend"],
        ]
        for cx, val in zip(col_xs, vals):
            color = row["color"] if cx == col_xs[0] else TEXT_COLOR
            axs.text(cx, y, val, ha="left", va="center",
                     color=color, fontsize=8, fontweight="bold", transform=axs.transAxes)

    fig.add_artist(matplotlib.lines.Line2D(
        [0.03, 0.97], [0.16, 0.16],
        transform=fig.transFigure, color=GRID_COLOR, linewidth=0.8,
    ))
    fig.subplots_adjust(top=0.91, left=0.09, right=0.97, bottom=0.02)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG_DARK, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


def build_outlier_image(
    outliers: list[tuple],
    pokemon_name: str,
    variant: str,
) -> list[io.BytesIO]:
    """
    Build paginated table image(s) for outlier sales.
    Each entry: (date, price, record, kind).
    Returns a list of BytesIO, one per page of up to OUTLIER_PAGE_SIZE rows.
    """
    headers    = ["#", "Type", "Auction ID", "Date", "Level", "IV %", "Winning Bid"]
    col_widths = [0.04, 0.08, 0.16, 0.20, 0.09, 0.12, 0.20]

    all_rows = []
    for i, entry in enumerate(outliers):
        d, p, r, kind = entry if len(entry) == 4 else (*entry, "high")
        aid        = str(r.get("aid", "?"))
        date_s     = d.strftime("%-d %b %Y")
        level      = str(r.get("lv", "???"))
        iv         = r.get("iv")
        iv_s       = f"{iv:.2f}%" if iv is not None else "???"
        kind_label = "▲ High" if kind == "high" else "▼ Low"
        all_rows.append([str(i + 1), kind_label, aid, date_s, level, iv_s, _format_price(p)])

    chunks = [all_rows[i:i + OUTLIER_PAGE_SIZE]
              for i in range(0, len(all_rows), OUTLIER_PAGE_SIZE)]

    bufs: list[io.BytesIO] = []
    for rows in chunks:
        n        = len(rows)
        row_h_in = 0.38
        head_h   = 0.50
        fig_h    = head_h + n * row_h_in

        fig, ax = plt.subplots(figsize=(11, fig_h), facecolor=BG_DARK)
        ax.set_facecolor(BG_DARK)
        ax.axis("off")

        tbl = ax.table(
            cellText=rows, colLabels=headers,
            colWidths=col_widths, loc="center", cellLoc="center",
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
                    cell.get_text().set_color("#ef476f" if "High" in kind_val else "#ffd166")
                    cell.get_text().set_fontweight("bold")
                elif col == 1:
                    cell.get_text().set_color("#ef476f" if "High" in kind_val else "#ffd166")
                    cell.get_text().set_fontweight("bold")
                elif col == 5:
                    cell.get_text().set_color("#ffd166")
                elif col in (0, 2):
                    cell.get_text().set_color(MUTED_COLOR)
                else:
                    cell.get_text().set_color(TEXT_COLOR)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=BG_DARK, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        bufs.append(buf)

    return bufs


# ─────────────────────────────────────────────────────────────────────────────
# DISCORD UI HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


def _make_graph_view(
    components: list,
    btn_rows: list[discord.ui.ActionRow],
    accent: int,
    timeout: int = 300,
) -> discord.ui.LayoutView:
    """Assemble a LayoutView from content components + button rows."""
    class GView(discord.ui.LayoutView):
        container = discord.ui.Container(*components, *btn_rows, accent_colour=accent)
        def __init__(self): super().__init__(timeout=timeout)
    return GView()


def _make_graph_components(
    heading: str,
    sub: str,
    protip: str,
) -> list:
    return [
        discord.ui.TextDisplay(content=heading),
        discord.ui.TextDisplay(content=sub),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://graph.png")),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=protip),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
    ]


@dataclass
class _RegenState:
    """Immutable snapshot of everything needed to redraw a graph on button press."""
    records:       list
    query:         dict
    display_str:   str
    limit:         int | None
    since_dt:      datetime | None
    before_dt:     datetime | None
    pokemon_name:  str
    variant:       str
    accent:        int
    heading:       str
    legend_text:   str
    filters_body:  str
    protip_text:   str
    # multi-name only
    found_names:   list | None = None
    variant_flags: list | None = None


# ─── Button classes (module-level so they're not re-created per request) ──────

class _HowToReadBtn(discord.ui.Button):
    def __init__(self, legend_text: str):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="📖 How to Read This Graph",
                         custom_id="g_legend")
        self._text = legend_text

    async def callback(self, interaction: discord.Interaction):
        class LV(discord.ui.LayoutView):
            c = discord.ui.Container(
                discord.ui.TextDisplay(content=self._text),
                accent_colour=config.EMBED_COLOR,
            )
        await interaction.response.send_message(view=LV(), ephemeral=True)


class _FiltersBtn(discord.ui.Button):
    def __init__(self, filters_text: str):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="🔍 Available Filters",
                         custom_id="g_filters")
        self._text = filters_text

    async def callback(self, interaction: discord.Interaction):
        class FV(discord.ui.LayoutView):
            c = discord.ui.Container(
                discord.ui.TextDisplay(content=self._text),
                accent_colour=config.EMBED_COLOR,
            )
        await interaction.response.send_message(view=FV(), ephemeral=True)


class _AlltimeBtn(discord.ui.Button):
    def __init__(self, is_alltime: bool, is_outliers: bool, regen_fn):
        if is_alltime:
            style, label = discord.ButtonStyle.success, f"📅 Since {GRAPH_START_YEAR} Only"
        else:
            style, label = discord.ButtonStyle.secondary, "🕐 Show All-time Data"
        super().__init__(style=style, label=label, custom_id="g_alltime")
        self._ia, self._io, self._fn = is_alltime, is_outliers, regen_fn

    async def callback(self, interaction: discord.Interaction):
        await self._fn(interaction, not self._ia, self._io)


class _OutliersToggleBtn(discord.ui.Button):
    def __init__(self, is_alltime: bool, is_outliers: bool, regen_fn):
        if is_outliers:
            style, label = discord.ButtonStyle.danger, "📊 Hide Outliers (Clean View)"
        else:
            style, label = discord.ButtonStyle.secondary, "⚠️ Include Outliers too"
        super().__init__(style=style, label=label, custom_id="g_outliers_toggle")
        self._ia, self._io, self._fn = is_alltime, is_outliers, regen_fn

    async def callback(self, interaction: discord.Interaction):
        await self._fn(interaction, self._ia, not self._io)


class _OutlierDetailBtn(discord.ui.Button):
    def __init__(self, label: str, ob_pages, count: int):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label=label, custom_id="g_outlier_detail")
        if ob_pages is None:
            self._pages = []
        elif isinstance(ob_pages, list):
            self._pages = [b.getvalue() for b in ob_pages]
        else:
            self._pages = [ob_pages.getvalue()]
        self._count = count

    async def callback(self, interaction: discord.Interaction):
        if not self._pages:
            await interaction.response.send_message("❌ Outlier image unavailable.", ephemeral=True)
            return
        n_pages = len(self._pages)
        files = [
            discord.File(
                io.BytesIO(page),
                filename=f"outliers_p{i + 1}.png" if n_pages > 1 else "outliers.png",
            )
            for i, page in enumerate(self._pages)
        ]
        page_note = f"  •  {n_pages} images" if n_pages > 1 else ""
        class OV(discord.ui.LayoutView):
            c = discord.ui.Container(
                discord.ui.TextDisplay(content=(
                    f"📋 **{self._count} sale(s) excluded from the graph{page_note}**\n"
                    f"_▲ Overpriced outliers inflate the average; ▼ sniped/underpriced sales compress the Y-axis._"
                )),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                *[discord.ui.MediaGallery(discord.MediaGalleryItem(
                    media=f"attachment://{f.filename}"
                )) for f in files],
                accent_colour=discord.Colour(0xef476f),
            )
        await interaction.response.send_message(view=OV(), files=files, ephemeral=True)


class _CompareBtn(discord.ui.Button):
    def __init__(self, col):
        super().__init__(style=discord.ButtonStyle.primary,
                         label="📊 Compare", custom_id="g_compare_modal")
        self._col = col

    async def callback(self, interaction: discord.Interaction):
        modal = _build_compare_modal(self._col)
        await interaction.response.send_modal(modal())


def _build_btn_list(
    legend_text: str,
    filters_text: str,
    has_outliers: bool,
    outlier_count: int,
    outlier_bytes,
    outlier_data: list,
    is_alltime: bool,
    is_outliers: bool,
    regenerate_fn,
    col=None,
) -> list[discord.ui.ActionRow]:
    """
    Build 1–2 ActionRows of buttons for a graph message.
    Row 1: HowToRead, Filters, Alltime, OutliersToggle (always present).
    Row 2: OutlierDetail, Compare (only if applicable).
    """
    row1 = [
        _HowToReadBtn(legend_text),
        _FiltersBtn(filters_text),
        _AlltimeBtn(is_alltime, is_outliers, regenerate_fn),
        _OutliersToggleBtn(is_alltime, is_outliers, regenerate_fn),
    ]
    row2 = []

    if col is not None:
        row2.append(_CompareBtn(col))

    if has_outliers and not is_outliers:
        n_data  = outlier_data
        n_high  = sum(1 for e in n_data if (e[3] if len(e) == 4 else "high") == "high")
        n_low   = outlier_count - n_high
        parts   = []
        if n_high: parts.append(f"▲{n_high} overpriced")
        if n_low:  parts.append(f"▼{n_low} sniped")
        detail_label = f"📋 View {outlier_count} Excluded Sale(s) ({', '.join(parts)})"
        row2.append(_OutlierDetailBtn(detail_label, outlier_bytes, outlier_count))

    rows = [discord.ui.ActionRow(*row1)]
    if row2:
        rows.append(discord.ui.ActionRow(*row2))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# COMPARE MODAL
# ─────────────────────────────────────────────────────────────────────────────

def _build_compare_modal(col) -> type[discord.ui.Modal]:
    """
    Factory returning a CompareModal class wired to the given MongoDB collection.
    TextInput fields must be class-level attributes so discord.py's metaclass sees them.
    """
    _col_ref = col

    class _CAlltimeBtn(discord.ui.Button):
        def __init__(self, ia, io, send_fn, the_series, errors):
            if ia:
                style, label = discord.ButtonStyle.success, f"📅 Since {GRAPH_START_YEAR} Only"
            else:
                style, label = discord.ButtonStyle.secondary, "🕐 Show All-time Data"
            super().__init__(style=style, label=label, custom_id="cmp_alltime")
            self._ia, self._io = ia, io
            self._fn, self._series, self._errors = send_fn, the_series, errors

        async def callback(self, intr: discord.Interaction):
            await intr.response.defer()
            await self._fn(intr, self._series, not self._ia, self._io, self._errors, edit=True)

    class _COutliersBtn(discord.ui.Button):
        def __init__(self, ia, io, send_fn, the_series, errors):
            if io:
                style, label = discord.ButtonStyle.danger, "📊 Hide Outliers (Clean View)"
            else:
                style, label = discord.ButtonStyle.secondary, "⚠️ Include Outliers too"
            super().__init__(style=style, label=label, custom_id="cmp_outliers")
            self._ia, self._io = ia, io
            self._fn, self._series, self._errors = send_fn, the_series, errors

        async def callback(self, intr: discord.Interaction):
            await intr.response.defer()
            await self._fn(intr, self._series, self._ia, not self._io, self._errors, edit=True)

    class _COutlierDetailBtn(discord.ui.Button):
        def __init__(self, series_outliers):
            n = sum(c for _, _, c in series_outliers)
            super().__init__(style=discord.ButtonStyle.secondary,
                             label=f"📋 View {n} Excluded Sale(s)",
                             custom_id="cmp_outlier_detail")
            self._so = series_outliers

        async def callback(self, intr: discord.Interaction):
            lines, files = [], []
            for name, ob_pages, count in self._so:
                if ob_pages and count > 0:
                    n_pages = len(ob_pages)
                    for pi, page_bytes in enumerate(ob_pages):
                        page_label = f" (part {pi + 1}/{n_pages})" if n_pages > 1 else ""
                        lines.append(f"**{name}** — {count} excluded{page_label}" if pi == 0 else f"**{name}**{page_label}")
                        safe = name.replace(" ", "_")
                        fname = f"outliers_{safe}_p{pi + 1}.png" if n_pages > 1 else f"outliers_{safe}.png"
                        files.append(discord.File(io.BytesIO(page_bytes), filename=fname))
            if not files:
                await intr.response.send_message("❌ No outlier data.", ephemeral=True)
                return
            files = files[:10]
            content = "📋 **Excluded sales per series:**\n" + "\n".join(lines)
            class _OV(discord.ui.LayoutView):
                container = discord.ui.Container(
                    discord.ui.TextDisplay(content=content),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    *[discord.ui.MediaGallery(discord.MediaGalleryItem(
                        media=f"attachment://{f.filename}"
                    )) for f in files],
                    accent_colour=discord.Colour(0xef476f),
                )
            await intr.response.send_message(view=_OV(), files=files, ephemeral=True)

    class CompareModal(discord.ui.Modal, title="📊 Compare Pokémon Prices"):
        slot_0 = discord.ui.TextInput(label="Slot 1 (required)", placeholder="--n meowth --sh",
                                       required=True, max_length=200, style=discord.TextStyle.short)
        slot_1 = discord.ui.TextInput(label="Slot 2 (optional)", placeholder="e.g. --n eevee --sh",
                                       required=False, max_length=200, style=discord.TextStyle.short)
        slot_2 = discord.ui.TextInput(label="Slot 3 (optional)", placeholder="e.g. --n pikachu --sh",
                                       required=False, max_length=200, style=discord.TextStyle.short)
        slot_3 = discord.ui.TextInput(label="Slot 4 (optional)", placeholder="e.g. --n garchomp --sh",
                                       required=False, max_length=200, style=discord.TextStyle.short)
        slot_4 = discord.ui.TextInput(label="Slot 5 (optional)", placeholder="e.g. --n rayquaza --sh",
                                       required=False, max_length=200, style=discord.TextStyle.short)

        async def on_submit(self, interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=False)

            raw_slots = [
                getattr(self, f"slot_{i}").value.strip()
                for i in range(COMPARE_MODAL_SLOTS)
            ]
            raw_slots = [s for s in raw_slots if s]

            if len(raw_slots) < 2:
                await interaction.followup.send(
                    "❌ Please fill in at least **2 slots** to compare.", ephemeral=True)
                return

            raw_slots = raw_slots[:len(_OVERLAY_PALETTE)]
            series: list[dict] = []
            errors: list[str]  = []

            for slot_str in raw_slots:
                tokens = slot_str.split()
                tokens = [t for t in tokens if t not in _GRAPH_ONLY_FLAGS]

                _since_str, tokens  = _extract_flag_value(tokens, FLAG_SINCE)
                _before_str, tokens = _extract_flag_value(tokens, FLAG_BEFORE)
                _since_dt  = _parse_date_flag(_since_str)  if _since_str  else None
                _before_dt = _parse_date_flag(_before_str) if _before_str else None

                _mnames, tokens = _extract_repeatable_flag_values(tokens, _NAME_FLAGS)
                if _mnames:
                    tokens = ["--name", _mnames[0]] + tokens

                try:
                    query, _, limit = build_query(tokens, expand_name_by_dex=True)
                except Exception as exc:
                    errors.append(f"❌ `{slot_str}` — parse error: {exc}")
                    continue

                ts_slot: dict = {"$exists": True}
                if _since_dt:
                    ts_slot["$gte"] = int(_since_dt.timestamp())
                if _before_dt:
                    ts_slot["$lt"] = int(_before_dt.timestamp())

                try:
                    loop = asyncio.get_running_loop()
                    recs, _capped = await loop.run_in_executor(
                        None,
                        lambda q=query, lim=limit, tsf=ts_slot: (
                            lambda raw: (
                                sorted(raw[:lim or MAX_FETCH], key=lambda r: r.get("ts", 0)),
                                len(raw) > (lim or MAX_FETCH),
                            )
                        )(list(_col_ref.find(
                            {**q, "ts": tsf, "bid": {"$exists": True}},
                            {"ts": 1, "bid": 1, "pn": 1, "sh": 1, "gx": 1, "iv": 1, "aid": 1, "lv": 1},
                        ).sort("ts", -1).limit((lim or MAX_FETCH) + 1))),
                    )
                except Exception as exc:
                    errors.append(f"❌ `{slot_str}` — fetch error: {exc}")
                    continue

                if not recs:
                    errors.append(f"⚠️ `{slot_str}` — no auctions found, skipped.")
                    continue

                variant = _detect_variant(query)
                tag     = _DISCORD_TAG.get(variant, "")
                label   = (_mnames[0].title() if _mnames
                           else Counter(r.get("pn", "") for r in recs if r.get("pn")).most_common(1)[0][0]
                           if recs else slot_str[:30])
                if tag:
                    label = f"{tag} {label}"

                series.append({
                    "name": label, "records": recs, "variant": variant,
                    "since_dt": _since_dt, "before_dt": _before_dt,
                })

            if len(series) < 2:
                msg = "❌ Need at least **2 slots with data** to build a comparison graph."
                if errors:
                    msg += "\n" + "\n".join(errors)
                await interaction.followup.send(msg, ephemeral=True)
                return

            # ── Helper: send/edit the compare result message ──────────────────
            async def _send_compare_result(
                target: discord.Interaction | None,
                the_series: list,
                is_alltime: bool,
                show_outliers: bool,
                extra_errors: list,
                *,
                edit: bool = False,
            ):
                cutoff_ts = int(datetime(GRAPH_START_YEAR, 1, 1, tzinfo=timezone.utc).timestamp())
                filtered: list[dict] = []
                for s in the_series:
                    gte = None if is_alltime else cutoff_ts
                    if s.get("since_dt"):
                        s_ts = int(s["since_dt"].timestamp())
                        gte  = max(gte, s_ts) if gte is not None else s_ts
                    lt = int(s["before_dt"].timestamp()) if s.get("before_dt") else None
                    recs = [
                        r for r in s["records"]
                        if (gte is None or r.get("ts", 0) >= gte)
                        and (lt  is None or r.get("ts", 0) <  lt)
                    ]
                    filtered.append({**s, "records": recs})

                try:
                    _check_memory()
                    cbuf = build_compare_graph(filtered, "", alltime=is_alltime, show_outliers=show_outliers)
                except (_LowMemoryError, MemoryError):
                    _free_memory()
                    msg = "❌ Can't plot — low memory. Try `--limit` to reduce data."
                    await (target or interaction).followup.send(msg, ephemeral=True)
                    return
                except Exception as exc:
                    log.exception("build_compare_graph failed: %s", exc)
                    await (target or interaction).followup.send(
                        f"❌ Failed to generate comparison graph: `{exc}`", ephemeral=True)
                    return

                # Collect per-series outliers for the detail button
                series_outliers: list[tuple] = []
                if not show_outliers:
                    for s in filtered:
                        recs = sorted(s["records"], key=lambda r: r.get("ts", 0))
                        if len(recs) > MAX_POINTS:
                            recs = recs[::len(recs) // MAX_POINTS]
                        prices = np.array([r["bid"] for r in recs], dtype=float)
                        if len(prices) < 2:
                            series_outliers.append((s["name"], None, 0))
                            continue
                        hi_m, lo_m = _detect_outliers(prices)
                        omask = hi_m | lo_m
                        orecs = [r for r, m in zip(recs, omask) if m]
                        okinds = ["high" if prices[i] > np.percentile(prices, 75) + 3.0 * (np.percentile(prices, 75) - np.percentile(prices, 25)) else "low"
                                  for i, m in enumerate(omask) if m]
                        if orecs:
                            odates  = [datetime.fromtimestamp(r.get("ts", 0), tz=timezone.utc) for r in orecs]
                            oprices = prices[omask]
                            odata   = list(zip(odates, oprices.tolist(), orecs, okinds))
                            obufs   = build_outlier_image(odata, s["name"], s.get("variant", "normal"))
                            series_outliers.append((s["name"], [b.getvalue() for b in obufs], len(orecs)))
                        else:
                            series_outliers.append((s["name"], None, 0))
                else:
                    series_outliers = [(s["name"], None, 0) for s in filtered]

                total_outliers = sum(n for _, _, n in series_outliers)
                has_outliers   = total_outliers > 0

                at_badge  = "  •  🕐 All-time" if is_alltime else ""
                out_badge = "  •  ⚠️ Raw data" if show_outliers else ""
                heading   = f"## {' vs '.join(s['name'] for s in filtered)} — Price Comparison"
                sub_parts = [f"_{len(filtered)} series{at_badge}{out_badge}  •  via Compare button_"]
                if extra_errors:
                    sub_parts.append("\n".join(extra_errors))
                sub = "\n".join(sub_parts)

                row1_btns = [
                    _CAlltimeBtn(is_alltime, show_outliers, _send_compare_result, the_series, extra_errors),
                    _COutliersBtn(is_alltime, show_outliers, _send_compare_result, the_series, extra_errors),
                ]
                row2_btns = []
                if has_outliers and not show_outliers:
                    row2_btns.append(_COutlierDetailBtn(series_outliers))

                cfile = discord.File(cbuf, filename="graph.png")
                comps = [
                    discord.ui.TextDisplay(content=heading),
                    discord.ui.TextDisplay(content=sub),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://graph.png")),
                    discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                    discord.ui.ActionRow(*row1_btns),
                ]
                if row2_btns:
                    comps.append(discord.ui.ActionRow(*row2_btns))

                class _CV(discord.ui.LayoutView):
                    container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
                    def __init__(self): super().__init__(timeout=300)

                if edit and target is not None:
                    await target.edit_original_response(attachments=[cfile], view=_CV())
                else:
                    await interaction.followup.send(view=_CV(), file=cfile, ephemeral=False)

            await _send_compare_result(None, series, False, False, errors)

    return CompareModal


# ─────────────────────────────────────────────────────────────────────────────
# PROJECTION & DB CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

_PROJECTION = {"ts": 1, "bid": 1, "pn": 1, "sh": 1, "gx": 1, "iv": 1, "aid": 1, "lv": 1}


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class Graph(commands.Cog):
    """Price history graphs for Pokémon auctions."""

    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self._mongo = MongoClient(config.MONGO_URI)
        self._col   = self._mongo[config.MONGO_DB_NAME][config.MONGO_COLLECTION]

    def cog_unload(self):
        self._mongo.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _fetch_sync(
        self,
        query: dict,
        ts_filter: dict,
        limit: int | None = None,
    ) -> tuple[list[dict], bool]:
        """Blocking MongoDB fetch. Always call via _fetch() to avoid blocking the event loop."""
        _check_memory()
        fetch_n = min(limit, MAX_FETCH) if limit is not None else MAX_FETCH
        cur     = self._col.find(
            {**query, "ts": ts_filter, "bid": {"$exists": True}},
            _PROJECTION,
        ).sort("ts", -1).limit(fetch_n + 1)
        recs   = list(cur)
        capped = len(recs) > fetch_n
        if capped:
            recs = recs[:fetch_n]
        recs.sort(key=lambda r: r.get("ts", 0))
        return recs, capped

    async def _fetch(
        self,
        query: dict,
        ts_filter: dict,
        limit: int | None = None,
    ) -> tuple[list[dict], bool]:
        """Async wrapper: offloads the blocking MongoDB call to a thread-pool executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._fetch_sync, query, ts_filter, limit),
        )

    @staticmethod
    def _make_sub(
        fetched: int,
        plotted: int,
        capped: bool,
        display_str: str,
        *,
        alltime: bool = False,
        since_dt: datetime | None = None,
        before_dt: datetime | None = None,
        outliers: bool = False,
        extra: str = "",
    ) -> str:
        cap_note     = " db has more" if capped else ""
        at_badge     = "  •  🕐 All-time" if alltime else ""
        since_badge  = f"  •  📅 Since {since_dt.strftime('%b %Y')}" if since_dt else ""
        before_badge = f"  •  📅 Before {before_dt.strftime('%b %Y')}" if before_dt else ""
        out_badge    = "  •  ⚠️ Raw data (all outliers included)" if outliers else ""
        return (
            f"_{fetched:,} sampled from DB{cap_note}  •  {plotted:,} dots on graph"
            f"{extra}{at_badge}{since_badge}{before_badge}{out_badge}"
            f"  •  filters: `{display_str}`_"
        )

    @staticmethod
    def _ref(ctx: commands.Context):
        return ctx.message if not (hasattr(ctx, "interaction") and ctx.interaction) else None

    async def _send_error(self, ctx: commands.Context, text: str) -> None:
        await ctx.send(view=_error_view(text), reference=self._ref(ctx), mention_author=False)

    # ── Command ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="graph", aliases=["g", "chart"])
    @app_commands.describe(
        filters="Same filters as auction search e.g: --sh --iv >80, or --n pikachu --n meowth --sh"
    )
    async def graph_command(self, ctx: commands.Context, *, filters: str = ""):
        """
        Show a price history graph for Pokémon auctions.

        No name needed — defaults to ALL Pokémon matching your filters.

        Examples:
          a!g --sh                               → shiny graph for ALL Pokémon
          a!g --n pikachu --sh                  → shiny pikachu
          a!g --n meowth --n zorua --n ralts --sh → plot 3 shinies on one graph
          a!g --evo pikachu --sh                → whole pikachu evo family
          a!g --sr 1/225 --sh                   → all 1/225 spawn-rate shinies
          a!g --n garchomp --since 2024-06-01
          a!g --compare mewtwo, iron valiant, brute bonnet --sh
        """
        raw = filters.split() if filters else []

        if not raw:
            await self._send_error(ctx, _FILTERS_BODY)
            return

        # ── Strip graph-only flags ────────────────────────────────────────────
        use_alltime  = FLAG_ALLTIME      in raw
        use_outliers = FLAG_WITHOUTLIERS in raw
        raw = [t for t in raw if t not in (FLAG_ALLTIME, FLAG_WITHOUTLIERS)]

        since_str,   raw = _extract_flag_value(raw, FLAG_SINCE)
        before_str,  raw = _extract_flag_value(raw, FLAG_BEFORE)
        compare_names, raw = _extract_flag_values(raw, FLAG_COMPARE)

        since_dt  = _parse_date_flag(since_str)  if since_str  else None
        before_dt = _parse_date_flag(before_str) if before_str else None

        if since_str and since_dt is None:
            await self._send_error(ctx, f"❌ Couldn't parse `--since {since_str}`. Use YYYY, YYYY-MM, or YYYY-MM-DD.")
            return
        if before_str and before_dt is None:
            await self._send_error(ctx, f"❌ Couldn't parse `--before {before_str}`. Use YYYY, YYYY-MM, or YYYY-MM-DD.")
            return

        # ── Extract name/evo/spawnrate flags ──────────────────────────────────
        multi_names,  raw_no_names = _extract_repeatable_flag_values(raw, _NAME_FLAGS)
        sr_val,       raw_no_names = _extract_flag_value_multi_alias(raw_no_names, _SPAWNRATE_FLAGS)
        evo_val,      raw_no_names = _extract_flag_value_multi_alias(raw_no_names, _EVO_FLAGS)

        # ── Validate names and flags ──────────────────────────────────────────
        from filters import is_flag, is_category_shortcut, resolve_flag

        _EXTRACTED = _NAME_FLAGS | _SPAWNRATE_FLAGS | _EVO_FLAGS | _GRAPH_ONLY_FLAGS
        invalid_names: list[str] = []
        unknown_flags: list[str] = []
        forms_db = get_forms_db()

        _names_to_check = list(multi_names) + ([evo_val] if evo_val else []) + list(compare_names)
        for mname in _names_to_check:
            check = mname.strip()
            if check.lower().endswith(" only"):   check = check[:-5].strip()
            if check.lower().startswith("normal "): check = check[7:].strip()
            if check and not forms_db.resolve_name_to_forms(check) and not resolve_pokemon_name(check):
                invalid_names.append(check)

        j = 0
        while j < len(raw_no_names):
            tok = raw_no_names[j]
            if tok.startswith("-"):
                if not is_flag(tok) and not is_category_shortcut(tok) and tok not in _EXTRACTED:
                    unknown_flags.append(tok)
                canon = resolve_flag(tok)
                info  = FLAG_DEFINITIONS.get(canon, {}) if canon else {}
                j += 1
                if info.get("takes_arg"):
                    while j < len(raw_no_names) and not raw_no_names[j].startswith("-"):
                        j += 1
            else:
                j += 1

        if invalid_names or unknown_flags:
            lines = [f"❌ **{n}** is not a valid Pokémon name." for n in invalid_names]
            lines += [f"❌ Unknown filter: `{f}`" for f in unknown_flags]
            lines.append(f"{REPLY} Check your spelling or use `a!a h` to see all available filters.")
            await self._send_error(ctx, "\n".join(lines))
            return

        # ── Shared setup ──────────────────────────────────────────────────────
        display_str = filters.strip() or "All auctions"
        ref         = self._ref(ctx)
        capped      = [False]

        if hasattr(ctx, "interaction") and ctx.interaction:
            await ctx.defer()
        else:
            await ctx.typing()

        # Always fetch all-time from DB so the alltime toggle works without an extra round-trip.
        # The year cutoff is applied in-memory when rendering.
        ts_filter_db = _build_ts_filter(True, since_dt, before_dt)

        async def _fetch_one(query: dict, limit: int | None = None) -> tuple[list[dict], bool]:
            recs, mc = await self._fetch(query, ts_filter_db, limit)
            if mc:
                capped[0] = True
            return recs, mc

        # ── COMPARE MODE ──────────────────────────────────────────────────────
        if compare_names:
            await self._handle_compare(
                ctx, ref, compare_names, multi_names, raw_no_names,
                _fetch_one, display_str, use_alltime, use_outliers, since_dt, before_dt,
            )
            return

        # ── MULTI-NAME MODE ───────────────────────────────────────────────────
        if len(multi_names) > 1:
            await self._handle_multi_name(
                ctx, ref, multi_names, raw_no_names,
                _fetch_one, display_str, use_alltime, use_outliers, since_dt, before_dt, capped,
            )
            return

        # ── SINGLE / ALL MODE ─────────────────────────────────────────────────
        await self._handle_single(
            ctx, ref, multi_names, sr_val, evo_val, raw_no_names,
            _fetch_one, display_str, use_alltime, use_outliers, since_dt, before_dt, capped,
        )

    # ── Mode handlers ─────────────────────────────────────────────────────────

    async def _handle_compare(
        self, ctx, ref, compare_names, multi_names, raw_no_names,
        fetch_one, display_str, use_alltime, use_outliers, since_dt, before_dt,
    ):
        """--compare mode: fetch each Pokémon separately and overlay on one chart."""
        variant_flags = [t for t in raw_no_names if t in ("--sh", "--shiny", "--gmax", "--noshiny")]
        no_name_flag  = not multi_names

        if no_name_flag:
            if len(compare_names) < 2:
                await self._send_error(ctx,
                    f"❌ `--compare` needs at least 2 Pokémon names.\n"
                    f"{REPLY} Example: `a!g --compare mewtwo, iron valiant, brute bonnet --sh`")
                return
            primary_name = compare_names[0]
            compare_names = compare_names[1:]
        else:
            primary_name  = multi_names[0]
            compare_names = (multi_names[1:] if len(multi_names) > 1 else []) + compare_names

        if len(compare_names) > 4:
            await self._send_error(ctx, "❌ Maximum 4 Pokémon in compare mode (5 total including primary).")
            return

        pquery, _, _ = build_query(["--name", primary_name] + variant_flags, expand_name_by_dex=True)
        primary_recs, _ = await fetch_one(pquery)
        if not primary_recs:
            await self._send_error(ctx, "❌ No auctions found for the primary Pokémon.")
            return

        series: list[dict] = [{
            "name":     primary_name.title(),
            "records":  primary_recs,
            "variant":  _detect_variant(pquery),
        }]

        for cname in compare_names:
            cquery, _, _ = build_query(["--name", cname] + variant_flags, expand_name_by_dex=True)
            crecs, _ = await fetch_one(cquery)
            if not crecs:
                await ctx.send(
                    view=_error_view(f"❌ No auctions found for `{cname}` — skipping."),
                    reference=ref, mention_author=False,
                )
                continue
            series.append({"name": cname.title(), "records": crecs, "variant": _detect_variant(pquery)})

        if len(series) < 2:
            await self._send_error(ctx, "❌ Need at least 2 Pokémon with data to compare.")
            return

        try:
            _check_memory()
            buf = build_compare_graph(series, display_str, alltime=use_alltime,
                                       show_outliers=use_outliers, since_dt=since_dt, before_dt=before_dt)
        except (_LowMemoryError, MemoryError):
            _free_memory()
            await self._send_error(ctx, "❌ Can't plot your graph due to low memory! Try adding a `--limit`.")
            return
        except Exception as e:
            await self._send_error(ctx, f"❌ Failed to generate comparison graph: `{e}`")
            return

        names_heading = " vs ".join(s["name"] for s in series)
        heading       = f"## {names_heading} — Price Comparison"
        at_badge      = "  •  🕐 All-time" if use_alltime else ""
        out_badge     = "  •  ⚠️ Raw data" if use_outliers else ""
        since_badge   = f"  •  📅 Since {since_dt.strftime('%b %Y')}" if since_dt else ""
        before_badge  = f"  •  📅 Before {before_dt.strftime('%b %Y')}" if before_dt else ""
        sub = (f"_Comparing {len(series)} Pokémon{at_badge}{since_badge}{before_badge}"
               f"{out_badge}  •  filters: `{display_str}`_")

        file = discord.File(buf, filename="graph.png")

        class _CView(discord.ui.LayoutView):
            container = discord.ui.Container(
                discord.ui.TextDisplay(content=heading),
                discord.ui.TextDisplay(content=sub),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://graph.png")),
                accent_colour=config.EMBED_COLOR,
            )
            def __init__(self): super().__init__(timeout=300)

        await ctx.send(view=_CView(), file=file, reference=ref, mention_author=False)

    async def _handle_multi_name(
        self, ctx, ref, multi_names, raw_no_names,
        fetch_one, display_str, use_alltime, use_outliers, since_dt, before_dt, capped,
    ):
        """--n meowth --n zorua mode: merge all into one combined series."""
        variant_flags   = list(raw_no_names)
        merged_records: list[dict] = []
        found_names:    list[str]  = []

        for mname in multi_names:
            mquery, _, mlimit = build_query(["--name", mname] + variant_flags, expand_name_by_dex=True)
            mrecs, _mc = await fetch_one(mquery, mlimit)
            if mrecs:
                merged_records.extend(mrecs)
                found_names.append(mname.title())

        if not merged_records:
            await self._send_error(ctx, "❌ No auctions found for any of the specified Pokémon.")
            return
        if len(merged_records) < 3:
            await self._send_error(ctx,
                f"❌ Only **{len(merged_records)}** auction(s) found — need at least 3.\n"
                f"{REPLY} Try broadening your filters.")
            return

        if len(found_names) <= 4:
            display_name = " / ".join(found_names)
        else:
            display_name = f"{', '.join(found_names[:4])} + {len(found_names) - 4} more"

        first_query, _, _ = build_query(["--name", multi_names[0]] + variant_flags, expand_name_by_dex=True)
        variant = _detect_variant(first_query)

        await self._render_and_send(
            ctx, ref, merged_records, first_query, display_str,
            use_alltime, use_outliers, since_dt, before_dt, capped,
            pokemon_name=display_name,
            extra_sub=f"  •  {len(found_names)} Pokémon",
            regen_extra={"multi_names": multi_names, "variant_flags": variant_flags,
                         "display_name": display_name, "found_names": found_names},
        )

    async def _handle_single(
        self, ctx, ref, multi_names, sr_val, evo_val, raw_no_names,
        fetch_one, display_str, use_alltime, use_outliers, since_dt, before_dt, capped,
    ):
        """Single Pokémon, spawn-rate, evo-family, or all-Pokémon mode."""
        requested_name: str | None = None

        if evo_val:
            query, _, limit = build_query(["--evo", evo_val] + list(raw_no_names), expand_name_by_dex=True)
            requested_name  = f"{evo_val.title()} family"

        elif multi_names:
            query, _, limit = build_query(["--name", multi_names[0]] + list(raw_no_names), expand_name_by_dex=True)
            requested_name  = multi_names[0].title()

        elif sr_val:
            query, _, limit = build_query(["--spawnrate", sr_val] + list(raw_no_names), expand_name_by_dex=True)
            sr_names = get_names_by_spawnrate(sr_val)
            if not sr_names:
                db     = get_spawnrate_db()
                valid  = ", ".join(f"1/{d}" for d in sorted(db.all_denominators())[:12])
                await self._send_error(ctx,
                    f"❌ No Pokémon found for spawn rate `{sr_val}`.\n"
                    f"{REPLY} Valid rates include: {valid} …\n"
                    f"{REPLY} Try `--sr 225` or `--sr 1/225`.")
                return
            requested_name = f"1/{sr_val.split('/')[-1]} spawn rate"

        else:
            query, _, limit = build_query(list(raw_no_names), expand_name_by_dex=True)
            requested_name  = None

        records, _ = await fetch_one(query, limit)

        if not records:
            await self._send_error(ctx, "❌ No auctions found matching your filters.")
            return

        # Apply year cutoff in-memory for initial render; full history kept for toggle
        ts_display = _build_ts_filter(use_alltime, since_dt, before_dt)
        display_records = _apply_ts_filter(records, ts_display)

        if not display_records:
            await self._send_error(ctx, "❌ No auctions found matching your filters.")
            return
        if len(display_records) < 3:
            await self._send_error(ctx,
                f"❌ Only **{len(display_records)}** auction(s) found — need at least 3.\n"
                f"{REPLY} Try broadening your filters.")
            return

        if requested_name is None:
            unique_pn = sorted({r.get("pn", "") for r in display_records if r.get("pn")})
            n_unique  = len(unique_pn)
            if n_unique == 1:
                requested_name = unique_pn[0]
            elif n_unique <= 3:
                requested_name = " / ".join(unique_pn)
            else:
                requested_name = f"{n_unique} Pokémon"

        await self._render_and_send(
            ctx, ref, display_records, query, display_str,
            use_alltime, use_outliers, since_dt, before_dt, capped,
            pokemon_name=requested_name,
            all_records=records,
        )

    async def _render_and_send(
        self,
        ctx: commands.Context,
        ref,
        display_records: list[dict],
        query: dict,
        display_str: str,
        use_alltime: bool,
        use_outliers: bool,
        since_dt: datetime | None,
        before_dt: datetime | None,
        capped: list[bool],
        *,
        pokemon_name: str,
        extra_sub: str = "",
        all_records: list[dict] | None = None,
        regen_extra: dict | None = None,
    ):
        """Build the graph, assemble the view, and send it. Shared by single and multi-name modes."""
        try:
            _check_memory()
            buf, outliers, fetched_count, plotted_count = build_graph(
                display_records, query, display_str,
                alltime=use_alltime,
                show_outliers=use_outliers,
                pokemon_name=pokemon_name,
            )
        except (_LowMemoryError, MemoryError):
            _free_memory()
            await self._send_error(ctx, "❌ Can't plot your graph due to low memory! Try adding a `--limit`.")
            return
        except Exception as e:
            await self._send_error(ctx, f"❌ Failed to generate graph: `{e}`")
            return

        variant   = _detect_variant(query)
        pal       = _PALETTE[variant]
        disc_tag  = _DISCORD_TAG[variant]
        accent    = config.SHINY_EMBED_COLOR if variant == "shiny" else config.EMBED_COLOR
        heading   = f"## {disc_tag} {pokemon_name} — Price History".strip()
        sub       = self._make_sub(
            fetched_count, plotted_count, capped[0], display_str,
            alltime=use_alltime, since_dt=since_dt, before_dt=before_dt,
            outliers=use_outliers, extra=extra_sub,
        )

        out_buf = build_outlier_image(outliers, pokemon_name, variant) if outliers else None

        # The full record set (all-time) needed for in-memory alltime toggle
        regen_records = all_records if all_records is not None else display_records

        regen_state = _RegenState(
            records=regen_records, query=query, display_str=display_str,
            limit=None, since_dt=since_dt, before_dt=before_dt,
            pokemon_name=pokemon_name, variant=variant, accent=accent,
            heading=heading, legend_text=_LEGEND_TEXT,
            filters_body=_FILTERS_BODY, protip_text=_PROTIP_TEXT,
        )
        if regen_extra:
            regen_state.found_names    = regen_extra.get("found_names")
            regen_state.variant_flags  = regen_extra.get("variant_flags")

        col = self._col

        async def _regen(interaction: discord.Interaction, new_alltime: bool, new_outliers: bool):
            await interaction.response.defer()
            st = regen_state

            # For multi-name mode we must re-fetch to get correct per-name records
            if st.found_names and st.variant_flags is not None:
                new_recs: list[dict] = []
                for mname in (regen_extra or {}).get("multi_names", []):
                    mq, _, ml = build_query(["--name", mname] + st.variant_flags, expand_name_by_dex=True)
                    mr, _ = await self._fetch(mq, _build_ts_filter(True, st.since_dt, st.before_dt), ml)
                    new_recs.extend(mr)
                if not new_recs:
                    await interaction.followup.send("❌ No data found.", ephemeral=True)
                    return
                new_display = _apply_ts_filter(new_recs, _build_ts_filter(new_alltime, st.since_dt, st.before_dt))
            else:
                new_display = _apply_ts_filter(st.records, _build_ts_filter(new_alltime, st.since_dt, st.before_dt))

            if not new_display:
                await interaction.followup.send("❌ No data found.", ephemeral=True)
                return

            try:
                _check_memory()
                new_buf, new_out, new_fetched, new_plotted = build_graph(
                    new_display, st.query, st.display_str,
                    alltime=new_alltime, show_outliers=new_outliers,
                    pokemon_name=st.pokemon_name,
                )
            except (_LowMemoryError, MemoryError):
                _free_memory()
                await interaction.followup.send(
                    "❌ Can't plot — low memory. Try `--limit`.", ephemeral=True)
                return
            except Exception as exc:
                await interaction.followup.send(f"❌ Failed to regenerate: `{exc}`", ephemeral=True)
                return

            new_out_buf = build_outlier_image(new_out, st.pokemon_name, st.variant) if new_out else None
            extra_s     = f"  •  {len(st.found_names)} Pokémon" if st.found_names else ""
            new_sub     = self._make_sub(
                new_fetched, new_plotted, capped[0], st.display_str,
                alltime=new_alltime, since_dt=st.since_dt, before_dt=st.before_dt,
                outliers=new_outliers, extra=extra_s,
            )
            new_btns    = _build_btn_list(
                st.legend_text, st.filters_body,
                bool(new_out), len(new_out), new_out_buf, new_out,
                new_alltime, new_outliers, _regen, col=col,
            )
            new_comps   = _make_graph_components(st.heading, new_sub, st.protip_text)
            new_file    = discord.File(new_buf, filename="graph.png")
            await interaction.edit_original_response(
                attachments=[new_file],
                view=_make_graph_view(new_comps, new_btns, st.accent),
            )

        btn_rows = _build_btn_list(
            _LEGEND_TEXT, _FILTERS_BODY,
            bool(outliers), len(outliers), out_buf, outliers,
            use_alltime, use_outliers, _regen, col=col,
        )
        comps = _make_graph_components(heading, sub, _PROTIP_TEXT)
        view  = _make_graph_view(comps, btn_rows, accent)
        file  = discord.File(buf, filename="graph.png")

        await ctx.send(view=view, file=file, reference=ref, mention_author=False)

        # Cleanup — do NOT clear regen_records; regen_state still holds a reference
        outliers.clear()
        buf.close()
        if out_buf:
            for b in out_buf:
                b.close()
        _free_memory()


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Graph(bot))
