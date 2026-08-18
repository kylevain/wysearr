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
    "submission_uncertain": "import-errors",
    "recovery_uncertain": "system-health",
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


def physical_media_notification(
    event: Mapping[str, Any], *, success: bool
) -> RoutedNotification:
    """Route a trusted disc import without inventing a request lifecycle."""

    title = safe_display_title(event.get("title"), "unidentified physical disc")
    year = event.get("year")
    label = f"{title} ({year})" if year else title
    media_type = str(event.get("media_type") or "movie").casefold()
    owner = "Sonarr" if media_type == "tv" else "the physical-video library" if media_type == "nonstandard" else "Radarr"
    if success:
        return _plan(
            "library_imported",
            f"📚 New library item from physical disc: {label} was imported "
            f"to its DAS library path by {owner}.",
        )
    detail = sanitize_display_text(event.get("error"), limit=500)
    if not detail or _SENSITIVE_DETAIL.search(str(event.get("error") or "")):
        detail = "The physical-media delivery requires administrator review."
    return _plan(
        "import_failed",
        f"🛠️ Physical-disc import needs review: {label}. {detail}",
    )


def _request_id(request: Mapping[str, Any], response: Mapping[str, Any] | None = None) -> Any:
    if response is not None and response.get("request_id") is not None:
        return response["request_id"]
    return request.get("id", "unknown")


def _service_name(request: Mapping[str, Any]) -> str:
    raw = sanitize_display_text(request.get("service"), limit=40)
    return raw.title() if raw else "the acquisition workflow"


def _safe_failure_detail(request: Mapping[str, Any]) -> str:
    media_type = str(request.get("media_type") or "").casefold()
    if media_type in {"ebooks", "audiobooks"}:
        return (
            f"The {'ebook' if media_type == 'ebooks' else 'audiobook'} acquisition "
            "or import failed. An administrator should "
            "review the saved Huey workflow."
        )
    raw = request.get("error")
    detail = sanitize_display_text(raw, limit=500)
    if not detail or _SENSITIVE_DETAIL.search(str(raw or "")):
        service = str(request.get("service") or "").casefold()
        services = (
            "Huey and Shelfarr"
            if service == "shelfarr"
            else "Huey, LazyLibrarian, and BookBot"
            if service == "lazylibrarian"
            else "Huey, ABBA, and BookBot"
            if service == "abba"
            else "Huey and BookBot"
        )
        return (
            "The import or acquisition failed. An administrator should review "
            f"{services} logs."
        )
    return detail


def shelfarr_state_notifications(
    request: Mapping[str, Any], external_status: str
) -> tuple[RoutedNotification, ...]:
    """Plan non-terminal Shelfarr download lifecycle updates.

    Initial acceptance already stages ``download_queued``.  Only the first
    observed download and post-processing states add another outbox event;
    terminal status and library notifications remain the responsibility of
    :func:`terminal_notifications`.
    """

    request_id = _request_id(request)
    title = safe_display_title(request.get("external_title"), request.get("title"))
    status = external_status.strip().casefold()
    ebook = str(request.get("media_type") or "").casefold() == "ebooks"
    if status == "downloading":
        return (
            _plan(
                "download_active",
                f"⬇️ Request #{request_id} is actively downloading"
                f"{' through Shelfarr' if not ebook else ''}: {title}.",
            ),
        )
    if status == "processing":
        return (
            _plan(
                "download_completed",
                f"📥 Request #{request_id} finished downloading; "
                f"{'Huey' if ebook else 'Shelfarr'} is validating and importing "
                f"{title}.",
            ),
        )
    return ()


def abba_state_notifications(
    request: Mapping[str, Any], external_status: str
) -> tuple[RoutedNotification, ...]:
    """Plan backend-neutral audiobook download lifecycle notifications."""

    request_id = _request_id(request)
    title = safe_display_title(request.get("external_title"), request.get("title"))
    status = external_status.strip().casefold()
    if status == "downloading":
        return (
            _plan(
                "download_active",
                f"⬇️ Request #{request_id} is actively downloading: {title}.",
            ),
        )
    if status in {"downloaded", "processing"}:
        return (
            _plan(
                "download_completed",
                f"📥 Request #{request_id} finished downloading; the audiobook is "
                f"being validated and imported: {title}.",
            ),
        )
    return ()


