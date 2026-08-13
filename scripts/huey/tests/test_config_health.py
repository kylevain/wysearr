import importlib
import asyncio
import sys
import tempfile
import types
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
from orchestrator import RequestProcessor
from results import result
from services import ServiceRegistry
from huey import (
    build_client,
    notification_loop,
    shelfarr_reconciliation_loop,
    reconcile_arr_requests,
    reconcile_notifications,
    reconcile_shelfarr_requests,
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

ACTIVITY = {
    "download-queue": 20,
    "request-status": 21,
    "recent-additions": 22,
}

SYSTEM = {
    "import-errors": 30,
    "system-health": 31,
}


def channel_mapping() -> dict[str, dict[str, int]]:
    return {
        "requests": dict(REQUESTS),
        "activity": dict(ACTIVITY),
        "system": dict(SYSTEM),
    }


def candidate_proposal():
    return tuple(
        {
            "fingerprint": character * 64,
            "label": label,
            "work_id": work_id,
            "source_work_ids": (work_id,),
            "title": "Dune",
            "author": author,
            "year": year,
            "content_kind": "book",
            "media_type": "ebooks",
            "book_type": "ebook",
        }
        for character, label, work_id, author, year in (
            (
                "a",
                "Dune by Frank Herbert (1965), Ebook, Open Library",
                "openlibrary:OL893415W",
                "Frank Herbert",
                1965,
            ),
            (
                "b",
                "Dune by Brian Herbert (2005), Ebook, Open Library",
                "openlibrary:OL2W",
                "Brian Herbert",
                2005,
            ),
        )
    )


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
            config.lifecycle_channels,
            {
                route: str(raw[section][route])
                for section, routes in (
                    (
                        "activity",
                        ("download-queue", "request-status", "recent-additions"),
                    ),
                    ("system", ("import-errors", "system-health")),
                )
                for route in routes
            },
        )
        lifecycle_ids = {
            str(channel_id)
            for group, channels in raw.items()
            if group in {"activity", "system"}
            for name, channel_id in channels.items()
        }
        self.assertEqual(len(lifecycle_ids), 6)
        self.assertTrue(lifecycle_ids.isdisjoint(config.request_channels))

    def test_valid_config_inverts_request_mapping_and_reads_status(self):
        config = validate_channel_config(channel_mapping())
        self.assertEqual(config.request_channels["1"], "movies-tv")
        self.assertEqual(
            config.lifecycle_channels,
            {
                "download-queue": "20",
                "request-status": "21",
                "recent-additions": "22",
                "import-errors": "30",
                "system-health": "31",
            },
        )
        for route, channel_id in config.lifecycle_channels.items():
            self.assertEqual(config.channel_for(route), channel_id)

    def test_all_lifecycle_routes_are_required(self):
        for section, route in (
            ("activity", "download-queue"),
            ("activity", "request-status"),
            ("activity", "recent-additions"),
            ("system", "import-errors"),
            ("system", "system-health"),
        ):
            with self.subTest(route=route):
                raw = channel_mapping()
                del raw[section][route]
                with self.assertRaisesRegex(ChannelConfigError, route):
                    validate_channel_config(raw)

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

    def test_lifecycle_channels_are_unique_from_every_other_channel(self):
        for section, route, duplicate_id in (
            ("activity", "request-status", REQUESTS["ebooks"]),
            ("activity", "download-queue", ACTIVITY["request-status"]),
            ("system", "import-errors", ACTIVITY["recent-additions"]),
            ("system", "system-health", SYSTEM["import-errors"]),
        ):
            with self.subTest(route=route):
                raw = channel_mapping()
                raw[section][route] = duplicate_id
                with self.assertRaisesRegex(ChannelConfigError, "more than one|separate|unique"):
                    validate_channel_config(raw)


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

    def test_shelfarr_disabled_preserves_existing_direct_book_path(self):
        services = ServiceRegistry({"SHELFARR_ENABLED": "false"})
        direct = Mock()
        direct.submit.return_value = result(
            "queued", "Queued in qBittorrent", service="qbittorrent"
        )
        services._clients["direct"] = direct
        request = {
            "id": 42,
            "media_type": "ebooks",
            "title": "Dune",
            "author": "Frank Herbert",
        }

        response = services.book(request)

        self.assertEqual(response["service"], "qbittorrent")
        direct.submit.assert_called_once_with(
            "ebooks", "Dune", "Frank Herbert", 42
        )
        self.assertNotIn("shelfarr", services._clients)

    def test_shelfarr_enabled_routes_only_book_requests(self):
        services = ServiceRegistry(
            {"SHELFARR_ENABLED": "true", "SHELFARR_API_TOKEN": "shf_secret"}
        )
        shelfarr = Mock()
        shelfarr.submit.return_value = result(
            "queued", "Queued in Shelfarr", service="shelfarr", external_id="73"
        )
        radarr = Mock()
        radarr.submit.return_value = result(
            "queued", "Queued in Radarr", service="radarr", external_id="44"
        )
        services._clients.update({"shelfarr": shelfarr, "radarr": radarr})

        book = services.book(
            {
                "id": 42,
                "media_type": "audiobooks",
                "title": "Dune",
                "author": "Frank Herbert",
                "discord_user_id": "1001",
                "channel_id": "2002",
            }
        )
        movie = HANDLERS["movies-tv"](
            {"kind": "movie", "title": "Arrival"}, services
        )

        self.assertEqual(book["service"], "shelfarr")
        self.assertEqual(movie["service"], "radarr")
        shelfarr.submit.assert_called_once_with(
            "audiobooks",
            "Dune",
            "Frank Herbert",
            42,
            discord_user_id="1001",
            discord_channel_id="2002",
        )
        radarr.submit.assert_called_once_with("Arrival")

    def test_invalid_shelfarr_feature_flag_fails_closed(self):
        for value in ("sometimes", "1", "yes", "on", "TRUE"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "SHELFARR_ENABLED"):
                    ServiceRegistry({"SHELFARR_ENABLED": value})

    def test_selected_book_uses_shelfarr_without_direct_fallback(self):
        services = ServiceRegistry(
            {"SHELFARR_ENABLED": "true", "SHELFARR_API_TOKEN": "shf_secret"}
        )
        shelfarr = Mock()
        shelfarr.submit_selected.return_value = result(
            "queued", "Queued in Shelfarr", service="shelfarr", external_id="73"
        )
        services._clients["shelfarr"] = shelfarr
        request = {
            "id": 42,
            "media_type": "ebooks",
            "title": "Dune",
            "author": "Frank Herbert",
            "discord_user_id": "1001",
            "channel_id": "2002",
        }
        candidate = {"fingerprint": "a" * 64}
        before_create = Mock()

        response = services.book_selected(
            request, candidate, before_create=before_create
        )

        self.assertEqual(response["service"], "shelfarr")
        shelfarr.submit_selected.assert_called_once_with(
            "ebooks",
            "Dune",
            "Frank Herbert",
            42,
            selected_candidate=candidate,
            discord_user_id="1001",
            discord_channel_id="2002",
            before_create=before_create,
        )

    def test_selected_book_fails_when_shelfarr_ownership_is_disabled(self):
        services = ServiceRegistry({"SHELFARR_ENABLED": "false"})
        services._clients["direct"] = Mock()

        with self.assertRaisesRegex(RuntimeError, "disabled"):
            services.book_selected(
                {
                    "id": 42,
                    "media_type": "ebooks",
                    "title": "Dune",
                    "author": None,
                },
                {"fingerprint": "a" * 64},
            )
        services._clients["direct"].submit.assert_not_called()


