"""锁定生产采集参数：生产 5 秒 + WGC 3 秒 + 60 秒采样，探针默认必须一致。

本文件只做静态与参数级校验，不启动任何真实探针（不占用桌面、不接触微信窗口）。
"""

from __future__ import annotations

import importlib
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TOOLS = ROOT / "tools"

PROBE_MODULES = (
    "real_schedule_60s",
    "occluded_new_text_check",
    "real_window_probe",
    "backend_compare",
    "roi_preview",
)

REPORT_PARAM_KEYS = {
    "capture_timeout_seconds",
    "wgc_timeout_seconds",
    "poll_interval_seconds",
    "production_params",
}


def _ensure_paths() -> None:
    for entry in (str(SRC), str(ROOT), str(TOOLS)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def load_module(name: str):
    """按名字加载 ``tools/`` 下的探针模块（不依赖 PYTHONPATH）。"""
    _ensure_paths()
    return importlib.import_module(name)


class ProductionParameterTests(unittest.TestCase):
    def test_window_capture_client_default_is_five_seconds(self) -> None:
        _ensure_paths()
        from deal_watch.window_capture import WindowCaptureClient, WindowFrameCapturer

        self.assertEqual(WindowCaptureClient().timeout, 5.0)
        self.assertEqual(WindowFrameCapturer().client.timeout, 5.0)

    def test_capture_worker_wgc_default_is_three_seconds(self) -> None:
        _ensure_paths()
        original = list(sys.path)
        try:
            module = importlib.import_module("deal_watch.capture_worker")
            default = inspect.signature(module.capture_wgc).parameters["timeout"].default
        finally:
            # capture_worker 会把 .capture-runtime 插到 sys.path 首位，导入后立即还原，
            # 避免把运行时自带的 numpy 顶到前面影响后续用例。
            sys.path[:] = original
        self.assertEqual(default, 3.0)

    def test_app_config_poll_interval_is_thirty_seconds(self) -> None:
        _ensure_paths()
        from deal_watch.config import AppConfig

        self.assertEqual(AppConfig().poll_interval_ms, 30000)

    def test_real_env_production_constants(self) -> None:
        real_env = load_module("real_env")
        self.assertEqual(real_env.PRODUCTION_CAPTURE_TIMEOUT, 5.0)
        self.assertEqual(real_env.PRODUCTION_WGC_TIMEOUT, 3.0)
        self.assertEqual(real_env.PRODUCTION_POLL_SECONDS, 30.0)
        # worker_payload 的默认超时也必须与生产一致。
        default = inspect.signature(real_env.worker_payload).parameters["timeout"].default
        self.assertEqual(default, 5.0)

    def test_production_param_flags_accepts_only_production_values(self) -> None:
        real_env = load_module("real_env")
        flags = real_env.production_param_flags(5.0)
        self.assertTrue(flags["production_params"])
        self.assertFalse(flags["diagnostic_params"])
        self.assertEqual(flags["capture_timeout_seconds"], 5.0)
        self.assertEqual(flags["wgc_timeout_seconds"], 3.0)
        self.assertEqual(flags["poll_interval_seconds"], 30.0)
        self.assertLessEqual(REPORT_PARAM_KEYS, set(flags))

        relaxed = real_env.production_param_flags(10.0)
        self.assertFalse(relaxed["production_params"])
        self.assertTrue(relaxed["diagnostic_params"])
        self.assertIn("relaxed_params_note", relaxed)

        slow_poll = real_env.production_param_flags(5.0, poll_interval=60.0)
        self.assertFalse(slow_poll["production_params"])
        self.assertTrue(slow_poll["diagnostic_params"])

    def test_every_probe_defaults_to_production_capture_timeout(self) -> None:
        real_env = load_module("real_env")
        for name in PROBE_MODULES:
            with self.subTest(probe=name):
                module = load_module(name)
                args = module.parse_args([])
                self.assertEqual(args.capture_timeout, 5.0)
                flags = real_env.production_param_flags(args.capture_timeout)
                self.assertTrue(flags["production_params"])
                self.assertFalse(flags["diagnostic_params"])
                # 放宽超时后必须能自证是诊断参数。
                relaxed = module.parse_args(["--capture-timeout", "10"])
                self.assertEqual(relaxed.capture_timeout, 10.0)
                relaxed_flags = real_env.production_param_flags(relaxed.capture_timeout)
                self.assertFalse(relaxed_flags["production_params"])
                self.assertTrue(relaxed_flags["diagnostic_params"])

    def test_schedule_probe_defaults_to_production_poll_interval(self) -> None:
        module = load_module("real_schedule_60s")
        args = module.parse_args([])
        self.assertEqual(args.interval_seconds, 30.0)
        real_env = load_module("real_env")
        flags = real_env.production_param_flags(
            args.capture_timeout, poll_interval=args.interval_seconds
        )
        self.assertTrue(flags["production_params"])


if __name__ == "__main__":
    unittest.main()
