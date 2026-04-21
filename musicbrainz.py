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
import threading
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

_mb_rate_lock = threading.Lock()
_mb_last_request_at = 0.0

# Minimum Lucene relevance score (0–100) to accept a search hit.
_MIN_SCORE = 85

# Bracket content that looks like a bad version but is actually
# MusicBrainz's canonical label for the original studio cut.
# Only 'album version' and 'single version' qualify; 'mono version',
# 'stereo version', 'studio version' etc. are still rejected.
_CANONICAL_SUFFIX_RE = re.compile(
    r"^\s*(?:album|single)\s+version\s*$",
    re.IGNORECASE,
)

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


def _bracketed_parts_are_bad(raw_title: str) -> bool:
    """
    Return True only if the content *inside* brackets contains a bad-version
    keyword, with one exception: "(Album Version)" and "(Single Version)" are
    whitelisted because MusicBrainz uses them to label the *original* studio
    cut, not a derivative.  All other uses of "version" (stereo, mono, studio,
    remastered …) remain bad.

    Examples
    --------
    "Jezebel (Album Version)"              → False  (canonical label)
    "Kicks (Single Version)"               → False  (canonical label)
    "Bohemian Rhapsody (Remastered 2011)"  → True
    "Something (Stereo Version)"           → True
    "Live and Let Die"                     → False  (no brackets at all)
    "Live and Let Die (Live at Wembley)"   → True
    """
    bracketed = re.findall(r"[\(\[\{](.*?)[\)\]\}]", raw_title)
    for part in bracketed:
        if _CANONICAL_SUFFIX_RE.match(part):
            continue   # "(Album Version)" / "(Single Version)" → keep
        if _BAD_VERSION_RE.search(part):
            return True
    return False


def _titles_match(query_title: str, candidate_title: str) -> bool:
    """Case-insensitive comparison after stripping noise from both sides."""
    return (
        _clean_title(query_title).casefold() == _clean_title(candidate_title).casefold()
    )


def _normalize_artist(name: str) -> str:
    """Normalise an artist name for fuzzy comparison.

    Strips leading articles (the/a/an), converts & → and, removes
    punctuation, and lowercases.  This lets "Percy Faith & His Orchestra"
    match "Percy Faith and His Orchestra", and "The Four Lads" match
    "Four Lads".
    """
    s = name.casefold()
    s = re.sub(r"\b(the|a|an)\b", "", s)  # strip articles
    s = re.sub(r"[&+]", "and", s)  # & → and
    s = re.sub(r"[^\w\s]", "", s)  # strip punctuation
    return re.sub(r"\s+", " ", s).strip()


def _artist_matches(query_artist: str, mb_name: str) -> bool:
    """Return True if the query artist name and the MusicBrainz credit
    refer to the same artist after normalisation.
    """
    q = _normalize_artist(query_artist)
    m = _normalize_artist(mb_name)
    return q == m or q in m or m in q


def _mb_get(endpoint: str, params: dict) -> dict:
    """
    GET against the MusicBrainz API with polite delay, retry, and back-off.
    """
    global _mb_last_request_at
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    params = {**params, "fmt": "json"}
    url = _MB_ROOT + endpoint

    for attempt in range(_MAX_RETRIES):
        time.sleep(_REQUEST_DELAY)

        response = requests.get(
            url,
            params=params,
            headers=headers,
            auth=_AUTH,
            timeout=20,
        )

        if response.status_code not in _RETRY_STATUSES:
            response.raise_for_status()
            return response.json()

        if attempt == _MAX_RETRIES - 1:
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else _BACKOFF_BASE * (2**attempt)
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


