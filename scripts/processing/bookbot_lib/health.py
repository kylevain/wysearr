"""Atomic service health marker and command-line health check."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def write_health_marker(
    path: Path,
    status: str,
    *,
    counts: dict[str, int] | None = None,
    message: str | None = None,
    now: int | None = None,
) -> None:
    timestamp = int(time.time()) if now is None else int(now)
    payload: dict[str, Any] = {
        "status": status,
        "timestamp": timestamp,
        "counts": counts or {},
    }
    if message:
        payload["message"] = message[:1000]
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def check_health_marker(
    path: Path, max_age_seconds: int, now: int | None = None
) -> tuple[bool, str]:
    timestamp = int(time.time()) if now is None else int(now)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return False, f"health marker unavailable: {exc}"
    if not isinstance(payload, dict):
        return False, "health marker is not an object"
    if payload.get("status") != "ok":
        return False, f"health status is {payload.get('status', 'missing')}"
    marker_time = payload.get("timestamp")
    if not isinstance(marker_time, int):
        return False, "health marker timestamp is invalid"
    age = timestamp - marker_time
    if age < -60:
        return False, "health marker timestamp is in the future"
    if age > max_age_seconds:
        return False, f"health marker is stale ({age} seconds old)"
    return True, "BookBot healthy"
