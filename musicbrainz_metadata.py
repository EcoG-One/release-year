"""
MusicMetadata Lookup Module
Fetches and sanitizes track and album metadata from the MusicBrainz API.
Includes robust API error handling, rate limiting, and title normalization.
"""

import re
import time
import json
import logging
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List
import musicbrainzngs

# Configure structured logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# --- Configuration & Constants ---

USER_AGENT_APP = "FirstReleaseYearLookup"
USER_AGENT_VERSION = "2.0"
USER_AGENT_CONTACT = "ecog@outlook.de"

_MAX_RETRIES = 3
RETRY_DELAY = 2.0
REQUEST_DELAY = 1.2
RETRY_STATUS_CODES = {429, 502, 503, 504}

EXCLUDED_SECONDARY_TYPES = {
    "compilation", "live", "remix", "dj-mix", "demo", "bootleg",
    "promotional", "promo", "interview", "audiobook", "audio drama",
    "spokenword", "field recording", "unofficial"
}

NOISE_TOKENS = [
    "live", "acoustic", "demo", "remix", "radio edit", "extended mix",
    "instrumental", "karaoke", "remastered", "anniversary edition",
    "deluxe edition", "bonus track"
]

_NOISE_PAREN_RE = re.compile(
    r"""
    \s*[\(\[\{]\s*                # opening bracket
    [^()\[\]\{\}]*?               # contents (no nested brackets)
    \b(
        version|
        remaster(?:ed)?|
        remix|
        radio\s*edit|
        extended|
        mono|
        stereo|
        deluxe|
        bonus|
        reissue
    )\b
    [^()\[\]\{\}]*?
    \s*[\)\]\}]\s*                 # closing bracket
    """,
    re.IGNORECASE | re.VERBOSE,
)


# --- Utility Classes ---

class TitleMatcher:
    """Handles robust title matching and normalization."""

    @staticmethod
    def normalize(title: str) -> str:
        """Removes brackets, punctuation, and extra whitespace for comparison."""
        # Remove bracketed suffixes
        clean_title = re.sub(r'[\(\[].*?[\)\]]', '', title)
        # drop parenthetical noise qualifiers
        clean_title = _NOISE_PAREN_RE.sub(" ", clean_title)  
        # Remove punctuation
        # clean_title = re.sub(r'[^\w\s]', '', clean_title)
        # Normalize apostrophes to standard single quote
        clean_title = re.sub(r"[’]", "'", clean_title)
        # Normalize whitespace and lowercase
        return re.sub(r'\s+', ' ', clean_title).strip().lower()

    @classmethod
    def is_canonical(cls, title: str) -> bool:
        """Checks if a title contains 'noise' tokens indicating non-original versions."""
        lower_title = title.lower()
        return not any(token in lower_title for token in NOISE_TOKENS)

    @classmethod
    def matches(cls, target: str, candidate: str) -> bool:
        """Compares two titles accurately by normalizing them first."""
        return cls.normalize(target) == cls.normalize(candidate)

class ReleaseFilter:
    """Filters out unwanted release types."""
    
    @staticmethod
    def is_valid_release(secondary_types: List[str]) -> bool:
        """Returns True if the release is a canonical studio release."""
        sec_types_lower = {t.lower() for t in secondary_types}
        return not sec_types_lower.intersection(EXCLUDED_SECONDARY_TYPES)

# --- API Layer ---

