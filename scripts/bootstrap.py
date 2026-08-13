#!/usr/bin/env python3
"""Reproducibly bootstrap the live WyseARR service configuration.

The script deliberately uses only the Python standard library.  It obtains API
keys from the ignored, persisted service configuration, keeps credentials in a
private ``.env`` file, and performs idempotent API updates.  Secret values are
never included in normal output or exception messages.
"""

from __future__ import annotations

import argparse
import ast
import copy
import http.cookiejar
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


BASE_CATEGORIES = (
    "tv",
    "movies",
    "music",
    "spicy",
    "ebooks",
    "audiobooks",
    "manga-comics",
    "roms",
    "sheet-music",
)
SHELFARR_DOWNLOAD_CATEGORY = "shelfarr"
EXTRA_DOWNLOAD_DIRECTORIES = (
    "incomplete",
    "incomplete/usenet",
    "usenet",
    SHELFARR_DOWNLOAD_CATEGORY,
)
CATEGORIES = tuple(
    category
    for base in BASE_CATEGORIES
    for category in (base, f"{base}-imported")
)

QBITTORRENT_PREFERENCES = {
    "save_path": "/downloads",
    "temp_path_enabled": True,
    "temp_path": "/downloads/incomplete",
    # Retention is performed by BookBot after 14 days.  qBittorrent must not
    # race an import or independently remove a torrent at a ratio/time limit.
    "max_ratio_enabled": False,
    "max_seeding_time_enabled": False,
}
QBITTORRENT_AUTH_FAILURE_LIMIT = 5
QBITTORRENT_ROTATION_GUARD_LIMIT = 1000

API_KEY_CONFIGS = {
    "PROWLARR_API_KEY": "prowlarr",
    "SONARR_API_KEY": "sonarr",
    "RADARR_API_KEY": "radarr",
    "LIDARR_API_KEY": "lidarr",
    "WHISPARR_API_KEY": "whisparr",
}


@dataclass(frozen=True)
class ArrService:
    name: str
    port_env: str
    default_port: int
    category: str
    api_versions: tuple[str, ...]
    category_fields: tuple[str, ...]
    imported_category_fields: tuple[str, ...]


ARR_SERVICES = (
    ArrService(
        "Sonarr", "SONARR_PORT", 8989, "tv", ("v3", "v1"),
        ("category", "tvCategory"),
        ("postImportCategory", "tvImportedCategory"),
    ),
    ArrService(
        "Radarr", "RADARR_PORT", 7878, "movies", ("v3", "v1"),
        ("category", "movieCategory"),
        ("postImportCategory", "movieImportedCategory"),
    ),
    ArrService(
        "Lidarr", "LIDARR_PORT", 8686, "music", ("v1", "v3"),
        ("category", "musicCategory"),
        ("postImportCategory", "musicImportedCategory"),
    ),
    ArrService(
        "Whisparr", "WHISPARR_PORT", 6969, "spicy", ("v3", "v1"),
        ("category", "tvCategory"),
        ("postImportCategory", "tvImportedCategory"),
    ),
)


@dataclass(frozen=True)
class IndexerSpec:
    name: str
    definition: str
    base_url: str | None


@dataclass(frozen=True)
class ManagedNewznabConfig:
    enabled: bool
    name: str
    base_url: str
    api_key: str
    api_path: str


PUBLIC_INDEXERS = (
    # Music-focused public source with a browse feed that Lidarr can validate.
    IndexerSpec("MixtapeTorrent", "mixtapetorrent", None),
    # These credential-free adult sources give Whisparr a real category-6000
    # path. Their dynamic URL providers are left for Prowlarr to resolve.
    IndexerSpec("Sukebei Nyaa", "sukebeinyaasi", None),
    IndexerSpec("PornRips", "pornrips", None),
    IndexerSpec("PornoTorrent", "pornotorrent", None),
    IndexerSpec("Nyaa.si", "nyaasi", "https://nyaa.si/"),
    IndexerSpec("LimeTorrents", "limetorrents", "https://www.limetorrents.fun/"),
    IndexerSpec(
        "Torrent Downloads",
        "torrentdownloads",
        "https://www.torrentdownloads.pro/",
    ),
    IndexerSpec("The Pirate Bay", "thepiratebay", "https://thepiratebay.org/"),
)

USENET_FEATURE_FLAG = "WYSEARR_USENET_ENABLED"
SHELFARR_PROWLARR_TAG = "shelfarr"
ARR_PROWLARR_TAG = "wysearr-arr"
DEFAULT_NEWZNAB_INDEXER_NAME = "WyseARR Books"
GENERIC_NEWZNAB_SCHEMA_NAME = "Generic Newznab"

BAZARR_PROVIDERS = ("embeddedsubtitles", "yifysubtitles", "subf2m")
BAZARR_USER_AGENT = "Mozilla/5.0 (WyseARR Bazarr)"
MASKED_SECRET_VALUES = {"********", "(removed)", "<redacted>"}
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)


class BootstrapError(RuntimeError):
    """An operator-actionable error which is safe to print."""


class ApiError(BootstrapError):
    """HTTP failure whose string form intentionally excludes response data."""

    def __init__(self, status: int, path: str, reason: str, body: bytes = b""):
        self.status = status
        self.path = path
        self.reason = reason
        self.body = body
        super().__init__(f"HTTP {status} from {path} ({reason or 'request failed'})")


class ApiTransportError(BootstrapError):
    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Unable to reach API endpoint {path}")


@dataclass(frozen=True)
class ApiResponse:
    status: int
    reason: str
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BootstrapError("API returned malformed JSON") from exc


