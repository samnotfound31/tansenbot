"""
bot.providers.playback_ranking
SoundCloud-friendly playback ranking system.

Optimized for:
- User playback intent
- SoundCloud culture (remixes, edits, phonk, etc.)
- Remix friendliness
- Playback quality
- Popularity
- Duration sanity
- Listener satisfaction

NOT optimized for:
- Strict canonical purity
- Anti-remix penalties
- Spotify-style original-track enforcement

Separate from canonical metadata/lyrics resolution.
"""

import re
import logging
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

logger = logging.getLogger("tansen.playback_ranking")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# ── Scoring weights (must sum to 1.0) ────────────────────────────────────────
# Playback-focused: prioritize what users actually want to hear
W_TITLE = 0.30          # Title similarity (most important for user intent)
W_RELEVANCE = 0.20      # SoundCloud search relevance/order
W_POPULARITY = 0.15     # Popularity/play count (indicates quality)
W_DURATION = 0.15       # Duration sanity
W_UPLOADER = 0.10       # Uploader quality
W_CLEANLINESS = 0.05    # Upload cleanliness (penalize excessive tags)
W_ARTIST = 0.05         # Artist match (lower priority - allow remixes)

# ── Confidence tiers ─────────────────────────────────────────────────────────
CONFIDENCE_AUTOPLAY = 90      # instant autoplay (raised from 80 for safer UX)
CONFIDENCE_QUICK_PICK = 50    # show top 15 choices
CONFIDENCE_FULL_MENU = 0      # full dropdown below this

# ── Autoplay safety thresholds ──────────────────────────────────────────────
MIN_SCORE_GAP_FOR_AUTOPLAY = 20  # Top score must be this much higher than second

# ── User modifier detection (BOOST when user explicitly searches these) ─────
USER_MODIFIERS = [
    "remix",
    "slowed",
    "reverb",
    "sped up",
    "bass boosted",
    "nightcore",
    "phonk",
    "edit",
    "8d audio",
    "lofi",
    "lo-fi",
    "chipmunk",
    "daycore",
    "mashup",
    "cover",
    "karaoke",
    "instrumental",
    "live",
    "acoustic",
]

VERSION_KEYWORDS = {
    "remix": ["remix", "rmx"],
    "slowed": ["slowed", "slow", "reverb"],
    "sped up": ["sped up", "spedup", "speed up", "speedup", "nightcore"],
    "bass boosted": ["bass boosted", "bassboosted", "bass"],
    "instrumental": ["instrumental", "backing", "karaoke"],
    "live": ["live", "concert"],
    "acoustic": ["acoustic", "unplugged"],
    "cover": ["cover"],
    "phonk": ["phonk"],
    "lofi": ["lofi", "lo-fi"],
    "edit": ["edit", "radio edit", "club edit"],
}

# ── Uploader quality: suspicious patterns ────────────────────────────────────
_BAD_UPLOADER_PATTERNS = [
    re.compile(r"\brepost", re.IGNORECASE),
    re.compile(r"\bpromotion", re.IGNORECASE),
    re.compile(r"\bnetwork\b", re.IGNORECASE),
    re.compile(r"\bmusic\s*(hub|zone|world|city|daily)\b", re.IGNORECASE),
    re.compile(r"\btrap\s*(nation|city|music)\b", re.IGNORECASE),
    re.compile(r"\bbass\s*(nation|boosted|music)\b", re.IGNORECASE),
    re.compile(r"\bmeme\b", re.IGNORECASE),
    re.compile(r"\blyrics?\s*(video|channel)?\b", re.IGNORECASE),
    re.compile(r"^user-?\d+$", re.IGNORECASE),
]

