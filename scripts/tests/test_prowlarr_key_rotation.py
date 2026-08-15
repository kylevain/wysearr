import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import bootstrap
from scripts import rotate_prowlarr_key as rotation


OLD_KEY = "a" * 32
NEW_KEY = "b" * 32


class FakeProwlarr:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.current_key = OLD_KEY
        self.generated_key = NEW_KEY
        self.behavior = "rotate"
        self.events = []
        self.commands = [
            {"id": 4, "name": "ResetApiKey", "status": "completed"},
            {"id": 5, "name": "ApplicationIndexerSync", "status": "completed"},
        ]
        self.next_command_id = 6
        self.after_reset = None

    def factory(self, base_url, api_key, timeout, retries):
        self.events.append(("factory", base_url, api_key, timeout, retries))
        return FakeClient(self, base_url, api_key)

    def write_config(self, key):
        self.config_path.write_text(
            f"<Config><ApiKey>{key}</ApiKey></Config>\n",
            encoding="utf-8",
        )
        os.chmod(self.config_path, 0o600)


class FakeClient:
    def __init__(self, service: FakeProwlarr, base_url: str, api_key: str):
        self.service = service
        self.base_url = base_url
        self.api_key = api_key

    def _authenticate(self, path):
        if self.api_key != self.service.current_key:
            raise bootstrap.ApiError(401, path, "Unauthorized")

    def get_json(self, path):
        self.service.events.append(("get", self.base_url, path, self.api_key))
        self._authenticate(path)
        if path == rotation.COMMAND_PATH:
            return [dict(command) for command in self.service.commands]
        if path == rotation.HOST_CONFIG_PATH:
            return {
                "id": 1,
                "apiKey": self.service.current_key,
                "bindAddress": "*",
                "port": 9696,
            }
        raise AssertionError(f"unexpected GET path: {path}")

    def post_json(self, path, payload, *, retry=False):
        self.service.events.append(
            ("post", self.base_url, path, self.api_key, dict(payload), retry)
        )
        self._authenticate(path)
        if path != rotation.COMMAND_PATH:
            raise AssertionError(f"unexpected POST path: {path}")
        if self.service.behavior == "raise_before":
            raise RuntimeError("transport failed before acceptance")

        identifier = self.service.next_command_id
        self.service.next_command_id += 1
        command = {"id": identifier, "name": "ResetApiKey", "status": "started"}
        self.service.commands.append(command)

        if self.service.behavior not in {"queued", "failed"}:
            if self.service.behavior == "malformed_config":
                self.service.current_key = NEW_KEY
                self.service.config_path.write_text(
                    "<Config><ApiKey>not-valid</ApiKey></Config>\n",
                    encoding="utf-8",
                )
                os.chmod(self.service.config_path, 0o600)
            elif self.service.behavior == "incoherent_auth":
                self.service.write_config(self.service.generated_key)
            else:
                self.service.current_key = self.service.generated_key
                self.service.write_config(self.service.generated_key)
            command["status"] = "completed"
            if self.service.after_reset is not None:
                self.service.after_reset()
        elif self.service.behavior == "failed":
            command["status"] = "failed"

        if self.service.behavior in {"raise_after", "queued"}:
            raise RuntimeError("transport response was lost")
        return dict(command)

    def put_json(self, path, payload):
        self.service.events.append(("put", self.base_url, path, self.api_key))
        raise AssertionError("API-key rotation must never use host-config PUT")


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config" / "prowlarr").mkdir(parents=True)
        self.env_path = self.root / ".env"
        self.config_path = self.root / "config" / "prowlarr" / "config.xml"
        self.env_path.write_text(
            "# retained operator note\n"
            "WYSEARR_BIND_ADDRESS=192.0.2.44\n"
            "PROWLARR_PORT=9696\n"
            f"PROWLARR_API_KEY={OLD_KEY}\n"
            "UNRELATED=value\n",
            encoding="utf-8",
        )
        self.config_path.write_text(
            f"<Config><ApiKey>{OLD_KEY}</ApiKey></Config>\n",
            encoding="utf-8",
        )
        os.chmod(self.env_path, 0o600)
        os.chmod(self.config_path, 0o600)
        self.service = FakeProwlarr(self.config_path)

    def tearDown(self):
        self.temporary.cleanup()

    def env_key(self):
        text = self.env_path.read_text(encoding="utf-8")
        return rotation._environment(text)["PROWLARR_API_KEY"]

    def config_key(self):
        return rotation._config_key(self.config_path.read_text(encoding="utf-8"))

    def rotate(self, **updates):
        options = {
            "apply": True,
            "client_factory": self.service.factory,
            "config_wait_seconds": 0.0,
        }
        options.update(updates)
        return rotation.rotate_prowlarr_key(self.root, **options)

    def post_events(self):
        return [event for event in self.service.events if event[0] == "post"]

    def test_success_posts_one_supported_command_and_converges_env(self):
        result = self.rotate()

        self.assertTrue(result.applied)
        self.assertFalse(result.resumed)
        self.assertEqual(self.service.current_key, NEW_KEY)
        self.assertEqual(self.env_key(), NEW_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        self.assertIn("# retained operator note", self.env_path.read_text())
        self.assertIn("UNRELATED=value", self.env_path.read_text())

        posts = self.post_events()
        self.assertEqual(len(posts), 1)
        operation, base_url, path, presented_key, payload, retry = posts[0]
        self.assertEqual(operation, "post")
        self.assertEqual(base_url, "http://192.0.2.44:9696")
        self.assertEqual(path, "/api/v1/command")
        self.assertEqual(presented_key, OLD_KEY)
        self.assertEqual(payload, {"name": "ResetApiKey"})
        self.assertFalse(retry)
        for rendered in (base_url, path, repr(payload)):
            self.assertNotIn(OLD_KEY, rendered)
            self.assertNotIn(NEW_KEY, rendered)
            self.assertNotIn("?", path)
        self.assertFalse(any(event[0] == "put" for event in self.service.events))

    def test_default_factory_places_key_only_in_header(self):
        sentinel = object()
        with mock.patch.object(rotation, "ApiClient", return_value=sentinel) as client:
            result = rotation._default_client_factory(
                "http://192.0.2.44:9696", OLD_KEY, 9.0, 2
            )

        self.assertIs(result, sentinel)
        client.assert_called_once_with(
            "http://192.0.2.44:9696",
            headers={"X-Api-Key": OLD_KEY},
            timeout=9.0,
            retries=2,
        )
        self.assertNotIn(OLD_KEY, client.call_args.args[0])

    def test_default_mode_is_read_only_preflight(self):
        result = rotation.rotate_prowlarr_key(
            self.root,
            client_factory=self.service.factory,
            config_wait_seconds=0.0,
        )

        self.assertFalse(result.applied)
        self.assertFalse(result.pending_local_convergence)
        self.assertEqual(self.service.current_key, OLD_KEY)
        self.assertEqual(self.env_key(), OLD_KEY)
        self.assertEqual(self.post_events(), [])

    def test_lost_response_after_acceptance_converges_without_repost(self):
        self.service.behavior = "raise_after"

        result = self.rotate()

        self.assertTrue(result.applied)
        self.assertEqual(self.service.current_key, NEW_KEY)
        self.assertEqual(self.env_key(), NEW_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)
        self.assertEqual(len(self.post_events()), 1)

    def test_lost_response_before_acceptance_is_generic_and_never_reposted(self):
        self.service.behavior = "raise_before"

        with self.assertRaises(rotation.RotationError) as raised:
            self.rotate()

        self.assertEqual(str(raised.exception), rotation.GENERIC_FAILURE)
        self.assertEqual(self.service.current_key, OLD_KEY)
        self.assertEqual(self.env_key(), OLD_KEY)
        self.assertEqual(self.config_key(), OLD_KEY)
        self.assertEqual(len(self.post_events()), 1)

    def test_visible_queued_command_is_forward_only_and_never_reposted(self):
        self.service.behavior = "queued"

        with self.assertRaises(rotation.RotationError) as raised:
            self.rotate()

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.current_key, OLD_KEY)
        self.assertEqual(self.env_key(), OLD_KEY)
        self.assertEqual(self.config_key(), OLD_KEY)
        self.assertEqual(len(self.post_events()), 1)

    def test_preexisting_pending_reset_is_reported_and_never_duplicated(self):
        self.service.commands.append(
            {"id": 6, "name": "ResetApiKey", "status": "queued"}
        )

        preflight = rotation.rotate_prowlarr_key(
            self.root,
            client_factory=self.service.factory,
            config_wait_seconds=0.0,
        )
        self.assertTrue(preflight.pending_remote_reset)

        for _attempt in range(2):
            with self.assertRaises(rotation.RotationError) as raised:
                self.rotate()
            self.assertEqual(
                str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE
            )
        self.assertEqual(self.post_events(), [])
        self.assertEqual(self.env_key(), OLD_KEY)
        self.assertEqual(self.config_key(), OLD_KEY)

    def test_preexisting_pending_reset_can_complete_and_converge_without_post(self):
        pending = {"id": 6, "name": "ResetApiKey", "status": "started"}
        self.service.commands.append(pending)
        clock = [0.0]

        def complete_during_poll(_seconds):
            self.service.current_key = NEW_KEY
            self.service.write_config(NEW_KEY)
            pending["status"] = "completed"
            clock[0] = 1.0

        result = self.rotate(
            config_wait_seconds=1.0,
            sleep=complete_during_poll,
            monotonic=lambda: clock[0],
        )

        self.assertTrue(result.applied)
        self.assertEqual(self.post_events(), [])
        self.assertEqual(self.env_key(), NEW_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)

    def test_new_config_with_incoherent_auth_fails_forward_without_env_write(self):
        self.service.behavior = "incoherent_auth"

        with self.assertRaises(rotation.RotationError) as raised:
            self.rotate()

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.current_key, OLD_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)
        self.assertEqual(self.env_key(), OLD_KEY)
        self.assertEqual(len(self.post_events()), 1)

    def test_env_failure_is_recoverable_forward_without_second_reset(self):
        def fail_before_replace(path, updates):
            raise OSError("local persistence failed")

        with self.assertRaises(rotation.RotationError) as raised:
            self.rotate(dotenv_updater=fail_before_replace)

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertEqual(self.service.current_key, NEW_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)
        self.assertEqual(self.env_key(), OLD_KEY)
        self.assertEqual(len(self.post_events()), 1)

        preflight = rotation.rotate_prowlarr_key(
            self.root,
            client_factory=self.service.factory,
            config_wait_seconds=0.0,
        )
        self.assertTrue(preflight.pending_local_convergence)
        self.assertEqual(len(self.post_events()), 1)

        resumed = self.rotate()
        self.assertTrue(resumed.applied)
        self.assertTrue(resumed.resumed)
        self.assertEqual(self.env_key(), NEW_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)
        self.assertEqual(len(self.post_events()), 1)

    def test_replace_then_updater_exception_is_verified_as_success(self):
        def replace_then_raise(path, updates):
            rotation.update_dotenv(path, updates)
            raise OSError("failure after atomic replace")

        result = self.rotate(dotenv_updater=replace_then_raise)

        self.assertTrue(result.applied)
        self.assertEqual(self.env_key(), NEW_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)
        self.assertEqual(len(self.post_events()), 1)

    def test_concurrent_env_edit_is_preserved_and_requires_forward_recovery(self):
        def edit_env():
            self.env_path.write_text(
                self.env_path.read_text(encoding="utf-8") + "OPERATOR_EDIT=yes\n",
                encoding="utf-8",
            )
            os.chmod(self.env_path, 0o600)

        self.service.after_reset = edit_env

        with self.assertRaises(rotation.RotationError) as raised:
            self.rotate()

        self.assertEqual(str(raised.exception), rotation.FORWARD_RECOVERY_FAILURE)
        self.assertIn("OPERATOR_EDIT=yes", self.env_path.read_text())
        self.assertEqual(self.env_key(), OLD_KEY)
        self.assertEqual(self.config_key(), NEW_KEY)
        self.assertEqual(len(self.post_events()), 1)

    def test_same_or_malformed_server_key_is_never_persisted(self):
        for behavior, generated in (("rotate", OLD_KEY), ("malformed_config", NEW_KEY)):
            with self.subTest(behavior=behavior, generated=generated):
                self.service.behavior = behavior
                self.service.generated_key = generated
                with self.assertRaises(rotation.RotationError):
                    self.rotate()
                self.assertEqual(self.env_key(), OLD_KEY)
                self.assertEqual(len(self.post_events()), 1)

                # Restore the fake and config for the next subtest only.
                self.service.events.clear()
                self.service.current_key = OLD_KEY
                self.service.commands = self.service.commands[:2]
                self.service.write_config(OLD_KEY)

    def test_duplicate_assignment_fails_before_api_mutation(self):
        self.env_path.write_text(
            f"PROWLARR_API_KEY={OLD_KEY}\nPROWLARR_API_KEY={OLD_KEY}\n",
            encoding="utf-8",
        )
        os.chmod(self.env_path, 0o600)

        with self.assertRaises(rotation.RotationError):
            rotation.rotate_prowlarr_key(
                self.root,
                client_factory=self.service.factory,
            )
        self.assertEqual(self.post_events(), [])

    def test_weak_or_oversized_private_file_fails_before_api_mutation(self):
        os.chmod(self.env_path, 0o644)
        with self.assertRaises(rotation.RotationError):
            rotation.rotate_prowlarr_key(
                self.root,
                client_factory=self.service.factory,
            )
        self.assertEqual(self.post_events(), [])

        self.env_path.write_text("x" * (rotation.MAX_ENV_BYTES + 1), encoding="utf-8")
        os.chmod(self.env_path, 0o600)
        with self.assertRaises(rotation.RotationError):
            rotation.rotate_prowlarr_key(
                self.root,
                client_factory=self.service.factory,
            )
        self.assertEqual(self.post_events(), [])

    def test_symlinked_env_fails_before_api_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "real.env"
            target.write_text(f"PROWLARR_API_KEY={OLD_KEY}\n", encoding="utf-8")
            os.chmod(target, 0o600)
            (root / ".env").symlink_to(target)
            (root / "config" / "prowlarr").mkdir(parents=True)
            config = root / "config" / "prowlarr" / "config.xml"
            config.write_text(
                f"<Config><ApiKey>{OLD_KEY}</ApiKey></Config>", encoding="utf-8"
            )
            os.chmod(config, 0o600)
            with self.assertRaises(rotation.RotationError):
                rotation.rotate_prowlarr_key(root, client_factory=self.service.factory)
        self.assertEqual(self.post_events(), [])

    def test_main_never_prints_an_underlying_secret(self):
        stderr = io.StringIO()
        with mock.patch.object(
            rotation,
            "rotate_prowlarr_key",
            side_effect=RuntimeError(f"URL contained {OLD_KEY}"),
        ), contextlib.redirect_stderr(stderr):
            status = rotation.main(["--root", str(self.root), "--apply"])

        self.assertEqual(status, 1)
        self.assertNotIn(OLD_KEY, stderr.getvalue())
        self.assertIn("rotation failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
