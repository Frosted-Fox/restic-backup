import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import restic_node_backup as app


class ConfigurationTests(unittest.TestCase):
    def test_config_requires_absolute_source_but_allows_disabled_encryption(self):
        with self.assertRaises(app.AppError):
            app.Config(source="relative", repository="s3:https://example/bucket", password="pw").validate()
        app.Config(repository="s3:https://example/bucket").validate()

    def test_config_rejects_invalid_schedule_and_types(self):
        with self.assertRaises(app.AppError):
            app.Config(repository="/tmp/restic", password="pw", interval_hours=0).validate()
        with self.assertRaises(app.AppError):
            app.Config(repository="/tmp/restic", password="pw", keep_last=True).validate()

    def test_config_rejects_unknown_time_zone(self):
        with self.assertRaisesRegex(app.AppError, "IANA name"):
            app.Config(repository="/tmp/restic", timezone_name="Not/AZone").validate()

    def test_time_zone_search_matches_country_and_zone_names(self):
        options = [
            ("UTC", "UTC"),
            ("Asia/Kolkata", "India — Asia/Kolkata"),
            ("America/Indiana/Indianapolis", "United States — America/Indiana/Indianapolis"),
            ("Europe/London", "United Kingdom — Europe/London"),
        ]
        self.assertEqual(app._timezone_matches(options, "india"), [options[1]])
        self.assertEqual(app._timezone_matches(options, "Europe/London"), [options[3]])

    def test_dashboard_timestamps_use_selected_zone(self):
        self.assertEqual(
            app._gui_timestamp_text("2026-10-15T09:00:00+00:00", "Asia/Kolkata"),
            "2026-10-15 14:30:00 IST",
        )
        self.assertEqual(
            app._localized_log_line("2026-10-15 09:00:00,123 INFO Backup completed.", "Asia/Kolkata"),
            "2026-10-15 14:30:00,123 IST INFO Backup completed.",
        )

    def test_cli_time_zone_selector_returns_selected_zone(self):
        options = [
            ("Asia/Kolkata", "India — Asia/Kolkata"),
            ("Europe/London", "United Kingdom — Europe/London"),
        ]
        with patch.object(app, "_timezone_options", return_value=options), \
             patch.object(app, "_prompt", return_value="India"), \
             patch("builtins.input", return_value="1"):
            self.assertEqual(app._select_timezone_cli("UTC"), "Asia/Kolkata")

    def test_timezone_picker_filters_while_typing_and_shows_local_time_preview(self):
        class FakeScreen:
            def __init__(self, keys):
                self.keys = iter(keys)
                self.rendered = []

            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 15, 100

            def addstr(self, _row, _column, text, *_args):
                self.rendered.append(str(text))

            @staticmethod
            def refresh():
                pass

            def getch(self):
                return next(self.keys)

        options = [
            ("Asia/Kolkata", "India — Asia/Kolkata"),
            ("Europe/London", "United Kingdom — Europe/London"),
        ]
        screen = FakeScreen([ord(char) for char in "India"] + [10])
        with patch.object(app, "_timezone_options", return_value=options), \
             patch.object(app.curses, "color_pair", return_value=0), \
             patch.object(app, "now_utc", return_value=app.datetime(2026, 1, 1, tzinfo=app.timezone.utc)):
            selected = app._timezone_picker(screen, "UTC")
        self.assertEqual(selected, "Asia/Kolkata")
        self.assertTrue(any("India — Asia/Kolkata" in line for line in screen.rendered))
        self.assertTrue(any("2026-01-01 05:30:00 IST (UTC+05:30)" in line for line in screen.rendered))

    def test_timezone_picker_backspace_restores_current_zone(self):
        class FakeScreen:
            def __init__(self, keys):
                self.keys = iter(keys)

            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 15, 100

            @staticmethod
            def addstr(*_args):
                pass

            @staticmethod
            def refresh():
                pass

            def getch(self):
                return next(self.keys)

        options = [
            ("Asia/Kolkata", "India — Asia/Kolkata"),
            ("Europe/London", "United Kingdom — Europe/London"),
        ]
        keys = [ord("I"), ord("n"), app.curses.KEY_BACKSPACE, 10]
        with patch.object(app, "_timezone_options", return_value=options), \
             patch.object(app.curses, "color_pair", return_value=0):
            selected = app._timezone_picker(FakeScreen(keys), "Europe/London")
        self.assertEqual(selected, "Europe/London")

    def test_configuration_editor_edits_one_field_and_saves_masked_secrets(self):
        class FakeScreen:
            def __init__(self, keys):
                self.keys = iter(keys)
                self.rendered = []

            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 30, 120

            def addstr(self, _row, _column, text, *_args):
                self.rendered.append(str(text))

            @staticmethod
            def refresh():
                pass

            def getch(self):
                return next(self.keys)

        original = app.Config(
            source="/var/lib/pterodactyl",
            repository="s3:https://s3.example.net/backups",
            password="never-display-this",
            access_key="private-access-key",
            secret_key="private-secret-key",
        )
        keys = [app.curses.KEY_DOWN] * 3 + [10]
        keys += [app.curses.KEY_DOWN] * 8 + [10]
        screen = FakeScreen(keys)
        saved = []
        with patch.object(app, "load_config", return_value=original), \
             patch.object(app, "_tui_prompt", return_value="/srv/game-backups"), \
             patch.object(app, "save_config", side_effect=saved.append), \
             patch.object(app.curses, "color_pair", return_value=0):
            result = app._configure_tui(screen)

        self.assertIn("saved securely", result)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].source, "/srv/game-backups")
        self.assertEqual(saved[0].password, "never-display-this")
        rendered = "\n".join(screen.rendered)
        self.assertNotIn("never-display-this", rendered)
        self.assertNotIn("private-access-key", rendered)
        self.assertNotIn("private-secret-key", rendered)

    def test_configuration_editor_cancel_does_not_save(self):
        class FakeScreen:
            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 30, 120

            @staticmethod
            def addstr(*_args):
                pass

            @staticmethod
            def refresh():
                pass

            @staticmethod
            def getch():
                return ord("q")

        with patch.object(app, "load_config", return_value=app.Config(repository="/tmp/repo")), \
             patch.object(app, "save_config") as save, \
             patch.object(app.curses, "color_pair", return_value=0):
            result = app._configure_tui(FakeScreen())
        self.assertIn("cancelled", result)
        save.assert_not_called()

    def test_dashboard_renders_branded_status_panels_and_watermark(self):
        class FakeScreen:
            def __init__(self):
                self.rendered = []

            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 30, 100

            def addstr(self, _row, _column, text, *_args):
                self.rendered.append(str(text))

            @staticmethod
            def refresh():
                pass

            @staticmethod
            def timeout(_value):
                pass

            @staticmethod
            def getch():
                return ord("q")

        screen = FakeScreen()
        config = app.Config(
            source="/var/lib/pterodactyl/with-a-long-server-path",
            repository="s3:https://storage.example.net/game-backups/node-production",
        )
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app.StateStore, "read", return_value={
                 "status": "success", "last_success": "2026-01-01T00:00:00+00:00",
                 "next_run": "2026-01-02T00:00:00+00:00",
                 "storage_bytes": 1024 ** 3,
                 "storage_repository": config.repository,
                 "storage_checked_at": "2026-01-02T00:00:00+00:00",
             }), \
             patch.object(app, "_maybe_refresh_storage", return_value=None), \
             patch.object(app.curses, "curs_set"), \
             patch.object(app.curses, "start_color"), \
             patch.object(app.curses, "use_default_colors"), \
             patch.object(app.curses, "init_pair"), \
             patch.object(app.curses, "color_pair", return_value=0):
            app._tui(screen)
        output = "\n".join(screen.rendered)
        self.assertIn("RYBERNT  /  RESTIC NODE BACKUP", output)
        self.assertIn("QUICK ACTIONS", output)
        self.assertIn("ACTIVITY", output)
        self.assertIn(app.WATERMARK, output)
        joined_output = "".join(screen.rendered).replace(" ", "")
        self.assertIn(config.source, joined_output)
        self.assertIn(config.repository, joined_output)
        self.assertIn("1.0 GiB used", output)
        self.assertNotIn("quota unknown", output)

    def test_quitting_dashboard_does_not_cancel_persistent_backup(self):
        class FakeScreen:
            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 30, 100

            @staticmethod
            def addstr(*_args):
                pass

            @staticmethod
            def refresh():
                pass

            @staticmethod
            def timeout(_value):
                pass

            @staticmethod
            def getch():
                return ord("q")

        state = {
            "status": "running",
            "task_status": "running",
            "operation": "Preparing backup",
        }
        with patch.object(app, "load_config", return_value=app.Config(repository="/tmp/repo")), \
             patch.object(app.StateStore, "read", return_value=state), \
             patch.object(app.StateStore, "update") as update, \
             patch.object(app, "_maybe_refresh_storage", return_value=None), \
             patch.object(app.curses, "curs_set"), \
             patch.object(app.curses, "start_color"), \
             patch.object(app.curses, "use_default_colors"), \
             patch.object(app.curses, "init_pair"), \
             patch.object(app.curses, "color_pair", return_value=0):
            app._tui(FakeScreen())
        update.assert_not_called()

    def test_s3_storage_usage_sums_object_sizes_under_repository_prefix(self):
        client = Mock()
        paginator = Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Size": 100}, {"Size": 250}]},
            {"Contents": [{"Size": 75}]},
        ]
        client.get_paginator.return_value = paginator
        session = Mock()
        session.client.return_value = client

        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.Session = Mock(return_value=session)
        fake_botocore = types.ModuleType("botocore")
        fake_botocore.__path__ = []
        fake_config = types.ModuleType("botocore.config")
        fake_config.Config = lambda **kwargs: kwargs
        fake_exceptions = types.ModuleType("botocore.exceptions")
        fake_exceptions.BotoCoreError = type("BotoCoreError", (Exception,), {})
        fake_exceptions.ClientError = type("ClientError", (Exception,), {})
        config = app.Config(
            repository="s3:https://storage.example.net/backups/game-node",
            access_key="access",
            secret_key="secret",
            region="eu-central-1",
        )
        with patch.dict(sys.modules, {
            "boto3": fake_boto3,
            "botocore": fake_botocore,
            "botocore.config": fake_config,
            "botocore.exceptions": fake_exceptions,
        }):
            used = app.s3_repository_storage_bytes(config)

        self.assertEqual(used, 425)
        client.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket="backups", Prefix="game-node/")
        session.client.assert_called_once_with(
            "s3",
            endpoint_url="https://storage.example.net",
            config={"s3": {"addressing_style": "path"}},
        )

    def test_config_is_atomically_written_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "config.json"
            config = app.Config(repository="/tmp/restic", password="not-printed")
            app.save_config(config, path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
            with patch.object(app.os, "chmod") as chmod:
                self.assertEqual(app.load_config(path), config)
                chmod.assert_not_called()

    def test_check_source_rejects_missing_directory(self):
        config = app.Config(source="/directory/that/does/not/exist", repository="/tmp/repo", password="pw")
        with patch.object(app, "load_config", return_value=config):
            with self.assertRaisesRegex(app.AppError, "Re-run configure"):
                app.check_source()

    def test_s3_endpoint_and_bucket_form_restic_repository_url(self):
        self.assertEqual(
            app.s3_repository("s3.fra.databucket.eu", "game-backups"),
            "s3:https://s3.fra.databucket.eu/game-backups",
        )
        self.assertEqual(
            app.s3_repository("https://s3.example.net:9443/", "game-backups"),
            "s3:https://s3.example.net:9443/game-backups",
        )

    def test_s3_endpoint_and_bucket_reject_invalid_inputs(self):
        for endpoint, bucket in (
            ("", "bucket-name"),
            ("https://user:password@s3.example.net", "bucket-name"),
            ("https://s3.example.net/path", "bucket-name"),
            ("https://s3.example.net", "../other-bucket"),
            ("https://s3.example.net", "Bad_Bucket"),
        ):
            with self.subTest(endpoint=endpoint, bucket=bucket):
                with self.assertRaises(app.AppError):
                    app.s3_repository(endpoint, bucket)


class StateTests(unittest.TestCase):
    def test_state_updates_survive_reload_and_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = app.StateStore(path)
            state.update(status="failed", failure_count=2)
            self.assertEqual(app.StateStore(path).read()["failure_count"], 2)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_parse_time_normalizes_timezone_less_values(self):
        parsed = app.parse_time("2026-01-01T00:00:00")
        self.assertEqual(parsed.tzinfo, app.timezone.utc)

    def test_interrupted_running_task_is_marked_failed_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.StateStore(Path(directory) / "state.json")
            store.update(
                status="running",
                operation="Backing up",
                operation_pid=None,
                progress={"files_done": 3},
                active_detail="done",
                cancel_requested=True,
                failure_count=1,
            )
            fixed_now = app.datetime(2026, 1, 1, tzinfo=app.timezone.utc)
            with patch.object(app, "now_utc", return_value=fixed_now):
                app._recover_interrupted_task(store, app.logging.getLogger("test"))

            state = store.read()
            self.assertEqual(state["status"], "failed")
            self.assertIsNone(state["operation"])
            self.assertIsNone(state["operation_pid"])
            self.assertEqual(state["progress"], {})
            self.assertIsNone(state["active_detail"])
            self.assertFalse(state["cancel_requested"])
            self.assertEqual(state["failure_count"], 2)
            self.assertEqual(
                app.parse_time(state["retry_at"]),
                fixed_now + app.timedelta(minutes=1),
            )

    def test_interrupted_manual_check_is_failed_without_backup_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.StateStore(Path(directory) / "state.json")
            store.update(status="running", operation="Restic check", operation_pid=1234)
            with patch.object(app, "_task_process_is_alive", return_value=False):
                app._recover_interrupted_task(store, app.logging.getLogger("test"))
            state = store.read()
            self.assertEqual(state["status"], "waiting")
            self.assertIsNone(state["retry_at"])
            self.assertIn("Restic check", state["last_operation_error"])

    def test_interrupted_manual_check_preserves_last_backup_success_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.StateStore(Path(directory) / "state.json")
            store.update(
                status="running",
                operation="Restic check",
                operation_pid=1234,
                last_success="2026-10-15T08:58:33+00:00",
                last_error=None,
            )
            with patch.object(app, "_task_process_is_alive", return_value=False):
                app._recover_interrupted_task(store, app.logging.getLogger("test"))
            state = store.read()
            self.assertEqual(state["status"], "success")
            self.assertIsNone(state["last_error"])
            self.assertIn("Restic check", state["last_operation_error"])

    def test_old_interrupted_check_failure_is_migrated_to_backup_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.StateStore(Path(directory) / "state.json")
            store.update(
                status="failed",
                operation=None,
                last_success="2026-10-15T08:58:33+00:00",
                last_error="Previous task (Restic check) stopped before recording completion.",
                retry_at=None,
            )
            app._recover_interrupted_task(store, app.logging.getLogger("test"))
            state = store.read()
            self.assertEqual(state["status"], "success")
            self.assertIsNone(state["last_error"])
            self.assertIn("Restic check", state["last_operation_error"])

    def test_old_manual_operation_failure_does_not_replace_backup_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.StateStore(Path(directory) / "state.json")
            store.update(
                status="failed",
                operation=None,
                failure_count=0,
                last_success="2026-10-15T08:58:33+00:00",
                last_error=None,
                retry_at=None,
            )
            app._recover_interrupted_task(store, app.logging.getLogger("test"))
            self.assertEqual(store.read()["status"], "success")

    def test_scheduler_does_not_recover_task_from_live_process(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.StateStore(Path(directory) / "state.json")
            store.update(status="running", operation_pid=1234)
            with patch.object(app, "_task_process_is_alive", return_value=True) as alive:
                app._recover_interrupted_task(store, app.logging.getLogger("test"))
            alive.assert_called_once_with(1234)
            self.assertEqual(store.read()["status"], "running")

    def test_scheduler_runs_due_retry_before_future_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            store = app.StateStore(state_path)
            store.update(
                status="failed",
                retry_at="2026-01-01T00:01:00+00:00",
                next_run="2026-01-01T01:00:00+00:00",
            )
            stop = app.threading.Event()
            config = app.Config(repository="/tmp/repo", password="secret", interval_hours=1)

            def successful_retry():
                stop.set()
                return True

            with patch.object(app, "STATE_PATH", state_path), \
                 patch.object(app, "create_logger", return_value=app.logging.getLogger("test-scheduler-retry")), \
                 patch.object(app, "load_config", return_value=config), \
                 patch.object(app, "now_utc", return_value=app.datetime(2026, 1, 1, 0, 5, tzinfo=app.timezone.utc)), \
                 patch.object(app, "run_backup", side_effect=successful_retry) as run_backup:
                app.run_scheduler(stop)

            run_backup.assert_called_once()
            self.assertEqual(store.read()["next_run"], "2026-01-01T01:00:00+00:00")


class ResticCommandTests(unittest.TestCase):
    def test_restic_progress_json_is_not_written_to_activity_log(self):
        self.assertTrue(app._is_restic_progress('{"message_type":"status","percent_done":0}'))
        self.assertTrue(app._is_restic_progress('{"message_type":"verbose_status","current_files":[]}'))
        self.assertFalse(app._is_restic_progress('{"message_type":"summary","snapshot_id":"abc"}'))
        self.assertFalse(app._is_restic_progress("Applying retention policy"))

    def test_restore_target_requires_absolute_path_and_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            config = app.Config(source=str(source), repository="/tmp/repo")
            with self.assertRaisesRegex(app.AppError, "absolute restore path"):
                app._validate_restore_target("relative/restore", config)
            with self.assertRaisesRegex(app.AppError, "Parent folder .* does not exist"):
                app._validate_restore_target(str(root / "missing-parent" / "restore"), config)

    def test_restore_target_rejects_files_and_live_source_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            file_target = root / "target-file"
            file_target.write_text("not a directory")
            config = app.Config(source=str(source), repository="/tmp/repo")
            with self.assertRaisesRegex(app.AppError, "not a directory"):
                app._validate_restore_target(str(file_target), config)
            with self.assertRaisesRegex(app.AppError, "overlaps the live backup source"):
                app._validate_restore_target(str(source / "restore"), config)

    def test_restore_target_allows_filesystem_root(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            config = app.Config(source=str(source), repository="/tmp/repo")
            with patch.object(app.os, "access", return_value=True):
                self.assertEqual(app._validate_restore_target("/", config), Path("/"))

    def test_restore_target_prompt_reports_bad_path_then_retries(self):
        class FakeScreen:
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            config = app.Config(source=str(source), repository="/tmp/repo")
            target = root / "restore"
            with patch.object(app, "_tui_prompt", side_effect=["wrong/relative", str(target)]), \
                 patch.object(app, "_show_output") as show_error:
                result = app._prompt_restore_target(FakeScreen(), "/srv/restore", config)
            self.assertEqual(result, (target, True))
            show_error.assert_called_once()
            self.assertIn("absolute restore path", show_error.call_args.args[1])

    def test_restore_confirmation_retries_on_typo(self):
        class FakeScreen:
            pass

        with patch.object(app, "_tui_prompt", side_effect=["RESTOR", "RESTORE"]), \
             patch.object(app, "_show_output") as show_error:
            confirmed = app._confirm_restore(
                FakeScreen(), "abcdef123456", Path("/tmp/restore"), create_target=False
            )
        self.assertTrue(confirmed)
        show_error.assert_called_once()
        self.assertIn("Type RESTORE exactly", show_error.call_args.args[1])

    def test_restore_to_root_requires_specific_confirmation(self):
        class FakeScreen:
            pass

        with patch.object(app, "_tui_prompt", side_effect=["RESTORE", "RESTORE TO /"]) as prompt_mock, \
             patch.object(app, "_show_output") as show_error:
            confirmed = app._confirm_restore(
                FakeScreen(), "abcdef123456", Path("/"), create_target=False
            )
        self.assertTrue(confirmed)
        show_error.assert_called_once()
        prompt = prompt_mock.call_args.args[1]
        self.assertIn("overwrite live files", prompt)

    def test_restore_snapshot_creates_only_final_directory_and_calls_restic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "restore"
            config = app.Config(source=str(source), repository="/tmp/repo")
            with patch.object(app, "load_config", return_value=config), \
                 patch.object(app, "create_logger", return_value=app.logging.getLogger("test-restore")), \
                 patch.object(app, "_run_restic") as restic:
                message = app.restore_snapshot("a" * 64, str(target))
            self.assertTrue(target.is_dir())
            self.assertIn(str(target), message)
            self.assertEqual(
                restic.call_args.args[1],
                ["restore", "a" * 64, "--target", str(target)],
            )

    def test_server_uuid_must_be_a_canonical_uuid(self):
        with self.assertRaisesRegex(app.AppError, "valid server UUID"):
            app._server_volume_path("../../etc")
        self.assertEqual(
            app._server_volume_path("2D976043-3B60-40CB-915B-BFAE65896C91"),
            Path("/var/lib/pterodactyl/volumes/2d976043-3b60-40cb-915b-bfae65896c91"),
        )

    def test_find_server_snapshots_returns_only_exact_volume_matches(self):
        server_uuid = "2d976043-3b60-40cb-915b-bfae65896c91"
        matching_id = "a" * 64
        snapshots = [
            {"id": matching_id, "time": "2026-01-02T00:00:00Z"},
            {"id": "b" * 64, "time": "2026-01-01T00:00:00Z"},
        ]
        results = [
            {
                "matches": [{
                    "path": f"/var/lib/pterodactyl/volumes/{server_uuid}/world",
                }],
                "hits": 1,
                "snapshot": matching_id,
            },
            {
                "matches": [{
                    "path": f"/var/lib/pterodactyl/volumes/{server_uuid}-old/world",
                }],
                "hits": 1,
                "snapshot": "b" * 64,
            },
        ]
        config = app.Config(repository="/tmp/repo")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-server-find")), \
             patch.object(app, "_run_restic", side_effect=[
                 subprocess.CompletedProcess([], 0, json.dumps(results)),
                 subprocess.CompletedProcess([], 0, json.dumps(snapshots)),
             ]) as restic:
            found = app.find_server_snapshots(server_uuid)
        self.assertEqual(found, snapshots[:1])
        self.assertEqual(restic.call_args_list[0].args[1], ["find", "--json", server_uuid])
        self.assertEqual(restic.call_args_list[1].args[1], ["snapshots", "--json"])

    def test_restore_server_snapshot_includes_only_requested_volume(self):
        server_uuid = "2d976043-3b60-40cb-915b-bfae65896c91"
        config = app.Config(repository="/tmp/repo")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-server-restore")), \
             patch.object(app, "_run_restic") as restic:
            message = app.restore_server_snapshot("a" * 64, server_uuid)
        self.assertEqual(
            restic.call_args.args[1],
            [
                "restore",
                "a" * 64,
                "--target",
                "/",
                "--include",
                f"/var/lib/pterodactyl/volumes/{server_uuid}",
            ],
        )
        self.assertIn(f"/var/lib/pterodactyl/volumes/{server_uuid}", message)

    def test_server_restore_requires_uuid_confirmation(self):
        server_uuid = "2d976043-3b60-40cb-915b-bfae65896c91"
        with patch.object(app, "_tui_prompt", side_effect=["RESTORE", f"RESTORE {server_uuid}"]) as prompt, \
             patch.object(app, "_show_output") as show_error:
            confirmed = app._confirm_server_restore(
                object(), "a" * 64, server_uuid
            )
        self.assertTrue(confirmed)
        self.assertEqual(prompt.call_count, 2)
        show_error.assert_called_once()
        self.assertIn(server_uuid, prompt.call_args.args[1])

    def test_activity_log_viewer_can_scroll_to_old_entries(self):
        class FakeScreen:
            def __init__(self):
                self.keys = iter([app.curses.KEY_HOME, ord("q")])
                self.rendered = []

            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 6, 80

            def addstr(self, _row, _column, text, *_args):
                self.rendered.append(str(text))

            @staticmethod
            def refresh():
                pass

            def getch(self):
                return next(self.keys)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.log"
            path.write_text("\n".join(f"entry {index}" for index in range(10)), encoding="utf-8")
            screen = FakeScreen()
            with patch.object(app, "LOG_PATH", path):
                app._activity_log_viewer(screen)
        self.assertIn("entry 9", screen.rendered)
        self.assertIn("entry 0", screen.rendered)

    def test_lock_error_unlocks_once_then_retries(self):
        config = app.Config(repository="/tmp/repo", password="pw")
        responses = iter([
            (1, "Fatal: repository is already locked"),
            (0, "ok"),
        ])

        def popen(*_args, **_kwargs):
            return self._fake_process(*next(responses))

        with patch.object(app.subprocess, "Popen", side_effect=popen) as run, \
             patch.object(app, "_unlock", return_value=True) as unlock:
            result = app._run_restic(config, ["snapshots"], app.logging.getLogger("test"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(run.call_count, 2)
        unlock.assert_called_once()

    @staticmethod
    def _fake_process(return_code, output):
        class FakeProcess:
            def __init__(self):
                self.stdout = io.StringIO(output)
                self.returncode = return_code

            def wait(self):
                return self.returncode

            def poll(self):
                return self.returncode

        return FakeProcess()

    def test_s3_secrets_are_environment_values_not_command_arguments(self):
        config = app.Config(
            repository="s3:https://example/bucket", password="restic-secret",
            access_key="access-secret", secret_key="secret-secret",
        )
        environment = app.restic_environment(config)
        self.assertEqual(environment["RESTIC_PASSWORD"], "restic-secret")
        self.assertEqual(environment["AWS_ACCESS_KEY_ID"], "access-secret")
        self.assertEqual(environment["AWS_SECRET_ACCESS_KEY"], "secret-secret")
        self.assertNotIn("RESTIC_PASSWORD", ["restic", "backup"])
        self.assertEqual(app._redact("access-secret secret-secret restic-secret", config), "[REDACTED] [REDACTED] [REDACTED]")

    def test_blank_password_uses_restic_insecure_flag_and_clears_inherited_password(self):
        config = app.Config(repository="/tmp/repo", password="")
        with patch.dict(os.environ, {
            "RESTIC_PASSWORD": "ambient-secret",
            "RESTIC_PASSWORD_FILE": "/tmp/ambient-password",
            "RESTIC_PASSWORD_COMMAND": "ambient-password-command",
        }):
            environment = app.restic_environment(config)
        self.assertNotIn("RESTIC_PASSWORD", environment)
        self.assertNotIn("RESTIC_PASSWORD_FILE", environment)
        self.assertNotIn("RESTIC_PASSWORD_COMMAND", environment)
        self.assertEqual(
            app._restic_command(config, ["backup", "/data"]),
            ["restic", "--insecure-no-password", "backup", "/data"],
        )

    def test_configured_password_uses_restic_encryption_without_insecure_flag(self):
        config = app.Config(repository="/tmp/repo", password="repository-secret")
        self.assertEqual(
            app._restic_command(config, ["backup", "/data"]),
            ["restic", "backup", "/data"],
        )
        self.assertEqual(app.restic_environment(config)["RESTIC_PASSWORD"], "repository-secret")

    def test_password_prompt_echoes_stars_and_supports_backspace(self):
        class TTYInput:
            @staticmethod
            def isatty():
                return True

            @staticmethod
            def fileno():
                return 12

        output = io.StringIO()
        with patch.object(app.sys, "stdin", TTYInput()), \
             patch.object(app.sys, "stdout", output), \
             patch.object(app.termios, "tcgetattr", return_value=[]), \
             patch.object(app.termios, "tcsetattr"), \
             patch.object(app.tty, "setraw"), \
             patch.object(app.os, "read", side_effect=[b"a", b"b", b"\x7f", b"c", b"\n"]):
            password = app._prompt_secret_masked("Password: ")
        self.assertEqual(password, "ac")
        self.assertEqual(output.getvalue(), "Password: **\b \b*\n")
        self.assertNotIn(password, output.getvalue())

    def test_snapshots_response_parser_rejects_non_list(self):
        config = app.Config(repository="/tmp/repo", password="pw")
        invalid_response = subprocess.CompletedProcess([], 0, "{}")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-snapshots")), \
             patch.object(app, "_run_restic", return_value=invalid_response):
            with self.assertRaises(app.AppError):
                app.restic_snapshots()

    def test_failed_backup_persists_retry_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            config_path = root / "config.json"
            state_path = root / "state" / "state.json"
            app.save_config(
                app.Config(source=str(source), repository="/tmp/repo", password="pw"),
                config_path,
            )
            lock = tempfile.TemporaryFile()
            with patch.object(app, "CONFIG_PATH", config_path), \
                 patch.object(app, "STATE_PATH", state_path), \
                 patch.object(app, "STATE_DIR", state_path.parent), \
                 patch.object(app, "create_logger", return_value=app.logging.getLogger("test-backup")), \
                 patch.object(app, "_backup_lock", return_value=lock), \
                 patch.object(app, "_stream_backup", side_effect=app.AppError("network unavailable")):
                self.assertFalse(app.run_backup())
            state = app.StateStore(state_path).read()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failure_count"], 1)
            self.assertIsNotNone(state["retry_at"])

    def test_successful_backup_scopes_retention_to_this_application_and_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            config_path = root / "config.json"
            state_path = root / "state" / "state.json"
            app.save_config(
                app.Config(source=str(source), repository="/tmp/repo", password="pw"),
                config_path,
            )
            lock = tempfile.TemporaryFile()
            with patch.object(app, "CONFIG_PATH", config_path), \
                 patch.object(app, "STATE_PATH", state_path), \
                 patch.object(app, "STATE_DIR", state_path.parent), \
                 patch.object(app, "create_logger", return_value=app.logging.getLogger("test-backup-success")), \
                 patch.object(app, "_backup_lock", return_value=lock), \
                 patch.object(app, "_stream_backup"), \
                 patch.object(
                     app, "_run_restic",
                     return_value=subprocess.CompletedProcess([], 0, ""),
                 ) as restic:
                self.assertTrue(app.run_backup())
            retention_args = restic.call_args.args[1]
            self.assertIn("--tag", retention_args)
            self.assertIn(app.BACKUP_TAG, retention_args)
            self.assertIn("--host", retention_args)
            self.assertIn(app.BACKUP_HOST, retention_args)
            state = app.StateStore(state_path).read()
            self.assertEqual(state["status"], "success")
            self.assertIsNotNone(state["last_success"])

    def test_active_repository_lock_retries_with_bounded_wait_when_unlock_fails(self):
        config = app.Config(repository="/tmp/repo", password="pw")
        with patch.object(
            app.subprocess,
            "Popen",
            side_effect=[
                self._fake_process(1, "Fatal: repository is already locked"),
                self._fake_process(0, "operation completed"),
            ],
        ) as run, \
             patch.object(app, "_unlock", return_value=False):
            result = app._run_restic(config, ["snapshots"], app.logging.getLogger("test"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "operation completed")
        self.assertEqual(run.call_count, 2)
        retry_command = run.call_args.args[0]
        self.assertIn("--retry-lock", retry_command)
        self.assertIn(app.LOCK_RETRY_INTERVAL, retry_command)

    def test_restic_command_adds_optional_lock_retry_duration(self):
        config = app.Config(repository="/tmp/repo", password="test-password")
        self.assertEqual(
            app._restic_command(config, ["forget", "--prune"], retry_lock="30s"),
            ["restic", "--retry-lock", "30s", "forget", "--prune"],
        )

    def test_stale_repository_lock_is_rechecked_until_it_clears(self):
        config = app.Config(repository="/tmp/repo")
        with patch.object(app, "LOCK_RETRY_ATTEMPTS", 3), \
             patch.object(
                 app.subprocess,
                 "Popen",
                 side_effect=[
                     self._fake_process(1, "Fatal: repository is already locked"),
                     self._fake_process(1, "Fatal: repository is already locked"),
                     self._fake_process(1, "Fatal: repository is already locked"),
                     self._fake_process(0, "operation completed"),
                 ],
             ) as run, \
             patch.object(app, "_unlock", return_value=False) as unlock:
            result = app._run_restic(config, ["forget", "--prune"], app.logging.getLogger("test"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(unlock.call_count, 3)
        self.assertEqual(
            run.call_args.args[0],
            [
                "restic", "--insecure-no-password", "--retry-lock",
                app.LOCK_RETRY_INTERVAL, "forget", "--prune",
            ],
        )

    def test_backup_waits_for_active_repository_lock_after_safe_unlock_attempt(self):
        config = app.Config(source="/tmp/source", repository="/tmp/repo")
        with patch.object(
            app.subprocess,
            "Popen",
            side_effect=[
                self._fake_process(1, "Fatal: repository is already locked"),
                self._fake_process(0, '{"message_type":"summary"}'),
            ],
        ) as run, \
             patch.object(app, "_unlock", return_value=False):
            app._stream_backup(config, app.logging.getLogger("test"))
        self.assertEqual(run.call_count, 2)
        retry_command = run.call_args.args[0]
        self.assertIn("--retry-lock", retry_command)
        self.assertIn(app.LOCK_RETRY_INTERVAL, retry_command)
        self.assertIn("backup", retry_command)

    def test_manual_backup_uses_transient_systemd_service(self):
        completed = subprocess.CompletedProcess([], 0, "Running as unit: restic-node-backup-manual-1.service\n")
        with patch.object(app.shutil, "which", return_value="/usr/bin/systemd-run"), \
             patch.object(app.subprocess, "run", return_value=completed) as run:
            message = app._start_manual_backup()
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--system", command)
        self.assertIn("--no-block", command)
        self.assertIn("--collect", command)
        self.assertIn("--property=Type=exec", command)
        self.assertIn("--property=StateDirectory=restic-node-backup", command)
        self.assertIn("--property=ProtectSystem=full", command)
        self.assertEqual(command[-1], "run-now")
        self.assertIn("continue if you close the dashboard", message)

    def test_manual_backup_reports_missing_systemd_run(self):
        with patch.object(app.shutil, "which", return_value=None):
            with self.assertRaisesRegex(app.AppError, "systemd-run is required"):
                app._start_manual_backup()

    def test_manual_backup_reports_systemd_start_failure(self):
        failed = subprocess.CompletedProcess([], 1, "Access denied")
        with patch.object(app.shutil, "which", return_value="/usr/bin/systemd-run"), \
             patch.object(app.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(app.AppError, "Access denied"):
                app._start_manual_backup()

    def test_restic_command_can_be_cancelled(self):
        config = app.Config(repository="/tmp/repo", password="pw")
        control = app.TaskControl()

        class CancellableProcess:
            stdout = io.StringIO("working\n")
            returncode = -15

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self):
                return self.returncode

        def popen(*_args, **_kwargs):
            control.cancel()
            return CancellableProcess()

        with patch.object(app.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(app.TaskCancelled):
                app._run_restic(
                    config,
                    ["check"],
                    app.logging.getLogger("test-cancel"),
                    control=control,
                )

    def test_successful_restic_command_wins_if_cancel_arrives_after_completion(self):
        config = app.Config(repository="/tmp/repo", password="pw")
        control = app.TaskControl()

        class CompletedProcess:
            stdout = io.StringIO("done\n")
            returncode = 0

            def poll(self):
                return 0

            def wait(self):
                return 0

        def popen(*_args, **_kwargs):
            control.cancel()
            return CompletedProcess()

        with patch.object(app.subprocess, "Popen", side_effect=popen):
            result = app._run_restic(
                config,
                ["check"],
                app.logging.getLogger("test-complete-cancel"),
                control=control,
            )
        self.assertEqual(result.returncode, 0)

    def test_snapshot_browser_returns_selected_snapshot(self):
        class FakeScreen:
            def __init__(self):
                self.keys = iter([app.curses.KEY_DOWN, 10])
                self.rendered = []

            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 30, 120

            def addstr(self, _row, _column, text, *_args):
                self.rendered.append(str(text))

            @staticmethod
            def refresh():
                pass

            def getch(self):
                return next(self.keys)

        snapshots = [
            {"id": "aaaa", "time": "2026-01-01T00:00:00Z"},
            {"id": "bbbb", "time": "2026-01-02T00:00:00Z"},
        ]
        with patch.object(app.curses, "color_pair", return_value=0):
            screen = FakeScreen()
            screen.keys = iter([app.curses.KEY_DOWN, ord("r")])
            selected = app._snapshot_browser(screen, snapshots, allow_restore=True)
        self.assertEqual(selected[0]["id"], "aaaa")
        self.assertEqual(selected[1], "restore")
        self.assertIn(app.WATERMARK, "\n".join(screen.rendered))
        self.assertTrue(any("2026-01-02 00:00:00 UTC" in line for line in screen.rendered))
        self.assertEqual(app._snapshot_time_text("2026-01-02T00:00:00Z"), "2026-01-02 00:00:00 UTC")
        self.assertEqual(
            app._snapshot_time_text("2026-01-02T00:00:00Z", "Asia/Kolkata"),
            "2026-01-02 05:30:00 IST",
        )

    def test_snapshot_details_can_return_selected_restore_action(self):
        class FakeScreen:
            def __init__(self):
                self.keys = iter([10, ord("r")])
                self.rendered = []

            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 24, 100

            def addstr(self, _row, _column, text, *_args):
                self.rendered.append(str(text))

            @staticmethod
            def refresh():
                pass

            def getch(self):
                return next(self.keys)

        screen = FakeScreen()
        snapshots = [{
            "id": "a" * 64,
            "time": "2026-01-02T00:00:00Z",
            "paths": ["/var/lib/pterodactyl"],
            "tags": ["game-node-backup"],
            "summary": {"total_files_processed": 10, "data_added": 20},
        }]
        with patch.object(app.curses, "color_pair", return_value=0):
            result = app._snapshot_browser(screen, snapshots, allow_restore=True)
        self.assertEqual(result, (snapshots[0], "restore"))
        self.assertTrue(any("Files: 10 processed" in line for line in screen.rendered))

    def test_snapshot_browser_cancel_returns_no_selection(self):
        class FakeScreen:
            @staticmethod
            def erase():
                pass

            @staticmethod
            def getmaxyx():
                return 30, 120

            @staticmethod
            def addstr(*_args):
                pass

            @staticmethod
            def refresh():
                pass

            @staticmethod
            def getch():
                return ord("q")

        with patch.object(app.curses, "color_pair", return_value=0):
            selected = app._snapshot_browser(
                FakeScreen(),
                [{"id": "aaaa", "time": "2026-01-01T00:00:00Z"}],
                allow_restore=False,
            )
        self.assertIsNone(selected)

    def test_snapshot_listing_keeps_large_full_repository_response(self):
        snapshots = [
            {"id": f"{index:064x}", "time": f"2026-01-{(index % 28) + 1:02d}T00:00:00Z"}
            for index in range(300)
        ]

        class SnapshotProcess:
            def __init__(self):
                self.stdout = io.StringIO(json.dumps(snapshots, indent=2))
                self.returncode = 0

            def wait(self):
                return self.returncode

            def poll(self):
                return self.returncode

        config = app.Config(repository="/tmp/repo", password="pw")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-snapshots-all")), \
             patch.object(app.subprocess, "Popen", return_value=SnapshotProcess()):
            result = app.restic_snapshots()
        self.assertEqual(len(result), 300)
        self.assertEqual(result[299]["id"], f"{299:064x}")

    def test_backup_cli_command_opens_dashboard(self):
        with patch.object(app, "run_tui") as run_tui:
            self.assertEqual(app.main(["backup"]), 0)
        run_tui.assert_called_once()

    def test_run_cli_starts_persistent_backup_service(self):
        with patch.object(app, "_start_manual_backup", return_value="started") as start, \
             patch("builtins.print") as print_output:
            self.assertEqual(app.main(["run"]), 0)
        start.assert_called_once()
        print_output.assert_called_once_with("started")

    def test_run_now_cli_executes_backup(self):
        with patch.object(app, "run_backup", return_value=True) as run_backup:
            self.assertEqual(app.main(["run-now"]), 0)
        run_backup.assert_called_once_with()

    def test_ensure_repository_does_not_initialize_on_authentication_failure(self):
        config = app.Config(repository="/tmp/repo", password="pw")
        wrong_password = app.AppError("Restic snapshots failed: wrong password or no key found")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-ensure")), \
             patch.object(app, "_run_restic", side_effect=wrong_password), \
             patch.object(app, "initialize_repository") as init:
            with self.assertRaises(app.AppError):
                app.ensure_repository()
        init.assert_not_called()

    def test_ensure_repository_accepts_existing_repository(self):
        config = app.Config(repository="/tmp/repo", password="pw")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-ensure")), \
             patch.object(
                 app, "_run_restic",
                 return_value=subprocess.CompletedProcess([], 0, "[]"),
             ) as restic, \
             patch.object(app, "initialize_repository") as init:
            self.assertIn("already initialized", app.ensure_repository())
        self.assertEqual(restic.call_args.args[1], ["cat", "config"])
        init.assert_not_called()

    def test_ensure_repository_initializes_missing_repo_then_verifies_connection(self):
        config = app.Config(repository="s3:https://s3.example/bucket", password="pw")
        missing = app.AppError("Restic cat config failed: config file does not exist")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-ensure")), \
             patch.object(app, "_run_restic", side_effect=[
             missing,
             subprocess.CompletedProcess([], 0, '{"version":2}'),
             ]) as restic, \
             patch.object(app, "initialize_repository", return_value="repository initialized") as init:
            result = app.ensure_repository()
        init.assert_called_once()
        self.assertEqual([call.args[1] for call in restic.call_args_list], [
            ["cat", "config"],
            ["cat", "config"],
        ])
        self.assertIn("S3 connection successful", result)

    def test_ensure_repository_does_not_initialize_after_connection_failure(self):
        config = app.Config(repository="s3:https://s3.example/bucket", password="pw")
        failure = app.AppError("Restic cat config failed: AccessDenied")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-ensure")), \
             patch.object(app, "_run_restic", side_effect=failure), \
             patch.object(app, "initialize_repository") as init:
            with self.assertRaisesRegex(app.AppError, "was not initialized"):
                app.ensure_repository()
        init.assert_not_called()

    def test_ensure_repository_recognizes_s3_missing_object_as_new_repo(self):
        config = app.Config(repository="s3:https://s3.example/bucket", password="pw")
        missing = app.AppError("Restic cat config failed: The specified key does not exist.")
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "create_logger", return_value=app.logging.getLogger("test-ensure")), \
             patch.object(app, "_run_restic", side_effect=[
                 missing,
                 subprocess.CompletedProcess([], 0, '{"version":2}'),
             ]), \
             patch.object(app, "initialize_repository", return_value="initialized") as init:
            self.assertIn("S3 connection successful", app.ensure_repository())
        init.assert_called_once()

    def test_first_setup_leaves_password_blank_to_disable_encryption(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            prompts = [
                "s3.fra.databucket.eu", "game-backups", "/var/lib/pterodactyl",
                "", "24", "7",
            ]
            stderr = io.StringIO()
            with patch.object(app, "CONFIG_PATH", config_path), \
                 patch.object(app, "_prompt", side_effect=prompts), \
                 patch.object(app, "_prompt_secret_masked", return_value=""), \
                 patch.object(app, "_prompt_optional_secret", return_value=""), \
                 patch.object(app, "_select_timezone_cli", return_value="Asia/Kolkata"), \
                 patch("sys.stderr", stderr):
                app.configure_cli()
            saved = app.load_config(config_path)
            self.assertEqual(
                saved.repository,
                "s3:https://s3.fra.databucket.eu/game-backups",
            )
            self.assertEqual(saved.password, "")
            self.assertEqual(saved.timezone_name, "Asia/Kolkata")
            self.assertIn("encryption is disabled", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
