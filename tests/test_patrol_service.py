import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codescout.conventions import RULES_FILENAME
from codescout.service import Service
from codescout.store import Store
from tests.helpers import finding, model_server
from tests.test_repair import repair_model


class PatrolServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "folder-name"
        self.root.mkdir()
        (self.root / "main.py").write_text("def value():\n    return 2\n", encoding="utf-8")
        (self.root / "README.md").write_text("returns 1\n", encoding="utf-8")
        self.store = Store(Path(self.temp.name) / "state.db")
        self.service = Service(self.store, debounce_seconds=0)

    def add(self, **options):
        return self.service.add_project({"path": str(self.root), "allow_send": True, **options})

    def test_folder_name_and_independent_markdown_rules(self):
        project = self.add(name="discard-alias", custom_rules="保持文档一致")
        self.assertEqual(project["name"], "folder-name")
        self.assertEqual((self.root / RULES_FILENAME).read_text(encoding="utf-8"), "保持文档一致")
        self.assertEqual(self.service.state()["projects"][0]["rules_file"], RULES_FILENAME)

    def test_external_rule_edit_is_loaded_and_stale_browser_save_is_rejected(self):
        project = self.add(custom_rules="first")
        (self.root / RULES_FILENAME).write_text("external", encoding="utf-8")
        self.assertEqual(self.service.state()["projects"][0]["custom_rules"], "external")
        with self.assertRaises(ValueError):
            self.service.update_options(project["id"], {"custom_rules": "browser", "rules_expected": "first"})
        self.assertEqual((self.root / RULES_FILENAME).read_text(encoding="utf-8"), "external")

    def test_restart_preserves_completed_batch_checkpoint(self):
        project = self.add()
        run = self.store.create_run(project["id"], "snap", "key", {})
        self.store.save_progress(run["id"], {"status": "running", "progress": {"completed": 1, "total": 3}, "batches": [{"id": "batch-one", "findings": []}]})
        Store(self.store.path).recover()
        checkpoint = self.store.checkpoint(project["id"], "key")
        self.assertEqual(checkpoint["progress"]["completed"], 1)
        self.assertEqual(checkpoint["batches"][0]["id"], "batch-one")

    def test_repair_queue_previews_then_applies_and_records_verification(self):
        with repair_model([{"path": "main.py", "old": "return 2", "new": "return 1"}]) as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            job = self.service.pending.pop(project["id"])
            run = self.store.create_run(project["id"], job["snapshot"].digest, job["key"], job["snapshot"].files)
            self.store.finish_run(run["id"], {"status": "completed", "findings": [finding(line=2)], "coverage": {"checked": ["main.py", "README.md"]}})
            issue = self.store.findings(project["id"])[0]
            repair = self.service.queue_repair(project["id"], issue["id"])
            self.assertEqual(repair["status"], "queued")
            self.service.process_repair_one()
            public = self.service.state()["projects"][0]["repairs"][0]
            self.assertEqual(public["status"], "ready")
            self.assertNotIn("before_bytes", public["changes"][0])
            self.assertIn("return 2", (self.root / "main.py").read_text())
            self.service.queue_apply(project["id"], repair["id"])
            self.service.process_repair_one()
        public = self.service.state()["projects"][0]["repairs"][0]
        self.assertEqual(public["status"], "applied")
        self.assertEqual(public["verification"]["status"], "not_configured")
        self.assertIn("return 1", (self.root / "main.py").read_text())

    def test_actual_service_check_persists_batch_progress(self):
        for index in range(4):
            (self.root / f"part{index}.txt").write_text("small document\n", encoding="utf-8")
        observed_completed = []
        def response(context):
            runs = self.store.runs(project["id"])
            observed_completed.append(runs[0]["result"].get("progress", {}).get("completed", 0))
            return []
        with model_server(response) as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
        run = self.store.runs(project["id"])[0]
        self.assertGreater(run["result"]["progress"]["total"], 1)
        self.assertEqual(run["result"]["progress"]["completed"], run["result"]["progress"]["total"])
        self.assertGreater(max(observed_completed), 0)

    def test_bad_rules_in_queued_project_do_not_block_other_projects(self):
        with model_server() as (config, _):
            self.service.configure_model(config)
            broken = self.add()
            other = Path(self.temp.name) / "other"
            other.mkdir()
            (other / "ok.py").write_text("x = 1\n")
            good = self.service.add_project({"path": str(other), "allow_send": True})
            self.service.observe()
            (self.root / RULES_FILENAME).write_bytes(b"\xff")
            self.assertTrue(self.service.process_one())
        self.assertEqual(self.store.project(broken["id"])["status"], "unavailable")
        self.assertEqual(self.store.runs(good["id"])[0]["status"], "completed")

    def test_rules_becoming_unreadable_during_check_finish_the_old_run(self):
        with model_server() as (config, _):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            def interrupted(*args, **kwargs):
                (self.root / RULES_FILENAME).write_bytes(b"\xff")
                return {"status": "completed", "findings": [], "coverage": {"checked": ["main.py"]}}
            with patch("codescout.service.check_snapshot", side_effect=interrupted):
                self.service.process_one()
        self.assertEqual(self.store.runs(project["id"])[0]["status"], "superseded")
        self.assertEqual(self.store.project(project["id"])["status"], "unavailable")

    def test_incremental_check_carries_unchanged_evidence_with_provenance(self):
        for path in self.root.iterdir():
            path.unlink()
        for name in "abcz":
            (self.root / f"{name}.py").write_text(f"{name} = 1\n")
        issue = {**finding("z.py", 1, "z = 1"), "category": "quality", "related": []}
        def response(context):
            return [issue] if context["focus"] == "z.py" else []
        with model_server(response) as (config, requests):
            self.service.configure_model(config)
            project = self.add()
            self.service.observe()
            self.service.process_one()
            first = self.store.runs(project["id"])[0]
            (self.root / "a.py").write_text("a = 2\n")
            self.service.observe()
            self.service.process_one()
            latest = self.store.runs(project["id"])[0]
            current_issue = self.store.findings(project["id"])[0]
            self.assertFalse(current_issue["stale"])
            self.assertEqual(current_issue["carried_from_run"], first["id"])
            self.assertEqual(current_issue["run_id"], latest["id"])
            self.assertEqual(latest["result"]["coverage"]["reused"], ["z.py"])
            self.assertNotIn("z.py", latest["result"]["coverage"]["checked"])
            self.service.queue_repair(project["id"], current_issue["id"])
            self.service.repairs.jobs.clear()
            # Changed rules/configuration require fresh inspection even when files also changed.
            self.service.configure_model(config)
            (self.root / "a.py").write_text("a = 3\n")
            self.service.observe()
            self.service.process_one()
            self.assertIn("z.py", self.store.runs(project["id"])[0]["result"]["coverage"]["checked"])
