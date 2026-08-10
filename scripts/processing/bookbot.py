#!/usr/bin/env python3
import os, time, shutil
from pathlib import Path

ROOT = Path(os.environ.get("TORRENT_ROOT", "/downloads"))
COMPLETE = ROOT / "complete"
PROCESSED = ROOT / "processed-torrents"
MEDIA = Path(os.environ.get("MEDIA_ROOT", "/media"))

for p in (COMPLETE, PROCESSED):
    p.mkdir(parents=True, exist_ok=True)

while True:
    # Processing hook: preserve payload for seeding; copy only.
    # Legacy BookBot logic can be merged here without changing container wiring.
    time.sleep(60)
