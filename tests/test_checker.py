import json
import unittest

from codescout.checker import check_snapshot
from codescout.snapshot import Snapshot
from tests.helpers import finding, model_server


class CheckerTests(unittest.TestCase):
    def snapshot(self, files, warnings=None):
        return Snapshot(files, {}, warnings or [], "digest", None)

    def test_real_protocol_includes_unchanged_docs_and_validates_evidence(self):
        snapshot = self.snapshot({"main.py": "return 2\n", "README.md": "returns 1\n", "AGENTS.md": "comments must match code"})
        with model_server([finding()]) as (config, requests):
            result = check_snapshot(snapshot, {"main.py": "return 1\n", "README.md": "returns 1\n"}, config, "保持注释准确")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["findings"][0]["path"], "main.py")
        payload = requests[0]
        self.assertEqual(payload["model"], "test-model")
        self.assertNotIn("top-secret", json.dumps(payload))
        context = json.loads(payload["messages"][1]["content"])
        self.assertIn("README.md", context["files"])
        self.assertIn("-return 1", context["diff"])
        self.assertIn("+return 2", context["diff"])
        self.assertIn("保持注释准确", context["custom_rules"])

    def test_nonexistent_path_line_and_fabricated_evidence_fail(self):
        for issue in [finding("outside.py"), finding(line=9), finding(evidence="fabricated")]:
            with self.subTest(issue=issue), model_server([issue]) as (config, _):
                result = check_snapshot(self.snapshot({"main.py": "return 2", "README.md": "returns 1"}), None, config, "")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["error"])

    def test_http_failure_never_echoes_response_or_key(self):
        with model_server(status=429, raw=b"top-secret private server body") as (config, _):
            result = check_snapshot(self.snapshot({"main.py": "return 2"}), None, config, "")
        self.assertEqual(result["status"], "failed")
        self.assertIn("429", result["error"])
        self.assertNotIn("top-secret", json.dumps(result))
        self.assertNotIn("private server body", json.dumps(result))

    def test_malformed_model_output_is_failed(self):
        with model_server(raw=b'{"choices": []}') as (config, _):
            result = check_snapshot(self.snapshot({"main.py": "return 2"}), None, config, "")
        self.assertEqual(result["status"], "failed")

    def test_budget_marks_uncovered_files_and_partial_status(self):
        with model_server() as (config, requests):
            config["context_chars"] = 2000
            result = check_snapshot(self.snapshot({"small.py": "x = 1", "big.py": "x" * 5000}), None, config, "")
        self.assertEqual(result["status"], "partial")
        self.assertIn("big.py", result["coverage"]["uncovered"])
        self.assertIn("small.py", result["coverage"]["checked"])
        self.assertLessEqual(len(json.dumps(requests[0], ensure_ascii=False)), 2000)

    def test_snapshot_limits_cannot_report_full_completion(self):
        with model_server() as (config, _):
            result = check_snapshot(self.snapshot({"x.py": "x=1"}, ["big.py：文件超限"]), None, config, "")
        self.assertEqual(result["status"], "partial")

    def test_new_exclusion_does_not_send_old_content_as_deletion(self):
        snapshot = Snapshot({"main.py": "return 2"}, {"private/": "用户排除", "secret.py": "用户排除"}, [], "new", None)
        with model_server() as (config, requests):
            result = check_snapshot(snapshot, {"main.py": "return 2", "secret.py": "private-token", "private/a.py": "private-content", "deleted.py": "removed"}, config, "")
        coverage = result["coverage"]
        self.assertNotIn("private-token", json.dumps(requests))
        self.assertNotIn("private-content", json.dumps(requests))
        self.assertEqual(coverage["deleted"], ["deleted.py"])
        self.assertIn("secret.py", coverage["removed_from_scope"])

