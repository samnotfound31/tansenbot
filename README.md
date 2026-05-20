<div align="center">

# 🎵 Tansen — Discord Music Bot

**A feature-rich Discord music bot with SoundCloud playback, synced lyrics, intelligent search, and a beautiful Now Playing UI.**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discordpy.readthedocs.io)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

</div>

---

## ✨ Features

- 🎵 **SoundCloud Playback** — Search and stream SoundCloud via Lavalink/Wavelink
- 🔍 **Intelligent Search** — Spotify-powered metadata resolution with confidence-based results
- 📋 **Queue Management** — Add, remove, reorder songs with ease
- 🔁 **Loop Mode** — Loop the current song
- 🎤 **Synced Lyrics** — Real-time karaoke-style lyrics via lrclib.net + Genius fallback
- 🖥️ **Now Playing Panel** — Live-updating embed with progress bar, equalizer animation, and control buttons
- � **Auto-Recovery** — Automatic SoundCloud 404 recovery with alternative stream selection
- 🔊 **Volume Control** — Per-guild volume adjustment
- 🤖 **Slash Commands** — Full Discord slash command support

---

## 🚀 Commands

| Command | Description |
|---------|-------------|
| `/play <song>` | Search SoundCloud and queue a song |
| `/skip` | Skip current song |
| `/stop` | Stop playback and clear queue |
| `/queue` | Show the current queue |
| `/nowplaying` | Show the Now Playing panel |
| `/lyrics` | Toggle synced karaoke lyrics |
| `/loop` | Toggle loop mode |
| `/clear` | Clear the queue |
| `/volume <0-100>` | Set volume percentage |

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
DISCORD_TOKEN=your_discord_bot_token
# Optional: for enhanced metadata resolution
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
DATABASE_PATH=/data/tansen_bot.db
GENIUS_ACCESS_TOKEN=your_genius_token
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
- Lavalink server running (see docker-compose.yml for setup)

```bash
# Install Python dependencies
pip install -r requirements.txt

# Run the bot
python run.py
```

---

## 📁 Project Structure

```
tansen/
├── run.py                    # Bot entry point
├── bot/
│   ├── bot.py               # Bot setup and initialization
│   ├── cogs/
│   │   └── music.py         # Slash commands and event handlers
│   ├── audio/
│   │   ├── player.py        # TansenPlayer (Wavelink extension)
│   │   ├── queue.py         # Queue persistence
│   │   └── lavalink.py      # Lavalink node management
│   ├── providers/
│   │   ├── playback_ranking.py    # Track scoring and ranking
│   │   ├── soundcloud.py         # SoundCloud search
│   │   ├── search_engine.py       # Search pipeline
│   │   ├── canonical_resolver.py # iTunes/Spotify metadata
│   │   ├── spotify_resolver.py    # Spotify query resolution
│   │   └── lyrics_canonicalizer.py # Lyrics metadata
│   ├── ui/
│   │   ├── nowplaying.py    # Now Playing embed and controls
│   │   └── queue_views.py    # Search result dropdowns
│   └── utils/
│       ├── formatting.py    # Text formatting utilities
│       └── cache.py         # Caching utilities
├── database.py              # SQLite database helpers
├── lyrics.py                # Lyrics fetching (lrclib + Genius)
├── keep_alive.py            # Flask keep-alive server
├── spotifyapi.py            # Spotify OAuth + search (optional)
├── requirements.txt         # Python dependencies
├── Dockerfile               # Docker build definition
├── docker-compose.yml       # Docker Compose orchestration
├── .env.example             # Environment variable template
└── lavalink/
    └── application.yml      # Lavalink configuration
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
- [Wavelink](https://github.com/Wavelink/Wavelink) for Lavalink audio streaming
- [Lavalink](https://github.com/lavalink-devs/lavalink) for audio processing
- [spotipy](https://spotipy.readthedocs.io) for Spotify API (optional metadata)
- [lrclib.net](https://lrclib.net) for synced lyrics

---

<div align="center">
Made with ❤️ for music lovers.
</div>
