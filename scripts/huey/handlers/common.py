"""Shared handler dependency access."""

from __future__ import annotations

from typing import Any

try:
    from ..services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from services import ServiceRegistry


def registry(value: Any | None) -> Any:
    return value if value is not None else ServiceRegistry()


def arr_client(value: Any | None, service: str) -> Any:
    services = registry(value)
    if hasattr(services, "arr"):
        return services.arr(service)
    return services[service]


def direct_client(value: Any | None) -> Any:
    services = registry(value)
    if hasattr(services, "direct"):
        return services.direct()
    return services["direct"]


def handle_direct(request: dict[str, Any], services: Any | None = None):
    return direct_client(services).submit(
        request["media_type"], request["title"], request.get("author"), request.get("id")
    )


def handle_book(request: dict[str, Any], services: Any | None = None):
    """Use Shelfarr for books when enabled, otherwise preserve direct qBit."""

    resolved = registry(services)
    if hasattr(resolved, "book"):
        return resolved.book(request)
    return handle_direct(request, resolved)


def handle_audiobook(request: dict[str, Any], services: Any | None = None):
    """Use ABBA when available while retaining the configured rollback path."""

    resolved = registry(services)
    if hasattr(resolved, "audiobook"):
        return resolved.audiobook(request)
    return handle_direct(request, resolved)
