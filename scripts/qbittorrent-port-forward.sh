#!/bin/sh
set -eu

# Continuously reconcile Gluetun's current PIA forwarded port into qBittorrent.
# Credentials are passed only as URL-encoded request bodies and are never logged.

qbittorrent_url="${QBITTORRENT_URL:-http://127.0.0.1:8080}"
qbittorrent_username="${QBITTORRENT_USERNAME:-}"
qbittorrent_password="${QBITTORRENT_PASSWORD:-}"
vpn_interface="${QBITTORRENT_VPN_INTERFACE:-tun0}"
gluetun_health_url="${GLUETUN_HEALTH_URL:-http://127.0.0.1:9999/}"
status_file="${VPN_PORT_FORWARDING_STATUS_FILE:-/gluetun/forwarded_port}"
interval="${PORT_FORWARD_SYNC_INTERVAL_SECONDS:-15}"

case "$interval" in
    ''|*[!0-9]*)
        echo "ERROR: PORT_FORWARD_SYNC_INTERVAL_SECONDS must be numeric" >&2
        exit 1
        ;;
esac
if [ "$interval" -lt 5 ] || [ "$interval" -gt 3600 ]; then
    echo "ERROR: PORT_FORWARD_SYNC_INTERVAL_SECONDS must be between 5 and 3600" >&2
    exit 1
fi
if [ -z "$qbittorrent_username" ] || [ -z "$qbittorrent_password" ]; then
    echo "ERROR: qBittorrent API credentials are not configured" >&2
    exit 1
fi

session_dir="$(mktemp -d /tmp/qb-port-forward.XXXXXX)"
cookie_file="$session_dir/cookies"
preferences_file="$session_dir/preferences.json"
umask 077

cleanup() {
    rm -f "$cookie_file" "$preferences_file"
    rmdir "$session_dir" 2>/dev/null || true
}
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

forwarded_port() {
    [ -r "$status_file" ] || return 1
    port="$(tr -d '[:space:]' < "$status_file")"
    case "$port" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || return 1
    printf '%s' "$port"
}

gluetun_is_healthy() {
    curl --silent --fail --connect-timeout 5 --max-time 10 \
        --output /dev/null "$gluetun_health_url"
}

login() {
    rm -f "$cookie_file"
    response="$(
        curl --silent --fail --connect-timeout 5 --max-time 10 \
            --cookie-jar "$cookie_file" \
            --header "Referer: ${qbittorrent_url}/" \
            --data-urlencode "username=${qbittorrent_username}" \
            --data-urlencode "password=${qbittorrent_password}" \
            "${qbittorrent_url}/api/v2/auth/login"
    )" || return 1
    [ "$response" = "Ok." ]
}

logout() {
    curl --silent --connect-timeout 5 --max-time 10 \
        --cookie "$cookie_file" \
        --header "Referer: ${qbittorrent_url}/" \
        --data '' \
        --output /dev/null \
        "${qbittorrent_url}/api/v2/auth/logout" 2>/dev/null || true
}

fetch_preferences() {
    curl --silent --fail --connect-timeout 5 --max-time 10 \
        --cookie "$cookie_file" \
        --header "Referer: ${qbittorrent_url}/" \
        --output "$preferences_file" \
        "${qbittorrent_url}/api/v2/app/preferences"
}

preference_number() {
    key="$1"
    sed -n "s/.*\"${key}\":\([0-9][0-9]*\).*/\1/p" "$preferences_file" | head -1
}

preference_boolean() {
    key="$1"
    sed -n "s/.*\"${key}\":\(true\|false\).*/\1/p" "$preferences_file" | head -1
}

preference_string() {
    key="$1"
    sed -n "s/.*\"${key}\":\"\([^\"]*\)\".*/\1/p" "$preferences_file" | head -1
}

preferences_match() {
    expected_port="$1"
    [ "$(preference_number listen_port)" = "$expected_port" ] &&
        [ "$(preference_string current_network_interface)" = "$vpn_interface" ] &&
        [ "$(preference_boolean random_port)" = "false" ] &&
        [ "$(preference_boolean upnp)" = "false" ]
}

check_port() {
    gluetun_is_healthy || return 1
    expected_port="$(forwarded_port)" || return 1
    login || return 1
    fetch_preferences || {
        logout
        return 1
    }
    if preferences_match "$expected_port"; then
        result=0
    else
        result=$?
    fi
    logout
    return "$result"
}

sync_port() {
    gluetun_is_healthy || return 1
    expected_port="$(forwarded_port)" || return 1
    login || return 1
    payload="{\"listen_port\":${expected_port},\"current_network_interface\":\"${vpn_interface}\",\"random_port\":false,\"upnp\":false}"
    curl --silent --fail --connect-timeout 5 --max-time 10 \
        --cookie "$cookie_file" \
        --header "Referer: ${qbittorrent_url}/" \
        --data-urlencode "json=${payload}" \
        --output /dev/null \
        "${qbittorrent_url}/api/v2/app/setPreferences" || {
        logout
        return 1
    }
    fetch_preferences || {
        logout
        return 1
    }
    if preferences_match "$expected_port"; then
        result=0
    else
        result=$?
    fi
    logout
    [ "$result" -eq 0 ] || return "$result"
    synced_port="$expected_port"
}

status() {
    gluetun_is_healthy || return 1
    expected_port="$(forwarded_port)" || return 1
    login || return 1
    fetch_preferences || {
        logout
        return 1
    }
    actual_port="$(preference_number listen_port)"
    actual_interface="$(preference_string current_network_interface)"
    actual_random_port="$(preference_boolean random_port)"
    actual_upnp="$(preference_boolean upnp)"
    logout
    printf 'forwarded_port=%s listen_port=%s interface=%s random_port=%s upnp=%s\n' \
        "$expected_port" "$actual_port" "$actual_interface" \
        "$actual_random_port" "$actual_upnp"
    [ "$expected_port" = "$actual_port" ] &&
        [ "$actual_interface" = "$vpn_interface" ] &&
        [ "$actual_random_port" = "false" ] &&
        [ "$actual_upnp" = "false" ]
}

case "${1:-}" in
    --check)
        check_port
        exit $?
        ;;
    --once)
        sync_port
        printf 'applied_forwarded_port=%s interface=%s\n' "$synced_port" "$vpn_interface"
        exit 0
        ;;
    --status)
        status
        exit $?
        ;;
    '') ;;
    *)
        echo "Usage: $0 [--check|--once|--status]" >&2
        exit 2
        ;;
esac

last_reported_port=""
failures=0
log "qBittorrent PIA port-forward reconciler started"
while :; do
    if sync_port; then
        if [ "$synced_port" != "$last_reported_port" ]; then
            log "applied PIA forwarded port ${synced_port} to qBittorrent on ${vpn_interface}"
            last_reported_port="$synced_port"
        fi
        failures=0
    else
        failures=$((failures + 1))
        if [ "$failures" -eq 1 ] || [ $((failures % 20)) -eq 0 ]; then
            log "waiting for healthy Gluetun, a valid forwarded port, and qBittorrent API readiness" >&2
        fi
    fi
    sleep "$interval"
done
