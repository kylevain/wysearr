"""SQLite persistence and migrations for Huey requests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


REQUEST_STATUSES = frozenset(
    {"new", "processing", "queued", "needs_selection", "failed", "complete", "completed"}
)
_REQUEST_COLUMNS = {
    "updated_at": "TEXT",
    "service": "TEXT",
    "external_id": "TEXT",
    "external_title": "TEXT",
    "error": "TEXT",
    "notified_at": "TEXT",
}


class _ClosingConnection(sqlite3.Connection):
    """Give SQLite connections normal context-manager close semantics."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class RequestStore:
    """Small transaction-oriented repository around the Huey SQLite database."""

    def __init__(self, path: str | Path, schema_path: str | Path | None = None):
        self.path = Path(path)
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).with_name("schema.sql")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = self.schema_path.read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.executescript(schema)
            self._add_missing_columns(connection)
            connection.execute(
                "UPDATE requests SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
            )
            self._merge_duplicate_messages(connection)
            self._ensure_unique_message_index(connection)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS requests_status_idx
                    ON requests(status, updated_at);
                CREATE INDEX IF NOT EXISTS requests_media_created_idx
                    ON requests(media_type, created_at);
                CREATE INDEX IF NOT EXISTS events_request_created_idx
                    ON events(request_id, created_at);
                """
            )

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(requests)").fetchall()
        }
        for name, definition in _REQUEST_COLUMNS.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE requests ADD COLUMN {name} {definition}")

    @staticmethod
    def _merge_duplicate_messages(connection: sqlite3.Connection) -> None:
        duplicates = connection.execute(
            """
            SELECT message_id, MIN(id) AS keep_id, COUNT(*) AS duplicate_count
            FROM requests
            GROUP BY message_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for duplicate in duplicates:
            latest = connection.execute(
                """
                SELECT * FROM requests
                WHERE message_id = ?
                ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
                LIMIT 1
                """,
                (duplicate["message_id"],),
            ).fetchone()
            connection.execute(
                """
                UPDATE requests
                SET updated_at = ?, title = COALESCE(?, title),
                    author = COALESCE(?, author), status = ?,
                    service = COALESCE(?, service),
                    external_id = COALESCE(?, external_id),
                    external_title = COALESCE(?, external_title),
                    error = COALESCE(?, error),
                    notified_at = COALESCE(?, notified_at)
                WHERE id = ?
                """,
                (
                    latest["updated_at"],
                    latest["title"],
                    latest["author"],
                    latest["status"],
                    latest["service"],
                    latest["external_id"],
                    latest["external_title"],
                    latest["error"],
                    latest["notified_at"],
                    duplicate["keep_id"],
                ),
            )
            duplicate_ids = connection.execute(
                "SELECT id FROM requests WHERE message_id = ? AND id <> ? ORDER BY id",
                (duplicate["message_id"], duplicate["keep_id"]),
            ).fetchall()
            for row in duplicate_ids:
                connection.execute(
                    "UPDATE events SET request_id = ? WHERE request_id = ?",
                    (duplicate["keep_id"], row["id"]),
                )
                connection.execute("DELETE FROM requests WHERE id = ?", (row["id"],))
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    duplicate["keep_id"],
                    "migration_deduplicated",
                    f"Merged {duplicate['duplicate_count'] - 1} duplicate Discord delivery record(s)",
                ),
            )

    @staticmethod
    def _ensure_unique_message_index(connection: sqlite3.Connection) -> None:
        expected_name = "requests_message_id_uq"
        existing = {
            row["name"]: row for row in connection.execute("PRAGMA index_list(requests)")
        }.get(expected_name)
        if existing is not None:
            columns = [
                row["name"]
                for row in connection.execute(
                    "PRAGMA index_info(requests_message_id_uq)"
                ).fetchall()
            ]
            if not existing["unique"] or columns != ["message_id"]:
                connection.execute("DROP INDEX requests_message_id_uq")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS requests_message_id_uq ON requests(message_id)"
        )

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return dict(row) if row else None

    def get_by_message_id(self, message_id: str | int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE message_id = ?", (str(message_id),)
            ).fetchone()
        return dict(row) if row else None

    def create_request(
        self,
        *,
        discord_user_id: str | int,
        discord_username: str,
        channel_id: str | int,
        message_id: str | int,
        media_type: str,
        raw_request: str,
        title: str | None,
        author: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert once by Discord message ID and return ``(record, created)``."""

        values = (
            str(discord_user_id),
            str(discord_username),
            str(channel_id),
            str(message_id),
            media_type,
            raw_request,
            title,
            author,
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO requests (
                    discord_user_id, discord_username, channel_id, message_id,
                    media_type, raw_request, title, author
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                values,
            )
            created = cursor.rowcount == 1
            if created:
                request_id = cursor.lastrowid
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (request_id, "received", "Request received from Discord"),
                )
            else:
                existing = connection.execute(
                    "SELECT id FROM requests WHERE message_id = ?", (str(message_id),)
                ).fetchone()
                if existing is None:
                    raise sqlite3.IntegrityError("Request could not be recorded")
                request_id = existing["id"]
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (request_id, "duplicate_delivery", "Duplicate Discord delivery ignored"),
                )
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        return dict(row), created

    def add_event(self, request_id: int, event_type: str, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (request_id, event_type, message),
            )

    def transition(
        self,
        request_id: int,
        status: str,
        message: str,
        *,
        event_type: str | None = None,
        service: str | None = None,
        external_id: str | int | None = None,
        external_title: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Atomically update request state and append its corresponding event."""

        if status not in REQUEST_STATUSES:
            raise ValueError(f"Invalid request status: {status}")
        if not message or not message.strip():
            raise ValueError("State transitions require an event message")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = ?, updated_at = CURRENT_TIMESTAMP,
                    service = ?, external_id = ?, external_title = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    service,
                    str(external_id) if external_id is not None else None,
                    external_title,
                    error,
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown request ID: {request_id}")
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (request_id, event_type or status, message.strip()),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        return dict(row)

    def events_for(self, request_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE request_id = ? ORDER BY id", (request_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return externally terminal requests which have no delivery marker."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status IN ('complete', 'completed', 'failed')
                  AND notified_at IS NULL
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notified(self, request_id: int, message: str) -> bool:
        """Atomically mark a terminal request notified exactly once."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET notified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND notified_at IS NULL
                  AND status IN ('complete', 'completed', 'failed')
                """,
                (request_id,),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (request_id, "completion_notified", message[:2000]),
            )
            return True


def transition_request(
    store: RequestStore, request_id: int, status: str, message: str, **fields: Any
) -> dict[str, Any]:
    """Functional wrapper used by scripts and tests."""

    return store.transition(request_id, status, message, **fields)
