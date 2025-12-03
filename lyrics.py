# lyrics.py
"""
Robust lyrics helpers.

Public functions:
- clean_song_title(title)
- get_lyrics_from_ovh(artist, title)
- get_lyrics_from_genius(query)   # query is usually "artist title" or "title"
- get_best_lyrics(artist, title)  # OVH first, Genius fallback

Notes:
- Requires GENIUS_API_TOKEN in env for Genius usage.
- Uses aiohttp and BeautifulSoup for scraping.
"""

import os
import re
import time
import asyncio
from typing import Optional, Dict, Any, List

import aiohttp
from bs4 import BeautifulSoup

GENIUS_TOKEN = os.getenv("GENIUS_API_TOKEN")

# simple in-memory cache: key -> (value, expiry_ts)
_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = asyncio.Lock()
CACHE_TTL = 60 * 60  # 1 hour

# limit concurrent fetches to avoid hammering external services
_FETCH_SEMAPHORE = asyncio.Semaphore(4)

# keywords that strongly indicate a non-song (article/album/calendar/etc.)
_REJECT_KEYWORDS = [
    r"\balbum\b", r"\brelease\b", r"\btracklist\b", r"\bcalendar\b",
    r"\brelease dates?\b", r"\bannounc", r"\bnews\b", r"\barticle\b",
    r"\binterview\b", r"\bcredits\b"
]


# ------------------------ helpers ------------------------

