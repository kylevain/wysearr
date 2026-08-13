"""Common structured handler results."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


RESULT_STATUSES = frozenset(
    {"queued", "awaiting_selection", "needs_selection", "failed", "completed"}
)
_SENSITIVE_DISPLAY_TEXT = re.compile(
    r"(?:https?://|ftp://|www\.|discord\.gg/|magnet:|"
    r"(?:api[\s_-]*key|token|password|secret|authorization)\s*[:=]|"
    r"authorization\s+bearer\s+)",
    re.IGNORECASE,
)


def sanitize_display_text(value: object, *, limit: int = 160) -> str | None:
    """Return inert bounded text, or ``None`` when metadata is sensitive/empty."""

    text = "".join(
        character for character in str(value or "") if character.isprintable()
    )
    text = " ".join(text.split())
    if not text or _SENSITIVE_DISPLAY_TEXT.search(text):
        return None
    text = (
        text.replace("@", "＠")
        .replace("`", "'")
        .replace("<", "‹")
        .replace(">", "›")
    )
    bounded_limit = max(16, min(int(limit), 500))
    if len(text) > bounded_limit:
        text = text[: bounded_limit - 3].rstrip() + "..."
    return text


def safe_display_title(value: object, fallback: object = None) -> str:
    """Prefer sanitized service metadata, then sanitized request text."""

    return (
        sanitize_display_text(value)
        or sanitize_display_text(fallback)
        or "your request"
    )


_SELECTION_FINGERPRINT = re.compile(r"\A[0-9a-f]{64}\Z")
_SELECTION_WORK_ID = re.compile(
    r"\A(?:hardcover|google_books|openlibrary):[A-Za-z0-9][A-Za-z0-9._:-]{0,230}\Z"
)
_SENSITIVE_SELECTION_IDENTITY = re.compile(
    r"(?:api[_-]?key|token|password|secret|authorization)", re.IGNORECASE
)


def _normalize_selection_proposal(value: object) -> tuple[dict[str, Any], ...]:
    """Copy the small, inert Shelfarr metadata-choice contract.

    The client computes the identity fingerprint.  This boundary independently
    rejects URL-like/free-form provider data so handler normalization cannot
    accidentally persist it for a later Discord interaction.
    """

    if value in (None, (), []):
        return ()
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 3:
        raise ValueError("Selection proposals require two or three candidates")

    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Selection proposals contain an invalid candidate")
        fingerprint = str(item.get("fingerprint") or "")
        label = sanitize_display_text(item.get("label"), limit=300)
        work_id = str(item.get("work_id") or "")
        source_work_ids = item.get("source_work_ids")
        title = sanitize_display_text(item.get("title"), limit=160)
        raw_author = item.get("author")
        author = (
            sanitize_display_text(raw_author, limit=160)
            if raw_author not in (None, "")
            else None
        )
        year = item.get("year")
        if (
            not _SELECTION_FINGERPRINT.fullmatch(fingerprint)
            or label is None
            or title is None
            or (raw_author not in (None, "") and author is None)
            or not _SELECTION_WORK_ID.fullmatch(work_id)
            or _SENSITIVE_SELECTION_IDENTITY.search(work_id)
            or not isinstance(source_work_ids, (list, tuple))
            or not 1 <= len(source_work_ids) <= 8
            or any(
                not _SELECTION_WORK_ID.fullmatch(str(source_id or ""))
                or _SENSITIVE_SELECTION_IDENTITY.search(str(source_id or ""))
                for source_id in source_work_ids
            )
            or item.get("media_type") not in {"ebooks", "audiobooks"}
            or item.get("book_type") not in {"ebook", "audiobook"}
            or item.get("content_kind") != "book"
            or isinstance(year, bool)
        ):
            raise ValueError("Selection proposals contain an invalid candidate")
        if year is not None and (not isinstance(year, int) or not 0 <= year <= 9999):
            raise ValueError("Selection proposals contain an invalid candidate")
        normalized.append(
            {
                "fingerprint": fingerprint,
                "label": label,
                "work_id": work_id,
                "source_work_ids": tuple(str(source_id) for source_id in source_work_ids),
                "title": title,
                "author": author,
                "year": year,
                "content_kind": "book",
                "media_type": item["media_type"],
                "book_type": item["book_type"],
            }
        )
    if len({item["fingerprint"] for item in normalized}) != len(normalized):
        raise ValueError("Selection proposals require distinct candidates")
    return tuple(normalized)


def result(
    status: str,
    message: str,
    *,
    service: str | None = None,
    external_id: str | int | None = None,
    external_title: str | None = None,
    external_status: str | None = None,
    manual_intervention: bool = False,
    selection_proposal: object = (),
) -> dict[str, Any]:
    if status not in RESULT_STATUSES:
        raise ValueError(f"Invalid handler result status: {status}")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Handler results require a message")
    proposal = _normalize_selection_proposal(selection_proposal)
    if status == "awaiting_selection" and not proposal:
        raise ValueError("Awaiting-selection results require candidate proposals")
    if status != "awaiting_selection" and proposal:
        raise ValueError("Only awaiting-selection results may include candidates")
    return {
        "status": status,
        "message": message.strip(),
        "service": service,
        "external_id": str(external_id) if external_id is not None else None,
        "external_title": external_title,
        "external_status": external_status,
        "manual_intervention": bool(manual_intervention),
        "selection_proposal": proposal,
    }


def normalize_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Handler returned an invalid result")
    return result(
        value.get("status", "failed"),
        value.get("message", "Request processing failed."),
        service=value.get("service"),
        external_id=value.get("external_id"),
        external_title=value.get("external_title"),
        external_status=value.get("external_status"),
        manual_intervention=value.get("manual_intervention") is True,
        selection_proposal=value.get("selection_proposal", ()),
    )
