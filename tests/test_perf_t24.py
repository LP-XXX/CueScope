"""0.2.2 T2.4「减少局部计算」的等价性证据与前后对照。

本项是**纯性能改动**：任何判定结果的改变都是缺陷，不是优化。因此这个文件的重心
不是"快了多少"，而是**"输出是否逐条相同"**：

* ``TrackingMatchEquivalenceTests`` 在文件里写一份**旧逻辑的参考实现**
  （``_match_reference``），用随机批 + 一组对抗样本断言
  ``FrameTracker._match(...) == _match_reference(...)``。这是本项的唯一硬门槛。
* ``MaskTests`` 锁住 ``ImageBlockFilter.mask`` 的两个契约：**空 boxes 不扫中位数**
  （省掉的那次整帧 median）与**永远返回拷贝**（下游可能原地写）。
* ``MatchBoundaryTests`` 把三个阈值（``0.62`` / ``45.0`` / ``2.5``）的边界各钉一条，
  防止有人"顺手"把 ``<`` 写成 ``<=``。
* ``MicroBenchTests`` 是 40×40 规模的前后对照，默认跳过（``DEALWATCH_BENCH=1`` 才跑），
  因为它是取证而不是回归——数字随机器变化，不能当门限。
"""

from __future__ import annotations

import os
import random
import statistics
import time
import unittest
from contextlib import contextmanager
from difflib import SequenceMatcher
from unittest.mock import patch

import numpy as np

from deal_watch import tracking as tracking_module
from deal_watch.models import MessageCandidate, Rect
from deal_watch.normalize import dedupe_normalize
from deal_watch.ocr import ImageBlockFilter
from deal_watch.tracking import FrameTracker

# ---------------------------------------------------------------------------
# 旧逻辑参考实现（改前的 ``_match`` 原样，只把 SequenceMatcher 的取用改成模属性，
# 这样"调用次数"可以用同一处打桩数到两条路径上）
# ---------------------------------------------------------------------------


def _match_reference(
    previous: list[MessageCandidate],
    current: list[MessageCandidate],
    dx: float,
    dy: float,
) -> dict[int, int]:
    """0.2.1 / 0.2.2-T2.4 改前的 ``_match``：每一对都先算文本相似度。

    刻意保持原始写法（包括把 ``dedupe_normalize(new.text)`` 放在内层循环里重复
    计算），因为它的作用就是"标准答案"——任何为了让参考实现看起来更漂亮而做的
    重排，都会让等价性断言失去意义。
    """
    scored: list[tuple[float, int, int]] = []
    for old_index, old in enumerate(previous):
        old_text = dedupe_normalize(old.text)
        old_x, old_y = old.box.center
        for new_index, new in enumerate(current):
            new_text = dedupe_normalize(new.text)
            text_score = tracking_module.SequenceMatcher(None, old_text, new_text).ratio()
            if min(len(old_text), len(new_text)) >= 4 and (
                old_text.startswith(new_text) or new_text.startswith(old_text)
            ):
                text_score = max(text_score, 0.93)
            new_x, new_y = new.box.center
            distance = abs(old_x + dx - new_x) + abs(old_y + dy - new_y)
            tolerance = max(45.0, (old.box.height + new.box.height) * 2.5)
            position_score = max(0.0, 1.0 - distance / tolerance)
            score = text_score * 0.8 + position_score * 0.2
            if text_score >= 0.62 and distance <= tolerance:
                scored.append((score, old_index, new_index))
    matches: dict[int, int] = {}
    used_new: set[int] = set()
    for _, old_index, new_index in sorted(scored, reverse=True):
        if old_index in matches or new_index in used_new:
            continue
        matches[old_index] = new_index
        used_new.add(new_index)
    return matches


def message(text: str, *, x: int = 0, y: int = 0, width: int = 200, height: int = 20, order: int = 0):
    """造一条候选消息；``order`` 只用来让 first_seen 递增，不影响匹配。"""
    return MessageCandidate(
        region_id="r1",
        text=text,
        box=Rect(x, y, width, height),
        first_seen=float(order),
        last_seen=float(order),
    )


def messages(count: int, *, texts: list[str], y_step: int = 30, height: int = 20):
    """按纵向堆叠造 ``count`` 条消息，文本从 ``texts`` 里循环取。"""
    return [
        message(texts[index % len(texts)], y=index * y_step, height=height, order=index)
        for index in range(count)
    ]


