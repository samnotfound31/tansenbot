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
import gc
import shutil
import random
import traceback
import logging
import asyncio
import time
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
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "skip_download": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "ignoreerrors": True,
    "prefer_ffmpeg": True,
    "geo_bypass": True,
    # Use TV/mweb clients — these produce stream URLs that work on cloud/datacenter IPs
    # where the default android/web clients get 403'd by YouTube's anti-bot.
    "extractor_args": {
        "youtube": {
            "player_client": ["tv_embedded", "mweb", "web"],
            "skip": ["dash", "hls"],  # prefer direct http streams; less likely to 403
        }
    },
}

# YouTube cookies — two ways to provide them:
# 1. YOUTUBE_COOKIES_FILE: direct path to a mounted cookies.txt (recommended for Docker)
#    docker run ... -v ~/cookies.txt:/app/cookies.txt -e YOUTUBE_COOKIES_FILE=/app/cookies.txt
# 2. YOUTUBE_COOKIES_B64: base64-encoded cookies.txt (good for Railway env vars)
import base64 as _b64, tempfile as _tmp, os as _os
YOUTUBE_COOKIES_FILE: Optional[str] = None

# Method 1: direct file path (Docker volume mount)
_yt_cookies_path = _os.environ.get("YOUTUBE_COOKIES_FILE", "")
if _yt_cookies_path and _os.path.isfile(_yt_cookies_path):
    YOUTUBE_COOKIES_FILE = _yt_cookies_path
    YDL_OPTS["cookiefile"] = YOUTUBE_COOKIES_FILE

# Method 2: base64-encoded string (Railway / env var)
if not YOUTUBE_COOKIES_FILE:
    _yt_cookies_b64 = _os.environ.get("YOUTUBE_COOKIES_B64", "")
    if _yt_cookies_b64:
        try:
            _cookie_bytes = _b64.b64decode(_yt_cookies_b64)
            _cookie_file = _tmp.NamedTemporaryFile(delete=False, suffix=".txt", mode="wb")
            _cookie_file.write(_cookie_bytes)
            _cookie_file.close()
            YOUTUBE_COOKIES_FILE = _cookie_file.name
            YDL_OPTS["cookiefile"] = YOUTUBE_COOKIES_FILE
        except Exception:
            pass


FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 10"
    " -buffer_size 16384k -thread_queue_size 4096"
)
FFMPEG_OPTS = "-vn -nostdin"

# Auto-detect FFmpeg — works on any machine; falls back to the Windows path
FFMPEG_PATH: str = (
    shutil.which("ffmpeg")
    or r"C:\ffmpeg\ffmpeg.exe"
)

