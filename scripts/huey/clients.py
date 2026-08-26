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
        ArrCandidate,
        RankedCandidate,
        Selection,
        normalize_text,
        normalize_identity_text,
        rank_arr_candidates,
        select_arr_candidate,
        select_shelfarr_candidate,
        title_similarity,
    )
    from .results import result, safe_display_title, sanitize_display_text
except ImportError:  # pragma: no cover - exercised by the container entrypoint
    from matching import (
        ArrCandidate,
        RankedCandidate,
        Selection,
        normalize_identity_text,
        normalize_text,
        rank_arr_candidates,
        select_arr_candidate,
        select_shelfarr_candidate,
        title_similarity,
    )
    from results import result, safe_display_title, sanitize_display_text


class ServiceError(RuntimeError):
    """A deliberately sanitized integration error safe for logs and Discord."""


class ServiceRejected(ServiceError):
    """The remote service returned a definite non-success response."""


class SubmissionUncertain(ServiceError):
    """A non-idempotent request may have succeeded despite a lost response."""


SQLITE_MAX_REQUEST_ID = 9_223_372_036_854_775_807
_HUEY_CORRELATION = re.compile(r"\Ahuey:([1-9][0-9]{0,18})\Z")
_CANONICAL_BOOK_WORK_ID = re.compile(
    r"\A(?:hardcover|google_books|openlibrary):"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,230}\Z"
)
_LAZYLIBRARIAN_WORK_ID = re.compile(r"\Alazylibrarian:[0-9a-f]{64}\Z")
_SENSITIVE_CANONICAL_WORK_ID = re.compile(
    r"(?:api[_-]?key|token|password|secret|authorization)", re.IGNORECASE
)
_LAZYLIBRARIAN_SOURCE_NAMES = {
    "google_books": "googlebooks",
    "hardcover": "hardcover",
    "openlibrary": "openlibrary",
}