def assert_same_matches(case: unittest.TestCase, previous, current, dx, dy, label: str = ""):
    """新实现与参考实现在同一输入下必须给出**完全相同**的映射。"""
    expected = _match_reference(previous, current, dx, dy)
    actual = FrameTracker._match(previous, current, dx, dy)
    case.assertEqual(actual, expected, f"{label}：匹配结果与旧实现不一致（{actual} != {expected}）")
    return actual


# ---------------------------------------------------------------------------
# 4.3.1 等价性：随机批 + 对抗样本
# ---------------------------------------------------------------------------

#: 对抗/边界样本清单。报告里要贴的就是这张表。
ADVERSARIAL_LABELS = [
    "空 vs 空",
    "单条 vs 单条（同文同位）",
    "10 条同文不同位置",
    "只差一个字",
    "相隔 3000 像素（远超 tolerance）",
    "距离恰等于 tolerance（必须接受）",
    "距离恰等于 tolerance+1（必须拒绝）",
    "text_score 恰为 0.62（必须接受）",
    "text_score 略低于 0.62（必须拒绝）",
    "两条 old × 两条 new 得分完全相同（索引定序）",
    "重叠区域边界（相邻矩形）",
    "dx/dy 非零（滚动位移）",
    "previous 空、current 非空",
    "previous 非空、current 空",
    "文本长度 < 4（不触发 0.93 提升）",
    "前缀关系触发 0.93 提升",
    "重复文本的一对多",
    "权重微调会换绑的排名对抗（0.8/0.2 vs 0.7/0.3）",
    "前缀提升值微调会换绑的排名对抗（0.93 vs 0.92）",
]

#: 排名对抗用的公共素材：一条 43 字（归一化后）的"新消息"。
NEW_TEXT = "手机 优惠 4999 元 包邮 第二件半价 限时 抢购 到货 通知 数量有限 先到先得"
#: 不参与归一化变换、也不出现在 ``NEW_TEXT`` 里的填充字，用来精确控制 ratio。
JUNK = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"


def raw_ratio(left: str, right: str) -> float:
    """不做前缀提升的 ``SequenceMatcher.ratio()``。"""
    return SequenceMatcher(
        None, dedupe_normalize(left), dedupe_normalize(right)
    ).ratio()


def text_score(old_text: str, new_text: str) -> float:
    """复算 ``text_score``（含 0.93 前缀提升），给排名对抗用例做前置自检。

    刻意在测试里再写一遍这个公式，而不是调 ``_match`` 反推：前置自检要能独立
    发现"构造失效"，如果它和被测算代码共用同一条路径，就又是一个"一起错"。
    """
    old_normalized = dedupe_normalize(old_text)
    new_normalized = dedupe_normalize(new_text)
    score = SequenceMatcher(None, old_normalized, new_normalized).ratio()
    if min(len(old_normalized), len(new_normalized)) >= 4 and (
        old_normalized.startswith(new_normalized) or new_normalized.startswith(old_normalized)
    ):
        score = max(score, 0.93)
    return score


def position_score(distance: float, tolerance: float = 45.0) -> float:
    """复算 ``position_score``；高度 8 时 ``max(45.0, (8+8)*2.5) == 45``。"""
    return max(0.0, 1.0 - distance / tolerance)


