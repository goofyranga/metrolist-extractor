from __future__ import annotations

import os
import re
import shlex
import shutil
import sqlite3
import base64
import difflib
import json
import struct
import subprocess
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence


PACKAGE = "com.metrolist.music"
MAX_ARTWORK_BYTES = 1_500_000
YOUTUBE_OEMBED = "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={song_id}&format=json"
ITUNES_SEARCH = "https://itunes.apple.com/search"
YOUTUBE_MUSIC_NEXT = "https://music.youtube.com/youtubei/v1/next"
YOUTUBE_MUSIC_SEARCH = "https://music.youtube.com/youtubei/v1/search"
YOUTUBE_MUSIC_CLIENT_VERSION = "1.20260707.12.00"
YOUTUBE_MUSIC_SONG_FILTER = "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"

SPAN_RE = re.compile(r"^(?P<cache_id>\d+)\.(?P<offset>\d+)\.(?P<stamp>\d+)\.v3\.exo$")
UID_RE = re.compile(r"^(?P<uid>[0-9a-fA-F]+)\.uid$")


class ExtractorError(RuntimeError):
    """A user-actionable extraction error."""


@dataclass(frozen=True)
class CacheSpan:
    cache_id: int
    offset: int
    timestamp: int
    size: int
    remote_path: str


@dataclass(frozen=True)
class MetrolistPaths:
    app_root: str
    database_dir: str
    download_dir: str
    song_db: str
    exoplayer_db: str


@dataclass(frozen=True)
class Artwork:
    data: bytes
    mime_type: str
    source_url: str


@dataclass(frozen=True)
class ExportResult:
    path: Path
    codec: str
    container: str
    artwork_embedded: bool
    artwork_warning: str | None = None


@dataclass(frozen=True)
class AudioFormat:
    codec: str
    extension: str
    muxer: str
    artwork_mode: str


@dataclass(frozen=True)
class AlbumMetadata:
    album_id: str | None
    title: str | None
    year: int | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True)
class CatalogMetadata:
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    year: int | None = None


@dataclass
class Track:
    cache_id: int
    song_id: str
    title: str
    album: str | None
    year: int | None
    duration_seconds: int | None
    thumbnail_url: str | None
    artists: list[str] = field(default_factory=list)
    spans: list[CacheSpan] = field(default_factory=list)
    album_id: str | None = None
    artist_source: str = "missing"
    album_source: str = "missing"
    genre: str | None = None
    genre_source: str = "missing"

    @property
    def artist(self) -> str:
        return ", ".join(self.artists) if self.artists else "Unknown Artist"

    @property
    def complete_size(self) -> int:
        return sum(span.size for span in self.spans)


def parse_span_line(line: str) -> CacheSpan | None:
    """Parse `find -printf '%s %p\n'` output for a Media3 v3 cache span."""
    try:
        size_text, remote_path = line.strip().split(maxsplit=1)
        size = int(size_text)
    except (ValueError, TypeError):
        return None
    match = SPAN_RE.match(remote_path.rsplit("/", 1)[-1])
    if not match:
        return None
    return CacheSpan(
        cache_id=int(match.group("cache_id")),
        offset=int(match.group("offset")),
        timestamp=int(match.group("stamp")),
        size=size,
        remote_path=remote_path,
    )


def validate_spans(spans: Sequence[CacheSpan]) -> list[CacheSpan]:
    """Return spans in offset order, rejecting missing or overlapping bytes."""
    ordered = sorted(spans, key=lambda item: item.offset)
    if not ordered:
        raise ExtractorError("download has no cache spans")
    expected = 0
    for span in ordered:
        if span.offset != expected:
            relation = "overlap" if span.offset < expected else "gap"
            raise ExtractorError(
                f"cache span {relation}: expected byte {expected}, found {span.offset}"
            )
        expected += span.size
    return ordered


def safe_component(value: str, fallback: str = "Unknown") -> str:
    """Produce a readable Linux filename component without path/control chars."""
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("/", "／").replace("\\", "⧵")
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        text = fallback
    # Keep well below common 255-byte component limits while retaining Unicode.
    encoded = text.encode("utf-8")
    if len(encoded) > 180:
        encoded = encoded[:180]
        while True:
            try:
                text = encoded.decode("utf-8").rstrip()
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
    return text


def meaningful_metadata(value: object, kind: str) -> str | None:
    """Discard placeholders that media players would otherwise treat as real tags."""
    if value is None:
        return None
    text = str(value).strip()
    placeholders = {
        "", "unknown", "n/a", "none", "null",
        f"unknown {kind}", f"no {kind}",
    }
    return None if text.casefold() in placeholders else text


def output_name(track: Track, extension: str = "opus") -> str:
    extension = extension.lstrip(".")
    return f"{safe_component(track.artist)} - {safe_component(track.title)}.{extension}"


def audio_format_for_codec(codec: str) -> AudioFormat:
    plans = {
        "opus": AudioFormat(codec, "opus", "opus", "comment"),
        "vorbis": AudioFormat(codec, "ogg", "ogg", "comment"),
        "aac": AudioFormat(codec, "m4a", "ipod", "attached_pic"),
        "alac": AudioFormat(codec, "m4a", "ipod", "attached_pic"),
        "mp3": AudioFormat(codec, "mp3", "mp3", "attached_pic"),
        "flac": AudioFormat(codec, "flac", "flac", "attached_pic"),
    }
    return plans.get(codec, AudioFormat(codec, "mka", "matroska", "attachment"))


def probe_audio_codec(path: Path) -> str:
    if shutil.which("ffprobe") is None:
        raise ExtractorError("ffprobe is required; on Ubuntu/Mint run: sudo apt install ffmpeg")
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "json", str(path),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        streams = json.loads(result.stdout).get("streams", [])
        codec = streams[0].get("codec_name") if streams else None
        if not codec:
            raise ExtractorError("no audio stream was found in the reconstructed cache file")
        return str(codec).lower()
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise ExtractorError(f"ffprobe could not identify the cached audio: {detail.strip()}") from error


def artwork_urls(url: str) -> list[str]:
    """Try a useful-size YouTube Music thumbnail before the stored small variant."""
    upgraded = re.sub(r"=w\d+-h\d+", "=w544-h544", url, count=1)
    return [upgraded, url] if upgraded != url else [url]


