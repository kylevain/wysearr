#!/usr/bin/env python3
"""Repair the known stale Whisparr quality-23 row left by an incompatible image."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[1]
DATABASE = STACK_ROOT / "config" / "whisparr" / "whisparr2.db"
CONFIG = STACK_ROOT / "config" / "whisparr" / "config.xml"


def host_endpoint() -> tuple[str, int]:
    address = "192.168.4.86"
    port = 6969
    env_path = STACK_ROOT / ".env"
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            if raw_line.startswith("WYSEARR_BIND_ADDRESS="):
                value = raw_line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    address = value
            elif raw_line.startswith("WHISPARR_PORT="):
                value = raw_line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    try:
                        port = int(value)
                    except ValueError as error:
                        raise RuntimeError("WHISPARR_PORT must be numeric") from error
    return address, port


def api_key() -> str:
    text = CONFIG.read_text(encoding="utf-8")
    start = text.index("<ApiKey>") + len("<ApiKey>")
    end = text.index("</ApiKey>", start)
    return text[start:end]


def endpoint_works() -> bool:
    for endpoint in ("qualitydefinition", "qualityprofile"):
        address, port = host_endpoint()
        request = urllib.request.Request(
            f"http://{address}:{port}/api/v3/{endpoint}",
            headers={"X-Api-Key": api_key()},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status != 200:
                    return False
        except (urllib.error.URLError, TimeoutError):
            return False
    return True


def run_compose(*arguments: str) -> None:
    subprocess.run(
        ["docker", "compose", *arguments], cwd=STACK_ROOT, check=True
    )


def start_whisparr() -> None:
    # During a first rollout the existing qBittorrent container predates the
    # Compose healthcheck. Starting this already-created container directly
    # avoids making recovery depend on that transitional Compose state.
    subprocess.run(["docker", "start", "whisparr"], check=True)


def filter_quality_23(value: object) -> object:
    if isinstance(value, list):
        return [
            filter_quality_23(item)
            for item in value
            if not (isinstance(item, dict) and item.get("quality") == 23)
        ]
    if isinstance(value, dict):
        return {key: filter_quality_23(item) for key, item in value.items()}
    return value


def repair() -> Path | None:
    if endpoint_works():
        return None

    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as connection:
        stale = connection.execute(
            "SELECT COUNT(*) FROM QualityDefinitions WHERE Quality = 23"
        ).fetchone()[0]
        content = connection.execute("SELECT COUNT(*) FROM Series").fetchone()[0]
        files = connection.execute("SELECT COUNT(*) FROM EpisodeFiles").fetchone()[0]
    if stale != 1:
        raise RuntimeError("Whisparr failed validation for an unknown reason; refusing DB repair")
    if content or files:
        raise RuntimeError("Whisparr has library content; refusing automatic quality repair")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = STACK_ROOT / "backups" / f"whisparr-quality-{timestamp}"
    backup_dir.mkdir(parents=True, mode=0o700)
    backup_path = backup_dir / DATABASE.name

    run_compose("stop", "whisparr")
    try:
        with sqlite3.connect(DATABASE) as connection:
            with sqlite3.connect(backup_path) as destination:
                connection.backup(destination)
            backup_path.chmod(0o600)

            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM QualityDefinitions WHERE Quality = 23")
            profiles = connection.execute("SELECT Id, Items FROM QualityProfiles").fetchall()
            for profile_id, raw_items in profiles:
                items = filter_quality_23(json.loads(raw_items))
                connection.execute(
                    "UPDATE QualityProfiles SET Items = ? WHERE Id = ?",
                    (json.dumps(items, indent=2), profile_id),
                )
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Whisparr DB failed integrity check after repair")
    finally:
        start_whisparr()

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if endpoint_works():
            return backup_path
        time.sleep(2)
    raise RuntimeError("Whisparr quality API did not recover after repair")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if endpoint_works():
        print("PASS: Whisparr quality data is compatible")
        return 0
    if not args.apply:
        print("FAIL: known Whisparr stale quality data detected; rerun with --apply")
        return 2
    backup = repair()
    if backup:
        print(f"PASS: Whisparr quality data repaired; backup: {backup}")
    else:
        print("PASS: Whisparr quality data is compatible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
