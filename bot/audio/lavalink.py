"""
bot.audio.lavalink
Lavalink / Wavelink node connection setup.

Reads LAVALINK_URI and LAVALINK_PASSWORD from environment variables
and exposes a single ``connect`` coroutine called during on_ready.
"""

import os
import logging

import wavelink
from discord.ext import commands

logger = logging.getLogger("tansen.lavalink")

DEFAULT_URI = "http://lavalink:2333"
DEFAULT_PASSWORD = "youshallnotpass"


async def connect(bot: commands.Bot) -> None:
    """Create a Wavelink node and connect it to the bot.

    Call this once in ``on_ready`` (idempotent — skips if already connected).
    """
    uri = os.getenv("LAVALINK_URI", DEFAULT_URI)
    password = os.getenv("LAVALINK_PASSWORD", DEFAULT_PASSWORD)

    nodes = [
        wavelink.Node(
            uri=uri,
            password=password,
        )
    ]

    try:
        await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=100)
        logger.info("Wavelink connected to Lavalink at %s", uri)
    except Exception:
        logger.exception("Failed to connect Wavelink to Lavalink at %s", uri)
        raise
