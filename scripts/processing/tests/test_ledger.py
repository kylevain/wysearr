from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bookbot_lib.ledger import ImportLedger


HASH = "a" * 40


def torrent() -> dict[str, object]:
    return {
        "hash": HASH,
        "category": "ebooks",
        "name": "Book",
        "content_path": "/downloads/ebooks/Book.epub",
    }


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "bookbot.db"
        self.ledger = ImportLedger(
            self.path, retry_base_seconds=10, retry_max_seconds=25
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_enables_foreign_keys_and_wal_per_connection(self) -> None:
        with self.ledger._connect() as connection:
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_retry_backoff_and_attempt_counter_are_durable(self) -> None:
        self.ledger.begin_attempt(torrent(), "ebooks-imported", now=100)
        self.ledger.mark_retry(HASH, "temporary failure", now=100)
        row = self.ledger.get(HASH)
        assert row is not None
        self.assertEqual("retry", row["status"])
        self.assertEqual(1, row["attempts"])
        self.assertEqual(110, row["next_retry_at"])
        self.assertFalse(self.ledger.should_copy(HASH, max_retries=3, now=109))
        self.assertTrue(self.ledger.should_copy(HASH, max_retries=3, now=110))

        self.ledger.begin_attempt(torrent(), "ebooks-imported", now=110)
        self.ledger.mark_retry(HASH, "again", now=110)
        row = self.ledger.get(HASH)
        assert row is not None
        self.assertEqual(2, row["attempts"])
        self.assertEqual(130, row["next_retry_at"])

    def test_retry_delay_is_capped(self) -> None:
        for attempt in range(1, 5):
            self.ledger.begin_attempt(torrent(), "ebooks-imported", now=attempt * 100)
            self.ledger.mark_retry(HASH, "failure", now=attempt * 100)
        row = self.ledger.get(HASH)
        assert row is not None
        self.assertEqual(425, row["next_retry_at"])

    def test_import_and_recent_addition_are_idempotent(self) -> None:
        destination = Path("/media/ebooks/Books/Book")
        self.ledger.begin_attempt(torrent(), "ebooks-imported", now=100)
        self.ledger.mark_copied(HASH, destination, now=101)
        self.ledger.mark_imported(
            HASH, "ebooks", "Book", destination, now=102
        )
        self.ledger.mark_imported(
            HASH, "ebooks", "Book", destination, now=103
        )
        row = self.ledger.get(HASH)
        assert row is not None
        self.assertEqual("imported", row["status"])
        self.assertEqual(103, row["imported_at"])
        with self.ledger._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM recent_additions WHERE torrent_hash = ?",
                (HASH,),
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_retention_eligibility_requires_imported_status_and_age(self) -> None:
        destination = Path("/media/ebooks/Books/Book")
        self.ledger.begin_attempt(torrent(), "ebooks-imported", now=100)
        self.ledger.mark_copied(HASH, destination, now=101)
        self.assertIsNone(self.ledger.eligible_for_deletion(HASH, cutoff=1000))
        self.ledger.mark_imported(
            HASH, "ebooks", "Book", destination, now=200
        )
        self.assertIsNone(self.ledger.eligible_for_deletion(HASH, cutoff=199))
        self.assertIsNotNone(self.ledger.eligible_for_deletion(HASH, cutoff=200))
        self.ledger.mark_deleted(HASH, now=300)
        self.assertIsNone(self.ledger.eligible_for_deletion(HASH, cutoff=1000))

    def test_rejected_and_failed_states_are_terminal(self) -> None:
        self.ledger.begin_attempt(torrent(), "ebooks-imported", now=100)
        self.ledger.mark_rejected(HASH, "unsupported", now=101)
        self.assertFalse(self.ledger.should_copy(HASH, max_retries=10, now=1000))

        other_hash = "b" * 40
        other = {**torrent(), "hash": other_hash}
        self.ledger.begin_attempt(other, "ebooks-imported", now=100)
        self.ledger.mark_failed(other_hash, "exhausted", now=101)
        self.assertFalse(
            self.ledger.should_copy(other_hash, max_retries=10, now=1000)
        )

    def test_category_retry_does_not_make_payload_copyable(self) -> None:
        destination = Path("/media/ebooks/Books/Book")
        self.ledger.begin_attempt(torrent(), "ebooks-imported", now=100)
        self.ledger.mark_copied(HASH, destination, now=101)
        self.ledger.mark_category_retry(HASH, "api unavailable", now=101)
        self.assertFalse(self.ledger.should_finalize(HASH, 3, now=110))
        self.assertTrue(self.ledger.should_finalize(HASH, 3, now=111))
        self.assertFalse(self.ledger.should_copy(HASH, 3, now=1000))

    def test_arr_retention_uses_persisted_first_observed_age(self) -> None:
        self.ledger.observe_arr_imported(HASH, "tv-imported", now=100)
        self.ledger.observe_arr_imported(HASH, "tv-imported", now=200)
        self.assertIsNone(
            self.ledger.arr_eligible_for_deletion(HASH, "tv-imported", 99)
        )
        row = self.ledger.arr_eligible_for_deletion(HASH, "tv-imported", 100)
        assert row is not None
        self.assertEqual(100, row["first_observed_at"])
        self.assertEqual(200, row["last_observed_at"])
        self.assertIsNone(
            self.ledger.arr_eligible_for_deletion(HASH, "movies-imported", 1000)
        )
        self.ledger.mark_arr_deleted(HASH, now=300)
        self.assertIsNone(
            self.ledger.arr_eligible_for_deletion(HASH, "tv-imported", 1000)
        )


if __name__ == "__main__":
    unittest.main()
