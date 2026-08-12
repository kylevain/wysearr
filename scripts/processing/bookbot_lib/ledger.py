"""SQLite-backed idempotency, retry, retention, and additions ledger."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1


class _ClosingConnection(sqlite3.Connection):
    """Retain sqlite transaction context semantics while closing deterministically."""

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


class ImportLedger:
    """Owns durable state for imports without coupling it to qBittorrent."""

    def __init__(
        self,
        path: Path,
        retry_base_seconds: int = 60,
        retry_max_seconds: int = 3600,
    ) -> None:
        self.path = path
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=30, factory=_ClosingConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS imports (
                    torrent_hash TEXT PRIMARY KEY,
                    source_category TEXT NOT NULL,
                    imported_category TEXT NOT NULL,
                    torrent_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    destination_path TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    category_attempts INTEGER NOT NULL DEFAULT 0,
                    first_seen_at INTEGER NOT NULL,
                    last_attempt_at INTEGER,
                    next_retry_at INTEGER NOT NULL DEFAULT 0,
                    imported_at INTEGER,
                    deleted_at INTEGER,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_imports_status_retry
                    ON imports(status, next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_imports_retention
                    ON imports(imported_category, imported_at, status);

                CREATE TABLE IF NOT EXISTS recent_additions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    torrent_hash TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    library_path TEXT NOT NULL,
                    added_at INTEGER NOT NULL,
                    FOREIGN KEY(torrent_hash) REFERENCES imports(torrent_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_recent_additions_added
                    ON recent_additions(added_at);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    torrent_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY(torrent_hash) REFERENCES imports(torrent_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_events_torrent_created
                    ON events(torrent_hash, created_at);

                CREATE TABLE IF NOT EXISTS retention (
                    torrent_hash TEXT PRIMARY KEY,
                    imported_category TEXT NOT NULL,
                    first_observed_at INTEGER NOT NULL,
                    last_observed_at INTEGER NOT NULL,
                    deleted_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_retention_category_age
                    ON retention(imported_category, first_observed_at, deleted_at);
                """
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _now(now: int | None) -> int:
        return int(time.time()) if now is None else int(now)

    @staticmethod
    def _safe_error(error: str | Exception) -> str:
        return str(error).replace("\x00", "")[:2000]

    def get(self, torrent_hash: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM imports WHERE torrent_hash = ?", (torrent_hash,)
            ).fetchone()

    def should_copy(
        self, torrent_hash: str, max_retries: int, now: int | None = None
    ) -> bool:
        row = self.get(torrent_hash)
        if row is None:
            return True
        if row["status"] in {
            "copied",
            "imported",
            "deleted",
            "rejected",
            "failed",
            "copied_failed",
        }:
            return False
        if row["attempts"] >= max_retries:
            return False
        return row["next_retry_at"] <= self._now(now)

    def should_finalize(
        self, torrent_hash: str, max_retries: int, now: int | None = None
    ) -> bool:
        row = self.get(torrent_hash)
        if row is None or row["status"] != "copied":
            return False
        if row["category_attempts"] >= max_retries:
            return False
        return row["next_retry_at"] <= self._now(now)

    def begin_attempt(
        self, torrent: Mapping[str, Any], imported_category: str, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        torrent_hash = str(torrent["hash"])
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO imports (
                    torrent_hash, source_category, imported_category,
                    torrent_name, source_path, status, attempts,
                    first_seen_at, last_attempt_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, 'processing', 1, ?, ?, 0)
                ON CONFLICT(torrent_hash) DO UPDATE SET
                    source_category = excluded.source_category,
                    imported_category = excluded.imported_category,
                    torrent_name = excluded.torrent_name,
                    source_path = excluded.source_path,
                    status = 'processing',
                    attempts = imports.attempts + 1,
                    last_attempt_at = excluded.last_attempt_at,
                    next_retry_at = 0,
                    last_error = NULL
                """,
                (
                    torrent_hash,
                    str(torrent["category"]),
                    imported_category,
                    str(torrent.get("name", "")),
                    str(torrent.get("content_path", "")),
                    timestamp,
                    timestamp,
                ),
            )
            self._event(connection, torrent_hash, "attempted", "Import attempted", timestamp)

    def mark_retry(
        self, torrent_hash: str, error: str | Exception, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM imports WHERE torrent_hash = ?", (torrent_hash,)
            ).fetchone()
            attempts = int(row["attempts"]) if row else 1
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, attempts - 1)),
            )
            message = self._safe_error(error)
            connection.execute(
                """
                UPDATE imports
                SET status = 'retry', next_retry_at = ?, last_error = ?
                WHERE torrent_hash = ?
                """,
                (timestamp + delay, message, torrent_hash),
            )
            self._event(connection, torrent_hash, "retry", message, timestamp)

    def mark_rejected(
        self, torrent_hash: str, error: str | Exception, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        message = self._safe_error(error)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE imports
                SET status = 'rejected', next_retry_at = 0, last_error = ?
                WHERE torrent_hash = ?
                """,
                (message, torrent_hash),
            )
            self._event(connection, torrent_hash, "rejected", message, timestamp)

    def mark_failed(
        self, torrent_hash: str, error: str | Exception, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        message = self._safe_error(error)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE imports
                SET status = 'failed', next_retry_at = 0, last_error = ?
                WHERE torrent_hash = ?
                """,
                (message, torrent_hash),
            )
            self._event(connection, torrent_hash, "failed", message, timestamp)

    def mark_copied(
        self, torrent_hash: str, destination: Path, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE imports
                SET status = 'copied', destination_path = ?, next_retry_at = 0,
                    category_attempts = 0, last_error = NULL
                WHERE torrent_hash = ?
                """,
                (str(destination), torrent_hash),
            )
            self._event(
                connection,
                torrent_hash,
                "copied",
                f"Payload copied to {destination}",
                timestamp,
            )

    def mark_category_retry(
        self, torrent_hash: str, error: str | Exception, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        message = self._safe_error(error)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT category_attempts FROM imports WHERE torrent_hash = ?",
                (torrent_hash,),
            ).fetchone()
            attempts = (int(row["category_attempts"]) if row else 0) + 1
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, attempts - 1)),
            )
            connection.execute(
                """
                UPDATE imports
                SET status = 'copied', category_attempts = ?, next_retry_at = ?,
                    last_error = ?
                WHERE torrent_hash = ?
                """,
                (attempts, timestamp + delay, message, torrent_hash),
            )
            self._event(connection, torrent_hash, "category_retry", message, timestamp)

    def mark_category_failed(
        self, torrent_hash: str, error: str | Exception, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        message = self._safe_error(error)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE imports
                SET status = 'copied_failed', last_error = ?, next_retry_at = 0
                WHERE torrent_hash = ?
                """,
                (message, torrent_hash),
            )
            self._event(
                connection, torrent_hash, "category_failed", message, timestamp
            )

    def mark_imported(
        self,
        torrent_hash: str,
        media_type: str,
        title: str,
        destination: Path,
        now: int | None = None,
    ) -> None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE imports
                SET status = 'imported', destination_path = ?, imported_at = ?,
                    next_retry_at = 0, last_error = NULL
                WHERE torrent_hash = ?
                """,
                (str(destination), timestamp, torrent_hash),
            )
            connection.execute(
                """
                INSERT INTO recent_additions (
                    torrent_hash, media_type, title, library_path, added_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(torrent_hash) DO NOTHING
                """,
                (torrent_hash, media_type, title, str(destination), timestamp),
            )
            self._event(
                connection,
                torrent_hash,
                "imported",
                f"Import completed at {destination}",
                timestamp,
            )

    def eligible_for_deletion(
        self, torrent_hash: str, cutoff: int
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM imports
                WHERE torrent_hash = ? AND status = 'imported'
                  AND imported_at IS NOT NULL AND imported_at <= ?
                """,
                (torrent_hash, cutoff),
            ).fetchone()

    def observe_arr_imported(
        self,
        torrent_hash: str,
        imported_category: str,
        now: int | None = None,
    ) -> None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO retention (
                    torrent_hash, imported_category,
                    first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(torrent_hash) DO UPDATE SET
                    imported_category = excluded.imported_category,
                    last_observed_at = excluded.last_observed_at
                """,
                (torrent_hash, imported_category, timestamp, timestamp),
            )

    def arr_eligible_for_deletion(
        self,
        torrent_hash: str,
        imported_category: str,
        cutoff: int,
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM retention
                WHERE torrent_hash = ? AND imported_category = ?
                  AND deleted_at IS NULL AND first_observed_at <= ?
                """,
                (torrent_hash, imported_category, cutoff),
            ).fetchone()

    def mark_arr_deleted(
        self, torrent_hash: str, now: int | None = None
    ) -> None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE retention SET deleted_at = ? WHERE torrent_hash = ?
                """,
                (timestamp, torrent_hash),
            )

    def mark_deleted(self, torrent_hash: str, now: int | None = None) -> None:
        timestamp = self._now(now)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE imports
                SET status = 'deleted', deleted_at = ?, last_error = NULL
                WHERE torrent_hash = ?
                """,
                (timestamp, torrent_hash),
            )
            self._event(
                connection,
                torrent_hash,
                "deleted",
                "Torrent and local payload deleted after retention",
                timestamp,
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM imports GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        torrent_hash: str,
        event_type: str,
        message: str,
        timestamp: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (torrent_hash, created_at, event_type, message)
            VALUES (?, ?, ?, ?)
            """,
            (torrent_hash, timestamp, event_type, message[:2000]),
        )
