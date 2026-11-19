"""QA 独立验证（0.2.2 阶段一 A/B 组）：区域保存可靠性与圈选可见性。

与工程师自测的构造方式刻意不同：

* 区域保存全部从 ``MainWindow`` 真实槽函数进入，故障注入落在**配置层**
  （``Path.write_text`` / ``Path.mkdir`` / ``os.replace`` / 回读校验），
  不在 ``region_save`` 上打桩，因此事务边界本身也被覆盖；
* 故障种类覆盖 ``PermissionError``（占用）、``ENOSPC``（磁盘满）、
  ``mkdir``（目录不可写）与 ``verify``（写后回读不一致），逐个断言
  “内存 = 磁盘 = 表格”；
* 圈选不使用 ``image_rect()`` 单一断言：用 ``grab()`` 做四边像素采样，
  并用带 ``devicePixelRatio`` 的 pixmap 验证高 DPI 下“画出来的”与
  “算出来的”指向同一块画面。

已知缺陷在文件末尾 ``KnownDefectTests``，失败即代表源码行为与设计不符。
"""

from __future__ import annotations

import errno
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox  # noqa: E402

from deal_watch.config import ConfigStore  # noqa: E402
from deal_watch.models import Rect, RegionConfig  # noqa: E402
from deal_watch.region_save import validate_region_config  # noqa: E402
from deal_watch.ui.main import PAUSE_FIRST_HINT, MainWindow  # noqa: E402
from deal_watch.ui.region import (  # noqa: E402
    RegionEditDialog,
    RegionSelectionDialog,
    SelectionLabel,
)
from region_fixtures import ready_region  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def pump(app: QApplication, condition, timeout: float = 8.0) -> bool:
    """在 offscreen 下推进事件循环，直到条件成立或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        app.processEvents()
        time.sleep(0.01)
    return condition()


class FakeRegionEditDialog:
    """替身：直接返回一个“已就绪但未预览确认”的区域。"""

    DialogCode = QDialog.DialogCode

    def __init__(self, _parent=None, existing: RegionConfig | None = None) -> None:
        self.existing = existing

    def exec(self) -> int:
        return QDialog.DialogCode.Accepted

    def region(self) -> RegionConfig:
        if self.existing is not None:
            return replace(
                self.existing,
                rect=Rect(10, 20, 360, 560),
                previewed=False,
            )
        return ready_region("新群", hwnd=2002, rect=Rect(10, 20, 400, 600), previewed=False)


class WindowFixture(unittest.TestCase):
    """每个用例一个干净的 ``%LOCALAPPDATA%`` 与一个真实 ``MainWindow``。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = self.make_window()

    def make_window(self) -> MainWindow:
        window = MainWindow(REPO_ROOT)
        window._initial_geometry_applied = True
        window.resize(1180, 780)
        window.show()
        self.app.processEvents()
        return window

    def shutdown(self, window: MainWindow) -> None:
        window.exit_app()
        self.assertTrue(pump(self.app, lambda: window._shutdown_done, 10))
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def tearDown(self):
        self.shutdown(self.window)
        self.environment.stop()
        self.temp.cleanup()

    # -- 断言辅助 ---------------------------------------------------------
    def seed(self, regions: list[RegionConfig]) -> None:
        """把区域同时写进内存与磁盘，保证起点满足不变量 I-1。"""
        self.window.config.regions = list(regions)
        self.window.config_store.save(self.window.config)
        self.window._load_regions()
        if regions:
            self.window.region_table.selectRow(0)

    def disk_regions(self) -> list[RegionConfig]:
        return ConfigStore(self.window.data_dir).load().regions

    def table_aliases(self) -> list[str]:
        return [
            self.window.region_table.item(row, 0).text()
            for row in range(self.window.region_table.rowCount())
        ]

    def table_states(self) -> list[str]:
        return [
            self.window.region_table.item(row, 1).text()
            for row in range(self.window.region_table.rowCount())
        ]

    def wait_save(self) -> None:
        self.assertTrue(pump(self.app, lambda: not self.window._saving), "保存没有在超时内结束")
        self.app.processEvents()

    def assert_invariant(self, expected: int) -> None:
        """不变量 I-1：内存 / 磁盘 / 表格三者行数必须一致。"""
        self.assertEqual(len(self.window.config.regions), expected, "内存条数与预期不符")
        self.assertEqual(len(self.disk_regions()), expected, "磁盘条数与预期不符")
        self.assertEqual(self.window.region_table.rowCount(), expected, "表格行数与预期不符")


