import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from huey import (
    build_client,
    reconcile_lazylibrarian_requests,
    reconcile_notifications,
    reconcile_shelfarr_requests,
    unavailable_retry_loop,
)
from database import RequestStore


class SilentRuntimeStore:
    def __init__(self):
        self.enqueued = []

    def unavailable_retry_is_silent(self, _request_id):
        return True

    def enqueue_notification(self, *values):
        self.enqueued.append(values)
        return True

    def claim_blocked_shelfarr_proof_checks(self):
        return []


class RetryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_loop_runs_processor_in_its_own_cycle(self):
        client = Mock()
        client.is_closed.return_value = False
        processor = Mock()
        processor.retry_due_unavailable_requests.return_value = 2

        async def stop_after_cycle(_seconds):
            raise asyncio.CancelledError

        with (
            patch(
                "huey.asyncio.to_thread",
                new=AsyncMock(side_effect=lambda function, *args: function(*args)),
            ),
            patch("huey.asyncio.sleep", side_effect=stop_after_cycle),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await unavailable_retry_loop(client, processor, 30)

        processor.retry_due_unavailable_requests.assert_called_once_with()

    async def test_build_client_starts_one_dedicated_retry_task(self):
        class Intents:
            message_content = False

            @classmethod
            def default(cls):
                return cls()

        class Client:
            def __init__(self, *, intents):
                self.intents = intents
                self.user = types.SimpleNamespace(id=777)

            def event(self, callback):
                setattr(self, callback.__name__, callback)
                return callback

            def is_closed(self):
                return False

            async def close(self):
                return None

        class Task:
            def done(self):
                return False

        names = []

        def capture_task(coroutine, *, name):
            names.append(name)
            coroutine.close()
            return Task()

        async def no_channel_validation(*_args):
            return None

        processor = types.SimpleNamespace(store=object(), services=object())
        config = types.SimpleNamespace(request_channels={})
        discord = types.SimpleNamespace(Intents=Intents, Client=Client)
        with (
            patch.dict(sys.modules, {"discord": discord}),
            patch("huey.validate_discord_channels", new=no_channel_validation),
            patch("huey._OneShotAsyncRecovery.ensure", new=AsyncMock(return_value=0)),
            patch("huey.write_ready_marker"),
            patch("huey.asyncio.create_task", side_effect=capture_task),
        ):
            client = build_client(config, processor, Path("/tmp/huey-test-ready"))
            await client.on_ready()

        self.assertEqual(names.count("huey-unavailable-retry-reconciliation"), 1)

    async def test_terminal_failure_is_silent_but_verified_completion_is_staged(self):
        silent_failure = {"id": 1, "status": "failed"}
        verified_completion = {"id": 2, "status": "completed"}
        store = Mock()
        store.pending_notifications.return_value = [
            silent_failure,
            verified_completion,
        ]
        store.unavailable_retry_is_silent.side_effect = lambda request_id: (
            request_id == 1
        )
        store.pending_notification_deliveries.return_value = []
        plan = types.SimpleNamespace(
            event_key="library_imported",
            route="recent-additions",
            message="Imported",
        )

        with (
            patch("huey.terminal_notifications", return_value=(plan,)),
            patch(
                "huey.asyncio.to_thread",
                new=AsyncMock(side_effect=lambda function, *args: function(*args)),
            ),
        ):
            await reconcile_notifications(
                types.SimpleNamespace(),
                types.SimpleNamespace(lifecycle_channels={}),
                store,
            )

        store.enqueue_notification.assert_called_once_with(
            2, "library_imported", "recent-additions", "Imported"
        )
        store.mark_notified_if_delivered.assert_called_once_with(
            2, "All staged Discord lifecycle notifications delivered"
        )

    async def test_stale_delivered_failure_cannot_mark_new_completion_notified(self):
        """Reproduce the failure-snapshot/final-import race deterministically."""

        with tempfile.TemporaryDirectory() as directory:
            store = RequestStore(Path(directory) / "huey.db")
            store.initialize()
            request, _ = store.create_request(
                discord_user_id="1",
                discord_username="reader",
                channel_id="2",
                message_id="notification-race",
                media_type="ebooks",
                raw_request="Dune by Frank Herbert",
                title="Dune",
                author="Frank Herbert",
            )
            request_id = int(request["id"])
            store.transition(
                request_id,
                "failed",
                "Initial request failed",
                event_type="handler_failed",
            )

            async def complete_while_sending_failure(_message):
                store.transition(
                    request_id,
                    "completed",
                    "Final import completed during notification delivery",
                    event_type="handler_completed",
                    service="bookbot",
                )

            channel = types.SimpleNamespace(
                send=AsyncMock(side_effect=complete_while_sending_failure)
            )
            client = types.SimpleNamespace(get_channel=lambda _channel_id: channel)
            config = types.SimpleNamespace(
                lifecycle_channels={"request-status": "21"}
            )

            async def direct_call(function, *args, **kwargs):
                return function(*args, **kwargs)

            with patch("huey.asyncio.to_thread", new=direct_call):
                self.assertEqual(
                    await reconcile_notifications(client, config, store),
                    1,
                )

            raced = store.get_request(request_id)
            self.assertEqual(raced["status"], "completed")
            self.assertIsNone(raced["notified_at"])
            self.assertTrue(
                store.notification_delivered(
                    request_id, "request_failed", "request-status"
                )
            )

            channel.send = AsyncMock()
            with patch("huey.asyncio.to_thread", new=direct_call):
                self.assertEqual(
                    await reconcile_notifications(client, config, store),
                    1,
                )

            self.assertTrue(
                store.notification_delivered(
                    request_id, "request_completed", "request-status"
                )
            )
            self.assertIsNotNone(store.get_request(request_id)["notified_at"])


class ShelfarrSilenceTests(unittest.TestCase):
    def test_recovered_acceptance_and_progress_do_not_create_plans(self):
        store = SilentRuntimeStore()
        recovered_notifications = []
        uncertain = {
            "id": 1,
            "media_type": "ebooks",
            "title": "Dune",
            "author": "Frank Herbert",
        }
        queued = {
            "id": 2,
            "media_type": "ebooks",
            "external_id": "73",
            "external_status": "pending",
            "title": "Dune",
        }
        store.uncertain_shelfarr_requests = Mock(return_value=[uncertain])
        store.interrupted_shelfarr_requests = Mock(return_value=[])
        store.queued_shelfarr_requests = Mock(return_value=[queued])
        store.get_ebook_cascade = Mock(return_value=None)
        store.get_candidate_confirmation = Mock(return_value=None)
        store.transition = Mock(
            side_effect=lambda *_args, **kwargs: recovered_notifications.extend(
                kwargs["notifications"]
            )
        )
        store.record_shelfarr_state = Mock(
            side_effect=lambda *_args, **_kwargs: True
        )
        shelfarr = Mock()
        shelfarr.recover_request.return_value = {
            "id": 73,
            "status": "pending",
            "book": {"title": "Dune", "book_type": "ebook"},
        }
        shelfarr.get_request.return_value = {
            "id": 73,
            "status": "processing",
            "attention_needed": False,
            "book": {"title": "Dune", "book_type": "ebook"},
        }
        services = types.SimpleNamespace(shelfarr=lambda: shelfarr)
        plan = types.SimpleNamespace(
            event_key="ebook_processing",
            route="request-status",
            message="Processing",
        )

        with patch(
            "huey.shelfarr_state_notifications", return_value=(plan,)
        ):
            self.assertEqual(reconcile_shelfarr_requests(store, services), 2)

        self.assertEqual(recovered_notifications, [])
        self.assertEqual(store.enqueued, [])

    def test_correlation_attention_remains_internal_only(self):
        store = SilentRuntimeStore()
        store.uncertain_shelfarr_requests = Mock(return_value=[{"id": 1}])
        store.interrupted_shelfarr_requests = Mock(return_value=[])
        store.queued_shelfarr_requests = Mock(return_value=[])
        shelfarr = Mock()
        shelfarr.recover_request.return_value = None
        services = types.SimpleNamespace(shelfarr=lambda: shelfarr)
        plan = types.SimpleNamespace(
            event_key="shelfarr_correlation_attention",
            route="system-health",
            message="Attention",
        )

        with patch(
            "huey.shelfarr_correlation_attention_notification",
            return_value=plan,
        ):
            self.assertEqual(reconcile_shelfarr_requests(store, services), 0)

        self.assertEqual(store.enqueued, [])


class LazyLibrarianSilenceTests(unittest.TestCase):
    def test_recovered_acceptance_and_download_progress_are_silent(self):
        store = SilentRuntimeStore()
        recovered_notifications = []
        progress_notifications = []
        uncertain = {
            "id": 1,
            "media_type": "ebooks",
            "lazylibrarian_book_id": "book-1",
            "title": "Dune",
        }
        queued = {
            "id": 2,
            "media_type": "ebooks",
            "external_id": "a" * 40,
            "external_status": "queued",
            "external_title": "Dune",
        }
        store.uncertain_lazylibrarian_requests = Mock(return_value=[uncertain])
        store.interrupted_lazylibrarian_requests = Mock(return_value=[])
        store.queued_lazylibrarian_requests = Mock(return_value=[queued])
        store.record_lazylibrarian_download = Mock(
            side_effect=lambda *_args, **kwargs: recovered_notifications.extend(
                kwargs["notifications"]
            )
            or True
        )
        store.record_lazylibrarian_state = Mock(
            side_effect=lambda *_args, **kwargs: progress_notifications.extend(
                kwargs["notifications"]
            )
            or True
        )
        lazylibrarian = Mock()
        lazylibrarian.recover_submission.return_value = {
            "state": "queued",
            "book_id": "book-1",
            "external_id": "a" * 40,
            "external_title": "Dune",
            "external_status": "queued",
        }
        qbittorrent = Mock()
        qbittorrent.find_torrent.return_value = {
            "hash": "a" * 40,
            "category": "ebooks",
            "save_path": "/downloads/ebooks",
            "state": "downloading",
            "progress": 0.5,
            "amount_left": 1,
        }
        services = types.SimpleNamespace(
            lazylibrarian=lambda: lazylibrarian,
            qbittorrent=lambda: qbittorrent,
        )

        self.assertEqual(reconcile_lazylibrarian_requests(store, services), 2)
        self.assertEqual(recovered_notifications, [])
        self.assertEqual(progress_notifications, [])
        self.assertEqual(store.enqueued, [])


if __name__ == "__main__":
    unittest.main()
