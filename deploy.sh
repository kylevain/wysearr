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

strict_env_value() {
    local key="$1"
    awk -v wanted="$key" '
        /^[[:space:]]*#/ { next }
        {
            raw = $0
            probe = $0
            sub(/^[[:space:]]*/, "", probe)
            sub(/^export[[:space:]]+/, "", probe)
            equals = index(probe, "=")
            if (equals == 0) next
            left = substr(probe, 1, equals - 1)
            gsub(/[[:space:]]/, "", left)
            if (left != wanted) next
            count += 1
            if (index(raw, wanted "=") != 1) invalid = 1
            value = substr(raw, length(wanted) + 2)
        }
        END {
            if (count > 1 || invalid) print "__WYSEARR_INVALID__"
            else if (count == 1) print value
        }
    ' .env
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
    config/{qbittorrent,prowlarr,sonarr,radarr,lidarr,whisparr,bazarr,bookbot,huey,sabnzbd,shelfarr} \
    state/huey \
    state/shelfarr-evaluation \
    state/shelfarr-staging/ebooks \
    "$torrent_root"/{watch,active,complete,processed-torrents,incomplete,incomplete/usenet,usenet,shelfarr,tv,movies,music,spicy,ebooks,audiobooks,manga-comics,roms,sheet-music}

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
# Shelfarr stores the Prowlarr key and encrypted client credentials in SQLite.
# Keep its entire state private even when individual SQLite files use 0644.
chmod 700 config/sabnzbd config/shelfarr
chmod 700 state/shelfarr-evaluation state/shelfarr-staging state/shelfarr-staging/ebooks
find state/shelfarr-evaluation -maxdepth 1 -type f -exec chmod 600 {} +
chmod 600 .env

if [ ! -w state/huey ] || { [ -e state/huey/huey.db ] && [ ! -w state/huey/huey.db ]; }; then
    fail "state/huey is not writable by UID $(id -u); repair ownership before deploying"
fi

docker compose config --quiet
deployment_id="$(date -u +%Y%m%d-%H%M%S)-$$"

feature_flag="$(env_value SHELFARR_ENABLED)"
usenet_flag="$(strict_env_value WYSEARR_USENET_ENABLED)"
core_services=(qbittorrent prowlarr sonarr radarr lidarr whisparr bazarr)
evaluation_services=()
case "$feature_flag" in
    true) evaluation_services=(sabnzbd shelfarr) ;;
    false|"") ;;
    *) fail "SHELFARR_ENABLED must be literal true or false" ;;
esac
case "$usenet_flag" in
    true) [ "$feature_flag" = "true" ] || fail "WYSEARR_USENET_ENABLED requires SHELFARR_ENABLED=true" ;;
    false|"") ;;
    *) fail "WYSEARR_USENET_ENABLED must be literal true or false" ;;
esac

service_is_running() {
    [ -n "$(docker compose ps --status running -q "$1" 2>/dev/null)" ]
}

huey_was_running=0
sabnzbd_was_running=0
shelfarr_was_running=0
service_is_running huey && huey_was_running=1
service_is_running sabnzbd && sabnzbd_was_running=1
service_is_running shelfarr && shelfarr_was_running=1
deployment_complete=0
runtime_replaced=0

restore_previous_runtime() {
    local status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] && [ "$deployment_complete" -ne 1 ]; then
        if [ "$runtime_replaced" -eq 0 ]; then
            echo "WARN: deployment failed before runtime replacement; restoring prior service states" >&2
            if [ "$sabnzbd_was_running" -eq 1 ]; then
                docker compose start sabnzbd >/dev/null 2>&1 || true
            else
                docker compose stop sabnzbd >/dev/null 2>&1 || true
            fi
            if [ "$shelfarr_was_running" -eq 1 ]; then
                docker compose start shelfarr >/dev/null 2>&1 || true
            else
                docker compose stop shelfarr >/dev/null 2>&1 || true
            fi
            if [ "$huey_was_running" -eq 1 ]; then
                docker compose start huey >/dev/null 2>&1 || true
            else
                docker compose stop huey >/dev/null 2>&1 || true
            fi
        else
            # A recreated container is not the prior generation. Never label
            # merely restarting it as rollback: leave intake/evaluation closed
            # until the verified pre-deploy checkpoint is restored or the
            # deployment error is repaired and validation rerun.
            echo "ERROR: deployment failed after runtime replacement; Huey/Shelfarr/SABnzbd are left stopped" >&2
            echo "ERROR: recover from backups/pre-deploy-$deployment_id before resuming intake" >&2
            docker compose stop huey shelfarr sabnzbd >/dev/null 2>&1 || true
        fi
    fi
    exit "$status"
}
trap restore_previous_runtime EXIT

