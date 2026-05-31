"""
cogs/stats.py – Auction statistics and leaderboards.

Field mapping (DB short name → meaning):
  mid  = message_id          aid  = auction_id
  ts   = unix_timestamp      pn   = pokemon_name
  lv   = level               sh   = shiny
  gx   = gmax                nat  = nature
  gen  = gender              hi   = held_item
  iv   = total_iv_percent    hp/atk/def/spa/spd/spe = individual IVs
  mv   = moves               bid  = winning_bid
  bdr  = bidder_id           sn   = seller_name
  sid  = seller_id           (stored directly as int)

Commands:
  j!stats [@user]        — full stats for a user (tabbed: overview / buying / selling)
  j!lb [type] [variant]  — leaderboards with dropdown switcher + time period + mode
  j!market               — server-wide market insights (tabbed dropdown)

Leaderboard dropdowns:
  1. Type     — which leaderboard (sellers, bidders, shiny, gmax, pokemon, expensive…)
  2. Period   — All Time / current month / last 3 months
  3. Mode     — Money (earned/spent) vs Count (auctions listed/won)
               Disabled with "N/A for this board" on pokemon/expensive boards.

Caching (two-layer):
  PRIMARY:   In-memory dict (_mem_cache) — nanosecond reads, zero I/O.
  SECONDARY: MongoDB lb_cache collection — persists across restarts.

  Flow:
    Startup  → bulk-load all valid docs from MongoDB into _mem_cache.
    Runtime  → j!lb reads exclusively from _mem_cache (pure dict lookup).
    Every 6h → background task recomputes all combos atomically.
    Restart  → MongoDB re-read; if still fresh (< 6h old) no recompute needed.

  j!stats always runs live, firing all queries in parallel via asyncio.gather().
"""
from __future__ import annotations

import asyncio
import logging
import time
from calendar import monthrange
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo import MongoClient

import config
from config import REPLY
from utils import shiny_prefix

log = logging.getLogger(__name__)

_mongo     = MongoClient(config.MONGO_URI)
_db        = _mongo[config.MONGO_DB_NAME]
_col       = _db[config.MONGO_COLLECTION]
_cache_col = _db["lb_cache"]

CACHE_TTL_SECONDS = 6 * 3600
LB_SIZE           = 10
SAFE_MENTIONS     = discord.AllowedMentions.none()

# mode values
MODE_MONEY = "money"
MODE_COUNT = "count"

# lb_types where the mode dropdown is meaningful
MODE_APPLICABLE = {
    "sellers",       "bidders",
    "shiny_sellers", "shiny_bidders",
    "gmax_sellers",  "gmax_bidders",
}

# Mongo filter presets
_FILTER_SHINY = {"sh": True,  "gx": {"$ne": True}}
_FILTER_GMAX  = {"gx": True}

# In-memory cache: { cache_key: {"rows": [...], "next_refresh": int} }
_mem_cache: dict[str, dict] = {}


# ═════════════════════════════════════════════════════════════════════════════
# TIME PERIOD HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _period_options() -> list[dict]:
    now  = datetime.now(timezone.utc)
    opts = [{"label": "All Time", "value": "all", "ts_gte": None, "ts_lt": None}]
    for i in range(4):
        if i == 0:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end   = now
        else:
            month = now.month - i
            year  = now.year
            while month <= 0:
                month += 12
                year  -= 1
            days_in_month = monthrange(year, month)[1]
            start = datetime(year, month, 1,  0, 0, 0, tzinfo=timezone.utc)
            end   = datetime(year, month, days_in_month, 23, 59, 59, tzinfo=timezone.utc)
        opts.append({
            "label":  start.strftime("%B %Y"),
            "value":  start.strftime("%Y-%m"),
            "ts_gte": int(start.timestamp()),
            "ts_lt":  int(end.timestamp()),
        })
    return opts


def _period_match(period_value: str, periods: list[dict]) -> dict:
    for p in periods:
        if p["value"] == period_value:
            if p["ts_gte"] is None:
                return {}
            return {"ts": {"$gte": p["ts_gte"], "$lte": p["ts_lt"]}}
    return {}


# ═════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def _fmt(val: float) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}k"
    return f"{int(val):,}"


def _medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"`#{i + 1}`"


def _fmt_user(user_id, name: str | None = None) -> str:
    if user_id is not None:
        try:
            mention = f"<@{int(user_id)}>"
            return f"{mention} (`{name}`)" if name else mention
        except (TypeError, ValueError):
            pass
    return f"`{name}`" if name else "`Unknown`"


def _sep(visible: bool = True) -> discord.ui.Separator:
    return discord.ui.Separator(visible=visible, spacing=discord.SeparatorSpacing.small)


def _get_date_range_footer() -> str:
    try:
        oldest = _col.find_one({"ts": {"$exists": True, "$ne": None}}, {"ts": 1}, sort=[("ts",  1)])
        newest = _col.find_one({"ts": {"$exists": True, "$ne": None}}, {"ts": 1}, sort=[("ts", -1)])
        if oldest and newest:
            fmt   = "%b %d, %Y"
            start = datetime.fromtimestamp(oldest["ts"], tz=timezone.utc).strftime(fmt)
            end   = datetime.fromtimestamp(newest["ts"], tz=timezone.utc).strftime(fmt)
            return f"📅 Data from **{start}** to **{end}**"
    except Exception:
        pass
    return ""


def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


def _interleave_seps(sections: list[str], final_sep: bool = True) -> list:
    comps = []
    for i, section in enumerate(sections):
        comps.append(discord.ui.TextDisplay(content=section))
        if final_sep or i < len(sections) - 1:
            comps.append(_sep())
    return comps


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD CACHE
# ═════════════════════════════════════════════════════════════════════════════

def _cache_key(lb_type: str, variant: str, period_value: str, mode: str) -> str:
    return f"{lb_type}__{variant}__{period_value}__{mode}"


def _write_cache(key: str, rows: list, next_refresh: int) -> None:
    _mem_cache[key] = {"rows": rows, "next_refresh": next_refresh}
    try:
        _cache_col.update_one(
            {"_id": key},
            {"$set": {"rows": rows, "next_refresh": next_refresh, "built_at": int(time.time())}},
            upsert=True,
        )
    except Exception:
        log.exception("Failed to persist cache to MongoDB for key=%s", key)


def _read_cache(key: str) -> tuple[list | None, int | None]:
    now   = int(time.time())
    entry = _mem_cache.get(key)
    if entry and entry["next_refresh"] > now:
        return entry["rows"], entry["next_refresh"]
    try:
        doc = _cache_col.find_one({"_id": key})
        if doc and doc.get("next_refresh", 0) > now:
            _mem_cache[doc["_id"]] = {"rows": doc["rows"], "next_refresh": doc["next_refresh"]}
            return doc["rows"], doc["next_refresh"]
    except Exception:
        log.exception("Failed to read cache from MongoDB for key=%s", key)
    return None, None


