import importlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ConventionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.rules_path = self.root / "CODESCOUT_RULES.md"

    def conventions(self):
        self.assertIsNotNone(importlib.util.find_spec("codescout.conventions"), "independent conventions helper is missing")
        return importlib.import_module("codescout.conventions")

    def test_absent_rules_are_empty_and_load_does_not_create_a_file(self):
        conventions = self.conventions()
        (self.root / "AGENTS.md").write_text("unrelated instructions", encoding="utf-8")
        self.assertEqual(conventions.load_rules(self.root), "")
        self.assertFalse(self.rules_path.exists())

    def test_save_creates_a_readable_purpose_named_utf8_markdown_file(self):
        conventions = self.conventions()
        conventions.save_rules(self.root, "# 项目约定\n注释需与实际行为一致。\n", expected=None)
        self.assertEqual(self.rules_path.read_bytes(), "# 项目约定\n注释需与实际行为一致。\n".encode("utf-8"))
        self.assertEqual(conventions.load_rules(self.root), "# 项目约定\n注释需与实际行为一致。\n")

    def test_matching_expected_text_updates_existing_rules(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"old\n")
        conventions.save_rules(self.root, "new\n", expected="old\n")
        self.assertEqual(self.rules_path.read_bytes(), b"new\n")

    def test_external_edit_conflict_preserves_new_content(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"original\n")
        seen = conventions.load_rules(self.root)
        self.rules_path.write_bytes(b"user edited\n")
        with self.assertRaisesRegex(ValueError, "改变|冲突|修改"):
            conventions.save_rules(self.root, "browser edit\n", expected=seen)
        self.assertEqual(self.rules_path.read_bytes(), b"user edited\n")

    def test_missing_expected_never_overwrites_unknown_existing_content(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"user authored rules\n")
        with self.assertRaises(ValueError):
            conventions.save_rules(self.root, "replacement", expected=None)
        self.assertEqual(self.rules_path.read_bytes(), b"user authored rules\n")

    def test_crlf_is_preserved_and_source_comparison_is_exact(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"first\r\nsecond\r\n")
        self.assertEqual(conventions.load_rules(self.root), "first\r\nsecond\r\n")
        with self.assertRaises(ValueError):
            conventions.save_rules(self.root, "new\n", expected="first\nsecond\n")
        conventions.save_rules(self.root, "first\nupdated\n", expected="first\r\nsecond\r\n")
        self.assertEqual(self.rules_path.read_bytes(), b"first\r\nupdated\r\n")

    def test_too_large_text_and_invalid_utf8_leave_disk_unchanged(self):
        conventions = self.conventions()
        with self.assertRaises(ValueError):
            conventions.save_rules(self.root, "x" * 20001, expected=None)
        self.assertFalse(self.rules_path.exists())
        self.rules_path.write_bytes(b"\xff\xfeunknown")
        with self.assertRaises(ValueError):
            conventions.load_rules(self.root)
        with self.assertRaises(ValueError):
            conventions.save_rules(self.root, "new", expected=None)
        self.assertEqual(self.rules_path.read_bytes(), b"\xff\xfeunknown")

    def test_preserving_crlf_must_not_create_an_unreadable_over_limit_file(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"original\r\n")
        with self.assertRaises(ValueError):
            conventions.save_rules(self.root, "x\n" * 10000, expected="original\r\n")
        self.assertEqual(self.rules_path.read_bytes(), b"original\r\n")

    def test_edit_while_preparing_replacement_is_detected(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"original")
        real_mkstemp = conventions.tempfile.mkstemp

        def prepare(**options):
            descriptor, name = real_mkstemp(**options)
            self.rules_path.write_bytes(b"external edit")
            return descriptor, name

        with patch.object(conventions.tempfile, "mkstemp", side_effect=prepare):
            with self.assertRaises(ValueError):
                conventions.save_rules(self.root, "new", expected="original")
        self.assertEqual(self.rules_path.read_bytes(), b"external edit")
        self.assertEqual(list(self.root.iterdir()), [self.rules_path])

    def test_file_created_between_final_check_and_first_save_is_never_overwritten(self):
        conventions = self.conventions()
        real_read = conventions._read_source
        reads = 0

        def concurrent_creation(path):
            nonlocal reads
            data = real_read(path)
            reads += 1
            if reads == 2:
                self.rules_path.write_bytes(b"new user file")
            return data

        with patch.object(conventions, "_read_source", side_effect=concurrent_creation):
            with self.assertRaises(ValueError):
                conventions.save_rules(self.root, "browser rules", expected=None)
        self.assertEqual(self.rules_path.read_bytes(), b"new user file")
        self.assertEqual(list(self.root.iterdir()), [self.rules_path])

    def test_atomic_replace_failure_keeps_original_and_removes_temporary_file(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"original")
        with patch.object(conventions.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(ValueError, "disk unavailable"):
                conventions.save_rules(self.root, "new", expected="original")
        self.assertEqual(self.rules_path.read_bytes(), b"original")
        self.assertEqual(list(self.root.iterdir()), [self.rules_path])

    def test_file_symlink_to_outside_is_rejected(self):
        conventions = self.conventions()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_bytes(b"outside original")
        try:
            self.rules_path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink permission unavailable: {exc}")
        with self.assertRaises(ValueError):
            conventions.load_rules(self.root)
        with self.assertRaises(ValueError):
            conventions.save_rules(self.root, "new", expected="outside original")
        self.assertEqual(outside.read_bytes(), b"outside original")

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary")
    def test_project_junction_outside_is_rejected(self):
        conventions = self.conventions()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "CODESCOUT_RULES.md").write_bytes(b"outside original")
        linked = Path(self.temp.name) / "linked"
        command = f'mklink /J "{linked}" "{outside}"'
        result = subprocess.run(f'"{os.environ["COMSPEC"]}" /d /c {command}', capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.rmdir, linked)
        with self.assertRaises(ValueError):
            conventions.load_rules(linked)
        with self.assertRaises(ValueError):
            conventions.save_rules(linked, "new", expected="outside original")
        self.assertEqual((outside / "CODESCOUT_RULES.md").read_bytes(), b"outside original")

    def test_absolute_root_and_fixed_filename_are_required(self):
        conventions = self.conventions()
        with self.assertRaises(ValueError):
            conventions.save_rules(Path("relative"), "new", expected=None)
        with patch.object(conventions, "RULES_FILENAME", "../outside.md"):
            with self.assertRaises(ValueError):
                conventions.save_rules(self.root, "new", expected=None)

    def test_opened_rules_handle_outside_project_is_rejected_before_read(self):
        conventions = self.conventions()
        self.rules_path.write_bytes(b"rules")
        outside = Path(self.temp.name) / "outside.md"

        def outside_handle(source, fallback):
            self.assertEqual(source.tell(), 0)
            return outside

        with patch.object(conventions, "_handle_path", side_effect=outside_handle, create=True):
            with self.assertRaises(ValueError):
                conventions.load_rules(self.root)