# ── Noise patterns to strip before comparison ────────────────────────────────
_NOISE_PATTERNS = [
    re.compile(r"\(official.*?\)", re.IGNORECASE),
    re.compile(r"\[official.*?\]", re.IGNORECASE),
    re.compile(r"\(audio\)", re.IGNORECASE),
    re.compile(r"\[audio\]", re.IGNORECASE),
    re.compile(r"\(lyrics?\)", re.IGNORECASE),
    re.compile(r"\[lyrics?\]", re.IGNORECASE),
    re.compile(r"\[hd\]|\[4k\]|\(hd\)|\(4k\)", re.IGNORECASE),
    re.compile(r"official\s*(audio|video|music\s*video|visualizer)", re.IGNORECASE),
    re.compile(r"\blyric\s*video\b", re.IGNORECASE),
    re.compile(r"\bpremiere\b", re.IGNORECASE),
    re.compile(r"\bfull\s*song\b", re.IGNORECASE),
    re.compile(r"\bfree\s*download\b", re.IGNORECASE),
    re.compile(r"\bout\s*now\b", re.IGNORECASE),
]

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def normalize(text: str) -> str:
    """Normalize text for comparison: lowercase, strip noise/emojis."""
    if not text:
        return ""
    text = text.lower()
    # Remove noise patterns
    for pat in _NOISE_PATTERNS:
        text = pat.sub("", text)
    # Remove emojis
    text = _EMOJI_RE.sub("", text)
    # Remove extra whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _seq_similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio (0–1)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _substring_bonus(needle: str, haystack: str) -> float:
    """Return 0–1 bonus if needle is a substantial substring of haystack."""
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        # Bonus proportional to how much of haystack is the needle
        ratio = len(needle) / len(haystack)
        return min(1.0, ratio * 2.0)  # Cap at 1.0
    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# USER MODIFIER DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def detect_user_modifiers(query: str) -> List[str]:
    """Detect modifiers user explicitly searched for (e.g., 'remix', 'slowed')."""
    query_lower = query.lower()
    detected = []
    for modifier in USER_MODIFIERS:
        if modifier in query_lower:
            detected.append(modifier)
    return detected


def track_contains_modifier(track_title: str, modifier: str) -> bool:
    """Check if track title contains a specific modifier."""
    return modifier.lower() in track_title.lower()


# ══════════════════════════════════════════════════════════════════════════════
# SCORING FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _score_title(track_title_norm: str, query_norm: str) -> float:
    """Score title similarity (0–100).

    Playback-focused: prioritize exact title matches and user intent.
    """
    if not query_norm:
        return 50.0  # neutral if no query

    if not track_title_norm:
        return 0.0

    # Direct similarity
    sim = _seq_similarity(track_title_norm, query_norm)

    # Substring bonus (if query is in track title)
    bonus = _substring_bonus(query_norm, track_title_norm)

    # Take the better signal
    best = max(sim, bonus)

    return min(100.0, best * 100)


def _score_artist(
    track_title_norm: str,
    track_author_norm: str,
    ref_artists: Optional[List[str]] = None,
) -> float:
    """Score artist/uploader match (0–100).

    Playback-focused: lower priority than title, allow remixes.
    """
    if not ref_artists:
        return 50.0  # neutral if no reference

    track_author = track_author_norm or ""
    best_score = 0.0

    for artist in ref_artists:
        artist_norm = normalize(artist)
        if not artist_norm:
            continue

        # Author similarity
        author_sim = _seq_similarity(artist_norm, track_author)

        # Check if artist name appears in track title (common on SC)
        title_contains = _substring_bonus(artist_norm, track_title_norm)

        # Take the better signal
        artist_signal = max(author_sim, title_contains * 2.0)
        best_score = max(best_score, artist_signal)

    return min(100.0, best_score * 100)


def _score_duration(track_duration_ms: int, ref_duration_ms: int = 0) -> float:
    """Score duration sanity (0–100).

    Playback-focused: ensure reasonable duration, but allow remix variations.
    """
    if not track_duration_ms:
        return 50.0  # neutral if no duration

    track_sec = track_duration_ms / 1000

    # Very short tracks are usually clips/previews
    if track_sec < 30:
        return 10.0

    # Very long tracks are usually mixes/compilations
    if track_sec > 600:
        return 20.0

    # If no reference, assume reasonable duration is good
    if not ref_duration_ms:
        return 80.0

    ref_sec = ref_duration_ms / 1000

    # Calculate relative difference
    diff = abs(track_sec - ref_sec)
    ratio = diff / max(ref_sec, 1.0)

    # More generous tolerance for remixes/edits
    if ratio <= 0.10:   # within 10% — excellent match
        return 100.0
    elif ratio <= 0.30:  # within 30% — good (allows remix variations)
        return 85.0
    elif ratio <= 0.50:  # within 50% — acceptable
        return 65.0
    elif ratio <= 1.00:  # within 100% — suspicious but possible
        return 40.0
    else:                # way off — likely wrong track
        return 10.0


