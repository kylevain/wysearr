"""Channel configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SUPPORTED_MEDIA_TYPES = frozenset(
    {"movies-tv", "ebooks", "audiobooks", "manga-comics", "roms", "sheet-music", "music"}
)
REQUIRED_MEDIA_TYPES = frozenset(
    {"movies-tv", "ebooks", "audiobooks", "manga-comics", "roms", "sheet-music"}
)


class ChannelConfigError(ValueError):
    """Raised for an invalid Huey channels file."""


def _channel_id(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise ChannelConfigError(f"{label} must be a Discord channel ID")
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ChannelConfigError(f"{label} must be a positive Discord channel ID")
    return text


@dataclass(frozen=True)
class ChannelConfig:
    request_channels: dict[str, str]
    request_status_channel: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ChannelConfig":
        if not isinstance(value, Mapping):
            raise ChannelConfigError("Channel configuration must be a mapping")
        requests = value.get("requests")
        if not isinstance(requests, Mapping):
            raise ChannelConfigError("Channel configuration requires a `requests` mapping")
        if any(not isinstance(media_type, str) for media_type in requests):
            raise ChannelConfigError("Request channel type names must be strings")

        missing = REQUIRED_MEDIA_TYPES.difference(requests)
        if missing:
            raise ChannelConfigError(
                "Missing request channel(s): " + ", ".join(sorted(missing))
            )
        unknown = set(requests).difference(SUPPORTED_MEDIA_TYPES)
        if unknown:
            raise ChannelConfigError(
                "Unsupported request channel type(s): " + ", ".join(sorted(unknown))
            )

        channel_map: dict[str, str] = {}
        for media_type, raw_id in requests.items():
            channel_id = _channel_id(raw_id, f"requests.{media_type}")
            if channel_id in channel_map:
                raise ChannelConfigError(
                    f"Discord channel {channel_id} is assigned to more than one media type"
                )
            channel_map[channel_id] = media_type

        activity = value.get("activity", {})
        if activity is None:
            activity = {}
        if not isinstance(activity, Mapping):
            raise ChannelConfigError("`activity` must be a mapping when present")
        status_value = activity.get("request-status")
        status_channel = (
            _channel_id(status_value, "activity.request-status")
            if status_value is not None
            else None
        )
        if status_channel in channel_map:
            raise ChannelConfigError(
                "activity.request-status must be separate from request intake channels"
            )
        return cls(channel_map, status_channel)


def validate_channel_config(value: Mapping[str, Any]) -> ChannelConfig:
    return ChannelConfig.from_mapping(value)


def load_channel_config(path: str | Path) -> ChannelConfig:
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - dependency is in the image
        raise RuntimeError("PyYAML is required to load Huey channel configuration") from error

    config_path = Path(path)
    try:
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ChannelConfigError(f"Cannot read channel configuration: {config_path}") from error
    except yaml.YAMLError as error:
        raise ChannelConfigError("Channel configuration is not valid YAML") from error
    return validate_channel_config(value)