# runtime state copied from original
now_playing: Dict[int, Optional[Dict[str, Any]]] = {}      # guild_id -> song dict
voice_locks: Dict[int, asyncio.Lock] = {}                  # serialize per-guild play_next
last_music_channel: Dict[int, discord.TextChannel] = {}    # guild_id -> last command's text channel
last_now_playing_messages: Dict[int, Dict[str, int]] = {}  # guild_id -> {"channel_id": int, "message_id": int}
guild_play_start: Dict[int, float] = {}                    # guild_id -> time.time() when current song started
guild_paused_duration: Dict[int, float] = {}               # guild_id -> total seconds spent paused
guild_pause_start: Dict[int, Optional[float]] = {}         # guild_id -> time.time() when last paused (or None)
guild_np_update_task: Dict[int, asyncio.Task] = {}         # guild_id -> live embed refresh task
guild_synced_lyrics: Dict[int, List[Tuple[float, str]]] = {}  # guild_id -> parsed LRC lines [(seconds, text)]
guild_lyrics_enabled: Dict[int, bool] = {}                 # guild_id -> whether karaoke window is visible

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
    - Safely disconnects any locked/existing clients to prevent Code 4017 errors using Discord's latest DAVE protocol
    - Uses supported `self_deaf=True` kwarg directly.
    """
    guild = channel.guild
    # If there's already a voice client in the guild, forcefully disconnect it
    vc_existing = getattr(guild, "voice_client", None)
    if vc_existing:
        try:
            await vc_existing.disconnect(force=True)
            await asyncio.sleep(0.5)
        except Exception:
            pass

    try:
        vc = await channel.connect(self_deaf=True, timeout=20.0, reconnect=True)
        logger.info("Connected to voice in guild %s", guild.id)
        return vc
    except Exception as e:
        logger.error("Failed to connect to voice channel: %s", e)
        raise RuntimeError(f"Failed to connect to voice channel: {e}")


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
            # Re-assert self-deaf after moving — move_to can drop the state
            try:
                await interaction.guild.change_voice_state(channel=s_channel, self_deaf=True)
            except Exception:
                pass
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
    loop = asyncio.get_running_loop()

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

async def _resolve_via_invidious(query: str) -> Optional[str]:
    """Search for a YouTube video → return a direct watch URL.

    Priority:
    1. YouTube Data API v3 (YOUTUBE_API_KEY env var) — official, no IP restrictions
    2. Public Invidious instances — open-source frontend, may be unreliable
    """
    import urllib.request, urllib.parse, json as _json, os as _os

    def _fetch() -> Optional[str]:
        # ── Method 1: YouTube Data API v3 ──────────────────────────────────────
        api_key = _os.environ.get("YOUTUBE_API_KEY", "")
        if api_key:
            try:
                params = urllib.parse.urlencode({
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "maxResults": "1",
                    "key": api_key,
                })
                req = urllib.request.Request(
                    f"https://www.googleapis.com/youtube/v3/search?{params}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = _json.loads(resp.read())
                    items = data.get("items", [])
                    if items:
                        vid_id = items[0].get("id", {}).get("videoId")
                        if vid_id:
                            return f"https://www.youtube.com/watch?v={vid_id}"
            except Exception:
                pass  # fall through to Invidious

        # ── Method 2: Invidious public instances ───────────────────────────────
        INSTANCES = [
            "https://inv.nadeko.net",
            "https://invidious.io.lol",
            "https://invidious.privacyredirect.com",
            "https://iv.datura.network",
            "https://i.nvidious.eu.org",
            "https://invidious.perennialte.ch",
        ]
        params = urllib.parse.urlencode({"q": query, "type": "video", "page": "1"})
        for base in INSTANCES:
            try:
                req = urllib.request.Request(
                    f"{base}/api/v1/search?{params}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = _json.loads(resp.read())
                    if isinstance(data, list) and data:
                        vid_id = data[0].get("videoId")
                        if vid_id:
                            return f"https://www.youtube.com/watch?v={vid_id}"
            except Exception:
                continue
        return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


# Create audio source helper (resolve a playable URL with yt-dlp if needed)
# Replace or add this helper. It centralizes yt-dlp extraction + ffmpeg creation and retries.
async def make_discord_audio_source(song: Dict[str, Any], *, retry_count: int = 3, volume: float = 1.0):
    import yt_dlp
    import discord

    YDL_OPTS_LOCAL = {
        # Use bestaudio/best without format restrictions — let yt-dlp pick the best
        # available format. The ANDROID_VR client (auto-selected by yt-dlp) works
        # well on cloud IPs and has compatible audio formats.
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        # NOTE: do NOT set ignoreerrors=True here — it makes yt-dlp silently return
        # None on failure, hiding errors and making all extractions look like timeouts.
        # Exceptions are caught in the _extract() try/except below instead.
        "default_search": "ytsearch",
        "extract_flat": False,
        "skip_download": True,
        "noprogress": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        # Do NOT specify extractor_args/player_client here — yt-dlp auto-selects
        # ANDROID_VR which works on Oracle/cloud IPs without format restrictions.
    }
    # Inject YouTube cookies if available (critical for cloud/datacenter IPs)
    if YOUTUBE_COOKIES_FILE:
        YDL_OPTS_LOCAL["cookiefile"] = YOUTUBE_COOKIES_FILE

    # Reconnect flags are REQUIRED for YouTube CDN streams (prevents 403 after handshake)
    # NOTE: Do NOT add -user_agent here — single quotes crash FFmpeg on Windows.
    # The user-agent is already set in http_headers during yt-dlp extraction.
    before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    options = "-vn -nostdin"
    executable_path = FFMPEG_PATH  # auto-detected: shutil.which("ffmpeg") or C:\ffmpeg\ffmpeg.exe

    if song.get("is_local") and song.get("local_path"):
        lp = song["local_path"]
        try:
            ff = discord.FFmpegPCMAudio(lp, before_options=None, options="-vn -nostdin", executable=executable_path)
            src = discord.PCMVolumeTransformer(ff, volume)
            return src
        except Exception as exc:
            logger.exception("Failed to create local audio source")
            raise

    # Prefer stream_query / spotify_url over 'url' — the stored 'url' may be a
    # stale YouTube CDN link (googlevideo.com) that immediately 403s.
    # stream_query is always a search term or a stable youtube.com/watch page URL.
    raw_url = song.get("url", "")
    is_stale_cdn = isinstance(raw_url, str) and ("googlevideo.com" in raw_url or "&expire=" in raw_url)
    if is_stale_cdn:
        query = song.get("stream_query") or song.get("spotify_url") or raw_url
    else:
        query = song.get("stream_query") or raw_url or song.get("spotify_url")
    if not query:
        raise RuntimeError("No URL or stream query available for song")

    logger.info("[audio] Extracting stream for '%s' using query: %s (stale_cdn=%s)",
                song.get("title"), query[:80] if query else "None", is_stale_cdn)

    loop = asyncio.get_running_loop()
    def _extract(q):
        try:
            with yt_dlp.YoutubeDL(YDL_OPTS_LOCAL) as ydl:
                info = ydl.extract_info(q, download=False)
                if isinstance(info, dict):
                    if "entries" in info and info["entries"]:
                        return info["entries"][0]
                    return info
                return None
        except Exception:
            return None

    # Build a search fallback query from song metadata (title + artists)
    _title = song.get("title", "")
    _artists = song.get("artists") or []
    if isinstance(_artists, list):
        _artists = ", ".join(_artists)
    search_fallback = f"ytsearch1:{_title} {_artists}".strip() if _title else None
    # Build a clean text query for Invidious fallback (title + artists)
    _invidious_query = f"{_title} {_artists}".strip() if _title else None

    last_exc = None
    attempt = 0
    active_query = query
    while attempt < retry_count:
        attempt += 1
        info = await loop.run_in_executor(None, _extract, active_query)
        if not info:
            last_exc = RuntimeError("yt-dlp returned no info")
            logger.warning("[audio] yt-dlp returned no info (attempt %d) for: %s", attempt, active_query[:80])
            # On first failure, try the search fallback if available
            if attempt == 1 and search_fallback and active_query != search_fallback:
                logger.info("[audio] Falling back to ytsearch: %s", search_fallback)
                active_query = search_fallback
            else:
                await asyncio.sleep(0.4 * attempt)
            continue

        # Try to get a direct audio URL from the formats list
        stream_url = None
        if isinstance(info.get("formats"), list):
            # Prefer audio-only formats with the best quality
            for f in reversed(info["formats"]):
                if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none") and f.get("url"):
                    stream_url = f["url"]
                    break
            # Fallback: any format with a URL
            if not stream_url:
                for f in reversed(info["formats"]):
                    if f.get("url"):
                        stream_url = f["url"]
                        break
        if not stream_url:
            stream_url = info.get("url") or info.get("webpage_url") or None

        if not stream_url:
            last_exc = RuntimeError("No usable stream URL")
            logger.warning("[audio] No stream URL found in yt-dlp result (attempt %d) for '%s'",
                           attempt, song.get("title"))
            await asyncio.sleep(0.3 * attempt)
            continue

        logger.info("[audio] Got stream URL (attempt %d), starting FFmpeg for '%s'",
                    attempt, song.get("title"))

        try:
            ff = discord.FFmpegPCMAudio(
                stream_url,
                before_options=before_options,
                options=options,
                executable=executable_path
            )
            src = discord.PCMVolumeTransformer(ff, volume)
            return src
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.5 * attempt)
            continue

    # ── Invidious fallback ────────────────────────────────────────────────────────
    # yt-dlp search fails on datacenter IPs (Oracle, Railway etc.) because YouTube
    # requires sign-in for search. Invidious is an open-source YouTube frontend whose
    # search API is publicly accessible and not IP-gated.
    # Once we get the video ID from Invidious, we use the direct YouTube watch URL
    # which yt-dlp CAN extract (Android/VR client path, works without sign-in).
    if _invidious_query:
        logger.info("[audio] Trying Invidious fallback for: %s", _invidious_query[:80])
        yt_url = await _resolve_via_invidious(_invidious_query)
        if yt_url:
            logger.info("[audio] Invidious resolved to: %s", yt_url)
            info = await loop.run_in_executor(None, _extract, yt_url)
            if info:
                stream_url = None
                if isinstance(info.get("formats"), list):
                    for f in reversed(info["formats"]):
                        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none") and f.get("url"):
                            stream_url = f["url"]
                            break
                    if not stream_url:
                        for f in reversed(info["formats"]):
                            if f.get("url"):
                                stream_url = f["url"]
                                break
                if not stream_url:
                    stream_url = info.get("url") or info.get("webpage_url")
                if stream_url:
                    logger.info("[audio] Invidious fallback succeeded for '%s'", song.get("title"))
                    try:
                        ff = discord.FFmpegPCMAudio(stream_url, before_options=before_options, options=options, executable=executable_path)
                        return discord.PCMVolumeTransformer(ff, volume)
                    except Exception:
                        pass

    raise RuntimeError(f"Failed to build audio source for {query}: {last_exc}")


# Per-guild playback locks — prevents multiple concurrent play loops
_playback_locks: Dict[int, asyncio.Lock] = {}

def _get_playback_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in _playback_locks:
        _playback_locks[guild_id] = asyncio.Lock()
    return _playback_locks[guild_id]

# After-playback scheduler
async def play_next_in_guild(guild: discord.Guild, text_ch: Optional[discord.TextChannel] = None):
    lock = _get_playback_lock(guild.id)

    # If already running for this guild, do nothing — prevents doubled-up loops
    if lock.locked():
        logger.debug("play_next_in_guild already running for guild %s, skipping.", guild.id)
        return

    async with lock:
        try:
            import time

            settings = load_guild_settings(str(guild.id))
            is_looping = settings.get("is_looping", False)

            while True:
                q = load_queue(str(guild.id)) or []
                if not q:
                    break

                song = q.pop(0)
                save_queue(str(guild.id), q)  # persist the pop immediately

                # Build audio source
                try:
                    volume = float(load_guild_settings(str(guild.id)).get("volume_level", 1.0))
                    src = await make_discord_audio_source(song, retry_count=3, volume=volume)
                except Exception:
                    logger.exception("Failed to create audio source for '%s' — skipping.", song.get("title"))
                    if text_ch:
                        try:
                            await text_ch.send(f"⚠️ Could not stream **{song.get('title', 'Unknown')}** — skipping.")
                        except Exception:
                            pass
                    continue  # try next song in queue

                # Ensure VC is still connected
                vc = vc_for_guild(guild)
                if not vc or not vc.is_connected():
                    logger.warning("Voice client gone for guild %s — aborting playback.", guild.id)
                    # put the song back so it can be replayed when bot reconnects
                    q2 = load_queue(str(guild.id)) or []
                    q2.insert(0, song)
                    save_queue(str(guild.id), q2)
                    return

                # Set up completion event
                play_finished = asyncio.Event()
                loop_ref = asyncio.get_running_loop()

                def _after_play(err):
                    if err:
                        logger.error("Playback error for '%s': %s", song.get("title"), err)
                    try:
                        loop_ref.call_soon_threadsafe(play_finished.set)
                    except Exception:
                        pass

                # Update now playing state
                set_now_playing(guild.id, song)
                guild_play_start[guild.id] = time.time()  # track when song started for progress bar

                # Cancel any previous embed update task for this guild
                old_task = guild_np_update_task.pop(guild.id, None)
                if old_task and not old_task.done():
                    old_task.cancel()

                # Send Now Playing embed and capture the message for live editing
                embed_ch = text_ch or last_music_channel.get(guild.id)
                np_message: Optional[discord.Message] = None
                if embed_ch:
                    try:
                        view = NowPlayingView(guild.id)
                        embed = create_now_playing_embed(song, guild_id=guild.id)
                        np_message = await embed_ch.send(embed=embed, view=view)
                    except Exception:
                        logger.exception("Failed to send Now Playing embed for '%s'", song.get("title"))

                # Start live update loop (updates progress bar + equalizer every 5s)
                if np_message:
                    task = asyncio.create_task(
                        _np_update_loop(guild.id, np_message, song)
                    )
                    guild_np_update_task[guild.id] = task

                # Lyrics are NOT shown until the user presses the Lyrics button.
                guild_synced_lyrics.pop(guild.id, None)   # clear previous song's lyrics
                guild_lyrics_enabled.pop(guild.id, None)  # reset toggle for new song
                guild_paused_duration.pop(guild.id, None) # reset pause tracking
                guild_pause_start.pop(guild.id, None)

                # Start playback — disable GC to prevent audio thread jitter
                start_time = time.time()
                try:
                    gc.disable()
                    vc.play(src, after=_after_play)
                except Exception:
                    gc.enable()
                    logger.exception("vc.play() failed for '%s'", song.get("title"))
                    continue

                # Wait for the song to finish — GC re-enabled in finally so it's always restored
                dur = song.get("duration") or 600
                try:
                    await asyncio.wait_for(play_finished.wait(), timeout=dur + 60)
                except asyncio.TimeoutError:
                    logger.warning("Playback timed out for '%s'", song.get("title"))
                    try:
                        if vc.is_playing():
                            vc.stop()
                    except Exception:
                        pass
                finally:
                    gc.enable()   # always restore GC after playback ends
                    gc.collect()  # collect any objects that built up during playback

                elapsed = time.time() - start_time

                # Fast-abort: FFmpeg died instantly (YouTube 403 / broken URL)
                if elapsed < 2.0:
                    logger.error("'%s' aborted in %.1fs — stream likely blocked (403). Breaking loop.", song.get("title"), elapsed)
                    if text_ch:
                        try:
                            await text_ch.send(f"⚠️ **{song.get('title')}** couldn't be streamed (blocked by YouTube). Skipping.")
                        except Exception:
                            pass
                    continue  # try next song, don't re-add this one

                # Reload looping setting FIRST — catches changes made mid-song via button or /loop
                settings = load_guild_settings(str(guild.id))
                is_looping = settings.get("is_looping", False)

                # If loop mode is on, re-add song to front of queue
                if is_looping:
                    q2 = load_queue(str(guild.id)) or []
                    q2.insert(0, song)
                    save_queue(str(guild.id), q2)

            # Queue exhausted
            set_now_playing(guild.id, None)
            guild_play_start.pop(guild.id, None)
            guild_paused_duration.pop(guild.id, None)
            guild_pause_start.pop(guild.id, None)
            guild_synced_lyrics.pop(guild.id, None)
            guild_lyrics_enabled.pop(guild.id, None)
            # Cancel live embed updates
            t = guild_np_update_task.pop(guild.id, None)
            if t and not t.done():
                t.cancel()
            logger.debug("Playback queue empty for guild %s.", guild.id)

        except Exception:
            logger.exception("Unexpected error in play_next_in_guild")


# Embed builder
def _make_progress_bar(elapsed: float, total: float, width: int = 18) -> str:
    """Returns a ──────●────── style progress bar."""
    if not total or total <= 0:
        return "─" * width
    ratio = min(1.0, elapsed / total)
    pos = int(ratio * width)
    bar = "─" * pos + "●" + "─" * (width - pos)
    return bar

def _make_equalizer() -> str:
    """Returns a random-looking Unicode equalizer bar for visual effect."""
    chars = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    # 10 bars, randomly picked to simulate movement each time embed is built
    return " ".join(random.choice(chars) for _ in range(10))

def create_now_playing_embed(song: Dict[str, Any], guild_id: Optional[int] = None) -> discord.Embed:
    title    = song.get("title") or "Unknown"
    artists  = song.get("artists") or []
    artists_str = ", ".join(artists) if isinstance(artists, list) else str(artists)
    album    = song.get("album") or ""
    duration = song.get("duration") or 0
    sp_url   = song.get("spotify_url")
    source   = song.get("source") or "YouTube"
    requester = song.get("requester") or ""

    # Live progress bar — subtract any accumulated pause time
    start = guild_play_start.get(guild_id) if guild_id else None
    paused_total = guild_paused_duration.get(guild_id, 0.0) if guild_id else 0.0
    pause_start = guild_pause_start.get(guild_id) if guild_id else None
    if pause_start:  # currently paused RIGHT NOW: add the ongoing pause segment
        paused_total += time.time() - pause_start
    elapsed = max(0.0, (time.time() - start) - paused_total) if start else 0.0
    elapsed = min(elapsed, duration) if duration else elapsed
    bar = _make_progress_bar(elapsed, duration)
    elapsed_str = format_mmss(int(elapsed))
    total_str   = format_mmss(duration)

    # Equalizer animation — freeze bars while paused
    is_currently_paused = bool(pause_start) if guild_id else False
    eq = "▄ ▄ ▄ ▄ ▄ ▄ ▄ ▄ ▄ ▄" if is_currently_paused else _make_equalizer()

    # Source badge
    if "Spotify" in source:
        source_badge = "🟢 Spotify → YouTube"
    elif "local" in source.lower():
        source_badge = "💾 Local File"
    else:
        source_badge = "🔴 YouTube"

    # Title line (linked if Spotify URL available)
    name_line = f"[{title}]({sp_url})" if sp_url else title

    # Build description
    lines = [
        f"### 🎵 {name_line}",
    ]
    if artists_str:
        lines.append(f"**{artists_str}**")
    if album:
        lines.append(f"*{album}*")

    lines.append("")  # spacer

    # Equalizer
    lines.append(f"`{eq}`")

    # Progress bar
    lines.append(f"`{elapsed_str}  {bar}  {total_str}`")

    lines.append("")  # spacer

    # Meta row
    meta = []
    if requester:
        meta.append(f"👤 {requester}")
    meta.append(source_badge)
    if meta:
        lines.append("  ·  ".join(meta))

    embed = discord.Embed(
        description="\n".join(lines),
        color=0x1DB954  # Spotify green — vibrant music colour
    )

    # Thumbnail
    if song.get("thumbnail"):
        try:
            embed.set_thumbnail(url=song["thumbnail"])
        except Exception:
            pass

    # Footer
    embed.set_footer(text="🎧 Tansen Music  •  Use the buttons below to control playback")

    # ── Synced Lyrics Karaoke Window (only when user has enabled it) ───────────
    synced = guild_synced_lyrics.get(guild_id, []) if guild_id else []
    show_lyrics = guild_lyrics_enabled.get(guild_id, False) if guild_id else False
    if show_lyrics and synced and start and elapsed > 0:
        # Find current line
        current_idx = 0
        for i, (ts, _) in enumerate(synced):
            if ts <= elapsed:
                current_idx = i
        # Build 5-line window around current
        window_start = max(0, current_idx - 2)
        window = synced[window_start: window_start + 5]
        lyric_lines = []
        for wi, (_, line) in enumerate(window):
            if not line:
                continue
            actual = window_start + wi
            if actual == current_idx:
                lyric_lines.append(f"**\u25b6 {line} ◀**")
            else:
                lyric_lines.append(f" {line}")
        if lyric_lines:
            embed.add_field(
                name="🎤 Lyrics",
                value="\n".join(lyric_lines),
                inline=False,
            )

    return embed

# ─── Synced Lyrics (lrclib.net) ──────────────────────────────────────────────

async def fetch_synced_lyrics(
    title: str,
    artists: List[str],
    duration: int,
) -> List[Tuple[float, str]]:
    """
    Fetch timestamped LRC lyrics from lrclib.net.
    Returns list of (timestamp_seconds, lyric_line) tuples.
    Falls back to a fuzzy search if exact match has no syncedLyrics.
    """
    import re as _re
    artist = artists[0] if artists else ""
    loop = asyncio.get_running_loop()

    def _get(url: str, params: dict):
        try:
            r = requests.get(url, params=params, timeout=6)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    # 1. Exact match
    data = await loop.run_in_executor(
        None, _get,
        "https://lrclib.net/api/get",
        {"artist_name": artist, "track_name": title, "duration": duration},
    )

    # 2. Fuzzy search fallback (picks first result with syncedLyrics)
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

    # Parse LRC timestamps: [MM:SS.mm] text
    pattern = _re.compile(r"\[(\d+):(\d+\.\d+)\]\s*(.*)")
    lines: List[Tuple[float, str]] = []
    for m in pattern.finditer(synced):
        mins, secs, text = m.groups()
        ts = int(mins) * 60 + float(secs)
        lines.append((ts, text.strip()))
    return lines


async def _fetch_and_store_lyrics(guild_id: int, song: Dict[str, Any]) -> None:
    """Background task: fetch synced lyrics and store them for the guild."""
    title   = song.get("title") or ""
    artists = song.get("artists") or []
    dur     = int(song.get("duration") or 0)
    try:
        lines = await fetch_synced_lyrics(title, artists, dur)
        if lines:
            guild_synced_lyrics[guild_id] = lines
            logger.info("Synced lyrics: %d lines loaded for '%s'", len(lines), title)
        else:
            guild_synced_lyrics.pop(guild_id, None)
            logger.debug("No synced lyrics found for '%s'", title)
    except Exception:
        logger.exception("Error fetching synced lyrics for '%s'", title)
        guild_synced_lyrics.pop(guild_id, None)

async def _np_update_loop(
    guild_id: int,
    message: discord.Message,
    song: Dict[str, Any],
) -> None:
    try:
        while True:
            await asyncio.sleep(5)
            # Stop if the song changed or nothing is playing
            current = now_playing.get(guild_id)
            if not current or current.get("title") != song.get("title"):
                break
            # Skip update cycle when paused — nothing would visually change
            vc_check = discord.utils.get(bot.voice_clients, guild__id=guild_id)
            if vc_check and vc_check.is_paused():
                continue
            try:
                new_embed = create_now_playing_embed(current, guild_id=guild_id)
                await message.edit(embed=new_embed)
            except discord.NotFound:
                break  # message deleted
            except discord.HTTPException:
                pass   # rate limit or transient — just try again next cycle
            except Exception:
                break
    except asyncio.CancelledError:
        pass

# NowPlaying View (buttons)
class NowPlayingView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.refresh_buttons()

    def _get_vc(self) -> Optional[discord.VoiceClient]:
        guild = bot.get_guild(int(self.guild_id))
        return vc_for_guild(guild) if guild else None

    def refresh_buttons(self):
        settings = load_guild_settings(str(self.guild_id))
        looping = settings.get("is_looping", False)
        volume = float(settings.get("volume_level", 1.0))
        vc = self._get_vc()
        is_playing = vc.is_playing() if vc else False
        is_paused = vc.is_paused() if vc else False

        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            cid = getattr(child, "custom_id", "")

            # Dynamic Play/Pause: green = playing, red = paused/stopped
            if cid == "tansen:pause_resume":
                if is_playing:
                    child.label = "⏸ Pause"
                    child.style = discord.ButtonStyle.success   # green
                elif is_paused:
                    child.label = "▶ Resume"
                    child.style = discord.ButtonStyle.danger    # red
                else:
                    child.label = "⏯ Play/Pause"
                    child.style = discord.ButtonStyle.secondary

            # Loop button: green when on, grey when off
            elif cid == "tansen:loop":
                child.label = f"🔁 Loop {'ON' if looping else 'OFF'}"
                child.style = discord.ButtonStyle.success if looping else discord.ButtonStyle.secondary

            # Volume buttons: show current volume in label
            elif cid == "tansen:vol_down":
                pct = max(0, int(volume * 100) - 10)
                child.label = f"🔉 -{10}% ({int(volume*100)}%)"
            elif cid == "tansen:vol_up":
                pct = min(200, int(volume * 100) + 10)
                child.label = f"🔊 +{10}% ({int(volume*100)}%)"

    async def refresh_message(self, interaction: discord.Interaction):
        self.refresh_buttons()
        try:
            if interaction and interaction.message:
                await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.success, custom_id="tansen:pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = vc_for_guild(interaction.guild)
        if not vc:
            await interaction.response.send_message("I'm not connected.", ephemeral=True)
            return
        try:
            if vc.is_playing():
                vc.pause()
                # Record when this pause started
                guild_pause_start[interaction.guild.id] = time.time()
                await interaction.response.send_message("⏸ Paused.", ephemeral=True)
            elif vc.is_paused():
                vc.resume()
                # Commit the paused segment
                ps = guild_pause_start.pop(interaction.guild.id, None)
                if ps:
                    guild_paused_duration[interaction.guild.id] = (
                        guild_paused_duration.get(interaction.guild.id, 0.0)
                        + (time.time() - ps)
                    )
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

    @discord.ui.button(label="🔁 Loop OFF", style=discord.ButtonStyle.secondary, custom_id="tansen:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_guild_settings(str(interaction.guild.id))
        current = bool(settings.get("is_looping", False))
        new_val = not current
        save_guild_settings(str(interaction.guild.id), settings.get("volume_level", 1.0), new_val, settings.get("last_played"), settings.get("previous_played"))
        await interaction.response.send_message(f"Loop is now **{'ON' if new_val else 'OFF'}**.", ephemeral=True)
        await self.refresh_message(interaction)

    @discord.ui.button(label="🔉 -10% (100%)", style=discord.ButtonStyle.secondary, custom_id="tansen:vol_down")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_guild_settings(str(self.guild_id))
        volume = float(settings.get("volume_level", 1.0))
        volume = max(0.0, round(volume - 0.10, 2))
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
        except Exception:
            pass
        await self.refresh_message(interaction)

    @discord.ui.button(label="🔊 +10% (100%)", style=discord.ButtonStyle.secondary, custom_id="tansen:vol_up")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_guild_settings(str(self.guild_id))
        volume = float(settings.get("volume_level", 1.0))
        volume = min(2.0, round(volume + 0.10, 2))
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
        except Exception:
            pass
        await self.refresh_message(interaction)

    @discord.ui.button(label="📋 Queue", style=discord.ButtonStyle.secondary, custom_id="tansen:show_queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = peek_queue(interaction.guild.id)
        current = now_playing.get(interaction.guild.id)
        if not q and not current:
            await interaction.response.send_message("Queue is empty and nothing is playing.", ephemeral=True)
            return
        lines = []
        if current:
            title = current.get("title") or "Unknown"
            artists = current.get("artists") or []
            if isinstance(artists, list):
                artists = ", ".join(artists)
            artists_str = f" — {artists}" if artists else ""
            dur = format_mmss(current.get("duration"))
            lines.append(f"▶ **Now Playing:** {title}{artists_str} [{dur}]")
        if q:
            lines.append(f"\n**Up Next** ({len(q)} song{'s' if len(q) != 1 else ''}):")
            for i, s in enumerate(q[:24], start=1):
                lines.append(format_song_line(s, i))
            if len(q) > 24:
                lines.append(f"*...and {len(q) - 24} more*")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="tansen:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = vc_for_guild(interaction.guild)
        delete_queue(str(interaction.guild.id))
        set_now_playing(interaction.guild.id, None)
        guild_play_start.pop(interaction.guild.id, None)
        # Cancel live embed update task
        t = guild_np_update_task.pop(interaction.guild.id, None)
        if t and not t.done():
            t.cancel()
        guild_synced_lyrics.pop(interaction.guild.id, None)
        guild_lyrics_enabled.pop(interaction.guild.id, None)
        if vc and vc.is_connected():
            try:
                vc.stop()
                await vc.disconnect()
            except Exception:
                pass
        await interaction.response.send_message("Stopped and cleared queue.", ephemeral=True)
        await self.refresh_message(interaction)


    @discord.ui.button(label="🎤 Lyrics", style=discord.ButtonStyle.secondary, custom_id="tansen:lyrics")
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle synced karaoke lyrics in the Now Playing embed."""
        song = now_playing.get(interaction.guild.id)
        if not song:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild.id
        currently_enabled = guild_lyrics_enabled.get(gid, False)

        if currently_enabled:
            # Toggle OFF
            guild_lyrics_enabled[gid] = False
            await interaction.followup.send("🎤 Lyrics hidden.", ephemeral=True)
        else:
            # Toggle ON — fetch lyrics first if not yet loaded
            if not guild_synced_lyrics.get(gid):
                await interaction.followup.send("⏳ Fetching synced lyrics…", ephemeral=True)
                await _fetch_and_store_lyrics(gid, song)

            if guild_synced_lyrics.get(gid):
                guild_lyrics_enabled[gid] = True
                await interaction.followup.send("🎤 Synced lyrics enabled! They'll appear in the Now Playing embed.", ephemeral=True)
            else:
                await interaction.followup.send("❌ No synced lyrics found for this song.", ephemeral=True)
                return

        # Force an immediate embed refresh so the change shows instantly
        info = last_now_playing_messages.get(gid)
        if info:
            try:
                ch = bot.get_channel(info["channel_id"])
                if ch:
                    msg = await ch.fetch_message(info["message_id"])
                    new_embed = create_now_playing_embed(song, guild_id=gid)
                    await msg.edit(embed=new_embed)
            except Exception:
                pass


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
                last_music_channel[interaction.guild.id] = interaction.channel
                bot.loop.create_task(play_next_in_guild(interaction.guild, interaction.channel))

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
    # Re-register persistent views so buttons in old messages still work after restart
    # guild_id=0 is a dummy — only the custom_ids matter for routing interactions
    try:
        bot.add_view(NowPlayingView(0))
        logger.info("Registered persistent NowPlayingView.")
    except Exception:
        logger.exception("Failed to register persistent view")
    # Clear all persisted queues on startup — prevents leftover songs from prior sessions auto-playing
    try:
        from database import connect, DB_LOCK
        with DB_LOCK:
            with connect() as conn:
                conn.execute("DELETE FROM queues")
                conn.commit()
        logger.info("Cleared all guild queues on startup.")
    except Exception:
        logger.exception("Failed to clear queues on startup")
    try:
        await bot.tree.sync()
    except Exception:
        logger.exception("Failed to sync tree")

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Re-deafen the bot instantly if someone un-deafens it."""
    if member.id != bot.user.id:
        return
    # Bot was un-self-deafened
    if before.self_deaf and not after.self_deaf:
        try:
            channel = after.channel or before.channel
            if channel:
                await member.guild.change_voice_state(channel=channel, self_deaf=True)
                logger.info("Re-asserted self-deaf for guild %s", member.guild.id)
        except Exception:
            logger.exception("Failed to re-deafen bot in guild %s", member.guild.id)

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
        bot.loop.create_task(play_next_in_guild(interaction.guild, interaction.channel))
    await interaction.followup.send(f"Queued: **{title}**", ephemeral=True)

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
    current = now_playing.get(interaction.guild.id)
    if not q and not current:
        await interaction.followup.send("Queue is empty and nothing is playing.", ephemeral=True)
        return
    lines = []
    if current:
        title = current.get("title") or "Unknown"
        artists = current.get("artists") or []
        if isinstance(artists, list):
            artists = ", ".join(artists)
        artists_str = f" — {artists}" if artists else ""
        dur = format_mmss(current.get("duration"))
        lines.append(f"▶ **Now Playing:** {title}{artists_str} [{dur}]")
    if q:
        lines.append("")
        lines.append(f"**Up Next** ({len(q)} song{'s' if len(q) != 1 else ''}):")
        for i, s in enumerate(q[:24], start=1):
            lines.append(format_song_line(s, i))
        if len(q) > 24:
            lines.append(f"*...and {len(q) - 24} more*")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

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
    embed = create_now_playing_embed(song, guild_id=interaction.guild.id)
    view = NowPlayingView(interaction.guild.id)
    await interaction.response.send_message(embed=embed, view=view)
    try:
        msg = await interaction.original_response()
        last_now_playing_messages[interaction.guild.id] = {"channel_id": msg.channel.id, "message_id": msg.id}
    except Exception:
        pass

@tree.command(name="nowrics", description="Toggle synced karaoke lyrics in the Now Playing embed.")
async def nowrics_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    song = now_playing.get(interaction.guild.id)
    if not song:
        await interaction.followup.send("Nothing is playing right now.", ephemeral=True)
        return

    gid = interaction.guild.id
    currently_enabled = guild_lyrics_enabled.get(gid, False)

    if currently_enabled:
        guild_lyrics_enabled[gid] = False
        await interaction.followup.send("🎤 Synced lyrics hidden.", ephemeral=True)
    else:
        if not guild_synced_lyrics.get(gid):
            await interaction.followup.send("⏳ Fetching synced lyrics…", ephemeral=True)
            await _fetch_and_store_lyrics(gid, song)

        if guild_synced_lyrics.get(gid):
            guild_lyrics_enabled[gid] = True
            await interaction.followup.send("🎤 Synced lyrics enabled! They'll appear in the Now Playing embed.", ephemeral=True)
        else:
            await interaction.followup.send("❌ No synced lyrics found for this song.", ephemeral=True)
            return

    # Force immediate embed refresh
    info = last_now_playing_messages.get(gid)
    if info:
        try:
            ch = bot.get_channel(info["channel_id"])
            if ch:
                msg = await ch.fetch_message(info["message_id"])
                new_embed = create_now_playing_embed(song, guild_id=gid)
                await msg.edit(embed=new_embed)
        except Exception:
            pass

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

# ---------------- Playlist commands & improved /playpl ----------------
# Existing helpers expected in your bot file:
# add_song_to_queue(guild_id, song_or_list, requester)
# spotify_track_to_song_dict(track, requester, guild_id)
# get_app_spotify_token()
# vc_for_guild(guild)
# play_next_in_guild(guild)
# format_mmss(duration_seconds)

# If your logger variable is named differently, adjust accordingly.
# This uses 'logger' as in your project logs.

# ----- /savequeue -----
@tree.command(name="savequeue", description="Save the current guild queue as a named playlist for you.")
@app_commands.describe(name="Name to save the playlist as", description="Short description (optional)")
async def savequeue_cmd(interaction: discord.Interaction, name: str, description: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if not interaction.guild:
            await interaction.followup.send("This command must be used inside a guild.", ephemeral=True)
            return

        # get current queue (expects load_queue to return list of song dicts)
        guild_id = str(interaction.guild.id)
        try:
            current_queue = load_queue(guild_id)
        except Exception as e:
            logger.exception("Failed to load queue for savequeue")
            current_queue = None

        if not current_queue:
            await interaction.followup.send("There is no queue to save (empty queue).", ephemeral=True)
            return

        # Save via DB function; we store the raw song dict list (JSON-serializable)
        save_playlist(str(interaction.user.id), name, description or "", current_queue)
        await interaction.followup.send(f"✅ Saved current queue as playlist **{name}**.", ephemeral=True)
    except Exception as e:
        logger.exception("savequeue command failed")
        await interaction.followup.send("An unexpected error occurred while saving the playlist.", ephemeral=True)


# ----- /myplaylists (interactive dropdown) -----
class MyPlaylistsSelect(discord.ui.Select):
    def __init__(self, author_id: int, playlists: Dict[str, Any]):
        # playlists: {name: {"description":..., "songs":[...]} }
        opts = []
        for name, meta in playlists.items():
            desc = meta.get("description") or ""
            opts.append(
                discord.SelectOption(
                    label=(name[:100]),
                    description=(desc[:75] or "No description"),
                    value=name,
                )
            )

        if not opts:
            opts = [
                discord.SelectOption(
                    label="(no playlists)",
                    description="You have no saved playlists.",
                    value="__empty__",
                    default=True,
                )
            ]

        super().__init__(placeholder="Select a playlist to queue", min_values=1, max_values=1, options=opts)
        self.author_id = author_id
        self.playlists = playlists

    async def callback(self, interaction: discord.Interaction):
        # Only allow the original user
        if interaction.user.id != self.author_id:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("This panel is only for the user who invoked it.", ephemeral=True)
                else:
                    await interaction.followup.send("This panel is only for the user who invoked it.", ephemeral=True)
            except Exception:
                pass
            return

        # Defer once (safe)
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            logger.exception("Failed to defer in MyPlaylistsSelect.callback")

        sel = self.values[0]
        if sel == "__empty__":
            await interaction.followup.send("You have no saved playlists.", ephemeral=True)
            return

        playlist = self.playlists.get(sel)
        if not playlist:
            await interaction.followup.send("Playlist not found.", ephemeral=True)
            return

        songs = playlist.get("songs") or []
        if not songs:
            await interaction.followup.send("That playlist has no songs.", ephemeral=True)
            return

        # Ensure the user is in a voice channel
        user_vc = None
        if interaction.user.voice and interaction.user.voice.channel:
            user_vc = interaction.user.voice.channel
        else:
            await interaction.followup.send("You must be in a voice channel for me to play this playlist.", ephemeral=True)
            return

        # Try to connect (prefer your safe helper)
        try:
            existing_vc = vc_for_guild(interaction.guild) if "vc_for_guild" in globals() else None
            connected = False

            # If there's already a connected VC and it's in the same channel, reuse it
            if existing_vc and getattr(existing_vc, "channel", None) and existing_vc.channel.id == user_vc.id:
                connected = True
                vc_client = existing_vc
            else:
                # attempt connection using safe_connect_voice() if available
                if "safe_connect_voice" in globals() and callable(globals()["safe_connect_voice"]):
                    try:
                        vc_client = await safe_connect_voice(user_vc)
                        connected = vc_client is not None
                    except Exception as exc:
                        logger.exception("safe_connect_voice failed")
                        connected = False
                else:
                    try:
                        vc_client = await user_vc.connect(timeout=20)
                        connected = True
                    except Exception as exc:
                        logger.exception("Direct voice connect failed")
                        connected = False

            if not connected:
                await interaction.followup.send("Failed to connect to your voice channel. Check permissions and try again.", ephemeral=True)
                return

            # Wait briefly to let voice handshake finish (helps avoid race when starting playback immediately)
            await asyncio.sleep(0.35)

        except Exception:
            logger.exception("Voice check/connect failed in MyPlaylistsSelect.callback")
            await interaction.followup.send("Failed to ensure voice connection (see logs).", ephemeral=True)
            return

        # Validate/normalize song dicts (simple heuristics)
        playable_songs = []
        for item in songs:
            if not isinstance(item, dict):
                continue
            if item.get("is_local") and item.get("local_path"):
                playable_songs.append(item)
            elif item.get("url") or item.get("stream_query") or item.get("spotify_url"):
                playable_songs.append(item)
            # else skip invalid entry

        if not playable_songs:
            await interaction.followup.send("No playable tracks found in that playlist (invalid data).", ephemeral=True)
            return

        # Add to queue
        try:
            await add_song_to_queue(interaction.guild.id, playable_songs, interaction.user.display_name)
        except NameError:
            await interaction.followup.send("Bot integration error: add_song_to_queue not found.", ephemeral=True)
            return
        except Exception:
            logger.exception("Failed to add saved playlist songs to queue")
            await interaction.followup.send("Failed to queue the playlist (see logs).", ephemeral=True)
            return

        # Ensure we have the latest voice client reference
        try:
            vc_now = vc_for_guild(interaction.guild) if "vc_for_guild" in globals() else None
        except Exception:
            vc_now = None

        # If not playing already, start playback in background
        try:
            should_start = True
            if vc_now:
                # discord.VoiceClient has is_playing/is_paused methods
                try:
                    if vc_now.is_playing() or vc_now.is_paused():
                        should_start = False
                except Exception:
                    # if we can't query, assume we should try to start
                    should_start = True

            if should_start:
                # Start playback in background so we don't block the interaction
                try:
                    last_music_channel[interaction.guild.id] = interaction.channel
                    bot.loop.create_task(play_next_in_guild(interaction.guild, interaction.channel))
                except Exception:
                    # fallback: run in executor if loop.create_task fails
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(play_next_in_guild(interaction.guild, interaction.channel))
                    except Exception:
                        logger.exception("Failed to schedule play_next_in_guild")
        except Exception:
            logger.exception("Error while attempting to start playback")

        # Confirmation message
        await interaction.followup.send(f"✅ Queued saved playlist **{sel}** ({len(playable_songs)} playable tracks). Playback started if the queue was idle.", ephemeral=True)

class MyPlaylistsView(discord.ui.View):
    def __init__(self, author_id: int, playlists: Dict[str, Any], timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.add_item(MyPlaylistsSelect(author_id, playlists))


@tree.command(name="myplaylists", description="Show and queue playlists you've saved with the bot (interactive).")
async def myplaylists_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        pl = load_playlists(str(interaction.user.id)) or {}
    except Exception:
        logger.exception("Failed to load playlists for user")
        pl = {}

    if not pl:
        await interaction.followup.send("You have no saved playlists.", ephemeral=True)
        return

    view = MyPlaylistsView(interaction.user.id, pl)
    embed = discord.Embed(title="Your saved playlists", description="Select one from the dropdown to queue it.", color=discord.Color.blurple())
    for name, meta in list(pl.items())[:10]:
        desc = meta.get("description") or ""
        embed.add_field(name=name, value=(desc[:200] or "No description"), inline=False)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ----- /deleteplaylist -----
@tree.command(name="deleteplaylist", description="Delete a playlist you previously saved.")
@app_commands.describe(name="Name of your saved playlist to delete")
async def deleteplaylist_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        # check exists
        pl = load_playlists(str(interaction.user.id)) or {}
        if name not in pl:
            await interaction.followup.send("No playlist with that name found in your saved playlists.", ephemeral=True)
            return

        delete_playlist(str(interaction.user.id), name)
        await interaction.followup.send(f"✅ Playlist **{name}** deleted.", ephemeral=True)
    except Exception:
        logger.exception("Failed deleting playlist")
        await interaction.followup.send("Failed to delete playlist due to an error.", ephemeral=True)


# ----- Helper: fetch spotify playlist tracks (returns list of track dicts) -----
def _fetch_spotify_playlist_tracks(playlist_id: str) -> List[Dict[str, Any]]:
    token = None
    try:
        token = get_app_spotify_token()
    except Exception:
        pass
    if not token:
        raise RuntimeError("Spotify app token not available (configure SPOTIFY_CLIENT_ID/SECRET).")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params = {"limit": 100}
    all_tracks: List[Dict[str, Any]] = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"Spotify API returned {r.status_code}")
        data = r.json()
        items = data.get("items", []) or []
        for it in items:
            t = it.get("track")
            if t:
                all_tracks.append(t)
        url = data.get("next")
        params = None
    return all_tracks


# ----- /playpl upgraded: accepts saved playlist name OR spotify id/url -----
@tree.command(name="playpl", description="Queue a playlist. Accepts a saved playlist name, or a Spotify playlist id/URL.")
@app_commands.describe(playlist="Saved playlist name (bot) OR Spotify playlist ID/URL")
async def playpl_cmd(interaction: discord.Interaction, playlist: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        if not interaction.guild:
            await interaction.followup.send("This command must be used inside a server.", ephemeral=True)
            return

        # 1) Check if the provided string matches a saved playlist for the invoking user
        user_pl = load_playlists(str(interaction.user.id)) or {}
        if playlist in user_pl:
            # This is a saved playlist -> queue song dicts as-is
            songs = user_pl[playlist].get("songs") or []
            if not songs:
                await interaction.followup.send("That saved playlist contains no songs.", ephemeral=True)
                return
            await add_song_to_queue(interaction.guild.id, songs, interaction.user.display_name)
            # start playback if idle
            try:
                vc = vc_for_guild(interaction.guild)
                if vc and not (vc.is_playing() or vc.is_paused()):
                    bot.loop.create_task(play_next_in_guild(interaction.guild))
            except Exception:
                logger.exception("Failed to start playback after adding saved playlist")
            await interaction.followup.send(f"Queued saved playlist **{playlist}** ({len(songs)} songs).", ephemeral=True)
            return

        # 2) Not a saved playlist => attempt to parse Spotify playlist ID from URL or treat as id
        m = re.search(r"(?:playlist/|playlists/)([A-Za-z0-9]+)", playlist)
        playlist_id = m.group(1) if m else playlist

        # fetch tracks from spotify
        try:
            spotify_tracks = _fetch_spotify_playlist_tracks(playlist_id)
        except Exception as e:
            logger.exception("Failed to fetch spotify playlist")
            await interaction.followup.send(f"Failed to fetch Spotify playlist: {e}", ephemeral=True)
            return

        # convert spotify tracks to song dicts (resolve to stream queries)
        songs_to_queue = []
        for tr in spotify_tracks:
            try:
                # spotify_track_to_song_dict should be async and accept (track, requester, guild_id)
                # Many versions of your code earlier used spotify_track_to_song_dict(track, requester) or with guild_id.
                try:
                    song = await spotify_track_to_song_dict(tr, requester=str(interaction.user.display_name), guild_id=interaction.guild.id)
                except TypeError:
                    # fallback to older signature
                    song = await spotify_track_to_song_dict(tr, requester=str(interaction.user.display_name))
                if song:
                    songs_to_queue.append(song)
            except Exception:
                logger.exception("Failed converting spotify track to song dict")
                continue

        if not songs_to_queue:
            await interaction.followup.send("No playable tracks found in the Spotify playlist.", ephemeral=True)
            return

        # Add to queue
        await add_song_to_queue(interaction.guild.id, songs_to_queue, interaction.user.display_name)

        # Start playback if idle
        try:
            vc = vc_for_guild(interaction.guild)
            if vc and not (vc.is_playing() or vc.is_paused()):
                bot.loop.create_task(play_next_in_guild(interaction.guild))
        except Exception:
            logger.exception("Failed to start playback after queuing spotify playlist")

        await interaction.followup.send(f"Queued {len(songs_to_queue)} tracks from Spotify playlist.", ephemeral=True)

    except Exception:
        logger.exception("playpl command raised an exception")
        try:
            await interaction.followup.send("An internal error occurred while processing the playlist.", ephemeral=True)
        except Exception:
            pass

# Improved /spotify_playlists command (async + interactive)
# ======= Playlist / queue / volume helper wrappers (paste into tansenmain.py) =======
import asyncio
import re
import aiohttp
from typing import List, Dict, Any, Optional

# These functions wrap your existing database helpers (save_queue, load_queue, save_guild_settings, load_guild_settings)
# so other parts of the code can call them by nicer names.

def get_queue_for_guild(guild_id: Any) -> List[Dict[str, Any]]:
    """Return the in-DB queue list for guild_id (always returns a list)."""
    try:
        return load_queue(str(guild_id)) or []
    except Exception:
        logger.exception("get_queue_for_guild: DB load_queue failed")
        return []

def save_queue_for_guild(guild_id, queue):
    try:
        save_queue(str(guild_id), queue)
    except Exception as e:
        print("❌ save_queue_for_guild FAILED")
        print(type(e).__name__, e)
        raise  # IMPORTANT: re-raise so you SEE it



def get_volume_for_guild(guild_id: Any) -> float:
    """Return stored volume (float). Defaults to 1.0 if missing or error."""
    try:
        s = load_guild_settings(str(guild_id))
        return float(s.get("volume_level", 1.0))
    except Exception:
        logger.exception("get_volume_for_guild failed, returning 1.0")
        return 1.0

# Backwards-compatible simple names used by earlier patches:
# if your code expects get_queue_for_guild(name) -> use this alias
get_volume_for_guild = get_volume_for_guild

# If your code uses last_music_channel mapping, ensure it exists:
try:
    last_music_channel  # if defined earlier, keep it
except NameError:
    last_music_channel = {}  # guild_id -> discord.VoiceChannel reference (optional)

# ======= Spotify playlist helpers & Views (async, using aiohttp) =======

async def _fetch_user_playlists_async(access_token: str, max_items: int = 200) -> List[Dict[str, Any]]:
    """Async fetch of the user's Spotify playlists (uses aiohttp)."""
    out: List[Dict[str, Any]] = []
    url = "https://api.spotify.com/v1/me/playlists"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"limit": 50}
    try:
        async with aiohttp.ClientSession() as session:
            while url and len(out) < max_items:
                async with session.get(url, headers=headers, params=params, timeout=20) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning("Spotify playlists fetch returned %s: %s", resp.status, text[:400])
                        break
                    data = await resp.json()
                for it in data.get("items", []) or []:
                    out.append({"name": it.get("name"), "id": it.get("id"), "tracks": it.get("tracks", {}).get("total", 0)})
                    if len(out) >= max_items:
                        break
                url = data.get("next")
                params = None
    except Exception:
        logger.exception("Failed to fetch user playlists async")
    return out

