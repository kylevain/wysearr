"""Best-effort completion updates for an optionally mounted Huey database."""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path


LOGGER = logging.getLogger(__name__)
HUEY_TAG = re.compile(r"(?:^|[,\s])huey[-:](\d+)(?:$|[,\s])", re.IGNORECASE)
HASH_COLUMNS = ("torrent_hash", "download_hash", "external_id")


class HueyUpdater:
    """Update Huey if a compatible DB is mounted; never fail an import."""

    def __init__(self, database_path: Path | None) -> None:
        self.database_path = database_path

    def complete(
        self,
        torrent_hash: str,
        destination: Path,
        tags: str = "",
    ) -> bool:
        if self.database_path is None or not self.database_path.is_file():
            return False
        try:
            return self._update(
                torrent_hash,
                tags,
                status="complete",
                event_type="completed",
                message=f"BookBot imported media to {destination}",
                destination=destination,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            LOGGER.warning("Huey completion update skipped: %s", exc)
            return False

    def failed(
        self,
        torrent_hash: str,
        error: str | Exception,
        tags: str = "",
    ) -> bool:
        if self.database_path is None or not self.database_path.is_file():
            return False
        message = str(error).replace("\x00", "")[:1800]
        try:
            return self._update(
                torrent_hash,
                tags,
                status="failed",
                event_type="failed",
                message=f"BookBot import failed: {message}",
                error=message,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            LOGGER.warning("Huey failure update skipped: %s", exc)
            return False

    def _update(
        self,
        torrent_hash: str,
        tags: str,
        *,
        status: str,
        event_type: str,
        message: str,
        destination: Path | None = None,
        error: str | None = None,
    ) -> bool:
        assert self.database_path is not None
        raw_connection = sqlite3.connect(self.database_path, timeout=5)
        with closing(raw_connection) as connection, connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            request_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(requests)").fetchall()
            }
            if not request_columns or "id" not in request_columns:
                return False

            request_id = self._request_id_from_tags(tags)
            if request_id is None:
                hash_column = next(
                    (column for column in HASH_COLUMNS if column in request_columns),
                    None,
                )
                if hash_column is None:
                    return False
                row = connection.execute(
                    f"SELECT id FROM requests WHERE {hash_column} = ? ORDER BY id DESC LIMIT 1",
                    (torrent_hash,),
                ).fetchone()
                if row is None:
                    return False
                request_id = int(row[0])
            else:
                row = connection.execute(
                    "SELECT id FROM requests WHERE id = ?", (request_id,)
                ).fetchone()
                if row is None:
                    return False

            assignments: list[str] = []
            values: list[object] = []
            if "status" in request_columns:
                assignments.append("status = ?")
                values.append(status)
            if destination is not None and "library_path" in request_columns:
                assignments.append("library_path = ?")
                values.append(str(destination))
            error_column = next(
                (
                    column
                    for column in ("error_message", "last_error", "error")
                    if column in request_columns
                ),
                None,
            )
            if error_column is not None:
                assignments.append(f"{error_column} = ?")
                values.append(error)
            if "updated_at" in request_columns:
                assignments.append("updated_at = CURRENT_TIMESTAMP")
            if assignments:
                values.append(request_id)
                connection.execute(
                    f"UPDATE requests SET {', '.join(assignments)} WHERE id = ?",
                    values,
                )

            event_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            required = {"request_id", "event_type", "message"}
            if required.issubset(event_columns):
                connection.execute(
                    """
                    INSERT INTO events (request_id, event_type, message)
                    VALUES (?, ?, ?)
                    """,
                    (
                        request_id,
                        event_type,
                        message[:2000],
                    ),
                )
            return True

    @staticmethod
    def _request_id_from_tags(tags: str) -> int | None:
        match = HUEY_TAG.search(tags or "")
        return int(match.group(1)) if match else None
