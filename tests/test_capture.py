from __future__ import annotations

import ctypes
import sys
import unittest
from ctypes import wintypes
from unittest.mock import Mock

import numpy as np

from deal_watch.capture import CaptureError, MssCapturer, WindowInfo, WindowsWindows
from deal_watch.models import CaptureErrorCode, Rect, RegionConfig


class CaptureValidationTests(unittest.TestCase):
    def setUp(self):
        self.region = RegionConfig("合成测试", "Synthetic", Rect(10, 20, 100, 80))
        self.window = WindowInfo(123, "Synthetic", "test", Rect(0, 0, 300, 200))
        self.capturer = MssCapturer()
        self.windows = Mock()
        self.windows.desktop_locked.return_value = False
        self.windows.find.return_value = self.window
        self.windows.occluded.return_value = False
        self.capturer._windows = self.windows

    def test_occlusion_rejects_capture_before_any_pixels_are_read(self):
        self.windows.occluded.return_value = True
        self.capturer._mss = Mock(side_effect=AssertionError("must not capture"))
        with self.assertRaises(CaptureError) as raised:
            self.capturer.capture(self.region)
        self.assertEqual(raised.exception.code, CaptureErrorCode.WINDOW_OCCLUDED)
        self.capturer._mss.assert_not_called()

    def test_lock_rejects_capture_without_enumerating_windows(self):
        self.windows.desktop_locked.return_value = True
        with self.assertRaises(CaptureError) as raised:
            self.capturer.validate(self.region)
        self.assertEqual(raised.exception.code, CaptureErrorCode.DESKTOP_LOCKED)
        self.windows.find.assert_not_called()

    def test_unobstructed_region_is_valid(self):
        self.assertEqual(self.capturer.validate(self.region), self.window)

    def test_short_new_text_is_detected_without_second_capture(self):
        original = np.full((80, 100, 4), 255, dtype=np.uint8)
        changed = original.copy()
        changed[10:13, 20:23, :3] = 0
        grab = Mock()
        grab.grab.side_effect = [original, changed]
        self.capturer._mss = lambda: grab
        self.assertIsNotNone(self.capturer.capture(self.region, force=True))
        self.assertIsNotNone(self.capturer.capture(self.region))
        self.assertEqual(grab.grab.call_count, 2)

    def test_small_changes_accumulate_against_last_submitted_frame(self):
        original = np.full((80, 100, 4), 255, dtype=np.uint8)
        first = original.copy()
        first[10:12, 20:22, :3] = 0
        second = first.copy()
        second[20:22, 20:22, :3] = 0
        grab = Mock()
        grab.grab.side_effect = [original, first, second, second]
        self.capturer._mss = lambda: grab
        self.assertIsNotNone(self.capturer.capture(self.region, force=True))
        self.assertIsNone(self.capturer.capture(self.region))
        self.assertIsNotNone(self.capturer.capture(self.region))
        self.assertIsNone(self.capturer.capture(self.region))


@unittest.skipUnless(sys.platform == "win32", "Win32 signatures")
class DesktopSafetyTests(unittest.TestCase):
    def test_handle_signatures_are_pointer_sized(self):
        windows = WindowsWindows()
        self.assertIs(windows._user32.OpenInputDesktop.restype, wintypes.HANDLE)
        self.assertEqual(ctypes.sizeof(windows._user32.OpenInputDesktop.restype), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(windows._user32.GetWindowRect.argtypes[0], wintypes.HWND)

    def test_lock_check_only_reads_desktop_name_and_closes_handle(self):
        for desktop_name, expected in (("Default", False), ("Winlogon", True)):
            with self.subTest(desktop_name=desktop_name):
                windows = object.__new__(WindowsWindows)
                api = Mock()
                api.OpenInputDesktop.return_value = 0x123456789

                def query(handle, index, buffer, length, needed, name=desktop_name):
                    needed._obj.value = (len(name) + 1) * ctypes.sizeof(ctypes.c_wchar)
                    if buffer is None:
                        return False
                    buffer.value = name
                    return True

                api.GetUserObjectInformationW.side_effect = query
                windows._user32 = api
                self.assertEqual(windows.desktop_locked(), expected)
                api.SwitchDesktop.assert_not_called()
                api.CloseDesktop.assert_called_once_with(0x123456789)

    def test_occlusion_uses_top_to_bottom_order_and_ignores_windows_below_target(self):
        windows = object.__new__(WindowsWindows)
        windows._enum_type = lambda callback: callback
        windows._user32 = Mock()
        windows._dwmapi = Mock()
        windows._dwmapi.DwmGetWindowAttribute.return_value = 1
        windows._user32.IsWindowVisible.return_value = True
        windows._user32.IsIconic.return_value = False
        windows._get_window_long = lambda hwnd, index: 0
        target = WindowInfo(20, "Synthetic", "test", Rect(0, 0, 300, 200))
        region = Rect(10, 20, 100, 80)
        rectangles = {10: Rect(150, 150, 20, 20), 30: Rect(0, 0, 300, 200)}
        windows.get_rect = rectangles.get

        def enumerate_windows(callback, parameter):
            for hwnd in (10, 20, 30):
                if not callback(hwnd, parameter):
                    break
            return True

        windows._user32.EnumWindows.side_effect = enumerate_windows
        self.assertFalse(windows.occluded(target, region))
        rectangles[10] = Rect(20, 30, 50, 50)
        self.assertTrue(windows.occluded(target, region))
        windows._get_window_long = lambda hwnd, index: 0x20
        self.assertFalse(windows.occluded(target, region))


if __name__ == "__main__":
    unittest.main()
