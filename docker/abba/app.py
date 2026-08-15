"""Hardened JSON adapter for AudioBookBay Automated (ABBA).

The upstream image supplies Flask, Requests, BeautifulSoup, and the original
ABBA assets.  This overlay deliberately exposes only the machine-readable
interface used by Huey.  Candidate references and acquisition state live in a
persistent SQLite journal so neither Discord redelivery nor an ABBA restart can
cause a second acquisition.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request


# The journal can contain user request metadata.  Apply the restrictive mask
# before SQLite can create the main database, WAL, or shared-memory files.
os.umask(0o077)


SERVICE_NAME = "abba"
EXPECTED_CATEGORY = "audiobooks"
IMPORTED_CATEGORY = "audiobooks-imported"
EXPECTED_SAVE_PATH = "/downloads/audiobooks"
ALLOWED_ABB_HOSTNAMES = frozenset({"audiobookbay.lu", "audiobookbay.is"})
DEFAULT_TRACKERS = (
    "udp://tracker.openbittorrent.com:80",
    "udp://opentor.org:2710",
    "udp://tracker.ccc.de:80",
    "udp://tracker.blackunicorn.xyz:6969",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://tracker.leechers-paradise.org:6969",
)
INFO_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
CANDIDATE_ID_RE = re.compile(r"^abba:[0-9a-f]{64}$")
CORRELATION_ID_RE = re.compile(r"^huey:([1-9][0-9]{0,18})$")
SQLITE_MAX_REQUEST_ID = 9_223_372_036_854_775_807
URL_RE = re.compile(r"(?i)(?:\bhttps?://\S+|\bmagnet:\?\S+)")
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
SEARCH_CONTRACT_VERSION = 3
USER_AGENT = "WyseARR-ABBA/1"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
LOGGER = logging.getLogger("wysearr.abba")


def correlation_request_id(value: object) -> int | None:
    """Return a correlation's SQLite-safe positive request ID."""

    if not isinstance(value, str):
        return None
    match = CORRELATION_ID_RE.fullmatch(value)
    if match is None:
        return None
    request_id = int(match.group(1))
    return request_id if request_id <= SQLITE_MAX_REQUEST_ID else None


