#!/usr/bin/env python3
"""Non-acquiring production validation for the WyseARR stack."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[1]
CORE_SERVICES = (
    "qbittorrent",
    "prowlarr",
    "sonarr",
    "radarr",
    "lidarr",
    "bazarr",
    "whisparr",
    "bookbot",
    "huey",
)
EVALUATION_SERVICES = ("sabnzbd", "shelfarr")
ABBA_SERVICE = "abba"
LAZYLIBRARIAN_SERVICE = "lazylibrarian"
DIRECT_CATEGORIES = ("ebooks", "audiobooks", "manga-comics", "roms", "sheet-music")
ARR_CATEGORIES = ("tv", "movies", "music", "spicy")
SHELFARR_DOWNLOAD_CATEGORY = "shelfarr"
ABBA_FEATURE_FLAG = "ABBA_ENABLED"
LAZYLIBRARIAN_FEATURE_FLAG = "LAZYLIBRARIAN_ENABLED"
EBOOK_BACKENDS_SETTING = "EBOOK_ACQUISITION_BACKENDS"
EBOOK_OWNER_SETTING = "EBOOK_ACQUISITION_OWNER"
USENET_FEATURE_FLAG = "WYSEARR_USENET_ENABLED"
SHELFARR_FEATURE_FLAG = "SHELFARR_ENABLED"
STRICT_FEATURE_FLAGS = (
    SHELFARR_FEATURE_FLAG,
    ABBA_FEATURE_FLAG,
    LAZYLIBRARIAN_FEATURE_FLAG,
    USENET_FEATURE_FLAG,
)
STRICT_ENV_ASSIGNMENTS = STRICT_FEATURE_FLAGS + (
    EBOOK_BACKENDS_SETTING,
    EBOOK_OWNER_SETTING,
)
SUPPORTED_EBOOK_BACKENDS = ("lazylibrarian", "shelfarr")
PRODUCTION_EBOOK_BACKENDS = SUPPORTED_EBOOK_BACKENDS
MANAGED_SABNZBD_SERVER = "WyseARR Primary"
MANAGED_NEWZNAB_INDEXER_DEFAULT = "WyseARR Books"
MANAGED_PROWLARR_TAG = "shelfarr"
MANAGED_LAZYLIBRARIAN_APPLICATION = "LazyLibrarian"
MANAGED_LAZYLIBRARIAN_TAG = "lazylibrarian-ebooks"
LAZYLIBRARIAN_SYNC_CATEGORIES = frozenset({7020})
EXPECTED_LAZYLIBRARIAN_VERSION = "02af0464"
LAZYLIBRARIAN_API_COMMANDS = (
    "findBook",
    "findAuthor",
    "addBook",
    "getAllBooks",
    "queueBook",
    "searchBook",
    "getHistory",
    "getVersion",
    "listNabProviders",
    "listProviders",
    "changeProvider",
    "addProvider",
    "delProvider",
    "readCFG",
)
MAX_PRIVATE_RESPONSE_BYTES = 1024 * 1024
LAZYLIBRARIAN_EFFECTIVE_SETTINGS: dict[str, dict[str, str]] = {
    "GENERAL": {
        "audio_tab": "0",
        "ebook_tab": "1",
        "launch_browser": "0",
        "ebook_type": "epub, mobi, azw3, pdf",
        "imp_preflang": "eng, en-US, en, English, en-GB",
        "imp_autoadd": "",
        "imp_autosearch": "0",
        "destination_copy": "0",
        "ebook_dir": "",
        "audio_dir": "",
        "download_dir": "/downloads",
    },
    "API": {"api_enabled": "1", "book_api": "OpenLibrary"},
    "LOGGING": {
        "loglevel": "20",
        "logredact": "1",
        "hostredact": "1",
        "logfileredact": "1",
        "redact_params": "apikey, api_key, prov_apikey, qbittorrent_pass",
    },
    "GIT": {"auto_update": "0"},
    "TELEMETRY": {
        "telemetry_enable": "0",
        "telemetry_send_config": "0",
        "telemetry_send_usage": "0",
        "telemetry_interval": "0",
    },
    "SEARCHSCAN": {
        "search_bookinterval": "0",
        "scan_interval": "0",
        "search_maginterval": "0",
        "searchrss_interval": "0",
        "wishlist_interval": "0",
        "search_comicinterval": "0",
        "versioncheck_interval": "0",
        "goodreads_interval": "0",
        "hardcover_interval": "0",
        "delaysearch": "0",
    },
    "LIBRARYSCAN": {
        "newbook_status": "Skipped",
        "newaudio_status": "Ignored",
        "newauthor_status": "Skipped",
        "newauthor_audio": "Ignored",
        "newauthor_books": "0",
    },
    "COMICS": {"comic_tab": "0"},
    "MAGAZINES": {"mag_tab": "0"},
    "USENET": {
        "nzb_downloader_sabnzbd": "0",
        "nzb_downloader_nzbget": "0",
        "nzb_downloader_synology": "0",
        "nzb_downloader_blackhole": "0",
    },
    "TORRENT": {
        "tor_downloader_blackhole": "0",
        "tor_downloader_utorrent": "0",
        "tor_downloader_rtorrent": "0",
        "tor_downloader_qbittorrent": "1",
        "tor_downloader_transmission": "0",
        "tor_downloader_synology": "0",
        "tor_downloader_deluge": "0",
        "torrent_paused": "0",
        "keep_seeding": "1",
        "seed_wait": "1",
        "prefer_magnet": "1",
    },
    "QBITTORRENT": {
        "qbittorrent_host": "qbittorrent",
        "qbittorrent_port": "8080",
        "qbittorrent_base": "",
        "qbittorrent_label": "ebooks",
        "qbittorrent_dir": "/downloads/ebooks",
        "qbittorrent_remote": "",
        "qbittorrent_local": "",
        "qbittorrent_ignore_ssl": "0",
    },
    "POSTPROCESS": {
        "del_downloadfailed": "0",
        "del_failed": "0",
        "del_completed": "0",
    },
    "KAT": {"kat": "0"},
    "TPB": {"tpb": "0"},
    "LIME": {"lime": "0"},
    "TDL": {"tdl": "0"},
    "BOK": {"bok": "0"},
}
LAZYLIBRARIAN_CSV_SETTINGS = frozenset(
    {
        ("GENERAL", "ebook_type"),
        ("GENERAL", "imp_preflang"),
        ("LOGGING", "redact_params"),
    }
)
ARR_PROWLARR_TAG = "wysearr-arr"
BAZARR_PROVIDERS = {"embeddedsubtitles", "yifysubtitles", "subf2m"}
REQUIRED_DISCORD_CHANNELS = {
    "requests": frozenset(
        {
            "movies-tv",
            "ebooks",
            "audiobooks",
            "manga-comics",
            "roms",
            "sheet-music",
        }
    ),
    "activity": frozenset(
        {"download-queue", "request-status", "recent-additions"}
    ),
    "system": frozenset({"import-errors", "system-health"}),
}
ARR_NOTIFICATION_DATABASES = {
    "sonarr": Path("config/sonarr/sonarr.db"),
    "radarr": Path("config/radarr/radarr.db"),
    "lidarr": Path("config/lidarr/lidarr.db"),
}
ARR_INDEXER_DATABASES = {
    "sonarr": Path("config/sonarr/sonarr.db"),
    "radarr": Path("config/radarr/radarr.db"),
    "lidarr": Path("config/lidarr/lidarr.db"),
    "whisparr": Path("config/whisparr/whisparr2.db"),
}
DISCORD_WEBHOOK_MARKERS = (
    "discord.com/api/webhooks/",
    "discordapp.com/api/webhooks/",
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        matched_flag = next(
            (
                key
                for key in STRICT_ENV_ASSIGNMENTS
                if re.match(
                    rf"^\s*(?:export\s+)?{re.escape(key)}\s*=",
                    raw_line,
                )
            ),
            None,
        )
        if matched_flag:
            values[matched_flag] = (
                "__WYSEARR_DUPLICATE__"
                if matched_flag in values
                else (
                    raw_line[len(f"{matched_flag}="):]
                    if raw_line.startswith(f"{matched_flag}=")
                    else raw_line
                )
            )
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _value = line.split("=", 1)
        normalized_key = key.strip()
        values[normalized_key] = _value.strip()
    return values


def load_channel_inventory(path: Path) -> dict[str, dict[str, str]]:
    """Parse the deliberately simple two-level Discord channel inventory."""

    inventory: dict[str, dict[str, str]] = {}
    current_section: str | None = None
    assigned_ids: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():
            if not line.endswith(":") or ":" in line[:-1]:
                raise ValueError(f"invalid channel section on line {line_number}")
            current_section = line[:-1].strip()
            if not current_section or current_section in inventory:
                raise ValueError(f"duplicate channel section on line {line_number}")
            inventory[current_section] = {}
            continue
        if current_section is None or ":" not in line:
            raise ValueError(f"invalid channel entry on line {line_number}")
        name, raw_value = line.strip().split(":", 1)
        name = name.strip()
        value = raw_value.strip()
        if not name or name in inventory[current_section]:
            raise ValueError(f"duplicate channel entry on line {line_number}")
        if not value.isdigit() or int(value) <= 0:
            raise ValueError(
                f"{current_section}.{name} is not a positive Discord channel ID"
            )
        assignment = f"{current_section}.{name}"
        if value in assigned_ids:
            raise ValueError(
                f"Discord channel ID is assigned to both "
                f"{assigned_ids[value]} and {assignment}"
            )
        inventory[current_section][name] = value
        assigned_ids[value] = assignment
    return inventory


def channel_inventory_check(path: Path) -> Check:
    try:
        inventory = load_channel_inventory(path)
        missing = [
            f"{section}.{name}"
            for section, required_names in REQUIRED_DISCORD_CHANNELS.items()
            for name in sorted(required_names.difference(inventory.get(section, {})))
        ]
    except (OSError, UnicodeError, ValueError) as error:
        return Check("huey:channels", False, f"invalid inventory: {error}")
    if missing:
        return Check(
            "huey:channels",
            False,
            "missing required channel(s): " + ", ".join(missing),
        )
    required_count = sum(len(names) for names in REQUIRED_DISCORD_CHANNELS.values())
    return Check(
        "huey:channels",
        True,
        f"{required_count} request and lifecycle channel IDs are valid and unique",
    )


def huey_ready_check(path: Path) -> Check:
    try:
        if not path.is_file():
            return Check("huey:discord-ready", False, "ready marker missing")
        ready = path.read_text(encoding="utf-8").strip() == "ready"
    except (OSError, UnicodeError) as error:
        return Check("huey:discord-ready", False, type(error).__name__)
    return Check(
        "huey:discord-ready",
        ready,
        "ready marker valid" if ready else "ready marker content invalid",
    )


def huey_selection_ttl_check(environment: dict[str, str]) -> Check:
    """Validate the bounded Discord candidate-confirmation lifetime."""

    raw = environment.get("HUEY_SELECTION_TTL_SECONDS", "900")
    if not re.fullmatch(r"[0-9]+", raw):
        return Check(
            "huey:selection-ttl",
            False,
            "must be a literal integer between 1 and 86400 seconds",
        )
    ttl = int(raw)
    valid = 1 <= ttl <= 86_400
    return Check(
        "huey:selection-ttl",
        valid,
        f"{ttl} seconds" if valid else "must be between 1 and 86400 seconds",
    )


def _sqlite_indexes(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[bool, tuple[str, ...]]]:
    if not re.fullmatch(r"[a-z_]+", table):  # pragma: no cover - constants only
        raise ValueError("invalid SQLite table name")
    definitions: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for row in connection.execute(f"PRAGMA index_list({table})"):
        name = str(row[1])
        columns = tuple(
            str(column[2])
            for column in connection.execute(f'PRAGMA index_info("{name}")')
        )
        definitions[name] = (bool(row[2]), columns)
    return definitions


def _has_unique_columns(
    indexes: dict[str, tuple[bool, tuple[str, ...]]], columns: tuple[str, ...]
) -> bool:
    return any(unique and indexed == columns for unique, indexed in indexes.values())


def huey_database_check(database: Path) -> Check:
    """Require Huey's request, ebook-cascade, and unavailable-retry schema."""

    try:
        with closing(_open_readonly_database(database)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required_tables = {
                "requests",
                "notification_deliveries",
                "candidate_confirmations",
                "candidate_options",
                "candidate_confirmation_replies",
                "ebook_cascades",
                "ebook_backend_attempts",
                "ebook_backend_reservations",
                "unavailable_retries",
                "trusted_library_events",
            }
            if not required_tables <= tables:
                return Check(
                    "huey:database", False, "required durable state tables missing"
                )

            def columns(table: str) -> set[str]:
                return {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }

            def primary_key(table: str) -> tuple[str, ...]:
                return tuple(
                    str(row[1])
                    for row in sorted(
                        (
                            row
                            for row in connection.execute(
                                f"PRAGMA table_info({table})"
                            )
                            if int(row[5]) > 0
                        ),
                        key=lambda row: int(row[5]),
                    )
                )

            def foreign_keys(
                table: str,
            ) -> set[tuple[str, str, str, str]]:
                return {
                    (
                        str(row[3]),
                        str(row[2]),
                        str(row[4]),
                        str(row[6]).casefold(),
                    )
                    for row in connection.execute(
                        f"PRAGMA foreign_key_list({table})"
                    )
                }

            request_columns = columns("requests")
            delivery_columns = columns("notification_deliveries")
            confirmation_columns = columns("candidate_confirmations")
            option_columns = columns("candidate_options")
            reply_columns = columns("candidate_confirmation_replies")
            cascade_columns = columns("ebook_cascades")
            attempt_columns = columns("ebook_backend_attempts")
            reservation_columns = columns("ebook_backend_reservations")
            retry_columns = columns("unavailable_retries")
            trusted_event_columns = columns("trusted_library_events")
            request_indexes = _sqlite_indexes(connection, "requests")
            delivery_indexes = _sqlite_indexes(connection, "notification_deliveries")
            confirmation_indexes = _sqlite_indexes(connection, "candidate_confirmations")
            option_indexes = _sqlite_indexes(connection, "candidate_options")
            reply_indexes = _sqlite_indexes(connection, "candidate_confirmation_replies")
            cascade_indexes = _sqlite_indexes(connection, "ebook_cascades")
            attempt_indexes = _sqlite_indexes(connection, "ebook_backend_attempts")
            reservation_indexes = _sqlite_indexes(
                connection, "ebook_backend_reservations"
            )
            retry_indexes = _sqlite_indexes(connection, "unavailable_retries")
            trusted_event_indexes = _sqlite_indexes(
                connection, "trusted_library_events"
            )
            retry_active_index_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'unavailable_retries_active_identity_uq'"
            ).fetchone()
            retry_active_sql = (
                " ".join(
                    str(retry_active_index_row[0] or "").casefold().split()
                )
                if retry_active_index_row
                else ""
            )
            retry_active_compact_sql = "".join(retry_active_sql.split())

            active_index = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'requests_active_target_uq'"
            ).fetchone()
            active_sql = str(active_index[0] or "").casefold() if active_index else ""
            ll_hash_index_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'requests_active_ll_hash_uq'"
            ).fetchone()
            ll_hash_index_sql = (
                "".join(str(ll_hash_index_row[0] or "").casefold().split())
                if ll_hash_index_row
                else ""
            )
            abba_hash_index_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'requests_active_abba_hash_uq'"
            ).fetchone()
            abba_hash_index_sql = (
                "".join(str(abba_hash_index_row[0] or "").casefold().split())
                if abba_hash_index_row
                else ""
            )
            abba_candidate_index_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'requests_active_abba_candidate_uq'"
            ).fetchone()
            abba_candidate_index_sql = (
                "".join(
                    str(abba_candidate_index_row[0] or "").casefold().split()
                )
                if abba_candidate_index_row
                else ""
            )
            expiry_index = confirmation_indexes.get(
                "candidate_confirmations_expiry_idx"
            )
            cascade_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'ebook_cascades'"
            ).fetchone()
            attempt_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'ebook_backend_attempts'"
            ).fetchone()
            cascade_sql = (
                str(cascade_sql_row[0] or "").casefold()
                if cascade_sql_row
                else ""
            )
            attempt_sql = (
                str(attempt_sql_row[0] or "").casefold()
                if attempt_sql_row
                else ""
            )
            retry_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'unavailable_retries'"
            ).fetchone()
            retry_sql = (
                " ".join(str(retry_sql_row[0] or "").casefold().split())
                if retry_sql_row
                else ""
            )
            terminal_trigger_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'ebook_request_terminal_sync'"
            ).fetchone()
            terminal_trigger_sql = (
                " ".join(str(terminal_trigger_row[0] or "").casefold().split())
                if terminal_trigger_row
                else ""
            )
            retry_failure_trigger_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'unavailable_retry_import_failure_sync'"
            ).fetchone()
            retry_failure_trigger_sql = (
                " ".join(
                    str(retry_failure_trigger_row[0] or "").casefold().split()
                )
                if retry_failure_trigger_row
                else ""
            )
            retry_blocked_guard_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'unavailable_retry_blocked_completion_guard'"
            ).fetchone()
            retry_blocked_guard_sql = (
                " ".join(
                    str(retry_blocked_guard_row[0] or "").casefold().split()
                )
                if retry_blocked_guard_row
                else ""
            )
            retry_terminal_trigger_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'unavailable_retry_terminal_sync'"
            ).fetchone()
            retry_terminal_trigger_sql = (
                " ".join(
                    str(retry_terminal_trigger_row[0] or "").casefold().split()
                )
                if retry_terminal_trigger_row
                else ""
            )
            cascade_active_index_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'ebook_cascades_active_identity_uq'"
            ).fetchone()
            cascade_active_sql = (
                str(cascade_active_index_row[0] or "").casefold()
                if cascade_active_index_row
                else ""
            )
            cascade_primary_key = primary_key("ebook_cascades")
            attempt_primary_key = primary_key("ebook_backend_attempts")
            reservation_primary_key = primary_key(
                "ebook_backend_reservations"
            )
            retry_primary_key = primary_key("unavailable_retries")
            cascade_foreign_keys = foreign_keys("ebook_cascades")
            attempt_foreign_keys = foreign_keys("ebook_backend_attempts")
            reservation_foreign_keys = foreign_keys(
                "ebook_backend_reservations"
            )
            retry_foreign_keys = foreign_keys("unavailable_retries")
            delivery_foreign_keys = foreign_keys("notification_deliveries")

            retry_rows = connection.execute(
                """
                SELECT retry.metadata_json, retry.canonical_title,
                       retry.canonical_creator, retry.canonical_year,
                       cascade.identity_json,
                       cascade.identity_fingerprint
                FROM unavailable_retries AS retry
                LEFT JOIN ebook_cascades AS cascade
                  ON cascade.request_id = retry.request_id
                """
            ).fetchall()
            retry_metadata_violations = 0
            retry_metadata_keys = {
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
            retry_sensitive_metadata = re.compile(
                r'(?:https?://|ftp://|magnet:|"(?:api[_-]?key|token|password|'
                r'secret|authorization)"\s*:)',
                re.IGNORECASE,
            )
            for retry_row in retry_rows:
                raw_metadata = str(retry_row["metadata_json"] or "")
                try:
                    metadata = json.loads(raw_metadata)
                except (TypeError, ValueError, json.JSONDecodeError):
                    retry_metadata_violations += 1
                    continue
                if not isinstance(metadata, dict):
                    retry_metadata_violations += 1
                    continue
                source_work_ids = metadata.get("source_work_ids", [])
                year = metadata.get("year")
                if (
                    set(metadata) != retry_metadata_keys
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(metadata.get("fingerprint") or ""),
                    )
                    or metadata.get("fingerprint")
                    != retry_row["identity_fingerprint"]
                    or not isinstance(metadata.get("label"), str)
                    or not isinstance(metadata.get("work_id"), str)
                    or not isinstance(source_work_ids, list)
                    or not 1 <= len(source_work_ids) <= 8
                    or any(
                        not isinstance(value, str) or not value
                        for value in source_work_ids
                    )
                    or source_work_ids[0] != metadata.get("work_id")
                    or metadata.get("title") != retry_row["canonical_title"]
                    or metadata.get("author") != retry_row["canonical_creator"]
                    or year != retry_row["canonical_year"]
                    or isinstance(year, bool)
                    or (year is not None and not isinstance(year, int))
                    or metadata.get("content_kind") != "book"
                    or metadata.get("media_type") != "ebooks"
                    or metadata.get("book_type") != "ebook"
                    or raw_metadata != str(retry_row["identity_json"] or "")
                    or retry_sensitive_metadata.search(raw_metadata)
                ):
                    retry_metadata_violations += 1

            retry_link_violations = connection.execute(
                """
                SELECT COUNT(*)
                FROM unavailable_retries AS retry
                LEFT JOIN requests AS request
                  ON request.id = retry.request_id
                LEFT JOIN ebook_cascades AS cascade
                  ON cascade.request_id = retry.request_id
                WHERE request.id IS NULL
                   OR cascade.request_id IS NULL
                   OR retry.media_type != 'ebooks'
                   OR request.media_type != 'ebooks'
                   OR cascade.identity_key IS NOT retry.identity_key
                   OR request.discord_user_id IS NOT retry.discord_user_id
                   OR request.discord_username IS NOT retry.discord_username
                   OR request.channel_id IS NOT retry.channel_id
                   OR request.message_id IS NOT retry.message_id
                   OR NOT EXISTS (
                       SELECT 1 FROM ebook_backend_attempts AS attempt
                       WHERE attempt.request_id = retry.request_id
                   )
                   OR (
                       retry.state IN (
                           'queued', 'retrying', 'awaiting_import', 'blocked',
                           'fulfilled'
                       )
                       AND (
                           NOT EXISTS (
                               SELECT 1 FROM ebook_backend_reservations AS reservation
                               WHERE reservation.request_id = retry.request_id
                           )
                           OR EXISTS (
                               SELECT 1 FROM ebook_backend_attempts AS attempt
                               WHERE attempt.request_id = retry.request_id
                                 AND attempt.backend_identity IS NOT NULL
                                 AND NOT EXISTS (
                                     SELECT 1
                                     FROM ebook_backend_reservations AS reservation
                                     WHERE reservation.request_id = retry.request_id
                                       AND reservation.backend = attempt.backend
                                       AND reservation.backend_identity =
                                           attempt.backend_identity
                                 )
                           )
                       )
                   )
                   OR (retry.retry_count = 0 AND retry.last_retry_at IS NOT NULL)
                   OR (retry.retry_count > 0 AND retry.last_retry_at IS NULL)
                   OR (
                       retry.state = 'queued'
                       AND (
                           request.status != 'failed'
                           OR cascade.state != 'failed'
                           OR retry.retry_count >= 7
                       )
                   )
                   OR (
                       retry.state = 'awaiting_import'
                       AND (
                           request.status != 'queued'
                           OR NOT (
                               (
                                   cascade.state = 'queued'
                                   AND cascade.final_backend IS NOT NULL
                                   AND cascade.finalizer IS NOT NULL
                               )
                               OR (
                                   cascade.state = 'uncertain'
                                   AND cascade.mutation_backend IS NOT NULL
                                   AND cascade.mutation_started_at IS NOT NULL
                                   AND cascade.final_backend IS NULL
                                   AND cascade.finalizer IS NULL
                               )
                           )
                       )
                   )
                   OR (
                       retry.state = 'blocked'
                       AND (
                           request.status != 'failed'
                           OR cascade.state != 'failed'
                           OR NOT (
                               (
                                   cascade.final_backend IS NOT NULL
                                   AND cascade.finalizer IS NOT NULL
                               )
                               OR (
                                   cascade.mutation_backend IS NOT NULL
                                   AND cascade.mutation_started_at IS NOT NULL
                               )
                           )
                           OR NOT EXISTS (
                               SELECT 1
                               FROM ebook_backend_attempts AS attempt
                               JOIN ebook_backend_reservations AS reservation
                                 ON reservation.request_id = attempt.request_id
                                AND reservation.backend = attempt.backend
                                AND reservation.backend_identity =
                                    attempt.backend_identity
                               WHERE attempt.request_id = retry.request_id
                                 AND attempt.ordinal = cascade.current_ordinal
                                 AND attempt.backend_identity IS NOT NULL
                           )
                       )
                   )
                   OR (
                       retry.state = 'fulfilled'
                       AND (
                           request.status NOT IN ('complete', 'completed')
                           OR cascade.state != 'completed'
                           OR cascade.final_backend IS NULL
                           OR cascade.finalizer IS NULL
                       )
                   )
                   OR (
                       retry.state = 'expired'
                       AND (
                           request.status != 'failed'
                           OR cascade.state != 'failed'
                           OR retry.retry_count != 7
                           OR EXISTS (
                               SELECT 1 FROM ebook_backend_reservations AS reservation
                               WHERE reservation.request_id = retry.request_id
                           )
                       )
                   )
                """
            ).fetchone()[0]
            duplicate_retry_owners = connection.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT identity_key
                    FROM unavailable_retries
                    WHERE state IN (
                        'queued', 'retrying', 'awaiting_import', 'blocked'
                    )
                    GROUP BY identity_key
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            retry_silence_violations = connection.execute(
                """
                SELECT COUNT(*)
                FROM notification_deliveries AS delivery
                JOIN unavailable_retries AS retry
                  ON retry.request_id = delivery.request_id
                WHERE retry.state IN ('retrying', 'awaiting_import', 'blocked')
                  AND delivery.event_key NOT IN (
                      'request_accepted', 'request_failed'
                  )
                """
            ).fetchone()[0]
            retry_proof_cursor_violations = connection.execute(
                """
                SELECT COUNT(*)
                FROM unavailable_retries
                WHERE last_proof_check_at IS NOT NULL
                  AND (
                      state NOT IN ('blocked', 'fulfilled')
                      OR datetime(last_proof_check_at) IS NULL
                  )
                """
            ).fetchone()[0]
            premature_retry_success = connection.execute(
                """
                SELECT COUNT(*)
                FROM notification_deliveries AS delivery
                JOIN unavailable_retries AS retry
                  ON retry.request_id = delivery.request_id
                WHERE retry.state != 'fulfilled'
                  AND delivery.event_key IN (
                      'request_completed', 'library_imported'
                  )
                """
            ).fetchone()[0]
            retry_violations = sum(
                int(value)
                for value in (
                    retry_metadata_violations,
                    retry_link_violations,
                    duplicate_retry_owners,
                    retry_silence_violations,
                    retry_proof_cursor_violations,
                    premature_retry_success,
                )
            )

            ownership_violations = 0
            if {"abba_candidate_id", "canonical_request_id"} <= request_columns:
                active_abba_filter = """
                    service = 'abba'
                    AND canonical_request_id IS NULL
                    AND status IN (
                        'processing', 'queued', 'complete', 'completed'
                    )
                """
                duplicate_abba_hashes = connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM requests
                        WHERE {active_abba_filter}
                          AND external_id IS NOT NULL
                        GROUP BY lower(external_id)
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
                duplicate_abba_candidates = connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM requests
                        WHERE {active_abba_filter}
                          AND abba_candidate_id IS NOT NULL
                        GROUP BY abba_candidate_id
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
                invalid_abba_aliases = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM requests AS alias
                    LEFT JOIN requests AS canonical
                      ON canonical.id = alias.canonical_request_id
                    WHERE alias.canonical_request_id IS NOT NULL
                      AND (
                          alias.id = alias.canonical_request_id
                          OR alias.service IS NOT 'abba'
                          OR alias.status IS NOT 'failed'
                          OR alias.external_status IS NOT 'canonical_duplicate'
                          OR canonical.id IS NULL
                          OR canonical.service IS NOT 'abba'
                          OR canonical.canonical_request_id IS NOT NULL
                      )
                    """
                ).fetchone()[0]
                pending_alias_deliveries = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM notification_deliveries AS delivery
                    JOIN requests AS alias ON alias.id = delivery.request_id
                    WHERE alias.canonical_request_id IS NOT NULL
                      AND delivery.delivered_at IS NULL
                    """
                ).fetchone()[0]
                ownership_violations = sum(
                    int(value)
                    for value in (
                        duplicate_abba_hashes,
                        duplicate_abba_candidates,
                        invalid_abba_aliases,
                        pending_alias_deliveries,
                    )
                )

        schema_ok = bool(
            {
                "status",
                "updated_at",
                "service",
                "external_id",
                "external_status",
                "error",
                "target_key",
                "message_id",
                "abba_candidate_id",
                "canonical_request_id",
            }
            <= request_columns
            and {
                "request_id",
                "event_key",
                "route",
                "message",
                "delivered_at",
                "trusted_event_id",
            }
            <= delivery_columns
            and {
                "source_type",
                "source_fingerprint",
                "source_path",
                "state",
                "radarr_movie_id",
                "radarr_command_id",
                "final_path",
                "size_bytes",
                "error",
            }
            <= trusted_event_columns
            and {
                "request_id",
                "shelfarr_correlation",
                "expires_at",
                "status",
                "prompt_message_id",
                "selected_ordinal",
                "dispatch_started_at",
            }
            <= confirmation_columns
            and {
                "confirmation_id",
                "ordinal",
                "fingerprint",
                "label",
                "book_type",
                "candidate_json",
            }
            <= option_columns
            and {
                "confirmation_id",
                "reply_message_id",
                "discord_user_id",
                "channel_id",
                "ordinal",
                "outcome",
            }
            <= reply_columns
            and {
                "request_id",
                "policy_json",
                "current_ordinal",
                "state",
                "identity_key",
                "identity_fingerprint",
                "identity_json",
                "mutation_backend",
                "mutation_started_at",
                "final_backend",
                "finalizer",
                "updated_at",
            }
            <= cascade_columns
            and {
                "request_id",
                "ordinal",
                "backend",
                "status",
                "started_at",
                "finished_at",
                "mutation_started_at",
                "mutation_resolved_at",
                "backend_identity",
                "external_id",
                "external_status",
                "outcome_message",
            }
            <= attempt_columns
            and {
                "backend",
                "backend_identity",
                "request_id",
                "created_at",
            }
            <= reservation_columns
            and {
                "request_id",
                "media_type",
                "identity_key",
                "metadata_json",
                "canonical_title",
                "canonical_creator",
                "canonical_year",
                "discord_user_id",
                "discord_username",
                "channel_id",
                "message_id",
                "first_unavailable_at",
                "last_retry_at",
                "last_proof_check_at",
                "next_retry_at",
                "retry_count",
                "state",
                "final_import_state",
                "fulfilled_at",
                "expired_at",
                "updated_at",
            }
            <= retry_columns
            and cascade_primary_key == ("request_id",)
            and attempt_primary_key
            == ("request_id", "ordinal")
            and reservation_primary_key
            == ("backend", "backend_identity")
            and retry_primary_key == ("request_id",)
            and (
                "request_id",
                "requests",
                "id",
                "cascade",
            )
            in cascade_foreign_keys
            and (
                "request_id",
                "ebook_cascades",
                "request_id",
                "cascade",
            )
            in attempt_foreign_keys
            and (
                "request_id",
                "ebook_cascades",
                "request_id",
                "cascade",
            )
            in reservation_foreign_keys
            and (
                "request_id",
                "requests",
                "id",
                "cascade",
            )
            in retry_foreign_keys
            and (
                "request_id", "requests", "id", "cascade"
            ) in delivery_foreign_keys
            and (
                "trusted_event_id", "trusted_library_events", "id", "cascade"
            ) in delivery_foreign_keys
            and _has_unique_columns(request_indexes, ("message_id",))
            and _has_unique_columns(request_indexes, ("target_key",))
            and request_indexes.get(
                "requests_active_ll_hash_uq", (False, ())
            )[0]
            and "onrequests(lower(external_id))" in ll_hash_index_sql
            and ll_hash_index_sql.partition("where")[2]
            == "service='lazylibrarian'andexternal_idisnotnull"
            "andstatusin('processing','queued','complete','completed')"
            and request_indexes.get("requests_active_abba_hash_uq", (False, ()))[0]
            and "onrequests(lower(external_id))" in abba_hash_index_sql
            and abba_hash_index_sql.partition("where")[2]
            == "service='abba'andexternal_idisnotnull"
            "andcanonical_request_idisnullandstatusin("
            "'processing','queued','complete','completed')"
            and request_indexes.get("requests_active_abba_candidate_uq")
            == (True, ("abba_candidate_id",))
            and "onrequests(abba_candidate_id)" in abba_candidate_index_sql
            and abba_candidate_index_sql.partition("where")[2]
            == "service='abba'andabba_candidate_idisnotnull"
            "andcanonical_request_idisnullandstatusin("
            "'processing','queued','complete','completed')"
            and request_indexes.get("requests_canonical_request_idx")
            == (False, ("canonical_request_id",))
            and _has_unique_columns(
                delivery_indexes, ("request_id", "event_key", "route")
            )
            and _has_unique_columns(
                delivery_indexes, ("trusted_event_id", "event_key", "route")
            )
            and _has_unique_columns(
                trusted_event_indexes, ("source_type", "source_fingerprint")
            )
            and _has_unique_columns(confirmation_indexes, ("request_id",))
            and _has_unique_columns(
                confirmation_indexes, ("shelfarr_correlation",)
            )
            and _has_unique_columns(confirmation_indexes, ("prompt_message_id",))
            and _has_unique_columns(
                option_indexes, ("confirmation_id", "ordinal")
            )
            and _has_unique_columns(
                option_indexes, ("confirmation_id", "fingerprint")
            )
            and _has_unique_columns(reply_indexes, ("reply_message_id",))
            and expiry_index == (
                False,
                ("status", "expires_at", "id"),
            )
            and cascade_indexes.get("ebook_cascades_active_identity_uq")
            == (True, ("identity_key",))
            and cascade_indexes.get("ebook_cascades_resume_idx")
            == (
                False,
                ("state", "current_ordinal", "updated_at", "request_id"),
            )
            and attempt_indexes.get("ebook_backend_attempts_state_idx")
            == (False, ("status", "request_id", "ordinal"))
            and _has_unique_columns(
                attempt_indexes, ("request_id", "backend")
            )
            and _has_unique_columns(
                reservation_indexes, ("backend", "backend_identity")
            )
            and retry_indexes.get("unavailable_retries_active_identity_uq")
            == (True, ("identity_key",))
            and "onunavailable_retries(identity_key)" in retry_active_compact_sql
            and retry_active_compact_sql.partition("where")[2]
            == "statein('queued','retrying','awaiting_import','blocked')"
            and retry_indexes.get("unavailable_retries_due_idx")
            == (False, ("state", "next_retry_at", "request_id"))
            and all(
                token in cascade_active_sql
                for token in (
                    "where identity_key is not null",
                    "'searching'",
                    "'awaiting_selection'",
                    "'mutating'",
                    "'uncertain'",
                    "'queued'",
                    "'completed'",
                )
            )
            and all(
                token in cascade_sql
                for token in (
                    "'searching'",
                    "'awaiting_selection'",
                    "'mutating'",
                    "'uncertain'",
                    "'queued'",
                    "'completed'",
                    "'failed'",
                    "mutation_backend is null",
                    "mutation_started_at is null",
                    "final_backend is null",
                    "finalizer is null",
                    "final_backend = 'lazylibrarian' and finalizer = 'bookbot'",
                    "final_backend = 'shelfarr' and finalizer = 'shelfarr'",
                    "or identity_key is not null",
                    "or final_backend is not null",
                    "or mutation_backend = final_backend",
                )
            )
            and all(
                token in attempt_sql
                for token in (
                    "'pending'",
                    "'searching'",
                    "'awaiting_selection'",
                    "'mutating'",
                    "'miss'",
                    "'unavailable'",
                    "'queued'",
                    "'completed'",
                    "'failed'",
                    "'uncertain'",
                )
            )
            and all(
                token in terminal_trigger_sql
                for token in (
                    "after update of status on requests",
                    "new.media_type = 'ebooks'",
                    "state in ('mutating', 'uncertain', 'queued')",
                    "update ebook_backend_attempts",
                    "update ebook_cascades",
                    "final_backend = case when new.status in ('complete', 'completed') then coalesce",
                    "finalizer = case when new.status in ('complete', 'completed') then coalesce",
                    "when 'lazylibrarian' then 'bookbot'",
                    "when 'shelfarr' then 'shelfarr'",
                    "delete from ebook_backend_reservations",
                    "new.status = 'failed'",
                    "state in ('queued', 'retrying', 'awaiting_import', 'blocked', 'fulfilled')",
                )
            )
            and all(
                token in retry_sql
                for token in (
                    "media_type = 'ebooks'",
                    "retry_count between 0 and 7",
                    "'queued'",
                    "'retrying'",
                    "'awaiting_import'",
                    "'blocked'",
                    "'fulfilled'",
                    "'expired'",
                    "final_import_state = 'verified'",
                    "state = 'queued' and next_retry_at is not null",
                    "state = 'expired' and expired_at is not null",
                )
            )
            and all(
                token in retry_active_sql
                for token in (
                    "where state in",
                    "'queued'",
                    "'retrying'",
                    "'awaiting_import'",
                    "'blocked'",
                )
            )
            and all(
                token in retry_failure_trigger_sql
                for token in (
                    "after update of status on requests",
                    "new.status = 'failed'",
                    "state = 'awaiting_import'",
                    "set state = 'blocked'",
                    "'unavailable_retry_blocked'",
                )
            )
            and all(
                token in retry_blocked_guard_sql
                for token in (
                    "before update of status on requests",
                    "state = 'blocked'",
                    "new.service in ('lazylibrarian', 'shelfarr')",
                    "cascade.state = 'failed'",
                    "lower(attempt.external_id) = lower(new.external_id)",
                    "raise(",
                    "blocked unavailable retry lacks exact final-import correlation",
                )
            )
            and all(
                token in retry_terminal_trigger_sql
                for token in (
                    "after update of status on requests",
                    "new.status in ('complete', 'completed')",
                    "state in ('retrying', 'awaiting_import', 'blocked')",
                    "update ebook_backend_attempts",
                    "update ebook_cascades",
                    "state != 'blocked'",
                    "lower(external_id) = lower(new.external_id)",
                    "set state = 'fulfilled', final_import_state = 'verified'",
                    "update requests set notified_at = null",
                    "'unavailable_retry_fulfilled'",
                )
            )
            and "awaiting_selection" in active_sql
            and ownership_violations == 0
            and retry_violations == 0
        )
        return Check(
            "huey:database",
            integrity == "ok" and schema_ok,
            (
                "integrity, request/outbox, selection, ebook cascade, unavailable "
                "retry, and canonical ABBA ownership valid; violations=0"
                if integrity == "ok" and schema_ok
                else "request/outbox, selection, ebook cascade, unavailable retry, "
                "or canonical ABBA ownership invalid; "
                f"violations={ownership_violations + retry_violations}"
            ),
        )
    except Exception as error:
        return Check("huey:database", False, type(error).__name__)


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _contains_discord_webhook(value: object) -> bool:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = str(value or "")
    normalized = text.casefold()
    return any(marker in normalized for marker in DISCORD_WEBHOOK_MARKERS)


