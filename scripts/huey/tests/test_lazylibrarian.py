import asyncio
import logging
import sqlite3
import sys
import tempfile
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock, Mock, patch

import requests


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))
PROCESSING_ROOT = HUEY_ROOT.parent / "processing"
sys.path.insert(0, str(PROCESSING_ROOT))

from clients import (
    LazyLibrarianClient,
    ServiceError,
    ServiceRejected,
    SubmissionUncertain,
)
from database import LazyLibrarianHashCollision, RequestStore
from huey import (
    lazylibrarian_reconciliation_loop,
    reconcile_lazylibrarian_requests,
)
from notifications import response_notifications
from orchestrator import RequestProcessor
from results import result
from services import ServiceRegistry
from bookbot_lib.huey import HueyUpdater


BOOK_A = "OL893415W"
BOOK_B = "OL27448W"
HASH_A = "a" * 40
HASH_B = "b" * 64


class FakeResponse:
    def __init__(self, value=None, *, status=200, text="", json_error=False):
        self.status_code = status
        self.value = value
        self.text = text
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("not json")
        return self.value


class ScriptedSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected HTTP request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeQbittorrent:
    def __init__(
        self,
        *,
        category="ebooks",
        save_path="/downloads/ebooks",
        error=None,
        tag_error=None,
    ):
        self.category = category
        self.save_path = save_path
        self.error = error
        self.tag_error = tag_error
        self.calls = []
        self.tag_calls = []
        self.tags = set()

    def find_torrent(self, torrent_hash):
        self.calls.append(torrent_hash)
        if self.error is not None:
            raise self.error
        if self.category is None:
            return None
        return {
            "hash": torrent_hash,
            "category": self.category,
            "save_path": self.save_path,
        }

    def add_tags(self, torrent_hash, tags):
        self.tag_calls.append((torrent_hash, tags))
        if self.tag_error is not None:
            raise self.tag_error
        self.tags.add((torrent_hash, tags))


def metadata(
    book_id=BOOK_A,
    *,
    title="Dune",
    author="Frank Herbert",
    year=1965,
    source="OpenLibrary",
):
    return {
        "bookid": book_id,
        "bookname": title,
        "authorname": author,
        "bookpub": year,
        "source": source,
        "booklink": "https://metadata.invalid/not-persisted",
    }


def library_book(
    book_id=BOOK_A,
    *,
    title="Dune",
    author="Frank Herbert",
    year=1965,
):
    return {
        "BookID": book_id,
        "BookName": title,
        "AuthorName": author,
        "BookPub": year,
        "Status": "Wanted",
    }


def history(
    book_id=BOOK_A,
    *,
    download_id=HASH_A,
    source="QBITTORRENT",
    aux_info="eBook",
):
    return {
        "BookID": book_id,
        "AuxInfo": aux_info,
        "Source": source,
        "DownloadID": download_id,
        "Status": "Snatched",
    }


def successful_responses(candidate=None, *, download_id=HASH_A):
    candidate = candidate or metadata()
    return (
        FakeResponse([candidate]),
        FakeResponse(True),
        FakeResponse([library_book(
            candidate["bookid"],
            title=candidate["bookname"],
            author=candidate["authorname"],
            year=candidate.get("bookpub", 0),
        )]),
        FakeResponse("OK"),
        FakeResponse("OK"),
        FakeResponse([history(candidate["bookid"], download_id=download_id)]),
    )


def delivery(message_id="100", *, content="Dune by Frank Herbert"):
    return {
        "discord_user_id": "10",
        "discord_username": "reader",
        "channel_id": "20",
        "message_id": message_id,
        "media_type": "ebooks",
        "content": content,
    }


