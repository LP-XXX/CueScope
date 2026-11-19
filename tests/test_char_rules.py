from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deal_watch.config import ConfigStore
from deal_watch.models import OcrObservation, Rect, TextBlock
from deal_watch.normalize import char_rule_normalize, contains_all_chars
from deal_watch.rules import RuleEngine
from deal_watch.storage import Storage
from deal_watch.tracking import FrameTracker


def observation(texts: list[str], at: float, region: str = "r1"):
    return OcrObservation(
        region_id=region,
        sampled_at=at,
        frame_sequence=int(at),
        blocks=[
            TextBlock(text=text, confidence=0.99, box=Rect(10, 10 + index * 70, 260, 26))
            for index, text in enumerate(texts)
        ],
    )


class CharRuleTests(unittest.TestCase):
    def test_rule_normalization_and_repeated_counts(self):
        self.assertEqual(char_rule_normalize("坦 博 尔！羽绒服"), "坦博尔羽绒服")
        self.assertTrue(contains_all_chars("坦博尔饼干羽绒服", "坦博尔羽绒服"))
        self.assertFalse(contains_all_chars("坦博尔羽服", "坦博尔羽绒服"))
        self.assertTrue(contains_all_chars("AaBb 123", "AB12"))
        self.assertFalse(contains_all_chars("AB", "AAB"))

    def test_multiple_keywords_merge_into_one_event(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "rules.db")
            rules = RuleEngine(storage, ["坦博尔", "羽绒服"])
            tracker = FrameTracker()
            tracker.process(observation(["普通"], 1))
            changed = tracker.process(observation(["普通", "坦博尔饼干羽绒服"], 2))
            events = rules.process(changed, "群 A", now=2)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].matched_keywords, ["坦博尔", "羽绒服"])
            self.assertEqual(events[0].keyword, "坦博尔、羽绒服")
            storage.close()

    def test_characters_in_different_messages_are_not_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "split.db")
            rules = RuleEngine(storage, ["坦博尔羽绒服"])
            tracker = FrameTracker()
            tracker.process(observation(["坦博尔"], 1))
            first = tracker.process(observation(["坦博尔", "饼干"], 2))
            second = tracker.process(observation(["坦博尔", "饼干", "羽绒服"], 3))
            self.assertEqual(rules.process(first, "群 A", now=2), [])
            # “坦博尔”和“羽绒服”分属两条消息，不能拼字命中。
            self.assertEqual(rules.process(second, "群 A", now=3), [])
            storage.close()

    def test_character_rule_only_uses_literal_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "model.db")
            rules = RuleEngine(storage, ["误报词", "漏报词"])
            tracker = FrameTracker()
            tracker.process(observation(["普通"], 1))
            changed = tracker.process(observation(["普通", "误报词 隐藏组合 漏报词"], 2))
            events = rules.process(changed, "群 A", now=2)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].matched_keywords, ["误报词", "漏报词"])
            changed = tracker.process(observation(["普通", "误报词 隐藏组合 漏报词", "隐藏组合"], 3))
            events = rules.process(changed, "群 A", now=3)
            self.assertEqual(events, [])
            storage.close()


class ConfigMigrationTests(unittest.TestCase):
    def test_v1_absolute_regions_require_recalibration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                """{
                  "version": 1,
                  "regions": [{
                    "alias": "旧群", "window_title": "微信",
                    "rect": {"x": 10, "y": 20, "width": 100, "height": 200}
                  }],
                  "keywords": ["手机"],
                  "poll_interval_ms": 500
                }""",
                encoding="utf-8",
            )
            config = store.load()
            self.assertEqual(config.version, 4)
            self.assertEqual(config.poll_interval_ms, 500)
            self.assertEqual(config.match_mode, "char_multiset")
            self.assertTrue(config.regions[0].needs_calibration)
            self.assertEqual(config.regions[0].coordinate_space, "desktop")
            self.assertEqual(config.regions[0].window_hwnd, 0)
            store.save(config)
            self.assertIn('"version": 4', store.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