async def _fetch_spotify_playlist_tracks_async(access_token: str, playlist_id: str, max_tracks: int = 1000) -> List[Dict[str, Any]]:
    """Async fetch of tracks for a Spotify playlist. Returns raw Spotify track objects."""
    out: List[Dict[str, Any]] = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"limit": 100}
    try:
        async with aiohttp.ClientSession() as session:
            while url and len(out) < max_tracks:
                async with session.get(url, headers=headers, params=params, timeout=30) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"Spotify API returned {resp.status}: {text[:400]}")
                    data = await resp.json()
                for item in data.get("items", []) or []:
                    tr = item.get("track")
                    if tr:
                        out.append(tr)
                        if len(out) >= max_tracks:
                            break
                url = data.get("next")
                params = None
    except Exception:
        logger.exception("Failed to fetch playlist tracks async")
        raise
    return out

# ---------- UI: Spotify playlist select / view ----------
class SpotifyUserPlaylistsSelect(discord.ui.Select):
    def __init__(self, author_id: int, playlists: List[Dict[str, Any]]):
        opts = []
        for p in (playlists or [])[:25]:
            raw_name = (p.get("name") or "").strip()
            safe_name = re.sub(r"[\r\n\t]+", " ", raw_name).strip()
            label = safe_name[:100] if safe_name else "Untitled playlist"
            tracks_count = p.get("tracks", 0)
            desc = f"{tracks_count} tracks"
            desc = desc[:100]
            opts.append(discord.SelectOption(label=label, description=desc, value=str(p.get("id"))))

        if not opts:
            opts = [discord.SelectOption(label="(no playlists)", description="You have no saved Spotify playlists.", value="__empty__", default=True)]

        super().__init__(placeholder="Choose a playlist to queue", min_values=1, max_values=1, options=opts)
        self.author_id = author_id
        self.playlists_map = {str(p.get("id")): p for p in (playlists or [])}

    async def callback(self, interaction: discord.Interaction):
        # Restrict to original user
        if interaction.user.id != self.author_id:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("This dialog is for the user who ran the command.", ephemeral=True)
                else:
                    await interaction.followup.send("This dialog is for the user who ran the command.", ephemeral=True)
            except Exception:
                pass
            return

        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except Exception:
                logger.exception("Failed to defer interaction in SpotifyUserPlaylistsSelect.callback")

        playlist_id = self.values[0]
        if playlist_id == "__empty__":
            await interaction.followup.send("No playlists available.", ephemeral=True)
            return

        p = self.playlists_map.get(playlist_id)
        if not p:
            await interaction.followup.send("Selected playlist not found (it may have been removed).", ephemeral=True)
            return

        # obtain token (try async getter first, then sync)
        token = None
        try:
            getter_async = globals().get("get_spotify_token_async")
            getter_sync = globals().get("get_spotify_token")
            if getter_async and asyncio.iscoroutinefunction(getter_async):
                token = await getter_async(str(interaction.user.id))
            elif getter_sync:
                loop = asyncio.get_running_loop()
                token = await loop.run_in_executor(None, getter_sync, str(interaction.user.id))
        except Exception:
            logger.exception("Failed to obtain spotify token in select callback")
            token = None

        if not token:
            await interaction.followup.send("Spotify token unavailable. You may need to re-link your account via /spotify_link.", ephemeral=True)
            return

        # fetch spotify tracks
        try:
            spotify_tracks = await _fetch_spotify_playlist_tracks_async(token, playlist_id)
        except Exception as e:
            await interaction.followup.send(f"Failed to fetch playlist tracks: {e}", ephemeral=True)
            return

        if not spotify_tracks:
            await interaction.followup.send("No playable tracks found in that playlist.", ephemeral=True)
            return

        # convert to song dicts using your helper (support two function signatures)
        songs_to_queue: List[Dict[str, Any]] = []
        for tr in spotify_tracks:
            try:
                # try the 3-arg version first, fallback to 2-arg
                if "spotify_track_to_song_dict" in globals():
                    fn = globals()["spotify_track_to_song_dict"]
                    try:
                        # prefer guild-aware signature if available
                        if hasattr(fn, "__call__"):
                            # call and await if coroutine
                            if asyncio.iscoroutinefunction(fn):
                                try:
                                    song = await fn(tr, requester=str(interaction.user.display_name), guild_id=interaction.guild.id)
                                except TypeError:
                                    song = await fn(tr, requester=str(interaction.user.display_name))
                            else:
                                # sync function
                                try:
                                    song = fn(tr, requester=str(interaction.user.display_name), guild_id=interaction.guild.id)
                                except TypeError:
                                    song = fn(tr, requester=str(interaction.user.display_name))
                        else:
                            song = None
                    except Exception:
                        logger.exception("spotify_track_to_song_dict failed for a track")
                        song = None
                else:
                    song = None
            except Exception:
                logger.exception("Error while converting spotify track to song dict")
                song = None

            if song:
                songs_to_queue.append(song)

        if not songs_to_queue:
            await interaction.followup.send("No playable tracks could be resolved from that playlist.", ephemeral=True)
            return

        # Ensure user is in voice and bot connects to same channel
        user_vc = None
        if interaction.user.voice and interaction.user.voice.channel:
            user_vc = interaction.user.voice.channel
        else:
            await interaction.followup.send("You must be in a voice channel to play music.", ephemeral=True)
            return

        # Connect (prefer safe_connect_voice if present)
        try:
            vc_client = None
            if "vc_for_guild" in globals():
                vc_client = vc_for_guild(interaction.guild)
            if not vc_client or not getattr(vc_client, "channel", None) or vc_client.channel.id != user_vc.id:
                if "safe_connect_voice" in globals() and callable(globals()["safe_connect_voice"]):
                    vc_client = await safe_connect_voice(user_vc)
                else:
                    vc_client = await user_vc.connect(timeout=20)
            # small pause to ensure readiness
            await asyncio.sleep(0.25)
        except Exception:
            logger.exception("Failed connecting to user's voice channel in SpotifyUserPlaylistsSelect.callback")
            await interaction.followup.send("Failed to connect to your voice channel. Check permissions and try again.", ephemeral=True)
            return

        # add to queue
        try:
            await add_song_to_queue(interaction.guild.id, songs_to_queue, interaction.user.display_name)
        except Exception:
            logger.exception("Failed to add spotify playlist songs to queue")
            await interaction.followup.send("Failed to add playlist to queue.", ephemeral=True)
            return

        # start playback if idle
        try:
            vc_now = vc_for_guild(interaction.guild) if "vc_for_guild" in globals() else None
            should_start = True
            if vc_now:
                try:
                    if vc_now.is_playing() or vc_now.is_paused():
                        should_start = False
                except Exception:
                    should_start = True
            if should_start:
                try:
                    bot.loop.create_task(play_next_in_guild(interaction.guild))
                except Exception:
                    logger.exception("Failed to schedule play_next_in_guild in SpotifyUserPlaylistsSelect.callback")
        except Exception:
            logger.exception("Error while attempting to start playback")

        await interaction.followup.send(f"Queued {len(songs_to_queue)} tracks from the Spotify playlist.", ephemeral=True)


