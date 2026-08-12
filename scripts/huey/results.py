"""Common structured handler results."""

from __future__ import annotations

from typing import Any


RESULT_STATUSES = frozenset({"queued", "needs_selection", "failed", "completed"})


def result(
    status: str,
    message: str,
    *,
    service: str | None = None,
    external_id: str | int | None = None,
    external_title: str | None = None,
) -> dict[str, str | None]:
    if status not in RESULT_STATUSES:
        raise ValueError(f"Invalid handler result status: {status}")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Handler results require a message")
    return {
        "status": status,
        "message": message.strip(),
        "service": service,
        "external_id": str(external_id) if external_id is not None else None,
        "external_title": external_title,
    }


def normalize_result(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise ValueError("Handler returned an invalid result")
    return result(
        value.get("status", "failed"),
        value.get("message", "Request processing failed."),
        service=value.get("service"),
        external_id=value.get("external_id"),
        external_title=value.get("external_title"),
    )
