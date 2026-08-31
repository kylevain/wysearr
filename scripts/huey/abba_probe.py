#!/usr/bin/env python3
"""Read-only probe of what AudioBookBay actually returns for a request.

Answers one question and asks nothing of the operator: for a title Huey
declined, what came back, what did each release score, and which gate stopped
it. It performs the same parse, the same search, the same ranking and the same
picker gates Huey performs, and then stops. Nothing is graded, grabbed,
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
    from .matching import normalize_text
    from .parser import RequestParseError, parse_request
    from .services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from clients import AbbaClient, ServiceError
    from matching import normalize_text
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

    wanted = set(normalize_text(title).split())
    releases = []
    for item in results:
        score = AbbaClient._release_title_score(title, item, author)
        present = wanted & set(normalize_text(item.get("title")).split())
        recall = len(present) / len(wanted) if wanted else 0.0
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
        # ABBA keeps a two-option picker: one survivor is not a choice.
        "would_offer_picker": len(offerable) >= 2,
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
        },
        "probes": [probe_one(client, raw) for raw in arguments.title],
    }
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