class TrackingMatchEquivalenceTests(unittest.TestCase):
    """硬门槛：新 ``_match`` 与旧实现的输出逐条相同。"""

    def test_empty_inputs(self):
        self.assertEqual(assert_same_matches(self, [], [], 0.0, 0.0, ADVERSARIAL_LABELS[0]), {})
        self.assertEqual(
            assert_same_matches(self, [message("你好")], [], 0.0, 0.0, ADVERSARIAL_LABELS[13]), {}
        )
        self.assertEqual(
            assert_same_matches(self, [], [message("你好")], 0.0, 0.0, ADVERSARIAL_LABELS[12]), {}
        )

    def test_single_pair(self):
        previous = [message("手机 优惠 4999 元")]
        current = [message("手机 优惠 4999 元")]
        self.assertEqual(
            assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[1]), {0: 0}
        )

    def test_many_identical_texts(self):
        texts = ["手机 优惠 4999 元"] * 10
        previous = messages(10, texts=texts)
        current = messages(10, texts=texts)
        assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[2])

    def test_one_character_differs(self):
        previous = messages(5, texts=["手机 优惠 4999 元"])
        current = messages(5, texts=["手机 优惠 4998 元"])
        assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[3])

    def test_far_apart_texts_are_not_matched(self):
        previous = [message("这是一条很长的线报内容 4999 元", y=0)]
        current = [message("这是完全不一样的一行字 8888 元", y=3000)]
        self.assertEqual(
            assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[4]), {}
        )

    def test_distance_exactly_tolerance_is_accepted(self):
        """距离恰等于 tolerance（45 或 (h1+h2)*2.5，两者取大）→ 必须接受。"""
        # 高度 10 → (10+10)*2.5 = 50 > 45，tolerance = 50。
        previous = [message("同样的文字 ABCDEF", y=0, height=10)]
        current = [message("同样的文字 ABCDEF", y=50, height=10)]
        result = assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[5])
        self.assertEqual(result, {0: 0}, "距离恰等于 tolerance 必须接受")

    def test_distance_just_over_tolerance_is_rejected(self):
        previous = [message("同样的文字 ABCDEF", y=0, height=10)]
        current = [message("同样的文字 ABCDEF", y=51, height=10)]
        self.assertEqual(
            assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[6]),
            {},
            "距离超过 tolerance 一个像素必须拒绝",
        )

    def test_text_score_exactly_threshold_is_accepted(self):
        """``ratio() == 0.62``（62/100）→ ``>= 0.62`` 成立，必须接受。"""
        previous = [message("A" * 31 + "B" * 19)]
        current = [message("A" * 31 + "C" * 19)]
        self.assertEqual(
            SequenceMatcher(None, dedupe_normalize(previous[0].text),
                            dedupe_normalize(current[0].text)).ratio(),
            0.62,
            "构造失败：样本的文本相似度不是恰好 0.62",
        )
        result = assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[7])
        self.assertEqual(result, {0: 0}, "text_score 恰为 0.62 必须接受")

    def test_text_score_just_below_threshold_is_rejected(self):
        previous = [message("A" * 30 + "B" * 20)]
        current = [message("A" * 30 + "C" * 20)]
        self.assertEqual(
            assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[8]), {}
        )

    def test_ties_are_resolved_by_index_not_by_insertion_order(self):
        """两条 old × 两条 new 得分完全相同：只能靠索引定序，插入顺序不得影响结果。

        old0/old1 文本与位置都相同，new0/new1 亦然 → 四个组合的 score 全等。
        ``sorted(reverse=True)`` 按整个元组比较，(score, 1, 1) 先于 (score, 1, 0)，
        因此结果必须是 ``{1: 1, 0: 0}``；若实现改成"边算边匹配"或用了稳定排序以外
        的策略，这里就会漂移。
        """
        previous = [message("相同的一句话", y=0), message("相同的一句话", y=0)]
        current = [message("相同的一句话", y=0), message("相同的一句话", y=0)]
        result = assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[9])
        self.assertEqual(result, {1: 1, 0: 0}, "完全同分时的定序结果与旧实现不同")

    def test_adjacent_boxes_at_tolerance_boundary(self):
        """相邻矩形（共享一条边）也要给出与旧实现相同的结果。"""
        previous = [message("边界测试文本一", y=0), message("边界测试文本二", y=50)]
        current = [message("边界测试文本一", y=25), message("边界测试文本二", y=75)]
        assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[10])

    def test_nonzero_shift(self):
        previous = messages(6, texts=["滚动 测试 内容 甲乙丙", "另一条 滚动 内容 丁戊己"])
        current = messages(6, texts=["滚动 测试 内容 甲乙丙", "另一条 滚动 内容 丁戊己"])
        assert_same_matches(self, previous, current, 0.0, -120.0, ADVERSARIAL_LABELS[11])
        assert_same_matches(self, previous, current, -30.0, 45.0, ADVERSARIAL_LABELS[11])

    def test_short_texts_do_not_get_the_prefix_boost(self):
        """长度 < 4 时 0.93 提升不生效；两侧都必须给出相同结论。"""
        previous = [message("AB", y=0)]
        current = [message("ABC", y=0)]
        assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[14])

    def test_prefix_boost_applies(self):
        previous = [message("ABCD", y=0)]
        current = [message("ABCDEFGH", y=0)]
        self.assertEqual(
            assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[15]), {0: 0}
        )

    def test_one_old_to_many_new_with_same_text(self):
        previous = [message("重复的文本 1234", y=0)]
        current = [message("重复的文本 1234", y=index * 40) for index in range(4)]
        assert_same_matches(self, previous, current, 0.0, 0.0, ADVERSARIAL_LABELS[16])

    def test_weight_change_flips_the_winner(self):
        """**排名**对抗：0.8 / 0.2 一改就换绑。

        变异验证（team-lead 的 M5）：把 ``text_score * 0.8 + position_score * 0.2``
        改成 ``* 0.7 + * 0.3``，本条必须失败。

        为什么需要这条：``0.8`` / ``0.2`` 只影响**排序**，不影响"是否入选"，所以
        "入选/不入选"的边界用例对它完全无感。只有当**一条新消息能被两条旧轨迹
        认领、且两者得分接近到权重微调就能翻盘**时，它才有可观测差异 —— 那正是
        线上会绑定到不同 ``tracking_id``、进而影响去重（漏报或多发）的场景。

        参数是反解出来的（扫 tolerance / 距离 / 文本切分长度，使现行 0.8/0.2 下
        a 胜且差距 < 0.01，同时 0.7/0.3 下 b 胜）。实际分数：

        * ``ta=0.863636``（``NEW_TEXT[:38] + JUNK[:7]``）
        * ``tb=0.835165``（``NEW_TEXT[:38] + JUNK[:10]``）
        * ``pa=0.911111``（距离 4px）``pb=1.0``（距离 0）
        * 现行 a=0.873131 b=0.868132 → **a 胜**（差 0.005）
        * 0.7/0.3 a=0.877879 b=0.884615 → **b 胜**
        """
        old_a = message(NEW_TEXT[:38] + JUNK[:7], y=4, height=8)  # 文本强、位置差 4px
        old_b = message(NEW_TEXT[:38] + JUNK[:10], y=0, height=8)  # 文本弱、位置完美
        current = message(NEW_TEXT, y=0, height=8)

        score_a_text = text_score(old_a.text, NEW_TEXT)
        score_b_text = text_score(old_b.text, NEW_TEXT)
        score_a_pos = position_score(4.0)
        score_b_pos = position_score(0.0)
        current_a = score_a_text * 0.8 + score_a_pos * 0.2
        current_b = score_b_text * 0.8 + score_b_pos * 0.2
        flipped_a = score_a_text * 0.7 + score_a_pos * 0.3
        flipped_b = score_b_text * 0.7 + score_b_pos * 0.3

        # 前置自检：构造必须真的"接近到能被权重翻盘"，否则这条用例会退化成
        # 一条无意义的断言（改了文本却还绿）。
        self.assertGreater(current_a, current_b, "构造失效：现行权重下 a 应当胜出")
        self.assertLess(current_a - current_b, 0.01, "构造失效：差距过大，权重微调翻不了盘")
        self.assertLess(flipped_a, flipped_b, "构造失效：0.7/0.3 下应当换成 b 胜")

        self.assertEqual(
            FrameTracker._match([old_a, old_b], [current], 0.0, 0.0),
            {0: 0},
            "权重改成 0.7/0.3 后这条新消息会绑到 old_b 上（tracking_id 换人）",
        )

    def test_prefix_boost_value_changes_the_winner(self):
        """**排名**对抗：前缀提升值 0.93 一改就换绑。

        变异验证（team-lead 的 M6）：把 ``text_score = max(text_score, 0.93)``
        改成 ``0.92``，本条必须失败。

        构造：``old_a`` 是 ``NEW_TEXT`` 的 4 字前缀（原始 ratio 只有 0.17，**靠提升
        才够 0.62 入选**），``old_b`` 不触发提升、ratio 恰好落在 0.92 与 0.93 之间。
        两个候选位置完全相同（都距离 0），所以胜负**只由这个常数决定**。

        实际分数：

        * ``old_a`` 原始 ratio=0.170213 → 提升后 0.93
        * ``old_b`` ratio=0.926829（``NEW_TEXT[:38] + "甲"``，76/82）
        * 0.93：a=0.944000 b=0.941463 → **a 胜**
        * 0.92：a=0.936000 b=0.941463 → **b 胜**
        """
        old_a = message(NEW_TEXT[:4], y=0, height=8)
        old_b = message(NEW_TEXT[:38] + "甲", y=0, height=8)
        current = message(NEW_TEXT, y=0, height=8)

        raw_a = raw_ratio(old_a.text, NEW_TEXT)
        boosted_a = text_score(old_a.text, NEW_TEXT)
        score_b_text = text_score(old_b.text, NEW_TEXT)

        self.assertLess(raw_a, 0.62, "构造失效：a 的原始相似度必须低于入选门槛")
        self.assertEqual(boosted_a, 0.93, "构造失效：a 必须走 0.93 提升")
        self.assertGreater(score_b_text, 0.92, "构造失效：b 的 ratio 必须高于 0.92")
        self.assertLess(score_b_text, 0.93, "构造失效：b 的 ratio 必须低于 0.93")

        score_b = score_b_text * 0.8 + position_score(0.0) * 0.2
        self.assertGreater(
            0.93 * 0.8 + position_score(0.0) * 0.2, score_b, "构造失效：0.93 下 a 应胜"
        )
        self.assertLess(
            0.92 * 0.8 + position_score(0.0) * 0.2, score_b, "构造失效：0.92 下应换成 b 胜"
        )

        self.assertEqual(
            FrameTracker._match([old_a, old_b], [current], 0.0, 0.0),
            {0: 0},
            "提升值改成 0.92 后这条新消息会绑到 old_b 上（tracking_id 换人）",
        )

    def test_random_batches(self):
        """20 组随机批：文本、位置、尺寸、位移都随机，含 0/1/多条与远距离噪声。"""
        rng = random.Random(20240101)
        words = ["手机", "优惠", "4999元", "线报", "包邮", "AA", "ABCD", "限时", "第二件半价"]
        for group in range(20):
            previous = []
            current = []
            for index in range(rng.randint(0, 6)):
                text = "".join(rng.choice(words) for _ in range(rng.randint(1, 3)))
                previous.append(
                    message(
                        text,
                        x=rng.randint(0, 400),
                        y=rng.randint(0, 600),
                        width=rng.randint(40, 300),
                        height=rng.randint(8, 40),
                        order=index,
                    )
                )
            for index in range(rng.randint(0, 6)):
                text = "".join(rng.choice(words) for _ in range(rng.randint(1, 3)))
                current.append(
                    message(
                        text,
                        x=rng.randint(0, 400),
                        y=rng.randint(0, 600),
                        width=rng.randint(40, 300),
                        height=rng.randint(8, 40),
                        order=index,
                    )
                )
            dx = float(rng.choice([0, 0, 5, -15, 120, -3000]))
            dy = float(rng.choice([0, 0, 10, -40, 800, -3000]))
            assert_same_matches(self, previous, current, dx, dy, f"随机批 #{group}")


