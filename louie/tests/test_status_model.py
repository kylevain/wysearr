import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


LOUIE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LOUIE_ROOT))

import server


SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY,
    media_type TEXT,
    status TEXT,
    service TEXT,
    external_id TEXT,
    external_title TEXT,
    external_status TEXT,
    error TEXT,
    title TEXT,
    raw_request TEXT,
    discord_username TEXT,
    discord_user_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    dispatch_started_at TEXT
);
CREATE TABLE trusted_library_events (id INTEGER PRIMARY KEY, state TEXT);
"""


class DeclineStatusTests(unittest.TestCase):
    """Huey now says why it declined; Louie must stop merging the reasons."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "huey.db"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)
            db.commit()
        self.original = server.HUEY_DB
        server.HUEY_DB = self.path

    def tearDown(self):
        server.HUEY_DB = self.original
        self.temporary.cleanup()

    def add(self, request_id, *, external_status=None, error=None):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO requests (id, media_type, status, service, "
                "external_status, error, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    "audiobooks",
                    "needs_selection",
                    "abba",
                    external_status,
                    error,
                    "Leaders Eat Last",
                ),
            )
            db.commit()

    def statuses(self):
        return {int(item["request_id"]): item["status"] for item in server.huey_items()}

    def test_nothing_found_is_not_a_clarification_problem(self):
        # There is nothing for the requester to clarify, so it does not belong
        # in the same column as the two reasons a reply can resolve.
        self.add(1, external_status="selection_no_results")

        self.assertEqual(self.statuses()[1], "no_results")

    def test_weak_and_indistinguishable_matches_remain_clarifiable(self):
        self.add(2, external_status="selection_low_confidence")
        self.add(3, external_status="selection_ambiguous")

        self.assertEqual(self.statuses(), {2: "ambiguous", 3: "ambiguous"})

    def test_the_reason_reaches_the_card(self):
        self.add(4, external_status="selection_low_confidence", error="none close enough")

        item = next(item for item in server.huey_items() if item["request_id"] == "4")
        self.assertEqual(item["error"], "none close enough")

    def test_a_parse_failure_is_still_unparsed(self):
        self.add(5, error="Start the request with `movie:` or `tv:`.")

        self.assertEqual(self.statuses()[5], "unparsed")

    def test_an_unmarked_decline_keeps_the_old_bucket(self):
        self.add(6)

        self.assertEqual(self.statuses()[6], "ambiguous")

    def test_every_mapped_status_has_a_column(self):
        self.add(7, external_status="selection_no_results")

        for status in self.statuses().values():
            self.assertIn(status, server.STATUS_ORDER)


class BoardVocabularyTests(unittest.TestCase):
    def test_the_board_renders_every_status_the_server_can_emit(self):
        # The column list lives in the page as well as the server. A status the
        # board does not know about silently disappears from the dashboard.
        page = (LOUIE_ROOT / "static" / "index.html").read_text()
        for status in server.STATUS_ORDER:
            self.assertIn(f"'{status}'", page)
            self.assertIn(f"{status}:'", page)


if __name__ == "__main__":
    unittest.main()
