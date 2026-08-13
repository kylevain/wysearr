"""Request parsing, deduplication, dispatch, and state transitions."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping

try:
    from .clients import ServiceError, SubmissionUncertain
    from .database import RequestStore
    from .handlers import dispatch
    from .matching import request_target_key
    from .notifications import response_notifications
    from .parser import RequestParseError, parse_request
    from .results import normalize_result, result
    from .services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import ServiceError, SubmissionUncertain
    from database import RequestStore
    from handlers import dispatch
    from matching import request_target_key
    from notifications import response_notifications
    from parser import RequestParseError, parse_request
    from results import normalize_result, result
    from services import ServiceRegistry


LOGGER = logging.getLogger("huey.orchestrator")


def _safe_service_message(error: ServiceError) -> str:
    message = str(error).strip()
    if not message or re.search(
        r"(?:https?://|magnet:|api[_-]?key|token|password|secret)", message, re.IGNORECASE
    ):
        return "The acquisition service rejected or could not complete the request."
    return message[:500]


class RequestProcessor:
    def __init__(
        self,
        store: RequestStore,
        *,
        services: Any | None = None,
        dispatcher: Callable[[dict[str, Any], Any], Mapping[str, Any]] = dispatch,
        selection_ttl_seconds: int = 900,
    ):
        if not 1 <= int(selection_ttl_seconds) <= 86_400:
            raise ValueError("selection_ttl_seconds must be between 1 and 86400")
        self.store = store
        self.services = services if services is not None else ServiceRegistry()
        self.dispatcher = dispatcher
        self.selection_ttl_seconds = int(selection_ttl_seconds)

    @staticmethod
    def _duplicate_result(
        record: Mapping[str, Any], delivery_message_id: str | int
    ) -> dict[str, Any]:
        status = str(record.get("status") or "")
        same_delivery = str(record.get("message_id")) == str(delivery_message_id)
        if same_delivery:
            message = (
                f"This Discord message is already request #{record['id']} "
                f"(status: {status})."
            )
        elif status in {"complete", "completed"}:
            message = f"Previous request #{record['id']} already completed this exact target."
        else:
            message = (
                f"This exact target is already tracked as request #{record['id']} "
                f"(status: {status})."
            )
        value = result(
            (
                "completed"
                if status in {"complete", "completed"}
                else "queued"
                if status in {"new", "processing", "queued"}
                else "needs_selection"
                if status == "awaiting_selection"
                else status
                if status in {"needs_selection", "failed"}
                else "failed"
            ),
            message,
            service=record.get("service"),
            external_id=record.get("external_id"),
            external_title=record.get("external_title"),
        )
        value.update({"request_id": record["id"], "duplicate": True})
        return value

    @staticmethod
    def _service_failure_result(
        request_id: int,
        media_type: str,
        callback: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Normalize one acquisition call without leaking service details."""

        try:
            return normalize_result(dict(callback()))
        except SubmissionUncertain:
            LOGGER.warning(
                "Request %s Shelfarr submission outcome requires correlation recovery",
                request_id,
            )
            return result(
                "queued",
                "Shelfarr submission is being reconciled before any retry is allowed.",
                service="shelfarr",
                external_status="submission_uncertain",
            )
        except ServiceError as error:
            safe_message = _safe_service_message(error)
            LOGGER.warning("Request %s service failure: %s", request_id, safe_message)
            return result(
                "failed",
                f"The acquisition service could not queue this request: {safe_message}",
            )
        except Exception as error:  # Keep Discord alive and avoid logging secrets.
            LOGGER.error(
                "Request %s failed during %s handling (%s)",
                request_id,
                media_type,
                type(error).__name__,
            )
            return result(
                "failed",
                "An internal processing error occurred. The request was saved for review.",
            )

    def _persist_handler_result(
        self,
        request_id: int,
        handler_result: dict[str, Any],
        *,
        notifications: tuple[Any, ...] = (),
    ) -> None:
        """Persist a normalized dispatch result, including a candidate prompt."""

        if handler_result["status"] == "awaiting_selection":
            candidates = handler_result.get("selection_proposal")
            if not isinstance(candidates, (list, tuple)) or not candidates:
                raise ValueError("Candidate selection result has no persisted candidates")
            self.store.create_candidate_confirmation(
                request_id,
                candidates,
                ttl_seconds=self.selection_ttl_seconds,
            )
            return

        error_message = (
            handler_result["message"] if handler_result["status"] == "failed" else None
        )
        event_type = (
            "shelfarr_submission_uncertain"
            if handler_result["external_status"] == "submission_uncertain"
            else f"handler_{handler_result['status']}"
        )
        self.store.transition(
            request_id,
            handler_result["status"],
            handler_result["message"],
            event_type=event_type,
            service=handler_result["service"],
            external_id=handler_result["external_id"],
            external_title=handler_result["external_title"],
            external_status=handler_result["external_status"],
            error=error_message,
            notifications=tuple(
                (plan.event_key, plan.route, plan.message) for plan in notifications
            ),
        )

    def process(self, delivery: Mapping[str, Any]) -> dict[str, Any]:
        """Process one serializable Discord delivery synchronously.

        The Discord layer runs this method in a worker thread so SQLite and HTTP
        activity never block the gateway event loop.
        """

        message_id = str(delivery["message_id"])
        existing = self.store.get_by_message_id(message_id)
        if existing is not None:
            self.store.add_event(
                existing["id"], "duplicate_delivery", "Duplicate Discord delivery ignored"
            )
            return self._duplicate_result(existing, message_id)

        content = str(delivery.get("content") or "")
        media_type = str(delivery["media_type"])
        try:
            parsed = parse_request(content, media_type)
        except RequestParseError as error:
            record, created = self.store.create_request(
                discord_user_id=delivery["discord_user_id"],
                discord_username=str(delivery["discord_username"]),
                channel_id=delivery["channel_id"],
                message_id=message_id,
                media_type=media_type,
                raw_request=content,
                title=None,
                author=None,
            )
            if not created:
                return self._duplicate_result(record, message_id)
            value = result("needs_selection", str(error))
            value.update({"request_id": record["id"], "duplicate": False})
            plans = response_notifications(media_type, value, record)
            self.store.transition(
                record["id"],
                "needs_selection",
                str(error),
                event_type="parse_rejected",
                error=str(error),
                notifications=tuple(
                    (plan.event_key, plan.route, plan.message) for plan in plans
                ),
            )
            return value

        record, created = self.store.create_request(
            discord_user_id=delivery["discord_user_id"],
            discord_username=str(delivery["discord_username"]),
            channel_id=delivery["channel_id"],
            message_id=message_id,
            media_type=media_type,
            raw_request=content,
            title=parsed["title"],
            author=parsed["author"],
            target_key=request_target_key(media_type, parsed),
        )
        if not created:
            return self._duplicate_result(record, message_id)

        request_id = record["id"]
        intended_service = (
            "shelfarr"
            if media_type in {"ebooks", "audiobooks"}
            and getattr(self.services, "shelfarr_enabled", False)
            else None
        )
        self.store.transition(
            request_id,
            "processing",
            "Dispatching request to acquisition service",
            service=intended_service,
        )
        request = dict(record)
        request.update(parsed)

        handler_result = self._service_failure_result(
            request_id,
            media_type,
            lambda: self.dispatcher(request, self.services),
        )
        durable_notifications = response_notifications(
            media_type,
            {**handler_result, "request_id": request_id, "duplicate": False},
            request,
        )
        self._persist_handler_result(
            request_id,
            handler_result,
            notifications=durable_notifications,
        )
        handler_result.update({"request_id": request_id, "duplicate": False})
        return handler_result

    def process_candidate_reply(self, delivery: Mapping[str, Any]) -> dict[str, Any]:
        """Claim and continue one reply to a persisted Shelfarr candidate prompt.

        The store performs the authorization, expiry, Discord-delivery dedup,
        and ``awaiting_selection`` to ``processing`` transition atomically.
        Only its explicit ``claimed`` outcome may cross the acquisition boundary.
        """

        claim = self.store.claim_candidate_selection(
            prompt_message_id=str(delivery["prompt_message_id"]),
            reply_message_id=str(delivery["message_id"]),
            discord_user_id=str(delivery["discord_user_id"]),
            channel_id=str(delivery["channel_id"]),
            ordinal=int(delivery["ordinal"]),
        )
        outcome = str(claim.get("outcome") or "not_found")
        request = claim.get("request")
        if outcome != "claimed":
            return {
                "selection_outcome": outcome,
                "request_id": request.get("id") if isinstance(request, Mapping) else None,
                "media_type": (
                    str(request.get("media_type"))
                    if isinstance(request, Mapping) and request.get("media_type")
                    else None
                ),
            }

        option = claim.get("option")
        if not isinstance(request, Mapping) or not isinstance(option, Mapping):
            raise RuntimeError("Candidate selection claim returned incomplete state")
        selected_candidate = option.get("candidate")
        if not isinstance(selected_candidate, Mapping):
            raise RuntimeError("Candidate selection claim returned an invalid snapshot")

        request_id = int(request["id"])
        media_type = str(request["media_type"])

        def mark_dispatch_started() -> None:
            if not self.store.mark_candidate_dispatch_started(request_id):
                raise RuntimeError(
                    "Candidate selection dispatch boundary could not be persisted"
                )

        handler_result = self._service_failure_result(
            request_id,
            media_type,
            lambda: self.services.book_selected(
                request,
                selected_candidate,
                before_create=mark_dispatch_started,
            ),
        )
        # A confirmed candidate cannot create a second confirmation session.
        # If fresh Shelfarr revalidation can no longer identify it, the client
        # returns the legacy needs_selection state and releases the target.
        if handler_result["status"] == "awaiting_selection":
            handler_result = result(
                "needs_selection",
                "That candidate is no longer available. Submit the title again to search anew.",
                service="shelfarr",
            )
        if handler_result["status"] in {"queued", "completed"}:
            handler_result["message"] = (
                "Confirmed. Continuing request. " + str(handler_result["message"])
            )
        else:
            handler_result["message"] = (
                "Selection received, but acquisition could not continue. "
                + str(handler_result["message"])
            )
        durable_notifications = response_notifications(
            media_type,
            {**handler_result, "request_id": request_id, "duplicate": False},
            request,
        )
        self._persist_handler_result(
            request_id,
            handler_result,
            notifications=durable_notifications,
        )
        handler_result.update(
            {
                "request_id": request_id,
                "duplicate": False,
                "selection_outcome": "claimed",
                "media_type": media_type,
            }
        )
        return handler_result
