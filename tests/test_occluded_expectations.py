"""遮挡验证工具的端到端判定：期望文字 + 逐轮覆盖复核。

只测纯函数与参数解析，不启动 Qt、不占桌面、不接触真实微信窗口。
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TOOLS = ROOT / "tools"


def _ensure_paths() -> None:
    for entry in (str(SRC), str(ROOT), str(TOOLS)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def load_module(name: str):
    _ensure_paths()
    return importlib.import_module(name)


def _covered() -> dict:
    return {"fully_covered": True}


def _not_covered() -> dict:
    return {"fully_covered": False}


class ExpectedTextMatchTests(unittest.TestCase):
    """唯一编号必须字面命中：部分命中、拼字、空期望都不算成功。"""

    def setUp(self) -> None:
        self.module = load_module("occluded_new_text_check")

    def test_full_expectation_is_matched(self) -> None:
        self.assertTrue(
            self.module.expected_items_matched(
                ["这是一条测试消息", "DW-A3F19C 手机 499 元"], ["DW-A3F19C"]
            )
        )

    def test_partial_expectation_is_not_matched(self) -> None:
        self.assertFalse(
            self.module.expected_items_matched(["DW-A3F19C 手机 499 元"], ["DW-A3F19C", "DW-B7"])
        )

    def test_reordered_characters_are_not_matched(self) -> None:
        # 字符齐全但顺序错：不得判中（这正是不能用 char_multiset 的原因）。
        self.assertFalse(self.module.expected_items_matched(["F19C-A3DW"], ["DW-A3F19C"]))
        self.assertTrue(self.module.expected_items_matched(["DW-A3F19C"], ["DW-A3F19C"]))

    def test_expectation_across_ocr_lines_still_matches(self) -> None:
        # OCR 把同一编号折成两行时仍算命中（match_normalize 去空白，容忍 OCR 折行）。
        self.assertTrue(
            self.module.expected_items_matched(["DW-A3", "F19C"], ["DW-A3F19C"])
        )

    def test_fullwidth_and_case_differences_still_match(self) -> None:
        self.assertTrue(
            self.module.expected_items_matched(["ｄｗ－ａ３ｆ１９ｃ"], ["DW-A3F19C"])
        )
        self.assertTrue(self.module.expected_items_matched(["DW - A3F19C"], ["dw-a3f19c"]))

    def test_empty_expectation_never_matches(self) -> None:
        self.assertFalse(self.module.expected_items_matched(["任意文字"], []))
        self.assertFalse(self.module.expected_items_matched(["任意文字"], ["   "]))
        self.assertEqual(self.module.normalize_expected_items(["   ", ""]), [])

    def test_expect_digest_does_not_leak_plaintext(self) -> None:
        digest = self.module.expect_sha256(["DW-A3F19C"])
        self.assertEqual(len(digest), 64)
        self.assertNotIn("DW-A3F19C", digest)
        # 全角/大小写差异归一后摘要相同，便于事后核对同一编号。
        self.assertEqual(digest, self.module.expect_sha256(["ｄｗ－ａ３ｆ１９ｃ"]))
        self.assertNotEqual(digest, self.module.expect_sha256(["DW-A3F19D"]))


class SampleVerdictTests(unittest.TestCase):
    """一次采样要同时满足“覆盖成立 + 期望命中”才算端到端成功。"""

    def setUp(self) -> None:
        self.module = load_module("occluded_new_text_check")

    def test_covered_and_matched_is_conclusive_success(self) -> None:
        verdict = self.module.evaluate_sample(
            _covered(),
            expected=["DW-A3F19C"],
            texts=["DW-A3F19C 手机 499 元"],
            new_line_count=1,
        )
        self.assertTrue(verdict["coverage_ok"])
        self.assertTrue(verdict["expected_text_matched"])
        self.assertTrue(verdict["success"])
        self.assertTrue(verdict["conclusive"])
        self.assertEqual(verdict["success_basis"], "expected_text")

    def test_covered_but_partial_expectation_is_not_success(self) -> None:
        verdict = self.module.evaluate_sample(
            _covered(),
            expected=["DW-A3F19C", "DW-B7"],
            texts=["DW-A3F19C 手机 499 元"],
            new_line_count=3,
        )
        self.assertFalse(verdict["expected_text_matched"])
        self.assertFalse(verdict["success"])
        self.assertFalse(verdict["conclusive"])

    def test_coverage_lost_disqualifies_text_hit(self) -> None:
        # 文字命中了，但这一轮覆盖已失效：不能算成功，也不能算 conclusive。
        verdict = self.module.evaluate_sample(
            _not_covered(),
            expected=["DW-A3F19C"],
            texts=["DW-A3F19C 手机 499 元"],
            new_line_count=1,
        )
        self.assertFalse(verdict["coverage_ok"])
        self.assertTrue(verdict["expected_text_matched"])
        self.assertFalse(verdict["success"])
        self.assertFalse(verdict["conclusive"])

    def test_new_line_only_basis_is_never_conclusive(self) -> None:
        verdict = self.module.evaluate_sample(
            _covered(), expected=[], texts=["随便一条新消息"], new_line_count=1
        )
        self.assertEqual(verdict["success_basis"], "new_line_only")
        self.assertTrue(verdict["success"])
        self.assertFalse(verdict["conclusive"], "无 --expect 时不得判为 conclusive")
        self.assertFalse(verdict["expected_text_matched"])

    def test_new_line_only_without_new_lines_is_not_success(self) -> None:
        verdict = self.module.evaluate_sample(
            _covered(), expected=[], texts=["旧消息"], new_line_count=0
        )
        self.assertFalse(verdict["success"])
        self.assertFalse(verdict["conclusive"])

    def test_uncovered_sample_with_new_lines_is_not_success(self) -> None:
        verdict = self.module.evaluate_sample(
            _not_covered(), expected=[], texts=["随便一条新消息"], new_line_count=1
        )
        self.assertFalse(verdict["success"])


class ProbeArgumentTests(unittest.TestCase):
    def test_expect_is_repeatable_and_optional(self) -> None:
        module = load_module("occluded_new_text_check")
        self.assertEqual(module.parse_args([]).expect, [])
        parsed = module.parse_args(["--expect", "DW-A3F19C", "--expect", "DW-B7"])
        self.assertEqual(parsed.expect, ["DW-A3F19C", "DW-B7"])

    def test_capture_timeout_defaults_to_production(self) -> None:
        module = load_module("occluded_new_text_check")
        self.assertEqual(module.parse_args([]).capture_timeout, 5.0)


class TimeoutConclusionTests(unittest.TestCase):
    """超时退出码：没有采样=采集失败(1)；有采样没命中=inconclusive(0)。"""

    def setUp(self) -> None:
        self.module = load_module("occluded_new_text_check")

    def test_no_sample_is_capture_failure(self) -> None:
        outcome = self.module.timeout_conclusion(0, ["DW-A3F19C"])
        self.assertEqual(outcome["exit_code"], 1)
        self.assertIn("没有任何一次成功采样", outcome["aborted"])
        self.assertIsNone(outcome["inconclusive"])

    def test_samples_without_expectation_are_inconclusive(self) -> None:
        outcome = self.module.timeout_conclusion(3, [])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertIn("用户未在该群发送新消息", outcome["inconclusive"])

    def test_samples_without_expected_text_are_inconclusive(self) -> None:
        outcome = self.module.timeout_conclusion(3, ["DW-A3F19C"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertIn("未在覆盖期间识别到期望文字", outcome["inconclusive"])


if __name__ == "__main__":
    unittest.main()
