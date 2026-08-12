#!/usr/bin/env python3
"""Read-only production validation for the WyseARR stack."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
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
DIRECT_CATEGORIES = ("ebooks", "audiobooks", "manga-comics", "roms", "sheet-music")
ARR_CATEGORIES = ("tv", "movies", "music", "spicy")
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
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
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


def request_json(url: str, *, api_key: str | None = None, timeout: int = 15) -> object:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    request = urllib.request.Request(url, headers=headers)
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


def writable_check(path: Path, name: str) -> Check:
    try:
        with tempfile.NamedTemporaryFile(prefix=".wysearr-validate-", dir=path) as probe:
            probe.write(b"ok")
            probe.flush()
        return Check(name, True, "writable")
    except OSError as error:
        return Check(name, False, f"not writable: {error.strerror}")


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

    media_root = Path(env.get("MEDIA_ROOT", "/mnt/media"))
    torrent_root = Path(env.get("TORRENT_ROOT", str(STACK_ROOT / "state" / "torrents")))
    checks.append(Check("media:mount", os.path.ismount(media_root), f"{media_root}"))
    if media_root.is_dir():
        checks.append(writable_check(media_root, "media:writable"))
    else:
        checks.append(Check("media:writable", False, "directory missing"))
    checks.append(writable_check(torrent_root, "torrents:writable") if torrent_root.is_dir() else Check("torrents:writable", False, "directory missing"))

    expected_dirs = [torrent_root / "incomplete"]
    expected_dirs += [torrent_root / category for category in ARR_CATEGORIES + DIRECT_CATEGORIES]
    missing = [str(path) for path in expected_dirs if not path.is_dir()]
    checks.append(Check("torrents:paths", not missing, "all category paths exist" if not missing else f"{len(missing)} paths missing"))

    for service in SERVICES:
        checks.append(container_check(service))

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
        app_names = {item.get("name") for item in applications if item.get("enable", True)}
        required_apps = {"Sonarr", "Radarr", "Lidarr", "Whisparr"}
        checks.append(Check("prowlarr:api", bool(status.get("version")), f"version={status.get('version')}"))
        enabled_indexers = sum(bool(item.get("enable")) for item in indexers)
        live_indexer = False
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
                break
            except Exception:
                continue
        checks.append(Check(
            "prowlarr:indexers",
            enabled_indexers > 0 and live_indexer,
            f"enabled={enabled_indexers} live_test={live_indexer}",
        ))
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

    try:
        database = STACK_ROOT / "state" / "huey" / "huey.db"
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(requests)")}
            indexes = list(connection.execute("PRAGMA index_list(requests)"))
            delivery_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(notification_deliveries)"
                )
            }
            delivery_indexes = list(
                connection.execute("PRAGMA index_list(notification_deliveries)")
            )
        required_columns = {"status", "updated_at", "service", "external_id", "error"}
        unique_message = any(row[2] for row in indexes)
        required_delivery_columns = {
            "request_id",
            "event_key",
            "route",
            "message",
            "delivered_at",
        }
        unique_delivery = any(row[2] for row in delivery_indexes)
        schema_ok = bool(
            required_columns <= columns
            and unique_message
            and required_delivery_columns <= delivery_columns
            and unique_delivery
        )
        checks.append(
            Check(
                "huey:database",
                integrity == "ok" and schema_ok,
                "integrity, request schema, and notification outbox valid",
            )
        )
    except Exception as error:
        checks.append(Check("huey:database", False, type(error).__name__))

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
