#!/usr/bin/env python3
"""Secret-safe, read-only post-check for a completed Prowlarr key rotation.

The checker intentionally emits only fixed check names and boolean results.
Credentials are sent in headers or form bodies, never URLs, and neither
credential values nor exception text are rendered.  No configuration endpoint
or other state-changing API is used.

An optional identity snapshot is a private (mode 0600) JSON file with this
shape::

    {
      "prowlarr": {"id": "<64 lowercase hex>", "started_at": "<Docker time>"},
      "qbittorrent": {"id": "<64 lowercase hex>", "started_at": "<Docker time>"}
    }

Supplying the snapshot proves those two containers were not recreated.  The
snapshot values are compared in memory and are never displayed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

try:
    from .bootstrap import ApiClient, QbittorrentClient
    from .rotate_prowlarr_key import (
        API_KEY_RE,
        MAX_CONFIG_BYTES,
        MAX_ENV_BYTES,
        _config_key,
        _environment,
        _private_path,
        _read_private_bounded,
        _safe_root,
    )
except ImportError:
    from bootstrap import ApiClient, QbittorrentClient
    from rotate_prowlarr_key import (
        API_KEY_RE,
        MAX_CONFIG_BYTES,
        MAX_ENV_BYTES,
        _config_key,
        _environment,
        _private_path,
        _read_private_bounded,
        _safe_root,
    )


STACK_ROOT = Path(__file__).resolve().parents[1]
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_IDENTITY_BYTES = 16 * 1024
AUTH_REJECTION_STATUSES = frozenset({401, 403})
ACTIVE_COMMAND_STATUSES = frozenset({"queued", "started"})
KNOWN_TERMINAL_COMMAND_STATUSES = frozenset(
    {"completed", "failed", "aborted", "cancelled"}
)
EBOOK_LANES = ("ebooks", "ebooks-imported", "shelfarr")
EBOOK_LANE_PATHS = {
    "ebooks": "/downloads/ebooks",
    "ebooks-imported": "/downloads/ebooks",
    "shelfarr": "/downloads/shelfarr",
}
IDENTITY_SERVICES = ("prowlarr", "qbittorrent")
IDENTIFIER_RE = re.compile(r"[0-9a-f]{64}")
STARTED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z"
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool


@dataclass(frozen=True)
class ArrSpec:
    name: str
    environment_prefix: str
    default_port: str
    api_version: str
    expected_indexers: int
    database_relative: str


@dataclass(frozen=True)
class ContainerIdentity:
    identifier: str
    started_at: str


ARR_SPECS = (
    ArrSpec("sonarr", "SONARR", "8989", "v3", 3, "config/sonarr/sonarr.db"),
    ArrSpec("radarr", "RADARR", "7878", "v3", 2, "config/radarr/radarr.db"),
    ArrSpec("lidarr", "LIDARR", "8686", "v1", 3, "config/lidarr/lidarr.db"),
    ArrSpec(
        "whisparr",
        "WHISPARR",
        "6969",
        "v3",
        4,
        "config/whisparr/whisparr2.db",
    ),
)
ARR_CHECK_NAMES = tuple(
    f"consumers:{spec.name}:{kind}"
    for spec in ARR_SPECS
    for kind in ("credential", "live")
)

BASE_CHECK_NAMES = (
    "credentials:key-shape",
    "credentials:key-changed",
    "credentials:local-convergence",
    "prowlarr:new-auth",
    "prowlarr:old-auth-rejected",
    "prowlarr:reset-terminal-or-pruned",
    "prowlarr:reset-active-zero",
    *ARR_CHECK_NAMES,
    "consumers:lazylibrarian",
    "consumers:shelfarr",
    "consumers:huey",
    "qbittorrent:ebook-lanes-empty",
)
IDENTITY_CHECK_NAMES = tuple(
    f"container:{service}-identity" for service in IDENTITY_SERVICES
)


class EvidenceBackend(Protocol):
    """The bounded observations used by the pure acceptance checks."""

    def prowlarr_auth_matches(self, api_key: str) -> bool: ...

    def prowlarr_auth_rejected(self, api_key: str) -> bool: ...

    def prowlarr_commands(self, api_key: str) -> object: ...

    def arr_indexers(self, spec: ArrSpec) -> object: ...

    def arr_persisted_keys_match(self, spec: ArrSpec, api_key: str) -> bool: ...

    def arr_indexers_live(self, spec: ArrSpec, indexers: object) -> bool: ...

    def lazylibrarian_providers(self) -> object: ...

    def shelfarr_key_matches(self, api_key: str) -> bool: ...

    def huey_key_matches(self, api_key: str) -> bool: ...

    def ebook_lanes_empty(self) -> bool: ...

    def container_identity_matches(
        self, service: str, expected: ContainerIdentity
    ) -> bool: ...


def _fixed_base_url(environment: Mapping[str, str], prefix: str, default: str) -> str:
    host = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86").strip()
    if not host or re.fullmatch(r"[A-Za-z0-9_.-]+", host) is None:
        raise ValueError("invalid host")
    raw_port = environment.get(f"{prefix}_PORT", default)
    port = int(raw_port, 10)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return f"http://{host}:{port}"


def _json_response(response: Any) -> object:
    try:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        close = getattr(response, "close", None)
        if close:
            close()
    if not 200 <= int(status) < 300 or len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("invalid response")
    return json.loads(payload.decode("utf-8"))


def _get_json(
    url: str,
    api_key: str,
    *,
    opener: Any,
    timeout: float,
) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "WyseARR-rotation-check/1",
            "X-Api-Key": api_key,
        },
        method="GET",
    )
    return _json_response(opener.open(request, timeout=timeout))


def _normal(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _command_records(payload: object) -> list[dict[str, object]] | None:
    records: object
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("records")
    else:
        return None
    if not isinstance(records, list) or any(
        not isinstance(item, dict) for item in records
    ):
        return None
    return records


def _reset_command_evidence(payload: object) -> tuple[bool, bool]:
    """Accept a completed reset or its documented-pruned absence.

    Prowlarr 2.5.2 removes completed command rows from this bounded endpoint.
    A valid response with no ResetApiKey row therefore means "pruned"; the
    surrounding credential-change/authentication checks remain the authority
    that a rotation actually occurred.
    """

    records = _command_records(payload)
    if records is None:
        return False, False
    resets: list[tuple[int, str]] = []
    for item in records:
        if _normal(item.get("name", item.get("commandName", ""))) != "resetapikey":
            continue
        identifier = item.get("id")
        status = _normal(item.get("status", ""))
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier < 1
            or not status
        ):
            return False, False
        resets.append((identifier, status))
    if not resets:
        return True, True
    statuses = {status for _, status in resets}
    if not statuses <= ACTIVE_COMMAND_STATUSES | KNOWN_TERMINAL_COMMAND_STATUSES:
        return False, False
    newest_status = max(resets)[1]
    return newest_status == "completed", all(
        status not in ACTIVE_COMMAND_STATUSES for _, status in resets
    )


def _provider_fields(resource: Mapping[str, object]) -> dict[str, object] | None:
    raw_fields = resource.get("fields")
    if not isinstance(raw_fields, list):
        return None
    fields: dict[str, object] = {}
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            return None
        name = str(raw_field.get("name") or "").casefold()
        if not name or name in fields:
            return None
        # Arr includes non-value UI action fields in this array.  Preserve
        # their names for duplicate detection without rejecting the resource.
        fields[name] = raw_field.get("value")
    return fields


def _prowlarr_proxy_route(fields: Mapping[str, object]) -> str | None:
    base_url = fields.get("baseurl")
    if not isinstance(base_url, str):
        return None
    parsed = urllib.parse.urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError:
        return None
    path = parsed.path.strip("/")
    if not (
        parsed.scheme == "http"
        and parsed.hostname == "prowlarr"
        and port == 9696
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"[1-9][0-9]*", path)
        and fields.get("apipath") == "/api"
    ):
        return None
    return path


def _arr_consumer_matches(
    payload: object,
    *,
    expected_count: int,
    prowlarr_api_key: str,
) -> bool:
    if (
        not isinstance(payload, list)
        or len(payload) != expected_count
        or any(not isinstance(item, dict) for item in payload)
    ):
        return False
    routes: list[str] = []
    for item in payload:
        fields = _provider_fields(item)
        stored_key = fields.get("apikey") if fields is not None else None
        route = _prowlarr_proxy_route(fields) if fields is not None else None
        if (
            item.get("enable", True) is not True
            or isinstance(item.get("downloadClientId"), bool)
            or item.get("downloadClientId") != 0
            or str(item.get("implementation") or "").casefold() != "torznab"
            or not isinstance(stored_key, str)
            or not (
                secrets.compare_digest(stored_key, prowlarr_api_key)
                or stored_key == "********"
            )
            or route is None
        ):
            return False
        routes.append(route)
    return len(set(routes)) == expected_count


def _persisted_arr_consumers_match(
    rows: object,
    *,
    expected_count: int,
    prowlarr_api_key: str,
) -> bool:
    """Validate exact unmasked credentials from a read-only Arr DB query."""

    if (
        not isinstance(rows, list)
        or len(rows) != expected_count
        or any(not isinstance(row, tuple) or len(row) != 3 for row in rows)
    ):
        return False
    routes: list[str] = []
    for download_client_id, implementation, raw_settings in rows:
        if not isinstance(raw_settings, str):
            return False
        try:
            settings = json.loads(raw_settings)
        except (TypeError, ValueError):
            return False
        if not isinstance(settings, dict):
            return False
        normalized = {str(key).casefold(): value for key, value in settings.items()}
        if len(normalized) != len(settings):
            return False
        stored_key = normalized.get("apikey")
        route = _prowlarr_proxy_route(normalized)
        if (
            isinstance(download_client_id, bool)
            or download_client_id not in {None, 0}
            or str(implementation).casefold() != "torznab"
            or not isinstance(stored_key, str)
            or not secrets.compare_digest(stored_key, prowlarr_api_key)
            or route is None
        ):
            return False
        routes.append(route)
    return len(set(routes)) == expected_count


def _enabled(value: object) -> bool:
    return value in {True, 1, "1", "true", "True"}


def _lazylibrarian_consumers_match(
    payload: object,
    *,
    prowlarr_api_key: str,
) -> bool:
    provider_types = ("newznab", "torznab", "rss", "irc", "torrent", "direct")
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(provider_type), list)
        or any(
            not isinstance(provider, dict)
            for provider in payload.get(provider_type, [])
        )
        for provider_type in provider_types
    ):
        return False
    active = [
        (provider_type, provider)
        for provider_type in provider_types
        for provider in payload[provider_type]
        if _enabled(provider.get("ENABLED"))
    ]
    return bool(
        len(active) == 2
        and all(provider_type == "torznab" for provider_type, _ in active)
        and all(
            isinstance(provider.get("API"), str)
            and secrets.compare_digest(provider["API"], prowlarr_api_key)
            for _, provider in active
        )
    )


def _environment_list(values: object) -> dict[str, str] | None:
    if not isinstance(values, list):
        return None
    environment: dict[str, str] = {}
    for item in values:
        if not isinstance(item, str) or "=" not in item:
            return None
        name, value = item.split("=", 1)
        if not name or name in environment:
            return None
        environment[name] = value
    return environment


def _load_identity_snapshot(path: Path) -> dict[str, ContainerIdentity]:
    text, _ = _read_private_bounded(path, maximum_bytes=MAX_IDENTITY_BYTES)
    payload = json.loads(text)
    if not isinstance(payload, dict) or set(payload) != set(IDENTITY_SERVICES):
        raise ValueError("invalid identity snapshot")
    identities: dict[str, ContainerIdentity] = {}
    for service in IDENTITY_SERVICES:
        resource = payload[service]
        if not isinstance(resource, dict) or set(resource) != {"id", "started_at"}:
            raise ValueError("invalid identity snapshot")
        identifier = resource["id"]
        started_at = resource["started_at"]
        if (
            not isinstance(identifier, str)
            or IDENTIFIER_RE.fullmatch(identifier) is None
            or not isinstance(started_at, str)
            or STARTED_AT_RE.fullmatch(started_at) is None
        ):
            raise ValueError("invalid identity snapshot")
        identities[service] = ContainerIdentity(identifier, started_at)
    return identities


class LiveEvidenceBackend:
    """Concrete read-only observations; every caller suppresses its failures."""

    def __init__(
        self,
        root: Path,
        environment: Mapping[str, str],
        *,
        timeout: float = 15.0,
        opener: Any | None = None,
        runner: Callable[..., Any] = subprocess.run,
        qbit_client_factory: Callable[..., Any] = QbittorrentClient,
    ):
        self.root = root
        self.environment = dict(environment)
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener()
        self.runner = runner
        self.qbit_client_factory = qbit_client_factory
        self._inspections: dict[str, dict[str, object] | None] = {}

    def _prowlarr_url(self, path: str) -> str:
        return _fixed_base_url(
            self.environment, "PROWLARR", "9696"
        ) + path

    def prowlarr_auth_matches(self, api_key: str) -> bool:
        try:
            payload = _get_json(
                self._prowlarr_url("/api/v1/config/host"),
                api_key,
                opener=self.opener,
                timeout=self.timeout,
            )
        except Exception:
            return False
        returned_key = payload.get("apiKey") if isinstance(payload, dict) else None
        return bool(
            isinstance(returned_key, str)
            and secrets.compare_digest(returned_key, api_key)
        )

    def prowlarr_auth_rejected(self, api_key: str) -> bool:
        try:
            _get_json(
                self._prowlarr_url("/api/v1/config/host"),
                api_key,
                opener=self.opener,
                timeout=self.timeout,
            )
        except urllib.error.HTTPError as error:
            try:
                return error.code in AUTH_REJECTION_STATUSES
            finally:
                error.close()
        except Exception:
            return False
        return False

    def prowlarr_commands(self, api_key: str) -> object:
        return _get_json(
            self._prowlarr_url("/api/v1/command"),
            api_key,
            opener=self.opener,
            timeout=self.timeout,
        )

    def _arr_client(self, spec: ArrSpec) -> Any:
        base_url = _fixed_base_url(
            self.environment, spec.environment_prefix, spec.default_port
        )
        api_key = self.environment.get(f"{spec.environment_prefix}_API_KEY", "")
        if not api_key:
            raise ValueError("missing consumer credential")
        return ApiClient(
            base_url,
            headers={"X-Api-Key": api_key},
            opener=self.opener,
            timeout=self.timeout,
            retries=1,
        )

    def arr_indexers(self, spec: ArrSpec) -> object:
        return self._arr_client(spec).get_json(
            f"/api/{spec.api_version}/indexer"
        )

    def arr_persisted_keys_match(self, spec: ArrSpec, api_key: str) -> bool:
        database = _private_path(self.root, Path(spec.database_relative))
        metadata = database.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            return False
        with closing(
            sqlite3.connect(
                f"file:{database}?mode=ro", uri=True, timeout=self.timeout
            )
        ) as connection:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT DownloadClientId, Implementation, Settings "
                "FROM Indexers ORDER BY Id"
            ).fetchall()
        return _persisted_arr_consumers_match(
            rows,
            expected_count=spec.expected_indexers,
            prowlarr_api_key=api_key,
        )

    def arr_indexers_live(self, spec: ArrSpec, indexers: object) -> bool:
        if not isinstance(indexers, list) or any(
            not isinstance(item, dict) for item in indexers
        ):
            return False
        client = self._arr_client(spec)
        results: list[bool] = []
        for indexer in indexers:
            try:
                client.post_json(
                    f"/api/{spec.api_version}/indexer/test",
                    indexer,
                    retry=False,
                )
                results.append(True)
            except BaseException:
                results.append(False)
        return bool(results) and all(results)

    def lazylibrarian_providers(self) -> object:
        raw_port = self.environment.get("LAZYLIBRARIAN_ADMIN_PORT", "5299")
        port = int(raw_port, 10)
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        api_key = self.environment.get("LAZYLIBRARIAN_API_KEY", "")
        if API_KEY_RE.fullmatch(api_key) is None:
            raise ValueError("invalid credential")
        body = urllib.parse.urlencode(
            {"apikey": api_key, "cmd": "listProviders"}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "WyseARR-rotation-check/1",
            },
            method="POST",
        )
        return _json_response(self.opener.open(request, timeout=self.timeout))

    def shelfarr_key_matches(self, api_key: str) -> bool:
        database = _private_path(
            self.root, Path("config/shelfarr/production.sqlite3")
        )
        metadata = database.lstat()
        if not database.is_file() or database.is_symlink() or metadata.st_uid != os.geteuid():
            return False
        with closing(
            sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=self.timeout)
        ) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'prowlarr_api_key'"
            ).fetchone()
        stored_key = row[0] if row and len(row) == 1 else None
        return bool(
            isinstance(stored_key, str)
            and secrets.compare_digest(stored_key, api_key)
        )

    def _inspect(self, service: str) -> dict[str, object] | None:
        if service in self._inspections:
            return self._inspections[service]
        try:
            selected = self.runner(
                ["docker", "compose", "ps", "-q", service],
                cwd=self.root,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout,
            )
            identifiers = selected.stdout.splitlines()
            if selected.returncode or len(identifiers) != 1 or not identifiers[0]:
                raise ValueError("container unavailable")
            inspected = self.runner(
                ["docker", "inspect", identifiers[0]],
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout,
            )
            payload = json.loads(inspected.stdout) if not inspected.returncode else None
            value = (
                payload[0]
                if isinstance(payload, list)
                and len(payload) == 1
                and isinstance(payload[0], dict)
                else None
            )
        except Exception:
            value = None
        self._inspections[service] = value
        return value

    def huey_key_matches(self, api_key: str) -> bool:
        details = self._inspect("huey")
        config = details.get("Config") if isinstance(details, dict) else None
        state = details.get("State") if isinstance(details, dict) else None
        environment = _environment_list(
            config.get("Env") if isinstance(config, dict) else None
        )
        stored_key = environment.get("PROWLARR_API_KEY") if environment else None
        return bool(
            isinstance(state, dict)
            and state.get("Running") is True
            and isinstance(stored_key, str)
            and secrets.compare_digest(stored_key, api_key)
        )

    def ebook_lanes_empty(self) -> bool:
        base_url = _fixed_base_url(
            self.environment, "QBITTORRENT", "8080"
        )
        username = self.environment.get("QBITTORRENT_USERNAME", "admin")
        password = self.environment.get("QBITTORRENT_PASSWORD", "")
        if not username or not password:
            return False
        client = self.qbit_client_factory(
            base_url, timeout=self.timeout, retries=1
        )
        if not client.login(username, password):
            return False
        categories = client.categories()
        return bool(
            isinstance(categories, dict)
            and all(
                isinstance(categories.get(category), dict)
                and categories[category].get("savePath") == expected_path
                for category, expected_path in EBOOK_LANE_PATHS.items()
            )
            and all(client.torrents(category) == [] for category in EBOOK_LANES)
        )

    def container_identity_matches(
        self, service: str, expected: ContainerIdentity
    ) -> bool:
        if service not in IDENTITY_SERVICES:
            return False
        details = self._inspect(service)
        state = details.get("State") if isinstance(details, dict) else None
        identifier = details.get("Id") if isinstance(details, dict) else None
        started_at = state.get("StartedAt") if isinstance(state, dict) else None
        return bool(
            isinstance(identifier, str)
            and isinstance(started_at, str)
            and secrets.compare_digest(identifier, expected.identifier)
            and secrets.compare_digest(started_at, expected.started_at)
        )


BackendFactory = Callable[[Path, Mapping[str, str]], EvidenceBackend]


def _safe_boolean(operation: Callable[[], object]) -> bool:
    try:
        return operation() is True
    except BaseException:
        return False


def _arr_evidence(
    backend: EvidenceBackend,
    spec: ArrSpec,
    prowlarr_api_key: str,
) -> tuple[bool, bool]:
    try:
        payload = backend.arr_indexers(spec)
    except BaseException:
        return False, False
    api_shape_ok = _safe_boolean(
        lambda: _arr_consumer_matches(
            payload,
            expected_count=spec.expected_indexers,
            prowlarr_api_key=prowlarr_api_key,
        )
    )
    persisted_ok = _safe_boolean(
        lambda: backend.arr_persisted_keys_match(spec, prowlarr_api_key)
    )
    # A valid masked resource is tested exactly once.  Evaluate this even when
    # the DB comparison failed so every resource is checked and the resulting
    # failure cannot be hidden by short-circuit evaluation.
    live_ok = bool(
        api_shape_ok
        and _safe_boolean(lambda: backend.arr_indexers_live(spec, payload))
    )
    return api_shape_ok and persisted_ok, live_ok


def _all_failures(*, identity_requested: bool) -> tuple[Check, ...]:
    names = BASE_CHECK_NAMES + (IDENTITY_CHECK_NAMES if identity_requested else ())
    return tuple(Check(name, False) for name in names)


def check_rotation(
    root: Path,
    old_environment_path: Path,
    *,
    identity_snapshot_path: Path | None = None,
    backend_factory: BackendFactory = LiveEvidenceBackend,
) -> tuple[Check, ...]:
    """Collect complete post-rotation evidence without leaking a failure cause."""

    identity_requested = identity_snapshot_path is not None
    try:
        safe_root = _safe_root(root)
        current_env_path = _private_path(safe_root, Path(".env"))
        config_path = _private_path(
            safe_root, Path("config/prowlarr/config.xml")
        )
        current_text, _ = _read_private_bounded(
            current_env_path, maximum_bytes=MAX_ENV_BYTES
        )
        config_text, _ = _read_private_bounded(
            config_path, maximum_bytes=MAX_CONFIG_BYTES
        )
        old_text, _ = _read_private_bounded(
            Path(os.path.abspath(os.fspath(old_environment_path))),
            maximum_bytes=MAX_ENV_BYTES,
        )
        current_environment = _environment(current_text)
        old_environment = _environment(old_text)
        current_key = current_environment.get("PROWLARR_API_KEY", "")
        old_key = old_environment.get("PROWLARR_API_KEY", "")
        persisted_key = _config_key(config_text)
        identities = (
            _load_identity_snapshot(
                Path(os.path.abspath(os.fspath(identity_snapshot_path)))
            )
            if identity_snapshot_path is not None
            else {}
        )
        backend = backend_factory(safe_root, current_environment)
    except BaseException:
        return _all_failures(identity_requested=identity_requested)

    shape_ok = all(
        API_KEY_RE.fullmatch(key) is not None
        for key in (old_key, current_key, persisted_key)
    )
    changed_ok = bool(
        shape_ok and not secrets.compare_digest(old_key, current_key)
    )
    convergence_ok = bool(
        shape_ok and secrets.compare_digest(current_key, persisted_key)
    )
    results: dict[str, bool] = {
        "credentials:key-shape": shape_ok,
        "credentials:key-changed": changed_ok,
        "credentials:local-convergence": convergence_ok,
        "prowlarr:new-auth": bool(
            convergence_ok
            and _safe_boolean(lambda: backend.prowlarr_auth_matches(current_key))
        ),
        "prowlarr:old-auth-rejected": bool(
            changed_ok
            and _safe_boolean(lambda: backend.prowlarr_auth_rejected(old_key))
        ),
    }

    try:
        terminal_or_pruned, active_zero = _reset_command_evidence(
            backend.prowlarr_commands(current_key)
        ) if convergence_ok else (False, False)
    except BaseException:
        terminal_or_pruned, active_zero = False, False
    results["prowlarr:reset-terminal-or-pruned"] = terminal_or_pruned
    results["prowlarr:reset-active-zero"] = active_zero

    for spec in ARR_SPECS:
        try:
            credential_ok, live_ok = (
                _arr_evidence(backend, spec, current_key)
                if convergence_ok
                else (False, False)
            )
        except BaseException:
            credential_ok, live_ok = False, False
        results[f"consumers:{spec.name}:credential"] = credential_ok
        results[f"consumers:{spec.name}:live"] = live_ok
    results["consumers:lazylibrarian"] = bool(
        convergence_ok
        and _safe_boolean(
            lambda: _lazylibrarian_consumers_match(
                backend.lazylibrarian_providers(),
                prowlarr_api_key=current_key,
            )
        )
    )
    results["consumers:shelfarr"] = bool(
        convergence_ok
        and _safe_boolean(lambda: backend.shelfarr_key_matches(current_key))
    )
    results["consumers:huey"] = bool(
        convergence_ok
        and _safe_boolean(lambda: backend.huey_key_matches(current_key))
    )
    results["qbittorrent:ebook-lanes-empty"] = _safe_boolean(
        backend.ebook_lanes_empty
    )

    if identity_requested:
        for service in IDENTITY_SERVICES:
            expected = identities.get(service)
            results[f"container:{service}-identity"] = bool(
                expected is not None
                and _safe_boolean(
                    lambda service=service, expected=expected: (
                        backend.container_identity_matches(service, expected)
                    )
                )
            )

    names = BASE_CHECK_NAMES + (IDENTITY_CHECK_NAMES if identity_requested else ())
    return tuple(Check(name, results.get(name, False)) for name in names)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=STACK_ROOT)
    parser.add_argument(
        "--old-env",
        type=Path,
        required=True,
        help="private .env from the pre-rotation checkpoint",
    )
    parser.add_argument(
        "--identity-snapshot",
        type=Path,
        help="optional private pre-rotation container identity JSON",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args(argv)


def _emit(checks: Sequence[Check], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "passed": all(check.ok for check in checks),
                    "checks": {check.name: check.ok for check in checks},
                },
                sort_keys=True,
            )
        )
        return
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'}: {check.name}")
    print(f"{'PASS' if all(check.ok for check in checks) else 'FAIL'}: overall")


def main(
    argv: Sequence[str] | None = None,
    *,
    collector: Callable[..., tuple[Check, ...]] = check_rotation,
) -> int:
    arguments = parse_args(argv)
    try:
        checks = collector(
            arguments.root,
            arguments.old_env,
            identity_snapshot_path=arguments.identity_snapshot,
        )
    except BaseException:
        checks = _all_failures(
            identity_requested=arguments.identity_snapshot is not None
        )
    _emit(checks, as_json=arguments.as_json)
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
