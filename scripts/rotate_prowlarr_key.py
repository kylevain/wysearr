#!/usr/bin/env python3
"""Rotate Prowlarr's API key through its supported ResetApiKey command.

The default mode is read-only.  ``--apply`` queues one non-retried reset,
learns Prowlarr's server-generated key only from the private ``config.xml``,
and atomically converges the private ``.env`` file.  Downstream intake must be
stopped by the operator before apply and left stopped after any failure.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .bootstrap import ApiClient, ApiError, update_dotenv
except ImportError:
    from bootstrap import ApiClient, ApiError, update_dotenv


STACK_ROOT = Path(__file__).resolve().parents[1]
ENV_RELATIVE = Path(".env")
PROWLARR_CONFIG_RELATIVE = Path("config/prowlarr/config.xml")
MAX_ENV_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
API_KEY_RE = re.compile(r"[0-9a-f]{32}")
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)
HOST_CONFIG_PATH = "/api/v1/config/host"
COMMAND_PATH = "/api/v1/command"
RESET_COMMAND_NAME = "ResetApiKey"
AUTH_REJECTION_STATUSES = frozenset({401, 403})
ACTIVE_COMMAND_STATUSES = frozenset({"queued", "started"})
GENERIC_FAILURE = "Prowlarr API key rotation failed; no credential was displayed."
FORWARD_RECOVERY_FAILURE = (
    "Prowlarr API key rotation requires forward recovery; keep intake stopped. "
    "No credential was displayed."
)


class RotationError(RuntimeError):
    """An intentionally fixed, credential-free rotation failure."""


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
    pending_local_convergence: bool = False
    pending_remote_reset: bool = False
    resumed: bool = False


@dataclass(frozen=True)
class AuthProbe:
    state: str
    api_key: str | None = None


ApiClientFactory = Callable[[str, str, float, int], Any]
DotenvUpdater = Callable[[Path, Mapping[str, str]], Mapping[str, str]]


def _safe_root(value: Path) -> Path:
    """Return an absolute, existing, non-symlink project root."""

    root = Path(os.path.abspath(os.fspath(value)))
    try:
        metadata = root.lstat()
    except OSError:
        raise RotationError(GENERIC_FAILURE) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RotationError(GENERIC_FAILURE)
    return root


def _private_path(root: Path, relative: Path) -> Path:
    """Resolve one fixed project-relative path without traversing symlinks."""

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


def _read_private_bounded(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[str, FileSnapshot]:
    """Read one stable, owner-private regular file without following links."""

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
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(
            os, "O_CLOEXEC", 0
        )
        descriptor = os.open(path, flags)
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
        value = b"".join(blocks)
        after = os.fstat(descriptor)
        if (
            len(value) > maximum_bytes
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_uid,
            )
            != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_uid,
            )
        ):
            raise RotationError(GENERIC_FAILURE)
        text = value.decode("utf-8")
        if "\x00" in text:
            raise RotationError(GENERIC_FAILURE)
        return text, FileSnapshot(
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            modified_ns=after.st_mtime_ns,
            mode=stat.S_IMODE(after.st_mode),
            owner=after.st_uid,
        )
    except RotationError:
        raise
    except (OSError, UnicodeDecodeError):
        raise RotationError(GENERIC_FAILURE) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _assert_snapshot(path: Path, expected: FileSnapshot) -> None:
    try:
        current = path.lstat()
    except OSError:
        raise RotationError(GENERIC_FAILURE) from None
    observed = FileSnapshot(
        device=current.st_dev,
        inode=current.st_ino,
        size=current.st_size,
        modified_ns=current.st_mtime_ns,
        mode=stat.S_IMODE(current.st_mode),
        owner=current.st_uid,
    )
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or observed != expected
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


def _config_key(text: str) -> str:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise RotationError(GENERIC_FAILURE) from None
    keys = root.findall("ApiKey") if root.tag == "Config" else []
    if len(keys) != 1:
        raise RotationError(GENERIC_FAILURE)
    value = (keys[0].text or "").strip()
    if API_KEY_RE.fullmatch(value) is None:
        raise RotationError(GENERIC_FAILURE)
    return value


def _base_url(environment: Mapping[str, str]) -> str:
    host = environment.get("WYSEARR_BIND_ADDRESS", "192.168.4.86").strip()
    if not host or re.fullmatch(r"[A-Za-z0-9_.-]+", host) is None:
        raise RotationError(GENERIC_FAILURE)
    try:
        port = int(environment.get("PROWLARR_PORT", "9696"), 10)
    except (TypeError, ValueError):
        raise RotationError(GENERIC_FAILURE) from None
    if not 1 <= port <= 65535:
        raise RotationError(GENERIC_FAILURE)
    return f"http://{host}:{port}"


def _default_client_factory(
    base_url: str,
    api_key: str,
    timeout: float,
    retries: int,
) -> ApiClient:
    """Create a client whose credential exists only in an HTTP header."""

    return ApiClient(
        base_url,
        headers={"X-Api-Key": api_key},
        timeout=timeout,
        retries=retries,
    )


def _probe_host(client: Any) -> AuthProbe:
    try:
        resource = client.get_json(HOST_CONFIG_PATH)
    except ApiError as exc:
        if exc.status in AUTH_REJECTION_STATUSES:
            return AuthProbe("rejected")
        return AuthProbe("unknown")
    except Exception:
        return AuthProbe("unknown")
    if not isinstance(resource, dict):
        return AuthProbe("unknown")
    value = resource.get("apiKey")
    if not isinstance(value, str) or API_KEY_RE.fullmatch(value) is None:
        return AuthProbe("unknown")
    return AuthProbe("accepted", value)


def _require_host_key(client: Any, expected: str) -> None:
    probe = _probe_host(client)
    if probe.state != "accepted" or probe.api_key != expected:
        raise RotationError(GENERIC_FAILURE)


def _command_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        items = payload["records"]
    else:
        raise RotationError(GENERIC_FAILURE)
    if any(not isinstance(item, dict) for item in items):
        raise RotationError(GENERIC_FAILURE)
    return items


def _reset_commands(client: Any) -> dict[int, str]:
    try:
        items = _command_items(client.get_json(COMMAND_PATH))
    except RotationError:
        raise
    except Exception:
        raise RotationError(GENERIC_FAILURE) from None
    commands: dict[int, str] = {}
    for item in items:
        name = item.get("name", item.get("commandName", ""))
        normalized = re.sub(r"[^a-z0-9]", "", str(name).lower())
        identifier = item.get("id")
        if normalized != RESET_COMMAND_NAME.lower():
            continue
        status_value = item.get("status", "")
        status = re.sub(r"[^a-z]", "", str(status_value).lower())
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier < 1
            or not status
        ):
            raise RotationError(GENERIC_FAILURE)
        commands[identifier] = status
    return commands


def _try_reset_commands(client: Any) -> dict[int, str] | None:
    try:
        return _reset_commands(client)
    except RotationError:
        return None


def _active_command_ids(commands: Mapping[int, str]) -> frozenset[int]:
    return frozenset(
        identifier
        for identifier, status in commands.items()
        if status in ACTIVE_COMMAND_STATUSES
    )


def _try_config_key(path: Path) -> str | None:
    try:
        text, _ = _read_private_bounded(path, maximum_bytes=MAX_CONFIG_BYTES)
        return _config_key(text)
    except RotationError:
        return None


def _response_command_id(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    identifier = payload.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        return None
    return identifier if identifier > 0 else None


def _wait_for_authoritative_key(
    *,
    config_path: Path,
    old_key: str,
    old_client: Any,
    base_url: str,
    client_factory: ApiClientFactory,
    timeout: float,
    retries: int,
    wait_seconds: float,
    prior_commands: Mapping[int, str],
    tracked_command_ids: frozenset[int],
    command_acknowledged: bool,
    response_command_id: int | None,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> str:
    """Resolve an acknowledged or ambiguous reset without ever reposting it."""

    deadline = monotonic() + max(0.0, wait_seconds)
    prior_command_ids = frozenset(prior_commands)
    evidence_of_acceptance = command_acknowledged or bool(tracked_command_ids)
    last_key: str | None = None
    last_old_probe = AuthProbe("unknown")
    while True:
        last_key = _try_config_key(config_path)
        last_old_probe = _probe_host(old_client)
        if last_key is not None and last_key != old_key:
            evidence_of_acceptance = True
            candidate_client = client_factory(base_url, last_key, timeout, retries)
            candidate_probe = _probe_host(candidate_client)
            candidate_commands = _try_reset_commands(candidate_client)
            if (
                candidate_probe.state == "accepted"
                and candidate_probe.api_key == last_key
                and last_old_probe.state == "rejected"
                and candidate_commands is not None
                and not _active_command_ids(candidate_commands)
                and _try_config_key(config_path) == last_key
            ):
                return last_key
        elif last_key == old_key and last_old_probe.state == "accepted":
            observed_commands = _try_reset_commands(old_client)
            if observed_commands is not None and (
                bool(frozenset(observed_commands) - prior_command_ids)
                or bool(_active_command_ids(observed_commands))
                or (
                    response_command_id is not None
                    and response_command_id in observed_commands
                )
            ):
                evidence_of_acceptance = True

        if monotonic() >= deadline:
            if (
                not evidence_of_acceptance
                and last_key == old_key
                and last_old_probe.state == "accepted"
                and last_old_probe.api_key == old_key
            ):
                raise RotationError(GENERIC_FAILURE)
            raise RotationError(FORWARD_RECOVERY_FAILURE)
        sleep(min(0.2, max(0.0, deadline - monotonic())))


def _converge_environment(
    *,
    env_path: Path,
    env_snapshot: FileSnapshot,
    config_path: Path,
    old_key: str,
    authoritative_key: str,
    old_client: Any,
    authoritative_client: Any,
    dotenv_updater: DotenvUpdater,
) -> None:
    """Atomically persist the authoritative key, with forward-only recovery."""

    try:
        if _try_config_key(config_path) != authoritative_key:
            raise RotationError(FORWARD_RECOVERY_FAILURE)
        _assert_snapshot(env_path, env_snapshot)
        try:
            dotenv_updater(env_path, {"PROWLARR_API_KEY": authoritative_key})
        except BaseException:
            # The helper may have completed the atomic replace before raising.
            # Verify actual state rather than attempting an unsafe rollback.
            pass
        persisted_text, _ = _read_private_bounded(
            env_path,
            maximum_bytes=MAX_ENV_BYTES,
        )
        if (
            _environment(persisted_text).get("PROWLARR_API_KEY")
            != authoritative_key
            or _try_config_key(config_path) != authoritative_key
        ):
            raise RotationError(FORWARD_RECOVERY_FAILURE)
        new_probe = _probe_host(authoritative_client)
        old_probe = _probe_host(old_client)
        if (
            new_probe.state != "accepted"
            or new_probe.api_key != authoritative_key
            or old_probe.state != "rejected"
        ):
            raise RotationError(FORWARD_RECOVERY_FAILURE)
    except RotationError as exc:
        if str(exc) == GENERIC_FAILURE:
            raise RotationError(FORWARD_RECOVERY_FAILURE) from None
        raise
    except BaseException:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None


def rotate_prowlarr_key(
    root: Path = STACK_ROOT,
    *,
    apply: bool = False,
    timeout: float = 15.0,
    retries: int = 2,
    config_wait_seconds: float = 30.0,
    client_factory: ApiClientFactory = _default_client_factory,
    dotenv_updater: DotenvUpdater = update_dotenv,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RotationResult:
    """Preflight, rotate, or resume a forward-only local convergence."""

    if timeout <= 0 or not 1 <= retries <= 3 or config_wait_seconds < 0:
        raise RotationError(GENERIC_FAILURE)
    safe_root = _safe_root(root)
    env_path = _private_path(safe_root, ENV_RELATIVE)
    config_path = _private_path(safe_root, PROWLARR_CONFIG_RELATIVE)
    env_text, env_snapshot = _read_private_bounded(
        env_path,
        maximum_bytes=MAX_ENV_BYTES,
    )
    config_text, _ = _read_private_bounded(
        config_path,
        maximum_bytes=MAX_CONFIG_BYTES,
    )
    environment = _environment(env_text)
    env_key = environment.get("PROWLARR_API_KEY", "")
    config_key = _config_key(config_text)
    if API_KEY_RE.fullmatch(env_key) is None:
        raise RotationError(GENERIC_FAILURE)
    base_url = _base_url(environment)
    env_client = client_factory(base_url, env_key, timeout, retries)

    if config_key != env_key:
        # A reset may have succeeded before .env could be replaced.  In this
        # one coherent divergence, config.xml is the forward authority.
        config_client = client_factory(base_url, config_key, timeout, retries)
        env_probe = _probe_host(env_client)
        config_probe = _probe_host(config_client)
        if (
            env_probe.state != "rejected"
            or config_probe.state != "accepted"
            or config_probe.api_key != config_key
            or _try_config_key(config_path) != config_key
        ):
            raise RotationError(GENERIC_FAILURE)
        if not apply:
            return RotationResult(
                applied=False,
                pending_local_convergence=True,
            )
        _converge_environment(
            env_path=env_path,
            env_snapshot=env_snapshot,
            config_path=config_path,
            old_key=env_key,
            authoritative_key=config_key,
            old_client=env_client,
            authoritative_client=config_client,
            dotenv_updater=dotenv_updater,
        )
        return RotationResult(applied=True, resumed=True)

    _require_host_key(env_client, env_key)
    prior_commands = _reset_commands(env_client)
    active_before = _active_command_ids(prior_commands)
    if not apply:
        return RotationResult(
            applied=False,
            pending_remote_reset=bool(active_before),
        )

    command_acknowledged = bool(active_before)
    response_command_id: int | None = None
    if not active_before:
        try:
            response = env_client.post_json(
                COMMAND_PATH,
                {"name": RESET_COMMAND_NAME},
                retry=False,
            )
            command_acknowledged = True
            response_command_id = _response_command_id(response)
        except BaseException:
            # A timeout may occur after Prowlarr accepted this non-idempotent
            # command.  Never repost or try to restore a caller-selected key.
            pass

    try:
        authoritative_key = _wait_for_authoritative_key(
            config_path=config_path,
            old_key=env_key,
            old_client=env_client,
            base_url=base_url,
            client_factory=client_factory,
            timeout=timeout,
            retries=retries,
            wait_seconds=config_wait_seconds,
            prior_commands=prior_commands,
            tracked_command_ids=active_before,
            command_acknowledged=command_acknowledged,
            response_command_id=response_command_id,
            sleep=sleep,
            monotonic=monotonic,
        )
    except RotationError:
        raise
    except BaseException:
        raise RotationError(FORWARD_RECOVERY_FAILURE) from None

    authoritative_client = client_factory(
        base_url,
        authoritative_key,
        timeout,
        retries,
    )
    _converge_environment(
        env_path=env_path,
        env_snapshot=env_snapshot,
        config_path=config_path,
        old_key=env_key,
        authoritative_key=authoritative_key,
        old_client=env_client,
        authoritative_client=authoritative_client,
        dotenv_updater=dotenv_updater,
    )
    return RotationResult(applied=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=STACK_ROOT)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply or resume rotation after downstream intake is stopped; "
            "without this flag only preflight is performed"
        ),
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    if arguments.timeout <= 0 or not 1 <= arguments.retries <= 3:
        print(f"ERROR: {GENERIC_FAILURE}", file=sys.stderr)
        return 1
    try:
        result = rotate_prowlarr_key(
            arguments.root,
            apply=arguments.apply,
            timeout=arguments.timeout,
            retries=arguments.retries,
        )
    except RotationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except BaseException:
        print(f"ERROR: {GENERIC_FAILURE}", file=sys.stderr)
        return 1
    if result.resumed:
        print("Prowlarr API key local convergence resumed and verified.")
    elif result.applied:
        print("Prowlarr API key rotated and locally verified.")
    elif result.pending_local_convergence:
        print(
            "Prowlarr API key reset is pending local convergence; "
            "no changes made."
        )
    elif result.pending_remote_reset:
        print(
            "A Prowlarr API key reset is already pending; no changes made."
        )
    else:
        print("Prowlarr API key rotation preflight passed; no changes made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
