"""Deterministic title normalization and candidate ranking."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def normalize_identity_text(value: Any) -> str:
    """Normalize case/spacing while preserving identity-bearing punctuation."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


FORMAT_HINTS = {
    "ebooks": ("epub", "pdf", "mobi", "azw", "azw3"),
    "audiobooks": ("m4b", "mp3", "aac", "flac", "audiobook"),
    "manga-comics": ("cbz", "cbr", "pdf", "comic", "manga"),
    "roms": ("rom", "iso", "chd", "zip", "7z"),
    "sheet-music": ("pdf", "musicxml", "mxl", "sheet music", "score"),
}

# Tokens naming a file format or edition qualifier rather than a work. Derived
# from FORMAT_HINTS so the two cannot drift apart, plus the spellings people
# actually type that the ranker has no hint for.
FORMAT_ONLY_TOKENS = frozenset(
    token
    for hints in FORMAT_HINTS.values()
    for hint in hints
    for token in normalize_text(hint).split()
) | {"m4a", "ogg", "opus", "wav", "djvu", "cb7", "ebook", "book", "audio"}

# Real requested titles reach two characters -- "It", "Us", "Up" are all
# genuine works. Anything above two would reject those, and length cannot
# catch the actual problem anyway: "m4b" (3) and "epub" (4) are both LONGER
# than "It". The format-token check is what does the real work here; this
# bound only rejects a single stray character.
MINIMUM_IDENTIFYING_TITLE = 2


def identifies_a_work(title: Any, author: Any = None) -> bool:
    """Report whether this text could name a work at all.

    A reply that Huey mis-read as a new request is usually a bare format token
    or a single character. Such input cannot identify anything, so it must not
    reserve a dedup target and must not reach an acquisition service, which
    would otherwise match it against any release whose filename contains it.
    """

    # Length is measured on identity text, not on normalize_text output:
    # normalize_text keeps only [a-z0-9], so a CJK title normalizes to the
    # empty string and would otherwise be rejected outright.
    identity = normalize_identity_text(title).replace(" ", "")
    if not identity:
        return False
    tokens = normalize_text(title).split()
    if tokens and all(token in FORMAT_ONLY_TOKENS for token in tokens):
        return False
    if normalize_identity_text(author):
        return True
    return len(identity) >= MINIMUM_IDENTIFYING_TITLE


