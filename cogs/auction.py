"""
cogs/auction.py – Auction search and info using hybrid_group (prefix + slash).

Field mapping (DB short name → meaning):
  mid  = message_id          aid  = auction_id
  ts   = unix_timestamp      pn   = pokemon_name
  lv   = level               sh   = shiny
  gx   = gmax                nat  = nature
  gen  = gender              hi   = held_item
  xp   = xp                  iv   = total_iv_percent
  hp   = iv_hp               atk  = iv_attack
  def  = iv_defense          spa  = iv_sp_atk
  spd  = iv_sp_def           spe  = iv_speed
  mv   = moves               bid  = winning_bid
  bdr  = bidder_id           sn   = seller_name
  sid  = seller_id

--name behaviour in THIS cog:
  Resolves to the canonical English name, then expands to ALL canonical names
  that share the same dex number (forms, variants, gigantamax, etc.).
  e.g. --name bulbasaur  →  matches Bulbasaur AND Ivysaur AND Venusaur AND
                             Mega Venusaur AND Gigantamax Venusaur
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

import config
from config import get_gender_emoji, REPLY
from filters import all_flags_help
from filters import FLAG_DEFINITIONS
from categories import list_categories
from utils import (
    build_query, format_date, iv_line,
    format_winning_bid, format_winning_bid_long,
    shiny_prefix, get_pokemon_image_url,
    resolve_pokemon_name, get_forms_db,
)

# ─── Name flag aliases (derived from filters.py — stays in sync automatically) ─
_NAME_FLAGS: frozenset[str] = frozenset(
    ["--name"] + FLAG_DEFINITIONS["--name"].get("aliases", [])
)

# ─── DB connection ─────────────────────────────────────────────────────────────
_mongo = MongoClient(config.MONGO_URI)
_db    = _mongo[config.MONGO_DB_NAME]
_col   = _db[config.MONGO_COLLECTION]

# ─── Message URL template ──────────────────────────────────────────────────────
_MSG_URL_TEMPLATE = "https://discord.com/channels/716390832034414685/766198531626106941/{mid}"

SAFE_MENTIONS = discord.AllowedMentions.none()


def _build_message_url(record: dict) -> str | None:
    mid = record.get("mid")
    if not mid:
        return None
    return _MSG_URL_TEMPLATE.format(mid=mid)


# ─────────────────────────────────────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH – single result line
# ─────────────────────────────────────────────────────────────────────────────

def _result_line(r: dict) -> str:
    auction_id = r.get("aid", "?")
    name       = r.get("pn") or "Unknown"
    level      = r.get("lv")
    level_s    = f"L{level}" if level is not None else "L???"
    shiny      = shiny_prefix(r)
    gender     = get_gender_emoji(r.get("gen"))
    iv         = r.get("iv")
    iv_s       = f"{iv:.2f}%" if iv is not None else "???%"
    bid_s      = format_winning_bid(r)
    date_s     = format_date(r)

    return (
        f"`#{auction_id}` {shiny}**{level_s} {name}** {gender}"
        f"　•　{iv_s}　•　`{bid_s}`　•　{date_s}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FILTERS VIEW  (paginated, ephemeral — opened by the 📋 All Filters button)
# ─────────────────────────────────────────────────────────────────────────────

# Pages are defined as static groups so each fits within Discord's char limit.
_FILTER_PAGES: list[tuple[str, list[str]]] = [
    (
        "🔤 Name / Identity",
        [
            "`--name <value>`  _--n / --pokemon / --poke_ — Pokémon name. Expands to all forms with the same dex number in search.",
            "`--gender <value>`  _--sex / --g_ — Gender: `male`, `female`, `unknown`",
            "`--nature <value>`  _-nat / --nat_ — Nature (case-insensitive, e.g. `timid`)",
            "`--shiny`  _--sh / --shinys_ — Shiny Pokémon only",
            "`--gmax`  _--gigantamax / --gm / --giga_ — Gigantamax only",
            "`--evo <value>`  _--evolution / --family / --fam_ — All Pokémon in the same evo family",
            "`--region <value>`  _--r / --reg_ — Filter by region (e.g. `kanto`, `galar`)",
            "`--type <value>`  _--t / --types_ — Filter by type. Stackable up to 2 (e.g. `--type fire --type flying`)",
        ],
    ),
    (
        "📊 IVs",
        [
            "`--iv <value>`  _--totaliv / --iv%_ — Total IV % (e.g. `90`, `>90`, `>=85.5`)",
            "`--hpiv <value>`  _--hp / --ivhp_ — HP IV",
            "`--atkiv <value>`  _--atk / --ivatk / --attack_ — Attack IV",
            "`--defiv <value>`  _--def / --ivdef / --defense_ — Defense IV",
            "`--spatkiv <value>`  _--spatk / --spa / --sp_atk_ — Sp. Attack IV",
            "`--spdefiv <value>`  _--spdef / --spd / --sp_def_ — Sp. Defense IV",
            "`--spdiv <value>`  _--spe / --speed / --speediv_ — Speed IV",
            "─",
            "**Multi-IV count filters** — at least N IVs equal a value:",
            "`--triple <value>`  _--three / --tri_ — At least 3 IVs equal this (e.g. `--triple 31`)",
            "`--quadruple <value>`  _--quad / --four_ — At least 4 IVs equal this",
            "`--pentuple <value>`  _--penta / --five_ — At least 5 IVs equal this",
            "`--hextuple <value>`  _--hex / --six_ — All 6 IVs equal this (e.g. `--hex 31` for perfect)",
            "─",
            "**Operator syntax** for all numeric fields:",
            "`31` · `>30` · `>=30` · `<100` · `<=100` · `30-100`",
        ],
    ),
    (
        "💰 Price / Sort / Misc",
        [
            "`--price <value>`  _--p / --bid_ — Price filter (e.g. `5000`, `>5000`, `500-5000`)",
            "`--minprice <value>`  _--minbid_ — Min price shorthand (same as `--price >=N`)",
            "`--maxprice <value>`  _--maxbid_ — Max price shorthand (same as `--price <=N`)",
            "`--seller <value>`  _--se / --soldby_ — @mention or ID for exact match; text matches name",
            "`--bidder <value>`  _--b / --buyer / --wonby_ — Bidder Discord @mention or ID",
            "`--move <value>`  _-m / --moves_ — Has this move. Stackable.",
            "`--limit <value>`  _--lim / --max / --top_ — Cap results to N most recent",
            "`--sort <value>`  _--orderby / --order_ — Sort order:",
            "　`iv+` / `iv-`  ·  `bid+` / `bid-`  ·  `level+` / `level-`",
            "　`date+` / `date-` _(default)_  ·  `id+` / `id-`",
        ],
    ),
    (
        "🚫 Exclude / Category filters",
        [
            "`--category <value>`  _--c / --cat / --group_ — Filter by category (see next page for list)",
            "`--noshiny`  _--nonshiny / --excludeshiny / --nosh_ — Exclude shiny Pokémon",
            "`--nogmax`  _--nongmax / --excludegmax / --nogm_ — Exclude Gigantamax Pokémon",
            "─",
            "**`--exclude`** (aliases: `--ex / --not / --no / --except / --without`)",
            "Stackable. Syntax: `--ex <kind> <value>`",
            "`--ex name <name>` — exclude one exact Pokémon",
            "`--ex evo <name>` — exclude entire evo family",
            "`--ex type <type>` — exclude all Pokémon of a type",
            "`--ex region <region>` — exclude all Pokémon from a region",
            "`--ex category <cat>` — exclude a whole category (e.g. `--ex category event`)",
            "─",
            "**Examples:**",
            "`--category legendaries --noshiny` — legends, no shinies",
            "`--ex category event --ex type ghost` — no events, no ghost-types",
            "`--nogmax --price <5000` — no Gmax, under 5 000 coins",
            "`--triple 31 --sort bid-` — at least 3×31 IV, priciest first",
            "`--name pikachu --shiny --iv >90` — shiny high-IV Pikachus",
        ],
    ),
]


def create_filters_view(page: int = 0) -> discord.ui.LayoutView:
    """Paginated ephemeral view shown when the 📋 All Filters button is clicked."""
    from categories import list_categories

    # Build the categories page dynamically so new cats appear automatically
    cat_lines: list[str] = []
    for c in list_categories():
        shortcuts = " / ".join(f"`--{a}`" for a in c["aliases"][:3])
        shortcut_part = f"  _{shortcuts}_" if shortcuts else ""
        cat_lines.append(f"`--category {c['key']}`{shortcut_part} — {c['name']}")

    pages: list[tuple[str, list[str]]] = list(_FILTER_PAGES) + [
        ("📦 Categories", cat_lines)
    ]

    TOTAL = len(pages)
    page  = max(0, min(page, TOTAL - 1))

    title, lines = pages[page]
    page_label   = f"Page {page + 1}/{TOTAL}"

    class PrevBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="◀ Prev",
                custom_id="f_prev",
                disabled=(page == 0),
            )
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.edit_message(view=create_filters_view(page - 1))

    class NextBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="Next ▶",
                custom_id="f_next",
                disabled=(page >= TOTAL - 1),
            )
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.edit_message(view=create_filters_view(page + 1))

    class FiltersView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.TextDisplay(content=f"**📋 {title}** — _{page_label}_"),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content="\n".join(lines)),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(PrevBtn(), NextBtn()),
            accent_colour=config.EMBED_COLOR,
        )
        def __init__(self):
            super().__init__(timeout=180)

    return FiltersView()


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH VIEW  (factory, paginated)
# ─────────────────────────────────────────────────────────────────────────────

def create_search_view(
    user_id: int,
    query: dict,
    sort: list,
    total: int,
    query_str: str,
    current_page: int = 0,
    limit: int | None = None,
    raw_tokens: list[str] | None = None,
) -> discord.ui.LayoutView:

    # Effective total respects --limit
    effective_total = min(total, limit) if limit is not None else total
    max_page        = max(0, (effective_total - 1) // config.RESULTS_PER_PAGE)
    skip            = current_page * config.RESULTS_PER_PAGE

    cursor  = _col.find(query).sort(sort).skip(skip)
    # Apply limit so we never fetch beyond the requested cap
    if limit is not None:
        remaining = max(0, limit - skip)
        cursor = cursor.limit(min(config.RESULTS_PER_PAGE, remaining))
    else:
        cursor = cursor.limit(config.RESULTS_PER_PAGE)

    results      = list(cursor)
    start        = skip + 1
    end          = skip + len(results)
    lines        = [_result_line(r) for r in results]
    results_text = "\n".join(lines) if lines else "_No results._"
    header_text  = f"**🔍 Auction Search** — _{query_str}_"
    limit_note   = f"  •  capped at {limit:,}" if limit is not None else ""
    footer_text  = (
        f"Showing {start}–{end} of {effective_total:,}{limit_note}  •  "
        f"Page {current_page + 1}/{max_page + 1}"
    )

    class PrevBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="◀ Prev",
                custom_id="s_prev",
                disabled=(current_page == 0),
            )
        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != user_id:
                await interaction.response.send_message(
                    view=_error_view("❌ Not your search!"), ephemeral=True)
                return
            await interaction.response.defer()
            new_view = create_search_view(user_id, query, sort, total, query_str, current_page - 1, limit)
            await interaction.edit_original_response(view=new_view)

    class NextBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="Next ▶",
                custom_id="s_next",
                disabled=(current_page >= max_page),
            )
        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != user_id:
                await interaction.response.send_message(
                    view=_error_view("❌ Not your search!"), ephemeral=True)
                return
            await interaction.response.defer()
            new_view = create_search_view(user_id, query, sort, total, query_str, current_page + 1, limit, raw_tokens)
            await interaction.edit_original_response(view=new_view)

    # ── Helper: rebuild view with modified token list ──────────────────────────
    _tokens = raw_tokens or []

    def _has_flag(tokens: list[str], *flags: str) -> bool:
        """Check if any of the given flags exist in the token list."""
        return any(t.lower() in flags for t in tokens)

    def _remove_flag(tokens: list[str], *flags: str) -> list[str]:
        """Remove a boolean flag from the token list."""
        return [t for t in tokens if t.lower() not in flags]

    def _add_flag(tokens: list[str], flag: str) -> list[str]:
        """Append a boolean flag to the token list."""
        return tokens + [flag]

    def _remove_flag_with_arg(tokens: list[str], flag: str, arg_value: str) -> list[str]:
        """Remove --flag <arg_value> pair (case-insensitive) from token list."""
        result, i = [], 0
        flag_l, arg_l = flag.lower(), arg_value.lower()
        while i < len(tokens):
            if tokens[i].lower() == flag_l and i + 1 < len(tokens) and tokens[i + 1].lower() == arg_l:
                i += 2  # skip flag + its arg
            else:
                result.append(tokens[i])
                i += 1
        return result

    def _has_flag_with_arg(tokens: list[str], flag: str, arg_value: str) -> bool:
        """Check if --flag <arg_value> exists in token list."""
        flag_l, arg_l = flag.lower(), arg_value.lower()
        for i in range(len(tokens) - 1):
            if tokens[i].lower() == flag_l and tokens[i + 1].lower() == arg_l:
                return True
        return False

    def _tokens_to_query_str(tokens: list[str]) -> str:
        return " ".join(tokens) if tokens else "All auctions"

    def _rebuild_view(new_tokens: list[str]) -> discord.ui.LayoutView:
        new_query, new_sort, new_limit = build_query(new_tokens, expand_name_by_dex=True)
        new_total = _col.count_documents(new_query)
        new_qstr  = _tokens_to_query_str(new_tokens)
        if new_total == 0:
            return _error_view("❌ No auctions found after applying that filter.")
        return create_search_view(user_id, new_query, new_sort, new_total, new_qstr, 0, new_limit, new_tokens)

    # ── Shiny was explicitly requested (--shiny / --sh / --shinys) ─────────────
    _SHINY_FLAGS = frozenset(["--shiny", "--sh", "--shinys"])
    _NOSHINY_FLAGS = frozenset(["--noshiny", "--nonshiny", "--excludeshiny", "--nosh"])
    _NOGMAX_FLAGS  = frozenset(["--nogmax", "--nongmax", "--excludegmax", "--nogm"])

    _shiny_requested     = _has_flag(_tokens, *_SHINY_FLAGS)
    _noshiny_active      = _has_flag(_tokens, *_NOSHINY_FLAGS)
    _nogmax_active       = _has_flag(_tokens, *_NOGMAX_FLAGS)

    # Check for "--ex category event" (all --exclude aliases + category aliases)
    _EX_ALIASES_SET  = frozenset(["--exclude", "--ex", "--not", "--no", "--except", "--without"])
    _CAT_KINDS_SET   = frozenset(["category", "cat", "group"])

    def _has_ex_category_event(tokens: list[str]) -> bool:
        for i in range(len(tokens) - 2):
            if (tokens[i].lower() in _EX_ALIASES_SET
                    and tokens[i+1].lower() in _CAT_KINDS_SET
                    and tokens[i+2].lower() == "event"):
                return True
        return False

    _no_event_active = _has_ex_category_event(_tokens)

    # ── Filter buttons ─────────────────────────────────────────────────────────

    class AllFiltersBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.primary,
                label="📋 All Filters",
                custom_id="s_allfilters",
            )
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.send_message(
                view=create_filters_view(page=0), ephemeral=True
            )

    class ExcludeShinyBtn(discord.ui.Button):
        def __init__(self):
            # Active = currently excluding shinies (button shows as green/on)
            # Disabled when shiny was specifically requested (--sh/--shiny)
            active = _noshiny_active
            super().__init__(
                style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary,
                label="✨ Exclude Shiny" if not active else "✨ Shiny Excluded",
                custom_id="s_exshiny",
                disabled=_shiny_requested,
            )
        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != user_id:
                await interaction.response.send_message(
                    view=_error_view("❌ Not your search!"), ephemeral=True)
                return
            await interaction.response.defer()
            if _noshiny_active:
                new_tokens = _remove_flag(list(_tokens), *_NOSHINY_FLAGS)
            else:
                new_tokens = _add_flag(list(_tokens), "--noshiny")
            await interaction.edit_original_response(view=_rebuild_view(new_tokens))

    class ExcludeGmaxBtn(discord.ui.Button):
        def __init__(self):
            active = _nogmax_active
            super().__init__(
                style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary,
                label="⚡ Gmax Excluded" if active else "⚡ Exclude Gmax",
                custom_id="s_exgmax",
            )
        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != user_id:
                await interaction.response.send_message(
                    view=_error_view("❌ Not your search!"), ephemeral=True)
                return
            await interaction.response.defer()
            if _nogmax_active:
                new_tokens = _remove_flag(list(_tokens), *_NOGMAX_FLAGS)
            else:
                new_tokens = _add_flag(list(_tokens), "--nogmax")
            await interaction.edit_original_response(view=_rebuild_view(new_tokens))

    class ExcludeEventBtn(discord.ui.Button):
        def __init__(self):
            active = _no_event_active
            super().__init__(
                style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary,
                label="🎉 Event Excluded" if active else "🎉 Exclude Event",
                custom_id="s_exevent",
            )
        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != user_id:
                await interaction.response.send_message(
                    view=_error_view("❌ Not your search!"), ephemeral=True)
                return
            await interaction.response.defer()
            if _no_event_active:
                # Remove the "--ex category event" triplet
                result, i = [], 0
                toks = list(_tokens)
                while i < len(toks):
                    if (toks[i].lower() in _EX_ALIASES_SET
                            and i + 2 < len(toks)
                            and toks[i+1].lower() in _CAT_KINDS_SET
                            and toks[i+2].lower() == "event"):
                        i += 3
                    else:
                        result.append(toks[i])
                        i += 1
                new_tokens = result
            else:
                new_tokens = list(_tokens) + ["--ex", "category", "event"]
            await interaction.edit_original_response(view=_rebuild_view(new_tokens))

    # Shiny check: only True (not {"$ne": True}) means shiny search
    accent    = config.SHINY_EMBED_COLOR if query.get("sh") is True else config.EMBED_COLOR
    has_pages = effective_total > config.RESULTS_PER_PAGE

    inner: list = [
        discord.ui.TextDisplay(content=header_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=results_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=f"_{footer_text}_"),
    ]
    if has_pages:
        inner += [
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(PrevBtn(), NextBtn()),
        ]
    # Separator + filter buttons — always shown under prev/next
    inner += [
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.ActionRow(
            AllFiltersBtn(),
            ExcludeShinyBtn(),
            ExcludeGmaxBtn(),
            ExcludeEventBtn(),
        ),
    ]

    class SearchView(discord.ui.LayoutView):
        container = discord.ui.Container(*inner, accent_colour=accent)
        def __init__(self):
            super().__init__(timeout=180)

    return SearchView()


# ─────────────────────────────────────────────────────────────────────────────
# INFO VIEW
# ─────────────────────────────────────────────────────────────────────────────

def create_info_view(record: dict) -> discord.ui.LayoutView:
    name       = record.get("pn") or "Unknown"
    shiny      = record.get("sh", False)
    level      = record.get("lv")
    gender     = get_gender_emoji(record.get("gen"))
    nature     = record.get("nat") or "???"
    xp         = record.get("xp", "???")
    held       = record.get("hi") or "None"
    auction_id = record.get("aid", "?")
    bid_s      = format_winning_bid_long(record)
    bidder_id  = record.get("bdr")
    seller     = record.get("sn") or "Unknown"
    seller_id  = record.get("sid")
    date_s     = format_date(record)
    iv_tot     = record.get("iv")
    iv_tot_s   = f"{iv_tot:.2f}%" if iv_tot is not None else "???%"
    moves      = record.get("mv") or []
    msg_url    = _build_message_url(record)
    level_s    = str(level) if level is not None else "???"
    shiny_s    = shiny_prefix(record)
    img_url    = get_pokemon_image_url(name, shiny)
    accent     = config.SHINY_EMBED_COLOR if shiny else config.EMBED_COLOR
    bidder_s   = (f"<@{bidder_id}> (`{bidder_id}`)" if bidder_id else "Unknown")
    seller_s   = (f"`{seller}`" if not seller_id else f"<@{seller_id}> (`{seller_id}`) (`{seller}`)")

    basic_text = (
        f"**📋 Basic Info**\n"
        f"{REPLY} **Name:** {shiny_s}{name}\n"
        f"{REPLY} **Level:** `{level_s}`\n"
        f"{REPLY} **Gender:** {gender}\n"
        f"{REPLY} **XP:** `{xp}`\n"
        f"{REPLY} **Held Item:** `{held}`\n"
        f"{REPLY} **Nature:** `{nature}`"
    )

    auction_text = (
        f"**💰 Auction Info**\n"
        f"{REPLY} **Winning Bid:** `{bid_s}`\n"
        f"{REPLY} **Bidder:** {bidder_s}\n"
        f"{REPLY} **Seller:** {seller_s}\n"
        f"{REPLY} **Ended On:** `{date_s}`"
    )

    def _iv_val(v) -> str:
        return str(int(v)) if v is not None else "?"

    iv_text = (
        f"**📊 IVs** — `{iv_tot_s}` total\n"
        + iv_line("HP",  record.get("hp"))   + "\n"
        + iv_line("ATK", record.get("atk"))  + "\n"
        + iv_line("DEF", record.get("def"))  + "\n"
        + iv_line("SpA", record.get("spa"))  + "\n"
        + iv_line("SpD", record.get("spd"))  + "\n"
        + iv_line("Spe", record.get("spe"))
    )

    stats_line = (
        f"`Hp-{_iv_val(record.get('hp'))}"
        f"/Atk-{_iv_val(record.get('atk'))}"
        f"/Def-{_iv_val(record.get('def'))}"
        f"/SpA-{_iv_val(record.get('spa'))}"
        f"/SpD-{_iv_val(record.get('spd'))}"
        f"/Spe-{_iv_val(record.get('spe'))}"
        f"/IV {iv_tot_s}`"
    )

    moves_text = (
        "**⚔️ Moves**\n"
        + ("\n".join(f"{REPLY} {m}" for m in moves) if moves else "_None_")
    )

    comps: list = [
        discord.ui.TextDisplay(content=f"## [SOLD] {shiny_s}Auction #{auction_id}"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
    ]

    if img_url:
        comps.append(discord.ui.Section(
            discord.ui.TextDisplay(content=basic_text),
            accessory=discord.ui.Thumbnail(media=img_url),
        ))
    else:
        comps.append(discord.ui.TextDisplay(content=basic_text))

    comps += [
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=auction_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=iv_text),
        discord.ui.TextDisplay(content=stats_line),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=moves_text),
    ]
    if msg_url:
        comps += [
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label="🔗 View Auction Log",
                    url=msg_url,
                )
            ),
        ]

    class InfoView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=accent)
        def __init__(self):
            super().__init__(timeout=300)

    return InfoView()


# ─────────────────────────────────────────────────────────────────────────────
# HELP VIEW
# ─────────────────────────────────────────────────────────────────────────────

def create_help_view(page: int = 0) -> discord.ui.LayoutView:
    from categories import list_categories

    flag_lines = []
    for f in all_flags_help():
        arg_s   = " <value>" if f["takes_arg"] else ""
        aliases = ", ".join(f["aliases"][:3]) if f["aliases"] else ""
        flag_lines.append(f"{REPLY} `{f['flag']}{arg_s}` — {f['help']}")
        if aliases:
            flag_lines.append(f"　_aliases: {aliases}_")

    cat_lines = []
    for c in list_categories():
        aliases_s = ", ".join(c["aliases"][:4])
        cat_lines.append(
            f"{REPLY} `{c['key']}` **{c['name']}** — _{aliases_s}_  "
            f"_(also: `--{c['key']}`"
            + (f", `--{c['aliases'][0]}`" if c["aliases"] else "")
            + ")_"
        )

    examples = (
        f"{REPLY} `j!a s --name Alcremie --gmax`\n"
        f"{REPLY} `j!a s --name pikachu --shiny --iv >90`\n"
        f"{REPLY} `j!a s --atkiv 31 --spdiv 31 --sort price`\n"
        f"{REPLY} `j!a s --evo bulbasaur`\n"
        f"{REPLY} `j!a s --category starters --iv >=85`\n"
        f"{REPLY} `j!a s --starters --iv >=85`\n"
        f"{REPLY} `j!a s --type fire --type flying`\n"
        f"{REPLY} `j!a s --region galar --shiny`\n"
        f"{REPLY} `j!a s --move fake out --level >50`\n"
        f"{REPLY} `j!a s --seller @user`\n"
        f"{REPLY} `j!a s --name goomy --limit 10`\n"
        f"{REPLY} `j!a i 1544762`"
    )

    CHUNK_SIZE = 15
    flag_chunks = [flag_lines[i:i+CHUNK_SIZE] for i in range(0, len(flag_lines), CHUNK_SIZE)]
    max_page = len(flag_chunks) - 1

    page = max(0, min(page, max_page + 1))

    TOTAL_PAGES = max_page + 2

    class PrevBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="◀ Prev",
                custom_id="h_prev",
                disabled=(page == 0),
            )
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.edit_message(view=create_help_view(page - 1))

    class NextBtn(discord.ui.Button):
        def __init__(self):
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="Next ▶",
                custom_id="h_next",
                disabled=(page >= TOTAL_PAGES - 1),
            )
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.edit_message(view=create_help_view(page + 1))

    header = discord.ui.Container(
        discord.ui.TextDisplay(content="## 📖 Auction Bot — Help"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=(
            "**Commands:**\n"
            f"{REPLY} `j!a s [flags]` or `/auction search` — search auctions\n"
            f"{REPLY} `j!a i <id>` or `/auction info` — full auction info"
        )),
        accent_colour=config.EMBED_COLOR,
    )

    if page <= max_page:
        chunk = flag_chunks[page]
        page_label = f"Page {page + 1}/{TOTAL_PAGES}"
        body = discord.ui.Container(
            discord.ui.TextDisplay(
                content=f"**🔍 Filters** _{page_label}_\n" + "\n".join(chunk)
            ),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(PrevBtn(), NextBtn()),
            accent_colour=config.EMBED_COLOR,
        )
    else:
        page_label = f"Page {page + 1}/{TOTAL_PAGES}"
        body = discord.ui.Container(
            discord.ui.TextDisplay(
                content=f"**📦 Categories (`--category` or shortcut)** _{page_label}_\n" + "\n".join(cat_lines)
            ),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content="**💡 Examples:**\n" + examples),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(PrevBtn(), NextBtn()),
            accent_colour=config.EMBED_COLOR,
        )

    class HelpView(discord.ui.LayoutView):
        c1 = header
        c2 = body
        def __init__(self):
            super().__init__(timeout=180)

    return HelpView()


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class Auction(commands.Cog):
    """Pokémon auction search and info – Components V2"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_group(name="auction", aliases=["a"], invoke_without_command=True)
    async def auction_group(self, ctx: commands.Context):
        """Pokémon auction commands"""
        await ctx.send(view=create_help_view())

    @auction_group.command(name="search", aliases=["s"])
    @app_commands.describe(filters="Filters e.g: --n pikachu --nosh --nogm --iv >90 --sort bid")
    async def auction_search(self, ctx: commands.Context, *, filters: str = ""):
        """Search past auctions with filters"""
        raw = filters.split() if filters else []

        # ── Validate --name values before building the query ──────────────────
        # Walk tokens, collect every value that follows a --name flag, and check
        # it resolves to a known Pokémon.  Unknown names get an immediate error.
        #
        # Special handling for --ex / --exclude:
        #   --ex takes TWO argument tokens: <kind> <value>
        #   e.g. "--ex name meowth" — "name" and "meowth" are both arguments,
        #   NOT flag names.  We must skip both so "name" isn't validated as a
        #   Pokémon name.  The known kind keywords are:
        #   name, evo, evolution, type, region, reg, category, cat, group
        _EXCLUDE_FLAGS = frozenset(
            ["--exclude"] + [a for a in
                __import__("filters").FLAG_DEFINITIONS["--exclude"].get("aliases", [])]
        )
        _EX_KIND_TOKENS = frozenset({
            "name", "evo", "evolution", "type", "region", "reg",
            "category", "cat", "group",
        })

        i = 0
        while i < len(raw):
            tok = raw[i]

            # Skip --ex <kind> <value> entirely — both argument tokens are
            # consumed here so neither gets mistaken for a Pokémon name below.
            if tok.lower() in _EXCLUDE_FLAGS:
                i += 1  # skip the flag itself
                if i < len(raw) and raw[i].lower() in _EX_KIND_TOKENS:
                    i += 1  # skip the kind token (e.g. "name", "type")
                # Skip the value tokens until the next flag
                while i < len(raw) and not raw[i].startswith("-"):
                    i += 1
                continue

            if tok.lower() in _NAME_FLAGS:
                # Consume the value tokens (everything until the next flag)
                i += 1
                name_parts: list[str] = []
                while i < len(raw) and not raw[i].startswith("-"):
                    name_parts.append(raw[i])
                    i += 1
                name_val = " ".join(name_parts).strip()
                # Strip trailing "only" keyword that build_query handles
                check_val = name_val
                if check_val.lower().endswith(" only"):
                    check_val = check_val[:-5].strip()
                if check_val:
                    # Valid if FormsDB knows it OR the name DB resolves it
                    forms_hit = bool(get_forms_db().resolve_name_to_forms(check_val))
                    name_hit  = bool(resolve_pokemon_name(check_val))
                    if not forms_hit and not name_hit:
                        await ctx.send(
                            view=_error_view(
                                f"❌ **{check_val}** is not a Pokémon name.\n"
                                f"{config.REPLY} Check the spelling or try the English name."
                            ),
                            reference=ctx.message,
                            mention_author=False,
                        )
                        return
            else:
                i += 1

        # expand_name_by_dex=True: --name bulbasaur matches all Bulbasaur dex-number forms
        query, sort, limit   = build_query(raw, expand_name_by_dex=True)
        total                = _col.count_documents(query)

        if total == 0:
            await ctx.send(view=_error_view("❌ No auctions found matching your filters."))
            return

        query_str = filters.strip() or "All auctions"
        await ctx.send(
            view=create_search_view(ctx.author.id, query, sort, total, query_str, 0, limit, raw),
            allowed_mentions=SAFE_MENTIONS
        )

    @auction_group.command(name="info", aliases=["i"])
    @app_commands.describe(auction_id="The auction ID number")
    async def auction_info(self, ctx: commands.Context, auction_id: str = ""):
        """View full details of a specific auction"""
        if not auction_id:
            await ctx.send(
                view=_error_view("❌ Usage: `j!a i <auction_id>`"),
                reference=ctx.message,
                mention_author=False,
            )
            return
        try:
            aid = int(auction_id)
        except ValueError:
            await ctx.send(
                view=_error_view("❌ Invalid auction ID — must be a number."),
                reference=ctx.message,
                mention_author=False,
            )
            return

        record = _col.find_one({"aid": aid})
        if not record:
            await ctx.send(
                view=_error_view(f"❌ Auction `#{aid}` not found."),
                reference=ctx.message,
                mention_author=False,
            )
            return

        await ctx.send(
            view=create_info_view(record),
            reference=ctx.message,
            mention_author=False,
            allowed_mentions=SAFE_MENTIONS,
        )

    @auction_group.command(name="help", aliases=["h"])
    async def auction_help(self, ctx: commands.Context):
        """Show all available filters and examples"""
        await ctx.send(view=create_help_view())


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Auction(bot))
