"""
bot.bot
Bot factory — creates and configures the Tansen bot instance.
"""

import os
import logging

import discord
from discord.ext import commands

from database import init_db, migrate, connect, DB_LOCK
from bot.audio import lavalink
from bot.ui.nowplaying import NowPlayingView

logger = logging.getLogger("tansen")


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True

    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ── on_ready ─────────────────────────────────────────────────────

    @bot.event
    async def on_ready():
        logger.info("Bot ready. Logged in as %s (%s)", bot.user, bot.user.id)

        # Register persistent views
        try:
            bot.add_view(NowPlayingView())
            logger.info("Registered persistent NowPlayingView.")
        except Exception:
            logger.exception("Failed to register persistent view")

        # Clear stale queues from prior session
        try:
            with DB_LOCK:
                with connect() as conn:
                    conn.execute("DELETE FROM queues")
                    conn.commit()
            logger.info("Cleared all guild queues on startup.")
        except Exception:
            logger.exception("Failed to clear queues on startup")

        # Connect to Lavalink
        try:
            await lavalink.connect(bot)
        except Exception:
            logger.exception("Lavalink connection failed — music commands will not work")

        # Load cogs
        try:
            await bot.load_extension("bot.cogs.music")
            logger.info("Loaded music cog.")
        except Exception:
            logger.exception("Failed to load music cog")

        # Sync slash commands
        try:
            synced = await bot.tree.sync()
            logger.info("Synced %d slash commands.", len(synced))
        except Exception:
            logger.exception("Failed to sync command tree")

    # ── Voice state: re-deafen ───────────────────────────────────────

    @bot.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.id != bot.user.id:
            return
        if before.self_deaf and not after.self_deaf:
            try:
                channel = after.channel or before.channel
                if channel:
                    await member.guild.change_voice_state(channel=channel, self_deaf=True)
            except Exception:
                logger.exception("Failed to re-deafen")

    # ── Global app command error handler ─────────────────────────────

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        logger.exception("App command error: %s", error)
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(f"Error: {error}", ephemeral=True)
            except Exception:
                pass

    return bot
