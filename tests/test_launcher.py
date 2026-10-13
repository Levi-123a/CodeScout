import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows BAT execution")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace with spaces"
        self.project = self.workspace / "mobileyun_project"
        self.project.mkdir(parents=True)
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.temp.name) / "calls.log"
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin)
        self.env["CODESCOUT_TEST_LOG"] = str(self.log)
        self.source_project = Path(__file__).resolve().parents[1]

    def copy_launcher(self, wrapper=False):
        source = self.source_project.parent / "start.bat" if wrapper else self.source_project / "start.bat"
        self.assertTrue(source.is_file(), f"double-click launcher missing: {source}")
        target = self.workspace / "start.bat" if wrapper else self.project / "start.bat"
        shutil.copyfile(source, target)
        if wrapper:
            self.copy_launcher()
        return target

    def stub(self, name="python", version=0, dependencies=0, run=0):
        body = f'''@echo off
echo %CD% ^| %*>>"%CODESCOUT_TEST_LOG%"
if "%1" == "-3" shift
if "%1" == "-m" exit /b {run}
echo %* | findstr /c:"version_info" >nul
if not errorlevel 1 exit /b {version}
exit /b {dependencies}
'''
        # findstr is deliberately copied so the PATH fixture needs no real interpreter.
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "findstr.exe", self.bin / "findstr.exe")
        (self.bin / f"{name}.bat").write_text(body, encoding="ascii")

    def run_launcher(self, launcher):
        # cmd uses its own quoting rules; list2cmdline would insert literal backslashes.
        return subprocess.run(f'"{os.environ["COMSPEC"]}" /d /c call "{launcher}"',
                              cwd=self.temp.name, env=self.env, input="\n", capture_output=True,
                              encoding="utf-8", errors="replace", timeout=15,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def test_missing_python_fails_clearly_and_reaches_pause(self):
        result = self.run_launcher(self.copy_launcher())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11", result.stdout)
        self.assertIn("[ERROR]", result.stdout)
        self.assertIn("[WAIT]", result.stdout)

    def test_failure_keeps_process_alive_until_input_arrives(self):
        launcher = self.copy_launcher()
        process = subprocess.Popen(f'"{os.environ["COMSPEC"]}" /d /c call "{launcher}"',
                                   cwd=self.temp.name, env=self.env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", errors="replace",
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            stdout, _ = process.communicate(input="\n", timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("[WAIT]", stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_too_old_python_does_not_start_service(self):
        self.stub(version=1)
        result = self.run_launcher(self.copy_launcher())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11", result.stdout)
        self.assertNotIn("-m codescout", self.log.read_text())

    def test_missing_dependencies_do_not_start_service(self):
        self.stub(dependencies=1)
        result = self.run_launcher(self.copy_launcher())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dependencies", result.stdout)
        self.assertIn("[WAIT]", result.stdout)
        self.assertNotIn("-m codescout", self.log.read_text())

    def test_nonzero_service_exit_is_reported_and_window_waits(self):
        self.stub(run=7)
        result = self.run_launcher(self.copy_launcher())
        self.assertEqual(result.returncode, 7)
        self.assertIn("exit code 7", result.stdout)
        self.assertIn("[WAIT]", result.stdout)

    def test_launcher_uses_script_directory_and_open_option(self):
        self.stub()
        result = self.run_launcher(self.copy_launcher())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.project) + " | -m codescout --open", self.log.read_text())
        self.assertNotIn("[WAIT]", result.stdout)

    def test_py_fallback_is_used_when_python_is_invalid(self):
        self.stub(version=1)
        self.stub(name="py")
        result = self.run_launcher(self.copy_launcher())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-3 -m codescout --open", self.log.read_text())

    def test_workspace_wrapper_routes_to_quoted_project_launcher(self):
        self.stub(run=9)
        result = self.run_launcher(self.copy_launcher(wrapper=True))
        self.assertEqual(result.returncode, 9)
        self.assertIn(str(self.project) + " | -m codescout --open", self.log.read_text())
        self.assertIn("exit code 9", result.stdout)
