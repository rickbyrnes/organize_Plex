#!/usr/bin/env python3
"""
organize_plex.py — Organize video files into Plex-style folders.

MOVIES
  1. Works out the title and release year (from the filename and, optionally,
     a metadata provider).
  2. Creates "<Title> (<Year>)" and moves the video in, renamed to match.
  3. Moves matching sidecars (subtitles, artwork, .nfo, ...) and downloads
     poster.jpg / fanart.jpg, movie.nfo and info.json.
  4. Bonus content goes into that movie's "Extra" subfolder.

TV SERIES
  1. Recognizes episode filenames (S01E01, 1x01, "Season 1 Episode 1").
  2. On the FIRST episode found, builds the entire structure:
       <Series> (<Year>)/
         poster.jpg, fanart.jpg, tvshow.nfo, info.json
         Season 01/  Season 02/  ...  (all seasons, from metadata)
         Extras/
     Each season gets poster.jpg, season.nfo and info.json.
  3. Later episodes land in the right season as
       "<Series> (<Year>) - S01E01 - <Episode Title>.<ext>"
     with subtitles, an episode .nfo/.info.json and a still image.
  4. Bonus content goes into the series-level "Extras" subfolder.

It uses only the Python standard library, so there is nothing to install.

Metadata providers are optional and configured in config.json (or via the
OMDB_API_KEY / TMDB_API_KEY environment variables). Without an API key the
script still works: it derives the year from the filename and groups/moves
your files, it just cannot download artwork or write detailed info.

Examples
--------
  # Preview what would happen (no changes made):
  python3 organize_plex.py --dry-run

  # Organize the current folder in place:
  python3 organize_plex.py

  # Organize a source folder into a separate library folder, recursively:
  python3 organize_plex.py --source /downloads --dest /Media/Movies --recursive

  # Continuously watch a folder for new videos:
  python3 organize_plex.py --source /downloads --dest /Media/Movies --watch
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

VERSION = "1.1.0"
DEFAULT_CONFIG_NAME = "config.json"

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv",
    ".flv", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".vob", ".mts",
}

SIDECAR_EXTENSIONS = {
    # subtitles
    ".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx", ".sup", ".smi",
    # artwork / info
    ".jpg", ".jpeg", ".png", ".webp", ".nfo", ".txt", ".json", ".xml",
    ".bif", ".cdb", ".ffindex",
}

# Filenames matching one of these patterns are bonus/"extra" content rather
# than the main movie. They get an "Extra" subfolder inside the movie folder.
EXTRA_PATTERNS: list[tuple[str, str]] = [
    (r"behind\s*(?:the\s*)?scenes?", "Behind The Scenes"),
    (r"making\s*of", "Making Of"),
    (r"deleted\s*scenes?", "Deleted Scenes"),
    (r"featurettes?", "Featurette"),
    (r"trailers?", "Trailer"),
    (r"teasers?", "Teaser"),
    (r"bloopers?", "Bloopers"),
    (r"outtakes?", "Outtakes"),
    (r"interviews?", "Interview"),
    (r"specials?", "Special Feature"),
    (r"bonus", "Bonus"),
    (r"extras?", "Extra"),
    (r"promo", "Promo"),
    (r"preview", "Preview"),
]

EXTRA_FOLDER_NAMES = {
    "extra", "extras", "featurettes", "trailers", "specials", "bonus",
}

# Episode identifiers: S01E01, s01e01, S01.E01, 1x01, "Season 1 Episode 1".
EPISODE_PATTERNS = [
    re.compile(r"\b[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})\b"),
    re.compile(r"\b(\d{1,2})[xX](\d{1,3})\b"),
    re.compile(r"\bSeason\s*(\d{1,2})\s*Episode\s*(\d{1,3})\b", re.IGNORECASE),
]

# Tokens commonly found in scene release names that are not part of the title.
TECH_TOKENS = re.compile(
    r"\b(?:"
    r"1080p|720p|2160p|480p|360p|4k|uhd|8k|"
    r"hdr|hdr10|hdr10plus|dv|dolby.?vision|"
    r"bluray|blu.?ray|bdrip|brrip|webrip|web.?dl|web|hdtv|dvdrip|"
    r"cam|ts|tc|remux|hdtvrip|"
    r"x264|x265|h264|h265|hevc|avc|xvid|divx|"
    r"aac|ac3|dts|dd5.?1|truehd|atmos|flac|mp3|"
    r"extended|unrated|directors?.cut|remastered|imax|proper|repack|"
    r"internal|limited|complete|amzn|nf|hulu|dsnp|hmax|atvp|"
    r"multi|dual|english|englishdubbed|"
    r"10bit|8bit|yify|"
    r"\bhd\b|\bsd\b"
    r")\b",
    re.IGNORECASE,
)

YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
BRACKET_YEAR_RE = re.compile(r"[\[(]\s*((?:19|20)\d{2})\s*[\])]")


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class Metadata:
    """A normalized set of movie metadata fields."""

    title: str = ""
    year: Optional[int] = None
    original_title: str = ""
    imdb_id: str = ""
    plot: str = ""
    genres: list[str] = field(default_factory=list)
    director: str = ""
    writers: str = ""
    actors: list[str] = field(default_factory=list)
    runtime_min: Optional[int] = None
    rating: Optional[float] = None
    votes: Optional[int] = None
    poster_url: str = ""
    fanart_url: str = ""
    source: str = ""

    @property
    def display_title(self) -> str:
        return self.title or self.original_title


@dataclass
class TvEpisodeMeta:
    number: int
    title: str = ""
    overview: str = ""
    air_date: str = ""
    rating: Optional[float] = None
    still_url: str = ""


@dataclass
class TvSeasonMeta:
    number: int
    title: str = ""
    episode_count: int = 0
    air_date: str = ""
    overview: str = ""
    poster_url: str = ""
    episodes: list[TvEpisodeMeta] = field(default_factory=list)


@dataclass
class TvMetadata:
    """A normalized set of TV-series metadata fields."""

    title: str = ""
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    imdb_id: str = ""
    overview: str = ""
    genres: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    rating: Optional[float] = None
    votes: Optional[int] = None
    poster_url: str = ""
    fanart_url: str = ""
    seasons: list[TvSeasonMeta] = field(default_factory=list)
    source: str = ""


@dataclass
class SeriesState:
    """Everything needed to place files for one series."""

    key: str
    title: str
    year: int
    folder: Path
    extras_folder: Path
    metadata: Optional[TvMetadata]
    seasons: set[int]
    season_details: dict[int, Optional[dict]] = field(default_factory=dict)
    warned: bool = False


@dataclass
class TvPlan:
    video: Path
    state: SeriesState
    season: int
    episode: int
    folder: Path
    target: Path
    episode_meta: Optional[TvEpisodeMeta]
    sidecars: list[Path]


# --------------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------------- #
class Log:
    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose

    def info(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def warn(self, msg: str) -> None:
        print(f"[warn] {msg}", file=sys.stderr, flush=True)

    def error(self, msg: str) -> None:
        print(f"[error] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Filename parsing
# --------------------------------------------------------------------------- #
def clean_title(raw: str) -> str:
    """Turn a release-style filename stem into a readable movie title."""
    text = raw.replace(".", " ").replace("_", " ").replace("+", " ")
    # Drop bracketed/parenthesised groups (release tags, years, etc.); the year
    # is extracted separately in extract_year().
    text = re.sub(r"[\[(]([^\])]*)[\])]", " ", text)
    # Remove technical/release tokens.
    text = TECH_TOKENS.sub(" ", text)
    # Remove a trailing uppercase release group (e.g. "-RARBG", "-YIFY").
    text = re.sub(r"-\s*[A-Z0-9]{2,12}\s*$", " ", text)
    # Collapse whitespace and stray separators.
    text = re.sub(r"\s+", " ", text).strip(" -_[]()")
    return text.strip()


def extract_year(stem: str) -> Optional[int]:
    """Find the most likely release year in a filename stem."""
    bracketed = BRACKET_YEAR_RE.findall(stem)
    if bracketed:
        return int(bracketed[0])
    plain = YEAR_RE.findall(stem)
    # Prefer a year at the end of the name (e.g. "Movie.2010.1080p" -> 2010).
    for y in reversed(plain):
        if 1900 <= int(y) <= 2099:
            return int(y)
    return None


def smart_titlecase(text: str) -> str:
    small = {"a", "an", "the", "and", "but", "or", "for", "nor", "on",
             "at", "to", "from", "by", "in", "of", "with", "as", "is"}
    words = text.split()
    out: list[str] = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and lw in small:
            out.append(lw)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def parse_filename(filename: str) -> tuple[str, Optional[int]]:
    """Return (cleaned title, year or None) for a video filename."""
    stem = Path(filename).stem
    year = extract_year(stem)
    title = clean_title(stem)

    # Remove the detected year from the title so it is not duplicated in the
    # folder name (which adds the year back in parentheses).
    if year:
        before = title
        title = re.sub(r"\b" + str(year) + r"\b", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" -_[]()")
        if not title:
            title = before

    if not title:
        title = smart_titlecase(stem.replace(".", " ").replace("_", " "))
    else:
        title = smart_titlecase(title)
    return title, year


def parse_episode(filename: str) -> Optional[tuple[str, Optional[int], int, int]]:
    """Return (series title, year, season, episode) for a TV episode filename."""
    stem = Path(filename).stem
    spaced = re.sub(r"[._+]", " ", stem)
    match = None
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(spaced)
        if match:
            break
    if not match:
        return None

    season, episode = int(match.group(1)), int(match.group(2))
    year = extract_year(stem)
    rest = (spaced[:match.start()] + " " + spaced[match.end():]).strip()
    title = clean_title(rest)
    if year:
        title = re.sub(r"\b" + str(year) + r"\b", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" -_[]()")
    if not title:
        title = clean_title(spaced[:match.start()])
    title = smart_titlecase(title) if title else "Unknown Series"
    return title, year, season, episode


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def series_key(title: str, year: Optional[int]) -> str:
    """Grouping key so all episodes of a series share one structure."""
    return f"{title_key(title)}|{year or ''}"


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = 20) -> Optional[bytes]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"organize-plex/{VERSION} (+https://github.com)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"request failed for {url}: {exc}") from exc


def http_get_json(url: str, timeout: int = 20) -> Optional[dict]:
    data = http_get(url, timeout=timeout)
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Metadata providers
# --------------------------------------------------------------------------- #
class BaseProvider:
    name = "none"

    def fetch(self, title: str, year: Optional[int], cfg: dict) -> Optional[Metadata]:
        return None


class OMDbProvider(BaseProvider):
    name = "omdb"
    BASE = "https://www.omdbapi.com/"

    def fetch(self, title: str, year: Optional[int], cfg: dict) -> Optional[Metadata]:
        key = cfg.get("omdb_api_key") or os.environ.get("OMDB_API_KEY", "")
        if not key:
            return None
        params = {
            "t": title,
            "apikey": key,
            "plot": "full",
            "r": "json",
        }
        if year:
            params["y"] = str(year)
        url = self.BASE + "?" + urllib.parse.urlencode(params)
        try:
            data = http_get_json(url)
        except RuntimeError:
            return None
        if not data or data.get("Response") != "True":
            return None

        def as_int(value: str) -> Optional[int]:
            m = re.search(r"\d+", value or "")
            return int(m.group(0)) if m else None

        def runtime_to_min(value: str) -> Optional[int]:
            m = re.search(r"(\d+)\s*min", value or "")
            return int(m.group(1)) if m else None

        return Metadata(
            title=data.get("Title", ""),
            year=as_int(data.get("Year", "")),
            plot=data.get("Plot", ""),
            genres=[g.strip() for g in data.get("Genre", "").split(",") if g.strip()],
            director=data.get("Director", ""),
            writers=data.get("Writer", ""),
            actors=[a.strip() for a in data.get("Actors", "").split(",") if a.strip()],
            runtime_min=runtime_to_min(data.get("Runtime", "")),
            rating=float(data["imdbRating"]) if data.get("imdbRating", "N/A") not in ("N/A", "", None) else None,
            poster_url=data.get("Poster", "") if data.get("Poster", "N/A") != "N/A" else "",
            imdb_id=data.get("imdbID", ""),
            source="omdb",
        )

    def fetch_tv(self, title: str, year: Optional[int], cfg: dict) -> Optional[TvMetadata]:
        key = cfg.get("omdb_api_key") or os.environ.get("OMDB_API_KEY", "")
        if not key:
            return None
        params = {
            "t": title, "type": "series", "apikey": key,
            "plot": "full", "r": "json",
        }
        if year:
            params["y"] = str(year)
        try:
            data = http_get_json(self.BASE + "?" + urllib.parse.urlencode(params))
        except RuntimeError:
            return None
        if not data or data.get("Response") != "True":
            return None

        def as_int(value: str) -> Optional[int]:
            m = re.search(r"\d+", value or "")
            return int(m.group(0)) if m else None

        total = as_int(data.get("totalSeasons", "")) or 0
        seasons = [TvSeasonMeta(number=n) for n in range(1, total + 1)]
        return TvMetadata(
            title=data.get("Title", ""),
            year=as_int(data.get("Year", "")),
            imdb_id=data.get("imdbID", ""),
            overview=data.get("Plot", ""),
            genres=[g.strip() for g in data.get("Genre", "").split(",") if g.strip()],
            creators=[c.strip() for c in data.get("Writer", "").split(",") if c.strip()],
            rating=float(data["imdbRating"]) if data.get("imdbRating", "N/A") not in ("N/A", "", None) else None,
            poster_url=data.get("Poster", "") if data.get("Poster", "N/A") != "N/A" else "",
            seasons=seasons,
            source="omdb",
        )


class TMDbProvider(BaseProvider):
    name = "tmdb"
    BASE = "https://api.themoviedb.org/3"
    IMG = "https://image.tmdb.org/t/p/"

    def _image(self, path: str, size: str) -> str:
        return f"{self.IMG}{size}{path}" if path else ""

    def fetch(self, title: str, year: Optional[int], cfg: dict) -> Optional[Metadata]:
        key = cfg.get("tmdb_api_key") or os.environ.get("TMDB_API_KEY", "")
        if not key:
            return None
        language = cfg.get("language", "en-US")

        search_params = {
            "api_key": key,
            "query": title,
            "language": language,
            "include_adult": "false",
        }
        if year:
            search_params["year"] = str(year)
        search_url = f"{self.BASE}/search/movie?{urllib.parse.urlencode(search_params)}"
        try:
            results = (http_get_json(search_url) or {}).get("results", [])
        except RuntimeError:
            return None
        if not results:
            return None
        movie_id = results[0].get("id")
        if not movie_id:
            return None

        detail_params = {
            "api_key": key,
            "language": language,
            "append_to_response": "credits",
        }
        detail_url = f"{self.BASE}/movie/{movie_id}?{urllib.parse.urlencode(detail_params)}"
        try:
            d = http_get_json(detail_url)
        except RuntimeError:
            return None
        if not d:
            return None

        credits = d.get("credits", {}) or {}
        cast = credits.get("cast", [])[:10]
        directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
        writers = [c["name"] for c in credits.get("crew", []) if c.get("department") == "Writing"]

        release = d.get("release_date", "") or ""
        m = re.match(r"(\d{4})", release)
        movie_year = int(m.group(1)) if m else None

        return Metadata(
            title=d.get("title", ""),
            original_title=d.get("original_title", ""),
            year=movie_year,
            imdb_id=d.get("imdb_id", ""),
            plot=d.get("overview", ""),
            genres=[g["name"] for g in d.get("genres", []) if g.get("name")],
            director=", ".join(directors),
            writers=", ".join(writers),
            actors=[c["name"] for c in cast if c.get("name")],
            runtime_min=d.get("runtime"),
            rating=d.get("vote_average") if d.get("vote_average") else None,
            votes=d.get("vote_count"),
            poster_url=self._image(d.get("poster_path", ""), "w780"),
            fanart_url=self._image(d.get("backdrop_path", ""), "original"),
            source="tmdb",
        )

    def _key(self, cfg: dict) -> str:
        return cfg.get("tmdb_api_key") or os.environ.get("TMDB_API_KEY", "")

    def fetch_tv(self, title: str, year: Optional[int], cfg: dict) -> Optional[TvMetadata]:
        key = self._key(cfg)
        if not key:
            return None
        language = cfg.get("language", "en-US")
        params = {
            "api_key": key, "query": title, "language": language,
            "include_adult": "false",
        }
        if year:
            params["first_air_date_year"] = str(year)
        try:
            results = (http_get_json(f"{self.BASE}/search/tv?{urllib.parse.urlencode(params)}")
                       or {}).get("results", [])
        except RuntimeError:
            return None
        if not results or not results[0].get("id"):
            return None
        tv_id = results[0]["id"]

        detail_params = {"api_key": key, "language": language, "append_to_response": "credits"}
        try:
            d = http_get_json(f"{self.BASE}/tv/{tv_id}?{urllib.parse.urlencode(detail_params)}")
        except RuntimeError:
            return None
        if not d:
            return None

        m = re.match(r"(\d{4})", d.get("first_air_date", "") or "")
        creators = [c["name"] for c in (d.get("created_by", []) or []) if c.get("name")]
        networks = [n["name"] for n in d.get("networks", []) if n.get("name")]
        genres = [g["name"] for g in d.get("genres", []) if g.get("name")]

        seasons: list[TvSeasonMeta] = []
        for s in d.get("seasons", []) or []:
            num = s.get("season_number")
            if num is None or num < 1:
                continue  # skip "Specials" season 0
            seasons.append(TvSeasonMeta(
                number=num,
                title=s.get("name", ""),
                episode_count=s.get("episode_count", 0) or 0,
                air_date=s.get("air_date", "") or "",
                overview=s.get("overview", "") or "",
                poster_url=self._image(s.get("poster_path", ""), "w780"),
            ))

        meta = TvMetadata(
            title=d.get("name", "") or title,
            year=int(m.group(1)) if m else year,
            tmdb_id=tv_id,
            imdb_id=d.get("external_ids", {}).get("imdb_id", "") if isinstance(d.get("external_ids"), dict) else "",
            overview=d.get("overview", "") or "",
            genres=genres,
            creators=creators,
            networks=networks,
            rating=d.get("vote_average") if d.get("vote_average") else None,
            votes=d.get("vote_count"),
            poster_url=self._image(d.get("poster_path", ""), "w780"),
            fanart_url=self._image(d.get("backdrop_path", ""), "original"),
            seasons=seasons,
            source="tmdb",
        )
        return meta

    def fetch_season(self, tv_id: int, season: int, cfg: dict) -> Optional[dict]:
        """Return the raw TMDb season detail (with its episode list)."""
        key = self._key(cfg)
        if not key:
            return None
        params = {"api_key": key, "language": cfg.get("language", "en-US")}
        try:
            return http_get_json(f"{self.BASE}/tv/{tv_id}/season/{season}?{urllib.parse.urlencode(params)}")
        except RuntimeError:
            return None


PROVIDERS: dict[str, type[BaseProvider]] = {
    "tmdb": TMDbProvider,
    "omdb": OMDbProvider,
}


def fetch_metadata(title: str, year: Optional[int], cfg: dict, log: Log) -> Optional[Metadata]:
    preference = cfg.get("provider", "auto")
    names: list[str] = []
    if preference == "auto":
        names = ["tmdb", "omdb"]
    elif preference in PROVIDERS:
        names = [preference]
    elif preference in ("none", ""):
        names = []

    for name in names:
        provider = PROVIDERS[name]()
        has_key = bool(cfg.get(f"{name}_api_key") or os.environ.get(f"{name.upper()}_API_KEY", ""))
        if not has_key:
            continue
        log.info(f"      querying {name.upper()} for metadata...")
        try:
            meta = provider.fetch(title, year, cfg)
        except Exception as exc:  # noqa: BLE001 - keep going with other providers
            log.warn(f"{name} lookup failed: {exc}")
            continue
        if meta and meta.display_title:
            return meta
    return None


def fetch_tv_metadata(title: str, year: Optional[int], cfg: dict, log: Log) -> Optional[TvMetadata]:
    """Look up a series using the same provider preference as movies."""
    preference = cfg.get("provider", "auto")
    if preference == "auto":
        names = ["tmdb", "omdb"]
    elif preference in PROVIDERS:
        names = [preference]
    else:
        names = []

    for name in names:
        provider = PROVIDERS[name]()
        has_key = bool(cfg.get(f"{name}_api_key") or os.environ.get(f"{name.upper()}_API_KEY", ""))
        if not has_key:
            continue
        log.info(f"      querying {name.upper()} for series metadata...")
        try:
            meta = provider.fetch_tv(title, year, cfg)
        except Exception as exc:  # noqa: BLE001 - try the next provider
            log.warn(f"{name} TV lookup failed: {exc}")
            continue
        if meta and meta.title:
            return meta
    return None


# --------------------------------------------------------------------------- #
# Output generation
# --------------------------------------------------------------------------- #
def safe_name(name: str) -> str:
    """Make a filesystem-safe folder/file name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Untitled"


