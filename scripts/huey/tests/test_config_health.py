import importlib
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


HUEY_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_CHANNELS = HUEY_ROOT.parents[1] / "docs" / "huey-channels.yml"
sys.path.insert(0, str(HUEY_ROOT))

from config import ChannelConfigError, validate_channel_config
from clients import ServiceError
from handlers import HANDLERS
from healthcheck import is_ready
from database import RequestStore
from services import ServiceRegistry
from huey import (
    format_completion_notification,
    notification_loop,
    reconcile_arr_requests,
    reconcile_notifications,
    validate_discord_channels,
)


REQUESTS = {
    "movies-tv": 1,
    "ebooks": 2,
    "audiobooks": 3,
    "manga-comics": 4,
    "roms": 5,
    "sheet-music": 6,
}


def read_production_channel_map() -> dict[str, dict[str, int]]:
    """Read the deliberately simple two-level channel inventory without PyYAML."""

    document: dict[str, dict[str, int]] = {}
    section: dict[str, int] | None = None
    for raw_line in PRODUCTION_CHANNELS.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():
            self_contained = line.removesuffix(":")
            section = document.setdefault(self_contained, {})
            continue
        if section is None:
            raise AssertionError("channel entry precedes its section")
        name, value = line.strip().split(":", 1)
        section[name] = int(value.strip())
    return document


class ConfigTests(unittest.TestCase):
    def test_production_map_has_only_six_real_request_channels(self):
        raw = read_production_channel_map()
        config = validate_channel_config(raw)
        configured_types = set(config.request_channels.values())

        self.assertEqual(configured_types, set(REQUESTS))
        self.assertEqual(len(config.request_channels), 6)
        self.assertTrue(configured_types <= set(HANDLERS))
        self.assertTrue(
            {"music", "adult", "spicy", "whisparr"}.isdisjoint(configured_types)
        )

        self.assertEqual(
            config.request_status_channel,
            str(raw["activity"]["request-status"]),
        )
        self.assertEqual(
            set(vars(config)), {"request_channels", "request_status_channel"}
        )
        reserved_ids = {
            str(channel_id)
            for group, channels in raw.items()
            if group in {"activity", "system"}
            for name, channel_id in channels.items()
            if name != "request-status"
        }
        parsed_ids = set(config.request_channels) | {config.request_status_channel}
        self.assertTrue(reserved_ids.isdisjoint(parsed_ids))

    def test_valid_config_inverts_request_mapping_and_reads_status(self):
        config = validate_channel_config(
            {"requests": REQUESTS, "activity": {"request-status": 20}}
        )
        self.assertEqual(config.request_channels["1"], "movies-tv")
        self.assertEqual(config.request_status_channel, "20")

    def test_missing_required_channel_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "Missing"):
            validate_channel_config({"requests": {"ebooks": 2}})

    def test_duplicate_or_invalid_channel_id_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "more than one"):
            validate_channel_config(
                {"requests": {**REQUESTS, "ebooks": REQUESTS["movies-tv"]}}
            )
        with self.assertRaises(ChannelConfigError):
            validate_channel_config({"requests": {**REQUESTS, "ebooks": "not-an-id"}})

    def test_unknown_media_type_is_rejected(self):
        with self.assertRaisesRegex(ChannelConfigError, "Unsupported"):
            validate_channel_config({"requests": {**REQUESTS, "unknown": 8}})

    def test_status_channel_cannot_also_be_a_request_channel(self):
        with self.assertRaisesRegex(ChannelConfigError, "separate"):
            validate_channel_config(
                {"requests": REQUESTS, "activity": {"request-status": 2}}
            )