class ApiClient:
    """Small JSON/form HTTP client with bounded retries and safe errors."""

    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        opener: Any | None = None,
        timeout: float = 10.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "WyseARR-bootstrap/1",
            **dict(headers or {}),
        }
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout
        self.retries = max(1, retries)
        self.sleep = sleep

    def request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = False,
    ) -> ApiResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        request_headers = {**self.headers, **dict(headers or {})}
        attempts = self.retries if retry else 1
        for attempt in range(attempts):
            request = urllib.request.Request(
                url,
                data=data,
                headers=request_headers,
                method=method.upper(),
            )
            try:
                response = self.opener.open(request, timeout=self.timeout)
                try:
                    body = response.read()
                    status = getattr(response, "status", response.getcode())
                    reason = str(getattr(response, "reason", ""))
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                if not 200 <= status < 300:
                    raise ApiError(status, path, reason, body)
                return ApiResponse(status, reason, body)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code in self.RETRYABLE_STATUS and attempt + 1 < attempts:
                    self.sleep(min(2**attempt, 4))
                    continue
                raise ApiError(exc.code, path, str(exc.reason), body) from None
            except ApiError as exc:
                if exc.status in self.RETRYABLE_STATUS and attempt + 1 < attempts:
                    self.sleep(min(2**attempt, 4))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt + 1 < attempts:
                    self.sleep(min(2**attempt, 4))
                    continue
                raise ApiTransportError(path) from None
        raise ApiTransportError(path)

    def get_json(self, path: str) -> Any:
        return self.request("GET", path, retry=True).json()

    def post_json(self, path: str, payload: Any, *, retry: bool = False) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self.request(
            "POST",
            path,
            data=body,
            headers={"Content-Type": "application/json"},
            retry=retry,
        )
        return response.json()

    def put_json(self, path: str, payload: Any) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self.request(
            "PUT",
            path,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def post_form_response(
        self,
        path: str,
        values: Mapping[str, Any] | Sequence[tuple[str, Any]],
        *,
        retry: bool = False,
    ) -> ApiResponse:
        data = urllib.parse.urlencode(values, doseq=True).encode("utf-8")
        return self.request(
            "POST",
            path,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            retry=retry,
        )

    def post_form(
        self,
        path: str,
        values: Mapping[str, Any] | Sequence[tuple[str, Any]],
        *,
        retry: bool = False,
    ) -> Any:
        return self.post_form_response(path, values, retry=retry).json()


class QbittorrentClient:
    def __init__(self, base_url: str, *, timeout: float = 10.0, retries: int = 3):
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        self.api = ApiClient(
            base_url,
            headers={"Referer": f"{base_url.rstrip('/')}/"},
            opener=opener,
            timeout=timeout,
            retries=retries,
        )

    def login(self, username: str, password: str) -> bool:
        response = self.api.post_form_response(
            "/api/v2/auth/login",
            {"username": username, "password": password},
            retry=True,
        )
        return response.text.strip() == "Ok."

    def preferences(self) -> dict[str, Any]:
        result = self.api.get_json("/api/v2/app/preferences")
        if not isinstance(result, dict):
            raise BootstrapError("qBittorrent returned invalid preferences")
        return result

    def set_preferences(self, preferences: Mapping[str, Any]) -> None:
        self.api.post_form_response(
            "/api/v2/app/setPreferences",
            {"json": json.dumps(dict(preferences), separators=(",", ":"))},
            retry=True,
        )

    def categories(self) -> dict[str, Any]:
        result = self.api.get_json("/api/v2/torrents/categories")
        if not isinstance(result, dict):
            raise BootstrapError("qBittorrent returned invalid categories")
        return result

    def torrents(self, category: str) -> list[dict[str, Any]]:
        result = self.api.get_json(
            "/api/v2/torrents/info?"
            + urllib.parse.urlencode({"category": category})
        )
        if not isinstance(result, list) or any(
            not isinstance(item, dict) for item in result
        ):
            raise BootstrapError("qBittorrent returned invalid torrent inventory")
        return result

    def create_category(self, name: str, save_path: str) -> None:
        self.api.post_form_response(
            "/api/v2/torrents/createCategory",
            {"category": name, "savePath": save_path},
            retry=True,
        )

    def edit_category(self, name: str, save_path: str) -> None:
        self.api.post_form_response(
            "/api/v2/torrents/editCategory",
            {"category": name, "savePath": save_path},
            retry=True,
        )

    def set_web_credentials(self, username: str, password: str) -> None:
        self.set_preferences(
            {"web_ui_username": username, "web_ui_password": password}
        )


class Reporter:
    def info(self, message: str) -> None:
        print(message)

    def warning(self, message: str) -> None:
        print(f"WARNING: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"ERROR: {message}", file=sys.stderr)


def detect_project_root(start: Path | None = None) -> Path:
    candidate = (start or Path(__file__)).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / "docker-compose.yml").is_file() and (
            directory / "config"
        ).is_dir():
            return directory
    raise BootstrapError("Unable to locate project root")


def _decode_env_value(raw_value: str) -> str:
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        parts = list(lexer)
    except ValueError as exc:
        raise BootstrapError("Malformed value in .env") from exc
    return " ".join(parts)


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BootstrapError("Unable to read .env") from exc
    for line in lines:
        match = ENV_ASSIGNMENT_RE.match(line)
        if match:
            key = match.group(1)
            if key == USENET_FEATURE_FLAG:
                # Match deploy.sh's literal contract. Quoting, whitespace, or
                # alternate assignment syntax must not silently enable NNTP.
                values[key] = (
                    "__WYSEARR_DUPLICATE__"
                    if key in values
                    else (
                        line[len(f"{key}="):]
                        if line.startswith(f"{key}=")
                        else line
                    )
                )
            else:
                values[key] = _decode_env_value(match.group(2))
    return values


def _encode_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+,=-]*", value):
        return value
    return json.dumps(value)


def render_dotenv(existing: str, updates: Mapping[str, str]) -> str:
    lines = existing.splitlines()
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = ENV_ASSIGNMENT_RE.match(line)
        if match:
            positions[match.group(1)] = index
    for key, value in updates.items():
        rendered = f"{key}={_encode_env_value(value)}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            positions[key] = len(lines)
            lines.append(rendered)
    return "\n".join(lines) + "\n"


def atomic_write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support directory fsync.  The file data
            # itself has already been flushed and replaced atomically.
            pass
    except OSError as exc:
        raise BootstrapError("Unable to update .env atomically") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def update_dotenv(path: Path, updates: Mapping[str, str]) -> dict[str, str]:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write_private_text(path, render_dotenv(existing, updates))
    return load_dotenv(path)


def read_api_key(config_xml: Path) -> str:
    try:
        root = ET.parse(config_xml).getroot()
    except (OSError, ET.ParseError) as exc:
        raise BootstrapError(
            f"Unable to read API key from {config_xml.parent.name}/config.xml"
        ) from exc
    api_key = (root.findtext("ApiKey") or "").strip()
    if not api_key:
        raise BootstrapError(
            f"API key is missing from {config_xml.parent.name}/config.xml"
        )
    return api_key


