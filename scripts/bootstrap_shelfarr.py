#!/usr/bin/env python3
"""Converge the controlled Shelfarr/SABnzbd evaluation without leaking secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .bootstrap import (
        BootstrapError,
        QbittorrentClient,
        load_dotenv,
        update_dotenv,
    )
except ImportError:
    from bootstrap import (
        BootstrapError,
        QbittorrentClient,
        load_dotenv,
        update_dotenv,
    )


STACK_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SENTINEL = "WYSEARR_BOOTSTRAP_RESULT="
BOOKBOT_EBOOK_CATEGORIES = (
    "ebooks",
    "ebooks-imported",
)
MANAGED_USENET_SERVER_NAME = "WyseARR Primary"
INI_SECTION_RE = re.compile(
    r"^\s*(?P<open>\[+)(?P<name>[^\[\]]+)(?P<close>\]+)\s*(?:[#;].*)?$"
)


class ManagedUsenetSettings:
    """Validated configuration for the one SAB server owned by WyseARR."""

    __slots__ = (
        "enabled",
        "host",
        "port",
        "username",
        "password",
        "connections",
        "ssl",
        "retention",
    )

    def __init__(
        self,
        *,
        enabled: bool,
        host: str = "",
        port: int = 563,
        username: str = "",
        password: str = "",
        connections: int = 0,
        ssl: bool = True,
        retention: int = 0,
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.connections = connections
        self.ssl = ssl
        self.retention = retention

    def __repr__(self) -> str:
        return "ManagedUsenetSettings(<private>)"


def parse_managed_usenet_settings(
    environment: Mapping[str, str],
) -> ManagedUsenetSettings:
    """Validate the opt-in provider contract before any live configuration."""

    enabled = environment.get("WYSEARR_USENET_ENABLED", "")
    if enabled not in {"", "true", "false"}:
        raise BootstrapError(
            "WYSEARR_USENET_ENABLED must be literal true, false, or blank"
        )
    if enabled in {"", "false"}:
        return ManagedUsenetSettings(enabled=False)

    required: dict[str, str] = {}
    for name in (
        "USENET_SERVER_HOST",
        "USENET_SERVER_USERNAME",
        "USENET_SERVER_PASSWORD",
        "USENET_SERVER_CONNECTIONS",
    ):
        raw_value = environment.get(name, "")
        if not raw_value or not raw_value.strip():
            raise BootstrapError(f"Required private setting is missing: {name}")
        required[name] = raw_value

    def integer_setting(
        name: str,
        default: str | None,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        raw_value = environment.get(name, "")
        if not raw_value and default is not None:
            raw_value = default
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise BootstrapError(f"{name} must be numeric") from exc
        if parsed < minimum or (maximum is not None and parsed > maximum):
            constraint = (
                f"between {minimum} and {maximum}"
                if maximum is not None
                else f"at least {minimum}"
            )
            raise BootstrapError(f"{name} must be {constraint}")
        return parsed

    ssl_value = environment.get("USENET_SERVER_SSL", "") or "true"
    if ssl_value != "true":
        raise BootstrapError("USENET_SERVER_SSL must be literal true")

    return ManagedUsenetSettings(
        enabled=True,
        host=required["USENET_SERVER_HOST"].strip().casefold(),
        port=integer_setting(
            "USENET_SERVER_PORT", "563", minimum=1, maximum=65535
        ),
        username=required["USENET_SERVER_USERNAME"].strip(),
        password=required["USENET_SERVER_PASSWORD"],
        connections=integer_setting(
            "USENET_SERVER_CONNECTIONS", None, minimum=1, maximum=500
        ),
        ssl=True,
        retention=integer_setting("USENET_SERVER_RETENTION", "0", minimum=0),
    )


def require_drained_bookbot_ebook_categories(
    environment: Mapping[str, str],
    *,
    client_factory=QbittorrentClient,
) -> None:
    """Refuse a Shelfarr ebook transition while BookBot ebook jobs exist."""

    bind_address = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    base_url = (
        "http://" + bind_address + ":" + environment.get("QBITTORRENT_PORT", "8080")
    )
    client = client_factory(base_url)
    if not client.login(
        environment.get("QBITTORRENT_USERNAME", "admin"),
        environment.get("QBITTORRENT_PASSWORD", ""),
    ):
        raise BootstrapError("qBittorrent rejected the drain-check credentials")
    active = [
        category
        for category in BOOKBOT_EBOOK_CATEGORIES
        if client.torrents(category)
    ]
    if active:
        raise BootstrapError(
            "Cannot enable Shelfarr while BookBot ebook-category torrents remain: "
            + ", ".join(active)
        )


def _sab_request(
    port: int,
    api_key: str,
    parameters: Mapping[str, str],
    *,
    timeout: float = 20,
) -> Any:
    body = urllib.parse.urlencode(
        {"output": "json", "apikey": api_key, **dict(parameters)}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/api",
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            timeout=timeout,
        ) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise BootstrapError("SABnzbd API configuration failed") from exc


def _read_sab_api_key(path: Path) -> str:
    misc = _read_sab_misc(path)
    api_key = misc.get("api_key", "")
    if api_key:
        return api_key
    raise BootstrapError("SABnzbd API key is missing")


def _read_sab_misc(path: Path) -> dict[str, str]:
    """Read scalar SAB misc values without logging their contents."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BootstrapError("SABnzbd configuration is unavailable") from exc
    section = ""
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
        elif section == "misc" and "=" in line:
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