class SpotifyUserPlaylistsView(discord.ui.View):
    def __init__(self, author_id: int, playlists: List[Dict[str, Any]]):
        super().__init__(timeout=300.0)
        self.add_item(SpotifyUserPlaylistsSelect(author_id, playlists))


# ---------- Slash command to list Spotify playlists ----------
@tree.command(name="spotify_playlists", description="List your Spotify playlists (and queue one).")
async def spotify_playlists_cmd(interaction: discord.Interaction):
    # Defer safely
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

    # get token (try async getter then sync)
    token = None
    try:
        if "get_spotify_token_async" in globals() and asyncio.iscoroutinefunction(globals()["get_spotify_token_async"]):
            token = await globals()["get_spotify_token_async"](str(interaction.user.id))
        elif "get_spotify_token" in globals():
            loop = asyncio.get_running_loop()
            token = await loop.run_in_executor(None, globals()["get_spotify_token"], str(interaction.user.id))
    except Exception:
        logger.exception("Error while obtaining spotify token in spotify_playlists_cmd")
        token = None

    if not token:
        await interaction.followup.send("You must link your Spotify via /spotify_link before using this command.", ephemeral=True)
        return

    try:
        playlists = await _fetch_user_playlists_async(token)
    except Exception:
        playlists = []

    # also fetch saved playlists from DB (if you have load_playlists)
    saved_playlists = {}
    try:
        if "load_playlists" in globals():
            saved_playlists = load_playlists(str(interaction.user.id)) or {}
    except Exception:
        logger.exception("Failed to load saved playlists for user")

    if not playlists and not saved_playlists:
        await interaction.followup.send("No Spotify playlists found and no saved playlists in the bot.", ephemeral=True)
        return

    # Send as TWO separate messages to avoid the confusing double-dropdown in one view
    if playlists:
        embed_sp = discord.Embed(
            title="🎵 Your Spotify Playlists",
            description="Select a playlist to queue it.",
            color=discord.Color.green()
        )
        for p in playlists[:10]:
            pname = (p.get("name") or "Untitled").strip()
            embed_sp.add_field(name=pname[:100], value=f"{p.get('tracks', 0)} tracks", inline=True)
        view_sp = discord.ui.View(timeout=300.0)
        view_sp.add_item(SpotifyUserPlaylistsSelect(interaction.user.id, playlists))
        await interaction.followup.send(embed=embed_sp, view=view_sp, ephemeral=True)

    if saved_playlists:
        embed_saved = discord.Embed(
            title="📋 Your Saved Bot Playlists",
            description="Select a saved playlist to queue it.",
            color=discord.Color.blurple()
        )
        for pname, meta in list(saved_playlists.items())[:10]:
            songs = meta.get("songs") or []
            embed_saved.add_field(name=pname[:100], value=f"{len(songs)} songs", inline=True)
        view_saved = discord.ui.View(timeout=300.0)
        view_saved.add_item(MyPlaylistsSelect(interaction.user.id, saved_playlists))
        await interaction.followup.send(embed=embed_saved, view=view_saved, ephemeral=True)




