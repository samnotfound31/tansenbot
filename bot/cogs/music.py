"""
bot.cogs.music
All music-related slash commands and Wavelink event handlers.
"""

import asyncio
import logging
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from database import (
    load_queue, save_queue, delete_queue,
    load_guild_settings, save_guild_settings,
)
from bot.audio.player import TansenPlayer
from bot.audio import queue as guild_queue
from bot.providers import soundcloud
from bot.providers.search_engine import find, track_to_dict, resolve_from_dict
from bot.ui.nowplaying import (
    NowPlayingView,
    create_now_playing_embed,
    np_update_loop,
)
from bot.ui.queue_views import SearchResultView
from bot.utils.formatting import format_mmss, format_song_line

logger = logging.getLogger("tansen.music")


class Music(commands.Cog):
    """SoundCloud music playback via Lavalink."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Wavelink Events ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        logger.info(
            "Lavalink node '%s' ready  (resumed=%s)",
            payload.node.identifier,
            payload.resumed,
        )

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player: TansenPlayer = payload.player
        if not isinstance(player, TansenPlayer):
            return
        
        # Restore pending song if it exists (recovery/replacement)
        if player.pending_song:
            player.set_now_playing(player.pending_song)
            logger.info(
                "[state-sync] track_start restored pending_song title='%s'",
                player.pending_song.get("title") or player.pending_song.get("playback_title") or "unknown"
            )
            player.pending_song = None
        
        logger.info("Track started: %s", payload.track.title)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: TansenPlayer = payload.player
        if not isinstance(player, TansenPlayer):
            return

        # "replaced" means a new track replaced the old one intentionally (skip, recovery, etc.)
        # Do NOT trigger queue advance, requeue, or recovery on this reason
        if payload.reason == "replaced":
            logger.info("Track replaced intentionally (skip/recovery) - skipping queue advance")
            return

        logger.info("Track ended: %s (reason=%s)", payload.track.title, payload.reason)

        # Cancel old NP updater
        player.cancel_np_task()

        # Play next
        started = await player.play_next()
        if started and player.get_active_song():
            try:
                song = player.get_active_song()
                embed = create_now_playing_embed(song, player=player)
                view = NowPlayingView(song=song, player=player)
                # UI spam protection: edit existing message if available
                if player.np_message:
                    try:
                        await player.np_message.edit(embed=embed, view=view)
                    except discord.NotFound:
                        # Message was deleted, send new one
                        player.np_message = await player.text_channel.send(embed=embed, view=view)
                else:
                    player.np_message = await player.text_channel.send(embed=embed, view=view)
                player.cancel_np_task()
                player.np_update_task = asyncio.create_task(np_update_loop(player))
            except Exception:
                logger.exception("Failed to send NP embed after track_end")
        elif not started:
            player.clear_now_playing(reason="queue_empty")
            logger.debug("Queue empty for guild %s.", player.guild.id)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        """Handle SoundCloud stream failures with automatic recovery."""
        player = getattr(payload, "player", None)
        if not isinstance(player, TansenPlayer):
            return

        # Skip recovery for "replaced" reason - this is intentional, not a failure
        if getattr(payload, "reason", None) == "replaced":
            logger.info("[audio-recovery] Skipping recovery for 'replaced' reason (intentional)")
            return

        exception = getattr(payload, "exception", None)
        track = getattr(payload, "track", None)
        
        exception_text = str(exception) if exception else str(payload)
        cause = getattr(exception, "cause", None) if exception else None
        message = getattr(exception, "message", None) if exception else None

        logger.warning(
            "Track exception: %s (exception='%s', cause='%s', message='%s')",
            getattr(track, "title", "unknown") if track else "unknown",
            exception_text[:200],
            str(cause)[:100] if cause else None,
            message[:100] if message else None,
        )

        # Check if this is a recoverable SoundCloud failure
        if player._is_recoverable_failure(exception_text):
            logger.info("[audio-recovery] Recoverable SoundCloud failure detected: %s", exception_text[:200])
            
            # Attempt recovery
            song = player.get_active_song()
            if song:
                recovered_song = await player._attempt_recovery(song)
                if recovered_song:
                    # Set pending song for track_start handler to restore
                    player.pending_song = recovered_song
                    logger.info(
                        "[state-sync] recovery set pending_song title='%s'",
                        recovered_song.get("title") or recovered_song.get("playback_title") or "unknown"
                    )
                    # Replace current song with recovered version
                    player.set_now_playing(recovered_song)
                    logger.info(
                        "[state-sync] recovery set_now_playing title='%s'",
                        recovered_song.get("title") or recovered_song.get("playback_title") or "unknown"
                    )
                    logger.info("[audio-recovery] Replacing failed track with recovered version")
                    # Play the recovered track
                    resolved_track = await resolve_from_dict(recovered_song)
                    if resolved_track:
                        await player.play(resolved_track)
                        return
                    else:
                        logger.warning("[audio-recovery] Failed to resolve recovered track, skipping")

        # Recovery failed or not recoverable - skip to next track
        logger.info("[audio-recovery] Skipping failed track")
        await player.skip(force=True)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player):
        """Auto-disconnect after inactivity (Wavelink 3.x fires this)."""
        if isinstance(player, TansenPlayer):
            player.set_now_playing(None)
            player.cancel_np_task()
        try:
            await player.disconnect()
        except Exception:
            pass
        logger.info("Disconnected inactive player in guild %s", player.guild.id)

    # ── Helper: ensure voice ─────────────────────────────────────────────

    async def _ensure_voice(self, interaction: discord.Interaction) -> TansenPlayer:
        member = interaction.user
        if not getattr(member, "voice", None) or not member.voice or not member.voice.channel:
            raise app_commands.AppCommandError("You must be in a voice channel.")

        channel = member.voice.channel
        player: Optional[TansenPlayer] = interaction.guild.voice_client

        if player and player.connected:
            if player.channel.id != channel.id:
                await player.move_to(channel)
            player.text_channel = interaction.channel
            return player

        player = await channel.connect(cls=TansenPlayer, self_deaf=True)
        player.text_channel = interaction.channel
        return player

    # ── Helper: queue + start playback ───────────────────────────────────

    async def _queue_and_play(
        self,
        interaction: discord.Interaction,
        player: TansenPlayer,
        song: Dict[str, Any],
    ) -> None:
        guild_queue.add(interaction.guild.id, [song])

        if not player.playing and not player.paused:
            started = await player.play_next()
            if started and player.get_active_song():
                song = player.get_active_song()
                embed = create_now_playing_embed(song, player=player)
                view = NowPlayingView(song=song, player=player)
                player.np_message = await interaction.channel.send(embed=embed, view=view)
                player.cancel_np_task()
                player.np_update_task = asyncio.create_task(np_update_loop(player))

    # ══════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="play", description="Search SoundCloud and play a track.")
    @app_commands.describe(query="Song name, artist, or SoundCloud URL")
    async def play_cmd(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        try:
            player = await self._ensure_voice(interaction)
        except app_commands.AppCommandError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        # Direct SoundCloud URL
        if soundcloud.is_soundcloud_url(query):
            track = await soundcloud.resolve_url(query)
            if not track:
                await interaction.followup.send("Could not resolve that SoundCloud URL.", ephemeral=True)
                return
            # Direct URLs have no canonical metadata (expected)
            song = track_to_dict(track, requester=interaction.user.display_name, canonical=None, playback_query=query)
            logger.info(
                "[play] Direct SoundCloud URL queued (no canonical metadata): %s",
                track.title,
            )
            await self._queue_and_play(interaction, player, song)
            await interaction.followup.send(f"Queued: **{track.title} \u2014 {track.author}**")
            return

        # Search + rank (Spotify-enriched)
        result = await find(query)

        # Debug logging: verify canonical metadata is present
        if result.canonical:
            logger.info(
                "[play] Spotify resolved: title='%s', artist='%s'",
                result.canonical.title,
                result.canonical.primary_artist,
            )
        else:
            logger.warning("[play] Spotify resolution FAILED for query: '%s'", query)

        if not result.ranked:
            await interaction.followup.send("No results found on SoundCloud.", ephemeral=True)
            return

        # ── Confidence tier routing ───────────────────────────────────────

        if result.tier == "autoplay" and result.best:
            # 80+ confidence → instant autoplay
            track = result.best.track
            logger.info(
                "[play] Autoplay tier: passing canonical=%s to track_to_dict",
                "YES" if result.canonical else "NO",
            )
            song = track_to_dict(track, requester=interaction.user.display_name, canonical=result.canonical, playback_query=query)
            await self._queue_and_play(interaction, player, song)
            meta = ""
            if result.canonical:
                meta = f"\n-# Matched: *{result.canonical.title}* by *{result.canonical.primary_artist}*"
            await interaction.followup.send(
                f"Queued: **{track.title} \u2014 {track.author}**{meta}"
            )

        elif result.tier == "quick_pick":
            # 50–79 confidence → show top 3 quick choices
            logger.info(
                "[play] Quick pick tier: passing canonical=%s to SearchResultView",
                "YES" if result.canonical else "NO",
            )
            view = SearchResultView(
                interaction.guild.id,
                interaction.user.display_name,
                result.quick_picks,
                canonical=result.canonical,
                query=query,
            )
            header = "Top matches"
            if result.canonical:
                header = f"Matches for *{result.canonical.title}* by *{result.canonical.primary_artist}*"
            await interaction.followup.send(
                f"{header} \u2014 pick one:",
                view=view,
            )

        else:
            # <50 confidence → full interactive dropdown
            logger.info(
                "[play] Full menu tier: passing canonical=%s to SearchResultView",
                "YES" if result.canonical else "NO",
            )
            view = SearchResultView(
                interaction.guild.id,
                interaction.user.display_name,
                result.alternatives,
                canonical=result.canonical,
                query=query,
            )
            await interaction.followup.send(
                "Low confidence \u2014 select the correct track:",
                view=view,
                ephemeral=True,
            )

    @app_commands.command(name="playurl", description="Play a direct SoundCloud URL.")
    @app_commands.describe(url="SoundCloud track URL")
    async def playurl_cmd(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(thinking=True)

        try:
            player = await self._ensure_voice(interaction)
        except app_commands.AppCommandError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        track = await soundcloud.resolve_url(url)
        if not track:
            await interaction.followup.send("Could not resolve that URL.", ephemeral=True)
            return

        song = track_to_dict(track, requester=interaction.user.display_name, playback_query=url)
        await self._queue_and_play(interaction, player, song)
        await interaction.followup.send(f"Queued: **{track.title} \u2014 {track.author}**")

    @app_commands.command(name="skip", description="Skip the current song.")
    async def skip_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        if not player or not (player.playing or player.paused):
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await player.skip(force=True)
        await interaction.response.send_message("\u23ed Skipped.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue.")
    async def stop_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        if player:
            await player.stop_and_clear()
        else:
            guild_queue.clear(interaction.guild.id)
        await interaction.response.send_message("\u23f9 Stopped and cleared queue.", ephemeral=True)

    @app_commands.command(name="pause", description="Pause or resume playback.")
    async def pause_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        if player.playing and not player.paused:
            await player.pause(True)
            player.mark_pause()
            await interaction.response.send_message("\u23f8 Paused.", ephemeral=True)
        elif player.paused:
            await player.pause(False)
            player.mark_resume()
            await interaction.response.send_message("\u25b6 Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)

    @app_commands.command(name="volume", description="Set playback volume (0\u2013200%).")
    @app_commands.describe(level="Volume percentage (0-200)")
    async def volume_cmd(self, interaction: discord.Interaction, level: int):
        if level < 0 or level > 200:
            await interaction.response.send_message("Volume must be **0\u2013200**.", ephemeral=True)
            return
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        vol = float(level) / 100.0
        if player:
            player.save_volume(vol)
            await player.set_volume(level)
        else:
            s = load_guild_settings(str(interaction.guild.id))
            save_guild_settings(
                str(interaction.guild.id),
                volume_level=vol,
                is_looping=s.get("is_looping", False),
                last_played=s.get("last_played"),
                previous_played=s.get("previous_played"),
            )
        await interaction.response.send_message(f"\U0001f50a Volume set to **{level}%**.", ephemeral=True)

    @app_commands.command(name="loop", description="Toggle loop of the current queue.")
    async def loop_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        if player:
            new_val = player.toggle_loop()
        else:
            s = load_guild_settings(str(interaction.guild.id))
            new_val = not bool(s.get("is_looping", False))
            save_guild_settings(
                str(interaction.guild.id),
                volume_level=s.get("volume_level", 1.0),
                is_looping=new_val,
                last_played=s.get("last_played"),
                previous_played=s.get("previous_played"),
            )
        await interaction.response.send_message(
            f"Loop is now **{'ON' if new_val else 'OFF'}**.", ephemeral=True
        )

    @app_commands.command(name="queue", description="Show the current queue.")
    async def queue_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        q = guild_queue.get(interaction.guild.id)
        current = player.get_active_song() if player else None

        if not q and not current:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return

        lines = []
        if current:
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

    @app_commands.command(name="remove", description="Remove a song from the queue by position.")
    @app_commands.describe(position="1-based position (see /queue)")
    async def remove_cmd(self, interaction: discord.Interaction, position: int):
        removed = guild_queue.remove_at(interaction.guild.id, position)
        if not removed:
            await interaction.response.send_message("Invalid position.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Removed: {format_song_line(removed)}", ephemeral=True
        )

    @app_commands.command(name="clear", description="Clear the queue (keeps current song).")
    async def clear_cmd(self, interaction: discord.Interaction):
        guild_queue.save(interaction.guild.id, [])
        await interaction.response.send_message("Cleared the queue.", ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the Now Playing panel.")
    async def nowplaying_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        if not player or not player.get_active_song():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        song = player.get_active_song()
        embed = create_now_playing_embed(song, player=player)
        view = NowPlayingView(song=song, player=player)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="lyrics", description="Toggle synced karaoke lyrics.")
    async def lyrics_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        if not player or not player.get_active_song():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if player.lyrics_enabled:
            player.lyrics_enabled = False
            await interaction.followup.send("\U0001f3a4 Lyrics hidden.", ephemeral=True)
        else:
            if not player.synced_lyrics:
                from bot.ui.nowplaying import fetch_synced_lyrics

                song = player.get_active_song()
                
                # Resolve canonical metadata if available (for accurate lyrics lookup)
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
                    "\U0001f3a4 Synced lyrics enabled!", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "\u274c No synced lyrics found.", ephemeral=True
                )

    @app_commands.command(name="join", description="Make the bot join your voice channel.")
    async def join_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await self._ensure_voice(interaction)
            await interaction.followup.send(
                f"Joined `{player.channel.name}`.", ephemeral=True
            )
        except app_commands.AppCommandError as e:
            await interaction.followup.send(str(e), ephemeral=True)
        except Exception:
            logger.exception("Join error")
            await interaction.followup.send("Failed to join voice channel.", ephemeral=True)

    @app_commands.command(name="leave", description="Disconnect the bot from voice.")
    async def leave_cmd(self, interaction: discord.Interaction):
        player: Optional[TansenPlayer] = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        guild_queue.clear(interaction.guild.id)
        player.set_now_playing(None)
        player.cancel_np_task()
        await player.disconnect()
        await interaction.response.send_message("Disconnected.", ephemeral=True)

    # ── Assist ───────────────────────────────────────────────────────────

    @app_commands.command(name="assist", description="Show bot help and available commands.")
    async def assist_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Tansen \u2014 Music Bot",
            description="SoundCloud-powered music bot using Lavalink.",
            color=0xFF5500,
        )
        cmds = {
            "Playback": "`/play` `/playurl` `/skip` `/stop` `/pause` `/volume` `/loop`",
            "Queue": "`/queue` `/remove` `/clear` `/nowplaying`",
            "Lyrics": "`/lyrics`",
            "Voice": "`/join` `/leave`",
        }
        for cat, text in cmds.items():
            embed.add_field(name=cat, value=text, inline=False)
        embed.set_footer(text="\U0001f3a7 Tansen Music \u2022 Powered by SoundCloud + Lavalink")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