class HealthAndImportTests(unittest.TestCase):
    def test_health_marker_requires_exact_ready_content(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            self.assertFalse(is_ready(marker))
            marker.write_text("starting\n", encoding="utf-8")
            self.assertFalse(is_ready(marker))
            marker.write_text("ready\n", encoding="utf-8")
            self.assertTrue(is_ready(marker))

    def test_huey_module_is_import_safe(self):
        module = importlib.import_module("huey")
        self.assertTrue(callable(module.main))


class ServiceRegistryTests(unittest.TestCase):
    def test_direct_client_uses_configured_prowlarr_search_budget(self):
        services = ServiceRegistry(
            {
                "PROWLARR_URL": "http://prowlarr:9696",
                "PROWLARR_API_KEY": "key",
                "PROWLARR_SEARCH_CONNECT_TIMEOUT_SECONDS": "4.5",
                "PROWLARR_SEARCH_READ_TIMEOUT_SECONDS": "95",
                "PROWLARR_SEARCH_ATTEMPTS": "2",
                "PROWLARR_SEARCH_RETRY_DELAY_SECONDS": "0.5",
                "QBITTORRENT_USERNAME": "user",
                "QBITTORRENT_PASSWORD": "password",
            }
        )

        direct = services.direct()

        self.assertEqual(direct.prowlarr.search_timeout, (4.5, 95.0))
        self.assertEqual(direct.prowlarr.search_attempts, 2)
        self.assertEqual(direct.prowlarr.search_retry_delay, 0.5)

    def test_invalid_prowlarr_search_budget_fails_before_network_use(self):
        services = ServiceRegistry(
            {
                "PROWLARR_API_KEY": "key",
                "PROWLARR_SEARCH_READ_TIMEOUT_SECONDS": "0",
                "QBITTORRENT_USERNAME": "user",
                "QBITTORRENT_PASSWORD": "password",
            }
        )
        with self.assertRaisesRegex(ValueError, "must be positive"):
            services.direct()


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply(self, message):
        self.replies.append(message)


class FakeChannel:
    def __init__(self, message=None):
        self.message = message
        self.sent = []

    async def fetch_message(self, _message_id):
        if self.message is None:
            raise LookupError("missing")
        return self.message

    async def send(self, message):
        self.sent.append(message)


class FakePermissions:
    def __init__(self, *, view=True, send=True, history=True):
        self.view_channel = view
        self.send_messages = send
        self.read_message_history = history


class PermissionChannel(FakeChannel):
    def __init__(self, permissions=None):
        super().__init__()
        self.guild = type("Guild", (), {"me": object()})()
        self.permissions = permissions or FakePermissions()

    def permissions_for(self, _member):
        return self.permissions


class FakeClient:
    def __init__(self, channels):
        self.channels = channels

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id not in self.channels:
            raise LookupError("missing")
        return self.channels[channel_id]


class FakeArrCompletionClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.entity_ids = []

    def has_imported_media(self, entity_id):
        self.entity_ids.append(entity_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeServices:
    def __init__(self, clients):
        self.clients = clients
        self.requested = []

    def arr(self, service):
        self.requested.append(service)
        return self.clients[service]


class CompletionReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()
        self.request, _ = self.store.create_request(
            discord_user_id="1",
            discord_username="reader",
            channel_id="2",
            message_id="100",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author=None,
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def reconcile(self, client, config):
        async def direct_call(function, *args, **kwargs):
            return function(*args, **kwargs)

        # Keep this a pure unit test even on constrained hosts where Python's
        # default executor cannot shut down worker threads cleanly.
        with patch("huey.asyncio.to_thread", new=direct_call):
            return await reconcile_notifications(client, config, self.store)

    async def test_completion_replies_and_posts_status_once(self):
        self.store.transition(self.request["id"], "complete", "BookBot imported media")
        original_message = FakeMessage()
        original_channel = FakeChannel(original_message)
        status_channel = FakeChannel()
        client = FakeClient({2: original_channel, 20: status_channel})
        config = validate_channel_config(
            {"requests": REQUESTS, "activity": {"request-status": 20}}
        )

        self.assertEqual(await self.reconcile(client, config), 1)
        self.assertEqual(await self.reconcile(client, config), 0)
        self.assertEqual(len(original_message.replies), 1)
        self.assertEqual(len(status_channel.sent), 1)
        self.assertIn("safely imported to its DAS library path", original_message.replies[0])
        saved = self.store.get_request(self.request["id"])
        self.assertIsNotNone(saved["notified_at"])

    def test_terminal_notification_sanitizes_persisted_external_title(self):
        notification = format_completion_notification(
            {
                "id": 8,
                "status": "completed",
                "service": "radarr",
                "external_title": "Dune @everyone https://invalid/?token=secret",
                "title": "Dune",
            }
        )
        self.assertIn("Dune", notification)
        self.assertNotIn("@everyone", notification)
        self.assertNotIn("https://", notification)
        self.assertNotIn("token", notification)

    async def test_status_channel_is_fallback_when_original_is_missing(self):
        self.store.transition(self.request["id"], "failed", "Import failed", error="Retry limit reached")
        status_channel = FakeChannel()
        client = FakeClient({20: status_channel})
        config = validate_channel_config(
            {"requests": REQUESTS, "activity": {"request-status": 20}}
        )
        self.assertEqual(await self.reconcile(client, config), 1)
        self.assertEqual(len(status_channel.sent), 1)
        self.assertIn("Retry limit reached", status_channel.sent[0])

    async def test_no_delivery_route_leaves_notification_pending(self):
        self.store.transition(self.request["id"], "complete", "BookBot imported media")
        config = validate_channel_config({"requests": REQUESTS})
        self.assertEqual(await self.reconcile(FakeClient({}), config), 0)
        self.assertEqual(len(self.store.pending_notifications()), 1)

    async def test_arr_completion_transitions_once_then_notifier_delivers(self):
        self.store.transition(
            self.request["id"],
            "queued",
            "Queued in Radarr",
            service="radarr",
            external_id="44",
            external_title="Arrival",
        )
        arr_client = FakeArrCompletionClient(True)
        services = FakeServices({"radarr": arr_client})

        self.assertEqual(reconcile_arr_requests(self.store, services), 1)
        self.assertEqual(reconcile_arr_requests(self.store, services), 0)
        self.assertEqual(arr_client.entity_ids, ["44"])
        events = self.store.events_for(self.request["id"])
        self.assertEqual(
            [event["event_type"] for event in events].count("arr_completed"), 1
        )

        original_message = FakeMessage()
        client = FakeClient({2: FakeChannel(original_message)})
        config = validate_channel_config({"requests": REQUESTS})
        self.assertEqual(await self.reconcile(client, config), 1)
        self.assertIn("imported to its DAS library path by Radarr", original_message.replies[0])

    async def test_arr_without_files_remains_queued(self):
        self.store.transition(
            self.request["id"],
            "queued",
            "Queued in Sonarr",
            service="sonarr",
            external_id="33",
        )
        arr_client = FakeArrCompletionClient(False)
        services = FakeServices({"sonarr": arr_client})

        self.assertEqual(reconcile_arr_requests(self.store, services), 0)
        self.assertEqual(self.store.get_request(self.request["id"])["status"], "queued")
        self.assertEqual(arr_client.entity_ids, ["33"])

    async def test_arr_probe_error_or_missing_entity_remains_queued(self):
        self.store.transition(
            self.request["id"],
            "queued",
            "Queued in Lidarr",
            service="lidarr",
            external_id="55",
        )
        services = FakeServices(
            {"lidarr": FakeArrCompletionClient(ServiceError("lidarr rejected the request"))}
        )

        with self.assertLogs("huey", level="WARNING") as logs:
            self.assertEqual(reconcile_arr_requests(self.store, services), 0)
        self.assertEqual(self.store.get_request(self.request["id"])["status"], "queued")
        self.assertTrue(any("deferred" in line for line in logs.output))

    async def test_notification_loop_probes_arr_before_terminal_notifications(self):
        client = Mock()
        client.is_closed.return_value = False
        config = validate_channel_config({"requests": REQUESTS})
        order = []

        def reconcile_arr(_store, _services):
            order.append("arr")

        async def reconcile_discord(_client, _config, _store):
            order.append("notifications")

        async def stop_after_cycle(_seconds):
            raise asyncio.CancelledError

        with (
            patch("huey.reconcile_arr_requests", side_effect=reconcile_arr),
            patch("huey.reconcile_notifications", side_effect=reconcile_discord),
            patch("huey.asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))),
            patch("huey.asyncio.sleep", side_effect=stop_after_cycle),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await notification_loop(client, config, self.store, object(), 30)
        self.assertEqual(order, ["arr", "notifications"])


class DiscordChannelValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_request_and_status_channels_are_verified(self):
        config = validate_channel_config(
            {"requests": REQUESTS, "activity": {"request-status": 20}}
        )
        client = FakeClient(
            {channel_id: PermissionChannel() for channel_id in (*REQUESTS.values(), 20)}
        )
        await validate_discord_channels(client, config)

    async def test_missing_or_unwritable_channel_is_rejected(self):
        config = validate_channel_config(
            {"requests": REQUESTS, "activity": {"request-status": 20}}
        )
        channels = {
            channel_id: PermissionChannel()
            for channel_id in (*REQUESTS.values(), 20)
        }
        channels[2] = PermissionChannel(FakePermissions(send=False))
        with self.assertRaisesRegex(RuntimeError, "send_messages"):
            await validate_discord_channels(FakeClient(channels), config)
        channels[2] = PermissionChannel()
        del channels[3]
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await validate_discord_channels(FakeClient(channels), config)


if __name__ == "__main__":
    unittest.main()
