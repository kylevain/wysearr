from __future__ import annotations

from typing import Any

from .common import arr_client


def handle(request: dict[str, Any], services: Any | None = None):
    kind = request.get("kind")
    service = {"movie": "radarr", "tv": "sonarr"}.get(kind)
    if service is None:
        raise ValueError("Movie/TV requests require a movie or tv kind")
    return arr_client(services, service).submit(request["title"])
