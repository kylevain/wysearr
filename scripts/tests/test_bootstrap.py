import contextlib
import copy
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

from scripts import bootstrap


def provider_resource(
    *,
    resource_id=1,
    category="old",
    imported="old-imported",
    password="********",
):
    return {
        "id": resource_id,
        "name": "qBittorrent",
        "implementation": "QBittorrent",
        "removeCompletedDownloads": True,
        "fields": [
            {"name": "host", "value": "qbittorrent"},
            {"name": "port", "value": 8080},
            {"name": "useSsl", "value": False},
            {"name": "username", "value": "admin"},
            {"name": "password", "value": password},
            {"name": "category", "value": category},
            {"name": "postImportCategory", "value": imported},
        ],
    }


def indexer_schema(spec):
    return {
        "id": 0,
        "name": spec.name,
        "definitionName": spec.definition,
        "implementation": "Cardigann",
        "configContract": "CardigannSettings",
        "enable": False,
        "supportsRss": True,
        "supportsSearch": True,
        "appProfileId": 1,
        "priority": 20,
        "fields": [{"name": "baseUrl", "value": "https://old.invalid/"}],
    }


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for index, (env_name, service) in enumerate(
            bootstrap.API_KEY_CONFIGS.items(), start=1
        ):
            directory = self.root / "config" / service
            directory.mkdir(parents=True)
            (directory / "config.xml").write_text(
                f"<Config><ApiKey>api-key-{index}</ApiKey></Config>",
                encoding="utf-8",
            )

    def tearDown(self):
        self.temporary.cleanup()

    def test_prepare_environment_preserves_secrets_comments_and_is_private(self):
        qbit_secret = "existing-qbit-secret"
        env_path = self.root / ".env"
        env_path.write_text(
            "# operator note\n"
            "UNRELATED=value\n"
            "QBITTORRENT_USERNAME=admin\n"
            f"QBITTORRENT_PASSWORD={qbit_secret}\n"
            "SONARR_API_KEY=stale\n",
            encoding="utf-8",
        )
        os.chmod(env_path, 0o644)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            environment, api_keys = bootstrap.prepare_environment(
                self.root,
                password_factory=lambda: self.fail("password was unexpectedly replaced"),
            )

        content = env_path.read_text(encoding="utf-8")
        self.assertIn("# operator note", content)
        self.assertIn("UNRELATED=value", content)
        self.assertEqual(environment["QBITTORRENT_PASSWORD"], qbit_secret)
        self.assertEqual(environment["SONARR_API_KEY"], "api-key-2")
        self.assertEqual(api_keys["WHISPARR_API_KEY"], "api-key-5")
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
        self.assertNotIn(qbit_secret, stdout.getvalue() + stderr.getvalue())
        self.assertFalse(list(self.root.glob("..env.*.tmp")))

    def test_missing_qbit_credentials_are_generated_only_once(self):
        calls = []

        def generate():
            calls.append(True)
            return "generated-password"

        first, _ = bootstrap.prepare_environment(
            self.root, password_factory=generate
        )
        second, _ = bootstrap.prepare_environment(
            self.root,
            password_factory=lambda: self.fail("generated a second password"),
        )
        self.assertEqual(calls, [True])
        self.assertEqual(first["QBITTORRENT_USERNAME"], "admin")
        self.assertEqual(first["QBITTORRENT_PASSWORD"], "generated-password")
        self.assertEqual(second["QBITTORRENT_PASSWORD"], "generated-password")

    def test_blank_qbit_credentials_are_treated_as_missing(self):
        (self.root / ".env").write_text(
            "QBITTORRENT_USERNAME=\nQBITTORRENT_PASSWORD=\n", encoding="utf-8"
        )
        environment, _ = bootstrap.prepare_environment(
            self.root, password_factory=lambda: "replacement-password"
        )
        self.assertEqual(environment["QBITTORRENT_USERNAME"], "admin")
        self.assertEqual(
            environment["QBITTORRENT_PASSWORD"], "replacement-password"
        )

    def test_download_directories_cover_base_categories_and_incomplete(self):
        torrent_root = self.root / "downloads"
        directories = bootstrap.ensure_download_directories(
            self.root, {"TORRENT_ROOT": str(torrent_root)}
        )
        expected = {*bootstrap.BASE_CATEGORIES, "incomplete"}
        self.assertEqual({path.name for path in directories}, expected)
        self.assertTrue(all(path.is_dir() for path in directories))