def _load_all_from_mongo() -> int:
    now   = int(time.time())
    count = 0
    try:
        for doc in _cache_col.find({"next_refresh": {"$gt": now}}):
            _mem_cache[doc["_id"]] = {"rows": doc["rows"], "next_refresh": doc["next_refresh"]}
            count += 1
    except Exception:
        log.exception("Failed to bulk-load cache from MongoDB")
    return count


def _next_refresh_ts() -> int:
    return int(time.time()) + CACHE_TTL_SECONDS


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD AGGREGATIONS
# ═════════════════════════════════════════════════════════════════════════════

def _seller_agg(ts_match: dict, mode: str, extra_match: dict | None = None) -> list[dict]:
    """
    Seller-side aggregation.
      mode=money → sort by total bid earned
      mode=count → sort by number of auctions listed
      extra_match → optional sh/gx filter
    """
    sort_field = "total" if mode == MODE_MONEY else "count"
    base: dict = {"$or": [
        {"sid": {"$exists": True, "$ne": None}},
        {"sn":  {"$exists": True, "$ne": None}},
    ]}
    if extra_match:
        base.update(extra_match)
    if ts_match:
        base.update(ts_match)
    pipe = [
        {"$match": base},
        {"$group": {
            "_id": {"$cond": {
                "if":   {"$and": [{"$ne": ["$sid", None]}, {"$ne": ["$sid", ""]}]},
                "then": {"type": "id",   "val": "$sid"},
                "else": {"type": "name", "val": "$sn"},
            }},
            "name":  {"$last": "$sn"},
            "sid":   {"$last": "$sid"},
            "total": {"$sum": "$bid"},
            "count": {"$sum": 1},
        }},
        {"$sort": {sort_field: -1}},
        {"$limit": LB_SIZE},
    ]
    rows = list(_col.aggregate(pipe))
    return [{"id": r.get("sid"), "name": r.get("name") or "Unknown", "total": r["total"], "count": r["count"]} for r in rows]


def _bidder_agg(ts_match: dict, mode: str, extra_match: dict | None = None) -> list[dict]:
    """
    Bidder-side aggregation.
      mode=money → sort by total bid spent
      mode=count → sort by number of auctions won
      extra_match → optional sh/gx filter
    """
    sort_field = "total" if mode == MODE_MONEY else "count"
    base: dict = {"bdr": {"$exists": True}}
    if extra_match:
        base.update(extra_match)
    if ts_match:
        base.update(ts_match)
    pipe = [
        {"$match": base},
        {"$group": {"_id": "$bdr", "total": {"$sum": "$bid"}, "count": {"$sum": 1}}},
        {"$sort": {sort_field: -1}},
        {"$limit": LB_SIZE},
    ]
    return list(_col.aggregate(pipe))


def _compute_lb_rows(lb_type: str, variant: str, ts_match: dict, mode: str) -> list:
    """Return JSON-serialisable rows for the given type/variant/period/mode combo."""

    # ── Overall ───────────────────────────────────────────────────────────────
    if lb_type == "sellers":
        return _seller_agg(ts_match, mode)

    if lb_type == "bidders":
        return _bidder_agg(ts_match, mode)

    # ── Shiny ─────────────────────────────────────────────────────────────────
    if lb_type == "shiny_sellers":
        return _seller_agg(ts_match, mode, _FILTER_SHINY)

    if lb_type == "shiny_bidders":
        return _bidder_agg(ts_match, mode, _FILTER_SHINY)

    # ── Gmax ──────────────────────────────────────────────────────────────────
    if lb_type == "gmax_sellers":
        return _seller_agg(ts_match, mode, _FILTER_GMAX)

    if lb_type == "gmax_bidders":
        return _bidder_agg(ts_match, mode, _FILTER_GMAX)

    # ── Pokémon volume (mode not applicable) ──────────────────────────────────
    if lb_type == "pokemon":
        vmap: dict = {
            "shiny":   _FILTER_SHINY,
            "gmax":    _FILTER_GMAX,
            "normal":  {"sh": {"$ne": True}, "gx": {"$ne": True}},
            "overall": {},
        }
        match = dict(vmap.get(variant, {}))
        match.update(ts_match)
        return list(_col.aggregate([
            {"$match": match},
            {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}},
            {"$sort": {"count": -1}}, {"$limit": LB_SIZE},
        ]))

    # ── Biggest single sales (mode not applicable) ────────────────────────────
    if lb_type == "expensive":
        match: dict = {}
        match.update(ts_match)
        return list(_col.find(
            match,
            {"aid": 1, "pn": 1, "bid": 1, "sn": 1, "sid": 1, "bdr": 1, "sh": 1, "gx": 1}
        ).sort("bid", -1).limit(LB_SIZE))

    return []


def _precompute_all_lb(periods: list[dict] | None = None) -> None:
    if periods is None:
        periods = _period_options()

    # (lb_type, variant)
    lb_combos = [
        ("sellers",       "overall"),
        ("bidders",       "overall"),
        ("shiny_sellers", "overall"),
        ("shiny_bidders", "overall"),
        ("gmax_sellers",  "overall"),
        ("gmax_bidders",  "overall"),
        ("pokemon",       "overall"),
        ("pokemon",       "normal"),
        ("pokemon",       "shiny"),
        ("pokemon",       "gmax"),
        ("expensive",     "overall"),
    ]

    next_refresh = _next_refresh_ts()

    for period in periods:
        ts_match = _period_match(period["value"], periods)
        for lb_type, variant in lb_combos:
            if lb_type in MODE_APPLICABLE:
                for mode in (MODE_MONEY, MODE_COUNT):
                    key  = _cache_key(lb_type, variant, period["value"], mode)
                    rows = _compute_lb_rows(lb_type, variant, ts_match, mode)
                    _write_cache(key, rows, next_refresh)
                    log.debug("Cache built: %s", key)
            else:
                # pokemon / expensive — mode irrelevant, only store under money key
                key  = _cache_key(lb_type, variant, period["value"], MODE_MONEY)
                rows = _compute_lb_rows(lb_type, variant, ts_match, MODE_MONEY)
                _write_cache(key, rows, next_refresh)
                log.debug("Cache built: %s", key)

    log.info("Leaderboard cache rebuild complete. Next refresh: %s",
             datetime.fromtimestamp(next_refresh, tz=timezone.utc).isoformat())


def _get_lb_rows(lb_type: str, variant: str, period_value: str, mode: str,
                 periods: list[dict]) -> tuple[list, int | None]:
    effective_mode = mode if lb_type in MODE_APPLICABLE else MODE_MONEY
    key  = _cache_key(lb_type, variant, period_value, effective_mode)
    rows, next_refresh = _read_cache(key)
    if rows is None:
        log.info("Cache miss for %s — computing live", key)
        ts_match     = _period_match(period_value, periods)
        rows         = _compute_lb_rows(lb_type, variant, ts_match, effective_mode)
        next_refresh = _next_refresh_ts()
        _write_cache(key, rows, next_refresh)
    return rows, next_refresh


