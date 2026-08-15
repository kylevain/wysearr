from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


STACK_ROOT = Path(__file__).resolve().parents[2]


class QbittorrentVpnInfrastructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = (STACK_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.deploy = (STACK_ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.sync = (STACK_ROOT / "scripts/qbittorrent-port-forward.sh").read_text(
            encoding="utf-8"
        )

    def service_block(self, name: str, next_name: str) -> str:
        start = self.compose.index(f"  {name}:\n")
        end = self.compose.index(f"  {next_name}:\n", start)
        return self.compose[start:end]

    def test_gluetun_is_pinned_to_pia_vancouver_with_port_forwarding(self) -> None:
        gluetun = self.service_block("gluetun", "qbittorrent")
        self.assertIn("qmcgaw/gluetun:v3.41.3@sha256:", gluetun)
        self.assertIn("VPN_TYPE: openvpn", gluetun)
        self.assertIn("SERVER_REGIONS: ${VPN_SERVER_REGIONS:-CA Vancouver}", gluetun)
        self.assertIn('PORT_FORWARD_ONLY: "on"', gluetun)
        self.assertIn('VPN_PORT_FORWARDING: "on"', gluetun)
        self.assertIn('IPV6: "off"', gluetun)
        self.assertIn("LOG_LEVEL: warn", gluetun)
        self.assertIn("HTTP_CONTROL_SERVER_ADDRESS: 127.0.0.1:8000", gluetun)
        self.assertIn(
            "./docker/gluetun/auth/config.toml:/gluetun/auth/config.toml:ro",
            gluetun,
        )
        self.assertIn("- NET_ADMIN", gluetun)
        self.assertIn("- /dev/net/tun:/dev/net/tun", gluetun)
        self.assertIn("- qbittorrent", gluetun)

    def test_qbittorrent_is_namespace_shared_and_has_no_host_ports(self) -> None:
        qbittorrent = self.service_block("qbittorrent", "qbittorrent-port-forward")
        self.assertIn("network_mode: service:gluetun", qbittorrent)
        self.assertNotIn("\n    ports:\n", qbittorrent)
        self.assertIn("gluetun:\n        condition: service_healthy", qbittorrent)
        self.assertIn("http://127.0.0.1:9999/", qbittorrent)
        self.assertIn("./config/qbittorrent:/config", qbittorrent)
        self.assertIn(":/downloads", qbittorrent)

    def test_port_forward_reconciler_is_secret_safe_and_fail_closed(self) -> None:
        helper = self.service_block("qbittorrent-port-forward", "prowlarr")
        self.assertIn("network_mode: service:gluetun", helper)
        self.assertIn("gluetun-data:/gluetun:ro", helper)
        self.assertIn("cap_drop:\n      - ALL", helper)
        self.assertIn("read_only: true", helper)
        self.assertIn('--data-urlencode "password=${qbittorrent_password}"', self.sync)
        self.assertNotIn("echo \"$qbittorrent_password", self.sync)
        self.assertIn("current_network_interface", self.sync)
        self.assertIn("random_port", self.sync)
        self.assertIn("upnp", self.sync)
        result = subprocess.run(
            ["sh", "-n", str(STACK_ROOT / "scripts/qbittorrent-port-forward.sh")],
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_lazylibrarian_does_not_retain_early_credential_stdout(self) -> None:
        if "  lazylibrarian:\n" not in self.compose:
            self.skipTest("LazyLibrarian is not part of this checkout")
        lazylibrarian = self.service_block("lazylibrarian", "sabnzbd")
        self.assertIn("logging:\n      driver: none", lazylibrarian)

    def test_whisparr_does_not_retain_unredacted_prowlarr_urls(self) -> None:
        whisparr = self.service_block("whisparr", "bookbot")
        self.assertIn("logging:\n      driver: none", whisparr)

    def test_deploy_migrates_legacy_qbittorrent_then_preserves_good_namespace(self) -> None:
        stop = self.deploy.index("docker compose stop qbittorrent")
        start_gluetun = self.deploy.index("docker compose up -d --no-deps gluetun", stop)
        wait_gluetun = self.deploy.index("wait_for_health 240 gluetun", start_gluetun)
        recreate_qbittorrent = self.deploy.index(
            "docker compose up -d --no-deps --force-recreate qbittorrent",
            wait_gluetun,
        )
        start_sync = self.deploy.index(
            "docker compose up -d --no-deps qbittorrent-port-forward",
            recreate_qbittorrent,
        )
        self.assertLess(stop, start_gluetun)
        self.assertLess(start_gluetun, wait_gluetun)
        self.assertLess(wait_gluetun, recreate_qbittorrent)
        self.assertLess(recreate_qbittorrent, start_sync)
        self.assertIn('"container:$gluetun_container"', self.deploy)


if __name__ == "__main__":
    unittest.main()
