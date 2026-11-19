"""0.2.3 E1：添加 / 重新校准的**真实对话框生命周期**回归。

0.2.2 之前的添加流程测试全部用 ``patch("deal_watch.ui.main.RegionEditDialog", ...)``
替换掉真实对话框，因此“开始圈选时把正在 ``exec()`` 的对话框隐藏掉、导致外层
``exec()`` 提前返回取消”这一整类缺陷不可能被发现。本文件只替换两件与环境相关
的东西：

* 窗口枚举（``WindowsWindows``）——用合成窗口信息；
* 界外采集（``SelectionCaptureWorker``）——用合成帧，可控延迟与失败。

其余全部是真实对象：真实 ``MainWindow``、真实 ``RegionEditDialog``、内联
``SelectionLabel``、真实按钮连接与真实外层模态事件循环。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import numpy as np  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402

from deal_watch.config import ConfigStore  # noqa: E402
from deal_watch.models import Rect, RegionConfig  # noqa: E402
from deal_watch.ui import region as region_ui  # noqa: E402
from deal_watch.ui.main import MainWindow  # noqa: E402
from region_fixtures import ready_region  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAME_WIDTH, FRAME_HEIGHT = 800, 1280


class FakeWindow:
    """合成窗口信息：字段与 ``capture.WindowInfo`` 对齐。"""

    def __init__(self, hwnd: int, title: str) -> None:
        self.hwnd = hwnd
        self.title = title
        self.class_name = "WeChatMainWndForPC"
        self.minimized = False
        self.process_id = 4321
        self.process_path = "C:/Program Files/WeChat/WeChat.exe"
        self.rect = Rect(0, 0, FRAME_WIDTH, FRAME_HEIGHT)


class FakeWindows:
    def __init__(self, windows: list[FakeWindow]) -> None:
        self._windows = windows

    def list_windows(self) -> list[FakeWindow]:
        return list(self._windows)

    def window_from_hwnd(self, hwnd: int):
        return next((item for item in self._windows if item.hwnd == hwnd), None)


class FakeSelectionWorker(QObject):
    """替身采集：在事件循环里异步回一次合成帧（可控延迟 / 失败）。"""

    completed = Signal(object, object, int, int, str)

    windows: dict[int, FakeWindow] = {}
    delay_ms = 0
    #: 每次新建 worker 依次弹出一个错误；空列表表示全部成功。
    fail_plan: list[str] = []

    def __init__(self, hwnd: int, backend: str, parent=None) -> None:
        super().__init__(parent)
        self.hwnd = hwnd
        self.backend = backend
        self._delay = int(type(self).delay_ms)
        plan = type(self).fail_plan
        self._error = plan.pop(0) if plan else ""
        self._running = False

    # 与 QThread 的最小兼容面
    def isRunning(self) -> bool:  # noqa: N802 - 与 QThread 同名
        return self._running

    def start(self) -> None:
        self._running = True
        QTimer.singleShot(self._delay, self._emit)

    def wait(self, _timeout: int = 0) -> bool:  # noqa: N802
        return not self._running

    def _emit(self) -> None:
        self._running = False
        window = type(self).windows.get(self.hwnd)
        if self._error:
            self.completed.emit(None, None, 0, 0, self._error)
            return
        frame = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), 245, dtype=np.uint8)
        self.completed.emit(window, frame, FRAME_WIDTH, FRAME_HEIGHT, "")


def pump(app: QApplication, condition, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        app.processEvents()
        time.sleep(0.005)
    return condition()


def button_with(dialog: QDialog, needle: str) -> QPushButton:
    for button in dialog.findChildren(QPushButton):
        if needle in button.text():
            return button
    raise AssertionError(f"对话框里没有包含 {needle!r} 的按钮")


class FlowDriver(QObject):
    """在嵌套模态循环里推进“打开 → 截取 → 圈选 → 保存”的状态机。"""

    def __init__(
        self,
        app: QApplication,
        *,
        alias: str = "第四群",
        cancel_selection: bool = False,
        cancel_during_capture: bool = False,
        reject_save: bool = False,
        retry_on_failure: bool = False,
    ) -> None:
        super().__init__()
        self.app = app
        self.alias = alias
        self.cancel_selection = cancel_selection
        self.cancel_during_capture = cancel_during_capture
        self.reject_save = reject_save
        self.retry_on_failure = retry_on_failure
        self.stage = "wait_edit"
        self.edit_dialog: region_ui.RegionEditDialog | None = None
        self.selection_dialog: region_ui.RegionSelectionDialog | None = None
        self.selection_cancelled = False
        self.ghost_dialog = False
        self.failure_status = ""
        self.retried = False
        self.alias_after_cancel = ""
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self) -> None:  # noqa: C901 - 状态机比拆成回调更可读
        modal = self.app.activeModalWidget()
        if self.stage == "wait_edit":
            if isinstance(modal, region_ui.RegionEditDialog):
                self.edit_dialog = modal
                modal.alias.setText(self.alias)
                if self.cancel_during_capture:
                    button_with(modal, "截取").click()
                    button_with(modal, "取消").click()
                    self.stage = "wait_stale"
                    return
                button_with(modal, "截取").click()
                self.stage = "wait_selection"
            return
        if self.stage == "wait_selection":
            if isinstance(modal, region_ui.RegionEditDialog):
                status = modal.capture_status.text()
                if "失败" in status:
                    self.failure_status = status
                    self.edit_dialog = modal
                    if self.retry_on_failure and not self.retried:
                        self.retried = True
                        modal.recapture_button.click()
                        return
                    modal.cancel_button.click()
                    self.stage = "done"
                    return
                if modal.steps.currentIndex() != 1 or modal.selector is None:
                    return
                if self.cancel_selection:
                    self.selection_cancelled = True
                    modal.back_button.click()
                    self.stage = "wait_edit_again"
                    return
                if modal.selected_rect is None:
                    target = modal.selector._target()
                    modal.selector.set_widget_selection(target.adjusted(
                        target.width() // 10, target.height() // 10,
                        -target.width() // 10, -target.height() // 10,
                    ))
                    return
                if modal.pending_button.isEnabled():
                    self.stage = "wait_edit_again"
            return
        if self.stage == "wait_edit_again":
            if isinstance(modal, region_ui.RegionEditDialog):
                self.edit_dialog = modal
                if self.selection_cancelled:
                    # 取消圈选后必须回到添加界面：记录证据，然后按用户“取消”结束本次添加。
                    self.alias_after_cancel = modal.alias.text()
                    self.stage = "back_to_edit"
                    button_with(modal, "取消").click()
                    self.stage = "done"
                    return
                if self.reject_save:
                    button_with(modal, "取消").click()
                else:
                    modal.pending_button.click()
                self.stage = "done"
            return
        if self.stage in {"wait_stale", "back_to_edit"}:
            if isinstance(modal, region_ui.RegionSelectionDialog):
                self.ghost_dialog = True

    def end_after_back(self) -> None:
        """取消圈选后，用户放弃这次添加：点“取消”，让 add_region 正常返回。"""
        if self.stage == "back_to_edit" and self.edit_dialog is not None:
            button_with(self.edit_dialog, "取消").click()
            self.stage = "done"
            self.app.processEvents()

    def stop(self) -> None:
        self.timer.stop()


class LifecycleFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.windows = [
            FakeWindow(1001, "群一"),
            FakeWindow(1002, "群二"),
            FakeWindow(1003, "群三"),
        ]
        FakeSelectionWorker.windows = {item.hwnd: item for item in self.windows}
        FakeSelectionWorker.delay_ms = 0
        FakeSelectionWorker.fail_plan = []
        self.patches = [
            patch(
                "deal_watch.ui.region.WindowsWindows",
                side_effect=lambda: FakeWindows(self.windows),
            ),
            patch("deal_watch.ui.region.SelectionCaptureWorker", FakeSelectionWorker),
            # 提示框不得阻塞离屏事件循环；需要时用断言检查调用。
            patch("deal_watch.ui.region.QMessageBox", MagicMock(spec=QMessageBox)),
            patch("deal_watch.ui.main.QMessageBox", MagicMock(spec=QMessageBox)),
        ]
        for item in self.patches:
            item.start()
        self.window = self.make_window()

    def make_window(self) -> MainWindow:
        window = MainWindow(REPO_ROOT)
        window.resize(1180, 780)
        return window

    def tearDown(self):
        self.shutdown(getattr(self, "window", None))
        for item in reversed(self.patches):
            item.stop()
        self.env.stop()
        self.temp.cleanup()

    def shutdown(self, window: MainWindow | None) -> None:
        if window is None:
            return
        window.exit_app()
        pump(self.app, lambda: window._shutdown_done, 10)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    # -- 辅助 -------------------------------------------------------------
    def seed(self, regions: list[RegionConfig]) -> None:
        self.window.config.regions = list(regions)
        self.window.config_store.save(self.window.config)
        self.window._load_regions()
        if regions:
            self.window.region_table.selectRow(0)

    def disk_regions(self) -> list[RegionConfig]:
        return ConfigStore(self.window.data_dir).load().regions

    def three_regions(self) -> list[RegionConfig]:
        return [
            ready_region(f"群{i}", hwnd=1000 + i, rect=Rect(0, 0, 300, 500), window_title=f"群{i}")
            for i in range(1, 4)
        ]

    def wait_save(self) -> None:
        self.assertTrue(pump(self.app, lambda: not self.window._saving, 10), "保存没有在超时内结束")
        self.app.processEvents()

    def run_add(self, **kwargs) -> FlowDriver:
        driver = FlowDriver(self.app, **kwargs)
        self.window.add_region()
        return driver

    def run_edit(self, **kwargs) -> FlowDriver:
        driver = FlowDriver(self.app, **kwargs)
        self.window.edit_region()
        return driver


class AddRegionLifecycleTests(LifecycleFixture):
    """A：从真实入口完成“添加第 4 条”，不允许流程提前中断。"""

    def test_fourth_region_saved_through_real_dialog(self):
        self.seed(self.three_regions())
        driver = self.run_add()
        self.wait_save()
        self.assertIsNotNone(driver.edit_dialog, "添加对话框没有被打开")
        self.assertEqual(len(self.window.config.regions), 4, "内存里没有第 4 条区域")
        self.assertEqual(len(self.disk_regions()), 4, "磁盘上没有第 4 条区域")
        self.assertEqual(self.window.region_table.rowCount(), 4)
        self.assertEqual(self.window.region_table.item(3, 0).text(), "第四群")
        self.assertEqual(self.window.region_table.item(3, 1).text(), "待确认")
        saved = self.window.config.regions[3]
        self.assertFalse(saved.previewed, "未预览的区域不得直接进入监控")
        self.assertEqual(saved.window_hwnd, 1001)
        self.assertGreaterEqual(saved.rect.width, 60)

    def test_fourth_region_survives_restart(self):
        self.seed(self.three_regions())
        self.run_add()
        self.wait_save()
        self.shutdown(self.window)
        self.window = self.make_window()
        aliases = [
            self.window.region_table.item(row, 0).text()
            for row in range(self.window.region_table.rowCount())
        ]
        self.assertEqual(aliases, ["群1", "群2", "群3", "第四群"])

    def test_fifth_region_is_saved_through_real_dialog(self):
        four = [
            ready_region(f"群{i}", hwnd=1000 + i, rect=Rect(0, 0, 300, 500), window_title=f"群{i}")
            for i in range(1, 5)
        ]
        self.seed(four)
        self.run_add()
        self.wait_save()
        self.assertEqual(len(self.window.config.regions), 5)
        self.assertEqual(len(self.disk_regions()), 5)

    def test_cancel_add_after_selection_writes_nothing(self):
        self.seed(self.three_regions())
        before = len(self.disk_regions())
        driver = self.run_add(reject_save=True)
        try:
            self.assertEqual(len(self.window.config.regions), before)
            self.assertEqual(len(self.disk_regions()), before)
        finally:
            driver.stop()


class RecalibrateLifecycleTests(LifecycleFixture):
    """B：重新校准复用同一个对话框，同样不能被截取打断。"""

    def test_recalibrate_updates_existing_region_in_place(self):
        self.seed(self.three_regions())
        self.window.region_table.selectRow(1)
        self.run_edit(alias="群2")
        self.wait_save()
        self.assertEqual(len(self.window.config.regions), 3, "重新校准不得新增区域")
        self.assertEqual(len(self.disk_regions()), 3)
        updated = self.window.config.regions[1]
        self.assertEqual(updated.alias, "群2")
        self.assertEqual(updated.window_hwnd, 1002, "重新校准绑定了错误的窗口")
        self.assertFalse(updated.previewed, "重新校准后必须重新确认")


class CancelAndRetryTests(LifecycleFixture):
    """C：取消圈选、取消添加、截取失败与迟到结果。"""

    def test_cancel_selection_returns_to_edit_without_writing(self):
        self.seed(self.three_regions())
        before = len(self.disk_regions())
        driver = self.run_add(cancel_selection=True)
        try:
            self.assertTrue(driver.selection_cancelled)
            self.assertEqual(driver.alias_after_cancel, "第四群", "取消圈选后没有回到添加界面")
            self.assertEqual(len(self.window.config.regions), before)
            self.assertEqual(len(self.disk_regions()), before)
        finally:
            driver.stop()

    def test_cancel_while_capturing_leaves_no_ghost_dialog(self):
        self.seed(self.three_regions())
        FakeSelectionWorker.delay_ms = 150
        before = len(self.disk_regions())
        driver = self.run_add(cancel_during_capture=True)
        try:
            # 让迟到的采集结果真正回到事件循环。
            pump(self.app, lambda: False, 0.8)
            self.assertFalse(driver.ghost_dialog, "取消后仍弹出了圈选窗口（幽灵弹窗）")
            self.assertEqual(len(self.disk_regions()), before)
            self.assertEqual(len(self.window.config.regions), before)
        finally:
            driver.stop()

    def test_capture_failure_keeps_input_and_allows_retry(self):
        self.seed(self.three_regions())
        FakeSelectionWorker.fail_plan = ["RuntimeError: PRINTWINDOW_BLANK_FRAME"]
        driver = self.run_add(retry_on_failure=True)
        try:
            self.wait_save()
            self.assertIsNotNone(driver.edit_dialog)
            self.assertTrue(driver.retried, "截取失败后没有提供重试入口")
            self.assertIn("失败", driver.failure_status, "截取失败后没有说明原因")
            self.assertIn("可重试", driver.failure_status)
            # 重试不需要重填别名：最终确实保存了第 4 条。
            self.assertEqual(len(self.window.config.regions), 4)
            self.assertEqual(self.window.config.regions[3].alias, "第四群")
            self.assertEqual(len(self.disk_regions()), 4)
        finally:
            driver.stop()


if __name__ == "__main__":
    unittest.main()