def arr_native_discord_check(service: str, database: Path) -> Check:
    """Ensure an ARR service has no native Discord notification connection."""

    name = f"{service}:native-discord"
    try:
        with closing(_open_readonly_database(database)) as connection:
            columns = {
                str(row[1]).casefold()
                for row in connection.execute("PRAGMA table_info(Notifications)")
            }
            required = {"name", "implementation", "configcontract", "settings"}
            if not required.issubset(columns):
                return Check(name, False, "notification schema unavailable")
            rows = connection.execute(
                "SELECT Name, Implementation, ConfigContract, Settings "
                "FROM Notifications"
            ).fetchall()
    except (OSError, sqlite3.Error) as error:
        return Check(name, False, type(error).__name__)

    native_discord = [
        row
        for row in rows
        if "discord" in str(row["Implementation"] or "").casefold()
        or "discord" in str(row["ConfigContract"] or "").casefold()
        or "discord" in str(row["Name"] or "").casefold()
        or _contains_discord_webhook(row["Settings"])
    ]
    return Check(
        name,
        not native_discord,
        "disabled" if not native_discord else f"configured={len(native_discord)}",
    )


def _simple_yaml_boolean(path: Path, key: str) -> bool:
    pattern = re.compile(
        rf"^\s*{re.escape(key)}\s*:\s*(true|false)\s*(?:#.*)?$",
        re.IGNORECASE,
    )
    values = [
        match.group(1).casefold() == "true"
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := pattern.match(line))
    ]
    if len(values) != 1:
        raise ValueError(f"expected exactly one {key} boolean")
    return values[0]