def clean_song_title(title: str) -> str:
    """
    Remove common noise from titles: (Official Video), [lyrics], HD, 4K, etc.
    """
    if not title:
        return title
    s = str(title)
    s = re.sub(r"\(.*official.*\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[.*official.*\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"official audio|official video|lyrics|mv|hd|4k", "", s, flags=re.IGNORECASE)
    s = re.sub(r"ft\.", "feat.", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _cache_get(key: str) -> Optional[str]:
    entry = _CACHE.get(key)
    if not entry:
        return None
    val, expiry = entry
    if time.time() > expiry:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: str, ttl: int = CACHE_TTL) -> None:
    _CACHE[key] = (val, time.time() + ttl)


def _looks_like_non_song(url: str, title: str) -> bool:
    u = (url or "").lower()
    t = (title or "").lower()
    for kw in _REJECT_KEYWORDS:
        if re.search(kw, u) or re.search(kw, t):
            return True
    # also reject if url contains '/albums/' or '/releases/' or '/artists/'
    if "/albums/" in u or "/releases/" in u or "/artists/" in u:
        return True
    return False


# ------------------------ OVH (fast) ------------------------

async def get_lyrics_from_ovh(artist: str, title: str) -> Optional[str]:
    """
    Fetch lyrics from lyrics.ovh (fast). Returns lyrics string or None.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return None

    cache_key = f"ovh:{artist}:{title}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
    try:
        async with _FETCH_SEMAPHORE:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=8) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    lyrics = data.get("lyrics")
                    if lyrics and lyrics.strip():
                        _cache_set(cache_key, lyrics.strip())
                        return lyrics.strip()
    except Exception:
        return None
    return None


# ------------------------ Genius (fallback) ------------------------

async def _genius_search_hits(query: str) -> List[Dict[str, Any]]:
    """
    Search Genius API and return hits (list). Empty list on failure or if token missing.
    """
    if not GENIUS_TOKEN or not query:
        return []
    url = "https://api.genius.com/search"
    headers = {"Authorization": f"Bearer {GENIUS_TOKEN}"}
    try:
        async with _FETCH_SEMAPHORE:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params={"q": query}, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    return data.get("response", {}).get("hits", []) or []
    except Exception:
        return []


def _score_genius_hit(hit: Dict[str, Any], target_artist: str = "", target_title: str = "") -> int:
    r = hit.get("result", {}) if isinstance(hit, dict) else {}
    score = 0
    pa = (r.get("primary_artist", {}).get("name") or "").lower()
    title = (r.get("title") or "").lower()
    target_artist = (target_artist or "").lower()
    target_title = (target_title or "").lower()

    if target_artist and pa and target_artist in pa:
        score += 50
    if target_title and target_title in title:
        score += 30
    if hit.get("type") == "song":
        score += 10
    return score


def _select_best_genius_result(hits: List[Dict[str, Any]], artist: str = "", title: str = "") -> Optional[Dict[str, Any]]:
    if not hits:
        return None
    artist = (artist or "").lower()
    title = (title or "").lower()

    candidates = []
    for h in hits:
        r = h.get("result", {}) or {}
        url = r.get("url") or ""
        t = r.get("title") or ""
        if _looks_like_non_song(url, t):
            continue
        candidates.append(h)

    if not candidates:
        candidates = hits

    def score(h):
        r = h.get("result", {}) or {}
        s = 0
        url = (r.get("url") or "").lower()
        if "-lyrics" in url:
            s += 100
        if h.get("type") == "song":
            s += 20
        pa = (r.get("primary_artist", {}).get("name") or "").lower()
        if artist and pa and artist in pa:
            s += 50
        title_here = (r.get("title") or "").lower()
        if title and title in title_here:
            s += 30
        idx = hits.index(h) if h in hits else 10
        s += max(0, 5 - idx)
        return s

    best = max(candidates, key=score)
    return best.get("result") if best else None


async def _fetch_genius_page(url: str) -> Optional[str]:
    try:
        async with _FETCH_SEMAPHORE:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.text()
    except Exception:
        return None


def _extract_from_genius_html(html: str) -> Optional[str]:
    """
    Extract lyrics from Genius page HTML. Prefer new-style data-lyrics-container divs.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # new style: divs with attribute data-lyrics-container="true"
    containers = soup.select('div[data-lyrics-container="true"]')
    if containers:
        parts = []
        for c in containers:
            txt = c.get_text(separator="\n", strip=True)
            if txt:
                parts.append(txt)
        if parts:
            combined = "\n\n".join(parts)
            return _sanitize_lyrics(combined)

    # fallback: legacy .lyrics container
    legacy = soup.find("div", class_="lyrics")
    if legacy:
        txt = legacy.get_text(separator="\n", strip=True)
        if txt:
            return _sanitize_lyrics(txt)

    return None


def _sanitize_lyrics(raw: str) -> str:
    """
    Remove obvious non-lyrics junk that sometimes appears on Genius pages.
    """
    if not raw:
        return raw
    lines = raw.splitlines()
    cleaned: List[str] = []
    for ln in lines:
        ln_s = ln.strip()
        if not ln_s:
            continue
        if re.match(r'^\d+\s+Contributors', ln_s, flags=re.IGNORECASE):
            continue
        if ln_s.lower().startswith("read more"):
            continue
        if ln_s.lower().startswith("visit") or ln_s.lower().startswith("see more"):
            continue
        if len(ln_s) > 200 and ln_s.count(".") > 2:
            continue
        cleaned.append(ln_s)
    out = "\n".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _is_likely_lyrics(text: str) -> bool:
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    long_paras = sum(1 for ln in lines if len(ln) > 180)
    short_lines = sum(1 for ln in lines if 1 <= len(ln) <= 80)
    bracketed = any(re.match(r"^\[.+\]$", ln) for ln in lines[:20])
    if long_paras >= 3 and short_lines < 5:
        return False
    month_pattern = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b", re.IGNORECASE)
    month_lines = sum(1 for ln in lines if month_pattern.search(ln))
    date_lines = sum(1 for ln in lines if re.match(r"^\d{1,2}\b", ln))
    if month_lines + date_lines > 5:
        return False
    if bracketed:
        return True
    first_n = lines[:20]
    short_ratio = sum(1 for ln in first_n if len(ln) < 120) / max(1, len(first_n))
    return short_ratio >= 0.5


async def get_lyrics_from_genius(query: str) -> Optional[str]:
    """
    Search Genius and pick a good song page. Strictly prefer URLs with '-lyrics', reject album/article pages,
    scrape data-lyrics-container blocks and validate the resulting text to be 'likely lyrics'. Try top alternates.
    """
    if not GENIUS_TOKEN or not query:
        return None

    cache_key = f"genius:{query}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    hits = await _genius_search_hits(query)
    if not hits:
        return None

    artist_from_q = ""
    title_from_q = ""
    if " - " in query:
        a, t = query.split(" - ", 1)
        artist_from_q, title_from_q = a.strip(), t.strip()
    else:
        artist_from_q, title_from_q = "", query

    best = _select_best_genius_result(hits, artist=artist_from_q, title=title_from_q)
    tried_urls = set()

    async def _try_result(res):
        if not res:
            return None
        url = res.get("url")
        if not url or url in tried_urls:
            return None
        tried_urls.add(url)
        html = await _fetch_genius_page(url)
        if not html:
            return None
        extracted = _extract_from_genius_html(html)
        if not extracted:
            return None
        if not _is_likely_lyrics(extracted):
            return None
        return extracted

    # 1) Try the selected best first
    if best:
        extracted = await _try_result(best)
        if extracted:
            _cache_set(cache_key, extracted)
            return extracted

    # 2) Iterate ordered hits preferring '-lyrics' URLs
    ordered = sorted(hits, key=lambda h: (0 if "-lyrics" in ((h.get("result") or {}).get("url", "").lower()) else 1))
    for h in ordered[:8]:
        res = h.get("result", {}) or {}
        extracted = await _try_result(res)
        if extracted:
            _cache_set(cache_key, extracted)
            return extracted

    # 3) Try top hits as a last resort
    for h in hits[:12]:
        res = h.get("result", {}) or {}
        extracted = await _try_result(res)
        if extracted:
            _cache_set(cache_key, extracted)
            return extracted

    return None


# ------------------------ public helper ------------------------

async def get_best_lyrics(artist: str, title: str) -> Optional[str]:
    """
    Public helper: try OVH first (fast), then Genius fallback.
    artist and title should be plain strings (artist may be empty).
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    title_clean = clean_song_title(title)

    cache_key = f"best:{artist}:{title_clean}"
    async with _CACHE_LOCK:
        cached = _cache_get(cache_key)
    if cached:
        return cached

    # OVH first
    try:
        ovh = await get_lyrics_from_ovh(artist, title_clean)
        if ovh:
            _cache_set(cache_key, ovh)
            return ovh
    except Exception:
        pass

    # Genius fallback
    q = f"{artist} {title_clean}".strip()
    try:
        gen = await get_lyrics_from_genius(q)
        if gen:
            _cache_set(cache_key, gen)
            return gen
    except Exception:
        pass

    return None


# ------------------------ optional lazy DB connection ------------------------
# kept for backward compatibility with your original file
conn = None


def get_db_conn():
    """
    Lazy PostgreSQL connection helper. Returns psycopg2 connection or None.
    Environment variable: DATABASE_URL
    """
    global conn
    if conn:
        return conn
    try:
        import psycopg2  # type: ignore
        DATABASE_URL = os.getenv("DATABASE_URL")
        if not DATABASE_URL:
            return None
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception:
        conn = None
        return None
