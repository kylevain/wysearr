from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from bookbot_lib.config import CATEGORY_SPECS, BookBotConfig
from bookbot_lib.huey import HueyUpdater
from bookbot_lib.ledger import ImportLedger
from bookbot_lib.service import BookBotService
from bookbot_lib.storage import LibraryImporter


HASH_A = "a" * 40
HASH_B = "b" * 40


class FakeQbittorrent:
    def __init__(self, torrents: list[dict[str, Any]], root: Path) -> None:
        self.items = torrents
        self.category_map = {
            name: {"savePath": str(root / name)} for name in CATEGORY_SPECS
        }
        self.category_map.update(
            {
                f"{name}-imported": {"savePath": str(root / name)}
                for name in CATEGORY_SPECS
            }
        )
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.create_calls: list[tuple[str, str]] = []
        self.fail_set_count = 0

    def application_version(self) -> str:
        return "v5.1.4"

    def categories(self) -> dict[str, dict[str, str]]:
        return dict(self.category_map)

    def torrents(self) -> list[dict[str, Any]]:
        return self.items

    def ensure_imported_category(
        self,
        source_category: str,
        imported_category: str,
        torrent_save_path: str,
        *,
        dry_run: bool = False,
    ) -> None:
        expected = self.category_map[source_category]["savePath"]
        existing = self.category_map.get(imported_category)
        if existing is not None and existing["savePath"] != expected:
            raise RuntimeError("category mismatch")
        if existing is None and not dry_run:
            self.category_map[imported_category] = {"savePath": expected}
            self.create_calls.append((imported_category, expected))

    def set_category(self, torrent_hash: str, category: str) -> None:
        if self.fail_set_count:
            self.fail_set_count -= 1
            raise RuntimeError("set category unavailable")
        self.set_calls.append((torrent_hash, category))
        for item in self.items:
            if item["hash"] == torrent_hash:
                item["category"] = category

    def delete_with_files(self, torrent_hash: str) -> None:
        self.delete_calls.append(torrent_hash)

    def close(self) -> None:
        return None


class FakeHuey:
    def __init__(self) -> None:
        self.completed: list[tuple[str, Path, str]] = []
        self.failures: list[tuple[str, str, str]] = []

    def complete(self, torrent_hash: str, destination: Path, tags: str = "") -> bool:
        self.completed.append((torrent_hash, destination, tags))
        return True

    def failed(self, torrent_hash: str, error: object, tags: str = "") -> bool:
        self.failures.append((torrent_hash, str(error), tags))
        return True


class AlwaysFailImporter:
    def import_payload(self, *_args: object, **_kwargs: object) -> object:
        raise OSError("temporary copy failure")


