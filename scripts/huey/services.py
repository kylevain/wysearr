"""Lazy environment-backed registry for Huey's acquisition clients."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

try:
    from .acquisition import DirectAcquirer
    from .clients import LidarrClient, ProwlarrClient, QBittorrentClient, RadarrClient, SonarrClient
except ImportError:  # pragma: no cover - direct container entrypoint
    from acquisition import DirectAcquirer
    from clients import LidarrClient, ProwlarrClient, QBittorrentClient, RadarrClient, SonarrClient


def _optional_int(value: str | None, label: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an integer") from error


class ServiceRegistry:
    """Construct service clients only when their request channel is used."""

    def __init__(self, environment: Mapping[str, str] | None = None):
        self.environment = dict(os.environ if environment is None else environment)
        self._clients: dict[str, Any] = {}

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

    def direct(self) -> DirectAcquirer:
        if "direct" in self._clients:
            return self._clients["direct"]
        prowlarr = ProwlarrClient(
            self._env("PROWLARR_URL", "http://prowlarr:9696"),
            self._env("PROWLARR_API_KEY"),
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