def _canonical_work_aliases(identity: Mapping[str, Any]) -> frozenset[str]:
    """Return non-reversible exact-work tokens suitable for provider crossover.

    LazyLibrarian intentionally persists a digest instead of its raw metadata
    BookID.  Shelfarr persists source-qualified IDs, so normalize those through
    the same digest construction before comparing them.  This preserves the
    Discord-safe snapshot contract and also works for legacy LL identities.
    """

    source_ids = identity.get("source_work_ids")
    if not isinstance(source_ids, (list, tuple)) or not 1 <= len(source_ids) <= 8:
        return frozenset()
    aliases: set[str] = set()
    for value in source_ids:
        normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
        if _LAZYLIBRARIAN_WORK_ID.fullmatch(normalized):
            aliases.add(normalized)
            continue
        if (
            not _CANONICAL_BOOK_WORK_ID.fullmatch(normalized)
            or _SENSITIVE_CANONICAL_WORK_ID.search(normalized)
        ):
            continue
        namespace, provider_id = normalized.split(":", 1)
        source_name = _LAZYLIBRARIAN_SOURCE_NAMES.get(namespace)
        if source_name is None:
            continue
        digest = hashlib.sha256(
            json.dumps(
                [source_name, provider_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        aliases.add(f"lazylibrarian:{digest}")
    return frozenset(aliases)


def _cross_provider_work_matches(
    authoritative: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Require bibliography plus a durable discriminator across providers.

    A shared canonical source ID is exact proof.  The non-null publication year
    path preserves legacy snapshots that predate source aliases while ensuring
    a yearless title/author match can never silently switch works or editions.
    """

    if normalize_identity_text(candidate.get("title")) != normalize_identity_text(
        authoritative.get("title")
    ):
        return False
    if normalize_identity_text(candidate.get("author")) != normalize_identity_text(
        authoritative.get("author")
    ):
        return False
    if _canonical_work_aliases(authoritative).intersection(
        _canonical_work_aliases(candidate)
    ):
        return True
    year = authoritative.get("year")
    return bool(
        not isinstance(year, bool)
        and isinstance(year, int)
        and candidate.get("year") == year
    )


def _correlation_request_id(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _HUEY_CORRELATION.fullmatch(value)
    if match is None:
        return None
    request_id = int(match.group(1))
    return request_id if request_id <= SQLITE_MAX_REQUEST_ID else None


class CanonicalAcquisition(RuntimeError):
    """ABBA proved this request aliases one already-owned torrent hash."""

    def __init__(
        self,
        owner_request_id: int,
        *,
        candidate_id: str,
        canonical_candidate_id: str,
        info_hash: str,
        title: str,
    ) -> None:
        super().__init__("Audiobook acquisition is already canonically owned")
        normalized_owner = int(owner_request_id)
        if not 1 <= normalized_owner <= SQLITE_MAX_REQUEST_ID:
            raise ValueError("Canonical owner is outside SQLite request range")
        self.owner_request_id = normalized_owner
        self.candidate_id = candidate_id
        self.canonical_candidate_id = canonical_candidate_id
        self.info_hash = info_hash
        self.title = title


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

    # Only Radarr and Sonarr offer a requester-facing picker. Lidarr keeps the
    # unchanged needs_selection behavior for the music channel.
    _PICKER_PROVIDER = {"radarr": "tmdb", "sonarr": "tvdb"}
    _PICKER_OPTION_KIND = {"radarr": "movie", "sonarr": "tv"}
    # Display-only floor. A candidate below this is too weak to be worth
    # showing; it has no bearing on what select_arr_candidate auto-accepts.
    _PICKER_MIN_SIMILARITY = 0.45

    def _candidate_work_id(self, candidate: Mapping[str, Any]) -> str | None:
        """Return the stable provider identity used to re-resolve a choice."""

        provider = self._PICKER_PROVIDER.get(self.service)
        if provider is None:
            return None
        text = str(candidate.get(self.spec.external_id_field) or "").strip()
        if not text.isdigit() or len(text) > 12 or int(text) <= 0:
            return None
        return f"{self.service}:{provider}:{int(text)}"

    def _selection_proposal(
        self, ranked: Iterable[ArrCandidate]
    ) -> tuple[dict[str, Any], ...]:
        """Offer the ranked results the automatic gate declined.

        The persisted option carries only the provider identity and inert
        display text. The full ARR payload is deliberately not stored: the
        selection is re-resolved from its provider ID at dispatch time.
        """

        option_kind = self._PICKER_OPTION_KIND.get(self.service)
        if option_kind is None:
            return ()
        options: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in ranked:
            if candidate.score < self._PICKER_MIN_SIMILARITY:
                break
            work_id = self._candidate_work_id(candidate.item)
            if work_id is None or work_id in seen:
                continue
            title = sanitize_display_text(candidate.title, limit=160)
            if title is None:
                continue
            year = (
                candidate.year
                if isinstance(candidate.year, int)
                and not isinstance(candidate.year, bool)
                and 0 <= candidate.year <= 9999
                else None
            )
            label = sanitize_display_text(
                f"{title} ({year})" if year is not None else title, limit=300
            )
            if label is None:
                continue
            seen.add(work_id)
            options.append(
                {
                    "fingerprint": hashlib.sha256(work_id.encode("utf-8")).hexdigest(),
                    "label": label,
                    "work_id": work_id,
                    "source_work_ids": (work_id,),
                    "title": title,
                    "author": None,
                    "year": year,
                    "content_kind": "video",
                    "media_type": "movies-tv",
                    "book_type": option_kind,
                }
            )
            if len(options) == 3:
                break
        # The persisted contract requires at least two distinct options; a
        # single plausible result is not a choice worth asking about.
        return tuple(options) if len(options) >= 2 else ()

    def submit(self, title: str) -> dict[str, str | None]:
        candidates = self.lookup(title)
        selected = select_arr_candidate(title, candidates)
        if selected is None:
            proposal = self._selection_proposal(rank_arr_candidates(title, candidates))
            if proposal:
                return result(
                    "awaiting_selection",
                    f"{self.service.title()} found more than one possible match.",
                    service=self.service,
                    selection_proposal=proposal,
                )
            return result(
                "needs_selection",
                f"{self.service.title()} could not identify one safe match. Add a year or a more exact title.",
                service=self.service,
            )
        return self._add_and_search(selected, title)

    def submit_selected(
        self,
        work_id: str,
        *,
        fallback_title: str = "",
        before_create: Callable[..., None] | None = None,
    ) -> dict[str, str | None]:
        """Add the exact provider identity the requester confirmed."""

        provider = self._PICKER_PROVIDER.get(self.service)
        if provider is None:
            raise ServiceError(f"{self.service} does not support candidate selection.")
        prefix = f"{self.service}:{provider}:"
        text = str(work_id or "")
        if not text.startswith(prefix) or not text[len(prefix) :].isdigit():
            raise ServiceError(f"{self.service} received an invalid selected identity.")
        provider_id = int(text[len(prefix) :])
        matches = [
            item
            for item in self.lookup(f"{provider}:{provider_id}")
            if isinstance(item, Mapping)
            and str(item.get(self.spec.external_id_field) or "") == str(provider_id)
        ]
        if len(matches) != 1:
            raise ServiceError(
                f"{self.service} could not resolve the confirmed title to one exact entry."
            )
        return self._add_and_search(
            matches[0], fallback_title, before_create=before_create
        )

    def _add_and_search(
        self,
        selected: Mapping[str, Any],
        title: str,
        *,
        before_create: Callable[..., None] | None = None,
    ) -> dict[str, str | None]:
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
                # Cross the durable dispatch boundary before the first mutation
                # so a restart can tell a confirmed choice from a submitted one.
                if before_create is not None:
                    before_create()
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
            if before_create is not None:
                before_create()
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
        matches = []
        page_size = 100
        value = self._request(
            "GET",
            "/api/v1/requests",
            params={"created_via": "api", "limit": page_size},
        )
        if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
            raise ServiceError("Shelfarr returned an invalid request list.")
        page = value["requests"]
        for item in page:
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
        if matches:
            return matches[0]
        if len(page) >= page_size:
            raise SubmissionUncertain(
                "Shelfarr correlation is outside its safe inspection horizon."
            )
        return None

    def _find_existing_work_request(
        self, work_ids: Iterable[str], book_type: str
    ) -> Mapping[str, Any] | None:
        """Reuse a completed/active exact work already owned by this API user."""

        page_size = 100
        value = self._request(
            "GET",
            "/api/v1/requests",
            params={"limit": page_size},
        )
        if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
            raise ServiceError("Shelfarr returned an invalid request list.")
        expected_work_ids = set(work_ids)
        matches = []
        for item in value["requests"]:
            if not isinstance(item, Mapping):
                raise SubmissionUncertain(
                    "Shelfarr returned a malformed duplicate-work list."
                )
            try:
                request = self._request_payload(item)
            except ServiceError as error:
                raise SubmissionUncertain(
                    "Shelfarr returned a malformed duplicate-work list."
                ) from error
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
        if matches:
            return matches[0]
        if len(value["requests"]) >= page_size:
            raise SubmissionUncertain(
                "Shelfarr duplicate-work inspection reached its safe horizon."
            )
        return None

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
        resolved_identity: Mapping[str, Any] | None = None,
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
                resolved_identity=resolved_identity,
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
                resolved_identity=resolved_identity,
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
                resolved_identity=resolved_identity,
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
                resolved_identity=resolved_identity,
            )
        return result(
            "queued",
            f"Shelfarr accepted {returned_title} for automatic acquisition.",
            service="shelfarr",
            external_id=shelfarr_id,
            external_title=returned_title,
            external_status=shelfarr_status,
            resolved_identity=resolved_identity,
        )

    @staticmethod
    def _selection_refresh_result() -> dict[str, Any]:
        return result(
            "needs_selection",
            "Shelfarr's metadata choices changed or could not be verified. "
            "Search again before confirming a title.",
            service="shelfarr",
            backend_outcome="ambiguous",
        )

    def _submit_candidate(
        self,
        selected: Mapping[str, Any],
        *,
        request_id: int | None,
        discord_user_id: str | int | None,
        discord_channel_id: str | int | None,
        before_create: Callable[[], None] | None = None,
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
    ) -> dict[str, Any]:
        """Use the existing correlation boundary for one verified work."""

        book_type = str(selected["book_type"])
        selected_title = str(selected["title"])
        selected_work_id = str(selected["work_id"])
        source_work_ids = tuple(str(value) for value in selected["source_work_ids"])

        if on_resolved is not None:
            on_resolved(selected, selected_work_id)

        if request_id is not None:
            recovered = self._find_correlated_request(int(request_id))
            if recovered is not None:
                try:
                    return self._submission_result(
                        recovered,
                        book_type=book_type,
                        fallback_title=selected_title,
                        expected_work_ids=source_work_ids,
                        resolved_identity=selected,
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
                resolved_identity=selected,
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
                resolved_identity=selected,
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
                    resolved_identity=selected,
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
        before_create: Callable[[], None] | None = None,
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
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
                    backend_outcome="ambiguous",
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
            return result(
                "needs_selection",
                message,
                service="shelfarr",
                backend_outcome=(
                    "ambiguous" if selection.reason == "ambiguous" else "miss"
                ),
            )

        selected = self._candidate_snapshot(selection.selected, media_type)
        if selected is None:
            return self._selection_refresh_result()
        return self._submit_candidate(
            selected,
            request_id=request_id,
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
            before_create=before_create,
            on_resolved=on_resolved,
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
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
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
            return result(
                "needs_selection",
                "The selected work is not available from this acquisition backend.",
                service="shelfarr",
                backend_outcome="miss" if not matches else "ambiguous",
                resolved_identity=persisted,
            )

        return self._submit_candidate(
            matches[0],
            request_id=int(request_id),
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
            before_create=before_create,
            on_resolved=on_resolved,
        )

    def submit_authoritative(
        self,
        media_type: str,
        request_id: int,
        *,
        resolved_identity: Mapping[str, Any],
        discord_user_id: str | int | None = None,
        discord_channel_id: str | int | None = None,
        before_create: Callable[[], None] | None = None,
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
    ) -> dict[str, Any]:
        """Acquire one already-resolved work without opening a second prompt."""

        expected_type = self.BOOK_TYPES.get(media_type)
        if expected_type != "ebook" or not isinstance(resolved_identity, Mapping):
            raise ValueError("Shelfarr authoritative continuation requires an ebook")
        work_id = str(resolved_identity.get("work_id") or "")
        same_provider_identity = not work_id.startswith("lazylibrarian:")
        persisted = (
            self._validated_proposal_snapshot(resolved_identity, media_type)
            if same_provider_identity
            else None
        )
        if same_provider_identity and persisted is None:
            return self._selection_refresh_result()
        title = sanitize_display_text(resolved_identity.get("title"), limit=160)
        raw_author = resolved_identity.get("author")
        author = (
            sanitize_display_text(raw_author, limit=160)
            if raw_author not in (None, "")
            else None
        )
        year = resolved_identity.get("year")
        if (
            title is None
            or (raw_author not in (None, "") and author is None)
            or isinstance(year, bool)
            or (year is not None and (not isinstance(year, int) or not 0 <= year <= 9999))
        ):
            return self._selection_refresh_result()

        exact: list[dict[str, Any]] = []
        for candidate in self.search(title, author):
            snapshot = self._candidate_snapshot(candidate, media_type)
            if snapshot is None:
                continue
            if same_provider_identity:
                if hmac.compare_digest(
                    snapshot["fingerprint"], persisted["fingerprint"]
                ):
                    exact.append(snapshot)
                continue
            if _cross_provider_work_matches(resolved_identity, snapshot):
                exact.append(snapshot)
        if len(exact) != 1:
            return result(
                "needs_selection",
                "No acquisition backend could prove one exact match for the selected work.",
                service="shelfarr",
                backend_outcome="miss" if not exact else "ambiguous",
                resolved_identity=resolved_identity,
            )
        return self._submit_candidate(
            exact[0],
            request_id=int(request_id),
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
            before_create=before_create,
            on_resolved=on_resolved,
        )


class LazyLibrarianClient(JsonClient):
    """Strict ebook-only client for LazyLibrarian's supported HTTP API.

    LazyLibrarian accepts command parameters, including its API key, as POST
    form data on the clean ``/api`` URL.  This client deliberately owns that
    request path instead of using :class:`JsonClient`, so neither prepared
    URLs nor transport exceptions can expose credentials.  No response text
    or transport exception is ever copied into a Huey exception or log message.
    """

    MAX_PROPOSAL_CANDIDATES = 3
    MAX_METADATA_RESULTS = 500
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    _WORK_ID = re.compile(r"\Alazylibrarian:[0-9a-f]{64}\Z")
    _FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")
    _DOWNLOAD_ID = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
    _BOOK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
    _SOURCE = re.compile(r"\A[A-Za-z][A-Za-z0-9_-]{0,63}\Z")

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        session: requests.Session | Any | None = None,
        qbittorrent: Any | None = None,
        timeout: float = 30,
        search_limit: int = 10,
        metadata_source: str = "OpenLibrary",
        minimum_confidence: float = 0.80,
        runner_up_gap: float = 0.05,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ServiceError("LazyLibrarian API key is not configured.")
        if (
            any(not character.isprintable() for character in api_key)
            or len(api_key) > 512
        ):
            raise ServiceError("LazyLibrarian API key is invalid.")
        if (
            isinstance(search_limit, bool)
            or not isinstance(search_limit, int)
            or not 1 <= search_limit <= 20
        ):
            raise ValueError("LazyLibrarian search_limit must be between 1 and 20")
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("LazyLibrarian minimum_confidence must be between 0 and 1")
        if not 0 <= runner_up_gap <= 1:
            raise ValueError("LazyLibrarian runner_up_gap must be between 0 and 1")
        source = str(metadata_source or "").strip()
        if not self._SOURCE.fullmatch(source):
            raise ValueError("LazyLibrarian metadata source is invalid")
        super().__init__(
            "LazyLibrarian",
            base_url,
            session=session,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._api_key = api_key.strip()
        self.qbittorrent = qbittorrent
        self.search_limit = search_limit
        self.metadata_source = source
        self.minimum_confidence = minimum_confidence
        self.runner_up_gap = runner_up_gap

    @staticmethod
    def _casefold_mapping(value: object, response_name: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ServiceError(
                f"LazyLibrarian returned an invalid {response_name} response."
            )
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in normalized:
                raise ServiceError(
                    f"LazyLibrarian returned an invalid {response_name} response."
                )
            normalized[key.casefold()] = item
        return normalized

    @classmethod
    def _unwrap_envelope(cls, value: object) -> object:
        """Unwrap LL's optional Success/Data/Error response contract."""

        if not isinstance(value, Mapping):
            return value
        folded = cls._casefold_mapping(value, "API")
        if "success" not in folded:
            return value
        if not isinstance(folded["success"], bool):
            raise ServiceError("LazyLibrarian returned an invalid API response.")
        if folded["success"] is not True:
            raise ServiceRejected("LazyLibrarian rejected the API command.")
        if "data" not in folded:
            raise ServiceError("LazyLibrarian returned an invalid API response.")
        return folded["data"]

    def _command(self, command: str, **arguments: object) -> object:
        """Run one API command with credentials confined to the form body."""

        form = {"apikey": self._api_key, "cmd": command, **arguments}
        try:
            response = self.session.request(
                "POST",
                urljoin(self.base_url, "api"),
                headers=dict(self.headers),
                timeout=self.timeout,
                data=form,
            )
        except requests.RequestException:
            raise ServiceError("LazyLibrarian is unavailable.") from None
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            raise ServiceError("LazyLibrarian returned an invalid response.")
        if not 200 <= status < 300:
            error_type = (
                ServiceRejected
                if 400 <= status < 500 and status != 408
                else ServiceError
            )
            raise error_type(
                f"LazyLibrarian rejected the request (HTTP {status})."
            ) from None

        try:
            value = response.json()
        except (TypeError, ValueError):
            raw_text = getattr(response, "text", "")
            if (
                not isinstance(raw_text, str)
                or len(raw_text.encode("utf-8")) > self.MAX_RESPONSE_BYTES
            ):
                raise ServiceError("LazyLibrarian returned an invalid response.") from None
            text = raw_text.strip()
            if not text:
                raise ServiceError("LazyLibrarian returned an invalid response.") from None
            try:
                value = json.loads(text)
            except (TypeError, ValueError):
                value = text
        try:
            encoded_size = len(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except (TypeError, ValueError, OverflowError):
            raise ServiceError("LazyLibrarian returned an invalid response.") from None
        if encoded_size > self.MAX_RESPONSE_BYTES:
            raise ServiceError("LazyLibrarian returned an invalid response.")
        return self._unwrap_envelope(value)

    @classmethod
    def _safe_book_id(cls, value: object) -> str | None:
        text = unicodedata.normalize("NFKC", str(value or "")).strip()
        return text if cls._BOOK_ID.fullmatch(text) else None

    def _work_id(self, book_id: str) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [self.metadata_source.casefold(), book_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return f"lazylibrarian:{digest}"

    @staticmethod
    def _year(*values: object) -> int | None:
        for value in values:
            if value in (None, "", 0, "0") or isinstance(value, bool):
                continue
            match = re.match(r"\A((?:1[0-9]{3}|2[0-9]{3}))", str(value).strip())
            if match:
                return int(match.group(1))
        return None

    def _metadata_candidate(self, value: object) -> dict[str, Any]:
        fields = self._casefold_mapping(value, "metadata search")
        book_id = self._safe_book_id(fields.get("bookid"))
        raw_title = fields.get("bookname")
        raw_author = fields.get("authorname")
        raw_source = fields.get("source")
        title = sanitize_display_text(raw_title, limit=160)
        author = sanitize_display_text(raw_author, limit=160)
        source = str(raw_source or "").strip()
        if (
            book_id is None
            or not isinstance(raw_title, str)
            or title is None
            or not isinstance(raw_author, str)
            or author is None
            or not isinstance(raw_source, str)
            or source.casefold() != self.metadata_source.casefold()
        ):
            raise ServiceError("LazyLibrarian returned an invalid metadata search result.")
        return {
            "_book_id": book_id,
            "work_id": self._work_id(book_id),
            "title": title,
            "author": author,
            "year": self._year(fields.get("bookpub"), fields.get("bookdate")),
            "content_kind": "book",
            "available_book_types": ("ebook",),
        }

    def search(self, title: str, author: str | None = None) -> list[dict[str, Any]]:
        value = self._command(
            "findBook", name=title, source=self.metadata_source
        )
        if not isinstance(value, list) or len(value) > self.MAX_METADATA_RESULTS:
            raise ServiceError("LazyLibrarian returned an invalid metadata search response.")
        candidates = [self._metadata_candidate(item) for item in value]
        if len({item["_book_id"] for item in candidates}) != len(candidates):
            raise ServiceError("LazyLibrarian returned duplicate metadata identities.")
        return candidates[: self.search_limit]

    def _candidate_snapshot(self, candidate: Mapping[str, Any]) -> dict[str, Any] | None:
        if not isinstance(candidate, Mapping):
            return None
        work_id = str(candidate.get("work_id") or "")
        title = sanitize_display_text(candidate.get("title"), limit=160)
        raw_author = candidate.get("author")
        author = (
            sanitize_display_text(raw_author, limit=160)
            if raw_author not in (None, "")
            else None
        )
        year = candidate.get("year")
        if (
            not self._WORK_ID.fullmatch(work_id)
            or title is None
            or (raw_author not in (None, "") and author is None)
            or isinstance(year, bool)
            or (year is not None and (not isinstance(year, int) or not 0 <= year <= 9999))
        ):
            return None
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "version": 1,
                    "work_id": work_id,
                    "title": normalize_identity_text(title),
                    "author": normalize_identity_text(author),
                    "year": year,
                    "source": self.metadata_source.casefold(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        label_parts = [title]
        if author:
            label_parts.append(f"by {author}")
        if year is not None:
            label_parts.append(f"({year})")
        label_parts.extend(("Ebook", self.metadata_source))
        label = sanitize_display_text(" · ".join(label_parts), limit=300)
        if label is None:
            return None
        return {
            "fingerprint": fingerprint,
            "label": label,
            "work_id": work_id,
            "source_work_ids": (work_id,),
            "title": title,
            "author": author,
            "year": year,
            "content_kind": "book",
            "media_type": "ebooks",
            "book_type": "ebook",
        }

    def _validated_snapshot(self, value: object) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        if (
            value.get("media_type") != "ebooks"
            or value.get("book_type") != "ebook"
            or value.get("content_kind") != "book"
            or tuple(value.get("source_work_ids") or ())
            != (str(value.get("work_id") or ""),)
        ):
            return None
        rebuilt = self._candidate_snapshot(value)
        supplied = str(value.get("fingerprint") or "")
        if (
            rebuilt is None
            or not self._FINGERPRINT.fullmatch(supplied)
            or not hmac.compare_digest(rebuilt["fingerprint"], supplied)
            or rebuilt["label"] != value.get("label")
        ):
            return None
        return rebuilt

    def _selection_proposal(self, selection: Selection) -> tuple[dict[str, Any], ...]:
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
            snapshot = self._candidate_snapshot(ranked.item)
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

    @staticmethod
    def _mutation_acknowledged(value: object) -> bool:
        if value is True:
            return True
        if isinstance(value, str) and value.strip().casefold() == "ok":
            return True
        return False

    def _all_books(self) -> list[Mapping[str, Any]]:
        value = self._command("getAllBooks")
        if not isinstance(value, list):
            raise ServiceError("LazyLibrarian returned an invalid book list.")
        return value

    def _exact_book(self, book_id: str) -> dict[str, Any] | None:
        matches: list[dict[str, Any]] = []
        for raw in self._all_books():
            fields = self._casefold_mapping(raw, "book list")
            raw_id = self._safe_book_id(fields.get("bookid"))
            if raw_id != book_id:
                continue
            title = sanitize_display_text(fields.get("bookname"), limit=160)
            author = sanitize_display_text(fields.get("authorname"), limit=160)
            if title is None or author is None:
                raise ServiceError("LazyLibrarian returned an invalid matching book.")
            matches.append(
                {
                    "book_id": raw_id,
                    "title": title,
                    "author": author,
                    "year": self._year(fields.get("bookpub"), fields.get("bookdate")),
                }
            )
        if len(matches) > 1:
            raise ServiceError("LazyLibrarian returned duplicate matching books.")
        return matches[0] if matches else None

    @staticmethod
    def _book_matches_candidate(
        book: Mapping[str, Any], candidate: Mapping[str, Any]
    ) -> bool:
        return bool(
            normalize_identity_text(book.get("title"))
            == normalize_identity_text(candidate.get("title"))
            and normalize_identity_text(book.get("author"))
            == normalize_identity_text(candidate.get("author"))
        )

    def _exact_download_id(self, book_id: str) -> str | None:
        value = self._command("getHistory")
        if not isinstance(value, list):
            raise ServiceError("LazyLibrarian returned an invalid history response.")
        matching_rows: list[dict[str, Any]] = []
        for raw in value:
            fields = self._casefold_mapping(raw, "history")
            if str(fields.get("bookid") or "") != book_id:
                continue
            if str(fields.get("auxinfo") or "").casefold() != "ebook":
                continue
            matching_rows.append(fields)
        if not matching_rows:
            return None
        hashes: set[str] = set()
        for row in matching_rows:
            source = str(row.get("source") or "").strip()
            download_id = str(row.get("downloadid") or "").strip()
            status = str(row.get("status") or "").strip().casefold()
            # LL can retain benign history placeholders from an earlier
            # provider pass.  They carry neither a downloader nor an identity
            # and are not evidence of a handoff.  Any half-populated or
            # unsupported identity is unsafe to correlate, however.
            if not source and not download_id:
                continue
            if (
                source.casefold() != "qbittorrent"
                or not self._DOWNLOAD_ID.fullmatch(download_id)
            ):
                raise ServiceRejected(
                    "LazyLibrarian did not confirm a supported qBittorrent handoff."
                )
            if status not in {"snatched", "seeding"}:
                # A completed/failed historical row is not proof that this
                # invocation handed anything to qBittorrent.
                continue
            hashes.add(download_id.casefold())
        if not hashes:
            return None
        if len(hashes) != 1:
            raise ServiceRejected(
                "LazyLibrarian returned conflicting download identities."
            )
        return hashes.pop()

    def _validate_qbittorrent_handoff(self, download_id: str) -> str:
        """Require LL history to resolve to one exact BookBot-owned path.

        A very small or cached payload can cross from ``ebooks`` to
        ``ebooks-imported`` before LL returns its waited search response.  The
        latter remains nonterminal here: BookBot's imported-ledger
        reconciliation is the only component allowed to mark Huey complete.
        """

        if self.qbittorrent is None or not callable(
            getattr(self.qbittorrent, "find_torrent", None)
        ):
            raise ServiceError(
                "LazyLibrarian qBittorrent verification is unavailable."
            )
        torrent = self.qbittorrent.find_torrent(download_id)
        if not isinstance(torrent, Mapping):
            raise ServiceRejected(
                "LazyLibrarian's qBittorrent handoff could not be verified."
            )
        returned_hash = str(torrent.get("hash") or "").strip().casefold()
        category = str(torrent.get("category") or "").strip()
        save_path = str(torrent.get("save_path") or "").strip()
        if (
            returned_hash != download_id.casefold()
            or category not in {"ebooks", "ebooks-imported"}
            or save_path != "/downloads/ebooks"
        ):
            raise ServiceRejected(
                "LazyLibrarian's qBittorrent handoff is outside BookBot's ebook intake."
            )
        return category

    def _acquire_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        request_id: int | None,
        before_create: Callable[[str], None] | None,
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
        release_preflight: Callable[[str, str | None], bool] | None = None,
    ) -> dict[str, Any]:
        book_id = self._safe_book_id(candidate.get("_book_id"))
        if book_id is None:
            raise ServiceError("LazyLibrarian candidate identity is invalid.")
        selected_title = safe_display_title(candidate.get("title"))
        resolved_identity = self._candidate_snapshot(candidate)
        if resolved_identity is None:
            raise ServiceError("LazyLibrarian candidate identity is invalid.")
        if on_resolved is not None:
            on_resolved(resolved_identity, book_id)
        if release_preflight is not None and not release_preflight(
            str(resolved_identity["title"]),
            (
                str(resolved_identity["author"])
                if resolved_identity.get("author")
                else None
            ),
        ):
            return result(
                "failed",
                "No usable ebook release is currently available.",
                service="lazylibrarian",
                external_title=selected_title,
                external_status="not_found",
                backend_outcome="miss",
                resolved_identity=resolved_identity,
            )
        if before_create is not None:
            before_create(book_id)
        try:
            added = self._command(
                "addBook",
                id=book_id,
                wait="1",
                source=self.metadata_source,
            )
            if not self._mutation_acknowledged(added):
                raise ServiceRejected("LazyLibrarian could not add the selected book.")
            book = self._exact_book(book_id)
            if book is None or not self._book_matches_candidate(book, candidate):
                raise ServiceRejected(
                    "LazyLibrarian could not verify the selected book identity."
                )
            queued = self._command("queueBook", id=book_id, type="eBook")
            if not self._mutation_acknowledged(queued):
                raise ServiceRejected("LazyLibrarian could not queue the selected ebook.")
            searched = self._command(
                "searchBook", id=book_id, type="eBook", wait="1"
            )
            if not self._mutation_acknowledged(searched):
                raise ServiceRejected("LazyLibrarian could not run the ebook search.")
            download_id = self._exact_download_id(book_id)
            handoff_category = (
                self._validate_qbittorrent_handoff(download_id)
                if download_id is not None
                else None
            )
        except ServiceError:
            raise SubmissionUncertain(
                "LazyLibrarian submission requires exact history reconciliation."
            ) from None

        if download_id is None:
            # The pinned LL API returns ``OK`` even when its internal search
            # catches a provider/downloader exception, and normal no-match has
            # no durable marker.  Empty history is therefore not proof of a
            # clean miss.  Keep the mutation owned for reconciliation instead
            # of allowing a second backend to race a late LL handoff.
            raise SubmissionUncertain(
                "LazyLibrarian search completion has no exact handoff evidence."
            )
        imported_awaiting_bookbot = handoff_category == "ebooks-imported"
        return result(
            "queued",
            (
                f"LazyLibrarian handed {selected_title} to BookBot; "
                "completion is being reconciled."
                if imported_awaiting_bookbot
                else f"LazyLibrarian queued {selected_title} in qBittorrent."
            ),
            service="lazylibrarian",
            external_id=download_id,
            external_title=selected_title,
            external_status=(
                "processing" if imported_awaiting_bookbot else "queued"
            ),
            resolved_identity=resolved_identity,
        )

    @staticmethod
    def _selection_refresh_result() -> dict[str, Any]:
        return result(
            "needs_selection",
            "LazyLibrarian's metadata choices changed or could not be verified. "
            "Submit the title again to search anew.",
            service="lazylibrarian",
            backend_outcome="ambiguous",
        )

    def submit(
        self,
        media_type: str,
        title: str,
        author: str | None = None,
        request_id: int | None = None,
        *,
        before_create: Callable[[str], None] | None = None,
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
        release_preflight: Callable[[str, str | None], bool] | None = None,
    ) -> dict[str, Any]:
        if media_type != "ebooks":
            raise ValueError("LazyLibrarian is restricted to ebook requests")
        candidates = self.search(title, author)
        selection = select_shelfarr_candidate(
            title,
            author,
            "ebooks",
            candidates,
            minimum_confidence=self.minimum_confidence,
            runner_up_gap=self.runner_up_gap,
        )
        if selection.selected is None:
            proposal = self._selection_proposal(selection)
            if proposal:
                return result(
                    "awaiting_selection",
                    "LazyLibrarian found multiple close metadata matches. "
                    "Choose one verified title before acquisition starts.",
                    service="lazylibrarian",
                    selection_proposal=proposal,
                    backend_outcome="ambiguous",
                )
            if selection.reason == "no_results":
                message = (
                    "LazyLibrarian found no matching metadata result. "
                    "Check the title and author."
                )
            elif selection.reason == "ambiguous":
                message = (
                    "LazyLibrarian found multiple indistinguishable matches. "
                    "Add or correct the author or edition details."
                )
            else:
                message = (
                    "LazyLibrarian could not identify one safe metadata match. "
                    "Add or correct the author or edition details."
                )
            return result(
                "needs_selection",
                message,
                service="lazylibrarian",
                backend_outcome=(
                    "ambiguous" if selection.reason == "ambiguous" else "miss"
                ),
            )
        return self._acquire_candidate(
            selection.selected,
            request_id=request_id,
            before_create=before_create,
            on_resolved=on_resolved,
            release_preflight=release_preflight,
        )

    def submit_selected(
        self,
        media_type: str,
        title: str,
        author: str | None,
        request_id: int,
        *,
        selected_candidate: Mapping[str, Any],
        before_create: Callable[[str], None] | None = None,
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
        release_preflight: Callable[[str, str | None], bool] | None = None,
    ) -> dict[str, Any]:
        if media_type != "ebooks":
            raise ValueError("LazyLibrarian is restricted to ebook requests")
        if isinstance(request_id, bool) or int(request_id) <= 0:
            raise ValueError("LazyLibrarian continuation requires a request ID")
        persisted = self._validated_snapshot(selected_candidate)
        if persisted is None:
            return self._selection_refresh_result()
        fresh = self.search(title, author)
        matching: list[Mapping[str, Any]] = []
        for candidate in fresh:
            snapshot = self._candidate_snapshot(candidate)
            if snapshot is not None and hmac.compare_digest(
                snapshot["fingerprint"], persisted["fingerprint"]
            ):
                matching.append(candidate)
        if len(matching) != 1:
            return result(
                "needs_selection",
                "The selected work is not available from this acquisition backend.",
                service="lazylibrarian",
                backend_outcome="miss" if not matching else "ambiguous",
                resolved_identity=persisted,
            )
        return self._acquire_candidate(
            matching[0],
            request_id=request_id,
            before_create=before_create,
            on_resolved=on_resolved,
            release_preflight=release_preflight,
        )

    def submit_authoritative(
        self,
        media_type: str,
        request_id: int,
        *,
        resolved_identity: Mapping[str, Any],
        before_create: Callable[[str], None] | None = None,
        on_resolved: Callable[[Mapping[str, Any], str], None] | None = None,
        release_preflight: Callable[[str, str | None], bool] | None = None,
    ) -> dict[str, Any]:
        """Acquire an identity selected through another backend, exactly once."""

        if media_type != "ebooks" or not isinstance(resolved_identity, Mapping):
            raise ValueError("LazyLibrarian authoritative continuation requires an ebook")
        work_id = str(resolved_identity.get("work_id") or "")
        same_provider_identity = work_id.startswith("lazylibrarian:")
        persisted = (
            self._validated_snapshot(resolved_identity)
            if same_provider_identity
            else None
        )
        if same_provider_identity and persisted is None:
            return self._selection_refresh_result()
        title = sanitize_display_text(resolved_identity.get("title"), limit=160)
        raw_author = resolved_identity.get("author")
        author = (
            sanitize_display_text(raw_author, limit=160)
            if raw_author not in (None, "")
            else None
        )
        year = resolved_identity.get("year")
        if title is None or (raw_author not in (None, "") and author is None):
            return self._selection_refresh_result()
        exact: list[Mapping[str, Any]] = []
        for candidate in self.search(title, author):
            snapshot = self._candidate_snapshot(candidate)
            if same_provider_identity:
                if snapshot is not None and hmac.compare_digest(
                    snapshot["fingerprint"], persisted["fingerprint"]
                ):
                    exact.append(candidate)
                continue
            if snapshot is not None and _cross_provider_work_matches(
                resolved_identity, snapshot
            ):
                exact.append(candidate)
        if len(exact) != 1:
            return result(
                "needs_selection",
                "No acquisition backend could prove one exact match for the selected work.",
                service="lazylibrarian",
                backend_outcome="miss" if not exact else "ambiguous",
                resolved_identity=resolved_identity,
            )
        return self._acquire_candidate(
            exact[0],
            request_id=request_id,
            before_create=before_create,
            on_resolved=on_resolved,
            release_preflight=release_preflight,
        )

    def recover_submission(
        self, book_id: object, *, request_id: int | None = None
    ) -> dict[str, Any]:
        """Read exact LL state without mutating search or torrent ownership.

        The caller attaches Huey's qBittorrent tag only after SQLite accepts
        this hash as the request's unique durable owner.
        """

        normalized_id = self._safe_book_id(book_id)
        if normalized_id is None:
            raise ServiceError("LazyLibrarian request has an invalid book ID.")
        book = self._exact_book(normalized_id)
        if book is None:
            return {"state": "unknown", "book_id": normalized_id}
        download_id = self._exact_download_id(normalized_id)
        handoff_category = (
            self._validate_qbittorrent_handoff(download_id)
            if download_id is not None
            else None
        )
        return {
            "state": "queued" if download_id is not None else "pending",
            "book_id": normalized_id,
            "external_id": download_id,
            "external_title": book["title"],
            "external_status": (
                "processing" if handoff_category == "ebooks-imported" else "queued"
            ),
        }


class AbbaClient(JsonClient):
    """Strict AudioBookBay search/grab client with Huey-owned selection."""

    MAX_PROPOSAL_CANDIDATES = 3
    _CANDIDATE_ID = re.compile(r"\Aabba:[0-9a-f]{64}\Z")
    _INFO_HASH = re.compile(r"\A[0-9a-fA-F]{40}\Z")
    _FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")
    _CANDIDATE_FIELDS = frozenset(
        {
            "id",
            "title",
            "author",
            "narrator",
            "year",
            "format",
            "edition",
            "size_bytes",
        }
    )
    _JOB_FIELDS = frozenset(
        {
            "correlation_id",
            "candidate_id",
            "status",
            "info_hash",
            "title",
            "category",
            "save_path",
            "tags",
            "error",
            "canonical_correlation_id",
            "canonical_candidate_id",
        }
    )
    _STATUS_ALIASES = {
        "queued": "queued",
        "downloading": "downloading",
        "downloaded": "downloaded",
        "processing": "processing",
        "failed": "failed",
        "duplicate": "duplicate",
    }

    def __init__(
        self,
        base_url: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: float = 30,
        search_limit: int = 10,
        minimum_confidence: float = 0.82,
        runner_up_gap: float = 0.08,
    ):
        if (
            isinstance(search_limit, bool)
            or not isinstance(search_limit, int)
            or not 1 <= search_limit <= 20
        ):
            raise ValueError("ABBA search_limit must be between 1 and 20")
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("ABBA minimum_confidence must be between 0 and 1")
        if not 0 <= runner_up_gap <= 1:
            raise ValueError("ABBA runner_up_gap must be between 0 and 1")
        super().__init__(
            "ABBA",
            base_url,
            session=session,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self.search_limit = int(search_limit)
        self.minimum_confidence = minimum_confidence
        self.runner_up_gap = runner_up_gap

    @staticmethod
    def _year(value: object) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ServiceError("ABBA returned an invalid search result.")
        year = value
        if not 0 <= year <= 9999:
            raise ServiceError("ABBA returned an invalid search result.")
        return year

    @staticmethod
    def _size_label(value: object) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ServiceError("ABBA returned an invalid search result.")
        size = value
        if size <= 0 or size > 1024**5:
            raise ServiceError("ABBA returned an invalid search result.")
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        amount = float(size)
        unit = units[0]
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                break
            amount /= 1024
        return f"{amount:.0f} {unit}" if unit in {"B", "KiB"} else f"{amount:.1f} {unit}"

    @classmethod
    def _search_candidate(cls, value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) - cls._CANDIDATE_FIELDS:
            raise ServiceError("ABBA returned an invalid search result.")
        raw_candidate_id = value.get("id")
        raw_title = value.get("title")
        if not isinstance(raw_candidate_id, str) or not isinstance(raw_title, str):
            raise ServiceError("ABBA returned an invalid search result.")
        candidate_id = raw_candidate_id
        if not cls._CANDIDATE_ID.fullmatch(candidate_id):
            raise ServiceError("ABBA returned an invalid search result.")
        title = sanitize_display_text(raw_title, limit=160)
        if title is None:
            raise ServiceError("ABBA returned an invalid search result.")

        optional_text: dict[str, str | None] = {}
        for field, limit in (
            ("author", 160),
            ("narrator", 160),
            ("format", 80),
            ("edition", 120),
        ):
            raw = value.get(field)
            if raw not in (None, "") and not isinstance(raw, str):
                raise ServiceError("ABBA returned an invalid search result.")
            normalized = (
                sanitize_display_text(raw, limit=limit) if raw not in (None, "") else None
            )
            if raw not in (None, "") and normalized is None:
                raise ServiceError("ABBA returned an invalid search result.")
            optional_text[field] = normalized

        raw_size = value.get("size_bytes")
        size_label = cls._size_label(raw_size)
        size_bytes = None if raw_size in (None, "") else raw_size
        return {
            "id": candidate_id,
            "title": title,
            **optional_text,
            "year": cls._year(value.get("year")),
            "size_bytes": size_bytes,
            "size_label": size_label,
            "content_kind": "book",
            "available_book_types": ("audiobook",),
            "work_id": candidate_id,
        }

    @classmethod
    def _candidate_snapshot(cls, candidate: Mapping[str, Any]) -> dict[str, Any]:
        candidate_id = str(candidate["id"])
        details: list[str] = []
        if candidate.get("author"):
            details.append(f"by {candidate['author']}")
        if candidate.get("narrator"):
            details.append(f"narrated by {candidate['narrator']}")
        if candidate.get("year") is not None:
            details.append(str(candidate["year"]))
        for field in ("format", "edition", "size_label"):
            if candidate.get(field):
                details.append(str(candidate[field]))
        label = sanitize_display_text(
            " · ".join((str(candidate["title"]), *details)), limit=300
        )
        if label is None:
            raise ServiceError("ABBA returned an unsafe search result.")
        fingerprint_payload = {
            "version": 1,
            "candidate_id": candidate_id,
            "title": normalize_identity_text(candidate["title"]),
            "author": normalize_identity_text(candidate.get("author")),
            "narrator": normalize_identity_text(candidate.get("narrator")),
            "year": candidate.get("year"),
            "format": normalize_identity_text(candidate.get("format")),
            "edition": normalize_identity_text(candidate.get("edition")),
            "size_bytes": candidate.get("size_bytes"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "fingerprint": fingerprint,
            "label": label,
            "work_id": candidate_id,
            "source_work_ids": (candidate_id,),
            "title": str(candidate["title"]),
            "author": candidate.get("author"),
            "year": candidate.get("year"),
            "content_kind": "book",
            "media_type": "audiobooks",
            "book_type": "audiobook",
        }

    @classmethod
    def _persisted_snapshot(cls, value: object) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        candidate_id = str(value.get("work_id") or "")
        source_ids = value.get("source_work_ids")
        fingerprint = str(value.get("fingerprint") or "")
        if (
            not cls._CANDIDATE_ID.fullmatch(candidate_id)
            or not cls._FINGERPRINT.fullmatch(fingerprint)
            or not isinstance(source_ids, (list, tuple))
            or tuple(str(item) for item in source_ids) != (candidate_id,)
            or value.get("content_kind") != "book"
            or value.get("media_type") != "audiobooks"
            or value.get("book_type") != "audiobook"
            or sanitize_display_text(value.get("label"), limit=300) != value.get("label")
        ):
            return None
        return value

    def search(self, title: str, author: str | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"title": title, "limit": self.search_limit}
        if author:
            payload["author"] = author
        value = self._request("POST", "/api/search", json=payload)
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("results"), list)
            or set(value) != {"results", "cached"}
            or not isinstance(value["cached"], bool)
        ):
            raise ServiceError("ABBA returned an invalid search response.")
        if len(value["results"]) > self.search_limit:
            raise ServiceError("ABBA returned too many search results.")
        candidates = [self._search_candidate(item) for item in value["results"]]
        if len({item["id"] for item in candidates}) != len(candidates):
            raise ServiceError("ABBA returned duplicate search identities.")
        return candidates

    @staticmethod
    def _release_title_variants(value: object, author: str | None) -> tuple[str, ...]:
        """Extract only common release-title boundaries for ranking.

        AudioBookBay titles commonly look like ``Author - Title [format]``.
        These variants are evaluation-only: Huey keeps the original sanitized
        title for display and never fabricates a structured author.
        """

        raw = str(value or "").strip()
        if not raw:
            return ()
        variants = {raw}
        pending = [raw]
        while pending:
            current = pending.pop()
            bracketless = re.sub(
                r"\s*(?:\[[^\]]{1,120}\]|\([^)]{1,120}\))\s*$",
                "",
                current,
            ).strip()
            if bracketless and bracketless not in variants:
                variants.add(bracketless)
                pending.append(bracketless)
            for segment in re.split(r"\s+(?:-|–|—|\|)\s+", current):
                segment = segment.strip()
                if segment and segment not in variants:
                    variants.add(segment)
                    pending.append(segment)
            by_parts = re.split(r"\s+by\s+", current, maxsplit=1, flags=re.IGNORECASE)
            if len(by_parts) == 2 and by_parts[0].strip() not in variants:
                variants.add(by_parts[0].strip())
                pending.append(by_parts[0].strip())

        wanted_author_tokens = set(normalize_text(author).split())
        if wanted_author_tokens:
            candidate_tokens = normalize_text(raw).split()
            without_author = [
                token for token in candidate_tokens if token not in wanted_author_tokens
            ]
            if without_author:
                variants.add(" ".join(without_author))
        return tuple(sorted(variants))

    @classmethod
    def _release_title_score(
        cls, title: str, candidate: Mapping[str, Any], author: str | None
    ) -> float:
        variants = cls._release_title_variants(candidate.get("title"), author)
        return max((title_similarity(title, value) for value in variants), default=0.0)

    @staticmethod
    def _author_is_evidenced(
        author: str, candidate: Mapping[str, Any]
    ) -> bool:
        wanted = set(normalize_text(author).split())
        if not wanted:
            return False
        structured = set(normalize_text(candidate.get("author")).split())
        raw_title = set(normalize_text(candidate.get("title")).split())
        return wanted.issubset(structured) or wanted.issubset(raw_title)

    def _selection(
        self,
        title: str,
        author: str | None,
        candidates: list[Mapping[str, Any]],
    ) -> Selection:
        """Rank sanitized ABBA releases without requiring structured authors."""

        ranked: list[RankedCandidate] = []
        for candidate in candidates:
            title_score = self._release_title_score(title, candidate, author)
            if author:
                author_score = (
                    1.0 if self._author_is_evidenced(author, candidate) else 0.0
                )
                score = (0.78 * title_score) + (0.22 * author_score)
            else:
                score = title_score
            ranked.append(
                RankedCandidate(
                    item=candidate,
                    score=max(0.0, min(1.0, score)),
                    seeders=0,
                    stable_key=str(candidate.get("id") or ""),
                )
            )
        ranked.sort(
            key=lambda item: (
                -item.score,
                normalize_text(item.item.get("title")),
                item.stable_key,
            )
        )
        if not ranked:
            return Selection(None, "no_results", ())
        if ranked[0].score < self.minimum_confidence:
            return Selection(None, "low_confidence", tuple(ranked))
        if (
            len(ranked) > 1
            and ranked[0].score - ranked[1].score < self.runner_up_gap
        ):
            return Selection(None, "ambiguous", tuple(ranked))
        return Selection(ranked[0].item, "selected", tuple(ranked))

    def _selection_proposal(self, selection: Any) -> tuple[dict[str, Any], ...]:
        if selection.reason != "ambiguous" or not selection.ranked:
            return ()
        top_score = selection.ranked[0].score
        values: list[dict[str, Any]] = []
        labels: set[str] = set()
        for ranked in selection.ranked:
            if ranked.score < self.minimum_confidence:
                continue
            if top_score - ranked.score >= self.runner_up_gap:
                continue
            snapshot = self._candidate_snapshot(ranked.item)
            if snapshot["label"] in labels:
                return ()
            labels.add(str(snapshot["label"]))
            values.append(snapshot)
            if len(values) == self.MAX_PROPOSAL_CANDIDATES:
                break
        return tuple(values) if len(values) >= 2 else ()

    @classmethod
    def _job_payload(
        cls,
        value: object,
        *,
        expected_correlation: str,
        expected_candidate_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ServiceError("ABBA returned an invalid job response.")
        wrapper_keys = set(value)
        if wrapper_keys == {"job"}:
            raw_job = value["job"]
        elif wrapper_keys == {"found", "job"} and value.get("found") is True:
            raw_job = value["job"]
        else:
            raise ServiceError("ABBA returned an invalid job response.")
        if not isinstance(raw_job, Mapping) or set(raw_job) - cls._JOB_FIELDS:
            raise ServiceError("ABBA returned an invalid job response.")
        raw_correlation = raw_job.get("correlation_id")
        raw_candidate_id = raw_job.get("candidate_id")
        raw_status_value = raw_job.get("status")
        raw_title = raw_job.get("title")
        raw_info_hash = raw_job.get("info_hash")
        if (
            not isinstance(raw_correlation, str)
            or not isinstance(raw_candidate_id, str)
            or not isinstance(raw_status_value, str)
            or not isinstance(raw_title, str)
            or (
                raw_info_hash not in (None, "")
                and not isinstance(raw_info_hash, str)
            )
        ):
            raise ServiceError("ABBA returned an invalid job response.")
        correlation = raw_correlation
        candidate_id = raw_candidate_id
        raw_status = raw_status_value.casefold()
        info_hash = str(raw_info_hash or "").lower()
        title = sanitize_display_text(raw_title, limit=160)
        tags = raw_job.get("tags")
        request_id = _correlation_request_id(expected_correlation)
        if request_id is None:  # pragma: no cover - internal invariant
            raise ValueError("Invalid Huey correlation")
        expected_tag = f"huey-{request_id}"
        duplicate = raw_status == "duplicate"
        raw_canonical_correlation = raw_job.get("canonical_correlation_id")
        raw_canonical_candidate = raw_job.get("canonical_candidate_id")
        canonical_request_id: int | None = None
        canonical_tag: str | None = None
        if duplicate:
            canonical_request_id = _correlation_request_id(
                raw_canonical_correlation
            )
            if (
                canonical_request_id is None
                or raw_canonical_correlation == expected_correlation
                or not isinstance(raw_canonical_candidate, str)
                or not cls._CANDIDATE_ID.fullmatch(raw_canonical_candidate)
            ):
                raise ServiceError("ABBA returned an invalid job response.")
            canonical_tag = f"huey-{canonical_request_id}"
        elif raw_canonical_correlation is not None or raw_canonical_candidate is not None:
            raise ServiceError("ABBA returned an invalid job response.")
        if (
            correlation != expected_correlation
            or not cls._CANDIDATE_ID.fullmatch(candidate_id)
            or (expected_candidate_id is not None and candidate_id != expected_candidate_id)
            or raw_status not in cls._STATUS_ALIASES
            or title is None
            or raw_job.get("category") != "audiobooks"
            or raw_job.get("save_path") != "/downloads/audiobooks"
            or not isinstance(tags, list)
            or tuple(tags) != ((canonical_tag,) if duplicate else (expected_tag,))
        ):
            raise ServiceError("ABBA returned an invalid job response.")
        raw_error = raw_job.get("error")
        if raw_error not in (None, "") and not isinstance(raw_error, str):
            raise ServiceError("ABBA returned an invalid job response.")
        error_detail = (
            sanitize_display_text(raw_error, limit=500)
            if raw_error not in (None, "")
            else None
        )
        status = cls._STATUS_ALIASES[raw_status]
        if (
            (
                status == "failed"
                and raw_info_hash not in (None, "")
                and not cls._INFO_HASH.fullmatch(info_hash)
            )
            or (status != "failed" and not cls._INFO_HASH.fullmatch(info_hash))
            or (raw_error not in (None, "") and error_detail is None)
            or (status == "failed" and error_detail is None)
            or (status != "failed" and raw_error not in (None, ""))
        ):
            raise ServiceError("ABBA returned an invalid job response.")
        return {
            "correlation_id": correlation,
            "candidate_id": candidate_id,
            "status": status,
            "info_hash": info_hash or None,
            "title": title,
            "category": "audiobooks",
            "save_path": "/downloads/audiobooks",
            "tags": ((canonical_tag,) if duplicate else (expected_tag,)),
            "error": error_detail,
            "canonical_request_id": canonical_request_id,
            "canonical_candidate_id": (
                str(raw_canonical_candidate) if duplicate else None
            ),
        }

    def recover_request(self, request_id: int) -> dict[str, Any] | None:
        normalized_request_id = int(request_id)
        if not 1 <= normalized_request_id <= SQLITE_MAX_REQUEST_ID:
            raise ValueError("ABBA recovery requires a SQLite-safe Huey request ID")
        correlation = f"huey:{normalized_request_id}"
        value = self._request(
            "GET", "/api/status", params={"correlation_id": correlation}
        )
        if not isinstance(value, Mapping):
            raise ServiceError("ABBA returned an invalid status response.")
        if value.get("found") is False:
            if set(value) - {"found", "correlation_id"} or value.get(
                "correlation_id", correlation
            ) != correlation:
                raise ServiceError("ABBA returned an invalid status response.")
            return None
        if value.get("found") is not True or set(value) - {"found", "job"}:
            raise ServiceError("ABBA returned an invalid status response.")
        return self._job_payload(value, expected_correlation=correlation)

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        return self.recover_request(request_id)

    @classmethod
    def recovered_request_matches_candidate(
        cls, remote: object, selected_candidate: object
    ) -> bool:
        selected = cls._persisted_snapshot(selected_candidate)
        if selected is None or not isinstance(remote, Mapping):
            return False
        return hmac.compare_digest(
            str(remote.get("candidate_id") or ""), str(selected.get("work_id") or "")
        )

    @staticmethod
    def _submission_result(job: Mapping[str, Any]) -> dict[str, Any]:
        title = safe_display_title(job.get("title"))
        if job["status"] == "duplicate":
            owner_request_id = job.get("canonical_request_id")
            canonical_candidate_id = str(
                job.get("canonical_candidate_id") or ""
            )
            info_hash = str(job.get("info_hash") or "")
            if not isinstance(owner_request_id, int):  # pragma: no cover - parser invariant
                raise ServiceError("ABBA returned an invalid canonical acquisition.")
            raise CanonicalAcquisition(
                owner_request_id,
                candidate_id=str(job["candidate_id"]),
                canonical_candidate_id=canonical_candidate_id,
                info_hash=info_hash,
                title=title,
            )
        if job["status"] == "failed":
            return result(
                "failed",
                f"ABBA could not queue {title}: {job['error']}",
                service="abba",
                external_id=job["info_hash"],
                external_title=title,
                external_status="failed",
            )
        return result(
            "queued",
            f"ABBA queued {title} in qBittorrent.",
            service="abba",
            external_id=job["info_hash"],
            external_title=title,
            external_status=str(job["status"]),
        )

    def _grab_candidate(
        self,
        candidate: Mapping[str, Any],
        request_id: int,
        *,
        before_create: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        candidate_id = str(candidate["work_id"])
        correlation = f"huey:{int(request_id)}"
        try:
            recovered = self.recover_request(int(request_id))
        except ServiceError:
            recovered = None
        if recovered is not None:
            if before_create is not None:
                before_create(candidate_id)
            if recovered["candidate_id"] != candidate_id:
                raise SubmissionUncertain(
                    "ABBA correlation exists for a different candidate."
                )
            return self._submission_result(recovered)

        if before_create is not None:
            before_create(candidate_id)
        try:
            value = self._request(
                "POST",
                "/api/grab",
                json={"candidate_id": candidate_id, "correlation_id": correlation},
            )
            job = self._job_payload(
                value,
                expected_correlation=correlation,
                expected_candidate_id=candidate_id,
            )
            return self._submission_result(job)
        except ServiceError as submission_error:
            try:
                recovered = self.recover_request(int(request_id))
            except ServiceError as recovery_error:
                raise SubmissionUncertain(
                    "ABBA submission outcome is awaiting correlation recovery."
                ) from recovery_error
            if recovered is not None:
                if recovered["candidate_id"] != candidate_id:
                    raise SubmissionUncertain(
                        "ABBA correlation exists for a different candidate."
                    ) from submission_error
                return self._submission_result(recovered)
            if isinstance(submission_error, ServiceRejected):
                raise submission_error
            raise SubmissionUncertain(
                "ABBA submission outcome is awaiting correlation recovery."
            ) from submission_error

    def resume_grab(self, request_id: int, candidate_id: str) -> dict[str, Any]:
        """Idempotently resume only the exact candidate persisted by Huey."""

        normalized_candidate_id = str(candidate_id or "")
        if not self._CANDIDATE_ID.fullmatch(normalized_candidate_id):
            raise ValueError("ABBA recovery requires a valid persisted candidate ID")
        if (
            isinstance(request_id, bool)
            or not 1 <= int(request_id) <= SQLITE_MAX_REQUEST_ID
        ):
            raise ValueError("ABBA recovery requires a positive Huey request ID")
        return self._grab_candidate(
            {"work_id": normalized_candidate_id}, int(request_id)
        )

    def submit(
        self,
        media_type: str,
        title: str,
        author: str | None,
        request_id: int,
        *,
        before_create: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if media_type != "audiobooks":
            raise ValueError("ABBA supports audiobook requests only")
        candidates = self.search(title, author)
        selection = self._selection(title, author, candidates)
        if selection.selected is None:
            proposal = self._selection_proposal(selection)
            if proposal:
                return result(
                    "awaiting_selection",
                    "ABBA found multiple close audiobook matches. Choose one before acquisition starts.",
                    service="abba",
                    selection_proposal=proposal,
                )
            if selection.reason == "no_results":
                message = "ABBA found no matching audiobook. Check the title and author."
            elif selection.reason == "ambiguous":
                message = (
                    "ABBA found close audiobook matches that could not be distinguished "
                    "safely. Add an author, narrator, year, format, or edition."
                )
            else:
                message = (
                    "ABBA found no audiobook with enough confidence. "
                    "Add an author, narrator, year, format, or edition."
                )
            return result("needs_selection", message, service="abba")
        snapshot = self._candidate_snapshot(selection.selected)
        return self._grab_candidate(
            snapshot, int(request_id), before_create=before_create
        )

    def submit_selected(
        self,
        media_type: str,
        title: str,
        author: str | None,
        request_id: int,
        *,
        selected_candidate: Mapping[str, Any],
        before_create: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if media_type != "audiobooks":
            raise ValueError("ABBA supports audiobook requests only")
        persisted = self._persisted_snapshot(selected_candidate)
        if persisted is None:
            return result(
                "needs_selection",
                "That audiobook choice could not be verified. Search again.",
                service="abba",
            )
        matches = []
        for candidate in self.search(title, author):
            snapshot = self._candidate_snapshot(candidate)
            if hmac.compare_digest(
                str(snapshot["fingerprint"]), str(persisted["fingerprint"])
            ):
                matches.append(snapshot)
        if len(matches) != 1:
            return result(
                "needs_selection",
                "That audiobook result changed or disappeared. Submit the title again.",
                service="abba",
            )
        return self._grab_candidate(
            matches[0], int(request_id), before_create=before_create
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