def _database_truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def arr_prowlarr_indexer_credentials_check(
    service: str, database: Path, expected_api_key: str
) -> Check:
    """Compare every enabled Prowlarr-backed ARR row to the private key."""

    name = f"{service}:prowlarr-indexer-credentials"
    try:
        if not database.is_file() or database.is_symlink():
            raise OSError("unsafe indexer database")
        with closing(_open_readonly_database(database)) as connection:
            columns = {
                str(row[1]).casefold()
                for row in connection.execute("PRAGMA table_info(Indexers)")
            }
            required = {
                "name",
                "settings",
                "enablerss",
                "enableautomaticsearch",
                "enableinteractivesearch",
            }
            if not required.issubset(columns):
                return Check(name, False, "indexer schema unavailable")
            rows = connection.execute(
                "SELECT Name, Settings, EnableRss, EnableAutomaticSearch, "
                "EnableInteractiveSearch FROM Indexers"
            ).fetchall()
    except (OSError, sqlite3.Error) as error:
        return Check(name, False, type(error).__name__)

    enabled_prowlarr = 0
    exact_credentials = 0
    for row in rows:
        if not any(
            _database_truthy(row[column])
            for column in (
                "EnableRss",
                "EnableAutomaticSearch",
                "EnableInteractiveSearch",
            )
        ):
            continue

        row_name = str(row["Name"] or "").strip().casefold()
        named_as_prowlarr = row_name.endswith(" (prowlarr)")
        settings: dict[str, object] | None = None
        try:
            parsed_settings = json.loads(row["Settings"])
            if isinstance(parsed_settings, dict):
                settings = parsed_settings
        except (TypeError, UnicodeError, ValueError):
            pass

        endpoint_is_prowlarr = False
        if settings is not None:
            try:
                endpoint_is_prowlarr = (
                    urllib.parse.urlparse(
                        str(settings.get("baseUrl") or "")
                    ).hostname
                    == "prowlarr"
                )
            except ValueError:
                endpoint_is_prowlarr = False

        if not named_as_prowlarr and not endpoint_is_prowlarr:
            continue
        enabled_prowlarr += 1
        stored_key = settings.get("apiKey") if settings is not None else None
        if (
            endpoint_is_prowlarr
            and isinstance(stored_key, str)
            and bool(expected_api_key)
            and secrets.compare_digest(stored_key, expected_api_key)
        ):
            exact_credentials += 1

    ok = enabled_prowlarr > 0 and exact_credentials == enabled_prowlarr
    mismatched = enabled_prowlarr - exact_credentials
    return Check(
        name,
        ok,
        (
            f"enabled_prowlarr={enabled_prowlarr} "
            f"exact_credentials={exact_credentials}"
            if ok
            else (
                f"enabled_prowlarr={enabled_prowlarr} "
                f"exact_credentials={exact_credentials} "
                f"mismatched_or_malformed={mismatched}"
            )
        ),
    )


def arr_qbittorrent_download_client_credentials_check(
    service: str,
    database: Path,
    expected_username: str,
    expected_password: str,
) -> Check:
    """Compare enabled ARR qBittorrent rows to private unmasked state."""

    name = f"{service}:qbittorrent-credentials"
    try:
        if not database.is_file() or database.is_symlink():
            raise OSError("unsafe download-client database")
        with closing(_open_readonly_database(database)) as connection:
            columns = {
                str(row[1]).casefold()
                for row in connection.execute(
                    "PRAGMA table_info(DownloadClients)"
                )
            }
            required = {"enable", "implementation", "settings"}
            if not required.issubset(columns):
                return Check(name, False, "download-client schema unavailable")
            rows = connection.execute(
                "SELECT Enable, Implementation, Settings FROM DownloadClients"
            ).fetchall()
    except (OSError, sqlite3.Error) as error:
        return Check(name, False, type(error).__name__)

    enabled_qbittorrent = 0
    exact_credentials = 0
    for row in rows:
        if (
            not _database_truthy(row["Enable"])
            or str(row["Implementation"] or "").strip().casefold()
            != "qbittorrent"
        ):
            continue
        enabled_qbittorrent += 1
        settings: dict[str, object] | None = None
        try:
            parsed_settings = json.loads(row["Settings"])
            if isinstance(parsed_settings, dict):
                settings = parsed_settings
        except (TypeError, UnicodeError, ValueError):
            pass
        stored_username = (
            settings.get("username") if settings is not None else None
        )
        stored_password = (
            settings.get("password") if settings is not None else None
        )
        if (
            isinstance(stored_username, str)
            and isinstance(stored_password, str)
            and bool(expected_username)
            and bool(expected_password)
            and secrets.compare_digest(stored_username, expected_username)
            and secrets.compare_digest(stored_password, expected_password)
        ):
            exact_credentials += 1

    ok = (
        enabled_qbittorrent > 0
        and exact_credentials == enabled_qbittorrent
    )
    mismatched = enabled_qbittorrent - exact_credentials
    return Check(
        name,
        ok,
        (
            f"enabled_qbittorrent={enabled_qbittorrent} "
            f"exact_credentials={exact_credentials}"
            if ok
            else (
                f"enabled_qbittorrent={enabled_qbittorrent} "
                f"exact_credentials={exact_credentials} "
                f"mismatched_or_malformed={mismatched}"
            )
        ),
    )


def _strict_feature_flag(
    environment: dict[str, str], key: str
) -> tuple[bool, bool, str]:
    """Return validity, enabled state, and a secret-free status detail."""

    value = environment.get(key, "")
    valid = value in {"", "false", "true"}
    return (
        valid,
        value == "true",
        f"literal {value or 'false'}" if valid else "must be literal true or false",
    )


def _strict_ebook_owner(
    environment: dict[str, str],
) -> tuple[bool, str, str]:
    """Return the deprecated primary assertion, defaulting only when absent."""

    value = environment.get(EBOOK_OWNER_SETTING)
    if value is None or not value.strip():
        value = "shelfarr"
    valid = value in {*SUPPORTED_EBOOK_BACKENDS, "direct"}
    return (
        valid,
        value,
        f"literal {value}"
        if valid
        else "must be lazylibrarian, shelfarr, or legacy direct",
    )


def _strict_ebook_backends(
    environment: dict[str, str],
) -> tuple[bool, tuple[str, ...], str]:
    """Parse the ordered cascade without silently repairing invalid policy."""

    raw = environment.get(EBOOK_BACKENDS_SETTING)
    explicit_policy = bool(raw is not None and raw.strip())
    explicit_owner = environment.get(EBOOK_OWNER_SETTING)
    owner_is_explicit = bool(
        explicit_owner is not None and explicit_owner.strip()
    )
    if not explicit_policy:
        raw = explicit_owner if owner_is_explicit else "shelfarr"

    if raw == "__WYSEARR_DUPLICATE__":
        return False, (), "must have one exact assignment"

    parts = raw.split(",")
    backends = tuple(part.strip() for part in parts)
    if not backends or any(not backend for backend in backends):
        return False, backends, "contains a blank backend"
    if not explicit_policy and backends == ("direct",):
        return True, backends, "legacy singleton direct policy"
    if any(backend not in SUPPORTED_EBOOK_BACKENDS for backend in backends):
        return False, backends, "contains an unknown or noncanonical backend"
    if len(set(backends)) != len(backends):
        return False, backends, "contains a duplicate backend"

    if owner_is_explicit:
        owner_valid, owner, _owner_detail = _strict_ebook_owner(environment)
        if not owner_valid:
            return False, backends, "compatibility owner is invalid"
        if owner != backends[0]:
            return False, backends, "compatibility owner must match the first backend"

    return True, backends, f"ordered policy {','.join(backends)}"


def ebook_backend_order_check(
    backends: tuple[str, ...], *, policy_valid: bool
) -> Check:
    """Require the authoritative production primary/fallback ordering."""

    ok = bool(policy_valid and backends == PRODUCTION_EBOOK_BACKENDS)
    return Check(
        "ebooks:backend-order",
        ok,
        (
            "LazyLibrarian primary, Shelfarr secondary"
            if ok
            else "production policy must be exactly lazylibrarian,shelfarr"
        ),
    )


def ebook_backend_availability_check(
    backends: tuple[str, ...],
    *,
    policy_valid: bool,
    environment: dict[str, str],
    shelfarr_enabled: bool,
    shelfarr_flag_valid: bool,
    lazylibrarian_enabled: bool,
    lazylibrarian_flag_valid: bool,
) -> Check:
    """Require every configured backend to be enabled and credentialed."""

    requirements = {
        "lazylibrarian": bool(
            lazylibrarian_flag_valid
            and lazylibrarian_enabled
            and environment.get(
                "LAZYLIBRARIAN_URL", "http://lazylibrarian:5299"
            )
            == "http://lazylibrarian:5299"
            and re.fullmatch(
                r"[0-9a-f]{32}",
                environment.get("LAZYLIBRARIAN_API_KEY", ""),
            )
        ),
        "shelfarr": bool(
            shelfarr_flag_valid
            and shelfarr_enabled
            and environment.get("SHELFARR_URL", "http://shelfarr")
            == "http://shelfarr"
            and environment.get("SHELFARR_API_TOKEN", "")
        ),
    }
    coherent = bool(
        policy_valid
        and backends
        and all(requirements.get(backend, False) for backend in backends)
    )
    return Check(
        "ebooks:backend-availability",
        coherent,
        (
            "all configured ebook backends are enabled and credentialed"
            if coherent
            else "a configured ebook backend is disabled, uncredentialed, or misrouted"
        ),
    )


def bazarr_native_discord_check(database: Path, config: Path) -> Check:
    """Ensure Bazarr cannot bypass Huey with a native Discord notification."""

    name = "bazarr:native-discord"
    try:
        with closing(_open_readonly_database(database)) as connection:
            columns = {
                str(row[1]).casefold()
                for row in connection.execute(
                    "PRAGMA table_info(table_settings_notifier)"
                )
            }
            if not {"name", "enabled", "url"}.issubset(columns):
                return Check(name, False, "notifier schema unavailable")
            rows = connection.execute(
                "SELECT name, enabled, url FROM table_settings_notifier"
            ).fetchall()
        external_webhook_enabled = _simple_yaml_boolean(
            config, "use_external_webhook"
        )
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as error:
        return Check(name, False, type(error).__name__)

    enabled_discord = [
        row
        for row in rows
        if _database_truthy(row["enabled"])
        and (
            str(row["name"] or "").strip().casefold() == "discord"
            or _contains_discord_webhook(row["url"])
        )
    ]
    ok = not enabled_discord and not external_webhook_enabled
    return Check(
        name,
        ok,
        "disabled"
        if ok
        else (
            f"enabled native routes={len(enabled_discord)} "
            f"external_webhook={external_webhook_enabled}"
        ),
    )


