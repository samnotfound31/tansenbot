"""
bot.audio.queue
Queue helpers that wrap database.py for persistent guild queues.
"""

import logging
from typing import Dict, Any, List, Optional

from database import save_queue, load_queue, delete_queue

logger = logging.getLogger("tansen.queue")


def get(guild_id: int) -> List[Dict[str, Any]]:
    """Return the persisted queue for a guild (always a list)."""
    try:
        queue = load_queue(str(guild_id)) or []

        # Debug logging: verify canonical metadata is preserved after load
        canonical_count = sum(1 for song in queue if song.get("canonical_title"))
        if canonical_count > 0:
            logger.info(
                "[queue] Loaded %d songs, %d with canonical metadata",
                len(queue),
                canonical_count,
            )

        return queue
    except Exception:
        logger.exception("Failed to load queue for guild %s", guild_id)
        return []


def save(guild_id: int, queue: List[Dict[str, Any]]) -> None:
    """Persist the queue list for a guild."""
    try:
        save_queue(str(guild_id), queue)
    except Exception:
        logger.exception("Failed to save queue for guild %s", guild_id)
        raise


def clear(guild_id: int) -> None:
    """Delete the entire queue for a guild."""
    try:
        delete_queue(str(guild_id))
    except Exception:
        logger.exception("Failed to clear queue for guild %s", guild_id)


def add(guild_id: int, songs: List[Dict[str, Any]], *, front: bool = False) -> int:
    """Append (or prepend) songs to the guild queue.  Returns count added."""
    q = get(guild_id)

    # Debug logging: verify canonical metadata is being preserved
    for song in songs:
        if song.get("canonical_title"):
            logger.info(
                "[queue] Adding song with canonical metadata: title='%s', artist='%s'",
                song.get("canonical_title"),
                song.get("canonical_primary_artist"),
            )

    if front:
        q = songs + q
    else:
        q.extend(songs)
    save(guild_id, q)
    return len(songs)


def pop_next(guild_id: int) -> Optional[Dict[str, Any]]:
    """Pop and return the first song from the queue, or None."""
    q = get(guild_id)
    if not q:
        return None
    song = q.pop(0)
    save(guild_id, q)
    return song


def remove_at(guild_id: int, position: int) -> Optional[Dict[str, Any]]:
    """Remove a song at 1-based *position*.  Returns the removed dict or None."""
    q = get(guild_id)
    idx = position - 1
    if idx < 0 or idx >= len(q):
        return None
    removed = q.pop(idx)
    save(guild_id, q)
    return removed


def length(guild_id: int) -> int:
    return len(get(guild_id))