def lazylibrarian_state_notifications(
    request: Mapping[str, Any], external_status: str
) -> tuple[RoutedNotification, ...]:
    """Plan deduplicated LL/qBittorrent progress while BookBot owns success."""

    request_id = _request_id(request)
    title = safe_display_title(request.get("external_title"), request.get("title"))
    status = external_status.strip().casefold()
    if status == "downloading":
        return (
            _plan(
                "download_active",
                f"⬇️ Request #{request_id} is actively downloading: {title}.",
            ),
        )
    if status == "processing":
        return (
            _plan(
                "download_completed",
                f"📥 Request #{request_id} finished downloading; the ebook is "
                f"being validated and imported: {title}.",
            ),
        )
    return ()


def service_correlation_attention_notification(
    request: Mapping[str, Any],
    *,
    service: str,
    startup: bool,
    identity_mismatch: bool = False,
) -> RoutedNotification:
    """Plan a fail-closed correlation alert for a named acquisition owner."""

    request_id = _request_id(request)
    title = safe_display_title(request.get("external_title"), request.get("title"))
    display_service = sanitize_display_text(service, limit=40) or "Acquisition service"
    media_type = str(request.get("media_type") or "").casefold()
    if media_type == "ebooks":
        display_service = "the ebook acquisition workflow"
    elif media_type == "audiobooks":
        display_service = "the audiobook acquisition workflow"
    if startup:
        sentence_service = display_service[:1].upper() + display_service[1:]
        problem = (
            f"{sentence_service} restored a correlation with an unexpected candidate"
            if identity_mismatch
            else f"Huey could not yet restore {display_service} correlation"
        )
        return _plan(
            "recovery_uncertain",
            f"⚠️ {problem} for request #{request_id} after a restart: {title}. "
            "Automatic retry remains blocked; an administrator should review service health.",
        )
    problem = (
        f"{display_service} returned a correlation with an unexpected candidate for"
        if identity_mismatch
        else f"Huey cannot yet confirm whether {display_service} received"
    )
    return _plan(
        "submission_uncertain",
        f"🛠️ Manual review required for request #{request_id}: {problem} {title}. "
        "Automatic retry remains blocked to prevent duplicate acquisition.",
    )


def shelfarr_attention_notification(
    request: Mapping[str, Any], *, import_failure: bool
) -> RoutedNotification:
    """Plan one durable alert while Shelfarr retains a recoverable request."""

    request_id = _request_id(request)
    title = safe_display_title(request.get("external_title"), request.get("title"))
    detail = _safe_failure_detail(request)
    if import_failure:
        return _plan(
            "import_failed",
            f"🛠️ Import failure for request #{request_id}: {title}. "
            f"The request was retained for safe recovery. {detail}",
        )
    return _plan(
        "manual_intervention",
        f"🛠️ Manual review required for request #{request_id}: {title}. {detail}",
    )