class AddFourthRegionTests(WindowFixture):
    """A 组 1-3：第四个区域必须立刻出现在表格、被选中、可滚动到、重启仍在。"""

    def three_regions(self) -> list[RegionConfig]:
        return [
            ready_region(f"群{i}", hwnd=1000 + i, rect=Rect(0, 0, 300, 500)) for i in range(1, 4)
        ]

    def test_fourth_region_is_listed_selected_and_scrollable(self):
        self.seed(self.three_regions())
        with patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog):
            self.window.add_region()
            self.wait_save()
        self.assert_invariant(4)
        self.assertEqual(self.table_aliases(), ["群1", "群2", "群3", "新群"])
        # 用户最痛的点：没预览确认也必须显示，状态必须是“待确认”。
        self.assertEqual(self.table_states()[-1], "待确认")
        self.assertEqual(self.window.region_table.currentRow(), 3)
        selected = [index.row() for index in self.window.region_table.selectionModel().selectedRows()]
        self.assertEqual(selected, [3], "新增行没有被选中")
        item = self.window.region_table.item(3, 0)
        visual = self.window.region_table.visualItemRect(item)
        viewport = self.window.region_table.viewport().rect()
        self.assertTrue(visual.isValid(), "新增行没有可测的可见矩形")
        self.assertTrue(
            viewport.contains(visual), f"新增行不在可见区域内：{visual} vs {viewport}"
        )
        self.assertIn("已保存，待确认", self.window.statusBar().currentMessage())

    def test_unpreviewed_region_saved_but_still_gated_for_monitoring(self):
        region = ready_region("未确认群", hwnd=2003, rect=Rect(0, 0, 300, 500), previewed=False)
        self.window._request_region_save("add", region)
        self.wait_save()
        self.assert_invariant(1)
        self.assertEqual(self.table_states(), ["待确认"])
        self.assertFalse(self.window.config.regions[0].previewed)
        # 预览门禁只管“能否监控”，不管“能否保存”：结构校验必须通过。
        self.assertEqual(validate_region_config(self.window.config.regions[0]), [])

    def test_fourth_region_survives_restart(self):
        self.seed(self.three_regions())
        with patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog):
            self.window.add_region()
            self.wait_save()
        self.assert_invariant(4)
        # 真实重启路径：关闭当前窗口后重新构造 MainWindow，读的是同一个 config.json。
        self.shutdown(self.window)
        self.window = self.make_window()
        self.assertEqual(len(self.window.config.regions), 4, "重启后第 4 个区域消失了")
        self.assertEqual(self.window.region_table.rowCount(), 4)
        self.assertEqual(self.table_aliases()[3], "新群")
        self.assertEqual(self.table_states()[3], "待确认")

    def test_fifth_region_is_saved_without_a_fixed_limit(self):
        self.seed(self.three_regions() + [ready_region("群4", hwnd=1004, rect=Rect(0, 0, 300, 500))])
        with patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog):
            self.window.add_region()
            self.wait_save()
        self.assert_invariant(5)