def _sab_misc_write_confirmed(result: Any, key: str, desired: str) -> bool:
    """Validate SAB 5's nested set_config echo without exposing secrets."""

    if not isinstance(result, dict):
        return False
    config = result.get("config")
    misc = config.get("misc") if isinstance(config, dict) else None
    if not isinstance(misc, dict) or key not in misc:
        return False
    actual = misc[key]
    if key == "password":
        return isinstance(actual, str) and bool(actual) and set(actual) == {"*"}
    if key == "api_logging":
        return actual in {False, 0, "0", "false", "False"}
    if key == "host_whitelist":
        if isinstance(actual, list):
            return "sabnzbd" in {str(item).strip() for item in actual}
        return "sabnzbd" in {
            item.strip() for item in str(actual or "").split(",") if item.strip()
        }
    return str(actual) == desired


def _sab_category_write_confirmed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    config = result.get("config")
    categories = config.get("categories") if isinstance(config, dict) else None
    if not isinstance(categories, list) or len(categories) != 1:
        return False
    category = categories[0]
    return bool(
        isinstance(category, dict)
        and category.get("name") == "shelfarr"
        and category.get("dir") == "shelfarr"
        and str(category.get("pp")) == "3"
        and str(category.get("priority")) == "0"
    )


def _sab_server_list(result: Any) -> list[dict[str, Any]]:
    """Return the public, password-masked SAB server records."""

    if not isinstance(result, dict) or result.get("status") is False:
        raise BootstrapError("SABnzbd returned invalid server configuration")
    config = result.get("config")
    if not isinstance(config, dict):
        raise BootstrapError("SABnzbd returned invalid server configuration")
    # SAB 5 omits the `servers` key entirely when no server exists. A present
    # key must still use the pinned public API's list-of-records contract.
    if "servers" not in config:
        if config:
            raise BootstrapError("SABnzbd returned invalid server configuration")
        return []
    servers: Any = config["servers"]
    if not isinstance(servers, list) or not all(
        isinstance(server, dict) for server in servers
    ):
        raise BootstrapError("SABnzbd returned invalid server configuration")
    return servers


def _sab_boolean(value: Any) -> bool | None:
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    return None


def _sab_masked_password(value: Any, *, allow_empty: bool = False) -> bool:
    if value == "" and allow_empty:
        return True
    return isinstance(value, str) and bool(value) and set(value) == {"*"}


def _sab_server_test_confirmed(result: Any) -> bool:
    if not isinstance(result, dict) or result.get("status") is False:
        return False
    value = result.get("value")
    return isinstance(value, dict) and value.get("result") is True