def completed_torrent(
    torrent_hash: str,
    category: str,
    source: Path,
    torrent_root: Path,
    **overrides: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "hash": torrent_hash,
        "name": source.name,
        "category": category,
        "content_path": str(source),
        "save_path": str(torrent_root / category.replace("-imported", "")),
        "progress": 1.0,
        "amount_left": 0,
        "tags": "huey-42",
    }
    value.update(overrides)
    return value


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.downloads = root / "downloads"
        self.media = root / "media"
        self.config_dir = root / "config"
        self.downloads.mkdir()
        self.media.mkdir()
        self.config_dir.mkdir()
        for name in CATEGORY_SPECS:
            (self.downloads / name).mkdir()
        self.config = BookBotConfig(
            torrent_root=self.downloads,
            media_root=self.media,
            database_path=self.config_dir / "bookbot.db",
            health_path=self.config_dir / "health.json",
            huey_database_path=None,
            qbittorrent_url="http://qbittorrent:8080",
            qbittorrent_username="operator",
            qbittorrent_password="secret",
            retry_base_seconds=1,
            retry_max_seconds=2,
            max_retries=2,
            retention_days=14,
        )
        self.huey = FakeHuey()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(
        self,
        qbit: FakeQbittorrent,
        importer: Any | None = None,
    ) -> BookBotService:
        return BookBotService(
            self.config,
            qbittorrent=qbit,  # type: ignore[arg-type]
            ledger=ImportLedger(
                self.config.database_path,
                retry_base_seconds=1,
                retry_max_seconds=2,
            ),
            importer=importer or LibraryImporter(self.downloads, self.media),
            huey=self.huey,  # type: ignore[arg-type]
        )

    def test_successful_import_preserves_source_and_is_idempotent(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"book")
        torrent = completed_torrent(
            HASH_A, "ebooks", source, self.downloads
        )
        qbit = FakeQbittorrent([torrent], self.downloads)
        service = self.service(qbit)

        first = service.run_cycle(now=100)
        second = service.run_cycle(now=101)

        destination = self.media / "ebooks" / "Books" / "Book"
        self.assertEqual(1, first.imported)
        self.assertEqual(0, second.imported)
        self.assertEqual(b"book", source.read_bytes())
        self.assertEqual(b"book", (destination / "Book.epub").read_bytes())
        self.assertFalse((destination / ".bookbot-import.json").exists())
        self.assertEqual([(HASH_A, "ebooks-imported")], qbit.set_calls)
        self.assertEqual(2, len(self.huey.completed))
        self.assertTrue(self.config.health_path.is_file())

    def test_retained_import_reconciles_later_duplicate_huey_request(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"book")
        torrent = completed_torrent(
            HASH_A,
            "ebooks",
            source,
            self.downloads,
            tags="huey-1,huey-4",
        )
        qbit = FakeQbittorrent([torrent], self.downloads)
        huey_database = self.config_dir / "huey.db"
        raw_connection = sqlite3.connect(huey_database)
        with closing(raw_connection) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    torrent_hash TEXT,
                    external_id TEXT,
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
                """
                INSERT INTO requests (id, status, torrent_hash, external_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (1, "downloading", HASH_A, HASH_A),
                    (3, "failed", HASH_A, HASH_A),
                    (4, "queued", HASH_B, HASH_B),
                ),
            )
        service = BookBotService(
            self.config,
            qbittorrent=qbit,  # type: ignore[arg-type]
            ledger=ImportLedger(
                self.config.database_path,
                retry_base_seconds=1,
                retry_max_seconds=2,
            ),
            importer=LibraryImporter(self.downloads, self.media),
            huey=HueyUpdater(huey_database),
        )

        first = service.run_cycle(now=100)
        imported_at = service.ledger.get(HASH_A)["imported_at"]
        raw_connection = sqlite3.connect(huey_database)
        with closing(raw_connection) as connection, connection:
            connection.execute(
                """
                INSERT INTO requests (id, status, torrent_hash, external_id)
                VALUES (2, 'queued', ?, ?)
                """,
                (HASH_A, HASH_A),
            )
        second = service.run_cycle(now=200)

        raw_connection = sqlite3.connect(huey_database)
        with closing(raw_connection) as connection, connection:
            statuses = connection.execute(
                "SELECT id, status FROM requests ORDER BY id"
            ).fetchall()
        self.assertEqual(1, first.imported)
        self.assertEqual(1, second.reconciled)
        self.assertEqual(imported_at, service.ledger.get(HASH_A)["imported_at"])
        self.assertEqual(
            [(1, "complete"), (2, "complete"), (3, "failed"), (4, "queued")],
            statuses,
        )

    def test_incomplete_torrent_is_ignored(self) -> None:
        source = self.downloads / "ebooks" / "Partial.epub"
        source.write_bytes(b"partial")
        torrent = completed_torrent(
            HASH_A,
            "ebooks",
            source,
            self.downloads,
            progress=0.9,
            amount_left=100,
        )
        service = self.service(FakeQbittorrent([torrent], self.downloads))
        counts = service.run_cycle(now=100)
        self.assertEqual(1, counts.ignored)
        self.assertIsNone(service.ledger.get(HASH_A))
        self.assertFalse((self.media / "ebooks").exists())

    def test_category_failure_retries_without_recopied_conflict(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"book")
        torrent = completed_torrent(HASH_A, "ebooks", source, self.downloads)
        qbit = FakeQbittorrent([torrent], self.downloads)
        qbit.fail_set_count = 1
        service = self.service(qbit)

        first = service.run_cycle(now=100)
        row = service.ledger.get(HASH_A)
        assert row is not None
        self.assertEqual(1, first.retried)
        self.assertEqual("copied", row["status"])

        second = service.run_cycle(now=101)
        self.assertEqual(1, second.imported)
        self.assertFalse((self.media / "duplicates").exists())
        self.assertEqual(b"book", source.read_bytes())

    def test_copy_failure_exhausts_retries_and_marks_huey_failed(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"book")
        torrent = completed_torrent(HASH_A, "ebooks", source, self.downloads)
        service = self.service(
            FakeQbittorrent([torrent], self.downloads), AlwaysFailImporter()
        )

        first = service.run_cycle(now=100)
        second = service.run_cycle(now=101)

        self.assertEqual(1, first.retried)
        self.assertEqual(1, second.errors)
        row = service.ledger.get(HASH_A)
        assert row is not None
        self.assertEqual("failed", row["status"])
        self.assertEqual(1, len(self.huey.failures))
        self.assertEqual("huey-42", self.huey.failures[0][2])

    def test_unsupported_payload_is_terminal_and_marks_huey_failed(self) -> None:
        source = self.downloads / "ebooks" / "Program.exe"
        source.write_bytes(b"program")
        torrent = completed_torrent(HASH_A, "ebooks", source, self.downloads)
        service = self.service(FakeQbittorrent([torrent], self.downloads))
        counts = service.run_cycle(now=100)
        self.assertEqual(1, counts.rejected)
        row = service.ledger.get(HASH_A)
        assert row is not None
        self.assertEqual("rejected", row["status"])
        self.assertEqual(1, len(self.huey.failures))

    def test_misrouted_content_path_is_rejected(self) -> None:
        source = self.downloads / "roms" / "Book.epub"
        source.write_bytes(b"book")
        torrent = completed_torrent(
            HASH_A,
            "ebooks",
            source,
            self.downloads,
            save_path=str(self.downloads / "ebooks"),
        )
        service = self.service(FakeQbittorrent([torrent], self.downloads))
        counts = service.run_cycle(now=100)
        self.assertEqual(1, counts.rejected)
        self.assertFalse((self.media / "ebooks").exists())

    def test_dry_run_writes_no_import_state_or_health_marker(self) -> None:
        source = self.downloads / "roms" / "Game.zip"
        source.write_bytes(b"rom")
        torrent = completed_torrent(HASH_A, "roms", source, self.downloads)
        qbit = FakeQbittorrent([torrent], self.downloads)
        service = self.service(qbit)
        counts = service.run_cycle(dry_run=True, now=100)
        self.assertEqual(1, counts.dry_run)
        self.assertIsNone(service.ledger.get(HASH_A))
        self.assertEqual([], qbit.set_calls)
        self.assertEqual([], qbit.create_calls)
        self.assertFalse(self.config.health_path.exists())
        self.assertFalse((self.media / "roms").exists())

    def test_retention_deletes_only_old_ledger_managed_imports(self) -> None:
        source = self.downloads / "ebooks" / "Book.epub"
        source.write_bytes(b"book")
        managed = completed_torrent(HASH_A, "ebooks", source, self.downloads)
        qbit = FakeQbittorrent([managed], self.downloads)
        service = self.service(qbit)
        service.run_cycle(now=100)

        untracked_source = self.downloads / "ebooks" / "Other.epub"
        untracked_source.write_bytes(b"other")
        untracked = completed_torrent(
            HASH_B,
            "ebooks-imported",
            untracked_source,
            self.downloads,
            save_path=str(self.downloads / "ebooks"),
        )
        qbit.items.append(untracked)
        counts = service.run_cycle(now=100 + 14 * 86400)

        self.assertEqual([HASH_A], qbit.delete_calls)
        self.assertEqual(1, counts.deleted)
        self.assertIsNone(service.ledger.get(HASH_B))

    def test_arr_imported_retention_starts_at_first_observation(self) -> None:
        source = self.downloads / "tv" / "Episode.mkv"
        (self.downloads / "tv").mkdir()
        source.write_bytes(b"episode")
        torrent = completed_torrent(
            HASH_A,
            "tv-imported",
            source,
            self.downloads,
            save_path=str(self.downloads / "tv"),
        )
        qbit = FakeQbittorrent([torrent], self.downloads)
        service = self.service(qbit)

        first = service.run_cycle(now=100)
        early = service.run_cycle(now=100 + 14 * 86400 - 1)
        due = service.run_cycle(now=100 + 14 * 86400)

        self.assertEqual(0, first.deleted)
        self.assertEqual(0, early.deleted)
        self.assertEqual(1, due.deleted)
        self.assertEqual([HASH_A], qbit.delete_calls)

    def test_arr_base_categories_are_never_age_deleted(self) -> None:
        source = self.downloads / "tv" / "Episode.mkv"
        (self.downloads / "tv").mkdir()
        source.write_bytes(b"episode")
        torrent = completed_torrent(
            HASH_A,
            "tv",
            source,
            self.downloads,
            save_path=str(self.downloads / "tv"),
        )
        qbit = FakeQbittorrent([torrent], self.downloads)
        service = self.service(qbit)
        service.run_cycle(now=100)
        service.run_cycle(now=100 + 100 * 86400)
        self.assertEqual([], qbit.delete_calls)

    def test_validate_requires_all_direct_categories_and_exact_paths(self) -> None:
        qbit = FakeQbittorrent([], self.downloads)
        service = self.service(qbit)
        result = service.validate()
        self.assertEqual("v5.1.4", result["qbittorrent_version"])
        del qbit.category_map["roms"]
        with self.assertRaises(Exception):
            service.validate()

    def test_validate_rejects_imported_category_relocation_path(self) -> None:
        qbit = FakeQbittorrent([], self.downloads)
        qbit.category_map["ebooks-imported"] = {
            "savePath": str(self.downloads / "ebooks-imported")
        }
        service = self.service(qbit)
        with self.assertRaises(Exception):
            service.validate()


if __name__ == "__main__":
    unittest.main()
