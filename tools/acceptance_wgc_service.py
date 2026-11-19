"""DISPLAY1 四窗 WGC 每分钟调度真机验收。

只使用本工具创建的四个目标窗口和遮挡窗口；不读取微信、用户配置或凭据。
通知只发送到 127.0.0.1 模拟飞书。默认执行十轮 60 秒调度。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import subprocess
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

import real_env  # noqa: E402  （先补齐 sys.path 才能导入共享层）

# 本工具直接用 WindowCaptureClient / WindowFrameCapturer 起采集子进程；子进程只继承
# os.environ，所以必须把 src/ROOT 放进 PYTHONPATH，否则开发态全部 capture_failed。
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


class DeferredDirectory:
    """退出 with 时暂不删除；等 worker 和 SQLite 连接关闭后再清理。"""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="deal-watch-wgc-acceptance-")

    def __enter__(self) -> str:
        return self._directory.name

    def __exit__(self, *_args) -> bool:
        return False

    def cleanup(self) -> None:
        self._directory.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    if args.rounds != 10:
        raise SystemExit("本验收固定为十轮场景")
    if sys.platform != "win32":
        raise SystemExit("需要已解锁的 Windows 桌面")

    from deal_watch.__main__ import enable_dpi_awareness

    enable_dpi_awareness()
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication([])
    report: dict = {
        "kind": "display1_four_window_wgc_60s_schedule_real_ocr_loopback_http",
        "scope": "仅自建合成目标窗口；无真实微信、真实飞书和手机送达验证",
        "started_at": time.time(),
        "passed": False,
        "checks": [],
        "rounds": args.rounds,
        "interval_seconds": args.interval_seconds,
    }
    received: list[dict] = []
    emitted: dict[str, dict] = {}
    health: dict[str, dict] = {}
    transitions: list[dict] = []
    resources: list[dict] = []
    lock = threading.Lock()
    target_processes: list[subprocess.Popen] = []
    control_files: list[Path] = []
    # 采集客户端实际生效的超时；创建后从 client.timeout 读回，报告只写实测值。
    capture_client_timeout: float | None = None
    blocker = None
    service = None
    dispatcher = None
    storage = None
    feedback = None
    server = None
    regions = []
    temporary_context = None
    process_started = time.process_time()

    def check(name: str, passed: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        print(("PASS " if passed else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
        if not passed:
            raise AssertionError(f"{name}: {detail}")

    def pump(duration: float = 0.2) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.015)

    def wait_until(predicate, label: str, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (args.timeout if timeout is None else timeout)
        last_resource = time.monotonic()
        while time.monotonic() < deadline:
            pump(0.1)
            if predicate():
                return
            if time.monotonic() - last_resource >= 10:
                resources.append(sample_resources())
                last_resource = time.monotonic()
        states = {key: value.get("status") for key, value in health.items()}
        raise AssertionError(f"等待超时：{label}；状态={states}；接收数={len(received)}")

    def sample_resources() -> dict:
        import os
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working_set", "working_set", "peak_paged", "paged",
                    "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private_usage",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        sample = {"at": time.time()}
        if psapi.GetProcessMemoryInfo(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
            sample["working_set_mb"] = round(counters.working_set / 1024**2, 2)
            sample["private_commit_mb"] = round(counters.private_usage / 1024**2, 2)
        if service:
            sample["queue_depth"] = len(service._pending)
        sample["cpu_count"] = os.cpu_count()
        resources.append(sample)
        return sample

    def write_targets(texts: list[str]) -> None:
        for path, text in zip(control_files, texts, strict=True):
            path.write_text(text, encoding="utf-8")

    def show_window(hwnd: int, command: int) -> None:
        ctypes.windll.user32.ShowWindow(hwnd, command)

    def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
        ctypes.windll.user32.MoveWindow(hwnd, x, y, width, height, True)

    def on_health(snapshot) -> None:
        value = {
            "status": snapshot.status.value, "detail": snapshot.detail,
            "last_ocr_ms": snapshot.last_ocr_ms, "queue_depth": snapshot.queue_depth,
            "dropped_frames": snapshot.dropped_frames, "at": time.time(),
        }
        with lock:
            previous = health.get(snapshot.region_id)
            health[snapshot.region_id] = value
            if not previous or previous["status"] != value["status"]:
                transitions.append({"region_id": snapshot.region_id, **value})

    def on_events(events) -> None:
        with lock:
            for event in events:
                emitted[event.id] = {
                    "region_id": event.region_id, "observed_at": event.observed_at,
                    "text": event.text, "tracking_id": event.tracking_id,
                    "enqueued_at": time.time(),
                }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
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
            return

    class Blocker(QWidget):
        def __init__(self, geometry) -> None:
            super().__init__()
            self.setWindowTitle("DealWatch WGC ACCEPTANCE OCCLUDER")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            )
            self.setGeometry(geometry)

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#304ffe"))

    try:
        from ctypes import wintypes

        class DisplayDevice(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128),
            ]

        user32 = ctypes.WinDLL("user32")
        user32.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DisplayDevice), wintypes.DWORD
        ]
        user32.EnumDisplayDevicesW.restype = wintypes.BOOL
        mapping = None
        for index in range(64):
            adapter = DisplayDevice()
            adapter.cb = ctypes.sizeof(adapter)
            if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(adapter), 0):
                break
            if adapter.DeviceName.upper() != r"\\.\DISPLAY1":
                continue
            for monitor_index in range(16):
                monitor = DisplayDevice()
                monitor.cb = ctypes.sizeof(monitor)
                if not user32.EnumDisplayDevicesW(
                    adapter.DeviceName, monitor_index, ctypes.byref(monitor), 0
                ):
                    break
                hardware = monitor.DeviceID.upper()
                if "SAC2763" in hardware:
                    raise RuntimeError("DISPLAY1映射到禁止的G72硬件")
                if "CSW1632" in hardware:
                    mapping = {"win32_display": adapter.DeviceName, "hardware_id": "CSW1632"}
        if not mapping:
            raise RuntimeError("未验证DISPLAY1到CSW1632内屏映射")
        screen = next((item for item in app.screens() if item.name() == "MNG007DA5-1"), None)
        if screen is None:
            raise RuntimeError("未找到MNG007DA5-1内屏")
        report["screen"] = {
            **mapping, "name": screen.name(), "device_pixel_ratio": screen.devicePixelRatio(),
        }
        geometry = screen.availableGeometry()
        target_width = min(430, (geometry.width() - 70) // 2)
        target_height = min(300, (geometry.height() - 80) // 2)
        positions = [
            (geometry.x() + 20, geometry.y() + 20),
            (geometry.x() + geometry.width() - target_width - 20, geometry.y() + 20),
            (geometry.x() + 20, geometry.y() + geometry.height() - target_height - 20),
            (
                geometry.x() + geometry.width() - target_width - 20,
                geometry.y() + geometry.height() - target_height - 20,
            ),
        ]
        from deal_watch.capture import WindowsWindows
        from deal_watch.config import AppConfig, ModelConfig
        from deal_watch.models import Rect, RegionConfig
        from deal_watch.notify import FeishuClient, NotificationDispatcher
        from deal_watch.ocr import LocalOcr
        from deal_watch.recognition import RecognitionStore
        from deal_watch.service import MonitorService
        from deal_watch.storage import Storage
        from deal_watch.window_capture import WindowCaptureClient, WindowFrameCapturer

        temporary_context = DeferredDirectory()
        with temporary_context as temporary:
            temporary_path = Path(temporary)
            for index, (x, y) in enumerate(positions):
                control = temporary_path / f"control-{index}.txt"
                control.write_text("历史手机999元", encoding="utf-8")
                control_files.append(control)
                process = subprocess.Popen(
                    [
                        sys.executable, str(ROOT / "tools" / "window_hold.py"),
                        "--control-file", str(control),
                        "--initial-text", "历史手机999元",
                        "--x", str(x), "--y", str(y),
                        "--width", str(target_width), "--height", str(target_height),
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8",
                )
                target_processes.append(process)
            time.sleep(1.0)
            pump(0.2)
            # 显式传生产超时，并把 client 实际生效值读回来写进报告（不自证常量）。
            client = WindowCaptureClient(timeout=real_env.PRODUCTION_CAPTURE_TIMEOUT)
            capture_client_timeout = float(client.timeout)
            windows_api = WindowsWindows()
            for index, process in enumerate(target_processes):
                line = process.stdout.readline() if process.stdout else ""
                if not line.strip().isdigit():
                    raise RuntimeError(f"目标窗口 {index + 1} 未返回 HWND")
                hwnd = int(line.strip())
                window = windows_api.window_from_hwnd(hwnd)
                if not window:
                    raise RuntimeError(f"目标窗口 {index + 1} 无效")
                frame = client.capture(hwnd, "wgc")
                roi_y = max(42, int(frame.height * 0.10))
                regions.append(
                    RegionConfig(
                        id=f"wgc-{index + 1}", alias=f"WGC合成群{index + 1}",
                        window_title=window.title, window_class=window.class_name,
                        window_rect=window.rect, window_hwnd=hwnd,
                        process_id=window.process_id, capture_backend="wgc",
                        coordinate_space="frame",
                        reference_frame_width=frame.width,
                        reference_frame_height=frame.height,
                        rect=Rect(8, roi_y, frame.width - 16, frame.height - roi_y - 8),
                        # 仅内存标记：0.2.1 的就绪门禁要求“已预览确认”，本工具在
                        # 生成区域前已经用 WGC 抓帧并按其尺寸构造 ROI，等价于人工确认；
                        # 该字段只存在内存 RegionConfig 里，绝不写入用户配置或落盘。
                        previewed=True,
                    )
                )
            report["frame_sizes"] = [
                [region.reference_frame_width, region.reference_frame_height] for region in regions
            ]

            storage = Storage(temporary_path / "wgc.db")
            recognition = RecognitionStore(temporary_path)
            recognition.prepare()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/ok"
            dispatcher = NotificationDispatcher(
                storage, lambda: FeishuClient(endpoint, timeout=3)
            )
            dispatcher.start()
            config = AppConfig(
                regions=regions, keywords=["手机"], save_hit_images=False,
                poll_interval_ms=int(args.interval_seconds * 1000),
                force_ocr_seconds=args.interval_seconds + 1,
                match_mode="char_multiset",
            )
            service = MonitorService(
                config, storage, LocalOcr(ModelConfig(), ROOT), capturer=WindowFrameCapturer(),
                data_dir=temporary_path, on_events=on_events, on_health=on_health,
                recognition_store=recognition,
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
                lambda: len(health) == 4 and all(value["status"] == "running" for value in health.values()),
                "四个 WGC 窗口建立基线", timeout=args.interval_seconds + args.timeout,
            )
            pump(1)
            check("baseline_does_not_notify", not received and not storage.list_events())

            expected = 0
            # 第 1 轮：四窗各自新增，字符规则允许同消息插入修饰词。
            write_targets(["手机499元A01", "手机499元A02", "手机499元A03", "手机499元A04"])
            expected += 4
            wait_until(lambda: len(received) >= expected, "第1轮四窗新增", args.interval_seconds + args.timeout)
            check("round1_four_unique", len(received) == expected, f"接收={len(received)}")

            # 第 2 轮：四群相同内容，仅第一条通知。
            write_targets(["手机399元COMMON"] * 4)
            expected += 1
            wait_until(lambda: len(received) >= expected, "第2轮跨群去重", args.interval_seconds + args.timeout)
            check("round2_cross_group_dedupe", len(received) == expected, f"接收={len(received)}")

            # 第 3 轮：价格变化必须提醒。
            write_targets(["手机299元CHANGE", "手机399元COMMON", "手机399元COMMON", "手机399元COMMON"])
            expected += 1
            wait_until(lambda: len(received) >= expected, "第3轮价格变化", args.interval_seconds + args.timeout)
            check("round3_price_change", len(received) == expected, f"接收={len(received)}")

            # 第 4 轮：窗口移动不影响帧内 ROI。
            first_window = windows_api.window_from_hwnd(regions[0].window_hwnd)
            assert first_window is not None
            move_window(
                regions[0].window_hwnd, first_window.rect.x + 24, first_window.rect.y,
                first_window.rect.width, first_window.rect.height,
            )
            write_targets(["手机289元MOVED", "手机399元COMMON", "手机399元COMMON", "手机399元COMMON"])
            expected += 1
            wait_until(lambda: len(received) >= expected, "第4轮移动后更新", args.interval_seconds + args.timeout)
            check("round4_window_move_keeps_roi", len(received) == expected, f"接收={len(received)}")

            # 第 5 轮：最小化暂停；其他窗口相同内容仍去重。
            show_window(regions[0].window_hwnd, 6)
            write_targets(["手机279元MINIMIZED", "手机269元OTHER", "手机269元OTHER", "手机269元OTHER"])
            expected += 1
            wait_until(lambda: len(received) >= expected, "第5轮最小化暂停", args.interval_seconds + args.timeout)
            wait_until(
                lambda: health.get(regions[0].id, {}).get("status") == "paused",
                "最小化区域进入暂停状态", args.timeout,
            )
            check("round5_minimized_pauses", len(received) == expected, f"接收={len(received)}")

            # 第 6 轮：恢复后先基线，不推送最小化期间文字。
            show_window(regions[0].window_hwnd, 9)
            wait_until(
                lambda: health.get(regions[0].id, {}).get("status") == "running",
                "恢复后重新基线", args.interval_seconds + args.timeout,
            )
            pump(2)
            check("round6_recovery_no_history", len(received) == expected, f"接收={len(received)}")

            # 第 7 轮：恢复后的新内容正常提醒。
            write_targets(["手机259元RESTORED", "手机269元OTHER", "手机269元OTHER", "手机269元OTHER"])
            expected += 1
            wait_until(lambda: len(received) >= expected, "第7轮恢复后新增", args.interval_seconds + args.timeout)
            check("round7_new_after_recovery", len(received) == expected, f"接收={len(received)}")

            # 第 8 轮：缩放要求重新校准，不读取旧结果。
            resized = windows_api.window_from_hwnd(regions[0].window_hwnd)
            assert resized is not None
            move_window(
                regions[0].window_hwnd, resized.rect.x, resized.rect.y,
                resized.rect.width + 60, resized.rect.height + 30,
            )
            write_targets(["手机249元RESIZED", "手机269元OTHER", "手机269元OTHER", "手机269元OTHER"])
            wait_until(
                lambda: health.get(regions[0].id, {}).get("status") == "error",
                "缩放后要求重新校准", args.interval_seconds + args.timeout,
            )
            check("round8_resize_requires_recalibration", len(received) == expected, f"接收={len(received)}")
            recalibrated = client.capture(regions[0].window_hwnd, "wgc")
            regions[0].reference_frame_width = recalibrated.width
            regions[0].reference_frame_height = recalibrated.height
            regions[0].rect = Rect(
                8, max(42, int(recalibrated.height * 0.10)),
                recalibrated.width - 16, recalibrated.height - max(42, int(recalibrated.height * 0.10)) - 8,
            )
            service.resume_region(regions[0].id)
            wait_until(
                lambda: health.get(regions[0].id, {}).get("status") == "running",
                "重新校准后基线", args.timeout,
            )

            # 第 9 轮：重新校准后的新内容提醒。
            write_targets(["手机239元RECALIBRATED", "手机269元OTHER", "手机269元OTHER", "手机269元OTHER"])
            expected += 1
            wait_until(lambda: len(received) >= expected, "第9轮重新校准后新增", args.interval_seconds + args.timeout)
            check("round9_after_recalibration", len(received) == expected, f"接收={len(received)}")

            # 第 10 轮：完全遮挡下 WGC 仍取得更新并通知。
            blocked_window = windows_api.window_from_hwnd(regions[1].window_hwnd)
            assert blocked_window is not None
            ratio = screen.devicePixelRatio()
            origin = screen.geometry()
            blocker = Blocker(QRect(
                round((blocked_window.rect.x - origin.x()) / ratio + origin.x()),
                round((blocked_window.rect.y - origin.y()) / ratio + origin.y()),
                round(blocked_window.rect.width / ratio),
                round(blocked_window.rect.height / ratio),
            ))
            blocker.show()
            write_targets(["手机239元RECALIBRATED", "手机229元OCCLUDED", "手机269元OTHER", "手机269元OTHER"])
            expected += 1
            wait_until(lambda: len(received) >= expected, "第10轮遮挡更新", args.interval_seconds + args.timeout)
            check("round10_occluded_update_notifies", len(received) == expected, f"接收={len(received)}")
            check(
                "occlusion_does_not_pause_wgc",
                health.get(regions[1].id, {}).get("status") == "running",
                str(health.get(regions[1].id, {}).get("detail", "")),
            )

            check("workers_alive", bool(
                service._capture_thread.is_alive() and service._ocr_thread.is_alive()
                and dispatcher._thread.is_alive()
            ))
            check("no_sender_error", not dispatcher.last_error, dispatcher.last_error)
            candidate_count = len(feedback.list_candidates(10000))
            check("feedback_records_all_samples", candidate_count >= 4, f"候选={candidate_count}")
            latencies = []
            for item in received:
                event = emitted.get(item["event_id"])
                if event:
                    latencies.append(round((item["accepted_at"] - event["observed_at"]) * 1000, 2))
            p95 = percentile(latencies, 0.95)
            check("p95_within_5s", p95 is not None and p95 <= 5000, f"P95={p95}ms")
            report.update({
                "http_accepted": len(received),
                "capture_to_http_ms": latencies,
                "capture_to_http_p50_ms": percentile(latencies, 0.5),
                "capture_to_http_p95_ms": p95,
                "feedback_candidate_count": candidate_count,
            })
            report["passed"] = True
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
        )[:900]
        print(report["error"], flush=True)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"[:500]
        print(report["error"], flush=True)
    finally:
        if blocker is not None:
            blocker.close()
        if service:
            service.stop()
        if dispatcher:
            dispatcher.stop()
        if server:
            server.shutdown()
            server.server_close()
        if storage:
            report["database_states"] = storage.queue_counts()
            storage.close()
        if feedback:
            try:
                recognition.close()
            except Exception:
                pass
        for process in target_processes:
            process.terminate()
        for process in target_processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        app.processEvents()
        app.quit()
        if temporary_context is not None:
            temporary_context.cleanup()

    report.update({
        "finished_at": time.time(),
        "wall_seconds": round(time.time() - report["started_at"], 3),
        "process_cpu_seconds": round(time.process_time() - process_started, 3),
        "final_health": health,
        "health_transitions": transitions,
        "resource_samples": resources,
        "readiness_gate_passed": bool(report.get("readiness_gate_passed")),
        "region_gate_failures": report.get("region_gate_failures", {}),
        # 从 service.start() 到门禁判定的实际秒数：用于事后核对有没有“秒杀”。
        "gate_wait_seconds": report.get("gate_wait_seconds"),
        "region_states_at_gate": report.get("region_states_at_gate", {}),
        "aborted": report.get("aborted"),
        "capture_backend": "wgc",
        "coordinate_space": "frame",
        # 写 client.timeout 的实测值；若客户端根本没建起来，退回生产常量并靠 aborted/error 说明。
        "capture_timeout_seconds": (
            capture_client_timeout
            if capture_client_timeout is not None
            else float(real_env.PRODUCTION_CAPTURE_TIMEOUT)
        ),
        "uses_legacy_desktop_capture": False,
        "emitted_texts_are_synthetic_only": [
            {"region_id": value["region_id"], "text": value["text"]} for value in emitted.values()
        ],
        "limitations": [
            "合成窗口和本机HTTP，不代表真实微信、真实飞书网络或手机送达",
            "十轮60秒调度不等同于八小时连续运行",
            "未训练RBT3，本报告只验证字符规则和反馈候选记录",
        ],
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {args.output}", flush=True)
    print(
        f"Success={report.get('passed')}; accepted={len(received)}; "
        f"P95={report.get('capture_to_http_p95_ms')}ms", flush=True,
    )
    if report.get("aborted") == READINESS_ABORT:
        print(f"ABORT: {READINESS_ABORT}（退出码 2）", flush=True)
        return 2
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
