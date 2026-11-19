"""两个自动化验收工具的就绪门禁硬闸与生产采集路径（不运行真实工具）。

覆盖两部分：
1. ``real_env.region_gate_failures`` 的纯函数判定（别名 → 最后一次 detail）；
2. 源码级回归锁：两个工具都必须走生产 WGC 帧坐标路径、都带门禁硬闸与退出码 2，
   防止有人改回 ``MssCapturer`` 或删掉门禁后仍然产出“通过”报告。
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TOOLS = ROOT / "tools"

_TOOLS_SOURCE = {
    name: (TOOLS / name).read_text(encoding="utf-8")
    for name in (
        "acceptance_synthetic.py",
        "acceptance_wgc_service.py",
        "real_schedule_60s.py",
        "real_env.py",
        "real_window_probe.py",
        "occluded_new_text_check.py",
    )
}


def _ensure_paths() -> None:
    for entry in (str(SRC), str(ROOT), str(TOOLS)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def load_module(name: str):
    _ensure_paths()
    return importlib.import_module(name)


def _source(name: str) -> str:
    return _TOOLS_SOURCE[name]


class _Region:
    """最小的 RegionConfig 替身：门禁只读 id 与 alias。"""

    def __init__(self, region_id: str, alias: str) -> None:
        self.id = region_id
        self.alias = alias


class RegionGateFailureTests(unittest.TestCase):
    """门禁硬闸：{别名: 最后一次 detail}，空字典才是通过。"""

    def setUp(self) -> None:
        self.real_env = load_module("real_env")
        self.regions = [_Region("a", "一群"), _Region("b", "二群")]

    def test_all_running_passes(self) -> None:
        transitions = [
            {"region_id": "a", "status": "baselining", "detail": "等待建立可见内容基线"},
            {"region_id": "a", "status": "running", "detail": "监控中"},
            {"region_id": "b", "status": "baselining", "detail": "等待建立可见内容基线"},
            {"region_id": "b", "status": "running", "detail": "监控中"},
        ]
        self.assertEqual(self.real_env.region_gate_failures(self.regions, transitions), {})

    def test_only_baselining_passes(self) -> None:
        transitions = [
            {"region_id": "a", "status": "baselining", "detail": "等待建立可见内容基线"},
            {"region_id": "b", "status": "baselining", "detail": "恢复后重新建立基线"},
        ]
        self.assertEqual(self.real_env.region_gate_failures(self.regions, transitions), {})

    def test_unready_paused_region_is_reported_with_detail(self) -> None:
        detail = "区域未就绪：尚未检查确认当前可见内容；请重新圈选或预览确认"
        transitions = [
            {"region_id": "a", "status": "running", "detail": "监控中"},
            {"region_id": "b", "status": "paused", "detail": detail},
        ]
        failures = self.real_env.region_gate_failures(self.regions, transitions)
        self.assertEqual(failures, {"二群": detail})

    def test_never_reported_region_is_reported(self) -> None:
        transitions = [{"region_id": "a", "status": "running", "detail": "监控中"}]
        failures = self.real_env.region_gate_failures(self.regions, transitions)
        self.assertEqual(set(failures), {"二群"})
        self.assertIn("从未上报", failures["二群"])

    def test_region_only_ever_paused_by_user_fails(self) -> None:
        transitions = [{"region_id": "a", "status": "paused", "detail": "用户暂停"}]
        failures = self.real_env.region_gate_failures(self.regions, transitions)
        self.assertEqual(failures, {"一群": "用户暂停", "二群": "从未上报健康状态"})

    # ---- 两段判定的关键回归（2026-11 实跑 8.2 秒误杀事故） ----

    def test_empty_transitions_is_never_a_definitive_failure(self) -> None:
        """等待刚开始时 transitions 为空：绝不能因此立即中止。"""
        self.assertEqual(self.real_env.definitive_gate_failures([], {}), {})
        self.assertEqual(self.real_env.definitive_gate_failures(self.regions, []), {})

    def test_unreported_regions_only_fail_the_final_judgement(self) -> None:
        """“从未上报”只在 include_unreported=True 时算失败，不能作为等待期条件。"""
        self.assertEqual(self.real_env.definitive_gate_failures(self.regions, []), {})
        final = self.real_env.region_gate_failures(
            self.regions, [], include_unreported=True
        )
        self.assertEqual(set(final), {"一群", "二群"})
        self.assertEqual(
            self.real_env.region_gate_failures(self.regions, [], include_unreported=False),
            {},
        )

    def test_unready_paused_is_definitive(self) -> None:
        detail = "区域未就绪：尚未检查确认当前可见内容；请重新圈选或预览确认"
        transitions = [{"region_id": "b", "status": "paused", "detail": detail}]
        self.assertEqual(
            self.real_env.definitive_gate_failures(self.regions, transitions),
            {"二群": detail},
        )

    def test_all_running_passes_both_judgements(self) -> None:
        transitions = [
            {"region_id": "a", "status": "running", "detail": "监控中"},
            {"region_id": "b", "status": "running", "detail": "监控中"},
        ]
        self.assertEqual(self.real_env.definitive_gate_failures(self.regions, transitions), {})
        self.assertEqual(
            self.real_env.region_gate_failures(
                self.regions, transitions, include_unreported=True
            ),
            {},
        )

    def test_gate_wait_selector_matches_startup_semantics(self) -> None:
        """等待条件 = 全部上报 或 确定性失败；空 transitions 必须返回 False。"""
        waiting = self.real_env.definitive_gate_failures(self.regions, [])
        self.assertFalse(waiting)
        unready = [{"region_id": "a", "status": "paused", "detail": "区域未就绪：尚未检查确认"}]
        self.assertTrue(self.real_env.definitive_gate_failures(self.regions, unready))


class SubprocessPythonPathTests(unittest.TestCase):
    """采集子进程环境：src/ROOT 必须排在最前、幂等、不破坏原有条目。"""

    def setUp(self) -> None:
        self.real_env = load_module("real_env")

    def test_ensure_puts_repo_first_and_is_idempotent(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"PYTHONPATH": "C:/other"}, clear=False):
            first = self.real_env.ensure_subprocess_pythonpath()
            parts = first.split(os.pathsep)
            self.assertEqual(parts[0], str(self.real_env.SRC))
            self.assertEqual(parts[1], str(self.real_env.ROOT))
            self.assertIn("C:/other", parts)
            # 幂等：再调一次不会重复追加。
            second = self.real_env.ensure_subprocess_pythonpath()
            self.assertEqual(first, second)

    def test_ensure_works_without_existing_pythonpath(self) -> None:
        import os
        from unittest.mock import patch

        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        with patch.dict(os.environ, environment, clear=True):
            value = self.real_env.ensure_subprocess_pythonpath()
        self.assertEqual(
            value.split(os.pathsep),
            [str(self.real_env.SRC), str(self.real_env.ROOT)],
        )

    def test_child_environment_does_not_mutate_parent(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"PYTHONPATH": "C:/other"}, clear=False):
            before = os.environ["PYTHONPATH"]
            environment = self.real_env.child_environment()
            self.assertEqual(os.environ["PYTHONPATH"], before)
        parts = environment["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(self.real_env.SRC))
        self.assertIn("C:/other", parts)


class AcceptanceToolSourceTests(unittest.TestCase):
    """源码级回归锁：两个工具必须用生产采集路径 + 门禁硬闸 + 退出码 2。"""

    def test_synthetic_tool_uses_production_wgc_capture(self) -> None:
        source = _source("acceptance_synthetic.py")
        self.assertIn("WindowFrameCapturer(", source)
        self.assertIn('capture_backend="wgc"', source)
        self.assertIn('coordinate_space="frame"', source)
        self.assertIn("previewed=True", source)
        self.assertIn('"uses_legacy_desktop_capture": False', source)
        self.assertIn('"frame_sizes"', source)
        self.assertIn("reference_frame_width=frame.width", source)
        self.assertIn("合成窗口 WGC 采集失败", source)
        # 0.2.1 退役路径不得再出现在这个工具里。
        self.assertNotIn("MssCapturer", source)
        self.assertNotIn("service.capturer._windows", source)

    def test_synthetic_tool_exports_pythonpath_for_capture_subprocess(self) -> None:
        source = _source("acceptance_synthetic.py")
        # WindowCaptureClient 用 subprocess.run 不传 env：必须显式准备子进程 PYTHONPATH，
        # 否则开发态子进程 `python -m deal_watch.capture_worker` 找不到包。
        self.assertIn("ensure_subprocess_pythonpath()", source)

    def test_all_capture_tools_prepare_subprocess_pythonpath(self) -> None:
        # 三个直接用 WindowCaptureClient / WindowFrameCapturer 的工具都必须准备 env；
        # real_schedule_60s 走 RecordingCapturer → WindowFrameCapturer 同理。
        for name in ("acceptance_synthetic.py", "acceptance_wgc_service.py",
                     "real_schedule_60s.py"):
            with self.subTest(tool=name):
                self.assertIn("ensure_subprocess_pythonpath()", _source(name))
        # 唯一实现在 real_env，内部才真正写 os.environ。
        self.assertIn('os.environ["PYTHONPATH"]', _source("real_env.py"))

    def test_synthetic_tool_has_readiness_gate_and_exit_code_two(self) -> None:
        source = _source("acceptance_synthetic.py")
        # 两段判定：等待期只用 definitive，超时后才发现“从未上报”，且后者退出码 1。
        self.assertIn("definitive_gate_failures", source)
        self.assertIn("region_gate_failures", source)
        self.assertIn("CaptureStartupError", source)
        self.assertIn("gate_wait_seconds", source)
        self.assertIn("READINESS_ABORT", source)
        self.assertIn('report["region_gate_failures"]', source)
        self.assertIn("return 2", source)
        # 等待期不得再用会包含“从未上报”的判定。
        self.assertNotIn("gate_settled", source)

    def test_wgc_tool_has_previewed_and_readiness_gate(self) -> None:
        source = _source("acceptance_wgc_service.py")
        self.assertIn("previewed=True", source)
        self.assertIn("definitive_gate_failures", source)
        self.assertIn("region_gate_failures", source)
        self.assertIn("CaptureStartupError", source)
        self.assertIn("gate_wait_seconds", source)
        self.assertIn("READINESS_ABORT", source)
        self.assertIn('report["region_gate_failures"]', source)
        self.assertIn("return 2", source)
        # 最小化必须走暂停语义（不是故障）。
        self.assertIn('== "paused"', source)
        # PYTHONPATH 准备：该文件必须显式调共享实现（唯一写入点见 real_env）。
        self.assertIn("ensure_subprocess_pythonpath()", source)
        self.assertIn('os.environ["PYTHONPATH"]', _source("real_env.py"))


class ConfiguredRegionModeTests(unittest.TestCase):
    """验收工具必须默认读"用户已配置并确认"的区域，禁止默认枚举/自动 ROI。

    这一组是**源码级**回归锁：只要有人把默认路径改回"枚举微信窗口 + 探针 ROI"，
    或者给工具加上写配置的调用，用例立刻失败。
    """

    def test_schedule_tool_defaults_to_user_config(self) -> None:
        source = _source("real_schedule_60s.py")
        self.assertIn('SOURCE_CONFIG = "config"', source)
        self.assertIn('SOURCE_PROBE = "probe"', source)
        self.assertIn('default=SOURCE_CONFIG', source, "默认必须读用户配置")
        self.assertIn("ConfigStore", source)
        self.assertIn("configured_regions", source)
        self.assertIn("region_readiness_problems", source)
        # 枚举窗口只能出现在 probe 分支，且必须同时显式 --allow-probe-roi。
        self.assertEqual(
            source.count("display1_weixin_windows"), 1, "枚举调用只允许出现在探针旁路"
        )
        self.assertIn("--allow-probe-roi", source)
        self.assertIn("allow_probe_roi", source)
        self.assertIn("--source probe", source)

    def test_schedule_tool_reports_required_evidence(self) -> None:
        source = _source("real_schedule_60s.py")
        for key in (
            '"region_count"',
            '"capture_mode"',
            '"roi_source"',
            '"user_confirmed_roi"',
            '"acceptance_eligible"',
            '"source_fingerprint"',
            '"processed_sequences"',
            '"stats_window"',
        ):
            with self.subTest(key=key):
                self.assertIn(key, source)
        # 超时参数由共享层统一写入报告（含 production_params / diagnostic_params）。
        self.assertIn("production_param_flags(args.capture_timeout", source)
        self.assertIn("--capture-timeout", source)

    def test_window_probe_defaults_to_user_config(self) -> None:
        source = _source("real_window_probe.py")
        self.assertIn('SOURCE_CONFIG = "config"', source)
        self.assertIn('default=SOURCE_CONFIG', source)
        self.assertIn("ConfigStore", source)
        self.assertIn("configured_regions", source)
        self.assertIn("roi_override", source, "配置 ROI 必须走 override 路径")
        self.assertIn('"roi_source"', source)
        self.assertIn('else "user_config"', source)
        self.assertEqual(source.count("display1_weixin_windows"), 1)
        self.assertIn("--allow-probe-roi", source)

    def test_no_tool_writes_user_config(self) -> None:
        for name in (
            "real_schedule_60s.py",
            "real_window_probe.py",
            "occluded_new_text_check.py",
            "acceptance_synthetic.py",
        ):
            with self.subTest(tool=name):
                source = _source(name)
                self.assertNotIn("ConfigStore().save", source)
                self.assertNotIn("store.save(", source)

    def test_tools_only_notify_mock_feishu(self) -> None:
        schedule = _source("real_schedule_60s.py")
        self.assertIn("start_mock_feishu()", schedule)
        self.assertIn("127.0.0.1", schedule)
        self.assertNotIn("open.feishu.cn", schedule)
        synthetic = _source("acceptance_synthetic.py")
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", 0)', synthetic)
        self.assertNotIn("open.feishu.cn", synthetic)

    def test_synthetic_tool_labels_its_own_roi(self) -> None:
        source = _source("acceptance_synthetic.py")
        self.assertIn('"capture_mode": "synthetic_own_windows"', source)
        self.assertIn('"roi_source": "synthetic_frame_roi"', source)
        self.assertIn('"user_confirmed_roi": False', source)
        self.assertIn('"region_count"', source)
        self.assertIn('"processed_sequences"', source)
        self.assertIn('"source_fingerprint"', source)

    def test_occluded_tool_labels_full_frame_target(self) -> None:
        source = _source("occluded_new_text_check.py")
        self.assertIn('"capture_mode": "explicit_target_window"', source)
        self.assertIn('"roi_source": "full_frame"', source)
        self.assertIn('"source_fingerprint"', source)


class ScheduleReportMetadataTests(unittest.TestCase):
    """报告元数据：指纹覆盖面、区域数文案、Git 溯源降级、regions 的 region_id。

    这里跑的是工具里的真实函数（``FINGERPRINT_FILES`` / ``submit_check_detail`` /
    ``git_provenance`` / ``_region_report_entry``），不是对着源码文本猜行为；
    只有报告键名这类"只能由 main 写出"的字段才做源码级断言。
    """

    def setUp(self) -> None:
        self.schedule = load_module("real_schedule_60s")

    def test_fingerprint_covers_service_and_capture_protocol(self) -> None:
        """完成标记修复（service）与采集协议（window_capture / capture_worker）必须入指纹。"""
        required = {
            "src/deal_watch/service.py",
            "src/deal_watch/window_capture.py",
            "src/deal_watch/capture_worker.py",
        }
        self.assertTrue(
            required <= set(self.schedule.FINGERPRINT_FILES),
            f"FINGERPRINT_FILES 缺少：{sorted(required - set(self.schedule.FINGERPRINT_FILES))}",
        )
        # 路径必须真的可读：指纹里出现 "missing" 等于没覆盖。
        fingerprint = self.schedule.source_fingerprint(
            [self.schedule.ROOT / name for name in self.schedule.FINGERPRINT_FILES],
            root=self.schedule.ROOT,
        )
        # 指纹的键在 Windows 上是反斜杠形式，比较前统一成正斜杠。
        files = {
            key.replace("\\", "/"): value
            for key, value in fingerprint["files"].items()
        }
        for name in sorted(required):
            with self.subTest(file=name):
                digest = files[name]
                self.assertNotEqual(digest, "missing", f"{name} 在仓库里读不到，指纹等于没覆盖")
                self.assertEqual(len(digest), 64, "指纹必须是 sha256")

    def test_fingerprint_changes_when_capture_file_content_changes(self) -> None:
        """同一组文件、只改其中一份内容，合并指纹必须变——否则指纹没有鉴别力。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in range(len(self.schedule.FINGERPRINT_FILES)):
                path = Path(directory) / f"f{index}.py"
                path.write_text("x = 1\n", encoding="utf-8")
                paths.append(path)
            before = self.schedule.source_fingerprint(paths, root=Path(directory))
            paths[-1].write_text("x = 2\n", encoding="utf-8")
            after = self.schedule.source_fingerprint(paths, root=Path(directory))
        self.assertNotEqual(before["combined_sha256"], after["combined_sha256"])

    def test_submit_check_detail_uses_actual_region_count(self) -> None:
        for count in (1, 2, 3, 4):
            with self.subTest(region_count=count):
                detail = self.schedule.submit_check_detail(count)
                self.assertIn(str(count), detail)
                self.assertNotIn("四", detail, "不得硬编码四区域")
                self.assertEqual(detail, f"每轮 {count} 个区域提交数均 > 0")
        # 文案来自实际区域数：3 个区域的报告不能说"四区域"。
        self.assertIn("3 个区域", self.schedule.submit_check_detail(3))
        # 确认 checks 与表头真的用了它，而不是留一个没人调用的辅助函数。
        source = _source("real_schedule_60s.py")
        self.assertIn('"detail": submit_check_detail(len(region_ids))', source)
        self.assertNotIn("四区提交", source)
        self.assertNotIn("每轮四区域提交数", source)

    def test_git_provenance_reads_full_commit_and_dirty_flag(self) -> None:
        commit = "e84498ef257a1aab04040508e5628b458df1a0e1"
        outputs = {
            "rev-parse HEAD": commit + "\n",
            "status --porcelain --untracked-files=no": " M src/deal_watch/service.py\n",
        }
        seen: list[list[str]] = []

        def runner(args: list[str], root: Path) -> str:
            seen.append(args)
            return outputs[" ".join(args)]

        dirty = self.schedule.git_provenance(Path("."), runner=runner)
        self.assertEqual(dirty["source_commit"], commit)
        self.assertIs(dirty["source_dirty"], True)

        def clean_runner(args: list[str], root: Path) -> str:
            return commit + "\n" if args[0] == "rev-parse" else ""

        clean = self.schedule.git_provenance(Path("."), runner=clean_runner)
        self.assertEqual(clean["source_commit"], commit)
        self.assertIs(clean["source_dirty"], False)
        # 只允许这两条只读命令：不能出现 add/commit/配置读取。
        self.assertEqual(
            seen,
            [
                ["rev-parse", "HEAD"],
                ["status", "--porcelain", "--untracked-files=no"],
            ],
        )

    def test_git_provenance_degrades_without_git(self) -> None:
        """Git 缺失（FileNotFoundError）与非仓库（返回 None）都必须降级为 null，不抛异常。"""

        def missing(args: list[str], root: Path) -> str | None:
            raise FileNotFoundError("git")

        def not_a_repo(args: list[str], root: Path) -> str | None:
            return None

        for runner in (missing, not_a_repo):
            with self.subTest(runner=getattr(runner, "__name__", "?")):
                info = self.schedule.git_provenance(Path("."), runner=runner)
                self.assertIsNone(info["source_commit"])
                self.assertIsNone(info["source_dirty"])

    def test_region_report_entry_carries_region_id_without_plaintext(self) -> None:
        class _Rect:
            x, y, width, height = 10, 20, 300, 400

        class _RegionConfig:
            id = "region-abc"
            window_hwnd = 0x1234
            window_title = "某个群聊"
            alias = "一群"
            rect = _Rect()
            reference_frame_width = 800
            reference_frame_height = 600
            capture_backend = "wgc"
            previewed = True

        entry = self.schedule._region_report_entry(1, _RegionConfig(), True)
        self.assertEqual(entry["region_id"], "region-abc")
        # 保留原有标识字段，且不出现标题/别名原文。
        self.assertEqual(entry["hwnd"], "0x1234")
        self.assertIn("title_sha256", entry)
        self.assertIn("alias_sha256", entry)
        self.assertNotIn("某个群聊", repr(entry))
        self.assertNotIn("'一群'", repr(entry))

    def test_report_declares_git_provenance_keys(self) -> None:
        """source_commit / source_dirty 由 main 写入 report，只能做源码级锁定。"""
        source = _source("real_schedule_60s.py")
        self.assertIn('"source_commit": git_info["source_commit"]', source)
        self.assertIn('"source_dirty": git_info["source_dirty"]', source)
        self.assertIn("git_provenance(ROOT)", source)


if __name__ == "__main__":
    unittest.main()