# ---------- Upgraded /assist interactive help command ----------
import discord
from discord import app_commands
from discord.ui import View, Select, Button
from typing import Dict, Any, List, Optional
import math

# --- static fallback DB (add richer descriptions/examples here) ---
STATIC_ASSIST_DB: Dict[str, Dict[str, Any]] = {
    "Playback": {
        "summary": "Play music from YouTube/Spotify queries, manage queue and playback.",
        "examples": [
            "/play bohemian rhapsody",
            "/play https://youtu.be/VIDEOID",
            "/playpl https://open.spotify.com/playlist/..."
        ],
    },
    "Spotify": {
        "summary": "Spotify linking and search helpers (OAuth flow & playlist support).",
        "examples": ["/spotify_link", "/playsp <song name>"],
    },
    "Lyrics": {
        "summary": "Fetch lyrics (OVH first, Genius fallback).",
        "examples": ["/lyrics", "/lyrics All The Stars - Kendrick Lamar"],
    },
    "Queue": {
        "summary": "Queue and playlist management (save/load/clear).",
        "examples": ["/queue", "/savepl mylist", "/loadpl mylist"],
    },
    "Moderation": {
        "summary": "Moderation log commands and warning management (if enabled).",
        "examples": ["/modlogs set #modlog", "/removewarnings @user"],
    },
    "Utilities": {
        "summary": "Ping, assist, help, keepalive and other small utilities.",
        "examples": ["/ping", "/assist"],
    },
    "Admin": {
        "summary": "Admin-only settings (volume, antiraid, ipban, etc.).",
        "examples": ["/set_volume 0.8", "/antiraidmode"],
    },
}

