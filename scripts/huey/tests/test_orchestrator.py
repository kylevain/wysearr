import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from clients import ServiceError
from database import RequestStore
from matching import request_target_key
from orchestrator import RequestProcessor
from results import result


class RequestProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()
        self.delivery = {
            "discord_user_id": "1",
            "discord_username": "reader",
            "channel_id": "2",
            "message_id": "100",
            "media_type": "ebooks",
            "content": "Dune by Frank Herbert",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_success_persists_structured_handler_result(self):
        dispatcher = Mock(
            return_value=result(
                "queued",
                "Queued Dune",
                service="qbittorrent",
                external_id="guid-1",
                external_title="Dune EPUB",
            )
        )
        response = RequestProcessor(
            self.store, services={"direct": object()}, dispatcher=dispatcher
        ).process(self.delivery)
        self.assertEqual(response["status"], "queued")
        self.assertFalse(response["duplicate"])
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["service"], "qbittorrent")
        self.assertEqual(saved["external_id"], "guid-1")
        passed_request = dispatcher.call_args.args[0]
        self.assertEqual(passed_request["title"], "Dune")
        self.assertEqual(passed_request["author"], "Frank Herbert")
        self.assertEqual(
            [event["event_type"] for event in self.store.events_for(saved["id"])],
            ["received", "processing", "handler_queued"],
        )

    def test_duplicate_delivery_returns_existing_without_dispatch(self):
        dispatcher = Mock(return_value=result("queued", "Queued"))
        processor = RequestProcessor(self.store, services={}, dispatcher=dispatcher)
        first = processor.process(self.delivery)
        second = processor.process(self.delivery)
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(dispatcher.call_count, 1)
        event_types = [
            event["event_type"] for event in self.store.events_for(first["request_id"])
        ]
        self.assertIn("duplicate_delivery", event_types)

    def test_distinct_message_for_exact_active_target_reuses_request(self):
        dispatcher = Mock(return_value=result("queued", "Queued"))
        processor = RequestProcessor(self.store, services={}, dispatcher=dispatcher)
        first = processor.process(self.delivery)
        duplicate = processor.process(
            {
                **self.delivery,
                "message_id": "101",
                "content": "dune by FRANK HERBERT",
            }
        )
        alias_redelivery = processor.process(
            {
                **self.delivery,
                "message_id": "101",
                "content": "dune by FRANK HERBERT",
            }
        )

        self.assertEqual(duplicate["status"], "queued")
        self.assertEqual(duplicate["request_id"], first["request_id"])
        self.assertIn("exact target is already tracked", duplicate["message"])
        self.assertEqual(alias_redelivery["request_id"], first["request_id"])
        self.assertEqual(dispatcher.call_count, 1)

    def test_completed_exact_target_returns_previous_request(self):
        dispatcher = Mock(return_value=result("queued", "Queued", service="qbittorrent"))
        processor = RequestProcessor(self.store, services={}, dispatcher=dispatcher)
        first = processor.process(self.delivery)
        self.store.transition(first["request_id"], "completed", "Imported")
        duplicate = processor.process({**self.delivery, "message_id": "102"})
        self.assertEqual(duplicate["status"], "completed")
        self.assertIn("Previous request", duplicate["message"])
        self.assertIn("already completed", duplicate["message"])
        self.assertEqual(dispatcher.call_count, 1)

    def test_failed_exact_target_can_be_retried_by_new_message(self):
        dispatcher = Mock(return_value=result("failed", "No provider accepted it"))
        processor = RequestProcessor(self.store, services={}, dispatcher=dispatcher)
        first = processor.process(self.delivery)
        retry = processor.process({**self.delivery, "message_id": "103"})
        self.assertNotEqual(retry["request_id"], first["request_id"])
        self.assertEqual(dispatcher.call_count, 2)

    def test_target_identity_preserves_kind_author_and_edition_tokens(self):
        ebook = request_target_key(
            "ebooks", {"title": "Dune", "author": "Frank Herbert"}
        )
        self.assertEqual(
            ebook,
            request_target_key("ebooks", {"title": "DUNE", "author": "frank herbert"}),
        )
        self.assertNotEqual(
            ebook,
            request_target_key("ebooks", {"title": "DUNE!!!", "author": "frank herbert"}),
        )
        self.assertNotEqual(
            ebook,
            request_target_key("ebooks", {"title": "Dune illustrated", "author": "Frank Herbert"}),
        )
        self.assertNotEqual(
            ebook,
            request_target_key("ebooks", {"title": "Dune", "author": "Brian Herbert"}),
        )
        self.assertNotEqual(
            request_target_key("movies-tv", {"kind": "movie", "title": "The Office"}),
            request_target_key("movies-tv", {"kind": "tv", "title": "The Office"}),
        )
        self.assertNotEqual(
            request_target_key("ebooks", {"title": "雪国"}),
            request_target_key("ebooks", {"title": "人間失格"}),
        )
        self.assertNotEqual(
            request_target_key("ebooks", {"title": "Resume"}),
            request_target_key("ebooks", {"title": "Résumé"}),
        )
        self.assertIn("雪国", request_target_key("ebooks", {"title": "雪国"}))
        self.assertIsNone(request_target_key("ebooks", {"title": "   "}))

    def test_parser_rejection_is_saved_and_actionable(self):
        dispatcher = Mock()
        delivery = {**self.delivery, "message_id": "101", "content": "   "}
        response = RequestProcessor(self.store, services={}, dispatcher=dispatcher).process(delivery)
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("title", response["message"].lower())
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "needs_selection")
        self.assertIsNotNone(saved["error"])
        dispatcher.assert_not_called()

    def test_movie_kind_reaches_dispatcher(self):
        dispatcher = Mock(return_value=result("queued", "Queued in Radarr", service="radarr"))
        delivery = {
            **self.delivery,
            "message_id": "102",
            "media_type": "movies-tv",
            "content": "movie: Arrival",
        }
        RequestProcessor(self.store, services={}, dispatcher=dispatcher).process(delivery)
        self.assertEqual(dispatcher.call_args.args[0]["kind"], "movie")

    def test_service_error_is_caught_and_persisted(self):
        def fail(_request, _services):
            raise ServiceError("Radarr is unavailable.")

        delivery = {**self.delivery, "message_id": "103"}
        response = RequestProcessor(self.store, services={}, dispatcher=fail).process(delivery)
        self.assertEqual(response["status"], "failed")
        self.assertIn("Radarr is unavailable", response["message"])
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], response["message"])

    def test_unexpected_error_is_caught_without_details(self):
        secret = "https://indexer.invalid/download?apikey=do-not-log"

        def fail(_request, _services):
            raise RuntimeError(secret)

        delivery = {**self.delivery, "message_id": "104"}
        response = RequestProcessor(self.store, services={}, dispatcher=fail).process(delivery)
        self.assertEqual(response["status"], "failed")
        self.assertNotIn(secret, response["message"])
        self.assertNotIn(secret, self.store.get_request(response["request_id"])["error"])

    def test_service_error_with_url_or_secret_is_redacted(self):
        secret = "https://service.invalid/search?apikey=do-not-log"

        def fail(_request, _services):
            raise ServiceError(secret)

        with self.assertLogs("huey.orchestrator", level="WARNING") as logs:
            response = RequestProcessor(self.store, services={}, dispatcher=fail).process(
                {**self.delivery, "message_id": "105"}
            )
        self.assertNotIn(secret, response["message"])
        self.assertNotIn(secret, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
