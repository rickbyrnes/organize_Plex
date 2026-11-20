# organize_Plex

A dependency-free Python tool that organizes video files into Plex-style
movie **and TV series** folders. It uses only the Python 3 standard library —
nothing to install.

## Movies

```
Movies/
└── Inception (2010)/
    ├── Inception (2010).mkv     ← the video
    ├── Inception (2010).en.srt  ← matching subtitles / sidecars
    ├── poster.jpg               ← cover art
    ├── fanart.jpg               ← background art
    ├── movie.nfo                ← rich metadata (Plex/Kodi compatible)
    ├── info.json                ← raw metadata for reference
    └── Extra/                   ← bonus content for this movie
        ├── Trailer.mp4
        ├── Trailer.en.srt
        └── Trailer.info.json
```

## TV series

The **first** episode found for a series builds the entire structure — every
season folder, an `Extras` folder, and metadata at series and season level:

```
TV Shows/
└── Breaking Bad (2008)/
    ├── poster.jpg, fanart.jpg, tvshow.nfo, info.json
    ├── Season 01/
    │   ├── poster.jpg, season.nfo, info.json
    │   ├── Breaking Bad (2008) - S01E01 - Pilot.mkv
    │   ├── Breaking Bad (2008) - S01E01 - Pilot.en.srt
    │   ├── Breaking Bad (2008) - S01E01 - Pilot.jpg      ← episode still
    │   ├── Breaking Bad (2008) - S01E01 - Pilot.info.json
    │   └── Breaking Bad (2008) - S01E01 - Pilot.nfo
    ├── Season 02/
    ├── Season 03/
    │   └── …
    └── Extras/
        ├── Trailer.mp4
        └── Trailer.info.json
```

Later episodes of the same series land in the correct season automatically.
Recognized episode formats: `S01E01`, `s01e01`, `S01.E01`, `1x01`, and
`Season 1 Episode 1`. TV bonus files go to the series-level `Extras/` folder.

## Quick start

```bash
# 1. Preview (makes no changes)
python3 organize_plex.py --dry-run

# 2. Organize files in this folder, in place
python3 organize_plex.py

# 3. Organize a downloads folder into a separate library, recursively
python3 organize_plex.py --source /downloads --dest /Media/Library --recursive

# 4. Keep watching a folder and auto-organize anything new
python3 organize_plex.py --source /downloads --dest /Media/Library --watch
```

By default `--source` and `--dest` are the folder the script lives in. Movie
folders are named `<Title> (<Year>)`; series folders use the same pattern with
season subfolders beneath.

## Enabling artwork and info (recommended)

Without an API key the tool still groups, renames and moves your files, but it
cannot fetch artwork or write detailed info. For TV, a **TMDb key is required**
to know the total number of seasons and to get episode titles/stills. Get a
free key and put it in `config.json`:

- **TMDb** (recommended — series, seasons, episodes, artwork): https://www.themoviedb.org/settings/api
- **OMDb** (fallback — year/plot/poster and season count): https://www.omdbapi.com/apikey.aspx

```json
{
  "tmdb_api_key": "YOUR_TMDB_KEY",
  "omdb_api_key": "YOUR_OMDB_KEY",
  "provider": "auto"
}
```

Keys can also be supplied via the `TMDB_API_KEY` / `OMDB_API_KEY` environment
variables. Without a key, the full folder tree is still created using only the
seasons actually seen in your files.

## Configuration

All settings in `config.json` can be overridden on the command line:

| Setting | CLI flag | Meaning |
|---|---|---|
| `source` | `--source` | Folder to scan for videos |
| `dest` | `--dest` | Folder where the library is created |
| `recursive` | `--recursive` | Also scan subfolders |
| `provider` | `--provider` | Metadata provider |
| `default_year` | `--default-year` | Year used when none is found in the filename |
| `keep_video_name` | `--keep-video-name` | Don't rename movie videos to `<Title> (<Year>)` |
| `extra_folder_name` | `--extra-folder-name` | Movie bonus folder (default `Extra`) |
| `no_extras` | `--no-extras` | Disable extra detection |
| `no_tv` | `--no-tv` | Disable TV detection (treat everything as a movie) |
| `tv_extras_folder_name` | `--tv-extras-folder-name` | Series bonus folder (default `Extras`) |
| `season_padding` | `--season-padding` | Digits for seasons, e.g. `2` → `Season 01` |
| `include_episode_title` | `--no-episode-title` | Omit episode titles from filenames |
| `fetch_all_season_metadata` | — | Create all seasons from metadata (default true) |
| `interval` | `--interval` | Watch-mode polling interval (seconds) |

Run `python3 organize_plex.py --help` for the full list.

## How titles and years are determined

1. Release tags (`1080p`, `x264`, `BluRay`, …) are stripped from the filename.
2. A year in `(2010)`, `[2010]`, or plain `2010` is detected.
3. For TV, the episode token (`S01E01`, etc.) is removed to reveal the series
   title and season/episode numbers.
4. If an API key is set, the provider is queried for authoritative titles,
   years, artwork and episode details. Otherwise the cleaned filename is used
   (or `default_year` / the current year).

> Tip: naming files `Series Name (YYYY) S01E01.ext` or
> `Movie Name (YYYY).ext` gives the best results without an API key.

## Files created

- **Movies:** `movie.nfo`, `info.json`, `poster.jpg`, `fanart.jpg`.
- **Series:** `tvshow.nfo`, `info.json`, `poster.jpg`, `fanart.jpg` at the root;
  `season.nfo`, `info.json`, `poster.jpg` in each season folder.
- **Episodes:** `<episode>.nfo` (`episodedetails`), `<episode>.info.json`, and a
  still image `<episode>.jpg` when TMDb provides one.
- Matching `.srt`, `.ass`, existing `.jpg`/`.nfo`, etc. are moved in and renamed
  to match the video/extra/episode.
