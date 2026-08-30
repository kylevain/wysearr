#!/usr/bin/env python3
"""Read-only survey of movies-tv requests stuck in ``needs_selection``.

These rows predate the candidate picker. Nothing re-drives them, so they are
inert: titles somebody asked for that never arrived. This reports what the ARR
lookup returns for each one *now* -- whether it would auto-match, offer a
picker, or still bail -- without writing to the database or mutating any
upstream.

It deliberately does not post anything. Re-driving is a separate, explicit
step; this exists so the list can be read before a single Discord message is
sent.

Run it where Huey runs, so it sees the same database and the same ARR hosts:

    python3 redrive_selection.py report --database /state/huey.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

import requests

try:
    from .database import TERMINAL_CONFIRMATION_STATUSES, RequestStore
    from .huey import format_candidate_prompt
    from .matching import rank_arr_candidates, select_arr_candidate
    from .parser import RequestParseError, parse_request
    from .results import sanitize_display_text
    from .services import ServiceRegistry
except ImportError:  # pragma: no cover - direct container entrypoint
    from database import TERMINAL_CONFIRMATION_STATUSES, RequestStore
    from huey import format_candidate_prompt
    from matching import rank_arr_candidates, select_arr_candidate
    from parser import RequestParseError, parse_request
    from results import sanitize_display_text
    from services import ServiceRegistry


MEDIA_TYPE = "movies-tv"
# Huey's own parser-failure text. Those rows are a different problem and are
# explicitly out of scope: they are reported as skipped, never classified.
PARSER_ERROR_MARKER = "Start the request with"
ARR_SERVICES = {"movie": "radarr", "tv": "sonarr"}

# Every outcome a row can be assigned. Anything not in ``REDRIVABLE`` must not
# be re-driven, for a reason the report states per row.
REDRIVABLE = ("auto_match", "picker")
TERMINAL = ("still_bails", "lookup_failed", "blocked_by_prior_prompt", "skipped_unparsed")


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open Huey's database read-only, so a survey cannot write by accident."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def stuck_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every movies-tv row sitting in needs_selection.

    Canonical duplicates are excluded: an aliased row is owned by another
    request and re-driving it would post a prompt nobody can answer.
    """

    rows = connection.execute(
        """
        SELECT requests.id, requests.channel_id, requests.discord_user_id,
               requests.discord_username, requests.raw_request, requests.title,
               requests.error, requests.created_at, requests.updated_at,
               (
                   SELECT candidate_confirmations.status
                   FROM candidate_confirmations
                   WHERE candidate_confirmations.request_id = requests.id
                   LIMIT 1
               ) AS prior_prompt
        FROM requests
        WHERE requests.media_type = ?
          AND requests.status = 'needs_selection'
          AND requests.canonical_request_id IS NULL
        ORDER BY requests.id
        """,
        (MEDIA_TYPE,),
    ).fetchall()
    return [dict(row) for row in rows]


def _ranked_summary(ranked: list[Any], limit: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "title": sanitize_display_text(candidate.title, limit=160),
            "year": candidate.year,
            "score": round(candidate.score, 3),
        }
        for candidate in ranked[:limit]
    ]


def classify(client: Any, title: str) -> dict[str, Any]:
    """Run the same three steps ``ArrClient.submit`` runs, minus the mutation.

    ``submit`` does lookup -> select_arr_candidate -> _selection_proposal, and
    then adds the selection to the ARR. This stops before the add, so the
    verdict is the live one without acquiring anything.
    """

    candidates = client.lookup(title)
    ranked = rank_arr_candidates(title, candidates)
    selected = select_arr_candidate(title, candidates)
    if selected is not None:
        top = ranked[0] if ranked else None
        return {
            "outcome": "auto_match",
            "match": _ranked_summary(ranked, 1)[0] if top is not None else None,
            "ranked": _ranked_summary(ranked),
        }
    proposal = client._selection_proposal(ranked)
    if proposal:
        return {
            "outcome": "picker",
            "options": [option["label"] for option in proposal],
            "proposal": list(proposal),
            "ranked": _ranked_summary(ranked),
        }
    return {
        "outcome": "still_bails",
        "reason": (
            "no lookup results" if not ranked else "no two candidates worth offering"
        ),
        "ranked": _ranked_summary(ranked),
    }


