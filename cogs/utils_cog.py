"""
cogs/utils_cog.py – Miscellaneous useful commands.

Commands:
  j!srlb [spawnrate] [order] [count]  — leaderboard of Pokémon by avg auction price,
                                        filtered to a specific spawn-rate tier.
  j!srinfo <pokemon>                  — look up a single Pokémon's spawn rate + price stats.

pokemon_chances.csv columns:
  Dex, Pokemon, Chance, Chance percentage
  e.g.  63, Abra, 1/225, 0.4449%

Spawnrate tiers are the denominator of the Chance fraction (225, 450, 900, …).
j!srlb with no arguments lists every available tier.
"""
from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

import config
from config import REPLY
from utils import resolve_pokemon_name

log = logging.getLogger(__name__)

_mongo = MongoClient(config.MONGO_URI)
_db    = _mongo[config.MONGO_DB_NAME]
_col   = _db[config.MONGO_COLLECTION]

# ── Path to the spawn-rate data ───────────────────────────────────────────────
CHANCES_CSV = Path("data/pokemon_chances.csv")

SAFE_MENTIONS = discord.AllowedMentions.none()

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_ORDER = "expensive"
DEFAULT_COUNT = 5
MAX_COUNT     = 25   # hard ceiling so the embed doesn't blow up


# ═════════════════════════════════════════════════════════════════════════════
# CSV LOADER  (loaded once, cached in module-level dict)
# ═════════════════════════════════════════════════════════════════════════════