def _sab_server_write_confirmed(
    result: Any,
    settings: ManagedUsenetSettings,
    *,
    enabled: bool,
) -> bool:
    """Validate a masked server echo without retaining or reporting secrets."""

    if not isinstance(result, dict) or result.get("status") is False:
        return False
    try:
        servers = _sab_server_list(result)
    except BootstrapError:
        return False
    if len(servers) != 1:
        return False
    server = servers[0]
    if (
        server.get("name") != MANAGED_USENET_SERVER_NAME
        or _sab_boolean(server.get("enable")) is not enabled
        or not _sab_masked_password(
            server.get("password"), allow_empty=not enabled
        )
    ):
        return False
    if not enabled:
        return True
    return bool(
        server.get("displayname") == MANAGED_USENET_SERVER_NAME
        and str(server.get("host", "")).casefold() == settings.host
        and str(server.get("port")) == str(settings.port)
        and server.get("username") == settings.username
        and str(server.get("connections")) == str(settings.connections)
        and _sab_boolean(server.get("ssl")) is settings.ssl
        and str(server.get("ssl_verify")) == "3"
        and str(server.get("retention")) == str(settings.retention)
        and str(server.get("priority")) == "0"
    )


def configure_managed_usenet_provider(
    settings: ManagedUsenetSettings,
    port: int,
    api_key: str,
    *,
    requester=_sab_request,
) -> None:
    """Test and converge, or safely disable, WyseARR's one managed server."""

    configured = _sab_server_list(
        requester(
            port,
            api_key,
            {"mode": "get_config", "section": "servers"},
        )
    )
    managed = [
        server
        for server in configured
        if str(server.get("name") or "").casefold()
        == MANAGED_USENET_SERVER_NAME.casefold()
    ]
    if len(managed) > 1:
        raise BootstrapError("SABnzbd returned duplicate managed Usenet servers")

    if settings.enabled is False:
        if not managed or _sab_boolean(managed[0].get("enable")) is False:
            return
        result = requester(
            port,
            api_key,
            {
                "mode": "set_config",
                "section": "servers",
                "keyword": MANAGED_USENET_SERVER_NAME,
                "enable": "0",
            },
        )
        if not _sab_server_write_confirmed(result, settings, enabled=False):
            raise BootstrapError("SABnzbd rejected managed Usenet server disablement")
        return

    test_result = requester(
        port,
        api_key,
        {
            "mode": "config",
            "name": "test_server",
            "server": MANAGED_USENET_SERVER_NAME,
            "host": settings.host,
            "port": str(settings.port),
            "username": settings.username,
            "password": settings.password,
            "connections": str(settings.connections),
            "ssl": "1" if settings.ssl else "0",
            "ssl_verify": "3",
        },
    )
    if not _sab_server_test_confirmed(test_result):
        raise BootstrapError("SABnzbd rejected managed Usenet server connection test")

    result = requester(
        port,
        api_key,
        {
            "mode": "set_config",
            "section": "servers",
            "keyword": MANAGED_USENET_SERVER_NAME,
            "displayname": MANAGED_USENET_SERVER_NAME,
            "host": settings.host,
            "port": str(settings.port),
            "username": settings.username,
            "password": settings.password,
            "connections": str(settings.connections),
            "ssl": "1" if settings.ssl else "0",
            "ssl_verify": "3",
            "enable": "1",
            "retention": str(settings.retention),
            "priority": "0",
        },
    )
    if not _sab_server_write_confirmed(result, settings, enabled=True):
        raise BootstrapError("SABnzbd rejected managed Usenet server configuration")


