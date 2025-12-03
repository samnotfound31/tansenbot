# spotifyapi.py
"""
Robust Spotify helper.

Features:
- get_spotify_oauth_url(state=None)
- exchange_code_for_token_sync(code, state=None) -> bool
- exchange_code_for_token_async(...)
- get_spotify_token(user_id) -> access_token or None (auto-refreshes)
- get_spotify_token_async(...)
- get_app_spotify_token() -> app-level token (cached)
- search_spotify_tracks(query, limit=8) (uses app token)
- async counterparts for long-running operations (run in executor)

This module is defensive about database helper signatures:
- It will call save_spotify_token(user_id, access, refresh, expires_at) if available,
  or fallback to fewer-arg variants if your database.py implements an older API.

Required env vars:
- SPOTIFY_CLIENT_ID
- SPOTIFY_CLIENT_SECRET
- SPOTIFY_REDIRECT_URI  # must exactly match value registered in Spotify Developer Dashboard
- (optional) SPOTIFY_SCOPE
"""

from typing import Optional, Dict, Any, List
import os
import time
import json
import base64
import requests
import asyncio
import inspect
from urllib.parse import urlencode

# Import database helpers expected to exist in your project. If some are missing,
# the module will raise at import time (so fix DB first). Typical names:
# save_spotify_token(user_id, access, refresh, expires_at)
# get_spotify_token_for_user(user_id) -> either dict or JSON string
# delete_spotify_token(user_id)
# save_token(key, value)
# get_token(key)
from database import (
    save_spotify_token,
    get_spotify_token_for_user,
    delete_spotify_token,
    save_token,
    get_token,
)

# env
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "https://example.org/callback")
SPOTIFY_SCOPE = os.getenv("SPOTIFY_SCOPE", "user-read-private playlist-read-private playlist-read-collaborative")
APP_TOKEN_KEY = "spotify_app_token_v1"
TOKEN_EXPIRY_MARGIN = 30  # seconds safety margin for expiry checks

# util
def _now() -> int:
    return int(time.time())

def _post(url: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 8) -> Optional[Dict[str, Any]]:
    try:
        r = requests.post(url, data=data, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def _get(url: str, params: Dict[str, Any] = None, headers: Dict[str, str] = None, timeout: int = 8) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

# ---------- OAuth URL ----------
def get_spotify_oauth_url(state: Optional[str] = None) -> str:
    """Return Spotify authorize URL. State is typically the Discord user id."""
    if not SPOTIFY_CLIENT_ID:
        raise RuntimeError("SPOTIFY_CLIENT_ID not set")

    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPE or "",
    }
    if state:
        params["state"] = str(state)

    # use urlencode for safe encoding; safe=':/' so redirect_uri remains readable
    qs = urlencode(params, safe=":/")
    return f"https://accounts.spotify.com/authorize?{qs}"

# ---------- token exchange helpers ----------
def _exchange_code_for_token_sync(code: str) -> Optional[Dict[str, Any]]:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
    }
    auth = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    headers = {"Authorization": f"Basic {base64.b64encode(auth.encode()).decode()}"}
    return _post(url, data, headers=headers)

def _refresh_token_sync(refresh_token: str) -> Optional[Dict[str, Any]]:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    auth = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    headers = {"Authorization": f"Basic {base64.b64encode(auth.encode()).decode()}"}
    return _post(url, data, headers=headers)

# ---------- DB-adapter: save user token robustly ----------
def _save_user_token_db(user_id: str, access_token: str, refresh_token: Optional[str], expires_at: Optional[int]) -> bool:
    """
    Attempt to call save_spotify_token with the signature your database module offers.
    Accepts multiple variants:
       save_spotify_token(user_id, access)
       save_spotify_token(user_id, access, refresh)
       save_spotify_token(user_id, access, refresh, expires_at)
    Returns True on success, False on failure.
    """
    try:
        sig = inspect.signature(save_spotify_token)
        params = len(sig.parameters)
    except Exception:
        # unknown signature; attempt 4-arg call and fallback
        params = None

    # Try the most complete call first, then fallbacks
    try:
        if params is None or params >= 4:
            # common modern signature
            save_spotify_token(str(user_id), access_token, refresh_token, expires_at)
            return True
    except TypeError:
        pass
    try:
        # 3-arg (user, access, refresh)
        save_spotify_token(str(user_id), access_token, refresh_token)
        return True
    except TypeError:
        pass
    try:
        # 2-arg (user, access)
        save_spotify_token(str(user_id), access_token)
        return True
    except Exception:
        return False

