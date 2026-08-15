import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import rotate_qbittorrent_password as rotation


OLD_PASSWORD = "old-private-password-123456"
NEW_PASSWORD = "new-private-password-654321"
TORRENT_HASHES = ("a" * 40, "b" * 40, "c" * 64)


class FakeQbittorrent:
    def __init__(self):
        self.password = OLD_PASSWORD
        self.auth_fail_limit = 5
        self.hashes = list(TORRENT_HASHES)
        self.events = []
        self.password_behavior = "success"
        self.guard_restore_behavior = "success"

    def factory(self, base_url, timeout):
        self.events.append(("factory", base_url, timeout))
        return FakeClient(self)


class FakeClient:
    def __init__(self, service):
        self.service = service
        self.authenticated = False

    def login(self, username, password):
        accepted = username == "admin" and password == self.service.password
        self.service.events.append(("login", username, password, accepted))
        self.authenticated = accepted
        return accepted

    def _require_auth(self):
        if not self.authenticated:
            raise RuntimeError("unauthorized")

    def version(self):
        self._require_auth()
        return rotation.EXPECTED_VERSION

    def webapi_version(self):
        self._require_auth()
        return rotation.EXPECTED_WEBAPI_VERSION

    def preferences(self):
        self._require_auth()
        return {"web_ui_max_auth_fail_count": self.service.auth_fail_limit}

    def torrents(self, category=None):
        self._require_auth()
        self.service.events.append(("torrents", category))
        return [{"hash": value} for value in self.service.hashes]

    def set_preferences_once(self, values):
        self._require_auth()
        self.service.events.append(("set", dict(values)))
        if "web_ui_password" in values:
            if self.service.password_behavior == "raise_before":
                self.service.password_behavior = "success"
                raise RuntimeError("request did not reach server")
            self.service.password = values["web_ui_password"]
            if self.service.password_behavior == "raise_after":
                self.service.password_behavior = "success"
                raise RuntimeError("response was lost")
        if "web_ui_max_auth_fail_count" in values:
            value = values["web_ui_max_auth_fail_count"]
            if (
                value == 5
                and self.service.guard_restore_behavior == "raise_before"
            ):
                self.service.guard_restore_behavior = "success"
                raise RuntimeError("guard response was lost")
            self.service.auth_fail_limit = value
            if (
                value == 5
                and self.service.guard_restore_behavior == "raise_after"
            ):
                self.service.guard_restore_behavior = "success"
                raise RuntimeError("guard response was lost")


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config" / "qbittorrent").mkdir(parents=True)
        self.env_path = self.root / ".env"
        self.env_path.write_text(
            "# retained operator note\n"
            "WYSEARR_BIND_ADDRESS=192.0.2.20\n"
            "QBITTORRENT_PORT=8080\n"
            "QBITTORRENT_USERNAME=admin\n"
            f"QBITTORRENT_PASSWORD={OLD_PASSWORD}\n"
            "UNRELATED=value\n",
            encoding="utf-8",
        )
        os.chmod(self.env_path, 0o600)
        self.service = FakeQbittorrent()
        self.password_calls = 0

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def private(self):
        return self.root / rotation.PRIVATE_DIRECTORY_RELATIVE

    @property
    def journal_path(self):
        return self.private / rotation.JOURNAL_NAME

    def password_factory(self):
        self.password_calls += 1
        return NEW_PASSWORD

    def env(self):
        return rotation._environment(self.env_path.read_text(encoding="utf-8"))

    def run_rotation(self, **updates):
        options = {
            "apply": True,
            "client_factory": self.service.factory,
            "password_factory": self.password_factory,
        }
        options.update(updates)
        return rotation.rotate_qbittorrent_password(self.root, **options)

    def password_sets(self):
        return [
            event
            for event in self.service.events
            if event[0] == "set" and "web_ui_password" in event[1]
        ]

    def test_default_preflight_is_read_only_and_uses_unfiltered_inventory(self):
        result = rotation.rotate_qbittorrent_password(
            self.root,
            client_factory=self.service.factory,
            password_factory=lambda: self.fail("generated during preflight"),
        )

        self.assertFalse(result.applied)
        self.assertFalse(result.pending_recovery)
        self.assertEqual(result.torrent_count, len(TORRENT_HASHES))
        self.assertFalse(self.private.exists())
        self.assertEqual(self.service.password, OLD_PASSWORD)
        self.assertEqual(self.service.auth_fail_limit, 5)
        self.assertIn(("torrents", None), self.service.events)
        self.assertEqual(self.password_sets(), [])

    def test_success_is_exactly_once_private_and_secret_free(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.run_rotation()

        self.assertTrue(result.applied)
        self.assertFalse(result.resumed)
        self.assertEqual(result.torrent_count, len(TORRENT_HASHES))
        self.assertEqual(self.password_calls, 1)
        self.assertEqual(self.service.password, NEW_PASSWORD)
        self.assertEqual(self.service.auth_fail_limit, 5)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], NEW_PASSWORD)
        self.assertIn("# retained operator note", self.env_path.read_text())
        self.assertIn("UNRELATED=value", self.env_path.read_text())
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.journal_path.exists())
        self.assertEqual(len(self.password_sets()), 1)
        guard_values = [
            event[1]["web_ui_max_auth_fail_count"]
            for event in self.service.events
            if event[0] == "set" and "web_ui_max_auth_fail_count" in event[1]
        ]
        self.assertEqual(guard_values, [rotation.ROTATION_GUARD_MINIMUM, 5])
        self.assertGreaterEqual(
            sum(event == ("torrents", None) for event in self.service.events), 2
        )
        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(OLD_PASSWORD, rendered)
        self.assertNotIn(NEW_PASSWORD, rendered)

    def test_lost_response_after_commit_converges_without_reposting(self):
        self.service.password_behavior = "raise_after"

        result = self.run_rotation()

        self.assertTrue(result.applied)
        self.assertEqual(self.service.password, NEW_PASSWORD)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], NEW_PASSWORD)
        self.assertEqual(len(self.password_sets()), 1)

    def test_guard_is_raised_before_the_first_wrong_password_probe(self):
        self.service.auth_fail_limit = 1

        result = self.run_rotation()

        self.assertTrue(result.applied)
        guard_index = next(
            index
            for index, event in enumerate(self.service.events)
            if event
            == (
                "set",
                {"web_ui_max_auth_fail_count": rotation.ROTATION_GUARD_MINIMUM},
            )
        )
        password_write_index = next(
            index
            for index, event in enumerate(self.service.events)
            if event == ("set", {"web_ui_password": NEW_PASSWORD})
        )
        rejected_probe_indexes = [
            index
            for index, event in enumerate(self.service.events)
            if event[0] == "login" and event[-1] is False
        ]
        self.assertLess(guard_index, password_write_index)
        self.assertTrue(rejected_probe_indexes)
        self.assertTrue(
            all(guard_index < index for index in rejected_probe_indexes)
        )
        restore_index = next(
            index
            for index, event in enumerate(self.service.events)
            if event == ("set", {"web_ui_max_auth_fail_count": 1})
        )
        self.assertTrue(
            all(index < restore_index for index in rejected_probe_indexes)
        )
        self.assertEqual(self.service.auth_fail_limit, 1)

    def test_remote_attempted_preflight_accepts_either_committed_state(self):
        self.service.password_behavior = "raise_before"
        with self.assertRaises(rotation.RotationError):
            self.run_rotation()
        self.assertEqual(
            rotation._read_journal(self.journal_path).phase,
            "remote_attempted",
        )

        # Model a process death after qBittorrent committed the write but
        # before the client received or reconciled the response.
        self.service.password = NEW_PASSWORD
        result = rotation.rotate_qbittorrent_password(
            self.root,
            client_factory=self.service.factory,
            password_factory=lambda: self.fail("generated during preflight"),
        )

        self.assertFalse(result.applied)
        self.assertTrue(result.pending_recovery)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], OLD_PASSWORD)
        self.assertTrue(self.journal_path.exists())

    def test_phase_checkpoint_failure_after_remote_commit_is_forward_only(self):
        original_write = rotation._write_journal

        def fail_remote_applied(path, journal):
            if journal.phase == "remote_applied":
                raise rotation.RotationError(rotation.GENERIC_FAILURE)
            original_write(path, journal)

        with mock.patch.object(
            rotation, "_write_journal", side_effect=fail_remote_applied
        ):
            with self.assertRaises(rotation.RotationError) as raised:
                self.run_rotation()

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.password, NEW_PASSWORD)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], OLD_PASSWORD)
        self.assertEqual(
            rotation._read_journal(self.journal_path).phase,
            "remote_attempted",
        )

        result = self.run_rotation()
        self.assertTrue(result.applied)
        self.assertTrue(result.resumed)
        self.assertEqual(len(self.password_sets()), 1)

    def test_lost_guard_restore_response_reguards_before_rejection_probe(self):
        self.service.guard_restore_behavior = "raise_after"
        with self.assertRaises(rotation.RotationError) as raised:
            self.run_rotation()

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.password, NEW_PASSWORD)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], NEW_PASSWORD)
        self.assertEqual(self.service.auth_fail_limit, 5)
        self.assertEqual(
            rotation._read_journal(self.journal_path).phase,
            "env_converged",
        )
        resume_start = len(self.service.events)

        result = self.run_rotation()

        self.assertTrue(result.applied)
        self.assertTrue(result.resumed)
        resumed_events = self.service.events[resume_start:]
        guard_index = resumed_events.index(
            (
                "set",
                {"web_ui_max_auth_fail_count": rotation.ROTATION_GUARD_MINIMUM},
            )
        )
        rejected_index = next(
            index
            for index, event in enumerate(resumed_events)
            if event[0] == "login" and event[-1] is False
        )
        self.assertLess(guard_index, rejected_index)
        self.assertEqual(len(self.password_sets()), 1)

    def test_guard_restored_resume_does_not_probe_old_or_raise_guard(self):
        with mock.patch.object(
            rotation,
            "_remove_journal",
            side_effect=rotation.RotationError(
                rotation.FORWARD_RECOVERY_FAILURE
            ),
        ):
            with self.assertRaises(rotation.RotationError):
                self.run_rotation()
        self.assertEqual(
            rotation._read_journal(self.journal_path).phase,
            "guard_restored",
        )
        self.assertEqual(self.service.auth_fail_limit, 5)
        resume_start = len(self.service.events)

        result = self.run_rotation()

        self.assertTrue(result.applied)
        self.assertTrue(result.resumed)
        resumed_events = self.service.events[resume_start:]
        self.assertFalse(
            any(event[0] == "login" and event[-1] is False for event in resumed_events)
        )
        self.assertFalse(any(event[0] == "set" for event in resumed_events))

    def test_failed_remote_attempt_resumes_same_generation(self):
        self.service.password_behavior = "raise_before"
        with self.assertRaises(rotation.RotationError) as raised:
            self.run_rotation()

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.password, OLD_PASSWORD)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], OLD_PASSWORD)
        self.assertEqual(self.service.auth_fail_limit, rotation.ROTATION_GUARD_MINIMUM)
        self.assertTrue(self.journal_path.is_file())
        self.assertEqual(self.journal_path.stat().st_mode & 0o777, 0o600)
        journal = rotation._read_journal(self.journal_path)
        self.assertEqual(journal.phase, "remote_attempted")
        self.assertEqual(journal.new_password, NEW_PASSWORD)

        result = self.run_rotation()
        self.assertTrue(result.applied)
        self.assertTrue(result.resumed)
        self.assertEqual(self.password_calls, 1)
        self.assertEqual(
            {event[1]["web_ui_password"] for event in self.password_sets()},
            {NEW_PASSWORD},
        )
        self.assertEqual(self.service.password, NEW_PASSWORD)
        self.assertEqual(self.service.auth_fail_limit, 5)
        self.assertFalse(self.journal_path.exists())

    def test_env_failure_is_forward_recovered_without_second_remote_change(self):
        def fail_before_write(_path, _updates):
            raise OSError("local write failed")

        with self.assertRaises(rotation.RotationError) as raised:
            self.run_rotation(dotenv_updater=fail_before_write)
        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.password, NEW_PASSWORD)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], OLD_PASSWORD)
        self.assertTrue(self.journal_path.exists())
        first_remote_writes = len(self.password_sets())

        result = self.run_rotation()
        self.assertTrue(result.applied)
        self.assertTrue(result.resumed)
        self.assertEqual(len(self.password_sets()), first_remote_writes)
        self.assertEqual(self.password_calls, 1)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], NEW_PASSWORD)

    def test_hash_inventory_change_fails_closed_and_keeps_journal(self):
        original_updater = rotation.update_dotenv

        def mutate_inventory(path, updates):
            result = original_updater(path, updates)
            self.service.hashes.append("d" * 40)
            return result

        with self.assertRaises(rotation.RotationError) as raised:
            self.run_rotation(dotenv_updater=mutate_inventory)

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertTrue(self.journal_path.exists())
        self.assertEqual(self.service.password, NEW_PASSWORD)
        self.assertEqual(self.service.auth_fail_limit, rotation.ROTATION_GUARD_MINIMUM)

    def test_hash_drift_before_remote_write_blocks_credential_mutation(self):
        def drift_then_generate():
            self.password_calls += 1
            self.service.hashes.append("d" * 40)
            return NEW_PASSWORD

        with self.assertRaises(rotation.RotationError) as raised:
            self.run_rotation(password_factory=drift_then_generate)

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.password, OLD_PASSWORD)
        self.assertEqual(self.env()["QBITTORRENT_PASSWORD"], OLD_PASSWORD)
        self.assertEqual(self.service.auth_fail_limit, 5)
        self.assertEqual(self.password_sets(), [])
        self.assertEqual(
            rotation._read_journal(self.journal_path).phase,
            "prepared",
        )

    def test_pending_journal_preflight_does_not_mutate_or_generate(self):
        self.service.password_behavior = "raise_before"
        with self.assertRaises(rotation.RotationError):
            self.run_rotation()
        events_before = list(self.service.events)

        result = rotation.rotate_qbittorrent_password(
            self.root,
            client_factory=self.service.factory,
            password_factory=lambda: self.fail("generated during recovery preflight"),
        )

        self.assertFalse(result.applied)
        self.assertTrue(result.pending_recovery)
        self.assertEqual(self.password_calls, 1)
        self.assertEqual(
            [event for event in self.service.events[len(events_before):] if event[0] == "set"],
            [],
        )

    def test_main_errors_are_fixed_and_never_render_credentials(self):
        stderr = io.StringIO()
        with mock.patch.object(
            rotation,
            "rotate_qbittorrent_password",
            side_effect=RuntimeError(OLD_PASSWORD + NEW_PASSWORD),
        ), contextlib.redirect_stderr(stderr):
            result = rotation.main(["--root", str(self.root)])

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue().strip(), "ERROR: " + rotation.GENERIC_FAILURE)
        self.assertNotIn(OLD_PASSWORD, stderr.getvalue())
        self.assertNotIn(NEW_PASSWORD, stderr.getvalue())

    def test_cli_has_no_credential_argument(self):
        parser_help = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(parser_help):
            rotation.parse_args(["--help"])
        rendered = parser_help.getvalue().casefold()
        self.assertNotIn("--password", rendered)
        self.assertNotIn("--username", rendered)

    def test_utility_has_no_process_control_or_restart_path(self):
        source = Path(rotation.__file__).read_text(encoding="utf-8").casefold()
        self.assertNotIn("subprocess", source)
        self.assertNotIn("docker", source)
        self.assertNotIn("restart_qbittorrent", source)
        self.assertNotIn("/api/v2/app/shutdown", source)


if __name__ == "__main__":
    unittest.main()
