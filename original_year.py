from __future__ import annotations

import time
import re
from dataclasses import dataclass
from typing import Optional, List, Tuple
from urllib.parse import quote_plus

import requests


_MB_BASE = "https://musicbrainz.org/ws/2"
_DISCOGS_BASE = "https://api.discogs.com"


def _extract_year(date_str: str) -> Optional[int]:
    """
    MusicBrainz dates can be "YYYY", "YYYY-MM", or "YYYY-MM-DD".
    Discogs may also provide full dates in some contexts, but typically "year" is int.
    """
    if not date_str:
        return None
    m = re.match(r"^(\d{4})", date_str.strip())
    return int(m.group(1)) if m else None


def _http_get_json(
    url: str, *, headers: dict, params: dict | None = None, timeout: int = 20
) -> dict:
    r = requests.get(url, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _musicbrainz_first_year(
    song_title: str, artist: str, user_agent: str
) -> Optional[int]:
    """
    Strategy:
      1) Search recordings by title+artist, pick best-scoring result
      2) Fetch recording details incl. releases
      3) Earliest release date year among returned releases
    """
    headers = {"User-Agent": user_agent}

    # 1) Search best matching recording
    query = f'recording:"{song_title}" AND artist:"{artist}"'
    search_url = f"{_MB_BASE}/recording"
    search_params = {"query": query, "fmt": "json", "limit": 5}

    search = _http_get_json(search_url, headers=headers, params=search_params)
    recordings = search.get("recordings") or []
    if not recordings:
        return None

    # Pick highest score; tie-break by presence of releases
    def rec_key(rec: dict) -> Tuple[int, int]:
        return (int(rec.get("score", 0)), int(bool(rec.get("releases"))))

    best = max(recordings, key=rec_key)
    mbid = best.get("id")
    if not mbid:
        return None

    # MusicBrainz rate-limits; keep it gentle
    time.sleep(1.05)

    # 2) Fetch recording with release list (may be partial but usually good enough)
    rec_url = f"{_MB_BASE}/recording/{mbid}"
    rec_params = {"inc": "releases", "fmt": "json"}
    rec = _http_get_json(rec_url, headers=headers, params=rec_params)

    releases = rec.get("releases") or []
    years: List[int] = []
    for rel in releases:
        y = _extract_year(rel.get("date", "") or "")
        if y:
            years.append(y)

    return min(years) if years else None


def _discogs_first_year(
    song_title: str, artist: str, user_agent: str, discogs_token: Optional[str]
) -> Optional[int]:
    """
    Strategy:
      1) /database/search with track+artist to find likely releases
      2) For each result:
         - if master_id: GET /masters/{id} and use master.year
         - else: GET /releases/{id} and use release.year
      3) return minimum year
    """
    headers = {"User-Agent": user_agent}
    if discogs_token:
        headers["Authorization"] = f"Discogs token={discogs_token}"

    search_url = f"{_DISCOGS_BASE}/database/search"
    params = {
        "type": "release",
        "artist": artist,
        "track": song_title,
        "per_page": 5,
        "page": 1,
    }
    # If you don't pass token via header, Discogs might still work but is more limited.
    search = _http_get_json(search_url, headers=headers, params=params)
    results = search.get("results") or []
    if not results:
        return None

    years: List[int] = []

    for item in results:
        # Be gentle with rate limits
        time.sleep(1.0)

        master_id = item.get("master_id")
        release_id = item.get("id")

        try:
            if master_id:
                master = _http_get_json(
                    f"{_DISCOGS_BASE}/masters/{master_id}", headers=headers
                )
                y = master.get("year")
                if isinstance(y, int) and y > 0:
                    years.append(y)
            elif release_id:
                rel = _http_get_json(
                    f"{_DISCOGS_BASE}/releases/{release_id}", headers=headers
                )
                y = rel.get("year")
                if isinstance(y, int) and y > 0:
                    years.append(y)
        except requests.HTTPError:
            # Skip bad/forbidden entries rather than failing the whole lookup
            continue

    return min(years) if years else None


def first_release_year(
    song_title: str, artist: str, *, discogs_token: Optional[str] = None
) -> Optional[int]:
    """
    Return the earliest release year found across MusicBrainz + Discogs.
    If neither source yields a year, returns None.

    Parameters
    ----------
    song_title : str
    artist : str
    discogs_token : Optional[str]
        Discogs personal access token (recommended). Create one in your Discogs account settings.

    Usage
    -----
    year = first_release_year("Smells Like Teen Spirit", "Nirvana", discogs_token="YOUR_TOKEN")
    """
    # Use a descriptive UA per API etiquette
    user_agent = "FirstReleaseYearLookup/1.0 (contact: you@example.com)"

    mb_year = None
    dc_year = None

    try:
        mb_year = _musicbrainz_first_year(song_title, artist, user_agent=user_agent)
    except (requests.RequestException, ValueError, KeyError):
        mb_year = None

    try:
        dc_year = _discogs_first_year(
            song_title, artist, user_agent=user_agent, discogs_token=discogs_token
        )
    except (requests.RequestException, ValueError, KeyError):
        dc_year = None

    years = [y for y in (mb_year, dc_year) if isinstance(y, int)]
    return min(years) if years else None
