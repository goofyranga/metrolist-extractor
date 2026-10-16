# Metrolist Extractor

Current version: **0.6.0**

Metrolist Extractor exports songs that **you explicitly downloaded for offline use** in the
Metrolist Android app running under Waydroid. It reconstructs AndroidX Media3 cache spans and
uses FFmpeg stream copy, selecting a normal output container for each downloaded audio codec
without re-encoding or losing quality. It also downloads Metrolist's artwork and embeds it using
the metadata mechanism appropriate for that container.

This first release targets the storage layout verified on Metrolist package
`com.metrolist.music`:

```text
song.db song ID
    ↕
ExoPlayerCacheIndex<download UID>: numeric cache ID → song ID
    ↕
files/download/*/<cache ID>.<byte offset>.<timestamp>.v3.exo
```

## Requirements

- Linux with a working Waydroid installation
- Metrolist installed in Waydroid
- `ffmpeg`
- Python 3.10 or newer
- `sqlite3` available inside Waydroid (present in the tested setup)

On Linux Mint/Ubuntu:

```bash
sudo apt install ffmpeg python3-venv pipx
```

## Install

From this project directory:

```bash
pipx install .
```

Or run it immediately without installing anything:

```bash
chmod +x metrolist-extract
./metrolist-extract doctor
```

For development, or if you want changes to take effect immediately:

```bash
pipx install --editable .
```

## Use

Show the complete command reference, or focus on one command:

```bash
metrolist-extract help
metrolist-extract help export
metrolist-extract help list
```

Check the setup:

```bash
metrolist-extract doctor
```

List complete offline downloads:

```bash
metrolist-extract list
```

Artist lookup is layered. The extractor first uses Metrolist's song and album relationships,
then compatible denormalized metadata, then artists learned from songs on the same album. If
Metrolist did not persist any of those, it looks up the YouTube video's public author metadata.
Generated music-channel names such as `ayowitty - Topic` are normalized to `ayowitty`.

Successful online results are cached at
`~/.cache/metrolist-extractor/artists.json`, so later lists and exports do not repeat hundreds of
requests. To retry names that are still unavailable:

```bash
metrolist-extract list --refresh-artists
```

To skip only online artist lookup and accept `Unknown Artist` where the database has no relationship:

```bash
metrolist-extract list --offline-artists
metrolist-extract export --offline-artists
```

Genre and album lookup are also automatic. Metrolist metadata is preferred. For a missing album,
the extractor checks the exact YouTube Music video metadata, then strongly matched YouTube Music
song-search results, and finally Apple's public music catalogue. Search candidates are scored
using title, artist, and duration so unrelated albums are not guessed. Successful matches are cached at
`~/.cache/metrolist-extractor/catalog.json`.

To refresh or disable catalogue matching:

```bash
metrolist-extract list --refresh-catalog
metrolist-extract export --offline-catalog
```

Online metadata lookups can also be disabled independently:

```bash
metrolist-extract export --no-artist-lookup
metrolist-extract export --no-album-lookup
metrolist-extract export --no-genre-lookup
```

To disable all three online searches at once while still using metadata already stored by
Metrolist:

```bash
metrolist-extract export --no-online-metadata
```

Preview an export:

```bash
metrolist-extract export --dry-run
```

Export every complete offline download:

```bash
metrolist-extract export
```

Exports run four tracks concurrently by default. Tune this for your machine with:

```bash
metrolist-extract export --jobs 6
metrolist-extract export --jobs 1  # sequential troubleshooting mode
```

Four jobs is recommended for typical laptops. Higher values can be faster on SSD-based systems,
but may overload Waydroid or cause more network/DNS failures when artwork is being fetched.

The output extension is selected automatically after probing the cached audio:

| Audio codec | Output |
| --- | --- |
| Opus | `.opus` |
| Vorbis | `.ogg` |
| AAC or ALAC | `.m4a` |
| MP3 | `.mp3` |
| FLAC | `.flac` |
| Other stream-copy-compatible codecs | `.mka` |

Every route uses stream copy. Artwork is stored as an Ogg picture comment, attached cover stream,
or Matroska attachment as appropriate.

The default output directory is `~/Music/Metrolist`. Choose another directory with:

```bash
metrolist-extract export --output /path/to/music
```

Existing files are skipped. To replace them:

