#!/usr/bin/env python3
"""Run and record the controlled Shelfarr comparison against failed book requests."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from bootstrap import BootstrapError, load_dotenv
from huey.clients import ProwlarrClient, ServiceError, ShelfarrClient
from huey.matching import normalize_text, select_shelfarr_candidate


STACK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUEST_IDS = (9, 10, 11, 12, 13, 18, 19)
EVALUATION_ID_OFFSET = 900_000_000
EVALUATION_ATTEMPT_STRIDE = 100_000
ACTIVE_RESULTS = frozenset(
    {"queued", "pending", "searching", "not_found_retrying", "downloading", "processing"}
)
UNRESOLVED_RESULTS = ACTIVE_RESULTS | {"cleanup_failed", "submission_uncertain"}
EBOOK_EXTENSIONS = frozenset({".azw", ".azw3", ".cbz", ".epub", ".mobi", ".pdf"})
AUDIOBOOK_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".m4b", ".mp3", ".ogg", ".opus"}
)
DIRECT_SOURCES = frozenset(
    {"anna_archive", "gutenberg", "librivox", "zlibrary"}
)
USENET_SOURCES = frozenset({"newznab", "nzb", "usenet"})
TORRENT_SOURCES = frozenset({"jackett", "torrent"})
CANDIDATE_SOURCE_KINDS = ("direct", "usenet", "torrent", "unknown")
DOWNLOAD_STATUS_NAMES = {
    0: "queued",
    1: "downloading",
    2: "paused",
    3: "completed",
    4: "failed",
}


@dataclass
class EvaluationRecord:
    huey_request_id: int
    shelfarr_request_id: str | None
    title: str
    author: str | None
    format: str
    previous_status: str
    found: bool | None = None
    acquisition_source: str | None = None
    selected_release: str | None = None
    download_result: str = "not_started"
    shelfarr_status: str = "not_submitted"
    final_path: str | None = None
    final_library_available: bool = False
    library_catalog_visible: str = "unverified"
    notes: str = ""
    correlation_id: int | None = None
    metadata_resolution: str = "not_observed"
    metadata_candidate_count: int | None = None
    acquisition_candidate_counts: dict[str, int] | None = None
    selected_source: str | None = None
    acquisition_result: str = "not_observed"
    import_result: str = "not_observed"


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def historical_requests(
    database: Path, request_ids: Iterable[int]
) -> list[dict[str, Any]]:
    ids = tuple(dict.fromkeys(int(value) for value in request_ids))
    if not ids:
        raise BootstrapError("At least one historical Huey request ID is required")
    placeholders = ",".join("?" for _ in ids)
    with closing(_open_readonly(database)) as connection:
        rows = connection.execute(
            f"""
            SELECT id, media_type, title, author, status
            FROM requests
            WHERE id IN ({placeholders})
            ORDER BY id
            """,
            ids,
        ).fetchall()
    found_ids = {int(row["id"]) for row in rows}
    missing = sorted(set(ids) - found_ids)
    if missing:
        raise BootstrapError(f"Historical Huey request is missing: {missing[0]}")
    result = []
    targets: set[tuple[str, str, str]] = set()
    for row in rows:
        value = dict(row)
        if value["media_type"] not in {"ebooks", "audiobooks"}:
            raise BootstrapError(f"Request #{value['id']} is not an ebook/audiobook")
        if value["status"] not in {"failed", "needs_selection"}:
            raise BootstrapError(f"Request #{value['id']} was not previously unsuccessful")
        if not value.get("title"):
            raise BootstrapError(f"Request #{value['id']} has no parsed title")
        target = (
            str(value["media_type"]),
            normalize_text(value["title"]),
            normalize_text(value.get("author")),
        )
        if target in targets:
            continue
        targets.add(target)
        result.append(value)
    return result


def evaluation_correlation_id(huey_request_id: int, attempt: int) -> int:
    if (
        isinstance(huey_request_id, bool)
        or not 1 <= int(huey_request_id) < EVALUATION_ATTEMPT_STRIDE
    ):
        raise BootstrapError(
            f"Huey request ID must be between 1 and {EVALUATION_ATTEMPT_STRIDE - 1}"
        )
    if attempt < 0 or attempt > 999:
        raise BootstrapError("Evaluation attempt must be between 0 and 999")
    return EVALUATION_ID_OFFSET + (attempt * EVALUATION_ATTEMPT_STRIDE) + int(
        huey_request_id
    )


def library_has_title(
    root: Path, title: str, author: str | None, media_format: str
) -> bool:
    """Return true only when a matching title contains a valid final artifact."""

    wanted = normalize_text(title)
    wanted_author = normalize_text(author)
    if not root.is_dir() or not wanted:
        return False
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        candidate = normalize_text(path.stem if path.is_file() else path.name)
        if wanted == candidate or (len(wanted) >= 12 and wanted in candidate):
            ancestors = " ".join(
                normalize_text(parent.name)
                for parent in (path, *path.parents)
                if parent != root.parent
            )
            if (
                (not wanted_author or wanted_author in ancestors)
                and final_artifact_available(path, media_format)
            ):
                return True
    return False


def _api_search_results(client: ShelfarrClient, request_id: str) -> list[dict[str, Any]]:
    value = client._request(
        "GET", f"/api/v1/requests/{int(request_id)}/search_results"
    )
    if not isinstance(value, dict) or not isinstance(value.get("search_results"), list):
        return []
    return [dict(item) for item in value["search_results"] if isinstance(item, dict)]


def observe_metadata_resolution(
    client: ShelfarrClient,
    record: EvaluationRecord,
    media_type: str,
) -> None:
    """Record the normal Huey matcher decision without creating a request."""

    candidates = client.search(record.title, record.author)
    selection = select_shelfarr_candidate(
        record.title,
        record.author,
        media_type,
        candidates,
        minimum_confidence=client.minimum_confidence,
        runner_up_gap=client.runner_up_gap,
    )
    record.metadata_candidate_count = len(selection.ranked)
    record.metadata_resolution = (
        "resolved" if selection.reason == "selected" else selection.reason
    )


def read_prowlarr_indexer_protocols(
    environment: dict[str, str],
) -> dict[str, str]:
    """Return a sanitized indexer-name -> protocol map using one read-only GET.

    Candidate visibility must never become a prerequisite for acquisition.  If
    Prowlarr is unavailable or returns an unfamiliar shape, callers classify
    its candidates as ``unknown`` rather than guessing from an indexer name.
    """

    api_key = environment.get("PROWLARR_API_KEY", "").strip()
    if not api_key:
        return {}
    base_url = environment.get("PROWLARR_URL", "").strip()
    if not base_url:
        bind_address = environment.get(
            "WYSEARR_BIND_ADDRESS", "192.168.4.86"
        ).strip()
        port = environment.get("PROWLARR_PORT", "9696").strip()
        base_url = f"http://{bind_address}:{port}"
    try:
        client = ProwlarrClient(base_url, api_key, timeout=10)
        value = client._request("GET", "/api/v1/indexer")
    except (ServiceError, ValueError):
        return {}
    if not isinstance(value, list):
        return {}
    protocols: dict[str, str] = {}
    conflicting_names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = normalize_text(item.get("name"))
        protocol = str(item.get("protocol") or "").strip().casefold()
        if not name or protocol not in {"torrent", "usenet"}:
            continue
        if name in conflicting_names:
            continue
        previous = protocols.get(name)
        if previous is not None and previous != protocol:
            protocols.pop(name, None)
            conflicting_names.add(name)
            continue
        protocols[name] = protocol
    return protocols


def _source_key(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .casefold()
        .replace("-", "_")
        .replace(" ", "_")
    )


def candidate_source_kind(
    candidate: dict[str, Any], indexer_protocols: dict[str, str] | None = None
) -> str:
    """Classify one Shelfarr result without inferring protocol from its name."""

    source = _source_key(candidate.get("source"))
    if source in DIRECT_SOURCES:
        return "direct"
    if source in USENET_SOURCES:
        return "usenet"
    if source in TORRENT_SOURCES:
        return "torrent"
    if source == "prowlarr":
        protocol = (indexer_protocols or {}).get(
            normalize_text(candidate.get("indexer"))
        )
        if protocol in {"torrent", "usenet"}:
            return protocol
    return "unknown"


def observe_acquisition_candidates(
    record: EvaluationRecord,
    results: Sequence[dict[str, Any]],
    indexer_protocols: dict[str, str] | None = None,
) -> None:
    """Record source counts and the source of Shelfarr's selected release."""

    counts = {kind: 0 for kind in CANDIDATE_SOURCE_KINDS}
    selected: dict[str, Any] | None = None
    for candidate in results:
        kind = candidate_source_kind(candidate, indexer_protocols)
        counts[kind] += 1
        if (
            selected is None
            and str(candidate.get("status") or "").casefold() == "selected"
        ):
            selected = candidate
    record.acquisition_candidate_counts = counts
    if selected is not None:
        record.selected_source = candidate_source_kind(
            selected, indexer_protocols
        )