def prepare_environment(
    root: Path,
    *,
    password_factory: Callable[[], str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    env_path = root / ".env"
    current = load_dotenv(env_path)
    password_factory = password_factory or (lambda: secrets.token_urlsafe(24))
    updates: dict[str, str] = {}
    if not current.get("QBITTORRENT_USERNAME"):
        updates["QBITTORRENT_USERNAME"] = "admin"
    if not current.get("QBITTORRENT_PASSWORD"):
        updates["QBITTORRENT_PASSWORD"] = password_factory()
    for env_name, service in API_KEY_CONFIGS.items():
        updates[env_name] = read_api_key(root / "config" / service / "config.xml")
    result = update_dotenv(env_path, updates)
    for required in ("QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD"):
        if not result.get(required):
            raise BootstrapError(f"{required} is present but empty in .env")
    api_keys = {key: result[key] for key in API_KEY_CONFIGS}
    return result, api_keys


def torrent_root_from_environment(root: Path, environment: Mapping[str, str]) -> Path:
    configured = environment.get("TORRENT_ROOT")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else root / path
    return root / "state" / "torrents"


def ensure_download_directories(
    root: Path, environment: Mapping[str, str]
) -> tuple[Path, ...]:
    torrent_root = torrent_root_from_environment(root, environment)
    directories = tuple(
        torrent_root / name
        for name in (*BASE_CATEGORIES, *EXTRA_DOWNLOAD_DIRECTORIES)
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    return directories


TEMPORARY_PASSWORD_RE = re.compile(
    r"temporary password(?:\s+is provided for this session)?\s*:\s*(\S+)",
    re.IGNORECASE,
)


def extract_temporary_password(log_text: str) -> str | None:
    matches = TEMPORARY_PASSWORD_RE.findall(log_text)
    return matches[-1].strip() if matches else None


def read_qbittorrent_logs() -> str:
    try:
        result = subprocess.run(
            ["docker", "logs", "--timestamps", "--tail", "2000", "qbittorrent"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("Unable to inspect qBittorrent container logs") from exc
    if result.returncode != 0:
        raise BootstrapError("Unable to inspect qBittorrent container logs")
    return result.stdout


def authenticate_qbittorrent(
    base_url: str,
    username: str,
    password: str,
    *,
    timeout: float = 10.0,
    retries: int = 3,
    client_factory: Callable[..., Any] = QbittorrentClient,
    logs_reader: Callable[[], str] = read_qbittorrent_logs,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    client = client_factory(base_url, timeout=timeout, retries=retries)
    if client.login(username, password):
        return client

    temporary_password = extract_temporary_password(logs_reader())
    if not temporary_password:
        raise BootstrapError(
            "qBittorrent rejected .env credentials and no current temporary "
            "WebUI password was found in container logs"
        )
    if not client.login("admin", temporary_password):
        raise BootstrapError("qBittorrent rejected its current temporary password")
    client.set_web_credentials(username, password)

    for attempt in range(retries):
        verifier = client_factory(base_url, timeout=timeout, retries=retries)
        if verifier.login(username, password):
            return verifier
        if attempt + 1 < retries:
            sleep(min(2**attempt, 4))
    raise BootstrapError("qBittorrent did not accept the persisted WebUI credentials")


def restart_qbittorrent_with_rotation_guard(
    client: Any,
    base_url: str,
    username: str,
    password: str,
    *,
    timeout: float,
    retries: int,
    runner: Callable[..., Any] = subprocess.run,
    client_factory: Callable[..., Any] = QbittorrentClient,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Clear stale-client IP bans while a new shared password is propagated."""

    client.set_preferences(
        {"web_ui_max_auth_fail_count": QBITTORRENT_ROTATION_GUARD_LIMIT}
    )
    try:
        result = runner(
            ["docker", "restart", "qbittorrent"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            client.set_preferences(
                {"web_ui_max_auth_fail_count": QBITTORRENT_AUTH_FAILURE_LIMIT}
            )
        except Exception:
            pass
        raise BootstrapError("Unable to restart qBittorrent for credential repair") from exc
    if result.returncode != 0:
        try:
            client.set_preferences(
                {"web_ui_max_auth_fail_count": QBITTORRENT_AUTH_FAILURE_LIMIT}
            )
        except Exception:
            pass
        raise BootstrapError("Unable to restart qBittorrent for credential repair")

    deadline = time.monotonic() + max(60.0, timeout * retries)
    while time.monotonic() < deadline:
        candidate = client_factory(base_url, timeout=timeout, retries=retries)
        try:
            if candidate.login(username, password):
                return candidate
        except BootstrapError:
            pass
        sleep(2)
    raise BootstrapError("qBittorrent did not recover after credential repair restart")


def configure_qbittorrent(client: Any) -> tuple[int, int]:
    current_preferences = client.preferences()
    desired_preferences = dict(QBITTORRENT_PREFERENCES)
    if "max_inactive_seeding_time_enabled" in current_preferences:
        desired_preferences["max_inactive_seeding_time_enabled"] = False
    changed_preferences = {
        key: value
        for key, value in desired_preferences.items()
        if current_preferences.get(key) != value
    }
    if changed_preferences:
        client.set_preferences(changed_preferences)

    current_categories = client.categories()
    category_changes = 0
    for category in (*CATEGORIES, SHELFARR_DOWNLOAD_CATEGORY):
        base_category = category.removesuffix("-imported")
        save_path = f"/downloads/{base_category}"
        current = current_categories.get(category)
        if current is None:
            client.create_category(category, save_path)
            category_changes += 1
        elif current.get("savePath") != save_path:
            client.edit_category(category, save_path)
            category_changes += 1
    return len(changed_preferences), category_changes


def _field_map(resource: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    fields = resource.get("fields")
    if not isinstance(fields, list):
        raise BootstrapError("Provider resource has no fields")
    result = {}
    for field in fields:
        if isinstance(field, dict) and isinstance(field.get("name"), str):
            result[field["name"].casefold()] = field
    return result


def set_provider_field(
    resource: dict[str, Any], name: str, value: Any, *, required: bool = True
) -> None:
    field = _field_map(resource).get(name.casefold())
    if field is None:
        if required:
            raise BootstrapError(f"Provider schema is missing required field {name}")
        return
    field["value"] = value


def get_provider_field(resource: Mapping[str, Any], name: str) -> Any:
    field = _field_map(resource).get(name.casefold())
    return None if field is None else field.get("value")


def set_provider_field_alias(
    resource: dict[str, Any], names: Sequence[str], value: Any
) -> str:
    fields = _field_map(resource)
    for name in names:
        if name.casefold() in fields:
            fields[name.casefold()]["value"] = value
            return name
    raise BootstrapError(
        f"Provider schema is missing required field {' or '.join(names)}"
    )


def build_arr_download_client_payload(
    resource: Mapping[str, Any],
    service: ArrService,
    username: str,
    password: str,
) -> dict[str, Any]:
    payload = copy.deepcopy(dict(resource))
    set_provider_field(payload, "host", "qbittorrent")
    set_provider_field(payload, "port", 8080)
    set_provider_field(payload, "useSsl", False, required=False)
    set_provider_field(payload, "username", username)
    set_provider_field(payload, "password", password)
    set_provider_field_alias(payload, service.category_fields, service.category)
    set_provider_field_alias(
        payload,
        service.imported_category_fields,
        f"{service.category}-imported",
    )
    if "removeCompletedDownloads" in payload:
        payload["removeCompletedDownloads"] = False
    return payload


def _arr_payload_needs_update(
    current: Mapping[str, Any],
    desired: Mapping[str, Any],
    *,
    current_test_ok: bool,
) -> bool:
    if not current_test_ok:
        return True
    managed_fields = (
        "host",
        "port",
        "useSsl",
        "username",
        "category",
        "postImportCategory",
        "tvCategory",
        "tvImportedCategory",
        "movieCategory",
        "movieImportedCategory",
        "musicCategory",
        "musicImportedCategory",
    )
    desired_fields = _field_map(desired)
    for name in managed_fields:
        if name.casefold() not in desired_fields:
            continue
        if get_provider_field(current, name) != get_provider_field(desired, name):
            return True
    current_password = get_provider_field(current, "password")
    desired_password = get_provider_field(desired, "password")
    if (
        current_password not in MASKED_SECRET_VALUES
        and current_password != desired_password
    ):
        return True
    if "removeCompletedDownloads" in desired and current.get(
        "removeCompletedDownloads"
    ) is not False:
        return True
    return False


def discover_arr_api_prefix(client: Any, service: ArrService) -> str:
    for version in service.api_versions:
        prefix = f"/api/{version}"
        try:
            client.get_json(f"{prefix}/system/status")
            return prefix
        except ApiError as exc:
            if exc.status == 404:
                continue
            raise
    raise BootstrapError(f"{service.name} does not expose a supported API version")


def configure_arr_service(
    client: Any,
    service: ArrService,
    username: str,
    password: str,
    *,
    prefix: str | None = None,
) -> int:
    prefix = prefix or discover_arr_api_prefix(client, service)
    resources = client.get_json(f"{prefix}/downloadclient")
    if not isinstance(resources, list):
        raise BootstrapError(f"{service.name} returned invalid download clients")
    qbit_resources = [
        resource
        for resource in resources
        if isinstance(resource, dict)
        and str(resource.get("implementation", "")).casefold() == "qbittorrent"
    ]
    if not qbit_resources:
        raise BootstrapError(
            f"{service.name} has no existing qBittorrent download client"
        )

    updates = 0
    for current in qbit_resources:
        try:
            client.post_json(
                f"{prefix}/downloadclient/test", current, retry=True
            )
            current_test_ok = True
        except (ApiError, ApiTransportError):
            current_test_ok = False

        desired = build_arr_download_client_payload(
            current, service, username, password
        )
        if not _arr_payload_needs_update(
            current, desired, current_test_ok=current_test_ok
        ):
            continue
        try:
            client.post_json(
                f"{prefix}/downloadclient/test", desired, retry=True
            )
        except (ApiError, ApiTransportError) as exc:
            raise BootstrapError(
                f"{service.name} could not validate repaired qBittorrent settings"
            ) from exc
        resource_id = desired.get("id")
        if not isinstance(resource_id, int):
            raise BootstrapError(f"{service.name} download client has no numeric id")
        client.put_json(
            f"{prefix}/downloadclient/{resource_id}?forceSave=true", desired
        )
        # Re-test the persisted definition as the final assertion, not merely
        # the pre-save candidate.  This also catches provider-side value
        # normalization that would otherwise leave a broken saved client.
        persisted = client.get_json(f"{prefix}/downloadclient/{resource_id}")
        client.post_json(
            f"{prefix}/downloadclient/test", persisted, retry=True
        )
        updates += 1
    return updates


def configure_arr_services(
    environment: Mapping[str, str],
    api_keys: Mapping[str, str],
    qbit_username: str,
    qbit_password: str,
    *,
    timeout: float,
    retries: int,
    client_factory: Callable[..., Any] = ApiClient,
) -> int:
    updates = 0
    bind_address = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    for service in ARR_SERVICES:
        try:
            port = int(environment.get(service.port_env, service.default_port))
        except ValueError as exc:
            raise BootstrapError(f"{service.port_env} must be numeric") from exc
        client = client_factory(
            f"http://{bind_address}:{port}",
            headers={"X-Api-Key": api_keys[f"{service.name.upper()}_API_KEY"]},
            timeout=timeout,
            retries=retries,
        )
        updates += configure_arr_service(
            client, service, qbit_username, qbit_password
        )
    return updates


def validate_prowlarr_applications(client: Any) -> None:
    applications = client.get_json("/api/v1/applications")
    if not isinstance(applications, list):
        raise BootstrapError("Prowlarr returned invalid applications")
    required = {service.name.casefold(): service.name for service in ARR_SERVICES}
    found: dict[str, dict[str, Any]] = {}
    for application in applications:
        if not isinstance(application, dict):
            continue
        implementation = str(application.get("implementation", "")).casefold()
        name = str(application.get("name", "")).casefold()
        for key in required:
            if implementation == key or name == key:
                found.setdefault(key, application)
    missing = [display for key, display in required.items() if key not in found]
    if missing:
        raise BootstrapError(
            "Prowlarr is missing existing applications: " + ", ".join(missing)
        )
    for key, application in found.items():
        try:
            client.post_json(
                "/api/v1/applications/test", application, retry=True
            )
        except (ApiError, ApiTransportError) as exc:
            raise BootstrapError(
                f"Prowlarr cannot reach its {required[key]} application"
            ) from exc


def _normalize_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def managed_newznab_config(
    environment: Mapping[str, str],
) -> ManagedNewznabConfig:
    """Parse the explicit, fail-closed Usenet feature contract."""

    raw_enabled = environment.get(USENET_FEATURE_FLAG, "")
    if raw_enabled not in {"", "true", "false"}:
        raise BootstrapError(
            f"{USENET_FEATURE_FLAG} must be blank or literal true or false"
        )

    configured_name = environment.get("NEWZNAB_INDEXER_NAME", "")
    if configured_name not in {"", DEFAULT_NEWZNAB_INDEXER_NAME}:
        raise BootstrapError(
            f"NEWZNAB_INDEXER_NAME must be exactly {DEFAULT_NEWZNAB_INDEXER_NAME}"
        )
    name = DEFAULT_NEWZNAB_INDEXER_NAME
    if raw_enabled != "true":
        return ManagedNewznabConfig(False, name, "", "", "")
    if environment.get("SHELFARR_ENABLED", "") != "true":
        raise BootstrapError(
            "WYSEARR_USENET_ENABLED=true requires SHELFARR_ENABLED=true"
        )

    base_url = environment.get("NEWZNAB_BASE_URL", "").strip()
    api_key = environment.get("NEWZNAB_API_KEY", "").strip()
    api_path = environment.get("NEWZNAB_API_PATH", "").strip() or "/api"
    for variable, value in (
        ("NEWZNAB_BASE_URL", base_url),
        ("NEWZNAB_API_KEY", api_key),
    ):
        if not value:
            raise BootstrapError(f"Required private setting is missing: {variable}")

    try:
        parsed_url = urllib.parse.urlsplit(base_url)
        parsed_hostname = parsed_url.hostname
        parsed_url.port
    except ValueError as exc:
        raise BootstrapError(
            "NEWZNAB_BASE_URL must be a valid HTTP(S) URL"
        ) from exc
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or not parsed_hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
        or "\\" in base_url
        or any(character.isspace() for character in base_url)
    ):
        raise BootstrapError(
            "NEWZNAB_BASE_URL must be an HTTP(S) URL without credentials or query data"
        )
    if (
        not api_path.startswith("/")
        or api_path.startswith("//")
        or any(character in api_path for character in ("?", "#", "\\"))
        or any(character.isspace() for character in api_path)
    ):
        raise BootstrapError("NEWZNAB_API_PATH must be an absolute URL path")

    return ManagedNewznabConfig(
        True,
        name,
        base_url.rstrip("/"),
        api_key,
        api_path,
    )


def flatten_indexer_schemas(schemas: Iterable[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        flattened.append(schema)
        presets = schema.get("presets")
        if isinstance(presets, list):
            flattened.extend(flatten_indexer_schemas(presets))
    return flattened


def indexer_matches(resource: Mapping[str, Any], spec: IndexerSpec) -> bool:
    candidates = (
        resource.get("definitionName"),
        resource.get("name"),
        resource.get("implementationName"),
    )
    expected = {_normalize_name(spec.definition), _normalize_name(spec.name)}
    return any(_normalize_name(candidate) in expected for candidate in candidates)


def build_indexer_payload(
    resource: Mapping[str, Any], spec: IndexerSpec, app_profile_id: int
) -> dict[str, Any]:
    payload = copy.deepcopy(dict(resource))
    payload.pop("presets", None)
    payload["name"] = spec.name
    payload["enable"] = True
    payload["appProfileId"] = app_profile_id
    payload["priority"] = 20
    if "downloadClientId" in payload:
        payload["downloadClientId"] = 0
    if spec.base_url:
        set_provider_field(payload, "baseUrl", spec.base_url)
    return payload


def _indexer_managed_state(resource: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        resource.get("name"),
        resource.get("enable"),
        resource.get("appProfileId"),
        resource.get("priority"),
        get_provider_field(resource, "baseUrl"),
    )


def _prowlarr_tag_ids(resource: Mapping[str, Any]) -> set[int]:
    raw_tags = resource.get("tags", [])
    if not isinstance(raw_tags, list):
        raise BootstrapError("Prowlarr returned invalid application tags")
    result: set[int] = set()
    for raw_tag in raw_tags:
        value = raw_tag.get("id") if isinstance(raw_tag, dict) else raw_tag
        if isinstance(value, bool):
            raise BootstrapError("Prowlarr returned invalid application tags")
        try:
            tag_id = int(value)
        except (TypeError, ValueError) as exc:
            raise BootstrapError("Prowlarr returned invalid application tags") from exc
        if tag_id <= 0:
            raise BootstrapError("Prowlarr returned invalid application tags")
        result.add(tag_id)
    return result


def _existing_prowlarr_tag(client: Any, label: str) -> int | None:
    tags = client.get_json("/api/v1/tag")
    if not isinstance(tags, list):
        raise BootstrapError("Prowlarr returned invalid tag data")
    matching = [
        tag
        for tag in tags
        if isinstance(tag, dict)
        and str(tag.get("label", "")).strip().casefold() == label.casefold()
    ]
    if len(matching) > 1:
        raise BootstrapError(f"Prowlarr has duplicate {label} tags")
    if not matching:
        return None
    tag_id = matching[0].get("id")
    if isinstance(tag_id, bool) or not isinstance(tag_id, int) or tag_id <= 0:
        raise BootstrapError(f"Prowlarr {label} tag has no numeric id")
    return tag_id


def ensure_prowlarr_tag(client: Any, label: str) -> int:
    existing = _existing_prowlarr_tag(client, label)
    if existing is not None:
        return existing
    created = client.post_json("/api/v1/tag", {"label": label}, retry=False)
    if not isinstance(created, dict):
        raise BootstrapError(f"Prowlarr did not create the {label} tag")
    tag_id = created.get("id")
    created_label = str(created.get("label", "")).strip()
    if (
        isinstance(tag_id, bool)
        or not isinstance(tag_id, int)
        or tag_id <= 0
        or created_label.casefold() != label.casefold()
    ):
        raise BootstrapError(f"Prowlarr did not create the {label} tag")
    return tag_id


def _required_arr_applications(
    applications: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(applications, list):
        raise BootstrapError("Prowlarr returned invalid applications")
    found: dict[str, dict[str, Any]] = {}
    for service in ARR_SERVICES:
        key = service.name.casefold()
        matches = [
            application
            for application in applications
            if isinstance(application, dict)
            and (
                str(application.get("implementation", "")).casefold() == key
                or str(application.get("name", "")).casefold() == key
            )
        ]
        if len(matches) != 1:
            raise BootstrapError(
                f"Prowlarr does not have exactly one {service.name} application"
            )
        found[service.name] = matches[0]
    return found


def assert_arr_application_tag_boundary(
    applications: Any,
    *,
    arr_tag_id: int,
    shelfarr_tag_id: int,
) -> dict[str, dict[str, Any]]:
    required = _required_arr_applications(applications)
    missing_arr = []
    shelfarr_conflicts = []
    for name, application in required.items():
        tags = _prowlarr_tag_ids(application)
        if arr_tag_id not in tags:
            missing_arr.append(name)
        if shelfarr_tag_id in tags:
            shelfarr_conflicts.append(name)
    if shelfarr_conflicts:
        raise BootstrapError(
            f"Prowlarr ARR applications must not carry the {SHELFARR_PROWLARR_TAG} tag: "
            + ", ".join(sorted(shelfarr_conflicts))
        )
    if missing_arr:
        raise BootstrapError(
            f"Prowlarr ARR applications are missing the {ARR_PROWLARR_TAG} tag: "
            + ", ".join(sorted(missing_arr))
        )
    return required


def _put_and_verify_tags(
    client: Any,
    endpoint: str,
    resource: Mapping[str, Any],
    desired_tags: set[int],
) -> dict[str, Any]:
    resource_id = resource.get("id")
    if isinstance(resource_id, bool) or not isinstance(resource_id, int):
        raise BootstrapError(f"Prowlarr {endpoint.rsplit('/', 1)[-1]} has no numeric id")
    desired = copy.deepcopy(dict(resource))
    desired["tags"] = sorted(desired_tags)
    saved = client.put_json(
        f"{endpoint}/{resource_id}?forceSave=true",
        desired,
    )
    saved_id = saved.get("id") if isinstance(saved, dict) else None
    if saved_id != resource_id:
        raise BootstrapError("Prowlarr did not persist its tag boundary")
    persisted = client.get_json(f"{endpoint}/{resource_id}")
    if not isinstance(persisted, dict) or _prowlarr_tag_ids(persisted) != desired_tags:
        raise BootstrapError("Prowlarr did not persist its tag boundary")
    return persisted


def _disable_managed_newznab(client: Any, installed: Mapping[str, Any]) -> dict[str, Any]:
    if not bool(installed.get("enable")):
        return copy.deepcopy(dict(installed))
    resource_id = installed.get("id")
    if isinstance(resource_id, bool) or not isinstance(resource_id, int):
        raise BootstrapError(
            f"Prowlarr {installed.get('name', 'managed')} indexer has no numeric id"
        )
    disabled = copy.deepcopy(dict(installed))
    disabled["enable"] = False
    saved = client.put_json(
        f"/api/v1/indexer/{resource_id}?forceSave=true", disabled
    )
    saved_id = saved.get("id") if isinstance(saved, dict) else None
    persisted = client.get_json(f"/api/v1/indexer/{resource_id}")
    if (
        saved_id != resource_id
        or not isinstance(persisted, dict)
        or bool(persisted.get("enable"))
    ):
        raise BootstrapError("Prowlarr did not disable its managed Newznab indexer")
    return persisted


def _assert_no_orphan_shelfarr_indexers(
    indexers: Sequence[Mapping[str, Any]],
    *,
    managed_name: str,
    shelfarr_tag_id: int | None,
) -> None:
    """Refuse to reclassify a previously managed book indexer as ARR-owned."""

    if shelfarr_tag_id is None:
        return
    orphans = sorted(
        str(indexer.get("name") or "unnamed")
        for indexer in indexers
        if indexer.get("name") != managed_name
        and shelfarr_tag_id in _prowlarr_tag_ids(indexer)
    )
    if orphans:
        raise BootstrapError(
            f"Prowlarr has unmanaged indexers carrying the {SHELFARR_PROWLARR_TAG} tag: "
            + ", ".join(orphans)
        )


def converge_prowlarr_arr_isolation(
    client: Any,
    *,
    managed_name: str,
    shelfarr_tag_id: int,
    arr_tag_id: int,
    indexers: list[dict[str, Any]],
    applications: Any,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Preserve the ARRs' prior all-indexer set while excluding Shelfarr's NZB."""

    # Prowlarr intentionally treats an application with no tags as matching
    # every valid indexer.  Tagging only the Newznab resource would therefore
    # still distribute it to all four ARRs.  First tag every indexer in their
    # existing shared set, then tag the applications, and only afterward let
    # the separately tagged managed Newznab become enabled.
    required = _required_arr_applications(applications)
    shelfarr_conflicts = [
        name
        for name, application in required.items()
        if shelfarr_tag_id in _prowlarr_tag_ids(application)
    ]
    if shelfarr_conflicts:
        raise BootstrapError(
            f"Prowlarr ARR applications must not carry the {SHELFARR_PROWLARR_TAG} tag: "
            + ", ".join(sorted(shelfarr_conflicts))
        )

    converged_indexers: list[dict[str, Any]] = []
    for indexer in indexers:
        if indexer.get("name") == managed_name:
            converged_indexers.append(indexer)
            continue
        desired_tags = _prowlarr_tag_ids(indexer) | {arr_tag_id}
        if _prowlarr_tag_ids(indexer) != desired_tags:
            indexer = _put_and_verify_tags(
                client, "/api/v1/indexer", indexer, desired_tags
            )
        converged_indexers.append(indexer)

    converged_applications: dict[str, dict[str, Any]] = {}
    for name, application in required.items():
        desired_tags = _prowlarr_tag_ids(application) | {arr_tag_id}
        if _prowlarr_tag_ids(application) != desired_tags:
            application = _put_and_verify_tags(
                client, "/api/v1/applications", application, desired_tags
            )
        converged_applications[name] = application

    assert_arr_application_tag_boundary(
        list(converged_applications.values()),
        arr_tag_id=arr_tag_id,
        shelfarr_tag_id=shelfarr_tag_id,
    )
    return converged_indexers, converged_applications


def _generic_newznab_resource(resource: Mapping[str, Any]) -> bool:
    return bool(
        str(resource.get("implementation", "")).casefold() == "newznab"
        and str(resource.get("configContract", "")).casefold()
        == "newznabsettings"
        and str(resource.get("protocol", "")).casefold() == "usenet"
    )


def _newznab_managed_state(resource: Mapping[str, Any]) -> tuple[Any, ...]:
    base_url = str(get_provider_field(resource, "baseUrl") or "").rstrip("/")
    return (
        resource.get("name"),
        bool(resource.get("enable")),
        resource.get("appProfileId"),
        resource.get("priority"),
        tuple(sorted(_prowlarr_tag_ids(resource))),
        base_url,
        get_provider_field(resource, "apiPath"),
        bool(str(get_provider_field(resource, "apiKey") or "").strip()),
    )


def _indexer_category_ids(resource: Mapping[str, Any]) -> set[int]:
    capabilities = resource.get("capabilities")
    categories = capabilities.get("categories") if isinstance(capabilities, dict) else []
    if not isinstance(categories, list):
        return set()
    result: set[int] = set()
    pending = list(categories)
    while pending:
        category = pending.pop()
        if not isinstance(category, dict):
            continue
        raw_id = category.get("id")
        if not isinstance(raw_id, bool):
            try:
                result.add(int(raw_id))
            except (TypeError, ValueError):
                pass
        children = category.get("subCategories", [])
        if isinstance(children, list):
            pending.extend(children)
    return result


def _newznab_supports_pilot_books(resource: Mapping[str, Any]) -> bool:
    category_ids = _indexer_category_ids(resource)
    return 3030 in category_ids and bool({7000, 7020} & category_ids)


def _newznab_payload_needs_update(
    current: Mapping[str, Any],
    desired: Mapping[str, Any],
    *,
    current_test_ok: bool,
) -> bool:
    if not current_test_ok:
        return True
    if not _newznab_supports_pilot_books(current):
        return True
    if _newznab_managed_state(current) != _newznab_managed_state(desired):
        return True
    current_api_key = get_provider_field(current, "apiKey")
    desired_api_key = get_provider_field(desired, "apiKey")
    return bool(
        current_api_key not in MASKED_SECRET_VALUES
        and current_api_key != desired_api_key
    )


def configure_managed_newznab(
    client: Any,
    config: ManagedNewznabConfig,
    *,
    secret_values: Iterable[str] = (),
) -> str:
    """Converge one book-only Generic Newznab without syncing it to the ARRs."""

    if config.name != DEFAULT_NEWZNAB_INDEXER_NAME:
        raise BootstrapError(
            f"NEWZNAB_INDEXER_NAME must be exactly {DEFAULT_NEWZNAB_INDEXER_NAME}"
        )

    existing_raw = client.get_json("/api/v1/indexer")
    if not isinstance(existing_raw, list):
        raise BootstrapError("Prowlarr returned invalid indexer data")
    existing = [item for item in existing_raw if isinstance(item, dict)]
    exact = [
        item
        for item in existing
        if item.get("name") == config.name
    ]
    if len(exact) > 1:
        raise BootstrapError(f"Prowlarr has duplicate indexers named {config.name}")
    installed = exact[0] if exact else None
    casefold_collisions = [
        item
        for item in existing
        if str(item.get("name", "")).casefold() == config.name.casefold()
        and item is not installed
    ]
    if casefold_collisions:
        raise BootstrapError(f"Prowlarr has a conflicting indexer named {config.name}")
    if installed is not None and not _generic_newznab_resource(installed):
        raise BootstrapError(
            f"Prowlarr indexer name {config.name} is not a Generic Newznab resource"
        )

    if not config.enabled:
        action = "disabled" if installed is not None else "absent"
        if installed is not None:
            installed = _disable_managed_newznab(client, installed)
            existing = [
                installed if item.get("name") == config.name else item
                for item in existing
            ]

        shelfarr_tag_id = _existing_prowlarr_tag(
            client, SHELFARR_PROWLARR_TAG
        )
        arr_tag_id = _existing_prowlarr_tag(client, ARR_PROWLARR_TAG)
        if shelfarr_tag_id is None and arr_tag_id is None:
            return action
        if (
            shelfarr_tag_id is None
            or arr_tag_id is None
            or shelfarr_tag_id == arr_tag_id
        ):
            raise BootstrapError(
                "Prowlarr has a partial or conflicting retained isolation topology"
            )
        _assert_no_orphan_shelfarr_indexers(
            existing,
            managed_name=config.name,
            shelfarr_tag_id=shelfarr_tag_id,
        )
        applications = client.get_json("/api/v1/applications")
        converge_prowlarr_arr_isolation(
            client,
            managed_name=config.name,
            shelfarr_tag_id=shelfarr_tag_id,
            arr_tag_id=arr_tag_id,
            indexers=existing,
            applications=applications,
        )
        return action

    try:
        existing_shelfarr_tag_id = _existing_prowlarr_tag(
            client, SHELFARR_PROWLARR_TAG
        )
        existing_arr_tag_id = _existing_prowlarr_tag(client, ARR_PROWLARR_TAG)
        _assert_no_orphan_shelfarr_indexers(
            existing,
            managed_name=config.name,
            shelfarr_tag_id=existing_shelfarr_tag_id,
        )
        applications = client.get_json("/api/v1/applications")
        required_applications = _required_arr_applications(applications)
        application_tag_ids = set().union(
            *(
                _prowlarr_tag_ids(application)
                for application in required_applications.values()
            )
        )
        topology_ready = bool(
            existing_shelfarr_tag_id is not None
            and existing_arr_tag_id is not None
            and existing_shelfarr_tag_id != existing_arr_tag_id
            and all(
                existing_arr_tag_id in _prowlarr_tag_ids(application)
                and existing_shelfarr_tag_id
                not in _prowlarr_tag_ids(application)
                for application in required_applications.values()
            )
            and all(
                indexer.get("name") == config.name
                or existing_arr_tag_id in _prowlarr_tag_ids(indexer)
                for indexer in existing
            )
            and (
                installed is None
                or (
                    existing_shelfarr_tag_id in _prowlarr_tag_ids(installed)
                    and not (
                        _prowlarr_tag_ids(installed) & application_tag_ids
                    )
                )
            )
        )
    except BootstrapError:
        if installed is not None:
            installed = _disable_managed_newznab(client, installed)
        raise
    if installed is not None and bool(installed.get("enable")) and not topology_ready:
        installed = _disable_managed_newznab(client, installed)
        existing = [
            installed if item.get("name") == config.name else item
            for item in existing
        ]

    shelfarr_tag_id = ensure_prowlarr_tag(client, SHELFARR_PROWLARR_TAG)
    arr_tag_id = ensure_prowlarr_tag(client, ARR_PROWLARR_TAG)
    if shelfarr_tag_id == arr_tag_id:
        raise BootstrapError("Prowlarr returned conflicting isolation tag IDs")

    existing, converged_applications = converge_prowlarr_arr_isolation(
        client,
        managed_name=config.name,
        shelfarr_tag_id=shelfarr_tag_id,
        arr_tag_id=arr_tag_id,
        indexers=existing,
        applications=applications,
    )
    installed = next(
        (item for item in existing if item.get("name") == config.name), None
    )

    schemas_raw = client.get_json("/api/v1/indexer/schema")
    profiles = client.get_json("/api/v1/appprofile")
    if not isinstance(schemas_raw, list):
        raise BootstrapError("Prowlarr returned invalid indexer schemas")
    if not isinstance(profiles, list) or not profiles:
        raise BootstrapError("Prowlarr has no application profile")
    profile_id = profiles[0].get("id")
    if isinstance(profile_id, bool) or not isinstance(profile_id, int):
        raise BootstrapError("Prowlarr application profile has no numeric id")

    generic_schemas = [
        item
        for item in flatten_indexer_schemas(schemas_raw)
        if item.get("name") == GENERIC_NEWZNAB_SCHEMA_NAME
        and _generic_newznab_resource(item)
    ]
    if len(generic_schemas) != 1:
        raise BootstrapError("Prowlarr has no unique Generic Newznab schema")

    source = installed or generic_schemas[0]
    desired = build_indexer_payload(
        source,
        IndexerSpec(config.name, "Newznab", config.base_url),
        profile_id,
    )
    managed_tags = (
        _prowlarr_tag_ids(installed) if installed is not None else set()
    )
    arr_application_tags = set().union(
        *(
            _prowlarr_tag_ids(application)
            for application in converged_applications.values()
        )
    )
    managed_tags.difference_update(arr_application_tags)
    managed_tags.add(shelfarr_tag_id)
    desired["tags"] = sorted(managed_tags)
    set_provider_field(desired, "apiPath", config.api_path)
    set_provider_field(desired, "apiKey", config.api_key)

    current_test_ok = False
    if installed is not None:
        try:
            client.post_json("/api/v1/indexer/test", installed, retry=True)
            current_test_ok = True
        except (ApiError, ApiTransportError):
            pass
        if not _newznab_payload_needs_update(
            installed, desired, current_test_ok=current_test_ok
        ):
            return "verified"

    try:
        client.post_json("/api/v1/indexer/test", desired, retry=True)
    except (ApiError, ApiTransportError) as exc:
        raise BootstrapError(
            f"Prowlarr {config.name} test failed: "
            f"{safe_api_error_detail(exc, secret_values)}"
        ) from exc

    if installed is None:
        saved = client.post_json(
            "/api/v1/indexer?forceSave=true", desired, retry=False
        )
        action = "created"
    else:
        resource_id = installed.get("id")
        if isinstance(resource_id, bool) or not isinstance(resource_id, int):
            raise BootstrapError(f"Prowlarr {config.name} indexer has no numeric id")
        saved = client.put_json(
            f"/api/v1/indexer/{resource_id}?forceSave=true", desired
        )
        action = "updated"
    saved_id = saved.get("id") if isinstance(saved, dict) else None
    if isinstance(saved_id, bool) or not isinstance(saved_id, int):
        raise BootstrapError(f"Prowlarr did not persist {config.name}")
    persisted = client.get_json(f"/api/v1/indexer/{saved_id}")
    if not (
        isinstance(persisted, dict)
        and _generic_newznab_resource(persisted)
        and _newznab_managed_state(persisted) == _newznab_managed_state(desired)
        and _newznab_supports_pilot_books(persisted)
    ):
        raise BootstrapError(
            f"Prowlarr {config.name} does not advertise ebook and audiobook categories"
        )
    try:
        client.post_json("/api/v1/indexer/test", persisted, retry=True)
    except (ApiError, ApiTransportError) as exc:
        raise BootstrapError(
            f"Prowlarr persisted {config.name} but its test failed: "
            f"{safe_api_error_detail(exc, secret_values)}"
        ) from exc
    return action


def _collect_error_strings(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in {"errormessage", "message"} and isinstance(
                child, str
            ):
                result.append(child)
            else:
                result.extend(_collect_error_strings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_collect_error_strings(child))
    return result


def safe_api_error_detail(error: Exception, secret_values: Iterable[str] = ()) -> str:
    detail = str(error)
    if isinstance(error, ApiError) and error.body:
        try:
            parsed = json.loads(error.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        messages = _collect_error_strings(parsed)
        if messages:
            detail = messages[0]
    detail = " ".join(detail.split())
    for secret_value in secret_values:
        if secret_value:
            detail = detail.replace(secret_value, "[redacted]")
    detail = re.sub(
        r"(?i)((?:api[ _-]?key|password|token)\s*[:=]\s*)\S+",
        r"\1[redacted]",
        detail,
    )
    return detail[:300]


def configure_prowlarr_indexers(
    client: Any,
    *,
    reporter: Reporter | Any | None = None,
    secret_values: Iterable[str] = (),
    mutation_pause_seconds: float = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    reporter = reporter or Reporter()
    schemas_raw = client.get_json("/api/v1/indexer/schema")
    existing_raw = client.get_json("/api/v1/indexer")
    profiles = client.get_json("/api/v1/appprofile")
    if not isinstance(schemas_raw, list) or not isinstance(existing_raw, list):
        raise BootstrapError("Prowlarr returned invalid indexer data")
    if not isinstance(profiles, list) or not profiles:
        raise BootstrapError("Prowlarr has no application profile")
    profile_id = profiles[0].get("id")
    if not isinstance(profile_id, int):
        raise BootstrapError("Prowlarr application profile has no numeric id")

    schemas = flatten_indexer_schemas(schemas_raw)
    successful: list[str] = []
    existing = [item for item in existing_raw if isinstance(item, dict)]
    for spec in PUBLIC_INDEXERS:
        installed = next(
            (item for item in existing if indexer_matches(item, spec)), None
        )
        schema = next(
            (item for item in schemas if indexer_matches(item, spec)), None
        )
        source = installed or schema
        if source is None:
            reporter.warning(
                f"Prowlarr has no schema for {spec.name}; indexer skipped"
            )
            continue
        try:
            desired = build_indexer_payload(source, spec, profile_id)
            client.post_json("/api/v1/indexer/test", desired, retry=True)
            changed = False
            if installed is None:
                client.post_json(
                    "/api/v1/indexer?forceSave=true", desired, retry=False
                )
                changed = True
            elif _indexer_managed_state(installed) != _indexer_managed_state(
                desired
            ):
                resource_id = installed.get("id")
                if not isinstance(resource_id, int):
                    raise BootstrapError(
                        f"Prowlarr {spec.name} indexer has no numeric id"
                    )
                client.put_json(
                    f"/api/v1/indexer/{resource_id}?forceSave=true", desired
                )
                changed = True
            if changed and mutation_pause_seconds > 0:
                # Prowlarr immediately fans mutations out to every ARR. Pace
                # them so downstream live tests do not trip public-indexer or
                # local Torznab rate limits during bootstrap.
                sleep(mutation_pause_seconds)
        except (ApiError, ApiTransportError) as exc:
            reporter.warning(
                f"{spec.name} test failed; indexer skipped: "
                f"{safe_api_error_detail(exc, secret_values)}"
            )
            continue
        successful.append(spec.name)
    if not successful:
        raise BootstrapError(
            "No selected public Prowlarr indexer passed its connection test"
        )
    return successful


def configure_prowlarr(
    environment: Mapping[str, str],
    api_key: str,
    *,
    timeout: float,
    retries: int,
    reporter: Reporter,
    secret_values: Iterable[str],
    client_factory: Callable[..., Any] = ApiClient,
) -> list[str]:
    newznab = managed_newznab_config(environment)
    try:
        port = int(environment.get("PROWLARR_PORT", 9696))
    except ValueError as exc:
        raise BootstrapError("PROWLARR_PORT must be numeric") from exc
    client = client_factory(
        f"http://{environment.get('WYSEARR_BIND_ADDRESS', '192.168.4.86')}:{port}",
        headers={"X-Api-Key": api_key},
        timeout=timeout,
        retries=retries,
    )
    validate_prowlarr_applications(client)
    successful = configure_prowlarr_indexers(
        client,
        reporter=reporter,
        secret_values=secret_values,
        mutation_pause_seconds=15,
    )
    configure_managed_newznab(
        client,
        newznab,
        secret_values=secret_values,
    )
    # Adding/updating an indexer initiates application synchronization.  A
    # second reachability pass makes sync failure a bootstrap failure instead
    # of allowing a locally valid but undistributed indexer configuration.
    validate_prowlarr_applications(client)
    return successful


def read_yaml_scalar(path: Path, section: str, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BootstrapError(f"Unable to read {path.parent.name} configuration") from exc
    in_section = False
    section_pattern = re.compile(rf"^{re.escape(section)}\s*:\s*$")
    key_pattern = re.compile(rf"^\s+{re.escape(key)}\s*:\s*(.*?)\s*$")
    for line in lines:
        if line and not line[0].isspace():
            in_section = bool(section_pattern.match(line))
            continue
        if in_section:
            match = key_pattern.match(line)
            if match:
                raw = match.group(1)
                if raw[:1] in {"'", '"'}:
                    try:
                        value = ast.literal_eval(raw)
                    except (ValueError, SyntaxError) as exc:
                        raise BootstrapError(
                            f"Malformed {section}.{key} in Bazarr configuration"
                        ) from exc
                    return str(value)
                return raw.split(" #", 1)[0].strip()
    raise BootstrapError(f"Bazarr configuration is missing {section}.{key}")


def _nested_setting(settings: Mapping[str, Any], section: str, key: str) -> Any:
    section_value = next(
        (
            value
            for candidate, value in settings.items()
            if str(candidate).casefold() == section.casefold()
        ),
        {},
    )
    if not isinstance(section_value, Mapping):
        return None
    return next(
        (
            value
            for candidate, value in section_value.items()
            if str(candidate).casefold() == key.casefold()
        ),
        None,
    )


def _form_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _settings_equal(current: Any, desired: Any) -> bool:
    if isinstance(desired, bool):
        if isinstance(current, str):
            return current.casefold() == str(desired).casefold()
        return current is desired
    if isinstance(desired, int):
        try:
            return int(current) == desired
        except (TypeError, ValueError):
            return False
    return str(current) == str(desired)


def english_language_profile(profile_id: int) -> dict[str, Any]:
    item_id = 1
    return {
        "profileId": profile_id,
        "name": "English",
        "cutoff": item_id,
        "items": [
            {
                "id": item_id,
                "language": "en",
                "hi": "False",
                "forced": "False",
                "audio_exclude": "False",
                "audio_only_include": "False",
            }
        ],
        "mustContain": [],
        "mustNotContain": [],
        "originalFormat": 0,
        "tag": None,
    }


def build_bazarr_settings_form(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    languages: Sequence[Mapping[str, Any]],
    sonarr_api_key: str,
    radarr_api_key: str,
) -> tuple[dict[str, Any], int]:
    form: dict[str, Any] = {}
    mutable_profiles = [copy.deepcopy(dict(profile)) for profile in profiles]
    english_profile = next(
        (
            profile
            for profile in mutable_profiles
            if str(profile.get("name", "")).casefold() == "english"
        ),
        None,
    )
    if english_profile is None:
        used_ids = [
            profile.get("profileId")
            for profile in mutable_profiles
            if isinstance(profile.get("profileId"), int)
        ]
        english_profile = english_language_profile(max(used_ids, default=0) + 1)
        mutable_profiles.append(english_profile)
        form["languages-profiles"] = json.dumps(
            mutable_profiles, separators=(",", ":")
        )
    profile_id = english_profile.get("profileId")
    if not isinstance(profile_id, int):
        raise BootstrapError("Bazarr English language profile has no numeric id")

    enabled_languages = [
        str(language.get("code2"))
        for language in languages
        if language.get("enabled") is True and language.get("code2")
    ]
    if "en" not in enabled_languages:
        form["languages-enabled"] = [*enabled_languages, "en"]

    desired_settings = (
        ("general", "use_sonarr", "settings-general-use_sonarr", True),
        ("sonarr", "ip", "settings-sonarr-ip", "sonarr"),
        ("sonarr", "port", "settings-sonarr-port", 8989),
        ("sonarr", "base_url", "settings-sonarr-base_url", "/"),
        ("sonarr", "ssl", "settings-sonarr-ssl", False),
        ("sonarr", "apikey", "settings-sonarr-apikey", sonarr_api_key),
        ("general", "use_radarr", "settings-general-use_radarr", True),
        ("radarr", "ip", "settings-radarr-ip", "radarr"),
        ("radarr", "port", "settings-radarr-port", 7878),
        ("radarr", "base_url", "settings-radarr-base_url", "/"),
        ("radarr", "ssl", "settings-radarr-ssl", False),
        ("radarr", "apikey", "settings-radarr-apikey", radarr_api_key),
        (
            "general",
            "serie_default_enabled",
            "settings-general-serie_default_enabled",
            True,
        ),
        (
            "general",
            "serie_default_profile",
            "settings-general-serie_default_profile",
            profile_id,
        ),
        (
            "general",
            "movie_default_enabled",
            "settings-general-movie_default_enabled",
            True,
        ),
        (
            "general",
            "movie_default_profile",
            "settings-general-movie_default_profile",
            profile_id,
        ),
        ("subf2m", "user_agent", "settings-subf2m-user_agent", BAZARR_USER_AGENT),
        ("subf2m", "verify_ssl", "settings-subf2m-verify_ssl", True),
    )
    for section, key, form_key, desired in desired_settings:
        if not _settings_equal(_nested_setting(settings, section, key), desired):
            form[form_key] = _form_value(desired)

    current_providers = _nested_setting(settings, "general", "enabled_providers")
    if not isinstance(current_providers, list):
        current_providers = []
    desired_providers = list(dict.fromkeys([*current_providers, *BAZARR_PROVIDERS]))
    if current_providers != desired_providers:
        form["settings-general-enabled_providers"] = desired_providers
    return form, profile_id


def configure_bazarr(
    root: Path,
    environment: Mapping[str, str],
    sonarr_api_key: str,
    radarr_api_key: str,
    *,
    timeout: float,
    retries: int,
    client_factory: Callable[..., Any] = ApiClient,
) -> bool:
    bazarr_api_key = read_yaml_scalar(
        root / "config" / "bazarr" / "config" / "config.yaml", "auth", "apikey"
    )
    if not bazarr_api_key:
        raise BootstrapError("Bazarr API key is empty")
    try:
        port = int(environment.get("BAZARR_PORT", 6767))
    except ValueError as exc:
        raise BootstrapError("BAZARR_PORT must be numeric") from exc
    client = client_factory(
        f"http://{environment.get('WYSEARR_BIND_ADDRESS', '192.168.4.86')}:{port}",
        headers={"X-API-KEY": bazarr_api_key},
        timeout=timeout,
        retries=retries,
    )
    settings = client.get_json("/api/system/settings")
    profiles = client.get_json("/api/system/languages/profiles")
    languages = client.get_json("/api/system/languages")
    if not isinstance(settings, dict):
        raise BootstrapError("Bazarr returned invalid settings")
    if not isinstance(profiles, list) or not isinstance(languages, list):
        raise BootstrapError("Bazarr returned invalid language configuration")
    form, _ = build_bazarr_settings_form(
        settings, profiles, languages, sonarr_api_key, radarr_api_key
    )
    if form:
        client.post_form("/api/system/settings", form)

    status = client.get_json("/api/system/status")
    data = status.get("data", {}) if isinstance(status, dict) else {}
    if not data.get("sonarr_version") or not data.get("radarr_version"):
        raise BootstrapError(
            "Bazarr cannot confirm its Sonarr and Radarr integrations"
        )
    return bool(form)


def bootstrap(
    root: Path,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    reporter: Reporter | None = None,
) -> None:
    reporter = reporter or Reporter()
    environment, api_keys = prepare_environment(root)
    reporter.info("Persisted private service credentials in .env (mode 0600).")

    directories = ensure_download_directories(root, environment)
    reporter.info(f"Verified {len(directories)} download directories.")

    qbit_port = environment.get("QBITTORRENT_PORT", "8080")
    try:
        int(qbit_port)
    except ValueError as exc:
        raise BootstrapError("QBITTORRENT_PORT must be numeric") from exc
    qbit_username = environment["QBITTORRENT_USERNAME"]
    qbit_password = environment["QBITTORRENT_PASSWORD"]
    bind_address = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    qbit_base_url = f"http://{bind_address}:{qbit_port}"
    qbit = authenticate_qbittorrent(
        qbit_base_url,
        qbit_username,
        qbit_password,
        timeout=timeout,
        retries=retries,
    )
    preference_changes, category_changes = configure_qbittorrent(qbit)
    reporter.info(
        "qBittorrent verified "
        f"({preference_changes} preference and {category_changes} category updates)."
    )

    qbit = restart_qbittorrent_with_rotation_guard(
        qbit,
        qbit_base_url,
        qbit_username,
        qbit_password,
        timeout=timeout,
        retries=retries,
    )
    try:
        arr_updates = configure_arr_services(
            environment,
            api_keys,
            qbit_username,
            qbit_password,
            timeout=timeout,
            retries=retries,
        )
    finally:
        qbit.set_preferences(
            {"web_ui_max_auth_fail_count": QBITTORRENT_AUTH_FAILURE_LIMIT}
        )
    reporter.info(
        f"ARR qBittorrent integrations verified ({arr_updates} repaired)."
    )

    all_secrets = [
        qbit_password,
        *api_keys.values(),
        environment.get("NEWZNAB_API_KEY", ""),
    ]
    indexers = configure_prowlarr(
        environment,
        api_keys["PROWLARR_API_KEY"],
        timeout=timeout,
        retries=retries,
        reporter=reporter,
        secret_values=all_secrets,
    )
    reporter.info(
        f"Prowlarr applications and {len(indexers)} public indexer(s) verified."
    )

    bazarr_changed = configure_bazarr(
        root,
        environment,
        api_keys["SONARR_API_KEY"],
        api_keys["RADARR_API_KEY"],
        timeout=timeout,
        retries=retries,
    )
    reporter.info(
        "Bazarr Sonarr/Radarr, English profile, and providers verified"
        + (" (updated)." if bazarr_changed else ".")
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="Project root (normally auto-detected from this script)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    reporter = Reporter()
    try:
        root = arguments.root.resolve() if arguments.root else detect_project_root()
        bootstrap(
            root,
            timeout=max(1.0, arguments.timeout),
            retries=max(1, arguments.retries),
            reporter=reporter,
        )
    except (BootstrapError, ApiError) as exc:
        reporter.error(str(exc))
        return 1
    reporter.info("Bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
