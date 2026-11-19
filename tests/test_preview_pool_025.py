"""监控中的用户预览借用已预热池，不创建第三个采集进程。"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from deal_watch.config import ModelConfig
from deal_watch.models import FrameSample, OcrObservation
from deal_watch.ui.main import MainWindow
from deal_watch.ui.region import PreviewWorker
from region_fixtures import ready_region


@pytest.mark.parametrize("persistent", [True, False, None])
def test_main_preview_borrows_only_running_persistent_client(tmp_path, persistent):
    region = ready_region(previewed=True)
    client = SimpleNamespace(persistent=persistent)
    window = SimpleNamespace(
        _shutdown_started=False, _saving=False, _settings_busy=False,
        selected_region=lambda: region, _decision_rules=lambda: None,
        config=SimpleNamespace(regions=[region], model=ModelConfig(), keywords=[]),
        base_dir=tmp_path, manual_send=Mock(),
        service=SimpleNamespace(capturer=SimpleNamespace(client=client)) if persistent is not None else None,
    )
    with patch("deal_watch.ui.main.PreviewDialog") as dialog:
        MainWindow.preview_region.__wrapped__(window)
    assert dialog.call_count == 1
    if persistent:
        assert dialog.call_args.kwargs["capture_client"] is client
    else:
        assert "capture_client" not in dialog.call_args.kwargs
    window.manual_send.assert_not_called()


def test_preview_worker_uses_borrowed_client_without_spawning_or_closing(tmp_path):
    region = ready_region()
    image = np.zeros((20, 20, 3), dtype=np.uint8)
    frame = FrameSample(region.id, 1, time.time(), image, region.rect)
    observation = OcrObservation(region.id, frame.captured_at, 1, [])
    shared_client = Mock(persistent=True)
    captured = Mock(return_value=frame)
    with (
        patch("deal_watch.ui.region.WindowCaptureClient") as constructor,
        patch("deal_watch.ui.region.WindowFrameCapturer", return_value=SimpleNamespace(capture=captured)) as adapter,
        patch("deal_watch.ui.region.LocalOcr") as ocr,
        patch("deal_watch.ui.region.array_to_qimage", return_value=None),
    ):
        ocr.return_value.recognize.return_value = observation
        worker = PreviewWorker(region, ModelConfig(), tmp_path, capture_client=shared_client)
        results = []
        worker.completed.connect(lambda *args: results.append(args))
        worker.run()
    constructor.assert_not_called()
    adapter.assert_called_once_with(client=shared_client)
    captured.assert_called_once_with(region, force=True)
    ocr.return_value.recognize.assert_called_once_with(frame)
    assert results == [(None, observation, "")]
    shared_client.close.assert_not_called()


def test_supplied_preview_frame_does_not_acquire_capture_client(tmp_path):
    region = ready_region()
    frame = FrameSample(region.id, 1, time.time(), np.zeros((2, 2, 3), dtype=np.uint8), region.rect)
    with (
        patch("deal_watch.ui.region.WindowCaptureClient") as constructor,
        patch("deal_watch.ui.region.WindowFrameCapturer") as adapter,
        patch("deal_watch.ui.region.LocalOcr"),
        patch("deal_watch.ui.region.array_to_qimage", return_value=None),
    ):
        worker = PreviewWorker(region, ModelConfig(), tmp_path, frame)
        worker.run()
    constructor.assert_not_called()
    adapter.assert_not_called()
