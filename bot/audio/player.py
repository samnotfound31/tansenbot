"""
bot.audio.player
Custom Wavelink player wrapper with per-guild state tracking.

Manages:
- now-playing state
- playback timing (for progress bars)
- pause duration tracking
- synced lyrics state
- loop mode
- SoundCloud stream failure recovery
"""

import time
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Set

import discord
import wavelink

from database import load_guild_settings, save_guild_settings
from bot.audio import queue as guild_queue
from bot.providers.search_engine import resolve_from_dict, track_to_dict
from bot.providers import soundcloud
from bot.providers.search_engine import find as search_engine_find

logger = logging.getLogger("tansen.player")

# ── Failed URI memory for recovery ───────────────────────────────────────────
_failed_uris: Set[str] = set()  # In-memory set of failed SoundCloud URIs
MAX_RECOVERY_ATTEMPTS = 2  # Max retries per song

# ── Loop guard for infinite replacement detection ────────────────────────────
_last_play_attempts: Dict[int, List[Tuple[str, float]]] = {}  # guild_id -> [(uri, timestamp), ...]
MAX_REPLACEMENTS_IN_WINDOW = 3
REPLACEMENT_WINDOW_SECONDS = 5


class TansenPlayer(wavelink.Player):
    """Extended Wavelink player with Tansen-specific state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Per-guild state
        self.now_playing: Optional[Dict[str, Any]] = None
        self.last_known_song: Optional[Dict[str, Any]] = None
        self.pending_song: Optional[Dict[str, Any]] = None  # For recovery/replacement
        self.play_start: Optional[float] = None
        self.paused_duration: float = 0.0
        self.pause_start: Optional[float] = None
        self.synced_lyrics: List[Tuple[float, str]] = []
        self.lyrics_enabled: bool = False
        self.text_channel: Optional[discord.TextChannel] = None
        self.np_message: Optional[discord.Message] = None
        self.np_update_task: Optional[asyncio.Task] = None

    # ── Settings helpers ─────────────────────────────────────────────────

    def _settings(self) -> Dict[str, Any]:
        return load_guild_settings(str(self.guild.id))

    @property
    def is_looping(self) -> bool:
        return bool(self._settings().get("is_looping", False))

    @property
    def stored_volume(self) -> float:
        return float(self._settings().get("volume_level", 1.0))

    def toggle_loop(self) -> bool:
        s = self._settings()
        new_val = not bool(s.get("is_looping", False))
        save_guild_settings(
            str(self.guild.id),
            volume_level=s.get("volume_level", 1.0),
            is_looping=new_val,
            last_played=s.get("last_played"),
            previous_played=s.get("previous_played"),
        )
        return new_val

    def save_volume(self, vol: float) -> None:
        s = self._settings()
        save_guild_settings(
            str(self.guild.id),
            volume_level=vol,
            is_looping=s.get("is_looping", False),
            last_played=s.get("last_played"),
            previous_played=s.get("previous_played"),
        )

    # ── Pause tracking ───────────────────────────────────────────────────

    def mark_pause(self) -> None:
        self.pause_start = time.time()

    def mark_resume(self) -> None:
        if self.pause_start:
            self.paused_duration += time.time() - self.pause_start
            self.pause_start = None

    @property
    def elapsed(self) -> float:
        if not self.play_start:
            return 0.0
        paused = self.paused_duration
        if self.pause_start:
            paused += time.time() - self.pause_start
        return max(0.0, (time.time() - self.play_start) - paused)

    # ── Playback lifecycle ───────────────────────────────────────────────

    def set_now_playing(self, song: Optional[Dict[str, Any]]) -> None:
        """Set the current song metadata and update last_known_song."""
        self.now_playing = song
        self.last_known_song = song
        self.play_start = time.time() if song else None
        self.paused_duration = 0.0
        self.pause_start = None
        self.synced_lyrics = []
        self.lyrics_enabled = False
        
        if song:
            logger.info(
                "[state-sync] set_now_playing title='%s'",
                song.get("title") or song.get("playback_title") or "unknown"
            )

        # Persist last_played in guild settings
        if song:
            try:
                s = self._settings()
                save_guild_settings(
                    str(self.guild.id),
                    volume_level=s.get("volume_level", 1.0),
                    is_looping=s.get("is_looping", False),
                    last_played=song,
                    previous_played=s.get("last_played"),
                )
            except Exception:
                pass

    def get_active_song(self) -> Optional[Dict[str, Any]]:
        """Get the active song, falling back to last_known_song if now_playing is None."""
        if self.now_playing:
            logger.info("[state-sync] get_active_song source='now_playing'")
            return self.now_playing
        elif self.last_known_song:
            logger.info("[state-sync] get_active_song source='last_known_song'")
            return self.last_known_song
        return None

    def clear_now_playing(self, reason: str = "") -> None:
        """Clear the current song metadata."""
        self.now_playing = None
        self.last_known_song = None
        logger.info("[state-sync] clear_now_playing reason='%s'", reason)

        # Reset timing and lyrics state
        self.play_start = None
        self.paused_duration = 0.0
        self.pause_start = None
        self.synced_lyrics = []
        self.lyrics_enabled = False

        # Persist last_played in guild settings
        if self.now_playing:
            try:
                s = self._settings()
                save_guild_settings(
                    str(self.guild.id),
                    volume_level=s.get("volume_level", 1.0),
                    is_looping=s.get("is_looping", False),
                    last_played=song,
                    previous_played=s.get("last_played"),
                )
            except Exception:
                pass

    def cancel_np_task(self) -> None:
        if self.np_update_task and not self.np_update_task.done():
            self.np_update_task.cancel()
        self.np_update_task = None

    # ── SoundCloud Stream Failure Recovery ───────────────────────────────────

    def _is_recoverable_failure(self, exception_text: str) -> bool:
        """Check if exception is a recoverable SoundCloud stream failure."""
        if not exception_text:
            return False
        exc_str = exception_text.lower()
        # Check for SoundCloud-specific failure indicators
        return any(
            keyword in exc_str
            for keyword in [
                "soundcloud stream: 404",
                "invalid status code",
                "ioexception",
                "loadfailed",
                "404",
            ]
        )

    async def _attempt_recovery(self, song: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Attempt to recover a failed song by re-searching and excluding failed URI.

        Returns new song dict if recovery succeeded, None if failed.
        """
        query = song.get("playback_query", "")
        if not query:
            logger.warning("[audio-recovery] No playback_query stored, cannot recover")
            return None

        # Check recovery attempt limit
        attempts = song.get("recovery_attempts", 0)
        if attempts >= MAX_RECOVERY_ATTEMPTS:
            logger.warning(
                "[audio-recovery] Max recovery attempts (%d) reached for query='%s', skipping track",
                MAX_RECOVERY_ATTEMPTS,
                query,
            )
            return None

        # Add failed URI to memory
        failed_uri = song.get("uri", "")
        if failed_uri:
            _failed_uris.add(failed_uri)
            logger.info("[audio-recovery] Added failed URI to memory: '%s'", failed_uri[:50])

        # Re-run SoundCloud search
        logger.info("[audio-recovery] Retrying query='%s'", query)
        try:
            result = await search_engine_find(query)
            if not result or not result.ranked:
                logger.warning("[audio-recovery] Re-search returned no results")
                return None

            # Find a candidate that doesn't match the failed URI
            replacement = None
            for st in result.ranked:
                track = st.track
                if track.uri != failed_uri and track.uri not in _failed_uris:
                    replacement = st
                    break

            if not replacement:
                logger.warning("[audio-recovery] No alternative candidates found")
                return None

            # Build new song dict with recovery metadata preserved
            new_song = track_to_dict(
                replacement.track,
                requester=song.get("requester", ""),
                canonical=result.canonical,
                playback_query=query,
            )
            new_song["failed_provider_ids"] = song.get("failed_provider_ids", []) + [failed_uri]
            new_song["recovery_attempts"] = attempts + 1

            logger.info(
                "[audio-recovery] Recovery successful: new title='%s', new uploader='%s'",
                replacement.track.title,
                replacement.track.author,
            )
            return new_song

        except Exception as e:
            logger.exception("[audio-recovery] Recovery attempt failed: %s", str(e))
            return None

    # ── Loop guard for infinite replacement detection ────────────────────────────

    def _check_loop_guard(self, song: Dict[str, Any]) -> bool:
        """Check if the same URI is being played repeatedly in a short time window.

        Returns True if loop detected (should block playback), False otherwise.
        """
        guild_id = self.guild.id
        uri = song.get("uri", "")
        now = time.time()

        # Initialize tracking for this guild
        if guild_id not in _last_play_attempts:
            _last_play_attempts[guild_id] = []

        # Clean old attempts outside the time window
        _last_play_attempts[guild_id] = [
            (u, ts) for u, ts in _last_play_attempts[guild_id]
            if now - ts < REPLACEMENT_WINDOW_SECONDS
        ]

        # Count recent attempts with the same URI
        same_uri_count = sum(1 for u, ts in _last_play_attempts[guild_id] if u == uri)

        # Add current attempt
        _last_play_attempts[guild_id].append((uri, now))

        # Check for loop
        if same_uri_count >= MAX_REPLACEMENTS_IN_WINDOW:
            logger.warning(
                "[loop-guard] Blocked infinite replacement loop: guild=%s, uri='%s', attempts=%d in %ds",
                guild_id,
                uri[:50],
                same_uri_count,
                REPLACEMENT_WINDOW_SECONDS,
            )
            return True

        return False

    async def play_next(self) -> bool:
        """Pop next song from queue, resolve it, and start playback.

        Returns True if a song was started, False if queue is empty.
        """
        song = guild_queue.pop_next(self.guild.id)
        if not song:
            self.set_now_playing(None)
            self.cancel_np_task()
            return False

        # Check loop guard before playing
        if self._check_loop_guard(song):
            logger.warning("[loop-guard] Stopping playback to prevent infinite loop")
            self.set_now_playing(None)
            self.cancel_np_task()
            return False

        # Debug logging: verify canonical metadata is preserved after queue load
        if song.get("canonical_title"):
            logger.info(
                "[player] Loaded song with canonical metadata: title='%s', artist='%s'",
                song.get("canonical_title"),
                song.get("canonical_primary_artist"),
            )
        else:
            logger.warning(
                "[player] Loaded song WITHOUT canonical metadata: title='%s', author='%s'",
                song.get("title"),
                song.get("author"),
            )

        # Resolve the stored dict back to a Playable
        try:
            playable = await resolve_from_dict(song)
        except Exception:
            logger.exception("Failed to resolve track '%s'", song.get("title"))
            playable = None

        if not playable:
            logger.warning("Could not resolve '%s' — skipping.", song.get("title"))
            if self.text_channel:
                try:
                    await self.text_channel.send(
                        f"\u26a0\ufe0f Could not stream **{song.get('title', 'Unknown')}** — skipping."
                    )
                except Exception:
                    pass
            return await self.play_next()  # try next

        # Apply stored volume (Wavelink uses 0–1000 scale)
        vol = int(self.stored_volume * 100)
        await self.set_volume(vol)

        # Update state and start playback
        # IMPORTANT: Set now_playing BEFORE playback starts for UI sync
        self.set_now_playing(song)
        # IMPORTANT: Only update provider metadata, NEVER overwrite canonical metadata
        song["duration"] = int((playable.length or 0) / 1000)
        # Only update artwork if we don't already have canonical artwork
        if not song.get("canonical_title") or not song.get("artwork"):
            song["artwork"] = getattr(playable, "artwork", None) or song.get("artwork")
        await self.play(playable, replace=True)
        logger.info("Now playing: %s", song.get("title"))
        return True

    async def stop_and_clear(self) -> None:
        """Stop playback, clear queue, reset state."""
        guild_queue.clear(self.guild.id)
        self.clear_now_playing(reason="stop")
        self.cancel_np_task()
        if self.playing or self.paused:
            await self.stop(force=True)
        try:
            await self.disconnect()
        except Exception:
            pass
