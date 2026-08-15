#!/usr/bin/env python3
"""Create a private local runtime checkpoint without copying media payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = STACK_ROOT / "backups"
QBITTORRENT_RESUME_RELATIVE = Path("config/qbittorrent/qBittorrent/BT_backup")
MAX_QBITTORRENT_RESUME_FILES = 20_000
MAX_QBITTORRENT_RESUME_FILE_BYTES = 32 * 1024 * 1024
MAX_QBITTORRENT_RESUME_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ENV_BYTES = 1024 * 1024
SHELFARR_STORAGE_RELATIVE = Path("config/shelfarr")
MAX_SHELFARR_STORAGE_FILES = 100_000
MAX_SHELFARR_STORAGE_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_SHELFARR_STORAGE_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
LAZYLIBRARIAN_CONFIG_RELATIVE = Path("config/lazylibrarian/config.ini")
MAX_LAZYLIBRARIAN_CONFIG_BYTES = 16 * 1024 * 1024
SQLITE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
EXCLUDED_SQLITE_NAMES = frozenset({"logs.db"})
EXCLUDED_PAYLOAD_ROOTS = (
    Path("state/torrents"),
    Path("state/shelfarr-staging"),
)
EPHEMERAL_ENV_KEYS = frozenset({b"AUDIOBOOKSHELF_API_TOKEN"})


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
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(destination)) as dst, dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite backup validation failed for {source}")
    destination.chmod(0o600)


def copy_private(source: Path, destination: Path, *, anchor: Path = BACKUP_ROOT) -> None:
    private_mkdir(destination.parent, anchor=anchor)
    shutil.copy2(source, destination)
    destination.chmod(0o600)


def sanitize_env_bytes(value: bytes) -> bytes:
    """Remove validation-only credentials that must not persist in checkpoints."""

    kept: list[bytes] = []
    for line in value.splitlines(keepends=True):
        name = line.partition(b"=")[0].strip()
        if name.startswith(b"export "):
            name = name.removeprefix(b"export ").strip()
        if name not in EPHEMERAL_ENV_KEYS:
            kept.append(line)
    return b"".join(kept)


def copy_private_env(
    source: Path,
    destination: Path,
    *,
    anchor: Path = BACKUP_ROOT,
) -> None:
    """Copy a stable dotenv file while excluding validation-only credentials."""

    private_mkdir(destination.parent, anchor=anchor)
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"checkpoint environment source is not a regular file: {source}")
    if before.st_size > MAX_ENV_BYTES:
        raise RuntimeError(f"checkpoint environment source exceeds its size limit: {source}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    destination_created = False
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise RuntimeError(
                f"checkpoint environment source changed before it was read: {source}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(MAX_ENV_BYTES + 1)
        after = os.fstat(descriptor)
        if len(value) > MAX_ENV_BYTES:
            raise RuntimeError(
                f"checkpoint environment source exceeds its size limit: {source}"
            )
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(opened, field) != getattr(after, field) for field in stable_fields):
            raise RuntimeError(
                f"checkpoint environment source changed while it was read: {source}"
            )
        with destination.open("xb") as output:
            destination_created = True
            output.write(sanitize_env_bytes(value))
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)

    destination.chmod(0o600)


def copy_stable_bounded_private(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    anchor: Path = BACKUP_ROOT,
) -> int:
    """Copy one regular file without following links and reject an unstable read."""
    private_mkdir(destination.parent, anchor=anchor)
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"checkpoint source is not a regular file: {source}")
    if before.st_size > maximum_bytes:
        raise RuntimeError(f"checkpoint source exceeds its size limit: {source}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    destination_created = False
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise RuntimeError(f"checkpoint source changed before it was read: {source}")
        if opened.st_size > maximum_bytes:
            raise RuntimeError(f"checkpoint source exceeds its size limit: {source}")

        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            with destination.open("xb") as output:
                destination_created = True
                copied_bytes = 0
                while True:
                    block = stream.read(
                        min(1024 * 1024, maximum_bytes + 1 - copied_bytes)
                    )
                    if not block:
                        break
                    copied_bytes += len(block)
                    if copied_bytes > maximum_bytes:
                        raise RuntimeError(
                            f"checkpoint source exceeds its size limit: {source}"
                        )
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(opened, field) != getattr(after, field) for field in stable_fields):
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"checkpoint source changed while it was read: {source}")
    except Exception:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)

    destination.chmod(0o600)
    return opened.st_size


def copy_qbittorrent_resume_state(
    output: Path,
    copied: list[dict[str, str]],
    *,
    stack_root: Path = STACK_ROOT,
) -> None:
    """Copy bounded qBittorrent session metadata, never torrent payload paths."""
    source_root = stack_root / QBITTORRENT_RESUME_RELATIVE
    if not source_root.exists():
        return
    current = stack_root
    for component in QBITTORRENT_RESUME_RELATIVE.parts:
        current = current / component
        if current.is_symlink():
            raise RuntimeError(
                f"qBittorrent resume source contains a symbolic link: {current}"
            )
    if source_root.is_symlink() or not source_root.is_dir():
        raise RuntimeError(f"qBittorrent resume source is not a directory: {source_root}")

    entries = sorted(source_root.iterdir(), key=lambda path: path.name)
    if len(entries) > MAX_QBITTORRENT_RESUME_FILES:
        raise RuntimeError("qBittorrent resume state exceeds the checkpoint file-count limit")

    total_bytes = 0
    for source in entries:
        relative = source.relative_to(stack_root)
        destination = output / relative
        copied_bytes = copy_stable_bounded_private(
            source,
            destination,
            maximum_bytes=MAX_QBITTORRENT_RESUME_FILE_BYTES,
            anchor=output,
        )
        total_bytes += copied_bytes
        if total_bytes > MAX_QBITTORRENT_RESUME_TOTAL_BYTES:
            destination.unlink(missing_ok=True)
            raise RuntimeError("qBittorrent resume state exceeds the checkpoint byte limit")
        copied.append({"path": str(relative), "sha256": sha256(destination)})


def copy_shelfarr_storage(
    output: Path,
    copied: list[dict[str, str]],
    sqlite_sources: set[Path],
    *,
    stack_root: Path = STACK_ROOT,
) -> None:
    """Checkpoint every persistent Shelfarr file outside live SQLite sidecars."""

    source_root = stack_root / SHELFARR_STORAGE_RELATIVE
    if not source_root.exists():
        return
    if source_root.is_symlink() or not source_root.is_dir():
        raise RuntimeError(
            f"Shelfarr storage source is not a directory: {source_root}"
        )

    entries = sorted(source_root.rglob("*"), key=lambda path: str(path))
    if len(entries) > MAX_SHELFARR_STORAGE_FILES:
        raise RuntimeError("Shelfarr storage exceeds the checkpoint file-count limit")

    total_bytes = 0
    for source in entries:
        if source.is_symlink():
            raise RuntimeError(f"Shelfarr storage contains a symbolic link: {source}")
        if source.is_dir():
            continue
        if not source.is_file():
            raise RuntimeError(
                f"Shelfarr storage contains an unsupported file type: {source}"
            )
        if source.name.casefold() in EXCLUDED_SQLITE_NAMES:
            continue
        # SQLite files are copied transactionally by sqlite_backup(). Their
        # live journal sidecars must never be copied independently.
        if source in sqlite_sources:
            continue
        if source.name.endswith(("-wal", "-shm")) and any(
            Path(source.name.removesuffix(sidecar)).suffix.casefold()
            in SQLITE_SUFFIXES
            for sidecar in ("-wal", "-shm")
            if source.name.endswith(sidecar)
        ):
            continue

        relative = source.relative_to(stack_root)
        destination = output / relative
        copied_bytes = copy_stable_bounded_private(
            source,
            destination,
            maximum_bytes=MAX_SHELFARR_STORAGE_FILE_BYTES,
            anchor=output,
        )
        total_bytes += copied_bytes
        if total_bytes > MAX_SHELFARR_STORAGE_TOTAL_BYTES:
            destination.unlink(missing_ok=True)
            raise RuntimeError("Shelfarr storage exceeds the checkpoint byte limit")
        copied.append({"path": str(relative), "sha256": sha256(destination)})


def git_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def git_dirty() -> bool | None:
    """Record whether a checkpoint came from an uncommitted code generation."""

    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return bool(result.stdout) if result.returncode == 0 else None


def _is_payload_path(source: Path) -> bool:
    relative = source.relative_to(STACK_ROOT)
    return any(
        relative == root or root in relative.parents
        for root in EXCLUDED_PAYLOAD_ROOTS
    )


def create_backup(output: Path) -> dict[str, object]:
    if output.exists():
        raise FileExistsError(output)
    private_mkdir(output, anchor=output)
    output.chmod(0o700)
    copied: list[dict[str, str]] = []
    shelfarr_storage = STACK_ROOT / SHELFARR_STORAGE_RELATIVE
    shelfarr_present = shelfarr_storage.is_dir()

    sqlite_candidates = {
        source
        for root in (STACK_ROOT / "config", STACK_ROOT / "state")
        for source in root.glob("**/*")
        if source.suffix.casefold() in SQLITE_SUFFIXES
        and source.name.casefold() not in EXCLUDED_SQLITE_NAMES
        and not _is_payload_path(source)
    }
    linked_databases = sorted(
        source for source in sqlite_candidates if source.is_symlink()
    )
    if linked_databases:
        raise RuntimeError(
            f"checkpoint SQLite source is a symbolic link: {linked_databases[0]}"
        )
    sqlite_sources = {source for source in sqlite_candidates if source.is_file()}
    for source in sorted(sqlite_sources):
        if not source.is_file():
            continue
        relative = source.relative_to(STACK_ROOT)
        destination = output / relative
        sqlite_backup(source, destination, anchor=output)
        copied.append({"path": str(relative), "sha256": sha256(destination)})

    config_patterns = (
        ".env",
        "docker-compose.yml",
        "state/shelfarr-evaluation/*.json",
        "config/*/config.xml",
        "config/bazarr/config/config.yaml",
        "config/qbittorrent/qBittorrent/*.conf",
        "config/qbittorrent/qBittorrent/categories.json",
        "config/qbittorrent/qBittorrent/watched_folders.json",
        "config/sabnzbd/sabnzbd.ini",
        "config/sabnzbd/admin/*.sab",
        str(LAZYLIBRARIAN_CONFIG_RELATIVE),
    )
    seen: set[Path] = set()
    for pattern in config_patterns:
        for source in sorted(STACK_ROOT.glob(pattern)):
            if not source.is_file() or source in seen:
                continue
            seen.add(source)
            relative = source.relative_to(STACK_ROOT)
            destination = output / relative
            if relative == Path(".env"):
                copy_private_env(source, destination, anchor=output)
            elif relative == LAZYLIBRARIAN_CONFIG_RELATIVE:
                copy_stable_bounded_private(
                    source,
                    destination,
                    maximum_bytes=MAX_LAZYLIBRARIAN_CONFIG_BYTES,
                    anchor=output,
                )
            else:
                copy_private(source, destination, anchor=output)
            copied.append({"path": str(relative), "sha256": sha256(destination)})

    copy_shelfarr_storage(
        output,
        copied,
        sqlite_sources,
        stack_root=STACK_ROOT,
    )
    copy_qbittorrent_resume_state(output, copied, stack_root=STACK_ROOT)

    manifest = {
        "format_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_head": git_head(),
        "git_dirty": git_dirty(),
        "boundary": {
            "includes": "selected configuration including LazyLibrarian config.ini, SQLite-safe databases, complete bounded Shelfarr storage, and bounded qBittorrent resume metadata",
            "excludes": [
                "state/torrents/** download payloads",
                "state/shelfarr-staging/** direct-download staging payloads",
                "/mnt/media/** library media",
                "service logs.db diagnostic databases",
            ],
            "shelfarr_consistency": (
                "service-stopped generation required for exact stateful rollback"
                if shelfarr_present
                else "not present"
            ),
            "lazylibrarian_consistency": (
                "service-stopped generation required for exact config/database rollback"
                if (STACK_ROOT / "config/lazylibrarian").is_dir()
                else "not present"
            ),
        },
        "files": copied,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    verify_backup(output)
    return manifest


def _manifest_target(checkpoint: Path, raw_path: object) -> tuple[str, Path]:
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("checkpoint manifest contains an invalid path")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"checkpoint manifest contains an unsafe path: {raw_path}")
    candidate = checkpoint / relative
    current = checkpoint
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise RuntimeError(f"checkpoint contains a symbolic link: {raw_path}")
    resolved = candidate.resolve()
    if checkpoint != resolved and checkpoint not in resolved.parents:
        raise RuntimeError(f"checkpoint path escapes its directory: {raw_path}")
    return raw_path, candidate


def verify_backup(checkpoint: Path) -> dict[str, object]:
    checkpoint = checkpoint.resolve()
    manifest_path = checkpoint / "manifest.json"
    if not checkpoint.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError(f"checkpoint manifest is missing: {checkpoint}")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise RuntimeError("checkpoint manifest exceeds its size limit")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise RuntimeError("checkpoint manifest has an invalid structure")
    if manifest.get("format_version", 0) not in (0, 1, 2):
        raise RuntimeError("checkpoint manifest uses an unsupported format")

    declared: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
            raise RuntimeError("checkpoint manifest contains an invalid file record")
        raw_path, target = _manifest_target(checkpoint, entry.get("path"))
        if raw_path in declared:
            raise RuntimeError(f"checkpoint manifest repeats a path: {raw_path}")
        declared.add(raw_path)
        if not target.is_file() or target.is_symlink():
            raise RuntimeError(f"checkpoint file is missing or unsafe: {raw_path}")
        if sha256(target) != entry["sha256"]:
            raise RuntimeError(f"checkpoint hash mismatch: {raw_path}")
        if target.suffix.casefold() in SQLITE_SUFFIXES:
            with closing(
                sqlite3.connect(f"file:{target}?mode=ro&immutable=1", uri=True)
            ) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"checkpoint SQLite integrity check failed: {raw_path}")

    actual: set[str] = set()
    for target in checkpoint.rglob("*"):
        if target.is_symlink():
            raise RuntimeError(
                f"checkpoint contains a symbolic link: {target.relative_to(checkpoint)}"
            )
        if target == manifest_path:
            continue
        if target.is_file():
            actual.add(str(target.relative_to(checkpoint)))
        elif not target.is_dir():
            raise RuntimeError(
                f"checkpoint contains an unsupported file type: {target.relative_to(checkpoint)}"
            )
    if actual != declared:
        raise RuntimeError("checkpoint contents do not match the manifest")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--output", type=Path)
    operation.add_argument("--verify", type=Path, metavar="CHECKPOINT")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.verify:
        manifest = verify_backup(args.verify)
        if not args.quiet:
            print(
                f"PASS: runtime checkpoint verified at {args.verify.resolve()} "
                f"({len(manifest['files'])} files)"
            )
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = (args.output or (BACKUP_ROOT / timestamp)).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = create_backup(output)
    if not args.quiet:
        print(f"PASS: runtime checkpoint created at {output} ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
