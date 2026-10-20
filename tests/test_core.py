from pathlib import Path
import base64
import contextlib
import io
import sqlite3
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from metrolist_extractor.core import (
    Artwork,
    audio_format_for_codec,
    CacheSpan,
    CatalogMetadata,
    AndroidBridge,
    Extractor,
    ExtractorError,
    Track,
    discover_cache_table,
    load_tracks,
    normalize_youtube_author,
    output_name,
    parse_span_line,
    safe_component,
    resolve_paths,
    resolve_online_artists,
    resolve_catalog_metadata,
    select_itunes_result,
    extract_youtube_music_album,
    extract_youtube_music_search_album,
    meaningful_metadata,
    validate_spans,
)
from metrolist_extractor.config import load_settings, update_setting
from metrolist_extractor.cli import command_help, parser


def span(cache_id: int, offset: int, size: int) -> CacheSpan:
    return CacheSpan(cache_id, offset, 1234, size, f"/download/5/{cache_id}.{offset}.1234.v3.exo")


class CoreTests(unittest.TestCase):
    def test_parallel_export_jobs_option(self):
        args = parser().parse_args(["export", "--jobs", "6"])
        self.assertEqual(args.jobs, 6)

    def test_individual_metadata_lookup_flags(self):
        args = parser().parse_args([
            "export", "--no-artist-lookup", "--no-album-lookup",
            "--no-genre-lookup",
        ])
        self.assertTrue(args.offline_artists)
        self.assertTrue(args.no_album_lookup)
        self.assertTrue(args.no_genre_lookup)

    def test_all_online_metadata_flag(self):
        args = parser().parse_args(["list", "--no-online-metadata"])
        self.assertTrue(args.no_online_metadata)

    def test_help_command_lists_every_command_and_options(self):
        args = parser().parse_args(["help"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(command_help(args), 0)
        rendered = output.getvalue()
        self.assertIn("metrolist-extract export", rendered)
        self.assertIn("--require-artwork", rendered)
        self.assertIn("--no-album-lookup", rendered)

    def test_help_command_can_focus_on_export(self):
        args = parser().parse_args(["help", "export"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(command_help(args), 0)
        self.assertIn("usage: metrolist-extract export", output.getvalue())

    def test_catalog_match_uses_title_artist_and_duration(self):
        track = Track(
            0, "video-id", "curse (feat. Skyte)", None, None, 194, None,
            ["ayowitty"],
        )
        metadata = select_itunes_result(
            track,
            [
                {
                    "kind": "song", "trackName": "Different Song",
                    "artistName": "ayowitty", "collectionName": "Wrong",
                    "primaryGenreName": "Pop", "trackTimeMillis": 194000,
                },
                {
                    "kind": "song", "trackName": "curse (feat. Skyte)",
                    "artistName": "ayowitty", "collectionName": "curse (feat. Skyte) - Single",
                    "primaryGenreName": "Rock", "trackTimeMillis": 193698,
                },
            ],
        )
        self.assertEqual(
            metadata,
            CatalogMetadata("ayowitty", "curse (feat. Skyte) - Single", "Rock"),
        )

    def test_catalog_lookup_fills_and_caches_genre_and_album(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "catalog.json"
            first = Track(0, "video-id", "Song", None, None, 180, None, ["Artist"])
            expected = CatalogMetadata("Artist", "Recovered Album", "Alternative")
            with patch(
                "metrolist_extractor.core.fetch_track_catalog_metadata", return_value=expected
            ) as lookup:
                self.assertEqual(resolve_catalog_metadata([first], cache), (1, 1, 0, 0))
                lookup.assert_called_once_with(first, True, True)
            second = Track(0, "video-id", "Song", None, None, 180, None, ["Artist"])
            with patch("metrolist_extractor.core.fetch_track_catalog_metadata") as lookup:
                self.assertEqual(resolve_catalog_metadata([second], cache), (1, 1, 0, 0))
                lookup.assert_not_called()
            self.assertEqual(second.album, "Recovered Album")
            self.assertEqual(second.genre, "Alternative")
            self.assertEqual(second.genre_source, "catalog-cache")

    def test_catalog_lookup_can_skip_album_only(self):
        track = Track(0, "video-id", "Song", None, None, 180, None, ["Artist"])
        expected = CatalogMetadata(album="Should Not Apply", genre="Alternative")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "metrolist_extractor.core.fetch_track_catalog_metadata",
            return_value=expected,
        ) as lookup:
            result = resolve_catalog_metadata(
                [track], Path(temporary) / "catalog.json", lookup_album=False
            )
        lookup.assert_called_once_with(track, False, True)
        self.assertIsNone(track.album)
        self.assertEqual(track.genre, "Alternative")
        self.assertEqual(result, (1, 0, 0, 0))

    def test_youtube_music_exact_video_extracts_typed_album(self):
        page_type = lambda value: {
            "browseEndpoint": {
                "browseEndpointContextSupportedConfigs": {
                    "browseEndpointContextMusicConfig": {"pageType": value}
                }
            }
        }
        document = {
            "contents": [{
                "playlistPanelVideoRenderer": {
                    "videoId": "6Ifveu-hVBY",
                    "longBylineText": {"runs": [
                        {"text": "ayowitty", "navigationEndpoint": page_type("MUSIC_PAGE_TYPE_ARTIST")},
                        {"text": " • "},
                        {"text": "curse", "navigationEndpoint": page_type("MUSIC_PAGE_TYPE_ALBUM")},
                        {"text": " • "},
                        {"text": "2024"},
                    ]},
                }
            }]
        }
        self.assertEqual(
            extract_youtube_music_album(document, "6Ifveu-hVBY"),
            CatalogMetadata(artist="ayowitty", album="curse", year=2024),
        )

    def test_youtube_music_search_recovers_album_from_strong_match(self):
        def endpoint(page_type, text, video_id=None):
            navigation = {
                "browseEndpoint": {
                    "browseEndpointContextSupportedConfigs": {
                        "browseEndpointContextMusicConfig": {"pageType": page_type}
                    }
                }
            }
            if video_id:
                navigation = {"watchEndpoint": {"videoId": video_id}}
            return {"text": text, "navigationEndpoint": navigation}

        renderer = {
            "playlistItemData": {"videoId": "catalog-video"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                    endpoint("", "Tek It", "catalog-video")
                ]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                    endpoint("MUSIC_PAGE_TYPE_ARTIST", "Cafuné"),
                    {"text": " • "},
                    endpoint("MUSIC_PAGE_TYPE_ALBUM", "Running"),
                    {"text": " • "},
                    {"text": "3:11"},
                    {"text": " • "},
                    {"text": "2021"},
                ]}}},
            ],
        }
        document = {"contents": [{"musicResponsiveListItemRenderer": renderer}]}
        track = Track(0, "uploaded-video", "Tek It", None, None, 191, None, ["Cafuné"])
        self.assertEqual(
            extract_youtube_music_search_album(document, track),
            CatalogMetadata(artist="Cafuné", album="Running", year=2021),
        )

    def test_youtube_music_search_rejects_weak_album_guess(self):
        renderer = {
            "playlistItemData": {"videoId": "other"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                    {"text": "Completely Different"}
                ]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                    {
                        "text": "Wrong Artist",
                        "navigationEndpoint": {"browseEndpoint": {
                            "browseEndpointContextSupportedConfigs": {
                                "browseEndpointContextMusicConfig": {
                                    "pageType": "MUSIC_PAGE_TYPE_ARTIST"
                                }
                            }
                        }},
                    },
                    {
                        "text": "Wrong Album",
                        "navigationEndpoint": {"browseEndpoint": {
                            "browseEndpointContextSupportedConfigs": {
                                "browseEndpointContextMusicConfig": {
                                    "pageType": "MUSIC_PAGE_TYPE_ALBUM"
                                }
                            }
                        }},
                    },
                ]}}},
            ],
        }
        track = Track(0, "wanted", "Tek It", None, None, 191, None, ["Cafuné"])
        self.assertIsNone(extract_youtube_music_search_album(
            {"musicResponsiveListItemRenderer": renderer}, track
        ))

    def test_youtube_music_search_can_match_without_known_artist(self):
        def typed(text, page_type):
            return {
                "text": text,
                "navigationEndpoint": {"browseEndpoint": {
                    "browseEndpointContextSupportedConfigs": {
                        "browseEndpointContextMusicConfig": {"pageType": page_type}
                    }
                }},
            }

        renderer = {
            "playlistItemData": {"videoId": "catalog-version"},
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                    {"text": "Supermassive Black Hole"}
                ]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                    typed("Muse", "MUSIC_PAGE_TYPE_ARTIST"),
                    typed("Black Holes and Revelations", "MUSIC_PAGE_TYPE_ALBUM"),
                    {"text": "3:29"},
                ]}}},
            ],
        }
        track = Track(
            0, "unofficial-upload", "Supermassive Black Hole", None, None,
            209, None,
        )
        metadata = extract_youtube_music_search_album(
            {"musicResponsiveListItemRenderer": renderer}, track
        )
        self.assertEqual(metadata.album, "Black Holes and Revelations")
        self.assertEqual(metadata.artist, "Muse")

    def test_unknown_album_placeholder_is_missing(self):
        self.assertIsNone(meaningful_metadata("Unknown Album", "album"))
        self.assertEqual(meaningful_metadata("Actual Album", "album"), "Actual Album")

    def test_youtube_topic_author_is_normalized(self):
        self.assertEqual(normalize_youtube_author("ayowitty - Topic"), "ayowitty")
        self.assertEqual(normalize_youtube_author("Ordinary Channel"), "Ordinary Channel")

    def test_online_artist_lookup_is_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "artists.json"
            first = Track(0, "video-id", "Song", None, None, None, None)
            with patch(
                "metrolist_extractor.core.fetch_youtube_artist",
                return_value="Resolved Artist",
            ) as lookup:
                self.assertEqual(resolve_online_artists([first], cache), (1, 0))
                lookup.assert_called_once_with("video-id")
            second = Track(0, "video-id", "Song", None, None, None, None)
            with patch("metrolist_extractor.core.fetch_youtube_artist") as lookup:
                self.assertEqual(resolve_online_artists([second], cache), (1, 0))
                lookup.assert_not_called()
            self.assertEqual(second.artists, ["Resolved Artist"])
            self.assertEqual(second.artist_source, "youtube-cache")

    def test_path_discovery_checks_multiple_android_locations(self):
        class FakeBridge:
            def discover_app_roots(self, _package):
                return ["/data/user/0/com.metrolist.music", "/data/user/10/com.metrolist.music"]

            def path_exists(self, path, kind="e"):
                return path.startswith("/data/user/10/")

        paths = resolve_paths(FakeBridge())
        self.assertEqual(paths.app_root, "/data/user/10/com.metrolist.music")
        self.assertEqual(paths.download_dir, "/data/user/10/com.metrolist.music/files/download")

    def test_exact_path_overrides_do_not_require_standard_layout(self):
        class FakeBridge:
            def discover_app_roots(self, _package):
                return []

            def path_exists(self, path, kind="e"):
                return path in {"/custom/songs.sqlite", "/custom/media.sqlite", "/cache/offline"}

        paths = resolve_paths(
            FakeBridge(),
            database_dir="/custom",
            download_dir="/cache/offline",
            song_db="/custom/songs.sqlite",
            exoplayer_db="/custom/media.sqlite",
        )
        self.assertEqual(paths.app_root, "(custom paths)")

    def test_config_set_and_unset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            update_setting(path, "app_root", "/alternate/metrolist")
            update_setting(path, "use_sudo", "false")
            settings = load_settings(path)
            self.assertEqual(settings.app_root, "/alternate/metrolist")
            self.assertFalse(settings.use_sudo)
            update_setting(path, "app_root", None)
            self.assertIsNone(load_settings(path).app_root)

    def test_waydroid_option_boundary_precedes_android_command(self):
        bridge = AndroidBridge(use_sudo=False)
        self.assertEqual(
            bridge.command("find", "/data", "-type", "f"),
            ["waydroid", "shell", "--", "find", "/data", "-type", "f"],
        )

    def test_parse_verified_metrolist_span(self):
        item = parse_span_line(
            "2201340 /data/user/0/com.metrolist.music/files/download/5/0.0.1788344394331.v3.exo"
        )
        self.assertEqual(
            item,
            CacheSpan(
                cache_id=0,
                offset=0,
                timestamp=1788344394331,
                size=2201340,
                remote_path="/data/user/0/com.metrolist.music/files/download/5/0.0.1788344394331.v3.exo",
            ),
        )

    def test_span_sorting_and_gap_detection(self):
        ordered = validate_spans([span(2, 100, 20), span(2, 0, 100)])
        self.assertEqual([item.offset for item in ordered], [0, 100])
        with self.assertRaisesRegex(ExtractorError, "gap"):
            validate_spans([span(2, 0, 99), span(2, 100, 20)])

    def test_safe_filename(self):
        track = Track(0, "abc", "song/name", None, None, None, None, ["Artist"], [span(0, 0, 1)])
        self.assertEqual(output_name(track), "Artist - song／name.opus")
        self.assertEqual(safe_component("../"), "／")

    def test_catalog_join_uses_uid_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            song_db, exo_db = make_databases(directory)
            tracks = load_tracks(song_db, exo_db, [span(0, 0, 2201340)], "4984dc511201368e")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].song_id, "elHMZwjIWH0")
        self.assertEqual(tracks[0].artists, ["tracebackinbound"])
        self.assertEqual(tracks[0].complete_size, 2201340)

    def test_artist_lookup_uses_sorted_and_album_relationships(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            song_db, exo_db = make_databases(directory)
            with sqlite3.connect(song_db) as db:
                db.executescript(
                    """
                    CREATE TABLE sorted_song_artist_map (songId TEXT, artistId TEXT, position INTEGER);
                    CREATE TABLE album_artist_map (albumId TEXT, artistId TEXT, position INTEGER);
                    INSERT INTO artist VALUES ('sorted-1', 'First Artist');
                    INSERT INTO artist VALUES ('sorted-2', 'Second Artist');
                    INSERT INTO artist VALUES ('album-artist', 'Album Artist');
                    INSERT INTO song VALUES ('sorted-song', 'Sorted Song', 100, NULL, 'Album', 2026, 1, NULL);
                    INSERT INTO song VALUES ('album-song', 'Album Song', 100, NULL, 'Fallback Album', 2026, 1, NULL);
                    INSERT INTO sorted_song_artist_map VALUES ('sorted-song', 'sorted-2', 1);
                    INSERT INTO sorted_song_artist_map VALUES ('sorted-song', 'sorted-1', 0);
                    INSERT INTO album_artist_map VALUES ('fallback-album-id', 'album-artist', 0);
                    UPDATE song SET albumId = 'fallback-album-id' WHERE id = 'album-song';
                    """
                )
            with sqlite3.connect(exo_db) as db:
                db.execute(
                    "INSERT INTO ExoPlayerCacheIndex4984dc511201368e VALUES (1, 'sorted-song', X'')"
                )
                db.execute(
                    "INSERT INTO ExoPlayerCacheIndex4984dc511201368e VALUES (2, 'album-song', X'')"
                )
            tracks = load_tracks(
                song_db, exo_db,
                [span(1, 0, 100), span(2, 0, 100)],
                "4984dc511201368e",
            )
        by_id = {track.song_id: track for track in tracks}
        self.assertEqual(by_id["sorted-song"].artists, ["First Artist", "Second Artist"])
        self.assertEqual(by_id["album-song"].artists, ["Album Artist"])

    def test_album_lookup_uses_song_album_relation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            song_db, exo_db = make_databases(directory)
            with sqlite3.connect(song_db) as db:
                db.executescript(
                    """
                    CREATE TABLE album (
                        id TEXT PRIMARY KEY, title TEXT NOT NULL, year INTEGER,
                        thumbnailUrl TEXT
                    );
                    CREATE TABLE song_album_map (songId TEXT, albumId TEXT, `index` INTEGER);
                    INSERT INTO album VALUES (
                        'mapped-album', 'Recovered Album', 2025,
                        'https://example.invalid/album.jpg'
                    );
                    INSERT INTO song VALUES (
                        'mapped-song', 'Mapped Song', 100, NULL, NULL, NULL, 1, NULL
                    );
                    INSERT INTO song_album_map VALUES ('mapped-song', 'mapped-album', 3);
                    """
                )
            with sqlite3.connect(exo_db) as db:
                db.execute(
                    "INSERT INTO ExoPlayerCacheIndex4984dc511201368e VALUES "
                    "(3, 'mapped-song', X'')"
                )
            tracks = load_tracks(
                song_db, exo_db, [span(3, 0, 100)], "4984dc511201368e"
            )
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].album_id, "mapped-album")
        self.assertEqual(tracks[0].album, "Recovered Album")
        self.assertEqual(tracks[0].year, 2025)
        self.assertEqual(tracks[0].thumbnail_url, "https://example.invalid/album.jpg")
        self.assertEqual(tracks[0].album_source, "album-map")

    def test_codec_container_selection(self):
        self.assertEqual(audio_format_for_codec("opus").extension, "opus")
        self.assertEqual(audio_format_for_codec("aac").extension, "m4a")
        self.assertEqual(audio_format_for_codec("vorbis").extension, "ogg")
        self.assertEqual(audio_format_for_codec("mp3").extension, "mp3")
        self.assertEqual(audio_format_for_codec("flac").extension, "flac")
        self.assertEqual(audio_format_for_codec("unusual_codec").extension, "mka")

    def test_ambiguous_cache_tables_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "exo.db"
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE ExoPlayerCacheIndexAAA (id INTEGER, key TEXT, metadata BLOB)")
                db.execute("CREATE TABLE ExoPlayerCacheIndexBBB (id INTEGER, key TEXT, metadata BLOB)")
                with self.assertRaisesRegex(ExtractorError, "could not identify"):
                    discover_cache_table(db, None)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_export_remuxes_opus_and_writes_tags(self):
        class LocalBridge:
            def __init__(self, source: Path):
                self.source = source

            def copy_remote(self, _remote_path, destination):
                with self.source.open("rb") as source:
                    shutil.copyfileobj(source, destination)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.webm"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.1", "-c:a", "libopus", str(source),
                ],
                check=True,
            )
            track = Track(
                0, "song-id", "Test Song", "Test Album", 2026, 1, None,
                ["Test Artist"], [span(0, 0, source.stat().st_size)],
                genre="Alternative",
            )
            result = Extractor(LocalBridge(source)).export_track(
                track, root / "out", embed_artwork=False
            )
            exported = result.path
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_name:stream_tags=title,artist,album,date,genre", "-of", "json", str(exported),
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            details = json.loads(probe.stdout)
            self.assertEqual(details["streams"][0]["codec_name"], "opus")
            self.assertEqual(details["streams"][0]["tags"]["title"], "Test Song")
            self.assertEqual(details["streams"][0]["tags"]["artist"], "Test Artist")
            self.assertEqual(details["streams"][0]["tags"]["album"], "Test Album")
            self.assertEqual(details["streams"][0]["tags"]["genre"], "Alternative")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_export_embeds_standard_picture_block(self):
        class LocalBridge:
            def __init__(self, source: Path):
                self.source = source

            def copy_remote(self, _remote_path, destination):
                with self.source.open("rb") as source:
                    shutil.copyfileobj(source, destination)

        class ArtworkExtractor(Extractor):
            def get_artwork(self, _track):
                png = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
                return Artwork(png, "image/png", "https://example.invalid/cover.png")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.webm"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.1", "-c:a", "libopus", str(source),
                ],
                check=True,
            )
            track = Track(
                0, "song-id", "Artwork Song", "Album", 2026, 1,
                "https://example.invalid/cover.png", ["Artist"],
                [span(0, 0, source.stat().st_size)],
            )
            result = ArtworkExtractor(LocalBridge(source)).export_track(track, root / "out")
            self.assertTrue(result.artwork_embedded)
            self.assertIn(b"METADATA_BLOCK_PICTURE=", result.path.read_bytes())
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_type:stream_disposition=attached_pic", "-of", "json", str(result.path),
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            streams = json.loads(probe.stdout)["streams"]
            pictures = [
                stream for stream in streams
                if stream.get("codec_type") == "video"
                and stream.get("disposition", {}).get("attached_pic") == 1
            ]
            self.assertEqual(len(pictures), 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_aac_is_stream_copied_to_m4a_with_artwork(self):
        class LocalBridge:
            def __init__(self, source: Path):
                self.source = source

            def copy_remote(self, _remote_path, destination):
                with self.source.open("rb") as source:
                    shutil.copyfileobj(source, destination)

        class ArtworkExtractor(Extractor):
            def get_artwork(self, _track):
                png = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
                return Artwork(png, "image/png", "https://example.invalid/cover.png")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.m4a"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.1", "-c:a", "aac", str(source),
                ],
                check=True,
            )
            track = Track(
                0, "aac-song", "AAC Song", "Album", 2026, 1,
                "https://example.invalid/cover.png", ["Artist"],
                [span(0, 0, source.stat().st_size)],
            )
            result = ArtworkExtractor(LocalBridge(source)).export_track(track, root / "out")
            self.assertEqual(result.codec, "aac")
            self.assertEqual(result.path.suffix, ".m4a")
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_name,codec_type:stream_disposition=attached_pic",
                    "-of", "json", str(result.path),
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            streams = json.loads(probe.stdout)["streams"]
            self.assertEqual(streams[0]["codec_name"], "aac")
            self.assertTrue(any(s.get("disposition", {}).get("attached_pic") == 1 for s in streams))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
    def test_other_common_codecs_keep_codec_and_artwork(self):
        class LocalBridge:
            def __init__(self, source: Path):
                self.source = source

            def copy_remote(self, _remote_path, destination):
                with self.source.open("rb") as source:
                    shutil.copyfileobj(source, destination)

        class ArtworkExtractor(Extractor):
            def get_artwork(self, _track):
                png = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
                return Artwork(png, "image/png", "https://example.invalid/cover.png")

        formats = (
            ("libvorbis", "ogg", "vorbis", ".ogg"),
            ("libmp3lame", "mp3", "mp3", ".mp3"),
            ("flac", "flac", "flac", ".flac"),
            ("alac", "ipod", "alac", ".m4a"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for encoder, source_muxer, expected_codec, expected_suffix in formats:
                with self.subTest(codec=expected_codec):
                    source = root / f"source-{expected_codec}.bin"
                    subprocess.run(
                        [
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=0.1", "-c:a", encoder,
                            "-f", source_muxer, str(source),
                        ],
                        check=True,
                    )
                    track = Track(
                        0, f"{expected_codec}-song", f"{expected_codec} Song", "Album", 2026, 1,
                        "https://example.invalid/cover.png", ["Artist"],
                        [span(0, 0, source.stat().st_size)],
                    )
                    result = ArtworkExtractor(LocalBridge(source)).export_track(
                        track, root / f"out-{expected_codec}"
                    )
                    self.assertEqual(result.codec, expected_codec)
                    self.assertEqual(result.path.suffix, expected_suffix)
                    self.assertTrue(result.artwork_embedded)


def make_databases(directory: Path) -> tuple[Path, Path]:
    song_db = directory / "song.db"
    exo_db = directory / "exo.db"
    with sqlite3.connect(song_db) as db:
        db.executescript(
            """
            CREATE TABLE song (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, duration INTEGER NOT NULL,
                thumbnailUrl TEXT, albumName TEXT, year INTEGER, isDownloaded INTEGER NOT NULL,
                albumId TEXT
            );
            CREATE TABLE artist (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE song_artist_map (songId TEXT, artistId TEXT, position INTEGER);
            INSERT INTO song VALUES (
                'elHMZwjIWH0', 'the light left my iris (feat. bunii)', 136,
                'https://example.invalid/art.jpg', 'the light left my iris', 2026, 1, 'album-1'
            );
            INSERT INTO artist VALUES ('artist-1', 'tracebackinbound');
            INSERT INTO song_artist_map VALUES ('elHMZwjIWH0', 'artist-1', 0);
            """
        )
    with sqlite3.connect(exo_db) as db:
        db.execute(
            "CREATE TABLE ExoPlayerCacheIndex4984dc511201368e "
            "(id INTEGER PRIMARY KEY NOT NULL, key TEXT NOT NULL, metadata BLOB NOT NULL)"
        )
        db.execute(
            "INSERT INTO ExoPlayerCacheIndex4984dc511201368e VALUES (0, 'elHMZwjIWH0', X'')"
        )
    return song_db, exo_db


if __name__ == "__main__":
    unittest.main()
