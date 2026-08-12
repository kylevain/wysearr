#!/usr/bin/env python3
"""Discord entrypoint for Huey.

Importing this module has no filesystem, database, network, or Discord side
effects. Runtime startup happens exclusively in :func:`main`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

try:
    from .clients import ServiceError
    from .config import ChannelConfig, load_channel_config
    from .database import RequestStore
    from .notifications import response_notifications, terminal_notifications
    from .orchestrator import RequestProcessor
    from .services import ServiceRegistry
except ImportError:  # Direct execution from /app/scripts/huey/huey.py.
    from clients import ServiceError
    from config import ChannelConfig, load_channel_config
    from database import RequestStore
    from notifications import response_notifications, terminal_notifications
    from orchestrator import RequestProcessor
    from services import ServiceRegistry


LOGGER = logging.getLogger("huey")


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
        "needs_selection": "⚠️",
        "failed": "❌",
        "complete": "✅",
        "completed": "✅",
    }
    return (
        f"{symbols.get(response['status'], 'ℹ️')} Request #{response['request_id']}\n"
        f"Type: {media_type}\n"
        f"{response['message']}"
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
        terminal_ids = {int(request["id"]) for request in pending_terminal}
        for request in pending_terminal:
            for plan in terminal_notifications(request):
                await asyncio.to_thread(
                    store.enqueue_notification,
                    int(request["id"]),
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

    @client.event
    async def on_ready():
        nonlocal reconcile_task
        try:
            await validate_discord_channels(client, channel_config)
        except RuntimeError as error:
            remove_ready_marker(ready_path)
            LOGGER.error("Huey Discord channel validation failed: %s", error)
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

    @client.event
    async def on_message(message):
        if getattr(message.author, "bot", False) or getattr(message, "webhook_id", None) is not None:
            return
        media_type = channel_config.request_channels.get(str(message.channel.id))
        if media_type is None:
            return

        delivery = {
            "discord_user_id": str(message.author.id),
            "discord_username": str(message.author),
            "channel_id": str(message.channel.id),
            "message_id": str(message.id),
            "media_type": media_type,
            "content": message.content,
        }
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
    processor = RequestProcessor(store, services=ServiceRegistry())
    try:
        reconcile_seconds = float(os.environ.get("HUEY_RECONCILE_SECONDS", "30"))
    except ValueError as error:
        raise SystemExit("HUEY_RECONCILE_SECONDS must be numeric") from error
    if reconcile_seconds < 1:
        raise SystemExit("HUEY_RECONCILE_SECONDS must be at least 1")
    client = build_client(channel_config, processor, ready_path, reconcile_seconds)
    try:
        client.run(token)
    finally:
        remove_ready_marker(ready_path)


if __name__ == "__main__":
    main()
