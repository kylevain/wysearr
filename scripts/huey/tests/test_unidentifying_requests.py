import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from database import RequestStore
from matching import (
    MINIMUM_IDENTIFYING_TITLE,
    identifies_a_work,
    request_target_key,
)
from orchestrator import RequestProcessor
from results import result


class IdentifyingTitleTests(unittest.TestCase):
    def test_bare_format_tokens_identify_nothing(self):
        for token in (
            "m4b", "M4B", "mp3", "epub", "mobi", "azw3", "azw", "pdf",
            "flac", "aac", "cbz", "cbr", "m4a", "opus",
        ):
            with self.subTest(token=token):
                self.assertFalse(identifies_a_work(token))
                self.assertIsNone(
                    request_target_key("audiobooks", {"title": token, "author": None})
                )

    def test_several_format_tokens_together_still_identify_nothing(self):
        self.assertFalse(identifies_a_work("epub mobi"))
        self.assertFalse(identifies_a_work("MP3 m4b"))
        self.assertIsNone(
            request_target_key("ebooks", {"title": "epub mobi", "author": None})
        )
        # Known limit: a connecting word is not a format token, so "epub or
        # mobi" still keys. The bare-token reply is the shape that caused #126.
        self.assertTrue(identifies_a_work("epub or mobi"))

    def test_an_author_does_not_rescue_a_format_token(self):
        # "m4b by John Green" must not become a keyed target either.
        self.assertFalse(identifies_a_work("m4b", "John Green"))
        self.assertIsNone(
            request_target_key(
                "audiobooks", {"title": "m4b", "author": "John Green"}
            )
        )

    def test_genuine_short_titles_survive(self):
        for title in ("It", "Us", "Up", "1984", "Dune", "Heat"):
            with self.subTest(title=title):
                self.assertTrue(identifies_a_work(title))
                self.assertIsNotNone(
                    request_target_key("ebooks", {"title": title, "author": None})
                )

    def test_a_format_token_inside_a_real_title_is_kept(self):
        self.assertTrue(identifies_a_work("Dune m4b"))
        self.assertIsNotNone(
            request_target_key("audiobooks", {"title": "Dune m4b", "author": None})
        )

    def test_single_characters_identify_nothing(self):
        self.assertEqual(MINIMUM_IDENTIFYING_TITLE, 2)
        for title in ("a", "?", "1", "-"):
            with self.subTest(title=title):
                self.assertFalse(identifies_a_work(title))

    def test_two_format_replies_no_longer_share_a_target(self):
        first = request_target_key("audiobooks", {"title": "m4b", "author": None})
        second = request_target_key("audiobooks", {"title": "m4b", "author": None})
        # Both unkeyed, so create_request cannot collapse one onto the other.
        self.assertIsNone(first)
        self.assertIsNone(second)


class UnidentifyingRequestDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.temporary.name) / "huey.db")
        self.store.initialize()
        self.dispatcher = Mock(
            return_value=result(
                "queued", "Queued", service="qbittorrent", external_id="guid-1"
            )
        )
        self.processor = RequestProcessor(
            self.store, services={"direct": object()}, dispatcher=self.dispatcher
        )
        self.delivery = {
            "discord_user_id": "1",
            "discord_username": "kyle",
            "channel_id": "2",
            "message_id": "200",
            "media_type": "audiobooks",
            "content": "m4b",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_format_token_never_reaches_an_acquisition_service(self):
        response = self.processor.process(self.delivery)

        self.dispatcher.assert_not_called()
        self.assertEqual(response["status"], "needs_selection")
        self.assertIn("That is a format, not a title", response["message"])
        self.assertIn("Reply action", response["message"])

        saved = self.store.get_request(response["request_id"])
        self.assertEqual(saved["status"], "needs_selection")
        self.assertIsNone(saved["target_key"])
        self.assertIsNone(saved["service"])
        self.assertIsNone(saved["external_id"])
        self.assertEqual(saved["raw_request"], "m4b")
        self.assertEqual(
            [event["event_type"] for event in self.store.events_for(saved["id"])],
            ["received", "unidentifying_request_rejected"],
        )

    def test_second_format_reply_is_a_separate_inert_row(self):
        first = self.processor.process(self.delivery)
        second = self.processor.process({**self.delivery, "message_id": "201"})

        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertFalse(second["duplicate"])
        self.assertEqual(second["status"], "needs_selection")
        # The #126 failure mode: neither row owns a target the other collapses onto.
        with self.store.connect() as connection:
            aliases = connection.execute(
                "SELECT COUNT(*) FROM delivery_aliases"
            ).fetchone()[0]
        self.assertEqual(aliases, 0)
        self.dispatcher.assert_not_called()

    def test_a_real_title_still_dispatches(self):
        response = self.processor.process(
            {**self.delivery, "message_id": "202", "content": "Dune by Frank Herbert"}
        )
        self.dispatcher.assert_called_once()
        self.assertEqual(response["status"], "queued")
        saved = self.store.get_request(response["request_id"])
        self.assertIsNotNone(saved["target_key"])

    def test_short_genuine_title_still_dispatches(self):
        response = self.processor.process(
            {**self.delivery, "message_id": "203", "content": "It"}
        )
        self.dispatcher.assert_called_once()
        self.assertEqual(response["status"], "queued")

    def test_movies_tv_format_token_is_rejected_before_radarr(self):
        response = self.processor.process(
            {
                **self.delivery,
                "message_id": "204",
                "media_type": "movies-tv",
                "content": "movie: m4b",
            }
        )
        self.dispatcher.assert_not_called()
        self.assertEqual(response["status"], "needs_selection")


if __name__ == "__main__":
    unittest.main()