# ═════════════════════════════════════════════════════════════════════════════
# RANK LOOKUP
# ═════════════════════════════════════════════════════════════════════════════

def _get_user_rank(lb_type: str, uid: int, period_value: str, mode: str,
                   periods: list[dict] | None = None) -> dict | None:
    if periods is None:
        periods = _period_options()
    ts_match   = _period_match(period_value, periods)
    sort_field = "total" if mode == MODE_MONEY else "count"

    try:
        if lb_type in ("sellers", "shiny_sellers", "gmax_sellers"):
            extra: dict = {}
            if lb_type == "shiny_sellers":
                extra = dict(_FILTER_SHINY)
            elif lb_type == "gmax_sellers":
                extra = dict(_FILTER_GMAX)
            base: dict = {"$or": [{"sid": {"$exists": True, "$ne": None}}, {"sn": {"$exists": True, "$ne": None}}]}
            base.update(extra)
            base.update(ts_match)
            pipe = [
                {"$match": base},
                {"$group": {
                    "_id": {"$cond": {
                        "if":   {"$and": [{"$ne": ["$sid", None]}, {"$ne": ["$sid", ""]}]},
                        "then": {"type": "id",   "val": "$sid"},
                        "else": {"type": "name", "val": "$sn"},
                    }},
                    "sid": {"$last": "$sid"}, "total": {"$sum": "$bid"}, "count": {"$sum": 1},
                }},
                {"$sort": {sort_field: -1}},
            ]
            rows = list(_col.aggregate(pipe))
            for i, r in enumerate(rows):
                if r.get("sid") == uid:
                    return {"rank": i + 1, "total": r["total"], "count": r["count"], "total_entries": len(rows)}

        elif lb_type in ("bidders", "shiny_bidders", "gmax_bidders"):
            extra = {}
            if lb_type == "shiny_bidders":
                extra = dict(_FILTER_SHINY)
            elif lb_type == "gmax_bidders":
                extra = dict(_FILTER_GMAX)
            m: dict = {"bdr": {"$exists": True}}
            m.update(extra)
            m.update(ts_match)
            pipe = [
                {"$match": m},
                {"$group": {"_id": "$bdr", "total": {"$sum": "$bid"}, "count": {"$sum": 1}}},
                {"$sort": {sort_field: -1}},
            ]
            rows = list(_col.aggregate(pipe))
            for i, r in enumerate(rows):
                if r["_id"] == uid:
                    return {"rank": i + 1, "total": r["total"], "count": r["count"], "total_entries": len(rows)}
    except Exception:
        log.exception("Error in _get_user_rank")
    return None


def _rank_footer(lb_type: str, uid: int, period_value: str, mode: str,
                 periods: list[dict] | None = None) -> str | None:
    if lb_type not in MODE_APPLICABLE:
        return None
    if periods is None:
        periods = _period_options()
    data = _get_user_rank(lb_type, uid, period_value, mode, periods)
    if not data:
        return None

    rank  = data["rank"]
    total = data["total_entries"]
    medal = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"**#{rank:,}**"
    is_seller = lb_type in ("sellers", "shiny_sellers", "gmax_sellers")

    if mode == MODE_MONEY:
        verb   = "earned" if is_seller else "spent"
        stat_s = f"`{_fmt(data['total'])}` {verb} across `{data['count']:,}` auctions"
    else:
        verb   = "listed" if is_seller else "won"
        stat_s = f"`{data['count']:,}` auctions {verb}"

    return f"📍 You are {medal} out of `{total:,}` — {stat_s}"


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD — DYNAMIC TITLES
# ═════════════════════════════════════════════════════════════════════════════

def _lb_title(lb_type: str, variant: str, mode: str) -> str:
    money = mode == MODE_MONEY

    # Static titles (mode irrelevant)
    if lb_type == "pokemon":
        return {
            "overall": "📦 Most Auctioned — Overall",
            "normal":  "🔵 Most Auctioned — Normal",
            "shiny":   "✨ Most Auctioned — Shiny",
            "gmax":    "⚡ Most Auctioned — Gigantamax",
        }.get(variant, "📦 Most Auctioned")
    if lb_type == "expensive":
        return "💰 Biggest Sales Ever"

    # Dynamic titles (money vs count)
    titles = {
        "sellers":       ("🏆 Top Sellers — Money Earned",        "🏆 Top Sellers — Most Auctions Listed"),
        "bidders":       ("💸 Top Bidders — Money Spent",         "💸 Top Bidders — Most Auctions Won"),
        "shiny_sellers": ("✨ Top Shiny Sellers — Money Earned",  "✨ Most Shinies Listed"),
        "shiny_bidders": ("✨ Top Shiny Buyers — Money Spent",    "✨ Most Shinies Bought"),
        "gmax_sellers":  ("⚡ Top Gmax Sellers — Money Earned",   "⚡ Most Gmaxes Listed"),
        "gmax_bidders":  ("⚡ Top Gmax Buyers — Money Spent",     "⚡ Most Gmaxes Bought"),
    }
    pair = titles.get(lb_type, ("🏆 Leaderboard", "🏆 Leaderboard"))
    return pair[0] if money else pair[1]


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD — BODY RENDERER
# ═════════════════════════════════════════════════════════════════════════════

def _render_lb_body(lb_type: str, rows: list, mode: str,
                    caller_id: int | None = None,
                    period_value: str = "all",
                    periods: list[dict] | None = None) -> str:
    if not rows:
        return "❌ No data found for this leaderboard."

    money = mode == MODE_MONEY
    lines: list[str] = []

    # ── Seller-side ───────────────────────────────────────────────────────────
    if lb_type in ("sellers", "shiny_sellers", "gmax_sellers"):
        for i, r in enumerate(rows):
            user_s = _fmt_user(r["id"], r.get("name"))
            if money:
                lines.append(f"{_medal(i)} {user_s} — `{_fmt(r['total'])}` earned  •  `{r['count']:,}` listed")
            else:
                lines.append(f"{_medal(i)} {user_s} — `{r['count']:,}` listed  •  `{_fmt(r['total'])}` earned")

    # ── Bidder-side ───────────────────────────────────────────────────────────
    elif lb_type in ("bidders", "shiny_bidders", "gmax_bidders"):
        for i, r in enumerate(rows):
            if money:
                lines.append(f"{_medal(i)} <@{r['_id']}> — `{_fmt(r['total'])}` spent  •  `{r['count']:,}` won")
            else:
                lines.append(f"{_medal(i)} <@{r['_id']}> — `{r['count']:,}` won  •  `{_fmt(r['total'])}` spent")

    # ── Pokémon volume ────────────────────────────────────────────────────────
    elif lb_type == "pokemon":
        lines = [
            f"{_medal(i)} **{r['_id']}** — `{r['count']:,}` auctions  •  avg `{_fmt(r['avg'])}`"
            for i, r in enumerate(rows)
        ]

    # ── Biggest single sales ──────────────────────────────────────────────────
    elif lb_type == "expensive":
        for i, r in enumerate(rows):
            seller_s = _fmt_user(r.get("sid"), r.get("sn")) if r.get("sid") else f"`{r.get('sn', '?')}`"
            bidder_s = f"<@{r['bdr']}>" if r.get("bdr") else "Unknown"
            lines.append(
                f"{_medal(i)} {shiny_prefix(r)}**{r.get('pn', '?')}** — `{_fmt(r.get('bid', 0))}`\n"
                f"　Sold by {seller_s} → {bidder_s}  •  `#{r.get('aid', '?')}`"
            )

    body = "\n".join(lines)

    if caller_id is not None and periods is not None:
        footer = _rank_footer(lb_type, caller_id, period_value, mode, periods)
        if footer:
            body += f"\n\n{footer}"

    return body


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD — VIEW  (three dropdowns: type / period / mode)
# ═════════════════════════════════════════════════════════════════════════════

