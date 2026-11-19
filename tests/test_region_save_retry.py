"""0.2.2 阶段一返工回归：D1 失败保留待保存区域、D2 改名重名拦截、D3 verify 前置。

与 QA 的验收基准（``tests/test_qa_stage1_*.py``）互补：QA 断言“不得发生什么”，
这里断言“修好之后用户实际能做什么”——

* 保存失败后待保存的区域仍在内存里，点“重试保存”就能写进去（不必重新圈选）；
* 改名撞到已有别名会被拒绝，而“保持原名”的历史同名配置不会被误拒；
* 回读校验失败时正式配置文件逐字节不变，内存也不动，临时文件被清理。

Qt 用例一律 offscreen，不打开任何占屏窗口，也不接触真实微信窗口。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from deal_watch.config import AppConfig, ConfigSaveError, ConfigStore  # noqa: E402
from deal_watch.models import Rect, RegionConfig  # noqa: E402
from deal_watch.region_save import (  # noqa: E402
    alias_needs_duplicate_check,
    commit_region_change,
)
from deal_watch.ui.main import MainWindow  # noqa: E402
from region_fixtures import ready_region  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRegionEditDialog:
    """替身：接受即返回固定区域，避免真正打开圈选对话框。"""

    DialogCode = QDialog.DialogCode

    def __init__(self, _parent=None, existing: RegionConfig | None = None) -> None:
        self.existing = existing

    def exec(self) -> int:
        return QDialog.DialogCode.Accepted

    def region(self) -> RegionConfig:
        if self.existing is not None:
            return replace(self.existing, rect=Rect(10, 20, 360, 560), previewed=False)
        return ready_region("新群", hwnd=2002, rect=Rect(10, 20, 400, 600), previewed=False)


# ----------------------------------------------------------------------
# 纯数据层：D2 别名撞车规则 + D3 verify 前置
# ----------------------------------------------------------------------


class _StoreFixture(unittest.TestCase):
    """一个干净的临时数据目录 + 一个满足不变量 I-1 的起点。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.store = ConfigStore(self.dir)

    def tearDown(self):
        self._temp.cleanup()

    def seed(self, regions: list[RegionConfig]) -> AppConfig:
        config = AppConfig(regions=list(regions))
        self.store.save(config)
        return config

    def disk_aliases(self) -> list[str]:
        return [region.alias for region in self.disk_regions()]

    def disk_regions(self) -> list[RegionConfig]:
        return ConfigStore(self.dir).load().regions

    def temp_leftovers(self) -> list[Path]:
        return list(self.dir.glob("config.json.*.tmp"))


class RenameAliasCollisionTests(_StoreFixture):
    """D2：区分“保持原名”与“改动别名”。"""

    def test_rename_to_existing_alias_is_rejected(self):
        config = self.seed(
            [
                ready_region("群A", hwnd=1001, rect=Rect(0, 0, 300, 500)),
                ready_region("群B", hwnd=1002, rect=Rect(0, 0, 300, 500)),
            ]
        )
        renamed = replace(config.regions[1], alias="群A")
        result = commit_region_change(self.store, config, "update", renamed, 1)
        self.assertFalse(result.ok, "改名撞到已有别名时必须拒绝")
        self.assertEqual(result.error_stage, "validate")
        self.assertIn("同名", result.message)
        # 内存、磁盘、界面共用同一份真值：二者都必须是旧快照。
        self.assertEqual([region.alias for region in config.regions], ["群A", "群B"])
        self.assertEqual(self.disk_aliases(), ["群A", "群B"])

    def test_unchanged_alias_is_not_blocked_by_legacy_duplicates(self):
        """历史配置里已有同名区域时，重新校准（不改名）不得被卡住。"""
        config = self.seed(
            [
                ready_region("群A", hwnd=1001, rect=Rect(0, 0, 300, 500)),
                ready_region("群A", hwnd=1002, rect=Rect(0, 0, 300, 500)),
            ]
        )
        new_rect = Rect(22, 32, 322, 532)
        recalibrated = replace(config.regions[1], rect=new_rect)
        result = commit_region_change(self.store, config, "update", recalibrated, 1)
        self.assertTrue(result.ok, result.message)
        self.assertEqual([region.alias for region in config.regions], ["群A", "群A"])
        self.assertEqual(self.disk_aliases(), ["群A", "群A"])
        # 实质断言：ok=True 必须意味着“新的圈选真的落盘了”，而不只是没报错。
        # 没有这两行时，把 apply_region_change 换成空操作这条用例照样通过。
        self.assertEqual(config.regions[1].rect, new_rect, "内存里的新圈选没有生效")
        self.assertEqual(self.disk_regions()[1].rect, new_rect, "新圈选没有写进配置文件")

    def test_duplicate_check_follows_alias_change_not_action_type(self):
        original = ready_region("群A", hwnd=1001, rect=Rect(0, 0, 300, 500))
        # 新增一律检查；改名检查；保持原名与只切状态都不检查。
        self.assertTrue(alias_needs_duplicate_check("add", original, None))
        self.assertTrue(
            alias_needs_duplicate_check("update", replace(original, alias="群B"), original)
        )
        self.assertFalse(alias_needs_duplicate_check("update", original, original))
        self.assertFalse(
            alias_needs_duplicate_check("toggle", replace(original, enabled=False), original)
        )
        # 找不到原区域（缺 id 的旧配置）时按“改动”从严处理。
        self.assertTrue(alias_needs_duplicate_check("update", original, None))


