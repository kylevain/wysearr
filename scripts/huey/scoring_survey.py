#!/usr/bin/env python3
"""Read-only survey: what changes if title-score agreement counts as evidence.

Today's scoring is one-sided. A candidate is penalised for disagreeing with the
requester -- a wrong year demotes it -- but nothing is credited for agreeing.
So a release which contains *every word the requester typed* and merely adds a
subtitle is scored as if the extra words were errors. Request #288 lost that
way: 0.767 on the title, 0.8182 blended, against a 0.82 automatic bar, with
recall 1.000.

The change this surveys is a bounded promotion, mirroring the year rule's
bounded demotion: when recall is complete -- every token of the requested title
survives in the candidate's -- the title score is raised by a fixed bonus,
capped at 1.0. Nothing else moves. The sweep runs 0.00 to 0.12 in steps of
0.02, which is the year rule's own step and its own cap, so no new number is
introduced by the survey itself.

The promotion has since shipped at ``COMPLETE_AGREEMENT_BONUS``, so every
projection here is a delta from *that* column, not from zero. A zero-bonus
column stopped describing live behaviour the moment it landed, and the
self-check below is run against the shipped value for the same reason.

**Nothing here writes.** The database is opened read-only, only search and
lookup endpoints are called, and there is no code path to an add, a grab, or a
Discord post. It exists to be read before anyone edits a scoring formula,
because raising scores moves *auto-accept* outcomes and that is a different
class of risk from a picker change.

Run it where Huey runs, so it sees the same database and the same sidecars:

    python3 scoring_survey.py --database /state/huey.db

The interesting number is not how many rows improve. It is how many rows move
into ``auto_match``, because those acquire without anyone being asked.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from .matching import (
        COMPLETE_AGREEMENT_BONUS,
        RankedCandidate,
        Selection,
        normalize_text,
        title_agreement,
        title_similarity,
    )
    from .parser import RequestParseError, parse_request
    from .results import sanitize_display_text
    from .services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from matching import (
        COMPLETE_AGREEMENT_BONUS,
        RankedCandidate,
        Selection,
        normalize_text,
        title_agreement,
        title_similarity,
    )
    from parser import RequestParseError, parse_request
    from results import sanitize_display_text
    from services import ServiceRegistry


MEDIA_TYPES = ("audiobooks", "ebooks")
# The year rule demotes 0.02 per year to a cap of 0.12. An agreement bonus is
# the same kind of adjustment in the other direction, so it is swept over the
# same step and the same ceiling rather than over a range invented here.
AGREEMENT_STEP = 0.02
AGREEMENT_CAP = 0.12
BONUSES = tuple(
    sorted(
        {
            round(AGREEMENT_STEP * step, 2)
            for step in range(int(round(AGREEMENT_CAP / AGREEMENT_STEP)) + 1)
        }
        # Whatever is shipped has to be in the sweep, or the baseline every
        # projection is measured against would not be in the table.
        | {round(COMPLETE_AGREEMENT_BONUS, 2)}
    )
)
# The bonus production already applies. Every projection is a delta from here,
# not from zero: a zero-bonus column stopped describing live behaviour the
# moment the promotion shipped.
BASELINE = f"{round(COMPLETE_AGREEMENT_BONUS, 2):.2f}"
# Rows that already committed to an acquisition. A scoring change can only be
# evaluated against these as a regression check: raising scores unevenly can
# close a runner-up gap and turn a working auto-match into a prompt.
COMMITTED_STATUSES = ("queued", "complete", "completed")
# Rows the change is meant to help.
STUCK_STATUSES = ("needs_selection", "failed")

AUTO_MATCH = "auto_match"


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open Huey's database read-only, so a survey cannot write by accident."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def survey_rows(
    connection: sqlite3.Connection, statuses: Iterable[str]
) -> list[dict[str, Any]]:
    """Return every non-aliased book row in the requested statuses.

    Aliased rows are excluded for the same reason the re-drive excludes them:
    they are owned by another request, so their own score decides nothing.
    """

    placeholders = ", ".join("?" for _ in tuple(statuses))
    rows = connection.execute(
        f"""
        SELECT id, media_type, status, external_status, raw_request, title,
               service, created_at
        FROM requests
        WHERE media_type IN ('audiobooks', 'ebooks')
          AND status IN ({placeholders})
          AND canonical_request_id IS NULL
        ORDER BY media_type, id
        """,
        tuple(statuses),
    ).fetchall()
    return [dict(row) for row in rows]


def promote(score: float, agreement: float, bonus: float) -> float:
    """Credit complete agreement, and only complete agreement.

    The shipped rule lives in ``matching.agreement_promoted`` and is fixed at
    one bonus. This is the same rule with the bonus left as a knob, so a sweep
    can be run without editing production scoring; ``title_agreement`` is
    imported rather than restated so the two cannot disagree about what
    complete agreement means.
    """

    if bonus <= 0 or agreement < 1.0:
        return score
    return min(1.0, score + bonus)


def abba_ranked(
    client: Any,
    title: str,
    author: str | None,
    ranked: tuple[Any, ...],
    bonus: float,
) -> tuple[RankedCandidate, ...]:
    """Re-score ABBA's own ranked candidates, mirroring ``_selection``.

    Only the score formula is duplicated. Everything upstream of it -- the
    search, the sanitising, the variant extraction, the author evidence test --
    is the client's, and ``bonus=0`` is asserted against the client's own
    numbers before any of this is believed.
    """

    rescored: list[RankedCandidate] = []
    for candidate in ranked:
        title_score = promote(
            client._release_title_score(title, candidate.item, author),
            title_agreement(title, candidate.item.get("title")),
            bonus,
        )
        if author:
            author_score = (
                1.0 if client._author_is_evidenced(author, candidate.item) else 0.0
            )
            score = (0.78 * title_score) + (0.22 * author_score)
        else:
            score = title_score
        rescored.append(
            RankedCandidate(
                item=candidate.item,
                score=max(0.0, min(1.0, score)),
                seeders=0,
                stable_key=candidate.stable_key,
            )
        )
    rescored.sort(
        key=lambda item: (
            -item.score,
            normalize_text(item.item.get("title")),
            item.stable_key,
        )
    )
    return tuple(rescored)


def book_ranked(
    title: str,
    author: str | None,
    ranked: tuple[Any, ...],
    bonus: float,
) -> tuple[RankedCandidate, ...]:
    """Re-score Shelfarr/LazyLibrarian candidates, mirroring the selector."""

    wanted_author = normalize_text(author)
    rescored: list[RankedCandidate] = []
    for candidate in ranked:
        item = candidate.item
        candidate_title = str(item.get("title") or "")
        title_score = promote(
            title_similarity(title, candidate_title),
            title_agreement(title, candidate_title),
            bonus,
        )
        if author:
            candidate_author = str(item.get("author") or "")
            score = (0.74 * title_score) + (
                0.26 * title_similarity(author, candidate_author)
            )
            if (
                normalize_text(title) == normalize_text(candidate_title)
                and wanted_author
                and wanted_author == normalize_text(candidate_author)
            ):
                score = 1.0
        else:
            score = title_score
        rescored.append(
            RankedCandidate(
                item=item,
                score=max(0.0, min(1.0, score)),
                seeders=0,
                stable_key=candidate.stable_key,
            )
        )
    rescored.sort(
        key=lambda candidate: (
            -candidate.score,
            normalize_text(candidate.item.get("title")),
            normalize_text(candidate.item.get("author")),
            candidate.stable_key,
        )
    )
    return tuple(rescored)


def gated(
    ranked: tuple[RankedCandidate, ...],
    *,
    minimum_confidence: float,
    runner_up_gap: float,
) -> Selection:
    """Apply the two gates every book selector applies, unchanged."""

    if not ranked:
        return Selection(None, "no_results", ())
    if ranked[0].score < minimum_confidence:
        return Selection(None, "low_confidence", ranked)
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < runner_up_gap:
        return Selection(None, "ambiguous", ranked)
    return Selection(ranked[0].item, "selected", ranked)


def abba_outcome(
    client: Any, title: str, author: str | None, selection: Selection
) -> str:
    """Classify one ABBA verdict the way ``AbbaClient.submit`` branches."""

    if selection.selected is not None:
        return AUTO_MATCH
    proposal = client._selection_proposal(title, author, selection)
    if len(proposal) >= 2:
        return f"picker_{len(proposal)}"
    if client._indistinguishable_band(selection):
        return "auto_settled_identical"
    if proposal:
        return "lone_confirmation"
    return f"decline_{selection.reason}"


def book_outcome(client: Any, media_type: str, selection: Selection) -> str:
    """Classify one Shelfarr/LazyLibrarian verdict the way ``submit`` branches."""

    if selection.selected is not None:
        return AUTO_MATCH
    try:
        proposal = client._selection_proposal(selection, media_type)
    except TypeError:  # LazyLibrarian's proposal takes the selection alone.
        proposal = client._selection_proposal(selection)
    if proposal:
        return f"picker_{len(proposal)}"
    return f"decline_{selection.reason}"


class Surveyor:
    """One live search per distinct request text, re-scored at every bonus."""

    def __init__(self, services: Any, *, pause: float, sleep: Any = time.sleep):
        self.services = services
        self.pause = pause
        self.sleep = sleep
        self._searched: dict[tuple[str, str, str], Any] = {}

    def _client(self, media_type: str) -> tuple[Any, str]:
        if media_type == "audiobooks":
            return self.services.abba(), "abba"
        if self.services.shelfarr_enabled:
            return self.services.shelfarr(), "shelfarr"
        return self.services.lazylibrarian(), "lazylibrarian"

    def _search(
        self, client: Any, media_type: str, title: str, author: str | None
    ) -> Any:
        key = (media_type, title, author or "")
        if key not in self._searched:
            if self._searched:
                # ABB is scraped, not an API with a quota to spend freely.
                self.sleep(self.pause)
            self._searched[key] = client.search(title, author)
        return self._searched[key]

    def row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "request_id": int(row["id"]),
            "media_type": str(row["media_type"]),
            "status": str(row["status"]),
            "external_status": row["external_status"],
            "raw_request": sanitize_display_text(row["raw_request"], limit=200),
        }
        try:
            parsed = parse_request(str(row["raw_request"] or ""), str(row["media_type"]))
        except RequestParseError as error:
            return {**record, "outcomes": None, "skipped": f"unparsed: {error}"}
        title = str(parsed["title"])
        author = parsed["author"]
        record.update({"title": title, "author": author})
        media_type = str(row["media_type"])
        try:
            client, service = self._client(media_type)
            candidates = self._search(client, media_type, title, author)
        except Exception as error:  # One dead upstream must not end the survey.
            return {
                **record,
                "outcomes": None,
                "skipped": f"search failed: {type(error).__name__}",
            }
        record["service"] = service

        if media_type == "audiobooks":
            live = client._selection(title, author, candidates)
            rank = lambda bonus: abba_ranked(client, title, author, live.ranked, bonus)
            verdict = lambda selection: abba_outcome(client, title, author, selection)
        else:
            from_selector = _book_selection(client, title, author, candidates)
            live = from_selector
            rank = lambda bonus: book_ranked(title, author, live.ranked, bonus)
            verdict = lambda selection: book_outcome(client, media_type, selection)

        # The survey's own arithmetic has to reproduce the client's before any
        # of its projections mean anything. A drift here is a bug in the
        # survey, and it is reported as one rather than averaged into a count.
        baseline = rank(COMPLETE_AGREEMENT_BONUS)
        for original, recomputed in zip(live.ranked, baseline):
            if abs(original.score - recomputed.score) > 1e-9:
                return {
                    **record,
                    "outcomes": None,
                    "skipped": (
                        "survey scoring disagrees with the client "
                        f"({original.score:.6f} vs {recomputed.score:.6f})"
                    ),
                }

        outcomes: dict[str, str] = {}
        for bonus in BONUSES:
            selection = gated(
                rank(bonus),
                minimum_confidence=client.minimum_confidence,
                runner_up_gap=client.runner_up_gap,
            )
            outcomes[f"{bonus:.2f}"] = verdict(selection)
        record["outcomes"] = outcomes
        record["top_score"] = round(live.ranked[0].score, 4) if live.ranked else None
        record["top_title"] = (
            sanitize_display_text(live.ranked[0].item.get("title"), limit=160)
            if live.ranked
            else None
        )
        return record


def _book_selection(client: Any, title: str, author: str | None, candidates: Any):
    """Run the real Shelfarr/LazyLibrarian selector for its filtered ranking."""

    try:
        from .matching import select_shelfarr_candidate
    except ImportError:  # pragma: no cover - direct container entrypoint
        from matching import select_shelfarr_candidate
    return select_shelfarr_candidate(
        title,
        author,
        "ebooks",
        candidates,
        minimum_confidence=client.minimum_confidence,
        runner_up_gap=client.runner_up_gap,
    )


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Count what each bonus changes, and what it changes *into*."""

    summary: dict[str, Any] = {}
    for bonus in BONUSES:
        key = f"{bonus:.2f}"
        if key == BASELINE:
            continue
        changed = [
            record
            for record in records
            if record.get("outcomes")
            and record["outcomes"][key] != record["outcomes"][BASELINE]
        ]
        transitions: dict[str, int] = {}
        for record in changed:
            move = f"{record['outcomes'][BASELINE]} -> {record['outcomes'][key]}"
            transitions[move] = transitions.get(move, 0) + 1
        summary[key] = {
            "changed": len(changed),
            "into_auto_match": sum(
                1 for record in changed if record["outcomes"][key] == AUTO_MATCH
            ),
            "out_of_auto_match": sum(
                1 for record in changed if record["outcomes"][BASELINE] == AUTO_MATCH
            ),
            "changed_by_media": _counted(record["media_type"] for record in changed),
            "changed_by_status": _counted(record["status"] for record in changed),
            "transitions": dict(sorted(transitions.items())),
            "request_ids": [record["request_id"] for record in changed],
        }
    return summary