class FakeQbitConfigurationClient:
    def __init__(self):
        self.current_preferences = {
            "save_path": "/wrong",
            "temp_path_enabled": False,
            "temp_path": "/wrong/incomplete",
            "max_ratio_enabled": True,
            "max_seeding_time_enabled": True,
            "max_inactive_seeding_time_enabled": True,
        }
        self.current_categories = {
            "tv": {"name": "tv", "savePath": "/downloads/tv"},
            "movies": {"name": "movies", "savePath": "/wrong"},
        }
        self.preference_calls = []
        self.created = []
        self.edited = []

    def preferences(self):
        return copy.deepcopy(self.current_preferences)

    def set_preferences(self, values):
        self.preference_calls.append(copy.deepcopy(values))
        self.current_preferences.update(values)

    def categories(self):
        return copy.deepcopy(self.current_categories)

    def create_category(self, name, save_path):
        self.created.append((name, save_path))
        self.current_categories[name] = {"name": name, "savePath": save_path}

    def edit_category(self, name, save_path):
        self.edited.append((name, save_path))
        self.current_categories[name]["savePath"] = save_path


class QbittorrentTests(unittest.TestCase):
    def test_preferences_and_categories_are_exact_and_idempotent(self):
        client = FakeQbitConfigurationClient()
        preference_count, category_count = bootstrap.configure_qbittorrent(client)
        self.assertEqual(preference_count, 6)
        self.assertEqual(category_count, len(bootstrap.CATEGORIES) - 1)
        self.assertFalse(client.preference_calls[0]["max_ratio_enabled"])
        self.assertFalse(client.preference_calls[0]["max_seeding_time_enabled"])
        self.assertFalse(
            client.preference_calls[0]["max_inactive_seeding_time_enabled"]
        )
        self.assertNotIn("max_seeding_time", client.preference_calls[0])
        self.assertEqual(client.edited, [("movies", "/downloads/movies")])
        for name in bootstrap.CATEGORIES:
            base_name = name.removesuffix("-imported")
            self.assertEqual(
                client.current_categories[name]["savePath"], f"/downloads/{base_name}"
            )

        mutations = (
            len(client.preference_calls), len(client.created), len(client.edited)
        )
        self.assertEqual(bootstrap.configure_qbittorrent(client), (0, 0))
        self.assertEqual(
            mutations,
            (len(client.preference_calls), len(client.created), len(client.edited)),
        )

    def test_temporary_password_fallback_uses_most_recent_without_output(self):
        old_temporary = "old-temporary-secret"
        current_temporary = "current-temporary-secret"
        persisted = "persisted-secret"

        class InitialClient:
            def __init__(self):
                self.logins = []
                self.credentials = []

            def login(self, username, password):
                self.logins.append((username, password))
                return username == "admin" and password == current_temporary

            def set_web_credentials(self, username, password):
                self.credentials.append((username, password))

        class VerifiedClient:
            def __init__(self):
                self.logins = []

            def login(self, username, password):
                self.logins.append((username, password))
                return username == "admin" and password == persisted

        initial = InitialClient()
        verified = VerifiedClient()
        clients = iter((initial, verified))

        def factory(*args, **kwargs):
            return next(clients)

        logs = (
            "A temporary password is provided for this session: "
            f"{old_temporary}\n"
            "A temporary password is provided for this session: "
            f"{current_temporary}\n"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = bootstrap.authenticate_qbittorrent(
                "http://qbit.invalid",
                "admin",
                persisted,
                client_factory=factory,
                logs_reader=lambda: logs,
                sleep=lambda _: None,
            )
        self.assertIs(result, verified)
        self.assertEqual(initial.logins[-1], ("admin", current_temporary))
        self.assertEqual(initial.credentials, [("admin", persisted)])
        for secret in (old_temporary, current_temporary, persisted):
            self.assertNotIn(secret, output.getvalue())


class FakeArrApi:
    def __init__(self):
        self.resource = provider_resource()
        self.current_valid = False
        self.tests = []
        self.puts = []

    def get_json(self, path):
        if path.endswith("/downloadclient"):
            return [copy.deepcopy(self.resource)]
        if "/downloadclient/" in path:
            return copy.deepcopy(self.resource)
        raise AssertionError(path)

    def post_json(self, path, payload, retry=False):
        self.tests.append(copy.deepcopy(payload))
        password = bootstrap.get_provider_field(payload, "password")
        if password == "********" and not self.current_valid:
            raise bootstrap.ApiError(400, path, "Bad Request", b"wrong password")
        return {}

    def put_json(self, path, payload):
        self.puts.append((path, copy.deepcopy(payload)))
        self.resource = copy.deepcopy(payload)
        bootstrap.set_provider_field(self.resource, "password", "********")
        self.current_valid = True
        return {}


class ArrTests(unittest.TestCase):
    def test_rotation_guard_restarts_and_reauthenticates(self):
        original = mock.Mock()
        ready = mock.Mock()
        ready.login.return_value = True
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        result = bootstrap.restart_qbittorrent_with_rotation_guard(
            original,
            "http://qbit.invalid",
            "admin",
            "secret",
            timeout=1,
            retries=1,
            runner=runner,
            client_factory=lambda *args, **kwargs: ready,
            sleep=lambda _: None,
        )
        self.assertIs(result, ready)
        original.set_preferences.assert_called_once_with(
            {"web_ui_max_auth_fail_count": bootstrap.QBITTORRENT_ROTATION_GUARD_LIMIT}
        )
        runner.assert_called_once()
        ready.login.assert_called_once_with("admin", "secret")

    def test_download_client_repair_payload_and_mask_aware_idempotency(self):
        api = FakeArrApi()
        service = bootstrap.ARR_SERVICES[0]
        updates = bootstrap.configure_arr_service(
            api,
            service,
            "admin",
            "new-password",
            prefix="/api/v3",
        )
        self.assertEqual(updates, 1)
        self.assertEqual(len(api.puts), 1)
        payload = api.puts[0][1]
        self.assertEqual(bootstrap.get_provider_field(payload, "category"), "tv")
        self.assertEqual(
            bootstrap.get_provider_field(payload, "postImportCategory"),
            "tv-imported",
        )
        self.assertEqual(
            bootstrap.get_provider_field(payload, "password"), "new-password"
        )
        self.assertFalse(payload["removeCompletedDownloads"])

        self.assertEqual(
            bootstrap.configure_arr_service(
                api,
                service,
                "admin",
                "new-password",
                prefix="/api/v3",
            ),
            0,
        )
        self.assertEqual(len(api.puts), 1)

    def test_all_service_category_mappings(self):
        for service in bootstrap.ARR_SERVICES:
            payload = bootstrap.build_arr_download_client_payload(
                provider_resource(), service, "admin", "secret"
            )
            self.assertEqual(
                bootstrap.get_provider_field(payload, "category"), service.category
            )
            self.assertEqual(
                bootstrap.get_provider_field(payload, "postImportCategory"),
                f"{service.category}-imported",
            )

    def test_current_media_specific_category_field_mappings(self):
        expected_fields = {
            "Sonarr": ("tvCategory", "tvImportedCategory"),
            "Radarr": ("movieCategory", "movieImportedCategory"),
            "Lidarr": ("musicCategory", "musicImportedCategory"),
            "Whisparr": ("tvCategory", "tvImportedCategory"),
        }
        for service in bootstrap.ARR_SERVICES:
            category_field, imported_field = expected_fields[service.name]
            resource = provider_resource()
            resource["fields"] = [
                field
                for field in resource["fields"]
                if field["name"] not in {"category", "postImportCategory"}
            ]
            resource["fields"].extend(
                [
                    {"name": category_field, "value": "old"},
                    {"name": imported_field, "value": "old-imported"},
                ]
            )
            payload = bootstrap.build_arr_download_client_payload(
                resource, service, "admin", "secret"
            )
            self.assertEqual(
                bootstrap.get_provider_field(payload, category_field),
                service.category,
            )
            self.assertEqual(
                bootstrap.get_provider_field(payload, imported_field),
                f"{service.category}-imported",
            )


class RecordingReporter:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)


