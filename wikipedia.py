"""
wikipedia_release_year.py
─────────────────────────
Returns the first (oldest) release year for a song or album using the
Wikipedia API — searching for the article, fetching its wikitext, and
parsing the infobox released / release_date fields.

No third-party dependencies beyond `requests`.
"""

import re
import requests

# ── Constants ────────────────────────────────────────────────────────────────

WIKIPEDIA_API        = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_USER_AGENT = "HitPlay/1.0 (ecog@outlook.de)"

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
    "compilation", "live", "remix", "dj-mix", "demo", "bootleg",
    "promotional", "promo", "interview", "audiobook", "audio drama",
    "spokenword", "field recording", "unofficial",
}

# Any 4-digit year between 1900 and 2099
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# Wikipedia infobox date templates:
#   {{Start date|1975|10|31}} or {{Start date and age|1975|10|31|...}}
#   {{Film date|1975|10|31}} etc.
_DATE_TEMPLATE_RE = re.compile(
    r"\{\{\s*(?:Start\s*date(?:\s*and\s*age)?|Film\s*date|Birth\s*date"
    r"|End\s*date|Release\s*date)\s*\|([^}]+)\}\}",
    re.IGNORECASE,
)

# Infobox field names that carry release dates (covers song, album, film)
_RELEASE_FIELDS = re.compile(
    r"^\s*\|\s*(?:released?(?:_date)?|release_date\d*|"
    r"release\d*|published|pub_date|airdate|air_date)\s*=\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_title(title: str) -> str:
    return _TITLE_NOISE_RE.sub(" ", title).strip()


def _titles_match(a: str, b: str) -> bool:
    return _clean_title(a).casefold() == _clean_title(b).casefold()


def _bracketed_parts_are_bad(text: str) -> bool:
    """True if any bracketed/parenthetical part contains a bad-version word."""
    parts = re.findall(r"[\(\[\{](.*?)[\)\]\}]", text)
    return any(_BAD_VERSION_RE.search(p) for p in parts)


def _field_is_bad(field_value: str) -> bool:
    """
    True if the *entire* field value (label + brackets) suggests a
    non-canonical variant.  Checks both base text and bracketed parts so we
    catch e.g. '| released = 1977 (remaster)'.
    """
    return bool(_BAD_VERSION_RE.search(field_value)) if _bracketed_parts_are_bad(field_value) \
        else False


def _years_from_text(text: str) -> list[int]:
    """Extract all 4-digit years (1900-2099) from an arbitrary string."""
    return [int(y) for y in _YEAR_RE.findall(text)]


def _years_from_date_templates(text: str) -> list[int]:
    """
    Extract years from {{Start date|YYYY|...}} and similar templates.
    The first pipe-delimited token after the template name is the year.
    """
    years = []
    for m in _DATE_TEMPLATE_RE.finditer(text):
        parts = m.group(1).split("|")
        if parts:
            try:
                years.append(int(parts[0].strip()))
            except ValueError:
                pass
    return years


# ── Wikipedia API ─────────────────────────────────────────────────────────────

def _wiki_get(params: dict) -> dict:
    """Thin wrapper around the Wikipedia API."""
    headers = {"User-Agent": WIKIPEDIA_USER_AGENT}
    params  = {"format": "json", "formatversion": "2", **params}
    r = requests.get(WIKIPEDIA_API, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def _search_articles(query: str, limit: int = 8) -> list[dict]:
    """
    Full-text search; returns a list of {title, snippet} dicts ranked by
    Wikipedia's own relevance score.
    """
    data = _wiki_get({
        "action": "query",
        "list":   "search",
        "srsearch": query,
        "srlimit": limit,
        "srinfo":  "",
        "srprop":  "snippet",
    })
    return data.get("query", {}).get("search", [])


def _get_wikitext(page_title: str) -> str | None:
    """Fetch the raw wikitext of a Wikipedia article by its exact title."""
    data = _wiki_get({
        "action":  "query",
        "prop":    "revisions",
        "titles":  page_title,
        "rvslots": "main",
        "rvprop":  "content",
        "redirects": 1,
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    try:
        return page["revisions"][0]["slots"]["main"]["content"]
    except (KeyError, IndexError):
        return None


def _get_plain_intro(page_title: str) -> str:
    """
    Fetch the plain-text introduction of a Wikipedia article (first ~500 chars).
    Used as a year-extraction fallback.
    """
    data = _wiki_get({
        "action":   "query",
        "prop":     "extracts",
        "titles":   page_title,
        "exintro":  True,
        "explaintext": True,
        "exsentences": 3,
        "redirects": 1,
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return ""
    return pages[0].get("extract", "")


# ── Infobox parsing ──────────────────────────────────────────────────────────

def _extract_years_from_infobox(wikitext: str) -> list[int]:
    """
    Pull years from all release-date fields in the wikitext infobox.

    Strategy
    ────────
    1. Find every infobox field whose name matches a release-date pattern.
    2. For each field value:
       a. Skip if the value's bracketed annotations are bad-version keywords
          (e.g. "1977 (remaster)" → skip; "1975" → keep).
       b. Extract years from {{Start date|...}} templates first (most precise).
       c. Fall back to bare year regex on the raw field value.
    3. Return the deduplicated list of years found.
    """
    years: list[int] = []

    for m in _RELEASE_FIELDS.finditer(wikitext):
        field_val = m.group(1).strip()

        # Skip field values whose bracketed content flags a bad variant
        if _bracketed_parts_are_bad(field_val):
            # But only skip if the *base* (non-bracketed) text also matches bad
            # — to avoid discarding "1975 (US)" style annotations
            base = _TITLE_NOISE_RE.sub("", field_val).strip()
            if _BAD_VERSION_RE.search(base):
                continue

        # Try {{Start date|...}} templates first
        template_years = _years_from_date_templates(field_val)
        if template_years:
            years.extend(template_years)
        else:
            # Fall back to bare year in the field value
            years.extend(_years_from_text(field_val))

    return years


# ── Article selection ─────────────────────────────────────────────────────────

# Patterns in article titles / snippets that indicate a non-canonical page
_BAD_ARTICLE_RE = re.compile(
    r"\b(greatest\s*hits?|best\s*of|compilation|discography|"
    r"live\s*at|tour|concert|tribute|karaoke|instrumental|"
    r"soundtrack|radio\s*edit|anniversary\s*edition|deluxe)\b",
    re.IGNORECASE,
)

_TYPE_KEYWORDS = {
    "single": ["single", "song"],
    "album":  ["album", "studio album", "record"],
}


def _score_candidate(result: dict, title: str, artist: str,
                      title_type: str) -> float:
    """
    Heuristic score for a Wikipedia search result (higher = better).
    Returns -1 to reject outright.
    """
    art_title  = result.get("title", "")
    snippet    = result.get("snippet", "").lower()
    art_lower  = art_title.lower()
    query_clean = _clean_title(title).casefold()

    # Hard reject: obviously bad article
    if _BAD_ARTICLE_RE.search(art_title):
        return -1.0

    score = 0.0

    # Title match in article name
    if query_clean in art_lower:
        score += 3.0
    # Artist name in article name or snippet
    if artist.casefold() in art_lower or artist.casefold() in snippet:
        score += 2.0
    # Type keyword in article name or snippet
    for kw in _TYPE_KEYWORDS.get(title_type, []):
        if kw in art_lower or kw in snippet:
            score += 1.5

    return score


def _find_best_article(title: str, artist: str, title_type: str) -> str | None:
    """
    Search Wikipedia and return the title of the most relevant article, or
    None if nothing credible is found.
    """
    # Try a specific query first
    for query in [
        f"{title} {artist} {title_type}",
        f"{title} {artist} song" if title_type == "single" else f"{title} {artist} album",
        f"{title} {artist}",
    ]:
        results = _search_articles(query, limit=10)
        if not results:
            continue

        scored = [
            (r, _score_candidate(r, title, artist, title_type))
            for r in results
        ]
        scored = [(r, s) for r, s in scored if s >= 0]
        if not scored:
            continue

        best_result, best_score = max(scored, key=lambda x: x[1])
        if best_score > 0:
            return best_result["title"]

    return None


# ── Core logic ────────────────────────────────────────────────────────────────

def _first_year_from_article(page_title: str) -> int | None:
    """
    Fetch a Wikipedia article and extract the earliest plausible release year
    from its infobox, with a plain-text intro fallback.
    """
    wikitext = _get_wikitext(page_title)
    if not wikitext:
        return None

    years = _extract_years_from_infobox(wikitext)

    if not years:
        # Fallback: scan the introductory paragraph for a bare year
        intro = _get_plain_intro(page_title)
        years = _years_from_text(intro)

    # Sanity clamp: ignore years before the LP era or in the future
    years = [y for y in years if 1940 <= y <= 2030]
    return min(years) if years else None


# ── Public API ────────────────────────────────────────────────────────────────

def get_first_release_year_wp(title: str, artist: str, title_type: str) -> int | None:
    """
    Return the first (oldest) canonical release year for a song or album
    using the Wikipedia API.

    Parameters
    ----------
    title       : Song title (title_type="single") or album title
                  (title_type="album").
    artist      : Artist / band name.
    title_type  : "single" or "album".

    Returns
    -------
    int | None  : Four-digit year, or None if not found.

    Raises
    ------
    ValueError  : If title_type is not "single" or "album".
    """
    if title_type not in ("single", "album"):
        raise ValueError(
            f'title_type must be "single" or "album", got {title_type!r}'
        )

    page_title = _find_best_article(title, artist, title_type)
    if not page_title:
        return None

    return _first_year_from_article(page_title)


# ── Quick smoke-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("Bohemian Rhapsody",          "Queen",               "single", 1975),
        ("A Night at the Opera",       "Queen",               "album",  1975),
        ("Smells Like Teen Spirit",    "Nirvana",             "single", 1991),
        ("Nevermind",                  "Nirvana",             "album",  1991),
        ("Live and Let Die",           "Wings",               "single", 1973),
        ("Shut Up and Dance",          "Walk the Moon",       "single", 2014),
        ("Die With a Smile",           "Lady Gaga",           "single", 2024),
        ("For What It's Worth",        "Buffalo Springfield", "single", 1966),
        ("Calypso",                    "John Denver",         "single", 1975),
        ("Driving Home for Christmas", "Chris Rea",           "single", 1986),
    ]
    print(f"{'Type':6}  {'Artist + Title':<50}  {'Got':>4}  {'Exp':>4}  OK?")
    print("-" * 74)
    for t_title, t_artist, t_type, expected in tests:
        got = get_first_release_year_wp(t_title, t_artist, t_type)
        ok  = "✓" if got == expected else "✗"
        label = f"{t_artist} – {t_title}"
        print(f"{t_type:6}  {label:<50}  {str(got):>4}  {expected:>4}  {ok}")