class LazyLibrarianClientTests(unittest.TestCase):
    def client(self, session, **kwargs):
        kwargs.setdefault("qbittorrent", FakeQbittorrent())
        return LazyLibrarianClient(
            "http://lazylibrarian:5299",
            "0123456789abcdef0123456789abcdef",
            session=session,
            **kwargs,
        )

    @staticmethod
    def shelfarr_identity(book_id=BOOK_A, *, year=None):
        return {
            "fingerprint": "f" * 64,
            "label": "Dune · by Frank Herbert · Ebook · Open Library",
            "work_id": f"openlibrary:{book_id}",
            "source_work_ids": (f"openlibrary:{book_id}",),
            "title": "Dune",
            "author": "Frank Herbert",
            "year": year,
            "content_kind": "book",
            "media_type": "ebooks",
            "book_type": "ebook",
        }

    def test_unique_match_uses_official_order_and_binds_exact_hash(self):
        session = ScriptedSession(*successful_responses())
        callback_events = []
        qbittorrent = FakeQbittorrent()
        client = self.client(session, qbittorrent=qbittorrent)

        def before_create(book_id):
            callback_events.append((book_id, len(session.calls)))

        response = client.submit(
            "ebooks", "Dune", "Frank Herbert", 42, before_create=before_create
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["service"], "lazylibrarian")
        self.assertEqual(response["external_id"], HASH_A)
        self.assertEqual(callback_events, [(BOOK_A, 1)])
        self.assertEqual(qbittorrent.tag_calls, [])
        self.assertEqual(
            [call[2]["data"]["cmd"] for call in session.calls],
            [
                "findBook",
                "addBook",
                "getAllBooks",
                "queueBook",
                "searchBook",
                "getHistory",
            ],
        )
        params = [call[2]["data"] for call in session.calls]
        self.assertEqual(
            params[0],
            {
                "apikey": "0123456789abcdef0123456789abcdef",
                "cmd": "findBook",
                "name": "Dune",
                "source": "OpenLibrary",
            },
        )
        self.assertEqual(params[1]["wait"], "1")
        self.assertEqual(params[1]["source"], "OpenLibrary")
        self.assertEqual(params[3]["type"], "eBook")
        self.assertEqual(params[4]["type"], "eBook")
        self.assertEqual(params[4]["wait"], "1")
        self.assertNotIn("source", params[3])
        self.assertNotIn("source", params[4])

    def test_authoritative_retry_rejects_replacement_book_id_with_same_bibliography(self):
        client = self.client(ScriptedSession())
        original = client._candidate_snapshot(client._metadata_candidate(metadata()))
        session = ScriptedSession(FakeResponse([metadata(BOOK_B)]))
        crossed = []

        response = self.client(session).submit_authoritative(
            "ebooks",
            42,
            resolved_identity=original,
            before_create=crossed.append,
        )

        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(response["backend_outcome"], "miss")
        self.assertEqual(crossed, [])
        self.assertEqual(
            [call[2]["data"]["cmd"] for call in session.calls], ["findBook"]
        )

    def test_cross_provider_yearless_retry_rejects_different_edition_alias(self):
        session = ScriptedSession(
            FakeResponse([metadata(BOOK_B, year=0)])
        )
        crossed = []

        response = self.client(session).submit_authoritative(
            "ebooks",
            42,
            resolved_identity=self.shelfarr_identity(BOOK_A, year=None),
            before_create=crossed.append,
        )

        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(response["backend_outcome"], "miss")
        self.assertEqual(crossed, [])
        self.assertEqual(
            [call[2]["data"]["cmd"] for call in session.calls], ["findBook"]
        )

    def test_cross_provider_yearless_retry_accepts_shared_canonical_alias(self):
        session = ScriptedSession(*successful_responses())
        crossed = []

        response = self.client(session).submit_authoritative(
            "ebooks",
            42,
            resolved_identity=self.shelfarr_identity(BOOK_A, year=None),
            before_create=crossed.append,
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(crossed, [BOOK_A])
        self.assertEqual(
            [call[2]["data"]["cmd"] for call in session.calls],
            [
                "findBook",
                "addBook",
                "getAllBooks",
                "queueBook",
                "searchBook",
                "getHistory",
            ],
        )

    def test_api_command_uses_clean_post_url_and_form_body_credentials(self):
        secret = "0123456789abcdef0123456789abcdef"
        session = ScriptedSession(FakeResponse([]))

        self.assertEqual(self.client(session).search("Dune"), [])

        method, url, request = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://lazylibrarian:5299/api")
        self.assertNotIn("?", url)
        self.assertNotIn(secret, url)
        self.assertNotIn("params", request)
        self.assertEqual(
            request["data"],
            {
                "apikey": secret,
                "cmd": "findBook",
                "name": "Dune",
                "source": "OpenLibrary",
            },
        )
        self.assertNotIn(secret, str(request["headers"]))

    def test_find_book_uses_title_only_and_author_is_checked_locally(self):
        session = ScriptedSession(
            FakeResponse(
                [
                    metadata(
                        title="A Psalm for the Wild-Built",
                        author="Different Author",
                    )
                ]
            )
        )
        response = self.client(session).submit(
            "ebooks", "A Psalm for the Wild-Built", "Becky Chambers", 42
        )
        self.assertEqual(response["status"], "needs_selection")
        self.assertEqual(
            session.calls[0][2]["data"]["name"],
            "A Psalm for the Wild-Built",
        )

    def test_raw_ok_is_not_grab_proof(self):
        values = list(successful_responses())
        values[-1] = FakeResponse([])
        session = ScriptedSession(*values)

        with self.assertRaises(SubmissionUncertain):
            self.client(session).submit(
                "ebooks", "Dune", "Frank Herbert", 42
            )

    def test_post_dispatch_rejections_remain_owned_and_sanitized(self):
        responses = list(successful_responses())
        responses[4] = FakeResponse("No search methods set, check config")
        session = ScriptedSession(*responses[:5])
        with self.assertRaises(SubmissionUncertain) as context:
            self.client(session).submit(
                "ebooks",
                "Dune",
                "Frank Herbert",
                42,
                before_create=lambda _book_id: None,
            )
        self.assertNotIn("config", str(context.exception).casefold())

        responses = list(successful_responses())
        responses[-1] = FakeResponse([history(source="SABNZBD")])
        with self.assertRaises(SubmissionUncertain):
            self.client(ScriptedSession(*responses)).submit(
                "ebooks",
                "Dune",
                "Frank Herbert",
                42,
                before_create=lambda _book_id: None,
            )

    def test_live_qbittorrent_handoff_must_match_hash_category_and_path(self):
        for qbittorrent in (
            FakeQbittorrent(category=None),
            FakeQbittorrent(category="other"),
            FakeQbittorrent(save_path="/downloads/other"),
            FakeQbittorrent(error=ServiceError("qBittorrent is unavailable.")),
        ):
            with self.subTest(
                category=qbittorrent.category,
                save_path=qbittorrent.save_path,
                error=type(qbittorrent.error).__name__,
            ):
                with self.assertRaises(SubmissionUncertain):
                    self.client(
                        ScriptedSession(*successful_responses()),
                        qbittorrent=qbittorrent,
                    ).submit(
                        "ebooks",
                        "Dune",
                        "Frank Herbert",
                        42,
                        before_create=lambda _book_id: None,
                    )
                self.assertEqual(qbittorrent.tag_calls, [])

        recovery = self.client(
            ScriptedSession(
                FakeResponse([library_book()]),
                FakeResponse([history()]),
            ),
            qbittorrent=FakeQbittorrent(category="other"),
        )
        with self.assertRaises(ServiceRejected):
            recovery.recover_submission(BOOK_A)

    def test_client_defers_tag_until_durable_hash_ownership(self):
        qbittorrent = FakeQbittorrent(
            tag_error=ServiceError("qBittorrent tagging is unavailable.")
        )
        crossed = []

        response = self.client(
            ScriptedSession(*successful_responses()),
            qbittorrent=qbittorrent,
        ).submit(
            "ebooks",
            "Dune",
            "Frank Herbert",
            42,
            before_create=crossed.append,
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(crossed, [BOOK_A])
        self.assertEqual(qbittorrent.calls, [HASH_A])
        self.assertEqual(qbittorrent.tag_calls, [])

    def test_fast_bookbot_import_binds_hash_but_remains_nonterminal(self):
        qbittorrent = FakeQbittorrent(category="ebooks-imported")
        response = self.client(
            ScriptedSession(*successful_responses()),
            qbittorrent=qbittorrent,
        ).submit("ebooks", "Dune", "Frank Herbert", 42)

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_id"], HASH_A)
        self.assertEqual(response["external_status"], "processing")
        self.assertNotEqual(response["status"], "completed")
        self.assertEqual(qbittorrent.calls, [HASH_A])

        recovery = self.client(
            ScriptedSession(
                FakeResponse([library_book()]),
                FakeResponse([history()]),
            ),
            qbittorrent=FakeQbittorrent(category="ebooks-imported"),
        ).recover_submission(BOOK_A)
        self.assertEqual(recovery["state"], "queued")
        self.assertEqual(recovery["external_id"], HASH_A)
        self.assertEqual(recovery["external_status"], "processing")

    def test_transport_loss_after_durable_callback_is_submission_uncertain(self):
        secret = "0123456789abcdef0123456789abcdef"
        session = ScriptedSession(
            FakeResponse([metadata()]),
            requests.ConnectionError(f"http://ll/api?apikey={secret}&cmd=addBook"),
        )
        crossed = []
        with self.assertRaises(SubmissionUncertain) as context:
            self.client(session).submit(
                "ebooks",
                "Dune",
                "Frank Herbert",
                42,
                before_create=crossed.append,
            )
        rendered = "".join(
            traceback.format_exception(
                type(context.exception), context.exception, context.exception.__traceback__
            )
        )
        self.assertEqual(crossed, [BOOK_A])
        self.assertNotIn(secret, str(context.exception))
        self.assertNotIn(secret, rendered)

    def test_transport_error_before_dispatch_never_leaks_api_key(self):
        secret = "0123456789abcdef0123456789abcdef"
        client = LazyLibrarianClient(
            "http://lazylibrarian:5299",
            secret,
            session=ScriptedSession(
                requests.ConnectionError(
                    f"POST http://lazylibrarian/api body apikey={secret}&cmd=findBook"
                )
            ),
        )
        with self.assertRaises(ServiceError) as context:
            client.search("Dune")
        self.assertNotIn(secret, str(context.exception))
        self.assertTrue(context.exception.__suppress_context__)

    def test_optional_success_envelope_is_supported_but_failure_is_not_echoed(self):
        session = ScriptedSession(
            FakeResponse(
                {
                    "Success": True,
                    "Data": [metadata()],
                    "Error": {"Code": 200, "Message": "OK"},
                }
            )
        )
        self.assertEqual(self.client(session).search("Dune")[0]["_book_id"], BOOK_A)

        session = ScriptedSession(
            FakeResponse(
                {
                    "Success": False,
                    "Data": "",
                    "Error": {
                        "Code": 401,
                        "Message": "apikey=do-not-copy",
                    },
                }
            )
        )
        with self.assertRaises(ServiceRejected) as context:
            self.client(session).search("Dune")
        self.assertNotIn("do-not-copy", str(context.exception))

    def test_oversized_response_is_rejected_without_echoing_it(self):
        oversized = "sensitive-value-" + (
            "x" * LazyLibrarianClient.MAX_RESPONSE_BYTES
        )
        with self.assertRaisesRegex(ServiceError, "invalid response") as context:
            self.client(ScriptedSession(FakeResponse(oversized))).search("Dune")
        self.assertNotIn("sensitive-value", str(context.exception))

    def test_ambiguity_returns_at_most_three_safe_stable_candidates(self):
        values = [
            metadata(BOOK_A, author="Frank Herbert"),
            metadata(BOOK_B, author="Brian Herbert"),
            metadata("OL3W", author="Jane Herbert"),
            metadata("OL4W", author="John Herbert"),
        ]
        session = ScriptedSession(FakeResponse(values))
        response = self.client(session).submit("ebooks", "Dune", None, 42)

        self.assertEqual(response["status"], "awaiting_selection")
        self.assertEqual(len(response["selection_proposal"]), 3)
        self.assertEqual(len(session.calls), 1)
        for option in response["selection_proposal"]:
            self.assertRegex(option["work_id"], r"^lazylibrarian:[0-9a-f]{64}$")
            self.assertNotIn("booklink", option)
            self.assertNotIn("OL893415W", str(option))

    def test_no_result_and_wrong_author_never_mutate(self):
        no_result = self.client(ScriptedSession(FakeResponse([]))).submit(
            "ebooks", "Dune", None, 42
        )
        self.assertEqual(no_result["status"], "needs_selection")

        for candidate_author in (
            "Completely Different Person",
            "Herbert",
            "Brian Herbert",
        ):
            with self.subTest(author=candidate_author):
                session = ScriptedSession(
                    FakeResponse([metadata(author=candidate_author)])
                )
                wrong_author = self.client(session).submit(
                    "ebooks", "Dune", "Frank Herbert", 42
                )
                self.assertEqual(wrong_author["status"], "needs_selection")
                self.assertEqual(len(session.calls), 1)

    def test_selected_candidate_is_freshly_revalidated_and_acquired_once(self):
        candidates = [
            metadata(BOOK_A, author="Frank Herbert"),
            metadata(BOOK_B, author="Brian Herbert"),
        ]
        session = ScriptedSession(FakeResponse(candidates))
        client = self.client(session)
        proposed = client.submit("ebooks", "Dune", None, 42)
        selected = proposed["selection_proposal"][1]
        selected_raw = next(
            candidate
            for candidate in candidates
            if client._candidate_snapshot(client._metadata_candidate(candidate))[
                "fingerprint"
            ]
            == selected["fingerprint"]
        )
        session.responses.extend(
            [FakeResponse(candidates), *successful_responses(selected_raw)[1:]]
        )
        crossed = []

        response = client.submit_selected(
            "ebooks",
            "Dune",
            None,
            42,
            selected_candidate=selected,
            before_create=crossed.append,
        )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(crossed, [selected_raw["bookid"]])
        self.assertEqual(
            [call[2]["data"]["cmd"] for call in session.calls].count("searchBook"),
            1,
        )

    def test_stale_or_tampered_selection_never_mutates(self):
        candidates = [metadata(BOOK_A), metadata(BOOK_B, author="Brian Herbert")]
        session = ScriptedSession(FakeResponse(candidates))
        client = self.client(session)
        proposal = client.submit("ebooks", "Dune", None, 42)["selection_proposal"]
        stale_option = proposal[1]
        remaining = next(
            candidate
            for candidate in candidates
            if client._candidate_snapshot(client._metadata_candidate(candidate))[
                "fingerprint"
            ]
            != stale_option["fingerprint"]
        )
        session.responses.append(FakeResponse([remaining]))
        stale = client.submit_selected(
            "ebooks", "Dune", None, 42, selected_candidate=stale_option
        )
        self.assertEqual(stale["status"], "needs_selection")
        self.assertEqual(len(session.calls), 2)

        tampered = dict(proposal[0])
        tampered["title"] = "Not Dune"
        client = self.client(ScriptedSession())
        response = client.submit_selected(
            "ebooks", "Dune", None, 42, selected_candidate=tampered
        )
        self.assertEqual(response["status"], "needs_selection")

    def test_strict_metadata_and_history_identities_fail_closed(self):
        malformed = (
            metadata(source="GoogleBooks"),
            {**metadata(), "bookid": "https://openlibrary.org/works/OL1W"},
            {**metadata(), "authorname": "apikey=secret"},
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(ServiceError):
                    self.client(ScriptedSession(FakeResponse([value]))).search("Dune")

        responses = list(successful_responses())
        responses[-1] = FakeResponse(
            [history(download_id=HASH_A), history(download_id=HASH_B)]
        )
        with self.assertRaises(SubmissionUncertain):
            self.client(ScriptedSession(*responses)).submit(
                "ebooks", "Dune", "Frank Herbert", 42
            )

    def test_history_ignores_empty_and_inactive_rows_but_rejects_conflicts(self):
        old = history(download_id=HASH_A)
        old["Status"] = "Processed"
        empty = history(download_id="", source="")
        empty["Status"] = "Failed"
        current = history(download_id=HASH_B)
        responses = list(successful_responses(download_id=HASH_B))
        responses[-1] = FakeResponse([old, empty, current])

        response = self.client(ScriptedSession(*responses)).submit(
            "ebooks", "Dune", "Frank Herbert", 42
        )
        self.assertEqual(response["external_id"], HASH_B)

        responses = list(successful_responses())
        responses[-1] = FakeResponse([old])
        with self.assertRaises(SubmissionUncertain):
            self.client(ScriptedSession(*responses)).submit(
                "ebooks", "Dune", "Frank Herbert", 42
            )

    def test_recovery_reads_exact_book_and_history_without_mutation(self):
        session = ScriptedSession(
            FakeResponse([library_book(), library_book(BOOK_B, title="Other")]),
            FakeResponse(
                [
                    history(BOOK_B, download_id=HASH_B),
                    history(BOOK_A, download_id=HASH_A),
                    history(BOOK_A, aux_info="AudioBook", download_id=HASH_B),
                ]
            ),
        )
        qbittorrent = FakeQbittorrent()
        recovered = self.client(
            session, qbittorrent=qbittorrent
        ).recover_submission(BOOK_A, request_id=42)

        self.assertEqual(recovered["state"], "queued")
        self.assertEqual(recovered["external_id"], HASH_A)
        self.assertEqual(
            [call[2]["data"]["cmd"] for call in session.calls],
            ["getAllBooks", "getHistory"],
        )
        self.assertEqual(qbittorrent.tag_calls, [])

    def test_exact_recovery_does_not_tag_before_durable_hash_binding(self):
        qbittorrent = FakeQbittorrent()
        client = self.client(
            ScriptedSession(
                FakeResponse([library_book()]),
                FakeResponse([history()]),
                FakeResponse([library_book()]),
                FakeResponse([history()]),
            ),
            qbittorrent=qbittorrent,
        )

        first = client.recover_submission(BOOK_A, request_id=42)
        second = client.recover_submission(BOOK_A, request_id=42)

        self.assertEqual(first["external_id"], HASH_A)
        self.assertEqual(second["external_id"], HASH_A)
        self.assertEqual(qbittorrent.tags, set())
        self.assertEqual(qbittorrent.tag_calls, [])

    def test_recovery_unknown_and_pending_do_not_repeat_search(self):
        unknown_session = ScriptedSession(FakeResponse([]))
        self.assertEqual(
            self.client(unknown_session).recover_submission(BOOK_A)["state"],
            "unknown",
        )
        pending_session = ScriptedSession(
            FakeResponse([library_book()]), FakeResponse([])
        )
        self.assertEqual(
            self.client(pending_session).recover_submission(BOOK_A)["state"],
            "pending",
        )
        for session in (unknown_session, pending_session):
            commands = [call[2]["data"]["cmd"] for call in session.calls]
            self.assertNotIn("addBook", commands)
            self.assertNotIn("queueBook", commands)
            self.assertNotIn("searchBook", commands)


class LazyLibrarianStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.directory.name) / "huey.db")
        self.store.initialize()

    def tearDown(self):
        self.directory.cleanup()

    def make_request(self, message_id="100"):
        request, created = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id=message_id,
            media_type="ebooks",
            raw_request="Dune by Frank Herbert",
            title="Dune",
            author="Frank Herbert",
            target_key=f"target:{message_id}",
        )
        self.assertTrue(created)
        return self.store.transition(
            request["id"],
            "processing",
            "dispatching",
            service="lazylibrarian",
        )

    @staticmethod
    def proposal(book_id, author):
        client = LazyLibrarianClient(
            "http://ll:5299", "key", session=ScriptedSession()
        )
        return client._candidate_snapshot(
            client._metadata_candidate(metadata(book_id, author=author))
        )

    def test_schema_migration_adds_durable_book_id_idempotently(self):
        legacy_path = Path(self.directory.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    discord_user_id TEXT NOT NULL,
                    discord_username TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    raw_request TEXT NOT NULL,
                    title TEXT,
                    author TEXT,
                    target_key TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    service TEXT,
                    external_id TEXT,
                    external_status TEXT,
                    external_title TEXT,
                    dispatch_started_at TEXT,
                    abba_candidate_id TEXT,
                    error TEXT,
                    notified_at TEXT
                );
                INSERT INTO requests (
                    discord_user_id, discord_username, channel_id, message_id,
                    media_type, raw_request, title, author, status, service,
                    external_id, dispatch_started_at, abba_candidate_id
                ) VALUES (
                    '10', 'reader', '20', '99', 'audiobooks',
                    'Dune by Frank Herbert', 'Dune', 'Frank Herbert',
                    'queued', 'abba', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    CURRENT_TIMESTAMP,
                    'abba:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                );
                """
            )
        legacy_store = RequestStore(legacy_path)
        legacy_store.initialize()
        with legacy_store.connect() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(requests)")
            }
            preserved = connection.execute(
                "SELECT * FROM requests WHERE message_id = '99'"
            ).fetchone()
        self.assertIn("lazylibrarian_book_id", columns)
        self.assertEqual(
            preserved["abba_candidate_id"],
            "abba:" + ("a" * 64),
        )
        legacy_store.initialize()
        with legacy_store.connect() as connection:
            count = sum(
                row["name"] == "lazylibrarian_book_id"
                for row in connection.execute("PRAGMA table_info(requests)")
            )
        self.assertEqual(count, 1)

    def test_initialize_quarantines_legacy_completed_and_queued_hash_collision(self):
        owner = self.make_request("legacy-hash-owner")
        self.store.transition(
            owner["id"],
            "queued",
            "Legacy LazyLibrarian handoff",
            service="lazylibrarian",
            external_id=HASH_A,
            external_title="Dune",
            external_status="queued",
        )
        self.store.transition(
            owner["id"],
            "complete",
            "Legacy BookBot final import",
            service="lazylibrarian",
            external_id=HASH_A,
            external_title="Dune",
            external_status="processing",
        )
        contender = self.make_request("legacy-hash-contender")

        # Recreate the former index, which protected only active rows and
        # therefore permitted a completed owner plus one queued competitor.
        with self.store.connect() as connection:
            connection.execute("DROP INDEX requests_active_ll_hash_uq")
            connection.execute(
                """
                CREATE UNIQUE INDEX requests_active_ll_hash_uq
                    ON requests(lower(external_id))
                    WHERE service = 'lazylibrarian'
                      AND external_id IS NOT NULL
                      AND status IN ('processing', 'queued')
                """
            )
            connection.execute(
                """
                UPDATE requests
                SET status = 'queued', external_id = ?,
                    external_status = 'queued', external_title = 'Dune Messiah'
                WHERE id = ?
                """,
                (HASH_A.upper(), contender["id"]),
            )
            connection.execute(
                """
                INSERT INTO notification_deliveries (
                    request_id, event_key, route, message
                ) VALUES (?, 'download_active', 'request-status', 'must not leak')
                """,
                (contender["id"],),
            )

        self.store.initialize()

        preserved = self.store.get_request(owner["id"])
        quarantined = self.store.get_request(contender["id"])
        self.assertEqual(preserved["status"], "complete")
        self.assertEqual(preserved["external_id"], HASH_A)
        self.assertEqual(quarantined["status"], "failed")
        self.assertEqual(quarantined["external_id"], HASH_A.upper())
        self.assertEqual(
            quarantined["external_status"],
            "lazylibrarian_hash_identity_conflict",
        )
        self.assertIsNotNone(quarantined["notified_at"])
        self.assertIsNone(quarantined["canonical_request_id"])
        self.assertEqual(
            [
                row
                for row in self.store.pending_notification_deliveries()
                if row["request_id"] == contender["id"]
            ],
            [],
        )
        event_types = [
            row["event_type"] for row in self.store.events_for(contender["id"])
        ]
        self.assertEqual(
            event_types.count("lazylibrarian_hash_collision_migrated"), 1
        )

        # The stricter index and explicit collision check both survive restart.
        replacement = self.make_request("legacy-hash-replacement")
        self.store.mark_request_dispatch_started(
            replacement["id"], "lazylibrarian", candidate_id=BOOK_B
        )
        with self.assertRaises(LazyLibrarianHashCollision):
            self.store.record_lazylibrarian_download(
                replacement["id"], BOOK_B, HASH_A, "Other", "legacy collision"
            )
        self.store.initialize()
        self.assertEqual(
            [
                row["event_type"]
                for row in self.store.events_for(contender["id"])
            ].count("lazylibrarian_hash_collision_migrated"),
            1,
        )

    def test_ll_candidate_authorization_claims_exactly_once(self):
        request = self.make_request()
        self.store.create_candidate_confirmation(
            request["id"],
            [self.proposal(BOOK_A, "Frank Herbert"), self.proposal(BOOK_B, "Brian Herbert")],
        )
        self.assertTrue(self.store.bind_candidate_prompt(request["id"], "900"))

        wrong_user = self.store.claim_candidate_selection(
            prompt_message_id="900",
            reply_message_id="901",
            discord_user_id="99",
            channel_id="20",
            ordinal=1,
        )
        wrong_channel = self.store.claim_candidate_selection(
            prompt_message_id="900",
            reply_message_id="902",
            discord_user_id="10",
            channel_id="99",
            ordinal=1,
        )
        claimed = self.store.claim_candidate_selection(
            prompt_message_id="900",
            reply_message_id="903",
            discord_user_id="10",
            channel_id="20",
            ordinal=2,
        )
        duplicate = self.store.claim_candidate_selection(
            prompt_message_id="900",
            reply_message_id="904",
            discord_user_id="10",
            channel_id="20",
            ordinal=1,
        )

        self.assertEqual(wrong_user["outcome"], "invalid")
        self.assertEqual(wrong_channel["outcome"], "invalid")
        self.assertEqual(claimed["outcome"], "claimed")
        self.assertEqual(duplicate["outcome"], "duplicate")
        self.assertEqual(claimed["option"]["candidate"]["book_type"], "ebook")

    def test_expired_ll_prompt_is_released_and_retryable(self):
        request = self.make_request()
        now = datetime.now(timezone.utc) - timedelta(minutes=20)
        self.store.create_candidate_confirmation(
            request["id"],
            [self.proposal(BOOK_A, "Frank Herbert"), self.proposal(BOOK_B, "Brian Herbert")],
            now=now,
            ttl_seconds=60,
        )
        self.store.bind_candidate_prompt(request["id"], "900")
        outcome = self.store.claim_candidate_selection(
            prompt_message_id="900",
            reply_message_id="901",
            discord_user_id="10",
            channel_id="20",
            ordinal=1,
        )
        self.assertEqual(outcome["outcome"], "expired")
        self.assertEqual(self.store.get_request(request["id"])["status"], "needs_selection")

    def test_restart_releases_pre_dispatch_claim_but_keeps_crossed_dispatch(self):
        request = self.make_request("100")
        self.store.create_candidate_confirmation(
            request["id"],
            [self.proposal(BOOK_A, "Frank Herbert"), self.proposal(BOOK_B, "Brian Herbert")],
        )
        self.store.bind_candidate_prompt(request["id"], "900")
        self.store.claim_candidate_selection(
            prompt_message_id="900",
            reply_message_id="901",
            discord_user_id="10",
            channel_id="20",
            ordinal=1,
        )
        self.store.initialize()
        self.assertEqual(self.store.get_request(request["id"])["status"], "needs_selection")

        crossed = self.make_request("101")
        self.store.create_candidate_confirmation(
            crossed["id"],
            [self.proposal(BOOK_A, "Frank Herbert"), self.proposal(BOOK_B, "Brian Herbert")],
        )
        self.store.bind_candidate_prompt(crossed["id"], "910")
        self.store.claim_candidate_selection(
            prompt_message_id="910",
            reply_message_id="911",
            discord_user_id="10",
            channel_id="20",
            ordinal=1,
        )
        self.assertTrue(self.store.mark_candidate_dispatch_started(crossed["id"]))
        self.assertTrue(
            self.store.mark_request_dispatch_started(
                crossed["id"], "lazylibrarian", candidate_id=BOOK_A
            )
        )
        self.store.initialize()
        saved = self.store.get_request(crossed["id"])
        self.assertEqual(saved["status"], "processing")
        self.assertEqual(saved["lazylibrarian_book_id"], BOOK_A)
        self.assertEqual(
            [row["id"] for row in self.store.interrupted_lazylibrarian_requests()],
            [crossed["id"]],
        )

    def test_uncertain_partition_and_hash_binding_are_exact(self):
        request = self.make_request()
        self.store.mark_request_dispatch_started(
            request["id"], "lazylibrarian", candidate_id=BOOK_A
        )
        self.store.transition(
            request["id"],
            "queued",
            "uncertain",
            event_type="lazylibrarian_submission_uncertain",
            service="lazylibrarian",
            external_status="submission_uncertain",
        )
        self.assertEqual(
            [row["id"] for row in self.store.uncertain_lazylibrarian_requests()],
            [request["id"]],
        )
        self.assertTrue(
            self.store.record_lazylibrarian_download(
                request["id"], BOOK_A, HASH_B.upper(), "Dune", "recovered"
            )
        )
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["external_id"], HASH_B)
        self.assertEqual(saved["lazylibrarian_book_id"], BOOK_A)
        self.assertFalse(
            self.store.record_lazylibrarian_download(
                request["id"], BOOK_A, HASH_B, "Dune", "same"
            )
        )
        with self.assertRaises(Exception):
            self.store.record_lazylibrarian_download(
                request["id"], BOOK_A, HASH_A, "Dune", "changed"
            )

    def test_active_download_hash_reservation_is_atomic_and_case_insensitive(self):
        first = self.make_request("hash-owner-a")
        second = self.make_request("hash-owner-b")
        self.assertTrue(
            self.store.mark_request_dispatch_started(
                first["id"], "lazylibrarian", candidate_id=BOOK_A
            )
        )
        self.assertTrue(
            self.store.mark_request_dispatch_started(
                second["id"], "lazylibrarian", candidate_id=BOOK_B
            )
        )
        barrier = Barrier(2)

        def bind_normal_submission():
            barrier.wait(timeout=5)
            try:
                self.store.transition(
                    first["id"],
                    "queued",
                    "normal submission",
                    service="lazylibrarian",
                    external_id=HASH_A.upper(),
                    external_title="Dune",
                    external_status="queued",
                )
            except LazyLibrarianHashCollision:
                return "collision"
            return "bound"

        def bind_recovery():
            barrier.wait(timeout=5)
            try:
                self.store.record_lazylibrarian_download(
                    second["id"], BOOK_B, HASH_A, "Dune", "recovered"
                )
            except LazyLibrarianHashCollision:
                return "collision"
            return "bound"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [
                future.result(timeout=10)
                for future in (
                    executor.submit(bind_normal_submission),
                    executor.submit(bind_recovery),
                )
            ]

        self.assertCountEqual(outcomes, ["bound", "collision"])
        saved = [
            self.store.get_request(first["id"]),
            self.store.get_request(second["id"]),
        ]
        self.assertEqual(
            [row["external_id"] for row in saved].count(HASH_A),
            1,
        )
        self.assertEqual(
            [row["external_id"] for row in saved].count(None),
            1,
        )

        owner = next(row for row in saved if row["external_id"] == HASH_A)
        unbound = next(row for row in saved if row["external_id"] is None)
        self.store.transition(
            owner["id"],
            "complete",
            "BookBot imported the first request",
            event_type="completed",
            service="lazylibrarian",
            external_id=HASH_A,
            external_title="Dune",
            external_status="processing",
        )
        with self.assertRaises(LazyLibrarianHashCollision):
            self.store.record_lazylibrarian_download(
                unbound["id"],
                str(unbound["lazylibrarian_book_id"]),
                HASH_A,
                "Dune",
                "retained imported payload recovered",
            )
        self.assertIsNone(self.store.get_request(unbound["id"])["external_id"])

    def test_qbit_observation_is_hash_guarded_and_cannot_overwrite_bookbot(self):
        request = self.make_request("state-race")
        self.assertTrue(
            self.store.mark_request_dispatch_started(
                request["id"], "lazylibrarian", candidate_id=BOOK_A
            )
        )
        self.assertTrue(
            self.store.record_lazylibrarian_download(
                request["id"], BOOK_A, HASH_A, "Dune", "bound"
            )
        )
        self.assertFalse(
            self.store.record_lazylibrarian_state(
                request["id"],
                HASH_B,
                "failed",
                "stale qBittorrent observation",
                terminal=True,
                error="stale",
            )
        )

        # Simulate BookBot winning the write race after its ledger-validated import.
        self.store.transition(
            request["id"],
            "complete",
            "BookBot imported the exact payload",
            event_type="completed",
            service="lazylibrarian",
            external_id=HASH_A,
            external_title="Dune",
            external_status="processing",
        )
        self.assertFalse(
            self.store.record_lazylibrarian_state(
                request["id"],
                HASH_A,
                "failed",
                "late qBittorrent failure",
                terminal=True,
                error="late failure",
            )
        )
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "complete")
        self.assertIsNone(saved["error"])

    def test_non_ll_external_identity_remains_opaque(self):
        request, created = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="opaque-id",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author=None,
            target_key="opaque-id",
        )
        self.assertTrue(created)
        self.store.transition(
            request["id"],
            "queued",
            "Shelfarr accepted the request",
            service="shelfarr",
            external_id="Request-AbC",
        )
        self.assertEqual(
            self.store.get_request(request["id"])["external_id"],
            "Request-AbC",
        )

    def test_active_book_id_reservation_blocks_different_request_text(self):
        first = self.make_request("book-a")
        second = self.make_request("book-b")

        self.assertTrue(
            self.store.mark_request_dispatch_started(
                first["id"], "lazylibrarian", candidate_id=BOOK_A
            )
        )
        self.assertFalse(
            self.store.mark_request_dispatch_started(
                second["id"], "lazylibrarian", candidate_id=BOOK_A
            )
        )
        self.assertIsNone(
            self.store.get_request(second["id"])["dispatch_started_at"]
        )


class LazyLibrarianOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RequestStore(Path(self.directory.name) / "huey.db")
        self.store.initialize()

    def tearDown(self):
        self.directory.cleanup()

    def registry(
        self,
        session=None,
        *,
        enabled=True,
        owner="lazylibrarian",
        backends=None,
        qbittorrent=None,
    ):
        environment = {
            "EBOOK_ACQUISITION_OWNER": owner,
            "LAZYLIBRARIAN_ENABLED": "true" if enabled else "false",
            "LAZYLIBRARIAN_API_KEY": "0123456789abcdef0123456789abcdef",
            "PROWLARR_API_KEY": "prowlarr-key",
            "SHELFARR_ENABLED": "true",
            "SHELFARR_API_TOKEN": "rollback-token",
        }
        if backends is not None:
            environment["EBOOK_ACQUISITION_BACKENDS"] = backends
        services = ServiceRegistry(environment)
        prowlarr = Mock()
        prowlarr.search.side_effect = lambda query, _categories: [
            {
                "title": f"{query} EPUB",
                "downloadProtocol": "torrent",
                "magnetUrl": f"magnet:?xt=urn:btih:{HASH_A}",
                "seeders": 20,
            }
        ]
        services._clients["prowlarr"] = prowlarr
        if session is not None:
            resolved_qbittorrent = qbittorrent or FakeQbittorrent()
            services._clients["qbittorrent"] = resolved_qbittorrent
            services._clients["lazylibrarian"] = LazyLibrarianClient(
                "http://ll:5299",
                "0123456789abcdef0123456789abcdef",
                session=session,
                qbittorrent=resolved_qbittorrent,
            )
        return services

    def bound_request(
        self,
        message_id="bound",
        *,
        book_id=BOOK_A,
        download_id=HASH_A,
        title="Dune",
    ):
        request, created = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id=message_id,
            media_type="ebooks",
            raw_request=title,
            title=title,
            author=None,
            target_key=f"target:{message_id}",
        )
        self.assertTrue(created)
        self.store.transition(
            request["id"], "processing", "dispatching", service="lazylibrarian"
        )
        self.assertTrue(
            self.store.mark_request_dispatch_started(
                request["id"], "lazylibrarian", candidate_id=book_id
            )
        )
        self.assertTrue(
            self.store.record_lazylibrarian_download(
                request["id"], book_id, download_id, title, "bound"
            )
        )
        return self.store.get_request(request["id"])

    def test_owner_routes_exclusively_and_disabled_ll_fails_closed(self):
        services = self.registry(ScriptedSession(*successful_responses()))
        shelfarr = Mock()
        direct = Mock()
        services._clients.update({"shelfarr": shelfarr, "direct": direct})
        response = services.book(
            {
                "id": 42,
                "media_type": "ebooks",
                "title": "Dune",
                "author": "Frank Herbert",
            }
        )
        self.assertEqual(response["service"], "lazylibrarian")
        shelfarr.submit.assert_not_called()
        direct.submit.assert_not_called()

        disabled = self.registry(enabled=False)
        disabled._clients.update({"shelfarr": Mock(), "direct": Mock()})
        with self.assertRaises(ServiceError):
            disabled.book(
                {
                    "id": 43,
                    "media_type": "ebooks",
                    "title": "Dune",
                    "author": None,
                }
            )
        disabled._clients["shelfarr"].submit.assert_not_called()
        disabled._clients["direct"].submit.assert_not_called()

    def test_default_owner_preserves_shelfarr_and_explicit_direct_bypasses_it(self):
        shelfarr_services = ServiceRegistry(
            {"SHELFARR_ENABLED": "true", "SHELFARR_API_TOKEN": "token"}
        )
        shelfarr = Mock(
            submit=Mock(return_value=result("queued", "ok", service="shelfarr"))
        )
        shelfarr_services._clients["shelfarr"] = shelfarr
        shelfarr_services.book(
            {"id": 1, "media_type": "ebooks", "title": "Dune", "author": None}
        )
        shelfarr.submit.assert_called_once()

        direct_services = ServiceRegistry(
            {"EBOOK_ACQUISITION_OWNER": "direct", "SHELFARR_ENABLED": "true"}
        )
        direct = Mock(
            submit=Mock(return_value=result("queued", "ok", service="qbittorrent"))
        )
        direct_services._clients.update({"direct": direct, "shelfarr": Mock()})
        direct_services.book(
            {"id": 2, "media_type": "ebooks", "title": "Dune", "author": None}
        )
        direct.submit.assert_called_once()
        direct_services._clients["shelfarr"].submit.assert_not_called()

        unavailable_shelfarr = ServiceRegistry(
            {
                "EBOOK_ACQUISITION_OWNER": "shelfarr",
                "SHELFARR_ENABLED": "false",
            }
        )
        unavailable_shelfarr._clients["direct"] = Mock()
        with self.assertRaises(ServiceError):
            unavailable_shelfarr.book(
                {
                    "id": 3,
                    "media_type": "ebooks",
                    "title": "Dune",
                    "author": None,
                }
            )
        unavailable_shelfarr._clients["direct"].submit.assert_not_called()

    def test_invalid_owner_and_availability_flag_fail_validation(self):
        with self.assertRaisesRegex(ValueError, "EBOOK_ACQUISITION_OWNER"):
            ServiceRegistry({"EBOOK_ACQUISITION_OWNER": "both"})
        with self.assertRaisesRegex(ValueError, "LAZYLIBRARIAN_ENABLED"):
            ServiceRegistry({"LAZYLIBRARIAN_ENABLED": "yes"})

    def test_processor_persists_dispatch_before_mutation_and_dedupes_replay(self):
        session = ScriptedSession(*successful_responses())
        processor = RequestProcessor(self.store, services=self.registry(session))
        response = processor.process(delivery())

        saved = self.store.get_request(response["request_id"])
        self.assertEqual(response["status"], "queued")
        self.assertEqual(saved["service"], "lazylibrarian")
        self.assertEqual(saved["lazylibrarian_book_id"], BOOK_A)
        self.assertEqual(saved["external_id"], HASH_A)
        command_count = len(session.calls)

        replay = processor.process(delivery())
        duplicate_target = processor.process(delivery("101"))
        self.assertTrue(replay["duplicate"])
        self.assertTrue(duplicate_target["duplicate"])
        self.assertEqual(len(session.calls), command_count)

    def test_different_text_resolving_same_book_dispatches_only_once(self):
        session = ScriptedSession(
            *successful_responses(),
            FakeResponse([metadata()]),
        )
        processor = RequestProcessor(self.store, services=self.registry(session))

        first = processor.process(delivery(content="Dune"))
        second = processor.process(
            delivery("101", content="Dune by Frank Herbert")
        )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "queued")
        self.assertTrue(second["duplicate"])
        self.assertIn(f"request #{first['request_id']}", second["message"])
        commands = [call[2]["data"]["cmd"] for call in session.calls]
        self.assertEqual(commands.count("findBook"), 2)
        self.assertEqual(commands.count("addBook"), 1)
        self.assertEqual(commands.count("searchBook"), 1)

    def test_different_books_cannot_own_one_active_download_hash(self):
        second_candidate = metadata(
            BOOK_B,
            title="Dune Messiah",
            author="Frank Herbert",
            year=1969,
        )
        session = ScriptedSession(
            *successful_responses(),
            *successful_responses(second_candidate, download_id=HASH_A),
        )
        qbittorrent = FakeQbittorrent()
        services = self.registry(session, qbittorrent=qbittorrent)
        processor = RequestProcessor(self.store, services=services)

        first = processor.process(delivery())
        second_delivery = delivery(
            "101", content="Dune Messiah by Frank Herbert"
        )
        second = processor.process(second_delivery)

        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["external_id"], HASH_A)
        self.assertEqual(second["status"], "queued")
        self.assertEqual(second["external_status"], "submission_uncertain")
        self.assertIsNone(second["external_id"])
        first_saved = self.store.get_request(first["request_id"])
        second_saved = self.store.get_request(second["request_id"])
        self.assertEqual(first_saved["external_id"], HASH_A)
        self.assertIsNone(second_saved["external_id"])
        self.assertEqual(second_saved["lazylibrarian_book_id"], BOOK_B)
        self.assertEqual(
            [row["id"] for row in self.store.uncertain_lazylibrarian_requests()],
            [second["request_id"]],
        )
        commands = [call[2]["data"]["cmd"] for call in session.calls]
        self.assertEqual(commands.count("searchBook"), 2)
        self.assertEqual(
            qbittorrent.tags,
            {(HASH_A, f"huey-{first['request_id']}")},
        )
        self.assertNotIn(
            (HASH_A, f"huey-{second['request_id']}"), qbittorrent.tags
        )
        self.assertTrue(
            HueyUpdater(self.store.path).complete(
                HASH_A,
                Path("/media/ebooks/Books/Dune"),
                f"huey-{first['request_id']}",
                source_category="ebooks",
            )
        )
        self.assertEqual(
            self.store.get_request(first["request_id"])["status"], "complete"
        )

        call_count = len(session.calls)
        replay = processor.process(second_delivery)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(len(session.calls), call_count)

        recovery = Mock()
        recovery.recover_submission.return_value = {
            "state": "queued",
            "book_id": BOOK_B,
            "external_id": HASH_A,
            "external_title": "Dune Messiah",
        }
        services._clients["lazylibrarian"] = recovery
        services._clients["qbittorrent"] = FakeQbittorrent(category=None)
        self.assertEqual(
            reconcile_lazylibrarian_requests(self.store, services),
            0,
        )
        recovery.recover_submission.assert_called_once_with(
            BOOK_B, request_id=second["request_id"]
        )
        self.assertEqual(len(session.calls), call_count)
        self.assertIsNone(
            self.store.get_request(second["request_id"])["external_id"]
        )

    def test_unavailable_owner_retains_attribution_without_fallback(self):
        services = self.registry(enabled=False)
        services._clients.update({"shelfarr": Mock(), "direct": Mock()})
        response = RequestProcessor(self.store, services=services).process(delivery())
        saved = self.store.get_request(response["request_id"])
        self.assertEqual(response["status"], "failed")
        self.assertEqual(saved["service"], "lazylibrarian")
        services._clients["shelfarr"].submit.assert_not_called()
        services._clients["direct"].submit.assert_not_called()

    def test_ll_candidate_reply_wrong_identity_duplicate_and_exact_once(self):
        choices = [metadata(BOOK_A), metadata(BOOK_B, author="Brian Herbert")]
        session = ScriptedSession(
            FakeResponse(choices),
            FakeResponse(choices),
            *successful_responses(choices[1])[1:],
        )
        processor = RequestProcessor(self.store, services=self.registry(session))
        response = processor.process(delivery(content="Dune"))
        self.assertEqual(response["status"], "awaiting_selection")
        self.store.bind_candidate_prompt(response["request_id"], "900")
        initial_calls = len(session.calls)

        wrong = processor.process_candidate_reply(
            {
                "prompt_message_id": "900",
                "message_id": "901",
                "discord_user_id": "99",
                "channel_id": "20",
                "ordinal": 1,
            }
        )
        self.assertEqual(wrong["selection_outcome"], "invalid")
        self.assertEqual(len(session.calls), initial_calls)

        selected = processor.process_candidate_reply(
            {
                "prompt_message_id": "900",
                "message_id": "902",
                "discord_user_id": "10",
                "channel_id": "20",
                "ordinal": 1,
            }
        )
        call_count = len(session.calls)
        duplicate = processor.process_candidate_reply(
            {
                "prompt_message_id": "900",
                "message_id": "902",
                "discord_user_id": "10",
                "channel_id": "20",
                "ordinal": 1,
            }
        )
        self.assertEqual(selected["status"], "queued")
        self.assertEqual(duplicate["selection_outcome"], "duplicate")
        self.assertEqual(len(session.calls), call_count)
        self.assertEqual(
            [call[2]["data"]["cmd"] for call in session.calls].count("searchBook"),
            1,
        )

    def test_submission_uncertain_is_durable_and_never_repeats_search(self):
        session = ScriptedSession(
            FakeResponse([metadata()]),
            requests.ConnectionError("lost after addBook dispatch"),
        )
        services = self.registry(session)
        processor = RequestProcessor(self.store, services=services)
        response = processor.process(delivery())
        saved = self.store.get_request(response["request_id"])

        self.assertEqual(response["external_status"], "submission_uncertain")
        self.assertEqual(saved["lazylibrarian_book_id"], BOOK_A)
        self.assertEqual(len(self.store.uncertain_lazylibrarian_requests()), 1)

        recovery_client = Mock()
        recovery_client.recover_submission.return_value = {
            "state": "pending",
            "book_id": BOOK_A,
            "external_id": None,
            "external_title": "Dune",
        }
        services._clients["lazylibrarian"] = recovery_client
        reconcile_lazylibrarian_requests(self.store, services)
        reconcile_lazylibrarian_requests(self.store, services)
        self.assertEqual(recovery_client.recover_submission.call_count, 2)
        self.assertEqual(
            len(
                [
                    row
                    for row in self.store.pending_notification_deliveries()
                    if row["event_key"] == "submission_uncertain"
                ]
            ),
            1,
        )

    def test_post_binding_tag_failure_never_invokes_fallback(self):
        qbittorrent = FakeQbittorrent(
            tag_error=ServiceError("sensitive qBittorrent transport detail")
        )
        services = self.registry(
            ScriptedSession(*successful_responses()),
            backends="lazylibrarian,shelfarr",
            qbittorrent=qbittorrent,
        )
        shelfarr = Mock()
        services._clients["shelfarr"] = shelfarr

        response = RequestProcessor(self.store, services=services).process(delivery())

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["external_status"], "queued")
        self.assertEqual(response["external_id"], HASH_A)
        self.assertNotIn("sensitive", response["message"].casefold())
        self.assertEqual(qbittorrent.tag_calls, [(HASH_A, "huey-1")])
        self.assertEqual(shelfarr.mock_calls, [])
        cascade = self.store.get_ebook_cascade(response["request_id"])
        self.assertEqual(cascade["state"], "queued")
        self.assertEqual(cascade["mutation_backend"], "lazylibrarian")
        self.assertEqual(cascade["final_backend"], "lazylibrarian")

    def test_restart_recovery_binds_hash_and_stages_acceptance_once(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="100",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author=None,
            target_key="dune",
        )
        self.store.transition(
            request["id"], "processing", "dispatching", service="lazylibrarian"
        )
        self.store.mark_request_dispatch_started(
            request["id"], "lazylibrarian", candidate_id=BOOK_A
        )
        self.store.initialize()
        client = Mock()
        client.recover_submission.return_value = {
            "state": "queued",
            "book_id": BOOK_A,
            "external_id": HASH_A,
            "external_title": "Dune",
        }
        services = Mock()
        services.lazylibrarian.return_value = client

        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 1)
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_id"], HASH_A)
        events = [event["event_type"] for event in self.store.events_for(request["id"])]
        self.assertEqual(events.count("lazylibrarian_history_recovered"), 1)

    def test_restart_recovery_late_binds_already_imported_hash_nonterminal(self):
        request, _ = self.store.create_request(
            discord_user_id="10",
            discord_username="reader",
            channel_id="20",
            message_id="fast-import",
            media_type="ebooks",
            raw_request="Dune",
            title="Dune",
            author=None,
            target_key="fast-import",
        )
        self.store.transition(
            request["id"], "processing", "dispatching", service="lazylibrarian"
        )
        self.store.mark_request_dispatch_started(
            request["id"], "lazylibrarian", candidate_id=BOOK_A
        )
        self.store.initialize()
        client = LazyLibrarianClient(
            "http://ll:5299",
            "0123456789abcdef0123456789abcdef",
            session=ScriptedSession(
                FakeResponse([library_book()]),
                FakeResponse([history()]),
            ),
            qbittorrent=FakeQbittorrent(category="ebooks-imported"),
        )
        services = Mock()
        services.lazylibrarian.return_value = client

        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 1)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_id"], HASH_A)
        self.assertEqual(saved["external_status"], "processing")
        self.assertNotIn(
            "completed",
            [event["event_type"] for event in self.store.events_for(request["id"])],
        )

        # BookBot alone may close this request. Its ledger-gated replay is
        # covered by processing/tests/test_service.py::
        # test_retained_import_reconciles_later_duplicate_huey_request.

    def test_qbittorrent_progress_is_nonterminal_and_notified_once(self):
        request = self.bound_request("progress")
        qbittorrent = Mock()
        qbittorrent.find_torrent.return_value = {
            "hash": HASH_A,
            "category": "ebooks",
            "save_path": "/downloads/ebooks",
            "state": "downloading",
            "progress": 0.5,
            "amount_left": 100,
        }
        services = Mock()
        services.qbittorrent.return_value = qbittorrent

        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 1)
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "downloading")

        # UP states prove payload completion even when qBittorrent omits the
        # optional progress counters. They still do not prove an import.
        qbittorrent.find_torrent.return_value = {
            "hash": HASH_A,
            "category": "ebooks",
            "save_path": "/downloads/ebooks",
            "state": "uploading",
        }
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 1)
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "processing")

        event_types = [
            event["event_type"] for event in self.store.events_for(request["id"])
        ]
        self.assertEqual(event_types.count("lazylibrarian_downloading"), 1)
        self.assertEqual(event_types.count("lazylibrarian_processing"), 1)
        self.assertNotIn("completed", event_types)
        event_keys = [
            row["event_key"]
            for row in self.store.pending_notification_deliveries()
            if row["request_id"] == request["id"]
        ]
        self.assertEqual(event_keys.count("download_active"), 1)
        self.assertEqual(event_keys.count("download_completed"), 1)

    def test_completed_progress_is_processing_until_bookbot_imports(self):
        request = self.bound_request("complete-progress")
        services = Mock()
        services.qbittorrent.return_value.find_torrent.return_value = {
            "hash": HASH_A,
            "category": "ebooks",
            "save_path": "/downloads/ebooks",
            "state": "moving",
            "progress": 1,
            "amount_left": 0,
        }

        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 1)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "processing")

    def test_only_reliable_qbittorrent_error_states_fail_once(self):
        first = self.bound_request("qbit-error", book_id=BOOK_A, download_id=HASH_A)
        second = self.bound_request(
            "qbit-missing",
            book_id=BOOK_B,
            download_id=HASH_B,
            title="Dune Messiah",
        )
        failure_states = {HASH_A: "error", HASH_B: "missingFiles"}
        qbittorrent = Mock()
        qbittorrent.find_torrent.side_effect = lambda torrent_hash: {
            "hash": torrent_hash,
            "category": "ebooks",
            "save_path": "/downloads/ebooks",
            "state": failure_states[torrent_hash],
        }
        services = Mock()
        services.qbittorrent.return_value = qbittorrent

        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 2)
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        for request in (first, second):
            with self.subTest(request_id=request["id"]):
                saved = self.store.get_request(request["id"])
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["external_status"], "failed")
                events = [
                    event["event_type"]
                    for event in self.store.events_for(request["id"])
                ]
                self.assertEqual(events.count("lazylibrarian_failed"), 1)
                deliveries = [
                    row
                    for row in self.store.pending_notification_deliveries()
                    if row["request_id"] == request["id"]
                    and row["event_key"] == "request_failed"
                ]
                self.assertEqual(len(deliveries), 1)

    def test_ambiguous_qbittorrent_states_never_infer_failure(self):
        request = self.bound_request("ambiguous-qbit")
        qbittorrent = Mock()
        services = Mock()
        services.qbittorrent.return_value = qbittorrent

        qbittorrent.find_torrent.return_value = None
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        qbittorrent.find_torrent.side_effect = ServiceError("qBittorrent unavailable")
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        qbittorrent.find_torrent.side_effect = None

        states = (
            "allocating",
            "checkingDL",
            "forcedMetaDL",
            "metaDL",
            "moving",
            "pausedDL",
            "queuedDL",
            "stalledDL",
            "stoppedDL",
            "futureState",
        )
        for state in states:
            with self.subTest(state=state):
                qbittorrent.find_torrent.return_value = {
                    "hash": HASH_A,
                    "category": "ebooks",
                    "save_path": "/downloads/ebooks",
                    "state": state,
                    "progress": 0.25,
                    "amount_left": 100,
                }
                reconcile_lazylibrarian_requests(self.store, services)
                saved = self.store.get_request(request["id"])
                self.assertEqual(saved["status"], "queued")
                self.assertNotEqual(saved["external_status"], "failed")

        events = [
            event["event_type"] for event in self.store.events_for(request["id"])
        ]
        self.assertNotIn("lazylibrarian_failed", events)

    def test_imported_category_overrides_raw_qbittorrent_failure(self):
        request = self.bound_request("already-imported")
        services = Mock()
        services.qbittorrent.return_value.find_torrent.return_value = {
            "hash": HASH_A,
            "category": "ebooks-imported",
            "save_path": "/downloads/ebooks",
            "state": "error",
            "progress": 0,
            "amount_left": 1,
        }

        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 1)
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "processing")
        self.assertIsNone(saved["error"])
        events = [
            event["event_type"] for event in self.store.events_for(request["id"])
        ]
        self.assertNotIn("lazylibrarian_failed", events)

    def test_qbittorrent_routing_mismatch_is_quarantined_not_failed(self):
        request = self.bound_request("routing-mismatch")
        services = Mock()
        qbittorrent = services.qbittorrent.return_value
        qbittorrent.find_torrent.return_value = {
            "hash": HASH_A,
            "category": "movies",
            "save_path": "/downloads/ebooks",
            "state": "uploading",
        }

        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        self.assertEqual(reconcile_lazylibrarian_requests(self.store, services), 0)
        saved = self.store.get_request(request["id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["external_status"], "queued")
        qbittorrent.add_tags.assert_not_called()
        attention = [
            row
            for row in self.store.pending_notification_deliveries()
            if row["request_id"] == request["id"]
            and row["event_key"] == "submission_uncertain"
        ]
        self.assertEqual(len(attention), 1)

    def test_uncertain_ebook_notification_is_backend_neutral(self):
        request = {
            "id": 42,
            "title": "Dune",
            "service": "lazylibrarian",
            "media_type": "ebooks",
        }
        response = result(
            "queued",
            "reconciling",
            service="lazylibrarian",
            external_status="submission_uncertain",
        )
        response.update({"request_id": 42, "duplicate": False})
        plans = response_notifications("ebooks", response, request)
        self.assertEqual(len(plans), 1)
        self.assertNotIn("LazyLibrarian", plans[0].message)
        self.assertNotIn("Shelfarr", plans[0].message)

    def test_audiobook_routing_is_unchanged_by_ebook_owner(self):
        services = self.registry()
        abba = Mock(
            submit=Mock(return_value=result("queued", "abba", service="abba"))
        )
        services.abba_enabled = True
        services._clients["abba"] = abba
        services.audiobook(
            {
                "id": 33,
                "media_type": "audiobooks",
                "title": "Dune",
                "author": "Frank Herbert",
            }
        )
        abba.submit.assert_called_once_with(
            "audiobooks", "Dune", "Frank Herbert", 33, before_create=None
        )


class LazyLibrarianLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconciliation_runs_in_an_independent_loop(self):
        client = Mock()
        client.is_closed.return_value = False
        observed = []

        async def stop_after_cycle(_seconds):
            raise asyncio.CancelledError

        with (
            patch(
                "huey.reconcile_lazylibrarian_requests",
                side_effect=lambda _store, _services: observed.append("ll"),
            ),
            patch(
                "huey.asyncio.to_thread",
                new=AsyncMock(side_effect=lambda function, *args: function(*args)),
            ),
            patch("huey.asyncio.sleep", side_effect=stop_after_cycle),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await lazylibrarian_reconciliation_loop(
                    client, object(), object(), 30
                )
        self.assertEqual(observed, ["ll"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
