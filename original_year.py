from __future__ import annotations

# from logging import root
from logging import root
from logging import root
import os
import re
import time
import threading
import tkinter as tk
from tkinter import Entry, messagebox, ttk, filedialog, Label, Button
from typing import List, Optional
from mediafile import MediaFile
from musicbrainz import get_first_release_year_mb
from wikipedia import get_first_release_year_wp
import requests
import webbrowser
import discogs_client
from discogs_client.exceptions import HTTPError


_DISCOGS_BASE = "https://api.discogs.com"
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
_USER_AGENT = "FirstReleaseYearLookup/2.0 (contact: ecog@outlook.de)"
_AUDIO_FILES = (
    "mp3",
    "flac",
    "wav",
    "aac",
    "ogg",
    "m4a",
    "opus",
    "alac",
    "aiff",
    "dsd",
    "pcm",
)

_DISCOGS_BURST_LIMIT = 60
_DISCOGS_PAUSE_SECONDS = 60


_discogs_rate_lock = threading.Lock()
_discogs_request_count = 0


def _read_env_value(key: str) -> Optional[str]:
    if not os.path.exists(_ENV_PATH):
        return None
    with open(_ENV_PATH, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            current_key, value = line.split("=", 1)
            if current_key.strip() != key:
                continue
            return value.strip().strip('"').strip("'")
    return None


def _save_env_value(key: str, value: str) -> None:
    lines: List[str] = []
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r", encoding="utf-8") as env_file:
            lines = env_file.readlines()

    updated = False
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        current_key = stripped.split("=", 1)[0].strip()
        if current_key == key:
            lines[index] = f'{key} = "{value}"\n'
            updated = True
            break

    if not updated:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = f"{lines[-1]}\n"
        lines.append(f'{key} = "{value}"\n')

    with open(_ENV_PATH, "w", encoding="utf-8") as env_file:
        env_file.writelines(lines)

    os.environ[key] = value


_DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN") or _read_env_value("DISCOGS_TOKEN")


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


def _extract_year(date_str: str) -> Optional[int]:
    if not date_str:
        return None
    match = re.match(r"^\s*(\d{4})", str(date_str))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2100 else None


def _coerce_media_year(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1900 <= value <= 2100 else None
    if isinstance(value, str):
        return _extract_year(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            year = _coerce_media_year(item)
            if year is not None:
                return year
    return _extract_year(str(value))


def _http_get_json(
    url: str,
    *,
    headers: dict,
    params: Optional[dict] = None,
    timeout: int = 25,
    mb: bool = None,
) -> dict:
    global _discogs_request_count

    if not mb:
        with _discogs_rate_lock:
            if _discogs_request_count >= _DISCOGS_BURST_LIMIT:
                print(
                    f"Discogs request limit reached. Sleeping for {_DISCOGS_PAUSE_SECONDS} seconds..."
                )
                time.sleep(_DISCOGS_PAUSE_SECONDS)
                _discogs_request_count = 0
            _discogs_request_count += 1

    response = requests.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    """headers = response.headers
    if "x-ratelimit-remaining" in headers and headers["x-ratelimit-remaining"] == "0":
        reset_time = int(headers.get('x-discogs-ratelimit-reset', '60'))
        print(f"Discogs rate limit hit. Sleeping for {reset_time} seconds...")
        time.sleep(reset_time + 1)  # Sleep a bit longer to be safe """
    return response.json()

    # ----------------------------
    #           Discogs
    # ----------------------------

def _discogs_authenticate() -> Optional[str]:
    consumer_key = os.environ.get("Consumer_Key")
    consumer_secret = os.environ.get("Consumer_Secret")
    user_agent = "FirstReleaseYearLookup/2.0 (contact: ecog@outlook.de)"
    code_var = tk.StringVar()
    token = None

    def enter_code():
        nonlocal token
        oauth_verifier = code_var.get().strip()
        if not oauth_verifier or oauth_verifier.isspace():
            auth_window.destroy()
            messagebox.showerror("Input Error", "Verification code is required for authentication.")
            return
        auth_window.destroy()
        try:
            access_token, access_secret = discogsclient.get_access_token(oauth_verifier)
            token = access_token
            _save_env_value("DISCOGS_TOKEN", access_token)
        except HTTPError:
            messagebox.showerror("Authentication Error", "Unable to authenticate.")
            return
        # fetch the identity object for the current logged in user.

        user = discogsclient.identity()

        print()
        print(" == User ==")
        print(f"    * username           = {user.username}")
        print(f"    * name               = {user.name}")
        print(" == Access Token ==")
        print(f"    * oauth_token        = {access_token}")
        print(f"    * oauth_token_secret = {access_secret}")
        print(" Authentication complete. Future requests will be signed with the above tokens.")
        return token

    # instantiate our discogs_client object.
    discogsclient = discogs_client.Client(user_agent)

    # prepare the client with our API consumer data.
    discogsclient.set_consumer_key(consumer_key, consumer_secret)
    token, secret, url = discogsclient.get_authorize_url()
    print(" == Request Token == ")
    if os.environ.get(f"WERKZEUG_RUN_MAIN") is None:
        webbrowser.open(url)
    auth_window = tk.Toplevel()
    auth_window.geometry("400x70")
    auth_window.title("Discogs Authentication")
    Label(auth_window, text="Enter Verification code : ").grid(row=0)
    Entry(auth_window, textvariable = code_var, font=('calibre',10,'normal'), width=20).grid(row=0, column=1)
    Button(auth_window, text="Enter", command=enter_code).grid(row=1, column=1)
    auth_window.wait_window()
    return token


def _discogs_search(
    song_title: str,
    artist: str,
    file_mode: str,
    mb: bool,
) -> List[dict]:
    headers = {"User-Agent": _USER_AGENT}
    global _DISCOGS_TOKEN
    if _DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={_DISCOGS_TOKEN}"
    else:
        try:
            if messagebox.askquestion(
                "No Discogs token",
                "You may be subject to very low rate limits. Click YES to set a token for better performance."
            ) == "yes":
                _DISCOGS_TOKEN = _discogs_authenticate()
                if _DISCOGS_TOKEN:
                    headers["Authorization"] = f"Discogs token={_DISCOGS_TOKEN}"
                    print(_DISCOGS_TOKEN)
                else:
                    _DISCOGS_BURST_LIMIT = 25
            else:
                _DISCOGS_BURST_LIMIT = 25
                messagebox.showwarning(
                    "Proceeding without token",
                    "You may be subject to very low rate limits. Consider setting a Discogs token for better performance."
                )
        except Exception as e:
            messagebox.showerror("Authentication Failed", f"Discogs authentication failed: {str(e)}. Proceeding without token.")
            _DISCOGS_BURST_LIMIT = 25

    params = {
        "type": "master" if file_mode == "album" else "release",
        "artist": _norm_artist(artist),
        "track": _norm_title(song_title) if file_mode == "single" else None,
        "release_title": _norm_title(song_title) if file_mode == "album" else None,
        "q": _norm_title(song_title) if file_mode == "single" else None,
        "per_page": 25,
        "page": 1,
    }
    params = {key: value for key, value in params.items() if value is not None}
    data = _http_get_json(
        f"{_DISCOGS_BASE}/database/search",
        headers=headers,
        params=params,
        mb=mb,
    )
    return data.get("results") or []


def _discogs_release_is_bad(release: dict) -> bool:
    # Skip unofficial; prefer avoiding compilations when possible, but some original releases
    #  are compilations so only filter those if explicitly tagged as such via secondary type or status
    if (release.get("status") or "").lower() in _BAD_SECONDARY_TYPES:
        return True
    for format in release.get("format") or []:
        if str(format).lower() in _BAD_SECONDARY_TYPES:
            return True
    return False


def _discogs_first_year(song_title: str, artist: str, file_mode: str, mb: bool) -> Optional[int]:
    want_title_norm = _norm_title(song_title)
    want_artist_norm = _norm_artist(artist)
    results = _discogs_search(song_title, artist, file_mode=file_mode, mb=mb)

    years: List[int] = []
    # Inspect a handful of the best-looking results more closely, and take
    #  the earliest year among those that look good
    for item in results:
        if _discogs_release_is_bad(item):
            continue

        if file_mode == "album":
            title_norm = _norm_title(item.get("title") or "")
            if want_title_norm not in title_norm and title_norm not in want_title_norm:
                continue

        artist_name = item.get("artist") or ""
        artist_norm = _norm_artist(artist_name)
        if (
            artist_norm
            and want_artist_norm not in artist_norm
            and artist_norm not in want_artist_norm
        ):
            continue

        year = _extract_year(item.get("year"))

        if not year:
            continue

        # Prefer non-compilation if possible
        formats = item.get("formats") or []
        is_compilation = any(
            "compilation" in " ".join((f.get("descriptions") or [])).lower()
            for f in formats
        )

        if year and not is_compilation:
            years.append(year)

    if file_mode == "single" and not years:
        _discogs_first_year(song_title, artist, file_mode="album", mb=mb)

    return min(years) if years else None


# ----------------------------
#       Public function
# ----------------------------


def first_release_year(
    artist: str, song_title: str, file_mode: str, mb: bool, dc: bool, wp: bool
) -> Optional[int]:
    """
    Returns earliest plausible release year found across MusicBrainz + Discogs, with heuristics
    to reduce false positives (covers/live/remasters/etc.). If not found, tries Wikipedia.Returns None if not found.

    """
    dc_year = None
    wp_year = None

    if dc:
        try:
            print("Searching Discogs...")
            dc_year = _discogs_first_year(song_title, artist, file_mode=file_mode, mb=mb)
            if dc_year:
                print(
                    f"{artist} - {song_title}. Found release year: {dc_year} in Discogs."
                )
            else:
                print(
                    f"Could not find a release year for {artist} - {song_title} in Discogs."
                )
        except requests.RequestException:
            dc_year = None

    if wp:
        try:
            print("Searching Wikipedia...")
            wp_year = get_first_release_year_wp(song_title, artist, file_mode)
            if wp_year:
                print(f"{artist} - {song_title}. Found release year: {wp_year} in Wikipedia.")
            else:
                print(f"Could not find a release year for {artist} - {song_title} in Wikipedia.")
        except requests.RequestException:
            wp_year = None

    years = [year for year in (dc_year, wp_year) if isinstance(year, int)]
    if years:
        return min(years)
    else:
        return None


# ----------------------------
#       GUI Application
# ----------------------------


class ReleaseYearApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Original Release Year")
        self.root.resizable(False, True)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.file_mode = tk.StringVar(value="single")
        self.source_mb = tk.BooleanVar(value=False)
        self.source_dc = tk.BooleanVar(value=True)
        self.source_wp = tk.BooleanVar(value=False)
        self.artist_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.file_path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.result_var = tk.StringVar(value="Select a file or folder to search.")

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

        source_menu = tk.Menu(menu_bar, tearoff=False)
        source_menu.add_checkbutton(
            label="MusicBrainz",
            variable=self.source_mb,
            command=self._update_source_label,
        )
        source_menu.add_checkbutton(
            label="Discogs",
            variable=self.source_dc,
            command=self._update_source_label,
        )
        source_menu.add_checkbutton(
            label="Wikipedia",
            variable=self.source_wp,
            command=self._update_source_label,
        )
        menu_bar.add_cascade(label="Source", menu=source_menu)

        select_menu = tk.Menu(menu_bar, tearoff=False)
        select_menu.add_command(label="File", command=self.open_file)
        select_menu.add_command(label="Folder", command=self.open_folder)
        menu_bar.add_cascade(label="Open", menu=select_menu)

        self.root.config(menu=menu_bar)

    def _build_layout(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(row=0, column=0, sticky="new")

        ttk.Label(frame, text="Results:").grid(row=0, column=0, sticky="w", pady=(0, 6))
        scrollbar = tk.Scrollbar(frame, orient="vertical")
        self.display = tk.Listbox(
            frame, height=10, width=150, yscrollcommand=scrollbar.set
        )
        scrollbar.config(command=self.display.yview)

        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        scrollbar.grid(row=1, column=1, sticky="ns")
        self.display.grid(row=1, column=0, sticky="nsew", pady=(0, 10))

        self.mode_label = ttk.Label(frame, text="Current mode: Singles")
        self.mode_label.grid(row=2, column=0, sticky="w", pady=(0, 10))

        self.source_label = ttk.Label(frame, text="Current source: Discogs")
        self.source_label.grid(row=3, column=0, sticky="w", pady=(0, 10))

        ttk.Label(frame, textvariable=self.result_var, wraplength=300).grid(
            row=4, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Label(frame, textvariable=self.status_var, foreground="#555555").grid(
            row=5, column=0, sticky="w"
        )

        self.display.focus()
        self.root.bind("<Return>", lambda _event: self.lookup_year())

    def _update_mode_label(self) -> None:
        label = "Albums" if self.file_mode.get() == "album" else "Singles"
        self.mode_label.config(text=f"Current mode: {label}")

    def _update_source_label(self) -> None:
        sources = []
        if self.source_mb.get():
            sources.append("MusicBrainz")
        if self.source_dc.get():
            sources.append("Discogs")
        if self.source_wp.get():
            sources.append("Wikipedia")
        label = ", ".join(sources) if sources else "None"
        if len(sources) > 1:
            self.source_label.config(text=f"Current sources: {label}")
        else:
            self.source_label.config(text=f"Current source: {label}")



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

    def _update_file_year_if_earlier(
        self, file_path: str, worker_year: Optional[int]
    ) -> Optional[int]:
        if worker_year is None:
            return None
        file = MediaFile(file_path)
        metadata_year = _coerce_media_year(getattr(file, "year", None))
        if metadata_year is None or worker_year >= metadata_year:
            return metadata_year
        file.year = worker_year
        file.save()
        return metadata_year

    def open_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Choose File to Open",
            filetypes=[("Audio Files", _AUDIO_FILES)],
        )
        if file_path:
            extension = file_path.split(".")[-1].casefold()
            if extension in _AUDIO_FILES:
                self.status_var.set(f"Selected file: {file_path}")
                metadata = self.get_basic_metadata(file_path)
                if metadata:
                    artist, song_title, _ = metadata
                    self.artist_var.set(artist)
                    self.title_var.set(song_title)
                    self.file_path_var.set(file_path)
                    self.lookup_year()
                    self.result_var.set("Finished! Select a file or folder for a new search.")
                    self.status_var.set("Ready!")
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
            self.result_var.set("Working...")
            audio_files = []
            for root, _, files in os.walk(directory):
                for file in files:
                    if file.split(".")[-1].casefold() in _AUDIO_FILES:
                        audio_files.append(os.path.join(root, file))
            if not audio_files:
                messagebox.showinfo(
                    "No audio files", "No audio files found in the selected folder."
                )
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
                            albums[album] = [(artist, song_title, file_path)]
                        else:
                            song = (artist, song_title, file_path)
                            albums[album].append(song)
            if not albums:
                messagebox.showinfo(
                    "No album data", "No album metadata found in the audio files."
                )
                self.status_var.set("Ready")
                return
            folder_artists = set()
            for album in albums:
                for song in albums[album]:
                    folder_artists.add(song[0])  # collect unique artists
            if not folder_artists:
                self.status_var.set(
                    f"No artist data found for album '{album}'. Skipping."
                )
                return
            if len(folder_artists) > 1:
                self.status_var.set(f"Multiple artists found")
                for album in albums:
                    for song in albums[album]:
                        self.artist_var.set(song[0])
                        self.title_var.set(song[1])
                        self.file_path_var.set(song[2])
                        self.file_mode.set("single")
                        if self.source_mb.get():
                            try:
                                print("Searching MusicBrainz...")
                                self._lookup_year_worker(
                                    song[0],
                                    song[1],
                                    "single",
                                    song[2],
                                )
                            except requests.RequestException:
                                mb_year = None
                        else:
                            self.lookup_year()
            else:
                album_file_path = next(iter(albums.values()))[0][2]
                self.artist_var.set(folder_artists.pop())
                self.title_var.set(album)
                self.file_path_var.set(album_file_path)
                self.file_mode.set("album")
                self.lookup_year()
        self.result_var.set("Finished! Select a file or folder for a new search.")
        self.status_var.set("Ready!")

    def lookup_year(self) -> None:
        artist = self.artist_var.get().strip()
        title = self.title_var.get().strip()

        if not artist or not title:
            messagebox.showerror("Missing data", "Please enter both artist and title.")
            return

        self.result_var.set("Working...")

        worker = threading.Thread(
            target=self._lookup_year_worker,
            args=(artist, title, self.file_mode.get(), self.file_path_var.get()),
            daemon=True,
        )
        worker.start()

    def _lookup_year_worker(
        self, artist: str, title: str, file_mode: str, file_path: Optional[str] = None
    ) -> None:
        try:
            mb=self.source_mb.get()
            dc=self.source_dc.get()
            wp=self.source_wp.get()
            file_mode=file_mode
            if mb: 
                mb_year = get_first_release_year_mb(
                    title,
                    artist,
                    file_mode,
                )
                if mb_year:
                    print(
                        f"{artist} - {title}. Found release year: {mb_year} in MusicBrainz."
                    )
                else:
                    print(f"Could not find a release year for {artist} - {title} in MusicBrainz.")
            else:
                mb_year = None
            dc_wp_year = first_release_year(
                artist,
                title,
                file_mode,
                mb,
                dc,
                wp,
            )
            year_candidates = [y for y in (mb_year, dc_wp_year) if isinstance(y, int)]
            year = min(year_candidates) if year_candidates else None
            metadata_year = None
            if file_path:
                if file_mode == "album":
                    directory = os.path.dirname(file_path)
                    for entry in os.listdir(directory):
                        entry_path = os.path.join(directory, entry)
                        if (
                            os.path.isfile(entry_path)
                            and entry.split(".")[-1].casefold() in _AUDIO_FILES
                        ):
                            self._update_file_year_if_earlier(entry_path, year)
                else:
                    metadata_year = self._update_file_year_if_earlier(file_path, year)
            self.root.after(
                0,
                lambda: self._handle_lookup_success(
                    artist,
                    title,
                    file_mode,
                    year,
                    file_path=file_path,
                    metadata_year=metadata_year,
                ),
            )
        except Exception as e:
            error_message = str(e)
            self.root.after(
                0, lambda: self._handle_lookup_error(error_message=error_message)
            )

    def _handle_lookup_success(
        self,
        artist: str,
        title: str,
        file_mode: str,
        year: Optional[int],
        file_path: Optional[str] = None,
        metadata_year: Optional[int] = None,
    ) -> None:
        mode_label = "album" if file_mode == "album" else "song"
        if year is None:
            result = f'No {mode_label} release year found for "{title}" by {artist}.'
            self.display.insert(tk.END, result + "\n")
            self.display.itemconfig(tk.END, {"foreground": "red"})
        else:
            result = (
                f'For {mode_label} "{title}" by {artist} found release year: {year}.'
            )
            if file_path and metadata_year is not None and year < metadata_year:
                result += f" Updated file metadata year from {metadata_year} to {year}."
            self.display.insert(tk.END, result + "\n")
            self.display.itemconfig(
                tk.END,
                (
                    {"foreground": "green"}
                    if file_path and metadata_year is not None and year < metadata_year
                    else {}
                ),
            )

    def _handle_lookup_error(self, error_message: str) -> None:
        self.result_var.set("Lookup failed.")
        self.status_var.set(error_message or "An unexpected error occurred.")


def main() -> None:
    root = tk.Tk()
    ReleaseYearApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
