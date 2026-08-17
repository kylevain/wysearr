#!/usr/bin/env bash
set -euo pipefail

# BatFire-side, manifest-last delivery helper. The MKV is transferred exactly
# once; the remote rename makes an interrupted rsync invisible to Huey.
source_mkv="${1:?usage: deliver_physical_media_from_batfire.sh SOURCE_MKV [TITLE YEAR [IMDB_ID]]}"
title="${2:-}"
year="${3:-}"
imdb_id="${4:-}"
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

[ -f "$source_mkv" ] || { echo "source MKV is missing" >&2; exit 2; }
[ "${source_mkv##*.}" = mkv ] || [ "${source_mkv##*.}" = MKV ] || {
    echo "source must be an MKV" >&2
    exit 2
}

sha256="$(sha256sum -- "$source_mkv" | awk '{print $1}')"
size_bytes="$(stat -c '%s' -- "$source_mkv")"
delivery_id="arm-${sha256:0:20}"

manifest_tmp="$(mktemp)"
trap 'rm -f -- "$manifest_tmp"' EXIT
PHYSICAL_TITLE="$title" PHYSICAL_YEAR="$year" PHYSICAL_IMDB_ID="$imdb_id" \
PHYSICAL_SHA256="$sha256" PHYSICAL_SIZE="$size_bytes" \
python3 - "$manifest_tmp" <<'PY'
import json, os, sys
payload = {
    "version": 1,
    "source": "arm",
    "file": "feature.mkv",
    "size_bytes": int(os.environ["PHYSICAL_SIZE"]),
    "sha256": os.environ["PHYSICAL_SHA256"],
}
if os.environ.get("PHYSICAL_TITLE"):
    payload["title"] = os.environ["PHYSICAL_TITLE"]
if os.environ.get("PHYSICAL_YEAR"):
    payload["year"] = os.environ["PHYSICAL_YEAR"]
if os.environ.get("PHYSICAL_IMDB_ID"):
    payload["imdb_id"] = os.environ["PHYSICAL_IMDB_ID"]
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
    stream.write("\n")
PY

remote_dir="$remote_root/$delivery_id"
ssh "${ssh_args[@]}" -- "$remote_user@$remote_host" \
    "mkdir -p -- '$remote_dir' && chmod 775 -- '$remote_dir'"
rsync -e "$rsync_rsh" --partial --append-verify --protect-args -- \
    "$source_mkv" "$remote_user@$remote_host:$remote_dir/feature.mkv.partial"
ssh "${ssh_args[@]}" -- "$remote_user@$remote_host" \
    "mv -- '$remote_dir/feature.mkv.partial' '$remote_dir/feature.mkv'"
rsync -e "$rsync_rsh" --protect-args -- "$manifest_tmp" \
    "$remote_user@$remote_host:$remote_dir/manifest.json.partial"
ssh "${ssh_args[@]}" -- "$remote_user@$remote_host" \
    "mv -- '$remote_dir/manifest.json.partial' '$remote_dir/manifest.json'"

printf 'Delivered %s to %s:%s\n' "$sha256" "$remote_host" "$remote_dir"
