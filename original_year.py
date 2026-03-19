from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from typing import List, Optional
from mediafile import MediaFile
from wiki import get_song_release_date
import requests


_MB_BASE = "https://musicbrainz.org/ws/2"
_DISCOGS_BASE = "https://api.discogs.com"
_DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN")
_USER_AGENT = "FirstReleaseYearLookup/2.0 (contact: ecog@outlook.de)"
_AUDIO_FILES = ("mp3", "flac", "wav", "aac", "ogg", "m4a", "opus", "alac", "aiff", "dsd", "pcm")


# ------------------------------------
# Normalization / filtering helpers
# ------------------------------------

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

# Secondary types to avoid when looking for "first release"
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
    value = re.sub(r"^the\s+", "", value)  # treat "The Beach Boys" ~ "Beach Boys"
    value = re.sub(r"[^\w\s]", "", value)  # drop punctuation
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _norm_title(value: str) -> str:
    value = value.strip().lower()
    value = _TITLE_NOISE_RE.sub(" ", value)  # drop parenthetical qualifiers
    value = value.replace("&", "and")
    value = re.sub(r"[’']", "", value)  # normalize apostrophes away (I'm -> Im)
    value = re.sub(
        r"[^\w\s]", " ", value
    )  # punctuation -> space (What's Up? -> Whats Up)
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


# ----------------------------
#       MusicBrainz
# ----------------------------