# ---------------------------------------------------------------------------
# 4.3.2 / 4.3.3 / 4.3.4 mask
# ---------------------------------------------------------------------------


def _mask_reference(image: np.ndarray, boxes: list[Rect]) -> np.ndarray:
    """改前的 ``mask``：无条件先算整帧中位数。"""
    result = image.copy()
    background = np.median(result.reshape(-1, 3), axis=0).astype(np.uint8)
    for box in boxes:
        result[box.y : box.y + box.height, box.x : box.x + box.width] = background
    return result


class MaskTests(unittest.TestCase):
    """``ImageBlockFilter.mask``：空 boxes 不扫中位数，但必须仍然返回拷贝。"""

    def setUp(self) -> None:
        self.image = np.zeros((12, 16, 3), dtype=np.uint8)
        self.image[0:4, 0:4] = [10, 20, 30]
        self.image[4:8, 4:8] = [40, 50, 60]
        self.image[8:12, 8:12] = [70, 80, 90]

    def test_empty_boxes_skips_median(self):
        with patch("deal_watch.ocr.np.median", wraps=np.median) as median:
            out = ImageBlockFilter.mask(self.image, [])
        self.assertEqual(median.call_count, 0, "boxes 为空时不该再做整帧中位数")
        self.assertTrue(np.array_equal(out, self.image))

    def test_boxes_present_still_computes_median_once(self):
        box = Rect(0, 0, 4, 4)
        with patch("deal_watch.ocr.np.median", wraps=np.median) as median:
            ImageBlockFilter.mask(self.image, [box])
        self.assertEqual(median.call_count, 1, "有 boxes 时中位数必须照旧算一次")

    def test_always_returns_a_copy(self):
        out = ImageBlockFilter.mask(self.image, [])
        self.assertIsNot(out, self.image)
        self.assertFalse(np.shares_memory(out, self.image) if hasattr(np, "shares_memory") else False)
        before = self.image.copy()
        out[:] = 255  # 下游原地写
        self.assertTrue(np.array_equal(self.image, before), "mask 返回了原数组，调用方的帧被改坏")

    def test_masked_pixels_match_reference(self):
        boxes = [Rect(0, 0, 4, 4), Rect(4, 4, 4, 4)]
        actual = ImageBlockFilter.mask(self.image, boxes)
        expected = _mask_reference(self.image, boxes)
        self.assertTrue(np.array_equal(actual, expected), "有 boxes 时的遮罩结果与旧实现不同")
        # 遮罩区域确实被涂成整帧背景（中位数），不是原像素。
        self.assertFalse(np.array_equal(actual[0:4, 0:4], self.image[0:4, 0:4]))