def _build_lb_view(
    current_type:    str = "sellers",
    current_variant: str = "overall",
    current_period:  str = "all",
    current_mode:    str = MODE_MONEY,
    caller_id:       int | None = None,
    periods:         list[dict] | None = None,
) -> discord.ui.LayoutView:

    if periods is None:
        periods = _period_options()

    title           = _lb_title(current_type, current_variant, current_mode)
    rows, next_r    = _get_lb_rows(current_type, current_variant, current_period, current_mode, periods)
    body            = _render_lb_body(current_type, rows, current_mode, caller_id, current_period, periods)
    date_footer     = _get_date_range_footer()
    period_label    = next((p["label"] for p in periods if p["value"] == current_period), "All Time")
    mode_applicable = current_type in MODE_APPLICABLE
    mode_label      = "💰 Money" if current_mode == MODE_MONEY else "🔢 Count"

    # title_key for default-marking the type select option
    title_key = f"{current_type}_{current_variant}" if current_type == "pokemon" else current_type

    refresh_line = ""
    if next_r:
        refresh_line = f"-# 🔄 Cache updates {discord.utils.format_dt(datetime.fromtimestamp(next_r, tz=timezone.utc), style='R')}"

    # ── Dropdown 1: Type ──────────────────────────────────────────────────────
    class TypeSelect(discord.ui.Select):
        def __init__(self):
            options = [
                # Overall
                discord.SelectOption(label="Top Sellers",        value="sellers",         emoji="🏆", description="Overall sellers — toggle money/count below"),
                discord.SelectOption(label="Top Bidders",        value="bidders",         emoji="💸", description="Overall bidders — toggle money/count below"),
                # Shiny
                discord.SelectOption(label="Shiny Sellers",      value="shiny_sellers",   emoji="✨", description="Money earned or shinies listed"),
                discord.SelectOption(label="Shiny Buyers",       value="shiny_bidders",   emoji="✨", description="Money spent or shinies bought"),
                # Gmax
                discord.SelectOption(label="Gmax Sellers",       value="gmax_sellers",    emoji="⚡", description="Money earned or gmaxes listed"),
                discord.SelectOption(label="Gmax Buyers",        value="gmax_bidders",    emoji="⚡", description="Money spent or gmaxes bought"),
                # Pokémon volume
                discord.SelectOption(label="Pokémon — Overall",  value="pokemon_overall", emoji="📦"),
                discord.SelectOption(label="Pokémon — Normal",   value="pokemon_normal",  emoji="🔵"),
                discord.SelectOption(label="Pokémon — Shiny",    value="pokemon_shiny",   emoji="✨"),
                discord.SelectOption(label="Pokémon — Gmax",     value="pokemon_gmax",    emoji="⚡"),
                # Sales
                discord.SelectOption(label="Biggest Sales Ever", value="expensive",       emoji="💰", description="Top single auction sales"),
            ]
            for o in options:
                if o.value == title_key:
                    o.default = True
            super().__init__(placeholder="Switch leaderboard…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                val = self.values[0]
                cid = interaction.user.id
                if val.startswith("pokemon_"):
                    lb_t, lb_v = "pokemon", val.split("_", 1)[1]
                else:
                    lb_t, lb_v = val, "overall"
                new_view = _build_lb_view(lb_t, lb_v, current_period, current_mode, cid, periods)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in TypeSelect callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    # ── Dropdown 2: Period ────────────────────────────────────────────────────
    class PeriodSelect(discord.ui.Select):
        def __init__(self):
            options = []
            for p in periods:
                opt       = discord.SelectOption(label=p["label"], value=p["value"])
                opt.emoji = "🗂️" if p["value"] == "all" else "📅"
                if p["value"] == current_period:
                    opt.default = True
                options.append(opt)
            super().__init__(placeholder="Switch time period…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                cid      = interaction.user.id
                new_view = _build_lb_view(current_type, current_variant, self.values[0], current_mode, cid, periods)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in PeriodSelect callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    # ── Dropdown 3: Mode ──────────────────────────────────────────────────────
    class ModeSelect(discord.ui.Select):
        def __init__(self):
            if mode_applicable:
                options = [
                    discord.SelectOption(
                        label="Money", value=MODE_MONEY, emoji="💰",
                        description="Rank by total money earned / spent",
                        default=(current_mode == MODE_MONEY),
                    ),
                    discord.SelectOption(
                        label="Count", value=MODE_COUNT, emoji="🔢",
                        description="Rank by number of auctions listed / won",
                        default=(current_mode == MODE_COUNT),
                    ),
                ]
                super().__init__(placeholder=f"Mode: {mode_label}", options=options)
            else:
                options = [
                    discord.SelectOption(label="N/A for this board", value="na", default=True),
                ]
                super().__init__(
                    placeholder="Mode: N/A for this board",
                    options=options,
                    disabled=True,
                )

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                val = self.values[0]
                if val == "na":
                    return
                cid      = interaction.user.id
                new_view = _build_lb_view(current_type, current_variant, current_period, val, cid, periods)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in ModeSelect callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    # ── Assemble ──────────────────────────────────────────────────────────────
    comps: list = [
        discord.ui.TextDisplay(content=f"## {title}\n-# 📅 {period_label}"),
        _sep(),
        discord.ui.TextDisplay(content=body),
        _sep(),
    ]
    if refresh_line:
        comps.append(discord.ui.TextDisplay(content=refresh_line))
    if date_footer:
        comps += [discord.ui.TextDisplay(content=f"-# {date_footer}"), _sep(False)]

    comps.append(discord.ui.ActionRow(TypeSelect()))
    comps.append(discord.ui.ActionRow(PeriodSelect()))
    comps.append(discord.ui.ActionRow(ModeSelect()))

    class LbView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return LbView()


# ═════════════════════════════════════════════════════════════════════════════
# USER STATS — PARALLEL DATA FETCHING
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_user_data(uid: int) -> dict:
    bm   = {"bdr": uid}
    sm   = {"sid": uid}
    loop = asyncio.get_event_loop()

    def _run(fn, *args, **kwargs):
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def agg(pipe):
        return list(_col.aggregate(pipe))

    def find_one_sorted(match, sort_field, direction=-1):
        return _col.find_one(match, sort=[(sort_field, direction)])

    def find_sorted_limit(match, sort_field, direction, limit, projection=None):
        q = _col.find(match, projection) if projection else _col.find(match)
        return list(q.sort(sort_field, direction).limit(limit))

    (
        won_res, fav_buys, priciest_buy, shiny_bought, gmax_bought,
        natures_b, iv_b_res, monthly_spent,
        sold_res, fav_sells, best_sales, shiny_sold, gmax_sold,
        natures_s, iv_s_res, monthly_earned,
    ) = await asyncio.gather(
        _run(agg, [{"$match": bm}, {"$group": {"_id": None, "total": {"$sum": "$bid"}, "count": {"$sum": 1}, "avg": {"$avg": "$bid"}, "max": {"$max": "$bid"}}}]),
        _run(agg, [{"$match": bm}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}]),
        _run(find_one_sorted, bm, "bid"),
        _run(agg, [{"$match": {**bm, **_FILTER_SHINY}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**bm, **_FILTER_GMAX}},  {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**bm, "nat": {"$ne": None}}}, {"$group": {"_id": "$nat", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**bm, "iv":  {"$ne": None}}}, {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}]),
        _run(agg, [
            {"$match": {**bm, "ts": {"$exists": True}}},
            {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
            {"$group": {"_id": "$month", "spent": {"$sum": "$bid"}, "count": {"$sum": 1}}},
            {"$sort": {"_id": -1}}, {"$limit": 4},
        ]),
        _run(agg, [{"$match": sm}, {"$group": {"_id": None, "total": {"$sum": "$bid"}, "count": {"$sum": 1}, "avg": {"$avg": "$bid"}, "max": {"$max": "$bid"}}}]),
        _run(agg, [{"$match": sm}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}]),
        _run(find_sorted_limit, sm, "bid", -1, 5),
        _run(agg, [{"$match": {**sm, **_FILTER_SHINY}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**sm, **_FILTER_GMAX}},  {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**sm, "nat": {"$ne": None}}}, {"$group": {"_id": "$nat", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**sm, "iv":  {"$ne": None}}}, {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}]),
        _run(agg, [
            {"$match": {**sm, "ts": {"$exists": True}}},
            {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
            {"$group": {"_id": "$month", "earned": {"$sum": "$bid"}, "count": {"$sum": 1}}},
            {"$sort": {"_id": -1}}, {"$limit": 4},
        ]),
    )

    return {
        "won":            won_res[0]  if won_res  else {},
        "sold":           sold_res[0] if sold_res else {},
        "fav_buys":       fav_buys,
        "priciest_buy":   priciest_buy,
        "shiny_bought":   shiny_bought,
        "gmax_bought":    gmax_bought,
        "natures_bought": natures_b,
        "iv_bought":      iv_b_res[0] if iv_b_res else {},
        "monthly_spent":  monthly_spent,
        "fav_sells":      fav_sells,
        "best_sales":     best_sales,
        "shiny_sold":     shiny_sold,
        "gmax_sold":      gmax_sold,
        "natures_sold":   natures_s,
        "iv_sold":        iv_s_res[0] if iv_s_res else {},
        "monthly_earned": monthly_earned,
    }