def survey_row(row: Mapping[str, Any], services: Any) -> dict[str, Any]:
    """Classify one stuck row without touching it."""

    record: dict[str, Any] = {
        "request_id": int(row["id"]),
        "raw_request": sanitize_display_text(row["raw_request"], limit=200),
        "requested_by": row["discord_username"] or row["discord_user_id"],
        "created_at": row["created_at"],
    }
    if row["error"] and PARSER_ERROR_MARKER in str(row["error"]):
        return {**record, "outcome": "skipped_unparsed",
                "reason": "parser failure, out of scope"}
    prior = str(row["prior_prompt"] or "")
    if prior and prior not in TERMINAL_CONFIRMATION_STATUSES:
        # A live or already-answered prompt owns the row. A finished one does
        # not: create_candidate_confirmation resets it, which is what makes an
        # expired prompt re-drivable rather than a dead end.
        return {**record, "outcome": "blocked_by_prior_prompt",
                "reason": f"prior candidate prompt is {prior}"}
    try:
        parsed = parse_request(str(row["raw_request"] or ""), MEDIA_TYPE)
    except RequestParseError as error:
        return {**record, "outcome": "skipped_unparsed", "reason": str(error)}

    kind = str(parsed.get("kind") or "")
    service = ARR_SERVICES.get(kind)
    if service is None:
        return {**record, "outcome": "skipped_unparsed",
                "reason": "no movie/tv kind to route on"}
    record.update({"kind": kind, "service": service, "title": parsed["title"]})
    try:
        return {**record, **classify(services.arr(service), str(parsed["title"]))}
    except Exception as error:  # Never let one dead upstream end the survey.
        return {**record, "outcome": "lookup_failed",
                "reason": f"{type(error).__name__}"}


