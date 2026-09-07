"""Deterministic parsing for messages posted in Huey request channels."""

from __future__ import annotations

import re


class RequestParseError(ValueError):
    """Raised when a Discord request cannot be parsed safely."""


class ReservedRequestSyntax(RequestParseError):
    """Raised for text reserved for a feature that does not exist yet.

    Deliberately a *subclass*, so every existing ``except RequestParseError``
    caller keeps its behaviour with no edit: the target-key backfill, the
    surveys and the probe all read reserved text as "identifies no work",
    which is exactly the required outcome -- it must reserve no target key
    and must never reach an acquisition service.
    """


_MOVIE_TV_RE = re.compile(r"^(movie|tv)(?:(?:\s*:\s*)|\s+)(.+)$", re.IGNORECASE)
_AUTHOR_DELIMITER_RE = re.compile(r"\s+by\s+", re.IGNORECASE)
# A publication year trailing an author, and nothing more permissive than that.
# "by Matt Dinniman 2019" is a requester being helpful; every word in the
# candidate having to contain a letter turned that into a five-word title and
# no author at all. Deliberately not general numeric tolerance: "Blink 182"
# stays rejected, because that is a name with a number in it, not a year.
_TRAILING_YEAR_RE = re.compile(r"\s+(1\d{3}|20\d{2})\Z")
_AUTHOR_MEDIA = frozenset({"ebooks", "audiobooks"})
# ``Author:`` names a person to browse, not a work to acquire, so Huey must not
# parse it at all. There is no " by " to split on, so "Author: Brandon
# Sanderson" would otherwise parse as a four-word *title*,
# clear ``identifies_a_work``, reserve a real ``target_key`` and be dispatched
# to an acquisition service, which is request #126's failure mode exactly.
#
# The colon is required. ``_MOVIE_TV_RE`` also accepts a space-separated
# prefix, but only because a prefix is mandatory in that channel; here a bare
# "Author X" form would swallow real titles.
_RESERVED_BOOK_SYNTAX = re.compile(r"^authors?\s*:", re.IGNORECASE)
# Browsing by author exists, in Pilot, which advertises ebooks, audiobooks and
# comics/manga. So the reservation covers every channel Pilot can browse, not
# just the two that split " by ": ``manga-comics`` reaches ``handle_direct``,
# which submits straight to an acquisition service, so "Author: Osamu Tezuka"
# there is the same accidental acquisition as in the book channels.
#
# Deliberately a separate set from ``_AUTHOR_MEDIA``. That set governs
# ``TITLE by AUTHOR`` splitting, which manga-comics must not gain.
_AUTHOR_BROWSE_MEDIA = _AUTHOR_MEDIA | frozenset({"manga-comics"})
# Pilot's phrasing and a directly-requestable title, per channel. The direct
# example is not decoration: manga-comics does not split " by ", so suggesting
# "Pluto by Naoki Urasawa" there would recommend the exact shape that buries an
# author inside a title (see "the parser keeps the author inside the title").
_BROWSE_EXAMPLES = {
    "ebooks": ("ebooks by Andy Weir", "`Project Hail Mary by Andy Weir`"),
    "audiobooks": ("audiobooks by Andy Weir", "`Project Hail Mary by Andy Weir`"),
    "manga-comics": ("comics by Naoki Urasawa", "`Pluto`"),
}
_DEFAULT_BROWSE_EXAMPLE = _BROWSE_EXAMPLES["ebooks"]

_NATURAL_TITLE_MEDIA = frozenset(
    {"ebooks", "audiobooks", "manga-comics", "roms", "sheet-music", "music"}
)


def _reserved_syntax_notice(media_type: str | None) -> str:
    """Build the notice, with the example matching the channel it is sent to."""

    normalized = media_type.lower() if isinstance(media_type, str) else None
    browse, example = _BROWSE_EXAMPLES.get(normalized, _DEFAULT_BROWSE_EXAMPLE)
    return (
        "Nothing was saved — Huey doesn't browse by author. Ask Pilot instead "
        f'("show me {browse}"); pick a number and it posts your choice here as '
        f"a normal request. To request one directly, send its title, like {example}."
    )


def _clean(value: str) -> str:
    return " ".join(value.strip().split())


def _is_browse_channel(media_type: str | None) -> bool:
    normalized = media_type.lower() if isinstance(media_type, str) else None
    return normalized in _AUTHOR_BROWSE_MEDIA or normalized is None


def reserved_syntax_notice(text: object, media_type: str | None = None) -> str | None:
    """Return the requester-facing notice if this text is reserved, else None.

    Exposed so the Discord layer can decline the message *before* it is
    persisted. A reserved message is not a failed request -- it is not a
    request -- so it should not land in the ``needs_selection`` backlog that
    nothing retries, and Louie should not show it as ``unparsed``.
    ``parse_request`` enforces the same rule for every other caller.
    """

    if not isinstance(text, str) or not _is_browse_channel(media_type):
        return None
    if _RESERVED_BOOK_SYNTAX.match(_clean(text)):
        return _reserved_syntax_notice(media_type)
    return None


def _split_trailing_year(value: str) -> tuple[str, int | None]:
    """Separate a trailing four-digit year from a candidate author."""

    match = _TRAILING_YEAR_RE.search(value)
    if match is None:
        return value, None
    return value[: match.start()].strip(), int(match.group(1))


def _looks_like_author(value: str) -> bool:
    """Conservatively recognize a trailing author and keep title phrases intact.

    Name-like trailing text is accepted, while pronouns are rejected so titles
    such as ``Stand by Me`` are not turned into title/author pairs.
    """

    # A trailing year is ignored rather than failing the candidate: supplying
    # more detail must never produce a worse parse than supplying less.
    words = _split_trailing_year(value)[0].split()
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

    if _RESERVED_BOOK_SYNTAX.match(cleaned) and _is_browse_channel(normalized_media_type):
        raise ReservedRequestSyntax(_reserved_syntax_notice(normalized_media_type))

    author = None
    year: int | None = None
    title = cleaned
    if normalized_media_type in _AUTHOR_MEDIA or normalized_media_type is None:
        # Prefer the last delimiter: ``Stand by Me by Stephen King`` should keep
        # the first "by" as part of the title.
        for match in reversed(list(_AUTHOR_DELIMITER_RE.finditer(cleaned))):
            possible_title = _clean(cleaned[: match.start()])
            possible_author, possible_year = _split_trailing_year(
                _clean(cleaned[match.end() :])
            )
            if possible_title and _looks_like_author(possible_author):
                title = possible_title
                author = possible_author
                year = possible_year
                break

    parsed: dict[str, str | int | None] = {"title": title, "author": author}
    if year is not None:
        # Present only when one was actually split off, so no caller's existing
        # shape changes. ``request_target_key`` reads named keys and is
        # unaffected either way.
        parsed["year"] = year
    return parsed
