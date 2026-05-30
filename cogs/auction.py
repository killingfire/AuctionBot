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
            await interaction.response.edit_message(
                view=create_search_view(user_id, query, sort, total, query_str, current_page - 1, limit))

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
            await interaction.response.edit_message(
                view=create_search_view(user_id, query, sort, total, query_str, current_page + 1, limit))

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
    @app_commands.describe(filters="Filters e.g: --name pikachu --shiny --iv >90 --sort price")
    async def auction_search(self, ctx: commands.Context, *, filters: str = ""):
        """Search past auctions with filters"""
        raw = filters.split() if filters else []

        # ── Validate --name values before building the query ──────────────────
        # Walk tokens, collect every value that follows a --name flag, and check
        # it resolves to a known Pokémon.  Unknown names get an immediate error.
        i = 0
        while i < len(raw):
            tok = raw[i]
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
            view=create_search_view(ctx.author.id, query, sort, total, query_str, 0, limit),
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