```bash
metrolist-extract export --overwrite
```

Use `--overwrite` when upgrading files exported by version 0.1.x so they receive artwork.
For version 0.6.0's improved metadata and parallel export, exporting into a new empty folder
is recommended so old `Unknown Artist` files do not remain beside corrected copies.

Artwork is automatic. If you need an audio-only export:

```bash
metrolist-extract export --no-artwork
```

Normally an artwork network error produces a warning while preserving the audio export. To make
missing artwork fail that track instead:

```bash
metrolist-extract export --require-artwork
```

The command normally uses `sudo waydroid shell`. If your account can run the Waydroid shell
directly, add `--no-sudo` before the subcommand:

```bash
metrolist-extract --no-sudo export
```

## Alternate Metrolist and Waydroid locations

The extractor automatically checks the normal `/data/user/0/com.metrolist.music` location, its
`/data/data` compatibility path, and matching app data belonging to other Android user profiles.
The `doctor` command prints every final path it selected.

All locations can be changed temporarily. Global location flags go before the subcommand:

```bash
metrolist-extract \
  --waydroid /custom/bin/waydroid \
  --app-root /data/user/10/com.metrolist.music \
  doctor
```

Individual directories or files can also be overridden:

```bash
metrolist-extract \
  --database-dir /alternate/databases \
  --download-dir /alternate/files/download \
  --song-db /alternate/databases/song.db \
  --exoplayer-db /alternate/databases/exoplayer_internal.db \
  list
```

For a Metrolist fork with a different Android package:

```bash
metrolist-extract --package example.metrolist.fork doctor
```

### Save location changes

Settings can be saved once instead of supplying flags every time:

```bash
metrolist-extract config set app_root /data/user/10/com.metrolist.music
metrolist-extract config set waydroid /custom/bin/waydroid
metrolist-extract config set output_dir /home/me/Music/Metrolist
metrolist-extract config show
```

Restore automatic discovery for a setting with:

```bash
metrolist-extract config unset app_root
```

Supported settings are:

| Setting | Purpose |
| --- | --- |
| `package` | Metrolist Android package name |
| `waydroid` | Waydroid executable name or absolute path |
| `use_sudo` | Whether to run the Waydroid shell through `sudo` |
| `app_root` | Complete Android app-data root |
| `database_dir` | Directory containing the two databases |
| `download_dir` | Media3 offline-download cache directory |
| `song_db` | Exact `song.db` path |
| `exoplayer_db` | Exact `exoplayer_internal.db` path |
| `output_dir` | Default Linux export directory |

The default config file is `~/.config/metrolist-extractor/config.toml`. Use the global
`--config-file` option to select a different one.

## What it does safely

1. Uses SQLite's online backup command inside Android to snapshot both live databases, including
   data that may otherwise still be in WAL files.
2. Finds the download cache UID from `files/download/*.uid`, rather than assuming a generated
   ExoPlayer table name.
3. Groups spans by the first number in each `.v3.exo` filename and sorts them by the second
   number (the original byte offset).
4. Rejects tracks with gaps or overlaps rather than exporting corrupt audio.
5. Joins the ExoPlayer key to Metrolist's song metadata. Album lookup combines the song row,
   canonical song-album relationship, album catalog, recognition history, and speed-dial data.
   Artist lookup combines song, album,
   album-sibling, recognition, and speed-dial metadata, then uses cached YouTube author metadata
   only for tracks that remain unresolved. Missing albums are first resolved against YouTube
   Music's exact video ID and typed album link, then strongly matched YouTube Music song search,
   with the scored Apple catalogue as a final fallback.
6. Fetches the stored artwork URL, first trying a 544×544 YouTube Music thumbnail when available.
7. Probes the actual audio codec and runs `ffmpeg -c:a copy` into a compatible output container.
8. Writes title/artist/album/year/genre/song ID and adds a standard front-cover picture without
   re-encoding the audio stream.
9. Verifies the completed file contains its expected artwork representation.
10. Writes to a partial file and only moves it into place after FFmpeg and verification succeed.

It only reads Metrolist's private data and removes its own temporary database snapshots from
`/data/local/tmp`. It does not modify Metrolist's library or downloads.

## Current limitations

- Tested against one current Metrolist/Waydroid database layout. The CLI detects generated cache
  table names, but a future Metrolist or Media3 schema change may need an update.
