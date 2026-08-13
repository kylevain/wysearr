"""Common structured handler results."""

from __future__ import annotations

import re
from typing import Any


RESULT_STATUSES = frozenset({"queued", "needs_selection", "failed", "completed"})
_SENSITIVE_DISPLAY_TEXT = re.compile(
    r"(?:https?://|ftp://|www\.|discord\.gg/|magnet:|"
    r"(?:api[\s_-]*key|token|password|secret|authorization)\s*[:=]|"
    r"authorization\s+bearer\s+)",
    re.IGNORECASE,
)


def sanitize_display_text(value: object, *, limit: int = 160) -> str | None:
    """Return inert bounded text, or ``None`` when metadata is sensitive/empty."""

    text = "".join(
        character for character in str(value or "") if character.isprintable()
    )
    text = " ".join(text.split())
    if not text or _SENSITIVE_DISPLAY_TEXT.search(text):
        return None
    text = (
        text.replace("@", "＠")
        .replace("`", "'")
        .replace("<", "‹")
        .replace(">", "›")
    )
    bounded_limit = max(16, min(int(limit), 500))
    if len(text) > bounded_limit:
        text = text[: bounded_limit - 3].rstrip() + "..."
    return text


def safe_display_title(value: object, fallback: object = None) -> str:
    """Prefer sanitized service metadata, then sanitized request text."""

    return (
        sanitize_display_text(value)
        or sanitize_display_text(fallback)
        or "your request"
    )


def result(
    status: str,
    message: str,
    *,
    service: str | None = None,
    external_id: str | int | None = None,
    external_title: str | None = None,
    external_status: str | None = None,
    manual_intervention: bool = False,
) -> dict[str, str | bool | None]:
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
        "external_status": external_status,
        "manual_intervention": bool(manual_intervention),
    }


def normalize_result(value: Any) -> dict[str, str | bool | None]:
    if not isinstance(value, dict):
        raise ValueError("Handler returned an invalid result")
    return result(
        value.get("status", "failed"),
        value.get("message", "Request processing failed."),
        service=value.get("service"),
        external_id=value.get("external_id"),
        external_title=value.get("external_title"),
        external_status=value.get("external_status"),
        manual_intervention=value.get("manual_intervention") is True,
    )
