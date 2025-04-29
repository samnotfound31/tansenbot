# --- Imports ---
import discord
import json
#import os
import tokens
from tokens import dctokenn
#dctoken = os.getenv("dctoken")
from discord.ext import commands
from discord import FFmpegPCMAudio
from discord import app_commands
import yt_dlp
import asyncio
import requests
from keep_alive import keep_alive

# --- Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True

client = commands.Bot(command_prefix="$", intents=intents, help_command=None)

music_queue = []
is_looping = False
user_playlists = {}

# --- FFMPEG Options ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# --- Save & Load ---
def save_queue():
    with open('music_queue.json', 'w') as f:
        json.dump(music_queue, f, default=str)

def load_queue():
    global music_queue
    try:
        with open('music_queue.json', 'r') as f:
            music_queue = json.load(f)
    except FileNotFoundError:
        music_queue = []

def save_playlists():
    with open('playlists.json', 'w') as f:
        json.dump(user_playlists, f, default=str)

def load_playlists():
    global user_playlists
    try:
        with open('playlists.json', 'r') as f:
            user_playlists = json.load(f)
    except FileNotFoundError:
        user_playlists = {}

# --- On Ready Event ---
@client.event
async def on_ready():
    load_queue()
    load_playlists()

    # Set custom presence/status here:
    await client.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Akbar's Court | /assist for help"
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
    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.gray)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.pause()
            await interaction.response.send_message("⏸️ Paused playback!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing playing to pause.", ephemeral=True)

    @discord.ui.button(label="⏯️ Resume", style=discord.ButtonStyle.green)
    async def resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client and not interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.resume()
            await interaction.response.send_message("▶️ Resumed!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to resume!", ephemeral=True)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.blurple)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
            await interaction.response.send_message("⏭️ Skipped!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing playing to skip!", ephemeral=True)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.red)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop()
            await interaction.response.send_message("⏹️ Stopped playback!", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing playing to stop!", ephemeral=True)
    
    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.green)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        global is_looping
        is_looping = not is_looping
        if is_looping:
            await interaction.response.send_message("🔁 Loop **enabled** — the current song will repeat!", ephemeral=True)
        else:
            await interaction.response.send_message("➡️ Loop **disabled** — moving to next songs normally.", ephemeral=True)

# --- Play Next Song ---
async def play_next(interaction_or_ctx):
    global music_queue
    voice = interaction_or_ctx.guild.voice_client

    if music_queue:
        song = music_queue[0]
        source = FFmpegPCMAudio(song['source'], **FFMPEG_OPTIONS)

        def after_playing(error):
            if error:
                print(f"Playback error: {error}")
            else:
                if not is_looping:
                    music_queue.pop(0)
                fut = play_next(interaction_or_ctx)
                asyncio.run_coroutine_threadsafe(fut, client.loop)

        voice.play(source, after=after_playing)

        display_name = song['requested_by']['name']
        webpage_url = song['webpage_url']

        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"[{song['title']}]({webpage_url})",
            color=discord.Color.green()
        )
        embed.set_footer(text=f"Requested by {display_name}")

        if isinstance(interaction_or_ctx, discord.Interaction):
            await interaction_or_ctx.followup.send(embed=embed, view=MusicControlButtons())
        else:
            await interaction_or_ctx.send(embed=embed, view=MusicControlButtons())
    else:
        if isinstance(interaction_or_ctx, discord.Interaction):
            await interaction_or_ctx.followup.send("✅ Queue finished!")
        else:
            await interaction_or_ctx.send("✅ Queue finished!")

# --- Slash Commands ---


