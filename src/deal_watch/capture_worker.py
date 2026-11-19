"""独立窗口采集 worker：WGC 默认，PrintWindow 兼容。

本进程只输出 JSON；父进程负责超时和废弃迟到结果。
源码运行时从项目 .capture-runtime 导入隔离依赖，主 OCR 环境不加载这里的 OpenCV。

0.2.2 追加的**阶段通道**走 stderr（逐行 JSON，每次请求 ≤16 行）：子进程被父进程
强杀时 stdout 的完整 payload 不存在，但已经 flush 进管道的阶段行还在，父进程因此
仍能说出“卡在哪个阶段”。通道里只允许阶段名、毫秒数与请求编号——禁止图像、OCR
文本、聊天正文、窗口标题与任何凭据。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ROOT / ".capture-runtime"))


class CapturePayloadError(RuntimeError):
    """带诊断字段的采集错误：``main()`` 会把 stats 一并写进错误 payload。"""

    def __init__(self, message: str, stats: dict | None = None) -> None:
        super().__init__(message)
        self.stats: dict = dict(stats or {})


# ---------------------------------------------------------------------------
# stderr 阶段通道
# ---------------------------------------------------------------------------

# 阶段行只记录累计 t_ms 里程碑；相邻阶段耗时使用 payload 的 delta 值，不能把
# stderr 的累计 t_ms 当独立耗时相加。有限行数包含 WGC import/构造/启动请求里程碑。
MAX_STAGE_LINES = 16

# WGC 会话退出上限：默认 500ms，硬上限 1000ms（父进程按同一上限下发）。
DEFAULT_SESSION_EXIT_TIMEOUT_MS = 500
MAX_SESSION_EXIT_TIMEOUT_MS = 1000


def clamp_session_exit_timeout_ms(value: int | float) -> float:
    """把 ``--session-exit-timeout-ms`` 夹到 ``[1, 1000]`` 毫秒并换算成秒。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = DEFAULT_SESSION_EXIT_TIMEOUT_MS
    if number <= 0:
        number = DEFAULT_SESSION_EXIT_TIMEOUT_MS
    return min(number, MAX_SESSION_EXIT_TIMEOUT_MS) / 1000.0


class StageChannel:
    """一次性请求的阶段打点器：写 stderr、累计分段耗时、记住最后阶段。

    * ``mark(name)`` 返回**本次阶段耗时（毫秒）**，同时把 ``name`` 记入
      ``stages["<name>_ms"]``（与成功 payload 的 ``stages`` 字段一一对应）；
    * ``t_ms`` 是相对 ``component_enter`` 的绝对偏移，便于事后画时间轴；
    * 行数有上限，且 ``sys.stderr`` 为 ``None``（窗口化冻结产物）时静默跳过——
      阶段通道是诊断旁路，任何情况下都不能影响采集本身。
    """

    def __init__(self, limit: int = MAX_STAGE_LINES) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._emitted = 0
        self._origin = time.monotonic()
        self._last_t_ms = 0.0
        self.request_id = ""
        self.region_id = ""
        self.generation = 0
        self.stages: dict[str, float] = {}
        self.last_stage = ""

    def reset(self, request_id: str = "") -> None:
        with self._lock:
            self._emitted = 0
            self._origin = time.monotonic()
            self._last_t_ms = 0.0
            self.request_id = request_id
            self.region_id = ""
            self.generation = 0
            self.stages = {}
            self.last_stage = ""

    def mark(self, stage: str) -> float:
        """记录一个阶段并返回本次耗时（毫秒，相对上一个阶段）。"""
        now = time.monotonic()
        with self._lock:
            t_ms = round((now - self._origin) * 1000.0, 1)
            delta = round(max(0.0, t_ms - self._last_t_ms), 1)
            self._last_t_ms = t_ms
            self.stages[f"{stage}_ms"] = delta
            self.last_stage = stage
            self._write(stage, t_ms, delta)
            return delta

    def _write(self, stage: str, t_ms: float, delta_ms: float) -> None:
        if self._emitted >= self._limit:
            return
        stream = sys.stderr
        if stream is None:
            return
        payload = {"stage": stage, "t_ms": t_ms, "delta_ms": delta_ms,
                   "request_id": self.request_id}
        if self.region_id:
            payload.update(region_id=self.region_id, generation=self.generation, pid=os.getpid())
        try:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
        except (OSError, ValueError):
            return
        self._emitted += 1


# 进程级单例：main() 每个进程只处理一个请求。
CHANNEL = StageChannel()