def update_measurement_outcomes(record: EvaluationRecord) -> None:
    """Derive acquisition and import phase outcomes from recorded evidence."""

    result = record.download_result.casefold()
    status = record.shelfarr_status.casefold()
    candidate_count = sum((record.acquisition_candidate_counts or {}).values())

    if result == "success":
        record.acquisition_result = "success"
        record.import_result = "success"
    elif result == "import_path_missing":
        record.acquisition_result = "success"
        record.import_result = "artifact_missing"
    elif result == "skipped_existing":
        record.acquisition_result = "not_started"
        record.import_result = "preexisting"
    elif result == "metadata_unresolved":
        record.acquisition_result = "not_started"
        record.import_result = "not_started"
    elif result in {"submission_uncertain", "cleanup_failed"}:
        record.acquisition_result = "uncertain"
        record.import_result = "uncertain"
    elif result in {"timed_out", "submission_failed"}:
        record.acquisition_result = result
        record.import_result = "not_started"
    elif result == "not_found":
        record.acquisition_result = "no_candidate"
        record.import_result = "not_started"
    elif result == "failure" or status == "failed":
        if record.acquisition_result == "success":
            record.import_result = "failure"
            return
        if record.selected_source or record.selected_release:
            record.acquisition_result = "failure"
        elif record.acquisition_candidate_counts is None:
            # A legacy row (or a monitoring observation failure) has no phase
            # evidence. Preserve unknown rather than recasting it as no-result.
            return
        elif candidate_count:
            record.acquisition_result = "candidate_unselected"
        else:
            record.acquisition_result = "no_candidate"
        record.import_result = "not_started"
    elif status == "completed" or result == "processing":
        record.acquisition_result = "success"
        record.import_result = "in_progress"
    elif result in ACTIVE_RESULTS:
        record.acquisition_result = "in_progress"
        record.import_result = "not_started"
    elif result == "not_started":
        record.acquisition_result = "not_started"
        record.import_result = "not_started"


