# --- Imports ---
import discord
import json
from lyrics import get_lyrics_from_ovh, get_lyrics_from_genius, clean_song_title
import re
from discord.ext import commands
from discord import FFmpegPCMAudio
from discord import app_commands
import yt_dlp
from yt_dlp import YoutubeDL
import asyncio
import requests
import spotipy
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
import spotifyapi
from spotifyapi import search_spotify_tracks
import os
import random
from dotenv import load_dotenv
load_dotenv()
# --- Load Tokens ---
dctokenn = os.getenv("DCTOKEN")
spotclid = os.getenv("SPOTIFY_CLIENT_ID")
spotclsec = os.getenv("SPOTIFY_CLIENT_SECRET")
# --- Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True
# Ensure the downloads directory exists
if not os.path.exists('./downloads'):
    os.makedirs('./downloads')


async def ensure_voice(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if not voice or not voice.is_connected():
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                return await interaction.user.voice.channel.connect(self_deaf=True)
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to connect: {e}", ephemeral=True)
                return None
        else:
            await interaction.followup.send("❌ You must be in a voice channel.", ephemeral=True)
            return None
    return voice

client = commands.Bot(command_prefix="$", intents=intents, help_command=None)

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=spotclid,
    client_secret=spotclsec
))


music_queue = []
volume_level = 0.5  # Default volume
is_looping = False
user_playlists = {}
last_played = {}  # Stores last played track per guild
# --- FFMPEG Options ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# --- Save & Load ---
def save_queue():
    with open('music_queue.json', 'w') as f:
        json.dump(music_queue, f, default=str)
    print("Music queue saved to music_queue.json")

def load_queue():
    global music_queue
    try:
        with open('music_queue.json', 'r') as f:
            music_queue = json.load(f)
        print("Music queue loaded from music_queue.json")
    except FileNotFoundError:
        music_queue = []

def save_playlists():
    with open('playlists.json', 'w') as f:
        json.dump(user_playlists, f, default=str)
    print("Playlists saved to playlists.json")

def load_playlists():
    global user_playlists
    try:
        with open('playlists.json', 'r') as f:
            user_playlists = json.load(f)
            print("Playlists loaded from playlists.json")
    except FileNotFoundError:
        user_playlists = {}

# Global yt_dlp options
ydl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "skip_download": True,
    "cookiefile": "cookies.txt",  # ✅ Add this line
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    },
}

from spotipy.oauth2 import SpotifyOAuth

# Spotify OAuth setup
sp_oauth = SpotifyOAuth(
    client_id=spotclid,
    client_secret=spotclsec,
    redirect_uri="https://example.org/callback",  # Updated to match the dashboard
    scope="playlist-read-private"
)

# Spotify client
sp = spotipy.Spotify(auth_manager=sp_oauth)
# --- On Ready Event ---
@client.event
async def on_ready():
    load_queue()
    load_playlists()

    # Set custom presence/status here:
    await client.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name="With Raags | /assist for help"
        )
    )

    try:
        synced = await client.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"❌ Slash command sync failed: {e}")

    print(f"✅ Bot is online as {client.user}")

# --- Music Buttons ---
class MusicControlButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.is_paused = False  # Track whether the music is paused

    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.gray)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client

        if not voice_client or not voice_client.is_connected():
            await interaction.response.send_message("❌ I'm not connected to a voice channel.", ephemeral=True)
            return

        if self.is_paused:  # If currently paused, resume playback
            if voice_client.is_paused():
                voice_client.resume()
                self.is_paused = False
                button.label = "⏸️ Pause"
                button.style = discord.ButtonStyle.gray
                await interaction.response.edit_message(content="▶️ Resumed playback!", view=self)
            else:
                await interaction.response.send_message("❌ Nothing to resume.", ephemeral=True)
        else:  # If currently playing, pause playback
            if voice_client.is_playing():
                voice_client.pause()
                self.is_paused = True
                button.label = "⏯️ Resume"
                button.style = discord.ButtonStyle.green
                await interaction.response.edit_message(content="⏸️ Paused playback!", view=self)
            else:
                await interaction.response.send_message("❌ Nothing is playing to pause.", ephemeral=True)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.red)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        global music_queue, is_looping

        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()

        music_queue.clear()
        is_looping = False

        await interaction.response.send_message("🛑 Stopped playback, cleared the queue.", ephemeral=True)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.green)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        global is_looping
        is_looping = not is_looping
        if is_looping:
            await interaction.response.send_message("🔁 Loop **enabled** — the current song will repeat!", ephemeral=True)
        else:
            await interaction.response.send_message("➡️ Loop **disabled** — moving to next songs normally.", ephemeral=True)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.blurple)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client

        if not voice_client or not voice_client.is_playing():
            await interaction.response.send_message("❌ No song is currently playing.", ephemeral=True)
            return

        # Stop the current song to trigger the next one
        voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped the current song!", ephemeral=True)
    
    @discord.ui.button(label="🔉 Volume -", style=discord.ButtonStyle.gray, custom_id="volume_down")
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        global volume_level
        volume_level = max(0.0, volume_level - 0.1)
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = volume_level
        await interaction.response.send_message(f"🔉 Volume: {int(volume_level * 100)}%", ephemeral=True)

    @discord.ui.button(label="🔊 Volume +", style=discord.ButtonStyle.gray, custom_id="volume_up")
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        global volume_level
        volume_level = min(1.0, volume_level + 0.1)
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = volume_level
        await interaction.response.send_message(f"🔊 Volume: {int(volume_level * 100)}%", ephemeral=True)

    @discord.ui.button(label="🔄 Replay", style=discord.ButtonStyle.blurple)
    async def replay(self, interaction: discord.Interaction, button: discord.ui.Button):
        global last_played

        # Check if there is a last played song for the guild
        guild_id = str(interaction.guild.id)
        if guild_id not in last_played or not last_played[guild_id]:
            await interaction.response.send_message("❌ No recently played song to replay.", ephemeral=True)
            return

        # Get the last played song
        song = last_played[guild_id]

        # Add the song back to the queue
        music_queue.insert(0, song)

        await interaction.response.send_message(f"🔄 Replaying **{song['title']}**!", ephemeral=True)

        # Start playback if not already playing
        voice = await ensure_voice(interaction)
        if voice and not voice.is_playing():
            await play_next(interaction)

