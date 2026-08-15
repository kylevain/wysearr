import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import check_prowlarr_rotation as checker


OLD_KEY = "a" * 32
NEW_KEY = "b" * 32
OTHER_KEY = "c" * 32
PROWLARR_ID = "d" * 64
QBITTORRENT_ID = "e" * 64
PROWLARR_STARTED = "2026-08-14T06:01:13.696954561Z"
QBITTORRENT_STARTED = "2026-08-14T06:27:22.090641702Z"


def arr_payload(count, key="********"):
    return [
        {
            "id": index + 1,
            "enable": True,
            "downloadClientId": 0,
            "implementation": "Torznab",
            "fields": [
                {"name": "baseUrl", "value": f"http://prowlarr:9696/{index + 1}/"},
                {"name": "apiPath", "value": "/api"},
                {"name": "apiKey", "value": key},
            ],
        }
        for index in range(count)
    ]


def ll_payload(key=NEW_KEY):
    return {
        "newznab": [],
        "torznab": [
            {"ENABLED": "1", "API": key, "DISPNAME": "First (prowlarr)"},
            {"ENABLED": True, "API": key, "DISPNAME": "Second (prowlarr)"},
        ],
        "rss": [],
        "irc": [],
        "torrent": [],
        "direct": [],
    }


class FakeBackend:
    def __init__(self):
        self.events = []
        self.new_auth = True
        self.old_rejected = True
        self.commands = [
            {"id": 8, "name": "ResetApiKey", "status": "completed"},
            {"id": 9, "name": "ApplicationIndexerSync", "status": "completed"},
        ]
        self.arr = {
            spec.name: arr_payload(spec.expected_indexers) for spec in checker.ARR_SPECS
        }
        self.arr_persisted = {
            spec.name: True for spec in checker.ARR_SPECS
        }
        self.arr_live = {spec.name: True for spec in checker.ARR_SPECS}
        self.ll = ll_payload()
        self.shelfarr = True
        self.huey = True
        self.lanes_empty = True
        self.identities = {service: True for service in checker.IDENTITY_SERVICES}

    def prowlarr_auth_matches(self, api_key):
        self.events.append(("new-auth", api_key))
        return self.new_auth

    def prowlarr_auth_rejected(self, api_key):
        self.events.append(("old-auth", api_key))
        return self.old_rejected

    def prowlarr_commands(self, api_key):
        self.events.append(("commands", api_key))
        return self.commands

    def arr_indexers(self, spec):
        self.events.append(("arr", spec.name))
        return self.arr[spec.name]

    def arr_persisted_keys_match(self, spec, api_key):
        self.events.append(("arr-db", spec.name, api_key))
        return self.arr_persisted[spec.name]

    def arr_indexers_live(self, spec, indexers):
        self.events.append(("arr-live", spec.name, len(indexers)))
        return self.arr_live[spec.name]

    def lazylibrarian_providers(self):
        self.events.append(("ll",))
        return self.ll

    def shelfarr_key_matches(self, api_key):
        self.events.append(("shelfarr", api_key))
        return self.shelfarr

    def huey_key_matches(self, api_key):
        self.events.append(("huey", api_key))
        return self.huey

    def ebook_lanes_empty(self):
        self.events.append(("lanes",))
        return self.lanes_empty

    def container_identity_matches(self, service, expected):
        self.events.append(("identity", service, expected))
        return self.identities[service]


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status
        self.closed = False

    def read(self, maximum=None):
        return self.payload if maximum is None else self.payload[:maximum]

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.payload)


class RotationCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "stack"
        (self.root / "config" / "prowlarr").mkdir(parents=True)
        self.current_env = self.root / ".env"
        self.old_env = Path(self.temporary.name) / "old.env"
        self.config = self.root / "config" / "prowlarr" / "config.xml"
        self._write_current(NEW_KEY)
        self._write_old(OLD_KEY)
        self._write_config(NEW_KEY)
        self.backend = FakeBackend()

    def tearDown(self):
        self.temporary.cleanup()

    def _private_write(self, path, content):
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)

    def _write_current(self, key):
        self._private_write(
            self.current_env,
            "WYSEARR_BIND_ADDRESS=192.0.2.10\n"
            f"PROWLARR_API_KEY={key}\n",
        )

    def _write_old(self, key):
        self._private_write(self.old_env, f"PROWLARR_API_KEY={key}\n")

    def _write_config(self, key):
        self._private_write(
            self.config, f"<Config><ApiKey>{key}</ApiKey></Config>\n"
        )

    def _factory(self, root, environment):
        self.assertEqual(root, self.root)
        self.assertEqual(environment["PROWLARR_API_KEY"], NEW_KEY)
        return self.backend

    def check(self, **kwargs):
        return checker.check_rotation(
            self.root,
            self.old_env,
            backend_factory=self._factory,
            **kwargs,
        )

    def as_dict(self, checks):
        return {check.name: check.ok for check in checks}

    def write_identity_snapshot(self, payload=None):
        path = Path(self.temporary.name) / "identities.json"
        payload = payload or {
            "prowlarr": {"id": PROWLARR_ID, "started_at": PROWLARR_STARTED},
            "qbittorrent": {
                "id": QBITTORRENT_ID,
                "started_at": QBITTORRENT_STARTED,
            },
        }
        self._private_write(path, json.dumps(payload))
        return path

    def test_complete_evidence_passes_with_exact_consumers(self):
        checks = self.check()
        self.assertEqual(
            [check.name for check in checks], list(checker.BASE_CHECK_NAMES)
        )
        self.assertTrue(all(check.ok for check in checks))
        self.assertEqual(
            [event for event in self.backend.events if event[0] == "arr"],
            [
                ("arr", "sonarr"),
                ("arr", "radarr"),
                ("arr", "lidarr"),
                ("arr", "whisparr"),
            ],
        )

    def test_each_arr_requires_exact_count_and_new_key(self):
        for spec in checker.ARR_SPECS:
            with self.subTest(spec=spec.name, failure="count"):
                self.backend.arr[spec.name] = arr_payload(spec.expected_indexers - 1)
                result = self.as_dict(self.check())
                self.assertFalse(result[f"consumers:{spec.name}:credential"])
                self.assertFalse(result[f"consumers:{spec.name}:live"])
                self.backend.arr[spec.name] = arr_payload(spec.expected_indexers)
            with self.subTest(spec=spec.name, failure="credential"):
                self.backend.arr[spec.name] = arr_payload(
                    spec.expected_indexers, OLD_KEY
                )
                result = self.as_dict(self.check())
                self.assertFalse(result[f"consumers:{spec.name}:credential"])
                self.assertFalse(result[f"consumers:{spec.name}:live"])
                self.backend.arr[spec.name] = arr_payload(spec.expected_indexers)

    def test_arr_requires_exact_db_key_and_exhaustive_live_tests(self):
        spec = checker.ARR_SPECS[0]
        self.backend.arr_persisted[spec.name] = False
        result = self.as_dict(self.check())
        self.assertFalse(result[f"consumers:{spec.name}:credential"])
        self.assertTrue(result[f"consumers:{spec.name}:live"])
        self.assertIn(
            ("arr-live", spec.name, spec.expected_indexers),
            self.backend.events,
        )

        self.backend.events.clear()
        self.backend.arr_persisted[spec.name] = True
        self.backend.arr_live[spec.name] = False
        result = self.as_dict(self.check())
        self.assertTrue(result[f"consumers:{spec.name}:credential"])
        self.assertFalse(result[f"consumers:{spec.name}:live"])
        self.assertIn(
            ("arr-live", spec.name, spec.expected_indexers),
            self.backend.events,
        )

    def test_arr_rejects_disabled_or_duplicate_credential_fields(self):
        payload = arr_payload(3)
        payload[0]["enable"] = False
        self.assertFalse(
            checker._arr_consumer_matches(
                payload, expected_count=3, prowlarr_api_key=NEW_KEY
            )
        )
        payload = arr_payload(3)
        payload[0]["fields"].append({"name": "action"})
        self.assertTrue(
            checker._arr_consumer_matches(
                payload, expected_count=3, prowlarr_api_key=NEW_KEY
            )
        )
        payload[0]["downloadClientId"] = 2
        self.assertFalse(
            checker._arr_consumer_matches(
                payload, expected_count=3, prowlarr_api_key=NEW_KEY
            )
        )
        payload = arr_payload(3)
        payload[0]["fields"].append({"name": "APIKEY", "value": NEW_KEY})
        self.assertFalse(
            checker._arr_consumer_matches(
                payload, expected_count=3, prowlarr_api_key=NEW_KEY
            )
        )

    def test_persisted_arr_rows_require_every_exact_unmasked_key(self):
        rows = [
            (
                0,
                "Torznab",
                json.dumps(
                    {
                        "apiKey": NEW_KEY,
                        "apiPath": "/api",
                        "baseUrl": f"http://prowlarr:9696/{index + 1}/",
                    }
                ),
            )
            for index in range(3)
        ]
        self.assertTrue(
            checker._persisted_arr_consumers_match(
                rows, expected_count=3, prowlarr_api_key=NEW_KEY
            )
        )
        stale = list(rows)
        stale[1] = (
            0,
            "Torznab",
            json.dumps(
                {
                    "apiKey": OLD_KEY,
                    "apiPath": "/api",
                    "baseUrl": "http://prowlarr:9696/2/",
                }
            ),
        )
        self.assertFalse(
            checker._persisted_arr_consumers_match(
                stale, expected_count=3, prowlarr_api_key=NEW_KEY
            )
        )

    def test_live_backend_reads_arr_db_without_masking(self):
        spec = checker.ARR_SPECS[0]
        database = self.root / spec.database_relative
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE Indexers ("
                "Id INTEGER PRIMARY KEY, DownloadClientId INTEGER, "
                "Implementation TEXT, Settings TEXT)"
            )
            for index in range(spec.expected_indexers):
                connection.execute(
                    "INSERT INTO Indexers "
                    "(DownloadClientId, Implementation, Settings) "
                    "VALUES (?, ?, ?)",
                    (
                        0,
                        "Torznab",
                        json.dumps(
                            {
                                "apiKey": NEW_KEY,
                                "apiPath": "/api",
                                "baseUrl": (
                                    f"http://prowlarr:9696/{index + 1}/"
                                ),
                            }
                        ),
                    ),
                )
        backend = checker.LiveEvidenceBackend(self.root, {})
        self.assertTrue(backend.arr_persisted_keys_match(spec, NEW_KEY))
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE Indexers SET Settings = ? WHERE Id = 2",
                (
                    json.dumps(
                        {
                            "apiKey": OLD_KEY,
                            "apiPath": "/api",
                            "baseUrl": "http://prowlarr:9696/2/",
                        }
                    ),
                ),
            )
        self.assertFalse(backend.arr_persisted_keys_match(spec, NEW_KEY))

    def test_live_backend_tests_every_arr_resource_once(self):
        opener = FakeOpener({})
        environment = {
            "WYSEARR_BIND_ADDRESS": "192.0.2.10",
            "SONARR_PORT": "8989",
            "SONARR_API_KEY": OTHER_KEY,
        }
        backend = checker.LiveEvidenceBackend(
            self.root, environment, opener=opener
        )
        spec = checker.ARR_SPECS[0]
        payload = arr_payload(spec.expected_indexers)
        self.assertTrue(backend.arr_indexers_live(spec, payload))
        self.assertEqual(len(opener.requests), spec.expected_indexers)
        self.assertTrue(
            all(
                request.get_method() == "POST"
                and request.full_url.endswith("/api/v3/indexer/test")
                and request.get_header("X-api-key") == OTHER_KEY
                for request, _timeout in opener.requests
            )
        )

    def test_lazylibrarian_requires_exactly_two_enabled_torznab_consumers(self):
        self.assertTrue(
            checker._lazylibrarian_consumers_match(
                ll_payload(), prowlarr_api_key=NEW_KEY
            )
        )
        wrong_key = ll_payload(OLD_KEY)
        self.assertFalse(
            checker._lazylibrarian_consumers_match(
                wrong_key, prowlarr_api_key=NEW_KEY
            )
        )
        extra = ll_payload()
        extra["rss"].append({"ENABLED": "1", "API": NEW_KEY})
        self.assertFalse(
            checker._lazylibrarian_consumers_match(
                extra, prowlarr_api_key=NEW_KEY
            )
        )

    def test_reset_accepts_completed_or_pruned_but_rejects_active(self):
        terminal_or_pruned, active_zero = checker._reset_command_evidence(
            [
                {"id": 1, "name": "ResetApiKey", "status": "completed"},
                {"id": 2, "name": "Reset API Key", "status": "started"},
            ]
        )
        self.assertFalse(terminal_or_pruned)
        self.assertFalse(active_zero)
        terminal_or_pruned, active_zero = checker._reset_command_evidence(
            {
                "records": [
                    {"id": 3, "commandName": "ResetApiKey", "status": "completed"}
                ]
            }
        )
        self.assertTrue(terminal_or_pruned)
        self.assertTrue(active_zero)
        terminal_or_pruned, active_zero = checker._reset_command_evidence(
            [
                {
                    "id": 4,
                    "commandName": "ApplicationIndexerSync",
                    "status": "completed",
                }
            ]
        )
        self.assertTrue(terminal_or_pruned)
        self.assertTrue(active_zero)
        self.assertEqual(checker._reset_command_evidence({}), (False, False))
        self.assertEqual(
            checker._reset_command_evidence(
                [{"id": 5, "name": "ResetApiKey", "status": "unknown"}]
            ),
            (False, False),
        )

    def test_same_old_key_blocks_rejection_evidence(self):
        self._write_old(NEW_KEY)
        result = self.as_dict(self.check())
        self.assertFalse(result["credentials:key-changed"])
        self.assertFalse(result["prowlarr:old-auth-rejected"])
        self.assertFalse(any(event[0] == "old-auth" for event in self.backend.events))

    def test_local_divergence_blocks_auth_and_consumers(self):
        self._write_config(OTHER_KEY)
        result = self.as_dict(self.check())
        self.assertFalse(result["credentials:local-convergence"])
        self.assertFalse(result["prowlarr:new-auth"])
        self.assertFalse(result["consumers:sonarr:credential"])
        self.assertFalse(result["consumers:sonarr:live"])
        self.assertFalse(result["consumers:lazylibrarian"])
        self.assertFalse(result["consumers:shelfarr"])
        self.assertFalse(result["consumers:huey"])
        self.assertFalse(any(event[0] == "new-auth" for event in self.backend.events))
        self.assertTrue(result["qbittorrent:ebook-lanes-empty"])

    def test_optional_identity_snapshot_is_private_and_exact(self):
        path = self.write_identity_snapshot()
        checks = self.check(identity_snapshot_path=path)
        result = self.as_dict(checks)
        self.assertTrue(result["container:prowlarr-identity"])
        self.assertTrue(result["container:qbittorrent-identity"])
        observed = [
            event for event in self.backend.events if event[0] == "identity"
        ]
        self.assertEqual(
            observed[0][1:],
            (
                "prowlarr",
                checker.ContainerIdentity(PROWLARR_ID, PROWLARR_STARTED),
            ),
        )
        self.assertEqual(
            observed[1][1:],
            (
                "qbittorrent",
                checker.ContainerIdentity(QBITTORRENT_ID, QBITTORRENT_STARTED),
            ),
        )

    def test_malformed_or_nonprivate_identity_snapshot_fails_closed(self):
        malformed = self.write_identity_snapshot({"prowlarr": {}})
        checks = self.check(identity_snapshot_path=malformed)
        self.assertTrue(all(not check.ok for check in checks))
        os.chmod(malformed, 0o644)
        checks = self.check(identity_snapshot_path=malformed)
        self.assertTrue(all(not check.ok for check in checks))

    def test_backend_exception_text_never_reaches_cli_output(self):
        leaked_hash = "f" * 64

        def explode(*args, **kwargs):
            raise RuntimeError(f"{OLD_KEY}:{NEW_KEY}:{leaked_hash}")

        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = checker.main(
                ["--root", str(self.root), "--old-env", str(self.old_env), "--json"],
                collector=explode,
            )
        rendered = output.getvalue() + errors.getvalue()
        self.assertEqual(status, 1)
        self.assertNotIn(OLD_KEY, rendered)
        self.assertNotIn(NEW_KEY, rendered)
        self.assertNotIn(leaked_hash, rendered)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["passed"])
        self.assertTrue(all(value is False for value in payload["checks"].values()))

    def test_human_output_contains_only_fixed_names_and_statuses(self):
        checks = tuple(checker.Check(name, True) for name in checker.BASE_CHECK_NAMES)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            checker._emit(checks, as_json=False)
        rendered = output.getvalue()
        self.assertNotRegex(rendered, r"[0-9a-f]{32}")
        self.assertTrue(
            all(
                line.startswith("PASS: ")
                and line.removeprefix("PASS: ") in {*checker.BASE_CHECK_NAMES, "overall"}
                for line in rendered.splitlines()
            )
        )

    def test_http_credential_is_header_only(self):
        opener = FakeOpener({"apiKey": NEW_KEY})
        payload = checker._get_json(
            "http://192.0.2.10:9696/api/v1/config/host",
            NEW_KEY,
            opener=opener,
            timeout=3.0,
        )
        request, timeout = opener.requests[0]
        self.assertEqual(payload, {"apiKey": NEW_KEY})
        self.assertNotIn(NEW_KEY, request.full_url)
        self.assertEqual(request.get_header("X-api-key"), NEW_KEY)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 3.0)

    def test_qbittorrent_probe_only_logs_in_and_reads_three_lanes(self):
        events = []

        class FakeQbit:
            def login(self, username, password):
                events.append(("login", username, password))
                return True

            def categories(self):
                events.append(("categories",))
                return {
                    category: {"savePath": path}
                    for category, path in checker.EBOOK_LANE_PATHS.items()
                }

            def torrents(self, category):
                events.append(("torrents", category))
                return []

        def factory(base_url, timeout, retries):
            events.append(("factory", base_url, timeout, retries))
            return FakeQbit()

        environment = {
            "WYSEARR_BIND_ADDRESS": "192.0.2.10",
            "QBITTORRENT_PORT": "8080",
            "QBITTORRENT_USERNAME": "operator",
            "QBITTORRENT_PASSWORD": "private-password",
        }
        backend = checker.LiveEvidenceBackend(
            self.root, environment, qbit_client_factory=factory
        )
        self.assertTrue(backend.ebook_lanes_empty())
        self.assertEqual(
            [event[1] for event in events if event[0] == "torrents"],
            list(checker.EBOOK_LANES),
        )
        self.assertEqual(
            [event[0] for event in events],
            [
                "factory",
                "login",
                "categories",
                "torrents",
                "torrents",
                "torrents",
            ],
        )

    def test_lazylibrarian_credential_is_form_body_not_url(self):
        opener = FakeOpener(ll_payload())
        environment = {
            "LAZYLIBRARIAN_ADMIN_PORT": "5299",
            "LAZYLIBRARIAN_API_KEY": OTHER_KEY,
        }
        backend = checker.LiveEvidenceBackend(
            self.root, environment, opener=opener
        )
        self.assertEqual(backend.lazylibrarian_providers(), ll_payload())
        request, _ = opener.requests[0]
        self.assertNotIn(OTHER_KEY, request.full_url)
        self.assertIn(OTHER_KEY.encode("utf-8"), request.data)
        self.assertEqual(request.get_method(), "POST")
        self.assertIn(b"cmd=listProviders", request.data)


if __name__ == "__main__":
    unittest.main()
