from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bookbot_lib.config import BookBotConfig
from bookbot_lib.errors import ConfigurationError, MetadataCorrelationError
from bookbot_lib.health import check_health_marker, write_health_marker
from bookbot_lib.huey import HueyUpdater


HASH = "a" * 40


class HealthcheckImportTests(unittest.TestCase):
    def test_health_module_does_not_load_worker_or_http_stack(self) -> None:
        processing_root = Path(__file__).resolve().parents[1]
        probe = (
            "import sys; import bookbot_lib.health; "
            "blocked = {'bookbot_lib.service', 'requests'} & set(sys.modules); "
            "sys.exit(','.join(sorted(blocked)) if blocked else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=processing_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)


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
                    media_type TEXT,
                    service TEXT,
                    title TEXT,
                    author TEXT,
                    torrent_hash TEXT,
                    external_id TEXT,
                    external_status TEXT,
                    canonical_request_id INTEGER,
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
                """
            )
            connection.executemany(
                "INSERT INTO requests (id, status, torrent_hash, external_id) VALUES (?, ?, ?, ?)",
                (
                    (42, "downloading", None, HASH),
                    (43, "downloading", "bbbb", "bbbb"),
                ),
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

    def test_lazylibrarian_ebook_completion_requires_exact_ebook_lane(self) -> None:
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                """
                UPDATE requests
                SET status='queued', media_type='ebooks',
                    service='lazylibrarian', external_id=?
                WHERE id=42
                """,
                (HASH,),
            )

        updater = HueyUpdater(self.database)
        self.assertFalse(
            updater.complete(
                HASH,
                Path("/media/ebooks/Comics/Book"),
                "huey-42",
                source_category="manga-comics",
            )
        )
        self.assertFalse(
            updater.complete(
                HASH,
                Path("/media/ebooks/Books/Book"),
                "huey-42",
            )
        )
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            status = connection.execute(
                "SELECT status FROM requests WHERE id=42"
            ).fetchone()[0]
        self.assertEqual("queued", status)

        self.assertTrue(
            updater.complete(
                HASH,
                Path("/media/ebooks/Books/Book"),
                "huey-42",
                source_category="ebooks",
            )
        )

    def test_failure_by_huey_tag_records_error_and_event(self) -> None:
        updater = HueyUpdater(self.database)
        self.assertTrue(updater.failed(HASH, "unsupported payload", "huey-42"))
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

    def test_completion_updates_only_the_exact_tagged_hash_owner(self) -> None:
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                "INSERT INTO requests (id, status, torrent_hash, external_id) VALUES (44, 'downloading', ?, ?)",
                (HASH, HASH),
            )
            connection.execute(
                "INSERT INTO requests (id, status, torrent_hash, external_id) VALUES (45, 'downloading', NULL, ?)",
                (HASH,),
            )
        destination = Path("/media/ebooks/Books/Shared")
        self.assertTrue(
            HueyUpdater(self.database).complete(
                HASH, destination, "huey-42"
            )
        )
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            rows = connection.execute(
                "SELECT id, status FROM requests WHERE id IN (42,44,45) ORDER BY id"
            ).fetchall()
            event_ids = connection.execute(
                "SELECT request_id FROM events WHERE event_type='completed' ORDER BY request_id"
            ).fetchall()
        self.assertEqual(
            rows,
            [(42, "complete"), (44, "downloading"), (45, "downloading")],
        )
        self.assertEqual(event_ids, [(42,)])

    def test_mismatched_tag_and_terminal_request_are_not_overwritten(self) -> None:
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                "INSERT INTO requests (id, status, torrent_hash, external_id) VALUES (46, 'queued', NULL, ?)",
                ("b" * 40,),
            )
            connection.execute(
                "INSERT INTO requests (id, status, torrent_hash, external_id) VALUES (47, 'failed', ?, ?)",
                (HASH, HASH),
            )
        self.assertFalse(
            HueyUpdater(self.database).complete(
                HASH, Path("/media/ebooks/Books/Shared"), "huey-46,huey-47"
            )
        )
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            rows = connection.execute(
                "SELECT id, status FROM requests WHERE id IN (46,47) ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, [(46, "queued"), (47, "failed")])

    def test_missing_or_incompatible_database_is_non_blocking(self) -> None:
        self.assertFalse(HueyUpdater(None).complete(HASH, Path("/media/book")))
        self.assertIsNone(
            HueyUpdater(None).abba_audiobook_metadata(HASH, "huey-42")
        )
        incompatible = Path(self.temporary.name) / "incompatible.db"
        sqlite3.connect(incompatible).close()
        self.assertFalse(
            HueyUpdater(incompatible).failed(HASH, "failed", "huey-42")
        )
        self.assertIsNone(
            HueyUpdater(incompatible).abba_audiobook_metadata(HASH, "huey-42")
        )

    def test_abba_metadata_requires_exact_huey_tag_and_hash_binding(self) -> None:
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                """
                UPDATE requests
                SET media_type='audiobooks', service='abba',
                    title=?, author=?, status='queued', external_id=?,
                    external_status='downloading'
                WHERE id=42
                """,
                ("Tourist Season", "Brynne Weaver", HASH),
            )

        updater = HueyUpdater(self.database)
        metadata = updater.abba_audiobook_metadata(HASH, "other,huey-42")
        assert metadata is not None
        self.assertEqual("Tourist Season", metadata.title)
        self.assertEqual("Brynne Weaver", metadata.author)
        self.assertIsNone(updater.abba_audiobook_metadata(HASH, "huey-43"))
        with self.assertRaises(MetadataCorrelationError):
            updater.abba_audiobook_metadata("b" * 40, "huey-42")

        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute("UPDATE requests SET author=NULL WHERE id=42")
        without_author = updater.abba_audiobook_metadata(HASH, "huey-42")
        assert without_author is not None
        self.assertIsNone(without_author.author)

        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                "UPDATE requests SET service='shelfarr' WHERE id=42"
            )
        self.assertIsNone(updater.abba_audiobook_metadata(HASH, "huey-42"))

    def test_abba_metadata_fails_closed_for_multiple_matching_rows(self) -> None:
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                """
                UPDATE requests
                SET media_type='audiobooks', service='abba',
                    title='First', author='Author One', status='queued',
                    external_id=?, external_status='downloading'
                WHERE id=42
                """,
                (HASH,),
            )
            connection.execute(
                """
                INSERT INTO requests (
                    id, status, media_type, service, title, author, external_id
                ) VALUES (
                    44, 'downloading', 'audiobooks', 'abba',
                    'Second', 'Author Two', ?
                )
                """,
                (HASH,),
            )

        updater = HueyUpdater(self.database)
        single = updater.abba_audiobook_metadata(HASH, "huey-42")
        assert single is not None
        self.assertEqual("First", single.title)
        with self.assertRaises(MetadataCorrelationError):
            updater.abba_audiobook_metadata(HASH, "huey-42,huey-44")

        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                "UPDATE requests SET external_id=? WHERE id=44", ("b" * 40,)
            )
        with self.assertRaises(MetadataCorrelationError):
            updater.abba_audiobook_metadata(HASH, "huey-42,huey-44")

    def test_abba_alias_tags_converge_on_one_exact_owner(self) -> None:
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                """
                UPDATE requests
                SET media_type='audiobooks', service='abba', status='downloading',
                    title='Canonical', author='Author', external_id=?,
                    external_status='downloading'
                WHERE id=42
                """,
                (HASH,),
            )
            connection.execute(
                """
                INSERT INTO requests (
                    id, status, media_type, service, title, author, external_id,
                    external_status, canonical_request_id
                ) VALUES (
                    44, 'failed', 'audiobooks', 'abba', 'Duplicate', 'Author', ?,
                    'canonical_duplicate', 42
                )
                """,
                (HASH,),
            )

        updater = HueyUpdater(self.database)
        metadata = updater.abba_audiobook_metadata(
            HASH, "huey-42,huey-44"
        )
        assert metadata is not None
        self.assertEqual(metadata.title, "Canonical")
        self.assertTrue(
            updater.complete(
                HASH,
                Path("/media/audiobooks/Canonical"),
                "huey-42,huey-44",
            )
        )
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            rows = connection.execute(
                "SELECT id, status FROM requests WHERE id IN (42,44) ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, [(42, "complete"), (44, "failed")])

    def test_abba_metadata_rejects_any_unrelated_or_missing_huey_tag(self) -> None:
        raw_connection = sqlite3.connect(self.database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                """
                UPDATE requests
                SET media_type='audiobooks', service='abba', status='queued',
                    title='Canonical', author='Author', external_id=?,
                    external_status='queued'
                WHERE id=42
                """,
                (HASH,),
            )
            connection.execute(
                """
                INSERT INTO requests (
                    id, status, media_type, service, title, external_id,
                    external_status
                ) VALUES (
                    44, 'queued', 'audiobooks', 'qbittorrent', 'Unrelated', ?,
                    'queued'
                )
                """,
                (HASH,),
            )

        updater = HueyUpdater(self.database)
        for tags in ("huey-42,huey-44", "huey-42,huey-99"):
            with self.subTest(tags=tags):
                with self.assertRaises(MetadataCorrelationError):
                    updater.abba_audiobook_metadata(HASH, tags)
                self.assertFalse(
                    updater.complete(HASH, Path("/media/audiobooks/Book"), tags)
                )

    def test_huey_tag_grammar_rejects_leading_zero_and_oversized_ids(self) -> None:
        updater = HueyUpdater(self.database)
        self.assertEqual(
            (9_223_372_036_854_775_807,),
            updater._request_ids_from_tags("huey-9223372036854775807"),
        )
        for tags in (
            "huey-042",
            "HUEY-42",
            "huey:42",
            "huey:9223372036854775808",
            "huey:9999999999999999999",
            "huey:12345678901234567890",
        ):
            with self.subTest(tags=tags):
                with self.assertRaises(MetadataCorrelationError):
                    updater.abba_audiobook_metadata(HASH, tags)
                self.assertFalse(
                    updater.complete(HASH, Path("/media/audiobooks/Book"), tags)
                )

        self.assertIsNone(updater.abba_audiobook_metadata(HASH, "not huey-42"))
        self.assertFalse(
            updater.complete(
                HASH, Path("/media/audiobooks/Book"), "not huey-42"
            )
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
