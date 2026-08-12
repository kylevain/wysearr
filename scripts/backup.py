#!/usr/bin/env python3
"""Create a local, secret-safe runtime checkpoint without copying media."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = STACK_ROOT / "backups"


def private_mkdir(path: Path, *, anchor: Path = BACKUP_ROOT) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    while current == path or anchor in current.parents:
        current.chmod(0o700)
        if current == anchor:
            break
        current = current.parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_backup(source: Path, destination: Path, *, anchor: Path = BACKUP_ROOT) -> None:
    private_mkdir(destination.parent, anchor=anchor)
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite backup validation failed for {source}")
    destination.chmod(0o600)


def copy_private(source: Path, destination: Path, *, anchor: Path = BACKUP_ROOT) -> None:
    private_mkdir(destination.parent, anchor=anchor)
    shutil.copy2(source, destination)
    destination.chmod(0o600)


def git_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def create_backup(output: Path) -> dict[str, object]:
    if output.exists():
        raise FileExistsError(output)
    private_mkdir(output, anchor=output)
    output.chmod(0o700)
    copied: list[dict[str, str]] = []

    sqlite_sources = sorted((STACK_ROOT / "config").glob("**/*.db"))
    sqlite_sources += sorted((STACK_ROOT / "state").glob("**/*.db"))
    for source in sqlite_sources:
        if not source.is_file():
            continue
        relative = source.relative_to(STACK_ROOT)
        destination = output / relative
        sqlite_backup(source, destination, anchor=output)
        copied.append({"path": str(relative), "sha256": sha256(destination)})

    config_patterns = (
        ".env",
        "docker-compose.yml",
        "config/*/config.xml",
        "config/bazarr/config/config.yaml",
        "config/qbittorrent/qBittorrent/*.conf",
        "config/qbittorrent/qBittorrent/categories.json",
        "config/qbittorrent/qBittorrent/watched_folders.json",
    )
    seen: set[Path] = set()
    for pattern in config_patterns:
        for source in sorted(STACK_ROOT.glob(pattern)):
            if not source.is_file() or source in seen:
                continue
            seen.add(source)
            relative = source.relative_to(STACK_ROOT)
            destination = output / relative
            copy_private(source, destination, anchor=output)
            copied.append({"path": str(relative), "sha256": sha256(destination)})

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_head": git_head(),
        "files": copied,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = (args.output or (BACKUP_ROOT / timestamp)).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = create_backup(output)
    if not args.quiet:
        print(f"PASS: runtime checkpoint created at {output} ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
