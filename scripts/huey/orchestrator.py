"""Request parsing, deduplication, dispatch, and state transitions."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping

try:
    from .clients import ServiceError
    from .database import RequestStore
    from .handlers import dispatch
    from .parser import RequestParseError, parse_request
    from .results import normalize_result, result
    from .services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import ServiceError
    from database import RequestStore
    from handlers import dispatch
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
    ):
        self.store = store
        self.services = services if services is not None else ServiceRegistry()
        self.dispatcher = dispatcher

    @staticmethod
    def _duplicate_result(record: Mapping[str, Any]) -> dict[str, Any]:
        value = result(
            (
                "completed"
                if record.get("status") == "complete"
                else record.get("status")
                if record.get("status") in {"queued", "needs_selection", "failed", "completed"}
                else "failed"
            ),
            f"This Discord message is already request #{record['id']} (status: {record['status']}).",
            service=record.get("service"),
            external_id=record.get("external_id"),
            external_title=record.get("external_title"),
        )
        value.update({"request_id": record["id"], "duplicate": True})
        return value

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
            return self._duplicate_result(existing)

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
                return self._duplicate_result(record)
            self.store.transition(
                record["id"],
                "needs_selection",
                str(error),
                event_type="parse_rejected",
                error=str(error),
            )
            value = result("needs_selection", str(error))
            value.update({"request_id": record["id"], "duplicate": False})
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
        )
        if not created:
            return self._duplicate_result(record)

        request_id = record["id"]
        self.store.transition(request_id, "processing", "Dispatching request to acquisition service")
        request = dict(record)
        request.update(parsed)

        try:
            handler_result = normalize_result(dict(self.dispatcher(request, self.services)))
        except ServiceError as error:
            safe_message = _safe_service_message(error)
            LOGGER.warning("Request %s service failure: %s", request_id, safe_message)
            handler_result = result(
                "failed",
                f"The acquisition service could not queue this request: {safe_message}",
            )
        except Exception as error:  # Keep Discord alive and avoid logging URLs or secrets.
            LOGGER.error(
                "Request %s failed during %s handling (%s)",
                request_id,
                media_type,
                type(error).__name__,
            )
            handler_result = result(
                "failed",
                "An internal processing error occurred. The request was saved for review.",
            )

        error_message = handler_result["message"] if handler_result["status"] == "failed" else None
        self.store.transition(
            request_id,
            handler_result["status"],
            handler_result["message"],
            event_type=f"handler_{handler_result['status']}",
            service=handler_result["service"],
            external_id=handler_result["external_id"],
            external_title=handler_result["external_title"],
            error=error_message,
        )
        handler_result.update({"request_id": request_id, "duplicate": False})
        return handler_result
