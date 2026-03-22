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

_USER_AGENT     = "FirstReleaseYearLookup/2.0 (contact: ecog@outlook.de)"
_MB_ROOT        = "https://musicbrainz.org/ws/2/"
_AUTH           = HTTPDigestAuth("EcoG", "3rfweqf345)^")

# MusicBrainz recommends ≤1 request/second for authenticated clients.
_REQUEST_DELAY  = 1.1     # seconds inserted before every request
_RETRY_STATUSES = {429, 502, 503, 504}
_MAX_RETRIES    = 5
_BACKOFF_BASE   = 2.0     # seconds; doubles on each retry (2 → 4 → 8 → 16 …)

# Minimum Lucene relevance score (0–100) to accept a search hit.
_MIN_SCORE      = 90

# Page size for browse requests (max allowed by MusicBrainz).
_BROWSE_LIMIT   = 100

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

# Release-group primary types we trust for a song's canonical release.
# "Other" covers soundtracks, radio shows, etc. — excluded by omission.
_GOOD_PRIMARY_TYPES = {"single", "album", "ep"}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_title(title: str) -> str:
    """Strip bracketed noise tokens, then collapse whitespace."""
    return _TITLE_NOISE_RE.sub(" ", title).strip()


def _bracketed_parts_are_bad(raw_title: str) -> bool:
    """
    Return True only if the content *inside* brackets contains a bad-version
    keyword.  The base title (outside brackets) is intentionally not checked
    so that songs like "Live and Let Die" are not rejected.

    Examples
    --------
    "Bohemian Rhapsody (Remastered 2011)"  → True
    "Live and Let Die"                     → False  (no brackets at all)
    "Live and Let Die (Live at Wembley)"   → True   (bracket content is bad)
    """
    bracketed = re.findall(r"[\(\[\{](.*?)[\)\]\}]", raw_title)
    return any(_BAD_VERSION_RE.search(part) for part in bracketed)


def _titles_match(query_title: str, candidate_title: str) -> bool:
    """Case-insensitive comparison after stripping noise from both sides."""
    return (
        _clean_title(query_title).casefold()
        == _clean_title(candidate_title).casefold()
    )


