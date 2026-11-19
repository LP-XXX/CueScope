from __future__ import annotations

import base64
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from deal_watch.capture import CaptureError, WindowInfo
from deal_watch.models import CaptureErrorCode, Rect, RegionConfig
from deal_watch.window_capture import (
    CapturedWindowFrame,
    WindowCaptureClient,
    WindowCaptureError,
    WindowFrameCapturer,
)


def region(**overrides):
    values = {
        "alias": "合成群",
        "window_title": "DealWatch SYNTHETIC",
        "rect": Rect(10, 20, 60, 40),
        "window_hwnd": 1234,
        "process_id": 5678,
        "coordinate_space": "frame",
        "reference_frame_width": 100,
        "reference_frame_height": 80,
    }
    values.update(overrides)
    return RegionConfig(**values)


class FakeWindows:
    def __init__(self, minimized: bool = False):
        self.minimized = minimized

    def desktop_locked(self):
        return False

    def window_from_hwnd(self, hwnd):
        return WindowInfo(
            hwnd=hwnd,
            title="DealWatch SYNTHETIC",
            class_name="Qt",
            rect=Rect(0, 0, 100, 80),
            process_id=5678,
            process_path="C:/WeChat/WeChat.exe",
            minimized=self.minimized,
        )


class FakeClient:
    def __init__(self, width: int = 100, height: int = 80):
        self.width, self.height = width, height
        self.calls = []
        self.rois = []

    def capture(self, hwnd, backend="wgc", *, roi=None, **kwargs):
        # 0.2.2 起 WindowCaptureClient.capture 多了 request_id/region_id 关键字
        # （阶段日志靠它们对齐）；T2.5 起还多了 roi（裁剪下推到子进程）。替身按同样
        # 的形状接收，并像真客户端一样：给了 roi 就只回传 ROI，同时保留整帧尺寸。
        self.calls.append((hwnd, backend))
        self.rois.append(roi)
        image = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        image[20:60, 10:70] = 180
        if roi is None:
            return CapturedWindowFrame(
                image=image,
                width=self.width,
                height=self.height,
                captured_at=123.0,
                backend=backend,
                frame_width=self.width,
                frame_height=self.height,
            )
        cropped = image[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width].copy()
        return CapturedWindowFrame(
            image=cropped,
            width=roi.width,
            height=roi.height,
            captured_at=123.0,
            backend=backend,
            frame_width=self.width,
            frame_height=self.height,
            roi=roi,
        )


class WindowFrameCapturerTests(unittest.TestCase):
    def test_captures_only_roi_in_frame_coordinates(self):
        client = FakeClient()
        capturer = WindowFrameCapturer(client=client, windows=FakeWindows())
        frame = capturer.capture(region())
        self.assertEqual(frame.image.shape, (40, 60, 3))
        self.assertEqual(frame.rect, Rect(10, 20, 60, 40))
        self.assertEqual(frame.sequence, 1)
        self.assertEqual(frame.captured_at, 123.0)
        self.assertEqual(client.calls, [(1234, "wgc")])

    def test_process_mismatch_and_minimized_pause(self):
        capturer = WindowFrameCapturer(client=FakeClient(), windows=FakeWindows())
        with self.assertRaises(CaptureError) as context:
            capturer.capture(region(process_id=9999))
        self.assertEqual(context.exception.code, CaptureErrorCode.PROCESS_MISMATCH)
        with self.assertRaises(CaptureError) as context:
            WindowFrameCapturer(
                client=FakeClient(), windows=FakeWindows(minimized=True)
            ).capture(region())
        self.assertEqual(context.exception.code, CaptureErrorCode.WINDOW_MINIMIZED)

    def test_class_title_and_process_path_identity_changes_pause(self):
        capturer = WindowFrameCapturer(client=FakeClient(), windows=FakeWindows())
        with self.assertRaises(CaptureError) as context:
            capturer.capture(region(window_class="Other"))
        self.assertEqual(context.exception.code, CaptureErrorCode.NEEDS_CALIBRATION)
        with self.assertRaises(CaptureError) as context:
            capturer.capture(region(window_title="Other group"))
        self.assertEqual(context.exception.code, CaptureErrorCode.NEEDS_CALIBRATION)
        with self.assertRaises(CaptureError) as context:
            capturer.capture(region(process_path="C:/other.exe"))
        self.assertEqual(context.exception.code, CaptureErrorCode.PROCESS_MISMATCH)

    def test_frame_size_change_requires_recalibration(self):
        capturer = WindowFrameCapturer(client=FakeClient(width=101), windows=FakeWindows())
        with self.assertRaises(CaptureError) as context:
            capturer.capture(region())
        self.assertEqual(context.exception.code, CaptureErrorCode.WINDOW_RESIZED)

    def test_worker_error_maps_to_capture_error(self):
        class ErrorClient:
            def capture(self, hwnd, backend="wgc", **kwargs):
                raise WindowCaptureError(CaptureErrorCode.BLACK_FRAME, "BLACK_FRAME")

        capturer = WindowFrameCapturer(client=ErrorClient(), windows=FakeWindows())
        with self.assertRaises(CaptureError) as context:
            capturer.capture(region())
        self.assertEqual(context.exception.code, CaptureErrorCode.BLACK_FRAME)


