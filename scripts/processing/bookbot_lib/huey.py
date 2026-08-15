"""Best-effort completion updates for an optionally mounted Huey database."""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .errors import MetadataCorrelationError
from .storage import AudiobookMetadata


LOGGER = logging.getLogger(__name__)
HUEY_TAG = re.compile(r"huey-([1-9][0-9]{0,18})")
HASH_COLUMNS = ("torrent_hash", "download_hash", "external_id")
ACTIVE_OWNER_STATUSES = frozenset({"queued", "downloading", "processing"})


class HueyUpdater:
    """Update Huey best-effort and resolve strictly correlated request metadata."""

    def __init__(self, database_path: Path | None) -> None:
        self.database_path = database_path

    def complete(
        self,
        torrent_hash: str,
        destination: Path,
        tags: str = "",
        *,
        source_category: str | None = None,
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
                source_category=source_category,
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

    def abba_audiobook_metadata(
        self, torrent_hash: str, tags: str = ""
    ) -> AudiobookMetadata | None:
        """Return metadata only for one exact Huey tag/hash ABBA binding."""

        if self.database_path is None or not self.database_path.is_file():
            return None
        try:
            tagged_ids = sorted(set(self._request_ids_from_tags(tags)))
        except ValueError as exc:
            raise MetadataCorrelationError("torrent has an invalid Huey tag") from exc
        if not tagged_ids:
            return None

        database_uri = self.database_path.resolve().as_uri() + "?mode=ro"
        raw_connection = sqlite3.connect(
            database_uri, timeout=5, uri=True
        )
        raw_connection.row_factory = sqlite3.Row
        with closing(raw_connection) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            request_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(requests)"
                ).fetchall()
            }
            required_columns = {
                "id", "media_type", "service", "title", "status",
                "external_status",
            }
            hash_columns = [
                column for column in HASH_COLUMNS if column in request_columns
            ]
            if not required_columns.issubset(request_columns) or not hash_columns:
                return None

            selected_columns = [
                "id",
                "media_type",
                "service",
                "title",
                "status",
                "external_status",
            ]
            if "author" in request_columns:
                selected_columns.append("author")
            if "canonical_request_id" in request_columns:
                selected_columns.append("canonical_request_id")
            selected_columns.extend(hash_columns)
            placeholders = ",".join("?" for _ in tagged_ids)
            tagged_rows = connection.execute(
                f"""
                SELECT {', '.join(selected_columns)}
                FROM requests
                WHERE id IN ({placeholders})
                """,
                tuple(tagged_ids),
            ).fetchall()

            canonical_ids = {
                int(row["canonical_request_id"])
                for row in tagged_rows
                if "canonical_request_id" in row.keys()
                and row["canonical_request_id"] is not None
            }
            rows = list(tagged_rows)
            if canonical_ids:
                owner_placeholders = ",".join("?" for _ in canonical_ids)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT {', '.join(selected_columns)}
                        FROM requests
                        WHERE id IN ({owner_placeholders})
                        """,
                        tuple(sorted(canonical_ids)),
                    ).fetchall()
                )

        normalized_hash = torrent_hash.casefold()
        rows_by_id = {int(row["id"]): row for row in rows}
        abba_rows: list[sqlite3.Row] = []
        resolved_owner_ids: set[int] = set()
        conflicting_ids: set[int] = set()
        if len(tagged_ids) > 1 and len(tagged_rows) != len(tagged_ids):
            raise MetadataCorrelationError(
                "not every Huey tag resolves to a saved audiobook request"
            )
        for row in tagged_rows:
            if (
                str(row["service"] or "").casefold() != "abba"
                or str(row["media_type"] or "").casefold() != "audiobooks"
            ):
                if len(tagged_ids) > 1:
                    conflicting_ids.add(int(row["id"]))
                continue
            abba_rows.append(row)
            row_hashes = {
                str(row[column]).strip().casefold()
                for column in hash_columns
                if row[column] is not None and str(row[column]).strip()
            }
            canonical_id = (
                int(row["canonical_request_id"])
                if "canonical_request_id" in row.keys()
                and row["canonical_request_id"] is not None
                else int(row["id"])
            )
            if canonical_id != int(row["id"]) and (
                str(row["status"] or "").casefold() != "failed"
                or str(row["external_status"] or "").casefold()
                != "canonical_duplicate"
            ):
                conflicting_ids.add(int(row["id"]))
            if row_hashes and row_hashes != {normalized_hash}:
                conflicting_ids.add(int(row["id"]))
            resolved_owner_ids.add(canonical_id)

        if not abba_rows:
            if len(tagged_ids) > 1:
                raise MetadataCorrelationError(
                    "multiple Huey tags do not resolve to one ABBA audiobook owner"
                )
            return None
        if (
            len(abba_rows) != len(tagged_ids)
            or len(resolved_owner_ids) != 1
            or conflicting_ids
        ):
            request_ids = ", ".join(str(int(row["id"])) for row in abba_rows)
            raise MetadataCorrelationError(
                "conflicting Huey ABBA audiobook requests are tagged for torrent "
                f"{torrent_hash[:12]}: {request_ids}"
            )

        owner_id = next(iter(resolved_owner_ids))
        row = rows_by_id.get(owner_id)
        if row is None:
            raise MetadataCorrelationError(
                f"canonical Huey ABBA request {owner_id} is missing"
            )
        owner_hashes = {
            str(row[column]).strip().casefold()
            for column in hash_columns
            if row[column] is not None and str(row[column]).strip()
        }
        if (
            str(row["service"] or "").casefold() != "abba"
            or str(row["media_type"] or "").casefold() != "audiobooks"
            or str(row["status"] or "").casefold()
            not in {
                "processing", "queued", "downloading", "complete", "completed"
            }
            or owner_hashes != {normalized_hash}
            or (
                "canonical_request_id" in row.keys()
                and row["canonical_request_id"] is not None
            )
        ):
            raise MetadataCorrelationError(
                f"Huey ABBA request {owner_id} is not an active exact hash owner"
            )
        try:
            return AudiobookMetadata(
                title=row["title"],
                author=row["author"] if "author" in row.keys() else None,
            )
        except ValueError as exc:
            raise MetadataCorrelationError(
                f"Huey request {int(row['id'])} has invalid audiobook metadata"
            ) from exc

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
        source_category: str | None = None,
    ) -> bool:
        assert self.database_path is not None
        raw_connection = sqlite3.connect(self.database_path, timeout=5)
        raw_connection.row_factory = sqlite3.Row
        with closing(raw_connection) as connection, connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            request_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(requests)").fetchall()
            }
            if not request_columns or "id" not in request_columns:
                return False

            hash_columns = [
                column for column in HASH_COLUMNS if column in request_columns
            ]
            if "status" not in request_columns or not hash_columns:
                return False
            tagged_ids = sorted(set(self._request_ids_from_tags(tags)))
            if not tagged_ids:
                return False
            selected_columns = ["id", "status", *hash_columns]
            for optional in (
                "service",
                "media_type",
                "external_status",
                "canonical_request_id",
            ):
                if optional in request_columns:
                    selected_columns.append(optional)
            placeholders = ",".join("?" for _ in tagged_ids)
            tagged_rows = connection.execute(
                f"""
                SELECT {', '.join(selected_columns)} FROM requests
                WHERE id IN ({placeholders})
                """,
                tuple(tagged_ids),
            ).fetchall()
            if len(tagged_rows) != len(tagged_ids):
                return False

            canonical_ids = {
                int(row["canonical_request_id"])
                for row in tagged_rows
                if "canonical_request_id" in row.keys()
                and row["canonical_request_id"] is not None
            }
            rows = list(tagged_rows)
            if canonical_ids:
                owner_placeholders = ",".join("?" for _ in canonical_ids)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT {', '.join(selected_columns)} FROM requests
                        WHERE id IN ({owner_placeholders})
                        """,
                        tuple(sorted(canonical_ids)),
                    ).fetchall()
                )
            rows_by_id = {int(row["id"]): row for row in rows}
            normalized_hash = str(torrent_hash or "").strip().casefold()
            if not normalized_hash:
                return False

            # A final-library copy may finish just after a previously
            # correlated unavailable retry was marked failed/blocked.  Permit
            # completion (never failure or reacquisition) only for that exact
            # persisted LazyLibrarian hash, current cascade attempt, and
            # BookBot finalizer.  Legacy/minimal Huey databases simply do not
            # expose this opt-in path and retain the normal active-only rule.
            blocked_completion_ids: set[int] = set()
            if status in {"complete", "completed"} and {
                "service",
                "media_type",
                "external_id",
            }.issubset(request_columns):
                table_names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                if {
                    "unavailable_retries",
                    "ebook_cascades",
                    "ebook_backend_attempts",
                }.issubset(table_names):
                    blocked_completion_ids = {
                        int(row[0])
                        for row in connection.execute(
                            """
                            SELECT request.id
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
                              AND request.service = 'lazylibrarian'
                              AND request.media_type = 'ebooks'
                              AND request.external_id IS NOT NULL
                              AND lower(request.external_id) = ?
                              AND cascade.state = 'failed'
                              AND cascade.identity_key IS NOT NULL
                              AND (
                                  cascade.final_backend = 'lazylibrarian'
                                  OR (
                                      cascade.final_backend IS NULL
                                      AND cascade.mutation_backend = 'lazylibrarian'
                                  )
                              )
                              AND attempt.backend = 'lazylibrarian'
                              AND attempt.external_id IS NOT NULL
                              AND lower(attempt.external_id) = ?
                            """,
                            (normalized_hash, normalized_hash),
                        ).fetchall()
                    }

            resolved_owner_ids: set[int] = set()
            for tagged in tagged_rows:
                tagged_id = int(tagged["id"])
                tagged_hashes = {
                    str(tagged[column]).strip().casefold()
                    for column in hash_columns
                    if tagged[column] is not None and str(tagged[column]).strip()
                }
                owner_id = (
                    int(tagged["canonical_request_id"])
                    if "canonical_request_id" in tagged.keys()
                    and tagged["canonical_request_id"] is not None
                    else tagged_id
                )
                is_alias = owner_id != tagged_id
                if is_alias:
                    if (
                        "service" not in tagged.keys()
                        or "media_type" not in tagged.keys()
                        or "external_status" not in tagged.keys()
                        or str(tagged["service"] or "").casefold() != "abba"
                        or str(tagged["media_type"] or "").casefold()
                        != "audiobooks"
                        or str(tagged["status"] or "").casefold() != "failed"
                        or str(tagged["external_status"] or "").casefold()
                        != "canonical_duplicate"
                        or (tagged_hashes and tagged_hashes != {normalized_hash})
                    ):
                        return False
                elif (
                    str(tagged["status"] or "").casefold()
                    not in ACTIVE_OWNER_STATUSES
                    and tagged_id not in blocked_completion_ids
                    or tagged_hashes != {normalized_hash}
                ):
                    return False

                owner = rows_by_id.get(owner_id)
                if owner is None:
                    return False
                owner_hashes = {
                    str(owner[column]).strip().casefold()
                    for column in hash_columns
                    if owner[column] is not None and str(owner[column]).strip()
                }
                if (
                    str(owner["status"] or "").casefold()
                    not in ACTIVE_OWNER_STATUSES
                    and owner_id not in blocked_completion_ids
                    or owner_hashes != {normalized_hash}
                    or (
                        "canonical_request_id" in owner.keys()
                        and owner["canonical_request_id"] is not None
                    )
                ):
                    return False
                if is_alias and (
                    str(owner["service"] or "").casefold() != "abba"
                    or str(owner["media_type"] or "").casefold() != "audiobooks"
                ):
                    return False
                resolved_owner_ids.add(owner_id)

            if len(resolved_owner_ids) != 1:
                return False
            existing_ids = resolved_owner_ids
            if not existing_ids:
                return False

            owner = rows_by_id.get(next(iter(existing_ids)))
            if (
                status in {"complete", "completed"}
                and owner is not None
                and "service" in owner.keys()
                and "media_type" in owner.keys()
                and str(owner["service"] or "").casefold()
                == "lazylibrarian"
                and str(owner["media_type"] or "").casefold() == "ebooks"
                and source_category != "ebooks"
            ):
                # A Huey tag/hash proves the logical request, but not the
                # processing lane.  A drifted qBittorrent category can route
                # the same bytes to Comics or another library, which is not an
                # ebook final-library proof.  Only BookBot's exact ebook
                # CategorySpec may complete a LazyLibrarian ebook owner.
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
            event_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            required = {"request_id", "event_type", "message"}
            for request_id in sorted(existing_ids):
                if assignments:
                    connection.execute(
                        f"UPDATE requests SET {', '.join(assignments)} WHERE id = ?",
                        (*values, request_id),
                    )
                if required.issubset(event_columns):
                    connection.execute(
                        """
                        INSERT INTO events (request_id, event_type, message)
                        VALUES (?, ?, ?)
                        """,
                        (request_id, event_type, message[:2000]),
                    )
            return True

    @staticmethod
    def _request_ids_from_tags(tags: str) -> tuple[int, ...]:
        request_ids: list[int] = []
        for raw_token in str(tags or "").split(","):
            token = raw_token.strip()
            if not token:
                continue
            if not token.startswith("huey-"):
                if token.casefold().startswith(("huey-", "huey:")):
                    raise ValueError("Invalid Huey correlation tag")
                continue
            match = HUEY_TAG.fullmatch(token)
            if match is None:
                raise ValueError("Invalid Huey correlation tag")
            request_id = int(match.group(1))
            if request_id > 9_223_372_036_854_775_807:
                raise ValueError("Huey correlation tag is outside SQLite range")
            request_ids.append(request_id)
        return tuple(request_ids)
