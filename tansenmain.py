"""
tansenmain.py
Main bot file with features reintroduced:
- Spotify search dropdown + OAuth linking (if SPOTIFY_* env vars + spotipy available)
- /play (search Spotify app token -> dropdown), /playurl, /playpl
- /skip /stop /loop /clear /remove /queue /nowplaying /nowrics /assist
- Now Playing panel: persistent message updates, button UI, refresh
- Lyrics OVH -> Genius fallback (uses lyrics.py)
- Robust voice connect helper and FFmpeg/yt-dlp streaming
- Uses database.py for queues, playlists, settings, spotify token storage
"""

import os
import re
import logging
import asyncio
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse, parse_qs
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import requests
from dotenv import load_dotenv
load_dotenv()
from database import (
    save_queue, load_queue, delete_queue,
    save_playlist, load_playlists, delete_playlist,
    save_spotify_token, get_spotify_token_for_user, delete_spotify_token,
    save_guild_settings, load_guild_settings
)
# prefer async helpers when running inside the bot's event loop
from spotifyapi import (
    get_spotify_oauth_url,
    exchange_code_for_token_async,
    get_spotify_token_async,
    search_spotify_tracks_async,
    get_spotify_token,            # sync fallback (if used)
    search_spotify_tracks,        # sync fallback
    get_app_spotify_token,
    get_app_spotify_token_async
)

from lyrics import clean_song_title, get_best_lyrics

from keep_alive import start_keep_alive

# Config
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DCTOKEN")
if not TOKEN:
    # allow later injection, but warn
    TOKEN = "<PUT_DISCORD_TOKEN_HERE>"

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.voice_states = True

bot = commands.Bot(command_prefix="!", intents=INTENTS, help_command=None)
tree = bot.tree

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tansen")

# yt-dlp & ffmpeg settings
YDL_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "skip_download": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "ignoreerrors": True,
    # sometimes helpful:
    "prefer_ffmpeg": True,
    "geo_bypass": True,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn -nostdin"

# runtime state copied from original
now_playing: Dict[int, Optional[Dict[str, Any]]] = {}      # guild_id -> song dict
voice_locks: Dict[int, asyncio.Lock] = {}                  # serialize per-guild play_next
last_music_channel: Dict[int, discord.TextChannel] = {}    # guild_id -> last command's text channel
last_now_playing_messages: Dict[int, Dict[str, int]] = {}  # guild_id -> {"channel_id": int, "message_id": int}

# helper locks
def get_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in voice_locks:
        voice_locks[guild_id] = asyncio.Lock()
    return voice_locks[guild_id]

# helper: run blocking
async def run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# voice helper (safe)
async def safe_connect_voice(channel: discord.VoiceChannel) -> discord.VoiceClient:
    """
    Robust voice connect helper:
    - Tries multiple connect() signatures depending on installed discord lib.
    - Falls back to calling connect() with no extra kwargs.
    - If the library doesn't support deaf/self_deaf keyword, tries to deafen after connect.
    """
    guild = channel.guild

    # If there's already a voice client in the guild, move it -> ensure single VC
    vc_existing = getattr(guild, "voice_client", None)
    if vc_existing and vc_existing.is_connected():
        try:
            await vc_existing.disconnect()
        except Exception:
            logger.exception("Error disconnecting previous voice client")

    connect_variants = [
        {"deaf": True, "timeout": 20},
        {"self_deaf": True, "timeout": 20},
        {"timeout": 20},
        {}
    ]

    last_exc = None
    for kw in connect_variants:
        try:
            # attempt connect with this kwarg set
            if kw:
                vc = await channel.connect(**kw)
            else:
                vc = await channel.connect()
            # success
            logger.info("Connected to voice (using kwargs=%s) in guild %s", kw, guild.id)
            # if we couldn't pass deafen arg into connect but want to ensure the bot is deafened,
            # try to set deafen on the member object (requires proper permissions).
            try:
                # Some libraries provide a Member object at guild.me; others via guild.get_member(bot.user.id)
                me = guild.me or guild.get_member(bot.user.id)
                if me and not me.voice.deaf:
                    # Attempt to deafen (this may require 'deafen members' permission)
                    try:
                        await me.edit(deafen=True)
                        logger.info("Self-deafened the bot after connect.")
                    except Exception:
                        # If edit fails (lack of permissions or API not supported), ignore
                        pass
            except Exception:
                pass
            return vc
        except TypeError as te:
            # This variant signature not accepted by library. Try next.
            last_exc = te
            # log at debug level to avoid noisy logs, but keep some context
            logger.debug("connect() variant %s not supported: %s", kw, te)
            continue
        except Exception as e:
            # Real connection failures (network/permissions) should stop trying further variants.
            logger.exception("Failed to connect to voice channel with kwargs=%s", kw)
            last_exc = e
            break

    # If we get here we failed to connect with any variant
    logger.error("Failed to connect to voice channel; last exception: %s", last_exc)
    # re-raise as a user-friendly error for callers to handle
    raise RuntimeError(f"Failed to connect to voice channel: {last_exc}")


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient:
    if not interaction.guild:
        raise app_commands.AppCommandError("This command requires a guild.")
    member = interaction.user
    if not getattr(member, "voice", None) or not member.voice or not member.voice.channel:
        raise app_commands.AppCommandError("You must be connected to a voice channel to use this command.")
    s_channel = member.voice.channel
    vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if vc and vc.is_connected():
        if vc.channel.id != s_channel.id:
            await vc.move_to(s_channel)
        return vc
    # connect
    return await safe_connect_voice(s_channel)

# yt-dlp extract info helper
async def ytdl_extract_info(query: str) -> Optional[Dict[str, Any]]:
    """
    Extract a playable info dict using yt_dlp in a thread.
    Tries the given query first. If the result has no usable formats,
    falls back to a ytsearch (ytsearch1:) and picks the best entry.

    Returns:
        A single info dict suitable for playback, or None on failure.
    """
    loop = asyncio.get_event_loop()

    def _extract(q: str):
        try:
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(q, download=False)
                return info
        except Exception as e:
            # Keep exception for debugging in Python main thread via logger.exception below
            return {"__error__": str(e)}

    # Try original query first
    info = await loop.run_in_executor(None, _extract, query)
    if isinstance(info, dict) and info.get("__error__"):
        logger.debug("yt-dlp first attempt error: %s", info.get("__error__"))
        info = None

    # Helper to pick the best single entry from yt-dlp results
    def _pick_best_entry(obj):
        if not obj:
            return None
        # If it's a playlist/search result, choose first viable entry that has formats or url
        if isinstance(obj, dict) and "entries" in obj:
            entries = obj.get("entries") or []
            for e in entries:
                if not e:
                    continue
                # Prefer entries that include formats or a webpage_url/url
                if e.get("formats") or e.get("webpage_url") or e.get("url"):
                    return e
            return None
        # If it's a single info dict, ensure it has playable data
        if isinstance(obj, dict):
            if obj.get("formats") or obj.get("webpage_url") or obj.get("url"):
                return obj
        return None

    picked = _pick_best_entry(info)
    if picked:
        return picked

    # If original didn't yield usable formats, try a search fallback
    try:
        fallback_query = query
        if not str(query).startswith("ytsearch1:"):
            fallback_query = f"ytsearch1:{query}"
        logger.debug("yt-dlp: trying fallback search query: %s", fallback_query)
        info2 = await loop.run_in_executor(None, _extract, fallback_query)
        if isinstance(info2, dict) and info2.get("__error__"):
            logger.debug("yt-dlp fallback attempt error: %s", info2.get("__error__"))
            info2 = None
        picked2 = _pick_best_entry(info2)
        if picked2:
            return picked2
    except Exception:
        logger.exception("yt-dlp fallback search failed")

    # As a final attempt, if info was a dict with entries, try to return first non-empty raw entry
    if isinstance(info, dict) and "entries" in info:
        for e in (info.get("entries") or []):
            if e:
                return e

    return None


