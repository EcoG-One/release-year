from __future__ import annotations

import re
import time
from typing import Optional, List, Tuple, Dict, Any
import requests


_MB_BASE = "https://musicbrainz.org/ws/2"
_DISCOGS_BASE = "https://api.discogs.com"

# Keywords that often indicate non-original / non-studio / non-canonical variants
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

# Some common “noise” tokens to strip from titles for robust matching
_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[\{].*?[\)\]\}]\s*"
)  # remove (...) / [...] / {...} parts


def _norm_artist(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"^the\s+", "", s)  # treat "The Beach Boys" ~ "Beach Boys"
    s = re.sub(r"[^\w\s]", "", s)  # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_title(s: str) -> str:
    s = s.strip().lower()
    s = _TITLE_NOISE_RE.sub(" ", s)  # drop parenthetical qualifiers
    s = s.replace("&", "and")
    s = re.sub(r"[’']", "", s)  # normalize apostrophes away (I'm -> Im)
    s = re.sub(r"[^\w\s]", " ", s)  # punctuation -> space (What's Up? -> Whats Up)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _looks_like_bad_version(text: str) -> bool:
    return bool(text and _BAD_VERSION_RE.search(text))


def _extract_year(date_str: str) -> Optional[int]:
    if not date_str:
        return None
    m = re.match(r"^\s*(\d{4})", date_str)
    if not m:
        return None
    y = int(m.group(1))
    return y if 1000 <= y <= 2100 else None


