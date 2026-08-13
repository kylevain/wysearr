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
