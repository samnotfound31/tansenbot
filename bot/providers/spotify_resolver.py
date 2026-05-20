"""
bot.providers.spotify_resolver
Spotify as a metadata intelligence / canonical truth source.

Resolves user queries into rich canonical metadata (title, artist, album,
duration, popularity, ISRC) WITHOUT using Spotify for playback.

Intelligence features:
- Query normalization & cleanup
- Featured artist parsing (ft., feat., &, x, with)
- Modifier detection (remix, slowed, live, acoustic, etc.)
- Spotify result re-ranking (prefer canonical studio versions)
- Artist alias normalization
- Partial query inference
- TTL caching with normalized keys
- Structured debug metadata
"""

import re
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

from bot.utils.cache import TTLCache

logger = logging.getLogger("tansen.spotify_resolver")

# ══════════════════════════════════════════════════════════════════════════════
# CACHING
# ══════════════════════════════════════════════════════════════════════════════

_metadata_cache = TTLCache(default_ttl=3600)      # resolved CanonicalTrack (1h)
_normalization_cache = TTLCache(default_ttl=7200)  # normalized query strings (2h)


# ══════════════════════════════════════════════════════════════════════════════
# ARTIST ALIAS SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

# Lightweight alias map: alias → canonical name
# Used to normalize known aliases in user queries before Spotify search.
_ARTIST_ALIASES: Dict[str, str] = {
    "abel": "The Weeknd",
    "la flame": "Travis Scott",
    "cactus jack": "Travis Scott",
    "k dot": "Kendrick Lamar",
    "k.dot": "Kendrick Lamar",
    "kung fu kenny": "Kendrick Lamar",
    "ye": "Kanye West",
    "yeezy": "Kanye West",
    "slim shady": "Eminem",
    "marshall mathers": "Eminem",
    "childish gambino": "Childish Gambino",
    "donald glover": "Childish Gambino",
    "posty": "Post Malone",
    "post malone": "Post Malone",
    "drizzy": "Drake",
    "champagne papi": "Drake",
    "a$ap rocky": "A$AP Rocky",
    "asap rocky": "A$AP Rocky",
    "trav": "Travis Scott",
    "sza": "SZA",
    "jcole": "J. Cole",
    "j cole": "J. Cole",
    "mj": "Michael Jackson",
    "freddie mercury": "Queen",
    "bey": "Beyoncé",
    "beyonce": "Beyoncé",
    "riri": "Rihanna",
    "bieber": "Justin Bieber",
    "jb": "Justin Bieber",
    "ariana": "Ariana Grande",
    "ari": "Ariana Grande",
    "weeknd": "The Weeknd",
    "the weeknd": "The Weeknd",
    "tswift": "Taylor Swift",
    "ed sheeran": "Ed Sheeran",
    "dua lipa": "Dua Lipa",
    "billie eilish": "Billie Eilish",
    "bad bunny": "Bad Bunny",
    "juice": "Juice WRLD",
    "juice wrld": "Juice WRLD",
    "xxxtentacion": "XXXTENTACION",
    "xxx": "XXXTENTACION",
    "lil uzi": "Lil Uzi Vert",
    "uzi": "Lil Uzi Vert",
    "thugger": "Young Thug",
    "young thug": "Young Thug",
    "21 savage": "21 Savage",
    "carti": "Playboi Carti",
    "playboi carti": "Playboi Carti",
}

# Build a lowercase lookup for fast matching
_ALIAS_LOOKUP: Dict[str, str] = {k.lower(): v for k, v in _ARTIST_ALIASES.items()}


def _resolve_alias(text: str) -> Optional[str]:
    """Check if text matches a known artist alias. Returns canonical name or None."""
    return _ALIAS_LOOKUP.get(text.lower().strip())


# ══════════════════════════════════════════════════════════════════════════════
# MODIFIER DETECTION
# ══════════════════════════════════════════════════════════════════════════════

