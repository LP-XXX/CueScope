"""0.2.1 最终复验：空白帧自动描边、OCR 动态描边余量、finalize 清单。

全部画面由本文件合成，不读取任何真实聊天截图或采集帧。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from deal_watch.capture_worker import frame_ink_ratio, frame_stats, is_blank, uniform_border
from deal_watch.models import OcrObservation, Rect, TextBlock
from deal_watch.ocr import assign_messages, find_bubbles, page_background
from deal_watch.rules import RuleEngine
from deal_watch.storage import Storage
from deal_watch.tracking import blocks_to_messages

try:  # Pillow 只用于合成帧；缺失时相关用例整体跳过。
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺 Pillow
    _PIL_AVAILABLE = False

_FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
_READY = _PIL_AVAILABLE and _FONT_PATH.is_file()

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PAGE = (245, 245, 245)
BUBBLE_WHITE = (255, 255, 255)
BUBBLE_GREEN = (149, 236, 105)
INK = (32, 32, 32)
DARK_BORDER = (96, 96, 96)
PRINTWINDOW_BORDER = (243, 243, 243)


def _mild_artifacts(shape: tuple[int, int, int], ratio: float = 0.01, seed: int = 5) -> np.ndarray:
    """纯白内部 + 少量浅色压缩痕迹（偏离中位色约 15，低于墨水阈值 24）。"""
    interior = np.full(shape, 255, dtype=np.uint8)
    rng = np.random.default_rng(seed)
    count = int(interior.shape[0] * interior.shape[1] * ratio)
    rows = rng.integers(0, shape[0], count)
    columns = rng.integers(0, shape[1], count)
    interior[rows, columns] = 240
    return interior


class BlankFrameAdversarialTests(unittest.TestCase):
    """PrintWindow 818x1289 空白帧 vs 正常帧 vs 照片窗口。"""

    @staticmethod
    def bordered(width: int, height: int, thickness: int, fill, interior: np.ndarray):
        image = np.full((height, width, 3), fill[0], dtype=np.uint8)
        image[thickness:height - thickness, thickness:width - thickness] = interior
        return image

    def test_eleven_pixel_border_with_mild_artifacts_is_blank(self) -> None:
        """内部 1% 浅色痕迹（PNG 无损，但边框/抗锯齿可能带浅色像素）仍判空白。"""
        interior = _mild_artifacts((1289 - 22, 818 - 22, 3))
        image = self.bordered(818, 1289, 11, PRINTWINDOW_BORDER, interior)
        self.assertEqual(uniform_border(image), (11, 11, 11, 11))
        self.assertLess(frame_ink_ratio(image), 0.004)
        self.assertTrue(is_blank(image), "1% 浅色痕迹的空白帧未判空白")

    def test_eleven_pixel_border_pure_white_is_blank(self) -> None:
        """内部完全无噪声（纯 255）时也必须判空白。"""
        image = self.bordered(818, 1289, 11, PRINTWINDOW_BORDER,
                              np.full((1289 - 22, 818 - 22, 3), 255, dtype=np.uint8))
        self.assertTrue(is_blank(image))

    def test_eleven_pixel_border_with_bubble_and_text_is_not_blank(self) -> None:
        interior = np.full((1289 - 22, 818 - 22, 3), 255, dtype=np.uint8)
        interior[200:340, 100:700] = 237      # 气泡底色
        interior[250:290, 140:660] = 32       # 文字行
        image = self.bordered(818, 1289, 11, PRINTWINDOW_BORDER, interior)
        self.assertGreater(frame_ink_ratio(image), 0.004)
        self.assertFalse(is_blank(image), "有内容的 11px 边框帧被判空白")

    def test_wgc_two_pixel_border_with_content_is_not_blank(self) -> None:
        interior = np.full((1280, 800, 3), 250, dtype=np.uint8)
        for y in range(120, 900, 60):
            interior[y:y + 24, 120:700] = 237
            interior[y + 4:y + 18, 160:640] = 32
        image = interior.copy()
        image[:2] = 235
        image[-2:] = 235
        image[:, :2] = 235
        image[:, -2:] = 235
        self.assertEqual(uniform_border(image), (2, 2, 2, 2))
        self.assertFalse(is_blank(image))

    def test_photo_window_is_not_blank(self) -> None:
        """纯照片窗口：内部高纹理、没有大面积纯色，不能判空白。"""
        height, width = 900, 700
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        plane = 120.0 + 60.0 * np.sin(xx / 9.0) + 40.0 * np.cos(yy / 11.0)
        image = np.stack([plane, plane * 0.8 + 20.0, plane * 0.6 + 40.0], axis=2)
        image = np.clip(image, 0, 255).astype(np.uint8)
        self.assertGreater(frame_ink_ratio(image), 0.004)
        self.assertFalse(is_blank(image), "照片窗口被判成空白帧")

    def test_stats_expose_border_and_ink(self) -> None:
        interior = np.full((1289 - 22, 818 - 22, 3), 255, dtype=np.uint8)
        image = self.bordered(818, 1289, 11, PRINTWINDOW_BORDER, interior)
        stats = frame_stats(image)
        self.assertEqual(
            stats["uniform_border"],
            {"top": 11, "bottom": 11, "left": 11, "right": 11},
        )
        self.assertLess(stats["ink_ratio"], 0.004)


class Frame:
    """合成帧构造器。"""

    def __init__(self, width: int, height: int, border: int = 0) -> None:
        self.width, self.height, self.border = width, height, border
        self.image = Image.new("RGB", (width, height), PAGE)
        self.draw = ImageDraw.Draw(self.image)
        self.blocks: list[TextBlock] = []

    def array(self) -> np.ndarray:
        image = np.asarray(self.image)[:, :, ::-1].copy()
        if self.border:
            image[: self.border] = DARK_BORDER
            image[-self.border:] = DARK_BORDER
            image[:, : self.border] = DARK_BORDER
            image[:, -self.border:] = DARK_BORDER
        return image

    def bubble(self, x: int, y: int, width: int, height: int, color=BUBBLE_WHITE) -> Rect:
        self.draw.rounded_rectangle(
            (x, y, x + width - 1, y + height - 1), radius=8, fill=color
        )
        return Rect(x, y, width, height)

    def lines(self, x: int, y: int, texts: list[str], *, size: int = 18, gap: int = 6):
        font = ImageFont.truetype(str(_FONT_PATH), size)
        cursor = y
        for text in texts:
            glyph = font.getbbox(text)
            self.draw.text((x, cursor - glyph[1]), text, font=font, fill=INK)
            self.blocks.append(
                TextBlock(
                    text,
                    0.99,
                    Rect(x, cursor, max(1, glyph[2] - glyph[0]), max(1, glyph[3] - glyph[1])),
                )
            )
            cursor += (glyph[3] - glyph[1]) + gap

    def message(self, x: int, y: int, texts: list[str], *, color=BUBBLE_WHITE,
                gap: int = 6, padding: int = 10) -> Rect:
        font = ImageFont.truetype(str(_FONT_PATH), 18)
        glyphs = [font.getbbox(text) for text in texts]
        heights = [box[3] - box[1] for box in glyphs]
        width = max(box[2] - box[0] for box in glyphs) + 2 * padding
        height = sum(heights) + gap * (len(texts) - 1) + 2 * padding
        panel = self.bubble(x, y, width, height, color)
        self.lines(x + padding, y + padding, texts, gap=gap)
        return panel


def _messages(blocks, uncovered=()) -> list:
    return blocks_to_messages(OcrObservation("r1", 1.0, 1, list(blocks), list(uncovered)))


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成帧用例")
class DynamicBorderTests(unittest.TestCase):
    """OCR 动态描边余量：11px 描边不能吃掉面板，2px 真实帧行为不变。"""

    def test_eleven_pixel_dark_border_still_finds_panels(self) -> None:
        frame = Frame(818, 1289, border=11)
        first = frame.message(60, 80, ["坦博尔羽绒服"])
        second = frame.message(60, 300, ["手机 4999 元"], color=BUBBLE_GREEN)
        image = frame.array()
        self.assertEqual(uniform_border(image), (11, 11, 11, 11))
        background = page_background(image)
        self.assertTrue(bool(np.all(background > 200.0)), f"背景被描边带偏：{background}")
        panels = find_bubbles(image, frame.blocks)
        self.assertEqual(len(panels), 2, f"11px 描边帧丢面板：{panels}")
        found = sorted((p.rect.x, p.rect.y, p.rect.width, p.rect.height) for p in panels)
        expected = sorted((r.x, r.y, r.width, r.height) for r in (first, second))
        self.assertEqual(found, expected)

    def test_fixed_three_pixel_margin_would_lose_every_panel(self) -> None:
        """对照：把动态检边关掉（等价旧实现 3px 固定余量）→ 面板数为 0。"""
        import deal_watch.ocr as ocr_module

        frame = Frame(818, 1289, border=11)
        frame.message(60, 80, ["坦博尔羽绒服"])
        frame.message(60, 300, ["手机 4999 元"], color=BUBBLE_GREEN)
        image = frame.array()
        original = ocr_module._frame_border
        try:
            ocr_module._frame_border = lambda _image: (0, 0, 0, 0)
            old_panels = find_bubbles(image, frame.blocks)
        finally:
            ocr_module._frame_border = original
        self.assertEqual(old_panels, [], f"旧 3px 固定余量本应丢面板，实际 {old_panels}")

    def test_two_pixel_border_real_size_behavior_unchanged(self) -> None:
        frame = Frame(800, 1280, border=2)
        first = frame.message(60, 60, ["坦博尔羽绒服"])
        second = frame.message(60, 400, ["手机 4999 元"], color=BUBBLE_GREEN)
        image = frame.array()
        self.assertEqual(uniform_border(image), (2, 2, 2, 2))
        self.assertTrue(bool(np.all(page_background(image) > 200.0)))
        panels = find_bubbles(image, frame.blocks)
        self.assertEqual(len(panels), 2, f"2px 描边帧面板数变化：{panels}")
        found = sorted((p.rect.x, p.rect.y, p.rect.width, p.rect.height) for p in panels)
        expected = sorted((r.x, r.y, r.width, r.height) for r in (first, second))
        self.assertEqual(found, expected)

    def test_light_eleven_pixel_border_finds_panels(self) -> None:
        """PrintWindow 的浅色边框同样要被清掉。"""
        frame = Frame(818, 1289)
        frame.message(60, 80, ["坦博尔羽绒服"])
        image = frame.array()
        image[:11] = PRINTWINDOW_BORDER
        image[-11:] = PRINTWINDOW_BORDER
        image[:, :11] = PRINTWINDOW_BORDER
        image[:, -11:] = PRINTWINDOW_BORDER
        panels = find_bubbles(image, frame.blocks)
        self.assertEqual(len(panels), 1, f"浅色 11px 边框帧丢面板：{panels}")
        self.assertTrue(panels[0].certain)


@unittest.skipUnless(_READY, "缺少 Pillow 或微软雅黑字体，跳过合成帧用例")
class SpliceRegressionTests(unittest.TestCase):
    """已修回归不得被本轮描边改动破坏（真实尺寸帧复测）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "final.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_zero_gap_touching_bubbles_not_spliced_at_real_size(self) -> None:
        frame = Frame(800, 1280)
        upper = frame.message(40, 100, ["坦博尔"])
        frame.message(40, upper.y + upper.height, ["羽绒服 199 元"], color=BUBBLE_GREEN)
        image = frame.array()
        blocks, uncovered = assign_messages(image, frame.blocks)
        messages = _messages(blocks, uncovered)
        for message in messages:
            self.assertFalse(
                "坦博尔" in message.text and "羽绒服" in message.text,
                f"0px 粘连被拼字：{message.text!r}",
            )
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertEqual(rules.process(messages, "群 A", now=1), [])

    def test_paragraph_gap_inside_one_bubble_stays_one_message_at_real_size(self) -> None:
        frame = Frame(800, 1280)
        frame.bubble(40, 60, 600, 560, BUBBLE_WHITE)
        frame.lines(80, 100, ["坦博尔饼干", "羽绒服 199 元"])
        frame.lines(80, 460, ["第二件半价 包邮"])
        image = frame.array()
        panels = find_bubbles(image, frame.blocks)
        self.assertEqual(len(panels), 1, f"同一气泡被切开：{panels}")
        blocks, uncovered = assign_messages(image, frame.blocks)
        messages = _messages(blocks, uncovered)
        self.assertEqual(len(messages), 1, [message.text for message in messages])
        rules = RuleEngine(self.storage, ["坦博尔羽绒服"])
        self.assertEqual(len(rules.process(messages, "群 A", now=1)), 1)


