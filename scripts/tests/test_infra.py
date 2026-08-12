import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import backup
import repair_whisparr_quality
import validate


class BackupTests(unittest.TestCase):
    def test_sqlite_backup_is_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination = root / "backup" / "source.db"
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE example (value TEXT)")
                connection.execute("INSERT INTO example VALUES ('kept')")
            backup.sqlite_backup(source, destination, anchor=root)
            with closing(sqlite3.connect(destination)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM example").fetchone()[0], "kept")
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(f"{destination}-wal").exists())
            self.assertFalse(Path(f"{destination}-shm").exists())

    def test_external_output_does_not_chmod_shared_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            output = parent / "checkpoint"
            backup.create_backup(output)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_checkpoint_includes_bounded_qbittorrent_resume_metadata_not_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / backup.QBITTORRENT_RESUME_RELATIVE
            resume.mkdir(parents=True)
            (resume / "abc.torrent").write_bytes(b"torrent metadata")
            (resume / "abc.fastresume").write_bytes(b"resume metadata")
            payload = root / "state/torrents/complete/movie.mkv"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"payload bytes")
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn(
                "config/qbittorrent/qBittorrent/BT_backup/abc.torrent", paths
            )
            self.assertIn(
                "config/qbittorrent/qBittorrent/BT_backup/abc.fastresume", paths
            )
            self.assertNotIn("state/torrents/complete/movie.mkv", paths)
            self.assertIn(
                "state/torrents/** download payloads",
                manifest["boundary"]["excludes"],
            )
            self.assertEqual(backup.verify_backup(output), manifest)

    def test_qbittorrent_resume_checkpoint_rejects_links_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / backup.QBITTORRENT_RESUME_RELATIVE
            resume.mkdir(parents=True)
            target = root / "elsewhere"
            target.write_bytes(b"not resume state")
            (resume / "unsafe.fastresume").symlink_to(target)
            output = root / "checkpoint"
            output.mkdir()
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                backup.copy_qbittorrent_resume_state(
                    output, [], stack_root=root
                )

            (resume / "unsafe.fastresume").unlink()
            (resume / "large.fastresume").write_bytes(b"1234")
            with mock.patch.object(backup, "MAX_QBITTORRENT_RESUME_FILE_BYTES", 3):
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    backup.copy_qbittorrent_resume_state(
                        output, [], stack_root=root
                    )

            (resume / "large.fastresume").unlink()
            (resume / "one.fastresume").write_bytes(b"12")
            (resume / "two.fastresume").write_bytes(b"34")
            with mock.patch.object(backup, "MAX_QBITTORRENT_RESUME_TOTAL_BYTES", 3):
                with self.assertRaisesRegex(RuntimeError, "byte limit"):
                    backup.copy_qbittorrent_resume_state(
                        output, [], stack_root=root
                    )

    def test_bounded_copy_never_overwrites_or_removes_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"new")
            destination.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                backup.copy_stable_bounded_private(
                    source, destination, maximum_bytes=10, anchor=root
                )
            self.assertEqual(destination.read_bytes(), b"keep")

    def test_verify_checkpoint_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TOKEN=private\n", encoding="utf-8")
            output = root / "checkpoint"
            with mock.patch.object(backup, "STACK_ROOT", root):
                backup.create_backup(output)
            (output / ".env").write_text("TOKEN=changed\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                backup.verify_backup(output)


class WhisparrRepairTests(unittest.TestCase):
    def test_quality_23_is_removed_recursively(self):
        value = [
            {"quality": 22, "items": []},
            {"quality": 23, "items": []},
            {"id": 1000, "items": [{"quality": 23}, {"quality": 4}]},
        ]
        self.assertEqual(
            repair_whisparr_quality.filter_quality_23(value),
            [
                {"quality": 22, "items": []},
                {"id": 1000, "items": [{"quality": 4}]},
            ],
        )

    def test_host_endpoint_reads_configurable_address_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            original = repair_whisparr_quality.STACK_ROOT
            try:
                repair_whisparr_quality.STACK_ROOT = Path(directory)
                (Path(directory) / ".env").write_text(
                    "WYSEARR_BIND_ADDRESS=10.0.0.5\nWHISPARR_PORT=7777\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    repair_whisparr_quality.host_endpoint(), ("10.0.0.5", 7777)
                )
            finally:
                repair_whisparr_quality.STACK_ROOT = original

    def test_failed_rollback_copy_validation_prevents_live_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "whisparr.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE TABLE QualityDefinitions (Quality INTEGER)"
                )
                connection.execute(
                    "CREATE TABLE QualityProfiles (Id INTEGER, Items TEXT)"
                )
                connection.execute("INSERT INTO QualityDefinitions VALUES (23)")
                connection.execute(
                    "INSERT INTO QualityProfiles VALUES (?, ?)",
                    (1, json.dumps([{"quality": 23}])),
                )

            with mock.patch.object(
                repair_whisparr_quality,
                "create_validated_rollback_copy",
                side_effect=RuntimeError("invalid rollback copy"),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid rollback copy"):
                    repair_whisparr_quality.repair_database(
                        database, root / "rollback.db"
                    )

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT Quality FROM QualityDefinitions"
                    ).fetchall(),
                    [(23,)],
                )

    def test_whisparr_rollback_copy_is_integrity_checked_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination = root / "rollback.db"
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE example (value TEXT)")
                connection.execute("INSERT INTO example VALUES ('kept')")
            with closing(sqlite3.connect(source)) as connection:
                repair_whisparr_quality.create_validated_rollback_copy(
                    connection, destination
                )
            with closing(sqlite3.connect(destination)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(f"{destination}-wal").exists())
            self.assertFalse(Path(f"{destination}-shm").exists())


