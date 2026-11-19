"""0.2.1 独立验收用例：QA 对抗性验证。

全部画面都由本文件合成（不读取任何真实聊天截图或本机采集帧），
覆盖气泡分割、描边帧背景估计、空白帧判定、暂停/恢复基线、圈选失效与去重。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from deal_watch.capture import CaptureError  # noqa: E402
from deal_watch.capture_worker import is_blank  # noqa: E402
from deal_watch.config import AppConfig  # noqa: E402
from deal_watch.models import (  # noqa: E402
    CaptureErrorCode,
    FrameSample,
    MessageCandidate,
    OcrObservation,
    Rect,
    TextBlock,
)
from deal_watch.normalize import content_digest  # noqa: E402
from deal_watch.ocr import assign_messages, find_bubbles, page_background  # noqa: E402
from deal_watch.rules import RuleEngine  # noqa: E402
from deal_watch.service import MonitorService  # noqa: E402
from deal_watch.storage import Storage  # noqa: E402
from deal_watch.tracking import FrameTracker, blocks_to_messages  # noqa: E402
from region_fixtures import ready_region  # noqa: E402

try:  # Pillow 只用于合成帧；缺失时相关用例整体跳过。
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺 Pillow
    _PIL_AVAILABLE = False

_FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
_READY = _PIL_AVAILABLE and _FONT_PATH.is_file()

PAGE = (245, 245, 245)
BUBBLE_WHITE = (255, 255, 255)
BUBBLE_GREEN = (149, 236, 105)
INK = (32, 32, 32)
# 真实 WGC 帧（800x1280）实测：最外 2px 为 117 灰描边，内部页面约 250。
FRAME_BORDER = (117, 117, 117)


def _font(size: int = 18):
    return ImageFont.truetype(str(_FONT_PATH), size)


class Frame:
    """合成帧构造器：只登记文字块，不做任何 OCR 推断。"""

    def __init__(self, width: int = 340, height: int = 460) -> None:
        self.image = Image.new("RGB", (width, height), PAGE)
        self.draw = ImageDraw.Draw(self.image)
        self.blocks: list[TextBlock] = []

    def array(self) -> np.ndarray:
        return np.asarray(self.image)[:, :, ::-1].copy()

    def bubble(self, x: int, y: int, width: int, height: int, color=BUBBLE_WHITE) -> Rect:
        self.draw.rounded_rectangle(
            (x, y, x + width - 1, y + height - 1), radius=8, fill=color
        )
        return Rect(x, y, width, height)

    def lines(
        self, x: int, y: int, texts: list[str], *, size: int = 18, gap: int = 6
    ) -> list[TextBlock]:
        font = _font(size)
        cursor = y
        drawn = []
        for text in texts:
            glyph = font.getbbox(text)
            self.draw.text((x, cursor - glyph[1]), text, font=font, fill=INK)
            block = TextBlock(
                text=text,
                confidence=0.99,
                box=Rect(
                    x,
                    cursor,
                    max(1, glyph[2] - glyph[0]),
                    max(1, glyph[3] - glyph[1]),
                ),
            )
            self.blocks.append(block)
            drawn.append(block)
            cursor += (glyph[3] - glyph[1]) + gap
        return drawn

    def message(
        self,
        x: int,
        y: int,
        texts: list[str],
        *,
        color=BUBBLE_WHITE,
        size: int = 18,
        gap: int = 6,
        padding: int = 10,
    ) -> Rect:
        """画气泡后写文字；气泡高度按真实字形框计算，便于构造 0px 粘连。"""
        font = _font(size)
        glyphs = [font.getbbox(text) for text in texts]
        heights = [box[3] - box[1] for box in glyphs]
        width = max(box[2] - box[0] for box in glyphs) + 2 * padding
        height = sum(heights) + gap * (len(texts) - 1) + 2 * padding
        panel = self.bubble(x, y, width, height, color)
        self.lines(x + padding, y + padding, texts, size=size, gap=gap)
        return panel

    def with_border(self, thickness: int = 2) -> np.ndarray:
        """在帧四边压上深色描边，模拟真实 WGC 帧。"""
        image = self.array()
        image[:thickness] = FRAME_BORDER
        image[-thickness:] = FRAME_BORDER
        image[:, :thickness] = FRAME_BORDER
        image[:, -thickness:] = FRAME_BORDER
        return image


def _messages(blocks, uncovered=()):
    return blocks_to_messages(
        OcrObservation("r1", 1.0, 1, list(blocks), list(uncovered))
    )


def _observation(texts: list[str], at: float, region: str = "r1") -> OcrObservation:
    return OcrObservation(
        region_id=region,
        sampled_at=at,
        frame_sequence=int(at),
        blocks=[
            TextBlock(text, 0.99, Rect(20, 20 + index * 70, 260, 28))
            for index, text in enumerate(texts)
        ],
    )


def _candidate(text: str, region: str = "r1") -> MessageCandidate:
    return MessageCandidate(
        region_id=region,
        text=text,
        box=Rect(20, 20, 260, 28),
        first_seen=1.0,
        last_seen=1.0,
        confidence=0.99,
    )


class StorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "acceptance.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成帧用例")
class BubbleSplitAdversarialTests(StorageCase):
    """对抗性气泡分割：描边帧、空行分段、0px 粘连、4px 间隔。"""

    def test_dark_two_pixel_border_frame_keeps_background_and_finds_panels(self) -> None:
        """真实 WGC 帧有 2px 深灰描边：背景估计不得被带偏，面板必须仍能找到。"""
        frame = Frame(340, 460)
        first = frame.message(20, 20, ["坦博尔羽绒服"])
        second = frame.message(20, 180, ["手机 4999 元"], color=BUBBLE_GREEN)
        image = frame.with_border(2)
        background = page_background(image)
        # 方向一：背景色必须是页面灰，不能落到描边的 117 灰上。
        self.assertTrue(
            bool(np.all(background > 200.0)), f"背景被描边带偏：{background}"
        )
        panels = find_bubbles(image, frame.blocks)
        # 方向二：面板数不为 0，且与两个气泡一一对应。
        self.assertEqual(len(panels), 2, f"描边帧丢面板：{panels}")
        self.assertTrue(all(panel.certain for panel in panels))
        found = sorted((item.rect.x, item.rect.y, item.rect.width, item.rect.height) for item in panels)
        expected = sorted((item.x, item.y, item.width, item.height) for item in (first, second))
        self.assertEqual(found, expected)
        blocks, uncovered = assign_messages(image, frame.blocks)
        self.assertEqual(uncovered, [])
        self.assertEqual(len({block.message_id for block in blocks}), 2)

    def test_single_bubble_blank_line_paragraphs_stay_one_message(self) -> None:
        """一个气泡内用空行分段的长消息仍是一条消息，不得被切成两条。"""
        frame = Frame(340, 460)
        panel = frame.bubble(20, 20, 300, 300, BUBBLE_WHITE)
        frame.lines(40, 50, ["坦博尔饼干", "羽绒服 199 元"])
        # 空行分段：与上一行间隔 139px，且行宽差异明显。
        frame.lines(40, 230, ["第二件半价 包邮"])
        image = frame.array()
        panels = find_bubbles(image, frame.blocks)
        self.assertEqual(len(panels), 1, f"同一气泡被切分：{panels}")
        self.assertTrue(panels[0].certain)
        self.assertEqual(panels[0].rect, panel)
        blocks, uncovered = assign_messages(image, frame.blocks)
        self.assertEqual(uncovered, [])
        messages = _messages(blocks, uncovered)
        self.assertEqual(len(messages), 1, [message.text for message in messages])
        self.assertIn("第二件半价 包邮", messages[0].text)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertTrue(rules.decide(messages[0]).should_notify)

    def test_zero_gap_touching_bubbles_with_different_fill_are_not_spliced(self) -> None:
        """两个 0px 粘连、底色不同的气泡不得拼成一条消息（不得跨气泡命中）。"""
        frame = Frame(360, 400)
        upper = frame.message(20, 40, ["坦博尔"])
        lower_y = upper.y + upper.height  # 0px 粘连
        frame.message(20, lower_y, ["羽绒服 199 元"], color=BUBBLE_GREEN)
        image = frame.array()
        panels = find_bubbles(image, frame.blocks)
        blocks, uncovered = assign_messages(image, frame.blocks)
        messages = _messages(blocks, uncovered)
        for message in messages:
            self.assertFalse(
                "坦博尔" in message.text and "羽绒服" in message.text,
                f"0px 粘连气泡被拼成一条消息：{message.text!r}（面板 {panels}）",
            )
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertEqual(
            [message.text for message in messages if rules.decide(message).should_notify],
            [],
            "跨气泡拼字导致关键词误命中",
        )
        # 更强证据：误命中不得进入通知队列。
        self.assertEqual(rules.process(messages, "群 A", now=1), [])

    def test_four_pixel_gap_bubbles_are_never_spliced(self) -> None:
        """两个气泡间隔 4px、各含一半字符时不得命中。"""
        frame = Frame(340, 460)
        upper = frame.message(20, 20, ["坦博尔"])
        frame.message(20, upper.y + upper.height + 4, ["羽绒服"])
        image = frame.array()
        panels = find_bubbles(image, frame.blocks)
        self.assertEqual(len(panels), 2, f"4px 间隔未分开：{panels}")
        blocks, uncovered = assign_messages(image, frame.blocks)
        self.assertNotEqual(blocks[0].message_id, blocks[1].message_id)
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        messages = _messages(blocks, uncovered)
        self.assertEqual([message.text for message in messages], ["坦博尔", "羽绒服"])
        self.assertFalse(any(rules.decide(message).should_notify for message in messages))


class BlankFrameTests(unittest.TestCase):
    """PrintWindow 空白帧判定：白帧、带描边白帧、含内容帧三个方向。"""

    def test_uniform_white_frame_is_blank(self) -> None:
        self.assertTrue(is_blank(np.full((200, 300, 3), 255, dtype=np.uint8)))

    def test_content_frame_is_not_blank(self) -> None:
        image = np.full((200, 300, 3), 255, dtype=np.uint8)
        image[40:120, 30:260] = 237
        self.assertFalse(is_blank(image))

    def test_border_only_frame_is_blank(self) -> None:
        """只有描边不同、内部全白的帧仍必须判空白（不得静默当成有效帧）。"""
        for thickness in (1, 2, 3):
            image = np.full((200, 300, 3), 255, dtype=np.uint8)
            image[:thickness] = FRAME_BORDER
            image[-thickness:] = FRAME_BORDER
            image[:, :thickness] = FRAME_BORDER
            image[:, -thickness:] = FRAME_BORDER
            self.assertTrue(is_blank(image), f"{thickness}px 描边白帧未判空白")


class _IdleCapturer:
    """本组用例手动投递帧，采集器只满足接口。"""

    def capture(self, source, force=False):
        return None

    def reset(self, region_id):
        pass


class _EchoOcr:
    """把帧里已有的文本列表直接变成文字块，避免加载真实 OCR 模型。"""

    def recognize(self, frame):
        return OcrObservation(
            region_id=frame.region_id,
            sampled_at=frame.captured_at,
            frame_sequence=frame.sequence,
            blocks=[
                TextBlock(text, 0.99, Rect(10, 10 + index * 60, 250, 25))
                for index, text in enumerate(frame.image)
            ],
            inference_ms=1.0,
        )


class PauseResumeTests(unittest.TestCase):
    """最小化/锁屏暂停不是故障；恢复只建基线、不补发历史。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "pause.db")
        self.region = ready_region(
            "合成群", window_title="Synthetic", rect=Rect(0, 0, 300, 500)
        )
        self.config = AppConfig(
            regions=[self.region], keywords=["手机"], save_hit_images=False
        )
        self.statuses: list[tuple[str, str]] = []
        self.processed = threading.Event()

        def health(snapshot):
            self.statuses.append((snapshot.status.value, snapshot.detail))
            self.processed.set()

        self.service = MonitorService(
            self.config,
            self.storage,
            _EchoOcr(),
            capturer=_IdleCapturer(),
            on_health=health,
        )

    def tearDown(self) -> None:
        self.service.stop()
        self.storage.close()
        self.temp.cleanup()

    def submit(self, sequence: int, texts: list[str], generation: int | None = None) -> None:
        self.processed.clear()
        frame = FrameSample(
            self.region.id, sequence, time.time(), texts, self.region.rect
        )
        self.service._submit(frame, generation)
        self.assertTrue(self.processed.wait(3), "帧未被 OCR 线程处理")

    def test_pause_region_marks_paused_without_failure_and_single_health(self) -> None:
        generation = self.service._generation[self.region.id]
        self.service._pause_region(
            self.region.id, generation, CaptureErrorCode.WINDOW_MINIMIZED
        )
        runtime = self.service._runtime[self.region.id]
        self.assertEqual(runtime.status.value, "paused")
        self.assertFalse(runtime.failed)
        self.assertEqual(runtime.detail, "已暂停：窗口最小化")
        self.assertEqual(self.statuses, [("paused", "已暂停：窗口最小化")])
        # 同一代再暂停一次：代数已变，必须是幂等的空操作，不重复通知。
        self.service._pause_region(
            self.region.id, generation, CaptureErrorCode.DESKTOP_LOCKED
        )
        self.assertEqual(runtime.detail, "已暂停：窗口最小化")
        self.assertEqual(len(self.statuses), 1)
        self.assertFalse(any(status == "error" for status, _ in self.statuses))

    def test_desktop_locked_uses_pause_semantics(self) -> None:
        generation = self.service._generation[self.region.id]
        self.service._pause_region(
            self.region.id, generation, CaptureErrorCode.DESKTOP_LOCKED
        )
        runtime = self.service._runtime[self.region.id]
        self.assertEqual(runtime.status.value, "paused")
        self.assertFalse(runtime.failed)
        self.assertEqual(runtime.detail, "已暂停：桌面已锁定")
        self.assertFalse(any(status == "error" for status, _ in self.statuses))

    def test_stale_generation_pause_is_ignored(self) -> None:
        generation = self.service._generation[self.region.id]
        self.service.resume_region(self.region.id)
        self.service._pause_region(
            self.region.id, generation, CaptureErrorCode.WINDOW_MINIMIZED
        )
        self.assertNotEqual(self.service._runtime[self.region.id].status.value, "paused")

    def test_resume_after_pause_baselines_without_backfilling_history(self) -> None:
        """暂停恢复后的首帧只建基线；命中词不补发，后续新消息才通知。"""
        self.service._ocr_thread = threading.Thread(
            target=self.service._ocr_loop, daemon=True
        )
        self.service._ocr_thread.start()
        self.submit(1, ["旧内容"])
        stale_generation = self.service._generation[self.region.id]

        self.service._pause_region(
            self.region.id, stale_generation, CaptureErrorCode.WINDOW_MINIMIZED
        )
        # 暂停判定后，先前发出的采集请求才返回：必须被丢弃。
        self.processed.clear()
        self.service._submit(
            FrameSample(
                self.region.id,
                2,
                time.time(),
                ["旧内容", "手机 4999 元"],
                self.region.rect,
            ),
            stale_generation,
        )
        self.assertEqual(self.service._pending, {})
        self.assertFalse(self.processed.wait(0.3))

        self.service.resume_region(self.region.id)
        # 恢复后的第一帧含命中词，只能作为基线，不得补发。
        self.submit(3, ["旧内容", "手机 4999 元"])
        self.assertEqual(self.storage.list_events(), [])
        # 同一静止内容继续采样也不重复。
        self.submit(4, ["旧内容", "手机 4999 元"])
        self.assertEqual(self.storage.list_events(), [])
        # 真正新增的消息才入队。
        self.submit(5, ["旧内容", "手机 4999 元", "手机 3999 元"])
        events = self.storage.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "手机 3999 元")
        self.assertFalse(any(status == "error" for status, _ in self.statuses))

    def test_capture_error_with_pause_code_is_routed_to_pause(self) -> None:
        """采集循环把最小化/锁屏错误交给暂停分支，而不是故障分支。"""
        class RaisingCapturer(_IdleCapturer):
            def capture(self, source, force=False):
                raise CaptureError(CaptureErrorCode.WINDOW_MINIMIZED, "窗口已最小化")

        service = MonitorService(
            self.config, self.storage, _EchoOcr(), capturer=RaisingCapturer()
        )
        paused = threading.Event()

        def health(snapshot):
            if snapshot.status.value == "paused":
                paused.set()

        service.on_health = health
        service.start()
        try:
            self.assertTrue(paused.wait(3), "最小化错误没有进入暂停分支")
            runtime = service._runtime[self.region.id]
            self.assertFalse(runtime.failed)
            self.assertEqual(runtime.status.value, "paused")
        finally:
            service.stop()