# ═════════════════════════════════════════════════════════════════════════════
# USER STATS — PAGE RENDERERS
# ═════════════════════════════════════════════════════════════════════════════

def _page_user_overview(data: dict, user: discord.User | discord.Member) -> list[str]:
    won  = data["won"]
    sold = data["sold"]
    wc   = won.get("count", 0)
    sc   = sold.get("count", 0)

    net     = sold.get("total", 0) - won.get("total", 0)
    net_s   = f"+{_fmt(net)}" if net >= 0 else f"-{_fmt(abs(net))}"
    net_ico = "🟢" if net >= 0 else "🔴"

    pb   = data["priciest_buy"]
    pb_s = f"{shiny_prefix(pb)}`{pb.get('pn','?')}` — `{_fmt(pb.get('bid',0))}`" if pb else "—"
    bs   = data["best_sales"][0] if data["best_sales"] else None
    bs_s = f"{shiny_prefix(bs)}`{bs.get('pn','?')}` — `{_fmt(bs.get('bid',0))}`" if bs else "—"

    if wc:
        bidder_block = "\n".join([
            "**💸 As Bidder**",
            f"{REPLY} Auctions Won: `{wc:,}`  •  Total Spent: `{_fmt(won.get('total', 0))}`",
            f"{REPLY} Avg per Win: `{_fmt(won.get('avg', 0))}`  •  Highest: `{_fmt(won.get('max', 0))}`",
            f"{REPLY} Priciest Buy: {pb_s}",
        ])
    else:
        bidder_block = f"**💸 As Bidder**\n{REPLY} _No wins recorded._"

    if sc:
        seller_block = "\n".join([
            "**🏷️ As Seller**",
            f"{REPLY} Auctions Listed: `{sc:,}`  •  Total Earned: `{_fmt(sold.get('total', 0))}`",
            f"{REPLY} Avg Sale: `{_fmt(sold.get('avg', 0))}`  •  Highest: `{_fmt(sold.get('max', 0))}`",
            f"{REPLY} Best Sale: {bs_s}",
        ])
    else:
        seller_block = f"**🏷️ As Seller**\n{REPLY} _No auctions listed._"

    net_block = f"**⚖️ Net Balance:** {net_ico} `{net_s}` _(earned − spent)_"
    return [bidder_block, seller_block, net_block]


def _page_user_buying(data: dict) -> list[str]:
    if not data["won"].get("count"):
        return ["**💸 Buying Details**\n\n_No auction wins recorded._"]

    fav_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["fav_buys"]) or "—"

    shiny_s = "\n".join(
        f"{REPLY} ✨**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["shiny_bought"]
    ) or f"{REPLY} —"

    gmax_s = "\n".join(
        f"{REPLY} ⚡**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["gmax_bought"]
    ) or f"{REPLY} —"

    nat_s   = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["natures_bought"]) or "—"
    iv      = data["iv_bought"]
    iv_s    = f"`{iv.get('avg', 0):.1f}%` avg  •  `{iv.get('max', 0):.2f}%` best" if iv else "—"
    monthly = "\n".join(
        f"{REPLY} `{r['_id']}` — `{_fmt(r['spent'])}` across `{r['count']:,}` wins"
        for r in data["monthly_spent"]
    ) or f"{REPLY} —"

    return [
        f"**Favourite Pokémon Bought**\n{REPLY} {fav_s}",
        f"**Top Shinies Bought**\n{shiny_s}",
        f"**Top Gmaxes Bought**\n{gmax_s}",
        f"**Preferred Natures**\n{REPLY} {nat_s}",
        f"**IV Quality Bought**\n{REPLY} {iv_s}",
        f"**Monthly Spending**\n{monthly}",
    ]


