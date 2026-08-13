#!/usr/bin/env python3
"""Non-acquiring production validation for the WyseARR stack."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[1]
CORE_SERVICES = (
    "qbittorrent",
    "prowlarr",
    "sonarr",
    "radarr",
    "lidarr",
    "bazarr",
    "whisparr",
    "bookbot",
    "huey",
)
EVALUATION_SERVICES = ("sabnzbd", "shelfarr")
DIRECT_CATEGORIES = ("ebooks", "audiobooks", "manga-comics", "roms", "sheet-music")
ARR_CATEGORIES = ("tv", "movies", "music", "spicy")
SHELFARR_DOWNLOAD_CATEGORY = "shelfarr"
USENET_FEATURE_FLAG = "WYSEARR_USENET_ENABLED"
MANAGED_SABNZBD_SERVER = "WyseARR Primary"
MANAGED_NEWZNAB_INDEXER_DEFAULT = "WyseARR Books"
MANAGED_PROWLARR_TAG = "shelfarr"
ARR_PROWLARR_TAG = "wysearr-arr"
BAZARR_PROVIDERS = {"embeddedsubtitles", "yifysubtitles", "subf2m"}
REQUIRED_DISCORD_CHANNELS = {
    "requests": frozenset(
        {
            "movies-tv",
            "ebooks",
            "audiobooks",
            "manga-comics",
            "roms",
            "sheet-music",
        }
    ),
    "activity": frozenset(
        {"download-queue", "request-status", "recent-additions"}
    ),
    "system": frozenset({"import-errors", "system-health"}),
}
ARR_NOTIFICATION_DATABASES = {
    "sonarr": Path("config/sonarr/sonarr.db"),
    "radarr": Path("config/radarr/radarr.db"),
    "lidarr": Path("config/lidarr/lidarr.db"),
}
DISCORD_WEBHOOK_MARKERS = (
    "discord.com/api/webhooks/",
    "discordapp.com/api/webhooks/",
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        flag_assignment = re.match(
            rf"^\s*(?:export\s+)?{re.escape(USENET_FEATURE_FLAG)}\s*=",
            raw_line,
        )
        if flag_assignment:
            values[USENET_FEATURE_FLAG] = (
                "__WYSEARR_DUPLICATE__"
                if USENET_FEATURE_FLAG in values
                else (
                    raw_line[len(f"{USENET_FEATURE_FLAG}="):]
                    if raw_line.startswith(f"{USENET_FEATURE_FLAG}=")
                    else raw_line
                )
            )
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _value = line.split("=", 1)
        normalized_key = key.strip()
        values[normalized_key] = _value.strip()
    return values


def load_channel_inventory(path: Path) -> dict[str, dict[str, str]]:
    """Parse the deliberately simple two-level Discord channel inventory."""

    inventory: dict[str, dict[str, str]] = {}
    current_section: str | None = None
    assigned_ids: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():
            if not line.endswith(":") or ":" in line[:-1]:
                raise ValueError(f"invalid channel section on line {line_number}")
            current_section = line[:-1].strip()
            if not current_section or current_section in inventory:
                raise ValueError(f"duplicate channel section on line {line_number}")
            inventory[current_section] = {}
            continue
        if current_section is None or ":" not in line:
            raise ValueError(f"invalid channel entry on line {line_number}")
        name, raw_value = line.strip().split(":", 1)
        name = name.strip()
        value = raw_value.strip()
        if not name or name in inventory[current_section]:
            raise ValueError(f"duplicate channel entry on line {line_number}")
        if not value.isdigit() or int(value) <= 0:
            raise ValueError(
                f"{current_section}.{name} is not a positive Discord channel ID"
            )
        assignment = f"{current_section}.{name}"
        if value in assigned_ids:
            raise ValueError(
                f"Discord channel ID is assigned to both "
                f"{assigned_ids[value]} and {assignment}"
            )
        inventory[current_section][name] = value
        assigned_ids[value] = assignment
    return inventory


def channel_inventory_check(path: Path) -> Check:
    try:
        inventory = load_channel_inventory(path)
        missing = [
            f"{section}.{name}"
            for section, required_names in REQUIRED_DISCORD_CHANNELS.items()
            for name in sorted(required_names.difference(inventory.get(section, {})))
        ]
    except (OSError, UnicodeError, ValueError) as error:
        return Check("huey:channels", False, f"invalid inventory: {error}")
    if missing:
        return Check(
            "huey:channels",
            False,
            "missing required channel(s): " + ", ".join(missing),
        )
    required_count = sum(len(names) for names in REQUIRED_DISCORD_CHANNELS.values())
    return Check(
        "huey:channels",
        True,
        f"{required_count} request and lifecycle channel IDs are valid and unique",
    )


def huey_ready_check(path: Path) -> Check:
    try:
        if not path.is_file():
            return Check("huey:discord-ready", False, "ready marker missing")
        ready = path.read_text(encoding="utf-8").strip() == "ready"
    except (OSError, UnicodeError) as error:
        return Check("huey:discord-ready", False, type(error).__name__)
    return Check(
        "huey:discord-ready",
        ready,
        "ready marker valid" if ready else "ready marker content invalid",
    )


def huey_selection_ttl_check(environment: dict[str, str]) -> Check:
    """Validate the bounded Discord candidate-confirmation lifetime."""

    raw = environment.get("HUEY_SELECTION_TTL_SECONDS", "900")
    if not re.fullmatch(r"[0-9]+", raw):
        return Check(
            "huey:selection-ttl",
            False,
            "must be a literal integer between 1 and 86400 seconds",
        )
    ttl = int(raw)
    valid = 1 <= ttl <= 86_400
    return Check(
        "huey:selection-ttl",
        valid,
        f"{ttl} seconds" if valid else "must be between 1 and 86400 seconds",
    )


def _sqlite_indexes(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[bool, tuple[str, ...]]]:
    if not re.fullmatch(r"[a-z_]+", table):  # pragma: no cover - constants only
        raise ValueError("invalid SQLite table name")
    definitions: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for row in connection.execute(f"PRAGMA index_list({table})"):
        name = str(row[1])
        columns = tuple(
            str(column[2])
            for column in connection.execute(f'PRAGMA index_info("{name}")')
        )
        definitions[name] = (bool(row[2]), columns)
    return definitions


def _has_unique_columns(
    indexes: dict[str, tuple[bool, tuple[str, ...]]], columns: tuple[str, ...]
) -> bool:
    return any(unique and indexed == columns for unique, indexed in indexes.values())


def huey_database_check(database: Path) -> Check:
    """Require Huey's request, outbox, and candidate-confirmation schema."""

    try:
        with closing(_open_readonly_database(database)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required_tables = {
                "requests",
                "notification_deliveries",
                "candidate_confirmations",
                "candidate_options",
                "candidate_confirmation_replies",
            }
            if not required_tables <= tables:
                return Check(
                    "huey:database", False, "candidate confirmation tables missing"
                )

            def columns(table: str) -> set[str]:
                return {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }

            request_columns = columns("requests")
            delivery_columns = columns("notification_deliveries")
            confirmation_columns = columns("candidate_confirmations")
            option_columns = columns("candidate_options")
            reply_columns = columns("candidate_confirmation_replies")
            request_indexes = _sqlite_indexes(connection, "requests")
            delivery_indexes = _sqlite_indexes(connection, "notification_deliveries")
            confirmation_indexes = _sqlite_indexes(connection, "candidate_confirmations")
            option_indexes = _sqlite_indexes(connection, "candidate_options")
            reply_indexes = _sqlite_indexes(connection, "candidate_confirmation_replies")

            active_index = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'requests_active_target_uq'"
            ).fetchone()
            active_sql = str(active_index[0] or "").casefold() if active_index else ""
            expiry_index = confirmation_indexes.get(
                "candidate_confirmations_expiry_idx"
            )

        schema_ok = bool(
            {
                "status",
                "updated_at",
                "service",
                "external_id",
                "external_status",
                "error",
                "target_key",
                "message_id",
            }
            <= request_columns
            and {
                "request_id",
                "event_key",
                "route",
                "message",
                "delivered_at",
            }
            <= delivery_columns
            and {
                "request_id",
                "shelfarr_correlation",
                "expires_at",
                "status",
                "prompt_message_id",
                "selected_ordinal",
                "dispatch_started_at",
            }
            <= confirmation_columns
            and {
                "confirmation_id",
                "ordinal",
                "fingerprint",
                "label",
                "book_type",
                "candidate_json",
            }
            <= option_columns
            and {
                "confirmation_id",
                "reply_message_id",
                "discord_user_id",
                "channel_id",
                "ordinal",
                "outcome",
            }
            <= reply_columns
            and _has_unique_columns(request_indexes, ("message_id",))
            and _has_unique_columns(request_indexes, ("target_key",))
            and _has_unique_columns(
                delivery_indexes, ("request_id", "event_key", "route")
            )
            and _has_unique_columns(confirmation_indexes, ("request_id",))
            and _has_unique_columns(
                confirmation_indexes, ("shelfarr_correlation",)
            )
            and _has_unique_columns(confirmation_indexes, ("prompt_message_id",))
            and _has_unique_columns(
                option_indexes, ("confirmation_id", "ordinal")
            )
            and _has_unique_columns(
                option_indexes, ("confirmation_id", "fingerprint")
            )
            and _has_unique_columns(reply_indexes, ("reply_message_id",))
            and expiry_index == (
                False,
                ("status", "expires_at", "id"),
            )
            and "awaiting_selection" in active_sql
        )
        return Check(
            "huey:database",
            integrity == "ok" and schema_ok,
            (
                "integrity, request/outbox, and candidate confirmation schema valid"
                if integrity == "ok" and schema_ok
                else "request/outbox or candidate confirmation schema invalid"
            ),
        )
    except Exception as error:
        return Check("huey:database", False, type(error).__name__)


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _contains_discord_webhook(value: object) -> bool:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = str(value or "")
    normalized = text.casefold()
    return any(marker in normalized for marker in DISCORD_WEBHOOK_MARKERS)


