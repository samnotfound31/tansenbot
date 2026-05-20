"""
bot.providers.canonical_resolver
Provider-agnostic canonical metadata resolver.

Architecture:
- iTunes Search API (PRIMARY)
- Spotify (OPTIONAL enhancement)
- Extensible for future providers

Provides canonical metadata for:
- title, artist, album, artwork
- duration, popularity
- canonical URLs

Spotify is optional - the bot must fully function without it.
"""

import re
import logging
import asyncio
import requests
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

from bot.utils.cache import TTLCache

logger = logging.getLogger("tansen.canonical_resolver")

# ══════════════════════════════════════════════════════════════════════════════
# CACHING
# ══════════════════════════════════════════════════════════════════════════════

_metadata_cache = TTLCache(default_ttl=3600)  # 1 hour cache


# ══════════════════════════════════════════════════════════════════════════════
# CANONICAL TRACK DATA STRUCTURE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CanonicalTrack:
    """Canonical track metadata from any provider (iTunes, Spotify, etc.)."""

    # Core identity
    title: str
    artists: List[str]
    primary_artist: str
    album: Optional[str] = None

    # Timing
    duration_ms: int = 0
    duration_sec: int = 0

    # Provider metadata
    popularity: int = 0
    provider: str = "unknown"  # "itunes", "spotify", etc.

    # URLs
    provider_url: Optional[str] = None
    artwork_url: Optional[str] = None

    # Provider-specific IDs
    isrc: Optional[str] = None
    provider_id: Optional[str] = None

    # Enriched query for SoundCloud search
    enriched_query: str = field(init=False)

    # Debug metadata
    debug: Dict[str, Any] = field(default_factory=dict)

    # Flags
    artist_inferred: bool = False
    canonical_version: bool = True

    def __post_init__(self):
        """Build enriched query for SoundCloud search."""
        # Use primary artist + title for best SoundCloud matching
        self.enriched_query = f"{self.primary_artist} {self.title}".strip()


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDER ABSTRACTION
# ══════════════════════════════════════════════════════════════════════════════

class CanonicalProvider:
    """Abstract base class for canonical metadata providers."""

    provider_name: str = "base"

    async def search(self, query: str, limit: int = 5) -> List[CanonicalTrack]:
        """Search for tracks matching query."""
        raise NotImplementedError

    async def resolve(self, query: str) -> Optional[CanonicalTrack]:
        """Resolve query to single best track."""
        results = await self.search(query, limit=5)
        return results[0] if results else None


# ══════════════════════════════════════════════════════════════════════════════
# ITUNES PROVIDER (PRIMARY)
# ══════════════════════════════════════════════════════════════════════════════