def _counted(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def report(
    database: Path,
    environment: Mapping[str, str],
    *,
    statuses: tuple[str, ...],
    limit: int | None,
    pause: float,
) -> dict[str, Any]:
    with closing(open_readonly(database)) as connection:
        rows = survey_rows(connection, statuses)
    if limit is not None:
        rows = rows[:limit]
    surveyor = Surveyor(ServiceRegistry(dict(environment)), pause=pause)
    records = [surveyor.row(row) for row in rows]
    return {
        "baseline": BASELINE,
        "bonuses": [f"{bonus:.2f}" for bonus in BONUSES],
        "statuses": list(statuses),
        "surveyed": len(records),
        "skipped": _counted(
            str(record["skipped"]).split(":")[0]
            for record in records
            if record.get("skipped")
        ),
        "baseline_outcomes": _counted(
            record["outcomes"][BASELINE] for record in records if record.get("outcomes")
        ),
        "by_bonus": summarize(records),
        "rows": records,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="/state/huey.db")
    parser.add_argument(
        "--statuses",
        default="stuck",
        choices=("stuck", "committed", "all"),
        help="stuck: the rows the change is meant to help. committed: the "
        "regression check. all: both.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--pause",
        type=float,
        default=1.0,
        help="Seconds between distinct live searches (default 1.0)",
    )
    return parser


def main() -> int:
    import os

    arguments = _parser().parse_args()
    statuses = {
        "stuck": STUCK_STATUSES,
        "committed": COMMITTED_STATUSES,
        "all": STUCK_STATUSES + COMMITTED_STATUSES,
    }[arguments.statuses]
    value = report(
        Path(arguments.database),
        dict(os.environ),
        statuses=tuple(statuses),
        limit=arguments.limit,
        pause=arguments.pause,
    )
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
