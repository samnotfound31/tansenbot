"""
bot.providers.search_engine
Intelligent search pipeline with provider-agnostic canonical metadata.

Architecture:
    User Query
    → Canonical Metadata Resolver (iTunes PRIMARY, Spotify OPTIONAL)
    → Enhanced SoundCloud Query (title + artist)
    → Playback Ranking Engine (SoundCloud-friendly, remix-aware)
    → Confidence Tier → autoplay / quick_pick / full_menu
"""

import logging
from typing import Dict, Any, List, Optional

import wavelink

from bot.providers import soundcloud
from bot.providers.playback_ranking import ScoredTrack, rank_tracks, confidence_tier
from bot.providers.canonical_resolver import resolve as resolve_canonical, CanonicalTrack

logger = logging.getLogger("tansen.search")


def track_to_dict(
    track: wavelink.Playable,
    requester: str = "",
    canonical: Optional[CanonicalTrack] = None,
    playback_query: str = "",
) -> Dict[str, Any]:
    """Convert a wavelink.Playable to a JSON-serializable song dict for queue storage.

    Stores both SoundCloud playback metadata AND canonical metadata (if available).
    Canonical metadata is used for lyrics lookup and UI display to ensure accuracy.

    Also stores recovery metadata for SoundCloud stream failure recovery.
    """
    # Debug logging: verify canonical parameter is received
    if canonical:
        logger.info(
            "[track_to_dict] Storing canonical metadata: title='%s', artist='%s'",
            canonical.title,
            canonical.primary_artist,
        )
    else:
        logger.info(
            "[track_to_dict] NO canonical metadata - storing SoundCloud only: title='%s', author='%s'",
            track.title,
            track.author,
        )

    song_dict = {
        # Legacy/compatibility fields referring to SoundCloud metadata
        "title": track.title or "Unknown",
        "author": track.author or "",
        "uri": track.uri or "",
        "duration": int((track.length or 0) / 1000),  # ms -> seconds
        "artwork": getattr(track, "artwork", None) or getattr(track, "thumb", None),
        "requester": requester,
        "source": "SoundCloud",
        "identifier": track.identifier or "",

        # Playback specific fields (fully separated and preserved)
        "playback_title": track.title or "Unknown",
        "playback_author": track.author or "",
        "playback_duration": int((track.length or 0) / 1000),
        "playback_artwork": getattr(track, "artwork", None) or getattr(track, "thumb", None),
        "playback_uri": track.uri or "",
        "playback_provider": "soundcloud",
    }

    # Store canonical metadata if available (for lyrics lookup, fully separated)
    if canonical:
        song_dict["canonical_title"] = canonical.title
        song_dict["canonical_artist"] = canonical.primary_artist
        song_dict["canonical_primary_artist"] = canonical.primary_artist
        song_dict["canonical_artists"] = canonical.artists
        song_dict["canonical_album"] = canonical.album
        song_dict["canonical_duration_ms"] = canonical.duration_ms
        song_dict["canonical_popularity"] = canonical.popularity
        song_dict["canonical_isrc"] = canonical.isrc
        song_dict["canonical_provider"] = canonical.provider
        song_dict["canonical_provider_url"] = canonical.provider_url
        song_dict["canonical_artwork_url"] = canonical.artwork_url
        song_dict["canonical_debug"] = canonical.debug

    # Recovery metadata for SoundCloud stream failure recovery
    song_dict["playback_query"] = playback_query
    song_dict["failed_provider_ids"] = []
    song_dict["recovery_attempts"] = 0

    return song_dict


async def resolve_from_dict(song: Dict[str, Any]) -> Optional[wavelink.Playable]:
    """Re-resolve a stored song dict back to a wavelink.Playable for playback.

    Tries the URI first (fast, exact), then falls back to a search.
    """
    uri = song.get("uri", "")
    if uri:
        try:
            tracks = await wavelink.Playable.search(uri)
            if tracks:
                return tracks[0]
        except Exception:
            logger.debug("URI resolve failed for %s, falling back to search", uri)

    # Fallback: search by title + author
    title = song.get("title", "")
    author = song.get("author", "")
    query = f"{title} {author}".strip()
    if not query:
        return None

    tracks = await soundcloud.search(query)
    if tracks:
        return tracks[0]
    return None


