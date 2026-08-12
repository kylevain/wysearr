import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from database import RequestStore
from matching import request_target_key


DUNE_TARGET = request_target_key("ebooks", {"title": "Dune", "author": "Frank Herbert"})
DUNE_NO_AUTHOR_TARGET = request_target_key("ebooks", {"title": "Dune", "author": None})
DUNE_EDITION_TARGET = request_target_key(
    "ebooks", {"title": "Dune illustrated", "author": "Frank Herbert"}
)


OLD_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    discord_user_id TEXT NOT NULL,
    discord_username TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    raw_request TEXT NOT NULL,
    title TEXT,
    author TEXT,
    status TEXT NOT NULL DEFAULT 'new'
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES requests(id)
);
"""


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "nested" / "huey.db"
        self.store = RequestStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def request_values(message_id="100"):
        return {
            "discord_user_id": "1",
            "discord_username": "reader",
            "channel_id": "2",
            "message_id": message_id,
            "media_type": "ebooks",
            "raw_request": "Dune by Frank Herbert",
            "title": "Dune",
            "author": "Frank Herbert",
        }

    def test_initialize_creates_parent_and_is_idempotent(self):
        self.store.initialize()
        self.store.initialize()
        columns = set()
        with self.store.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(requests)")}
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertTrue(
            {
                "updated_at",
                "service",
                "external_id",
                "external_title",
                "error",
                "notified_at",
                "target_key",
            }
            <= columns
        )
        with self.store.connect() as connection:
            aliases = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_aliases'"
            ).fetchone()
            outbox = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_deliveries'"
            ).fetchone()
            target_index = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='requests_active_target_uq'"
            ).fetchone()
        self.assertIsNotNone(aliases)
        self.assertIsNotNone(outbox)
        self.assertIsNotNone(target_index)

    def test_old_schema_migration_merges_duplicates_and_preserves_events(self):
        self.path.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.path)
        connection.executescript(OLD_SCHEMA)
        values = ("1", "reader", "2", "same", "ebooks", "Dune", "Dune", None)
        connection.execute(
            "INSERT INTO requests (discord_user_id,discord_username,channel_id,message_id,media_type,raw_request,title,author) VALUES (?,?,?,?,?,?,?,?)",
            values,
        )
        first_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "INSERT INTO requests (discord_user_id,discord_username,channel_id,message_id,media_type,raw_request,title,author) VALUES (?,?,?,?,?,?,?,?)",
            values,
        )
        second_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "UPDATE requests SET status = 'queued' WHERE id = ?", (second_id,)
        )
        connection.execute(
            "INSERT INTO events (request_id,event_type,message) VALUES (?,?,?)",
            (second_id, "received", "duplicate event"),
        )
        connection.commit()
        connection.close()

        self.store.initialize()
        with self.store.connect() as migrated:
            rows = migrated.execute("SELECT * FROM requests WHERE message_id='same'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], first_id)
            self.assertEqual(rows[0]["status"], "queued")
            self.assertEqual(rows[0]["target_key"], DUNE_NO_AUTHOR_TARGET)
            event_request_ids = {
                row[0] for row in migrated.execute("SELECT request_id FROM events").fetchall()
            }
            self.assertEqual(event_request_ids, {first_id})
            with self.assertRaises(sqlite3.IntegrityError):
                migrated.execute(
                    "INSERT INTO requests (discord_user_id,discord_username,channel_id,message_id,media_type,raw_request) VALUES ('1','u','2','same','ebooks','x')"
                )

    def test_create_deduplicates_and_transition_records_event(self):
        self.store.initialize()
        first, created = self.store.create_request(**self.request_values())
        duplicate, duplicate_created = self.store.create_request(**self.request_values())
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["id"], duplicate["id"])

        updated = self.store.transition(
            first["id"],
            "queued",
            "Queued in qBittorrent",
            service="qbittorrent",
            external_id="guid-1",
            external_title="Dune EPUB",
        )
        self.assertEqual(updated["status"], "queued")
        self.assertEqual(updated["service"], "qbittorrent")
        event_types = [event["event_type"] for event in self.store.events_for(first["id"])]
        self.assertEqual(event_types, ["received", "duplicate_delivery", "queued"])

    def test_exact_active_target_reserves_one_canonical_request_and_alias(self):
        self.store.initialize()
        target_key = DUNE_TARGET
        first, created = self.store.create_request(
            **self.request_values("100"), target_key=target_key
        )
        self.store.transition(first["id"], "queued", "Queued")
        second, second_created = self.store.create_request(
            **self.request_values("101"), target_key=target_key
        )
        redelivery, redelivery_created = self.store.create_request(
            **self.request_values("101"), target_key=target_key
        )

        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertFalse(redelivery_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["id"], redelivery["id"])
        self.assertEqual(self.store.get_by_message_id("101")["id"], first["id"])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM delivery_aliases").fetchone()[0], 1
            )
        event_types = [event["event_type"] for event in self.store.events_for(first["id"])]
        self.assertEqual(event_types.count("duplicate_target"), 1)
        self.assertEqual(event_types.count("duplicate_delivery"), 1)

    def test_simultaneous_exact_target_reservation_dispatches_one_canonical(self):
        self.store.initialize()
        barrier = Barrier(2)

        def reserve(message_id):
            barrier.wait()
            return self.store.create_request(
                **self.request_values(message_id),
                target_key=DUNE_TARGET,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, ("100", "101")))
        records = [record for record, _created in results]
        self.assertEqual(sum(created for _record, created in results), 1)
        self.assertEqual({record["id"] for record in records}, {records[0]["id"]})
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM delivery_aliases").fetchone()[0], 1
            )

    def test_failed_or_needs_selection_target_can_be_retried_by_new_message(self):
        for status in ("failed", "needs_selection"):
            with self.subTest(status=status):
                self.temporary.cleanup()
                self.temporary = tempfile.TemporaryDirectory()
                self.path = Path(self.temporary.name) / "huey.db"
                self.store = RequestStore(self.path)
                self.store.initialize()
                first, _ = self.store.create_request(
                    **self.request_values("100"), target_key=DUNE_TARGET
                )
                self.store.transition(first["id"], status, "Retry allowed")
                retry, created = self.store.create_request(
                    **self.request_values("101"), target_key=DUNE_TARGET
                )
                self.assertTrue(created)
                self.assertNotEqual(retry["id"], first["id"])

    def test_completed_exact_target_is_reused_but_distinct_key_is_not(self):
        self.store.initialize()
        first, _ = self.store.create_request(
            **self.request_values("100"), target_key=DUNE_TARGET
        )
        self.store.transition(first["id"], "completed", "Imported")
        duplicate, created = self.store.create_request(
            **self.request_values("101"), target_key=DUNE_TARGET
        )
        edition, edition_created = self.store.create_request(
            **self.request_values("102"),
            target_key=DUNE_EDITION_TARGET,
        )
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertTrue(edition_created)
        self.assertNotEqual(edition["id"], first["id"])

    def test_initialize_backfills_completed_legacy_target_without_replaying_it(self):
        self.store.initialize()
        legacy, _ = self.store.create_request(**self.request_values("legacy"))
        self.store.transition(legacy["id"], "completed", "Imported")
        self.assertIsNone(self.store.get_request(legacy["id"])["target_key"])

        self.store.initialize()
        migrated = self.store.get_request(legacy["id"])
        self.assertEqual(
            migrated["target_key"], DUNE_TARGET
        )
        duplicate, created = self.store.create_request(
            **self.request_values("new-message"),
            target_key=DUNE_TARGET,
        )
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], legacy["id"])

    def test_foreign_key_enforcement(self):
        self.store.initialize()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "INSERT INTO events (request_id,event_type,message) VALUES (999,'x','x')"
                )

    def test_notification_outbox_is_idempotent_per_event_and_route(self):
        self.store.initialize()
        request, _ = self.store.create_request(**self.request_values())
        self.assertTrue(
            self.store.enqueue_notification(
                request["id"],
                "request_completed",
                "request-status",
                "Request complete",
            )
        )
        self.assertFalse(
            self.store.enqueue_notification(
                request["id"],
                "request_completed",
                "request-status",
                "Changed duplicate message must not replace the first",
            )
        )
        self.assertTrue(
            self.store.enqueue_notification(
                request["id"],
                "library_imported",
                "recent-additions",
                "New library item",
            )
        )

        pending = self.store.pending_notification_deliveries()
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["message"], "Request complete")
        self.assertFalse(
            self.store.notification_delivered(
                request["id"], "request_completed", "request-status"
            )
        )
        self.assertTrue(self.store.mark_notification_delivered(pending[0]["id"]))
        self.assertFalse(self.store.mark_notification_delivered(pending[0]["id"]))
        self.assertTrue(
            self.store.notification_delivered(
                request["id"], "request_completed", "request-status"
            )
        )
        self.assertEqual(
            [row["event_key"] for row in self.store.pending_notification_deliveries()],
            ["library_imported"],
        )

    def test_terminal_request_is_not_notified_until_every_staged_route_succeeds(self):
        self.store.initialize()
        request, _ = self.store.create_request(**self.request_values())
        self.store.transition(
            request["id"],
            "complete",
            "Imported by BookBot",
            event_type="completed",
            service="bookbot",
        )
        self.store.enqueue_notification(
            request["id"], "request_completed", "request-status", "Request complete"
        )
        self.store.enqueue_notification(
            request["id"], "library_imported", "recent-additions", "New library item"
        )
        deliveries = self.store.pending_notification_deliveries()

        self.assertFalse(
            self.store.mark_notified_if_delivered(request["id"], "not complete")
        )
        self.assertTrue(self.store.mark_notification_delivered(deliveries[0]["id"]))
        self.assertFalse(
            self.store.mark_notified_if_delivered(request["id"], "still partial")
        )
        self.assertIsNone(self.store.get_request(request["id"])["notified_at"])

        self.assertTrue(self.store.mark_notification_delivered(deliveries[1]["id"]))
        self.assertTrue(
            self.store.mark_notified_if_delivered(request["id"], "all routes delivered")
        )
        self.assertFalse(
            self.store.mark_notified_if_delivered(request["id"], "duplicate marker")
        )
        self.assertEqual(self.store.pending_notifications(), [])
        events = self.store.events_for(request["id"])
        self.assertEqual(
            [event["event_type"] for event in events].count("completion_notified"), 1
        )

    def test_nonterminal_request_cannot_be_marked_notified_from_delivered_outbox(self):
        self.store.initialize()
        request, _ = self.store.create_request(**self.request_values())
        self.store.enqueue_notification(
            request["id"], "request_completed", "request-status", "invalid early result"
        )
        delivery = self.store.pending_notification_deliveries()[0]
        self.assertTrue(self.store.mark_notification_delivered(delivery["id"]))
        self.assertFalse(
            self.store.mark_notified_if_delivered(request["id"], "not terminal")
        )
        self.assertIsNone(self.store.get_request(request["id"])["notified_at"])

    def test_queued_arr_requests_exclude_direct_and_terminal_requests(self):
        self.store.initialize()
        queued_arr, _ = self.store.create_request(**self.request_values("arr"))
        direct, _ = self.store.create_request(**self.request_values("direct"))
        terminal, _ = self.store.create_request(**self.request_values("terminal"))
        self.store.transition(
            queued_arr["id"],
            "queued",
            "Queued in Radarr",
            service="radarr",
            external_id="44",
        )
        self.store.transition(
            direct["id"],
            "queued",
            "Queued in qBittorrent",
            service="qbittorrent",
            external_id="hash",
        )
        self.store.transition(
            terminal["id"],
            "completed",
            "Already imported",
            service="sonarr",
            external_id="33",
        )

        rows = self.store.queued_arr_requests()
        self.assertEqual([row["id"] for row in rows], [queued_arr["id"]])

    def test_arr_completion_transition_is_atomic_and_idempotent(self):
        self.store.initialize()
        request, _ = self.store.create_request(**self.request_values())
        self.store.transition(
            request["id"],
            "queued",
            "Queued in Lidarr",
            service="lidarr",
            external_id="55",
            external_title="Massive Attack",
        )

        self.assertTrue(
            self.store.mark_arr_completed(request["id"], "Lidarr reports imported media")
        )
        self.assertFalse(
            self.store.mark_arr_completed(request["id"], "Duplicate completion")
        )
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["external_id"], "55")
        self.assertEqual(saved["external_title"], "Massive Attack")
        events = self.store.events_for(request["id"])
        self.assertEqual(
            [event["event_type"] for event in events].count("arr_completed"), 1
        )

    def test_initialize_fails_interrupted_request_for_safe_reconciliation(self):
        self.store.initialize()
        request, _ = self.store.create_request(**self.request_values())
        self.store.initialize()
        reconciled = self.store.get_request(request["id"])
        self.assertEqual(reconciled["status"], "failed")
        self.assertIn("review acquisition services", reconciled["error"])
        self.assertEqual(
            self.store.events_for(request["id"])[-1]["event_type"],
            "startup_reconciled",
        )


if __name__ == "__main__":
    unittest.main()
