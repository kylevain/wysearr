#!/usr/bin/env python3

import os
import sqlite3
from pathlib import Path

import discord
import yaml

ROOT = Path("/app")
CONFIG = ROOT / "config" / "channels.yml"
DB = Path("/state/huey.db")
SCHEMA = ROOT / "scripts" / "huey" / "schema.sql"

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

if not TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN missing")

with open(CONFIG, "r") as f:
    CHANNELS = yaml.safe_load(f)

REQUEST_CHANNELS = {
    str(channel_id): media_type
    for media_type, channel_id in CHANNELS["requests"].items()
}


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB) as conn:
        with open(SCHEMA, "r") as f:
            conn.executescript(f.read())


def record_request(message, media_type):
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO requests (
                discord_user_id,
                discord_username,
                channel_id,
                message_id,
                media_type,
                raw_request
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(message.author.id),
                str(message.author),
                str(message.channel.id),
                str(message.id),
                media_type,
                message.content,
            ),
        )

        request_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO events (
                request_id,
                event_type,
                message
            )
            VALUES (?, ?, ?)
            """,
            (
                request_id,
                "received",
                "Request received from Discord",
            ),
        )

        return request_id


intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Huey online: {client.user}")
    print(f"Watching channels: {REQUEST_CHANNELS}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    media_type = REQUEST_CHANNELS.get(str(message.channel.id))

    if not media_type:
        return

    request_id = record_request(message, media_type)

    await message.reply(
        f"Request received #{request_id}\n"
        f"Type: {media_type}"
    )


init_db()
client.run(TOKEN)
