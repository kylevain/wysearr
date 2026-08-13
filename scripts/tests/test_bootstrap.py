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


def generic_newznab_schema():
    return {
        "id": 0,
        "name": "Generic Newznab",
        "definitionName": "Newznab",
        "implementation": "Newznab",
        "implementationName": "Newznab",
        "configContract": "NewznabSettings",
        "protocol": "usenet",
        "enable": False,
        "supportsRss": True,
        "supportsSearch": True,
        "appProfileId": 1,
        "priority": 25,
        "tags": [],
        "fields": [
            {"name": "baseUrl", "value": ""},
            {"name": "apiPath", "value": "/api"},
            {"name": "apiKey", "value": ""},
            {"name": "additionalParameters", "value": None},
        ],
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
        expected = {
            *(torrent_root / name for name in bootstrap.BASE_CATEGORIES),
            *(torrent_root / name for name in bootstrap.EXTRA_DOWNLOAD_DIRECTORIES),
        }
        self.assertEqual(set(directories), expected)
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
        self.assertEqual(category_count, len(bootstrap.CATEGORIES))
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
        self.assertEqual(
            client.current_categories[bootstrap.SHELFARR_DOWNLOAD_CATEGORY]["savePath"],
            "/downloads/shelfarr",
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


class FakeManagedNewznabApi:
    def __init__(
        self,
        *,
        tags=None,
        applications=None,
        existing=None,
        book_categories=True,
    ):
        self.tags = copy.deepcopy(tags or [])
        self.applications = copy.deepcopy(
            applications
            or [
                {
                    "id": 100 + index,
                    "name": service.name,
                    "implementation": service.name,
                    "tags": [],
                }
                for index, service in enumerate(bootstrap.ARR_SERVICES, start=1)
            ]
        )
        self.existing = copy.deepcopy(existing or [])
        self.schemas = [
            {
                "name": "Newznab",
                "presets": [
                    dict(generic_newznab_schema(), name="Provider-specific preset"),
                    generic_newznab_schema(),
                ],
            }
        ]
        self.creates = []
        self.puts = []
        self.tests = []
        self.tag_creates = []
        self.application_puts = []
        self.events = []
        self.fail_test_with = None
        self.book_categories = book_categories
        self.fail_tag_label = None

    def get_json(self, path):
        resource_suffix = path.removeprefix("/api/v1/indexer/")
        if resource_suffix.isdigit():
            resource_id = int(resource_suffix)
            return copy.deepcopy(
                next(item for item in self.existing if item.get("id") == resource_id)
            )
        application_suffix = path.removeprefix("/api/v1/applications/")
        if application_suffix.isdigit():
            resource_id = int(application_suffix)
            return copy.deepcopy(
                next(
                    item
                    for item in self.applications
                    if item.get("id") == resource_id
                )
            )
        values = {
            "/api/v1/tag": self.tags,
            "/api/v1/applications": self.applications,
            "/api/v1/indexer": self.existing,
            "/api/v1/indexer/schema": self.schemas,
            "/api/v1/appprofile": [{"id": 1, "name": "Standard"}],
        }
        if path not in values:
            raise AssertionError(path)
        return copy.deepcopy(values[path])

    def _mask_api_key(self, payload):
        stored = copy.deepcopy(payload)
        field = next(
            (
                item
                for item in stored.get("fields", [])
                if item.get("name") == "apiKey"
            ),
            None,
        )
        if field is not None:
            field["value"] = "********"
        if stored.get("implementation") == "Newznab":
            categories = (
                [
                    {
                        "id": 3000,
                        "name": "Audio",
                        "subCategories": [
                            {
                                "id": 3030,
                                "name": "Audio/Audiobook",
                                "subCategories": [],
                            }
                        ],
                    },
                    {
                        "id": 7000,
                        "name": "Books",
                        "subCategories": [
                            {
                                "id": 7020,
                                "name": "Books/EBook",
                                "subCategories": [],
                            }
                        ],
                    },
                ]
                if self.book_categories
                else [{"id": 2000, "name": "Movies", "subCategories": []}]
            )
            stored["capabilities"] = {"categories": categories}
        return stored

    def post_json(self, path, payload, retry=False):
        if path == "/api/v1/tag":
            if payload["label"] == self.fail_tag_label:
                raise bootstrap.ApiTransportError(path)
            next_id = max((item["id"] for item in self.tags), default=6) + 1
            created = {"id": next_id, "label": payload["label"]}
            self.tags.append(created)
            self.tag_creates.append(copy.deepcopy(payload))
            self.events.append(("tag", payload["label"]))
            return copy.deepcopy(created)
        if path == "/api/v1/indexer/test":
            self.tests.append(copy.deepcopy(payload))
            self.events.append(("test", payload.get("name")))
            if self.fail_test_with is not None:
                raise self.fail_test_with
            return {}
        if path == "/api/v1/indexer?forceSave=true":
            created = copy.deepcopy(payload)
            created["id"] = 41
            self.creates.append(created)
            self.existing.append(self._mask_api_key(created))
            self.events.append(("indexer-create", created["name"]))
            return copy.deepcopy(created)
        raise AssertionError(path)

    def put_json(self, path, payload):
        updated = copy.deepcopy(payload)
        self.puts.append((path, updated))
        resource_id = updated["id"]
        if path.startswith("/api/v1/applications/"):
            self.applications = [
                updated if item.get("id") == resource_id else item
                for item in self.applications
            ]
            self.application_puts.append((path, updated))
            self.events.append(("application-put", updated["name"]))
        else:
            self.existing = [
                self._mask_api_key(updated)
                if item.get("id") == resource_id
                else item
                for item in self.existing
            ]
            self.events.append(
                ("indexer-put", updated["name"], bool(updated.get("enable")))
            )
        return copy.deepcopy(updated)


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

    def test_managed_newznab_environment_contract_is_strict_and_private(self):
        disabled = bootstrap.managed_newznab_config({})
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.name, "WyseARR Books")
        self.assertFalse(
            bootstrap.managed_newznab_config(
                {"WYSEARR_USENET_ENABLED": "false"}
            ).enabled
        )

        for enabled in ("false", "true"):
            with self.subTest(custom_name_enabled=enabled), self.assertRaisesRegex(
                bootstrap.BootstrapError,
                "NEWZNAB_INDEXER_NAME must be exactly WyseARR Books",
            ):
                bootstrap.managed_newznab_config(
                    {
                        "WYSEARR_USENET_ENABLED": enabled,
                        "NEWZNAB_INDEXER_NAME": "Old Books Pilot",
                    }
                )

        for invalid_flag in ("yes", " true", "true ", "False"):
            with self.subTest(flag=invalid_flag), self.assertRaisesRegex(
                bootstrap.BootstrapError,
                "must be blank or literal true or false",
            ):
                bootstrap.managed_newznab_config(
                    {"WYSEARR_USENET_ENABLED": invalid_flag}
                )

        with self.assertRaisesRegex(
            bootstrap.BootstrapError,
            "requires SHELFARR_ENABLED=true",
        ):
            bootstrap.managed_newznab_config({"WYSEARR_USENET_ENABLED": "true"})

        with self.assertRaisesRegex(
            bootstrap.BootstrapError,
            "Required private setting is missing: NEWZNAB_BASE_URL",
        ):
            bootstrap.managed_newznab_config(
                {
                    "WYSEARR_USENET_ENABLED": "true",
                    "SHELFARR_ENABLED": "true",
                }
            )

        configured = bootstrap.managed_newznab_config(
            {
                "WYSEARR_USENET_ENABLED": "true",
                "SHELFARR_ENABLED": "true",
                "NEWZNAB_BASE_URL": "https://indexer.example/",
                "NEWZNAB_API_KEY": "private-newznab-key",
            }
        )
        self.assertEqual(
            configured,
            bootstrap.ManagedNewznabConfig(
                True,
                "WyseARR Books",
                "https://indexer.example",
                "private-newznab-key",
                "/api",
            ),
        )

        for bad_url in (
            "ftp://indexer.example",
            "https://user:secret@indexer.example",
            "https://indexer.example?apikey=secret",
            "https://[",
            "https://indexer.example\\redirect",
            "https://indexer example",
        ):
            with self.subTest(base_url=bad_url), self.assertRaisesRegex(
                bootstrap.BootstrapError, "NEWZNAB_BASE_URL"
            ):
                bootstrap.managed_newznab_config(
                    {
                        "WYSEARR_USENET_ENABLED": "true",
                        "SHELFARR_ENABLED": "true",
                        "NEWZNAB_BASE_URL": bad_url,
                        "NEWZNAB_API_KEY": "private-newznab-key",
                    }
                )

        for bad_path in (
            "api",
            "//other.example/api",
            "/api?apikey=secret",
            "/api#fragment",
            "/api\\redirect",
            "/api path",
        ):
            with self.subTest(api_path=bad_path), self.assertRaisesRegex(
                bootstrap.BootstrapError, "NEWZNAB_API_PATH"
            ):
                bootstrap.managed_newznab_config(
                    {
                        "WYSEARR_USENET_ENABLED": "true",
                        "SHELFARR_ENABLED": "true",
                        "NEWZNAB_BASE_URL": "https://indexer.example",
                        "NEWZNAB_API_KEY": "private-newznab-key",
                        "NEWZNAB_API_PATH": bad_path,
                    }
                )

    def test_managed_newznab_creates_exact_tagged_generic_and_is_idempotent(self):
        api = FakeManagedNewznabApi()
        config = bootstrap.managed_newznab_config(
            {
                "WYSEARR_USENET_ENABLED": "true",
                "SHELFARR_ENABLED": "true",
                "NEWZNAB_BASE_URL": "https://indexer.example",
                "NEWZNAB_API_KEY": "private-newznab-key",
            }
        )

        self.assertEqual(
            bootstrap.configure_managed_newznab(
                api, config, secret_values=[config.api_key]
            ),
            "created",
        )
        self.assertEqual(
            api.tag_creates,
            [{"label": "shelfarr"}, {"label": "wysearr-arr"}],
        )
        self.assertEqual(len(api.application_puts), len(bootstrap.ARR_SERVICES))
        for application in api.applications:
            self.assertIn(8, application["tags"])
            self.assertNotIn(7, application["tags"])
        self.assertEqual(len(api.creates), 1)
        created = api.creates[0]
        self.assertEqual(created["name"], "WyseARR Books")
        self.assertEqual(created["implementation"], "Newznab")
        self.assertEqual(created["configContract"], "NewznabSettings")
        self.assertEqual(created["protocol"], "usenet")
        self.assertTrue(created["enable"])
        self.assertEqual(created["priority"], 20)
        self.assertEqual(created["appProfileId"], 1)
        self.assertEqual(created["tags"], [7])
        self.assertEqual(
            bootstrap.get_provider_field(created, "baseUrl"),
            "https://indexer.example",
        )
        self.assertEqual(bootstrap.get_provider_field(created, "apiPath"), "/api")
        self.assertEqual(
            bootstrap.get_provider_field(created, "apiKey"),
            "private-newznab-key",
        )

        first_put_count = len(api.puts)
        self.assertEqual(
            bootstrap.configure_managed_newznab(
                api, config, secret_values=[config.api_key]
            ),
            "verified",
        )
        self.assertEqual(len(api.creates), 1)
        self.assertEqual(first_put_count, len(bootstrap.ARR_SERVICES))
        self.assertEqual(
            api.tag_creates,
            [{"label": "shelfarr"}, {"label": "wysearr-arr"}],
        )
        self.assertEqual(len(api.tests), 3)
        self.assertEqual(len(api.puts), first_put_count)

    def test_arr_isolation_quarantines_existing_managed_before_tag_transition(self):
        managed = generic_newznab_schema()
        managed.update(
            {
                "id": 42,
                "name": "WyseARR Books",
                "enable": True,
                "priority": 20,
                "tags": [8, 9, 12],
            }
        )
        bootstrap.set_provider_field(managed, "baseUrl", "https://indexer.example")
        bootstrap.set_provider_field(managed, "apiKey", "********")
        unrelated = dict(
            indexer_schema(bootstrap.PUBLIC_INDEXERS[0]),
            id=99,
            enable=True,
            tags=[11],
        )
        applications = [
            {
                "id": 100 + index,
                "name": service.name,
                "implementation": service.name,
                "tags": [12],
            }
            for index, service in enumerate(bootstrap.ARR_SERVICES, start=1)
        ]
        api = FakeManagedNewznabApi(
            tags=[
                {"id": 7, "label": "shelfarr"},
                {"id": 8, "label": "wysearr-arr"},
            ],
            applications=applications,
            existing=[managed, unrelated],
        )
        config = bootstrap.ManagedNewznabConfig(
            True,
            "WyseARR Books",
            "https://indexer.example",
            "private-newznab-key",
            "/api",
        )

        self.assertEqual(
            bootstrap.configure_managed_newznab(api, config),
            "updated",
        )
        indexer_puts = [event for event in api.events if event[0] == "indexer-put"]
        self.assertEqual(indexer_puts[0], ("indexer-put", "WyseARR Books", False))
        self.assertEqual(indexer_puts[-1], ("indexer-put", "WyseARR Books", True))
        first_application = next(
            index
            for index, event in enumerate(api.events)
            if event[0] == "application-put"
        )
        managed_reenable = max(
            index
            for index, event in enumerate(api.events)
            if event == ("indexer-put", "WyseARR Books", True)
        )
        unrelated_tagged = next(
            index
            for index, event in enumerate(api.events)
            if event == ("indexer-put", unrelated["name"], True)
        )
        self.assertLess(unrelated_tagged, first_application)
        self.assertLess(first_application, managed_reenable)
        self.assertEqual(api.existing[0]["tags"], [7, 9])
        self.assertEqual(api.existing[1]["tags"], [8, 11])
        for application in api.applications:
            self.assertEqual(application["tags"], [8, 12])

    def test_tag_creation_failure_leaves_existing_managed_newznab_quarantined(self):
        managed = generic_newznab_schema()
        managed.update(
            {
                "id": 42,
                "name": "WyseARR Books",
                "enable": True,
                "priority": 20,
                "tags": [7],
            }
        )
        bootstrap.set_provider_field(managed, "baseUrl", "https://indexer.example")
        bootstrap.set_provider_field(managed, "apiKey", "********")
        api = FakeManagedNewznabApi(
            tags=[{"id": 7, "label": "shelfarr"}],
            existing=[managed],
        )
        api.fail_tag_label = "wysearr-arr"
        config = bootstrap.ManagedNewznabConfig(
            True,
            "WyseARR Books",
            "https://indexer.example",
            "private-newznab-key",
            "/api",
        )

        with self.assertRaises(bootstrap.ApiTransportError):
            bootstrap.configure_managed_newznab(api, config)

        self.assertEqual(
            api.events,
            [("indexer-put", "WyseARR Books", False)],
        )
        self.assertFalse(api.existing[0]["enable"])
        self.assertEqual(api.application_puts, [])
        self.assertEqual(api.creates, [])

    def test_managed_newznab_repairs_only_the_exact_managed_resource(self):
        installed = generic_newznab_schema()
        installed.update(
            {
                "id": 42,
                "name": "WyseARR Books",
                "enable": False,
                "priority": 50,
                "tags": [8, 9],
            }
        )
        bootstrap.set_provider_field(installed, "baseUrl", "https://old.invalid")
        bootstrap.set_provider_field(installed, "apiPath", "/old-api")
        bootstrap.set_provider_field(installed, "apiKey", "********")
        unrelated = dict(
            indexer_schema(bootstrap.PUBLIC_INDEXERS[0]),
            id=99,
            enable=True,
            tags=[11],
        )
        api = FakeManagedNewznabApi(
            tags=[
                {"id": 7, "label": "shelfarr"},
                {"id": 8, "label": "wysearr-arr"},
            ],
            existing=[installed, unrelated],
        )
        config = bootstrap.ManagedNewznabConfig(
            True,
            "WyseARR Books",
            "https://indexer.example",
            "private-newznab-key",
            "/api",
        )

        self.assertEqual(
            bootstrap.configure_managed_newznab(api, config),
            "updated",
        )
        managed_puts = [
            item for item in api.puts if item[0].startswith("/api/v1/indexer/42")
        ]
        self.assertEqual(len(managed_puts), 1)
        path, updated = managed_puts[0]
        self.assertEqual(path, "/api/v1/indexer/42?forceSave=true")
        self.assertEqual(updated["tags"], [7, 9])
        self.assertEqual(updated["priority"], 20)
        self.assertTrue(updated["enable"])
        self.assertEqual(api.existing[1]["tags"], [8, 11])
        for application in api.applications:
            self.assertEqual(application["tags"], [8])

    def test_disabled_managed_newznab_disables_exact_name_and_absent_is_noop(self):
        installed = generic_newznab_schema()
        installed.update({"id": 42, "name": "WyseARR Books", "enable": True})
        unrelated = dict(
            indexer_schema(bootstrap.PUBLIC_INDEXERS[0]),
            id=99,
            enable=True,
            tags=[11],
        )
        api = FakeManagedNewznabApi(existing=[installed, unrelated])
        config = bootstrap.ManagedNewznabConfig(False, "WyseARR Books", "", "", "")

        self.assertEqual(
            bootstrap.configure_managed_newznab(api, config),
            "disabled",
        )
        self.assertEqual(len(api.puts), 1)
        self.assertFalse(api.puts[0][1]["enable"])
        self.assertEqual(api.existing[1], unrelated)
        self.assertTrue(all(not item["tags"] for item in api.applications))
        self.assertEqual(api.tests, [])

        absent = FakeManagedNewznabApi()
        self.assertEqual(
            bootstrap.configure_managed_newznab(absent, config),
            "absent",
        )
        self.assertEqual(absent.creates, [])
        self.assertEqual(absent.puts, [])
        self.assertEqual(absent.tag_creates, [])

    def test_disabled_mode_maintains_an_existing_arr_isolation_topology(self):
        installed = generic_newznab_schema()
        installed.update(
            {
                "id": 42,
                "name": "WyseARR Books",
                "enable": True,
                "tags": [7],
            }
        )
        unrelated = dict(
            indexer_schema(bootstrap.PUBLIC_INDEXERS[0]),
            id=99,
            enable=True,
            tags=[11],
        )
        applications = [
            {
                "id": 100 + index,
                "name": service.name,
                "implementation": service.name,
                "tags": [12],
            }
            for index, service in enumerate(bootstrap.ARR_SERVICES, start=1)
        ]
        api = FakeManagedNewznabApi(
            tags=[
                {"id": 7, "label": "shelfarr"},
                {"id": 8, "label": "wysearr-arr"},
            ],
            applications=applications,
            existing=[installed, unrelated],
        )
        config = bootstrap.ManagedNewznabConfig(
            False, "WyseARR Books", "", "", ""
        )

        self.assertEqual(
            bootstrap.configure_managed_newznab(api, config), "disabled"
        )
        self.assertEqual(
            api.events[0], ("indexer-put", "WyseARR Books", False)
        )
        self.assertEqual(api.existing[0]["tags"], [7])
        self.assertEqual(api.existing[1]["tags"], [8, 11])
        for application in api.applications:
            self.assertEqual(application["tags"], [8, 12])
        first_application = next(
            index
            for index, event in enumerate(api.events)
            if event[0] == "application-put"
        )
        unrelated_tagged = api.events.index(
            ("indexer-put", unrelated["name"], True)
        )
        self.assertLess(unrelated_tagged, first_application)

    def test_disabled_mode_rejects_partial_retained_topology_after_quarantine(self):
        installed = generic_newznab_schema()
        installed.update(
            {
                "id": 42,
                "name": "WyseARR Books",
                "enable": True,
                "tags": [7],
            }
        )
        api = FakeManagedNewznabApi(
            tags=[{"id": 7, "label": "shelfarr"}],
            existing=[installed],
        )
        config = bootstrap.ManagedNewznabConfig(
            False, "WyseARR Books", "", "", ""
        )

        with self.assertRaisesRegex(
            bootstrap.BootstrapError, "partial or conflicting retained"
        ):
            bootstrap.configure_managed_newznab(api, config)

        self.assertEqual(
            api.events, [("indexer-put", "WyseARR Books", False)]
        )
        self.assertFalse(api.existing[0]["enable"])
        self.assertEqual(api.application_puts, [])

    def test_unmanaged_shelfarr_tag_fails_before_any_tag_migration(self):
        orphan = dict(
            generic_newznab_schema(),
            id=77,
            name="Old Books Pilot",
            enable=False,
            tags=[7],
        )
        unrelated = dict(
            indexer_schema(bootstrap.PUBLIC_INDEXERS[0]),
            id=99,
            enable=True,
            tags=[],
        )
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                api = FakeManagedNewznabApi(
                    tags=[
                        {"id": 7, "label": "shelfarr"},
                        {"id": 8, "label": "wysearr-arr"},
                    ],
                    existing=[orphan, unrelated],
                )
                config = bootstrap.ManagedNewznabConfig(
                    enabled,
                    "WyseARR Books",
                    "https://indexer.example" if enabled else "",
                    "private-newznab-key" if enabled else "",
                    "/api" if enabled else "",
                )

                with self.assertRaisesRegex(
                    bootstrap.BootstrapError,
                    "unmanaged indexers carrying the shelfarr tag: Old Books Pilot",
                ):
                    bootstrap.configure_managed_newznab(api, config)

                self.assertEqual(api.puts, [])
                self.assertEqual(api.application_puts, [])
                self.assertEqual(api.tag_creates, [])

    def test_configure_rejects_noncanonical_managed_name_before_api_access(self):
        api = FakeManagedNewznabApi()
        with self.assertRaisesRegex(
            bootstrap.BootstrapError,
            "NEWZNAB_INDEXER_NAME must be exactly WyseARR Books",
        ):
            bootstrap.configure_managed_newznab(
                api,
                bootstrap.ManagedNewznabConfig(
                    False, "Old Books Pilot", "", "", ""
                ),
            )
        self.assertEqual(api.events, [])

    def test_managed_newznab_fails_before_indexer_mutation_if_arr_has_tag(self):
        applications = [
            {
                "id": 100 + index,
                "name": service.name,
                "implementation": service.name,
                "tags": [7] if service.name == "Radarr" else [],
            }
            for index, service in enumerate(bootstrap.ARR_SERVICES, start=1)
        ]
        api = FakeManagedNewznabApi(
            tags=[
                {"id": 7, "label": "shelfarr"},
                {"id": 8, "label": "wysearr-arr"},
            ],
            applications=applications,
        )
        config = bootstrap.ManagedNewznabConfig(
            True,
            "WyseARR Books",
            "https://indexer.example",
            "private-newznab-key",
            "/api",
        )

        with self.assertRaisesRegex(
            bootstrap.BootstrapError,
            "ARR applications must not carry the shelfarr tag: Radarr",
        ):
            bootstrap.configure_managed_newznab(api, config)
        self.assertEqual(api.tests, [])
        self.assertEqual(api.creates, [])
        self.assertEqual(api.puts, [])

    def test_managed_newznab_rejects_name_collision_and_redacts_test_error(self):
        collision = dict(
            indexer_schema(bootstrap.PUBLIC_INDEXERS[0]),
            id=42,
            name="WyseARR Books",
        )
        config = bootstrap.ManagedNewznabConfig(
            True,
            "WyseARR Books",
            "https://indexer.example",
            "private-newznab-key",
            "/api",
        )
        with self.assertRaisesRegex(
            bootstrap.BootstrapError,
            "is not a Generic Newznab resource",
        ):
            bootstrap.configure_managed_newznab(
                FakeManagedNewznabApi(existing=[collision]), config
            )

        api = FakeManagedNewznabApi(
            tags=[
                {"id": 7, "label": "shelfarr"},
                {"id": 8, "label": "wysearr-arr"},
            ]
        )
        body = json.dumps(
            {"message": f"invalid apiKey={config.api_key}"}
        ).encode()
        api.fail_test_with = bootstrap.ApiError(
            400, "/api/v1/indexer/test", "Bad Request", body
        )
        with self.assertRaises(bootstrap.BootstrapError) as raised:
            bootstrap.configure_managed_newznab(
                api, config, secret_values=[config.api_key]
            )
        self.assertNotIn(config.api_key, str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))
        self.assertEqual(api.creates, [])

    def test_managed_newznab_requires_ebook_and_audiobook_categories(self):
        api = FakeManagedNewznabApi(book_categories=False)
        config = bootstrap.ManagedNewznabConfig(
            True,
            "WyseARR Books",
            "https://indexer.example",
            "private-newznab-key",
            "/api",
        )
        with self.assertRaisesRegex(
            bootstrap.BootstrapError,
            "does not advertise ebook and audiobook categories",
        ):
            bootstrap.configure_managed_newznab(api, config)


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
    def test_dotenv_preserves_raw_usenet_feature_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "WYSEARR_USENET_ENABLED= true \nOTHER=value\n",
                encoding="utf-8",
            )
            values = bootstrap.load_dotenv(path)
        self.assertEqual(values["WYSEARR_USENET_ENABLED"], " true ")
        self.assertEqual(values["OTHER"], "value")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.managed_newznab_config(values)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "WYSEARR_USENET_ENABLED=true\n"
                "WYSEARR_USENET_ENABLED=true\n",
                encoding="utf-8",
            )
            duplicate = bootstrap.load_dotenv(path)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.managed_newznab_config(duplicate)

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