def _mb_search_releases(
    song_title: str,
    artist: str,
    file_mode: str,
    limit: int = 25,
) -> List[dict]:
    headers = {"User-Agent": _USER_AGENT}
    release_type = "album" if file_mode == "album" else "single"
    query = (
        f'release:"{_norm_title(song_title)}" AND artist:"{_norm_artist(artist)}" AND status:"official" '
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


def _musicbrainz_first_year(song_title: str, artist: str, file_mode: str) -> Optional[int]:
    releases = _mb_search_releases(song_title, artist, file_mode=file_mode)
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
    file_mode: str,
    per_page: int = 25,
) -> List[dict]:
    headers = {"User-Agent": _USER_AGENT}
    if _DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={_DISCOGS_TOKEN}"

    params = {
        "type": "master" if file_mode == "album" else "release",
        "artist": artist,
        "track": song_title if file_mode == "single" else None,
        "release_title": song_title if file_mode == "album" else None,
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


def _discogs_first_year(song_title: str, artist: str, file_mode: str) -> Optional[int]:
    want_title_norm = _norm_title(song_title)
    want_artist_norm = _norm_artist(artist)
    results = _discogs_search(want_title_norm, want_artist_norm, file_mode=file_mode)

    years: List[int] = []
    for item in results:
        if _discogs_release_is_bad(item):
            continue

        if file_mode == "album":
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


def fetch_musicbrainz_year(artist, title):
    if not artist or not title:
        return ""
    query = (
        f'artist:"{artist}" AND recording:"{title}" AND NOT release-group:compilation'
    )
    url = f"{MUSICBRAINZ_RECORDING_URL}?query={quote(query)}&fmt=json&inc=releases"
    time.sleep(1)  # To respect MusicBrainz rate limiting
    for attempt in range(2):
        try:
            request = Request(url, headers={"User-Agent": MUSICBRAINZ_USER_AGENT})
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except Exception as e:
            is_reset = (
                getattr(e, "winerror", None) == 10054
                or "[WinError 10054]" in str(e)
            )
            is_503 = "HTTP Error 503: Service Temporarily Unavailable" in str(e)
            if (is_reset or is_503) and attempt == 0:
                print(str(e))
                print("Retrying MusicBrainz lookup after a short delay...")
                if is_503:
                    time.sleep(1)
                else:
                    time.sleep(0.5)
                continue
            print(f"Warning: MusicBrainz lookup failed for {artist} - {title}: {e}")
            return ""

    best_year = ""
    for recording in data.get("recordings", []):
        release = recording.get("releases", [])
        if not release:
            continue
        release_group = release[0].get("release-group", [])
        secondary_types = release_group.get("secondary-types", [])
        for secondary_type in secondary_types:
            if secondary_type in ["Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo"]:
                continue

        year = _extract_year(recording.get("first-release-date", ""))
        if not year:
            for release in recording.get("releases", []) or []:
                year = _extract_year(release.get("date", ""))
                if year:
                    break
        if year and (not best_year or year < best_year):
            best_year = year
    return best_year


def first_release_year(artist: str, song_title: str, file_mode: str) -> Optional[int]:
    mb_year = None
    dc_year = None

    try:
        mb_year = _musicbrainz_first_year(song_title, artist, file_mode=file_mode)
    except requests.RequestException:
        mb_year = None

    try:
        dc_year = _discogs_first_year(song_title, artist, file_mode=file_mode)
    except requests.RequestException:
        dc_year = None

    years = [year for year in (mb_year, dc_year) if isinstance(year, int)]
    if years:
        print(f"Found years: {years} (MusicBrainz: {mb_year}, Discogs: {dc_year})")
        return min(years)
    else:
        wikipedia_date = get_song_release_date(song_title.strip(), artist.strip())
        if wikipedia_date:
            min_year = _extract_year(wikipedia_date)
            return
        else:
           return None


class ReleaseYearApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Original Release Year")
        self.root.resizable(False, False)

        self.file_mode = tk.StringVar(value="single")
        self.artist_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.result_var = tk.StringVar(value="Enter an artist and title to start.")

        self._build_menu()
        self._build_layout()

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self.root)
        mode_menu = tk.Menu(menu_bar, tearoff=False)
        mode_menu.add_radiobutton(
            label="Singles",
            variable=self.file_mode,
            value="single",
            command=self._update_mode_label,
        )
        mode_menu.add_radiobutton(
            label="Albums",
            variable=self.file_mode,
            value="album",
            command=self._update_mode_label,
        )
        menu_bar.add_cascade(label="Mode", menu=mode_menu)

        select_menu = tk.Menu(menu_bar, tearoff=False)
        select_menu.add_command(label="File", command=self.open_file)
        select_menu.add_command(label="Folder",command=self.open_folder)
        menu_bar.add_cascade(label="Open", menu=select_menu)

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
        label = "Albums" if self.file_mode.get() == "album" else "Singles"
        self.mode_label.config(text=f"Current mode: {label}")

    def get_basic_metadata(self, file_path):
        song_title = os.path.basename(file_path)
        artist = "Unknown Artist"
        album = "Unknown Album"
        try:
            file = MediaFile(file_path)
            if file is None:
                self.status_var.showMessage(
                    f"Could not read audio file: {file_path}. Make sure the file exists."
                )
                return None

            # Get basic metadata
            song_title = file.title
            artist = file.artist
            album = file.album

        except Exception as e:
            self.status_var.showMessage(
                f"Error extracting metadata from {file_path}: {str(e)}"
            )

        #        self.status_var.showMessage(f"{artist} - {song_title} ({album})")
        return artist, song_title, album

    def open_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Choose File to Open",
            filetypes=[("Audio Files", _AUDIO_FILES)],
        )
        if file_path:
            extension = file_path.split('.')[-1].casefold()
            if extension in _AUDIO_FILES:
                self.status_var.set(f"Selected file: {file_path}")
                metadata = self.get_basic_metadata(file_path)
                if metadata:
                    artist, song_title, _ = metadata
                    self.artist_var.set(artist)
                    self.title_var.set(song_title)
                    self._lookup_year_worker(
                        artist, song_title, file_mode=self.file_mode.get()
                    )
        else:
            self.status_var.set("No file selected.")

    def open_folder(self) -> None:
        directory = filedialog.askdirectory(
            title="Choose Folder to Open", mustexist=True
        )
        if directory == "":
            directory = None
        if directory:
            self.status_var.set(f"Selected folder: {directory}")
            audio_files = []
            for root, _, files in os.walk(directory):
                for file in files:
                    if file.split('.')[-1].casefold() in _AUDIO_FILES:
                        audio_files.append(os.path.join(root, file))
            if not audio_files:
                messagebox.showinfo("No audio files", "No audio files found in the selected folder.")
                self.status_var.set("Ready")
                return
            self.status_var.set(f"Found {len(audio_files)} audio files. Processing...")
            albums = {}            
            for file_path in audio_files:
                metadata = self.get_basic_metadata(file_path)
                if metadata:
                    artist, song_title, album = metadata
                    if album:
                        if album not in albums:
                            albums[album] = [(artist, song_title)]
                        else:
                            song = (artist, song_title)
                            albums[album].append(song)
            if not albums:
                messagebox.showinfo("No album data", "No album metadata found in the audio files.")
                self.status_var.set("Ready")
                return
            for album in albums:
                album_data = set()
                for i in range(len(albums[album])):
                        song = albums[album][i]
                        album_data.add(song[0]) # artist
                if len(album_data) > 1:
                    self.status_var.set(f"Multiple artists found for album '{album}'. Skipping.")
                    continue
                if not album_data:
                    self.status_var.set(f"No artist data found for album '{album}'. Skipping.")
                    continue
                artist = album_data.pop()
                self._lookup_year_worker(
                    artist, album, file_mode=self.file_mode.get()
                )
            return

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
            args=(artist, title, self.file_mode.get()),
            daemon=True,
        )
        worker.start()

    def _lookup_year_worker(
        self, artist: str, title: str, file_mode: str) -> None:
        try:
            year = first_release_year(artist, title, file_mode=file_mode)
            self.root.after(0, lambda: self._handle_lookup_success(artist, title, file_mode, year))
        except Exception as exc:
            self.root.after(0, lambda: self._handle_lookup_error(str(exc)))

    def _handle_lookup_success(
        self,
        artist: str,
        title: str,
        file_mode: str,
        year: Optional[int],
    ) -> None:
        mode_label = "album" if file_mode == "album" else "single"
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
