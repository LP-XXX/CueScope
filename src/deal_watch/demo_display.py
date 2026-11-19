"""A user-operated synthetic text source. It never reads or controls another window."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

DISPLAY1 = r"\\.\DISPLAY1"


def display1_work_area() -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        raise RuntimeError("此演示窗口需要 Windows；没有显示任何窗口")
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                         callback_type, wintypes.LPARAM]
    result = []

    def visit(handle, _dc, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)) and info.szDevice.upper() == DISPLAY1:
            rect = info.rcWork
            result.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, callback_type(visit), 0)
    if len(result) != 1:
        raise RuntimeError("无法唯一定位 Windows DISPLAY1；没有显示任何窗口")
    return result[0]


def window_device(hwnd: int) -> str:
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    handle = user32.MonitorFromWindow(hwnd, 2)
    if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.szDevice.upper()


def place_hidden_window(hwnd: int, bounds: tuple[int, int, int, int]) -> None:
    """Place only the owned, still-hidden demo HWND using physical Win32 coordinates."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    left, top, right, bottom = bounds
    width, height = min(1000, right - left - 48), min(760, bottom - top - 48)
    if width < 480 or height < 360:
        raise RuntimeError("DISPLAY1 工作区太小；没有显示任何窗口")
    x, y = left + (right - left - width) // 2, top + (bottom - top - height) // 2
    if not user32.SetWindowPos(hwnd, None, x, y, width, height, 0x0004 | 0x0010):
        raise ctypes.WinError(ctypes.get_last_error())
    actual = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(actual)):
        raise ctypes.WinError(ctypes.get_last_error())
    if window_device(hwnd) != DISPLAY1:
        raise RuntimeError("演示窗口没有映射到 Windows DISPLAY1；没有显示任何窗口")
    if not (left <= actual.left < actual.right <= right and top <= actual.top < actual.bottom <= bottom):
        raise RuntimeError("演示窗口未完整位于 DISPLAY1；没有显示任何窗口")
