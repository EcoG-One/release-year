# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`original_year.py` is a Tkinter desktop app that finds the earliest plausible release year for a song or album and can write that year back into audio-file metadata when it finds an earlier value than the file already has. Lookups run against MusicBrainz, Discogs, and Wikipedia; the user can enable any combination from a `Source` menu and choose between `Singles` and `Albums` modes.

The three lookup modules each expose a single public function with the same shape: `get_first_release_year_<source>(title, artist, title_type) -> int | None`, where `title_type` is `"single"` or `"album"`. The GUI in `original_year.py` collects candidates from the enabled sources and returns `min()` of the integer years (or `None`). So adding a new source is a matter of writing a module that matches that signature and wiring it into the GUI's source checkboxes + the aggregator.

## Key files

- `original_year.py` — Tkinter GUI, Discogs client + rate limiting, metadata writing via `mediafile`. The main entry point (`python original_year.py`).
- `musicbrainz.py` — MusicBrainz search/browse with retry + exponential backoff and a 1.1s-per-request politeness delay. Has a `__main__` smoke test.
- `wikipedia.py` — Wikipedia API client that scores search candidates and parses infobox `released` / `release_date` fields. Has a `__main__` smoke test.
- `wiki.py` — Older, simpler Wikipedia scraper. **Not imported by `original_year.py`** — it is legacy/dead code kept around; do not extend it.
- `discogs_reg.py` — Standalone CLI that runs the Discogs OAuth handshake. Not imported by the app; only useful for debugging token issues outside the GUI.
- `original_year.spec` — PyInstaller spec. Note the produced binary is named `release_year` (not `original_year`), and `dist/release_year.exe` is the build output.

## Filtering heuristics (shared across all three modules)

Each source module applies the same two filters to reject non-original releases before the year is considered. They are defined as `_BAD_VERSION_RE` (regex of words like `live`, `remaster`, `demo`, `remix`, `cover`, `acoustic`, `reissue`, …) and `_BAD_SECONDARY_TYPES` (set including `compilation`, `live`, `remix`, `dj-mix`, `demo`, `bootleg`, `promo`, …). When tuning the "earliest year" result, edit the regex/set in all three modules together so they stay consistent — a candidate the MusicBrainz filter rejects is one the Discogs/Wikipedia modules should also reject.

## Threading model

`_lookup_year_worker` in `original_year.py` runs the network calls in a `threading.Thread(daemon=True)` and posts results back to Tk via `self.root.after(0, …)`. Never touch Tk widgets from the worker thread directly — only call back through `root.after`.

## Discogs specifics

- The Discogs rate limiter is a process-global burst counter (`_DISCOGS_BURST_LIMIT = 60` requests, then sleep 60s) guarded by `_discogs_rate_lock`. The burst threshold drops to 25 if the user runs without a token.
- OAuth tokens are persisted to `Path.home() / ".FirstReleaseYear" / ".env"`, **not** the project-root `.env`. The README incorrectly implies the project `.env` is used; only `Consumer_Key` / `Consumer_Secret` come from the environment for the OAuth handshake, and the user's shell / OS env is what supplies them (the project `.env` file is not loaded by `python-dotenv` or otherwise).
- An HTTP 401 from Discogs is re-raised with a custom message; if it appears, the saved `DISCOGS_TOKEN` / `DISCOGS_SECRET` in `~/.FirstReleaseYear/.env` is almost certainly stale and needs to be deleted to force re-auth.

## Configuration

The project-root `.env` file is the place for `Consumer_Key` and `Consumer_Secret` and is gitignored. The file is read by hand from the OS environment, not by the script. The repo currently has a `.env` checked in despite `.gitignore` listing it — treat any token values in that file as compromised if the repo has ever been public, and rotate them.

## Commands

Install:
```powershell
pip install -r requirements.txt
```

Run the GUI (requires a display):
```powershell
python original_year.py
```

Smoke-test the lookup modules in isolation (no GUI, no audio files needed):
```powershell
python musicbrainz.py
python wikipedia.py
```
Each prints a table of `Artist – Title → got vs expected` against a small hardcoded test set. Use these to validate changes to filtering or normalization logic before touching the GUI.

Build the Windows executable:
```powershell
pyinstaller original_year.spec
```
Output appears at `dist/release_year.exe` (note: spec file uses the name `release_year` even though the source is `original_year.py`).

## Supported audio formats

Defined by the `_AUDIO_FILES` tuple at the top of `original_year.py`. When adding a new format, add the extension there in addition to wherever `mediafile` may need it.