def encode(
    image,
    width: int,
    height: int,
    *,
    channel: StageChannel | None = None,
    frame_width: int | None = None,
    frame_height: int | None = None,
    roi: tuple[int, int, int, int] | None = None,
    stats: dict | None = None,
) -> dict:
    """把**最终要回传的图**编码一次。

    ``width/height`` 是返回图尺寸；``frame_width/frame_height`` 是原始整帧尺寸；
    ``roi`` 原样回显请求的区域（整帧请求为 ``None``）。统计字段默认按传入图算，
    但采集路径必须传**整帧**的 ``stats``：ROI 的墨水占比不能替代整窗诊断。
    """
    import cv2

    success, buffer = cv2.imencode(".png", image)
    if not success:
        raise RuntimeError("PNG_ENCODE_FAILED")
    if channel is not None:
        channel.mark("encode")
    payload = {
        "ok": True,
        "width": int(width),
        "height": int(height),
        "frame_width": int(frame_width if frame_width is not None else width),
        "frame_height": int(frame_height if frame_height is not None else height),
        "roi": list(roi) if roi is not None else None,
        "captured_at": time.time(),
        "image": base64.b64encode(buffer.tobytes()).decode("ascii"),
    }
    # 两个后端都输出边框与墨水占比：事后可按帧核验“空白帧”判定是否成立。
    payload.update(stats if stats is not None else frame_stats(image))
    return payload


def is_black(image) -> bool:
    if image is None or image.size == 0:
        return True
    import numpy as np

    if image.shape[0] <= 0 or image.shape[1] <= 0:
        return True
    # 同色整帧或近黑整帧视为无效，不把黑帧当作成功采集。
    if float(np.max(image)) <= 2.0:
        return True
    return bool(np.ptp(image) <= 1)


# 空白帧判据：先自动检出均匀边框，再看内部区域的墨水占比与纯色占比。
_UNIFORM_BORDER_LIMIT = 16
_UNIFORM_BORDER_DELTA = 6.0
_BLANK_INK_DELTA = 24.0
_BLANK_INK_MAX = 0.004
_BLANK_SOLID_DELTA = 6.0
_BLANK_SOLID_MIN = 0.98