# ---------- Exchange and save ----------
def exchange_code_for_token_sync(code: str, state: Optional[str] = None) -> bool:
    """
    Exchange the authorization code with Spotify and save tokens to DB under `state`.
    `state` should be the user's id (string). Returns True on success.
    """
    tok = _exchange_code_for_token_sync(code)
    if not tok:
        return False

    access = tok.get("access_token")
    refresh = tok.get("refresh_token")
    expires_in = int(tok.get("expires_in", 3600))
    expires_at = _now() + expires_in

    if state:
        # save to DB using robust adapter
        ok = _save_user_token_db(str(state), access, refresh, expires_at)
        return ok
    return False

async def exchange_code_for_token_async(code: str, state: Optional[str] = None) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, exchange_code_for_token_sync, code, state)

# ---------- App token (client credentials) ----------
def _fetch_app_token_from_spotify() -> Optional[Dict[str, Any]]:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    url = "https://accounts.spotify.com/api/token"
    auth = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    headers = {"Authorization": f"Basic {base64.b64encode(auth.encode()).decode()}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials"}
    return _post(url, data, headers=headers)

def get_app_spotify_token() -> Optional[str]:
    raw = get_token(APP_TOKEN_KEY)
    if raw:
        try:
            obj = json.loads(raw)
            if obj and "access_token" in obj and "expires_at" in obj:
                if obj["expires_at"] - TOKEN_EXPIRY_MARGIN > _now():
                    return obj["access_token"]
        except Exception:
            # malformed cached entry; ignore
            pass

    tok = _fetch_app_token_from_spotify()
    if not tok:
        return None
    access = tok.get("access_token")
    expires_in = int(tok.get("expires_in", 3600))
    if access:
        payload = {"access_token": access, "expires_at": _now() + expires_in}
        # store JSON string via provided save_token
        save_token(APP_TOKEN_KEY, json.dumps(payload))
        return access
    return None

async def get_app_spotify_token_async() -> Optional[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_app_spotify_token)

# ---------- User token access & refresh ----------
def _normalize_token_obj(raw) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    except Exception:
        return None
    return None

def get_spotify_token(user_id: str) -> Optional[str]:
    """
    Return a valid access_token for a user (sync). Refreshes if needed.
    Expects get_spotify_token_for_user(user_id) to return either a JSON string or dict containing:
      {access_token, refresh_token, expires_at}
    If refresh succeeds, token is saved back to DB.
    """
    raw = get_spotify_token_for_user(str(user_id))
    token_obj = _normalize_token_obj(raw)

    # Some older DB implementations may have stored only a single access_token string.
    if token_obj is None:
        # try treating raw as a plain access token
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    access = token_obj.get("access_token")
    refresh = token_obj.get("refresh_token")
    expires_at = int(token_obj.get("expires_at", 0))

    if access and expires_at - TOKEN_EXPIRY_MARGIN > _now():
        return access

    # Need refresh
    if not refresh:
        return None

    new = _refresh_token_sync(refresh)
    if not new:
        return None

    new_access = new.get("access_token")
    new_refresh = new.get("refresh_token") or refresh
    expires_in = int(new.get("expires_in", 3600))
    new_expires_at = _now() + expires_in

    # Save refreshed tokens
    _save_user_token_db(str(user_id), new_access, new_refresh, new_expires_at)
    return new_access

async def get_spotify_token_async(user_id: str) -> Optional[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_spotify_token, user_id)

# ---------- Delete user token ----------
def delete_spotify_user_token(user_id: str) -> None:
    try:
        delete_spotify_token(str(user_id))
    except Exception:
        pass

# ---------- Search helper (uses app token) ----------
def search_spotify_tracks(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    token = get_app_spotify_token()
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": query, "type": "track", "limit": limit}
    try:
        r = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        return data.get("tracks", {}).get("items", []) or []
    except Exception:
        return []

async def search_spotify_tracks_async(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, search_spotify_tracks, query, limit)

# ---------- exports ----------
__all__ = [
    "get_spotify_oauth_url",
    "exchange_code_for_token_sync",
    "exchange_code_for_token_async",
    "get_spotify_token",
    "get_spotify_token_async",
    "delete_spotify_user_token",
    "get_app_spotify_token",
    "get_app_spotify_token_async",
    "search_spotify_tracks",
    "search_spotify_tracks_async",
]