def vc_for_guild(guild: discord.Guild) -> Optional[discord.VoiceClient]:
    return next((v for v in bot.voice_clients if v.guild.id == guild.id), None)

def format_mmss(seconds: Optional[int]) -> str:
    if seconds is None:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

def format_song_line(song: Dict[str, Any], idx: Optional[int] = None) -> str:
    title = song.get("title") or "Untitled"
    artists = song.get("artists") or ""
    if isinstance(artists, list):
        artists = ", ".join(artists)
    artists_str = f" — {artists}" if artists else ""
    by = f" • requested by {song.get('requester')}" if song.get("requester") else ""
    head = f"[{idx}] " if idx is not None else ""
    return f"{head}{title}{artists_str}{by}"

def build_song_dict(
    *,
    title: str,
    artists: List[str],
    album: Optional[str],
    duration_sec: Optional[int],
    thumbnail: Optional[str],
    requester: str,
    spotify_url: Optional[str],
    source: str = "Spotify→YouTube",
    is_local: bool = False,
    local_path: Optional[str] = None,
    stream_search_query: Optional[str] = None,
    youtube_webpage: Optional[str] = None,
    guild_id: Optional[int] = None,   # <-- ADDED
) -> Dict[str, Any]:
    return {
        "title": title,
        "artists": artists,
        "album": album,
        "duration": duration_sec,
        "thumbnail": thumbnail,
        "requester": requester,
        "source": source,
        "is_local": is_local,
        "local_path": local_path,
        "spotify_url": spotify_url,
        "stream_query": stream_search_query,
        "url": youtube_webpage or stream_search_query,
        "guild_id": guild_id,          # <-- ADDED
    }


def spotify_track_to_metadata(track: Dict[str, Any]) -> Tuple[str, List[str], str, Optional[int], Optional[str], Optional[str]]:
    title = track.get("name") or "Unknown"
    artists = [a.get("name") for a in (track.get("artists") or [])]
    album = (track.get("album") or {}).get("name") or ""
    duration_ms = track.get("duration_ms")
    duration_sec = int(duration_ms / 1000) if duration_ms else None
    images = (track.get("album") or {}).get("images") or []
    cover_url = images[0]["url"] if images else None
    spotify_url = (track.get("external_urls") or {}).get("spotify")
    return title, artists, album, duration_sec, cover_url, spotify_url

# Queue helpers (wrap DB)
async def add_song_to_queue(guild_id: int, song_data, requester_name: str, play_now: bool = False) -> int:
    if isinstance(song_data, dict):
        songs = [song_data]
    else:
        songs = list(song_data)
    for s in songs:
        s.setdefault("requester", requester_name)
    q = load_queue(str(guild_id)) or []
    if play_now:
        q = songs + q
    else:
        q.extend(songs)
    save_queue(str(guild_id), q)
    return len(songs)

def pop_next_song(guild_id: int) -> Optional[Dict[str, Any]]:
    q = load_queue(str(guild_id))
    if not q:
        return None
    s = q.pop(0)
    save_queue(str(guild_id), q)
    return s

def peek_queue(guild_id: int) -> List[Dict[str, Any]]:
    return load_queue(str(guild_id)) or []

def set_now_playing(guild_id: int, song: Optional[Dict[str, Any]]):
    now_playing[guild_id] = song
    # persist last_played in guild settings
    try:
        settings = load_guild_settings(str(guild_id))
        prev = settings.get("last_played")
        save_guild_settings(str(guild_id), settings.get("volume_level", 1.0), settings.get("is_looping", False), song, prev)
    except Exception:
        pass

# Create audio source helper (resolve a playable URL with yt-dlp if needed)
async def make_discord_audio_source(song: Dict[str, Any], seek: int = 0, reconnect: bool = True):
    """
    Build a FFmpeg audio source with the guild's saved volume.
    """
    url_or_q = song.get("url") or song.get("stream_query")
    if not url_or_q:
        raise RuntimeError("No URL or stream query to build audio source.")

    # resolve using yt-dlp
    info = await ytdl_extract_info(url_or_q)
    if not info:
        raise RuntimeError("Failed to resolve stream via yt-dlp")

    stream_url = (
        info.get("url")
        or info.get("webpage_url")
        or info.get("formats", [{}])[-1].get("url")
    )
    if not stream_url:
        raise RuntimeError("yt-dlp returned no playable URL")

    # load volume
    guild_id = song.get("guild_id")
    if guild_id:
        settings = load_guild_settings(str(guild_id))
        volume_level = float(settings.get("volume_level", 1.0))
    else:
        volume_level = 1.0

    before = FFMPEG_BEFORE
    options = FFMPEG_OPTS
    if seek > 0:
        before = f"-ss {seek} " + before

    ff = discord.FFmpegPCMAudio(stream_url, before_options=before, options=options)
    return discord.PCMVolumeTransformer(ff, volume=volume_level)

# After-playback scheduler
async def play_next_in_guild(guild: discord.Guild):
    async with get_lock(guild.id):
        # load loop setting
        settings = load_guild_settings(str(guild.id))
        looping = settings.get("is_looping", False)

        # pop next
        song = pop_next_song(guild.id)
        if not song:
            # nothing queued - clear now_playing and schedule idle disconnect
            set_now_playing(guild.id, None)
            vc = vc_for_guild(guild)
            if vc:
                async def idle_disconnect():
                    await asyncio.sleep(int(os.getenv("IDLE_TIMEOUT", "300")))
                    if not peek_queue(guild.id):
                        try:
                            await vc.disconnect()
                        except Exception:
                            pass
                bot.loop.create_task(idle_disconnect())
            return

        # if looping is enabled, reappend the song at end after selecting
        if looping:
            q = load_queue(str(guild.id))
            q.append(song)
            save_queue(str(guild.id), q)

        set_now_playing(guild.id, song)

    vc = vc_for_guild(guild)
    if not vc:
        logger.warning("No VC for guild %s when attempting to play", guild.id)
        return

    try:
        source = await make_discord_audio_source(song)
    except Exception as e:
        logger.exception("Failed to create audio source: %s", e)
        # attempt next
        bot.loop.create_task(play_next_in_guild(guild))
        return

    def _after_play(error):
        if error:
            logger.exception("Playback error: %s", error)
        try:
            bot.loop.create_task(play_next_in_guild(guild))
        except Exception:
            logger.exception("Failed to schedule play_next")

    try:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        vc.play(source, after=_after_play)
    except Exception:
        logger.exception("Failed to start playback")
        bot.loop.create_task(play_next_in_guild(guild))
        return

    # send/update Now Playing panel
    try:
        channel = last_music_channel.get(guild.id)
        embed = create_now_playing_embed(song)
        view = NowPlayingView(guild.id)
        if channel:
            try:
                if guild.id in last_now_playing_messages:
                    info = last_now_playing_messages[guild.id]
                    panel_msg = await bot.get_channel(info["channel_id"]).fetch_message(info["message_id"])
                    await panel_msg.edit(embed=embed, view=view)
                else:
                    msg = await channel.send(embed=embed, view=view)
                    last_now_playing_messages[guild.id] = {"channel_id": msg.channel.id, "message_id": msg.id}
            except Exception:
                logger.exception("Failed to send/update Now Playing panel")
    except Exception:
        logger.exception("Now Playing panel error")