class FakeProwlarrApi:
    def __init__(self, successful_definitions=("nyaasi",)):
        self.schemas = [indexer_schema(spec) for spec in bootstrap.PUBLIC_INDEXERS]
        self.existing = []
        self.successful_definitions = set(successful_definitions)
        self.creates = []
        self.puts = []

    def get_json(self, path):
        if path == "/api/v1/indexer/schema":
            return copy.deepcopy(self.schemas)
        if path == "/api/v1/indexer":
            return copy.deepcopy(self.existing)
        if path == "/api/v1/appprofile":
            return [{"id": 1, "name": "Standard"}]
        raise AssertionError(path)

    def post_json(self, path, payload, retry=False):
        if path == "/api/v1/indexer/test":
            if payload["definitionName"] not in self.successful_definitions:
                body = json.dumps(
                    {"message": "site blocked; password=never-print-this"}
                ).encode()
                raise bootstrap.ApiError(400, path, "Bad Request", body)
            return {}
        if path == "/api/v1/indexer?forceSave=true":
            created = copy.deepcopy(payload)
            created["id"] = len(self.existing) + 1
            self.existing.append(created)
            self.creates.append(created)
            return created
        raise AssertionError(path)

    def put_json(self, path, payload):
        self.puts.append((path, copy.deepcopy(payload)))
        return payload


