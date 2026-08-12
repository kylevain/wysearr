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
ACTIVITY_ROUTES = ("download-queue", "request-status", "recent-additions")
SYSTEM_ROUTES = ("import-errors", "system-health")
OPTIONAL_SYSTEM_CHANNELS = ("automation-admin",)


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
    lifecycle_channels: dict[str, str]

    def channel_for(self, route: str) -> str:
        """Return the configured Discord channel for one lifecycle route."""

        try:
            return self.lifecycle_channels[route]
        except KeyError as error:
            raise ChannelConfigError(f"Unknown Discord lifecycle route: {route}") from error

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

        activity = value.get("activity")
        system = value.get("system")
        if not isinstance(activity, Mapping):
            raise ChannelConfigError("Channel configuration requires an `activity` mapping")
        if not isinstance(system, Mapping):
            raise ChannelConfigError("Channel configuration requires a `system` mapping")

        lifecycle_channels: dict[str, str] = {}
        assigned = dict(channel_map)
        for section_name, section, required_routes in (
            ("activity", activity, ACTIVITY_ROUTES),
            ("system", system, SYSTEM_ROUTES),
        ):
            missing_routes = [route for route in required_routes if route not in section]
            if missing_routes:
                raise ChannelConfigError(
                    f"Missing {section_name} lifecycle channel(s): "
                    + ", ".join(missing_routes)
                )
            for route in required_routes:
                channel_id = _channel_id(section[route], f"{section_name}.{route}")
                if channel_id in assigned:
                    raise ChannelConfigError(
                        f"Discord channel {channel_id} is assigned more than one role; "
                        "request and lifecycle channels must be unique"
                    )
                assigned[channel_id] = route
                lifecycle_channels[route] = channel_id

        # automation-admin remains intentionally unwired, but validating its
        # inventory entry prevents an accidental collision with an operational
        # intake or lifecycle channel.
        for route in OPTIONAL_SYSTEM_CHANNELS:
            if route not in system:
                continue
            channel_id = _channel_id(system[route], f"system.{route}")
            if channel_id in assigned:
                raise ChannelConfigError(
                    f"Discord channel {channel_id} is assigned more than one role; "
                    "all configured channel IDs must be unique"
                )
            assigned[channel_id] = route

        return cls(channel_map, lifecycle_channels)


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