def download_status_name(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None and str(value).strip().isdigit():
        return DOWNLOAD_STATUS_NAMES.get(numeric)
    normalized = str(value).strip().casefold()
    return normalized if normalized in set(DOWNLOAD_STATUS_NAMES.values()) else None


def shelfarr_artifact(database: Path, request_id: str) -> dict[str, Any]:
    with closing(_open_readonly(database)) as connection:
        row = connection.execute(
            """
            SELECT books.file_path,
                   downloads.download_type,
                   downloads.status AS download_status,
                   downloads.download_path,
                   download_clients.client_type,
                   search_results.source,
                   search_results.indexer,
                   search_results.title AS release_title
            FROM requests
            JOIN books ON books.id = requests.book_id
            LEFT JOIN downloads ON downloads.id = (
                SELECT candidate.id FROM downloads AS candidate
                WHERE candidate.request_id = requests.id
                ORDER BY candidate.id DESC LIMIT 1
            )
            LEFT JOIN download_clients
              ON CAST(download_clients.id AS TEXT) = downloads.download_client_id
            LEFT JOIN search_results ON search_results.id = downloads.search_result_id
            WHERE requests.id = ?
            """,
            (int(request_id),),
        ).fetchone()
    return dict(row) if row is not None else {}


def acquisition_source(
    artifact: dict[str, Any],
    results: Sequence[dict[str, Any]] | None,
    indexer_protocols: dict[str, str] | None = None,
) -> str | None:
    client_type = str(artifact.get("client_type") or "").casefold()
    source = str(artifact.get("source") or "").casefold()
    download_type = str(artifact.get("download_type") or "").casefold()
    if client_type in {"sabnzbd", "nzbget"}:
        return "usenet"
    if client_type in {"qbittorrent", "decypharr", "deluge", "transmission"}:
        return "torrent"
    if download_type == "direct" or source in DIRECT_SOURCES:
        return "direct"
    selected = next(
        (
            item
            for item in (results or ())
            if str(item.get("status") or "").casefold() == "selected"
        ),
        None,
    )
    if selected:
        selected_kind = candidate_source_kind(selected, indexer_protocols)
        if selected_kind != "unknown":
            return selected_kind
    artifact_kind = candidate_source_kind(artifact, indexer_protocols)
    if artifact_kind != "unknown":
        return artifact_kind
    return source or None


def apply_artifact_observation(
    record: EvaluationRecord,
    artifact: dict[str, Any],
    results: Sequence[dict[str, Any]] | None,
    media_root: Path,
    indexer_protocols: dict[str, str] | None = None,
) -> None:
    """Apply source/release/path evidence without trusting remote status alone."""

    if results is not None:
        observe_acquisition_candidates(record, results, indexer_protocols)
    record.found = bool(results) or bool(artifact.get("download_type"))
    record.acquisition_source = acquisition_source(
        artifact, results, indexer_protocols
    )
    if record.acquisition_source in {"direct", "usenet", "torrent"}:
        record.selected_source = record.acquisition_source
    observed_download_status = download_status_name(artifact.get("download_status"))
    if observed_download_status == "completed":
        record.acquisition_result = "success"
        record.import_result = "in_progress"
    elif observed_download_status == "failed":
        record.acquisition_result = "failure"
        record.import_result = "not_started"
    elif observed_download_status in {"queued", "downloading", "paused"}:
        record.acquisition_result = "in_progress"
        record.import_result = "not_started"
    record.selected_release = (
        str(artifact["release_title"])
        if artifact.get("release_title")
        else next(
            (
                str(item.get("title"))
                for item in (results or ())
                if str(item.get("status")) == "selected" and item.get("title")
            ),
            None,
        )
    )
    record.final_path = (
        str(artifact["file_path"]) if artifact.get("file_path") else None
    )
    record.final_library_available = final_artifact_available(
        host_final_path(record.final_path, media_root), record.format
    )


def verify_recovered_completions(
    records: Sequence[EvaluationRecord], shelfarr_database: Path, media_root: Path
) -> None:
    """Resolve final-cleanup recoveries without cancelling completed requests."""

    for record in records:
        if (
            record.shelfarr_status.casefold() != "completed"
            or record.download_result != "processing"
            or not record.shelfarr_request_id
        ):
            continue
        try:
            artifact = shelfarr_artifact(
                shelfarr_database, record.shelfarr_request_id
            )
            apply_artifact_observation(record, artifact, None, media_root)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            record.download_result = "cleanup_failed"
            record.notes = (
                "Shelfarr completed the request, but final DAS verification failed; "
                "operator verification is required."
            )
            continue
        record.download_result = (
            "success" if record.final_library_available else "import_path_missing"
        )
        record.notes = (
            "Recovered a completed Shelfarr request and verified its final DAS artifact."
            if record.final_library_available
            else "Shelfarr reported completion, but no final DAS artifact was verified."
        )


def host_final_path(container_path: str | None, media_root: Path) -> Path | None:
    if not container_path:
        return None
    path = Path(container_path)
    mappings = (
        (Path("/ebooks"), media_root / "ebooks" / "Books"),
        (Path("/audiobooks"), media_root / "audiobooks"),
    )
    for container_root, host_root in mappings:
        try:
            relative = path.relative_to(container_root)
        except ValueError:
            continue
        base = host_root.resolve(strict=False)
        candidate = (base / relative).resolve(strict=False)
        if candidate != base and candidate.is_relative_to(base):
            return candidate
    return None


def final_artifact_available(path: Path | None, media_format: str) -> bool:
    """Require a readable, nonempty media artifact below the mapped final path."""

    if path is None or path.is_symlink():
        return False
    extensions = (
        EBOOK_EXTENSIONS if media_format == "ebook" else AUDIOBOOK_EXTENSIONS
    )
    candidates = [path] if path.is_file() else path.rglob("*") if path.is_dir() else []
    for candidate in candidates:
        try:
            if (
                not candidate.is_symlink()
                and candidate.is_file()
                and candidate.suffix.casefold() in extensions
                and candidate.stat().st_size > 0
                and os.access(candidate, os.R_OK)
            ):
                return True
        except OSError:
            continue
    return False


def _acquire_evaluation_lock(path: Path) -> int:
    _prepare_private_evaluation_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise BootstrapError("Another Shelfarr evaluation process is active") from exc
    return descriptor


def _prepare_private_evaluation_directory(path: Path) -> Path:
    """Create or validate the dedicated owner-only evaluation state directory."""

    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise BootstrapError("Shelfarr evaluation output directory is unsafe")
    path.chmod(0o700)
    return path


def validate_evaluation_output(root: Path, output: Path) -> Path:
    """Confine reports to the private evaluation state tree."""

    boundary = (root / "state" / "shelfarr-evaluation").resolve(strict=False)
    candidate = output.resolve(strict=False)
    if candidate.parent != boundary or candidate == boundary:
        raise BootstrapError(
            "Shelfarr evaluation output must be directly below state/shelfarr-evaluation"
        )
    _prepare_private_evaluation_directory(boundary)
    if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()):
        raise BootstrapError("Shelfarr evaluation output is unsafe")
    return candidate