class SpawnRateDB:
    """
    Loads pokemon_chances.csv and provides:
      • all_tiers()              → sorted list of unique denominator ints
      • names_for_tier(denom)    → list of canonical Pokémon names
      • tier_for_name(name)      → (denom, chance_str, pct_str) | None
    """

    def __init__(self, path: Path):
        # denom (int) → list of {"name": str, "chance": str, "pct": str}
        self._tiers:   dict[int, list[dict]] = {}
        # normalised_name → (denom, chance_str, pct_str)
        self._by_name: dict[str, tuple[int, str, str]] = {}
        self._load(path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            log.warning("pokemon_chances.csv not found at %s", path)
            return

        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name    = row.get("Pokemon", "").strip()
                chance  = row.get("Chance", "").strip()   # e.g. "1/225"
                pct     = row.get("Chance percentage", "").strip()

                if not name or not chance or "/" not in chance:
                    continue

                try:
                    denom = int(chance.split("/")[1])
                except (ValueError, IndexError):
                    continue

                entry = {"name": name, "chance": chance, "pct": pct}
                self._tiers.setdefault(denom, []).append(entry)
                self._by_name[name.lower()] = (denom, chance, pct)

    # ── Public API ────────────────────────────────────────────────────────────

    def all_tiers(self) -> list[int]:
        return sorted(self._tiers.keys())

    def names_for_tier(self, denom: int) -> list[str]:
        return [e["name"] for e in self._tiers.get(denom, [])]

    def tier_for_name(self, name: str) -> tuple[int, str, str] | None:
        """Case-insensitive name lookup. Returns (denom, chance_str, pct_str) or None."""
        return self._by_name.get(name.lower())

    def fuzzy_find(self, query: str) -> list[str]:
        """Return canonical names whose lowercase contains the query string."""
        q = query.lower()
        return [n for n in self._by_name if q in n]

    def all_entries(self) -> dict[str, tuple[int, str, str]]:
        return dict(self._by_name)


_spawn_db: SpawnRateDB | None = None


def get_spawn_db() -> SpawnRateDB:
    global _spawn_db
    if _spawn_db is None:
        _spawn_db = SpawnRateDB(CHANCES_CSV)
    return _spawn_db


# ═════════════════════════════════════════════════════════════════════════════
# FORMATTING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _fmt(val: float) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}k"
    return f"{int(val):,}"


def _medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"`#{i + 1}`"


def _sep(visible: bool = True) -> discord.ui.Separator:
    return discord.ui.Separator(visible=visible, spacing=discord.SeparatorSpacing.small)


def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


# ═════════════════════════════════════════════════════════════════════════════
# MONGODB  — avg price lookup per Pokémon name list
# ═════════════════════════════════════════════════════════════════════════════

# How far back to look for price data — keeps results reflecting current market.
PRICE_LOOKBACK_MONTHS = 4


def _fetch_avg_prices(names: list[str]) -> dict[str, dict]:
    """
    For a list of canonical Pokémon names, return a dict:
      name → {"avg": float, "median": float, "p25": float, "p75": float,
              "count": int, "outliers_removed": int, "total_sales": int}

    Only shiny (non-gmax) auctions from the last PRICE_LOOKBACK_MONTHS are considered.

    Outlier removal (same IQR method as graph.py):
      fence = Q3 + 3.0 * IQR
      Bids above the fence are excluded from stats.
      If fewer than 3 clean bids remain, raw data is used as-is.
    """
    import numpy as np
    import time as _time
    from datetime import datetime, timezone

    if not names:
        return {}

    # Cutoff timestamp — only auctions from the last N months
    now       = datetime.now(timezone.utc)
    cut_month = now.month - PRICE_LOOKBACK_MONTHS
    cut_year  = now.year
    while cut_month <= 0:
        cut_month += 12
        cut_year  -= 1
    cutoff_ts = int(datetime(cut_year, cut_month, now.day, tzinfo=timezone.utc).timestamp())

    # Fetch every individual bid so we can run IQR filtering in Python
    pipe = [
        {"$match": {
            "pn":  {"$in": names},
            "sh":  True,
            "gx":  {"$ne": True},
            "bid": {"$exists": True},
            "ts":  {"$gte": cutoff_ts},
        }},
        {"$group": {
            "_id":  "$pn",
            "bids": {"$push": "$bid"},
        }},
    ]

    # Build a lowercase → original name map so we can do case-insensitive
    # matching between MongoDB pn values and the names we passed in.
    names_lower = {n.lower(): n for n in names}

    result = {}
    for r in _col.aggregate(pipe):
        raw_name = r["_id"]
        # Map back to the caller's casing (CSV casing) so dict keys are consistent
        name = names_lower.get(raw_name.lower(), raw_name)
        bids = r["bids"]
        if not bids:
            continue
        log.debug("_fetch_avg_prices: pn=%r  total_bids=%d  sample_max=%s",
                  raw_name, len(bids), max(bids))

        arr        = np.array(bids, dtype=float)
        q1, q3     = np.percentile(arr, 25), np.percentile(arr, 75)
        iqr        = q3 - q1
        fence      = q3 + 3.0 * iqr if iqr > 0 else arr.max()
        clean      = arr[arr <= fence]

        # Fall back to raw data if too few survive filtering
        if len(clean) < 3:
            clean = arr

        result[name] = {
            "avg":              float(clean.mean()),
            "median":           float(np.median(clean)),
            "p25":              float(np.percentile(clean, 25)),
            "p75":              float(np.percentile(clean, 75)),
            "count":            int(len(clean)),
            "outliers_removed": int(len(arr) - len(clean)),
            "total_sales":      int(len(arr)),
        }

    return result


# ═════════════════════════════════════════════════════════════════════════════
# TIERS LIST VIEW  (shown when j!srlb is called with no tier)
# ═════════════════════════════════════════════════════════════════════════════

def _build_tiers_view() -> discord.ui.LayoutView:
    db    = get_spawn_db()
    tiers = db.all_tiers()

    if not tiers:
        return _error_view("❌ `pokemon_chances.csv` not found or empty.")

    lines = []
    for denom in tiers:
        count = len(db.names_for_tier(denom))
        lines.append(f"{REPLY} `1/{denom}` — **{count}** Pokémon  •  `j!srlb {denom}`")

    text = (
        "## 🎲 Spawn Rate Tiers\n"
        "-# Use `j!srlb <rate>` to see that tier's price leaderboard.\n\n"
        + "\n".join(lines)
        + f"\n\n**Usage:**\n"
        f"{REPLY} `j!srlb <rate> [expensive|cheap] [count]`\n"
        f"{REPLY} `j!srlb 225` — priciest 1/225 Pokémon (default top 10)\n"
        f"{REPLY} `j!srlb 450 cheap 15` — cheapest 1/450 Pokémon, top 15\n"
        f"{REPLY} `j!srinfo <name>` — spawn rate + price stats for one Pokémon"
    )

    class TiersView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
        def __init__(self):
            super().__init__(timeout=120)

    return TiersView()


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _build_srlb_view(
    denom:   int,
    order:   str,
    count:   int,
    rows:    list[dict],   # pre-sorted, pre-sliced rows
    total_in_tier: int,
    total_with_data: int,
) -> discord.ui.LayoutView:
    """
    Build the Components-V2 leaderboard view for a spawn-rate tier.

    Each row: {"name": str, "avg": float, "count": int, "min": float, "max": float,
               "chance": str, "pct": str}
    """
    order_label = "💰 Most Expensive" if order == "expensive" else "🪙 Cheapest"
    title       = f"## 🎲 Spawn Rate `1/{denom}` — {order_label}"
    sub         = (
        f"-# Showing top `{len(rows)}` of `{total_with_data}` Pokémon with auction data "
        f"(`{total_in_tier}` total in this tier)  •  ✨ shiny  •  last {PRICE_LOOKBACK_MONTHS} months"
    )

    if not rows:
        body = "_No auction data found for any Pokémon in this tier._"
    else:
        lines = []
        for i, r in enumerate(rows):
            no_data = r["avg"] is None
            if no_data:
                lines.append(f"{_medal(i)} **{r['name']}** — _no auction data_")
            else:
                removed     = r.get("outliers_removed", 0)
                total_sales = r.get("total_sales", r["count"])
                outlier_s   = f"  •  _+{removed} ignored_" if removed else ""
                lines.append(
                    f"{_medal(i)} **{r['name']}**\n"
                    f"\u3000`{_fmt(r['median'])}` median  •  `{total_sales:,}` sales{outlier_s}"
                    f"\u3000typical `{_fmt(r['p25'])}` – `{_fmt(r['p75'])}`"
                )
        body = "\n".join(lines)

    # ── Dropdowns ─────────────────────────────────────────────────────────────

    db    = get_spawn_db()
    tiers = db.all_tiers()

    class TierSelect(discord.ui.Select):
        def __init__(self):
            options = []
            for t in tiers:
                opt = discord.SelectOption(
                    label=f"1/{t}",
                    value=str(t),
                    description=f"{len(db.names_for_tier(t))} Pokémon",
                    default=(t == denom),
                )
                options.append(opt)
            # Discord caps selects at 25 options — truncate gracefully
            super().__init__(
                placeholder="Switch spawn-rate tier…",
                options=options[:25],
            )

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                new_denom = int(self.values[0])
                new_view  = await _compute_and_build(new_denom, order, count)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("TierSelect callback error")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    class OrderSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(
                    label="Most Expensive", value="expensive", emoji="💰",
                    description="Highest avg price first",
                    default=(order == "expensive"),
                ),
                discord.SelectOption(
                    label="Cheapest", value="cheap", emoji="🪙",
                    description="Lowest avg price first",
                    default=(order == "cheap"),
                ),
            ]
            super().__init__(placeholder=f"Order: {order_label}", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                new_view = await _compute_and_build(denom, self.values[0], count)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("OrderSelect callback error")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    class CountSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(
                    label=f"Top {n}", value=str(n),
                    default=(n == count),
                )
                for n in [5, 10, 15, 20, 25]
            ]
            super().__init__(placeholder=f"Show: Top {count}", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                new_view = await _compute_and_build(denom, order, int(self.values[0]))
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("CountSelect callback error")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    # ── Assemble ──────────────────────────────────────────────────────────────

    comps: list = [
        discord.ui.TextDisplay(content=title),
        discord.ui.TextDisplay(content=sub),
        _sep(),
        discord.ui.TextDisplay(content=body),
        _sep(),
        discord.ui.ActionRow(TierSelect()),
        discord.ui.ActionRow(OrderSelect()),
        discord.ui.ActionRow(CountSelect()),
    ]

    class SrlbView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return SrlbView()


async def _compute_and_build(
    denom: int,
    order: str,
    count: int,
) -> discord.ui.LayoutView:
    """
    Fetch avg prices from Mongo (in executor so we don't block the event loop),
    sort, slice, and return the leaderboard view.
    """
    db    = get_spawn_db()
    names = db.names_for_tier(denom)

    if not names:
        return _error_view(f"❌ No Pokémon found for spawn rate `1/{denom}`.")

    loop       = asyncio.get_event_loop()
    price_data = await loop.run_in_executor(None, lambda: _fetch_avg_prices(names))

    # Build rows — include Pokémon even if they have no data, place them last
    rows_with_data    = []
    rows_without_data = []

    for name in names:
        info = price_data.get(name)
        if info:
            entry = db.tier_for_name(name)
            chance_str = entry[1] if entry else "?"
            pct_str    = entry[2] if entry else "?"
            rows_with_data.append({
                "name":             name,
                "avg":              info["avg"],
                "median":           info["median"],
                "p25":              info["p25"],
                "p75":              info["p75"],
                "count":            info["count"],
                "outliers_removed": info.get("outliers_removed", 0),
                "total_sales":      info.get("total_sales", info["count"]),
                "chance":           chance_str,
                "pct":              pct_str,
            })
        else:
            rows_without_data.append({
                "name":             name,
                "avg":              None,
                "median":           None,
                "p25":              None,
                "p75":              None,
                "count":            0,
                "outliers_removed": 0,
                "total_sales":      0,
                "chance":           "?",
                "pct":              "?",
            })

    # Sort by median price
    reverse = (order == "expensive")
    rows_with_data.sort(key=lambda r: r["median"], reverse=reverse)

    total_in_tier    = len(names)
    total_with_data  = len(rows_with_data)

    # Combine: data rows first (sorted), no-data rows appended at the bottom
    all_rows = rows_with_data + rows_without_data
    clamped  = min(max(1, count), MAX_COUNT)
    sliced   = all_rows[:clamped]

    return _build_srlb_view(denom, order, clamped, sliced, total_in_tier, total_with_data)


# ═════════════════════════════════════════════════════════════════════════════
# SRINFO VIEW  — single Pokémon lookup
# ═════════════════════════════════════════════════════════════════════════════

def _build_srinfo_view(name: str, denom: int, chance: str, pct: str,
                       price_data: dict | None,
                       alias: str | None = None) -> discord.ui.LayoutView:
    tier_mons  = get_spawn_db().names_for_tier(denom)
    alias_line = f"\n{REPLY} _Resolved from: `{alias}`_" if alias else ""

    spawn_block = (
        f"**🎲 Spawn Rate — {name}**{alias_line}\n"
        f"{REPLY} Rate: `{chance}`  ({pct})\n"
        f"{REPLY} Tier: `1/{denom}` — shared with `{len(tier_mons)}` Pokémon\n"
        f"{REPLY} Use `j!srlb {denom}` to see the full tier leaderboard"
    )
    if price_data:
        removed     = price_data.get("outliers_removed", 0)
        total_sales = price_data.get("total_sales", price_data["count"])
        outlier_s   = f"  _(+{removed} extreme sale{'s' if removed > 1 else ''} ignored)_" if removed else ""
        price_block = (
            f"**💰 Shiny Auction Prices** _(✨ shiny  •  last {PRICE_LOOKBACK_MONTHS} months)_\n"
            f"{REPLY} Median: `{_fmt(price_data['median'])}`{outlier_s}\n"
            f"{REPLY} Typical range: `{_fmt(price_data['p25'])}` – `{_fmt(price_data['p75'])}` _(middle 50% of sales)_\n"
            f"{REPLY} Total sales: `{total_sales:,}`"
        )
    else:
        price_block = f"**💰 Shiny Auction Prices**\n{REPLY} _No shiny auction data found for last {PRICE_LOOKBACK_MONTHS} months._"

    comps = [
        discord.ui.TextDisplay(content=spawn_block),
        _sep(),
        discord.ui.TextDisplay(content=price_block),
    ]

    class SrInfoView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=120)

    return SrInfoView()


