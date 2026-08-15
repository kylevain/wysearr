"""Regression tests for the hardened ABBA JSON adapter.

Run in the built adapter image (which supplies upstream ABBA dependencies):

    python -m unittest discover -s /tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import requests


TEST_PATH = Path(__file__).resolve()
ROOT = TEST_PATH.parents[3] if len(TEST_PATH.parents) > 3 else Path("/")
ADAPTER_PATH = ROOT / "docker" / "abba" / "app.py"
if not ADAPTER_PATH.is_file():
    ADAPTER_PATH = Path("/app/app.py")
SPEC = importlib.util.spec_from_file_location(
    "wysearr_abba_adapter", ADAPTER_PATH
)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)

HASH_A = "a" * 40
HASH_B = "b" * 40
PATH_A = "/audio-books/example-book/"
PATH_B = "/audio-books/example-book-alternate/"
CANDIDATE_A = "abba:" + __import__("hashlib").sha256(PATH_A.encode()).hexdigest()
CANDIDATE_B = "abba:" + __import__("hashlib").sha256(PATH_B.encode()).hexdigest()


def settings(database_path: Path, **changes: Any) -> Any:
    values = {
        "abb_hostname": "audiobookbay.lu",
        "qbittorrent_url": "http://qbittorrent:8080",
        "qbittorrent_username": "user",
        "qbittorrent_password": "password",
        "database_path": database_path,
        "search_cache_seconds": 300,
        "search_min_interval_seconds": 0.0,
        "result_ttl_seconds": 86400,
        "http_timeout_seconds": 2.0,
        "qbittorrent_timeout_seconds": 2.0,
        "max_results": 10,
    }
    values.update(changes)
    return adapter.Settings(**values)


def candidate(
    *,
    title: str = "Example Book",
    info_path: str = PATH_A,
    candidate_id: str = CANDIDATE_A,
) -> Any:
    return adapter.Candidate(
        candidate_id=candidate_id,
        path=info_path,
        title=title,
        query_title="Example Book",
        query_author="Example Author",
        author="Example Author",
        narrator="Example Narrator",
        year=2025,
        format="M4B",
        edition="Unabridged",
        size_bytes=1024,
        fingerprint="f" * 64,
    )


class FakeABB:
    def __init__(self) -> None:
        self.search_results = [candidate()]
        self.search_error: Exception | None = None
        self.resolve_error: Exception | None = None
        self.resolve_hash = HASH_A
        self.search_calls = 0
        self.resolve_calls = 0

    def search(self, title: str, author: str | None, limit: int) -> list[Any]:
        self.search_calls += 1
        if self.search_error:
            raise self.search_error
        return list(self.search_results[:limit])

    def resolve(self, item: Any) -> Any:
        self.resolve_calls += 1
        if self.resolve_error:
            raise self.resolve_error
        return adapter.ResolvedResult(
            title=item.title,
            info_hash=self.resolve_hash,
            magnet=f"magnet:?xt=urn:btih:{self.resolve_hash}",
        )


class FakeQbit:
    def __init__(self) -> None:
        self.torrents: dict[str, dict[str, Any]] = {}
        self.category_path = adapter.EXPECTED_SAVE_PATH
        self.category_present = True
        self.destination_error: Exception | None = None
        self.torrent_error: Exception | None = None
        self.add_error: Exception | None = None
        self.add_creates = True
        self.add_calls = 0
        self.add_tag_calls = 0
        self.validate_calls = 0
        self.on_add: Any = None

    def categories(self) -> dict[str, dict[str, Any]]:
        if self.destination_error:
            raise self.destination_error
        if not self.category_present:
            return {}
        return {adapter.EXPECTED_CATEGORY: {"savePath": self.category_path}}

    def validate_destination(self) -> None:
        self.validate_calls += 1
        if self.destination_error:
            raise self.destination_error
        if not self.category_present or self.category_path != adapter.EXPECTED_SAVE_PATH:
            raise adapter.AdapterError(
                "qbit_destination_mismatch", "unsafe destination", 409
            )

    def torrent(self, info_hash: str) -> dict[str, Any] | None:
        if self.torrent_error:
            raise self.torrent_error
        value = self.torrents.get(info_hash)
        return dict(value) if value is not None else None

    def add_torrent(self, magnet: str, tag: str) -> None:
        self.add_calls += 1
        if self.on_add:
            self.on_add()
        if self.add_error:
            raise self.add_error
        match = adapter.re.search(r"btih:([0-9a-f]{40})", magnet)
        if self.add_creates and match:
            self.torrents[match.group(1)] = {
                "hash": match.group(1),
                "name": "Example Book",
                "category": adapter.EXPECTED_CATEGORY,
                "save_path": adapter.EXPECTED_SAVE_PATH,
                "tags": tag,
                "progress": 0.0,
                "state": "queuedDL",
            }

    def add_tag(self, info_hash: str, tag: str) -> None:
        self.add_tag_calls += 1
        current = self.torrents[info_hash]
        tags = self.tags(current)
        tags.add(tag)
        current["tags"] = ", ".join(sorted(tags))

    @staticmethod
    def tags(torrent: Any) -> set[str]:
        return adapter.QbitClient.tags(torrent)

    def validate_torrent(self, torrent: Any, tag: str, *, require_tag: bool) -> None:
        category = str(torrent.get("category") or "")
        save_path = adapter.normalize_save_path(str(torrent.get("save_path") or ""))
        if category != adapter.EXPECTED_CATEGORY or save_path != adapter.EXPECTED_SAVE_PATH:
            raise adapter.AdapterError(
                "qbit_destination_mismatch", "unsafe existing torrent", 409
            )
        if require_tag and tag not in self.tags(torrent):
            raise adapter.AdapterError("qbit_rejected", "missing tag", 502)

    def validate_status_torrent(self, torrent: Any, tag: str) -> bool:
        category = str(torrent.get("category") or "")
        save_path = adapter.normalize_save_path(str(torrent.get("save_path") or ""))
        if category not in {"audiobooks", "audiobooks-imported"} or save_path != adapter.EXPECTED_SAVE_PATH:
            raise adapter.AdapterError(
                "qbit_destination_mismatch", "unsafe status routing", 409
            )
        if tag not in self.tags(torrent):
            raise adapter.AdapterError("qbit_rejected", "missing tag", 502)
        return category == "audiobooks-imported"

    def public_status(self, torrent: Any) -> dict[str, Any]:
        return adapter.QbitClient.public_status(self, torrent)


class FakeResponse:
    def __init__(
        self,
        body: bytes | str = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        json_value: Any = None,
    ) -> None:
        self._body = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.headers = headers or {}
        self._json_value = json_value
        self.closed = False
        self._content = self._body
        self._content_consumed = True

    @property
    def text(self) -> str:
        return bytes(self._content).decode("utf-8", "replace")

    @property
    def content(self) -> bytes:
        return bytes(self._content)

    def iter_content(self, chunk_size: int = 65536):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True

    def json(self) -> Any:
        if isinstance(self._json_value, Exception):
            raise self._json_value
        return self._json_value


class FakeHTTPSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.trust_env = True
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeQbitSession:
    def __init__(
        self,
        *,
        login: list[FakeResponse] | None = None,
        requests_: list[FakeResponse | Exception] | None = None,
    ) -> None:
        self.login = list(login or [FakeResponse("Ok.")])
        self.responses = list(requests_ or [])
        self.headers: dict[str, str] = {}
        self.trust_env = True
        self.post_calls = 0
        self.request_calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls += 1
        return self.login.pop(0)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.request_calls.append({"method": method, "url": url, **kwargs})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class AdapterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "abba.db"
        self.settings = settings(self.database)
        self.journal = adapter.Journal(self.database)
        self.abb = FakeABB()
        self.qbit = FakeQbit()
        self.app = adapter.create_app(
            self.settings,
            journal=self.journal,
            abb=self.abb,
            qbit=self.qbit,
            sleeper=lambda _seconds: None,
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed(self, item: Any | None = None) -> Any:
        value = item or candidate()
        self.journal.store_search("seed", [value], 300, 86400)
        return value

    def search(self) -> Any:
        return self.client.post(
            "/api/search",
            json={"title": "Example Book", "author": "Example Author", "limit": 10},
        )


class ConfigurationTests(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {"DL_USERNAME": "user", "DL_PASSWORD": "secret"}

    def test_canonical_environment_defaults(self) -> None:
        value = adapter.Settings.from_env(self.base_env())
        self.assertEqual(value.port, 5078)
        self.assertEqual(value.database_path, Path("/config/abba.db"))
        self.assertEqual(value.page_limit, 1)

    def test_rejects_noncanonical_database_path(self) -> None:
        env = {**self.base_env(), "ABBA_DB_PATH": "/tmp/abba.db"}
        with self.assertRaisesRegex(adapter.AdapterError, "ABBA_DB_PATH"):
            adapter.Settings.from_env(env)

    def test_rejects_wrong_category_and_path(self) -> None:
        for key, value in (("DL_CATEGORY", "books"), ("SAVE_PATH_BASE", "/tmp")):
            with self.subTest(key=key):
                with self.assertRaises(adapter.AdapterError):
                    adapter.Settings.from_env({**self.base_env(), key: value})

    def test_rejects_dl_url_query_fragment_credentials_and_bad_port(self) -> None:
        bad = (
            "http://qbit:8080/?token=x",
            "http://qbit:8080/#fragment",
            "http://user:pass@qbit:8080",
            "http://qbit:bad",
        )
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(adapter.AdapterError):
                    adapter.Settings.from_env({**self.base_env(), "DL_URL": url})

    def test_result_ttl_cannot_be_shorter_than_cache(self) -> None:
        env = {
            **self.base_env(),
            "ABBA_SEARCH_CACHE_SECONDS": "300",
            "ABBA_RESULT_TTL_SECONDS": "60",
        }
        with self.assertRaises(adapter.AdapterError):
            adapter.Settings.from_env(env)

    def test_correlation_requires_positive_decimal_request_id(self) -> None:
        accepted = ("huey:1", "huey:9223372036854775807")
        rejected = (
            "huey:0",
            "huey:-1",
            "huey:01",
            "huey:abc",
            "huey:9223372036854775808",
            "huey:9999999999999999999",
            "huey:10000000000000000000",
        )
        for value in accepted:
            self.assertIsNotNone(adapter.correlation_request_id(value))
        for value in rejected:
            self.assertIsNone(adapter.correlation_request_id(value))


class JournalTests(AdapterTestCase):
    def test_database_permissions_are_private(self) -> None:
        self.assertEqual(os.stat(self.database).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.database.parent).st_mode & 0o077, 0)

    def test_prepare_is_durable_across_restart(self) -> None:
        row = self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Example", "huey-1")
        reopened = adapter.Journal(self.database)
        self.assertEqual(reopened.acquisition("huey:1")["info_hash"], HASH_A)
        self.assertEqual(row["state"], "prepared")

    def test_terminal_failure_updates_prepared_row(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Example", "huey-1")
        error = adapter.AdapterError("qbit_rejected", "rejected", 502)
        row = self.journal.terminal_failure(
            "huey:1", CANDIDATE_A, "Example", "huey-1", error, HASH_A
        )
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "qbit_rejected")

    def test_correlation_cannot_change_candidate(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Example", "huey-1")
        other = "abba:" + "b" * 64
        with self.assertRaisesRegex(adapter.AdapterError, "another candidate"):
            self.journal.prepare("huey:1", other, HASH_A, "Other", "huey-1")

    def test_correlation_cannot_change_info_hash(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Example", "huey-1")
        with self.assertRaisesRegex(adapter.AdapterError, "result changed"):
            self.journal.prepare("huey:1", CANDIDATE_A, HASH_B, "Example", "huey-1")

    def test_hash_lookup_ignores_released_pre_mutation_failure(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "First", "huey-1")
        self.journal.terminal_failure(
            "huey:1",
            CANDIDATE_A,
            "First",
            "huey-1",
            adapter.AdapterError("qbit_rejected", "rejected", 502),
            HASH_A,
        )
        reopened = adapter.Journal(self.database)
        self.assertIsNone(
            reopened.acquisition("huey:1")["mutation_started_at"]
        )
        second = reopened.prepare(
            "huey:2", CANDIDATE_B, HASH_A, "Second", "huey-2"
        )
        self.assertIsNone(second["canonical_correlation_id"])
        self.assertEqual(
            reopened.acquisition_for_hash(HASH_A)["correlation_id"],
            "huey:2",
        )

    def test_temporary_prepared_owner_never_strands_a_hash_alias(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "First", "huey-1")

        with self.assertRaisesRegex(adapter.AdapterError, "still being established") as raised:
            self.journal.prepare(
                "huey:2", CANDIDATE_B, HASH_A, "Second", "huey-2"
            )
        self.assertEqual(raised.exception.http_status, 503)
        self.assertTrue(raised.exception.retryable)
        self.assertIsNone(self.journal.acquisition("huey:2"))

        self.journal.terminal_failure(
            "huey:1",
            CANDIDATE_A,
            "First",
            "huey-1",
            adapter.AdapterError("qbit_rejected", "rejected", 502),
            HASH_A,
        )
        retried = self.journal.prepare(
            "huey:2", CANDIDATE_B, HASH_A, "Second", "huey-2"
        )
        self.assertIsNone(retried["canonical_correlation_id"])
        self.assertEqual(
            self.journal.acquisition_for_hash(HASH_A)["correlation_id"],
            "huey:2",
        )

    def test_restart_migrates_existing_hash_collision_to_one_owner(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "First", "huey-1")
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_hash_owner_uq")
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'queued', 0, 2, 2)
                """,
                ("huey:2", CANDIDATE_B, HASH_A, "Second", "huey-2"),
            )
        reopened = adapter.Journal(self.database)
        first = reopened.acquisition("huey:1")
        second = reopened.acquisition("huey:2")
        self.assertIsNone(first["canonical_correlation_id"])
        self.assertEqual(second["canonical_correlation_id"], "huey:1")
        self.assertEqual(
            reopened.acquisition_for_hash(HASH_A)["correlation_id"], "huey:1"
        )

    def test_restart_reparents_existing_hash_alias_to_new_lower_root(self) -> None:
        candidate_c = "abba:" + "c" * 64
        self.journal.prepare("huey:2", CANDIDATE_A, HASH_A, "Root", "huey-2")
        self.journal.update("huey:2", "queued")
        alias = self.journal.prepare(
            "huey:3", CANDIDATE_B, HASH_A, "Alias", "huey-3"
        )
        self.assertEqual(alias["canonical_correlation_id"], "huey:2")
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_hash_owner_uq")
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'queued', 0, 0, 0)
                """,
                ("huey:1", candidate_c, HASH_A, "Lower", "huey-1"),
            )

        reopened = adapter.Journal(self.database)
        self.assertEqual(
            reopened.acquisition("huey:2")["canonical_correlation_id"],
            "huey:1",
        )
        self.assertEqual(
            reopened.acquisition("huey:3")["canonical_correlation_id"],
            "huey:1",
        )
        canonical = reopened.canonical_acquisition("huey:3")
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical["correlation_id"], "huey:1")

    def test_restart_reparents_existing_candidate_alias_to_new_lower_root(self) -> None:
        self.journal.prepare("huey:2", CANDIDATE_A, HASH_A, "Root", "huey-2")
        self.journal.update("huey:2", "queued")
        alias = self.journal.prepare(
            "huey:3", CANDIDATE_A, HASH_A, "Alias", "huey-3"
        )
        self.assertEqual(
            alias["canonical_candidate_correlation_id"], "huey:2"
        )
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_hash_owner_uq")
            connection.execute("DROP INDEX acquisitions_candidate_owner_uq")
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'queued', 0, 0, 0)
                """,
                ("huey:1", CANDIDATE_A, HASH_A, "Lower", "huey-1"),
            )

        reopened = adapter.Journal(self.database)
        for correlation_id in ("huey:2", "huey:3"):
            row = reopened.acquisition(correlation_id)
            self.assertEqual(row["canonical_correlation_id"], "huey:1")
            self.assertEqual(
                row["canonical_candidate_correlation_id"], "huey:1"
            )

    def test_restart_retains_failed_root_referenced_by_existing_alias(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Root", "huey-1")
        self.journal.terminal_failure(
            "huey:1",
            CANDIDATE_A,
            "Root",
            "huey-1",
            adapter.AdapterError("qbit_rejected", "rejected", 502),
            HASH_A,
        )
        with self.journal._connect() as connection:
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable,
                    canonical_correlation_id,
                    canonical_candidate_correlation_id,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'prepared', 0, ?, ?, 2, 2)
                """,
                (
                    "huey:2", CANDIDATE_A, HASH_A, "Alias", "huey-2",
                    "huey:1", "huey:1",
                ),
            )

        reopened = adapter.Journal(self.database)
        root = reopened.acquisition("huey:1")
        alias = reopened.acquisition("huey:2")
        self.assertIsNotNone(root["mutation_started_at"])
        self.assertEqual(alias["canonical_correlation_id"], "huey:1")
        self.assertEqual(
            alias["canonical_candidate_correlation_id"], "huey:1"
        )
        self.assertEqual(
            reopened.acquisition_for_hash(HASH_A)["correlation_id"],
            "huey:1",
        )
        self.assertEqual(
            reopened.canonical_acquisition("huey:2")["correlation_id"],
            "huey:1",
        )

    def test_restart_retains_failed_candidate_owner_that_is_hash_alias(self) -> None:
        candidate_c = "abba:" + "c" * 64
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Hash root", "huey-1")
        self.journal.update("huey:1", "queued")
        candidate_owner = self.journal.prepare(
            "huey:2", candidate_c, HASH_A, "Candidate root", "huey-2"
        )
        candidate_alias = self.journal.prepare(
            "huey:3", candidate_c, HASH_A, "Candidate alias", "huey-3"
        )
        self.assertEqual(candidate_owner["canonical_correlation_id"], "huey:1")
        self.assertEqual(
            candidate_alias["canonical_candidate_correlation_id"], "huey:2"
        )
        with self.journal._connect() as connection:
            connection.execute(
                """
                UPDATE acquisitions
                SET state='failed', mutation_started_at=NULL,
                    error_code='qbit_rejected', error_message='legacy failure',
                    error_retryable=0, error_http_status=502
                WHERE correlation_id='huey:2'
                """
            )

        reopened = adapter.Journal(self.database)
        retained = reopened.acquisition("huey:2")
        descendant = reopened.acquisition("huey:3")
        self.assertEqual(retained["canonical_correlation_id"], "huey:1")
        self.assertIsNotNone(retained["mutation_started_at"])
        self.assertEqual(
            descendant["canonical_candidate_correlation_id"], "huey:2"
        )

    def test_restart_retains_prepared_root_before_creating_legacy_alias(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "First", "huey-1")
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_hash_owner_uq")
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'prepared', 0, 2, 2)
                """,
                ("huey:2", CANDIDATE_B, HASH_A, "Second", "huey-2"),
            )

        reopened = adapter.Journal(self.database)
        root = reopened.acquisition("huey:1")
        alias = reopened.acquisition("huey:2")
        self.assertIsNotNone(root["mutation_started_at"])
        self.assertEqual(alias["canonical_correlation_id"], "huey:1")
        failed = reopened.terminal_failure(
            "huey:1",
            CANDIDATE_A,
            "First",
            "huey-1",
            adapter.AdapterError("qbit_rejected", "rejected", 502),
            HASH_A,
        )
        self.assertEqual(failed["state"], "failed")
        self.assertIsNotNone(failed["mutation_started_at"])
        self.assertEqual(
            reopened.acquisition_for_hash(HASH_A)["correlation_id"],
            "huey:1",
        )
        service = adapter.AbbaService(
            self.settings,
            reopened,
            self.abb,
            self.qbit,
            sleeper=lambda _seconds: None,
        )
        duplicate = service.grab(CANDIDATE_B, "huey:2")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["canonical_correlation_id"], "huey:1")
        self.assertEqual(self.qbit.validate_calls, 0)

    def test_restart_prefers_nonfailed_owner_over_lower_failed_mutation(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "First", "huey-1")
        self.journal.update("huey:1", "submitting", mutation_started=True)
        self.journal.terminal_failure(
            "huey:1",
            CANDIDATE_A,
            "First",
            "huey-1",
            adapter.AdapterError("qbit_rejected", "rejected", 502),
            HASH_A,
        )
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_hash_owner_uq")
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'queued', 0, 2, 2)
                """,
                ("huey:2", CANDIDATE_B, HASH_A, "Second", "huey-2"),
            )

        reopened = adapter.Journal(self.database)
        self.assertEqual(
            reopened.acquisition("huey:1")["canonical_correlation_id"],
            "huey:2",
        )
        self.assertIsNone(
            reopened.acquisition("huey:2")["canonical_correlation_id"]
        )
        self.assertEqual(
            reopened.acquisition_for_hash(HASH_A)["correlation_id"], "huey:2"
        )

    def test_restart_prefers_mutated_candidate_owner_over_prepared_reservation(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "First", "huey-1")
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_candidate_owner_uq")
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'queued', 0, 2, 2)
                """,
                ("huey:2", CANDIDATE_A, HASH_B, "Changed", "huey-2"),
            )

        reopened = adapter.Journal(self.database)
        conflict = reopened.acquisition("huey:1")
        owner = reopened.acquisition("huey:2")
        self.assertEqual(
            conflict["canonical_candidate_correlation_id"], "huey:2"
        )
        self.assertIsNone(owner["canonical_candidate_correlation_id"])
        self.assertEqual(conflict["state"], "failed")
        self.assertEqual(conflict["error_code"], "result_changed")
        self.assertEqual(conflict["error_retryable"], 0)
        self.assertEqual(conflict["error_http_status"], 409)
        self.assertIsNone(conflict["mutation_started_at"])
        self.assertIsNotNone(owner["mutation_started_at"])
        self.assertEqual(
            reopened.acquisition_for_hash(HASH_B)["correlation_id"], "huey:2"
        )
        service = adapter.AbbaService(
            self.settings,
            reopened,
            self.abb,
            self.qbit,
            sleeper=lambda _seconds: None,
        )
        replay = service.grab(CANDIDATE_A, "huey:1")
        self.assertEqual(replay["status"], "failed")
        self.assertEqual(replay["error"], "result_changed")
        self.assertEqual(self.qbit.add_calls, 0)

    def test_restart_preserves_candidate_axis_for_different_candidate_hash_alias(self) -> None:
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "A/X", "huey-1")
        self.journal.update("huey:1", "queued")
        hash_alias = self.journal.prepare(
            "huey:2", CANDIDATE_B, HASH_A, "B/X", "huey-2"
        )
        self.assertEqual(hash_alias["canonical_correlation_id"], "huey:1")
        self.assertIsNone(hash_alias["canonical_candidate_correlation_id"])
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_candidate_owner_uq")
            connection.execute(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'queued', 0, 3, 3)
                """,
                ("huey:3", CANDIDATE_B, HASH_B, "B/Y", "huey-3"),
            )

        reopened = adapter.Journal(self.database)
        candidate_owner = reopened.acquisition("huey:2")
        conflict = reopened.acquisition("huey:3")
        self.assertEqual(candidate_owner["canonical_correlation_id"], "huey:1")
        self.assertIsNone(
            candidate_owner["canonical_candidate_correlation_id"]
        )
        self.assertEqual(
            conflict["canonical_candidate_correlation_id"], "huey:2"
        )
        self.assertEqual(conflict["state"], "failed")

    def test_restart_candidate_conflict_wins_over_legacy_hash_alias(self) -> None:
        candidate_c = "abba:" + "c" * 64
        with self.journal._connect() as connection:
            connection.execute("DROP INDEX acquisitions_hash_owner_uq")
            connection.execute("DROP INDEX acquisitions_candidate_owner_uq")
            connection.executemany(
                """
                INSERT INTO acquisitions(
                    correlation_id, candidate_id, info_hash, title, category,
                    save_path, tag, state, error_retryable, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'audiobooks', '/downloads/audiobooks',
                         ?, 'queued', 0, ?, ?)
                """,
                (
                    ("huey:1", CANDIDATE_A, HASH_A, "A/X", "huey-1", 1, 1),
                    ("huey:2", CANDIDATE_B, HASH_A, "B/X", "huey-2", 2, 2),
                    ("huey:3", candidate_c, HASH_B, "C/Y", "huey-3", 3, 3),
                    ("huey:4", CANDIDATE_B, HASH_B, "B/Y", "huey-4", 4, 4),
                ),
            )

        reopened = adapter.Journal(self.database)
        conflict = reopened.acquisition("huey:4")
        self.assertEqual(conflict["canonical_correlation_id"], "huey:3")
        self.assertEqual(
            conflict["canonical_candidate_correlation_id"], "huey:2"
        )
        self.assertEqual(conflict["state"], "failed")
        self.assertEqual(conflict["error_code"], "result_changed")

        service = adapter.AbbaService(
            self.settings,
            reopened,
            self.abb,
            self.qbit,
            sleeper=lambda _seconds: None,
        )
        resumed = service.grab(CANDIDATE_B, "huey:4")
        status = service.status_for_correlation("huey:4")
        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(resumed["error"], "result_changed")
        self.assertEqual(status["job"]["status"], "failed")
        self.assertEqual(status["job"]["error"], "result_changed")
        self.assertEqual(self.qbit.validate_calls, 0)

    def test_sqlite_schema_rejects_wrong_routing_values(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.journal._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO acquisitions(
                        correlation_id, candidate_id, info_hash, title,
                        category, save_path, tag, state, error_code,
                        error_message, error_retryable, error_http_status,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "huey:1", CANDIDATE_A, HASH_A, "Example", "books", "/tmp",
                        "huey-1", "prepared", None, None, 0, None, 0.0, 0.0,
                    ),
                )

    def test_begin_failure_is_not_masked_by_rollback(self) -> None:
        class FailedBegin:
            in_transaction = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, sql: str, *_args: Any) -> None:
                self.calls.append(sql)
                raise sqlite3.OperationalError("begin failed")

            def close(self) -> None:
                pass

        connection = FailedBegin()
        with patch.object(self.journal, "_connect", return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "begin failed"):
                with self.journal._transaction():
                    pass
        self.assertEqual(connection.calls, ["BEGIN IMMEDIATE"])

    def test_ping_proves_database_is_writable(self) -> None:
        real = self.journal._connect()
        statements: list[str] = []

        class ReadOnlyAfterBegin:
            @property
            def in_transaction(self) -> bool:
                return real.in_transaction

            def execute(self, sql: str, parameters: Any = ()) -> Any:
                statements.append(sql)
                if sql.startswith("INSERT INTO service_state"):
                    raise sqlite3.OperationalError("attempt to write a readonly database")
                return real.execute(sql, parameters)

            def close(self) -> None:
                real.close()

        with patch.object(self.journal, "_connect", return_value=ReadOnlyAfterBegin()):
            with self.assertRaises(adapter.AdapterError) as caught:
                self.journal.ping()
        self.assertEqual(caught.exception.code, "database_unavailable")
        self.assertTrue(any(sql.startswith("INSERT INTO service_state") for sql in statements))

    def test_expired_cache_and_unreferenced_candidates_are_pruned(self) -> None:
        now = [1000.0]
        path = Path(self.temp.name) / "prune.db"
        journal = adapter.Journal(path, clock=lambda: now[0])
        old = candidate()
        journal.store_search("old", [old], 1, 1)
        now[0] += 2
        new_path = "/audio-books/new-book/"
        new_id = "abba:" + __import__("hashlib").sha256(new_path.encode()).hexdigest()
        journal.store_search(
            "new", [candidate(title="New", info_path=new_path, candidate_id=new_id)], 10, 10
        )
        with journal._connect() as connection:
            cache_keys = [row[0] for row in connection.execute("SELECT cache_key FROM search_cache")]
            candidate_ids = [row[0] for row in connection.execute("SELECT candidate_id FROM candidates")]
        self.assertEqual(cache_keys, ["new"])
        self.assertEqual(candidate_ids, [new_id])


class ABBClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings(Path(self.temp.name) / "abba.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def search_html(*posts: str) -> str:
        return "<html><body>" + "".join(posts) + "</body></html>"

    @staticmethod
    def post(path: str = PATH_A, title: str = "Example Book") -> str:
        return (
            '<div class="post"><div class="postTitle"><h2>'
            f'<a href="{path}">{title}</a></h2></div>'
            "<div>Author: Example Author\nNarrator: A Reader\nYear: 2025\n"
            "Format: M4B\nEdition: Unabridged\nFile Size: 1 GiB</div></div>"
        )

    @staticmethod
    def detail(title: str = "Example Book", info_hash: str = HASH_A) -> str:
        return (
            f'<div class="postTitle"><h1>{title}</h1></div><table><tr>'
            f"<td>Info Hash</td><td>{info_hash}</td></tr>"
            "<tr><td>https://127.0.0.1/private</td></tr></table>"
        )

    def client(self, *responses: FakeResponse | Exception) -> Any:
        return adapter.ABBClient(self.settings, FakeHTTPSession(list(responses)))

    def test_search_parses_bounded_public_metadata(self) -> None:
        value = self.client(FakeResponse(self.search_html(self.post()))).search(
            "Example", None, 10
        )[0].public_dict()
        self.assertEqual(value["id"], CANDIDATE_A)
        self.assertEqual(value["author"], "Example Author")
        self.assertEqual(value["size_bytes"], 1024**3)
        self.assertNotIn("url", value)

    def test_search_accepts_current_abss_result_paths(self) -> None:
        path = "/abss/the-yellow-wallpaper/"
        value = self.client(
            FakeResponse(self.search_html(self.post(path=path, title="The Yellow Wallpaper")))
        ).search("The Yellow Wallpaper", "Charlotte Perkins Gilman", 10)
        self.assertEqual(len(value), 1)
        self.assertEqual(value[0].path, path)
        self.assertEqual(
            value[0].candidate_id,
            "abba:" + __import__("hashlib").sha256(path.encode()).hexdigest(),
        )

    def test_search_posts_root_form_data_with_fixed_same_origin_referer(self) -> None:
        session = FakeHTTPSession([FakeResponse(self.search_html(self.post()))])
        client = adapter.ABBClient(self.settings, session)
        client.search(
            "The Yellow Wallpaper", "Charlotte Perkins Gilman", 10
        )
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://audiobookbay.lu/")
        self.assertEqual(
            call["data"],
            {"s": "The Yellow Wallpaper Charlotte Perkins Gilman"},
        )
        self.assertEqual(call["headers"], {"Referer": "https://audiobookbay.lu/"})
        self.assertFalse(call["allow_redirects"])
        prepared = requests.Request(
            "POST", call["url"], data=call["data"], headers=call["headers"]
        ).prepare()
        self.assertEqual(
            prepared.body,
            "s=The+Yellow+Wallpaper+Charlotte+Perkins+Gilman",
        )
        self.assertEqual(prepared.headers["Referer"], "https://audiobookbay.lu/")

    def test_search_redirects_are_never_followed_or_replayed(self) -> None:
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                redirected = FakeResponse(
                    status=status, headers={"Location": "/search-results/"}
                )
                session = FakeHTTPSession(
                    [redirected, FakeResponse(self.search_html(self.post()))]
                )
                client = adapter.ABBClient(self.settings, session)
                with self.assertRaises(adapter.AdapterError) as caught:
                    client.search("Example", None, 10)
                self.assertEqual(caught.exception.code, "malformed_upstream")
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(session.calls[0]["method"], "POST")
                self.assertTrue(redirected.closed)

    def test_result_path_allowlist_rejects_near_matches_queries_and_traversal(self) -> None:
        client = self.client()
        rejected = (
            "/abss",
            "/abss-evil/book/",
            "/abssish/book/",
            "/abss/book/?redirect=/private",
            "/abss/%2e%2e/private/",
            "//127.0.0.1/abss/book/",
        )
        for href in rejected:
            with self.subTest(href=href):
                with self.assertRaises(adapter.AdapterError):
                    client._candidate_path(href)

    def test_legacy_audio_books_result_path_remains_supported(self) -> None:
        client = self.client()
        self.assertEqual(client._candidate_path(PATH_A), PATH_A)

    def test_search_sanitizes_links_and_mentions(self) -> None:
        title = "@all https://secret.invalid magnet:?xt=urn:btih:" + HASH_A
        value = self.client(FakeResponse(self.search_html(self.post(title=title)))).search(
            "Example", None, 10
        )[0].title
        self.assertNotIn("https://", value)
        self.assertNotIn("magnet:?", value)
        self.assertNotIn("@", value)

    def test_search_deduplicates_candidate_paths(self) -> None:
        body = self.search_html(self.post(), self.post(title="Duplicate title"))
        values = self.client(FakeResponse(body)).search("Example", None, 10)
        self.assertEqual(len(values), 1)

    def test_explicit_wordpress_no_results_is_valid(self) -> None:
        body = "<html><body>Sorry, no posts matched your criteria.</body></html>"
        self.assertEqual(self.client(FakeResponse(body)).search("Missing", None, 10), [])

    def test_challenge_page_fails_closed(self) -> None:
        body = "<html><title>Just a moment...</title>Checking your browser</html>"
        with self.assertRaisesRegex(adapter.AdapterError, "invalid search"):
            self.client(FakeResponse(body)).search("Example", None, 10)

    def test_malformed_posts_fail_closed(self) -> None:
        body = '<div class="post"><div>missing title anchor</div></div>'
        with self.assertRaisesRegex(adapter.AdapterError, "malformed search"):
            self.client(FakeResponse(body)).search("Example", None, 10)

    def test_upstream_timeout_is_safe(self) -> None:
        with self.assertRaisesRegex(adapter.AdapterError, "temporarily unreachable") as caught:
            self.client(requests.Timeout("contains https://secret.invalid")).search(
                "Example", None, 10
            )
        self.assertEqual(caught.exception.code, "abb_unreachable")
        self.assertNotIn("secret", caught.exception.message)

    def test_cross_host_redirect_is_rejected(self) -> None:
        response = FakeResponse(status=302, headers={"Location": "https://127.0.0.1/"})
        with self.assertRaisesRegex(adapter.AdapterError, "invalid reference"):
            self.client(response)._get(PATH_A)

    def test_malformed_url_port_is_normalized(self) -> None:
        client = self.client()
        with self.assertRaises(adapter.AdapterError) as caught:
            client._validated_url("https://audiobookbay.lu:bad/path")
        self.assertEqual(caught.exception.code, "malformed_upstream")

    def test_declared_oversized_body_is_rejected_before_iteration(self) -> None:
        response = FakeResponse(
            "small", headers={"Content-Length": str(adapter.MAX_UPSTREAM_BYTES + 1)}
        )
        with self.assertRaisesRegex(adapter.AdapterError, "oversized"):
            self.client(response).search("Example", None, 10)
        self.assertTrue(response.closed)

    def test_chunked_oversized_body_is_rejected(self) -> None:
        response = FakeResponse(b"x" * (adapter.MAX_UPSTREAM_BYTES + 1))
        with self.assertRaisesRegex(adapter.AdapterError, "oversized"):
            self.client(response).search("Example", None, 10)

    def test_resolve_revalidates_title_and_info_hash(self) -> None:
        valid = self.client(FakeResponse(self.detail())).resolve(candidate())
        self.assertEqual(valid.info_hash, HASH_A)
        self.assertTrue(valid.magnet.startswith("magnet:?xt=urn:btih:" + HASH_A))
        self.assertNotIn("127.0.0.1", valid.magnet)

    def test_resolve_accepts_only_matching_audiobook_format_decoration(self) -> None:
        decorated = self.client(
            FakeResponse(self.detail(title="Example Book Audiobook M4B"))
        ).resolve(candidate())
        self.assertEqual(decorated.info_hash, HASH_A)
        with self.assertRaises(adapter.AdapterError) as caught:
            self.client(
                FakeResponse(self.detail(title="Example Book Audiobook MP3"))
            ).resolve(candidate())
        self.assertEqual(caught.exception.code, "result_changed")

    def test_disappearing_or_changed_result_fails(self) -> None:
        for response in (
            FakeResponse(status=404),
            FakeResponse(self.detail(title="Another Book")),
        ):
            with self.subTest(status=response.status_code):
                with self.assertRaises(adapter.AdapterError) as caught:
                    self.client(response).resolve(candidate())
                self.assertEqual(caught.exception.code, "result_changed")

    def test_title_substrings_never_pass_fresh_revalidation(self) -> None:
        cases = (("It", "The Institute"), ("Dune", "Dune Messiah"))
        for selected, detail in cases:
            with self.subTest(selected=selected, detail=detail):
                with self.assertRaises(adapter.AdapterError) as caught:
                    self.client(FakeResponse(self.detail(title=detail))).resolve(
                        candidate(title=selected)
                    )
                self.assertEqual(caught.exception.code, "result_changed")

    def test_interrupted_stream_is_retryable_abb_outage(self) -> None:
        class Interrupted(FakeResponse):
            def iter_content(self, chunk_size: int = 65536):
                yield b"partial"
                raise requests.ConnectionError("secret URL")

        response = Interrupted()
        with self.assertRaises(adapter.AdapterError) as caught:
            self.client(response).search("Example", None, 10)
        self.assertEqual(caught.exception.code, "abb_unreachable")
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(response.closed)

    def test_shared_abb_session_serializes_full_stream_lifecycle(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        body = self.search_html(self.post()).encode()

        class Blocking(FakeResponse):
            def iter_content(self, chunk_size: int = 65536):
                entered.set()
                self.assert_release()
                yield self._body

            @staticmethod
            def assert_release() -> None:
                if not release.wait(timeout=2):
                    raise AssertionError("test stream release timed out")

        session = FakeHTTPSession([Blocking(body), FakeResponse(body)])
        client = adapter.ABBClient(self.settings, session)
        failures: list[Exception] = []

        def run(query: str) -> None:
            try:
                client.search(query, None, 1)
            except Exception as error:  # pragma: no cover - assertion reports it
                failures.append(error)

        first = threading.Thread(target=run, args=("One",))
        second = threading.Thread(target=run, args=("Two",))
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        second.start()
        second.join(timeout=0.05)
        self.assertEqual(len(session.calls), 1)
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(failures)
        self.assertEqual(len(session.calls), 2)

    def test_invalid_info_hash_is_magnet_failure(self) -> None:
        with self.assertRaises(adapter.AdapterError) as caught:
            self.client(FakeResponse(self.detail(info_hash="not-a-hash"))).resolve(
                candidate()
            )
        self.assertEqual(caught.exception.code, "magnet_failure")


class QbitClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings(Path(self.temp.name) / "abba.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_403_reauthentication_does_not_deadlock(self) -> None:
        session = FakeQbitSession(
            login=[FakeResponse("Ok."), FakeResponse("Ok.")],
            requests_=[
                FakeResponse(status=403),
                FakeResponse(json_value={"audiobooks": {"savePath": adapter.EXPECTED_SAVE_PATH}}),
            ],
        )
        client = adapter.QbitClient(self.settings, session)
        result: list[Any] = []
        thread = threading.Thread(target=lambda: result.append(client.categories()))
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertIn("audiobooks", result[0])
        self.assertEqual(session.post_calls, 2)

    def test_transport_during_add_is_submission_uncertain(self) -> None:
        session = FakeQbitSession(requests_=[requests.Timeout("secret URL")])
        client = adapter.QbitClient(self.settings, session)
        with self.assertRaises(adapter.AdapterError) as caught:
            client.add_torrent("magnet:?xt=urn:btih:" + HASH_A, "huey-1")
        self.assertEqual(caught.exception.code, "submission_uncertain")

    def test_torrent_lookup_requires_exact_hash(self) -> None:
        session = FakeQbitSession(
            requests_=[FakeResponse(json_value=[{"hash": HASH_B}])]
        )
        client = adapter.QbitClient(self.settings, session)
        self.assertIsNone(client.torrent(HASH_A))

    def test_destination_validation_fails_closed(self) -> None:
        session = FakeQbitSession(
            requests_=[FakeResponse(json_value={"audiobooks": {"savePath": "/tmp"}})]
        )
        with self.assertRaises(adapter.AdapterError) as caught:
            adapter.QbitClient(self.settings, session).validate_destination()
        self.assertEqual(caught.exception.code, "qbit_destination_mismatch")

    def test_public_status_supports_qbit_v5_stopped_up(self) -> None:
        value = adapter.QbitClient.public_status(
            object(), {"progress": 1.0, "state": "stoppedUP", "name": "Book"}
        )
        self.assertEqual(value["status"], "downloaded")

    def test_public_status_reports_terminal_states(self) -> None:
        for state in ("error", "missingFiles"):
            with self.subTest(state=state):
                value = adapter.QbitClient.public_status(
                    object(), {"progress": 0.5, "state": state, "name": "Book"}
                )
                self.assertEqual(value["status"], "failed")

    def test_public_status_rejects_bool_and_nonfinite_progress(self) -> None:
        for progress in (True, float("nan"), float("inf"), "0.5"):
            with self.subTest(progress=progress):
                with self.assertRaises(adapter.AdapterError):
                    adapter.QbitClient.public_status(
                        object(), {"progress": progress, "state": "downloading"}
                    )


class RouteContractTests(AdapterTestCase):
    def test_health_has_exact_ready_schema_and_never_calls_abb(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "status": "ok",
                "service": "abba",
                "checks": {
                    "database": "ok",
                    "qbittorrent": "ok",
                    "category": "ok",
                    "save_path": "ok",
                },
            },
        )
        self.assertEqual(self.abb.search_calls + self.abb.resolve_calls, 0)

    def test_health_reports_qbit_outage_without_secrets(self) -> None:
        self.qbit.destination_error = adapter.AdapterError(
            "qbit_unreachable", "qBittorrent is unreachable", 503, retryable=True
        )
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        value = response.get_json()
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["checks"]["qbittorrent"], "error")

    def test_only_json_api_is_exposed(self) -> None:
        for path in ("/", "/send", "/status"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.content_type, "application/json")

    def test_search_requires_strict_json_fields(self) -> None:
        values = (
            self.client.post("/api/search", data="title=x"),
            self.client.post("/api/search", json={"title": "x", "url": "bad"}),
            self.client.post("/api/search", json={"title": "x", "limit": True}),
            self.client.post("/api/search", json={"title": "x", "limit": 11}),
        )
        self.assertTrue(all(response.status_code in {400, 415} for response in values))

    def test_search_response_contract_and_cache(self) -> None:
        first = self.search()
        second = self.search()
        self.assertEqual(set(first.get_json()), {"results", "cached"})
        self.assertFalse(first.get_json()["cached"])
        self.assertTrue(second.get_json()["cached"])
        self.assertEqual(self.abb.search_calls, 1)
        self.assertNotIn("path", first.get_json()["results"][0])

    def test_versioned_search_key_does_not_reuse_version_2_cache(self) -> None:
        legacy_value = adapter.json.dumps(
            {
                "version": 2,
                "title": adapter.normalize_identity("Example Book"),
                "author": adapter.normalize_identity("Example Author"),
                "limit": 10,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        legacy_key = __import__("hashlib").sha256(legacy_value.encode()).hexdigest()
        with self.journal._connect() as connection:
            connection.execute(
                "INSERT INTO search_cache(cache_key, payload_json, created_at, expires_at) "
                "VALUES(?, ?, ?, ?)",
                (
                    legacy_key,
                    '[{"id":"abba:' + "0" * 64 + '","title":"stale"}]',
                    self.journal.clock(),
                    self.journal.clock() + 300,
                ),
            )
        response = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["cached"])
        self.assertEqual(self.abb.search_calls, 1)
        self.assertEqual(response.get_json()["results"][0]["title"], "Example Book")

    def test_search_rate_limit_avoids_second_abb_call(self) -> None:
        self.settings = settings(
            self.database, search_min_interval_seconds=60.0
        )
        self.app = adapter.create_app(
            self.settings,
            journal=self.journal,
            abb=self.abb,
            qbit=self.qbit,
            sleeper=lambda _seconds: None,
        )
        client = self.app.test_client()
        first = client.post("/api/search", json={"title": "One", "limit": 1})
        second = client.post("/api/search", json={"title": "Two", "limit": 1})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(self.abb.search_calls, 1)

    def test_grab_rejects_legacy_routing_fields(self) -> None:
        self.seed()
        response = self.client.post(
            "/api/grab",
            json={
                "candidate_id": CANDIDATE_A,
                "correlation_id": "huey:1",
                "category": "audiobooks",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_grab_and_status_contract(self) -> None:
        self.seed()
        response = self.client.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": "huey:1"},
        )
        self.assertEqual(response.status_code, 200)
        job = response.get_json()["job"]
        self.assertEqual(
            set(job),
            {
                "correlation_id", "candidate_id", "status", "info_hash",
                "title", "category", "save_path", "tags",
            },
        )
        self.assertEqual(job["info_hash"], HASH_A)
        self.assertEqual(job["category"], "audiobooks")
        self.assertEqual(job["save_path"], "/downloads/audiobooks")
        self.assertEqual(job["tags"], ["huey-1"])
        status = self.client.get("/api/status?correlation_id=huey:1").get_json()
        self.assertTrue(status["found"])

    def test_missing_status_contract_is_strict(self) -> None:
        response = self.client.get("/api/status?correlation_id=huey:7")
        self.assertEqual(
            response.get_json(), {"found": False, "correlation_id": "huey:7"}
        )
        self.assertEqual(self.client.get("/api/status").status_code, 400)
        self.assertEqual(
            self.client.get("/api/status?correlation_id=huey:0").status_code, 400
        )
        self.assertEqual(
            self.client.get(
                "/api/status?correlation_id=huey:9223372036854775807"
            ).status_code,
            200,
        )
        for request_id in (
            "9223372036854775808",
            "9999999999999999999",
        ):
            self.assertEqual(
                self.client.get(
                    f"/api/status?correlation_id=huey:{request_id}"
                ).status_code,
                400,
            )

    def test_hash_status_rejects_malformed_values(self) -> None:
        self.assertEqual(self.client.get("/api/status/not-a-hash").status_code, 400)
        self.assertEqual(self.client.get("/api/status/" + HASH_A.upper()).status_code, 400)


class AcquisitionTests(AdapterTestCase):
    def grab(self, correlation: str = "huey:1") -> dict[str, Any]:
        return self.client.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": correlation},
        ).get_json()["job"]

    def test_prepare_is_committed_before_qbit_mutation(self) -> None:
        self.seed()
        observed: list[str] = []
        self.qbit.on_add = lambda: observed.append(
            self.journal.acquisition("huey:1")["state"]
        )
        job = self.grab()
        self.assertEqual(observed, ["submitting"])
        self.assertEqual(job["status"], "queued")

    def test_duplicate_grab_adds_exact_hash_once(self) -> None:
        self.seed()
        first = self.grab()
        second = self.grab()
        self.assertEqual(first["info_hash"], second["info_hash"])
        self.assertEqual(self.qbit.add_calls, 1)

    def test_different_candidates_same_hash_alias_before_qbit_mutation(self) -> None:
        alternate = candidate(
            title="Example Book Alternate",
            info_path=PATH_B,
            candidate_id=CANDIDATE_B,
        )
        self.journal.store_search("seed", [candidate(), alternate], 300, 86400)
        service = self.app.extensions["abba_service"]
        first = service.grab(CANDIDATE_A, "huey:1")
        second = service.grab(CANDIDATE_B, "huey:2")

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["canonical_correlation_id"], "huey:1")
        self.assertEqual(second["canonical_candidate_id"], CANDIDATE_A)
        self.assertEqual(second["tags"], ["huey-1"])
        self.assertEqual(self.qbit.add_calls, 1)
        self.assertEqual(self.qbit.add_tag_calls, 0)
        self.assertEqual(self.qbit.torrents[HASH_A]["tags"], "huey-1")
        self.assertEqual(
            self.journal.acquisition("huey:2")["canonical_correlation_id"],
            "huey:1",
        )

    def test_same_candidate_same_hash_records_both_canonical_axes(self) -> None:
        self.seed()
        first = self.grab("huey:1")
        second = self.grab("huey:2")

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["canonical_correlation_id"], "huey:1")
        self.assertEqual(second["canonical_candidate_id"], CANDIDATE_A)
        self.assertEqual(self.qbit.add_calls, 1)
        alias = self.journal.acquisition("huey:2")
        self.assertEqual(alias["state"], "prepared")
        self.assertIsNone(alias["error_code"])
        self.assertEqual(alias["canonical_correlation_id"], "huey:1")
        self.assertEqual(
            alias["canonical_candidate_correlation_id"], "huey:1"
        )

    def test_prepared_outage_does_not_strand_later_hash_request(self) -> None:
        alternate = candidate(
            title="Example Book Alternate",
            info_path=PATH_B,
            candidate_id=CANDIDATE_B,
        )
        self.journal.store_search("seed", [candidate(), alternate], 300, 86400)
        service = self.app.extensions["abba_service"]
        self.qbit.destination_error = adapter.AdapterError(
            "qbit_unreachable", "unreachable", 503, retryable=True
        )

        with self.assertRaises(adapter.AdapterError) as first_error:
            service.grab(CANDIDATE_A, "huey:1")
        self.assertEqual(first_error.exception.code, "qbit_unreachable")
        with self.assertRaises(adapter.AdapterError) as pending_error:
            service.grab(CANDIDATE_B, "huey:2")
        self.assertEqual(pending_error.exception.code, "acquisition_pending")
        self.assertTrue(pending_error.exception.retryable)
        self.assertIsNone(self.journal.acquisition("huey:2"))

        self.qbit.destination_error = None
        self.abb.resolve_hash = HASH_B
        failed = service.grab(CANDIDATE_A, "huey:1")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "result_changed")
        self.abb.resolve_hash = HASH_A
        retried = service.grab(CANDIDATE_B, "huey:2")
        self.assertEqual(retried["status"], "queued")
        self.assertIsNone(
            self.journal.acquisition("huey:2")["canonical_correlation_id"]
        )
        self.assertEqual(self.qbit.add_calls, 1)

    def test_same_candidate_different_hash_is_quarantined_before_qbit_mutation(self) -> None:
        self.seed()
        first = self.grab("huey:1")
        self.abb.resolve_hash = HASH_B
        second = self.grab("huey:2")

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["error"], "result_changed")
        self.assertEqual(self.qbit.add_calls, 1)
        conflict = self.journal.acquisition("huey:2")
        self.assertEqual(
            conflict["canonical_candidate_correlation_id"], "huey:1"
        )
        self.assertIsNone(conflict["canonical_correlation_id"])
        with self.journal._connect() as connection:
            indexes = {
                row[1]
                for row in connection.execute("PRAGMA index_list(acquisitions)")
            }
        self.assertIn("acquisitions_candidate_owner_uq", indexes)

    def test_mixed_candidate_and_hash_axes_never_create_b_y_owner(self) -> None:
        alternate = candidate(
            title="Example Book Alternate",
            info_path=PATH_B,
            candidate_id=CANDIDATE_B,
        )
        self.journal.store_search("seed", [candidate(), alternate], 300, 86400)
        service = self.app.extensions["abba_service"]
        first = service.grab(CANDIDATE_A, "huey:1")
        second = service.grab(CANDIDATE_B, "huey:2")
        self.abb.resolve_hash = HASH_B
        third = service.grab(CANDIDATE_B, "huey:3")

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(third["status"], "failed")
        self.assertEqual(third["error"], "result_changed")
        self.assertEqual(self.qbit.add_calls, 1)
        candidate_owner = self.journal.acquisition("huey:2")
        conflict = self.journal.acquisition("huey:3")
        self.assertEqual(candidate_owner["canonical_correlation_id"], "huey:1")
        self.assertIsNone(
            candidate_owner["canonical_candidate_correlation_id"]
        )
        self.assertEqual(
            conflict["canonical_candidate_correlation_id"], "huey:2"
        )

    def test_live_candidate_and_hash_conflict_records_both_safe_roots(self) -> None:
        candidate_c_id = "abba:" + "c" * 64
        candidate_c = candidate(
            title="Third Book",
            info_path="/audio-books/third-book/",
            candidate_id=candidate_c_id,
        )
        alternate = candidate(
            title="Example Book Alternate",
            info_path=PATH_B,
            candidate_id=CANDIDATE_B,
        )
        self.journal.store_search(
            "seed", [candidate(), alternate, candidate_c], 300, 86400
        )
        service = self.app.extensions["abba_service"]
        service.grab(CANDIDATE_A, "huey:1")
        service.grab(CANDIDATE_B, "huey:2")
        self.abb.resolve_hash = HASH_B
        service.grab(candidate_c_id, "huey:3")

        conflict_job = service.grab(CANDIDATE_B, "huey:4")
        conflict = self.journal.acquisition("huey:4")
        self.assertEqual(conflict_job["status"], "failed")
        self.assertEqual(conflict_job["error"], "result_changed")
        self.assertEqual(conflict["canonical_correlation_id"], "huey:3")
        self.assertEqual(
            conflict["canonical_candidate_correlation_id"], "huey:2"
        )
        self.assertEqual(
            service.status_for_correlation("huey:4")["job"]["status"],
            "failed",
        )
        self.assertEqual(self.qbit.add_calls, 2)

    def test_restart_recovers_without_second_add(self) -> None:
        self.seed()
        self.grab()
        reopened = adapter.Journal(self.database)
        restarted = adapter.create_app(
            self.settings,
            journal=reopened,
            abb=FakeABB(),
            qbit=self.qbit,
            sleeper=lambda _seconds: None,
        ).test_client()
        response = restarted.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": "huey:1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.qbit.add_calls, 1)

    def test_existing_exact_hash_is_reused_and_tagged(self) -> None:
        self.seed()
        self.qbit.torrents[HASH_A] = {
            "hash": HASH_A, "name": "Book", "category": "audiobooks",
            "save_path": "/downloads/audiobooks", "tags": "existing",
            "progress": 0.2, "state": "downloading",
        }
        job = self.grab()
        self.assertEqual(self.qbit.add_calls, 0)
        self.assertEqual(self.qbit.add_tag_calls, 1)
        self.assertEqual(job["status"], "queued")

    def test_existing_hash_wrong_category_fails_closed(self) -> None:
        self.seed()
        self.qbit.torrents[HASH_A] = {
            "hash": HASH_A, "name": "Book", "category": "movies",
            "save_path": "/downloads/movies", "tags": "", "progress": 0.0,
            "state": "queuedDL",
        }
        job = self.grab()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "qbit_destination_mismatch")
        self.assertEqual(self.qbit.add_calls, 0)

    def test_qbit_outage_before_add_is_safe_and_retriable(self) -> None:
        self.seed()
        self.qbit.destination_error = adapter.AdapterError(
            "qbit_unreachable", "unreachable", 503, retryable=True
        )
        response = self.client.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": "huey:1"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "qbit_unreachable")
        self.assertEqual(self.qbit.add_calls, 0)
        self.assertEqual(self.journal.acquisition("huey:1")["state"], "prepared")

    def test_qbit_explicit_rejection_is_terminal(self) -> None:
        self.seed()
        self.qbit.add_error = adapter.AdapterError(
            "qbit_rejected", "rejected", 502
        )
        first = self.grab()
        second = self.grab()
        self.assertEqual(first["error"], "qbit_rejected")
        self.assertEqual(second["error"], "qbit_rejected")
        self.assertEqual(self.qbit.add_calls, 1)
        self.assertEqual(self.journal.acquisition("huey:1")["state"], "failed")

    def test_transport_during_add_is_never_replayed(self) -> None:
        self.seed()
        self.qbit.add_error = adapter.AdapterError(
            "submission_uncertain", "uncertain", 503, retryable=True
        )
        first = self.client.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": "huey:1"},
        )
        self.qbit.add_error = None
        second = self.client.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": "huey:1"},
        )
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 503)
        self.assertEqual(first.get_json()["error"]["code"], "submission_uncertain")
        self.assertEqual(second.get_json()["error"]["code"], "submission_uncertain")
        self.assertEqual(self.qbit.add_calls, 1)

    def test_unconfirmed_success_is_never_replayed(self) -> None:
        self.seed()
        self.qbit.add_creates = False
        first = self.client.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": "huey:1"},
        )
        second = self.client.post(
            "/api/grab",
            json={"candidate_id": CANDIDATE_A, "correlation_id": "huey:1"},
        )
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 503)
        self.assertEqual(first.get_json()["error"]["code"], "submission_uncertain")
        self.assertEqual(self.qbit.add_calls, 1)

    def test_prepared_status_absent_returns_not_found_for_exact_resume(self) -> None:
        self.seed()
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Book", "huey-1")
        response = self.client.get("/api/status?correlation_id=huey:1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(), {"found": False, "correlation_id": "huey:1"}
        )

    def test_uncertain_status_absent_stays_nonterminal_503(self) -> None:
        self.seed()
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Book", "huey-1")
        error = adapter.AdapterError(
            "submission_uncertain", "uncertain", 503, retryable=True
        )
        self.journal.update("huey:1", "submission_uncertain", error)
        response = self.client.get("/api/status?correlation_id=huey:1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "submission_uncertain")
        self.assertNotEqual(self.journal.acquisition("huey:1")["state"], "failed")

    def test_restart_uncertain_exact_hash_recovers_without_add(self) -> None:
        self.seed()
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Book", "huey-1")
        self.journal.update(
            "huey:1",
            "submission_uncertain",
            adapter.AdapterError("submission_uncertain", "uncertain", 503, retryable=True),
        )
        self.qbit.torrents[HASH_A] = {
            "hash": HASH_A, "name": "Book", "category": "audiobooks",
            "save_path": "/downloads/audiobooks", "tags": "huey-1",
            "progress": 0.1, "state": "downloading",
        }
        reopened = adapter.Journal(self.database)
        restarted = adapter.create_app(
            self.settings,
            journal=reopened,
            abb=FakeABB(),
            qbit=self.qbit,
            sleeper=lambda _seconds: None,
        ).test_client()
        response = restarted.get("/api/status?correlation_id=huey:1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job"]["status"], "downloading")
        self.assertEqual(self.qbit.add_calls, 0)

    def test_committed_state_qbit_outage_is_submission_uncertain(self) -> None:
        self.seed()
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Book", "huey-1")
        self.journal.update("huey:1", "queued")
        self.qbit.torrent_error = adapter.AdapterError(
            "qbit_unreachable", "unreachable", 503, retryable=True
        )
        response = self.client.get("/api/status?correlation_id=huey:1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "submission_uncertain")

    def test_disappearing_result_is_durable_without_qbit_call(self) -> None:
        self.seed()
        self.abb.resolve_error = adapter.AdapterError(
            "result_changed", "gone", 409
        )
        first = self.grab()
        self.abb.resolve_error = None
        second = self.grab()
        self.assertEqual(first["error"], "result_changed")
        self.assertIsNone(first["info_hash"])
        self.assertEqual(second["error"], "result_changed")
        self.assertEqual(self.abb.resolve_calls, 1)
        self.assertEqual(self.qbit.validate_calls, 0)

    def test_magnet_failure_is_durable_without_qbit_call(self) -> None:
        self.seed()
        self.abb.resolve_error = adapter.AdapterError(
            "magnet_failure", "missing hash", 502
        )
        first = self.grab()
        second = self.grab()
        self.assertEqual(first["error"], "magnet_failure")
        self.assertEqual(second["error"], "magnet_failure")
        self.assertIsNone(first["info_hash"])
        self.assertEqual(self.qbit.add_calls, 0)

    def test_unknown_candidate_is_durable_result_changed(self) -> None:
        first = self.grab()
        self.seed()
        second = self.grab()
        self.assertEqual(first["error"], "result_changed")
        self.assertEqual(second["error"], "result_changed")
        self.assertEqual(self.abb.resolve_calls, 0)

    def test_prepared_retry_never_submits_changed_hash(self) -> None:
        self.seed()
        self.journal.prepare("huey:1", CANDIDATE_A, HASH_A, "Book", "huey-1")
        self.abb.resolve_hash = HASH_B
        job = self.grab()
        self.assertEqual(job["error"], "result_changed")
        self.assertEqual(job["info_hash"], HASH_A)
        self.assertEqual(self.qbit.add_calls, 0)

    def test_qbit_terminal_torrent_state_becomes_failed(self) -> None:
        self.seed()
        self.qbit.torrents[HASH_A] = {
            "hash": HASH_A, "name": "Book", "category": "audiobooks",
            "save_path": "/downloads/audiobooks", "tags": "huey-1",
            "progress": 0.4, "state": "missingFiles",
        }
        job = self.grab()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "qbit_rejected")
        root = self.journal.acquisition("huey:1")
        self.assertIsNotNone(root["mutation_started_at"])
        self.assertEqual(
            self.journal.acquisition_for_hash(HASH_A)["correlation_id"],
            "huey:1",
        )

    def test_hash_status_uses_only_journaled_exact_hash(self) -> None:
        self.seed()
        self.grab()
        found = self.client.get("/api/status/" + HASH_A).get_json()
        missing = self.client.get("/api/status/" + HASH_B).get_json()
        self.assertTrue(found["found"])
        self.assertEqual(missing, {"found": False})

    def test_status_accepts_bookbot_imported_category_as_processing(self) -> None:
        self.seed()
        self.grab()
        self.qbit.torrents[HASH_A]["category"] = "audiobooks-imported"
        status = self.client.get("/api/status?correlation_id=huey:1").get_json()
        self.assertEqual(status["job"]["status"], "processing")
        self.assertEqual(status["job"]["category"], "audiobooks")
        self.assertEqual(status["job"]["save_path"], "/downloads/audiobooks")

    def test_status_missing_huey_tag_fails_closed(self) -> None:
        self.seed()
        self.grab()
        self.qbit.torrents[HASH_A]["tags"] = ""
        status = self.client.get("/api/status?correlation_id=huey:1").get_json()
        self.assertEqual(status["job"]["status"], "failed")
        self.assertEqual(status["job"]["error"], "qbit_rejected")

    def test_concurrent_same_correlation_adds_once(self) -> None:
        self.seed()
        jobs: list[dict[str, Any]] = []
        threads = [threading.Thread(target=lambda: jobs.append(self.app.extensions["abba_service"].grab(CANDIDATE_A, "huey:1"))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(len(jobs), 6)
        self.assertEqual(self.qbit.add_calls, 1)
        self.assertTrue(all(job["info_hash"] == HASH_A for job in jobs))


if __name__ == "__main__":
    unittest.main()