class RegionSelectionTests(unittest.TestCase):
    """圈选失效：切换窗口/后端后旧 ROI 不能保存，迟到结果必须丢弃。"""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        from PySide6.QtWidgets import QDialog

        self.qdialog = QDialog
        self.windows = [
            _FakeWindow(101, "群 A"),
            _FakeWindow(202, "群 B"),
        ]
        self.fake = _FakeWindows(self.windows)
        self.patcher = patch(
            "deal_watch.ui.region.WindowsWindows", return_value=self.fake
        )
        self.patcher.start()
        from deal_watch.ui.region import RegionEditDialog

        self.dialog = RegionEditDialog()
        self.dialog.alias.setText("群 B")
        # 默认下拉是“群 A”，先切到“群 B”再圈选，保持圈选窗口与下拉一致。
        self.dialog.window_combo.setCurrentIndex(1)

    def tearDown(self) -> None:
        self.patcher.stop()
        self.dialog.deleteLater()
        self.app.processEvents()

    def complete_selection(self, window_index: int, backend: str) -> None:
        self.dialog.resize(900, 680)
        self.dialog.show()
        self.app.processEvents()
        self.dialog._on_selection_completed(
            self.dialog._selection_generation, self.windows[window_index].hwnd,
            backend, self.windows[window_index], np.full((1280, 800, 3), 240, dtype=np.uint8),
            800, 1280, "",
        )
        self.app.processEvents()
        target = self.dialog.selector._target()
        self.dialog.selector.set_widget_selection(target.adjusted(8, 8, -8, -8))
        self.app.processEvents()

    def test_selection_then_backend_switch_blocks_save_and_region(self) -> None:
        self.complete_selection(1, "wgc")
        self.assertEqual(self.dialog.selection_problems(), [])
        self.dialog.backend_combo.setCurrentIndex(1)  # 切到 PrintWindow
        problems = self.dialog.selection_problems()
        self.assertTrue(problems, "切换后端后旧圈选未被判为失效")
        with self.assertRaises(RuntimeError):
            self.dialog.region()
        with patch("deal_watch.ui.region.QMessageBox.warning") as warning:
            self.dialog.accept()
            warning.assert_not_called()
            self.assertIn("预览", self.dialog.capture_status.text())
        self.assertNotEqual(self.dialog.result(), self.qdialog.DialogCode.Accepted)

    def test_selection_then_window_switch_blocks_save(self) -> None:
        self.complete_selection(1, "wgc")
        self.dialog.window_combo.setCurrentIndex(0)
        self.assertTrue(self.dialog.selection_problems())
        self.assertIsNone(self.dialog.selected_rect)
        with self.assertRaises(RuntimeError):
            self.dialog.region()

    def test_late_result_from_old_generation_is_dropped(self) -> None:
        stale = self.dialog._selection_generation
        self.dialog.backend_combo.setCurrentIndex(1)
        with patch("deal_watch.ui.region.RegionSelectionDialog") as selection:
            self.dialog._on_selection_completed(
                stale, 101, "wgc", self.windows[0], object(), 800, 1280, ""
            )
            selection.assert_not_called()
        self.assertIsNone(self.dialog.selected_rect)
        self.assertTrue(self.dialog.selection_problems())