# --- helper: build a dynamic help DB from tree commands (best-effort) ---
def build_dynamic_assist_db() -> Dict[str, Dict[str, Any]]:
    db: Dict[str, Dict[str, Any]] = {}
    try:
        # tree.walk_commands() yields app_commands.Command
        for cmd in tree.walk_commands():
            # skip hidden / non-root commands?
            name = cmd.name or "unknown"
            desc = (cmd.description or "").strip()
            # infer a category: use first token before '.' or '_' or the module name
            if getattr(cmd, "parent", None):
                # if nested (has parent), use parent's name as category
                category = cmd.parent.name.title()
            else:
                # guess category from command name pieces
                tokens = name.split("_")
                category = tokens[0].title() if tokens else "General"
                # map some common names to nicer categories
                if category.lower() in ("play", "playsp", "playpl", "nowplaying", "queue"):
                    category = "Playback"
            # ensure category entry
            if category not in db:
                db[category] = {"summary": STATIC_ASSIST_DB.get(category, {}).get("summary", ""), "commands": [], "examples": STATIC_ASSIST_DB.get(category, {}).get("examples", [])}
            # build signature with parameters (best-effort)
            sig = f"/{name}"
            # list parameters
            try:
                params = []
                for p in getattr(cmd, "parameters", []) or []:
                    # app_commands.Parameter like object? best-effort: show name
                    pname = getattr(p, "name", None) or str(p)
                    params.append(f"<{pname}>")
                if params:
                    sig += " " + " ".join(params)
            except Exception:
                pass
            db[category]["commands"].append({"sig": sig, "desc": desc or "—"})
    except Exception:
        # fallback to static DB if something goes wrong
        return {k: {"summary": v.get("summary", ""), "commands": [{"sig": k, "desc": v.get("summary", "")}], "examples": v.get("examples", [])} for k, v in STATIC_ASSIST_DB.items()}
    # sort commands alphabetically
    for cat in db:
        db[cat]["commands"].sort(key=lambda x: x["sig"])
    return db

