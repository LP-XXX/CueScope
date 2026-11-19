"""真机验收工具的共享环境层（只读、不发送微信消息、不写用户配置）。

本模块只做五件事：
1. 强制 DISPLAY1 校验：用 ``MonitorFromWindow`` + ``GetMonitorInfoW`` 取窗口所属
   显示器名，不是 ``\\\\.\\DISPLAY1`` 一律 ``sys.exit("不是 DISPLAY1，拒绝执行")``；
2. 枚举 ``Weixin.exe`` 的可见窗口，用 DWM 可见边界（``DWMWA_EXTENDED_FRAME_BOUNDS``）
   判断是否完全落在 DISPLAY1 内；
3. 以子进程调用 ``deal_watch.capture_worker`` 并取回**完整 payload**（含
   ``dpi_awareness`` / ``window_rect`` / ``client_rect``），失败也返回 payload 不抛异常；
4. 提供探针 ROI、脱敏、摘要与报告落盘的小工具；
5. 提供进程内模拟飞书接收器（只绑定 127.0.0.1 随机端口）。

硬约束（所有工具共同遵守）：不修改 ``%LOCALAPPDATA%\\DealWatch\\config.json``（只读）、
不写用户 feedback.db、不发送微信消息、不点击/滚动/输入、不移动或缩放被监控窗口、
不保存聊天截图、报告里不出现聊天原文。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VALIDATION_DIR = ROOT / "build" / "validation"
DISPLAY1 = r"\\.\DISPLAY1"
WEIXIN_EXE = "weixin.exe"
DPI_EXPECTED = "per_monitor_v2"

# 探针 ROI 的内缩比例；这是工具内部探针 ROI，不是用户校准的 ROI。
PROBE_TOP_RATIO = 0.08
PROBE_BOTTOM_RATIO = 0.32
PROBE_SIDE_RATIO = 0.02

# 生产采集参数：与生产代码保持同一组默认值，探针默认必须复刻这一组。
# 生产侧对应关系：WindowCaptureClient(timeout=5.0) /
# capture_worker.capture_wgc(timeout=3.0) / AppConfig.poll_interval_ms=30000。
PRODUCTION_CAPTURE_TIMEOUT = 5.0
PRODUCTION_WGC_TIMEOUT = 3.0
PRODUCTION_POLL_SECONDS = 30.0


def production_param_flags(
    capture_timeout: float = PRODUCTION_CAPTURE_TIMEOUT,
    wgc_timeout: float = PRODUCTION_WGC_TIMEOUT,
    poll_interval: float = PRODUCTION_POLL_SECONDS,
) -> dict[str, Any]:
    """报告里的采集参数标注：只有三项都与生产一致时才算生产参数。

    任何一项被放宽（诊断用的大超时）都会让 ``production_params`` 变 false 并标
    ``diagnostic_params: true``，避免诊断数据冒充生产验收证据。
    """
    flags: dict[str, Any] = {
        "capture_timeout_seconds": float(capture_timeout),
        "wgc_timeout_seconds": float(wgc_timeout),
        "poll_interval_seconds": float(poll_interval),
    }
    relaxed = (
        flags["capture_timeout_seconds"] != PRODUCTION_CAPTURE_TIMEOUT
        or flags["wgc_timeout_seconds"] != PRODUCTION_WGC_TIMEOUT
        or flags["poll_interval_seconds"] != PRODUCTION_POLL_SECONDS
    )
    flags["production_params"] = not relaxed
    flags["diagnostic_params"] = relaxed
    if relaxed:
        flags["relaxed_params_note"] = (
            f"诊断参数（capture {flags['capture_timeout_seconds']}s / "
            f"wgc {flags['wgc_timeout_seconds']}s / "
            f"poll {flags['poll_interval_seconds']}s）不得作为生产验收证据"
        )
    return flags


def _scan_health_transitions(
    transitions: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, str], dict[str, str]]:
    """把健康状态变化记录整理成（每区域状态集合，最后 detail，未就绪 detail）。"""
    statuses: dict[str, set[str]] = {}
    last_detail: dict[str, str] = {}
    unready: dict[str, str] = {}
    for item in transitions:
        region_id = str(item.get("region_id", ""))
        status = str(item.get("status", ""))
        detail = str(item.get("detail", ""))
        statuses.setdefault(region_id, set()).add(status)
        if detail:
            last_detail[region_id] = detail
        if status == "paused" and detail.startswith("区域未就绪") and region_id not in unready:
            unready[region_id] = detail
    return statuses, last_detail, unready


def _alias_map(regions: Any) -> list[tuple[str, str]]:
    """``[(region_id, alias)]``；与 RegionConfig 解耦，便于单测用替身。"""
    pairs: list[tuple[str, str]] = []
    for region in regions:
        region_id = str(getattr(region, "id", ""))
        pairs.append((region_id, str(getattr(region, "alias", region_id))))
    return pairs


def definitive_gate_failures(
    regions: Any,
    transitions: list[dict[str, Any]],
) -> dict[str, str]:
    """**唯一**可以立刻中止等待的门禁失败：返回 ``{别名: detail}``。

    只认“出现过 ``paused`` 且 ``detail`` 以「区域未就绪」开头”这一种。
    “从未上报”刻意不在这里：它只能是**等待超时之后**的结论，否则 t=0 时
    ``transitions`` 还是空的、必然命中，会把“首次 OCR 还没跑完”误判成“门禁拦住”。
    """
    _statuses, _last, unready = _scan_health_transitions(transitions)
    failures: dict[str, str] = {}
    for region_id, alias in _alias_map(regions):
        if region_id in unready:
            failures[alias] = unready[region_id]
    return failures


def region_gate_failures(
    regions: Any,
    transitions: list[dict[str, Any]],
    *,
    include_unreported: bool = True,
) -> dict[str, str]:
    """最终门禁判定：返回 ``{别名: 最后一次 detail}``，空字典表示通过。

    ``include_unreported=True``（默认）时把“从未进入 baselining/running”也算失败，
    用于**等待结束后的最终判定**；等待期间请用 :func:`definitive_gate_failures`，
    否则会在 t=0 就把“还没上报”当成失败。
    """
    statuses, last_detail, unready = _scan_health_transitions(transitions)
    failures: dict[str, str] = {}
    for region_id, alias in _alias_map(regions):
        if region_id in unready:
            failures[alias] = unready[region_id]
            continue
        if include_unreported and not statuses.get(region_id, set()) & {"baselining", "running"}:
            failures[alias] = last_detail.get(region_id, "从未上报健康状态")
    return failures


def bootstrap() -> None:
    """把 ``src`` 加入 ``sys.path``，便于以脚本方式运行工具。"""
    for entry in (str(SRC), str(ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def app_version() -> str:
    """读取应用包的**实际**版本号；报告的 ``version`` 字段与报告文件名一律取它。

    0.2.1 时代的探针把 ``"0.2.1"`` 写死在报告里，一旦升版，报告就会自称旧版本，
    “版本自证”反而变成假证据。工具因此不再持有任何版本字面量。
    """
    bootstrap()
    from deal_watch import __version__

    return __version__


def require_windows() -> None:
    """非 Windows 直接退出，不做任何模拟。"""
    if sys.platform != "win32":
        sys.exit("本工具只在 Windows 桌面运行；未执行任何采集。")


def enable_dpi_awareness() -> None:
    """在任何 Win32 坐标读取之前请求每监视器 V2 DPI 感知。"""
    bootstrap()
    from deal_watch.__main__ import enable_dpi_awareness as enable

    enable()


@dataclass(slots=True, frozen=True)
class WindowTarget:
    """一个候选窗口：只带身份与几何，不带标题明文以外的聊天内容。"""

    hwnd: int
    title: str
    rect: Any  # deal_watch.models.Rect
    process_id: int
    process_path: str
    class_name: str = ""

    @property
    def is_weixin(self) -> bool:
        return self.process_path.casefold().endswith(WEIXIN_EXE)


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", ctypes.c_long * 4),
        ("rcWork", ctypes.c_long * 4),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


def _user32() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("Win32 不可用")
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    api.MonitorFromWindow.restype = ctypes.c_void_p
    api.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfoEx)]
    api.GetMonitorInfoW.restype = ctypes.c_bool
    return api


def monitor_device(hwnd: int) -> str:
    """返回窗口所属显示器设备名（例如 ``\\\\.\\DISPLAY1``）。"""
    api = _user32()
    # MONITOR_DEFAULTTONEAREST = 2
    monitor = api.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)
    if not monitor:
        return ""
    info = _MonitorInfoEx()
    info.cbSize = ctypes.sizeof(info)
    if not api.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return ""
    return str(info.szDevice).upper()


def assert_display1(hwnd: int) -> None:
    """不是 DISPLAY1 的窗口一律拒绝操作。"""
    device = monitor_device(hwnd)
    if device != DISPLAY1.upper():
        sys.exit("不是 DISPLAY1，拒绝执行")


def _get_window_rect(hwnd: int) -> Any:
    bootstrap()
    from deal_watch.capture import WindowsWindows

    return WindowsWindows().get_rect(hwnd)


def visible_rect(hwnd: int) -> Any:
    """取窗口的**可见**边界：DWM 扩展边框优先，失败回退 ``GetWindowRect``。

    ``GetWindowRect`` 在现代窗口上会多出约 9px 的不可见拉伸边框，用它判断
    “是否完全落在 DISPLAY1 内”会误判；DWM 可见边界才是屏幕上真正看到的内容区。
    """
    if sys.platform != "win32":
        return _get_window_rect(hwnd)
    bootstrap()
    from ctypes import wintypes

    from deal_watch.models import Rect

    dwm = ctypes.WinDLL("dwmapi")
    raw = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS = 9
    status = dwm.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 9, ctypes.byref(raw), ctypes.sizeof(raw)
    )
    width = int(raw.right - raw.left)
    height = int(raw.bottom - raw.top)
    if status == 0 and width > 0 and height > 0:
        return Rect(int(raw.left), int(raw.top), width, height)
    return _get_window_rect(hwnd)


def window_geometry(hwnd: int) -> dict[str, dict[str, int]]:
    """取窗口矩形、客户区矩形与可见矩形（物理像素，DPI 感知后）。"""
    bootstrap()
    from ctypes import wintypes

    result: dict[str, dict[str, int]] = {}
    rect = _get_window_rect(hwnd)
    if rect is not None:
        result["window_rect"] = {
            "x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height,
        }
    visible = visible_rect(hwnd)
    if visible is not None:
        result["visible_rect"] = {
            "x": visible.x, "y": visible.y, "width": visible.width, "height": visible.height,
        }
    api = _user32()
    api.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    api.GetClientRect.restype = ctypes.c_bool
    api.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
    api.ClientToScreen.restype = ctypes.c_bool
    client = wintypes.RECT()
    if api.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(client)):
        origin = wintypes.POINT(0, 0)
        origin_x = origin_y = 0
        if api.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(origin)):
            origin_x, origin_y = int(origin.x), int(origin.y)
        result["client_rect"] = {
            "x": origin_x,
            "y": origin_y,
            "width": int(client.right - client.left),
            "height": int(client.bottom - client.top),
        }
    return result


def monitor_rect(device: str = DISPLAY1) -> dict[str, int]:
    """返回指定显示器的物理矩形；找不到时返回空字典。

    先确保本进程已请求每监视器 V2 DPI 感知，否则系统会把显示器矩形“虚拟化”成
    逻辑坐标（1600×2560 会变成 1067×1707），与窗口物理矩形不可比。
    """
    enable_dpi_awareness()
    api = _user32()
    found: dict[str, int] = {}

    class _Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long), ("top", ctypes.c_long),
            ("right", ctypes.c_long), ("bottom", ctypes.c_long),
        ]

    enum_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(_Rect), ctypes.c_long,
    )
    api.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, enum_type, ctypes.c_long,
    ]
    api.EnumDisplayMonitors.restype = ctypes.c_bool

    def visit(monitor: int, _hdc: int, _rect: Any, _param: int) -> bool:
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if not api.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            return True
        if str(info.szDevice).upper() != device.upper():
            return True
        left, top, right, bottom = info.rcMonitor
        found.update(left=left, top=top, width=right - left, height=bottom - top)
        return False

    api.EnumDisplayMonitors(None, None, enum_type(visit), 0)
    return found


def list_weixin_windows() -> list[WindowTarget]:
    """枚举所有可见的 Weixin.exe 顶层窗口（按可见矩形 y、x 排序）。

    同样先确保 DPI 感知，保证返回的矩形与 DISPLAY1 物理矩形同一坐标系。
    """
    enable_dpi_awareness()
    bootstrap()
    from deal_watch.capture import WindowsWindows

    windows = WindowsWindows()
    targets: list[WindowTarget] = []
    for item in windows.list_windows():
        if not item.process_path.casefold().endswith(WEIXIN_EXE):
            continue
        rect = visible_rect(item.hwnd) or item.rect
        targets.append(
            WindowTarget(
                hwnd=item.hwnd,
                title=item.title,
                rect=rect,
                process_id=item.process_id,
                process_path=item.process_path,
                class_name=item.class_name,
            )
        )
    targets.sort(key=lambda target: (target.rect.y, target.rect.x))
    return targets


def fully_inside(rect: Any, bounds: dict[str, int]) -> bool:
    """矩形是否完全落在显示器物理矩形内。"""
    if not bounds:
        return False
    return (
        rect.x >= bounds["left"]
        and rect.y >= bounds["top"]
        and rect.x + rect.width <= bounds["left"] + bounds["width"]
        and rect.y + rect.height <= bounds["top"] + bounds["height"]
    )


def display1_weixin_windows(limit: int = 4) -> list[WindowTarget]:
    """只返回完全落在 DISPLAY1 内的 Weixin.exe 窗口（最多 ``limit`` 个）。"""
    bounds = monitor_rect(DISPLAY1)
    return [
        target for target in list_weixin_windows() if fully_inside(target.rect, bounds)
    ][:limit]


def select_target(hwnd: int | None, title_contains: str | None) -> WindowTarget:
    """按显式 HWND 或标题子串选择窗口；选不到就带提示安全退出。"""
    if hwnd:
        rect = visible_rect(hwnd)
        if rect is None:
            sys.exit("指定的 HWND 不存在或不可见，已安全退出")
        bootstrap()
        from deal_watch.capture import WindowsWindows

        info = WindowsWindows().window_from_hwnd(hwnd)
        if info is None:
            sys.exit("指定的 HWND 不存在或不可见，已安全退出")
        return WindowTarget(
            hwnd=hwnd,
            title=info.title,
            rect=rect,
            process_id=info.process_id,
            process_path=info.process_path,
            class_name=info.class_name,
        )
    if not title_contains:
        sys.exit("请用 --hwnd 或 --title-contains 指定一个微信窗口")
    bounds = monitor_rect(DISPLAY1)
    matches = [
        target
        for target in list_weixin_windows()
        if title_contains in target.title and fully_inside(target.rect, bounds)
    ]
    if not matches:
        sys.exit(f"未在 DISPLAY1 找到标题包含“{title_contains}”的微信窗口，已安全退出")
    if len(matches) > 1:
        hwnds = ", ".join(f"0x{target.hwnd:x}" for target in matches)
        sys.exit(f"标题包含“{title_contains}”的窗口不唯一（{hwnds}），请改用 --hwnd")
    return matches[0]


def require_weixin(target: WindowTarget) -> None:
    """目标必须是 Weixin.exe，避免误采其他程序。"""
    if not target.is_weixin:
        sys.exit("目标进程不是 Weixin.exe，拒绝执行")


def _pythonpath_with_repo(parts_source: str) -> str:
    """把 ``src`` 与 ``ROOT`` 放到 PYTHONPATH 最前面（去重、保留原有条目）。

    ``src``/``ROOT`` 排在最前，保证本仓库的 ``deal_watch`` 优先于环境里可能存在的
    同名包；原有条目保持相对顺序排在后面。
    """
    own = [str(SRC), str(ROOT)]
    existing = [item for item in parts_source.split(os.pathsep) if item and item not in own]
    return os.pathsep.join(own + existing)


def ensure_subprocess_pythonpath() -> str:
    """让子进程也能 ``-m deal_watch.capture_worker``；返回最终的 PYTHONPATH。

    ``WindowCaptureClient.capture()`` 用 ``subprocess.run`` 且**不传 env**，子进程只
    继承 ``os.environ``——工具里的 ``sys.path.insert`` 对子进程完全无效。所以凡是
    直接使用 ``WindowCaptureClient`` / ``WindowFrameCapturer`` 的工具，都必须在启动
    处调用本函数。幂等：重复调用不会产生重复条目。
    """
    os.environ["PYTHONPATH"] = _pythonpath_with_repo(os.environ.get("PYTHONPATH", ""))
    return os.environ["PYTHONPATH"]


def child_environment() -> dict[str, str]:
    """返回给采集子进程用的环境副本（不修改 ``os.environ``）。"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = _pythonpath_with_repo(environment.get("PYTHONPATH", ""))
    return environment


