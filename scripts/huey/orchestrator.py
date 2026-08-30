"""Request parsing, deduplication, dispatch, and state transitions."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping

try:
    from .clients import CanonicalAcquisition, ServiceError, SubmissionUncertain
    from .database import (
        EbookCascadeStateError,
        EbookIdentityCollision,
        LazyLibrarianHashCollision,
        RequestStore,
    )
    from .handlers import dispatch
    from .matching import identifies_a_work, request_target_key
    from .notifications import response_notifications
    from .parser import RequestParseError, parse_request
    from .results import SELECTION_DECLINE_STATUSES, normalize_result, result
    from .services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import CanonicalAcquisition, ServiceError, SubmissionUncertain
    from database import (
        EbookCascadeStateError,
        EbookIdentityCollision,
        LazyLibrarianHashCollision,
        RequestStore,
    )
    from handlers import dispatch
    from matching import identifies_a_work, request_target_key
    from notifications import response_notifications
    from parser import RequestParseError, parse_request
    from results import SELECTION_DECLINE_STATUSES, normalize_result, result
    from services import ServiceRegistry


LOGGER = logging.getLogger("huey.orchestrator")

# One requester-facing sentence per decline reason. Collapsing them into a
# single generic line made "nothing was found" indistinguishable from "found,
# but too close to tell apart" -- to the requester, to the lifecycle channels,
# and to Louie. The acquisition backend is still never named here: the reason
# travels as state on the request, not as text.
_AUDIOBOOK_DECLINE_MESSAGES = {
    "selection_no_results": (
        "Huey found no audiobook matching that title. Check the spelling, or "
        "try again later if it is a new release."
    ),
    "selection_low_confidence": (
        "Huey found audiobook releases but none close enough to that title to "
        "offer safely. Add the author, narrator, year, or edition."
    ),
    "selection_ambiguous": (
        "Huey found audiobook releases it could not tell apart. Add the "
        "narrator, year, format, or edition."
    ),
}
if set(_AUDIOBOOK_DECLINE_MESSAGES) != SELECTION_DECLINE_STATUSES:  # pragma: no cover
    raise RuntimeError("Audiobook decline messages must cover every decline reason")


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
        retry_state = str(record.get("_unavailable_retry_state") or "")
        same_delivery = str(record.get("message_id")) == str(delivery_message_id)
        if retry_state:
            message = (
                f"This exact target is already tracked as request #{record['id']} "
                f"in the unavailable retry backlog (state: {retry_state})."
            )
        elif same_delivery:
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
                if retry_state in {"queued", "retrying", "awaiting_import"}
                else "failed"
                if retry_state == "blocked"
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
        intended_service: str | None,
        callback: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Normalize one acquisition call without leaking service details."""

        try:
            return normalize_result(dict(callback()))
        except CanonicalAcquisition as canonical:
            value = result(
                "queued",
                "This exact audiobook acquisition is already tracked.",
                service="abba",
                external_id=canonical.info_hash or None,
                external_title=canonical.title,
                external_status="canonical_duplicate",
            )
            value.update(
                {
                    "_canonical_request_id": canonical.owner_request_id,
                    "_candidate_id": canonical.candidate_id,
                    "_canonical_candidate_id": canonical.canonical_candidate_id,
                }
            )
            return value
        except SubmissionUncertain:
            service = intended_service or "acquisition service"
            LOGGER.warning(
                "Request %s %s submission outcome requires correlation recovery",
                request_id,
                service,
            )
            return result(
                "queued",
                "The acquisition submission is being reconciled before any retry is allowed.",
                service=intended_service,
                external_status="submission_uncertain",
            )
        except ServiceError as error:
            safe_message = _safe_service_message(error)
            LOGGER.warning("Request %s service failure: %s", request_id, safe_message)
            return result(
                "failed",
                f"The acquisition service could not queue this request: {safe_message}",
                service=(
                    intended_service
                    if intended_service == "lazylibrarian"
                    else None
                ),
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
        request: Mapping[str, Any],
        notifications: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None:
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
            return None

        # A ``needs_selection`` row already carries its reason in ``error`` when
        # a candidate prompt expires or a confirmation fails. Recording it for
        # an intake decline too is what lets a reader see why a request is
        # sitting in clarification without replaying the Discord channel.
        error_message = (
            handler_result["message"]
            if handler_result["status"] in {"failed", "needs_selection"}
            else None
        )
        event_type = (
            f"{handler_result['service'] or 'acquisition'}_submission_uncertain"
            if handler_result["external_status"] == "submission_uncertain"
            else f"handler_{handler_result['status']}"
        )
        try:
            persisted = self.store.transition(
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
                    (plan.event_key, plan.route, plan.message)
                    for plan in notifications
                ),
            )
        except LazyLibrarianHashCollision:
            if handler_result.get("service") != "lazylibrarian":
                raise
            collision = result(
                "queued",
                "The qBittorrent download identity is already owned by another "
                "active LazyLibrarian request. Huey retained this request for "
                "manual correlation and will not repeat the search.",
                service="lazylibrarian",
                external_title=handler_result.get("external_title"),
                external_status="submission_uncertain",
            )
            collision_plans = response_notifications(
                str(request["media_type"]),
                {**collision, "request_id": request_id, "duplicate": False},
                request,
            )
            self.store.transition(
                request_id,
                "queued",
                collision["message"],
                event_type="lazylibrarian_submission_uncertain",
                service="lazylibrarian",
                external_status="submission_uncertain",
                notifications=tuple(
                    (plan.event_key, plan.route, plan.message)
                    for plan in collision_plans
                ),
            )
            handler_result.clear()
            handler_result.update(collision)
            return None
        return persisted

    @staticmethod
    def _generic_audiobook_result(value: Mapping[str, Any]) -> dict[str, Any]:
        """Present one backend-neutral audiobook acquisition product."""

        normalized = normalize_result(dict(value))
        status = normalized["status"]
        uncertain = normalized.get("external_status") == "submission_uncertain"
        if status == "awaiting_selection":
            message = (
                "Huey found multiple close audiobook matches. Choose one verified "
                "title before acquisition starts."
            )
        elif status == "queued" and uncertain:
            message = (
                "Huey is reconciling the audiobook acquisition before any retry "
                "is allowed."
            )
        elif status == "queued":
            message = "Found an audiobook match and queued it for download."
        elif status == "completed":
            message = "This exact audiobook is already available in the library workflow."
        elif status == "needs_selection":
            message = _AUDIOBOOK_DECLINE_MESSAGES.get(
                str(normalized.get("external_status") or ""),
                "Huey could not prove one exact audiobook identity. Add the author, "
                "narrator, year, or edition and try again.",
            )
        else:
            message = (
                "Huey could not complete this audiobook acquisition. An "
                "administrator can review the saved request safely."
            )
        normalized["message"] = message
        return normalized

    def _coalesce_canonical_audiobook(
        self,
        request_id: int,
        handler_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner_id = int(handler_result["_canonical_request_id"])
        info_hash = str(handler_result.get("external_id") or "")
        return self.store.coalesce_abba_request(
            int(request_id),
            owner_id,
            candidate_id=str(handler_result.get("_candidate_id") or "") or None,
            canonical_candidate_id=(
                str(handler_result.get("_canonical_candidate_id") or "") or None
            ),
            info_hash=info_hash or None,
        )

    @staticmethod
    def _generic_ebook_result(value: Mapping[str, Any]) -> dict[str, Any]:
        """Keep backend provenance internal while presenting one Huey product."""

        normalized = normalize_result(dict(value))
        status = normalized["status"]
        uncertain = normalized.get("external_status") == "submission_uncertain"
        if status == "awaiting_selection":
            message = (
                "Huey found multiple close metadata matches. Choose one verified "
                "title before acquisition starts."
            )
        elif status == "queued" and uncertain:
            message = (
                "Huey is reconciling the acquisition before any retry is allowed."
            )
        elif status == "queued":
            message = "Found a match and queued it for download."
        elif status == "completed":
            message = "This exact ebook is already available in the library workflow."
        elif status == "needs_selection":
            message = (
                "Huey could not prove one exact ebook identity. Add the author, "
                "year, or edition and try again."
            )
        else:
            message = (
                "Huey could not complete this ebook acquisition. An administrator "
                "can review the saved request safely."
            )
        normalized["message"] = message
        return normalized

    @staticmethod
    def _ebook_exhausted_result() -> dict[str, Any]:
        return result(
            "failed",
            "I couldn't find a usable ebook release for that title. Try adding "
            "the author, year, or edition.",
        )

    def _ebook_notifications(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> tuple[tuple[str, str, str], ...]:
        if self._silent_unavailable_retry(int(request["id"])):
            return ()
        plans = response_notifications(
            "ebooks",
            {**dict(response), "request_id": int(request["id"]), "duplicate": False},
            request,
        )
        return tuple((plan.event_key, plan.route, plan.message) for plan in plans)

    def _silent_unavailable_retry(self, request_id: int) -> bool:
        checker = getattr(self.store, "unavailable_retry_is_silent", None)
        return bool(callable(checker) and checker(int(request_id)) is True)

    def _tag_lazylibrarian_owner(
        self, request_id: int, download_id: object
    ) -> None:
        """Attach BookBot correlation only after the DB accepts hash ownership."""

        try:
            qbittorrent = self.services.qbittorrent()
            add_tags = getattr(qbittorrent, "add_tags", None)
            if not callable(add_tags):
                raise ServiceError("qBittorrent tagging is unavailable.")
            add_tags(str(download_id), f"huey-{int(request_id)}")
        except Exception as error:
            # The exact DB/hash owner is already durable.  LL reconciliation
            # restores this idempotent tag before BookBot can mark completion;
            # never authorize fallback merely because correlation is delayed.
            LOGGER.warning(
                "LazyLibrarian correlation tag deferred for request %s (%s)",
                int(request_id),
                type(error).__name__,
            )

    def _run_ebook_cascade(
        self,
        request: Mapping[str, Any],
        *,
        selected_candidate: Mapping[str, Any] | None = None,
        allow_prompt: bool = True,
        retry_now: Any | None = None,
    ) -> dict[str, Any]:
        """Run serial attempts until one owns acquisition or policy exhausts."""

        request_id = int(request["id"])
        while True:
            cascade = self.store.get_ebook_cascade(request_id)
            if cascade is None:
                raise EbookCascadeStateError("Ebook request has no cascade state")
            ordinal = int(cascade["current_ordinal"])
            backend = str(cascade["policy"][ordinal])

            if selected_candidate is not None and cascade.get("identity") is None:
                try:
                    self.store.set_ebook_identity(
                        request_id, backend, selected_candidate
                    )
                except EbookIdentityCollision as collision:
                    self.store.force_unavailable_retry(collision.owner_request_id)
                    collision_message = (
                        f"This exact ebook is already tracked as request "
                        f"#{collision.owner_request_id}."
                    )
                    self.store.terminalize_ebook_cascade(
                        request_id,
                        backend,
                        "failed",
                        collision_message,
                        event_type="ebook_identity_collision",
                        duplicate_owner_request_id=collision.owner_request_id,
                    )
                    duplicate = result("queued", collision_message)
                    duplicate.update(
                        {"request_id": request_id, "duplicate": True}
                    )
                    return duplicate

            self.store.begin_ebook_attempt(request_id, backend)
            cascade = self.store.get_ebook_cascade(request_id)
            if cascade is None:  # pragma: no cover - transaction invariant
                raise EbookCascadeStateError("Ebook cascade disappeared")

            current_request = self.store.get_request(request_id)
            if current_request is None:
                raise KeyError(f"Unknown request ID: {request_id}")
            dispatch_request = {**dict(current_request), **dict(request)}
            dispatch_request["id"] = request_id
            dispatch_request["service"] = backend

            def on_resolved(
                identity: Mapping[str, Any], backend_identity: str
            ) -> None:
                latest = self.store.get_ebook_cascade(request_id)
                authoritative = (
                    latest.get("identity") if latest is not None else None
                )
                self.store.set_ebook_identity(
                    request_id,
                    backend,
                    (
                        authoritative
                        if isinstance(authoritative, Mapping)
                        else identity
                    ),
                    backend_identity=backend_identity,
                    backend_aliases=(
                        tuple(identity.get("source_work_ids", ()))
                        if backend == "shelfarr"
                        else ()
                    ),
                )

            def before_dispatch(candidate_id: str | None = None) -> None:
                if not self.store.lock_ebook_mutation(
                    request_id,
                    backend,
                    backend_identity=candidate_id,
                ):
                    raise SubmissionUncertain(
                        "The ebook acquisition boundary is already owned."
                    )

            dispatch_request["_on_resolved"] = on_resolved
            dispatch_request["_before_dispatch"] = before_dispatch
            cascade_identity = cascade.get("identity")
            submit_backend = getattr(self.services, "submit_ebook_backend")
            try:
                raw_result = submit_backend(
                    dispatch_request,
                    backend,
                    resolved_identity=cascade_identity,
                    selected_candidate=selected_candidate,
                )
                handler_result = normalize_result(dict(raw_result))
            except EbookIdentityCollision as collision:
                self.store.force_unavailable_retry(collision.owner_request_id)
                collision_message = (
                    f"This exact ebook is already tracked as request "
                    f"#{collision.owner_request_id}."
                )
                self.store.terminalize_ebook_cascade(
                    request_id,
                    backend,
                    "failed",
                    collision_message,
                    event_type="ebook_identity_collision",
                    duplicate_owner_request_id=collision.owner_request_id,
                )
                duplicate = result("queued", collision_message)
                duplicate.update(
                    {"request_id": request_id, "duplicate": True}
                )
                return duplicate
            except SubmissionUncertain:
                handler_result = result(
                    "queued",
                    "Huey is reconciling the acquisition before any retry is allowed.",
                    service=backend,
                    external_status="submission_uncertain",
                )
            except ServiceError as error:
                latest = self.store.get_ebook_cascade(request_id)
                if latest is not None and latest.get("mutation_backend") is not None:
                    LOGGER.warning(
                        "Ebook request %s entered uncertainty after its mutation boundary",
                        request_id,
                    )
                    handler_result = result(
                        "queued",
                        "Huey is reconciling the acquisition before any retry is allowed.",
                        service=backend,
                        external_status="submission_uncertain",
                    )
                else:
                    safe_message = _safe_service_message(error)
                    LOGGER.warning(
                        "Ebook request %s backend unavailable before mutation: %s",
                        request_id,
                        safe_message,
                    )
                    unavailable = result(
                        "failed",
                        "An ebook backend was unavailable before acquisition began.",
                        service=backend,
                    )
                    exhausted = self._ebook_exhausted_result()
                    next_backend = self.store.advance_ebook_backend(
                        request_id,
                        backend,
                        "unavailable",
                        unavailable,
                        final_message=exhausted["message"],
                        notifications=self._ebook_notifications(
                            dispatch_request, exhausted
                        ),
                        now=retry_now,
                    )
                    if next_backend is not None:
                        continue
                    exhausted.update(
                        {"request_id": request_id, "duplicate": False}
                    )
                    return exhausted
            except Exception as error:
                LOGGER.error(
                    "Ebook request %s failed during cascade handling (%s)",
                    request_id,
                    type(error).__name__,
                )
                latest = self.store.get_ebook_cascade(request_id)
                if latest is not None and latest.get("mutation_backend") is not None:
                    handler_result = result(
                        "queued",
                        "Huey is reconciling the acquisition before any retry is allowed.",
                        service=backend,
                        external_status="submission_uncertain",
                    )
                else:
                    internal = self._generic_ebook_result(
                        result(
                            "failed",
                            "Huey saved an internal processing failure for review.",
                            service=backend,
                        )
                    )
                    self.store.terminalize_ebook_cascade(
                        request_id,
                        backend,
                        "failed",
                        internal["message"],
                        notifications=self._ebook_notifications(
                            dispatch_request, internal
                        ),
                    )
                    internal.update(
                        {"request_id": request_id, "duplicate": False}
                    )
                    return internal

            handler_result = self._generic_ebook_result(handler_result)
            latest = self.store.get_ebook_cascade(request_id)
            authoritative_identity = (
                latest.get("identity") if latest is not None else None
            )
            if (
                isinstance(authoritative_identity, Mapping)
                and (
                    handler_result["status"] in {
                        "awaiting_selection",
                        "needs_selection",
                    }
                    or handler_result.get("backend_outcome") == "ambiguous"
                )
            ):
                # Once one work has been resolved, a later provider cannot ask
                # the requester to choose a different identity.  Failure to map
                # that authoritative work exactly is a pre-mutation backend miss.
                if latest.get("mutation_backend") is not None:
                    handler_result = self._generic_ebook_result(
                        result(
                            "queued",
                            "Huey is reconciling the acquisition before any retry is allowed.",
                            service=backend,
                            external_status="submission_uncertain",
                        )
                    )
                else:
                    handler_result = self._generic_ebook_result(
                        result(
                            "needs_selection",
                            "This backend could not prove an exact mapping for the resolved work.",
                            service=backend,
                            backend_outcome="miss",
                            resolved_identity=authoritative_identity,
                        )
                    )
            if handler_result["status"] == "awaiting_selection":
                if allow_prompt:
                    self.store.create_candidate_confirmation(
                        request_id,
                        handler_result["selection_proposal"],
                        ttl_seconds=self.selection_ttl_seconds,
                    )
                    handler_result.update(
                        {"request_id": request_id, "duplicate": False}
                    )
                    return handler_result
                handler_result = self._generic_ebook_result(
                    result(
                        "needs_selection",
                        "Huey needs the requester to resolve this title again.",
                        service=backend,
                    )
                )

            if handler_result.get("backend_outcome") == "miss":
                exhausted = self._ebook_exhausted_result()
                next_backend = self.store.advance_ebook_backend(
                    request_id,
                    backend,
                    "miss",
                    handler_result,
                    final_message=exhausted["message"],
                    notifications=self._ebook_notifications(
                        dispatch_request, exhausted
                    ),
                    now=retry_now,
                )
                if next_backend is not None:
                    continue
                exhausted.update({"request_id": request_id, "duplicate": False})
                return exhausted

            if handler_result["status"] == "needs_selection" or handler_result.get(
                "backend_outcome"
            ) == "ambiguous":
                self.store.terminalize_ebook_cascade(
                    request_id,
                    backend,
                    "needs_selection",
                    handler_result["message"],
                    notifications=self._ebook_notifications(
                        dispatch_request, handler_result
                    ),
                    event_type="ebook_identity_ambiguous",
                )
                handler_result.update(
                    {"request_id": request_id, "duplicate": False}
                )
                return handler_result

            notifications = self._ebook_notifications(
                dispatch_request, handler_result
            )
            try:
                self.store.persist_ebook_result(
                    request_id,
                    backend,
                    handler_result,
                    notifications=notifications,
                )
            except LazyLibrarianHashCollision:
                if backend != "lazylibrarian":
                    raise
                handler_result = self._generic_ebook_result(
                    result(
                        "queued",
                        "Huey is reconciling the acquisition before any retry is allowed.",
                        service=backend,
                        external_status="submission_uncertain",
                    )
                )
                notifications = self._ebook_notifications(
                    dispatch_request, handler_result
                )
                self.store.persist_ebook_result(
                    request_id,
                    backend,
                    handler_result,
                    notifications=notifications,
                )
            if (
                backend == "lazylibrarian"
                and handler_result.get("external_id") is not None
                and handler_result.get("external_status") != "submission_uncertain"
                and handler_result["status"] in {"queued", "completed"}
            ):
                self._tag_lazylibrarian_owner(
                    request_id, handler_result["external_id"]
                )
            handler_result.update({"request_id": request_id, "duplicate": False})
            return handler_result

    def resume_ebook_cascades(self, limit: int = 100) -> int:
        """Resume restart-safe searches without repeating a mutation."""

        resumed = 0
        batch_limit = max(1, min(int(limit), 1000))
        while True:
            batch = self.store.resumable_ebook_requests(limit=batch_limit)
            if not batch:
                return resumed
            request_ids = tuple(int(request["id"]) for request in batch)
            for request in batch:
                selected_candidate = None
                silent_retry = self._silent_unavailable_retry(int(request["id"]))
                confirmation = (
                    None
                    if silent_retry
                    else self.store.get_candidate_confirmation(int(request["id"]))
                )
                if confirmation is not None and confirmation.get("status") == "claimed":
                    selected = [
                        option
                        for option in confirmation.get("options", ())
                        if option.get("ordinal")
                        == confirmation.get("selected_ordinal")
                    ]
                    if len(selected) != 1 or not isinstance(
                        selected[0].get("candidate"), Mapping
                    ):
                        raise EbookCascadeStateError(
                            "Claimed ebook confirmation has no unique persisted selection"
                        )
                    selected_candidate = selected[0]["candidate"]
                try:
                    self._run_ebook_cascade(
                        request,
                        selected_candidate=selected_candidate,
                        allow_prompt=False,
                    )
                finally:
                    if silent_retry:
                        # Scheduler claims normally close their retry state in
                        # retry_due_unavailable_requests().  A restart can
                        # resume the same search through this path instead, so
                        # give terminal pre-mutation outcomes the identical
                        # deterministic requeue/expiry cleanup.
                        self.store.finish_unavailable_retry_attempt(
                            int(request["id"])
                        )
                resumed += 1
            remaining = self.store.resumable_ebook_requests(limit=batch_limit)
            if remaining and tuple(int(row["id"]) for row in remaining) == request_ids:
                raise EbookCascadeStateError(
                    "Ebook startup recovery made no durable progress"
                )

    def retry_due_unavailable_requests(
        self, *, now: Any | None = None, limit: int = 100
    ) -> int:
        """Claim due backlog rows and feed them through the same ebook cascade."""

        claimed = self.store.claim_due_unavailable_retries(now=now, limit=limit)
        attempted = 0
        for request in claimed:
            request_id = int(request["id"])
            try:
                self._run_ebook_cascade(
                    request,
                    allow_prompt=False,
                    retry_now=now,
                )
            except Exception as error:
                LOGGER.error(
                    "Silent unavailable retry %s deferred after %s",
                    request_id,
                    type(error).__name__,
                )
            finally:
                # Normal miss/handoff/completion paths close this state inside
                # their transaction.  This repairs only a pre-mutation
                # terminal path that returned without doing so.
                self.store.finish_unavailable_retry_attempt(
                    request_id, now=now
                )
            attempted += 1
        return attempted

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

        if not identifies_a_work(parsed.get("title"), parsed.get("author")):
            # A bare format token matches any release whose filename contains
            # it, so this must fail before any acquisition service sees it. It
            # is almost always a picker reply Huey read as a new request.
            message = (
                "That is a format, not a title. If you were answering a Huey "
                "prompt, use Discord's Reply action on the prompt itself and "
                "send one listed number. Otherwise send the full title."
            )
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
                return self._duplicate_result(record, message_id)
            value = result("needs_selection", message)
            value.update({"request_id": record["id"], "duplicate": False})
            plans = response_notifications(media_type, value, record)
            self.store.transition(
                record["id"],
                "needs_selection",
                message,
                event_type="unidentifying_request_rejected",
                error=message,
                notifications=tuple(
                    (plan.event_key, plan.route, plan.message) for plan in plans
                ),
            )
            return value

        configured_backends = tuple(
            getattr(self.services, "ebook_acquisition_backends", ()) or ()
        )
        cascade_intake = bool(
            media_type == "ebooks"
            and configured_backends
            and callable(getattr(self.services, "submit_ebook_backend", None))
        )
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
            ebook_backends=configured_backends if cascade_intake else None,
        )
        if not created:
            return self._duplicate_result(record, message_id)

        request_id = record["id"]
        request = dict(record)
        request.update(parsed)
        if cascade_intake:
            return self._run_ebook_cascade(request)

        intended_service = None
        if media_type == "movies-tv":
            # Naming the owning ARR before dispatch lets an ambiguous lookup
            # persist a candidate prompt, which requires a supported service.
            intended_service = {"movie": "radarr", "tv": "sonarr"}.get(
                str(parsed.get("kind") or "")
            )
        elif media_type == "audiobooks" and getattr(
            self.services, "abba_enabled", False
        ):
            intended_service = "abba"
        elif media_type == "ebooks":
            ebook_service = getattr(self.services, "ebook_service", None)
            if callable(ebook_service):
                intended_service = ebook_service()
            elif getattr(self.services, "shelfarr_enabled", False):
                intended_service = "shelfarr"
        self.store.transition(
            request_id,
            "processing",
            "Dispatching request to acquisition service",
            service=intended_service,
        )
        request["service"] = intended_service
        if intended_service in {"abba", "lazylibrarian"}:
            def mark_initial_dispatch(candidate_id: str) -> None:
                if intended_service == "abba":
                    owner = self.store.reserve_abba_dispatch(
                        request_id, candidate_id
                    )
                    if owner is None:
                        raise RuntimeError(
                            "Acquisition dispatch boundary could not be persisted"
                        )
                    if int(owner["id"]) != int(request_id):
                        raise CanonicalAcquisition(
                            int(owner["id"]),
                            candidate_id=str(candidate_id),
                            canonical_candidate_id=str(
                                owner.get("abba_candidate_id") or candidate_id
                            ),
                            info_hash="",
                            title=str(request.get("title") or "your request"),
                        )
                    return
                if not self.store.mark_request_dispatch_started(
                    request_id, intended_service, candidate_id=candidate_id
                ):
                    if intended_service == "lazylibrarian":
                        raise ServiceError(
                            "LazyLibrarian could not reserve this exact book for acquisition."
                        )
                    raise RuntimeError(
                        "Acquisition dispatch boundary could not be persisted"
                    )

            request["_before_dispatch"] = mark_initial_dispatch

        handler_result = self._service_failure_result(
            request_id,
            media_type,
            intended_service,
            lambda: self.dispatcher(request, self.services),
        )
        if "_canonical_request_id" in handler_result:
            owner = self._coalesce_canonical_audiobook(request_id, handler_result)
            return self._duplicate_result(owner, message_id)
        if media_type == "audiobooks":
            handler_result = self._generic_audiobook_result(handler_result)
        durable_notifications = response_notifications(
            media_type,
            {**handler_result, "request_id": request_id, "duplicate": False},
            request,
        )
        persisted = self._persist_handler_result(
            request_id,
            handler_result,
            request=request,
            notifications=durable_notifications,
        )
        if persisted is not None and int(persisted["id"]) != int(request_id):
            return self._duplicate_result(persisted, message_id)
        handler_result.update({"request_id": request_id, "duplicate": False})
        return handler_result

    def process_candidate_reply(self, delivery: Mapping[str, Any]) -> dict[str, Any]:
        """Claim and continue one reply to a persisted candidate prompt.

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

        if media_type == "ebooks" and self.store.get_ebook_cascade(request_id) is not None:
            handler_result = self._run_ebook_cascade(
                request,
                selected_candidate=selected_candidate,
            )
            if handler_result["status"] in {"queued", "completed"}:
                handler_result["message"] = (
                    "Selection accepted. " + str(handler_result["message"])
                )
            elif handler_result["status"] != "awaiting_selection":
                handler_result["message"] = (
                    "Selection accepted, but " + str(handler_result["message"])
                )
            handler_result.update(
                {
                    "selection_outcome": "claimed",
                    "media_type": media_type,
                }
            )
            return handler_result

        def mark_dispatch_started(candidate_id: str | None = None) -> None:
            service = str(request.get("service") or "")
            if service == "abba":
                owner = self.store.reserve_abba_dispatch(
                    request_id, str(candidate_id or "")
                )
                if owner is None:
                    raise RuntimeError(
                        "Candidate selection dispatch boundary could not be persisted"
                    )
                if int(owner["id"]) != int(request_id):
                    raise CanonicalAcquisition(
                        int(owner["id"]),
                        candidate_id=str(candidate_id or ""),
                        canonical_candidate_id=str(
                            owner.get("abba_candidate_id") or candidate_id or ""
                        ),
                        info_hash="",
                        title=str(request.get("title") or "your request"),
                    )
                return
            if not self.store.mark_candidate_dispatch_started(request_id):
                raise RuntimeError(
                    "Candidate selection dispatch boundary could not be persisted"
                )
            if service in {"abba", "lazylibrarian"} and not self.store.mark_request_dispatch_started(
                request_id,
                service,
                candidate_id=candidate_id,
            ):
                if service == "lazylibrarian":
                    raise ServiceError(
                        "LazyLibrarian could not reserve this exact book for acquisition."
                    )
                raise RuntimeError("Request dispatch boundary could not be persisted")

        continue_selection = getattr(self.services, "selection_selected", None)
        if not callable(continue_selection):
            continue_selection = self.services.book_selected

        handler_result = self._service_failure_result(
            request_id,
            media_type,
            str(request.get("service") or "") or None,
            lambda: continue_selection(
                request,
                selected_candidate,
                before_create=mark_dispatch_started,
            ),
        )
        if "_canonical_request_id" in handler_result:
            owner = self._coalesce_canonical_audiobook(request_id, handler_result)
            duplicate = self._duplicate_result(owner, str(delivery["message_id"]))
            duplicate.update(
                {
                    "selection_outcome": "claimed",
                    "media_type": media_type,
                }
            )
            return duplicate
        if media_type == "audiobooks":
            handler_result = self._generic_audiobook_result(handler_result)
        # A confirmed candidate cannot create a second confirmation session.
        # If fresh service revalidation can no longer identify it, the client
        # returns needs_selection and releases the target.
        if handler_result["status"] == "awaiting_selection":
            handler_result = result(
                "needs_selection",
                "That candidate is no longer available. Submit the title again to search anew.",
                service=str(request.get("service") or "") or None,
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
        persisted = self._persist_handler_result(
            request_id,
            handler_result,
            request=request,
            notifications=durable_notifications,
        )
        if persisted is not None and int(persisted["id"]) != request_id:
            duplicate = self._duplicate_result(
                persisted, str(delivery["message_id"])
            )
            duplicate.update(
                {
                    "selection_outcome": "claimed",
                    "media_type": media_type,
                }
            )
            return duplicate
        handler_result.update(
            {
                "request_id": request_id,
                "duplicate": False,
                "selection_outcome": "claimed",
                "media_type": media_type,
            }
        )
        return handler_result
