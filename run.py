"""
run.py
Tansen bot entry point.

Usage:
    python run.py
"""

import os
import logging

from dotenv import load_dotenv
load_dotenv()

from database import init_db, migrate

# Ensure DB is initialized
try:
    init_db()
    migrate()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
)
logger = logging.getLogger("tansen")
logger.info("=== TANSEN BOT STARTUP ===")
logger.info("Using NEW architecture: run.py -> bot/bot.py")
logger.info("Canonical metadata enforcement: ENABLED")

# Optional keep-alive server
if os.getenv("KEEP_ALIVE", "false").lower() in ("1", "true", "yes"):
    try:
        from keep_alive import start_keep_alive
        start_keep_alive()
        logger.info("Started keep-alive web thread.")
    except Exception:
        logger.exception("Failed to start keep-alive")

# Create and run bot
from bot.bot import create_bot

bot = create_bot()

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DCTOKEN")
if not TOKEN:
    logger.error("Set DISCORD_TOKEN or DCTOKEN environment variable.")
    raise SystemExit(1)

bot.run(TOKEN)
