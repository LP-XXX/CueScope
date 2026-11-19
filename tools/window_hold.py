"""为采集探测提供独立进程的可见目标窗口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-file", type=Path, required=True)
    parser.add_argument("--initial-text", default="WGC初始123")
    parser.add_argument("--x", type=int, default=None)
    parser.add_argument("--y", type=int, default=None)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    args = parser.parse_args()

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor, QFont, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    from deal_watch.__main__ import enable_dpi_awareness

    enable_dpi_awareness()
    app = QApplication([])
    screen = next((item for item in app.screens() if item.name() == "MNG007DA5-1"), None)
    if screen is None:
        raise RuntimeError("未找到MNG007DA5-1内屏")

    class Target(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.text = args.initial_text
            self.setWindowTitle("DealWatch WGC TARGET")
            self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
            geometry = screen.availableGeometry()
            width = args.width or min(460, (geometry.width() - 80) // 2)
            height = args.height or min(320, (geometry.height() - 90) // 2)
            x = args.x if args.x is not None else geometry.x() + 40
            y = args.y if args.y is not None else geometry.y() + 40
            self.setGeometry(x, y, width, height)

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#f5f5f5"))
            painter.setFont(QFont("Microsoft YaHei UI", 28))
            painter.setPen(QColor("#111111"))
            painter.drawText(30, 100, self.text)

    target = Target()
    target.show()
    print(int(target.winId()), flush=True)

    def refresh() -> None:
        try:
            value = args.control_file.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if value and value != target.text:
            target.text = value
            target.repaint()

    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(100)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