def _read_private_regular_text(path: Path) -> str:
    """Read a private regular file without following a final-component link."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError("SABnzbd configuration is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BootstrapError("SABnzbd configuration path is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    except UnicodeError as exc:
        raise BootstrapError("SABnzbd configuration is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sab_section(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return None
    match = INI_SECTION_RE.fullmatch(line)
    if match is None:
        if stripped.startswith("["):
            raise BootstrapError("SABnzbd configuration has malformed sections")
        return None
    opening = match.group("open")
    closing = match.group("close")
    if len(opening) != len(closing) or len(opening) not in {1, 2}:
        raise BootstrapError("SABnzbd configuration has ambiguous nesting")
    return len(opening), match.group("name").strip()


def _sab_private_rewrite(lines: list[str]) -> tuple[list[str], bool]:
    """Force private logging and quarantine the managed NNTP server offline."""

    misc_sections: list[int] = []
    servers_sections: list[int] = []
    managed_sections: list[int] = []
    current_top = ""
    current_child = ""
    misc_logging: list[int] = []
    managed_enable: list[int] = []

    for index, raw_line in enumerate(lines):
        section = _sab_section(raw_line)
        if section is not None:
            depth, name = section
            folded = name.casefold()
            if depth == 1:
                current_top = folded
                current_child = ""
                if folded == "misc":
                    misc_sections.append(index)
                if folded == "servers":
                    servers_sections.append(index)
                if folded == MANAGED_USENET_SERVER_NAME.casefold():
                    raise BootstrapError(
                        "Managed SABnzbd server has ambiguous section nesting"
                    )
            else:
                current_child = folded
                if folded == MANAGED_USENET_SERVER_NAME.casefold():
                    if current_top != "servers":
                        raise BootstrapError(
                            "Managed SABnzbd server is outside the servers section"
                        )
                    managed_sections.append(index)
            continue

        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip().casefold()
        if current_top == "misc" and not current_child and key == "api_logging":
            misc_logging.append(index)
        if (
            current_top == "servers"
            and current_child == MANAGED_USENET_SERVER_NAME.casefold()
            and key == "enable"
        ):
            managed_enable.append(index)

    if len(misc_sections) != 1:
        raise BootstrapError("SABnzbd misc configuration is unavailable or ambiguous")
    if len(servers_sections) > 1 or len(managed_sections) > 1:
        raise BootstrapError("SABnzbd managed server configuration is ambiguous")
    if len(misc_logging) > 1 or len(managed_enable) > 1:
        raise BootstrapError("SABnzbd managed settings are duplicated")

    inserts: list[tuple[int, str]] = []
    if misc_logging:
        lines[misc_logging[0]] = "api_logging = 0"
    else:
        end = next(
            (
                index
                for index in range(misc_sections[0] + 1, len(lines))
                if (_sab_section(lines[index]) or (0, ""))[0] == 1
            ),
            len(lines),
        )
        inserts.append((end, "api_logging = 0"))

    managed_present = bool(managed_sections)
    if managed_present:
        if managed_enable:
            lines[managed_enable[0]] = "enable = 0"
        else:
            start = managed_sections[0]
            end = next(
                (
                    index
                    for index in range(start + 1, len(lines))
                    if _sab_section(lines[index]) is not None
                ),
                len(lines),
            )
            inserts.append((end, "enable = 0"))

    for index, value in sorted(inserts, reverse=True):
        lines.insert(index, value)
    return lines, managed_present


def _atomic_private_replace(path: Path, content: str) -> None:
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise BootstrapError("SABnzbd configuration directory is unavailable") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise BootstrapError("SABnzbd configuration directory is unsafe")

    temporary = path.with_name(
        f".{path.name}.wysearr-tmp-{os.getpid()}-{secrets.token_hex(6)}"
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
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise BootstrapError("Cannot stage the private SABnzbd configuration") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def prepare_sabnzbd_private_config(path: Path) -> None:
    """Disable API logging and quarantine WyseARR's NNTP server while stopped."""

    original = _read_private_regular_text(path)
    lines, managed_present = _sab_private_rewrite(original.splitlines())
    _atomic_private_replace(path, "\n".join(lines) + "\n")

    persisted = _read_private_regular_text(path).splitlines()
    verified, verified_managed = _sab_private_rewrite(list(persisted))
    if persisted != verified or verified_managed != managed_present:
        raise BootstrapError("SABnzbd private offline convergence did not persist")


def configure_sabnzbd(
    config_path: Path,
    port: int,
    username: str,
    password: str,
    *,
    requester=_sab_request,
) -> str:
    """Set isolated download roots/category and verify the authenticated API."""

    api_key = _read_sab_api_key(config_path)
    desired = {
        # Disable parameter logging before any credential-bearing request.
        "api_logging": "0",
        "download_dir": "/downloads/incomplete/usenet",
        "complete_dir": "/downloads/usenet",
        "host_whitelist": "sabnzbd",
        "username": username,
        "password": password,
    }
    for key, value in desired.items():
        result = requester(
            port,
            api_key,
            {
                "mode": "set_config",
                "section": "misc",
                "keyword": key,
                "value": value,
            },
        )
        if not _sab_misc_write_confirmed(result, key, value):
            raise BootstrapError(f"SABnzbd rejected configuration key: {key}")

    category = requester(
        port,
        api_key,
        {
            "mode": "set_config",
            "section": "categories",
            "name": "shelfarr",
            "dir": "shelfarr",
            "pp": "3",
            "script": "None",
            "priority": "0",
        },
    )
    if not _sab_category_write_confirmed(category):
        raise BootstrapError("SABnzbd rejected the Shelfarr category")

    persisted = _read_sab_misc(config_path)
    whitelist = {
        item.strip()
        for item in persisted.get("host_whitelist", "").split(",")
        if item.strip()
    }
    if not (
        persisted.get("api_logging") in {"0", "false", "False"}
        and persisted.get("download_dir") == "/downloads/incomplete/usenet"
        and persisted.get("complete_dir") == "/downloads/usenet"
        and "sabnzbd" in whitelist
        and persisted.get("username") == username
        and bool(persisted.get("password"))
    ):
        raise BootstrapError("SABnzbd did not persist its private configuration")

    version = requester(port, api_key, {"mode": "version"})
    categories = requester(port, api_key, {"mode": "get_cats"})
    category_values = categories.get("categories", []) if isinstance(categories, dict) else []
    if not (
        isinstance(version, dict)
        and version.get("version")
        and isinstance(category_values, list)
        and "shelfarr" in category_values
    ):
        raise BootstrapError("SABnzbd API/category validation failed")
    return api_key


