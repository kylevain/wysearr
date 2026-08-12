"""Safe direct acquisition for media types without an ARR manager."""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

try:
    from .clients import ProwlarrClient, QBittorrentClient, ServiceError
    from .matching import Selection, select_release
    from .results import result, safe_display_title, sanitize_display_text
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import ProwlarrClient, QBittorrentClient, ServiceError
    from matching import Selection, select_release
    from results import result, safe_display_title, sanitize_display_text


PROWLARR_CATEGORIES = {
    "ebooks": (7020, 7000),
    "audiobooks": (3030, 3000),
    "manga-comics": (7030, 7000),
    "roms": (4050, 1000, 8000),
    "sheet-music": (7010, 7000),
}
INFO_HASH = re.compile(r"^[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?$")
LOGGER = logging.getLogger("huey.acquisition")
_FORMAT_WORDS = (
    "epub",
    "pdf",
    "mobi",
    "azw3",
    "azw",
    "m4b",
    "mp3",
    "flac",
    "aac",
)


class UnsupportedTorrentVersion(ValueError):
    """The payload uses an info-hash scheme Huey cannot correlate safely."""


def _human_size(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        size = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(size) or size <= 0 or size > 1024**5:
        return None
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.0f} {unit}" if unit in {"B", "KiB"} else f"{size:.1f} {unit}"


def _safe_candidate_summary(item: Mapping[str, Any]) -> str | None:
    title = sanitize_display_text(item.get("title"), limit=120)
    if title is None:
        return None

    normalized_title = title.casefold()
    details: list[str] = []
    for format_name in _FORMAT_WORDS:
        if re.search(
            rf"(?<![a-z0-9]){re.escape(format_name)}(?![a-z0-9])",
            normalized_title,
        ):
            details.append(format_name.upper())
            break
    size = _human_size(item.get("size"))
    if size:
        details.append(size)
    return f"{title} ({', '.join(details)})" if details else title


def ambiguity_message(
    selection: Selection,
    *,
    minimum_confidence: float,
    runner_up_gap: float,
) -> str:
    """Render at most three safe distinctions from the actual ambiguity band."""

    summaries: list[str] = []
    if selection.reason == "ambiguous" and selection.ranked:
        top_score = selection.ranked[0].score
        for candidate in selection.ranked:
            if candidate.score < minimum_confidence:
                continue
            if top_score - candidate.score >= runner_up_gap:
                continue
            summary = _safe_candidate_summary(candidate.item)
            if summary and summary not in summaries:
                summaries.append(summary)
            if len(summaries) == 3:
                break
    guidance = "Add an author, edition, platform, year, or format."
    if not summaries:
        return f"Several releases matched too closely to choose safely. {guidance}"
    candidates = "\n".join(f"- {summary}" for summary in summaries)
    return (
        "Several releases matched too closely to choose safely. "
        f"{guidance}\nClosest safe distinctions:\n{candidates}"
    )


def normalize_info_hash(value: object) -> str | None:
    text = str(value or "").strip()
    if INFO_HASH.fullmatch(text):
        return text.lower()
    if len(text) == 32:
        try:
            decoded = base64.b32decode(text.upper())
        except (ValueError, TypeError):
            return None
        return decoded.hex() if len(decoded) == 20 else None
    return None


def magnet_info_hash(magnet: str) -> str | None:
    exact_topics = parse_qs(urlparse(magnet).query).get("xt", [])
    if any(
        value.rpartition(":")[0].casefold().endswith("urn:btmh")
        for value in exact_topics
    ):
        # A hybrid magnet can expose both btih and btmh topics. Reject both
        # pure-v2 and hybrid sources rather than assuming which hash qBittorrent
        # will expose through its Web API for downstream correlation.
        raise UnsupportedTorrentVersion(
            "BitTorrent v2 and hybrid magnets are not supported"
        )
    for value in exact_topics:
        prefix, separator, candidate = value.rpartition(":")
        if separator and prefix.casefold().endswith("urn:btih"):
            normalized = normalize_info_hash(candidate)
            if normalized:
                return normalized
    return None


def _bencode_end(data: bytes, position: int, depth: int = 0) -> int:
    if depth > 100 or position >= len(data):
        raise ValueError("invalid bencoded torrent")
    marker = data[position : position + 1]
    if marker == b"i":
        end = data.find(b"e", position + 1)
        if end < 0:
            raise ValueError("invalid bencoded integer")
        int(data[position + 1 : end])
        return end + 1
    if marker in {b"l", b"d"}:
        cursor = position + 1
        while cursor < len(data) and data[cursor : cursor + 1] != b"e":
            cursor = _bencode_end(data, cursor, depth + 1)
            if marker == b"d":
                cursor = _bencode_end(data, cursor, depth + 1)
        if cursor >= len(data):
            raise ValueError("unterminated bencoded container")
        return cursor + 1
    colon = data.find(b":", position)
    if colon < 0:
        raise ValueError("invalid bencoded string")
    length = int(data[position:colon])
    end = colon + 1 + length
    if length < 0 or end > len(data):
        raise ValueError("invalid bencoded string length")
    return end


