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

trim_ascii_whitespace() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

validate_ebook_backend_policy() {
    local raw="$1"
    local owner="$2"
    local part backend normalized="" first=""
    local explicit_policy=1
    local -a parts=()
    local -A seen=()

    [ "$raw" != "__WYSEARR_INVALID__" ] || \
        fail "EBOOK_ACQUISITION_BACKENDS must have one exact assignment"
    [ "$owner" != "__WYSEARR_INVALID__" ] || \
        fail "EBOOK_ACQUISITION_OWNER must have one exact assignment"

    raw="$(trim_ascii_whitespace "$raw")"
    if [ -z "$(trim_ascii_whitespace "$owner")" ]; then
        owner=""
    fi
    if [ -z "$raw" ]; then
        # Compatibility only: Huey synthesizes a singleton policy from the old
        # owner setting. Production still fails the exact two-backend gate
        # below until the explicit cascade is configured.
        explicit_policy=0
        raw="${owner:-shelfarr}"
    fi

    [[ "$raw" != ,* && "$raw" != *, ]] || \
        fail "EBOOK_ACQUISITION_BACKENDS contains a blank backend"
    IFS=',' read -r -a parts <<< "$raw"
    [ "${#parts[@]}" -gt 0 ] || \
        fail "EBOOK_ACQUISITION_BACKENDS must not be empty"
    for part in "${parts[@]}"; do
        backend="$(trim_ascii_whitespace "$part")"
        [ -n "$backend" ] || \
            fail "EBOOK_ACQUISITION_BACKENDS contains a blank backend"
        case "$backend" in
            lazylibrarian|shelfarr) ;;
            direct)
                [ "$explicit_policy" -eq 0 ] || \
                    fail "EBOOK_ACQUISITION_BACKENDS contains unknown or noncanonical backend"
                ;;
            *) fail "EBOOK_ACQUISITION_BACKENDS contains unknown or noncanonical backend" ;;
        esac
        [ -z "${seen[$backend]+present}" ] || \
            fail "EBOOK_ACQUISITION_BACKENDS contains duplicate backend: $backend"
        seen[$backend]=1
        [ -n "$first" ] || first="$backend"
        normalized="${normalized:+$normalized,}$backend"
    done

    if [ -n "$owner" ]; then
        case "$owner" in
            lazylibrarian|shelfarr) ;;
            direct)
                [ "$explicit_policy" -eq 0 ] || \
                    fail "EBOOK_ACQUISITION_OWNER=direct requires EBOOK_ACQUISITION_BACKENDS to be absent"
                ;;
            *) fail "EBOOK_ACQUISITION_OWNER must be lazylibrarian, shelfarr, or legacy direct with no backend policy" ;;
        esac
        [ "$owner" = "$first" ] || \
            fail "EBOOK_ACQUISITION_OWNER must match the first configured ebook backend"
    fi

    [ "$normalized" = "lazylibrarian,shelfarr" ] || \
        fail "production EBOOK_ACQUISITION_BACKENDS must be exactly lazylibrarian,shelfarr"
    printf '%s' "$normalized"
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
    config/{qbittorrent,prowlarr,sonarr,radarr,lidarr,whisparr,bazarr,bookbot,huey,sabnzbd,shelfarr,abba,lazylibrarian} \
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
# qBittorrent and ARR config trees persist credential material alongside
# databases and logs. Restrict traversal at the directory boundary even when
# an application creates a file with a more permissive mode.
chmod 700 config/{qbittorrent,prowlarr,sonarr,radarr,lidarr,whisparr}
# Shelfarr stores the Prowlarr key and encrypted client credentials in SQLite.
# Keep its entire state private even when individual SQLite files use 0644.
chmod 700 config/abba config/lazylibrarian config/sabnzbd config/shelfarr
chmod 700 state/shelfarr-evaluation state/shelfarr-staging state/shelfarr-staging/ebooks
find state/shelfarr-evaluation -maxdepth 1 -type f -exec chmod 600 {} +
chmod 600 .env

if [ ! -w state/huey ] || { [ -e state/huey/huey.db ] && [ ! -w state/huey/huey.db ]; }; then
    fail "state/huey is not writable by UID $(id -u); repair ownership before deploying"
fi

vpn_provider="$(strict_env_value VPN_SERVICE_PROVIDER)"
openvpn_user="$(strict_env_value OPENVPN_USER)"
openvpn_password="$(strict_env_value OPENVPN_PASSWORD)"
openvpn_protocol="$(strict_env_value OPENVPN_PROTOCOL)"
[ "$vpn_provider" = "private internet access" ] || \
    fail "VPN_SERVICE_PROVIDER must be exactly 'private internet access'"
[ -n "$openvpn_user" ] && [ "$openvpn_user" != "__WYSEARR_INVALID__" ] || \
    fail "OPENVPN_USER must have one nonempty exact assignment"