def validate_shelfarr_database(path: Path) -> None:
    """Fail before the first API write if the pinned Shelfarr schema is unavailable."""

    try:
        with closing(_open_readonly(path)) as connection:
            required = {"requests", "books", "downloads", "download_clients", "search_results"}
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not required <= present:
                raise BootstrapError("Shelfarr evaluation database schema is incomplete")
            connection.execute(
                "SELECT books.file_path, downloads.download_type, "
                "download_clients.client_type, search_results.source "
                "FROM requests JOIN books ON books.id = requests.book_id "
                "LEFT JOIN downloads ON downloads.request_id = requests.id "
                "LEFT JOIN download_clients ON CAST(download_clients.id AS TEXT) = "
                "downloads.download_client_id "
                "LEFT JOIN search_results ON search_results.id = downloads.search_result_id "
                "LIMIT 0"
            )
    except sqlite3.Error as exc:
        raise BootstrapError("Shelfarr evaluation database schema is unavailable") from exc


def cancel_evaluation_request(
    client: ShelfarrClient,
    record: EvaluationRecord,
    *,
    result: str,
    note: str,
) -> bool:
    """Require Shelfarr to confirm cancellation before recording a terminal result."""

    try:
        remote = client.cancel_request(record.shelfarr_request_id or "")
    except ServiceError:
        record.download_result = "cleanup_failed"
        record.notes = f"{note} Cancellation failed; the request may still be active."
        return False
    if str(remote.get("status") or "").casefold() != "failed":
        record.download_result = "cleanup_failed"
        record.notes = f"{note} Shelfarr did not confirm cancellation."
        return False
    record.shelfarr_status = "failed"
    record.download_result = result
    record.notes = note
    return True


