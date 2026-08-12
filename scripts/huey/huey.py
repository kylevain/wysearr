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
from pathlib import Path
from typing import Any

try:
    from .config import ChannelConfig, load_channel_config
    from .database import RequestStore
    from .orchestrator import RequestProcessor
    from .services import ServiceRegistry
except ImportError:  # Direct execution from /app/scripts/huey/huey.py.
    from config import ChannelConfig, load_channel_config
    from database import RequestStore
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
    title = request.get("external_title") or request.get("title") or "your request"
    if request["status"] in {"complete", "completed"}:
        return f"✅ Request #{request['id']} complete: {title} is now available in the library."
    detail = request.get("error") or ""
    if not detail or re.search(
        r"(?:https?://|magnet:|api[_-]?key|token|password|secret)", detail, re.IGNORECASE
    ):
        detail = "The import or acquisition failed. An administrator should review Huey and BookBot logs."
    return f"❌ Request #{request['id']} failed: {title}. {detail}"


async def _discord_channel(client: Any, channel_id: str) -> Any | None:
    channel = client.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(int(channel_id))
    except Exception:
        return None


async def reconcile_notifications(
    client: Any,
    channel_config: ChannelConfig,
    store: RequestStore,
) -> int:
    """Deliver completion/failure notifications and persist a one-time marker."""

    pending = await asyncio.to_thread(store.pending_notifications)
    delivered_count = 0
    for request in pending:
        notification = format_completion_notification(request)
        delivered_to: list[str] = []

        original_channel = await _discord_channel(client, str(request["channel_id"]))
        if original_channel is not None and hasattr(original_channel, "fetch_message"):
            try:
                original_message = await original_channel.fetch_message(int(request["message_id"]))
                await original_message.reply(notification)
                delivered_to.append("original request")
            except Exception as error:
                LOGGER.warning(
                    "Could not reply with completion for request %s (%s)",
                    request["id"],
                    type(error).__name__,
                )

        status_channel_id = channel_config.request_status_channel
        if status_channel_id:
            status_channel = await _discord_channel(client, status_channel_id)
            if status_channel is not None and hasattr(status_channel, "send"):
                try:
                    await status_channel.send(notification)
                    delivered_to.append("request-status channel")
                except Exception as error:
                    LOGGER.warning(
                        "Could not post completion for request %s (%s)",
                        request["id"],
                        type(error).__name__,
                    )

        if delivered_to:
            marked = await asyncio.to_thread(
                store.mark_notified,
                request["id"],
                "Completion notification delivered to " + " and ".join(delivered_to),
            )
            if marked:
                delivered_count += 1
    return delivered_count


async def notification_loop(
    client: Any,
    channel_config: ChannelConfig,
    store: RequestStore,
    interval_seconds: float = 30,
) -> None:
    """Reconcile BookBot terminal updates for the lifetime of the Discord client."""

    while not client.is_closed():
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
        write_ready_marker(ready_path)
        LOGGER.info("Huey is ready as %s; watching %d channel(s)", client.user, len(channel_config.request_channels))
        if reconcile_task is None or reconcile_task.done():
            reconcile_task = asyncio.create_task(
                notification_loop(client, channel_config, processor.store, reconcile_seconds),
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
        delivered_to: list[str] = []
        try:
            await message.reply(reply)
            delivered_to.append("original request")
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

        status_channel_id = channel_config.request_status_channel
        if status_channel_id and str(message.channel.id) != status_channel_id:
            try:
                status_channel = await _discord_channel(client, status_channel_id)
                if status_channel is None:
                    raise LookupError("request-status channel is unavailable")
                await status_channel.send(reply)
                delivered_to.append("request-status channel")
            except Exception as error:
                LOGGER.warning(
                    "Could not update the request-status channel for request %s (%s)",
                    response["request_id"],
                    type(error).__name__,
                )
                await asyncio.to_thread(
                    processor.store.add_event,
                    response["request_id"],
                    "notification_failed",
                    "Could not update the Discord request-status channel",
                )

        if response["status"] in {"complete", "completed", "failed"} and delivered_to:
            await asyncio.to_thread(
                processor.store.mark_notified,
                response["request_id"],
                "Terminal request result delivered to " + " and ".join(delivered_to),
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