def arr_native_discord_check(service: str, database: Path) -> Check:
    """Ensure an ARR service has no native Discord notification connection."""

    name = f"{service}:native-discord"
    try:
        with closing(_open_readonly_database(database)) as connection:
            columns = {
                str(row[1]).casefold()
                for row in connection.execute("PRAGMA table_info(Notifications)")
            }
            required = {"name", "implementation", "configcontract", "settings"}
            if not required.issubset(columns):
                return Check(name, False, "notification schema unavailable")
            rows = connection.execute(
                "SELECT Name, Implementation, ConfigContract, Settings "
                "FROM Notifications"
            ).fetchall()
    except (OSError, sqlite3.Error) as error:
        return Check(name, False, type(error).__name__)

    native_discord = [
        row
        for row in rows
        if "discord" in str(row["Implementation"] or "").casefold()
        or "discord" in str(row["ConfigContract"] or "").casefold()
        or "discord" in str(row["Name"] or "").casefold()
        or _contains_discord_webhook(row["Settings"])
    ]
    return Check(
        name,
        not native_discord,
        "disabled" if not native_discord else f"configured={len(native_discord)}",
    )


def _simple_yaml_boolean(path: Path, key: str) -> bool:
    pattern = re.compile(
        rf"^\s*{re.escape(key)}\s*:\s*(true|false)\s*(?:#.*)?$",
        re.IGNORECASE,
    )
    values = [
        match.group(1).casefold() == "true"
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := pattern.match(line))
    ]
    if len(values) != 1:
        raise ValueError(f"expected exactly one {key} boolean")
    return values[0]


def _database_truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _strict_feature_flag(
    environment: dict[str, str], key: str
) -> tuple[bool, bool, str]:
    """Return validity, enabled state, and a secret-free status detail."""

    value = environment.get(key, "")
    valid = value in {"", "false", "true"}
    return (
        valid,
        value == "true",
        f"literal {value or 'false'}" if valid else "must be literal true or false",
    )


def bazarr_native_discord_check(database: Path, config: Path) -> Check:
    """Ensure Bazarr cannot bypass Huey with a native Discord notification."""

    name = "bazarr:native-discord"
    try:
        with closing(_open_readonly_database(database)) as connection:
            columns = {
                str(row[1]).casefold()
                for row in connection.execute(
                    "PRAGMA table_info(table_settings_notifier)"
                )
            }
            if not {"name", "enabled", "url"}.issubset(columns):
                return Check(name, False, "notifier schema unavailable")
            rows = connection.execute(
                "SELECT name, enabled, url FROM table_settings_notifier"
            ).fetchall()
        external_webhook_enabled = _simple_yaml_boolean(
            config, "use_external_webhook"
        )
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as error:
        return Check(name, False, type(error).__name__)

    enabled_discord = [
        row
        for row in rows
        if _database_truthy(row["enabled"])
        and (
            str(row["name"] or "").strip().casefold() == "discord"
            or _contains_discord_webhook(row["url"])
        )
    ]
    ok = not enabled_discord and not external_webhook_enabled
    return Check(
        name,
        ok,
        "disabled"
        if ok
        else (
            f"enabled native routes={len(enabled_discord)} "
            f"external_webhook={external_webhook_enabled}"
        ),
    )


