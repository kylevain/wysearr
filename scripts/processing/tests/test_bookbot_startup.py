from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any

from bookbot_lib.errors import (
    ConfigurationError,
    QbittorrentAuthenticationError,
    QbittorrentUnavailableError,
)


BOOKBOT_PATH = Path(__file__).parents[1] / "bookbot.py"
SPEC = importlib.util.spec_from_file_location("bookbot_entrypoint", BOOKBOT_PATH)
assert SPEC is not None and SPEC.loader is not None
bookbot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bookbot)


class FakeStopEvent:
    def __init__(self) -> None:
        self.waits: list[int] = []
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, seconds: int) -> bool:
        self.waits.append(seconds)
        return self.stopped


class SequenceService:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def validate(self) -> dict[str, object]:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StartupValidationTests(unittest.TestCase):
    def test_transient_unavailability_is_retried_until_validation_succeeds(self) -> None:
        service = SequenceService(
            [
                QbittorrentUnavailableError("connection refused"),
                QbittorrentUnavailableError("HTTP 503"),
                {"qbittorrent_version": "v5.1.4"},
            ]
        )
        stop_event = FakeStopEvent()

        result = bookbot.wait_for_startup_validation(
            service,  # type: ignore[arg-type]
            stop_event=stop_event,  # type: ignore[arg-type]
            retry_seconds=3,
        )

        self.assertEqual({"qbittorrent_version": "v5.1.4"}, result)
        self.assertEqual([3, 3], stop_event.waits)
        self.assertEqual(3, service.calls)

    def test_permanent_configuration_error_is_not_retried(self) -> None:
        service = SequenceService([ConfigurationError("category path mismatch")])
        stop_event = FakeStopEvent()

        with self.assertRaises(ConfigurationError):
            bookbot.wait_for_startup_validation(
                service,  # type: ignore[arg-type]
                stop_event=stop_event,  # type: ignore[arg-type]
            )

        self.assertEqual([], stop_event.waits)
        self.assertEqual(1, service.calls)

    def test_authentication_error_is_not_retried(self) -> None:
        service = SequenceService(
            [QbittorrentAuthenticationError("invalid credentials")]
        )
        stop_event = FakeStopEvent()

        with self.assertRaises(QbittorrentAuthenticationError):
            bookbot.wait_for_startup_validation(
                service,  # type: ignore[arg-type]
                stop_event=stop_event,  # type: ignore[arg-type]
            )

        self.assertEqual([], stop_event.waits)
        self.assertEqual(1, service.calls)

    def test_stop_during_outage_exits_retry_loop_cleanly(self) -> None:
        service = SequenceService([QbittorrentUnavailableError("connection refused")])

        class StopOnWait(FakeStopEvent):
            def wait(self, seconds: int) -> bool:
                self.waits.append(seconds)
                self.stopped = True
                return True

        stop_event = StopOnWait()
        result = bookbot.wait_for_startup_validation(
            service,  # type: ignore[arg-type]
            stop_event=stop_event,  # type: ignore[arg-type]
            retry_seconds=3,
        )

        self.assertIsNone(result)
        self.assertEqual([3], stop_event.waits)
        self.assertEqual(1, service.calls)


if __name__ == "__main__":
    unittest.main()