- Output is a flat `Artist - Title.<format>` directory.
- Incomplete/in-progress downloads are deliberately ignored or rejected.

Only export media you are legally entitled to copy, and respect the service terms and copyright
law that apply to you.

## Run tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

The runtime package has no Python dependencies beyond the standard library.

## Changes in 0.6.0

- Adds `metrolist-extract help` for the complete option reference and `help <command>` for focused help.
- Reworks doctor, list, lookup, export, warning, and summary output into clearer grouped sections.
- Adds colour when attached to a compatible terminal while retaining clean redirected output.
- Reports title, artist, album, genre, year, song ID, and artwork status for every exported file.
- Adds strongly scored YouTube Music song-search matching when exact-video metadata has no album.
- Requires typed album relationships and strong title/artist agreement to avoid unrelated guesses.

## Changes in 0.5.3

- Adds independent `--no-artist-lookup`, `--no-album-lookup`, and `--no-genre-lookup` flags.
- Adds `--no-online-metadata` to disable all three searches at once.
- Keeps the older `--offline-artists` and `--offline-catalog` options compatible.
- Preserves cached metadata fields when only one type of catalogue lookup is enabled.

## Changes in 0.5.2

- Resolves missing albums from YouTube Music using the exact Metrolist/YouTube video ID.
- Reads only typed album links, avoiding fuzzy title guesses when YouTube Music has a canonical album.
- Recovers the artist and year exposed alongside that album when available.
- Treats placeholders such as `Unknown Album` and `Unknown Genre` as missing metadata.
- Reports both album and genre counts that genuinely remain unavailable after lookup.

## Changes in 0.5.1

- Exports four tracks concurrently instead of processing the entire library sequentially.
- Adds `--jobs N`, including `--jobs 1` for the original sequential behaviour.
- Gives concurrent workers independent partial files before atomic completion.
- Coordinates shared artwork requests so tracks from the same album reuse one download.

## Changes in 0.5.0

- Adds `GENRE` tags without re-encoding audio.
- Reads local recognition-history genre metadata when available.
- Uses scored Apple music-catalogue matches for missing genres and albums.
- Matches on title, artist, and duration to reduce false positives.
- Caches successful catalogue results and adds refresh/offline/worker/cache options.
- Shows resolved genre alongside album in `list` output.

## Changes in 0.4.1

- Resolves albums through Metrolist's canonical `song_album_map` and `album` tables when the
  denormalized song row has no album name.
- Falls back to recognition-history and speed-dial album labels when available.
- Recovers album year and artwork from the album catalog when those song fields are missing.
- Shows the resolved album in `list` output for easy verification before exporting.

## Changes in 0.4.0

- Fixes artist coverage when Metrolist downloaded audio without persisting a song-artist row.
- Adds album-sibling, recognition-history, and speed-dial metadata fallbacks.
- Uses YouTube's public author metadata only for tracks that remain unresolved and removes the
  generated `- Topic` suffix.
- Resolves missing names concurrently and caches successful results between runs.
- Adds `--offline-artists`, `--refresh-artists`, `--artist-cache`, and `--artist-workers`.

## Changes in 0.3.0

- Detects the cached audio codec instead of assuming every track is Opus.
- Losslessly exports Opus, Vorbis, AAC, ALAC, MP3 and FLAC, with Matroska audio fallback.
- Embeds and verifies artwork across Ogg, MP4/M4A, MP3, FLAC and Matroska containers.
- Combines `sorted_song_artist_map` and `song_artist_map`, preserving order and removing duplicates.
- Falls back through `album_artist_map` when a track has no direct artist relationship.
- Reports the detected codec and selected extension for every exported track.

## Changes in 0.2.0

- Downloads and embeds album artwork automatically, with an explicit verification step.
- Discovers Metrolist across standard, compatibility, and multi-user Android app-data roots.
- Adds exact path/package/Waydroid overrides and persistent configuration commands.
- Shows resolved paths in `doctor` and artwork availability in `list`.
- Adds `--no-artwork` and `--require-artwork` export policies.

## Changes in 0.1.1

- Passes Android commands after Waydroid's `--` option boundary, preventing Waydroid from
  misreading options such as `find -type` and `find -printf` as its own arguments.
