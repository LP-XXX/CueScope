from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from deal_watch.replay import ReplayRunner


def frame(region, timestamp, texts, expected=None):
    return {
        "region_id": region,
        "alias": region,
        "timestamp": timestamp,
        "blocks": [
            {"text": text, "box": {"x": 10, "y": 10 + index * 60, "width": 240, "height": 25}}
            for index, text in enumerate(texts)
        ],
        "expected_notifications": expected or [],
    }


class ReplayTests(unittest.TestCase):
    def evaluate(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            return ReplayRunner(["手机"]).run(path)

    def test_empty_or_negative_only_dataset_does_not_claim_perfect_recall(self):
        for rows in ([], [frame("a", 1000, ["普通聊天"])]):
            report = self.evaluate(rows)
            self.assertIsNone(report.recall)
            self.assertIsNone(report.precision)

    def test_unlabeled_frame_is_invalid_instead_of_negative_example(self):
        row = frame("a", 1000, ["普通聊天"])
        row.pop("expected_notifications")
        report = self.evaluate([row])
        self.assertEqual(report.frames, 0)
        self.assertEqual(report.invalid_frames, 1)
        self.assertIsNone(report.recall)

    def test_annotated_replay_metrics_and_cross_group_duplicate(self):
        rows = [
            frame("a", 1000, ["普通"]),
            frame("b", 1000, ["普通"]),
            frame("a", 1001, ["普通", "手机 4999 元"], ["手机 4999 元"]),
            frame("b", 1002, ["普通", "手机 4999 元"], []),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.jsonl"
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            report = ReplayRunner(["手机"]).run(path)
        self.assertEqual(report.frames, 4)
        self.assertEqual(report.true_positives, 1)
        self.assertEqual(report.false_positives, 0)
        self.assertEqual(report.false_negatives, 0)
        self.assertEqual(report.recall, 1.0)
        self.assertEqual(report.precision, 1.0)


if __name__ == "__main__":
    unittest.main()