def _bencoded_string(data: bytes, position: int) -> tuple[bytes, int]:
    colon = data.find(b":", position)
    if colon < 0:
        raise ValueError("invalid bencoded string")
    length_bytes = data[position:colon]
    if not length_bytes or not length_bytes.isdigit():
        raise ValueError("invalid bencoded string length")
    if len(length_bytes) > 1 and length_bytes.startswith(b"0"):
        raise ValueError("noncanonical bencoded string length")
    length = int(length_bytes)
    end = colon + 1 + length
    if end > len(data):
        raise ValueError("invalid bencoded string length")
    return data[colon + 1 : end], end


def _bencoded_dictionary(
    data: bytes, position: int
) -> tuple[list[tuple[bytes, int, int]], int]:
    if data[position : position + 1] != b"d":
        raise ValueError("expected bencoded dictionary")
    items: list[tuple[bytes, int, int]] = []
    cursor = position + 1
    previous_key: bytes | None = None
    while cursor < len(data) and data[cursor : cursor + 1] != b"e":
        key, value_start = _bencoded_string(data, cursor)
        if previous_key is not None and key <= previous_key:
            raise ValueError("noncanonical bencoded dictionary")
        value_end = _bencode_end(data, value_start)
        items.append((key, value_start, value_end))
        previous_key = key
        cursor = value_end
    if cursor >= len(data):
        raise ValueError("unterminated bencoded dictionary")
    return items, cursor + 1


def torrent_info_hash(data: bytes) -> str | None:
    """Derive qBittorrent's v1 identity from the exact bencoded info bytes.

    BitTorrent v2 uses a SHA-256 identity, while hybrid torrent identity
    exposure varies between APIs. Until Huey can correlate both forms against
    qBittorrent with certainty, the BEP 52 ``meta version`` marker is rejected
    instead of guessing an identity or trusting Prowlarr metadata.
    """

    try:
        top_level, payload_end = _bencoded_dictionary(data, 0)
        if payload_end != len(data):
            return None
        info_values = [item for item in top_level if item[0] == b"info"]
        if len(info_values) != 1:
            return None
        _, info_start, info_end = info_values[0]
        info_items, parsed_info_end = _bencoded_dictionary(data, info_start)
        if parsed_info_end != info_end:
            return None
        meta_versions = [item for item in info_items if item[0] == b"meta version"]
        if meta_versions:
            _, version_start, version_end = meta_versions[0]
            if data[version_start:version_end] == b"i2e":
                raise UnsupportedTorrentVersion(
                    "BitTorrent v2 and hybrid payloads are not supported"
                )
            return None
        return hashlib.sha1(data[info_start:info_end]).hexdigest()
    except UnsupportedTorrentVersion:
        raise
    except (IndexError, ValueError, TypeError):
        return None