def cancel_active_evaluation_requests(
    client: ShelfarrClient, records: Sequence[EvaluationRecord]
) -> None:
    """Close every still-active remote request at the bounded pilot deadline."""

    for record in records:
        if (
            record.shelfarr_status.casefold() == "completed"
            or not record.shelfarr_request_id
            or record.download_result not in (
            ACTIVE_RESULTS | {"cleanup_failed"}
            )
        ):
            continue
        if record.download_result == "cleanup_failed":
            result = "timed_out"
            note = "Retrying cleanup for a previously unconfirmed cancellation."
        elif record.download_result == "not_found_retrying":
            result = "not_found"
            note = (
                "No automatic acquisition candidate was available before the "
                "evaluation deadline."
            )
        else:
            result = "timed_out"
            note = (
                "Shelfarr did not reach a terminal state before the evaluation "
                "deadline."
            )
        cancel_evaluation_request(client, record, result=result, note=note)


def recover_uncertain_evaluation_requests(
    client: ShelfarrClient,
    records: Sequence[EvaluationRecord],
    *,
    final_attempt: bool = True,
) -> None:
    """Recover and cancel submissions whose POST outcome was uncertain."""

    for record in records:
        if (
            record.shelfarr_request_id
            or record.correlation_id is None
            or record.download_result != "submission_uncertain"
        ):
            continue
        try:
            remote = client.recover_request(record.correlation_id)
        except ServiceError:
            if final_attempt:
                record.download_result = "cleanup_failed"
            record.notes = (
                "Shelfarr submission outcome is unknown and correlation recovery failed; "
                + (
                    "operator cleanup is required."
                    if final_attempt
                    else "cleanup recovery will be retried before exit."
                )
            )
            continue
        if remote is None:
            if final_attempt:
                record.download_result = "cleanup_failed"
                record.notes = (
                    "Shelfarr did not expose the uncertain correlation before exit. "
                    "A late request creation remains possible; operator cleanup is required."
                )
            else:
                record.notes = (
                    "Shelfarr did not yet expose the uncertain correlation; "
                    "cleanup recovery will be retried before exit."
                )
            continue
        record.shelfarr_request_id = str(remote["id"])
        record.shelfarr_status = str(remote.get("status") or "")
        status = record.shelfarr_status.casefold()
        if status == "completed":
            # Preserve the correlation for the normal artifact inspection
            # loop; a remote completed flag alone is not a DAS success.
            record.download_result = "processing"
            record.notes = (
                "Recovered a completed Shelfarr request after an uncertain submission; "
                "verifying its final DAS artifact."
            )
            continue
        if status == "failed":
            record.download_result = "failure"
            record.notes = "Recovered a failed Shelfarr request after uncertain submission."
            continue
        cancel_evaluation_request(
            client,
            record,
            result="submission_failed",
            note="Recovered and cancelled an uncertain Shelfarr submission.",
        )


