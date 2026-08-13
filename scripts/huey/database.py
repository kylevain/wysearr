"""SQLite persistence and migrations for Huey requests."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REQUEST_STATUSES = frozenset(
    {
        "new",
        "processing",
        "awaiting_selection",
        "queued",
        "needs_selection",
        "failed",
        "complete",
        "completed",
    }
)
CANDIDATE_CONFIRMATION_TTL_SECONDS = 15 * 60
_SELECTION_FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")
_SELECTION_WORK_ID = re.compile(
    r"\A(?:hardcover|google_books|openlibrary):[A-Za-z0-9][A-Za-z0-9._:-]{0,230}\Z"
)
_SENSITIVE_SELECTION_TEXT = re.compile(
    r"(?:https?://|ftp://|www\.|magnet:|"
    r"(?:api[\s_-]*key|token|password|secret|authorization)\s*[:=]|"
    r"authorization\s+bearer\s+)",
    re.IGNORECASE,
)
_SENSITIVE_SELECTION_IDENTITY = re.compile(
    r"(?:api[_-]?key|token|password|secret|authorization)", re.IGNORECASE
)
_SELECTION_SNAPSHOT_KEYS = frozenset(
    {
        "fingerprint",
        "label",
        "work_id",
        "source_work_ids",
        "title",
        "author",
        "year",
        "content_kind",
        "media_type",
        "book_type",
    }
)
_REQUEST_COLUMNS = {
    "updated_at": "TEXT",
    "service": "TEXT",
    "external_id": "TEXT",
    "external_title": "TEXT",
    "error": "TEXT",
    "notified_at": "TEXT",
    "target_key": "TEXT",
    "external_status": "TEXT",
}
_CANDIDATE_CONFIRMATION_COLUMNS = {
    "dispatch_started_at": "TEXT",
}

SHELFARR_STATUSES = frozenset(
    {
        "pending",
        "searching",
        "awaiting_purchase",
        "not_found",
        "downloading",
        "processing",
        "completed",
        "failed",
    }
)


class _ClosingConnection(sqlite3.Connection):
    """Give SQLite connections normal context-manager close semantics."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _selection_timestamp(value: datetime | None = None) -> str:
    moment = value or datetime.now(timezone.utc)
    if not isinstance(moment, datetime):
        raise TypeError("Selection timestamps must be datetime values")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Selection timestamps must include a timezone")
    return moment.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat(
        sep=" "
    )


def _safe_selection_text(value: object, *, limit: int, optional: bool = False) -> str | None:
    if optional and value in (None, ""):
        return None
    text = " ".join(
        "".join(character for character in str(value or "") if character.isprintable()).split()
    )
    if (
        not text
        or len(text) > limit
        or _SENSITIVE_SELECTION_TEXT.search(text)
        or any(marker in text for marker in ("@", "`", "<", ">"))
    ):
        raise ValueError("Candidate confirmation contains unsafe display text")
    return text