def _http_get_json(
    url: str, *, headers: dict, params: dict | None = None, timeout: int = 25
) -> dict:
    r = requests.get(url, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _mb_search_recordings(
    song_title: str, artist: str, user_agent: str, limit: int = 10
) -> List[dict]:
    headers = {"User-Agent": user_agent}
    query = f'recording:"{song_title}" AND artist:"{artist}"'
    url = f"{_MB_BASE}/recording"
    params = {"query": query, "fmt": "json", "limit": limit}
    data = _http_get_json(url, headers=headers, params=params)
    return data.get("recordings") or []


def _mb_artist_credit_str(rec: dict) -> str:
    # MusicBrainz returns artist-credit as list of {artist:{name}, name, joinphrase}
    parts = []
    for ac in rec.get("artist-credit") or []:
        name = ac.get("name") or (ac.get("artist") or {}).get("name") or ""
        join = ac.get("joinphrase") or ""
        parts.append(f"{name}{join}")
    return "".join(parts).strip()


def _mb_recording_quality_score(
    rec: dict, want_title_norm: str, want_artist_norm: str
) -> int:
    """
    Higher is better. We use this to pick the best candidate recordings
    before doing heavier detail fetches.
    """
    score = int(rec.get("score", 0))  # 0..100 from MB search

    title = rec.get("title") or ""
    title_norm = _norm_title(title)

    ac_str = _mb_artist_credit_str(rec)
    ac_norm = _norm_artist(ac_str)

    # Strong preference for normalized exact title match
    if title_norm == want_title_norm:
        score += 40
    elif want_title_norm in title_norm or title_norm in want_title_norm:
        score += 15

    # Strong preference for normalized artist match
    if ac_norm == want_artist_norm:
        score += 40
    elif want_artist_norm in ac_norm or ac_norm in want_artist_norm:
        score += 15

    # Penalize likely variants
    if _looks_like_bad_version(title) or _looks_like_bad_version(
        rec.get("disambiguation", "")
    ):
        score -= 60

    # Slight preference if releases already present in search payload
    if rec.get("releases"):
        score += 5

    return score


def _mb_fetch_recording_years(recording_id: str, user_agent: str) -> List[int]:
    """
    Fetch recording details and return plausible original release years.
    Filters:
      - Prefer releases with status=Official when available
      - Avoid obviously bad versions via disambiguation in release title
    """
    headers = {"User-Agent": user_agent}
    url = f"{_MB_BASE}/recording/{recording_id}"
    params = {
        "fmt": "json",
        "inc": "artists+releases",  # keep it light; still useful
    }

    # Be polite to MB rate limiting
    time.sleep(1.05)
    rec = _http_get_json(url, headers=headers, params=params)

    years_all: List[int] = []
    years_official: List[int] = []

    for rel in rec.get("releases") or []:
        y = _extract_year(rel.get("date") or "")
        if not y:
            continue

        rel_title = rel.get("title") or ""
        if _looks_like_bad_version(rel_title):
            continue

        years_all.append(y)
        if (rel.get("status") or "").lower() == "official":
            years_official.append(y)

    # Prefer official years if we have any; otherwise fall back
    return sorted(set(years_official or years_all))


def _musicbrainz_first_year(
    song_title: str, artist: str, user_agent: str
) -> Optional[int]:
    want_title_norm = _norm_title(song_title)
    want_artist_norm = _norm_artist(artist)

    recs = _mb_search_recordings(song_title, artist, user_agent=user_agent, limit=12)
    if not recs:
        return None

    # Rank candidates (cheap) and then fetch details for best few (expensive)
    ranked = sorted(
        recs,
        key=lambda r: _mb_recording_quality_score(r, want_title_norm, want_artist_norm),
        reverse=True,
    )

    candidate_years: List[int] = []

    # Try top N candidates; stop early if we get a very plausible early year
    for rec in ranked[:4]:
        rid = rec.get("id")
        if not rid:
            continue

        title = rec.get("title") or ""
        if _looks_like_bad_version(title) or _looks_like_bad_version(
            rec.get("disambiguation", "")
        ):
            continue

        # Ensure artist-credit isn't wildly off
        ac_norm = _norm_artist(_mb_artist_credit_str(rec))
        if want_artist_norm not in ac_norm and ac_norm not in want_artist_norm:
            continue

        years = _mb_fetch_recording_years(rid, user_agent=user_agent)
        candidate_years.extend(years)

        # If we found something, we can keep going a bit for even earlier,
        # but avoid too many calls.
        if candidate_years:
            break

    return min(candidate_years) if candidate_years else None


def _discogs_release_has_track(release_json: dict, want_title_norm: str) -> bool:
    tracklist = release_json.get("tracklist") or []
    for tr in tracklist:
        t = tr.get("title") or ""
        if not t:
            continue
        if _looks_like_bad_version(t):
            continue
        if _norm_title(t) == want_title_norm:
            return True
    return False


def _discogs_release_is_bad(release_json: dict) -> bool:
    # Skip unofficial; prefer avoiding compilations when possible
    if (release_json.get("status") or "").lower() == "unofficial":
        return True

    formats = release_json.get("formats") or []
    for f in formats:
        desc = " ".join((f.get("descriptions") or [])).lower()
        name = (f.get("name") or "").lower()
        if "unofficial" in desc or "unofficial" in name:
            return True
    return False


def _discogs_release_artist_match(release_json: dict, want_artist_norm: str) -> bool:
    artists = release_json.get("artists") or []
    # Some releases use "Various"; treat as mismatch
    names = [_norm_artist(a.get("name", "")) for a in artists if a.get("name")]
    if not names:
        return False
    if any(n == "various" for n in names):
        return False
    # Accept if any main artist matches loosely
    return any(
        n == want_artist_norm or want_artist_norm in n or n in want_artist_norm
        for n in names
    )


def _discogs_search(
    song_title: str,
    artist: str,
    user_agent: str,
    token: Optional[str],
    per_page: int = 8,
) -> List[dict]:
    headers = {"User-Agent": user_agent}
    if token:
        headers["Authorization"] = f"Discogs token={token}"

    url = f"{_DISCOGS_BASE}/database/search"
    params = {
        "type": "release",
        "artist": artist,
        "track": song_title,
        "per_page": per_page,
        "page": 1,
    }
    data = _http_get_json(url, headers=headers, params=params)
    return data.get("results") or []


def _discogs_first_year(
    song_title: str, artist: str, user_agent: str, discogs_token: Optional[str]
) -> Optional[int]:
    want_title_norm = _norm_title(song_title)
    want_artist_norm = _norm_artist(artist)

    results = _discogs_search(
        song_title, artist, user_agent, discogs_token, per_page=10
    )
    if not results:
        return None

    years_good: List[int] = []
    years_compilation: List[int] = []

    headers = {"User-Agent": user_agent}
    if discogs_token:
        headers["Authorization"] = f"Discogs token={discogs_token}"

    # Inspect a handful of the best-looking results
    for item in results[:7]:
        rid = item.get("id")
        if not rid:
            continue

        # Rate limiting / politeness
        time.sleep(1.0)

        # Fetch full release details to validate tracklist + artist properly
        try:
            rel = _http_get_json(f"{_DISCOGS_BASE}/releases/{rid}", headers=headers)
        except requests.HTTPError:
            continue

        if _discogs_release_is_bad(rel):
            continue

        if not _discogs_release_artist_match(rel, want_artist_norm):
            continue

        if not _discogs_release_has_track(rel, want_title_norm):
            continue

        y = rel.get("year")
        if not isinstance(y, int) or y <= 0:
            continue

        # Prefer non-compilation if possible
        formats = rel.get("formats") or []
        is_comp = any(
            "compilation" in " ".join((f.get("descriptions") or [])).lower()
            for f in formats
        )

        if is_comp:
            years_compilation.append(y)
        else:
            years_good.append(y)

    if years_good:
        return min(years_good)
    if years_compilation:
        return min(years_compilation)
    return None


def first_release_year(
    song_title: str, artist: str, *, discogs_token: Optional[str] = None
) -> Optional[int]:
    """
    Returns earliest plausible release year found across MusicBrainz + Discogs, with heuristics
    to reduce false positives (covers/live/remasters/etc.). Returns None if not found.

    Requirements:
      pip install requests

    Discogs:
      Pass discogs_token for best reliability.
    """
    user_agent = "FirstReleaseYearLookup/2.0 (contact: you@example.com)"

    mb_year = None
    dc_year = None

    try:
        mb_year = _musicbrainz_first_year(song_title, artist, user_agent=user_agent)
    except requests.RequestException:
        mb_year = None

    try:
        dc_year = _discogs_first_year(
            song_title, artist, user_agent=user_agent, discogs_token=discogs_token
        )
    except requests.RequestException:
        dc_year = None

    years = [y for y in (mb_year, dc_year) if isinstance(y, int)]
    return min(years) if years else None


if __name__ == "__main__":
    # Your known-good examples (expected “true” year)
    tests = [
        ("I'm Not In Love", "10cc", 1975),
        ("What's Up?", "4 Non Blondes", 1993),
        ("No Time To Die", "Billie Eilish", 2020),
        ("Surfin' U.S.A.", "The Beach Boys", 1963),
    ]

    for title, art, expected in tests:
        got = first_release_year(
            title, art, discogs_token=None
        )  # add token for best results
        print(f"{art} — {title} | expected {expected} | got {got}")