# Modifiers that indicate a non-canonical version
_MODIFIER_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(slowed)\b", re.IGNORECASE), "slowed"),
    (re.compile(r"\b(reverb)\b", re.IGNORECASE), "reverb"),
    (re.compile(r"\bsped\s*up\b", re.IGNORECASE), "sped up"),
    (re.compile(r"\b(remix)\b", re.IGNORECASE), "remix"),
    (re.compile(r"\b(acoustic)\b", re.IGNORECASE), "acoustic"),
    (re.compile(r"\b(live)\b", re.IGNORECASE), "live"),
    (re.compile(r"\b(instrumental)\b", re.IGNORECASE), "instrumental"),
    (re.compile(r"\b(nightcore)\b", re.IGNORECASE), "nightcore"),
    (re.compile(r"\b(phonk)\b", re.IGNORECASE), "phonk"),
    (re.compile(r"\bbass\s*boost(ed)?\b", re.IGNORECASE), "bass boosted"),
    (re.compile(r"\b(lofi|lo-fi)\b", re.IGNORECASE), "lofi"),
    (re.compile(r"\b(mashup)\b", re.IGNORECASE), "mashup"),
    (re.compile(r"\b(cover)\b", re.IGNORECASE), "cover"),
    (re.compile(r"\b(karaoke)\b", re.IGNORECASE), "karaoke"),
    (re.compile(r"\b8d\s*audio\b", re.IGNORECASE), "8d audio"),
    (re.compile(r"\b(edit)\b", re.IGNORECASE), "edit"),
]


def _detect_modifiers(query: str) -> List[str]:
    """Detect version modifiers in the query. Returns list of detected modifiers."""
    found = []
    for pat, label in _MODIFIER_PATTERNS:
        if pat.search(query):
            found.append(label)
    return found


def _strip_modifiers(query: str) -> str:
    """Remove detected modifiers from query for cleaner Spotify search."""
    cleaned = query
    for pat, _ in _MODIFIER_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# ══════════════════════════════════════════════════════════════════════════════
# FEATURED ARTIST PARSING
# ══════════════════════════════════════════════════════════════════════════════

# Patterns that indicate featured artists in user queries
_FEAT_PATTERNS = [
    re.compile(r"\s+feat\.?\s+", re.IGNORECASE),
    re.compile(r"\s+ft\.?\s+", re.IGNORECASE),
    re.compile(r"\s+featuring\s+", re.IGNORECASE),
    re.compile(r"\s+with\s+", re.IGNORECASE),
]

# Separators between multiple artists
_ARTIST_SEPARATORS = re.compile(r"\s*[&x,]\s*|\s+and\s+", re.IGNORECASE)


def _parse_featured_artists(query: str) -> Tuple[str, List[str]]:
    """Parse featured artists from query.

    Returns (base_query, [featured_artists]).
    Example: "kendrick lamar ft sza" → ("kendrick lamar", ["sza"])

    Artist names are capped at 3 words max to avoid absorbing song titles.
    """
    for pat in _FEAT_PATTERNS:
        match = pat.search(query)
        if match:
            base = query[: match.start()].strip()
            feat_part = query[match.end():].strip()
            # Split multiple featured artists
            raw_feats = [a.strip() for a in _ARTIST_SEPARATORS.split(feat_part) if a.strip()]
            # Cap each artist name to 3 words (rest is likely song title)
            feats = []
            leftover_parts = []
            for raw in raw_feats:
                words = raw.split()
                if len(words) <= 3:
                    feats.append(raw)
                else:
                    # First 1-3 words are likely the artist, rest is song title
                    # Heuristic: check if 2nd/3rd word looks like a common song word
                    feats.append(" ".join(words[:1]))
                    leftover_parts.append(" ".join(words[1:]))
            # Append leftover (song title fragments) back to base
            if leftover_parts:
                base = f"{base} {' '.join(leftover_parts)}"
            return base, feats
    return query, []


# ══════════════════════════════════════════════════════════════════════════════
# QUERY NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════

# Noise patterns to remove for cleaner search
_QUERY_NOISE = [
    re.compile(r"\(official.*?\)", re.IGNORECASE),
    re.compile(r"\[official.*?\]", re.IGNORECASE),
    re.compile(r"\(audio\)", re.IGNORECASE),
    re.compile(r"\[audio\]", re.IGNORECASE),
    re.compile(r"\(lyrics?\)", re.IGNORECASE),
    re.compile(r"\[lyrics?\]", re.IGNORECASE),
    re.compile(r"\[hd\]|\[4k\]|\(hd\)|\(4k\)", re.IGNORECASE),
    re.compile(r"\bofficial\s*(audio|video|music\s*video|visualizer)\b", re.IGNORECASE),
    re.compile(r"\blyric\s*video\b", re.IGNORECASE),
]


