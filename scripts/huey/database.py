"""SQLite persistence and migrations for Huey requests."""

from __future__ import annotations

import json
import hashlib
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
UNAVAILABLE_FIRST_RETRY_DELAY = timedelta(days=7)
UNAVAILABLE_RETRY_INTERVAL = timedelta(days=30)
UNAVAILABLE_RETRY_LIMIT = 7
UNAVAILABLE_RETRY_ACTIVE_STATES = frozenset(
    {"queued", "retrying", "awaiting_import", "blocked"}
)
_SELECTION_FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")
_ABBA_CANDIDATE_ID = re.compile(r"\Aabba:[0-9a-f]{64}\Z")
_ABBA_INFO_HASH = re.compile(r"\A[0-9a-f]{40}\Z")
_LAZYLIBRARIAN_BOOK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
_EBOOK_BACKENDS = frozenset({"lazylibrarian", "shelfarr"})
_DOWNLOAD_ID = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_SELECTION_WORK_ID = re.compile(
    r"\A(?:(?:hardcover|google_books|openlibrary):"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,230}|"
    r"(?:abba|lazylibrarian):[0-9a-f]{64})\Z"
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
    "dispatch_started_at": "TEXT",
    "abba_candidate_id": "TEXT",
    "lazylibrarian_book_id": "TEXT",
    "canonical_request_id": "INTEGER",
}
_CANDIDATE_CONFIRMATION_COLUMNS = {
    "dispatch_started_at": "TEXT",
}
_UNAVAILABLE_RETRY_COLUMNS = {
    "last_proof_check_at": "TEXT",
}
_TRUSTED_LIBRARY_EVENT_COLUMNS = {
    "media_type": "TEXT NOT NULL DEFAULT 'movie'",
    "group_key": "TEXT",
    "sonarr_series_id": "INTEGER",
    "sonarr_command_id": "INTEGER",
    "metadata_json": "TEXT",
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
ABBA_STATUSES = frozenset(
    {"queued", "downloading", "downloaded", "processing", "failed"}
)
LAZYLIBRARIAN_STATUSES = frozenset(
    {"queued", "downloading", "processing", "failed"}
)


class LazyLibrarianHashCollision(sqlite3.IntegrityError):
    """A LazyLibrarian request resolved to an already-reserved qBit identity."""


class EbookIdentityCollision(sqlite3.IntegrityError):
    """Another logical Huey request already owns the resolved ebook work."""

    def __init__(self, owner_request_id: int):
        super().__init__("Resolved ebook identity is already reserved")
        self.owner_request_id = int(owner_request_id)


class EbookCascadeStateError(RuntimeError):
    """A cascade transition would violate its serial mutation invariant."""


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


def _ebook_identity_key(snapshot: Mapping[str, Any]) -> str:
    """Return a provider-independent exact-work reservation key."""

    try:
        from .matching import normalize_identity_text
    except ImportError:  # pragma: no cover - direct container entrypoint
        from matching import normalize_identity_text

    payload = {
        "version": 1,
        "media_type": "ebooks",
        "title": normalize_identity_text(snapshot.get("title")),
        "author": normalize_identity_text(snapshot.get("author")),
        "year": snapshot.get("year"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _ebook_policy(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("Ebook backend policy must be an ordered sequence")
    policy = tuple(str(backend) for backend in value)
    if (
        not policy
        or len(set(policy)) != len(policy)
        or any(backend not in _EBOOK_BACKENDS for backend in policy)
    ):
        raise ValueError("Ebook backend policy is invalid")
    return policy


def _ebook_backend_identity(backend: str, value: object) -> str:
    identity = str(value or "")
    if backend == "lazylibrarian":
        if not _LAZYLIBRARIAN_BOOK_ID.fullmatch(identity):
            raise ValueError("LazyLibrarian reservation requires an exact BookID")
    elif backend == "shelfarr":
        if not _SELECTION_WORK_ID.fullmatch(identity) or identity.startswith(
            ("abba:", "lazylibrarian:")
        ):
            raise ValueError("Shelfarr reservation requires an exact work ID")
    else:
        raise ValueError("Unsupported ebook backend")
    return identity


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
            self._migrate_trusted_notification_outbox(connection)
            self._add_missing_columns(connection)
            connection.execute(
                "UPDATE requests SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
            )
            self._merge_duplicate_messages(connection)
            self._ensure_unique_message_index(connection)
            self._fail_unbound_candidate_confirmations(connection)
            self._fail_claimed_pre_dispatch_confirmations(connection)
            self._mark_interrupted_shelfarr_requests(connection)
            self._mark_interrupted_abba_requests(connection)
            self._mark_interrupted_lazylibrarian_requests(connection)
            self._fail_interrupted_requests(connection)
            self._recover_interrupted_unavailable_retries(connection)
            self._backfill_target_keys(connection)
            # Keep legacy-collision repair and every stricter replacement index
            # in one SQLite savepoint.  ``executescript`` commits pending work
            # before it runs, which could otherwise persist a partial quarantine
            # if a later uniqueness check failed.
            connection.execute("SAVEPOINT huey_identity_index_migration")
            try:
                for index_name in (
                    "requests_active_abba_hash_uq",
                    "requests_active_abba_candidate_uq",
                    "requests_active_ll_hash_uq",
                    "requests_active_target_uq",
                ):
                    connection.execute(f"DROP INDEX IF EXISTS {index_name}")
                self._migrate_abba_collisions(connection)
                self._migrate_lazylibrarian_hash_collisions(connection)
                index_statements = (
                    """
                    CREATE UNIQUE INDEX requests_active_target_uq
                        ON requests(target_key)
                        WHERE target_key IS NOT NULL
                          AND status IN (
                              'new', 'processing', 'awaiting_selection',
                              'queued', 'complete', 'completed'
                          )
                    """,
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS requests_active_ll_book_uq
                        ON requests(lazylibrarian_book_id)
                        WHERE service = 'lazylibrarian'
                          AND lazylibrarian_book_id IS NOT NULL
                          AND status IN (
                              'processing', 'queued', 'complete', 'completed'
                          )
                    """,
                    """
                    CREATE UNIQUE INDEX requests_active_ll_hash_uq
                        ON requests(lower(external_id))
                        WHERE service = 'lazylibrarian'
                          AND external_id IS NOT NULL
                          AND status IN (
                              'processing', 'queued', 'complete', 'completed'
                          )
                    """,
                    """
                    CREATE UNIQUE INDEX requests_active_abba_hash_uq
                        ON requests(lower(external_id))
                        WHERE service = 'abba'
                          AND external_id IS NOT NULL
                          AND canonical_request_id IS NULL
                          AND status IN (
                              'processing', 'queued', 'complete', 'completed'
                          )
                    """,
                    """
                    CREATE UNIQUE INDEX requests_active_abba_candidate_uq
                        ON requests(abba_candidate_id)
                        WHERE service = 'abba'
                          AND abba_candidate_id IS NOT NULL
                          AND canonical_request_id IS NULL
                          AND status IN (
                              'processing', 'queued', 'complete', 'completed'
                          )
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS requests_canonical_request_idx
                        ON requests(canonical_request_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS requests_status_idx
                        ON requests(status, updated_at)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS requests_media_created_idx
                        ON requests(media_type, created_at)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS events_request_created_idx
                        ON events(request_id, created_at)
                    """,
                )
                for statement in index_statements:
                    connection.execute(statement)
            except Exception:
                connection.execute("ROLLBACK TO huey_identity_index_migration")
                connection.execute("RELEASE huey_identity_index_migration")
                raise
            else:
                connection.execute("RELEASE huey_identity_index_migration")

    @staticmethod
    def _migrate_trusted_notification_outbox(
        connection: sqlite3.Connection,
    ) -> None:
        """Make the lifecycle outbox usable by requests and trusted events."""

        columns = {
            row["name"]: row
            for row in connection.execute(
                "PRAGMA table_info(notification_deliveries)"
            ).fetchall()
        }
        if (
            "trusted_event_id" in columns
            and int(columns["request_id"]["notnull"]) == 0
        ):
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS notification_deliveries_request_uq
                ON notification_deliveries(request_id, event_key, route)
                WHERE request_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS notification_deliveries_trusted_event_uq
                ON notification_deliveries(trusted_event_id, event_key, route)
                WHERE trusted_event_id IS NOT NULL
                """
            )
            return
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            DROP INDEX IF EXISTS notification_deliveries_pending_idx;
            DROP INDEX IF EXISTS notification_deliveries_request_uq;
            DROP INDEX IF EXISTS notification_deliveries_trusted_event_uq;
            ALTER TABLE notification_deliveries
                RENAME TO notification_deliveries_legacy;
            CREATE TABLE notification_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER,
                trusted_event_id INTEGER,
                event_key TEXT NOT NULL,
                route TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at TEXT,
                FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE,
                FOREIGN KEY(trusted_event_id) REFERENCES trusted_library_events(id) ON DELETE CASCADE,
                CHECK ((request_id IS NOT NULL) != (trusted_event_id IS NOT NULL))
            );
            INSERT INTO notification_deliveries (
                id, request_id, event_key, route, message, created_at, delivered_at
            )
            SELECT id, request_id, event_key, route, message, created_at, delivered_at
            FROM notification_deliveries_legacy;
            DROP TABLE notification_deliveries_legacy;
            CREATE UNIQUE INDEX notification_deliveries_request_uq
                ON notification_deliveries(request_id, event_key, route)
                WHERE request_id IS NOT NULL;
            CREATE UNIQUE INDEX notification_deliveries_trusted_event_uq
                ON notification_deliveries(trusted_event_id, event_key, route)
                WHERE trusted_event_id IS NOT NULL;
            CREATE INDEX notification_deliveries_pending_idx
                ON notification_deliveries(delivered_at, id);
            COMMIT;
            """
        )

    @staticmethod
    def _release_unowned_ebook_backend_reservations(
        connection: sqlite3.Connection, request_id: int
    ) -> None:
        """Release provider IDs only after durable retry ownership has ended."""

        connection.execute(
            """
            DELETE FROM ebook_backend_reservations
            WHERE request_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM unavailable_retries
                  WHERE request_id = ?
                    AND state IN (
                        'queued', 'retrying', 'awaiting_import', 'blocked',
                        'fulfilled'
                    )
              )
            """,
            (int(request_id), int(request_id)),
        )

    @staticmethod
    def _recover_interrupted_unavailable_retries(
        connection: sqlite3.Connection,
    ) -> None:
        """Repair retry ownership after a restart without repeating mutations."""

        rows = connection.execute(
            """
            SELECT unavailable_retries.request_id,
                   unavailable_retries.retry_count,
                   unavailable_retries.last_retry_at,
                   unavailable_retries.state,
                   requests.status
            FROM unavailable_retries
            JOIN requests ON requests.id = unavailable_retries.request_id
            WHERE unavailable_retries.state IN ('retrying', 'awaiting_import')
            """
        ).fetchall()
        for row in rows:
            status = str(row["status"] or "")
            retry_state = str(row["state"] or "")
            if retry_state == "awaiting_import" and status == "failed":
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'blocked', next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'awaiting_import'
                    """,
                    (int(row["request_id"]),),
                )
                continue
            if retry_state == "awaiting_import" and status not in {
                "complete",
                "completed",
            }:
                continue
            if retry_state == "retrying" and status == "needs_selection":
                # A crash can land after the search-safe cascade transaction
                # rejects stale metadata but before the scheduler's cleanup.
                # Preserve the no-prompt retry contract and restore the exact
                # failed shape required by a later atomic claim.
                connection.execute(
                    """
                    UPDATE requests
                    SET status = 'failed',
                        error = 'Stored ebook identity could not be resolved exactly during silent retry',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'needs_selection'
                    """,
                    (int(row["request_id"]),),
                )
                status = "failed"
            if status in {"processing", "new"}:
                # Search-only ebook cascades are resumed by RequestProcessor;
                # crossed mutation boundaries are handled by the established
                # backend reconciliation paths.
                continue
            if status == "queued":
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'awaiting_import', next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (int(row["request_id"]),),
                )
                continue
            if status in {"complete", "completed"}:
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'fulfilled', final_import_state = 'verified',
                        fulfilled_at = COALESCE(fulfilled_at, CURRENT_TIMESTAMP),
                        next_retry_at = NULL, expired_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ?
                      AND state IN ('retrying', 'awaiting_import')
                    """,
                    (int(row["request_id"]),),
                )
                connection.execute(
                    "UPDATE requests SET notified_at = NULL WHERE id = ?",
                    (int(row["request_id"]),),
                )
                continue
            last_retry = str(row["last_retry_at"] or "")
            if int(row["retry_count"]) >= UNAVAILABLE_RETRY_LIMIT:
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'expired', next_retry_at = NULL,
                        expired_at = COALESCE(expired_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (int(row["request_id"]),),
                )
                RequestStore._release_unowned_ebook_backend_reservations(
                    connection, int(row["request_id"])
                )
            else:
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'queued',
                        next_retry_at = datetime(?, '+30 days'),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (last_retry, int(row["request_id"])),
                )

    @staticmethod
    def _close_released_ebook_cascade(
        connection: sqlite3.Connection, request_id: int
    ) -> None:
        """Keep a released candidate target and its cascade ledger consistent."""

        cascade = connection.execute(
            """
            SELECT current_ordinal, mutation_backend FROM ebook_cascades
            WHERE request_id = ?
            """,
            (int(request_id),),
        ).fetchone()
        if cascade is None:
            return
        if cascade["mutation_backend"] is not None:
            raise EbookCascadeStateError(
                "A mutated ebook cascade cannot be released as a prompt failure"
            )
        connection.execute(
            """
            UPDATE ebook_backend_attempts
            SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
                outcome_message = COALESCE(
                    outcome_message, 'Candidate confirmation ended before mutation'
                )
            WHERE request_id = ? AND ordinal = ?
              AND status IN ('searching', 'awaiting_selection')
            """,
            (int(request_id), int(cascade["current_ordinal"])),
        )
        connection.execute(
            """
            UPDATE ebook_cascades
            SET state = 'failed', updated_at = CURRENT_TIMESTAMP
            WHERE request_id = ?
            """,
            (int(request_id),),
        )
        RequestStore._release_unowned_ebook_backend_reservations(
            connection, int(request_id)
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
            RequestStore._close_released_ebook_cascade(
                connection, int(row["request_id"])
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
        runs immediately before the service's non-idempotent request POST.  A
        claimed row without that marker after restart therefore cannot have
        reached the service and may be released without risking a duplicate.
        Rows with the marker remain owned for correlation recovery.
        """

        message = (
            "Huey restarted before the confirmed selection reached the acquisition "
            "service; submit the title again."
        )
        rows = connection.execute(
            """
            SELECT candidate_confirmations.id, candidate_confirmations.request_id
            FROM candidate_confirmations
            JOIN requests ON requests.id = candidate_confirmations.request_id
            WHERE candidate_confirmations.status = 'claimed'
              AND candidate_confirmations.dispatch_started_at IS NULL
              AND requests.status = 'processing'
              AND requests.service IN ('shelfarr', 'abba', 'lazylibrarian')
              AND requests.external_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ebook_cascades
                  WHERE ebook_cascades.request_id = requests.id
              )
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
                  AND service IN ('shelfarr', 'abba', 'lazylibrarian')
                  AND external_id IS NULL
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
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM ebook_cascades
                      WHERE ebook_cascades.request_id = requests.id
                  )
                  OR EXISTS (
                      SELECT 1 FROM ebook_cascades
                      WHERE ebook_cascades.request_id = requests.id
                        AND ebook_cascades.mutation_backend = 'shelfarr'
                  )
              )
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
    def _mark_interrupted_abba_requests(connection: sqlite3.Connection) -> None:
        """Keep only ABBA dispatches which crossed the durable grab boundary."""

        rows = connection.execute(
            """
            SELECT id FROM requests
            WHERE status = 'processing' AND service = 'abba'
              AND external_id IS NULL AND dispatch_started_at IS NOT NULL
              AND abba_candidate_id IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            exists = connection.execute(
                """
                SELECT 1 FROM events
                WHERE request_id = ? AND event_type = 'startup_abba_recovery'
                """,
                (row["id"],),
            ).fetchone()
            if exists is None:
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        row["id"],
                        "startup_abba_recovery",
                        "Huey restarted during an ABBA grab; recovering correlation",
                    ),
                )

    @staticmethod
    def _mark_interrupted_lazylibrarian_requests(
        connection: sqlite3.Connection,
    ) -> None:
        """Keep LL mutations owned without ever repeating their book search."""

        rows = connection.execute(
            """
            SELECT id FROM requests
            WHERE status = 'processing' AND service = 'lazylibrarian'
              AND external_id IS NULL AND dispatch_started_at IS NOT NULL
              AND lazylibrarian_book_id IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            exists = connection.execute(
                """
                SELECT 1 FROM events
                WHERE request_id = ?
                  AND event_type = 'startup_lazylibrarian_recovery'
                """,
                (row["id"],),
            ).fetchone()
            if exists is None:
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        row["id"],
                        "startup_lazylibrarian_recovery",
                        "Huey restarted during a LazyLibrarian dispatch; recovering exact history",
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
              AND NOT (
                  status = 'processing' AND media_type = 'ebooks'
                  AND EXISTS (
                      SELECT 1 FROM ebook_cascades
                      WHERE ebook_cascades.request_id = requests.id
                        AND ebook_cascades.mutation_backend IS NULL
                        AND ebook_cascades.state IN (
                            'searching', 'awaiting_selection'
                        )
                  )
              )
              AND NOT (status = 'processing' AND service = 'shelfarr')
              AND NOT (
                  status = 'processing' AND service = 'abba'
                  AND dispatch_started_at IS NOT NULL
                  AND abba_candidate_id IS NOT NULL
              )
              AND NOT (
                  status = 'processing' AND service = 'lazylibrarian'
                  AND dispatch_started_at IS NOT NULL
                  AND lazylibrarian_book_id IS NOT NULL
              )
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

    def interrupted_abba_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return ABBA grabs which crossed the durable dispatch boundary."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'processing'
                  AND service = 'abba'
                  AND external_id IS NULL
                  AND dispatch_started_at IS NOT NULL
                  AND abba_candidate_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM events
                      WHERE events.request_id = requests.id
                        AND events.event_type = 'startup_abba_recovery'
                  )
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def interrupted_lazylibrarian_requests(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return LL requests which crossed their durable mutation boundary."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'processing'
                  AND service = 'lazylibrarian'
                  AND external_id IS NULL
                  AND dispatch_started_at IS NOT NULL
                  AND lazylibrarian_book_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM events
                      WHERE events.request_id = requests.id
                        AND events.event_type = 'startup_lazylibrarian_recovery'
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
        retry_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'unavailable_retries'"
        ).fetchone()
        if retry_table is not None:
            retry_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(unavailable_retries)"
                ).fetchall()
            }
            for name, definition in _UNAVAILABLE_RETRY_COLUMNS.items():
                if name not in retry_columns:
                    connection.execute(
                        f"ALTER TABLE unavailable_retries "
                        f"ADD COLUMN {name} {definition}"
                    )
        trusted_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'trusted_library_events'"
        ).fetchone()
        if trusted_table is not None:
            trusted_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(trusted_library_events)"
                ).fetchall()
            }
            for name, definition in _TRUSTED_LIBRARY_EVENT_COLUMNS.items():
                if name not in trusted_columns:
                    connection.execute(
                        f"ALTER TABLE trusted_library_events "
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
    def _coalesce_abba_row(
        connection: sqlite3.Connection,
        alias_request_id: int,
        canonical_request_id: int,
        *,
        reason: str,
    ) -> None:
        """Turn one duplicate ABBA row into an inert canonical-request alias."""

        alias_id = int(alias_request_id)
        owner_id = int(canonical_request_id)
        if alias_id == owner_id:
            raise sqlite3.IntegrityError("An ABBA request cannot alias itself")
        message = (
            "ABBA acquisition identity is already owned by canonical request "
            f"#{owner_id} ({reason})"
        )
        cursor = connection.execute(
            """
            UPDATE requests
            SET status = 'failed', canonical_request_id = ?,
                external_status = 'canonical_duplicate', error = NULL,
                notified_at = COALESCE(notified_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND service = 'abba'
              AND canonical_request_id IS NULL
            """,
            (owner_id, alias_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("ABBA duplicate could not be aliased")
        connection.execute(
            """
            UPDATE requests
            SET canonical_request_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE canonical_request_id = ?
            """,
            (owner_id, alias_id),
        )
        connection.execute(
            """
            UPDATE delivery_aliases
            SET request_id = ?
            WHERE request_id = ?
            """,
            (owner_id, alias_id),
        )
        connection.execute(
            """
            UPDATE candidate_confirmations
            SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
                failure_message = ?
            WHERE request_id = ? AND status IN ('pending', 'claimed')
            """,
            (message[:500], alias_id),
        )
        delivery_ids = {
            str(row["message_id"])
            for row in connection.execute(
                "SELECT message_id FROM requests WHERE id = ?", (alias_id,)
            ).fetchall()
        }
        delivery_ids.update(
            str(row["reply_message_id"])
            for row in connection.execute(
                """
                SELECT candidate_confirmation_replies.reply_message_id
                FROM candidate_confirmation_replies
                JOIN candidate_confirmations
                  ON candidate_confirmations.id =
                     candidate_confirmation_replies.confirmation_id
                WHERE candidate_confirmations.request_id = ?
                """,
                (alias_id,),
            ).fetchall()
        )
        for delivery_id in delivery_ids:
            connection.execute(
                """
                INSERT OR REPLACE INTO delivery_aliases (message_id, request_id)
                VALUES (?, ?)
                """,
                (delivery_id, owner_id),
            )
        connection.execute(
            "DELETE FROM notification_deliveries "
            "WHERE request_id = ? AND delivered_at IS NULL",
            (alias_id,),
        )
        connection.execute(
            "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
            (alias_id, "abba_canonical_alias", message),
        )
        connection.execute(
            "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
            (
                owner_id,
                "abba_duplicate_coalesced",
                f"Coalesced duplicate ABBA request #{alias_id} ({reason})",
            ),
        )

    @classmethod
    def _migrate_abba_collisions(cls, connection: sqlite3.Connection) -> None:
        """Deterministically reconcile ABBA collisions created by older code."""

        active = "'processing', 'queued', 'complete', 'completed'"
        # Resolve candidate identity before hash identity.  Otherwise a row
        # whose candidate changed from X to Y can be hidden as a harmless hash
        # alias before the candidate conflict is inspected.  Equal non-null
        # hashes are deliberately left unlinked here: the global hash pass
        # below will point every duplicate directly at the ultimate root and
        # cannot create an alias chain.
        duplicate_candidates = connection.execute(
            f"""
            SELECT abba_candidate_id
            FROM requests
            WHERE service = 'abba' AND abba_candidate_id IS NOT NULL
              AND canonical_request_id IS NULL
              AND status IN ({active})
            GROUP BY abba_candidate_id
            HAVING COUNT(*) > 1
            ORDER BY abba_candidate_id
            """
        ).fetchall()
        for duplicate in duplicate_candidates:
            rows = connection.execute(
                f"""
                SELECT id, external_id, status
                FROM requests
                WHERE service = 'abba' AND abba_candidate_id = ?
                  AND canonical_request_id IS NULL
                  AND status IN ({active})
                ORDER BY
                    CASE
                        WHEN external_id IS NOT NULL
                          OR status IN ('queued', 'complete', 'completed')
                        THEN 0 ELSE 1
                    END,
                    id
                """,
                (duplicate["abba_candidate_id"],),
            ).fetchall()
            owner_id = int(rows[0]["id"])
            owner_hash = (
                None
                if rows[0]["external_id"] is None
                else str(rows[0]["external_id"]).casefold()
            )
            for conflict in rows[1:]:
                conflict_id = int(conflict["id"])
                conflict_hash = (
                    None
                    if conflict["external_id"] is None
                    else str(conflict["external_id"]).casefold()
                )
                if conflict_hash == owner_hash:
                    if owner_hash is None:
                        cls._coalesce_abba_row(
                            connection,
                            conflict_id,
                            owner_id,
                            reason="existing candidate collision migration",
                        )
                    continue

                # One opaque candidate resolving to a different hash is not a
                # safe alias.  Preserve the deterministic owner and quarantine
                # only the rows whose hash differs from it; equal-hash rows
                # remain eligible for the direct global hash election below.
                connection.execute(
                    """
                    UPDATE requests
                    SET status = 'failed',
                        external_status = 'candidate_identity_conflict',
                        error = 'ABBA candidate resolved to conflicting torrent hashes',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND canonical_request_id IS NULL
                    """,
                    (conflict_id,),
                )
                connection.execute(
                    """
                    UPDATE candidate_confirmations
                    SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
                        failure_message =
                            'ABBA candidate identity conflict requires review'
                    WHERE request_id = ? AND status IN ('pending', 'claimed')
                    """,
                    (conflict_id,),
                )
                connection.execute(
                    """
                    DELETE FROM notification_deliveries
                    WHERE request_id = ? AND delivered_at IS NULL
                    """,
                    (conflict_id,),
                )
                connection.execute(
                    """
                    INSERT INTO events (request_id, event_type, message)
                    VALUES (?, 'abba_candidate_identity_conflict', ?)
                    """,
                    (
                        conflict_id,
                        f"Candidate conflicts with canonical request #{owner_id}",
                    ),
                )

        duplicate_hashes = connection.execute(
            f"""
            SELECT lower(external_id) AS info_hash
            FROM requests
            WHERE service = 'abba' AND external_id IS NOT NULL
              AND canonical_request_id IS NULL
              AND status IN ({active})
            GROUP BY lower(external_id)
            HAVING COUNT(*) > 1
            ORDER BY lower(external_id)
            """
        ).fetchall()
        for duplicate in duplicate_hashes:
            rows = connection.execute(
                f"""
                SELECT id FROM requests
                WHERE service = 'abba' AND lower(external_id) = ?
                  AND canonical_request_id IS NULL
                  AND status IN ({active})
                ORDER BY id
                """,
                (duplicate["info_hash"],),
            ).fetchall()
            owner_id = int(rows[0]["id"])
            for alias in rows[1:]:
                cls._coalesce_abba_row(
                    connection,
                    int(alias["id"]),
                    owner_id,
                    reason="existing hash collision migration",
                )

    @staticmethod
    def _migrate_lazylibrarian_hash_collisions(
        connection: sqlite3.Connection,
    ) -> None:
        """Quarantine LL hash collisions permitted by the former active-only index.

        A shared qBittorrent hash does not prove that two LazyLibrarian BookIDs
        represent the same work, so these rows must never be coalesced as
        aliases.  Preserve one already completed owner when final-import proof
        exists, quarantine every nonterminal competitor, and detach only extra
        terminal rows from the hash reservation while retaining their completed
        state.  If no completed owner exists, every ambiguous active row is
        quarantined rather than authorizing an arbitrary download owner.
        """

        tracked = "'processing', 'queued', 'complete', 'completed'"
        duplicate_hashes = connection.execute(
            f"""
            SELECT lower(external_id) AS download_id
            FROM requests
            WHERE service = 'lazylibrarian' AND external_id IS NOT NULL
              AND status IN ({tracked})
            GROUP BY lower(external_id)
            HAVING COUNT(*) > 1
            ORDER BY lower(external_id)
            """
        ).fetchall()
        for duplicate in duplicate_hashes:
            rows = connection.execute(
                f"""
                SELECT id, status FROM requests
                WHERE service = 'lazylibrarian'
                  AND lower(external_id) = ?
                  AND status IN ({tracked})
                ORDER BY
                    CASE WHEN status IN ('complete', 'completed') THEN 0 ELSE 1 END,
                    id
                """,
                (duplicate["download_id"],),
            ).fetchall()
            terminal = [
                row for row in rows if row["status"] in {"complete", "completed"}
            ]
            owner_id = int(terminal[0]["id"]) if terminal else None

            for row in rows:
                request_id = int(row["id"])
                if owner_id is not None and request_id == owner_id:
                    continue
                if row["status"] in {"complete", "completed"}:
                    # The final-library result remains authoritative, but only
                    # one terminal request may retain the downloader identity.
                    connection.execute(
                        """
                        UPDATE requests
                        SET external_id = NULL,
                            external_status =
                                'lazylibrarian_hash_identity_conflict',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND service = 'lazylibrarian'
                          AND status IN ('complete', 'completed')
                        """,
                        (request_id,),
                    )
                    message = (
                        "Detached a duplicate terminal LazyLibrarian hash from "
                        f"canonical completed request #{owner_id}"
                    )
                else:
                    message = (
                        "Quarantined an ambiguous LazyLibrarian hash owned by "
                        f"completed request #{owner_id}"
                        if owner_id is not None
                        else "Quarantined an ambiguous LazyLibrarian hash with no "
                        "completed owner"
                    )
                    # Promote retry ownership before the request failure trigger
                    # runs, so its backend reservation remains durable for
                    # exact final-proof recovery.
                    connection.execute(
                        """
                        UPDATE unavailable_retries
                        SET state = 'blocked', next_retry_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE request_id = ?
                          AND state IN (
                              'queued', 'retrying', 'awaiting_import', 'blocked'
                          )
                        """,
                        (request_id,),
                    )
                    cursor = connection.execute(
                        """
                        UPDATE requests
                        SET status = 'failed',
                            external_status =
                                'lazylibrarian_hash_identity_conflict',
                            error =
                                'LazyLibrarian download identity conflict requires operator review',
                            notified_at = COALESCE(
                                notified_at, CURRENT_TIMESTAMP
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND service = 'lazylibrarian'
                          AND status IN ('processing', 'queued')
                        """,
                        (request_id,),
                    )
                    if cursor.rowcount != 1:
                        raise sqlite3.IntegrityError(
                            "LazyLibrarian hash conflict could not be quarantined"
                        )
                    connection.execute(
                        """
                        UPDATE candidate_confirmations
                        SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
                            failure_message =
                                'LazyLibrarian download identity conflict requires review'
                        WHERE request_id = ? AND status IN ('pending', 'claimed')
                        """,
                        (request_id,),
                    )
                    connection.execute(
                        """
                        DELETE FROM notification_deliveries
                        WHERE request_id = ? AND delivered_at IS NULL
                        """,
                        (request_id,),
                    )

                connection.execute(
                    """
                    INSERT INTO events (request_id, event_type, message)
                    VALUES (?, 'lazylibrarian_hash_collision_migrated', ?)
                    """,
                    (request_id, message),
                )
                if owner_id is not None:
                    connection.execute(
                        """
                        INSERT INTO events (request_id, event_type, message)
                        VALUES (?, 'lazylibrarian_hash_owner_preserved', ?)
                        """,
                        (
                            owner_id,
                            f"Preserved completed owner over legacy request #{request_id}",
                        ),
                    )

        remaining = connection.execute(
            f"""
            SELECT lower(external_id) AS download_id
            FROM requests
            WHERE service = 'lazylibrarian' AND external_id IS NOT NULL
              AND status IN ({tracked})
            GROUP BY lower(external_id)
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if remaining is not None:
            raise sqlite3.IntegrityError(
                "LazyLibrarian hash collision migration left ambiguous ownership"
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
                    dispatch_started_at = COALESCE(?, dispatch_started_at),
                    abba_candidate_id = COALESCE(?, abba_candidate_id),
                    lazylibrarian_book_id = COALESCE(?, lazylibrarian_book_id),
                    canonical_request_id = COALESCE(?, canonical_request_id),
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
                    latest["dispatch_started_at"],
                    latest["abba_candidate_id"],
                    latest["lazylibrarian_book_id"],
                    latest["canonical_request_id"],
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

    @staticmethod
    def _unavailable_retry_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        try:
            metadata = json.loads(str(value["metadata_json"]))
            value["metadata"] = _normalize_candidate_snapshot(
                metadata, request_media_type=str(value["media_type"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise sqlite3.DatabaseError("Unavailable retry metadata is invalid") from error
        return value

    def get_unavailable_retry(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM unavailable_retries WHERE request_id = ?",
                (int(request_id),),
            ).fetchone()
        return self._unavailable_retry_row(row)

    def list_unavailable_retries(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return retry records for operator inspection in stable due order."""

        if state is not None and state not in {
            "queued",
            "retrying",
            "awaiting_import",
            "blocked",
            "fulfilled",
            "expired",
        }:
            raise ValueError("Unknown unavailable retry state")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM unavailable_retries
                WHERE (? IS NULL OR state = ?)
                ORDER BY COALESCE(next_retry_at, updated_at), request_id
                LIMIT ?
                """,
                (state, state, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [
            value
            for row in rows
            if (value := self._unavailable_retry_row(row)) is not None
        ]

    def unavailable_retry_is_silent(self, request_id: int) -> bool:
        """Return whether routine lifecycle Discord traffic must be suppressed."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM unavailable_retries
                WHERE request_id = ?
                  AND state IN ('retrying', 'awaiting_import', 'blocked')
                """,
                (int(request_id),),
            ).fetchone()
        return row is not None

    def force_unavailable_retry(
        self, request_id: int, *, now: datetime | None = None
    ) -> bool:
        """Make a queued, pre-mutation retry immediately eligible."""

        timestamp = _selection_timestamp(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE unavailable_retries
                SET next_retry_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND state = 'queued'
                """,
                (timestamp, int(request_id)),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO events (request_id, event_type, message)
                    VALUES (?, 'unavailable_retry_forced',
                            'Operator made the queued unavailable retry immediately eligible')
                    """,
                    (int(request_id),),
                )
            return cursor.rowcount == 1

    def claim_due_unavailable_retries(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Atomically rearm due ebook cascades while preserving canonical identity."""

        timestamp = _selection_timestamp(now)
        claimed: list[dict[str, Any]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT unavailable_retries.request_id
                FROM unavailable_retries
                JOIN requests ON requests.id = unavailable_retries.request_id
                JOIN ebook_cascades
                  ON ebook_cascades.request_id = unavailable_retries.request_id
                WHERE unavailable_retries.state = 'queued'
                  AND unavailable_retries.next_retry_at <= ?
                  AND unavailable_retries.retry_count < ?
                  AND requests.status = 'failed'
                  AND ebook_cascades.state = 'failed'
                  AND ebook_cascades.mutation_backend IS NULL
                  AND ebook_cascades.identity_key = unavailable_retries.identity_key
                ORDER BY unavailable_retries.next_retry_at,
                         unavailable_retries.request_id
                LIMIT ?
                """,
                (
                    timestamp,
                    UNAVAILABLE_RETRY_LIMIT,
                    max(1, min(int(limit), 1000)),
                ),
            ).fetchall()
            for selected in rows:
                request_id = int(selected["request_id"])
                cascade = self._ebook_cascade_row(connection, request_id)
                if cascade is None or not cascade["policy"]:
                    continue
                first_backend = str(cascade["policy"][0])
                attempts = connection.execute(
                    """
                    UPDATE ebook_backend_attempts
                    SET status = 'pending', started_at = NULL,
                        finished_at = NULL, mutation_started_at = NULL,
                        mutation_resolved_at = NULL,
                        external_id = NULL, external_status = NULL,
                        outcome_message = NULL
                    WHERE request_id = ?
                    """,
                    (request_id,),
                )
                cascade_cursor = connection.execute(
                    """
                    UPDATE ebook_cascades
                    SET current_ordinal = 0, state = 'searching',
                        mutation_backend = NULL, mutation_started_at = NULL,
                        final_backend = NULL, finalizer = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'failed'
                      AND mutation_backend IS NULL
                    """,
                    (request_id,),
                )
                request_cursor = connection.execute(
                    """
                    UPDATE requests
                    SET status = 'processing', service = ?, external_id = NULL,
                        external_status = NULL, external_title = NULL,
                        dispatch_started_at = NULL, lazylibrarian_book_id = NULL,
                        error = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'failed'
                    """,
                    (first_backend, request_id),
                )
                retry_cursor = connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'retrying', retry_count = retry_count + 1,
                        last_retry_at = ?, next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'queued'
                    """,
                    (timestamp, request_id),
                )
                if (
                    attempts.rowcount != len(cascade["policy"])
                    or cascade_cursor.rowcount != 1
                    or request_cursor.rowcount != 1
                    or retry_cursor.rowcount != 1
                ):
                    raise EbookCascadeStateError(
                        "Unavailable retry could not be claimed atomically"
                    )
                connection.execute(
                    """
                    INSERT INTO events (request_id, event_type, message)
                    VALUES (?, 'unavailable_retry_started',
                            'Started one due silent unavailable retry')
                    """,
                    (request_id,),
                )
                request = connection.execute(
                    "SELECT * FROM requests WHERE id = ?", (request_id,)
                ).fetchone()
                if request is None:  # pragma: no cover - transaction invariant
                    raise sqlite3.DatabaseError("Unavailable retry request disappeared")
                claimed.append(dict(request))
        return claimed

    @staticmethod
    def _finish_retry_miss(
        connection: sqlite3.Connection,
        request_id: int,
        *,
        now: datetime | None = None,
        event_type: str = "unavailable_retry_deferred",
    ) -> bool:
        row = connection.execute(
            """
            SELECT retry_count, last_retry_at FROM unavailable_retries
            WHERE request_id = ? AND state = 'retrying'
            """,
            (int(request_id),),
        ).fetchone()
        if row is None:
            return False
        moment = now or datetime.now(timezone.utc)
        timestamp = _selection_timestamp(moment)
        if int(row["retry_count"]) >= UNAVAILABLE_RETRY_LIMIT:
            connection.execute(
                """
                UPDATE unavailable_retries
                SET state = 'expired', next_retry_at = NULL,
                    expired_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND state = 'retrying'
                """,
                (timestamp, int(request_id)),
            )
            RequestStore._release_unowned_ebook_backend_reservations(
                connection, int(request_id)
            )
            event_type = "unavailable_retry_expired"
            message = "Unavailable retry reached its bounded retry ceiling"
        else:
            try:
                retry_started = datetime.fromisoformat(str(row["last_retry_at"]))
            except (TypeError, ValueError) as error:
                raise sqlite3.DatabaseError(
                    "Unavailable retry has an invalid last-retry timestamp"
                ) from error
            if retry_started.tzinfo is None or retry_started.utcoffset() is None:
                retry_started = retry_started.replace(tzinfo=timezone.utc)
            next_retry = _selection_timestamp(
                retry_started.astimezone(timezone.utc) + UNAVAILABLE_RETRY_INTERVAL
            )
            connection.execute(
                """
                UPDATE unavailable_retries
                SET state = 'queued', next_retry_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND state = 'retrying'
                """,
                (next_retry, int(request_id)),
            )
            message = "Silent unavailable retry ended before a safe handoff"
        connection.execute(
            "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
            (int(request_id), event_type, message),
        )
        return True

    def finish_unavailable_retry_attempt(
        self, request_id: int, *, now: datetime | None = None
    ) -> bool:
        """Close a retry left terminal by an operational pre-mutation failure."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retry = connection.execute(
                """
                SELECT state FROM unavailable_retries
                WHERE request_id = ?
                """,
                (int(request_id),),
            ).fetchone()
            if retry is None or retry["state"] != "retrying":
                return False
            request = connection.execute(
                "SELECT status FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            if request is None:
                raise KeyError(f"Unknown request ID: {request_id}")
            status = str(request["status"] or "")
            if status == "queued":
                cursor = connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'awaiting_import', next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (int(request_id),),
                )
                return cursor.rowcount == 1
            if status in {"complete", "completed"}:
                # The schema trigger normally owns this edge; this branch is a
                # restart/migration repair and preserves the same semantics.
                cursor = connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'fulfilled', final_import_state = 'verified',
                        fulfilled_at = COALESCE(fulfilled_at, CURRENT_TIMESTAMP),
                        next_retry_at = NULL, expired_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (int(request_id),),
                )
                connection.execute(
                    "UPDATE requests SET notified_at = NULL WHERE id = ?",
                    (int(request_id),),
                )
                return cursor.rowcount == 1
            if status == "needs_selection":
                # A background retry must never ask the requester to resolve
                # the same metadata again.  Normalize the pre-mutation stale
                # mapping back to the failed/search-safe shape required by the
                # next deterministic claim while retaining the canonical retry
                # identity and its silent ownership.
                cursor = connection.execute(
                    """
                    UPDATE requests
                    SET status = 'failed',
                        error = 'Stored ebook identity could not be resolved exactly during silent retry',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'needs_selection'
                    """,
                    (int(request_id),),
                )
                if cursor.rowcount != 1:
                    raise EbookCascadeStateError(
                        "Stale unavailable retry could not be normalized"
                    )
                status = "failed"
            if status == "failed":
                return self._finish_retry_miss(
                    connection, int(request_id), now=now
                )
            return False

    @staticmethod
    def _ebook_cascade_row(
        connection: sqlite3.Connection, request_id: int
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM ebook_cascades WHERE request_id = ?", (int(request_id),)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            policy = json.loads(str(value["policy_json"]))
            value["policy"] = _ebook_policy(policy)
            value["identity"] = (
                json.loads(str(value["identity_json"]))
                if value.get("identity_json") is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise sqlite3.DatabaseError("Ebook cascade state is invalid") from error
        attempts = connection.execute(
            """
            SELECT * FROM ebook_backend_attempts
            WHERE request_id = ? ORDER BY ordinal
            """,
            (int(request_id),),
        ).fetchall()
        reservations = connection.execute(
            """
            SELECT backend, backend_identity FROM ebook_backend_reservations
            WHERE request_id = ? ORDER BY backend, backend_identity
            """,
            (int(request_id),),
        ).fetchall()
        identities_by_backend: dict[str, list[str]] = {}
        for reservation in reservations:
            identities_by_backend.setdefault(str(reservation["backend"]), []).append(
                str(reservation["backend_identity"])
            )
        value["attempts"] = []
        for attempt_row in attempts:
            attempt = dict(attempt_row)
            attempt["backend_identities"] = tuple(
                identities_by_backend.get(str(attempt["backend"]), ())
            )
            value["attempts"].append(attempt)
        return value

    def get_ebook_cascade(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._ebook_cascade_row(connection, int(request_id))

    def create_ebook_cascade(
        self, request_id: int, backends: Sequence[str]
    ) -> dict[str, Any]:
        """Snapshot one immutable serial policy before its first search."""

        policy = _ebook_policy(backends)
        encoded = json.dumps(policy, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT media_type, status FROM requests WHERE id = ?",
                (int(request_id),),
            ).fetchone()
            if request is None:
                raise KeyError(f"Unknown request ID: {request_id}")
            if request["media_type"] != "ebooks":
                raise ValueError("Only ebook requests may own an ebook cascade")
            existing = self._ebook_cascade_row(connection, int(request_id))
            if existing is not None:
                if existing["policy"] != policy:
                    raise EbookCascadeStateError(
                        "Persisted ebook backend policy cannot be changed"
                    )
                return existing
            if request["status"] != "new":
                raise EbookCascadeStateError(
                    "Ebook cascade must be reserved before request dispatch"
                )
            connection.execute(
                """
                INSERT INTO ebook_cascades (
                    request_id, policy_json, current_ordinal, state
                ) VALUES (?, ?, 0, 'searching')
                """,
                (int(request_id), encoded),
            )
            for ordinal, backend in enumerate(policy):
                connection.execute(
                    """
                    INSERT INTO ebook_backend_attempts (
                        request_id, ordinal, backend, status
                    ) VALUES (?, ?, ?, 'pending')
                    """,
                    (int(request_id), ordinal, backend),
                )
            connection.execute(
                """
                UPDATE requests
                SET status = 'processing', service = ?,
                    updated_at = CURRENT_TIMESTAMP, error = NULL
                WHERE id = ? AND status = 'new'
                """,
                (policy[0], int(request_id)),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "ebook_cascade_started",
                    "Started the configured serial ebook acquisition policy",
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    request_id, event_key, route, message
                ) VALUES (?, 'request_accepted', 'request-status', ?)
                """,
                (
                    int(request_id),
                    f"✅ Request #{int(request_id)} accepted: Huey is searching "
                    "for a usable ebook release.",
                ),
            )
            value = self._ebook_cascade_row(connection, int(request_id))
            if value is None:  # pragma: no cover - transaction invariant
                raise sqlite3.DatabaseError("Ebook cascade was not persisted")
            return value

    def begin_ebook_attempt(
        self, request_id: int, backend: str | None = None
    ) -> dict[str, Any]:
        """Begin or safely replay the current search-only attempt."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cascade = self._ebook_cascade_row(connection, int(request_id))
            if cascade is None:
                raise EbookCascadeStateError("Request has no ebook cascade")
            ordinal = int(cascade["current_ordinal"])
            policy = cascade["policy"]
            if ordinal >= len(policy):
                raise EbookCascadeStateError("Ebook cascade is exhausted")
            expected = policy[ordinal]
            if backend is not None and backend != expected:
                raise EbookCascadeStateError("Ebook backend is not the current attempt")
            if cascade["mutation_backend"] is not None:
                raise EbookCascadeStateError(
                    "Ebook mutation is already owned and cannot be redispatched"
                )
            if cascade["state"] not in {"searching", "awaiting_selection"}:
                raise EbookCascadeStateError("Ebook cascade is not resumable")
            attempt = cascade["attempts"][ordinal]
            if attempt["status"] not in {
                "pending",
                "searching",
                "awaiting_selection",
            }:
                raise EbookCascadeStateError("Ebook attempt is not search-safe")
            first_start = attempt["status"] == "pending"
            attempt_cursor = connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET status = 'searching',
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
                WHERE request_id = ? AND ordinal = ?
                """,
                (int(request_id), ordinal),
            )
            cascade_cursor = connection.execute(
                """
                UPDATE ebook_cascades
                SET state = 'searching', updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ?
                """,
                (int(request_id),),
            )
            request_cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'processing', service = ?, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'processing'
                """,
                (expected, int(request_id)),
            )
            if request_cursor.rowcount != 1:
                raise EbookCascadeStateError("Ebook request is no longer processing")
            if first_start:
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        int(request_id),
                        "ebook_backend_search",
                        f"Searching ebook backend attempt {ordinal + 1}",
                    ),
                )
            value = self._ebook_cascade_row(connection, int(request_id))
            if value is None:  # pragma: no cover
                raise sqlite3.DatabaseError("Ebook cascade disappeared")
            return value

    def set_ebook_identity(
        self,
        request_id: int,
        backend: str,
        identity: Mapping[str, Any],
        *,
        backend_identity: object | None = None,
        backend_aliases: Sequence[object] = (),
    ) -> bool:
        """Atomically reserve one resolved work and optional provider ID."""

        if backend not in _EBOOK_BACKENDS:
            raise ValueError("Unsupported ebook backend")
        normalized = _normalize_candidate_snapshot(
            identity, request_media_type="ebooks"
        )
        identity_key = _ebook_identity_key(normalized)
        encoded = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if isinstance(backend_aliases, (str, bytes)) or len(backend_aliases) > 8:
            raise ValueError("Ebook backend aliases must be a bounded sequence")
        provider_identity = (
            _ebook_backend_identity(backend, backend_identity)
            if backend_identity is not None
            else None
        )
        provider_identities = []
        for raw_identity in (
            *((provider_identity,) if provider_identity is not None else ()),
            *backend_aliases,
        ):
            normalized_provider = _ebook_backend_identity(backend, raw_identity)
            if normalized_provider not in provider_identities:
                provider_identities.append(normalized_provider)
        if provider_identity is not None and provider_identity not in provider_identities:
            raise ValueError("Primary ebook backend identity is not reserved")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retry_owner = connection.execute(
                """
                SELECT request_id FROM unavailable_retries
                WHERE identity_key = ? AND request_id != ?
                  AND state IN ('queued', 'retrying', 'awaiting_import', 'blocked')
                ORDER BY request_id LIMIT 1
                """,
                (identity_key, int(request_id)),
            ).fetchone()
            if retry_owner is not None:
                raise EbookIdentityCollision(int(retry_owner["request_id"]))
            cascade = self._ebook_cascade_row(connection, int(request_id))
            if cascade is None:
                raise EbookCascadeStateError("Request has no ebook cascade")
            ordinal = int(cascade["current_ordinal"])
            if cascade["policy"][ordinal] != backend:
                raise EbookCascadeStateError("Identity came from a non-current backend")
            attempt = cascade["attempts"][ordinal]
            if (
                cascade["state"] not in {"searching", "awaiting_selection"}
                or attempt["status"] not in {"searching", "awaiting_selection"}
            ):
                raise EbookCascadeStateError(
                    "Ebook identity cannot be changed outside a search-safe attempt"
                )
            existing_key = cascade.get("identity_key")
            if existing_key is not None and existing_key != identity_key:
                raise EbookCascadeStateError(
                    "Ebook backend resolved a different work identity"
                )
            if existing_key is None:
                try:
                    connection.execute(
                        """
                        UPDATE ebook_cascades
                        SET identity_key = ?, identity_fingerprint = ?,
                            identity_json = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE request_id = ? AND identity_key IS NULL
                        """,
                        (
                            identity_key,
                            normalized["fingerprint"],
                            encoded,
                            int(request_id),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    owner = connection.execute(
                        """
                        SELECT request_id FROM ebook_cascades
                        WHERE identity_key = ? AND request_id != ?
                          AND state IN (
                              'searching', 'awaiting_selection', 'mutating',
                              'uncertain', 'queued', 'completed'
                          )
                        ORDER BY request_id LIMIT 1
                        """,
                        (identity_key, int(request_id)),
                    ).fetchone()
                    if owner is not None:
                        raise EbookIdentityCollision(owner["request_id"]) from error
                    raise
            if provider_identity is not None:
                if attempt.get("backend_identity") not in {None, provider_identity}:
                    raise EbookCascadeStateError(
                        "Ebook attempt already reserved a different backend identity"
                    )
                for reserved_identity in provider_identities:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO ebook_backend_reservations (
                            backend, backend_identity, request_id
                        ) VALUES (?, ?, ?)
                        """,
                        (backend, reserved_identity, int(request_id)),
                    )
                    owner = connection.execute(
                        """
                        SELECT request_id FROM ebook_backend_reservations
                        WHERE backend = ? AND backend_identity = ?
                        """,
                        (backend, reserved_identity),
                    ).fetchone()
                    if owner is None:  # pragma: no cover
                        raise sqlite3.DatabaseError("Ebook reservation disappeared")
                    if int(owner["request_id"]) != int(request_id):
                        raise EbookIdentityCollision(owner["request_id"])
                connection.execute(
                    """
                    UPDATE ebook_backend_attempts
                    SET backend_identity = COALESCE(backend_identity, ?)
                    WHERE request_id = ? AND ordinal = ?
                      AND backend = ?
                    """,
                    (provider_identity, int(request_id), ordinal, backend),
                )
            return existing_key is None

    def lock_ebook_mutation(
        self,
        request_id: int,
        backend: str,
        *,
        backend_identity: object | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Cross the request-wide mutation boundary for exactly one backend."""

        if backend not in _EBOOK_BACKENDS:
            raise ValueError("Unsupported ebook backend")
        provider_identity = (
            _ebook_backend_identity(backend, backend_identity)
            if backend_identity is not None
            else None
        )
        observed_at = _selection_timestamp(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cascade = self._ebook_cascade_row(connection, int(request_id))
            if cascade is None:
                raise EbookCascadeStateError("Request has no ebook cascade")
            ordinal = int(cascade["current_ordinal"])
            if cascade["policy"][ordinal] != backend:
                return False
            attempt = cascade["attempts"][ordinal]
            if cascade["mutation_backend"] is not None:
                # A pre-existing lock is reconciliation-only. Returning true
                # here would authorize a replayed callback to POST twice.
                return False
            if cascade["state"] != "searching" or attempt["status"] != "searching":
                return False
            if cascade.get("identity_key") is None:
                raise EbookCascadeStateError(
                    "Ebook work identity must be reserved before mutation"
                )
            if provider_identity is not None and attempt.get("backend_identity") not in {
                None,
                provider_identity,
            }:
                return False
            reserved_identity = provider_identity or attempt.get("backend_identity")
            if reserved_identity is None:
                raise EbookCascadeStateError(
                    "Backend identity must be reserved before mutation"
                )
            reservation = connection.execute(
                """
                SELECT request_id FROM ebook_backend_reservations
                WHERE backend = ? AND backend_identity = ?
                """,
                (backend, reserved_identity),
            ).fetchone()
            if reservation is None or int(reservation["request_id"]) != int(request_id):
                raise EbookCascadeStateError(
                    "Backend mutation identity is not owned by this request"
                )
            cascade_cursor = connection.execute(
                """
                UPDATE ebook_cascades
                SET state = 'mutating', mutation_backend = ?,
                    mutation_started_at = ?, updated_at = ?
                WHERE request_id = ? AND mutation_backend IS NULL
                """,
                (backend, observed_at, observed_at, int(request_id)),
            )
            if cascade_cursor.rowcount != 1:
                raise EbookCascadeStateError(
                    "Ebook cascade mutation marker could not be persisted"
                )
            attempt_cursor = connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET status = 'mutating', mutation_started_at = ?,
                    backend_identity = COALESCE(backend_identity, ?)
                WHERE request_id = ? AND ordinal = ? AND status = 'searching'
                """,
                (
                    observed_at,
                    provider_identity,
                    int(request_id),
                    ordinal,
                ),
            )
            if attempt_cursor.rowcount != 1:
                raise EbookCascadeStateError(
                    "Ebook attempt mutation marker could not be persisted"
                )
            request_cursor = connection.execute(
                """
                UPDATE requests
                SET dispatch_started_at = ?, updated_at = ?,
                    lazylibrarian_book_id = CASE
                        WHEN ? = 'lazylibrarian' THEN ?
                        ELSE lazylibrarian_book_id
                    END
                WHERE id = ? AND status = 'processing' AND service = ?
                """,
                (
                    observed_at,
                    observed_at,
                    backend,
                    provider_identity,
                    int(request_id),
                    backend,
                ),
            )
            if request_cursor.rowcount != 1:
                raise EbookCascadeStateError(
                    "Ebook request mutation marker could not be persisted"
                )
            connection.execute(
                """
                UPDATE candidate_confirmations
                SET dispatch_started_at = COALESCE(dispatch_started_at, ?),
                    updated_at = ?
                WHERE request_id = ? AND status = 'claimed'
                """,
                (observed_at, observed_at, int(request_id)),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "ebook_mutation_started",
                    f"Ebook backend attempt {ordinal + 1} crossed its mutation boundary",
                ),
            )
            return True

    def resumable_ebook_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return only cascade attempts proven not to have crossed mutation."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT requests.* FROM requests
                JOIN ebook_cascades ON ebook_cascades.request_id = requests.id
                WHERE requests.media_type = 'ebooks'
                  AND requests.status = 'processing'
                  AND ebook_cascades.mutation_backend IS NULL
                  AND (
                      ebook_cascades.state = 'searching'
                      OR (
                          ebook_cascades.state = 'awaiting_selection'
                          AND EXISTS (
                              SELECT 1 FROM candidate_confirmations
                              WHERE candidate_confirmations.request_id = requests.id
                                AND candidate_confirmations.status = 'claimed'
                                AND candidate_confirmations.dispatch_started_at IS NULL
                          )
                      )
                  )
                ORDER BY requests.updated_at, requests.id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _normalized_deliveries(
        notifications: Sequence[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        normalized: list[tuple[str, str, str]] = []
        for delivery in notifications:
            if (
                not isinstance(delivery, (list, tuple))
                or len(delivery) != 3
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in delivery
                )
            ):
                raise ValueError("Notification delivery fields cannot be empty")
            normalized.append(
                (delivery[0].strip(), delivery[1].strip(), delivery[2].strip()[:2000])
            )
        return normalized

    def advance_ebook_backend(
        self,
        request_id: int,
        backend: str,
        outcome: str,
        handler_result: Mapping[str, Any],
        *,
        final_message: str,
        notifications: Sequence[tuple[str, str, str]] = (),
        now: datetime | None = None,
    ) -> str | None:
        """CAS-advance after a safe miss/unavailable outcome, or exhaust once."""

        if backend not in _EBOOK_BACKENDS or outcome not in {"miss", "unavailable"}:
            raise ValueError("Invalid ebook backend advancement")
        if outcome == "miss" and handler_result.get("backend_outcome") != "miss":
            raise ValueError("Ebook miss advancement requires a typed miss result")
        if not final_message or not final_message.strip():
            raise ValueError("Ebook exhaustion requires a user-safe message")
        deliveries = self._normalized_deliveries(notifications)
        moment = now or datetime.now(timezone.utc)
        timestamp = _selection_timestamp(moment)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cascade = self._ebook_cascade_row(connection, int(request_id))
            if cascade is None:
                raise EbookCascadeStateError("Request has no ebook cascade")
            ordinal = int(cascade["current_ordinal"])
            if cascade["policy"][ordinal] != backend:
                raise EbookCascadeStateError("Ebook backend is not current")
            attempt = cascade["attempts"][ordinal]
            mutation_backend = cascade.get("mutation_backend")
            if mutation_backend is not None:
                raise EbookCascadeStateError(
                    "Post-mutation ebook outcome cannot advance safely"
                )
            if cascade["state"] != "searching" or attempt["status"] != "searching":
                raise EbookCascadeStateError(
                    "Only a search-only ebook attempt may advance"
                )

            finished_status = "miss" if outcome == "miss" else "unavailable"
            attempt_cursor = connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET status = ?, finished_at = CURRENT_TIMESTAMP,
                    mutation_resolved_at = CASE
                        WHEN mutation_started_at IS NOT NULL THEN CURRENT_TIMESTAMP
                        ELSE mutation_resolved_at
                    END,
                    external_status = ?,
                    outcome_message = ?
                WHERE request_id = ? AND ordinal = ?
                """,
                (
                    finished_status,
                    handler_result.get("external_status"),
                    str(handler_result.get("message") or "")[:1000],
                    int(request_id),
                    ordinal,
                ),
            )
            if attempt_cursor.rowcount != 1:
                raise EbookCascadeStateError("Ebook attempt could not be advanced")
            retry = connection.execute(
                """
                SELECT state FROM unavailable_retries WHERE request_id = ?
                """,
                (int(request_id),),
            ).fetchone()
            silent_retry = retry is not None and retry["state"] == "retrying"
            if silent_retry:
                deliveries = []
            next_ordinal = ordinal + 1
            if next_ordinal < len(cascade["policy"]):
                next_backend = cascade["policy"][next_ordinal]
                cascade_cursor = connection.execute(
                    """
                    UPDATE ebook_cascades
                    SET current_ordinal = ?, state = 'searching',
                        mutation_backend = NULL, mutation_started_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND current_ordinal = ?
                    """,
                    (next_ordinal, int(request_id), ordinal),
                )
                request_cursor = connection.execute(
                    """
                    UPDATE requests
                    SET status = 'processing', service = ?, external_id = NULL,
                        external_status = NULL, external_title = NULL,
                        dispatch_started_at = NULL, error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'processing'
                    """,
                    (next_backend, int(request_id)),
                )
                if cascade_cursor.rowcount != 1 or request_cursor.rowcount != 1:
                    raise EbookCascadeStateError(
                        "Ebook backend advancement could not be persisted atomically"
                    )
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        int(request_id),
                        f"ebook_backend_{finished_status}",
                        f"Ebook backend attempt {ordinal + 1} ended before a handoff; advancing serially",
                    ),
                )
                return next_backend

            cascade_cursor = connection.execute(
                """
                UPDATE ebook_cascades
                SET state = 'failed', mutation_backend = NULL,
                    mutation_started_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND current_ordinal = ?
                """,
                (int(request_id), ordinal),
            )
            request_cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
                    service = ?, external_id = NULL, external_status = NULL,
                    external_title = NULL, dispatch_started_at = NULL,
                    error = ?
                WHERE id = ? AND status = 'processing'
                """,
                (backend, final_message.strip()[:1000], int(request_id)),
            )
            if cascade_cursor.rowcount != 1 or request_cursor.rowcount != 1:
                raise EbookCascadeStateError(
                    "Ebook cascade exhaustion could not be persisted atomically"
                )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "ebook_cascade_exhausted",
                    "Every configured ebook backend ended without a safe acquisition",
                ),
            )
            if silent_retry:
                self._finish_retry_miss(
                    connection,
                    int(request_id),
                    now=moment,
                    event_type="unavailable_retry_miss",
                )
            elif cascade.get("identity_key") is not None:
                attempt_summary = connection.execute(
                    """
                    SELECT COUNT(*) AS attempt_count,
                           SUM(CASE WHEN status = 'miss' THEN 1 ELSE 0 END) AS miss_count,
                           SUM(CASE WHEN external_status = 'not_found' THEN 1 ELSE 0 END)
                               AS release_miss_count
                    FROM ebook_backend_attempts WHERE request_id = ?
                    """,
                    (int(request_id),),
                ).fetchone()
                conclusively_unavailable = bool(
                    attempt_summary is not None
                    and int(attempt_summary["attempt_count"] or 0)
                    == len(cascade["policy"])
                    and int(attempt_summary["miss_count"] or 0)
                    == len(cascade["policy"])
                    and int(attempt_summary["release_miss_count"] or 0) >= 1
                )
                if conclusively_unavailable:
                    request = connection.execute(
                        "SELECT * FROM requests WHERE id = ?", (int(request_id),)
                    ).fetchone()
                    identity = cascade.get("identity")
                    if request is None or not isinstance(identity, Mapping):
                        raise EbookCascadeStateError(
                            "Unavailable retry has no canonical request identity"
                        )
                    active_owner = connection.execute(
                        """
                        SELECT request_id FROM unavailable_retries
                        WHERE identity_key = ? AND request_id != ?
                          AND state IN (
                              'queued', 'retrying', 'awaiting_import', 'blocked'
                          )
                        ORDER BY request_id LIMIT 1
                        """,
                        (cascade["identity_key"], int(request_id)),
                    ).fetchone()
                    if active_owner is None:
                        next_retry = _selection_timestamp(
                            moment + UNAVAILABLE_FIRST_RETRY_DELAY
                        )
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO unavailable_retries (
                                request_id, media_type, identity_key, metadata_json,
                                canonical_title, canonical_creator, canonical_year,
                                discord_user_id, discord_username, channel_id,
                                message_id, first_unavailable_at, next_retry_at
                            ) VALUES (?, 'ebooks', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                int(request_id),
                                cascade["identity_key"],
                                str(cascade["identity_json"]),
                                identity["title"],
                                identity.get("author"),
                                identity.get("year"),
                                request["discord_user_id"],
                                request["discord_username"],
                                request["channel_id"],
                                request["message_id"],
                                timestamp,
                                next_retry,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO events (request_id, event_type, message)
                            VALUES (?, 'unavailable_retry_queued',
                                    'Queued canonical ebook identity for silent retry')
                            """,
                            (int(request_id),),
                        )
            self._release_unowned_ebook_backend_reservations(
                connection, int(request_id)
            )
            for event_key, route, message in deliveries:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (int(request_id), event_key, route, message),
                )
            return None

    def persist_ebook_result(
        self,
        request_id: int,
        backend: str,
        handler_result: Mapping[str, Any],
        *,
        notifications: Sequence[tuple[str, str, str]] = (),
    ) -> dict[str, Any]:
        """Atomically finish one cascade attempt and its Huey request state."""

        if backend not in _EBOOK_BACKENDS:
            raise ValueError("Unsupported ebook backend")
        status = str(handler_result.get("status") or "")
        if status not in {"queued", "completed", "failed"}:
            raise ValueError("Unsupported durable ebook result")
        deliveries = self._normalized_deliveries(notifications)
        external_id = (
            str(handler_result["external_id"])
            if handler_result.get("external_id") is not None
            else None
        )
        if backend == "lazylibrarian" and external_id is not None:
            external_id = external_id.casefold()
            if not _DOWNLOAD_ID.fullmatch(external_id):
                raise ValueError("Invalid LazyLibrarian download ID")
        if (
            backend == "shelfarr"
            and external_id is not None
            and (not external_id.isdigit() or int(external_id) <= 0)
        ):
            raise ValueError("Invalid Shelfarr request ID")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retry_attempt = connection.execute(
                """
                SELECT 1 FROM unavailable_retries
                WHERE request_id = ? AND state = 'retrying'
                """,
                (int(request_id),),
            ).fetchone() is not None
            if retry_attempt:
                deliveries = []
            if backend == "lazylibrarian" and external_id is not None:
                duplicate = connection.execute(
                    """
                    SELECT requests.id FROM requests
                    WHERE requests.id != ?
                      AND requests.service = 'lazylibrarian'
                      AND lower(requests.external_id) = ?
                      AND (
                          requests.status IN (
                              'processing', 'queued', 'complete', 'completed'
                          )
                          OR requests.external_status =
                              'lazylibrarian_hash_identity_conflict'
                          OR EXISTS (
                              SELECT 1 FROM unavailable_retries
                              WHERE unavailable_retries.request_id = requests.id
                                AND unavailable_retries.state = 'blocked'
                          )
                      )
                    LIMIT 1
                    """,
                    (int(request_id), external_id),
                ).fetchone()
                if duplicate is not None:
                    raise LazyLibrarianHashCollision(
                        "Reserved LazyLibrarian download identity collision"
                    )
            cascade = self._ebook_cascade_row(connection, int(request_id))
            if cascade is None:
                raise EbookCascadeStateError("Request has no ebook cascade")
            ordinal = int(cascade["current_ordinal"])
            if cascade["policy"][ordinal] != backend:
                raise EbookCascadeStateError("Ebook backend is not current")
            attempt = cascade["attempts"][ordinal]
            uncertain = bool(
                status == "queued"
                and handler_result.get("external_status") == "submission_uncertain"
            )
            if uncertain:
                if not (
                    (
                        cascade.get("mutation_backend") == backend
                        and attempt["status"] == "mutating"
                    )
                    or (
                        cascade.get("mutation_backend") is None
                        and attempt["status"] == "searching"
                    )
                ):
                    raise EbookCascadeStateError(
                        "Ebook uncertainty is not attached to the current attempt"
                    )
                cascade_state = "uncertain"
                attempt_status = "uncertain"
            elif status in {"queued", "completed"}:
                if external_id is None:
                    raise EbookCascadeStateError(
                        "A successful ebook handoff requires an external ID"
                    )
                if cascade.get("identity_key") is None:
                    raise EbookCascadeStateError(
                        "A successful ebook attempt has no resolved identity"
                    )
                existing_attach = bool(
                    cascade.get("mutation_backend") is None
                    and cascade["state"] == "searching"
                    and attempt["status"] == "searching"
                )
                local_submission = bool(
                    cascade.get("mutation_backend") == backend
                    and cascade["state"] == "mutating"
                    and attempt["status"] == "mutating"
                )
                if not (existing_attach or local_submission):
                    raise EbookCascadeStateError(
                        "Successful ebook result has no valid handoff boundary"
                    )
                cascade_state = "queued" if status == "queued" else "completed"
                attempt_status = cascade_state
            else:
                if not (
                    cascade.get("mutation_backend") == backend
                    and cascade["state"] in {"mutating", "uncertain"}
                    and attempt["status"] == cascade["state"]
                ):
                    raise EbookCascadeStateError(
                        "A failed ebook result has no valid mutation boundary"
                    )
                cascade_state = "failed"
                attempt_status = "failed"

            final_backend = (
                backend
                if status in {"queued", "completed"} and not uncertain
                else None
            )
            finalizer = (
                None
                if final_backend is None
                else "bookbot"
                if backend == "lazylibrarian"
                else "shelfarr"
            )
            cascade_cursor = connection.execute(
                """
                UPDATE ebook_cascades
                SET state = ?, final_backend = COALESCE(final_backend, ?),
                    finalizer = COALESCE(finalizer, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND current_ordinal = ?
                """,
                (
                    cascade_state,
                    final_backend,
                    finalizer,
                    int(request_id),
                    ordinal,
                ),
            )
            if status == "failed":
                self._release_unowned_ebook_backend_reservations(
                    connection, int(request_id)
                )
            attempt_cursor = connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET status = ?, finished_at = CASE
                        WHEN ? IN ('completed', 'failed') THEN CURRENT_TIMESTAMP
                        ELSE finished_at
                    END,
                    mutation_resolved_at = CASE
                        WHEN mutation_started_at IS NOT NULL
                             AND ? IN ('queued', 'completed', 'failed')
                        THEN CURRENT_TIMESTAMP
                        ELSE mutation_resolved_at
                    END,
                    external_id = ?, external_status = ?, outcome_message = ?
                WHERE request_id = ? AND ordinal = ?
                """,
                (
                    attempt_status,
                    attempt_status,
                    attempt_status,
                    external_id,
                    handler_result.get("external_status"),
                    str(handler_result.get("message") or "")[:1000],
                    int(request_id),
                    ordinal,
                ),
            )
            if cascade_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                raise EbookCascadeStateError(
                    "Ebook result ledger could not be persisted atomically"
                )
            error = str(handler_result.get("message") or "") if status == "failed" else None
            try:
                cursor = connection.execute(
                    """
                    UPDATE requests
                    SET status = ?, service = ?, external_id = ?,
                        external_status = ?, external_title = ?, error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status IN ('processing', 'queued')
                      AND external_id IS NULL
                    """,
                    (
                        status,
                        backend,
                        external_id,
                        handler_result.get("external_status"),
                        handler_result.get("external_title"),
                        error,
                        int(request_id),
                    ),
                )
            except sqlite3.IntegrityError as collision:
                if backend == "lazylibrarian" and external_id is not None:
                    raise LazyLibrarianHashCollision(
                        "Active LazyLibrarian download identity collision"
                    ) from collision
                raise
            if cursor.rowcount != 1:
                raise EbookCascadeStateError("Ebook request is no longer processing")
            if retry_attempt and status == "queued":
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'awaiting_import', next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (int(request_id),),
                )
            elif retry_attempt and status == "failed":
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'blocked', next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (int(request_id),),
                )
            event_type = (
                f"{backend}_submission_uncertain"
                if uncertain
                else "handler_completed"
                if status == "completed"
                else f"ebook_cascade_{status}"
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    event_type,
                    str(handler_result.get("message") or "")[:2000],
                ),
            )
            for event_key, route, message in deliveries:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (int(request_id), event_key, route, message),
                )
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise sqlite3.DatabaseError("Ebook request disappeared")
            return dict(row)

    def terminalize_ebook_cascade(
        self,
        request_id: int,
        backend: str,
        status: str,
        message: str,
        *,
        notifications: Sequence[tuple[str, str, str]] = (),
        event_type: str = "ebook_cascade_failed",
        duplicate_owner_request_id: int | None = None,
    ) -> dict[str, Any]:
        """Close a non-mutating ambiguity/collision without backend fallback."""

        if backend not in _EBOOK_BACKENDS or status not in {
            "needs_selection",
            "failed",
        }:
            raise ValueError("Invalid ebook cascade terminalization")
        if not message or not message.strip():
            raise ValueError("Ebook terminalization requires a message")
        deliveries = self._normalized_deliveries(notifications)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retry_attempt = connection.execute(
                """
                SELECT 1 FROM unavailable_retries
                WHERE request_id = ? AND state = 'retrying'
                """,
                (int(request_id),),
            ).fetchone() is not None
            if retry_attempt:
                deliveries = []
            cascade = self._ebook_cascade_row(connection, int(request_id))
            if cascade is None:
                raise EbookCascadeStateError("Request has no ebook cascade")
            ordinal = int(cascade["current_ordinal"])
            if cascade["policy"][ordinal] != backend:
                raise EbookCascadeStateError("Ebook backend is not current")
            if cascade.get("mutation_backend") is not None:
                raise EbookCascadeStateError(
                    "A mutated ebook attempt cannot use pre-mutation terminalization"
                )
            attempt_cursor = connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
                    outcome_message = ?
                WHERE request_id = ? AND ordinal = ?
                  AND status IN ('searching', 'awaiting_selection')
                """,
                (message.strip()[:1000], int(request_id), ordinal),
            )
            cascade_cursor = connection.execute(
                """
                UPDATE ebook_cascades
                SET state = 'failed', updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND current_ordinal = ?
                  AND state IN ('searching', 'awaiting_selection')
                """,
                (int(request_id), ordinal),
            )
            self._release_unowned_ebook_backend_reservations(
                connection, int(request_id)
            )
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = ?, service = ?, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN ('processing', 'awaiting_selection')
                """,
                (
                    status,
                    backend,
                    message.strip()[:1000],
                    int(request_id),
                ),
            )
            if (
                attempt_cursor.rowcount != 1
                or cascade_cursor.rowcount != 1
                or cursor.rowcount != 1
            ):
                raise EbookCascadeStateError(
                    "Ebook terminalization could not be persisted atomically"
                )
            if duplicate_owner_request_id is not None:
                owner = connection.execute(
                    "SELECT id FROM requests WHERE id = ?",
                    (int(duplicate_owner_request_id),),
                ).fetchone()
                delivery = connection.execute(
                    "SELECT message_id FROM requests WHERE id = ?",
                    (int(request_id),),
                ).fetchone()
                if owner is None or delivery is None:
                    raise EbookCascadeStateError(
                        "Ebook identity collision owner could not be linked"
                    )
                # The redundant row remains as an internal audit, while future
                # Discord redelivery resolves to the canonical logical request.
                connection.execute(
                    "DELETE FROM notification_deliveries WHERE request_id = ?",
                    (int(request_id),),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO delivery_aliases (message_id, request_id)
                    VALUES (?, ?)
                    """,
                    (str(delivery["message_id"]), int(duplicate_owner_request_id)),
                )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (int(request_id), event_type, message.strip()[:2000]),
            )
            for event_key, route, notification_message in deliveries:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (int(request_id), event_key, route, notification_message),
                )
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise sqlite3.DatabaseError("Ebook request disappeared")
            return dict(row)

    def record_ebook_recovered_handoff(
        self,
        request_id: int,
        backend: str,
        external_id: object,
        external_title: str,
        external_status: str,
        message: str,
        *,
        backend_identity: object | None = None,
        event_type: str = "ebook_handoff_recovered",
        notifications: Sequence[tuple[str, str, str]] = (),
    ) -> bool:
        """Atomically bind a quarantined attempt to its exact recovered handoff."""

        if backend not in _EBOOK_BACKENDS:
            raise ValueError("Unsupported ebook backend")
        if not message or not message.strip():
            raise ValueError("Recovered ebook handoff requires a message")
        normalized_external_id = str(external_id or "")
        if not normalized_external_id:
            raise ValueError("Recovered ebook handoff requires an external ID")
        if backend == "lazylibrarian":
            normalized_external_id = normalized_external_id.casefold()
            if not _DOWNLOAD_ID.fullmatch(normalized_external_id):
                raise ValueError("Invalid LazyLibrarian download ID")
        elif not normalized_external_id.isdigit() or int(normalized_external_id) <= 0:
            raise ValueError("Invalid Shelfarr request ID")
        provider_identity = (
            _ebook_backend_identity(backend, backend_identity)
            if backend_identity is not None
            else None
        )
        deliveries = self._normalized_deliveries(notifications)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            retry_attempt = connection.execute(
                """
                SELECT 1 FROM unavailable_retries
                WHERE request_id = ? AND state = 'retrying'
                """,
                (int(request_id),),
            ).fetchone() is not None
            if retry_attempt:
                deliveries = []
            cascade = self._ebook_cascade_row(connection, int(request_id))
            if cascade is None:
                raise EbookCascadeStateError("Request has no ebook cascade")
            ordinal = int(cascade["current_ordinal"])
            attempt = cascade["attempts"][ordinal]
            owned_identities = set(attempt.get("backend_identities", ()))
            owned_identity = provider_identity or attempt.get("backend_identity")
            if (
                cascade["policy"][ordinal] != backend
                or attempt["backend"] != backend
                or attempt["status"] not in {"mutating", "uncertain"}
                or cascade["state"] not in {"mutating", "uncertain"}
                or owned_identity is None
                or (
                    provider_identity is not None
                    and provider_identity not in owned_identities
                )
                or attempt.get("backend_identity") not in owned_identities
            ):
                raise EbookCascadeStateError(
                    "Recovered handoff does not match the current ebook attempt"
                )
            reservation = connection.execute(
                """
                SELECT request_id FROM ebook_backend_reservations
                WHERE backend = ? AND backend_identity = ?
                """,
                (backend, owned_identity),
            ).fetchone()
            if reservation is None or int(reservation["request_id"]) != int(request_id):
                raise EbookCascadeStateError(
                    "Recovered ebook backend identity is not reserved"
                )
            if backend == "lazylibrarian":
                duplicate = connection.execute(
                    """
                    SELECT requests.id FROM requests
                    WHERE requests.id != ?
                      AND requests.service = 'lazylibrarian'
                      AND lower(requests.external_id) = ?
                      AND (
                          requests.status IN (
                              'processing', 'queued', 'complete', 'completed'
                          )
                          OR requests.external_status =
                              'lazylibrarian_hash_identity_conflict'
                          OR EXISTS (
                              SELECT 1 FROM unavailable_retries
                              WHERE unavailable_retries.request_id = requests.id
                                AND unavailable_retries.state = 'blocked'
                          )
                      )
                    LIMIT 1
                    """,
                    (int(request_id), normalized_external_id),
                ).fetchone()
                if duplicate is not None:
                    raise LazyLibrarianHashCollision(
                        "Active LazyLibrarian download identity collision"
                    )
            request_cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'queued', service = ?, external_id = ?,
                    external_status = ?, external_title = ?, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND service = ? AND external_id IS NULL
                  AND status IN ('processing', 'queued')
                """,
                (
                    backend,
                    normalized_external_id,
                    external_status,
                    external_title,
                    int(request_id),
                    backend,
                ),
            )
            if request_cursor.rowcount != 1:
                return False
            attempt_cursor = connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET status = 'queued', external_id = ?, external_status = ?,
                    outcome_message = ?,
                    mutation_resolved_at = CASE
                        WHEN mutation_started_at IS NOT NULL THEN CURRENT_TIMESTAMP
                        ELSE mutation_resolved_at
                    END
                WHERE request_id = ? AND ordinal = ?
                  AND status IN ('mutating', 'uncertain')
                """,
                (
                    normalized_external_id,
                    external_status,
                    message.strip()[:1000],
                    int(request_id),
                    ordinal,
                ),
            )
            cascade_cursor = connection.execute(
                """
                UPDATE ebook_cascades
                SET state = 'queued', final_backend = ?, finalizer = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND state IN ('mutating', 'uncertain')
                """,
                (
                    backend,
                    "bookbot" if backend == "lazylibrarian" else "shelfarr",
                    int(request_id),
                ),
            )
            if attempt_cursor.rowcount != 1 or cascade_cursor.rowcount != 1:
                raise EbookCascadeStateError(
                    "Recovered ebook handoff ledger could not be persisted"
                )
            if retry_attempt:
                retry_cursor = connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET state = 'awaiting_import', next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'retrying'
                    """,
                    (int(request_id),),
                )
                if retry_cursor.rowcount != 1:
                    raise EbookCascadeStateError(
                        "Recovered retry handoff ownership could not be promoted"
                    )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (int(request_id), event_type, message.strip()[:2000]),
            )
            for event_key, route, notification_message in deliveries:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(request_id),
                        event_key,
                        route,
                        notification_message,
                    ),
                )
            return True

    def get_by_message_id(self, message_id: str | int) -> dict[str, Any] | None:
        with self.connect() as connection:
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
                row = connection.execute(
                    "SELECT * FROM requests WHERE message_id = ?", (str(message_id),)
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
            value = dict(row) if row else None
            if value is not None:
                retry = connection.execute(
                    """
                    SELECT state FROM unavailable_retries
                    WHERE request_id = ?
                      AND state IN (
                          'queued', 'retrying', 'awaiting_import', 'blocked'
                      )
                    """,
                    (int(value["id"]),),
                ).fetchone()
                if retry is not None:
                    value["_unavailable_retry_state"] = str(retry["state"])
        return value

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
        ebook_backends: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Reserve one exact target and return its canonical request record.

        Discord redeliveries are keyed by message ID. Distinct messages are
        coalesced only while the exact canonical target is active or complete;
        failed and ``needs_selection`` requests deliberately remain retryable.
        """

        policy = _ebook_policy(ebook_backends) if ebook_backends is not None else None
        if policy is not None and media_type != "ebooks":
            raise ValueError("Only ebook requests may snapshot ebook backends")
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
                "SELECT request_id AS id FROM delivery_aliases WHERE message_id = ?",
                (str(message_id),),
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT id FROM requests WHERE message_id = ?", (str(message_id),)
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

            retry_owner = None
            if target_key:
                retry_owner = connection.execute(
                    """
                    SELECT requests.*, unavailable_retries.state AS retry_state
                    FROM unavailable_retries
                    JOIN requests ON requests.id = unavailable_retries.request_id
                    WHERE requests.target_key = ?
                      AND unavailable_retries.state IN (
                          'queued', 'retrying', 'awaiting_import', 'blocked'
                      )
                    ORDER BY requests.id
                    LIMIT 1
                    """,
                    (target_key,),
                ).fetchone()
            if retry_owner is not None:
                request_id = int(retry_owner["id"])
                connection.execute(
                    "INSERT INTO delivery_aliases (message_id, request_id) VALUES (?, ?)",
                    (str(message_id), request_id),
                )
                connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET next_retry_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE request_id = ? AND state = 'queued'
                    """,
                    (request_id,),
                )
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        request_id,
                        "unavailable_retry_reused",
                        "A new live request reused the existing unavailable retry owner",
                    ),
                )
                value = {
                    key: retry_owner[key]
                    for key in retry_owner.keys()
                    if key != "retry_state"
                }
                value["_unavailable_retry_state"] = str(retry_owner["retry_state"])
                return value, False

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
            if policy is not None:
                encoded = json.dumps(policy, separators=(",", ":"))
                connection.execute(
                    """
                    INSERT INTO ebook_cascades (
                        request_id, policy_json, current_ordinal, state
                    ) VALUES (?, ?, 0, 'searching')
                    """,
                    (int(request_id), encoded),
                )
                for ordinal, backend in enumerate(policy):
                    connection.execute(
                        """
                        INSERT INTO ebook_backend_attempts (
                            request_id, ordinal, backend, status
                        ) VALUES (?, ?, ?, 'pending')
                        """,
                        (int(request_id), ordinal, backend),
                    )
                connection.execute(
                    """
                    UPDATE requests
                    SET status = 'processing', service = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'new'
                    """,
                    (policy[0], int(request_id)),
                )
                connection.execute(
                    "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                    (
                        int(request_id),
                        "ebook_cascade_started",
                        "Started the configured serial ebook acquisition policy",
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, 'request_accepted', 'request-status', ?)
                    """,
                    (
                        int(request_id),
                        f"✅ Request #{int(request_id)} accepted: Huey is searching "
                        "for a usable ebook release.",
                    ),
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
        """Persist a bounded acquisition choice and reserve its target atomically."""

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
            if request["status"] != "processing" or request["service"] not in {
                "shelfarr",
                "abba",
                "lazylibrarian",
            }:
                raise ValueError(
                    "Candidate confirmations require a supported processing request"
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
            request_cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'awaiting_selection', updated_at = ?, error = NULL
                WHERE id = ? AND status = 'processing'
                  AND service IN ('shelfarr', 'abba', 'lazylibrarian')
                """,
                (created_at, int(request_id)),
            )
            if request_cursor.rowcount != 1:
                raise EbookCascadeStateError(
                    "Candidate request state could not be persisted"
                )
            cascade_cursor = connection.execute(
                """
                UPDATE ebook_cascades
                SET state = 'awaiting_selection', updated_at = ?
                WHERE request_id = ? AND state = 'searching'
                  AND mutation_backend IS NULL
                """,
                (created_at, int(request_id)),
            )
            attempt_cursor = connection.execute(
                """
                UPDATE ebook_backend_attempts
                SET status = 'awaiting_selection'
                WHERE request_id = ?
                  AND ordinal = (
                      SELECT current_ordinal FROM ebook_cascades
                      WHERE request_id = ?
                  )
                  AND status = 'searching'
                """,
                (int(request_id), int(request_id)),
            )
            has_cascade = connection.execute(
                "SELECT 1 FROM ebook_cascades WHERE request_id = ?",
                (int(request_id),),
            ).fetchone() is not None
            if has_cascade and (
                cascade_cursor.rowcount != 1 or attempt_cursor.rowcount != 1
            ):
                raise EbookCascadeStateError(
                    "Ebook candidate prompt diverged from cascade state"
                )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "selection_requested",
                    "Acquisition candidates require requester confirmation",
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
                self._close_released_ebook_cascade(
                    connection, int(request["id"])
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
                WHERE id = ? AND status = 'awaiting_selection'
                  AND service IN ('shelfarr', 'abba', 'lazylibrarian')
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

        The selected service invokes this immediately before its sole
        non-idempotent POST. A restart can consequently distinguish a confirmed
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
                  AND requests.service IN ('shelfarr', 'abba', 'lazylibrarian')
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
                    "Confirmed candidate crossed the request dispatch boundary",
                ),
            )
            return True

    def reserve_abba_dispatch(
        self,
        request_id: int,
        candidate_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Reserve one ABBA candidate and selected prompt before any mutation.

        The returned row is the canonical owner.  A different request ID means
        this request was atomically converted into an inert delivery alias, so
        the caller must not contact ABBA for the duplicate correlation.
        """

        normalized_candidate_id = str(candidate_id or "")
        if not _ABBA_CANDIDATE_ID.fullmatch(normalized_candidate_id):
            raise ValueError("ABBA dispatch requires an exact candidate ID")
        observed_at = _selection_timestamp(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM requests
                WHERE id = ? AND service = 'abba'
                """,
                (int(request_id),),
            ).fetchone()
            if row is None:
                return None
            if row["canonical_request_id"] is not None:
                owner = connection.execute(
                    "SELECT * FROM requests WHERE id = ?",
                    (int(row["canonical_request_id"]),),
                ).fetchone()
                return dict(owner) if owner is not None else None
            if row["status"] != "processing" or row["external_id"] is not None:
                return None
            if row["dispatch_started_at"] is not None:
                return (
                    dict(row)
                    if row["abba_candidate_id"] == normalized_candidate_id
                    else None
                )

            owner = connection.execute(
                """
                SELECT * FROM requests
                WHERE id != ? AND service = 'abba'
                  AND abba_candidate_id = ?
                  AND canonical_request_id IS NULL
                  AND status IN (
                      'processing', 'queued', 'complete', 'completed'
                  )
                ORDER BY
                    CASE
                        WHEN status IN ('complete', 'completed') THEN 0
                        WHEN status = 'queued' THEN 1
                        ELSE 2
                    END,
                    id
                LIMIT 1
                """,
                (int(request_id), normalized_candidate_id),
            ).fetchone()
            if owner is not None:
                self._coalesce_abba_row(
                    connection,
                    int(request_id),
                    int(owner["id"]),
                    reason="candidate identity",
                )
                return dict(owner)

            cursor = connection.execute(
                """
                UPDATE requests
                SET dispatch_started_at = ?, updated_at = ?,
                    abba_candidate_id = ?
                WHERE id = ? AND status = 'processing'
                  AND service = 'abba' AND external_id IS NULL
                  AND dispatch_started_at IS NULL
                  AND canonical_request_id IS NULL
                """,
                (
                    observed_at,
                    observed_at,
                    normalized_candidate_id,
                    int(request_id),
                ),
            )
            if cursor.rowcount != 1:
                return None
            confirmation = connection.execute(
                """
                SELECT id, dispatch_started_at
                FROM candidate_confirmations
                WHERE request_id = ? AND status = 'claimed'
                """,
                (int(request_id),),
            ).fetchone()
            if confirmation is not None and confirmation["dispatch_started_at"] is None:
                connection.execute(
                    """
                    UPDATE candidate_confirmations
                    SET dispatch_started_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'claimed'
                      AND dispatch_started_at IS NULL
                    """,
                    (observed_at, observed_at, int(confirmation["id"])),
                )
                connection.execute(
                    """
                    INSERT INTO events (request_id, event_type, message)
                    VALUES (?, 'selection_dispatch_started', ?)
                    """,
                    (
                        int(request_id),
                        "Confirmed candidate crossed the request dispatch boundary",
                    ),
                )
            connection.execute(
                """
                INSERT INTO events (request_id, event_type, message)
                VALUES (?, 'abba_dispatch_started', ?)
                """,
                (
                    int(request_id),
                    "Abba request crossed the dispatch boundary",
                ),
            )
            saved = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            return dict(saved) if saved is not None else None

    def coalesce_abba_request(
        self,
        request_id: int,
        canonical_request_id: int,
        *,
        candidate_id: str | None = None,
        canonical_candidate_id: str | None = None,
        info_hash: str | None = None,
        reason: str = "torrent hash identity",
    ) -> dict[str, Any]:
        """Alias one ABBA request only after validating its canonical owner."""

        normalized_candidate = str(candidate_id or "")
        normalized_owner_candidate = str(canonical_candidate_id or "")
        normalized_hash = str(info_hash or "").casefold()
        if candidate_id is not None and not _ABBA_CANDIDATE_ID.fullmatch(
            normalized_candidate
        ):
            raise ValueError("Invalid ABBA alias candidate")
        if canonical_candidate_id is not None and not _ABBA_CANDIDATE_ID.fullmatch(
            normalized_owner_candidate
        ):
            raise ValueError("Invalid canonical ABBA candidate")
        if info_hash is not None and not _ABBA_INFO_HASH.fullmatch(normalized_hash):
            raise ValueError("Invalid canonical ABBA download hash")

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
            owner = connection.execute(
                "SELECT * FROM requests WHERE id = ?",
                (int(canonical_request_id),),
            ).fetchone()
            if request is None or owner is None:
                raise sqlite3.IntegrityError("ABBA canonical request is missing")
            if request["canonical_request_id"] is not None:
                if int(request["canonical_request_id"]) != int(canonical_request_id):
                    raise sqlite3.IntegrityError("ABBA request has another canonical owner")
                return dict(owner)
            if (
                int(request_id) == int(canonical_request_id)
                or request["service"] != "abba"
                or owner["service"] != "abba"
                or owner["canonical_request_id"] is not None
                or owner["status"]
                not in {"processing", "queued", "complete", "completed", "failed"}
                or request["status"] not in {"processing", "queued"}
            ):
                raise sqlite3.IntegrityError("Invalid ABBA canonical ownership")
            if owner["status"] == "failed" and (
                candidate_id is None
                or canonical_candidate_id is None
                or info_hash is None
            ):
                raise sqlite3.IntegrityError(
                    "Failed ABBA ownership requires exact canonical evidence"
                )
            if (
                candidate_id is not None
                and request["abba_candidate_id"] != normalized_candidate
            ):
                raise sqlite3.IntegrityError("ABBA alias candidate changed")
            if (
                canonical_candidate_id is not None
                and owner["abba_candidate_id"] != normalized_owner_candidate
            ):
                raise sqlite3.IntegrityError("Canonical ABBA candidate changed")
            if info_hash is not None:
                for value in (request["external_id"], owner["external_id"]):
                    if value is not None and str(value).casefold() != normalized_hash:
                        raise sqlite3.IntegrityError(
                            "ABBA canonical download hash changed"
                        )
            self._coalesce_abba_row(
                connection,
                int(request_id),
                int(canonical_request_id),
                reason=reason,
            )
            owner = connection.execute(
                "SELECT * FROM requests WHERE id = ?",
                (int(canonical_request_id),),
            ).fetchone()
            assert owner is not None
            return dict(owner)

    def mark_request_dispatch_started(
        self,
        request_id: int,
        service: str,
        *,
        candidate_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Persist the generic remote-mutation boundary for restart recovery."""

        if service not in {"shelfarr", "abba", "lazylibrarian"}:
            raise ValueError("Unsupported correlated dispatch service")
        normalized_candidate_id = str(candidate_id or "")
        if service == "abba":
            if not _ABBA_CANDIDATE_ID.fullmatch(normalized_candidate_id):
                raise ValueError("ABBA dispatch requires an exact candidate ID")
            owner = self.reserve_abba_dispatch(
                int(request_id), normalized_candidate_id, now=now
            )
            return owner is not None and int(owner["id"]) == int(request_id)
        elif service == "lazylibrarian":
            if not _LAZYLIBRARIAN_BOOK_ID.fullmatch(normalized_candidate_id):
                raise ValueError(
                    "LazyLibrarian dispatch requires an exact book ID"
                )
        elif candidate_id is not None:
            raise ValueError(
                "Only ABBA and LazyLibrarian dispatches accept a candidate ID"
            )
        observed_at = _selection_timestamp(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT dispatch_started_at, abba_candidate_id,
                       lazylibrarian_book_id
                FROM requests
                WHERE id = ? AND status = 'processing' AND service = ?
                  AND external_id IS NULL
                """,
                (int(request_id), service),
            ).fetchone()
            if row is None:
                return False
            if service == "lazylibrarian":
                duplicate = connection.execute(
                    """
                    SELECT id FROM requests
                    WHERE id != ? AND service = 'lazylibrarian'
                      AND lazylibrarian_book_id = ?
                      AND status IN (
                          'processing', 'queued', 'complete', 'completed'
                      )
                    LIMIT 1
                    """,
                    (int(request_id), normalized_candidate_id),
                ).fetchone()
                if duplicate is not None:
                    return False
            if row["dispatch_started_at"] is not None:
                if service == "lazylibrarian":
                    return (
                        row["lazylibrarian_book_id"]
                        == normalized_candidate_id
                    )
                return True
            cursor = connection.execute(
                """
                UPDATE requests
                SET dispatch_started_at = ?, updated_at = ?,
                    abba_candidate_id = CASE
                        WHEN service = 'abba' THEN ? ELSE abba_candidate_id
                    END,
                    lazylibrarian_book_id = CASE
                        WHEN service = 'lazylibrarian' THEN ?
                        ELSE lazylibrarian_book_id
                    END
                WHERE id = ? AND status = 'processing' AND service = ?
                  AND external_id IS NULL AND dispatch_started_at IS NULL
                """,
                (
                    observed_at,
                    observed_at,
                    normalized_candidate_id if service == "abba" else None,
                    (
                        normalized_candidate_id
                        if service == "lazylibrarian"
                        else None
                    ),
                    int(request_id),
                    service,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    f"{service}_dispatch_started",
                    f"{service.title()} request crossed the dispatch boundary",
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
                self._close_released_ebook_cascade(
                    connection, int(row["request_id"])
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
            self._close_released_ebook_cascade(connection, int(request_id))
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
        normalized_external_id = str(external_id) if external_id is not None else None
        if service in {"lazylibrarian", "abba"} and normalized_external_id is not None:
            normalized_external_id = normalized_external_id.casefold()
        if service == "lazylibrarian" and normalized_external_id is not None:
            if not _DOWNLOAD_ID.fullmatch(normalized_external_id):
                raise ValueError("Invalid LazyLibrarian download ID")
        if service == "abba" and normalized_external_id is not None:
            if not _ABBA_INFO_HASH.fullmatch(normalized_external_id):
                raise ValueError("Invalid ABBA download ID")
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
            if (
                service in {"lazylibrarian", "abba"}
                and normalized_external_id is not None
                and status in {"processing", "queued"}
            ):
                connection.execute("BEGIN IMMEDIATE")
            if (
                service == "abba"
                and normalized_external_id is not None
                and status in {"processing", "queued"}
            ):
                duplicate = connection.execute(
                    """
                    SELECT * FROM requests
                    WHERE id != ? AND service = 'abba'
                      AND lower(external_id) = ?
                      AND canonical_request_id IS NULL
                      AND status IN (
                          'processing', 'queued', 'complete', 'completed'
                      )
                    ORDER BY
                        CASE
                            WHEN status IN ('complete', 'completed') THEN 0
                            WHEN status = 'queued' THEN 1
                            ELSE 2
                        END,
                        id
                    LIMIT 1
                    """,
                    (int(request_id), normalized_external_id),
                ).fetchone()
                if duplicate is not None:
                    self._coalesce_abba_row(
                        connection,
                        int(request_id),
                        int(duplicate["id"]),
                        reason="Huey hash defense",
                    )
                    return dict(duplicate)
            if (
                service == "lazylibrarian"
                and normalized_external_id is not None
                and status in {"processing", "queued"}
            ):
                duplicate = connection.execute(
                    """
                    SELECT requests.id FROM requests
                    WHERE requests.id != ?
                      AND requests.service = 'lazylibrarian'
                      AND lower(requests.external_id) = ?
                      AND (
                          requests.status IN (
                              'processing', 'queued', 'complete', 'completed'
                          )
                          OR requests.external_status =
                              'lazylibrarian_hash_identity_conflict'
                          OR EXISTS (
                              SELECT 1 FROM unavailable_retries
                              WHERE unavailable_retries.request_id = requests.id
                                AND unavailable_retries.state = 'blocked'
                          )
                      )
                    LIMIT 1
                    """,
                    (int(request_id), normalized_external_id),
                ).fetchone()
                if duplicate is not None:
                    raise LazyLibrarianHashCollision(
                        "Active LazyLibrarian download identity collision"
                    )
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = ?, updated_at = CURRENT_TIMESTAMP,
                    service = ?, external_id = ?, external_title = ?,
                    external_status = ?, error = ?
                WHERE id = ? AND canonical_request_id IS NULL
                """,
                (
                    status,
                    service,
                    normalized_external_id,
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
                  AND requests.canonical_request_id IS NULL
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

    def register_trusted_library_event(
        self,
        *,
        source_fingerprint: str,
        source_path: str,
        size_bytes: int,
        title: str | None = None,
        year: int | None = None,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
        media_type: str = "movie",
        group_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one physical-disc identity without creating a Huey request."""

        fingerprint = str(source_fingerprint or "").casefold()
        if not _SELECTION_FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("Trusted event fingerprint must be a SHA-256 value")
        if not source_path or int(size_bytes) <= 0:
            raise ValueError("Trusted event requires a non-empty source file")
        if media_type not in {"movie", "tv", "nonstandard", "ambiguous"}:
            raise ValueError("Trusted event media type is invalid")
        metadata_json = None
        if metadata is not None:
            metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
            if not 2 <= len(metadata_json) <= 65536:
                raise ValueError("Trusted event metadata is too large")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO trusted_library_events (
                    source_type, source_fingerprint, source_path, size_bytes,
                    title, year, imdb_id, tmdb_id, media_type, group_key, metadata_json
                ) VALUES ('physical-disc', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    str(source_path),
                    int(size_bytes),
                    title,
                    year,
                    imdb_id,
                    tmdb_id,
                    media_type,
                    group_key,
                    metadata_json,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM trusted_library_events
                WHERE source_type = 'physical-disc' AND source_fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
        return dict(row), cursor.rowcount == 1

    def trusted_library_events(
        self, *, states: Sequence[str] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM trusted_library_events"
        parameters: list[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" WHERE state IN ({placeholders})"
            parameters.extend(states)
        query += " ORDER BY updated_at, id LIMIT ?"
        parameters.append(max(1, min(int(limit), 1000)))
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def update_trusted_library_event_source_path(
        self, event_id: int, source_path: str
    ) -> bool:
        if not source_path:
            raise ValueError("Trusted event source path cannot be empty")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE trusted_library_events
                SET source_path = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND state != 'completed'
                """,
                (str(source_path), int(event_id)),
            )
            return cursor.rowcount == 1

    def transition_trusted_library_event(
        self,
        event_id: int,
        state: str,
        *,
        title: str | None = None,
        year: int | None = None,
        imdb_id: str | None = None,
        tmdb_id: int | None = None,
        radarr_movie_id: int | None = None,
        radarr_command_id: int | None = None,
        sonarr_series_id: int | None = None,
        sonarr_command_id: int | None = None,
        final_path: str | None = None,
        error: str | None = None,
    ) -> bool:
        allowed = {
            "received", "validated", "identity_resolved", "import_submitting",
            "importing", "completed", "manual_review", "failed",
        }
        if state not in allowed:
            raise ValueError("Invalid trusted library event state")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE trusted_library_events
                SET state = ?, updated_at = CURRENT_TIMESTAMP,
                    title = COALESCE(?, title), year = COALESCE(?, year),
                    imdb_id = COALESCE(?, imdb_id), tmdb_id = COALESCE(?, tmdb_id),
                    radarr_movie_id = COALESCE(?, radarr_movie_id),
                    radarr_command_id = COALESCE(?, radarr_command_id),
                    sonarr_series_id = COALESCE(?, sonarr_series_id),
                    sonarr_command_id = COALESCE(?, sonarr_command_id),
                    final_path = COALESCE(?, final_path), error = ?
                WHERE id = ? AND state != 'completed'
                """,
                (
                    state, title, year, imdb_id, tmdb_id, radarr_movie_id,
                    radarr_command_id, sonarr_series_id, sonarr_command_id,
                    final_path, error, int(event_id),
                ),
            )
            return cursor.rowcount == 1

    def enqueue_trusted_notification(
        self,
        trusted_event_id: int,
        event_key: str,
        route: str,
        message: str,
    ) -> bool:
        """Stage one trusted-event notification in Huey's shared outbox."""

        if not event_key or not route or not message or not message.strip():
            raise ValueError("Notification delivery fields cannot be empty")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (
                    trusted_event_id, event_key, route, message
                ) VALUES (?, ?, ?, ?)
                """,
                (int(trusted_event_id), event_key, route, message.strip()[:2000]),
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
        """Finalize the request only from its current terminal generation.

        Request completion may race a reconciliation pass which staged and
        delivered the row's earlier failure notification.  Keep the status and
        its required terminal event in this one atomic predicate so that stale
        failure delivery can never mark a now-completed row as notified (or the
        inverse).
        """

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET notified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND notified_at IS NULL
                  AND status IN ('complete', 'completed', 'failed')
                  AND (
                      (
                          status IN ('complete', 'completed')
                          AND EXISTS (
                              SELECT 1 FROM notification_deliveries
                              WHERE request_id = requests.id
                                AND event_key = 'request_completed'
                                AND delivered_at IS NOT NULL
                          )
                      )
                      OR (
                          status = 'failed'
                          AND EXISTS (
                              SELECT 1 FROM notification_deliveries
                              WHERE request_id = requests.id
                                AND event_key = 'request_failed'
                                AND delivered_at IS NOT NULL
                          )
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

    def claim_blocked_shelfarr_proof_checks(
        self,
        limit: int = 100,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically claim a fair batch of blocked Shelfarr proof checks.

        These rows are terminal for acquisition.  They are exposed solely so
        reconciliation can observe a late ``completed`` result for the same
        already-persisted Shelfarr request ID.  ``last_proof_check_at`` is a
        durable least-recently-checked cursor; advancing it before any remote
        read prevents a permanently non-completing leading batch from starving
        later owners across process restarts or concurrent reconciliation.
        """

        moment = now or datetime.now(timezone.utc)
        if not isinstance(moment, datetime):
            raise TypeError("Shelfarr proof-check timestamps must be datetime values")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Shelfarr proof-check timestamps must include a timezone")
        claimed_at = moment.astimezone(timezone.utc).replace(tzinfo=None)
        bounded_limit = max(1, min(int(limit), 1000))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            last_claim = connection.execute(
                """
                SELECT MAX(last_proof_check_at) AS value
                FROM unavailable_retries
                WHERE state = 'blocked' AND last_proof_check_at IS NOT NULL
                """
            ).fetchone()["value"]
            if last_claim is not None:
                try:
                    previous = datetime.fromisoformat(str(last_claim))
                except ValueError as error:
                    raise sqlite3.DatabaseError(
                        "Blocked Shelfarr proof-check cursor is invalid"
                    ) from error
                if previous.tzinfo is not None and previous.utcoffset() is not None:
                    previous = previous.astimezone(timezone.utc).replace(tzinfo=None)
                if claimed_at <= previous:
                    claimed_at = previous + timedelta(microseconds=1)
            timestamp = claimed_at.isoformat(sep=" ", timespec="microseconds")
            rows = connection.execute(
                """
                SELECT request.*
                FROM requests AS request
                JOIN unavailable_retries AS retry
                  ON retry.request_id = request.id
                JOIN ebook_cascades AS cascade
                  ON cascade.request_id = request.id
                JOIN ebook_backend_attempts AS attempt
                  ON attempt.request_id = cascade.request_id
                 AND attempt.ordinal = cascade.current_ordinal
                WHERE retry.state = 'blocked'
                  AND request.status = 'failed'
                  AND request.service = 'shelfarr'
                  AND request.media_type = 'ebooks'
                  AND request.external_id IS NOT NULL
                  AND cascade.state = 'failed'
                  AND cascade.identity_key IS NOT NULL
                  AND (
                      cascade.final_backend = 'shelfarr'
                      OR (
                          cascade.final_backend IS NULL
                          AND cascade.mutation_backend = 'shelfarr'
                      )
                  )
                  AND attempt.backend = 'shelfarr'
                  AND attempt.external_id IS NOT NULL
                  AND attempt.external_id = request.external_id
                ORDER BY retry.last_proof_check_at IS NOT NULL,
                         retry.last_proof_check_at,
                         request.id
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE unavailable_retries
                    SET last_proof_check_at = ?
                    WHERE request_id = ? AND state = 'blocked'
                    """,
                    (timestamp, int(row["id"])),
                )
                if cursor.rowcount != 1:  # pragma: no cover - write lock invariant
                    raise sqlite3.DatabaseError(
                        "Blocked Shelfarr proof-check claim was not atomic"
                    )
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

    def queued_abba_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return active ABBA/qBittorrent jobs awaiting BookBot completion."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'queued' AND service = 'abba'
                  AND external_id IS NOT NULL
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def queued_lazylibrarian_requests(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return correlated LL downloads awaiting BookBot's terminal import."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'queued' AND service = 'lazylibrarian'
                  AND external_id IS NOT NULL
                  AND lazylibrarian_book_id IS NOT NULL
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def uncertain_abba_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return ABBA grabs whose response was lost after dispatch began."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'queued' AND service = 'abba'
                  AND external_id IS NULL
                  AND external_status = 'submission_uncertain'
                  AND dispatch_started_at IS NOT NULL
                  AND abba_candidate_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM events
                      WHERE events.request_id = requests.id
                        AND events.event_type = 'abba_submission_uncertain'
                  )
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def uncertain_lazylibrarian_requests(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return LL submissions awaiting an exact history correlation."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status = 'queued' AND service = 'lazylibrarian'
                  AND external_id IS NULL
                  AND external_status = 'submission_uncertain'
                  AND dispatch_started_at IS NOT NULL
                  AND lazylibrarian_book_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM events
                      WHERE events.request_id = requests.id
                        AND events.event_type =
                            'lazylibrarian_submission_uncertain'
                  )
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_lazylibrarian_download(
        self,
        request_id: int,
        book_id: str,
        download_id: str,
        external_title: str,
        message: str,
        *,
        external_status: str = "queued",
        notifications: Sequence[tuple[str, str, str]] = (),
    ) -> bool:
        """Bind one exact LL BookID to one immutable qBittorrent hash."""

        normalized_book_id = str(book_id or "")
        normalized_download_id = str(download_id or "").casefold()
        if not _LAZYLIBRARIAN_BOOK_ID.fullmatch(normalized_book_id):
            raise ValueError("Invalid LazyLibrarian book ID")
        if not _DOWNLOAD_ID.fullmatch(normalized_download_id):
            raise ValueError("Invalid LazyLibrarian download ID")
        if not message or not message.strip():
            raise ValueError("LazyLibrarian recovery requires an event message")
        if external_status not in {"queued", "processing"}:
            raise ValueError("Invalid LazyLibrarian handoff status")
        normalized_notifications: list[tuple[str, str, str]] = []
        for delivery in notifications:
            if (
                not isinstance(delivery, (list, tuple))
                or len(delivery) != 3
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in delivery
                )
            ):
                raise ValueError("Notification delivery fields cannot be empty")
            normalized_notifications.append(
                (delivery[0].strip(), delivery[1].strip(), delivery[2].strip()[:2000])
            )

        if self.get_ebook_cascade(int(request_id)) is not None:
            return self.record_ebook_recovered_handoff(
                int(request_id),
                "lazylibrarian",
                normalized_download_id,
                external_title,
                external_status,
                message,
                backend_identity=normalized_book_id,
                event_type="lazylibrarian_history_recovered",
                notifications=normalized_notifications,
            )

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, external_id, lazylibrarian_book_id
                FROM requests
                WHERE id = ? AND service = 'lazylibrarian'
                """,
                (int(request_id),),
            ).fetchone()
            if row is None:
                return False
            if row["lazylibrarian_book_id"] != normalized_book_id:
                raise sqlite3.IntegrityError(
                    "LazyLibrarian recovery book identity changed"
                )
            if row["external_id"] is not None:
                if str(row["external_id"]).casefold() != normalized_download_id:
                    raise sqlite3.IntegrityError(
                        "LazyLibrarian recovery download identity changed"
                    )
                return False
            if row["status"] not in {"processing", "queued"}:
                return False
            duplicate = connection.execute(
                """
                SELECT requests.id FROM requests
                WHERE requests.id != ?
                  AND requests.service = 'lazylibrarian'
                  AND lower(requests.external_id) = ?
                  AND (
                      requests.status IN (
                          'processing', 'queued', 'complete', 'completed'
                      )
                      OR requests.external_status =
                          'lazylibrarian_hash_identity_conflict'
                      OR EXISTS (
                          SELECT 1 FROM unavailable_retries
                          WHERE unavailable_retries.request_id = requests.id
                            AND unavailable_retries.state = 'blocked'
                      )
                  )
                LIMIT 1
                """,
                (int(request_id), normalized_download_id),
            ).fetchone()
            if duplicate is not None:
                raise LazyLibrarianHashCollision(
                    "Active LazyLibrarian download identity collision"
                )
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'queued', external_id = ?,
                    external_status = ?, external_title = ?,
                    error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND service = 'lazylibrarian'
                  AND status IN ('processing', 'queued')
                  AND external_id IS NULL
                  AND lazylibrarian_book_id = ?
                """,
                (
                    normalized_download_id,
                    external_status,
                    external_title,
                    int(request_id),
                    normalized_book_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "lazylibrarian_history_recovered",
                    message.strip()[:2000],
                ),
            )
            for event_key, route, notification_message in normalized_notifications:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(request_id),
                        event_key,
                        route,
                        notification_message,
                    ),
                )
            return True

    def record_lazylibrarian_state(
        self,
        request_id: int,
        expected_download_id: str,
        external_status: str,
        message: str,
        *,
        terminal: bool = False,
        error: str | None = None,
        notifications: Sequence[tuple[str, str, str]] = (),
    ) -> bool:
        """Persist one reliable qBit phase while BookBot owns success."""

        normalized = external_status.strip().casefold()
        normalized_download_id = str(expected_download_id or "").casefold()
        if not _DOWNLOAD_ID.fullmatch(normalized_download_id):
            raise ValueError("Invalid LazyLibrarian download ID")
        if normalized not in LAZYLIBRARIAN_STATUSES:
            raise ValueError(f"Invalid LazyLibrarian status: {external_status}")
        if not message or not message.strip():
            raise ValueError("LazyLibrarian state observations require a message")
        if terminal != (normalized == "failed"):
            raise ValueError(
                "LazyLibrarian failures must be terminal before BookBot"
            )
        normalized_notifications: list[tuple[str, str, str]] = []
        for delivery in notifications:
            if (
                not isinstance(delivery, (list, tuple))
                or len(delivery) != 3
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in delivery
                )
            ):
                raise ValueError("Notification delivery fields cannot be empty")
            normalized_notifications.append(
                (delivery[0].strip(), delivery[1].strip(), delivery[2].strip()[:2000])
            )

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT external_status FROM requests
                WHERE id = ? AND status = 'queued'
                  AND service = 'lazylibrarian'
                  AND external_id IS NOT NULL
                  AND lower(external_id) = ?
                """,
                (int(request_id), normalized_download_id),
            ).fetchone()
            if row is None or (
                str(row["external_status"] or "").casefold() == normalized
                and not terminal
            ):
                return False
            next_status = "failed" if terminal else "queued"
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = ?, external_status = ?, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'queued'
                  AND service = 'lazylibrarian'
                  AND external_id IS NOT NULL
                  AND lower(external_id) = ?
                """,
                (
                    next_status,
                    normalized,
                    error if terminal else None,
                    int(request_id),
                    normalized_download_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    f"lazylibrarian_{normalized}",
                    message.strip()[:2000],
                ),
            )
            for event_key, route, notification_message in normalized_notifications:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(request_id),
                        event_key,
                        route,
                        notification_message,
                    ),
                )
            return True

    def record_abba_state(
        self,
        request_id: int,
        external_status: str,
        message: str,
        *,
        terminal: bool = False,
        error: str | None = None,
        notifications: Sequence[tuple[str, str, str]] = (),
    ) -> bool:
        """Persist one quiet ABBA/qBittorrent lifecycle edge."""

        normalized = external_status.strip().casefold()
        if normalized not in ABBA_STATUSES:
            raise ValueError(f"Invalid ABBA status: {external_status}")
        if not message or not message.strip():
            raise ValueError("ABBA state observations require a message")
        if terminal and normalized != "failed":
            raise ValueError("Only a failed ABBA job is terminal before BookBot")
        normalized_notifications: list[tuple[str, str, str]] = []
        for delivery in notifications:
            if (
                not isinstance(delivery, (list, tuple))
                or len(delivery) != 3
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in delivery
                )
            ):
                raise ValueError("Notification delivery fields cannot be empty")
            normalized_notifications.append(
                (delivery[0].strip(), delivery[1].strip(), delivery[2].strip()[:2000])
            )

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT external_status FROM requests
                WHERE id = ? AND status = 'queued' AND service = 'abba'
                """,
                (int(request_id),),
            ).fetchone()
            if row is None or (
                str(row["external_status"] or "").casefold() == normalized
                and not terminal
            ):
                return False
            next_status = "failed" if terminal else "queued"
            connection.execute(
                """
                UPDATE requests
                SET status = ?, external_status = ?, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'queued' AND service = 'abba'
                """,
                (
                    next_status,
                    normalized,
                    error if terminal else None,
                    int(request_id),
                ),
            )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    f"abba_{normalized}",
                    message.strip()[:2000],
                ),
            )
            for event_key, route, notification_message in normalized_notifications:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_deliveries (
                        request_id, event_key, route, message
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(request_id),
                        event_key,
                        route,
                        notification_message,
                    ),
                )
            return True

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

    def record_blocked_shelfarr_completion(
        self,
        request_id: int,
        remote_request_id: object,
        message: str = "Shelfarr completed its exact retained final library import",
    ) -> bool:
        """Fulfil one blocked retry from an exact late Shelfarr completion.

        This method cannot submit, cancel, or otherwise mutate Shelfarr.  It
        accepts only the persisted remote request ID on the failed current
        attempt and relies on the schema trigger to repair the cascade and
        retry ownership in the same transaction.
        """

        normalized_remote_id = str(remote_request_id or "").strip()
        if (
            not normalized_remote_id.isdigit()
            or int(normalized_remote_id) <= 0
        ):
            raise ValueError("Invalid Shelfarr request ID")
        if not message or not message.strip():
            raise ValueError("Shelfarr completion requires an event message")

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exact_owner = connection.execute(
                """
                SELECT 1
                FROM requests AS request
                JOIN unavailable_retries AS retry
                  ON retry.request_id = request.id
                JOIN ebook_cascades AS cascade
                  ON cascade.request_id = request.id
                JOIN ebook_backend_attempts AS attempt
                  ON attempt.request_id = cascade.request_id
                 AND attempt.ordinal = cascade.current_ordinal
                WHERE request.id = ?
                  AND retry.state = 'blocked'
                  AND request.status = 'failed'
                  AND request.service = 'shelfarr'
                  AND request.media_type = 'ebooks'
                  AND request.external_id = ?
                  AND cascade.state = 'failed'
                  AND cascade.identity_key IS NOT NULL
                  AND (
                      cascade.final_backend = 'shelfarr'
                      OR (
                          cascade.final_backend IS NULL
                          AND cascade.mutation_backend = 'shelfarr'
                      )
                  )
                  AND attempt.backend = 'shelfarr'
                  AND attempt.external_id = ?
                """,
                (
                    int(request_id),
                    normalized_remote_id,
                    normalized_remote_id,
                ),
            ).fetchone()
            if exact_owner is None:
                return False

            cursor = connection.execute(
                """
                UPDATE requests
                SET status = 'completed', external_status = 'completed',
                    error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'failed'
                  AND service = 'shelfarr' AND external_id = ?
                """,
                (int(request_id), normalized_remote_id),
            )
            if cursor.rowcount != 1:
                return False
            proof = connection.execute(
                """
                SELECT 1
                FROM unavailable_retries AS retry
                JOIN ebook_cascades AS cascade
                  ON cascade.request_id = retry.request_id
                JOIN ebook_backend_attempts AS attempt
                  ON attempt.request_id = cascade.request_id
                 AND attempt.ordinal = cascade.current_ordinal
                WHERE retry.request_id = ?
                  AND retry.state = 'fulfilled'
                  AND retry.final_import_state = 'verified'
                  AND cascade.state = 'completed'
                  AND cascade.final_backend = 'shelfarr'
                  AND cascade.finalizer = 'shelfarr'
                  AND attempt.status = 'completed'
                  AND attempt.backend = 'shelfarr'
                  AND attempt.external_id = ?
                """,
                (int(request_id), normalized_remote_id),
            ).fetchone()
            if proof is None:
                raise EbookCascadeStateError(
                    "Blocked Shelfarr completion did not preserve exact proof"
                )
            connection.execute(
                "INSERT INTO events (request_id, event_type, message) VALUES (?, ?, ?)",
                (
                    int(request_id),
                    "shelfarr_completed",
                    message.strip()[:2000],
                ),
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