def write_results(path: Path, records: Sequence[EvaluationRecord]) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise BootstrapError("Shelfarr evaluation output directory is unsafe")
    for record in records:
        update_measurement_outcomes(record)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "records": [asdict(record) for record in records],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def merge_evaluation_records(
    existing: Sequence[EvaluationRecord], incoming: Sequence[EvaluationRecord]
) -> list[EvaluationRecord]:
    """Consolidate retries without hiding a current safety obligation.

    A proven success remains the canonical availability result when a later run
    merely observes the existing file or reaches another ordinary terminal
    outcome.  Unresolved acquisition/cleanup state must always remain visible,
    even if an earlier attempt succeeded, so the CLI cannot exit successfully
    while a remote request may still be active.
    """

    merged = {record.huey_request_id: record for record in existing}
    order = [record.huey_request_id for record in existing]
    for record in incoming:
        current = merged.get(record.huey_request_id)
        if current is None:
            order.append(record.huey_request_id)
            merged[record.huey_request_id] = record
        elif record.download_result in UNRESOLVED_RESULTS:
            merged[record.huey_request_id] = record
        elif current.download_result in UNRESOLVED_RESULTS:
            continue
        elif current.download_result != "success" or record.download_result == "success":
            merged[record.huey_request_id] = record
    return [merged[request_id] for request_id in order]


def load_evaluation_records(path: Path) -> list[EvaluationRecord]:
    """Load a prior report so approved retries preserve proven outcomes."""

    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(raw_records, list):
            raise ValueError("records is not a list")
        allowed = set(EvaluationRecord.__dataclass_fields__)
        records: list[EvaluationRecord] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict) or set(raw_record) - allowed:
                raise ValueError("record has an invalid shape")
            records.append(EvaluationRecord(**raw_record))
        return records
    except (OSError, TypeError, ValueError) as exc:
        raise BootstrapError("Existing Shelfarr evaluation report is invalid") from exc