def worker_payload(
    hwnd: int, backend: str, timeout: float = PRODUCTION_CAPTURE_TIMEOUT
) -> dict[str, Any]:
    """以子进程运行采集 worker 并取回完整 payload（失败不抛异常）。

    命令行与 ``WindowCaptureClient._command()`` 一致；额外保留完整 payload，
    以便读取 ``dpi_awareness`` / ``dpi_request`` / ``window_rect`` / ``client_rect``。
    """
    environment = child_environment()
    command = [
        sys.executable,
        "-m",
        "deal_watch.capture_worker",
        "--hwnd",
        str(hwnd),
        "--backend",
        backend,
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=str(ROOT),
            env=environment,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "backend": backend, "error": "CAPTURE_TIMEOUT: 采集组件超时"}
    except OSError as error:
        return {
            "ok": False,
            "backend": backend,
            "error": f"CAPTURE_SPAWN_FAILED: {type(error).__name__}",
        }
    stdout = (result.stdout or "").strip()
    if not stdout:
        return {
            "ok": False,
            "backend": backend,
            "error": "CAPTURE_NO_OUTPUT: 采集组件无输出",
            "stderr_tail": (result.stderr or "").strip()[-200:],
        }
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        return {
            "ok": False,
            "backend": backend,
            "error": "CAPTURE_INVALID_PAYLOAD: 采集组件返回无效数据",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "backend": backend,
            "error": "CAPTURE_INVALID_PAYLOAD: 采集组件返回非对象",
        }
    return payload


