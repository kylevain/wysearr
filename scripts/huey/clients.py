"""HTTP clients for ARR, Prowlarr, and qBittorrent services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlparse

import requests

try:  # Support both package imports and direct container script execution.
    from .matching import select_arr_candidate
    from .results import result
except ImportError:  # pragma: no cover - exercised by the container entrypoint
    from matching import select_arr_candidate
    from results import result


class ServiceError(RuntimeError):
    """A deliberately sanitized integration error safe for logs and Discord."""


MAX_TORRENT_BYTES = 16 * 1024 * 1024
TORRENT_CHUNK_BYTES = 64 * 1024


def _base_url(value: str, service: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{service} URL must be an HTTP(S) base URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{service} URL must not contain credentials, a query, or a fragment")
    return value.rstrip("/") + "/"


class JsonClient:
    def __init__(
        self,
        service: str,
        base_url: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: float = 20,
        headers: Mapping[str, str] | None = None,
    ):
        self.service = service
        self.base_url = _base_url(base_url, service)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.headers = dict(headers or {})

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        expected: Iterable[int] = range(200, 300),
        parse_json: bool = True,
        **kwargs: Any,
    ) -> Any:
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        request_headers = dict(self.headers)
        request_headers.update(kwargs.pop("headers", {}))
        try:
            response = self.session.request(
                method,
                url,
                headers=request_headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as error:
            raise ServiceError(f"{self.service} is unavailable.") from error
        if response.status_code not in set(expected):
            raise ServiceError(
                f"{self.service} rejected the request (HTTP {response.status_code})."
            )
        if not parse_json:
            return response
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            raise ServiceError(f"{self.service} returned an invalid response.") from error


@dataclass(frozen=True)
class ArrSpec:
    name: str
    api_prefix: str
    entity: str
    title_field: str
    external_id_field: str
    search_command: str
    command_ids_field: str


ARR_SPECS = {
    "sonarr": ArrSpec(
        "sonarr", "/api/v3", "series", "title", "tvdbId", "SeriesSearch", "seriesId"
    ),
    "radarr": ArrSpec(
        "radarr", "/api/v3", "movie", "title", "tmdbId", "MoviesSearch", "movieIds"
    ),
    "lidarr": ArrSpec(
        "lidarr",
        "/api/v1",
        "artist",
        "artistName",
        "foreignArtistId",
        "ArtistSearch",
        "artistId",
    ),
}


class ArrClient(JsonClient):
    """Lookup, monitor, add, and actively search one ARR entity type."""

    def __init__(
        self,
        service: str,
        base_url: str,
        api_key: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: float = 20,
        root_folder: str | None = None,
        quality_profile_id: int | None = None,
    ):
        if service not in ARR_SPECS:
            raise ValueError(f"Unsupported ARR service: {service}")
        if not api_key:
            raise ServiceError(f"{service} API key is not configured.")
        super().__init__(
            service,
            base_url,
            session=session,
            timeout=timeout,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )
        self.spec = ARR_SPECS[service]
        self.root_folder = root_folder
        self.quality_profile_id = quality_profile_id

    def _api(self, endpoint: str) -> str:
        return f"{self.spec.api_prefix}/{endpoint.lstrip('/')}"

    def lookup(self, title: str) -> list[Mapping[str, Any]]:
        value = self._request(
            "GET", self._api(f"{self.spec.entity}/lookup"), params={"term": title}
        )
        if not isinstance(value, list):
            raise ServiceError(f"{self.service} returned an invalid lookup response.")
        return value

    def _discover_root_folder(self) -> str:
        roots = self._request("GET", self._api("rootfolder"))
        if not isinstance(roots, list):
            raise ServiceError(f"{self.service} returned invalid root folders.")
        usable = [root for root in roots if isinstance(root, Mapping) and root.get("path")]
        if self.root_folder:
            for root in usable:
                if root["path"] == self.root_folder:
                    return str(root["path"])
            raise ServiceError(f"Configured {self.service} root folder is unavailable.")
        if not usable:
            raise ServiceError(f"{self.service} has no configured root folder.")
        usable.sort(key=lambda root: (int(root.get("id") or 0), str(root["path"])))
        return str(usable[0]["path"])

    def _discover_quality_profile(self) -> int:
        profiles = self._request("GET", self._api("qualityprofile"))
        if not isinstance(profiles, list):
            raise ServiceError(f"{self.service} returned invalid quality profiles.")
        usable = [
            profile
            for profile in profiles
            if isinstance(profile, Mapping) and profile.get("id") is not None
        ]
        if self.quality_profile_id is not None:
            for profile in usable:
                if int(profile["id"]) == int(self.quality_profile_id):
                    return int(profile["id"])
            raise ServiceError(f"Configured {self.service} quality profile is unavailable.")
        if not usable:
            raise ServiceError(f"{self.service} has no configured quality profile.")
        usable.sort(key=lambda profile: (int(profile["id"]), str(profile.get("name") or "")))
        return int(usable[0]["id"])

    def _discover_metadata_profile(self) -> int:
        profiles = self._request("GET", self._api("metadataprofile"))
        if not isinstance(profiles, list):
            raise ServiceError("lidarr returned invalid metadata profiles.")
        usable = [
            profile
            for profile in profiles
            if isinstance(profile, Mapping) and profile.get("id") is not None
        ]
        if not usable:
            raise ServiceError("lidarr has no configured metadata profile.")
        usable.sort(key=lambda profile: (int(profile["id"]), str(profile.get("name") or "")))
        return int(usable[0]["id"])

    def _payload(
        self,
        candidate: Mapping[str, Any],
        root: str,
        profile_id: int,
        metadata_profile_id: int | None = None,
    ) -> dict[str, Any]:
        payload = dict(candidate)
        payload.pop("id", None)
        payload.update(
            {
                "rootFolderPath": root,
                "qualityProfileId": profile_id,
                "monitored": True,
            }
        )
        if self.service == "sonarr":
            payload["addOptions"] = {"searchForMissingEpisodes": False}
        elif self.service == "radarr":
            payload.setdefault("minimumAvailability", "released")
            payload["addOptions"] = {"searchForMovie": False}
        else:
            payload["metadataProfileId"] = metadata_profile_id
            payload["addOptions"] = {"searchForNewAlbum": False}
        return payload

    def _trigger_search(self, entity_id: int) -> None:
        ids: int | list[int]
        if self.spec.command_ids_field.endswith("Ids"):
            ids = [entity_id]
        else:
            ids = entity_id
        self._request(
            "POST",
            self._api("command"),
            json={"name": self.spec.search_command, self.spec.command_ids_field: ids},
        )

    @staticmethod
    def _positive_statistic(value: Any) -> bool:
        """Accept only an explicit positive numeric file statistic."""

        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0

    def has_imported_media(self, entity_id: str | int) -> bool:
        """Read one ARR entity and conservatively detect an imported media file.

        A missing entity, authentication problem, or unavailable ARR instance is
        surfaced as :class:`ServiceError`; callers must not turn those failures
        into a terminal request state.
        """

        try:
            internal_id = int(entity_id)
        except (TypeError, ValueError) as error:
            raise ServiceError(f"{self.service} request has an invalid entity ID.") from error
        if internal_id <= 0:
            raise ServiceError(f"{self.service} request has an invalid entity ID.")

        entity = self._request(
            "GET", self._api(f"{self.spec.entity}/{internal_id}")
        )
        if not isinstance(entity, Mapping):
            raise ServiceError(f"{self.service} returned an invalid entity response.")

        if self.service == "radarr":
            return entity.get("hasFile") is True

        statistics = entity.get("statistics")
        if not isinstance(statistics, Mapping):
            return False
        count_field = "episodeFileCount" if self.service == "sonarr" else "trackFileCount"
        return self._positive_statistic(
            statistics.get(count_field)
        ) or self._positive_statistic(statistics.get("sizeOnDisk"))

    def submit(self, title: str) -> dict[str, str | None]:
        candidates = self.lookup(title)
        selected = select_arr_candidate(title, candidates)
        if selected is None:
            return result(
                "needs_selection",
                f"{self.service.title()} could not identify one safe match. Add a year or a more exact title.",
                service=self.service,
            )

        selected_title = str(selected.get(self.spec.title_field) or title)
        existing_id = selected.get("id")
        if existing_id:
            entity_id = int(existing_id)
            if selected.get("monitored") is not True:
                monitored = dict(selected)
                monitored["monitored"] = True
                self._request(
                    "PUT",
                    self._api(f"{self.spec.entity}/{entity_id}"),
                    json=monitored,
                )
        else:
            root = self._discover_root_folder()
            profile_id = self._discover_quality_profile()
            metadata_profile_id = (
                self._discover_metadata_profile() if self.service == "lidarr" else None
            )
            added = self._request(
                "POST",
                self._api(self.spec.entity),
                json=self._payload(selected, root, profile_id, metadata_profile_id),
            )
            if not isinstance(added, Mapping) or not added.get("id"):
                raise ServiceError(f"{self.service} did not confirm the added item.")
            entity_id = int(added["id"])

        self._trigger_search(entity_id)
        return result(
            "queued",
            f"Queued {selected_title} in {self.service.title()} and started a search.",
            service=self.service,
            # The local entity ID is required for later read-only completion
            # reconciliation. Provider IDs from lookup results cannot address
            # an entity through the ARR instance API.
            external_id=entity_id,
            external_title=selected_title,
        )


class SonarrClient(ArrClient):
    def __init__(self, base_url: str, api_key: str, **kwargs: Any):
        super().__init__("sonarr", base_url, api_key, **kwargs)


class RadarrClient(ArrClient):
    def __init__(self, base_url: str, api_key: str, **kwargs: Any):
        super().__init__("radarr", base_url, api_key, **kwargs)


class LidarrClient(ArrClient):
    def __init__(self, base_url: str, api_key: str, **kwargs: Any):
        super().__init__("lidarr", base_url, api_key, **kwargs)


class ProwlarrClient(JsonClient):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: float = 30,
    ):
        if not api_key:
            raise ServiceError("Prowlarr API key is not configured.")
        super().__init__(
            "prowlarr",
            base_url,
            session=session,
            timeout=timeout,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )

    def search(self, query: str, categories: Iterable[int]) -> list[Mapping[str, Any]]:
        value = self._request(
            "GET",
            "/api/v1/search",
            params={"query": query, "type": "search", "categories": list(categories), "limit": 100},
        )
        if not isinstance(value, list):
            raise ServiceError("Prowlarr returned an invalid search response.")
        return value

    def download_torrent(self, download_url: str) -> bytes:
        if not download_url:
            raise ServiceError("Prowlarr result has no download source.")
        parsed = urlparse(download_url)
        if parsed.scheme not in {"http", "https"}:
            if parsed.scheme:
                raise ServiceError("Prowlarr result has an unsupported download source.")
            download_url = urljoin(self.base_url, download_url.lstrip("/"))
            parsed = urlparse(download_url)
        if parsed.username or parsed.password:
            raise ServiceError("Prowlarr result has an invalid download source.")
        base = urlparse(self.base_url)
        for _redirect in range(4):
            parsed = urlparse(download_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ServiceError("Prowlarr result has an invalid download source.")
            same_origin = (parsed.scheme, parsed.netloc) == (base.scheme, base.netloc)
            headers = self.headers if same_origin else {}
            try:
                response = self.session.request(
                    "GET",
                    download_url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as error:
                raise ServiceError("Prowlarr could not retrieve the selected torrent.") from error
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                if not location:
                    raise ServiceError("Prowlarr returned an invalid download redirect.")
                download_url = urljoin(download_url, location)
                continue
            if response.status_code not in range(200, 300):
                response.close()
                raise ServiceError("Prowlarr could not retrieve the selected torrent.")
            try:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except (TypeError, ValueError) as error:
                        raise ServiceError(
                            "Prowlarr returned an invalid torrent response."
                        ) from error
                    if declared_size < 0:
                        raise ServiceError("Prowlarr returned an invalid torrent response.")
                    if declared_size > MAX_TORRENT_BYTES:
                        raise ServiceError("Prowlarr torrent download is too large.")

                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_content(chunk_size=TORRENT_CHUNK_BYTES):
                    if not chunk:
                        continue
                    chunk_bytes = bytes(chunk)
                    received += len(chunk_bytes)
                    if received > MAX_TORRENT_BYTES:
                        raise ServiceError("Prowlarr torrent download is too large.")
                    chunks.append(chunk_bytes)
                if not chunks:
                    raise ServiceError("Prowlarr could not retrieve the selected torrent.")
                return b"".join(chunks)
            except requests.RequestException as error:
                raise ServiceError(
                    "Prowlarr could not retrieve the selected torrent."
                ) from error
            finally:
                response.close()
        raise ServiceError("Prowlarr returned too many download redirects.")


class QBittorrentClient(JsonClient):
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: float = 20,
    ):
        if not username or not password:
            raise ServiceError("qBittorrent credentials are not configured.")
        super().__init__("qBittorrent", base_url, session=session, timeout=timeout)
        # Give qBittorrent an explicit same-origin CSRF context for Web API
        # compatibility. The normalized base URL is already credential- and
        # query-free, so it is safe to attach to every call.
        self.headers["Referer"] = self.base_url
        self.username = username
        self.password = password
        self._authenticated = False

    def login(self) -> None:
        if self._authenticated:
            return
        response = self._request(
            "POST",
            "/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            parse_json=False,
        )
        if not response.text.strip().lower().startswith("ok"):
            raise ServiceError("qBittorrent authentication failed.")
        self._authenticated = True

    def _authenticated_request(
        self,
        method: str,
        endpoint: str,
        *,
        expected: Iterable[int] = range(200, 300),
        retry_after_reauthentication: bool,
        **kwargs: Any,
    ) -> Any:
        """Make one authenticated request with an explicit safe-retry policy.

        qBittorrent reports expired cookie sessions as HTTP 403. Repeating
        createCategory and addTags is safe because both operations are
        idempotent. The torrent-add POST is deliberately never replayed: even
        though a 403 normally means it was rejected before mutation, avoiding
        an automatic second add preserves at-most-once submission if a proxy
        or future qBittorrent version behaves unexpectedly.
        """

        self.login()
        expected_statuses = set(expected)
        response = self._request(
            method,
            endpoint,
            expected=expected_statuses | {403},
            parse_json=False,
            **kwargs,
        )
        if response.status_code != 403:
            return response

        self._authenticated = False
        if not retry_after_reauthentication:
            raise ServiceError(
                "qBittorrent session expired before the request was accepted; "
                "the torrent was not retried."
            )

        self.login()
        response = self._request(
            method,
            endpoint,
            expected=expected_statuses | {403},
            parse_json=False,
            **kwargs,
        )
        if response.status_code == 403:
            self._authenticated = False
            raise ServiceError("qBittorrent authentication expired.")
        return response

    def _ensure_category(self, category: str) -> None:
        response = self._authenticated_request(
            "POST",
            "/api/v2/torrents/createCategory",
            expected={200, 409},
            retry_after_reauthentication=True,
            data={"category": category},
        )
        if response.status_code not in {200, 409}:  # Defensive for simple test doubles.
            raise ServiceError("qBittorrent could not prepare the request category.")

    def _submit(self, *, category: str, data: Mapping[str, Any], files: Any = None) -> None:
        self._ensure_category(category)
        kwargs: dict[str, Any] = {"data": dict(data)}
        if files is not None:
            kwargs["files"] = files
        response = self._authenticated_request(
            "POST",
            "/api/v2/torrents/add",
            retry_after_reauthentication=False,
            **kwargs,
        )
        if not response.text.strip().lower().startswith("ok"):
            raise ServiceError("qBittorrent did not accept the selected torrent.")

    def add_magnet(self, magnet: str, category: str, tags: str | None = None) -> None:
        if not magnet.startswith("magnet:"):
            raise ServiceError("Selected result is not a valid magnet link.")
        data = {"urls": magnet, "category": category}
        if tags:
            data["tags"] = tags
        self._submit(category=category, data=data)

    def add_torrent(self, torrent: bytes, category: str, tags: str | None = None) -> None:
        if not torrent:
            raise ServiceError("Selected torrent file is empty.")
        data = {"category": category}
        if tags:
            data["tags"] = tags
        self._submit(
            category=category,
            data=data,
            files={"torrents": ("huey-request.torrent", torrent, "application/x-bittorrent")},
        )

    def add_tags(self, torrent_hash: str, tags: str) -> None:
        if not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", torrent_hash):
            raise ServiceError("Selected result has an invalid torrent identity.")
        self._authenticated_request(
            "POST",
            "/api/v2/torrents/addTags",
            retry_after_reauthentication=True,
            data={"hashes": torrent_hash.lower(), "tags": tags},
        )
