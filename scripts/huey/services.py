"""Lazy environment-backed registry for Huey's acquisition clients."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

try:
    from .acquisition import DirectAcquirer, UnsupportedTorrentVersion, magnet_info_hash
    from .clients import (
        AbbaClient,
        LazyLibrarianClient,
        LidarrClient,
        ProwlarrClient,
        QBittorrentClient,
        RadarrClient,
        ServiceError,
        ShelfarrClient,
        SonarrClient,
    )
    from .matching import normalize_text, score_release
    from .results import sanitize_display_text
except ImportError:  # pragma: no cover - direct container entrypoint
    from acquisition import DirectAcquirer, UnsupportedTorrentVersion, magnet_info_hash
    from clients import (
        AbbaClient,
        LazyLibrarianClient,
        LidarrClient,
        ProwlarrClient,
        QBittorrentClient,
        RadarrClient,
        ServiceError,
        ShelfarrClient,
        SonarrClient,
    )
    from matching import normalize_text, score_release
    from results import sanitize_display_text


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
        self.abba_enabled = _boolean(
            self.environment.get("ABBA_ENABLED", "false"),
            "ABBA_ENABLED",
        )
        self.lazylibrarian_enabled = _boolean(
            self.environment.get("LAZYLIBRARIAN_ENABLED", "false"),
            "LAZYLIBRARIAN_ENABLED",
        )
        raw_owner = self.environment.get("EBOOK_ACQUISITION_OWNER", "").strip()
        raw_backends = self.environment.get(
            "EBOOK_ACQUISITION_BACKENDS", ""
        ).strip()
        self._ebook_owner_explicit = bool(raw_owner)
        self._legacy_direct_ebook_owner = False
        if raw_backends:
            tokens = tuple(part.strip() for part in raw_backends.split(","))
            if any(not token for token in tokens):
                raise ValueError(
                    "EBOOK_ACQUISITION_BACKENDS cannot contain empty entries"
                )
            if any(
                token not in {"lazylibrarian", "shelfarr"} for token in tokens
            ):
                raise ValueError(
                    "EBOOK_ACQUISITION_BACKENDS supports only exact lowercase "
                    "lazylibrarian and shelfarr tokens"
                )
            if len(set(tokens)) != len(tokens):
                raise ValueError(
                    "EBOOK_ACQUISITION_BACKENDS cannot contain duplicates"
                )
            if raw_owner and raw_owner != tokens[0]:
                raise ValueError(
                    "EBOOK_ACQUISITION_OWNER must match the first configured backend"
                )
            self.ebook_acquisition_backends = tokens
            self.ebook_acquisition_owner = tokens[0]
        else:
            owner = raw_owner or "shelfarr"
            if owner == "direct":
                # Compatibility-only rollback.  Direct is intentionally not a
                # legal member of the new production cascade.
                self._legacy_direct_ebook_owner = True
                self.ebook_acquisition_backends = ()
                self.ebook_acquisition_owner = "direct"
            elif owner in {"lazylibrarian", "shelfarr"}:
                self.ebook_acquisition_backends = (owner,)
                self.ebook_acquisition_owner = owner
            else:
                raise ValueError(
                    "EBOOK_ACQUISITION_OWNER must be shelfarr, lazylibrarian, or "
                    "legacy direct when EBOOK_ACQUISITION_BACKENDS is absent"
                )

        if (
            "lazylibrarian" in self.ebook_acquisition_backends
            and self.lazylibrarian_enabled
            and not self._raw_env("LAZYLIBRARIAN_API_KEY").strip()
        ):
            raise ServiceError(
                "LazyLibrarian is enabled but its API key is not configured."
            )
        if (
            "lazylibrarian" in self.ebook_acquisition_backends
            and self.lazylibrarian_enabled
            and not self._raw_env("PROWLARR_API_KEY").strip()
        ):
            raise ServiceError(
                "LazyLibrarian release preflight requires the Prowlarr API key."
            )
        if (
            "shelfarr" in self.ebook_acquisition_backends
            and self.shelfarr_enabled
            and not self._raw_env("SHELFARR_API_TOKEN").strip()
        ):
            raise ServiceError(
                "Shelfarr is enabled but its API token is not configured."
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
        """Compatibility entrypoint for the first configured ebook backend."""

        media_type = str(request["media_type"])
        if media_type == "audiobooks":
            return self.audiobook(request)
        if media_type != "ebooks":
            raise ValueError(f"Unsupported book media type: {media_type}")

        if self._legacy_direct_ebook_owner:
            return self.direct().submit(
                "ebooks",
                str(request["title"]),
                str(request["author"]) if request.get("author") else None,
                int(request["id"]),
            )
        return self.submit_ebook_backend(
            request, self.ebook_acquisition_backends[0]
        )

    def submit_ebook_backend(
        self,
        request: Mapping[str, Any],
        backend: str,
        *,
        resolved_identity: Mapping[str, Any] | None = None,
        selected_candidate: Mapping[str, Any] | None = None,
    ):
        """Run exactly one configured serial backend attempt."""

        if backend not in {"lazylibrarian", "shelfarr"}:
            raise ValueError("Unsupported ebook backend")
        if backend not in self.ebook_acquisition_backends:
            raise ServiceError(
                "This ebook backend was disabled after the request began."
            )
        title = str(request["title"])
        author = str(request["author"]) if request.get("author") else None
        request_id = int(request["id"])
        before_dispatch = request.get("_before_dispatch")
        on_resolved = request.get("_on_resolved")
        if backend == "lazylibrarian":
            if not self.lazylibrarian_enabled:
                raise ServiceError("The primary ebook backend is unavailable.")
            client = self.lazylibrarian()
            if selected_candidate is not None and str(
                selected_candidate.get("work_id") or ""
            ).startswith("lazylibrarian:"):
                return client.submit_selected(
                    "ebooks",
                    title,
                    author,
                    request_id,
                    selected_candidate=selected_candidate,
                    before_create=before_dispatch,
                    on_resolved=on_resolved,
                    release_preflight=self.ebook_release_available,
                )
            if resolved_identity is not None:
                return client.submit_authoritative(
                    "ebooks",
                    request_id,
                    resolved_identity=resolved_identity,
                    before_create=before_dispatch,
                    on_resolved=on_resolved,
                    release_preflight=self.ebook_release_available,
                )
            return client.submit(
                "ebooks",
                title,
                author,
                request_id,
                before_create=before_dispatch,
                on_resolved=on_resolved,
                release_preflight=self.ebook_release_available,
            )

        if not self.shelfarr_enabled:
            raise ServiceError("The fallback ebook backend is unavailable.")
        client = self.shelfarr()
        common: dict[str, Any] = {
            "discord_user_id": request.get("discord_user_id"),
            "discord_channel_id": request.get("channel_id"),
        }
        if before_dispatch is not None:
            common["before_create"] = before_dispatch
        if on_resolved is not None:
            common["on_resolved"] = on_resolved
        if selected_candidate is not None and not str(
            selected_candidate.get("work_id") or ""
        ).startswith("lazylibrarian:"):
            return client.submit_selected(
                "ebooks",
                title,
                author,
                request_id,
                selected_candidate=selected_candidate,
                **common,
            )
        if resolved_identity is not None:
            return client.submit_authoritative(
                "ebooks",
                request_id,
                resolved_identity=resolved_identity,
                **common,
            )
        return client.submit("ebooks", title, author, request_id, **common)

    def ebook_service(self) -> str | None:
        """Return the persisted owner name for a new ebook request."""

        if self._legacy_direct_ebook_owner:
            return None
        return self.ebook_acquisition_backends[0]

    def lazylibrarian(self) -> LazyLibrarianClient:
        """Return LL for new intake or draining already-owned requests."""

        if "lazylibrarian" in self._clients:
            return self._clients["lazylibrarian"]
        client = LazyLibrarianClient(
            self._env("LAZYLIBRARIAN_URL", "http://lazylibrarian:5299"),
            self._raw_env("LAZYLIBRARIAN_API_KEY"),
            qbittorrent=self.qbittorrent(),
            timeout=_positive_float(
                self._env("LAZYLIBRARIAN_TIMEOUT_SECONDS", "30"),
                "LAZYLIBRARIAN_TIMEOUT_SECONDS",
            ),
            search_limit=_bounded_int(
                self._env("LAZYLIBRARIAN_SEARCH_LIMIT", "10"),
                "LAZYLIBRARIAN_SEARCH_LIMIT",
                1,
                20,
            ),
            metadata_source=self._env(
                "LAZYLIBRARIAN_METADATA_SOURCE", "OpenLibrary"
            ),
            minimum_confidence=_bounded_float(
                self._env("HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE", "0.80"),
                "HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE",
                0,
                1,
            ),
            runner_up_gap=_bounded_float(
                self._env("HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP", "0.05"),
                "HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP",
                0,
                1,
            ),
        )
        self._clients["lazylibrarian"] = client
        return client

    def qbittorrent(self) -> QBittorrentClient:
        """Return the shared qBittorrent client used for exact hash checks."""

        if "qbittorrent" in self._clients:
            return self._clients["qbittorrent"]
        username = self._env("QBITTORRENT_USERNAME") or self._env("QBIT_USERNAME")
        password = self._raw_env("QBITTORRENT_PASSWORD") or self._raw_env(
            "QBIT_PASSWORD"
        )
        client = QBittorrentClient(
            self._env("QBITTORRENT_URL", "http://qbittorrent:8080"),
            username,
            password,
        )
        self._clients["qbittorrent"] = client
        return client

    def abba(self) -> AbbaClient:
        """Return ABBA for new intake or draining already-owned requests."""

        if "abba" in self._clients:
            return self._clients["abba"]
        client = AbbaClient(
            self._env("ABBA_URL", "http://abba:5078"),
            timeout=_positive_float(
                self._env("ABBA_TIMEOUT_SECONDS", "30"),
                "ABBA_TIMEOUT_SECONDS",
            ),
            search_limit=_bounded_int(
                self._env("ABBA_SEARCH_LIMIT", "10"),
                "ABBA_SEARCH_LIMIT",
                1,
                20,
            ),
            minimum_confidence=_bounded_float(
                self._env("HUEY_ABBA_MINIMUM_CONFIDENCE", "0.82"),
                "HUEY_ABBA_MINIMUM_CONFIDENCE",
                0,
                1,
            ),
            runner_up_gap=_bounded_float(
                self._env("HUEY_ABBA_RUNNER_UP_GAP", "0.08"),
                "HUEY_ABBA_RUNNER_UP_GAP",
                0,
                1,
            ),
        )
        self._clients["abba"] = client
        return client

    def audiobook(self, request: Mapping[str, Any]):
        """Route audiobook intake to ABBA or the direct rollback path."""

        if not self.abba_enabled:
            return self.direct().submit(
                "audiobooks",
                str(request["title"]),
                str(request["author"]) if request.get("author") else None,
                int(request["id"]),
            )
        return self.abba().submit(
            "audiobooks",
            str(request["title"]),
            str(request["author"]) if request.get("author") else None,
            int(request["id"]),
            before_create=request.get("_before_dispatch"),
        )

    def book_selected(
        self,
        request: Mapping[str, Any],
        selected_candidate: Mapping[str, Any],
        *,
        before_create: Callable[..., None] | None = None,
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

    def selection_selected(
        self,
        request: Mapping[str, Any],
        selected_candidate: Mapping[str, Any],
        *,
        before_create: Callable[..., None] | None = None,
    ):
        """Continue a persisted choice through its original owning service."""

        service = str(request.get("service") or "")
        if service == "abba":
            if not self.abba_enabled:
                raise RuntimeError(
                    "ABBA ownership was disabled before candidate confirmation."
                )
            return self.abba().submit_selected(
                str(request["media_type"]),
                str(request["title"]),
                str(request["author"]) if request.get("author") else None,
                int(request["id"]),
                selected_candidate=selected_candidate,
                before_create=before_create,
            )
        if service == "lazylibrarian":
            if (
                self.ebook_acquisition_owner != "lazylibrarian"
                or not self.lazylibrarian_enabled
            ):
                raise RuntimeError(
                    "LazyLibrarian ownership was disabled before candidate confirmation."
                )
            return self.lazylibrarian().submit_selected(
                str(request["media_type"]),
                str(request["title"]),
                str(request["author"]) if request.get("author") else None,
                int(request["id"]),
                selected_candidate=selected_candidate,
                before_create=before_create,
            )
        if service == "shelfarr":
            if (
                str(request.get("media_type") or "") == "ebooks"
                and self.ebook_acquisition_owner != "shelfarr"
            ):
                raise RuntimeError(
                    "Shelfarr ownership was disabled before candidate confirmation."
                )
            return self.book_selected(
                request,
                selected_candidate,
                before_create=before_create,
            )
        raise RuntimeError("Candidate confirmation has no supported acquisition owner.")

    def direct(self) -> DirectAcquirer:
        if "direct" in self._clients:
            return self._clients["direct"]
        prowlarr = self.prowlarr()
        direct = DirectAcquirer(
            prowlarr,
            self.qbittorrent(),
            minimum_confidence=float(self._env("HUEY_MINIMUM_CONFIDENCE", "0.70")),
            runner_up_gap=float(self._env("HUEY_RUNNER_UP_GAP", "0.08")),
            category_prefix=self._env("HUEY_QBIT_CATEGORY_PREFIX", ""),
        )
        self._clients["direct"] = direct
        return direct

    def prowlarr(self) -> ProwlarrClient:
        """Return the shared read-only search client."""

        if "prowlarr" in self._clients:
            return self._clients["prowlarr"]
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
        self._clients["prowlarr"] = prowlarr
        return prowlarr

    def ebook_release_available(self, title: str, author: str | None) -> bool:
        """Read-only exact-category gate before LL may mutate its wanted list."""

        short_title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()
        queries: list[str] = []
        for candidate_query in (
            f"{title} {author}" if author else title,
            title,
            f"{short_title} {author}" if author and short_title != title else "",
            short_title if short_title != title else "",
        ):
            normalized_query = " ".join(str(candidate_query or "").split())
            if normalized_query and normalized_query not in queries:
                queries.append(normalized_query)

        wrong_lane = re.compile(
            r"\b(?:m4a|m4b|mp3|aac|flac|ogg|opus|wav|cue|"
            r"audio ?books?|audible|cbz|cbr|comics?|mangas?|manhwas?|"
            r"manhuas?|webtoons?|magazines?|graphic novels?)\b",
            re.IGNORECASE,
        )
        supported_format = re.compile(
            r"(?<![a-z0-9])(?:epub|mobi|azw3|pdf)(?![a-z0-9])",
            re.IGNORECASE,
        )
        unsupported_format = re.compile(
            r"(?<![a-z0-9])(?:azw|azw4|kfx|prc|tpz|acsm|txt|docx?|rtf|"
            r"djvu|lit|fb2|html?)(?![a-z0-9])",
            re.IGNORECASE,
        )
        saw_malformed = False
        seen: set[tuple[str, str, str]] = set()

        def usable_download_reference(magnet: str, download: str) -> bool:
            if any(ord(character) < 32 for character in magnet + download):
                return False
            if magnet:
                try:
                    if urlsplit(magnet).scheme.casefold() != "magnet":
                        raise ValueError("Not a magnet URI")
                    info_hash = magnet_info_hash(magnet)
                    if info_hash is not None:
                        # ``normalize_info_hash`` also accepts a 64-hex value
                        # for other acquisition paths.  A valid v1 ``btih``
                        # topic is exactly 40 hex; accepting a 64-hex topic
                        # here would violate the shared v1/hybrid contract.
                        return len(info_hash) == 40
                except UnsupportedTorrentVersion:
                    # A pure-v2 or hybrid source remains unsupported even if
                    # the row also advertises a download URL.
                    return False
                except ValueError:
                    pass
            try:
                parsed = urlsplit(download)
                parsed.port
            except ValueError:
                return False
            if (
                not parsed.scheme
                and not parsed.netloc
                and bool(parsed.path)
                and not download.startswith("//")
                and "\\" not in download
                and ".." not in parsed.path.split("/")
            ):
                return True
            return bool(
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
            )
        for query in queries:
            try:
                candidates = self.prowlarr().search(query, (7020,))
            except ServiceError:
                raise
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    saw_malformed = True
                    continue
                candidate_title = sanitize_display_text(
                    candidate.get("title"), limit=300
                )
                canonical_protocol = candidate.get("protocol")
                legacy_protocol = candidate.get("downloadProtocol")
                if (
                    canonical_protocol not in (None, "")
                    and legacy_protocol not in (None, "")
                    and str(canonical_protocol).casefold()
                    != str(legacy_protocol).casefold()
                ):
                    saw_malformed = True
                    continue
                raw_protocol = (
                    canonical_protocol
                    if canonical_protocol not in (None, "")
                    else legacy_protocol
                )
                protocol = (
                    raw_protocol.strip().casefold()
                    if isinstance(raw_protocol, str) and raw_protocol.strip()
                    else None
                )
                magnet = str(candidate.get("magnetUrl") or "").strip()
                download = str(candidate.get("downloadUrl") or "").strip()
                if candidate_title is None or protocol is None:
                    saw_malformed = True
                    continue
                if not usable_download_reference(magnet, download):
                    saw_malformed = True
                    continue
                signature = (candidate_title.casefold(), magnet, download)
                if signature in seen:
                    continue
                seen.add(signature)
                if protocol != "torrent" or wrong_lane.search(
                    normalize_text(candidate_title)
                ):
                    continue
                if unsupported_format.search(candidate_title) and not supported_format.search(
                    candidate_title
                ):
                    continue
                if author:
                    author_tokens = set(normalize_text(author).split())
                    candidate_tokens = set(normalize_text(candidate_title).split())
                    if author_tokens and not author_tokens.issubset(candidate_tokens):
                        continue
                if score_release(
                    title, author, "ebooks", candidate
                ).score >= 0.70:
                    return True
        if saw_malformed:
            raise ServiceError(
                "The ebook release preflight could not prove provider availability."
            )
        return False
