import copy
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import bootstrap_lazylibrarian as lazy


API_KEY = "a" * 32
QBIT_PASSWORD = "private-qbit-password"


def environment(**updates):
    values = {
        "EBOOK_ACQUISITION_OWNER": "lazylibrarian",
        "LAZYLIBRARIAN_ENABLED": "true",
        "LAZYLIBRARIAN_API_KEY": API_KEY,
        "LAZYLIBRARIAN_ADMIN_PORT": "5299",
        "PROWLARR_API_KEY": "private-prowlarr-key",
        "PROWLARR_PORT": "9696",
        "QBITTORRENT_USERNAME": "operator",
        "QBITTORRENT_PASSWORD": QBIT_PASSWORD,
        "QBITTORRENT_PORT": "8080",
        "WYSEARR_BIND_ADDRESS": "192.0.2.10",
    }
    values.update(updates)
    return values


def render_environment(values):
    return "".join(f"{key}={value}\n" for key, value in values.items())


def empty_providers():
    return {
        "newznab": [],
        "torznab": [],
        "rss": [],
        "irc": [],
        "torrent": [],
        "direct": [],
    }


def provider(
    name="LimeTorrents (Prowlarr)",
    *,
    host="http://prowlarr:9696/1/api",
    enabled=True,
    dltypes="E",
    bookcat="7020",
    audiocat="3030",
    magcat="8030",
    comiccat="8020",
    manual=True,
):
    return {
        "DISPNAME": name,
        "HOST": host,
        "ENABLED": enabled,
        "DLTYPES": dltypes,
        "BOOKCAT": bookcat,
        "AUDIOCAT": audiocat,
        "MAGCAT": magcat,
        "COMICCAT": comiccat,
        "MANUAL": manual,
    }


