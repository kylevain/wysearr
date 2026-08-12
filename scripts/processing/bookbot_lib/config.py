"""Environment-backed BookBot configuration and media routing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .errors import ConfigurationError


SAFE_METADATA_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".nfo",
        ".txt",
        ".md",
        ".opf",
        ".xml",
    }
)


@dataclass(frozen=True)
class CategorySpec:
    """A qBittorrent category and its permitted library representation."""

    name: str
    destination: tuple[str, ...]
    primary_extensions: frozenset[str]
    metadata_extensions: frozenset[str] = SAFE_METADATA_EXTENSIONS

    @property
    def imported_name(self) -> str:
        return f"{self.name}-imported"

    @property
    def allowed_extensions(self) -> frozenset[str]:
        return self.primary_extensions | self.metadata_extensions


CATEGORY_SPECS: Mapping[str, CategorySpec] = {
    "ebooks": CategorySpec(
        "ebooks",
        ("ebooks", "Books"),
        frozenset({".epub", ".mobi", ".azw3", ".pdf"}),
    ),
    "audiobooks": CategorySpec(
        "audiobooks",
        ("audiobooks",),
        frozenset(
            {
                ".mp3",
                ".m4b",
                ".m4a",
                ".aac",
                ".flac",
                ".ogg",
                ".opus",
                ".wav",
                ".cue",
            }
        ),
    ),
    "manga-comics": CategorySpec(
        "manga-comics",
        ("ebooks", "Comics"),
        frozenset({".cbz", ".cbr", ".pdf", ".epub"}),
    ),
    "roms": CategorySpec(
        "roms",
        ("roms",),
        frozenset(
            {
                ".zip",
                ".7z",
                ".nes",
                ".sfc",
                ".smc",
                ".gba",
                ".gbc",
                ".nds",
                ".iso",
                ".chd",
                ".cue",
                ".bin",
                ".rvz",
                ".wbfs",
            }
        ),
    ),
    "sheet-music": CategorySpec(
        "sheet-music",
        ("sheetmusic",),
        frozenset({".pdf", ".musicxml", ".mxl"}),
    ),
}


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _env_float(
    env: Mapping[str, str], name: str, default: float, minimum: float
) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _absolute_path(raw: str, name: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path")
    return path


@dataclass(frozen=True)
class BookBotConfig:
    torrent_root: Path
    media_root: Path
    database_path: Path
    health_path: Path
    huey_database_path: Path | None
    qbittorrent_url: str
    qbittorrent_username: str
    qbittorrent_password: str
    verify_tls: bool = True
    request_timeout_seconds: float = 15.0
    poll_seconds: int = 60
    retention_days: int = 14
    retry_base_seconds: int = 60
    retry_max_seconds: int = 3600
    max_retries: int = 10
    health_max_age_seconds: int = 300

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BookBotConfig":
        source = os.environ if env is None else env
        username = source.get("QBITTORRENT_USERNAME", "").strip()
        password = source.get("QBITTORRENT_PASSWORD", "")
        if not username:
            raise ConfigurationError("QBITTORRENT_USERNAME is required")
        if not password:
            raise ConfigurationError("QBITTORRENT_PASSWORD is required")

        url = source.get("QBITTORRENT_URL", "http://qbittorrent:8080").rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("QBITTORRENT_URL must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ConfigurationError(
                "QBITTORRENT_URL must not contain credentials; use auth variables"
            )

        torrent_root = _absolute_path(
            source.get("TORRENT_ROOT", "/downloads"), "TORRENT_ROOT"
        )
        media_root = _absolute_path(
            source.get("MEDIA_ROOT", "/media"), "MEDIA_ROOT"
        )
        database_path = _absolute_path(
            source.get("BOOKBOT_DB_PATH", "/config/bookbot.db"),
            "BOOKBOT_DB_PATH",
        )
        health_path = _absolute_path(
            source.get("BOOKBOT_HEALTH_PATH", "/config/bookbot-health.json"),
            "BOOKBOT_HEALTH_PATH",
        )
        huey_raw = source.get("HUEY_DB_PATH", source.get("HUEY_DB", "")).strip()
        huey_path = _absolute_path(huey_raw, "HUEY_DB_PATH") if huey_raw else None
        poll_seconds = _env_int(source, "POLL_SECONDS", 60, 5)

        return cls(
            torrent_root=torrent_root,
            media_root=media_root,
            database_path=database_path,
            health_path=health_path,
            huey_database_path=huey_path,
            qbittorrent_url=url,
            qbittorrent_username=username,
            qbittorrent_password=password,
            verify_tls=_env_bool(source, "QBITTORRENT_VERIFY_TLS", True),
            request_timeout_seconds=_env_float(
                source, "QBITTORRENT_TIMEOUT_SECONDS", 15.0, 1.0
            ),
            poll_seconds=poll_seconds,
            retention_days=_env_int(source, "RETENTION_DAYS", 14, 1),
            retry_base_seconds=_env_int(source, "RETRY_BASE_SECONDS", 60, 1),
            retry_max_seconds=_env_int(source, "RETRY_MAX_SECONDS", 3600, 1),
            max_retries=_env_int(source, "MAX_RETRIES", 10, 1),
            health_max_age_seconds=_env_int(
                source,
                "HEALTH_MAX_AGE_SECONDS",
                max(300, poll_seconds * 3),
                30,
            ),
        )

    def validate_filesystem(self, require_write: bool = True) -> None:
        for path, label in (
            (self.torrent_root, "TORRENT_ROOT"),
            (self.media_root, "MEDIA_ROOT"),
        ):
            if not path.exists() or not path.is_dir():
                raise ConfigurationError(f"{label} is not an existing directory")
            if path.is_symlink():
                raise ConfigurationError(f"{label} must not be a symlink")
            if require_write and not os.access(path, os.W_OK):
                raise ConfigurationError(f"{label} is not writable")

        torrent_resolved = self.torrent_root.resolve(strict=True)
        media_resolved = self.media_root.resolve(strict=True)
        if torrent_resolved == media_resolved:
            raise ConfigurationError("torrent and media roots must be different")
        if torrent_resolved in media_resolved.parents:
            raise ConfigurationError("MEDIA_ROOT must not be inside TORRENT_ROOT")
        if media_resolved in torrent_resolved.parents:
            raise ConfigurationError("TORRENT_ROOT must not be inside MEDIA_ROOT")

        for path, label in (
            (self.database_path, "BOOKBOT_DB_PATH"),
            (self.health_path, "BOOKBOT_HEALTH_PATH"),
        ):
            parent = path.parent
            if not parent.exists() or not parent.is_dir():
                raise ConfigurationError(f"parent directory for {label} is missing")
            if parent.is_symlink():
                raise ConfigurationError(f"parent directory for {label} is a symlink")
            if require_write and not os.access(parent, os.W_OK):
                raise ConfigurationError(f"parent directory for {label} is not writable")

        for spec in CATEGORY_SPECS.values():
            destination = self.media_root.joinpath(*spec.destination)
            try:
                destination.relative_to(self.media_root)
            except ValueError as exc:
                raise ConfigurationError(
                    f"destination for {spec.name} escapes MEDIA_ROOT"
                ) from exc