class ProwlarrTests(unittest.TestCase):
    def test_selected_indexer_payloads_fail_independently_and_are_idempotent(self):
        api = FakeProwlarrApi()
        reporter = RecordingReporter()
        successful = bootstrap.configure_prowlarr_indexers(
            api,
            reporter=reporter,
            secret_values=["never-print-this"],
        )
        self.assertEqual(successful, ["Nyaa.si"])
        self.assertEqual(len(api.creates), 1)
        created = api.creates[0]
        self.assertTrue(created["enable"])
        self.assertEqual(created["appProfileId"], 1)
        self.assertEqual(
            bootstrap.get_provider_field(created, "baseUrl"), "https://nyaa.si/"
        )
        self.assertEqual(
            len(reporter.warnings), len(bootstrap.PUBLIC_INDEXERS) - 1
        )
        self.assertNotIn("never-print-this", " ".join(reporter.warnings))
        self.assertIn("[redacted]", " ".join(reporter.warnings))

        self.assertEqual(
            bootstrap.configure_prowlarr_indexers(
                api,
                reporter=reporter,
                secret_values=["never-print-this"],
            ),
            ["Nyaa.si"],
        )
        self.assertEqual(len(api.creates), 1)
        self.assertEqual(api.puts, [])

    def test_requires_one_working_public_indexer(self):
        api = FakeProwlarrApi(successful_definitions=())
        with self.assertRaisesRegex(
            bootstrap.BootstrapError, "No selected public Prowlarr indexer"
        ):
            bootstrap.configure_prowlarr_indexers(
                api, reporter=RecordingReporter()
            )

    def test_application_validation_only_tests_existing_required_apps(self):
        class ApplicationsApi:
            def __init__(self):
                self.tests = []

            def get_json(self, path):
                self.assert_path = path
                return [
                    {"name": service.name, "implementation": service.name}
                    for service in bootstrap.ARR_SERVICES
                ]

            def post_json(self, path, payload, retry=False):
                self.tests.append((path, payload["name"]))
                return {}

        api = ApplicationsApi()
        bootstrap.validate_prowlarr_applications(api)
        self.assertEqual(len(api.tests), 4)
        self.assertTrue(
            all(path == "/api/v1/applications/test" for path, _ in api.tests)
        )


