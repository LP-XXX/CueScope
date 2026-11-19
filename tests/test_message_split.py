"""0.2.1 消息分割与图片/卡片排除的离线用例（PIL 合成微信风格版面）。

用例直接调用 find_bubbles / assign_messages / blocks_to_messages / RuleEngine，
不加载 OCR 引擎，保证 CI 可重复。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from deal_watch.models import OcrObservation, Rect, TextBlock
from deal_watch.ocr import (
    ImageBlockFilter,
    _light_card_region,
    assign_messages,
    find_bubbles,
)
from deal_watch.rules import RuleEngine
from deal_watch.storage import Storage
from deal_watch.tracking import blocks_to_messages

try:  # Pillow 只在合成用例里使用，缺失时整组跳过。
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺 Pillow
    _PIL_AVAILABLE = False

_FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
_READY = _PIL_AVAILABLE and _FONT_PATH.is_file()

PAGE = (237, 237, 237)
BUBBLE_WHITE = (255, 255, 255)
BUBBLE_GREEN = (149, 236, 105)
TEXT_COLOR = (32, 32, 32)


def _font(size: int):
    return ImageFont.truetype(str(_FONT_PATH), size)


class Layout:
    """合成版面：气泡用圆角矩形，文字块按真实字形框生成。"""

    def __init__(self, width: int = 340, height: int = 460) -> None:
        self.image = Image.new("RGB", (width, height), PAGE)
        self.draw = ImageDraw.Draw(self.image)
        self.blocks: list[TextBlock] = []

    def array(self) -> np.ndarray:
        return np.asarray(self.image)[:, :, ::-1].copy()

    def bubble(
        self, x: int, y: int, width: int, height: int, color=BUBBLE_WHITE
    ) -> Rect:
        self.draw.rounded_rectangle(
            (x, y, x + width - 1, y + height - 1), radius=8, fill=color
        )
        return Rect(x, y, width, height)

    def message(
        self,
        x: int,
        y: int,
        lines: list[str],
        *,
        color=BUBBLE_WHITE,
        size: int = 18,
        line_gap: int = 6,
        padding: int = 10,
    ) -> Rect:
        """画一个气泡并在其中写入若干行文字，返回气泡矩形。"""
        font = _font(size)
        glyph_boxes = [font.getbbox(line) for line in lines]
        heights = [box[3] - box[1] for box in glyph_boxes]
        widths = [box[2] - box[0] for box in glyph_boxes]
        text_width = max(widths)
        text_height = sum(heights) + line_gap * (len(lines) - 1)
        panel = self.bubble(
            x, y, text_width + 2 * padding, text_height + 2 * padding, color
        )
        cursor = y + padding
        for line, glyph, height, width in zip(
            lines, glyph_boxes, heights, widths, strict=True
        ):
            self.draw.text((x + padding, cursor - glyph[1]), line, font=font, fill=TEXT_COLOR)
            self.blocks.append(
                TextBlock(
                    text=line,
                    confidence=0.99,
                    box=Rect(x + padding, cursor, max(1, width), max(1, height)),
                )
            )
            cursor += height + line_gap
        return panel

    def text_on_photo(self, x: int, y: int, text: str, size: int = 18, fill=(255, 255, 255)):
        """在已有图片像素上写字，返回文字块。"""
        font = _font(size)
        glyph = font.getbbox(text)
        self.draw.text((x, y - glyph[1]), text, font=font, fill=fill)
        block = TextBlock(
            text=text,
            confidence=0.99,
            box=Rect(x, y, max(1, glyph[2] - glyph[0]), max(1, glyph[3] - glyph[1])),
        )
        self.blocks.append(block)
        return block

    def photo(self, x: int, y: int, width: int, height: int, *, gray: bool) -> None:
        rng = np.random.default_rng(7)
        if gray:
            plane = rng.integers(40, 200, size=(height, width), dtype=np.uint8)
            pixels = np.repeat(plane[:, :, None], 3, axis=2)
        else:
            pixels = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
        self.image.paste(Image.fromarray(pixels), (x, y))

    def thumbnail(self, x: int, y: int, width: int, height: int, *, gray: bool = False) -> Rect:
        """画一张小缩略图：连续色调（层级连续、唯一色多），不是文字块。"""
        rng = np.random.default_rng(11)
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        base = 128.0 + 62.0 * np.sin(xx / 6.0) + 42.0 * np.cos(yy / 7.0)
        if gray:
            channels = [base, base, base]
        else:
            channels = [base + 18.0 * np.cos(xx / 4.0), base * 0.85, base * 0.65 + 40.0]
        pixels = np.stack(channels, axis=2) + rng.normal(0.0, 3.0, (height, width, 3))
        self.image.paste(Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8)), (x, y))
        return Rect(x, y, width, height)

    def card(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        fill=(255, 255, 255),
        border=None,
    ) -> Rect:
        """画一个浅色卡片（可带 1px 边框）；常用于商品/笔记卡片版面。"""
        self.draw.rounded_rectangle(
            (x, y, x + width - 1, y + height - 1),
            radius=6,
            fill=fill,
            outline=border,
            width=1 if border else 0,
        )
        return Rect(x, y, width, height)

    def label(
        self, x: int, y: int, text: str, *, size: int = 15, fill=TEXT_COLOR
    ) -> TextBlock:
        """在已有像素上写一行文字并登记文字块（用于卡片标题/价格）。"""
        font = _font(size)
        glyph = font.getbbox(text)
        self.draw.text((x, y - glyph[1]), text, font=font, fill=fill)
        block = TextBlock(
            text=text,
            confidence=0.99,
            box=Rect(x, y, max(1, glyph[2] - glyph[0]), max(1, glyph[3] - glyph[1])),
        )
        self.blocks.append(block)
        return block


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成版面用例")
class MessageSplitTests(unittest.TestCase):
    """验收场景 1-9：分割、合并、图片/卡片排除。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "split.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def observe(self, layout: Layout) -> OcrObservation:
        blocks, uncovered = assign_messages(layout.array(), layout.blocks)
        return OcrObservation(
            region_id="r1",
            sampled_at=1.0,
            frame_sequence=1,
            blocks=blocks,
            uncovered_boxes=uncovered,
        )

    def assert_hits(
        self, layout: Layout, keywords: list[str], expected: list[str]
    ) -> list[str]:
        """返回命中消息文本，并核对命中数量与字符关键词。"""
        observation = self.observe(layout)
        rules = RuleEngine(self.storage, keywords)
        messages = blocks_to_messages(observation)
        matched = [
            message.text for message in messages if rules.decide(message).should_notify
        ]
        self.assertEqual(len(matched), len(expected), f"实际命中：{matched}")
        if expected:
            self.assertEqual(matched, expected)
            for text in matched:
                self.assertTrue(rules.matching_keywords(text), f"未命中关键词：{text}")
        else:
            for message in messages:
                self.assertEqual(rules.matching_keywords(message.text), [])
        return matched

    # 场景 1：同一气泡内的修饰词不应阻断字符命中。
    def test_same_bubble_extra_words_still_match(self) -> None:
        layout = Layout()
        layout.message(20, 20, ["坦博尔饼干羽绒服"])
        self.assert_hits(layout, ["坦博尔羽绒服"], ["坦博尔饼干羽绒服"])

    # 场景 2：同一气泡多行字符齐全时只命中一次（组内合并）。
    def test_multiline_same_bubble_matches_once(self) -> None:
        layout = Layout()
        layout.message(20, 20, ["坦博尔饼干", "羽绒服 199 元"])
        self.assertEqual(len(layout.blocks), 2)
        observation = self.observe(layout)
        message_ids = [block.message_id for block in observation.blocks]
        self.assertEqual(len(set(message_ids)), 1)
        self.assertTrue(message_ids[0])
        messages = blocks_to_messages(observation)
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0].boundary_uncertain)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        decisions = [rules.decide(message) for message in messages]
        self.assertEqual(sum(item.should_notify for item in decisions), 1)
        self.assertEqual(decisions[0].matched_keywords, ["坦博尔羽绒服"])
        # 只有一条消息入队，绝不重复通知。
        self.assertEqual(len(rules.process(messages, "群 A", now=10)), 1)

    # 场景 2 补充：明显高于同帧其它气泡的多行合集不能被当成粘连气泡丢弃。
    def test_tall_multiline_collection_stays_intact(self) -> None:
        layout = Layout(440, 640)
        layout.message(100, 30, ["普通聊天"], color=BUBBLE_WHITE)
        layout.message(
            60,
            80,
            ["坦博尔饼干 羽绒服", "199 元 包邮", "第二件半价", "尺码 165-175"],
            color=BUBBLE_GREEN,
        )
        layout.message(100, 220, ["另一条消息"], color=BUBBLE_WHITE)
        panels = find_bubbles(layout.array(), layout.blocks)
        collection = [panel for panel in panels if panel.rect.x == 60]
        self.assertEqual(len(collection), 1)
        self.assertTrue(collection[0].certain)
        observation = self.observe(layout)
        message_ids = {block.message_id for block in observation.blocks if block.message_id}
        self.assertEqual(len(message_ids), 3)
        messages = blocks_to_messages(observation)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        hits = [message.text for message in messages if rules.decide(message).should_notify]
        self.assertEqual(len(hits), 1)
        self.assertIn("尺码 165-175", hits[0])

    # 场景 3：两个相邻气泡间隔 4px，不得跨气泡拼字。
    def test_bubbles_four_pixels_apart_are_not_spliced(self) -> None:
        layout = Layout()
        layout.message(20, 20, ["坦博尔"])
        layout.message(20, 66, ["羽绒服"])
        panels = find_bubbles(layout.array())
        self.assertEqual(len(panels), 2)
        observation = self.observe(layout)
        message_ids = [block.message_id for block in observation.blocks]
        self.assertEqual(len(message_ids), 2)
        self.assertNotEqual(message_ids[0], message_ids[1])
        messages = blocks_to_messages(observation)
        self.assertEqual([message.text for message in messages], ["坦博尔", "羽绒服"])
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertFalse(any(rules.decide(message).should_notify for message in messages))

    # 场景 4：缺字符或字符次数不足都不命中（消息级）。
    def test_missing_character_or_insufficient_repeat_does_not_match(self) -> None:
        missing = Layout()
        missing.message(20, 20, ["坦博尔羽服"])
        self.assert_hits(missing, ["坦博尔羽绒服"], [])
        repeated = Layout()
        repeated.message(20, 20, ["手机 19 元 特价"])
        self.assert_hits(repeated, ["手机199元"], [])
        complete = Layout()
        complete.message(20, 20, ["手机 199 元 特价"])
        self.assert_hits(complete, ["手机199元"], ["手机 199 元 特价"])

    # 场景 5：英文/数字分散出现，数量足够才命中。
    def test_letters_and_digits_need_enough_occurrences(self) -> None:
        enough = Layout()
        enough.message(20, 20, ["A1 B2 特价"], color=BUBBLE_GREEN)
        self.assert_hits(enough, ["AB12"], ["A1 B2 特价"])
        too_few = Layout()
        too_few.message(20, 20, ["AB1 特价"], color=BUBBLE_GREEN)
        self.assert_hits(too_few, ["AB12"], [])

    # 场景 6a：灰度照片上的文字（ImageBlockFilter 因无彩色纹理漏检）必须排除。
    def test_text_on_grayscale_photo_is_excluded(self) -> None:
        layout = Layout()
        layout.photo(20, 20, 300, 140, gray=True)
        block = layout.text_on_photo(40, 70, "坦博尔羽绒服")
        image = layout.array()
        # 纯灰度面板没有彩色纹理种子，图片块过滤器找不到它。
        self.assertEqual(ImageBlockFilter().find(image), [])
        observation = self.observe(layout)
        self.assertEqual(observation.blocks, [])
        self.assertIn(block.box, observation.uncovered_boxes)
        self.assertEqual(blocks_to_messages(observation), [])

    # 场景 6b：彩色商品图（白底黑字）整块判为 image_card，不得通知。
    def test_text_in_colored_product_panel_is_excluded(self) -> None:
        image = np.full((360, 340, 3), 237, dtype=np.uint8)
        image[20:200, 20:320] = 255
        image[30:150, 30:150] = np.random.default_rng(8).integers(
            0, 256, size=(120, 120, 3), dtype=np.uint8
        )
        image[160:180, 40:300] = 0
        image_boxes = ImageBlockFilter().find(image)
        self.assertTrue(image_boxes)
        block = TextBlock(
            text="坦博尔羽绒服", confidence=0.99, box=Rect(40, 160, 260, 20)
        )
        blocks, _uncovered = assign_messages(image, [block], image_boxes)
        self.assertEqual([item.kind for item in blocks], ["image_card"])
        observation = OcrObservation("r1", 1.0, 1, blocks, list(image_boxes))
        messages = blocks_to_messages(observation)
        self.assertEqual(len(messages), 1)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        decision = rules.decide(messages[0])
        self.assertFalse(decision.should_notify)
        self.assertEqual(decision.reason, "unsupported_message_type")

    # 场景 7a：纯文字风格的“聊天记录”卡片（含底栏结构）不通知。
    def test_forwarded_card_structure_is_excluded(self) -> None:
        layout = Layout()
        layout.message(
            20,
            20,
            ["商品合集 12 件", "手机 4999 元 优惠券 满减 限时", "查看全部聊天记录"],
        )
        layout.blocks[-1] = TextBlock(
            text="查看全部聊天记录",
            confidence=0.99,
            box=Rect(layout.blocks[-1].box.x, layout.blocks[-1].box.y, 96, 14),
        )
        observation = self.observe(layout)
        messages = blocks_to_messages(observation)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].kind, "forwarded_card")
        rules = RuleEngine(self.storage, ["手机"])
        self.assertFalse(rules.decide(messages[0]).should_notify)

    # 场景 7b：普通句子提到“聊天记录”但同时命中商品关键词必须通知。
    def test_plain_sentence_mentioning_history_still_notifies(self) -> None:
        layout = Layout()
        layout.message(20, 20, ["有人发了聊天记录 手机 4999 元"])
        observation = self.observe(layout)
        messages = blocks_to_messages(observation)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].kind, "normal_text")
        rules = RuleEngine(self.storage, ["手机"])
        self.assertTrue(rules.decide(messages[0]).should_notify)

    # 场景 8：0px 粘连、宽度明显不同的两个气泡应二次切分成功。
    def test_touching_bubbles_with_different_width_are_split(self) -> None:
        layout = Layout()
        layout.message(20, 20, ["普通聊天"])
        layout.message(20, 70, ["坦博尔"])
        layout.message(20, 108, ["羽绒服 特价 199 元"], color=BUBBLE_GREEN)
        observation = self.observe(layout)
        message_ids = [block.message_id for block in observation.blocks]
        self.assertEqual(len(set(message_ids)), 3)
        messages = blocks_to_messages(observation)
        self.assertEqual(len(messages), 3)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertFalse(any(rules.decide(message).should_notify for message in messages))

    # 场景 9：面板高于同帧其它面板、文字只占其中一小部分 → 无法判定，计入未覆盖。
    def test_undecidable_touching_bubbles_are_uncovered(self) -> None:
        layout = Layout(360, 520)
        layout.message(20, 20, ["普通聊天"])
        # 上层是只有图片、没有文字的气泡，与下层文字气泡 0px 粘连。
        layout.bubble(20, 80, 240, 110, BUBBLE_WHITE)
        layout.message(20, 190, ["坦博尔羽绒服"])
        image = layout.array()
        observation = self.observe(layout)
        self.assertEqual([block.text for block in observation.blocks], ["普通聊天"])
        self.assertEqual(len(observation.uncovered_boxes), 1)
        dropped = observation.uncovered_boxes[0]
        self.assertEqual((dropped.x, dropped.y), (20, 80))
        self.assertGreater(dropped.height, 100)
        panels = find_bubbles(image, layout.blocks)
        self.assertTrue(any(not panel.certain for panel in panels))
        messages = blocks_to_messages(observation)
        self.assertEqual([message.text for message in messages], ["普通聊天"])
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertEqual(rules.process(messages, "群 A", now=1), [])

    # 场景 2 补充：同一气泡内用空行分段（空隙 ≥10px、行宽差异大）不得被切断。
    def test_paragraph_gaps_inside_one_bubble_are_not_split(self) -> None:
        layout = Layout(380, 540)
        layout.message(20, 20, ["普通聊天"])
        layout.message(
            20, 80, ["牧高笛冷山雪翼 30d", "199 元 包邮 拍下立减"], line_gap=44
        )
        panels = find_bubbles(layout.array(), layout.blocks)
        segmented = [panel for panel in panels if panel.rect.y == 80]
        self.assertEqual(len(segmented), 1)
        self.assertTrue(segmented[0].certain)
        observation = self.observe(layout)
        messages = blocks_to_messages(observation)
        self.assertEqual([message.text for message in messages], [
            "普通聊天", "牧高笛冷山雪翼 30d\n199 元 包邮 拍下立减",
        ])
        rules = RuleEngine(self.storage, ["牧高笛冷山雪翼"])
        hits = [message.text for message in messages if rules.decide(message).should_notify]
        self.assertEqual(len(hits), 1)

    # 场景 6/7 的补充：图片面板与卡片都不得进入通知，但未覆盖矩形要能画出来。
    def test_uncovered_boxes_are_reported_for_dropped_content(self) -> None:
        layout = Layout()
        layout.photo(20, 20, 300, 140, gray=True)
        layout.text_on_photo(40, 70, "手机 4999 元")
        layout.message(20, 200, ["普通聊天 手机 4999 元"])
        observation = self.observe(layout)
        self.assertEqual([block.text for block in observation.blocks], ["普通聊天 手机 4999 元"])
        self.assertTrue(observation.uncovered_boxes)
        messages = blocks_to_messages(observation)
        rules = RuleEngine(self.storage, ["手机"])
        self.assertEqual(len(messages), 1)
        self.assertTrue(rules.decide(messages[0]).should_notify)


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成版面用例")
class BoundaryDecisionTests(unittest.TestCase):
    """场景 9 的纯函数面：不确定面板不参与组装、面板矩形进未覆盖。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "boundary.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_uncertain_panel_never_produces_keyword_hit(self) -> None:
        layout = Layout(360, 520)
        layout.message(20, 20, ["普通聊天"])
        layout.bubble(20, 80, 240, 110, BUBBLE_WHITE)
        layout.message(20, 190, ["坦博尔羽绒服"])
        blocks, uncovered = assign_messages(layout.array(), layout.blocks)
        self.assertEqual([block.text for block in blocks], ["普通聊天"])
        self.assertTrue(uncovered)
        padded = [
            TextBlock(text=block.text, confidence=block.confidence, box=block.box)
            for block in blocks
        ]
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        candidates = blocks_to_messages(OcrObservation("r1", 1.0, 1, padded))
        self.assertFalse(any(rules.decide(item).should_notify for item in candidates))


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成版面用例")
class FrameBorderTests(unittest.TestCase):
    """P3-2：帧四周的均匀边框宽度必须动态清除（写死 3px 会漏掉 11px 边框）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "border.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def bordered(self, layout: Layout, thickness: int, color=120) -> np.ndarray:
        image = layout.array().copy()
        image[:thickness] = color
        image[-thickness:] = color
        image[:, :thickness] = color
        image[:, -thickness:] = color
        return image

    def test_eleven_pixel_border_frame_still_finds_panels(self) -> None:
        layout = Layout(320, 420)
        panel = layout.message(30, 60, ["坦博尔羽绒服 199 元"], color=BUBBLE_GREEN)
        image = self.bordered(layout, 11)
        panels = find_bubbles(image, layout.blocks)
        self.assertEqual(len(panels), 1, f"11px 边框帧丢了面板：{panels}")
        self.assertTrue(panels[0].certain)
        self.assertEqual(panels[0].rect, panel)
        blocks, uncovered = assign_messages(image, layout.blocks)
        messages = blocks_to_messages(OcrObservation("r1", 1.0, 1, blocks, uncovered))
        self.assertEqual(len(messages), 1)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertTrue(rules.decide(messages[0]).should_notify)

    def test_three_pixel_border_frame_keeps_previous_behaviour(self) -> None:
        layout = Layout(320, 420)
        panel = layout.message(30, 60, ["手机 4999 元"], color=BUBBLE_GREEN)
        image = self.bordered(layout, 3)
        panels = find_bubbles(image, layout.blocks)
        self.assertEqual(len(panels), 1, f"3px 边框帧丢了面板：{panels}")
        self.assertEqual(panels[0].rect, panel)


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成版面用例")
class SinglePanelTests(unittest.TestCase):
    """A0：帧内只有一个面板时也要能判“一条长消息”还是“两段粘连”。"""

    """A0：帧内只有一个面板时也要能判“一条长消息”还是“两段粘连”。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "single.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_single_panel_four_line_collection_stays_one_message(self) -> None:
        layout = Layout(340, 460)
        layout.message(
            20,
            20,
            ["坦博尔饼干 羽绒服", "199 元 包邮", "第二件半价", "尺码 165-175"],
            color=BUBBLE_GREEN,
        )
        panels = find_bubbles(layout.array(), layout.blocks)
        self.assertEqual(len(panels), 1)
        self.assertTrue(panels[0].certain)
        blocks, uncovered = assign_messages(layout.array(), layout.blocks)
        self.assertEqual(uncovered, [])
        self.assertEqual(len({block.message_id for block in blocks}), 1)
        messages = blocks_to_messages(OcrObservation("r1", 1.0, 1, blocks, uncovered))
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0].boundary_uncertain)
        self.assertIn("尺码 165-175", messages[0].text)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertEqual(len(rules.process(messages, "群 A", now=1)), 1)

    def test_single_panel_touching_segments_never_splice_characters(self) -> None:
        layout = Layout(360, 400)
        # 两个 0px 相邻、底色不同的气泡：不能被拼成一条消息。
        layout.message(20, 40, ["普通聊天"], color=BUBBLE_WHITE)
        layout.message(60, 130, ["坦博尔", "羽绒服 199 元"], color=BUBBLE_GREEN)
        panels = find_bubbles(layout.array(), layout.blocks)
        blocks, uncovered = assign_messages(layout.array(), layout.blocks)
        messages = blocks_to_messages(OcrObservation("r1", 1.0, 1, blocks, uncovered))
        for message in messages:
            self.assertFalse(
                "普通聊天" in message.text and "坦博尔" in message.text,
                f"两段粘连被拼成一条：{message.text}",
            )
        self.assertTrue(
            len(panels) >= 2 or any(not panel.certain for panel in panels),
            f"既没切分也没判为不确定：{panels}",
        )
        self.assertTrue(len(messages) <= 2)


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成版面用例")
class CardStructureTests(unittest.TestCase):
    """0.2.1 卡片漏检修复：小尺寸缩略图种子 + 浅色卡片结构证据。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "card.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def assert_covers(self, box: Rect, target: Rect) -> None:
        self.assertTrue(
            box.x <= target.x and box.y <= target.y
            and box.x + box.width >= target.x + target.width
            and box.y + box.height >= target.y + target.height,
            f"{box} 未覆盖 {target}",
        )

    def build_card(
        self,
        *,
        gray_photo: bool = False,
        border=None,
        fill=(255, 255, 255),
        with_photo: bool = True,
    ) -> tuple[Layout, Rect, list[TextBlock]]:
        layout = Layout(360, 320)
        card = layout.card(16, 16, 320, 140, fill=fill, border=border)
        if with_photo:
            layout.thumbnail(24, 24, 65, 60, gray=gray_photo)
        blocks = [
            layout.label(100, 30, "天猫茉寻官方旗舰店"),
            layout.label(100, 62, "双11 赵露思同款 鲨鱼裤"),
            layout.label(100, 96, "限时秒杀 02:54:23"),
        ]
        # 下方普通聊天气泡：任何情况下都不得被遮罩。
        layout.message(16, 200, ["普通聊天 手机 4999 元"], color=BUBBLE_WHITE)
        return layout, card, blocks

    def test_small_colored_thumbnail_masks_whole_card(self) -> None:
        layout, card, card_blocks = self.build_card(border=(230, 230, 230))
        image = layout.array()
        boxes = ImageBlockFilter().find(image)
        self.assertTrue(boxes)
        self.assertTrue(any(
            box.x <= card.x + 4 and box.y <= card.y + 4
            and box.x + box.width >= card.x + card.width - 4
            and box.y + box.height >= card.y + card.height - 4
            for box in boxes
        ), f"卡片未被整块遮罩：{boxes}")
        for box in boxes:
            self.assertFalse(
                box.y + box.height > 190 and box.x < 340,
                f"遮罩越界到聊天气泡：{box}",
            )
        blocks, _uncovered = assign_messages(image, card_blocks, boxes)
        self.assertEqual({block.kind for block in blocks}, {"image_card"})

    def test_grayscale_thumbnail_card_is_masked(self) -> None:
        layout, card, card_blocks = self.build_card(gray_photo=True)
        image = layout.array()
        boxes = ImageBlockFilter().find(image)
        self.assertTrue(boxes)
        self.assertTrue(any(
            box.x <= card.x + 4 and box.y + box.height >= card.y + card.height - 4
            for box in boxes
        ))
        blocks, _uncovered = assign_messages(image, card_blocks, boxes)
        self.assertEqual({block.kind for block in blocks}, {"image_card"})

    def test_borderless_light_card_expands_from_thumbnail(self) -> None:
        # 卡片底色与页面只差 4：轮廓法抓不到，只能靠卡片结构证据扩展。
        layout, card, card_blocks = self.build_card(fill=(241, 241, 241))
        image = layout.array()
        boxes = ImageBlockFilter().find(image)
        self.assertTrue(boxes)
        self.assertTrue(any(
            box.x <= card.x + 6 and box.y <= card.y + 6
            and box.x + box.width >= card.x + card.width - 6
            and box.y + box.height >= card.y + card.height - 6
            for box in boxes
        ), f"无边框浅色卡片没有整块遮罩：{boxes}")
        blocks, _uncovered = assign_messages(image, card_blocks, boxes)
        self.assertEqual({block.kind for block in blocks}, {"image_card"})

    def test_text_only_card_uses_structure_evidence(self) -> None:
        layout = Layout(360, 320)
        layout.card(16, 16, 320, 140, border=(230, 230, 230))
        lines = [
            layout.label(30, 26, "商品合集 12 件"),
            layout.label(30, 54, "手机 4999 元 优惠券 满减 限时"),
            layout.label(30, 82, "第二件半价 包邮 七天价保"),
            layout.label(30, 110, "查看全部聊天记录"),
        ]
        layout.blocks[-1] = TextBlock(
            text=lines[-1].text,
            confidence=0.99,
            box=Rect(lines[-1].box.x, lines[-1].box.y, 96, 14),
        )
        layout.message(16, 200, ["普通聊天 手机 4999 元"], color=BUBBLE_WHITE)
        image = layout.array()
        self.assertEqual(ImageBlockFilter().find(image), [])
        observation = OcrObservation(
            "r1", 1.0, 1, *assign_messages(image, list(layout.blocks))
        )
        messages = blocks_to_messages(observation)
        card_message = next(
            message for message in messages if "商品合集" in message.text
        )
        self.assertEqual(card_message.kind, "forwarded_card")
        rules = RuleEngine(self.storage, ["手机4999元"])
        self.assertFalse(rules.decide(card_message).should_notify)
        bubble_message = next(
            message for message in messages if "普通聊天" in message.text
        )
        self.assertEqual(bubble_message.kind, "normal_text")

    def test_plain_bubble_text_is_not_masked_by_card(self) -> None:
        layout, _card, _blocks = self.build_card()
        image = layout.array()
        boxes = ImageBlockFilter().find(image)
        bubble = Rect(16, 200, 200, 60)
        for box in boxes:
            self.assertEqual(
                max(0, min(box.x + box.width, bubble.x + bubble.width) - max(box.x, bubble.x))
                * max(0, min(box.y + box.height, bubble.y + bubble.height) - max(box.y, bubble.y)),
                0,
            )
        blocks, _uncovered = assign_messages(image, list(layout.blocks), boxes)
        bubble_blocks = [block for block in blocks if "普通聊天" in block.text]
        self.assertEqual(len(bubble_blocks), 1)
        messages = blocks_to_messages(OcrObservation("r1", 1.0, 1, bubble_blocks))
        self.assertEqual(messages[0].kind, "normal_text")
        rules = RuleEngine(self.storage, ["手机"])
        self.assertTrue(rules.decide(messages[0]).should_notify)


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成版面用例")
class BorderlessLightCardTests(unittest.TestCase):
    """白底无边框商品卡片：卡片底色只比页面亮 3 级，卡内文字必须整卡排除。

    真实故障：淘宝多商品卡片白底（253）贴在浅灰页面（250）上，差值只有 3 级，
    前景差分拿不到卡片本体，卡内的店铺行/标题行/`×1`/规格行/服务行/价格行
    全部变成 singleton 普通文字，商品词因此误报。判据只用结构证据：缩略图
    种子 + 与页面底色差 ≥2 级的平板边界 + 逐行/逐列的墨水占比，不含任何
    商品词或“行数即卡片”的规则。
    """

    # 页面底色沿用 Layout 的 PAGE(237)；卡片填 240，只差 3 级。
    CARD_FILL = (240, 240, 240)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "light-card.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def build_card(
        self, fill: tuple[int, int, int] | None = None
    ) -> tuple[Layout, Rect, list[str]]:
        """合成一张白底无边框商品卡片：灰色商品方块 + 六级文字层级。"""
        layout = Layout(520, 640)
        card = layout.card(40, 40, 380, 250, fill=fill or self.CARD_FILL)
        layout.thumbnail(54, 64, 84, 120, gray=True)
        layout.thumbnail(54, 194, 84, 62, gray=True)
        texts = [
            "天猫 某某官方旗舰店 >",
            "抓绒外套 低至99元起",
            "×1",
            "黑色 K3400; M >",
            "百亿补贴60元 8.5折金刚券",
            "退货宝 88VIP退货包运费 买责必赔",
            "补贴后 ¥60.19 ¥159 >",
        ]
        layout.label(154, 60, texts[0], size=15)
        layout.label(154, 88, texts[1], size=15)
        layout.label(370, 88, texts[2], size=13)
        layout.label(154, 118, texts[3], size=14)
        layout.label(154, 146, texts[4], size=13)
        layout.label(154, 174, texts[5], size=13)
        layout.label(154, 228, texts[6], size=14)
        # 卡片下方是普通聊天气泡，任何情况下都不得被遮罩。
        layout.message(40, 400, ["普通聊天 抓绒外套 199 元"], color=BUBBLE_WHITE)
        return layout, card, texts

    def assert_covers(self, box: Rect, target: Rect, pad: int = 8) -> None:
        self.assertTrue(
            box.x <= target.x + pad and box.y <= target.y + pad
            and box.x + box.width >= target.x + target.width - pad
            and box.y + box.height >= target.y + target.height - pad,
            f"{box} 未整卡遮罩 {target}",
        )

    def test_card_body_only_three_levels_above_page_is_masked(self) -> None:
        layout, card, texts = self.build_card()
        image = layout.array()
        # 前提：卡片本体确实比页面只亮 3 级，前景差分看不到它。
        body = image[card.y + 222:card.y + 242, card.x + 160:card.x + 340]
        self.assertEqual(int(body.max()) - int(PAGE[0]), 3)
        self.assertEqual(int(body.min()), int(body.max()), "前提：卡片本体是纯色平板")
        boxes = ImageBlockFilter().find(image)
        self.assertTrue(
            any(
                box.x <= card.x + 8 and box.y <= card.y + 8
                and box.x + box.width >= card.x + card.width - 8
                and box.y + box.height >= card.y + card.height - 8
                for box in boxes
            ),
            f"卡片未整卡遮罩：{boxes}",
        )
        # 遮罩后卡片区域不留任何墨水，OCR 不可能再读到卡内文字。
        masked = ImageBlockFilter.mask(image, boxes)
        region = masked[card.y:card.y + card.height, card.x:card.x + card.width]
        ink = float((np.abs(region.astype(np.int16) - int(PAGE[0])).max(axis=2) > 12).mean())
        self.assertLess(ink, 0.01, f"卡片区域仍有墨水 {ink:.3f}")
        # 卡内文字块全部判为 image_card，不得有任何一块按普通文字送出。
        blocks, uncovered = assign_messages(image, list(layout.blocks), boxes)
        card_blocks = [block for block in blocks if block.text in texts]
        self.assertEqual(len(card_blocks), len(texts))
        self.assertEqual({block.kind for block in card_blocks}, {"image_card"})
        self.assertTrue(
            any(
                box.x <= card.x and box.y <= card.y
                and box.x + box.width >= card.x + card.width
                and box.y + box.height >= card.y + card.height
                for box in [*boxes, *uncovered]
            ),
            f"整卡未进未覆盖区域：{[*boxes, *uncovered]}",
        )

    def test_body_two_levels_above_page_is_still_masked(self) -> None:
        """卡片底色只比页面亮 2 级：``delta > 2`` 的判据整个看不到卡片本体。"""
        layout, card, texts = self.build_card(fill=(239, 239, 239))
        image = layout.array()
        body = image[card.y + 222:card.y + 242, card.x + 160:card.x + 340]
        self.assertEqual(int(body.max()) - int(PAGE[0]), 2)
        self.assertEqual(int(body.min()), int(body.max()), "前提：卡片本体是纯色平板")
        boxes = ImageBlockFilter().find(image)
        self.assertTrue(
            any(
                box.x <= card.x + 8 and box.y <= card.y + 8
                and box.x + box.width >= card.x + card.width - 8
                and box.y + box.height >= card.y + card.height - 8
                for box in boxes
            ),
            f"底色只差 2 级的卡片未整卡遮罩：{boxes}",
        )
        masked = ImageBlockFilter.mask(image, boxes)
        region = masked[card.y:card.y + card.height, card.x:card.x + card.width]
        ink = float((np.abs(region.astype(np.int16) - int(PAGE[0])).max(axis=2) > 12).mean())
        self.assertLess(ink, 0.01, f"卡片区域仍有墨水 {ink:.3f}")
        blocks, _uncovered = assign_messages(image, list(layout.blocks), boxes)
        kinds = {block.kind for block in blocks if block.text in texts}
        self.assertEqual(kinds, {"image_card"}, "卡内文字仍按普通文字送出")

    def test_light_card_region_crosses_tag_band(self) -> None:
        """纯函数面：标签底色带（没有卡片底色、也不是成片深色）必须能跨过。"""
        content = np.zeros((240, 240), dtype=bool)
        content[20:220, 20:220] = True
        ink = np.zeros((240, 240), dtype=bool)
        ink[100:124, 20:140] = True   # 60% 宽的标签带
        content[100:124, 20:140] = False
        region = _light_card_region(content, ink, Rect(30, 140, 60, 50))
        self.assertEqual(
            (region.x, region.y, region.width, region.height), (20, 20, 200, 200)
        )

    def test_light_card_region_stops_at_full_width_dark_band(self) -> None:
        """纯函数面：整行成片深色（绿色气泡/深色图片）必须立刻停止扩展。"""
        content = np.zeros((240, 240), dtype=bool)
        content[20:220, 20:220] = True
        ink = np.zeros((240, 240), dtype=bool)
        ink[100:124, 20:220] = True
        content[100:124, 20:220] = False
        region = _light_card_region(content, ink, Rect(30, 140, 60, 50))
        self.assertEqual(region.y, 124, f"越过成片深色带继续扩展：{region}")

    def test_card_text_never_notifies_even_with_product_keyword(self) -> None:
        layout, _card, texts = self.build_card()
        image = layout.array()
        boxes = ImageBlockFilter().find(image)
        blocks, _uncovered = assign_messages(image, list(layout.blocks), boxes)
        observation = OcrObservation("r1", 1.0, 1, blocks)
        messages = blocks_to_messages(observation)
        card_messages = [
            message for message in messages
            if any(text in message.text for text in texts)
        ]
        self.assertTrue(card_messages)
        self.assertEqual({message.kind for message in card_messages}, {"forwarded_card"})
        rules = RuleEngine(self.storage, ["抓绒外套"])
        self.assertFalse(any(rules.decide(message).should_notify for message in card_messages))
        # 卡片下方的真实气泡仍能命中。
        bubble = next(message for message in messages if "普通聊天" in message.text)
        self.assertEqual(bubble.kind, "normal_text")
        self.assertTrue(rules.decide(bubble).should_notify)

    def test_same_contrast_collection_bubble_without_thumbnail_stays_text(self) -> None:
        """反向用例：与卡片同底色差的多行合集气泡（无缩略图）不得被当成卡片。"""
        layout = Layout(560, 460)
        lines = [
            "速度10点结束",
            "抓绒软壳裤 160",
            "①领券3000-400",
            "/ HU071 fmOl6bizLMd/",
            "②凑单拍完退",
            "/ HU926 JnmC6biz0sr/",
        ]
        # 与真实白气泡一致：比页面亮 5 级（卡片是 3 级），仍属“浅色气泡”。
        layout.bubble(40, 40, 480, 190, (242, 242, 242))
        for index, line in enumerate(lines):
            layout.label(60, 58 + index * 28, line, size=15)
        image = layout.array()
        self.assertEqual(ImageBlockFilter().find(image), [], "无缩略图的多行合集被当成卡片遮罩")
        blocks, uncovered = assign_messages(image, list(layout.blocks))
        self.assertEqual(len(blocks), len(lines))
        self.assertEqual(uncovered, [])
        self.assertEqual(len({block.message_id for block in blocks}), 1)
        messages = blocks_to_messages(OcrObservation("r1", 1.0, 1, blocks))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].kind, "normal_text")
        for line in lines:
            self.assertIn(line, messages[0].text)
        rules = RuleEngine(self.storage, ["抓绒软壳裤"])
        self.assertEqual(len(rules.process(messages, "群 A", now=1)), 1)

    def test_tall_light_collection_bubble_with_price_row_stays_text(self) -> None:
        """反向用例：带价格行、带链接行的高合集气泡仍是完整一条普通文字。"""
        layout = Layout(560, 520)
        lines = [
            "三峰出飘云2 三季",
            "674",
            "1领消费券3000-400",
            "/ HU071 fmOl6bizLMd/",
            "2复制打开叠金币加购物车",
            "/ HU926 JnmC6biz0sr/",
            "3凑单3000-400",
        ]
        layout.bubble(40, 40, 480, 250, BUBBLE_WHITE)
        for index, line in enumerate(lines):
            layout.label(60, 58 + index * 30, line, size=15)
        layout.label(430, 58, "674", size=15)
        image = layout.array()
        self.assertEqual(ImageBlockFilter().find(image), [])
        blocks, _uncovered = assign_messages(image, list(layout.blocks))
        messages = blocks_to_messages(OcrObservation("r1", 1.0, 1, blocks))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].kind, "normal_text")
        rules = RuleEngine(self.storage, ["三峰出飘云2"])
        self.assertEqual(len(rules.process(messages, "群 A", now=1)), 1)


if __name__ == "__main__":
    unittest.main()