# cached dynamic DB on import (rebuild if needed)
ASSIST_DB = build_dynamic_assist_db()

# --- UI: pagination helper view -----------------------------------------
class PaginationView(View):
    def __init__(self, author_id: int, pages: List[discord.Embed], *, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.pages = pages
        self.index = 0
        # add prev/next and close
        self.prev_btn = Button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="assist_prev")
        self.next_btn = Button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="assist_next")
        self.close_btn = Button(label="Close", style=discord.ButtonStyle.danger, custom_id="assist_close")
        # attach callbacks
        self.prev_btn.callback = self._prev_cb
        self.next_btn.callback = self._next_cb
        self.close_btn.callback = self._close_cb
        # add to view
        self.add_item(self.prev_btn)
        self.add_item(self.next_btn)
        self.add_item(self.close_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This help panel is not for you — use /assist to open your own.", ephemeral=True)
            return False
        return True

    async def _update_message(self, interaction: discord.Interaction, *, edit=False):
        # clamp index
        self.index = max(0, min(self.index, len(self.pages) - 1))
        if edit:
            await interaction.response.edit_message(embed=self.pages[self.index], view=self)
        else:
            await interaction.response.send_message(embed=self.pages[self.index], view=self, ephemeral=True)

    async def _prev_cb(self, interaction: discord.Interaction):
        self.index = max(0, self.index - 1)
        await self._update_message(interaction, edit=True)

    async def _next_cb(self, interaction: discord.Interaction):
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._update_message(interaction, edit=True)

    async def _close_cb(self, interaction: discord.Interaction):
        # try to delete, otherwise disable
        try:
            await interaction.message.delete()
        except Exception:
            for i in self.children:
                i.disabled = True
            await interaction.response.edit_message(content="(Assistant closed)", embed=None, view=self)
        finally:
            self.stop()

# --- UI: main Assist view ----------------------------------------------
class AssistSelect(Select):
    def __init__(self, author_id: int, categories: List[str]):
        # build options: All + per category
        opts = [discord.SelectOption(label="All categories", description="Show a compact overview of all categories.", value="__all__")]
        for cat in categories:
            desc = (ASSIST_DB.get(cat, {}).get("summary") or "")[:100]
            opts.append(discord.SelectOption(label=cat, description=desc or "Category", value=cat))
        super().__init__(placeholder="Choose a category to view details…", min_values=1, max_values=1, options=opts)
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction):
        # ensure only invoker interacts
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This help panel is not for you — open your own with /assist.", ephemeral=True)
            return

        sel = self.values[0]
        if sel == "__all__":
            # compact overview embed
            emb = discord.Embed(title="All Categories — Overview", color=discord.Color.green())
            for cat, data in ASSIST_DB.items():
                summary = data.get("summary", "").strip()
                count = len(data.get("commands", []))
                emb.add_field(name=f"{cat} ({count})", value=summary or "—", inline=False)
            await interaction.response.edit_message(embed=emb, view=self.view)
            return

        # show detailed commands for the selected category with pagination if needed
        data = ASSIST_DB.get(sel)
        if not data:
            await interaction.response.send_message("No data for that category.", ephemeral=True)
            return

        cmds = data.get("commands", [])
        if not cmds:
            await interaction.response.edit_message(embed=discord.Embed(title=sel, description="No commands listed.", color=discord.Color.red()), view=self.view)
            return

        # build pages of embeds (10 commands per page)
        per_page = 8
        pages: List[discord.Embed] = []
        total = math.ceil(len(cmds) / per_page)
        for i in range(total):
            start = i * per_page
            end = start + per_page
            page_cmds = cmds[start:end]
            emb = discord.Embed(title=f"{sel} — Commands (page {i+1}/{total})", description=data.get("summary", ""), color=discord.Color.blurple())
            for c in page_cmds:
                sig = c.get("sig", "")
                desc = c.get("desc", "—")
                emb.add_field(name=sig[:256], value=desc[:1024], inline=False)
            # add examples if first page
            if i == 0:
                examples = data.get("examples", []) or []
                if examples:
                    emb.add_field(name="Examples", value="\n".join(examples[:5]), inline=False)
            pages.append(emb)

        # send paginated view
        pview = PaginationView(author_id=self.author_id, pages=pages)
        await pview._update_message(interaction, edit=True)

