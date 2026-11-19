"""采集 worker 的空白帧判定、DPI 自检与 ROI 一次编码协议（不需要真实窗口）。"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from deal_watch.capture_worker import (
    StageChannel,
    capture_wgc,
    frame_ink_ratio,
    frame_stats,
    is_black,
    is_blank,
    query_dpi_awareness,
    set_dpi_awareness,
    uniform_border,
)


def printwindow_like_frame(width: int = 818, height: int = 1289, border: int = 11,
                           fill=(250, 250, 250)):
    """模拟 PrintWindow 对现代微信的返回：11px 纯色边框 + 内部整片纯白。

    真实帧内部并非“整行完全同色”（极轻的压缩噪声），所以边框检测会停在
    第 11 行/列；这里用 4 条各含 3 个暗像素的线还原这一点，保证误差可忽略
    （12 个像素，占内部 1e-5 量级，不影响纯色占比判据）。
    """
    image = np.full((height, width, 3), fill[0], dtype=np.uint8)
    image[border:height - border, border:width - border] = 255
    for row in (border, height - border - 1):
        image[row, width // 3:width // 3 + 3] = 240
    for column in (border, width - border - 1):
        image[height // 3:height // 3 + 3, column] = 240
    return image


class UniformBorderTests(unittest.TestCase):
    def test_detects_eleven_pixel_border(self):
        image = printwindow_like_frame()
        self.assertEqual(uniform_border(image), (11, 11, 11, 11))

    def test_detects_wgc_two_pixel_border(self):
        image = np.full((1280, 800, 3), 250, dtype=np.uint8)
        image[:2] = 235
        image[-2:] = 235
        image[:, :2] = 235
        image[:, -2:] = 235
        image[100:400, 100:600] = 237
        self.assertEqual(uniform_border(image), (2, 2, 2, 2))

    def test_frame_without_border_reports_zero(self):
        image = np.full((200, 300, 3), 250, dtype=np.uint8)
        # 第一行/第一列有内容（不是整行同色）→ 边框宽度必须是 0。
        image[0, ::7] = 120
        image[::7, 0] = 120
        border = uniform_border(image)
        self.assertEqual(border[0], 0)
        self.assertEqual(border[2], 0)


class _Buffer:
    def __init__(self, data: bytes = b"png") -> None:
        self._data = data

    def tobytes(self) -> bytes:
        return self._data


class FakeCv2:
    """最小的 cv2 替身：只记录编码/解码次数，用于证明 ROI 只编码一次。"""

    def __init__(self) -> None:
        self.encodes = 0
        self.decodes = 0
        self.last_image = None

    def imencode(self, extension, image):
        self.encodes += 1
        self.last_image = np.array(image, copy=True)
        return True, _Buffer()

    def imdecode(self, buffer, flags):  # pragma: no cover - 调用了就是错误
        self.decodes += 1
        return None


class _Control:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def wait(self) -> None:
        return None


class _WorkerFrame:
    def __init__(self, image: np.ndarray) -> None:
        self.width = int(image.shape[1])
        self.height = int(image.shape[0])
        self._image = image

    def convert_to_bgr(self):
        return types.SimpleNamespace(frame_buffer=self._image)


class _FakeWindowsCapture:
    """``windows_capture.WindowsCapture`` 替身：start 时立刻投递一帧。"""

    def __init__(self, image: np.ndarray) -> None:
        self._image = image
        self._handlers: dict[str, object] = {}

    def event(self, callback):
        self._handlers[callback.__name__] = callback
        return callback

    def start_free_threaded(self):
        control = _Control()
        self._handlers["on_frame_arrived"](_WorkerFrame(self._image), control)
        return control


def text_like_frame(width: int = 40, height: int = 30) -> np.ndarray:
    """有文字的普通帧：既不是黑帧也不是整片纯色。"""
    image = np.full((height, width, 3), 245, dtype=np.uint8)
    for y in range(4, height - 4, 8):
        image[y : y + 3, 4 : width - 4] = 40
    return image


def run_wgc(image: np.ndarray, *, roi=None):
    fake_cv2 = FakeCv2()
    module = types.ModuleType("windows_capture")

    def factory(**_kwargs):
        return _FakeWindowsCapture(image)

    module.WindowsCapture = factory
    channel = StageChannel()
    with patch.dict(sys.modules, {"windows_capture": module, "cv2": fake_cv2}):
        payload = capture_wgc(0x1234, timeout=1.0, session_exit_timeout=0.5,
                              channel=channel, roi=roi)
    return payload, fake_cv2


class RoiProtocolTests(unittest.TestCase):
    """ROI 在编码前裁剪：一次 PNG 编码、零次 PNG 解码。"""

    def test_roi_is_cropped_before_the_only_png_encode(self):
        image = text_like_frame()
        payload, fake_cv2 = run_wgc(image, roi=(5, 4, 10, 8))
        self.assertEqual(fake_cv2.encodes, 1, "ROI 路径编码次数不是 1")
        self.assertEqual(fake_cv2.decodes, 0, "ROI 路径仍在解码整帧")
        self.assertEqual(fake_cv2.last_image.shape, (8, 10, 3), "编码的不是裁剪后的 ROI")
        self.assertEqual((payload["width"], payload["height"]), (10, 8))
        self.assertEqual((payload["frame_width"], payload["frame_height"]), (40, 30))
        self.assertEqual(payload["roi"], [5, 4, 10, 8])
        # 诊断口径仍来自整帧，而不是 ROI。
        self.assertIn("uniform_border", payload)
        self.assertIn("ink_ratio", payload)

    def test_full_frame_request_keeps_frame_fields_equal_to_image(self):
        image = text_like_frame()
        payload, fake_cv2 = run_wgc(image)
        self.assertEqual(fake_cv2.encodes, 1)
        self.assertEqual(fake_cv2.decodes, 0)
        self.assertEqual((payload["width"], payload["height"]), (40, 30))
        self.assertEqual((payload["frame_width"], payload["frame_height"]), (40, 30))
        self.assertIsNone(payload["roi"])

    def test_invalid_roi_fails_without_encoding_anything(self):
        image = text_like_frame()
        fake_cv2 = FakeCv2()
        module = types.ModuleType("windows_capture")
        module.WindowsCapture = lambda **_kwargs: _FakeWindowsCapture(image)
        with patch.dict(sys.modules, {"windows_capture": module, "cv2": fake_cv2}):
            with self.assertRaises(RuntimeError) as context:
                capture_wgc(0x1234, timeout=1.0, channel=StageChannel(),
                            roi=(35, 0, 10, 8))
        self.assertIn("INVALID_REGION", str(context.exception))
        # 抛错前不允许退化成“悄悄回传整帧”。
        self.assertEqual(fake_cv2.encodes, 0)

    def test_black_full_frame_fails_even_with_roi(self):
        image = np.zeros((30, 40, 3), dtype=np.uint8)
        with self.assertRaises(RuntimeError) as context:
            run_wgc(image, roi=(0, 0, 10, 10))
        self.assertIn("BLACK_FRAME", str(context.exception))

    def test_negative_roi_is_rejected(self):
        image = text_like_frame()
        with self.assertRaises(RuntimeError) as context:
            run_wgc(image, roi=(-1, 0, 10, 8))
        self.assertIn("INVALID_REGION", str(context.exception))


class PrintWindowBlankTests(unittest.TestCase):
    """回归：11px 纯色边框的 PrintWindow 帧曾经被当成正常帧。"""

    def test_bordered_white_frame_is_blank(self):
        image = printwindow_like_frame()
        self.assertTrue(is_blank(image))
        self.assertLess(frame_ink_ratio(image), 0.004)

    def test_bordered_frame_with_content_is_not_blank(self):
        image = printwindow_like_frame()
        # 内部画一条气泡与一行文字：墨水占比必须超过阈值。
        image[200:320, 120:700] = 237
        image[240:270, 160:660] = 32
        self.assertFalse(is_blank(image))

    def test_normal_wgc_frame_is_not_blank(self):
        image = np.full((1280, 800, 3), 250, dtype=np.uint8)
        image[:2] = 235
        image[-2:] = 235
        image[:, :2] = 235
        image[:, -2:] = 235
        for y in range(120, 900, 60):
            image[y:y + 24, 120:700] = 237
            image[y + 4:y + 18, 160:640] = 32
        self.assertFalse(is_blank(image))

    def test_photo_window_is_not_blank(self):
        rng = np.random.default_rng(3)
        image = rng.integers(0, 256, size=(900, 700, 3), dtype=np.uint8)
        self.assertFalse(is_blank(image))

    def test_payload_stats_expose_border_and_ink(self):
        stats = frame_stats(printwindow_like_frame())
        self.assertEqual(
            stats["uniform_border"], {"top": 11, "bottom": 11, "left": 11, "right": 11}
        )
        self.assertEqual(stats["ink_ratio"], 0.0)


class BlankFrameTests(unittest.TestCase):
    def test_uniform_white_frame_is_blank(self):
        image = np.full((200, 300, 3), 255, dtype=np.uint8)
        self.assertTrue(is_blank(image))

    def test_uniform_frame_with_dark_outline_is_blank(self):
        # PrintWindow 返回的空白帧常带一圈深色描边，仍应判为空白。
        image = np.full((200, 300, 3), 255, dtype=np.uint8)
        image[:2] = 60
        image[-2:] = 60
        image[:, :2] = 60
        image[:, -2:] = 60
        self.assertTrue(is_blank(image))

    def test_frame_with_content_is_not_blank(self):
        image = np.full((200, 300, 3), 255, dtype=np.uint8)
        image[40:120, 30:260] = 237
        self.assertFalse(is_blank(image))

    def test_empty_or_missing_image_is_blank(self):
        self.assertTrue(is_blank(None))
        self.assertTrue(is_blank(np.empty((0, 0, 3), dtype=np.uint8)))

    def test_black_frame_is_still_detected(self):
        image = np.zeros((120, 80, 3), dtype=np.uint8)
        self.assertTrue(is_black(image))
        self.assertTrue(is_blank(image))


class DpiAwarenessTests(unittest.TestCase):
    """DPI 查询必须永远安全返回，不能因为缺 API 或非 Windows 抛异常。"""

    ALLOWED = {"unaware", "system", "per_monitor", "per_monitor_v2", "unknown"}

    def test_query_returns_known_state(self):
        self.assertIn(query_dpi_awareness(), self.ALLOWED)

    def test_set_returns_request_and_last_error(self):
        result = set_dpi_awareness()
        self.assertIsInstance(result, dict)
        self.assertIn("request", result)
        self.assertIn("last_error", result)
        self.assertIsInstance(result["last_error"], int)

    def test_query_is_idempotent_after_request(self):
        first = set_dpi_awareness()
        self.assertIn(first["request"], {"per_monitor_v2", "per_monitor", "system", "unavailable",
                                        "not_windows"})
        self.assertIn(query_dpi_awareness(), self.ALLOWED)


if __name__ == "__main__":
    unittest.main()
