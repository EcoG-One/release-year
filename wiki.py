# python
import re
import requests
from typing import Optional

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_USER_AGENT = "HitPlay/1.0 (ecog@outlook.de)"


def _fetch_page_info(title: str) -> Optional[dict]:
    headers = {"User-Agent": WIKIPEDIA_USER_AGENT}
    params = {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "categories|revisions",
        "rvprop": "content",
        "rvslots": "main",
        "redirects": 1,
    }
    resp = requests.get(WIKIPEDIA_API, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("query", {})
    pages = data.get("pages", {})
    if not pages:
        return None
    # there will be a single page in the response
    page = next(iter(pages.values()))
    if "missing" in page:
        return None
    return page


def _is_disambiguation(page: dict) -> bool:
    cats = page.get("categories", []) or []
    for c in cats:
        title = c.get("title", "").lower()
        if "disambiguation pages" in title or title.endswith("disambiguation pages") or "disambiguation" in title:
            return True
    # fallback: check content for common disambig template
    revs = page.get("revisions") or []
    if revs:
        content = revs[0].get("slots", {}).get("main", {}).get("*", "") or ""
        if re.search(r"\{\{[^}]*disambig", content, flags=re.I):
            return True
    return False


def _extract_infobox_wikitext(wikitext: str, template_name: str = "Infobox song") -> Optional[str]:
    idx = wikitext.find("{{" + template_name)
    if idx == -1:
        # try alternative capitalization or variants
        pattern = re.compile(r"\{\{\s*infobox\s+song", flags=re.I)
        m = pattern.search(wikitext)
        if m:
            idx = m.start()
        else:
            return None
    i = idx
    length = len(wikitext)
    count = 0
    # parse balanced templates
    while i < length - 1:
        if wikitext[i:i+2] == "{{":
            count += 1
            i += 2
            continue
        if wikitext[i:i+2] == "}}":
            count -= 1
            i += 2
            if count == 0:
                return wikitext[idx:i]
            continue
        i += 1
    return None


def _parse_released_from_infobox(infobox: str, artist:str) -> str:
    lines = infobox.splitlines()
    key = None
    artist_raw = ''
    value_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            # commit previous
            if key and key.lower() == "artist":
                artist_raw = _clean_wikitext("\n".join(value_lines).strip())
                print(f"Debug: Found artist: {_clean_wikitext(artist_raw)}")
            if key and key.lower() == "released":
                break
            # new key
            parts = stripped[1:].split("=", 1)
            key = parts[0].strip() if parts else None
            value = parts[1].strip() if len(parts) > 1 else ""
            value_lines = [value]
        else:
            # continuation lines (sometimes values span lines)
            if key:
                value_lines.append(stripped)
    if (key and key.lower() == "released") and artist_raw == artist:
        raw = "\n".join(value_lines).strip()
        return _clean_wikitext(raw)
    return ""


def _clean_wikitext(text: str) -> str:
    if not text:
        return ""
    s = text
    # remove references
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S | re.I)
    s = re.sub(r"<ref[^/]*/>", "", s, flags=re.I)
    # remove HTML tags
    s = re.sub(r"<[^>]+>", "", s)
    # handle links [[Page|Text]] or [[Text]]
    s = re.sub(r"\[\[([^|\]]*\|)?([^\]]+)\]\]", lambda m: m.group(2), s)
    # remove templates like {{...}} — try to keep inner text for common date templates
    # for date templates like {{Start date|1997|11|25}} -> try to extract numbers
    def _tpl_repl(m):
        inner = m.group(1)
        # start date / date formats
        if inner:
            parts = inner.split("|")
            if parts[0].strip().lower() in ("start date", "start date and age", "start-date", "birth date", "birth-date", "date"):
                nums = [p for p in parts[1:] if p.strip().isdigit()]
                if nums:
                    return "-".join(nums)
        return ""
    s = re.sub(r"\{\{([^\}]*)\}\}", _tpl_repl, s)
    # remove remaining brackets, citations, and excessive whitespace
    s = re.sub(r"\[|\]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get_song_release_date(title: str, artist: str) -> str:
    """
    Return the 'released' field from the Infobox song for the given title and artist.
    Returns empty string if not found.
    """
    # normalize by stripping surrounding quotes/spaces
    title = title.strip().strip('"').strip("'")
    artist = artist.strip()

    # 1) try exact title
    page = _fetch_page_info(title)
    if page:
        if not _is_disambiguation(page):
            # attempt extract
            revs = page.get("revisions") or []
            if revs:
                content = revs[0].get("slots", {}).get("main", {}).get("*", "") or ""
                infobox = _extract_infobox_wikitext(content)
                if infobox:
                    released = _parse_released_from_infobox(infobox, artist)
                    if released:
                        return released
            # if no released or no infobox, return empty (per spec)
            return ""

    # 2) if disambiguation or missing, try "<title> (song)"
    alt1 = f"{title} (song)"
    page = _fetch_page_info(alt1)
    if page:
        revs = page.get("revisions") or []
        if revs:
            content = revs[0].get("slots", {}).get("main", {}).get("*", "") or ""
            infobox = _extract_infobox_wikitext(content)
            if infobox:
                released = _parse_released_from_infobox(infobox, artist)
                if released:
                    return released
            else:

                # 3) try "<title> (<artist> song)"
                # sanitize artist for title use: remove problematic slashes and parentheses
                artist_for_title = re.sub(r"[\/\(\)]", "", artist).strip()
                alt2 = f"{title} ({artist_for_title} song)"
                page = _fetch_page_info(alt2)
                if page:
                    revs = page.get("revisions") or []
                    if revs:
                        content = revs[0].get("slots", {}).get("main", {}).get("*", "") or ""
                        infobox = _extract_infobox_wikitext(content)
                        if infobox:
                            released = _parse_released_from_infobox(infobox, artist)
                            if released:
                                return released
                    return ""

    # nothing found
    return ""

# print(
#    "Released Date: ", get_song_release_date("Y.M.C.A", "Village People"))
# Example usage
