"""
musicbrainz_release_year.py
────────────────────────────
Returns the first (oldest) release year for a song or album via the
MusicBrainz API, filtering out non-canonical variants.
"""

import re
import time
import requests
from requests.auth import HTTPDigestAuth

# ── Constants ────────────────────────────────────────────────────────────────

_USER_AGENT = "FirstReleaseYearLookup/2.0 (contact: ecog@outlook.de)"
_MB_ROOT    = "https://musicbrainz.org/ws/2/"
_AUTH       = HTTPDigestAuth("EcoG", "3rfweqf345)^")

_BAD_VERSION_RE = re.compile(
    r"""
    \b(
        live|remaster(?:ed)?|demo|karaoke|tribute|cover|instrumental|
        remix|mix|edit|radio\s*edit|extended|mono|stereo|acoustic|
        session|bbc|peel|alternate|outtake|version|re-record(?:ed|ing)?|
        anniversary|deluxe|bonus|reissue|speed\s*up|slowed|nightcore
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[\{].*?[\)\]\}]\s*"
)  # remove (...) / [...] / {...} parts

_BAD_SECONDARY_TYPES = {
    "compilation",
    "live",
    "remix",
    "dj-mix",
    "demo",
    "bootleg",
    "promotional",
    "promo",
    "interview",
    "audiobook",
    "audio drama",
    "spokenword",
    "field recording",
    "unofficial",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_title(title: str) -> str:
    """Strip bracketed noise tokens, then collapse whitespace."""
    return _TITLE_NOISE_RE.sub(" ", title).strip()


def _is_bad_version(title: str) -> bool:
    """Return True if the title contains a non-canonical keyword."""
    return bool(_BAD_VERSION_RE.search(title))


def _titles_match(query_title: str, candidate_title: str) -> bool:
    """
    Case-insensitive comparison after stripping noise from both sides.
    """
    return (
        _clean_title(query_title).casefold()
        == _clean_title(candidate_title).casefold()
    )


def _mb_get(endpoint: str, params: dict) -> dict:
    """Perform a GET request against the MusicBrainz API."""
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    params["fmt"] = "json"
    response = requests.get(
        _MB_ROOT + endpoint,
        params=params,
        headers=headers,
        auth=_AUTH,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _parse_year(date_str: str | None) -> int | None:
    """Extract the 4-digit year from a MusicBrainz date string."""
    if not date_str:
        return None
    m = re.match(r"(\d{4})", date_str)
    return int(m.group(1)) if m else None


# ── Core logic ───────────────────────────────────────────────────────────────

def _first_year_for_recording(recording_mbid: str) -> int | None:
    """
    Given a recording MBID, fetch all its releases and return the
    earliest canonical release year.
    """
    data = _mb_get(
        f"recording/{recording_mbid}",
        {"inc": "releases"},
    )
    years = []
    for release in data.get("releases", []):
        release_title = release.get("title", "")
        # Skip non-canonical release variants by title keywords
        if _is_bad_version(release_title):
            continue
        # Check secondary types via the release-group if present
        rg = release.get("release-group", {})
        secondary_types = {
            t.casefold() for t in rg.get("secondary-types", [])
        }
        if secondary_types & _BAD_SECONDARY_TYPES:
            continue
        year = _parse_year(release.get("date"))
        if year:
            years.append(year)
    return min(years) if years else None


def _first_year_single(title: str, artist: str) -> int | None:
    """
    Search for a recording (song) and return its earliest canonical
    release year.
    """
    query = f'recording:"{title}" AND artist:"{artist}"'
    data  = _mb_get("recording", {"query": query, "limit": 25})

    best_year: int | None = None

    for rec in data.get("recordings", []):
        rec_title = rec.get("title", "")

        # Must match the requested title (after noise removal)
        if not _titles_match(title, rec_title):
            continue

        # Skip obviously non-canonical recordings by title
        if _is_bad_version(rec_title):
            continue

        # Verify the artist name
        artist_credits = rec.get("artist-credit", [])
        artist_names   = [
            ac.get("artist", {}).get("name", "").casefold()
            for ac in artist_credits
            if isinstance(ac, dict)
        ]
        if not any(artist.casefold() in name or name in artist.casefold()
                   for name in artist_names):
            continue

        mbid = rec.get("id")
        if not mbid:
            continue

        year = _first_year_for_recording(mbid)
        if year and (best_year is None or year < best_year):
            best_year = year

    return best_year


def _first_year_album(title: str, artist: str) -> int | None:
    """
    Search for a release-group (album) and return its earliest
    canonical first-release year.
    """
    query = f'release-group:"{title}" AND artist:"{artist}"'
    data  = _mb_get("release-group", {"query": query, "limit": 25})

    best_year: int | None = None

    for rg in data.get("release-groups", []):
        rg_title = rg.get("title", "")

        # Must match the requested title
        if not _titles_match(title, rg_title):
            continue

        # Primary type must be Album (or unset); never a bad primary type
        primary_type = rg.get("primary-type", "Album")
        if primary_type and primary_type.casefold() not in ("album", "single", "ep", ""):
            continue

        # Reject bad secondary types
        secondary_types = {
            t.casefold() for t in rg.get("secondary-types", [])
        }
        if secondary_types & _BAD_SECONDARY_TYPES:
            continue

        # Skip non-canonical variants by title keywords
        if _is_bad_version(rg_title):
            continue

        year = _parse_year(rg.get("first-release-date"))
        if year and (best_year is None or year < best_year):
            best_year = year

    return best_year


# ── Public API ───────────────────────────────────────────────────────────────

def get_first_release_year(title: str, artist: str, title_type: str) -> int | None:
    """
    Return the first (oldest) canonical release year for a song or album.

    Parameters
    ----------
    title       : Song title (if title_type="single") or album title
                  (if title_type="album").
    artist      : Artist / band name.
    title_type  : "single" → look up a recording;
                  "album"  → look up a release-group.

    Returns
    -------
    int | None  : Four-digit year, or None if nothing canonical was found.

    Raises
    ------
    ValueError  : If title_type is not "single" or "album".
    """
    if title_type not in ("single", "album"):
        raise ValueError(
            f'title_type must be "single" or "album", got {title_type!r}'
        )

    if title_type == "single":
        return _first_year_single(title, artist)
    else:
        return _first_year_album(title, artist)


# ── Quick smoke-test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("Bohemian Rhapsody", "Queen",   "single"),
        ("A Night at the Opera", "Queen", "album"),
        ("Smells Like Teen Spirit", "Nirvana", "single"),
        ("Nevermind", "Nirvana", "album"),
    ]
    for t_title, t_artist, t_type in tests:
        year = get_first_release_year(t_title, t_artist, t_type)
        print(f"{t_type:6} | {t_artist} – {t_title!r:35} → {year}")
        # Be polite to MB rate limiting
        print("sleeping....")
        time.sleep(1.05)
        print("-" * 80)