def _page_user_selling(data: dict) -> list[str]:
    if not data["sold"].get("count"):
        return ["**🏷️ Selling Details**\n\n_No auctions listed._"]

    fav_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["fav_sells"]) or "—"

    best_s = "\n".join(
        f"{REPLY} {shiny_prefix(s)}`{s.get('pn', '?')}` — `{_fmt(s.get('bid', 0))}`"
        for s in data["best_sales"]
    ) or f"{REPLY} —"

    shiny_s = "\n".join(
        f"{REPLY} ✨**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["shiny_sold"]
    ) or f"{REPLY} —"

    gmax_s = "\n".join(
        f"{REPLY} ⚡**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["gmax_sold"]
    ) or f"{REPLY} —"

    nat_s   = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["natures_sold"]) or "—"
    iv      = data["iv_sold"]
    iv_s    = f"`{iv.get('avg', 0):.1f}%` avg  •  `{iv.get('max', 0):.2f}%` best" if iv else "—"
    monthly = "\n".join(
        f"{REPLY} `{r['_id']}` — `{_fmt(r['earned'])}` across `{r['count']:,}` sales"
        for r in data["monthly_earned"]
    ) or f"{REPLY} —"

    return [
        f"**Favourite Pokémon Sold**\n{REPLY} {fav_s}",
        f"**Best Sales**\n{best_s}",
        f"**Top Shinies Sold**\n{shiny_s}",
        f"**Top Gmaxes Sold**\n{gmax_s}",
        f"**Top Natures Sold**\n{REPLY} {nat_s}",
        f"**IV Quality Sold**\n{REPLY} {iv_s}",
        f"**Monthly Earnings**\n{monthly}",
    ]


# ═════════════════════════════════════════════════════════════════════════════
# USER STATS — VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _build_user_stats_view(
    user: discord.User | discord.Member,
    page: str = "overview",
    data: dict | None = None,
) -> discord.ui.LayoutView:

    if not data["won"].get("count") and not data["sold"].get("count"):
        return _error_view(f"❌ No auction activity found for **{user.display_name}**.")

    page_labels = {
        "overview": "👤 Overview",
        "buying":   "💸 Buying Details",
        "selling":  "🏷️ Selling Details",
    }
    page_sections = {
        "overview": _page_user_overview(data, user),
        "buying":   _page_user_buying(data),
        "selling":  _page_user_selling(data),
    }

    label       = page_labels.get(page, "👤 Overview")
    sections    = page_sections.get(page, page_sections["overview"])
    date_footer = _get_date_range_footer()

    class PageSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="Overview",        value="overview", emoji="👤", description="Summary of all activity"),
                discord.SelectOption(label="Buying Details",  value="buying",   emoji="💸", description="Detailed buying stats"),
                discord.SelectOption(label="Selling Details", value="selling",  emoji="🏷️", description="Detailed selling stats"),
            ]
            for o in options:
                if o.value == page:
                    o.default = True
            super().__init__(placeholder="Switch section…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                new_view = _build_user_stats_view(user, self.values[0], data)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in user stats select callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    comps: list = [
        discord.ui.TextDisplay(content=f"## 📊 {user.display_name} — {label}"),
        _sep(),
    ]
    comps += _interleave_seps(sections, final_sep=True)
    if date_footer:
        comps += [discord.ui.TextDisplay(content=f"-# {date_footer}"), _sep(False)]
    comps.append(discord.ui.ActionRow(PageSelect()))

    class StatsView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return StatsView()