def uniform_border(image, limit: int = _UNIFORM_BORDER_LIMIT) -> tuple[int, int, int, int]:
    """从四条边向内扫描“整行/整列与自身中位数偏差 <6”的均匀边框宽度。

    PrintWindow 对现代微信返回的帧带 8–11px 纯色边框，WGC 帧通常只有 0–2px。
    上限 limit 与“不超过该方向 1/4”同时生效，保证内部总留得出可判区域。
    """
    if image is None:
        return (0, 0, 0, 0)
    import numpy as np

    pixels = np.asarray(image)
    if pixels.size == 0 or pixels.ndim < 2:
        return (0, 0, 0, 0)
    height, width = pixels.shape[:2]
    if height < 2 or width < 2:
        return (0, 0, 0, 0)
    data = pixels.astype(np.int32)
    if data.ndim == 2:
        data = data[:, :, None]
    limit_v = max(0, min(limit, height // 4))
    limit_h = max(0, min(limit, width // 4))

    def uniform(line) -> bool:
        flat = line.reshape(-1, line.shape[-1]).astype(np.float32)
        median = np.median(flat, axis=0)
        return bool(float(np.abs(flat - median).max()) < _UNIFORM_BORDER_DELTA)

    top = 0
    while top < limit_v and uniform(data[top]):
        top += 1
    bottom = 0
    while bottom < limit_v and uniform(data[height - 1 - bottom]):
        bottom += 1
    left = 0
    while left < limit_h and uniform(data[:, left]):
        left += 1
    right = 0
    while right < limit_h and uniform(data[:, width - 1 - right]):
        right += 1
    return (top, bottom, left, right)


def _blank_measures(image, border=None) -> tuple[float, float]:
    """返回（内部墨水占比, 内部纯色占比）。

    墨水 = 与内部中位色最大通道差 >24 的像素；纯色 = 差 ≤6 的像素。
    边框本身已经由 ``uniform_border`` 排除，否则 11px 白边框会把墨水占比稀释。
    """
    import numpy as np

    pixels = np.asarray(image)
    if pixels.size == 0 or pixels.ndim < 2:
        return (0.0, 1.0)
    height, width = pixels.shape[:2]
    if border is None:
        border = uniform_border(pixels)
    top, bottom, left, right = border
    inner = pixels[top : max(top, height - bottom), left : max(left, width - right)]
    if inner.size == 0:
        return (0.0, 1.0)
    flat = inner.reshape(-1, inner.shape[-1]) if inner.ndim == 3 else inner.reshape(-1, 1)
    flat = flat.astype(np.float32)
    median = np.median(flat, axis=0)
    delta = np.abs(flat - median).max(axis=1)
    ink = float((delta > _BLANK_INK_DELTA).mean())
    solid = float((delta <= _BLANK_SOLID_DELTA).mean())
    return (ink, solid)


def frame_ink_ratio(image, border=None) -> float:
    """内部区域墨水占比；写进 payload 便于事后核验空白帧判定。"""
    return _blank_measures(image, border)[0]


def frame_stats(image) -> dict:
    """边框宽度与内部墨水占比，作为采集 payload 的诊断字段。"""
    border = uniform_border(image)
    inks = frame_ink_ratio(image, border)
    return {
        "uniform_border": {
            "top": int(border[0]), "bottom": int(border[1]),
            "left": int(border[2]), "right": int(border[3]),
        },
        "ink_ratio": round(float(inks), 4),
    }


def is_blank(image) -> bool:
    """判“整片纯色”的空白帧：PrintWindow 对现代微信会返回这种帧且 ok=true。

    只内缩 3px 不够：PrintWindow 空白帧带 8–11px 纯色边框，内缩 3px 后
    边框仍在内部，同色占比到不了阈值，会被当成正常帧（界面显示“监控中”
    却永远识别不到文字）。这里先检出均匀边框再看内部墨水占比：
    两条判据都满足才算空白，正常帧（墨水 >0.4%）与照片窗口都不会被误判。
    """
    if image is None:
        return True
    import numpy as np

    if getattr(image, "size", 0) == 0 or image.ndim < 2:
        return True
    height, width = image.shape[:2]
    if height < 8 or width < 8:
        return False
    ink, solid = _blank_measures(np.asarray(image))
    return bool(ink < _BLANK_INK_MAX and solid >= _BLANK_SOLID_MIN)


_PER_MONITOR_AWARE_V2 = -4


def set_dpi_awareness() -> dict:
    """在取得任何 Win32 坐标之前请求每监视器 V2 DPI 感知。

    SetProcessDpiAwarenessContext 在 user32（不在 shcore），且返回 BOOL；
    这里按 BOOL 语义判成功，并把 GetLastError 一并带回，便于诊断。
    """
    if sys.platform != "win32":
        return {"request": "not_windows", "last_error": 0}
    from ctypes import wintypes

    last_error = 0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_void_p]
            setter.restype = wintypes.BOOL
            if setter(ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
                return {"request": "per_monitor_v2", "last_error": 0}
            last_error = ctypes.get_last_error()
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        setter = getattr(shcore, "SetProcessDpiAwareness", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_int]
            setter.restype = ctypes.c_long
            if setter(2) == 0:
                return {"request": "per_monitor", "last_error": last_error}
            last_error = ctypes.get_last_error() or last_error
    except (AttributeError, OSError):
        pass
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = getattr(user32, "SetProcessDPIAware", None)
        if setter is not None and setter():
            return {"request": "system", "last_error": last_error}
        last_error = ctypes.get_last_error() or last_error
    except (AttributeError, OSError):
        pass
    return {"request": "unavailable", "last_error": int(last_error)}


def query_dpi_awareness() -> str:
    """查询本线程实际的 DPI 感知级别；老系统或缺 API 时返回 unknown。"""
    if sys.platform != "win32":
        return "unknown"
    from ctypes import wintypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_context = getattr(user32, "GetThreadDpiAwarenessContext", None)
        get_awareness = getattr(user32, "GetAwarenessFromDpiAwarenessContext", None)
        are_equal = getattr(user32, "AreDpiAwarenessContextsEqual", None)
        if get_context is None or get_awareness is None:
            return "unknown"
        get_context.argtypes = []
        get_context.restype = ctypes.c_void_p
        get_awareness.argtypes = [ctypes.c_void_p]
        get_awareness.restype = ctypes.c_int
        context = get_context()
        if not context:
            return "unknown"
        if are_equal is not None:
            are_equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            are_equal.restype = wintypes.BOOL
            if are_equal(ctypes.c_void_p(context), ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
                return "per_monitor_v2"
        value = int(get_awareness(ctypes.c_void_p(context)))
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"
    return {0: "unaware", 1: "system", 2: "per_monitor"}.get(value, "unknown")


def stop_session(control, timeout: float) -> bool:
    """Bound BOTH stop and wait; native calls may block even before wait begins.

    The parent remains the final deadline enforcer when a native call holds the GIL.
    A false result poisons a persistent worker: it must exit instead of accumulating
    further cleanup threads. Exceptions are not evidence of a completed exit.
    """
    done = threading.Event()
    outcome = [False]
    deadline = time.monotonic() + max(0.0, timeout)

    def _stop_and_wait() -> None:
        try:
            try:
                control.stop()
            except Exception:
                # A session which already stopped may reject stop; wait must still
                # positively confirm its completion before this worker is reusable.
                pass
            control.wait()
            outcome[0] = True
        except Exception:
            pass
        finally:
            done.set()

    worker = threading.Thread(target=_stop_and_wait, name="wgc-session-exit", daemon=True)
    worker.start()
    return bool(done.wait(max(0.0, deadline - time.monotonic())) and outcome[0]
                and time.monotonic() <= deadline)


def capture_wgc(
    hwnd: int,
    timeout: float = 3.0,
    *,
    session_exit_timeout: float = 0.5,
    channel: StageChannel | None = None,
    roi: tuple[int, int, int, int] | None = None,
) -> dict:
    """One fresh frame; construction, native start and frame wait share a deadline.

    Reusable workers keep imported dependencies, never a running WGC session.
    A parent-side watchdog independently enforces this budget if native code holds
    the GIL. Timed-out start/stop helpers poison the worker and are never reused.
    """
    marks = channel or CHANNEL
    marks.mark("deps_init_started")
    from windows_capture import WindowsCapture

    marks.mark("deps_init")
    result: dict = {}
    captured = threading.Event()
    closed = threading.Event()
    cancelled = threading.Event()
    startup_done = threading.Event()
    first_claimed = threading.Event()
    frame_lock = threading.Lock()
    holder: dict = {}
    deadline = time.monotonic() + max(0.0, min(float(timeout), 3.0))
    marks.mark("wgc_construct_started")

    def on_frame_arrived(frame, _control):
        with frame_lock:
            if cancelled.is_set() or first_claimed.is_set():
                return
            first_claimed.set()
        # Distinguish "no native callback" from a callback blocked while mapping,
        # copying or validating the first frame. This still fits the 16-line cap.
        marks.mark("frame_callback_enter")
        current: dict = {}
        try:
            if time.monotonic() > deadline:
                raise TimeoutError("WGC_FRAME_TIMEOUT")
            image = frame.convert_to_bgr().frame_buffer.copy()
            height, width = int(image.shape[0]), int(image.shape[1])
            current.update(frame_width=width, frame_height=height,
                           stats=frame_stats(image), black=is_black(image),
                           image=crop_frame(image, roi) if roi is not None else image,
                           roi=roi)
            if time.monotonic() > deadline:
                raise TimeoutError("WGC_FRAME_TIMEOUT")
        except (RoiError, TimeoutError) as error:
            current = {"error": error}
        except Exception:
            # Do not expose exception text from the native callback or leave the
            # waiting thread unaware that conversion/validation has failed.
            current = {"error": RuntimeError("WGC_CALLBACK_FAILED")}
        with frame_lock:
            if cancelled.is_set():
                return
            result.update(current)
            result["frame_ready_at"] = time.monotonic()
            if "error" not in result:
                marks.mark("first_valid_frame")
            captured.set()
        # Stop is performed by the bounded cleanup helper, never synchronously in
        # this callback. Further frames are ignored while that helper closes WGC.

    def on_closed():
        closed.set()
        captured.set()

    def _start() -> None:
        capture = None
        try:
            capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)
            holder["capture"] = capture
            if cancelled.is_set():
                return
            marks.mark("wgc_construct_finished")
            capture.event(on_frame_arrived)
            capture.event(on_closed)
            marks.mark("wgc_start_requested")
            control = capture.start_free_threaded()
            holder["control"] = control
            holder["started_at"] = time.monotonic()
            if cancelled.is_set():
                stop_session(control, session_exit_timeout)
            else:
                marks.mark("wgc_start")
        except Exception:
            holder["error"] = RuntimeError("COMPONENT_START_FAILED")
        finally:
            startup_done.set()

    starter = threading.Thread(target=_start, name="wgc-session-start", daemon=True)
    starter.start()
    if not startup_done.wait(max(0.0, deadline - time.monotonic())):
        cancelled.set()
        with frame_lock:
            result.clear()
        raise TimeoutError("WGC_START_TIMEOUT")
    if "error" in holder:
        cancelled.set()
        raise holder["error"]
    control = holder.get("control")
    if control is None:
        cancelled.set()
        raise RuntimeError("COMPONENT_START_FAILED")
    start_timeout = holder.get("started_at", deadline + 1) > deadline
    frame_timeout = False
    session_closed = False
    exited = False
    try:
        if not captured.wait(max(0.0, deadline - time.monotonic())):
            frame_timeout = True
        session_closed = closed.is_set() and "image" not in result and "error" not in result
        if result.get("frame_ready_at", deadline + 1) > deadline and not session_closed:
            frame_timeout = True
    finally:
        cancelled.set()
        marks.mark("session_stop")
        exited = stop_session(control, session_exit_timeout)
        marks.mark("session_exit")
        if exited:
            # Break the native object <-> bound callback cycle only after confirmed
            # exit. Each subsequent request gets completely new callback state.
            capture = holder.get("capture")
            for attribute in ("frame_handler", "closed_handler", "capture"):
                if capture is not None and hasattr(capture, attribute):
                    setattr(capture, attribute, None)
            holder.clear()
    try:
        if not exited:
            raise RuntimeError("WGC_SESSION_EXIT_TIMEOUT")
        if start_timeout:
            raise TimeoutError("WGC_START_TIMEOUT")
        if frame_timeout:
            raise TimeoutError("WGC_FRAME_TIMEOUT")
        if session_closed:
            raise RuntimeError("WGC_SESSION_CLOSED")
        error = result.get("error")
        if error is not None:
            raise error
        image = result.get("image")
        if image is None:
            raise RuntimeError("WGC_NO_FRAME")
        if result.get("black"):
            raise RuntimeError("BLACK_FRAME")
        height, width = int(image.shape[0]), int(image.shape[1])
        return encode(image, width, height, channel=marks,
                      frame_width=int(result["frame_width"]),
                      frame_height=int(result["frame_height"]), roi=roi, stats=result["stats"])
    finally:
        with frame_lock:
            result.clear()


def capture_print_window(
    hwnd: int,
    *,
    channel: StageChannel | None = None,
    roi: tuple[int, int, int, int] | None = None,
) -> dict:
    from ctypes import wintypes

    import numpy as np

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GET_WINDOW_RECT_FAILED")
    width, height = int(rect.right - rect.left), int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise RuntimeError("INVALID_WINDOW_SIZE")

    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        # PW_RENDERFULLCONTENT = 2，尽量取得现代窗口内容。
        rendered = user32.PrintWindow(hwnd, memory_dc, 2)
        if not rendered:
            raise RuntimeError("PRINTWINDOW_FAILED")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(header)
        header.biWidth = width
        header.biHeight = -height  # 自上而下
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0  # BI_RGB
        row_size = ((width * 32 + 31) // 32) * 4
        buffer = np.empty(row_size * height, dtype=np.uint8)
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer.ctypes.data_as(ctypes.POINTER(ctypes.c_void_p)),
            ctypes.byref(header),
            0,
        )
        if copied != height:
            raise RuntimeError("GETDIBITS_FAILED")
        bgra = buffer.reshape(height, row_size)[:, : width * 4].reshape(height, width, 4)
        image = np.ascontiguousarray(bgra[:, :, :3])
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
    stats = frame_stats(image)
    if is_black(image):
        raise RuntimeError("BLACK_FRAME")
    if is_blank(image):
        # 现代微信经 PrintWindow 会返回全白空白帧且 ok=true；必须显式报错，
        # 绝不静默降级到桌面截图。诊断数字随错误 payload 一起回传。
        raise CapturePayloadError(
            "PRINTWINDOW_BLANK_FRAME: PrintWindow 无法取得当前微信内容（空白帧），请改用 WGC",
            stats,
        )
    marks = channel or CHANNEL
    marks.mark("print_window")
    # 裁剪发生在编码之前：只编码一次 ROI，不编码整帧。
    cropped = crop_frame(image, roi) if roi is not None else image
    cropped_height, cropped_width = int(cropped.shape[0]), int(cropped.shape[1])
    payload = encode(
        cropped,
        cropped_width,
        cropped_height,
        channel=marks,
        frame_width=width,
        frame_height=height,
        roi=roi,
        stats=stats,
    )
    payload["window_rect"] = {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": width,
        "height": height,
    }
    client = RECT()
    if user32.GetClientRect(hwnd, ctypes.byref(client)):
        origin = wintypes.POINT(0, 0)
        origin_x, origin_y = 0, 0
        if user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            origin_x, origin_y = int(origin.x), int(origin.y)
        payload["client_rect"] = {
            "x": origin_x,
            "y": origin_y,
            "width": int(client.right - client.left),
            "height": int(client.bottom - client.top),
        }
    return payload


def import_self_test() -> dict:
    """验证冻结产物里的 numpy / cv2 / windows_capture 能真正导入。

    2026-11-05 的 0.2.1 便携包曾因组件里混入两份 numpy（纯 Python 取自
    ``.capture-runtime`` 2.4.6、C 扩展取自 site-packages 2.2.6）导致
    ``DLL load failed while importing _multiarray_umath``；当时只有“点圈选”
    才会暴露。此自检不需要窗口，构建后必须执行。
    """
    try:
        import cv2
        import numpy
        import windows_capture  # noqa: F401  仅验证可导入

        return {
            "ok": True,
            "numpy": numpy.__version__,
            "cv2": cv2.__version__,
            "numpy_file": str(Path(numpy.__file__).name),
        }
    except Exception as error:
        return {
            "ok": False,
            "error": f"{type(error).__name__}: {error}"[:600],
            "error_detail": traceback.format_exc()[-1200:],
        }


# 失败标记 → 失败阶段。父进程只能看到 error 文本，这里把“卡在哪个阶段”说清楚。
_ERROR_STAGE_HINTS: tuple[tuple[str, str], ...] = (
    ("WGC_START_TIMEOUT", "wgc_start_requested"),
    ("WGC_CALLBACK_FAILED", "frame_callback"),
    ("WGC_FRAME_TIMEOUT", "frame_wait"),
    ("WGC_SESSION_CLOSED", "frame_wait"),
    ("WGC_SESSION_EXIT_TIMEOUT", "session_exit"),
    ("COMPONENT_START_FAILED", "component_start"),
    ("WGC_NO_FRAME", "frame_wait"),
    ("PNG_ENCODE_FAILED", "encode"),
    ("BLACK_FRAME", "black_check"),
    ("INVALID_HWND", "component_enter"),
    ("PRINTWINDOW_BLANK_FRAME", "print_window"),
    ("PRINTWINDOW_FAILED", "print_window"),
    ("GET_WINDOW_RECT_FAILED", "print_window"),
    ("INVALID_WINDOW_SIZE", "print_window"),
    ("GETDIBITS_FAILED", "print_window"),
    ("INVALID_REGION", "roi_crop"),
)


def classify_stage_failure(error: BaseException, channel: StageChannel) -> str:
    """失败阶段名：先用错误标记定位，没命中就退回“最后完成的阶段”。"""
    text = f"{type(error).__name__}: {error}"
    for token, stage in _ERROR_STAGE_HINTS:
        if token in text:
            return stage
    return channel.last_stage or "unknown"


class RoiError(RuntimeError):
    """ROI 相关的失败。

    错误文本必须以 ``INVALID_REGION`` 开头：父进程的 ``map_child_error`` 靠这个
    标记把它映射成与本地裁剪完全相同的错误码，界面上的提示才不会因为"这次是
    子进程裁的"而变样。
    """


def parse_roi(text: str) -> tuple[int, int, int, int] | None:
    """解析 ``x,y,w,h``；格式非法返回 ``None``（由调用方按 INVALID_REGION 报）。"""
    parts = [item.strip() for item in text.replace("，", ",").split(",")]
    if len(parts) != 4:
        return None
    try:
        x, y, width, height = (int(item, 10) for item in parts)
    except ValueError:
        return None
    return x, y, width, height


def crop_frame(image, roi: tuple[int, int, int, int]):
    """在**编码之前**裁出 ROI；越界/非法一律抛 ``RoiError``（不返回整帧兜底）。

    这是 ROI 协议的唯一裁剪点：WGC 回调与 PrintWindow 都只保留这块副本，随后
    的 ``encode`` 只编码一次。父进程不会再裁，所以这里必须把边界判严格。
    """
    import numpy as np

    x, y, width, height = (int(value) for value in roi)
    if image is None:
        raise RoiError("INVALID_REGION: 采集帧为空，无法裁剪消息区域")
    frame_height, frame_width = int(image.shape[0]), int(image.shape[1])
    if (
        width <= 0
        or height <= 0
        or x < 0
        or y < 0
        or x + width > frame_width
        or y + height > frame_height
    ):
        raise RoiError(
            "INVALID_REGION: 请求的消息区域超出采集帧"
            f"（消息区域 {x},{y},{width},{height} 帧 {frame_width}x{frame_height}）"
        )
    # 强制复制：整宽裁剪时切片本身就是连续的，``ascontiguousarray`` 会返回视图，
    # 从而把整窗缓冲一起留在内存里——那正是“只保存 ROI 副本”要避免的。
    cropped = np.array(image[y : y + height, x : x + width], copy=True)
    if cropped.size == 0:
        raise RoiError("INVALID_REGION: 请求的消息区域超出采集帧（裁剪结果为空）")
    return cropped


SERVE_PROTOCOL_VERSION = 2
MAX_REQUEST_CHARS = 4096
_REQUEST_KEYS = frozenset({
    "type", "protocol_version", "request_id", "region_id", "generation", "hwnd",
    "backend", "roi", "reference_frame_width", "reference_frame_height",
    "wgc_timeout_ms", "session_exit_timeout_ms",
})
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_FATAL_ERRORS = frozenset({
    "WGC_START_TIMEOUT", "WGC_FRAME_TIMEOUT", "WGC_SESSION_EXIT_TIMEOUT",
    "WGC_CALLBACK_FAILED", "COMPONENT_START_FAILED", "CAPTURE_FAILED",
})


def validate_serve_request(value: object) -> dict:
    """Reject malformed inputs without echoing their potentially private contents."""
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise ValueError("PROTOCOL_ERROR")
    if value["type"] != "capture" or type(value["protocol_version"]) is not int:
        raise ValueError("PROTOCOL_ERROR")
    if value["protocol_version"] != SERVE_PROTOCOL_VERSION:
        raise ValueError("PROTOCOL_VERSION_MISMATCH")
    for name in ("request_id", "region_id"):
        text = value[name]
        if not isinstance(text, str) or (name == "region_id" and text == ""):
            if name == "region_id" and text == "":
                continue
            raise ValueError("PROTOCOL_ERROR")
        if _REQUEST_ID.fullmatch(text) is None:
            raise ValueError("PROTOCOL_ERROR")
    for name, minimum, maximum in (
        ("generation", 0, 2**63 - 1), ("hwnd", 1, 2**64 - 1),
        ("reference_frame_width", 0, 65535), ("reference_frame_height", 0, 65535),
        ("wgc_timeout_ms", 1, 3000), ("session_exit_timeout_ms", 1, 1000),
    ):
        if type(value[name]) is not int or not minimum <= value[name] <= maximum:
            raise ValueError("PROTOCOL_ERROR")
    if value["backend"] not in ("wgc", "print-window"):
        raise ValueError("PROTOCOL_ERROR")
    roi = value["roi"]
    if roi is not None:
        if not isinstance(roi, list) or len(roi) != 4 or any(type(x) is not int for x in roi):
            raise ValueError("PROTOCOL_ERROR")
        if not (0 <= roi[0] <= 65535 and 0 <= roi[1] <= 65535
                and 1 <= roi[2] <= 65535 and 1 <= roi[3] <= 65535):
            raise ValueError("PROTOCOL_ERROR")
    return dict(value)


def _serve_error_token(error: BaseException) -> str:
    text = str(error)
    for token, _stage in _ERROR_STAGE_HINTS:
        if token in text:
            return token
    return "CAPTURE_FAILED"


def handle_serve_request(request: dict) -> tuple[dict, bool]:
    """Run one validated request; returning fatal=True forbids reusing this process."""
    channel = StageChannel()
    channel.reset(request["request_id"])
    channel.region_id = request["region_id"]
    channel.generation = request["generation"]
    channel.mark("request_received")
    identity = {key: request[key] for key in (
        "request_id", "region_id", "generation", "hwnd", "backend",
        "reference_frame_width", "reference_frame_height",
    )}
    fatal = False
    stage_failed = ""
    roi = tuple(request["roi"]) if request["roi"] is not None else None
    try:
        if request["backend"] == "wgc":
            payload = capture_wgc(
                request["hwnd"], request["wgc_timeout_ms"] / 1000,
                session_exit_timeout=request["session_exit_timeout_ms"] / 1000,
                channel=channel, roi=roi,
            )
        else:
            payload = capture_print_window(request["hwnd"], channel=channel, roi=roi)
        for key, reference in (("frame_width", request["reference_frame_width"]),
                               ("frame_height", request["reference_frame_height"])):
            if reference and payload.get(key) != reference:
                payload = {"ok": False, "error": "FRAME_SIZE_CHANGED", "roi": request["roi"]}
                stage_failed = "frame_validation"
                break
    except Exception as error:
        token = _serve_error_token(error)
        fatal = token in _FATAL_ERRORS
        stage_failed = classify_stage_failure(error, channel)
        payload = {"ok": False, "error": token, "roi": request["roi"]}
        stats = getattr(error, "stats", None)
        if isinstance(stats, dict):
            # Existing capture errors only expose numerical frame diagnostics.
            payload.update({key: stats[key] for key in ("ink_ratio", "uniform_border") if key in stats})
    channel.mark("worker_complete")
    payload.update(identity)
    payload.update(type="result", protocol_version=SERVE_PROTOCOL_VERSION, pid=os.getpid(),
                   stage_failed=stage_failed, stages=dict(channel.stages),
                   session_exit_timeout=payload.get("error") == "WGC_SESSION_EXIT_TIMEOUT",
                   worker_reusable=not fatal)
    return payload, fatal


def serve(input_stream=None, output_stream=None) -> int:
    """Preload once, then process bounded newline JSON requests serially without recording."""
    from collections import deque

    source = input_stream if input_stream is not None else sys.stdin
    target = output_stream if output_stream is not None else sys.stdout

    def emit(payload: dict) -> None:
        target.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
        target.flush()

    ready = {"type": "ready", "protocol_version": SERVE_PROTOCOL_VERSION, "pid": os.getpid()}
    try:
        dpi_request = set_dpi_awareness()
        dpi_awareness = query_dpi_awareness()
        dependencies = import_self_test()
        if not dependencies.get("ok"):
            emit({**ready, "ok": False, "error": "DEPENDENCY_INIT_FAILED"})
            return 1
        # Only the isolated persistent capture process owns this OpenCV setting.
        # OCR and the one-shot baseline retain their existing threading policy.
        set_threads = getattr(sys.modules.get("cv2"), "setNumThreads", None)
        if callable(set_threads):
            set_threads(1)
        emit({**ready, "ok": True, "dependencies": {
            key: dependencies[key] for key in ("numpy", "cv2") if key in dependencies
        }, "dpi_awareness": dpi_awareness, "dpi_request": dpi_request})
    except Exception:
        emit({**ready, "ok": False, "error": "DEPENDENCY_INIT_FAILED"})
        return 1
    recent_ids: deque[str] = deque(maxlen=128)
    while True:
        line = source.readline(MAX_REQUEST_CHARS + 1)
        if not line:
            return 0
        try:
            if len(line) > MAX_REQUEST_CHARS:
                raise ValueError("PROTOCOL_ERROR")
            request = json.loads(line)
            if (isinstance(request, dict) and set(request) == {"type", "protocol_version"}
                    and request["type"] == "shutdown"
                    and type(request["protocol_version"]) is int
                    and request["protocol_version"] == SERVE_PROTOCOL_VERSION):
                return 0
            request = validate_serve_request(request)
            if request["request_id"] in recent_ids:
                raise ValueError("PROTOCOL_ERROR")
        except (ValueError, TypeError, RecursionError) as error:
            code = "PROTOCOL_VERSION_MISMATCH" if str(error) == "PROTOCOL_VERSION_MISMATCH" else "PROTOCOL_ERROR"
            emit({"type": "error", "protocol_version": SERVE_PROTOCOL_VERSION,
                  "ok": False, "error": code})
            return 1
        recent_ids.append(request["request_id"])
        payload, fatal = handle_serve_request(request)
        emit(payload)
        # Release the encoded ROI while idle instead of retaining the previous
        # response until the next capture has produced its replacement.
        del payload
        if fatal:
            return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hwnd", type=lambda value: int(value, 0), default=0)
    parser.add_argument("--backend", choices=("wgc", "print-window"), default="wgc")
    parser.add_argument("--wgc-timeout", type=float, default=3.0)
    # 每次请求的编号：父进程用同一编号写 logs/capture-stages.log，两边靠它对齐。
    # 全部可选，不带时行为与 0.2.1 完全一致。
    parser.add_argument("--request-id", default="")
    # WGC 会话退出上限（默认 500ms，硬上限 1000ms）；父进程按同一上限下发。
    parser.add_argument(
        "--session-exit-timeout-ms", type=int, default=DEFAULT_SESSION_EXIT_TIMEOUT_MS
    )
    # 只验证依赖能否在这个冻结产物里真正导入（不需要窗口、不采集任何内容）。
    # 便携包构建后必须跑一次：numpy/cv2 的 C 扩展缺失或版本混用时，
    # 只有真正走到采集才会暴露，靠“组件文件存在”是查不出来的。
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--serve", action="store_true", help="预热后逐条处理版本化采集请求")
    # 只要消息区域（x,y,w,h）：在 PNG 编码前裁剪，整窗诊断仍在临时缓冲上算。
    # 不带 = 回传整帧，与 0.2.1 完全一致。字段缺失/多出都不是合法协议。
    parser.add_argument("--roi", default="", help="消息区域 x,y,w,h；默认回传整帧")

    # 先解析参数、再设 DPI：argparse 不读任何 Win32 坐标，而把 parse_args 放到
    # 前面才能让 component_enter / dpi_aware 这两行阶段日志也带上 request_id。
    args = parser.parse_args()
    if args.serve:
        if args.self_test:
            parser.error("--serve and --self-test are mutually exclusive")
        return serve()
    # --self-test 保持“不碰屏幕、不产生任何额外输出”的约定：不打任何阶段行。
    if not args.self_test:
        CHANNEL.reset(args.request_id)
        CHANNEL.mark("component_enter")
        CHANNEL.mark("worker_started")
    # DPI 感知必须在任何 Win32 坐标读取（GetWindowRect / ClientToScreen /
    # WGC 帧尺寸）之前设置，否则 150% 缩放下拿到的是被系统缩放过的逻辑坐标。
    # 这里只请求一次并记住结果，随 payload 一起回传给父进程诊断。
    dpi_request = set_dpi_awareness()
    dpi_awareness = query_dpi_awareness()
    if not args.self_test:
        CHANNEL.mark("dpi_aware")
    session_exit_timeout = clamp_session_exit_timeout_ms(args.session_exit_timeout_ms)

    stage_failed = ""
    # self-test 分支完全不碰 --roi：自检不采集、不编码，任何 ROI 处理都不该发生。
    requested_roi = None if args.self_test else (parse_roi(args.roi) if args.roi.strip() else None)
    if args.self_test:
        payload = import_self_test()
    elif args.roi.strip() and requested_roi is None:
        payload = {
            "ok": False,
            "backend": args.backend,
            "error": "INVALID_REGION: 消息区域参数格式非法，需要 x,y,w,h",
        }
        stage_failed = "roi_parse"
    elif args.hwnd <= 0:
        payload = {"ok": False, "backend": args.backend, "error": "RuntimeError: INVALID_HWND"}
        stage_failed = "component_enter"
    else:
        try:
            if args.backend == "wgc":
                payload = capture_wgc(
                    args.hwnd,
                    args.wgc_timeout,
                    session_exit_timeout=session_exit_timeout,
                    channel=CHANNEL,
                    roi=requested_roi,
                )
            else:
                payload = capture_print_window(
                    args.hwnd, channel=CHANNEL, roi=requested_roi
                )
            payload["backend"] = args.backend
        except Exception as error:
            stage_failed = classify_stage_failure(error, CHANNEL)
            payload = {
                "ok": False,
                "backend": args.backend,
                "error": f"{type(error).__name__}: {error}"[:600],
                # 界面只显示 error，但排查时必须看到真正抛错的位置与原始异常
                # （numpy 这类库会把真实原因藏在建议文本之后，300 字截断会把它切掉）。
                "error_detail": traceback.format_exc()[-1200:],
            }
            stats = getattr(error, "stats", None)
            if isinstance(stats, dict):
                payload.update(stats)
    payload["dpi_awareness"] = dpi_awareness
    payload["dpi_request"] = dpi_request
    # 0.2.2 追加字段：全部只增不改，老父进程忽略它们即可。
    payload["request_id"] = CHANNEL.request_id
    payload["stage_failed"] = stage_failed
    payload["session_exit_timeout"] = "WGC_SESSION_EXIT_TIMEOUT" in str(payload.get("error", ""))
    if args.self_test:
        # 自检不做真实采集，只回显参数被正确接受（打包回归用）。
        payload["session_exit_timeout_ms"] = int(round(session_exit_timeout * 1000))
    else:
        # frame_width/frame_height 必须由采集路径自己给出（整帧尺寸，不是 ROI 尺寸）。
        # 这里**不再**用 width/height 兜底：字段缺失就是要让父进程按协议失败处理，
        # 悄悄补一个值会把“组件版本不匹配”伪装成一次正常采集。
        CHANNEL.mark("total")
        CHANNEL.mark("worker_complete")
        payload["stages"] = dict(CHANNEL.stages)
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
