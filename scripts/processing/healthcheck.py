#!/usr/bin/env python3
"""Container healthcheck for the BookBot worker."""

from __future__ import annotations

import os
from pathlib import Path

from bookbot_lib.errors import ConfigurationError
from bookbot_lib.health import check_health_marker


def positive_integer(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def main() -> int:
    health_path = Path(
        os.environ.get("BOOKBOT_HEALTH_PATH", "/config/bookbot-health.json")
    )
    poll_seconds = positive_integer("POLL_SECONDS", 60)
    max_age = positive_integer(
        "HEALTH_MAX_AGE_SECONDS", max(300, poll_seconds * 3)
    )
    healthy, message = check_health_marker(health_path, max_age)
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigurationError as exc:
        print(f"BookBot healthcheck configuration error: {exc}")
        raise SystemExit(2) from exc
