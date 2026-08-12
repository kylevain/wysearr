"""Safe direct acquisition for media types without an ARR manager."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

try:
    from .clients import ProwlarrClient, QBittorrentClient, ServiceError
    from .matching import Selection, select_release
    from .results import result
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import ProwlarrClient, QBittorrentClient, ServiceError
    from matching import Selection, select_release
    from results import result


PROWLARR_CATEGORIES = {
    "ebooks": (7020, 7000),
    "audiobooks": (3030, 3000),
    "manga-comics": (7030, 7000),
    "roms": (4050, 1000, 8000),
    "sheet-music": (7010, 7000),
}
INFO_HASH = re.compile(r"^[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?$")


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
    for value in parse_qs(urlparse(magnet).query).get("xt", []):
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


def torrent_info_hash(data: bytes) -> str | None:
    if not data.startswith(b"d"):
        return None
    cursor = 1
    try:
        while cursor < len(data) and data[cursor : cursor + 1] != b"e":
            key_end = _bencode_end(data, cursor)
            colon = data.find(b":", cursor, key_end)
            if colon < 0:
                return None
            key = data[colon + 1 : key_end]
            value_start = key_end
            value_end = _bencode_end(data, value_start)
            if key == b"info":
                return hashlib.sha1(data[value_start:value_end]).hexdigest()
            cursor = value_end
    except (ValueError, TypeError):
        return None
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
                message = (
                    "Several releases matched too closely to choose safely. "
                    "Add an author, edition, platform, or format."
                )
            else:
                message = (
                    "No release matched with enough confidence. "
                    "Add an author, edition, platform, or format."
                )
            return result("needs_selection", message, service="prowlarr")

        selected = selection.selected
        selected_title = str(selected.get("title") or title)
        category = f"{self.category_prefix}{media_type}"
        tags = f"huey-{request_id}" if request_id is not None else None
        magnet = str(selected.get("magnetUrl") or "")
        download_url = str(selected.get("downloadUrl") or "")
        if not magnet and download_url.startswith("magnet:"):
            magnet = download_url

        supplied_info_hash = normalize_info_hash(selected.get("infoHash"))
        submitted_info_hash: str | None = None
        torrent: bytes | None = None
        if magnet:
            submitted_info_hash = magnet_info_hash(magnet)
        elif download_url:
            torrent = self.prowlarr.download_torrent(download_url)
            submitted_info_hash = torrent_info_hash(torrent)
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
        info_hash = submitted_info_hash or supplied_info_hash
        if not info_hash:
            return result(
                "needs_selection",
                "The best match has no stable torrent identity. Try a different or more specific release.",
                service="prowlarr",
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
            self.qbittorrent.add_tags(info_hash, tags)
        return result(
            "queued",
            f"Queued {selected_title} in qBittorrent.",
            service="qbittorrent",
            external_id=info_hash,
            external_title=selected_title,
        )
