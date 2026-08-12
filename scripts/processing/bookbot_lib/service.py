"""BookBot import, reconciliation, and retention orchestration."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CATEGORY_SPECS, BookBotConfig, CategorySpec
from .errors import (
    ConfigurationError,
    UnsafeSourceError,
    UnsupportedMediaError,
)
from .health import write_health_marker
from .huey import HueyUpdater
from .ledger import ImportLedger
from .qbittorrent import QbittorrentClient
from .storage import LibraryImporter


LOGGER = logging.getLogger(__name__)
TORRENT_HASH = re.compile(r"^[a-fA-F0-9]{40,64}$")
ARR_IMPORTED_CATEGORIES = frozenset(
    {"tv-imported", "movies-imported", "music-imported", "spicy-imported"}
)
DIRECT_IMPORTED_CATEGORIES = frozenset(
    spec.imported_name for spec in CATEGORY_SPECS.values()
)


@dataclass
class CycleCounts:
    observed: int = 0
    eligible: int = 0
    imported: int = 0
    dry_run: int = 0
    retried: int = 0
    rejected: int = 0
    deleted: int = 0
    ignored: int = 0
    reconciled: int = 0
    errors: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        values = {
            "observed": self.observed,
            "eligible": self.eligible,
            "imported": self.imported,
            "dry_run": self.dry_run,
            "retried": self.retried,
            "rejected": self.rejected,
            "deleted": self.deleted,
            "ignored": self.ignored,
            "reconciled": self.reconciled,
            "errors": self.errors,
        }
        values.update(self.extra)
        return values


class BookBotService:
    def __init__(
        self,
        config: BookBotConfig,
        *,
        qbittorrent: QbittorrentClient | None = None,
        ledger: ImportLedger | None = None,
        importer: LibraryImporter | None = None,
        huey: HueyUpdater | None = None,
    ) -> None:
        self.config = config
        self.qbittorrent = qbittorrent or QbittorrentClient(
            config.qbittorrent_url,
            config.qbittorrent_username,
            config.qbittorrent_password,
            config.request_timeout_seconds,
            config.verify_tls,
        )
        self.ledger = ledger or ImportLedger(
            config.database_path,
            config.retry_base_seconds,
            config.retry_max_seconds,
        )
        self.importer = importer or LibraryImporter(
            config.torrent_root, config.media_root
        )
        self.huey = huey or HueyUpdater(config.huey_database_path)

    def close(self) -> None:
        close = getattr(self.qbittorrent, "close", None)
        if callable(close):
            close()

    def validate(self) -> dict[str, Any]:
        self.config.validate_filesystem(require_write=True)
        version = self.qbittorrent.application_version()
        categories = self.qbittorrent.categories()
        required_categories = set(CATEGORY_SPECS) | DIRECT_IMPORTED_CATEGORIES
        missing = sorted(required_categories - set(categories))
        if missing:
            raise ConfigurationError(
                "qBittorrent is missing BookBot categories: " + ", ".join(missing)
            )
        for name in CATEGORY_SPECS:
            save_path = str(categories[name].get("savePath") or "")
            if not save_path:
                raise ConfigurationError(
                    f"qBittorrent category {name} needs an explicit save path"
                )
            category_path = Path(save_path)
            if not category_path.is_absolute():
                raise ConfigurationError(
                    f"qBittorrent category {name} save path must be absolute"
                )
            expected_path = self.config.torrent_root / name
            if category_path != expected_path:
                raise ConfigurationError(
                    f"qBittorrent category {name} must save directly to {expected_path}"
                )
            imported = f"{name}-imported"
            imported_path = Path(str(categories[imported].get("savePath") or ""))
            expected_imported = expected_path
            if imported_path != expected_imported:
                raise ConfigurationError(
                    f"qBittorrent category {imported} must save to {expected_imported}"
                )
        return {"qbittorrent_version": version, "categories": len(categories)}

    def run_cycle(self, *, dry_run: bool = False, now: int | None = None) -> CycleCounts:
        timestamp = int(time.time()) if now is None else int(now)
        torrents = self.qbittorrent.torrents()
        counts = CycleCounts(observed=len(torrents))

        for torrent in torrents:
            category = str(torrent.get("category") or "")
            if category in DIRECT_IMPORTED_CATEGORIES:
                if not dry_run and self._reconcile_imported(torrent, timestamp):
                    counts.reconciled += 1
            elif category in ARR_IMPORTED_CATEGORIES and not dry_run:
                torrent_hash = self._torrent_hash(torrent)
                if torrent_hash is not None:
                    self.ledger.observe_arr_imported(
                        torrent_hash, category, timestamp
                    )

        for torrent in torrents:
            category = str(torrent.get("category") or "")
            spec = CATEGORY_SPECS.get(category)
            if spec is None:
                counts.ignored += 1
                continue
            if not self._is_complete(torrent):
                counts.ignored += 1
                continue
            counts.eligible += 1
            outcome = self._process_torrent(torrent, spec, dry_run, timestamp)
            setattr(counts, outcome, getattr(counts, outcome) + 1)

        cutoff = timestamp - (self.config.retention_days * 86400)
        for torrent in torrents:
            category = str(torrent.get("category") or "")
            if category not in DIRECT_IMPORTED_CATEGORIES | ARR_IMPORTED_CATEGORIES:
                continue
            torrent_hash = self._torrent_hash(torrent)
            if torrent_hash is None:
                counts.errors += 1
                continue
            if category in DIRECT_IMPORTED_CATEGORIES:
                eligible = self.ledger.eligible_for_deletion(torrent_hash, cutoff)
            else:
                eligible = self.ledger.arr_eligible_for_deletion(
                    torrent_hash, category, cutoff
                )
            if eligible is None:
                continue
            if dry_run:
                LOGGER.info(
                    "Dry run: would delete retained torrent hash=%s category=%s",
                    torrent_hash[:12],
                    category,
                )
                continue
            try:
                self.qbittorrent.delete_with_files(torrent_hash)
                if category in DIRECT_IMPORTED_CATEGORIES:
                    self.ledger.mark_deleted(torrent_hash, timestamp)
                else:
                    self.ledger.mark_arr_deleted(torrent_hash, timestamp)
                counts.deleted += 1
            except Exception as exc:  # ledger makes the next cycle safe to retry
                LOGGER.error(
                    "Retention deletion failed hash=%s: %s", torrent_hash[:12], exc
                )
                counts.errors += 1

        counts.extra = {
            f"ledger_{status}": count
            for status, count in self.ledger.counts().items()
        }
        if not dry_run:
            write_health_marker(
                self.config.health_path, "ok", counts=counts.as_dict(), now=timestamp
            )
        return counts

    def _process_torrent(
        self,
        torrent: dict[str, Any],
        spec: CategorySpec,
        dry_run: bool,
        timestamp: int,
    ) -> str:
        torrent_hash = self._torrent_hash(torrent)
        if torrent_hash is None:
            LOGGER.error("Ignoring torrent with invalid hash")
            return "errors"
        content_path = str(torrent.get("content_path") or "")

        if dry_run:
            try:
                self._validate_torrent_routing(torrent, spec)
                self.qbittorrent.ensure_imported_category(
                    spec.name,
                    spec.imported_name,
                    str(torrent.get("save_path") or ""),
                    dry_run=True,
                )
                result = self.importer.import_payload(
                    content_path, spec, torrent_hash, dry_run=True
                )
                LOGGER.info(
                    "Dry run: would import hash=%s to %s (%d files)",
                    torrent_hash[:12],
                    result.destination,
                    result.copied_files,
                )
                return "dry_run"
            except (UnsafeSourceError, UnsupportedMediaError) as exc:
                LOGGER.error("Dry-run payload rejected hash=%s: %s", torrent_hash[:12], exc)
                return "rejected"
            except Exception as exc:
                LOGGER.error("Dry-run import failed hash=%s: %s", torrent_hash[:12], exc)
                return "errors"

        row = self.ledger.get(torrent_hash)
        if row is not None and row["status"] == "copied":
            if not self.ledger.should_finalize(
                torrent_hash, self.config.max_retries, timestamp
            ):
                return "ignored"
            return self._finalize(torrent, spec, row, timestamp)
        if not self.ledger.should_copy(
            torrent_hash, self.config.max_retries, timestamp
        ):
            return "ignored"

        self.ledger.begin_attempt(torrent, spec.imported_name, timestamp)
        try:
            self._validate_torrent_routing(torrent, spec)
            self.qbittorrent.ensure_imported_category(
                spec.name,
                spec.imported_name,
                str(torrent.get("save_path") or ""),
            )
            result = self.importer.import_payload(content_path, spec, torrent_hash)
            self.ledger.mark_copied(torrent_hash, result.destination, timestamp)
        except (UnsafeSourceError, UnsupportedMediaError) as exc:
            self.ledger.mark_rejected(torrent_hash, exc, timestamp)
            self.huey.failed(torrent_hash, exc, str(torrent.get("tags") or ""))
            LOGGER.error("Payload rejected hash=%s: %s", torrent_hash[:12], exc)
            return "rejected"
        except Exception as exc:
            row = self.ledger.get(torrent_hash)
            attempts = int(row["attempts"]) if row is not None else 1
            if attempts >= self.config.max_retries:
                self.ledger.mark_failed(torrent_hash, exc, timestamp)
                self.huey.failed(
                    torrent_hash, exc, str(torrent.get("tags") or "")
                )
                LOGGER.error(
                    "Import exhausted retries hash=%s: %s", torrent_hash[:12], exc
                )
                return "errors"
            self.ledger.mark_retry(torrent_hash, exc, timestamp)
            LOGGER.error("Import will retry hash=%s: %s", torrent_hash[:12], exc)
            return "retried"

        row = self.ledger.get(torrent_hash)
        if row is None:
            raise RuntimeError("BookBot ledger lost a copied import")
        if result.archived_path is not None:
            LOGGER.warning(
                "Archived existing library content to %s", result.archived_path
            )
        return self._finalize(torrent, spec, row, timestamp)

    def _finalize(
        self,
        torrent: dict[str, Any],
        spec: CategorySpec,
        row: Any,
        timestamp: int,
    ) -> str:
        torrent_hash = self._torrent_hash(torrent)
        assert torrent_hash is not None
        destination_raw = row["destination_path"]
        if not destination_raw:
            self.ledger.mark_category_failed(
                torrent_hash, "copied import has no destination", timestamp
            )
            return "errors"
        destination = Path(str(destination_raw))
        try:
            self.importer.validate_import_destination(destination, spec)
            self.importer.clear_import_marker(destination, torrent_hash)
        except Exception as exc:
            self.ledger.mark_category_failed(torrent_hash, exc, timestamp)
            self.huey.failed(
                torrent_hash, exc, str(torrent.get("tags") or "")
            )
            LOGGER.error(
                "Copied destination validation failed hash=%s: %s",
                torrent_hash[:12],
                exc,
            )
            return "errors"
        try:
            self.qbittorrent.ensure_imported_category(
                spec.name,
                spec.imported_name,
                str(torrent.get("save_path") or ""),
            )
            self.qbittorrent.set_category(torrent_hash, spec.imported_name)
        except Exception as exc:
            category_attempts = int(row["category_attempts"]) + 1
            if category_attempts >= self.config.max_retries:
                self.ledger.mark_category_failed(torrent_hash, exc, timestamp)
                self.huey.failed(
                    torrent_hash, exc, str(torrent.get("tags") or "")
                )
                LOGGER.error(
                    "Category update exhausted retries hash=%s: %s",
                    torrent_hash[:12],
                    exc,
                )
                return "errors"
            self.ledger.mark_category_retry(torrent_hash, exc, timestamp)
            LOGGER.error(
                "Category update will retry hash=%s: %s", torrent_hash[:12], exc
            )
            return "retried"

        title = destination.name
        self.ledger.mark_imported(
            torrent_hash, spec.name, title, destination, timestamp
        )
        self.huey.complete(
            torrent_hash,
            destination,
            str(torrent.get("tags") or ""),
        )
        LOGGER.info(
            "Import complete hash=%s category=%s destination=%s",
            torrent_hash[:12],
            spec.name,
            destination,
        )
        return "imported"

    def _reconcile_imported(self, torrent: dict[str, Any], timestamp: int) -> bool:
        torrent_hash = self._torrent_hash(torrent)
        if torrent_hash is None:
            return False
        row = self.ledger.get(torrent_hash)
        if row is None or row["status"] in {"imported", "deleted", "rejected"}:
            return False
        destination_raw = row["destination_path"]
        if not destination_raw:
            return False
        destination = Path(str(destination_raw))
        if not destination.exists() or destination.is_symlink():
            return False
        source_category = str(row["source_category"])
        if source_category not in CATEGORY_SPECS:
            return False
        self.ledger.mark_imported(
            torrent_hash,
            source_category,
            destination.name,
            destination,
            timestamp,
        )
        self.huey.complete(
            torrent_hash,
            destination,
            str(torrent.get("tags") or ""),
        )
        return True

    @staticmethod
    def _is_complete(torrent: dict[str, Any]) -> bool:
        try:
            progress = float(torrent.get("progress", 0.0))
            amount_left = int(torrent.get("amount_left", 1))
        except (TypeError, ValueError):
            return False
        return progress >= 1.0 and amount_left == 0

    @staticmethod
    def _torrent_hash(torrent: dict[str, Any]) -> str | None:
        value = str(torrent.get("hash") or "").lower()
        return value if TORRENT_HASH.fullmatch(value) else None

    def _validate_torrent_routing(
        self, torrent: dict[str, Any], spec: CategorySpec
    ) -> None:
        expected = self.config.torrent_root / spec.name
        save_path = Path(str(torrent.get("save_path") or ""))
        content_path = Path(str(torrent.get("content_path") or ""))
        if not save_path.is_absolute() or save_path != expected:
            raise UnsafeSourceError(
                f"torrent category {spec.name} is not routed directly to {expected}"
            )
        if not content_path.is_absolute() or content_path == save_path:
            raise UnsafeSourceError("torrent content path is not a child payload")
        try:
            content_path.relative_to(save_path)
        except ValueError as exc:
            raise UnsafeSourceError(
                "torrent content path escapes its category save path"
            ) from exc
