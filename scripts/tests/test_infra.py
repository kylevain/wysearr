import hashlib
import json
import os
import re
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


VALID_CHANNEL_INVENTORY = """\
requests:
  movies-tv: 1
  ebooks: 2
  audiobooks: 3
  manga-comics: 4
  roms: 5
  sheet-music: 6
activity:
  download-queue: 7
  request-status: 8
  recent-additions: 9
system:
  import-errors: 10
  system-health: 11
"""


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
            with mock.patch.object(backup, "STACK_ROOT", parent):
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

    def test_checkpoint_includes_complete_shelfarr_storage_without_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / backup.SHELFARR_STORAGE_RELATIVE
            blob = storage / "blobs/ab/cd/book.epub"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(b"active storage payload")
            (storage / ".secret_key_base").write_text(
                "private-secret\n", encoding="utf-8"
            )
            evaluation = root / "state" / "shelfarr-evaluation"
            evaluation.mkdir(parents=True)
            (evaluation / "results.json").write_text(
                '{"records":[]}\n', encoding="utf-8"
            )
            database = storage / "production.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE requests (title TEXT)")
                connection.execute("INSERT INTO requests VALUES ('kept')")
            output = root / "checkpoint"

            with mock.patch.object(backup, "STACK_ROOT", root):
                manifest = backup.create_backup(output)

            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("config/shelfarr/.secret_key_base", paths)
            self.assertIn("config/shelfarr/blobs/ab/cd/book.epub", paths)
            self.assertIn("config/shelfarr/production.sqlite3", paths)
            self.assertIn("state/shelfarr-evaluation/results.json", paths)
            self.assertNotIn("config/shelfarr/production.sqlite3-wal", paths)
            self.assertNotIn("config/shelfarr/production.sqlite3-shm", paths)
            self.assertIn(
                "state/shelfarr-staging/** direct-download staging payloads",
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

    def test_deploy_quiesces_owners_and_gates_shelfarr_before_start(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('case "$feature_flag" in', deploy)
        self.assertIn("evaluation_services=(sabnzbd shelfarr)", deploy)
        self.assertIn('if [ "${#evaluation_services[@]}" -gt 0 ]', deploy)
        pre_stop = deploy.index(
            "docker compose stop huey shelfarr sabnzbd",
            deploy.index("trap restore_previous_runtime EXIT"),
        )
        # Ignore the recovery hint in the EXIT trap and select the operational
        # pre-deploy checkpoint created after the owners are quiesced.
        pre_checkpoint = deploy.index("pre-deploy-$deployment_id", pre_stop)
        drain_check = deploy.index(
            "python3 scripts/bootstrap_shelfarr.py --check-drain-only"
        )
        start_sabnzbd = deploy.index(
            "docker compose up -d --remove-orphans sabnzbd", drain_check
        )
        stop_sabnzbd = deploy.index("docker compose stop sabnzbd", start_sabnzbd)
        prepare_sabnzbd = deploy.index(
            "python3 scripts/bootstrap_shelfarr.py --prepare-sab-config",
            stop_sabnzbd,
        )
        restart_sabnzbd = deploy.index("docker compose start sabnzbd", prepare_sabnzbd)
        start_shelfarr = deploy.index(
            "docker compose up -d --remove-orphans shelfarr", restart_sabnzbd
        )
        bootstrap_shelfarr = deploy.index(
            "python3 scripts/bootstrap_shelfarr.py\n", start_shelfarr
        )
        start_huey = deploy.index("docker compose up -d --build --remove-orphans bookbot huey")
        first_validation = deploy.index("python3 scripts/validate.py")
        post_stop = deploy.index(
            "docker compose stop huey shelfarr sabnzbd", pre_stop + 1
        )
        post_checkpoint = deploy.index("post-deploy-$deployment_id")
        restart_evaluation = deploy.index(
            "docker compose start sabnzbd shelfarr", post_checkpoint
        )
        restart_huey = deploy.index("docker compose start huey", post_checkpoint)
        second_validation = deploy.index(
            "python3 scripts/validate.py", first_validation + 1
        )

        self.assertLess(pre_stop, pre_checkpoint)
        self.assertLess(pre_checkpoint, drain_check)
        self.assertLess(drain_check, start_sabnzbd)
        self.assertLess(start_sabnzbd, stop_sabnzbd)
        self.assertLess(stop_sabnzbd, prepare_sabnzbd)
        self.assertLess(prepare_sabnzbd, restart_sabnzbd)
        self.assertLess(restart_sabnzbd, start_shelfarr)
        self.assertLess(start_shelfarr, bootstrap_shelfarr)
        self.assertLess(bootstrap_shelfarr, start_huey)
        self.assertLess(start_huey, first_validation)
        self.assertLess(first_validation, post_stop)
        self.assertLess(post_stop, post_checkpoint)
        self.assertLess(post_checkpoint, restart_evaluation)
        self.assertLess(restart_evaluation, restart_huey)
        self.assertLess(restart_huey, second_validation)

        evaluation_if = deploy.index(
            'if [ "${#evaluation_services[@]}" -gt 0 ]; then'
        )
        disabled_else = deploy.index("\nelse\n", evaluation_if)
        disabled_end = deploy.index("\nfi\n", disabled_else)
        disabled_branch = deploy[disabled_else:disabled_end]
        offline_prepare = disabled_branch.index(
            "python3 scripts/bootstrap_shelfarr.py --prepare-sab-config"
        )
        first_disabled_sab_start = disabled_branch.index(
            "docker compose up -d --remove-orphans sabnzbd"
        )
        self.assertIn(
            "python3 scripts/bootstrap_shelfarr.py --converge-usenet-only",
            disabled_branch,
        )
        self.assertLess(
            offline_prepare,
            first_disabled_sab_start,
        )
        self.assertLess(
            first_disabled_sab_start,
            disabled_branch.index(
                "python3 scripts/bootstrap_shelfarr.py --converge-usenet-only"
            ),
        )
        self.assertLess(
            disabled_branch.index(
                "python3 scripts/bootstrap_shelfarr.py --converge-usenet-only"
            ),
            disabled_branch.index("docker compose stop shelfarr sabnzbd"),
        )

    def test_deploy_rejects_usenet_without_shelfarr_before_runtime_mutation(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        ownership_gate = deploy.index(
            'WYSEARR_USENET_ENABLED requires SHELFARR_ENABLED=true'
        )
        first_stop = deploy.index("docker compose stop huey shelfarr sabnzbd")
        self.assertLess(ownership_gate, first_stop)

    def test_arr_recreate_preserves_compose_dependency_metadata(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn(
            "docker compose up -d --force-recreate sonarr radarr lidarr whisparr",
            deploy,
        )
        self.assertNotIn(
            "--force-recreate --no-deps sonarr radarr lidarr whisparr",
            deploy,
        )

    def test_deploy_failure_trap_tracks_all_evaluation_and_intake_services(self):
        deploy = (SCRIPTS.parent / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("trap restore_previous_runtime EXIT", deploy)
        restore = deploy.split("restore_previous_runtime() {", 1)[1].split(
            "\n}\ntrap restore_previous_runtime EXIT", 1
        )[0]
        self.assertIn('if [ "$runtime_replaced" -eq 0 ]', restore)
        self.assertIn(
            "deployment failed after runtime replacement; Huey/Shelfarr/SABnzbd are left stopped",
            restore,
        )
        self.assertIn("docker compose stop huey shelfarr sabnzbd", restore)
        for service in ("huey", "sabnzbd", "shelfarr"):
            self.assertIn(f"{service}_was_running=0", deploy)
            self.assertIn(f"service_is_running {service}", deploy)
            self.assertIn(f'if [ "${service}_was_running" -eq 1 ]', restore)
            self.assertIn(f"docker compose start {service}", restore)
            self.assertIn(f"docker compose stop {service}", restore)


class ShelfarrDeploymentTests(unittest.TestCase):
    def test_shelfarr_sab_client_follows_usenet_feature_flag(self):
        convergence = (SCRIPTS / "shelfarr_bootstrap.rb").read_text(
            encoding="utf-8"
        )
        self.assertIn('input.fetch("usenet_enabled") == true', convergence)
        self.assertIn(
            "usenet_enabled ? %w[direct usenet torrent] : %w[direct torrent]",
            convergence,
        )
        self.assertIn("enabled: usenet_enabled", convergence)

    def test_evaluation_services_are_immutable_private_and_persistent(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ghcr.io/pedro-revez-silva/shelfarr:2026.08.09.1@sha256:"
            "5e331192a8a7b55e3bee055d28403f83fd9d4977f52b6dcb11c86adcdbb70083",
            compose,
        )
        self.assertIn(
            "lscr.io/linuxserver/sabnzbd:version-5.0.4@sha256:"
            "4307a4ef4a1687c6f7dfa36fc745c1fc10b78db9916b64970b9ff682539dd03b",
            compose,
        )
        self.assertIn('"127.0.0.1:${SHELFARR_ADMIN_PORT:-5056}:80"', compose)
        self.assertIn('"127.0.0.1:${SABNZBD_ADMIN_PORT:-8085}:8080"', compose)
        self.assertIn("./config/shelfarr:/rails/storage", compose)
        self.assertIn(
            "./state/shelfarr-staging/ebooks:/ebooks/.shelfarr-staging", compose
        )
        self.assertIn("/ebooks/Books:/ebooks", compose)
        self.assertIn("/audiobooks:/audiobooks", compose)
        shelfarr = re.search(r"(?ms)^  shelfarr:\n(.*?)(?=^  \S|\Z)", compose)
        sabnzbd = re.search(r"(?ms)^  sabnzbd:\n(.*?)(?=^  \S|\Z)", compose)
        self.assertIsNotNone(shelfarr)
        self.assertIsNotNone(sabnzbd)
        self.assertNotIn("}:/downloads\n", shelfarr.group(1))
        self.assertNotIn("}:/downloads\n", sabnzbd.group(1))
        self.assertIn("/shelfarr:/downloads/shelfarr", shelfarr.group(1))
        self.assertIn("/usenet:/downloads/usenet", shelfarr.group(1))
        self.assertIn(
            "/incomplete/usenet:/downloads/incomplete/usenet",
            sabnzbd.group(1),
        )
        self.assertIn("/usenet:/downloads/usenet", sabnzbd.group(1))

    def test_huey_defaults_to_shelfarr_disabled_and_bookbot_remains_deployed(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("SHELFARR_ENABLED: ${SHELFARR_ENABLED:-false}", compose)
        self.assertIn("  bookbot:\n", compose)
        self.assertIn("dockerfile: docker/bookbot/Dockerfile", compose)
        self.assertIn(
            "HUEY_SELECTION_TTL_SECONDS: ${HUEY_SELECTION_TTL_SECONDS:-900}",
            compose,
        )

    def test_huey_is_not_health_coupled_to_evaluation_services(self):
        compose = (SCRIPTS.parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        match = re.search(r"(?ms)^  huey:\n(.*?)(?=^  \S|\Z)", compose)
        self.assertIsNotNone(match)
        huey = match.group(1)
        self.assertNotIn("shelfarr:", huey)
        self.assertNotIn("sabnzbd:", huey)

    def test_bookbot_does_not_claim_shelfarr_category(self):
        sys.path.insert(0, str(SCRIPTS / "processing"))
        from bookbot_lib.config import CATEGORY_SPECS

        self.assertNotIn("shelfarr", CATEGORY_SPECS)

    def test_disabled_container_check_rejects_restart_loop(self):
        compose_result = mock.MagicMock(returncode=0, stdout="container-id\n")
        restarting_result = mock.MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "State": {
                        "Status": "restarting",
                        "Running": True,
                        "Restarting": True,
                        "Paused": False,
                    }
                }
            ).join(["[", "]"]),
        )
        with mock.patch.object(
            validate.subprocess, "run", side_effect=[compose_result, restarting_result]
        ):
            check = validate.container_stopped_check("shelfarr")
        self.assertFalse(check.ok)
        self.assertIn("restarting", check.detail)

    def test_disabled_container_check_accepts_compose_stopped(self):
        compose_result = mock.MagicMock(returncode=0, stdout="container-id\n")
        stopped_result = mock.MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "State": {
                        "Status": "exited",
                        "Running": False,
                        "Restarting": False,
                        "Paused": False,
                    }
                }
            ).join(["[", "]"]),
        )
        with mock.patch.object(
            validate.subprocess, "run", side_effect=[compose_result, stopped_result]
        ):
            check = validate.container_stopped_check("shelfarr")
        self.assertTrue(check.ok)


class ValidationTests(unittest.TestCase):
    def test_env_parser_ignores_comments_and_preserves_equals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# ignored\nONE=1\nTOKEN=abc=def\n\n", encoding="utf-8")
            self.assertEqual(validate.load_env(path), {"ONE": "1", "TOKEN": "abc=def"})

    def test_usenet_feature_flag_is_literal_and_blank_means_disabled(self):
        for value, expected_enabled in (("", False), ("false", False), ("true", True)):
            with self.subTest(value=value):
                valid, enabled, _detail = validate._strict_feature_flag(
                    {"WYSEARR_USENET_ENABLED": value},
                    "WYSEARR_USENET_ENABLED",
                )
                self.assertTrue(valid)
                self.assertIs(enabled, expected_enabled)
        for value in (" true ", "TRUE", "1", "yes"):
            with self.subTest(value=value):
                valid, _enabled, _detail = validate._strict_feature_flag(
                    {"WYSEARR_USENET_ENABLED": value},
                    "WYSEARR_USENET_ENABLED",
                )
                self.assertFalse(valid)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("WYSEARR_USENET_ENABLED= true \n", encoding="utf-8")
            loaded = validate.load_env(path)
            self.assertEqual(loaded["WYSEARR_USENET_ENABLED"], " true ")
            self.assertFalse(
                validate._strict_feature_flag(
                    loaded, "WYSEARR_USENET_ENABLED"
                )[0]
            )
            path.write_text(
                "WYSEARR_USENET_ENABLED=true\n"
                "WYSEARR_USENET_ENABLED=true\n",
                encoding="utf-8",
            )
            duplicate = validate.load_env(path)
            self.assertFalse(
                validate._strict_feature_flag(
                    duplicate, "WYSEARR_USENET_ENABLED"
                )[0]
            )

    def test_writable_check_uses_and_removes_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            check = validate.writable_check(Path(directory), "test")
            self.assertTrue(check.ok)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_private_service_storage_rejects_group_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sabnzbd"
            path.mkdir(mode=0o700)
            self.assertTrue(
                validate.private_service_storage_check(path, "sabnzbd:test").ok
            )
            path.chmod(0o750)
            self.assertFalse(
                validate.private_service_storage_check(path, "sabnzbd:test").ok
            )

    def test_evaluation_reports_must_be_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation"
            path.mkdir(mode=0o700)
            report = path / "results.json"
            report.write_text("{}\n", encoding="utf-8")
            report.chmod(0o600)
            self.assertTrue(validate.evaluation_report_permissions_check(path).ok)
            report.chmod(0o640)
            self.assertFalse(validate.evaluation_report_permissions_check(path).ok)

    def test_channel_inventory_requires_unique_positive_request_and_lifecycle_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.yml"
            path.write_text(VALID_CHANNEL_INVENTORY, encoding="utf-8")
            inventory = validate.load_channel_inventory(path)
            self.assertEqual(inventory["activity"]["download-queue"], "7")
            self.assertTrue(validate.channel_inventory_check(path).ok)

            invalid_documents = (
                VALID_CHANNEL_INVENTORY.replace("  system-health: 11\n", ""),
                VALID_CHANNEL_INVENTORY.replace(
                    "  system-health: 11", "  system-health: invalid"
                ),
                VALID_CHANNEL_INVENTORY.replace(
                    "  system-health: 11", "  system-health: 10"
                ),
            )
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_text(document, encoding="utf-8")
                    self.assertFalse(validate.channel_inventory_check(path).ok)

    def test_huey_ready_marker_must_exist_and_contain_only_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            self.assertFalse(validate.huey_ready_check(marker).ok)
            marker.write_text("starting\n", encoding="utf-8")
            self.assertFalse(validate.huey_ready_check(marker).ok)
            marker.write_text("ready\n", encoding="utf-8")
            self.assertTrue(validate.huey_ready_check(marker).ok)

    def test_huey_selection_ttl_is_literal_and_bounded(self):
        self.assertTrue(validate.huey_selection_ttl_check({}).ok)
        for value in ("1", "900", "86400"):
            with self.subTest(value=value):
                self.assertTrue(
                    validate.huey_selection_ttl_check(
                        {"HUEY_SELECTION_TTL_SECONDS": value}
                    ).ok
                )
        for value in ("", "0", "86401", " 900 ", "15m", "1.0", "+900"):
            with self.subTest(value=value):
                self.assertFalse(
                    validate.huey_selection_ttl_check(
                        {"HUEY_SELECTION_TTL_SECONDS": value}
                    ).ok
                )

    def test_huey_database_requires_candidate_confirmation_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "huey.db"
            schema = (SCRIPTS / "huey" / "schema.sql").read_text(encoding="utf-8")
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(schema)
                connection.executescript(
                    """
                    CREATE UNIQUE INDEX requests_message_id_uq
                        ON requests(message_id);
                    CREATE UNIQUE INDEX requests_active_target_uq
                        ON requests(target_key)
                        WHERE target_key IS NOT NULL
                          AND status IN (
                              'new', 'processing', 'awaiting_selection',
                              'queued', 'complete', 'completed'
                          );
                    """
                )

            self.assertTrue(validate.huey_database_check(database).ok)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DROP INDEX candidate_confirmations_expiry_idx")
            self.assertFalse(validate.huey_database_check(database).ok)

    def test_shelfarr_storage_requires_all_databases_private_keys_and_no_discord(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            os.chmod(storage, 0o700)
            for filename in (
                "production.sqlite3",
                "production_cache.sqlite3",
                "production_queue.sqlite3",
                "production_cable.sqlite3",
            ):
                with closing(sqlite3.connect(storage / filename)) as connection, connection:
                    connection.execute("CREATE TABLE example (value TEXT)")
            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "CREATE TABLE settings (key TEXT UNIQUE, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO settings VALUES ('discord_enabled', 'false')"
                )
            for filename in (".secret_key_base", ".encryption_keys"):
                path = storage / filename
                path.write_text("private\n", encoding="utf-8")
                os.chmod(path, 0o600)

            checks = {
                check.name: check for check in validate.shelfarr_storage_checks(storage)
            }
            self.assertTrue(all(check.ok for check in checks.values()))

            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "UPDATE settings SET value = 'true' WHERE key = 'discord_enabled'"
                )
            checks = {
                check.name: check for check in validate.shelfarr_storage_checks(storage)
            }
            self.assertFalse(checks["shelfarr:native-discord"].ok)

            os.chmod(storage, 0o755)
            checks = {
                check.name: check for check in validate.shelfarr_storage_checks(storage)
            }
            self.assertFalse(checks["shelfarr:storage-permissions"].ok)

    def test_shelfarr_evaluation_configuration_enforces_isolated_clients_and_token(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            token = "shf_test_token"
            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute("CREATE TABLE settings (key TEXT, value TEXT)")
                connection.executemany(
                    "INSERT INTO settings VALUES (?, ?)",
                    (
                        ("indexer_provider", "prowlarr"),
                        ("prowlarr_url", "http://prowlarr:9696"),
                        ("prowlarr_api_key", "configured"),
                        ("preferred_download_types", '["direct","usenet","torrent"]'),
                        ("prowlarr_tags", ""),
                        ("ebook_output_path", "/ebooks"),
                        ("audiobook_output_path", "/audiobooks"),
                        ("download_local_path", "/downloads"),
                        ("immediate_search_enabled", "true"),
                        ("auto_approve_requests", "true"),
                        ("auto_select_enabled", "true"),
                        ("auto_select_confidence_threshold", "90"),
                        ("auto_select_min_seeders", "1"),
                        ("completed_download_import_mode", "copy"),
                        ("default_language", "en"),
                        ("auth_disabled", "false"),
                        ("librivox_enabled", "false"),
                        ("gutenberg_enabled", "true"),
                        ("anna_archive_enabled", "false"),
                        ("zlibrary_enabled", "false"),
                        ("ebooks_com_enabled", "false"),
                        ("discord_enabled", "false"),
                        ("discord_webhook_url", ""),
                        ("webhook_enabled", "false"),
                        ("webhook_url", ""),
                        ("telegram_enabled", "false"),
                    ),
                )
                connection.execute(
                    "CREATE TABLE download_clients "
                    "(client_type TEXT, url TEXT, category TEXT, download_path TEXT, enabled INTEGER, "
                    "password TEXT, api_key TEXT)"
                )
                connection.executemany(
                    "INSERT INTO download_clients VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            "qbittorrent",
                            "http://qbittorrent:8080",
                            "shelfarr",
                            "/downloads/shelfarr",
                            1,
                            "encrypted",
                            None,
                        ),
                        (
                            "sabnzbd",
                            "http://sabnzbd:8080",
                            "shelfarr",
                            "/downloads/usenet",
                            1,
                            None,
                            "encrypted",
                        ),
                    ),
                )
                connection.execute(
                    "CREATE TABLE api_tokens "
                    "(token_digest TEXT, scopes TEXT, revoked_at TEXT, expires_at TEXT, user_id INTEGER)"
                )
                connection.execute(
                    "CREATE TABLE users (id INTEGER, role INTEGER, deleted_at TEXT)"
                )
                connection.execute("INSERT INTO users VALUES (1, 0, NULL)")
                connection.execute(
                    "INSERT INTO api_tokens VALUES (?, ?, NULL, NULL, 1)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        '["search:read","requests:read","requests:write"]',
                    ),
                )

            checks = validate.shelfarr_configuration_checks(
                storage,
                {
                    "SHELFARR_API_TOKEN": token,
                    "WYSEARR_USENET_ENABLED": "true",
                },
            )
            self.assertTrue(all(check.ok for check in checks))

            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "UPDATE settings SET value = ? "
                    "WHERE key = 'preferred_download_types'",
                    ('["direct","torrent"]',),
                )
                connection.execute(
                    "UPDATE download_clients SET enabled = 0 "
                    "WHERE client_type = 'sabnzbd'"
                )
            disabled_checks = validate.shelfarr_configuration_checks(
                storage,
                {
                    "SHELFARR_API_TOKEN": token,
                    "WYSEARR_USENET_ENABLED": "false",
                },
            )
            self.assertTrue(all(check.ok for check in disabled_checks))

            with closing(
                sqlite3.connect(storage / "production.sqlite3")
            ) as connection, connection:
                connection.execute(
                    "UPDATE download_clients SET category = 'ebooks' "
                    "WHERE client_type = 'qbittorrent'"
                )
            checks = {
                check.name: check
                for check in validate.shelfarr_configuration_checks(
                    storage,
                    {
                        "SHELFARR_API_TOKEN": token,
                        "WYSEARR_USENET_ENABLED": "false",
                    },
                )
            }
            self.assertFalse(checks["shelfarr:download-clients"].ok)

    def test_shelfarr_direct_staging_requires_owner_only_local_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir(mode=0o700)
            self.assertTrue(validate.shelfarr_direct_staging_check(staging).ok)
            os.chmod(staging, 0o775)
            self.assertFalse(validate.shelfarr_direct_staging_check(staging).ok)

    def test_sabnzbd_evaluation_requires_isolated_paths_and_category(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                "password = private-password\n"
                "api_logging = 0\n"
                "[servers]\n",
                encoding="utf-8",
            )

            def requester(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["*", "shelfarr"]}
                return {"config": {"servers": []}}

            checks = validate.sabnzbd_configuration_checks(
                config, "8085", "operator", requester=requester
            )
            self.assertTrue(all(check.ok for check in checks))

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "/downloads/usenet", "/wrong"
                ),
                encoding="utf-8",
            )
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config, "8085", "operator", requester=requester
                )
            }
            self.assertFalse(checks["sabnzbd:paths"].ok)

            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                'password = ""\n'
                "api_logging = 1\n",
                encoding="utf-8",
            )
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config, "8085", "operator", requester=requester
                )
            }
            self.assertFalse(checks["sabnzbd:authentication"].ok)

            def no_provider(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["shelfarr"]}
                return {"config": {"servers": []}}

            checks = validate.sabnzbd_configuration_checks(
                config, "8085", "operator", requester=no_provider
            )
            provider = next(
                check for check in checks
                if check.name == "sabnzbd:provider-observation"
            )
            self.assertTrue(provider.ok)
            self.assertIn("unavailable", provider.detail)

    def test_sabnzbd_usenet_provider_requires_exact_live_tls_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                "password = private-password\n"
                "api_logging = 0\n",
                encoding="utf-8",
            )
            environment = {
                "WYSEARR_USENET_ENABLED": "true",
                "USENET_SERVER_HOST": "news.example",
                "USENET_SERVER_PORT": "563",
                "USENET_SERVER_SSL": "true",
                "USENET_SERVER_USERNAME": "reader",
                "USENET_SERVER_PASSWORD": "private-provider-password",
                "USENET_SERVER_CONNECTIONS": "8",
                "USENET_SERVER_RETENTION": "3000",
            }

            def requester(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["shelfarr"]}
                return {
                    "config": {
                        "servers": [
                            {
                                "name": "WyseARR Primary",
                                "displayname": "WyseARR Primary",
                                "host": "news.example",
                                "port": 563,
                                "username": "reader",
                                "password": "**********",
                                "connections": 8,
                                "ssl": 1,
                                "ssl_verify": 3,
                                "retention": 3000,
                                "priority": 0,
                                "enable": 1,
                            }
                        ]
                    }
                }

            tester = mock.MagicMock(return_value={"value": {"result": True}})
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=requester,
                    server_tester=tester,
                )
            }
            self.assertTrue(checks["sabnzbd:usenet-provider"].ok)
            parameters = tester.call_args.args[2]
            self.assertEqual(parameters["mode"], "config")
            self.assertEqual(parameters["name"], "test_server")
            self.assertNotIn(environment["USENET_SERVER_PASSWORD"], str(checks))

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "api_logging = 0", "api_logging = 1"
                ),
                encoding="utf-8",
            )
            tester.reset_mock()
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=requester,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:authentication"].ok)
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)
            tester.assert_not_called()
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "api_logging = 1", "api_logging = 0"
                ),
                encoding="utf-8",
            )

            tester.return_value = {
                "value": {"result": False, "message": "rejected"}
            }
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=requester,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

            def duplicate_provider(url):
                value = requester(url)
                if "mode=get_config" in url:
                    value["config"]["servers"].append(
                        {**value["config"]["servers"][0], "name": "wysearr primary"}
                    )
                return value

            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=duplicate_provider,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

            def wrong_priority(url):
                value = requester(url)
                if "mode=get_config" in url:
                    value["config"]["servers"][0]["priority"] = 99
                return value

            tester.return_value = {"value": {"result": True}}
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    environment,
                    requester=wrong_priority,
                    server_tester=tester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

    def test_sabnzbd_usenet_provider_rejects_partial_or_disabled_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "download_dir = /downloads/incomplete/usenet\n"
                "complete_dir = /downloads/usenet\n"
                "api_key = private\n"
                "username = operator\n"
                "password = private-password\n"
                "api_logging = 0\n",
                encoding="utf-8",
            )

            def requester(url):
                if "mode=version" in url:
                    return {"version": "5.0.4"}
                if "mode=get_cats" in url:
                    return {"categories": ["shelfarr"]}
                return {
                    "config": {
                        "servers": [
                            {
                                "name": "WyseARR Primary",
                                "host": "news.example",
                                "enable": 1,
                            }
                        ]
                    }
                }

            enabled = {
                "WYSEARR_USENET_ENABLED": "true",
                "USENET_SERVER_HOST": "news.example",
            }
            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config, "8085", "operator", enabled, requester=requester
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

            checks = {
                check.name: check
                for check in validate.sabnzbd_configuration_checks(
                    config,
                    "8085",
                    "operator",
                    {"WYSEARR_USENET_ENABLED": "false"},
                    requester=requester,
                )
            }
            self.assertFalse(checks["sabnzbd:usenet-provider"].ok)

    def test_managed_newznab_requires_live_tag_isolation_and_private_contract(self):
        environment = {
            "NEWZNAB_INDEXER_NAME": "WyseARR Books",
            "NEWZNAB_BASE_URL": "https://indexer.example",
            "NEWZNAB_API_PATH": "/api",
            "NEWZNAB_API_KEY": "private-indexer-key",
        }
        indexer = {
            "id": 17,
            "name": "WyseARR Books",
            "enable": True,
            "implementation": "Newznab",
            "configContract": "NewznabSettings",
            "protocol": "usenet",
            "priority": 20,
            "tags": [4],
            "capabilities": {
                "categories": [
                    {
                        "id": 3000,
                        "subCategories": [{"id": 3030, "subCategories": []}],
                    },
                    {
                        "id": 7000,
                        "subCategories": [{"id": 7020, "subCategories": []}],
                    },
                ]
            },
            "fields": [
                {"name": "baseUrl", "value": "https://indexer.example/"},
                {"name": "apiPath", "value": "/api"},
                {"name": "apiKey", "value": "(removed)"},
            ],
        }
        torrent = {
            "id": 18,
            "name": "Torrent Fallback",
            "enable": True,
            "implementation": "Cardigann",
            "protocol": "torrent",
            "tags": [5],
        }
        tags = [
            {"id": 4, "label": "shelfarr"},
            {"id": 5, "label": "wysearr-arr"},
        ]
        applications = [
            {"name": name, "implementation": name, "tags": [5]}
            for name in ("Sonarr", "Radarr", "Lidarr", "Whisparr")
        ]
        check = validate.prowlarr_managed_newznab_check(
            [indexer, torrent],
            tags,
            applications,
            {17},
            environment,
            enabled=True,
        )
        self.assertTrue(check.ok)
        self.assertNotIn(environment["NEWZNAB_API_KEY"], check.detail)

        for changed_indexers, changed_apps, live_ids in (
            ([{**indexer, "tags": []}, torrent], applications, {17}),
            ([indexer, {**torrent, "tags": []}], applications, {17}),
            ([indexer, {**torrent, "tags": [4, 5]}], applications, {17}),
            ([{**indexer, "priority": 99}, torrent], applications, {17}),
            (
                [indexer, torrent],
                [
                    {**app, "tags": []}
                    if app["name"] == "Radarr"
                    else app
                    for app in applications
                ],
                {17},
            ),
            (
                [{**indexer, "tags": [4, 9]}, torrent],
                [
                    {**app, "tags": [5, 9]}
                    if app["name"] == "Sonarr"
                    else app
                    for app in applications
                ],
                {17},
            ),
            ([indexer, torrent], applications, set()),
            (
                [
                    {
                        **indexer,
                        "capabilities": {
                            "categories": [
                                {"id": 2000, "subCategories": []}
                            ]
                        },
                    },
                    torrent,
                ],
                applications,
                {17},
            ),
        ):
            with self.subTest(
                applications=changed_apps,
                live=live_ids,
            ):
                self.assertFalse(
                    validate.prowlarr_managed_newznab_check(
                        changed_indexers,
                        tags,
                        changed_apps,
                        live_ids,
                        environment,
                        enabled=True,
                    ).ok
                )

    def test_managed_newznab_disabled_state_only_rejects_managed_indexer(self):
        disabled = {
            "id": 17,
            "name": "WyseARR Books",
            "enable": False,
            "implementation": "Newznab",
            "protocol": "usenet",
        }
        self.assertTrue(
            validate.prowlarr_managed_newznab_check(
                [disabled], [], [], set(), {}, enabled=False
            ).ok
        )
        self.assertFalse(
            validate.prowlarr_managed_newznab_check(
                [disabled, {**disabled, "id": 18, "name": "wysearr books"}],
                [],
                [],
                set(),
                {},
                enabled=False,
            ).ok
        )
        self.assertFalse(
            validate.prowlarr_managed_newznab_check(
                [{**disabled, "enable": True}], [], [], set(), {}, enabled=False
            ).ok
        )
        self.assertFalse(
            validate.prowlarr_managed_newznab_check(
                [disabled],
                [],
                [],
                set(),
                {"NEWZNAB_INDEXER_NAME": "Custom Books"},
                enabled=False,
            ).ok
        )
        self.assertTrue(
            validate.prowlarr_managed_newznab_check(
                [
                    {
                        **disabled,
                        "name": "Unrelated Usenet",
                        "enable": True,
                    }
                ],
                [],
                [],
                set(),
                {},
                enabled=False,
            ).ok
        )

    def test_stopped_sab_requires_managed_provider_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sabnzbd.ini"
            config.write_text(
                "[misc]\n"
                "api_key = private\n"
                "[servers]\n"
                "[[WyseARR Primary]]\n"
                "displayname = WyseARR Primary\n"
                "enable = 1\n"
                "host = news.example\n",
                encoding="utf-8",
            )
            self.assertFalse(
                validate.sabnzbd_stopped_managed_provider_check(config).ok
            )
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "enable = 1", "enable = 0"
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                validate.sabnzbd_stopped_managed_provider_check(config).ok
            )

            config.write_text(
                "[servers]\n"
                "[[Unrelated Provider]]\n"
                "enable = 1\n",
                encoding="utf-8",
            )
            self.assertTrue(
                validate.sabnzbd_stopped_managed_provider_check(config).ok
            )
    def test_shelfarr_runtime_parses_client_results_after_rails_logs(self):
        runner = mock.MagicMock()
        runner.return_value = mock.MagicMock(
            returncode=0,
            stdout=(
                "[info] qBittorrent connection successful\n"
                "[info] SABnzbd connection successful\n"
                "WYSEARR_CLIENT_RESULTS="
                '[["qbittorrent","shelfarr",true],'
                '["sabnzbd","shelfarr",true]]\n'
            ),
        )
        checks = validate.shelfarr_runtime_checks(
            "5056",
            "shf_token",
            True,
            runner=runner,
            requester=lambda *_args, **_kwargs: {"requests": []},
        )
        self.assertTrue(all(check.ok for check in checks))

    def test_shelfarr_runtime_rejects_enabled_sab_when_usenet_is_disabled(self):
        runner = mock.MagicMock()
        runner.return_value = mock.MagicMock(
            returncode=0,
            stdout=(
                "WYSEARR_CLIENT_RESULTS="
                '[["qbittorrent","shelfarr",true],'
                '["sabnzbd","shelfarr",true]]\n'
            ),
        )
        checks = validate.shelfarr_runtime_checks(
            "5056",
            "shf_token",
            False,
            runner=runner,
            requester=lambda *_args, **_kwargs: {"requests": []},
        )
        self.assertFalse(
            next(check for check in checks if check.name == "shelfarr:client-connectivity").ok
        )

    def test_arr_native_discord_check_reads_notification_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "arr.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE Notifications (
                        Name TEXT, Implementation TEXT,
                        ConfigContract TEXT, Settings TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO Notifications VALUES (?, ?, ?, ?)",
                    ("Email", "Email", "EmailSettings", "{}"),
                )

            self.assertTrue(
                validate.arr_native_discord_check("radarr", database).ok
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO Notifications VALUES (?, ?, ?, ?)",
                    ("Lifecycle", "Discord", "DiscordSettings", "{}"),
                )
            self.assertFalse(
                validate.arr_native_discord_check("radarr", database).ok
            )

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DELETE FROM Notifications WHERE Name = 'Lifecycle'")
                connection.execute(
                    "INSERT INTO Notifications VALUES (?, ?, ?, ?)",
                    (
                        "Generic webhook",
                        "Webhook",
                        "WebhookSettings",
                        '{"url":"https://discord.com/api/webhooks/1/private"}',
                    ),
                )
            self.assertFalse(
                validate.arr_native_discord_check("radarr", database).ok
            )

    def test_bazarr_native_discord_and_external_webhook_must_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "bazarr.db"
            config = root / "config.yaml"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE table_settings_notifier (
                        name TEXT, enabled INTEGER, url TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO table_settings_notifier VALUES (?, ?, ?)",
                    ("Discord", 0, None),
                )
            config.write_text(
                "general:\n  use_external_webhook: false\n", encoding="utf-8"
            )
            self.assertTrue(
                validate.bazarr_native_discord_check(database, config).ok
            )

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE table_settings_notifier SET enabled = 1 "
                    "WHERE name = 'Discord'"
                )
            self.assertFalse(
                validate.bazarr_native_discord_check(database, config).ok
            )

            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE table_settings_notifier SET enabled = 0 "
                    "WHERE name = 'Discord'"
                )
            config.write_text(
                "general:\n  use_external_webhook: true\n", encoding="utf-8"
            )
            self.assertFalse(
                validate.bazarr_native_discord_check(database, config).ok
            )

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