class MusicBrainzAPI:
    """Handles communications with the MusicBrainz API securely and reliably."""

    def __init__(self):
        musicbrainzngs.set_useragent(
            USER_AGENT_APP, 
            USER_AGENT_VERSION, 
            USER_AGENT_CONTACT
        )

    def _request(self, func, *args, **kwargs) -> Dict[str, Any]:
        """A resilient request execution wrapper enforcing delays and retries."""
        time.sleep(REQUEST_DELAY)  # Enforce mandatory 1.2s delay
        
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            
            except musicbrainzngs.ResponseError as e:
                # Handle HTTP Errors
                status = getattr(e.cause, 'code', None)
                if status in RETRY_STATUS_CODES:
                    if attempt < _MAX_RETRIES:
                        logger.warning(f"API HTTP {status}. Retrying {attempt + 1}/{_MAX_RETRIES} in {RETRY_DELAY}s...")
                        time.sleep(RETRY_DELAY)
                        continue
                logger.error(f"API ResponseError: {e}")
                raise
            
            except (musicbrainzngs.NetworkError, musicbrainzngs.WebServiceError) as e:
                # Handle standard network errors/timeouts
                if attempt < _MAX_RETRIES:
                    logger.warning(f"Network error: {e}. Retrying {attempt + 1}/{_MAX_RETRIES} in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                    continue
                logger.error(f"API NetworkError: {e}")
                raise
                
        raise Exception("Max API retries exceeded.")

    def search_recordings(self, artist: str, title: str) -> Dict[str, Any]:
        query = f'artist:"{artist}" AND recording:"{title}"'
        return self._request(musicbrainzngs.search_recordings, query=query, limit=50)

    def search_release_groups(self, artist: str, title: str) -> Dict[str, Any]:
        query = f'artist:"{artist}" AND releasegroup:"{title}"'
        return self._request(musicbrainzngs.search_release_groups, query=query, limit=10)

    def get_releases_for_group(self, release_group_id: str) -> Dict[str, Any]:
        return self._request(
            musicbrainzngs.browse_releases, 
            release_group=release_group_id, 
            includes=["recordings"], 
            limit=10
        )

# --- Core Business Logic ---

class MusicMetadataService:
    """High-level service orchestrating searches, filtering, and data extraction."""

    def __init__(self):
        self.api = MusicBrainzAPI()

    def resolve_first_release_date(self, artist: str, title: str) -> Optional[str]:
        """
        Determines the earliest valid release date by examining all 
        available MusicBrainz recordings and their releases.
        """
        logger.info(f"Resolving first release date for '{title}' by {artist}...")
        title = TitleMatcher.normalize(title)
        response = self.api.search_recordings(artist, title)
        recordings = response.get("recording-list", [])

        earliest_date = "9999-99-99"

        for rec in recordings:
            rec_title = rec.get("title", "")

            # Skip if title doesn't match or isn't a canonical version
            if not TitleMatcher.matches(title, rec_title) or not TitleMatcher.is_canonical(rec_title):
                continue

            releases = rec.get("release-list", [])
            for release in releases:
                rg = release.get("release-group", {})
                secondary_types = rg.get("secondary-type-list", [])

                # Skip live, compilation, promo, etc.
                if not ReleaseFilter.is_valid_release(secondary_types):
                    continue

                # Check release and release-group dates
                date = release.get("date") or rg.get("first-release-date")
                if date and date < earliest_date:
                    earliest_date = date

        if earliest_date == "9999-99-99":
            logger.info("No valid release date found.")
            return None

        logger.info(f"Resolved earliest release date: {earliest_date}")
        return earliest_date

    def fetch_album(self, artist: str, title: str) -> Dict[str, Any]:
        """Fetches metadata and tracklist for a specific album."""
        logger.info(f"Fetching album data for '{title}' by {artist}...")
        response = self.api.search_release_groups(artist, title)
        groups = response.get("release-group-list", [])

        first_date = []
        best_group = None
        for group in groups:
            if TitleMatcher.matches(title, group.get("title", "")) and group.get("primary-type", "").lower() == "album":
                best_group = group
                break

        if not best_group:
            logger.info("Album not found on MusicBrainz.")
            return None

        group_id = best_group.get("id")
        first_date = best_group.get("first-release-date")

        if first_date:
            logger.info(f"Resolved earliest release date: {first_date}")
            return first_date[:10]  # Ensure YYYY-MM-DD format

        # Fetch the official releases for this group to get the tracklist
        releases_resp = self.api.get_releases_for_group(group_id)
        releases = releases_resp.get("release-list", [])
        earliest_date = "9999-99-99"
        for release in releases:
            rg = release.get("release-group", {})
            secondary_types = rg.get("secondary-type-list", [])

            # Skip live, compilation, promo, etc.
            if not ReleaseFilter.is_valid_release(secondary_types):
                continue

            # Check release and release-group dates
            date = release.get("date") or rg.get("first-release-date")
            if date and date < earliest_date:
                earliest_date = date

        if earliest_date == "9999-99-99":
            logger.info("No valid release date found.")
            return None

        logger.info(f"Resolved earliest release date: {earliest_date}")
        return earliest_date

def lookup_musicbrainz(artist: str, title: str, lookup_type: str) -> Dict[str, Any]:
    """
    Main entry point. Looks up a track or album on MusicBrainz.
    
    :param artist: Name of the artist.
    :param title: Title of the track or album.
    :param lookup_type: 'single' or 'album'.
    :return: A JSON-serializable dictionary with metadata.
    """
    service = MusicMetadataService()

    if lookup_type.lower() == "single":
        return service.resolve_first_release_date(artist, title)
    elif lookup_type.lower() == "album":
        return service.fetch_album(artist, title)
    else:
        raise ValueError("Invalid lookup type. Must be 'single' or 'album'.")

# --- Example Usage ---

if __name__ == "__main__":
    print("--- Lookup Track ---")
    try:
        track_data = lookup_musicbrainz(
            artist="The Cure", 
            title="10:15 Saturday Night", 
            lookup_type="single"
        )
        print(json.dumps(track_data, indent=2))
    except Exception as e:
        print(f"Error looking up single: {e}")

    print("\n--- Lookup Album ---")
    try:
        album_data = lookup_musicbrainz(
            artist="The Cure", 
            title="Disintegration", 
            lookup_type="album"
        )
        print(json.dumps(album_data, indent=2))
    except Exception as e:
        print(f"Error looking up album: {e}")
