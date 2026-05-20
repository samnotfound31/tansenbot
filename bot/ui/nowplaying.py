"""
bot.ui.nowplaying
Now Playing embed builder and persistent button view.
"""

import time
import asyncio
import logging
from typing import Dict, Any, Optional, List, Tuple

import discord
import wavelink
import requests

from database import load_guild_settings, save_guild_settings
from bot.utils.formatting import format_mmss, make_progress_bar, make_equalizer
from bot.audio import queue as guild_queue

logger = logging.getLogger("tansen.ui.np")


def create_now_playing_embed(
    song: Dict[str, Any],
    *,
    player: Optional["bot.audio.player.TansenPlayer"] = None,
) -> discord.Embed:
    """Build a rich Now Playing embed with progress bar and equalizer.

    Playback UI should primarily use playback fields (original SoundCloud title, uploader, URI).
    """
    # Fallback for None song
    if not song:
        logger.warning("[ui:np] create_now_playing_embed called with None song")
        return discord.Embed(
            title="Nothing is playing",
            color=discord.Color.dark_gray()
        )
    # Use playback metadata (fully SoundCloud-native) as primary for UI display
    title = song.get("playback_title") or song.get("title") or "Unknown"
    primary_artist = song.get("playback_author") or song.get("author") or ""
    duration = song.get("playback_duration") or song.get("duration") or 0
    song_artwork = song.get("playback_artwork") or song.get("artwork")
    uri = song.get("playback_uri") or song.get("uri") or ""
    requester = song.get("requester") or ""

    # Debug logging for metadata source tracking
    has_canonical = bool(song.get("canonical_title"))
    if has_canonical:
        logger.debug(
            "[ui:np] Using canonical metadata for lyrics lookup: title='%s', artist='%s' "
            "(playback_title='%s', playback_author='%s')",
            song.get("canonical_title"),
            song.get("canonical_primary_artist"),
            title,
            primary_artist,
        )
    else:
        logger.debug(
            "[ui:np] Using playback metadata (no canonical): title='%s', author='%s'",
            title,
            primary_artist,
        )

    # Timing
    elapsed = player.elapsed if player else 0.0
    elapsed = min(elapsed, duration) if duration else elapsed
    is_paused = player.pause_start is not None if player else False

    bar = make_progress_bar(elapsed, duration)
    elapsed_str = format_mmss(int(elapsed))
    total_str = format_mmss(duration)
    eq = make_equalizer(paused=is_paused)

    # Title line linked to original SoundCloud track URI
    if uri:
        name_line = f"[{title}]({uri})"
    else:
        name_line = title

    lines = [f"### \U0001f3b5 {name_line}"]
    if primary_artist:
        lines.append(f"**{primary_artist}**")
    lines.append("")
    lines.append(f"`{eq}`")
    lines.append(f"`{elapsed_str}  {bar}  {total_str}`")
    lines.append("")

    meta = []
    if requester:
        meta.append(f"\U0001f464 {requester}")
    meta.append("\U0001f3a7 SoundCloud")
    lines.append("  \u00b7  ".join(meta))

    embed = discord.Embed(description="\n".join(lines), color=0xFF5500)  # SoundCloud orange

    if song_artwork:
        try:
            embed.set_thumbnail(url=song_artwork)
        except Exception:
            pass

    embed.set_footer(text="\U0001f3a7 Tansen Music  \u2022  Use the buttons below to control playback")

    # Synced lyrics window
    if player and player.lyrics_enabled and player.synced_lyrics and elapsed > 0:
        _add_lyrics_field(embed, player.synced_lyrics, elapsed)

    return embed


def _add_lyrics_field(
    embed: discord.Embed,
    synced: List[Tuple[float, str]],
    elapsed: float,
) -> None:
    current_idx = 0
    for i, (ts, _) in enumerate(synced):
        if ts <= elapsed:
            current_idx = i
    window_start = max(0, current_idx - 2)
    window = synced[window_start: window_start + 5]
    lyric_lines = []
    for wi, (_, line) in enumerate(window):
        if not line:
            continue
        actual = window_start + wi
        if actual == current_idx:
            lyric_lines.append(f"**\u25b6 {line} \u25c0**")
        else:
            lyric_lines.append(f" {line}")
    if lyric_lines:
        embed.add_field(name="\U0001f3a4 Lyrics", value="\n".join(lyric_lines), inline=False)


