from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from .config import (
    Settings,
    apply_cli_overrides,
    default_config_path,
    display_settings,
    load_settings,
    update_setting,
)
from .core import (
    AndroidBridge,
    Extractor,
    ExtractorError,
    MetrolistPaths,
    human_size,
    output_name,
    resolve_catalog_metadata,
    resolve_online_artists,
    resolve_paths,
)


def color(text: str, code: str, stream=None) -> str:
    stream = stream or sys.stdout
    enabled = (
        hasattr(stream, "isatty")
        and stream.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )
    return f"\033[{code}m{text}\033[0m" if enabled else text


def heading(title: str, subtitle: str | None = None) -> None:
    print()
    print(color(title, "1;36"))
    print(color("─" * max(24, len(title)), "36"))
    if subtitle:
        print(color(subtitle, "2"))


def status_line(okay: bool, detail: str) -> None:
    marker = color("✓", "1;32") if okay else color("✗", "1;31", sys.stderr)
    label = color("OK", "1;32") if okay else color("FAIL", "1;31", sys.stderr)
    print(f"{marker} {label:<4} {detail}")


def metadata_badges(track, artwork_embedded: bool) -> str:
    entries = [
        ("title", True),
        ("artist", bool(track.artists)),
        ("album", bool(track.album)),
        ("genre", bool(track.genre)),
        ("year", bool(track.year)),
        ("song ID", True),
        ("artwork", artwork_embedded),
    ]
    badges = []
    for name, embedded in entries:
        if embedded:
            badges.append(color(f"[{name} embedded]", "32"))
        else:
            badges.append(color(f"[{name} unavailable]", "33"))
    return " ".join(badges)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="metrolist-extract",
        description="Export Metrolist offline downloads from Waydroid without re-encoding.",
        epilog=(
            "Run 'metrolist-extract help' to see every command and option, or "
            "'metrolist-extract help export' for one command."
        ),
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root.add_argument(
        "--config-file", type=Path, default=default_config_path(),
        help="configuration file (default: ~/.config/metrolist-extractor/config.toml)",
    )
    root.add_argument(
        "--no-sudo", action="store_true", default=None,
        help="run `waydroid shell` directly (overrides saved configuration)",
    )
    root.add_argument("--waydroid", help="Waydroid executable name or path")
    root.add_argument("--package", help="Android package name (default: com.metrolist.music)")
    root.add_argument("--app-root", help="Android app-data root; otherwise discovered automatically")
    root.add_argument("--database-dir", help="Android directory containing Metrolist databases")
    root.add_argument("--download-dir", help="Android Media3 offline-download cache directory")
    root.add_argument("--song-db", help="exact Android path to song.db")
    root.add_argument("--exoplayer-db", help="exact Android path to exoplayer_internal.db")
    commands = root.add_subparsers(dest="command", required=True)

    help_command = commands.add_parser("help", help="show all options or help for one command")
    help_command.add_argument(
        "topic", nargs="?", choices=("all", "doctor", "list", "export", "config"),
        help="command to explain (default: all)",
    )
    commands.add_parser("doctor", help="check Waydroid and ffmpeg availability")
    list_command = commands.add_parser("list", help="list complete downloaded tracks")
    config = commands.add_parser("config", help="show or persist alternate paths/settings")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="show effective saved settings")
    set_command = config_commands.add_parser("set", help="save a setting")
    set_command.add_argument("key")
    set_command.add_argument("value")
    unset_command = config_commands.add_parser("unset", help="restore a setting to automatic/default")
    unset_command.add_argument("key")
    export = commands.add_parser("export", help="export all complete downloaded tracks")
    export.add_argument(
        "-o", "--output", type=Path,
        help="destination directory (default: saved output_dir or ~/Music/Metrolist)",
    )
    export.add_argument("--overwrite", action="store_true", help="replace existing matching files")
    export.add_argument("--dry-run", action="store_true", help="show what would be exported")
    export.add_argument(
        "-j", "--jobs", type=int, default=4,
        help="tracks to export concurrently (default: 4; use 1 for sequential export)",
    )
    for command in (list_command, export):
        command.add_argument(
            "--offline-artists", "--no-artist-lookup", dest="offline_artists",
            action="store_true",
            help="do not use YouTube uploader metadata when Metrolist lacks an artist relation",
        )
        command.add_argument(
            "--no-album-lookup", action="store_true",
            help="do not search online for missing album metadata",
        )
        command.add_argument(
            "--no-genre-lookup", action="store_true",
            help="do not search online for missing genre metadata",
        )
        command.add_argument(
            "--no-online-metadata", action="store_true",
            help="disable online artist, album, and genre lookups",
        )
        command.add_argument(
            "--refresh-artists", action="store_true",
            help="refresh unresolved artist names instead of reusing the local lookup cache",
        )
        command.add_argument(
            "--artist-cache", type=Path,
            help="artist lookup cache (default: ~/.cache/metrolist-extractor/artists.json)",
        )
        command.add_argument(
            "--artist-workers", type=int, default=8,
            help="parallel YouTube artist lookups (default: 8)",
        )
        command.add_argument(
            "--offline-catalog", action="store_true",
            help="do not use the online music catalogue for missing genres/albums",
        )
        command.add_argument(
            "--refresh-catalog", action="store_true",
            help="refresh genre/album matches instead of reusing the local catalogue cache",
        )
        command.add_argument(
            "--catalog-cache", type=Path,
            help="catalogue cache (default: ~/.cache/metrolist-extractor/catalog.json)",
        )
        command.add_argument(
            "--catalog-workers", type=int, default=4,
            help="parallel catalogue lookups (default: 4)",
        )
    artwork = export.add_mutually_exclusive_group()
    artwork.add_argument(
        "--no-artwork", action="store_true", help="do not download or embed album artwork"
    )
    artwork.add_argument(
        "--require-artwork", action="store_true",
        help="fail a track instead of exporting it when artwork cannot be embedded",
    )
    return root