@client.tree.command(name="play", description="Play a song from YouTube")
@app_commands.describe(song="Name or URL of the song")
async def play(interaction: discord.Interaction, song: str):
    await interaction.response.defer()

    voice = interaction.guild.voice_client
    if not voice:
        if interaction.user.voice:
            channel = interaction.user.voice.channel
            voice = await channel.connect()
            await interaction.guild.me.edit(deafen=True)
        else:
            await interaction.followup.send("❌ Join a voice channel first!", ephemeral=True)
            return

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'default_search': 'ytsearch1',
        'noplaylist': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(song, download=False)
        if 'entries' in info:
            info = info['entries'][0]
        url = info['url']
        title = info.get('title', 'Unknown Title')
        webpage_url = info.get('webpage_url', '')

    song_obj = {
        'source': url,
        'title': title,
        'webpage_url': webpage_url,
        'requested_by': {
            'id': interaction.user.id,
            'name': interaction.user.display_name
        }
    }
    music_queue.append(song_obj)
    save_queue()

    if not voice.is_playing():
        await play_next(interaction)
    else:
        await interaction.followup.send(f"🎶 Added to queue: [{title}]({webpage_url})")

@client.tree.command(name="queue", description="Show the current queue")
async def queue(interaction: discord.Interaction):
    if music_queue:
        embed = discord.Embed(title="📜 Music Queue", color=discord.Color.blue())
        for i, song in enumerate(music_queue):
            embed.add_field(name=f"{i+1}. {song['title']}", value=f"[Link]({song['webpage_url']})", inline=False)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("📜 Queue is empty!")

@client.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_playing():
        voice.stop()
        await interaction.response.send_message("⏭️ Skipped current song!")
    else:
        await interaction.response.send_message("\u274c No song playing.")

@client.tree.command(name="stop", description="Stop the current playback")
async def stop(interaction: discord.Interaction):
    voice = interaction.guild.voice_client
    if voice and voice.is_playing():
        voice.stop()
        await interaction.response.send_message("⏹️ Playback stopped!")
    else:
        await interaction.response.send_message("❌ No song playing.")

@client.tree.command(name="loop", description="Toggle loop for the current song")
async def loop(interaction: discord.Interaction):
    global is_looping
    is_looping = not is_looping
    if is_looping:
        await interaction.response.send_message("🔁 Loop **enabled**. Current song will repeat!")
    else:
        await interaction.response.send_message("🔁 Loop **disabled**. Songs will continue normally.")

@client.tree.command(name="leave", description="Make the bot leave the voice channel")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Left the voice channel.")
    else:
        await interaction.response.send_message("❌ I'm not connected to any voice channel.")

# --- Playlist Saving and Playing ---

@client.tree.command(name="savepl", description="Save the current queue as your default playlist")
async def savepl(interaction: discord.Interaction):
    user_id = str(interaction.user.id)

    if not music_queue:
        await interaction.response.send_message("❌ No queue to save.")
        return

    if user_id not in user_playlists:
        user_playlists[user_id] = {"default": [], "moods": {}}

    user_playlists[user_id]["default"] = music_queue.copy()
    save_playlists()
    await interaction.response.send_message("🎶 Playlist saved as your default!")

@client.tree.command(name="savemoodpl", description="Save the current queue under a mood")
@app_commands.describe(mood="Mood name for the playlist")
async def savemoodpl(interaction: discord.Interaction, mood: str):
    user_id = str(interaction.user.id)
    mood = mood.lower()

    if not music_queue:
        await interaction.response.send_message("❌ No queue to save.")
        return

    if user_id not in user_playlists:
        user_playlists[user_id] = {"default": [], "moods": {}}

    user_playlists[user_id]["moods"][mood] = music_queue.copy()
    save_playlists()
    await interaction.response.send_message(f"🎶 Playlist saved under mood **{mood}**!")

