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


def request_target_key(media_type: str, parsed: Mapping[str, Any]) -> str | None:
    """Return a conservative exact request identity, never a fuzzy match.

    Kind, title, author, and edition/year tokens remain part of the boundary.
    Normalization removes only superficial case and whitespace differences.
    Punctuation, accents, kind, author, and edition/year terms are retained to
    avoid merging titles which merely look similar.
    """

    normalized_title = normalize_identity_text(parsed.get("title"))
    if not normalized_title:
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


FORMAT_HINTS = {
    "ebooks": ("epub", "pdf", "mobi", "azw", "azw3"),
    "audiobooks": ("m4b", "mp3", "aac", "flac", "audiobook"),
    "manga-comics": ("cbz", "cbr", "pdf", "comic", "manga"),
    "roms": ("rom", "iso", "chd", "zip", "7z"),
    "sheet-music": ("pdf", "musicxml", "mxl", "sheet music", "score"),
}


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


def select_arr_candidate(title: str, items: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    def candidate_title(item: Mapping[str, Any]) -> str:
        return str(
            item.get("title")
            or item.get("artistName")
            or item.get("sortTitle")
            or item.get("originalTitle")
            or ""
        )

    year_match = re.search(r"\b((?:18|19|20|21)\d{2})\b", title)
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
        item_year = candidate_year(item)
        similarity = title_similarity(wanted_title, candidate_title(item))
        if wanted_year is not None:
            if item_year is not None and item_year != wanted_year:
                continue
            if item_year is None:
                similarity = max(0.0, similarity - 0.15)
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
            )
        )
    scored.sort(key=lambda value: (-value[0], value[1], value[2], value[3]))
    if not scored or scored[0][0] < 0.62:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None
    return scored[0][4]