def effective_settings(args: argparse.Namespace) -> Settings:
    return apply_cli_overrides(load_settings(args.config_file), args)


def make_bridge(settings: Settings) -> AndroidBridge:
    return AndroidBridge(use_sudo=settings.use_sudo, waydroid=settings.waydroid)


def configured_paths(bridge: AndroidBridge, settings: Settings) -> MetrolistPaths:
    return resolve_paths(
        bridge,
        package=settings.package,
        app_root=settings.app_root,
        database_dir=settings.database_dir,
        download_dir=settings.download_dir,
        song_db=settings.song_db,
        exoplayer_db=settings.exoplayer_db,
    )


def scan(bridge: AndroidBridge, paths: MetrolistPaths):
    with tempfile.TemporaryDirectory(prefix="metrolist-scan-") as temporary:
        return Extractor(bridge, paths).scan(Path(temporary))


def command_doctor(bridge: AndroidBridge, settings: Settings) -> int:
    heading("Metrolist Extractor · Doctor", "Checking required tools and resolved paths")
    okay = True
    reachable, detail = bridge.is_available()
    status_line(reachable, detail)
    okay &= reachable
    ffmpeg = shutil.which("ffmpeg")
    status_line(bool(ffmpeg), f"ffmpeg: {ffmpeg or 'not found'}")
    okay &= bool(ffmpeg)
    ffprobe = shutil.which("ffprobe")
    status_line(bool(ffprobe), f"ffprobe: {ffprobe or 'not found'}")
    okay &= bool(ffprobe)
    if reachable:
        try:
            paths = configured_paths(bridge, settings)
            status_line(True, f"app root: {paths.app_root}")
            status_line(True, f"song database: {paths.song_db}")
            status_line(True, f"ExoPlayer database: {paths.exoplayer_db}")
            status_line(True, f"download cache: {paths.download_dir}")
            tracks = scan(bridge, paths)
            status_line(True, f"found {len(tracks)} complete downloaded track(s)")
        except ExtractorError as error:
            status_line(False, f"scan: {error}")
            okay = False
    return 0 if okay else 1


def complete_artist_lookup(tracks, args: argparse.Namespace) -> None:
    missing = sum(not track.artists for track in tracks)
    if not missing or args.offline_artists or args.no_online_metadata:
        return
    print(color(f"  • Resolving {missing} missing artist name(s)…", "36"))
    resolved, unresolved = resolve_online_artists(
        tracks,
        cache_path=args.artist_cache,
        refresh=args.refresh_artists,
        workers=args.artist_workers,
    )
    print(f"    Artist lookup: {resolved} resolved · {unresolved} unavailable")


def complete_catalog_lookup(tracks, args: argparse.Namespace) -> None:
    lookup_genres = not (
        args.offline_catalog or args.no_genre_lookup or args.no_online_metadata
    )
    lookup_albums = not (
        args.offline_catalog or args.no_album_lookup or args.no_online_metadata
    )
    missing_genres = sum(not track.genre for track in tracks) if lookup_genres else 0
    missing_albums = sum(not track.album for track in tracks) if lookup_albums else 0
    if not missing_genres and not missing_albums:
        return
    print(color(
        f"  • Resolving {missing_albums} missing album(s) and "
        f"{missing_genres} missing genre(s)…", "36"
    ))
    genres, albums, unresolved_genres, unresolved_albums = resolve_catalog_metadata(
        tracks,
        cache_path=args.catalog_cache,
        refresh=args.refresh_catalog,
        workers=args.catalog_workers,
        lookup_album=lookup_albums,
        lookup_genre=lookup_genres,
    )
    print(
        f"    Catalogue lookup: {albums} album(s) + {genres} genre(s) resolved · "
        f"{unresolved_albums} album(s) + {unresolved_genres} genre(s) unavailable"
    )


