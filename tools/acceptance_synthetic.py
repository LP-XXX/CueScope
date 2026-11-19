"""四个自建合成窗口的真实采集/OCR/本机通知闭环验收。

只采集本工具创建窗口的内部区域；不读取微信、用户配置或凭据。
采集走与生产完全相同的路径（``WindowFrameCapturer`` + WGC 子进程 + 帧内坐标 ROI），
不再使用 0.2.1 已退役的 MSS 桌面绝对坐标采集。
结果只代表合成场景，不代替真实微信、人工标注集或手机推送验收。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT / "src"), str(ROOT), str(ROOT / "tools")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# WindowCaptureClient 用显式 Popen 起采集子进程（stdout/stderr 都是管道，0.2.2 起
# stderr 还是阶段通道），不传 env，子进程继承 os.environ；
# 开发态下必须把 src 与 ROOT 追加进 PYTHONPATH（sys.path.insert 对子进程无效），
# 否则 `python -m deal_watch.capture_worker` 找不到包，四个区域会全部 capture_failed。
# 实现在 tools/real_env.py 的 ensure_subprocess_pythonpath()（幂等、src/ROOT 排最前）。
import real_env  # noqa: E402  （先补齐 sys.path 才能导入共享层）

real_env.ensure_subprocess_pythonpath()

READINESS_ABORT = "区域未通过就绪门禁"
STARTUP_ABORT = "区域未在限定时间内进入采集"


class ReadinessGateError(RuntimeError):
    """区域未通过就绪门禁：禁止产出看起来像通过的报告。"""


class CaptureStartupError(RuntimeError):
    """采集/OCR 在限定时间内没跑起来：不是门禁拦截，按启动失败处理（退出码 1）。"""


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "build" / "validation" / "synthetic-four-window.json",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="每阶段最长等待秒数")
    parser.add_argument("--soak-seconds", type=float, default=0.0, help="额外合成稳定运行秒数")
    parser.add_argument(
        "--screen-index", type=int, default=None,
        help="仅允许 Win32 DISPLAY1 / CSW1632 / MNG007DA5-1 内屏，不按primary推断",
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("本工具需要已解锁的 Windows 可见桌面")

    from deal_watch.__main__ import enable_dpi_awareness

    enable_dpi_awareness()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    from deal_watch.capture import WindowsWindows
    from deal_watch.config import AppConfig, ModelConfig
    from deal_watch.models import Rect, RegionConfig
    from deal_watch.normalize import meaningful_normalize
    from deal_watch.notify import FeishuClient, NotificationDispatcher
    from deal_watch.ocr import LocalOcr
    from deal_watch.service import MonitorService
    from deal_watch.storage import Storage
    from deal_watch.window_capture import WindowCaptureClient, WindowFrameCapturer

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    received: list[dict] = []
    emitted: dict[str, dict] = {}
    health: dict[str, dict] = {}
    transitions: list[dict] = []
    occlusion_layers: list[dict] = []
    resource_samples: list[dict] = []
    recognition_markers: list[dict] = []
    lock = threading.Lock()
    windows = []
    own_handles: set[int] = set()
    service = None
    dispatcher = None
    storage = None
    server = None
    started = time.time()
    process_started = time.process_time()
    report: dict = {
        "kind": "synthetic_four_window_native_capture_real_ocr_loopback_http",
        "scope": "仅自建合成窗口；无真实微信、真实飞书和手机送达验证",
        "started_at": started,
        # 区域全部由本工具创建，不是用户配置；ROI 由自建帧尺寸生成，不是用户确认。
        "capture_mode": "synthetic_own_windows",
        "roi_source": "synthetic_frame_roi",
        "user_confirmed_roi": False,
        "acceptance_eligible": False,
        "region_count": 0,
        "checks": [],
        "success": False,
    }

    from deal_watch.diagnostics import source_fingerprint

    report["source_fingerprint"] = source_fingerprint(
        [
            ROOT / "tools/acceptance_synthetic.py",
            ROOT / "src/deal_watch/ocr.py",
            ROOT / "src/deal_watch/ocr_cache.py",
            ROOT / "src/deal_watch/notify.py",
        ],
        root=ROOT,
    )

    def permitted_screen():
        """Windows的屏幕1不等同于primary；校验硬件映射后只用指定内屏。"""
        from ctypes import wintypes

        class DisplayDevice(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128),
            ]

        user32 = ctypes.WinDLL("user32")
        user32.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DisplayDevice), wintypes.DWORD,
        ]
        user32.EnumDisplayDevicesW.restype = wintypes.BOOL
        expected_display = r"\\.\DISPLAY1"
        mapping = None
        for index in range(64):
            adapter = DisplayDevice()
            adapter.cb = ctypes.sizeof(adapter)
            if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(adapter), 0):
                break
            if adapter.DeviceName.upper() != expected_display.upper():
                continue
            for monitor_index in range(16):
                monitor = DisplayDevice()
                monitor.cb = ctypes.sizeof(monitor)
                if not user32.EnumDisplayDevicesW(
                    adapter.DeviceName, monitor_index, ctypes.byref(monitor), 0
                ):
                    break
                hardware_id = monitor.DeviceID.upper()
                if "SAC2763" in hardware_id:
                    raise RuntimeError("DISPLAY1映射到禁止的G72硬件SAC2763；拒绝测试")
                if "CSW1632" in hardware_id:
                    mapping = {
                        "win32_display": adapter.DeviceName,
                        "hardware_id": "CSW1632",
                        "manufacturer_code": "CSW",
                        "win32_primary": bool(adapter.StateFlags & 4),
                    }
        if not mapping:
            raise RuntimeError("未验证DISPLAY1到CSW1632内屏映射；未创建窗口或采集屏幕")
        screens = app.screens()
        candidates = [screen for screen in screens if screen.name() == "MNG007DA5-1"]
        if args.screen_index is not None:
            candidates = [screens[args.screen_index]]
        if len(candidates) != 1 or candidates[0].name() != "MNG007DA5-1":
            raise RuntimeError("仅允许MNG007DA5-1内屏；禁止G72且不自动切换其他显示器")
        return candidates[0], mapping

    class AuditedWindows(WindowsWindows):
        """只记录遮挡层的类别和几何；不读取其标题、正文或像素。"""

        def occluded(self, window, region) -> bool:
            blocked = super().occluded(window, region)
            if not blocked:
                return False
            from ctypes import wintypes

            def visit(hwnd, _argument):
                if int(hwnd) == window.hwnd:
                    return False
                if not self._user32.IsWindowVisible(hwnd) or self._user32.IsIconic(hwnd):
                    return True
                extended_style = self._get_window_long(hwnd, -20)
                if extended_style & 0x20:
                    return True
                cloaked = wintypes.DWORD()
                if self._dwmapi.DwmGetWindowAttribute(
                    hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
                ) == 0 and cloaked.value:
                    return True
                rect = self.get_rect(hwnd)
                if not rect or not (
                    rect.x < region.x + region.width and region.x < rect.x + rect.width
                    and rect.y < region.y + region.height and region.y < rect.y + rect.height
                ):
                    return True
                class_name = ctypes.create_unicode_buffer(256)
                self._user32.GetClassNameW(hwnd, class_name, 256)
                layer = {
                    "region_id": next(
                        item.id for item in regions if item.window_title == window.title
                    ),
                    "class": class_name.value,
                    "own_synthetic_window": int(hwnd) in own_handles,
                    "rect": [rect.x, rect.y, rect.width, rect.height],
                    "extended_style": extended_style,
                }
                with lock:
                    if not any(all(old.get(key) == value for key, value in layer.items()) for old in occlusion_layers):
                        occlusion_layers.append({**layer, "first_seen_at": time.time()})
                return False

            self._user32.EnumWindows(self._enum_type(visit), 0)
            return True

    def sample_resources(previous: tuple[float, float] | None = None) -> tuple[float, float]:
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working_set", "working_set", "peak_paged", "paged", "peak_nonpaged",
                    "nonpaged", "pagefile", "peak_pagefile", "private_usage",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        now, cpu = time.monotonic(), time.process_time()
        sample = {"at": time.time()}
        if psapi.GetProcessMemoryInfo(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
            sample["working_set_mb"] = round(counters.working_set / 1024**2, 2)
            sample["private_commit_mb"] = round(counters.private_usage / 1024**2, 2)
        if previous and now > previous[0]:
            cores = (cpu - previous[1]) / (now - previous[0])
            sample["cpu_one_core_percent"] = round(cores * 100, 2)
            sample["cpu_all_logical_percent"] = round(cores / (os.cpu_count() or 1) * 100, 2)
        if service:
            sample["queue_depth"] = len(service._pending)
            sample["dropped_frames"] = sum(item["dropped_frames"] for item in health.values())
        resource_samples.append(sample)
        return now, cpu

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/ok":
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            rows = body["content"]["post"]["zh_cn"]["content"]
            text = "".join(part.get("text", "") for row in rows for part in row)
            event_id = text.rsplit("事件编号：", 1)[-1].strip()
            with lock:
                received.append({"event_id": event_id, "accepted_at": time.time()})
            data = b'{"code":0,"msg":"success"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args) -> None:
            pass

    class Scene(QWidget):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index
            self.lines = ["普通聊天", "历史 手机 999 元"]
            self.setWindowTitle(f"DealWatch SYNTHETIC ONLY {index + 1}")
            self.setWindowFlags(
                Qt.WindowType.Window
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
            )

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#f8f8f8"))
            painter.setFont(QFont("Microsoft YaHei UI", 11))
            painter.setPen(QColor("#666666"))
            painter.drawText(18, 24, f"SYNTHETIC TEST {self.index + 1} — 非微信")
            painter.setFont(QFont("Microsoft YaHei UI", 20))
            for index, line in enumerate(self.lines):
                y = 45 + index * 76
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#e9e9e9"))
                painter.drawRoundedRect(14, y, self.width() - 28, 54, 7, 7)
                painter.setPen(QColor("#111111"))
                painter.drawText(26, y + 36, line)

    class GatedOcr:
        """仍执行真实 OCR，仅在结果返回时加闸以确定性验证过期结果废弃。"""

        def __init__(self) -> None:
            self.engine = LocalOcr(ModelConfig(), ROOT)
            self.target_region = ""
            self.entered = threading.Event()
            self.release = threading.Event()
            # 每个区域最近一次完成识别的帧序号：报告的"处理完成序号"。
            self.sequences: dict[str, int] = {}

        def recognize(self, frame):
            observation = self.engine.recognize(frame)
            with lock:
                previous = self.sequences.get(frame.region_id, 0)
                if frame.sequence > previous:
                    self.sequences[frame.region_id] = frame.sequence
            markers = []
            for block in observation.blocks:
                markers.append({
                    "has_keyword": "手机" in block.text,
                    "fixture_tokens": [token for token in (
                        "999", "499", "399", "299", "199", "A01", "A02", "A03", "A04",
                        "COMMON", "MOVED", "普通", "历史",
                    ) if token in block.text],
                    "text_length": len(block.text),
                    "whitespace_count": sum(character.isspace() for character in block.text),
                    "meaningful_sha256": hashlib.sha256(
                        meaningful_normalize(block.text).encode("utf-8")
                    ).hexdigest(),
                    "box": [block.box.x, block.box.y, block.box.width, block.box.height],
                })
            with lock:
                if len(recognition_markers) < 200:
                    recognition_markers.append({
                        "region_id": frame.region_id, "frame_sequence": frame.sequence,
                        "at": time.time(), "blocks": markers,
                        "uncovered_boxes": len(observation.uncovered_boxes),
                    })
            if frame.region_id == self.target_region and any(
                "199" in block.text for block in observation.blocks
            ):
                self.target_region = ""
                self.entered.set()
                if not self.release.wait(30):
                    raise TimeoutError("合成验收闸门超时")
            return observation

    def pump(duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.015)

    def wait_until(predicate, label: str, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (args.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(0.025)
        states = {key: value["status"] for key, value in health.items()}
        raise AssertionError(f"等待超时：{label}；区域状态={states}；接收数={len(received)}")

    def check(name: str, condition: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "passed": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"{name}: {detail}")
        print(f"PASS {name}", flush=True)

    def on_health(item) -> None:
        value = {
            "status": item.status.value, "detail": item.detail,
            "last_ocr_ms": item.last_ocr_ms, "dropped_frames": item.dropped_frames,
            "queue_depth": item.queue_depth, "at": time.time(),
        }
        with lock:
            previous = health.get(item.region_id)
            health[item.region_id] = value
            if not previous or previous["status"] != value["status"]:
                transitions.append({"region_id": item.region_id, **value})

    def on_events(events) -> None:
        with lock:
            for event in events:
                emitted[event.id] = {
                    "region_id": event.region_id, "observed_at": event.observed_at,
                    "text": event.text, "enqueued_at": time.time(),
                }

    with tempfile.TemporaryDirectory(prefix="deal-watch-synthetic-") as temporary:
        try:
            screens = app.screens()
            selected_screen, screen_mapping = permitted_screen()
            screen = selected_screen.availableGeometry()
            report["screen"] = {
                **screen_mapping,
                "index": screens.index(selected_screen),
                "name": selected_screen.name(),
                "device_pixel_ratio": selected_screen.devicePixelRatio(),
                "available_geometry_logical": [screen.x(), screen.y(), screen.width(), screen.height()],
            }
            width = min(460, (screen.width() - 80) // 2)
            height = min(300, (screen.height() - 90) // 2)
            if width < 380 or height < 290:
                raise RuntimeError("可见工作区过小，无法安全摆放四个无重叠合成窗口")
            for index in range(4):
                scene = Scene(index)
                scene.setGeometry(
                    screen.x() + screen.width() - 2 * width - 40 + (index % 2) * (width + 20),
                    screen.y() + 25 + (index // 2) * (height + 20), width, height,
                )
                scene.show()
                windows.append(scene)
            pump(0.6)
            regions = []
            windows_api = WindowsWindows()
            # 生产路径：先用 WGC 采一帧拿帧尺寸，ROI 用帧内坐标（与用户圈选一致）。
            capture_timeout = real_env.PRODUCTION_CAPTURE_TIMEOUT
            capture_client = WindowCaptureClient(timeout=capture_timeout)
            for index, scene in enumerate(windows):
                hwnd = int(scene.winId())
                own_handles.add(hwnd)
                info = windows_api.window_from_hwnd(hwnd)
                if info is None:
                    raise RuntimeError("无法取得自建合成窗口身份")
                try:
                    frame = capture_client.capture(hwnd, "wgc")
                except Exception as error:
                    # 采不到帧就明确失败，绝不退回已退役的 MSS 桌面坐标路径。
                    raise RuntimeError(f"合成窗口 WGC 采集失败：{error}") from error
                margin = 8
                regions.append(RegionConfig(
                    id=f"synthetic-{index + 1}", alias=f"合成群{index + 1}",
                    # window_title/class 用操作系统回报的实值：采集期 validate() 就是拿
                    # window_from_hwnd() 的这两个字段比对，用 Qt 侧值一旦被系统规范化就会
                    # 被判成“身份已变化”。
                    window_title=info.title, window_class=info.class_name,
                    window_rect=info.rect, window_hwnd=hwnd,
                    process_id=info.process_id, process_path=info.process_path,
                    capture_backend="wgc", coordinate_space="frame",
                    reference_frame_width=frame.width,
                    reference_frame_height=frame.height,
                    needs_calibration=False, enabled=True,
                    rect=Rect(
                        margin, margin,
                        max(40, frame.width - 2 * margin),
                        max(40, frame.height - 2 * margin),
                    ),
                    # 仅内存标记：本工具已用 WGC 实采帧确认 ROI 可用，等价于“已预览确认”；
                    # 绝不写入用户配置或落盘。
                    previewed=True,
                ))
            report["region_sizes_physical_pixels"] = [
                [region.rect.width, region.rect.height] for region in regions
            ]
            report["region_count"] = len(regions)
            report["frame_sizes"] = [
                [region.reference_frame_width, region.reference_frame_height]
                for region in regions
            ]
            storage = Storage(Path(temporary) / "synthetic.db")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/ok"
            dispatcher = NotificationDispatcher(storage, lambda: FeishuClient(endpoint, timeout=3))
            dispatcher.start()
            ocr = GatedOcr()
            config = AppConfig(
                regions=regions, keywords=["手机"], save_hit_images=False,
                poll_interval_ms=500, force_ocr_seconds=10,
            )
            service = MonitorService(
                config, storage, ocr, data_dir=Path(temporary),
                capturer=WindowFrameCapturer(
                    client=WindowCaptureClient(timeout=capture_timeout),
                    windows=AuditedWindows(),
                ),
                on_events=on_events, on_health=on_health,
            )
            gate_started = time.monotonic()
            service.start()

            # 门禁分两段：等待期间只有“确定性失败”才能提前结束（paused + 区域未就绪）；
            # “从未上报”必须等超时后由最终判定给出，否则 t=0 时就会误杀。
            def gate_wait_done() -> bool:
                if len(health) >= len(regions):
                    return True
                return bool(real_env.definitive_gate_failures(regions, transitions))

            try:
                wait_until(gate_wait_done, "第一批健康状态")
            except AssertionError:
                pass  # 超时不再等待，交给下面的最终判定说明原因

            report["gate_wait_seconds"] = round(time.monotonic() - gate_started, 2)
            definitive = real_env.definitive_gate_failures(regions, transitions)
            if definitive:
                report["region_gate_failures"] = definitive
                raise ReadinessGateError(
                    "；".join(f"{alias}：{detail}" for alias, detail in definitive.items())
                )
            not_started = real_env.region_gate_failures(regions, transitions)
            if not_started:
                report["region_gate_failures"] = not_started
                raise CaptureStartupError(
                    "；".join(f"{alias}：{detail}" for alias, detail in not_started.items())
                )
            report["readiness_gate_passed"] = True
            wait_until(
                lambda: len(health) == 4 and all(
                    value["status"] == "running" for value in health.values()
                ), "四窗建立基线",
            )
            pump(2)
            check("baseline_does_not_notify", not received and not storage.list_events())

            for index, scene in enumerate(windows):
                scene.lines.append(f"手机 499 元 A0{index + 1}")
                scene.repaint()
            pump(0.1)
            wait_until(lambda: len(received) >= 4, "四窗各有新文字命中")
            pump(3)
            check("four_window_new_messages", len(received) == 4, f"HTTP接受数={len(received)}")

            for scene in windows:
                scene.lines[-1] = "手机 399 元 COMMON"
                scene.repaint()
            pump(0.1)
            wait_until(
                lambda: len(received) >= 5 and len(storage.list_events()) >= 8,
                "四群相同线报全部完成识别与去重",
            )
            pump(3)
            duplicate_count = sum(item["state"] == "duplicate" for item in storage.list_events())
            check(
                "cross_group_same_content_once", len(received) == 5 and duplicate_count == 3,
                f"HTTP接受数={len(received)}；重复观察数={duplicate_count}",
            )

            windows[0].lines[-1] = "手机 299 元 COMMON"
            windows[0].repaint()
            pump(0.1)
            wait_until(lambda: len(received) >= 6, "价格变化提醒")
            pump(2)
            check("price_change_not_suppressed", len(received) == 6)

            ocr.target_region = regions[0].id
            windows[0].lines[-1] = "手机 199 元 MOVED"
            windows[0].repaint()
            wait_until(ocr.entered.is_set, "真实OCR已完成但结果仍在途中")
            original_geometry = windows[0].geometry()
            # 生产路径按 WGC 帧内坐标验收：移动窗口不影响 ROI，只有帧尺寸变化才要求
            # 重新校准。这里用“改高度”制造 WINDOW_RESIZED，验证在途结果被作废。
            windows[0].resize(original_geometry.width(), original_geometry.height() + 12)
            pump(0.2)
            wait_until(
                lambda: health.get(regions[0].id, {}).get("status") == "error",
                "帧尺寸变化后要求重新校准",
            )
            ocr.release.set()
            pump(3)
            check("resized_frame_drops_inflight_result", len(received) == 6)

            windows[0].setGeometry(original_geometry)
            pump(0.2)
            wait_until(
                lambda: health.get(regions[0].id, {}).get("status") == "running",
                "帧尺寸恢复后重新基线",
            )
            pump(3)
            check("recovery_rebaselines_without_history", len(received) == 6)

            # 移动窗口不改变帧内 ROI：必须保持 running 且不产生新提醒。
            moved_geometry = windows[0].geometry()
            windows[0].move(moved_geometry.x() + 8, moved_geometry.y())
            pump(3)
            check(
                "window_move_keeps_frame_roi",
                health.get(regions[0].id, {}).get("status") == "running"
                and len(received) == 6,
                str(health.get(regions[0].id, {})),
            )

            blocker = QWidget()
            blocker.setWindowTitle("DealWatch SYNTHETIC OCCLUDER")
            blocker.setWindowFlags(
                Qt.WindowType.Window
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
            )
            blocker.setGeometry(windows[0].geometry())
            blocker.show()
            own_handles.add(int(blocker.winId()))
            pump(2)
            # WGC 采集不依赖窗口可见性：覆盖期间必须继续 running，也不得多发提醒。
            check(
                "occlusion_does_not_pause_production_capture",
                health.get(regions[0].id, {}).get("status") == "running",
                str(health.get(regions[0].id, {})),
            )
            check("occlusion_adds_no_notification", len(received) == 6)
            blocker.close()
            for scene in windows:
                scene.raise_()
            pump(2)
            wait_until(
                lambda: len(health) == 4 and all(
                    value["status"] == "running" for value in health.values()
                ), "所有四窗均恢复running",
            )
            check("all_four_regions_recovered", all(
                value["status"] == "running" for value in health.values()
            ))
            check("workers_alive", bool(
                service._capture_thread.is_alive() and service._ocr_thread.is_alive()
                and dispatcher._thread.is_alive()
            ))
            check("no_sender_error", not dispatcher.last_error, dispatcher.last_error)
            previous_resource = sample_resources()
            soak_started = time.monotonic()
            next_progress = 60.0
            next_sample = 10.0
            while time.monotonic() - soak_started < args.soak_seconds:
                pump(0.2)
                if len(received) != 6:
                    raise AssertionError(
                        f"静止稳定阶段通知数异常：预期 6，实际 {len(received)}"
                    )
                elapsed = time.monotonic() - soak_started
                if elapsed >= next_sample:
                    previous_resource = sample_resources(previous_resource)
                    next_sample += 10
                    if not (
                        service._capture_thread.is_alive() and service._ocr_thread.is_alive()
                        and dispatcher._thread.is_alive()
                    ):
                        raise AssertionError("合成稳定运行阶段worker退出")
                if elapsed >= next_progress:
                    print(f"SOAK {elapsed:.0f}s: received={len(received)} health=" + str({
                        key: value["status"] for key, value in health.items()
                    }) + f" resource={resource_samples[-1]}", flush=True)
                    next_progress += 60
            if args.soak_seconds:
                sample_resources(previous_resource)
                check("soak_no_duplicate_notifications", len(received) == 6)
                check("soak_final_all_running", all(
                    value["status"] == "running" for value in health.values()
                ))
            report["soak_seconds"] = round(time.monotonic() - soak_started, 3)
            if args.soak_seconds >= config.dedupe_seconds:
                # 静止画面跨TTL不得重发；实际离开画面并经三次复核后，
                # 同一群重新出现的相同线报必须允许提醒。
                repeated = windows[1].lines.pop()
                windows[1].repaint()

                def old_content_confirmed_absent():
                    with service._condition:
                        tracks = service.tracker._regions.get(regions[1].id, {})
                        return not any("COMMON" in item.message.text for item in tracks.values())

                wait_until(old_content_confirmed_absent, "旧消息像素改变并连续复核离开画面")
                windows[1].lines.append(repeated)
                windows[1].repaint()
                wait_until(lambda: len(received) >= 7, "冷却后真正重新出现的同文线报提醒")
                pump(2)
                check("same_group_true_reappearance_after_cooldown", len(received) == 7)
            report["success"] = True
        except KeyboardInterrupt:
            report["status"] = "user_interrupted"
            report["error"] = "用户中断测试；未完成的验收不得记为通过"
            print(report["error"], flush=True)
        except ReadinessGateError as error:
            report["aborted"] = READINESS_ABORT
            report["readiness_gate_passed"] = False
            report["error"] = f"{type(error).__name__}: {error}"[:500]
            print(report["error"], flush=True)
        except CaptureStartupError as error:
            report["aborted"] = STARTUP_ABORT
            report["readiness_gate_passed"] = False
            last_states = {
                region.alias: health.get(region.id, {}).get("status", "未上报")
                for region in regions
            }
            report["region_states_at_gate"] = last_states
            report["error"] = (
                f"{type(error).__name__}: {error}"[:400]
                + f"；区域状态={last_states}"
                + f"；采集错误={report.get('capture_errors', [])[:3]}"
            )[:900]
            print(report["error"], flush=True)
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {error}"
            print(report["error"], flush=True)
        finally:
            if "ocr" in locals():
                ocr.release.set()
            if service:
                service.stop()
            if dispatcher:
                dispatcher.stop()
            if server:
                server.shutdown()
                server.server_close()
            if storage:
                report["database_states"] = storage.queue_counts()
                report["total_event_records"] = len(storage.list_events(10000))
                storage.close()
            if "blocker" in locals():
                blocker.close()
            for scene in windows:
                scene.close()
            app.processEvents()
            app.quit()

    latencies = []
    for item in received:
        event = emitted.get(item["event_id"])
        if event:
            latencies.append(round((item["accepted_at"] - event["observed_at"]) * 1000, 2))
    report.update({
        "finished_at": time.time(), "wall_seconds": round(time.time() - started, 3),
        "process_cpu_seconds": round(time.process_time() - process_started, 3),
        "http_accepted": len(received), "capture_to_http_ms": latencies,
        "capture_to_http_p50_ms": percentile(latencies, 0.5),
        "capture_to_http_p95_ms": percentile(latencies, 0.95),
        "final_health": health, "health_transitions": transitions,
        "occlusion_layers_without_titles": occlusion_layers,
        "occlusion_note": (
            "生产 WGC 采集不按遮挡暂停；遮挡层的采集证据由 occluded_new_text_check 提供"
        ),
        "resource_samples": resource_samples,
        # 采集路径必须是生产路径，报告自己写明参数来源。
        "capture_backend": "wgc",
        "coordinate_space": "frame",
        "capture_timeout_seconds": real_env.PRODUCTION_CAPTURE_TIMEOUT,
        "uses_legacy_desktop_capture": False,
        "readiness_gate_passed": bool(report.get("readiness_gate_passed")),
        "region_gate_failures": report.get("region_gate_failures", {}),
        # 从 service.start() 到门禁判定的实际秒数：用于事后核对有没有“秒杀”。
        "gate_wait_seconds": report.get("gate_wait_seconds"),
        "region_states_at_gate": report.get("region_states_at_gate", {}),
        "aborted": report.get("aborted"),
        "capture_config": {
            "poll_interval_ms": 500, "force_ocr_seconds": 10,
            "capture_timeout_seconds": real_env.PRODUCTION_CAPTURE_TIMEOUT,
        },
        "recognition_fixture_markers_only": recognition_markers,
        "emitted_fixture_markers_only": [
            {"region_id": item["region_id"], "observed_at": item["observed_at"],
             "fixture_tokens": [token for token in (
                 "999", "499", "399", "299", "199", "A01", "A02", "A03", "A04", "COMMON", "MOVED",
             ) if token in item["text"]]}
            for item in emitted.values()
        ],
        "limitations": [
            "合成文本、字体、四窗尺寸及本机HTTP网络，不能推断真实微信群与飞书耗时",
            "不是200个人工标注事件，不代表95%召回率或98%精确率",
            "不是8小时运行验证，未测试真实锁屏、多DPI和手机送达",
        ],
        # 处理完成序号：每个自建区域最后完成识别的帧序号（不是提交序号）。
        "processed_sequences": dict(getattr(ocr, "sequences", {})) if "ocr" in locals() else {},
        "final_in_flight": service.in_flight() if service else None,
        "stats_window": {
            "started_at": started,
            "finished_at": time.time(),
            "note": "capture_to_http_ms 只覆盖这段区间内的通知；不是 60 秒调度证据",
        },
    })
    if report["success"]:
        p95 = report["capture_to_http_p95_ms"]
        performance_passed = p95 is not None and p95 <= 5000
        report["checks"].append({
            "name": "synthetic_capture_to_loopback_http_p95_within_5s",
            "passed": performance_passed,
            "detail": f"P95={p95}ms；仅合成四窗与本机HTTP",
        })
        report["success"] = performance_passed
        if performance_passed:
            print(f"PASS synthetic_capture_to_loopback_http_p95_within_5s: {p95}ms", flush=True)
        else:
            print(f"FAIL synthetic_capture_to_loopback_http_p95_within_5s: {p95}ms", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {args.output}", flush=True)
    print(f"Success={report['success']}; accepted={len(received)}; P95={report['capture_to_http_p95_ms']}ms")
    if report.get("aborted") == READINESS_ABORT:
        # 退出码 2：门禁未通过，报告不得被当成通过。
        print(f"ABORT: {READINESS_ABORT}（退出码 2）", flush=True)
        return 2
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
