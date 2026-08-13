#!/usr/bin/env python3
"""Converge the controlled Shelfarr/SABnzbd evaluation without leaking secrets."""

from __future__ import annotations

import argparse
import json
import os
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
        authenticate_qbittorrent,
        load_dotenv,
        update_dotenv,
    )
except ImportError:
    from bootstrap import (
        BootstrapError,
        QbittorrentClient,
        authenticate_qbittorrent,
        load_dotenv,
        update_dotenv,
    )


STACK_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SENTINEL = "WYSEARR_BOOTSTRAP_RESULT="
BOOKBOT_BOOK_CATEGORIES = (
    "ebooks",
    "ebooks-imported",
    "audiobooks",
    "audiobooks-imported",
)


def require_drained_bookbot_book_categories(
    environment: Mapping[str, str],
    *,
    client_factory=QbittorrentClient,
) -> None:
    """Refuse a Shelfarr ownership transition while BookBot book jobs exist."""

    bind_address = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    base_url = (
        "http://" + bind_address + ":" + environment.get("QBITTORRENT_PORT", "8080")
    )
    client = authenticate_qbittorrent(
        base_url,
        environment.get("QBITTORRENT_USERNAME", "admin"),
        environment.get("QBITTORRENT_PASSWORD", ""),
        client_factory=client_factory,
    )
    active = [
        category
        for category in BOOKBOT_BOOK_CATEGORIES
        if client.torrents(category)
    ]
    if active:
        raise BootstrapError(
            "Cannot enable Shelfarr while BookBot book-category torrents remain: "
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


def prepare_sabnzbd_private_config(path: Path) -> None:
    """Disable SAB API parameter logging while the service is stopped."""

    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BootstrapError("SABnzbd configuration path is unsafe")
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BootstrapError("SABnzbd configuration is unavailable") from exc

    section = ""
    replaced = False
    misc_end = len(lines)
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            if section == "misc" and misc_end == len(lines):
                misc_end = index
            section = line[1:-1].strip().casefold()
        elif section == "misc" and "=" in line:
            key = line.split("=", 1)[0].strip().casefold()
            if key == "api_logging":
                lines[index] = "api_logging = 0"
                replaced = True
    if "[misc]" not in {line.strip().casefold() for line in lines}:
        raise BootstrapError("SABnzbd misc configuration is unavailable")
    if not replaced:
        lines.insert(misc_end, "api_logging = 0")

    temporary = path.with_name(f"{path.name}.wysearr-tmp-{os.getpid()}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except OSError as exc:
        raise BootstrapError("Cannot stage the private SABnzbd configuration") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
    enabling_now = enable or environment.get("SHELFARR_ENABLED", "") == "true"
    if enabling_now:
        require_drained_bookbot_book_categories(environment)
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
        "enabled": enabling_now,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=STACK_ROOT)
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable Shelfarr ownership for new Huey ebook/audiobook requests",
    )
    parser.add_argument(
        "--check-drain-only",
        action="store_true",
        help="Only verify that all BookBot-owned book categories are drained",
    )
    parser.add_argument(
        "--prepare-sab-config",
        action="store_true",
        help="Disable SAB API logging while SABnzbd is stopped",
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
            )
        )
        if selected_modes > 1:
            raise BootstrapError("Shelfarr bootstrap modes are mutually exclusive")
        if arguments.prepare_sab_config:
            prepare_sabnzbd_private_config(
                arguments.root.resolve() / "config" / "sabnzbd" / "sabnzbd.ini"
            )
            print("SABnzbd API parameter logging is disabled on disk.")
            return 0
        if arguments.check_drain_only:
            environment = load_dotenv(arguments.root.resolve() / ".env")
            require_drained_bookbot_book_categories(environment)
            print("BookBot ebook/audiobook categories are drained.")
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
