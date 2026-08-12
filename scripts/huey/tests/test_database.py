import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from database import RequestStore


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
            {"updated_at", "service", "external_id", "external_title", "error", "notified_at"}
            <= columns
        )

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

    def test_foreign_key_enforcement(self):
        self.store.initialize()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as connection:
                connection.execute(
                    "INSERT INTO events (request_id,event_type,message) VALUES (999,'x','x')"
                )

    def test_terminal_notification_marker_is_idempotent(self):
        self.store.initialize()
        request, _ = self.store.create_request(**self.request_values())
        self.store.transition(request["id"], "complete", "Imported by BookBot")
        self.assertEqual([row["id"] for row in self.store.pending_notifications()], [request["id"]])
        self.assertTrue(self.store.mark_notified(request["id"], "Discord reply delivered"))
        self.assertFalse(self.store.mark_notified(request["id"], "Duplicate delivery"))
        self.assertEqual(self.store.pending_notifications(), [])
        events = self.store.events_for(request["id"])
        self.assertEqual(
            [event["event_type"] for event in events].count("completion_notified"), 1
        )

    def test_nonterminal_request_cannot_be_marked_completion_notified(self):
        self.store.initialize()
        request, _ = self.store.create_request(**self.request_values())
        self.assertFalse(self.store.mark_notified(request["id"], "not terminal"))


if __name__ == "__main__":
    unittest.main()
