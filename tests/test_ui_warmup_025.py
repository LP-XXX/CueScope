"""Startup summaries never claim that an unprocessed region is monitoring."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from deal_watch.models import HealthSnapshot, RegionStatus  # noqa: E402
from deal_watch.ui.main import MainWindow  # noqa: E402
from region_fixtures import ready_region  # noqa: E402


def pump_until(app, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert condition()


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    widget = MainWindow(Path(__file__).resolve().parents[1])
    pump_until(app, lambda: widget._recognition_ready)
    widget.config.regions = [ready_region(f"合成区域 {i}", id=f"r{i}") for i in range(5)]
    widget.config.keywords = ["合成关键词"]
    widget.webhook = "http://127.0.0.1:18080/ok"
    widget._load_regions()
    monkeypatch.setattr(widget, "queue_system_message", Mock())
    monkeypatch.setattr(widget.tray, "showMessage", Mock())
    monkeypatch.setattr(widget, "readiness_problems", lambda _region: [])
    with (
        patch("deal_watch.ui.main.MonitorService") as service,
        patch("deal_watch.ui.main.LocalOcr"),
        patch("deal_watch.ui.main.WindowFrameCapturer"),
    ):
        # No background captures or health emissions: the warmup gate is held.
        service.return_value.start.return_value = None
        widget.start_monitoring()
    yield widget
    widget.service = None
    widget._stopping_service = None
    widget._saving = False
    widget._pending_save = None
    widget._failed_save_stage = ""
    widget.exit_app()
    pump_until(app, lambda: widget._shutdown_done)
    widget.deleteLater()
    app.processEvents()


def emit(window, index, state, detail="合成健康状态"):
    window.on_health(HealthSnapshot(f"r{index}", state, detail))


def summaries(window):
    return (window.tray.toolTip(), window.shell.status.text(), window.statusBar().currentMessage())


def test_initial_unknown_and_warmup_do_not_claim_monitoring(window):
    for text in summaries(window):
        assert "准备" in text
        assert "监控中" not in text
    for index in range(5):
        emit(window, index, RegionStatus.BASELINING, "采集组件正在预热，尚未开始采样")
    for text in summaries(window):
        assert "准备" in text
        assert "监控中" not in text
    window.queue_system_message.assert_not_called()


def test_baseline_completion_is_required_before_normal_summary(window):
    for index in range(5):
        emit(window, index, RegionStatus.BASELINING, "组件已就绪，等待新帧建立基线")
    for text in summaries(window):
        assert "建立基线 5" in text
        assert "监控中" not in text
    for index in range(5):
        emit(window, index, RegionStatus.RUNNING)
    for text in summaries(window):
        assert "监控中（5 个区域）" in text


def test_mixed_states_report_separate_counts(window):
    emit(window, 0, RegionStatus.RUNNING)
    emit(window, 1, RegionStatus.ERROR)
    emit(window, 2, RegionStatus.PAUSED)
    emit(window, 3, RegionStatus.BASELINING)
    for text in summaries(window):
        for expected in ("采样 1", "异常 1", "暂停 1", "建立基线 1", "准备 1"):
            assert expected in text
        assert "监控中" not in text
    assert [call.args[0] for call in window.queue_system_message.call_args_list] == ["监控故障"]
    emit(window, 1, RegionStatus.ERROR)
    emit(window, 1, RegionStatus.BASELINING)
    emit(window, 1, RegionStatus.RUNNING)
    emit(window, 1, RegionStatus.RUNNING)
    assert [call.args[0] for call in window.queue_system_message.call_args_list] == ["监控故障", "监控恢复"]


def test_all_paused_keeps_existing_pause_summary(window):
    for index in range(5):
        emit(window, index, RegionStatus.PAUSED)
    assert window.tray_summary() == "CueScope｜已暂停"
    assert window.shell.status.text() == "已暂停"
    assert "监控中" not in window.statusBar().currentMessage()


@pytest.mark.parametrize("field,value", [
    ("_saving", True), ("_pending_save", ("add", None, None)),
    ("_failed_save_stage", "replace"), ("_settings_busy", True),
])
def test_health_does_not_overwrite_save_progress_or_retained_draft(window, field, value):
    setattr(window, field, value)
    window.save_status.setText("配置保存尚未完成，请重试")
    window.statusBar().showMessage("保存任务仍在处理中")
    emit(window, 0, RegionStatus.RUNNING)
    assert window.save_status.text() == "配置保存尚未完成，请重试"
    assert window.statusBar().currentMessage() == "保存任务仍在处理中"
    assert "采样 1" in window.tray.toolTip()


def test_temporary_statusbar_action_is_not_overwritten(window):
    window.statusBar().showMessage("预览已经完成")
    for index in range(5):
        emit(window, index, RegionStatus.RUNNING)
    assert window.statusBar().currentMessage() == "预览已经完成"
    assert "监控中（5 个区域）" in window.shell.status.text()
