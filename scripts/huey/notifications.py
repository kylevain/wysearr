"""Discord lifecycle notification policy.

This module deliberately contains no Discord or acquisition calls.  Producers
describe a logical request event here; the Huey runtime persists and delivers
the returned plan through exactly one channel per event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

try:
    from .results import safe_display_title, sanitize_display_text
except ImportError:  # pragma: no cover - direct container entrypoint
    from results import safe_display_title, sanitize_display_text


EVENT_ROUTES = {
    "request_accepted": "request-status",
    "request_rejected": "request-status",
    "request_completed": "request-status",
    "request_failed": "request-status",
    "download_queued": "download-queue",
    "download_active": "download-queue",
    "download_completed": "download-queue",
    "library_imported": "recent-additions",
    "import_failed": "import-errors",
    "manual_intervention": "import-errors",
    "system_health": "system-health",
}

_SENSITIVE_DETAIL = re.compile(
    r"(?:https?://|ftp://|magnet:|api[\s_-]*key|token|password|secret|authorization)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoutedNotification:
    """One logical event routed to one lifecycle channel."""

    event_key: str
    route: str
    message: str

    def __post_init__(self) -> None:
        expected = EVENT_ROUTES.get(self.event_key)
        if expected is None:
            raise ValueError(f"Unknown notification event: {self.event_key}")
        if self.route != expected:
            raise ValueError(
                f"Notification event {self.event_key} must use route {expected}"
            )
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Notification messages cannot be empty")


def _plan(event_key: str, message: str) -> RoutedNotification:
    return RoutedNotification(event_key, EVENT_ROUTES[event_key], message.strip())


def _request_id(request: Mapping[str, Any], response: Mapping[str, Any] | None = None) -> Any:
    if response is not None and response.get("request_id") is not None:
        return response["request_id"]
    return request.get("id", "unknown")


def _service_name(request: Mapping[str, Any]) -> str:
    raw = sanitize_display_text(request.get("service"), limit=40)
    return raw.title() if raw else "the acquisition workflow"


def _safe_failure_detail(request: Mapping[str, Any]) -> str:
    raw = request.get("error")
    detail = sanitize_display_text(raw, limit=500)
    if not detail or _SENSITIVE_DETAIL.search(str(raw or "")):
        return (
            "The import or acquisition failed. An administrator should review "
            "Huey and BookBot logs."
        )
    return detail


def response_notifications(
    media_type: str,
    response: Mapping[str, Any],
    request: Mapping[str, Any],
) -> tuple[RoutedNotification, ...]:
    """Plan lifecycle messages for the synchronous intake result.

    The Discord acknowledgement is intentionally not represented here: it is a
    reply to the original message, while every plan returned here is a separate
    lifecycle event. Gateway redeliveries/duplicate targets receive another
    acknowledgement but never repeat lifecycle notifications.
    """

    if response.get("duplicate"):
        return ()

    request_id = _request_id(request, response)
    title = safe_display_title(
        response.get("external_title") or request.get("external_title"),
        request.get("title"),
    )
    status = str(response.get("status") or "").casefold()
    service = _service_name({**dict(request), **dict(response)})
    response_detail = sanitize_display_text(response.get("message"), limit=500)

    if status == "queued":
        accepted = _plan(
            "request_accepted",
            f"✅ Request #{request_id} accepted: {title} is now being tracked.",
        )
        queue_detail = response_detail or f"Queued through {service}."
        queued = _plan(
            "download_queued",
            f"⬇️ Request #{request_id} queued for acquisition: {queue_detail}",
        )
        return (accepted, queued)
    if status == "needs_selection":
        detail = response_detail or "The request needs a more specific title."
        plans = [
            _plan(
                "request_rejected",
                f"⚠️ Request #{request_id} needs clarification: {detail}",
            )
        ]
        if response.get("manual_intervention") is True:
            plans.append(
                _plan(
                    "manual_intervention",
                    f"🛠️ Manual review required for request #{request_id}: "
                    f"{title}. {detail}",
                )
            )
        return tuple(plans)
    if status in {"complete", "completed"}:
        return (
            _plan(
                "request_completed",
                f"✅ Request #{request_id} complete: {title} was already imported "
                f"to its DAS library path according to {service}.",
            ),
        )
    if status == "failed":
        detail = response_detail or "The request could not be accepted."
        plans = [
            _plan(
                "request_failed",
                f"❌ Request #{request_id} failed: {title}. {detail}",
            )
        ]
        if response.get("manual_intervention") is True:
            plans.append(
                _plan(
                    "manual_intervention",
                    f"🛠️ Manual review required for request #{request_id}: "
                    f"{title}. {detail}",
                )
            )
        return tuple(plans)
    return ()


def terminal_notifications(
    request: Mapping[str, Any],
) -> tuple[RoutedNotification, ...]:
    """Plan terminal messages from ARR or BookBot reconciliation.

    ``handler_completed`` means the item was already present during intake, so
    it is a completed request but not a new recent addition. Likewise,
    ``handler_failed`` is an acquisition/request failure rather than an import
    failure requiring the import-errors channel.
    """

    request_id = _request_id(request)
    title = safe_display_title(request.get("external_title"), request.get("title"))
    status = str(request.get("status") or "").casefold()
    service_key = str(request.get("service") or "").casefold()
    service = _service_name(request)
    terminal_event = str(request.get("terminal_event_type") or "").casefold()

    if status in {"complete", "completed"}:
        if service_key in {"sonarr", "radarr", "lidarr"}:
            completed_message = (
                f"✅ Request #{request_id} complete: {title} was imported to its "
                f"DAS library path by {service}."
            )
        else:
            completed_message = (
                f"✅ Request #{request_id} complete: {title} was safely imported "
                "to its DAS library path."
            )
        plans = [_plan("request_completed", completed_message)]
        if terminal_event != "handler_completed":
            if service_key in {"sonarr", "radarr", "lidarr"}:
                addition_message = (
                    f"📚 New library item from request #{request_id}: {title} was "
                    f"imported to its DAS library path by {service}."
                )
            else:
                addition_message = (
                    f"📚 New library item from request #{request_id}: {title} was "
                    "safely imported to its DAS library path."
                )
            plans.append(_plan("library_imported", addition_message))
        return tuple(plans)

    detail = _safe_failure_detail(request)
    plans = [
        _plan(
            "request_failed",
            f"❌ Request #{request_id} failed: {title}. {detail}",
        )
    ]
    is_import_failure = terminal_event in {"failed", "bookbot_failed"} or (
        not terminal_event and service_key == "bookbot"
    )
    if is_import_failure:
        plans.append(
            _plan(
                "import_failed",
                f"🛠️ Import failure for request #{request_id}: {title}. "
                f"Manual review is required. {detail}",
            )
        )
    elif terminal_event == "startup_reconciled":
        plans.append(
            _plan(
                "system_health",
                f"⚠️ Huey runtime recovery affected request #{request_id}; "
                "an administrator should review service health and logs.",
            )
        )
    return tuple(plans)