def evaluate(
    root: Path,
    request_ids: Sequence[int],
    *,
    monitor_seconds: int,
    poll_seconds: int,
    output: Path,
    attempt: int = 0,
) -> list[EvaluationRecord]:
    output = validate_evaluation_output(root, output)
    lock_descriptor = _acquire_evaluation_lock(
        root / "state" / "shelfarr-evaluation" / "evaluation.lock"
    )
    try:
        prior_records = load_evaluation_records(output)
        report_directory = output.parent
        unresolved: list[EvaluationRecord] = []
        for report in sorted(report_directory.glob("*.json")):
            if report.is_symlink() or not report.is_file():
                raise BootstrapError("Shelfarr evaluation report path is unsafe")
            unresolved.extend(
                record
                for record in load_evaluation_records(report)
                if record.download_result in UNRESOLVED_RESULTS
            )
        if unresolved:
            identifiers = ", ".join(
                sorted({f"#{record.huey_request_id}" for record in unresolved})
            )
            raise BootstrapError(
                "Existing Shelfarr evaluation records require confirmed operator cleanup "
                f"before another attempt: {identifiers}"
            )
        return _evaluate_locked(
            root,
            request_ids,
            monitor_seconds=monitor_seconds,
            poll_seconds=poll_seconds,
            output=output,
            attempt=attempt,
            prior_records=prior_records,
        )
    finally:
        os.close(lock_descriptor)


def _evaluate_locked(
    root: Path,
    request_ids: Sequence[int],
    *,
    monitor_seconds: int,
    poll_seconds: int,
    output: Path,
    attempt: int,
    prior_records: Sequence[EvaluationRecord] = (),
) -> list[EvaluationRecord]:
    environment = load_dotenv(root / ".env")
    if environment.get("SHELFARR_ENABLED", "") != "true":
        raise BootstrapError("SHELFARR_ENABLED must be true for the production evaluation")
    token = environment.get("SHELFARR_API_TOKEN", "")
    if not token:
        raise BootstrapError("SHELFARR_API_TOKEN is not configured")
    client = ShelfarrClient(
        f"http://127.0.0.1:{environment.get('SHELFARR_ADMIN_PORT', '5056')}",
        token,
        timeout=float(environment.get("SHELFARR_TIMEOUT_SECONDS", "20")),
        search_limit=int(environment.get("SHELFARR_SEARCH_LIMIT", "10")),
        minimum_confidence=float(
            environment.get("HUEY_SHELFARR_MINIMUM_CONFIDENCE", "0.80")
        ),
        runner_up_gap=float(
            environment.get("HUEY_SHELFARR_RUNNER_UP_GAP", "0.05")
        ),
        language=environment.get("SHELFARR_LANGUAGE", "en"),
    )
    indexer_protocols = read_prowlarr_indexer_protocols(environment)
    historical = historical_requests(root / "state" / "huey" / "huey.db", request_ids)
    media_root = Path(environment.get("MEDIA_ROOT", "/mnt/media"))
    shelfarr_database = root / "config" / "shelfarr" / "production.sqlite3"
    validate_shelfarr_database(shelfarr_database)
    records: list[EvaluationRecord] = []

    def persist() -> list[EvaluationRecord]:
        consolidated = merge_evaluation_records(prior_records, records)
        write_results(output, consolidated)
        return consolidated

    try:
        for request in historical:
            media_type = str(request["media_type"])
            library_root = (
                media_root / "ebooks" / "Books"
                if media_type == "ebooks"
                else media_root / "audiobooks"
            )
            record = EvaluationRecord(
                huey_request_id=int(request["id"]),
                shelfarr_request_id=None,
                title=str(request["title"]),
                author=str(request["author"]) if request.get("author") else None,
                format="ebook" if media_type == "ebooks" else "audiobook",
                previous_status=str(request["status"]),
            )
            if library_has_title(
                library_root, record.title, record.author, record.format
            ):
                record.download_result = "skipped_existing"
                record.final_library_available = True
                record.notes = "Target already existed before evaluation; no acquisition attempted."
                records.append(record)
                continue
            try:
                observe_metadata_resolution(client, record, media_type)
            except ServiceError:
                record.metadata_resolution = "observation_failed"
                record.metadata_candidate_count = None
            try:
                record.correlation_id = evaluation_correlation_id(
                    record.huey_request_id, attempt
                )
                record.download_result = "submission_uncertain"
                records.append(record)
                persist()
                response = client.submit(
                    media_type,
                    record.title,
                    record.author,
                    record.correlation_id,
                )
            except ServiceError as exc:
                record.download_result = "submission_uncertain"
                record.notes = str(exc)
                recover_uncertain_evaluation_requests(
                    client, [record], final_attempt=False
                )
                continue
            record.shelfarr_status = str(response["status"])
            record.shelfarr_request_id = (
                str(response["external_id"]) if response.get("external_id") else None
            )
            if response["status"] == "needs_selection":
                record.found = False
                record.download_result = "metadata_unresolved"
                record.notes = str(response["message"])
                if record.metadata_resolution == "observation_failed":
                    record.metadata_resolution = "unresolved"
            elif response["status"] in {"complete", "completed"}:
                record.found = True
                record.download_result = "processing"
            else:
                record.download_result = "queued"
            persist()

        deadline = time.monotonic() + max(0, monitor_seconds)
        while time.monotonic() <= deadline:
            active = False
            for record in records:
                if (
                    not record.shelfarr_request_id
                    or record.download_result not in ACTIVE_RESULTS
                ):
                    continue
                active = True
                try:
                    remote = client.get_request(record.shelfarr_request_id)
                    status = str(remote.get("status") or "")
                    results = _api_search_results(client, record.shelfarr_request_id)
                    artifact = shelfarr_artifact(
                        shelfarr_database, record.shelfarr_request_id
                    )
                except (ServiceError, OSError, sqlite3.Error) as exc:
                    record.notes = f"Monitoring deferred: {type(exc).__name__}"
                    continue

                record.shelfarr_status = status
                apply_artifact_observation(
                    record,
                    artifact,
                    results,
                    media_root,
                    indexer_protocols,
                )
                if status == "completed":
                    record.download_result = (
                        "success"
                        if record.final_library_available
                        else "import_path_missing"
                    )
                elif status == "failed":
                    record.download_result = "failure"
                    record.notes = str(
                        remote.get("issue_description") or "Shelfarr failed"
                    )
                elif status in {"not_found", "awaiting_purchase"}:
                    note = (
                        "No automatic acquisition candidate was available in this evaluation."
                    )
                    if status == "not_found" and time.monotonic() < deadline:
                        record.download_result = "not_found_retrying"
                        record.notes = (
                            f"{note} Kept open until the evaluation deadline for retry observation."
                        )
                    else:
                        cancel_evaluation_request(
                            client, record, result="not_found", note=note
                        )
                elif remote.get("attention_needed") is True:
                    note = str(
                        remote.get("issue_description")
                        or "Shelfarr requested operator review"
                    )
                    cancel_evaluation_request(
                        client, record, result="failure", note=note
                    )
                else:
                    record.download_result = status or "queued"
            persist()
            if not active or all(
                record.download_result not in ACTIVE_RESULTS
                for record in records
            ):
                break
            time.sleep(max(1, poll_seconds))
    finally:
        # Expected exceptions and operator interrupts must not leave remote
        # acquisition work running after the bounded production evaluation.
        recover_uncertain_evaluation_requests(client, records)
        verify_recovered_completions(records, shelfarr_database, media_root)
        cancel_active_evaluation_requests(client, records)
        if records:
            persist()
    return merge_evaluation_records(prior_records, records)


