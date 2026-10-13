import tempfile
import unittest
from pathlib import Path

from codescout.service import Service
from codescout.store import Store
from tests.helpers import finding, model_server


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.source = self.root / "main.py"
        self.source.write_text("return 2\n", encoding="utf-8")
        (self.root / "README.md").write_text("returns 1\n", encoding="utf-8")
        self.store = Store(Path(self.temp.name) / "state.db")
        self.service = Service(self.store, debounce_seconds=0)

    def add(self, **changes):
        return self.service.add_project({"path": str(self.root), "allow_send": True, **changes})

    def test_initial_check_then_save_triggers_and_same_snapshot_deduplicates(self):
        with model_server([finding()]) as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.assertTrue(self.service.process_one())
            self.assertEqual(len(self.store.runs(project["id"])), 1)
            self.service.observe()
            self.assertFalse(self.service.process_one())
        self.assertEqual(self.store.findings(project["id"])[0]["status"], "open")
        with model_server() as (config, _):
            self.service.configure_model(config)
            self.source.write_text("return 1\n", encoding="utf-8")
            self.service.observe()
            self.assertTrue(self.service.process_one())
        self.assertEqual(self.store.findings(project["id"])[0]["status"], "resolved")

    def test_without_send_permission_or_model_waits_without_request(self):
        project = self.add(allow_send=False)
        self.service.observe()
        self.assertFalse(self.service.process_one())
        self.assertEqual(self.store.project(project["id"])["status"], "waiting_consent")
        self.store.update_project(project["id"], allow_send=True)
        self.service.observe()
        self.assertFalse(self.service.process_one())
        self.assertEqual(self.store.project(project["id"])["status"], "waiting_model")

    def test_failed_check_does_not_advance_baseline_and_can_retry(self):
        with model_server(status=500) as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
        self.assertIsNone(self.store.baseline(project["id"]))
        self.assertEqual(self.store.runs(project["id"])[0]["status"], "failed")
        with model_server() as (config, _):
            self.service.configure_model(config)
            self.service.observe()
            self.assertTrue(self.service.process_one())
        self.assertIsNotNone(self.store.baseline(project["id"]))

    def test_rule_change_rechecks_unchanged_code(self):
        with model_server() as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
            (self.root / "AGENTS.md").write_text("comments must match", encoding="utf-8")
            self.service.observe()
            self.assertTrue(self.service.process_one())
        self.assertEqual(len(self.store.runs(project["id"])), 2)
        self.assertTrue(self.store.runs(project["id"])[1]["stale"])

    def test_pause_and_remove_do_not_delete_source(self):
        project = self.add()
        self.service.action(project["id"], "pause")
        self.service.observe()
        self.assertFalse(self.service.process_one())
        self.service.remove_project(project["id"])
        self.assertTrue(self.source.exists())

    def test_restart_reconciles_offline_edits(self):
        with model_server() as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
            self.source.write_text("return 3\n", encoding="utf-8")
            resumed = Service(Store(self.store.path), debounce_seconds=0)
            resumed.observe()
            self.assertTrue(resumed.process_one())
        self.assertEqual(len(self.store.runs(project["id"])), 2)

    def test_non_directory_invalid_config_and_duplicate_are_rejected(self):
        with self.assertRaises(ValueError):
            self.add(path=str(self.source))
        self.add()
        with self.assertRaises(ValueError):
            self.add()
        with self.assertRaises(ValueError):
            self.service.configure_model({"base_url": "file:///private", "model": "x"})

    def test_retries_stop_after_three_and_manual_retry_resumes(self):
        with model_server(status=503) as (config, requests):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
            for _ in range(3):
                self.service.pending[project["id"]]["due"] = 0
                self.assertTrue(self.service.process_one())
            self.service.observe()
            self.assertFalse(self.service.process_one())
            self.assertEqual(len(requests), 4)
            self.service.action(project["id"], "retry")
            self.service.observe()
            self.assertTrue(self.service.process_one())
            self.assertEqual(len(requests), 5)

    def test_project_options_can_be_changed_without_losing_reports(self):
        project = self.add(allow_send=False)
        self.service.update_options(project["id"], {"allow_send": True, "custom_rules": "同步文档", "excludes": ["generated/**"]})
        stored = self.store.project(project["id"])
        self.assertTrue(stored["allow_send"])
        self.assertEqual(stored["custom_rules"], "同步文档")
        self.assertEqual(stored["excludes"], ["generated/**"])

    def test_reverting_to_checked_content_replaces_queued_snapshot(self):
        with model_server() as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
            self.source.write_text("return 3\n", encoding="utf-8")
            self.service.observe()
            self.source.write_text("return 2\n", encoding="utf-8")
            self.service.observe()
            self.service.process_one()
        self.assertEqual(self.store.baseline(project["id"])["snapshot"]["main.py"], "return 2\n")

    def test_configuration_changed_during_capture_discards_old_snapshot(self):
        from unittest.mock import patch
        from codescout.snapshot import capture
        project = self.add()
        def capture_then_update(*args):
            result = capture(*args)
            self.service.update_options(project["id"], {"excludes": ["main.py"]})
            return result
        with patch("codescout.service.capture", side_effect=capture_then_update):
            self.service.observe()
        self.assertNotIn(project["id"], self.service.pending)
        self.service.observe()
        self.assertNotIn("main.py", self.service.pending[project["id"]]["snapshot"].files)

    def test_read_failure_during_model_request_makes_result_stale(self):
        from unittest.mock import patch
        self.service.configure_model({"base_url": "https://example.com/v1", "model": "x"})
        project = self.add()
        self.service.observe()
        def interrupted(*args):
            with patch("codescout.service.capture", side_effect=ValueError("continuing edits")):
                self.service.observe()
            return {"status": "completed", "findings": [], "coverage": {"checked": ["main.py", "README.md"]}}
        with patch("codescout.service.check_snapshot", side_effect=interrupted):
            self.service.process_one()
        self.assertEqual(self.store.project(project["id"])["status"], "unavailable")
        self.assertTrue(self.store.runs(project["id"])[0]["stale"])
        self.assertIsNone(self.store.baseline(project["id"]))

    def test_recovered_directory_rechecks_instead_of_staying_unavailable(self):
        from unittest.mock import patch
        with model_server() as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
            with patch("codescout.service.capture", side_effect=ValueError("unavailable")):
                self.service.observe()
            self.service.observe()
            self.assertTrue(self.service.process_one())
        self.assertEqual(self.store.project(project["id"])["status"], "completed")

