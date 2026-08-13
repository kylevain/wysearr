import sys
import unittest
from pathlib import Path


HUEY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUEY_ROOT))

from notifications import (  # noqa: E402
    EVENT_ROUTES,
    RoutedNotification,
    response_notifications,
    shelfarr_state_notifications,
    shelfarr_correlation_attention_notification,
    terminal_notifications,
)


class NotificationPolicyTests(unittest.TestCase):
    def request(self, **overrides):
        value = {
            "id": 42,
            "media_type": "movies-tv",
            "title": "Arrival",
            "external_title": "Arrival (2016)",
            "status": "queued",
            "service": "radarr",
            "channel_id": "1",
            "message_id": "100",
            "error": None,
        }
        value.update(overrides)
        return value

    def response(self, status, **overrides):
        value = {
            "request_id": 42,
            "status": status,
            "message": f"Handler returned {status}",
            "duplicate": False,
        }
        value.update(overrides)
        return value

    def assert_valid_plans(self, plans):
        self.assertEqual(len(plans), len({plan.event_key for plan in plans}))
        for plan in plans:
            self.assertIsInstance(plan, RoutedNotification)
            self.assertEqual(EVENT_ROUTES[plan.event_key], plan.route)
            self.assertTrue(plan.message.strip())

    def test_event_policy_covers_every_required_lifecycle_route(self):
        self.assertEqual(
            set(EVENT_ROUTES.values()),
            {
                "download-queue",
                "request-status",
                "recent-additions",
                "import-errors",
                "system-health",
            },
        )

    def test_new_queued_response_stages_accepted_and_download_events(self):
        plans = response_notifications(
            "movies-tv", self.response("queued"), self.request()
        )

        self.assert_valid_plans(plans)
        self.assertCountEqual(
            [plan.route for plan in plans],
            ["request-status", "download-queue"],
        )
        self.assertEqual(2, len(plans))

    def test_uncertain_submission_does_not_claim_acceptance_or_download(self):
        plans = response_notifications(
            "ebooks",
            self.response(
                "queued",
                service="shelfarr",
                external_status="submission_uncertain",
                message="Shelfarr submission is being reconciled.",
            ),
            self.request(media_type="ebooks", service="shelfarr", title="Dune"),
        )

        self.assert_valid_plans(plans)
        self.assertEqual([plan.event_key for plan in plans], ["submission_uncertain"])
        self.assertEqual([plan.route for plan in plans], ["import-errors"])
        self.assertNotIn("accepted", plans[0].message.casefold())
        self.assertNotIn("queued for acquisition", plans[0].message.casefold())

    def test_needs_selection_is_a_rejected_request_status_only(self):
        plans = response_notifications(
            "ebooks",
            self.response(
                "needs_selection",
                message="Please provide a more specific title.",
            ),
            self.request(
                media_type="ebooks",
                title="Dune",
                external_title=None,
                status="needs_selection",
                service=None,
            ),
        )

        self.assert_valid_plans(plans)
        self.assertEqual(["request-status"], [plan.route for plan in plans])

    def test_immediate_handler_terminal_response_uses_request_status_only(self):
        for status in ("failed", "complete", "completed"):
            with self.subTest(status=status):
                plans = response_notifications(
                    "movies-tv",
                    self.response(status),
                    self.request(status=status),
                )
                self.assert_valid_plans(plans)
                self.assertEqual(
                    ["request-status"], [plan.route for plan in plans]
                )

    def test_explicit_manual_intervention_adds_distinct_import_error_event(self):
        plans = response_notifications(
            "ebooks",
            self.response(
                "failed",
                message="The selected payload needs administrator review.",
                manual_intervention=True,
            ),
            self.request(
                media_type="ebooks",
                title="Dune",
                external_title="Dune EPUB",
                status="failed",
                service="qbittorrent",
            ),
        )

        self.assert_valid_plans(plans)
        self.assertCountEqual(
            [plan.route for plan in plans],
            ["request-status", "import-errors"],
        )
        self.assertCountEqual(
            [plan.event_key for plan in plans],
            ["request_failed", "manual_intervention"],
        )
        self.assertEqual(2, len({plan.message for plan in plans}))

    def test_duplicate_response_does_not_repeat_any_lifecycle_event(self):
        for status in ("queued", "needs_selection", "failed", "completed"):
            with self.subTest(status=status):
                plans = response_notifications(
                    "movies-tv",
                    self.response(status, duplicate=True),
                    self.request(status=status),
                )
                self.assertEqual([], list(plans))

    def test_arr_terminal_success_has_distinct_status_and_addition_events(self):
        for service in ("radarr", "sonarr", "lidarr"):
            with self.subTest(service=service):
                plans = terminal_notifications(
                    self.request(status="completed", service=service)
                )
                self.assert_valid_plans(plans)
                self.assertCountEqual(
                    [plan.route for plan in plans],
                    ["request-status", "recent-additions"],
                )
                self.assertEqual(2, len({plan.message for plan in plans}))
                combined = " ".join(plan.message for plan in plans)
                self.assertIn("DAS library path", combined)
                self.assertNotIn("Plex", combined)

    def test_shelfarr_intermediate_events_stay_in_download_queue(self):
        for status, event_key in (
            ("downloading", "download_active"),
            ("processing", "download_completed"),
        ):
            with self.subTest(status=status):
                plans = shelfarr_state_notifications(
                    self.request(
                        media_type="ebooks",
                        service="shelfarr",
                        title="Dune",
                        external_title="Dune",
                    ),
                    status,
                )
                self.assert_valid_plans(plans)
                self.assertEqual([plan.route for plan in plans], ["download-queue"])
                self.assertEqual([plan.event_key for plan in plans], [event_key])

    def test_uncertain_shelfarr_submission_requires_manual_review(self):
        plan = shelfarr_correlation_attention_notification(
            self.request(media_type="ebooks", service="shelfarr", title="Dune"),
            startup=False,
        )

        self.assert_valid_plans((plan,))
        self.assertEqual(plan.event_key, "submission_uncertain")
        self.assertEqual(plan.route, "import-errors")
        self.assertIn("Automatic retry remains blocked", plan.message)

    def test_uncertain_startup_recovery_is_a_system_health_event(self):
        plan = shelfarr_correlation_attention_notification(
            self.request(media_type="ebooks", service="shelfarr", title="Dune"),
            startup=True,
        )

        self.assert_valid_plans((plan,))
        self.assertEqual(plan.event_key, "recovery_uncertain")
        self.assertEqual(plan.route, "system-health")
        self.assertIn("Automatic retry remains blocked", plan.message)

    def test_shelfarr_terminal_success_has_status_and_addition(self):
        plans = terminal_notifications(
            self.request(
                media_type="ebooks",
                service="shelfarr",
                title="Dune",
                external_title="Dune",
                status="completed",
                terminal_event_type="shelfarr_completed",
            )
        )
        self.assert_valid_plans(plans)
        self.assertCountEqual(
            [plan.route for plan in plans], ["request-status", "recent-additions"]
        )
        self.assertIn("by Shelfarr", " ".join(plan.message for plan in plans))

    def test_bookbot_terminal_success_has_distinct_status_and_addition_events(self):
        plans = terminal_notifications(
            self.request(
                media_type="ebooks",
                title="Dune",
                external_title="Dune EPUB",
                status="complete",
                service="bookbot",
            )
        )

        self.assert_valid_plans(plans)
        self.assertCountEqual(
            [plan.route for plan in plans],
            ["request-status", "recent-additions"],
        )
        self.assertEqual(2, len({plan.message for plan in plans}))
        combined = " ".join(plan.message for plan in plans)
        self.assertIn("DAS library path", combined)
        self.assertNotIn("Plex", combined)

    def test_bookbot_terminal_failure_has_distinct_status_and_import_error_events(self):
        plans = terminal_notifications(
            self.request(
                media_type="ebooks",
                title="Dune",
                external_title="Dune EPUB",
                status="failed",
                service="bookbot",
                error="Validated copy failed and needs manual intervention",
            )
        )

        self.assert_valid_plans(plans)
        self.assertCountEqual(
            [plan.route for plan in plans],
            ["request-status", "import-errors"],
        )
        self.assertEqual(2, len({plan.message for plan in plans}))

    def test_runtime_recovery_routes_request_failure_and_system_health(self):
        plans = terminal_notifications(
            self.request(
                status="failed",
                service=None,
                terminal_event_type="startup_reconciled",
                error="Huey restarted before a durable queued state",
            )
        )

        self.assert_valid_plans(plans)
        self.assertCountEqual(
            [plan.route for plan in plans],
            ["request-status", "system-health"],
        )
        self.assertCountEqual(
            [plan.event_key for plan in plans],
            ["request_failed", "system_health"],
        )

    def test_terminal_messages_sanitize_untrusted_persisted_fields(self):
        plans = terminal_notifications(
            self.request(
                status="failed",
                service="bookbot",
                external_title="Dune @everyone https://invalid/?token=secret",
                error="https://service.invalid/?apikey=do-not-post",
            )
        )

        self.assert_valid_plans(plans)
        combined = " ".join(plan.message for plan in plans)
        self.assertIn("Arrival", combined)
        for secret in ("@everyone", "https://", "token", "apikey", "do-not-post"):
            self.assertNotIn(secret, combined)


if __name__ == "__main__":
    unittest.main()