def image_mime(data: bytes, content_type: str | None = None) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime.startswith("image/"):
            return mime
    return None


def download_artwork(url: str, timeout: float = 20.0) -> Artwork:
    errors: list[str] = []
    for candidate in artwork_urls(url):
        request = urllib.request.Request(
            candidate,
            headers={"User-Agent": "Metrolist-Extractor/0.6 (+https://github.com/MetrolistGroup/Metrolist)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_ARTWORK_BYTES:
                    raise ExtractorError(f"artwork is larger than {MAX_ARTWORK_BYTES} bytes")
                data = response.read(MAX_ARTWORK_BYTES + 1)
                if len(data) > MAX_ARTWORK_BYTES:
                    raise ExtractorError(f"artwork is larger than {MAX_ARTWORK_BYTES} bytes")
                mime = image_mime(data, response.headers.get("Content-Type"))
                if not mime:
                    raise ExtractorError("server response is not a recognised image")
                return Artwork(data=data, mime_type=mime, source_url=candidate)
        except (OSError, ValueError, urllib.error.URLError, ExtractorError) as error:
            errors.append(str(error))
    raise ExtractorError("could not download artwork: " + "; ".join(errors))


def metadata_picture_block(artwork: Artwork) -> str:
    """Create a base64 FLAC picture block for the Vorbis/Opus comment standard."""
    mime = artwork.mime_type.encode("utf-8")
    description = b"Cover"
    block = b"".join(
        (
            struct.pack(">I", 3),  # Front cover
            struct.pack(">I", len(mime)), mime,
            struct.pack(">I", len(description)), description,
            struct.pack(">IIII", 0, 0, 0, 0),  # Unknown dimensions/depth/colour count
            struct.pack(">I", len(artwork.data)), artwork.data,
        )
    )
    return base64.b64encode(block).decode("ascii")


def write_picture_metadata(path: Path, artwork: Artwork) -> None:
    value = metadata_picture_block(artwork)
    path.write_text(f";FFMETADATA1\nMETADATA_BLOCK_PICTURE={value}\n", encoding="ascii")


def has_embedded_artwork(path: Path) -> bool:
    """Check the final OpusTags packet for the standard picture field."""
    # OpusTags precedes audio packets, and the configured artwork limit keeps
    # this field below roughly 2 MiB after base64 expansion.
    with path.open("rb") as source:
        return b"METADATA_BLOCK_PICTURE=" in source.read(3_000_000)


def artwork_extension(mime_type: str) -> str:
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(mime_type, "img")


def write_artwork_file(directory: Path, artwork: Artwork) -> Path:
    path = directory / f"cover.{artwork_extension(artwork.mime_type)}"
    path.write_bytes(artwork.data)
    return path


def probe_streams(path: Path) -> list[dict]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type:stream_disposition=attached_pic:stream_tags=filename,mimetype",
                "-of", "json", str(path),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return list(json.loads(result.stdout).get("streams", []))
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise ExtractorError(f"could not verify output streams: {detail.strip()}") from error


def verify_artwork(path: Path, mode: str) -> bool:
    if mode == "comment":
        return has_embedded_artwork(path)
    streams = probe_streams(path)
    if mode == "attached_pic":
        return any(
            stream.get("codec_type") == "video"
            and stream.get("disposition", {}).get("attached_pic") == 1
            for stream in streams
        )
    if mode == "attachment":
        return any(stream.get("codec_type") == "attachment" for stream in streams)
    return False


class AndroidBridge:
    def __init__(self, use_sudo: bool = True, waydroid: str = "waydroid") -> None:
        # `--` is essential here: Waydroid uses argparse for its own CLI and
        # otherwise consumes Android-command options such as find's `-type`.
        self.prefix = (["sudo"] if use_sudo and os.geteuid() != 0 else []) + [
            waydroid,
            "shell",
            "--",
        ]

    def command(self, *args: str) -> list[str]:
        return [*self.prefix, *args]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self.command(*args),
                check=check,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            missing = self.prefix[0]
            raise ExtractorError(f"required command not found: {missing}") from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or str(error)).strip()
            raise ExtractorError(f"Waydroid command failed: {detail}") from error

    def is_available(self) -> tuple[bool, str]:
        result = self.run("true", check=False)
        if result.returncode == 0:
            return True, "Waydroid shell is reachable"
        detail = (result.stderr or result.stdout).strip()
        return False, detail or "Waydroid shell is not reachable"

    def path_exists(self, remote_path: str, kind: str = "e") -> bool:
        result = self.run("test", f"-{kind}", remote_path, check=False)
        return result.returncode == 0

    def discover_app_roots(self, package: str) -> list[str]:
        candidates = [f"/data/user/0/{package}", f"/data/data/{package}"]
        result = self.run(
            "find", "/data/user", "-mindepth", "2", "-maxdepth", "2",
            "-type", "d", "-name", package, "-print", check=False,
        )
        if result.returncode == 0:
            candidates.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
        unique: list[str] = []
        for candidate in candidates:
            normalized = candidate.rstrip("/")
            if normalized not in unique:
                unique.append(normalized)
        return unique

    def copy_remote(self, remote_path: str, destination: Path | BinaryIO) -> None:
        close_output = False
        if isinstance(destination, Path):
            output: BinaryIO = destination.open("wb")
            close_output = True
        else:
            output = destination
        try:
            process = subprocess.run(
                self.command("cat", remote_path),
                stdout=output,
                stderr=subprocess.PIPE,
            )
        finally:
            if close_output:
                output.close()
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", "replace").strip()
            raise ExtractorError(f"could not copy {remote_path}: {detail}")

    def snapshot_database(self, remote_db: str, local_db: Path, label: str) -> None:
        remote_backup = f"/data/local/tmp/metrolist-extractor-{os.getpid()}-{label}.db"
        try:
            self.run("sqlite3", remote_db, f".backup '{remote_backup}'")
            self.copy_remote(remote_backup, local_db)
        finally:
            self.run("rm", "-f", remote_backup, check=False)

    def list_spans(self, download_dir: str) -> tuple[list[CacheSpan], str | None]:
        result = self.run(
            "find", download_dir, "-maxdepth", "2", "-type", "f", "-printf", "%s %p\\n"
        )
        spans: list[CacheSpan] = []
        uid: str | None = None
        for line in result.stdout.splitlines():
            span = parse_span_line(line)
            if span:
                spans.append(span)
                continue
            try:
                _size, path = line.strip().split(maxsplit=1)
            except ValueError:
                continue
            match = UID_RE.match(path.rsplit("/", 1)[-1])
            if match:
                uid = match.group("uid")
        return spans, uid


def resolve_paths(
    bridge: AndroidBridge,
    package: str = PACKAGE,
    app_root: str | None = None,
    database_dir: str | None = None,
    download_dir: str | None = None,
    song_db: str | None = None,
    exoplayer_db: str | None = None,
) -> MetrolistPaths:
    roots = [app_root.rstrip("/")] if app_root else bridge.discover_app_roots(package)
    if not roots and not (database_dir and download_dir):
        raise ExtractorError(f"could not discover Android app data for package {package}")

    attempted: list[str] = []
    for root in roots or [""]:
        resolved_database_dir = (database_dir or f"{root}/databases").rstrip("/")
        resolved_download_dir = (download_dir or f"{root}/files/download").rstrip("/")
        resolved_song_db = song_db or f"{resolved_database_dir}/song.db"
        resolved_exoplayer_db = exoplayer_db or f"{resolved_database_dir}/exoplayer_internal.db"
        required = (resolved_song_db, resolved_exoplayer_db, resolved_download_dir)
        missing = [path for path in required if not bridge.path_exists(path, "d" if path == resolved_download_dir else "f")]
        if not missing:
            return MetrolistPaths(
                app_root=root or "(custom paths)",
                database_dir=resolved_database_dir,
                download_dir=resolved_download_dir,
                song_db=resolved_song_db,
                exoplayer_db=resolved_exoplayer_db,
            )
        attempted.append(f"{root or '(custom paths)'} (missing: {', '.join(missing)})")
    raise ExtractorError(
        "could not find a complete Metrolist data layout. Checked: " + "; ".join(attempted)
        + ". Override it with --app-root, --database-dir, --download-dir, --song-db, or --exoplayer-db."
    )


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    safe = table.replace("'", "''")
    return [row[1] for row in connection.execute(f"PRAGMA table_info('{safe}')")]


def choose_column(columns: Iterable[str], *candidates: str) -> str | None:
    by_lower = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    return None


def discover_cache_table(connection: sqlite3.Connection, uid: str | None) -> str:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ExoPlayerCacheIndex%'"
        )
    ]
    if uid:
        expected = f"ExoPlayerCacheIndex{uid}"
        for table in tables:
            if table.lower() == expected.lower():
                return table
    if len(tables) == 1:
        return tables[0]
    if not tables:
        raise ExtractorError("no ExoPlayer cache-index table was found")
    raise ExtractorError(
        "could not identify the download cache index; found " + ", ".join(tables)
    )