def application_resource(
    *, resource_id=None, api_key=API_KEY, tag_id=41, app_profile_id=None
):
    resource = {
        "name": lazy.MANAGED_APPLICATION_NAME,
        "implementation": "LazyLibrarian",
        "configContract": "LazyLibrarianSettings",
        "enable": True,
        "syncLevel": "fullSync",
        "appProfileId": app_profile_id,
        "tags": [tag_id],
        "fields": [
            {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
            {"name": "baseUrl", "value": "http://lazylibrarian:5299"},
            {"name": "apiKey", "value": api_key},
            {"name": "authUsername", "value": ""},
            {"name": "authPassword", "value": ""},
            {
                "name": "syncCategories",
                "value": list(lazy.MANAGED_SYNC_CATEGORIES),
            },
        ],
    }
    if resource_id is not None:
        resource["id"] = resource_id
    return resource


def indexer(
    resource_id,
    name,
    *,
    protocol="torrent",
    enabled=True,
    categories=(7000, 7020),
    tags=(),
):
    return {
        "id": resource_id,
        "name": name,
        "protocol": protocol,
        "enable": enabled,
        "tags": list(tags),
        "capabilities": {
            "categories": [
                {
                    "id": 1000,
                    "subCategories": [{"id": value} for value in categories],
                }
            ]
        },
    }


class ConfigPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_env(self, values=None, *, mode=0o600):
        path = self.root / ".env"
        path.write_text(render_environment(values or environment()), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_prepare_generates_key_converges_ebook_only_config_and_is_idempotent(self):
        values = environment()
        del values["LAZYLIBRARIAN_API_KEY"]
        env_path = self.write_env(values, mode=0o644)
        config_path = self.root / "config" / "lazylibrarian" / "config.ini"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "[GENERAL]\ncustom_operator_setting = preserved\naudio_tab = 1\n",
            encoding="utf-8",
        )

        with mock.patch.object(lazy.secrets, "token_hex", return_value="b" * 32):
            first = lazy.prepare_lazylibrarian_config(self.root)

        first_env = env_path.read_bytes()
        first_config = config_path.read_bytes()
        second = lazy.prepare_lazylibrarian_config(self.root)

        self.assertEqual(first["LAZYLIBRARIAN_API_KEY"], "b" * 32)
        self.assertEqual(second["LAZYLIBRARIAN_API_KEY"], "b" * 32)
        self.assertEqual(env_path.read_bytes(), first_env)
        self.assertEqual(config_path.read_bytes(), first_config)
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(config_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

        parser = lazy.read_lazylibrarian_config(config_path)
        lazy.assert_lazylibrarian_config(parser, second)
        self.assertEqual(parser.get("GENERAL", "custom_operator_setting"), "preserved")
        self.assertEqual(parser.get("GENERAL", "audio_tab"), "0")
        self.assertEqual(parser.get("GENERAL", "ebook_tab"), "1")
        self.assertEqual(parser.get("API", "book_api"), "OpenLibrary")
        self.assertEqual(parser.get("QBITTORRENT", "qbittorrent_label"), "ebooks")
        self.assertEqual(
            parser.get("QBITTORRENT", "qbittorrent_dir"), "/downloads/ebooks"
        )
        self.assertEqual(parser.get("SEARCHSCAN", "search_bookinterval"), "0")
        self.assertEqual(parser.get("SEARCHSCAN", "scan_interval"), "0")
        self.assertEqual(parser.get("TORRENT", "keep_seeding"), "1")
        self.assertEqual(parser.get("POSTPROCESS", "del_completed"), "0")
        self.assertEqual(parser.get("LOGGING", "logredact"), "1")
        self.assertEqual(parser.get("TELEMETRY", "telemetry_enable"), "0")

    def test_prepare_enforces_private_mode_when_existing_key_is_reused(self):
        env_path = self.write_env(mode=0o644)

        result = lazy.prepare_lazylibrarian_config(self.root)

        self.assertEqual(result["LAZYLIBRARIAN_API_KEY"], API_KEY)
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_strict_environment_accepts_comments_export_and_quoted_values(self):
        path = self.root / ".env"
        path.write_text(
            "# retained note\nexport ONE=first\nTWO=\"two words\" # note\n",
            encoding="utf-8",
        )
        self.assertEqual(
            lazy.load_strict_environment(path),
            {"ONE": "first", "TWO": "two words"},
        )

    def test_strict_environment_rejects_ambiguous_unsafe_or_unbounded_files(self):
        cases = {
            "duplicate": b"ONE=1\nONE=2\n",
            "malformed": b"ONE=1\nthis is not an assignment\n",
            "invalid UTF-8": b"ONE=\xff\n",
            "too large": b"#" + b"x" * lazy.MAX_PRIVATE_FILE_BYTES,
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                path = self.root / f"{name.replace(' ', '-')}.env"
                path.write_bytes(content)
                with self.assertRaises(lazy.BootstrapError):
                    lazy.load_strict_environment(path)

        target = self.root / "real.env"
        target.write_text("ONE=1\n", encoding="utf-8")
        link = self.root / "linked.env"
        link.symlink_to(target)
        with self.assertRaisesRegex(lazy.BootstrapError, "Unable to read private"):
            lazy.load_strict_environment(link)

    def test_prepare_rejects_bad_key_owner_and_disabled_owner(self):
        invalid = (
            (
                environment(LAZYLIBRARIAN_API_KEY="A" * 32),
                "32 lowercase hexadecimal",
            ),
            (environment(EBOOK_ACQUISITION_OWNER="calibre"), "must be shelfarr"),
            (
                environment(LAZYLIBRARIAN_ENABLED="True"),
                "requires LAZYLIBRARIAN_ENABLED=true",
            ),
            (environment(QBITTORRENT_PASSWORD=""), "QBITTORRENT_PASSWORD"),
        )
        for index, (values, message) in enumerate(invalid):
            with self.subTest(index=index):
                self.write_env(values)
                with self.assertRaisesRegex(lazy.BootstrapError, message):
                    lazy.prepare_lazylibrarian_config(self.root)

    def test_config_reader_rejects_duplicate_case_variants_symlink_and_oversize(self):
        config = self.root / "config.ini"
        config.write_text("[API]\napi_enabled=1\n[api]\napi_key=x\n", encoding="utf-8")
        parser = lazy.read_lazylibrarian_config(config)
        with self.assertRaisesRegex(lazy.BootstrapError, "case-variant"):
            lazy._section_name(parser, "API")

        target = self.root / "target.ini"
        target.write_text("[API]\napi_enabled=1\n", encoding="utf-8")
        link = self.root / "link.ini"
        link.symlink_to(target)
        with self.assertRaisesRegex(lazy.BootstrapError, "Unable to read private"):
            lazy.read_lazylibrarian_config(link)

        oversized = self.root / "large.ini"
        oversized.write_bytes(b"x" * (lazy.MAX_PRIVATE_FILE_BYTES + 1))
        with self.assertRaisesRegex(lazy.BootstrapError, "too large"):
            lazy.read_lazylibrarian_config(oversized)

    def test_prepare_refuses_unsafe_config_directory(self):
        self.write_env()
        real = self.root / "real-config"
        real.mkdir()
        config_parent = self.root / "config"
        config_parent.mkdir()
        (config_parent / "lazylibrarian").symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(lazy.BootstrapError, "unsafe"):
            lazy.prepare_lazylibrarian_config(self.root)


class FakeResponse:
    def __init__(self, data=b"{}", *, status=200):
        self.data = data
        self.status = status
        self.closed = False
        self.read_limit = None

    def read(self, limit):
        self.read_limit = limit
        return self.data[:limit]

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


class LazyLibrarianApiTests(unittest.TestCase):
    def test_raw_builds_bounded_request_and_closes_response(self):
        response = FakeResponse(b'{"Success":true}')
        opener = mock.Mock()
        opener.open.return_value = response
        api = lazy.LazyLibrarianApi(
            "http://127.0.0.1:5299/", API_KEY, timeout=7, opener=opener
        )

        self.assertEqual(api.json("findBook", {"name": "A & B", "id": [1, 2]}), {"Success": True})

        request = opener.open.call_args.args[0]
        parsed_url = urllib.parse.urlsplit(request.full_url)
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "http://127.0.0.1:5299/api")
        self.assertEqual(parsed_url.query, "")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).path, "/api")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.get_header("Content-type"),
            "application/x-www-form-urlencoded",
        )
        self.assertNotIn(API_KEY, request.full_url)
        self.assertEqual(form["apikey"], [API_KEY])
        self.assertEqual(form["cmd"], ["findBook"])
        self.assertEqual(form["name"], ["A & B"])
        self.assertEqual(form["id"], ["1", "2"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        self.assertEqual(response.read_limit, lazy.MAX_PRIVATE_FILE_BYTES + 1)
        self.assertTrue(response.closed)

    def test_api_errors_are_bounded_and_do_not_disclose_key(self):
        failures = (
            mock.Mock(open=mock.Mock(side_effect=urllib.error.URLError(API_KEY))),
            mock.Mock(open=mock.Mock(return_value=FakeResponse(b"{}", status=500))),
            mock.Mock(
                open=mock.Mock(
                    return_value=FakeResponse(
                        b"x" * (lazy.MAX_PRIVATE_FILE_BYTES + 1), status=200
                    )
                )
            ),
        )
        for opener in failures:
            with self.subTest(opener=opener):
                api = lazy.LazyLibrarianApi("http://ll:5299", API_KEY, opener=opener)
                with self.assertRaises(lazy.BootstrapError) as raised:
                    api.raw("getVersion")
                self.assertNotIn(API_KEY, str(raised.exception))

        api = lazy.LazyLibrarianApi(
            "http://ll:5299", API_KEY, opener=mock.Mock(open=mock.Mock(return_value=FakeResponse(b"not-json")))
        )
        with self.assertRaisesRegex(lazy.BootstrapError, "malformed JSON"):
            api.json("getVersion")

    def test_version_and_help_capability_gate(self):
        api = mock.Mock()
        api.json.return_value = {
            "Success": True,
            "current_version": lazy.EXPECTED_LAZYLIBRARIAN_VERSION,
        }
        api.raw.return_value = " ".join(lazy.REQUIRED_API_COMMANDS).encode()

        self.assertEqual(
            lazy.validate_lazylibrarian_api(api), lazy.EXPECTED_LAZYLIBRARIAN_VERSION
        )
        api.json.assert_called_once_with("getVersion")
        api.raw.assert_called_once_with("help")

    def test_version_gate_accepts_only_empty_pinned_image_quirk(self):
        api = mock.Mock()
        api.json.return_value = {
            "Success": True,
            "install_type": "",
            "current_version": "",
            "latest_version": "",
            "commits_behind": 0,
        }
        api.raw.return_value = " ".join(lazy.REQUIRED_API_COMMANDS).encode()

        self.assertEqual(
            lazy.validate_lazylibrarian_api(api),
            lazy.EXPECTED_LAZYLIBRARIAN_VERSION,
        )

    def test_version_and_help_reject_invalid_or_incomplete_api(self):
        invalid_versions = (
            [],
            {},
            {"Success": False, "current_version": lazy.EXPECTED_LAZYLIBRARIAN_VERSION},
            {"Success": True},
            {"Success": True, "current_version": None},
            {"Success": True, "current_version": "different"},
        )
        for value in invalid_versions:
            with self.subTest(value=value):
                api = mock.Mock()
                api.json.return_value = value
                with self.assertRaises(lazy.BootstrapError):
                    lazy.validate_lazylibrarian_api(api)

        api = mock.Mock()
        api.json.return_value = {
            "Success": True,
            "current_version": lazy.EXPECTED_LAZYLIBRARIAN_VERSION,
        }
        api.raw.return_value = b"getVersion findBook"
        with self.assertRaisesRegex(lazy.BootstrapError, "missing required commands"):
            lazy.validate_lazylibrarian_api(api)

    def test_effective_config_accepts_runtime_types_and_canonical_csv_order(self):
        values = environment()
        desired = lazy._desired_config(values)
        calls = []

        def read_config(command, parameters=None):
            self.assertEqual(command, "readCFG")
            identity = (parameters["group"], parameters["name"])
            calls.append(identity)
            expected = desired[identity[0]][identity[1]]
            if identity in lazy.BOOLEAN_CONFIG_KEYS:
                effective = "True" if expected == "1" else "False"
            elif identity in lazy.INTEGER_CONFIG_KEYS:
                effective = f"+{int(expected):03d}"
            elif identity in lazy.CSV_CONFIG_KEYS:
                effective = ", ".join(
                    reversed([item.strip() for item in expected.split(",")])
                )
            else:
                effective = expected
            return f"[{effective}]".encode("utf-8")

        api = mock.Mock()
        api.raw.side_effect = read_config

        lazy.assert_effective_lazylibrarian_config(api, values)

        all_managed = {
            (section, key)
            for section, settings in desired.items()
            for key in settings
        }
        typed = (
            lazy.BOOLEAN_CONFIG_KEYS
            | lazy.INTEGER_CONFIG_KEYS
            | lazy.CSV_CONFIG_KEYS
        )
        self.assertLessEqual(typed, all_managed)
        self.assertFalse(lazy.BOOLEAN_CONFIG_KEYS & lazy.INTEGER_CONFIG_KEYS)
        self.assertFalse(lazy.BOOLEAN_CONFIG_KEYS & lazy.CSV_CONFIG_KEYS)
        self.assertFalse(lazy.INTEGER_CONFIG_KEYS & lazy.CSV_CONFIG_KEYS)
        self.assertEqual(set(calls), all_managed)
        self.assertEqual(len(calls), len(all_managed))
        self.assertEqual(
            calls[: len(desired["LOGGING"])],
            [("LOGGING", key) for key in desired["LOGGING"]],
        )

    def test_effective_config_rejects_drift_without_disclosing_values(self):
        values = environment()
        desired = lazy._desired_config(values)
        wrong_password = "wrong-secret-value"

        def read_config(command, parameters=None):
            identity = (parameters["group"], parameters["name"])
            effective = desired[identity[0]][identity[1]]
            if identity in lazy.BOOLEAN_CONFIG_KEYS:
                effective = "1" if effective == "1" else ""
            if identity == ("QBITTORRENT", "qbittorrent_pass"):
                effective = wrong_password
            return f"[{effective}]".encode("utf-8")

        api = mock.Mock()
        api.raw.side_effect = read_config
        with self.assertRaisesRegex(
            lazy.BootstrapError,
            r"QBITTORRENT\.qbittorrent_pass is incorrect",
        ) as raised:
            lazy.assert_effective_lazylibrarian_config(api, values)

        message = str(raised.exception)
        self.assertNotIn(QBIT_PASSWORD, message)
        self.assertNotIn(wrong_password, message)
        self.assertNotIn(API_KEY, message)

    def test_effective_config_rejects_unwrapped_or_non_utf8_responses(self):
        for response in (b"No config entry", b"\xff"):
            with self.subTest(response=response):
                api = mock.Mock()
                api.raw.return_value = response
                with self.assertRaisesRegex(
                    lazy.BootstrapError,
                    r"LOGGING\.loglevel",
                ):
                    lazy.assert_effective_lazylibrarian_config(api, environment())


class IndexerClient:
    def __init__(self, indexers, statuses=None):
        self.indexers = {item["id"]: copy.deepcopy(item) for item in indexers}
        self.statuses = [] if statuses is None else copy.deepcopy(statuses)
        self.puts = []

    def get_json(self, path):
        if path == "/api/v1/indexer":
            return copy.deepcopy(list(self.indexers.values()))
        if path == "/api/v1/indexerstatus":
            return copy.deepcopy(self.statuses)
        prefix = "/api/v1/indexer/"
        if path.startswith(prefix):
            return copy.deepcopy(self.indexers[int(path[len(prefix):])])
        raise AssertionError(path)

    def put_json(self, path, payload):
        self.puts.append((path, copy.deepcopy(payload)))
        resource_id = payload["id"]
        self.indexers[resource_id] = copy.deepcopy(payload)
        return copy.deepcopy(payload)


class ProwlarrIndexerTests(unittest.TestCase):
    def test_tags_only_enabled_torrent_book_indexers_and_is_idempotent(self):
        client = IndexerClient(
            [
                indexer(1, "LimeTorrents", tags=(9,)),
                indexer(2, "Nyaa", categories=(7000, 7020)),
                indexer(3, "Disabled Books", enabled=False),
                indexer(4, "Usenet Books", protocol="usenet"),
                indexer(5, "Movies", categories=(2000,)),
            ]
        )

        names = lazy.converge_ebook_indexer_tags(client, 41)

        self.assertEqual(names, {"LimeTorrents": "7020", "Nyaa": "7020"})
        self.assertEqual(client.indexers[1]["tags"], [9, 41])
        self.assertEqual(client.indexers[2]["tags"], [41])
        self.assertEqual(client.indexers[3]["tags"], [])
        mutations = len(client.puts)
        self.assertEqual(lazy.converge_ebook_indexer_tags(client, 41), names)
        self.assertEqual(len(client.puts), mutations)

    def test_temporarily_blocked_indexer_loses_only_managed_tag(self):
        client = IndexerClient(
            [
                indexer(1, "LimeTorrents", tags=(9,)),
                indexer(2, "Torrent Downloads", tags=(7, 41)),
            ],
            statuses=[
                {
                    "indexerId": 2,
                    "initialFailure": "2026-08-13T16:34:27Z",
                    "mostRecentFailure": "2026-08-14T17:16:29Z",
                    "disabledTill": "2026-08-14T18:16:29Z",
                }
            ],
        )

        categories = lazy.converge_ebook_indexer_tags(client, 41)

        self.assertEqual(categories, {"LimeTorrents": "7020"})
        self.assertEqual(client.indexers[1]["tags"], [9, 41])
        self.assertEqual(client.indexers[2]["tags"], [7])

    def test_requires_explicit_7020_and_removes_managed_tag_from_broad_books(self):
        client = IndexerClient(
            [
                indexer(1, "Nyaa", categories=(7000,), tags=(7, 41)),
                indexer(2, "Lime", categories=(7000, 7020)),
                indexer(3, "Pirate Bay", categories=(7000, 7020, 7050)),
            ]
        )

        self.assertEqual(
            lazy.converge_ebook_indexer_tags(client, 41),
            {
                "Lime": "7020",
                "Pirate Bay": "7020",
            },
        )
        self.assertEqual(client.indexers[1]["tags"], [7])

    def test_indexer_convergence_rejects_no_available_or_bad_status_evidence(self):
        blocked = IndexerClient(
            [indexer(1, "Books")],
            statuses=[
                {
                    "indexerId": 1,
                    "initialFailure": "2020-01-01T00:00:00Z",
                    "mostRecentFailure": "2020-01-01T01:00:00Z",
                    # Deliberately expired: retained failure state remains
                    # excluded until Prowlarr clears the row.
                    "disabledTill": "2020-01-01T02:00:00Z",
                }
            ],
        )
        with self.assertRaisesRegex(lazy.BootstrapError, "no available"):
            lazy.converge_ebook_indexer_tags(blocked, 41)

        invalid_statuses = (
            {},
            ["invalid"],
            [
                {
                    "indexerId": True,
                    "initialFailure": None,
                    "mostRecentFailure": None,
                    "disabledTill": None,
                }
            ],
            [
                {
                    "indexerId": 1,
                    "initialFailure": None,
                    "mostRecentFailure": "not-a-time",
                    "disabledTill": None,
                }
            ],
            [
                {
                    "indexerId": 1,
                    "initialFailure": None,
                    "mostRecentFailure": "2026-08-14T18:16:29",
                    "disabledTill": None,
                }
            ],
            [{"indexerId": 1, "disabledTill": None}],
            [
                {
                    "indexerId": 1,
                    "initialFailure": None,
                    "mostRecentFailure": None,
                    "disabledTill": None,
                },
                {
                    "indexerId": 1,
                    "initialFailure": None,
                    "mostRecentFailure": None,
                    "disabledTill": None,
                },
            ],
        )
        for statuses in invalid_statuses:
            with self.subTest(statuses=statuses), self.assertRaisesRegex(
                lazy.BootstrapError, "invalid indexer status"
            ):
                lazy.converge_ebook_indexer_tags(
                    IndexerClient([indexer(1, "Books")], statuses=statuses),
                    41,
                )

        cleared = IndexerClient(
            [indexer(1, "Books")],
            statuses=[
                {
                    "indexerId": 1,
                    "initialFailure": None,
                    "mostRecentFailure": None,
                    "disabledTill": None,
                }
            ],
        )
        self.assertEqual(
            lazy.converge_ebook_indexer_tags(cleared, 41), {"Books": "7020"}
        )

    def test_indexer_convergence_removes_ineligible_tag_and_fails_on_ambiguous_set(self):
        client = IndexerClient(
            [
                indexer(1, "Books"),
                indexer(2, "Movies", categories=(2000,), tags=(9, 41)),
            ]
        )
        self.assertEqual(
            lazy.converge_ebook_indexer_tags(client, 41), {"Books": "7020"}
        )
        self.assertEqual(client.indexers[2]["tags"], [9])

        cases = (
            ([indexer(1, "Books"), indexer(2, "books")], "duplicated"),
            (
                [indexer(1, "Broad Books", categories=(7000,))],
                "explicit ebook category 7020",
            ),
        )
        for indexers, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                lazy.BootstrapError, message
            ):
                lazy.converge_ebook_indexer_tags(IndexerClient(indexers), 41)

    def test_indexer_convergence_rejects_invalid_api_shapes(self):
        for value in ({}, ["not-an-indexer"]):
            with self.subTest(value=value):
                client = mock.Mock()
                client.get_json.return_value = value
                with self.assertRaisesRegex(lazy.BootstrapError, "invalid indexer data"):
                    lazy.converge_ebook_indexer_tags(client, 41)

        for categories in (
            None,
            [{"id": True, "subCategories": []}],
            [{"id": 7020, "subCategories": {}}],
        ):
            malformed = indexer(1, "Books")
            malformed["capabilities"]["categories"] = categories
            with self.subTest(categories=categories), self.assertRaisesRegex(
                lazy.BootstrapError, "invalid indexer categories"
            ):
                lazy.converge_ebook_indexer_tags(IndexerClient([malformed]), 41)

    def test_category_tree_allows_same_custom_id_in_multiple_branches(self):
        resource = indexer(1, "Books")
        resource["capabilities"]["categories"] = [
            {
                "id": 7000,
                "subCategories": [{"id": 7020}, {"id": 125996}],
            },
            {"id": 8000, "subCategories": [{"id": 125996}]},
        ]

        self.assertEqual(
            lazy.converge_ebook_indexer_tags(IndexerClient([resource]), 41),
            {"Books": "7020"},
        )


class ApplicationClient:
    def __init__(self, applications=(), *, fail_tests=False):
        self.applications = [copy.deepcopy(item) for item in applications]
        self.schema = application_resource()
        self.schema.pop("appProfileId")
        self.posts = []
        self.puts = []
        self.gets = []
        self.fail_tests = fail_tests

    def get_json(self, path):
        self.gets.append(path)
        if path == "/api/v1/applications":
            return copy.deepcopy(self.applications)
        if path == "/api/v1/applications/schema":
            return [copy.deepcopy(self.schema)]
        prefix = "/api/v1/applications/"
        if path.startswith(prefix):
            resource_id = int(path[len(prefix):])
            return copy.deepcopy(
                next(item for item in self.applications if item["id"] == resource_id)
            )
        raise AssertionError(path)

    def post_json(self, path, payload, *, retry=False):
        self.posts.append((path, copy.deepcopy(payload), retry))
        if path == "/api/v1/applications/test":
            if self.fail_tests:
                raise lazy.ApiTransportError("private failure")
            return {}
        if path == "/api/v1/applications?forceSave=true":
            saved = copy.deepcopy(payload)
            saved["id"] = 17
            self.applications.append(saved)
            return copy.deepcopy(saved)
        raise AssertionError(path)

    def put_json(self, path, payload):
        self.puts.append((path, copy.deepcopy(payload)))
        resource_id = payload["id"]
        self.applications = [
            copy.deepcopy(payload) if item["id"] == resource_id else item
            for item in self.applications
        ]
        return copy.deepcopy(payload)


class ProwlarrApplicationTests(unittest.TestCase):
    def test_creates_exact_private_full_sync_application_then_is_idempotent(self):
        client = ApplicationClient()

        saved = lazy.converge_lazylibrarian_application(client, API_KEY, 41)

        self.assertEqual(saved["id"], 17)
        self.assertEqual(saved["name"], "LazyLibrarian")
        self.assertEqual(saved["tags"], [41])
        self.assertEqual(saved["syncLevel"], "fullSync")
        self.assertIsNone(saved.get("appProfileId"))
        self.assertNotIn("/api/v1/appprofile", client.gets)
        fields = lazy._field_values(saved)
        self.assertEqual(fields["prowlarrUrl"], "http://prowlarr:9696")
        self.assertEqual(fields["baseUrl"], "http://lazylibrarian:5299")
        self.assertEqual(fields["apiKey"], API_KEY)
        self.assertEqual(fields["authUsername"], "")
        self.assertEqual(fields["authPassword"], "")
        self.assertEqual(fields["syncCategories"], list(lazy.MANAGED_SYNC_CATEGORIES))
        self.assertEqual(len([call for call in client.posts if "forceSave" in call[0]]), 1)

        writes = (len(client.puts), len([call for call in client.posts if "forceSave" in call[0]]))
        lazy.converge_lazylibrarian_application(client, API_KEY, 41)
        self.assertEqual(
            (len(client.puts), len([call for call in client.posts if "forceSave" in call[0]])),
            writes,
        )

    def test_masked_persisted_api_key_does_not_trigger_secret_churn(self):
        existing = application_resource(resource_id=17, api_key="********")
        client = ApplicationClient([existing])

        saved = lazy.converge_lazylibrarian_application(client, API_KEY, 41)

        self.assertEqual(lazy._field_values(saved)["apiKey"], "********")
        self.assertEqual(client.puts, [])

    def test_repairs_drift_and_removes_noncanonical_application_profile(self):
        existing = application_resource(
            resource_id=17,
            api_key="wrong-key",
            tag_id=9,
            app_profile_id=2,
        )
        existing["enable"] = False
        existing["syncLevel"] = "addOnly"
        client = ApplicationClient([existing])

        saved = lazy.converge_lazylibrarian_application(client, API_KEY, 41)

        self.assertIsNone(saved.get("appProfileId"))
        self.assertNotIn("appProfileId", client.puts[0][1])
        self.assertEqual(saved["tags"], [41])
        self.assertTrue(saved["enable"])
        self.assertEqual(saved["syncLevel"], "fullSync")
        self.assertEqual(lazy._field_values(saved)["apiKey"], API_KEY)
        self.assertEqual(len(client.puts), 1)

    def test_application_convergence_rejects_duplicates_conflicts_and_unreachable(self):
        duplicate = ApplicationClient(
            [
                application_resource(resource_id=1),
                application_resource(resource_id=2),
            ]
        )
        with self.assertRaisesRegex(lazy.BootstrapError, "duplicate"):
            lazy.converge_lazylibrarian_application(duplicate, API_KEY, 41)

        conflict = application_resource(resource_id=1)
        conflict["implementation"] = "Sonarr"
        with self.assertRaisesRegex(lazy.BootstrapError, "conflicting"):
            lazy.converge_lazylibrarian_application(
                ApplicationClient([conflict]), API_KEY, 41
            )

        with self.assertRaisesRegex(lazy.BootstrapError, "cannot reach"):
            lazy.converge_lazylibrarian_application(
                ApplicationClient(fail_tests=True), API_KEY, 41
            )

    def test_application_convergence_requires_unique_schema(self):
        client = ApplicationClient()
        client.schema["implementation"] = "Sonarr"
        with self.assertRaisesRegex(lazy.BootstrapError, "unique"):
            lazy.converge_lazylibrarian_application(client, API_KEY, 41)


class SyncClient:
    def __init__(self, statuses, command={"id": 91}):
        self.statuses = list(statuses)
        self.command = command
        self.posts = []

    def post_json(self, path, payload, *, retry=False):
        self.posts.append((path, payload, retry))
        return copy.deepcopy(self.command)

    def get_json(self, path):
        self.last_status = self.statuses.pop(0)
        return copy.deepcopy(self.last_status)


class ProwlarrSyncTests(unittest.TestCase):
    def test_sync_waits_for_successful_completion(self):
        client = SyncClient(
            [
                {"status": "started", "result": "unknown"},
                {"status": "completed", "result": "successful"},
            ]
        )
        sleeps = []
        lazy.run_application_indexer_sync(client, timeout=10, sleep=sleeps.append)
        self.assertEqual(sleeps, [1])
        self.assertEqual(
            client.posts,
            [("/api/v1/command", {"name": "ApplicationIndexerSync"}, False)],
        )

    def test_sync_rejects_missing_id_and_failed_command(self):
        with self.assertRaisesRegex(lazy.BootstrapError, "did not start"):
            lazy.run_application_indexer_sync(SyncClient([], command={"id": True}))
        with self.assertRaisesRegex(lazy.BootstrapError, "sync failed"):
            lazy.run_application_indexer_sync(
                SyncClient([{"status": "failed", "result": "failed"}])
            )

    def test_sync_times_out(self):
        client = SyncClient([{"status": "running"}])
        with mock.patch.object(lazy.time, "monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(lazy.BootstrapError, "timed out"):
                lazy.run_application_indexer_sync(client, timeout=1, sleep=lambda _: None)


class ProviderApi:
    def __init__(self, responses, *, change_error=False):
        self.responses = [copy.deepcopy(item) for item in responses]
        self.calls = []
        self.change_error = change_error

    def json(self, command, parameters=None):
        self.calls.append((command, copy.deepcopy(parameters)))
        if command == "listProviders":
            return self.responses.pop(0)
        if command == "changeProvider":
            if self.change_error:
                raise lazy.BootstrapError("ambiguous transport failure")
            return {"Success": True}
        raise AssertionError(command)


class ProviderConvergenceTests(unittest.TestCase):
    def test_corrects_prowlarr_provider_to_ebook_only_then_verifies(self):
        before = empty_providers()
        before["torznab"] = [
            provider(
                name="LimeTorrents (Prowlarr)",
                bookcat="8000,8010",
                dltypes="A,E,M",
                manual=False,
            )
        ]
        after = empty_providers()
        # The pinned capability refresh repopulates these dormant non-book
        # category fields.  DLTYPES=E is the hard search-dispatch gate.
        after["torznab"] = [provider(name="LimeTorrents (Prowlarr)")]
        api = ProviderApi([before, after])

        count = lazy.converge_ebook_providers(api, {"LimeTorrents": "7020"})

        self.assertEqual(count, 1)
        self.assertEqual(
            api.calls,
            [
                ("listProviders", None),
                (
                    "changeProvider",
                    {
                        "name": "LimeTorrents (Prowlarr)",
                        "providertype": "torznab",
                        "BOOKCAT": "7020",
                        "DLTYPES": "E",
                        "MANUAL": "1",
                    },
                ),
                ("listProviders", None),
            ],
        )

    def test_provider_convergence_is_idempotent_and_accepts_ambiguous_corrected_write(self):
        correct = empty_providers()
        correct["torznab"] = [provider()]
        api = ProviderApi([correct, correct])
        self.assertEqual(
            lazy.converge_ebook_providers(api, {"LimeTorrents": "7020"}), 1
        )
        self.assertEqual([name for name, _ in api.calls], ["listProviders", "listProviders"])

        stale = empty_providers()
        stale["torznab"] = [provider(dltypes="A")]
        api = ProviderApi([stale, correct], change_error=True)
        self.assertEqual(
            lazy.converge_ebook_providers(api, {"LimeTorrents": "7020"}), 1
        )

    def test_provider_convergence_rejects_wrong_set_usenet_or_non_prowlarr_host(self):
        cases = []
        wrong_name = empty_providers()
        wrong_name["torznab"] = [provider(name="Different (Prowlarr)")]
        cases.append((wrong_name, {"LimeTorrents": "7020"}, "does not match"))
        usenet = empty_providers()
        usenet["newznab"] = [provider()]
        cases.append((usenet, {"LimeTorrents": "7020"}, "Usenet"))
        remote = empty_providers()
        remote["torznab"] = [provider(host="https://example.invalid:9696/api")]
        cases.append((remote, {"LimeTorrents": "7020"}, "not routed"))
        for providers, expected, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                lazy.BootstrapError, message
            ):
                lazy.converge_ebook_providers(ProviderApi([providers]), expected)

    def test_provider_revalidation_rejects_provider_that_changes_to_usenet(self):
        first = empty_providers()
        first["torznab"] = [provider()]
        second = empty_providers()
        second["newznab"] = [provider()]

        with self.assertRaisesRegex(lazy.BootstrapError, "Usenet"):
            lazy.converge_ebook_providers(
                ProviderApi([first, second]), {"LimeTorrents": "7020"}
            )

    def test_provider_convergence_rejects_non_ebook_categories_or_rival_provider(self):
        for categories in (
            "7000",
            "7000,7020",
            "7040",
            "7050",
            "7060",
            "7010",
            "7030",
            "7999",
            "",
        ):
            invalid_book = empty_providers()
            invalid_book["torznab"] = [provider(bookcat=categories)]
            with self.subTest(categories=categories), self.assertRaisesRegex(
                lazy.BootstrapError, "not ebook-only"
            ):
                lazy.converge_ebook_providers(
                    ProviderApi([invalid_book, invalid_book]),
                    {"LimeTorrents": "7020"},
                )

        with self.assertRaisesRegex(
            lazy.BootstrapError, "invalid ebook provider categories"
        ):
            lazy.converge_ebook_providers(
                ProviderApi([]), {"Nyaa.si": "7000"}
            )

        rival = empty_providers()
        rival["torznab"] = [provider()]
        rival["direct"] = [{"ENABLED": True, "DISPNAME": "Rival"}]
        with self.assertRaisesRegex(lazy.BootstrapError, "outside managed Prowlarr"):
            lazy.converge_ebook_providers(
                ProviderApi([rival, rival]), {"LimeTorrents": "7020"}
            )

    def test_provider_convergence_rejects_every_non_e_download_type(self):
        for download_types in ("A", "M", "C", "A,E", "E,M", "A,C,E,M", "e"):
            unsafe = empty_providers()
            unsafe["torznab"] = [provider(dltypes=download_types)]
            with self.subTest(download_types=download_types), self.assertRaisesRegex(
                lazy.BootstrapError, "not ebook-only"
            ):
                lazy.converge_ebook_providers(
                    ProviderApi([unsafe, unsafe]), {"LimeTorrents": "7020"}
                )

    def test_provider_convergence_requires_manual_and_canonical_dormant_categories(self):
        correct = empty_providers()
        correct["torznab"] = [provider()]
        not_manual = empty_providers()
        not_manual["torznab"] = [provider(manual=False)]
        self.assertEqual(
            lazy.converge_ebook_providers(
                ProviderApi([not_manual, correct]), {"LimeTorrents": "7020"}
            ),
            1,
        )

        for field, value in (
            ("audiocat", "3030, 3040"),
            ("magcat", "8030,8030"),
            ("comiccat", "8020,7030"),
            ("audiocat", "audio"),
        ):
            malformed = empty_providers()
            malformed["torznab"] = [provider(**{field: value})]
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                lazy.BootstrapError, "not ebook-only"
            ):
                lazy.converge_ebook_providers(
                    ProviderApi([malformed, malformed]),
                    {"LimeTorrents": "7020"},
                )

    def test_provider_convergence_rejects_invalid_response_and_unpersisted_change(self):
        with self.assertRaisesRegex(lazy.BootstrapError, "invalid provider data"):
            lazy.converge_ebook_providers(
                ProviderApi([{"torznab": []}]), {"LimeTorrents": "7020"}
            )

        stale = empty_providers()
        stale["torznab"] = [provider(dltypes="A")]
        with self.assertRaisesRegex(lazy.BootstrapError, "not ebook-only"):
            lazy.converge_ebook_providers(
                ProviderApi([stale, stale], change_error=True),
                {"LimeTorrents": "7020"},
            )


