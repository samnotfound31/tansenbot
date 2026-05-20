"""
bot.ui.queue_views
Interactive selection views for search results.
"""

import logging
from typing import Dict, Any, List, Optional

import discord
import wavelink

from bot.providers.playback_ranking import ScoredTrack
from bot.providers.search_engine import track_to_dict
from bot.audio import queue as guild_queue
from bot.audio.player import TansenPlayer
from bot.utils.formatting import format_mmss, truncate

logger = logging.getLogger("tansen.ui.queue")


class SearchResultSelect(discord.ui.Select):
    """Dropdown for picking from ranked SoundCloud search results."""

    def __init__(self, guild_id: int, requester: str, scored: List[ScoredTrack], canonical=None, query: str = ""):
        self.guild_id = guild_id
        self.requester = requester
        self.scored = scored
        self.canonical = canonical  # Canonical metadata for lyrics and UI
        self.query = query  # Original query for recovery

        # Debug logging for metadata source tracking
        if canonical:
            logger.info(
                "[ui:queue] Dropdown using canonical metadata: title='%s', artist='%s'",
                canonical.title,
                canonical.primary_artist,
            )
        else:
            logger.info("[ui:queue] Dropdown using SoundCloud metadata (no canonical)")

        opts = []
        for idx, st in enumerate(scored[:10]):
            t = st.track
            dur = format_mmss(int((t.length or 0) / 1000))
            confidence = int(st.score)

            label = truncate(f"{t.title}", 90)
            author = truncate(t.author or "Unknown", 40)
            desc = truncate(f"{author}  \u2022  {dur}  \u2022  \u2605 {confidence}%", 100)

            opts.append(discord.SelectOption(label=label, description=desc, value=f"sc_{idx}"))

        super().__init__(placeholder="Select a track\u2026", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        try:
            idx = int(self.values[0].replace("sc_", ""))
            st = self.scored[idx]
            track = st.track

            # Debug logging: verify canonical metadata is available at callback time
            if self.canonical:
                logger.info(
                    "[ui:queue] Callback has canonical metadata: title='%s', artist='%s'",
                    self.canonical.title,
                    self.canonical.primary_artist,
                )
            else:
                logger.warning(
                    "[ui:queue] Callback MISSING canonical metadata - this is a bug"
                )

            # Use canonical metadata if available (for lyrics lookup and queue storage)
            song = track_to_dict(track, requester=self.requester, canonical=self.canonical, playback_query=self.query)

            # Ensure bot is in voice
            member = interaction.user
            if not getattr(member, "voice", None) or not member.voice or not member.voice.channel:
                await interaction.response.send_message(
                    "You must be in a voice channel.", ephemeral=True
                )
                return

            channel = member.voice.channel
            player: Optional[TansenPlayer] = interaction.guild.voice_client

            if not player or not player.connected:
                player = await channel.connect(cls=TansenPlayer, self_deaf=True)
            elif player.channel.id != channel.id:
                await player.move_to(channel)

            player.text_channel = interaction.channel

            guild_queue.add(self.guild_id, [song])
            await interaction.response.send_message(
                f"Queued: **{track.title} \u2014 {track.author}**", ephemeral=True
            )

            # Start playback if idle
            if not player.playing and not player.paused:
                await player.play_next()
                # Send NP embed
                from bot.ui.nowplaying import NowPlayingView, create_now_playing_embed, np_update_loop
                import asyncio

                if player.now_playing:
                    embed = create_now_playing_embed(player.now_playing, player=player)
                    view = NowPlayingView()
                    player.np_message = await interaction.channel.send(embed=embed, view=view)
                    player.cancel_np_task()
                    player.np_update_task = asyncio.create_task(np_update_loop(player))

        except Exception:
            logger.exception("SearchResultSelect callback error")
            try:
                await interaction.response.send_message("An error occurred.", ephemeral=True)
            except Exception:
                pass


class SearchResultView(discord.ui.View):
    """View wrapping the search result select dropdown."""

    def __init__(self, guild_id: int, requester: str, scored: List[ScoredTrack], canonical=None, query: str = ""):
        super().__init__(timeout=60)
        self.add_item(SearchResultSelect(guild_id, requester, scored, canonical=canonical, query=query))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