class _FakeWindow:
    def __init__(self, hwnd: int, title: str) -> None:
        self.hwnd = hwnd
        self.title = title
        self.class_name = "WeChatMainWndForPC"
        self.minimized = False
        self.process_id = 4321
        self.process_path = "C:/Program Files/WeChat/WeChat.exe"
        self.rect = Rect(0, 0, 800, 1280)


class _FakeWindows:
    def __init__(self, windows: list[_FakeWindow]) -> None:
        self._windows = windows

    def list_windows(self) -> list[_FakeWindow]:
        return list(self._windows)

    def window_from_hwnd(self, hwnd: int):
        return next((item for item in self._windows if item.hwnd == hwnd), None)


def _accepted_selection():
    """构造一个“用户点了确定”的 RegionSelectionDialog 替身。"""
    from unittest.mock import MagicMock

    from PySide6.QtWidgets import QDialog

    mock = MagicMock()
    mock.return_value.exec.return_value = QDialog.DialogCode.Accepted
    mock.return_value.frame_rect.return_value = Rect(10, 10, 700, 900)
    return mock


class DedupeTests(StorageCase):
    """跨群五分钟去重与“价格/型号变化不算重复”。"""

    def test_same_content_across_groups_is_queued_once_within_cooldown(self) -> None:
        rules = RuleEngine(self.storage, ["手机"], cooldown_seconds=300)
        tracker_a, tracker_b = FrameTracker(), FrameTracker()
        tracker_a.process(_observation(["普通聊天"], 0, "a"))
        tracker_b.process(_observation(["普通聊天"], 0, "b"))
        new_a = tracker_a.process(_observation(["普通聊天", "手机 4999 元"], 10, "a"))
        new_b = tracker_b.process(_observation(["普通聊天", "手机 4999 元"], 20, "b"))
        self.assertEqual(len(rules.process(new_a, "群 A", now=1000)), 1)
        # 200 秒后另一个群出现完全相同的文字：只能进一条队列。
        self.assertEqual(rules.process(new_b, "群 B", now=1200), [])
        self.assertEqual(self.storage.queue_counts().get("pending"), 1)
        records = self.storage.list_events()
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(item["state"] == "duplicate" for item in records), 1)

    def test_same_region_repeat_within_cooldown_is_duplicate(self) -> None:
        rules = RuleEngine(self.storage, ["手机"], cooldown_seconds=300)
        self.assertEqual(len(rules.process([_candidate("手机 4999 元")], "群 A", now=1000)), 1)
        self.assertEqual(rules.process([_candidate("手机 4999 元")], "群 A", now=1200), [])
        self.assertEqual(self.storage.queue_counts().get("pending"), 1)

    def test_decimal_and_model_separator_are_material_differences(self) -> None:
        self.assertNotEqual(
            content_digest("手机 1.99 元"), content_digest("手机 199 元")
        )
        self.assertNotEqual(content_digest("手机 AB-12"), content_digest("手机 AB12"))
        rules = RuleEngine(self.storage, ["手机"], cooldown_seconds=300)
        texts = ["手机 1.99 元", "手机 199 元", "手机 AB-12", "手机 AB12"]
        for offset, text in enumerate(texts):
            self.assertEqual(
                len(rules.process([_candidate(text)], "群 A", now=1000 + offset)),
                1,
                f"{text} 被误判为重复",
            )
        self.assertEqual(self.storage.queue_counts().get("pending"), 4)


if __name__ == "__main__":
    unittest.main()