def _parse_ids(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("request IDs must be comma-separated integers") from exc
    if not result:
        raise argparse.ArgumentTypeError("at least one request ID is required")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=STACK_ROOT)
    parser.add_argument(
        "--request-ids", type=_parse_ids, default=DEFAULT_REQUEST_IDS
    )
    parser.add_argument("--monitor-seconds", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument(
        "--attempt",
        type=int,
        default=0,
        help="Correlation generation for an explicitly approved retry (0-999)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=STACK_ROOT / "state" / "shelfarr-evaluation" / "results.json",
    )
    arguments = parser.parse_args(argv)
    try:
        records = evaluate(
            arguments.root.resolve(),
            arguments.request_ids,
            monitor_seconds=max(0, arguments.monitor_seconds),
            poll_seconds=max(1, arguments.poll_seconds),
            output=arguments.output,
            attempt=arguments.attempt,
        )
    except (BootstrapError, ServiceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    succeeded = sum(record.download_result == "success" for record in records)
    terminal = sum(
        record.download_result not in UNRESOLVED_RESULTS
        for record in records
    )
    print(
        f"Shelfarr evaluation: {succeeded}/{len(records)} final-DAS successes; "
        f"{terminal}/{len(records)} terminal observations. Results: {arguments.output}"
    )
    if any(record.download_result == "cleanup_failed" for record in records):
        return 3
    return 0 if terminal == len(records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
