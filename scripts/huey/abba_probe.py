#!/usr/bin/env python3
"""Read-only probe of what AudioBookBay actually returns for a request.

Answers one question and asks nothing of the operator: for a title Huey
declined, what came back, what did each release score, and which gate stopped
it. It performs the same parse, the same search, the same ranking and the same
picker gates Huey performs, and then stops. ``title_score`` and ``recall`` are
the picker's own quantities, unpromoted; ``gate_score`` is what the confidence
bar weighed, with complete agreement credited. They differ by design, and a
decline is explained by the second. Nothing is graded, grabbed,
queued, or written -- there is no code path here that reaches ``/api/grab``.

Run it where Huey runs, so it sees the same ABBA sidecar:

    python3 abba_probe.py --title "Kaiju: Battlefield Surgeon by Matt Dinniman"

Pass --title once per request to compare several phrasings of one book.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

try:
    from .clients import AbbaClient, ServiceError
    from .matching import COMPLETE_AGREEMENT_BONUS, title_agreement
    from .parser import RequestParseError, parse_request
    from .services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import AbbaClient, ServiceError
    from matching import COMPLETE_AGREEMENT_BONUS, title_agreement
    from parser import RequestParseError, parse_request
    from services import ServiceRegistry


MEDIA_TYPE = "audiobooks"


def probe_one(client: AbbaClient, raw: str) -> dict[str, Any]:
    """Report the live result set for one request exactly as Huey reads it."""

    try:
        parsed = parse_request(raw, MEDIA_TYPE)
    except RequestParseError as error:
        return {"request": raw, "error": f"parse failed: {error}"}
    title = str(parsed["title"])
    author = parsed["author"]
    try:
        results = client.search(title, author)
    except ServiceError as error:
        # The failure #289/#290 hit. Report it rather than dying, so probing
        # several phrasings still produces a comparable table.
        return {
            "request": raw,
            "parsed_title": title,
            "parsed_author": author,
            "search_error": str(error),
        }

    selection = client._selection(title, author, results)
    # The blended score the confidence gate actually read, per release. Without
    # this the probe reports the picker's unpromoted title score for a decline
    # the gate made on a promoted one, and the operator investigating "why was
    # this declined" is shown a number that did not decide anything.
    gate_scores = {
        str(ranked.item.get("id") or ""): ranked.score for ranked in selection.ranked
    }
    releases = []
    for item in results:
        # Unpromoted, deliberately: the picker's display floors read this
        # quantity, and agreement is credited to the ranking, not to them.
        score = AbbaClient._release_title_score(title, item, author)
        recall = title_agreement(title, item.get("title"))
        blocked = []
        if score < AbbaClient.PICKER_MIN_TITLE_SCORE:
            blocked.append(f"title score {score:.3f} < {AbbaClient.PICKER_MIN_TITLE_SCORE}")
        if recall < AbbaClient.PICKER_MIN_TITLE_RECALL:
            blocked.append(f"recall {recall:.3f} < {AbbaClient.PICKER_MIN_TITLE_RECALL}")
        try:
            AbbaClient._candidate_snapshot(item)
        except ServiceError as error:
            blocked.append(f"unrenderable: {error}")
        releases.append(
            {
                "release_title": item.get("title"),
                "author": item.get("author"),
                "year": item.get("year"),
                "format": item.get("format"),
                "title_score": round(score, 3),
                "recall": round(recall, 3),
                # What the confidence gate weighed: the title score with
                # complete agreement credited, blended with author evidence.
                "gate_score": round(
                    gate_scores.get(str(item.get("id") or ""), 0.0), 4
                ),
                "blocked_by": blocked or None,
            }
        )
    releases.sort(key=lambda row: row["title_score"], reverse=True)
    offerable = [row for row in releases if row["blocked_by"] is None]
    return {
        "request": raw,
        "parsed_title": title,
        "parsed_author": author,
        "returned": len(releases),
        "offerable": len(offerable),
        # What Huey actually decided, rather than what the picker gates alone
        # would suggest: a release can clear every gate below and still be
        # declined by the confidence bar, or clear the bar and be acquired.
        "selection": selection.reason,
        # Not inferred from the count above: a lone survivor is offered as a
        # confirmation only when it cleared the confidence bar, and two
        # listings rendering one label are settled rather than offered. Ask
        # the client what it would do instead of restating its rules here.
        "would_offer": [
            str(option["label"])
            for option in client._selection_proposal(title, author, selection)
        ],
        "indistinguishable_listings": len(
            client._indistinguishable_band(selection)
        ),
        "releases": releases,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--title",
        action="append",
        required=True,
        help="Request text exactly as it was typed in Discord; repeatable",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    client = ServiceRegistry(dict(os.environ)).abba()
    value = {
        "floors": {
            "PICKER_MIN_TITLE_SCORE": AbbaClient.PICKER_MIN_TITLE_SCORE,
            "PICKER_MIN_TITLE_RECALL": AbbaClient.PICKER_MIN_TITLE_RECALL,
            "minimum_confidence": client.minimum_confidence,
            "runner_up_gap": client.runner_up_gap,
            "COMPLETE_AGREEMENT_BONUS": COMPLETE_AGREEMENT_BONUS,
        },
        "probes": [probe_one(client, raw) for raw in arguments.title],
    }
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
