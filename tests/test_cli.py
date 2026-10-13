import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_module_help_explains_data_directory(self):
        result = subprocess.run([os.sys.executable, "-m", "codescout", "--help"], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"--data-dir", result.stdout)
        self.assertIn(b"--port", result.stdout)

    def test_real_server_serves_ui_and_protected_state(self):
        import json
        from urllib.request import Request, urlopen
        with tempfile.TemporaryDirectory() as data_dir:
            process = subprocess.Popen([os.sys.executable, "-u", "-m", "codescout", "--port", "0", "--data-dir", data_dir], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            try:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    url = executor.submit(process.stdout.readline).result(timeout=8).strip()
                self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                base_url, fragment = url.split("#token=")
                with urlopen(base_url, timeout=3) as response:
                    self.assertIn("巡码".encode(), response.read())
                with urlopen(Request(base_url.rstrip("/") + "/api/state", headers={"Authorization": "Bearer " + fragment}), timeout=3) as response:
                    self.assertEqual(json.load(response)["projects"], [])
                self.assertTrue((Path(data_dir) / "state.db").exists())
            finally:
                process.terminate()
                process.communicate(timeout=5)