def request_target_key(media_type: str, parsed: Mapping[str, Any]) -> str | None:
    """Return a conservative exact request identity, never a fuzzy match.

    Kind, title, author, and edition/year tokens remain part of the boundary.
    Normalization removes only superficial case and whitespace differences.
    Punctuation, accents, kind, author, and edition/year terms are retained to
    avoid merging titles which merely look similar.

    Text which cannot identify a work reserves no target at all. Keying a bare
    format token made one accidental request the canonical owner of every later
    reply carrying the same word.
    """

    normalized_title = normalize_identity_text(parsed.get("title"))
    if not normalized_title:
        return None
    if not identifies_a_work(parsed.get("title"), parsed.get("author")):
        return None
    return "v1:" + json.dumps(
        [
            normalize_identity_text(media_type),
            normalize_identity_text(parsed.get("kind")),
            normalized_title,
            normalize_identity_text(parsed.get("author")),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _tokens(value: str) -> set[str]:
    return set(normalize_text(value).split())


def title_similarity(wanted: str, candidate: str) -> float:
    wanted_normalized = normalize_text(wanted)
    candidate_normalized = normalize_text(candidate)
    if not wanted_normalized or not candidate_normalized:
        return 0.0
    if wanted_normalized == candidate_normalized:
        return 1.0

    wanted_tokens = _tokens(wanted_normalized)
    candidate_tokens = _tokens(candidate_normalized)
    overlap = len(wanted_tokens.intersection(candidate_tokens))
    recall = overlap / len(wanted_tokens)
    precision = overlap / len(candidate_tokens)
    token_score = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    sequence_score = SequenceMatcher(None, wanted_normalized, candidate_normalized).ratio()
    containment_bonus = 0.08 if wanted_normalized in candidate_normalized else 0.0
    return min(1.0, (0.65 * token_score) + (0.35 * sequence_score) + containment_bonus)


@dataclass(frozen=True)
class RankedCandidate:
    item: Mapping[str, Any]
    score: float
    seeders: int
    stable_key: str


def score_release(
    title: str,
    author: str | None,
    media_type: str,
    item: Mapping[str, Any],
) -> RankedCandidate:
    candidate_title = str(item.get("title") or "")
    similarity = title_similarity(title, candidate_title)
    wanted_tokens = _tokens(title)
    candidate_tokens = _tokens(candidate_title)
    # Release names commonly append author/platform/format metadata. Full query
    # token containment is a strong signal for multi-word titles, while a bare
    # one-word title stays conservative unless the caller supplied an author.
    if wanted_tokens and wanted_tokens.issubset(candidate_tokens) and (
        len(wanted_tokens) >= 2 or author
    ):
        similarity = max(similarity, 0.82)
    score = similarity * 0.82

    normalized_candidate = normalize_text(candidate_title)
    if author:
        normalized_author = normalize_text(author)
        if normalized_author and normalized_author in normalized_candidate:
            score += 0.10
        else:
            author_tokens = _tokens(normalized_author)
            author_overlap = len(author_tokens.intersection(candidate_tokens)) / max(
                len(author_tokens), 1
            )
            score += 0.06 * author_overlap

    hints = FORMAT_HINTS.get(media_type, ())
    if any(normalize_text(hint) in normalized_candidate for hint in hints):
        score += 0.04

    try:
        seeders = max(0, int(item.get("seeders") or 0))
    except (TypeError, ValueError):
        seeders = 0
    score += min(0.04, math.log1p(seeders) / math.log(1001) * 0.04)

    stable_key = "|".join(
        normalize_text(item.get(field))
        for field in ("guid", "infoHash", "indexerId", "indexer", "downloadUrl", "magnetUrl")
    ) or normalize_text(candidate_title)
    return RankedCandidate(item, min(1.0, score), seeders, stable_key)


def rank_releases(
    title: str,
    author: str | None,
    media_type: str,
    items: Iterable[Mapping[str, Any]],
) -> list[RankedCandidate]:
    ranked = [score_release(title, author, media_type, item) for item in items]
    return sorted(
        ranked,
        key=lambda candidate: (
            -candidate.score,
            -candidate.seeders,
            normalize_text(candidate.item.get("title")),
            candidate.stable_key,
        ),
    )


@dataclass(frozen=True)
class Selection:
    selected: Mapping[str, Any] | None
    reason: str
    ranked: tuple[RankedCandidate, ...]


def select_release(
    title: str,
    author: str | None,
    media_type: str,
    items: Iterable[Mapping[str, Any]],
    *,
    minimum_confidence: float = 0.70,
    runner_up_gap: float = 0.08,
) -> Selection:
    ranked = rank_releases(title, author, media_type, items)
    if not ranked:
        return Selection(None, "no_results", ())
    if ranked[0].score < minimum_confidence:
        return Selection(None, "low_confidence", tuple(ranked))
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < runner_up_gap:
        return Selection(None, "ambiguous", tuple(ranked))
    return Selection(ranked[0].item, "selected", tuple(ranked))


# A requested year orders the field; it must never empty it. TMDb's release
# year routinely disagrees with every consumer source -- "Cashback" is listed
# as 2006 by Google, Netflix and the BFI, while TMDb offers 2021, 2007 and
# 2004 and no 2006 at all -- and excluding on that left nothing for the
# automatic gate *or* the picker, so an accurate year produced strictly less
# than no year.
#
# The cap is set against the real backlog rather than a guess: among the rows
# a year fix can rescue, the weakest stripped-title score is 0.596 ("the
# wildlife" against "The Wild Life"), and 0.12 keeps it above the picker's
# 0.45 display floor even at maximum penalty, while still ordering a one-year
# miss above a two-year miss.
# The title score the automatic gate demands before it will accept a match
# without asking. Named so the picker can reuse it as the bar a lone option
# must clear to be worth confirming.
ARR_AUTO_MATCH_MIN_SIMILARITY = 0.62
YEAR_MISMATCH_PENALTY_PER_YEAR = 0.02
MAX_YEAR_MISMATCH_PENALTY = 0.12
_YEAR_TOKEN = re.compile(r"\b((?:18|19|20|21)\d{2})\b")


@dataclass(frozen=True)
class ArrCandidate:
    item: Mapping[str, Any]
    score: float
    title: str
    year: int | None
    provider_id: str
    # True when a requested year was a genuine release-year hint and this
    # candidate disagrees with it. Ranking demotes such a candidate so the
    # requester can still be offered it; the automatic gate drops it.
    year_conflict: bool = False


def rank_arr_candidates(
    title: str, items: Iterable[Mapping[str, Any]]
) -> list[ArrCandidate]:
    """Rank ARR lookup results deterministically without applying any gate.

    ``select_arr_candidate`` applies the automatic-match thresholds on top of
    this ordering.  Splitting the ranking out lets an ambiguous lookup offer the
    same ordered candidates to the requester instead of discarding them, without
    changing what qualifies as an automatic match.
    """

    def candidate_title(item: Mapping[str, Any]) -> str:
        return str(
            item.get("title")
            or item.get("artistName")
            or item.get("sortTitle")
            or item.get("originalTitle")
            or ""
        )

    year_match = _YEAR_TOKEN.search(title)
    wanted_year = int(year_match.group(1)) if year_match else None
    wanted_title = (
        " ".join((title[: year_match.start()] + " " + title[year_match.end() :]).split())
        if year_match
        else title
    )

    def candidate_year(item: Mapping[str, Any]) -> int | None:
        raw = item.get("year")
        if raw not in {None, "", 0, "0"}:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        for field in ("firstAired", "premiereDate", "inCinemas", "digitalRelease"):
            match = re.match(r"((?:18|19|20|21)\d{2})", str(item.get(field) or ""))
            if match:
                return int(match.group(1))
        return None

    scored = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_year = candidate_year(item)
        similarity = title_similarity(title, candidate_title(item))
        year_conflict = False
        if wanted_year is not None:
            # Score the title with the year token left in as well as taken
            # out, and keep whichever fits better. A candidate that matches
            # the full form is telling us the token is part of its name --
            # "Blade Runner 2049", "1917", "2012" -- not a release-year hint,
            # and it must not then be filtered against a release year no film
            # has. Taking the better of two forms is the same approach
            # AbbaClient._release_title_score already uses for release names.
            stripped_similarity = title_similarity(wanted_title, candidate_title(item))
            if stripped_similarity > similarity:
                similarity = stripped_similarity
                if item_year is None:
                    similarity = max(0.0, similarity - 0.15)
                elif item_year != wanted_year:
                    similarity = max(
                        0.0,
                        similarity
                        - min(
                            MAX_YEAR_MISMATCH_PENALTY,
                            YEAR_MISMATCH_PENALTY_PER_YEAR
                            * abs(item_year - wanted_year),
                        ),
                    )
                    year_conflict = True
        scored.append(
            (
                similarity,
                normalize_text(candidate_title(item)),
                item_year or 0,
                str(
                    item.get("tvdbId")
                    or item.get("tmdbId")
                    or item.get("foreignArtistId")
                    or item.get("id")
                    or ""
                ),
                item,
                year_conflict,
            )
        )
    # Ties break toward the most recent release. A request that names no year
    # usually means the current one, and when more candidates tie than the
    # picker can show, the oldest are the safest to drop. The year is only ever
    # consulted when score and normalized title are already equal, which makes
    # the runner-up gap zero, so this cannot change an automatic match.
    scored.sort(key=lambda value: (-value[0], value[1], -value[2], value[3]))
    return [
        ArrCandidate(
            item=value[4],
            score=value[0],
            title=candidate_title(value[4]),
            year=candidate_year(value[4]),
            provider_id=value[3],
            year_conflict=value[5],
        )
        for value in scored
    ]


def select_arr_candidate(title: str, items: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    # The automatic gate still requires the requested year. Dropping the
    # conflicting candidates here rather than during ranking is what lets the
    # picker offer them: nothing that could not auto-match before can now, and
    # nothing that auto-matched before stops.
    ranked = [
        candidate
        for candidate in rank_arr_candidates(title, items)
        if not candidate.year_conflict
    ]
    if not ranked or ranked[0].score < ARR_AUTO_MATCH_MIN_SIMILARITY:
        return None
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < 0.05:
        return None
    return ranked[0].item


def select_shelfarr_candidate(
    title: str,
    author: str | None,
    media_type: str,
    items: Iterable[Mapping[str, Any]],
    *,
    minimum_confidence: float = 0.80,
    runner_up_gap: float = 0.05,
) -> Selection:
    """Select one work-level Shelfarr metadata result conservatively.

    Shelfarr request creation accepts a metadata ``work_id`` rather than a raw
    title.  A wrong automatic choice therefore has a much larger blast radius
    than a display-only search result: it controls every later acquisition
    source and Shelfarr's final library placement.  Deduplicate identical work
    IDs, require normal book results which support the requested book type, and
    decline low-confidence or ambiguous matches for clarification in Discord.
    """

    book_type = {"ebooks": "ebook", "audiobooks": "audiobook"}.get(media_type)
    if book_type is None:
        raise ValueError(f"Unsupported Shelfarr media type: {media_type}")
    if not 0 <= minimum_confidence <= 1:
        raise ValueError("minimum_confidence must be between 0 and 1")
    if not 0 <= runner_up_gap <= 1:
        raise ValueError("runner_up_gap must be between 0 and 1")

    wanted_author = normalize_text(author)
    wanted_author_tokens = set(wanted_author.split())
    unique: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        work_id = str(item.get("work_id") or "").strip()
        candidate_title = str(item.get("title") or "").strip()
        if not work_id or not candidate_title:
            continue
        content_kind = normalize_text(item.get("content_kind") or "book")
        if content_kind != "book":
            continue
        available = item.get("available_book_types")
        if isinstance(available, (list, tuple, set)) and book_type not in {
            str(value).casefold() for value in available
        }:
            continue
        if author:
            candidate_author_tokens = set(
                normalize_text(item.get("author")).split()
            )
            if (
                not wanted_author_tokens
                or not wanted_author_tokens.issubset(candidate_author_tokens)
            ):
                continue
        # Apply every identity gate before deduplication so an invalid first
        # copy cannot shadow a later valid alias for the same work ID.
        unique.setdefault(work_id, item)

    ranked: list[RankedCandidate] = []
    for work_id, item in unique.items():
        title_score = title_similarity(title, str(item.get("title") or ""))
        if author:
            candidate_author = str(item.get("author") or "")
            author_score = title_similarity(author, candidate_author)
            score = (0.74 * title_score) + (0.26 * author_score)
            if (
                normalize_text(title) == normalize_text(item.get("title"))
                and wanted_author
                and wanted_author == normalize_text(candidate_author)
            ):
                score = 1.0
        else:
            score = title_score
        ranked.append(
            RankedCandidate(
                item=item,
                score=max(0.0, min(1.0, score)),
                seeders=0,
                stable_key=work_id,
            )
        )

    ranked.sort(
        key=lambda candidate: (
            -candidate.score,
            normalize_text(candidate.item.get("title")),
            normalize_text(candidate.item.get("author")),
            candidate.stable_key,
        )
    )
    if not ranked:
        return Selection(None, "no_results", ())
    if ranked[0].score < minimum_confidence:
        return Selection(None, "low_confidence", tuple(ranked))
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < runner_up_gap:
        return Selection(None, "ambiguous", tuple(ranked))
    return Selection(ranked[0].item, "selected", tuple(ranked))