class AssistView(View):
    def __init__(self, author_id: int):
        super().__init__(timeout=300.0)
        self.author_id = author_id
        # categories from ASSIST_DB
        categories = list(ASSIST_DB.keys())
        # add select
        self.add_item(AssistSelect(author_id, categories))
        # Add functional buttons (no decorator for more control)
        self.save_btn = Button(label="All Commands", style=discord.ButtonStyle.secondary, custom_id="assist_all")
        self.examples_btn = Button(label="Usage Examples", style=discord.ButtonStyle.primary, custom_id="assist_examples")
        self.copy_btn = Button(label="Post to Channel", style=discord.ButtonStyle.success, custom_id="assist_copy")
        self.close_btn = Button(label="Close", style=discord.ButtonStyle.danger, custom_id="assist_close")
        # link button (Docs) must be added directly (decorator cannot create link buttons)
        self.docs_btn = Button(label="Open Docs", style=discord.ButtonStyle.link, url="https://example.org/docs")
        # bind callbacks
        self.save_btn.callback = self._show_all
        self.examples_btn.callback = self._show_examples
        self.copy_btn.callback = self._post_to_channel
        self.close_btn.callback = self._close
        # add to view (order matters)
        self.add_item(self.save_btn)
        self.add_item(self.examples_btn)
        self.add_item(self.copy_btn)
        self.add_item(self.close_btn)
        self.add_item(self.docs_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This help panel is personal — use /assist to open your own.", ephemeral=True)
            return False
        return True

    async def _show_all(self, interaction: discord.Interaction):
        emb = discord.Embed(title="Commands — Full Reference", color=discord.Color.green())
        for cat, data in ASSIST_DB.items():
            lines = []
            for c in data.get("commands", []):
                lines.append(f"**{c.get('sig','')}** — {c.get('desc','')}")
            emb.add_field(name=cat, value="\n".join(lines) or "No commands listed", inline=False)
        await interaction.response.edit_message(embed=emb, view=self)

    async def _show_examples(self, interaction: discord.Interaction):
        emb = discord.Embed(title="Usage Examples", color=discord.Color.blurple())
        for cat, data in ASSIST_DB.items():
            ex = data.get("examples", [])
            if ex:
                emb.add_field(name=cat, value="\n".join(ex), inline=False)
        await interaction.response.edit_message(embed=emb, view=self)

    async def _post_to_channel(self, interaction: discord.Interaction):
        # post a compact help message to the current channel (not ephemeral)
        emb = discord.Embed(title="Tansen — Commands Overview", description="Use `/assist` to open a private interactive panel with more details.", color=discord.Color.blurple())
        for cat, data in ASSIST_DB.items():
            emb.add_field(name=cat, value=data.get("summary","—"), inline=False)
        try:
            await interaction.response.send_message("Posted full command overview to the channel.", ephemeral=True)
            await interaction.channel.send(embed=emb)
        except Exception:
            await interaction.response.send_message("Failed to post to channel — missing permissions?", ephemeral=True)

    async def _close(self, interaction: discord.Interaction):
        try:
            await interaction.message.delete()
        except Exception:
            for i in self.children:
                i.disabled = True
            await interaction.response.edit_message(content="(Assistant closed)", embed=None, view=self)
        finally:
            self.stop()

# --- Slash command registration ------------------------------------------
@tree.command(name="assist", description="Open an interactive assistant describing features & commands.")
async def assist_cmd(interaction: discord.Interaction):
    # rebuild ASSIST_DB live to capture newly synced commands
    global ASSIST_DB
    ASSIST_DB = build_dynamic_assist_db()
    embed = discord.Embed(title="Tansen — Assistant", description="Choose a category from the dropdown or use the buttons for a full reference.", color=discord.Color.blurple())
    embed.add_field(name="Quick tips", value="• This panel is private to you (ephemeral).\n• Use 'Post to Channel' to share a summary publicly.", inline=False)
    view = AssistView(author_id=interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# --------------------------------------------------------------------------
# End of upgraded /assist command


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