def converge_shelfarr(
    root: Path,
    environment: Mapping[str, str],
    sabnzbd_api_key: str,
    *,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Run the in-image ActiveRecord convergence and return its safe result."""

    required = (
        "PROWLARR_API_KEY",
        "QBITTORRENT_USERNAME",
        "QBITTORRENT_PASSWORD",
        "SHELFARR_ADMIN_USERNAME",
        "SHELFARR_ADMIN_PASSWORD",
    )
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise BootstrapError(f"Required private setting is missing: {missing[0]}")

    payload = {
        "prowlarr_api_key": environment["PROWLARR_API_KEY"],
        "qbittorrent_username": environment["QBITTORRENT_USERNAME"],
        "qbittorrent_password": environment["QBITTORRENT_PASSWORD"],
        "sabnzbd_api_key": sabnzbd_api_key,
        "admin_username": environment["SHELFARR_ADMIN_USERNAME"],
        "admin_password": environment["SHELFARR_ADMIN_PASSWORD"],
        "existing_huey_token": environment.get("SHELFARR_API_TOKEN", ""),
        "usenet_enabled": parse_managed_usenet_settings(environment).enabled,
    }
    result = runner(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "--user",
            f"{environment.get('PUID', '1000')}:{environment.get('PGID', '1000')}",
            "shelfarr",
            "ruby",
            "/opt/wysearr/shelfarr_exec.rb",
            "bin/rails",
            "runner",
            "/opt/wysearr/shelfarr_bootstrap.rb",
        ],
        cwd=root,
        input=json.dumps(payload, separators=(",", ":")),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise BootstrapError("Shelfarr rejected production convergence")
    lines = [
        line.removeprefix(BOOTSTRAP_SENTINEL)
        for line in result.stdout.splitlines()
        if line.startswith(BOOTSTRAP_SENTINEL)
    ]
    if len(lines) != 1:
        raise BootstrapError("Shelfarr convergence produced no valid result")
    try:
        value = json.loads(lines[0])
    except ValueError as exc:
        raise BootstrapError("Shelfarr convergence produced no valid result") from exc
    token = value.get("huey_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token.startswith("shf_"):
        raise BootstrapError("Shelfarr did not issue a valid Huey token")
    return value


def bootstrap_shelfarr(root: Path, *, enable: bool = False) -> dict[str, Any]:
    env_path = root / ".env"
    environment = load_dotenv(env_path)
    usenet_settings = parse_managed_usenet_settings(environment)
    shelfarr_enabled = enable or environment.get("SHELFARR_ENABLED", "") == "true"
    if usenet_settings.enabled is True and not shelfarr_enabled:
        raise BootstrapError(
            "WYSEARR_USENET_ENABLED requires SHELFARR_ENABLED=true"
        )
    updates: dict[str, str] = {}
    if not environment.get("SHELFARR_ADMIN_USERNAME"):
        updates["SHELFARR_ADMIN_USERNAME"] = "wyseadmin"
    if not environment.get("SHELFARR_ADMIN_PASSWORD"):
        updates["SHELFARR_ADMIN_PASSWORD"] = f"WyseARR-{secrets.token_urlsafe(24)}-9aA"
    if not environment.get("SABNZBD_ADMIN_USERNAME"):
        updates["SABNZBD_ADMIN_USERNAME"] = "wyseadmin"
    if not environment.get("SABNZBD_ADMIN_PASSWORD"):
        updates["SABNZBD_ADMIN_PASSWORD"] = f"WyseARR-{secrets.token_urlsafe(24)}-9aA"
    if updates:
        environment = update_dotenv(env_path, updates)

    try:
        port = int(environment.get("SABNZBD_ADMIN_PORT", "8085"))
    except ValueError as exc:
        raise BootstrapError("SABNZBD_ADMIN_PORT must be numeric") from exc
    sab_key = configure_sabnzbd(
        root / "config" / "sabnzbd" / "sabnzbd.ini",
        port,
        environment["SABNZBD_ADMIN_USERNAME"],
        environment["SABNZBD_ADMIN_PASSWORD"],
    )
    configure_managed_usenet_provider(usenet_settings, port, sab_key)
    value = converge_shelfarr(root, environment, sab_key)
    final_updates = {
        "SABNZBD_API_KEY": sab_key,
        "SHELFARR_API_TOKEN": str(value["huey_token"]),
    }
    if enable:
        final_updates["SHELFARR_ENABLED"] = "true"
    update_dotenv(env_path, final_updates)
    return {
        "token_reused": bool(value.get("huey_token_reused")),
        "settings_count": int(value.get("settings_count", 0)),
        "download_clients": list(value.get("download_clients", [])),
        "enabled": shelfarr_enabled,
    }


def converge_managed_usenet_only(root: Path) -> bool:
    """Converge only the managed SAB provider while Shelfarr may be stopped."""

    environment = load_dotenv(root / ".env")
    settings = parse_managed_usenet_settings(environment)
    if (
        settings.enabled is True
        and environment.get("SHELFARR_ENABLED", "") != "true"
    ):
        raise BootstrapError(
            "WYSEARR_USENET_ENABLED requires SHELFARR_ENABLED=true"
        )
    try:
        port = int(environment.get("SABNZBD_ADMIN_PORT", "8085"))
    except ValueError as exc:
        raise BootstrapError("SABNZBD_ADMIN_PORT must be numeric") from exc
    api_key = environment.get("SABNZBD_API_KEY", "") or _read_sab_api_key(
        root / "config" / "sabnzbd" / "sabnzbd.ini"
    )
    configure_managed_usenet_provider(settings, port, api_key)
    return settings.enabled


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=STACK_ROOT)
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable Shelfarr ownership for new Huey ebook requests",
    )
    parser.add_argument(
        "--check-drain-only",
        action="store_true",
        help="Only verify that BookBot-owned ebook categories are drained",
    )
    parser.add_argument(
        "--prepare-sab-config",
        action="store_true",
        help="Disable SAB API logging and quarantine managed NNTP while stopped",
    )
    parser.add_argument(
        "--converge-usenet-only",
        action="store_true",
        help="Only enable/disable the managed SABnzbd NNTP provider",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        selected_modes = sum(
            bool(value)
            for value in (
                arguments.enable,
                arguments.check_drain_only,
                arguments.prepare_sab_config,
                arguments.converge_usenet_only,
            )
        )
        if selected_modes > 1:
            raise BootstrapError("Shelfarr bootstrap modes are mutually exclusive")
        if arguments.prepare_sab_config:
            prepare_sabnzbd_private_config(
                arguments.root.resolve() / "config" / "sabnzbd" / "sabnzbd.ini"
            )
            print("SABnzbd private logging and managed NNTP are disabled on disk.")
            return 0
        if arguments.check_drain_only:
            environment = load_dotenv(arguments.root.resolve() / ".env")
            require_drained_bookbot_ebook_categories(environment)
            print("BookBot ebook categories are drained.")
            return 0
        if arguments.converge_usenet_only:
            enabled = converge_managed_usenet_only(arguments.root.resolve())
            state = "enabled and connection-tested" if enabled else "disabled"
            print(f"Managed SABnzbd Usenet provider is {state}.")
            return 0
        result = bootstrap_shelfarr(arguments.root.resolve(), enable=arguments.enable)
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    state = "enabled" if result["enabled"] else "configured (disabled)"
    print(
        "Shelfarr evaluation "
        f"{state}; {result['settings_count']} settings and "
        f"{len(result['download_clients'])} clients verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