def decode_frame(payload: dict[str, Any]) -> np.ndarray | None:
    """把 payload 里的 PNG 解成 BGR ndarray；失败返回 None。"""
    encoded = payload.get("image")
    if not payload.get("ok") or not encoded:
        return None
    try:
        from PIL import Image

        image = Image.open(BytesIO(base64.b64decode(encoded))).convert("RGB")
        return np.asarray(image)[:, :, ::-1].copy()
    except Exception:
        return None


def is_blank_frame(image: np.ndarray) -> bool:
    """复用采集 worker 的空白帧判据（PrintWindow 会返回纯色空白帧）。

    worker 的判据会先自动检出 8–11px 均匀边框再看内部墨水占比，比“内缩 3px 比
    中位色”更能抓住带边框的空白帧；这里不复制该逻辑，避免两处判据漂移。

    显式 ``import numpy`` 之后再导入 worker，避免 worker 把 ``.capture-runtime``
    插到 ``sys.path`` 首位时把该目录的 numpy 顶到前面（本模块已在顶部导入 numpy）。
    """
    bootstrap()
    from deal_watch.capture_worker import is_blank

    return bool(is_blank(image))


def probe_region(width: int, height: int) -> Any:
    """探针 ROI：顶部内缩 8% 高、底部内缩 32% 高、左右各内缩 2% 宽。

    这是探针 ROI，不是用户校准的 ROI，不写入配置、不进用户 config.json；
    仅用于真机验收时避开标题栏与输入框。
    """
    bootstrap()
    from deal_watch.models import Rect

    left = int(round(width * PROBE_SIDE_RATIO))
    right = width - left
    top = int(round(height * PROBE_TOP_RATIO))
    bottom = height - int(round(height * PROBE_BOTTOM_RATIO))
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def crop(image: np.ndarray, rect: Any) -> np.ndarray:
    """按 ROI 裁剪；越界会先夹到帧内。"""
    height, width = image.shape[:2]
    left = max(0, min(width - 1, rect.x))
    top = max(0, min(height - 1, rect.y))
    right = max(left + 1, min(width, rect.x + rect.width))
    bottom = max(top + 1, min(height, rect.y + rect.height))
    return image[top:bottom, left:right].copy()


