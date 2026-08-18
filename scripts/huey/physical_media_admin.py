"""Operator tools for resolving quarantined physical-media artifacts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("manifest must be a JSON object")
    return value


def _write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ": ")) + "\n", encoding="utf-8")
    tmp.replace(path)


def resolve_movie(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest)
    manifest = _load_json(manifest_path)
    manifest.update({
        "version": 1,
        "media_type": "movie",
        "title": args.title,
        "year": int(args.year),
        "imdb_id": args.imdb_id,
        "tmdb_id": int(args.tmdb_id),
        "manual_resolution": {
            "type": "movie",
            "reason": args.reason,
        },
    })
    _write_json(manifest_path, manifest)

    with sqlite3.connect(args.database) as connection:
        connection.execute(
            """
            UPDATE trusted_library_events
            SET title = ?, year = ?, imdb_id = ?, tmdb_id = ?,
                media_type = 'movie', state = 'validated', error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE source_fingerprint = ? AND state IN ('manual_review', 'failed', 'validated')
            """,
            (
                args.title,
                int(args.year),
                args.imdb_id,
                int(args.tmdb_id),
                str(args.fingerprint).casefold(),
            ),
        )


def mark_nonstandard(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest)
    manifest = _load_json(manifest_path)
    manifest["version"] = 2
    manifest["media_type"] = "nonstandard"
    manifest["title"] = args.title
    manifest["manual_resolution"] = {
        "type": "nonstandard",
        "reason": args.reason,
    }
    _write_json(manifest_path, manifest)

    with sqlite3.connect(args.database) as connection:
        connection.execute(
            """
            UPDATE trusted_library_events
            SET title = ?, media_type = 'nonstandard', state = 'manual_review',
                error = 'Approved nonstandard physical video; awaiting library placement.',
                updated_at = CURRENT_TIMESTAMP
            WHERE source_fingerprint = ? AND state IN ('manual_review', 'failed', 'validated')
            """,
            (args.title, str(args.fingerprint).casefold()),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="/state/huey.db")
    subparsers = parser.add_subparsers(required=True)

    movie = subparsers.add_parser("resolve-movie")
    movie.add_argument("--fingerprint", required=True)
    movie.add_argument("--manifest", required=True)
    movie.add_argument("--title", required=True)
    movie.add_argument("--year", required=True, type=int)
    movie.add_argument("--imdb-id", required=True)
    movie.add_argument("--tmdb-id", required=True, type=int)
    movie.add_argument("--reason", required=True)
    movie.set_defaults(function=resolve_movie)

    nonstandard = subparsers.add_parser("mark-nonstandard")
    nonstandard.add_argument("--fingerprint", required=True)
    nonstandard.add_argument("--manifest", required=True)
    nonstandard.add_argument("--title", required=True)
    nonstandard.add_argument("--reason", required=True)
    nonstandard.set_defaults(function=mark_nonstandard)

    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
