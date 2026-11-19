"""0.2.3 E2：主窗口缩放与页签宽度约束回归（离屏，**正式样式**）。

0.2.2 的现象是“窗口横向缩不下去”：``resize(1180, 780)`` 之后没有设最小尺寸，
真正的最小宽度由五个页签里不换行的长文本撑出来（实测 2180 逻辑像素）。这些文本
大多在**隐藏页签**里，所以只改“监控区域”页无效。

本文件固定三件事：

1. 任何页签都不能再把主窗口最小宽度顶大（切页签不改变窗口宽度）；
2. 760×520 下关键按钮与状态都在窗口可见范围内；
3. 默认 1000×720，且首次显示按所在屏幕可用区收缩。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget  # noqa: E402

from deal_watch.models import Rect  # noqa: E402
from deal_watch.ui.main import MainWindow  # noqa: E402
from deal_watch.ui.region_list import RegionColumn  # noqa: E402
from deal_watch.ui.shell import PageId  # noqa: E402
from deal_watch.ui.theme import APP_STYLE  # noqa: E402
from region_fixtures import ready_region  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MIN_WIDTH, MIN_HEIGHT = 760, 520


class LayoutTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            for name in ("segoeui.ttf", "seguisb.ttf", "msyh.ttc", "msyhbd.ttc"):
                font_path = Path("C:/Windows/Fonts") / name
                if font_path.exists():
                    QFontDatabase.addApplicationFont(str(font_path))
        cls.app.setQuitOnLastWindowClosed(False)
        # 与入口完全同一份样式：QSS 的 padding 会改变控件最小提示。
        cls.app.setStyleSheet(APP_STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.window = MainWindow(REPO_ROOT)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.exit_app()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.window._shutdown_done:
            self.app.processEvents()
            time.sleep(0.01)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.env.stop()
        self.temp.cleanup()

    # -- 辅助 -------------------------------------------------------------
    def pages(self) -> list[tuple[str, QWidget, int]]:
        return [
            (self.window.tabs.tabText(index), self.window.tabs.widget(index), index)
            for index in range(self.window.tabs.count())
        ]

    def visible_in_window(self, widget: QWidget) -> bool:
        rect = widget.rect()
        if rect.isEmpty() or not widget.isVisible():
            return False
        top_left = widget.mapTo(self.window, rect.topLeft())
        bottom_right = widget.mapTo(self.window, rect.bottomRight())
        return (
            top_left.x() >= 0
            and top_left.y() >= 0
            and bottom_right.x() < self.window.width()
            and bottom_right.y() < self.window.height()
        )


class MinimumSizeTests(LayoutTestCase):
    def test_declared_minimum_size_and_default_size(self):
        self.assertEqual(self.window.minimumWidth(), MIN_WIDTH)
        self.assertEqual(self.window.minimumHeight(), MIN_HEIGHT)
        # 离屏屏幕比默认尺寸小，首次显示会按可用区收缩，因此只断言“不超过默认值”。
        self.assertLessEqual(self.window.width(), 1000)
        self.assertLessEqual(self.window.height(), 720)

    def test_no_tab_forces_the_window_wider_than_the_minimum(self):
        for title, page, _index in self.pages():
            hint = page.minimumSizeHint()
            self.assertLessEqual(
                hint.width(),
                MIN_WIDTH,
                f"页签“{title}”的最小提示宽度 {hint.width()} 超过最小窗口宽度，窗口缩不下去",
            )

    def test_switching_tabs_does_not_change_window_width(self):
        self.window.resize(MIN_WIDTH, MIN_HEIGHT)
        self.app.processEvents()
        baseline = self.window.width()
        self.assertEqual(baseline, MIN_WIDTH, "窗口无法缩到声明的最小宽度")
        for title, _page, index in self.pages():
            self.window.navigate(PageId(index))
            self.app.processEvents()
            self.assertEqual(
                self.window.width(), baseline, f"切到“{title}”后主窗口被撑宽了"
            )

    def test_layout_shrinks_at_three_reference_sizes(self):
        self.assertFalse(self.window.shell.controls.isVisible(), "空列表只突出添加入口")
        self.window.config.regions = [ready_region("合成区域")]
        self.window._load_regions()
        for width, height in ((MIN_WIDTH, MIN_HEIGHT), (900, 600), (1180, 780)):
            self.window.resize(width, height)
            self.app.processEvents()
            self.assertLessEqual(self.window.width(), max(width, MIN_WIDTH))
            self.assertGreaterEqual(self.window.width(), MIN_WIDTH)
            # 导航与当前主操作均可达；互斥的开始/暂停和无草稿的重试不占空间。
            self.assertTrue(self.visible_in_window(self.window.shell.rail))
            self.assertTrue(self.visible_in_window(self.window.start_button), "开始监控按钮被裁掉")
            self.assertFalse(self.window.stop_button.isVisible())
            self.assertFalse(self.window.retry_button.isVisible())
            with patch.object(self.window, "_retry_ready", return_value=True):
                self.window._set_saving(False)
                self.app.processEvents()
                self.assertTrue(self.visible_in_window(self.window.retry_button), "失败草稿的重试入口被裁掉")
            self.window._set_saving(False)


class RegionPageLayoutTests(LayoutTestCase):
    def test_region_actions_are_all_reachable_at_minimum_size(self):
        self.window.resize(MIN_WIDTH, MIN_HEIGHT)
        self.app.processEvents()
        for button in self.window.configuration_buttons:
            self.assertTrue(
                self.visible_in_window(button), f"“{button.text()}”在最小尺寸下不可见"
            )
        self.assertTrue(self.visible_in_window(self.window.region_summary))
        self.assertTrue(self.visible_in_window(self.window.save_status))

    def test_region_row_actions_remain_keyboard_reachable(self):
        region = ready_region("合成区域")
        self.window.config.regions = [region]
        self.window._load_regions()
        self.window.resize(MIN_WIDTH, MIN_HEIGHT)
        self.app.processEvents()
        table = self.window.region_table
        table.selectRow(0)
        table.setFocus()
        with patch.object(self.window, "preview_region") as preview:
            QTest.keyClick(table, Qt.Key.Key_Return)
            preview.assert_called_once_with()
        from PySide6.QtWidgets import QMenu
        shown = []
        class Menu(QMenu):
            def exec(self, *_args):
                shown.append([action.text() for action in self.actions()])
        with patch("deal_watch.ui.main.QMenu", Menu):
            QTest.keyClick(table, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(shown)
        for label in ("查看详情", "暂停此区域", "重新校准", "删除区域"):
            self.assertIn(label, shown[0])
        cell = table.visualRect(table.model().index(0, RegionColumn.ACTIONS))
        for action in table.delegate.action_rects(cell):
            self.assertTrue(table.viewport().rect().contains(action))

    def test_region_summary_shows_count_without_limit(self):
        regions = [
            ready_region(f"群{i}", hwnd=1000 + i, rect=Rect(0, 0, 300, 500)) for i in range(1, 4)
        ]
        self.window.config.regions = regions
        self.window._load_regions()
        self.assertIn("已配置 3", self.window.region_summary.text())
        self.assertNotIn("待确认", self.window.region_summary.text())
        regions[0].previewed = False
        self.window._load_regions()
        self.assertIn("待确认 1", self.window.region_summary.text())

    def test_four_regions_do_not_display_a_quota(self):
        regions = [
            ready_region(f"群{i}", hwnd=1000 + i, rect=Rect(0, 0, 300, 500)) for i in range(1, 5)
        ]
        self.window.config.regions = regions
        self.window._load_regions()
        self.assertIn("已配置 4", self.window.region_summary.text())
        self.assertNotIn("上限", self.window.region_summary.text())


class TextConstraintTests(LayoutTestCase):
    def test_long_paths_and_hints_do_not_raise_minimum_width(self):
        for title, page, _index in self.pages():
            for label in page.findChildren(QLabel):
                if not label.wordWrap() or "配置路径" in label.text() or "运行数据" in label.text():
                    continue
                self.assertLess(
                    label.minimumSizeHint().width(),
                    MIN_WIDTH,
                    f"页签“{title}”的说明文字仍然按整行宽度计算最小尺寸",
                )

    def test_keyword_rule_is_brief_by_default_with_complete_disclosure(self):
        self.window.navigate(PageId.KEYWORDS)
        self.app.processEvents()
        page = self.window.pages.widget(PageId.KEYWORDS)
        from PySide6.QtWidgets import QTabWidget
        page.findChild(QTabWidget).setCurrentIndex(1)
        self.app.processEvents()
        labels = page.findChildren(QLabel)
        self.assertTrue(any(label.isVisible() and label.text() == "同条消息内按字符匹配。" for label in labels))
        details = next(label for label in labels if "不跨消息" in label.text())
        self.assertFalse(details.isVisible(), "完整规则不应常驻挤占编辑区域")
        button = next(button for button in page.findChildren(QPushButton) if button.text() == "规则说明")
        button.click()
        self.app.processEvents()
        self.assertTrue(details.isVisible())
        self.assertIn("按字符计数", details.text())
        self.assertNotIn("任一字面包含即命中", details.text())

    def test_status_bar_does_not_carry_the_data_path(self):
        message = self.window.statusBar().currentMessage()
        self.assertNotIn(str(self.window.data_dir), message, "状态栏仍在显示完整数据路径")


if __name__ == "__main__":
    unittest.main()