class FinalizePackageTests(unittest.TestCase):
    """finalize_package 报告清单：不得夹带 0.2.0 报告，版本不符必须中止。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.module = self._load_module()
        # 产物目录名与报告名一律取模块的 VERSION（它来自 pyproject.toml），
        # 用例不再持有版本字面量，否则每次升版都要改一遍验收用例。
        self.version = self.module.VERSION
        self.dist = self.root / "dist" / f"CueScope-{self.version}"
        self.reports = self.root / "build" / "validation"
        self.dist.mkdir(parents=True)
        self.reports.mkdir(parents=True)
        (self.dist / "CueScope.exe").write_bytes(b"synthetic-exe")
        for name in (
            "README.md", "QUICK_START.md", "PLAN_STATUS.md", "VALIDATION.md",
            "AGENTS.md", "requirements.lock", "THIRD_PARTY_NOTICES.md", "LICENSE",
        ):
            (self.root / name).write_text(f"# synthetic {name}\n", encoding="utf-8")
        (self.root / "packaging").mkdir()
        (self.root / "packaging" / "capture-requirements.lock").write_text(
            "numpy==2.2.6\nopencv-python==5.0.0.93\nwindows-capture==2.0.1\n",
            encoding="utf-8",
        )
        self.module.ROOT = self.root
        self.module.DIST = self.dist
        self.module.ARCHIVE = self.root / "dist" / f"CueScope-{self.version}-win-x64.zip"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _load_module():
        path = Path(__file__).resolve().parents[1] / "tools" / "finalize_package.py"
        spec = importlib.util.spec_from_file_location("finalize_package_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def write_report(self, name: str, payload: dict) -> None:
        (self.reports / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def run_main(self) -> None:
        from unittest.mock import patch

        with patch.object(self.module, "collect_licenses", lambda: None):
            self.module.main()

    def seed_all_reports(self, smoke_version: str | None = None) -> None:
        smoke_version = smoke_version or self.version
        self.write_report("automated-tests.json", {"passed": True})
        self.write_report(f"package-smoke-{self.version}.json",
                          {"version": smoke_version, "passed": True, "ui_tabs": 5})
        self.write_report("package-smoke.json",
                          {"passed": True, "ui_tabs": 4})  # 0.2.0 旧报告
        self.write_report("wgc-service-60s.json", {"kind": "wgc", "rounds": 10})
        self.write_report("wgc-service-quick.json", {"kind": "wgc-quick"})
        self.write_report("source-snapshot.json", {"README.md": hashlib.sha256((self.root / "README.md").read_bytes()).hexdigest()})
        self.write_report(f"capture-component-self-test-{self.version}.json", {"payload": {"ok": True}})
        self.write_report(f"capture-pool-self-test-{self.version}.json", {"passed": True})
        self.write_report("synthetic-four-window.json", {"kind": "synthetic"})
        self.write_report(f"synthetic-four-window-{self.version}.json",
                          {"success": True, "readiness_gate_passed": True})
        self.write_report("attachment-token-probe.json", {"ok": True})

    def test_legacy_zero_two_zero_report_is_not_copied(self) -> None:
        self.seed_all_reports()
        self.run_main()
        copied = sorted(path.name for path in (self.dist / "validation").iterdir())
        self.assertNotIn("package-smoke.json", copied, f"夹带旧 0.2.0 报告：{copied}")
        self.assertIn(f"package-smoke-{self.version}.json", copied)

    def test_raw_schedule_and_attachment_reports_are_not_packaged(self) -> None:
        self.seed_all_reports()
        self.run_main()
        copied = sorted(path.name for path in (self.dist / "validation").iterdir())
        self.assertNotIn("wgc-service-60s.json", copied, f"0.2.0 名称未改：{copied}")
        self.assertNotIn("synthetic-schedule-0.2.0-reference.json", copied)
        self.assertNotIn("attachment-token-probe.json", copied)

    def test_versioned_synthetic_report_is_packaged(self) -> None:
        """当前版本的合成四窗闭环报告必须入包（源名 = 包内名）。"""
        self.seed_all_reports()
        self.run_main()
        copied = sorted(path.name for path in (self.dist / "validation").iterdir())
        self.assertIn(
            f"synthetic-four-window-{self.version}.json",
            copied,
            f"缺 {self.version} 合成报告：{copied}",
        )

    def test_unversioned_legacy_synthetic_report_is_not_copied(self) -> None:
        """0.2.0 时代不带版本号的合成报告不得随当前版本的包分发。"""
        self.seed_all_reports()
        self.run_main()
        copied = sorted(path.name for path in (self.dist / "validation").iterdir())
        self.assertNotIn("synthetic-four-window.json", copied, f"夹带旧合成报告：{copied}")

    def test_legacy_synthetic_report_left_in_dist_aborts(self) -> None:
        """旧脚本残留的 synthetic-four-window.json 留在包目录时，必须拒绝打包。"""
        self.seed_all_reports()
        leftover = self.dist / "validation" / "synthetic-four-window.json"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text(json.dumps({"success": True}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_main()
        self.assertFalse(self.module.ARCHIVE.exists(), "夹带旧合成报告仍然生成了 zip")

    def test_report_with_wrong_version_aborts_before_packaging(self) -> None:
        self.seed_all_reports(smoke_version="0.2.0")
        with self.assertRaises(ValueError):
            self.run_main()
        # 关键是“不出包”：压缩包与校验清单都不得生成。
        self.assertFalse(self.module.ARCHIVE.exists(), "版本不符仍然生成了 zip")
        self.assertFalse(
            (self.dist / "SHA256SUMS.txt").exists(), "版本不符仍然生成了校验清单"
        )

    def test_legacy_report_left_in_dist_aborts(self) -> None:
        """旧脚本残留的 0.2.0 报告留在包目录里时，必须拒绝打包。"""
        self.seed_all_reports()
        leftover = self.dist / "validation" / "package-smoke.json"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text(json.dumps({"ui_tabs": 4}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_main()
        self.assertFalse(self.module.ARCHIVE.exists(), "夹带旧报告仍然生成了 zip")

    def test_legacy_reference_is_not_copied_even_with_a_version(self) -> None:
        """改名后的 0.2.0 参考件允许自证版本为 0.2.0，不应被门禁拦下。"""
        self.seed_all_reports()
        self.write_report("wgc-service-60s.json", {"version": "0.2.0", "rounds": 10})
        self.run_main()
        reference = self.dist / "validation" / "synthetic-schedule-0.2.0-reference.json"
        self.assertFalse(reference.exists())

    def test_package_outputs_are_built(self) -> None:
        self.seed_all_reports()
        self.run_main()
        self.assertTrue((self.dist / "SHA256SUMS.txt").is_file())
        self.assertTrue((self.dist / "README.md").is_file())
        archive = self.module.ARCHIVE
        self.assertTrue(archive.is_file(), "未生成 zip")
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
        self.assertTrue(
            any(name.endswith(f"CueScope-{self.version}/CueScope.exe") for name in names),
            f"zip 内顶层目录不是 CueScope-{self.version}：{names[:5]}",
        )


if __name__ == "__main__":
    unittest.main()