def command_list(bridge: AndroidBridge, settings: Settings, args: argparse.Namespace) -> int:
    heading("Metrolist Downloads", "Scanning Waydroid's completed offline-download cache")
    paths = configured_paths(bridge, settings)
    tracks = scan(bridge, paths)
    if not tracks:
        print("No complete Metrolist offline downloads were found.")
        return 0
    complete_artist_lookup(tracks, args)
    complete_catalog_lookup(tracks, args)
    print()
    for number, track in enumerate(tracks, 1):
        album = track.album or "Unknown Album"
        genre = track.genre or "Unknown Genre"
        print(color(f"{number:>3}. {track.artist} — {track.title}", "1"))
        print(
            f"     Album: {album} · Genre: {genre}\n"
            f"     {human_size(track.complete_size)} · {len(track.spans)} span(s) · "
            f"Artwork: {'yes' if track.thumbnail_url else 'no'} · ID: {track.song_id}"
        )
    print()
    print(color(f"Found {len(tracks)} complete downloaded track(s).", "1;32"))
    return 0


def command_export(bridge: AndroidBridge, settings: Settings, args: argparse.Namespace) -> int:
    heading("Metrolist Export", "Lossless stream-copy export with metadata and artwork")
    paths = configured_paths(bridge, settings)
    tracks = scan(bridge, paths)
    if not tracks:
        print("No complete Metrolist offline downloads were found.")
        return 0
    complete_artist_lookup(tracks, args)
    complete_catalog_lookup(tracks, args)
    print(color(f"  • Found {len(tracks)} complete downloaded track(s)", "36"))
    destination = args.output or (
        Path(settings.output_dir).expanduser() if settings.output_dir else Path.home() / "Music" / "Metrolist"
    )
    if args.dry_run:
        for track in tracks:
            artwork = "with artwork" if track.thumbnail_url and not args.no_artwork else "without artwork"
            print(f"  → {destination / output_name(track, 'detected-format')} · {artwork}")
        return 0

    extractor = Extractor(bridge, paths)
    exported = skipped = failed = 0

    def export_one(track):
        return extractor.export_track(
            track,
            destination,
            overwrite=args.overwrite,
            embed_artwork=not args.no_artwork,
            require_artwork=args.require_artwork,
        )

    jobs = max(1, args.jobs)
    print(color(f"  • Exporting with {jobs} concurrent job(s) to {destination}", "36"))
    print()
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(export_one, track): track for track in tracks}
        for future in as_completed(futures):
            track = futures[future]
            try:
                result = future.result()
                print(f"{color('✓ EXPORTED', '1;32')}  {result.path}")
                print(f"  Audio     [{result.codec} → .{result.container}]")
                print(f"  Metadata  {metadata_badges(track, result.artwork_embedded)}")
                if result.artwork_warning:
                    print(f"  {color('WARNING', '1;33')}   artwork: {result.artwork_warning}")
                print()
                exported += 1
            except FileExistsError as error:
                print(f"{color('↷ SKIPPED', '1;33')}   {error.args[0]}")
                print("  Already exists; use --overwrite to replace it.\n")
                skipped += 1
            except ExtractorError as error:
                print(
                    f"{color('✗ FAILED', '1;31', sys.stderr)}    "
                    f"{track.artist} — {track.title}: {error}\n",
                    file=sys.stderr,
                )
                failed += 1
    print(color("Export complete", "1"))
    print(f"  {exported} exported · {skipped} skipped · {failed} failed")
    return 1 if failed else 0


def command_config(args: argparse.Namespace) -> int:
    heading("Metrolist Extractor · Configuration")
    if args.config_command == "show":
        print(display_settings(load_settings(args.config_file), args.config_file))
        return 0
    if args.config_command == "set":
        settings = update_setting(args.config_file, args.key, args.value)
        print(f"Saved {args.key}={getattr(settings, args.key)!r} in {args.config_file}")
        return 0
    if args.config_command == "unset":
        update_setting(args.config_file, args.key, None)
        print(f"Restored {args.key} to automatic/default in {args.config_file}")
        return 0
    return 2


def command_help(args: argparse.Namespace) -> int:
    root = parser()
    subparsers = next(
        action for action in root._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    topic = args.topic or "all"
    if topic != "all":
        subparsers.choices[topic].print_help()
        return 0
    root.print_help()
    for name in ("doctor", "list", "export", "config"):
        print("\n" + "═" * 72 + "\n")
        subparsers.choices[name].print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "help":
            return command_help(args)
        if args.command == "config":
            return command_config(args)
        settings = effective_settings(args)
        bridge = make_bridge(settings)
        if args.command == "doctor":
            return command_doctor(bridge, settings)
        if args.command == "list":
            return command_list(bridge, settings, args)
        if args.command == "export":
            return command_export(bridge, settings, args)
    except ExtractorError as error:
        print(f"metrolist-extract: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
