from __future__ import annotations

from typing import Any

from .common import arr_client


def handle(request: dict[str, Any], services: Any | None = None):
    """Future music-channel handler; enabled as soon as a channel is configured."""

    return arr_client(services, "lidarr").submit(request["title"])
