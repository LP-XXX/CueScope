"""User-operated sample window for testing a real configured window."""

from __future__ import annotations

import argparse
import ctypes
import sys

from deal_watch.demo_display import DISPLAY1, display1_work_area, place_hidden_window, window_device


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="打开 CueScope 合成示例，供用户手动选择与监测")
    parser.add_argument("--require-monitor", default="", help="可选：要求 DISPLAY1 对应此 Qt 显示器名称（精确匹配）")
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    bounds = display1_work_area()
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget

    app = QApplication(sys.argv[:1])
    window = QWidget()
    window.setWindowTitle("CueScope · 合成示例窗口")
    window.setFont(QFont("Microsoft YaHei UI", 12))
    window.setStyleSheet("QWidget{background:#f7f8f6;color:#192923;} QLabel#sample{background:white;"
                        "border:1px solid #cdd8d2;border-radius:8px;padding:24px;font-size:18px;}"
                        "QPushButton{padding:10px;} QLabel{padding:4px;}")
    layout = QVBoxLayout(window)
    layout.setContentsMargins(24, 24, 24, 24)
    title = QLabel("选择下方白色文字区作为监测区域")
    title.setWordWrap(True)
    layout.addWidget(title)
    sample = QLabel("准备就绪\n示例任务正在等待\n所有内容均为人工合成数据")
    sample.setObjectName("sample")
    sample.setWordWrap(True)
    layout.addWidget(sample, 1)
    state = {"count": 0}

    def update_sample():
        state["count"] += 1
        sample.setText(f"DEMO-{state['count']:04d} 任务完成\n已生成结果\n示例耗时 12 秒")

    trigger = QPushButton("生成一条“任务完成”")
    trigger.clicked.connect(update_sample)
    layout.addWidget(trigger)
    reset = QPushButton("恢复等待状态")
    reset.clicked.connect(lambda: sample.setText("准备就绪\n示例任务正在等待\n所有内容均为人工合成数据"))
    layout.addWidget(reset)
    periodic = QCheckBox("每 45 秒生成一条示例（默认关闭）")
    timer = QTimer(window)
    timer.setInterval(45000)
    timer.timeout.connect(update_sample)
    periodic.toggled.connect(lambda checked: timer.start() if checked else timer.stop())
    layout.addWidget(periodic)
    hint = QLabel("先在 CueScope 预览并确认区域、开始监测，再生成示例。启动首帧只建立基线。")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    window.setMinimumSize(420, 300)
    if args.require_monitor:
        screens = [screen for screen in app.screens() if screen.name() == args.require_monitor]
        if len(screens) != 1:
            raise RuntimeError("未找到唯一匹配的 Qt 显示设备；没有显示任何窗口")
        window.winId()
        window.windowHandle().setScreen(screens[0])
        window.move(screens[0].availableGeometry().topLeft())
        if window_device(int(window.winId())) != DISPLAY1:
            raise RuntimeError("指定显示设备没有映射到 Windows DISPLAY1；没有显示任何窗口")
    place_hidden_window(int(window.winId()), bounds)
    window.show()
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
