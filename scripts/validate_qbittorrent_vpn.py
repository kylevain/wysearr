#!/usr/bin/env python3
"""Secret-safe, non-disruptive validation for qBittorrent behind Gluetun."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[1]
IPV4_PATTERN = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


class CommandError(RuntimeError):
    def __init__(self, command: tuple[str, ...], returncode: int):
        super().__init__(f"command failed with exit {returncode}: {command[0]}")
        self.command = command
        self.returncode = returncode


def run(*command: str, timeout: int = 30) -> str:
    result = subprocess.run(
        command,
        cwd=STACK_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise CommandError(command, result.returncode)
    return result.stdout.strip()


def try_run(*command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=STACK_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def env_value(key: str) -> str:
    path = STACK_ROOT / ".env"
    if not path.exists():
        return ""
    prefix = f"{key}="
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith(prefix):
            return raw_line[len(prefix) :]
    return ""


def container_id(service: str) -> str:
    return run("docker", "compose", "ps", "-q", service)


def container_health(service: str) -> Check:
    try:
        identifier = container_id(service)
        if not identifier:
            return Check(f"container:{service}", False, "container absent")
        state_health = run(
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            identifier,
        )
        ok = state_health == "running healthy"
        return Check(f"container:{service}", ok, state_health)
    except (CommandError, subprocess.TimeoutExpired) as error:
        return Check(f"container:{service}", False, type(error).__name__)


def valid_ipv4(value: str) -> bool:
    if not IPV4_PATTERN.fullmatch(value):
        return False
    return all(0 <= int(part) <= 255 for part in value.split("."))


def add_runtime_checks(checks: list[Check]) -> None:
    gluetun_id = container_id("gluetun")
    qbittorrent_id = container_id("qbittorrent")

    network_mode = run(
        "docker",
        "inspect",
        "--format",
        "{{.HostConfig.NetworkMode}}",
        qbittorrent_id,
    )
    expected_mode = f"container:{gluetun_id}"
    checks.append(
        Check(
            "vpn:shared-network-namespace",
            network_mode == expected_mode,
            "qBittorrent shares Gluetun" if network_mode == expected_mode else "namespace mismatch",
        )
    )

    networks = json.loads(
        run(
            "docker",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            gluetun_id,
        )
    )
    aliases = {
        alias
        for network in networks.values()
        for alias in (network.get("Aliases") or [])
    }
    checks.append(
        Check(
            "vpn:qbittorrent-network-alias",
            "qbittorrent" in aliases,
            "default-network alias present" if "qbittorrent" in aliases else "alias absent",
        )
    )

    qbittorrent_bindings = json.loads(
        run(
            "docker",
            "inspect",
            "--format",
            "{{json .HostConfig.PortBindings}}",
            qbittorrent_id,
        )
        or "{}"
    )
    gluetun_bindings = json.loads(
        run(
            "docker",
            "inspect",
            "--format",
            "{{json .HostConfig.PortBindings}}",
            gluetun_id,
        )
        or "{}"
    )
    expected_address = env_value("WYSEARR_BIND_ADDRESS") or "192.168.4.86"
    expected_port = env_value("QBITTORRENT_PORT") or "8080"
    web_bindings = gluetun_bindings.get("8080/tcp") or []
    webui_owned_by_gluetun = any(
        item.get("HostIp") == expected_address and item.get("HostPort") == expected_port
        for item in web_bindings
    )
    checks.append(
        Check(
            "vpn:published-ports",
            not qbittorrent_bindings and webui_owned_by_gluetun,
            "only Gluetun publishes the unchanged LAN WebUI"
            if not qbittorrent_bindings and webui_owned_by_gluetun
            else "unexpected qBittorrent or Gluetun port bindings",
        )
    )

    cap_add = json.loads(
        run("docker", "inspect", "--format", "{{json .HostConfig.CapAdd}}", gluetun_id)
        or "[]"
    )
    devices = json.loads(
        run("docker", "inspect", "--format", "{{json .HostConfig.Devices}}", gluetun_id)
        or "[]"
    )
    tun_device = any(
        item.get("PathOnHost") == "/dev/net/tun"
        and item.get("PathInContainer") == "/dev/net/tun"
        for item in devices
    )
    checks.append(
        Check(
            "vpn:tunnel-device",
            "NET_ADMIN" in cap_add and tun_device,
            "NET_ADMIN and /dev/net/tun present"
            if "NET_ADMIN" in cap_add and tun_device
            else "missing NET_ADMIN or /dev/net/tun",
        )
    )

    region = run(
        "docker",
        "compose",
        "exec",
        "-T",
        "gluetun",
        "sh",
        "-c",
        'printf "%s" "$SERVER_REGIONS"',
    )
    tunnel_probe = try_run(
        "docker",
        "compose",
        "exec",
        "-T",
        "gluetun",
        "sh",
        "-c",
        "test -d /sys/class/net/tun0 && wget -qO- -T 5 http://127.0.0.1:9999/ >/dev/null",
        timeout=15,
    )
    checks.append(
        Check(
            "vpn:tunnel-status",
            tunnel_probe.returncode == 0 and region == "CA Vancouver",
            f"healthy OpenVPN tunnel; region={region}"
            if tunnel_probe.returncode == 0
            else "tunnel or health endpoint unavailable",
        )
    )

    pf_status = run(
        "docker",
        "compose",
        "exec",
        "-T",
        "qbittorrent-port-forward",
        "/bin/sh",
        "/opt/wysearr/qbittorrent-port-forward.sh",
        "--status",
        timeout=20,
    )
    fields = dict(item.split("=", 1) for item in pf_status.split() if "=" in item)
    forwarded_port = fields.get("forwarded_port", "")
    listen_port = fields.get("listen_port", "")
    pf_ok = (
        forwarded_port.isdigit()
        and 1 <= int(forwarded_port) <= 65535
        and listen_port == forwarded_port
        and fields.get("interface") == "tun0"
        and fields.get("random_port") == "false"
        and fields.get("upnp") == "false"
    )
    checks.append(
        Check(
            "vpn:pia-port-forward",
            pf_ok,
            pf_status if pf_ok else "forwarded port is absent, stale, or not bound to tun0",
        )
    )

    host_ip = run("curl", "-4fsS", "--max-time", "15", "https://api.ipify.org", timeout=20)
    vpn_ip = run(
        "docker",
        "compose",
        "exec",
        "-T",
        "qbittorrent",
        "curl",
        "-4fsS",
        "--max-time",
        "15",
        "https://api.ipify.org",
        timeout=25,
    )
    egress_ok = valid_ipv4(host_ip) and valid_ipv4(vpn_ip) and host_ip != vpn_ip
    checks.append(
        Check(
            "vpn:ipv4-egress",
            egress_ok,
            f"host={host_ip} qbittorrent={vpn_ip}"
            if valid_ipv4(host_ip) and valid_ipv4(vpn_ip)
            else "one or both IPv4 probes failed",
        )
    )

    ipv6_routes = run(
        "docker",
        "compose",
        "exec",
        "-T",
        "qbittorrent",
        "sh",
        "-c",
        "ip -6 route show default || true",
    )
    ipv6_probe = try_run(
        "docker",
        "compose",
        "exec",
        "-T",
        "qbittorrent",
        "curl",
        "-6fsS",
        "--max-time",
        "10",
        "https://api64.ipify.org",
        timeout=20,
    )
    ipv6_ok = not ipv6_routes.strip() and ipv6_probe.returncode != 0
    checks.append(
        Check(
            "vpn:ipv6-leak",
            ipv6_ok,
            "no IPv6 default route and public IPv6 probe blocked"
            if ipv6_ok
            else "usable IPv6 route or public IPv6 response detected",
        )
    )

    webui = try_run(
        "curl",
        "-fsS",
        "--max-time",
        "10",
        "--output",
        "/dev/null",
        f"http://{expected_address}:{expected_port}/",
        timeout=15,
    )
    checks.append(
        Check(
            "vpn:lan-webui",
            webui.returncode == 0,
            f"http://{expected_address}:{expected_port}/"
            if webui.returncode == 0
            else "LAN WebUI request failed",
        )
    )


def validate() -> list[Check]:
    checks: list[Check] = []
    config = try_run("docker", "compose", "config", "--quiet", timeout=30)
    checks.append(
        Check(
            "compose",
            config.returncode == 0,
            "configuration valid" if config.returncode == 0 else "configuration invalid",
        )
    )
    for service in ("gluetun", "qbittorrent", "qbittorrent-port-forward"):
        checks.append(container_health(service))
    try:
        add_runtime_checks(checks)
    except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as error:
        checks.append(Check("vpn:runtime", False, type(error).__name__))
    return checks


def main() -> int:
    checks = validate()
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'}: {check.name}: {check.detail}")
    passed = sum(check.ok for check in checks)
    print(f"{'PASS' if passed == len(checks) else 'FAIL'}: {passed}/{len(checks)} VPN checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