[ -n "$openvpn_password" ] && [ "$openvpn_password" != "__WYSEARR_INVALID__" ] || \
    fail "OPENVPN_PASSWORD must have one nonempty exact assignment"
case "$openvpn_protocol" in
    ""|udp) ;;
    *) fail "OPENVPN_PROTOCOL must be udp when explicitly configured" ;;
esac
unset openvpn_user openvpn_password

docker compose config --quiet
deployment_id="$(date -u +%Y%m%d-%H%M%S)-$$"

shelfarr_flag="$(strict_env_value SHELFARR_ENABLED)"
abba_flag="$(strict_env_value ABBA_ENABLED)"
lazylibrarian_flag="$(strict_env_value LAZYLIBRARIAN_ENABLED)"
ebook_backends="$(strict_env_value EBOOK_ACQUISITION_BACKENDS)"
ebook_owner="$(strict_env_value EBOOK_ACQUISITION_OWNER)"
usenet_flag="$(strict_env_value WYSEARR_USENET_ENABLED)"
core_services=(prowlarr sonarr radarr lidarr whisparr bazarr)
shelfarr_services=()
abba_services=()
lazylibrarian_services=()
case "$shelfarr_flag" in
    true) shelfarr_services=(sabnzbd shelfarr) ;;
    false|"") ;;
    *) fail "SHELFARR_ENABLED must be literal true or false" ;;
esac
case "$abba_flag" in
    true) abba_services=(abba) ;;
    false|"") ;;
    *) fail "ABBA_ENABLED must be literal true or false" ;;
esac
case "$lazylibrarian_flag" in
    true) lazylibrarian_services=(lazylibrarian) ;;
    false|"") ;;
    *) fail "LAZYLIBRARIAN_ENABLED must be literal true or false" ;;
esac
case "$usenet_flag" in
    true) [ "$shelfarr_flag" = "true" ] || fail "WYSEARR_USENET_ENABLED requires SHELFARR_ENABLED=true" ;;
    false|"") ;;
    *) fail "WYSEARR_USENET_ENABLED must be literal true or false" ;;
esac
ebook_backends="$(validate_ebook_backend_policy "$ebook_backends" "$ebook_owner")"
ebook_owner="${ebook_owner:-${ebook_backends%%,*}}"
[ "$lazylibrarian_flag" = "true" ] || \
    fail "EBOOK_ACQUISITION_BACKENDS requires LAZYLIBRARIAN_ENABLED=true"
[ "$shelfarr_flag" = "true" ] || \
    fail "EBOOK_ACQUISITION_BACKENDS requires SHELFARR_ENABLED=true"

service_is_running() {
    [ -n "$(docker compose ps --status running -q "$1" 2>/dev/null)" ]
}

huey_was_running=0
bookbot_was_running=0
abba_was_running=0
lazylibrarian_was_running=0
sabnzbd_was_running=0
shelfarr_was_running=0
service_is_running huey && huey_was_running=1
service_is_running bookbot && bookbot_was_running=1
service_is_running abba && abba_was_running=1
service_is_running lazylibrarian && lazylibrarian_was_running=1
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
            if [ "$abba_was_running" -eq 1 ]; then
                docker compose start abba >/dev/null 2>&1 || true
            else
                docker compose stop abba >/dev/null 2>&1 || true
            fi
            if [ "$lazylibrarian_was_running" -eq 1 ]; then
                docker compose start lazylibrarian >/dev/null 2>&1 || true
            else
                docker compose stop lazylibrarian >/dev/null 2>&1 || true
            fi
            if [ "$bookbot_was_running" -eq 1 ]; then
                docker compose start bookbot >/dev/null 2>&1 || true
            else
                docker compose stop bookbot >/dev/null 2>&1 || true
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
            echo "ERROR: deployment failed after runtime replacement; Huey/BookBot/ABBA/LazyLibrarian/Shelfarr/SABnzbd are left stopped" >&2
            echo "ERROR: recover from backups/pre-deploy-$deployment_id before resuming intake" >&2
            docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd >/dev/null 2>&1 || true
        fi
    fi
    exit "$status"
}
trap restore_previous_runtime EXIT

# Freeze request intake before inspecting BookBot ownership or allowing a
# persisted Shelfarr, LazyLibrarian, or ABBA queue to resume. Stop every book
# acquisition owner and the importer so the checkpoint represents one coherent
# request/acquisition/import generation.
docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd
python3 scripts/backup.py \
    --output "$stack_root/backups/pre-deploy-$deployment_id" \
    --quiet

docker compose pull --ignore-buildable
application_build_services=(bookbot huey)
if [ "${#abba_services[@]}" -gt 0 ]; then
    application_build_services+=(abba)
fi
docker compose build --pull "${application_build_services[@]}"

