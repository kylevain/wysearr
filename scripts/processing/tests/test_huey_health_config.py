from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bookbot_lib.config import BookBotConfig
from bookbot_lib.errors import ConfigurationError
from bookbot_lib.health import check_health_marker, write_health_marker
from bookbot_lib.huey import HueyUpdater


HASH = "a" * 40


class HueyUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "huey.db"
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    torrent_hash TEXT,
                    library_path TEXT,
                    error_message TEXT,
                    updated_at TEXT
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    request_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                INSERT INTO requests (id, status, torrent_hash)
                VALUES (42, 'downloading', NULL), (43, 'downloading', 'bbbb');
                """
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_completion_by_huey_tag_updates_status_and_event(self) -> None:
        updater = HueyUpdater(self.database)
        destination = Path("/media/ebooks/Books/Book")
        self.assertTrue(updater.complete(HASH, destination, "other, huey-42"))
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            request = connection.execute(
                "SELECT status, library_path, error_message FROM requests WHERE id=42"
            ).fetchone()
            event = connection.execute(
                "SELECT event_type, message FROM events WHERE request_id=42"
            ).fetchone()
        self.assertEqual(("complete", str(destination), None), request)
        self.assertEqual("completed", event[0])
        self.assertIn(str(destination), event[1])

    def test_failure_by_huey_tag_records_error_and_event(self) -> None:
        updater = HueyUpdater(self.database)
        self.assertTrue(updater.failed(HASH, "unsupported payload", "huey:42"))
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            request = connection.execute(
                "SELECT status, error_message FROM requests WHERE id=42"
            ).fetchone()
            event = connection.execute(
                "SELECT event_type, message FROM events WHERE request_id=42"
            ).fetchone()
        self.assertEqual(("failed", "unsupported payload"), request)
        self.assertEqual("failed", event[0])
        self.assertIn("unsupported payload", event[1])

    def test_missing_or_incompatible_database_is_non_blocking(self) -> None:
        self.assertFalse(HueyUpdater(None).complete(HASH, Path("/media/book")))
        incompatible = Path(self.temporary.name) / "incompatible.db"
        sqlite3.connect(incompatible).close()
        self.assertFalse(
            HueyUpdater(incompatible).failed(HASH, "failed", "huey-42")
        )


class HealthTests(unittest.TestCase):
    def test_marker_is_atomic_parseable_and_age_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            write_health_marker(path, "ok", counts={"imported": 2}, now=100)
            self.assertEqual(2, json.loads(path.read_text())["counts"]["imported"])
            self.assertEqual((True, "BookBot healthy"), check_health_marker(path, 30, 120))
            healthy, message = check_health_marker(path, 30, 131)
            self.assertFalse(healthy)
            self.assertIn("stale", message)

    def test_error_marker_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            write_health_marker(path, "error", message="API unavailable", now=100)
            healthy, message = check_health_marker(path, 30, 100)
            self.assertFalse(healthy)
            self.assertIn("error", message)


class ConfigurationTests(unittest.TestCase):
    def base_env(self, root: Path) -> dict[str, str]:
        downloads = root / "downloads"
        media = root / "media"
        config = root / "config"
        downloads.mkdir()
        media.mkdir()
        config.mkdir()
        return {
            "TORRENT_ROOT": str(downloads),
            "MEDIA_ROOT": str(media),
            "BOOKBOT_DB_PATH": str(config / "bookbot.db"),
            "BOOKBOT_HEALTH_PATH": str(config / "health.json"),
            "QBITTORRENT_URL": "http://qbittorrent:8080",
            "QBITTORRENT_USERNAME": "operator",
            "QBITTORRENT_PASSWORD": "secret",
            "HUEY_DB": str(config / "huey.db"),
        }

    def test_valid_environment_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = self.base_env(Path(temporary))
            config = BookBotConfig.from_env(env)
            config.validate_filesystem()
            self.assertEqual(14, config.retention_days)
            self.assertEqual(60, config.poll_seconds)
            self.assertTrue(config.verify_tls)
            self.assertEqual(Path(env["HUEY_DB"]), config.huey_database_path)

    def test_credentials_in_url_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = self.base_env(Path(temporary))
            env["QBITTORRENT_URL"] = "http://user:password@qbittorrent:8080"
            with self.assertRaises(ConfigurationError):
                BookBotConfig.from_env(env)

    def test_missing_cookie_auth_credentials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = self.base_env(Path(temporary))
            env["QBITTORRENT_PASSWORD"] = ""
            with self.assertRaises(ConfigurationError):
                BookBotConfig.from_env(env)

    def test_nested_media_and_torrent_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = self.base_env(root)
            nested = Path(env["TORRENT_ROOT"]) / "media"
            nested.mkdir()
            env["MEDIA_ROOT"] = str(nested)
            with self.assertRaises(ConfigurationError):
                BookBotConfig.from_env(env).validate_filesystem()


if __name__ == "__main__":
    unittest.main()