# Play Next Song
async def play_next(interaction_or_ctx):
    global music_queue
    voice = interaction_or_ctx.guild.voice_client

    if not music_queue:
        msg = "✅ Queue finished!"
        if isinstance(interaction_or_ctx, discord.Interaction):
            await interaction_or_ctx.followup.send(msg, ephemeral=True)
        else:
            await interaction_or_ctx.send(msg)
        return

    song = music_queue[0]
    source = None

    try:
        if os.path.isfile(song['url']):
            source = discord.PCMVolumeTransformer(
                FFmpegPCMAudio(song['url'], options='-vn -loglevel quiet'),
                volume=volume_level
            )
        else:
            ydl_opts = {
                "format": "bestaudio/best",
                "quiet": True,
                "noplaylist": True,
                "default_search": "auto"
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(song['url'], download=False)
                stream_url = info.get("url")
                source = discord.PCMVolumeTransformer(
                    FFmpegPCMAudio(stream_url, before_options=FFMPEG_OPTIONS['before_options'], options='-vn -loglevel quiet'),
                    volume=volume_level
                )
    except Exception as e:
        print(f"FFmpeg/YDL Error: {e}")
        music_queue.pop(0)
        await play_next(interaction_or_ctx)
        return

    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        else:
            if not is_looping:
                music_queue.pop(0)

            if music_queue:
                fut = play_next(interaction_or_ctx)
                asyncio.run_coroutine_threadsafe(fut, client.loop)
    
    #Update the last played song for the guild
    guild_id = str(interaction_or_ctx.guild.id)
    last_played[guild_id] = song

    voice.play(source, after=after_playing)

    embed = discord.Embed(
        title="🎶 Now Playing",
        description=f"[{song['title']}]({song.get('spotify_url', song['url'])})",
        color=discord.Color.brand_green()
    )
    embed.set_footer(text=f"Requested by {song.get('requester', 'Unknown')}")
    if song.get("image"):
        embed.set_thumbnail(url=song["image"])

    view = MusicControlButtons()
    if isinstance(interaction_or_ctx, discord.Interaction):
        await interaction_or_ctx.followup.send(embed=embed, view=view)
    else:
        await interaction_or_ctx.send(embed=embed, view=view)


# --- Slash Commands ---

@client.tree.command(name="play", description="Search and play a song using Spotify metadata, play from YouTube")
@app_commands.describe(song="The song to search and play")
async def play(interaction: discord.Interaction, song: str):
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Search for tracks on Spotify
    tracks = search_spotify_tracks(song)
    if not tracks:
        await interaction.followup.send("❌ No tracks found on Spotify.", ephemeral=True)
        return

    class TrackSelect(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=900)
            options = []
            for index, track in enumerate(tracks[:5]):
                title = track["name"]
                artists = ", ".join(artist["name"] for artist in track["artists"])
                label = f"{title} - {artists}"
                value = str(index)
                options.append(discord.SelectOption(label=label[:100], value=value))

            self.select = discord.ui.Select(placeholder="Choose a track to play", options=options)
            self.select.callback = self.callback
            self.add_item(self.select)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            index = int(self.select.values[0])
            selected = tracks[index]
            title = selected["name"]
            artists = ", ".join(artist["name"] for artist in selected["artists"])
            image = selected["album"]["images"][0]["url"]
            spotify_url = selected["external_urls"]["spotify"]

            query = f"{title} {artists}"
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(query, download=False)
                    if not info or "entries" not in info or not info["entries"]:
                        await interaction.followup.send("❌ Could not find the song on YouTube.", ephemeral=True)
                        return
                    url = info["entries"][0]["url"]
            except Exception as e:
                print(f"Error during YouTube search: {e}")
                await interaction.followup.send(f"❌ Error during YouTube search: {e}", ephemeral=True)
                return

            music_queue.append({
                "title": f"{title} - {artists}",
                "url": url,
                "image": image,
                "spotify_url": spotify_url,
                "requester": interaction.user.name
            })

            await interaction.followup.send(f"🎶 Added **{title}** by **{artists}** to the queue!", ephemeral=True)

            if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                if not interaction.guild.voice_client:
                    if interaction.user.voice and interaction.user.voice.channel:
                        await interaction.user.voice.channel.connect(self_deaf=True)
                    else:
                        await interaction.followup.send("❌ You must be in a voice channel to play music.", ephemeral=True)
                        return
                await play_next(interaction)

    await interaction.followup.send("🎵 Select a track to play:", view=TrackSelect(), ephemeral=True)

@client.tree.command(name="queue", description="Show the current queue")
async def queue(interaction: discord.Interaction):
    if music_queue:
        # Create an embed to display the queue
        embed = discord.Embed(
            title="📜 Music Queue",
            description="Here are the songs currently in the queue:",
            color=discord.Color.blue()
        )
        for i, song in enumerate(music_queue):
            embed.add_field(
                name=f"{i + 1}. {song['title']}",
                value=f"[Link]({song.get('spotify_url', song['url'])})",
                inline=False
            )
        embed.set_footer(text=f"Total songs: {len(music_queue)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        # Send a message if the queue is empty
        embed = discord.Embed(
            title="📜 Music Queue",
            description="The queue is currently empty.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_playing():
        voice.stop()
        await interaction.response.send_message("⏭️ Skipped current song!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ No song playing.", ephemeral=True)


@client.tree.command(name="stop", description="Stop the music and clear the queue")
async def stop(interaction: discord.Interaction):
    global music_queue, is_looping, auto_play

    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()

    music_queue.clear()
    is_looping = False

    await interaction.response.send_message("🛑 Stopped playback, cleared the queue, and left the voice channel.", ephemeral=True)


@client.tree.command(name="loop", description="Toggle loop for the current song")
async def loop(interaction: discord.Interaction):
    global is_looping
    is_looping = not is_looping
    if is_looping:
        await interaction.response.send_message("🔁 Loop **enabled**. Current song will repeat!",ephemeral=True)
    else:
        await interaction.response.send_message("🔁 Loop **disabled**. Songs will continue normally.",ephemeral=True)

@client.tree.command(name="leave", description="Make the bot leave the voice channel")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Left the voice channel.",ephemeral=True)
    else:
        await interaction.response.send_message("❌ I'm not connected to any voice channel.",ephemeral=True)
# --- Lyrics ---
@client.tree.command(name="nowrics", description="Get lyrics for the currently playing song")
async def nowrics(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not music_queue:
        await interaction.followup.send("❌ No song is currently playing.", ephemeral=True)
        return

    # Get the YouTube title of the currently playing song
    current_song = music_queue[0]
    title = current_song.get("title")
    if not title:
        await interaction.followup.send("❌ No title found for the current song.", ephemeral=True)
        return

    # Clean the title for better lyric search
    cleaned_title = clean_song_title(title)
    print(f"Debug: Cleaned Title for Lyrics Search: {cleaned_title}")  # Debug log

    # Extract artist name if available
    artist = "Unknown Artist"
    if " - " in cleaned_title:
        parts = cleaned_title.split(" - ")
        cleaned_title = parts[0].strip()
        artist = parts[1].strip()

    # Try fetching lyrics from Genius
    query = f"{cleaned_title} {artist}"
    lyrics = await get_lyrics_from_genius(query)
    source = "Genius" if lyrics else None

    # Fallback to lyrics.ovh if Genius fails
    if not lyrics:
        print(f"Debug: No lyrics found on Genius for {cleaned_title}. Trying Lyrics.ovh...")
        lyrics = await get_lyrics_from_ovh(artist, cleaned_title)
        source = "Lyrics.ovh" if lyrics else None

    if not lyrics:
        await interaction.followup.send(f"❌ No lyrics found for **{cleaned_title}**.", ephemeral=True)
        return

    if len(lyrics) > 3900:
        lyrics = lyrics[:3900] + "\n...\n(lyrics truncated)"

    embed = discord.Embed(
        title=f"📜 Lyrics for {cleaned_title} - {artist}",
        description=lyrics,
        color=discord.Color.purple()
    )
    embed.set_footer(text=f"Source: {source}")

    await interaction.followup.send(embed=embed, ephemeral=True)
    
@client.tree.command(name="replay", description="Replay the most recently played song")
async def replay(interaction: discord.Interaction):
    global last_played

    # Check if there is a last played song for the guild
    guild_id = str(interaction.guild.id)
    if guild_id not in last_played or not last_played[guild_id]:
        await interaction.response.send_message("❌ No recently played song to replay.", ephemeral=True)
        return

    # Get the last played song
    song = last_played[guild_id]

    # Add the song back to the queue
    music_queue.insert(0, song)

    await interaction.response.send_message(f"🔁 Replaying **{song['title']}**!", ephemeral=True)

    # Start playback if not already playing
    voice = await ensure_voice(interaction)
    if voice and not voice.is_playing():
        await play_next(interaction)

#---- FILE AND URL---- 


from spotifyapi import get_spotify_token

async def extract_info_async(url):
    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await asyncio.to_thread(extract)

@client.tree.command(name="playurl", description="Play from any YouTube or Spotify track/playlist URL")
@app_commands.describe(url="Provide a YouTube or Spotify URL (track or playlist)")
async def playurl(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    # Detect Spotify playlist
    if "open.spotify.com/playlist/" in url:
        playlist_id = url.split("/")[-1].split("?")[0]
        try:
            token = get_spotify_token()
            if not token:
                await interaction.followup.send("❌ Spotify token error.", ephemeral=True)
                return
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks", headers=headers)

            if response.status_code in [401, 403]:
                await interaction.followup.send(
                    "🔒 This Spotify playlist appears to be private.\n"
                    "Please link your account and use `/spotify_playlists` to access private playlists.",
                    ephemeral=True
                )
                return

            tracks = response.json()["items"]
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to get Spotify playlist: {e}", ephemeral=True)
            return

        added = 0
        batch_size = 10  # Process 10 tracks at a time
        for i in range(0, len(tracks), batch_size):
            batch = tracks[i:i + batch_size]
            for item in batch:
                track = item["track"]
                title = track["name"]
                artists = ", ".join(a["name"] for a in track["artists"])
                query = f"{title} {artists}"
                try:
                    info = await extract_info_async(f"ytsearch:{query}")
                    video = info["entries"][0]
                except Exception:
                    continue
                music_queue.append({
                    "title": video["title"],
                    "url": video["webpage_url"],
                    "image": video.get("thumbnail"),
                    "spotify_url": track["external_urls"]["spotify"],
                    "requester": interaction.user.name
                })
                added += 1

            # Send progress update to the user
            await interaction.followup.send(f"✅ Added {added} tracks so far...", ephemeral=True)

        await interaction.followup.send(f"✅ Finished adding {added} tracks from Spotify playlist.", ephemeral=True)

    # Detect YouTube playlist
    elif "youtube.com/playlist" in url or ("youtube.com/watch" in url and "list=" in url):
        try:
            info = await extract_info_async(url)
        except yt_dlp.utils.DownloadError as e:
            await interaction.followup.send(f"❌ Failed to process the URL: {e}", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)
            return

        if "entries" not in info:
            await interaction.followup.send("❌ No playlist found at URL.", ephemeral=True)
            return

        added = 0
        batch_size = 10  # Process 10 videos at a time
        for i in range(0, len(info["entries"]), batch_size):
            batch = info["entries"][i:i + batch_size]
            for entry in batch:
                music_queue.append({
                    "title": entry["title"],
                    "url": entry["webpage_url"],
                    "image": entry.get("thumbnail"),
                    "spotify_url": None,
                    "requester": interaction.user.name
                })
                added += 1

            # Send progress update to the user
            await interaction.followup.send(f"✅ Added {added} tracks so far...", ephemeral=True)

        await interaction.followup.send(f"✅ Finished adding {added} tracks from YouTube playlist.", ephemeral=True)

    # Otherwise assume single track
    else:
        if "open.spotify.com/track/" in url:
            # Spotify track
            track_id = url.split("/")[-1].split("?")[0]
            try:
                token = get_spotify_token()
                headers = {"Authorization": f"Bearer {token}"}
                track = requests.get(f"https://api.spotify.com/v1/tracks/{track_id}", headers=headers).json()
                title = track["name"]
                artists = ", ".join(a["name"] for a in track["artists"])
                query = f"{title} {artists}"
                info = await extract_info_async(f"ytsearch:{query}")
                video = info["entries"][0]
            except Exception as e:
                await interaction.followup.send(f"❌ Spotify track lookup failed: {e}", ephemeral=True)
                return
            music_queue.append({
                "title": video["title"],
                "url": video["webpage_url"],
                "image": video.get("thumbnail"),
                "spotify_url": track["external_urls"]["spotify"],
                "requester": interaction.user.name
            })
            await interaction.followup.send(f"🎵 Added: `{video['title']}`")

        else:
            # YouTube video
            try:
                info = await extract_info_async(url)
            except yt_dlp.utils.DownloadError as e:
                await interaction.followup.send(f"❌ Failed to process the URL: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)
                return
            music_queue.append({
                "title": info["title"],
                "url": url,
                "image": info.get("thumbnail"),
                "spotify_url": None,
                "requester": interaction.user.name
            })
            await interaction.followup.send(f"🎵 Added: `{info['title']}`")

    # Auto play if idle
    voice = await ensure_voice(interaction)
    if voice and not voice.is_playing():
        await play_next(interaction)


@client.tree.command(name="playfile", description="Upload a music file to play")
@app_commands.describe(file="The music file to play")
async def playfile(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Check if the file is an audio file
    valid_extensions = ['.mp3', '.wav', '.ogg', '.flac', '.m4a', '.weba']
    if not any(file.filename.lower().endswith(ext) for ext in valid_extensions):
        await interaction.followup.send("❌ Invalid file type. Please upload a valid music file (e.g., .mp3, .wav, .ogg, .flac, .m4a, .weba).", ephemeral=True)
        return

    # Download the file
    try:
        file_path = f"./downloads/{file.filename}"
        await file.save(file_path)
        print(f"File saved: {file_path}")
    except Exception as e:
        print(f"Error saving file: {e}")
        await interaction.followup.send("❌ Failed to download the file. Please try again.", ephemeral=True)
        return

    # Add the file to the queue
    music_queue.append({
        "title": file.filename,
        "url": file_path,
        "image": None,
        "spotify_url": None,
        "requester": interaction.user.name
    })

    await interaction.followup.send(f"🎶 Added **{file.filename}** to the queue!", ephemeral=True)

    # Start playback if not already playing
    if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
        if not interaction.guild.voice_client:
            # Join the user's voice channel if not already connected
            if interaction.user.voice and interaction.user.voice.channel:
                await interaction.user.voice.channel.connect(self_deaf=True)
            else:
                await interaction.followup.send("❌ You must be in a voice channel to play music.", ephemeral=True)
                return
        await play_next(interaction)


#--Spotify Connect---


spotify_usernames = {}  # Dictionary to store Spotify access tokens for users
@client.tree.command(name="spotify_user", description="Link your Spotify account to the bot")
async def spotify_user(interaction: discord.Interaction):
    # Generate the authorization URL
    auth_url = sp_oauth.get_authorize_url()

    await interaction.response.send_message(
        f"🔗 Click [here]({auth_url}) to link your Spotify account. After authorizing, paste the redirected URL here.",
        ephemeral=True
    )

@client.tree.command(name="spotify_auth", description="Complete Spotify account linking by providing the redirected URL")
@app_commands.describe(redirect_url="The URL you were redirected to after authorizing")
async def spotify_auth(interaction: discord.Interaction, redirect_url: str):
    try:
        # Extract the authorization code from the redirected URL
        code = redirect_url.split("code=")[-1]
        sp_oauth.get_access_token(code)  # This caches the token
        token_info = sp_oauth.get_cached_token()  # Retrieve the cached token
        access_token = token_info["access_token"]

        # Save the access token for the user
        user_id = str(interaction.user.id)
        spotify_usernames[user_id] = access_token

        await interaction.response.send_message(
            "✅ Your Spotify account has been successfully linked! You can now use `/spotify_playlists` to view your playlists.",
            ephemeral=True
        )
    except Exception as e:
        print(f"Spotify OAuth Error: {e}")
        await interaction.response.send_message(
            "❌ Failed to authenticate with Spotify. Please try linking your account again using `/spotify_user`.",
            ephemeral=True
        )

@client.tree.command(name="spotify_playlists", description="View and play your Spotify playlists")
async def spotify_playlists(interaction: discord.Interaction):
    user_id = str(interaction.user.id)

    if user_id not in spotify_usernames:
        await interaction.response.send_message(
            "❌ You need to link your Spotify account first using `/spotify_user`.",
            ephemeral=True
        )
        return

    access_token = spotify_usernames[user_id]
    sp_user = spotipy.Spotify(auth=access_token)

    try:
        playlists = sp_user.current_user_playlists()["items"]
    except spotipy.exceptions.SpotifyException as e:
        print(f"Spotify API Error: {e}")
        await interaction.response.send_message(
            "❌ Failed to fetch your playlists. Please ensure your Spotify account is linked correctly.",
            ephemeral=True
        )
        return
    except Exception as e:
        print(f"Unexpected Error: {e}")
        await interaction.response.send_message(
            "❌ An unexpected error occurred while fetching your playlists.",
            ephemeral=True
        )
        return

    if not playlists:
        await interaction.response.send_message("❌ You have no playlists.", ephemeral=True)
        return

    class PlaylistSelect(discord.ui.View):
        def __init__(self, playlists):
            super().__init__(timeout=900)
            options = []

            for playlist in playlists:
                name = str(playlist["name"]).strip()
                if not name or len(name) > 100:
                    continue  # Skip invalid or too-long names
                options.append(
                    discord.SelectOption(
                        label=name[:100],
                        description=f"{playlist['tracks']['total']} songs"[:100],
                        value=playlist["id"]
                    )
                )

            if not options:
                options.append(
                    discord.SelectOption(
                        label="(No valid playlists found)",
                        description="Try refreshing your account or renaming them.",
                        value="none"
                    )
                )

            self.select = discord.ui.Select(placeholder="Choose a playlist to play", options=options)
            self.select.callback = self.callback
            self.add_item(self.select)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)

            playlist_id = self.select.values[0]
            if playlist_id == "none":
                await interaction.followup.send("❌ No valid playlist to play.", ephemeral=True)
                return

            try:
                playlist_tracks = sp_user.playlist_tracks(playlist_id)["items"]
            except Exception as e:
                print(f"Spotify API Error: {e}")
                await interaction.followup.send(
                    "❌ Failed to fetch the playlist's tracks.",
                    ephemeral=True
                )
                return

            added = 0
            batch_size = 10  # Process 10 tracks at a time
            for i in range(0, len(playlist_tracks), batch_size):
                batch = playlist_tracks[i:i + batch_size]
                for track in batch:
                    track_info = track["track"]
                    title = track_info["name"]
                    artists = ", ".join(artist["name"] for artist in track_info["artists"])
                    query = f"{title} {artists}"

                    try:
                        info = await extract_info_async(f"ytsearch:{query}")
                        video = info["entries"][0]
                    except Exception as e:
                        print(f"Error during YouTube search: {e}")
                        continue

                    music_queue.append({
                        "title": f"{title} - {artists}",
                        "url": video["webpage_url"],
                        "image": track_info["album"]["images"][0]["url"] if track_info["album"]["images"] else None,
                        "spotify_url": track_info["external_urls"]["spotify"],
                        "requester": interaction.user.name
                    })
                    added += 1

                # Send progress update to the user
                await interaction.followup.send(f"✅ Added {added} tracks so far...", ephemeral=True)

            await interaction.followup.send(
                f"🎶 Finished adding {added} tracks from the playlist to the queue!",
                ephemeral=True
            )

            if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                if not interaction.guild.voice_client:
                    if interaction.user.voice and interaction.user.voice.channel:
                        await interaction.user.voice.channel.connect()
                    else:
                        await interaction.followup.send(
                            "❌ You must be in a voice channel to play music.",
                            ephemeral=True
                        )
                        return
                await play_next(interaction)

    await interaction.response.send_message(
        "🎶 Select a playlist to play:",
        view=PlaylistSelect(playlists),
        ephemeral=True
    )

@client.tree.command(name="spotify_unlink", description="Unlink your Spotify account from the bot")
async def spotify_unlink(interaction: discord.Interaction):
    user_id = str(interaction.user.id)

    if user_id in spotify_usernames:
        del spotify_usernames[user_id]
        await interaction.response.send_message(
            "✅ Your Spotify account has been unlinked from the bot.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ No Spotify account is currently linked to your profile.",
            ephemeral=True
        )


# --- Playlist Saving and Playing ---

@client.tree.command(name="savepl", description="Save the current queue as a playlist with a name and description")
@app_commands.describe(playlist_name="The name of the playlist", description="A short description of the playlist")
async def savepl(interaction: discord.Interaction, playlist_name: str, description: str):
    user_id = str(interaction.user.id)

    if not music_queue:
        await interaction.response.send_message("❌ No queue to save.", ephemeral=True)
        return

    if user_id not in user_playlists:
        user_playlists[user_id] = {}

    # Save the playlist with its name and description
    user_playlists[user_id][playlist_name] = {
        "description": description,
        "songs": music_queue.copy()
    }
    save_playlists()
    await interaction.response.send_message(f"🎶 Playlist **{playlist_name}** saved with description: {description}", ephemeral=True)


@client.tree.command(name="playpl", description="Play one of your saved playlists")
async def playpl(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    playlists = user_playlists.get(user_id, {})

    if not playlists:
        await interaction.response.send_message("❌ You have no saved playlists.", ephemeral=True)
        return

    # Define a dropdown menu for playlist selection
    class PlaylistSelect(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=900)  # 15-minute timeout
            options = [
                discord.SelectOption(
                    label=name,
                    description=details.get("description", "No description provided")[:100],  # Limit description to 100 characters
                    value=name
                )
                for name, details in playlists.items()
            ]
            self.select = discord.ui.Select(placeholder="Choose a playlist to play", options=options)
            self.select.callback = self.callback
            self.add_item(self.select)

        async def callback(self, interaction: discord.Interaction):
            playlist_name = self.select.values[0]
            playlist = playlists[playlist_name].get("songs", [])

            if playlist:
                music_queue.extend(playlist)
                save_queue()
                await interaction.response.send_message(f"🎵 Added playlist **{playlist_name}** to the queue!", ephemeral=True)
                if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                    await play_next(interaction)
            else:
                await interaction.response.send_message(f"❌ Playlist **{playlist_name}** is empty.", ephemeral=True)

    # Send the dropdown menu to the user
    await interaction.response.send_message("🎶 Select a playlist to play:", view=PlaylistSelect(), ephemeral=True)




@client.tree.command(name="mypl", description="Show your playlists")
async def mypl(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    playlists = user_playlists.get(user_id, {})

    if playlists:
        embed = discord.Embed(
            title=f"🎵 {interaction.user.display_name}'s Playlists",
            description="Here are your saved playlists:",
            color=discord.Color.green()
        )

        for name, details in playlists.items():
            # Ensure details is a dictionary and has the correct structure
            description = details.get("description", "No description provided")
            num_songs = len(details.get("songs", []))
            embed.add_field(
                name=f"📂 {name}",
                value=f"{description}\n**{num_songs} songs**",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("❌ You have no saved playlists.", ephemeral=True)
# --- Remove Commands ---

@client.tree.command(name="removequeue", description="Remove a song from the queue by its number")
@app_commands.describe(index="The song number shown in /queue")
async def removequeue(interaction: discord.Interaction, index: int):
    if 0 < index <= len(music_queue):
        removed_song = music_queue.pop(index - 1)
        save_queue()
        await interaction.response.send_message(
            f"✅ Removed **{removed_song['title']}** from the queue.", ephemeral=True
        )
    else:
        await interaction.response.send_message("❌ Invalid song number.", ephemeral=True)

@client.tree.command(name="removepl", description="Remove a song from a specific playlist")
@app_commands.describe(playlist_name="The name of the playlist", index="The song number in the playlist")
async def removepl(interaction: discord.Interaction, playlist_name: str, index: int):
    user_id = str(interaction.user.id)
    playlists = user_playlists.get(user_id, {})

    if playlist_name not in playlists:
        await interaction.response.send_message(f"❌ Playlist **{playlist_name}** does not exist.", ephemeral=True)
        return

    playlist = playlists[playlist_name]["songs"]

    if 0 < index <= len(playlist):
        removed_song = playlist.pop(index - 1)
        save_playlists()
        await interaction.response.send_message(
            f"✅ Removed **{removed_song['title']}** from playlist **{playlist_name}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message("❌ Invalid song number.", ephemeral=True)



@client.tree.command(name="clearqueue", description="Clear the entire music queue")
async def clearqueue(interaction: discord.Interaction):
    global music_queue
    if music_queue:
        music_queue.clear()
        save_queue()
        await interaction.response.send_message("🗑️ Music queue cleared.", ephemeral=True)
    else:
        await interaction.response.send_message("ℹ️ Queue is already empty.", ephemeral=True)

@client.tree.command(name="delpl", description="Delete an entire playlist by its name")
@app_commands.describe(playlist_name="The name of the playlist to clear")
async def delpl(interaction: discord.Interaction, playlist_name: str):
    user_id = str(interaction.user.id)
    playlists = user_playlists.get(user_id, {})

    if playlist_name not in playlists:
        await interaction.response.send_message(f"❌ Playlist **{playlist_name}** does not exist.", ephemeral=True)
        return

    del playlists[playlist_name]
    save_playlists()
    await interaction.response.send_message(f"🗑️ Playlist **{playlist_name}** has been deleted.", ephemeral=True)


# --- Help Menu with Buttons ---
class HelpMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎵 Music Commands", style=discord.ButtonStyle.blurple)
    async def music_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🎵 Select a music command to learn more:",
            view=CommandDropdown("music"),
            ephemeral=True
        )

    @discord.ui.button(label="🎶 Playlist Commands", style=discord.ButtonStyle.green)
    async def playlist_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🎶 Select a playlist command to learn more:",
            view=CommandDropdown("playlist"),
            ephemeral=True
        )

    @discord.ui.button(label="🛠️ Queue Editing", style=discord.ButtonStyle.gray)
    async def edit_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🛠️ Select a queue editing command to learn more:",
            view=CommandDropdown("queue"),
            ephemeral=True
        )


class CommandDropdown(discord.ui.View):
    def __init__(self, category):
        super().__init__(timeout=None)
        self.category = category

        # Define commands for each category
        commands = {
            "music": [
                ("play", "Play a song from YouTube"),
                ("queue", "Show the current queue"),
                ("skip", "Skip the current song"),
                ("stop", "Stop the music"),
                ("loop", "Toggle looping"),
                ("leave", "Disconnect from VC"),
                ("nowrics", "Display the lyrics of the current song"),
                ("playfile", "Upload a file to play it"),
                ("playurl", "Paste a URL to play"),
                ("/replay", "Replay the last played song"),
            ],
            "playlist": [
                ("savepl", "Save queue as a playlist"),
                ("playpl", "Play a saved playlist"),
                ("mypl", "Show your saved playlists"),
                ("removepl", "Remove a song from a playlist"),
                ("delpl", "Delete a playlist"),
                ("spotify_user", "Link your Spotify account"),
                ("spotify_auth", "Complete Spotify account linking"),
                ("spotify_playlists", "View and play your Spotify playlists"),
                ("spotify_unlink", "Unlink your Spotify account"),
            ],
            "queue": [
                ("removequeue", "Remove a song from the queue"),
                ("clearqueue", "Clear the queue"),
            ],
        }

        # Add dropdown options
        options = [
            discord.SelectOption(label=cmd[0], description=cmd[1], value=cmd[0])
            for cmd in commands[category]
        ]
        self.select = discord.ui.Select(placeholder="Select a command", options=options)
        self.select.callback = self.show_command_help
        self.add_item(self.select)

    async def show_command_help(self, interaction: discord.Interaction):
        command = self.select.values[0]

        # Define detailed help for each command
        detailed_help = {
            "play": "**Usage:** `/play [song]`\nSearch and play a song from YouTube.",
            "queue": "**Usage:** `/queue`\nShow the current music queue.",
            "skip": "**Usage:** `/skip`\nSkip the currently playing song.",
            "stop": "**Usage:** `/stop`\nStop the music and clear the queue.",
            "loop": "**Usage:** `/loop`\nToggle looping for the current song.",
            "leave": "**Usage:** `/leave`\nDisconnect the bot from the voice channel.",
            "nowrics": "**Usage:** `/nowrics`\nDisplay the lyrics of the currently playing song.",
            "playfile": "**Usage:** `/playfile [file]`\nUpload a music file to play it.",
            "playurl": "**Usage:** `/playurl [url]`\nPlay a song or playlist from a URL.",
            "replay": "**Usage:** `/replay`\nReplay the most recently played song.",
            "savepl": "**Usage:** `/savepl [name] [description]`\nSave the current queue as a playlist.",
            "playpl": "**Usage:** `/playpl`\nPlay one of your saved playlists.",
            "mypl": "**Usage:** `/mypl`\nShow your saved playlists.",
            "removepl": "**Usage:** `/removepl [playlist] [number]`\nRemove a song from a playlist.",
            "delpl": "**Usage:** `/delpl [playlist]`\nDelete a playlist.",
            "spotify_user": (
                "**Usage:** `/spotify_user`\nLink your Spotify account to the bot.\n\n"
                "**Steps to link your Spotify account:**\n"
                "1. Use `/spotify_user` to get the authorization link.\n"
                "2. Authorize the bot on Spotify.\n"
                "3. Copy the redirected URL and use `/spotify_auth` to complete the process."
            ),
            "spotify_auth": (
                "**Usage:** `/spotify_auth [redirect_url]`\nComplete Spotify account linking by providing the redirected URL.\n\n"
                "**Steps to complete Spotify linking:**\n"
                "1. After authorizing the bot on Spotify, you will be redirected to a URL.\n"
                "2. Copy the entire URL and paste it as the argument for `/spotify_auth`."
            ),
            "spotify_playlists": (
                "**Usage:** `/spotify_playlists`\nView and play your Spotify playlists.\n\n"
                "**How to get your Spotify username:**\n"
                "• **Mobile:** Home > Picture (top left) > Settings and Privacy > Account > Username\n"
                "• **Web Player:** Home > Picture (top right) > Account > Edit Profile > Username"
            ),
            "spotify_unlink": "**Usage:** `/spotify_unlink`\nUnlink your Spotify account from the bot.",
            "removequeue": "**Usage:** `/removequeue [number]`\nRemove a song from the queue by its number.",
            "clearqueue": "**Usage:** `/clearqueue`\nClear the entire music queue.",
        }

        # Send detailed help for the selected command
        help_text = detailed_help.get(command, "No help available for this command.")
        embed = discord.Embed(
            title=f"Help: {command}",
            description=help_text,
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="assist", description="Show the bot help menu with buttons")
async def assist(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Tansen's Help Menu",
        description="Click a button below to see commands by category!",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=HelpMenu(), ephemeral=True)

# --- Run the Bot ---

client.run(dctokenn)
