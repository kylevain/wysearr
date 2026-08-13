import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from clients import ServiceError, SubmissionUncertain
from database import RequestStore
from matching import request_target_key
from orchestrator import RequestProcessor
from results import result


def selection_proposal():
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
                "Dune · by Frank Herbert · (1965) · Ebook · Open Library",
                "openlibrary:OL893415W",
                "Frank Herbert",
                1965,
            ),
            (
                "b",
                "Dune · by Brian Herbert · (2005) · Ebook · Open Library",
                "openlibrary:OL2W",
                "Brian Herbert",
                2005,
            ),
        )
    )


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
        self.assertCountEqual(
            [
                delivery["event_key"]
                for delivery in self.store.pending_notification_deliveries()
            ],
            ["request_accepted", "download_queued"],
        )

    def test_shelfarr_request_id_and_initial_status_are_correlated(self):
        dispatcher = Mock(
            return_value=result(
                "queued",
                "Shelfarr accepted Dune",
                service="shelfarr",
                external_id="73",
                external_title="Dune",
                external_status="pending",
            )
        )

        response = RequestProcessor(
            self.store, services=object(), dispatcher=dispatcher
        ).process(self.delivery)

        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["service"], "shelfarr")
        self.assertEqual(saved["external_id"], "73")
        self.assertEqual(saved["external_status"], "pending")

    def test_shelfarr_dispatch_marks_intended_service_before_remote_call(self):
        observed = {}

        class EnabledServices:
            shelfarr_enabled = True

        def dispatcher(request, _services):
            observed.update(self.store.get_request(request["id"]))
            return result(
                "queued",
                "Shelfarr accepted Dune",
                service="shelfarr",
                external_id="73",
                external_title="Dune",
                external_status="pending",
            )

        RequestProcessor(
            self.store, services=EnabledServices(), dispatcher=dispatcher
        ).process(self.delivery)

        self.assertEqual(observed["status"], "processing")
        self.assertEqual(observed["service"], "shelfarr")

    def test_ambiguous_shelfarr_result_persists_active_confirmation(self):
        class EnabledServices:
            shelfarr_enabled = True

        response = RequestProcessor(
            self.store,
            services=EnabledServices(),
            dispatcher=Mock(
                return_value=result(
                    "awaiting_selection",
                    "Choose one candidate.",
                    service="shelfarr",
                    selection_proposal=selection_proposal(),
                )
            ),
            selection_ttl_seconds=900,
        ).process(self.delivery)

        self.assertEqual(response["status"], "awaiting_selection")
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "awaiting_selection")
        self.assertEqual(saved["service"], "shelfarr")
        confirmation = self.store.get_candidate_confirmation(saved["id"])
        self.assertEqual(len(confirmation["options"]), 2)
        self.assertEqual(confirmation["options"][0]["ordinal"], 1)
        duplicate = RequestProcessor(
            self.store,
            services=EnabledServices(),
            dispatcher=Mock(),
        ).process(self.delivery)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["status"], "needs_selection")

    def test_candidate_reply_dispatches_selected_snapshot_once(self):
        class EnabledServices:
            shelfarr_enabled = True

            def __init__(self):
                def selected(_request, _candidate, *, before_create):
                    before_create()
                    return result(
                        "queued",
                        "Shelfarr accepted Dune.",
                        service="shelfarr",
                        external_id="73",
                        external_title="Dune",
                        external_status="pending",
                    )

                self.book_selected = Mock(side_effect=selected)

        services = EnabledServices()
        processor = RequestProcessor(
            self.store,
            services=services,
            dispatcher=Mock(
                return_value=result(
                    "awaiting_selection",
                    "Choose one candidate.",
                    service="shelfarr",
                    selection_proposal=selection_proposal(),
                )
            ),
        )
        intake = processor.process(self.delivery)
        self.assertTrue(
            self.store.bind_candidate_prompt(intake["request_id"], "500")
        )
        selection = {
            "prompt_message_id": "500",
            "message_id": "600",
            "discord_user_id": "1",
            "channel_id": "2",
            "ordinal": 2,
        }

        response = processor.process_candidate_reply(selection)
        duplicate = processor.process_candidate_reply(selection)

        self.assertEqual(response["selection_outcome"], "claimed")
        self.assertEqual(response["status"], "queued")
        self.assertIn("Confirmed. Continuing request.", response["message"])
        self.assertEqual(response["request_id"], intake["request_id"])
        self.assertEqual(duplicate["selection_outcome"], "duplicate")
        services.book_selected.assert_called_once()
        continued_request, selected = services.book_selected.call_args.args
        self.assertEqual(continued_request["id"], intake["request_id"])
        self.assertEqual(selected["work_id"], "openlibrary:OL2W")
        self.assertTrue(callable(services.book_selected.call_args.kwargs["before_create"]))
        self.assertIsNotNone(
            self.store.get_candidate_confirmation(intake["request_id"])[
                "dispatch_started_at"
            ]
        )
        self.assertEqual(self.store.get_request(intake["request_id"])["status"], "queued")
        self.assertCountEqual(
            [
                (delivery["event_key"], delivery["route"])
                for delivery in self.store.pending_notification_deliveries()
            ],
            [
                ("request_accepted", "request-status"),
                ("download_queued", "download-queue"),
            ],
        )

    def test_claimed_candidate_reply_redelivery_without_reference_reuses_canonical_request(self):
        class EnabledServices:
            shelfarr_enabled = True

            def __init__(self):
                def selected(_request, _candidate, *, before_create):
                    before_create()
                    return result(
                        "queued",
                        "Shelfarr accepted Dune.",
                        service="shelfarr",
                        external_id="73",
                        external_title="Dune",
                        external_status="pending",
                    )

                self.book_selected = Mock(side_effect=selected)

        services = EnabledServices()
        dispatcher = Mock(
            return_value=result(
                "awaiting_selection",
                "Choose one candidate.",
                service="shelfarr",
                selection_proposal=selection_proposal(),
            )
        )
        processor = RequestProcessor(
            self.store, services=services, dispatcher=dispatcher
        )
        intake = processor.process(self.delivery)
        self.assertTrue(
            self.store.bind_candidate_prompt(intake["request_id"], "500")
        )
        selection = {
            "prompt_message_id": "500",
            "message_id": "600",
            "discord_user_id": "1",
            "channel_id": "2",
            "ordinal": 2,
        }

        claimed = processor.process_candidate_reply(selection)
        redelivery_without_reference = processor.process(
            {
                **self.delivery,
                "message_id": "600",
                "content": "2",
            }
        )

        self.assertEqual(claimed["selection_outcome"], "claimed")
        self.assertTrue(redelivery_without_reference["duplicate"])
        self.assertEqual(
            redelivery_without_reference["request_id"], intake["request_id"]
        )
        self.assertEqual(
            self.store.get_by_message_id("600")["id"], intake["request_id"]
        )
        self.assertEqual(dispatcher.call_count, 1)
        services.book_selected.assert_called_once()
        with self.store.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
                1,
            )

    def test_invalid_candidate_reply_never_dispatches(self):
        class EnabledServices:
            shelfarr_enabled = True
            book_selected = Mock()

        services = EnabledServices()
        processor = RequestProcessor(
            self.store,
            services=services,
            dispatcher=Mock(
                return_value=result(
                    "awaiting_selection",
                    "Choose one candidate.",
                    service="shelfarr",
                    selection_proposal=selection_proposal(),
                )
            ),
        )
        intake = processor.process(self.delivery)
        self.store.bind_candidate_prompt(intake["request_id"], "500")

        response = processor.process_candidate_reply(
            {
                "prompt_message_id": "500",
                "message_id": "601",
                "discord_user_id": "1",
                "channel_id": "2",
                "ordinal": 99,
            }
        )

        self.assertEqual(response["selection_outcome"], "invalid")
        services.book_selected.assert_not_called()
        self.assertEqual(
            self.store.get_request(intake["request_id"])["status"],
            "awaiting_selection",
        )

    def test_invalid_candidate_reply_redelivery_without_reference_reuses_canonical_request(self):
        class EnabledServices:
            shelfarr_enabled = True
            book_selected = Mock()

        services = EnabledServices()
        dispatcher = Mock(
            return_value=result(
                "awaiting_selection",
                "Choose one candidate.",
                service="shelfarr",
                selection_proposal=selection_proposal(),
            )
        )
        processor = RequestProcessor(
            self.store, services=services, dispatcher=dispatcher
        )
        intake = processor.process(self.delivery)
        self.assertTrue(
            self.store.bind_candidate_prompt(intake["request_id"], "500")
        )
        selection = {
            "prompt_message_id": "500",
            "message_id": "601",
            "discord_user_id": "1",
            "channel_id": "2",
            "ordinal": 99,
        }

        invalid = processor.process_candidate_reply(selection)
        redelivery_without_reference = processor.process(
            {
                **self.delivery,
                "message_id": "601",
                "content": "99",
            }
        )

        self.assertEqual(invalid["selection_outcome"], "invalid")
        self.assertTrue(redelivery_without_reference["duplicate"])
        self.assertEqual(
            redelivery_without_reference["request_id"], intake["request_id"]
        )
        self.assertEqual(
            self.store.get_by_message_id("601")["id"], intake["request_id"]
        )
        self.assertEqual(dispatcher.call_count, 1)
        services.book_selected.assert_not_called()
        with self.store.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
                1,
            )

    def test_stale_candidate_does_not_claim_acquisition_continued(self):
        class EnabledServices:
            shelfarr_enabled = True

            @staticmethod
            def book_selected(_request, _candidate, *, before_create):
                before_create()
                return result(
                    "needs_selection",
                    "Shelfarr's metadata choices changed.",
                    service="shelfarr",
                )

        processor = RequestProcessor(
            self.store,
            services=EnabledServices(),
            dispatcher=Mock(
                return_value=result(
                    "awaiting_selection",
                    "Choose one candidate.",
                    service="shelfarr",
                    selection_proposal=selection_proposal(),
                )
            ),
        )
        intake = processor.process(self.delivery)
        self.store.bind_candidate_prompt(intake["request_id"], "500")

        response = processor.process_candidate_reply(
            {
                "prompt_message_id": "500",
                "message_id": "602",
                "discord_user_id": "1",
                "channel_id": "2",
                "ordinal": 1,
            }
        )

        self.assertEqual(response["status"], "needs_selection")
        self.assertTrue(response["message"].startswith("Selection received, but"))
        self.assertNotIn("Confirmed. Continuing request.", response["message"])

    def test_uncertain_shelfarr_submission_remains_active_for_recovery(self):
        class EnabledServices:
            shelfarr_enabled = True

        def uncertain(_request, _services):
            raise SubmissionUncertain("outcome unknown")

        response = RequestProcessor(
            self.store,
            services=EnabledServices(),
            dispatcher=uncertain,
        ).process(self.delivery)

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["service"], "shelfarr")
        self.assertEqual(response["external_status"], "submission_uncertain")
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["service"], "shelfarr")
        self.assertIsNone(saved["external_id"])
        self.assertEqual(
            self.store.events_for(saved["id"])[-1]["event_type"],
            "shelfarr_submission_uncertain",
        )
        self.assertEqual(
            [row["id"] for row in self.store.uncertain_shelfarr_requests()],
            [saved["id"]],
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
        deliveries = self.store.pending_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["event_key"], "request_rejected")
        self.assertEqual(deliveries[0]["route"], "request-status")
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