class ITunesProvider(CanonicalProvider):
    """iTunes Search API provider for canonical metadata.

    Free, no API key required, no Premium needed.
    """

    provider_name = "itunes"
    API_URL = "https://itunes.apple.com/search"

    async def search(self, query: str, limit: int = 5) -> List[CanonicalTrack]:
        """Search iTunes for tracks matching query."""
        # Cache check
        cache_key = f"itunes:{query.lower().strip()}:{limit}"
        cached = _metadata_cache.get(cache_key)
        if cached is not None:
            return cached

        # Normalize query for iTunes
        normalized_query = self._normalize_query(query)

        # Search iTunes
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                self._search_sync,
                normalized_query,
                limit
            )
        except Exception as e:
            logger.exception("[itunes] Search failed for query: '%s' - %s", query, str(e))
            return []

        # Cache results
        _metadata_cache.set(cache_key, results, ttl=3600)

        return results

    def _search_sync(self, query: str, limit: int) -> List[CanonicalTrack]:
        """Synchronous iTunes search (runs in executor)."""
        params = {
            "term": query,
            "entity": "song",
            "limit": min(limit, 50),  # iTunes max is 50
        }

        try:
            response = requests.get(self.API_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            tracks = []
            for item in data.get("results", []):
                track = self._parse_itunes_track(item)
                if track:
                    tracks.append(track)

            logger.info(
                "[itunes] Search: query='%s' → %d results",
                query,
                len(tracks),
            )

            return tracks

        except Exception as e:
            logger.exception("[itunes] API call failed: %s", str(e))
            return []

    def _normalize_query(self, query: str) -> str:
        """Normalize query for iTunes search."""
        # Remove common noise
        query = re.sub(r'\b(live|acoustic|remix|slowed|reverb|cover|karaoke)\b', '', query, flags=re.IGNORECASE)
        # Clean up extra spaces
        query = re.sub(r'\s+', ' ', query).strip()
        return query

    def _parse_itunes_track(self, item: Dict[str, Any]) -> Optional[CanonicalTrack]:
        """Parse iTunes API response into CanonicalTrack."""
        try:
            # Extract metadata
            title = item.get("trackName", "")
            artist = item.get("artistName", "")
            album = item.get("collectionName", "")
            artwork = item.get("artworkUrl100", "").replace("100x100", "600x600")  # Get higher res
            duration_ms = item.get("trackTimeMillis", 0)
            duration_sec = duration_ms // 1000

            # Parse artists (iTunes only gives single artist name)
            artists = [artist] if artist else []

            # URLs
            track_url = item.get("trackViewUrl")
            collection_url = item.get("collectionViewUrl")

            # Build canonical track
            return CanonicalTrack(
                title=title,
                artists=artists,
                primary_artist=artist,
                album=album,
                duration_ms=duration_ms,
                duration_sec=duration_sec,
                popularity=0,  # iTunes doesn't provide popularity
                provider="itunes",
                provider_url=track_url or collection_url,
                artwork_url=artwork,
                provider_id=str(item.get("trackId", "")),
                debug={
                    "itunes_id": item.get("trackId"),
                    "itunes_collection_id": item.get("collectionId"),
                },
            )

        except Exception as e:
            logger.exception("[itunes] Failed to parse track: %s", str(e))
            return None


# ══════════════════════════════════════════════════════════════════════════════
# SPOTIFY PROVIDER (OPTIONAL)
# ══════════════════════════════════════════════════════════════════════════════

class OptionalSpotifyProvider(CanonicalProvider):
    """Optional Spotify provider with graceful degradation.

    Only used if credentials are available and API is working.
    Falls back to iTunes if unavailable.
    """

    provider_name = "spotify"

    async def search(self, query: str, limit: int = 5) -> List[CanonicalTrack]:
        """Search Spotify for tracks matching query (optional)."""
        try:
            # Try to import spotifyapi
            from spotifyapi import search_spotify_tracks_async
        except ImportError:
            logger.debug("[spotify] spotifyapi module not available")
            return []

        # Check if credentials are configured
        import os
        if not os.getenv("SPOTIFY_CLIENT_ID") or not os.getenv("SPOTIFY_CLIENT_SECRET"):
            logger.debug("[spotify] Credentials not configured")
            return []

        # Try Spotify search
        try:
            results = await search_spotify_tracks_async(query, limit=limit)
            tracks = [self._parse_spotify_track(r) for r in results if r]
            logger.info(
                "[spotify] Search: query='%s' → %d results",
                query,
                len(tracks),
            )
            return tracks
        except Exception as e:
            logger.debug("[spotify] Search failed: %s", str(e))
            return []

    def _parse_spotify_track(self, item: Dict[str, Any]) -> Optional[CanonicalTrack]:
        """Parse Spotify API response into CanonicalTrack."""
        try:
            # Extract metadata
            title = item.get("name", "")
            album = item.get("album", {}).get("name", "")
            duration_ms = item.get("duration_ms", 0)
            duration_sec = duration_ms // 1000
            popularity = item.get("popularity", 0)

            # Parse artists
            artists = [a.get("name", "") for a in item.get("artists", [])]
            primary_artist = artists[0] if artists else ""

            # URLs
            spotify_url = item.get("external_urls", {}).get("spotify")
            album_url = item.get("album", {}).get("external_urls", {}).get("spotify")

            # Artwork (best available)
            images = item.get("album", {}).get("images", [])
            artwork_url = images[0].get("url") if images else None

            return CanonicalTrack(
                title=title,
                artists=artists,
                primary_artist=primary_artist,
                album=album,
                duration_ms=duration_ms,
                duration_sec=duration_sec,
                popularity=popularity,
                provider="spotify",
                provider_url=spotify_url or album_url,
                artwork_url=artwork_url,
                provider_id=item.get("id"),
                isrc=item.get("external_ids", {}).get("isrc"),
                debug={
                    "spotify_id": item.get("id"),
                    "spotify_uri": item.get("uri"),
                    "provider": "spotify",
                },
            )

        except Exception as e:
            logger.exception("[spotify] Failed to parse track: %s", str(e))
            return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RESOLVER
# ══════════════════════════════════════════════════════════════════════════════

class CanonicalResolver:
    """Provider-agnostic canonical metadata resolver.

    Tries providers in order:
    1. iTunes (PRIMARY - always available)
    2. Spotify (OPTIONAL - if credentials available)

    Always returns a result or None (never fails completely).
    """

    def __init__(self):
        self.itunes = ITunesProvider()
        self.spotify = OptionalSpotifyProvider()

    async def resolve(self, query: str) -> Optional[CanonicalTrack]:
        """Resolve query to canonical metadata using best available provider."""
        # Try iTunes first (always available)
        logger.info("[resolver] Trying iTunes for query: '%s'", query)
        itunes_result = await self.itunes.resolve(query)

        if itunes_result:
            logger.info(
                "[resolver] iTunes SUCCESS: title='%s', artist='%s'",
                itunes_result.title,
                itunes_result.primary_artist,
            )
            return itunes_result

        # Fallback to Spotify (optional)
        logger.info("[resolver] iTunes failed, trying Spotify (optional)")
        spotify_result = await self.spotify.resolve(query)

        if spotify_result:
            logger.info(
                "[resolver] Spotify SUCCESS: title='%s', artist='%s'",
                spotify_result.title,
                spotify_result.primary_artist,
            )
            return spotify_result

        # No results from any provider
        logger.warning("[resolver] All providers failed for query: '%s'", query)
        return None

    async def search(self, query: str, limit: int = 5) -> List[CanonicalTrack]:
        """Search for multiple canonical track candidates."""
        # Try iTunes first
        itunes_results = await self.itunes.search(query, limit=limit)

        if itunes_results:
            logger.info(
                "[resolver] iTunes search: %d results",
                len(itunes_results),
            )
            return itunes_results

        # Fallback to Spotify
        spotify_results = await self.spotify.search(query, limit=limit)

        if spotify_results:
            logger.info(
                "[resolver] Spotify search: %d results",
                len(spotify_results),
            )
            return spotify_results

        return []


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

_resolver_instance: Optional[CanonicalResolver] = None


def get_resolver() -> CanonicalResolver:
    """Get singleton resolver instance."""
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = CanonicalResolver()
    return _resolver_instance


async def resolve(query: str) -> Optional[CanonicalTrack]:
    """Resolve query to canonical metadata (convenience function)."""
    resolver = get_resolver()
    return await resolver.resolve(query)


async def resolve_multiple(query: str, limit: int = 3) -> List[CanonicalTrack]:
    """Resolve query to multiple canonical candidates (convenience function)."""
    resolver = get_resolver()
    return await resolver.search(query, limit=limit)
