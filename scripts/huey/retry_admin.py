"""Small, non-networked operator CLI for unavailable ebook retries."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from .database import RequestStore
except ImportError:  # pragma: no cover - direct container entrypoint
    from database import RequestStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=os.environ.get("HUEY_DB_PATH", "/state/huey.db"),
        help="Huey SQLite path (defaults to HUEY_DB_PATH or /state/huey.db)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List persisted retry records")
    listing.add_argument(
        "--state",
        choices=(
            "queued",
            "retrying",
            "awaiting_import",
            "blocked",
            "fulfilled",
            "expired",
        ),
    )
    listing.add_argument("--limit", type=int, default=100)
    force = commands.add_parser(
        "force", help="Make one safe queued retry immediately eligible"
    )
    force.add_argument("request_id", type=int)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    database_path = Path(arguments.database)
    if not database_path.is_file():
        raise SystemExit(f"Huey database does not exist: {database_path}")
    store = RequestStore(database_path)
    if arguments.command == "force":
        if not store.force_unavailable_retry(arguments.request_id):
            print(
                json.dumps(
                    {
                        "request_id": arguments.request_id,
                        "forced": False,
                        "reason": "request is not in the safe queued state",
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(json.dumps({"request_id": arguments.request_id, "forced": True}))
        return 0

    records = store.list_unavailable_retries(
        state=arguments.state, limit=arguments.limit
    )
    safe_fields = (
        "request_id",
        "media_type",
        "canonical_title",
        "canonical_creator",
        "canonical_year",
        "first_unavailable_at",
        "last_retry_at",
        "last_proof_check_at",
        "next_retry_at",
        "retry_count",
        "state",
        "final_import_state",
        "fulfilled_at",
        "expired_at",
    )
    print(
        json.dumps(
            [
                {field: record.get(field) for field in safe_fields}
                for record in records
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