def report(database: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    services = ServiceRegistry(dict(environment))
    with closing(open_readonly(database)) as connection:
        rows = stuck_rows(connection)
    records = [survey_row(row, services) for row in rows]
    counts: dict[str, int] = {}
    for record in records:
        counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1
    return {
        "total": len(records),
        "counts": counts,
        "redrivable": sum(counts.get(name, 0) for name in REDRIVABLE),
        "rows": records,
    }


class DiscordPoster:
    """Post one message to one channel as the Huey bot, and nothing else.

    The backfill runs outside the gateway process, so it cannot reuse the live
    client object. It uses the same bot identity and the same channel, and it
    has no other capability: there is no edit, no delete, and no way to name a
    channel other than the one the operator passed on the command line.
    """

    API = "https://discord.com/api/v10"

    def __init__(self, token: str, *, timeout: float = 20.0):
        if not token.strip():
            raise ValueError("DISCORD_BOT_TOKEN is required to post prompts")
        self.token = token.strip()
        self.timeout = timeout

    def post(self, channel_id: str, content: str) -> str:
        response = requests.post(
            f"{self.API}/channels/{int(channel_id)}/messages",
            headers={
                "Authorization": f"Bot {self.token}",
                "Content-Type": "application/json",
            },
            json={"content": content},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            # Never echo the body: it can carry the token back in an error.
            raise RuntimeError(f"Discord rejected the prompt ({response.status_code})")
        message_id = str((response.json() or {}).get("id") or "")
        if not message_id.isdigit() or int(message_id) <= 0:
            raise RuntimeError("Discord did not confirm the prompt message ID")
        return message_id


def redrive_batch(
    store: Any,
    services: Any,
    *,
    channel_id: str,
    poster: Any | None,
    limit: int,
    ttl_seconds: int = 900,
    pause_seconds: float = 1.5,
    sleep: Any = time.sleep,
) -> list[dict[str, Any]]:
    """Prompt at most ``limit`` stuck rows, or plan the batch when poster is None.

    Every row is classified read-only *before* anything is written, and only a
    row that would produce a picker is touched at all. A row that would
    auto-match is reported and left exactly as it was: the backfill re-drives
    requests into a prompt, and never acquires on the requester's behalf.
    """

    with closing(open_readonly(Path(store.path))) as connection:
        rows = stuck_rows(connection)
    results: list[dict[str, Any]] = []
    prompted = 0
    for row in rows:
        if prompted >= limit:
            break
        if str(row["channel_id"]) != str(channel_id):
            results.append(
                {
                    "request_id": int(row["id"]),
                    "action": "skipped",
                    "reason": "row belongs to a different channel",
                }
            )
            continue
        survey = survey_row(row, services)
        if survey["outcome"] != "picker":
            results.append(
                {
                    **survey,
                    "action": "skipped",
                    "reason": (
                        "would auto-match; the backfill never acquires"
                        if survey["outcome"] == "auto_match"
                        else survey["outcome"]
                    ),
                }
            )
            continue

        request_id = int(row["id"])
        prompt = format_candidate_prompt(
            MEDIA_TYPE,
            {
                "request_id": request_id,
                "service": survey["service"],
                "selection_proposal": survey["proposal"],
            },
            ttl_seconds=ttl_seconds,
        )
        if poster is None:
            results.append(
                {**survey, "action": "would_prompt", "prompt": prompt}
            )
            prompted += 1
            continue

        store.transition(
            request_id,
            "processing",
            "Re-driving a stuck selection",
            service=survey["service"],
        )
        store.create_candidate_confirmation(
            request_id, survey["proposal"], ttl_seconds=ttl_seconds
        )
        try:
            message_id = poster.post(str(row["channel_id"]), prompt)
            if not store.bind_candidate_prompt(request_id, message_id):
                raise RuntimeError("Candidate prompt could not be bound")
        except Exception as error:
            # Same release the gateway performs, so the row returns to
            # needs_selection and stays re-drivable rather than stranding.
            store.fail_candidate_prompt(
                request_id, "Could not deliver the Discord candidate prompt"
            )
            results.append(
                {
                    **survey,
                    "action": "failed",
                    "reason": type(error).__name__,
                }
            )
            continue
        results.append({**survey, "action": "prompted", "prompt_message_id": message_id})
        prompted += 1
        if prompted < limit:
            sleep(pause_seconds)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=os.environ.get("HUEY_DB_PATH", "/state/huey.db"),
        help="Huey SQLite path (defaults to HUEY_DB_PATH or /state/huey.db)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report", help="Read-only survey; writes and posts nothing")
    run = commands.add_parser(
        "run", help="Prompt one batch of stuck rows (plans unless --post is given)"
    )
    run.add_argument(
        "--channel",
        required=True,
        help="The only channel this batch may post to; other rows are skipped",
    )
    run.add_argument("--batch", type=int, default=5, help="Rows to prompt (default 5)")
    run.add_argument(
        "--post",
        action="store_true",
        help="Actually post. Without it the batch is planned and nothing is written.",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    database = Path(arguments.database)
    if not database.is_file():
        raise SystemExit(f"Huey database does not exist: {database}")
    if arguments.command == "report":
        value = report(database, os.environ)
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not 1 <= int(arguments.batch) <= 25:
        raise SystemExit("Batch size must be between 1 and 25")
    if not str(arguments.channel).isdigit():
        raise SystemExit("Channel must be a Discord channel ID")
    services = ServiceRegistry(dict(os.environ))
    store = RequestStore(database)
    poster = (
        DiscordPoster(os.environ.get("DISCORD_BOT_TOKEN", ""))
        if arguments.post
        else None
    )
    results = redrive_batch(
        store,
        services,
        channel_id=str(arguments.channel),
        poster=poster,
        limit=int(arguments.batch),
    )
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