# Embed builder
def create_now_playing_embed(song: Dict[str, Any]) -> discord.Embed:
    title = song.get("title") or "Unknown"
    artists = song.get("artists") or []
    artists_str = ", ".join(artists) if isinstance(artists, list) else str(artists)
    album = song.get("album") or ""
    dur = format_mmss(song.get("duration"))
    sp_url = song.get("spotify_url")
    name_line = f"[{title}]({sp_url})" if sp_url else title
    desc = f"**{name_line}**"
    if artists_str:
        desc += f"\n*by* **{artists_str}**"
    if album:
        desc += f"\n*Album:* {album}"
    desc += f"\n*Duration:* {dur}"
    req = song.get("requester")
    if req:
        desc += f"\n*Requested by:* {req}"
    embed = discord.Embed(title="Now Playing", description=desc, color=discord.Color.blurple())
    if song.get("thumbnail"):
        try:
            embed.set_thumbnail(url=song["thumbnail"])
        except Exception:
            pass
    if sp_url:
        embed.add_field(name="Spotify", value=sp_url, inline=False)
    return embed

# NowPlaying View (buttons)
class NowPlayingView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.refresh_buttons()

    def refresh_buttons(self):
        settings = load_guild_settings(str(self.guild_id))
        looping = settings.get("is_looping", False)
        vc = vc_for_guild(bot.get_guild(int(self.guild_id))) if isinstance(self.guild_id, str) else None
        # adjust loop button style
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label and "Loop" in child.label:
                child.style = discord.ButtonStyle.success if looping else discord.ButtonStyle.danger

    async def refresh_message(self, interaction: discord.Interaction):
        self.refresh_buttons()
        try:
            if interaction and interaction.message:
                await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="⏯ Play/Pause", style=discord.ButtonStyle.primary, custom_id="tansen:pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = vc_for_guild(interaction.guild)
        if not vc:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        try:
            if vc.is_playing():
                vc.pause()
                await interaction.response.send_message("⏸ Paused.", ephemeral=True)
            elif vc.is_paused():
                vc.resume()
                await interaction.response.send_message("▶ Resumed.", ephemeral=True)
            else:
                await interaction.response.send_message("Nothing playing.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Failed to toggle pause.", ephemeral=True)
        await self.refresh_message(interaction)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.secondary, custom_id="tansen:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = vc_for_guild(interaction.guild)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)
            return
        try:
            vc.stop()
            await interaction.response.send_message("⏭ Skipped.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Failed to skip.", ephemeral=True)
        await self.refresh_message(interaction)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.danger, custom_id="tansen:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_guild_settings(str(interaction.guild.id))
        current = bool(settings.get("is_looping", False))
        new_val = not current
        save_guild_settings(str(interaction.guild.id), settings.get("volume_level", 1.0), new_val, settings.get("last_played"), settings.get("previous_played"))
        await interaction.response.send_message(f"Loop is now **{'ON' if new_val else 'OFF'}**.", ephemeral=True)
        await self.refresh_message(interaction)
    
    @discord.ui.button(label="🔉 -5%", style=discord.ButtonStyle.secondary, custom_id="tansen:vol_down")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_guild_settings(str(self.guild_id))
        volume = float(settings.get("volume_level", 1.0))
        volume = max(0.0, volume - 0.05)
        save_guild_settings(
            str(self.guild_id),
            volume_level=volume,
            is_looping=settings.get("is_looping", False),
            last_played=settings.get("last_played"),
            previous_played=settings.get("previous_played"),
        )

        vc = vc_for_guild(interaction.guild)
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume

        try:
            await interaction.response.send_message(f"🔉 Volume: **{int(volume*100)}%**", ephemeral=True)
        except:
            pass
        await self.refresh_message(interaction)

    @discord.ui.button(label="🔊 +5%", style=discord.ButtonStyle.secondary, custom_id="tansen:vol_up")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_guild_settings(str(self.guild_id))
        volume = float(settings.get("volume_level", 1.0))
        volume = min(2.0, volume + 0.05)
        save_guild_settings(
            str(self.guild_id),
            volume_level=volume,
            is_looping=settings.get("is_looping", False),
            last_played=settings.get("last_played"),
            previous_played=settings.get("previous_played"),
        )

        vc = vc_for_guild(interaction.guild)
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume

        try:
            await interaction.response.send_message(f"🔊 Volume: **{int(volume*100)}%**", ephemeral=True)
        except:
            pass
        await self.refresh_message(interaction)


    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="tansen:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = vc_for_guild(interaction.guild)
        delete_queue(str(interaction.guild.id))
        set_now_playing(interaction.guild.id, None)
        if vc and vc.is_connected():
            try:
                vc.stop()
                await vc.disconnect()
            except Exception:
                pass
        await interaction.response.send_message("Stopped and cleared queue.", ephemeral=True)
        await self.refresh_message(interaction)

    @discord.ui.button(label="📜 Lyrics", style=discord.ButtonStyle.secondary, custom_id="tansen:lyrics")
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        Robust lyrics handler:
        - Immediately defers the interaction to avoid "Unknown interaction" on long ops.
        - Uses followup to send ephemeral confirmation.
        - Falls back to channel.send if followup fails (e.g., interaction expired).
        """
        # Quick existence check
        song = now_playing.get(interaction.guild.id)
        if not song:
            # try to respond directly if still possible
            try:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            except Exception:
                # fallback to channel message
                try:
                    await interaction.channel.send("Nothing is playing.")
                except Exception:
                    logger.exception("Failed to notify user that nothing is playing.")
            return

        title = song.get("title", "")
        artists = song.get("artists") or []
        artist_name = artists[0] if isinstance(artists, list) and artists else ""

        # Acknowledge the interaction early (give ourselves more time)
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            # if defer fails, it's likely the interaction was already acknowledged/expired;
            # we'll continue and use channel-based fallbacks later.
            logger.debug("Interaction.defer() failed or not needed (interaction may be already acknowledged).")

        # Fetch lyrics (may take time)
        try:
            lyrics = await get_best_lyrics(artist_name, title)
        except Exception:
            logger.exception("Error while fetching lyrics")
            lyrics = None

        if not lyrics:
            # prefer followup (since we deferred)
            try:
                await interaction.followup.send("Lyrics not found.", ephemeral=True)
            except Exception:
                # fallback to channel message
                try:
                    await interaction.channel.send("Lyrics not found.")
                except Exception:
                    logger.exception("Failed to send 'lyrics not found' fallback.")
            return

        # Split into chunks so we don't exceed embed/content limits
        chunks = [lyrics[i:i+4000] for i in range(0, len(lyrics), 4000)]
        embed_chunks = []
        for idx, chunk in enumerate(chunks, start=1):
            em = discord.Embed(title=f"{title} — Lyrics ({idx}/{len(chunks)})", description=chunk)
            if idx == 1 and song.get("thumbnail"):
                try:
                    em.set_thumbnail(url=song.get("thumbnail"))
                except Exception:
                    pass
            embed_chunks.append(em)

        # Try to add lyrics into the Now Playing panel if possible (non-ephemeral),
        # otherwise send them into the channel one by one.
        info = last_now_playing_messages.get(interaction.guild.id)
        if info:
            try:
                ch = bot.get_channel(info["channel_id"])
                if ch:
                    panel_msg = await ch.fetch_message(info["message_id"])
                    # store only up to 10 embeds to avoid exceeding message limits
                    base_embeds = list(panel_msg.embeds)[:1] if panel_msg.embeds else []
                    combined = base_embeds + embed_chunks
                    combined = combined[:10]
                    await panel_msg.edit(embeds=combined, view=self)
                    # Let the user know via followup (ephemeral)
                    try:
                        await interaction.followup.send("Added lyrics to the Now Playing panel.", ephemeral=True)
                    except Exception:
                        # fallback to channel acknowledgement
                        await interaction.channel.send("Added lyrics to the Now Playing panel.")
                    return
            except Exception:
                logger.exception("Failed to edit Now Playing message; falling back to channel sends.")

        # Fallback: send lyrics as separate channel embeds (public)
        success = True
        for em in embed_chunks:
            try:
                await interaction.channel.send(embed=em)
            except Exception:
                logger.exception("Failed to send a lyrics embed to channel")
                success = False
                break

        # Final acknowledgement to the user
        if success:
            try:
                await interaction.followup.send("Posted lyrics below.", ephemeral=True)
            except Exception:
                # If followup failed (interaction expired), send a normal channel message
                try:
                    await interaction.channel.send("Posted lyrics below.")
                except Exception:
                    logger.exception("Failed to send final acknowledgement for lyrics.")
        else:
            try:
                await interaction.followup.send("Failed to post lyrics.", ephemeral=True)
            except Exception:
                try:
                    await interaction.channel.send("Failed to post lyrics.")
                except Exception:
                    logger.exception("Failed to send failure acknowledgement for lyrics.")


# Spotify Select view (interaction-safe)
class SpotifySelect(discord.ui.Select):
    MAX_LABEL = 100
    MAX_DESC = 100

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if not text:
            return ""
        text = str(text).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "…"

    @staticmethod
    def _make_label(title: str, artists: List[str]) -> str:
        # Compact: "Title — Artist1, Artist2"
        if artists:
            base = f"{title} — {', '.join(artists)}"
        else:
            base = title
        return SpotifySelect._truncate(base, SpotifySelect.MAX_LABEL)

    @staticmethod
    def _make_desc(album: str, dur: Optional[int]) -> str:
        parts = []
        if album:
            parts.append(album)
        if dur:
            parts.append(format_mmss(dur))
        desc = " • ".join(parts)
        return SpotifySelect._truncate(desc, SpotifySelect.MAX_DESC)

    def __init__(self, guild_id: int, requester: str, options_data: List[Dict[str, Any]]):
        self.guild_id = guild_id
        self.requester = requester
        self.options_data = options_data

        opts = []
        for idx, tr in enumerate(options_data):
            # Metadata extraction
            t, artists, album, dur, _, _ = spotify_track_to_metadata(tr)

            label = self._make_label(t, artists)
            desc = self._make_desc(album, dur)

            # value MUST be short, ≤100 chars, unique
            value = f"opt_{idx}"

            opts.append(
                discord.SelectOption(
                    label=label,
                    description=desc,
                    value=value
                )
            )

        super().__init__(
            placeholder="Select a track…",
            min_values=1,
            max_values=1,
            options=opts
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            choice = self.values[0]
            idx = int(choice.replace("opt_", ""))
            track = self.options_data[idx]

            song = await spotify_track_to_song_dict(
                track,
                requester=self.requester,
                guild_id=interaction.guild.id
            )
            if not song:
                await interaction.response.send_message("Failed to build song.", ephemeral=True)
                return

            # Ensure bot is in VC
            try:
                await ensure_voice(interaction)
            except app_commands.AppCommandError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            except Exception:
                logger.exception("Failed to ensure voice in SpotifySelect callback")
                await interaction.response.send_message("Could not join your voice channel.", ephemeral=True)
                return

            # Queue and notify
            await add_song_to_queue(self.guild_id, song, self.requester)
            await interaction.response.send_message(
                f"Queued: **{song['title']} — {', '.join(song.get('artists', []))}**",
                ephemeral=True
            )

            # Start playback if idle
            vc = vc_for_guild(interaction.guild)
            if vc and not (vc.is_playing() or vc.is_paused()):
                bot.loop.create_task(play_next_in_guild(interaction.guild))

        except Exception:
            logger.exception("SpotifySelect callback error")
            await interaction.response.send_message("An error occurred while selecting!", ephemeral=True)


class SpotifySearchView(discord.ui.View):
    def __init__(self, guild_id: int, requester: str, results: List[Dict[str, Any]]):
        super().__init__(timeout=60)
        self.add_item(SpotifySelect(guild_id, requester, results))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

# Spotify helpers
def top5_spotify_search(query: str):
    items = search_spotify_tracks(query) or []
    return items[:5]

async def spotify_track_to_song_dict(track: Dict[str, Any], requester: str, guild_id: Optional[str] = None):
    title, artists, album, duration_sec, cover_url, spotify_url = spotify_track_to_metadata(track)
    if not title or not artists:
        return None

    stream_q = f"{title} {' '.join(artists)}"

    song = build_song_dict(
        title=title,
        artists=artists,
        album=album,
        duration_sec=duration_sec,
        thumbnail=cover_url,
        requester=requester,
        spotify_url=spotify_url,
        source="Spotify→YouTube",
        is_local=False,
        local_path=None,
        stream_search_query=stream_q,
        youtube_webpage=None,
        guild_id=guild_id,        # <-- ADDED
    )
    return song


# -----------------------------
# Slash commands (app commands)
# -----------------------------
@bot.event
async def on_ready():
    logger.info("Bot ready. Logged in as %s (%s)", bot.user, bot.user.id)

    # Sync slash commands
    try:
        await bot.tree.sync()
        logger.info("Synced application commands.")
    except Exception:
        logger.exception("Failed to sync tree")

    # Set presence
    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="with raags"
            )
        )
        logger.info("Bot presence set: Playing with raags")
    except Exception:
        logger.exception("Failed to set bot presence")


@tree.command(name="join", description="Make the bot join your voice channel")
async def join(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        vc = await ensure_voice(interaction)
        await interaction.followup.send(f"Joined `{vc.channel.name}`.", ephemeral=True)
    except app_commands.AppCommandError as e:
        await interaction.followup.send(str(e), ephemeral=True)
    except Exception:
        logger.exception("Join error")
        await interaction.followup.send("Failed to join voice channel.", ephemeral=True)

@tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    vc = vc_for_guild(interaction.guild)
    if not vc:
        await interaction.followup.send("I'm not connected.", ephemeral=True)
        return
    try:
        await vc.disconnect()
        delete_queue(str(interaction.guild.id))
        await interaction.followup.send("Disconnected and cleared queue.", ephemeral=True)
    except Exception:
        logger.exception("Leave failed")
        await interaction.followup.send("Failed to disconnect.", ephemeral=True)

@tree.command(name="play", description="Search Spotify and pick result (requires SPOTIFY creds).")
@app_commands.describe(query="Search term (Spotify search)")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    # set last music channel
    last_music_channel[interaction.guild.id] = interaction.channel
    # search spotify (app token)
    results = top5_spotify_search(query)
    if not results:
        await interaction.followup.send("No results found on Spotify (or Spotify app token missing). Try /playurl with a YouTube link.", ephemeral=True)
        return
    view = SpotifySearchView(interaction.guild.id, interaction.user.display_name, results)
    await interaction.followup.send("Select a track from Spotify results:", view=view, ephemeral=True)

@tree.command(name="playurl", description="Play directly from a YouTube or direct URL")
@app_commands.describe(url="YouTube/stream URL or search query")
async def playurl_cmd(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    last_music_channel[interaction.guild.id] = interaction.channel
    try:
        # ensure voice
        await ensure_voice(interaction)
    except app_commands.AppCommandError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return

    # try to build song dict via yt-dlp
    info = await ytdl_extract_info(url)
    if not info:
        await interaction.followup.send("Could not resolve the provided URL/query.", ephemeral=True)
        return
    title = info.get("title") or "Unknown"
    dur = info.get("duration")
    thumb = info.get("thumbnail")
    song = build_song_dict(title=title, artists=[], album=None, duration_sec=dur, thumbnail=thumb, requester=interaction.user.display_name, spotify_url=None, stream_search_query=None, youtube_webpage=info.get("webpage_url") or info.get("url"))
    await add_song_to_queue(interaction.guild.id, song, interaction.user.display_name)
    # if idle, start playback
    vc = vc_for_guild(interaction.guild)
    if vc and not (vc.is_playing() or vc.is_paused()):
        bot.loop.create_task(play_next_in_guild(interaction.guild))
    await interaction.followup.send(f"Queued: **{title}**", ephemeral=True)

@tree.command(name="playpl", description="Queue an entire Spotify playlist (playlist id or url)")
@app_commands.describe(playlist="Playlist ID or URL")
async def playpl_cmd(interaction: discord.Interaction, playlist: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    last_music_channel[interaction.guild.id] = interaction.channel

    # parse playlist id if user provided a URL
    m = re.search(r"playlist/([A-Za-z0-9]+)", playlist)
    playlist_id = m.group(1) if m else playlist

    # get app token (async)
    token = await get_app_spotify_token_async()
    if not token:
        await interaction.followup.send("Spotify app token not configured. Cannot fetch playlists.", ephemeral=True)
        return

    headers = {"Authorization": f"Bearer {token}"}

    # fetch playlist tracks off the event loop to avoid blocking
    loop = asyncio.get_event_loop()

    def _fetch_all_tracks_sync(start_url: str, headers: dict):
        import requests
        all_tracks_local = []
        url = start_url
        params = {"limit": 100}
        try:
            while url:
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if r.status_code != 200:
                    # bubble some info for debugging
                    return {"error": True, "status": r.status_code, "body": r.text, "tracks": all_tracks_local}
                data = r.json()
                for it in data.get("items", []):
                    t = it.get("track")
                    if t:
                        all_tracks_local.append(t)
                url = data.get("next")
                params = None
            return {"error": False, "tracks": all_tracks_local}
        except Exception as exc:
            return {"error": True, "exception": str(exc), "tracks": all_tracks_local}

    playlist_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    fetch_result = await loop.run_in_executor(None, _fetch_all_tracks_sync, playlist_url, headers)

    if fetch_result.get("error"):
        # try to give a helpful message for common failures
        stat = fetch_result.get("status")
        exc = fetch_result.get("exception")
        body = fetch_result.get("body")
        logger.warning("Failed to fetch spotify playlist %s status=%s exc=%s body=%s", playlist_id, stat, exc, body)
        await interaction.followup.send(
            "Failed to fetch playlist from Spotify. Make sure the playlist/id is public and the bot's Spotify app token is configured.",
            ephemeral=True
        )
        return

    all_tracks = fetch_result.get("tracks", [])
    if not all_tracks:
        await interaction.followup.send("No tracks found in playlist (or failed to fetch).", ephemeral=True)
        return

    # Convert Spotify track objects -> internal song dicts (async)
    songs = []
    for tr in all_tracks:
        try:
            # spotify_track_to_song_dict should be an async function returning a song dict or None
            s = await spotify_track_to_song_dict(tr, requester=str(interaction.user.display_name))
            if s:
                songs.append(s)
        except Exception:
            logger.exception("Failed converting spotify track to song dict")

    if not songs:
        await interaction.followup.send("No playable tracks found in playlist.", ephemeral=True)
        return

    # Enqueue songs: support both an add_song_to_queue that takes a list or needs single songs.
    async def _maybe_await(fn, *a, **k):
        res = fn(*a, **k)
        if asyncio.iscoroutine(res):
            return await res
        return res

    try:
        # Try calling with the whole list first (many implementations accept that)
        await _maybe_await(add_song_to_queue, str(interaction.guild.id), songs, str(interaction.user.display_name))
    except TypeError:
        # fallback: call per-song
        for song in songs:
            try:
                await _maybe_await(add_song_to_queue, str(interaction.guild.id), song, str(interaction.user.display_name))
            except Exception:
                logger.exception("Failed to enqueue song from playlist")
    except Exception:
        # unexpected error while enqueueing
        logger.exception("Failed to enqueue playlist songs")
        await interaction.followup.send("There was an error queueing the playlist.", ephemeral=True)
        return

    # Ensure bot is in voice: if user is in a voice channel, connect/move there.
    try:
        vc = await ensure_voice(interaction)
    except Exception as exc:
        logger.warning("ensure_voice failed: %s", exc)
        # still respond that songs were queued but the bot couldn't join
        await interaction.followup.send(f"Queued {len(songs)} songs from playlist, but I couldn't join your voice channel: {exc}", ephemeral=True)
        return

    # start playback if idle
    # prefer guild.voice_client (discord.py standard)
    guild_vc = interaction.guild.voice_client
    try:
        if guild_vc and not (guild_vc.is_playing() or guild_vc.is_paused()):
            # start playback in background
            bot.loop.create_task(play_next_in_guild(interaction.guild))
    except Exception:
        logger.exception("Failed to start playback task after queuing playlist")

    await interaction.followup.send(f"Queued {len(songs)} songs from playlist.", ephemeral=True)


@tree.command(name="skip", description="Skip the current song.")
async def skip_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        vc = vc_for_guild(interaction.guild)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.followup.send("Nothing is playing.", ephemeral=True)
            return
        vc.stop()
        await interaction.followup.send("⏭ Skipped.", ephemeral=True)
    except Exception:
        logger.exception("skip error")
        await interaction.followup.send("Error while skipping.", ephemeral=True)

@tree.command(name="volume", description="Set playback volume (0–200%).")
@app_commands.describe(level="Volume percentage (0-200)")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if level < 0 or level > 200:
        await interaction.response.send_message("Volume must be between **0–200**.", ephemeral=True)
        return

    # save to DB
    settings = load_guild_settings(str(interaction.guild.id))
    save_guild_settings(
        str(interaction.guild.id),
        volume_level=float(level) / 100.0,
        is_looping=settings.get("is_looping", False),
        last_played=settings.get("last_played"),
        previous_played=settings.get("previous_played")
    )

    # apply to current audio source
    vc = vc_for_guild(interaction.guild)
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = float(level) / 100.0

    await interaction.response.send_message(f"🔊 Volume set to **{level}%**.", ephemeral=True)

@tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        vc = vc_for_guild(interaction.guild)
        delete_queue(str(interaction.guild.id))
        set_now_playing(interaction.guild.id, None)
        if vc and vc.is_connected():
            try:
                vc.stop()
                await vc.disconnect()
            except Exception:
                pass
        await interaction.followup.send("⏹ Stopped and cleared queue.", ephemeral=True)
    except Exception:
        logger.exception("stop error")
        await interaction.followup.send("Error while stopping.", ephemeral=True)

@tree.command(name="loop", description="Toggle loop of the current queue.")
async def loop_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        settings = load_guild_settings(str(interaction.guild.id))
        current = bool(settings.get("is_looping", False))
        new_val = not current
        save_guild_settings(str(interaction.guild.id), settings.get("volume_level", 1.0), new_val, settings.get("last_played"), settings.get("previous_played"))
        await interaction.followup.send(f"Loop is now **{'ON' if new_val else 'OFF'}**.", ephemeral=True)
    except Exception:
        logger.exception("loop error")
        await interaction.followup.send("Failed to toggle loop.", ephemeral=True)

@tree.command(name="queue", description="Show current queue (top 25).")
async def queue_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    q = peek_queue(interaction.guild.id)
    if not q:
        await interaction.followup.send("Queue is empty.", ephemeral=True)
        return
    lines = [format_song_line(s, i+1) for i, s in enumerate(q[:25])]
    await interaction.followup.send("Upcoming:\n" + "\n".join(lines), ephemeral=True)

@tree.command(name="remove", description="Remove item from queue by position.")
@app_commands.describe(position="1-based position (see /queue)")
async def remove_cmd(interaction: discord.Interaction, position: int):
    await interaction.response.defer(ephemeral=True)
    q = peek_queue(interaction.guild.id)
    if position < 1 or position > len(q):
        await interaction.followup.send("Invalid position.", ephemeral=True)
        return
    removed = q.pop(position - 1)
    save_queue(str(interaction.guild.id), q)
    await interaction.followup.send(f"Removed: {format_song_line(removed)}", ephemeral=True)

@tree.command(name="clear", description="Clear the queue (keeps current song).")
async def clear_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    # clear rest of queue but keep now_playing
    save_queue(str(interaction.guild.id), [])
    await interaction.followup.send("Cleared the queue.", ephemeral=True)

@tree.command(name="nowplaying", description="Show the Now Playing panel again.")
async def nowplaying_cmd(interaction: discord.Interaction):
    song = now_playing.get(interaction.guild.id)
    if not song:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    last_music_channel[interaction.guild.id] = interaction.channel
    embed = create_now_playing_embed(song)
    view = NowPlayingView(interaction.guild.id)
    await interaction.response.send_message(embed=embed, view=view)
    try:
        msg = await interaction.original_response()
        last_now_playing_messages[interaction.guild.id] = {"channel_id": msg.channel.id, "message_id": msg.id}
    except Exception:
        pass

@tree.command(name="nowrics", description="Show lyrics for the currently playing track.")
async def nowrics_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    song = now_playing.get(interaction.guild.id)
    if not song:
        await interaction.followup.send("Nothing is playing right now.", ephemeral=True)
        return
    title = song.get("title", "")
    artists = song.get("artists") or []
    artist_name = artists[0] if isinstance(artists, list) and artists else ""
    try:
        lyrics = await get_best_lyrics(artist_name, title)
    except Exception:
        lyrics = None
    if not lyrics:
        await interaction.followup.send("Lyrics not found.", ephemeral=True)
        return
    chunks = [lyrics[i:i+4000] for i in range(0, len(lyrics), 4000)]
    lyric_embeds = []
    for idx, chunk in enumerate(chunks, start=1):
        em = discord.Embed(title=f"Lyrics — {title} ({idx}/{len(chunks)})", description=chunk, color=discord.Color.dark_teal())
        if idx == 1 and song.get("thumbnail"):
            em.set_thumbnail(url=song.get("thumbnail"))
        lyric_embeds.append(em)
    info = last_now_playing_messages.get(interaction.guild.id)
    if info:
        try:
            ch = bot.get_channel(info["channel_id"])
            if ch:
                panel_msg = await ch.fetch_message(info["message_id"])
                base_embeds = list(panel_msg.embeds)[:1] if panel_msg.embeds else []
                combined = (base_embeds + lyric_embeds)[:10]
                await panel_msg.edit(embeds=combined, view=NowPlayingView(interaction.guild.id))
                await interaction.followup.send("Added lyrics to the Now Playing panel.", ephemeral=True)
                return
        except Exception:
            logger.exception("Failed to edit Now Playing message")
    for em in lyric_embeds:
        await interaction.channel.send(embed=em)
    await interaction.followup.send("Posted lyrics below.", ephemeral=True)

from urllib.parse import urlparse, parse_qs
import discord

# -- Modal: keep label <= 45 characters, put instructions in placeholder or pre-text
class SpotifyRedirectModal(discord.ui.Modal, title="Paste Spotify redirect URL"):
    redirect_url = discord.ui.TextInput(
        label="Redirected URL",                      # <= 45 chars (important)
        style=discord.TextStyle.long,                # paragraph input
        placeholder="Paste the full URL your browser was redirected to (contains ?code=...&state=... )",
        required=True,
        max_length=1500,
    )

    def __init__(self, *, author_id: int, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You didn't open this modal — action cancelled.", ephemeral=True)
            return

        raw = self.redirect_url.value.strip()
        try:
            parsed = urlparse(raw)
            qs = parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]
        except Exception:
            code = None
            state = None

        if not code:
            await interaction.response.send_message(
                "Couldn't find an authorization `code` in the URL you pasted. Make sure you pasted the full redirected URL (it contains `?code=...`).",
                ephemeral=True
            )
            return

        # exchange the code for tokens and save (async helper)
        try:
            ok = await exchange_code_for_token_async(code, state)
        except Exception as exc:
            await interaction.response.send_message(f"Failed to exchange code: {exc}", ephemeral=True)
            return

        if ok:
            await interaction.response.send_message("✅ Spotify linked successfully! You can now use Spotify features.", ephemeral=True)
        else:
            await interaction.response.send_message("Failed to link Spotify. The authorization code may be invalid or expired. Try again.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        try:
            await interaction.response.send_message("An unexpected error occurred while processing the URL.", ephemeral=True)
        except Exception:
            pass


# -- View: create link button in __init__ (link buttons must be constructed with url)
class SpotifyLinkView(discord.ui.View):
    def __init__(self, author_id: int, oauth_url: str, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.oauth_url = oauth_url

        # Link button MUST be constructed with the url argument
        self.add_item(discord.ui.Button(label="Open OAuth link", style=discord.ButtonStyle.link, url=self.oauth_url))

    @discord.ui.button(label="Paste redirected URL", style=discord.ButtonStyle.primary, custom_id="spotify_paste_redirect")
    async def paste_redirect(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the user who requested this link can paste the redirect URL here.", ephemeral=True)
            return
        modal = SpotifyRedirectModal(author_id=self.author_id)
        await interaction.response.send_modal(modal)


# -- Command to spawn the view
@tree.command(name="spotify_link", description="Get an OAuth URL to link your Spotify account (paste the redirect URL back here).")
async def spotify_link_cmd(interaction: discord.Interaction):
    try:
        oauth_url = get_spotify_oauth_url(state=str(interaction.user.id))
    except Exception as e:
        await interaction.response.send_message(f"Failed to build Spotify OAuth URL: {e}", ephemeral=True)
        return

    view = SpotifyLinkView(author_id=interaction.user.id, oauth_url=oauth_url)
    text = (
        "Click **Open OAuth link** to authorize the bot with Spotify.\n\n"
        "After authorising, Spotify will redirect your browser to a URL (the 'redirect URL').\n\n"
        "Copy that full redirected URL from your browser's address bar and click **Paste redirected URL** and paste it into the modal.\n\n"
        "This flow avoids requiring a public callback web server."
    )
    await interaction.response.send_message(text, view=view, ephemeral=True)


@tree.command(name="spotify_playlists", description="List your Spotify playlists (requires linking).")
async def spotify_playlists_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    token = get_spotify_token(str(interaction.user.id))
    if not token:
        await interaction.followup.send("You need to link your Spotify account first. Use the OAuth URL from the bot owner or /spotify_link.", ephemeral=True)
        return
    out = []
    url = "https://api.spotify.com/v1/me/playlists"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": 50}
    try:
        while url:
            r = requests.get(url, headers=headers, params=params, timeout=8)
            if r.status_code != 200:
                break
            data = r.json()
            for it in data.get("items", []):
                out.append({"name": it.get("name"), "id": it.get("id"), "tracks": it.get("tracks", {}).get("total", 0)})
            url = data.get("next")
            params = None
    except Exception:
        logger.exception("Failed to fetch user playlists")
    if not out:
        await interaction.followup.send("No playlists found or failed to fetch.", ephemeral=True)
        return
    lines = [f"**{p['name']}** — {p['tracks']} tracks — ID: `{p['id']}`" for p in out[:25]]
    await interaction.followup.send("Your playlists:\n" + "\n".join(lines), ephemeral=True)

# Assist/help command with multi-page view
# --- /assist interactive help panel -------------------------
import discord
from discord import app_commands
from discord.ui import View, Select, Button
from typing import Dict, Any, List

# A structured help database for the assist command.
# Add or edit entries to reflect your bot's current commands.
ASSIST_DB: Dict[str, Dict[str, Any]] = {
    "Playback": {
        "summary": "Play music from YouTube/Spotify queries, manage queue and playback.",
        "commands": {
            "/play <query|url>": "Queue and play a track. Query can be a YouTube URL, search terms, or a Spotify link.",
            "/playsp <spotify uri/url>": "Search Spotify and pick a track — then the bot will find a playable stream (YouTube fallback).",
            "/playpl <playlist id|url>": "Queue an entire Spotify playlist (will convert tracks to playable items).",
            "/queue": "Show the current queue for this guild.",
            "/skip": "Skip the current track.",
            "/pause": "Pause playback.",
            "/resume": "Resume playback after pause.",
            "/stop": "Stop playback and clear the queue.",
            "/nowplaying": "Show the currently playing track (with requester and progress).",
            "/volume <0.0-2.0>": "Set playback volume (0.0 silent — 1.0 default — 2.0 max).",
        },
        "usage": [
            "Example: `/play bohemian rhapsody`",
            "Example: `/play https://youtu.be/VIDEOID`",
            "Example: `/playpl https://open.spotify.com/playlist/...`",
        ],
    },
    "Spotify Integration": {
        "summary": "Link Spotify account, use app token search, and queue Spotify tracks/playlists.",
        "commands": {
            "/spotify_link": "Start an OAuth flow to link your Spotify account (paste redirect URL into modal).",
            "Spotify search UI": "Type a query and select results from the dropdown. The selected track is queued and played.",
        },
        "usage": [
            "Run `/spotify_link` — open the OAuth link, authorize, then paste redirect URL in the modal.",
            "Search: `/playsp <song or artist>` and use the select menu to pick a result.",
        ],
    },
    "Lyrics": {
        "summary": "Fetch lyrics from multiple providers (OVH first, Genius fallback).",
        "commands": {
            "/lyrics <title|auto>": "Fetch and post lyrics for the current or provided track. Auto attempts to detect current song.",
        },
        "usage": [
            "Example: `/lyrics` (gets lyrics for the now playing song)",
            "Example: `/lyrics All The Stars - Kendrick Lamar`",
        ],
    },
    "Queue & Playlists": {
        "summary": "Save and load user playlists, and persistent queue per guild.",
        "commands": {
            "/savepl <name>": "Save current queue as a playlist under your account.",
            "/loadpl <name>": "Load a saved playlist into the queue.",
            "/playpl <playlist id|url>": "Queue an entire Spotify playlist (see Playback above).",
        },
        "usage": [
            "Example: `/savepl chill-vibes`",
            "Example: `/loadpl chill-vibes`",
        ],
    },
    "Moderation / Logs": {
        "summary": "Moderation logging utilities and lookup (if enabled).",
        "commands": {
            "/modlogs set <channel>": "Set the modlog channel for the guild.",
            "/modlogs <user>": "Show a user's moderation history (paginated).",
            "/removewarnings <user>": "Remove warnings via dropdown selection.",
            "/clearlogs <user>": "Delete a user's stored moderation logs.",
        },
        "usage": [
            "Example: `/modlogs set #mod-log`",
            "Example: `/modlogs @offending_user`",
        ],
    },
    "Utilities": {
        "summary": "Misc helpful utilities and bot maintenance commands.",
        "commands": {
            "/ping": "Bot latency / health check.",
            "/assist": "Open this interactive help panel.",
            "/keepalive": "Internal keepalive endpoint (for hosting or pingers).",
            "/help (legacy)": "Text help (non-interactive).",
        },
        "usage": [
            "Example: `/ping`",
            "Use `/assist` for the full interactive guide.",
        ],
    },
    "Settings & Admin": {
        "summary": "Guild-specific settings stored persistently (volume, looping, modlog channel).",
        "commands": {
            "/set_volume <0.0-2.0>": "Set default guild playback volume.",
            "/antiraidmode": "Toggle anti-raid protections (admins only).",
            "/ipban <ip>": "Ban an IP (alt-detection) — admin-only and logs to DB.",
        },
        "usage": [
            "Only admins may change guild-level settings.",
        ],
    },
}


def _build_summary_embed(user: discord.User) -> discord.Embed:
    e = discord.Embed(
        title="Tansen — Assistant & Command Reference",
        description="Interactive command guide. Use the dropdown to pick a category or a specific command. "
                    "Buttons let you quickly view all commands, usage examples, or close this panel.",
        color=discord.Color.blurple()
    )
    e.add_field(name="Quick facts", value=(
        "• Playback: YouTube & Spotify → YouTube fallback\n"
        "• Lyrics: OVH first, Genius fallback\n"
        "• Persistent queues & playlists (SQLite)\n"
        "• Spotify OAuth linking supported (no public server required)\n"
    ), inline=False)
    e.set_footer(text=f"Requested by {user}", icon_url=getattr(user, "display_avatar", None))
    return e


class AssistSelect(Select):
    def __init__(self):
        options = []
        # top-level option to show everything
        options.append(discord.SelectOption(label="All command categories", description="Show the full list of categories and commands.", value="__all__"))
        # add each category
        for key, val in ASSIST_DB.items():
            # description trimmed to 100 chars
            desc = val.get("summary", "")[:100]
            options.append(discord.SelectOption(label=key, description=desc or "Category", value=key))
        super().__init__(placeholder="Choose a category to view details…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        try:
            choice = self.values[0]
            if choice == "__all__":
                # build a combined embed summarising all categories
                emb = discord.Embed(title="All Command Categories — Summary", color=discord.Color.green())
                for k, v in ASSIST_DB.items():
                    short = v.get("summary", "")
                    emb.add_field(name=k, value=short, inline=False)
                await interaction.response.edit_message(embed=emb, view=self.view)
                return

            data = ASSIST_DB.get(choice)
            if not data:
                await interaction.response.send_message("Unknown category.", ephemeral=True)
                return

            emb = discord.Embed(title=f"{choice} — Details", color=discord.Color.blurple())
            emb.description = data.get("summary", "")
            cmds = data.get("commands", {})
            # show each command and its short description (as fields)
            for cmd_sig, cmd_desc in cmds.items():
                # embed field names have limits; ensure they are short
                emb.add_field(name=cmd_sig[:256], value=cmd_desc[:1024], inline=False)

            # usage examples
            usage = data.get("usage", [])
            if usage:
                emb.add_field(name="Usage examples", value="\n".join(usage), inline=False)

            await interaction.response.edit_message(embed=emb, view=self.view)
        except Exception:
            # graceful fallback
            await interaction.response.send_message("An error occurred while fetching help.", ephemeral=True)


class AssistView(View):
    def __init__(self, *, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.add_item(AssistSelect())

        # Show all commands button
        self.add_item(Button(label="Show All Commands", style=discord.ButtonStyle.secondary, custom_id="assist_all"))
        # Usage examples button
        self.add_item(Button(label="Usage Examples", style=discord.ButtonStyle.primary, custom_id="assist_usage"))
        # Close button
        self.add_item(Button(label="Close", style=discord.ButtonStyle.danger, custom_id="assist_close"))

    @discord.ui.button(label="Open Docs", style=discord.ButtonStyle.link, url="https://example.org/docs", row=0)
    async def docs_button(self, interaction: discord.Interaction, button: Button):
        # link button included for convenience — replace URL with your docs if you have one
        # This handler won't be called for link buttons, but kept as a reference.
        pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # only allow the user who invoked to interact (the message will be ephemeral anyway)
        return True

    async def on_timeout(self):
        # disable children when view times out
        for item in self.children:
            item.disabled = True

    async def on_error(self, error: Exception, item, interaction: discord.Interaction):
        try:
            await interaction.response.send_message("An unexpected error occurred in the assistant UI.", ephemeral=True)
        except Exception:
            pass

    # override to route non-select/button custom logic
    async def _handle_button_press(self, interaction: discord.Interaction, custom_id: str):
        # not used; kept for clarity if you want programmatic routing
        pass

    @discord.ui.button(label="Show All Commands", style=discord.ButtonStyle.secondary, custom_id="assist_all_btn", row=1)
    async def _show_all_commands(self, interaction: discord.Interaction, button: Button):
        emb = discord.Embed(title="Commands — Full Reference", color=discord.Color.green())
        for cat, data in ASSIST_DB.items():
            lines = []
            for sig, desc in data.get("commands", {}).items():
                lines.append(f"**{sig}** — {desc}")
            emb.add_field(name=cat, value="\n".join(lines) or "No commands listed", inline=False)
        await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Usage Examples", style=discord.ButtonStyle.primary, custom_id="assist_usage_btn", row=1)
    async def _usage_examples(self, interaction: discord.Interaction, button: Button):
        emb = discord.Embed(title="Usage Examples", color=discord.Color.blurple())
        for cat, data in ASSIST_DB.items():
            usage = data.get("usage", [])
            if usage:
                emb.add_field(name=cat, value="\n".join(usage), inline=False)
        await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="assist_close_btn", row=1)
    async def _close(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.message.delete()
            # If delete fails (permissions), disable view instead
        except Exception:
            for x in self.children:
                x.disabled = True
            await interaction.response.edit_message(content="(Assistant closed)", embed=None, view=self)


# Register the slash command (uses `tree` app_commands tree — adjust if you named it differently)
@tree.command(name="assist", description="Open an interactive assistant describing all features and commands.")
async def assist_cmd(interaction: discord.Interaction):
    """
    /assist - shows an interactive embed with buttons and dropdowns to learn about every feature.
    """
    # Build initial embed and view
    embed = _build_summary_embed(interaction.user)
    view = AssistView()
    # send ephemeral so only the user sees it
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# If you use a custom sync/register step at startup, ensure the command is synced:
# await tree.sync()  # called elsewhere in your startup code


# App command error handler
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.exception("App command error: %s", error)
    try:
        await interaction.response.send_message(f"Error: {error}", ephemeral=True)
    except Exception:
        try:
            await interaction.followup.send(f"Error: {error}", ephemeral=True)
        except Exception:
            pass

# optionally start keep-alive thread
if os.getenv("KEEP_ALIVE", "false").lower() in ("1", "true", "yes"):
    try:
        start_keep_alive()
        logger.info("Started keep-alive web thread.")
    except Exception:
        logger.exception("Failed to start keep-alive")

# run
if __name__ == "__main__":
    if TOKEN.startswith("<PUT_"):
        logger.error("Please set your Discord token in DISCORD_TOKEN or DCTOKEN environment variable.")
    bot.run(TOKEN)
