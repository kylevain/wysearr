"""Small qBittorrent Web API client using cookie authentication."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

import requests

from .errors import ConfigurationError, QbittorrentError


class QbittorrentClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 15.0,
        verify_tls: bool = True,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": "WyseARR-BookBot/1",
                "Referer": f"{self.base_url}/",
            }
        )
        self._authenticated = False

    def close(self) -> None:
        self.session.close()

    def login(self) -> None:
        try:
            response = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout_seconds,
                verify=self.verify_tls,
            )
        except requests.RequestException as exc:
            raise QbittorrentError(f"qBittorrent login request failed: {exc}") from exc
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise QbittorrentError(
                f"qBittorrent authentication failed with HTTP {response.status_code}"
            )
        self._authenticated = True

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> requests.Response:
        if not self._authenticated:
            self.login()
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{endpoint}",
                params=params,
                data=data,
                timeout=self.timeout_seconds,
                verify=self.verify_tls,
            )
        except requests.RequestException as exc:
            raise QbittorrentError(f"qBittorrent API request failed: {exc}") from exc
        if response.status_code == 403 and retry_auth:
            self._authenticated = False
            self.login()
            return self._request(
                method,
                endpoint,
                params=params,
                data=data,
                retry_auth=False,
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise QbittorrentError(
                f"qBittorrent API {endpoint} returned HTTP {response.status_code}"
            ) from exc
        return response

    def application_version(self) -> str:
        return self._request("GET", "/api/v2/app/version").text.strip()

    def torrents(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v2/torrents/info")
        try:
            payload = response.json()
        except ValueError as exc:
            raise QbittorrentError("qBittorrent returned invalid torrent JSON") from exc
        if not isinstance(payload, list):
            raise QbittorrentError("qBittorrent torrent response was not a list")
        return [item for item in payload if isinstance(item, dict)]

    def categories(self) -> dict[str, dict[str, Any]]:
        response = self._request("GET", "/api/v2/torrents/categories")
        try:
            payload = response.json()
        except ValueError as exc:
            raise QbittorrentError("qBittorrent returned invalid category JSON") from exc
        if not isinstance(payload, dict):
            raise QbittorrentError("qBittorrent category response was not an object")
        return {
            str(name): details
            for name, details in payload.items()
            if isinstance(details, dict)
        }

    @staticmethod
    def _normalized_save_path(path: str) -> str:
        if not path:
            return ""
        return str(PurePosixPath(path))

    def ensure_imported_category(
        self,
        source_category: str,
        imported_category: str,
        torrent_save_path: str,
        *,
        dry_run: bool = False,
    ) -> None:
        categories = self.categories()
        source_details = categories.get(source_category, {})
        source_path = str(source_details.get("savePath") or torrent_save_path or "")
        if not source_path:
            raise ConfigurationError(
                f"qBittorrent category {source_category} has no save path"
            )
        # Imported is a lifecycle marker, not a storage move. Sharing the base
        # save path prevents automatic torrent management from relocating data.
        expected_path = source_path
        expected_normalized = self._normalized_save_path(expected_path)
        existing = categories.get(imported_category)
        if existing is not None:
            existing_path = self._normalized_save_path(
                str(existing.get("savePath") or "")
            )
            if existing_path != expected_normalized:
                raise ConfigurationError(
                    f"qBittorrent category {imported_category} has a different save path"
                )
            return
        if dry_run:
            return
        self._request(
            "POST",
            "/api/v2/torrents/createCategory",
            data={"category": imported_category, "savePath": expected_path},
        )

    def set_category(self, torrent_hash: str, category: str) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/setCategory",
            data={"hashes": torrent_hash, "category": category},
        )

    def delete_with_files(self, torrent_hash: str) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": "true"},
        )