class FakeProcess:
    """``subprocess.Popen`` 的替身：真实文本管道用 ``io.StringIO``，行为按剧本走。

    0.2.2 把 ``subprocess.run`` 换成显式 ``Popen`` 以自控预算，补丁点因此从
    ``subprocess.run`` 收到模块级别名 ``deal_watch.window_capture.spawn``；
    下面三条用例的断言内容与错误码与 0.2.1 完全一致。
    """

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        *,
        stall_first_wait: bool = False,
        ignore_kill: bool = False,
    ) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False
        self.waits = 0
        self._stall_first_wait = stall_first_wait
        self._ignore_kill = ignore_kill

    def wait(self, timeout=None):
        self.waits += 1
        if self._stall_first_wait and self.waits == 1:
            raise subprocess.TimeoutExpired(cmd=["worker"], timeout=timeout or 0)
        if self._ignore_kill and self.killed:
            raise subprocess.TimeoutExpired(cmd=["worker"], timeout=timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if not self._ignore_kill:
            self.returncode = -9


class WindowCaptureClientTests(unittest.TestCase):
    @staticmethod
    def png_payload(width: int = 4, height: int = 3) -> str:
        image = Image.new("RGB", (width, height), (20, 30, 40))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def payload(self, **overrides) -> str:
        """合法协议的最小整帧 payload：width/height 与 frame_* 必须一致。"""
        data = {
            "ok": True,
            "width": 4,
            "height": 3,
            "frame_width": 4,
            "frame_height": 3,
            "roi": None,
            "captured_at": 12.5,
            "image": self.png_payload(),
            "backend": "wgc",
        }
        data.update(overrides)
        return json.dumps(data)

    def test_decodes_valid_worker_payload(self):
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stdout=self.payload())
            frame = client.capture(123)
        self.assertEqual((frame.height, frame.width), (3, 4))
        self.assertEqual((frame.frame_width, frame.frame_height), (4, 3))
        self.assertEqual(frame.backend, "wgc")
        self.assertEqual(frame.image.shape, (3, 4, 3))
        self.assertIsNone(frame.roi)

    def test_roi_request_returns_cropped_frame_without_parent_recrop(self):
        """ROI 路径：只解码子进程回传的 ROI，父进程不得再裁一次。"""
        client = WindowCaptureClient()
        roi = Rect(2, 1, 4, 3)
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(
                stdout=self.payload(frame_width=10, frame_height=8, roi=[2, 1, 4, 3])
            )
            frame = client.capture(123, roi=roi)
        command = spawn.call_args.args[0]
        self.assertIn("--roi", command)
        self.assertEqual(command[command.index("--roi") + 1], "2,1,4,3")
        self.assertEqual(spawn.call_count, 1, "ROI 请求不得重发第二次")
        self.assertEqual(frame.image.shape, (3, 4, 3))
        self.assertEqual((frame.width, frame.height), (4, 3))
        self.assertEqual((frame.frame_width, frame.frame_height), (10, 8))
        self.assertEqual(frame.roi, roi)

    def test_missing_frame_fields_with_roi_is_version_mismatch_not_retry(self):
        """老组件不认识 --roi：明确报版本不匹配，绝不能悄悄重发整帧请求。"""
        legacy = json.dumps(
            {
                "ok": True,
                "width": 10,
                "height": 8,
                "captured_at": 12.5,
                "image": self.png_payload(10, 8),
                "backend": "wgc",
            }
        )
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stdout=legacy)
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123, roi=Rect(2, 1, 4, 3))
        self.assertEqual(spawn.call_count, 1, "同一请求内自动重新采集是禁止的")
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertIn("版本不匹配", str(context.exception))
        self.assertEqual(context.exception.trace.stage_failed, "component_version")

    def test_missing_roi_echo_with_roi_request_is_protocol_failure(self):
        payload = json.loads(self.payload(frame_width=10, frame_height=8))
        payload.pop("roi")
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stdout=json.dumps(payload))
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123, roi=Rect(2, 1, 4, 3))
        self.assertEqual(spawn.call_count, 1)
        self.assertIn("版本不匹配", str(context.exception))

    def test_roi_echo_mismatch_is_protocol_failure(self):
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(
                stdout=self.payload(frame_width=10, frame_height=8, roi=[0, 0, 4, 3])
            )
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123, roi=Rect(2, 1, 4, 3))
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertIn("roi", str(context.exception))
        self.assertEqual(context.exception.trace.stage_failed, "protocol")

    def test_declared_size_contradicting_pixels_is_protocol_failure(self):
        """width/height 说 5x3，实际 PNG 是 4x3：不能当成成功帧。"""
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stdout=self.payload(width=5))
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertIn("尺寸", str(context.exception))

    def test_wrong_field_type_is_protocol_failure(self):
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stdout=self.payload(width="4"))
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertIn("整数", str(context.exception))

    def test_full_frame_payload_must_not_carry_roi(self):
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stdout=self.payload(roi=[0, 0, 4, 3]))
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertIn("roi", str(context.exception))

    def test_timeout_is_mapped_and_late_result_discarded(self):
        # Leave room for Windows thread scheduling; the fake wait injects timeout.
        client = WindowCaptureClient(timeout=0.2)
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stall_first_wait=True)
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_TIMEOUT)

    def test_invalid_output_is_rejected(self):
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn") as spawn:
            spawn.return_value = FakeProcess(stdout="not json", returncode=1)
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(123)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_FAILED)


if __name__ == "__main__":
    unittest.main()
