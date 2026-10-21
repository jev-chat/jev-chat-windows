"""Offline fill regression: mock Win32 calls, never touch a real window or clipboard."""
import ctypes
import importlib.util
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, call, patch


class FillTests(unittest.TestCase):
    def setUp(self):
        mocks = ExitStack()
        self.addCleanup(mocks.close)
        windll = Mock()
        mocks.enter_context(patch.object(ctypes, "windll", windll, create=True))
        # Load the real fill module without importing the Windows capture dependencies.
        spec = importlib.util.spec_from_file_location(
            "fill_under_test", Path(__file__).resolve().parents[1] / "app" / "fill.py")
        self.fill = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fill)
        self.clipboard = mocks.enter_context(patch.object(self.fill, "set_clipboard"))
        mocks.enter_context(patch.object(self.fill, "time"))
        capture = ModuleType("app.capture")
        capture.unminimize = Mock()
        mocks.enter_context(patch.dict(sys.modules, {"app.capture": capture}))

        def window_rect(hwnd, attribute, rect, size):
            rect._obj.left, rect._obj.top = 100, 200
            return 0

        def cursor_pos(point):
            point._obj.x, point._obj.y = 11, 22
            return 1

        def system_metric(index):
            self.assertEqual(index, 23)  # SM_SWAPBUTTON
            return self.swapped

        self.u32 = windll.user32
        windll.dwmapi.DwmGetWindowAttribute.side_effect = window_rect
        self.u32.GetForegroundWindow.return_value = 100
        self.u32.GetCursorPos.side_effect = cursor_pos
        self.u32.GetSystemMetrics.side_effect = system_metric

    def assert_fill(self, swapped, down, up):
        self.swapped = swapped
        self.u32.reset_mock()
        self.clipboard.reset_mock()
        self.fill.fill(100, (20, 30, 420, 500), "候选回复")
        self.assertEqual(self.u32.mouse_event.call_args_list, [
            call(down, 0, 0, 0, 0), call(up, 0, 0, 0, 0)])
        self.clipboard.assert_called_once_with("候选回复")
        self.assertEqual(self.u32.SetCursorPos.call_args_list, [
            call(180, 740), call(11, 22)])
        # Still append with Ctrl+End, then paste with Ctrl+V; never send Enter.
        self.assertEqual(self.u32.keybd_event.call_args_list, [
            call(0x11, 0, 0, 0), call(0x23, 0, 0, 0),
            call(0x23, 0, 2, 0), call(0x11, 0, 2, 0),
            call(0x11, 0, 0, 0), call(0x56, 0, 0, 0),
            call(0x56, 0, 2, 0), call(0x11, 0, 2, 0)])

    def test_default_primary_button(self):
        self.assert_fill(0, 0x2, 0x4)

    def test_swapped_primary_button(self):
        self.assert_fill(1, 0x8, 0x10)

    def test_nonzero_swap_value(self):
        self.assert_fill(2, 0x8, 0x10)

    def test_setting_changes_between_fills(self):
        for swapped, down, up in ((0, 0x2, 0x4), (1, 0x8, 0x10), (0, 0x2, 0x4)):
            with self.subTest(swapped=swapped):
                self.assert_fill(swapped, down, up)


if __name__ == "__main__":
    unittest.main()