class VerifyBeforeReplaceTests(_StoreFixture):
    """D3：回读校验前移到 ``os.replace`` 之前，失败即“原文件一个字节都不动”。"""

    def test_verify_failure_keeps_disk_bytes_and_memory_on_old_snapshot(self):
        config = self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        before = self.store.path.read_bytes()
        with patch(
            "deal_watch.config.ConfigStore._verify",
            side_effect=ConfigSaveError("verify", self.store.path),
        ), patch("deal_watch.config.time.sleep"):
            result = commit_region_change(
                self.store,
                config,
                "add",
                ready_region("新群", hwnd=2002, rect=Rect(10, 20, 400, 600)),
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_stage, "verify")
        # 不变量：磁盘 == 内存（旧快照）。不存在“已写入但校验失败”的中间态。
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(len(config.regions), 1)
        self.assertEqual(len(ConfigStore(self.dir).load().regions), 1)
        # 校验不过的临时文件必须立刻丢弃，不给下一次保存留垃圾。
        self.assertEqual(self.temp_leftovers(), [])


# ----------------------------------------------------------------------
# 界面层：D1 失败后“重试保存”真的能成功
# ----------------------------------------------------------------------


class RetryAfterFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.window = MainWindow(REPO_ROOT)

    def tearDown(self):
        self.window.exit_app()
        self.pump_until(lambda: self.window._shutdown_done, 10)
        self.window.deleteLater()
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def pump_until(self, condition, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(condition())

    def seed(self, regions: list[RegionConfig]) -> None:
        self.window.config.regions = list(regions)
        self.window.config_store.save(self.window.config)
        self.window._load_regions()
        if regions:
            self.window.region_table.selectRow(0)

    def disk_aliases(self) -> list[str]:
        return [region.alias for region in ConfigStore(self.window.data_dir).load().regions]

    def failing_replaces(self, allowed_failures: int):
        """前 ``allowed_failures`` 次 ``os.replace`` 失败，之后恢复正常。

        ``ConfigStore.save`` 内部会重试一次，所以一次界面保存 = 两次 replace。
        """
        real_replace = os.replace
        calls: list[int] = []

        def flaky_replace(source, destination):
            calls.append(1)
            if len(calls) <= allowed_failures:
                raise PermissionError("[WinError 5] 拒绝访问")
            return real_replace(source, destination)

        return patch("deal_watch.config.os.replace", side_effect=flaky_replace), calls

    def test_failed_add_is_kept_in_memory_and_retry_succeeds(self):
        """D1：失败后草稿留在内存里，点“重试保存”即可成功，不必重新圈选。"""
        self.seed([ready_region("群1", hwnd=1001, rect=Rect(0, 0, 300, 500))])
        replace_patch, calls = self.failing_replaces(allowed_failures=2)
        with replace_patch, patch("deal_watch.config.time.sleep"), patch(
            "deal_watch.ui.main.RegionEditDialog", FakeRegionEditDialog
        ), patch("deal_watch.ui.main.QMessageBox.critical") as critical:
            self.window.add_region()
            self.pump_until(lambda: not self.window._saving)
            # 第一次保存失败：草稿必须还在，且重试出口真的可用。
            self.assertEqual(critical.call_args.args[1], "保存失败")
            self.assertIsNotNone(
                self.window._pending_save, "失败后待保存区域被丢弃，用户只能重新圈选"
            )
            self.assertTrue(self.window.retry_button.isEnabled(), "重试出口不可用")
            self.assertIn("重试保存", critical.call_args.args[2])
            self.assertIn("重试保存", self.window.statusBar().currentMessage())
            self.assertEqual(len(calls), 2)  # 两次内部尝试都失败
            # 环境恢复后点一下就能写入。
            self.window.retry_button.click()
            self.pump_until(lambda: not self.window._saving)
        self.assertEqual([r.alias for r in self.window.config.regions], ["群1", "新群"])
        self.assertEqual(self.disk_aliases(), ["群1", "新群"])
        self.assertEqual(self.window.region_table.rowCount(), 2)
        self.assertIsNone(self.window._pending_save, "成功后必须清掉草稿")
        self.assertFalse(self.window.retry_button.isEnabled())
        self.assertEqual(self.window._failed_save_stage, "")

    def test_validation_failure_drops_draft_and_offers_no_retry(self):
        """校验类失败重发同一份内容必然再失败：不保留草稿，提示用户修正。"""
        self.seed(
            [
                ready_region("群A", hwnd=1001, rect=Rect(0, 0, 300, 500)),
                ready_region("群B", hwnd=1002, rect=Rect(0, 0, 300, 500)),
            ]
        )
        self.window.region_table.selectRow(1)

        class RenameDialog(FakeRegionEditDialog):
            def region(self) -> RegionConfig:
                return replace(self.existing, alias="群A")

        with patch("deal_watch.ui.main.RegionEditDialog", RenameDialog), patch(
            "deal_watch.ui.main.QMessageBox.critical"
        ) as critical:
            self.window.edit_region()
            self.pump_until(lambda: not self.window._saving)
        self.assertIsNone(self.window._pending_save)
        self.assertFalse(self.window.retry_button.isEnabled())
        self.assertIn("同名", critical.call_args.args[2])
        self.assertIn("修正", critical.call_args.args[2])
        self.assertEqual(self.disk_aliases(), ["群A", "群B"])
        self.assertEqual([r.alias for r in self.window.config.regions], ["群A", "群B"])


if __name__ == "__main__":
    unittest.main()
