"""Local quiet hours, digest policy and privacy-preserving diagnostic export."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTimeEdit,
)

from ..quiet_hours import QuietHours
from ..support_bundle import support_counts, write_support_bundle


class NotificationTools(QObject):
    completed = Signal(str)

    def __init__(self, host, page, notification_form, storage_layout):
        super().__init__(page)
        self.page = page
        self.host = host
        self._thread = None
        self.quiet_button = QPushButton("设置免打扰时段和摘要")
        self.export_button = QPushButton("导出脱敏诊断包")
        notification_form.addRow("免打扰", self.quiet_button)
        storage_layout.addWidget(self.export_button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        storage_layout.addWidget(self.status)
        self.quiet_button.clicked.connect(self.edit_quiet)
        self.export_button.clicked.connect(self.choose_export)
        self.completed.connect(self.finished)

    def background(self, work, success):
        if getattr(self.host, "_shutdown_started", False) or self._thread and self._thread.is_alive():
            return
        self.export_button.setEnabled(False)
        self.quiet_button.setEnabled(False)
        self.status.setText("正在处理…")

        def run():
            try:
                work()
                message = success
            except Exception:
                message = "未能保存，请检查目标路径、可用磁盘空间和写入权限。"
            try:
                self.completed.emit(message)
            except RuntimeError:
                pass  # The application was closed during the explicit export.

        self._thread = threading.Thread(target=run, name="notification-preferences", daemon=True)
        self._thread.start()

    def wait(self):
        """Called from the shutdown worker before SQLite or Qt objects are closed."""
        if self._thread:
            self._thread.join()

    def finished(self, message: str):
        self.status.setText(message)
        self.export_button.setEnabled(True)
        self.quiet_button.setEnabled(True)

    def edit_quiet(self):
        previous = self.host.storage.quiet_hours
        dialog = QDialog(self.page)
        dialog.setWindowTitle("免打扰时段")
        form = QFormLayout(dialog)
        enabled = QCheckBox("启用免打扰")
        enabled.setChecked(previous.enabled)
        form.addRow(enabled)
        selectors = []
        for label, minutes in (("开始", previous.start_minute), ("结束", previous.end_minute)):
            widget = QTimeEdit(QTime(minutes // 60, minutes % 60))
            widget.setDisplayFormat("HH:mm")
            widget.setAccessibleName(label)
            selectors.append(widget)
            form.addRow(label, widget)
        summary = QCheckBox("结束时，每个已选接收端合并提醒一次")
        summary.setChecked(previous.summary)
        form.addRow(summary)
        hint = QLabel("按本机时间，支持跨午夜。时段内继续记录命中。摘要只含命中条数，结束后五分钟内有效。")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        form.addRow(buttons)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            minutes = [widget.time().hour() * 60 + widget.time().minute() for widget in selectors]
            try:
                selected = QuietHours(enabled.isChecked(), *minutes, summary.isChecked())
            except ValueError as exc:
                QMessageBox.information(dialog, "检查时段", str(exc))
                continue
            self.background(lambda value=selected: self.host.storage.save_quiet_hours(value), "免打扰设置已保存。")
            break
        dialog.deleteLater()

    def choose_export(self):
        choice = QMessageBox.question(
            self.page, "导出脱敏诊断包",
            "诊断包包含软件和系统版本、任务数量、采集方式、故障阶段及通知状态计数。保存位置由你选择。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if choice != QMessageBox.StandardButton.Ok:
            return
        path, _ = QFileDialog.getSaveFileName(self.page, "保存脱敏诊断包", "CueScope-diagnostics.zip", "ZIP (*.zip)")
        if not path:
            return
        counts = support_counts(self.host.config.regions, self.host.health_snapshots.values())
        self.background(lambda: write_support_bundle(Path(path), counts, self.host.storage.queue_counts()),
                        f"脱敏诊断包已保存到 {path}")
