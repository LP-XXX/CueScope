"""Compact rows keep state/freshness visible and complete diagnostics reachable."""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import patch

from PySide6.QtGui import QFont, QFontMetrics

from deal_watch.models import HealthSnapshot, RegionStatus
from deal_watch.ui.main import health_status_text
from deal_watch.ui.region_list import RegionColumn, RegionRole
from region_fixtures import ready_region
from test_main_window_layout import LayoutTestCase


class HealthLayoutTests(LayoutTestCase):
    def test_compact_health_and_details_remain_available_at_minimum_size(self):
        regions = [ready_region(f"合成区域 {i + 1}") for i in range(5)]
        self.window.config.regions = regions
        self.window._load_regions()
        self.window.service = object()
        now = time.time()
        try:
            with patch.object(self.window, "queue_system_message"):
                for region in regions:
                    self.window.on_health(HealthSnapshot(
                        region.id, RegionStatus.ERROR, "request_id=synthetic-test",
                        capture_error_summary="采集会话启动未完成",
                        capture_worker_state="reaping", next_retry_at=now + 30,
                        last_success_at=now - 30, last_ocr_ms=185,
                        target_interval_ms=30000, actual_interval_ms=30100,
                        schedule_delay_ms=100, ocr_queue_ms=80,
                        stages_summary="OCR P50 185ms / P95 240ms",
                    ))
            for width, height in ((1000, 720), (760, 520)):
                self.window.resize(width, height)
                self.app.processEvents()
                table = self.window.region_table
                self.assertEqual(self.window.width(), width)
                self.assertEqual(table.columnCount(), 4)
                self.assertEqual(table.horizontalScrollBar().maximum(), 0)
                status = table.item(0, RegionColumn.STATUS)
                self.assertEqual(status.data(RegionRole.STATE), "error")
                self.assertEqual(status.data(RegionRole.DETAIL), "采集会话启动未完成")
                self.assertIn("重试", status.data(RegionRole.RETRY))
                self.assertNotIn("request_id", status.text())
                self.assertIn("synthetic-test", status.toolTip())
                self.assertIn("P95 240ms", status.toolTip())
                detail_font = QFont(table.font())
                detail_font.setPixelSize(12)
                metrics = QFontMetrics(detail_font)
                for line in (status.data(RegionRole.DETAIL), status.data(RegionRole.RETRY)):
                    self.assertLessEqual(metrics.horizontalAdvance(line), table.columnWidth(RegionColumn.STATUS) - 22)
                success = table.item(0, RegionColumn.SUCCESS)
                self.assertEqual(success.text(), datetime.fromtimestamp(now - 30).strftime("%H:%M:%S"))
                self.assertNotIn("ms", success.text())
                self.assertIn("P95 240ms", success.toolTip())
                self.assertGreaterEqual(table.rowHeight(0), 64)
                self.assertTrue(self.visible_in_window(self.window.add_region_button))
                self.assertTrue(self.visible_in_window(self.window.stop_button))
                self.assertFalse(self.window.start_button.isVisible())
            self.window.region_table.selectRow(0)
            with patch("deal_watch.ui.main.RecordDetailDialog") as detail:
                self.window.show_region_detail()
            fields = dict(detail.call_args.args[1])
            self.assertIn("物理像素区域", fields)
            self.assertIn("窗口句柄 / 进程", fields)
            self.assertIn("synthetic-test", fields["诊断"])
            self.assertIn("P95 240ms", fields["诊断"])
        finally:
            self.window.service = None

    def test_status_prefix_is_not_repeated(self):
        running = health_status_text(HealthSnapshot(
            "test", RegionStatus.RUNNING, "监控中；无新消息时也保持此状态",
        ))
        paused = health_status_text(HealthSnapshot(
            "test", RegionStatus.PAUSED, "已暂停：窗口最小化",
        ))
        self.assertEqual(running, "监控中")
        self.assertEqual(paused, "已暂停\n窗口最小化")
