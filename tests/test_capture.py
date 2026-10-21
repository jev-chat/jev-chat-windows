# -*- coding: utf-8 -*-
"""Offline capture-startup regressions; no Windows session or screenshots needed."""
import ctypes
import importlib.util
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, call, patch


class CaptureStartupTests(unittest.TestCase):
    HWND = 0x123456789  # Wider than a DWORD: the Win32 argument must stay pointer-sized.

    def setUp(self):
        self.affinity = 0
        self.query_ok = True
        self.query = Mock(side_effect=self.get_affinity)
        self.windows_capture = Mock()
        with patch.object(ctypes, "windll", SimpleNamespace(
            user32=SimpleNamespace(GetWindowDisplayAffinity=self.query),
        ), create=True):
            # Load separately so a fake user32 never leaks into the real app.capture module.
            spec = importlib.util.spec_from_file_location(
                "_test_capture", Path(__file__).resolve().parents[1] / "app" / "capture.py")
            self.capture = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.capture)
        capture_module = patch.dict("sys.modules", {
            "windows_capture": SimpleNamespace(WindowsCapture=self.windows_capture),
        })
        capture_module.start()
        self.addCleanup(capture_module.stop)

    def get_affinity(self, hwnd, value):
        ctypes.cast(value, ctypes.POINTER(wintypes.DWORD))[0] = self.affinity
        return self.query_ok

    def assert_started(self):
        capture = self.capture.Capture(self.HWND)
        self.query.assert_called_once_with(self.HWND, ANY)
        self.windows_capture.assert_called_once_with(
            cursor_capture=None, draw_border=None, window_hwnd=self.HWND)
        cap = self.windows_capture.return_value
        self.assertEqual(cap.event.call_args_list,
                         [call(capture.on_frame_arrived), call(capture.on_closed)])
        cap.start_free_threaded.assert_called_once_with()
        self.assertIs(capture.ctl, cap.start_free_threaded.return_value)

    def test_excluded_window_is_rejected_before_wgc_is_constructed(self):
        self.affinity = 0x11
        with self.assertRaisesRegex(RuntimeError, "WDA_EXCLUDEFROMCAPTURE") as error:
            self.capture.Capture(self.HWND)
        self.query.assert_called_once_with(self.HWND, ANY)
        self.windows_capture.assert_not_called()
        self.assertLessEqual(len(str(error.exception)), 120)  # worker.run's error limit

    def test_unprotected_window_starts_capture(self):
        self.assert_started()

    def test_failed_query_preserves_capture_startup(self):
        self.query_ok = False
        self.affinity = 0x11  # Failed calls must not trust the output value.
        self.assert_started()

    def test_query_has_explicit_win32_types(self):
        self.assertEqual(self.query.argtypes,
                         [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)])
        self.assertIs(self.query.restype, wintypes.BOOL)


if __name__ == "__main__":
    unittest.main()
