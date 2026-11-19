"""0.2.2 T1.2：区域配置保存的事务边界（离线，无 Qt、无真实桌面）。

核心断言：任何写盘失败都不得让“内存 / 磁盘 / 表格”三者出现镜像不一致。
"""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest.mock import patch

from deal_watch.config import (
    SAVE_RETRY_DELAY_SECONDS,
    AppConfig,
    ConfigSaveError,
    ConfigStore,
)
from deal_watch.models import Rect, RegionConfig
from deal_watch.region_save import (
    apply_region_change,
    commit_config_change,
    commit_region_change,
    validate_region_config,
)
from region_fixtures import ready_region


class RegionSaveTestBase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.store = ConfigStore(self.dir)
        self.config = AppConfig(
            keywords=["手机"],
            regions=[ready_region("合成群", hwnd=1001, rect=Rect(0, 0, 300, 500))],
        )
        self.store.save(self.config)

    def tearDown(self):
        self._temp.cleanup()

    def disk_regions(self) -> list[RegionConfig]:
        return ConfigStore(self.dir).load().regions

    def new_region(self, alias: str = "新群", hwnd: int = 2002) -> RegionConfig:
        return ready_region(alias, hwnd=hwnd, rect=Rect(10, 20, 400, 600))

    def assert_consistent(self, expected_count: int) -> None:
        """内存 = 磁盘 = 期望条数（表格行数由界面用例断言）。"""
        self.assertEqual(len(self.config.regions), expected_count)
        self.assertEqual(len(self.disk_regions()), expected_count)


class ConfigSaveFaultTests(RegionSaveTestBase):
    """五种写盘故障：内存与磁盘都必须停在旧快照，且错误阶段可定位。"""

    def test_permission_error_on_replace_keeps_memory_and_disk(self):
        with patch(
            "deal_watch.config.os.replace",
            side_effect=PermissionError("[WinError 5] 拒绝访问"),
        ), patch("deal_watch.config.time.sleep") as sleep:
            result = commit_region_change(self.store, self.config, "add", self.new_region())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_stage, "replace")
        # 明确给出“移出同步盘 / 加入杀软白名单”的可操作提示
        self.assertIn("同步盘", result.message)
        self.assertIn("白名单", result.message)
        # 已批准的设计决策 Q2：300ms 后重试一次
        sleep.assert_called_once_with(SAVE_RETRY_DELAY_SECONDS)
        self.assert_consistent(1)

    def test_os_error_on_replace_keeps_memory_and_disk(self):
        with patch(
            "deal_watch.config.os.replace",
            side_effect=OSError(errno.EIO, "I/O error"),
        ), patch("deal_watch.config.time.sleep"):
            result = commit_region_change(self.store, self.config, "add", self.new_region())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_stage, "replace")
        self.assert_consistent(1)

    def test_read_only_temp_file_fails_at_write_stage(self):
        with patch.object(
            Path,
            "write_text",
            side_effect=PermissionError("[WinError 19] 介质受写保护"),
        ), patch("deal_watch.config.time.sleep"):
            result = commit_region_change(self.store, self.config, "add", self.new_region())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_stage, "write")
        self.assert_consistent(1)

    def test_disk_error_fails_at_write_stage(self):
        with patch.object(
            Path, "write_text", side_effect=OSError(errno.ENOSPC, "磁盘空间不足")
        ), patch("deal_watch.config.time.sleep"):
            result = commit_region_change(self.store, self.config, "add", self.new_region())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_stage, "write")
        self.assert_consistent(1)

    def test_retry_once_succeeds_and_everything_matches(self):
        real_replace = os.replace
        calls: list[int] = []

        def flaky_replace(source, destination):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError("[WinError 32] 文件正被另一进程使用")
            return real_replace(source, destination)

        with patch("deal_watch.config.os.replace", side_effect=flaky_replace), patch(
            "deal_watch.config.time.sleep"
        ):
            result = commit_region_change(self.store, self.config, "add", self.new_region())
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(calls), 2)
        self.assert_consistent(2)
        self.assertEqual(self.disk_regions()[1].alias, "新群")

    def test_verify_failure_does_not_commit_memory(self):
        """回读内容被外部改写（半写/被篡改）时必须判失败，且内存与磁盘都停在旧快照。

        0.2.2 D3 修复后回读校验发生在 ``os.replace`` **之前**，因此故障注入点从
        “替换时被改写”移到“临时文件内容被改写”（同步盘/杀软在半写后动手，H1 的
        变体）：校验不过就丢弃临时文件，正式配置一个字节都不动。
        """
        real_write = Path.write_text

        def corrupting_write(path_self, _data, *_args, **_kwargs):
            # 临时文件“写入成功”，但落盘内容不是我们序列化出来的 payload。
            return real_write(Path(path_self), "{ 这不是 JSON", encoding="utf-8")

        with patch.object(
            Path, "write_text", corrupting_write
        ), patch("deal_watch.config.time.sleep"):
            result = commit_region_change(self.store, self.config, "add", self.new_region())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_stage, "verify")
        # 内存必须是旧快照：失败时绝不能把磁盘的半成品状态当成成功。
        self.assertEqual(len(self.config.regions), 1)
        # 校验失败发生在替换之前 → 磁盘与内存一致，且没有残留临时文件。
        self.assertEqual(len(self.disk_regions()), 1)
        self.assertEqual(list(self.dir.glob("config.json.*.tmp")), [])


class ConfigStoreSaveTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.store = ConfigStore(self.dir)

    def tearDown(self):
        self._temp.cleanup()

    def test_temporary_file_name_is_unique_and_cleaned_up(self):
        config = AppConfig(keywords=["手机"])
        self.store.save(config)
        self.assertEqual(self.store.path.read_text(encoding="utf-8").count("手机"), 1)
        # 固定名 config.tmp 会残留（假设 H2）；新命名必须带 pid + uuid 且用完即弃。
        self.assertEqual(list(self.dir.glob("config.tmp")), [])
        self.assertEqual(list(self.dir.glob("config.json.*.tmp")), [])

    def test_save_error_is_oserror_subclass_for_backward_compatibility(self):
        with patch(
            "deal_watch.config.os.replace", side_effect=PermissionError("busy")
        ), patch("deal_watch.config.time.sleep"):
            with self.assertRaises(ConfigSaveError) as raised:
                self.store.save(AppConfig(keywords=["手机"]))
        self.assertIsInstance(raised.exception, OSError)
        self.assertEqual(raised.exception.stage, "replace")

    def test_save_signature_stays_backward_compatible(self):
        # 0.2.1 的调用方式（无关键字参数）必须继续可用。
        self.store.save(AppConfig(keywords=["手机"]))
        self.assertTrue(self.store.path.exists())
        # verify=True 是新增可选能力，热路径默认关闭。
        self.store.save(AppConfig(keywords=["手机"]), verify=True)


class ValidateRegionConfigTests(RegionSaveTestBase):
    def test_unpreviewed_region_is_savable(self):
        """预览门禁只决定能否监控；未确认的区域必须能被保存并显示。"""
        region = self.new_region()
        region.previewed = False
        self.assertEqual(validate_region_config(region, existing=()), [])

    def test_small_region_and_out_of_frame_are_reported(self):
        small = ready_region("小区域", rect=Rect(0, 0, 10, 10))
        self.assertTrue(any("过小" in item for item in validate_region_config(small)))
        outside = ready_region("越界", rect=Rect(700, 100, 300, 300))
        self.assertTrue(any("超出采集帧" in item for item in validate_region_config(outside)))

    def test_duplicate_alias_is_rejected_but_self_is_allowed(self):
        duplicate = self.new_region(alias="合成群")
        problems = validate_region_config(duplicate, existing=self.config.regions)
        self.assertTrue(any("同名" in item for item in problems))
        existing_self = self.config.regions[0]
        self.assertEqual(
            validate_region_config(existing_self, existing=self.config.regions), []
        )

    def test_region_count_has_no_fixed_limit(self):
        existing = [ready_region(f"群{i}", hwnd=3000 + i) for i in range(20)]
        self.assertEqual(validate_region_config(self.new_region(alias="第二十一"), existing=existing), [])

    def test_apply_delete_removes_and_toggle_replaces(self):
        region = self.config.regions[0]
        paused = apply_region_change(
            self.config, "toggle", dataclass_replace(region, enabled=False), 0
        )
        self.assertEqual(paused, 0)
        self.assertFalse(self.config.regions[0].enabled)
        apply_region_change(self.config, "delete", None, 0)
        self.assertEqual(self.config.regions, [])


class CommitConfigChangeTests(RegionSaveTestBase):
    def test_keywords_failure_leaves_memory_untouched(self):
        with patch(
            "deal_watch.config.os.replace", side_effect=PermissionError("busy")
        ), patch("deal_watch.config.time.sleep"):
            result = commit_config_change(
                self.store,
                self.config,
                lambda candidate: setattr(candidate, "keywords", ["券"]),
            )
        self.assertFalse(result.ok)
        self.assertEqual(self.config.keywords, ["手机"])
        self.assertEqual(ConfigStore(self.dir).load().keywords, ["手机"])

    def test_keywords_success_commits_memory_and_disk(self):
        result = commit_config_change(
            self.store,
            self.config,
            lambda candidate: setattr(candidate, "keywords", ["券", "红包"]),
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.config.keywords, ["券", "红包"])
        self.assertEqual(ConfigStore(self.dir).load().keywords, ["券", "红包"])


if __name__ == "__main__":
    unittest.main()
