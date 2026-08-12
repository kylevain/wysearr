"""Safe, atomic, source-preserving library imports."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CategorySpec
from .errors import SourceChangedError, UnsafeSourceError, UnsupportedMediaError


LOGGER = logging.getLogger(__name__)
LEGACY_MARKER = re.compile(
    r"\s*\(\s*z-library\.sk\s*,\s*1lib\.sk\s*,\s*z-lib\.sk\s*\)\s*",
    flags=re.IGNORECASE,
)
INVALID_CHARACTERS = re.compile(r"[<>:\"\\|?*\x00-\x1f\x7f]")
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SourceEntry:
    source: Path
    relative_destination: Path
    size: int
    mtime_ns: int
    atime_ns: int
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class ImportPlan:
    source: Path
    destination_root: Path
    destination: Path
    title: str
    directories: tuple[Path, ...]
    files: tuple[SourceEntry, ...]
    total_bytes: int


@dataclass(frozen=True)
class ImportResult:
    destination: Path
    title: str
    copied_files: int
    copied_bytes: int
    archived_path: Path | None = None
    adopted: bool = False


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_component(name: str, max_bytes: int = 180) -> str:
    """Normalize a single path component without permitting traversal."""

    if not isinstance(name, str):
        raise UnsafeSourceError("path component is not text")
    normalized = unicodedata.normalize("NFC", name)
    normalized = LEGACY_MARKER.sub(" ", normalized)
    normalized = INVALID_CHARACTERS.sub("-", normalized)
    normalized = WHITESPACE.sub(" ", normalized).strip(" .")
    if name.lstrip().startswith("."):
        raise UnsafeSourceError("hidden path components are not imported")
    if not normalized or normalized in {".", ".."}:
        raise UnsafeSourceError("path component is empty or traversal-like")
    normalized = _truncate_utf8(normalized, max_bytes).rstrip(" .")
    if not normalized:
        raise UnsafeSourceError("path component became empty after sanitization")
    return normalized


def sanitize_filename(name: str) -> str:
    path = Path(name)
    suffix = path.suffix.lower()
    if not suffix:
        raise UnsupportedMediaError(f"file has no extension: {name}")
    stem = sanitize_component(path.stem)
    return f"{stem}{suffix}"


class LibraryImporter:
    """Validate and atomically copy completed payloads into media libraries."""

    def __init__(self, torrent_root: Path, media_root: Path) -> None:
        self.torrent_root = torrent_root
        self.media_root = media_root

    def plan(self, content_path: str, spec: CategorySpec) -> ImportPlan:
        source = self._validate_source_path(Path(content_path))
        destination_root = self.media_root.joinpath(*spec.destination)
        self._validate_destination_root(destination_root)

        if source.is_file():
            title = sanitize_component(source.stem)
            relative_file = Path(sanitize_filename(source.name))
            source_stat = source.stat(follow_symlinks=False)
            self._validate_extension(source, spec)
            files = (
                SourceEntry(
                    source=source,
                    relative_destination=relative_file,
                    size=source_stat.st_size,
                    mtime_ns=source_stat.st_mtime_ns,
                    atime_ns=source_stat.st_atime_ns,
                    device=source_stat.st_dev,
                    inode=source_stat.st_ino,
                    mode=source_stat.st_mode,
                ),
            )
            directories: tuple[Path, ...] = ()
        elif source.is_dir():
            title = sanitize_component(source.name)
            directories, files = self._walk_directory(source, spec)
        else:
            raise UnsafeSourceError("content path is not a regular file or directory")

        if not files:
            raise UnsupportedMediaError("completed payload contains no regular files")
        primary_files = sum(
            1
            for entry in files
            if entry.source.suffix.lower() in spec.primary_extensions
        )
        if primary_files == 0:
            raise UnsupportedMediaError(
                f"payload contains no supported primary {spec.name} file"
            )

        destination = destination_root / title
        self._ensure_relative(destination, self.media_root, "library destination")
        total_bytes = sum(entry.size for entry in files)
        return ImportPlan(
            source=source,
            destination_root=destination_root,
            destination=destination,
            title=title,
            directories=directories,
            files=files,
            total_bytes=total_bytes,
        )

    def import_payload(
        self,
        content_path: str,
        spec: CategorySpec,
        torrent_hash: str,
        dry_run: bool = False,
    ) -> ImportResult:
        plan = self.plan(content_path, spec)
        if dry_run:
            return ImportResult(
                destination=plan.destination,
                title=plan.title,
                copied_files=len(plan.files),
                copied_bytes=plan.total_bytes,
            )

        adopted = self._adopt_interrupted_import(plan, torrent_hash)
        if adopted is not None:
            return adopted

        plan.destination_root.mkdir(parents=True, exist_ok=True)
        self._validate_destination_root(plan.destination_root)
        free_bytes = shutil.disk_usage(plan.destination_root).free
        if free_bytes < plan.total_bytes:
            raise OSError(
                f"insufficient destination space: need {plan.total_bytes}, have {free_bytes}"
            )

        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".bookbot-{torrent_hash[:12]}-",
                dir=plan.destination_root,
            )
        )
        archived: Path | None = None
        try:
            for relative_directory in plan.directories:
                (temporary / relative_directory).mkdir(parents=True, exist_ok=True)
            for entry in plan.files:
                target = temporary / entry.relative_destination
                target.parent.mkdir(parents=True, exist_ok=True)
                self._copy_regular_file(entry, target)
                if target.stat().st_size != entry.size:
                    raise SourceChangedError(f"copied size changed for {entry.source}")
                self._fsync_file(target)

            self._verify_sources_unchanged(plan.files)
            self._write_import_marker(temporary, torrent_hash)
            self._fsync_tree(temporary)
            archived = self._archive_conflict(
                plan.destination, spec.name, torrent_hash
            )
            try:
                os.replace(temporary, plan.destination)
            except Exception:
                if archived is not None and not os.path.lexists(plan.destination):
                    os.replace(archived, plan.destination)
                raise
            self._fsync_directory(plan.destination_root)
        finally:
            if os.path.lexists(temporary):
                shutil.rmtree(temporary)

        return ImportResult(
            destination=plan.destination,
            title=plan.title,
            copied_files=len(plan.files),
            copied_bytes=plan.total_bytes,
            archived_path=archived,
        )

    def clear_import_marker(self, destination: Path, torrent_hash: str) -> None:
        marker = destination / ".bookbot-import.json"
        if not marker.exists():
            return
        if marker.is_symlink() or not marker.is_file():
            raise UnsafeSourceError("BookBot import marker is not a regular file")
        if self._read_import_marker(marker) != torrent_hash:
            raise UnsafeSourceError("BookBot import marker belongs to another torrent")
        marker.unlink()
        self._fsync_directory(destination)

    def validate_import_destination(
        self, destination: Path, spec: CategorySpec
    ) -> None:
        expected_root = self.media_root.joinpath(*spec.destination)
        if not destination.is_absolute() or destination.parent != expected_root:
            raise UnsafeSourceError("ledger destination is outside its category library")
        self._validate_destination_root(expected_root)
        if destination.is_symlink() or not destination.is_dir():
            raise UnsafeSourceError("ledger destination is not a safe directory")
        if sanitize_component(destination.name) != destination.name:
            raise UnsafeSourceError("ledger destination name is not normalized")

    def _adopt_interrupted_import(
        self, plan: ImportPlan, torrent_hash: str
    ) -> ImportResult | None:
        if not os.path.lexists(plan.destination):
            return None
        if plan.destination.is_symlink() or not plan.destination.is_dir():
            return None
        marker = plan.destination / ".bookbot-import.json"
        if not marker.exists() or marker.is_symlink() or not marker.is_file():
            return None
        if self._read_import_marker(marker) != torrent_hash:
            return None
        return ImportResult(
            destination=plan.destination,
            title=plan.title,
            copied_files=len(plan.files),
            copied_bytes=plan.total_bytes,
            adopted=True,
        )

    @staticmethod
    def _read_import_marker(marker: Path) -> str:
        try:
            with marker.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            raise UnsafeSourceError("BookBot import marker is invalid") from exc
        value = payload.get("torrent_hash") if isinstance(payload, dict) else None
        if not isinstance(value, str):
            raise UnsafeSourceError("BookBot import marker has no torrent hash")
        return value

    @staticmethod
    def _write_import_marker(directory: Path, torrent_hash: str) -> None:
        marker = directory / ".bookbot-import.json"
        with marker.open("x", encoding="utf-8") as handle:
            json.dump({"torrent_hash": torrent_hash}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _validate_source_path(self, candidate: Path) -> Path:
        if not candidate.is_absolute():
            raise UnsafeSourceError("qBittorrent content path must be absolute")
        if not self.torrent_root.exists() or self.torrent_root.is_symlink():
            raise UnsafeSourceError("torrent root is missing or is a symlink")
        root = self.torrent_root.resolve(strict=True)
        try:
            lexical_relative = candidate.relative_to(self.torrent_root)
        except ValueError as exc:
            raise UnsafeSourceError("content path is outside the torrent root") from exc

        cursor = self.torrent_root
        for part in lexical_relative.parts:
            cursor = cursor / part
            try:
                metadata = cursor.lstat()
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"content path is missing: {candidate}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafeSourceError(f"symlink is not allowed in content path: {cursor}")

        resolved = candidate.resolve(strict=True)
        self._ensure_relative(resolved, root, "content path")
        if resolved == root:
            raise UnsafeSourceError("content path must not be the torrent root")
        metadata = resolved.stat(follow_symlinks=False)
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise UnsafeSourceError("content path is not regular content")
        return resolved

    def _walk_directory(
        self, source: Path, spec: CategorySpec
    ) -> tuple[tuple[Path, ...], tuple[SourceEntry, ...]]:
        directories: list[Path] = []
        files: list[SourceEntry] = []
        destinations: set[str] = set()

        for current, directory_names, file_names in os.walk(
            source, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            current_relative = current_path.relative_to(source)
            safe_current = self._sanitize_relative_directory(current_relative)

            for directory_name in sorted(directory_names):
                child = current_path / directory_name
                child_stat = child.lstat()
                if stat.S_ISLNK(child_stat.st_mode):
                    raise UnsafeSourceError(f"symlinked directory rejected: {child}")
                if not stat.S_ISDIR(child_stat.st_mode):
                    raise UnsafeSourceError(f"non-directory entry rejected: {child}")
                safe_child = safe_current / sanitize_component(directory_name)
                key = str(safe_child).casefold()
                if key in destinations:
                    raise UnsafeSourceError(
                        f"sanitization creates duplicate destination: {safe_child}"
                    )
                destinations.add(key)
                directories.append(safe_child)

            for file_name in sorted(file_names):
                child = current_path / file_name
                child_stat = child.lstat()
                if stat.S_ISLNK(child_stat.st_mode):
                    raise UnsafeSourceError(f"symlinked file rejected: {child}")
                if not stat.S_ISREG(child_stat.st_mode):
                    raise UnsafeSourceError(f"special file rejected: {child}")
                self._validate_extension(child, spec)
                safe_file = safe_current / sanitize_filename(file_name)
                key = str(safe_file).casefold()
                if key in destinations:
                    raise UnsafeSourceError(
                        f"sanitization creates duplicate destination: {safe_file}"
                    )
                destinations.add(key)
                files.append(
                    SourceEntry(
                        source=child,
                        relative_destination=safe_file,
                        size=child_stat.st_size,
                        mtime_ns=child_stat.st_mtime_ns,
                        atime_ns=child_stat.st_atime_ns,
                        device=child_stat.st_dev,
                        inode=child_stat.st_ino,
                        mode=child_stat.st_mode,
                    )
                )

        return tuple(directories), tuple(files)

    @staticmethod
    def _sanitize_relative_directory(relative: Path) -> Path:
        if relative == Path("."):
            return Path()
        result = Path()
        for component in relative.parts:
            result /= sanitize_component(component)
        return result

    @staticmethod
    def _validate_extension(path: Path, spec: CategorySpec) -> None:
        extension = path.suffix.lower()
        if extension not in spec.allowed_extensions:
            raise UnsupportedMediaError(
                f"unsupported {spec.name} file extension: {extension or '[none]'}"
            )

    def _validate_destination_root(self, destination: Path) -> None:
        if not self.media_root.exists() or self.media_root.is_symlink():
            raise UnsafeSourceError("media root is missing or is a symlink")
        media_root = self.media_root.resolve(strict=True)
        self._ensure_relative(destination, self.media_root, "destination root")

        cursor = self.media_root
        relative = destination.relative_to(self.media_root)
        for part in relative.parts:
            cursor = cursor / part
            if os.path.lexists(cursor):
                metadata = cursor.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise UnsafeSourceError(
                        f"symlink is not allowed in destination path: {cursor}"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    raise UnsafeSourceError(
                        f"destination path component is not a directory: {cursor}"
                    )
        existing_parent = destination
        while not existing_parent.exists():
            existing_parent = existing_parent.parent
        resolved_parent = existing_parent.resolve(strict=True)
        self._ensure_relative(resolved_parent, media_root, "destination parent")

    def _archive_conflict(
        self, destination: Path, category: str, torrent_hash: str
    ) -> Path | None:
        if not os.path.lexists(destination):
            return None
        metadata = destination.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeSourceError("existing destination is a symlink")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise UnsafeSourceError("existing destination is not regular content")

        archive_root = self.media_root / "duplicates" / sanitize_component(category)
        self._validate_destination_root(archive_root)
        archive_root.mkdir(parents=True, exist_ok=True)
        self._validate_destination_root(archive_root)
        if destination.stat().st_dev != archive_root.stat().st_dev:
            raise UnsafeSourceError("duplicate archive is not on the destination filesystem")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive_name = f"{destination.name}-{stamp}-{torrent_hash[:8]}"
        archive = archive_root / archive_name
        if os.path.lexists(archive):
            archive = archive_root / f"{archive_name}-{uuid.uuid4().hex[:8]}"
        os.replace(destination, archive)
        self._fsync_directory(archive_root)
        return archive

    @staticmethod
    def _verify_sources_unchanged(files: tuple[SourceEntry, ...]) -> None:
        for entry in files:
            metadata = entry.source.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceChangedError(f"source type changed: {entry.source}")
            if (
                metadata.st_dev != entry.device
                or metadata.st_ino != entry.inode
                or metadata.st_size != entry.size
                or metadata.st_mtime_ns != entry.mtime_ns
            ):
                raise SourceChangedError(f"source changed during copy: {entry.source}")

    @staticmethod
    def _copy_regular_file(entry: SourceEntry, target: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(entry.source, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeSourceError(f"source is no longer regular: {entry.source}")
            if (
                metadata.st_dev != entry.device
                or metadata.st_ino != entry.inode
                or metadata.st_size != entry.size
                or metadata.st_mtime_ns != entry.mtime_ns
            ):
                raise SourceChangedError(f"source changed before copy: {entry.source}")
            with os.fdopen(descriptor, "rb") as source_handle:
                descriptor = None
                with target.open("xb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
            os.chmod(target, stat.S_IMODE(entry.mode) & 0o666)
            os.utime(
                target,
                ns=(entry.atime_ns, entry.mtime_ns),
                follow_symlinks=False,
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _ensure_relative(path: Path, root: Path, label: str) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise UnsafeSourceError(f"{label} escapes its allowed root") from exc

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    @classmethod
    def _fsync_tree(cls, root: Path) -> None:
        for current, _, _ in os.walk(root, topdown=False):
            cls._fsync_directory(Path(current))

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            LOGGER.debug("Directory fsync is unavailable for %s", path)
            return
        try:
            os.fsync(descriptor)
        except OSError:
            LOGGER.debug("Directory fsync failed for %s", path)
        finally:
            os.close(descriptor)