class AdapterError(Exception):
    """An expected failure that is safe to serialize."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class JobError(AdapterError):
    """An expected grab failure with a durable job representation."""

    def __init__(self, error: AdapterError, job: dict[str, Any]) -> None:
        super().__init__(
            error.code,
            error.message,
            error.http_status,
            retryable=error.retryable,
        )
        self.job = job


def _env_int(
    env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AdapterError("configuration_error", f"{name} must be an integer", 500) from exc
    if not minimum <= value <= maximum:
        raise AdapterError(
            "configuration_error",
            f"{name} must be between {minimum} and {maximum}",
            500,
        )
    return value


def _env_float(
    env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float
) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise AdapterError("configuration_error", f"{name} must be numeric", 500) from exc
    if not minimum <= value <= maximum:
        raise AdapterError(
            "configuration_error",
            f"{name} must be between {minimum} and {maximum}",
            500,
        )
    return value


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AdapterError("configuration_error", f"{name} must be true or false", 500)


@dataclass(frozen=True)
class Settings:
    abb_hostname: str
    qbittorrent_url: str
    qbittorrent_username: str
    qbittorrent_password: str
    database_path: Path
    port: int = 5078
    page_limit: int = 1
    search_cache_seconds: int = 300
    search_min_interval_seconds: float = 2.0
    result_ttl_seconds: int = 86400
    http_timeout_seconds: float = 15.0
    qbittorrent_timeout_seconds: float = 15.0
    verify_tls: bool = True
    max_results: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        download_client = source.get("DOWNLOAD_CLIENT", "qbittorrent").strip().lower()
        if download_client != "qbittorrent":
            raise AdapterError(
                "configuration_error", "DOWNLOAD_CLIENT must be qbittorrent", 500
            )

        abb_hostname = source.get("ABB_HOSTNAME", "audiobookbay.lu").strip().lower()
        if abb_hostname not in ALLOWED_ABB_HOSTNAMES:
            raise AdapterError(
                "configuration_error", "ABB_HOSTNAME is not an approved hostname", 500
            )

        page_limit = _env_int(source, "PAGE_LIMIT", 1, 1, 1)
        category = source.get("DL_CATEGORY", EXPECTED_CATEGORY).strip()
        save_path = source.get("SAVE_PATH_BASE", EXPECTED_SAVE_PATH).rstrip("/")
        if category != EXPECTED_CATEGORY:
            raise AdapterError(
                "configuration_error", "DL_CATEGORY must be audiobooks", 500
            )
        if save_path != EXPECTED_SAVE_PATH:
            raise AdapterError(
                "configuration_error",
                "SAVE_PATH_BASE must be /downloads/audiobooks",
                500,
            )

        dl_url = source.get("DL_URL", "").strip()
        if dl_url:
            parsed = urlsplit(dl_url)
            try:
                parsed_port = parsed.port
            except ValueError as exc:
                raise AdapterError(
                    "configuration_error", "DL_URL has an invalid port", 500
                ) from exc
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed_port is not None and not 1 <= parsed_port <= 65535
            ):
                raise AdapterError(
                    "configuration_error", "DL_URL must be an HTTP(S) URL", 500
                )
            if (
                parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise AdapterError(
                    "configuration_error",
                    "DL_URL must not contain credentials or a path",
                    500,
                )
            qbittorrent_url = dl_url.rstrip("/")
        else:
            scheme = source.get("DL_SCHEME", "http").strip().lower()
            host = source.get("DL_HOST", "qbittorrent").strip()
            port = _env_int(source, "DL_PORT", 8080, 1, 65535)
            if scheme not in {"http", "https"} or not host:
                raise AdapterError(
                    "configuration_error", "qBittorrent endpoint is invalid", 500
                )
            host_check = urlsplit(f"{scheme}://{host}:{port}")
            if host_check.hostname != host or host_check.username or host_check.password:
                raise AdapterError(
                    "configuration_error", "DL_HOST is invalid", 500
                )
            qbittorrent_url = f"{scheme}://{host}:{port}"

        username = source.get("DL_USERNAME", "").strip()
        password = source.get("DL_PASSWORD", "")
        if not username or not password:
            raise AdapterError(
                "configuration_error", "qBittorrent credentials are required", 500
            )

        database_path = Path(source.get("ABBA_DB_PATH", "/config/abba.db"))
        if database_path != Path("/config/abba.db"):
            raise AdapterError(
                "configuration_error",
                "ABBA_DB_PATH must be /config/abba.db",
                500,
            )

        settings = cls(
            abb_hostname=abb_hostname,
            qbittorrent_url=qbittorrent_url,
            qbittorrent_username=username,
            qbittorrent_password=password,
            database_path=database_path,
            port=_env_int(source, "PORT", 5078, 1, 65535),
            page_limit=page_limit,
            search_cache_seconds=_env_int(
                source, "ABBA_SEARCH_CACHE_SECONDS", 300, 1, 3600
            ),
            search_min_interval_seconds=_env_float(
                source, "ABBA_SEARCH_MIN_INTERVAL_SECONDS", 2.0, 0.0, 60.0
            ),
            result_ttl_seconds=_env_int(
                source, "ABBA_RESULT_TTL_SECONDS", 86400, 60, 604800
            ),
            http_timeout_seconds=_env_float(
                source, "ABBA_HTTP_TIMEOUT_SECONDS", 15.0, 1.0, 60.0
            ),
            qbittorrent_timeout_seconds=_env_float(
                source, "DL_TIMEOUT_SECONDS", 15.0, 1.0, 60.0
            ),
            verify_tls=_env_bool(source, "DL_VERIFY_TLS", True),
            max_results=_env_int(source, "ABBA_MAX_RESULTS", 10, 1, 10),
        )
        if settings.result_ttl_seconds < settings.search_cache_seconds:
            raise AdapterError(
                "configuration_error",
                "ABBA_RESULT_TTL_SECONDS must not be shorter than the search cache",
                500,
            )
        return settings


def normalize_display(value: Any, maximum: int = 200) -> str:
    """Return bounded text safe for downstream chat presentation."""

    if not isinstance(value, str):
        return ""
    value = html.unescape(value)
    value = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    value = URL_RE.sub("[link removed]", value)
    value = value.replace("@", "＠").replace("`", "'")
    value = value.replace("<", "‹").replace(">", "›")
    return " ".join(value.split())[:maximum].strip()


def normalize_identity(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def normalize_save_path(value: str) -> str:
    if not value:
        return ""
    normalized = str(PurePosixPath(value))
    return normalized.rstrip("/") or "/"


def build_error(code: str, message: str, status: int, retryable: bool = False) -> AdapterError:
    return AdapterError(code, message, status, retryable=retryable)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    path: str
    title: str
    query_title: str
    query_author: str | None = None
    author: str | None = None
    narrator: str | None = None
    year: int | None = None
    format: str | None = None
    edition: str | None = None
    size_bytes: int | None = None
    fingerprint: str = ""

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.candidate_id, "title": self.title}
        for name in ("author", "narrator", "year", "format", "edition", "size_bytes"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True)
class ResolvedResult:
    title: str
    info_hash: str
    magnet: str


class Journal:
    """Small durable journal for candidates, caches, and acquisitions."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            # A bind mount may not permit chmod; the file itself is still 0600.
            pass
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS candidates (
                        candidate_id TEXT PRIMARY KEY,
                        path TEXT NOT NULL,
                        title TEXT NOT NULL,
                        query_title TEXT NOT NULL,
                        query_author TEXT,
                        author TEXT,
                        narrator TEXT,
                        year INTEGER,
                        format TEXT,
                        edition TEXT,
                        size_bytes INTEGER,
                        fingerprint TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS search_cache (
                        cache_key TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS service_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS acquisitions (
                        correlation_id TEXT PRIMARY KEY,
                        candidate_id TEXT NOT NULL,
                        info_hash TEXT,
                        title TEXT,
                        category TEXT NOT NULL CHECK(category = 'audiobooks'),
                        save_path TEXT NOT NULL CHECK(save_path = '/downloads/audiobooks'),
                        tag TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN (
                            'prepared', 'submitting', 'queued',
                            'submission_uncertain', 'failed'
                        )),
                        error_code TEXT,
                        error_message TEXT,
                        error_retryable INTEGER NOT NULL DEFAULT 0,
                        error_http_status INTEGER,
                        canonical_correlation_id TEXT,
                        canonical_candidate_correlation_id TEXT,
                        mutation_started_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        CHECK (
                            canonical_correlation_id IS NULL
                            OR canonical_correlation_id != correlation_id
                        ),
                        CHECK (
                            canonical_candidate_correlation_id IS NULL
                            OR canonical_candidate_correlation_id != correlation_id
                        )
                    );
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(acquisitions)"
                    ).fetchall()
                }
                if "canonical_correlation_id" not in columns:
                    connection.execute(
                        "ALTER TABLE acquisitions "
                        "ADD COLUMN canonical_correlation_id TEXT"
                    )
                if "mutation_started_at" not in columns:
                    connection.execute(
                        "ALTER TABLE acquisitions ADD COLUMN mutation_started_at REAL"
                    )
                if "canonical_candidate_correlation_id" not in columns:
                    connection.execute(
                        "ALTER TABLE acquisitions "
                        "ADD COLUMN canonical_candidate_correlation_id TEXT"
                    )
                connection.executescript(
                    """
                    DROP INDEX IF EXISTS acquisitions_hash_owner_uq;
                    DROP INDEX IF EXISTS acquisitions_candidate_owner_uq;
                    """
                )

                # Older adapters permitted more than one correlation to journal
                # the same v1 hash.  Collapse those rows deterministically before
                # installing the ownership constraint.  Both the adapter and
                # Huey prefer a retained nonfailed owner and then the lowest
                # positive request ID, so independent migrations cannot disagree.
                connection.execute(
                    """
                    UPDATE acquisitions
                    SET mutation_started_at = COALESCE(
                        mutation_started_at, updated_at, created_at
                    )
                    WHERE info_hash IS NOT NULL
                      AND state IN (
                          'submitting', 'queued', 'submission_uncertain'
                      )
                    """
                )
                # Prepared and pre-mutation failed rows are normally released.
                # Legacy adapters could already have pointed a durable hash or
                # candidate alias at one, including at a candidate owner which
                # is itself a hash alias.  Retain any referenced owner
                # conservatively so a later failure cannot orphan descendants.
                # New live contenders are refused below instead.
                connection.execute(
                    """
                    UPDATE acquisitions
                    SET mutation_started_at = COALESCE(
                        mutation_started_at, updated_at, created_at
                    )
                    WHERE state IN ('prepared', 'failed')
                      AND mutation_started_at IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM acquisitions AS alias
                          WHERE alias.canonical_correlation_id =
                                    acquisitions.correlation_id
                             OR alias.canonical_candidate_correlation_id =
                                    acquisitions.correlation_id
                      )
                    """
                )
                duplicate_hashes = connection.execute(
                    """
                    SELECT info_hash
                    FROM acquisitions
                    WHERE info_hash IS NOT NULL
                      AND canonical_correlation_id IS NULL
                      AND (
                          state != 'failed'
                          OR mutation_started_at IS NOT NULL
                      )
                    GROUP BY info_hash
                    HAVING COUNT(*) > 1
                    ORDER BY info_hash
                    """
                ).fetchall()
                for duplicate in duplicate_hashes:
                    rows = connection.execute(
                        """
                        SELECT correlation_id
                        FROM acquisitions
                        WHERE info_hash = ?
                          AND canonical_correlation_id IS NULL
                          AND (
                              state != 'failed'
                              OR mutation_started_at IS NOT NULL
                        )
                        ORDER BY
                            CASE WHEN state = 'failed' THEN 1 ELSE 0 END,
                            length(substr(correlation_id, 6)),
                            substr(correlation_id, 6),
                            correlation_id
                        """,
                        (duplicate["info_hash"],),
                    ).fetchall()
                    canonical = str(rows[0]["correlation_id"])
                    connection.execute(
                        """
                        UPDATE acquisitions
                        SET mutation_started_at = COALESCE(
                            mutation_started_at, updated_at, created_at
                        )
                        WHERE correlation_id = ?
                          AND state = 'prepared'
                          AND mutation_started_at IS NULL
                        """,
                        (canonical,),
                    )
                    for alias in rows[1:]:
                        connection.execute(
                            """
                            UPDATE acquisitions
                            SET canonical_correlation_id = ?,
                                updated_at = MAX(updated_at, created_at)
                            WHERE correlation_id = ?
                              AND correlation_id != ?
                            """,
                            (
                                canonical,
                                str(alias["correlation_id"]),
                                canonical,
                            ),
                        )

                # A newly discovered lower canonical root can make an older
                # root an alias.  Flatten every pre-existing descendant for
                # that hash directly to the elected root; canonical lookups
                # intentionally reject alias chains.
                aliased_hashes = connection.execute(
                    """
                    SELECT DISTINCT info_hash
                    FROM acquisitions
                    WHERE info_hash IS NOT NULL
                      AND canonical_correlation_id IS NOT NULL
                    ORDER BY info_hash
                    """
                ).fetchall()
                for aliased in aliased_hashes:
                    root = connection.execute(
                        """
                        SELECT correlation_id
                        FROM acquisitions
                        WHERE info_hash = ?
                          AND canonical_correlation_id IS NULL
                          AND (
                              state != 'failed'
                              OR mutation_started_at IS NOT NULL
                          )
                        ORDER BY
                            CASE WHEN state = 'failed' THEN 1 ELSE 0 END,
                            length(substr(correlation_id, 6)),
                            substr(correlation_id, 6),
                            correlation_id
                        LIMIT 1
                        """,
                        (aliased["info_hash"],),
                    ).fetchone()
                    if root is None:
                        continue
                    root_correlation = str(root["correlation_id"])
                    connection.execute(
                        """
                        UPDATE acquisitions
                        SET canonical_correlation_id = ?,
                            updated_at = MAX(updated_at, created_at)
                        WHERE info_hash = ?
                          AND correlation_id != ?
                          AND canonical_correlation_id IS NOT NULL
                          AND canonical_correlation_id != ?
                        """,
                        (
                            root_correlation,
                            aliased["info_hash"],
                            root_correlation,
                            root_correlation,
                        ),
                    )

                # Candidate IDs are also durable identities.  Same-hash rows
                # have already converged above.  A legacy candidate which
                # resolved to a different hash cannot be treated as a hash
                # alias: quarantine its candidate ownership while retaining
                # any post-mutation hash ownership for fail-closed recovery.
                duplicate_candidates = connection.execute(
                    """
                    SELECT candidate_id
                    FROM acquisitions
                    WHERE canonical_candidate_correlation_id IS NULL
                      AND (
                          state != 'failed'
                          OR mutation_started_at IS NOT NULL
                      )
                    GROUP BY candidate_id
                    HAVING COUNT(*) > 1
                    ORDER BY candidate_id
                    """
                ).fetchall()
                for duplicate in duplicate_candidates:
                    rows = connection.execute(
                        """
                        SELECT correlation_id, info_hash
                        FROM acquisitions
                        WHERE candidate_id = ?
                          AND canonical_candidate_correlation_id IS NULL
                          AND (
                              state != 'failed'
                              OR mutation_started_at IS NOT NULL
                          )
                        ORDER BY
                            CASE
                                WHEN state != 'failed' AND (
                                    state != 'prepared'
                                    OR mutation_started_at IS NOT NULL
                                    OR canonical_correlation_id IS NOT NULL
                                ) THEN 0
                                WHEN state = 'failed' THEN 1
                                ELSE 2
                            END,
                            length(substr(correlation_id, 6)),
                            substr(correlation_id, 6),
                            correlation_id
                        """,
                        (duplicate["candidate_id"],),
                    ).fetchall()
                    canonical = str(rows[0]["correlation_id"])
                    canonical_hash = str(rows[0]["info_hash"] or "")
                    connection.execute(
                        """
                        UPDATE acquisitions
                        SET mutation_started_at = COALESCE(
                            mutation_started_at, updated_at, created_at
                        )
                        WHERE correlation_id = ?
                          AND canonical_correlation_id IS NULL
                          AND state = 'prepared'
                          AND mutation_started_at IS NULL
                        """,
                        (canonical,),
                    )
                    for conflict in rows[1:]:
                        conflict_hash = str(conflict["info_hash"] or "")
                        same_hash = bool(
                            canonical_hash and conflict_hash == canonical_hash
                        )
                        connection.execute(
                            """
                            UPDATE acquisitions
                            SET canonical_candidate_correlation_id = ?,
                                state = CASE WHEN ? THEN state ELSE 'failed' END,
                                error_code = CASE
                                    WHEN ? THEN error_code ELSE 'result_changed'
                                END,
                                error_message = CASE
                                    WHEN ? THEN error_message
                                    ELSE 'The selected AudioBookBay result changed'
                                END,
                                error_retryable = CASE
                                    WHEN ? THEN error_retryable ELSE 0
                                END,
                                error_http_status = CASE
                                    WHEN ? THEN error_http_status ELSE 409
                                END,
                                updated_at = MAX(updated_at, created_at)
                            WHERE correlation_id = ?
                              AND correlation_id != ?
                            """,
                            (
                                canonical,
                                int(same_hash),
                                int(same_hash),
                                int(same_hash),
                                int(same_hash),
                                int(same_hash),
                                str(conflict["correlation_id"]),
                                canonical,
                            ),
                        )

                # Candidate ownership is independent of hash ownership and can
                # have the same legacy chain shape.  Reparent every candidate
                # alias directly and preserve only same-hash aliases as benign;
                # aliases whose candidate now points at another hash remain a
                # durable result-changed conflict.
                aliased_candidates = connection.execute(
                    """
                    SELECT DISTINCT candidate_id
                    FROM acquisitions
                    WHERE canonical_candidate_correlation_id IS NOT NULL
                    ORDER BY candidate_id
                    """
                ).fetchall()
                for aliased in aliased_candidates:
                    root = connection.execute(
                        """
                        SELECT correlation_id, info_hash
                        FROM acquisitions
                        WHERE candidate_id = ?
                          AND canonical_candidate_correlation_id IS NULL
                          AND (
                              state != 'failed'
                              OR mutation_started_at IS NOT NULL
                          )
                        ORDER BY
                            CASE
                                WHEN state != 'failed' AND (
                                    state != 'prepared'
                                    OR mutation_started_at IS NOT NULL
                                    OR canonical_correlation_id IS NOT NULL
                                ) THEN 0
                                WHEN state = 'failed' THEN 1
                                ELSE 2
                            END,
                            length(substr(correlation_id, 6)),
                            substr(correlation_id, 6),
                            correlation_id
                        LIMIT 1
                        """,
                        (aliased["candidate_id"],),
                    ).fetchone()
                    if root is None:
                        continue
                    root_correlation = str(root["correlation_id"])
                    root_hash = str(root["info_hash"] or "")
                    aliases = connection.execute(
                        """
                        SELECT correlation_id, info_hash
                        FROM acquisitions
                        WHERE candidate_id = ?
                          AND correlation_id != ?
                          AND canonical_candidate_correlation_id IS NOT NULL
                        """,
                        (aliased["candidate_id"], root_correlation),
                    ).fetchall()
                    for candidate_alias in aliases:
                        alias_hash = str(candidate_alias["info_hash"] or "")
                        same_hash = bool(root_hash and alias_hash == root_hash)
                        connection.execute(
                            """
                            UPDATE acquisitions
                            SET canonical_candidate_correlation_id = ?,
                                state = CASE WHEN ? THEN state ELSE 'failed' END,
                                error_code = CASE
                                    WHEN ? THEN error_code ELSE 'result_changed'
                                END,
                                error_message = CASE
                                    WHEN ? THEN error_message
                                    ELSE 'The selected AudioBookBay result changed'
                                END,
                                error_retryable = CASE
                                    WHEN ? THEN error_retryable ELSE 0
                                END,
                                error_http_status = CASE
                                    WHEN ? THEN error_http_status ELSE 409
                                END,
                                updated_at = MAX(updated_at, created_at)
                            WHERE correlation_id = ?
                            """,
                            (
                                root_correlation,
                                int(same_hash),
                                int(same_hash),
                                int(same_hash),
                                int(same_hash),
                                int(same_hash),
                                str(candidate_alias["correlation_id"]),
                            ),
                        )
                connection.executescript(
                    """
                    DROP INDEX IF EXISTS acquisitions_info_hash_idx;
                    DROP INDEX IF EXISTS acquisitions_hash_owner_uq;
                    DROP INDEX IF EXISTS acquisitions_candidate_owner_uq;
                    CREATE INDEX IF NOT EXISTS acquisitions_info_hash_idx
                    ON acquisitions(info_hash);
                    CREATE UNIQUE INDEX acquisitions_hash_owner_uq
                    ON acquisitions(info_hash)
                    WHERE info_hash IS NOT NULL
                      AND canonical_correlation_id IS NULL
                      AND (
                          state != 'failed'
                          OR mutation_started_at IS NOT NULL
                      );
                    CREATE UNIQUE INDEX acquisitions_candidate_owner_uq
                    ON acquisitions(candidate_id)
                    WHERE canonical_candidate_correlation_id IS NULL
                      AND (
                          state != 'failed'
                          OR mutation_started_at IS NOT NULL
                      );
                    CREATE INDEX IF NOT EXISTS acquisitions_canonical_idx
                    ON acquisitions(canonical_correlation_id);
                    CREATE INDEX IF NOT EXISTS acquisitions_candidate_canonical_idx
                    ON acquisitions(canonical_candidate_correlation_id);
                    """
                )
            self.path.chmod(0o600)
        except (OSError, sqlite3.Error) as exc:
            raise AdapterError(
                "database_unavailable",
                "ABBA acquisition journal is unavailable",
                503,
                retryable=True,
            ) from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            # BEGIN itself can fail (read-only volume, lock exhaustion).  Never
            # mask that useful original error with a second invalid ROLLBACK.
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()

    def ping(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO service_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("health_write_probe", str(self.clock())),
            )
            connection.execute("ROLLBACK")
            self.path.chmod(0o600)
        except (OSError, sqlite3.Error) as exc:
            if connection is not None and connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise AdapterError(
                "database_unavailable",
                "ABBA acquisition journal is unavailable",
                503,
                retryable=True,
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _prune_expired(connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM search_cache WHERE expires_at <= ?", (now,))
        connection.execute(
            "DELETE FROM candidates WHERE expires_at <= ? AND NOT EXISTS ("
            "SELECT 1 FROM acquisitions WHERE acquisitions.candidate_id = candidates.candidate_id"
            ")",
            (now,),
        )

    def cached_search(self, cache_key: str) -> list[dict[str, Any]] | None:
        now = self.clock()
        with self._transaction() as connection:
            self._prune_expired(connection, now)
            row = connection.execute(
                "SELECT payload_json FROM search_cache "
                "WHERE cache_key = ? AND expires_at > ?",
                (cache_key, now),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            return None
        return payload

    def claim_search_slot(self, minimum_interval: float) -> float:
        now = self.clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT value FROM service_state WHERE key = 'last_search_at'"
            ).fetchone()
            last_search = float(row["value"]) if row is not None else 0.0
            retry_after = minimum_interval - (now - last_search)
            if retry_after > 0:
                return retry_after
            connection.execute(
                "INSERT INTO service_state(key, value) VALUES('last_search_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(now),),
            )
        return 0.0

    def store_search(
        self,
        cache_key: str,
        candidates: list[Candidate],
        cache_seconds: int,
        result_ttl_seconds: int,
    ) -> None:
        now = self.clock()
        public_payload = [candidate.public_dict() for candidate in candidates]
        with self._transaction() as connection:
            self._prune_expired(connection, now)
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO candidates(
                        candidate_id, path, title, query_title, query_author,
                        author, narrator, year, format, edition, size_bytes,
                        fingerprint, created_at, last_seen_at, expires_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        path = excluded.path,
                        title = excluded.title,
                        query_title = excluded.query_title,
                        query_author = excluded.query_author,
                        author = excluded.author,
                        narrator = excluded.narrator,
                        year = excluded.year,
                        format = excluded.format,
                        edition = excluded.edition,
                        size_bytes = excluded.size_bytes,
                        fingerprint = excluded.fingerprint,
                        last_seen_at = excluded.last_seen_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        candidate.candidate_id,
                        candidate.path,
                        candidate.title,
                        candidate.query_title,
                        candidate.query_author,
                        candidate.author,
                        candidate.narrator,
                        candidate.year,
                        candidate.format,
                        candidate.edition,
                        candidate.size_bytes,
                        candidate.fingerprint,
                        now,
                        now,
                        now + result_ttl_seconds,
                    ),
                )
            connection.execute(
                "INSERT INTO search_cache(cache_key, payload_json, created_at, expires_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload_json = excluded.payload_json, created_at = excluded.created_at, "
                "expires_at = excluded.expires_at",
                (
                    cache_key,
                    json.dumps(public_payload, separators=(",", ":"), sort_keys=True),
                    now,
                    now + cache_seconds,
                ),
            )

    def candidate(self, candidate_id: str) -> Candidate | None:
        now = self.clock()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ? AND expires_at > ?",
                (candidate_id, now),
            ).fetchone()
        if row is None:
            return None
        return Candidate(
            candidate_id=row["candidate_id"],
            path=row["path"],
            title=row["title"],
            query_title=row["query_title"],
            query_author=row["query_author"],
            author=row["author"],
            narrator=row["narrator"],
            year=row["year"],
            format=row["format"],
            edition=row["edition"],
            size_bytes=row["size_bytes"],
            fingerprint=row["fingerprint"],
        )

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def acquisition(self, correlation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM acquisitions WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
        return self._row_dict(row)

    def acquisition_for_hash(self, info_hash: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM acquisitions WHERE info_hash = ? "
                "AND canonical_correlation_id IS NULL "
                "AND (state != 'failed' OR mutation_started_at IS NOT NULL) "
                "ORDER BY created_at ASC LIMIT 1",
                (info_hash,),
            ).fetchone()
        return self._row_dict(row)

    def canonical_acquisition(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        """Return the one canonical row named by a durable alias."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT canonical.*
                FROM acquisitions AS alias
                JOIN acquisitions AS canonical
                  ON canonical.correlation_id = alias.canonical_correlation_id
                WHERE alias.correlation_id = ?
                  AND alias.canonical_correlation_id IS NOT NULL
                  AND canonical.canonical_correlation_id IS NULL
                  AND canonical.info_hash = alias.info_hash
                """,
                (correlation_id,),
            ).fetchone()
        return self._row_dict(row)

    def _insert_or_existing(
        self,
        correlation_id: str,
        candidate_id: str,
        info_hash: str | None,
        title: str | None,
        tag: str,
        state: str,
        error: AdapterError | None,
    ) -> dict[str, Any]:
        now = self.clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM acquisitions WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
            if row is not None:
                existing = dict(row)
                if existing["candidate_id"] != candidate_id:
                    raise AdapterError(
                        "request_conflict",
                        "Correlation ID is already bound to another candidate",
                        409,
                    )
                if (
                    info_hash is not None
                    and existing["info_hash"] is not None
                    and existing["info_hash"] != info_hash
                ):
                    raise AdapterError(
                        "result_changed",
                        "The selected AudioBookBay result changed",
                        409,
                    )
                if existing["tag"] != tag:
                    raise AdapterError(
                        "request_conflict",
                        "Correlation ID has inconsistent routing metadata",
                        409,
                    )
                return existing
            canonical = None
            candidate_owner = connection.execute(
                """
                SELECT * FROM acquisitions
                WHERE candidate_id = ?
                  AND canonical_candidate_correlation_id IS NULL
                  AND (
                      state != 'failed'
                      OR mutation_started_at IS NOT NULL
                  )
                ORDER BY created_at, correlation_id
                LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            candidate_conflict = None
            candidate_alias_owner = None
            if candidate_owner is not None:
                if (
                    candidate_owner["state"] == "prepared"
                    and candidate_owner["mutation_started_at"] is None
                    and candidate_owner["canonical_correlation_id"] is None
                ):
                    raise AdapterError(
                        "acquisition_pending",
                        "Audiobook acquisition ownership is still being established",
                        503,
                        retryable=True,
                    )
                owner_hash = str(candidate_owner["info_hash"] or "")
                if info_hash is not None and owner_hash == info_hash:
                    candidate_alias_owner = candidate_owner
                    hash_owner_correlation = str(
                        candidate_owner["canonical_correlation_id"]
                        or candidate_owner["correlation_id"]
                    )
                    canonical = connection.execute(
                        """
                        SELECT * FROM acquisitions
                        WHERE correlation_id = ?
                          AND info_hash = ?
                          AND canonical_correlation_id IS NULL
                        """,
                        (hash_owner_correlation, info_hash),
                    ).fetchone()
                    if canonical is None:
                        raise sqlite3.IntegrityError(
                            "Candidate owner has no canonical hash owner"
                        )
                else:
                    candidate_conflict = candidate_owner
            if info_hash is not None:
                if canonical is None:
                    canonical = connection.execute(
                        """
                        SELECT * FROM acquisitions
                        WHERE info_hash = ?
                          AND canonical_correlation_id IS NULL
                          AND (
                              state != 'failed'
                              OR mutation_started_at IS NOT NULL
                          )
                        ORDER BY created_at, correlation_id
                        LIMIT 1
                        """,
                        (info_hash,),
                    ).fetchone()
            if (
                canonical is not None
                and canonical["state"] == "prepared"
                and canonical["mutation_started_at"] is None
            ):
                raise AdapterError(
                    "acquisition_pending",
                    "Audiobook acquisition ownership is still being established",
                    503,
                    retryable=True,
                )
            canonical_correlation_id = (
                str(canonical["correlation_id"])
                if canonical is not None
                and str(canonical["correlation_id"]) != correlation_id
                else None
            )
            canonical_candidate_correlation_id = (
                str((candidate_conflict or candidate_alias_owner)["correlation_id"])
                if (candidate_conflict or candidate_alias_owner) is not None
                and str((candidate_conflict or candidate_alias_owner)["correlation_id"])
                != correlation_id
                else None
            )
            conflict_error = (
                AdapterError(
                    "result_changed",
                    "The selected AudioBookBay result changed",
                    409,
                )
                if candidate_conflict is not None
                else None
            )
            inserted_state = "failed" if conflict_error is not None else state
            inserted_error = conflict_error or error
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_code, error_message,
                    error_retryable, error_http_status,
                    canonical_correlation_id,
                    canonical_candidate_correlation_id,
                    mutation_started_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correlation_id,
                    candidate_id,
                    info_hash,
                    title,
                    EXPECTED_CATEGORY,
                    EXPECTED_SAVE_PATH,
                    tag,
                    inserted_state,
                    inserted_error.code if inserted_error else None,
                    inserted_error.message if inserted_error else None,
                    int(inserted_error.retryable) if inserted_error else 0,
                    inserted_error.http_status if inserted_error else None,
                    canonical_correlation_id,
                    canonical_candidate_correlation_id,
                    None,
                    now,
                    now,
                ),
            )
            inserted = connection.execute(
                "SELECT * FROM acquisitions WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
        assert inserted is not None
        return dict(inserted)

    def prepare(
        self,
        correlation_id: str,
        candidate_id: str,
        info_hash: str,
        title: str,
        tag: str,
    ) -> dict[str, Any]:
        return self._insert_or_existing(
            correlation_id,
            candidate_id,
            info_hash,
            title,
            tag,
            "prepared",
            None,
        )

    def terminal_failure(
        self,
        correlation_id: str,
        candidate_id: str,
        title: str | None,
        tag: str,
        error: AdapterError,
        info_hash: str | None = None,
    ) -> dict[str, Any]:
        row = self._insert_or_existing(
            correlation_id,
            candidate_id,
            info_hash,
            title,
            tag,
            "failed",
            error,
        )
        # The prepare record is committed before any qBittorrent mutation.  A
        # later definitive rejection must therefore replace that non-terminal
        # state rather than silently returning it unchanged.
        if (
            row.get("canonical_correlation_id") is not None
            or row.get("canonical_candidate_correlation_id") is not None
        ):
            return row
        if row["state"] != "failed":
            return self.update(correlation_id, "failed", error)
        return row

    def update(
        self,
        correlation_id: str,
        state: str,
        error: AdapterError | None = None,
        *,
        mutation_started: bool = False,
    ) -> dict[str, Any]:
        now = self.clock()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE acquisitions SET state = ?, error_code = ?,
                    error_message = ?, error_retryable = ?,
                    error_http_status = ?, mutation_started_at = CASE
                        WHEN ? THEN COALESCE(mutation_started_at, ?)
                        ELSE mutation_started_at
                    END, updated_at = ?
                WHERE correlation_id = ?
                  AND canonical_correlation_id IS NULL
                  AND canonical_candidate_correlation_id IS NULL
                """,
                (
                    state,
                    error.code if error else None,
                    error.message if error else None,
                    int(error.retryable) if error else 0,
                    error.http_status if error else None,
                    int(bool(mutation_started)),
                    now,
                    now,
                    correlation_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM acquisitions WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
        if row is None:
            raise AdapterError("journal_error", "Acquisition journal entry is missing", 500)
        return dict(row)


def _parse_size_bytes(value: str) -> int | None:
    match = re.search(
        r"(?i)\b(\d+(?:\.\d+)?)\s*(B|KB|KIB|MB|MIB|GB|GIB|TB|TIB)\b", value
    )
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).upper()
    powers = {
        "B": 0,
        "KB": 1,
        "KIB": 1,
        "MB": 2,
        "MIB": 2,
        "GB": 3,
        "GIB": 3,
        "TB": 4,
        "TIB": 4,
    }
    return int(amount * (1024 ** powers[unit]))


def _labeled_value(text: str, labels: tuple[str, ...], maximum: int = 160) -> str | None:
    for label in labels:
        match = re.search(
            rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", text
        )
        if match:
            value = normalize_display(match.group(1), maximum)
            if value and value.casefold() not in {"n/a", "unknown"}:
                return value
    return None


class ABBClient:
    """Strictly bounded reuse of ABBA's HTML search and magnet extraction."""

    _DETAIL_AUDIO_FORMAT = re.compile(
        r"\b(mp3|m4b|m4a|aac|flac|ogg|opus|wav)\b", re.IGNORECASE
    )

    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._request_lock = threading.RLock()

    def _validated_url(self, value: str) -> str:
        parsed = urlsplit(value)
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise AdapterError(
                "malformed_upstream", "AudioBookBay returned an invalid reference", 502
            ) from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.settings.abb_hostname
            or parsed.username
            or parsed.password
            or parsed_port not in {None, 443}
            or parsed.fragment
        ):
            raise AdapterError(
                "malformed_upstream", "AudioBookBay returned an invalid reference", 502
            )
        decoded_path = unquote(parsed.path)
        if not parsed.path.startswith("/") or ".." in decoded_path.split("/"):
            raise AdapterError(
                "malformed_upstream", "AudioBookBay returned an invalid reference", 502
            )
        return value

    def _candidate_path(self, href: str) -> str:
        if not isinstance(href, str) or len(href) > 768:
            raise AdapterError(
                "malformed_upstream", "AudioBookBay returned an invalid result", 502
            )
        absolute = self._validated_url(
            urljoin(f"https://{self.settings.abb_hostname}/", href)
        )
        parsed = urlsplit(absolute)
        decoded_path = unquote(parsed.path)
        allowed_result_path = parsed.path.startswith(
            ("/abss/", "/audio-books/")
        )
        if not allowed_result_path or parsed.query or ".." in decoded_path.split("/"):
            raise AdapterError(
                "malformed_upstream", "AudioBookBay returned an invalid result", 502
            )
        return parsed.path

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        missing_is_changed: bool = False,
    ) -> requests.Response:
        return self._request(
            "GET", path, params=params, missing_is_changed=missing_is_changed
        )

    def _post_search(self, query: str) -> requests.Response:
        return self._request(
            "POST",
            "/",
            form_data={"s": query},
            headers={"Referer": f"https://{self.settings.abb_hostname}/"},
            reject_redirects=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        form_data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        missing_is_changed: bool = False,
        reject_redirects: bool = False,
    ) -> requests.Response:
        # requests.Session mutates cookie/header state and is not documented as
        # thread-safe.  Keep redirects and streamed consumption under the same
        # lock while allowing unrelated /health work to proceed through qBit.
        with self._request_lock:
            return self._request_locked(
                method,
                path,
                params=params,
                form_data=form_data,
                headers=headers,
                missing_is_changed=missing_is_changed,
                reject_redirects=reject_redirects,
            )

    def _request_locked(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None,
        form_data: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        missing_is_changed: bool,
        reject_redirects: bool,
    ) -> requests.Response:
        url = self._validated_url(
            urljoin(f"https://{self.settings.abb_hostname}/", path)
        )
        request_params = dict(params or {})
        request_data = dict(form_data or {})
        for _redirect in range(4):
            try:
                if method == "GET":
                    response = self.session.get(
                        url,
                        params=request_params or None,
                        timeout=self.settings.http_timeout_seconds,
                        allow_redirects=False,
                        stream=True,
                    )
                elif method == "POST":
                    response = self.session.post(
                        url,
                        data=request_data,
                        headers=dict(headers or {}),
                        timeout=self.settings.http_timeout_seconds,
                        allow_redirects=False,
                        stream=True,
                    )
                else:  # pragma: no cover - private invariant
                    raise RuntimeError("Unsupported ABB request method")
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise AdapterError(
                    "abb_unreachable",
                    "AudioBookBay is temporarily unreachable",
                    503,
                    retryable=True,
                ) from exc
            except requests.RequestException as exc:
                raise AdapterError(
                    "abb_unreachable",
                    "AudioBookBay request failed",
                    503,
                    retryable=True,
                ) from exc

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                if reject_redirects:
                    raise AdapterError(
                        "malformed_upstream",
                        "AudioBookBay redirected the search submission",
                        502,
                    )
                if not location:
                    raise AdapterError(
                        "malformed_upstream", "AudioBookBay returned an invalid redirect", 502
                    )
                url = self._validated_url(urljoin(url, location))
                request_params = {}
                continue
            if response.status_code == 404 and missing_is_changed:
                response.close()
                raise AdapterError(
                    "result_changed",
                    "The selected AudioBookBay result is no longer available",
                    409,
                )
            if response.status_code == 429 or response.status_code >= 500:
                response.close()
                raise AdapterError(
                    "abb_unreachable",
                    "AudioBookBay is temporarily unavailable",
                    503,
                    retryable=True,
                )
            if response.status_code >= 400:
                response.close()
                raise AdapterError(
                    "malformed_upstream", "AudioBookBay rejected the request", 502
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    response.close()
                    raise AdapterError(
                        "malformed_upstream",
                        "AudioBookBay returned an invalid response",
                        502,
                    ) from exc
                if declared_length < 0 or declared_length > MAX_UPSTREAM_BYTES:
                    response.close()
                    raise AdapterError(
                        "malformed_upstream",
                        "AudioBookBay returned an oversized response",
                        502,
                    )
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=65536):
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise AdapterError(
                            "malformed_upstream",
                            "AudioBookBay returned an invalid response",
                            502,
                        )
                    body.extend(chunk)
                    if len(body) > MAX_UPSTREAM_BYTES:
                        raise AdapterError(
                            "malformed_upstream",
                            "AudioBookBay returned an oversized response",
                            502,
                        )
            except requests.RequestException as exc:
                raise AdapterError(
                    "abb_unreachable",
                    "AudioBookBay response was interrupted",
                    503,
                    retryable=True,
                ) from exc
            finally:
                response.close()
            if not body:
                raise AdapterError(
                    "malformed_upstream", "AudioBookBay returned an invalid response", 502
                )
            response._content = bytes(body)
            response._content_consumed = True
            return response
        raise AdapterError(
            "malformed_upstream", "AudioBookBay returned too many redirects", 502
        )

    @staticmethod
    def _details_values(post: Any) -> tuple[str | None, str | None, int | None, str | None, str | None, int | None]:
        text = post.get_text("\n", strip=True)
        author = _labeled_value(text, ("Author", "Written by"))
        narrator = _labeled_value(text, ("Narrator", "Reader", "Read by"))
        year_raw = _labeled_value(text, ("Year", "Release year"), 8)
        year = int(year_raw) if year_raw and re.fullmatch(r"(?:19|20)\d{2}", year_raw) else None
        book_format = _labeled_value(text, ("Format",), 80)
        edition = _labeled_value(text, ("Edition",), 120)
        size_raw = _labeled_value(text, ("File Size", "Size"), 80) or text
        size_bytes = _parse_size_bytes(size_raw)
        return author, narrator, year, book_format, edition, size_bytes

    def search(self, title: str, author: str | None, limit: int) -> list[Candidate]:
        query = title if not author else f"{title} {author}"
        response = self._post_search(query)
        soup = BeautifulSoup(response.text, "html.parser")
        posts = soup.select(".post")
        candidates: list[Candidate] = []
        seen_ids: set[str] = set()
        malformed = 0
        for post in posts:
            if len(candidates) >= limit:
                break
            try:
                anchor = post.select_one(".postTitle > h2 > a")
                if anchor is None:
                    raise ValueError("missing title")
                title_value = normalize_display(anchor.get_text(" ", strip=True), 200)
                href = anchor.get("href")
                if not title_value or not isinstance(href, str):
                    raise ValueError("invalid title")
                path = self._candidate_path(href)
                author_value, narrator, year, book_format, edition, size_bytes = (
                    self._details_values(post)
                )
                candidate_id = "abba:" + hashlib.sha256(path.encode("utf-8")).hexdigest()
                if candidate_id in seen_ids:
                    continue
                seen_ids.add(candidate_id)
                metadata = {
                    "path": path,
                    "title": title_value,
                    "author": author_value,
                    "narrator": narrator,
                    "year": year,
                    "format": book_format,
                    "edition": edition,
                    "size_bytes": size_bytes,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                candidates.append(
                    Candidate(
                        candidate_id=candidate_id,
                        path=path,
                        title=title_value,
                        query_title=title,
                        query_author=author,
                        author=author_value,
                        narrator=narrator,
                        year=year,
                        format=book_format,
                        edition=edition,
                        size_bytes=size_bytes,
                        fingerprint=fingerprint,
                    )
                )
            except (AdapterError, KeyError, TypeError, ValueError):
                malformed += 1
        if posts and not candidates and malformed:
            raise AdapterError(
                "malformed_upstream", "AudioBookBay returned malformed search results", 502
            )
        if not posts:
            page_text = normalize_display(soup.get_text(" ", strip=True), 2000).casefold()
            explicit_empty = re.search(
                r"\b(?:nothing found|no results|no posts (?:found|matched)|nothing matched)\b",
                page_text,
            )
            challenge = re.search(
                r"\b(?:cloudflare|captcha|checking your browser|just a moment|"
                r"attention required|access denied)\b",
                page_text,
            )
            if challenge or not explicit_empty:
                raise AdapterError(
                    "malformed_upstream",
                    "AudioBookBay returned an invalid search response",
                    502,
                )
        return candidates

    @staticmethod
    def _detail_title(soup: BeautifulSoup) -> str:
        element = soup.select_one(
            ".postTitle h1, .postTitle h2, h1.entry-title, article h1, meta[property='og:title'], title"
        )
        if element is None:
            return ""
        raw = element.get("content", "") if element.name == "meta" else element.get_text(" ", strip=True)
        value = normalize_display(raw, 240)
        value = re.sub(r"(?i)\s*[-|]\s*AudioBook\s*Bay.*$", "", value).strip()
        return value

    @classmethod
    def _canonical_detail_identity(
        cls, value: str, expected_format: str | None
    ) -> str:
        """Remove only ABB's known trailing ``Audiobook <format>`` decoration."""

        identity = normalize_identity(value)
        decorated = re.fullmatch(
            r"(.+?) audiobook(?: (mp3|m4b|m4a|aac|flac|ogg|opus|wav))?",
            identity,
        )
        if decorated is None:
            return identity
        detail_format = decorated.group(2)
        if detail_format is None:
            return decorated.group(1)
        expected = cls._DETAIL_AUDIO_FORMAT.search(expected_format or "")
        if expected is not None and detail_format == expected.group(1).casefold():
            return decorated.group(1)
        return identity

    def resolve(self, candidate: Candidate) -> ResolvedResult:
        # Candidate.path came from a validated search and is revalidated here so
        # persisted/tampered SQLite state cannot turn this into an arbitrary URL.
        path = self._candidate_path(candidate.path)
        response = self._get(path, missing_is_changed=True)
        soup = BeautifulSoup(response.text, "html.parser")
        detail_title = self._detail_title(soup)
        if not detail_title:
            raise AdapterError(
                "result_changed", "The selected AudioBookBay result is malformed", 409
            )
        expected = self._canonical_detail_identity(candidate.title, candidate.format)
        actual = self._canonical_detail_identity(detail_title, candidate.format)
        if not expected or not actual or expected != actual:
            raise AdapterError(
                "result_changed", "The selected AudioBookBay result changed", 409
            )

        info_label = soup.find("td", string=re.compile(r"Info Hash", re.IGNORECASE))
        info_cell = info_label.find_next_sibling("td") if info_label is not None else None
        info_hash = normalize_display(info_cell.get_text(" ", strip=True), 64).lower() if info_cell else ""
        if not INFO_HASH_RE.fullmatch(info_hash):
            raise AdapterError(
                "magnet_failure", "AudioBookBay did not provide a valid info hash", 502
            )

        # Tracker rows are upstream-controlled URLs.  Fixed, non-secret tracker
        # defaults retain ABBA's magnet behavior without accepting arbitrary
        # hosts, credentials, malformed ports, or private-network literals.
        trackers = DEFAULT_TRACKERS
        magnet = f"magnet:?xt=urn:btih:{info_hash}" + "".join(
            f"&tr={quote(tracker, safe='')}" for tracker in trackers
        )
        return ResolvedResult(
            title=candidate.title,
            info_hash=info_hash,
            magnet=magnet,
        )


class QbitClient:
    """Minimal qBittorrent Web API client with mutation-aware failures."""

    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Referer": f"{settings.qbittorrent_url}/"}
        )
        self._authenticated = False
        self._lock = threading.RLock()

    def _login(self) -> None:
        try:
            response = self.session.post(
                f"{self.settings.qbittorrent_url}/api/v2/auth/login",
                data={
                    "username": self.settings.qbittorrent_username,
                    "password": self.settings.qbittorrent_password,
                },
                timeout=self.settings.qbittorrent_timeout_seconds,
                verify=self.settings.verify_tls,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise AdapterError(
                "qbit_unreachable",
                "qBittorrent is unreachable",
                503,
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise AdapterError(
                "qbit_unreachable",
                "qBittorrent authentication failed",
                503,
                retryable=True,
            ) from exc
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise AdapterError(
                "qbit_unreachable",
                "qBittorrent authentication failed",
                503,
                retryable=True,
            )
        self._authenticated = True

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        uncertain_on_transport: bool = False,
        retry_auth: bool = True,
    ) -> requests.Response:
        with self._lock:
            if not self._authenticated:
                self._login()
            try:
                response = self.session.request(
                    method,
                    f"{self.settings.qbittorrent_url}{endpoint}",
                    params=dict(params or {}),
                    data=dict(data or {}),
                    timeout=self.settings.qbittorrent_timeout_seconds,
                    verify=self.settings.verify_tls,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                code = "submission_uncertain" if uncertain_on_transport else "qbit_unreachable"
                message = (
                    "qBittorrent submission outcome is uncertain"
                    if uncertain_on_transport
                    else "qBittorrent is unreachable"
                )
                raise AdapterError(code, message, 503, retryable=True) from exc
            except requests.RequestException as exc:
                code = "submission_uncertain" if uncertain_on_transport else "qbit_unreachable"
                message = (
                    "qBittorrent submission outcome is uncertain"
                    if uncertain_on_transport
                    else "qBittorrent request failed"
                )
                raise AdapterError(code, message, 503, retryable=True) from exc

            if response.status_code == 403 and retry_auth and not uncertain_on_transport:
                self._authenticated = False
                self._login()
                return self._request(
                    method,
                    endpoint,
                    params=params,
                    data=data,
                    uncertain_on_transport=False,
                    retry_auth=False,
                )
            if response.status_code == 429 or response.status_code >= 500:
                code = "submission_uncertain" if uncertain_on_transport else "qbit_unreachable"
                message = (
                    "qBittorrent submission outcome is uncertain"
                    if uncertain_on_transport
                    else "qBittorrent is temporarily unavailable"
                )
                raise AdapterError(code, message, 503, retryable=True)
            if response.status_code != 200:
                raise AdapterError(
                    "qbit_rejected", "qBittorrent rejected the request", 502
                )
            return response

    def categories(self) -> dict[str, dict[str, Any]]:
        response = self._request("GET", "/api/v2/torrents/categories")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError(
                "qbit_rejected", "qBittorrent returned invalid category data", 502
            ) from exc
        if not isinstance(payload, dict):
            raise AdapterError(
                "qbit_rejected", "qBittorrent returned invalid category data", 502
            )
        return {
            str(name): details
            for name, details in payload.items()
            if isinstance(details, dict)
        }

    def validate_destination(self) -> None:
        details = self.categories().get(EXPECTED_CATEGORY)
        if details is None or normalize_save_path(str(details.get("savePath") or "")) != EXPECTED_SAVE_PATH:
            raise AdapterError(
                "qbit_destination_mismatch",
                "qBittorrent audiobook category or save path is incorrect",
                409,
            )

    def torrent(self, info_hash: str) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": info_hash},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError(
                "qbit_rejected", "qBittorrent returned invalid torrent data", 502
            ) from exc
        if not isinstance(payload, list):
            raise AdapterError(
                "qbit_rejected", "qBittorrent returned invalid torrent data", 502
            )
        matches = [
            item
            for item in payload
            if isinstance(item, dict)
            and str(item.get("hash", "")).lower() == info_hash
        ]
        if len(matches) > 1:
            raise AdapterError(
                "qbit_rejected", "qBittorrent returned duplicate hash data", 502
            )
        return matches[0] if matches else None

    def add_torrent(self, magnet: str, tag: str) -> None:
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data={
                "urls": magnet,
                "savepath": EXPECTED_SAVE_PATH,
                "category": EXPECTED_CATEGORY,
                "tags": tag,
                "autoTMM": "false",
            },
            uncertain_on_transport=True,
        )
        if response.text.strip() not in {"", "Ok."}:
            raise AdapterError(
                "qbit_rejected", "qBittorrent rejected the audiobook", 502
            )

    def add_tag(self, info_hash: str, tag: str) -> None:
        response = self._request(
            "POST",
            "/api/v2/torrents/addTags",
            data={"hashes": info_hash, "tags": tag},
        )
        if response.text.strip() not in {"", "Ok."}:
            raise AdapterError(
                "qbit_rejected", "qBittorrent rejected the request tag", 502
            )

    @staticmethod
    def tags(torrent: Mapping[str, Any]) -> set[str]:
        value = torrent.get("tags", "")
        if not isinstance(value, str):
            return set()
        return {tag.strip() for tag in value.split(",") if tag.strip()}

    def validate_torrent(
        self, torrent: Mapping[str, Any], tag: str, *, require_tag: bool
    ) -> None:
        category = str(torrent.get("category") or "")
        save_path = normalize_save_path(str(torrent.get("save_path") or ""))
        if category != EXPECTED_CATEGORY or save_path != EXPECTED_SAVE_PATH:
            raise AdapterError(
                "qbit_destination_mismatch",
                "Existing torrent has an unsafe category or save path",
                409,
            )
        if require_tag and tag not in self.tags(torrent):
            raise AdapterError(
                "qbit_rejected", "qBittorrent did not retain the request tag", 502
            )

    def validate_status_torrent(self, torrent: Mapping[str, Any], tag: str) -> bool:
        """Validate routing for status; return true during BookBot import.

        New/adopted submissions remain restricted to ``audiobooks``.  BookBot
        deliberately moves a completed torrent to ``audiobooks-imported`` at
        the same safe path, so read-only status accepts that one downstream
        transition while still requiring the exact Huey tag.
        """

        category = str(torrent.get("category") or "")
        save_path = normalize_save_path(str(torrent.get("save_path") or ""))
        if (
            category not in {EXPECTED_CATEGORY, IMPORTED_CATEGORY}
            or save_path != EXPECTED_SAVE_PATH
        ):
            raise AdapterError(
                "qbit_destination_mismatch",
                "Existing torrent has an unsafe category or save path",
                409,
            )
        if tag not in self.tags(torrent):
            raise AdapterError(
                "qbit_rejected", "qBittorrent request tag is missing", 502
            )
        return category == IMPORTED_CATEGORY

    def public_status(self, torrent: Mapping[str, Any]) -> dict[str, Any]:
        raw_progress = torrent.get("progress", 0.0)
        if isinstance(raw_progress, bool) or not isinstance(raw_progress, (int, float)):
            raise AdapterError(
                "qbit_rejected", "qBittorrent returned invalid progress data", 502
            )
        progress = float(raw_progress)
        if not math.isfinite(progress):
            raise AdapterError(
                "qbit_rejected", "qBittorrent returned invalid progress data", 502
            )
        progress = max(0.0, min(progress, 1.0))
        state = normalize_display(str(torrent.get("state") or "unknown"), 40)
        failed_states = {"error", "missingFiles"}
        complete_states = {
            "uploading", "stalledUP", "pausedUP", "forcedUP", "queuedUP", "stoppedUP"
        }
        if state in failed_states:
            status = "failed"
        elif progress >= 1.0 or state in complete_states:
            status = "downloaded"
        else:
            status = "downloading"
        return {
            "status": status,
            "qbit_state": state,
            "progress": progress,
            "title": normalize_display(str(torrent.get("name") or ""), 200),
            **({"error": "qBittorrent reported a terminal torrent failure"} if status == "failed" else {}),
        }