class QbittorrentValidationTests(unittest.TestCase):
    def test_validates_authentication_category_and_exact_save_path_without_mutation(self):
        client = mock.Mock()
        client.login.return_value = True
        client.categories.return_value = {
            "ebooks": {"name": "ebooks", "savePath": "/downloads/ebooks"}
        }
        factory = mock.Mock(return_value=client)
        lazy.validate_qbittorrent(environment(), client_factory=factory)

        factory.assert_called_once_with(
            "http://192.0.2.10:8080", timeout=10.0, retries=3
        )
        client.login.assert_called_once_with("operator", QBIT_PASSWORD)
        client.categories.assert_called_once_with()
        self.assertEqual(
            client.method_calls,
            [mock.call.login("operator", QBIT_PASSWORD), mock.call.categories()],
        )

    def test_qbittorrent_validation_rejects_missing_invalid_or_wrong_category(self):
        for name in ("QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD"):
            values = environment(**{name: ""})
            with self.subTest(name=name), self.assertRaisesRegex(
                lazy.BootstrapError, name
            ):
                lazy.validate_qbittorrent(values)

        with self.assertRaisesRegex(lazy.BootstrapError, "must be numeric"):
            lazy.validate_qbittorrent(environment(QBITTORRENT_PORT="eight"))

        for port in ("0", "-1", "65536"):
            with self.subTest(port=port), self.assertRaises(lazy.BootstrapError):
                lazy.validate_qbittorrent(environment(QBITTORRENT_PORT=port))

        client = mock.Mock()
        client.login.return_value = True
        client.categories.return_value = {
            "ebooks": {"savePath": "/downloads/wrong"}
        }
        with self.assertRaisesRegex(lazy.BootstrapError, "category/save path"):
            lazy.validate_qbittorrent(
                environment(), client_factory=mock.Mock(return_value=client)
            )

        for outcome in (False, lazy.BootstrapError("unavailable")):
            failing = mock.Mock()
            if isinstance(outcome, Exception):
                failing.login.side_effect = outcome
            else:
                failing.login.return_value = outcome
            with self.subTest(outcome=type(outcome).__name__), self.assertRaisesRegex(
                lazy.BootstrapError, "validation login failed"
            ):
                lazy.validate_qbittorrent(
                    environment(), client_factory=mock.Mock(return_value=failing)
                )
            failing.categories.assert_not_called()