def shelfarr_correlation_attention_notification(
    request: Mapping[str, Any], *, startup: bool, format_mismatch: bool = False
) -> RoutedNotification:
    """Alert once while an ambiguous Shelfarr submission stays quarantined.

    An empty correlation lookup cannot prove that Shelfarr rejected a request:
    the request-creation transaction may still be completing.  Huey therefore
    keeps the exact target active and blocks automatic resubmission until the
    correlation appears or an administrator resolves it.
    """

    request_id = _request_id(request)
    title = safe_display_title(request.get("external_title"), request.get("title"))
    ebook = str(request.get("media_type") or "").casefold() == "ebooks"
    if startup:
        problem = (
            (
                "The ebook workflow restored a correlation with an unexpected book "
                "format or confirmed identity"
                if ebook
                else "Shelfarr restored a correlation with an unexpected book format "
                "or confirmed identity"
            )
            if format_mismatch
            else (
                "Huey could not yet restore ebook acquisition correlation"
                if ebook
                else "Huey could not yet restore Shelfarr correlation"
            )
        )
        return _plan(
            "recovery_uncertain",
            f"⚠️ {problem} for request #{request_id} after a restart: "
            f"{title}. Automatic retry remains "
            "blocked; an administrator should review "
            f"{'the saved Huey workflow' if ebook else 'Shelfarr and Huey health'}.",
        )
    problem = (
        (
            "The ebook workflow returned a correlation with an unexpected book format "
            "or confirmed identity for"
            if ebook
            else "Shelfarr returned a correlation with an unexpected book format or "
            "confirmed identity for"
        )
        if format_mismatch
        else (
            "Huey cannot yet confirm whether the ebook workflow received"
            if ebook
            else "Huey cannot yet confirm whether Shelfarr created a request for"
        )
    )
    return _plan(
        "submission_uncertain",
        f"🛠️ Manual review required for request #{request_id}: {problem} "
        f"{title}. Automatic retry remains "
        "blocked to prevent duplicate acquisition.",
    )


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
    if media_type == "ebooks":
        response_detail = {
            "queued": "Huey found a usable ebook release and queued it safely.",
            "needs_selection": (
                "Huey could not prove one exact ebook identity; add an author, "
                "year, or edition."
            ),
            "failed": (
                "Huey could not complete the ebook acquisition; an administrator "
                "can review the saved workflow."
            ),
        }.get(status, response_detail)
    elif media_type == "audiobooks":
        response_detail = {
            "queued": "Huey found a usable audiobook release and queued it safely.",
            "needs_selection": (
                "Huey could not prove one exact audiobook identity; add an author, "
                "narrator, year, or edition."
            ),
            "failed": (
                "Huey could not complete the audiobook acquisition; an "
                "administrator can review the saved workflow."
            ),
        }.get(status, response_detail)

    if status == "awaiting_selection":
        # This is an intake conversation, not an accepted, rejected, or queued
        # lifecycle event.  Huey replies to the original request with the
        # persisted candidate prompt and emits lifecycle notifications only
        # after the requester confirms one candidate.
        return ()

    if (
        status == "queued"
        and str(response.get("external_status") or "").casefold()
        == "submission_uncertain"
    ):
        # Huey owns and deduplicates the request, but service acceptance is
        # not yet known.  Do not claim an accepted/queued acquisition.
        combined = {**dict(request), **dict(response)}
        if str(combined.get("service") or "").casefold() == "abba":
            return (
                service_correlation_attention_notification(
                    combined, service="ABBA", startup=False
                ),
            )
        if str(combined.get("service") or "").casefold() == "lazylibrarian":
            return (
                service_correlation_attention_notification(
                    combined, service="LazyLibrarian", startup=False
                ),
            )
        return (
            shelfarr_correlation_attention_notification(
                combined, startup=False
            ),
        )

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
        plans = [accepted, queued]
        if (
            str(response.get("service") or request.get("service") or "").casefold()
            == "lazylibrarian"
            and str(response.get("external_status") or "").casefold()
            == "processing"
        ):
            plans.extend(
                lazylibrarian_state_notifications(
                    {**dict(request), **dict(response)}, "processing"
                )
            )
        if response.get("manual_intervention") is True:
            plans.append(
                _plan(
                    "manual_intervention",
                    f"🛠️ Manual review required for request #{request_id}: "
                    f"{title}. {queue_detail}",
                )
            )
        return tuple(plans)
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
        if media_type in {"ebooks", "audiobooks"}:
            library_kind = "ebook" if media_type == "ebooks" else "audiobook"
            return (
                _plan(
                    "request_completed",
                    f"✅ Request #{request_id} complete: {title} is available in "
                    f"the {library_kind} library workflow.",
                ),
            )
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
        ebook = str(request.get("media_type") or "").casefold() == "ebooks"
        if ebook:
            completed_message = (
                f"✅ Request #{request_id} complete: {title} was imported to its "
                "ebook DAS library path."
            )
        elif service_key in {"sonarr", "radarr", "lidarr"}:
            completed_message = (
                f"✅ Request #{request_id} complete: {title} was imported to its "
                f"DAS library path by {service}."
            )
        elif service_key == "shelfarr":
            completed_message = (
                f"✅ Request #{request_id} complete: {title} was imported to its "
                "DAS library path by Shelfarr."
            )
        else:
            completed_message = (
                f"✅ Request #{request_id} complete: {title} was safely imported "
                "to its DAS library path."
            )
        plans = [_plan("request_completed", completed_message)]
        if terminal_event != "handler_completed":
            if ebook:
                addition_message = (
                    f"📚 New library item from request #{request_id}: {title} was "
                    "imported to its ebook DAS library path."
                )
            elif service_key in {"sonarr", "radarr", "lidarr"}:
                addition_message = (
                    f"📚 New library item from request #{request_id}: {title} was "
                    f"imported to its DAS library path by {service}."
                )
            elif service_key == "shelfarr":
                addition_message = (
                    f"📚 New library item from request #{request_id}: {title} was "
                    "imported to its DAS library path by Shelfarr."
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
    is_import_failure = terminal_event in {
        "failed",
        "bookbot_failed",
        "shelfarr_import_failed",
    } or (
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
    elif terminal_event == "shelfarr_manual_intervention":
        plans.append(
            _plan(
                "manual_intervention",
                f"🛠️ Manual review required for request #{request_id}: {title}. "
                f"{detail}",
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
