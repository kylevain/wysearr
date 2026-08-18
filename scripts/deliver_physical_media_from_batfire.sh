#!/usr/bin/env bash
set -euo pipefail

# BatFire-side, manifest-last delivery helper. The MKV is transferred exactly
# once; the remote rename makes an interrupted rsync invisible to Huey.
source_media="${1:?usage: deliver_physical_media_from_batfire.sh SOURCE_MKV_OR_DIR [TITLE YEAR [IMDB_ID [DISC_LABEL DVD_CRC64 DURATION_SECONDS ARM_JOB_ID ARM_TITLE ARM_YEAR ARM_IMDB_ID]]]}"
title="${2:-}"
year="${3:-}"
imdb_id="${4:-}"
disc_label="${5:-}"
dvd_crc64="${6:-}"
duration_seconds="${7:-}"
arm_job_id="${8:-}"
arm_title="${9:-}"
arm_year="${10:-}"
arm_imdb_id="${11:-}"
remote_host="${WYSEARR_SSH_HOST:-192.168.4.86}"
remote_user="${WYSEARR_SSH_USER:-wyseadmin}"
remote_root="${WYSEARR_PHYSICAL_ROOT:-/home/wyseadmin/homelab/state/physical-media/incoming}"
ssh_identity="${WYSEARR_SSH_IDENTITY:-}"

ssh_args=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [ -n "$ssh_identity" ]; then
    [ -r "$ssh_identity" ] || { echo "SSH identity is unreadable" >&2; exit 2; }
    ssh_args+=(-i "$ssh_identity")
fi
printf -v rsync_rsh '%q ' ssh "${ssh_args[@]}"

media_type="${PHYSICAL_MEDIA_TYPE:-movie}"
case "$media_type" in
    movie|tv|nonstandard|ambiguous) ;;
    *) echo "PHYSICAL_MEDIA_TYPE is invalid" >&2; exit 2 ;;
esac

if [ -f "$source_media" ]; then
    [ "${source_media##*.}" = mkv ] || [ "${source_media##*.}" = MKV ] || {
        echo "source must be an MKV" >&2
        exit 2
    }
elif [ -d "$source_media" ]; then
    [ "$media_type" != "movie" ] || media_type=ambiguous
else
    echo "source media is missing" >&2
    exit 2
fi

sha256="$(
    if [ -f "$source_media" ]; then
        sha256sum -- "$source_media" | awk '{print $1}'
    else
        find "$source_media" -maxdepth 1 -type f \( -iname '*.mkv' \) -print0 |
            sort -z |
            xargs -0 sha256sum -- |
            sha256sum |
            awk '{print $1}'
    fi
)"
size_bytes="$(
    if [ -f "$source_media" ]; then
        stat -c '%s' -- "$source_media"
    else
        find "$source_media" -maxdepth 1 -type f \( -iname '*.mkv' \) -printf '%s\n' |
            awk '{total += $1} END {print total + 0}'
    fi
)"
delivery_id="arm-${sha256:0:20}"

manifest_tmp="$(mktemp)"
trap 'rm -f -- "$manifest_tmp"' EXIT
PHYSICAL_SOURCE="$source_media" PHYSICAL_MEDIA_TYPE="$media_type" \
PHYSICAL_TITLE="$title" PHYSICAL_YEAR="$year" PHYSICAL_IMDB_ID="$imdb_id" \
PHYSICAL_DISC_LABEL="$disc_label" PHYSICAL_DVD_CRC64="$dvd_crc64" \
PHYSICAL_DURATION_SECONDS="$duration_seconds" PHYSICAL_ARM_JOB_ID="$arm_job_id" \
PHYSICAL_ARM_TITLE="$arm_title" PHYSICAL_ARM_YEAR="$arm_year" \
PHYSICAL_ARM_IMDB_ID="$arm_imdb_id" \
PHYSICAL_SHA256="$sha256" PHYSICAL_SIZE="$size_bytes" \
python3 - "$manifest_tmp" <<'PY'
import hashlib, json, os, sys
from pathlib import Path
payload = {
    "version": 1 if os.environ["PHYSICAL_MEDIA_TYPE"] == "movie" else 2,
    "source": "arm",
    "media_type": os.environ["PHYSICAL_MEDIA_TYPE"],
    "size_bytes": int(os.environ["PHYSICAL_SIZE"]),
    "sha256": os.environ["PHYSICAL_SHA256"],
}
source = Path(os.environ["PHYSICAL_SOURCE"])
if source.is_file():
    payload["file"] = "feature.mkv"