runtime_replaced=1
# Gluetun owns the published WebUI port and the `qbittorrent` network alias.
# Stop a legacy/direct-network qBittorrent before the first migration so both
# containers can never contend for the same LAN port. Later deploys preserve a
# running qBittorrent unless Gluetun itself was recreated or the namespace link
# is stale.
previous_gluetun_container="$(docker compose ps -q gluetun 2>/dev/null || true)"
qbittorrent_container="$(docker compose ps -aq qbittorrent 2>/dev/null || true)"
qbittorrent_network_mode=""
if [ -n "$qbittorrent_container" ]; then
    qbittorrent_network_mode="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$qbittorrent_container")"
fi
if service_is_running qbittorrent && \
    { [ -z "$previous_gluetun_container" ] || \
      [ "$qbittorrent_network_mode" != "container:$previous_gluetun_container" ]; }; then
    docker compose stop qbittorrent
fi

docker compose up -d --no-deps gluetun
wait_for_health 240 gluetun
gluetun_container="$(docker compose ps -q gluetun)"
[ -n "$gluetun_container" ] || fail "Gluetun has no running container"

qbittorrent_container="$(docker compose ps -aq qbittorrent 2>/dev/null || true)"
qbittorrent_network_mode=""
if [ -n "$qbittorrent_container" ]; then
    qbittorrent_network_mode="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$qbittorrent_container")"
fi
if ! service_is_running qbittorrent || \
    [ "$qbittorrent_network_mode" != "container:$gluetun_container" ]; then
    docker compose up -d --no-deps --force-recreate qbittorrent
fi
wait_for_health 240 qbittorrent

docker compose up -d --no-deps qbittorrent-port-forward
wait_for_health 240 qbittorrent-port-forward
docker compose up -d --remove-orphans --no-deps "${core_services[@]}"
wait_for_health 300 "${core_services[@]}"

python3 scripts/repair_whisparr_quality.py --apply
wait_for_health 180 whisparr

python3 scripts/bootstrap.py

# Bootstrap itself performs a guarded qBittorrent restart only when it actually
# repairs WebUI credentials. Ordinary idempotent bootstrap runs leave the
# existing container and every transfer uninterrupted.
wait_for_health 180 qbittorrent
# Bootstrap persists ARR downloader repairs through their APIs. A second
# Compose recreation would only interrupt unrelated monitoring activity.
wait_for_health 300 sonarr radarr lidarr whisparr

if [ "${#shelfarr_services[@]}" -gt 0 ]; then
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
    docker compose up -d --remove-orphans --no-deps shelfarr
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

if [ "${#lazylibrarian_services[@]}" -gt 0 ]; then
    # LazyLibrarian owns catalog/search/acquisition only and receives neither a
    # download payload mount nor final-library authority.
    python3 scripts/bootstrap_lazylibrarian.py --prepare-config
    docker compose up -d --remove-orphans --no-deps lazylibrarian
    wait_for_health 300 lazylibrarian
    python3 scripts/bootstrap_lazylibrarian.py
else
    docker compose stop lazylibrarian
fi

if [ "${#abba_services[@]}" -gt 0 ]; then
    # qBittorrent credentials and the dedicated audiobook category have already
    # been converged. ABBA receives no media mount and can only submit the exact
    # `/downloads/audiobooks` path through qBittorrent's private Compose API.
    docker compose up -d --remove-orphans --no-deps abba
    wait_for_health 300 abba
else
    docker compose stop abba
fi

if ! grep -Eq '^DISCORD_BOT_TOKEN=.{20,}$' .env; then
    fail "DISCORD_BOT_TOKEN is missing from .env"
fi

# Required dependencies and every enabled optional acquisition service are
# already health-gated above. Keep this recreation scoped to the two rebuilt
# application containers so unrelated services and active transfers stay put.
docker compose up -d --remove-orphans --no-deps bookbot huey
wait_for_health 300 bookbot huey

python3 scripts/validate_qbittorrent_vpn.py
python3 scripts/validate.py

# Take the post-deploy rollback generation while every book acquisition state
# writer and its Discord submitter are stopped, then validate the restarted
# runtime.
docker compose stop huey bookbot abba lazylibrarian shelfarr sabnzbd
python3 scripts/backup.py \
    --output "$stack_root/backups/post-deploy-$deployment_id" \
    --quiet
if [ "${#shelfarr_services[@]}" -gt 0 ]; then
    docker compose start sabnzbd shelfarr
    wait_for_health 300 sabnzbd shelfarr
fi
if [ "${#lazylibrarian_services[@]}" -gt 0 ]; then
    docker compose start lazylibrarian
    wait_for_health 180 lazylibrarian
fi
if [ "${#abba_services[@]}" -gt 0 ]; then
    docker compose start abba
    wait_for_health 180 abba
fi
docker compose start bookbot huey
wait_for_health 180 bookbot huey
python3 scripts/validate_qbittorrent_vpn.py
python3 scripts/validate.py

deployment_complete=1
echo "PASS: WyseARR production stack deployed and validated"
docker compose ps
