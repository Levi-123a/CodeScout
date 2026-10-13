import json
import unittest

from codescout.checker import check_snapshot
from codescout.snapshot import Snapshot
from tests.helpers import model_server


class BatchTests(unittest.TestCase):
    def snapshot(self, files):
        return Snapshot(files, {}, [], "stable", None)

    def test_initial_check_batches_files_with_hard_request_budget(self):
        files = {f"module{i}.py": f"def value{i}():\n    return {i}\n" * 20 for i in range(8)}
        progress = []
        with model_server() as (config, requests):
            config["context_chars"] = 3000
            result = check_snapshot(self.snapshot(files), None, config, "", on_progress=progress.append)
        self.assertEqual(result["status"], "completed")
        self.assertGreater(len(requests), 1)
        self.assertEqual(set(result["coverage"]["checked"]), set(files))
        self.assertEqual(progress[-1]["progress"]["completed"], progress[-1]["progress"]["total"])
        for request in requests:
            self.assertLessEqual(len(json.dumps(request, ensure_ascii=False)), 3000)
            context = json.loads(request["messages"][1]["content"])
            self.assertLess(len(context["files"]), len(files))

    def test_long_file_is_checked_in_line_ranges_without_truncation(self):
        text = "".join(f"value_{i} = {i}\n" for i in range(400))
        with model_server() as (config, requests):
            config["context_chars"] = 3000
            result = check_snapshot(self.snapshot({"large.py": text}), None, config, "")
        self.assertEqual(result["status"], "completed")
        ranges = [json.loads(request["messages"][1]["content"])["file_ranges"]["large.py"] for request in requests]
        self.assertEqual(ranges[0][0], 1)
        self.assertEqual(ranges[-1][1], 400)
        self.assertEqual(sum(end - start + 1 for start, end in ranges), 400)

    def test_failed_batch_preserves_progress_and_resume_skips_completed(self):
        files = {f"part{i}.txt": "text\n" * 80 for i in range(4)}
        counter = [0]
        def response(context):
            counter[0] += 1
            return [] if counter[0] == 1 else {"bad": "response"}
        saved = []
        with model_server(response) as (config, _):
            config["context_chars"] = 3000
            failed = check_snapshot(self.snapshot(files), None, config, "", on_progress=saved.append)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["progress"]["completed"], 1)
        self.assertTrue(failed["batches"])
        with model_server() as (config, requests):
            config["context_chars"] = 3000
            resumed = check_snapshot(self.snapshot(files), None, config, "", resume=failed)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(requests), resumed["progress"]["total"] - 1)

    def test_python_syntax_issue_is_located_without_model_guess(self):
        with model_server() as (config, _):
            result = check_snapshot(self.snapshot({"broken.py": "def broken(:\n    pass\n"}), None, config, "")
        issue = next(item for item in result["findings"] if item.get("checker") == "python-ast")
        self.assertEqual(issue["path"], "broken.py")
        self.assertEqual(issue["line"], 1)
        self.assertEqual(issue["evidence"], "def broken(:")

