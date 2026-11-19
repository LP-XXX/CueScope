"""QA 独立验证（0.2.2 阶段一 D 组）：回归与“只增不改”的兼容性。

关注三件事：

1. 预览（人工门禁）不占去重名额——0.2.1 的硬约定，任何对判断入口的改动都
   最容易在这里出回归；
2. 采集组件 payload 的既有字段只增不改（老父进程 / 工具脚本仍可用）；
3. ``--self-test`` 的对外约定：stdout 单行 JSON、不打阶段行、不需要窗口。
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from deal_watch.capture_worker import encode
from deal_watch.config import AppConfig, ConfigStore
from deal_watch.models import MatchEvent, MessageCandidate, Rect, RegionConfig
from deal_watch.region_save import validate_region_config
from deal_watch.rules import RuleEngine
from deal_watch.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 0.2.1 成功负载的既有字段（新增字段只允许追加，不允许改名/删除）。
BASE_SUCCESS_FIELDS = {
    "ok",
    "width",
    "height",
    "captured_at",
    "image",
    "uniform_border",
    "ink_ratio",
}
# 0.2.2 追加字段：阶段埋点与请求编号。
NEW_SUCCESS_FIELDS = {
    "request_id",
    "stage_failed",
    "session_exit_timeout",
    "dpi_awareness",
    "dpi_request",
}


def message(text: str, *, region_id: str = "r1", tracking_id: str = "t1") -> MessageCandidate:
    now = time.time()
    return MessageCandidate(
        region_id=region_id,
        text=text,
        box=Rect(0, 0, 200, 30),
        first_seen=now,
        last_seen=now,
        tracking_id=tracking_id,
    )


class PreviewDedupeTests(unittest.TestCase):
    """D 组 21：预览只判断不落库，不得消耗跨群去重名额。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "dedupe.db")
        self.engine = RuleEngine(self.storage, ["手机"], cooldown_seconds=300)

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def test_preview_decision_does_not_consume_the_dedupe_slot(self):
        preview = message("手机 优惠 4999 元")
        decision = self.engine.decide(preview)
        self.assertTrue(decision.should_notify, "预览判断本身应当命中关键词")
        self.assertEqual(self.storage.list_events(), [], "预览判断就写了事件")
        # 预览之后自动监控第一次看到同一条消息：必须仍然能通知。
        events = self.engine.process([message("手机 优惠 4999 元", tracking_id="auto1")], "群A")
        self.assertEqual(len(events), 1, "预览占掉了去重名额，自动监控漏报")
        self.assertEqual(len(self.storage.list_events()), 1)

    def test_same_content_across_groups_is_deduped_but_price_difference_is_kept(self):
        engine = self.engine
        first = engine.process([message("手机 优惠 4999 元", region_id="r1", tracking_id="a")], "群A")
        self.assertEqual(len(first), 1)
        # 另一个群、同样的完整归一化内容：五分钟内只提醒一次。
        second = engine.process(
            [message("手机 优惠 4999 元", region_id="r2", tracking_id="b")], "群B"
        )
        self.assertEqual(second, [], "跨群同内容没有被去重")
        # 价格不同：必须保留。
        third = engine.process(
            [message("手机 优惠 3999 元", region_id="r3", tracking_id="c")], "群C"
        )
        self.assertEqual(len(third), 1, "价格差异被当成重复丢掉了")
        # 型号差异（字符集不同）也必须保留。
        fourth = engine.process(
            [message("手机 优惠 4999 元 iPhone15", region_id="r4", tracking_id="d")], "群D"
        )
        self.assertEqual(len(fourth), 1, "型号差异被当成重复丢掉了")

    def test_identical_content_is_not_requeued_within_the_same_run(self):
        engine = self.engine
        now = time.time()
        first = engine.process([message("手机 优惠 4999 元", tracking_id="same")], "群A", now=now)
        self.assertEqual(len(first), 1)
        # 同一 tracking_id 再次出现（OCR 抖动 / 静止画面重扫）不得重复入队。
        # 0.2.2 T2.3 授权变更：原来断言 engine.last_decisions["same"].queue_state，
        # 那锁的是 RuleEngine 的内部字典（实现细节，加个上限就会红）。现在改为
        # 断言**调用方可见的契约**：decide() 说应该通知，process() 不产生事件，
        # 且调用方传进去的 decision 对象被标注为 already_processed —— service 正是
        # 靠读这个字段落库的，这条链比内部字典更贴近真实行为。
        repeat_decision = engine.decide(message("手机 优惠 4999 元", tracking_id="same"))
        self.assertTrue(repeat_decision.should_notify, "同一内容仍应判定为应通知")
        again = engine.process(
            [message("手机 优惠 4999 元", tracking_id="same")],
            "群A",
            now=now + 1,
            decisions={"same": repeat_decision},
        )
        self.assertEqual(again, [], "同一消息被重复通知")
        self.assertEqual(
            repeat_decision.queue_state,
            "already_processed",
            "同一条消息第二次处理时必须标注为已处理",
        )
        self.assertTrue(repeat_decision.should_notify, "标注已处理不得篡改判定本身")
        events = self.storage.list_events()
        self.assertEqual(len(events), 1)


