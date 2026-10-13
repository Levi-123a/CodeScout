import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_busy_port_does_not_print_an_unusable_new_session_link(self):
        from queue import Queue, Empty
        from threading import Thread
        from urllib.parse import urlsplit
        with tempfile.TemporaryDirectory() as directory:
            first = subprocess.Popen([os.sys.executable, "-u", "-m", "codescout", "--port", "0", "--data-dir", str(Path(directory) / "first")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            second = None
            reader = None
            try:
                lines = Queue()
                reader = Thread(target=lambda: lines.put(first.stdout.readline()), daemon=True)
                reader.start()
                try:
                    url = lines.get(timeout=8).strip()
                except Empty:
                    self.fail("服务未在启动截止时间内输出链接")
                self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                port = urlsplit(url).port
                second = subprocess.Popen([os.sys.executable, "-u", "-m", "codescout", "--port", str(port), "--data-dir", str(Path(directory) / "second")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    output, errors = second.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    self.fail("占用端口时仍然启动了另一服务，而没有报告失败")
                self.assertEqual(second.returncode, 1)
                self.assertNotIn(b"#token=", output)
                self.assertTrue(errors)
            finally:
                for process in (second, first):
                    if process:
                        if process.poll() is None:
                            process.terminate()
                        process.communicate(timeout=5)
                if reader:
                    reader.join(timeout=1)

    def test_open_flag_opens_browser_after_service_start(self):
        from unittest.mock import patch, Mock
        from codescout.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            server = Mock(server_port=12345)
            server.serve_forever.side_effect = KeyboardInterrupt
            with patch("sys.argv", ["codescout", "--open", "--data-dir", directory]), patch("codescout.__main__.create_server", return_value=server), patch("codescout.__main__.Service") as service, patch("codescout.__main__.webbrowser.open_new_tab") as opened:
                opened.side_effect = lambda url: self.assertTrue(service.return_value.start.called)
                self.assertEqual(main(), 0)
                self.assertTrue(opened.call_args.args[0].startswith("http://127.0.0.1:12345/#token="))

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

    def test_second_instance_does_not_recover_running_service_database(self):
        with tempfile.TemporaryDirectory() as directory:
            first = subprocess.Popen([os.sys.executable, "-u", "-m", "codescout", "--port", "0", "--data-dir", directory], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            second = None
            try:
                self.assertTrue(first.stdout.readline().startswith("http://127.0.0.1:"))
                second = subprocess.Popen([os.sys.executable, "-m", "codescout", "--port", "0", "--data-dir", directory], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output, errors = second.communicate(timeout=3)
                self.assertEqual(second.returncode, 1)
                self.assertTrue(errors)
                self.assertFalse(output)
            finally:
                first.terminate()
                first.communicate(timeout=5)
                if second and second.poll() is None:
                    second.terminate()
                    second.communicate(timeout=5)
