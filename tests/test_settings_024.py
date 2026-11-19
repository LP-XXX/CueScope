"""0.2.4 settings and ordinary history UI, isolated from desktop and network."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QPushButton

from deal_watch.models import MessageCandidate, Rect
from deal_watch.ui.main import MainWindow
from deal_watch.ui.shell import PageId, RecordView


class Settings024Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.directory.name})
        self.environment.start()
        self.window = MainWindow(Path(__file__).resolve().parents[1])
        self.wait(lambda: self.window._recognition_ready)
        self.window.timer.stop()
        self.window.health_timer.stop()

    def wait(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition())

    def tearDown(self):
        self.window.service = None
        self.window.exit_app()
        self.wait(lambda: self.window._shutdown_done)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.directory.cleanup()

    def test_settings_apply_to_running_service_after_durable_save(self):
        intervals = []
        self.window.service = SimpleNamespace(
            hit_images=SimpleNamespace(enabled=True), update_poll_interval=intervals.append
        )
        self.window.poll_seconds.setValue(15)
        self.window.record_limit.setValue(12345)
        with patch.object(self.window.credentials, "save"), patch("deal_watch.ui.main.set_windows_autostart"):
            self.window.save_settings()
            self.wait(lambda: not self.window._settings_busy)
        saved = self.window.config_store.load()
        self.assertEqual(intervals, [15000])
        self.assertEqual(saved.poll_interval_ms, 15000)
        self.assertEqual(saved.recognition_record_limit, 12345)
        self.assertEqual(self.window.recognition.limit, 12345)

    def test_failed_save_does_not_apply_new_interval(self):
        intervals = []
        self.window.service = SimpleNamespace(
            hit_images=SimpleNamespace(enabled=True), update_poll_interval=intervals.append
        )
        self.window.poll_seconds.setValue(10)
        with patch.object(self.window.config_store, "save", side_effect=OSError("synthetic")), \
                patch("deal_watch.ui.main.QMessageBox.warning"):
            self.window.save_settings()
            self.wait(lambda: not self.window._settings_busy)
        self.assertEqual(intervals, [])
        self.assertEqual(self.window.config.poll_interval_ms, 30000)

    def test_history_has_pagination_and_no_label_controls(self):
        messages = [
            MessageCandidate("synthetic", f"ordinary {index}", Rect(0, 0, 100, 20), 1, 1)
            for index in range(205)
        ]
        self.window.recognition.record_candidates("synthetic", "synthetic", time.time(), messages, {})
        self.window.show()
        self.window.navigate(PageId.RECORDS, RecordView.RECOGNITION)
        self.window.refresh_recognition()
        self.wait(lambda: self.window.candidate_table.rowCount() == 200)
        self.assertEqual(self.window.candidate_table.columnCount(), 5)
        self.window._change_recognition_page(1)
        self.wait(lambda: self.window.candidate_table.rowCount() == 5)
        self.assertRegex(self.window.recognition_page_label.text(), r"2\s*/\s*2\s*页")
        self.assertIn("205", self.window.recognition_page_label.text())
        buttons = [button.text() for button in self.window.findChildren(QPushButton)]
        self.assertFalse(any("标注" in text or "训练" in text or "补录" in text for text in buttons))
