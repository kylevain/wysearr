#!/usr/bin/env python3
"""Discord entrypoint for Huey.

Importing this module has no filesystem, database, network, or Discord side
effects. Runtime startup happens exclusively in :func:`main`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .clients import CanonicalAcquisition, ServiceError, ServiceRejected
    from .config import ChannelConfig, load_channel_config
    from .database import EbookCascadeStateError, RequestStore
    from .notifications import (
        abba_state_notifications,
        lazylibrarian_state_notifications,
        response_notifications,
        service_correlation_attention_notification,
        shelfarr_attention_notification,
        shelfarr_correlation_attention_notification,
        shelfarr_state_notifications,
        terminal_notifications,
    )
    from .orchestrator import RequestProcessor
    from .results import sanitize_display_text
    from .services import ServiceRegistry
except ImportError:  # Direct execution from /app/scripts/huey/huey.py.
    from clients import CanonicalAcquisition, ServiceError, ServiceRejected
    from config import ChannelConfig, load_channel_config
    from database import EbookCascadeStateError, RequestStore
    from notifications import (
        abba_state_notifications,
        lazylibrarian_state_notifications,
        response_notifications,
        service_correlation_attention_notification,
        shelfarr_attention_notification,
        shelfarr_correlation_attention_notification,
        shelfarr_state_notifications,
        terminal_notifications,
    )
    from orchestrator import RequestProcessor
    from results import sanitize_display_text
    from services import ServiceRegistry


LOGGER = logging.getLogger("huey")
_SHELFARR_IMPORT_FAILURE = re.compile(
    r"(?:post[- ]?process|import|library|storage|write|writab|permission|filesystem|"
    r"disk space|no space|destination|finali[sz])",
    re.IGNORECASE,
)
_SELECTION_ORDINAL = re.compile(r"^[1-9][0-9]*$")


def write_ready_marker(path: str | Path) -> None:
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text("ready\n", encoding="utf-8")
    temporary.replace(marker)


def remove_ready_marker(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def format_reply(media_type: str, response: dict[str, Any]) -> str:
    symbols = {
        "queued": "✅",
        "awaiting_selection": "⚠️",
        "needs_selection": "⚠️",
        "failed": "❌",
        "complete": "✅",
        "completed": "✅",
    }
    symbol = (
        "⚠️"
        if str(response.get("external_status") or "").casefold()
        == "submission_uncertain"
        else symbols.get(response["status"], "ℹ️")
    )
    return (
        f"{symbol} Request #{response['request_id']}\n"
        f"Type: {media_type}\n"
        f"{response['message']}"
    )


def format_candidate_prompt(
    media_type: str,
    response: dict[str, Any],
    *,
    ttl_seconds: int,
) -> str:
    """Render a bounded prompt from already-sanitized persisted candidates."""

    candidates = response.get("selection_proposal")
    if not isinstance(candidates, (list, tuple)) or not candidates:
        raise ValueError("Candidate prompt requires at least one option")
    lines = []
    for ordinal, candidate in enumerate(candidates[:3], start=1):
        if not isinstance(candidate, dict):
            raise ValueError("Candidate prompt contains an invalid option")
        label = sanitize_display_text(candidate.get("label"), limit=300)
        if not label:
            raise ValueError("Candidate prompt contains an invalid label")
        lines.append(f"{ordinal}. {label}")
    minutes = max(1, (int(ttl_seconds) + 59) // 60)
    choice_kind = (
        "audiobook choice"
        if str(response.get("service") or "").casefold() == "abba"
        else "metadata choice"
    )
    return (
        f"⚠️ Request #{response['request_id']} needs one {choice_kind}\n"
        f"Type: {media_type}\n"
        + "\n".join(lines)
        + f"\nUse Discord's Reply action on this message, then send one number within {minutes} minute"
        + ("" if minutes == 1 else "s")
        + "."
    )


def _reply_reference_message_id(message: Any) -> str | None:
    reference = getattr(message, "reference", None)
    message_id = getattr(reference, "message_id", None)
    if isinstance(message_id, bool) or not str(message_id or "").isdigit():
        return None
    if int(message_id) <= 0:
        return None
    return str(message_id)


def _selection_ordinal(content: object) -> int:
    text = str(content or "")
    # Avoid Python's large-integer conversion limit while still treating an
    # all-digit but absurd reply as an out-of-range selection.
    if not _SELECTION_ORDINAL.fullmatch(text) or len(text) > 6:
        return 0
    return int(text)


async def _reply_targets_huey_candidate_prompt(
    client: Any, message: Any, prompt_message_id: str
) -> bool | None:
    """Identify a referenced Huey candidate prompt without trusting its reply.

    ``None`` means Discord could not resolve the reference. Callers use that
    distinction to fail closed for book-channel replies during the short
    interval between Discord accepting a prompt and SQLite binding its ID.
    """

    reference = getattr(message, "reference", None)
    referenced = getattr(reference, "resolved", None)
    if referenced is None:
        referenced = getattr(reference, "cached_message", None)
    if referenced is None:
        fetch_message = getattr(getattr(message, "channel", None), "fetch_message", None)
        if not callable(fetch_message):
            return None
        try:
            referenced = await fetch_message(int(prompt_message_id))
        except Exception:
            return None

    author = getattr(referenced, "author", None)
    client_user = getattr(client, "user", None)
    author_id = getattr(author, "id", None)
    client_user_id = getattr(client_user, "id", None)
    if author_id is None or client_user_id is None:
        return None
    if str(author_id) != str(client_user_id):
        return False
    content = getattr(referenced, "content", None)
    if not isinstance(content, str):
        return None
    return bool(
        content.startswith("⚠️ Request #")
        and (
            " needs one metadata choice\n" in content
            or " needs one audiobook choice\n" in content
        )
        and (
            "\nUse Discord's Reply action on this message, then send one number within "
            in content
            or "\nReply directly to this message with one number within " in content
        )
    )


def selection_correction(outcome: str, request_id: object = None) -> str:
    suffix = f" for request #{request_id}" if request_id is not None else ""
    if outcome == "expired":
        return (
            f"⚠️ That candidate-choice prompt{suffix} expired. "
            "Submit the title again to start a new search."
        )
    if outcome == "inactive":
        return (
            "⚠️ That Huey candidate-choice prompt is not active. "
            "Submit the title as a new standalone message to start another search."
        )
    if outcome == "unreferenced":
        return (
            "⚠️ Huey did not apply that number because it was not sent with "
            "Discord's Reply action on a candidate-choice prompt. Use Reply on the "
            "Huey prompt and send one listed whole number; if the prompt expired, "
            "submit the title again."
        )
    return (
        f"⚠️ That is not a valid choice{suffix}. "
        "The original requester must reply in this channel with one listed whole number."
    )


def format_completion_notification(request: dict[str, Any]) -> str:
    """Compatibility wrapper for the request-status terminal message."""

    plans = terminal_notifications(request)
    for plan in plans:
        if plan.route == "request-status":
            return plan.message
    raise ValueError("Request is not in a terminal notification state")


async def _discord_channel(client: Any, channel_id: str) -> Any | None:
    channel = client.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(int(channel_id))
    except Exception:
        return None


async def validate_discord_channels(
    client: Any, channel_config: ChannelConfig
) -> None:
    """Require every operational channel to be visible and writable by Huey."""

    channel_ids = set(channel_config.request_channels)
    channel_ids.update(channel_config.lifecycle_channels.values())
    for channel_id in sorted(channel_ids, key=int):
        channel = await _discord_channel(client, channel_id)
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError(f"Discord channel {channel_id} is unavailable")
        guild = getattr(channel, "guild", None)
        member = getattr(guild, "me", None)
        if member is None and guild is not None and getattr(client, "user", None):
            get_member = getattr(guild, "get_member", None)
            if callable(get_member):
                member = get_member(client.user.id)
        permissions_for = getattr(channel, "permissions_for", None)
        if member is None or not callable(permissions_for):
            raise RuntimeError(
                f"Discord permissions cannot be verified for channel {channel_id}"
            )
        permissions = permissions_for(member)
        required = ["view_channel", "send_messages"]
        if channel_id in channel_config.request_channels:
            required.append("read_message_history")
        missing = [name for name in required if not getattr(permissions, name, False)]
        if missing:
            raise RuntimeError(
                f"Discord channel {channel_id} lacks required bot permissions: "
                + ", ".join(missing)
            )


def _unavailable_retry_is_silent(store: Any, request_id: int) -> bool:
    checker = getattr(store, "unavailable_retry_is_silent", None)
    return bool(callable(checker) and checker(int(request_id)) is True)


async def reconcile_notifications(
    client: Any,
    channel_config: ChannelConfig,
    store: RequestStore,
) -> int:
    """Stage and deliver lifecycle events through their single configured routes."""

    lock = getattr(client, "_huey_notification_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        try:
            setattr(client, "_huey_notification_lock", lock)
        except Exception:  # pragma: no cover - discord.py clients are mutable
            pass

    async with lock:
        pending_terminal = await asyncio.to_thread(store.pending_notifications)
        terminal_ids: set[int] = set()
        for request in pending_terminal:
            request_id = int(request["id"])
            if await asyncio.to_thread(
                _unavailable_retry_is_silent, store, request_id
            ):
                continue
            terminal_ids.add(request_id)
            for plan in terminal_notifications(request):
                await asyncio.to_thread(
                    store.enqueue_notification,
                    request_id,
                    plan.event_key,
                    plan.route,
                    plan.message,
                )

        deliveries = await asyncio.to_thread(store.pending_notification_deliveries)
        delivered_count = 0
        for delivery in deliveries:
            route = str(delivery["route"])
            channel_id = channel_config.lifecycle_channels.get(route)
            if channel_id is None:
                LOGGER.error(
                    "No Discord lifecycle channel configured for route %s", route
                )
                continue
            channel = await _discord_channel(client, channel_id)
            if channel is None or not hasattr(channel, "send"):
                LOGGER.warning(
                    "Discord lifecycle route %s is unavailable for request %s",
                    route,
                    delivery["request_id"],
                )
                continue
            try:
                await channel.send(str(delivery["message"]))
            except Exception as error:
                LOGGER.warning(
                    "Could not deliver %s for request %s (%s)",
                    route,
                    delivery["request_id"],
                    type(error).__name__,
                )
                continue
            if await asyncio.to_thread(
                store.mark_notification_delivered, int(delivery["id"])
            ):
                delivered_count += 1

        for request_id in terminal_ids:
            await asyncio.to_thread(
                store.mark_notified_if_delivered,
                request_id,
                "All staged Discord lifecycle notifications delivered",
            )
        return delivered_count


def reconcile_arr_requests(store: RequestStore, services: Any) -> int:
    """Complete queued ARR requests only after an explicit imported-file signal."""

    completed_count = 0
    for request in store.queued_arr_requests():
        service = str(request.get("service") or "")
        try:
            client = services.arr(service)
            if not client.has_imported_media(request.get("external_id")):
                continue
        except ServiceError as error:
            # A 404 is deliberately treated like any other service failure: the
            # entity may have been removed, but that does not prove the request
            # completed or failed.
            LOGGER.warning(
                "ARR reconciliation deferred for request %s via %s (%s)",
                request["id"],
                service,
                type(error).__name__,
            )
            continue
        except Exception as error:
            # Isolate one malformed row or client failure so other queued
            # requests and terminal notifications still get reconciled.
            LOGGER.warning(
                "ARR reconciliation could not inspect request %s via %s (%s)",
                request["id"],
                service,
                type(error).__name__,
            )
            continue

        message = f"{service.title()} reports imported media for its queued entity"
        if store.mark_arr_completed(request["id"], message):
            completed_count += 1
            LOGGER.info(
                "ARR request %s completed after imported media was detected",
                request["id"],
            )
    return completed_count


def reconcile_ebook_cascades(store: RequestStore, services: Any) -> int:
    """Resume only search-safe ebook attempts preserved across a restart."""

    return RequestProcessor(store, services=services).resume_ebook_cascades()


def reconcile_unavailable_retries(processor: RequestProcessor) -> int:
    """Run one bounded batch of due, identity-preserving silent retries."""

    return processor.retry_due_unavailable_requests()


def _visible_notification_plans(
    store: RequestStore, request_id: int, plans: Any
) -> tuple[Any, ...]:
    """Drop routine Discord plans while a durable unavailable retry owns work."""

    if _unavailable_retry_is_silent(store, int(request_id)):
        return ()
    return tuple(plans)


def _enqueue_visible_notification(
    store: RequestStore, request_id: int, plan: Any
) -> None:
    """Enqueue one plan only when the request is outside the silent lifecycle."""

    if _unavailable_retry_is_silent(store, int(request_id)):
        return
    store.enqueue_notification(
        int(request_id), plan.event_key, plan.route, plan.message
    )


class _OneShotAsyncRecovery:
    """Share one cached startup result (including failure) across all waiters."""

    def __init__(self, callback: Any, *args: Any):
        self.callback = callback
        self.args = args
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.complete = False

    async def ensure(self) -> Any:
        if self.task is None:
            self.task = asyncio.create_task(
                asyncio.to_thread(self.callback, *self.args),
                name="huey-ebook-startup-recovery",
            )
        value = await asyncio.shield(self.task)
        self.complete = True
        self.ready.set()
        await self.ready.wait()
        return value


def reconcile_shelfarr_requests(store: RequestStore, services: Any) -> int:
    """Poll correlated Shelfarr requests and persist each lifecycle edge once."""

    def recovered_acceptance_plans(
        request: dict[str, Any], recovered: dict[str, Any]
    ) -> tuple[Any, ...]:
        response = {
            "request_id": int(request["id"]),
            "status": "queued",
            "message": (
                "Recovered the ebook acquisition and queued it safely."
                if str(request.get("media_type") or "").casefold() == "ebooks"
                else "Recovered a Shelfarr request after correlation completed."
            ),
            "duplicate": False,
            "service": "shelfarr",
            "external_id": str(recovered["id"]),
            "external_title": sanitize_display_text(
                recovered.get("book", {}).get("title"), limit=300
            )
            or request.get("title"),
            "external_status": str(recovered["status"]).casefold(),
        }
        return _visible_notification_plans(
            store,
            int(request["id"]),
            response_notifications(
                str(request.get("media_type") or "ebooks"), response, request
            ),
        )

    def persist_recovered_shelfarr(
        request: dict[str, Any],
        recovered: dict[str, Any],
        plans: tuple[Any, ...],
        *,
        message: str,
    ) -> None:
        """Promote new cascade rows atomically while retaining legacy recovery."""

        external_title = sanitize_display_text(
            recovered.get("book", {}).get("title"), limit=300
        ) or request.get("title")
        external_status = str(recovered["status"]).casefold()
        if external_status == "failed":
            plans = response_notifications(
                str(request.get("media_type") or "ebooks"),
                {
                    "request_id": int(request["id"]),
                    "status": "failed",
                    "message": (
                        "Huey recovered a definitive failed ebook acquisition."
                        if str(request.get("media_type") or "").casefold() == "ebooks"
                        else "Recovered a definitive failed Shelfarr acquisition."
                    ),
                    "duplicate": False,
                    "service": "shelfarr",
                    "external_id": str(recovered["id"]),
                    "external_title": external_title,
                    "external_status": "failed",
                },
                request,
            )
        plans = _visible_notification_plans(store, int(request["id"]), plans)
        notifications = tuple(
            (plan.event_key, plan.route, plan.message) for plan in plans
        )
        cascade = store.get_ebook_cascade(int(request["id"]))
        if cascade is not None and external_status == "failed":
            store.persist_ebook_result(
                int(request["id"]),
                "shelfarr",
                {
                    "status": "failed",
                    "message": message,
                    "external_id": str(recovered["id"]),
                    "external_title": external_title,
                    "external_status": "failed",
                },
                notifications=notifications,
            )
            return
        if cascade is not None:
            store.record_ebook_recovered_handoff(
                int(request["id"]),
                "shelfarr",
                str(recovered["id"]),
                external_title,
                external_status,
                message,
                backend_identity=str(
                    recovered.get("book", {}).get("work_id") or ""
                ),
                event_type="shelfarr_recovered",
                notifications=notifications,
            )
            return
        store.transition(
            int(request["id"]),
            "failed" if external_status == "failed" else "queued",
            message,
            event_type="shelfarr_recovered",
            service="shelfarr",
            external_id=str(recovered["id"]),
            external_title=external_title,
            external_status=external_status,
            error=message if external_status == "failed" else None,
            notifications=notifications,
        )

    def recovered_identity_matches(
        request: dict[str, Any], recovered: dict[str, Any], shelfarr_client: Any
    ) -> bool:
        expected = {"ebooks": "ebook", "audiobooks": "audiobook"}.get(
            str(request.get("media_type") or "").casefold()
        )
        book = recovered.get("book")
        actual = (
            str(book.get("book_type") or "").casefold()
            if isinstance(book, dict)
            else ""
        )
        if expected is None or actual != expected:
            return False

        try:
            cascade = store.get_ebook_cascade(int(request["id"]))
            if cascade is not None:
                ordinal = int(cascade["current_ordinal"])
                attempts = cascade.get("attempts", ())
                if not 0 <= ordinal < len(attempts):
                    return False
                backend_identity = attempts[ordinal].get("backend_identity")
                backend_identities = set(
                    attempts[ordinal].get("backend_identities", ())
                )
                return bool(
                    str(request.get("media_type") or "") == "ebooks"
                    and cascade["policy"][ordinal] == "shelfarr"
                    and isinstance(book, dict)
                    and book.get("content_kind") == "book"
                    and backend_identity is not None
                    and str(backend_identity) in backend_identities
                    and str(book.get("work_id") or "") in backend_identities
                )
            confirmation = store.get_candidate_confirmation(int(request["id"]))
            if confirmation is None:
                return True
            if confirmation.get("status") != "claimed":
                return False
            selected_ordinal = confirmation.get("selected_ordinal")
            selected_options = [
                option
                for option in confirmation.get("options", ())
                if isinstance(option, dict)
                and option.get("ordinal") == selected_ordinal
            ]
            if len(selected_options) != 1:
                return False
            selected_candidate = selected_options[0].get("candidate")
            matcher = getattr(
                shelfarr_client, "recovered_request_matches_candidate", None
            )
            if not callable(matcher):
                return False
            return matcher(
                recovered,
                selected_candidate,
                str(request.get("media_type") or ""),
            ) is True
        except Exception:
            return False

    def quarantine_format_mismatch(
        request: dict[str, Any], *, startup: bool
    ) -> None:
        plan = shelfarr_correlation_attention_notification(
            request, startup=startup, format_mismatch=True
        )
        _enqueue_visible_notification(store, int(request["id"]), plan)
        LOGGER.warning(
            "Shelfarr correlation has an unexpected book format for request %s; "
            "automatic retry is blocked",
            request["id"],
        )

    changed_count = 0
    for uncertain in store.uncertain_shelfarr_requests():
        try:
            shelfarr_client = services.shelfarr()
            recovered = shelfarr_client.recover_request(int(uncertain["id"]))
        except Exception as error:
            LOGGER.warning(
                "Shelfarr uncertain submission recovery deferred for request %s (%s)",
                uncertain["id"],
                type(error).__name__,
            )
            continue
        if recovered is None:
            plan = shelfarr_correlation_attention_notification(
                uncertain, startup=False
            )
            _enqueue_visible_notification(store, int(uncertain["id"]), plan)
            LOGGER.warning(
                "Shelfarr submission correlation remains uncertain for request %s; "
                "automatic retry is blocked",
                uncertain["id"],
            )
            continue
        if not recovered_identity_matches(uncertain, recovered, shelfarr_client):
            quarantine_format_mismatch(uncertain, startup=False)
            continue
        plans = recovered_acceptance_plans(uncertain, recovered)
        persist_recovered_shelfarr(
            uncertain,
            recovered,
            plans,
            message="Recovered ebook acquisition correlation after an uncertain submission",
        )
        changed_count += 1

    for interrupted in store.interrupted_shelfarr_requests():
        try:
            shelfarr_client = services.shelfarr()
            recovered = shelfarr_client.recover_request(int(interrupted["id"]))
        except Exception as error:
            LOGGER.warning(
                "Shelfarr crash-window recovery deferred for request %s (%s)",
                interrupted["id"],
                type(error).__name__,
            )
            continue
        if recovered is None:
            plan = shelfarr_correlation_attention_notification(
                interrupted, startup=True
            )
            _enqueue_visible_notification(store, int(interrupted["id"]), plan)
            LOGGER.warning(
                "Shelfarr crash-window correlation remains uncertain for request %s; "
                "automatic retry is blocked",
                interrupted["id"],
            )
            continue
        if not recovered_identity_matches(interrupted, recovered, shelfarr_client):
            quarantine_format_mismatch(interrupted, startup=True)
            continue
        plans = recovered_acceptance_plans(interrupted, recovered)
        persist_recovered_shelfarr(
            interrupted,
            recovered,
            plans,
            message="Recovered ebook acquisition correlation after interrupted Huey dispatch",
        )
        changed_count += 1

    # ``blocked`` is terminal for acquisition, but Shelfarr may publish the
    # exact already-correlated final import after Huey observed an earlier
    # failure.  Polling this retained remote ID is read-only: only an exact
    # ``completed`` response can repair and fulfil the owner, and every other
    # response remains silent and blocked.
    for request in store.claim_blocked_shelfarr_proof_checks():
        persisted_remote_id = str(request.get("external_id") or "")
        try:
            remote = services.shelfarr().get_request(persisted_remote_id)
        except ServiceError as error:
            LOGGER.warning(
                "Blocked Shelfarr final-proof check deferred for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue
        except Exception as error:
            LOGGER.warning(
                "Blocked Shelfarr final-proof check could not inspect request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue

        if (
            str(remote.get("id") or "") != persisted_remote_id
            or str(remote.get("status") or "").casefold() != "completed"
        ):
            continue
        try:
            changed = store.record_blocked_shelfarr_completion(
                int(request["id"]),
                remote.get("id"),
            )
        except (ValueError, EbookCascadeStateError) as error:
            LOGGER.warning(
                "Blocked Shelfarr final proof was rejected for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue
        if changed:
            changed_count += 1

    for request in store.queued_shelfarr_requests():
        try:
            remote = services.shelfarr().get_request(request.get("external_id"))
        except ServiceError as error:
            LOGGER.warning(
                "Shelfarr reconciliation deferred for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue
        except Exception as error:
            LOGGER.warning(
                "Shelfarr reconciliation could not inspect request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue

        external_status = str(remote.get("status") or "").casefold()
        issue = sanitize_display_text(remote.get("issue_description"), limit=500)
        attention_needed = remote.get("attention_needed") is True
        previous_status = str(request.get("external_status") or "").casefold()
        terminal_status: str | None = None
        event_type = f"shelfarr_{external_status}"
        message = f"Shelfarr reports {external_status}"
        error_message: str | None = None

        if external_status == "completed":
            terminal_status = "completed"
            event_type = "shelfarr_completed"
            message = "Shelfarr completed its final library import"
        elif external_status == "awaiting_purchase" or (
            attention_needed and external_status != "failed"
        ):
            cancellation_reason = external_status
            import_failure = (
                external_status == "processing" or previous_status == "processing"
            )
            try:
                remote = services.shelfarr().cancel_request(request.get("external_id"))
            except Exception as error:
                attention_message = issue or (
                    "Shelfarr retained a recoverable import failure for administrator review."
                    if import_failure
                    else "Shelfarr retained a request that requires administrator review."
                )
                changed = store.record_shelfarr_state(
                    int(request["id"]),
                    external_status,
                    attention_message,
                    event_type=(
                        "shelfarr_import_attention"
                        if import_failure
                        else "shelfarr_manual_attention"
                    ),
                    error=None,
                )
                alert_request = {
                    **dict(request),
                    "error": attention_message,
                    "external_status": external_status,
                }
                plan = shelfarr_attention_notification(
                    alert_request, import_failure=import_failure
                )
                _enqueue_visible_notification(store, int(request["id"]), plan)
                LOGGER.warning(
                    "Shelfarr retained an attention request %s for recovery (%s)",
                    request["id"],
                    type(error).__name__,
                )
                if changed:
                    changed_count += 1
                continue
            if str(remote.get("status") or "").casefold() != "failed":
                attention_message = issue or (
                    "Shelfarr retained a recoverable import failure for administrator review."
                    if import_failure
                    else "Shelfarr retained a request that requires administrator review."
                )
                changed = store.record_shelfarr_state(
                    int(request["id"]),
                    external_status,
                    attention_message,
                    event_type=(
                        "shelfarr_import_attention"
                        if import_failure
                        else "shelfarr_manual_attention"
                    ),
                    error=None,
                )
                plan = shelfarr_attention_notification(
                    {**dict(request), "error": attention_message},
                    import_failure=import_failure,
                )
                _enqueue_visible_notification(store, int(request["id"]), plan)
                LOGGER.warning(
                    "Shelfarr retained attention request %s without confirmed cancellation",
                    request["id"],
                )
                if changed:
                    changed_count += 1
                continue
            external_status = "failed"
            terminal_status = "failed"
            event_type = (
                "shelfarr_import_failed"
                if import_failure
                else "shelfarr_manual_intervention"
            )
            error_message = issue or (
                "Shelfarr found only a purchase/manual-upload option; "
                "no automatic acquisition source was available."
                if cancellation_reason == "awaiting_purchase"
                else "Shelfarr requires manual review after import processing failed."
                if import_failure
                else "Shelfarr required administrator review; Huey closed the "
                "automatic acquisition attempt."
            )
            message = error_message
        elif external_status == "failed":
            terminal_status = "failed"
            # Shelfarr's public request API does not expose a durable failure
            # phase. An actionable attention flag is therefore the reliable
            # signal for import-errors even when a 30-second poll missed the
            # transient processing state.
            import_failure = bool(
                previous_status == "processing"
                or attention_needed
                or (issue and _SHELFARR_IMPORT_FAILURE.search(issue))
            )
            event_type = (
                "shelfarr_import_failed" if import_failure else "shelfarr_failed"
            )
            error_message = issue or (
                "Shelfarr requires manual review after import processing failed."
                if import_failure
                else "Shelfarr could not complete automatic acquisition."
            )
            message = error_message
        try:
            changed = store.record_shelfarr_state(
                int(request["id"]),
                external_status,
                message,
                event_type=event_type,
                terminal_status=terminal_status,
                error=error_message,
            )
        except ValueError as error:
            LOGGER.warning(
                "Shelfarr returned an unsupported state for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue
        if changed:
            changed_count += 1

        if terminal_status is None:
            for plan in shelfarr_state_notifications(request, external_status):
                _enqueue_visible_notification(store, int(request["id"]), plan)
    return changed_count


def reconcile_lazylibrarian_requests(
    store: RequestStore, services: Any
) -> int:
    """Recover exact LL BookID/history correlations without repeating search."""

    def recovered_plans(
        request: dict[str, Any], recovered: dict[str, Any]
    ) -> tuple[Any, ...]:
        response = {
            "request_id": int(request["id"]),
            "status": "queued",
            "message": "Recovered the ebook acquisition and queued it safely.",
            "duplicate": False,
            "service": "lazylibrarian",
            "external_id": recovered["external_id"],
            "external_title": recovered["external_title"],
            "external_status": recovered.get("external_status") or "queued",
        }
        return _visible_notification_plans(
            store,
            int(request["id"]),
            response_notifications("ebooks", response, request),
        )

    def attention(
        request: dict[str, Any], *, startup: bool, identity_mismatch: bool = False
    ) -> None:
        plan = service_correlation_attention_notification(
            request,
            service="LazyLibrarian",
            startup=startup,
            identity_mismatch=identity_mismatch,
        )
        _enqueue_visible_notification(store, int(request["id"]), plan)

    def inspect(request: dict[str, Any], *, startup: bool) -> bool:
        try:
            remote = services.lazylibrarian().recover_submission(
                request.get("lazylibrarian_book_id"),
                request_id=int(request["id"]),
            )
        except ServiceRejected as error:
            attention(request, startup=startup, identity_mismatch=True)
            LOGGER.warning(
                "LazyLibrarian recovery rejected exact history for request %s (%s); "
                "automatic search retry is blocked",
                request["id"],
                type(error).__name__,
            )
            return False
        except ServiceError as error:
            LOGGER.warning(
                "LazyLibrarian recovery deferred for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            return False
        except Exception as error:
            LOGGER.warning(
                "LazyLibrarian recovery could not inspect request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            return False

        if not isinstance(remote, dict) or remote.get("state") not in {
            "unknown",
            "pending",
            "queued",
        }:
            attention(request, startup=startup, identity_mismatch=True)
            return False
        if remote["state"] != "queued":
            attention(request, startup=startup)
            LOGGER.warning(
                "LazyLibrarian exact history is not yet available for request %s; "
                "automatic search retry is blocked",
                request["id"],
            )
            return False

        plans = recovered_plans(request, remote)
        try:
            return store.record_lazylibrarian_download(
                int(request["id"]),
                str(remote["book_id"]),
                str(remote["external_id"]),
                str(remote["external_title"]),
                "Recovered exact LazyLibrarian BookID and qBittorrent DownloadID",
                external_status=str(remote.get("external_status") or "queued"),
                notifications=tuple(
                    (plan.event_key, plan.route, plan.message) for plan in plans
                ),
            )
        except Exception as error:
            attention(request, startup=startup, identity_mismatch=True)
            LOGGER.warning(
                "LazyLibrarian recovery could not bind request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            return False

    changed_count = 0
    for request in store.uncertain_lazylibrarian_requests():
        changed_count += inspect(request, startup=False)
    for request in store.interrupted_lazylibrarian_requests():
        changed_count += inspect(request, startup=True)

    queued_requests = store.queued_lazylibrarian_requests()
    if not queued_requests:
        return changed_count
    try:
        qbittorrent = services.qbittorrent()
    except Exception as error:
        LOGGER.warning(
            "LazyLibrarian qBittorrent status client is unavailable (%s)",
            type(error).__name__,
        )
        return changed_count

    terminal_failure_states = {"error", "missingfiles"}
    active_download_states = {"downloading", "forceddl"}
    downloaded_states = {
        "checkingup",
        "forcedup",
        "pausedup",
        "queuedup",
        "stalledup",
        "stoppedup",
        "uploading",
    }
    known_nonterminal_states = active_download_states | downloaded_states | {
        "allocating",
        "checkingdl",
        "checkingresumedata",
        "forcedmetadl",
        "metadl",
        "moving",
        "pauseddl",
        "queueddl",
        "stalleddl",
        "stoppeddl",
    }

    for request in queued_requests:
        expected_hash = str(request.get("external_id") or "").casefold()
        try:
            torrent = qbittorrent.find_torrent(expected_hash)
        except ServiceError as error:
            LOGGER.warning(
                "LazyLibrarian qBittorrent status polling is unavailable (%s)",
                type(error).__name__,
            )
            break
        except Exception as error:
            LOGGER.warning(
                "LazyLibrarian qBittorrent status deferred for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue
        if torrent is None:
            # Absence is not a terminal failure: BookBot may already have
            # completed retention, or qBittorrent may be transiently stale.
            continue
        if not isinstance(torrent, Mapping):
            attention(request, startup=False, identity_mismatch=True)
            continue
        observed_hash = str(torrent.get("hash") or "").casefold()
        category = str(torrent.get("category") or "")
        save_path = str(torrent.get("save_path") or "")
        if (
            observed_hash != expected_hash
            or category not in {"ebooks", "ebooks-imported"}
            or save_path != "/downloads/ebooks"
        ):
            attention(request, startup=False, identity_mismatch=True)
            LOGGER.warning(
                "LazyLibrarian qBittorrent routing changed for request %s",
                request["id"],
            )
            continue

        # The hash is durably bound to this Huey request, but qBittorrent
        # routing is mutable.  Restore BookBot correlation only after the
        # current torrent still proves the exact ebook category and path.
        try:
            qbittorrent.add_tags(
                expected_hash, f"huey-{int(request['id'])}"
            )
        except ServiceError as error:
            LOGGER.warning(
                "LazyLibrarian correlation tag deferred for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue
        except Exception as error:
            LOGGER.warning(
                "LazyLibrarian correlation tag could not be restored for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue

        qbit_state = str(torrent.get("state") or "").strip().casefold()
        try:
            download_complete = (
                float(torrent.get("progress", 0.0)) >= 1.0
                and int(torrent.get("amount_left", 1)) == 0
            )
        except (TypeError, ValueError):
            download_complete = False

        if category == "ebooks-imported":
            phase = "processing"
        elif qbit_state in terminal_failure_states:
            detail = (
                "qBittorrent reports a terminal ebook download failure; "
                "BookBot cannot import this payload."
            )
            response = {
                "request_id": int(request["id"]),
                "status": "failed",
                "message": detail,
                "duplicate": False,
                "service": "lazylibrarian",
                "external_id": expected_hash,
                "external_title": request.get("external_title"),
                "external_status": "failed",
            }
            plans = _visible_notification_plans(
                store,
                int(request["id"]),
                response_notifications("ebooks", response, request),
            )
            if store.record_lazylibrarian_state(
                int(request["id"]),
                expected_hash,
                "failed",
                detail,
                terminal=True,
                error=detail,
                notifications=tuple(
                    (plan.event_key, plan.route, plan.message) for plan in plans
                ),
            ):
                changed_count += 1
            continue
        elif download_complete or qbit_state in downloaded_states:
            phase = "processing"
        elif qbit_state in active_download_states:
            phase = "downloading"
        elif qbit_state in known_nonterminal_states:
            phase = "queued"
        else:
            # Unknown/new qB states are never guessed into failure.
            continue

        current = {**dict(request), "external_status": phase}
        plans = _visible_notification_plans(
            store,
            int(request["id"]),
            lazylibrarian_state_notifications(current, phase),
        )
        if store.record_lazylibrarian_state(
            int(request["id"]),
            expected_hash,
            phase,
            f"LazyLibrarian qBittorrent handoff reports {phase}",
            notifications=tuple(
                (plan.event_key, plan.route, plan.message) for plan in plans
            ),
        ):
            changed_count += 1
    return changed_count


def reconcile_abba_requests(store: RequestStore, services: Any) -> int:
    """Recover ABBA grabs and observe qBittorrent progress without polling spam."""

    recovery_groups = (
        (store.uncertain_abba_requests(), False),
        (store.interrupted_abba_requests(), True),
    )
    if (
        not any(requests for requests, _startup in recovery_groups)
        and not store.queued_abba_requests()
    ):
        return 0
    try:
        abba = services.abba()
    except Exception as error:
        LOGGER.warning(
            "ABBA reconciliation client is unavailable (%s)", type(error).__name__
        )
        return 0

    def identity_matches(request: dict[str, Any], job: dict[str, Any]) -> bool:
        expected_candidate_id = str(request.get("abba_candidate_id") or "")
        if (
            not expected_candidate_id
            or str(job.get("candidate_id") or "") != expected_candidate_id
        ):
            return False
        confirmation = store.get_candidate_confirmation(int(request["id"]))
        if confirmation is None:
            return True
        if confirmation.get("status") != "claimed":
            return False
        selected = [
            option
            for option in confirmation.get("options", ())
            if isinstance(option, dict)
            and option.get("ordinal") == confirmation.get("selected_ordinal")
        ]
        if len(selected) != 1:
            return False
        matcher = getattr(abba, "recovered_request_matches_candidate", None)
        return bool(
            callable(matcher)
            and matcher(job, selected[0].get("candidate")) is True
        )

    def quarantine(request: dict[str, Any], *, startup: bool) -> None:
        plan = service_correlation_attention_notification(
            request,
            service="ABBA",
            startup=startup,
            identity_mismatch=True,
        )
        store.enqueue_notification(
            int(request["id"]), plan.event_key, plan.route, plan.message
        )
        LOGGER.warning(
            "ABBA correlation has an unexpected candidate for request %s",
            request["id"],
        )

    def coalesce_alias(
        request: dict[str, Any],
        *,
        owner_request_id: int,
        candidate_id: str,
        canonical_candidate_id: str,
        info_hash: str,
    ) -> bool:
        try:
            store.coalesce_abba_request(
                int(request["id"]),
                int(owner_request_id),
                candidate_id=candidate_id,
                canonical_candidate_id=canonical_candidate_id,
                info_hash=info_hash,
                reason="ABBA restart correlation",
            )
            return True
        except Exception as error:
            LOGGER.warning(
                "ABBA canonical correlation could not be persisted for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            return False

    def persist_recovered(
        request: dict[str, Any], remote: dict[str, Any], *, resumed: bool
    ) -> None:
        """Atomically attach the exact qBit hash, or persist pre-hash failure."""

        raw_status = str(remote.get("status") or "").casefold()
        external_status = str(remote.get("external_status") or raw_status).casefold()
        failed = raw_status == "failed"
        external_id = remote.get("info_hash", remote.get("external_id"))
        if external_id is not None:
            external_id = str(external_id).lower()
        external_title = (
            remote.get("title")
            or remote.get("external_title")
            or request.get("title")
        )
        if failed:
            operator_detail = sanitize_display_text(
                remote.get("error") or remote.get("message"), limit=500
            ) or "The persisted audiobook acquisition failed during recovery."
            response = {
                "request_id": int(request["id"]),
                "status": "failed",
                "message": (
                    "The audiobook acquisition could not be completed during "
                    "recovery. An administrator can review the saved workflow."
                ),
                "duplicate": False,
                "service": "abba",
                "external_id": external_id,
                "external_title": external_title,
                "external_status": "failed",
            }
            message = "Recovered a terminal ABBA failure after interrupted submission"
            event_type = "abba_failed"
            error = operator_detail
        else:
            response = {
                "request_id": int(request["id"]),
                "status": "queued",
                "message": (
                    "Safely resumed the persisted audiobook acquisition."
                    if resumed
                    else "Recovered the correlated audiobook acquisition."
                ),
                "duplicate": False,
                "service": "abba",
                "external_id": external_id,
                "external_title": external_title,
                "external_status": external_status,
            }
            message = "Recovered ABBA correlation after an interrupted submission"
            event_type = "abba_recovered"
            error = None
        plans = response_notifications("audiobooks", response, request)
        store.transition(
            int(request["id"]),
            str(response["status"]),
            message,
            event_type=event_type,
            service="abba",
            external_id=external_id,
            external_title=str(external_title) if external_title else None,
            external_status=str(response["external_status"]),
            error=error,
            notifications=tuple(
                (plan.event_key, plan.route, plan.message) for plan in plans
            ),
        )

    def reject_unrecoverable_candidate(request: dict[str, Any]) -> None:
        message = (
            "The audiobook workflow could not revalidate the persisted result. "
            "Submit the title again to search anew."
        )
        response = {
            "request_id": int(request["id"]),
            "status": "needs_selection",
            "message": message,
            "duplicate": False,
            "service": "abba",
        }
        plans = response_notifications("audiobooks", response, request)
        store.transition(
            int(request["id"]),
            "needs_selection",
            message,
            event_type="abba_recovery_rejected",
            service="abba",
            error=message,
            notifications=tuple(
                (plan.event_key, plan.route, plan.message) for plan in plans
            ),
        )

    changed_count = 0
    for requests, startup in recovery_groups:
        for request in requests:
            try:
                job = abba.recover_request(int(request["id"]))
            except Exception as error:
                LOGGER.warning(
                    "ABBA correlation recovery deferred for request %s (%s)",
                    request["id"],
                    type(error).__name__,
                )
                plan = service_correlation_attention_notification(
                    request, service="ABBA", startup=startup
                )
                store.enqueue_notification(
                    int(request["id"]), plan.event_key, plan.route, plan.message
                )
                continue
            if job is None:
                candidate_id = str(request.get("abba_candidate_id") or "")
                resume = getattr(abba, "resume_grab", None)
                if not candidate_id or not callable(resume):
                    quarantine(request, startup=startup)
                    continue
                try:
                    response = resume(int(request["id"]), candidate_id)
                except CanonicalAcquisition as canonical:
                    if coalesce_alias(
                        request,
                        owner_request_id=canonical.owner_request_id,
                        candidate_id=canonical.candidate_id,
                        canonical_candidate_id=canonical.canonical_candidate_id,
                        info_hash=canonical.info_hash,
                    ):
                        changed_count += 1
                    else:
                        quarantine(request, startup=startup)
                    continue
                except ServiceRejected:
                    reject_unrecoverable_candidate(request)
                    changed_count += 1
                    continue
                except Exception as error:
                    LOGGER.warning(
                        "ABBA exact-candidate recovery deferred for request %s (%s)",
                        request["id"],
                        type(error).__name__,
                    )
                    plan = service_correlation_attention_notification(
                        request, service="ABBA", startup=startup
                    )
                    store.enqueue_notification(
                        int(request["id"]), plan.event_key, plan.route, plan.message
                    )
                    continue
                persist_recovered(request, dict(response), resumed=True)
                changed_count += 1
                continue
            if str(job.get("status") or "").casefold() == "duplicate":
                if coalesce_alias(
                    request,
                    owner_request_id=int(job.get("canonical_request_id") or 0),
                    candidate_id=str(job.get("candidate_id") or ""),
                    canonical_candidate_id=str(
                        job.get("canonical_candidate_id") or ""
                    ),
                    info_hash=str(job.get("info_hash") or ""),
                ):
                    changed_count += 1
                else:
                    quarantine(request, startup=startup)
                continue
            if not identity_matches(request, job):
                quarantine(request, startup=startup)
                continue
            persist_recovered(request, dict(job), resumed=False)
            changed_count += 1

    for request in store.queued_abba_requests():
        try:
            job = abba.get_request(int(request["id"]))
        except Exception as error:
            LOGGER.warning(
                "ABBA status reconciliation deferred for request %s (%s)",
                request["id"],
                type(error).__name__,
            )
            continue
        if job is None:
            LOGGER.warning("ABBA has not exposed status for request %s", request["id"])
            continue
        external_status = str(job["status"]).casefold()
        observed_hash = str(job.get("info_hash") or "").casefold()
        expected_hash = str(request.get("external_id") or "").casefold()
        hash_mismatch = observed_hash != expected_hash
        if hash_mismatch or not identity_matches(request, job):
            quarantine(request, startup=False)
            continue

        if external_status == "failed":
            detail = sanitize_display_text(job.get("error"), limit=500) or (
                "The audiobook workflow could not complete the download."
            )
            if store.record_abba_state(
                int(request["id"]),
                "failed",
                detail,
                terminal=True,
                error=detail,
            ):
                changed_count += 1
            continue

        current = {**dict(request), "external_status": external_status}
        plans = abba_state_notifications(current, external_status)
        if store.record_abba_state(
            int(request["id"]),
            external_status,
            f"ABBA reports {external_status}",
            notifications=tuple(
                (plan.event_key, plan.route, plan.message) for plan in plans
            ),
        ):
            changed_count += 1
    return changed_count


async def notification_loop(
    client: Any,
    channel_config: ChannelConfig,
    store: RequestStore,
    services: Any,
    interval_seconds: float = 30,
) -> None:
    """Reconcile acquisition state and notifications for the Discord client lifetime."""

    while not client.is_closed():
        try:
            await asyncio.to_thread(store.expire_candidate_confirmations)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error(
                "Candidate confirmation expiry failed (%s)", type(error).__name__
            )
        try:
            await asyncio.to_thread(reconcile_arr_requests, store, services)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error("ARR completion reconciliation failed (%s)", type(error).__name__)
        try:
            await reconcile_notifications(client, channel_config, store)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error("Completion reconciliation failed (%s)", type(error).__name__)
        await asyncio.sleep(max(1.0, interval_seconds))


async def shelfarr_reconciliation_loop(
    client: Any,
    store: RequestStore,
    services: Any,
    interval_seconds: float = 30,
) -> None:
    """Poll book lifecycle independently so an outage cannot starve ARR/Discord."""

    while not client.is_closed():
        try:
            await asyncio.to_thread(reconcile_shelfarr_requests, store, services)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error(
                "Shelfarr lifecycle reconciliation failed (%s)",
                type(error).__name__,
            )
        await asyncio.sleep(max(1.0, interval_seconds))


async def lazylibrarian_reconciliation_loop(
    client: Any,
    store: RequestStore,
    services: Any,
    interval_seconds: float = 30,
) -> None:
    """Recover LL history independently without repeating provider searches."""

    while not client.is_closed():
        try:
            await asyncio.to_thread(
                reconcile_lazylibrarian_requests, store, services
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error(
                "LazyLibrarian correlation reconciliation failed (%s)",
                type(error).__name__,
            )
        await asyncio.sleep(max(1.0, interval_seconds))


async def abba_reconciliation_loop(
    client: Any,
    store: RequestStore,
    services: Any,
    interval_seconds: float = 30,
) -> None:
    """Poll ABBA independently so its outage cannot affect other workflows."""

    while not client.is_closed():
        try:
            await asyncio.to_thread(reconcile_abba_requests, store, services)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error("ABBA lifecycle reconciliation failed (%s)", type(error).__name__)
        await asyncio.sleep(max(1.0, interval_seconds))


async def unavailable_retry_loop(
    client: Any,
    processor: RequestProcessor,
    interval_seconds: float = 30,
) -> None:
    """Run durable unavailable retries independently of lifecycle polling."""

    while not client.is_closed():
        try:
            await asyncio.to_thread(reconcile_unavailable_retries, processor)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error(
                "Unavailable ebook retry reconciliation failed (%s)",
                type(error).__name__,
            )
        await asyncio.sleep(max(1.0, interval_seconds))


def build_client(
    channel_config: ChannelConfig,
    processor: RequestProcessor,
    ready_path: str | Path,
    reconcile_seconds: float = 30,
):
    """Create and register the Discord client without connecting it."""

    try:
        import discord
    except ImportError as error:  # pragma: no cover - installed in production image
        raise RuntimeError("discord.py is required to run Huey") from error

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    reconcile_task: asyncio.Task | None = None
    shelfarr_task: asyncio.Task | None = None
    lazylibrarian_task: asyncio.Task | None = None
    abba_task: asyncio.Task | None = None
    unavailable_retry_task: asyncio.Task | None = None
    ebook_recovery = _OneShotAsyncRecovery(
        reconcile_ebook_cascades,
        processor.store,
        processor.services,
    )

    async def ensure_ebook_recovery() -> None:
        """Serialize startup recovery ahead of every Discord intake path."""

        # A cancelled Discord callback cannot cancel the shared one-shot, and
        # a failed task remains cached so another waiter cannot repeat searches.
        await ebook_recovery.ensure()

    @client.event
    async def on_ready():
        nonlocal reconcile_task, shelfarr_task, lazylibrarian_task, abba_task
        nonlocal unavailable_retry_task
        try:
            await validate_discord_channels(client, channel_config)
        except RuntimeError as error:
            remove_ready_marker(ready_path)
            LOGGER.error("Huey Discord channel validation failed: %s", error)
            await client.close()
            return
        if not ebook_recovery.complete:
            try:
                await ensure_ebook_recovery()
            except Exception as error:
                remove_ready_marker(ready_path)
                LOGGER.error(
                    "Huey ebook cascade startup recovery failed (%s)",
                    type(error).__name__,
                )
                await client.close()
                return
        write_ready_marker(ready_path)
        LOGGER.info("Huey is ready as %s; watching %d channel(s)", client.user, len(channel_config.request_channels))
        if reconcile_task is None or reconcile_task.done():
            reconcile_task = asyncio.create_task(
                notification_loop(
                    client,
                    channel_config,
                    processor.store,
                    processor.services,
                    reconcile_seconds,
                ),
                name="huey-completion-reconciliation",
            )
        if shelfarr_task is None or shelfarr_task.done():
            shelfarr_task = asyncio.create_task(
                shelfarr_reconciliation_loop(
                    client,
                    processor.store,
                    processor.services,
                    reconcile_seconds,
                ),
                name="huey-shelfarr-reconciliation",
            )
        if lazylibrarian_task is None or lazylibrarian_task.done():
            lazylibrarian_task = asyncio.create_task(
                lazylibrarian_reconciliation_loop(
                    client,
                    processor.store,
                    processor.services,
                    reconcile_seconds,
                ),
                name="huey-lazylibrarian-reconciliation",
            )
        if abba_task is None or abba_task.done():
            abba_task = asyncio.create_task(
                abba_reconciliation_loop(
                    client,
                    processor.store,
                    processor.services,
                    reconcile_seconds,
                ),
                name="huey-abba-reconciliation",
            )
        if unavailable_retry_task is None or unavailable_retry_task.done():
            unavailable_retry_task = asyncio.create_task(
                unavailable_retry_loop(client, processor, reconcile_seconds),
                name="huey-unavailable-retry-reconciliation",
            )

    async def deliver_response(message: Any, media_type: str, response: dict[str, Any]) -> None:
        """Reply once, then stage only the response's lifecycle events."""

        reply = format_reply(media_type, response)
        try:
            await message.reply(reply)
        except Exception as error:
            LOGGER.warning(
                "Could not reply to request %s (%s)",
                response["request_id"],
                type(error).__name__,
            )
            await asyncio.to_thread(
                processor.store.add_event,
                response["request_id"],
                "notification_failed",
                "Could not reply to the original Discord request",
            )

        try:
            request = await asyncio.to_thread(
                processor.store.get_request, int(response["request_id"])
            )
            if request is None:
                raise LookupError("persisted request is unavailable")
            for plan in response_notifications(media_type, response, request):
                await asyncio.to_thread(
                    processor.store.enqueue_notification,
                    int(response["request_id"]),
                    plan.event_key,
                    plan.route,
                    plan.message,
                )
            await reconcile_notifications(client, channel_config, processor.store)
        except Exception as error:
            LOGGER.warning(
                "Could not reconcile lifecycle notifications for request %s (%s)",
                response["request_id"],
                type(error).__name__,
            )
            await asyncio.to_thread(
                processor.store.add_event,
                response["request_id"],
                "notification_failed",
                "Could not reconcile Discord lifecycle notifications",
            )

    @client.event
    async def on_message(message):
        if getattr(message.author, "bot", False) or getattr(message, "webhook_id", None) is not None:
            return
        media_type = channel_config.request_channels.get(str(message.channel.id))
        if media_type is None:
            return
        try:
            await ensure_ebook_recovery()
        except Exception as error:
            LOGGER.error(
                "Huey ebook cascade startup recovery blocked Discord intake (%s)",
                type(error).__name__,
            )
            return

        delivery = {
            "discord_user_id": str(message.author.id),
            "discord_username": str(message.author),
            "channel_id": str(message.channel.id),
            "message_id": str(message.id),
            "media_type": media_type,
            "content": message.content,
        }

        prompt_message_id = _reply_reference_message_id(message)
        if prompt_message_id is not None:
            selection_delivery = {
                **delivery,
                "prompt_message_id": prompt_message_id,
                "ordinal": _selection_ordinal(message.content),
            }
            try:
                selection_response = await asyncio.to_thread(
                    processor.process_candidate_reply, selection_delivery
                )
            except Exception as error:
                LOGGER.error(
                    "Discord candidate reply could not be persisted (%s)",
                    type(error).__name__,
                )
                try:
                    await message.reply(
                        "❌ Huey could not save this selection. Do not retry it yet; "
                        "an administrator should check Huey and the acquisition service."
                    )
                except Exception as reply_error:
                    LOGGER.warning(
                        "Could not send candidate persistence failure reply (%s)",
                        type(reply_error).__name__,
                    )
                return

            selection_outcome = str(
                selection_response.get("selection_outcome") or "not_found"
            )
            if selection_outcome == "duplicate":
                return
            if selection_outcome in {"invalid", "expired"}:
                try:
                    await message.reply(
                        selection_correction(
                            selection_outcome, selection_response.get("request_id")
                        )
                    )
                except Exception as error:
                    LOGGER.warning(
                        "Could not send candidate correction (%s)", type(error).__name__
                    )
                return
            if selection_outcome == "claimed":
                selected_media_type = str(
                    selection_response.get("media_type") or media_type
                )
                await deliver_response(message, selected_media_type, selection_response)
                return
            if selection_outcome == "not_found":
                targets_huey_prompt = await _reply_targets_huey_candidate_prompt(
                    client, message, prompt_message_id
                )
                if targets_huey_prompt is True or (
                    targets_huey_prompt is None
                    and media_type in {"ebooks", "audiobooks"}
                ):
                    try:
                        await message.reply(selection_correction("inactive"))
                    except Exception as error:
                        LOGGER.warning(
                            "Could not send inactive candidate correction (%s)",
                            type(error).__name__,
                        )
                    return
            # A reply to any message other than a live persisted Huey prompt is
            # ordinary request-channel input and follows the unchanged parser.

        # A bare integer is a selection token, not a safe standalone book
        # request. This also closes the failover edge where Discord redelivers a
        # recorded choice without its message reference: the durable reply-ID
        # lookup above normally coalesces it, and this guard prevents a race from
        # ever dispatching a separate request whose title is merely "1".
        if (
            prompt_message_id is None
            and media_type in {"ebooks", "audiobooks"}
            and _selection_ordinal(message.content) in {1, 2, 3}
        ):
            try:
                await message.reply(selection_correction("unreferenced"))
            except Exception as error:
                LOGGER.warning(
                    "Could not send standalone selection correction (%s)",
                    type(error).__name__,
                )
            return

        try:
            response = await asyncio.to_thread(processor.process, delivery)
        except Exception as error:
            LOGGER.error("Discord delivery could not be persisted (%s)", type(error).__name__)
            try:
                await message.reply(
                    "❌ Huey could not save this request. Please try again; "
                    "an administrator should check the Huey logs."
                )
            except Exception as reply_error:
                LOGGER.warning(
                    "Could not send persistence failure reply (%s)",
                    type(reply_error).__name__,
                )
            return

        if (
            response.get("status") == "awaiting_selection"
            and response.get("selection_proposal")
            and not response.get("duplicate")
        ):
            try:
                prompt = await message.reply(
                    format_candidate_prompt(
                        media_type,
                        response,
                        ttl_seconds=processor.selection_ttl_seconds,
                    )
                )
                prompt_message_id = getattr(prompt, "id", None)
                if (
                    isinstance(prompt_message_id, bool)
                    or not str(prompt_message_id or "").isdigit()
                    or int(prompt_message_id) <= 0
                ):
                    raise RuntimeError("Discord did not confirm the candidate prompt ID")
                bound = await asyncio.to_thread(
                    processor.store.bind_candidate_prompt,
                    int(response["request_id"]),
                    str(prompt_message_id),
                )
                if not bound:
                    raise RuntimeError("Candidate prompt could not be bound")
            except Exception as error:
                LOGGER.warning(
                    "Could not deliver candidate prompt for request %s (%s)",
                    response["request_id"],
                    type(error).__name__,
                )
                try:
                    await asyncio.to_thread(
                        processor.store.fail_candidate_prompt,
                        int(response["request_id"]),
                        "Could not deliver the Discord candidate prompt",
                    )
                except Exception as persistence_error:
                    LOGGER.error(
                        "Could not release failed candidate prompt for request %s (%s)",
                        response["request_id"],
                        type(persistence_error).__name__,
                    )
            return

        await deliver_response(message, media_type, response)

    return client


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HUEY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ready_path = Path(os.environ.get("HUEY_READY_FILE", "/state/ready"))
    remove_ready_marker(ready_path)

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN missing")

    config_path = Path(os.environ.get("HUEY_CONFIG_PATH", "/app/config/channels.yml"))
    database_path = Path(os.environ.get("HUEY_DB_PATH", "/state/huey.db"))

    channel_config = load_channel_config(config_path)
    store = RequestStore(database_path)
    store.initialize()
    try:
        reconcile_seconds = float(os.environ.get("HUEY_RECONCILE_SECONDS", "30"))
    except ValueError as error:
        raise SystemExit("HUEY_RECONCILE_SECONDS must be numeric") from error
    if reconcile_seconds < 1:
        raise SystemExit("HUEY_RECONCILE_SECONDS must be at least 1")
    try:
        selection_ttl_seconds = int(
            os.environ.get("HUEY_SELECTION_TTL_SECONDS", "900")
        )
    except ValueError as error:
        raise SystemExit("HUEY_SELECTION_TTL_SECONDS must be an integer") from error
    if not 1 <= selection_ttl_seconds <= 86_400:
        raise SystemExit(
            "HUEY_SELECTION_TTL_SECONDS must be between 1 and 86400"
        )
    processor = RequestProcessor(
        store,
        services=ServiceRegistry(),
        selection_ttl_seconds=selection_ttl_seconds,
    )
    client = build_client(channel_config, processor, ready_path, reconcile_seconds)
    try:
        client.run(token)
    finally:
        remove_ready_marker(ready_path)


if __name__ == "__main__":
    main()
