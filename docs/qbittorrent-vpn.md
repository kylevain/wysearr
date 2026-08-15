# qBittorrent VPN architecture and runbook

## Production boundary

qBittorrent is fail-closed behind Private Internet Access (PIA) through the
official Gluetun image. SABnzbd and every other WyseARR service keep their normal
Docker networking and normal Internet route.

The shipped boundary is:

```text
LAN 192.168.4.86:8080 ──published by──> Gluetun network namespace
                                                │
Docker DNS qbittorrent:8080 ──alias──────────────┤
                                                ├── qBittorrent WebUI/API
                                                ├── qBittorrent peer traffic
                                                └── tun0 ──> PIA CA Vancouver
```

`qbittorrent` uses `network_mode: service:gluetun`; it has no network endpoint or
host port of its own. Gluetun owns the `qbittorrent` alias on `wysearr_default`
and publishes the same `${WYSEARR_BIND_ADDRESS}:${QBITTORRENT_PORT}` WebUI mapping
used before the migration. Sonarr, Radarr, Lidarr, Whisparr, ABBA, Huey, BookBot,
Shelfarr, and LazyLibrarian therefore continue to use
`http://qbittorrent:8080`. The old fixed host `6881/tcp` and `6881/udp` peer
mappings are intentionally absent. Gluetun owns PIA's transient forwarded-port
lease and permits that port through the VPN firewall on `tun0`; no peer port is
published on the host NIC.

The persistent qBittorrent mounts are unchanged:

```yaml
- ./config/qbittorrent:/config
- ${TORRENT_ROOT}:/downloads
```

Active, queued, completed, and seeding state remains in the existing config and
download trees. Gluetun's transient server/port-forward state uses the private
Docker volume `wysearr_gluetun-data`; no VPN credential or generated state is
written to Git.

## Tunnel and kill switch

Gluetun `v3.41.3` is tag-and-digest pinned. It uses PIA OpenVPN over UDP with
IPv6 disabled. `CA Vancouver` is the default region pool: Gluetun's bundled PIA
catalog marks it port-forward capable, and it is the lowest-latency compatible
region measured from Hawaii. `PORT_FORWARD_ONLY=on` prevents fallback to a PIA
server without forwarding support.

qBittorrent shares Gluetun's routes and firewall. It has no Docker interface
from which it could use the host WAN directly. If OpenVPN is unavailable,
Gluetun's firewall blocks public egress while permitting established local
Docker/LAN access to the WebUI. qBittorrent's healthcheck requires both
Gluetun's tunnel-health endpoint and its own WebUI before any dependent service
can pass the Compose startup gate.

The Gluetun control API binds only to `127.0.0.1` inside the shared namespace; it
is not published to the host or other Compose services. Its tracked auth policy
explicitly permits only local GET/PUT access to `/v1/vpn/status`, avoiding
Gluetun's implicit-default warning without adding another secret. `LOG_LEVEL=warn`
is deliberate: Gluetun `v3.41.3` can include the PIA username in its INFO
settings summary when port forwarding is enabled. Never change the level to INFO
while collecting or sharing logs unless that upstream behavior has first been
reverified.

Upstream references:

- [Gluetun container sharing](https://github.com/qdm12/gluetun-wiki/blob/main/setup/connect-a-container-to-gluetun.md)
- [PIA provider options](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/private-internet-access.md)
- [VPN port forwarding](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/vpn-port-forwarding.md)
- [Firewall behavior](https://github.com/qdm12/gluetun-wiki/blob/main/faq/firewall.md)
- [Gluetun v3.41.3](https://github.com/passteque/gluetun/releases/tag/v3.41.3)

## Dynamic PIA port forwarding

Gluetun obtains and refreshes PIA's transient forwarded port, permits it through
the VPN firewall on `tun0`, and writes the lease to
`/gluetun/forwarded_port`. The filesystem-read-only, capability-free
`qbittorrent-port-forward` helper checks that file every 15 seconds, logs in to
qBittorrent over shared loopback using the existing private WebUI credentials,
and mutates exactly four qBittorrent preferences:

- `listen_port` equals PIA's current forwarded port;
- `current_network_interface` is `tun0`;
- random-port selection is disabled;
- UPnP/NAT-PMP is disabled.

The helper cannot alter Gluetun's lease, firewall, routes, or network namespace.
It has only a private tmpfs plus read-only mounts for the port file and script,
URL-encodes credentials in the request body, and never prints them. Its
continuous qBittorrent reconciliation avoids the initial startup race of a
one-shot hook and repairs client-preference drift after a Gluetun, qBittorrent,
or host restart.

LazyLibrarian's early config loader can echo downloader settings before its own
redaction policy initializes. The deployed Whisparr release can likewise send a
failed Prowlarr URL to its console target with a query value that its redactor
misses. Both services therefore use Docker logging driver `none` and must not be
included in Docker-log collection commands. Their private application-log trees
remain access-controlled; inspect them only with secret-safe, count-only
diagnostics unless upstream redaction has been independently verified.

Check the live, non-secret forwarding state with:

```bash
docker compose exec -T qbittorrent-port-forward \
  /bin/sh /opt/wysearr/qbittorrent-port-forward.sh --status
```

The reported `forwarded_port` and `listen_port` must match, the interface must
be `tun0`, and both boolean preferences must be `false`. PIA controls the lease
and may assign a different port after reconnect; never hard-code the observed
number.

## Routine validation

Run both validators. They do not acquire media and do not intentionally disrupt
the VPN:

```bash
docker compose config --quiet
python3 scripts/validate_qbittorrent_vpn.py
python3 scripts/validate.py
```

The VPN validator checks container health, namespace identity, Docker alias and
port ownership, tunnel device/region, PIA port synchronization, host-versus-VPN
IPv4 separation, absence of IPv6 egress, and the unchanged LAN WebUI. Use
`docker compose config --quiet`; unsuppressed `docker compose config` expands
and prints secret environment values.

The production validator separately exercises qBittorrent authentication and
categories, ARR download-client tests, ABBA readiness, Shelfarr's client test,
LazyLibrarian configuration, and Huey/BookBot integration. These tests do not
submit an acquisition.

## Live kill-switch acceptance

The following test intentionally stops only Gluetun's VPN loop through its
loopback control API. It proves a fresh public request from qBittorrent fails,
proves the host WAN and qBittorrent LAN WebUI remain available, restores the
tunnel, and waits for both VPN egress and PIA port synchronization. An EXIT trap
requests VPN recovery if the test is interrupted:

```bash
./scripts/test-qbittorrent-killswitch.sh
```

This is an acceptance/maintenance test, not a routine healthcheck. Do not call a
new or changed VPN boundary accepted unless it completes successfully. For an
application-only acquisition release on the same unchanged qBittorrent/Gluetun
runtime, retain the prior live kill-switch proof and run the non-disruptive VPN
validator instead; do not stop a working tunnel merely to repeat identical
evidence. Gluetun `v3.41.3` can retain a stale Docker-health result after an
intentional API stop, so the script checks the control API and real Internet
behavior rather than inferring the result from container health.

## Deployment and recovery

Use `./deploy.sh` for normal convergence. On the first migration it recognizes
the legacy direct-network qBittorrent, stops it to release the LAN WebUI port,
starts and health-gates Gluetun, recreates qBittorrent in the VPN namespace, and
then starts the port reconciler. On later deployments it leaves a correctly
attached running qBittorrent alone unless Gluetun was recreated or the namespace
link is stale.

For acquisition-only releases, record a secret-free aggregate torrent inventory
before and after deployment and require it to be unchanged unless a separately
authorized acquisition or cleanup explains the difference. A correctly attached
runtime retains the same qBittorrent configuration/download mounts, shared
Gluetun namespace, LAN WebUI ownership, and active jobs; rebuilding Huey,
BookBot, or ABBA is not a reason to recreate qBittorrent or Gluetun.

Before manual recovery, create and verify a private checkpoint:

```bash
python3 scripts/backup.py
```

Preferred recovery is another idempotent `./deploy.sh`. For a narrowly scoped
manual recovery:

```bash
docker compose up -d --no-deps gluetun
# Wait until `docker compose ps gluetun` reports healthy.
docker compose up -d --no-deps --force-recreate qbittorrent
# Wait until `docker compose ps qbittorrent` reports healthy.
docker compose up -d --no-deps qbittorrent-port-forward
python3 scripts/validate_qbittorrent_vpn.py
```

If the VPN was intentionally left stopped, request recovery from inside
Gluetun, then run the VPN validator:

```bash
docker compose exec -T gluetun wget -qO- \
  --method=PUT \
  --header='Content-Type: application/json' \
  --body-data='{"status":"running"}' \
  http://127.0.0.1:8000/v1/vpn/status
python3 scripts/validate_qbittorrent_vpn.py
```

Do not put qBittorrent back on `wysearr_default`, add a direct host peer-port
mapping, add a broad `FIREWALL_OUTBOUND_SUBNETS`, or route SABnzbd through
Gluetun as a workaround. If PIA forwarding is temporarily unavailable,
qBittorrent remains VPN-enforced but inbound peer reachability is degraded until
Gluetun reacquires a port and the helper reports healthy.