class SaveFailureConsistencyTests(WindowFixture):
    """A 组 4：四种写盘故障下“内存 = 磁盘 = 表格”，且按钮与保存标志复位。"""

    def faults(self) -> dict:
        return {
            "replace-PermissionError": patch(
                "deal_watch.config.os.replace",
                side_effect=PermissionError("[WinError 5] 拒绝访问"),
            ),
            "write-ENOSPC": patch(
                "deal_watch.config.Path.write_text",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ),
            "mkdir-PermissionError": patch(
                "deal_watch.config.Path.mkdir", side_effect=PermissionError("目录不可写")
            ),
        }

    def test_each_write_fault_keeps_all_three_snapshots_consistent(self):
        self.seed(
            [ready_region(f"群{i}", hwnd=1000 + i, rect=Rect(0, 0, 300, 500)) for i in range(1, 4)]
        )
        for name, patcher in self.faults().items():
            with self.subTest(fault=name):
                with patcher, patch(
                    "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
                ), patch("deal_watch.ui.main.QMessageBox.critical") as critical, patch(
                    "deal_watch.config.time.sleep"
                ):
                    self.window.add_region()
                    self.wait_save()
                self.assert_invariant(3)
                self.assertTrue(critical.called, f"{name}：保存失败却没有提示")
                self.assertEqual(critical.call_args.args[1], "保存失败")
                self.assertFalse(self.window._saving)
                self.assertTrue(
                    all(button.isEnabled() for button in self.window.configuration_buttons),
                    f"{name}：失败后配置按钮没有恢复可用",
                )
                self.assertIn("保存失败", self.window.statusBar().currentMessage())

    def test_retry_after_transient_fault_succeeds_and_stays_consistent(self):
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        calls = {"count": 0}
        real_replace = os.replace

        def flaky_replace(source, target):
            calls["count"] += 1
            if calls["count"] <= 2:  # 两次尝试都失败：ConfigStore 内部重试一次
                raise PermissionError("[WinError 5] 拒绝访问")
            return real_replace(source, target)

        with patch("deal_watch.config.os.replace", side_effect=flaky_replace), patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ), patch("deal_watch.ui.main.QMessageBox.critical"), patch("deal_watch.config.time.sleep"):
            self.window.add_region()
            self.wait_save()
        self.assert_invariant(1)
        # 故障消失后重试一次：不需要重开程序，结果必须完全一致。
        with patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog):
            self.window.add_region()
            self.wait_save()
        self.assert_invariant(2)
        self.assertEqual(len(self.disk_regions()), 2)

    def test_double_click_starts_only_one_save_and_never_duplicates_rows(self):
        self.seed(
            [ready_region(f"群{i}", hwnd=1000 + i, rect=Rect(0, 0, 300, 500)) for i in range(1, 4)]
        )
        started: list[int] = []

        def slow(*_args, **_kwargs):
            started.append(1)
            time.sleep(0.4)
            from deal_watch.region_save import commit_region_change as real

            return real(*_args, **_kwargs)

        with patch("deal_watch.ui.main.commit_region_change", side_effect=slow), patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ), patch("deal_watch.ui.main.QMessageBox.information") as information:
            self.window.add_region()
            self.window.add_region()  # 用户连点第二次
            self.wait_save()
        self.assertEqual(len(started), 1, "连点两次发起了两次保存")
        self.assertTrue(information.called)
        self.assert_invariant(4)
        self.assertEqual(self.table_aliases().count("新群"), 1)

    def test_validation_failure_keeps_all_three_snapshots(self):
        """校验失败（未通过 region_save 复核）同样不得改动内存/磁盘/表格。"""
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])

        class DuplicateAliasDialog(FakeRegionEditDialog):
            def region(self) -> RegionConfig:
                return ready_region("群1", hwnd=3003, rect=Rect(0, 0, 300, 500), previewed=False)

        with patch("deal_watch.ui.main.RegionEditDialog", DuplicateAliasDialog), patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ) as critical:
            self.window.add_region()
            self.wait_save()
        self.assert_invariant(1)
        self.assertTrue(critical.called)
        self.assertIn("同名", critical.call_args.args[2])
        self.assertIn("validate", self.window.statusBar().currentMessage())

    def test_user_cancel_changes_nothing(self):
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])

        class CancelDialog(FakeRegionEditDialog):
            def exec(self) -> int:
                return QDialog.DialogCode.Rejected

        with patch("deal_watch.ui.main.RegionEditDialog", CancelDialog):
            self.window.add_region()
        self.assertFalse(self.window._saving, "用户取消后仍进入了保存流程")
        self.assert_invariant(1)

    def test_preview_confirm_failure_rolls_back_previewed(self):
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500), previewed=False)])

        class ConfirmPreview:
            def __init__(self, target, *_args, **_kwargs) -> None:
                self.region = target

            def exec(self) -> int:
                self.region.previewed = True  # 模拟用户点“确认此区域”
                return QDialog.DialogCode.Accepted

        with patch(
            "deal_watch.config.Path.write_text",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), patch("deal_watch.ui.main.PreviewDialog", ConfirmPreview), patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ), patch("deal_watch.config.time.sleep"):
            self.window.preview_region()
            self.wait_save()
        self.assertFalse(self.window.config.regions[0].previewed, "预览确认失败后 previewed 没回滚")
        self.assertEqual(self.table_states(), ["待确认"])
        self.assertFalse(self.disk_regions()[0].previewed)
        self.assert_invariant(1)

    def test_toggle_and_delete_failures_rollback_with_enospc(self):
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        with patch(
            "deal_watch.config.Path.write_text",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), patch("deal_watch.ui.main.QMessageBox.critical"), patch("deal_watch.config.time.sleep"):
            self.window.toggle_region()
            self.wait_save()
            self.assertTrue(self.window.config.regions[0].enabled, "暂停失败后 enabled 没有回滚")
            self.assertEqual(self.table_states(), ["已就绪"])
            self.assert_invariant(1)
            with patch(
                "deal_watch.ui.main.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                self.window.delete_region()
                self.wait_save()
        self.assertEqual(len(self.window.config.regions), 1, "删除失败却从内存删掉了区域")
        self.assert_invariant(1)


class GateTests(WindowFixture):
    """A 组 5：运行中 / 正在停止时的入口闸门。"""

    def test_add_is_refused_while_monitoring(self):
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
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
        self.assert_invariant(1)

    def test_all_entries_refused_while_stop_is_in_progress(self):
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        self.window._stopping_service = object()
        try:
            with patch("deal_watch.ui.main.RegionEditDialog") as dialog, patch(
                "deal_watch.ui.main.QMessageBox.information"
            ) as information:
                self.window.add_region()
                self.window.edit_region()
                self.window.delete_region()
            dialog.assert_not_called()
            self.assertEqual(information.call_count, 3)
            for call in information.call_args_list:
                self.assertEqual(call.args[2], PAUSE_FIRST_HINT)
        finally:
            self.window._stopping_service = None

    def test_recalibrate_while_monitoring_stops_first_then_saves(self):
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        order: list[str] = []

        class FakeService:
            def request_stop(self):
                order.append("request_stop")

            def stop(self):
                order.append("stop_done")

            def is_alive(self):
                return False

        self.window.service = FakeService()

        def record(*_args, **_kwargs):
            order.append("save")
            from deal_watch.region_save import commit_region_change as real

            return real(*_args, **_kwargs)

        with patch("deal_watch.ui.main.commit_region_change", side_effect=record), patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ):
            self.window.edit_region()
            self.assertTrue(pump(self.app, lambda: "save" in order))
        self.assertEqual(order[0], "request_stop")
        self.assertLess(order.index("stop_done"), order.index("save"))
        self.assert_invariant(1)


