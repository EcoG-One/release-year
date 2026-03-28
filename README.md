# Original Release Year

`original_year.py` is a small Tkinter desktop app that looks up the earliest plausible release year for a song or album and can write that year back into audio file metadata when it finds an earlier value.

It supports:

- Single-file lookup
- Folder lookup
- Song mode and album mode
- MusicBrainz, Discogs, and Wikipedia as lookup sources
- Updating local audio file year tags when a better year is found
- Persisting the Discogs token in `.env` after the first successful authentication

## How It Works

The app reads artist, title, and album metadata from audio files with `mediafile`, then searches one or more external sources:

- `MusicBrainz`
- `Discogs`
- `Wikipedia`

It filters noisy results such as remasters, live versions, compilations, promos, and unofficial releases, then chooses the earliest plausible year returned by the enabled sources.

If the discovered year is earlier than the current file metadata year, the app updates the file.

## Requirements

- Python 3
- A GUI-capable environment for Tkinter
- Internet access for external metadata lookups

Install dependencies:

```powershell
pip install -r requirements.txt
```

## Configuration

The script expects a `.env` file in the project root.

Example:

```env
Consumer_Key = "your_discogs_consumer_key"
Consumer_Secret = "your_discogs_consumer_secret"
DISCOGS_TOKEN = ""
```

Notes:

- `Consumer_Key` and `Consumer_Secret` are used for Discogs OAuth.
- `DISCOGS_TOKEN` is optional on first run.
- When the app authenticates with Discogs successfully, it saves the returned token into `.env` and reuses it on later runs.

## Running

Start the app with:

```powershell
python original_year.py
```

## Using The App

1. Launch the app.
2. Choose the lookup mode from the `Mode` menu:
   `Singles` or `Albums`.
3. Enable one or more lookup sources from the `Source` menu.
4. Choose `Open > File` to process a single audio file, or `Open > Folder` to process a folder.
5. Review results in the list shown in the main window.

## Metadata Behavior

- For a single file, the app reads the embedded metadata and looks up the release year for that track.
- For album mode, the app treats the selected title as an album and applies the discovered year to audio files in that album folder.
- Existing metadata is only updated when the discovered year is earlier than the stored year.

## Discogs Authentication

If no saved Discogs token is available and Discogs is enabled:

1. The app opens the Discogs authorization page in your browser.
2. You paste the verification code into the Tkinter dialog.
3. The returned token is saved to `.env`.

On the next run, the saved token is loaded automatically, so the authorization step does not need to be repeated.

## Known Limitations

- The script reads `.env` directly; it does not use `python-dotenv`.
- Folder processing depends on the quality of the embedded artist, title, and album metadata.
- Network errors or missing metadata can cause a lookup to fail for some files.

## Project Files

- `original_year.py`: main GUI application
- `musicbrainz.py`: MusicBrainz lookup logic
- `wikipedia.py`: Wikipedia lookup logic
- `.env`: local credentials and saved Discogs token