def download_image(url: str, dest: Path, log: Log) -> bool:
    if not url:
        return False
    try:
        data = http_get(url)
    except RuntimeError as exc:
        log.warn(f"could not download artwork: {exc}")
        return False
    if not data or len(data) < 100:
        return False
    try:
        dest.write_bytes(data)
    except OSError as exc:
        log.warn(f"could not write artwork {dest}: {exc}")
        return False
    return True


def build_nfo(meta: Metadata, video_filename: str, poster: bool, fanart: bool) -> str:
    def tag(name: str, value: object) -> str:
        if value is None or value == "":
            return ""
        return f"  <{name}>{escape(str(value))}</{name}>\n"

    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n', "<movie>\n"]
    lines.append(tag("title", meta.display_title))
    lines.append(tag("originaltitle", meta.original_title))
    lines.append(tag("year", meta.year))
    if meta.imdb_id:
        lines.append(tag("id", meta.imdb_id))
    lines.append(tag("plot", meta.plot))
    lines.append(tag("runtime", meta.runtime_min))
    lines.append(tag("rating", meta.rating))
    lines.append(tag("votes", meta.votes))
    lines.append(tag("director", meta.director))
    lines.append(tag("writer", meta.writers))
    for genre in meta.genres:
        lines.append(tag("genre", genre))
    for actor in meta.actors:
        lines.append("  <actor>\n")
        lines.append(tag("name", actor))
        lines.append("  </actor>\n")
    lines.append(tag("file", video_filename))
    if poster:
        lines.append("  <poster>poster.jpg</poster>\n")
    if fanart:
        lines.append("  <fanart>fanart.jpg</fanart>\n")
    lines.append("</movie>\n")
    return "".join(lines)


def escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def _nfo(tag: str, fields: list[tuple[str, object]]) -> str:
    def add(name: str, value: object) -> str:
        if value is None or value == "":
            return ""
        return f"  <{name}>{escape(str(value))}</{name}>\n"

    body = "".join(add(name, value) for name, value in fields)
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<{tag}>\n{body}</{tag}>\n'


def build_tvshow_nfo(meta: TvMetadata, poster: bool, fanart: bool) -> str:
    fields: list[tuple[str, object]] = [
        ("title", meta.title), ("year", meta.year), ("id", meta.imdb_id),
        ("plot", meta.overview), ("rating", meta.rating),
    ]
    fields += [("genre", g) for g in meta.genres]
    fields += [("director", c) for c in meta.creators]
    fields += [("studio", n) for n in meta.networks]
    fields += [("season", s.number) for s in meta.seasons]
    if poster:
        fields.append(("thumb", "poster.jpg"))
    if fanart:
        fields.append(("fanart", "fanart.jpg"))
    return _nfo("tvshow", fields)


def build_season_nfo(season: TvSeasonMeta, series_title: str, poster: bool) -> str:
    fields: list[tuple[str, object]] = [
        ("title", season.title or f"Season {season.number:02d}"),
        ("showtitle", series_title),
        ("seasonnumber", season.number),
        ("year", season.air_date[:4] if season.air_date else ""),
        ("plot", season.overview),
    ]
    if poster:
        fields.append(("thumb", "poster.jpg"))
    return _nfo("season", fields)