def text_digest(text: str) -> str:
    """文本摘要；报告只用摘要，不写聊天原文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prefix_digest(text: str, size: int = 40) -> str:
    """归一化后前 ``size`` 个字符的摘要，用于“是不是新行”的判定。"""
    bootstrap()
    from deal_watch.normalize import char_rule_normalize

    return text_digest(char_rule_normalize(text)[:size])


def char_set(text: str) -> set[str]:
    """参与比较的字符集合（与字符规则同一套归一化）。"""
    bootstrap()
    from deal_watch.normalize import char_rule_normalize

    return set(char_rule_normalize(text))


def jaccard(first: set[str], second: set[str]) -> float:
    """两个字符集合的 Jaccard 相似度；都为空时记为 1.0。"""
    if not first and not second:
        return 1.0
    union = first | second
    return len(first & second) / len(union) if union else 1.0


def redact(text: str) -> str:
    """控制台脱敏：中文与全角标点替换为 ●，ASCII 保留便于核对数字。"""
    redacted: list[str] = []
    for character in text:
        code = ord(character)
        if character == "\n":
            redacted.append("\\n")
        elif (
            0x2E80 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0xFF00 <= code <= 0xFFEF
            or 0x3000 <= code <= 0x303F
        ):
            redacted.append("●")
        else:
            redacted.append(character)
    return "".join(redacted)


def read_keywords() -> list[str]:
    """只读副本读取用户关键词；任何异常都按“无关键词”处理，绝不写回配置。"""
    bootstrap()
    try:
        from deal_watch.config import ConfigStore

        return list(ConfigStore().load().keywords)
    except Exception:
        return []


def model_config() -> Any:
    """返回默认 OCR 模型配置（固定 SHA-256 清单，不读取用户配置）。"""
    bootstrap()
    from deal_watch.config import ModelConfig

    return ModelConfig()


def uniform_border(image: np.ndarray, tolerance: int = 2) -> dict[str, int]:
    """从四边向内扫描“同色条带”宽度（像素），用于客户区边界证据。

    以最外一行/列的中位色为参照，逐行/逐列比较；一旦该行/列出现超过容差的
    像素就停止。PrintWindow 帧会带上标题栏与窗口边框，WGC 帧应与客户区等大。
    """
    height, width = image.shape[:2]
    if height < 4 or width < 4:
        return {"left": 0, "right": 0, "top": 0, "bottom": 0}
    pixels = image.astype(np.int16)

    def matches(values: np.ndarray, reference: np.ndarray) -> bool:
        return bool(np.all(np.abs(values - reference) <= tolerance))

    def column(index: int) -> np.ndarray:
        return pixels[:, index, :]

    def row(index: int) -> np.ndarray:
        return pixels[index, :, :]

    counts = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    top_reference = np.median(row(0), axis=0)
    for index in range(height):
        if not matches(row(index), top_reference):
            break
        counts["top"] += 1
    bottom_reference = np.median(row(height - 1), axis=0)
    for index in range(height - 1, -1, -1):
        if not matches(row(index), bottom_reference):
            break
        counts["bottom"] += 1
    left_reference = np.median(column(0), axis=0)
    for index in range(width):
        if not matches(column(index), left_reference):
            break
        counts["left"] += 1
    right_reference = np.median(column(width - 1), axis=0)
    for index in range(width - 1, -1, -1):
        if not matches(column(index), right_reference):
            break
        counts["right"] += 1
    return counts


def start_mock_feishu() -> tuple[Any, int]:
    """进程内启动模拟飞书接收器（127.0.0.1 随机端口），复用 tools/mock_feishu.py。"""
    import threading
    from http.server import ThreadingHTTPServer

    tools_dir = str(Path(__file__).resolve().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from mock_feishu import Handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="mock-feishu", daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def write_report(path: Path, report: dict[str, Any]) -> Path:
    """把报告写入 ``build/validation`` 下的文件（不含聊天原文与凭据）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {path}")
    return path


def timestamp() -> float:
    return time.time()