else:
    files = []
    for index, path in enumerate(sorted(source.glob("*.mkv")), start=1):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        files.append({
            "file": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
            "track_number": index,
            "kind": "episode" if os.environ["PHYSICAL_MEDIA_TYPE"] == "tv" else "unknown",
        })
    if not files:
        raise SystemExit("source directory contains no MKV files")
    payload["files"] = files
if os.environ.get("PHYSICAL_TITLE"):
    payload["title"] = os.environ["PHYSICAL_TITLE"]
    if os.environ["PHYSICAL_MEDIA_TYPE"] == "tv":
        payload["series_title"] = os.environ["PHYSICAL_TITLE"]
if os.environ.get("PHYSICAL_YEAR"):
    payload["year"] = os.environ["PHYSICAL_YEAR"]
if os.environ.get("PHYSICAL_IMDB_ID"):
    payload["imdb_id"] = os.environ["PHYSICAL_IMDB_ID"]
if os.environ.get("PHYSICAL_DISC_LABEL"):
    payload["disc_label"] = os.environ["PHYSICAL_DISC_LABEL"]
if os.environ.get("PHYSICAL_DVD_CRC64"):
    payload["dvd_crc64"] = os.environ["PHYSICAL_DVD_CRC64"]
if os.environ.get("PHYSICAL_DURATION_SECONDS"):
    payload["duration_seconds"] = int(os.environ["PHYSICAL_DURATION_SECONDS"])
if os.environ.get("PHYSICAL_ARM_JOB_ID"):
    payload["arm_job_id"] = int(os.environ["PHYSICAL_ARM_JOB_ID"])
if os.environ.get("PHYSICAL_ARM_TITLE"):
    payload["arm_title"] = os.environ["PHYSICAL_ARM_TITLE"]
if os.environ.get("PHYSICAL_ARM_YEAR"):
    payload["arm_year"] = int(os.environ["PHYSICAL_ARM_YEAR"])
if os.environ.get("PHYSICAL_ARM_IMDB_ID"):
    payload["arm_imdb_id"] = os.environ["PHYSICAL_ARM_IMDB_ID"]
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
    stream.write("\n")
PY

remote_dir="$remote_root/$delivery_id"
ssh "${ssh_args[@]}" -- "$remote_user@$remote_host" \
    "mkdir -p -- '$remote_dir' && chmod 775 -- '$remote_dir'"
if [ -f "$source_media" ]; then
    rsync -e "$rsync_rsh" --partial --append-verify --protect-args -- \
        "$source_media" "$remote_user@$remote_host:$remote_dir/feature.mkv.partial"
    ssh "${ssh_args[@]}" -- "$remote_user@$remote_host" \
        "mv -- '$remote_dir/feature.mkv.partial' '$remote_dir/feature.mkv'"
else
    while IFS= read -r -d '' mkv; do
        base="$(basename -- "$mkv")"
        rsync -e "$rsync_rsh" --partial --append-verify --protect-args -- \
            "$mkv" "$remote_user@$remote_host:$remote_dir/$base.partial"
        ssh "${ssh_args[@]}" -- "$remote_user@$remote_host" \
            "mv -- '$remote_dir/$base.partial' '$remote_dir/$base'"
    done < <(find "$source_media" -maxdepth 1 -type f \( -iname '*.mkv' \) -print0 | sort -z)
fi
rsync -e "$rsync_rsh" --protect-args -- "$manifest_tmp" \
    "$remote_user@$remote_host:$remote_dir/manifest.json.partial"
ssh "${ssh_args[@]}" -- "$remote_user@$remote_host" \
    "mv -- '$remote_dir/manifest.json.partial' '$remote_dir/manifest.json'"

printf 'Delivered %s to %s:%s\n' "$sha256" "$remote_host" "$remote_dir"