# ---------------------------------------------------------------------------
# 4.4 微基准（默认跳过）
# ---------------------------------------------------------------------------

BENCH_PREVIOUS = 40
BENCH_CURRENT = 40
BENCH_REPEAT = 20


@contextmanager
def counting_sequence_matcher():
    """把模块属性换成计数子类：``_match`` 与 ``_match_reference`` 都走这一处。"""
    original = tracking_module.SequenceMatcher
    counter = {"calls": 0}

    class Counting(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            counter["calls"] += 1
            super().__init__(*args, **kwargs)

    tracking_module.SequenceMatcher = Counting
    try:
        yield counter
    finally:
        tracking_module.SequenceMatcher = original


def _bench_batch() -> tuple[list[MessageCandidate], list[MessageCandidate]]:
    """四窗 × 每窗约 10 条可见消息的规模：40 条 previous × 40 条 current。

    只有约 10% 的消息是"同一位置同一文本"（滚动后仍在视野内），其余是位置或
    文本变化——这正是改造要解决的场景：绝大多数配对在图像上离得很远。
    """
    base = [
        "手机 优惠 4999 元 包邮",
        "限时 第二件半价 线报",
        "AA 团购 到货通知",
        "求 一张 显卡 3060",
        "出 二手 显示器 27 寸",
    ]
    previous = [
        message(base[index % len(base)] + f" 编号{index}", y=index * 34, height=28, order=index)
        for index in range(BENCH_PREVIOUS)
    ]
    current = [
        message(base[(index + 3) % len(base)] + f" 编号{index}", y=index * 34 + 2, height=28, order=index)
        for index in range(BENCH_CURRENT)
    ]
    # 让一部分真正能配上，避免基准退化成"全不匹配"。
    for index in range(0, BENCH_PREVIOUS, 10):
        current[index] = message(base[index % len(base)] + f" 编号{index}", y=index * 34 + 2, height=28)
    return previous, current


@unittest.skipUnless(
    os.environ.get("DEALWATCH_BENCH"), "取证用微基准，默认跳过；设 DEALWATCH_BENCH=1 才跑"
)
class MicroBenchTests(unittest.TestCase):
    """40×40 的前后对照：p50 耗时 + ``SequenceMatcher`` 调用次数。

    **不是门限**：数字随机器变化，这里只断言"改造后不比原来慢"这个弱方向，
    具体倍数写进交付报告。
    """

    def test_bench_report(self):
        previous, current = _bench_batch()
        for _ in range(3):  # 预热
            _match_reference(previous, current, 0.0, -3.0)
            FrameTracker._match(previous, current, 0.0, -3.0)

        with counting_sequence_matcher() as before_counter:
            _match_reference(previous, current, 0.0, -3.0)
        with counting_sequence_matcher() as after_counter:
            FrameTracker._match(previous, current, 0.0, -3.0)

        before_samples: list[float] = []
        after_samples: list[float] = []
        for _ in range(BENCH_REPEAT):
            started = time.perf_counter()
            _match_reference(previous, current, 0.0, -3.0)
            before_samples.append((time.perf_counter() - started) * 1000.0)

            started = time.perf_counter()
            FrameTracker._match(previous, current, 0.0, -3.0)
            after_samples.append((time.perf_counter() - started) * 1000.0)

        before_p50 = statistics.median(before_samples)
        after_p50 = statistics.median(after_samples)
        print(
            "\n[micro-bench] 40×40，p50：改前 "
            f"{before_p50:.3f}ms → 改后 {after_p50:.3f}ms；"
            f"SequenceMatcher 调用 {before_counter['calls']} → {after_counter['calls']}；"
            f"倍数 {before_p50 / after_p50 if after_p50 else float('inf'):.2f}×"
        )
        self.assertEqual(
            FrameTracker._match(previous, current, 0.0, -3.0),
            _match_reference(previous, current, 0.0, -3.0),
            "基准数据上两者输出必须一致",
        )
        self.assertLess(after_p50, before_p50 * 1.2, "改造后不应比原来慢")


if __name__ == "__main__":
    unittest.main()