def request_json(
    url: str,
    *,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> object:
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    if api_key:
        request_headers["X-Api-Key"] = api_key
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def sabnzbd_post_json(
    port: str,
    api_key: str,
    parameters: dict[str, object],
    *,
    timeout: int = 30,
) -> object:
    """Call a SAB configuration endpoint without putting credentials in a URL."""

    body = urllib.parse.urlencode(
        {"output": "json", "apikey": api_key, **parameters}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def qbit_opener(base_url: str, username: str, password: str) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("Referer", f"{base_url.rstrip('/')}/")]
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(f"{base_url}/api/v2/auth/login", data=body)
    with opener.open(request, timeout=15) as response:
        result = response.read().decode("utf-8", "replace").strip()
    if result != "Ok.":
        raise RuntimeError("qBittorrent authentication rejected")
    return opener


def provider_fields(resource: dict[str, object]) -> dict[str, object]:
    fields = resource.get("fields")
    if not isinstance(fields, list):
        return {}
    return {
        str(field.get("name", "")).casefold(): field.get("value")
        for field in fields
        if isinstance(field, dict) and field.get("name")
    }


def indexer_category_ids(resource: dict[str, object]) -> set[int]:
    capabilities = resource.get("capabilities")
    categories = capabilities.get("categories") if isinstance(capabilities, dict) else []
    if not isinstance(categories, list):
        return set()
    result: set[int] = set()
    pending = list(categories)
    while pending:
        category = pending.pop()
        if not isinstance(category, dict):
            continue
        raw_id = category.get("id")
        if not isinstance(raw_id, bool):
            try:
                result.add(int(raw_id))
            except (TypeError, ValueError):
                pass
        children = category.get("subCategories", [])
        if isinstance(children, list):
            pending.extend(children)
    return result


def prowlarr_managed_newznab_check(
    indexers: list[dict[str, object]],
    tags: list[dict[str, object]],
    applications: list[dict[str, object]],
    live_indexer_ids: set[int],
    environment: dict[str, str],
    *,
    enabled: bool,
    flag_valid: bool = True,
) -> Check:
    """Require a live Shelfarr-only Generic Newznab when Usenet is enabled."""

    configured_name = environment.get("NEWZNAB_INDEXER_NAME", "")
    identity_valid = configured_name in {"", MANAGED_NEWZNAB_INDEXER_DEFAULT}
    managed_name = MANAGED_NEWZNAB_INDEXER_DEFAULT
    managed_matches = [
        item
        for item in indexers
        if str(item.get("name") or "").casefold() == managed_name.casefold()
    ]
    managed_indexer = managed_matches[0] if len(managed_matches) == 1 else None
    shelfarr_tags = [
        item
        for item in tags
        if str(item.get("label") or "").casefold() == MANAGED_PROWLARR_TAG
    ]
    arr_tags = [
        item
        for item in tags
        if str(item.get("label") or "").casefold() == ARR_PROWLARR_TAG
    ]
    shelfarr_tag_id = (
        shelfarr_tags[0].get("id") if len(shelfarr_tags) == 1 else None
    )
    arr_tag_id = arr_tags[0].get("id") if len(arr_tags) == 1 else None
    required_apps: dict[str, list[dict[str, object]]] = {}
    for required_name in ("sonarr", "radarr", "lidarr", "whisparr"):
        required_apps[required_name] = [
            app
            for app in applications
            if str(app.get("name") or "").casefold() == required_name
            or str(app.get("implementation") or "").casefold() == required_name
        ]
    if not enabled:
        managed_disabled = len(managed_matches) <= 1 and (
            managed_indexer is None or managed_indexer.get("enable") is False
        )
        topology_absent = not shelfarr_tags and not arr_tags
        topology_retained = bool(
            len(shelfarr_tags) == 1
            and len(arr_tags) == 1
            and isinstance(shelfarr_tag_id, int)
            and isinstance(arr_tag_id, int)
            and shelfarr_tag_id != arr_tag_id
            and all(len(matches) == 1 for matches in required_apps.values())
            and all(
                arr_tag_id in set(matches[0].get("tags") or [])
                and shelfarr_tag_id not in set(matches[0].get("tags") or [])
                for matches in required_apps.values()
                if len(matches) == 1
            )
            and all(
                item is managed_indexer
                or (
                    arr_tag_id in set(item.get("tags") or [])
                    and shelfarr_tag_id not in set(item.get("tags") or [])
                )
                for item in indexers
            )
        )
        ok = identity_valid and managed_disabled and (
            topology_absent or topology_retained
        )
        return Check(
            "prowlarr:managed-newznab",
            ok,
            "disabled"
            if ok
            else "managed Newznab or retained tag isolation is unsafe while disabled",
        )
    expected_url = environment.get("NEWZNAB_BASE_URL", "").strip().rstrip("/")
    expected_path = environment.get("NEWZNAB_API_PATH", "/api").strip()
    fields = (
        provider_fields(managed_indexer)
        if isinstance(managed_indexer, dict)
        else {}
    )
    book_categories = (
        indexer_category_ids(managed_indexer)
        if isinstance(managed_indexer, dict)
        else set()
    )
    private_settings_ok = bool(
        flag_valid
        and identity_valid
        and managed_name
        and expected_url
        and environment.get("NEWZNAB_API_KEY", "").strip()
        and expected_path.startswith("/")
    )
    app_isolation_ok = bool(
        isinstance(shelfarr_tag_id, int)
        and isinstance(arr_tag_id, int)
        and shelfarr_tag_id != arr_tag_id
        and all(len(matches) == 1 for matches in required_apps.values())
        and all(
            arr_tag_id in set(matches[0].get("tags") or [])
            and shelfarr_tag_id not in set(matches[0].get("tags") or [])
            for matches in required_apps.values()
            if len(matches) == 1
        )
    )
    application_tag_ids = set().union(
        *(
            set(matches[0].get("tags") or [])
            for matches in required_apps.values()
            if len(matches) == 1
        )
    )
    indexer_isolation_ok = bool(
        isinstance(shelfarr_tag_id, int)
        and isinstance(arr_tag_id, int)
        and all(
            (
                shelfarr_tag_id in set(item.get("tags") or [])
                and not (set(item.get("tags") or []) & application_tag_ids)
            )
            if item is managed_indexer
            else (
                arr_tag_id in set(item.get("tags") or [])
                and shelfarr_tag_id not in set(item.get("tags") or [])
            )
            for item in indexers
        )
    )
    ok = bool(
        private_settings_ok
        and isinstance(managed_indexer, dict)
        and managed_indexer.get("enable") is True
        and str(managed_indexer.get("implementation") or "").casefold()
        == "newznab"
        and str(managed_indexer.get("configContract") or "").casefold()
        == "newznabsettings"
        and str(managed_indexer.get("protocol") or "").casefold() == "usenet"
        and managed_indexer.get("name") == MANAGED_NEWZNAB_INDEXER_DEFAULT
        and str(managed_indexer.get("priority")) == "20"
        and managed_indexer.get("id") in live_indexer_ids
        and len(managed_matches) == 1
        and app_isolation_ok
        and indexer_isolation_ok
        and str(fields.get("baseurl") or "").rstrip("/") == expected_url
        and str(fields.get("apipath") or "") == expected_path
        and bool(str(fields.get("apikey") or "").strip())
        and 3030 in book_categories
        and bool({7000, 7020} & book_categories)
    )
    return Check(
        "prowlarr:managed-newznab",
        ok,
        "tag-isolated Generic Newznab configured and live"
        if ok
        else "managed indexer, private settings, tag isolation, or live test failed",
    )


def arr_download_client_accepted(
    resource: dict[str, object],
    *,
    username: str,
    category: str,
    category_fields: tuple[str, ...],
    imported_fields: tuple[str, ...],
) -> bool:
    fields = provider_fields(resource)

    def first(names: tuple[str, ...]) -> object:
        return next(
            (fields[name.casefold()] for name in names if name.casefold() in fields),
            None,
        )

    try:
        port_ok = int(fields.get("port", -1)) == 8080
    except (TypeError, ValueError):
        port_ok = False
    return bool(
        resource.get("enable")
        and str(resource.get("implementation", "")).casefold() == "qbittorrent"
        and str(fields.get("host", "")).casefold() == "qbittorrent"
        and port_ok
        and fields.get("usessl") is False
        and fields.get("username") == username
        and first(category_fields) == category
        and first(imported_fields) == f"{category}-imported"
        and resource.get("removeCompletedDownloads") is False
    )


def post_json_ok(url: str, payload: object, *, api_key: str, timeout: int = 15) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def exhaustive_indexer_live_check(
    service: str,
    indexers: object,
    tester: Callable[[dict[str, object]], None],
    *,
    enabled_default: bool,
) -> tuple[Check, set[int], set[str]]:
    """Test every enabled indexer and return only secret-free aggregates."""

    if not isinstance(indexers, list) or any(
        not isinstance(indexer, dict) for indexer in indexers
    ):
        return (
            Check(f"{service}:indexers", False, "invalid indexer inventory"),
            set(),
            set(),
        )

    enabled = 0
    live = 0
    live_ids: set[int] = set()
    live_protocols: set[str] = set()
    for indexer in indexers:
        if "enable" in indexer:
            indexer_enabled = _database_truthy(indexer.get("enable"))
        else:
            search_flags = (
                "enableRss",
                "enableAutomaticSearch",
                "enableInteractiveSearch",
            )
            present_search_flags = [
                flag for flag in search_flags if flag in indexer
            ]
            indexer_enabled = (
                any(
                    _database_truthy(indexer.get(flag))
                    for flag in present_search_flags
                )
                if present_search_flags
                else enabled_default
            )
        if not indexer_enabled:
            continue
        enabled += 1
        try:
            tester(indexer)
        except Exception:
            continue
        live += 1
        indexer_id = indexer.get("id")
        if (
            isinstance(indexer_id, int)
            and not isinstance(indexer_id, bool)
            and indexer_id > 0
        ):
            live_ids.add(indexer_id)
        protocol = str(indexer.get("protocol") or "").strip().casefold()
        if protocol in {"torrent", "usenet"}:
            live_protocols.add(protocol)

    failed = enabled - live
    return (
        Check(
            f"{service}:indexers",
            enabled > 0 and failed == 0,
            f"enabled={enabled} live={live} failed={failed}",
            blocking=enabled == 0,
        ),
        live_ids,
        live_protocols,
    )


def container_check(service: str) -> Check:
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode or not container:
        return Check(f"container:{service}", False, "not created")
    inspect = subprocess.run(
        ["docker", "inspect", container], text=True, capture_output=True, check=False
    )
    if inspect.returncode:
        return Check(f"container:{service}", False, "inspect failed")
    state = json.loads(inspect.stdout)[0]["State"]
    health = state.get("Health", {}).get("Status", "none")
    ok = state.get("Status") == "running" and health == "healthy"
    return Check(
        f"container:{service}",
        ok,
        f"state={state.get('Status')} health={health}",
    )


def container_stopped_check(
    service: str, ownership: str = "service ownership"
) -> Check:
    """Require an acquisition worker to be stopped when its owner flag is off."""

    result = subprocess.run(
        ["docker", "compose", "ps", "-a", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode != 0:
        stopped = False
        detail = "container state unavailable"
    elif not container:
        stopped = True
        detail = "not created"
    else:
        inspect = subprocess.run(
            ["docker", "inspect", container],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            state = json.loads(inspect.stdout)[0]["State"]
            status = str(state.get("Status") or "")
            stopped = bool(
                inspect.returncode == 0
                and status in {"created", "exited", "dead"}
                and not state.get("Running")
                and not state.get("Restarting")
                and not state.get("Paused")
            )
            detail = status or "unknown"
        except (ValueError, TypeError, KeyError, IndexError):
            stopped = False
            detail = "inspect failed"
    return Check(
        f"container:{service}:disabled",
        stopped,
        detail if stopped else f"unsafe state={detail} while {ownership} is disabled",
    )


def writable_check(path: Path, name: str) -> Check:
    try:
        with tempfile.NamedTemporaryFile(prefix=".wysearr-validate-", dir=path) as probe:
            probe.write(b"ok")
            probe.flush()
        return Check(name, True, "writable")
    except OSError as error:
        return Check(name, False, f"not writable: {error.strerror}")


def private_published_port_check(service: str) -> Check:
    """Require every published admin port for a private service to be loopback."""

    name = f"{service}:host-access"
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode or not container:
        return Check(name, False, "container unavailable")
    inspect = subprocess.run(
        ["docker", "inspect", container],
        text=True,
        capture_output=True,
        check=False,
    )
    if inspect.returncode:
        return Check(name, False, "inspect failed")
    bindings = json.loads(inspect.stdout)[0].get("HostConfig", {}).get(
        "PortBindings", {}
    )
    host_ips = {
        str(binding.get("HostIp", ""))
        for values in bindings.values()
        for binding in values or []
    }
    private = bool(host_ips) and host_ips <= {"127.0.0.1", "::1"}
    return Check(
        name,
        private,
        "loopback only" if private else "published beyond host loopback",
    )


def unpublished_service_check(
    service: str, *, runner: object = subprocess.run
) -> Check:
    """Require an internal API to have no host port bindings at all."""

    name = f"{service}:host-access"
    result = runner(
        ["docker", "compose", "ps", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode or not container:
        return Check(name, False, "container unavailable")
    inspect = runner(
        ["docker", "inspect", container],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        bindings = json.loads(inspect.stdout)[0].get("HostConfig", {}).get(
            "PortBindings", {}
        )
        unpublished = inspect.returncode == 0 and not any(
            values for values in (bindings or {}).values()
        )
    except (ValueError, TypeError, KeyError, IndexError):
        unpublished = False
    return Check(
        name,
        unpublished,
        "Compose-network only" if unpublished else "one or more host ports published",
    )


def service_mount_absent_check(
    service: str, destination: str, *, runner: object = subprocess.run
) -> Check:
    """Require a service to have no mount at an ownership-sensitive path."""

    name = f"{service}:mount:{destination}"
    details = _inspect_service(service, runner=runner)
    mounts = details.get("Mounts", []) if isinstance(details, dict) else []
    absent = bool(
        details is not None
        and not any(
            isinstance(mount, dict)
            and str(mount.get("Destination") or "").rstrip("/")
            == destination.rstrip("/")
            for mount in mounts
        )
    )
    return Check(
        name,
        absent,
        "absent; ownership retired"
        if absent
        else "mounted or container unavailable; ownership boundary violated",
    )


def shelfarr_storage_checks(storage: Path) -> list[Check]:
    """Validate Shelfarr's complete persistent state and notification boundary."""

    checks: list[Check] = []
    try:
        storage_stat = storage.stat()
        storage_private = bool(
            storage.is_dir()
            and storage_stat.st_uid == os.getuid()
            and stat.S_IMODE(storage_stat.st_mode) & 0o077 == 0
        )
    except OSError:
        storage_private = False
    checks.append(
        Check(
            "shelfarr:storage-permissions",
            storage_private,
            "owner-only directory"
            if storage_private
            else "directory must be owned by the service user and mode 0700",
        )
    )
    expected_databases = (
        "production.sqlite3",
        "production_cache.sqlite3",
        "production_queue.sqlite3",
        "production_cable.sqlite3",
    )
    database_results: list[bool] = []
    for filename in expected_databases:
        path = storage / filename
        try:
            with closing(_open_readonly_database(path)) as connection:
                database_results.append(
                    connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                )
        except (OSError, sqlite3.Error):
            database_results.append(False)
    checks.append(
        Check(
            "shelfarr:databases",
            all(database_results),
            f"integrity={sum(database_results)}/{len(expected_databases)}",
        )
    )

    secret_results: list[bool] = []
    for filename in (".secret_key_base", ".encryption_keys"):
        path = storage / filename
        try:
            secret_results.append(
                path.is_file()
                and bool(path.read_text(encoding="utf-8").strip())
                and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
            )
        except (OSError, UnicodeError):
            secret_results.append(False)
    checks.append(
        Check(
            "shelfarr:secrets",
            all(secret_results),
            "persistent and private"
            if all(secret_results)
            else "missing, empty, or accessible outside owner",
        )
    )

    discord_disabled = False
    try:
        with closing(_open_readonly_database(storage / "production.sqlite3")) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'discord_enabled'"
            ).fetchone()
        discord_disabled = row is None or not _database_truthy(row[0])
    except (OSError, sqlite3.Error):
        pass
    checks.append(
        Check(
            "shelfarr:native-discord",
            discord_disabled,
            "disabled" if discord_disabled else "enabled or unverifiable",
        )
    )
    return checks


def private_service_storage_check(path: Path, name: str) -> Check:
    """Require service state containing history or credentials to be owner-only."""

    try:
        info = path.stat()
        ok = bool(
            path.is_dir()
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) & 0o077 == 0
        )
    except OSError:
        ok = False
    return Check(
        name,
        ok,
        "owner-only directory" if ok else "directory must be owned and mode 0700",
    )


def _inspect_service(
    service: str, *, runner: object = subprocess.run
) -> dict[str, object] | None:
    """Return one Docker inspection document without exposing its environment."""

    result = runner(
        ["docker", "compose", "ps", "-q", service],
        cwd=STACK_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    container = result.stdout.strip()
    if result.returncode or not container:
        return None
    inspected = runner(
        ["docker", "inspect", container],
        text=True,
        capture_output=True,
        check=False,
    )
    if inspected.returncode:
        return None
    try:
        payload = json.loads(inspected.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return None
    return payload[0]


def _container_environment(details: dict[str, object] | None) -> dict[str, str]:
    if not isinstance(details, dict):
        return {}
    config = details.get("Config")
    raw_environment = config.get("Env", []) if isinstance(config, dict) else []
    values: dict[str, str] = {}
    for item in raw_environment if isinstance(raw_environment, list) else []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def qbittorrent_container_credentials_checks(
    environment: dict[str, str],
    services: tuple[str, ...],
    *,
    runner: object = subprocess.run,
) -> list[Check]:
    """Require exact qBittorrent credentials in each consuming container."""

    expected_username = environment.get("QBITTORRENT_USERNAME", "admin")
    expected_password = environment.get("QBITTORRENT_PASSWORD", "")
    contracts = {
        "huey": (
            "QBITTORRENT_URL",
            "http://qbittorrent:8080",
            "QBITTORRENT_USERNAME",
            "QBITTORRENT_PASSWORD",
        ),
        "bookbot": (
            "QBITTORRENT_URL",
            "http://qbittorrent:8080",
            "QBITTORRENT_USERNAME",
            "QBITTORRENT_PASSWORD",
        ),
        "abba": (
            "DL_HOST",
            "qbittorrent",
            "DL_USERNAME",
            "DL_PASSWORD",
        ),
    }
    checks: list[Check] = []
    for service in services:
        contract = contracts.get(service)
        details = _inspect_service(service, runner=runner)
        actual = _container_environment(details)
        ok = False
        if contract is not None:
            endpoint_key, endpoint, username_key, password_key = contract
            stored_password = actual.get(password_key)
            ok = bool(
                details is not None
                and expected_username
                and expected_password
                and actual.get(endpoint_key) == endpoint
                and actual.get(username_key) == expected_username
                and isinstance(stored_password, str)
                and secrets.compare_digest(stored_password, expected_password)
            )
        checks.append(
            Check(
                f"{service}:qbittorrent-credentials",
                ok,
                "runtime credential exactly matches the private environment"
                if ok
                else "runtime endpoint, username, or credential mismatch",
            )
        )
    return checks


def lazylibrarian_runtime_checks(
    environment: dict[str, str], *, runner: object = subprocess.run
) -> list[Check]:
    """Validate LazyLibrarian's mount boundary and Huey's private API route."""

    lazylibrarian = _inspect_service(LAZYLIBRARIAN_SERVICE, runner=runner)
    huey = _inspect_service("huey", runner=runner)
    if lazylibrarian is None:
        return [
            Check(
                "lazylibrarian:configuration",
                False,
                "container unavailable",
            )
        ]

    config = lazylibrarian.get("Config")
    config = config if isinstance(config, dict) else {}
    ll_environment = _container_environment(lazylibrarian)
    expected_image = (
        "lscr.io/linuxserver/lazylibrarian@sha256:"
        "f2fd332fb4c5918571f8babd4d52fbcb9ca514be254ba101a47c275cd57eb33f"
    )
    runtime_ok = bool(
        config.get("Image") == expected_image
        and ll_environment.get("PUID") == environment.get("PUID", "1000")
        and ll_environment.get("PGID") == environment.get("PGID", "1000")
        and ll_environment.get("TZ") == environment.get("TZ", "Pacific/Honolulu")
        and ll_environment.get("UMASK") == "077"
    )
    checks = [
        Check(
            "lazylibrarian:configuration",
            runtime_ok,
            "digest-pinned private coordinator runtime"
            if runtime_ok
            else "image identity or runtime environment mismatch",
        )
    ]

    mounts = lazylibrarian.get("Mounts", [])
    mounts = mounts if isinstance(mounts, list) else []
    config_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == "/config"
    ]
    expected_source = str(
        (STACK_ROOT / "config" / "lazylibrarian").resolve()
    )
    persistence_ok = bool(
        len(mounts) == 1
        and len(config_mounts) == 1
        and config_mounts[0].get("RW") is True
        and str(config_mounts[0].get("Source") or "") == expected_source
    )
    checks.append(
        Check(
            "lazylibrarian:persistence",
            persistence_ok,
            "only /config mounted; no download or media authority"
            if persistence_ok
            else "mount boundary must contain only the writable config directory",
        )
    )

    huey_environment = _container_environment(huey)
    owner_valid, _owner, _owner_detail = _strict_ebook_owner(environment)
    backends_valid, backends, _backends_detail = _strict_ebook_backends(
        environment
    )
    expected_huey_environment = {
        "EBOOK_ACQUISITION_BACKENDS": environment.get(
            EBOOK_BACKENDS_SETTING, ""
        ),
        "EBOOK_ACQUISITION_OWNER": environment.get(EBOOK_OWNER_SETTING, ""),
        "LAZYLIBRARIAN_ENABLED": "true",
        "LAZYLIBRARIAN_URL": "http://lazylibrarian:5299",
        "LAZYLIBRARIAN_API_KEY": environment.get("LAZYLIBRARIAN_API_KEY", ""),
        "LAZYLIBRARIAN_TIMEOUT_SECONDS": environment.get(
            "LAZYLIBRARIAN_TIMEOUT_SECONDS", "30"
        ),
        "LAZYLIBRARIAN_SEARCH_LIMIT": environment.get(
            "LAZYLIBRARIAN_SEARCH_LIMIT", "10"
        ),
        "LAZYLIBRARIAN_METADATA_SOURCE": environment.get(
            "LAZYLIBRARIAN_METADATA_SOURCE", "OpenLibrary"
        ),
        "HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE": environment.get(
            "HUEY_LAZYLIBRARIAN_MINIMUM_CONFIDENCE", "0.80"
        ),
        "HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP": environment.get(
            "HUEY_LAZYLIBRARIAN_RUNNER_UP_GAP", "0.05"
        ),
        "SHELFARR_ENABLED": "true",
        "SHELFARR_URL": "http://shelfarr",
        "SHELFARR_API_TOKEN": environment.get("SHELFARR_API_TOKEN", ""),
        "SHELFARR_TIMEOUT_SECONDS": environment.get(
            "SHELFARR_TIMEOUT_SECONDS", "20"
        ),
        "SHELFARR_SEARCH_LIMIT": environment.get(
            "SHELFARR_SEARCH_LIMIT", "10"
        ),
        "SHELFARR_LANGUAGE": environment.get("SHELFARR_LANGUAGE", "en"),
        "HUEY_SHELFARR_MINIMUM_CONFIDENCE": environment.get(
            "HUEY_SHELFARR_MINIMUM_CONFIDENCE", "0.80"
        ),
        "HUEY_SHELFARR_RUNNER_UP_GAP": environment.get(
            "HUEY_SHELFARR_RUNNER_UP_GAP", "0.05"
        ),
    }
    huey_ok = bool(
        owner_valid
        and backends_valid
        and backends == PRODUCTION_EBOOK_BACKENDS
        and environment.get("PROWLARR_API_KEY", "")
        and environment.get("SHELFARR_API_TOKEN", "")
        and secrets.compare_digest(
            huey_environment.get("PROWLARR_API_KEY", ""),
            environment.get("PROWLARR_API_KEY", ""),
        )
        and environment.get("LAZYLIBRARIAN_URL", "http://lazylibrarian:5299")
        == "http://lazylibrarian:5299"
        and environment.get("SHELFARR_URL", "http://shelfarr")
        == "http://shelfarr"
        and re.fullmatch(
            r"[0-9a-f]{32}", environment.get("LAZYLIBRARIAN_API_KEY", "")
        )
        and all(
            huey_environment.get(key) == value
            for key, value in expected_huey_environment.items()
        )
    )
    checks.append(
        Check(
            "huey:lazylibrarian-routing",
            huey_ok,
            "exact private ebook coordinator contract"
            if huey_ok
            else "Huey backend policy, private URL, feature flag, or API contract mismatch",
        )
    )
    return checks


def lazylibrarian_config_checks(
    path: Path, environment: dict[str, str]
) -> list[Check]:
    """Validate private LazyLibrarian secrets at rest without revealing them."""

    descriptor = -1
    content = ""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        permissions_ok = bool(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size <= MAX_PRIVATE_RESPONSE_BYTES
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            content = handle.read(MAX_PRIVATE_RESPONSE_BYTES + 1)
        permissions_ok = bool(
            permissions_ok
            and len(content.encode("utf-8")) <= MAX_PRIVATE_RESPONSE_BYTES
        )
    except (OSError, UnicodeError):
        permissions_ok = False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    checks = [
        Check(
            "lazylibrarian:config-permissions",
            permissions_ok,
            "owner-only regular file"
            if permissions_ok
            else "config.ini must be an owner-owned mode-0600 regular file",
        )
    ]

    if not permissions_ok:
        checks.append(
            Check(
                "lazylibrarian:config-secrets",
                False,
                "configuration unavailable or unsafe",
            )
        )
        return checks

    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    try:
        parser.read_string(content)
        folded_sections = [section.casefold() for section in parser.sections()]
        if len(folded_sections) != len(set(folded_sections)):
            raise configparser.Error("duplicate case-variant section")

        def value(section: str, key: str) -> str | None:
            matches = [
                current
                for current in parser.sections()
                if current.casefold() == section.casefold()
            ]
            if len(matches) != 1:
                return None
            return parser.get(matches[0], key, fallback=None)

        api_key = environment.get("LAZYLIBRARIAN_API_KEY", "")
        expected_qbittorrent_username = environment.get(
            "QBITTORRENT_USERNAME", ""
        )
        expected_qbittorrent_password = environment.get(
            "QBITTORRENT_PASSWORD", ""
        )
        stored_qbittorrent_username = value(
            "QBITTORRENT", "qbittorrent_user"
        )
        stored_qbittorrent_password = value(
            "QBITTORRENT", "qbittorrent_pass"
        )
        secrets_ok = bool(
            re.fullmatch(r"[0-9a-f]{32}", api_key)
            and value("API", "api_key") == api_key
            and expected_qbittorrent_username
            and expected_qbittorrent_password
            and isinstance(stored_qbittorrent_username, str)
            and isinstance(stored_qbittorrent_password, str)
            and secrets.compare_digest(
                stored_qbittorrent_username,
                expected_qbittorrent_username,
            )
            and secrets.compare_digest(
                stored_qbittorrent_password,
                expected_qbittorrent_password,
            )
        )
    except (OSError, UnicodeError, configparser.Error):
        secrets_ok = False

    checks.append(
        Check(
            "lazylibrarian:config-secrets",
            secrets_ok,
            "API and qBittorrent credentials are private and consistent"
            if secrets_ok
            else "credential values are missing, malformed, duplicated, or inconsistent",
        )
    )
    return checks


def _lazylibrarian_api_bytes(
    port: str,
    api_key: str,
    command: str,
    parameters: dict[str, str] | None = None,
    *,
    opener: object,
) -> bytes:
    """Issue one bounded read-only API call without a credential-bearing URL."""

    parsed_port = int(port)
    if not 1 <= parsed_port <= 65535:
        raise ValueError("invalid port")
    if re.fullmatch(r"[0-9a-f]{32}", api_key) is None:
        raise ValueError("invalid API credential")
    if command not in {"getVersion", "help", "listProviders", "readCFG"}:
        raise ValueError("command is not an approved read-only probe")
    parameters = dict(parameters or {})
    if command == "readCFG":
        if set(parameters) != {"group", "name"}:
            raise ValueError("readCFG requires an exact managed setting")
        group = parameters["group"]
        name = parameters["name"]
        if (
            re.fullmatch(r"[A-Z]+", group) is None
            or re.fullmatch(r"[A-Z0-9_]+", name) is None
            or group not in LAZYLIBRARIAN_EFFECTIVE_SETTINGS
            or name.casefold()
            not in LAZYLIBRARIAN_EFFECTIVE_SETTINGS[group]
        ):
            raise ValueError("readCFG setting is not an approved non-secret probe")
    elif parameters:
        raise ValueError("unexpected API probe parameters")
    body = urllib.parse.urlencode(
        {"apikey": api_key, "cmd": command, **parameters}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{parsed_port}/api",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "WyseARR-validator/1",
        },
        method="POST",
    )
    response = opener.open(request, timeout=30)
    try:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        payload = response.read(MAX_PRIVATE_RESPONSE_BYTES + 1)
    finally:
        close = getattr(response, "close", None)
        if close:
            close()
    if not 200 <= int(status) < 300 or len(payload) > MAX_PRIVATE_RESPONSE_BYTES:
        raise ValueError("invalid API response")
    return payload


def lazylibrarian_api_capability_check(
    port: str,
    api_key: str,
    *,
    opener: object | None = None,
) -> Check:
    """Probe only version/help and report no credential-bearing URL or response."""

    opener = opener or urllib.request.build_opener()
    try:
        version = json.loads(
            _lazylibrarian_api_bytes(
                port, api_key, "getVersion", opener=opener
            ).decode("utf-8")
        )
        help_text = _lazylibrarian_api_bytes(
            port, api_key, "help", opener=opener
        ).decode("utf-8", "replace")
        ok = bool(
            isinstance(version, dict)
            and version.get("Success") is True
            # This pinned LSIO build reports an empty current_version.  Accept
            # only that known omission or the expected source version; the
            # digest-pinned container check remains the identity authority.
            and version.get("current_version")
            in {"", EXPECTED_LAZYLIBRARIAN_VERSION}
            and all(command in help_text for command in LAZYLIBRARIAN_API_COMMANDS)
        )
    except Exception:
        ok = False
    return Check(
        "lazylibrarian:api-capabilities",
        ok,
        "required read-only API contract present; image digest pins the version"
        if ok
        else "version or required API capability mismatch",
    )


def _lazylibrarian_readcfg_value(payload: bytes) -> str:
    """Parse readCFG's bounded ``[value]`` envelope without rendering it."""

    rendered = payload.decode("utf-8").strip()
    if (
        len(rendered) < 2
        or not rendered.startswith("[")
        or not rendered.endswith("]")
        or any(ord(character) < 32 for character in rendered)
    ):
        raise ValueError("malformed readCFG response")
    return rendered[1:-1]


def _lazylibrarian_setting_matches(
    section: str, key: str, actual: str, expected: str
) -> bool:
    """Compare effective values across LL's bool and CSV serialization forms."""

    if (section, key) in LAZYLIBRARIAN_CSV_SETTINGS:
        def normalize_csv(value: str) -> tuple[str, ...]:
            return tuple(
                sorted(
                    item.strip().casefold()
                    for item in value.split(",")
                    if item.strip()
                )
            )

        return normalize_csv(actual) == normalize_csv(expected)
    if expected in {"0", "1"}:
        normalized = actual.strip().casefold()
        if normalized in {"", "0", "false"}:
            return expected == "0"
        if normalized in {"1", "true"}:
            return expected == "1"
    return actual == expected


def lazylibrarian_effective_config_check(
    port: str,
    api_key: str,
    *,
    opener: object | None = None,
) -> Check:
    """Read every managed non-secret value from LL without mutating or logging it."""

    opener = opener or urllib.request.build_opener()
    checked = 0
    mismatches = 0
    try:
        for section, section_values in LAZYLIBRARIAN_EFFECTIVE_SETTINGS.items():
            for key, expected in section_values.items():
                payload = _lazylibrarian_api_bytes(
                    port,
                    api_key,
                    "readCFG",
                    {"group": section, "name": key.upper()},
                    opener=opener,
                )
                actual = _lazylibrarian_readcfg_value(payload)
                checked += 1
                if not _lazylibrarian_setting_matches(
                    section, key, actual, expected
                ):
                    mismatches += 1
        expected_count = sum(
            len(section_values)
            for section_values in LAZYLIBRARIAN_EFFECTIVE_SETTINGS.values()
        )
        ok = checked == expected_count and mismatches == 0
    except Exception:
        ok = False
    return Check(
        "lazylibrarian:ebook-only-config",
        ok,
        (
            f"{checked} effective settings enforce ebook-only acquisition and BookBot finalization"
            if ok
            else "effective managed settings are unavailable, malformed, or mismatched"
        ),
    )


def _resource_tag_ids(resource: dict[str, object]) -> set[int] | None:
    values = resource.get("tags", [])
    if not isinstance(values, list):
        return None
    result: set[int] = set()
    for item in values:
        raw = item.get("id") if isinstance(item, dict) else item
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            return None
        if raw in result:
            return None
        result.add(raw)
    return result


def _lazylibrarian_category_ids(
    resource: dict[str, object],
) -> set[int] | None:
    """Strictly flatten one Prowlarr capability tree for ownership decisions."""

    capabilities = resource.get("capabilities")
    categories = (
        capabilities.get("categories")
        if isinstance(capabilities, dict)
        else None
    )
    if not isinstance(categories, list):
        return None
    pending = list(categories)
    result: set[int] = set()
    while pending:
        category = pending.pop()
        if not isinstance(category, dict):
            return None
        category_id = category.get("id")
        children = category.get("subCategories", [])
        if (
            isinstance(category_id, bool)
            or not isinstance(category_id, int)
            or category_id <= 0
            or not isinstance(children, list)
        ):
            return None
        # Prowlarr can expose the same numeric category under multiple tree
        # branches, so repeated category ids are valid and collapse here.
        result.add(category_id)
        pending.extend(children)
    return result


def _prowlarr_failed_indexer_ids(
    statuses: object,
) -> set[int] | None:
    """Treat every retained Prowlarr failure row as blocking, even if expired."""

    if not isinstance(statuses, list) or any(
        not isinstance(status, dict) for status in statuses
    ):
        return None
    blocked: set[int] = set()
    seen: set[int] = set()
    for status in statuses:
        indexer_id = status.get("indexerId")
        if (
            isinstance(indexer_id, bool)
            or not isinstance(indexer_id, int)
            or indexer_id <= 0
            or indexer_id in seen
        ):
            return None
        seen.add(indexer_id)
        has_failure = False
        for field in ("initialFailure", "mostRecentFailure", "disabledTill"):
            if field not in status:
                return None
            value = status[field]
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                return None
            try:
                parsed = datetime.fromisoformat(
                    value.strip().replace("Z", "+00:00")
                )
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            has_failure = True
        if has_failure:
            blocked.add(indexer_id)
    return blocked


def _ebook_indexer_contract(
    indexers: object, statuses: object
) -> dict[str, int] | None:
    """Return the available 7020-capable torrent indexers by name and id."""

    if not isinstance(indexers, list) or any(
        not isinstance(indexer, dict) for indexer in indexers
    ):
        return None
    blocked_ids = _prowlarr_failed_indexer_ids(statuses)
    if blocked_ids is None:
        return None
    seen_ids: set[int] = set()
    eligible: list[dict[str, object]] = []
    for indexer in indexers:
        indexer_id = indexer.get("id")
        categories = _lazylibrarian_category_ids(indexer)
        if (
            isinstance(indexer_id, bool)
            or not isinstance(indexer_id, int)
            or indexer_id <= 0
            or indexer_id in seen_ids
            or not isinstance(indexer.get("enable"), bool)
            or not isinstance(indexer.get("protocol"), str)
            or categories is None
            or _resource_tag_ids(indexer) is None
        ):
            return None
        seen_ids.add(indexer_id)
        if (
            indexer["enable"] is True
            and str(indexer["protocol"]).casefold() == "torrent"
            and 7020 in categories
        ):
            eligible.append(indexer)

    eligible_names = [
        str(indexer.get("name") or "").strip() for indexer in eligible
    ]
    if (
        not eligible
        or any(not name for name in eligible_names)
        or len({name.casefold() for name in eligible_names})
        != len(eligible_names)
    ):
        return None
    available = [
        indexer for indexer in eligible if indexer["id"] not in blocked_ids
    ]
    if not available:
        return None
    return {
        str(indexer["name"]).strip(): int(indexer["id"])
        for indexer in available
    }


def prowlarr_lazylibrarian_check(
    indexers: object,
    tags: object,
    applications: object,
    indexer_statuses: object,
) -> Check:
    """Validate the read-only Prowlarr side of the managed ebook sync."""

    managed_tags = [
        item
        for item in tags
        if isinstance(item, dict)
        if str(item.get("label") or "").casefold()
        == MANAGED_LAZYLIBRARIAN_TAG.casefold()
    ] if isinstance(tags, list) else []
    tag_id = managed_tags[0].get("id") if len(managed_tags) == 1 else None
    indexer_contract = _ebook_indexer_contract(indexers, indexer_statuses)
    safe_ids = set(indexer_contract.values()) if indexer_contract else set()
    indexers_ok = bool(
        isinstance(tag_id, int)
        and not isinstance(tag_id, bool)
        and tag_id > 0
        and indexer_contract
        and isinstance(indexers, list)
        and all(
            _resource_tag_ids(item) is not None
            and (
                (tag_id in (_resource_tag_ids(item) or set()))
                == (item.get("id") in safe_ids)
            )
            for item in indexers
        )
    )

    matching_apps = [
        item
        for item in applications
        if isinstance(item, dict)
        if str(item.get("name") or "").casefold()
        == MANAGED_LAZYLIBRARIAN_APPLICATION.casefold()
        or str(item.get("implementation") or "").casefold() == "lazylibrarian"
    ] if isinstance(applications, list) else []
    application = matching_apps[0] if len(matching_apps) == 1 else None
    raw_fields = application.get("fields") if isinstance(application, dict) else None
    field_names = (
        [
            str(field.get("name") or "").casefold()
            for field in raw_fields
            if isinstance(field, dict) and field.get("name")
        ]
        if isinstance(raw_fields, list)
        else []
    )
    fields_shape_ok = bool(
        isinstance(raw_fields, list)
        and len(field_names) == len(raw_fields)
        and len(field_names) == len(set(field_names))
    )
    fields = provider_fields(application) if isinstance(application, dict) else {}
    raw_categories = fields.get("synccategories")
    sync_categories = (
        frozenset(raw_categories)
        if isinstance(raw_categories, list)
        and len(raw_categories) == 1
        and raw_categories[0] == 7020
        and not isinstance(raw_categories[0], bool)
        else frozenset()
    )
    app_tags = _resource_tag_ids(application) if isinstance(application, dict) else None
    profile_id = application.get("appProfileId") if isinstance(application, dict) else None
    application_ok = bool(
        isinstance(application, dict)
        and fields_shape_ok
        and application.get("name") == MANAGED_LAZYLIBRARIAN_APPLICATION
        and application.get("enable") is True
        and str(application.get("implementation") or "").casefold()
        == "lazylibrarian"
        and str(application.get("configContract") or "").casefold()
        == "lazylibrariansettings"
        and application.get("syncLevel") == "fullSync"
        # Prowlarr 2.5.2 canonicalizes every application profile reference to
        # null in its persisted API resource. Require that observed contract;
        # accepting an invented numeric profile would mask topology drift.
        and profile_id is None
        and isinstance(tag_id, int)
        and app_tags == {tag_id}
        and fields.get("prowlarrurl") == "http://prowlarr:9696"
        and fields.get("baseurl") == "http://lazylibrarian:5299"
        and bool(str(fields.get("apikey") or "").strip())
        and not str(fields.get("authusername") or "")
        and not str(fields.get("authpassword") or "")
        and sync_categories == LAZYLIBRARIAN_SYNC_CATEGORIES
    )
    ok = indexers_ok and application_ok
    return Check(
        "prowlarr:lazylibrarian",
        ok,
        f"full-sync 7020 application and tag isolate {len(safe_ids)} available torrent indexers"
        if ok
        else "managed application, retained failure, tag isolation, or 7020 sync mismatch",
    )


def _canonical_provider_categories(
    value: object, *, allow_empty: bool = False
) -> tuple[str, ...] | None:
    if not isinstance(value, str):
        return None
    if value == "":
        return () if allow_empty else None
    if re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", value) is None:
        return None
    categories = tuple(value.split(","))
    if (
        len(set(categories)) != len(categories)
        or tuple(sorted(categories, key=int)) != categories
    ):
        return None
    return categories


def lazylibrarian_provider_check(
    port: str,
    api_key: str,
    expected_prowlarr_api_key: str,
    expected_indexers: dict[str, int],
    *,
    opener: object | None = None,
) -> Check:
    """Require exactly Prowlarr's ebook Torznab providers, without printing keys."""

    opener = opener or urllib.request.build_opener()
    try:
        providers = json.loads(
            _lazylibrarian_api_bytes(
                port, api_key, "listProviders", opener=opener
            ).decode("utf-8")
        )
        provider_types = ("newznab", "torznab", "rss", "irc", "torrent", "direct")
        arrays_ok = bool(
            isinstance(providers, dict)
            and all(
                isinstance(providers.get(provider_type), list)
                and all(
                    isinstance(item, dict)
                    for item in providers.get(provider_type, [])
                )
                for provider_type in provider_types
            )
        )
        if not arrays_ok:
            raise ValueError("invalid provider response")

        def enabled(value: object) -> bool:
            return value in {True, 1, "1", "true", "True"}

        active = [
            (provider_type, provider)
            for provider_type in provider_types
            for provider in providers[provider_type]
            if enabled(provider.get("ENABLED"))
        ]

        def provider_name(provider: dict[str, object]) -> str:
            name = str(provider.get("DISPNAME") or "").strip()
            suffix = " (prowlarr)"
            return (
                name[: -len(suffix)].strip()
                if name.casefold().endswith(suffix)
                else name
            )

        expected_folded = {
            name.casefold(): indexer_id
            for name, indexer_id in expected_indexers.items()
            if name.strip()
            and isinstance(indexer_id, int)
            and not isinstance(indexer_id, bool)
            and indexer_id > 0
        }
        active_names = [
            provider_name(provider).casefold() for _, provider in active
        ]
        providers_ok = bool(
            expected_prowlarr_api_key
            and expected_indexers
            and len(expected_folded) == len(expected_indexers)
            and len(set(expected_folded.values())) == len(expected_folded)
            and len(active) == len(expected_folded)
            and len(set(active_names)) == len(active_names)
            and set(active_names) == set(expected_folded)
            and all(provider_type == "torznab" for provider_type, _ in active)
        )
        for _provider_type, provider in active:
            name = provider_name(provider).casefold()
            expected_id = expected_folded.get(name)
            parsed = urllib.parse.urlsplit(str(provider.get("HOST") or ""))
            providers_ok = bool(
                providers_ok
                and isinstance(provider.get("API"), str)
                and secrets.compare_digest(
                    provider["API"], expected_prowlarr_api_key
                )
                and parsed.scheme == "http"
                and parsed.netloc == "prowlarr:9696"
                and parsed.path == f"/{expected_id}/api"
                and not parsed.query
                and not parsed.fragment
                and _canonical_provider_categories(provider.get("BOOKCAT"))
                == ("7020",)
                and str(provider.get("DLTYPES") or "") == "E"
                and enabled(provider.get("MANUAL"))
                and all(
                    _canonical_provider_categories(
                        provider.get(key), allow_empty=True
                    )
                    is not None
                    for key in ("AUDIOCAT", "MAGCAT", "COMICCAT")
                )
            )
        ok = providers_ok
    except Exception:
        ok = False
    return Check(
        "lazylibrarian:providers",
        ok,
        f"{len(expected_indexers)} manual 7020-only Prowlarr Torznab providers"
        if ok
        else "provider presence, credential, protocol, host, or ebook category boundary mismatch",
    )


def ebook_category_ownership_check(
    categories: dict[str, object], _owner: str
) -> Check:
    """Require qBittorrent's ebook category phases to share one exact path."""

    paths_ok = bool(
        isinstance(categories.get("ebooks"), dict)
        and categories["ebooks"].get("savePath") == "/downloads/ebooks"
        and isinstance(categories.get("ebooks-imported"), dict)
        and categories["ebooks-imported"].get("savePath")
        == "/downloads/ebooks"
    )
    return Check(
        "ebooks:category-ownership",
        paths_ok,
        "ebooks and ebooks-imported share exactly /downloads/ebooks"
        if paths_ok
        else "ebooks and ebooks-imported must share only /downloads/ebooks",
    )


def abba_configuration_checks(
    environment: dict[str, str], *, runner: object = subprocess.run
) -> list[Check]:
    """Validate ABBA's private, least-authority production boundary."""

    abba = _inspect_service(ABBA_SERVICE, runner=runner)
    huey = _inspect_service("huey", runner=runner)
    if abba is None:
        return [Check("abba:configuration", False, "container unavailable")]

    abba_environment = _container_environment(abba)
    expected_environment = {
        "DOWNLOAD_CLIENT": "qbittorrent",
        "DL_SCHEME": "http",
        "DL_HOST": "qbittorrent",
        "DL_PORT": "8080",
        "DL_CATEGORY": "audiobooks",
        "SAVE_PATH_BASE": "/downloads/audiobooks",
        "DL_VERIFY_TLS": "true",
        "ABBA_DB_PATH": "/config/abba.db",
        "ABB_HOSTNAME": environment.get(
            "ABBA_ABB_HOSTNAME", "audiobookbay.lu"
        ),
        "PAGE_LIMIT": "1",
        "PORT": "5078",
        "ABBA_SEARCH_CACHE_SECONDS": environment.get(
            "ABBA_SEARCH_CACHE_SECONDS", "300"
        ),
        "ABBA_SEARCH_MIN_INTERVAL_SECONDS": environment.get(
            "ABBA_SEARCH_MIN_INTERVAL_SECONDS", "2"
        ),
        "ABBA_RESULT_TTL_SECONDS": environment.get(
            "ABBA_RESULT_TTL_SECONDS", "86400"
        ),
        "ABBA_HTTP_TIMEOUT_SECONDS": environment.get(
            "ABBA_HTTP_TIMEOUT_SECONDS", "15"
        ),
        "ABBA_MAX_RESULTS": "10",
    }
    configuration_ok = bool(
        expected_environment["ABB_HOSTNAME"]
        and all(
            abba_environment.get(key) == value
            for key, value in expected_environment.items()
        )
        and abba_environment.get("DL_USERNAME")
        == environment.get("QBITTORRENT_USERNAME", "admin")
        and not abba_environment.get("DL_URL")
        and bool(environment.get("QBITTORRENT_PASSWORD", ""))
        and isinstance(abba_environment.get("DL_PASSWORD"), str)
        and secrets.compare_digest(
            abba_environment.get("DL_PASSWORD", ""),
            environment.get("QBITTORRENT_PASSWORD", ""),
        )
    )
    checks = [
        Check(
            "abba:configuration",
            configuration_ok,
            "private AudioBookBay/qBittorrent adapter configured"
            if configuration_ok
            else "adapter environment or downloader ownership mismatch",
        )
    ]

    host_config = abba.get("HostConfig") if isinstance(abba, dict) else None
    config = abba.get("Config") if isinstance(abba, dict) else None
    host_config = host_config if isinstance(host_config, dict) else {}
    config = config if isinstance(config, dict) else {}
    security_options = {str(value) for value in host_config.get("SecurityOpt", []) or []}
    dropped = {str(value).upper() for value in host_config.get("CapDrop", []) or []}
    restart_policy = host_config.get("RestartPolicy")
    restart_name = (
        restart_policy.get("Name") if isinstance(restart_policy, dict) else None
    )
    expected_user = "1000:1000"
    tmpfs = host_config.get("Tmpfs")
    security_ok = bool(
        config.get("User") == expected_user
        and environment.get("PUID", "1000") == "1000"
        and environment.get("PGID", "1000") == "1000"
        and host_config.get("ReadonlyRootfs") is True
        and host_config.get("Privileged") is False
        and "ALL" in dropped
        and any(value.startswith("no-new-privileges") for value in security_options)
        and isinstance(tmpfs, dict)
        and "/tmp" in tmpfs
        and restart_name == "unless-stopped"
    )
    checks.append(
        Check(
            "abba:security",
            security_ok,
            "non-root, read-only, capability-free container"
            if security_ok
            else "container user, read-only root, capabilities, tmpfs, or restart policy mismatch",
        )
    )

    mounts = abba.get("Mounts", []) if isinstance(abba, dict) else []
    config_mounts = [
        mount
        for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == "/config"
    ]
    expected_source = str((STACK_ROOT / "config" / "abba").resolve())
    persistence_ok = bool(
        len(config_mounts) == 1
        and config_mounts[0].get("RW") is True
        and str(config_mounts[0].get("Source") or "") == expected_source
        and not any(
            mount.get("RW") is True
            and str(mount.get("Destination") or "") != "/config"
            for mount in mounts
            if isinstance(mount, dict)
        )
    )
    checks.append(
        Check(
            "abba:persistence",
            persistence_ok,
            "only /config is persistently writable; no payload mount"
            if persistence_ok
            else "persistent config or filesystem authority mismatch",
        )
    )

    huey_environment = _container_environment(huey)
    expected_url = environment.get("ABBA_URL", "http://abba:5078")
    expected_huey_environment = {
        "ABBA_URL": expected_url,
        "ABBA_TIMEOUT_SECONDS": environment.get("ABBA_TIMEOUT_SECONDS", "30"),
        "ABBA_SEARCH_LIMIT": environment.get("ABBA_SEARCH_LIMIT", "10"),
        "HUEY_ABBA_MINIMUM_CONFIDENCE": environment.get(
            "HUEY_ABBA_MINIMUM_CONFIDENCE", "0.82"
        ),
        "HUEY_ABBA_RUNNER_UP_GAP": environment.get(
            "HUEY_ABBA_RUNNER_UP_GAP", "0.08"
        ),
    }
    huey_ok = bool(
        huey_environment.get("ABBA_ENABLED") == "true"
        and expected_url == "http://abba:5078"
        and all(
            huey_environment.get(key) == value
            for key, value in expected_huey_environment.items()
        )
    )
    checks.append(
        Check(
            "huey:abba-routing",
            huey_ok,
            "audiobooks owned by private ABBA endpoint"
            if huey_ok
            else "Huey ABBA feature flag or private URL mismatch",
        )
    )
    return checks


def abba_database_check(path: Path) -> Check:
    """Require durable canonical ABBA owners for every acquired identity."""

    try:
        if not path.is_file() or path.is_symlink():
            raise OSError("unsafe database path")
        with closing(_open_readonly_database(path)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            acquisition_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(acquisitions)")
            }
            acquisition_primary_key = tuple(
                str(row[1])
                for row in sorted(
                    (
                        row
                        for row in connection.execute(
                            "PRAGMA table_info(acquisitions)"
                        )
                        if int(row[5]) > 0
                    ),
                    key=lambda row: int(row[5]),
                )
            )
            acquisition_indexes = _sqlite_indexes(connection, "acquisitions")
            hash_owner_index_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'acquisitions_hash_owner_uq'"
            ).fetchone()
            hash_owner_index_sql = (
                "".join(str(hash_owner_index_row[0] or "").casefold().split())
                if hash_owner_index_row
                else ""
            )
            candidate_owner_index_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'acquisitions_candidate_owner_uq'"
            ).fetchone()
            candidate_owner_index_sql = (
                "".join(
                    str(candidate_owner_index_row[0] or "").casefold().split()
                )
                if candidate_owner_index_row
                else ""
            )
            schema_ok = bool(
                {
                    "correlation_id",
                    "candidate_id",
                    "info_hash",
                    "category",
                    "save_path",
                    "tag",
                    "state",
                    "error_code",
                    "error_retryable",
                    "error_http_status",
                    "canonical_correlation_id",
                    "canonical_candidate_correlation_id",
                    "mutation_started_at",
                    "created_at",
                    "updated_at",
                }
                <= acquisition_columns
                and acquisition_primary_key == ("correlation_id",)
                and acquisition_indexes.get("acquisitions_hash_owner_uq")
                == (True, ("info_hash",))
                and "onacquisitions(info_hash)" in hash_owner_index_sql
                and hash_owner_index_sql.partition("where")[2]
                == "info_hashisnotnull"
                "andcanonical_correlation_idisnulland("
                "state!='failed'ormutation_started_atisnotnull)"
                and acquisition_indexes.get("acquisitions_candidate_owner_uq")
                == (True, ("candidate_id",))
                and "onacquisitions(candidate_id)" in candidate_owner_index_sql
                and candidate_owner_index_sql.partition("where")[2]
                == "canonical_candidate_correlation_idisnulland("
                "state!='failed'ormutation_started_atisnotnull)"
                and acquisition_indexes.get("acquisitions_canonical_idx")
                == (False, ("canonical_correlation_id",))
                and acquisition_indexes.get(
                    "acquisitions_candidate_canonical_idx"
                )
                == (False, ("canonical_candidate_correlation_id",))
            )

            ownership_violations = 0
            if {
                "correlation_id",
                "info_hash",
                "state",
                "canonical_correlation_id",
                "canonical_candidate_correlation_id",
                "mutation_started_at",
            } <= acquisition_columns:
                duplicate_hash_owners = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM acquisitions
                        WHERE info_hash IS NOT NULL
                          AND canonical_correlation_id IS NULL
                          AND (
                              state != 'failed'
                              OR mutation_started_at IS NOT NULL
                          )
                        GROUP BY lower(info_hash)
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
                duplicate_candidate_owners = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM acquisitions
                        WHERE canonical_candidate_correlation_id IS NULL
                          AND (
                              state != 'failed'
                              OR mutation_started_at IS NOT NULL
                          )
                        GROUP BY candidate_id
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
                invalid_hash_aliases = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM acquisitions AS alias
                    LEFT JOIN acquisitions AS canonical
                      ON canonical.correlation_id =
                         alias.canonical_correlation_id
                    WHERE alias.canonical_correlation_id IS NOT NULL
                      AND (
                          canonical.correlation_id IS NULL
                          OR canonical.canonical_correlation_id IS NOT NULL
                          OR alias.info_hash IS NULL
                          OR canonical.info_hash IS NULL
                          OR lower(alias.info_hash) != lower(canonical.info_hash)
                          OR (
                              canonical.state = 'failed'
                              AND canonical.mutation_started_at IS NULL
                          )
                          OR (
                              canonical.state = 'prepared'
                              AND canonical.mutation_started_at IS NULL
                          )
                      )
                    """
                ).fetchone()[0]
                invalid_candidate_aliases = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM acquisitions AS alias
                    LEFT JOIN acquisitions AS candidate_root
                      ON candidate_root.correlation_id =
                         alias.canonical_candidate_correlation_id
                    WHERE alias.canonical_candidate_correlation_id IS NOT NULL
                      AND (
                          candidate_root.correlation_id IS NULL
                          OR candidate_root.canonical_candidate_correlation_id
                             IS NOT NULL
                          OR alias.candidate_id IS NOT candidate_root.candidate_id
                          OR (
                              candidate_root.state = 'failed'
                              AND candidate_root.mutation_started_at IS NULL
                          )
                          OR (
                              candidate_root.state = 'prepared'
                              AND candidate_root.mutation_started_at IS NULL
                              AND candidate_root.canonical_correlation_id IS NULL
                          )
                          OR (
                              alias.info_hash IS NOT NULL
                              AND alias.info_hash IS candidate_root.info_hash
                              AND alias.canonical_correlation_id IS NOT
                                  COALESCE(
                                      candidate_root.canonical_correlation_id,
                                      candidate_root.correlation_id
                                  )
                          )
                          OR (
                              alias.info_hash IS NOT candidate_root.info_hash
                              AND (
                                  alias.state IS NOT 'failed'
                                  OR alias.error_code IS NOT 'result_changed'
                                  OR alias.error_retryable IS NOT 0
                                  OR alias.error_http_status IS NOT 409
                              )
                          )
                      )
                    """
                ).fetchone()[0]
                ownership_violations = sum(
                    int(value)
                    for value in (
                        duplicate_hash_owners,
                        duplicate_candidate_owners,
                        invalid_hash_aliases,
                        invalid_candidate_aliases,
                    )
                )
        ok = integrity == "ok" and schema_ok and ownership_violations == 0
    except (OSError, sqlite3.Error, TypeError):
        ok = False
        ownership_violations = 0
    return Check(
        "abba:database",
        ok,
        "integrity, canonical hash/candidate schema, and alias ownership valid; "
        "violations=0"
        if ok
        else "database missing, unsafe, corrupt, or canonical ownership invalid; "
        f"violations={ownership_violations}",
    )


def abba_api_readiness_check(*, runner: object = subprocess.run) -> Check:
    """Verify Huey can reach ABBA readiness without invoking an ABB search."""

    probe = (
        "import json,urllib.request;"
        "response=urllib.request.urlopen('http://abba:5078/health',timeout=10);"
        "payload=json.load(response);"
        "assert isinstance(payload,dict);"
        "assert payload.get('status')=='ok';"
        "assert payload.get('service')=='abba';"
        "checks=payload.get('checks');"
        "assert isinstance(checks,dict);"
        "assert all(checks.get(key)=='ok' for key in "
        "('database','qbittorrent','category','save_path'));"
        "print('ready')"
    )
    try:
        result = runner(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "huey",
                "python",
                "-B",
                "-c",
                probe,
            ],
            cwd=STACK_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        ok = result.returncode == 0 and result.stdout.strip().splitlines()[-1:] == [
            "ready"
        ]
    except (OSError, subprocess.SubprocessError):
        ok = False
    return Check(
        "abba:api-readiness",
        ok,
        "Huey-to-ABBA readiness passed without AudioBookBay search"
        if ok
        else "private readiness API unavailable or unhealthy",
    )


def evaluation_report_permissions_check(path: Path) -> Check:
    """Protect benchmark request titles and outcomes from other host users."""

    try:
        directory = path.stat()
        reports = list(path.glob("*.json"))
        ok = bool(
            path.is_dir()
            and not path.is_symlink()
            and directory.st_uid == os.getuid()
            and stat.S_IMODE(directory.st_mode) == 0o700
            and reports
            and all(
                report.is_file()
                and not report.is_symlink()
                and report.stat().st_uid == os.getuid()
                and stat.S_IMODE(report.stat().st_mode) == 0o600
                for report in reports
            )
        )
    except OSError:
        ok = False
    return Check(
        "shelfarr:evaluation-report-permissions",
        ok,
        "private reports" if ok else "directory/reports must be owner-only",
    )


def shelfarr_direct_staging_check(path: Path) -> Check:
    """Require Shelfarr's CIFS-incompatible private staging on local storage."""

    try:
        info = path.stat()
        ok = bool(
            path.is_dir()
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700
            and os.access(path, os.W_OK | os.X_OK)
        )
    except OSError:
        ok = False
    return Check(
        "shelfarr:direct-ebook-staging",
        ok,
        "local owner-only staging"
        if ok
        else "local staging must be writable, owned by the service user, and mode 0700",
    )


def shelfarr_configuration_checks(
    storage: Path, environment: dict[str, str]
) -> list[Check]:
    """Validate the live evaluation boundary without decrypting stored secrets."""

    checks: list[Check] = []
    try:
        with closing(_open_readonly_database(storage / "production.sqlite3")) as connection:
            settings = {
                str(row[0]): row[1]
                for row in connection.execute("SELECT key, value FROM settings")
            }
            clients = connection.execute(
                "SELECT client_type, url, category, download_path, enabled, "
                "password, api_key "
                "FROM download_clients"
            ).fetchall()
            token = environment.get("SHELFARR_API_TOKEN", "")
            token_row = connection.execute(
                "SELECT api_tokens.scopes FROM api_tokens "
                "JOIN users ON users.id = api_tokens.user_id "
                "WHERE token_digest = ? AND revoked_at IS NULL "
                "AND users.deleted_at IS NULL AND users.role = 0 "
                "AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))",
                (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return [Check("shelfarr:configuration", False, "database unavailable")]

    stored_prowlarr_key = settings.get("prowlarr_api_key")
    expected_prowlarr_key = environment.get("PROWLARR_API_KEY", "")
    indexer_ok = bool(
        settings.get("indexer_provider") == "prowlarr"
        and str(settings.get("prowlarr_url") or "").rstrip("/")
        == "http://prowlarr:9696"
        and isinstance(stored_prowlarr_key, str)
        and expected_prowlarr_key
        and secrets.compare_digest(
            stored_prowlarr_key, expected_prowlarr_key
        )
    )
    checks.append(
        Check(
            "shelfarr:prowlarr",
            indexer_ok,
            "internal provider credential matches the private environment"
            if indexer_ok
            else "provider endpoint or credential mismatch",
        )
    )

    flag_valid, usenet_enabled, _flag_detail = _strict_feature_flag(
        environment, USENET_FEATURE_FLAG
    )
    expected_clients = {
        "qbittorrent": ("http://qbittorrent:8080", "/downloads/shelfarr"),
        "sabnzbd": ("http://sabnzbd:8080", "/downloads/usenet"),
    }
    configured_clients: set[str] = set()
    client_states: dict[str, list[bool]] = {}
    for row in clients:
        client_type = str(row["client_type"] or "")
        if client_type in expected_clients:
            client_states.setdefault(client_type, []).append(
                _database_truthy(row["enabled"])
            )
        if (
            _database_truthy(row["enabled"])
            and client_type in expected_clients
            and str(row["url"] or "").rstrip("/") == expected_clients[client_type][0]
            and row["category"] == SHELFARR_DOWNLOAD_CATEGORY
            and row["download_path"] == expected_clients[client_type][1]
            and (
                bool(row["api_key"])
                if client_type == "sabnzbd"
                else bool(row["password"])
            )
        ):
            configured_clients.add(client_type)
    expected_enabled = {"qbittorrent"} | ({"sabnzbd"} if usenet_enabled else set())
    client_rows_ok = bool(
        all(len(client_states.get(name, [])) == 1 for name in expected_clients)
        and client_states.get("qbittorrent") == [True]
        and client_states.get("sabnzbd") == [usenet_enabled]
    )
    clients_ok = bool(
        flag_valid and client_rows_ok and configured_clients == expected_enabled
    )
    checks.append(
        Check(
            "shelfarr:download-clients",
            clients_ok,
            "qBittorrent isolated; SABnzbd follows the Usenet feature flag"
            if clients_ok
            else "missing, disabled, or not using category shelfarr",
        )
    )

    try:
        preferred_types = json.loads(
            str(settings.get("preferred_download_types") or "[]")
        )
    except json.JSONDecodeError:
        preferred_types = []
    expected_order = (
        ["direct", "usenet", "torrent"]
        if usenet_enabled
        else ["direct", "torrent"]
    )
    order_ok = flag_valid and preferred_types == expected_order
    checks.append(
        Check(
            "shelfarr:acquisition-order",
            order_ok,
            ", ".join(expected_order)
            if order_ok
            else "acquisition order does not match the Usenet feature flag",
        )
    )

    search_scope_ok = settings.get("prowlarr_tags") == ""
    checks.append(
        Check(
            "shelfarr:indexer-scope",
            search_scope_ok,
            "all Prowlarr fallback protocols visible"
            if search_scope_ok
            else "Prowlarr tag filter would hide an acquisition source",
        )
    )

    paths_ok = bool(
        settings.get("ebook_output_path") == "/ebooks"
        and settings.get("download_local_path") == "/downloads"
    )
    checks.append(
        Check(
            "shelfarr:paths",
            paths_ok,
            "ebook download and final-library paths mapped"
            if paths_ok
            else "ebook output or download path mismatch",
        )
    )

    automation_ok = bool(
        _database_truthy(settings.get("immediate_search_enabled"))
        and _database_truthy(settings.get("auto_approve_requests"))
        and _database_truthy(settings.get("auto_select_enabled"))
        and str(settings.get("auto_select_confidence_threshold")) == "90"
        and str(settings.get("auto_select_min_seeders")) == "1"
        and settings.get("completed_download_import_mode") == "copy"
        and settings.get("default_language") == "en"
        and not _database_truthy(settings.get("auth_disabled"))
    )
    checks.append(
        Check(
            "shelfarr:automation",
            automation_ok,
            "auto-approved search, selection policy, and copy import enabled"
            if automation_ok
            else "automation, selection, language, or import policy mismatch",
        )
    )

    direct_ok = bool(
        not _database_truthy(settings.get("librivox_enabled"))
        and _database_truthy(settings.get("gutenberg_enabled"))
        and not _database_truthy(settings.get("anna_archive_enabled"))
        and not _database_truthy(settings.get("zlibrary_enabled"))
        and not _database_truthy(settings.get("ebooks_com_enabled"))
    )
    checks.append(
        Check(
            "shelfarr:direct-sources",
            direct_ok,
            "Gutenberg enabled; CIFS-incompatible and credentialed sources disabled"
            if direct_ok
            else "direct-source evaluation policy mismatch",
        )
    )

    outbound_disabled = bool(
        not _database_truthy(settings.get("discord_enabled"))
        and not str(settings.get("discord_webhook_url") or "").strip()
        and not _database_truthy(settings.get("webhook_enabled"))
        and not str(settings.get("webhook_url") or "").strip()
        and not _database_truthy(settings.get("telegram_enabled"))
    )
    checks.append(
        Check(
            "shelfarr:outbound-notifications",
            outbound_disabled,
            "Discord, webhook, and Telegram disabled"
            if outbound_disabled
            else "Shelfarr outbound notification channel enabled or configured",
        )
    )

    required_scopes = {"search:read", "requests:read", "requests:write"}
    try:
        token_scopes = set(json.loads(str(token_row[0]))) if token_row else set()
    except (json.JSONDecodeError, TypeError):
        token_scopes = set()
    token_ok = token.startswith("shf_") and token_scopes == required_scopes
    checks.append(
        Check(
            "shelfarr:huey-token",
            token_ok,
            "active least-privilege token"
            if token_ok
            else "token missing, inactive, or lacks required scopes",
        )
    )
    return checks


def _ini_section_values(path: Path, section: str) -> dict[str, str]:
    """Read scalar values from one top-level INI section without its secrets."""

    values: dict[str, str] = {}
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().casefold()
            continue
        if current == section.casefold() and "=" in line:
            key, value = line.split("=", 1)
            scalar = value.strip()
            if (
                len(scalar) >= 2
                and scalar[0] == scalar[-1]
                and scalar[0] in {"'", '"'}
            ):
                scalar = scalar[1:-1]
            values[key.strip().casefold()] = scalar
    return values


def sabnzbd_configuration_checks(
    config_path: Path,
    port: str,
    expected_username: str = "",
    environment: dict[str, str] | None = None,
    *,
    requester: object = request_json,
    server_tester: object = sabnzbd_post_json,
) -> list[Check]:
    """Validate isolated SAB paths/API and the feature-gated NNTP provider."""

    environment = dict(environment or {})

    try:
        misc = _ini_section_values(config_path, "misc")
    except (OSError, UnicodeError):
        return [Check("sabnzbd:configuration", False, "configuration unavailable")]

    paths_ok = bool(
        misc.get("download_dir") == "/downloads/incomplete/usenet"
        and misc.get("complete_dir") == "/downloads/usenet"
    )
    checks = [
        Check(
            "sabnzbd:paths",
            paths_ok,
            "isolated incomplete and complete paths"
            if paths_ok
            else "download paths do not match evaluation mounts",
        )
    ]
    auth_ok = bool(
        expected_username
        and misc.get("username") == expected_username
        and str(misc.get("password") or "").strip()
        and not _database_truthy(misc.get("api_logging"))
    )
    checks.append(
        Check(
            "sabnzbd:authentication",
            auth_ok,
            "operator authentication configured"
            if auth_ok
            else "operator auth missing/mismatched or API parameter logging enabled",
        )
    )

    api_key = misc.get("api_key", "")
    try:
        base_url = f"http://127.0.0.1:{int(port)}"
        encoded_key = urllib.parse.quote(api_key, safe="")
        version_payload = requester(
            f"{base_url}/api?mode=version&output=json&apikey={encoded_key}"
        )
        categories_payload = requester(
            f"{base_url}/api?mode=get_cats&output=json&apikey={encoded_key}"
        )
        servers_payload = requester(
            f"{base_url}/api?mode=get_config&section=servers&output=json&apikey={encoded_key}"
        )
        raw_servers = (
            servers_payload.get("config", {}).get("servers", [])
            if isinstance(servers_payload, dict)
            else []
        )
        servers = (
            list(raw_servers.values())
            if isinstance(raw_servers, dict)
            else raw_servers
        )
        enabled_servers = [
            server
            for server in servers
            if isinstance(server, dict)
            and _database_truthy(server.get("enable"))
            and bool(str(server.get("host") or "").strip())
        ]
        enabled_server = bool(enabled_servers)
        managed_servers = [
            server
            for server in servers
            if isinstance(server, dict)
            and str(server.get("name") or "").casefold()
            == MANAGED_SABNZBD_SERVER.casefold()
        ]
        managed_server = managed_servers[0] if len(managed_servers) == 1 else None
        categories = (
            categories_payload.get("categories", [])
            if isinstance(categories_payload, dict)
            else []
        )
        api_ok = bool(
            api_key
            and isinstance(version_payload, dict)
            and version_payload.get("version")
            and isinstance(categories, list)
            and SHELFARR_DOWNLOAD_CATEGORY in categories
        )
        detail = (
            f"version={version_payload.get('version')} category=shelfarr"
            if api_ok
            else "API unavailable or Shelfarr category missing"
        )
    except (TypeError, ValueError, OSError, urllib.error.URLError):
        api_ok = False
        enabled_server = False
        managed_server = None
        managed_servers = []
        detail = "API unavailable or Shelfarr category missing"
    checks.append(Check("sabnzbd:api-category", api_ok, detail))
    checks.append(
        Check(
            "sabnzbd:provider-observation",
            True,
            "enabled Usenet provider configured"
            if enabled_server
            else "no enabled provider; Usenet acquisition unavailable",
        )
    )

    flag_valid, usenet_enabled, flag_detail = _strict_feature_flag(
        environment, USENET_FEATURE_FLAG
    )
    if not flag_valid:
        checks.append(
            Check(
                "sabnzbd:usenet-provider",
                False,
                "provider validation blocked by invalid feature flag",
            )
        )
        return checks

    if not usenet_enabled:
        provider_disabled = len(managed_servers) <= 1 and not (
            isinstance(managed_server, dict)
            and _database_truthy(managed_server.get("enable"))
        )
        checks.append(
            Check(
                "sabnzbd:usenet-provider",
                provider_disabled,
                "disabled"
                if provider_disabled
                else "managed NNTP provider is enabled while feature is disabled",
            )
        )
        return checks

    required = (
        "USENET_SERVER_HOST",
        "USENET_SERVER_USERNAME",
        "USENET_SERVER_PASSWORD",
        "USENET_SERVER_CONNECTIONS",
    )
    missing = [key for key in required if not environment.get(key, "").strip()]
    ssl_value = environment.get("USENET_SERVER_SSL", "true").strip()
    try:
        provider_port = int(environment.get("USENET_SERVER_PORT", "563"))
        connections = int(environment.get("USENET_SERVER_CONNECTIONS", ""))
        retention = int(environment.get("USENET_SERVER_RETENTION", "0"))
        numeric_ok = 1 <= provider_port <= 65535 and 1 <= connections <= 500 and retention >= 0
    except (TypeError, ValueError):
        provider_port = 0
        connections = 0
        retention = -1
        numeric_ok = False
    provider_env_ok = not missing and ssl_value == "true" and numeric_ok
    ssl_enabled = True

    live_ok = False
    if provider_env_ok and isinstance(managed_server, dict):
        try:
            password_echo = str(managed_server.get("password") or "")
            live_ok = bool(
                _database_truthy(managed_server.get("enable"))
                and str(managed_server.get("displayname") or "")
                == MANAGED_SABNZBD_SERVER
                and str(managed_server.get("host") or "").casefold()
                == environment["USENET_SERVER_HOST"].strip().casefold()
                and int(managed_server.get("port", 0)) == provider_port
                and str(managed_server.get("username") or "")
                == environment["USENET_SERVER_USERNAME"]
                and int(managed_server.get("connections", 0)) == connections
                and _database_truthy(managed_server.get("ssl")) == ssl_enabled
                and int(managed_server.get("ssl_verify", -1)) == 3
                and int(managed_server.get("retention", -1)) == retention
                and int(managed_server.get("priority", -1)) == 0
                and password_echo
                and not password_echo.strip("*")
            )
        except (TypeError, ValueError):
            live_ok = False

    connection_ok = False
    # Never send the private NNTP password through SAB's test endpoint while
    # parameter logging is enabled or operator authentication is incomplete.
    if auth_ok and provider_env_ok and live_ok and api_ok:
        try:
            tested = server_tester(
                port,
                api_key,
                {
                    "mode": "config",
                    "name": "test_server",
                    "server": MANAGED_SABNZBD_SERVER,
                    "host": environment["USENET_SERVER_HOST"],
                    "port": str(provider_port),
                    "username": environment["USENET_SERVER_USERNAME"],
                    "password": environment["USENET_SERVER_PASSWORD"],
                    "connections": str(connections),
                    "ssl": "1" if ssl_enabled else "0",
                    "ssl_verify": "3",
                },
            )
            test_value = tested.get("value") if isinstance(tested, dict) else None
            connection_ok = bool(
                isinstance(test_value, dict) and test_value.get("result") is True
            )
        except (TypeError, ValueError, OSError, urllib.error.URLError):
            connection_ok = False

    provider_ok = provider_env_ok and live_ok and connection_ok
    checks.append(
        Check(
            "sabnzbd:usenet-provider",
            provider_ok,
            "managed TLS provider configured and connection-tested"
            if provider_ok
            else (
                "required private provider settings are missing or invalid"
                if not provider_env_ok
                else "managed provider configuration or connection test failed"
            ),
        )
    )
    return checks


def sabnzbd_stopped_managed_provider_check(config_path: Path) -> Check:
    """Verify the managed NNTP server is disabled while SAB itself is stopped."""

    if not config_path.exists():
        return Check("sabnzbd:usenet-provider", True, "absent")
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return Check("sabnzbd:usenet-provider", False, "configuration unavailable")

    in_servers = False
    current_name: str | None = None
    current_values: dict[str, str] = {}
    servers: list[tuple[str, dict[str, str]]] = []

    def finish_current() -> None:
        nonlocal current_name, current_values
        if current_name is not None:
            servers.append((current_name, current_values))
        current_name = None
        current_values = {}

    for raw_line in lines:
        line = raw_line.strip()
        if line == "[servers]":
            finish_current()
            in_servers = True
            continue
        if in_servers and line.startswith("[[") and line.endswith("]]"):
            finish_current()
            current_name = line[2:-2].strip()
            continue
        if line.startswith("[") and line.endswith("]"):
            if in_servers:
                finish_current()
            in_servers = False
            continue
        if in_servers and current_name is not None and "=" in line:
            key, raw_value = line.split("=", 1)
            value = raw_value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            current_values[key.strip().casefold()] = value
    finish_current()

    managed = [
        values
        for section_name, values in servers
        if section_name.casefold() == MANAGED_SABNZBD_SERVER.casefold()
        or str(values.get("name") or values.get("displayname") or "").casefold()
        == MANAGED_SABNZBD_SERVER.casefold()
    ]
    if len(managed) > 1:
        return Check(
            "sabnzbd:usenet-provider", False, "duplicate managed providers"
        )
    if not managed:
        return Check("sabnzbd:usenet-provider", True, "absent")
    disabled = not _database_truthy(managed[0].get("enable", "1"))
    return Check(
        "sabnzbd:usenet-provider",
        disabled,
        "disabled" if disabled else "managed NNTP provider remains enabled",
    )


def shelfarr_runtime_checks(
    port: str,
    api_token: str,
    usenet_enabled: bool = False,
    *,
    qbit_username: str = "",
    qbit_password: str = "",
    runner: object = subprocess.run,
    requester: object = request_json,
) -> list[Check]:
    """Verify Huey's scoped API token and Shelfarr's live client adapters."""

    checks: list[Check] = []
    try:
        request_payload = requester(
            f"http://127.0.0.1:{int(port)}/api/v1/requests?limit=1",
            headers={"Authorization": f"Bearer {api_token}"},
        )
        api_ok = isinstance(request_payload, dict) and isinstance(
            request_payload.get("requests"), list
        )
    except (TypeError, ValueError, OSError, urllib.error.URLError):
        api_ok = False
    checks.append(
        Check(
            "shelfarr:huey-api",
            api_ok,
            "scoped request API authenticated" if api_ok else "authentication failed",
        )
    )

    sentinel = "WYSEARR_CLIENT_RESULTS="
    code = (
        "expected=JSON.parse(STDIN.read);"
        "items=DownloadClient.enabled.order(:client_type).map{|c| "
        "credential_ok=(c.client_type!='qbittorrent'||("
        "ActiveSupport::SecurityUtils.secure_compare("
        "c.username.to_s,expected.fetch('username'))&&"
        "ActiveSupport::SecurityUtils.secure_compare("
        "c.password.to_s,expected.fetch('password'))));"
        "[c.client_type,c.category,c.test_connection,credential_ok]};"
        f"puts({json.dumps(sentinel)}+JSON.generate(items))"
    )
    try:
        result = runner(
            [
                "docker", "compose", "exec", "-T", "--user",
                f"{os.getuid()}:{os.getgid()}", "shelfarr",
                "ruby", "/opt/wysearr/shelfarr_exec.rb",
                "bin/rails", "runner", code,
            ],
            cwd=STACK_ROOT,
            text=True,
            capture_output=True,
            input=json.dumps(
                {"username": qbit_username, "password": qbit_password},
                separators=(",", ":"),
            ),
            timeout=120,
            check=False,
        )
        payload_line = next(
            (
                line[len(sentinel):]
                for line in reversed(result.stdout.splitlines())
                if line.startswith(sentinel)
            ),
            "[]",
        )
        items = json.loads(payload_line) if result.returncode == 0 else []
        expected = {"qbittorrent"} | ({"sabnzbd"} if usenet_enabled else set())
        working = {
            str(item[0])
            for item in items
            if isinstance(item, list)
            and len(item) == 4
            and item[1] == SHELFARR_DOWNLOAD_CATEGORY
            and item[2] is True
        }
        clients_ok = expected == working
        qbit_credentials = [
            item[3]
            for item in items
            if isinstance(item, list)
            and len(item) == 4
            and item[0] == "qbittorrent"
        ]
        qbit_credentials_ok = bool(
            qbit_username
            and qbit_password
            and qbit_credentials == [True]
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        clients_ok = False
        qbit_credentials_ok = False
    checks.append(
        Check(
            "shelfarr:client-connectivity",
            clients_ok,
            "enabled download-client live tests passed"
            if clients_ok
            else "one or more client tests failed",
        )
    )
    checks.append(
        Check(
            "shelfarr:qbittorrent-credentials",
            qbit_credentials_ok,
            "decrypted runtime credential exactly matches the private environment"
            if qbit_credentials_ok
            else "decrypted runtime username or credential mismatch",
        )
    )
    return checks


def bazarr_acceptance(
    settings: dict[str, object], profiles: list[dict[str, object]], status: dict[str, object]
) -> tuple[bool, bool, bool]:
    general = settings.get("general", {})
    if not isinstance(general, dict):
        return False, False, False
    integrations_ok = bool(
        general.get("use_sonarr")
        and general.get("use_radarr")
        and status.get("sonarr_version")
        and status.get("radarr_version")
    )
    english_ids = {
        profile.get("profileId")
        for profile in profiles
        if str(profile.get("name", "")).casefold() == "english"
        and profile.get("profileId") is not None
    }
    profile_ok = bool(
        english_ids
        and general.get("serie_default_enabled")
        and general.get("movie_default_enabled")
        and general.get("serie_default_profile") in english_ids
        and general.get("movie_default_profile") in english_ids
    )
    configured_providers = set(general.get("enabled_providers") or [])
    providers_ok = BAZARR_PROVIDERS <= configured_providers
    return integrations_ok, profile_ok, providers_ok


def validate() -> list[Check]:
    env = load_env(STACK_ROOT / ".env")
    bind_address = env.get("WYSEARR_BIND_ADDRESS", "192.168.4.86")
    checks: list[Check] = []

    compose = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=STACK_ROOT,
        capture_output=True,
        check=False,
    )
    checks.append(Check("compose", compose.returncode == 0, "configuration valid" if compose.returncode == 0 else "configuration invalid"))
    checks.append(
        channel_inventory_check(STACK_ROOT / "docs" / "huey-channels.yml")
    )
    checks.append(huey_ready_check(STACK_ROOT / "state" / "huey" / "ready"))
    checks.append(huey_selection_ttl_check(env))

    media_root = Path(env.get("MEDIA_ROOT", "/mnt/media"))
    torrent_root = Path(env.get("TORRENT_ROOT", str(STACK_ROOT / "state" / "torrents")))
    checks.append(Check("media:mount", os.path.ismount(media_root), f"{media_root}"))
    if media_root.is_dir():
        checks.append(writable_check(media_root, "media:writable"))
    else:
        checks.append(Check("media:writable", False, "directory missing"))
    checks.append(writable_check(torrent_root, "torrents:writable") if torrent_root.is_dir() else Check("torrents:writable", False, "directory missing"))

    expected_dirs = [
        torrent_root / "incomplete",
        torrent_root / "incomplete" / "usenet",
        torrent_root / "usenet",
        torrent_root / SHELFARR_DOWNLOAD_CATEGORY,
    ]
    expected_dirs += [torrent_root / category for category in ARR_CATEGORIES + DIRECT_CATEGORIES]
    missing = [str(path) for path in expected_dirs if not path.is_dir()]
    checks.append(Check("torrents:paths", not missing, "all category paths exist" if not missing else f"{len(missing)} paths missing"))

    feature_flag_ok, shelfarr_enabled, shelfarr_flag_detail = _strict_feature_flag(
        env, SHELFARR_FEATURE_FLAG
    )
    checks.append(
        Check(
            "shelfarr:feature-flag",
            feature_flag_ok,
            shelfarr_flag_detail,
        )
    )
    abba_flag_valid, abba_enabled, abba_flag_detail = _strict_feature_flag(
        env, ABBA_FEATURE_FLAG
    )
    checks.append(
        Check("abba:feature-flag", abba_flag_valid, abba_flag_detail)
    )
    (
        lazylibrarian_flag_valid,
        lazylibrarian_enabled,
        lazylibrarian_flag_detail,
    ) = _strict_feature_flag(env, LAZYLIBRARIAN_FEATURE_FLAG)
    checks.append(
        Check(
            "lazylibrarian:feature-flag",
            lazylibrarian_flag_valid,
            lazylibrarian_flag_detail,
        )
    )
    ebook_owner_valid, ebook_owner, ebook_owner_detail = _strict_ebook_owner(env)
    checks.append(
        Check(
            "ebooks:acquisition-owner",
            ebook_owner_valid,
            ebook_owner_detail,
        )
    )
    (
        ebook_backends_valid,
        ebook_backends,
        ebook_backends_detail,
    ) = _strict_ebook_backends(env)
    checks.append(
        Check(
            "ebooks:acquisition-backends",
            ebook_backends_valid,
            ebook_backends_detail,
        )
    )
    checks.append(
        ebook_backend_order_check(
            ebook_backends,
            policy_valid=ebook_backends_valid,
        )
    )
    checks.append(
        ebook_backend_availability_check(
            ebook_backends,
            policy_valid=ebook_backends_valid,
            environment=env,
            shelfarr_enabled=shelfarr_enabled,
            shelfarr_flag_valid=feature_flag_ok,
            lazylibrarian_enabled=lazylibrarian_enabled,
            lazylibrarian_flag_valid=lazylibrarian_flag_valid,
        )
    )
    usenet_flag_valid, usenet_enabled, usenet_flag_detail = _strict_feature_flag(
        env, USENET_FEATURE_FLAG
    )
    checks.append(
        Check("usenet:feature-flag", usenet_flag_valid, usenet_flag_detail)
    )
    checks.append(
        Check(
            "usenet:ownership-boundary",
            not usenet_enabled or shelfarr_enabled,
            "Shelfarr owns enabled ebook Usenet acquisition"
            if usenet_enabled and shelfarr_enabled
            else (
                "disabled"
                if not usenet_enabled
                else "Usenet cannot be enabled while Shelfarr ownership is disabled"
            ),
        )
    )
    services = (
        CORE_SERVICES
        + (EVALUATION_SERVICES if shelfarr_enabled else ())
        + ((ABBA_SERVICE,) if abba_enabled else ())
        + ((LAZYLIBRARIAN_SERVICE,) if lazylibrarian_enabled else ())
    )
    for service in services:
        checks.append(container_check(service))
    checks.extend(
        qbittorrent_container_credentials_checks(
            env,
            ("huey", "bookbot")
            + ((ABBA_SERVICE,) if abba_enabled else ()),
        )
    )
    if not shelfarr_enabled:
        for service in EVALUATION_SERVICES:
            checks.append(
                container_stopped_check(service, "Shelfarr ebook ownership")
            )
        checks.append(
            sabnzbd_stopped_managed_provider_check(
                STACK_ROOT / "config" / "sabnzbd" / "sabnzbd.ini"
            )
        )
    if not abba_enabled:
        checks.append(
            container_stopped_check(ABBA_SERVICE, "ABBA audiobook ownership")
        )
    if not lazylibrarian_enabled:
        checks.append(
            container_stopped_check(
                LAZYLIBRARIAN_SERVICE, "LazyLibrarian ebook coordination"
            )
        )
    if shelfarr_enabled:
        checks.append(private_published_port_check("sabnzbd"))
        checks.append(private_published_port_check("shelfarr"))
        ebook_output = media_root / "ebooks" / "Books"
        checks.append(
            writable_check(ebook_output, "shelfarr:ebooks-output")
            if ebook_output.is_dir()
            else Check("shelfarr:ebooks-output", False, "directory missing")
        )
        checks.append(service_mount_absent_check("shelfarr", "/audiobooks"))
        checks.extend(shelfarr_storage_checks(STACK_ROOT / "config" / "shelfarr"))
        checks.append(
            shelfarr_direct_staging_check(
                STACK_ROOT / "state" / "shelfarr-staging" / "ebooks"
            )
        )
        checks.append(
            evaluation_report_permissions_check(
                STACK_ROOT / "state" / "shelfarr-evaluation"
            )
        )
        checks.extend(
            shelfarr_configuration_checks(
                STACK_ROOT / "config" / "shelfarr", env
            )
        )
        checks.extend(
            sabnzbd_configuration_checks(
                STACK_ROOT / "config" / "sabnzbd" / "sabnzbd.ini",
                env.get("SABNZBD_ADMIN_PORT", "8085"),
                env.get("SABNZBD_ADMIN_USERNAME", ""),
                env,
            )
        )
        checks.append(
            private_service_storage_check(
                STACK_ROOT / "config" / "sabnzbd", "sabnzbd:storage-permissions"
            )
        )
        checks.extend(
            shelfarr_runtime_checks(
                env.get("SHELFARR_ADMIN_PORT", "5056"),
                env.get("SHELFARR_API_TOKEN", ""),
                usenet_enabled,
                qbit_username=env.get("QBITTORRENT_USERNAME", "admin"),
                qbit_password=env.get("QBITTORRENT_PASSWORD", ""),
            )
        )

    if abba_enabled:
        checks.append(unpublished_service_check(ABBA_SERVICE))
        checks.append(
            private_service_storage_check(
                STACK_ROOT / "config" / "abba", "abba:storage-permissions"
            )
        )
        checks.extend(abba_configuration_checks(env))
        checks.append(
            abba_database_check(STACK_ROOT / "config" / "abba" / "abba.db")
        )
        checks.append(abba_api_readiness_check())

    if lazylibrarian_enabled:
        checks.append(private_published_port_check(LAZYLIBRARIAN_SERVICE))
        checks.append(
            private_service_storage_check(
                STACK_ROOT / "config" / "lazylibrarian",
                "lazylibrarian:storage-permissions",
            )
        )
        checks.extend(
            lazylibrarian_config_checks(
                STACK_ROOT / "config" / "lazylibrarian" / "config.ini",
                env,
            )
        )
        checks.extend(lazylibrarian_runtime_checks(env))
        checks.append(
            lazylibrarian_api_capability_check(
                env.get("LAZYLIBRARIAN_ADMIN_PORT", "5299"),
                env.get("LAZYLIBRARIAN_API_KEY", ""),
            )
        )
        checks.append(
            lazylibrarian_effective_config_check(
                env.get("LAZYLIBRARIAN_ADMIN_PORT", "5299"),
                env.get("LAZYLIBRARIAN_API_KEY", ""),
            )
        )

    try:
        qbit_url = f"http://{bind_address}:{env.get('QBITTORRENT_PORT', '8080')}"
        opener = qbit_opener(
            qbit_url,
            env.get("QBITTORRENT_USERNAME", "admin"),
            env.get("QBITTORRENT_PASSWORD", ""),
        )
        with opener.open(f"{qbit_url}/api/v2/app/version", timeout=15) as response:
            version = response.read().decode().strip()
        with opener.open(f"{qbit_url}/api/v2/torrents/categories", timeout=15) as response:
            categories = json.load(response)
        expected = set(ARR_CATEGORIES + DIRECT_CATEGORIES)
        expected |= {f"{category}-imported" for category in expected}
        expected.add(SHELFARR_DOWNLOAD_CATEGORY)
        missing_categories = sorted(expected - set(categories))
        wrong_category_paths = sorted(
            category
            for category in expected.intersection(categories)
            if categories[category].get("savePath")
            != f"/downloads/{category.removesuffix('-imported')}"
        )
        checks.append(Check("qbittorrent:api", True, f"version={version}"))
        checks.append(Check(
            "qbittorrent:categories",
            not missing_categories and not wrong_category_paths,
            "categories and shared imported paths configured"
            if not missing_categories and not wrong_category_paths
            else f"{len(missing_categories)} missing, {len(wrong_category_paths)} misrouted",
        ))
        audiobook_paths_ok = bool(
            categories.get("audiobooks", {}).get("savePath")
            == "/downloads/audiobooks"
            and categories.get("audiobooks-imported", {}).get("savePath")
            == "/downloads/audiobooks"
            and categories.get(SHELFARR_DOWNLOAD_CATEGORY, {}).get("savePath")
            == "/downloads/shelfarr"
        )
        checks.append(
            Check(
                "audiobooks:category-ownership",
                audiobook_paths_ok,
                (
                    "ABBA acquisition and BookBot import share only the audiobook category path"
                    if abba_enabled
                    else "legacy direct acquisition and BookBot import retain the audiobook category path"
                )
                if audiobook_paths_ok
                else "audiobook, imported, or Shelfarr category path violates ownership",
            )
        )
        checks.append(ebook_category_ownership_check(categories, ebook_owner))
    except Exception as error:  # validation must aggregate all failures
        checks.append(Check("qbittorrent:api", False, type(error).__name__))

    prowlarr_key = env.get("PROWLARR_API_KEY", "")
    try:
        prowlarr_url = f"http://{bind_address}:{env.get('PROWLARR_PORT', '9696')}"
        status = request_json(f"{prowlarr_url}/api/v1/system/status", api_key=prowlarr_key)
        indexers = request_json(f"{prowlarr_url}/api/v1/indexer", api_key=prowlarr_key)
        applications = request_json(f"{prowlarr_url}/api/v1/applications", api_key=prowlarr_key)
        tags = request_json(f"{prowlarr_url}/api/v1/tag", api_key=prowlarr_key)
        indexer_statuses = (
            request_json(
                f"{prowlarr_url}/api/v1/indexerstatus",
                api_key=prowlarr_key,
            )
            if lazylibrarian_enabled
            else []
        )
        app_names = {item.get("name") for item in applications if item.get("enable", True)}
        required_apps = {"Sonarr", "Radarr", "Lidarr", "Whisparr"}
        checks.append(Check("prowlarr:api", bool(status.get("version")), f"version={status.get('version')}"))
        managed_newznab_name = MANAGED_NEWZNAB_INDEXER_DEFAULT.casefold()
        managed_newznab_id = next(
            (
                item.get("id")
                for item in indexers
                if str(item.get("name") or "").casefold()
                == managed_newznab_name
                and isinstance(item.get("id"), int)
            ),
            None,
        )
        live_check, live_indexer_ids, live_protocols = (
            exhaustive_indexer_live_check(
                "prowlarr",
                indexers,
                lambda indexer: post_json_ok(
                    f"{prowlarr_url}/api/v1/indexer/test",
                    indexer,
                    api_key=prowlarr_key,
                    timeout=60,
                ),
                enabled_default=False,
            )
        )
        checks.append(live_check)
        required_book_protocols = {"torrent"} | ({"usenet"} if usenet_enabled else set())
        checks.append(
            Check(
                "prowlarr:book-protocols",
                required_book_protocols <= live_protocols,
                "live torrent indexer; "
                + ("Usenet indexer available" if "usenet" in live_protocols else "Usenet indexer unavailable")
                if required_book_protocols <= live_protocols
                else "missing a live torrent indexer",
            )
        )
        checks.append(
            prowlarr_managed_newznab_check(
                indexers,
                tags,
                applications,
                live_indexer_ids,
                env,
                enabled=usenet_enabled,
                flag_valid=usenet_flag_valid,
            )
        )
        if lazylibrarian_enabled:
            ebook_indexers = _ebook_indexer_contract(indexers, indexer_statuses)
            checks.append(
                prowlarr_lazylibrarian_check(
                    indexers, tags, applications, indexer_statuses
                )
            )
            checks.append(
                lazylibrarian_provider_check(
                    env.get("LAZYLIBRARIAN_ADMIN_PORT", "5299"),
                    env.get("LAZYLIBRARIAN_API_KEY", ""),
                    env.get("PROWLARR_API_KEY", ""),
                    ebook_indexers or {},
                )
            )
        checks.append(Check("prowlarr:applications", required_apps <= app_names, f"configured={len(app_names)}"))
    except Exception as error:
        checks.append(Check("prowlarr:api", False, type(error).__name__))

    arr_specs = (
        ("sonarr", "SONARR", "8989", "v3", "/media/tv", "tv", ("category", "tvCategory"), ("postImportCategory", "tvImportedCategory")),
        ("radarr", "RADARR", "7878", "v3", "/media/movies", "movies", ("category", "movieCategory"), ("postImportCategory", "movieImportedCategory")),
        ("lidarr", "LIDARR", "8686", "v1", "/media/music", "music", ("category", "musicCategory"), ("postImportCategory", "musicImportedCategory")),
        ("whisparr", "WHISPARR", "6969", "v3", "/media/spicy", "spicy", ("category", "tvCategory"), ("postImportCategory", "tvImportedCategory")),
    )
    for name, prefix, default_port, api_version, root_path, category, category_fields, imported_fields in arr_specs:
        checks.append(
            arr_qbittorrent_download_client_credentials_check(
                name,
                STACK_ROOT / ARR_INDEXER_DATABASES[name],
                env.get("QBITTORRENT_USERNAME", "admin"),
                env.get("QBITTORRENT_PASSWORD", ""),
            )
        )
        checks.append(
            arr_prowlarr_indexer_credentials_check(
                name,
                STACK_ROOT / ARR_INDEXER_DATABASES[name],
                prowlarr_key,
            )
        )
        try:
            base = f"http://{bind_address}:{env.get(prefix + '_PORT', default_port)}"
            key = env.get(prefix + "_API_KEY", "")
            status = request_json(f"{base}/api/{api_version}/system/status", api_key=key)
            roots = request_json(f"{base}/api/{api_version}/rootfolder", api_key=key)
            clients = request_json(f"{base}/api/{api_version}/downloadclient", api_key=key)
            indexers = request_json(f"{base}/api/{api_version}/indexer", api_key=key)
            root_ok = any(item.get("path") == root_path and item.get("accessible", True) for item in roots)
            accepted_clients = [
                item
                for item in clients
                if arr_download_client_accepted(
                    item,
                    username=env.get("QBITTORRENT_USERNAME", "admin"),
                    category=category,
                    category_fields=category_fields,
                    imported_fields=imported_fields,
                )
            ]
            client_ok = False
            for client in accepted_clients:
                try:
                    post_json_ok(
                        f"{base}/api/{api_version}/downloadclient/test",
                        client,
                        api_key=key,
                    )
                    client_ok = True
                    break
                except Exception:
                    continue
            checks.append(Check(f"{name}:api", bool(status.get("version")), f"version={status.get('version')}"))
            checks.append(Check(f"{name}:root", root_ok, root_path))
            checks.append(Check(f"{name}:download-client", client_ok, "configuration and live test passed" if client_ok else "configuration or live test failed"))
            live_check, _, _ = exhaustive_indexer_live_check(
                name,
                indexers,
                lambda indexer: post_json_ok(
                    f"{base}/api/{api_version}/indexer/test",
                    indexer,
                    api_key=key,
                    timeout=60,
                ),
                enabled_default=True,
            )
            checks.append(live_check)
            if name == "whisparr":
                qualities = request_json(f"{base}/api/v3/qualityprofile", api_key=key)
                checks.append(Check("whisparr:quality-profiles", bool(qualities), f"configured={len(qualities)}"))
        except Exception as error:
            checks.append(Check(f"{name}:api", False, type(error).__name__))

    try:
        bazarr_port = env.get("BAZARR_PORT", "6767")
        bazarr_url = f"http://{bind_address}:{bazarr_port}"
        yaml_text = (STACK_ROOT / "config" / "bazarr" / "config" / "config.yaml").read_text(encoding="utf-8")
        auth_block = yaml_text.split("auth:", 1)[1].split("\n", 1)[1]
        bazarr_key = next(
            line.split(":", 1)[1].strip()
            for line in auth_block.splitlines()
            if line.startswith("  apikey:")
        )
        headers = {"X-API-KEY": bazarr_key}
        settings_request = urllib.request.Request(f"{bazarr_url}/api/system/settings", headers=headers)
        profiles_request = urllib.request.Request(f"{bazarr_url}/api/system/languages/profiles", headers=headers)
        status_request = urllib.request.Request(f"{bazarr_url}/api/system/status", headers=headers)
        with urllib.request.urlopen(settings_request, timeout=15) as response:
            settings = json.load(response)
        with urllib.request.urlopen(profiles_request, timeout=15) as response:
            profiles = json.load(response)
        with urllib.request.urlopen(status_request, timeout=15) as response:
            status = json.load(response)["data"]
        integrated, profile_ok, providers_ok = bazarr_acceptance(
            settings, profiles, status
        )
        checks.append(Check("bazarr:arr-integration", integrated, f"sonarr={bool(status.get('sonarr_version'))} radarr={bool(status.get('radarr_version'))}"))
        checks.append(Check("bazarr:language-profile", profile_ok, "English defaults configured" if profile_ok else "English defaults missing"))
        checks.append(Check("bazarr:providers", providers_ok, "required providers enabled" if providers_ok else "required providers missing"))
    except Exception as error:
        checks.append(Check("bazarr:api", False, type(error).__name__))

    checks.append(
        huey_database_check(STACK_ROOT / "state" / "huey" / "huey.db")
    )

    try:
        database = STACK_ROOT / "config" / "bookbot" / "bookbot.db"
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        required_tables = {"imports", "events", "recent_additions"}
        checks.append(Check(
            "bookbot:database",
            integrity == "ok" and required_tables <= tables,
            "integrity and schema valid",
        ))
    except Exception as error:
        checks.append(Check("bookbot:database", False, type(error).__name__))

    for service, relative_database in ARR_NOTIFICATION_DATABASES.items():
        checks.append(
            arr_native_discord_check(service, STACK_ROOT / relative_database)
        )
    checks.append(
        bazarr_native_discord_check(
            STACK_ROOT / "config" / "bazarr" / "db" / "bazarr.db",
            STACK_ROOT / "config" / "bazarr" / "config" / "config.yaml",
        )
    )

    token_present = len(env.get("DISCORD_BOT_TOKEN", "")) >= 20
    checks.append(Check("huey:token", token_present, "configured" if token_present else "missing"))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    checks = validate()
    required = [check for check in checks if check.blocking]
    warnings = [check for check in checks if not check.blocking and not check.ok]
    passed = all(check.ok for check in required)
    if args.as_json:
        print(
            json.dumps(
                {
                    "passed": passed,
                    "warning_count": len(warnings),
                    "checks": [asdict(check) for check in checks],
                },
                indent=2,
            )
        )
    else:
        for check in checks:
            outcome = "PASS" if check.ok else "FAIL" if check.blocking else "WARN"
            print(f"{outcome}: {check.name}: {check.detail}")
        print(
            f"{'PASS' if passed else 'FAIL'}: "
            f"{sum(item.ok for item in required)}/{len(required)} required "
            f"production checks passed; upstream warnings={len(warnings)}"
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