class BootstrapOrchestrationTests(unittest.TestCase):
    def test_runtime_bootstrap_converges_in_safe_order_and_returns_counts(self):
        values = environment()
        events = []
        ll_api = mock.Mock()
        prowlarr = mock.Mock()

        def ll_factory(url, key, *, timeout):
            events.append(("ll-factory", url, key, timeout))
            return ll_api

        def prowlarr_factory(url, *, headers, timeout, retries):
            events.append(("prowlarr-factory", url, headers, timeout, retries))
            return prowlarr

        with mock.patch.multiple(
            lazy,
            load_strict_environment=mock.DEFAULT,
            validate_lazylibrarian_api=mock.DEFAULT,
            assert_effective_lazylibrarian_config=mock.DEFAULT,
            validate_qbittorrent=mock.DEFAULT,
            ensure_prowlarr_tag=mock.DEFAULT,
            converge_ebook_indexer_tags=mock.DEFAULT,
            converge_lazylibrarian_application=mock.DEFAULT,
            run_application_indexer_sync=mock.DEFAULT,
            converge_ebook_providers=mock.DEFAULT,
        ) as patched:
            patched["load_strict_environment"].return_value = values
            patched["validate_lazylibrarian_api"].return_value = lazy.EXPECTED_LAZYLIBRARIAN_VERSION
            patched["ensure_prowlarr_tag"].return_value = 41
            patched["converge_ebook_indexer_tags"].return_value = {
                "One": "7020",
                "Two": "7020",
            }
            patched["converge_ebook_providers"].return_value = 2

            result = lazy.bootstrap_lazylibrarian(
                Path("/stack"),
                timeout=12,
                prowlarr_client_factory=prowlarr_factory,
                ll_api_factory=ll_factory,
            )

        self.assertEqual(
            result,
            {"version": lazy.EXPECTED_LAZYLIBRARIAN_VERSION, "indexers": 2, "providers": 2},
        )
        self.assertIn(("ll-factory", "http://127.0.0.1:5299", API_KEY, 12), events)
        self.assertIn(
            (
                "prowlarr-factory",
                "http://192.0.2.10:9696",
                {"X-Api-Key": "private-prowlarr-key"},
                12,
                3,
            ),
            events,
        )
        patched["validate_qbittorrent"].assert_called_once_with(values)
        patched["assert_effective_lazylibrarian_config"].assert_called_once_with(
            ll_api, values
        )
        patched["converge_lazylibrarian_application"].assert_called_once_with(
            prowlarr, API_KEY, 41
        )
        patched["run_application_indexer_sync"].assert_called_once_with(
            prowlarr, timeout=60.0
        )
        patched["converge_ebook_providers"].assert_called_once_with(
            ll_api, {"One": "7020", "Two": "7020"}
        )

    def test_runtime_environment_rejects_missing_credentials_and_bad_ports(self):
        for name in (
            "LAZYLIBRARIAN_API_KEY",
            "PROWLARR_API_KEY",
            "QBITTORRENT_USERNAME",
            "QBITTORRENT_PASSWORD",
        ):
            values = environment(**{name: ""})
            with self.subTest(name=name), self.assertRaisesRegex(
                lazy.BootstrapError, name
            ):
                lazy._validate_runtime_environment(values)
        with self.assertRaisesRegex(lazy.BootstrapError, "invalid format"):
            lazy._validate_runtime_environment(environment(LAZYLIBRARIAN_API_KEY="bad"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                lazy, "load_strict_environment", return_value=environment(PROWLARR_PORT="bad")
            ):
                with self.assertRaisesRegex(lazy.BootstrapError, "port must be numeric"):
                    lazy.bootstrap_lazylibrarian(root)

            for name in ("LAZYLIBRARIAN_ADMIN_PORT", "PROWLARR_PORT"):
                for port in ("0", "-1", "65536"):
                    with self.subTest(name=name, port=port), mock.patch.object(
                        lazy,
                        "load_strict_environment",
                        return_value=environment(**{name: port}),
                    ):
                        with self.assertRaises(lazy.BootstrapError):
                            lazy.bootstrap_lazylibrarian(root)

    def test_effective_config_failure_precedes_qbit_and_prowlarr(self):
        values = environment()
        ll_api = mock.Mock()
        ll_factory = mock.Mock(return_value=ll_api)
        prowlarr_factory = mock.Mock()

        with mock.patch.object(
            lazy, "load_strict_environment", return_value=values
        ), mock.patch.object(
            lazy,
            "validate_lazylibrarian_api",
            return_value=lazy.EXPECTED_LAZYLIBRARIAN_VERSION,
        ), mock.patch.object(
            lazy,
            "assert_effective_lazylibrarian_config",
            side_effect=lazy.BootstrapError("effective config drift"),
        ), mock.patch.object(lazy, "validate_qbittorrent") as qbit:
            with self.assertRaisesRegex(lazy.BootstrapError, "effective config drift"):
                lazy.bootstrap_lazylibrarian(
                    Path("/stack"),
                    ll_api_factory=ll_factory,
                    prowlarr_client_factory=prowlarr_factory,
                )

        qbit.assert_not_called()
        prowlarr_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
