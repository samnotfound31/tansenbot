"""
bot.providers.soundcloud
SoundCloud search via Wavelink / Lavalink.

This module is the raw SoundCloud interface — it searches and resolves URLs.
Ranking/scoring is handled externally by the search_engine + ranking modules.
"""

import logging
from typing import List, Optional

import wavelink

from bot.utils.cache import search_cache
from bot.providers.playback_ranking import normalize

logger = logging.getLogger("tansen.soundcloud")

# Maximum results to fetch from Lavalink per search
MAX_SEARCH_RESULTS = 15


async def search(query: str, *, max_results: int = MAX_SEARCH_RESULTS) -> List[wavelink.Playable]:
    """Search SoundCloud via Lavalink and return raw (unranked) results."""
    cache_key = f"sc_raw:{normalize(query)}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        tracks: List[wavelink.Playable] = await wavelink.Playable.search(
            query, source=wavelink.TrackSource.SoundCloud
        )
    except wavelink.LavalinkLoadException as exc:
        logger.warning("Lavalink search failed for '%s': %s", query, exc)
        return []
    except Exception:
        logger.exception("Unexpected error during SoundCloud search")
        return []

    results = tracks[:max_results] if tracks else []
    if results:
        search_cache.set(cache_key, results, ttl=1800)
    return results


def is_soundcloud_url(url: str) -> bool:
    """Check if a URL is a SoundCloud link."""
    if not url:
        return False
    lower = url.lower().strip()
    return "soundcloud.com/" in lower or "snd.sc/" in lower


async def resolve_url(url: str) -> Optional[wavelink.Playable]:
    """Resolve a direct SoundCloud URL to a playable track."""
    try:
        tracks = await wavelink.Playable.search(url)
        if tracks:
            return tracks[0]
    except Exception:
        logger.exception("Failed to resolve SoundCloud URL: %s", url)
    return None
