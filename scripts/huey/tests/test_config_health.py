import importlib
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from config import ChannelConfigError, validate_channel_config
from healthcheck import is_ready
from database import RequestStore
from huey import reconcile_notifications


REQUESTS = {
    "movies-tv": 1,
    "ebooks": 2,
    "audiobooks": 3,
    "manga-comics": 4,
    "roms": 5,
    "sheet-music": 6,
}


class ConfigTests(unittest.TestCase):
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


class FakeClient:
    def __init__(self, channels):
        self.channels = channels

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id not in self.channels:
            raise LookupError("missing")
        return self.channels[channel_id]


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
        self.assertIn("now available", original_message.replies[0])
        saved = self.store.get_request(self.request["id"])
        self.assertIsNotNone(saved["notified_at"])

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


if __name__ == "__main__":
    unittest.main()
