"""
musicbrainz_release_year.py
────────────────────────────
Returns the first (oldest) release year for a song or album via the
MusicBrainz API, filtering out non-canonical variants.

Resilience: _mb_get retries automatically on transient HTTP errors
(503 / 429 / 502 / 504) with exponential back-off and honours the
Retry-After response header when present.
"""

import re
import time
import requests
from requests.auth import HTTPDigestAuth

# ── Constants ────────────────────────────────────────────────────────────────

_USER_AGENT = "FirstReleaseYearLookup/2.0 (contact: ecog@outlook.de)"
_MB_ROOT = "https://musicbrainz.org/ws/2/"
_AUTH = HTTPDigestAuth("EcoG", "3rfweqf345)^")

# MusicBrainz recommends ≤1 request/second for authenticated clients.
_REQUEST_DELAY = 1.1  # seconds inserted before every request
_RETRY_STATUSES = {429, 502, 503, 504}
_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds; doubles on each retry (2 → 4 → 8 → 16 …)

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
    """Case-insensitive comparison after stripping noise from both sides."""
    return (
        _clean_title(query_title).casefold() == _clean_title(candidate_title).casefold()
    )


def _mb_get(endpoint: str, params: dict) -> dict:
    """
    GET request against the MusicBrainz API with:
      • a polite inter-request delay (_REQUEST_DELAY seconds)
      • automatic retry + exponential back-off on transient errors
        (429, 502, 503, 504); honours the Retry-After header when present.

    Raises the underlying HTTPError only after all retries are exhausted.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    params = {**params, "fmt": "json"}
    url = _MB_ROOT + endpoint

    for attempt in range(_MAX_RETRIES):
        # Polite delay before every request (including the very first one)
        time.sleep(_REQUEST_DELAY)

        response = requests.get(
            url,
            params=params,
            headers=headers,
            auth=_AUTH,
            timeout=20,
        )

        # Success or a permanent client error → return / raise immediately
        if response.status_code not in _RETRY_STATUSES:
            response.raise_for_status()
            return response.json()

        # Transient server error – decide how long to wait
        if attempt == _MAX_RETRIES - 1:
            # All retries spent; surface the error
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else _BACKOFF_BASE * (2**attempt)

        print(
            f"[MusicBrainz] HTTP {response.status_code} on attempt "
            f"{attempt + 1}/{_MAX_RETRIES} – retrying in {wait:.1f}s …"
        )
        time.sleep(wait)

    # Unreachable, but keeps type-checkers happy
    raise RuntimeError("Exceeded maximum retries for MusicBrainz API")


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
        {"inc": "releases release-groups"},
    )
    years = []
    for release in data.get("releases", []):
        release_title = release.get("title", "")
        # Skip non-canonical release variants by title keywords
        if _is_bad_version(release_title):
            continue
        # Check secondary types on the release-group if present
        rg = release.get("release-group", {})
        secondary_types = {t.casefold() for t in rg.get("secondary-types", [])}
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
    data = _mb_get("recording", {"query": query, "limit": 25})

    best_year: int | None = None

    for rec in data.get("recordings", []):
        rec_title = rec.get("title", "")

        # Must match the requested title (after noise removal)
        if not _titles_match(title, rec_title):
            continue

        # Skip obviously non-canonical recordings by title
        if _is_bad_version(rec_title):
            continue

        # Verify the artist name (partial / case-insensitive)
        artist_credits = rec.get("artist-credit", [])
        artist_names = [
            ac.get("artist", {}).get("name", "").casefold()
            for ac in artist_credits
            if isinstance(ac, dict)
        ]
        if not any(
            artist.casefold() in name or name in artist.casefold()
            for name in artist_names
        ):
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
    data = _mb_get("release-group", {"query": query, "limit": 25})

    best_year: int | None = None

    for rg in data.get("release-groups", []):
        rg_title = rg.get("title", "")

        # Must match the requested title
        if not _titles_match(title, rg_title):
            continue

        # Reject bad secondary types
        secondary_types = {t.casefold() for t in rg.get("secondary-types", [])}
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
        raise ValueError(f'title_type must be "single" or "album", got {title_type!r}')

    if title_type == "single":
        return _first_year_single(title, artist)
    else:
        return _first_year_album(title, artist)


# ── Quick smoke-test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("Bohemian Rhapsody", "Queen", "single"),
        ("A Night at the Opera", "Queen", "album"),
        ("Smells Like Teen Spirit", "Nirvana", "single"),
        ("Nevermind", "Nirvana", "album"),
    ]
    for t_title, t_artist, t_type in tests:
        year = get_first_release_year(t_title, t_artist, t_type)
        print(f"{t_type:6} | {t_artist} – {t_title!r:35} → {year}")
