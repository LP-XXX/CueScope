"""脱敏 JSONL 回放评估，不访问桌面或真实飞书。"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import OcrObservation, Rect, TextBlock
from .normalize import content_digest
from .rules import RuleEngine
from .storage import Storage
from .tracking import FrameTracker


@dataclass(slots=True)
class ReplayReport:
    frames: int = 0
    invalid_frames: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    errors: list[dict[str, int | str]] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else None

    @property
    def precision(self) -> float | None:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else None


class ReplayRunner:
    def __init__(self, keywords: list[str], cooldown_seconds: int = 300) -> None:
        self.keywords = keywords
        self.cooldown_seconds = cooldown_seconds

    def run(self, dataset: Path) -> ReplayReport:
        report = ReplayReport()
        tracker = FrameTracker()
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "replay.db")
            rules = RuleEngine(storage, self.keywords, self.cooldown_seconds)
            with dataset.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        labels = raw["expected_notifications"]
                        if not isinstance(labels, list) or not all(isinstance(text, str) for text in labels):
                            raise ValueError("每帧必须有显式人工标签列表，包括负例的空列表")
                        blocks = [
                            TextBlock(
                                text=item["text"],
                                confidence=float(item.get("confidence", 1.0)),
                                box=Rect(**item["box"]),
                            )
                            for item in raw["blocks"]
                        ]
                        timestamp = float(raw["timestamp"])
                        observation = OcrObservation(
                            region_id=raw["region_id"],
                            sampled_at=timestamp,
                            frame_sequence=line_number,
                            blocks=blocks,
                        )
                        candidates = tracker.process(observation)
                        events = rules.process(
                            candidates, raw.get("alias", raw["region_id"]), now=timestamp
                        )
                        expected = Counter(
                            content_digest(text)
                            for text in labels
                        )
                        predicted = Counter(event.dedupe_key for event in events)
                        report.frames += 1
                        report.true_positives += sum((expected & predicted).values())
                        report.false_positives += sum((predicted - expected).values())
                        report.false_negatives += sum((expected - predicted).values())
                        if expected != predicted:
                            report.errors.append(
                                {
                                    "line": line_number,
                                    "missing": sum((expected - predicted).values()),
                                    "unexpected": sum((predicted - expected).values()),
                                }
                            )
                    except Exception as error:
                        report.invalid_frames += 1
                        report.errors.append({"line": line_number, "error": type(error).__name__})
            storage.close()
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 OCR 回放，不发送真实通知")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--keywords", required=True, help="逗号分隔的字面关键词")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = ReplayRunner([item for item in args.keywords.split(",") if item]).run(args.dataset)
    valid = report.frames > 0 and report.invalid_frames == 0
    result = asdict(report) | {
        "recall": report.recall, "precision": report.precision, "dataset_valid": valid,
        "note": "无正例或无预测时，相应比率为null，不能视为100%通过；标签来源须人工核实。",
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    if not valid or report.false_positives or report.false_negatives:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
