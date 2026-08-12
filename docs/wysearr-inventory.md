# WyseARR inventory

Captured and reconciled: 2026-08-11.

| Item | Production value |
| --- | --- |
| Host | Dell Wyse 5010, hostname `wysearr` |
| OS | Debian 13, x86_64 |
| Network | `192.168.4.86/22`, wired Gigabit Ethernet |
| CPU | AMD G-T48E, 2 cores / 2 threads |
| Memory | 8 GB RAM, 7.6 GiB swap |
| Local storage | approximately 1 TB SATA SSD |
| Docker | Engine 26.1.5, Compose plugin 2.26.1 |
| DAS mount | `//192.168.4.46/Media` at `/mnt/media` via CIFS |
| Container identity | UID/GID 1000 for media access |

The local SSD holds acquisition activity and disposable retained torrents. The
DAS holds permanent library files. At validation time the DAS had approximately
9.8 TiB free and the required media roots were writable from the containers.
