import importlib
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


class NativePickerTests(unittest.TestCase):
    def native(self):
        self.assertIsNotNone(importlib.util.find_spec("codescout.native"), "native folder picker is missing")
        return importlib.import_module("codescout.native")

    def test_selected_folder_is_returned_as_an_absolute_path(self):
        native = self.native()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native, "_show_folder_dialog", return_value=directory):
                self.assertEqual(native.choose_folder(), str(Path(directory).resolve()))

    def test_cancel_returns_none(self):
        native = self.native()
        with patch.object(native, "_show_folder_dialog", return_value=""):
            self.assertIsNone(native.choose_folder())

    def test_tk_boundary_invokes_native_directory_chooser_and_cleans_up(self):
        native = self.native()
        import tkinter
        from tkinter import filedialog

        class Window:
            destroyed = False
            withdrawn = False

            def withdraw(self):
                self.withdrawn = True

            def attributes(self, *_):
                pass

            def destroy(self):
                self.destroyed = True

        window = Window()

        def select(*, parent, title, mustexist):
            self.assertIs(parent, window)
            self.assertTrue(window.withdrawn)
            self.assertTrue(mustexist)
            self.assertIn("项目文件夹", title)
            return ""

        with patch.object(tkinter, "Tk", return_value=window), patch.object(filedialog, "askdirectory", side_effect=select):
            self.assertIsNone(native.choose_folder())
        self.assertTrue(window.destroyed)

    def test_dialog_failure_explains_native_picker_is_unavailable(self):
        native = self.native()
        with patch.object(native, "_show_folder_dialog", side_effect=RuntimeError("no display")):
            with self.assertRaisesRegex(ValueError, "文件夹.*no display"):
                native.choose_folder()

    def test_non_folder_or_relative_result_is_rejected(self):
        native = self.native()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.py"
            source.write_text("x = 1", encoding="utf-8")
            for result in [str(source), "relative/folder"]:
                with self.subTest(result=result), patch.object(native, "_show_folder_dialog", return_value=result):
                    with self.assertRaises(ValueError):
                        native.choose_folder()

    def test_concurrent_requests_have_at_most_one_active_dialog(self):
        native = self.native()
        active = 0
        maximum = 0
        count_lock = threading.Lock()

        def dialog():
            nonlocal active, maximum
            with count_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with count_lock:
                active -= 1
            return ""

        with patch.object(native, "_show_folder_dialog", side_effect=dialog):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: native.choose_folder(), range(3)))
        self.assertEqual(results, [None, None, None])
        self.assertEqual(maximum, 1)
