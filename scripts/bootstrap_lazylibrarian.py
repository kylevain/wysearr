#!/usr/bin/env python3
"""Converge the private, ebook-only LazyLibrarian integration.

The offline mode writes only private local configuration.  The normal mode
validates the pinned LazyLibrarian API, qBittorrent handoff, and Prowlarr's
managed ebook-only application/indexers.  It never adds a book, starts a
search, or mutates a torrent.
"""

from __future__ import annotations

import argparse
import configparser
import copy
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .bootstrap import (
        ApiClient,
        ApiError,
        ApiTransportError,
        BootstrapError,
        MASKED_SECRET_VALUES,
        QbittorrentClient,
        ensure_prowlarr_tag,
        get_provider_field,
        load_dotenv,
        set_provider_field,
        update_dotenv,
    )
except ImportError:
    from bootstrap import (
        ApiClient,
        ApiError,
        ApiTransportError,
        BootstrapError,
        MASKED_SECRET_VALUES,
        QbittorrentClient,
        ensure_prowlarr_tag,
        get_provider_field,
        load_dotenv,
        set_provider_field,
        update_dotenv,
    )


STACK_ROOT = Path(__file__).resolve().parents[1]
MAX_PRIVATE_FILE_BYTES = 1024 * 1024
EXPECTED_LAZYLIBRARIAN_VERSION = "02af0464"
MANAGED_APPLICATION_NAME = "LazyLibrarian"
MANAGED_PROWLARR_TAG = "lazylibrarian-ebooks"
REQUIRED_EBOOK_CATEGORY = 7020
MANAGED_SYNC_CATEGORIES = (REQUIRED_EBOOK_CATEGORY,)
REQUIRED_API_COMMANDS = (
    "findBook",
    "findAuthor",
    "addBook",
    "getAllBooks",
    "queueBook",
    "searchBook",
    "getHistory",
    "getVersion",
    "readCFG",
    "listNabProviders",
    "listProviders",
    "changeProvider",
    "addProvider",
    "delProvider",
)
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)
API_KEY_RE = re.compile(r"[0-9a-f]{32}")


# LazyLibrarian is deliberately a search/acquisition coordinator.  BookBot is
# the only ebook postprocessor/importer, so every autonomous scheduler and
# postprocessing/delete path that could race it is disabled here.
CONFIG_VALUES: dict[str, dict[str, str]] = {
    "GENERAL": {
        "audio_tab": "0",
        "ebook_tab": "1",
        "launch_browser": "0",
        "ebook_type": "epub, mobi, azw3, pdf",
        "imp_preflang": "eng, en-US, en, English, en-GB",
        "imp_autoadd": "",
        "imp_autosearch": "0",
        "destination_copy": "0",
        "ebook_dir": "",
        "audio_dir": "",
        "download_dir": "/downloads",
    },
    "API": {
        "api_enabled": "1",
        "book_api": "OpenLibrary",
    },
    "LOGGING": {
        "loglevel": "20",
        "logredact": "1",
        "hostredact": "1",
        "logfileredact": "1",
        "redact_params": "apikey, api_key, prov_apikey, qbittorrent_pass",
    },
    "GIT": {"auto_update": "0"},
    "TELEMETRY": {
        "telemetry_enable": "0",
        "telemetry_send_config": "0",
        "telemetry_send_usage": "0",
        "telemetry_interval": "0",
    },
    "SEARCHSCAN": {
        "search_bookinterval": "0",
        "scan_interval": "0",
        "search_maginterval": "0",
        "searchrss_interval": "0",
        "wishlist_interval": "0",
        "search_comicinterval": "0",
        "versioncheck_interval": "0",
        "goodreads_interval": "0",
        "hardcover_interval": "0",
        "delaysearch": "0",
    },
    "LIBRARYSCAN": {
        "newbook_status": "Skipped",
        "newaudio_status": "Ignored",
        "newauthor_status": "Skipped",
        "newauthor_audio": "Ignored",
        "newauthor_books": "0",
    },
    "COMICS": {"comic_tab": "0"},
    "MAGAZINES": {"mag_tab": "0"},
    "USENET": {
        "nzb_downloader_sabnzbd": "0",
        "nzb_downloader_nzbget": "0",
        "nzb_downloader_synology": "0",
        "nzb_downloader_blackhole": "0",
    },
    "TORRENT": {
        "tor_downloader_blackhole": "0",
        "tor_downloader_utorrent": "0",
        "tor_downloader_rtorrent": "0",
        "tor_downloader_qbittorrent": "1",
        "tor_downloader_transmission": "0",
        "tor_downloader_synology": "0",
        "tor_downloader_deluge": "0",
        "torrent_paused": "0",
        "keep_seeding": "1",
        "seed_wait": "1",
        "prefer_magnet": "1",
    },
    "QBITTORRENT": {
        "qbittorrent_host": "qbittorrent",
        "qbittorrent_port": "8080",
        "qbittorrent_base": "",
        "qbittorrent_label": "ebooks",
        "qbittorrent_dir": "/downloads/ebooks",
        "qbittorrent_remote": "",
        "qbittorrent_local": "",
        "qbittorrent_ignore_ssl": "0",
    },
    "POSTPROCESS": {
        "del_downloadfailed": "0",
        "del_failed": "0",
        "del_completed": "0",
    },
    # Built-in torrent/direct sources remain off.  Prowlarr is the only source
    # of acquisition providers in this deployment.
    "KAT": {"kat": "0"},
    "TPB": {"tpb": "0"},
    "LIME": {"lime": "0"},
    "TDL": {"tdl": "0"},
    "BOK": {"bok": "0"},
}