@dataclass
class NormalizedQuery:
    """Result of query normalization — captures all parsed intent."""

    original: str
    cleaned: str               # noise-stripped, punctuation-cleaned
    spotify_query: str         # optimized for Spotify API
    modifiers: List[str]       # detected modifiers (remix, slowed, etc.)
    parsed_artist: Optional[str]   # artist parsed from query (if any)
    featured_artists: List[str]    # featured artists parsed
    alias_resolved: Optional[str]  # alias that was resolved (if any)
    wants_canonical: bool      # True if user wants original studio version


def normalize_query(raw: str) -> NormalizedQuery:
    """Full query normalization pipeline.

    Steps:
    1. Strip noise patterns ([official audio], etc.)
    2. Detect and extract modifiers (remix, slowed, etc.)
    3. Parse featured artists (ft., feat., &, x, with)
    4. Detect artist-title separator patterns (artist - title)
    5. Resolve artist aliases
    6. Build optimized Spotify query
    """
    # Check normalization cache
    cache_key = raw.lower().strip()
    cached = _normalization_cache.get(cache_key)
    if cached is not None:
        return cached

    original = raw.strip()
    text = original

    # Step 1: Strip noise
    for pat in _QUERY_NOISE:
        text = pat.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Step 2: Detect modifiers
    modifiers = _detect_modifiers(text)
    wants_canonical = len(modifiers) == 0

    # Strip modifiers for Spotify search (Spotify handles canonical better)
    # But only if modifiers weren't the ENTIRE point of the query
    spotify_text = _strip_modifiers(text) if modifiers else text

    # Step 3: Parse featured artists
    spotify_text, featured_artists = _parse_featured_artists(spotify_text)

    # Step 4: Detect "Artist - Title" pattern
    parsed_artist: Optional[str] = None
    for sep in (" - ", " – ", " — "):
        if sep in spotify_text:
            parts = spotify_text.split(sep, 1)
            parsed_artist = parts[0].strip()
            spotify_text = f"{parts[1].strip()} {parts[0].strip()}"
            break

    # Step 5: Resolve artist aliases
    alias_resolved: Optional[str] = None
    words = spotify_text.split()

    # 5a: If we already parsed an artist (from "artist - title"), check if it's an alias
    if parsed_artist:
        artist_canonical = _resolve_alias(parsed_artist)
        if artist_canonical:
            alias_resolved = artist_canonical
            original_alias = parsed_artist
            parsed_artist = artist_canonical
            # Remove the original alias from spotify_text and append canonical name
            spotify_text = spotify_text.replace(original_alias, "")
            spotify_text = re.sub(r"\s+", " ", spotify_text).strip()
            spotify_text = f"{spotify_text} {artist_canonical}".strip()
            words = spotify_text.split()

    # 5b: Check if a prefix of the query is a known artist alias
    if not alias_resolved:
        for length in range(min(4, len(words)), 0, -1):
            prefix = " ".join(words[:length])
            canonical_name = _resolve_alias(prefix)
            if canonical_name:
                remaining = " ".join(words[length:])
                # Skip single-word alias matches when remaining is 3+ words
                # (likely a song title that happens to start with the alias word)
                remaining_word_count = len(words) - length
                if length == 1 and remaining_word_count > 2:
                    continue
                alias_resolved = canonical_name
                spotify_text = f"{remaining} {canonical_name}".strip() if remaining else canonical_name
                if not parsed_artist:
                    parsed_artist = canonical_name
                break

    # Also check featured artists for aliases
    resolved_feats = []
    for feat in featured_artists:
        resolved = _resolve_alias(feat)
        resolved_feats.append(resolved if resolved else feat)
    featured_artists = resolved_feats

    # Step 6: Build final Spotify query
    # Include featured artists for better Spotify matching
    spotify_query = spotify_text
    if featured_artists:
        spotify_query = f"{spotify_text} {' '.join(featured_artists)}"

    # Clean up extra whitespace
    spotify_query = re.sub(r"\s+", " ", spotify_query).strip()

    result = NormalizedQuery(
        original=original,
        cleaned=text,
        spotify_query=spotify_query,
        modifiers=modifiers,
        parsed_artist=parsed_artist,
        featured_artists=featured_artists,
        alias_resolved=alias_resolved,
        wants_canonical=wants_canonical,
    )

    _normalization_cache.set(cache_key, result, ttl=7200)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SPOTIFY RESULT RE-RANKING
