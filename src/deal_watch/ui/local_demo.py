"""An isolated, synthetic desktop demonstration. Only this widget's pixels are read."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from ..config import ModelConfig
from ..demo_display import display1_work_area, place_hidden_window
from ..models import FrameSample, Rect
from ..ocr import LocalOcr
from ..rule_config import RuleCondition, RuleGroup
from ..rule_evaluation import evaluate_groups
from ..tracking import blocks_to_messages


class DemoRecognition(QThread):
    result = Signal(object, str)

    def __init__(self, image, base_dir, parent):
        super().__init__(parent)
        self.image = image
        self.base_dir = base_dir

    def run(self):
        try:
            height, width = self.image.shape[:2]
            frame = FrameSample("demo", 1, time.time(), self.image, Rect(0, 0, width, height))
            frame.content_mode = "generic_line"
            observation = LocalOcr(ModelConfig(), self.base_dir).recognize(frame)
            self.result.emit([unit.text for unit in blocks_to_messages(observation)], "")
        except Exception as error:
            self.result.emit([], str(error))


class LocalDemo(QWidget):
    def __init__(self, base_dir: Path):
        super().__init__()
        self.base_dir = base_dir
        self.worker = None
        self.closing = False
        self.sequence = 0
        self.rule = RuleGroup("完成提醒", [RuleCondition("任务完成")])
        self.setWindowTitle("CueScope · 体验本地提醒")
        self.setStyleSheet(
            "QWidget{background:#f5f7f8;color:#243339;font:15px 'Microsoft YaHei UI';}"
            "QLabel#heading{font-size:26px;font-weight:600;} QLabel#section{font-weight:600;}"
            "QLabel#target{background:white;border:2px solid #287365;border-radius:6px;"
            "padding:16px;color:#14231e;font:25px 'Microsoft YaHei UI';}"
            "QLabel#output,QLabel#alert{background:white;border:1px solid #cbd7d4;border-radius:5px;padding:10px;}"
            "QLabel#alert[matched='true']{background:#e1f3ea;color:#15513d;border-color:#86bca4;}"
            "QPushButton{background:#246959;color:white;border:0;border-radius:5px;padding:9px 16px;}"
            "QPushButton:disabled{background:#72817d;}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)
        title = QLabel("体验本地提醒")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("点击生成示例，让本机 OCR 读取绿色边框里的文字。演示只处理合成画面。"))
        label = QLabel("目标窗口文字区（合成）")
        label.setObjectName("section")
        layout.addWidget(label)
        self.target = QLabel("导出任务\nDEMO-0001  等待中")
        self.target.setObjectName("target")
        self.target.setMinimumHeight(99)
        layout.addWidget(self.target)
        self.button = QPushButton("生成一条“任务完成”")
        self.button.clicked.connect(self.generate)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        label = QLabel("OCR 读到的原文")
        label.setObjectName("section")
        layout.addWidget(label)
        self.output = QLabel("等待生成示例")
        self.output.setObjectName("output")
        self.output.setMinimumHeight(70)
        self.output.setTextFormat(Qt.TextFormat.PlainText)
        self.output.setWordWrap(True)
        layout.addWidget(self.output)
        label = QLabel("本地提醒 · 规则包含“任务完成”")
        label.setObjectName("section")
        layout.addWidget(label)
        self.alert = QLabel("尚无提醒")
        self.alert.setObjectName("alert")
        self.alert.setTextFormat(Qt.TextFormat.PlainText)
        self.alert.setWordWrap(True)
        layout.addWidget(self.alert)
        layout.addStretch()
        self.setMinimumSize(500, 448)

    def generate(self):
        if self.worker is not None:
            return
        self.sequence += 1
        self.target.setText(f"导出任务\nDEMO-{self.sequence:04d} 任务完成")
        self.target.repaint()
        image = self.target.grab().toImage().convertToFormat(QImage.Format.Format_RGB888)
        pixels = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
        bgr = pixels[:, :image.width() * 3].reshape(image.height(), image.width(), 3)[:, :, ::-1].copy()
        self.output.setText("正在本机识别…")
        self.alert.setText("等待识别结果")
        self.button.setEnabled(False)
        self.worker = DemoRecognition(bgr, self.base_dir, self)
        self.worker.result.connect(self.show_result)
        self.worker.finished.connect(self.finish)
        self.worker.start()

    def show_result(self, units, error):
        self.output.setText(error or "\n".join(units) or "当前画面未识别到文字")
        matches = [text for text in units if evaluate_groups(text, [self.rule]).should_notify]
        self.alert.setText("本地提醒  ·  " + "、".join(matches) if matches else "未触发提醒")
        self.alert.setProperty("matched", bool(matches))
        self.alert.style().unpolish(self.alert)
        self.alert.style().polish(self.alert)

    def finish(self):
        self.worker.deleteLater()
        self.worker = None
        self.button.setEnabled(True)
        if self.closing:
            self.close()

    def closeEvent(self, event):  # noqa: N802
        if self.worker is not None:
            self.closing = True
            self.button.setText("正在结束演示…")
            event.ignore()
        else:
            super().closeEvent(event)


def run_demo(base_dir):
    bounds = display1_work_area()
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(True)
    widget = LocalDemo(base_dir)
    place_hidden_window(int(widget.winId()), bounds)
    widget.show()
    return app.exec()
