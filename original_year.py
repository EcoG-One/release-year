from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import List, Optional

import requests


_MB_BASE = "https://musicbrainz.org/ws/2"
_DISCOGS_BASE = "https://api.discogs.com"
_DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN")
_USER_AGENT = "FirstReleaseYearLookup/2.0 (contact: ecog@outlook.de)"

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

_TITLE_NOISE_RE = re.compile(r"\s*[\(\[\{].*?[\)\]\}]\s*")
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


def _norm_artist(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^the\s+", "", value)
    value = re.sub(r"[^\w\s]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _norm_title(value: str) -> str:
    value = value.strip().lower()
    value = _TITLE_NOISE_RE.sub(" ", value)
    value = value.replace("&", "and")
    value = re.sub(r"[’']", "", value)
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _looks_like_bad_version(text: str) -> bool:
    return bool(text and _BAD_VERSION_RE.search(text))


def _extract_year(date_str: str) -> Optional[int]:
    if not date_str:
        return None
    match = re.match(r"^\s*(\d{4})", str(date_str))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2100 else None


def _http_get_json(
    url: str,
    *,
    headers: dict,
    params: Optional[dict] = None,
    timeout: int = 25,
) -> dict:
    response = requests.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _mb_search_releases(
    song_title: str,
    artist: str,
    scan_for: str,
    limit: int = 25,
) -> List[dict]:
    headers = {"User-Agent": _USER_AGENT}
    release_type = "album" if scan_for == "album" else "single"
    query = (
        f'release:"{song_title}" AND artist:"{artist}" AND status:"official" '
        f'AND primarytype:"{release_type}"'
    )
    params = {"query": query, "fmt": "json", "limit": limit}
    data = _http_get_json(f"{_MB_BASE}/release", headers=headers, params=params)
    return data.get("releases") or []


def _mb_release_artist_str(release: dict) -> str:
    parts = []
    for credit in release.get("artist-credit") or []:
        name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
        join_phrase = credit.get("joinphrase") or ""
        parts.append(f"{name}{join_phrase}")
    return "".join(parts).strip()


def _mb_release_quality_score(
    release: dict,
    want_title_norm: str,
    want_artist_norm: str,
) -> int:
    score = int(release.get("score", 0))
    title_norm = _norm_title(release.get("title") or "")
    artist_norm = _norm_artist(_mb_release_artist_str(release))

    if title_norm == want_title_norm:
        score += 40
    elif want_title_norm in title_norm or title_norm in want_title_norm:
        score += 15

    if artist_norm == want_artist_norm:
        score += 40
    elif want_artist_norm in artist_norm or artist_norm in want_artist_norm:
        score += 15

    if _looks_like_bad_version(release.get("title", "")):
        score -= 60

    return score


def _musicbrainz_first_year(song_title: str, artist: str, scan_for: str) -> Optional[int]:
    releases = _mb_search_releases(song_title, artist, scan_for=scan_for)
    if not releases:
        return None

    want_title_norm = _norm_title(song_title)
    want_artist_norm = _norm_artist(artist)
    ranked = sorted(
        releases,
        key=lambda release: _mb_release_quality_score(
            release, want_title_norm, want_artist_norm
        ),
        reverse=True,
    )

    years: List[int] = []
    for release in ranked:
        artist_norm = _norm_artist(_mb_release_artist_str(release))
        if artist_norm and want_artist_norm not in artist_norm and artist_norm not in want_artist_norm:
            continue
        year = _extract_year(release.get("date"))
        if year:
            years.append(year)

    return min(years) if years else None


def _discogs_search(
    song_title: str,
    artist: str,
    scan_for: str,
    per_page: int = 25,
) -> List[dict]:
    headers = {"User-Agent": _USER_AGENT}
    if _DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={_DISCOGS_TOKEN}"

    params = {
        "type": "master" if scan_for == "album" else "release",
        "artist": artist,
        "track": song_title if scan_for == "single" else None,
        "release_title": song_title if scan_for == "album" else None,
        "q": song_title,
        "per_page": per_page,
        "page": 1,
    }
    params = {key: value for key, value in params.items() if value is not None}
    data = _http_get_json(f"{_DISCOGS_BASE}/database/search", headers=headers, params=params)
    return data.get("results") or []


def _discogs_release_is_bad(release: dict) -> bool:
    if (release.get("status") or "").lower() in _BAD_SECONDARY_TYPES:
        return True
    for fmt in release.get("format") or []:
        if str(fmt).lower() in _BAD_SECONDARY_TYPES:
            return True
    return False


def _discogs_first_year(song_title: str, artist: str, scan_for: str) -> Optional[int]:
    want_title_norm = _norm_title(song_title)
    want_artist_norm = _norm_artist(artist)
    results = _discogs_search(song_title, artist, scan_for=scan_for)

    years: List[int] = []
    for item in results:
        if _discogs_release_is_bad(item):
            continue

        title_norm = _norm_title(item.get("title") or "")
        if want_title_norm not in title_norm and title_norm not in want_title_norm:
            continue

        artist_name = item.get("artist") or ""
        artist_norm = _norm_artist(artist_name)
        if artist_norm and want_artist_norm not in artist_norm and artist_norm not in want_artist_norm:
            continue

        year = _extract_year(item.get("year"))
        if year:
            years.append(year)

    return min(years) if years else None


def first_release_year(artist: str, song_title: str, scan_for: str = "single") -> Optional[int]:
    mb_year = None
    dc_year = None

    try:
        mb_year = _musicbrainz_first_year(song_title, artist, scan_for=scan_for)
    except requests.RequestException:
        mb_year = None

    try:
        dc_year = _discogs_first_year(song_title, artist, scan_for=scan_for)
    except requests.RequestException:
        dc_year = None

    years = [year for year in (mb_year, dc_year) if isinstance(year, int)]
    return min(years) if years else None


class ReleaseYearApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Original Release Year")
        self.root.resizable(False, False)

        self.scan_for = tk.StringVar(value="single")
        self.artist_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.result_var = tk.StringVar(value="Enter an artist and title to start.")

        self._build_menu()
        self._build_layout()

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self.root)
        scan_menu = tk.Menu(menu_bar, tearoff=False)
        scan_menu.add_radiobutton(
            label="Singles",
            variable=self.scan_for,
            value="single",
            command=self._update_mode_label,
        )
        scan_menu.add_radiobutton(
            label="Albums",
            variable=self.scan_for,
            value="album",
            command=self._update_mode_label,
        )
        menu_bar.add_cascade(label="Scan for", menu=scan_menu)
        self.root.config(menu=menu_bar)

    def _build_layout(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frame, text="Artist").grid(row=0, column=0, sticky="w", pady=(0, 6))
        artist_entry = ttk.Entry(frame, textvariable=self.artist_var, width=38)
        artist_entry.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(frame, text="Title").grid(row=2, column=0, sticky="w", pady=(0, 6))
        title_entry = ttk.Entry(frame, textvariable=self.title_var, width=38)
        title_entry.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.mode_label = ttk.Label(frame, text="Current mode: Singles")
        self.mode_label.grid(row=4, column=0, sticky="w", pady=(0, 10))

        self.lookup_button = ttk.Button(frame, text="Find original year", command=self.lookup_year)
        self.lookup_button.grid(row=5, column=0, sticky="ew", pady=(0, 12))

        ttk.Label(frame, textvariable=self.result_var, wraplength=300).grid(
            row=6, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Label(frame, textvariable=self.status_var, foreground="#555555").grid(
            row=7, column=0, sticky="w"
        )

        artist_entry.focus()
        self.root.bind("<Return>", lambda _event: self.lookup_year())

    def _update_mode_label(self) -> None:
        label = "Albums" if self.scan_for.get() == "album" else "Singles"
        self.mode_label.config(text=f"Current mode: {label}")

    def lookup_year(self) -> None:
        artist = self.artist_var.get().strip()
        title = self.title_var.get().strip()

        if not artist or not title:
            messagebox.showerror("Missing data", "Please enter both artist and title.")
            return

        self.lookup_button.config(state="disabled")
        self.status_var.set("Searching MusicBrainz and Discogs...")
        self.result_var.set("Working...")

        worker = threading.Thread(
            target=self._lookup_year_worker,
            args=(artist, title, self.scan_for.get()),
            daemon=True,
        )
        worker.start()

    def _lookup_year_worker(self, artist: str, title: str, scan_for: str) -> None:
        try:
            year = first_release_year(artist, title, scan_for=scan_for)
            self.root.after(0, lambda: self._handle_lookup_success(artist, title, scan_for, year))
        except Exception as exc:
            self.root.after(0, lambda: self._handle_lookup_error(str(exc)))

    def _handle_lookup_success(
        self,
        artist: str,
        title: str,
        scan_for: str,
        year: Optional[int],
    ) -> None:
        mode_label = "album" if scan_for == "album" else "single"
        if year is None:
            self.result_var.set(f'No original {mode_label} year found for "{title}" by {artist}.')
        else:
            self.result_var.set(
                f'The earliest {mode_label} year for "{title}" by {artist} is {year}.'
            )
        self.status_var.set("Finished")
        self.lookup_button.config(state="normal")

    def _handle_lookup_error(self, error_message: str) -> None:
        self.result_var.set("Lookup failed.")
        self.status_var.set(error_message or "An unexpected error occurred.")
        self.lookup_button.config(state="normal")


def main() -> None:
    root = tk.Tk()
    ReleaseYearApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