class PayloadCompatibilityTests(unittest.TestCase):
    """D 组 23：payload 既有字段只增不改，``--self-test`` 保持安静。"""

    def run_worker(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        return subprocess.run(
            [sys.executable, "-m", "deal_watch.capture_worker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=str(ROOT),
            env=env,
            check=False,
        )

    def test_success_payload_keeps_every_021_field(self):
        import numpy as np

        image = np.full((6, 8, 3), 200, dtype=np.uint8)
        payload = encode(image, 8, 6)
        self.assertTrue(BASE_SUCCESS_FIELDS <= set(payload), BASE_SUCCESS_FIELDS - set(payload))
        self.assertTrue(payload["ok"])
        self.assertEqual((payload["width"], payload["height"]), (8, 6))
        self.assertEqual(set(payload["uniform_border"]), {"top", "bottom", "left", "right"})
        decoded = base64.b64decode(payload["image"])
        self.assertTrue(decoded.startswith(b"\x89PNG"), "image 字段不再是 PNG")
        from PIL import Image

        with Image.open(io.BytesIO(decoded)) as picture:
            self.assertEqual(picture.size, (8, 6))

    def test_error_payload_is_a_superset_with_the_new_diagnostics(self):
        result = self.run_worker("--hwnd", "0", "--request-id", "3f9c1d2a")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["ok"])
        for field in ("ok", "backend", "error", "dpi_awareness", "dpi_request"):
            self.assertIn(field, payload, f"0.2.1 字段 {field} 被删掉了")
        for field in NEW_SUCCESS_FIELDS:
            self.assertIn(field, payload, f"0.2.2 新字段 {field} 缺失")
        self.assertEqual(payload["request_id"], "3f9c1d2a")
        self.assertTrue(payload["stage_failed"])

    def test_self_test_stays_single_json_and_silent(self):
        result = self.run_worker("--self-test")
        lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"--self-test 输出了多行：{lines}")
        payload = json.loads(lines[0])
        self.assertIn("ok", payload)
        self.assertEqual(result.stderr.strip(), "", "--self-test 往 stderr 打了阶段行")

    def test_old_cli_usage_still_works(self):
        result = self.run_worker("--hwnd", "0")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["error"], "RuntimeError: INVALID_HWND")
        self.assertEqual(payload["request_id"], "")


class SavedConfigCompatibilityTests(unittest.TestCase):
    """D 组补充：0.2.1 已保存的区域配置不得因“统一差 1 像素”的口径变化被改写。"""

    def test_saved_frame_rect_round_trips_without_being_shifted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            region = RegionConfig(
                alias="合成群",
                window_title="微信",
                rect=Rect(123, 234, 567, 678),
                window_class="WeChatMainWndForPC",
                window_hwnd=1001,
                process_id=4321,
                process_path="C:/Program Files/WeChat/WeChat.exe",
                capture_backend="wgc",
                coordinate_space="frame",
                reference_frame_width=800,
                reference_frame_height=1280,
                previewed=True,
            )
            store.save(AppConfig(regions=[region]))
            loaded = ConfigStore(Path(directory)).load().regions
            self.assertEqual(loaded[0].rect, Rect(123, 234, 567, 678), "已存矩形被改写了")
            self.assertFalse(loaded[0].needs_calibration)
            self.assertEqual(validate_region_config(loaded[0]), [])


class StorageDedupeContractTests(unittest.TestCase):
    """D 组 21：去重与冷却期语义没有被 0.2.2 的改动带偏。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "cooldown.db")

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def event(self, text: str, key: str) -> MatchEvent:
        now = time.time()
        return MatchEvent(
            region_id="r1",
            region_alias="群A",
            keyword="手机",
            text=text,
            dedupe_key=key,
            observed_at=now,
        )

    def test_cooldown_blocks_immediate_repeat_but_not_after_it_expires(self):
        now = time.time()
        self.assertTrue(self.storage.record_match(self.event("手机 4999", "k1"), 300, now))
        self.assertFalse(self.storage.record_match(self.event("手机 4999", "k1"), 300, now + 10))
        self.assertTrue(
            self.storage.record_match(self.event("手机 4999", "k1"), 300, now + 301),
            "冷却期结束后仍被当成重复",
        )


if __name__ == "__main__":
    unittest.main()
