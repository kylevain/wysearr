"""Media request handler registry."""

from __future__ import annotations

from typing import Any

from .audiobooks import handle as handle_audiobooks
from .ebooks import handle as handle_ebooks
from .manga_comics import handle as handle_manga_comics
from .movies_tv import handle as handle_movies_tv
from .music import handle as handle_music
from .roms import handle as handle_roms
from .sheet_music import handle as handle_sheet_music


HANDLERS = {
    "movies-tv": handle_movies_tv,
    "ebooks": handle_ebooks,
    "audiobooks": handle_audiobooks,
    "manga-comics": handle_manga_comics,
    "roms": handle_roms,
    "sheet-music": handle_sheet_music,
    "music": handle_music,
}


def dispatch(request: dict[str, Any], services: Any | None = None):
    media_type = request.get("media_type")
    try:
        handler = HANDLERS[media_type]
    except KeyError as error:
        raise ValueError(f"No handler configured for {media_type}") from error
    return handler(request, services)


__all__ = ["HANDLERS", "dispatch"]
