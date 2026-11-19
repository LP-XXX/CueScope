"""可见窗口校验和 MSS 物理像素采集，不做点击、滚动或进程注入。"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

import numpy as np

from .models import CaptureErrorCode, FrameSample, Rect, RegionConfig


class CaptureError(RuntimeError):
    def __init__(self, code: CaptureErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True, frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    rect: Rect
    process_id: int = 0
    process_path: str = ""
    minimized: bool = False
    process_start_id: str = ""
    dpi: int = 0


class WindowsWindows:
    """小型 Win32 只读封装，集中管理 ctypes 句柄生命周期。"""

    def __init__(self) -> None:
        self._user32: Any = None
        self._dwmapi: Any = None
        if sys.platform != "win32":
            return
        api = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = {
            "EnumWindows": ([self._enum_type, wintypes.LPARAM], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetWindowThreadProcessId": (
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD
            ),
            "OpenInputDesktop": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseDesktop": ([wintypes.HANDLE], wintypes.BOOL),
            "GetUserObjectInformationW": (
                [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                 ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        get_long_name = "GetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "GetWindowLongW"
        self._get_window_long = getattr(api, get_long_name)
        self._get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_window_long.restype = ctypes.c_ssize_t
        self._user32 = api
        self._dwmapi = ctypes.WinDLL("dwmapi")
        self._dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ]
        self._dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    def list_windows(self) -> list[WindowInfo]:
        if sys.platform != "win32":
            return []
        user32 = self._user32
        result: list[WindowInfo] = []

        def visit(hwnd: int, _: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            class_buffer = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            user32.GetClassNameW(hwnd, class_buffer, 256)
            rect = self.get_rect(hwnd)
            if rect and rect.width > 50 and rect.height > 50:
                result.append(
                    WindowInfo(
                        hwnd=int(hwnd),
                        title=title_buffer.value,
                        class_name=class_buffer.value,
                        rect=rect,
                        process_id=self.process_id(hwnd),
                        process_path=self.process_path(hwnd),
                        minimized=bool(user32.IsIconic(hwnd)),
                        process_start_id=self.process_start_id(hwnd),
                        dpi=self.window_dpi(hwnd),
                    )
                )
            return True

        user32.EnumWindows(self._enum_type(visit), 0)
        return sorted(result, key=lambda item: item.title.casefold())

    def process_id(self, hwnd: int) -> int:
        if sys.platform != "win32":
            return 0
        value = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(value))
        return int(value.value)

    def process_path(self, hwnd: int) -> str:
        """以最低权限查询进程映像路径；不读取进程内存。"""
        if sys.platform != "win32":
            return ""
        process_id = self.process_id(hwnd)
        if not process_id:
            return ""
        kernel32 = getattr(self, "_kernel32", None)
        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            self._kernel32 = kernel32
        handle = kernel32.OpenProcess(0x1000, False, process_id)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)

    def process_start_id(self, hwnd: int) -> str:
        if sys.platform != "win32":
            return ""
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, self.process_id(hwnd))
        if not handle:
            return ""
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return ""
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)

    def window_dpi(self, hwnd: int) -> int:
        if sys.platform != "win32":
            return 0
        method = getattr(self._user32, "GetDpiForWindow", None)
        if method is None:
            return 0
        method.argtypes, method.restype = [wintypes.HWND], wintypes.UINT
        return int(method(hwnd))

    def window_from_hwnd(self, hwnd: int) -> WindowInfo | None:
        if sys.platform != "win32" or not self._user32.IsWindow(hwnd):
            return None
        rect = self.get_rect(hwnd)
        if not rect or rect.width <= 0 or rect.height <= 0:
            return None
        length = self._user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        class_buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        self._user32.GetClassNameW(hwnd, class_buffer, 256)
        return WindowInfo(
            hwnd=int(hwnd),
            title=title_buffer.value,
            class_name=class_buffer.value,
            rect=rect,
            process_id=self.process_id(hwnd),
            process_path=self.process_path(hwnd),
            minimized=bool(self._user32.IsIconic(hwnd)),
            process_start_id=self.process_start_id(hwnd),
            dpi=self.window_dpi(hwnd),
        )

    def find(self, config: RegionConfig) -> WindowInfo | None:
        if config.window_hwnd:
            return self.window_from_hwnd(config.window_hwnd)
        candidates = [
            item
            for item in self.list_windows()
            if item.title == config.window_title
            and (not config.window_class or item.class_name == config.window_class)
        ]
        return candidates[0] if candidates else None

    def get_rect(self, hwnd: int) -> Rect | None:
        if sys.platform != "win32":
            return None

        raw = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            return None
        width, height = raw.right - raw.left, raw.bottom - raw.top
        return Rect(raw.left, raw.top, width, height) if width > 0 and height > 0 else None

    def desktop_locked(self) -> bool:
        if sys.platform != "win32":
            return False
        user32 = self._user32
        # Read the input desktop name. Never call SwitchDesktop: a monitor must
        # not change the user's desktop as a side effect of a health check.
        desktop = user32.OpenInputDesktop(0, False, 0x0001)
        if not desktop:
            return True
        try:
            needed = wintypes.DWORD()
            user32.GetUserObjectInformationW(desktop, 2, None, 0, ctypes.byref(needed))
            if not needed.value:
                return True
            name = ctypes.create_unicode_buffer(needed.value // ctypes.sizeof(ctypes.c_wchar) + 1)
            if not user32.GetUserObjectInformationW(
                desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
            ):
                return True
            return name.value.casefold() != "default"
        finally:
            user32.CloseDesktop(desktop)

    def occluded(self, window: WindowInfo, region: Rect) -> bool:
        """Conservatively detect visible top-level windows above the capture ROI.

        Bounding rectangles cannot prove visibility for every shaped/transparent
        window. Ignore click-through overlays and cloaked desktops, but pause
        on an ordinary intersecting foreground window.
        """
        if sys.platform != "win32":
            return False
        blocked = False

        def visit(hwnd: int, _: int) -> bool:
            nonlocal blocked
            if int(hwnd) == window.hwnd:
                return False
            if not self._user32.IsWindowVisible(hwnd) or self._user32.IsIconic(hwnd):
                return True
            if self._get_window_long(hwnd, -20) & 0x20:  # WS_EX_TRANSPARENT
                return True
            cloaked = wintypes.DWORD()
            if self._dwmapi.DwmGetWindowAttribute(
                hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            ) == 0 and cloaked.value:
                return True
            rect = self.get_rect(hwnd)
            if rect and (
                rect.x < region.x + region.width
                and region.x < rect.x + rect.width
                and rect.y < region.y + region.height
                and region.y < rect.y + rect.height
            ):
                blocked = True
                return False
            return True

        self._user32.EnumWindows(self._enum_type(visit), 0)
        return blocked


class MssCapturer:
    def __init__(self) -> None:
        self._local = threading.local()
        self._windows = WindowsWindows()
        self._sequences: dict[str, int] = {}
        self._last_change: dict[str, float] = {}
        self._last_signature: dict[str, np.ndarray] = {}

    def _mss(self) -> Any:
        if not hasattr(self._local, "instance"):
            import mss

            self._local.instance = mss.mss()
        return self._local.instance

    def validate(self, region: RegionConfig) -> WindowInfo:
        if self._windows.desktop_locked():
            raise CaptureError(CaptureErrorCode.DESKTOP_LOCKED, "桌面已锁定，请解锁后重新校准")
        window = self._windows.find(region)
        if not window:
            raise CaptureError(CaptureErrorCode.WINDOW_NOT_FOUND, "绑定窗口不存在或不可见")
        if region.process_id and window.process_id != region.process_id:
            raise CaptureError(
                CaptureErrorCode.PROCESS_MISMATCH, "窗口句柄所属进程已变化，请重新绑定"
            )
        if window.minimized:
            raise CaptureError(CaptureErrorCode.WINDOW_MINIMIZED, "窗口已最小化")
        if region.window_rect and window.rect != region.window_rect:
            raise CaptureError(
                CaptureErrorCode.WINDOW_MOVED, "窗口位置或尺寸已变化，请重新校准"
            )
        rect = region.rect
        bounds = window.rect
        if not (
            bounds.x <= rect.x
            and bounds.y <= rect.y
            and rect.x + rect.width <= bounds.x + bounds.width
            and rect.y + rect.height <= bounds.y + bounds.height
        ):
            raise CaptureError(CaptureErrorCode.INVALID_REGION, "采集区域超出窗口边界")
        if self._windows.occluded(window, rect):
            raise CaptureError(CaptureErrorCode.WINDOW_OCCLUDED, "采集区域被其他窗口遮挡，请移开遮挡窗口")
        return window

    def capture(self, region: RegionConfig, force: bool = False) -> FrameSample | None:
        self.validate(region)
        now = time.time()
        forced = force or now - self._last_change.get(region.id, 0) >= 10
        try:
            raw = self._mss().grab(region.rect.as_mss())
            image = np.asarray(raw, dtype=np.uint8)[:, :, :3].copy()
        except Exception as error:
            raise CaptureError(CaptureErrorCode.CAPTURE_FAILED, "区域截图失败") from error
        if image.size == 0:
            raise CaptureError(CaptureErrorCode.CAPTURE_FAILED, "截图为空")
        # Detect change on exactly the pixels queued below, not an earlier grab.
        # Compare with the last submitted image so small incremental edits can
        # accumulate instead of being forgotten on every 500ms poll.
        if not forced and not self._changed(region.id, image):
            return None
        self._last_signature[region.id] = image.copy()
        self._sequences[region.id] = self._sequences.get(region.id, 0) + 1
        self._last_change[region.id] = now
        return FrameSample(
            region_id=region.id,
            sequence=self._sequences[region.id],
            captured_at=now,
            image=image,
            rect=region.rect,
            forced=forced,
        )

    def _changed(self, region_id: str, image: np.ndarray) -> bool:
        previous = self._last_signature.get(region_id)
        if previous is None or previous.shape != image.shape:
            return True
        difference = np.abs(image.astype(np.int16) - previous.astype(np.int16))
        # Eight changed pixels reject isolated display noise without diluting a
        # short new word by the area of a large otherwise unchanged chat ROI.
        return int(np.count_nonzero(np.any(difference > 16, axis=2))) >= 8

    def reset(self, region_id: str) -> None:
        self._last_signature.pop(region_id, None)
        self._last_change.pop(region_id, None)