class BazarrTests(unittest.TestCase):
    def test_form_configures_integrations_english_defaults_and_free_providers(self):
        settings = {"general": {"enabled_providers": ["existing"]}}
        form, profile_id = bootstrap.build_bazarr_settings_form(
            settings,
            [],
            [
                {"code2": "fr", "enabled": True},
                {"code2": "en", "enabled": False},
            ],
            "sonarr-secret",
            "radarr-secret",
        )
        self.assertEqual(profile_id, 1)
        profiles = json.loads(form["languages-profiles"])
        self.assertEqual(len(profiles), 1)
        profile = profiles[0]
        self.assertEqual(profile["name"], "English")
        self.assertEqual(profile["cutoff"], profile["items"][0]["id"])
        self.assertEqual(profile["items"][0]["language"], "en")
        self.assertEqual(profile["items"][0]["hi"], "False")
        self.assertEqual(profile["items"][0]["forced"], "False")
        self.assertEqual(form["languages-enabled"], ["fr", "en"])
        self.assertEqual(form["settings-sonarr-ip"], "sonarr")
        self.assertEqual(form["settings-radarr-ip"], "radarr")
        self.assertEqual(form["settings-sonarr-apikey"], "sonarr-secret")
        self.assertEqual(form["settings-radarr-apikey"], "radarr-secret")
        self.assertEqual(
            form["settings-general-enabled_providers"],
            ["existing", *bootstrap.BAZARR_PROVIDERS],
        )
        self.assertEqual(
            form["settings-subf2m-user_agent"], bootstrap.BAZARR_USER_AGENT
        )
        self.assertEqual(form["settings-subf2m-verify_ssl"], "true")
        self.assertEqual(form["settings-general-serie_default_profile"], "1")
        self.assertEqual(form["settings-general-movie_default_profile"], "1")

    def test_matching_bazarr_state_produces_no_mutation(self):
        settings = {
            "general": {
                "use_sonarr": True,
                "use_radarr": True,
                "serie_default_enabled": True,
                "serie_default_profile": 1,
                "movie_default_enabled": True,
                "movie_default_profile": 1,
                "enabled_providers": list(bootstrap.BAZARR_PROVIDERS),
            },
            "sonarr": {
                "ip": "sonarr",
                "port": 8989,
                "base_url": "/",
                "ssl": False,
                "apikey": "sonarr-secret",
            },
            "radarr": {
                "ip": "radarr",
                "port": 7878,
                "base_url": "/",
                "ssl": False,
                "apikey": "radarr-secret",
            },
            "subf2m": {
                "user_agent": bootstrap.BAZARR_USER_AGENT,
                "verify_ssl": True,
            },
        }
        form, profile_id = bootstrap.build_bazarr_settings_form(
            settings,
            [bootstrap.english_language_profile(1)],
            [{"code2": "en", "enabled": True}],
            "sonarr-secret",
            "radarr-secret",
        )
        self.assertEqual(profile_id, 1)
        self.assertEqual(form, {})


class HttpAndSecretSafetyTests(unittest.TestCase):
    def test_get_retries_transient_transport_failure_with_mocked_opener(self):
        class Response:
            status = 200
            reason = "OK"

            def read(self):
                return b'{"ok":true}'

            def getcode(self):
                return self.status

            def close(self):
                pass

        class FlakyOpener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise urllib.error.URLError("offline")
                return Response()

        opener = FlakyOpener()
        sleeps = []
        client = bootstrap.ApiClient(
            "http://example.invalid",
            opener=opener,
            retries=2,
            sleep=sleeps.append,
        )
        self.assertEqual(client.get_json("/status"), {"ok": True})
        self.assertEqual(opener.calls, 2)
        self.assertEqual(sleeps, [1])

    def test_printable_api_error_redacts_known_secrets(self):
        secret = "do-not-print"
        body = json.dumps(
            {"validationFailures": [{"errorMessage": f"password={secret}"}]}
        ).encode()
        error = bootstrap.ApiError(400, "/test", "Bad Request", body)
        self.assertNotIn(secret, str(error))
        detail = bootstrap.safe_api_error_detail(error, [secret])
        self.assertNotIn(secret, detail)
        self.assertIn("[redacted]", detail)


if __name__ == "__main__":
    unittest.main()