def request_json(
    url: str,
    *,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> object:
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    if api_key:
        request_headers["X-Api-Key"] = api_key
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def sabnzbd_post_json(
    port: str,
    api_key: str,
    parameters: dict[str, object],
    *,
    timeout: int = 30,
) -> object:
    """Call a SAB configuration endpoint without putting credentials in a URL."""

    body = urllib.parse.urlencode(
        {"output": "json", "apikey": api_key, **parameters}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def qbit_opener(base_url: str, username: str, password: str) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("Referer", f"{base_url.rstrip('/')}/")]
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(f"{base_url}/api/v2/auth/login", data=body)
    with opener.open(request, timeout=15) as response:
        result = response.read().decode("utf-8", "replace").strip()
    if result != "Ok.":
        raise RuntimeError("qBittorrent authentication rejected")
    return opener


def provider_fields(resource: dict[str, object]) -> dict[str, object]:
    fields = resource.get("fields")
    if not isinstance(fields, list):
        return {}
    return {
        str(field.get("name", "")).casefold(): field.get("value")
        for field in fields
        if isinstance(field, dict) and field.get("name")
    }


def indexer_category_ids(resource: dict[str, object]) -> set[int]:
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


def prowlarr_managed_newznab_check(
    indexers: list[dict[str, object]],
    tags: list[dict[str, object]],
    applications: list[dict[str, object]],
    live_indexer_ids: set[int],
    environment: dict[str, str],
    *,
    enabled: bool,
    flag_valid: bool = True,
) -> Check:
    """Require a live Shelfarr-only Generic Newznab when Usenet is enabled."""

    configured_name = environment.get("NEWZNAB_INDEXER_NAME", "")
    identity_valid = configured_name in {"", MANAGED_NEWZNAB_INDEXER_DEFAULT}
    managed_name = MANAGED_NEWZNAB_INDEXER_DEFAULT
    managed_matches = [
        item
        for item in indexers
        if str(item.get("name") or "").casefold() == managed_name.casefold()
    ]
    managed_indexer = managed_matches[0] if len(managed_matches) == 1 else None
    shelfarr_tags = [
        item
        for item in tags
        if str(item.get("label") or "").casefold() == MANAGED_PROWLARR_TAG
    ]
    arr_tags = [
        item
        for item in tags
        if str(item.get("label") or "").casefold() == ARR_PROWLARR_TAG
    ]
    shelfarr_tag_id = (
        shelfarr_tags[0].get("id") if len(shelfarr_tags) == 1 else None
    )
    arr_tag_id = arr_tags[0].get("id") if len(arr_tags) == 1 else None
    required_apps: dict[str, list[dict[str, object]]] = {}
    for required_name in ("sonarr", "radarr", "lidarr", "whisparr"):
        required_apps[required_name] = [
            app
            for app in applications
            if str(app.get("name") or "").casefold() == required_name
            or str(app.get("implementation") or "").casefold() == required_name
        ]
    if not enabled:
        managed_disabled = len(managed_matches) <= 1 and (
            managed_indexer is None or managed_indexer.get("enable") is False
        )
        topology_absent = not shelfarr_tags and not arr_tags
        topology_retained = bool(
            len(shelfarr_tags) == 1
            and len(arr_tags) == 1
            and isinstance(shelfarr_tag_id, int)
            and isinstance(arr_tag_id, int)
            and shelfarr_tag_id != arr_tag_id
            and all(len(matches) == 1 for matches in required_apps.values())
            and all(
                arr_tag_id in set(matches[0].get("tags") or [])
                and shelfarr_tag_id not in set(matches[0].get("tags") or [])
                for matches in required_apps.values()
                if len(matches) == 1
            )
            and all(
                item is managed_indexer
                or (
                    arr_tag_id in set(item.get("tags") or [])
                    and shelfarr_tag_id not in set(item.get("tags") or [])
                )
                for item in indexers
            )
        )
        ok = identity_valid and managed_disabled and (
            topology_absent or topology_retained
        )
        return Check(
            "prowlarr:managed-newznab",
            ok,
            "disabled"
            if ok
            else "managed Newznab or retained tag isolation is unsafe while disabled",
        )
    expected_url = environment.get("NEWZNAB_BASE_URL", "").strip().rstrip("/")
    expected_path = environment.get("NEWZNAB_API_PATH", "/api").strip()
    fields = (
        provider_fields(managed_indexer)
        if isinstance(managed_indexer, dict)
        else {}
    )
    book_categories = (
        indexer_category_ids(managed_indexer)
        if isinstance(managed_indexer, dict)
        else set()
    )
    private_settings_ok = bool(
        flag_valid
        and identity_valid
        and managed_name
        and expected_url
        and environment.get("NEWZNAB_API_KEY", "").strip()
        and expected_path.startswith("/")
    )
    app_isolation_ok = bool(
        isinstance(shelfarr_tag_id, int)
        and isinstance(arr_tag_id, int)
        and shelfarr_tag_id != arr_tag_id
        and all(len(matches) == 1 for matches in required_apps.values())
        and all(
            arr_tag_id in set(matches[0].get("tags") or [])
            and shelfarr_tag_id not in set(matches[0].get("tags") or [])
            for matches in required_apps.values()
            if len(matches) == 1
        )
    )
    application_tag_ids = set().union(
        *(
            set(matches[0].get("tags") or [])
            for matches in required_apps.values()
            if len(matches) == 1
        )
    )
    indexer_isolation_ok = bool(
        isinstance(shelfarr_tag_id, int)
        and isinstance(arr_tag_id, int)
        and all(
            (
                shelfarr_tag_id in set(item.get("tags") or [])
                and not (set(item.get("tags") or []) & application_tag_ids)
            )
            if item is managed_indexer
            else (
                arr_tag_id in set(item.get("tags") or [])
                and shelfarr_tag_id not in set(item.get("tags") or [])
            )
            for item in indexers
        )
    )
    ok = bool(
        private_settings_ok
        and isinstance(managed_indexer, dict)
        and managed_indexer.get("enable") is True
        and str(managed_indexer.get("implementation") or "").casefold()
        == "newznab"
        and str(managed_indexer.get("configContract") or "").casefold()
        == "newznabsettings"
        and str(managed_indexer.get("protocol") or "").casefold() == "usenet"
        and managed_indexer.get("name") == MANAGED_NEWZNAB_INDEXER_DEFAULT
        and str(managed_indexer.get("priority")) == "20"
        and managed_indexer.get("id") in live_indexer_ids
        and len(managed_matches) == 1
        and app_isolation_ok
        and indexer_isolation_ok
        and str(fields.get("baseurl") or "").rstrip("/") == expected_url
        and str(fields.get("apipath") or "") == expected_path
        and bool(str(fields.get("apikey") or "").strip())
        and 3030 in book_categories
        and bool({7000, 7020} & book_categories)
    )
    return Check(
        "prowlarr:managed-newznab",
        ok,
        "tag-isolated Generic Newznab configured and live"
        if ok
        else "managed indexer, private settings, tag isolation, or live test failed",
    )


def arr_download_client_accepted(
    resource: dict[str, object],
    *,
    username: str,
    category: str,
    category_fields: tuple[str, ...],
    imported_fields: tuple[str, ...],
) -> bool:
    fields = provider_fields(resource)

    def first(names: tuple[str, ...]) -> object:
        return next(
            (fields[name.casefold()] for name in names if name.casefold() in fields),
            None,
        )

    try:
        port_ok = int(fields.get("port", -1)) == 8080
    except (TypeError, ValueError):
        port_ok = False
    return bool(
        resource.get("enable")
        and str(resource.get("implementation", "")).casefold() == "qbittorrent"
        and str(fields.get("host", "")).casefold() == "qbittorrent"
        and port_ok
        and fields.get("usessl") is False
        and fields.get("username") == username
        and first(category_fields) == category
        and first(imported_fields) == f"{category}-imported"
        and resource.get("removeCompletedDownloads") is False
    )


def post_json_ok(url: str, payload: object, *, api_key: str, timeout: int = 15) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def container_check(service: str) -> Check:
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode or not container:
        return Check(f"container:{service}", False, "not created")
    inspect = subprocess.run(
        ["docker", "inspect", container], text=True, capture_output=True, check=False
    )
    if inspect.returncode:
        return Check(f"container:{service}", False, "inspect failed")
    state = json.loads(inspect.stdout)[0]["State"]
    health = state.get("Health", {}).get("Status", "none")
    ok = state.get("Status") == "running" and health == "healthy"
    return Check(
        f"container:{service}",
        ok,
        f"state={state.get('Status')} health={health}",
    )


def container_stopped_check(service: str) -> Check:
    """Require an evaluation worker to be stopped when its owner flag is off."""

    result = subprocess.run(
        ["docker", "compose", "ps", "-a", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode != 0:
        stopped = False
        detail = "container state unavailable"
    elif not container:
        stopped = True
        detail = "not created"
    else:
        inspect = subprocess.run(
            ["docker", "inspect", container],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            state = json.loads(inspect.stdout)[0]["State"]
            status = str(state.get("Status") or "")
            stopped = bool(
                inspect.returncode == 0
                and status in {"created", "exited", "dead"}
                and not state.get("Running")
                and not state.get("Restarting")
                and not state.get("Paused")
            )
            detail = status or "unknown"
        except (ValueError, TypeError, KeyError, IndexError):
            stopped = False
            detail = "inspect failed"
    return Check(
        f"container:{service}:disabled",
        stopped,
        detail if stopped else f"unsafe state={detail} while Shelfarr ownership is disabled",
    )


def writable_check(path: Path, name: str) -> Check:
    try:
        with tempfile.NamedTemporaryFile(prefix=".wysearr-validate-", dir=path) as probe:
            probe.write(b"ok")
            probe.flush()
        return Check(name, True, "writable")
    except OSError as error:
        return Check(name, False, f"not writable: {error.strerror}")


def private_published_port_check(service: str) -> Check:
    """Require every published admin port for a private service to be loopback."""

    name = f"{service}:host-access"
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode or not container:
        return Check(name, False, "container unavailable")
    inspect = subprocess.run(
        ["docker", "inspect", container],
        text=True,
        capture_output=True,
        check=False,
    )
    if inspect.returncode:
        return Check(name, False, "inspect failed")
    bindings = json.loads(inspect.stdout)[0].get("HostConfig", {}).get(
        "PortBindings", {}
    )
    host_ips = {
        str(binding.get("HostIp", ""))
        for values in bindings.values()
        for binding in values or []
    }
    private = bool(host_ips) and host_ips <= {"127.0.0.1", "::1"}
    return Check(
        name,
        private,
        "loopback only" if private else "published beyond host loopback",
    )


def shelfarr_storage_checks(storage: Path) -> list[Check]:
    """Validate Shelfarr's complete persistent state and notification boundary."""

    checks: list[Check] = []
    try:
        storage_stat = storage.stat()
        storage_private = bool(
            storage.is_dir()
            and storage_stat.st_uid == os.getuid()
            and stat.S_IMODE(storage_stat.st_mode) & 0o077 == 0
        )
    except OSError:
        storage_private = False
    checks.append(
        Check(
            "shelfarr:storage-permissions",
            storage_private,
            "owner-only directory"
            if storage_private
            else "directory must be owned by the service user and mode 0700",
        )
    )
    expected_databases = (
        "production.sqlite3",
        "production_cache.sqlite3",
        "production_queue.sqlite3",
        "production_cable.sqlite3",
    )
    database_results: list[bool] = []
    for filename in expected_databases:
        path = storage / filename
        try:
            with closing(_open_readonly_database(path)) as connection:
                database_results.append(
                    connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                )
        except (OSError, sqlite3.Error):
            database_results.append(False)
    checks.append(
        Check(
            "shelfarr:databases",
            all(database_results),
            f"integrity={sum(database_results)}/{len(expected_databases)}",
        )
    )

    secret_results: list[bool] = []
    for filename in (".secret_key_base", ".encryption_keys"):
        path = storage / filename
        try:
            secret_results.append(
                path.is_file()
                and bool(path.read_text(encoding="utf-8").strip())
                and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
            )
        except (OSError, UnicodeError):
            secret_results.append(False)
    checks.append(
        Check(
            "shelfarr:secrets",
            all(secret_results),
            "persistent and private"
            if all(secret_results)
            else "missing, empty, or accessible outside owner",
        )
    )

    discord_disabled = False
    try:
        with closing(_open_readonly_database(storage / "production.sqlite3")) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'discord_enabled'"
            ).fetchone()
        discord_disabled = row is None or not _database_truthy(row[0])
    except (OSError, sqlite3.Error):
        pass
    checks.append(
        Check(
            "shelfarr:native-discord",
            discord_disabled,
            "disabled" if discord_disabled else "enabled or unverifiable",
        )
    )
    return checks


def private_service_storage_check(path: Path, name: str) -> Check:
    """Require service state containing history or credentials to be owner-only."""

    try:
        info = path.stat()
        ok = bool(
            path.is_dir()
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) & 0o077 == 0
        )
    except OSError:
        ok = False
    return Check(
        name,
        ok,
        "owner-only directory" if ok else "directory must be owned and mode 0700",
    )


def evaluation_report_permissions_check(path: Path) -> Check:
    """Protect benchmark request titles and outcomes from other host users."""

    try:
        directory = path.stat()
        reports = list(path.glob("*.json"))
        ok = bool(
            path.is_dir()
            and not path.is_symlink()
            and directory.st_uid == os.getuid()
            and stat.S_IMODE(directory.st_mode) == 0o700
            and reports
            and all(
                report.is_file()
                and not report.is_symlink()
                and report.stat().st_uid == os.getuid()
                and stat.S_IMODE(report.stat().st_mode) == 0o600
                for report in reports
            )
        )
    except OSError:
        ok = False
    return Check(
        "shelfarr:evaluation-report-permissions",
        ok,
        "private reports" if ok else "directory/reports must be owner-only",
    )


def shelfarr_direct_staging_check(path: Path) -> Check:
    """Require Shelfarr's CIFS-incompatible private staging on local storage."""

    try:
        info = path.stat()
        ok = bool(
            path.is_dir()
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700
            and os.access(path, os.W_OK | os.X_OK)
        )
    except OSError:
        ok = False
    return Check(
        "shelfarr:direct-ebook-staging",
        ok,
        "local owner-only staging"
        if ok
        else "local staging must be writable, owned by the service user, and mode 0700",
    )


def shelfarr_configuration_checks(
    storage: Path, environment: dict[str, str]
) -> list[Check]:
    """Validate the live evaluation boundary without decrypting stored secrets."""

    checks: list[Check] = []
    try:
        with closing(_open_readonly_database(storage / "production.sqlite3")) as connection:
            settings = {
                str(row[0]): row[1]
                for row in connection.execute("SELECT key, value FROM settings")
            }
            clients = connection.execute(
                "SELECT client_type, url, category, download_path, enabled, "
                "password, api_key "
                "FROM download_clients"
            ).fetchall()
            token = environment.get("SHELFARR_API_TOKEN", "")
            token_row = connection.execute(
                "SELECT api_tokens.scopes FROM api_tokens "
                "JOIN users ON users.id = api_tokens.user_id "
                "WHERE token_digest = ? AND revoked_at IS NULL "
                "AND users.deleted_at IS NULL AND users.role = 0 "
                "AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))",
                (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return [Check("shelfarr:configuration", False, "database unavailable")]

    indexer_ok = bool(
        settings.get("indexer_provider") == "prowlarr"
        and str(settings.get("prowlarr_url") or "").rstrip("/")
        == "http://prowlarr:9696"
        and str(settings.get("prowlarr_api_key") or "").strip()
    )
    checks.append(
        Check(
            "shelfarr:prowlarr",
            indexer_ok,
            "internal provider configured" if indexer_ok else "configuration missing",
        )
    )

    flag_valid, usenet_enabled, _flag_detail = _strict_feature_flag(
        environment, USENET_FEATURE_FLAG
    )
    expected_clients = {
        "qbittorrent": ("http://qbittorrent:8080", "/downloads/shelfarr"),
        "sabnzbd": ("http://sabnzbd:8080", "/downloads/usenet"),
    }
    configured_clients: set[str] = set()
    client_states: dict[str, list[bool]] = {}
    for row in clients:
        client_type = str(row["client_type"] or "")
        if client_type in expected_clients:
            client_states.setdefault(client_type, []).append(
                _database_truthy(row["enabled"])
            )
        if (
            _database_truthy(row["enabled"])
            and client_type in expected_clients
            and str(row["url"] or "").rstrip("/") == expected_clients[client_type][0]
            and row["category"] == SHELFARR_DOWNLOAD_CATEGORY
            and row["download_path"] == expected_clients[client_type][1]
            and (
                bool(row["api_key"])
                if client_type == "sabnzbd"
                else bool(row["password"])
            )
        ):
            configured_clients.add(client_type)
    expected_enabled = {"qbittorrent"} | ({"sabnzbd"} if usenet_enabled else set())
    client_rows_ok = bool(
        all(len(client_states.get(name, [])) == 1 for name in expected_clients)
        and client_states.get("qbittorrent") == [True]
        and client_states.get("sabnzbd") == [usenet_enabled]
    )
    clients_ok = bool(
        flag_valid and client_rows_ok and configured_clients == expected_enabled
    )
    checks.append(
        Check(
            "shelfarr:download-clients",
            clients_ok,
            "qBittorrent isolated; SABnzbd follows the Usenet feature flag"
            if clients_ok
            else "missing, disabled, or not using category shelfarr",
        )
    )

    try:
        preferred_types = json.loads(
            str(settings.get("preferred_download_types") or "[]")
        )
    except json.JSONDecodeError:
        preferred_types = []
    expected_order = (
        ["direct", "usenet", "torrent"]
        if usenet_enabled
        else ["direct", "torrent"]
    )
    order_ok = flag_valid and preferred_types == expected_order
    checks.append(
        Check(
            "shelfarr:acquisition-order",
            order_ok,
            ", ".join(expected_order)
            if order_ok
            else "acquisition order does not match the Usenet feature flag",
        )
    )

    search_scope_ok = settings.get("prowlarr_tags") == ""
    checks.append(
        Check(
            "shelfarr:indexer-scope",
            search_scope_ok,
            "all Prowlarr fallback protocols visible"
            if search_scope_ok
            else "Prowlarr tag filter would hide an acquisition source",
        )
    )

    paths_ok = bool(
        settings.get("ebook_output_path") == "/ebooks"
        and settings.get("audiobook_output_path") == "/audiobooks"
        and settings.get("download_local_path") == "/downloads"
    )
    checks.append(
        Check(
            "shelfarr:paths",
            paths_ok,
            "downloads and final libraries mapped"
            if paths_ok
            else "output or download path mismatch",
        )
    )

    automation_ok = bool(
        _database_truthy(settings.get("immediate_search_enabled"))
        and _database_truthy(settings.get("auto_approve_requests"))
        and _database_truthy(settings.get("auto_select_enabled"))
        and str(settings.get("auto_select_confidence_threshold")) == "90"
        and str(settings.get("auto_select_min_seeders")) == "1"
        and settings.get("completed_download_import_mode") == "copy"
        and settings.get("default_language") == "en"
        and not _database_truthy(settings.get("auth_disabled"))
    )
    checks.append(
        Check(
            "shelfarr:automation",
            automation_ok,
            "auto-approved search, selection policy, and copy import enabled"
            if automation_ok
            else "automation, selection, language, or import policy mismatch",
        )
    )

    direct_ok = bool(
        not _database_truthy(settings.get("librivox_enabled"))
        and _database_truthy(settings.get("gutenberg_enabled"))
        and not _database_truthy(settings.get("anna_archive_enabled"))
        and not _database_truthy(settings.get("zlibrary_enabled"))
        and not _database_truthy(settings.get("ebooks_com_enabled"))
    )
    checks.append(
        Check(
            "shelfarr:direct-sources",
            direct_ok,
            "Gutenberg enabled; CIFS-incompatible and credentialed sources disabled"
            if direct_ok
            else "direct-source evaluation policy mismatch",
        )
    )

    outbound_disabled = bool(
        not _database_truthy(settings.get("discord_enabled"))
        and not str(settings.get("discord_webhook_url") or "").strip()
        and not _database_truthy(settings.get("webhook_enabled"))
        and not str(settings.get("webhook_url") or "").strip()
        and not _database_truthy(settings.get("telegram_enabled"))
    )
    checks.append(
        Check(
            "shelfarr:outbound-notifications",
            outbound_disabled,
            "Discord, webhook, and Telegram disabled"
            if outbound_disabled
            else "Shelfarr outbound notification channel enabled or configured",
        )
    )

    required_scopes = {"search:read", "requests:read", "requests:write"}
    try:
        token_scopes = set(json.loads(str(token_row[0]))) if token_row else set()
    except (json.JSONDecodeError, TypeError):
        token_scopes = set()
    token_ok = token.startswith("shf_") and token_scopes == required_scopes
    checks.append(
        Check(
            "shelfarr:huey-token",
            token_ok,
            "active least-privilege token"
            if token_ok
            else "token missing, inactive, or lacks required scopes",
        )
    )
    return checks


def _ini_section_values(path: Path, section: str) -> dict[str, str]:
    """Read scalar values from one top-level INI section without its secrets."""

    values: dict[str, str] = {}
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().casefold()
            continue
        if current == section.casefold() and "=" in line:
            key, value = line.split("=", 1)
            scalar = value.strip()
            if (
                len(scalar) >= 2
                and scalar[0] == scalar[-1]
                and scalar[0] in {"'", '"'}
            ):
                scalar = scalar[1:-1]
            values[key.strip().casefold()] = scalar
    return values


def sabnzbd_configuration_checks(
    config_path: Path,
    port: str,
    expected_username: str = "",
    environment: dict[str, str] | None = None,
    *,
    requester: object = request_json,
    server_tester: object = sabnzbd_post_json,
) -> list[Check]:
    """Validate isolated SAB paths/API and the feature-gated NNTP provider."""

    environment = dict(environment or {})

    try:
        misc = _ini_section_values(config_path, "misc")
    except (OSError, UnicodeError):
        return [Check("sabnzbd:configuration", False, "configuration unavailable")]

    paths_ok = bool(
        misc.get("download_dir") == "/downloads/incomplete/usenet"
        and misc.get("complete_dir") == "/downloads/usenet"
    )
    checks = [
        Check(
            "sabnzbd:paths",
            paths_ok,
            "isolated incomplete and complete paths"
            if paths_ok
            else "download paths do not match evaluation mounts",
        )
    ]
    auth_ok = bool(
        expected_username
        and misc.get("username") == expected_username
        and str(misc.get("password") or "").strip()
        and not _database_truthy(misc.get("api_logging"))
    )
    checks.append(
        Check(
            "sabnzbd:authentication",
            auth_ok,
            "operator authentication configured"
            if auth_ok
            else "operator auth missing/mismatched or API parameter logging enabled",
        )
    )

    api_key = misc.get("api_key", "")
    try:
        base_url = f"http://127.0.0.1:{int(port)}"
        encoded_key = urllib.parse.quote(api_key, safe="")
        version_payload = requester(
            f"{base_url}/api?mode=version&output=json&apikey={encoded_key}"
        )
        categories_payload = requester(
            f"{base_url}/api?mode=get_cats&output=json&apikey={encoded_key}"
        )
        servers_payload = requester(
            f"{base_url}/api?mode=get_config&section=servers&output=json&apikey={encoded_key}"
        )
        raw_servers = (
            servers_payload.get("config", {}).get("servers", [])
            if isinstance(servers_payload, dict)
            else []
        )
        servers = (
            list(raw_servers.values())
            if isinstance(raw_servers, dict)
            else raw_servers
        )
        enabled_servers = [
            server
            for server in servers
            if isinstance(server, dict)
            and _database_truthy(server.get("enable"))
            and bool(str(server.get("host") or "").strip())
        ]
        enabled_server = bool(enabled_servers)
        managed_servers = [
            server
            for server in servers
            if isinstance(server, dict)
            and str(server.get("name") or "").casefold()
            == MANAGED_SABNZBD_SERVER.casefold()
        ]
        managed_server = managed_servers[0] if len(managed_servers) == 1 else None
        categories = (
            categories_payload.get("categories", [])
            if isinstance(categories_payload, dict)
            else []
        )
        api_ok = bool(
            api_key
            and isinstance(version_payload, dict)
            and version_payload.get("version")
            and isinstance(categories, list)
            and SHELFARR_DOWNLOAD_CATEGORY in categories
        )
        detail = (
            f"version={version_payload.get('version')} category=shelfarr"
            if api_ok
            else "API unavailable or Shelfarr category missing"
        )
    except (TypeError, ValueError, OSError, urllib.error.URLError):
        api_ok = False
        enabled_server = False
        managed_server = None
        managed_servers = []
        detail = "API unavailable or Shelfarr category missing"
    checks.append(Check("sabnzbd:api-category", api_ok, detail))
    checks.append(
        Check(
            "sabnzbd:provider-observation",
            True,
            "enabled Usenet provider configured"
            if enabled_server
            else "no enabled provider; Usenet acquisition unavailable",
        )
    )

    flag_valid, usenet_enabled, flag_detail = _strict_feature_flag(
        environment, USENET_FEATURE_FLAG
    )
    if not flag_valid:
        checks.append(
            Check(
                "sabnzbd:usenet-provider",
                False,
                "provider validation blocked by invalid feature flag",
            )
        )
        return checks

    if not usenet_enabled:
        provider_disabled = len(managed_servers) <= 1 and not (
            isinstance(managed_server, dict)
            and _database_truthy(managed_server.get("enable"))
        )
        checks.append(
            Check(
                "sabnzbd:usenet-provider",
                provider_disabled,
                "disabled"
                if provider_disabled
                else "managed NNTP provider is enabled while feature is disabled",
            )
        )
        return checks

    required = (
        "USENET_SERVER_HOST",
        "USENET_SERVER_USERNAME",
        "USENET_SERVER_PASSWORD",
        "USENET_SERVER_CONNECTIONS",
    )
    missing = [key for key in required if not environment.get(key, "").strip()]
    ssl_value = environment.get("USENET_SERVER_SSL", "true").strip()
    try:
        provider_port = int(environment.get("USENET_SERVER_PORT", "563"))
        connections = int(environment.get("USENET_SERVER_CONNECTIONS", ""))
        retention = int(environment.get("USENET_SERVER_RETENTION", "0"))
        numeric_ok = 1 <= provider_port <= 65535 and 1 <= connections <= 500 and retention >= 0
    except (TypeError, ValueError):
        provider_port = 0
        connections = 0
        retention = -1
        numeric_ok = False
    provider_env_ok = not missing and ssl_value == "true" and numeric_ok
    ssl_enabled = True

    live_ok = False
    if provider_env_ok and isinstance(managed_server, dict):
        try:
            password_echo = str(managed_server.get("password") or "")
            live_ok = bool(
                _database_truthy(managed_server.get("enable"))
                and str(managed_server.get("displayname") or "")
                == MANAGED_SABNZBD_SERVER
                and str(managed_server.get("host") or "").casefold()
                == environment["USENET_SERVER_HOST"].strip().casefold()
                and int(managed_server.get("port", 0)) == provider_port
                and str(managed_server.get("username") or "")
                == environment["USENET_SERVER_USERNAME"]
                and int(managed_server.get("connections", 0)) == connections
                and _database_truthy(managed_server.get("ssl")) == ssl_enabled
                and int(managed_server.get("ssl_verify", -1)) == 3
                and int(managed_server.get("retention", -1)) == retention
                and int(managed_server.get("priority", -1)) == 0
                and password_echo
                and not password_echo.strip("*")
            )
        except (TypeError, ValueError):
            live_ok = False

    connection_ok = False
    # Never send the private NNTP password through SAB's test endpoint while
    # parameter logging is enabled or operator authentication is incomplete.
    if auth_ok and provider_env_ok and live_ok and api_ok:
        try:
            tested = server_tester(
                port,
                api_key,
                {
                    "mode": "config",
                    "name": "test_server",
                    "server": MANAGED_SABNZBD_SERVER,
                    "host": environment["USENET_SERVER_HOST"],
                    "port": str(provider_port),
                    "username": environment["USENET_SERVER_USERNAME"],
                    "password": environment["USENET_SERVER_PASSWORD"],
                    "connections": str(connections),
                    "ssl": "1" if ssl_enabled else "0",
                    "ssl_verify": "3",
                },
            )
            test_value = tested.get("value") if isinstance(tested, dict) else None
            connection_ok = bool(
                isinstance(test_value, dict) and test_value.get("result") is True
            )
        except (TypeError, ValueError, OSError, urllib.error.URLError):
            connection_ok = False

    provider_ok = provider_env_ok and live_ok and connection_ok
    checks.append(
        Check(
            "sabnzbd:usenet-provider",
            provider_ok,
            "managed TLS provider configured and connection-tested"
            if provider_ok
            else (
                "required private provider settings are missing or invalid"
                if not provider_env_ok
                else "managed provider configuration or connection test failed"
            ),
        )
    )
    return checks


def sabnzbd_stopped_managed_provider_check(config_path: Path) -> Check:
    """Verify the managed NNTP server is disabled while SAB itself is stopped."""

    if not config_path.exists():
        return Check("sabnzbd:usenet-provider", True, "absent")
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return Check("sabnzbd:usenet-provider", False, "configuration unavailable")

    in_servers = False
    current_name: str | None = None
    current_values: dict[str, str] = {}
    servers: list[tuple[str, dict[str, str]]] = []

    def finish_current() -> None:
        nonlocal current_name, current_values
        if current_name is not None:
            servers.append((current_name, current_values))
        current_name = None
        current_values = {}

    for raw_line in lines:
        line = raw_line.strip()
        if line == "[servers]":
            finish_current()
            in_servers = True
            continue
        if in_servers and line.startswith("[[") and line.endswith("]]"):
            finish_current()
            current_name = line[2:-2].strip()
            continue
        if line.startswith("[") and line.endswith("]"):
            if in_servers:
                finish_current()
            in_servers = False
            continue
        if in_servers and current_name is not None and "=" in line:
            key, raw_value = line.split("=", 1)
            value = raw_value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            current_values[key.strip().casefold()] = value
    finish_current()

    managed = [
        values
        for section_name, values in servers
        if section_name.casefold() == MANAGED_SABNZBD_SERVER.casefold()
        or str(values.get("name") or values.get("displayname") or "").casefold()
        == MANAGED_SABNZBD_SERVER.casefold()
    ]
    if len(managed) > 1:
        return Check(
            "sabnzbd:usenet-provider", False, "duplicate managed providers"
        )
    if not managed:
        return Check("sabnzbd:usenet-provider", True, "absent")
    disabled = not _database_truthy(managed[0].get("enable", "1"))
    return Check(
        "sabnzbd:usenet-provider",
        disabled,
        "disabled" if disabled else "managed NNTP provider remains enabled",
    )


def shelfarr_runtime_checks(
    port: str,
    api_token: str,
    usenet_enabled: bool = False,
    *,
    runner: object = subprocess.run,
    requester: object = request_json,
) -> list[Check]:
    """Verify Huey's scoped API token and Shelfarr's live client adapters."""

    checks: list[Check] = []
    try:
        request_payload = requester(
            f"http://127.0.0.1:{int(port)}/api/v1/requests?limit=1",
            headers={"Authorization": f"Bearer {api_token}"},
        )
        api_ok = isinstance(request_payload, dict) and isinstance(
            request_payload.get("requests"), list
        )
    except (TypeError, ValueError, OSError, urllib.error.URLError):
        api_ok = False
    checks.append(
        Check(
            "shelfarr:huey-api",
            api_ok,
            "scoped request API authenticated" if api_ok else "authentication failed",
        )
    )

    sentinel = "WYSEARR_CLIENT_RESULTS="
    code = (
        "items=DownloadClient.enabled.order(:client_type).map{"
        "|c| [c.client_type,c.category,c.test_connection]};"
        f"puts({json.dumps(sentinel)}+JSON.generate(items))"
    )
    try:
        result = runner(
            [
                "docker", "compose", "exec", "-T", "--user",
                f"{os.getuid()}:{os.getgid()}", "shelfarr",
                "ruby", "/opt/wysearr/shelfarr_exec.rb",
                "bin/rails", "runner", code,
            ],
            cwd=STACK_ROOT,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        payload_line = next(
            (
                line[len(sentinel):]
                for line in reversed(result.stdout.splitlines())
                if line.startswith(sentinel)
            ),
            "[]",
        )
        items = json.loads(payload_line) if result.returncode == 0 else []
        expected = {"qbittorrent"} | ({"sabnzbd"} if usenet_enabled else set())
        working = {
            str(item[0])
            for item in items
            if isinstance(item, list)
            and len(item) == 3
            and item[1] == SHELFARR_DOWNLOAD_CATEGORY
            and item[2] is True
        }
        clients_ok = expected == working
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        clients_ok = False
    checks.append(
        Check(
            "shelfarr:client-connectivity",
            clients_ok,
            "enabled download-client live tests passed"
            if clients_ok
            else "one or more client tests failed",
        )
    )
    return checks


def bazarr_acceptance(
    settings: dict[str, object], profiles: list[dict[str, object]], status: dict[str, object]
) -> tuple[bool, bool, bool]:
    general = settings.get("general", {})
    if not isinstance(general, dict):
        return False, False, False
    integrations_ok = bool(
        general.get("use_sonarr")
        and general.get("use_radarr")
        and status.get("sonarr_version")
        and status.get("radarr_version")
    )
    english_ids = {
        profile.get("profileId")
        for profile in profiles
        if str(profile.get("name", "")).casefold() == "english"
        and profile.get("profileId") is not None
    }
    profile_ok = bool(
        english_ids
        and general.get("serie_default_enabled")
        and general.get("movie_default_enabled")
        and general.get("serie_default_profile") in english_ids
        and general.get("movie_default_profile") in english_ids
    )
    configured_providers = set(general.get("enabled_providers") or [])
    providers_ok = BAZARR_PROVIDERS <= configured_providers
    return integrations_ok, profile_ok, providers_ok


def validate() -> list[Check]:
    env = load_env(STACK_ROOT / ".env")
    bind_address = env.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    checks: list[Check] = []

    compose = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=STACK_ROOT,
        capture_output=True,
        check=False,
    )
    checks.append(Check("compose", compose.returncode == 0, "configuration valid" if compose.returncode == 0 else "configuration invalid"))
    checks.append(
        channel_inventory_check(STACK_ROOT / "docs" / "huey-channels.yml")
    )
    checks.append(huey_ready_check(STACK_ROOT / "state" / "huey" / "ready"))
    checks.append(huey_selection_ttl_check(env))

    media_root = Path(env.get("MEDIA_ROOT", "/mnt/media"))
    torrent_root = Path(env.get("TORRENT_ROOT", str(STACK_ROOT / "state" / "torrents")))
    checks.append(Check("media:mount", os.path.ismount(media_root), f"{media_root}"))
    if media_root.is_dir():
        checks.append(writable_check(media_root, "media:writable"))
    else:
        checks.append(Check("media:writable", False, "directory missing"))
    checks.append(writable_check(torrent_root, "torrents:writable") if torrent_root.is_dir() else Check("torrents:writable", False, "directory missing"))

    expected_dirs = [
        torrent_root / "incomplete",
        torrent_root / "incomplete" / "usenet",
        torrent_root / "usenet",
        torrent_root / SHELFARR_DOWNLOAD_CATEGORY,
    ]
    expected_dirs += [torrent_root / category for category in ARR_CATEGORIES + DIRECT_CATEGORIES]
    missing = [str(path) for path in expected_dirs if not path.is_dir()]
    checks.append(Check("torrents:paths", not missing, "all category paths exist" if not missing else f"{len(missing)} paths missing"))

    feature_flag = env.get("SHELFARR_ENABLED", "false").strip()
    feature_flag_ok = feature_flag in {"true", "false", ""}
    checks.append(
        Check(
            "shelfarr:feature-flag",
            feature_flag_ok,
            f"literal {feature_flag or 'false'}"
            if feature_flag_ok
            else "must be literal true or false",
        )
    )
    shelfarr_enabled = feature_flag == "true"
    usenet_flag_valid, usenet_enabled, usenet_flag_detail = _strict_feature_flag(
        env, USENET_FEATURE_FLAG
    )
    checks.append(
        Check("usenet:feature-flag", usenet_flag_valid, usenet_flag_detail)
    )
    checks.append(
        Check(
            "usenet:ownership-boundary",
            not usenet_enabled or shelfarr_enabled,
            "Shelfarr owns enabled book Usenet acquisition"
            if usenet_enabled and shelfarr_enabled
            else (
                "disabled"
                if not usenet_enabled
                else "Usenet cannot be enabled while Shelfarr ownership is disabled"
            ),
        )
    )
    services = CORE_SERVICES + (EVALUATION_SERVICES if shelfarr_enabled else ())
    for service in services:
        checks.append(container_check(service))
    if not shelfarr_enabled:
        for service in EVALUATION_SERVICES:
            checks.append(container_stopped_check(service))
        checks.append(
            sabnzbd_stopped_managed_provider_check(
                STACK_ROOT / "config" / "sabnzbd" / "sabnzbd.ini"
            )
        )
    if shelfarr_enabled:
        checks.append(private_published_port_check("sabnzbd"))
        checks.append(private_published_port_check("shelfarr"))
        for relative, name in (
            (Path("ebooks") / "Books", "shelfarr:ebooks-output"),
            (Path("audiobooks"), "shelfarr:audiobooks-output"),
        ):
            path = media_root / relative
            checks.append(
                writable_check(path, name)
                if path.is_dir()
                else Check(name, False, "directory missing")
            )
        checks.extend(shelfarr_storage_checks(STACK_ROOT / "config" / "shelfarr"))
        checks.append(
            shelfarr_direct_staging_check(
                STACK_ROOT / "state" / "shelfarr-staging" / "ebooks"
            )
        )
        checks.append(
            evaluation_report_permissions_check(
                STACK_ROOT / "state" / "shelfarr-evaluation"
            )
        )
        checks.extend(
            shelfarr_configuration_checks(
                STACK_ROOT / "config" / "shelfarr", env
            )
        )
        checks.extend(
            sabnzbd_configuration_checks(
                STACK_ROOT / "config" / "sabnzbd" / "sabnzbd.ini",
                env.get("SABNZBD_ADMIN_PORT", "8085"),
                env.get("SABNZBD_ADMIN_USERNAME", ""),
                env,
            )
        )
        checks.append(
            private_service_storage_check(
                STACK_ROOT / "config" / "sabnzbd", "sabnzbd:storage-permissions"
            )
        )
        checks.extend(
            shelfarr_runtime_checks(
                env.get("SHELFARR_ADMIN_PORT", "5056"),
                env.get("SHELFARR_API_TOKEN", ""),
                usenet_enabled,
            )
        )

    try:
        qbit_url = f"http://{bind_address}:{env.get('QBITTORRENT_PORT', '8080')}"
        opener = qbit_opener(
            qbit_url,
            env.get("QBITTORRENT_USERNAME", "admin"),
            env.get("QBITTORRENT_PASSWORD", ""),
        )
        with opener.open(f"{qbit_url}/api/v2/app/version", timeout=15) as response:
            version = response.read().decode().strip()
        with opener.open(f"{qbit_url}/api/v2/torrents/categories", timeout=15) as response:
            categories = json.load(response)
        expected = set(ARR_CATEGORIES + DIRECT_CATEGORIES)
        expected |= {f"{category}-imported" for category in expected}
        expected.add(SHELFARR_DOWNLOAD_CATEGORY)
        missing_categories = sorted(expected - set(categories))
        wrong_category_paths = sorted(
            category
            for category in expected.intersection(categories)
            if categories[category].get("savePath")
            != f"/downloads/{category.removesuffix('-imported')}"
        )
        checks.append(Check("qbittorrent:api", True, f"version={version}"))
        checks.append(Check(
            "qbittorrent:categories",
            not missing_categories and not wrong_category_paths,
            "categories and shared imported paths configured"
            if not missing_categories and not wrong_category_paths
            else f"{len(missing_categories)} missing, {len(wrong_category_paths)} misrouted",
        ))
    except Exception as error:  # validation must aggregate all failures
        checks.append(Check("qbittorrent:api", False, type(error).__name__))

    prowlarr_key = env.get("PROWLARR_API_KEY", "")
    try:
        prowlarr_url = f"http://{bind_address}:{env.get('PROWLARR_PORT', '9696')}"
        status = request_json(f"{prowlarr_url}/api/v1/system/status", api_key=prowlarr_key)
        indexers = request_json(f"{prowlarr_url}/api/v1/indexer", api_key=prowlarr_key)
        applications = request_json(f"{prowlarr_url}/api/v1/applications", api_key=prowlarr_key)
        tags = request_json(f"{prowlarr_url}/api/v1/tag", api_key=prowlarr_key)
        app_names = {item.get("name") for item in applications if item.get("enable", True)}
        required_apps = {"Sonarr", "Radarr", "Lidarr", "Whisparr"}
        checks.append(Check("prowlarr:api", bool(status.get("version")), f"version={status.get('version')}"))
        enabled_indexers = sum(bool(item.get("enable")) for item in indexers)
        managed_newznab_name = MANAGED_NEWZNAB_INDEXER_DEFAULT.casefold()
        managed_newznab_id = next(
            (
                item.get("id")
                for item in indexers
                if str(item.get("name") or "").casefold()
                == managed_newznab_name
                and isinstance(item.get("id"), int)
            ),
            None,
        )
        live_indexer = False
        live_protocols: set[str] = set()
        live_indexer_ids: set[int] = set()
        for indexer in indexers:
            if not indexer.get("enable"):
                continue
            try:
                post_json_ok(
                    f"{prowlarr_url}/api/v1/indexer/test",
                    indexer,
                    api_key=prowlarr_key,
                    timeout=60,
                )
                live_indexer = True
                protocol = str(indexer.get("protocol", "")).strip().casefold()
                if protocol in {"torrent", "usenet"}:
                    live_protocols.add(protocol)
                if isinstance(indexer.get("id"), int):
                    live_indexer_ids.add(indexer["id"])
                if (
                    "torrent" in live_protocols
                    and (
                        not usenet_enabled
                        or (
                            "usenet" in live_protocols
                            and managed_newznab_id in live_indexer_ids
                        )
                    )
                ):
                    break
            except Exception:
                continue
        checks.append(Check(
            "prowlarr:indexers",
            enabled_indexers > 0 and live_indexer,
            f"enabled={enabled_indexers} live_test={live_indexer}",
        ))
        required_book_protocols = {"torrent"} | ({"usenet"} if usenet_enabled else set())
        checks.append(
            Check(
                "prowlarr:book-protocols",
                required_book_protocols <= live_protocols,
                "live torrent indexer; "
                + ("Usenet indexer available" if "usenet" in live_protocols else "Usenet indexer unavailable")
                if required_book_protocols <= live_protocols
                else "missing a live torrent indexer",
            )
        )
        checks.append(
            prowlarr_managed_newznab_check(
                indexers,
                tags,
                applications,
                live_indexer_ids,
                env,
                enabled=usenet_enabled,
                flag_valid=usenet_flag_valid,
            )
        )
        checks.append(Check("prowlarr:applications", required_apps <= app_names, f"configured={len(app_names)}"))
    except Exception as error:
        checks.append(Check("prowlarr:api", False, type(error).__name__))

    arr_specs = (
        ("sonarr", "SONARR", "8989", "v3", "/media/tv", "tv", ("category", "tvCategory"), ("postImportCategory", "tvImportedCategory")),
        ("radarr", "RADARR", "7878", "v3", "/media/movies", "movies", ("category", "movieCategory"), ("postImportCategory", "movieImportedCategory")),
        ("lidarr", "LIDARR", "8686", "v1", "/media/music", "music", ("category", "musicCategory"), ("postImportCategory", "musicImportedCategory")),
        ("whisparr", "WHISPARR", "6969", "v3", "/media/spicy", "spicy", ("category", "tvCategory"), ("postImportCategory", "tvImportedCategory")),
    )
    for name, prefix, default_port, api_version, root_path, category, category_fields, imported_fields in arr_specs:
        try:
            base = f"http://{bind_address}:{env.get(prefix + '_PORT', default_port)}"
            key = env.get(prefix + "_API_KEY", "")
            status = request_json(f"{base}/api/{api_version}/system/status", api_key=key)
            roots = request_json(f"{base}/api/{api_version}/rootfolder", api_key=key)
            clients = request_json(f"{base}/api/{api_version}/downloadclient", api_key=key)
            indexers = request_json(f"{base}/api/{api_version}/indexer", api_key=key)
            root_ok = any(item.get("path") == root_path and item.get("accessible", True) for item in roots)
            accepted_clients = [
                item
                for item in clients
                if arr_download_client_accepted(
                    item,
                    username=env.get("QBITTORRENT_USERNAME", "admin"),
                    category=category,
                    category_fields=category_fields,
                    imported_fields=imported_fields,
                )
            ]
            client_ok = False
            for client in accepted_clients:
                try:
                    post_json_ok(
                        f"{base}/api/{api_version}/downloadclient/test",
                        client,
                        api_key=key,
                    )
                    client_ok = True
                    break
                except Exception:
                    continue
            checks.append(Check(f"{name}:api", bool(status.get("version")), f"version={status.get('version')}"))
            checks.append(Check(f"{name}:root", root_ok, root_path))
            checks.append(Check(f"{name}:download-client", client_ok, "configuration and live test passed" if client_ok else "configuration or live test failed"))
            enabled_indexers = sum(bool(item.get("enable", True)) for item in indexers)
            live_indexer = False
            for indexer in indexers:
                if not indexer.get("enable", True):
                    continue
                try:
                    post_json_ok(
                        f"{base}/api/{api_version}/indexer/test",
                        indexer,
                        api_key=key,
                        timeout=60,
                    )
                    live_indexer = True
                    break
                except Exception:
                    continue
            checks.append(Check(
                f"{name}:indexers",
                enabled_indexers > 0 and live_indexer,
                f"enabled={enabled_indexers} live_test={live_indexer}",
            ))
            if name == "whisparr":
                qualities = request_json(f"{base}/api/v3/qualityprofile", api_key=key)
                checks.append(Check("whisparr:quality-profiles", bool(qualities), f"configured={len(qualities)}"))
        except Exception as error:
            checks.append(Check(f"{name}:api", False, type(error).__name__))

    try:
        bazarr_port = env.get("BAZARR_PORT", "6767")
        bazarr_url = f"http://{bind_address}:{bazarr_port}"
        yaml_text = (STACK_ROOT / "config" / "bazarr" / "config" / "config.yaml").read_text(encoding="utf-8")
        auth_block = yaml_text.split("auth:", 1)[1].split("\n", 1)[1]
        bazarr_key = next(
            line.split(":", 1)[1].strip()
            for line in auth_block.splitlines()
            if line.startswith("  apikey:")
        )
        headers = {"X-API-KEY": bazarr_key}
        settings_request = urllib.request.Request(f"{bazarr_url}/api/system/settings", headers=headers)
        profiles_request = urllib.request.Request(f"{bazarr_url}/api/system/languages/profiles", headers=headers)
        status_request = urllib.request.Request(f"{bazarr_url}/api/system/status", headers=headers)
        with urllib.request.urlopen(settings_request, timeout=15) as response:
            settings = json.load(response)
        with urllib.request.urlopen(profiles_request, timeout=15) as response:
            profiles = json.load(response)
        with urllib.request.urlopen(status_request, timeout=15) as response:
            status = json.load(response)["data"]
        integrated, profile_ok, providers_ok = bazarr_acceptance(
            settings, profiles, status
        )
        checks.append(Check("bazarr:arr-integration", integrated, f"sonarr={bool(status.get('sonarr_version'))} radarr={bool(status.get('radarr_version'))}"))
        checks.append(Check("bazarr:language-profile", profile_ok, "English defaults configured" if profile_ok else "English defaults missing"))
        checks.append(Check("bazarr:providers", providers_ok, "required providers enabled" if providers_ok else "required providers missing"))
    except Exception as error:
        checks.append(Check("bazarr:api", False, type(error).__name__))

    checks.append(
        huey_database_check(STACK_ROOT / "state" / "huey" / "huey.db")
    )

    try:
        database = STACK_ROOT / "config" / "bookbot" / "bookbot.db"
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        required_tables = {"imports", "events", "recent_additions"}
        checks.append(Check(
            "bookbot:database",
            integrity == "ok" and required_tables <= tables,
            "integrity and schema valid",
        ))
    except Exception as error:
        checks.append(Check("bookbot:database", False, type(error).__name__))

    for service, relative_database in ARR_NOTIFICATION_DATABASES.items():
        checks.append(
            arr_native_discord_check(service, STACK_ROOT / relative_database)
        )
    checks.append(
        bazarr_native_discord_check(
            STACK_ROOT / "config" / "bazarr" / "db" / "bazarr.db",
            STACK_ROOT / "config" / "bazarr" / "config" / "config.yaml",
        )
    )

    token_present = len(env.get("DISCORD_BOT_TOKEN", "")) >= 20
    checks.append(Check("huey:token", token_present, "configured" if token_present else "missing"))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    checks = validate()
    passed = all(check.ok for check in checks)
    if args.as_json:
        print(json.dumps({"passed": passed, "checks": [asdict(check) for check in checks]}, indent=2))
    else:
        for check in checks:
            print(f"{'PASS' if check.ok else 'FAIL'}: {check.name}: {check.detail}")
        print(f"{'PASS' if passed else 'FAIL'}: {sum(item.ok for item in checks)}/{len(checks)} production checks passed")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