# ``readCFG`` returns each value through its config type's ``get_str`` method.
# In the pinned LazyLibrarian build that means booleans are represented as
# ``"1"``/``""`` and integer-backed schedulers as decimal strings.  Keep the
# type map explicit so an empty boolean cannot accidentally satisfy an integer
# setting whose required value is zero.
BOOLEAN_CONFIG_KEYS = frozenset(
    {
        ("GENERAL", "audio_tab"),
        ("GENERAL", "ebook_tab"),
        ("GENERAL", "launch_browser"),
        ("GENERAL", "imp_autosearch"),
        ("GENERAL", "destination_copy"),
        ("API", "api_enabled"),
        ("LOGGING", "logredact"),
        ("LOGGING", "hostredact"),
        ("LOGGING", "logfileredact"),
        ("GIT", "auto_update"),
        ("TELEMETRY", "telemetry_enable"),
        ("TELEMETRY", "telemetry_send_config"),
        ("TELEMETRY", "telemetry_send_usage"),
        ("SEARCHSCAN", "delaysearch"),
        ("LIBRARYSCAN", "newauthor_books"),
        ("COMICS", "comic_tab"),
        ("MAGAZINES", "mag_tab"),
        ("USENET", "nzb_downloader_sabnzbd"),
        ("USENET", "nzb_downloader_nzbget"),
        ("USENET", "nzb_downloader_synology"),
        ("USENET", "nzb_downloader_blackhole"),
        ("TORRENT", "tor_downloader_blackhole"),
        ("TORRENT", "tor_downloader_utorrent"),
        ("TORRENT", "tor_downloader_rtorrent"),
        ("TORRENT", "tor_downloader_qbittorrent"),
        ("TORRENT", "tor_downloader_transmission"),
        ("TORRENT", "tor_downloader_synology"),
        ("TORRENT", "tor_downloader_deluge"),
        ("TORRENT", "torrent_paused"),
        ("TORRENT", "keep_seeding"),
        ("TORRENT", "seed_wait"),
        ("TORRENT", "prefer_magnet"),
        ("QBITTORRENT", "qbittorrent_ignore_ssl"),
        ("POSTPROCESS", "del_downloadfailed"),
        ("POSTPROCESS", "del_failed"),
        ("POSTPROCESS", "del_completed"),
        ("KAT", "kat"),
        ("TPB", "tpb"),
        ("LIME", "lime"),
        ("TDL", "tdl"),
        ("BOK", "bok"),
    }
)

INTEGER_CONFIG_KEYS = frozenset(
    {
        ("LOGGING", "loglevel"),
        ("TELEMETRY", "telemetry_interval"),
        ("SEARCHSCAN", "search_bookinterval"),
        ("SEARCHSCAN", "scan_interval"),
        ("SEARCHSCAN", "search_maginterval"),
        ("SEARCHSCAN", "searchrss_interval"),
        ("SEARCHSCAN", "wishlist_interval"),
        ("SEARCHSCAN", "search_comicinterval"),
        ("SEARCHSCAN", "versioncheck_interval"),
        ("SEARCHSCAN", "goodreads_interval"),
        ("SEARCHSCAN", "hardcover_interval"),
        ("QBITTORRENT", "qbittorrent_port"),
    }
)

CSV_CONFIG_KEYS = frozenset(
    {
        ("GENERAL", "ebook_type"),
        ("GENERAL", "imp_preflang"),
        ("LOGGING", "redact_params"),
    }
)


