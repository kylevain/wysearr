#!/usr/bin/env bash
set -euo pipefail

stack_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$stack_root"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

env_value() {
    local key="$1"
    awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' .env
}

wait_for_health() {
    local timeout_seconds="$1"
    shift
    local deadline=$((SECONDS + timeout_seconds))
    local service container state health

    while (( SECONDS < deadline )); do
        local pending=0
        for service in "$@"; do
            container="$(docker compose ps -q "$service")"
            if [ -z "$container" ]; then
                pending=1
                continue
            fi
            state="$(docker inspect --format '{{.State.Status}}' "$container")"
            health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")"
            if [ "$state" != "running" ] || [ "$health" != "healthy" ]; then
                pending=1
            fi
        done
        if [ "$pending" -eq 0 ]; then
            return 0
        fi
        sleep 2
    done

    docker compose ps >&2
    fail "services did not become healthy within ${timeout_seconds}s: $*"
}

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
fi

media_root="${MEDIA_ROOT:-$(env_value MEDIA_ROOT)}"
torrent_root="${TORRENT_ROOT:-$(env_value TORRENT_ROOT)}"
media_root="${media_root:-/mnt/media}"
torrent_root="${torrent_root:-$stack_root/state/torrents}"

mkdir -p \
    config/{qbittorrent,prowlarr,sonarr,radarr,lidarr,whisparr,bazarr,bookbot,huey} \
    state/huey \
    "$torrent_root"/{watch,active,complete,processed-torrents,incomplete,tv,movies,music,spicy,ebooks,audiobooks,manga-comics,roms,sheet-music}

[ -d "$media_root" ] || fail "$media_root is missing"
mountpoint -q "$media_root" || fail "$media_root is not a mountpoint"

probe_path="$media_root/.wysearr-write-probe-$$"
if ! (umask 077 && : > "$probe_path"); then
    fail "$media_root is not writable"
fi
rm -f -- "$probe_path"

mkdir -p \
    "$media_root"/{tv,movies,music,spicy,audiobooks,roms,sheetmusic} \
    "$media_root"/ebooks/{Books,Comics} \
    "$media_root"/duplicates/{ebooks,audiobooks,manga-comics,roms,sheet-music}

chmod 775 config/bookbot state/huey "$torrent_root" "$torrent_root"/*
chmod 600 .env

if [ ! -w state/huey ] || { [ -e state/huey/huey.db ] && [ ! -w state/huey/huey.db ]; }; then
    fail "state/huey is not writable by UID $(id -u); repair ownership before deploying"
fi

docker compose config --quiet
python3 scripts/backup.py --quiet

docker compose pull --ignore-buildable
docker compose build --pull bookbot huey

core_services=(qbittorrent prowlarr sonarr radarr lidarr whisparr bazarr)
docker compose up -d --remove-orphans "${core_services[@]}"
wait_for_health 300 "${core_services[@]}"

python3 scripts/repair_whisparr_quality.py --apply
wait_for_health 180 whisparr

python3 scripts/bootstrap.py

# Bootstrap creates/rotates credentials consumed by Compose. Recreate the
# qBittorrent clients so every service receives the newly persisted values.
docker compose up -d --force-recreate --no-deps qbittorrent
wait_for_health 180 qbittorrent
docker compose up -d --force-recreate --no-deps sonarr radarr lidarr whisparr
wait_for_health 300 sonarr radarr lidarr whisparr

if ! grep -Eq '^DISCORD_BOT_TOKEN=.{20,}$' .env; then
    fail "DISCORD_BOT_TOKEN is missing from .env"
fi

docker compose up -d --build --remove-orphans bookbot huey
wait_for_health 300 bookbot huey

python3 scripts/validate.py

echo "PASS: WyseARR production stack deployed and validated"
docker compose ps
