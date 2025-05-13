import aiohttp
from dotenv import load_dotenv
import os
import psycopg2
import re

# Load environment variables from .env file
load_dotenv()

# Get the database URL from the environment variables
DATABASE_URL = os.getenv("DATABASE_URL")

# Connect to the database
conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

geniustoken = os.getenv("GENIUS_API_TOKEN")

# Clean the song title for better lyric search
def clean_song_title(title: str) -> str:
    """
    Cleans the song title by removing unnecessary parts like "Official Video" or "Lyrics."
    Retains the first artist's name if present.
    """
    patterns_to_remove = [
        r"\(.*official.*\)",  # Matches "(Official Video)" or "(Official Audio)"
        r"\[.*official.*\]",  # Matches "[Official Video]" or "[Official Audio]"
        r"official video",    # Matches "Official Video"
        r"official audio",    # Matches "Official Audio"
        r"lyrics",            # Matches "Lyrics"
        r"hd",                # Matches "HD"
        r"4k",                # Matches "4K"
        r"mv",                # Matches "MV"
    ]

    for pattern in patterns_to_remove:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)

    # Remove extra spaces
    title = re.sub(r"\s+", " ", title).strip()

    return title


# Fetch lyrics from lyrics.ovh
async def get_lyrics_from_ovh(artist: str, title: str) -> str | None:
    """
    Fetches lyrics from lyrics.ovh using the artist and title.
    Returns the lyrics string, or None if not found.
    """
    url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("lyrics")
        except Exception as e:
            print(f"Error fetching lyrics from Lyrics.ovh: {e}")
    return None


# Fetch lyrics from Genius
async def get_lyrics_from_genius(query: str) -> str | None:
    """
    Searches Genius for the query and scrapes the lyrics page.
    Requires a Genius API token in the `.env` file.
    """
    token = geniustoken
    if not token:
        print("Error: Genius API token is missing.")
        return None

    headers = {"Authorization": f"Bearer {token}"}
    search_url = f"https://api.genius.com/search?q={query}"
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            # Search for the song on Genius
            async with session.get(search_url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            hits = data.get("response", {}).get("hits", [])
            if not hits:
                return None

            # Take the first result's URL
            song_url = hits[0]["result"]["url"]

            # Fetch the lyrics page HTML
            async with session.get(song_url, timeout=10) as page:
                html = await page.text()

        except Exception as e:
            print(f"Error fetching lyrics from Genius: {e}")
            return None

    # Parse the lyrics from the HTML using BeautifulSoup
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        lyrics_divs = soup.find_all("div", attrs={"data-lyrics-container": "true"})
        lyrics = "\n".join(div.get_text(separator="\n").strip() for div in lyrics_divs)
        return lyrics or None
    except Exception as e:
        print(f"Error parsing lyrics from Genius: {e}")
    return None