def _normalize_candidate_snapshot(
    value: object, *, request_media_type: str
) -> dict[str, Any]:
    """Copy only the bounded, inert metadata contract used for revalidation."""

    if not isinstance(value, Mapping) or set(value) - _SELECTION_SNAPSHOT_KEYS:
        raise ValueError("Candidate confirmation contains unsupported fields")
    fingerprint = str(value.get("fingerprint") or "")
    work_id = str(value.get("work_id") or "")
    raw_source_ids = value.get("source_work_ids")
    media_type = str(value.get("media_type") or "")
    book_type = str(value.get("book_type") or "")
    content_kind = str(value.get("content_kind") or "")
    year = value.get("year")
    if (
        not _SELECTION_FINGERPRINT.fullmatch(fingerprint)
        or not _SELECTION_WORK_ID.fullmatch(work_id)
        or _SENSITIVE_SELECTION_IDENTITY.search(work_id)
        or not isinstance(raw_source_ids, (list, tuple))
        or not 1 <= len(raw_source_ids) <= 8
        or any(
            not _SELECTION_WORK_ID.fullmatch(str(source_id or ""))
            or bool(_SENSITIVE_SELECTION_IDENTITY.search(str(source_id or "")))
            for source_id in raw_source_ids
        )
        or len({str(source_id) for source_id in raw_source_ids}) != len(raw_source_ids)
        or str(raw_source_ids[0]) != work_id
        or media_type != request_media_type
        or media_type not in {"ebooks", "audiobooks"}
        or book_type != {"ebooks": "ebook", "audiobooks": "audiobook"}.get(media_type)
        or content_kind != "book"
        or isinstance(year, bool)
        or (year is not None and (not isinstance(year, int) or not 0 <= year <= 9999))
    ):
        raise ValueError("Candidate confirmation contains an invalid identity")

    snapshot = {
        "fingerprint": fingerprint,
        "label": _safe_selection_text(value.get("label"), limit=300),
        "work_id": work_id,
        "source_work_ids": [str(source_id) for source_id in raw_source_ids],
        "title": _safe_selection_text(value.get("title"), limit=160),
        "author": _safe_selection_text(value.get("author"), limit=160, optional=True),
        "year": year,
        "content_kind": "book",
        "media_type": media_type,
        "book_type": book_type,
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("Candidate confirmation snapshot is too large")
    return snapshot


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
            self._fail_unbound_candidate_confirmations(connection)
            self._fail_claimed_pre_dispatch_confirmations(connection)
            self._mark_interrupted_shelfarr_requests(connection)
            self._fail_interrupted_requests(connection)
            self._backfill_target_keys(connection)
            connection.executescript(
                """
                DROP INDEX IF EXISTS requests_active_target_uq;
                CREATE UNIQUE INDEX IF NOT EXISTS requests_active_target_uq
                    ON requests(target_key)
                    WHERE target_key IS NOT NULL
                      AND status IN (
                          'new', 'processing', 'awaiting_selection',
                          'queued', 'complete', 'completed'
                      );
                CREATE INDEX IF NOT EXISTS requests_status_idx
                    ON requests(status, updated_at);
                CREATE INDEX IF NOT EXISTS requests_media_created_idx
                    ON requests(media_type, created_at);
                CREATE INDEX IF NOT EXISTS events_request_created_idx
                    ON events(request_id, created_at);
                """
            )

    @staticmethod
    def _fail_unbound_candidate_confirmations(
        connection: sqlite3.Connection,
    ) -> None:
        """Release crash-window prompts which Discord never durably bound."""

        message = (
            "Huey restarted before the candidate prompt was durably bound; "
            "submit the request again."
        )
        rows = connection.execute(
            """
            SELECT candidate_confirmations.id, candidate_confirmations.request_id
            FROM candidate_confirmations
            JOIN requests ON requests.id = candidate_confirmations.request_id
            WHERE candidate_confirmations.status = 'pending'
              AND candidate_confirmations.prompt_message_id IS NULL
              AND requests.status = 'awaiting_selection'
            ORDER BY candidate_confirmations.id
            """
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE candidate_confirmations
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
                    failure_message = ?
                WHERE id = ? AND status = 'pending'
                """,
                (message, row["id"]),
            )
            connection.execute(
                """
                UPDATE requests
                SET status = 'needs_selection', updated_at = CURRENT_TIMESTAMP,
                    error = ?
                WHERE id = ? AND status = 'awaiting_selection'
                """,
                (message, row["request_id"]),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (row["request_id"], "selection_prompt_recovered", message),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    request_id, event_key, route, message
                ) VALUES (?, 'request_rejected', 'request-status', ?)
                """,
                (
                    row["request_id"],
                    f"⚠️ Request #{row['request_id']} needs clarification: {message}",
                ),
            )

    @staticmethod
    def _fail_claimed_pre_dispatch_confirmations(
        connection: sqlite3.Connection,
    ) -> None:
        """Release a selection proven not to have crossed the POST boundary.

        ``dispatch_started_at`` is set transactionally by the callback which
        runs immediately before Shelfarr's non-idempotent request POST.  A
        claimed row without that marker after restart therefore cannot have
        reached Shelfarr and may be released without risking a duplicate.
        Rows with the marker remain owned for correlation recovery.
        """

        message = (
            "Huey restarted before the confirmed selection reached Shelfarr; "
            "submit the title again."
        )
        rows = connection.execute(
            """
            SELECT candidate_confirmations.id, candidate_confirmations.request_id
            FROM candidate_confirmations
            JOIN requests ON requests.id = candidate_confirmations.request_id
            WHERE candidate_confirmations.status = 'claimed'
              AND candidate_confirmations.dispatch_started_at IS NULL
              AND requests.status = 'processing'
              AND requests.service = 'shelfarr'
              AND requests.external_id IS NULL
            ORDER BY candidate_confirmations.id
            """
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE candidate_confirmations
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
                    failure_message = ?
                WHERE id = ? AND status = 'claimed'
                  AND dispatch_started_at IS NULL
                """,
                (message, row["id"]),
            )
            connection.execute(
                """
                UPDATE requests
                SET status = 'needs_selection', updated_at = CURRENT_TIMESTAMP,
                    error = ?
                WHERE id = ? AND status = 'processing'
                  AND service = 'shelfarr' AND external_id IS NULL
                """,
                (message, row["request_id"]),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (row["request_id"], "selection_dispatch_recovered", message),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    request_id, event_key, route, message
                ) VALUES (?, 'request_rejected', 'request-status', ?)
                """,
                (
                    row["request_id"],
                    f"⚠️ Request #{row['request_id']} needs clarification: {message}",
                ),
            )

    @staticmethod
    def _mark_interrupted_shelfarr_requests(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id FROM requests
            WHERE status = 'processing' AND service = 'shelfarr'
              AND external_id IS NULL
            """
        ).fetchall()
        for row in rows:
            exists = connection.execute(
                """
                SELECT 1 FROM events
                WHERE request_id = ? AND event_type = 'startup_shelfarr_recovery'
                """,
                (row["id"],),
            ).fetchone()
            if exists is None:
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        row["id"],
                        "startup_shelfarr_recovery",
                        "Huey restarted during a Shelfarr dispatch; recovering correlation",
                    ),
                )

    @staticmethod
    def _fail_interrupted_requests(connection: sqlite3.Connection) -> None:
        message = (
            "Huey restarted before this request reached a durable queued state; "
            "review acquisition services before resubmitting"
        )
        rows = connection.execute(
            """
            SELECT id FROM requests
            WHERE status IN ('new', 'processing')
              AND NOT (status = 'processing' AND service = 'shelfarr')
            """
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE requests
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP, error = ?
                WHERE id = ?
                """,
                (message, row["id"]),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (row["id"], "startup_reconciled", message),
            )

    def interrupted_shelfarr_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return dispatches whose Shelfarr POST may have crossed a crash window."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'processing'
                  AND service = 'shelfarr'
                  AND external_id IS NULL
                  AND EXISTS (
                      SELECT 1 FROM events
                      WHERE events.request_id = requests.id
                        AND events.event_type = 'startup_shelfarr_recovery'
                  )
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(requests)").fetchall()
        }
        for name, definition in _REQUEST_COLUMNS.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE requests ADD COLUMN {name} {definition}")
        confirmation_tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_confirmations'"
        ).fetchone()
        if confirmation_tables is not None:
            confirmation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(candidate_confirmations)"
                ).fetchall()
            }
            for name, definition in _CANDIDATE_CONFIRMATION_COLUMNS.items():
                if name not in confirmation_columns:
                    connection.execute(
                        f"ALTER TABLE candidate_confirmations "
                        f"ADD COLUMN {name} {definition}"
                    )

    @staticmethod
    def _backfill_target_keys(connection: sqlite3.Connection) -> None:
        """Give historical active/completed rows the same exact identity boundary.

        Historical failed and selection-needed rows stay unkeyed so they remain
        retryable. If historical active rows already duplicate one another, one
        terminal-first canonical row is keyed and the pre-existing records are
        otherwise left untouched rather than silently merged.
        """

        try:
            from .matching import request_target_key
            from .parser import RequestParseError, parse_request
        except ImportError:  # pragma: no cover - direct container entrypoint
            from matching import request_target_key
            from parser import RequestParseError, parse_request

        rows = connection.execute(
            """
            SELECT id, media_type, raw_request, status
            FROM requests
            WHERE target_key IS NULL
              AND status IN ('queued', 'complete', 'completed')
            ORDER BY id
            """
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            try:
                parsed = parse_request(str(row["raw_request"]), str(row["media_type"]))
            except RequestParseError:
                continue
            key = request_target_key(str(row["media_type"]), parsed)
            if key is None:
                continue
            grouped.setdefault(key, []).append(row)

        for key, candidates in grouped.items():
            already_keyed = connection.execute(
                """
                SELECT id FROM requests
                WHERE target_key = ?
                  AND status IN (
                      'new', 'processing', 'awaiting_selection',
                      'queued', 'complete', 'completed'
                  )
                LIMIT 1
                """,
                (key,),
            ).fetchone()
            if already_keyed is not None:
                continue
            canonical = min(
                candidates,
                key=lambda row: (
                    0 if row["status"] in {"complete", "completed"} else 1,
                    row["id"],
                ),
            )
            connection.execute(
                "UPDATE requests SET target_key = ? WHERE id = ? AND target_key IS NULL",
                (key, canonical["id"]),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    canonical["id"],
                    "migration_target_key",
                    "Backfilled conservative exact-target identity",
                ),
            )

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
                    external_status = COALESCE(?, external_status),
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
                    latest["external_status"],
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
            if row is None:
                row = connection.execute(
                    """
                    SELECT requests.*
                    FROM delivery_aliases
                    JOIN requests ON requests.id = delivery_aliases.request_id
                    WHERE delivery_aliases.message_id = ?
                    """,
                    (str(message_id),),
                ).fetchone()
            if row is None:
                # Discord normally preserves a reply reference on gateway
                # redelivery, but the selection reply ID is itself the durable
                # idempotency key.  Recognize it even when a later delivery has
                # lost that reference so its numeric body cannot become a new
                # book-title request.
                row = connection.execute(
                    """
                    SELECT requests.*
                    FROM candidate_confirmation_replies
                    JOIN candidate_confirmations
                      ON candidate_confirmations.id =
                         candidate_confirmation_replies.confirmation_id
                    JOIN requests
                      ON requests.id = candidate_confirmations.request_id
                    WHERE candidate_confirmation_replies.reply_message_id = ?
                    """,
                    (str(message_id),),
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
        target_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Reserve one exact target and return its canonical request record.

        Discord redeliveries are keyed by message ID. Distinct messages are
        coalesced only while the exact canonical target is active or complete;
        failed and ``needs_selection`` requests deliberately remain retryable.
        """

        values = (
            str(discord_user_id),
            str(discord_username),
            str(channel_id),
            str(message_id),
            media_type,
            raw_request,
            title,
            author,
            target_key,
        )
        with self.connect() as connection:
            # Serialize the read-before-insert reservation. This prevents two
            # simultaneous Discord messages for one exact target from both
            # reaching an acquisition handler.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM requests WHERE message_id = ?", (str(message_id),)
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT request_id AS id FROM delivery_aliases WHERE message_id = ?",
                    (str(message_id),),
                ).fetchone()
            if existing is None:
                existing = connection.execute(
                    """
                    SELECT candidate_confirmations.request_id AS id
                    FROM candidate_confirmation_replies
                    JOIN candidate_confirmations
                      ON candidate_confirmations.id =
                         candidate_confirmation_replies.confirmation_id
                    WHERE candidate_confirmation_replies.reply_message_id = ?
                    """,
                    (str(message_id),),
                ).fetchone()
            if existing is not None:
                request_id = existing["id"]
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (request_id, "duplicate_delivery", "Duplicate Discord delivery ignored"),
                )
                row = connection.execute(
                    "SELECT * FROM requests WHERE id = ?", (request_id,)
                ).fetchone()
                return dict(row), False

            canonical = None
            if target_key:
                canonical = connection.execute(
                    """
                    SELECT id FROM requests
                    WHERE target_key = ?
                      AND status IN (
                          'new', 'processing', 'awaiting_selection',
                          'queued', 'complete', 'completed'
                      )
                    ORDER BY id
                    LIMIT 1
                    """,
                    (target_key,),
                ).fetchone()
            if canonical is not None:
                request_id = canonical["id"]
                connection.execute(
                    "INSERT INTO delivery_aliases (message_id, request_id) VALUES (?, ?)",
                    (str(message_id), request_id),
                )
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        request_id,
                        "duplicate_target",
                        "Exact active or completed target already has a canonical request",
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM requests WHERE id = ?", (request_id,)
                ).fetchone()
                return dict(row), False

            cursor = connection.execute(
                """
                INSERT INTO requests (
                    discord_user_id, discord_username, channel_id, message_id,
                    media_type, raw_request, title, author, target_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            request_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (request_id, "received", "Request received from Discord"),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        return dict(row), True

    @staticmethod
    def _candidate_confirmation_row(
        connection: sqlite3.Connection, request_id: int
    ) -> dict[str, Any] | None:
        confirmation = connection.execute(
            "SELECT * FROM candidate_confirmations WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if confirmation is None:
            return None
        options = []
        for row in connection.execute(
            """
            SELECT ordinal, fingerprint, label, title, author, year,
                   book_type, candidate_json
            FROM candidate_options
            WHERE confirmation_id = ?
            ORDER BY ordinal
            """,
            (confirmation["id"],),
        ).fetchall():
            try:
                candidate = json.loads(str(row["candidate_json"]))
            except (json.JSONDecodeError, TypeError) as error:
                raise sqlite3.DatabaseError(
                    "Candidate confirmation contains invalid JSON"
                ) from error
            if not isinstance(candidate, dict):
                raise sqlite3.DatabaseError(
                    "Candidate confirmation contains an invalid snapshot"
                )
            options.append(
                {
                    "ordinal": row["ordinal"],
                    "fingerprint": row["fingerprint"],
                    "label": row["label"],
                    "title": row["title"],
                    "author": row["author"],
                    "year": row["year"],
                    "book_type": row["book_type"],
                    "candidate": candidate,
                }
            )
        value = dict(confirmation)
        value["options"] = options
        return value

    def get_candidate_confirmation(self, request_id: int) -> dict[str, Any] | None:
        """Return one persisted prompt and its inert candidate snapshots."""

        with self.connect() as connection:
            return self._candidate_confirmation_row(connection, int(request_id))

    def create_candidate_confirmation(
        self,
        request_id: int,
        candidates: Sequence[Mapping[str, Any]],
        *,
        now: datetime | None = None,
        ttl_seconds: int = CANDIDATE_CONFIRMATION_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Persist a bounded Shelfarr choice and reserve its target atomically."""

        if isinstance(candidates, (str, bytes)) or not 2 <= len(candidates) <= 3:
            raise ValueError("Candidate confirmations require two or three options")
        if isinstance(ttl_seconds, bool) or not 1 <= int(ttl_seconds) <= 86_400:
            raise ValueError("Candidate confirmation TTL must be between 1 and 86400 seconds")
        moment = now or datetime.now(timezone.utc)
        created_at = _selection_timestamp(moment)
        expires_at = _selection_timestamp(moment + timedelta(seconds=int(ttl_seconds)))

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            if request is None:
                raise KeyError(f"Unknown request ID: {request_id}")

            existing = self._candidate_confirmation_row(connection, int(request_id))
            if existing is not None:
                if existing["status"] == "pending" and request["status"] == "awaiting_selection":
                    return existing
                raise ValueError("Request already has a candidate confirmation")
            if request["status"] != "processing" or request["service"] != "shelfarr":
                raise ValueError(
                    "Candidate confirmations require a processing Shelfarr request"
                )

            normalized = [
                _normalize_candidate_snapshot(
                    candidate, request_media_type=str(request["media_type"])
                )
                for candidate in candidates
            ]
            if len({candidate["fingerprint"] for candidate in normalized}) != len(
                normalized
            ):
                raise ValueError("Candidate confirmations require distinct fingerprints")

            cursor = connection.execute(
                """
                INSERT INTO candidate_confirmations (
                    request_id, shelfarr_correlation, created_at, updated_at,
                    expires_at, status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    int(request_id),
                    f"huey:{int(request_id)}",
                    created_at,
                    created_at,
                    expires_at,
                ),
            )
            confirmation_id = int(cursor.lastrowid)
            for ordinal, candidate in enumerate(normalized, start=1):
                connection.execute(
                    """
                    INSERT INTO candidate_options (
                        confirmation_id, ordinal, fingerprint, label, title,
                        author, year, book_type, candidate_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        confirmation_id,
                        ordinal,
                        candidate["fingerprint"],
                        candidate["label"],
                        candidate["title"],
                        candidate["author"],
                        candidate["year"],
                        candidate["book_type"],
                        json.dumps(
                            candidate,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            connection.execute(
                """
                UPDATE requests
                SET status = 'awaiting_selection', updated_at = ?, error = NULL
                WHERE id = ? AND status = 'processing' AND service = 'shelfarr'
                """,
                (created_at, int(request_id)),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "selection_requested",
                    "Shelfarr metadata candidates require requester confirmation",
                ),
            )
            value = self._candidate_confirmation_row(connection, int(request_id))
            if value is None:  # pragma: no cover - transaction invariant
                raise sqlite3.DatabaseError("Candidate confirmation was not persisted")
            return value

    @staticmethod
    def _discord_snowflake(value: str | int) -> str | None:
        text = str(value).strip()
        return text if text.isdecimal() and 1 <= len(text) <= 32 else None

    def bind_candidate_prompt(
        self, request_id: int, prompt_message_id: str | int
    ) -> bool:
        """Bind the exact Huey reply ID once; conflicting bindings fail closed."""

        prompt_id = self._discord_snowflake(prompt_message_id)
        if prompt_id is None:
            return False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT prompt_message_id FROM candidate_confirmations
                WHERE request_id = ? AND status = 'pending'
                """,
                (int(request_id),),
            ).fetchone()
            if row is None:
                return False
            if row["prompt_message_id"] == prompt_id:
                return True
            if row["prompt_message_id"] is not None:
                return False
            collision = connection.execute(
                "SELECT 1 FROM candidate_confirmations WHERE prompt_message_id = ?",
                (prompt_id,),
            ).fetchone()
            if collision is not None:
                return False
            cursor = connection.execute(
                """
                UPDATE candidate_confirmations
                SET prompt_message_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND status = 'pending'
                  AND prompt_message_id IS NULL
                """,
                (prompt_id, int(request_id)),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _selection_claim_result(
        outcome: str,
        request: sqlite3.Row | Mapping[str, Any] | None,
        option: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "request": dict(request) if request is not None else None,
            "option": dict(option) if option is not None else None,
        }

    def claim_candidate_selection(
        self,
        *,
        prompt_message_id: str | int,
        reply_message_id: str | int,
        discord_user_id: str | int,
        channel_id: str | int,
        ordinal: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Authorize and claim one reply; only ``claimed`` permits dispatch."""

        prompt_id = self._discord_snowflake(prompt_message_id)
        reply_id = self._discord_snowflake(reply_message_id)
        if prompt_id is None:
            return self._selection_claim_result("not_found", None)
        observed_at = _selection_timestamp(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            joined = connection.execute(
                """
                SELECT candidate_confirmations.id AS confirmation_id,
                       candidate_confirmations.status AS confirmation_status,
                       candidate_confirmations.expires_at,
                       requests.*
                FROM candidate_confirmations
                JOIN requests ON requests.id = candidate_confirmations.request_id
                WHERE candidate_confirmations.prompt_message_id = ?
                """,
                (prompt_id,),
            ).fetchone()
            if joined is None:
                return self._selection_claim_result("not_found", None)

            request = {
                key: joined[key]
                for key in joined.keys()
                if key not in {"confirmation_id", "confirmation_status", "expires_at"}
            }
            confirmation_id = int(joined["confirmation_id"])
            existing_reply = (
                connection.execute(
                    "SELECT 1 FROM candidate_confirmation_replies WHERE reply_message_id = ?",
                    (reply_id,),
                ).fetchone()
                if reply_id is not None
                else None
            )
            if existing_reply is not None:
                return self._selection_claim_result("duplicate", request)
            if joined["confirmation_status"] == "claimed":
                # Persist every distinct late reply ID as a consumed delivery.
                # Discord should retain its reference on redelivery, but this
                # prevents a later reference-less copy from being interpreted
                # as a standalone request.
                if reply_id is not None:
                    connection.execute(
                        """
                        INSERT INTO candidate_confirmation_replies (
                            confirmation_id, reply_message_id, created_at,
                            discord_user_id, channel_id, ordinal, outcome
                        ) VALUES (?, ?, ?, ?, ?, ?, 'duplicate')
                        """,
                        (
                            confirmation_id,
                            reply_id,
                            observed_at,
                            str(discord_user_id),
                            str(channel_id),
                            int(ordinal)
                            if isinstance(ordinal, int)
                            and not isinstance(ordinal, bool)
                            else 0,
                        ),
                    )
                return self._selection_claim_result("duplicate", request)

            identity_valid = bool(
                reply_id is not None
                and str(discord_user_id) == str(joined["discord_user_id"])
                and str(channel_id) == str(joined["channel_id"])
            )
            if not identity_valid:
                if reply_id is not None:
                    connection.execute(
                        """
                        INSERT INTO candidate_confirmation_replies (
                            confirmation_id, reply_message_id, created_at,
                            discord_user_id, channel_id, ordinal, outcome
                        ) VALUES (?, ?, ?, ?, ?, ?, 'invalid')
                        """,
                        (
                            confirmation_id,
                            reply_id,
                            observed_at,
                            str(discord_user_id),
                            str(channel_id),
                            int(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else 0,
                        ),
                    )
                return self._selection_claim_result("invalid", request)

            confirmation_status = str(joined["confirmation_status"])
            if confirmation_status == "expired":
                connection.execute(
                    """
                    INSERT INTO candidate_confirmation_replies (
                        confirmation_id, reply_message_id, created_at,
                        discord_user_id, channel_id, ordinal, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, 'expired')
                    """,
                    (
                        confirmation_id,
                        reply_id,
                        observed_at,
                        str(discord_user_id),
                        str(channel_id),
                        int(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else 0,
                    ),
                )
                return self._selection_claim_result("expired", request)
            if confirmation_status != "pending" or request["status"] != "awaiting_selection":
                connection.execute(
                    """
                    INSERT INTO candidate_confirmation_replies (
                        confirmation_id, reply_message_id, created_at,
                        discord_user_id, channel_id, ordinal, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, 'invalid')
                    """,
                    (
                        confirmation_id,
                        reply_id,
                        observed_at,
                        str(discord_user_id),
                        str(channel_id),
                        int(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else 0,
                    ),
                )
                return self._selection_claim_result("invalid", request)

            if str(joined["expires_at"]) <= observed_at:
                message = "Candidate confirmation expired; submit the request again."
                connection.execute(
                    """
                    UPDATE candidate_confirmations
                    SET status = 'expired', updated_at = ?, failure_message = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (observed_at, message, confirmation_id),
                )
                connection.execute(
                    """
                    UPDATE requests
                    SET status = 'needs_selection', updated_at = ?, error = ?
                    WHERE id = ? AND status = 'awaiting_selection'
                    """,
                    (observed_at, message, request["id"]),
                )
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (request["id"], "selection_expired", message),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, 'request_rejected', 'request-status', ?)
                    """,
                    (
                        request["id"],
                        f"⚠️ Request #{request['id']} needs clarification: {message}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO candidate_confirmation_replies (
                        confirmation_id, reply_message_id, created_at,
                        discord_user_id, channel_id, ordinal, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, 'expired')
                    """,
                    (
                        confirmation_id,
                        reply_id,
                        observed_at,
                        str(discord_user_id),
                        str(channel_id),
                        int(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else 0,
                    ),
                )
                request = connection.execute(
                    "SELECT * FROM requests WHERE id = ?", (request["id"],)
                ).fetchone()
                return self._selection_claim_result("expired", request)

            selected_ordinal = (
                int(ordinal)
                if isinstance(ordinal, int) and not isinstance(ordinal, bool)
                else 0
            )
            option_row = connection.execute(
                """
                SELECT ordinal, fingerprint, label, title, author, year,
                       book_type, candidate_json
                FROM candidate_options
                WHERE confirmation_id = ? AND ordinal = ?
                """,
                (confirmation_id, selected_ordinal),
            ).fetchone()
            if option_row is None:
                connection.execute(
                    """
                    INSERT INTO candidate_confirmation_replies (
                        confirmation_id, reply_message_id, created_at,
                        discord_user_id, channel_id, ordinal, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, 'invalid')
                    """,
                    (
                        confirmation_id,
                        reply_id,
                        observed_at,
                        str(discord_user_id),
                        str(channel_id),
                        selected_ordinal,
                    ),
                )
                return self._selection_claim_result("invalid", request)

            candidate = json.loads(str(option_row["candidate_json"]))
            option = {
                "ordinal": option_row["ordinal"],
                "fingerprint": option_row["fingerprint"],
                "label": option_row["label"],
                "title": option_row["title"],
                "author": option_row["author"],
                "year": option_row["year"],
                "book_type": option_row["book_type"],
                "candidate": candidate,
            }
            connection.execute(
                """
                UPDATE candidate_confirmations
                SET status = 'claimed', selected_ordinal = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (selected_ordinal, observed_at, confirmation_id),
            )
            connection.execute(
                """
                INSERT INTO candidate_confirmation_replies (
                    confirmation_id, reply_message_id, created_at,
                    discord_user_id, channel_id, ordinal, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, 'claimed')
                """,
                (
                    confirmation_id,
                    reply_id,
                    observed_at,
                    str(discord_user_id),
                    str(channel_id),
                    selected_ordinal,
                ),
            )
            connection.execute(
                """
                UPDATE requests
                SET status = 'processing', updated_at = ?, error = NULL
                WHERE id = ? AND status = 'awaiting_selection' AND service = 'shelfarr'
                """,
                (observed_at, request["id"]),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    request["id"],
                    "selection_claimed",
                    f"Requester confirmed candidate {selected_ordinal}",
                ),
            )
            request = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (request["id"],)
            ).fetchone()
            return self._selection_claim_result("claimed", request, option)

    def mark_candidate_dispatch_started(
        self, request_id: int, *, now: datetime | None = None
    ) -> bool:
        """Durably cross the selected-candidate POST boundary exactly once.

        The Shelfarr client invokes this immediately before its sole request
        creation POST. A restart can consequently distinguish a confirmed
        choice which was never submitted from one whose outcome needs
        correlation recovery.
        """

        observed_at = _selection_timestamp(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT candidate_confirmations.id,
                       candidate_confirmations.dispatch_started_at
                FROM candidate_confirmations
                JOIN requests ON requests.id = candidate_confirmations.request_id
                WHERE candidate_confirmations.request_id = ?
                  AND candidate_confirmations.status = 'claimed'
                  AND requests.status = 'processing'
                  AND requests.service = 'shelfarr'
                  AND requests.external_id IS NULL
                """,
                (int(request_id),),
            ).fetchone()
            if row is None:
                return False
            if row["dispatch_started_at"] is not None:
                return True
            cursor = connection.execute(
                """
                UPDATE candidate_confirmations
                SET dispatch_started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'claimed'
                  AND dispatch_started_at IS NULL
                """,
                (observed_at, observed_at, row["id"]),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "selection_dispatch_started",
                    "Confirmed Shelfarr candidate crossed the request dispatch boundary",
                ),
            )
            return True

    def expire_candidate_confirmations(
        self, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Expire every due pending prompt and release its target reservation."""

        observed_at = _selection_timestamp(now)
        message = "Candidate confirmation expired; submit the request again."
        expired: list[dict[str, Any]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT candidate_confirmations.id, candidate_confirmations.request_id
                FROM candidate_confirmations
                JOIN requests ON requests.id = candidate_confirmations.request_id
                WHERE candidate_confirmations.status = 'pending'
                  AND candidate_confirmations.expires_at <= ?
                  AND requests.status = 'awaiting_selection'
                ORDER BY candidate_confirmations.id
                """,
                (observed_at,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE candidate_confirmations
                    SET status = 'expired', updated_at = ?, failure_message = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (observed_at, message, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE requests
                    SET status = 'needs_selection', updated_at = ?, error = ?
                    WHERE id = ? AND status = 'awaiting_selection'
                    """,
                    (observed_at, message, row["request_id"]),
                )
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (row["request_id"], "selection_expired", message),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, 'request_rejected', 'request-status', ?)
                    """,
                    (
                        row["request_id"],
                        f"⚠️ Request #{row['request_id']} needs clarification: {message}",
                    ),
                )
                request = connection.execute(
                    "SELECT * FROM requests WHERE id = ?", (row["request_id"],)
                ).fetchone()
                if request is not None:
                    expired.append(dict(request))
        return expired

    def fail_candidate_prompt(self, request_id: int, message: str) -> bool:
        """Fail a prompt that Discord could not durably bind and release its target."""

        failure = _safe_selection_text(message, limit=500)
        if failure is None:  # pragma: no cover - optional=False invariant
            raise ValueError("Candidate prompt failure requires a message")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            confirmation = connection.execute(
                """
                SELECT id FROM candidate_confirmations
                WHERE request_id = ? AND status = 'pending'
                """,
                (int(request_id),),
            ).fetchone()
            request = connection.execute(
                "SELECT status FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            if confirmation is None or request is None or request["status"] != "awaiting_selection":
                return False
            connection.execute(
                """
                UPDATE candidate_confirmations
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
                    failure_message = ?
                WHERE id = ? AND status = 'pending'
                """,
                (failure, confirmation["id"]),
            )
            connection.execute(
                """
                UPDATE requests
                SET status = 'needs_selection', updated_at = CURRENT_TIMESTAMP,
                    error = ?
                WHERE id = ? AND status = 'awaiting_selection'
                """,
                (failure, int(request_id)),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (int(request_id), "selection_prompt_failed", failure),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    request_id, event_key, route, message
                ) VALUES (?, 'request_rejected', 'request-status', ?)
                """,
                (
                    int(request_id),
                    f"⚠️ Request #{int(request_id)} needs clarification: {failure}",
                ),
            )
            return True

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
        external_status: str | None = None,
        error: str | None = None,
        notifications: Sequence[tuple[str, str, str]] = (),
    ) -> dict[str, Any]:
        """Atomically persist state, its event, and optional lifecycle outbox."""

        if status not in REQUEST_STATUSES:
            raise ValueError(f"Invalid request status: {status}")
        if not message or not message.strip():
            raise ValueError("State transitions require an event message")
        normalized_notifications: list[tuple[str, str, str]] = []
        for delivery in notifications:
            if (
                not isinstance(delivery, (list, tuple))
                or len(delivery) != 3
                or any(not isinstance(value, str) or not value.strip() for value in delivery)
            ):
                raise ValueError("Notification delivery fields cannot be empty")
            normalized_notifications.append(
                (delivery[0].strip(), delivery[1].strip(), delivery[2].strip()[:2000])
            )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = ?, updated_at = CURRENT_TIMESTAMP,
                    service = ?, external_id = ?, external_title = ?,
                    external_status = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    service,
                    str(external_id) if external_id is not None else None,
                    external_title,
                    external_status,
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
            for event_key, route, notification_message in normalized_notifications:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (request_id, event_key, route, notification_message),
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
                SELECT requests.*,
                       (
                           SELECT events.event_type
                           FROM events
                           WHERE events.request_id = requests.id
                             AND events.event_type IN (
                                 'handler_complete', 'handler_completed',
                                 'handler_failed', 'arr_completed',
                                 'shelfarr_completed', 'shelfarr_failed',
                                 'shelfarr_import_failed',
                                 'shelfarr_manual_intervention',
                                 'complete', 'completed', 'failed',
                                 'startup_reconciled'
                             )
                           ORDER BY events.id DESC
                           LIMIT 1
                       ) AS terminal_event_type
                FROM requests
                WHERE requests.status IN ('complete', 'completed', 'failed')
                  AND requests.notified_at IS NULL
                ORDER BY requests.updated_at, requests.id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue_notification(
        self,
        request_id: int,
        event_key: str,
        route: str,
        message: str,
    ) -> bool:
        """Persist one logical event/route pair without duplicating it."""

        if not event_key or not route or not message or not message.strip():
            raise ValueError("Notification delivery fields cannot be empty")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    request_id, event_key, route, message
                ) VALUES (?, ?, ?, ?)
                """,
                (request_id, event_key, route, message.strip()[:2000]),
            )
            return cursor.rowcount == 1

    def pending_notification_deliveries(
        self, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return undelivered outbox rows in stable creation order."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_deliveries
                WHERE delivered_at IS NULL
                ORDER BY id
                LIMIT ?
                """,
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_delivered(self, delivery_id: int) -> bool:
        """Atomically record successful delivery of one outbox row."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE notification_deliveries
                SET delivered_at = CURRENT_TIMESTAMP
                WHERE id = ? AND delivered_at IS NULL
                """,
                (delivery_id,),
            )
            return cursor.rowcount == 1

    def notification_delivered(
        self, request_id: int, event_key: str, route: str
    ) -> bool:
        """Report whether an exact logical event route has been delivered."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT delivered_at FROM notification_deliveries
                WHERE request_id = ? AND event_key = ? AND route = ?
                """,
                (request_id, event_key, route),
            ).fetchone()
        return row is not None and row["delivered_at"] is not None

    def mark_notified_if_delivered(self, request_id: int, message: str) -> bool:
        """Finalize a terminal request after every staged route has succeeded."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET notified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND notified_at IS NULL
                  AND status IN ('complete', 'completed', 'failed')
                  AND EXISTS (
                      SELECT 1 FROM notification_deliveries
                      WHERE request_id = requests.id
                        AND event_key IN (
                            'request_completed', 'request_failed'
                        )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_deliveries
                      WHERE request_id = requests.id AND delivered_at IS NULL
                  )
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

    def queued_arr_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return queued requests whose ARR entity may now contain imported media."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'queued'
                  AND service IN ('sonarr', 'radarr', 'lidarr')
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_arr_completed(self, request_id: int, message: str) -> bool:
        """Atomically complete a still-queued ARR request and append one event."""

        if not message or not message.strip():
            raise ValueError("ARR completion requires an event message")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'completed', updated_at = CURRENT_TIMESTAMP, error = NULL
                WHERE id = ? AND status = 'queued'
                  AND service IN ('sonarr', 'radarr', 'lidarr')
                """,
                (request_id,),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (request_id, "arr_completed", message.strip()[:2000]),
            )
            return True

    def queued_shelfarr_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return active Huey requests owned by Shelfarr."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'queued'
                  AND service = 'shelfarr'
                  AND external_id IS NOT NULL
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def uncertain_shelfarr_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return lost-response submissions awaiting durable correlation."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'queued'
                  AND service = 'shelfarr'
                  AND external_id IS NULL
                  AND external_status = 'submission_uncertain'
                  AND EXISTS (
                      SELECT 1 FROM events
                      WHERE events.request_id = requests.id
                        AND events.event_type = 'shelfarr_submission_uncertain'
                  )
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_shelfarr_state(
        self,
        request_id: int,
        external_status: str,
        message: str,
        *,
        event_type: str,
        terminal_status: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Persist one observed Shelfarr state, with atomic terminalization."""

        normalized = external_status.strip().casefold()
        if normalized not in SHELFARR_STATUSES:
            raise ValueError(f"Invalid Shelfarr status: {external_status}")
        if terminal_status not in {None, "completed", "failed"}:
            raise ValueError(f"Invalid Shelfarr terminal status: {terminal_status}")
        if not event_type or not message or not message.strip():
            raise ValueError("Shelfarr state observations require an event and message")

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, external_status FROM requests
                WHERE id = ? AND status = 'queued' AND service = 'shelfarr'
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                return False
            if row["external_status"] == normalized and terminal_status is None:
                return False

            next_status = terminal_status or "queued"
            next_error = error if terminal_status == "failed" else None
            connection.execute(
                """
                UPDATE requests
                SET status = ?, external_status = ?, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'queued' AND service = 'shelfarr'
                """,
                (next_status, normalized, next_error, request_id),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (request_id, event_type, message.strip()[:2000]),
            )
            return True

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