# Freeze request intake before inspecting BookBot ownership or allowing a
# persisted Shelfarr queue to resume. Stop both evaluation services so the
# checkpoint represents one coherent Shelfarr/SAB generation.
docker compose stop huey shelfarr sabnzbd
python3 scripts/backup.py \
    --output "$stack_root/backups/pre-deploy-$deployment_id" \
    --quiet

docker compose pull --ignore-buildable
docker compose build --pull bookbot huey

runtime_replaced=1
docker compose up -d --remove-orphans "${core_services[@]}"
wait_for_health 300 "${core_services[@]}"

python3 scripts/repair_whisparr_quality.py --apply
wait_for_health 180 whisparr

python3 scripts/bootstrap.py

# Bootstrap creates/rotates credentials consumed by Compose. Recreate the
# qBittorrent clients so every service receives the newly persisted values.
docker compose up -d --force-recreate --no-deps qbittorrent
wait_for_health 180 qbittorrent
# Preserve Compose dependency metadata/config hashes. qBittorrent is already
# healthy, so including dependencies here does not recreate it unnecessarily.
docker compose up -d --force-recreate sonarr radarr lidarr whisparr
wait_for_health 300 sonarr radarr lidarr whisparr

if [ "${#evaluation_services[@]}" -gt 0 ]; then
    # This gate runs before Shelfarr's persisted worker queue is allowed to
    # resume, then full convergence repeats it immediately before enablement.
    python3 scripts/bootstrap_shelfarr.py --check-drain-only
    # A first SAB start creates its INI on a fresh installation. Stop it before
    # disabling API parameter logging on disk, so the first authenticated
    # configuration request cannot write credentials into SAB's logs.
    if [ -f config/sabnzbd/sabnzbd.ini ]; then
        python3 scripts/bootstrap_shelfarr.py --prepare-sab-config
    fi
    docker compose up -d --remove-orphans sabnzbd
    wait_for_health 300 sabnzbd
    docker compose stop sabnzbd
    python3 scripts/bootstrap_shelfarr.py --prepare-sab-config
    docker compose start sabnzbd
    wait_for_health 180 sabnzbd
    docker compose up -d --remove-orphans shelfarr
    wait_for_health 300 shelfarr
    python3 scripts/bootstrap_shelfarr.py
else
    # Quarantine the exact managed NNTP server on disk before SAB can resume a
    # persisted queue. The live pass verifies that the disabled state loads.
    if [ -f config/sabnzbd/sabnzbd.ini ]; then
        python3 scripts/bootstrap_shelfarr.py --prepare-sab-config
        docker compose up -d --remove-orphans sabnzbd
        wait_for_health 300 sabnzbd
        python3 scripts/bootstrap_shelfarr.py --converge-usenet-only
    fi
    docker compose stop shelfarr sabnzbd
fi

if ! grep -Eq '^DISCORD_BOT_TOKEN=.{20,}$' .env; then
    fail "DISCORD_BOT_TOKEN is missing from .env"
fi

docker compose up -d --build --remove-orphans bookbot huey
wait_for_health 300 bookbot huey

python3 scripts/validate.py

# Take the post-deploy rollback generation while every Shelfarr state writer
# and its Discord submitter are stopped, then validate the restarted runtime.
docker compose stop huey shelfarr sabnzbd
python3 scripts/backup.py \
    --output "$stack_root/backups/post-deploy-$deployment_id" \
    --quiet
if [ "${#evaluation_services[@]}" -gt 0 ]; then
    docker compose start sabnzbd shelfarr
    wait_for_health 300 sabnzbd shelfarr
fi
docker compose start huey
wait_for_health 180 huey
python3 scripts/validate.py

deployment_complete=1
echo "PASS: WyseARR production stack deployed and validated"
docker compose ps