class AbbaService:
    """Coordinates search, durable prepare, and exact-hash qBit recovery."""

    TERMINAL_RESOLUTION_ERRORS = frozenset({"result_changed", "magnet_failure"})
    TERMINAL_QBIT_ERRORS = frozenset(
        {"qbit_rejected", "qbit_destination_mismatch"}
    )

    def __init__(
        self,
        settings: Settings,
        journal: Journal,
        abb: ABBClient,
        qbit: QbitClient,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.journal = journal
        self.abb = abb
        self.qbit = qbit
        self.sleeper = sleeper
        # The production image intentionally runs one process.  Serializing
        # grabs also keeps prepare/check/add/reconcile atomic within that
        # process; SQLite remains the durable authority across restarts.
        self._grab_lock = threading.RLock()

    @staticmethod
    def _cache_key(title: str, author: str | None, limit: int) -> str:
        value = json.dumps(
            {
                "version": SEARCH_CONTRACT_VERSION,
                "title": normalize_identity(title),
                "author": normalize_identity(author or ""),
                "limit": limit,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def search(
        self, title: str, author: str | None, limit: int
    ) -> dict[str, Any]:
        cache_key = self._cache_key(title, author, limit)
        cached = self.journal.cached_search(cache_key)
        if cached is not None:
            return {"results": cached, "cached": True}
        retry_after = self.journal.claim_search_slot(
            self.settings.search_min_interval_seconds
        )
        if retry_after > 0:
            raise AdapterError(
                "search_rate_limited",
                "AudioBookBay searches are temporarily rate limited",
                429,
                retryable=True,
            )
        candidates = self.abb.search(title, author, limit)
        # ABBClient deduplicates already; this defensive pass ensures the API
        # can never violate Huey's opaque identity contract.
        unique: list[Candidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.candidate_id not in seen:
                unique.append(candidate)
                seen.add(candidate.candidate_id)
        self.journal.store_search(
            cache_key,
            unique,
            self.settings.search_cache_seconds,
            self.settings.result_ttl_seconds,
        )
        return {"results": [item.public_dict() for item in unique], "cached": False}

    @staticmethod
    def _tag(correlation_id: str) -> str:
        request_id = correlation_request_id(correlation_id)
        if request_id is None:  # Routes validate this; retain a fail-closed invariant.
            raise AdapterError("invalid_request", "Invalid correlation_id", 400)
        return f"huey-{request_id}"

    @staticmethod
    def _stored_error(row: Mapping[str, Any], fallback: str) -> str:
        code = normalize_display(str(row.get("error_code") or fallback), 80)
        return code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "acquisition_failed"

    def _job(
        self,
        row: Mapping[str, Any],
        status: str,
        *,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        info_hash_raw = str(row.get("info_hash") or "").lower()
        info_hash = info_hash_raw if INFO_HASH_RE.fullmatch(info_hash_raw) else None
        if status != "failed" and info_hash is None:
            raise AdapterError(
                "journal_error", "Acquisition journal contains invalid state", 500
            )
        title = normalize_display(row.get("title"), 160) or "Unavailable audiobook"
        job: dict[str, Any] = {
            "correlation_id": str(row["correlation_id"]),
            "candidate_id": str(row["candidate_id"]),
            "status": status,
            "info_hash": info_hash,
            "title": title,
            "category": EXPECTED_CATEGORY,
            "save_path": EXPECTED_SAVE_PATH,
            "tags": [str(row["tag"])],
        }
        if status == "failed":
            job["error"] = error_code or self._stored_error(row, "acquisition_failed")
        return job

    def _alias_job(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Expose a hash collision as a canonical correlation, never a retry."""

        canonical = self.journal.canonical_acquisition(str(row["correlation_id"]))
        if canonical is None:
            raise AdapterError(
                "journal_error", "Canonical acquisition correlation is missing", 500
            )
        canonical_correlation = str(canonical["correlation_id"])
        canonical_request_id = correlation_request_id(canonical_correlation)
        canonical_candidate = str(canonical["candidate_id"])
        canonical_tag = str(canonical["tag"])
        info_hash = str(row.get("info_hash") or "").casefold()
        if (
            canonical_request_id is None
            or not CANDIDATE_ID_RE.fullmatch(canonical_candidate)
            or not INFO_HASH_RE.fullmatch(info_hash)
            or canonical_tag != f"huey-{canonical_request_id}"
        ):
            raise AdapterError(
                "journal_error", "Canonical acquisition correlation is invalid", 500
            )
        return {
            "correlation_id": str(row["correlation_id"]),
            "candidate_id": str(row["candidate_id"]),
            "status": "duplicate",
            "info_hash": info_hash,
            "title": normalize_display(row.get("title"), 160)
            or normalize_display(canonical.get("title"), 160)
            or "Unavailable audiobook",
            "category": EXPECTED_CATEGORY,
            "save_path": EXPECTED_SAVE_PATH,
            "tags": [canonical_tag],
            "canonical_correlation_id": canonical_correlation,
            "canonical_candidate_id": canonical_candidate,
        }

    def _terminal_job(
        self, row: Mapping[str, Any], error: AdapterError
    ) -> dict[str, Any]:
        updated = self.journal.terminal_failure(
            str(row["correlation_id"]),
            str(row["candidate_id"]),
            str(row.get("title") or "") or None,
            str(row["tag"]),
            error,
            str(row.get("info_hash") or "") or None,
        )
        return self._job(updated, "failed", error_code=error.code)

    def _prehash_failure(
        self,
        correlation_id: str,
        candidate_id: str,
        tag: str,
        title: str | None,
        error: AdapterError,
    ) -> dict[str, Any]:
        row = self.journal.terminal_failure(
            correlation_id, candidate_id, title, tag, error, None
        )
        return self._job(row, "failed", error_code=error.code)

    def _qbit_status_job(
        self,
        row: Mapping[str, Any],
        torrent: Mapping[str, Any],
        *,
        initial: bool,
    ) -> dict[str, Any]:
        # Reaching this point proves qBittorrent has the exact journal hash and
        # correlation tag.  Retain ownership even if its current state is
        # terminal; otherwise a later request could reuse the hash while the
        # first tagged torrent still exists.
        row = self.journal.update(
            str(row["correlation_id"]),
            str(row["state"]),
            mutation_started=True,
        )
        public = self.qbit.public_status(torrent)
        if public["status"] == "failed":
            error = AdapterError(
                "qbit_rejected", "qBittorrent reported a terminal torrent failure", 502
            )
            return self._terminal_job(row, error)
        updated = self.journal.update(str(row["correlation_id"]), "queued")
        status = str(public["status"])
        if initial and status == "downloading":
            status = "queued"
        return self._job(updated, status)

    def _existing_torrent(
        self,
        row: Mapping[str, Any],
        torrent: Mapping[str, Any],
        *,
        add_missing_tag: bool,
        initial: bool,
        status_only: bool = False,
    ) -> dict[str, Any]:
        tag = str(row["tag"])
        if status_only:
            if self.qbit.validate_status_torrent(torrent, tag):
                updated = self.journal.update(
                    str(row["correlation_id"]),
                    "queued",
                    mutation_started=True,
                )
                return self._job(updated, "processing")
            return self._qbit_status_job(row, torrent, initial=False)
        self.qbit.validate_torrent(torrent, tag, require_tag=False)
        if tag not in self.qbit.tags(torrent):
            if not add_missing_tag:
                error = AdapterError(
                    "qbit_rejected", "qBittorrent request tag is missing", 502
                )
                return self._terminal_job(row, error)
            mutating = self.journal.update(
                str(row["correlation_id"]),
                "submitting",
                mutation_started=True,
            )
            try:
                self.qbit.add_tag(str(row["info_hash"]), tag)
            except AdapterError as error:
                if error.code in self.TERMINAL_QBIT_ERRORS:
                    return self._terminal_job(mutating, error)
                uncertain = AdapterError(
                    "submission_uncertain",
                    "qBittorrent tag mutation could not be safely verified",
                    503,
                    retryable=True,
                )
                self.journal.update(
                    str(row["correlation_id"]),
                    "submission_uncertain",
                    uncertain,
                )
                raise uncertain from error
            refreshed = self.qbit.torrent(str(row["info_hash"]))
            if refreshed is None:
                raise AdapterError(
                    "qbit_unreachable",
                    "qBittorrent did not confirm the request tag",
                    503,
                    retryable=True,
                )
            torrent = refreshed
            row = mutating
        self.qbit.validate_torrent(torrent, tag, require_tag=True)
        return self._qbit_status_job(row, torrent, initial=initial)

    def _adopt_or_recover_torrent(
        self,
        row: Mapping[str, Any],
        torrent: Mapping[str, Any],
        *,
        initial: bool,
    ) -> dict[str, Any]:
        if str(torrent.get("category") or "") == IMPORTED_CATEGORY:
            return self._existing_torrent(
                row,
                torrent,
                add_missing_tag=False,
                initial=False,
                status_only=True,
            )
        return self._existing_torrent(
            row, torrent, add_missing_tag=True, initial=initial
        )

    def _resume_committed(
        self, row: Mapping[str, Any], *, initial: bool
    ) -> dict[str, Any]:
        if (
            row.get("canonical_candidate_correlation_id") is not None
            and row.get("state") == "failed"
            and row.get("error_code") == "result_changed"
        ):
            return self._job(row, "failed")
        if row.get("canonical_correlation_id") is not None:
            return self._alias_job(row)
        state = str(row["state"])
        if state == "failed":
            return self._job(row, "failed")
        info_hash = str(row.get("info_hash") or "").lower()
        if not INFO_HASH_RE.fullmatch(info_hash):
            return self._terminal_job(
                row,
                AdapterError(
                    "submission_uncertain", "Acquisition state could not be verified", 503
                ),
            )
        try:
            self.qbit.validate_destination()
            torrent = self.qbit.torrent(info_hash)
            if torrent is not None:
                return self._adopt_or_recover_torrent(
                    row, torrent, initial=initial
                )
        except AdapterError as error:
            if error.code in self.TERMINAL_QBIT_ERRORS:
                return self._terminal_job(row, error)
            if error.code == "qbit_unreachable":
                if state == "prepared":
                    self.journal.update(
                        str(row["correlation_id"]), "prepared", error
                    )
                    raise
                raise AdapterError(
                    "submission_uncertain",
                    "qBittorrent acquisition state could not be confirmed",
                    503,
                    retryable=True,
                ) from error
            raise
        if state in {"submitting", "submission_uncertain", "queued"}:
            raise AdapterError(
                "submission_uncertain",
                "qBittorrent submission outcome is still uncertain",
                503,
                retryable=True,
            )
        # A prepared row is safe to resolve again, provided the exact committed
        # info hash remains unchanged.  It is never re-added once submission may
        # have crossed the qBittorrent boundary.
        return self._submit_prepared(row)

    def _submit_prepared(self, row: Mapping[str, Any]) -> dict[str, Any]:
        candidate = self.journal.candidate(str(row["candidate_id"]))
        if candidate is None:
            return self._terminal_job(
                row,
                AdapterError(
                    "result_changed",
                    "The selected AudioBookBay result is no longer available",
                    409,
                ),
            )
        try:
            resolved = self.abb.resolve(candidate)
        except AdapterError as error:
            if error.code in self.TERMINAL_RESOLUTION_ERRORS:
                return self._terminal_job(row, error)
            raise
        if resolved.info_hash != str(row["info_hash"]):
            return self._terminal_job(
                row,
                AdapterError(
                    "result_changed", "The selected AudioBookBay result changed", 409
                ),
            )

        try:
            self.qbit.validate_destination()
            existing = self.qbit.torrent(resolved.info_hash)
            if existing is not None:
                return self._adopt_or_recover_torrent(row, existing, initial=True)
        except AdapterError as error:
            if error.code in self.TERMINAL_QBIT_ERRORS:
                return self._terminal_job(row, error)
            if error.code == "qbit_unreachable":
                self.journal.update(
                    str(row["correlation_id"]), "prepared", error
                )
                raise
            raise

        submitting = self.journal.update(
            str(row["correlation_id"]),
            "submitting",
            mutation_started=True,
        )
        try:
            self.qbit.add_torrent(resolved.magnet, str(row["tag"]))
        except AdapterError as error:
            if error.code == "submission_uncertain":
                self.journal.update(
                    str(row["correlation_id"]), "submission_uncertain", error
                )
                raise
            if error.code == "qbit_unreachable":
                self.journal.update(
                    str(row["correlation_id"]), "prepared", error
                )
                raise
            return self._terminal_job(submitting, error)

        torrent: Mapping[str, Any] | None = None
        try:
            for attempt in range(3):
                torrent = self.qbit.torrent(resolved.info_hash)
                if torrent is not None:
                    break
                if attempt < 2:
                    self.sleeper(0.2)
        except AdapterError:
            torrent = None
        if torrent is None:
            error = AdapterError(
                "submission_uncertain",
                "qBittorrent submission could not be confirmed",
                503,
                retryable=True,
            )
            self.journal.update(
                str(row["correlation_id"]), "submission_uncertain", error
            )
            raise error
        try:
            return self._existing_torrent(
                submitting, torrent, add_missing_tag=False, initial=True
            )
        except AdapterError as error:
            if error.code in self.TERMINAL_QBIT_ERRORS:
                return self._terminal_job(submitting, error)
            uncertain_error = AdapterError(
                "submission_uncertain",
                "qBittorrent submission could not be safely verified",
                503,
                retryable=True,
            )
            self.journal.update(
                str(row["correlation_id"]),
                "submission_uncertain",
                uncertain_error,
            )
            raise uncertain_error

    def grab(self, candidate_id: str, correlation_id: str) -> dict[str, Any]:
        tag = self._tag(correlation_id)
        with self._grab_lock:
            row = self.journal.acquisition(correlation_id)
            if row is not None:
                if row["candidate_id"] != candidate_id:
                    raise AdapterError(
                        "request_conflict",
                        "Correlation ID is already bound to another candidate",
                        409,
                    )
                return self._resume_committed(row, initial=True)

            candidate = self.journal.candidate(candidate_id)
            if candidate is None:
                error = AdapterError(
                    "result_changed",
                    "The selected AudioBookBay result is no longer available",
                    409,
                )
                return self._prehash_failure(
                    correlation_id,
                    candidate_id,
                    tag,
                    "Unavailable audiobook",
                    error,
                )
            try:
                resolved = self.abb.resolve(candidate)
            except AdapterError as error:
                if error.code in self.TERMINAL_RESOLUTION_ERRORS:
                    return self._prehash_failure(
                        correlation_id,
                        candidate_id,
                        tag,
                        candidate.title,
                        error,
                    )
                raise
            try:
                row = self.journal.prepare(
                    correlation_id,
                    candidate_id,
                    resolved.info_hash,
                    resolved.title,
                    tag,
                )
            except AdapterError as error:
                if error.code != "result_changed":
                    raise
                existing = self.journal.acquisition(correlation_id)
                if existing is None:
                    raise
                return self._terminal_job(existing, error)
            # The first resolve already supplied a bounded magnet.  Reuse it
            # without a second ABB request while preserving the prepared hash.
            if row["state"] != "prepared":
                return self._resume_committed(row, initial=True)
            if row.get("canonical_correlation_id") is not None:
                return self._alias_job(row)
            return self._submit_resolved(row, resolved)

    def _submit_resolved(
        self, row: Mapping[str, Any], resolved: ResolvedResult
    ) -> dict[str, Any]:
        if resolved.info_hash != str(row["info_hash"]):
            return self._terminal_job(
                row,
                AdapterError(
                    "result_changed", "The selected AudioBookBay result changed", 409
                ),
            )
        try:
            self.qbit.validate_destination()
            existing = self.qbit.torrent(resolved.info_hash)
            if existing is not None:
                return self._adopt_or_recover_torrent(row, existing, initial=True)
        except AdapterError as error:
            if error.code in self.TERMINAL_QBIT_ERRORS:
                return self._terminal_job(row, error)
            if error.code == "qbit_unreachable":
                self.journal.update(
                    str(row["correlation_id"]), "prepared", error
                )
                raise
            raise

        submitting = self.journal.update(
            str(row["correlation_id"]),
            "submitting",
            mutation_started=True,
        )
        try:
            self.qbit.add_torrent(resolved.magnet, str(row["tag"]))
        except AdapterError as error:
            if error.code == "submission_uncertain":
                self.journal.update(
                    str(row["correlation_id"]), "submission_uncertain", error
                )
                raise
            if error.code == "qbit_unreachable":
                self.journal.update(
                    str(row["correlation_id"]), "prepared", error
                )
                raise
            return self._terminal_job(submitting, error)
        try:
            torrent: Mapping[str, Any] | None = None
            for attempt in range(3):
                torrent = self.qbit.torrent(resolved.info_hash)
                if torrent is not None:
                    break
                if attempt < 2:
                    self.sleeper(0.2)
            if torrent is None:
                raise AdapterError(
                    "submission_uncertain",
                    "qBittorrent submission could not be confirmed",
                    503,
                    retryable=True,
                )
            return self._existing_torrent(
                submitting, torrent, add_missing_tag=False, initial=True
            )
        except AdapterError as error:
            if error.code in self.TERMINAL_QBIT_ERRORS:
                return self._terminal_job(submitting, error)
            uncertain_error = AdapterError(
                "submission_uncertain",
                "qBittorrent submission could not be safely verified",
                503,
                retryable=True,
            )
            self.journal.update(
                str(row["correlation_id"]),
                "submission_uncertain",
                uncertain_error,
            )
            raise uncertain_error

    def status_for_correlation(self, correlation_id: str) -> dict[str, Any]:
        with self._grab_lock:
            row = self.journal.acquisition(correlation_id)
            if row is None:
                return {"found": False, "correlation_id": correlation_id}
            if (
                row.get("canonical_candidate_correlation_id") is not None
                and row.get("state") == "failed"
                and row.get("error_code") == "result_changed"
            ):
                return {"found": True, "job": self._job(row, "failed")}
            if row.get("canonical_correlation_id") is not None:
                return {"found": True, "job": self._alias_job(row)}
            if row["state"] == "failed":
                return {"found": True, "job": self._job(row, "failed")}
            info_hash = str(row.get("info_hash") or "")
            if not INFO_HASH_RE.fullmatch(info_hash):
                raise AdapterError(
                    "submission_uncertain",
                    "Acquisition state could not be verified",
                    503,
                    retryable=True,
                )
            try:
                torrent = self.qbit.torrent(info_hash)
            except AdapterError as error:
                if error.code == "qbit_unreachable" and row["state"] != "prepared":
                    raise AdapterError(
                        "submission_uncertain",
                        "qBittorrent acquisition state could not be confirmed",
                        503,
                        retryable=True,
                    ) from error
                raise
            if torrent is None:
                if row["state"] == "prepared":
                    return {"found": False, "correlation_id": correlation_id}
                raise AdapterError(
                    "submission_uncertain",
                    "qBittorrent acquisition state could not be confirmed",
                    503,
                    retryable=True,
                )
            try:
                job = self._existing_torrent(
                    row,
                    torrent,
                    add_missing_tag=False,
                    initial=False,
                    status_only=True,
                )
            except AdapterError as error:
                if error.code in self.TERMINAL_QBIT_ERRORS:
                    job = self._terminal_job(row, error)
                else:
                    raise
            return {"found": True, "job": job}

    def status_for_hash(self, info_hash: str) -> dict[str, Any]:
        row = self.journal.acquisition_for_hash(info_hash)
        if row is None:
            return {"found": False}
        return self.status_for_correlation(str(row["correlation_id"]))

    def health(self) -> tuple[dict[str, Any], int]:
        checks = {
            "database": "unknown",
            "qbittorrent": "unknown",
            "category": "unknown",
            "save_path": "unknown",
        }
        try:
            self.journal.ping()
            checks["database"] = "ok"
        except AdapterError as error:
            checks["database"] = "error"
            return {
                "status": "error",
                "service": SERVICE_NAME,
                "checks": checks,
                "error": error.payload(),
            }, 503
        try:
            categories = self.qbit.categories()
            checks["qbittorrent"] = "ok"
        except AdapterError as error:
            checks["qbittorrent"] = "error"
            return {
                "status": "error",
                "service": SERVICE_NAME,
                "checks": checks,
                "error": error.payload(),
            }, 503
        details = categories.get(EXPECTED_CATEGORY)
        if details is None:
            checks["category"] = "error"
            error = AdapterError(
                "qbit_destination_mismatch",
                "qBittorrent audiobook category is missing",
                503,
            )
        else:
            checks["category"] = "ok"
            if normalize_save_path(str(details.get("savePath") or "")) == EXPECTED_SAVE_PATH:
                checks["save_path"] = "ok"
                return {
                    "status": "ok", "service": SERVICE_NAME, "checks": checks
                }, 200
            checks["save_path"] = "error"
            error = AdapterError(
                "qbit_destination_mismatch",
                "qBittorrent audiobook save path is incorrect",
                503,
            )
        return {
            "status": "error",
            "service": SERVICE_NAME,
            "checks": checks,
            "error": error.payload(),
        }, 503


def _json_body(required: set[str], optional: set[str]) -> dict[str, Any]:
    if not request.is_json:
        raise AdapterError(
            "invalid_request", "Content-Type must be application/json", 415
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise AdapterError("invalid_request", "Request body must be a JSON object", 400)
    keys = set(payload)
    if not required <= keys or keys - required - optional:
        raise AdapterError("invalid_request", "Request body has invalid fields", 400)
    return payload


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AdapterError(
            "invalid_request", f"{name} must be a non-empty bounded string", 400
        )
    normalized = normalize_display(value, maximum)
    if not normalized:
        raise AdapterError("invalid_request", f"{name} is invalid", 400)
    return normalized


def create_app(
    settings: Settings,
    *,
    journal: Journal | None = None,
    abb: ABBClient | None = None,
    qbit: QbitClient | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> Flask:
    journal_instance = journal or Journal(settings.database_path)
    abb_instance = abb or ABBClient(settings)
    qbit_instance = qbit or QbitClient(settings)
    service = AbbaService(
        settings,
        journal_instance,
        abb_instance,
        qbit_instance,
        sleeper=sleeper,
    )
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=4096, JSON_SORT_KEYS=False)
    app.extensions["abba_service"] = service

    @app.after_request
    def secure_response(response: Any) -> Any:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(JobError)
    def handle_job_error(error: JobError) -> tuple[Any, int]:
        return jsonify({"job": error.job}), 200

    @app.errorhandler(AdapterError)
    def handle_adapter_error(error: AdapterError) -> tuple[Any, int]:
        return jsonify({"error": error.payload()}), error.http_status

    @app.errorhandler(404)
    def handle_not_found(_error: Any) -> tuple[Any, int]:
        error = AdapterError("not_found", "Route not found", 404)
        return jsonify({"error": error.payload()}), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(_error: Any) -> tuple[Any, int]:
        error = AdapterError("method_not_allowed", "Method not allowed", 405)
        return jsonify({"error": error.payload()}), 405

    @app.errorhandler(413)
    def handle_too_large(_error: Any) -> tuple[Any, int]:
        error = AdapterError("invalid_request", "Request body is too large", 413)
        return jsonify({"error": error.payload()}), 413

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception) -> tuple[Any, int]:
        LOGGER.error("Unhandled ABBA adapter failure: %s", type(error).__name__)
        safe = AdapterError("internal_error", "Internal adapter failure", 500)
        return jsonify({"error": safe.payload()}), 500

    @app.post("/api/search")
    def api_search() -> Any:
        payload = _json_body({"title"}, {"author", "limit"})
        title = _bounded_text(payload["title"], "title", 200)
        author = None
        if "author" in payload:
            author = _bounded_text(payload["author"], "author", 160)
        limit = payload.get("limit", settings.max_results)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= settings.max_results
        ):
            raise AdapterError("invalid_request", "limit is out of range", 400)
        return jsonify(service.search(title, author, limit))

    @app.post("/api/grab")
    def api_grab() -> Any:
        payload = _json_body({"candidate_id", "correlation_id"}, set())
        candidate_id = payload["candidate_id"]
        correlation_id = payload["correlation_id"]
        if not isinstance(candidate_id, str) or not CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise AdapterError("invalid_request", "Invalid candidate_id", 400)
        if correlation_request_id(correlation_id) is None:
            raise AdapterError("invalid_request", "Invalid correlation_id", 400)
        return jsonify({"job": service.grab(candidate_id, correlation_id)})

    @app.get("/api/status")
    def api_status_correlation() -> Any:
        if set(request.args) != {"correlation_id"} or len(
            request.args.getlist("correlation_id")
        ) != 1:
            raise AdapterError("invalid_request", "Invalid status query", 400)
        correlation_id = request.args["correlation_id"]
        if correlation_request_id(correlation_id) is None:
            raise AdapterError("invalid_request", "Invalid correlation_id", 400)
        return jsonify(service.status_for_correlation(correlation_id))

    @app.get("/api/status/<info_hash>")
    def api_status_hash(info_hash: str) -> Any:
        normalized = info_hash.lower()
        if info_hash != normalized or not INFO_HASH_RE.fullmatch(normalized):
            raise AdapterError("invalid_request", "Invalid info hash", 400)
        return jsonify(service.status_for_hash(normalized))

    @app.get("/health")
    def health() -> tuple[Any, int]:
        payload, status = service.health()
        return jsonify(payload), status

    return app


def create_app_from_env(env: Mapping[str, str] | None = None) -> Flask:
    return create_app(Settings.from_env(env))


if __name__ == "__main__":  # pragma: no cover - local diagnostic only
    configured = Settings.from_env()
    create_app(configured).run(host="0.0.0.0", port=configured.port)
