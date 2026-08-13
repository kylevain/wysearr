"""HTTP clients for ARR, Prowlarr, and qBittorrent services."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlparse

import requests

try:  # Support both package imports and direct container script execution.
    from .matching import (
        normalize_identity_text,
        select_arr_candidate,
        select_shelfarr_candidate,
    )
    from .results import result, safe_display_title, sanitize_display_text
except ImportError:  # pragma: no cover - exercised by the container entrypoint
    from matching import normalize_identity_text, select_arr_candidate, select_shelfarr_candidate
    from results import result, safe_display_title, sanitize_display_text


class ServiceError(RuntimeError):
    """A deliberately sanitized integration error safe for logs and Discord."""


class ServiceRejected(ServiceError):
    """The remote service returned a definite non-success response."""


class SubmissionUncertain(ServiceError):
    """A non-idempotent request may have succeeded despite a lost response."""


LOGGER = logging.getLogger("huey.clients")
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
            error_type = (
                ServiceRejected
                if 400 <= response.status_code < 500
                and response.status_code != 408
                else ServiceError
            )
            raise error_type(
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

    def _get_entity(self, entity_id: int) -> Mapping[str, Any]:
        entity = self._request(
            "GET", self._api(f"{self.spec.entity}/{entity_id}")
        )
        if not isinstance(entity, Mapping):
            raise ServiceError(f"{self.service} returned an invalid entity response.")
        return entity

    def _entity_has_imported_media(self, entity: Mapping[str, Any]) -> bool:
        if self.service == "radarr":
            return entity.get("hasFile") is True

        statistics = entity.get("statistics")
        if not isinstance(statistics, Mapping):
            return False
        count_field = "episodeFileCount" if self.service == "sonarr" else "trackFileCount"
        return self._positive_statistic(
            statistics.get(count_field)
        ) or self._positive_statistic(statistics.get("sizeOnDisk"))

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

        return self._entity_has_imported_media(self._get_entity(internal_id))

    def submit(self, title: str) -> dict[str, str | None]:
        candidates = self.lookup(title)
        selected = select_arr_candidate(title, candidates)
        if selected is None:
            return result(
                "needs_selection",
                f"{self.service.title()} could not identify one safe match. Add a year or a more exact title.",
                service=self.service,
            )

        selected_title = safe_display_title(selected.get(self.spec.title_field), title)
        existing_id = selected.get("id")
        existing_state = "new"
        if existing_id:
            entity_id = int(existing_id)
            existing = self._get_entity(entity_id)
            if self._entity_has_imported_media(existing):
                return result(
                    "completed",
                    f"{self.service.title()} already has imported media for {selected_title} on the DAS.",
                    service=self.service,
                    external_id=entity_id,
                    external_title=selected_title,
                )
            if existing.get("monitored") is True:
                return result(
                    "queued",
                    f"{selected_title} is already monitored in {self.service.title()}; "
                    "no duplicate search was started.",
                    service=self.service,
                    external_id=entity_id,
                    external_title=selected_title,
                )
            else:
                monitored = dict(existing)
                monitored["monitored"] = True
                self._request(
                    "PUT",
                    self._api(f"{self.spec.entity}/{entity_id}"),
                    json=monitored,
                )
                existing_state = "unmonitored"
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
        if existing_state == "unmonitored":
            message = (
                f"{selected_title} already existed in {self.service.title()}; "
                "enabled monitoring and started a search."
            )
        else:
            message = (
                f"Queued {selected_title} in {self.service.title()} and started a search."
            )
        return result(
            "queued",
            message,
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


class ShelfarrClient(JsonClient):
    """Work-level ebook/audiobook request and lifecycle client."""

    BOOK_TYPES = {"ebooks": "ebook", "audiobooks": "audiobook"}
    STATUSES = frozenset(
        {
            "pending",
            "searching",
            "awaiting_purchase",
            "not_found",
            "downloading",
            "processing",
            "completed",
            "failed",
        }
    )
    MAX_PROPOSAL_CANDIDATES = 3
    MAX_SOURCE_WORK_IDS = 8
    _WORK_ID = re.compile(
        r"\A(?:hardcover|google_books|openlibrary):[A-Za-z0-9][A-Za-z0-9._:-]{0,230}\Z"
    )
    _FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")
    _SENSITIVE_IDENTITY = re.compile(
        r"(?:api[_-]?key|token|password|secret|authorization)", re.IGNORECASE
    )
    _SOURCE_LABELS = {
        "hardcover": "Hardcover",
        "google_books": "Google Books",
        "openlibrary": "Open Library",
    }

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: float = 20,
        search_limit: int = 10,
        minimum_confidence: float = 0.80,
        runner_up_gap: float = 0.05,
        language: str = "en",
    ):
        if not api_token or not api_token.strip():
            raise ServiceError("Shelfarr API token is not configured.")
        if not 1 <= int(search_limit) <= 20:
            raise ValueError("Shelfarr search_limit must be between 1 and 20")
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("Shelfarr minimum_confidence must be between 0 and 1")
        if not 0 <= runner_up_gap <= 1:
            raise ValueError("Shelfarr runner_up_gap must be between 0 and 1")
        if not language or not language.strip():
            raise ValueError("Shelfarr language cannot be empty")
        super().__init__(
            "Shelfarr",
            base_url,
            session=session,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_token.strip()}",
                "Accept": "application/json",
            },
        )
        self.search_limit = int(search_limit)
        self.minimum_confidence = minimum_confidence
        self.runner_up_gap = runner_up_gap
        self.language = language.strip()

    @classmethod
    def _safe_work_id(cls, value: object) -> str | None:
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        if (
            not text
            or len(text.encode("utf-8")) > 255
            or not cls._WORK_ID.fullmatch(text)
            or cls._SENSITIVE_IDENTITY.search(text)
        ):
            return None
        return text

    @classmethod
    def _candidate_snapshot(
        cls, candidate: Mapping[str, Any], media_type: str
    ) -> dict[str, Any] | None:
        """Return the bounded identity that may cross a Discord confirmation gap."""

        book_type = cls.BOOK_TYPES.get(media_type)
        if book_type is None or not isinstance(candidate, Mapping):
            return None
        if str(candidate.get("content_kind") or "").strip().casefold() != "book":
            return None
        available = candidate.get("available_book_types")
        if not isinstance(available, (list, tuple, set)) or book_type not in {
            str(value).strip().casefold() for value in available
        }:
            return None

        work_id = cls._safe_work_id(candidate.get("work_id"))
        sources = candidate.get("sources")
        if work_id is None or not isinstance(sources, list):
            return None
        source_work_ids: list[str] = []
        for source in sources:
            if not isinstance(source, Mapping):
                return None
            source_work_id = cls._safe_work_id(source.get("work_id"))
            if source_work_id is None:
                return None
            if source_work_id not in source_work_ids:
                source_work_ids.append(source_work_id)
            if len(source_work_ids) > cls.MAX_SOURCE_WORK_IDS:
                return None
        if not source_work_ids or source_work_ids[0] != work_id:
            return None

        title = sanitize_display_text(candidate.get("title"), limit=160)
        raw_author = candidate.get("author")
        author = (
            sanitize_display_text(raw_author, limit=160)
            if raw_author not in (None, "")
            else None
        )
        if title is None or (raw_author not in (None, "") and author is None):
            return None

        raw_year = candidate.get("year")
        if raw_year in (None, ""):
            year = None
        elif isinstance(raw_year, bool):
            return None
        else:
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                return None
            if not 0 <= year <= 9999:
                return None

        fingerprint_payload = {
            "version": 1,
            "media_type": media_type,
            "book_type": book_type,
            "content_kind": "book",
            "work_id": work_id,
            "source_work_ids": source_work_ids,
            "title": normalize_identity_text(title),
            "author": normalize_identity_text(author),
            "year": year,
        }
        encoded = json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()

        source = work_id.split(":", 1)[0]
        format_label = "Ebook" if book_type == "ebook" else "Audiobook"
        label_parts = [title]
        if author:
            label_parts.append(f"by {author}")
        if year is not None:
            label_parts.append(f"({year})")
        label_parts.extend((format_label, cls._SOURCE_LABELS[source]))
        label = sanitize_display_text(" · ".join(label_parts), limit=300)
        if label is None:
            return None

        return {
            "fingerprint": fingerprint,
            "label": label,
            "work_id": work_id,
            "source_work_ids": tuple(source_work_ids),
            "title": title,
            "author": author,
            "year": year,
            "content_kind": "book",
            "media_type": media_type,
            "book_type": book_type,
        }

    @classmethod
    def _validated_proposal_snapshot(
        cls, selected_candidate: object, media_type: str
    ) -> dict[str, Any] | None:
        """Recompute a persisted proposal fingerprint instead of trusting it."""

        if not isinstance(selected_candidate, Mapping):
            return None
        if (
            selected_candidate.get("media_type") != media_type
            or selected_candidate.get("book_type") != cls.BOOK_TYPES.get(media_type)
            or selected_candidate.get("content_kind") != "book"
        ):
            return None
        source_work_ids = selected_candidate.get("source_work_ids")
        if not isinstance(source_work_ids, (list, tuple)):
            return None
        rebuilt = cls._candidate_snapshot(
            {
                "work_id": selected_candidate.get("work_id"),
                "title": selected_candidate.get("title"),
                "author": selected_candidate.get("author"),
                "year": selected_candidate.get("year"),
                "content_kind": selected_candidate.get("content_kind"),
                "available_book_types": [selected_candidate.get("book_type")],
                "sources": [
                    {"work_id": source_work_id} for source_work_id in source_work_ids
                ],
            },
            media_type,
        )
        supplied_fingerprint = str(selected_candidate.get("fingerprint") or "")
        if (
            rebuilt is None
            or not cls._FINGERPRINT.fullmatch(supplied_fingerprint)
            or not hmac.compare_digest(rebuilt["fingerprint"], supplied_fingerprint)
            or rebuilt["label"] != selected_candidate.get("label")
        ):
            return None
        return rebuilt

    def _selection_proposal(self, selection: Any, media_type: str) -> tuple[dict[str, Any], ...]:
        if selection.reason != "ambiguous" or not selection.ranked:
            return ()
        top_score = selection.ranked[0].score
        proposals: list[dict[str, Any]] = []
        fingerprints: set[str] = set()
        labels: set[str] = set()
        for ranked in selection.ranked:
            if ranked.score < self.minimum_confidence:
                continue
            if top_score - ranked.score >= self.runner_up_gap:
                continue
            snapshot = self._candidate_snapshot(ranked.item, media_type)
            if snapshot is None or snapshot["fingerprint"] in fingerprints:
                continue
            if snapshot["label"] in labels:
                return ()
            fingerprints.add(snapshot["fingerprint"])
            labels.add(snapshot["label"])
            proposals.append(snapshot)
            if len(proposals) == self.MAX_PROPOSAL_CANDIDATES:
                break
        return tuple(proposals) if len(proposals) >= 2 else ()

    @classmethod
    def _request_payload(cls, value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ServiceError("Shelfarr returned an invalid request response.")
        request_id = value.get("id")
        status = value.get("status")
        attention_needed = value.get("attention_needed")
        nested_request = value.get("request")
        book = value.get("book")
        if (
            isinstance(request_id, bool)
            or not str(request_id or "").isdigit()
            or int(request_id) <= 0
            or not isinstance(status, str)
            or status not in cls.STATUSES
            or not isinstance(attention_needed, bool)
            or not isinstance(nested_request, Mapping)
            or not isinstance(book, Mapping)
        ):
            raise ServiceError("Shelfarr returned an invalid request response.")
        nested_id = nested_request.get("id")
        nested_status = nested_request.get("status")
        nested_attention = nested_request.get("attention_needed")
        book_id = book.get("id")
        book_title = book.get("title")
        book_type = book.get("book_type")
        book_work_id = book.get("work_id")
        if (
            isinstance(nested_id, bool)
            or not str(nested_id or "").isdigit()
            or int(nested_id) != int(request_id)
            or nested_status != status
            or nested_attention is not attention_needed
            or nested_request.get("created_via") not in {"api", "web", "telegram"}
            or nested_request.get("request_scope") not in {"single", "collection"}
            or isinstance(book_id, bool)
            or not str(book_id or "").isdigit()
            or int(book_id) <= 0
            or not isinstance(book_title, str)
            or not book_title.strip()
            or book_type not in {"ebook", "audiobook"}
            or book.get("content_kind") != "book"
            or cls._safe_work_id(book_work_id) != book_work_id
        ):
            raise ServiceError("Shelfarr returned an invalid request response.")
        return value

    @classmethod
    def _created_request_payload(
        cls,
        value: Any,
        *,
        expected_external_source: str,
    ) -> Mapping[str, Any]:
        """Validate Shelfarr's pinned synchronous single-create contract."""

        if (
            not isinstance(value, Mapping)
            or value.get("queued") is not False
            or not isinstance(value.get("warnings"), list)
            or value["warnings"]
            or not isinstance(value.get("errors"), list)
            or value["errors"]
            or not isinstance(value.get("requests"), list)
            or len(value["requests"]) != 1
        ):
            raise SubmissionUncertain(
                "Shelfarr returned an ambiguous request confirmation."
            )
        created = cls._request_payload(value["requests"][0])
        nested_request = created["request"]
        if (
            nested_request.get("created_via") != "api"
            or nested_request.get("external_source") != expected_external_source
            or nested_request.get("request_scope") != "single"
        ):
            raise SubmissionUncertain(
                "Shelfarr returned an ambiguous request confirmation."
            )
        return created

    def search(self, title: str, author: str | None = None) -> list[Mapping[str, Any]]:
        query = f"{title} {author}" if author else title
        value = self._request(
            "GET",
            "/api/v1/search",
            params={"q": query, "limit": self.search_limit, "content_kind": "book"},
        )
        if not isinstance(value, Mapping) or not isinstance(value.get("results"), list):
            raise ServiceError("Shelfarr returned an invalid search response.")
        if any(not isinstance(item, Mapping) for item in value["results"]):
            raise ServiceError("Shelfarr returned an invalid search response.")
        return value["results"]

    def get_request(self, request_id: str | int) -> Mapping[str, Any]:
        if isinstance(request_id, bool) or not str(request_id or "").isdigit():
            raise ServiceError("Shelfarr request has an invalid request ID.")
        normalized_id = int(request_id)
        if normalized_id <= 0:
            raise ServiceError("Shelfarr request has an invalid request ID.")
        return self._request_payload(
            self._request("GET", f"/api/v1/requests/{normalized_id}")
        )

    def cancel_request(self, request_id: str | int) -> Mapping[str, Any]:
        """Cancel one nonterminal Shelfarr request before Huey fails it."""

        if isinstance(request_id, bool) or not str(request_id or "").isdigit():
            raise ServiceError("Shelfarr request has an invalid request ID.")
        normalized_id = int(request_id)
        if normalized_id <= 0:
            raise ServiceError("Shelfarr request has an invalid request ID.")
        return self._request_payload(
            self._request("DELETE", f"/api/v1/requests/{normalized_id}")
        )

    def _find_correlated_request(self, request_id: int) -> Mapping[str, Any] | None:
        """Recover an API request accepted before a lost POST response."""

        marker = f"huey:{int(request_id)}"
        value = self._request(
            "GET",
            "/api/v1/requests",
            params={"created_via": "api", "limit": 100},
        )
        if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
            raise ServiceError("Shelfarr returned an invalid request list.")
        matches = []
        for item in value["requests"]:
            try:
                request = self._request_payload(item)
            except ServiceError as error:
                raise SubmissionUncertain(
                    "Shelfarr returned a malformed Huey correlation list."
                ) from error
            nested = request["request"]
            if nested.get("created_via") != "api":
                raise SubmissionUncertain(
                    "Shelfarr returned a malformed Huey correlation list."
                )
            if nested.get("external_source") == marker:
                matches.append(request)
        if len(matches) > 1:
            raise SubmissionUncertain(
                "Shelfarr returned duplicate Huey correlations."
            )
        return matches[0] if matches else None

    def _find_existing_work_request(
        self, work_ids: Iterable[str], book_type: str
    ) -> Mapping[str, Any] | None:
        """Reuse a completed/active exact work already owned by this API user."""

        value = self._request(
            "GET",
            "/api/v1/requests",
            params={"limit": 100},
        )
        if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
            raise ServiceError("Shelfarr returned an invalid request list.")
        expected_work_ids = set(work_ids)
        matches = []
        for item in value["requests"]:
            if not isinstance(item, Mapping):
                continue
            request = self._request_payload(item)
            book = request["book"]
            if (
                str(book.get("work_id") or "") in expected_work_ids
                and str(book.get("book_type") or "").casefold() == book_type
                and str(request.get("status") or "").casefold()
                in {"pending", "searching", "downloading", "processing", "completed"}
            ):
                matches.append(request)
        priority = {
            "completed": 0,
            "processing": 1,
            "downloading": 2,
            "searching": 3,
            "pending": 4,
        }

        def request_id(request: Mapping[str, Any]) -> int:
            try:
                return int(request.get("id") or 0)
            except (TypeError, ValueError):
                return 0

        matches.sort(
            key=lambda request: (
                priority.get(str(request.get("status") or "").casefold(), 99),
                -request_id(request),
            )
        )
        return matches[0] if matches else None

    def recover_request(self, request_id: int) -> Mapping[str, Any] | None:
        """Find a request accepted under Huey's durable correlation marker."""

        return self._find_correlated_request(request_id)

    @classmethod
    def recovered_request_matches_candidate(
        cls,
        remote: object,
        selected_candidate: object,
        media_type: str,
    ) -> bool:
        """Bind crash recovery to the exact work the requester confirmed.

        ``recover_request`` separately establishes the durable ``huey:<id>``
        correlation.  This check lets the reconciler require that the recovered
        request also has the confirmed format and one of the candidate's exact,
        bounded provider aliases before it accepts the correlation.
        """

        selected = cls._validated_proposal_snapshot(selected_candidate, media_type)
        if selected is None:
            return False
        try:
            request = cls._request_payload(remote)
        except ServiceError:
            return False
        book = request["book"]
        return bool(
            book["book_type"] == selected["book_type"]
            and book["content_kind"] == "book"
            and book["work_id"] in set(selected["source_work_ids"])
        )

    @staticmethod
    def _submission_result(
        created_request: Mapping[str, Any],
        *,
        book_type: str,
        fallback_title: str,
        expected_work_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        shelfarr_id = str(created_request["id"])
        shelfarr_status = str(created_request["status"]).casefold()
        returned_book = created_request["book"]
        if str(returned_book.get("book_type") or "").casefold() != book_type:
            raise ServiceError("Shelfarr returned a mismatched book request.")
        if expected_work_ids is not None and str(returned_book.get("work_id") or "") not in set(
            expected_work_ids
        ):
            raise ServiceError("Shelfarr returned a mismatched book request.")
        returned_title = safe_display_title(returned_book.get("title"), fallback_title)

        if shelfarr_status == "completed":
            return result(
                "completed",
                f"Shelfarr already has {returned_title} in its DAS library path.",
                service="shelfarr",
                external_id=shelfarr_id,
                external_title=returned_title,
                external_status=shelfarr_status,
            )
        if shelfarr_status == "failed":
            return result(
                "failed",
                f"Shelfarr could not accept {returned_title} for automatic acquisition.",
                service="shelfarr",
                external_id=shelfarr_id,
                external_title=returned_title,
                external_status=shelfarr_status,
                manual_intervention=created_request.get("attention_needed") is True,
            )
        if created_request.get("attention_needed") is True:
            return result(
                "queued",
                f"Shelfarr accepted {returned_title} but requires administrator review.",
                service="shelfarr",
                external_id=shelfarr_id,
                external_title=returned_title,
                external_status=shelfarr_status,
                manual_intervention=True,
            )
        if shelfarr_status == "awaiting_purchase":
            return result(
                "queued",
                f"Shelfarr found only a purchase/manual-upload path for {returned_title}; "
                "Huey will close this automatic acquisition attempt.",
                service="shelfarr",
                external_id=shelfarr_id,
                external_title=returned_title,
                external_status=shelfarr_status,
                manual_intervention=True,
            )
        return result(
            "queued",
            f"Shelfarr accepted {returned_title} for automatic acquisition.",
            service="shelfarr",
            external_id=shelfarr_id,
            external_title=returned_title,
            external_status=shelfarr_status,
        )

    @staticmethod
    def _selection_refresh_result() -> dict[str, Any]:
        return result(
            "needs_selection",
            "Shelfarr's metadata choices changed or could not be verified. "
            "Search again before confirming a title.",
            service="shelfarr",
        )

    def _submit_candidate(
        self,
        selected: Mapping[str, Any],
        *,
        request_id: int | None,
        discord_user_id: str | int | None,
        discord_channel_id: str | int | None,
        before_create: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Use the existing correlation boundary for one verified work."""

        book_type = str(selected["book_type"])
        selected_title = str(selected["title"])
        selected_work_id = str(selected["work_id"])
        source_work_ids = tuple(str(value) for value in selected["source_work_ids"])

        if request_id is not None:
            recovered = self._find_correlated_request(int(request_id))
            if recovered is not None:
                try:
                    return self._submission_result(
                        recovered,
                        book_type=book_type,
                        fallback_title=selected_title,
                        expected_work_ids=source_work_ids,
                    )
                except ServiceError as error:
                    raise SubmissionUncertain(
                        "Shelfarr correlation exists but its state is awaiting validation."
                    ) from error
        existing_work = self._find_existing_work_request(source_work_ids, book_type)
        if existing_work is not None:
            return self._submission_result(
                existing_work,
                book_type=book_type,
                fallback_title=selected_title,
                expected_work_ids=source_work_ids,
            )

        payload: dict[str, Any] = {
            "work_id": selected_work_id,
            "book_type": book_type,
            "title": selected_title,
            "author": str(selected.get("author") or ""),
            "content_kind": "book",
            "language": self.language,
            "external_source": (
                f"huey:{int(request_id)}" if request_id is not None else "huey"
            ),
            "source_work_ids": list(source_work_ids),
        }
        if request_id is not None:
            payload["notes"] = f"Huey request #{int(request_id)}"
        if discord_user_id is not None:
            payload["external_user_id"] = str(discord_user_id)
        if discord_channel_id is not None:
            payload["external_chat_id"] = str(discord_channel_id)
        if selected.get("year") is not None:
            payload["year"] = selected["year"]

        if before_create is not None:
            before_create()
        try:
            value = self._request(
                "POST",
                "/api/v1/requests",
                expected=(201,),
                json=payload,
            )
            created = self._created_request_payload(
                value,
                expected_external_source=str(payload["external_source"]),
            )
            return self._submission_result(
                created,
                book_type=book_type,
                fallback_title=selected_title,
                expected_work_ids=source_work_ids,
            )
        except ServiceError as submission_error:
            if request_id is None:
                raise
            try:
                recovered = self._find_correlated_request(int(request_id))
            except ServiceError as recovery_error:
                raise SubmissionUncertain(
                    "Shelfarr submission outcome is awaiting correlation recovery."
                ) from recovery_error
            if recovered is None:
                if isinstance(submission_error, ServiceRejected):
                    raise submission_error
                raise SubmissionUncertain(
                    "Shelfarr submission outcome is awaiting correlation recovery."
                ) from submission_error
            try:
                return self._submission_result(
                    recovered,
                    book_type=book_type,
                    fallback_title=selected_title,
                    expected_work_ids=source_work_ids,
                )
            except ServiceError as recovery_error:
                raise SubmissionUncertain(
                    "Shelfarr correlation exists but its state is awaiting validation."
                ) from recovery_error

    def submit(
        self,
        media_type: str,
        title: str,
        author: str | None = None,
        request_id: int | None = None,
        *,
        discord_user_id: str | int | None = None,
        discord_channel_id: str | int | None = None,
    ) -> dict[str, Any]:
        book_type = self.BOOK_TYPES.get(media_type)
        if book_type is None:
            raise ValueError(f"Unsupported Shelfarr media type: {media_type}")

        candidates = self.search(title, author)
        selection = select_shelfarr_candidate(
            title,
            author,
            media_type,
            candidates,
            minimum_confidence=self.minimum_confidence,
            runner_up_gap=self.runner_up_gap,
        )
        if selection.selected is None:
            proposal = self._selection_proposal(selection, media_type)
            if proposal:
                return result(
                    "awaiting_selection",
                    "Shelfarr found multiple close metadata matches. "
                    "Choose one of the verified title options before acquisition starts.",
                    service="shelfarr",
                    selection_proposal=proposal,
                )
            if selection.reason == "no_results":
                message = (
                    "Shelfarr could not identify this title in its metadata sources. "
                    "Check the title and author."
                )
            elif selection.reason == "ambiguous":
                message = (
                    "Shelfarr found multiple close title matches. "
                    "Add or correct the author or edition details."
                )
            else:
                message = (
                    "Shelfarr could not identify one title with enough confidence. "
                    "Add or correct the author or edition details."
                )
            return result("needs_selection", message, service="shelfarr")

        selected = self._candidate_snapshot(selection.selected, media_type)
        if selected is None:
            return self._selection_refresh_result()
        return self._submit_candidate(
            selected,
            request_id=request_id,
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
        )

    def submit_selected(
        self,
        media_type: str,
        title: str,
        author: str | None,
        request_id: int,
        *,
        selected_candidate: Mapping[str, Any],
        discord_user_id: str | int | None = None,
        discord_channel_id: str | int | None = None,
        before_create: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Continue one human-selected work only after a fresh exact recheck."""

        if self.BOOK_TYPES.get(media_type) is None:
            raise ValueError(f"Unsupported Shelfarr media type: {media_type}")
        if (
            isinstance(request_id, bool)
            or not str(request_id or "").isdigit()
            or int(request_id) <= 0
        ):
            raise ValueError("Shelfarr continuation requires a positive Huey request ID")
        persisted = self._validated_proposal_snapshot(selected_candidate, media_type)
        if persisted is None:
            return self._selection_refresh_result()

        fresh_candidates = self.search(title, author)
        fresh_selection = select_shelfarr_candidate(
            title,
            author,
            media_type,
            fresh_candidates,
            minimum_confidence=self.minimum_confidence,
            runner_up_gap=self.runner_up_gap,
        )
        matches: list[dict[str, Any]] = []
        for ranked in fresh_selection.ranked:
            if ranked.score < self.minimum_confidence:
                continue
            snapshot = self._candidate_snapshot(ranked.item, media_type)
            if snapshot is not None and hmac.compare_digest(
                snapshot["fingerprint"], persisted["fingerprint"]
            ):
                matches.append(snapshot)
        if len(matches) != 1:
            return self._selection_refresh_result()

        return self._submit_candidate(
            matches[0],
            request_id=int(request_id),
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
            before_create=before_create,
        )


class ProwlarrClient(JsonClient):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: float = 30,
        search_connect_timeout: float = 5,
        search_read_timeout: float = 90,
        search_attempts: int = 2,
        search_retry_delay: float = 1,
    ):
        if not api_key:
            raise ServiceError("Prowlarr API key is not configured.")
        if search_connect_timeout <= 0 or search_read_timeout <= 0:
            raise ValueError("Prowlarr search timeouts must be positive")
        if search_attempts < 1 or search_attempts > 3:
            raise ValueError("Prowlarr search attempts must be between 1 and 3")
        if search_retry_delay < 0:
            raise ValueError("Prowlarr search retry delay cannot be negative")
        super().__init__(
            "prowlarr",
            base_url,
            session=session,
            timeout=timeout,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )
        self.search_timeout = (search_connect_timeout, search_read_timeout)
        self.search_attempts = search_attempts
        self.search_retry_delay = search_retry_delay

    def search(self, query: str, categories: Iterable[int]) -> list[Mapping[str, Any]]:
        # Prowlarr waits synchronously for every applicable indexer before its
        # aggregate search response is complete. Keep the connect budget short,
        # but allow the read to exceed an individual indexer's timeout. This GET
        # is idempotent, so one bounded transient retry is safe; downloads and
        # qBittorrent mutations deliberately do not use this retry path.
        url = urljoin(self.base_url, "api/v1/search")
        params = {
            "query": query,
            "type": "search",
            "categories": list(categories),
            "limit": 100,
        }
        response: Any | None = None
        for attempt in range(1, self.search_attempts + 1):
            try:
                response = self.session.request(
                    "GET",
                    url,
                    headers=dict(self.headers),
                    params=params,
                    timeout=self.search_timeout,
                )
            except requests.Timeout as error:
                if attempt < self.search_attempts:
                    LOGGER.warning(
                        "Prowlarr search attempt %d/%d timed out; retrying",
                        attempt,
                        self.search_attempts,
                    )
                    if self.search_retry_delay:
                        time.sleep(self.search_retry_delay)
                    continue
                raise ServiceError(
                    "Prowlarr search timed out while waiting for indexers."
                ) from error
            except requests.ConnectionError as error:
                if attempt < self.search_attempts:
                    LOGGER.warning(
                        "Prowlarr search attempt %d/%d could not connect; retrying",
                        attempt,
                        self.search_attempts,
                    )
                    if self.search_retry_delay:
                        time.sleep(self.search_retry_delay)
                    continue
                raise ServiceError("Prowlarr is unavailable.") from error
            except requests.RequestException as error:
                raise ServiceError("Prowlarr is unavailable.") from error
            break

        if response is None:  # Defensive; the bounded loop always returns or raises.
            raise ServiceError("Prowlarr is unavailable.")
        if response.status_code not in range(200, 300):
            raise ServiceError(
                f"Prowlarr rejected the search (HTTP {response.status_code})."
            )
        try:
            value = response.json()
        except (TypeError, ValueError) as error:
            raise ServiceError("Prowlarr returned an invalid search response.") from error
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
        read-only lookups, createCategory, and addTags is safe because those
        operations are idempotent. The torrent-add POST is deliberately never
        replayed: even
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

    def find_torrent(self, torrent_hash: str) -> Mapping[str, Any] | None:
        """Return only an exact qBittorrent hash match through a read-only API."""

        if not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", torrent_hash):
            raise ServiceError("Selected result has an invalid torrent identity.")
        normalized = torrent_hash.lower()
        response = self._authenticated_request(
            "GET",
            "/api/v2/torrents/info",
            retry_after_reauthentication=True,
            params={"hashes": normalized},
        )
        try:
            value = response.json()
        except (TypeError, ValueError) as error:
            raise ServiceError("qBittorrent returned an invalid torrent response.") from error
        if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
            raise ServiceError("qBittorrent returned an invalid torrent response.")
        matches = [
            item for item in value if str(item.get("hash") or "").casefold() == normalized
        ]
        if value and len(matches) != 1:
            raise ServiceError("qBittorrent returned an invalid torrent response.")
        return matches[0] if matches else None
