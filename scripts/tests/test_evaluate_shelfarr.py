import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "evaluate_shelfarr.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("evaluate_shelfarr", SCRIPT)
evaluate_shelfarr = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = evaluate_shelfarr
SPEC.loader.exec_module(evaluate_shelfarr)


class ShelfarrEvaluationTests(unittest.TestCase):
    def test_retry_attempt_uses_a_distinct_bounded_correlation(self):
        self.assertEqual(
            evaluate_shelfarr.evaluation_correlation_id(12, 0), 900_000_012
        )
        self.assertEqual(
            evaluate_shelfarr.evaluation_correlation_id(12, 1), 900_100_012
        )
        with self.assertRaises(evaluate_shelfarr.BootstrapError):
            evaluate_shelfarr.evaluation_correlation_id(12, -1)
        with self.assertRaises(evaluate_shelfarr.BootstrapError):
            evaluate_shelfarr.evaluation_correlation_id(100_012, 0)

    def test_historical_cohort_requires_failed_book_targets_and_dedupes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE requests "
                "(id INTEGER, media_type TEXT, title TEXT, author TEXT, status TEXT)"
            )
            connection.executemany(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "ebooks", "Dune", "Frank Herbert", "needs_selection"),
                    (2, "ebooks", "Dune", "Frank Herbert", "failed"),
                    (3, "audiobooks", "Dune", "Frank Herbert", "failed"),
                ],
            )
            connection.commit()
            connection.close()

            rows = evaluate_shelfarr.historical_requests(database, [1, 2, 3])
            self.assertEqual([row["id"] for row in rows], [1, 3])

    def test_final_path_mapping_stays_within_expected_library(self):
        root = Path("/mnt/media")
        self.assertEqual(
            evaluate_shelfarr.host_final_path(
                "/ebooks/Frank Herbert/Dune/book.epub", root
            ),
            Path("/mnt/media/ebooks/Books/Frank Herbert/Dune/book.epub"),
        )
        self.assertEqual(
            evaluate_shelfarr.host_final_path(
                "/audiobooks/Frank Herbert/Dune/book.m4b", root
            ),
            Path("/mnt/media/audiobooks/Frank Herbert/Dune/book.m4b"),
        )
        self.assertIsNone(
            evaluate_shelfarr.host_final_path("/tmp/untrusted", root)
        )
        self.assertIsNone(
            evaluate_shelfarr.host_final_path(
                "/ebooks/../audiobooks/escaped.m4b", root
            )
        )
        self.assertIsNone(evaluate_shelfarr.host_final_path("/ebooks", root))

    def test_final_artifact_requires_nonempty_expected_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty.epub"
            empty.touch()
            wrong = root / "notes.txt"
            wrong.write_text("not media", encoding="utf-8")
            valid = root / "book.epub"
            valid.write_bytes(b"book")

            self.assertFalse(
                evaluate_shelfarr.final_artifact_available(empty, "ebook")
            )
            self.assertFalse(
                evaluate_shelfarr.final_artifact_available(wrong, "ebook")
            )
            self.assertTrue(
                evaluate_shelfarr.final_artifact_available(root, "ebook")
            )

    def test_existing_title_requires_nonempty_expected_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            title = root / "Dune"
            title.mkdir()
            self.assertFalse(
                evaluate_shelfarr.library_has_title(root, "Dune", None, "ebook")
            )
            (title / "notes.txt").write_text("not media", encoding="utf-8")
            self.assertFalse(
                evaluate_shelfarr.library_has_title(root, "Dune", None, "ebook")
            )
            (title / "book.epub").write_bytes(b"book")
            self.assertTrue(
                evaluate_shelfarr.library_has_title(root, "Dune", None, "ebook")
            )

    def test_existing_title_with_author_rejects_different_author(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            title = root / "Different Author" / "Work in Progress"
            title.mkdir(parents=True)
            (title / "book.epub").write_bytes(b"book")
            self.assertFalse(
                evaluate_shelfarr.library_has_title(
                    root, "Work in Progress", "Michael Eisner", "ebook"
                )
            )

    def test_source_classification_uses_actual_download_client(self):
        self.assertEqual(
            evaluate_shelfarr.acquisition_source(
                {"client_type": "sabnzbd", "source": "prowlarr"}, []
            ),
            "usenet",
        )
        self.assertEqual(
            evaluate_shelfarr.acquisition_source(
                {"client_type": "qbittorrent", "source": "prowlarr"}, []
            ),
            "torrent",
        )
        self.assertEqual(
            evaluate_shelfarr.acquisition_source(
                {"download_type": "direct", "source": "gutenberg"}, []
            ),
            "direct",
        )

    def test_newznab_and_prowlarr_candidates_have_protocol_counts(self):
        record = evaluate_shelfarr.EvaluationRecord(
            12, "73", "Dune", "Frank Herbert", "ebook", "needs_selection"
        )
        results = [
            {
                "id": 1,
                "source": "newznab",
                "indexer": "Books NZB",
                "status": "selected",
            },
            {"id": 2, "source": "gutenberg", "status": "pending"},
            {
                "id": 3,
                "source": "prowlarr",
                "indexer": "The Pirate Bay",
                "status": "pending",
            },
            {
                "id": 4,
                "source": "prowlarr",
                "indexer": "Books NZB",
                "status": "pending",
            },
            {
                "id": 5,
                "source": "prowlarr",
                "indexer": "Unmapped Indexer",
                "status": "pending",
            },
        ]

        evaluate_shelfarr.observe_acquisition_candidates(
            record,
            results,
            {"the pirate bay": "torrent", "books nzb": "usenet"},
        )

        self.assertEqual(
            record.acquisition_candidate_counts,
            {"direct": 1, "usenet": 2, "torrent": 1, "unknown": 1},
        )
        self.assertEqual(record.selected_source, "usenet")

    def test_prowlarr_protocol_map_uses_only_read_only_indexer_api(self):
        class ReadOnlyProwlarr:
            calls = []

            def __init__(self, base_url, api_key, *, timeout):
                self.__class__.calls.append(
                    ("init", base_url, bool(api_key), timeout)
                )

            def _request(self, method, endpoint):
                self.__class__.calls.append((method, endpoint))
                return [
                    {"name": "Books NZB", "protocol": "usenet"},
                    {"name": "The Pirate Bay", "protocol": "torrent"},
                    {"name": "Untrusted", "protocol": "other"},
                ]

        with patch.object(
            evaluate_shelfarr, "ProwlarrClient", ReadOnlyProwlarr
        ):
            protocols = evaluate_shelfarr.read_prowlarr_indexer_protocols(
                {
                    "PROWLARR_API_KEY": "secret",
                    "WYSEARR_BIND_ADDRESS": "192.0.2.1",
                    "PROWLARR_PORT": "9696",
                }
            )

        self.assertEqual(
            protocols,
            {"books nzb": "usenet", "the pirate bay": "torrent"},
        )
        self.assertEqual(ReadOnlyProwlarr.calls[-1], ("GET", "/api/v1/indexer"))

    def test_metadata_visibility_distinguishes_ambiguity_and_no_results(self):
        class MetadataClient:
            minimum_confidence = 0.80
            runner_up_gap = 0.05

            def __init__(self, candidates):
                self.candidates = candidates

            def search(self, _title, _author):
                return self.candidates

        ambiguous = [
            {
                "work_id": "openlibrary:ONE",
                "title": "Work in Progress",
                "author": "Michael Eisner",
                "content_kind": "book",
                "available_book_types": ["ebook"],
            },
            {
                "work_id": "openlibrary:TWO",
                "title": "Work in Progress",
                "author": "Michael D. Eisner",
                "content_kind": "book",
                "available_book_types": ["ebook"],
            },
        ]
        ambiguous_record = evaluate_shelfarr.EvaluationRecord(
            10,
            None,
            "Work in Progress",
            "Michael Eisner",
            "ebook",
            "needs_selection",
        )
        evaluate_shelfarr.observe_metadata_resolution(
            MetadataClient(ambiguous), ambiguous_record, "ebooks"
        )
        self.assertEqual(ambiguous_record.metadata_resolution, "ambiguous")
        self.assertEqual(ambiguous_record.metadata_candidate_count, 2)

        missing_record = evaluate_shelfarr.EvaluationRecord(
            19,
            None,
            "Down in the Dirt",
            "Trevor Burbage",
            "ebook",
            "needs_selection",
        )
        evaluate_shelfarr.observe_metadata_resolution(
            MetadataClient([]), missing_record, "ebooks"
        )
        self.assertEqual(missing_record.metadata_resolution, "no_results")
        self.assertEqual(missing_record.metadata_candidate_count, 0)

    def test_terminal_visibility_separates_acquisition_from_import(self):
        successful = evaluate_shelfarr.EvaluationRecord(
            1,
            "71",
            "Book",
            None,
            "ebook",
            "failed",
            download_result="success",
            shelfarr_status="completed",
            final_library_available=True,
        )
        missing_import = evaluate_shelfarr.EvaluationRecord(
            2,
            "72",
            "Book Two",
            None,
            "ebook",
            "failed",
            download_result="import_path_missing",
            shelfarr_status="completed",
        )
        unselected = evaluate_shelfarr.EvaluationRecord(
            3,
            "73",
            "Book Three",
            None,
            "ebook",
            "failed",
            download_result="failure",
            shelfarr_status="failed",
            acquisition_candidate_counts={
                "direct": 0,
                "usenet": 1,
                "torrent": 0,
                "unknown": 0,
            },
        )
        selected_failure = evaluate_shelfarr.EvaluationRecord(
            4,
            "74",
            "Book Four",
            None,
            "ebook",
            "failed",
            download_result="failure",
            shelfarr_status="failed",
            selected_source="torrent",
        )
        import_failure = evaluate_shelfarr.EvaluationRecord(
            5,
            "75",
            "Book Five",
            None,
            "ebook",
            "failed",
            download_result="failure",
            shelfarr_status="failed",
        )
        evaluate_shelfarr.apply_artifact_observation(
            import_failure,
            {
                "download_status": 3,
                "download_type": "torrent",
                "client_type": "qbittorrent",
                "release_title": "Book Five EPUB",
            },
            [],
            Path("/mnt/media"),
        )

        for record in (
            successful,
            missing_import,
            unselected,
            selected_failure,
            import_failure,
        ):
            evaluate_shelfarr.update_measurement_outcomes(record)

        self.assertEqual(
            (successful.acquisition_result, successful.import_result),
            ("success", "success"),
        )
        self.assertEqual(
            (missing_import.acquisition_result, missing_import.import_result),
            ("success", "artifact_missing"),
        )
        self.assertEqual(
            (unselected.acquisition_result, unselected.import_result),
            ("candidate_unselected", "not_started"),
        )
        self.assertEqual(
            (selected_failure.acquisition_result, selected_failure.import_result),
            ("failure", "not_started"),
        )
        self.assertEqual(
            (import_failure.acquisition_result, import_failure.import_result),
            ("success", "failure"),
        )

    def test_legacy_report_loads_with_visibility_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "legacy.json"
            report.write_text(
                __import__("json").dumps(
                    {
                        "records": [
                            {
                                "huey_request_id": 12,
                                "shelfarr_request_id": "7",
                                "title": "The Boxcar Children",
                                "author": "Gertrude Chandler Warner",
                                "format": "ebook",
                                "previous_status": "needs_selection",
                                "download_result": "failure",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            record = evaluate_shelfarr.load_evaluation_records(report)[0]

            self.assertEqual(record.metadata_resolution, "not_observed")
            self.assertIsNone(record.metadata_candidate_count)
            self.assertIsNone(record.acquisition_candidate_counts)
            self.assertIsNone(record.selected_source)
            self.assertEqual(record.acquisition_result, "not_observed")
            self.assertEqual(record.import_result, "not_observed")

            evaluate_shelfarr.write_results(report, [record])
            rewritten = evaluate_shelfarr.load_evaluation_records(report)[0]
            self.assertEqual(rewritten.acquisition_result, "not_observed")
            self.assertEqual(rewritten.import_result, "not_observed")

    def test_cancellation_must_be_confirmed(self):
        record = evaluate_shelfarr.EvaluationRecord(
            1, "73", "Dune", "Frank Herbert", "ebook", "failed"
        )

        class FailedClient:
            def cancel_request(self, request_id):
                raise evaluate_shelfarr.ServiceError("unavailable")

        self.assertFalse(
            evaluate_shelfarr.cancel_evaluation_request(
                FailedClient(), record, result="not_found", note="No result."
            )
        )
        self.assertEqual(record.download_result, "cleanup_failed")

        class UnconfirmedClient:
            def cancel_request(self, request_id):
                return {"status": "not_found"}

        self.assertFalse(
            evaluate_shelfarr.cancel_evaluation_request(
                UnconfirmedClient(), record, result="not_found", note="No result."
            )
        )
        self.assertEqual(record.download_result, "cleanup_failed")

        class ConfirmedClient:
            def cancel_request(self, request_id):
                return {"status": "failed"}

        self.assertTrue(
            evaluate_shelfarr.cancel_evaluation_request(
                ConfirmedClient(), record, result="not_found", note="No result."
            )
        )
        self.assertEqual(record.download_result, "not_found")
        self.assertEqual(record.shelfarr_status, "failed")

    def test_every_remote_nonterminal_state_keeps_monitoring_active(self):
        self.assertEqual(
            evaluate_shelfarr.ACTIVE_RESULTS,
            {
                "queued",
                "pending",
                "searching",
                "not_found_retrying",
                "downloading",
                "processing",
            },
        )

    def test_deadline_cancels_every_correlated_nonterminal_request(self):
        records = [
            evaluate_shelfarr.EvaluationRecord(
                number,
                str(70 + number),
                f"Book {number}",
                None,
                "ebook",
                "failed",
                download_result=status,
            )
            for number, status in enumerate(
                sorted(evaluate_shelfarr.ACTIVE_RESULTS), start=1
            )
        ]
        completed = evaluate_shelfarr.EvaluationRecord(
            99,
            "199",
            "Completed Book",
            None,
            "ebook",
            "failed",
            download_result="success",
        )
        records.append(completed)

        class ConfirmedClient:
            def __init__(self):
                self.cancelled = []

            def cancel_request(self, request_id):
                self.cancelled.append(request_id)
                return {"status": "failed"}

        client = ConfirmedClient()
        evaluate_shelfarr.cancel_active_evaluation_requests(client, records)

        self.assertEqual(len(client.cancelled), len(evaluate_shelfarr.ACTIVE_RESULTS))
        self.assertEqual(completed.download_result, "success")
        self.assertTrue(
            all(
                record.download_result in {"not_found", "timed_out"}
                for record in records[:-1]
            )
        )

    def test_monitor_exception_cancels_submitted_remote_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state" / "huey").mkdir(parents=True)
            (root / "config" / "shelfarr").mkdir(parents=True)
            (root / ".env").write_text(
                "SHELFARR_ENABLED=true\nSHELFARR_API_TOKEN=shf_test\n",
                encoding="utf-8",
            )
            huey_db = root / "state" / "huey" / "huey.db"
            connection = sqlite3.connect(huey_db)
            connection.execute(
                "CREATE TABLE requests "
                "(id INTEGER, media_type TEXT, title TEXT, author TEXT, status TEXT)"
            )
            connection.execute(
                "INSERT INTO requests VALUES (12, 'ebooks', 'Dune', "
                "'Frank Herbert', 'needs_selection')"
            )
            connection.commit()
            connection.close()

            class FakeClient:
                cancelled = []

                def __init__(self, *_args, **_kwargs):
                    pass

                def submit(self, *_args, **_kwargs):
                    return {
                        "status": "queued",
                        "external_id": "73",
                        "message": "queued",
                    }

                def get_request(self, _request_id):
                    raise KeyboardInterrupt

                def cancel_request(self, request_id):
                    self.cancelled.append(request_id)
                    return {"status": "failed"}

            output = root / "state" / "shelfarr-evaluation" / "results.json"
            with (
                patch.object(evaluate_shelfarr, "ShelfarrClient", FakeClient),
                patch.object(evaluate_shelfarr, "observe_metadata_resolution"),
                patch.object(evaluate_shelfarr, "validate_shelfarr_database"),
                patch.object(evaluate_shelfarr, "library_has_title", return_value=False),
                self.assertRaises(KeyboardInterrupt),
            ):
                evaluate_shelfarr.evaluate(
                    root,
                    [12],
                    monitor_seconds=30,
                    poll_seconds=1,
                    output=output,
                )

            self.assertEqual(FakeClient.cancelled, ["73"])
            payload = __import__("json").loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["records"][0]["download_result"], "timed_out")

    def test_lost_post_is_recovered_and_cancelled_before_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state" / "huey").mkdir(parents=True)
            (root / "config" / "shelfarr").mkdir(parents=True)
            (root / ".env").write_text(
                "SHELFARR_ENABLED=true\nSHELFARR_API_TOKEN=shf_test\n",
                encoding="utf-8",
            )
            with closing(
                sqlite3.connect(root / "state" / "huey" / "huey.db")
            ) as connection, connection:
                connection.execute(
                    "CREATE TABLE requests "
                    "(id INTEGER, media_type TEXT, title TEXT, author TEXT, status TEXT)"
                )
                connection.execute(
                    "INSERT INTO requests VALUES "
                    "(12, 'ebooks', 'Dune', 'Frank Herbert', 'needs_selection')"
                )

            class LostPostClient:
                cancelled = []
                recoveries = 0

                def __init__(self, *_args, **_kwargs):
                    pass

                def submit(self, *_args, **_kwargs):
                    raise evaluate_shelfarr.ServiceError("lost response")

                def recover_request(self, request_id):
                    self.__class__.recoveries += 1
                    self.assert_correlation = request_id
                    return {"id": 73, "status": "searching"}

                def cancel_request(self, request_id):
                    self.cancelled.append(request_id)
                    return {"status": "failed"}

            output = root / "state" / "shelfarr-evaluation" / "results.json"
            with (
                patch.object(evaluate_shelfarr, "ShelfarrClient", LostPostClient),
                patch.object(evaluate_shelfarr, "observe_metadata_resolution"),
                patch.object(evaluate_shelfarr, "validate_shelfarr_database"),
                patch.object(evaluate_shelfarr, "library_has_title", return_value=False),
            ):
                records = evaluate_shelfarr.evaluate(
                    root,
                    [12],
                    monitor_seconds=0,
                    poll_seconds=1,
                    output=output,
                )

            self.assertEqual(LostPostClient.recoveries, 1)
            self.assertEqual(LostPostClient.cancelled, ["73"])
            self.assertEqual(records[0].correlation_id, 900_000_012)
            self.assertEqual(records[0].download_result, "submission_failed")
            self.assertEqual(records[0].shelfarr_status, "failed")

    def test_uncertain_submission_requires_operator_when_recovery_never_works(self):
        record = evaluate_shelfarr.EvaluationRecord(
            12,
            None,
            "Dune",
            "Frank Herbert",
            "ebook",
            "needs_selection",
            download_result="submission_uncertain",
            correlation_id=900_000_012,
        )

        class UnavailableClient:
            def recover_request(self, _request_id):
                raise evaluate_shelfarr.ServiceError("unavailable")

        evaluate_shelfarr.recover_uncertain_evaluation_requests(
            UnavailableClient(), [record]
        )
        self.assertEqual(record.download_result, "cleanup_failed")
        self.assertIn("operator cleanup", record.notes)

    def test_uncertain_submission_retries_transient_recovery_before_exit(self):
        record = evaluate_shelfarr.EvaluationRecord(
            12,
            None,
            "Dune",
            "Frank Herbert",
            "ebook",
            "needs_selection",
            download_result="submission_uncertain",
            correlation_id=900_000_012,
        )

        class EventuallyAvailableClient:
            calls = 0

            def recover_request(self, _request_id):
                self.calls += 1
                if self.calls == 1:
                    raise evaluate_shelfarr.ServiceError("temporary outage")
                return None

        client = EventuallyAvailableClient()
        evaluate_shelfarr.recover_uncertain_evaluation_requests(
            client, [record], final_attempt=False
        )
        self.assertEqual(record.download_result, "submission_uncertain")
        evaluate_shelfarr.recover_uncertain_evaluation_requests(client, [record])
        self.assertEqual(record.download_result, "cleanup_failed")
        self.assertIn("late request creation", record.notes)
        self.assertIn("operator cleanup", record.notes)
        self.assertEqual(client.calls, 2)

    def test_uncertain_completed_recovery_still_requires_artifact_verification(self):
        record = evaluate_shelfarr.EvaluationRecord(
            12,
            None,
            "Dune",
            "Frank Herbert",
            "ebook",
            "needs_selection",
            download_result="submission_uncertain",
            correlation_id=900_000_012,
        )

        class CompletedClient:
            def recover_request(self, _request_id):
                return {"id": 73, "status": "completed"}

        evaluate_shelfarr.recover_uncertain_evaluation_requests(
            CompletedClient(), [record]
        )
        self.assertEqual(record.shelfarr_request_id, "73")
        self.assertEqual(record.shelfarr_status, "completed")
        self.assertEqual(record.download_result, "processing")
        self.assertFalse(record.final_library_available)
        self.assertIn("verifying", record.notes)

    def test_finally_recovered_completion_verifies_das_without_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            final_file = (
                media_root
                / "ebooks"
                / "Books"
                / "Frank Herbert"
                / "Dune"
                / "book.epub"
            )
            final_file.parent.mkdir(parents=True)
            final_file.write_bytes(b"verified ebook")
            (root / "state" / "huey").mkdir(parents=True)
            (root / "config" / "shelfarr").mkdir(parents=True)
            (root / ".env").write_text(
                "SHELFARR_ENABLED=true\n"
                "SHELFARR_API_TOKEN=shf_test\n"
                f"MEDIA_ROOT={media_root}\n",
                encoding="utf-8",
            )
            with closing(
                sqlite3.connect(root / "state" / "huey" / "huey.db")
            ) as connection, connection:
                connection.execute(
                    "CREATE TABLE requests "
                    "(id INTEGER, media_type TEXT, title TEXT, author TEXT, status TEXT)"
                )
                connection.execute(
                    "INSERT INTO requests VALUES "
                    "(12, 'ebooks', 'Dune', 'Frank Herbert', 'needs_selection')"
                )

            class FinalRecoveryClient:
                recoveries = 0
                cancellations = []

                def __init__(self, *_args, **_kwargs):
                    pass

                def submit(self, *_args, **_kwargs):
                    raise evaluate_shelfarr.ServiceError("lost response")

                def recover_request(self, _request_id):
                    self.__class__.recoveries += 1
                    if self.recoveries == 1:
                        raise evaluate_shelfarr.ServiceError("temporary list failure")
                    return {"id": 73, "status": "completed"}

                def cancel_request(self, request_id):
                    self.cancellations.append(request_id)
                    return {"status": "failed"}

            artifact = {
                "file_path": "/ebooks/Frank Herbert/Dune/book.epub",
                "download_type": "direct",
                "source": "gutenberg",
                "release_title": "Dune",
            }
            output = root / "state" / "shelfarr-evaluation" / "results.json"
            with (
                patch.object(evaluate_shelfarr, "ShelfarrClient", FinalRecoveryClient),
                patch.object(evaluate_shelfarr, "observe_metadata_resolution"),
                patch.object(evaluate_shelfarr, "validate_shelfarr_database"),
                patch.object(evaluate_shelfarr, "library_has_title", return_value=False),
                patch.object(evaluate_shelfarr, "shelfarr_artifact", return_value=artifact),
            ):
                records = evaluate_shelfarr.evaluate(
                    root,
                    [12],
                    monitor_seconds=0,
                    poll_seconds=1,
                    output=output,
                )

            self.assertEqual(FinalRecoveryClient.recoveries, 2)
            self.assertEqual(FinalRecoveryClient.cancellations, [])
            self.assertEqual(records[0].download_result, "success")
            self.assertTrue(records[0].final_library_available)
            self.assertEqual(records[0].acquisition_source, "direct")

    def test_completed_recovery_verification_error_is_never_cancelled(self):
        record = evaluate_shelfarr.EvaluationRecord(
            12,
            "73",
            "Dune",
            "Frank Herbert",
            "ebook",
            "needs_selection",
            download_result="processing",
            shelfarr_status="completed",
        )

        with patch.object(
            evaluate_shelfarr,
            "shelfarr_artifact",
            side_effect=sqlite3.OperationalError("database busy"),
        ):
            evaluate_shelfarr.verify_recovered_completions(
                [record], Path("ignored.sqlite3"), Path("/mnt/media")
            )
        self.assertEqual(record.download_result, "cleanup_failed")
        self.assertIn("operator verification", record.notes)

        class MustNotCancel:
            def cancel_request(self, _request_id):
                raise AssertionError("completed request must not be cancelled")

        evaluate_shelfarr.cancel_active_evaluation_requests(
            MustNotCancel(), [record]
        )
        self.assertEqual(record.download_result, "cleanup_failed")

    def test_final_cleanup_retries_previous_cancel_failure(self):
        record = evaluate_shelfarr.EvaluationRecord(
            12,
            "73",
            "Dune",
            "Frank Herbert",
            "ebook",
            "needs_selection",
            download_result="cleanup_failed",
        )

        class RecoveredClient:
            def cancel_request(self, _request_id):
                return {"status": "failed"}

        evaluate_shelfarr.cancel_active_evaluation_requests(
            RecoveredClient(), [record]
        )
        self.assertEqual(record.download_result, "timed_out")
        self.assertEqual(record.shelfarr_status, "failed")

    def test_result_files_and_parent_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state" / "shelfarr-evaluation" / "report.json"
            record = evaluate_shelfarr.EvaluationRecord(
                1, None, "Dune", None, "ebook", "needs_selection"
            )
            path = evaluate_shelfarr.validate_evaluation_output(root, path)
            evaluate_shelfarr.write_results(path, [record])
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_retry_consolidation_supersedes_failure_but_preserves_success(self):
        failed = evaluate_shelfarr.EvaluationRecord(
            12, "3", "The Boxcar Children", None, "ebook", "needs_selection",
            download_result="failure",
        )
        succeeded = evaluate_shelfarr.EvaluationRecord(
            12, "7", "The Boxcar Children", None, "ebook", "needs_selection",
            download_result="success",
        )
        skipped = evaluate_shelfarr.EvaluationRecord(
            12, None, "The Boxcar Children", None, "ebook", "needs_selection",
            download_result="skipped_existing",
        )

        merged = evaluate_shelfarr.merge_evaluation_records([failed], [succeeded])
        self.assertEqual(merged[0].shelfarr_request_id, "7")
        preserved = evaluate_shelfarr.merge_evaluation_records(merged, [skipped])
        self.assertEqual(preserved[0].download_result, "success")
        self.assertEqual(preserved[0].shelfarr_request_id, "7")

        cleanup_failed = evaluate_shelfarr.EvaluationRecord(
            12, None, "The Boxcar Children", None, "ebook", "needs_selection",
            download_result="cleanup_failed",
            correlation_id=900_300_012,
            notes="A late request creation remains possible.",
        )
        unsafe = evaluate_shelfarr.merge_evaluation_records(
            preserved, [cleanup_failed]
        )
        self.assertEqual(unsafe[0].download_result, "cleanup_failed")
        self.assertEqual(unsafe[0].correlation_id, 900_300_012)
        still_unsafe = evaluate_shelfarr.merge_evaluation_records(unsafe, [skipped])
        self.assertEqual(still_unsafe[0].download_result, "cleanup_failed")
        self.assertEqual(still_unsafe[0].correlation_id, 900_300_012)

    def test_prior_unresolved_record_blocks_new_evaluation_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "state" / "shelfarr-evaluation" / "results.json"
            output = evaluate_shelfarr.validate_evaluation_output(root, output)
            unresolved = evaluate_shelfarr.EvaluationRecord(
                12,
                None,
                "The Boxcar Children",
                "Gertrude Chandler Warner",
                "ebook",
                "needs_selection",
                download_result="cleanup_failed",
                correlation_id=900_300_012,
            )
            evaluate_shelfarr.write_results(output, [unresolved])

            with (
                patch.object(evaluate_shelfarr, "_evaluate_locked") as locked,
                self.assertRaisesRegex(
                    evaluate_shelfarr.BootstrapError, "confirmed operator cleanup"
                ),
            ):
                evaluate_shelfarr.evaluate(
                    root,
                    [12],
                    monitor_seconds=0,
                    poll_seconds=1,
                    output=output,
                    attempt=4,
                )
            locked.assert_not_called()

    def test_unresolved_record_in_another_report_blocks_every_new_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "state" / "shelfarr-evaluation"
            current_output = evaluate_shelfarr.validate_evaluation_output(
                root, report_dir / "new-attempt.json"
            )
            unresolved = evaluate_shelfarr.EvaluationRecord(
                99,
                None,
                "Another Book",
                None,
                "ebook",
                "needs_selection",
                download_result="submission_uncertain",
                correlation_id=900_000_099,
            )
            evaluate_shelfarr.write_results(report_dir / "old-attempt.json", [unresolved])

            with (
                patch.object(evaluate_shelfarr, "_evaluate_locked") as locked,
                self.assertRaisesRegex(
                    evaluate_shelfarr.BootstrapError, "confirmed operator cleanup"
                ),
            ):
                evaluate_shelfarr.evaluate(
                    root,
                    [12],
                    monitor_seconds=0,
                    poll_seconds=1,
                    output=current_output,
                    attempt=5,
                )
            locked.assert_not_called()

    def test_evaluate_persists_prior_success_during_later_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "state" / "shelfarr-evaluation" / "results.json"
            output = evaluate_shelfarr.validate_evaluation_output(root, output)
            succeeded = evaluate_shelfarr.EvaluationRecord(
                12,
                "7",
                "The Boxcar Children",
                "Gertrude Chandler Warner",
                "ebook",
                "needs_selection",
                download_result="success",
                acquisition_source="direct",
                final_path="/ebooks/Gertrude Chandler Warner/The Boxcar Children/42796.epub",
                final_library_available=True,
            )
            evaluate_shelfarr.write_results(output, [succeeded])

            with patch.object(
                evaluate_shelfarr,
                "_evaluate_locked",
                side_effect=lambda *_args, prior_records=(), **_kwargs: list(
                    prior_records
                ),
            ) as locked:
                records = evaluate_shelfarr.evaluate(
                    root,
                    [12],
                    monitor_seconds=0,
                    poll_seconds=1,
                    output=output,
                    attempt=2,
                )

            self.assertEqual(records[0].download_result, "success")
            self.assertEqual(records[0].shelfarr_request_id, "7")
            self.assertEqual(
                locked.call_args.kwargs["prior_records"][0].acquisition_source,
                "direct",
            )

    def test_invalid_prior_report_fails_before_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "state" / "shelfarr-evaluation" / "results.json"
            output = evaluate_shelfarr.validate_evaluation_output(root, output)
            output.write_text('{"records":"invalid"}\n', encoding="utf-8")
            with self.assertRaises(evaluate_shelfarr.BootstrapError):
                evaluate_shelfarr.evaluate(
                    root,
                    [12],
                    monitor_seconds=0,
                    poll_seconds=1,
                    output=output,
                )

    def test_output_cannot_escape_or_chmod_shared_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "stack"
            root.mkdir()
            outside = Path(directory) / "report.json"
            with self.assertRaises(evaluate_shelfarr.BootstrapError):
                evaluate_shelfarr.validate_evaluation_output(root, outside)


if __name__ == "__main__":
    unittest.main()
