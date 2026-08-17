#!/usr/bin/env bash
set -euo pipefail

# ARM invokes BASH_SCRIPT for every notification. Ignore everything except the
# exact, error-free visual-media completion notification, then resolve the
# active ARM database row and its single completed main-feature MKV.
notification_title="${1:-}"
notification_body="${2:-}"

if [[ "$notification_title" != "ARM notification" || "$notification_body" != *" processing complete." ]]; then
    exit 0
fi

exec python3 - "$notification_body" <<'PY'
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

body = sys.argv[1]
database = Path(os.environ.get("ARM_DATABASE", "/home/arm/db/arm.db"))
completed_root = Path(os.environ.get("ARM_COMPLETED_ROOT", "/home/arm/media/completed")).resolve()
helper = Path(os.environ.get(
    "WYSEARR_DELIVERY_HELPER",
    "/etc/arm/config/hooks/deliver_physical_media_from_batfire.sh",
))

with sqlite3.connect(database) as connection:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT job_id, title, year, imdb_id, path
        FROM job
        WHERE stop_time IS NULL
          AND video_type = 'movie'
          AND disctype IN ('dvd', 'bluray')
          AND COALESCE(errors, '') = ''
        ORDER BY job_id DESC
        """
    ).fetchall()

matches = [row for row in rows if body == f"{row['title']} processing complete."]
if len(matches) != 1:
    raise SystemExit(f"fail-closed: expected one active completed ARM movie job, found {len(matches)}")

row = matches[0]
job_path = Path(row["path"] or "").resolve()
if completed_root not in job_path.parents:
    raise SystemExit(f"fail-closed: ARM job path is outside {completed_root}")

mkvs = [path for path in job_path.iterdir() if path.is_file() and path.suffix.lower() == ".mkv"]
if len(mkvs) != 1:
    raise SystemExit(f"fail-closed: expected one completed main-feature MKV, found {len(mkvs)}")
if mkvs[0].stat().st_size < 52_428_800:
    raise SystemExit("fail-closed: completed main-feature MKV is below the minimum size")

environment = os.environ.copy()
environment.update({
    "WYSEARR_SSH_HOST": "192.168.4.86",
    "WYSEARR_SSH_USER": "wyseadmin",
    "WYSEARR_SSH_IDENTITY": "/home/arm/.ssh/id_ed25519_wysearr_physical",
    "WYSEARR_PHYSICAL_ROOT": "/home/wyseadmin/homelab/state/physical-media/incoming",
})
arguments = [str(helper), str(mkvs[0]), str(row["title"] or "")]
arguments.append(str(row["year"] or ""))
arguments.append(str(row["imdb_id"] or ""))
subprocess.run(arguments, check=True, env=environment)
PY