def _score_uploader_quality(track_author: str, ref_artists: Optional[List[str]] = None) -> float:
    """Score uploader quality (0–100).

    Playback-focused: boost artist channels, penalize repost/promo accounts.
    """
    author_lower = (track_author or "").lower()

    # Check if uploader matches a reference artist (strong boost)
    for artist in (ref_artists or []):
        artist_norm = normalize(artist)
        if artist_norm and (
            artist_norm in normalize(track_author) or
            _seq_similarity(artist_norm, normalize(track_author)) > 0.8
        ):
            return 100.0

    # Check for bad patterns
    for pat in _BAD_UPLOADER_PATTERNS:
        if pat.search(author_lower):
            return 25.0

    # Neutral — unknown uploader, neither good nor bad
    return 60.0


def _score_upload_cleanliness(track_title: str) -> float:
    """Score upload cleanliness (0–100).

    Penalizes tracks with excessive tags/noise in title.
    Tracks with too many parentheses, brackets, or tags are likely low quality.
    """
    if not track_title:
        return 50.0

    # Count excessive tags
    paren_count = track_title.count("(") + track_title.count(")")
    bracket_count = track_title.count("[") + track_title.count("]")
    total_tags = paren_count + bracket_count

    # Penalize excessive tagging
    if total_tags == 0:
        return 100.0
    elif total_tags == 1:
        return 85.0
    elif total_tags == 2:
        return 65.0
    else:
        return 30.0


def _score_popularity(track, canonical_popularity: int = 0) -> float:
    """Score popularity/play count (0–100).

    Playback-focused: use Spotify popularity as proxy, or neutral.
    """
    if canonical_popularity > 0:
        # Normalize Spotify popularity (0-100)
        return float(canonical_popularity)

    # If no canonical popularity, assume neutral
    return 50.0


def _apply_modifier_boost(
    track_title_norm: str,
    user_modifiers: List[str],
) -> float:
    """Return a boost multiplier (1.0–1.5) for user-requested modifiers.

    If user searched for "remix", boost tracks containing "remix".
    """
    if not user_modifiers:
        return 1.0

    boost_count = 0
    for modifier in user_modifiers:
        if modifier in track_title_norm:
            boost_count += 1

    if boost_count == 0:
        return 1.0
    elif boost_count == 1:
        return 1.3   # single modifier match — moderate boost
    else:
        return 1.5   # multiple modifiers — strong boost


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCORING API
# ══════════════════════════════════════════════════════════════════════════════

class ScoredTrack:
    """Wraps a wavelink.Playable with a normalized 0–100 score."""

    __slots__ = ("track", "score", "breakdown")

    def __init__(self, track, score: float = 0.0, breakdown: Optional[dict] = None):
        self.track = track
        self.score = score
        self.breakdown = breakdown or {}

    def __repr__(self) -> str:
        return f"<ScoredTrack score={self.score:.0f} title={getattr(self.track, 'title', '?')}>"


