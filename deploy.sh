#!/usr/bin/env bash
set -euo pipefail
ROOT="${HOME}/homelab"
cd "$ROOT"

if [ ! -f .env ]; then
  cp .env.example .env
fi

mkdir -p \
  config/{qbittorrent,prowlarr,sonarr,radarr,lidarr,readarr,whisparr,bazarr,bookbot} \
  state/torrents/{watch,active,complete,processed-torrents}

if [ ! -d /mnt/media ]; then
  echo "FAIL: /mnt/media missing"
  exit 1
fi

if mountpoint -q /mnt/media; then
  echo "PASS: /mnt/media mounted"
else
  echo "FAIL: /mnt/media is not a mountpoint"
  exit 1
fi

docker compose pull --ignore-buildable
docker compose build bookbot
docker compose up -d

echo "PASS: WyseARR stack deployed"
docker compose ps
