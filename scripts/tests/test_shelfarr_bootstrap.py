import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bootstrap_shelfarr.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("bootstrap_shelfarr", SCRIPT)
bootstrap_shelfarr = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bootstrap_shelfarr)


class ShelfarrBootstrapTests(unittest.TestCase):
    def sab_config(self, directory: str) -> Path:
        path = Path(directory) / "sabnzbd.ini"
        path.write_text(
            "[misc]\napi_key = private-key\n",
            encoding="utf-8",
        )
        return path

    def usenet_environment(self, **updates):
        environment = {
            "WYSEARR_USENET_ENABLED": "true",
            "USENET_SERVER_HOST": "News.Example",
            "USENET_SERVER_USERNAME": "reader",
            "USENET_SERVER_PASSWORD": "private-provider-password",
            "USENET_SERVER_CONNECTIONS": "12",
        }
        environment.update(updates)
        return environment

    def sab_server_echo(self, settings, *, enabled=True, password="********"):
        return {
            "config": {
                "servers": [
                    {
                        "name": bootstrap_shelfarr.MANAGED_USENET_SERVER_NAME,
                        "displayname": bootstrap_shelfarr.MANAGED_USENET_SERVER_NAME,
                        "host": settings.host,
                        "port": settings.port,
                        "username": settings.username,
                        "password": password,
                        "connections": settings.connections,
                        "ssl": settings.ssl,
                        "ssl_verify": 3,
                        "enable": enabled,
                        "retention": settings.retention,
                        "priority": 0,
                    }
                ]
            }
        }

    def test_sab_api_credentials_use_post_body_not_url(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"status":true}'

        with patch.object(
            bootstrap_shelfarr.urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as opener:
            response = bootstrap_shelfarr._sab_request(
                8085,
                "private-api-key",
                {"mode": "version"},
            )

        request = opener.call_args.args[0]
        self.assertEqual(response, {"status": True})
        self.assertEqual(request.full_url, "http://127.0.0.1:8085/api")
        self.assertEqual(request.get_method(), "POST")
        self.assertNotIn("private-api-key", request.full_url)
        self.assertEqual(
            bootstrap_shelfarr.urllib.parse.parse_qs(request.data.decode("utf-8")),
            {
                "output": ["json"],
                "apikey": ["private-api-key"],
                "mode": ["version"],
            },
        )

    def test_sab_convergence_sets_isolated_paths_and_category(self):
        calls = []

        def requester(port, key, parameters):
            calls.append((port, key, parameters))
            if parameters["mode"] == "version":
                return {"version": "5.0.4"}
            if parameters["mode"] == "get_cats":
                return {"categories": ["*", "shelfarr"]}
            if parameters.get("section") == "categories":
                return {
                    "config": {
                        "categories": [
                            {
                                "name": "shelfarr",
                                "dir": "shelfarr",
                                "pp": "3",
                                "priority": 0,
                            }
                        ]
                    }
                }
            keyword = parameters["keyword"]
            value = parameters["value"]
            if keyword == "password":
                echo = "**********"
            elif keyword == "api_logging":
                echo = False
            elif keyword == "host_whitelist":
                echo = ["sabnzbd"]
            else:
                echo = value
            path = current_path
            text = path.read_text(encoding="utf-8")
            replacement = value
            if keyword == "password":
                replacement = "encoded-private-password"
            if f"{keyword} =" in text:
                lines = [
                    f"{keyword} = {replacement}"
                    if line.startswith(f"{keyword} =")
                    else line
                    for line in text.splitlines()
                ]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            else:
                path.write_text(
                    text.rstrip("\n") + f"\n{keyword} = {replacement}\n",
                    encoding="utf-8",
                )
            return {"config": {"misc": {keyword: echo}}}

        with tempfile.TemporaryDirectory() as directory:
            current_path = self.sab_config(directory)
            key = bootstrap_shelfarr.configure_sabnzbd(
                current_path,
                8085,
                "operator",
                "private-password",
                requester=requester,
            )

        self.assertEqual(key, "private-key")
        writes = [item[2] for item in calls if item[2]["mode"] == "set_config"]
        self.assertIn(
            {
                "mode": "set_config",
                "section": "misc",
                "keyword": "download_dir",
                "value": "/downloads/incomplete/usenet",
            },
            writes,
        )
        self.assertTrue(
            any(
                item.get("section") == "categories"
                and item.get("name") == "shelfarr"
                for item in writes
            )
        )
        self.assertTrue(
            any(
                item.get("keyword") == "username" and item.get("value") == "operator"
                for item in writes
            )
        )
        self.assertTrue(
            any(
                item.get("keyword") == "password"
                and item.get("value") == "private-password"
                for item in writes
            )
        )

    def test_sab_convergence_rejects_mismatched_nested_echo(self):
        def requester(_port, _key, parameters):
            keyword = parameters.get("keyword", "")
            return {"config": {"misc": {keyword: "wrong"}}}

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                bootstrap_shelfarr.BootstrapError,
                "rejected configuration key: api_logging",
            ):
                bootstrap_shelfarr.configure_sabnzbd(
                    self.sab_config(directory),
                    8085,
                    "operator",
                    "private-password",
                    requester=requester,
                )

    def test_managed_usenet_flag_is_strict_and_blank_is_disabled(self):
        blank = bootstrap_shelfarr.parse_managed_usenet_settings({})
        disabled = bootstrap_shelfarr.parse_managed_usenet_settings(
            {"WYSEARR_USENET_ENABLED": "false"}
        )
        self.assertIs(blank.enabled, False)
        self.assertIs(disabled.enabled, False)

        for value in ("TRUE", "False", "1", "yes", " true "):
            with self.subTest(value=value), self.assertRaisesRegex(
                bootstrap_shelfarr.BootstrapError,
                "WYSEARR_USENET_ENABLED must be literal",
            ):
                bootstrap_shelfarr.parse_managed_usenet_settings(
                    {"WYSEARR_USENET_ENABLED": value}
                )

    def test_enabled_managed_usenet_requires_every_private_setting(self):
        for name in (
            "USENET_SERVER_HOST",
            "USENET_SERVER_USERNAME",
            "USENET_SERVER_PASSWORD",
            "USENET_SERVER_CONNECTIONS",
        ):
            with self.subTest(name=name):
                environment = self.usenet_environment()
                environment.pop(name)
                with self.assertRaisesRegex(
                    bootstrap_shelfarr.BootstrapError, name
                ):
                    bootstrap_shelfarr.parse_managed_usenet_settings(environment)

    def test_enabled_managed_usenet_validates_numeric_and_boolean_values(self):
        malformed = (
            ("USENET_SERVER_CONNECTIONS", "not-a-number"),
            ("USENET_SERVER_CONNECTIONS", "0"),
            ("USENET_SERVER_CONNECTIONS", "501"),
            ("USENET_SERVER_PORT", "not-a-number"),
            ("USENET_SERVER_PORT", "0"),
            ("USENET_SERVER_PORT", "65536"),
            ("USENET_SERVER_SSL", "yes"),
            ("USENET_SERVER_SSL", "false"),
            ("USENET_SERVER_RETENTION", "not-a-number"),
            ("USENET_SERVER_RETENTION", "-1"),
        )
        for name, value in malformed:
            with self.subTest(name=name, value=value), self.assertRaisesRegex(
                bootstrap_shelfarr.BootstrapError, name
            ):
                bootstrap_shelfarr.parse_managed_usenet_settings(
                    self.usenet_environment(**{name: value})
                )

    def test_enabled_managed_usenet_applies_safe_defaults(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings(
            self.usenet_environment()
        )
        self.assertIs(settings.enabled, True)
        self.assertEqual(settings.host, "news.example")
        self.assertEqual(settings.port, 563)
        self.assertEqual(settings.connections, 12)
        self.assertIs(settings.ssl, True)
        self.assertEqual(settings.retention, 0)
        self.assertNotIn("private-provider-password", repr(settings))

    def test_enabled_managed_usenet_accepts_explicit_optional_values(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings(
            self.usenet_environment(
                USENET_SERVER_PORT="443",
                USENET_SERVER_SSL="true",
                USENET_SERVER_RETENTION="4500",
            )
        )
        self.assertEqual(settings.port, 443)
        self.assertIs(settings.ssl, True)
        self.assertEqual(settings.retention, 4500)

    def test_managed_usenet_tests_before_server_mutation_and_masks_echo(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings(
            self.usenet_environment()
        )
        calls = []

        def requester(port, key, parameters):
            calls.append((port, key, parameters))
            if parameters["mode"] == "get_config":
                return {"config": {"servers": []}}
            if parameters["mode"] == "config":
                return {
                    "value": {"result": True, "message": "Connection Successful"},
                }
            return self.sab_server_echo(settings)

        bootstrap_shelfarr.configure_managed_usenet_provider(
            settings,
            8085,
            "sab-private-key",
            requester=requester,
        )

        self.assertEqual(
            [call[2]["mode"] for call in calls],
            ["get_config", "config", "set_config"],
        )
        test_call = calls[1][2]
        self.assertEqual(test_call["name"], "test_server")
        self.assertEqual(test_call["ssl_verify"], "3")
        self.assertEqual(test_call["password"], "private-provider-password")
        write = calls[2][2]
        self.assertEqual(write["keyword"], "WyseARR Primary")
        self.assertEqual(write["ssl"], "1")
        self.assertEqual(write["ssl_verify"], "3")
        self.assertEqual(write["enable"], "1")
        self.assertEqual(write["priority"], "0")

    def test_failed_managed_usenet_connection_never_mutates_or_leaks(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings(
            self.usenet_environment()
        )
        calls = []

        def requester(_port, _key, parameters):
            calls.append(parameters)
            if parameters["mode"] == "get_config":
                return {"config": {"servers": []}}
            return {
                "value": {
                    "result": False,
                    "message": "private-provider-password news.example reader",
                },
            }

        with self.assertRaises(bootstrap_shelfarr.BootstrapError) as caught:
            bootstrap_shelfarr.configure_managed_usenet_provider(
                settings,
                8085,
                "sab-private-key",
                requester=requester,
            )

        self.assertEqual([call["mode"] for call in calls], ["get_config", "config"])
        self.assertNotIn("set_config", [call["mode"] for call in calls])
        for private_value in (
            "private-provider-password",
            "news.example",
            "reader",
        ):
            self.assertNotIn(private_value, str(caught.exception))

    def test_managed_usenet_rejects_unmasked_or_mismatched_echo(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings(
            self.usenet_environment()
        )
        for response in (
            self.sab_server_echo(
                settings, password="private-provider-password"
            ),
            self.sab_server_echo(settings) | {"config": {"servers": []}},
            self.sab_server_echo(settings) | {"status": False},
        ):
            with self.subTest(response=response):
                self.assertFalse(
                    bootstrap_shelfarr._sab_server_write_confirmed(
                        response, settings, enabled=True
                    )
                )

    def test_managed_usenet_rejects_duplicate_casefold_identity(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings(
            self.usenet_environment()
        )

        def requester(_port, _key, _parameters):
            return {
                "config": {
                    "servers": [
                        {"name": "WyseARR Primary", "enable": True},
                        {"name": "wysearr primary", "enable": False},
                    ]
                }
            }

        with self.assertRaisesRegex(
            bootstrap_shelfarr.BootstrapError,
            "duplicate managed Usenet servers",
        ):
            bootstrap_shelfarr.configure_managed_usenet_provider(
                settings, 8085, "sab-private-key", requester=requester
            )

    def test_disabled_managed_usenet_is_idempotent_and_absence_is_noop(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings(
            {"WYSEARR_USENET_ENABLED": "false"}
        )
        for existing_enabled, expected_modes in (
            (None, ["get_config"]),
            (False, ["get_config"]),
            (True, ["get_config", "set_config"]),
        ):
            with self.subTest(existing_enabled=existing_enabled):
                calls = []

                def requester(_port, _key, parameters):
                    calls.append(parameters)
                    if parameters["mode"] == "get_config":
                        servers = []
                        if existing_enabled is not None:
                            servers = [
                                {
                                    "name": bootstrap_shelfarr.MANAGED_USENET_SERVER_NAME,
                                    "enable": existing_enabled,
                                    "password": "********",
                                }
                            ]
                        return {"config": {"servers": servers}}
                    return self.sab_server_echo(settings, enabled=False)

                bootstrap_shelfarr.configure_managed_usenet_provider(
                    settings,
                    8085,
                    "sab-private-key",
                    requester=requester,
                )
                self.assertEqual(
                    [call["mode"] for call in calls], expected_modes
                )
                if existing_enabled is True:
                    self.assertEqual(calls[1]["enable"], "0")

    def test_disabled_managed_usenet_accepts_sab_omitted_empty_server_key(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings({})
        calls = []

        def requester(_port, _key, parameters):
            calls.append(parameters)
            return {"config": {}}

        bootstrap_shelfarr.configure_managed_usenet_provider(
            settings,
            8085,
            "sab-private-key",
            requester=requester,
        )
        self.assertEqual([call["mode"] for call in calls], ["get_config"])

    def test_managed_usenet_rejects_malformed_server_responses(self):
        for payload in (
            {},
            {"config": None},
            {"config": {"unexpected": 123}},
            {"config": {"misc": {}}},
            {"config": {"servers": None}},
            {"config": {"servers": {}}},
            {"config": {"servers": ["not-a-record"]}},
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                bootstrap_shelfarr.BootstrapError,
                "invalid server configuration",
            ):
                bootstrap_shelfarr._sab_server_list(payload)

    def test_blank_managed_usenet_setting_disables_only_the_managed_server(self):
        settings = bootstrap_shelfarr.parse_managed_usenet_settings({})
        calls = []

        def requester(_port, _key, parameters):
            calls.append(parameters)
            return {"config": {"servers": []}}

        bootstrap_shelfarr.configure_managed_usenet_provider(
            settings,
            8085,
            "sab-private-key",
            requester=requester,
        )
        self.assertEqual([call["mode"] for call in calls], ["get_config"])

    def test_bootstrap_converges_provider_after_private_sab_configuration(self):
        environment = self.usenet_environment(
            SABNZBD_ADMIN_PORT="8085",
            SABNZBD_ADMIN_USERNAME="operator",
            SABNZBD_ADMIN_PASSWORD="sab-admin-secret",
            PROWLARR_API_KEY="prowlarr-secret",
            QBITTORRENT_USERNAME="qbit-user",
            QBITTORRENT_PASSWORD="qbit-secret",
            SHELFARR_ADMIN_USERNAME="shelfarr-admin",
            SHELFARR_ADMIN_PASSWORD="shelfarr-secret",
            SHELFARR_ENABLED="true",
        )
        order = []

        def configure_base(*_args, **_kwargs):
            order.append("base")
            return "sab-private-key"

        def configure_provider(settings, port, api_key):
            order.append("provider")
            self.assertIs(settings.enabled, True)
            self.assertEqual(port, 8085)
            self.assertEqual(api_key, "sab-private-key")

        def converge(*_args, **_kwargs):
            order.append("shelfarr")
            return {
                "huey_token": "shf_generated",
                "huey_token_reused": False,
                "settings_count": 24,
                "download_clients": ["WyseARR SABnzbd"],
            }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            bootstrap_shelfarr, "load_dotenv", return_value=environment
        ), patch.object(
            bootstrap_shelfarr, "configure_sabnzbd", side_effect=configure_base
        ), patch.object(
            bootstrap_shelfarr, "require_drained_bookbot_book_categories"
        ), patch.object(
            bootstrap_shelfarr,
            "configure_managed_usenet_provider",
            side_effect=configure_provider,
        ), patch.object(
            bootstrap_shelfarr, "converge_shelfarr", side_effect=converge
        ), patch.object(
            bootstrap_shelfarr,
            "update_dotenv",
            side_effect=lambda _path, updates: environment | updates,
        ):
            bootstrap_shelfarr.bootstrap_shelfarr(Path(directory))

        self.assertEqual(order, ["base", "provider", "shelfarr"])

    def test_malformed_provider_contract_fails_before_sab_mutation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            bootstrap_shelfarr,
            "load_dotenv",
            return_value={"WYSEARR_USENET_ENABLED": "yes"},
        ), patch.object(bootstrap_shelfarr, "configure_sabnzbd") as configure_base, patch.object(
            bootstrap_shelfarr, "update_dotenv"
        ) as update_environment:
            with self.assertRaisesRegex(
                bootstrap_shelfarr.BootstrapError,
                "WYSEARR_USENET_ENABLED must be literal",
            ):
                bootstrap_shelfarr.bootstrap_shelfarr(Path(directory))

        configure_base.assert_not_called()
        update_environment.assert_not_called()

    def test_enabled_usenet_requires_shelfarr_ownership_before_mutation(self):
        environment = self.usenet_environment(SHELFARR_ENABLED="false")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            bootstrap_shelfarr, "load_dotenv", return_value=environment
        ), patch.object(
            bootstrap_shelfarr, "configure_sabnzbd"
        ) as configure_base, patch.object(
            bootstrap_shelfarr, "configure_managed_usenet_provider"
        ) as configure_provider:
            with self.assertRaisesRegex(
                bootstrap_shelfarr.BootstrapError,
                "requires SHELFARR_ENABLED=true",
            ):
                bootstrap_shelfarr.bootstrap_shelfarr(Path(directory))

        configure_base.assert_not_called()
        configure_provider.assert_not_called()

    def test_usenet_only_mode_disables_managed_provider_without_shelfarr(self):
        environment = {
            "WYSEARR_USENET_ENABLED": "false",
            "SHELFARR_ENABLED": "false",
            "SABNZBD_ADMIN_PORT": "8085",
            "SABNZBD_API_KEY": "sab-private-key",
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            bootstrap_shelfarr, "load_dotenv", return_value=environment
        ), patch.object(
            bootstrap_shelfarr, "configure_managed_usenet_provider"
        ) as configure_provider, patch.object(
            bootstrap_shelfarr, "configure_sabnzbd"
        ) as configure_base, patch.object(
            bootstrap_shelfarr, "converge_shelfarr"
        ) as converge_shelfarr:
            enabled = bootstrap_shelfarr.converge_managed_usenet_only(
                Path(directory)
            )

        self.assertFalse(enabled)
        settings, port, api_key = configure_provider.call_args.args
        self.assertFalse(settings.enabled)
        self.assertEqual(port, 8085)
        self.assertEqual(api_key, "sab-private-key")
        configure_base.assert_not_called()
        converge_shelfarr.assert_not_called()

    def test_usenet_only_cli_mode_is_mutually_exclusive(self):
        arguments = bootstrap_shelfarr.parse_args(
            ["--converge-usenet-only"]
        )
        self.assertTrue(arguments.converge_usenet_only)
        with patch.object(
            bootstrap_shelfarr,
            "converge_managed_usenet_only",
            return_value=False,
        ) as converge:
            self.assertEqual(
                bootstrap_shelfarr.main(["--converge-usenet-only"]), 0
            )
        converge.assert_called_once()
        self.assertEqual(
            bootstrap_shelfarr.main(
                ["--converge-usenet-only", "--check-drain-only"]
            ),
            1,
        )

    def test_prepare_sab_config_disables_api_logging_while_preserving_ini(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sabnzbd.ini"
            path.write_text(
                "[misc]\n"
                "api_key = private-key\n"
                "api_logging = 1\n"
                "username = operator\n"
                "[servers]\n"
                "[[news]]\n"
                "host = news.example\n",
                encoding="utf-8",
            )
            path.chmod(0o640)

            bootstrap_shelfarr.prepare_sabnzbd_private_config(path)

            content = path.read_text(encoding="utf-8")
            self.assertIn("api_logging = 0\n", content)
            self.assertNotIn("api_logging = 1", content)
            self.assertIn("host = news.example", content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual([item.name for item in path.parent.iterdir()], [path.name])

    def test_prepare_sab_config_quarantines_managed_provider_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sabnzbd.ini"
            path.write_text(
                "[misc]\n"
                "api_key = private-key\n"
                "api_logging = 1\n"
                "[servers]\n"
                "[[Unrelated Provider]]\n"
                "enable = 1\n"
                "password = unrelated-secret\n"
                "[[wysearr primary]]\n"
                "enable = 1\n"
                "password = managed-secret\n"
                "[categories]\n"
                "[[shelfarr]]\n"
                "name = shelfarr\n",
                encoding="utf-8",
            )
            path.chmod(0o640)

            bootstrap_shelfarr.prepare_sabnzbd_private_config(path)
            first = path.read_text(encoding="utf-8")
            bootstrap_shelfarr.prepare_sabnzbd_private_config(path)

            self.assertEqual(path.read_text(encoding="utf-8"), first)
            self.assertIn("[[Unrelated Provider]]\nenable = 1", first)
            self.assertIn("password = unrelated-secret", first)
            self.assertIn("[[wysearr primary]]\nenable = 0", first)
            self.assertIn("password = managed-secret", first)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual([item.name for item in path.parent.iterdir()], [path.name])

    def test_prepare_sab_config_inserts_missing_managed_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sabnzbd.ini"
            path.write_text(
                "[misc]\napi_key = private-key\n"
                "[servers]\n[[WyseARR Primary]]\nhost = news.example\n"
                "[[Other]]\nenable = 1\n",
                encoding="utf-8",
            )

            bootstrap_shelfarr.prepare_sabnzbd_private_config(path)

            content = path.read_text(encoding="utf-8")
            self.assertIn(
                "[[WyseARR Primary]]\nhost = news.example\nenable = 0\n[[Other]]",
                content,
            )

    def test_prepare_sab_config_rejects_ambiguous_managed_sections_atomically(self):
        fixtures = (
            "[misc]\napi_logging = 1\n[servers]\n"
            "[[WyseARR Primary]]\nenable = 1\n"
            "[[wysearr primary]]\nenable = 0\n",
            "[misc]\napi_logging = 1\n[servers]\n"
            "[[WyseARR Primary]]\nenable = 1\nenable = 0\n",
            "[misc]\napi_logging = 1\n[categories]\n"
            "[[WyseARR Primary]]\nenable = 1\n",
        )
        for content in fixtures:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sabnzbd.ini"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(bootstrap_shelfarr.BootstrapError):
                    bootstrap_shelfarr.prepare_sabnzbd_private_config(path)
                self.assertEqual(path.read_text(encoding="utf-8"), content)
                self.assertEqual([item.name for item in path.parent.iterdir()], [path.name])

    def test_prepare_sab_config_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.ini"
            target.write_text("[misc]\napi_logging = 1\n", encoding="utf-8")
            path = Path(directory) / "sabnzbd.ini"
            path.symlink_to(target)
            with self.assertRaises(bootstrap_shelfarr.BootstrapError):
                bootstrap_shelfarr.prepare_sabnzbd_private_config(path)
            self.assertIn("api_logging = 1", target.read_text(encoding="utf-8"))

    def test_prepare_sab_config_adds_api_logging_inside_misc_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sabnzbd.ini"
            path.write_text(
                "[misc]\napi_key = private-key\n[servers]\n",
                encoding="utf-8",
            )

            bootstrap_shelfarr.prepare_sabnzbd_private_config(path)

            content = path.read_text(encoding="utf-8")
            self.assertLess(content.index("api_logging = 0"), content.index("[servers]"))
            self.assertEqual(content.count("api_logging = 0"), 1)

    def test_enable_blocks_every_base_and_imported_bookbot_category(self):
        environment = {
            "QBITTORRENT_PORT": "8080",
            "WYSEARR_BIND_ADDRESS": "192.0.2.10",
            "QBITTORRENT_USERNAME": "admin",
            "QBITTORRENT_PASSWORD": "secret",
        }

        for blocking_category in bootstrap_shelfarr.BOOKBOT_BOOK_CATEGORIES:
            with self.subTest(category=blocking_category):
                urls = []
                categories = []

                class FakeClient:
                    def __init__(self, *_args, **_kwargs):
                        urls.append(_args[0])

                    def login(self, _username, _password):
                        return True

                    def torrents(self, category):
                        categories.append(category)
                        return (
                            [{"hash": "abc"}]
                            if category == blocking_category
                            else []
                        )

                with self.assertRaisesRegex(
                    bootstrap_shelfarr.BootstrapError, blocking_category
                ):
                    bootstrap_shelfarr.require_drained_bookbot_book_categories(
                        environment,
                        client_factory=FakeClient,
                    )
                self.assertEqual(urls, ["http://192.0.2.10:8080"])
                self.assertEqual(
                    categories, list(bootstrap_shelfarr.BOOKBOT_BOOK_CATEGORIES)
                )

    def test_enable_accepts_only_fully_drained_bookbot_categories(self):
        categories = []

        class EmptyClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def login(self, _username, _password):
                return True

            def torrents(self, category):
                categories.append(category)
                return []

        bootstrap_shelfarr.require_drained_bookbot_book_categories(
            {
                "QBITTORRENT_PORT": "8080",
                "WYSEARR_BIND_ADDRESS": "192.0.2.10",
                "QBITTORRENT_USERNAME": "admin",
                "QBITTORRENT_PASSWORD": "secret",
            },
            client_factory=EmptyClient,
        )
        self.assertEqual(
            categories, list(bootstrap_shelfarr.BOOKBOT_BOOK_CATEGORIES)
        )

    def test_active_record_convergence_uses_private_exec_streams(self):
        environment = {
            "PROWLARR_API_KEY": "prowlarr-secret",
            "QBITTORRENT_USERNAME": "admin",
            "QBITTORRENT_PASSWORD": "qbit-secret",
            "SHELFARR_ADMIN_USERNAME": "operator",
            "SHELFARR_ADMIN_PASSWORD": "Admin-password-1",
            "SHELFARR_API_TOKEN": "",
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config" / "shelfarr").mkdir(parents=True)

            def runner(command, **kwargs):
                self.assertNotIn("prowlarr-secret", " ".join(command))
                payload = json.loads(kwargs["input"])
                self.assertEqual(payload["sabnzbd_api_key"], "sab-secret")
                self.assertFalse(payload["usenet_enabled"])
                self.assertNotIn("sh", command)
                self.assertIn("/opt/wysearr/shelfarr_exec.rb", command)
                response = {
                    "huey_token": "shf_generated",
                    "huey_token_reused": False,
                    "settings_count": 24,
                    "download_clients": ["WyseARR qBittorrent", "WyseARR SABnzbd"],
                }
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "Rails informational line\n"
                        + bootstrap_shelfarr.BOOTSTRAP_SENTINEL
                        + json.dumps(response)
                        + "\n"
                    ),
                    stderr="",
                )

            value = bootstrap_shelfarr.converge_shelfarr(
                root, environment, "sab-secret", runner=runner
            )
            self.assertEqual(value["huey_token"], "shf_generated")


if __name__ == "__main__":
    unittest.main()