class DeploymentCheckpointTests(unittest.TestCase):
    def test_deploy_pairs_pre_and_post_validation_checkpoints(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        pre = deploy.index("pre-deploy-$deployment_id")
        validation = deploy.index("python3 scripts/validate.py")
        post = deploy.index("post-deploy-$deployment_id")
        success = deploy.index("PASS: WyseARR production stack deployed")
        self.assertLess(pre, validation)
        self.assertLess(validation, post)
        self.assertLess(post, success)


class ValidationTests(unittest.TestCase):
    def test_env_parser_ignores_comments_and_preserves_equals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# ignored\nONE=1\nTOKEN=abc=def\n\n", encoding="utf-8")
            self.assertEqual(validate.load_env(path), {"ONE": "1", "TOKEN": "abc=def"})

    def test_writable_check_uses_and_removes_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            check = validate.writable_check(Path(directory), "test")
            self.assertTrue(check.ok)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_bazarr_acceptance_requires_live_integrations_english_defaults_and_all_providers(self):
        settings = {
            "general": {
                "use_sonarr": True,
                "use_radarr": True,
                "serie_default_enabled": True,
                "serie_default_profile": 7,
                "movie_default_enabled": True,
                "movie_default_profile": 7,
                "enabled_providers": [
                    "embeddedsubtitles",
                    "yifysubtitles",
                    "subf2m",
                ],
            }
        }
        profiles = [{"profileId": 7, "name": "English"}]
        status = {"sonarr_version": "4.0", "radarr_version": "6.0"}
        self.assertEqual(
            validate.bazarr_acceptance(settings, profiles, status),
            (True, True, True),
        )
        settings["general"]["enabled_providers"].remove("subf2m")
        status["radarr_version"] = ""
        self.assertEqual(
            validate.bazarr_acceptance(settings, profiles, status),
            (False, True, False),
        )

    def test_arr_download_client_acceptance_requires_managed_contract(self):
        resource = {
            "enable": True,
            "implementation": "QBittorrent",
            "removeCompletedDownloads": False,
            "fields": [
                {"name": "host", "value": "qbittorrent"},
                {"name": "port", "value": 8080},
                {"name": "useSsl", "value": False},
                {"name": "username", "value": "admin"},
                {"name": "movieCategory", "value": "movies"},
                {"name": "movieImportedCategory", "value": "movies-imported"},
            ],
        }
        arguments = {
            "username": "admin",
            "category": "movies",
            "category_fields": ("category", "movieCategory"),
            "imported_fields": ("postImportCategory", "movieImportedCategory"),
        }
        self.assertTrue(validate.arr_download_client_accepted(resource, **arguments))
        for field, bad_value in (
            ("host", "localhost"),
            ("port", 9999),
            ("movieCategory", "wrong"),
            ("movieImportedCategory", "wrong-imported"),
        ):
            changed = {**resource, "fields": [dict(item) for item in resource["fields"]]}
            next(item for item in changed["fields"] if item["name"] == field)["value"] = bad_value
            self.assertFalse(validate.arr_download_client_accepted(changed, **arguments))
        changed = dict(resource, removeCompletedDownloads=True)
        self.assertFalse(validate.arr_download_client_accepted(changed, **arguments))

    def test_post_json_ok_uses_api_key_and_json_body(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response) as opener:
            validate.post_json_ok(
                "http://service.invalid/api/v1/indexer/test",
                {"id": 4, "enable": True},
                api_key="private-key",
                timeout=60,
            )
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"id": 4, "enable": True})
        self.assertEqual(request.headers["X-api-key"], "private-key")
        self.assertEqual(opener.call_args.kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