def score_track(
    track,
    raw_query: str,
    canonical: Optional["CanonicalTrack"] = None,
    position: int = 0,
) -> ScoredTrack:
    """Score a single wavelink.Playable for playback ranking.

    Playback-focused: prioritize user intent, allow remixes, respect SoundCloud culture.

    Parameters
    ----------
    track : wavelink.Playable
    raw_query : str – original user input
    canonical : CanonicalTrack | None – canonical metadata (for reference only)
    position : int – original index in search results
    """
    breakdown: dict = {}

    track_title_norm = normalize(getattr(track, "title", "") or "")
    track_author_norm = normalize(getattr(track, "author", "") or "")
    track_author_raw = getattr(track, "author", "") or ""
    track_duration_ms = getattr(track, "length", 0) or 0
    track_title_raw = getattr(track, "title", "") or ""

    # Detect user modifiers (e.g., "remix", "slowed")
    user_modifiers = detect_user_modifiers(raw_query)
    if user_modifiers:
        logger.info(
            "[playback_ranking] User modifiers detected: %s",
            ", ".join(user_modifiers),
        )
        breakdown["user_modifiers"] = user_modifiers

    # Always use the raw query as title reference for title similarity matching
    ref_title = normalize(raw_query)

    # Determine reference for other fields from canonical if available
    if canonical:
        ref_artists = canonical.artists
        ref_duration_ms = canonical.duration_ms
        ref_popularity = canonical.popularity
    else:
        ref_artists = []
        ref_duration_ms = 0
        ref_popularity = 0

    # ── Score each component (0–100) ──────────────────────────────────────

    title_score = _score_title(track_title_norm, ref_title)
    breakdown["title"] = round(title_score, 1)

    # SoundCloud search relevance/order score
    relevance_score = max(0.0, 100.0 - (position * 10.0))
    breakdown["relevance"] = round(relevance_score, 1)

    artist_score = _score_artist(track_title_norm, track_author_norm, ref_artists)
    breakdown["artist"] = round(artist_score, 1)

    duration_score = _score_duration(track_duration_ms, ref_duration_ms)
    breakdown["duration"] = round(duration_score, 1)

    uploader_score = _score_uploader_quality(track_author_raw, ref_artists)
    breakdown["uploader"] = round(uploader_score, 1)

    cleanliness_score = _score_upload_cleanliness(track_title_raw)
    breakdown["cleanliness"] = round(cleanliness_score, 1)

    popularity_score = _score_popularity(track, ref_popularity)
    breakdown["popularity"] = round(popularity_score, 1)

    # ── Weighted combination ──────────────────────────────────────────────

    raw_score = (
        title_score * W_TITLE +
        relevance_score * W_RELEVANCE +
        popularity_score * W_POPULARITY +
        duration_score * W_DURATION +
        uploader_score * W_UPLOADER +
        cleanliness_score * W_CLEANLINESS +
        artist_score * W_ARTIST
    )
    breakdown["raw"] = round(raw_score, 1)

    # ── Apply modifier boost (if user explicitly searched for remix, etc.) ─────

    boost_mult = _apply_modifier_boost(track_title_norm, user_modifiers)
    breakdown["boost_mult"] = boost_mult

    final_score = raw_score * boost_mult
    breakdown["final"] = round(final_score, 1)

    return ScoredTrack(track=track, score=round(final_score, 1), breakdown=breakdown)


def detect_version(title: str) -> str:
    """Detect version/modifier of a track from its title."""
    title_lower = title.lower()
    for version_name, keywords in VERSION_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                return version_name
    return "original"