# ---------------------------------------------------------------------------
# B 组：圈选可见性
# ---------------------------------------------------------------------------

FRAME_SIZE = (1000, 600)
WIDGET_SIZE = (700, 700)
TARGET = QRect(0, 140, 700, 420)  # 1000×600 等比放进 700×700 后的绘制区域


def make_quadrant_pixmap(dpr: float = 1.0) -> QPixmap:
    """四象限彩图：用于验证“画出来的像素”与“算出来的坐标”是否一致。"""
    image = QImage(FRAME_SIZE[0], FRAME_SIZE[1], QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.fillRect(QRect(0, 0, 500, 300), QColor("#ff0000"))
    painter.fillRect(QRect(500, 0, 500, 300), QColor("#00ff00"))
    painter.fillRect(QRect(0, 300, 500, 300), QColor("#0000ff"))
    painter.fillRect(QRect(500, 300, 500, 300), QColor("#ffff00"))
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


class SelectionVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def make_label(self, dpr: float = 1.0) -> SelectionLabel:
        label = SelectionLabel(make_quadrant_pixmap(dpr))
        label.resize(*WIDGET_SIZE)
        return label

    def drag(self, label: SelectionLabel, start: QPoint, end: QPoint) -> None:
        QTest.mousePress(label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(label, end)
        QTest.mouseRelease(label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)

    def test_released_selection_is_actually_painted_on_all_four_edges(self):
        """B 组 7：松手后用像素采样证明边框与填充仍在（不是只看属性）。"""
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        self.assertEqual(label.selection_image_rect(), Rect(100, 100, 500, 300))
        shot = label.grab().toImage()
        border = QColor("#175cd3")
        # 四条边各取一个点：必须被 2px 蓝色边框覆盖。
        for point in (
            QPoint(200, 210),
            QPoint(200, 419),
            QPoint(70, 300),
            QPoint(419, 300),
        ):
            colour = shot.pixelColor(point)
            self.assertLess(
                abs(colour.red() - border.red())
                + abs(colour.green() - border.green())
                + abs(colour.blue() - border.blue()),
                120,
                f"{point} 不是选区边框颜色，实际 {colour.name()}",
            )
        # 内部：半透明蓝色填充（白底 + alpha 64）。
        inside = shot.pixelColor(300, 400)
        self.assertGreater(inside.blue(), inside.red(), "选区内没有蓝色填充")
        self.assertLess(inside.red(), 240, "选区内仍是纯白，说明没有绘制填充")
        # 图像范围内但选区外：仍是原图（该点位于绿色象限）。
        outside = shot.pixelColor(560, 200)
        self.assertGreater(outside.green(), 200)
        self.assertLess(outside.red(), 80)

    def test_four_drag_directions_give_identical_roi_and_pixels(self):
        """B 组 8：四个方向得到同一 ROI，且画面像素一致（统一差 1px 的取舍要一致）。"""
        label = self.make_label()
        results = []
        for start, end in (
            (QPoint(70, 210), QPoint(420, 420)),
            (QPoint(420, 420), QPoint(70, 210)),
            (QPoint(420, 210), QPoint(70, 420)),
            (QPoint(70, 420), QPoint(420, 210)),
        ):
            label.clear_selection()
            self.drag(label, start, end)
            results.append((label.selection_image_rect(), label.grab().toImage().pixelColor(300, 400)))
        first_rect = results[0][0]
        for rect, colour in results[1:]:
            self.assertEqual(rect, first_rect, "不同拖动方向的 ROI 不一致")
            self.assertEqual(colour.name(), results[0][1].name(), "不同方向绘制出的画面不一致")

    def test_reselect_replaces_and_blank_or_tiny_drag_is_rejected_with_reason(self):
        """B 组 9：重选替换、点击留白不生成、过小被拒绝且有说明。"""
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        self.drag(label, QPoint(100, 250), QPoint(300, 400))
        sx = FRAME_SIZE[0] / TARGET.width()
        sy = FRAME_SIZE[1] / TARGET.height()
        self.assertEqual(
            label.selection_image_rect(),
            Rect(
                round((100 - TARGET.x()) * sx),
                round((250 - TARGET.y()) * sy),
                round(200 * sx),
                round(150 * sy),
            ),
        )
        # 留白（图像上方）不生成选区，并给出可读提示。
        self.drag(label, QPoint(200, 40), QPoint(400, 100))
        self.assertIsNone(label.selection_image_rect())
        self.assertIn("画面", label.hint())
        # 过小：显式取消 + 说明原因。
        label.clear_selection()
        self.drag(label, QPoint(300, 300), QPoint(302, 302))
        self.assertIsNone(label.selection_image_rect())
        self.assertIn("过小", label.hint())

    def test_resize_and_high_dpi_keep_the_same_frame_pixels(self):
        """B 组 10：改控件尺寸、模拟 DPI 缩放后，同一选区映射到同一帧内像素。"""
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        before = label.selection_image_rect()
        for size in ((700, 700), (1100, 760), (420, 300), (700, 700)):
            label.resize(*size)
            self.app.processEvents()
            self.assertEqual(label.selection_image_rect(), before, f"{size} 下选区真值被改了")
            self.assertEqual(label.image_rect(), QRect(100, 100, 500, 300))
        # 高 DPI：pixmap 带 devicePixelRatio，绘制出的象限位置必须与映射一致。
        for dpr in (1.0, 1.25, 1.5):
            scaled = self.make_label(dpr)
            shot = scaled.grab().toImage()
            for name, (fx, fy) in {
                "左上红": (250, 150),
                "右上绿": (750, 150),
                "左下蓝": (250, 450),
                "右下黄": (750, 450),
            }.items():
                wx = TARGET.x() + round(fx * TARGET.width() / FRAME_SIZE[0])
                wy = TARGET.y() + round(fy * TARGET.height() / FRAME_SIZE[1])
                colour = shot.pixelColor(wx, wy)
                self.assertGreater(colour.saturation(), 200, f"dpr={dpr} {name} 未按帧坐标绘制")

    def test_resize_does_not_move_the_painted_selection(self):
        label = self.make_label()
        self.drag(label, QPoint(70, 210), QPoint(420, 420))
        label.resize(1100, 760)
        self.app.processEvents()
        rect = label.selection_image_rect()
        widget_rect = label.selected
        shot = label.grab().toImage()
        self.assertGreater(shot.pixelColor(widget_rect.left(), widget_rect.center().y()).blue(), 150)
        self.assertGreater(shot.pixelColor(widget_rect.center().x(), widget_rect.top()).blue(), 150)
        self.assertEqual(rect, Rect(100, 100, 500, 300))


class FakeWindow:
    def __init__(self, hwnd: int, title: str) -> None:
        self.hwnd = hwnd
        self.title = title
        self.class_name = "WeChatMainWndForPC"
        self.minimized = False
        self.process_id = 4321
        self.process_path = "C:/Program Files/WeChat/WeChat.exe"
        self.rect = Rect(0, 0, 800, 1280)


class FakeWindows:
    def __init__(self, windows: list[FakeWindow]) -> None:
        self._windows = windows

    def list_windows(self) -> list[FakeWindow]:
        return list(self._windows)

    def window_from_hwnd(self, hwnd: int):
        return next((item for item in self._windows if item.hwnd == hwnd), None)


class SelectionInvalidationTests(unittest.TestCase):
    """B 组 11：切换窗口/后端后旧选区失效、旧异步结果被丢弃。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.windows = [FakeWindow(101, "群 A"), FakeWindow(202, "群 B")]
        self.patcher = patch(
            "deal_watch.ui.region.WindowsWindows", return_value=FakeWindows(self.windows)
        )
        self.patcher.start()
        self.dialog = RegionEditDialog()
        self.dialog.alias.setText("群 B")
        self.dialog.window_combo.setCurrentIndex(1)

    def tearDown(self):
        self.patcher.stop()
        self.dialog.deleteLater()
        self.app.processEvents()

    def complete(self, generation: int, hwnd: int, backend: str, window) -> None:
        import numpy as np

        self.dialog._on_selection_completed(
            generation, hwnd, backend, window,
            np.full((1280, 800, 3), 245, dtype=np.uint8), 800, 1280, "",
        )
        if self.dialog.selector is not None:
            self.dialog.selector.set_widget_selection(self.dialog.selector._target())

    def test_late_result_from_previous_generation_is_discarded(self):
        stale = self.dialog._selection_generation
        self.dialog._invalidate_selection()  # 模拟切换窗口/后端
        self.complete(stale, 202, "wgc", self.windows[1])
        self.assertIsNone(self.dialog.selected_rect, "过期异步结果被采纳了")
        self.assertFalse(self.dialog.ok_button.isEnabled())
        # 当前代数的结果必须被采纳（证明上面的丢弃不是“永远不采纳”）。
        self.complete(self.dialog._selection_generation, 202, "wgc", self.windows[1])
        self.assertIsNotNone(self.dialog.selected_rect)

    def test_backend_change_during_capture_rejects_the_result(self):
        self.dialog.backend_combo.setCurrentIndex(1)
        # 切换后端会让代数 +1；这里用“当前代数”发起一次采集，模拟采集期间换了后端，
        # 只有 hwnd/后端的二次核对能拦住它（代数核对会先放行）。
        generation = self.dialog._selection_generation
        with patch("deal_watch.ui.region.QMessageBox.warning") as warning:
            self.complete(generation, 202, "wgc", self.windows[1])
        warning.assert_not_called()
        self.assertIn("变化", self.dialog.capture_status.text())
        self.assertIsNone(self.dialog.selected_rect)
        self.assertFalse(self.dialog.ok_button.isEnabled())
        self.assertIn("重新圈选", self.dialog.selection_label.text())

    def test_selection_problems_reports_missing_calibration_after_backend_switch(self):
        self.complete(self.dialog._selection_generation, 202, "wgc", self.windows[1])
        self.assertEqual(self.dialog.selection_problems(), [])
        self.dialog.backend_combo.setCurrentIndex(1)
        problems = self.dialog.selection_problems()
        self.assertTrue(problems)
        self.assertTrue(any("圈选" in item for item in problems))


class SelectionDialogLayoutTests(unittest.TestCase):
    """B 组 12：说明文字换行、弹窗受可用屏幕约束、按钮不出屏。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_dialog(self) -> RegionSelectionDialog:
        import numpy as np

        image = np.full((1280, 800, 3), 255, dtype=np.uint8)
        dialog = RegionSelectionDialog(FakeWindow(101, "合成群"), image, 800, 1280)
        dialog.layout().activate()
        return dialog

    def test_hint_wraps_and_buttons_stay_inside_the_dialog(self):
        from PySide6.QtGui import QGuiApplication

        dialog = self.make_dialog()
        try:
            hints = [label for label in dialog.findChildren(QLabel) if "只圈选" in label.text()]
            self.assertTrue(hints, "没有找到说明文字")
            self.assertTrue(hints[0].wordWrap(), "说明文字没有开换行，关键指引会被截断")
            self.assertTrue(dialog.coordinates.wordWrap())
            available = QGuiApplication.primaryScreen().availableGeometry()
            self.assertLessEqual(dialog.width(), max(240, int(available.width() * 0.9)))
            self.assertLessEqual(dialog.height(), max(180, int(available.height() * 0.9)))
            self.assertLessEqual(
                dialog.buttons.geometry().bottom(), dialog.height(), "按钮组落到了弹窗外面"
            )
            self.assertGreaterEqual(dialog.selector.width(), dialog.selector.minimumWidth())
            self.assertFalse(dialog.ok_button.isEnabled(), "没有选区时保存按钮必须禁用")
        finally:
            dialog.deleteLater()

    def test_three_distinct_reasons_are_explained(self):
        dialog = self.make_dialog()
        try:
            self.assertIn("尚未选择区域", dialog._missing_reason(""))
            self.assertIn("过小", dialog._missing_reason("拖动距离过小（需大于 4 像素），已取消选区"))
            self.assertTrue(dialog.rect_problems(Rect(0, 0, 5, 5)))
            self.assertIn("过小", " ".join(dialog.rect_problems(Rect(0, 0, 5, 5))))
            self.assertIn("超出", " ".join(dialog.rect_problems(Rect(700, 1200, 300, 300))))
            self.assertEqual(dialog.rect_problems(Rect(10, 20, 400, 600)), [])
        finally:
            dialog.deleteLater()


# ---------------------------------------------------------------------------
# 已知缺陷：以下用例断言的是设计/PRD 要求的行为，当前实现不满足。
# 失败不是测试写错，而是缺陷证据（见交付报告）。
# ---------------------------------------------------------------------------


class KnownDefectTests(WindowFixture):
    def test_rename_cannot_create_two_indistinguishable_aliases(self):
        """D1：同名别名只在“新增”拦截，改 名可以造出两个同名区域。

        设计交接里明确要求确认“不会让用户存进两份无法区分的区域”。当前
        ``region_save.commit_region_change`` 对 update 传
        ``check_duplicate_alias=False``（region_save.py:216-218），因此把
        群B 改名成群A 会被接受，表格出现两行“群A”。
        """
        first = ready_region("群A", hwnd=1001, rect=Rect(0, 0, 300, 500))
        second = ready_region("群B", hwnd=1002, rect=Rect(0, 0, 300, 500))
        self.seed([first, second])
        self.window.region_table.selectRow(1)

        class RenameDialog(FakeRegionEditDialog):
            def region(self) -> RegionConfig:
                return replace(self.existing, alias="群A")

        with patch("deal_watch.ui.main.RegionEditDialog", RenameDialog), patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ) as critical:
            self.window.edit_region()
            self.wait_save()
        aliases = [region.alias for region in self.window.config.regions]
        self.assertEqual(
            aliases,
            ["群A", "群B"],
            f"改名后出现了无法区分的同名区域：{aliases}（提示：{critical.call_args}）",
        )

    def test_failed_add_keeps_the_configured_region_for_retry(self):
        """D2：保存失败后“编辑内容保留、可重试”没有兑现。

        设计 3.1 T1.2 验收要求“界面保留编辑内容 / 重试一次成功后一切一致”，
        4.1 时序也写“critical 保存失败 可重试 原编辑内容保留”。当前
        ``_on_region_save_failed``（ui/main.py:630-652）清空 ``_pending_save``
        且圈选对话框已关闭，用户即使只遇到杀软瞬时占用，也必须重新采集窗口
        并重新拖动选区——状态栏却写着“可直接重试”。
        """
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        with patch(
            "deal_watch.config.os.replace", side_effect=PermissionError("[WinError 5] 拒绝访问")
        ), patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog), patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ), patch("deal_watch.config.time.sleep"):
            self.window.add_region()
            self.wait_save()
        self.assert_invariant(1)
        pending = getattr(self.window, "_pending_save", None)
        self.assertIsNotNone(
            pending, "保存失败后没有保留待保存区域，用户必须重新圈选才能重试"
        )

    def test_verify_failure_does_not_claim_nothing_was_written(self):
        """D3：回读校验失败时文件已经被替换，界面却声称“配置没有写入”。

        真实触发：写入后、回读前文件被同步盘/杀软改写（H1 的变体）。此时
        ``config.json`` 已含新区域，内存与表格仍是旧快照，重启后区域“突然出现”，
        与用户最初的“重启后消失”正好相反。属于不变量 I-1 的例外状态，
        提示文案必须如实说明“已写入但校验失败，请重启后核对”。
        """
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        with patch(
            "deal_watch.config.ConfigStore._verify",
            side_effect=Exception("回读内容与写入内容不一致"),
        ), patch("deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog), patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ) as critical, patch("deal_watch.config.time.sleep"):
            self.window.add_region()
            self.wait_save()
        disk = len(self.disk_regions())
        message = critical.call_args.args[2] if critical.called else ""
        self.assertEqual(
            disk,
            len(self.window.config.regions),
            f"磁盘 {disk} 条 / 内存 {len(self.window.config.regions)} 条不一致，"
            f"但提示写的是：{message}",
        )


if __name__ == "__main__":
    unittest.main()
