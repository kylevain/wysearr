"""Deterministic parsing for messages posted in Huey request channels."""

from __future__ import annotations

import re


class RequestParseError(ValueError):
    """Raised when a Discord request cannot be parsed safely."""


_MOVIE_TV_RE = re.compile(r"^(movie|tv)(?:(?:\s*:\s*)|\s+)(.+)$", re.IGNORECASE)
_AUTHOR_DELIMITER_RE = re.compile(r"\s+by\s+", re.IGNORECASE)
_AUTHOR_MEDIA = frozenset({"ebooks", "audiobooks"})
_NATURAL_TITLE_MEDIA = frozenset(
    {"ebooks", "audiobooks", "manga-comics", "roms", "sheet-music", "music"}
)


def _clean(value: str) -> str:
    return " ".join(value.strip().split())


def _looks_like_author(value: str) -> bool:
    """Conservatively recognize a trailing author and keep title phrases intact.

    Name-like trailing text is accepted, while pronouns are rejected so titles
    such as ``Stand by Me`` are not turned into title/author pairs.
    """

    words = value.split()
    if len(words) >= 2:
        return all(any(character.isalpha() for character in word) for word in words)
    if not words or not any(character.isalpha() for character in words[0]):
        return False
    return words[0].casefold() not in {
        "me",
        "you",
        "us",
        "them",
        "him",
        "her",
        "it",
        "thee",
        "myself",
        "yourself",
        "ourselves",
        "themselves",
    }


def parse_request(text: str, media_type: str | None = None) -> dict[str, str | None]:
    """Parse a request into a stable mapping.

    ``movies-tv`` deliberately requires a ``movie`` or ``tv`` prefix because the
    destination service cannot be inferred reliably from a free-form title. Other
    request channels accept a natural title. Ebook and audiobook channels also
    accept a conservative ``TITLE by AUTHOR`` form.

    ``media_type=None`` retains the original parser's ebook-like behavior for
    callers that have not yet started passing channel context.
    """

    if not isinstance(text, str):
        raise RequestParseError("Request text must be a string.")

    cleaned = _clean(text)
    if not cleaned:
        raise RequestParseError("Please include a title in your request.")

    normalized_media_type = media_type.lower() if isinstance(media_type, str) else None

    if normalized_media_type == "movies-tv":
        match = _MOVIE_TV_RE.fullmatch(cleaned)
        if not match:
            raise RequestParseError(
                "Start the request with `movie:` or `tv:` (for example, `movie: Arrival`)."
            )
        title = _clean(match.group(2))
        if not title or title.startswith(":"):
            raise RequestParseError("Please include a title after `movie:` or `tv:`.")
        return {"kind": match.group(1).lower(), "title": title, "author": None}

    if normalized_media_type not in _NATURAL_TITLE_MEDIA and normalized_media_type is not None:
        raise RequestParseError(f"Unsupported request channel type: {normalized_media_type}")

    author = None
    title = cleaned
    if normalized_media_type in _AUTHOR_MEDIA or normalized_media_type is None:
        # Prefer the last delimiter: ``Stand by Me by Stephen King`` should keep
        # the first "by" as part of the title.
        for match in reversed(list(_AUTHOR_DELIMITER_RE.finditer(cleaned))):
            possible_title = _clean(cleaned[: match.start()])
            possible_author = _clean(cleaned[match.end() :])
            if possible_title and _looks_like_author(possible_author):
                title = possible_title
                author = possible_author
                break

    return {"title": title, "author": author}