def _mb_get(endpoint: str, params: dict) -> dict:
    """
    GET against the MusicBrainz API with polite delay, retry, and back-off.
    inc= values must be joined with '+'.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    params  = {**params, "fmt": "json"}
    url     = _MB_ROOT + endpoint

    for attempt in range(_MAX_RETRIES):
        time.sleep(_REQUEST_DELAY)

        response = requests.get(
            url, params=params, headers=headers, auth=_AUTH, timeout=20,
        )

        if response.status_code not in _RETRY_STATUSES:
            response.raise_for_status()
            return response.json()

        if attempt == _MAX_RETRIES - 1:
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else _BACKOFF_BASE * (2 ** attempt)
        print(
            f"[MusicBrainz] HTTP {response.status_code} on attempt "
            f"{attempt + 1}/{_MAX_RETRIES} – retrying in {wait:.1f}s …"
        )
        time.sleep(wait)

    raise RuntimeError("Exceeded maximum retries for MusicBrainz API")


def _parse_year(date_str: str | None) -> int | None:
    """Extract the 4-digit year from a MusicBrainz date string."""
    if not date_str:
        return None
    m = re.match(r"(\d{4})", date_str)
    return int(m.group(1)) if m else None


# ── Core logic ───────────────────────────────────────────────────────────────

def _browse_all_releases_for_recording(recording_mbid: str) -> list[dict]:
    """
    Return ALL official releases containing this recording via paginated
    browse requests (bypasses the 25-result lookup cap).
    """
    releases: list[dict] = []
    offset = 0

    while True:
        data = _mb_get(
            "release",
            {
                "recording": recording_mbid,
                "inc": "release-groups",   # '+'-joined; single value here
                "limit": _BROWSE_LIMIT,
                "offset": offset,
                "status": "official",      # drop unofficial server-side
            },
        )
        page = data.get("releases", [])
        releases.extend(page)

        total = data.get("release-count", len(releases))
        offset += len(page)
        if offset >= total or not page:
            break

    return releases


def _release_is_canonical(release: dict) -> bool:
    """
    Return True if a release passes all canonical filters:

    1. Its own title's bracketed content must not contain bad-version keywords.
    2. Its release-group must have a good primary type (Single / Album / EP).
       "Other" (soundtracks, radio shows, …) is rejected by omission.
    3. Its release-group must have no bad secondary types (compilation, live …).
    4. Its release-group title's bracketed content must not be a bad variant.
    """
    # 1. Release title brackets
    if _bracketed_parts_are_bad(release.get("title", "")):
        return False

    rg = release.get("release-group", {})

    # 2. Primary type must be Single, Album, or EP
    primary = rg.get("primary-type", "")
    if primary.casefold() not in _GOOD_PRIMARY_TYPES:
        return False

    # 3. No bad secondary types
    secondary = {t.casefold() for t in rg.get("secondary-types", [])}
    if secondary & _BAD_SECONDARY_TYPES:
        return False

    # 4. Release-group title brackets
    if _bracketed_parts_are_bad(rg.get("title", "")):
        return False

    return True


def _earliest_canonical_release_year(recording_mbid: str) -> int | None:
    """
    Browse all official releases for a recording and return the earliest
    year among canonical ones.
    """
    years = [
        year
        for release in _browse_all_releases_for_recording(recording_mbid)
        if _release_is_canonical(release)
        and (year := _parse_year(release.get("date"))) is not None
    ]
    return min(years) if years else None


def _first_year_single(title: str, artist: str) -> int | None:
    """
    Search for recordings (song) and return the earliest canonical release
    year across all matching recordings.
    """
    query = f'recording:"{title}" AND artist:"{artist}"'
    data  = _mb_get("release", {"query": query, "limit": 25})

    years: list[int] = []

    for rec in data.get("recordings", []):
        # 1. Confidence gate
        if int(rec.get("score", 0)) < _MIN_SCORE:
            continue

        rec_title = rec.get("title", "")

        # 2. Title must match after noise stripping
        if not _titles_match(title, rec_title):
            continue

        # 3. Reject if the *bracketed* portion of the recording title is bad
        if _bracketed_parts_are_bad(rec_title):
            continue

        # 4. Artist must match (partial / case-insensitive)
        credits = rec.get("artist-credit", [])
        names   = [
            ac.get("artist", {}).get("name", "").casefold()
            for ac in credits if isinstance(ac, dict)
        ]
        if not any(
            artist.casefold() in n or n in artist.casefold() for n in names
        ):
            continue

        # 5. Browse all releases for this recording, apply canonical filter
        mbid = rec.get("id")
        if not mbid:
            continue

        year = _earliest_canonical_release_year(mbid)
        if year:
            years.append(year)

    return min(years) if years else None


def _first_year_album(title: str, artist: str) -> int | None:
    """
    Search for a release-group (album) and return its earliest
    canonical first-release year.
    """
    query = f'release-group:"{title}" AND artist:"{artist}"'
    data  = _mb_get("release-group", {"query": query, "limit": 25})

    years: list[int] = []

    for rg in data.get("release-groups", []):
        if int(rg.get("score", 0)) < _MIN_SCORE:
            continue

        rg_title = rg.get("title", "")

        if not _titles_match(title, rg_title):
            continue

        secondary = {t.casefold() for t in rg.get("secondary-types", [])}
        if secondary & _BAD_SECONDARY_TYPES:
            continue

        if _bracketed_parts_are_bad(rg_title):
            continue

        year = _parse_year(rg.get("first-release-date"))
        if year:
            years.append(year)

    return min(years) if years else None


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
    return _first_year_single(title, artist) if title_type == "single" \
        else _first_year_album(title, artist)


# ── Quick smoke-test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("Bohemian Rhapsody", "Queen", "single", 1975),
        ("A Night at the Opera", "Queen", "album", 1975),
        ("Smells Like Teen Spirit", "Nirvana", "single", 1991),
        ("Nevermind", "Nirvana", "album", 1991),
        ("Live and Let Die", "Wings", "single", 1973),
        ("Hey Ho", "GFreddy Kalas", "single", 2015),
        ("Driving Home for Christmas (2019 Remaster)", "Chris Rea", "single", 1986),
        ("Shut Up and Dance", "Walk the Moon", "single", 2014),
        ("Die With A Smile", "Lady Gaga & Bruno Mars", "single", 2024),
        ("For What It's Worth", "Buffalo Springfield", "single", 1966),
        ("Calypso", "John Denver", "single", 1975),
    ]
    print(f"{'Type':6}  {'Artist + Title':<42}  {'Got':>4}  {'Exp':>4}  OK?")
    print("-" * 65)
    for t_title, t_artist, t_type, expected in tests:
        got = get_first_release_year(t_title, t_artist, t_type)
        ok  = "✓" if got == expected else "✗"
        label = f"{t_artist} – {t_title}"
        print(f"{t_type:6}  {label:<42}  {str(got):>4}  {expected:>4}  {ok}")
