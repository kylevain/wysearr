"""Safe direct acquisition for media types without an ARR manager."""

from __future__ import annotations

from typing import Any, Mapping

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

        if magnet:
            self.qbittorrent.add_magnet(magnet, category, tags)
        elif download_url:
            torrent = self.prowlarr.download_torrent(download_url)
            self.qbittorrent.add_torrent(torrent, category, tags)
        else:
            return result(
                "needs_selection",
                "The best match has no usable download source. Try a more specific request.",
                service="prowlarr",
                external_title=selected_title,
            )

        external_id = (
            selected.get("infoHash")
            or selected.get("guid")
            or selected.get("indexerId")
            or selected_title
        )
        return result(
            "queued",
            f"Queued {selected_title} in qBittorrent.",
            service="qbittorrent",
            external_id=external_id,
            external_title=selected_title,
        )