# ══════════════════════════════════════════════════════════════════════════════

# Album types that indicate non-canonical versions
_NON_CANONICAL_ALBUM_PATTERNS = [
    re.compile(r"\bremaster(ed)?\b", re.IGNORECASE),
    re.compile(r"\bdeluxe\b", re.IGNORECASE),
    re.compile(r"\bkaraoke\b", re.IGNORECASE),
    re.compile(r"\btribute\b", re.IGNORECASE),
    re.compile(r"\bcover\b", re.IGNORECASE),
    re.compile(r"\bcompilation\b", re.IGNORECASE),
    re.compile(r"\bgreatest\s*hits\b", re.IGNORECASE),
]

# Title modifiers that indicate non-original
_NON_CANONICAL_TITLE_PATTERNS = [
    re.compile(r"\bremaster(ed)?\b", re.IGNORECASE),
    re.compile(r"\b(live)\b", re.IGNORECASE),
    re.compile(r"\b(acoustic)\b", re.IGNORECASE),
    re.compile(r"\b(remix)\b", re.IGNORECASE),
    re.compile(r"\b(cover)\b", re.IGNORECASE),
    re.compile(r"\b(karaoke)\b", re.IGNORECASE),
    re.compile(r"\b(instrumental)\b", re.IGNORECASE),
    re.compile(r"\b(radio edit)\b", re.IGNORECASE),
    re.compile(r"\b(demo)\b", re.IGNORECASE),
]


def _spotify_rerank_score(
    track: Dict[str, Any],
    norm: NormalizedQuery,
) -> float:
    """Score a Spotify result for internal re-ranking.

    Higher = better match for the user's intent.
    Combines: popularity, title exactness, canonical preference, artist match.
    """
    name = (track.get("name") or "").lower()
    artists = [a.get("name", "").lower() for a in (track.get("artists") or [])]
    album_name = ((track.get("album") or {}).get("name") or "").lower()
    popularity = int(track.get("popularity") or 0)
    query_lower = norm.spotify_query.lower()

    score = 0.0

    # ── Popularity (0–30) ─────────────────────────────────────────────────
    score += (popularity / 100.0) * 30.0

    # ── Title exactness (0–35) ────────────────────────────────────────────
    # Extract just the song title portion from the query for comparison
    query_title = norm.cleaned.lower()
    if norm.parsed_artist:
        query_title = query_title.replace(norm.parsed_artist.lower(), "").strip(" -–—")
    query_title = _strip_modifiers(query_title).strip()

    if query_title and query_title in name:
        score += 35.0
    elif name and name in query_lower:
        score += 28.0
    else:
        # Partial word overlap
        q_words = set(query_title.split())
        t_words = set(name.split())
        if q_words and t_words:
            overlap = len(q_words & t_words) / max(len(q_words), 1)
            score += overlap * 20.0

    # ── Artist match (0–20) ───────────────────────────────────────────────
    if norm.parsed_artist:
        artist_lower = norm.parsed_artist.lower()
        if any(artist_lower in a or a in artist_lower for a in artists):
            score += 20.0
        elif norm.alias_resolved and norm.alias_resolved.lower() in " ".join(artists):
            score += 18.0

    # ── Canonical version preference (0–15 or penalty) ────────────────────
    if norm.wants_canonical:
        is_canonical = True
        for pat in _NON_CANONICAL_TITLE_PATTERNS:
            if pat.search(name):
                is_canonical = False
                break
        for pat in _NON_CANONICAL_ALBUM_PATTERNS:
            if pat.search(album_name):
                is_canonical = False
                break
        if is_canonical:
            score += 15.0
        else:
            score -= 10.0
    else:
        # User wants a specific version — check if track matches modifiers
        for mod in norm.modifiers:
            if mod.lower() in name:
                score += 10.0
                break

    return score


