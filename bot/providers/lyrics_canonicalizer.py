"""
bot.providers.lyrics_canonicalizer
Independent canonical metadata resolver for lyrics lookup.

Optimized for:
- Original artist inference
- Canonical song identity
- Lyrics accuracy
- Metadata normalization

Separate from playback ranking.
"""

import re
import logging
from typing import Optional, List

logger = logging.getLogger("tansen.lyrics_canonicalizer")

# ══════════════════════════════════════════════════════════════════════════════
# METADATA CLEANING
# ══════════════════════════════════════════════════════════════════════════════

def clean_title(title: str) -> str:
    """Clean track title for canonical inference.

    Removes:
    - Remix/edits modifiers
    - Noise patterns
    - Parenthetical content
    - Bracketed content
    """
    if not title:
        return ""

    title = title.strip()

    # Remove remix/edit modifiers (for canonical inference)
    remix_patterns = [
        r"\(.*?remix.*?\)",
        r"\[.*?remix.*?\]",
        r"\(.*?edit.*?\)",
        r"\[.*?edit.*?\]",
        r"\(.*?slowed.*?\)",
        r"\[.*?slowed.*?\]",
        r"\(.*?reverb.*?\)",
        r"\[.*?reverb.*?\]",
        r"\(.*?sped.*?up.*?\)",
        r"\[.*?sped.*?up.*?\]",
        r"\(.*?nightcore.*?\)",
        r"\[.*?nightcore.*?\]",
        r"\(.*?phonk.*?\)",
        r"\[.*?phonk.*?\]",
        r"\(.*?cover.*?\)",
        r"\[.*?cover.*?\]",
        r"\(.*?live.*?\)",
        r"\[.*?live.*?\]",
        r"\(.*?acoustic.*?\)",
        r"\[.*?acoustic.*?\]",
    ]

    for pattern in remix_patterns:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)

    # Remove official/noise patterns
    noise_patterns = [
        r"\(official.*?\)",
        r"\[official.*?\]",
        r"\(audio\)",
        r"\[audio\]",
        r"\(lyrics?\)",
        r"\[lyrics?\]",
        r"\[hd\]|\[4k\]|\(hd\)|\(4k\)",
        r"official\s*(audio|video|music\s*video|visualizer)",
        r"\blyric\s*video\b",
        r"\bpremiere\b",
        r"\bfull\s*song\b",
        r"\bfree\s*download\b",
    ]

    for pattern in noise_patterns:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)

    # Clean up extra whitespace
    title = re.sub(r"\s+", " ", title).strip()

    return title


def clean_artist(artist: str) -> str:
    """Clean artist name for canonical inference."""
    if not artist:
        return ""

    artist = artist.strip()

    # Remove common noise
    noise_patterns = [
        r"\(official\)",
        r"\[official\]",
        r"\(artist\)",
        r"\[artist\]",
    ]

    for pattern in noise_patterns:
        artist = re.sub(pattern, "", artist, flags=re.IGNORECASE)

    # Clean up extra whitespace
    artist = re.sub(r"\s+", " ", artist).strip()

    return artist


# ══════════════════════════════════════════════════════════════════════════════
# CANONICAL INFERENCE
# ══════════════════════════════════════════════════════════════════════════════

async def infer_canonical_metadata(
    title: str,
    artist: Optional[str] = None,
) -> dict:
    """Infer canonical metadata from playback metadata.

    Uses:
    1. iTunes Search API (PRIMARY)
    2. Heuristics (fallback)

    Returns dict with:
    - canonical_title
    - canonical_artist
    - canonical_album
    - provider (itunes/none)
    """
    cleaned_title = clean_title(title)
    cleaned_artist = clean_artist(artist) if artist else None

    logger.info(
        "[lyrics_canonicalizer] Inferring canonical: title='%s', artist='%s'",
        cleaned_title,
        cleaned_artist,
    )

    # Try iTunes Search API
    try:
        from bot.providers.canonical_resolver import ITunesProvider

        itunes = ITunesProvider()
        query = f"{cleaned_title} {cleaned_artist}" if cleaned_artist else cleaned_title

        results = await itunes.search(query, limit=3)

        if results:
            best = results[0]
            logger.info(
                "[lyrics_canonicalizer] iTunes SUCCESS: title='%s', artist='%s'",
                best.title,
                best.primary_artist,
            )
            return {
                "canonical_title": best.title,
                "canonical_artist": best.primary_artist,
                "canonical_album": best.album,
                "provider": "itunes",
            }
        else:
            logger.info("[lyrics_canonicalizer] iTunes no results, using heuristics")

    except Exception as e:
        logger.exception("[lyrics_canonicalizer] iTunes search failed: %s", str(e))

    # Fallback: use cleaned metadata as canonical
    logger.info("[lyrics_canonicalizer] Using heuristics: title='%s', artist='%s'", cleaned_title, cleaned_artist or "Unknown")

    return {
        "canonical_title": cleaned_title,
        "canonical_artist": cleaned_artist or "Unknown",
        "canonical_album": None,
        "provider": "heuristics",
    }


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def get_canonical_for_lyrics(song: dict) -> dict:
    """Get canonical metadata for lyrics lookup from song dict.

    This is the main entry point for the lyrics system.
    """
    title = song.get("title") or ""
    artist = song.get("author") or ""

    # If canonical metadata is already stored, use it
    if song.get("canonical_title"):
        logger.info(
            "[lyrics_canonicalizer] Using stored canonical: title='%s', artist='%s'",
            song.get("canonical_title"),
            song.get("canonical_primary_artist"),
        )
        return {
            "canonical_title": song.get("canonical_title"),
            "canonical_artist": song.get("canonical_primary_artist"),
            "canonical_album": song.get("canonical_album"),
            "provider": song.get("canonical_provider", "stored"),
        }

    # Otherwise infer from playback metadata
    return await infer_canonical_metadata(title, artist)
