"""
cogs/usage_logger.py – Batched command usage reporter.

Tracks every command invocation and sends a formatted report to a designated
log channel once every BATCH_SIZE uses. No timer — only fires on threshold.

Setup:
  1. Set LOG_CHANNEL_ID below to your target channel's ID.
  2. Add "cogs.usage_logger" to COGS in main.py.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config

log = logging.getLogger("usage_logger")

# ── Config ────────────────────────────────────────────────────────────────────
# ID of the Discord channel where batch reports are sent.
LOG_CHANNEL_ID: int = 1510518266442027148  # ← Replace with your channel ID

# How many command uses to collect before sending a report.
BATCH_SIZE: int = 5
# ─────────────────────────────────────────────────────────────────────────────


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M UTC")


class UsageLogger(commands.Cog):
    """Batched command usage logger — sends a report every BATCH_SIZE uses."""

    def __init__(self, bot: commands.Bot):
        self.bot  = bot
        self._buf: deque[dict] = deque()

    # ── Listener ──────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        """Fires after every successful prefix-command invocation."""
        if ctx.author.bot:
            return

        guild_name   = ctx.guild.name   if ctx.guild   else "DM"
        guild_id     = ctx.guild.id     if ctx.guild   else "—"
        channel_name = ctx.channel.name if hasattr(ctx.channel, "name") else "DM"
        channel_id   = ctx.channel.id

        self._buf.append({
            "ts":           _now_str(),
            "user":         str(ctx.author),
            "user_id":      ctx.author.id,
            "command":      ctx.message.content[:120],   # raw invocation, truncated
            "command_name": ctx.command.qualified_name if ctx.command else "?",
            "guild":        guild_name,
            "guild_id":     guild_id,
            "channel":      channel_name,
            "channel_id":   channel_id,
        })

        if len(self._buf) >= BATCH_SIZE:
            await self._flush()

    # ── Flush ─────────────────────────────────────────────────────────────────

    async def _flush(self):
        """Drain the buffer and send a formatted report to the log channel."""
        if not self._buf:
            return

        channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(LOG_CHANNEL_ID)
            except Exception as e:
                log.error(f"Usage logger: cannot reach channel {LOG_CHANNEL_ID}: {e}")
                self._buf.clear()
                return

        entries = list(self._buf)
        self._buf.clear()

        # ── Build the embed ────────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"📊 Command Usage — last {len(entries)} use(s)",
            colour=discord.Colour(0x7b2fff),
            timestamp=datetime.now(timezone.utc),
        )

        lines = []
        for i, e in enumerate(entries, 1):
            line = (
                f"**{i}.** `{e['command_name']}`\n"
                f"  👤 {discord.utils.escape_markdown(e['user'])} (`{e['user_id']}`)\n"
                f"  🏠 {discord.utils.escape_markdown(e['guild'])} (`{e['guild_id']}`)\n"
                f"  💬 #{discord.utils.escape_markdown(e['channel'])} (`{e['channel_id']}`)\n"
                f"  📝 `{discord.utils.escape_markdown(e['command'])}`\n"
                f"  🕐 {e['ts']}"
            )
            lines.append(line)

        # Discord embed field values cap at 1024 chars; split into fields if needed.
        chunk: list[str] = []
        chunk_len = 0
        field_idx = 1

        for line in lines:
            if chunk_len + len(line) + 1 > 1000:
                embed.add_field(
                    name=f"Entries (part {field_idx})",
                    value="\n\n".join(chunk),
                    inline=False,
                )
                chunk = []
                chunk_len = 0
                field_idx += 1
            chunk.append(line)
            chunk_len += len(line) + 1

        if chunk:
            name = "Entries" if field_idx == 1 else f"Entries (part {field_idx})"
            embed.add_field(name=name, value="\n\n".join(chunk), inline=False)

        embed.set_footer(text=f"Batch of {len(entries)}  •  threshold: every {BATCH_SIZE} uses")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.error(f"Usage logger: missing Send Messages / Embed Links in channel {LOG_CHANNEL_ID}")
        except discord.HTTPException as e:
            log.error(f"Usage logger: failed to send report: {e}")


async def setup(bot: commands.Bot):
    if not LOG_CHANNEL_ID:
        log.warning(
            "usage_logger: LOG_CHANNEL_ID is not set (still 0). "
            "Set it in cogs/usage_logger.py before loading this cog."
        )
    await bot.add_cog(UsageLogger(bot))
