#!/usr/bin/env bash
set -euo pipefail

# Destructive-in-the-small acceptance test: intentionally stop only Gluetun's
# VPN loop, prove qBittorrent has no fallback, and always request recovery.

stack_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$stack_root"

control_url="http://127.0.0.1:8000/v1/vpn/status"
restore_required=0

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

vpn_status() {
    docker compose exec -T gluetun wget -qO- -T 10 "$control_url"
}

set_vpn_status() {
    local status="$1"
    docker compose exec -T gluetun wget -qO- -T 20 \
        --method=PUT \
        --header='Content-Type: application/json' \
        --body-data="{\"status\":\"${status}\"}" \
        "$control_url"
}

host_ipv4() {
    curl -4fsS --connect-timeout 5 --max-time 15 https://api.ipify.org
}

qbittorrent_ipv4() {
    docker compose exec -T qbittorrent \
        curl -4fsS --connect-timeout 5 --max-time 15 https://api.ipify.org
}

wait_for_vpn_state() {
    local expected="$1"
    local timeout_seconds="$2"
    local deadline=$((SECONDS + timeout_seconds))
    local observed
    while (( SECONDS < deadline )); do
        observed="$(vpn_status 2>/dev/null || true)"
        if [[ "$observed" == *"\"status\":\"${expected}\""* ]]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

restore_on_exit() {
    local status=$?
    trap - EXIT
    if [ "$restore_required" -eq 1 ]; then
        echo "WARN: requesting VPN recovery before exit" >&2
        set_vpn_status running >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap restore_on_exit EXIT
trap 'exit 130' INT TERM

docker compose config --quiet
for service in gluetun qbittorrent qbittorrent-port-forward; do
    [ -n "$(docker compose ps --status running -q "$service")" ] || \
        fail "$service is not running"
done

[[ "$(vpn_status)" == *'"status":"running"'* ]] || fail "VPN API is not running"
host_before="$(host_ipv4)"
qbittorrent_before="$(qbittorrent_ipv4)"
[ -n "$host_before" ] && [ -n "$qbittorrent_before" ] || fail "initial IPv4 probe failed"
[ "$host_before" != "$qbittorrent_before" ] || fail "qBittorrent initially shares host public IPv4"
docker compose exec -T qbittorrent-port-forward \
    /bin/sh /opt/wysearr/qbittorrent-port-forward.sh --check
printf 'PASS: precondition host_ipv4=%s qbittorrent_vpn_ipv4=%s\n' \
    "$host_before" "$qbittorrent_before"

restore_required=1
stop_result="$(set_vpn_status stopped)"
[[ "$stop_result" == *'"outcome":"stopped"'* ]] || fail "VPN stop was not acknowledged"
wait_for_vpn_state stopped 30 || fail "VPN did not report stopped"
printf 'PASS: Gluetun VPN control API reports stopped\n'

# Give in-flight DNS and TCP work a moment to leave the tunnel path, then prove
# a brand-new public request cannot fall through eth0.
sleep 3
if docker compose exec -T qbittorrent \
    curl -4fsS --connect-timeout 5 --max-time 12 https://api.ipify.org \
    >/dev/null 2>&1; then
    fail "qBittorrent reached the public Internet while the VPN was stopped"
fi
printf 'PASS: qBittorrent public IPv4 probe is blocked with the VPN stopped\n'

host_during="$(host_ipv4)"
[ -n "$host_during" ] || fail "host WAN failed during the isolated VPN stop"
printf 'PASS: host WAN remains available at %s during the isolated VPN stop\n' "$host_during"

bind_address="$(awk -F= '$1 == "WYSEARR_BIND_ADDRESS" {sub(/^[^=]*=/, ""); print; exit}' .env)"
webui_port="$(awk -F= '$1 == "QBITTORRENT_PORT" {sub(/^[^=]*=/, ""); print; exit}' .env)"
bind_address="${bind_address:-192.168.4.86}"
webui_port="${webui_port:-8080}"
curl -fsS --connect-timeout 5 --max-time 10 --output /dev/null \
    "http://${bind_address}:${webui_port}/" || fail "LAN WebUI failed while VPN was stopped"
printf 'PASS: qBittorrent LAN WebUI remains reachable while Internet egress is blocked\n'

start_result="$(set_vpn_status running)"
[[ "$start_result" == *'"outcome":"running"'* ]] || fail "VPN start was not acknowledged"
wait_for_vpn_state running 30 || fail "VPN did not report running"

deadline=$((SECONDS + 240))
qbittorrent_after=""
while (( SECONDS < deadline )); do
    qbittorrent_after="$(qbittorrent_ipv4 2>/dev/null || true)"
    if [ -n "$qbittorrent_after" ] && \
        [ "$qbittorrent_after" != "$host_during" ] && \
        docker compose exec -T qbittorrent-port-forward \
            /bin/sh /opt/wysearr/qbittorrent-port-forward.sh --check \
            >/dev/null 2>&1; then
        break
    fi
    sleep 3
done
[ -n "$qbittorrent_after" ] || fail "qBittorrent VPN egress did not recover"
[ "$qbittorrent_after" != "$host_during" ] || fail "recovered qBittorrent uses host WAN"
docker compose exec -T qbittorrent-port-forward \
    /bin/sh /opt/wysearr/qbittorrent-port-forward.sh --check || \
    fail "PIA forwarded port did not resynchronize after recovery"
restore_required=0
printf 'PASS: VPN recovered with qbittorrent_vpn_ipv4=%s and port forwarding synchronized\n' \
    "$qbittorrent_after"
printf 'PASS: live qBittorrent kill-switch test completed\n'