@client.tree.command(name="playpl", description="Play your saved default playlist")
async def playpl(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    playlists = user_playlists.get(user_id, {})
    playlist = playlists.get("default", [])

    if playlist:
        music_queue.extend(playlist)
        save_queue()
        await interaction.response.send_message("🎵 Added your default playlist to the queue!")
        if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
            await play_next(interaction)
    else:
        await interaction.response.send_message("❌ No saved default playlist.")

@client.tree.command(name="playmood", description="Play a mood playlist")
@app_commands.describe(mood="Mood to play")
async def playmood(interaction: discord.Interaction, mood: str):
    user_id = str(interaction.user.id)
    playlists = user_playlists.get(user_id, {}).get("moods", {})
    playlist = playlists.get(mood.lower())

    if playlist:
        music_queue.extend(playlist)
        save_queue()
        await interaction.response.send_message(f"🎶 Added your **{mood}** playlist to the queue!")
        if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
            await play_next(interaction)
    else:
        await interaction.response.send_message(f"❌ No saved playlist for mood **{mood}**.")

@client.tree.command(name="mypl", description="Show your playlists")
async def mypl(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    playlists = user_playlists.get(user_id, {})

    if playlists:
        embed = discord.Embed(title=f"🎵 {interaction.user.display_name}'s Playlists", color=discord.Color.green())

        default_playlist = playlists.get("default", [])
        if default_playlist:
            embed.add_field(name="📂 Default Playlist", value=f"{len(default_playlist)} songs. Use `/playpl`.", inline=False)

        moods = playlists.get("moods", {})
        if moods:
            for mood, songs in moods.items():
                embed.add_field(name=f"• {mood.title()} ({len(songs)} songs)", value=f"Use `/playmood {mood}`", inline=False)

        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ No saved playlists.")

# --- Remove Commands ---

@client.tree.command(name="removequeue", description="Remove a song from the queue by its number")
@app_commands.describe(index="The song number shown in /queue")
async def removequeue(interaction: discord.Interaction, index: int):
    if 0 < index <= len(music_queue):
        removed_song = music_queue.pop(index - 1)
        save_queue()
        await interaction.response.send_message(f"❌ Removed **{removed_song['title']}** from the queue.")
    else:
        await interaction.response.send_message("❌ Invalid song number.")

@client.tree.command(name="removepl", description="Remove a song from your default playlist")
@app_commands.describe(index="The song number in your saved playlist")
async def removepl(interaction: discord.Interaction, index: int):
    user_id = str(interaction.user.id)
    playlist = user_playlists.get(user_id, {}).get("default", [])

    if 0 < index <= len(playlist):
        removed_song = playlist.pop(index - 1)
        save_playlists()
        await interaction.response.send_message(f"❌ Removed **{removed_song['title']}** from your default playlist.")
    else:
        await interaction.response.send_message("❌ Invalid song number.")

@client.tree.command(name="removemoodpl", description="Remove a song from a mood playlist")
@app_commands.describe(mood="Mood name", index="The song number in that mood playlist")
async def removemoodpl(interaction: discord.Interaction, mood: str, index: int):
    user_id = str(interaction.user.id)
    playlist = user_playlists.get(user_id, {}).get("moods", {}).get(mood.lower(), [])

    if playlist and 0 < index <= len(playlist):
        removed_song = playlist.pop(index - 1)
        save_playlists()
        await interaction.response.send_message(f"❌ Removed **{removed_song['title']}** from your **{mood}** playlist.")
    else:
        await interaction.response.send_message("❌ Invalid mood name or song number.")

@client.tree.command(name="clearqueue", description="Clear the entire music queue")
async def clearqueue(interaction: discord.Interaction):
    global music_queue
    if music_queue:
        music_queue.clear()
        save_queue()
        await interaction.response.send_message("🗑️ Music queue cleared.")
    else:
        await interaction.response.send_message("ℹ️ Queue is already empty.")

@client.tree.command(name="clearpl", description="Clear your default saved playlist")
async def clearpl(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id in user_playlists and user_playlists[user_id].get("default"):
        user_playlists[user_id]["default"] = []
        save_playlists()
        await interaction.response.send_message("🗑️ Default playlist cleared.")
    else:
        await interaction.response.send_message("ℹ️ No saved default playlist.")

@client.tree.command(name="clearmoodpl", description="Clear all songs from a mood playlist")
@app_commands.describe(mood="Mood name to clear")
async def clearmoodpl(interaction: discord.Interaction, mood: str):
    user_id = str(interaction.user.id)
    moods = user_playlists.get(user_id, {}).get("moods", {})

    if mood.lower() in moods:
        moods[mood.lower()] = []
        save_playlists()
        await interaction.response.send_message(f"🗑️ Cleared all songs from **{mood}** playlist.")
    else:
        await interaction.response.send_message("❌ Mood playlist not found.")

# Dropdown for Mood Playlists 

class MoodDropdown(discord.ui.Select):
    def __init__(self, user_id):
        self.user_id = str(user_id)
        options = []

        moods = user_playlists.get(self.user_id, {}).get('moods', {})
        for mood in moods.keys():
            options.append(discord.SelectOption(label=mood.title(), description=f"Your {mood} playlist"))

        super().__init__(placeholder="Select a Mood 🎶", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_mood = self.values[0].lower()
        playlist = user_playlists.get(self.user_id, {}).get('moods', {}).get(selected_mood)

        if playlist:
            music_queue.extend(playlist)
            save_queue()
            await interaction.response.send_message(f"🎶 Now playing your **{selected_mood}** playlist!")
            if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                await play_next(interaction)
        else:
            await interaction.response.send_message("❌ Playlist not found.", ephemeral=True)

class MoodDropdownView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__()
        self.add_item(MoodDropdown(user_id))

@client.tree.command(name="choosemood", description="Select a mood playlist to play")
async def choosemood(interaction: discord.Interaction):
    await interaction.response.send_message("🎶 Choose a mood playlist:", view=MoodDropdownView(interaction.user.id), ephemeral=True)

# --- Help Menu with Buttons ---

class HelpMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎵 Music Commands", style=discord.ButtonStyle.blurple)
    async def music_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🎵 Music Commands", color=discord.Color.blue())
        embed.add_field(name="/play [song]", value="Play a song from YouTube", inline=False)
        embed.add_field(name="/queue", value="Show the current queue", inline=False)
        embed.add_field(name="/skip", value="Skip the current song", inline=False)
        embed.add_field(name="/stop", value="Stop the music", inline=False)
        embed.add_field(name="/loop", value="Toggle looping", inline=False)
        embed.add_field(name="/leave", value="Disconnect from VC", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🎶 Playlist Commands", style=discord.ButtonStyle.green)
    async def playlist_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🎶 Playlist Management", color=discord.Color.green())
        embed.add_field(name="/savepl", value="Save queue as default playlist", inline=False)
        embed.add_field(name="/playpl", value="Play your default playlist", inline=False)
        embed.add_field(name="/savemoodpl [mood]", value="Save playlist under mood", inline=False)
        embed.add_field(name="/playmood [mood]", value="Play a mood playlist", inline=False)
        embed.add_field(name="/mypl", value="Show your saved playlists", inline=False)
        embed.add_field(name="/choosemood", value="Pick a mood from dropdown", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🛠️ Queue Editing", style=discord.ButtonStyle.gray)
    async def edit_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🛠️ Editing Queue/Playlist", color=discord.Color.dark_blue())
        embed.add_field(name="/removequeue [number]", value="Remove song from queue", inline=False)
        embed.add_field(name="/clearqueue", value="Clear the queue", inline=False)
        embed.add_field(name="/removepl [number]", value="Remove song from default playlist", inline=False)
        embed.add_field(name="/clearpl", value="Clear your default playlist", inline=False)
        embed.add_field(name="/removemoodpl [mood] [number]", value="Remove song from mood playlist", inline=False)
        embed.add_field(name="/clearmoodpl [mood]", value="Clear a mood playlist", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Slash Command for Help ---
@client.tree.command(name="assist", description="Show the bot help menu with buttons")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Tansen's Help Menu",
        description="Click a button below to see detailed commands!",
        color=discord.Color.blurple()
    )
    embed.set_footer(text="✨ You can also use $play, $queue, etc.")

    await interaction.response.send_message(embed=embed, view=HelpMenu())

# --- Run the Bot ---
keep_alive()
client.run(dctokenn)
