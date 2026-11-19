from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from deal_watch.models import MessageCandidate, OcrObservation, Rect, TextBlock
from deal_watch.rules import RuleEngine
from deal_watch.storage import Storage
from deal_watch.tracking import FrameTracker, blocks_to_messages


def observation(texts: list[str], at: float, region: str = "r1", offset: int = 0):
    return OcrObservation(
        region_id=region,
        sampled_at=at,
        frame_sequence=int(at),
        blocks=[
            TextBlock(text=text, confidence=0.99, box=Rect(20, 20 + index * 70 + offset, 260, 28))
            for index, text in enumerate(texts)
        ],
    )


def block(text: str, y: int, message_id: str = "", kind: str = "text") -> TextBlock:
    return TextBlock(
        text=text, confidence=0.99, box=Rect(20, y, 260, 28),
        kind=kind, message_id=message_id,
    )


def message(text: str, kind: str = "normal_text", boundary: bool = False) -> MessageCandidate:
    return MessageCandidate(
        region_id="r1", text=text, box=Rect(20, 20, 260, 28),
        first_seen=1.0, last_seen=1.0, confidence=0.99,
        kind=kind, boundary_uncertain=boundary, tracking_id=text,
    )


class TrackingTests(unittest.TestCase):
    def test_baseline_does_not_emit_existing_keyword(self):
        tracker = FrameTracker()
        self.assertEqual(tracker.process(observation(["旧消息 关键词"], 1)), [])

    def test_new_message_emits_once(self):
        tracker = FrameTracker()
        tracker.process(observation(["旧消息"], 1))
        changed = tracker.process(observation(["旧消息", "新线报 99 元"], 2))
        self.assertEqual([item.text for item in changed], ["新线报 99 元"])
        self.assertEqual(tracker.process(observation(["旧消息", "新线报 99 元"], 3)), [])

    def test_overall_scroll_is_not_new(self):
        tracker = FrameTracker()
        tracker.process(observation(["甲", "乙", "丙"], 1))
        changed = tracker.process(observation(["甲", "乙", "丙"], 2, offset=-45))
        self.assertEqual(changed, [])

    def test_gradually_displayed_message_is_update(self):
        tracker = FrameTracker()
        tracker.process(observation(["稳定"], 1))
        first = tracker.process(observation(["稳定", "苹果手机 15 Pro 256"], 2))
        second = tracker.process(observation(["稳定", "苹果手机 15 Pro 256GB 4999元"], 3))
        self.assertTrue(first[0].is_new)
        self.assertTrue(second[0].is_update)
        self.assertEqual(first[0].tracking_id, second[0].tracking_id)

    def test_temporary_ocr_dropout_does_not_reemit_baseline(self):
        tracker = FrameTracker()
        tracker.process(observation(["手机 1.99 元"], 1))
        for at in (2, 3, 4):
            self.assertEqual(tracker.process(observation([], at)), [])
        self.assertEqual(tracker.process(observation(["手机 1.99 元"], 5)), [])

    def test_missing_track_history_is_bounded(self):
        tracker = FrameTracker()
        def text(index):
            return hashlib.sha256(str(index).encode()).hexdigest()

        tracker.process(observation([text(0)], 0))
        for index in range(1, 400):
            tracker.process(observation([text(index)], index))
        tracks = tracker._regions["r1"]
        self.assertLessEqual(len(tracks), 257)
        self.assertEqual(sum(item.missed == 0 for item in tracks.values()), 1)


class MessageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "boundary-rules.db")

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def test_same_bubble_id_keeps_multiline_message_together(self):
        observation = OcrObservation("r1", 1, 1, [
            block("坦博尔", 20, message_id="b1"),
            block("羽绒服", 48, message_id="b1"),
        ])
        candidates = blocks_to_messages(observation)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].text, "坦博尔\n羽绒服")
        self.assertFalse(candidates[0].boundary_uncertain)

    def test_adjacent_bubbles_are_not_spliced(self):
        observation = OcrObservation("r1", 1, 1, [
            block("坦博尔", 20, message_id="b1"),
            block("羽绒服", 48, message_id="b2"),
        ])
        candidates = blocks_to_messages(observation)
        self.assertEqual([item.text for item in candidates], ["坦博尔", "羽绒服"])

    def test_unknown_boundary_is_split_without_cross_block_matching(self):
        observation = OcrObservation("r1", 1, 1, [block("坦博尔", 20), block("羽绒服", 30)])
        candidates = blocks_to_messages(observation)
        self.assertEqual([item.text for item in candidates], ["坦博尔", "羽绒服"])
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertFalse(all(rules.decide(item).should_notify for item in candidates))

    def test_forwarded_card_is_excluded_before_matching(self):
        rules = RuleEngine(self.storage, ["手机"])
        decision = rules.decide(message("合并聊天记录：手机 4999 元", kind="forwarded_card"))
        self.assertFalse(decision.should_notify)
        self.assertEqual(decision.reason, "unsupported_message_type")


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "test.db")
        self.rules = RuleEngine(self.storage, ["手机", "优惠"], cooldown_seconds=300)
        self.tracker = FrameTracker()

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def test_static_ocr_dropout_beyond_cooldown_does_not_reemit(self):
        self.tracker.process(observation(["普通聊天"], 0))
        first = self.tracker.process(observation(["普通聊天", "手机 1.99 元"], 1))
        self.assertEqual(len(self.rules.process(first, "群 A", now=1)), 1)
        for at in (100, 200, 300, 400):
            self.assertEqual(self.tracker.process(observation(["普通聊天"], at)), [])
        changed = self.tracker.process(observation(["普通聊天", "手机 1.99 元"], 500))
        self.assertEqual(self.rules.process(changed, "群 A", now=500), [])
        self.assertEqual(len(self.storage.list_events()), 1)

    def test_pixels_distinguish_static_ocr_dropout_from_true_reappearance(self):
        blank = np.full((200, 300, 3), 250, dtype=np.uint8)
        shown = blank.copy()
        shown[90:118, 20:280] = 0
        self.tracker.process(observation(["普通聊天"], 0), image=blank)
        changed = self.tracker.process(
            observation(["普通聊天", "手机 1.99 元"], 1), image=shown
        )
        self.assertEqual(len(self.rules.process(changed, "群 A", now=1)), 1)
        # 超过冷却期仍显示同一内容，即使连续多次漏读也不重复发。
        for at in (100, 200, 300, 400):
            self.tracker.process(observation(["普通聊天"], at), image=shown)
        changed = self.tracker.process(
            observation(["普通聊天", "手机 1.99 元"], 500), image=shown
        )
        self.assertEqual(self.rules.process(changed, "群 A", now=500), [])
        # 确实离开画面且经复核后，新的相同消息在冷却后应正常提醒。
        for at in (510, 520, 530):
            self.tracker.process(observation(["普通聊天"], at), image=blank)
        changed = self.tracker.process(
            observation(["普通聊天", "手机 1.99 元"], 600), image=shown
        )
        self.assertEqual(len(self.rules.process(changed, "群 A", now=600)), 1)

    def test_cross_group_dedupe_and_local_duplicate_record(self):
        first = self.tracker.process(observation(["手机 4999 元"], 1, "a"))
        second = self.tracker.process(observation(["手机 4999 元"], 1, "b"))
        self.assertEqual(first, [])  # 各区域基线
        first = self.tracker.process(observation(["手机 4999 元", "手机 4999 元 新品"], 2, "a"))
        second = self.tracker.process(observation(["手机 4999 元", "手机 4999 元 新品"], 2, "b"))
        events_a = self.rules.process(first, "群 A", now=1000)
        events_b = self.rules.process(second, "群 B", now=1001)
        self.assertEqual(len(events_a), 1)
        self.assertEqual(events_b, [])
        records = self.storage.list_events()
        self.assertEqual(len(records), 2)
        self.assertTrue(any(item["state"] == "duplicate" for item in records))
        self.assertEqual(self.storage.queue_counts()["pending"], 1)

    def test_price_change_is_not_merged(self):
        self.tracker.process(observation(["手机"], 1))
        first = self.tracker.process(observation(["手机", "手机 4999 元"], 2))
        second = self.tracker.process(observation(["手机", "手机 4599 元"], 3))
        self.assertEqual(len(self.rules.process(first, "群 A", now=1000)), 1)
        self.assertEqual(len(self.rules.process(second, "群 A", now=1001)), 1)

    def test_punctuation_jitter_does_not_notify_twice(self):
        self.tracker.process(observation(["手机"], 1))
        first = self.tracker.process(observation(["手机", "手机 优惠 4999 元"], 2))
        second = self.tracker.process(observation(["手机", "手机 优惠 4999 元。"], 3))
        self.assertEqual(len(self.rules.process(first, "群 A", now=1000)), 1)
        self.assertEqual(self.rules.process(second, "群 A", now=1001), [])

    def test_baseline_whitespace_or_punctuation_jitter_never_sends_history(self):
        self.tracker.process(observation(["手机 999 元"], 1))
        for at, text in enumerate(("手机999 元", "手机 999 元。"), 2):
            changed = self.tracker.process(observation([text], at))
            self.assertEqual(self.rules.process(changed, "群 A", now=1000 + at), [])
        self.assertEqual(self.storage.list_events(), [])

    def test_same_content_can_notify_after_cooldown(self):
        self.tracker.process(observation(["手机"], 1))
        first = self.tracker.process(observation(["手机", "手机 优惠"], 2))
        self.assertEqual(len(self.rules.process(first, "群 A", now=1000)), 1)
        other = FrameTracker()
        other.process(observation(["手机"], 1, "b"))
        later = other.process(observation(["手机", "手机 优惠"], 2, "b"))
        self.assertEqual(len(self.rules.process(later, "群 B", now=1300)), 1)

    def test_decimal_and_model_punctuation_are_material_updates(self):
        self.tracker.process(observation(["普通聊天"], 1))
        for at, text in enumerate(("手机 1.99 元 AB-12", "手机 199 元 AB-12", "手机 199 元 AB12"), 2):
            changed = self.tracker.process(observation([text], at))
            self.assertEqual(len(self.rules.process(changed, "群 A", now=1000 + at)), 1)

    def test_url_case_is_preserved_in_dedupe_and_updates(self):
        self.tracker.process(observation(["普通聊天"], 1))
        for at, path in enumerate(("A1", "a1"), 2):
            changed = self.tracker.process(observation([f"手机 https://example.invalid/{path}"], at))
            self.assertEqual(len(self.rules.process(changed, "群 A", now=1000 + at)), 1)

    def test_url_case_difference_does_not_merge_between_groups(self):
        from deal_watch.normalize import content_digest

        self.assertNotEqual(
            content_digest("手机 https://example.invalid/A1?p=X"),
            content_digest("手机 https://example.invalid/a1?p=x"),
        )

    def test_character_rule_decision_records_no_model_state(self):
        rules = RuleEngine(self.storage, ["手机"], cooldown_seconds=300)
        decision = rules.decide(message("手机 4999 元"))
        self.assertTrue(decision.should_notify)
        self.assertEqual(decision.reason, "should_notify")
        self.assertEqual(decision.queue_state, "not_applicable")
        self.assertFalse(hasattr(decision, "model_score"))

    def test_decision_and_actual_queue_state_are_separate(self):
        rules = RuleEngine(self.storage, ["手机"])
        decision = rules.decide(message("手机 4999 元"))
        self.assertTrue(decision.should_notify)
        events = rules.process(
            [message("手机 4999 元")], "群 A", now=1,
            decisions={text: decision for text in ["手机 4999 元"]},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(decision.queue_state, "queued")
        self.assertEqual(decision.event_id, events[0].id)
        rules.process(
            [message("手机 4999 元")], "群 A", now=2,
            decisions={"手机 4999 元": decision},
        )
        self.assertEqual(decision.queue_state, "already_processed")


if __name__ == "__main__":
    unittest.main()
