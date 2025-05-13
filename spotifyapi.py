import requests
from base64 import b64encode
import os
import dotenv

spotclid = os.getenv("SPOTIFY_CLIENT_ID")
spotclsec = os.getenv("SPOTIFY_CLIENT_SECRET")

def get_spotify_token():
    client_id = spotclid
    client_secret = spotclsec
    auth = f"{client_id}:{client_secret}"

    headers = {
        "Authorization": f"Basic {b64encode(auth.encode()).decode()}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    data = {"grant_type": "client_credentials"}

    try:
        response = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data)
        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in")
            print(f"✅ Token received. Expires in {expires_in} seconds.")
            return access_token
        else:
            print(f"❌ Failed to get token: {response.status_code} — {response.text}")
            return None
    except Exception as e:
        print("❌ Exception while getting Spotify token:", e)
        return None

def search_spotify_tracks(query):
    token = get_spotify_token()
    if not token:
        return []

    headers = {
        "Authorization": f"Bearer {token}"
    }
    params = {
        "q": query,
        "type": "track",
        "limit": 5
    }
    response = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params)
    if response.status_code != 200:
        print("Spotify search failed:", response.text)
        return []

    return response.json().get("tracks", {}).get("items", [])
