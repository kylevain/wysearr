#!/usr/bin/env python3
"""Rotate the live qBittorrent WebUI password without touching its process.

The default mode is a read-only preflight. ``--apply`` changes only the
write-only ``web_ui_password`` preference, the temporary authentication-failure
guard, and the private local ``.env`` assignment. The operator must quiesce all
qBittorrent credential consumers before apply and converge them from ``.env``
before resuming them.

A private recovery journal makes an ambiguous HTTP response or local write
failure forward-recoverable. No password is accepted through argv or the
process environment, and normal output and errors are deliberately fixed.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

try:
    from .bootstrap import QbittorrentClient, update_dotenv
except ImportError:
    from bootstrap import QbittorrentClient, update_dotenv


STACK_ROOT = Path(__file__).resolve().parents[1]
ENV_RELATIVE = Path(".env")
PRIVATE_DIRECTORY_RELATIVE = Path("config/qbittorrent/.wysearr-private")
JOURNAL_NAME = "password-rotation.json"
LOCK_NAME = "password-rotation.lock"
MAX_ENV_BYTES = 1024 * 1024
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
EXPECTED_VERSION = "v5.1.4"
EXPECTED_WEBAPI_VERSION = "2.11.4"
ROTATION_GUARD_MINIMUM = 1000
HASH_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)
GENERIC_FAILURE = (
    "qBittorrent password rotation failed; no credential was displayed."
)
FORWARD_RECOVERY_FAILURE = (
    "qBittorrent password rotation requires forward recovery; keep all "
    "credential consumers stopped. No credential was displayed."
)

PHASES = (
    "prepared",
    "guarded",
    "remote_attempted",
    "remote_applied",
    "env_converged",
    "guard_restored",
)


class RotationError(RuntimeError):
    """A fixed, credential-free rotation failure."""


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    mode: int
    owner: int


@dataclass(frozen=True)
class RotationResult:
    applied: bool
    pending_recovery: bool = False
    resumed: bool = False
    torrent_count: int = 0


@dataclass(frozen=True)
class Journal:
    phase: str
    username: str
    old_password: str
    new_password: str
    original_auth_fail_limit: int
    guard_auth_fail_limit: int
    torrent_hashes: tuple[str, ...]


@dataclass(frozen=True)
class AuthProbe:
    state: str
    client: Any | None = None


ClientFactory = Callable[[str, float], Any]
PasswordFactory = Callable[[], str]
DotenvUpdater = Callable[[Path, Mapping[str, str]], Mapping[str, str]]


class LiveClient(QbittorrentClient):
    """The bounded production adapter; preference writes are never retried."""

    def version(self) -> str:
        return self.api.request("GET", "/api/v2/app/version").text.strip()

    def webapi_version(self) -> str:
        return self.api.request("GET", "/api/v2/app/webapiVersion").text.strip()

    def set_preferences_once(self, values: Mapping[str, Any]) -> None:
        self.api.post_form_response(
            "/api/v2/app/setPreferences",
            {"json": json.dumps(dict(values), separators=(",", ":"))},
            retry=False,
        )


def _default_client_factory(base_url: str, timeout: float) -> LiveClient:
    return LiveClient(base_url, timeout=timeout, retries=1)


def _safe_root(value: Path) -> Path:
    root = Path(os.path.abspath(os.fspath(value)))
    try:
        metadata = root.lstat()
    except OSError:
        raise RotationError(GENERIC_FAILURE) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RotationError(GENERIC_FAILURE)
    return root


def _fixed_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise RotationError(GENERIC_FAILURE)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError:
            raise RotationError(GENERIC_FAILURE) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RotationError(GENERIC_FAILURE)
    return root / relative


def _snapshot(metadata: os.stat_result) -> FileSnapshot:
    return FileSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        mode=stat.S_IMODE(metadata.st_mode),
        owner=metadata.st_uid,
    )


def _read_private_bounded(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[str, FileSnapshot]:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum_bytes
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
        ):
            raise RotationError(GENERIC_FAILURE)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
        ):
            raise RotationError(GENERIC_FAILURE)
        blocks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        payload = b"".join(blocks)
        after = os.fstat(descriptor)
        if len(payload) > maximum_bytes or _snapshot(after) != _snapshot(before):
            raise RotationError(GENERIC_FAILURE)
        text = payload.decode("utf-8")
        if "\x00" in text:
            raise RotationError(GENERIC_FAILURE)
        return text, _snapshot(after)
    except RotationError:
        raise
    except (OSError, UnicodeDecodeError):
        raise RotationError(GENERIC_FAILURE) from None
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _assert_snapshot(path: Path, expected: FileSnapshot) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise RotationError(GENERIC_FAILURE) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or _snapshot(metadata) != expected
    ):
        raise RotationError(GENERIC_FAILURE)


def _decode_env_value(raw_value: str) -> str:
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        parts = list(lexer)
    except ValueError:
        raise RotationError(GENERIC_FAILURE) from None
    return " ".join(parts)


def _environment(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = ENV_ASSIGNMENT_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name in values:
            raise RotationError(GENERIC_FAILURE)
        values[name] = _decode_env_value(match.group(2))
    return values


def _base_url(environment: Mapping[str, str]) -> str:
    host = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86").strip()
    if not host or re.fullmatch(r"[A-Za-z0-9_.-]+", host) is None:
        raise RotationError(GENERIC_FAILURE)
    try:
        port = int(environment.get("QBITTORRENT_PORT", "8080"), 10)
    except (TypeError, ValueError):
        raise RotationError(GENERIC_FAILURE) from None
    if not 1 <= port <= 65535:
        raise RotationError(GENERIC_FAILURE)
    return f"http://{host}:{port}"


def _valid_credential(value: str) -> bool:
    return bool(
        12 <= len(value) <= 256
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _torrent_hashes(client: Any) -> tuple[str, ...]:
    try:
        torrents = client.torrents()
    except BaseException:
        raise RotationError(GENERIC_FAILURE) from None
    if not isinstance(torrents, list):
        raise RotationError(GENERIC_FAILURE)
    hashes: list[str] = []
    for torrent in torrents:
        value = torrent.get("hash") if isinstance(torrent, dict) else None
        if not isinstance(value, str):
            raise RotationError(GENERIC_FAILURE)
        normalized = value.casefold()
        if HASH_RE.fullmatch(normalized) is None:
            raise RotationError(GENERIC_FAILURE)
        hashes.append(normalized)
    if len(hashes) != len(set(hashes)):
        raise RotationError(GENERIC_FAILURE)
    return tuple(sorted(hashes))


def _require_expected_api(client: Any) -> None:
    try:
        valid = (
            client.version() == EXPECTED_VERSION
            and client.webapi_version() == EXPECTED_WEBAPI_VERSION
        )
    except BaseException:
        valid = False
    if not valid:
        raise RotationError(GENERIC_FAILURE)


def _auth_fail_limit(client: Any) -> int:
    try:
        preferences = client.preferences()
        value = preferences.get("web_ui_max_auth_fail_count")
    except BaseException:
        raise RotationError(GENERIC_FAILURE) from None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RotationError(GENERIC_FAILURE)
    return value


def _probe(
    client_factory: ClientFactory,
    base_url: str,
    timeout: float,
    username: str,
    password: str,
) -> AuthProbe:
    try:
        client = client_factory(base_url, timeout)
        accepted = client.login(username, password)
    except BaseException:
        return AuthProbe("unknown")
    if accepted is True:
        return AuthProbe("accepted", client)
    if accepted is False:
        return AuthProbe("rejected")
    return AuthProbe("unknown")


def _require_auth(
    client_factory: ClientFactory,
    base_url: str,
    timeout: float,
    username: str,
    password: str,
) -> Any:
    probe = _probe(client_factory, base_url, timeout, username, password)
    if probe.state != "accepted" or probe.client is None:
        raise RotationError(GENERIC_FAILURE)
    return probe.client


def _private_directory(root: Path, *, create: bool) -> Path | None:
    parent = _fixed_path(root, Path("config/qbittorrent/.sentinel")).parent
    path = parent / PRIVATE_DIRECTORY_RELATIVE.name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700, follow_symlinks=False)
            metadata = path.lstat()
        except OSError:
            raise RotationError(GENERIC_FAILURE) from None
    except OSError:
        raise RotationError(GENERIC_FAILURE) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise RotationError(GENERIC_FAILURE)
    return path


@contextlib.contextmanager
def _rotation_lock(directory: Path) -> Iterator[None]:
    path = directory / LOCK_NAME
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise RotationError(GENERIC_FAILURE)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RotationError(GENERIC_FAILURE) from None
        yield
    except RotationError:
        raise
    except OSError:
        raise RotationError(GENERIC_FAILURE) from None
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _atomic_private_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}")
    descriptor: int | None = None
    try:
        if path.exists() and (
            path.is_symlink()
            or not path.is_file()
            or stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o600
            or path.stat(follow_symlinks=False).st_uid != os.geteuid()
        ):
            raise RotationError(GENERIC_FAILURE)
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        payload = text.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except RotationError:
        raise
    except OSError:
        raise RotationError(GENERIC_FAILURE) from None
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()


def _journal_payload(journal: Journal) -> str:
    return json.dumps(
        {
            "version": 1,
            "phase": journal.phase,
            "username": journal.username,
            "old_password": journal.old_password,
            "new_password": journal.new_password,
            "original_auth_fail_limit": journal.original_auth_fail_limit,
            "guard_auth_fail_limit": journal.guard_auth_fail_limit,
            "torrent_hashes": list(journal.torrent_hashes),
        },
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _parse_journal(text: str) -> Journal:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise RotationError(GENERIC_FAILURE) from None
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "phase",
        "username",
        "old_password",
        "new_password",
        "original_auth_fail_limit",
        "guard_auth_fail_limit",
        "torrent_hashes",
    }:
        raise RotationError(GENERIC_FAILURE)
    phase = payload.get("phase")
    username = payload.get("username")
    old_password = payload.get("old_password")
    new_password = payload.get("new_password")
    original = payload.get("original_auth_fail_limit")
    guard = payload.get("guard_auth_fail_limit")
    raw_hashes = payload.get("torrent_hashes")
    if (
        payload.get("version") != 1
        or phase not in PHASES
        or not isinstance(username, str)
        or not username
        or not isinstance(old_password, str)
        or not _valid_credential(old_password)
        or not isinstance(new_password, str)
        or not _valid_credential(new_password)
        or secrets.compare_digest(old_password, new_password)
        or isinstance(original, bool)
        or not isinstance(original, int)
        or original < 0
        or isinstance(guard, bool)
        or not isinstance(guard, int)
        or guard < max(original, ROTATION_GUARD_MINIMUM)
        or not isinstance(raw_hashes, list)
        or any(not isinstance(value, str) for value in raw_hashes)
    ):
        raise RotationError(GENERIC_FAILURE)
    hashes = tuple(raw_hashes)
    if (
        hashes != tuple(sorted(hashes))
        or len(hashes) != len(set(hashes))
        or any(HASH_RE.fullmatch(value) is None for value in hashes)
    ):
        raise RotationError(GENERIC_FAILURE)
    return Journal(
        phase=phase,
        username=username,
        old_password=old_password,
        new_password=new_password,
        original_auth_fail_limit=original,
        guard_auth_fail_limit=guard,
        torrent_hashes=hashes,
    )


def _read_journal(path: Path) -> Journal:
    text, _ = _read_private_bounded(path, maximum_bytes=MAX_JOURNAL_BYTES)
    return _parse_journal(text)


def _write_journal(path: Path, journal: Journal) -> None:
    _atomic_private_write(path, _journal_payload(journal))


def _with_phase(journal: Journal, phase: str) -> Journal:
    if phase not in PHASES:
        raise RotationError(GENERIC_FAILURE)
    return Journal(
        phase=phase,
        username=journal.username,
        old_password=journal.old_password,
        new_password=journal.new_password,
        original_auth_fail_limit=journal.original_auth_fail_limit,
        guard_auth_fail_limit=journal.guard_auth_fail_limit,
        torrent_hashes=journal.torrent_hashes,
    )


def _set_phase(path: Path, journal: Journal, phase: str) -> Journal:
    updated = _with_phase(journal, phase)
    try:
        _write_journal(path, updated)
    except RotationError:
        # Every phase transition occurs after a guard or credential mutation
        # may have taken effect. Losing the checkpoint is therefore always a
        # forward-recovery condition, even when the underlying write error was
        # otherwise generic.
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    return updated


def _remove_journal(path: Path) -> None:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise RotationError(GENERIC_FAILURE)
        path.unlink()
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    except OSError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None


def _set_preferences_once(client: Any, values: Mapping[str, Any]) -> None:
    try:
        client.set_preferences_once(values)
    except BaseException:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None


def _ensure_guard(client: Any, journal: Journal) -> None:
    try:
        current_limit = _auth_fail_limit(client)
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    if current_limit == journal.guard_auth_fail_limit:
        return
    _set_preferences_once(
        client,
        {"web_ui_max_auth_fail_count": journal.guard_auth_fail_limit},
    )
    try:
        guarded_limit = _auth_fail_limit(client)
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    if guarded_limit != journal.guard_auth_fail_limit:
        raise RotationError(FORWARD_RECOVERY_FAILURE)


def _require_inventory(client: Any, expected: tuple[str, ...]) -> None:
    try:
        current = _torrent_hashes(client)
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    if current != expected:
        raise RotationError(FORWARD_RECOVERY_FAILURE)


def _reconcile_auth(
    client_factory: ClientFactory,
    base_url: str,
    timeout: float,
    journal: Journal,
    *,
    prefer_new: bool,
) -> tuple[str, Any]:
    candidates = (
        (("new", journal.new_password), ("old", journal.old_password))
        if prefer_new
        else (("old", journal.old_password), ("new", journal.new_password))
    )
    probes: dict[str, AuthProbe] = {}
    for name, password in candidates:
        probes[name] = _probe(
            client_factory,
            base_url,
            timeout,
            journal.username,
            password,
        )
        if probes[name].state == "unknown":
            raise RotationError(FORWARD_RECOVERY_FAILURE)
    accepted = [name for name, probe in probes.items() if probe.state == "accepted"]
    if len(accepted) != 1:
        raise RotationError(FORWARD_RECOVERY_FAILURE)
    name = accepted[0]
    client = probes[name].client
    if client is None:
        raise RotationError(FORWARD_RECOVERY_FAILURE)
    # A successful login from this source clears the failed-attempt counter
    # created by the rejected credential probe.
    cleared = _probe(
        client_factory,
        base_url,
        timeout,
        journal.username,
        journal.new_password if name == "new" else journal.old_password,
    )
    if cleared.state != "accepted" or cleared.client is None:
        raise RotationError(FORWARD_RECOVERY_FAILURE)
    return name, cleared.client


def _preflight(
    *,
    root: Path,
    env_path: Path,
    environment: Mapping[str, str],
    base_url: str,
    timeout: float,
    client_factory: ClientFactory,
) -> RotationResult:
    private = _private_directory(root, create=False)
    journal_path = private / JOURNAL_NAME if private is not None else None
    if journal_path is not None and journal_path.exists():
        try:
            journal = _read_journal(journal_path)
            if (
                environment.get("QBITTORRENT_USERNAME", "admin")
                != journal.username
                or environment.get("QBITTORRENT_PASSWORD", "")
                not in {journal.old_password, journal.new_password}
            ):
                raise RotationError(FORWARD_RECOVERY_FAILURE)
            if journal.phase == "remote_attempted":
                _, client = _reconcile_auth(
                    client_factory,
                    base_url,
                    timeout,
                    journal,
                    prefer_new=True,
                )
            else:
                password = (
                    journal.new_password
                    if journal.phase
                    in {"remote_applied", "env_converged", "guard_restored"}
                    else journal.old_password
                )
                client = _require_auth(
                    client_factory,
                    base_url,
                    timeout,
                    journal.username,
                    password,
                )
            _require_expected_api(client)
            _require_inventory(client, journal.torrent_hashes)
        except RotationError:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None
        return RotationResult(
            applied=False,
            pending_recovery=True,
            torrent_count=len(journal.torrent_hashes),
        )

    username = environment.get("QBITTORRENT_USERNAME", "admin")
    password = environment.get("QBITTORRENT_PASSWORD", "")
    if not username or not _valid_credential(password):
        raise RotationError(GENERIC_FAILURE)
    client = _require_auth(
        client_factory, base_url, timeout, username, password
    )
    _require_expected_api(client)
    _auth_fail_limit(client)
    hashes = _torrent_hashes(client)
    return RotationResult(applied=False, torrent_count=len(hashes))


def _apply_locked(
    *,
    root: Path,
    env_path: Path,
    env_snapshot: FileSnapshot,
    environment: Mapping[str, str],
    base_url: str,
    timeout: float,
    client_factory: ClientFactory,
    password_factory: PasswordFactory,
    dotenv_updater: DotenvUpdater,
    private: Path,
) -> RotationResult:
    journal_path = private / JOURNAL_NAME
    resumed = journal_path.exists()
    initial_client: Any | None = None
    if resumed:
        try:
            journal = _read_journal(journal_path)
        except RotationError:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    else:
        username = environment.get("QBITTORRENT_USERNAME", "admin")
        old_password = environment.get("QBITTORRENT_PASSWORD", "")
        if not username or not _valid_credential(old_password):
            raise RotationError(GENERIC_FAILURE)
        client = _require_auth(
            client_factory, base_url, timeout, username, old_password
        )
        initial_client = client
        _require_expected_api(client)
        original_limit = _auth_fail_limit(client)
        hashes = _torrent_hashes(client)
        try:
            new_password = password_factory()
        except BaseException:
            raise RotationError(GENERIC_FAILURE) from None
        if (
            not isinstance(new_password, str)
            or not _valid_credential(new_password)
            or secrets.compare_digest(old_password, new_password)
        ):
            raise RotationError(GENERIC_FAILURE)
        journal = Journal(
            phase="prepared",
            username=username,
            old_password=old_password,
            new_password=new_password,
            original_auth_fail_limit=original_limit,
            guard_auth_fail_limit=max(original_limit, ROTATION_GUARD_MINIMUM),
            torrent_hashes=hashes,
        )
        _write_journal(journal_path, journal)

    try:
        current_environment = _environment(
            _read_private_bounded(env_path, maximum_bytes=MAX_ENV_BYTES)[0]
        )
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    env_password = current_environment.get("QBITTORRENT_PASSWORD", "")
    if env_password not in {journal.old_password, journal.new_password}:
        raise RotationError(FORWARD_RECOVERY_FAILURE)
    if current_environment.get("QBITTORRENT_USERNAME", "admin") != journal.username:
        raise RotationError(FORWARD_RECOVERY_FAILURE)

    if journal.phase == "guard_restored":
        try:
            final_client = _require_auth(
                client_factory,
                base_url,
                timeout,
                journal.username,
                journal.new_password,
            )
            _require_expected_api(final_client)
            restored_limit = _auth_fail_limit(final_client)
        except RotationError:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None
        _require_inventory(final_client, journal.torrent_hashes)
        if restored_limit != journal.original_auth_fail_limit:
            raise RotationError(FORWARD_RECOVERY_FAILURE)
        _remove_journal(journal_path)
        return RotationResult(
            applied=True,
            resumed=True,
            torrent_count=len(journal.torrent_hashes),
        )

    if journal.phase in {"prepared", "guarded"}:
        try:
            client = initial_client or _require_auth(
                client_factory,
                base_url,
                timeout,
                journal.username,
                journal.old_password,
            )
            _require_expected_api(client)
        except RotationError:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None
        auth_state = "old"
    elif journal.phase == "remote_attempted":
        auth_state, client = _reconcile_auth(
            client_factory,
            base_url,
            timeout,
            journal,
            prefer_new=True,
        )
        try:
            _require_expected_api(client)
        except RotationError:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    else:
        try:
            client = _require_auth(
                client_factory,
                base_url,
                timeout,
                journal.username,
                journal.new_password,
            )
            _require_expected_api(client)
        except RotationError:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None
        auth_state = "new"
    _require_inventory(client, journal.torrent_hashes)
    _ensure_guard(client, journal)
    if PHASES.index(journal.phase) < PHASES.index("guarded"):
        journal = _set_phase(journal_path, journal, "guarded")

    if auth_state == "old":
        journal = _set_phase(journal_path, journal, "remote_attempted")
        try:
            client.set_preferences_once(
                {"web_ui_password": journal.new_password}
            )
        except BaseException:
            # The server may have committed before the response was lost.
            pass
        auth_state, client = _reconcile_auth(
            client_factory,
            base_url,
            timeout,
            journal,
            prefer_new=True,
        )
        if auth_state != "new":
            raise RotationError(FORWARD_RECOVERY_FAILURE)

    journal = _set_phase(journal_path, journal, "remote_applied")

    if env_password == journal.old_password:
        # Only assert the original snapshot in the initial invocation. A
        # resumed run re-reads and validates the current private file above.
        if not resumed:
            try:
                _assert_snapshot(env_path, env_snapshot)
            except RotationError:
                raise RotationError(FORWARD_RECOVERY_FAILURE) from None
        try:
            dotenv_updater(
                env_path,
                {"QBITTORRENT_PASSWORD": journal.new_password},
            )
        except BaseException:
            pass
    try:
        persisted_text, _ = _read_private_bounded(
            env_path, maximum_bytes=MAX_ENV_BYTES
        )
        persisted = _environment(persisted_text)
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    if (
        persisted.get("QBITTORRENT_USERNAME", "admin") != journal.username
        or persisted.get("QBITTORRENT_PASSWORD") != journal.new_password
    ):
        raise RotationError(FORWARD_RECOVERY_FAILURE)
    journal = _set_phase(journal_path, journal, "env_converged")

    try:
        new_client = _require_auth(
            client_factory,
            base_url,
            timeout,
            journal.username,
            journal.new_password,
        )
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    _require_inventory(new_client, journal.torrent_hashes)

    final_state, final_client = _reconcile_auth(
        client_factory,
        base_url,
        timeout,
        journal,
        prefer_new=True,
    )
    if final_state != "new":
        raise RotationError(FORWARD_RECOVERY_FAILURE)

    _set_preferences_once(
        final_client,
        {"web_ui_max_auth_fail_count": journal.original_auth_fail_limit},
    )
    try:
        restored_limit = _auth_fail_limit(final_client)
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None
    if restored_limit != journal.original_auth_fail_limit:
        raise RotationError(FORWARD_RECOVERY_FAILURE)
    journal = _set_phase(journal_path, journal, "guard_restored")

    try:
        _require_auth(
            client_factory,
            base_url,
            timeout,
            journal.username,
            journal.new_password,
        )
    except RotationError:
        raise RotationError(FORWARD_RECOVERY_FAILURE)
    _remove_journal(journal_path)
    return RotationResult(
        applied=True,
        resumed=resumed,
        torrent_count=len(journal.torrent_hashes),
    )


def rotate_qbittorrent_password(
    root: Path = STACK_ROOT,
    *,
    apply: bool = False,
    timeout: float = 15.0,
    client_factory: ClientFactory = _default_client_factory,
    password_factory: PasswordFactory | None = None,
    dotenv_updater: DotenvUpdater = update_dotenv,
) -> RotationResult:
    """Preflight or perform/resume one forward-only password rotation."""

    if timeout <= 0 or timeout > 120:
        raise RotationError(GENERIC_FAILURE)
    safe_root = _safe_root(root)
    env_path = _fixed_path(safe_root, ENV_RELATIVE)
    env_text, env_snapshot = _read_private_bounded(
        env_path, maximum_bytes=MAX_ENV_BYTES
    )
    environment = _environment(env_text)
    base_url = _base_url(environment)
    if not apply:
        return _preflight(
            root=safe_root,
            env_path=env_path,
            environment=environment,
            base_url=base_url,
            timeout=timeout,
            client_factory=client_factory,
        )

    private = _private_directory(safe_root, create=True)
    if private is None:
        raise RotationError(GENERIC_FAILURE)
    with _rotation_lock(private):
        try:
            return _apply_locked(
                root=safe_root,
                env_path=env_path,
                env_snapshot=env_snapshot,
                environment=environment,
                base_url=base_url,
                timeout=timeout,
                client_factory=client_factory,
                password_factory=password_factory
                or (lambda: secrets.token_urlsafe(32)),
                dotenv_updater=dotenv_updater,
                private=private,
            )
        except RotationError:
            raise
        except BaseException:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=STACK_ROOT)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform or resume the no-process-interruption rotation",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        result = rotate_qbittorrent_password(
            arguments.root.resolve(),
            apply=arguments.apply,
            timeout=arguments.timeout,
        )
    except BaseException as exc:
        message = (
            str(exc)
            if isinstance(exc, RotationError)
            and str(exc) in {GENERIC_FAILURE, FORWARD_RECOVERY_FAILURE}
            else GENERIC_FAILURE
        )
        print(f"ERROR: {message}", file=sys.stderr)
        return 1
    if result.applied:
        print(
            "qBittorrent password rotated and local environment converged "
            "without process interruption."
        )
    elif result.pending_recovery:
        print(
            "qBittorrent password rotation has pending forward recovery; "
            "no changes were made."
        )
    else:
        print("qBittorrent password rotation preflight passed; no changes were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
