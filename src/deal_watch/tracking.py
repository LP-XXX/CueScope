"""文字块重组与跨帧对应。首帧只建立基线，不产生候选事件。"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median
from typing import Any

from .models import MessageCandidate, OcrObservation, Rect, TextBlock
from .normalize import dedupe_normalize

_MAX_MISSING_TRACKS = 256


def blocks_to_messages(observation: OcrObservation) -> list[MessageCandidate]:
    """按显式消息边界重组普通文字；未确认边界时保守拆分，不跨块拼字。"""
    blocks = sorted(
        observation.blocks,
        key=lambda b: (b.message_id or "", b.box.y, b.box.x),
    )
    if not blocks:
        return []

    groups: list[list[TextBlock]] = []
    for block in blocks:
        if block.message_id:
            if groups and groups[-1][0].message_id == block.message_id:
                groups[-1].append(block)
            else:
                groups.append([block])
        else:
            # OCR 未取得可靠气泡边界时，每个文字块独立处理，防止相邻消息拼字。
            groups.append([block])

    messages: list[MessageCandidate] = []
    for group in groups:
        left = min(item.box.x for item in group)
        top = min(item.box.y for item in group)
        right = max(item.box.x + item.box.width for item in group)
        bottom = max(item.box.y + item.box.height for item in group)
        text = "\n".join(item.text.strip() for item in group if item.text.strip())
        if not text:
            continue
        kind = (
            "forwarded_card"
            if any(item.kind in {"forwarded_card", "image_card", "video_card"} for item in group)
            else "normal_text"
        )
        messages.append(
            MessageCandidate(
                region_id=observation.region_id,
                text=text,
                box=Rect(left, top, right - left, bottom - top),
                first_seen=observation.sampled_at,
                last_seen=observation.sampled_at,
                confidence=sum(item.confidence for item in group) / len(group),
                boundary_uncertain=False,
                kind=kind,
            )
        )
    return messages


@dataclass(slots=True)
class _Tracked:
    message: MessageCandidate
    missed: int = 0
    pixels: bytes | None = None
    changed_misses: int = 0


class FrameTracker:
    """每区域独立跟踪；滚动时先估计整体位移，再匹配文字和位置。"""

    def __init__(self) -> None:
        self._regions: dict[str, dict[str, _Tracked]] = {}
        self._baselined: set[str] = set()
        self._semantics: dict[str, tuple[str, bool]] = {}

    def reset(self, region_id: str | None = None) -> None:
        if region_id is None:
            self._regions.clear()
            self._baselined.clear()
            self._semantics.clear()
            return
        self._regions.pop(region_id, None)
        self._baselined.discard(region_id)
        self._semantics.pop(region_id, None)

    def process(
        self, observation: OcrObservation, *, image: Any = None,
        current_messages: list[MessageCandidate] | None = None,
    ) -> list[MessageCandidate]:
        current = current_messages if current_messages is not None else blocks_to_messages(observation)
        region_id = observation.region_id
        content_mode = getattr(observation, "content_mode", "wechat_chat")
        strict = bool(getattr(observation, "strict_text", False) or content_mode != "wechat_chat")
        semantics = (content_mode, strict)
        if region_id in self._semantics and self._semantics[region_id] != semantics:
            self.reset(region_id)
        self._semantics[region_id] = semantics
        tracked = self._regions.setdefault(region_id, {})
        if region_id not in self._baselined:
            for message in current:
                tracked[message.tracking_id] = _Tracked(
                    message, pixels=self._pixels(image, message.box)
                )
            self._baselined.add(region_id)
            return []

        previous = [item.message for item in tracked.values()]
        visible_previous = [item.message for item in tracked.values() if item.missed == 0]
        dx, dy = self._estimate_shift(visible_previous, current)
        matches = self._match(previous, current, dx, dy)
        changed: list[MessageCandidate] = []
        next_tracked: dict[str, _Tracked] = {}

        for old_index, new_index in matches.items():
            old = previous[old_index]
            message = current[new_index]
            message.tracking_id = old.tracking_id
            message.first_seen = old.first_seen
            # Candidate identity may tolerate OCR jitter, but exact/case-sensitive
            # rule inputs must retain changes that the legacy matcher normalizes.
            normalize = self._strict_text if strict else dedupe_normalize
            if normalize(message.text) != normalize(old.text):
                message.is_new = False
                message.is_update = True
                message.previous_text = old.text
                changed.append(message)
            next_tracked[message.tracking_id] = _Tracked(
                message, pixels=self._pixels(image, message.box)
            )

        matched_new = set(matches.values())
        for index, message in enumerate(current):
            if index not in matched_new:
                changed.append(message)
                next_tracked[message.tracking_id] = _Tracked(
                    message, pixels=self._pixels(image, message.box)
                )

        matched_old = set(matches)
        for index, old in enumerate(previous):
            if index not in matched_old:
                # 原文字区域像素没变时是漏读，不应随冷却期再次通知。
                # 像素已变且连续三次未识别到旧消息，才确认其离开；这样
                # 五分钟后真正重新出现的相同线报仍能作为新消息通知。
                prior = tracked[old.tracking_id]
                pixels = self._pixels(image, old.box)
                pixels_changed = pixels is not None and prior.pixels is not None and pixels != prior.pixels
                changed_misses = prior.changed_misses + 1 if pixels_changed else 0
                if changed_misses < 3:
                    next_tracked[old.tracking_id] = _Tracked(
                        old, prior.missed + 1, prior.pixels, changed_misses
                    )
        missing = sorted(
            (item for item in next_tracked.values() if item.missed),
            key=lambda item: item.message.last_seen,
        )
        for item in missing[: max(0, len(missing) - _MAX_MISSING_TRACKS)]:
            next_tracked.pop(item.message.tracking_id, None)
        self._regions[region_id] = next_tracked
        return changed

    @staticmethod
    def _strict_text(text: str) -> str:
        return unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _pixels(image: Any, box: Rect) -> bytes | None:
        if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
            return None
        height, width = image.shape[:2]
        crop = image[max(0, box.y):min(height, box.y + box.height),
                     max(0, box.x):min(width, box.x + box.width)]
        if not crop.size:
            return None
        # 只保存消息局部指纹，不保留旧截图，也不写入日志或数据库。
        return hashlib.blake2b(crop.tobytes(), digest_size=16).digest()

    @staticmethod
    def _estimate_shift(
        previous: list[MessageCandidate], current: list[MessageCandidate]
    ) -> tuple[float, float]:
        old_by_text: dict[str, list[MessageCandidate]] = {}
        for message in previous:
            old_by_text.setdefault(dedupe_normalize(message.text), []).append(message)
        shifts: list[tuple[float, float]] = []
        for message in current:
            candidates = old_by_text.get(dedupe_normalize(message.text), [])
            if len(candidates) == 1:
                old_center = candidates[0].box.center
                new_center = message.box.center
                shifts.append((new_center[0] - old_center[0], new_center[1] - old_center[1]))
        if not shifts:
            return 0.0, 0.0
        return median(item[0] for item in shifts), median(item[1] for item in shifts)

    @staticmethod
    def _match(
        previous: list[MessageCandidate],
        current: list[MessageCandidate],
        dx: float,
        dy: float,
    ) -> dict[int, int]:
        """给旧轨迹与新消息配对。

        0.2.2 T2.4：**先算位置门槛，再算文本相似度**。原来的内层循环对每一对
        (old, new) 都跑 ``dedupe_normalize`` + ``SequenceMatcher.ratio()``，即使两者
        在图上相隔上千像素；n 条消息就是 n×m 次归一化（其中 n×(m−1) 次是把同一条
        新消息反复归一化）加 n×m 次编辑距离。位置门槛与文本无关，可以先算。

        **输出逐条不变**（不是"基本不变"）：

        * 原实现只在 ``text_score >= 0.62 and distance <= tolerance`` 时 append，
          这里用两个 ``continue`` 提前排掉**同一批**对；
        * ``score`` 的算式、``text_score`` 的 0.93 提升条件、``tolerance`` 的表达式
          一字未改，所以进入 ``scored`` 的元组集合与数值完全相同；
        * 后面的 ``sorted(scored, reverse=True)`` 按**整个元组**比较
          （score → old_index → new_index），而 ``(old_index, new_index)`` 组合全局
          唯一，因此插入顺序不影响排序结果，``matches`` 逐条相同。

        这些阈值是用户明确要求保留的：``0.62`` / ``45.0`` / ``2.5`` / ``0.8`` / ``0.2``
        / ``0.93`` / ``min(len) >= 4``。任何一处从 ``<`` 变 ``<=`` 都可能让线上偶尔
        漏一条线报，因此等价性由 ``tests/test_perf_t24.py`` 的随机对抗用例兜住。
        """
        old_texts = [dedupe_normalize(old.text) for old in previous]
        new_texts = [dedupe_normalize(new.text) for new in current]
        old_centers = [old.box.center for old in previous]
        new_centers = [new.box.center for new in current]
        old_heights = [old.box.height for old in previous]
        new_heights = [new.box.height for new in current]

        scored: list[tuple[float, int, int]] = []
        for old_index, old_text in enumerate(old_texts):
            old_x, old_y = old_centers[old_index]
            old_height = old_heights[old_index]
            for new_index, new_text in enumerate(new_texts):
                new_x, new_y = new_centers[new_index]
                # 位置门槛先判：够远的一对直接跳过，连 SequenceMatcher 都不开。
                distance = abs(old_x + dx - new_x) + abs(old_y + dy - new_y)
                tolerance = max(45.0, (old_height + new_heights[new_index]) * 2.5)
                if distance > tolerance:
                    continue
                text_score = SequenceMatcher(None, old_text, new_text).ratio()
                if min(len(old_text), len(new_text)) >= 4 and (
                    old_text.startswith(new_text) or new_text.startswith(old_text)
                ):
                    text_score = max(text_score, 0.93)
                if text_score < 0.62:
                    continue
                position_score = max(0.0, 1.0 - distance / tolerance)
                scored.append((text_score * 0.8 + position_score * 0.2, old_index, new_index))
        matches: dict[int, int] = {}
        used_new: set[int] = set()
        for _, old_index, new_index in sorted(scored, reverse=True):
            if old_index in matches or new_index in used_new:
                continue
            matches[old_index] = new_index
            used_new.add(new_index)
        return matches
