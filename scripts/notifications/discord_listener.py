#!/usr/bin/env python3
import os
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL = os.getenv("DISCORD_CHANNEL_ID", "")
if __name__ == "__main__":
    if not TOKEN or not CHANNEL:
        raise SystemExit("Discord env not configured")
