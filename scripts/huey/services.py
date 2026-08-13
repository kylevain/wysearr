"""Lazy environment-backed registry for Huey's acquisition clients."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from typing import Any

try:
    from .acquisition import DirectAcquirer
    from .clients import (
        LidarrClient,
        ProwlarrClient,
        QBittorrentClient,
        RadarrClient,
        ShelfarrClient,
        SonarrClient,
    )
except ImportError:  # pragma: no cover - direct container entrypoint
    from acquisition import DirectAcquirer
    from clients import (
        LidarrClient,
        ProwlarrClient,
        QBittorrentClient,
        RadarrClient,
        ShelfarrClient,
        SonarrClient,
    )


def _optional_int(value: str | None, label: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an integer") from error


def _positive_float(value: str, label: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a number") from error
    invalid = parsed < 0 if allow_zero else parsed <= 0
    if not math.isfinite(parsed) or invalid:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be {qualifier}")
    return parsed


def _bounded_int(value: str, label: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(value: str, label: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a number") from error
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _boolean(value: str, label: str) -> bool:
    normalized = value.strip()
    if normalized == "true":
        return True
    if normalized in {"false", ""}:
        return False
    raise ValueError(f"{label} must be literal true or false")


class ServiceRegistry:
    """Construct service clients only when their request channel is used."""

    def __init__(self, environment: Mapping[str, str] | None = None):
        self.environment = dict(os.environ if environment is None else environment)
        self._clients: dict[str, Any] = {}
        self.shelfarr_enabled = _boolean(
            self.environment.get("SHELFARR_ENABLED", "false"),
            "SHELFARR_ENABLED",
        )

    def _env(self, name: str, default: str = "") -> str:
        return self.environment.get(name, default).strip()

    def _raw_env(self, name: str, default: str = "") -> str:
        return self.environment.get(name, default)

    def arr(self, service: str):
        if service in self._clients:
            return self._clients[service]
        defaults = {
            "sonarr": "http://sonarr:8989",
            "radarr": "http://radarr:7878",
            "lidarr": "http://lidarr:8686",
        }
        classes = {"sonarr": SonarrClient, "radarr": RadarrClient, "lidarr": LidarrClient}
        if service not in classes:
            raise ValueError(f"Unsupported ARR service: {service}")
        prefix = service.upper()
        client = classes[service](
            self._env(f"{prefix}_URL", defaults[service]),
            self._env(f"{prefix}_API_KEY"),
            root_folder=self._env(f"{prefix}_ROOT_FOLDER") or None,
            quality_profile_id=_optional_int(
                self._env(f"{prefix}_QUALITY_PROFILE_ID") or None,
                f"{prefix}_QUALITY_PROFILE_ID",
            ),
        )
        self._clients[service] = client
        return client

    def shelfarr(self) -> ShelfarrClient:
        """Return the Shelfarr client used for submissions and reconciliation.

        The enabled flag controls only ownership of *new* book requests.  A
        configured client remains callable after the flag is turned off so
        already-submitted Shelfarr requests can be drained before rollback.
        """

        if "shelfarr" in self._clients:
            return self._clients["shelfarr"]
        client = ShelfarrClient(
            self._env("SHELFARR_URL", "http://shelfarr"),
            self._raw_env("SHELFARR_API_TOKEN"),
            timeout=_positive_float(
                self._env("SHELFARR_TIMEOUT_SECONDS", "20"),
                "SHELFARR_TIMEOUT_SECONDS",
            ),
            search_limit=_bounded_int(
                self._env("SHELFARR_SEARCH_LIMIT", "10"),
                "SHELFARR_SEARCH_LIMIT",
                1,
                20,
            ),
            minimum_confidence=_bounded_float(
                self._env("HUEY_SHELFARR_MINIMUM_CONFIDENCE", "0.80"),
                "HUEY_SHELFARR_MINIMUM_CONFIDENCE",
                0,
                1,
            ),
            runner_up_gap=_bounded_float(
                self._env("HUEY_SHELFARR_RUNNER_UP_GAP", "0.05"),
                "HUEY_SHELFARR_RUNNER_UP_GAP",
                0,
                1,
            ),
            language=self._env("SHELFARR_LANGUAGE", "en"),
        )
        self._clients["shelfarr"] = client
        return client

    def book(self, request: Mapping[str, Any]):
        """Submit an ebook/audiobook through the selected ownership path."""

        if self.shelfarr_enabled:
            return self.shelfarr().submit(
                str(request["media_type"]),
                str(request["title"]),
                str(request["author"]) if request.get("author") else None,
                int(request["id"]),
                discord_user_id=request.get("discord_user_id"),
                discord_channel_id=request.get("channel_id"),
            )
        return self.direct().submit(
            str(request["media_type"]),
            str(request["title"]),
            str(request["author"]) if request.get("author") else None,
            int(request["id"]),
        )

    def book_selected(
        self,
        request: Mapping[str, Any],
        selected_candidate: Mapping[str, Any],
        *,
        before_create: Callable[[], None] | None = None,
    ):
        """Continue a persisted Shelfarr metadata confirmation.

        Candidate confirmation is an evaluation-only Shelfarr capability.  It
        must never fall back to the legacy direct/qBittorrent path if Shelfarr
        ownership is later disabled while a Discord prompt is outstanding.
        """

        if not self.shelfarr_enabled:
            raise RuntimeError(
                "Shelfarr ownership was disabled before candidate confirmation."
            )
        return self.shelfarr().submit_selected(
            str(request["media_type"]),
            str(request["title"]),
            str(request["author"]) if request.get("author") else None,
            int(request["id"]),
            selected_candidate=selected_candidate,
            discord_user_id=request.get("discord_user_id"),
            discord_channel_id=request.get("channel_id"),
            before_create=before_create,
        )

    def direct(self) -> DirectAcquirer:
        if "direct" in self._clients:
            return self._clients["direct"]
        prowlarr = ProwlarrClient(
            self._env("PROWLARR_URL", "http://prowlarr:9696"),
            self._env("PROWLARR_API_KEY"),
            search_connect_timeout=_positive_float(
                self._env("PROWLARR_SEARCH_CONNECT_TIMEOUT_SECONDS", "5"),
                "PROWLARR_SEARCH_CONNECT_TIMEOUT_SECONDS",
            ),
            search_read_timeout=_positive_float(
                self._env("PROWLARR_SEARCH_READ_TIMEOUT_SECONDS", "90"),
                "PROWLARR_SEARCH_READ_TIMEOUT_SECONDS",
            ),
            search_attempts=_bounded_int(
                self._env("PROWLARR_SEARCH_ATTEMPTS", "2"),
                "PROWLARR_SEARCH_ATTEMPTS",
                1,
                3,
            ),
            search_retry_delay=_positive_float(
                self._env("PROWLARR_SEARCH_RETRY_DELAY_SECONDS", "1"),
                "PROWLARR_SEARCH_RETRY_DELAY_SECONDS",
                allow_zero=True,
            ),
        )
        username = self._env("QBITTORRENT_USERNAME") or self._env("QBIT_USERNAME")
        password = self._raw_env("QBITTORRENT_PASSWORD") or self._raw_env("QBIT_PASSWORD")
        qbittorrent = QBittorrentClient(
            self._env("QBITTORRENT_URL", "http://qbittorrent:8080"), username, password
        )
        direct = DirectAcquirer(
            prowlarr,
            qbittorrent,
            minimum_confidence=float(self._env("HUEY_MINIMUM_CONFIDENCE", "0.70")),
            runner_up_gap=float(self._env("HUEY_RUNNER_UP_GAP", "0.08")),
            category_prefix=self._env("HUEY_QBIT_CATEGORY_PREFIX", ""),
        )
        self._clients["direct"] = direct
        return direct
