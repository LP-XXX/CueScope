"""DISPLAY1 上的 WGC/PrintWindow 单窗可行性探测。

目标窗口运行在独立进程，避免 PrintWindow 向本进程阻塞的 UI 线程发送绘制消息
造成假超时。只创建本工具自己的合成窗口；不读取微信、用户配置或凭据。
不保存截图，报告只记录尺寸、OCR 命中和状态。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "build" / "validation" / "wgc-probe.json"
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("需要已解锁的 Windows 桌面")

    from deal_watch.__main__ import enable_dpi_awareness

    enable_dpi_awareness()
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication([])
    report = {
        "kind": "display1_synthetic_wgc_printwindow_probe",
        "started_at": time.time(),
        "passed": False,
        "screen": {},
        "checks": [],
    }
    target_process = None
    control_file = None
    blocker = None

    def check(name: str, passed: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        print(("PASS " if passed else "FAIL ") + name + (f": {detail}" if detail else ""))

    class Blocker(QWidget):
        def __init__(self, geometry) -> None:
            super().__init__()
            self.setWindowTitle("DealWatch WGC PROBE OCCLUDER")
            self.setWindowFlags(
                Qt.WindowType.Window
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
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
            **mapping,
            "name": screen.name(),
            "device_pixel_ratio": screen.devicePixelRatio(),
        }

        from deal_watch.capture import WindowsWindows
        from deal_watch.config import ModelConfig
        from deal_watch.models import FrameSample, Rect
        from deal_watch.ocr import LocalOcr
        from deal_watch.window_capture import WindowCaptureClient

        with tempfile.NamedTemporaryFile(
            prefix="deal-watch-wgc-control-", suffix=".txt", delete=False
        ) as handle:
            control_file = Path(handle.name)
        control_file.write_text("WGC初始123", encoding="utf-8")
        target_process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tools" / "window_hold.py"),
                "--control-file",
                str(control_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        target_line = target_process.stdout.readline() if target_process.stdout else ""
        if not target_line.strip().isdigit():
            raise RuntimeError("目标窗口进程未返回有效 HWND")
        hwnd = int(target_line.strip())
        time.sleep(0.4)

        client = WindowCaptureClient()
        frame = client.capture(hwnd, "wgc")
        check("wgc_frame_valid", frame.width > 0 and frame.height > 0,
              f"{frame.width}x{frame.height}")

        target_rect = WindowsWindows().get_rect(hwnd)
        if target_rect is None:
            raise RuntimeError("无法取得目标窗口边界")
        ratio = screen.devicePixelRatio()
        origin = screen.geometry()
        blocker_geometry = QRect(
            round((target_rect.x - origin.x()) / ratio + origin.x()),
            round((target_rect.y - origin.y()) / ratio + origin.y()),
            round(target_rect.width / ratio),
            round(target_rect.height / ratio),
        )
        blocker = Blocker(blocker_geometry)
        blocker.show()
        control_file.write_text("WGC更新456", encoding="utf-8")
        time.sleep(0.6)
        app.processEvents()
        occluded = client.capture(hwnd, "wgc")
        ocr = LocalOcr(ModelConfig(), ROOT)
        observation = ocr.recognize(FrameSample(
            "wgc-probe", 1, time.time(), occluded.image, Rect(0, 0, occluded.width, occluded.height)
        ))
        text = "\n".join(item.text for item in observation.blocks)
        check("wgc_updates_under_occlusion", "456" in text, text[:120])

        print_frame = client.capture(hwnd, "print-window")
        check(
            "print_window_frame_valid_under_occlusion",
            print_frame.width > 0 and print_frame.height > 0,
            f"{print_frame.width}x{print_frame.height}",
        )
        report["passed"] = all(item["passed"] for item in report["checks"])
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"[:300]
        print(report["error"])
    finally:
        if blocker is not None:
            blocker.close()
        if target_process is not None:
            target_process.terminate()
            try:
                target_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                target_process.kill()
        if control_file is not None:
            control_file.unlink(missing_ok=True)
        app.processEvents()
        app.quit()
    report["finished_at"] = time.time()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {args.output}")
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