def build_episode_nfo(title: str, season: int, episode: int,
                      meta: Optional[TvEpisodeMeta], still: bool) -> str:
    fields: list[tuple[str, object]] = [
        ("title", (meta.title if meta else "") or title),
        ("season", season), ("episode", episode),
    ]
    if meta:
        fields += [("plot", meta.overview), ("aired", meta.air_date), ("rating", meta.rating)]
    if still:
        fields.append(("thumb", "poster.jpg"))
    return _nfo("episodedetails", fields)


# --------------------------------------------------------------------------- #
# Organization logic
# --------------------------------------------------------------------------- #
def _normalized_stem(text: str) -> str:
    """Normalize a stem for comparison: drop years, brackets, dots, casing."""
    text = re.sub(r"[\[(][^\])]*[\])]", " ", text)
    text = YEAR_RE.sub(" ", text)
    text = text.replace(".", " ").replace("_", " ").replace("+", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def find_sidecars(video: Path, candidates: Iterable[Path]) -> list[Path]:
    """Find files that belong to the same movie as ``video``."""
    stem = video.stem.lower()
    vbase = _normalized_stem(video.stem)
    found: list[Path] = []
    for path in candidates:
        if path.resolve() == video.resolve():
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in SIDECAR_EXTENSIONS:
            continue
        pstem = path.stem.lower()
        pbase = _normalized_stem(path.stem)
        # Allow a trailing language/flag token, e.g. "movie.en.srt".
        pbase_no_lang = re.sub(r"\s+[a-z]{2,5}$", "", pbase)
        if (
            pstem == stem
            or pstem.startswith(stem + ".")
            or pstem.startswith(stem + "-")
            or pbase == vbase
            or (pbase_no_lang == vbase and pbase != pbase_no_lang)
        ):
            found.append(path)
    return found


@dataclass
class ExtraInfo:
    """Details detected about an extra video."""

    parent_source: str   # text used to work out the parent movie's title/year
    extra_title: str     # title for the extra itself, e.g. "Trailer"
    keyword: str = ""


@dataclass
class ExtraPlan:
    video: Path
    extra_title: str
    parent_title: str
    parent_year: Optional[int]
    folder: Path         # the "Extra" subfolder
    sidecars: list[Path]


@dataclass
class Plan:
    video: Path
    title: str
    year: Optional[int]
    metadata: Optional[Metadata]
    sidecars: list[Path]
    folder: Path


def build_plan(video: Path, source: Path, dest: Path, cfg: dict,
               all_files: list[Path], log: Log) -> Plan:
    title, year = parse_filename(video.name)
    log.info(f"    parsed title: {title!r}, year: {year}")

    meta = fetch_metadata(title, year, cfg, log) if not cfg.get("no_metadata") else None
    if meta:
        title = meta.display_title or title
        year = meta.year or year
    if not year:
        fallback = cfg.get("default_year")
        if fallback:
            try:
                year = int(fallback)
            except ValueError:
                year = None
        if not year:
            year = datetime.now().year
            log.warn(f"no release year found for {video.name!r}; using {year} "
                     "(set 'default_year' in config.json to override)")

    year_str = str(year) if year else "Unknown"
    folder_name = safe_name(f"{title} ({year_str})")
    folder = dest / folder_name
    sidecars = find_sidecars(video, all_files)
    return Plan(video=video, title=title, year=year, metadata=meta,
                sidecars=sidecars, folder=folder)


def detect_extra(video: Path, source: Path) -> Optional[ExtraInfo]:
    """Detect bonus content and work out which movie it belongs to."""
    stem = re.sub(r"[._+]", " ", video.stem)
    stem = re.sub(r"[\[\]()]", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    lowered = stem.lower()

    # A file living in an "extras"-style folder is an extra even if its name
    # has no keyword. The parent movie is the nearest non-extras ancestor.
    parent_hint: Optional[str] = None
    if video.parent.name.lower() in EXTRA_FOLDER_NAMES and video.parent != source:
        for ancestor in video.parent.parents:
            if ancestor == source:
                break
            if ancestor.name.lower() not in EXTRA_FOLDER_NAMES:
                parent_hint = ancestor.name
                break

    matched_display: Optional[str] = None
    matched_pattern: Optional[str] = None
    for pattern, display in EXTRA_PATTERNS:
        if re.search(r"\b" + pattern + r"\b", lowered):
            matched_display, matched_pattern = display, pattern
            break

    if not matched_display and not parent_hint:
        return None

    if matched_pattern:
        # Everything up to (and including) the year is the parent movie; the
        # rest is the extra's descriptor ("- Official Trailer 2", etc.).
        year_match = BRACKET_YEAR_RE.search(stem) or YEAR_RE.search(stem)
        if year_match:
            parent_source = stem[:year_match.end()]
        else:
            parent_source = re.sub(r"\b" + matched_pattern + r"\b", " ", stem,
                                   flags=re.IGNORECASE)
        extra_title = matched_display or "Extra"
    else:
        parent_source = stem
        extra_title = smart_titlecase(clean_title(stem)) or "Extra"

    parent_source = parent_source.strip(" -_")
    # If the filename carried no parent name, fall back to the folder containing
    # the extras folder (e.g. "Interstellar (2014)/extras/trailer.mkv").
    if not parent_source and parent_hint:
        parent_source = parent_hint

    return ExtraInfo(parent_source=parent_source,
                     extra_title=extra_title, keyword=matched_display or "")


def build_extra_plan(video: Path, info: ExtraInfo, source: Path, dest: Path,
                     cfg: dict, all_files: list[Path], log: Log) -> ExtraPlan:
    if info.parent_source:
        parent_title, parent_year = parse_filename(info.parent_source + video.suffix)
    else:
        parent_title, parent_year = "", None
    log.info(f"    extra {info.extra_title!r} -> parent {parent_title!r}, year {parent_year}")

    # Use the same metadata logic as the main movie so the folder names match.
    meta = None
    if not cfg.get("no_metadata") and parent_title:
        meta = fetch_metadata(parent_title, parent_year, cfg, log)
    if meta:
        parent_title = meta.display_title or parent_title
        parent_year = meta.year or parent_year

    if not parent_title:
        parent_title = "Unknown Movie"
    if not parent_year:
        fallback = cfg.get("default_year")
        if fallback:
            try:
                parent_year = int(fallback)
            except ValueError:
                pass
        if not parent_year:
            parent_year = datetime.now().year
            log.warn(f"no year for parent of {video.name!r}; using {parent_year}")

    movie_folder = dest / safe_name(f"{parent_title} ({parent_year})")
    folder = movie_folder / safe_name(cfg.get("extra_folder_name", "Extra"))
    return ExtraPlan(video=video, extra_title=info.extra_title,
                     parent_title=parent_title, parent_year=parent_year,
                     folder=folder, sidecars=find_sidecars(video, all_files))


def execute_extra_plan(plan: ExtraPlan, cfg: dict, log: Log, dry_run: bool) -> None:
    ext = plan.video.suffix.lower()
    base = safe_name(plan.extra_title)
    target = plan.folder / f"{base}{ext}"

    log.info(f"    extra folder: {plan.folder}")
    log.info(f"    extra video : {plan.video.name} -> {target.name}")
    for side in plan.sidecars:
        log.info(f"    extra data  : {side.name}")

    if dry_run:
        log.info("    [dry-run] no changes made")
        return

    plan.folder.mkdir(parents=True, exist_ok=True)

    # Avoid overwriting another extra of the same type (two trailers, etc.).
    counter = 2
    while target.exists() and target.resolve() != plan.video.resolve():
        target = plan.folder / f"{base} {counter}{ext}"
        counter += 1

    if target.resolve() != plan.video.resolve():
        if target.exists():
            log.warn(f"destination exists, skipping extra: {target}")
        else:
            shutil.move(str(plan.video), str(target))

    # Move the extra's sidecars/subtitles/artwork into the same Extra folder.
    stem = target.stem
    for side in plan.sidecars:
        suffix_part = side.stem[len(plan.video.stem):].lstrip(".-_ ")
        new_name = f"{stem}{('.' + suffix_part) if suffix_part else ''}{side.suffix}"
        new_path = plan.folder / new_name
        if new_path.resolve() == side.resolve():
            continue
        if new_path.exists():
            log.warn(f"destination exists, skipping extra data: {new_path}")
            continue
        shutil.move(str(side), str(new_path))

    # Write info about the extra (one file per extra so none overwrite another).
    info = {
        "type": "extra",
        "title": plan.extra_title,
        "parent_title": plan.parent_title,
        "parent_year": plan.parent_year,
        "source_file": plan.video.name,
        "organized_at": datetime.now().isoformat(timespec="seconds"),
    }
    info_name = f"{target.stem}.info.json"
    (plan.folder / info_name).write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info(f"    wrote extra info: {info_name}")


# --------------------------------------------------------------------------- #
# TV series logic
# --------------------------------------------------------------------------- #
def _get_episode_meta(state: SeriesState, season: int, episode: int,
                      cfg: dict, log: Log) -> Optional[TvEpisodeMeta]:
    """Get episode-level detail, lazily fetching a season's episode list once."""
    meta = state.metadata
    if not meta:
        return None
    if meta.source == "tmdb" and meta.tmdb_id:
        if season not in state.season_details:
            log.info(f"      fetching TMDb season {season} episode list...")
            state.season_details[season] = TMDbProvider().fetch_season(
                meta.tmdb_id, season, cfg
            ) or {}
        detail = state.season_details[season] or {}
        for item in detail.get("episodes", []) or []:
            if item.get("episode_number") == episode:
                return TvEpisodeMeta(
                    number=episode,
                    title=item.get("name", "") or "",
                    overview=item.get("overview", "") or "",
                    air_date=item.get("air_date", "") or "",
                    rating=item.get("vote_average") if item.get("vote_average") else None,
                    still_url=TMDbProvider()._image(item.get("still_path", ""), "original"),
                )
        return None
    # OMDb has no episode-level endpoint; use anything already available.
    for s in meta.seasons:
        if s.number == season:
            for e in s.episodes:
                if e.number == episode:
                    return e
    return None


def _season_dir(folder: Path, season: int, cfg: dict) -> Path:
    pad = max(1, int(cfg.get("season_padding", 2) or 2))
    return folder / f"Season {season:0{pad}d}"


def setup_series(key: str, title: str, year_hint: Optional[int],
                 observed_seasons: set[int], dest: Path, cfg: dict,
                 log: Log, dry_run: bool) -> SeriesState:
    """Build the complete folder tree for a series on first encounter."""
    meta = None if cfg.get("no_metadata") else fetch_tv_metadata(title, year_hint, cfg, log)
    if meta:
        title = meta.title or title
        year_hint = meta.year or year_hint
    if not year_hint:
        fallback = cfg.get("default_year")
        if fallback:
            try:
                year_hint = int(fallback)
            except ValueError:
                pass
        if not year_hint:
            year_hint = datetime.now().year
            log.warn(f"no year for series {title!r}; using {year_hint}")

    year = year_hint
    folder = dest / safe_name(f"{title} ({year})")
    extras = folder / safe_name(cfg.get("tv_extras_folder_name", "Extras"))

    if meta and cfg.get("fetch_all_season_metadata", True):
        seasons = ({s.number for s in meta.seasons} | observed_seasons)
    else:
        seasons = set(observed_seasons)
    if not seasons:
        seasons = {1}

    state = SeriesState(key=key, title=title, year=year, folder=folder,
                        extras_folder=extras, metadata=meta, seasons=seasons)
    log.info(f"    series : {title} ({year}) — "
             f"{len(seasons)} season folder(s) + {extras.name}/")

    if dry_run:
        return state

    folder.mkdir(parents=True, exist_ok=True)
    extras.mkdir(parents=True, exist_ok=True)
    for number in sorted(seasons):
        _season_dir(folder, number, cfg).mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat(timespec="seconds")
    if meta:
        poster_ok = download_image(meta.poster_url, folder / "poster.jpg", log)
        fanart_ok = download_image(meta.fanart_url, folder / "fanart.jpg", log)
        (folder / "tvshow.nfo").write_text(
            build_tvshow_nfo(meta, poster_ok, fanart_ok), encoding="utf-8"
        )
    else:
        poster_ok = fanart_ok = False
        (folder / "tvshow.nfo").write_text(
            build_tvshow_nfo(TvMetadata(title=title, year=year), False, False),
            encoding="utf-8",
        )

    series_info = (asdict(meta) if meta else {"title": title, "year": year})
    series_info.update({
        "type": "series", "seasons": sorted(seasons),
        "poster": "poster.jpg" if poster_ok else "",
        "fanart": "fanart.jpg" if fanart_ok else "",
        "organized_at": now,
    })
    (folder / "info.json").write_text(
        json.dumps(series_info, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    season_meta = {s.number: s for s in (meta.seasons if meta else [])}
    for number in sorted(seasons):
        sdir = _season_dir(folder, number, cfg)
        sm = season_meta.get(number)
        season_poster = download_image(sm.poster_url, sdir / "poster.jpg", log) if sm else False
        (sdir / "season.nfo").write_text(
            build_season_nfo(sm or TvSeasonMeta(number=number), title, season_poster),
            encoding="utf-8",
        )
        sinfo = {
            "type": "season", "series": title, "series_year": year,
            "season": number,
            "episode_count": sm.episode_count if sm else 0,
            "air_date": sm.air_date if sm else "",
            "poster": "poster.jpg" if season_poster else "",
            "organized_at": now,
        }
        (sdir / "info.json").write_text(
            json.dumps(sinfo, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    if not meta and not state.warned:
        log.warn(f"no series metadata available; created season folders from files only")
        state.warned = True
    log.info("    wrote series + season metadata and created the full structure")
    return state


def build_tv_plan(video: Path, state: SeriesState, season: int, episode: int,
                  cfg: dict, all_files: list[Path], log: Log) -> TvPlan:
    em = _get_episode_meta(state, season, episode, cfg, log)
    base = f"{state.title} ({state.year}) - S{season:02d}E{episode:02d}"
    if cfg.get("include_episode_title", True) and em and em.title:
        base += f" - {em.title}"
    season_dir = _season_dir(state.folder, season, cfg)
    target = season_dir / (safe_name(base) + video.suffix.lower())
    return TvPlan(video=video, state=state, season=season, episode=episode,
                  folder=season_dir, target=target, episode_meta=em,
                  sidecars=find_sidecars(video, all_files))


def execute_tv_plan(plan: TvPlan, cfg: dict, log: Log, dry_run: bool) -> None:
    log.info(f"    season folder: {plan.folder}")
    log.info(f"    episode      : {plan.video.name} -> {plan.target.name}")
    for side in plan.sidecars:
        log.info(f"    sidecar      : {side.name}")

    if dry_run:
        log.info("    [dry-run] no changes made")
        return

    plan.folder.mkdir(parents=True, exist_ok=True)
    target = plan.target
    counter = 2
    while target.exists() and target.resolve() != plan.video.resolve():
        target = plan.folder / f"{plan.target.stem} {counter}{plan.video.suffix.lower()}"
        counter += 1

    if target.resolve() != plan.video.resolve():
        if target.exists():
            log.warn(f"destination exists, skipping episode: {target}")
        else:
            shutil.move(str(plan.video), str(target))

    stem = target.stem
    for side in plan.sidecars:
        suffix_part = side.stem[len(plan.video.stem):].lstrip(".-_ ")
        new_name = f"{stem}{('.' + suffix_part) if suffix_part else ''}{side.suffix}"
        new_path = plan.folder / new_name
        if new_path.resolve() == side.resolve():
            continue
        if new_path.exists():
            log.warn(f"destination exists, skipping sidecar: {new_path}")
            continue
        shutil.move(str(side), str(new_path))

    em = plan.episode_meta
    info = {
        "type": "episode",
        "series": plan.state.title,
        "series_year": plan.state.year,
        "season": plan.season,
        "episode": plan.episode,
        "title": em.title if em else "",
        "plot": em.overview if em else "",
        "air_date": em.air_date if em else "",
        "rating": em.rating if em else None,
        "source_file": plan.video.name,
        "organized_at": datetime.now().isoformat(timespec="seconds"),
    }
    (plan.folder / f"{stem}.info.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    still = plan.folder / f"{stem}.jpg"
    still_ok = False
    if em and em.still_url and not still.exists():
        still_ok = download_image(em.still_url, still, log)
    elif still.exists():
        still_ok = True
    ep_title = (em.title if em else "") or f"S{plan.season:02d}E{plan.episode:02d}"
    (plan.folder / f"{stem}.nfo").write_text(
        build_episode_nfo(ep_title, plan.season, plan.episode, em, still_ok),
        encoding="utf-8",
    )


def execute_plan(plan: Plan, cfg: dict, log: Log, dry_run: bool) -> None:
    video = plan.video
    year_str = str(plan.year) if plan.year else "Unknown"
    new_video_name = safe_name(f"{plan.title} ({year_str})") + video.suffix.lower()
    dest_video = plan.folder / new_video_name

    log.info(f"    folder : {plan.folder}")
    log.info(f"    video  : {video.name} -> {new_video_name}")
    for s in plan.sidecars:
        log.info(f"    sidecar: {s.name}")

    if dry_run:
        log.info("    [dry-run] no changes made")
        return

    plan.folder.mkdir(parents=True, exist_ok=True)

    # Move the video (rename unless configured to keep the original name).
    target = dest_video if not cfg.get("keep_video_name") else plan.folder / video.name
    if target.resolve() != video.resolve():
        if target.exists():
            log.warn(f"destination exists, skipping video: {target}")
        else:
            shutil.move(str(video), str(target))

    # Move sidecars, renaming them to match the new video stem.
    stem = target.stem
    for side in plan.sidecars:
        suffix_part = side.stem[len(video.stem):].lstrip(".-_ ")
        new_name = f"{stem}{('.' + suffix_part) if suffix_part else ''}{side.suffix}"
        new_path = plan.folder / new_name
        if new_path.resolve() == side.resolve():
            continue
        if new_path.exists():
            log.warn(f"destination exists, skipping sidecar: {new_path}")
            continue
        shutil.move(str(side), str(new_path))

    # Artwork + info.
    meta = plan.metadata
    if meta and not cfg.get("no_metadata"):
        poster_ok = download_image(meta.poster_url, plan.folder / "poster.jpg", log)
        fanart_ok = download_image(meta.fanart_url, plan.folder / "fanart.jpg", log)
        nfo = build_nfo(meta, target.name, poster_ok, fanart_ok)
        (plan.folder / "movie.nfo").write_text(nfo, encoding="utf-8")
        info = asdict(meta)
        info["organized_at"] = datetime.now().isoformat(timespec="seconds")
        (plan.folder / "info.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info(f"    wrote artwork + info from {meta.source.upper()}")


def collect_videos(source: Path, recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(
        p for p in source.glob(pattern)
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def organize_once(cfg: dict, log: Log) -> int:
    source = Path(cfg["source"]).expanduser().resolve()
    dest = Path(cfg["dest"]).expanduser().resolve()
    if not source.is_dir():
        log.error(f"source directory does not exist: {source}")
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    videos = collect_videos(source, cfg.get("recursive", False))
    if not videos:
        log.info(f"No video files found in {source}")
        return 0

    log.info(f"Found {len(videos)} video file(s) in {source}")
    all_files = [p for p in (source.glob("**/*" if cfg.get("recursive") else "*")) if p.is_file()]

    # Pre-pass: discover series (and their seasons) so bonus files can be sent
    # to the right series' Extras folder even if they are processed first.
    episodes: list[tuple[Path, tuple[str, Optional[int], int, int]]] = []
    series_hints: dict[str, tuple[str, Optional[int], int, int]] = {}
    hints_by_title: dict[str, str] = {}
    observed: dict[str, set[int]] = {}
    if not cfg.get("no_tv"):
        for video in videos:
            ep = parse_episode(video.name)
            if not ep:
                continue
            episodes.append((video, ep))
            key = series_key(ep[0], ep[1])
            series_hints.setdefault(key, ep)
            hints_by_title.setdefault(title_key(ep[0]), key)
            observed.setdefault(key, set()).add(ep[2])
        if series_hints:
            log.info(f"Detected {len(series_hints)} TV series in this batch")

    states: dict[str, SeriesState] = {}

    def state_for(key: str) -> SeriesState:
        if key not in states:
            title, year, _, _ = series_hints[key]
            states[key] = setup_series(key, title, year, observed.get(key, set()),
                                       dest, cfg, log, cfg.get("dry_run", False))
        return states[key]

    for i, video in enumerate(videos, 1):
        log.info(f"[{i}/{len(videos)}] {video.name}")
        try:
            extra_info = None if cfg.get("no_extras") else detect_extra(video, source)
            if extra_info:
                parent_title, parent_year = "", None
                if extra_info.parent_source:
                    parent_ep = parse_episode(extra_info.parent_source + video.suffix)
                    if parent_ep:
                        # e.g. "Breaking Bad S01E01 - Behind the Scenes"
                        parent_title, parent_year = parent_ep[0], parent_ep[1]
                    else:
                        parent_title, parent_year = parse_filename(
                            extra_info.parent_source + video.suffix
                        )
                key = series_key(parent_title, parent_year)
                if key not in series_hints:
                    key = hints_by_title.get(title_key(parent_title), key)
                if not cfg.get("no_tv") and key in series_hints:
                    state = state_for(key)
                    plan = ExtraPlan(
                        video=video, extra_title=extra_info.extra_title,
                        parent_title=state.title, parent_year=state.year,
                        folder=state.extras_folder,
                        sidecars=find_sidecars(video, all_files),
                    )
                else:
                    plan = build_extra_plan(video, extra_info, source, dest,
                                            cfg, all_files, log)
                execute_extra_plan(plan, cfg, log, cfg.get("dry_run", False))
                continue

            ep = parse_episode(video.name) if not cfg.get("no_tv") else None
            if ep:
                state = state_for(series_key(ep[0], ep[1]))
                plan = build_tv_plan(video, state, ep[2], ep[3], cfg, all_files, log)
                execute_tv_plan(plan, cfg, log, cfg.get("dry_run", False))
                continue

            plan = build_plan(video, source, dest, cfg, all_files, log)
            execute_plan(plan, cfg, log, cfg.get("dry_run", False))
        except Exception as exc:  # noqa: BLE001 - one bad file should not stop the run
            log.error(f"failed to process {video.name}: {exc}")
    return 0


def watch_mode(cfg: dict, log: Log) -> int:
    interval = max(5, int(cfg.get("interval", 30)))
    log.info(f"Watching {cfg['source']} every {interval}s (Ctrl+C to stop)")
    try:
        while True:
            organize_once(cfg, log)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Stopped watching.")
    return 0


# --------------------------------------------------------------------------- #
# Config + CLI
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    cfg: dict = {}
    if path.is_file():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] could not read config {path}: {exc}", file=sys.stderr)
    return cfg


def build_arg_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        description="Organize videos into Plex-style '<Title> (<Year>)' folders "
                    "with artwork and info files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", help="folder containing videos (default: script folder)")
    p.add_argument("--dest", help="library folder to create movie folders in (default: source)")
    p.add_argument("--config", default=str(here / DEFAULT_CONFIG_NAME),
                   help="path to JSON config file")
    p.add_argument("--recursive", action="store_true", help="scan subfolders")
    p.add_argument("--dry-run", action="store_true", help="preview changes without applying them")
    p.add_argument("--watch", action="store_true", help="continuously watch for new files")
    p.add_argument("--interval", type=int, default=30, help="watch polling interval in seconds")
    p.add_argument("--provider", choices=["auto", "tmdb", "omdb", "none"],
                   help="metadata provider (overrides config)")
    p.add_argument("--default-year", help="year to use when one cannot be detected")
    p.add_argument("--no-metadata", action="store_true",
                   help="skip all network lookups; use filename only")
    p.add_argument("--keep-video-name", action="store_true",
                   help="do not rename the video file to '<Title> (<Year>)'")
    p.add_argument("--extra-folder-name", help="name of the bonus-content subfolder")
    p.add_argument("--no-extras", action="store_true",
                   help="disable extra detection (treat every video as a main movie)")
    p.add_argument("--no-tv", action="store_true",
                   help="disable TV episode detection (treat everything as a movie)")
    p.add_argument("--tv-extras-folder-name",
                   help="name of the series-level bonus folder (default: Extras)")
    p.add_argument("--season-padding", type=int,
                   help="digits for season folders, e.g. 2 -> Season 01")
    p.add_argument("--no-episode-title", action="store_true",
                   help="leave episode titles out of episode filenames")
    p.add_argument("--quiet", action="store_true", help="suppress progress output")
    p.add_argument("--version", action="version", version=f"organize_plex {VERSION}")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    here = Path(__file__).resolve().parent
    cfg = load_config(Path(args.config).expanduser())
    cfg.setdefault("source", str(here))
    cfg.setdefault("dest", cfg["source"])
    cfg.setdefault("extra_folder_name", "Extra")
    cfg.setdefault("tv_extras_folder_name", "Extras")
    cfg.setdefault("season_padding", 2)
    cfg.setdefault("include_episode_title", True)
    cfg.setdefault("fetch_all_season_metadata", True)

    # CLI options override config.
    if args.source:
        cfg["source"] = args.source
    if args.dest:
        cfg["dest"] = args.dest
    if args.recursive:
        cfg["recursive"] = True
    if args.dry_run:
        cfg["dry_run"] = True
    if args.watch:
        cfg["watch"] = True
    cfg["interval"] = args.interval
    if args.provider:
        cfg["provider"] = args.provider
    if args.default_year:
        cfg["default_year"] = args.default_year
    if args.no_metadata:
        cfg["no_metadata"] = True
    if args.keep_video_name:
        cfg["keep_video_name"] = True
    if args.extra_folder_name:
        cfg["extra_folder_name"] = args.extra_folder_name
    if args.no_extras:
        cfg["no_extras"] = True
    if args.no_tv:
        cfg["no_tv"] = True
    if args.tv_extras_folder_name:
        cfg["tv_extras_folder_name"] = args.tv_extras_folder_name
    if args.season_padding is not None:
        cfg["season_padding"] = args.season_padding
    if args.no_episode_title:
        cfg["include_episode_title"] = False

    log = Log(verbose=not args.quiet)
    if cfg.get("watch"):
        return watch_mode(cfg, log)
    return organize_once(cfg, log)


if __name__ == "__main__":
    raise SystemExit(main())
