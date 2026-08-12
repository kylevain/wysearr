#!/usr/bin/env python3
"""Container readiness probe for Huey."""

from __future__ import annotations

import os
from pathlib import Path


def is_ready(path: str | Path) -> bool:
    marker = Path(path)
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8").strip() == "ready"
    except OSError:
        return False


def main() -> int:
    path = os.environ.get("HUEY_READY_FILE", "/state/ready")
    return 0 if is_ready(path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