# ── Synced lyrics fetcher (lrclib.net) ───────────────────────────────────

async def fetch_synced_lyrics(
    title: str,
    artists: List[str],
    duration: int,
    *,
    is_canonical: bool = False,
) -> List[Tuple[float, str]]:
    """Fetch timestamped LRC lyrics from lrclib.net."""
    import re as _re

    artist = artists[0] if artists else ""
    logger.info(
        "[lyrics] LRCLib lookup: title='%s', artist='%s', duration=%d, is_canonical=%s",
        title, artist, duration, is_canonical
    )
    loop = asyncio.get_running_loop()

    def _get(url: str, params: dict):
        try:
            r = requests.get(url, params=params, timeout=6)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    data = await loop.run_in_executor(
        None, _get,
        "https://lrclib.net/api/get",
        {"artist_name": artist, "track_name": title, "duration": duration},
    )

    if not data or not data.get("syncedLyrics"):
        results = await loop.run_in_executor(
            None, _get,
            "https://lrclib.net/api/search",
            {"q": f"{artist} {title}"},
        )
        if isinstance(results, list):
            for item in results:
                if item.get("syncedLyrics"):
                    data = item
                    break

    synced = (data or {}).get("syncedLyrics", "")
    if not synced:
        return []

    pattern = _re.compile(r"\[(\d+):(\d+\.\d+)\]\s*(.*)")
    lines: List[Tuple[float, str]] = []
    for m in pattern.finditer(synced):
        mins, secs, text = m.groups()
        ts = int(mins) * 60 + float(secs)
        lines.append((ts, text.strip()))
    return lines


# ── Now Playing embed live-update loop ───────────────────────────────────

async def np_update_loop(player) -> None:
    """Periodically refresh the Now Playing embed (progress bar + equalizer)."""
    try:
        while True:
            await asyncio.sleep(5)
            if not player.now_playing:
                break
            if not player.np_message:
                break
            # Skip updates while paused
            if player.paused:
                continue
            try:
                new_embed = create_now_playing_embed(player.now_playing, player=player)
                await player.np_message.edit(embed=new_embed)
            except discord.NotFound:
                break
            except discord.HTTPException:
                pass
            except Exception:
                break
    except asyncio.CancelledError:
        pass


# ── Persistent NowPlaying Button View ────────────────────────────────────

