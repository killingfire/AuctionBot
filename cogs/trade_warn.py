"""
cogs/trade_evo_warn.py – Listens to Pokétwo trade embeds and warns users
about Pokémon that evolve via trading.

Watches for:
  • New messages from the Pokétwo bot containing a trade embed
  • Edits to those same messages (new Pokémon may appear after an edit)

Pokémon that trigger a warning:
  Alolan Graveler, Karrablast, Pumpkaboo, Graveler, Phantump,
  Kadabra, Machoke, Haunter, Boldore, Gurdurr, Shelmet

Parsing rules:
  • Names are wrapped in **bold** in the embed field values.
  • Only custom Discord emoji (<:name:id>) and leading emoji/sparkle characters
    (e.g. ✨) are stripped — the rest of the name is kept verbatim.
  • "Gigantamax Meowth" stays "Gigantamax Meowth" after cleaning.
  • Matching is exact and case-insensitive against the full cleaned name.
  • "Gigantamax Haunter" will NOT match "Haunter" — only exact names trigger.

The warning is sent as a reply to the trade message and auto-deletes after
15 seconds so it doesn't clutter the channel.
"""
from __future__ import annotations

import re
import logging
from collections import OrderedDict

import discord
from discord.ext import commands

import config

log = logging.getLogger(__name__)

# ── Pokétwo bot user ID ────────────────────────────────────────────────────────
POKETWO_ID = 716390085896962058

# ── Trade-evolution Pokémon (canonical, lowercase for matching) ────────────────
TRADE_EVO_POKEMON: frozenset[str] = frozenset({
    "alolan graveler",
    "karrablast",
    "pumpkaboo",
    "graveler",
    "phantump",
    "kadabra",
    "machoke",
    "haunter",
    "boldore",
    "gurdurr",
    "shelmet",
    "electabuzz",     
    "poliwhirl",    
    "porygon", 
    "spritzee", 
    "slowpoke",        
    "porygon2",      
    "dusclops",        
    "clamperl",        
    "scyther",       
    "swirlix",        
    "rhydon",      
    "seadra",      
    "magmar",       
    "onix",
})

# ── What each Pokémon evolves into when traded ─────────────────────────────────
EVOLVES_INTO: dict[str, str] = {
    "alolan graveler": "Alolan Golem",
    "karrablast":      "Escavalier",
    "pumpkaboo":       "Gourgeist",
    "graveler":        "Golem",
    "phantump":        "Trevenant",
    "kadabra":         "Alakazam",
    "machoke":         "Machamp",
    "haunter":         "Gengar",
    "boldore":         "Gigalith",
    "gurdurr":         "Conkeldurr",
    "shelmet":         "Accelgor",
    "electabuzz":       "Electivire (while holding Electirizer)",
    "poliwhirl":      "Politoed (while holding King's Rock)",
    "porygon":       "Porygon2 (while holding Upgrade)",
    "slowpoke":        "Slowking (while holding King's Rock)",
    "porygon2":        "Porygon-z (while holding Dubious Disc)",
    "dusclops":         "Dusknoir (while holding Reaper Cloth)",
    "clamperl":         "Gorebyss (while holding Deep Sea Scale) or into Huntail (while holding Deep Sea Tooth)",
    "scyther":         "Scizor (while holding Metal Coat)",
    "swirlix":         "Slurpuff (while holding Whipped Dream)",
    "rhydon":        "Rhyperior (while holding Protector)",
    "seadra":        "Kingdra (while holding Dragon Scale)",
    "magmar":         "Magmortar (while holding Magmarizer)",
    "spritzee":         "Aromatisse (while holding Sachet)",
    "onix":         "Steelix (while holding Metal Coat)",
    
}

# ── Regex to extract bold names from embed field values ───────────────────────
# Matches text between **…** (non-greedy, single line)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Strip custom Discord emoji like <:name:123456> or <a:name:123456>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:[^:]+:\d+>")

# Strip any unicode emoji characters (covers ✨ and all standard emoji)
# We use a broad range that covers all emoji blocks
_UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # misc symbols, emoticons, transport, etc.
    "\U00002600-\U000027BF"   # misc symbols
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0001F900-\U0001F9FF"   # supplemental symbols
    "\u2702-\u27B0"
    "✨"
    "]+"
)


def _clean_name(raw: str) -> str:
    """
    Remove custom Discord emoji and unicode emoji characters from the bold text,
    then strip surrounding whitespace.

    Examples:
      "✨ Droopy Tatsugiri"              → "Droopy Tatsugiri"
      "<:_:1242455099213877248> Gigantamax Meowth" → "Gigantamax Meowth"
      "Karrablast"                        → "Karrablast"
      "✨ Gurdurr"                        → "Gurdurr"
    """
    s = _CUSTOM_EMOJI_RE.sub("", raw)
    s = _UNICODE_EMOJI_RE.sub("", s)
    return s.strip()


def _extract_names_from_field(value: str) -> list[str]:
    """Return cleaned Pokémon names from all **bold** tokens in an embed field."""
    names: list[str] = []
    for m in _BOLD_RE.finditer(value):
        cleaned = _clean_name(m.group(1))
        if cleaned:
            names.append(cleaned)
    return names