# ═════════════════════════════════════════════════════════════════════════════
# COG
# ═════════════════════════════════════════════════════════════════════════════

class UtilsCog(commands.Cog, name="Utils"):
    """Miscellaneous useful commands"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Warm up the CSV on load so first command is instant
        get_spawn_db()

    # ──────────────────────────────────────────────────────────────────────────
    # j!srlb  [spawnrate] [order] [count]
    # ──────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="srlb", aliases=["spawnlb", "spawnratelb"])
    @app_commands.describe(
        spawnrate="Spawn rate denominator, e.g. 225 for 1/225. Leave blank to list all tiers.",
        order="expensive (default) | cheap",
        count="Number of Pokémon to show (default 10, max 25)",
    )
    async def srlb_cmd(
        self,
        ctx: commands.Context,
        spawnrate: int | None = None,
        order: str = DEFAULT_ORDER,
        count: int = DEFAULT_COUNT,
    ):
        """
        Leaderboard of Pokémon ranked by avg auction price within a spawn-rate tier.

        Examples:
          j!srlb            — list all available spawn-rate tiers
          j!srlb 225        — top 10 most expensive 1/225 Pokémon
          j!srlb 450 cheap  — cheapest 1/450 Pokémon
          j!srlb 900 expensive 20 — top 20 priciest 1/900 Pokémon
        """
        # No tier given → show tier list
        if spawnrate is None:
            await ctx.reply(view=_build_tiers_view(), mention_author=False)
            return

        # Validate order
        order = order.lower().strip()
        if order not in ("expensive", "cheap"):
            order = DEFAULT_ORDER

        # Clamp count
        count = max(1, min(count, MAX_COUNT))

        # Check tier exists
        db = get_spawn_db()
        if not db.all_tiers():
            await ctx.reply(
                view=_error_view("❌ `pokemon_chances.csv` not found. Please check `data/pokemon_chances.csv`."),
                mention_author=False,
            )
            return

        if spawnrate not in db.all_tiers():
            tiers_s = ", ".join(f"`1/{t}`" for t in db.all_tiers())
            await ctx.reply(
                view=_error_view(
                    f"❌ Unknown spawn rate `1/{spawnrate}`.\n"
                    f"{REPLY} Available tiers: {tiers_s}"
                ),
                mention_author=False,
            )
            return

        async with ctx.typing():
            view = await _compute_and_build(spawnrate, order, count)

        await ctx.reply(view=view, mention_author=False, allowed_mentions=SAFE_MENTIONS)

    # ──────────────────────────────────────────────────────────────────────────
    # j!srinfo  <pokemon name>
    # ──────────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="srinfo", aliases=["spawninfo","sr","spawnrate"])
    @app_commands.describe(name="Pokémon name to look up")
    async def srinfo_cmd(self, ctx: commands.Context, *, name: str = ""):
        """
        Show the spawn rate and avg auction price for a single Pokémon.

        Example:  j!srinfo Abra
        """
        if not name:
            await ctx.reply(
                view=_error_view(f"❌ Usage: `j!srinfo <pokemon name>`\nExample: `j!srinfo Abra`"),
                mention_author=False,
            )
            return

        db         = get_spawn_db()
        raw_input  = name.strip()

        # Resolve any alias / other-language name to canonical English first
        resolved = resolve_pokemon_name(raw_input)
        lookup   = resolved if resolved else raw_input

        result = db.tier_for_name(lookup)

        if result is None:
            matches = db.fuzzy_find(lookup)
            if not matches and resolved:
                matches = db.fuzzy_find(raw_input)
            if matches:
                suggestions = ", ".join(f"**{m.title()}**" for m in matches[:5])
                await ctx.reply(
                    view=_error_view(
                        f"❌ `{raw_input}` not found in spawn rate data.\n"
                        f"{REPLY} Did you mean: {suggestions}?"
                    ),
                    mention_author=False,
                )
            else:
                resolved_note = f"\n{REPLY} Resolved to: `{resolved}`" if resolved and resolved.lower() != raw_input.lower() else ""
                await ctx.reply(
                    view=_error_view(
                        f"❌ `{raw_input}` not found in spawn rate data.{resolved_note}\n"
                        f"{REPLY} This Pokémon may not be wild-spawnable."
                    ),
                    mention_author=False,
                )
            return

        denom, chance_str, pct_str = result

        # Recover properly-cased name from the CSV using the resolved lookup name
        canonical = lookup
        for entry_name in db.names_for_tier(denom):
            if entry_name.lower() == lookup.lower():
                canonical = entry_name
                break

        # Fetch price data using the correctly-cased canonical name
        loop           = asyncio.get_event_loop()
        price_data_map = await loop.run_in_executor(None, lambda: _fetch_avg_prices([canonical]))
        price_data     = price_data_map.get(canonical)

        # If still no match, try a case-insensitive scan of the result keys
        # (handles any remaining casing mismatch between CSV and MongoDB pn field)
        if price_data is None and price_data_map:
            for key, val in price_data_map.items():
                if key.lower() == canonical.lower():
                    price_data = val
                    break

        # Show alias note if user typed a different name than the canonical one
        alias_note = raw_input if raw_input.lower() != canonical.lower() else None
        view = _build_srinfo_view(canonical, denom, chance_str, pct_str, price_data, alias_note)
        await ctx.reply(view=view, mention_author=False, allowed_mentions=SAFE_MENTIONS)


# ═════════════════════════════════════════════════════════════════════════════
# SETUP
# ═════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(UtilsCog(bot))