class NowPlayingView(discord.ui.View):
    """Persistent view with playback control buttons.

    Uses custom_ids so Discord routes button presses to the bot
    even after a restart.
    """

    def __init__(self, song=None, player=None):
        super().__init__(timeout=None)
        self.player = player  # Track player for state refresh
        self.song = song  # Track current song for state sync
        
        # Initial button state refresh
        if player:
            self.refresh_button_states(player, song)

    @staticmethod
    def _get_player(guild: discord.Guild) -> Optional["bot.audio.player.TansenPlayer"]:
        vc = guild.voice_client
        if vc and isinstance(vc, wavelink.Player):
            return vc
        return None

    def refresh_button_states(self, player, song=None) -> None:
        """Update button labels/styles based on current player state."""
        # Sync self.song
        if song:
            self.song = song
        elif player:
            song = player.get_active_song()
            self.song = song

        logger.info(
            "[ui-state] player.now_playing='%s', view.song='%s'",
            player.now_playing.get("title") if player and player.now_playing else None,
            self.song.get("title") if self.song else None,
        )

        # Find buttons by custom_id
        pause_btn = None
        loop_btn = None
        lyrics_btn = None
        for child in self.children:
            if hasattr(child, "custom_id"):
                if child.custom_id == "tansen:pause_resume":
                    pause_btn = child
                elif child.custom_id == "tansen:loop":
                    loop_btn = child
                elif child.custom_id == "tansen:lyrics":
                    lyrics_btn = child

        # Pause/Resume button
        if pause_btn:
            if player and player.paused:
                pause_btn.label = "▶️ Resume"
                pause_btn.emoji = None
                pause_btn.style = discord.ButtonStyle.success
                logger.info("[ui-buttons] Updated pause button -> Resume")
            else:
                pause_btn.label = "⏸ Pause"
                pause_btn.emoji = None
                pause_btn.style = discord.ButtonStyle.success
                logger.info("[ui-buttons] Updated pause button -> Pause")

        # Loop button
        if loop_btn:
            if player and player.is_looping:
                loop_btn.label = "🔁 Loop: ON"
                loop_btn.emoji = None
                loop_btn.style = discord.ButtonStyle.success
                logger.info("[ui-buttons] Updated loop button -> Loop: ON")
            else:
                loop_btn.label = "🔁 Loop: OFF"
                loop_btn.emoji = None
                loop_btn.style = discord.ButtonStyle.secondary
                logger.info("[ui-buttons] Updated loop button -> Loop: OFF")

        # Lyrics button
        if lyrics_btn:
            if player and player.lyrics_enabled:
                lyrics_btn.label = "🎤 Lyrics: ON"
                lyrics_btn.emoji = None
                lyrics_btn.style = discord.ButtonStyle.success
                logger.info("[ui-buttons] Updated lyrics button -> Lyrics: ON")
            else:
                lyrics_btn.label = "🎤 Lyrics"
                lyrics_btn.emoji = None
                lyrics_btn.style = discord.ButtonStyle.secondary
                logger.info("[ui-buttons] Updated lyrics button -> Lyrics")

    async def safe_edit(self, interaction: discord.Interaction, embed=None, view=None) -> None:
        """Safely edit message without Interaction already acknowledged errors."""
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.message.edit(embed=embed, view=view)

    # ── Pause / Resume ────────────────────────────────────────────────

    @discord.ui.button(label="\u23f8 Pause", style=discord.ButtonStyle.success, custom_id="tansen:pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._get_player(interaction.guild)
        if not player:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        if player.playing and not player.paused:
            await player.pause(True)
            player.mark_pause()
        elif player.paused:
            await player.pause(False)
            player.mark_resume()
        else:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return

        # Refresh button states and edit message
        song = player.get_active_song()
        self.refresh_button_states(player, song)
        new_embed = create_now_playing_embed(song, player=player)
        await self.safe_edit(interaction, embed=new_embed, view=self)

    # ── Skip ──────────────────────────────────────────────────────────

    @discord.ui.button(label="\u23ed Skip", style=discord.ButtonStyle.secondary, custom_id="tansen:skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._get_player(interaction.guild)
        if not player or not (player.playing or player.paused):
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)
            return
        await player.skip(force=True)
        await interaction.response.send_message("\u23ed Skipped.", ephemeral=True)

    # ── Loop ──────────────────────────────────────────────────────────

    @discord.ui.button(label="\U0001f501 Loop OFF", style=discord.ButtonStyle.secondary, custom_id="tansen:loop")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._get_player(interaction.guild)
        if not player:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        new_val = player.toggle_loop()

        # Refresh button states and edit message
        song = player.get_active_song()
        if not song:
            await interaction.response.send_message("Song metadata missing, try /nowplaying", ephemeral=True)
            return
        logger.info("[ui-state] button='loop' using='%s'", "player.now_playing" if player.now_playing else "last_known_song")
        self.refresh_button_states(player, song)
        new_embed = create_now_playing_embed(song, player=player)
        await self.safe_edit(interaction, embed=new_embed, view=self)

    # ── Volume Down ───────────────────────────────────────────────────

    @discord.ui.button(label="\U0001f509 -10%", style=discord.ButtonStyle.secondary, custom_id="tansen:vol_down")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._get_player(interaction.guild)
        if not player:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        vol = max(0.0, round(player.stored_volume - 0.10, 2))
        player.save_volume(vol)
        await player.set_volume(int(vol * 100))
        await interaction.response.send_message(f"\U0001f509 Volume: **{int(vol * 100)}%**", ephemeral=True)

    # ── Volume Up ─────────────────────────────────────────────────────

    @discord.ui.button(label="\U0001f50a +10%", style=discord.ButtonStyle.secondary, custom_id="tansen:vol_up")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._get_player(interaction.guild)
        if not player:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        vol = min(2.0, round(player.stored_volume + 0.10, 2))
        player.save_volume(vol)
        await player.set_volume(int(vol * 100))
        await interaction.response.send_message(f"\U0001f50a Volume: **{int(vol * 100)}%**", ephemeral=True)

    # ── Queue ─────────────────────────────────────────────────────────

    @discord.ui.button(label="\U0001f4cb Queue", style=discord.ButtonStyle.secondary, custom_id="tansen:show_queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        from bot.utils.formatting import format_song_line

        player = self._get_player(interaction.guild)
        q = guild_queue.get(interaction.guild.id)
        current = player.get_active_song() if player else self.song

        if not q and not current:
            await interaction.response.send_message("Queue is empty and nothing is playing.", ephemeral=True)
            return

        lines = []
        if current:
            # Use playback metadata (fully SoundCloud-native) as primary for UI display
            title = current.get("playback_title") or current.get("title") or "Unknown"
            author = current.get("playback_author") or current.get("author") or ""
            author_str = f" \u2014 {author}" if author else ""
            dur = format_mmss(current.get("playback_duration") or current.get("duration"))
            lines.append(f"\u25b6 **Now Playing:** {title}{author_str} [{dur}]")
        if q:
            lines.append(f"\n**Up Next** ({len(q)} song{'s' if len(q) != 1 else ''}):")
            for i, s in enumerate(q[:24], start=1):
                lines.append(format_song_line(s, i))
            if len(q) > 24:
                lines.append(f"*...and {len(q) - 24} more*")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── Stop ──────────────────────────────────────────────────────────

    @discord.ui.button(label="\u23f9 Stop", style=discord.ButtonStyle.danger, custom_id="tansen:stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._get_player(interaction.guild)
        if player:
            await player.stop_and_clear()
        else:
            guild_queue.clear(interaction.guild.id)

        # Disable all controls
        for child in self.children:
            child.disabled = True

        # Edit message with disabled controls
        await self.safe_edit(interaction, view=self)

    # ── Lyrics ────────────────────────────────────────────────────────

    @discord.ui.button(label="\U0001f3a4 Lyrics", style=discord.ButtonStyle.secondary, custom_id="tansen:lyrics")
    async def lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._get_player(interaction.guild)
        song = player.get_active_song() if player else self.song
        
        if not player or not song:
            if player and (player.playing or player.paused):
                await interaction.response.send_message("Song metadata is temporarily unavailable.", ephemeral=True)
            else:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        logger.info("[ui-state] button='lyrics' using='%s'", "player.now_playing" if player.now_playing else "last_known_song")

        await interaction.response.defer(ephemeral=True)

        if player.lyrics_enabled:
            player.lyrics_enabled = False
            await interaction.followup.send("\U0001f3a4 Lyrics hidden.", ephemeral=True)
        else:
            if not player.synced_lyrics:
                await interaction.followup.send("\u23f3 Fetching synced lyrics\u2026", ephemeral=True)

                # Use canonical metadata if available (for accurate lyrics lookup)
                if song.get("canonical_title"):
                    title = song.get("canonical_title") or ""
                    artists = song.get("canonical_artists") or [song.get("author") or ""]
                    is_canonical = True
                    # Log under lyrics-resolver format
                    logger.info(
                        "\n[lyrics-resolver]\nplayback_title=\"%s\"\ncanonical_title=\"%s\"\ncanonical_artist=\"%s\"",
                        song.get("playback_title") or song.get("title") or "",
                        title,
                        artists[0] if artists else "",
                    )
                else:
                    # Infer canonical metadata from playback metadata using lyrics_canonicalizer
                    from bot.providers.lyrics_canonicalizer import get_canonical_for_lyrics
                    canonical = await get_canonical_for_lyrics(song)
                    title = canonical.get("canonical_title") or song.get("title") or ""
                    artists = [canonical.get("canonical_artist") or song.get("author") or ""]
                    is_canonical = canonical.get("provider") != "heuristics"
                    # Log under lyrics-resolver format
                    logger.info(
                        "\n[lyrics-resolver]\nplayback_title=\"%s\"\ncanonical_title=\"%s\"\ncanonical_artist=\"%s\"",
                        song.get("playback_title") or song.get("title") or "",
                        title,
                        artists[0] if artists else "",
                    )

                dur = int(song.get("duration") or 0)

                try:
                    lines = await fetch_synced_lyrics(title, artists, dur, is_canonical=is_canonical)
                    if lines:
                        player.synced_lyrics = lines
                except Exception:
                    logger.exception("Lyrics fetch failed")

            if player.synced_lyrics:
                player.lyrics_enabled = True
                await interaction.followup.send(
                    "\U0001f3a4 Synced lyrics enabled! They'll appear in the Now Playing embed.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "\u274c No synced lyrics found for this song.", ephemeral=True
                )

        # Refresh button states and edit message
        self.refresh_button_states(player, song)
        new_embed = create_now_playing_embed(song, player=player)
        await self.safe_edit(interaction, embed=new_embed, view=self)