class DirectAcquirer:
    def __init__(
        self,
        prowlarr: ProwlarrClient,
        qbittorrent: QBittorrentClient,
        *,
        minimum_confidence: float = 0.70,
        runner_up_gap: float = 0.08,
        category_prefix: str = "",
    ):
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if not 0 <= runner_up_gap <= 1:
            raise ValueError("runner_up_gap must be between 0 and 1")
        self.prowlarr = prowlarr
        self.qbittorrent = qbittorrent
        self.minimum_confidence = minimum_confidence
        self.runner_up_gap = runner_up_gap
        self.category_prefix = category_prefix

    def choose(
        self,
        media_type: str,
        title: str,
        author: str | None,
        items: list[Mapping[str, Any]],
    ) -> Selection:
        return select_release(
            title,
            author,
            media_type,
            items,
            minimum_confidence=self.minimum_confidence,
            runner_up_gap=self.runner_up_gap,
        )

    def submit(
        self,
        media_type: str,
        title: str,
        author: str | None = None,
        request_id: int | None = None,
    ) -> dict[str, str | None]:
        categories = PROWLARR_CATEGORIES.get(media_type)
        if categories is None:
            raise ValueError(f"Unsupported direct acquisition type: {media_type}")

        query = f"{title} {author}" if author else title
        candidates = [
            candidate
            for candidate in self.prowlarr.search(query, categories)
            if str(candidate.get("downloadProtocol") or "torrent").casefold() == "torrent"
        ]
        selection = self.choose(media_type, title, author, candidates)
        if selection.selected is None:
            if selection.reason == "no_results":
                message = "No matching release was found. Check the title and try again later."
            elif selection.reason == "ambiguous":
                message = ambiguity_message(
                    selection,
                    minimum_confidence=self.minimum_confidence,
                    runner_up_gap=self.runner_up_gap,
                )
            else:
                message = (
                    "No release matched with enough confidence. "
                    "Add an author, edition, platform, or format."
                )
            return result("needs_selection", message, service="prowlarr")

        selected = selection.selected
        selected_title = safe_display_title(selected.get("title"), title)
        category = f"{self.category_prefix}{media_type}"
        tags = f"huey-{request_id}" if request_id is not None else None
        magnet = str(selected.get("magnetUrl") or "")
        download_url = str(selected.get("downloadUrl") or "")
        if not magnet and download_url.startswith("magnet:"):
            magnet = download_url

        raw_supplied_info_hash = selected.get("infoHash")
        supplied_info_hash = normalize_info_hash(raw_supplied_info_hash)
        if (
            raw_supplied_info_hash is not None
            and str(raw_supplied_info_hash).strip()
            and not supplied_info_hash
        ):
            return result(
                "needs_selection",
                "The release provides an invalid torrent identity. Try a different release.",
                service="prowlarr",
                external_title=selected_title,
            )
        submitted_info_hash: str | None = None
        torrent: bytes | None = None
        if magnet:
            try:
                submitted_info_hash = magnet_info_hash(magnet)
            except UnsupportedTorrentVersion:
                return result(
                    "needs_selection",
                    "This release uses an unsupported BitTorrent v2 or hybrid torrent. "
                    "Try a BitTorrent v1 release.",
                    service="prowlarr",
                    external_title=selected_title,
                )
        elif download_url:
            torrent = self.prowlarr.download_torrent(download_url)
            try:
                submitted_info_hash = torrent_info_hash(torrent)
            except UnsupportedTorrentVersion:
                return result(
                    "needs_selection",
                    "This release uses an unsupported BitTorrent v2 or hybrid torrent. "
                    "Try a BitTorrent v1 release.",
                    service="prowlarr",
                    external_title=selected_title,
                )
            if not submitted_info_hash:
                return result(
                    "needs_selection",
                    "The downloaded torrent has no verifiable payload identity. "
                    "Try a different release.",
                    service="prowlarr",
                    external_title=selected_title,
                )
        if (
            supplied_info_hash
            and submitted_info_hash
            and supplied_info_hash != submitted_info_hash
        ):
            return result(
                "needs_selection",
                "The release identity does not match its download source. Try a different release.",
                service="prowlarr",
                external_title=selected_title,
            )
        # Prowlarr's infoHash is useful only as a consistency check. The
        # submitted source must independently prove the correlation identity.
        info_hash = submitted_info_hash
        if not info_hash:
            return result(
                "needs_selection",
                "The best match has no stable torrent identity. Try a different or more specific release.",
                service="prowlarr",
                external_title=selected_title,
            )

        existing = self.qbittorrent.find_torrent(info_hash)
        if existing is not None:
            existing_category = str(existing.get("category") or "")
            allowed_categories = {category, f"{category}-imported"}
            if existing_category not in allowed_categories:
                return result(
                    "needs_selection",
                    "That exact torrent already exists outside this media category. "
                    "An administrator must review it before retrying.",
                    service="qbittorrent",
                    external_id=info_hash,
                    external_title=selected_title,
                )
            if tags:
                try:
                    self.qbittorrent.add_tags(info_hash, tags)
                except ServiceError:
                    # Persisting the exact hash still lets BookBot reconcile a
                    # safe imported ledger entry if the tag update is delayed.
                    LOGGER.warning(
                        "qBittorrent already had a request but deferred its correlation tag"
                    )
            if existing_category.endswith("-imported"):
                message = (
                    f"{selected_title} already has an imported qBittorrent record; "
                    "BookBot will verify its import ledger."
                )
            else:
                message = f"{selected_title} is already queued in qBittorrent."
            return result(
                "queued",
                message,
                service="qbittorrent",
                external_id=info_hash,
                external_title=selected_title,
            )

        if magnet:
            self.qbittorrent.add_magnet(magnet, category, tags)
        elif torrent is not None:
            self.qbittorrent.add_torrent(torrent, category, tags)
        else:
            return result(
                "needs_selection",
                "The best match has no usable download source. Try a more specific request.",
                service="prowlarr",
                external_title=selected_title,
            )

        if tags:
            try:
                self.qbittorrent.add_tags(info_hash, tags)
            except ServiceError:
                # The add already succeeded. Returning the stable hash lets
                # BookBot reconcile this request even if qBittorrent has not
                # materialized the torrent quickly enough for tagging.
                LOGGER.warning(
                    "qBittorrent accepted a request but deferred its correlation tag"
                )
        return result(
            "queued",
            f"Queued {selected_title} in qBittorrent.",
            service="qbittorrent",
            external_id=info_hash,
            external_title=selected_title,
        )
