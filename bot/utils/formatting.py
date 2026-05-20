"""
bot.utils.formatting
Shared formatting helpers used across the bot.
"""

import logging
import random
from typing import Dict, Any, List, Optional

logger = logging.getLogger("tansen.formatting")


def format_mmss(seconds: Optional[int]) -> str:
    if seconds is None:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def make_progress_bar(elapsed: float, total: float, width: int = 18) -> str:
    if not total or total <= 0:
        return "\u2500" * width
    ratio = min(1.0, elapsed / total)
    pos = int(ratio * width)
    bar = "\u2500" * pos + "\u25cf" + "\u2500" * (width - pos)
    return bar


def make_equalizer(paused: bool = False) -> str:
    if paused:
        return "\u2584 \u2584 \u2584 \u2584 \u2584 \u2584 \u2584 \u2584 \u2584 \u2584"
    chars = ["\u2581", "\u2582", "\u2583", "\u2584", "\u2585", "\u2586", "\u2587", "\u2588"]
    return " ".join(random.choice(chars) for _ in range(10))


def format_song_line(song: Dict[str, Any], idx: Optional[int] = None) -> str:
    # Use playback metadata (fully SoundCloud-native) as primary for UI display
    title = song.get("playback_title") or song.get("title") or "Untitled"
    author = song.get("playback_author") or song.get("author") or ""
    author_str = f" \u2014 {author}" if author else ""
    by = f" \u2022 requested by {song.get('requester')}" if song.get("requester") else ""
    head = f"[{idx}] " if idx is not None else ""

    # Debug logging for metadata source tracking
    has_canonical = bool(song.get("canonical_title"))
    if has_canonical:
        logger.debug(
            "[formatting] Queue line using playback metadata (stored canonical available: title='%s', artist='%s')",
            song.get("canonical_title"),
            song.get("canonical_primary_artist"),
        )

    return f"{head}{title}{author_str}{by}"


def truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"
