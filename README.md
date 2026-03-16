<div align="center">

# 🎵 Tansen — Discord Music Bot

**A feature-rich Discord music bot with Spotify search, synced lyrics, playlist management, and a beautiful Now Playing UI.**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discordpy.readthedocs.io)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

</div>

---

## ✨ Features

- 🎵 **Spotify Search** — Search Spotify and stream via YouTube
- 📋 **Queue Management** — Add, remove, reorder songs with ease
- 🔁 **Loop Mode** — Loop the current song or the entire queue
- 🎤 **Synced Lyrics** — Real-time karaoke-style lyrics via lrclib.net + Genius fallback
- 🖥️ **Now Playing Panel** — Live-updating embed with progress bar, equalizer animation, and control buttons
- 📂 **Playlists** — Save and load personal playlists per user
- 🔊 **Volume Control** — Per-guild volume adjustment
- 🤖 **Slash Commands** — Full Discord slash command support

---

## 🚀 Commands

| Command | Description |
|---------|-------------|
| `/play <song>` | Search Spotify and queue a song |
| `/playurl <url>` | Play directly from a YouTube URL |
| `/playpl <name>` | Play a saved playlist |
| `/skip` | Skip current song |
| `/stop` | Stop playback and clear queue |
| `/queue` | Show the current queue |
| `/nowplaying` | Show the Now Playing panel |
| `/nowrics` | Fetch and display full lyrics |
| `/loop` | Toggle loop mode |
| `/clear` | Clear the queue |
| `/remove <index>` | Remove a song from the queue |
| `/assist` | Show help information |

---

## 🐳 Self-Hosting with Docker (Recommended)

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/) installed
- A [Discord Bot Token](https://discord.com/developers/applications)
- (Optional) [Spotify API credentials](https://developer.spotify.com/dashboard)

### Setup

**1. Clone the repository**
```bash
git clone https://github.com/YOUR_USERNAME/tansen.git
cd tansen
```

**2. Create your `.env` file**
```bash
cp .env.example .env
```
Then edit `.env` and fill in your tokens:
```env
DCTOKEN=your_discord_bot_token
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
DATABASE_PATH=/data/tansen_bot.db
```

**3. Build and run**
```bash
docker compose up -d --build
```

**4. View logs**
```bash
docker compose logs -f
```

**5. Stop the bot**
```bash
docker compose down
```

---

## 🛠️ Manual Setup (Without Docker)

### Requirements
- Python 3.11+
- [FFmpeg](https://ffmpeg.org/download.html) installed and in PATH

```bash
# Install Python dependencies
pip install -r requirements.txt

# Run the bot
python tansenmain.py
```

---

## 📁 Project Structure

```
tansen/
├── tansenmain.py      # Main bot file (commands, playback engine)
├── database.py        # SQLite database helpers
├── spotifyapi.py      # Spotify OAuth + search integration
├── lyrics.py          # Lyrics fetching (lrclib + Genius)
├── keep_alive.py      # Flask keep-alive server
├── requirements.txt   # Python dependencies
├── Dockerfile         # Docker build definition
├── docker-compose.yml # Docker Compose orchestration
└── .env.example       # Environment variable template
```

---

## 🔒 Security Notes

- **Never commit your `.env` file** — it contains your bot token and API secrets.
- The `.gitignore` and `.dockerignore` are pre-configured to prevent accidental exposure.
- The Docker image does **not** bake in any secrets — they are loaded at runtime from your `.env`.

---

## 🙏 Credits

Built with:
- [discord.py](https://discordpy.readthedocs.io) for Discord integration
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) for YouTube streaming
- [spotipy](https://spotipy.readthedocs.io) for Spotify API
- [lrclib.net](https://lrclib.net) for synced lyrics

---

<div align="center">
Made with ❤️ for music lovers.
</div>
