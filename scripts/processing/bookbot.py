#!/usr/bin/env python3
"""Production BookBot importer and qBittorrent retention worker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from bookbot_lib.config import BookBotConfig
from bookbot_lib.errors import ConfigurationError
from bookbot_lib.health import check_health_marker, write_health_marker
from bookbot_lib.service import BookBotService


LOGGER = logging.getLogger("bookbot")
STOP = threading.Event()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import completed direct-media torrents and enforce retention"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--validate",
        action="store_true",
        help="validate filesystem, credentials, API access, and category routing",
    )
    action.add_argument(
        "--healthcheck",
        action="store_true",
        help="check the most recent successful-cycle health marker",
    )
    parser.add_argument(
        "--once", action="store_true", help="run one import/retention cycle and exit"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan one cycle without copying, changing categories, or deleting",
    )
    args = parser.parse_args(argv)
    if args.dry_run and (args.validate or args.healthcheck):
        parser.error("--dry-run cannot be combined with --validate or --healthcheck")
    if args.once and (args.validate or args.healthcheck):
        parser.error("--once cannot be combined with --validate or --healthcheck")
    return args


def configure_logging() -> None:
    level_name = os.environ.get("BOOKBOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _positive_int_from_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def run_healthcheck() -> int:
    health_path = Path(
        os.environ.get("BOOKBOT_HEALTH_PATH", "/config/bookbot-health.json")
    )
    poll_seconds = _positive_int_from_env("POLL_SECONDS", 60)
    max_age = _positive_int_from_env(
        "HEALTH_MAX_AGE_SECONDS", max(300, poll_seconds * 3)
    )
    healthy, message = check_health_marker(health_path, max_age)
    print(message)
    return 0 if healthy else 1


def install_signal_handlers() -> None:
    def stop(_signum: int, _frame: object) -> None:
        STOP.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    if args.healthcheck:
        try:
            return run_healthcheck()
        except ConfigurationError as exc:
            print(f"BookBot healthcheck configuration error: {exc}", file=sys.stderr)
            return 2

    try:
        config = BookBotConfig.from_env()
        config.validate_filesystem(require_write=True)
        service = BookBotService(config)
    except Exception as exc:
        LOGGER.error("BookBot startup failed: %s", exc)
        return 2

    try:
        validation = service.validate()
        if args.validate:
            print(json.dumps({"status": "ok", **validation}, sort_keys=True))
            return 0

        install_signal_handlers()
        single_cycle = args.once or args.dry_run
        while not STOP.is_set():
            try:
                counts = service.run_cycle(dry_run=args.dry_run)
                LOGGER.info("Cycle complete: %s", counts.as_dict())
            except Exception as exc:
                LOGGER.exception("BookBot cycle failed")
                try:
                    write_health_marker(
                        config.health_path,
                        "error",
                        message=str(exc),
                    )
                except OSError:
                    LOGGER.exception("Unable to write BookBot error health marker")
                if single_cycle:
                    return 1
            if single_cycle:
                return 0
            STOP.wait(config.poll_seconds)
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