class FakeMessage:
    def __init__(self, *, reply_message_id=900):
        self.replies = []
        self.reply_message_id = reply_message_id

    async def reply(self, message):
        self.replies.append(message)
        return types.SimpleNamespace(id=self.reply_message_id)


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


class FailOnceChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def send(self, message):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("transient Discord failure")
        await super().send(message)


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


class FakeDiscordClient(FakeClient):
    def __init__(self, *, intents):
        super().__init__({})
        self.intents = intents

    def event(self, callback):
        setattr(self, callback.__name__, callback)
        return callback

    def is_closed(self):
        return False


class FakeIntents:
    message_content = False

    @classmethod
    def default(cls):
        return cls()


class FakeIncomingMessage(FakeMessage):
    def __init__(
        self,
        *,
        message_id,
        channel,
        content,
        author_id=99,
        reference_id=None,
        reply_message_id=900,
    ):
        super().__init__(reply_message_id=reply_message_id)
        self.id = message_id
        self.channel = channel
        self.content = content
        self.webhook_id = None
        self.reference = (
            types.SimpleNamespace(message_id=reference_id)
            if reference_id is not None
            else None
        )
        self.author = type(
            "Author",
            (),
            {
                "id": author_id,
                "bot": False,
                "__str__": lambda _self: "reader",
            },
        )()


class FailReplyIncomingMessage(FakeIncomingMessage):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reply_attempts = 0

    async def reply(self, _message):
        self.reply_attempts += 1
        raise RuntimeError("Discord reply failed")


class FakeArrCompletionClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.entity_ids = []

    def has_imported_media(self, entity_id):
        self.entity_ids.append(entity_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeShelfarrCompletionClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.request_ids = []
        self.cancelled_ids = []

    def get_request(self, request_id):
        self.request_ids.append(request_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def cancel_request(self, request_id):
        self.cancelled_ids.append(request_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {**self.outcome, "status": "failed", "attention_needed": False}

    def recover_request(self, request_id):
        self.request_ids.append(request_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    @staticmethod
    def recovered_request_matches_candidate(remote, selected_candidate, media_type):
        expected_type = {"ebooks": "ebook", "audiobooks": "audiobook"}.get(
            media_type
        )
        book = remote.get("book") if isinstance(remote, dict) else None
        aliases = (
            selected_candidate.get("source_work_ids")
            if isinstance(selected_candidate, dict)
            else None
        )
        return bool(
            isinstance(book, dict)
            and isinstance(aliases, (list, tuple))
            and book.get("book_type") == expected_type
            and book.get("content_kind") == "book"
            and book.get("work_id") in aliases
        )


class FakeServices:
    def __init__(self, clients):
        self.clients = clients
        self.requested = []

    def arr(self, service):
        self.requested.append(service)
        return self.clients[service]

    def shelfarr(self):
        self.requested.append("shelfarr")
        return self.clients["shelfarr"]


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

    def queue_shelfarr(self, external_status="pending"):
        self.store.transition(
            self.request["id"],
            "queued",
            "Queued in Shelfarr",
            service="shelfarr",
            external_id="73",
            external_status=external_status,
            external_title="Dune",
        )

    def queue_uncertain_shelfarr(self):
        self.store.transition(
            self.request["id"],
            "queued",
            "Shelfarr submission is awaiting correlation recovery",
            event_type="shelfarr_submission_uncertain",
            service="shelfarr",
            external_status="submission_uncertain",
        )

    def claim_candidate_for_dispatch(self):
        self.store.transition(
            self.request["id"],
            "processing",
            "Searching Shelfarr",
            service="shelfarr",
        )
        self.store.create_candidate_confirmation(
            self.request["id"], candidate_proposal()
        )
        self.store.bind_candidate_prompt(self.request["id"], "9001")
        claimed = self.store.claim_candidate_selection(
            prompt_message_id="9001",
            reply_message_id="9002",
            discord_user_id="1",
            channel_id="2",
            ordinal=1,
        )
        self.assertEqual(claimed["outcome"], "claimed")
        self.assertTrue(
            self.store.mark_candidate_dispatch_started(self.request["id"])
        )

    async def test_bookbot_completion_routes_status_and_addition_once_without_reply(self):
        self.store.transition(
            self.request["id"],
            "complete",
            "BookBot imported media",
            event_type="completed",
            service="bookbot",
            external_title="Dune EPUB",
        )
        original_message = FakeMessage()
        original_channel = FakeChannel(original_message)
        status_channel = FakeChannel()
        addition_channel = FakeChannel()
        client = FakeClient(
            {2: original_channel, 21: status_channel, 22: addition_channel}
        )
        config = validate_channel_config(channel_mapping())

        await self.reconcile(client, config)
        await self.reconcile(client, config)
        self.assertEqual(original_message.replies, [])
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(addition_channel.sent), 1)
        self.assertNotEqual(status_channel.sent[0], addition_channel.sent[0])
        combined = " ".join(status_channel.sent + addition_channel.sent)
        self.assertIn("DAS library path", combined)
        self.assertNotIn("Plex", combined)
        saved = self.store.get_request(self.request["id"])
        self.assertIsNotNone(saved["notified_at"])

    async def test_bookbot_failure_routes_status_and_import_error_without_reply(self):
        self.store.transition(
            self.request["id"],
            "failed",
            "Import failed",
            event_type="failed",
            service="bookbot",
            external_title="Dune EPUB",
            error="Retry limit reached",
        )
        original_message = FakeMessage()
        status_channel = FakeChannel()
        error_channel = FakeChannel()
        client = FakeClient(
            {
                2: FakeChannel(original_message),
                21: status_channel,
                30: error_channel,
            }
        )
        config = validate_channel_config(channel_mapping())
        await self.reconcile(client, config)
        await self.reconcile(client, config)
        self.assertEqual(original_message.replies, [])
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(error_channel.sent), 1)
        self.assertNotEqual(status_channel.sent[0], error_channel.sent[0])
        self.assertIn("Retry limit reached", " ".join(status_channel.sent + error_channel.sent))

    async def test_no_delivery_route_leaves_notification_pending(self):
        self.store.transition(
            self.request["id"],
            "complete",
            "BookBot imported media",
            event_type="completed",
            service="bookbot",
        )
        config = validate_channel_config(channel_mapping())
        await self.reconcile(FakeClient({}), config)
        self.assertGreaterEqual(len(self.store.pending_notification_deliveries()), 2)
        self.assertIsNone(self.store.get_request(self.request["id"])["notified_at"])

    async def test_partial_terminal_delivery_retries_only_missing_route(self):
        self.store.transition(
            self.request["id"],
            "complete",
            "BookBot imported media",
            event_type="completed",
            service="bookbot",
            external_title="Dune EPUB",
        )
        original_message = FakeMessage()
        status_channel = FakeChannel()
        addition_channel = FailOnceChannel()
        client = FakeClient(
            {
                2: FakeChannel(original_message),
                21: status_channel,
                22: addition_channel,
            }
        )
        config = validate_channel_config(channel_mapping())

        await self.reconcile(client, config)
        self.assertEqual(original_message.replies, [])
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(addition_channel.sent, [])
        self.assertIsNone(self.store.get_request(self.request["id"])["notified_at"])

        await self.reconcile(client, config)
        await self.reconcile(client, config)
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(addition_channel.sent), 1)
        self.assertEqual(addition_channel.attempts, 2)
        self.assertIsNotNone(self.store.get_request(self.request["id"])["notified_at"])

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
        status_channel = FakeChannel()
        addition_channel = FakeChannel()
        client = FakeClient(
            {
                2: FakeChannel(original_message),
                21: status_channel,
                22: addition_channel,
            }
        )
        config = validate_channel_config(channel_mapping())
        await self.reconcile(client, config)
        self.assertEqual(original_message.replies, [])
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(addition_channel.sent), 1)
        self.assertIn(
            "imported to its DAS library path by Radarr",
            " ".join(status_channel.sent + addition_channel.sent),
        )

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

    async def test_shelfarr_intermediate_states_are_delivered_once(self):
        self.queue_shelfarr()
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "downloading",
                "attention_needed": False,
                "issue_description": None,
                "book": {"title": "Dune", "book_type": "ebook"},
            }
        )
        services = FakeServices({"shelfarr": shelfarr})
        queue_channel = FakeChannel()
        config = validate_channel_config(channel_mapping())
        client = FakeClient({20: queue_channel})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        await self.reconcile(client, config)
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        await self.reconcile(client, config)
        self.assertEqual(len(queue_channel.sent), 1)
        self.assertIn("actively downloading", queue_channel.sent[0])

        shelfarr.outcome = {
            **shelfarr.outcome,
            "status": "processing",
        }
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        await self.reconcile(client, config)
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        await self.reconcile(client, config)
        self.assertEqual(len(queue_channel.sent), 2)
        self.assertIn("validating and importing", queue_channel.sent[1])
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "processing")

    async def test_interrupted_shelfarr_dispatch_recovers_original_request_id(self):
        self.store.transition(
            self.request["id"],
            "processing",
            "Dispatching to Shelfarr",
            service="shelfarr",
        )
        self.store.initialize()
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "searching",
                "attention_needed": False,
                "issue_description": None,
                "book": {"title": "Dune", "book_type": "ebook"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_id"], "73")
        self.assertEqual(saved["external_status"], "searching")
        self.assertEqual(shelfarr.request_ids[0], self.request["id"])
        self.assertEqual(
            [event["event_type"] for event in self.store.events_for(self.request["id"])].count(
                "shelfarr_recovered"
            ),
            1,
        )
        recovered_deliveries = self.store.pending_notification_deliveries()
        self.assertCountEqual(
            [(row["event_key"], row["route"]) for row in recovered_deliveries],
            [
                ("request_accepted", "request-status"),
                ("download_queued", "download-queue"),
            ],
        )

    async def test_claimed_interrupted_selection_requires_exact_recovered_work(self):
        self.claim_candidate_for_dispatch()
        self.store.initialize()
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "searching",
                "attention_needed": False,
                "book": {
                    "title": "Wrong Dune",
                    "book_type": "ebook",
                    "content_kind": "book",
                    "work_id": "openlibrary:OTHER",
                },
            }
        )
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        self.assertEqual(self.store.get_request(self.request["id"])["status"], "processing")
        self.assertIsNone(self.store.get_request(self.request["id"])["external_id"])
        alerts = self.store.pending_notification_deliveries()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["event_key"], "recovery_uncertain")
        self.assertIn("confirmed identity", alerts[0]["message"])

        shelfarr.outcome = {
            **shelfarr.outcome,
            "book": {
                **shelfarr.outcome["book"],
                "title": "Dune",
                "work_id": "openlibrary:OL893415W",
            },
        }
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        recovered = self.store.get_request(self.request["id"])
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["external_id"], "73")
        self.assertCountEqual(
            [
                (row["event_key"], row["route"])
                for row in self.store.pending_notification_deliveries()
            ],
            [
                ("recovery_uncertain", "system-health"),
                ("request_accepted", "request-status"),
                ("download_queued", "download-queue"),
            ],
        )

    async def test_claimed_uncertain_selection_requires_exact_recovered_work(self):
        self.claim_candidate_for_dispatch()
        self.store.transition(
            self.request["id"],
            "queued",
            "Shelfarr submission is awaiting correlation recovery",
            event_type="shelfarr_submission_uncertain",
            service="shelfarr",
            external_status="submission_uncertain",
        )
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "pending",
                "attention_needed": False,
                "book": {
                    "title": "Wrong Dune",
                    "book_type": "ebook",
                    "content_kind": "book",
                    "work_id": "openlibrary:OTHER",
                },
            }
        )
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        pending = self.store.get_request(self.request["id"])
        self.assertEqual(pending["status"], "queued")
        self.assertIsNone(pending["external_id"])
        self.assertEqual(
            self.store.pending_notification_deliveries()[0]["event_key"],
            "submission_uncertain",
        )

        shelfarr.outcome = {
            **shelfarr.outcome,
            "book": {
                **shelfarr.outcome["book"],
                "title": "Dune",
                "work_id": "openlibrary:OL893415W",
            },
        }
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        recovered = self.store.get_request(self.request["id"])
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["external_id"], "73")

    async def test_corrupt_claimed_confirmation_quarantines_without_stopping_loop(self):
        self.claim_candidate_for_dispatch()
        second, _ = self.store.create_request(
            discord_user_id="2",
            discord_username="another reader",
            channel_id="2",
            message_id="101",
            media_type="ebooks",
            raw_request="Foundation",
            title="Foundation",
            author="Isaac Asimov",
        )
        self.store.transition(
            second["id"],
            "processing",
            "Dispatching to Shelfarr",
            service="shelfarr",
        )
        self.store.initialize()
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "searching",
                "attention_needed": False,
                "book": {
                    "title": "Dune",
                    "book_type": "ebook",
                    "content_kind": "book",
                    "work_id": "openlibrary:OL893415W",
                },
            }
        )

        real_get = self.store.get_candidate_confirmation

        def corrupt_first(request_id):
            if request_id == self.request["id"]:
                raise ValueError("corrupt candidate snapshot")
            return real_get(request_id)

        with patch.object(
            self.store, "get_candidate_confirmation", side_effect=corrupt_first
        ):
            self.assertEqual(
                reconcile_shelfarr_requests(
                    self.store, FakeServices({"shelfarr": shelfarr})
                ),
                1,
            )

        self.assertEqual(
            self.store.get_request(self.request["id"])["status"], "processing"
        )
        self.assertIsNone(self.store.get_request(self.request["id"])["external_id"])
        self.assertEqual(self.store.get_request(second["id"])["status"], "queued")
        self.assertEqual(self.store.get_request(second["id"])["external_id"], "73")

    async def test_missing_claimed_option_stays_quarantined(self):
        self.claim_candidate_for_dispatch()
        self.store.initialize()
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "searching",
                "attention_needed": False,
                "book": {
                    "title": "Dune",
                    "book_type": "ebook",
                    "content_kind": "book",
                    "work_id": "openlibrary:OL893415W",
                },
            }
        )
        missing_option = {
            **self.store.get_candidate_confirmation(self.request["id"]),
            "options": [],
        }

        with patch.object(
            self.store,
            "get_candidate_confirmation",
            return_value=missing_option,
        ):
            self.assertEqual(
                reconcile_shelfarr_requests(
                    self.store, FakeServices({"shelfarr": shelfarr})
                ),
                0,
            )

        pending = self.store.get_request(self.request["id"])
        self.assertEqual(pending["status"], "processing")
        self.assertIsNone(pending["external_id"])
        self.assertEqual(
            self.store.pending_notification_deliveries()[0]["event_key"],
            "recovery_uncertain",
        )

    async def test_interrupted_shelfarr_dispatch_repeated_absence_stays_owned_then_recovers(self):
        self.store.transition(
            self.request["id"],
            "processing",
            "Dispatching to Shelfarr",
            service="shelfarr",
        )
        self.store.initialize()
        shelfarr = FakeShelfarrCompletionClient(None)
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        restarted = RequestStore(self.store.path)
        restarted.initialize()
        self.assertEqual(reconcile_shelfarr_requests(restarted, services), 0)

        pending = restarted.get_request(self.request["id"])
        self.assertEqual(pending["status"], "processing")
        self.assertEqual(pending["service"], "shelfarr")
        self.assertIsNone(pending["external_id"])
        self.assertEqual(
            [row["id"] for row in restarted.interrupted_shelfarr_requests()],
            [self.request["id"]],
        )
        event_types = [
            event["event_type"] for event in restarted.events_for(self.request["id"])
        ]
        self.assertNotIn("startup_reconciled", event_types)
        self.assertNotIn("shelfarr_submission_failed", event_types)
        self.assertEqual(restarted.pending_notifications(), [])
        alerts = restarted.pending_notification_deliveries()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["event_key"], "recovery_uncertain")
        self.assertEqual(alerts[0]["route"], "system-health")

        shelfarr.outcome = {
            "id": 73,
            "status": "searching",
            "attention_needed": False,
            "issue_description": None,
            "book": {"title": "Dune", "book_type": "ebook"},
        }
        self.assertEqual(reconcile_shelfarr_requests(restarted, services), 1)
        recovered = restarted.get_request(self.request["id"])
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["service"], "shelfarr")
        self.assertEqual(recovered["external_id"], "73")
        self.assertEqual(recovered["external_status"], "searching")
        self.assertEqual(restarted.interrupted_shelfarr_requests(), [])
        self.assertEqual(
            [row["id"] for row in restarted.queued_shelfarr_requests()],
            [self.request["id"]],
        )
        event_types = [
            event["event_type"] for event in restarted.events_for(self.request["id"])
        ]
        self.assertEqual(event_types.count("shelfarr_recovered"), 1)
        self.assertNotIn("startup_reconciled", event_types)
        self.assertEqual(shelfarr.cancelled_ids, [])
        self.assertCountEqual(
            [
                row["event_key"]
                for row in restarted.pending_notification_deliveries()
            ],
            ["recovery_uncertain", "request_accepted", "download_queued"],
        )
        self.assertTrue(shelfarr.request_ids)
        self.assertEqual(
            set(shelfarr.request_ids),
            {self.request["id"], "73"},
        )

    async def test_shelfarr_completion_routes_status_and_addition(self):
        self.queue_shelfarr("processing")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "completed",
                "attention_needed": False,
                "issue_description": None,
                "book": {"title": "Dune"},
            }
        )
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["external_status"], "completed")
        self.assertEqual(
            [event["event_type"] for event in self.store.events_for(self.request["id"])].count(
                "shelfarr_completed"
            ),
            1,
        )

        status_channel = FakeChannel()
        addition_channel = FakeChannel()
        await self.reconcile(
            FakeClient({21: status_channel, 22: addition_channel}),
            validate_channel_config(channel_mapping()),
        )
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(addition_channel.sent), 1)
        self.assertIn("by Shelfarr", " ".join(status_channel.sent + addition_channel.sent))

    async def test_shelfarr_attention_failure_routes_status_and_import_error(self):
        self.queue_shelfarr("searching")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "failed",
                "attention_needed": True,
                "issue_description": "All automatic candidates were exhausted",
                "book": {"title": "Dune"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], "All automatic candidates were exhausted")
        status_channel = FakeChannel()
        error_channel = FakeChannel()
        await self.reconcile(
            FakeClient({21: status_channel, 30: error_channel}),
            validate_channel_config(channel_mapping()),
        )
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(error_channel.sent), 1)
        self.assertIn("All automatic candidates were exhausted", error_channel.sent[0])

    async def test_shelfarr_plain_acquisition_failure_is_request_status_only(self):
        self.queue_shelfarr("searching")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "failed",
                "attention_needed": False,
                "issue_description": "All automatic candidates were exhausted",
                "book": {"title": "Dune"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        status_channel = FakeChannel()
        error_channel = FakeChannel()
        await self.reconcile(
            FakeClient({21: status_channel, 30: error_channel}),
            validate_channel_config(channel_mapping()),
        )
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(error_channel.sent, [])

    async def test_shelfarr_write_failure_routes_import_error_without_processing_poll(self):
        self.queue_shelfarr("searching")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "failed",
                "attention_needed": False,
                "issue_description": (
                    "Direct download could not write to the configured library storage"
                ),
                "book": {"title": "Dune"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        status_channel = FakeChannel()
        error_channel = FakeChannel()
        await self.reconcile(
            FakeClient({21: status_channel, 30: error_channel}),
            validate_channel_config(channel_mapping()),
        )
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(error_channel.sent), 1)

    async def test_shelfarr_post_processing_failure_routes_import_error(self):
        self.queue_shelfarr("processing")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "failed",
                "attention_needed": True,
                "issue_description": "Final import validation failed",
                "book": {"title": "Dune"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        status_channel = FakeChannel()
        error_channel = FakeChannel()
        await self.reconcile(
            FakeClient({21: status_channel, 30: error_channel}),
            validate_channel_config(channel_mapping()),
        )
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(error_channel.sent), 1)
        self.assertIn("Final import validation failed", error_channel.sent[0])

    async def test_shelfarr_retryable_not_found_remains_queued(self):
        self.queue_shelfarr("searching")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "not_found",
                "attention_needed": False,
                "issue_description": None,
                "book": {"title": "Dune"},
            }
        )
        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "not_found")
        self.assertEqual(self.store.pending_notification_deliveries(), [])

    async def test_shelfarr_purchase_only_result_is_cancelled_with_specific_reason(self):
        self.queue_shelfarr("searching")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "awaiting_purchase",
                "attention_needed": True,
                "issue_description": None,
                "book": {"title": "Dune"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["external_status"], "failed")
        self.assertIn("purchase/manual-upload", saved["error"])

    async def test_shelfarr_attention_is_cancelled_before_terminal_failure(self):
        self.queue_shelfarr("searching")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "searching",
                "attention_needed": True,
                "issue_description": "Administrator review required",
                "book": {"title": "Dune"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        self.assertEqual(shelfarr.cancelled_ids, ["73"])
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["external_status"], "failed")
        self.assertEqual(saved["error"], "Administrator review required")
        events = self.store.events_for(self.request["id"])
        self.assertEqual(events[-1]["event_type"], "shelfarr_manual_intervention")
        status_channel = FakeChannel()
        error_channel = FakeChannel()
        await self.reconcile(
            FakeClient({21: status_channel, 30: error_channel}),
            validate_channel_config(channel_mapping()),
        )
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(error_channel.sent), 1)
        self.assertIn("Manual review required", error_channel.sent[0])

    async def test_uncertain_submission_recovers_original_huey_id_after_outage(self):
        self.queue_uncertain_shelfarr()
        shelfarr = FakeShelfarrCompletionClient(ServiceError("temporary outage"))
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        pending = self.store.get_request(self.request["id"])
        self.assertEqual(pending["external_status"], "submission_uncertain")
        self.assertIsNone(pending["external_id"])

        shelfarr.outcome = {
            "id": 73,
            "status": "pending",
            "attention_needed": False,
            "book": {"title": "Dune", "book_type": "ebook"},
        }
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        recovered = self.store.get_request(self.request["id"])
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["service"], "shelfarr")
        self.assertEqual(recovered["external_id"], "73")
        self.assertEqual(recovered["external_status"], "pending")
        self.assertEqual(
            self.store.events_for(self.request["id"])[-1]["event_type"],
            "shelfarr_recovered",
        )
        self.assertCountEqual(
            [
                row["event_key"]
                for row in self.store.pending_notification_deliveries()
            ],
            ["request_accepted", "download_queued"],
        )

    async def test_uncertain_submission_repeated_absence_stays_owned_then_recovers(self):
        self.queue_uncertain_shelfarr()
        shelfarr = FakeShelfarrCompletionClient(None)
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        restarted = RequestStore(self.store.path)
        restarted.initialize()
        self.assertEqual(reconcile_shelfarr_requests(restarted, services), 0)

        pending = restarted.get_request(self.request["id"])
        self.assertEqual(pending["status"], "queued")
        self.assertEqual(pending["service"], "shelfarr")
        self.assertIsNone(pending["external_id"])
        self.assertEqual(pending["external_status"], "submission_uncertain")
        self.assertEqual(
            [row["id"] for row in restarted.uncertain_shelfarr_requests()],
            [self.request["id"]],
        )
        event_types = [
            event["event_type"] for event in restarted.events_for(self.request["id"])
        ]
        self.assertNotIn("shelfarr_submission_failed", event_types)
        self.assertNotIn("startup_reconciled", event_types)
        self.assertEqual(restarted.pending_notifications(), [])
        alerts = restarted.pending_notification_deliveries()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["event_key"], "submission_uncertain")
        self.assertEqual(alerts[0]["route"], "import-errors")

        shelfarr.outcome = {
            "id": 73,
            "status": "pending",
            "attention_needed": False,
            "issue_description": None,
            "book": {"title": "Dune", "book_type": "ebook"},
        }
        self.assertEqual(reconcile_shelfarr_requests(restarted, services), 1)
        recovered = restarted.get_request(self.request["id"])
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["service"], "shelfarr")
        self.assertEqual(recovered["external_id"], "73")
        self.assertEqual(recovered["external_status"], "pending")
        self.assertEqual(restarted.uncertain_shelfarr_requests(), [])
        self.assertEqual(
            [row["id"] for row in restarted.queued_shelfarr_requests()],
            [self.request["id"]],
        )
        event_types = [
            event["event_type"] for event in restarted.events_for(self.request["id"])
        ]
        self.assertEqual(
            event_types.count("shelfarr_recovered"),
            1,
        )
        self.assertNotIn("shelfarr_submission_failed", event_types)
        self.assertEqual(shelfarr.cancelled_ids, [])
        self.assertCountEqual(
            [
                row["event_key"]
                for row in restarted.pending_notification_deliveries()
            ],
            ["submission_uncertain", "request_accepted", "download_queued"],
        )
        self.assertTrue(shelfarr.request_ids)
        self.assertEqual(
            set(shelfarr.request_ids),
            {self.request["id"], "73"},
        )

    async def test_uncertain_submission_wrong_format_stays_quarantined(self):
        self.queue_uncertain_shelfarr()
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "pending",
                "attention_needed": False,
                "book": {"title": "Dune", "book_type": "audiobook"},
            }
        )
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        pending = self.store.get_request(self.request["id"])
        self.assertEqual(pending["status"], "queued")
        self.assertEqual(pending["service"], "shelfarr")
        self.assertIsNone(pending["external_id"])
        self.assertEqual(pending["external_status"], "submission_uncertain")
        self.assertEqual(
            [row["id"] for row in self.store.uncertain_shelfarr_requests()],
            [self.request["id"]],
        )
        deliveries = self.store.pending_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["event_key"], "submission_uncertain")
        self.assertEqual(deliveries[0]["route"], "import-errors")
        self.assertIn("unexpected book format", deliveries[0]["message"])
        self.assertEqual(shelfarr.cancelled_ids, [])

    async def test_shelfarr_processing_attention_routes_import_error_if_poll_was_missed(self):
        self.queue_shelfarr("downloading")
        shelfarr = FakeShelfarrCompletionClient(
            {
                "id": 73,
                "status": "processing",
                "attention_needed": True,
                "issue_description": "Final import validation failed",
                "book": {"title": "Dune"},
            }
        )

        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        self.assertEqual(shelfarr.cancelled_ids, ["73"])
        events = self.store.events_for(self.request["id"])
        self.assertEqual(events[-1]["event_type"], "shelfarr_import_failed")
        status_channel = FakeChannel()
        error_channel = FakeChannel()
        await self.reconcile(
            FakeClient({21: status_channel, 30: error_channel}),
            validate_channel_config(channel_mapping()),
        )
        self.assertEqual(len(status_channel.sent), 1)
        self.assertEqual(len(error_channel.sent), 1)

    async def test_recoverable_processing_attention_alerts_once_then_completes(self):
        self.queue_shelfarr("downloading")

        class RecoverableShelfarr(FakeShelfarrCompletionClient):
            def __init__(self):
                super().__init__(None)
                self.completed = False

            def get_request(self, request_id):
                self.request_ids.append(request_id)
                if self.completed:
                    return {
                        "id": 73,
                        "status": "completed",
                        "attention_needed": False,
                        "book": {"title": "Dune"},
                    }
                return {
                    "id": 73,
                    "status": "processing",
                    "attention_needed": True,
                    "issue_description": "Final import validation failed",
                    "book": {"title": "Dune"},
                }

            def cancel_request(self, request_id):
                self.cancelled_ids.append(request_id)
                raise ServiceError("recovery owner retained request")

        shelfarr = RecoverableShelfarr()
        services = FakeServices({"shelfarr": shelfarr})

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        pending = self.store.pending_notification_deliveries()
        alerts = [row for row in pending if row["event_key"] == "import_failed"]
        self.assertEqual(len(alerts), 1)
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "processing")

        shelfarr.completed = True
        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        completed = self.store.get_request(self.request["id"])
        self.assertEqual(completed["status"], "completed")

    async def test_unconfirmed_attention_cancellation_still_alerts_import_errors(self):
        self.queue_shelfarr("downloading")

        class UnconfirmedShelfarr(FakeShelfarrCompletionClient):
            def cancel_request(self, request_id):
                self.cancelled_ids.append(request_id)
                return {
                    "id": 73,
                    "status": "processing",
                    "attention_needed": True,
                }

        shelfarr = UnconfirmedShelfarr(
            {
                "id": 73,
                "status": "processing",
                "attention_needed": True,
                "issue_description": "Final import validation failed",
                "book": {"title": "Dune"},
            }
        )
        self.assertEqual(
            reconcile_shelfarr_requests(
                self.store, FakeServices({"shelfarr": shelfarr})
            ),
            1,
        )
        saved = self.store.get_request(self.request["id"])
        self.assertEqual(saved["status"], "queued")
        alerts = [
            row
            for row in self.store.pending_notification_deliveries()
            if row["event_key"] == "import_failed"
        ]
        self.assertEqual(len(alerts), 1)

    async def test_notification_loop_delivers_arr_without_waiting_for_shelfarr(self):
        client = Mock()
        client.is_closed.return_value = False
        config = validate_channel_config(channel_mapping())
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

    async def test_shelfarr_reconciliation_runs_in_independent_loop(self):
        client = Mock()
        client.is_closed.return_value = False
        order = []

        async def stop_after_cycle(_seconds):
            raise asyncio.CancelledError

        with (
            patch(
                "huey.reconcile_shelfarr_requests",
                side_effect=lambda _store, _services: order.append("shelfarr"),
            ),
            patch(
                "huey.asyncio.to_thread",
                new=AsyncMock(side_effect=lambda fn, *args: fn(*args)),
            ),
            patch("huey.asyncio.sleep", side_effect=stop_after_cycle),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await shelfarr_reconciliation_loop(
                    client, self.store, object(), 30
                )
        self.assertEqual(order, ["shelfarr"])


class DiscordAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledgements_reply_to_each_original_but_duplicate_target_has_no_lifecycle_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            dispatcher = Mock(
                return_value=result(
                    "queued",
                    "Queued Dune in qBittorrent",
                    service="qbittorrent",
                    external_id="a" * 40,
                    external_title="Dune EPUB",
                )
            )
            processor = RequestProcessor(
                store,
                services={"direct": object()},
                dispatcher=dispatcher,
            )
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")

            intake_channel = FakeChannel()
            intake_channel.id = 2
            status_channel = FakeChannel()
            queue_channel = FakeChannel()
            client.channels = {
                2: intake_channel,
                20: queue_channel,
                21: status_channel,
            }
            first = FakeIncomingMessage(
                message_id=200,
                channel=intake_channel,
                content="Dune by Frank Herbert",
            )
            duplicate = FakeIncomingMessage(
                message_id=201,
                channel=intake_channel,
                content="dune by FRANK HERBERT",
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(first)
                await reconcile_notifications(client, config, store)
                lifecycle_counts = (len(status_channel.sent), len(queue_channel.sent))

                await client.on_message(duplicate)
                await reconcile_notifications(client, config, store)

            self.assertEqual(len(first.replies), 1)
            self.assertEqual(len(duplicate.replies), 1)
            self.assertIn("Request #", first.replies[0])
            self.assertIn("Request #", duplicate.replies[0])
            self.assertEqual(dispatcher.call_count, 1)
            self.assertEqual(lifecycle_counts, (1, 1))
            self.assertEqual(
                (len(status_channel.sent), len(queue_channel.sent)),
                lifecycle_counts,
            )

    async def test_candidate_reply_confirms_once_then_uses_normal_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()

            class SelectionServices:
                shelfarr_enabled = True

                def __init__(self):
                    self.book_selected = Mock(
                        return_value=result(
                            "queued",
                            "Shelfarr accepted Dune.",
                            service="shelfarr",
                            external_id="73",
                            external_title="Dune",
                            external_status="pending",
                        )
                    )

            services = SelectionServices()
            dispatcher = Mock(
                return_value=result(
                    "awaiting_selection",
                    "Choose one candidate.",
                    service="shelfarr",
                    selection_proposal=candidate_proposal(),
                )
            )
            processor = RequestProcessor(store, services=services, dispatcher=dispatcher)
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")

            intake_channel = FakeChannel()
            intake_channel.id = 2
            queue_channel = FakeChannel()
            status_channel = FakeChannel()
            client.channels = {
                2: intake_channel,
                20: queue_channel,
                21: status_channel,
            }
            request_message = FakeIncomingMessage(
                message_id=700,
                channel=intake_channel,
                content="Dune by Frank Herbert",
                reply_message_id=900,
            )
            selection_message = FakeIncomingMessage(
                message_id=701,
                channel=intake_channel,
                content="2",
                reference_id=900,
                reply_message_id=901,
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(request_message)
                self.assertEqual(status_channel.sent, [])
                self.assertEqual(queue_channel.sent, [])
                self.assertIn("Reply directly", request_message.replies[0])
                self.assertEqual(store.get_request(1)["status"], "awaiting_selection")

                await client.on_message(selection_message)
                lifecycle_counts = (len(status_channel.sent), len(queue_channel.sent))
                await client.on_message(selection_message)

            self.assertEqual(dispatcher.call_count, 1)
            services.book_selected.assert_called_once()
            self.assertEqual(len(selection_message.replies), 1)
            self.assertIn(
                "Confirmed. Continuing request.", selection_message.replies[0]
            )
            self.assertEqual(lifecycle_counts, (1, 1))
            self.assertEqual(
                (len(status_channel.sent), len(queue_channel.sent)), lifecycle_counts
            )

    async def test_invalid_or_wrong_user_candidate_reply_is_corrective_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()

            class SelectionServices:
                shelfarr_enabled = True
                book_selected = Mock()

            services = SelectionServices()
            processor = RequestProcessor(
                store,
                services=services,
                dispatcher=Mock(
                    return_value=result(
                        "awaiting_selection",
                        "Choose one candidate.",
                        service="shelfarr",
                        selection_proposal=candidate_proposal(),
                    )
                ),
            )
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")

            intake_channel = FakeChannel()
            intake_channel.id = 2
            client.channels = {2: intake_channel}
            request_message = FakeIncomingMessage(
                message_id=710,
                channel=intake_channel,
                content="Dune by Frank Herbert",
                reply_message_id=910,
            )
            malformed = FakeIncomingMessage(
                message_id=711,
                channel=intake_channel,
                content=" 1 ",
                reference_id=910,
            )
            wrong_user = FakeIncomingMessage(
                message_id=712,
                channel=intake_channel,
                content="1",
                author_id=100,
                reference_id=910,
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(request_message)
                await client.on_message(malformed)
                await client.on_message(wrong_user)

            self.assertEqual(len(malformed.replies), 1)
            self.assertIn("not a valid choice", malformed.replies[0])
            self.assertEqual(len(wrong_user.replies), 1)
            self.assertIn("original requester", wrong_user.replies[0])
            services.book_selected.assert_not_called()
            self.assertEqual(store.get_request(1)["status"], "awaiting_selection")

    async def test_candidate_prompt_reply_failure_releases_exact_target(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()

            class SelectionServices:
                shelfarr_enabled = True

            dispatcher = Mock(
                return_value=result(
                    "awaiting_selection",
                    "Choose one candidate.",
                    service="shelfarr",
                    selection_proposal=candidate_proposal(),
                )
            )
            processor = RequestProcessor(
                store, services=SelectionServices(), dispatcher=dispatcher
            )
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")

            intake_channel = FakeChannel()
            intake_channel.id = 2
            client.channels = {2: intake_channel}
            failed_prompt = FailReplyIncomingMessage(
                message_id=720,
                channel=intake_channel,
                content="Dune by Frank Herbert",
            )
            retry = FakeIncomingMessage(
                message_id=721,
                channel=intake_channel,
                content="Dune by Frank Herbert",
                reply_message_id=920,
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(failed_prompt)
                self.assertEqual(store.get_request(1)["status"], "needs_selection")
                await client.on_message(retry)

            self.assertEqual(store.get_request(2)["status"], "awaiting_selection")
            self.assertEqual(dispatcher.call_count, 2)

    async def test_orphan_or_unresolved_candidate_reply_never_becomes_book_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()

            class SelectionServices:
                shelfarr_enabled = True
                book_selected = Mock()

            services = SelectionServices()
            dispatcher = Mock(
                return_value=result(
                    "awaiting_selection",
                    "Choose one candidate.",
                    service="shelfarr",
                    selection_proposal=candidate_proposal(),
                )
            )
            processor = RequestProcessor(
                store, services=services, dispatcher=dispatcher
            )
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")
            client.user = types.SimpleNamespace(id=42, bot=True)

            intake_channel = FakeChannel()
            intake_channel.id = 2
            client.channels = {2: intake_channel}
            request_message = FakeIncomingMessage(
                message_id=730,
                channel=intake_channel,
                content="Dune by Frank Herbert",
                reply_message_id=930,
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with (
                patch("huey.asyncio.to_thread", new=direct_call),
                patch.object(store, "bind_candidate_prompt", return_value=False),
            ):
                await client.on_message(request_message)

            self.assertEqual(store.get_request(1)["status"], "needs_selection")
            intake_channel.message = types.SimpleNamespace(
                author=client.user,
                content=(
                    "⚠️ Request #1 needs one metadata choice\n"
                    "Type: ebooks\n1. Dune\n"
                    "Reply directly to this message with one number within 15 minutes."
                ),
            )
            orphan_reply = FakeIncomingMessage(
                message_id=731,
                channel=intake_channel,
                content="1",
                reference_id=930,
            )
            unresolved_reply = FakeIncomingMessage(
                message_id=732,
                channel=intake_channel,
                content="first",
                reference_id=931,
            )

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(orphan_reply)
                intake_channel.message = None
                await client.on_message(unresolved_reply)

            self.assertIn("not active", orphan_reply.replies[0])
            self.assertIn("not active", unresolved_reply.replies[0])
            self.assertEqual(dispatcher.call_count, 1)
            services.book_selected.assert_not_called()
            with store.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
                    1,
                )

    async def test_reply_to_unrelated_message_preserves_movie_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            dispatcher = Mock(
                return_value=result(
                    "queued",
                    "Radarr accepted Alien.",
                    service="radarr",
                    external_id="73",
                    external_title="Alien",
                )
            )
            processor = RequestProcessor(
                store, services=object(), dispatcher=dispatcher
            )
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")
            client.user = types.SimpleNamespace(id=42, bot=True)

            movie_channel = FakeChannel(
                message=types.SimpleNamespace(
                    author=types.SimpleNamespace(id=100, bot=False)
                )
            )
            movie_channel.id = 1
            client.channels = {1: movie_channel}
            message = FakeIncomingMessage(
                message_id=740,
                channel=movie_channel,
                content="movie: Alien",
                reference_id=739,
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(message)

            dispatcher.assert_called_once()
            self.assertEqual(store.get_request(1)["media_type"], "movies-tv")
            self.assertEqual(store.get_request(1)["status"], "queued")

    async def test_reply_to_non_candidate_huey_message_preserves_movie_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            dispatcher = Mock(
                return_value=result(
                    "queued",
                    "Radarr accepted Alien.",
                    service="radarr",
                    external_id="73",
                    external_title="Alien",
                )
            )
            processor = RequestProcessor(store, services=object(), dispatcher=dispatcher)
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")
            client.user = types.SimpleNamespace(id=42, bot=True)

            movie_channel = FakeChannel(
                message=types.SimpleNamespace(
                    author=client.user,
                    content="✅ Request #9\nType: movies-tv\nRadarr accepted Arrival.",
                )
            )
            movie_channel.id = 1
            client.channels = {1: movie_channel}
            message = FakeIncomingMessage(
                message_id=750,
                channel=movie_channel,
                content="movie: Alien",
                reference_id=749,
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(message)

            dispatcher.assert_called_once()
            self.assertEqual(store.get_request(1)["media_type"], "movies-tv")
            self.assertEqual(store.get_request(1)["status"], "queued")

    async def test_standalone_numeric_book_message_is_rejected_before_persistence(self):
        for media_type in ("ebooks", "audiobooks"):
            for selection_token in ("1", "2", "3"):
                with (
                    self.subTest(
                        media_type=media_type, selection_token=selection_token
                    ),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    store = RequestStore(Path(directory) / "huey.db")
                    store.initialize()
                    dispatcher = Mock(return_value=result("queued", "Unexpected dispatch"))
                    processor = RequestProcessor(
                        store, services=object(), dispatcher=dispatcher
                    )
                    config = validate_channel_config(channel_mapping())
                    discord_module = types.SimpleNamespace(
                        Intents=FakeIntents,
                        Client=FakeDiscordClient,
                    )
                    with patch.dict(sys.modules, {"discord": discord_module}):
                        client = build_client(
                            config, processor, Path(directory) / "ready"
                        )

                    intake_channel = FakeChannel()
                    intake_channel.id = REQUESTS[media_type]
                    client.channels = {intake_channel.id: intake_channel}
                    message = FakeIncomingMessage(
                        message_id=760,
                        channel=intake_channel,
                        content=selection_token,
                    )

                    async def direct_call(function, *args, **kwargs):
                        return function(*args, **kwargs)

                    with patch("huey.asyncio.to_thread", new=direct_call):
                        await client.on_message(message)

                    dispatcher.assert_not_called()
                    self.assertEqual(len(message.replies), 1)
                    with store.connect() as connection:
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM requests"
                            ).fetchone()[0],
                            0,
                        )

    async def test_numeric_ebook_title_uses_ordinary_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            dispatcher = Mock(
                return_value=result(
                    "queued",
                    "Shelfarr accepted 1984.",
                    service="shelfarr",
                    external_id="73",
                    external_title="1984",
                    external_status="pending",
                )
            )
            processor = RequestProcessor(
                store, services=object(), dispatcher=dispatcher
            )
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")

            intake_channel = FakeChannel()
            intake_channel.id = REQUESTS["ebooks"]
            status_channel = FakeChannel()
            queue_channel = FakeChannel()
            client.channels = {
                intake_channel.id: intake_channel,
                ACTIVITY["request-status"]: status_channel,
                ACTIVITY["download-queue"]: queue_channel,
            }
            message = FakeIncomingMessage(
                message_id=761,
                channel=intake_channel,
                content="1984",
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(message)

            dispatcher.assert_called_once()
            self.assertEqual(len(message.replies), 1)
            saved = store.get_request(1)
            self.assertEqual(saved["media_type"], "ebooks")
            self.assertEqual(saved["raw_request"], "1984")
            self.assertEqual(saved["title"], "1984")
            self.assertEqual(saved["status"], "queued")
            self.assertEqual(len(status_channel.sent), 1)
            self.assertEqual(len(queue_channel.sent), 1)

    async def test_immediate_handler_terminal_result_does_not_create_import_event(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                store = RequestStore(Path(directory) / "huey.db")
                store.initialize()
                dispatcher = Mock(
                    return_value=result(
                        status,
                        f"Handler returned {status}",
                        service="qbittorrent",
                        external_id="b" * 40,
                        external_title="Dune EPUB",
                    )
                )
                processor = RequestProcessor(
                    store,
                    services={"direct": object()},
                    dispatcher=dispatcher,
                )
                config = validate_channel_config(channel_mapping())
                discord_module = types.SimpleNamespace(
                    Intents=FakeIntents,
                    Client=FakeDiscordClient,
                )
                with patch.dict(sys.modules, {"discord": discord_module}):
                    client = build_client(
                        config, processor, Path(directory) / "ready"
                    )

                intake_channel = FakeChannel()
                intake_channel.id = 2
                status_channel = FakeChannel()
                queue_channel = FakeChannel()
                addition_channel = FakeChannel()
                error_channel = FakeChannel()
                client.channels = {
                    2: intake_channel,
                    20: queue_channel,
                    21: status_channel,
                    22: addition_channel,
                    30: error_channel,
                }
                message = FakeIncomingMessage(
                    message_id=300,
                    channel=intake_channel,
                    content="Dune by Frank Herbert",
                )

                async def direct_call(function, *args, **kwargs):
                    return function(*args, **kwargs)

                with patch("huey.asyncio.to_thread", new=direct_call):
                    await client.on_message(message)
                    await reconcile_notifications(client, config, store)

                self.assertEqual(len(message.replies), 1)
                self.assertEqual(len(status_channel.sent), 1)
                self.assertEqual(queue_channel.sent, [])
                self.assertEqual(addition_channel.sent, [])
                self.assertEqual(error_channel.sent, [])
                self.assertIsNotNone(
                    store.get_request(1)["notified_at"]
                )

    async def test_acknowledgement_reply_failure_still_delivers_lifecycle_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            dispatcher = Mock(
                return_value=result(
                    "queued",
                    "Queued Dune in qBittorrent",
                    service="qbittorrent",
                    external_id="c" * 40,
                    external_title="Dune EPUB",
                )
            )
            processor = RequestProcessor(
                store,
                services={"direct": object()},
                dispatcher=dispatcher,
            )
            config = validate_channel_config(channel_mapping())
            discord_module = types.SimpleNamespace(
                Intents=FakeIntents,
                Client=FakeDiscordClient,
            )
            with patch.dict(sys.modules, {"discord": discord_module}):
                client = build_client(config, processor, Path(directory) / "ready")

            intake_channel = FakeChannel()
            intake_channel.id = 2
            queue_channel = FakeChannel()
            status_channel = FakeChannel()
            addition_channel = FakeChannel()
            error_channel = FakeChannel()
            client.channels = {
                2: intake_channel,
                20: queue_channel,
                21: status_channel,
                22: addition_channel,
                30: error_channel,
            }
            message = FailReplyIncomingMessage(
                message_id=400,
                channel=intake_channel,
                content="Dune by Frank Herbert",
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                await client.on_message(message)
                await reconcile_notifications(client, config, store)

            self.assertEqual(message.reply_attempts, 1)
            self.assertEqual(intake_channel.sent, [])
            self.assertEqual(len(status_channel.sent), 1)
            self.assertEqual(len(queue_channel.sent), 1)
            self.assertEqual(addition_channel.sent, [])
            self.assertEqual(error_channel.sent, [])
            event_types = [event["event_type"] for event in store.events_for(1)]
            self.assertEqual(event_types.count("notification_failed"), 1)


class DiscordChannelValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_request_and_lifecycle_channels_are_verified(self):
        config = validate_channel_config(channel_mapping())
        client = FakeClient(
            {
                channel_id: PermissionChannel()
                for channel_id in (
                    *REQUESTS.values(),
                    *ACTIVITY.values(),
                    *SYSTEM.values(),
                )
            }
        )
        await validate_discord_channels(client, config)

    async def test_missing_or_unwritable_channel_is_rejected(self):
        config = validate_channel_config(channel_mapping())
        channels = {
            channel_id: PermissionChannel()
            for channel_id in (
                *REQUESTS.values(),
                *ACTIVITY.values(),
                *SYSTEM.values(),
            )
        }
        channels[30] = PermissionChannel(FakePermissions(send=False))
        with self.assertRaisesRegex(RuntimeError, "send_messages"):
            await validate_discord_channels(FakeClient(channels), config)
        channels[30] = PermissionChannel()
        del channels[31]
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await validate_discord_channels(FakeClient(channels), config)

    async def test_history_permission_is_required_only_for_intake_channels(self):
        config = validate_channel_config(channel_mapping())
        channels = {
            channel_id: PermissionChannel(
                FakePermissions(history=channel_id in REQUESTS.values())
            )
            for channel_id in (
                *REQUESTS.values(),
                *ACTIVITY.values(),
                *SYSTEM.values(),
            )
        }
        await validate_discord_channels(FakeClient(channels), config)

        channels[2] = PermissionChannel(FakePermissions(history=False))
        with self.assertRaisesRegex(RuntimeError, "read_message_history"):
            await validate_discord_channels(FakeClient(channels), config)


if __name__ == "__main__":
    unittest.main()
