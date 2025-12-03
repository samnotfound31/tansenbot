# database.py
# Central DB utilities. Uses sqlite3 and stores a single DB file.
#
# Provides:
# - init_db()
# - connect / get_conn()
# - Queue functions: save_queue, load_queue, delete_queue
# - Playlist functions: save_playlist, load_playlists, delete_playlist
# - Spotify user token functions: save_spotify_token, get_spotify_token_for_user, delete_spotify_token
#   (now supports access_token, refresh_token, expires_at)
# - Guild settings: save_guild_settings, load_guild_settings
# - Generic token KV: save_token/get_token (used to cache app tokens)
#
# The module auto-creates tables and will add missing spotify token columns via migration.

import os
import sqlite3
import json
import threading
import time
from contextlib import contextmanager
from typing import Optional, Dict, Any

DB_LOCK = threading.Lock()

def _db_path() -> str:
    # Allow explicit path via env var DATABASE_PATH for backwards compatibility
    env = os.getenv("DATABASE_PATH")
    if env:
        return env
    # default to a file next to this module
    return os.path.join(os.path.dirname(__file__), "tansen_bot.db")

DB_PATH = _db_path()

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Create required tables if they don't exist."""
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            # queues stored as JSON list per guild (simpler to manage)
            c.execute("""
            CREATE TABLE IF NOT EXISTS queues (
                guild_id TEXT PRIMARY KEY,
                queue_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """)
            # playlists per user
            c.execute("""
            CREATE TABLE IF NOT EXISTS user_playlists (
                user_id TEXT,
                name TEXT,
                description TEXT,
                songs TEXT,
                PRIMARY KEY (user_id, name)
            )
            """)
            # spotify user tokens for OAuth
            # include expires_at column (nullable) to store token expiry
            c.execute("""
            CREATE TABLE IF NOT EXISTS spotify_users (
                user_id TEXT PRIMARY KEY,
                access_token TEXT,
                refresh_token TEXT,
                expires_at INTEGER
            )
            """)
            # guild settings
            c.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                volume_level REAL DEFAULT 1.0,
                is_looping INTEGER DEFAULT 0,
                last_played TEXT,
                previous_played TEXT
            )
            """)
            # key-value generic tokens (for caching app tokens like spotify client credentials)
            c.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """)
            conn.commit()

def migrate():
    """
    Programmatic migration utility.
    - Adds refresh_token and expires_at columns if missing in spotify_users.
    Safe to call multiple times.
    """
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            # Ensure spotify_users has all required columns
            c.execute("PRAGMA table_info(spotify_users)")
            cols = [row["name"] for row in c.fetchall()]
            if "refresh_token" not in cols:
                try:
                    c.execute("ALTER TABLE spotify_users ADD COLUMN refresh_token TEXT")
                except Exception:
                    pass
            if "expires_at" not in cols:
                try:
                    c.execute("ALTER TABLE spotify_users ADD COLUMN expires_at INTEGER")
                except Exception:
                    pass
            conn.commit()

# initialize DB and run migrations at import
try:
    init_db()
    migrate()
except Exception:
    # do not crash on import if path not writable or other issue
    pass

# --- Queue functions (JSON stored per guild) ---
def save_queue(guild_id: str, queue: list) -> None:
    with DB_LOCK:
        ts = int(time.time())
        with connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO queues (guild_id, queue_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET queue_json=excluded.queue_json, updated_at=excluded.updated_at
            """, (str(guild_id), json.dumps(queue), ts))
            conn.commit()

def load_queue(guild_id: str) -> list:
    with connect() as conn:
        c = conn.cursor()
        c.execute("SELECT queue_json FROM queues WHERE guild_id = ?", (str(guild_id),))
        row = c.fetchone()
        if not row:
            return []
        try:
            return json.loads(row["queue_json"])
        except Exception:
            return []

def delete_queue(guild_id: str) -> None:
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM queues WHERE guild_id = ?", (str(guild_id),))
            conn.commit()

# --- Playlist functions ---
def save_playlist(user_id: str, playlist_name: str, description: str, songs: list) -> None:
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO user_playlists (user_id, name, description, songs)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, name) DO UPDATE SET description=excluded.description, songs=excluded.songs
            """, (str(user_id), playlist_name, description, json.dumps(songs)))
            conn.commit()

def load_playlists(user_id: str) -> Dict[str, Any]:
    with connect() as conn:
        c = conn.cursor()
        c.execute("SELECT name, description, songs FROM user_playlists WHERE user_id = ?", (str(user_id),))
        out = {}
        rows = c.fetchall()
        for row in rows:
            name = row["name"]
            desc = row["description"]
            songs = row["songs"]
            try:
                out[name] = {"description": desc, "songs": json.loads(songs)}
            except Exception:
                out[name] = {"description": desc, "songs": []}
        return out