class SearchResult:
    """Container for a search operation's outcome."""

    __slots__ = ("query", "ranked", "best", "tier", "canonical")

    def __init__(
        self,
        query: str,
        ranked: List[ScoredTrack],
        best: Optional[ScoredTrack],
        tier: str,
        canonical: Optional[CanonicalTrack] = None,
    ):
        self.query = query
        self.ranked = ranked
        self.best = best
        self.tier = tier  # "autoplay", "quick_pick", or "full_menu"
        self.canonical = canonical

    @property
    def confident(self) -> bool:
        return self.tier == "autoplay"

    @property
    def track(self) -> Optional[wavelink.Playable]:
        return self.best.track if self.best else None

    @property
    def quick_picks(self) -> List[ScoredTrack]:
        """Top results for the quick-pick tier (up to 15)."""
        return self.ranked[:15]

    @property
    def alternatives(self) -> List[ScoredTrack]:
        """All results for the full menu tier (up to 15)."""
        return self.ranked[:15]


async def find(query: str) -> SearchResult:
    """Primary search entry point — Spotify-enriched intelligent retrieval.

    Flow:
    1. Resolve canonical metadata via Spotify
    2. Search SoundCloud using the raw user query (primary candidates)
    3. Search SoundCloud using enriched query and merge secondary candidates uniquely
    4. Rank results using metadata-aware scoring
    5. Return result with confidence tier
    """

    # ── Step 1: Canonical metadata resolution (iTunes primary, Spotify optional) ───────────────────────────────
    canonical: Optional[CanonicalTrack] = None
    try:
        canonical = await resolve_canonical(query)
        if canonical:
            logger.info(
                "[find] Canonical resolution SUCCESS: provider='%s', title='%s', artist='%s'",
                canonical.provider,
                canonical.title,
                canonical.primary_artist,
            )
        else:
            logger.warning("[find] Canonical resolver returned None for query: '%s'", query)
    except Exception as e:
        logger.exception("[find] Canonical resolution FAILED for query: '%s' - %s", query, str(e))

    # ── Step 2 & 3: Search SoundCloud with raw query and optionally merge enriched query ───────────────────────────
    # Primary search: raw query
    tracks = await soundcloud.search(query)
    if not tracks:
        tracks = []

    # Optionally merge canonical-enriched search results as secondary candidates for better diversity
    if canonical and canonical.enriched_query != query:
        logger.info(
            "Merging secondary candidates via enriched query: '%s' (for canonical: %s by %s)",
            canonical.enriched_query,
            canonical.title,
            canonical.primary_artist,
        )
        enriched_tracks = await soundcloud.search(canonical.enriched_query)
        if enriched_tracks:
            # Merge preserving uniqueness of wavelink.Playable objects
            seen_uris = {t.uri for t in tracks if getattr(t, "uri", None)}
            for et in enriched_tracks:
                uri = getattr(et, "uri", None)
                if uri and uri in seen_uris:
                    continue
                tracks.append(et)

    if not tracks:
        return SearchResult(query=query, ranked=[], best=None, tier="full_menu", canonical=canonical)

    # ── Step 4: Rank with metadata-aware scoring ──────────────────────────
    ranked = rank_tracks(tracks, query, canonical=canonical)

    if not ranked:
        return SearchResult(query=query, ranked=[], best=None, tier="full_menu", canonical=canonical)

    # ── Playback Search Logging ──────────────────────────────────────────
    # Log ranked candidates with [playback-search] tag (uploader, scores, rank order) without lyrics info
    logger.info("\n[playback-search]\nquery=\"%s\"", query)
    for idx, st in enumerate(ranked, start=1):
        t = st.track
        logger.info(
            "Rank #%d: title=\"%s\", author=\"%s\", score=%.1f, breakdown=%s",
            idx,
            getattr(t, "title", "Unknown"),
            getattr(t, "author", "Unknown"),
            st.score,
            st.breakdown,
        )

    # ── Step 5: Determine confidence tier ─────────────────────────────────
    tier = confidence_tier(ranked, raw_query=query)
    best = ranked[0] if tier == "autoplay" else None

    logger.info(
        "Search result: tier=%s, top_score=%.0f, query='%s'",
        tier,
        ranked[0].score,
        query,
    )

    return SearchResult(
        query=query,
        ranked=ranked,
        best=best,
        tier=tier,
        canonical=canonical,
    )
