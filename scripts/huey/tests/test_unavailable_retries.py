import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))
PROCESSING_ROOT = HUEY_ROOT.parent / "processing"
sys.path.insert(0, str(PROCESSING_ROOT))

from clients import ServiceError
from database import (
    EbookIdentityCollision,
    LazyLibrarianHashCollision,
    RequestStore,
    UNAVAILABLE_RETRY_LIMIT,
)
from huey import reconcile_shelfarr_requests
from notifications import terminal_notifications
from orchestrator import RequestProcessor
from results import result
from bookbot_lib.huey import HueyUpdater


LL_BOOK_ID = "OL893415W"
LL_HASH = "1" * 40
SHELFARR_WORK_ID = "openlibrary:OL893415W"


def canonical_identity():
    return {
        "fingerprint": "a" * 64,
        "label": "Dune by Frank Herbert (1965)",
        "work_id": "lazylibrarian:" + "a" * 64,
        "source_work_ids": ("lazylibrarian:" + "a" * 64,),
        "title": "Dune",
        "author": "Frank Herbert",
        "year": 1965,
        "content_kind": "book",
        "media_type": "ebooks",
        "book_type": "ebook",
    }


def request_delivery(message_id="100", content="Dune by Frank Herbert"):
    return {
        "discord_user_id": "10",
        "discord_username": "reader",
        "channel_id": "20",
        "message_id": str(message_id),
        "media_type": "ebooks",
        "content": content,
    }


class ScriptedBackends:
    ebook_acquisition_backends = ("lazylibrarian", "shelfarr")

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []

    def submit_ebook_backend(
        self,
        request,
        backend,
        *,
        resolved_identity=None,
        selected_candidate=None,
    ):
        self.calls.append(
            {
                "backend": backend,
                "resolved_identity": resolved_identity,
                "selected_candidate": selected_candidate,
            }
        )
        if not self.actions:
            raise AssertionError("Unexpected ebook retry dispatch")
        return self.actions.pop(0)(
            request, backend, resolved_identity, selected_candidate
        )


def release_miss(request, backend, authoritative, _selected):
    work = authoritative or canonical_identity()
    request["_on_resolved"](work, LL_BOOK_ID)
    return result(
        "failed",
        "No usable ebook release is currently available.",
        service=backend,
        external_status="not_found",
        backend_outcome="miss",
        resolved_identity=work,
    )


def exact_mapping_miss(_request, backend, authoritative, _selected):
    return result(
        "needs_selection",
        "No exact provider mapping is currently resolvable.",
        service=backend,
        backend_outcome="miss",
        resolved_identity=authoritative,
    )


def stale_metadata_miss(_request, backend, authoritative, _selected):
    return result(
        "needs_selection",
        "The stored metadata identity is no longer resolvable.",
        service=backend,
        backend_outcome="miss",
        resolved_identity=authoritative,
    )


def ll_handoff(request, backend, authoritative, _selected):
    work = authoritative or canonical_identity()
    request["_on_resolved"](work, LL_BOOK_ID)
    request["_before_dispatch"](LL_BOOK_ID)
    return result(
        "queued",
        "Exact handoff accepted.",
        service=backend,
        external_id=LL_HASH,
        external_title=work["title"],
        external_status="queued",
        resolved_identity=work,
    )


def shelfarr_handoff(request, backend, authoritative, _selected):
    if backend != "shelfarr":
        raise AssertionError("Shelfarr handoff ran against the wrong backend")
    resolved = {
        **canonical_identity(),
        "work_id": SHELFARR_WORK_ID,
        "source_work_ids": (SHELFARR_WORK_ID,),
    }
    request["_on_resolved"](resolved, SHELFARR_WORK_ID)
    request["_before_dispatch"]()
    return result(
        "queued",
        "Exact Shelfarr handoff accepted.",
        service=backend,
        external_id="73",
        external_title=resolved["title"],
        external_status="pending",
        resolved_identity=authoritative or resolved,
    )


class UnavailableRetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "huey.db"
        self.store = RequestStore(self.path)
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def exhaust(self, *, services=None):
        services = services or ScriptedBackends(
            [release_miss, exact_mapping_miss]
        )
        response = RequestProcessor(self.store, services=services).process(
            request_delivery()
        )
        self.assertEqual(response["status"], "failed")
        return response, services

    def all_deliveries(self, request_id):
        with self.store.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM notification_deliveries
                    WHERE request_id = ? ORDER BY id
                    """,
                    (int(request_id),),
                ).fetchall()
            ]

    def test_conclusive_unavailable_creates_one_durable_canonical_record(self):
        response, _ = self.exhaust()

        retry = self.store.get_unavailable_retry(response["request_id"])
        self.assertEqual(retry["state"], "queued")
        self.assertEqual(retry["retry_count"], 0)
        self.assertEqual(retry["final_import_state"], "pending")
        self.assertEqual(retry["metadata"]["work_id"], canonical_identity()["work_id"])
        self.assertEqual(retry["canonical_title"], "Dune")
        self.assertEqual(retry["canonical_creator"], "Frank Herbert")
        self.assertEqual(retry["canonical_year"], 1965)
        self.assertEqual(retry["channel_id"], "20")
        self.assertEqual(retry["message_id"], "100")
        first = datetime.fromisoformat(retry["first_unavailable_at"])
        due = datetime.fromisoformat(retry["next_retry_at"])
        self.assertEqual(due - first, timedelta(days=7))
        self.assertEqual(len(self.store.list_unavailable_retries()), 1)

        reopened = RequestStore(self.path)
        reopened.initialize()
        self.assertEqual(
            reopened.get_unavailable_retry(response["request_id"])["state"],
            "queued",
        )

    def test_provider_ids_remain_owned_while_retry_is_queued_and_retrying(self):
        shelfarr_alias = "hardcover:dune-canonical"

        def shelfarr_mapped_miss(request, backend, authoritative, _selected):
            mapping = {
                **canonical_identity(),
                "work_id": SHELFARR_WORK_ID,
                "source_work_ids": (SHELFARR_WORK_ID, shelfarr_alias),
            }
            request["_on_resolved"](mapping, SHELFARR_WORK_ID)
            return result(
                "needs_selection",
                "No exact provider release is currently available.",
                service=backend,
                backend_outcome="miss",
                resolved_identity=authoritative,
            )

        initial, _ = self.exhaust(
            services=ScriptedBackends([release_miss, shelfarr_mapped_miss])
        )
        request_id = initial["request_id"]
        expected_reservations = [
            ("lazylibrarian", LL_BOOK_ID),
            ("shelfarr", shelfarr_alias),
            ("shelfarr", SHELFARR_WORK_ID),
        ]

        def reservations():
            with self.store.connect() as connection:
                return [
                    (row["backend"], row["backend_identity"])
                    for row in connection.execute(
                        """
                        SELECT backend, backend_identity
                        FROM ebook_backend_reservations
                        WHERE request_id = ?
                        ORDER BY backend, backend_identity
                        """,
                        (request_id,),
                    ).fetchall()
                ]

        def assert_changed_work_cannot_take_book_id(message_id, marker):
            changed = {
                **canonical_identity(),
                "fingerprint": marker * 64,
                "label": f"Dune Revised {marker}",
                "work_id": "lazylibrarian:" + marker * 64,
                "source_work_ids": ("lazylibrarian:" + marker * 64,),
                "title": f"Dune Revised {marker}",
                "year": 1966,
            }
            contender, created = self.store.create_request(
                discord_user_id="11",
                discord_username="other-reader",
                channel_id="20",
                message_id=message_id,
                media_type="ebooks",
                raw_request=changed["title"],
                title=changed["title"],
                author=changed["author"],
                target_key=f"provider-drift:{marker}",
                ebook_backends=("lazylibrarian",),
            )
            self.assertTrue(created)
            self.store.begin_ebook_attempt(contender["id"], "lazylibrarian")
            with self.assertRaises(EbookIdentityCollision) as collision:
                self.store.set_ebook_identity(
                    contender["id"],
                    "lazylibrarian",
                    changed,
                    backend_identity=LL_BOOK_ID,
                )
            self.assertEqual(collision.exception.owner_request_id, request_id)
            self.assertIsNone(
                self.store.get_ebook_cascade(contender["id"])["identity"]
            )

        retry = self.store.get_unavailable_retry(request_id)
        self.assertEqual(retry["state"], "queued")
        self.assertEqual(reservations(), expected_reservations)
        cascade = self.store.get_ebook_cascade(request_id)
        self.assertEqual(cascade["attempts"][0]["backend_identity"], LL_BOOK_ID)
        self.assertEqual(
            cascade["attempts"][1]["backend_identity"], SHELFARR_WORK_ID
        )
        assert_changed_work_cannot_take_book_id("provider-drift-queued", "b")

        due = datetime.fromisoformat(retry["next_retry_at"]).replace(
            tzinfo=timezone.utc
        )
        self.assertEqual(
            [row["id"] for row in self.store.claim_due_unavailable_retries(now=due)],
            [request_id],
        )
        cascade = self.store.get_ebook_cascade(request_id)
        self.assertEqual(
            [attempt["backend_identity"] for attempt in cascade["attempts"]],
            [LL_BOOK_ID, SHELFARR_WORK_ID],
        )
        self.assertEqual(reservations(), expected_reservations)
        self.store.begin_ebook_attempt(request_id, "lazylibrarian")
        self.assertFalse(
            self.store.set_ebook_identity(
                request_id,
                "lazylibrarian",
                canonical_identity(),
                backend_identity=LL_BOOK_ID,
            )
        )
        self.assertEqual(reservations(), expected_reservations)
        assert_changed_work_cannot_take_book_id("provider-drift-retrying", "c")

    def test_transient_backend_failure_does_not_create_retry_record(self):
        def transient(*_args):
            raise ServiceError("backend temporarily unavailable")

        response, _ = self.exhaust(
            services=ScriptedBackends([release_miss, transient])
        )
        self.assertIsNone(self.store.get_unavailable_retry(response["request_id"]))

    def test_sensitive_operational_failure_is_not_persisted_or_logged(self):
        marker = "do-not-persist-secret"

        def sensitive_failure(*_args):
            raise ServiceError(
                f"https://user:{marker}@provider.invalid token={marker}"
            )

        with self.assertLogs("huey.orchestrator", level="WARNING") as captured:
            response, _ = self.exhaust(
                services=ScriptedBackends([release_miss, sensitive_failure])
            )
        self.assertNotIn(marker, "\n".join(captured.output))
        self.assertNotIn(
            marker,
            "\n".join(
                str(event.get("message") or "")
                for event in self.store.events_for(response["request_id"])
            ),
        )

    def test_metadata_only_misses_do_not_create_retry_record(self):
        response, _ = self.exhaust(
            services=ScriptedBackends([stale_metadata_miss, stale_metadata_miss])
        )
        self.assertIsNone(self.store.get_unavailable_retry(response["request_id"]))

    def test_duplicate_live_request_reuses_owner_and_nudges_due_time(self):
        first, _ = self.exhaust()
        duplicate = RequestProcessor(
            self.store, services=ScriptedBackends([])
        ).process(request_delivery("101"))

        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["request_id"], first["request_id"])
        self.assertEqual(duplicate["status"], "queued")
        self.assertEqual(len(self.store.list_unavailable_retries()), 1)
        self.assertLessEqual(
            datetime.fromisoformat(
                self.store.get_unavailable_retry(first["request_id"])["next_retry_at"]
            ),
            datetime.now(timezone.utc).replace(tzinfo=None),
        )

    def test_differently_worded_live_request_collides_before_mutation(self):
        first, _ = self.exhaust()
        services = ScriptedBackends([release_miss])
        second = RequestProcessor(self.store, services=services).process(
            request_delivery("102", "Dune novel by Frank Herbert")
        )

        self.assertTrue(second["duplicate"])
        self.assertIn(f"request #{first['request_id']}", second["message"])
        self.assertEqual(len(services.calls), 1)
        self.assertEqual(len(self.store.list_unavailable_retries()), 1)
        with self.store.connect() as connection:
            aliases = connection.execute(
                "SELECT request_id FROM delivery_aliases WHERE message_id = '102'"
            ).fetchall()
        self.assertEqual([row["request_id"] for row in aliases], [first["request_id"]])

    def test_retry_is_not_eligible_early_then_reuses_stored_identity_silently(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        retry = self.store.get_unavailable_retry(request_id)
        due = datetime.fromisoformat(retry["next_retry_at"]).replace(
            tzinfo=timezone.utc
        )
        services = ScriptedBackends([release_miss, exact_mapping_miss])
        processor = RequestProcessor(self.store, services=services)
        baseline = self.all_deliveries(request_id)

        self.assertEqual(
            processor.retry_due_unavailable_requests(now=due - timedelta(seconds=1)),
            0,
        )
        self.assertEqual(services.calls, [])
        self.assertEqual(processor.retry_due_unavailable_requests(now=due), 1)

        self.assertEqual(
            [call["backend"] for call in services.calls],
            ["lazylibrarian", "shelfarr"],
        )
        self.assertEqual(
            services.calls[0]["resolved_identity"]["fingerprint"],
            canonical_identity()["fingerprint"],
        )
        self.assertIsNone(services.calls[0]["selected_candidate"])
        saved = self.store.get_unavailable_retry(request_id)
        self.assertEqual(saved["state"], "queued")
        self.assertEqual(saved["retry_count"], 1)
        self.assertEqual(
            datetime.fromisoformat(saved["next_retry_at"]) - due.replace(tzinfo=None),
            timedelta(days=30),
        )
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_stale_metadata_retry_remains_queued_without_prompt_or_discord(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        services = ScriptedBackends([stale_metadata_miss, stale_metadata_miss])
        baseline = self.all_deliveries(request_id)

        self.assertEqual(
            RequestProcessor(
                self.store, services=services
            ).retry_due_unavailable_requests(now=due),
            1,
        )

        retry = self.store.get_unavailable_retry(request_id)
        self.assertEqual(retry["state"], "queued")
        next_due = datetime.fromisoformat(retry["next_retry_at"]).replace(
            tzinfo=timezone.utc
        )
        with self.store.connect() as connection:
            request = connection.execute(
                "SELECT status FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        self.assertEqual(request["status"], "failed")
        self.assertEqual(
            [row["id"] for row in self.store.claim_due_unavailable_retries(now=next_due)],
            [request_id],
        )
        self.assertIsNone(self.store.get_candidate_confirmation(request_id))
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_restart_resumes_claimed_search_silently(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        baseline = self.all_deliveries(request_id)
        self.assertEqual(
            [row["id"] for row in self.store.claim_due_unavailable_retries(now=due)],
            [request_id],
        )

        restarted = RequestStore(self.path)
        restarted.initialize()
        self.assertEqual(
            restarted.get_unavailable_retry(request_id)["state"], "retrying"
        )
        processor = RequestProcessor(
            restarted,
            services=ScriptedBackends([release_miss, exact_mapping_miss]),
        )
        self.assertEqual(processor.resume_ebook_cascades(), 1)
        self.assertEqual(
            restarted.get_unavailable_retry(request_id)["state"], "queued"
        )
        self.store = restarted
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_restart_recovery_closes_pre_mutation_exception_silently(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        baseline = self.all_deliveries(request_id)
        self.assertEqual(
            [row["id"] for row in self.store.claim_due_unavailable_retries(now=due)],
            [request_id],
        )

        def internal_failure(*_args):
            raise RuntimeError("test-only pre-mutation failure")

        restarted = RequestStore(self.path)
        restarted.initialize()
        processor = RequestProcessor(
            restarted, services=ScriptedBackends([internal_failure])
        )
        self.assertEqual(processor.resume_ebook_cascades(), 1)
        saved = restarted.get_unavailable_retry(request_id)
        self.assertEqual(saved["state"], "queued")
        self.assertEqual(saved["retry_count"], 1)
        self.assertEqual(restarted.get_request(request_id)["status"], "failed")
        self.store = restarted
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_restart_repairs_crash_after_stale_metadata_terminalization(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        baseline = self.all_deliveries(request_id)
        self.assertEqual(
            [row["id"] for row in self.store.claim_due_unavailable_retries(now=due)],
            [request_id],
        )
        self.store.begin_ebook_attempt(request_id, "lazylibrarian")
        self.store.terminalize_ebook_cascade(
            request_id,
            "lazylibrarian",
            "needs_selection",
            "Stored metadata is stale",
            notifications=(("must_stay_silent", "request-status", "hidden"),),
        )

        restarted = RequestStore(self.path)
        restarted.initialize()
        retry = restarted.get_unavailable_retry(request_id)
        self.assertEqual(retry["state"], "queued")
        self.assertEqual(restarted.get_request(request_id)["status"], "failed")
        next_due = datetime.fromisoformat(retry["next_retry_at"]).replace(
            tzinfo=timezone.utc
        )
        self.assertEqual(
            [
                row["id"]
                for row in restarted.claim_due_unavailable_retries(now=next_due)
            ],
            [request_id],
        )
        self.store = restarted
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_crash_window_handoff_promotes_owner_and_later_failure_blocks(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        baseline = self.all_deliveries(request_id)
        self.assertEqual(
            [row["id"] for row in self.store.claim_due_unavailable_retries(now=due)],
            [request_id],
        )
        self.store.begin_ebook_attempt(request_id, "lazylibrarian")
        self.store.set_ebook_identity(
            request_id,
            "lazylibrarian",
            canonical_identity(),
            backend_identity=LL_BOOK_ID,
        )
        self.assertTrue(
            self.store.lock_ebook_mutation(
                request_id, "lazylibrarian", backend_identity=LL_BOOK_ID
            )
        )

        self.assertTrue(
            self.store.record_ebook_recovered_handoff(
                request_id,
                "lazylibrarian",
                LL_HASH,
                "Dune",
                "queued",
                "Recovered exact crash-window handoff",
                backend_identity=LL_BOOK_ID,
                notifications=(("must_stay_silent", "download-queue", "hidden"),),
            )
        )
        self.assertEqual(
            self.store.get_unavailable_retry(request_id)["state"],
            "awaiting_import",
        )
        self.assertEqual(self.all_deliveries(request_id), baseline)

        self.store.record_lazylibrarian_state(
            request_id,
            LL_HASH,
            "failed",
            "Recovered payload could not be imported",
            terminal=True,
            error="Recovered payload could not be imported",
        )
        restarted = RequestStore(self.path)
        restarted.initialize()
        self.assertEqual(
            restarted.get_unavailable_retry(request_id)["state"], "blocked"
        )
        self.assertEqual(
            restarted.claim_due_unavailable_retries(
                now=due + timedelta(days=365)
            ),
            [],
        )
        self.store = restarted
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_handoff_and_download_completion_do_not_fulfil_or_notify(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        baseline = self.all_deliveries(request_id)

        RequestProcessor(
            self.store, services=ScriptedBackends([ll_handoff])
        ).retry_due_unavailable_requests(now=due)
        accepted = self.store.get_unavailable_retry(request_id)
        self.assertEqual(accepted["state"], "awaiting_import")
        self.assertEqual(accepted["final_import_state"], "pending")
        self.assertEqual(self.store.get_request(request_id)["status"], "queued")
        self.assertEqual(self.all_deliveries(request_id), baseline)

        self.store.record_lazylibrarian_state(
            request_id,
            LL_HASH,
            "processing",
            "qBittorrent payload is downloaded but not imported",
        )
        downloaded = self.store.get_unavailable_retry(request_id)
        self.assertEqual(downloaded["state"], "awaiting_import")
        self.assertEqual(downloaded["final_import_state"], "pending")
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_verified_final_import_fulfils_and_stages_one_completion_once(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        RequestProcessor(
            self.store, services=ScriptedBackends([ll_handoff])
        ).retry_due_unavailable_requests(now=due)

        updater = HueyUpdater(self.path)
        self.assertTrue(
            updater.complete(
                LL_HASH,
                Path("/media/ebooks/Books/Dune"),
                f"huey-{request_id}",
                source_category="ebooks",
            )
        )

        retry = self.store.get_unavailable_retry(request_id)
        self.assertEqual(retry["state"], "fulfilled")
        self.assertEqual(retry["final_import_state"], "verified")
        self.assertIsNotNone(retry["fulfilled_at"])
        with self.store.connect() as connection:
            reservations = connection.execute(
                """
                SELECT backend, backend_identity
                FROM ebook_backend_reservations
                WHERE request_id = ? ORDER BY backend, backend_identity
                """,
                (request_id,),
            ).fetchall()
        self.assertEqual(
            [(row["backend"], row["backend_identity"]) for row in reservations],
            [("lazylibrarian", LL_BOOK_ID)],
        )
        terminal = self.store.pending_notifications()
        self.assertEqual([row["id"] for row in terminal], [request_id])
        plans = terminal_notifications(terminal[0])
        completion = [plan for plan in plans if plan.event_key == "request_completed"]
        self.assertEqual(len(completion), 1)
        for plan in plans:
            self.store.enqueue_notification(
                request_id, plan.event_key, plan.route, plan.message
            )
            self.store.enqueue_notification(
                request_id, plan.event_key, plan.route, plan.message
            )
        deliveries = self.all_deliveries(request_id)
        self.assertEqual(
            sum(row["event_key"] == "request_completed" for row in deliveries),
            1,
        )

        self.assertFalse(
            updater.complete(
                LL_HASH,
                Path("/media/ebooks/Books/Dune"),
                f"huey-{request_id}",
                source_category="ebooks",
            )
        )
        self.assertEqual(
            sum(
                row["event_key"] == "request_completed"
                for row in self.all_deliveries(request_id)
            ),
            1,
        )

    def test_post_mutation_failure_blocks_without_retrying_or_notifying(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        baseline = self.all_deliveries(request_id)
        RequestProcessor(
            self.store, services=ScriptedBackends([ll_handoff])
        ).retry_due_unavailable_requests(now=due)

        self.store.record_lazylibrarian_state(
            request_id,
            LL_HASH,
            "failed",
            "BookBot cannot import this exact payload",
            terminal=True,
            error="BookBot cannot import this exact payload",
        )
        retry = self.store.get_unavailable_retry(request_id)
        self.assertEqual(retry["state"], "blocked")
        self.assertFalse(self.store.force_unavailable_retry(request_id, now=due))
        self.assertEqual(self.all_deliveries(request_id), baseline)

    def test_blocked_retry_keeps_its_lazylibrarian_hash_reserved(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        RequestProcessor(
            self.store, services=ScriptedBackends([ll_handoff])
        ).retry_due_unavailable_requests(now=due)
        self.store.record_lazylibrarian_state(
            request_id,
            LL_HASH,
            "failed",
            "BookBot cannot import this exact payload",
            terminal=True,
            error="BookBot cannot import this exact payload",
        )
        self.assertEqual(
            self.store.get_unavailable_retry(request_id)["state"], "blocked"
        )

        bindings = (
            "transition",
            "recovery",
            "cascade_result",
            "cascade_recovery",
        )
        for ordinal, bind in enumerate(bindings, start=1):
            candidate, _ = self.store.create_request(
                discord_user_id="11",
                discord_username="other-reader",
                channel_id="20",
                message_id=f"blocked-hash-{ordinal}",
                media_type="ebooks",
                raw_request=f"Other Book {ordinal}",
                title=f"Other Book {ordinal}",
                author="Other Author",
                target_key=f"blocked-hash-{ordinal}",
            )
            if bind.startswith("cascade_"):
                backend_book_id = f"OLBLOCKED{ordinal}W"
                identity_character = chr(ord("a") + ordinal)
                identity = {
                    **canonical_identity(),
                    "fingerprint": identity_character * 64,
                    "work_id": "lazylibrarian:" + identity_character * 64,
                    "source_work_ids": (
                        "lazylibrarian:" + identity_character * 64,
                    ),
                    "title": f"Other Book {ordinal}",
                    "year": 1970 + ordinal,
                }
                self.store.create_ebook_cascade(
                    candidate["id"], ("lazylibrarian",)
                )
                self.store.begin_ebook_attempt(
                    candidate["id"], "lazylibrarian"
                )
                self.store.set_ebook_identity(
                    candidate["id"],
                    "lazylibrarian",
                    identity,
                    backend_identity=backend_book_id,
                )
                self.assertTrue(
                    self.store.lock_ebook_mutation(
                        candidate["id"],
                        "lazylibrarian",
                        backend_identity=backend_book_id,
                    )
                )
                if bind == "cascade_result":
                    with self.assertRaises(LazyLibrarianHashCollision):
                        self.store.persist_ebook_result(
                            candidate["id"],
                            "lazylibrarian",
                            result(
                                "queued",
                                "colliding cascade handoff",
                                service="lazylibrarian",
                                external_id=LL_HASH,
                                external_title=identity["title"],
                                external_status="queued",
                            ),
                        )
                else:
                    with self.assertRaises(LazyLibrarianHashCollision):
                        self.store.record_ebook_recovered_handoff(
                            candidate["id"],
                            "lazylibrarian",
                            LL_HASH,
                            identity["title"],
                            "queued",
                            "colliding recovered cascade handoff",
                            backend_identity=backend_book_id,
                        )
                self.assertIsNone(
                    self.store.get_request(candidate["id"])["external_id"]
                )
                continue

            self.store.transition(
                candidate["id"],
                "processing",
                "dispatching",
                service="lazylibrarian",
            )
            if bind == "transition":
                with self.assertRaises(LazyLibrarianHashCollision):
                    self.store.transition(
                        candidate["id"],
                        "queued",
                        "colliding handoff",
                        service="lazylibrarian",
                        external_id=LL_HASH,
                        external_title="Other Book",
                        external_status="queued",
                    )
            else:
                self.store.mark_request_dispatch_started(
                    candidate["id"],
                    "lazylibrarian",
                    candidate_id="OL27448W",
                )
                with self.assertRaises(LazyLibrarianHashCollision):
                    self.store.record_lazylibrarian_download(
                        candidate["id"],
                        "OL27448W",
                        LL_HASH,
                        "Other Book",
                        "colliding recovery",
                    )
            self.assertIsNone(
                self.store.get_request(candidate["id"])["external_id"]
            )
        with self.store.connect() as connection:
            reservations = connection.execute(
                """
                SELECT backend, backend_identity
                FROM ebook_backend_reservations
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchall()
        self.assertEqual(
            [(row["backend"], row["backend_identity"]) for row in reservations],
            [("lazylibrarian", LL_BOOK_ID)],
        )

    def test_blocked_ll_owner_accepts_only_exact_late_final_import_proof(self):
        initial, services = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        retry_services = ScriptedBackends([ll_handoff])
        RequestProcessor(self.store, services=retry_services).retry_due_unavailable_requests(
            now=due
        )
        self.store.record_lazylibrarian_state(
            request_id,
            LL_HASH,
            "failed",
            "BookBot observed an import failure before the copy committed",
            terminal=True,
            error="BookBot observed an import failure before the copy committed",
        )
        baseline = self.all_deliveries(request_id)
        updater = HueyUpdater(self.path)

        self.assertFalse(
            updater.failed(LL_HASH, "repeat failure", f"huey-{request_id}")
        )
        self.assertFalse(
            updater.complete(
                LL_HASH,
                Path("/media/ebooks/Comics/Dune"),
                f"huey-{request_id}",
                source_category="manga-comics",
            )
        )
        self.assertFalse(
            updater.complete(
                LL_HASH,
                Path("/media/ebooks/Books/Dune"),
                f"huey-{request_id}",
            )
        )
        self.assertFalse(
            updater.complete(
                "2" * 40,
                Path("/media/ebooks/Books/Dune"),
                f"huey-{request_id}",
                source_category="ebooks",
            )
        )
        self.assertEqual(
            self.store.get_unavailable_retry(request_id)["state"], "blocked"
        )
        self.assertEqual(self.all_deliveries(request_id), baseline)
        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as connection:
            connection.execute(
                """
                UPDATE requests
                SET status = 'complete', external_id = ?
                WHERE id = ?
                """,
                ("2" * 40, request_id),
            )
        self.assertEqual(self.store.get_request(request_id)["status"], "failed")

        self.assertTrue(
            updater.complete(
                LL_HASH,
                Path("/media/ebooks/Books/Dune"),
                f"huey-{request_id}",
                source_category="ebooks",
            )
        )
        retry = self.store.get_unavailable_retry(request_id)
        cascade = self.store.get_ebook_cascade(request_id)
        self.assertEqual(retry["state"], "fulfilled")
        self.assertEqual(retry["final_import_state"], "verified")
        self.assertEqual(cascade["state"], "completed")
        self.assertEqual(cascade["final_backend"], "lazylibrarian")
        self.assertEqual(cascade["finalizer"], "bookbot")
        self.assertEqual(cascade["attempts"][0]["status"], "completed")
        self.assertEqual([call["backend"] for call in retry_services.calls], ["lazylibrarian"])
        self.assertEqual(services.actions, [])
        terminal = self.store.pending_notifications()
        self.assertEqual([row["id"] for row in terminal], [request_id])
        self.assertEqual(
            sum(
                plan.event_key == "request_completed"
                for plan in terminal_notifications(terminal[0])
            ),
            1,
        )

    def test_blocked_shelfarr_owner_fulfils_only_on_exact_remote_completion(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        due = datetime.fromisoformat(
            self.store.get_unavailable_retry(request_id)["next_retry_at"]
        ).replace(tzinfo=timezone.utc)
        retry_services = ScriptedBackends([release_miss, shelfarr_handoff])
        RequestProcessor(self.store, services=retry_services).retry_due_unavailable_requests(
            now=due
        )
        self.store.record_shelfarr_state(
            request_id,
            "failed",
            "Shelfarr reported an import failure before finalization",
            event_type="shelfarr_import_failed",
            terminal_status="failed",
            error="Shelfarr reported an import failure before finalization",
        )
        baseline = self.all_deliveries(request_id)
        remote = {
            "id": 73,
            "status": "processing",
            "attention_needed": False,
        }
        client = type(
            "ShelfarrProofClient",
            (),
            {"get_request": lambda _self, _request_id: dict(remote)},
        )()
        runtime_services = type(
            "ShelfarrProofServices",
            (),
            {"shelfarr": lambda _self: client},
        )()

        self.assertEqual(
            reconcile_shelfarr_requests(self.store, runtime_services), 0
        )
        remote.update({"id": 74, "status": "completed"})
        self.assertEqual(
            reconcile_shelfarr_requests(self.store, runtime_services), 0
        )
        self.assertEqual(
            self.store.get_unavailable_retry(request_id)["state"], "blocked"
        )
        self.assertEqual(self.all_deliveries(request_id), baseline)

        remote["id"] = 73
        self.assertEqual(
            reconcile_shelfarr_requests(self.store, runtime_services), 1
        )
        retry = self.store.get_unavailable_retry(request_id)
        cascade = self.store.get_ebook_cascade(request_id)
        self.assertEqual(retry["state"], "fulfilled")
        self.assertEqual(retry["final_import_state"], "verified")
        self.assertEqual(self.store.get_request(request_id)["status"], "completed")
        self.assertEqual(cascade["state"], "completed")
        self.assertEqual(cascade["final_backend"], "shelfarr")
        self.assertEqual(cascade["finalizer"], "shelfarr")
        self.assertEqual(cascade["attempts"][1]["status"], "completed")
        self.assertEqual(
            [call["backend"] for call in retry_services.calls],
            ["lazylibrarian", "shelfarr"],
        )
        terminal = self.store.pending_notifications()
        self.assertEqual([row["id"] for row in terminal], [request_id])
        self.assertEqual(
            sum(
                plan.event_key == "request_completed"
                for plan in terminal_notifications(terminal[0])
            ),
            1,
        )

    def test_blocked_shelfarr_proof_polling_rotates_past_first_hundred(self):
        remote_ids = []
        with self.store.connect() as connection:
            for ordinal in range(1, 102):
                identity_key = f"{ordinal:064x}"
                fingerprint = f"{ordinal + 1000:064x}"
                work_id = f"openlibrary:OL{ordinal}W"
                remote_id = str(1000 + ordinal)
                title = f"Proof Book {ordinal}"
                metadata = {
                    "fingerprint": fingerprint,
                    "label": f"{title} — Proof Author (2026)",
                    "work_id": work_id,
                    "source_work_ids": [work_id],
                    "title": title,
                    "author": "Proof Author",
                    "year": 2026,
                    "content_kind": "book",
                    "media_type": "ebooks",
                    "book_type": "ebook",
                }
                metadata_json = json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO requests(
                        discord_user_id, discord_username, channel_id,
                        message_id, media_type, raw_request, title, author,
                        target_key, status, service, external_id,
                        external_status, error
                    ) VALUES (
                        '10', 'reader', '20', ?, 'ebooks', ?, ?,
                        'Proof Author', ?, 'failed', 'shelfarr', ?, 'failed',
                        'Shelfarr finalization failed'
                    )
                    """,
                    (
                        f"proof-message-{ordinal}",
                        f"{title} by Proof Author",
                        title,
                        f"ebooks:{identity_key}",
                        remote_id,
                    ),
                )
                request_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO ebook_cascades(
                        request_id, policy_json, current_ordinal, state,
                        identity_key, identity_fingerprint, identity_json,
                        final_backend, finalizer
                    ) VALUES (
                        ?, '["lazylibrarian","shelfarr"]', 1, 'failed',
                        ?, ?, ?, 'shelfarr', 'shelfarr'
                    )
                    """,
                    (request_id, identity_key, fingerprint, metadata_json),
                )
                connection.executemany(
                    """
                    INSERT INTO ebook_backend_attempts(
                        request_id, ordinal, backend, status, started_at,
                        finished_at, backend_identity, external_id,
                        external_status
                    ) VALUES (?, ?, ?, ?, '2026-01-08 00:00:00',
                              '2026-01-08 00:01:00', ?, ?, ?)
                    """,
                    (
                        (
                            request_id,
                            0,
                            "lazylibrarian",
                            "miss",
                            None,
                            None,
                            "not_found",
                        ),
                        (
                            request_id,
                            1,
                            "shelfarr",
                            "failed",
                            work_id,
                            remote_id,
                            "failed",
                        ),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO ebook_backend_reservations(
                        backend, backend_identity, request_id
                    ) VALUES ('shelfarr', ?, ?)
                    """,
                    (work_id, request_id),
                )
                connection.execute(
                    """
                    INSERT INTO unavailable_retries(
                        request_id, media_type, identity_key, metadata_json,
                        canonical_title, canonical_creator, canonical_year,
                        discord_user_id, discord_username, channel_id,
                        message_id, first_unavailable_at, last_retry_at,
                        retry_count, state, final_import_state
                    ) VALUES (
                        ?, 'ebooks', ?, ?, ?, 'Proof Author', 2026,
                        '10', 'reader', '20', ?, '2026-01-01 00:00:00',
                        '2026-01-08 00:00:00', 1, 'blocked', 'pending'
                    )
                    """,
                    (
                        request_id,
                        identity_key,
                        metadata_json,
                        title,
                        f"proof-message-{ordinal}",
                    ),
                )
                remote_ids.append((request_id, remote_id))

        target_request_id, target_remote_id = remote_ids[-1]
        observed = []

        class ProofClient:
            def get_request(self, remote_id):
                persisted = str(remote_id)
                observed.append(persisted)
                return {
                    "id": int(persisted),
                    "status": (
                        "completed"
                        if persisted == target_remote_id
                        else "processing"
                    ),
                    "attention_needed": False,
                }

        client = ProofClient()
        services = type(
            "ProofServices",
            (),
            {"shelfarr": lambda _self: client},
        )()

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 0)
        self.assertNotIn(target_remote_id, observed)
        first_batch = list(observed)
        self.assertEqual(len(first_batch), 100)

        self.assertEqual(reconcile_shelfarr_requests(self.store, services), 1)
        self.assertIn(target_remote_id, observed[100:])
        self.assertEqual(
            self.store.get_unavailable_retry(target_request_id)["state"],
            "fulfilled",
        )
        with self.store.connect() as connection:
            first_cursor = connection.execute(
                "SELECT last_proof_check_at FROM unavailable_retries "
                "WHERE request_id = ?",
                (remote_ids[99][0],),
            ).fetchone()[0]
            target_cursor = connection.execute(
                "SELECT last_proof_check_at FROM unavailable_retries "
                "WHERE request_id = ?",
                (target_request_id,),
            ).fetchone()[0]
        self.assertIsNotNone(first_cursor)
        self.assertIsNotNone(target_cursor)
        self.assertGreater(target_cursor, first_cursor)

    def test_seventh_failed_retry_expires_and_cannot_run_twice(self):
        initial, _ = self.exhaust()
        request_id = initial["request_id"]
        retry = self.store.get_unavailable_retry(request_id)
        due = datetime.fromisoformat(retry["next_retry_at"]).replace(
            tzinfo=timezone.utc
        )
        services = ScriptedBackends(
            [action for _ in range(UNAVAILABLE_RETRY_LIMIT) for action in (
                release_miss,
                exact_mapping_miss,
            )]
        )
        processor = RequestProcessor(self.store, services=services)
        baseline = self.all_deliveries(request_id)

        for attempt in range(1, UNAVAILABLE_RETRY_LIMIT + 1):
            self.assertEqual(
                processor.retry_due_unavailable_requests(now=due), 1
            )
            saved = self.store.get_unavailable_retry(request_id)
            self.assertEqual(saved["retry_count"], attempt)
            if attempt < UNAVAILABLE_RETRY_LIMIT:
                self.assertEqual(saved["state"], "queued")
                due = datetime.fromisoformat(saved["next_retry_at"]).replace(
                    tzinfo=timezone.utc
                )

        self.assertEqual(saved["state"], "expired")
        self.assertIsNone(saved["next_retry_at"])
        self.assertIsNotNone(saved["expired_at"])
        with self.store.connect() as connection:
            reservations = connection.execute(
                """
                SELECT 1 FROM ebook_backend_reservations WHERE request_id = ?
                """,
                (request_id,),
            ).fetchall()
        self.assertEqual(reservations, [])
        self.assertEqual(
            processor.retry_due_unavailable_requests(now=due + timedelta(days=365)),
            0,
        )
        self.assertEqual(services.actions, [])
        self.assertEqual(self.all_deliveries(request_id), baseline)


if __name__ == "__main__":
    unittest.main()