def delete_playlist(user_id: str, playlist_name: str) -> None:
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM user_playlists WHERE user_id = ? AND name = ?", (str(user_id), playlist_name))
            conn.commit()

# --- Spotify user token functions ---
def save_spotify_token(user_id: str, access_token: str, refresh_token: Optional[str] = None, expires_at: Optional[int] = None) -> None:
    """
    Upsert the user's Spotify token info.
    - user_id: str
    - access_token: str
    - refresh_token: optional str
    - expires_at: optional int (unix timestamp)
    """
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO spotify_users (user_id, access_token, refresh_token, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    access_token=excluded.access_token,
                    refresh_token=COALESCE(excluded.refresh_token, spotify_users.refresh_token),
                    expires_at=COALESCE(excluded.expires_at, spotify_users.expires_at)
            """, (str(user_id), access_token, refresh_token, expires_at))
            conn.commit()

def get_spotify_token_for_user(user_id: str) -> Optional[str]:
    """
    Return a JSON string with keys: access_token, refresh_token, expires_at (if present).
    If only an access token exists (legacy), returns JSON string {"access_token": "..."}.
    Returns None if not found.
    """
    with connect() as conn:
        c = conn.cursor()
        c.execute("SELECT access_token, refresh_token, expires_at FROM spotify_users WHERE user_id = ?", (str(user_id),))
        row = c.fetchone()
        if not row:
            return None
        access_token = row["access_token"]
        refresh_token = row["refresh_token"] if "refresh_token" in row.keys() else None
        expires_at = row["expires_at"] if "expires_at" in row.keys() else None
        obj: Dict[str, Any] = {}
        if access_token:
            obj["access_token"] = access_token
        if refresh_token:
            obj["refresh_token"] = refresh_token
        if expires_at is not None:
            try:
                obj["expires_at"] = int(expires_at)
            except Exception:
                pass
        return json.dumps(obj)

def delete_spotify_token(user_id: str) -> None:
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM spotify_users WHERE user_id = ?", (str(user_id),))
            conn.commit()

# --- Guild settings ---
def save_guild_settings(guild_id: str, volume_level: float = 1.0, is_looping: bool = False,
                        last_played: Optional[dict] = None, previous_played: Optional[dict] = None) -> None:
    with DB_LOCK:
        with connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO guild_settings (guild_id, volume_level, is_looping, last_played, previous_played)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    volume_level=excluded.volume_level,
                    is_looping=excluded.is_looping,
                    last_played=excluded.last_played,
                    previous_played=excluded.previous_played
            """, (
                str(guild_id),
                float(volume_level),
                int(bool(is_looping)),
                json.dumps(last_played) if last_played is not None else None,
                json.dumps(previous_played) if previous_played is not None else None
            ))
            conn.commit()

def load_guild_settings(guild_id: str) -> Dict[str, Any]:
    with connect() as conn:
        c = conn.cursor()
        c.execute("SELECT volume_level, is_looping, last_played, previous_played FROM guild_settings WHERE guild_id = ?", (str(guild_id),))
        row = c.fetchone()
        if not row:
            return {"volume_level": 1.0, "is_looping": False, "last_played": None, "previous_played": None}
        volume = row["volume_level"]
        looping = row["is_looping"]
        last = row["last_played"]
        prev = row["previous_played"]
        try:
            last_parsed = json.loads(last) if last else None
        except Exception:
            last_parsed = None
        try:
            prev_parsed = json.loads(prev) if prev else None
        except Exception:
            prev_parsed = None
        return {"volume_level": float(volume), "is_looping": bool(looping), "last_played": last_parsed, "previous_played": prev_parsed}

# --- Generic KV store (for caching app tokens, etc.) ---
def save_token(key: str, value: str) -> None:
    with DB_LOCK:
        ts = int(time.time())
        with connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO kv_store (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, value, ts))
            conn.commit()

def get_token(key: str) -> Optional[str]:
    with connect() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = c.fetchone()
        if row:
            return row[0]
        return None

# ---- Migration helper (manual usage) ----
# If you prefer to run the ALTER TABLE statements manually using sqlite cli, use:
# ALTER TABLE spotify_users ADD COLUMN refresh_token TEXT;
# ALTER TABLE spotify_users ADD COLUMN expires_at INTEGER;
#
# Or run this module interactively:
# python -c "import database; database.migrate()"
