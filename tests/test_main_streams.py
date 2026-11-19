"""入口的标准流守卫（窗口化打包 console=False）。

``sys.stdout`` / ``sys.stderr`` 为 ``None`` 时，``argparse`` 的 ``--help``、参数错误
与任何 ``print`` 都会抛 ``AttributeError``——用户双击图标后“什么都没发生”。
这里锁住两条：守卫本身有效，且它必须在 ``argparse`` 之前执行。
"""

from __future__ import annotations

import argparse
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from deal_watch.__main__ import _ensure_standard_streams

ROOT = Path(__file__).resolve().parents[1]


class StandardStreamGuardTests(unittest.TestCase):
    def test_none_streams_are_replaced_and_printing_works(self):
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            _ensure_standard_streams()
            self.assertIsNotNone(sys.stdout)
            self.assertIsNotNone(sys.stderr)
            # 真实场景：任何 print 都不该再炸。
            print("窗口化打包下也应该能走完")
            print("stderr 同样", file=sys.stderr)

    def test_argparse_help_does_not_raise_attribute_error(self):
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            _ensure_standard_streams()
            parser = argparse.ArgumentParser(description="线报监控助手", add_help=True)
            with self.assertRaises(SystemExit) as context:
                parser.parse_args(["--help"])
            self.assertEqual(context.exception.code, 0)

    def test_existing_streams_are_left_alone(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            _ensure_standard_streams()
            self.assertIs(sys.stdout, stdout)
            self.assertIs(sys.stderr, stderr)

    def test_only_the_missing_stream_is_replaced(self):
        stderr = io.StringIO()
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", stderr):
            _ensure_standard_streams()
            self.assertIsNotNone(sys.stdout)
            self.assertIs(sys.stderr, stderr)

    def test_guard_runs_before_argparse(self):
        text = (ROOT / "src" / "deal_watch" / "__main__.py").read_text(encoding="utf-8")
        body = text[text.index("def main()") :]
        self.assertIn("_ensure_standard_streams()", body)
        self.assertLess(
            body.index("_ensure_standard_streams()"),
            body.index("argparse.ArgumentParser("),
            "标准流守卫必须在 argparse 之前执行",
        )


if __name__ == "__main__":
    unittest.main()
