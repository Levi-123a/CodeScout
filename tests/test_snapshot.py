import os
import tempfile
import unittest
import subprocess
from pathlib import Path

from codescout.snapshot import capture


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_digest_tracks_content_and_deletion(self):
        self.write("main.py", "print(1)\n")
        first = capture(self.root)
        self.assertEqual(first.files, {"main.py": "print(1)\n"})
        self.assertEqual(first.digest, capture(self.root).digest)
        self.write("main.py", "print(2)\n")
        self.assertNotEqual(first.digest, capture(self.root).digest)
        (self.root / "main.py").unlink()
        self.assertEqual(capture(self.root).files, {})

    def test_excludes_dependencies_credentials_binary_and_custom_paths(self):
        for name in ["AGENTS.md", "src/CLAUDE.md", "main.py", "node_modules/a.js", ".env", "private.pem", "generated/x.txt"]:
            self.write(name, "hello")
        (self.root / "image.png").write_bytes(b"\x00\xff\x01")
        result = capture(self.root, ["generated/**"])
        self.assertEqual(set(result.files), {"AGENTS.md", "src/CLAUDE.md", "main.py"})
        self.assertIn(".env", result.excluded)
        self.assertIn("image.png", result.excluded)
        self.assertIsNone(result.git)

    def test_large_file_is_uncovered_and_reported(self):
        self.write("big.txt", "x" * (256 * 1024 + 1))
        result = capture(self.root)
        self.assertNotIn("big.txt", result.files)
        self.assertTrue(result.warnings)
        self.assertIn("big.txt", result.excluded)

    def test_missing_project_fails_instead_of_becoming_empty(self):
        with self.assertRaises(ValueError):
            capture(self.root / "missing")

    def test_symlink_does_not_send_external_content(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "secret.txt"
            target.write_text("outside", encoding="utf-8")
            try:
                os.symlink(target, self.root / "linked.txt")
            except OSError:
                self.skipTest("OS does not permit creating symlinks")
            result = capture(self.root)
            self.assertNotIn("linked.txt", result.files)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_does_not_send_external_content(self):
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "private.txt").write_text("outside", encoding="utf-8")
            link = self.root / "linked"
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), outside], capture_output=True)
            self.assertEqual(created.returncode, 0)
            self.addCleanup(lambda: link.rmdir() if link.exists() else None)
            snapshot = capture(self.root)
            self.assertNotIn("linked/private.txt", snapshot.files)
            self.assertIn("linked/", snapshot.excluded)

    def test_open_file_read_is_bounded_even_without_precheck(self):
        from codescout.snapshot import read_file
        self.write("big.txt", "x" * (256 * 1024 + 1))
        with self.assertRaises(ValueError):
            read_file(self.root, self.root / "big.txt")

    def test_open_handle_outside_root_is_rejected(self):
        from codescout.snapshot import read_file
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "private.txt"
            path.write_text("outside", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_file(self.root, path)

