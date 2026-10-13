import json
import sys
import time
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codescout.repair import RepairError, apply_repair, generate_repair
from codescout.snapshot import capture
from codescout.verification import verify_project
from tests.helpers import finding


@contextmanager
def repair_model(edits):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "read-1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "main.py", "start_line": 1, "end_line": 20})}}]}
            else:
                message = {"role": "assistant", "content": json.dumps({"edits": edits})}
            payload = json.dumps({"choices": [{"message": message}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "repair", "api_key": "test-key", "timeout": 3, "context_chars": 12000}, requests
    finally:
        server.shutdown()
        server.server_close()


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.source = self.root / "main.py"
        self.source.write_bytes(b"def value():\r\n    return 2\r\n")
        (self.root / "README.md").write_text("returns 1\n", encoding="utf-8")
        self.backups = Path(self.temp.name) / "backups"

    def proposal(self, edits=None):
        with repair_model(edits or [{"path": "main.py", "old": "return 2", "new": "return 1"}]) as (config, requests):
            repair = generate_repair(self.root, capture(self.root), finding(line=2), config, "注释与实现一致")
        return repair, requests

    def test_reads_source_generates_diff_without_writing_then_applies(self):
        repair, requests = self.proposal()
        self.assertEqual(self.source.read_bytes(), b"def value():\r\n    return 2\r\n")
        self.assertIn("-    return 2", repair["diff"])
        self.assertIn("+    return 1", repair["diff"])
        self.assertIn("return 2", requests[1]["messages"][-1]["content"])
        self.assertTrue(requests[0]["tools"])
        result = apply_repair(self.root, repair, self.backups)
        self.assertEqual(self.source.read_bytes(), b"def value():\r\n    return 1\r\n")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["verification"]["status"], "not_configured")
        self.assertTrue(any(self.backups.rglob("*.bak")))

    def test_concurrent_edit_is_not_overwritten(self):
        repair, _ = self.proposal()
        self.source.write_text("def value():\n    return 99\n", encoding="utf-8")
        with self.assertRaises(RepairError):
            apply_repair(self.root, repair, self.backups)
        self.assertIn("return 99", self.source.read_text(encoding="utf-8"))

    def test_outside_scope_and_ambiguous_replacements_are_rejected(self):
        for edits in [[{"path": "../outside.py", "old": "x", "new": "y"}], [{"path": "main.py", "old": " ", "new": "x"}]]:
            with self.subTest(edits=edits), self.assertRaises(RepairError):
                self.proposal(edits)
        self.assertIn(b"return 2", self.source.read_bytes())

    def test_invalid_python_proposal_never_becomes_ready(self):
        with self.assertRaises(RepairError):
            self.proposal([{"path": "main.py", "old": "return 2", "new": "return ("}])
        self.assertIn(b"return 2", self.source.read_bytes())

    def test_configured_validation_runs_actual_project_command(self):
        repair, _ = self.proposal()
        command = f'"{sys.executable}" -c "from main import value; assert value() == 1; print(12345)"'
        result = apply_repair(self.root, repair, self.backups, command)
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertIn("12345", result["verification"]["output"])

    def test_validation_failure_is_not_reported_as_pass(self):
        repair, _ = self.proposal()
        command = f'"{sys.executable}" -c "raise SystemExit(2)"'
        result = apply_repair(self.root, repair, self.backups, command)
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["verification"]["exit_code"], 2)

    def test_verification_timeout_and_output_are_bounded(self):
        command = f'"{sys.executable}" -c "import time; print(123,flush=True); time.sleep(10)"'
        result = verify_project(self.root, ["main.py"], command, timeout=0.1)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["timed_out"])
        result = verify_project(self.root, ["main.py"], f'"{sys.executable}" -c "print(\'z\' * 20000)"')
        self.assertLessEqual(len(result["output"]), 8192)
        self.assertTrue(result["output_truncated"])

    def test_child_holding_stdout_cannot_outlive_validation_deadline(self):
        # The parent exits successfully while its child still owns the output pipe.
        script = self.root / "spawn.py"
        script.write_text("import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'])\n")
        started = time.monotonic()
        result = verify_project(self.root, ["main.py"], f'"{sys.executable}" spawn.py', timeout=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["timed_out"])