def _read_bounded_regular(path: Path, label: str) -> str:
    """Read a bounded regular file without following its final component."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError(f"Unable to read private {label}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PRIVATE_FILE_BYTES:
            raise BootstrapError(f"Private {label} is unsafe or too large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read(MAX_PRIVATE_FILE_BYTES + 1)
    except UnicodeError as exc:
        raise BootstrapError(f"Private {label} is not valid UTF-8") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_strict_environment(path: Path) -> dict[str, str]:
    """Reject ambiguous dotenv input before using the shared parser."""

    if not path.exists():
        raise BootstrapError("Private .env is missing")
    content = _read_bounded_regular(path, ".env")
    seen: set[str] = set()
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_ASSIGNMENT_RE.fullmatch(raw_line)
        if match is None:
            raise BootstrapError(f"Malformed .env assignment on line {line_number}")
        key = match.group(1)
        if key in seen:
            raise BootstrapError(f"Duplicate .env assignment: {key}")
        seen.add(key)
    environment = load_dotenv(path)
    if set(environment) != seen:
        raise BootstrapError("Private .env could not be parsed unambiguously")
    return environment


def _require_private_directory(path: Path) -> None:
    try:
        if not path.exists():
            path.mkdir(mode=0o700, parents=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BootstrapError("LazyLibrarian configuration directory is unsafe")
        os.chmod(path, 0o700, follow_symlinks=False)
    except OSError as exc:
        raise BootstrapError("LazyLibrarian configuration directory is unavailable") from exc


def _atomic_private_replace(path: Path, content: str) -> None:
    _require_private_directory(path.parent)
    temporary = path.with_name(
        f".{path.name}.wysearr-{os.getpid()}-{secrets.token_hex(6)}"
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600, follow_symlinks=False)
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise BootstrapError("Unable to write private LazyLibrarian configuration") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _new_parser() -> configparser.ConfigParser:
    return configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )


def read_lazylibrarian_config(path: Path) -> configparser.ConfigParser:
    parser = _new_parser()
    if not path.exists():
        return parser
    content = _read_bounded_regular(path, "LazyLibrarian configuration")
    try:
        parser.read_string(content)
    except configparser.Error as exc:
        raise BootstrapError("LazyLibrarian configuration is malformed or duplicated") from exc
    return parser


def _section_name(
    parser: configparser.ConfigParser, expected: str
) -> str | None:
    matches = [name for name in parser.sections() if name.casefold() == expected.casefold()]
    if len(matches) > 1:
        raise BootstrapError(
            f"LazyLibrarian has duplicate case-variant section {expected}"
        )
    return matches[0] if matches else None


def _desired_config(environment: Mapping[str, str]) -> dict[str, dict[str, str]]:
    required = (
        "LAZYLIBRARIAN_API_KEY",
        "QBITTORRENT_USERNAME",
        "QBITTORRENT_PASSWORD",
    )
    for name in required:
        if not environment.get(name):
            raise BootstrapError(f"Required private setting is missing: {name}")
    desired = copy.deepcopy(CONFIG_VALUES)
    desired["API"]["api_key"] = environment["LAZYLIBRARIAN_API_KEY"]
    desired["QBITTORRENT"]["qbittorrent_user"] = environment[
        "QBITTORRENT_USERNAME"
    ]
    desired["QBITTORRENT"]["qbittorrent_pass"] = environment[
        "QBITTORRENT_PASSWORD"
    ]
    return desired


def _apply_desired_config(
    parser: configparser.ConfigParser,
    desired: Mapping[str, Mapping[str, str]],
) -> None:
    for section, values in desired.items():
        actual_section = _section_name(parser, section)
        if actual_section is None:
            parser.add_section(section)
            actual_section = section
        for key, value in values.items():
            parser.set(actual_section, key, value)


def assert_lazylibrarian_config(
    parser: configparser.ConfigParser,
    environment: Mapping[str, str],
) -> None:
    desired = _desired_config(environment)
    for section, values in desired.items():
        actual_section = _section_name(parser, section)
        if actual_section is None:
            raise BootstrapError(f"LazyLibrarian is missing section {section}")
        for key, value in values.items():
            if parser.get(actual_section, key, fallback=None) != value:
                raise BootstrapError(
                    f"LazyLibrarian did not persist managed setting {section}.{key}"
                )


def prepare_lazylibrarian_config(root: Path) -> dict[str, str]:
    """Generate/reuse the API key and converge config.ini while LL is stopped."""

    env_path = root / ".env"
    environment = load_strict_environment(env_path)
    try:
        os.chmod(env_path, 0o600, follow_symlinks=False)
    except OSError as exc:
        raise BootstrapError("Unable to make private .env owner-only") from exc
    owner = environment.get("EBOOK_ACQUISITION_OWNER", "shelfarr")
    if owner not in {"shelfarr", "lazylibrarian", "direct"}:
        raise BootstrapError(
            "EBOOK_ACQUISITION_OWNER must be shelfarr, lazylibrarian, or direct"
        )
    if (
        owner == "lazylibrarian"
        and environment.get("LAZYLIBRARIAN_ENABLED", "false") != "true"
    ):
        raise BootstrapError(
            "LazyLibrarian ebook ownership requires LAZYLIBRARIAN_ENABLED=true"
        )

    api_key = environment.get("LAZYLIBRARIAN_API_KEY", "")
    if not api_key:
        api_key = secrets.token_hex(16)
        environment = update_dotenv(env_path, {"LAZYLIBRARIAN_API_KEY": api_key})
        # Re-parse the newly persisted generation through the strict boundary.
        environment = load_strict_environment(env_path)
    if API_KEY_RE.fullmatch(api_key) is None:
        raise BootstrapError(
            "LAZYLIBRARIAN_API_KEY must be exactly 32 lowercase hexadecimal characters"
        )

    config_path = root / "config" / "lazylibrarian" / "config.ini"
    parser = read_lazylibrarian_config(config_path)
    _apply_desired_config(parser, _desired_config(environment))
    rendered = StringIO()
    parser.write(rendered)
    _atomic_private_replace(config_path, rendered.getvalue())
    persisted = read_lazylibrarian_config(config_path)
    assert_lazylibrarian_config(persisted, environment)
    return environment


class LazyLibrarianApi:
    """Bounded API client whose errors never contain its credential-bearing URL."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        opener: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/api"
        self.api_key = api_key
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener()

    def raw(self, command: str, parameters: Mapping[str, Any] | None = None) -> bytes:
        values: list[tuple[str, Any]] = [
            ("apikey", self.api_key),
            ("cmd", command),
        ]
        values.extend((str(key), value) for key, value in (parameters or {}).items())
        body = urllib.parse.urlencode(values, doseq=True).encode("utf-8")
        request = urllib.request.Request(
            self.base_url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "WyseARR-bootstrap/1",
            },
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            try:
                data = response.read(MAX_PRIVATE_FILE_BYTES + 1)
                status = getattr(response, "status", response.getcode())
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise BootstrapError(
                f"LazyLibrarian API command {command} is unavailable"
            ) from exc
        if not 200 <= status < 300 or len(data) > MAX_PRIVATE_FILE_BYTES:
            raise BootstrapError(f"LazyLibrarian API command {command} failed")
        return data

    def json(
        self, command: str, parameters: Mapping[str, Any] | None = None
    ) -> Any:
        try:
            return json.loads(self.raw(command, parameters).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BootstrapError(
                f"LazyLibrarian API command {command} returned malformed JSON"
            ) from exc


def validate_lazylibrarian_api(api: LazyLibrarianApi) -> str:
    version = api.json("getVersion")
    if not isinstance(version, dict) or version.get("Success") is not True:
        raise BootstrapError("LazyLibrarian version response is invalid")
    current = version.get("current_version")
    # The pinned LinuxServer non-git build reports an empty API version even
    # though its image label and /build_version carry ``02af0464-ls331``.
    # scripts/validate.py's exact Compose digest check is the independent image
    # identity authority; reject every non-empty API version except the pinned
    # source version here.
    if current not in {"", EXPECTED_LAZYLIBRARIAN_VERSION}:
        raise BootstrapError("LazyLibrarian running version does not match the pinned image")
    help_text = api.raw("help").decode("utf-8", errors="replace")
    missing = [command for command in REQUIRED_API_COMMANDS if command not in help_text]
    if missing:
        raise BootstrapError(
            "LazyLibrarian API is missing required commands: " + ", ".join(missing)
        )
    return EXPECTED_LAZYLIBRARIAN_VERSION


def _read_effective_config_value(
    api: LazyLibrarianApi, section: str, key: str
) -> str:
    """Read one active config value without putting credentials in the URL."""

    data = api.raw("readCFG", {"group": section, "name": key})
    try:
        value = data.decode("utf-8")
    except UnicodeError as exc:
        raise BootstrapError(
            f"LazyLibrarian returned an invalid effective setting for {section}.{key}"
        ) from exc
    # The pinned API deliberately wraps even scalar values in square brackets.
    # Slice only the outer pair so brackets inside a configured password remain
    # part of the value.
    if len(value) < 2 or value[0] != "[" or value[-1] != "]":
        raise BootstrapError(
            f"LazyLibrarian returned an invalid effective setting for {section}.{key}"
        )
    return value[1:-1]


def _semantic_config_value(
    section: str, key: str, value: str
) -> tuple[str, Any] | None:
    identity = (section, key)
    if identity in BOOLEAN_CONFIG_KEYS:
        normalized = value.strip().casefold()
        if normalized in {"", "0", "false", "no", "off"}:
            return ("bool", False)
        if normalized in {"1", "true", "yes", "on"}:
            return ("bool", True)
        return None
    if identity in INTEGER_CONFIG_KEYS:
        if re.fullmatch(r"[+-]?[0-9]+", value.strip()) is None:
            return None
        return ("int", int(value.strip(), 10))
    if identity in CSV_CONFIG_KEYS:
        entries = [] if not value.strip() else [
            item.strip() for item in value.split(",")
        ]
        if any(not item for item in entries):
            return None
        # These managed CSVs are membership sets.  Retaining duplicates in the
        # sorted tuple still rejects an extra or missing entry while tolerating
        # LazyLibrarian's canonical ordering.
        return ("csv", tuple(sorted(entries)))
    return ("str", value)


def assert_effective_lazylibrarian_config(
    api: LazyLibrarianApi, environment: Mapping[str, str]
) -> None:
    """Validate active typed values after LazyLibrarian has loaded config.ini."""

    desired = _desired_config(environment)
    # Establish the non-secret logging/redaction guard first.  Some later
    # managed values are credentials, and neither values nor API bodies are
    # ever included in bootstrap diagnostics.
    ordered_sections = ["LOGGING"] + [
        section for section in desired if section != "LOGGING"
    ]
    for section in ordered_sections:
        for key, expected in desired[section].items():
            actual = _read_effective_config_value(api, section, key)
            expected_semantic = _semantic_config_value(section, key, expected)
            actual_semantic = _semantic_config_value(section, key, actual)
            if expected_semantic is None or actual_semantic != expected_semantic:
                raise BootstrapError(
                    "LazyLibrarian effective managed setting "
                    f"{section}.{key} is incorrect"
                )


def _tag_ids(resource: Mapping[str, Any]) -> set[int]:
    tags = resource.get("tags", [])
    if not isinstance(tags, list):
        raise BootstrapError("Prowlarr returned invalid tags")
    result: set[int] = set()
    for item in tags:
        value = item.get("id") if isinstance(item, dict) else item
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BootstrapError("Prowlarr returned invalid tags")
        result.add(value)
    return result


def _category_ids(resource: Mapping[str, Any]) -> set[int]:
    capabilities = resource.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(
        capabilities.get("categories"), list
    ):
        raise BootstrapError("Prowlarr returned invalid indexer categories")
    pending = list(capabilities["categories"])
    result: set[int] = set()
    while pending:
        category = pending.pop()
        if not isinstance(category, dict):
            raise BootstrapError("Prowlarr returned invalid indexer categories")
        raw_id = category.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise BootstrapError("Prowlarr returned invalid indexer categories")
        category_id = raw_id
        # Prowlarr may expose the same custom category under more than one
        # hierarchy branch (the pinned Nyaa schema does this), so duplicate
        # numeric ids are a valid tree shape and collapse naturally here.
        if category_id <= 0:
            raise BootstrapError("Prowlarr returned invalid indexer categories")
        result.add(category_id)
        children = category.get("subCategories", [])
        if not isinstance(children, list):
            raise BootstrapError("Prowlarr returned invalid indexer categories")
        pending.extend(children)
    return result


def _set_prowlarr_tags(
    client: Any, endpoint: str, resource: Mapping[str, Any], tags: set[int]
) -> dict[str, Any]:
    resource_id = resource.get("id")
    if isinstance(resource_id, bool) or not isinstance(resource_id, int):
        raise BootstrapError("Prowlarr managed resource has no numeric id")
    payload = copy.deepcopy(dict(resource))
    payload["tags"] = sorted(tags)
    saved = client.put_json(f"{endpoint}/{resource_id}?forceSave=true", payload)
    if not isinstance(saved, dict) or saved.get("id") != resource_id:
        raise BootstrapError("Prowlarr did not persist managed tags")
    persisted = client.get_json(f"{endpoint}/{resource_id}")
    if not isinstance(persisted, dict) or _tag_ids(persisted) != tags:
        raise BootstrapError("Prowlarr did not persist managed tags")
    return persisted


def _failed_indexer_ids(client: Any) -> set[int]:
    statuses = client.get_json("/api/v1/indexerstatus")
    if not isinstance(statuses, list) or any(
        not isinstance(item, dict) for item in statuses
    ):
        raise BootstrapError("Prowlarr returned invalid indexer status data")
    blocked: set[int] = set()
    seen: set[int] = set()
    for status in statuses:
        indexer_id = status.get("indexerId")
        if (
            isinstance(indexer_id, bool)
            or not isinstance(indexer_id, int)
            or indexer_id <= 0
            or indexer_id in seen
        ):
            raise BootstrapError("Prowlarr returned invalid indexer status data")
        seen.add(indexer_id)
        has_failure_state = False
        for field in ("initialFailure", "mostRecentFailure", "disabledTill"):
            if field not in status:
                raise BootstrapError("Prowlarr returned invalid indexer status data")
            value = status[field]
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise BootstrapError("Prowlarr returned invalid indexer status data")
            try:
                parsed = datetime.fromisoformat(
                    value.strip().replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise BootstrapError(
                    "Prowlarr returned invalid indexer status data"
                ) from exc
            if parsed.tzinfo is None:
                raise BootstrapError("Prowlarr returned invalid indexer status data")
            has_failure_state = True
        # A disabledTill timestamp may already have elapsed while the backing
        # indexer remains broken (the deployed Torrent Downloads endpoint was
        # still returning 429).  Keep every retained failure-status row out of
        # the managed set until Prowlarr clears all failure fields after a
        # successful use.
        if has_failure_state:
            blocked.add(indexer_id)
    return blocked


def converge_ebook_indexer_tags(client: Any, tag_id: int) -> dict[str, str]:
    raw_indexers = client.get_json("/api/v1/indexer")
    if not isinstance(raw_indexers, list) or any(
        not isinstance(item, dict) for item in raw_indexers
    ):
        raise BootstrapError("Prowlarr returned invalid indexer data")
    indexers: list[dict[str, Any]] = raw_indexers
    indexer_ids: set[int] = set()
    category_ids: dict[int, set[int]] = {}
    current_tags: dict[int, set[int]] = {}
    for item in indexers:
        indexer_id = item.get("id")
        if (
            isinstance(indexer_id, bool)
            or not isinstance(indexer_id, int)
            or indexer_id <= 0
            or indexer_id in indexer_ids
            or not isinstance(item.get("enable"), bool)
            or not isinstance(item.get("protocol"), str)
        ):
            raise BootstrapError("Prowlarr returned invalid indexer data")
        indexer_ids.add(indexer_id)
        category_ids[indexer_id] = _category_ids(item)
        current_tags[indexer_id] = _tag_ids(item)

    eligible = [
        item
        for item in indexers
        if item["enable"]
        and item["protocol"].casefold() == "torrent"
        and REQUIRED_EBOOK_CATEGORY in category_ids[item["id"]]
    ]
    if not eligible:
        raise BootstrapError(
            "Prowlarr has no enabled torrent indexer with explicit ebook category 7020"
        )
    names = [str(item.get("name") or "").strip() for item in eligible]
    if any(not name for name in names) or len({name.casefold() for name in names}) != len(
        names
    ):
        raise BootstrapError("Prowlarr ebook indexer names are empty or duplicated")
    blocked_ids = _failed_indexer_ids(client)
    available = [item for item in eligible if item["id"] not in blocked_ids]
    if not available:
        raise BootstrapError("Prowlarr has no available torrent indexer for Books")
    available_ids = {item["id"] for item in available}
    # This tag is owned by the integration.  Removing only this tag from
    # blocked or non-7020 indexers narrows future full-sync scope while
    # preserving every unrelated tag.
    for item in indexers:
        existing_tags = current_tags[item["id"]]
        tags = (
            existing_tags | {tag_id}
            if item["id"] in available_ids
            else existing_tags - {tag_id}
        )
        if tags != existing_tags:
            _set_prowlarr_tags(client, "/api/v1/indexer", item, tags)
    return {
        str(item.get("name") or "").strip(): str(REQUIRED_EBOOK_CATEGORY)
        for item in available
    }


def _field_values(resource: Mapping[str, Any]) -> dict[str, Any]:
    fields = resource.get("fields", [])
    if not isinstance(fields, list):
        raise BootstrapError("Prowlarr returned invalid application fields")
    result: dict[str, Any] = {}
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            raise BootstrapError("Prowlarr returned invalid application fields")
        name = field["name"]
        if name in result:
            raise BootstrapError("Prowlarr returned duplicate application fields")
        result[name] = field.get("value")
    return result


def _application_managed_state(resource: Mapping[str, Any]) -> tuple[Any, ...]:
    fields = _field_values(resource)
    sync_categories = fields.get("syncCategories", [])
    if not isinstance(sync_categories, list):
        raise BootstrapError("Prowlarr returned invalid sync categories")
    return (
        resource.get("name"),
        bool(resource.get("enable")),
        resource.get("implementation"),
        resource.get("configContract"),
        resource.get("syncLevel"),
        resource.get("appProfileId"),
        tuple(sorted(_tag_ids(resource))),
        fields.get("prowlarrUrl"),
        fields.get("baseUrl"),
        fields.get("authUsername"),
        fields.get("authPassword"),
        tuple(sync_categories),
    )


def converge_lazylibrarian_application(
    client: Any, api_key: str, tag_id: int
) -> dict[str, Any]:
    applications = client.get_json("/api/v1/applications")
    if not isinstance(applications, list):
        raise BootstrapError("Prowlarr returned invalid applications")
    matches = [
        item
        for item in applications
        if isinstance(item, dict)
        and (
            str(item.get("name", "")).casefold()
            == MANAGED_APPLICATION_NAME.casefold()
            or str(item.get("implementation", "")).casefold()
            == "lazylibrarian"
        )
    ]
    if len(matches) > 1:
        raise BootstrapError("Prowlarr has duplicate LazyLibrarian applications")
    existing = matches[0] if matches else None
    if existing is not None and str(existing.get("implementation", "")).casefold() != "lazylibrarian":
        raise BootstrapError("Prowlarr has a conflicting LazyLibrarian application name")

    schemas = client.get_json("/api/v1/applications/schema")
    schema_matches = [
        item
        for item in schemas
        if isinstance(item, dict)
        and str(item.get("implementation", "")).casefold() == "lazylibrarian"
    ] if isinstance(schemas, list) else []
    if len(schema_matches) != 1:
        raise BootstrapError("Prowlarr has no unique LazyLibrarian application schema")

    desired = copy.deepcopy(existing or schema_matches[0])
    desired.pop("presets", None)
    # Prowlarr's application schema omits appProfileId and its API normalizes
    # that property to null on persisted applications.  Application profiles
    # are not part of this integration contract (unlike an indexer profile), so
    # do not send a numeric id that Prowlarr will immediately discard.
    desired.pop("appProfileId", None)
    desired["name"] = MANAGED_APPLICATION_NAME
    desired["enable"] = True
    desired["syncLevel"] = "fullSync"
    desired["tags"] = [tag_id]
    set_provider_field(desired, "prowlarrUrl", "http://prowlarr:9696")
    set_provider_field(desired, "baseUrl", "http://lazylibrarian:5299")
    set_provider_field(desired, "apiKey", api_key)
    set_provider_field(desired, "authUsername", "")
    set_provider_field(desired, "authPassword", "")
    set_provider_field(desired, "syncCategories", list(MANAGED_SYNC_CATEGORIES))

    needs_update = existing is None
    if existing is not None:
        needs_update = _application_managed_state(existing) != _application_managed_state(
            desired
        )
        current_key = get_provider_field(existing, "apiKey")
        if current_key not in MASKED_SECRET_VALUES and current_key != api_key:
            needs_update = True
        if not needs_update:
            try:
                client.post_json("/api/v1/applications/test", existing, retry=True)
            except (ApiError, ApiTransportError):
                needs_update = True

    if needs_update:
        try:
            client.post_json("/api/v1/applications/test", desired, retry=True)
        except (ApiError, ApiTransportError) as exc:
            raise BootstrapError("Prowlarr cannot reach LazyLibrarian") from exc
        if existing is None:
            saved = client.post_json(
                "/api/v1/applications?forceSave=true", desired, retry=False
            )
        else:
            resource_id = existing.get("id")
            if isinstance(resource_id, bool) or not isinstance(resource_id, int):
                raise BootstrapError("Prowlarr LazyLibrarian application has no numeric id")
            saved = client.put_json(
                f"/api/v1/applications/{resource_id}?forceSave=true", desired
            )
        saved_id = saved.get("id") if isinstance(saved, dict) else None
        if isinstance(saved_id, bool) or not isinstance(saved_id, int):
            raise BootstrapError("Prowlarr did not persist LazyLibrarian")
    else:
        saved_id = existing.get("id")

    persisted = client.get_json(f"/api/v1/applications/{saved_id}")
    if not isinstance(persisted, dict) or _application_managed_state(
        persisted
    ) != _application_managed_state(desired):
        raise BootstrapError("Prowlarr did not persist LazyLibrarian settings")
    try:
        client.post_json("/api/v1/applications/test", persisted, retry=True)
    except (ApiError, ApiTransportError) as exc:
        raise BootstrapError("Prowlarr cannot validate persisted LazyLibrarian") from exc
    return persisted


def run_application_indexer_sync(
    client: Any,
    *,
    timeout: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    command = client.post_json(
        "/api/v1/command", {"name": "ApplicationIndexerSync"}, retry=False
    )
    command_id = command.get("id") if isinstance(command, dict) else None
    if isinstance(command_id, bool) or not isinstance(command_id, int):
        raise BootstrapError("Prowlarr did not start application indexer sync")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get_json(f"/api/v1/command/{command_id}")
        if not isinstance(status, dict):
            raise BootstrapError("Prowlarr returned invalid sync status")
        state = str(status.get("status", "")).casefold()
        result = str(status.get("result", "")).casefold()
        if state == "completed" and result in {"successful", "success", ""}:
            return
        if state in {"failed", "aborted", "cancelled"} or result in {
            "failed",
            "aborted",
        }:
            raise BootstrapError("Prowlarr application indexer sync failed")
        sleep(1)
    raise BootstrapError("Prowlarr application indexer sync timed out")


def _provider_arrays(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise BootstrapError("LazyLibrarian returned invalid provider data")
    result: dict[str, list[dict[str, Any]]] = {}
    for provider_type in ("newznab", "torznab", "rss", "irc", "torrent", "direct"):
        items = value.get(provider_type)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise BootstrapError("LazyLibrarian returned invalid provider data")
        result[provider_type] = items
    return result


def _enabled(value: Any) -> bool:
    return value in {True, 1, "1", "true", "True"}


def _prowlarr_provider_name(value: Any) -> str:
    name = str(value or "").strip()
    suffix = " (prowlarr)"
    return name[: -len(suffix)].strip() if name.casefold().endswith(suffix) else name


def _assert_prowlarr_provider_host(provider: Mapping[str, Any]) -> None:
    host = str(provider.get("HOST") or "")
    try:
        parsed = urllib.parse.urlsplit(host)
        port = parsed.port
    except ValueError as exc:
        raise BootstrapError("LazyLibrarian provider host is invalid") from exc
    if parsed.scheme != "http" or parsed.hostname != "prowlarr" or port != 9696:
        raise BootstrapError("LazyLibrarian provider is not routed through Prowlarr")


def _canonical_category_csv(
    value: Any, *, allow_empty: bool = False
) -> tuple[str, ...] | None:
    if not isinstance(value, str):
        return None
    if value == "":
        return () if allow_empty else None
    if re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", value) is None:
        return None
    categories = tuple(value.split(","))
    if (
        len(set(categories)) != len(categories)
        or tuple(sorted(categories, key=int)) != categories
    ):
        return None
    return categories


def _dormant_provider_categories_are_canonical(
    provider: Mapping[str, Any],
) -> bool:
    return all(
        _canonical_category_csv(provider.get(key), allow_empty=True) is not None
        for key in ("AUDIOCAT", "MAGCAT", "COMICCAT")
    )


def _expected_provider_categories(
    values: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    if not values:
        raise BootstrapError("Prowlarr has no available ebook provider")
    result: dict[str, tuple[str, ...]] = {}
    required = (str(REQUIRED_EBOOK_CATEGORY),)
    for name, categories in values.items():
        folded = str(name).strip().casefold()
        normalized = _canonical_category_csv(categories)
        if (
            not folded
            or folded in result
            or normalized != required
        ):
            raise BootstrapError("Prowlarr returned invalid ebook provider categories")
        result[folded] = normalized
    return result


def converge_ebook_providers(
    api: LazyLibrarianApi,
    expected_categories: Mapping[str, str],
) -> int:
    """Force Prowlarr-created providers to ebook-only and verify no rival source.

    The pinned LL ``changeProvider`` implementation always refreshes Torznab
    capabilities after applying API values.  That refresh repopulates its
    AUDIOCAT/MAGCAT/COMICCAT metadata, so those fields cannot be cleared via
    the supported API.  They are dormant when DLTYPES is exactly ``E``: the
    pinned search dispatcher rejects every audio, magazine, and comic search
    before consulting the corresponding category.  MANUAL prevents unrelated
    background capability refreshes.  Prowlarr compares its desired BOOKCAT
    with LL's merged BOOKCAT/AUDIOCAT/MAGCAT/COMICCAT view, so its scheduled
    six-hour full-sync will harmlessly refresh these providers again; that
    update cannot change either DLTYPES or MANUAL, and BOOKCAT returns to 7020
    from the explicitly required capability.
    """

    expected = _expected_provider_categories(expected_categories)
    providers = _provider_arrays(api.json("listProviders"))
    active_nab: list[tuple[str, dict[str, Any]]] = []
    for provider_type in ("newznab", "torznab"):
        for provider in providers[provider_type]:
            if _enabled(provider.get("ENABLED")):
                active_nab.append((provider_type, provider))
    names = [_prowlarr_provider_name(item.get("DISPNAME")) for _, item in active_nab]
    folded_names = [name.casefold() for name in names]
    if (
        len(folded_names) != len(set(folded_names))
        or set(folded_names) != set(expected)
    ):
        raise BootstrapError(
            "LazyLibrarian provider set does not match Prowlarr ebook indexers"
        )
    if any(provider_type != "torznab" for provider_type, _ in active_nab):
        raise BootstrapError("LazyLibrarian unexpectedly received a Usenet provider")

    for provider_type, provider in active_nab:
        _assert_prowlarr_provider_host(provider)
        provider_name = _prowlarr_provider_name(provider.get("DISPNAME")).casefold()
        book_categories = expected[provider_name]
        desired = {
            "BOOKCAT": ",".join(book_categories),
            "DLTYPES": "E",
            "MANUAL": "1",
        }
        needs_update = (
            _canonical_category_csv(provider.get("BOOKCAT")) != book_categories
            or str(provider.get("DLTYPES") or "") != "E"
            or not _enabled(provider.get("MANUAL"))
            or not _dormant_provider_categories_are_canonical(provider)
        )
        if needs_update:
            parameters = {
                "name": str(provider.get("DISPNAME") or ""),
                "providertype": provider_type,
                **desired,
            }
            try:
                api.json("changeProvider", parameters)
            except BootstrapError:
                # The mutation may have reached LL before a transport failure.
                pass

    verified = _provider_arrays(api.json("listProviders"))
    active_verified: list[tuple[str, dict[str, Any]]] = [
        (provider_type, provider)
        for provider_type in ("newznab", "torznab")
        for provider in verified[provider_type]
        if _enabled(provider.get("ENABLED"))
    ]
    verified_names = {
        _prowlarr_provider_name(provider.get("DISPNAME")).casefold()
        for _, provider in active_verified
    }
    if len(active_verified) != len(expected) or verified_names != set(expected):
        raise BootstrapError("LazyLibrarian provider set changed during convergence")
    if any(provider_type != "torznab" for provider_type, _ in active_verified):
        raise BootstrapError("LazyLibrarian unexpectedly received a Usenet provider")
    for _, provider in active_verified:
        _assert_prowlarr_provider_host(provider)
        provider_name = _prowlarr_provider_name(provider.get("DISPNAME")).casefold()
        if (
            _canonical_category_csv(provider.get("BOOKCAT"))
            != expected[provider_name]
            or str(provider.get("DLTYPES") or "") != "E"
            or not _enabled(provider.get("MANUAL"))
            or not _dormant_provider_categories_are_canonical(provider)
        ):
            raise BootstrapError("LazyLibrarian provider is not ebook-only")
    for provider_type in ("rss", "irc", "torrent", "direct"):
        if any(_enabled(provider.get("ENABLED")) for provider in verified[provider_type]):
            raise BootstrapError(
                "LazyLibrarian has an enabled provider outside managed Prowlarr"
            )
    return len(active_verified)


def _parse_tcp_port(value: Any, label: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise BootstrapError(f"{label} port must be numeric") from exc
    if not 1 <= port <= 65535:
        raise BootstrapError(f"{label} port must be between 1 and 65535")
    return port


def validate_qbittorrent(
    environment: Mapping[str, str],
    *,
    client_factory: Callable[..., Any] = QbittorrentClient,
) -> None:
    required = ("QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD")
    for name in required:
        if not environment.get(name):
            raise BootstrapError(f"Required private setting is missing: {name}")
    bind_address = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    port = environment.get("QBITTORRENT_PORT", "8080")
    parsed_port = _parse_tcp_port(port, "qBittorrent")
    client = client_factory(
        f"http://{bind_address}:{parsed_port}", timeout=10.0, retries=3
    )
    try:
        authenticated = client.login(
            environment["QBITTORRENT_USERNAME"],
            environment["QBITTORRENT_PASSWORD"],
        )
    except BootstrapError as exc:
        raise BootstrapError("qBittorrent validation login failed") from exc
    if authenticated is not True:
        raise BootstrapError("qBittorrent validation login failed")
    categories = client.categories()
    category = categories.get("ebooks") if isinstance(categories, dict) else None
    if not isinstance(category, dict) or category.get("savePath") != "/downloads/ebooks":
        raise BootstrapError("qBittorrent ebook category/save path is not ready")


def _validate_runtime_environment(environment: Mapping[str, str]) -> None:
    required = (
        "LAZYLIBRARIAN_API_KEY",
        "PROWLARR_API_KEY",
        "QBITTORRENT_USERNAME",
        "QBITTORRENT_PASSWORD",
    )
    for name in required:
        if not environment.get(name):
            raise BootstrapError(f"Required private setting is missing: {name}")
    if API_KEY_RE.fullmatch(environment["LAZYLIBRARIAN_API_KEY"]) is None:
        raise BootstrapError("LAZYLIBRARIAN_API_KEY has an invalid format")


def bootstrap_lazylibrarian(
    root: Path,
    *,
    timeout: float = 30.0,
    prowlarr_client_factory: Callable[..., Any] = ApiClient,
    ll_api_factory: Callable[..., LazyLibrarianApi] = LazyLibrarianApi,
) -> dict[str, Any]:
    environment = load_strict_environment(root / ".env")
    _validate_runtime_environment(environment)

    admin_port = _parse_tcp_port(
        environment.get("LAZYLIBRARIAN_ADMIN_PORT", "5299"), "LazyLibrarian"
    )
    prowlarr_port = _parse_tcp_port(
        environment.get("PROWLARR_PORT", "9696"), "Prowlarr"
    )
    ll_api = ll_api_factory(
        f"http://127.0.0.1:{admin_port}",
        environment["LAZYLIBRARIAN_API_KEY"],
        timeout=timeout,
    )
    version = validate_lazylibrarian_api(ll_api)
    assert_effective_lazylibrarian_config(ll_api, environment)
    validate_qbittorrent(environment)

    bind_address = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    prowlarr = prowlarr_client_factory(
        f"http://{bind_address}:{prowlarr_port}",
        headers={"X-Api-Key": environment["PROWLARR_API_KEY"]},
        timeout=timeout,
        retries=3,
    )
    tag_id = ensure_prowlarr_tag(prowlarr, MANAGED_PROWLARR_TAG)
    indexer_categories = converge_ebook_indexer_tags(prowlarr, tag_id)
    converge_lazylibrarian_application(
        prowlarr, environment["LAZYLIBRARIAN_API_KEY"], tag_id
    )
    run_application_indexer_sync(prowlarr, timeout=max(60.0, timeout))
    provider_count = converge_ebook_providers(ll_api, indexer_categories)
    return {
        "version": version,
        "indexers": len(indexer_categories),
        "providers": provider_count,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=STACK_ROOT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--prepare-config",
        action="store_true",
        help="Converge private config.ini while LazyLibrarian is stopped",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    root = arguments.root.resolve()
    try:
        if arguments.prepare_config:
            prepare_lazylibrarian_config(root)
            print("LazyLibrarian private ebook-only configuration is prepared.")
            return 0
        result = bootstrap_lazylibrarian(
            root,
            timeout=max(1.0, arguments.timeout),
        )
    except (BootstrapError, ApiError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "LazyLibrarian ebook-only integration verified "
        f"(version {result['version']}, {result['indexers']} Prowlarr indexers, "
        f"{result['providers']} providers)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