# ═════════════════════════════════════════════════════════════════════════════
# MARKET INSIGHTS — DATA FETCHING
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_market_data() -> dict:
    agg = lambda pipe: list(_col.aggregate(pipe))  # noqa: E731

    total         = _col.count_documents({})
    shiny_count   = _col.count_documents(_FILTER_SHINY)
    gmax_count    = _col.count_documents(_FILTER_GMAX)
    normal_count  = total - shiny_count - gmax_count
    perfect_count = _col.count_documents({"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31})
    zero_count    = _col.count_documents({"hp": 0,  "atk": 0,  "def": 0,  "spa": 0,  "spd": 0,  "spe": 0})

    vol_res   = agg([{"$group": {"_id": None, "total": {"$sum": "$bid"}, "avg": {"$avg": "$bid"}}}])
    total_vol = vol_res[0]["total"] if vol_res else 0
    avg_price = vol_res[0]["avg"]   if vol_res else 0

    iv_res   = agg([{"$match": {"iv": {"$ne": None}}}, {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}])
    iv_stats = iv_res[0] if iv_res else {}

    busiest_res = agg([
        {"$match": {"ts": {"$exists": True}}},
        {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
        {"$group": {"_id": "$month", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": 1},
    ])
    busiest = busiest_res[0] if busiest_res else {}

    monthly_trend = agg([
        {"$match": {"ts": {"$exists": True}}},
        {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
        {"$group": {"_id": "$month", "count": {"$sum": 1}, "volume": {"$sum": "$bid"}}},
        {"$sort": {"_id": -1}}, {"$limit": 6},
    ])

    top_natures  = agg([{"$match": {"nat": {"$ne": None}}}, {"$group": {"_id": "$nat", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}])
    most_overall = agg([{"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}])
    most_normal  = agg([{"$match": {"sh": {"$ne": True}, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}])
    most_shiny   = agg([{"$match": _FILTER_SHINY}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 5}])
    most_gmax    = agg([{"$match": _FILTER_GMAX},  {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 5}])

    avg_normal = agg([{"$match": {"sh": {"$ne": True}, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "avg": {"$avg": "$bid"}, "count": {"$sum": 1}}}, {"$match": {"count": {"$gte": 5}}}, {"$sort": {"avg": -1}}, {"$limit": 5}])
    avg_shiny  = agg([{"$match": _FILTER_SHINY}, {"$group": {"_id": "$pn", "avg": {"$avg": "$bid"}, "count": {"$sum": 1}}}, {"$match": {"count": {"$gte": 3}}}, {"$sort": {"avg": -1}}, {"$limit": 5}])
    avg_gmax   = agg([{"$match": _FILTER_GMAX},  {"$group": {"_id": "$pn", "avg": {"$avg": "$bid"}, "count": {"$sum": 1}}}, {"$match": {"count": {"$gte": 3}}}, {"$sort": {"avg": -1}}, {"$limit": 5}])

    big_overall = list(_col.find({},            {"pn": 1, "bid": 1, "sh": 1, "gx": 1}).sort("bid", -1).limit(5))
    big_shiny   = list(_col.find(_FILTER_SHINY, {"pn": 1, "bid": 1, "sh": 1}).sort("bid", -1).limit(5))
    big_gmax    = list(_col.find(_FILTER_GMAX,  {"pn": 1, "bid": 1, "gx": 1}).sort("bid", -1).limit(5))
    rarest      = agg([{"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": 1}}, {"$limit": 5}])

    return {
        "total": total, "shiny_count": shiny_count, "gmax_count": gmax_count,
        "normal_count": normal_count, "perfect_count": perfect_count, "zero_count": zero_count,
        "total_vol": total_vol, "avg_price": avg_price, "iv_stats": iv_stats,
        "busiest": busiest, "monthly_trend": monthly_trend, "top_natures": top_natures,
        "most_overall": most_overall, "most_normal": most_normal,
        "most_shiny": most_shiny, "most_gmax": most_gmax,
        "avg_normal": avg_normal, "avg_shiny": avg_shiny, "avg_gmax": avg_gmax,
        "big_overall": big_overall, "big_shiny": big_shiny, "big_gmax": big_gmax,
        "rarest": rarest,
    }


# ═════════════════════════════════════════════════════════════════════════════
# MARKET INSIGHTS — PAGE RENDERERS
# ═════════════════════════════════════════════════════════════════════════════

def _trade_lines(rows: list, show_avg: bool = False) -> str:
    lines = []
    for i, r in enumerate(rows):
        line = f"{_medal(i)} **{r['_id']}** — `{r['count']:,}` auctions"
        if show_avg and r.get("avg"):
            line += f"  •  avg `{_fmt(r['avg'])}`"
        lines.append(line)
    return "\n".join(lines) or "_No data_"


def _avg_lines(rows: list) -> str:
    return "\n".join(
        f"{_medal(i)} **{r['_id']}** — avg `{_fmt(r['avg'])}` over `{r['count']:,}` sales"
        for i, r in enumerate(rows)
    ) or "_No data_"


def _big_lines(rows: list) -> str:
    return "\n".join(
        f"{_medal(i)} {shiny_prefix(r)}**{r.get('pn', '?')}** — `{_fmt(r.get('bid', 0))}`"
        for i, r in enumerate(rows)
    ) or "_No data_"


def _page_market_overview(d: dict) -> list[str]:
    iv   = d["iv_stats"]
    busy = d["busiest"]
    lines = [
        "**📈 Overview**",
        f"{REPLY} Total Auctions: `{d['total']:,}`  •  Total Volume: `{_fmt(d['total_vol'])}`",
        f"{REPLY} Avg Sale Price: `{_fmt(d['avg_price'])}`",
        f"{REPLY} Normal: `{d['normal_count']:,}`  •  Shiny: `{d['shiny_count']:,}`  •  Gmax: `{d['gmax_count']:,}`",
        f"{REPLY} Busiest Month: `{busy.get('_id', '?')}` — `{busy.get('count', 0):,}` auctions",
        f"{REPLY} Perfect IVs (6×31): `{d['perfect_count']:,}`  •  Zero IVs (6×0): `{d['zero_count']:,}`",
    ]
    if iv:
        lines.append(f"{REPLY} Avg IV %: `{iv.get('avg', 0):.1f}%`  •  Highest Ever: `{iv.get('max', 0):.2f}%`")

    trend = ["**📅 Monthly Trend** _(most recent first)_"] + [
        f"{REPLY} `{r['_id']}` — `{r['count']:,}` auctions  •  vol `{_fmt(r['volume'])}`"
        for r in d["monthly_trend"]
    ] or [f"{REPLY} —"]

    nats = "  ".join(f"**{r['_id']}** `{r['count']:,}`" for r in d["top_natures"]) or "—"
    return ["\n".join(lines), "\n".join(trend), f"**🌿 Most Common Natures**\n{REPLY} {nats}"]


def _page_market_traded(d: dict) -> list[str]:
    return [
        f"**🔥 Most Traded — Overall**\n\n{_trade_lines(d['most_overall'])}",
        f"**🔵 Most Traded — Normal**\n\n{_trade_lines(d['most_normal'])}",
        f"**✨ Most Traded — Shiny**\n\n{_trade_lines(d['most_shiny'], show_avg=True)}",
        f"**⚡ Most Traded — Gigantamax**\n\n{_trade_lines(d['most_gmax'], show_avg=True)}",
    ]


def _page_market_prices(d: dict) -> list[str]:
    return [
        f"**💎 Priciest on Average — Normal** _(min 5 sales)_\n\n{_avg_lines(d['avg_normal'])}",
        f"**💎 Priciest on Average — Shiny** _(min 3 sales)_\n\n{_avg_lines(d['avg_shiny'])}",
        f"**💎 Priciest on Average — Gmax** _(min 3 sales)_\n\n{_avg_lines(d['avg_gmax'])}",
        f"**💰 Biggest Single Sales — Overall**\n\n{_big_lines(d['big_overall'])}",
        f"**💰 Biggest Single Sales — Shiny**\n\n{_big_lines(d['big_shiny'])}",
        f"**💰 Biggest Single Sales — Gmax**\n\n{_big_lines(d['big_gmax'])}",
    ]


def _page_market_rarity(d: dict) -> list[str]:
    return [f"**🌙 Rarest Pokémon** _(fewest total sales)_\n\n{_trade_lines(d['rarest'])}"]


def _build_market_view(page: str = "overview", data: dict | None = None) -> discord.ui.LayoutView:
    if data is None:
        data = _fetch_market_data()

    pages = {
        "overview": ("📈 Overview & Trends",   _page_market_overview(data)),
        "traded":   ("🔥 Most Traded Pokémon", _page_market_traded(data)),
        "prices":   ("💎 Prices & Big Sales",  _page_market_prices(data)),
        "rarity":   ("🌙 Rarity",              _page_market_rarity(data)),
    }
    title, sections = pages.get(page, pages["overview"])
    date_footer = _get_date_range_footer()

    class PageSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="Overview & Trends",   value="overview", emoji="📈", description="Volume, breakdown, monthly trend"),
                discord.SelectOption(label="Most Traded Pokémon", value="traded",   emoji="🔥", description="Top traded overall/normal/shiny/gmax"),
                discord.SelectOption(label="Prices & Big Sales",  value="prices",   emoji="💎", description="Avg prices and record sales"),
                discord.SelectOption(label="Rarity",              value="rarity",   emoji="🌙", description="Rarest Pokémon by sale count"),
            ]
            for o in options:
                if o.value == page:
                    o.default = True
            super().__init__(placeholder="Switch section…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                new_view = _build_market_view(self.values[0], data)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in market select callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    comps: list = [
        discord.ui.TextDisplay(content=f"## 🌐 Auction Insights — {title}"),
        _sep(),
    ]
    comps += _interleave_seps(sections, final_sep=True)
    if date_footer:
        comps += [discord.ui.TextDisplay(content=f"-# {date_footer}"), _sep(False)]
    comps.append(discord.ui.ActionRow(PageSelect()))

    class MarketView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return MarketView()


# ═════════════════════════════════════════════════════════════════════════════
# HELP VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _build_help_view() -> discord.ui.LayoutView:
    text = "\n".join([
        "**Commands:**",
        f"{REPLY} `j!stats [@user]` — auction stats for yourself or another user",
        f"{REPLY} `j!lb sellers` — top sellers overall",
        f"{REPLY} `j!lb bidders` — top bidders overall",
        f"{REPLY} `j!lb shiny_sellers` — shiny sellers",
        f"{REPLY} `j!lb shiny_bidders` — shiny buyers",
        f"{REPLY} `j!lb gmax_sellers` — gmax sellers",
        f"{REPLY} `j!lb gmax_bidders` — gmax buyers",
        f"{REPLY} `j!lb pokemon [normal|shiny|gmax|overall]` — most auctioned Pokémon",
        f"{REPLY} `j!lb expensive` — biggest single sales ever",
        f"{REPLY} `j!market` — server-wide market insights",
        "",
        "**Three dropdowns on every leaderboard:**",
        f"{REPLY} **Leaderboard** — switch board type",
        f"{REPLY} **Time Period** — All Time / monthly",
        f"{REPLY} **Mode** — 💰 Money (earned/spent)  vs  🔢 Count (listed/won)",
        "　_(Mode is disabled on Pokémon and Biggest Sales boards)_",
        "",
        "**Examples:**",
        f"{REPLY} `j!lb shiny_sellers` → switch Mode to **Count** → who listed the most shinies",
        f"{REPLY} `j!lb gmax_bidders` → switch Mode to **Money** → who spent most on gmaxes",
    ])

    class HelpView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.TextDisplay(content="## 📊 Stats & Leaderboards — Help"),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
        def __init__(self):
            super().__init__(timeout=180)

    return HelpView()


# ═════════════════════════════════════════════════════════════════════════════
# COG
# ═════════════════════════════════════════════════════════════════════════════

class Stats(commands.Cog):
    """Auction statistics and leaderboards"""

    def __init__(self, bot: commands.Bot):
        self.bot     = bot
        self.periods = _period_options()
        self._refresh_cache_task.start()

    def cog_unload(self):
        self._refresh_cache_task.cancel()

    @tasks.loop(hours=6)
    async def _refresh_cache_task(self):
        log.info("Rebuilding leaderboard cache…")
        self.periods = _period_options()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _precompute_all_lb(self.periods))
        log.info("Leaderboard cache rebuilt.")

    @_refresh_cache_task.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()
        loop   = asyncio.get_event_loop()
        loaded = await loop.run_in_executor(None, _load_all_from_mongo)
        if loaded > 0:
            log.info("Leaderboard cache: loaded %d keys from MongoDB into memory.", loaded)
        else:
            log.info("Cold start: building leaderboard cache now…")
            await loop.run_in_executor(None, lambda: _precompute_all_lb(self.periods))

    @commands.hybrid_command(name="auction_stats", aliases=["stats"])
    @app_commands.describe(user="User to look up (leave empty for yourself)")
    async def stats_cmd(self, ctx: commands.Context, *, user: discord.Member | discord.User | str | None = None):
        """Show detailed auction stats for a user"""
        target: discord.User | discord.Member | None = None

        if isinstance(user, (discord.Member, discord.User)):
            # Slash command resolved the mention to a Member/User object directly
            target = user
        elif isinstance(user, str):
            # Prefix command — parse the raw string
            raw = user.strip().lstrip("<@!").rstrip(">")
            if raw.isdigit():
                uid = int(raw)
                target = ctx.guild.get_member(uid) if ctx.guild else None
                if target is None:
                    try:
                        target = await self.bot.fetch_user(uid)
                    except discord.NotFound:
                        await ctx.reply(
                            view=_error_view("❌ No user found with that ID."),
                            mention_author=False,
                        )
                        return
                    except discord.HTTPException:
                        await ctx.reply(
                            view=_error_view("❌ Something went wrong while looking up that user."),
                            mention_author=False,
                        )
                        return
            else:
                # treat as a username/display name — search guild members
                if ctx.guild:
                    target = discord.utils.find(
                        lambda m: m.name.lower() == raw.lower() or m.display_name.lower() == raw.lower(),
                        ctx.guild.members,
                    )
                if target is None:
                    await ctx.reply(
                        view=_error_view(f"❌ No user found matching `{raw}`."),
                        mention_author=False,
                    )
                    return

        if target is None:
            target = ctx.author

        async with ctx.typing():
            data = await _fetch_user_data(target.id)
            view = _build_user_stats_view(target, data=data)
        await ctx.reply(view=view, mention_author=False)

    @commands.hybrid_command(name="auction_leaderboard", aliases=["leaderboard"])
    @app_commands.describe(
        lb_type="sellers | bidders | shiny_sellers | shiny_bidders | gmax_sellers | gmax_bidders | pokemon | expensive",
        variant="For pokemon: normal | shiny | gmax | overall",
        mode="money (default) | count",
    )
    async def lb_cmd(self, ctx: commands.Context,
                     lb_type: str = "sellers",
                     variant: str = "overall",
                     mode: str = MODE_MONEY):
        """Show a leaderboard — use the three dropdowns to switch type, period and mode"""
        lb_type = lb_type.lower().strip()
        variant = variant.lower().strip()
        mode    = mode.lower().strip()

        valid_types = {
            "sellers", "bidders",
            "shiny_sellers", "shiny_bidders",
            "gmax_sellers",  "gmax_bidders",
            "pokemon", "expensive",
        }
        if lb_type not in valid_types:
            await ctx.reply(view=_build_help_view(), mention_author=False)
            return
        if variant not in {"normal", "shiny", "gmax", "overall"}:
            variant = "overall"
        if mode not in {MODE_MONEY, MODE_COUNT}:
            mode = MODE_MONEY

        default_period = self.periods[1]["value"]
        view = _build_lb_view(lb_type, variant, default_period, mode, ctx.author.id, self.periods)
        await ctx.reply(view=view, mention_author=False, allowed_mentions=SAFE_MENTIONS)

    @commands.hybrid_command(name="auction_insights", aliases=["market", "ai"])
    async def market_cmd(self, ctx: commands.Context):
        """Poketwo auction market insights"""
        async with ctx.typing():
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _fetch_market_data)
            view = _build_market_view(data=data)
        await ctx.reply(view=view, mention_author=False)


# ═════════════════════════════════════════════════════════════════════════════
# SETUP
# ═════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