def _rerank_spotify_results(
    results: List[Dict[str, Any]],
    norm: NormalizedQuery,
) -> List[Dict[str, Any]]:
    """Re-rank Spotify results using intelligent scoring."""
    if not results:
        return results

    scored = [(track, _spotify_rerank_score(track, norm)) for track in results]
    scored.sort(key=lambda x: x[1], reverse=True)

    if scored:
        logger.debug(
            "Spotify re-rank: top='%s' (score=%.1f), bottom='%s' (score=%.1f)",
            (scored[0][0].get("name") or "?"),
            scored[0][1],
            (scored[-1][0].get("name") or "?"),
            scored[-1][1],
        )

    return [track for track, _ in scored]


# ══════════════════════════════════════════════════════════════════════════════
# CANONICAL TRACK DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CanonicalTrack:
    """Canonical track metadata resolved from Spotify."""

    title: str
    artists: List[str]
    album: str
    duration_ms: int
    popularity: int  # 0–100
    isrc: Optional[str] = None
    spotify_url: Optional[str] = None

    # Intelligence metadata
    modifiers_detected: List[str] = field(default_factory=list)
    featured_artists: List[str] = field(default_factory=list)
    artist_inferred: bool = False
    canonical_version: bool = True
    debug: Dict[str, Any] = field(default_factory=dict)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def all_artists_str(self) -> str:
        return " ".join(self.artists)

    @property
    def duration_sec(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def enriched_query(self) -> str:
        """Build the enriched SoundCloud search query from canonical metadata.

        Strategy:
        - Always include title + primary artist
        - Include featured artists (max 2)
        - If user requested a specific modifier, append it
        """
        parts = [self.title]

        if self.primary_artist:
            parts.append(self.primary_artist)

        # Featured artists (helps find collabs on SoundCloud)
        feats = self.featured_artists or [a for a in self.artists[1:3]]
        for feat in feats[:2]:
            if feat and feat != self.primary_artist:
                parts.append(feat)

        # If user explicitly requested a modifier, include it in SC search
        if self.modifiers_detected:
            parts.extend(self.modifiers_detected[:1])

        return " ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# PARSING
# ══════════════════════════════════════════════════════════════════════════════

def _parse_spotify_track(
    track: Dict[str, Any],
    norm: Optional[NormalizedQuery] = None,
) -> Optional[CanonicalTrack]:
    """Parse a raw Spotify API track object into a CanonicalTrack."""
    if not track:
        return None
    try:
        title = track.get("name") or ""
        artists = [a.get("name", "") for a in (track.get("artists") or []) if a.get("name")]
        album = (track.get("album") or {}).get("name", "")
        duration_ms = int(track.get("duration_ms") or 0)
        popularity = int(track.get("popularity") or 0)
        isrc = (track.get("external_ids") or {}).get("isrc")
        spotify_url = (track.get("external_urls") or {}).get("spotify")

        if not title:
            return None

        # Determine if this is a canonical version
        is_canonical = True
        title_lower = title.lower()
        for pat in _NON_CANONICAL_TITLE_PATTERNS:
            if pat.search(title_lower):
                is_canonical = False
                break

        # Extract featured artists from Spotify's artist list
        featured = artists[1:] if len(artists) > 1 else []

        # Determine modifiers and artist inference from normalization
        modifiers = norm.modifiers if norm else []
        artist_inferred = norm is not None and norm.parsed_artist is None

        return CanonicalTrack(
            title=title,
            artists=artists,
            album=album,
            duration_ms=duration_ms,
            popularity=popularity,
            isrc=isrc,
            spotify_url=spotify_url,
            modifiers_detected=modifiers,
            featured_artists=featured,
            artist_inferred=artist_inferred,
            canonical_version=is_canonical,
        )
    except Exception:
        logger.exception("Failed to parse Spotify track object")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

async def resolve(query: str) -> Optional[CanonicalTrack]:
    """Resolve a user query to canonical track metadata via Spotify.

    Full pipeline:
    1. Normalize query (strip noise, parse features, detect modifiers, resolve aliases)
    2. Search Spotify with optimized query
    3. Re-rank Spotify results (prefer canonical studio versions)
    4. Parse best result into CanonicalTrack with debug metadata

    Returns the best CanonicalTrack, or None if unavailable.
    """
    # Cache check with normalized key
    cache_key = f"sp_meta:{query.lower().strip()}"
    cached = _metadata_cache.get(cache_key)
    if cached is not None:
        return cached

    # ── Step 1: Normalize query ───────────────────────────────────────────
    norm = normalize_query(query)

    logger.debug(
        "Query normalization: '%s' → spotify_query='%s', modifiers=%s, "
        "parsed_artist=%s, featured=%s, alias=%s",
        query, norm.spotify_query, norm.modifiers,
        norm.parsed_artist, norm.featured_artists, norm.alias_resolved,
    )

    # ── Step 2: Search Spotify ────────────────────────────────────────────
    try:
        from spotifyapi import search_spotify_tracks_async
        results = await search_spotify_tracks_async(norm.spotify_query, limit=8)
        logger.info(
            "[spotify_resolver] Spotify search: query='%s' → %d results",
            norm.spotify_query,
            len(results) if results else 0,
        )
    except ImportError:
        logger.warning("spotifyapi module not available — skipping metadata resolution")
        return None
    except Exception as e:
        logger.exception("Spotify search failed for '%s' - %s", norm.spotify_query, str(e))
        return None

    if not results:
        # Fallback: try original query if normalized differs
        if norm.spotify_query != query.strip():
            logger.info(
                "[spotify_resolver] No results for normalized query, trying original: '%s'",
                query.strip(),
            )
            try:
                from spotifyapi import search_spotify_tracks_async
                results = await search_spotify_tracks_async(query.strip(), limit=5)
                logger.info(
                    "[spotify_resolver] Original query search: query='%s' → %d results",
                    query.strip(),
                    len(results) if results else 0,
                )
            except Exception as e:
                logger.exception("Spotify search failed for original query '%s' - %s", query.strip(), str(e))
                pass
        if not results:
            logger.warning(
                "[spotify_resolver] No Spotify results found for query: '%s' (normalized: '%s')",
                query,
                norm.spotify_query,
            )
            return None

    # ── Step 3: Re-rank Spotify results ───────────────────────────────────
    ranked = _rerank_spotify_results(results, norm)

    # ── Step 4: Parse best result ─────────────────────────────────────────
    best: Optional[CanonicalTrack] = None
    for track_data in ranked:
        parsed = _parse_spotify_track(track_data, norm)
        if parsed:
            best = parsed
            break

    if not best:
        return None

    # Attach debug metadata
    best.debug = {
        "original_query": query,
        "spotify_query": norm.spotify_query,
        "modifiers_detected": norm.modifiers,
        "parsed_artist": norm.parsed_artist,
        "featured_artists": norm.featured_artists,
        "alias_resolved": norm.alias_resolved,
        "wants_canonical": norm.wants_canonical,
        "artist_inferred": best.artist_inferred,
        "canonical_version": best.canonical_version,
        "popularity": best.popularity,
        "rerank_applied": True,
    }

    # Cache the result
    _metadata_cache.set(cache_key, best, ttl=3600)

    logger.info(
        "Resolved '%s' → '%s' by %s (pop=%d, dur=%ds, canonical=%s, inferred=%s)",
        query, best.title, best.primary_artist, best.popularity,
        int(best.duration_sec), best.canonical_version, best.artist_inferred,
    )

    return best


async def resolve_multiple(query: str, limit: int = 3) -> List[CanonicalTrack]:
    """Resolve query to multiple Spotify candidates (for ambiguous queries)."""
    norm = normalize_query(query)

    try:
        from spotifyapi import search_spotify_tracks_async
        results = await search_spotify_tracks_async(norm.spotify_query, limit=limit + 3)
    except Exception:
        return []

    ranked = _rerank_spotify_results(results, norm)

    candidates = []
    for track_data in ranked[:limit]:
        parsed = _parse_spotify_track(track_data, norm)
        if parsed:
            candidates.append(parsed)
    return candidates