def _find_trade_evo_pokemon(
    embed: discord.Embed,
) -> dict[str, list[str]]:
    """
    Scan all fields of a trade embed.
    Returns a dict:  field_name (trader) → [list of matched trade-evo names]

    Matching is EXACT (case-insensitive) against the full cleaned name.
    "Gigantamax Meowth" will never match "Meowth".
    "✨ Gurdurr" cleans to "Gurdurr" and matches.
    """
    found: dict[str, list[str]] = {}

    for field in embed.fields:
        trader_name = field.name or "Unknown Trader"
        hits: list[str] = []

        for name in _extract_names_from_field(field.value or ""):
            if name.lower() in TRADE_EVO_POKEMON:
                hits.append(name)

        if hits:
            found[trader_name] = hits

    return found


def _is_trade_embed(embed: discord.Embed) -> bool:
    """Return True if this embed looks like a Pokétwo trade embed."""
    title = embed.title or ""
    return title.lower().startswith("trade between")


def _build_warning_view(
    found: dict[str, list[str]],
    already_warned: set[str],
) -> discord.ui.LayoutView | None:
    """
    Build a Components-V2 warning view.
    Only includes Pokémon not already warned about (to avoid duplicate lines
    when processing edits).
    Returns None if there's nothing new to warn about.
    """
    lines: list[str] = []

    for trader, names in found.items():
        # Strip the red circle prefix Pokétwo uses: "🔴 Username" → "Username"
        display = re.sub(r"^[🔴🟢🟡⚪]\s*", "", trader).strip()

        for name in names:
            key = f"{display}:{name.lower()}"
            if key in already_warned:
                continue
            already_warned.add(key)
            evolves_to = EVOLVES_INTO.get(name.lower(), "another form")
            lines.append(
                f"⚠️ **{display}** is offering **{name}** — "
                f"this will evolve into **{evolves_to}** upon trade!\n"
                f"　-# If this is intentional, ignore this warning."
            )

    if not lines:
        return None

    warning_text = (
        "## 🔔 Trade Evolution Warning\n"
        "-# One or more Pokémon in this trade will **evolve** when traded.\n\n"
        + "\n\n".join(lines)
        + "\n\n-# This message will auto-delete in 15 seconds."
    )

    class WarnView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.TextDisplay(content=warning_text),
            accent_colour=discord.Colour.from_str("#fe9ac9"),
        )
        def __init__(self):
            super().__init__(timeout=90)

    return WarnView()


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class TradeEvoWarn(commands.Cog):
    """Warns users about trade-evolution Pokémon in Pokétwo trade windows."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # message_id → set of "trader:pokemon" keys already warned about.
        # Capped at 500 entries (oldest evicted first) to prevent unbounded growth.
        self._warned: OrderedDict[int, set[str]] = OrderedDict()
        self._WARNED_MAX = 500

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _process_trade_message(
        self,
        message: discord.Message,
        *,
        is_edit: bool = False,
    ) -> None:
        """
        Core logic: scan trade embed fields and send a warning if needed.
        `is_edit=True` suppresses duplicate warnings for already-warned Pokémon.
        """
        if message.author.id != POKETWO_ID:
            return

        trade_embed: discord.Embed | None = None
        for embed in message.embeds:
            if _is_trade_embed(embed):
                trade_embed = embed
                break

        if trade_embed is None:
            return

        found = _find_trade_evo_pokemon(trade_embed)
        if not found:
            return

        # Retrieve (or create) the already-warned set for this message
        if message.id not in self._warned:
            if len(self._warned) >= self._WARNED_MAX:
                self._warned.popitem(last=False)  # evict oldest
            self._warned[message.id] = set()
        already_warned = self._warned[message.id]

        view = _build_warning_view(found, already_warned)
        if view is None:
            return  # Nothing new to warn about

        label = "edit" if is_edit else "new trade"
        log.info(
            "Trade evo warning triggered (%s) in channel %s (message %s)",
            label, message.channel.id, message.id,
        )

        try:
            sent = await message.reply(
                view=view,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Schedule auto-delete after 15 s
            self.bot.loop.call_later(15, self.bot.loop.create_task, _try_delete(sent))
        except discord.HTTPException as e:
            log.warning("Failed to send trade evo warning: %s", e)

    # ── Events ────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._process_trade_message(message, is_edit=False)

    @commands.Cog.listener()
    async def on_message_edit(
        self,
        before: discord.Message,
        after: discord.Message,
    ) -> None:
        # Only care if the embed content actually changed
        if before.embeds == after.embeds:
            return
        await self._process_trade_message(after, is_edit=True)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        """Clean up tracking data when a trade message is deleted."""
        self._warned.pop(message.id, None)


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-DELETE HELPER
# ─────────────────────────────────────────────────────────────────────────────

async def _try_delete(message: discord.Message) -> None:
    try:
        await message.delete()
    except discord.NotFound:
        pass  # Already deleted — that's fine
    except discord.HTTPException as e:
        log.warning("Could not auto-delete trade evo warning: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(TradeEvoWarn(bot))
