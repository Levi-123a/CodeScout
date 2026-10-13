import tempfile
import unittest
from pathlib import Path

from codescout.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "state.db"
        self.store = Store(self.db)

    def test_projects_survive_restart_and_delete_keeps_source(self):
        source = Path(self.temp.name) / "source"
        source.mkdir()
        project = self.store.add_project(str(source), "项目", [], "保持注释准确", True)
        again = Store(self.db)
        self.assertEqual(again.project(project["id"])["custom_rules"], "保持注释准确")
        again.delete_project(project["id"])
        self.assertTrue(source.is_dir())
        self.assertEqual(again.projects(), [])

    def test_model_public_config_never_returns_key_and_blank_keeps_it(self):
        self.store.configure_model({"base_url": "https://example.com/v1", "model": "test", "api_key": "secret", "timeout": 10, "context_chars": 60000})
        public = self.store.public_model()
        self.assertNotIn("api_key", public)
        self.assertTrue(public["has_key"])
        version = public["version"]
        self.store.configure_model({"base_url": "https://example.com/v1", "model": "other", "api_key": "", "timeout": 10, "context_chars": 60000})
        self.assertEqual(self.store.model()["api_key"], "secret")
        self.assertGreater(self.store.public_model()["version"], version)

    def test_failed_run_does_not_advance_successful_baseline(self):
        project = self.store.add_project(self.temp.name, "x", [], "", True)
        run = self.store.create_run(project["id"], "one", "key-one", {"a.py": "one"})
        self.store.finish_run(run["id"], {"status": "failed", "error": "offline", "findings": [], "coverage": {}})
        self.assertIsNone(self.store.baseline(project["id"]))
        run = self.store.create_run(project["id"], "two", "key-two", {"a.py": "two"})
        self.store.finish_run(run["id"], {"status": "completed", "findings": [], "coverage": {"checked": ["a.py"], "uncovered": []}})
        self.assertEqual(self.store.baseline(project["id"])["snapshot"], {"a.py": "two"})

    def test_restart_marks_interrupted_runs_failed(self):
        project = self.store.add_project(self.temp.name, "x", [], "", True)
        self.store.create_run(project["id"], "one", "key-one", {})
        self.store.recover()
        self.assertEqual(self.store.runs(project["id"])[0]["status"], "failed")

    def test_stale_and_partial_checks_do_not_resolve_existing_findings(self):
        from tests.helpers import finding
        for state in ("partial", "stale"):
            with self.subTest(state=state):
                project = self.store.add_project(self.temp.name + state, state, [], "", True)
                run = self.store.create_run(project["id"], "one", "one", {"main.py": "return 2"})
                self.store.finish_run(run["id"], {"status": "completed", "findings": [finding()], "coverage": {"checked": ["main.py", "README.md"]}})
                newer = self.store.create_run(project["id"], "two", "two", {})
                if state == "stale":
                    self.store.mark_stale(project["id"])
                self.store.finish_run(newer["id"], {"status": "partial" if state == "partial" else "completed", "findings": [], "coverage": {"checked": ["main.py"], "uncovered": ["README.md"]}})
                self.assertEqual(self.store.findings(project["id"])[0]["status"], "open")

    def test_same_file_independent_findings_are_preserved_after_line_moves(self):
        from tests.helpers import finding
        project = self.store.add_project(self.temp.name, "x", [], "", True)
        for lines in ((2, 4), (3, 5)):
            run = self.store.create_run(project["id"], str(lines), str(lines), {})
            self.store.finish_run(run["id"], {"status": "completed", "findings": [finding(line=lines[0]), finding(line=lines[1])], "coverage": {"checked": ["main.py", "README.md"]}})
            issues = self.store.findings(project["id"])
            self.assertEqual(len(issues), 2)
            self.assertEqual({item["line"] for item in issues}, set(lines))

