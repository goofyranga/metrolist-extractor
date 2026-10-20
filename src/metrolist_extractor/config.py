from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .core import ExtractorError, PACKAGE


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "metrolist-extractor" / "config.toml"


@dataclass
class Settings:
    package: str = PACKAGE
    waydroid: str = "waydroid"
    use_sudo: bool = True
    app_root: str | None = None
    database_dir: str | None = None
    download_dir: str | None = None
    song_db: str | None = None
    exoplayer_db: str | None = None
    output_dir: str | None = None


SETTING_TYPES = {field.name: field.type for field in fields(Settings)}
PATH_KEYS = {"app_root", "database_dir", "download_dir", "song_db", "exoplayer_db", "output_dir"}


def load_settings(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    try:
        with path.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ExtractorError(f"could not read config {path}: {error}") from error
    values = document.get("metrolist", {})
    if not isinstance(values, dict):
        raise ExtractorError(f"config {path} must contain a [metrolist] table")
    unknown = set(values) - set(SETTING_TYPES)
    if unknown:
        raise ExtractorError("unknown config setting(s): " + ", ".join(sorted(unknown)))
    settings = Settings()
    for key, value in values.items():
        if key == "use_sudo":
            if not isinstance(value, bool):
                raise ExtractorError("config setting use_sudo must be true or false")
        elif not isinstance(value, str):
            raise ExtractorError(f"config setting {key} must be a string")
        setattr(settings, key, value)
    return settings


def parse_setting(key: str, raw_value: str) -> Any:
    if key not in SETTING_TYPES:
        raise ExtractorError(
            f"unknown setting {key!r}; choose from: " + ", ".join(sorted(SETTING_TYPES))
        )
    if key == "use_sudo":
        lowered = raw_value.lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
        raise ExtractorError("use_sudo must be true or false")
    return raw_value


def toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_settings(path: Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[metrolist]"]
    defaults = Settings()
    for field in fields(settings):
        value = getattr(settings, field.name)
        default = getattr(defaults, field.name)
        if value is None or value == default:
            continue
        encoded = str(value).lower() if isinstance(value, bool) else toml_string(value)
        lines.append(f"{field.name} = {encoded}")
    lines.append("")
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temporary, path)


def update_setting(path: Path, key: str, raw_value: str | None) -> Settings:
    settings = load_settings(path)
    if key not in SETTING_TYPES:
        raise ExtractorError(
            f"unknown setting {key!r}; choose from: " + ", ".join(sorted(SETTING_TYPES))
        )
    if raw_value is None:
        setattr(settings, key, getattr(Settings(), key))
    else:
        setattr(settings, key, parse_setting(key, raw_value))
    save_settings(path, settings)
    return settings


def apply_cli_overrides(settings: Settings, args: Any) -> Settings:
    for key in (
        "package", "waydroid", "app_root", "database_dir", "download_dir", "song_db", "exoplayer_db"
    ):
        value = getattr(args, key, None)
        if value is not None:
            setattr(settings, key, str(value))
    if getattr(args, "no_sudo", None):
        settings.use_sudo = False
    return settings


def display_settings(settings: Settings, path: Path) -> str:
    lines = [f"Config file: {path}"]
    for field in fields(settings):
        value = getattr(settings, field.name)
        lines.append(f"{field.name} = {value!r}")
    return "\n".join(lines)

