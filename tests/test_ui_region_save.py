"""0.2.2 T1.3：区域保存交互与状态机（offscreen，不占屏）。

覆盖：成功后才刷新表格、保存中重复提交只发起一次、停止期间三个入口都拒绝、
新增行被选中且表格可见、失败时内存/表格/磁盘三者停在旧快照、各动作失败回滚。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from deal_watch.config import ConfigStore  # noqa: E402
from deal_watch.models import Rect, RegionConfig  # noqa: E402
from deal_watch.region_save import RegionSaveResult  # noqa: E402
from deal_watch.ui.main import PAUSE_FIRST_HINT, SAVING_HINT, MainWindow  # noqa: E402
from region_fixtures import ready_region  # noqa: E402


class FakeRegionEditDialog:
    """替身：接受即返回固定区域，避免真正打开圈选对话框。"""

    DialogCode = QDialog.DialogCode

    def __init__(self, _parent=None, existing: RegionConfig | None = None) -> None:
        self.existing = existing

    def exec(self) -> int:
        return QDialog.DialogCode.Accepted

    def region(self) -> RegionConfig:
        if self.existing is not None:
            return ready_region(
                self.existing.alias,
                hwnd=self.existing.window_hwnd,
                rect=Rect(0, 0, 320, 520),
                previewed=False,
            )
        return ready_region("新群", hwnd=2002, rect=Rect(10, 20, 400, 600), previewed=False)


class RegionSaveInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(Path(__file__).resolve().parents[1])
        ready_deadline = time.monotonic() + 5
        while not self.window._recognition_ready and time.monotonic() < ready_deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(self.window._recognition_ready)

    def tearDown(self):
        self.window.exit_app()
        self.pump_until(lambda: self.window._shutdown_done)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def pump_until(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(condition())

    def seed(self, regions: list[RegionConfig]) -> None:
        """把区域同时写进内存与磁盘，保证用例起点满足不变量 I-1。"""
        self.window.config.regions = list(regions)
        self.window.config_store.save(self.window.config)
        self.window._load_regions()
        if regions:
            self.window.region_table.selectRow(0)

    def disk_regions(self) -> list[RegionConfig]:
        return ConfigStore(self.window.data_dir).load().regions

    def table_states(self) -> list[str]:
        return [
            self.window.region_table.item(row, 1).text()
            for row in range(self.window.region_table.rowCount())
        ]

    def fail_writes(self):
        """注入“替换配置文件被占用”的故障（真实配置层，不是打桩 region_save）。"""
        return (
            patch(
                "deal_watch.config.os.replace",
                side_effect=PermissionError("[WinError 5] 拒绝访问"),
            ),
            patch("deal_watch.config.time.sleep"),
        )

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------

    def test_add_region_updates_table_only_after_success(self):
        loads: list[int] = []
        original = self.window._load_regions

        def counting_load() -> None:
            loads.append(1)
            original()

        self.window._load_regions = counting_load
        with patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog):
            self.window.add_region()
            # 保存是后台任务：此时表格还没有新行，内存也未提交。
            self.assertEqual(self.window.region_table.rowCount(), 0)
            self.assertTrue(self.window._saving)
            self.pump_until(lambda: not self.window._saving)
        self.assertEqual(self.window.region_table.rowCount(), 1)
        self.assertGreaterEqual(len(loads), 1)
        self.assertEqual(len(self.window.config.regions), 1)
        self.assertEqual(len(self.disk_regions()), 1)

    def test_new_row_is_selected_and_visible_with_pending_state(self):
        with patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog):
            self.window.add_region()
            self.pump_until(lambda: not self.window._saving)
        # 用户最痛的点：没预览确认的区域也必须出现在表格里。
        self.assertEqual(self.table_states(), ["待确认"])
        self.assertEqual(self.window.region_table.currentRow(), 0)
        selected = self.window.region_table.selectionModel().selectedRows()
        self.assertEqual([index.row() for index in selected], [0])
        self.assertIn("已保存，待确认", self.window.statusBar().currentMessage())

    def test_saving_disables_configuration_buttons_then_restores(self):
        release = threading.Event()

        def slow(*_args, **_kwargs):
            release.wait(5)
            return RegionSaveResult(ok=True, action="add")

        with patch("deal_watch.ui.main.commit_region_change", side_effect=slow), patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ):
            self.window.add_region()
            self.assertFalse(self.window.configuration_buttons[0].isEnabled())
            release.set()
            self.pump_until(lambda: not self.window._saving)
        self.assertTrue(self.window.configuration_buttons[0].isEnabled())

    def test_duplicate_submit_while_saving_starts_only_one_save(self):
        release = threading.Event()
        calls: list[int] = []

        def slow(*_args, **_kwargs):
            calls.append(1)
            release.wait(5)
            return RegionSaveResult(ok=True, action="add")

        with patch("deal_watch.ui.main.commit_region_change", side_effect=slow), patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ), patch("deal_watch.ui.main.QMessageBox.information") as information:
            self.window.add_region()
            self.window.add_region()
            self.window.add_region()
            release.set()
            self.pump_until(lambda: not self.window._saving)
        self.assertEqual(len(calls), 1)
        self.assertTrue(information.called)
        self.assertIn("正在保存", information.call_args.args[2])
        self.assertEqual(information.call_args.args[2], SAVING_HINT)

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------

    def test_all_three_entries_reject_while_stopping(self):
        self.window._stopping_service = object()
        try:
            with patch("deal_watch.ui.main.RegionEditDialog") as dialog, patch(
                "deal_watch.ui.main.QMessageBox.information"
            ) as information:
                self.window.add_region()
                self.window.edit_region()
                self.window.delete_region()
        finally:
            self.window._stopping_service = None
        dialog.assert_not_called()
        self.assertEqual(information.call_count, 3)
        for call in information.call_args_list:
            self.assertEqual(call.args[2], PAUSE_FIRST_HINT)

    def test_add_is_rejected_while_monitoring(self):
        self.window.service = object()
        try:
            with patch("deal_watch.ui.main.RegionEditDialog") as dialog, patch(
                "deal_watch.ui.main.QMessageBox.information"
            ) as information:
                self.window.add_region()
            dialog.assert_not_called()
            self.assertEqual(information.call_args.args[2], PAUSE_FIRST_HINT)
        finally:
            self.window.service = None

    def test_edit_region_stops_monitoring_before_saving(self):
        order: list[str] = []

        class FakeService:
            def request_stop(self):
                order.append("request_stop")

            def stop(self):
                order.append("stop_done")

            def is_alive(self):
                return False

        self.seed([ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        self.window.service = FakeService()

        def record(*_args, **_kwargs):
            order.append("save")
            return RegionSaveResult(ok=True, action="update")

        with patch("deal_watch.ui.main.commit_region_change", side_effect=record), patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ):
            self.window.edit_region()
            self.pump_until(lambda: "save" in order)
        self.assertEqual(order[0], "request_stop")
        self.assertLess(order.index("stop_done"), order.index("save"))

    # ------------------------------------------------------------------
    # 失败回滚
    # ------------------------------------------------------------------

    def test_add_failure_keeps_memory_table_and_disk(self):
        replace_patch, sleep_patch = self.fail_writes()
        with replace_patch, sleep_patch, patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ), patch("deal_watch.ui.main.QMessageBox.critical") as critical:
            self.window.add_region()
            self.pump_until(lambda: not self.window._saving)
        self.assertEqual(self.window.config.regions, [])
        self.assertEqual(self.window.region_table.rowCount(), 0)
        self.assertEqual(self.disk_regions(), [])
        self.assertTrue(critical.called)
        self.assertEqual(critical.call_args.args[1], "保存失败")

    def test_toggle_pause_failure_keeps_enabled(self):
        self.seed([ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        replace_patch, sleep_patch = self.fail_writes()
        with replace_patch, sleep_patch, patch("deal_watch.ui.main.QMessageBox.critical"):
            self.window.toggle_region()
            self.pump_until(lambda: not self.window._saving)
        self.assertTrue(self.window.config.regions[0].enabled)
        self.assertEqual(self.table_states(), ["已就绪"])
        self.assertTrue(self.disk_regions()[0].enabled)

    def test_toggle_pause_success_updates_everywhere(self):
        self.seed([ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        self.window.toggle_region()
        self.pump_until(lambda: not self.window._saving)
        self.assertFalse(self.window.config.regions[0].enabled)
        self.assertEqual(self.table_states(), ["已暂停"])
        self.assertFalse(self.disk_regions()[0].enabled)

    def test_delete_failure_keeps_region(self):
        self.seed([ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        replace_patch, sleep_patch = self.fail_writes()
        with replace_patch, sleep_patch, patch(
            "deal_watch.ui.main.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch("deal_watch.ui.main.QMessageBox.critical"):
            self.window.delete_region()
            self.pump_until(lambda: not self.window._saving)
        self.assertEqual(len(self.window.config.regions), 1)
        self.assertEqual(self.window.region_table.rowCount(), 1)
        self.assertEqual(len(self.disk_regions()), 1)

    def test_delete_success_removes_only_after_saved(self):
        self.seed([ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        with patch(
            "deal_watch.ui.main.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.delete_region()
            self.pump_until(lambda: not self.window._saving)
        self.assertEqual(self.window.config.regions, [])
        self.assertEqual(self.window.region_table.rowCount(), 0)
        self.assertEqual(self.disk_regions(), [])

    def test_preview_confirm_failure_rolls_back_previewed(self):
        self.seed(
            [ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500), previewed=False)]
        )

        class FakePreviewDialog:
            def __init__(self, target, *_args, **_kwargs) -> None:
                self.region = target

            def exec(self) -> int:
                # 模拟用户点“确认此区域”：先只改内存对象。
                self.region.previewed = True
                return QDialog.DialogCode.Accepted

        replace_patch, sleep_patch = self.fail_writes()
        with replace_patch, sleep_patch, patch(
            "deal_watch.ui.main.PreviewDialog", FakePreviewDialog
        ), patch("deal_watch.ui.main.QMessageBox.critical"):
            self.window.preview_region()
            self.pump_until(lambda: not self.window._saving)
        self.assertFalse(self.window.config.regions[0].previewed)
        self.assertEqual(self.table_states(), ["待确认"])
        self.assertFalse(self.disk_regions()[0].previewed)

    def test_preview_confirm_success_persists_and_enables(self):
        self.seed(
            [ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500), previewed=False)]
        )

        class FakePreviewDialog:
            def __init__(self, target, *_args, **_kwargs) -> None:
                self.region = target

            def exec(self) -> int:
                self.region.previewed = True
                return QDialog.DialogCode.Accepted

        with patch("deal_watch.ui.main.PreviewDialog", FakePreviewDialog):
            self.window.preview_region()
            self.pump_until(lambda: not self.window._saving)
        self.assertTrue(self.window.config.regions[0].previewed)
        self.assertEqual(self.table_states(), ["已就绪"])
        self.assertTrue(self.disk_regions()[0].previewed)

    def test_start_monitoring_blocked_when_snapshot_fails(self):
        self.seed([ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        self.window.config.keywords = ["手机"]
        self.window.webhook = "http://127.0.0.1:18080/ok"
        replace_patch, sleep_patch = self.fail_writes()
        with replace_patch, sleep_patch, patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ) as critical:
            self.window.start_monitoring()
        self.assertIsNone(self.window.service)
        self.assertTrue(critical.called)
        self.assertIn("启动前", critical.call_args.args[2])

    def test_keywords_failure_keeps_old_keywords(self):
        self.window.config.keywords = ["手机"]
        self.window.config_store.save(self.window.config)
        self.window.keywords_edit.setPlainText("优惠券")
        replace_patch, sleep_patch = self.fail_writes()
        with replace_patch, sleep_patch, patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ) as critical:
            self.window.save_keywords()
        self.assertEqual(self.window.config.keywords, ["手机"])
        self.assertEqual(ConfigStore(self.window.data_dir).load().keywords, ["手机"])
        self.assertTrue(critical.called)


if __name__ == "__main__":
    unittest.main()