def _browse_canonical_releases(recording_mbid: str) -> list[dict]:
    """
    Return all official Single/Album/EP releases containing this recording,
    using paginated browse requests.

    Key API facts exploited here
    ────────────────────────────
    • Browse /release supports server-side `type=` filtering by release-group
      primary type (Album|Single|EP), which eliminates compilations, soundtracks
      and "Other"-typed releases *before* they reach us — no secondary-type
      data needed for that coarse filter.
    • Browse /release supports `status=official` server-side.
    • Browse responses cap at `limit` per page; pagination via `offset` is
      required for popular songs that appear on hundreds of releases.
    • `inc=release-groups` on a browse request returns the release-group object
      with at least `id`, `primary-type`, and `secondary-types`.
    """
    releases: list[dict] = []
    offset = 0

    while True:
        data = _mb_get(
            "release",
            {
                "recording": recording_mbid,
                "inc": "release-groups",
                "status": "official",
                # Server-side primary-type filter: rejects compilations,
                # soundtracks, and anything typed "Other" automatically.
                "type": "album|single|ep",
                "limit": _BROWSE_LIMIT,
                "offset": offset,
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
    Secondary (client-side) filter applied after the server-side type/status
    filters.  Checks:
      1. Release title brackets must not contain bad-version keywords.
      2. Release-group secondary types must not include compilation, live, …
         (catches cases the server-side `type=` filter misses, e.g. an Album
         with secondary type "Compilation").
      3. Release-group title brackets must not be a bad variant.
    """
    # 1. Release title brackets
    if _bracketed_parts_are_bad(release.get("title", "")):
        return False

    rg = release.get("release-group", {})

    # 2. Secondary types (may be present in browse response)
    secondary = {t.casefold() for t in rg.get("secondary-types", [])}
    if secondary & _BAD_SECONDARY_TYPES:
        return False

    # 3. Release-group title brackets
    if _bracketed_parts_are_bad(rg.get("title", "")):
        return False

    return True


def _earliest_canonical_release_year(recording_mbid: str) -> int | None:
    """
    Browse all canonical releases for a recording and return the earliest year.
    """
    years = [
        year
        for release in _browse_canonical_releases(recording_mbid)
        if _release_is_canonical(release)
        and (year := _parse_year(release.get("date"))) is not None
    ]
    return min(years) if years else None


def _recording_search_query(title: str, artist: str) -> str:
    """
    Build a recording query that asks the search index for plausible official
    single/album/EP matches only, so one search can yield the first year.
    """
    clean_title = _clean_title(title)
    query_parts = [
        f'recording:"{clean_title}"',
        f'artist:"{artist}"',
        "status:official",
        "(primarytype:single OR primarytype:album OR primarytype:ep)",
    ]
    query_parts.extend(
        f'NOT secondarytype:"{secondary}"' for secondary in sorted(_BAD_SECONDARY_TYPES)
    )
    return " AND ".join(query_parts)


def _first_year_single(title: str, artist: str) -> int | None:
    """
    Search for recordings (song) and return the earliest indexed
    first-release year across all matching canonical recordings.

    This stays to one MusicBrainz request per song by using the search
    index's `first-release-date` field instead of browsing each recording's
    releases separately.
    """
    query = _recording_search_query(title, artist)
    data = _mb_get("recording", {"query": query, "limit": 25})

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
        names = [
            ac.get("artist", {}).get("name", "").casefold()
            for ac in credits
            if isinstance(ac, dict)
        ]
        if not any(_artist_matches(artist, n) for n in names):
            continue

        # 5. Use the indexed first-release date carried on the search hit.
        year = _parse_year(rec.get("first-release-date"))
        if year:
            years.append(year)

    return min(years) if years else None


def _first_year_album(title: str, artist: str) -> int | None:
    """
    Search for a release-group (album) and return its earliest
    canonical first-release year.
    """
    query = f'release-group:"{title}" AND artist:"{artist}"'
    data = _mb_get("release-group", {"query": query, "limit": 25})

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


def get_first_release_year_mb(title: str, artist: str, title_type: str) -> int | None:
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
    return (
        _first_year_single(title, artist)
        if title_type == "single"
        else _first_year_album(title, artist)
    )


# ── Quick smoke-test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("Bohemian Rhapsody", "Queen", "single", 1975),
        ("A Night at the Opera", "Queen", "album", 1975),
        ("Smells Like Teen Spirit", "Nirvana", "single", 1991),
        ("Nevermind", "Nirvana", "album", 1991),
        ("Live and Let Die", "Wings", "single", 1973),
        ("Shut Up and Dance", "Walk the Moon", "single", 2014),
        ("Die With a Smile", "Lady Gaga", "single", 2024),
        ("For What It's Worth", "Buffalo Springfield", "single", 1966),
        ("Calypso", "John Denver", "single", 1975),
        ("Driving Home for Christmas", "Chris Rea", "single", 1986),
        # Titles with "(Album Version)" / "(Single Version)" annotations
        ("Jezebel", "Frankie Laine", "single", 1951),
        ("Because Of You", "Tony Bennett", "single", 1951),
        ("I Left My Heart in San Francisco", "Tony Bennett", "single", 1962),
        ("People", "Barbra Streisand", "single", 1964),
        ("Kicks", "Paul Revere & The Raiders", "single", 1966),
        ("Like A Rolling Stone", "Bob Dylan", "single", 1965),
        # Parenthetical subtitle in MB title
        ("Standing On The Corner", "The Four Lads", "single", 1956),
        # Ampersand in artist name
        ('Theme from "A Summer Place"', "Percy Faith & His Orchestra", "single", 1960),
    ]
    print(f"{'Type':6}  {'Artist + Title':<48}  {'Got':>4}  {'Exp':>4}  OK?")
    print("-" * 72)
    for t_title, t_artist, t_type, expected in tests:
        got = get_first_release_year_mb(t_title, t_artist, t_type)
        ok = "✓" if got == expected else "✗"
        label = f"{t_artist} – {t_title}"
        print(f"{t_type:6}  {label:<48}  {str(got):>4}  {expected:>4}  {ok}")