def load_relation_artists(
    connection: sqlite3.Connection,
    relation_tables: Sequence[str],
    entity_columns: Sequence[str],
) -> dict[str, list[str]]:
    tables = {
        row[0].lower(): row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    artist_table = tables.get("artist")
    if not artist_table:
        return {}

    artist_columns = table_columns(connection, artist_table)
    artist_id = choose_column(artist_columns, "id", "artistId", "artist_id")
    artist_name = choose_column(artist_columns, "name", "title", "artistName")
    if not artist_id or not artist_name:
        return {}

    artists: dict[str, list[str]] = {}
    for requested_table in relation_tables:
        map_table = tables.get(requested_table.lower())
        if not map_table:
            continue
        map_columns = table_columns(connection, map_table)
        entity_id = choose_column(map_columns, *entity_columns)
        mapped_artist_id = choose_column(map_columns, "artistId", "artist_id", "artistid")
        position = choose_column(
            map_columns, "position", "index", "order", "sortOrder", "sort_order"
        )
        if not entity_id or not mapped_artist_id:
            continue
        order = f", m.{quote_identifier(position)}" if position else ""
        sql = f"""
            SELECT m.{quote_identifier(entity_id)}, a.{quote_identifier(artist_name)}
            FROM {quote_identifier(map_table)} AS m
            JOIN {quote_identifier(artist_table)} AS a
              ON a.{quote_identifier(artist_id)} = m.{quote_identifier(mapped_artist_id)}
            ORDER BY m.{quote_identifier(entity_id)}{order}
        """
        for mapped_entity_id, name in connection.execute(sql):
            if not name:
                continue
            bucket = artists.setdefault(str(mapped_entity_id), [])
            normalized = str(name)
            if normalized not in bucket:
                bucket.append(normalized)
    return artists


def load_artist_maps(
    connection: sqlite3.Connection,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    # The sorted relationship is preferred because it retains display order.
    song_artists = load_relation_artists(
        connection,
        ("sorted_song_artist_map", "song_artist_map"),
        ("songId", "song_id", "songid"),
    )
    album_artists = load_relation_artists(
        connection,
        ("album_artist_map",),
        ("albumId", "album_id", "albumid"),
    )
    return song_artists, album_artists


def load_song_albums(
    connection: sqlite3.Connection,
) -> tuple[dict[str, AlbumMetadata], dict[str, AlbumMetadata]]:
    """Load canonical song-to-album relations and the album catalog."""
    objects = {
        row[0].lower(): row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    album_table = objects.get("album")
    if not album_table:
        return {}, {}

    album_columns = table_columns(connection, album_table)
    album_id = choose_column(album_columns, "id", "albumId", "album_id")
    album_title = choose_column(album_columns, "title", "name", "albumName")
    album_year = choose_column(album_columns, "year", "releaseYear")
    album_thumbnail = choose_column(album_columns, "thumbnailUrl", "artworkUrl", "coverUrl")
    if not album_id or not album_title:
        return {}, {}

    optional = [album_year, album_thumbnail]
    selected = [album_id, album_title, *[column for column in optional if column]]
    catalog: dict[str, AlbumMetadata] = {}
    sql = (
        f"SELECT {', '.join(quote_identifier(column) for column in selected)} "
        f"FROM {quote_identifier(album_table)}"
    )
    for row in connection.execute(sql):
        identifier = str(row[0])
        values = dict(zip(selected, row))
        catalog[identifier] = AlbumMetadata(
            album_id=identifier,
            title=str(values[album_title]) if values.get(album_title) else None,
            year=int(values[album_year]) if album_year and values.get(album_year) else None,
            thumbnail_url=(
                str(values[album_thumbnail])
                if album_thumbnail and values.get(album_thumbnail)
                else None
            ),
        )

    song_albums: dict[str, AlbumMetadata] = {}
    for requested_table in ("sorted_song_album_map", "song_album_map"):
        map_table = objects.get(requested_table)
        if not map_table:
            continue
        columns = table_columns(connection, map_table)
        song_id = choose_column(columns, "songId", "song_id", "videoId")
        mapped_album_id = choose_column(columns, "albumId", "album_id")
        position = choose_column(columns, "index", "position", "order", "sortOrder")
        if not song_id or not mapped_album_id:
            continue
        order = f", {quote_identifier(position)}" if position else ""
        relation_sql = (
            f"SELECT {quote_identifier(song_id)}, {quote_identifier(mapped_album_id)} "
            f"FROM {quote_identifier(map_table)} "
            f"ORDER BY {quote_identifier(song_id)}{order}"
        )
        for mapped_song_id, mapped_id in connection.execute(relation_sql):
            metadata = catalog.get(str(mapped_id))
            if mapped_song_id and metadata:
                song_albums.setdefault(str(mapped_song_id), metadata)
    return song_albums, catalog


def load_auxiliary_albums(connection: sqlite3.Connection) -> dict[str, AlbumMetadata]:
    """Read denormalized album labels from optional Metrolist feature tables."""
    objects = {
        row[0].lower(): row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    found: dict[str, AlbumMetadata] = {}
    for table_name, id_candidates, title_candidates, album_id_candidates in (
        (
            "recognition_history",
            ("youtubeVideoId", "videoId", "songId", "trackId"),
            ("album", "albumName"),
            (),
        ),
        (
            "speed_dial_item",
            ("id", "songId", "videoId"),
            ("albumName", "album", "collectionName"),
            ("albumId", "album_id"),
        ),
    ):
        table = objects.get(table_name)
        if not table:
            continue
        columns = table_columns(connection, table)
        song_id = choose_column(columns, *id_candidates)
        title = choose_column(columns, *title_candidates)
        mapped_album_id = choose_column(columns, *album_id_candidates) if album_id_candidates else None
        if not song_id or not title:
            continue
        selected = [song_id, title] + ([mapped_album_id] if mapped_album_id else [])
        sql = (
            f"SELECT {', '.join(quote_identifier(column) for column in selected)} "
            f"FROM {quote_identifier(table)}"
        )
        for row in connection.execute(sql):
            if not row[0] or not row[1]:
                continue
            found.setdefault(
                str(row[0]),
                AlbumMetadata(
                    album_id=str(row[2]) if mapped_album_id and row[2] else None,
                    title=str(row[1]),
                ),
            )
    return found


def load_auxiliary_genres(connection: sqlite3.Connection) -> dict[str, str]:
    """Load genres from optional Metrolist tables that actually retain one."""
    objects = {
        row[0].lower(): row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    found: dict[str, str] = {}
    recognition = objects.get("recognition_history")
    if recognition:
        columns = table_columns(connection, recognition)
        song_id = choose_column(columns, "youtubeVideoId", "videoId", "songId", "trackId")
        genre = choose_column(columns, "genre", "genreName", "primaryGenreName")
        if song_id and genre:
            sql = (
                f"SELECT {quote_identifier(song_id)}, {quote_identifier(genre)} "
                f"FROM {quote_identifier(recognition)}"
            )
            for identifier, name in connection.execute(sql):
                if identifier and name:
                    found.setdefault(str(identifier), str(name))
    return found


def load_auxiliary_artists(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """Read reliable denormalized artist fields used by newer Metrolist features."""
    objects = {
        row[0].lower(): row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    found: dict[str, list[str]] = {}

    recognition = objects.get("recognition_history")
    if recognition:
        columns = table_columns(connection, recognition)
        song_id = choose_column(columns, "youtubeVideoId", "videoId", "songId", "trackId")
        artist = choose_column(columns, "artist", "artistName", "author")
        if song_id and artist:
            sql = (
                f"SELECT {quote_identifier(song_id)}, {quote_identifier(artist)} "
                f"FROM {quote_identifier(recognition)}"
            )
            for identifier, name in connection.execute(sql):
                if identifier and name:
                    found.setdefault(str(identifier), []).append(str(name))

    speed_dial = objects.get("speed_dial_item")
    if speed_dial:
        columns = table_columns(connection, speed_dial)
        song_id = choose_column(columns, "id", "songId", "videoId")
        subtitle = choose_column(columns, "subtitle", "artist", "artistName")
        item_type = choose_column(columns, "type", "itemType")
        if song_id and subtitle:
            selected = [quote_identifier(song_id), quote_identifier(subtitle)]
            if item_type:
                selected.append(quote_identifier(item_type))
            sql = f"SELECT {', '.join(selected)} FROM {quote_identifier(speed_dial)}"
            for row in connection.execute(sql):
                if not row[0] or not row[1]:
                    continue
                if item_type and str(row[2]).lower() not in {"song", "video", "music"}:
                    continue
                # Metrolist displays these as "Artist • Album" (or a middle-dot variant).
                name = re.split(r"\s+[•·]\s+", str(row[1]), maxsplit=1)[0].strip()
                if name:
                    found.setdefault(str(row[0]), []).append(name)

    return {key: list(dict.fromkeys(values)) for key, values in found.items()}


def normalize_youtube_author(author: str) -> str:
    """Turn YouTube's generated music-channel label into a useful artist tag."""
    value = re.sub(r"\s+-\s+Topic$", "", author.strip(), flags=re.IGNORECASE)
    return value if value.lower() not in {"", "youtube"} else ""


def fetch_youtube_artist(song_id: str, timeout: float = 10.0) -> str | None:
    """Use YouTube's public oEmbed metadata as a last-resort artist/uploader source."""
    url = YOUTUBE_OEMBED.format(song_id=urllib.parse.quote(song_id, safe=""))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Metrolist-Extractor/0.6 (+https://github.com/MetrolistGroup/Metrolist)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.load(response)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None
    author = document.get("author_name")
    if not isinstance(author, str):
        return None
    return normalize_youtube_author(author) or None


def default_artist_cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "metrolist-extractor" / "artists.json"


def resolve_online_artists(
    tracks: Sequence[Track],
    cache_path: Path | None = None,
    refresh: bool = False,
    workers: int = 8,
) -> tuple[int, int]:
    """Fill unresolved tracks from a persistent cache and YouTube oEmbed."""
    cache_path = cache_path or default_artist_cache_path()
    cache: dict[str, str | None] = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = {
                    str(key): value if isinstance(value, str) else None
                    for key, value in loaded.items()
                }
        except (OSError, ValueError, json.JSONDecodeError):
            cache = {}

    unresolved = [track for track in tracks if not track.artists]
    for track in unresolved:
        cached = None if refresh else cache.get(track.song_id)
        if cached:
            track.artists = [cached]
            track.artist_source = "youtube-cache"

    pending = [
        track for track in unresolved
        if not track.artists and (refresh or track.song_id not in cache)
    ]
    results: dict[str, str | None] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(fetch_youtube_artist, track.song_id): track.song_id
                for track in pending
            }
            for future in as_completed(futures):
                song_id = futures[future]
                try:
                    results[song_id] = future.result()
                except Exception:
                    results[song_id] = None
        cache.update({song_id: artist for song_id, artist in results.items() if artist})
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, cache_path)
        except OSError:
            pass

    for track in unresolved:
        resolved = results.get(track.song_id)
        if resolved:
            track.artists = [resolved]
            track.artist_source = "youtube"

    resolved_count = sum(1 for track in unresolved if track.artists)
    return resolved_count, len(unresolved) - resolved_count


def normalized_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = re.sub(r"\b(feat|ft)\.?\s+", "feat ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def select_itunes_result(track: Track, results: Sequence[dict]) -> CatalogMetadata | None:
    """Select a catalogue candidate using title, artist, and duration agreement."""
    wanted_title = normalized_match_text(track.title)
    wanted_artist = normalized_match_text(track.artist) if track.artists else ""
    best: tuple[float, dict] | None = None
    for result in results:
        if result.get("kind") != "song":
            continue
        title = normalized_match_text(str(result.get("trackName") or ""))
        artist = normalized_match_text(str(result.get("artistName") or ""))
        if not title or not artist:
            continue
        title_score = difflib.SequenceMatcher(None, wanted_title, title).ratio()
        artist_score = difflib.SequenceMatcher(None, wanted_artist, artist).ratio()
        if title_score < 0.55 or artist_score < 0.40:
            continue
        duration_score = 0.5
        duration_ms = result.get("trackTimeMillis")
        if track.duration_seconds and isinstance(duration_ms, (int, float)):
            difference = abs(float(duration_ms) / 1000 - track.duration_seconds)
            duration_score = max(0.0, 1.0 - difference / 20.0)
        score = title_score * 0.55 + artist_score * 0.30 + duration_score * 0.15
        if best is None or score > best[0]:
            best = (score, result)
    if not best or best[0] < 0.64:
        return None
    result = best[1]
    return CatalogMetadata(
        artist=str(result["artistName"]) if result.get("artistName") else None,
        album=str(result["collectionName"]) if result.get("collectionName") else None,
        genre=str(result["primaryGenreName"]) if result.get("primaryGenreName") else None,
    )


def fetch_catalog_metadata(track: Track, timeout: float = 12.0) -> CatalogMetadata | None:
    parameters = urllib.parse.urlencode(
        {
            "term": f"{track.artist} {track.title}",
            "media": "music",
            "entity": "song",
            "limit": 10,
            "country": "US",
        }
    )
    request = urllib.request.Request(
        f"{ITUNES_SEARCH}?{parameters}",
        headers={"User-Agent": "Metrolist-Extractor/0.6"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.load(response)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None
    results = document.get("results")
    if not isinstance(results, list):
        return None
    return select_itunes_result(track, results)


def extract_youtube_music_album(document: dict, song_id: str) -> CatalogMetadata | None:
    """Extract a typed album link for an exact video ID from Music's next response."""
    renderers: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("videoId") == song_id and isinstance(value.get("longBylineText"), dict):
                renderers.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    for renderer in renderers:
        runs = renderer.get("longBylineText", {}).get("runs", [])
        album: str | None = None
        artist: str | None = None
        year: int | None = None
        for run in runs:
            if not isinstance(run, dict):
                continue
            text = meaningful_metadata(run.get("text"), "album")
            endpoint = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
            page_type = (
                endpoint.get("browseEndpointContextSupportedConfigs", {})
                .get("browseEndpointContextMusicConfig", {})
                .get("pageType")
            )
            if page_type == "MUSIC_PAGE_TYPE_ALBUM" and text:
                album = text
            elif page_type == "MUSIC_PAGE_TYPE_ARTIST" and text and not artist:
                artist = text
            elif text and re.fullmatch(r"(?:19|20)\d{2}", text):
                year = int(text)
        if album:
            return CatalogMetadata(artist=artist, album=album, year=year)
    return None


def fetch_youtube_music_album(song_id: str, timeout: float = 12.0) -> CatalogMetadata | None:
    body = {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": YOUTUBE_MUSIC_CLIENT_VERSION,
                "hl": "en",
                "gl": "US",
            },
            "request": {"useSsl": True},
            "user": {"lockedSafetyMode": False},
        },
        "videoId": song_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    request = urllib.request.Request(
        YOUTUBE_MUSIC_NEXT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
                "Gecko/20100101 Firefox/140.0"
            ),
            "X-YouTube-Client-Name": "67",
            "X-YouTube-Client-Version": YOUTUBE_MUSIC_CLIENT_VERSION,
            "Origin": "https://music.youtube.com",
            "Referer": "https://music.youtube.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.load(response)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return extract_youtube_music_album(document, song_id) if isinstance(document, dict) else None


def youtube_music_page_type(run: dict) -> str | None:
    return (
        run.get("navigationEndpoint", {})
        .get("browseEndpoint", {})
        .get("browseEndpointContextSupportedConfigs", {})
        .get("browseEndpointContextMusicConfig", {})
        .get("pageType")
    )


def youtube_music_renderer_video_id(renderer: dict) -> str | None:
    direct = renderer.get("playlistItemData", {}).get("videoId")
    if direct:
        return str(direct)
    direct = renderer.get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
    if direct:
        return str(direct)
    for column in renderer.get("flexColumns", []):
        runs = column.get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [])
        for run in runs:
            video_id = run.get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
            if video_id:
                return str(video_id)
    return None


def extract_youtube_music_search_album(
    document: dict,
    track: Track,
) -> CatalogMetadata | None:
    """Choose a strongly matching song result that exposes a typed album link."""
    renderers: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            renderer = value.get("musicResponsiveListItemRenderer")
            if isinstance(renderer, dict):
                renderers.append(renderer)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    wanted_title = normalized_match_text(track.title)
    wanted_artist = normalized_match_text(track.artist) if track.artists else ""
    best: tuple[float, CatalogMetadata] | None = None
    for renderer in renderers:
        columns = renderer.get("flexColumns", [])
        if not columns:
            continue
        title_runs = (
            columns[0]
            .get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        title = next(
            (str(run.get("text")) for run in title_runs if run.get("text")),
            "",
        )
        metadata_runs = [
            run
            for column in columns[1:]
            for run in (
                column.get("musicResponsiveListItemFlexColumnRenderer", {})
                .get("text", {})
                .get("runs", [])
            )
            if isinstance(run, dict)
        ]
        album: str | None = None
        artists: list[str] = []
        year: int | None = None
        duration: int | None = None
        for run in metadata_runs:
            text = meaningful_metadata(run.get("text"), "album")
            page_type = youtube_music_page_type(run)
            if page_type == "MUSIC_PAGE_TYPE_ALBUM" and text:
                album = text
            elif page_type == "MUSIC_PAGE_TYPE_ARTIST" and text:
                artists.append(text)
            elif text and re.fullmatch(r"(?:19|20)\d{2}", text):
                year = int(text)
            elif text and re.fullmatch(r"\d{1,2}:\d{2}", text):
                minutes, seconds = (int(part) for part in text.split(":"))
                duration = minutes * 60 + seconds
        if not album or not title:
            continue
        renderer_id = youtube_music_renderer_video_id(renderer)
        if renderer_id == track.song_id:
            score = 2.0
        else:
            title_score = difflib.SequenceMatcher(
                None, wanted_title, normalized_match_text(title)
            ).ratio()
            artist_score = difflib.SequenceMatcher(
                None, wanted_artist, normalized_match_text(", ".join(artists))
            ).ratio() if artists and wanted_artist else 0.5
            duration_score = 0.5
            if duration is not None and track.duration_seconds:
                duration_score = max(
                    0.0, 1.0 - abs(duration - track.duration_seconds) / 20.0
                )
            if title_score < 0.70 or artist_score < 0.45:
                continue
            if not wanted_artist and (title_score < 0.90 or duration_score < 0.75):
                continue
            score = title_score * 0.60 + artist_score * 0.25 + duration_score * 0.15
            if score < 0.72:
                continue
        metadata = CatalogMetadata(
            artist=", ".join(artists) if artists else None,
            album=album,
            year=year,
        )
        if best is None or score > best[0]:
            best = (score, metadata)
    return best[1] if best else None


def fetch_youtube_music_search_album(
    track: Track,
    timeout: float = 12.0,
) -> CatalogMetadata | None:
    body = {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": YOUTUBE_MUSIC_CLIENT_VERSION,
                "hl": "en",
                "gl": "US",
            }
        },
        "query": f"{track.artist} {track.title}" if track.artists else track.title,
        "params": YOUTUBE_MUSIC_SONG_FILTER,
    }
    request = urllib.request.Request(
        YOUTUBE_MUSIC_SEARCH,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "X-YouTube-Client-Name": "67",
            "X-YouTube-Client-Version": YOUTUBE_MUSIC_CLIENT_VERSION,
            "Origin": "https://music.youtube.com",
            "Referer": "https://music.youtube.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.load(response)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return (
        extract_youtube_music_search_album(document, track)
        if isinstance(document, dict)
        else None
    )


def fetch_track_catalog_metadata(
    track: Track,
    lookup_album: bool = True,
    lookup_genre: bool = True,
) -> CatalogMetadata | None:
    """Prefer an exact Music album link, then add genre/album from Apple if needed."""
    exact = (
        fetch_youtube_music_album(track.song_id)
        if lookup_album and not track.album
        else None
    )
    music_search = (
        fetch_youtube_music_search_album(track)
        if lookup_album and not track.album and not exact
        else None
    )
    youtube_music = exact or music_search
    apple = (
        fetch_catalog_metadata(track)
        if track.artists and (
            (lookup_genre and not track.genre)
            or (lookup_album and not track.album and not youtube_music)
        )
        else None
    )
    if not youtube_music and not apple:
        return None
    return CatalogMetadata(
        artist=(youtube_music.artist if youtube_music else None) or (apple.artist if apple else None),
        album=(
            (youtube_music.album if youtube_music else None) or (apple.album if apple else None)
            if lookup_album else None
        ),
        genre=apple.genre if apple and lookup_genre else None,
        year=youtube_music.year if youtube_music and lookup_album else None,
    )


def default_catalog_cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "metrolist-extractor" / "catalog.json"


def resolve_catalog_metadata(
    tracks: Sequence[Track],
    cache_path: Path | None = None,
    refresh: bool = False,
    workers: int = 4,
    lookup_album: bool = True,
    lookup_genre: bool = True,
) -> tuple[int, int, int, int]:
    """Fill missing genre/album fields from a scored, persistent catalogue cache."""
    cache_path = cache_path or default_catalog_cache_path()
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = {str(key): value for key, value in loaded.items() if isinstance(value, dict)}
        except (OSError, ValueError, json.JSONDecodeError):
            cache = {}

    def needs_lookup(track: Track) -> bool:
        return (
            (lookup_genre and not track.genre)
            or (lookup_album and not track.album)
        )

    candidates = [track for track in tracks if needs_lookup(track)]
    original_genres = sum(bool(track.genre) for track in candidates)
    original_albums = sum(bool(track.album) for track in candidates)

    def apply(track: Track, metadata: CatalogMetadata, source: str) -> None:
        genre = meaningful_metadata(metadata.genre, "genre")
        album = meaningful_metadata(metadata.album, "album")
        if lookup_genre and not track.genre and genre:
            track.genre = genre
            track.genre_source = source
        if lookup_album and not track.album and album:
            track.album = album
            track.album_source = source
        if lookup_album and not track.year and metadata.year:
            track.year = metadata.year

    pending: list[Track] = []
    for track in candidates:
        cached = None if refresh else cache.get(track.song_id)
        if cached:
            apply(
                track,
                CatalogMetadata(
                    artist=cached.get("artist"),
                    album=cached.get("album"),
                    genre=cached.get("genre"),
                    year=cached.get("year") if isinstance(cached.get("year"), int) else None,
                ),
                "catalog-cache",
            )
        if refresh or needs_lookup(track):
            pending.append(track)

    results: dict[str, CatalogMetadata] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    fetch_track_catalog_metadata, track, lookup_album, lookup_genre
                ): track
                for track in pending
            }
            for future in as_completed(futures):
                track = futures[future]
                try:
                    metadata = future.result()
                except Exception:
                    metadata = None
                if metadata:
                    results[track.song_id] = metadata
                    apply(track, metadata, "catalog")
        for song_id, metadata in results.items():
            cached_result = cache.setdefault(song_id, {})
            for key, value in (
                ("artist", metadata.artist),
                ("album", metadata.album),
                ("genre", metadata.genre),
                ("year", metadata.year),
            ):
                if value is not None:
                    cached_result[key] = value
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, cache_path)
        except OSError:
            pass

    genres_resolved = sum(bool(track.genre) for track in candidates) - original_genres
    albums_resolved = sum(bool(track.album) for track in candidates) - original_albums
    genres_missing = sum(not track.genre for track in tracks) if lookup_genre else 0
    albums_missing = sum(not track.album for track in tracks) if lookup_album else 0
    return genres_resolved, albums_resolved, genres_missing, albums_missing


def load_tracks(
    song_db: Path,
    exoplayer_db: Path,
    spans: Sequence[CacheSpan],
    cache_uid: str | None,
) -> list[Track]:
    by_cache_id: dict[int, list[CacheSpan]] = {}
    for span in spans:
        by_cache_id.setdefault(span.cache_id, []).append(span)

    with sqlite3.connect(exoplayer_db) as exo:
        table = discover_cache_table(exo, cache_uid)
        cache_mapping = {
            int(cache_id): str(key)
            for cache_id, key in exo.execute(
                f"SELECT id, key FROM {quote_identifier(table)}"
            )
        }

    with sqlite3.connect(song_db) as songs:
        songs.row_factory = sqlite3.Row
        song_columns = table_columns(songs, "song")
        required = {"id", "title"}
        if not required.issubset(set(song_columns)):
            raise ExtractorError("Metrolist song table is missing required id/title columns")
        song_artists, album_artists = load_artist_maps(songs)
        auxiliary_artists = load_auxiliary_artists(songs)
        song_albums, album_catalog = load_song_albums(songs)
        auxiliary_albums = load_auxiliary_albums(songs)
        auxiliary_genres = load_auxiliary_genres(songs)
        song_genre_column = choose_column(song_columns, "genre", "genreName", "primaryGenreName")
        selected = [
            column
            for column in (
                "id", "title", "albumId", "albumName", "year", "duration", "thumbnailUrl",
                "isDownloaded"
            )
            if column in song_columns
        ]
        if song_genre_column and song_genre_column not in selected:
            selected.append(song_genre_column)
        sql = "SELECT " + ", ".join(quote_identifier(c) for c in selected) + " FROM song"
        rows = {str(row["id"]): row for row in songs.execute(sql)}

        # Reuse a known song artist for siblings with the same stable album ID.
        learned_album_artists: dict[str, list[str]] = {}
        for song_id, row in rows.items():
            album_id = str(row["albumId"]) if "albumId" in row.keys() and row["albumId"] else None
            if album_id and song_artists.get(song_id):
                bucket = learned_album_artists.setdefault(album_id, [])
                for name in song_artists[song_id]:
                    if name not in bucket:
                        bucket.append(name)

    tracks: list[Track] = []
    for cache_id, track_spans in sorted(by_cache_id.items()):
        song_id = cache_mapping.get(cache_id)
        if not song_id or song_id not in rows:
            continue
        row = rows[song_id]
        if "isDownloaded" in row.keys() and not row["isDownloaded"]:
            continue
        ordered = validate_spans(track_spans)
        album_id = str(row["albumId"]) if "albumId" in row.keys() and row["albumId"] else None
        album = (
            meaningful_metadata(row["albumName"], "album")
            if "albumName" in row.keys()
            else None
        )
        year = int(row["year"]) if "year" in row.keys() and row["year"] else None
        thumbnail_url = (
            str(row["thumbnailUrl"])
            if "thumbnailUrl" in row.keys() and row["thumbnailUrl"]
            else None
        )
        album_source = "song" if album else "missing"

        related_album = song_albums.get(song_id)
        if not related_album and album_id:
            related_album = album_catalog.get(album_id)
        if related_album:
            album_id = album_id or related_album.album_id
            related_title = meaningful_metadata(related_album.title, "album")
            if not album and related_title:
                album = related_title
                album_source = "album-map"
            year = year or related_album.year
            thumbnail_url = thumbnail_url or related_album.thumbnail_url

        if not album:
            auxiliary_album = auxiliary_albums.get(song_id)
            if auxiliary_album:
                album = meaningful_metadata(auxiliary_album.title, "album")
                album_id = album_id or auxiliary_album.album_id
                album_source = "auxiliary" if album else "missing"
        genre = (
            meaningful_metadata(row[song_genre_column], "genre")
            if song_genre_column and row[song_genre_column]
            else meaningful_metadata(auxiliary_genres.get(song_id), "genre")
        )
        genre_source = "song" if song_genre_column and row[song_genre_column] else (
            "auxiliary" if genre else "missing"
        )
        artists = song_artists.get(song_id, [])
        artist_source = "song-map" if artists else "missing"
        if not artists and album_id:
            artists = album_artists.get(album_id, [])
            artist_source = "album-map" if artists else "missing"
        if not artists and album_id:
            artists = learned_album_artists.get(album_id, [])
            artist_source = "album-sibling" if artists else "missing"
        if not artists:
            artists = auxiliary_artists.get(song_id, [])
            artist_source = "auxiliary" if artists else "missing"
        tracks.append(
            Track(
                cache_id=cache_id,
                song_id=song_id,
                title=str(row["title"]),
                album=album,
                year=year,
                duration_seconds=int(row["duration"]) if "duration" in row.keys() and row["duration"] else None,
                thumbnail_url=thumbnail_url,
                artists=artists,
                spans=ordered,
                album_id=album_id,
                artist_source=artist_source,
                album_source=album_source,
                genre=genre,
                genre_source=genre_source,
            )
        )
    return tracks


class Extractor:
    def __init__(self, bridge: AndroidBridge, paths: MetrolistPaths | None = None) -> None:
        self.bridge = bridge
        self.paths = paths
        self._artwork_cache: dict[str, Artwork | ExtractorError] = {}
        self._artwork_lock = threading.Lock()
        self._artwork_inflight: dict[str, threading.Event] = {}

    def scan(self, workspace: Path, paths: MetrolistPaths | None = None) -> list[Track]:
        paths = paths or self.paths
        if paths is None:
            paths = resolve_paths(self.bridge)
        self.paths = paths
        workspace.mkdir(parents=True, exist_ok=True)
        song_db = workspace / "song.db"
        exo_db = workspace / "exoplayer_internal.db"
        self.bridge.snapshot_database(paths.song_db, song_db, "songs")
        self.bridge.snapshot_database(paths.exoplayer_db, exo_db, "exoplayer")
        spans, uid = self.bridge.list_spans(paths.download_dir)
        return load_tracks(song_db, exo_db, spans, uid)

    def get_artwork(self, track: Track) -> Artwork:
        if not track.thumbnail_url:
            raise ExtractorError("Metrolist has no artwork URL for this track")
        url = track.thumbnail_url
        while True:
            with self._artwork_lock:
                cached = self._artwork_cache.get(url)
                if isinstance(cached, Artwork):
                    return cached
                if isinstance(cached, ExtractorError):
                    raise cached
                waiting = self._artwork_inflight.get(url)
                if waiting is None:
                    waiting = threading.Event()
                    self._artwork_inflight[url] = waiting
                    owner = True
                else:
                    owner = False
            if owner:
                break
            waiting.wait()
        try:
            artwork = download_artwork(url)
            with self._artwork_lock:
                self._artwork_cache[url] = artwork
            return artwork
        except ExtractorError as error:
            with self._artwork_lock:
                self._artwork_cache[url] = error
            raise
        finally:
            with self._artwork_lock:
                self._artwork_inflight.pop(url, None)
                waiting.set()

    def export_track(
        self,
        track: Track,
        destination: Path,
        overwrite: bool = False,
        embed_artwork: bool = True,
        require_artwork: bool = False,
    ) -> ExportResult:
        if shutil.which("ffmpeg") is None:
            raise ExtractorError("ffmpeg is required; on Ubuntu/Mint run: sudo apt install ffmpeg")
        destination.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="metrolist-track-") as temporary:
            temporary_path = Path(temporary)
            reconstructed = temporary_path / f"{track.song_id}.media"
            with reconstructed.open("wb") as output:
                for span in validate_spans(track.spans):
                    self.bridge.copy_remote(span.remote_path, output)
            codec = probe_audio_codec(reconstructed)
            format_plan = audio_format_for_codec(codec)
            final_path = destination / output_name(track, format_plan.extension)
            if final_path.exists() and not overwrite:
                raise FileExistsError(final_path)
            partial = destination / (
                f".{final_path.name}.{os.getpid()}.{threading.get_ident()}.partial"
            )
            artwork: Artwork | None = None
            artwork_warning: str | None = None
            metadata_input: Path | None = None
            artwork_file: Path | None = None
            if embed_artwork:
                try:
                    artwork = self.get_artwork(track)
                    if format_plan.artwork_mode == "comment":
                        metadata_input = temporary_path / "artwork.ffmetadata"
                        write_picture_metadata(metadata_input, artwork)
                    else:
                        artwork_file = write_artwork_file(temporary_path, artwork)
                except ExtractorError as error:
                    artwork_warning = str(error)
                    if require_artwork:
                        raise
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(reconstructed),
            ]
            if metadata_input:
                command += ["-f", "ffmetadata", "-i", str(metadata_input)]
            elif artwork_file and format_plan.artwork_mode == "attached_pic":
                command += ["-i", str(artwork_file)]
            command += [
                "-map", "0:a:0", "-c:a", "copy",
            ]
            if metadata_input:
                command += ["-map_metadata", "1"]
            elif artwork_file and format_plan.artwork_mode == "attached_pic":
                command += [
                    "-map", "1:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic",
                    "-metadata:s:v:0", "title=Album cover",
                    "-metadata:s:v:0", "comment=Cover (front)",
                ]
            command += [
                "-metadata", f"title={track.title}",
                "-metadata", f"artist={track.artist}",
                "-metadata", f"comment=Metrolist song ID: {track.song_id}",
            ]
            if track.album:
                command += ["-metadata", f"album={track.album}"]
            if track.year:
                command += ["-metadata", f"date={track.year}"]
            if track.genre:
                command += ["-metadata", f"genre={track.genre}"]
            if artwork_file and format_plan.artwork_mode == "attachment":
                command += [
                    "-attach", str(artwork_file),
                    "-metadata:s:t:0", f"mimetype={artwork.mime_type}",
                    "-metadata:s:t:0", f"filename=cover.{artwork_extension(artwork.mime_type)}",
                ]
            if format_plan.muxer == "mp3":
                command += ["-id3v2_version", "3"]
            if format_plan.muxer == "ipod":
                command += ["-movflags", "+faststart"]
            command += ["-f", format_plan.muxer, str(partial)]
            try:
                subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if artwork and not verify_artwork(partial, format_plan.artwork_mode):
                    raise ExtractorError("FFmpeg completed but the output artwork tag could not be verified")
                os.replace(partial, final_path)
            except ExtractorError:
                partial.unlink(missing_ok=True)
                raise
            except subprocess.CalledProcessError as error:
                partial.unlink(missing_ok=True)
                detail = error.stderr.decode("utf-8", "replace").strip()
                raise ExtractorError(f"ffmpeg could not remux {track.title}: {detail}") from error
        return ExportResult(
            path=final_path,
            codec=codec,
            container=format_plan.extension,
            artwork_embedded=artwork is not None,
            artwork_warning=artwork_warning,
        )


def human_size(size: int) -> str:
    value = float(size)
    for suffix in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or suffix == "GiB":
            return f"{value:.1f} {suffix}" if suffix != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)