def get_cluster_id(title: str) -> str:
    """Get a normalized cluster ID representing the core song name without version tags."""
    t = normalize(title)
    for keywords in VERSION_KEYWORDS.values():
        for kw in keywords:
            t = re.sub(rf"\b{kw}\b", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def filter_diversity(scored: List[ScoredTrack], max_results: int = 15) -> List[ScoredTrack]:
    """Filter results to ensure diversity (avoid near-duplicates).

    Removes:
    - Near-duplicate titles (same normalized title)
    - Same uploader spam (multiple from same uploader)
    - Excessive similar variants

    Prefers:
    - Higher scores
    - Different uploaders
    - Different versions (remix, instrumental, etc.)
    """
    if not scored:
        return []

    filtered = []
    seen_uploaders = {}
    seen_version_by_cluster = {}

    for st in scored:
        if len(filtered) >= max_results:
            break

        track = st.track
        title = getattr(track, "title", "") or ""
        uploader = (getattr(track, "author", "") or "").lower().strip()
        version = detect_version(title)
        cluster_id = get_cluster_id(title)

        # Limit same uploader to max 2
        uploader_count = seen_uploaders.get(uploader, 0)
        if uploader_count >= 2:
            continue

        # Limit near-duplicate versions of the same cluster
        if cluster_id not in seen_version_by_cluster:
            seen_version_by_cluster[cluster_id] = set()

        versions_in_cluster = seen_version_by_cluster[cluster_id]
        if version in versions_in_cluster:
            continue

        # Add to filtered
        filtered.append(st)
        seen_uploaders[uploader] = uploader_count + 1
        seen_version_by_cluster[cluster_id].add(version)

    # Fallback refill if filter was too aggressive
    if len(filtered) < min(5, len(scored)):
        for st in scored:
            if len(filtered) >= max_results:
                break
            if st in filtered:
                continue

            track = st.track
            title = getattr(track, "title", "") or ""
            uploader = (getattr(track, "author", "") or "").lower().strip()

            uploader_count = seen_uploaders.get(uploader, 0)
            if uploader_count >= 2:
                continue

            exact_dup = any(normalize(getattr(f.track, "title", "")) == normalize(title) for f in filtered)
            if exact_dup:
                continue

            filtered.append(st)
            seen_uploaders[uploader] = uploader_count + 1

    logger.info(
        "[playback_ranking] Diversity filter: %d → %d results",
        len(scored),
        len(filtered),
    )

    return filtered


def rank_tracks(
    tracks,
    raw_query: str,
    *,
    canonical: Optional["CanonicalTrack"] = None,
    max_results: int = 15,
) -> List[ScoredTrack]:
    """Score and rank tracks for playback.

    Playback-focused: prioritize user intent, allow remixes, respect SoundCloud culture.
    Returns sorted by descending score, capped at *max_results*.
    """
    if not tracks:
        return []

    logger.info(
        "[playback_ranking] Ranking %d tracks for query: '%s'",
        len(tracks),
        raw_query,
    )

    scored = [
        score_track(t, raw_query, canonical=canonical, position=idx)
        for idx, t in enumerate(tracks)
    ]
    scored.sort(key=lambda st: st.score, reverse=True)

    logger.info(
        "[playback_ranking] Top score: %.1f, Bottom score: %.1f",
        scored[0].score if scored else 0,
        scored[-1].score if scored else 0,
    )

    # Apply diversity filter
    scored = filter_diversity(scored, max_results)

    return scored


def detect_ambiguity(scored: List[ScoredTrack]) -> bool:
    """Detect if results are ambiguous (should show chooser instead of autoplay).

    Ambiguity indicators:
    - Multiple top results have similar scores (score gap < 20)
    - Top track has other versions in top results with close scores (< 25 points difference)
    """
    if len(scored) < 2:
        return False

    top_score = scored[0].score
    second_score = scored[1].score
    score_gap = top_score - second_score

    # 1. Ambiguous if score gap is small
    if score_gap < MIN_SCORE_GAP_FOR_AUTOPLAY:
        logger.info(
            "[playback_ranking] Ambiguity detected: top_score=%.1f, second_score=%.1f, gap=%.1f",
            top_score,
            second_score,
            score_gap,
        )
        return True

    # 2. Check for version ambiguity / duplicate cluster issue
    top_track = scored[0].track
    top_title = getattr(top_track, "title", "") or ""
    top_cluster = get_cluster_id(top_title)

    other_versions_count = 0
    for st in scored[1:5]:
        st_title = getattr(st.track, "title", "") or ""
        st_cluster = get_cluster_id(st_title)
        
        # If it belongs to the same cluster and has a score close to the top score (within 25 points)
        if st_cluster == top_cluster and (top_score - st.score) <= 25.0:
            other_versions_count += 1

    if other_versions_count >= 1:
        logger.info(
            "[playback_ranking] Version ambiguity: top track has %d other version(s) in top 5 with close scores",
            other_versions_count,
        )
        return True

    return False


def confidence_tier(scored: List[ScoredTrack], raw_query: str = "") -> str:
    """Return 'autoplay', 'quick_pick', or 'full_menu' based on score analysis.

    Autoplay conditions (MUST satisfy ALL):
    - Top score >= 90
    - Score gap between top and second >= 20
    - No ambiguity detected
    - No user modifiers in query (remix, slowed, etc.)
    """
    if not scored:
        return "full_menu"

    top_score = scored[0].score

    # Detect user modifiers in query
    user_modifiers = detect_user_modifiers(raw_query)

    # Detect ambiguity
    is_ambiguous = detect_ambiguity(scored)

    # Autoplay decision logic
    should_autoplay = (
        top_score >= CONFIDENCE_AUTOPLAY and
        not is_ambiguous and
        not user_modifiers
    )

    if should_autoplay:
        logger.info(
            "[playback_ranking] Autoplay APPROVED: score=%.1f, gap_ok=True, no_modifiers=True",
            top_score,
        )
        return "autoplay"
    else:
        reasons = []
        if top_score < CONFIDENCE_AUTOPLAY:
            reasons.append(f"score_too_low({top_score:.1f})")
        if is_ambiguous:
            reasons.append("ambiguous")
        if user_modifiers:
            reasons.append(f"modifiers({','.join(user_modifiers)})")

        logger.info(
            "[playback_ranking] Autoplay BLOCKED: score=%.1f, reasons=%s",
            top_score,
            ", ".join(reasons),
        )
        return "quick_pick" if top_score >= CONFIDENCE_QUICK_PICK else "full_menu"
