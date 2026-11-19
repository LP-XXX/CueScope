"""Four-page desktop shell and the existing monitoring action coordinators."""

from __future__ import annotations

import copy
import csv
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from functools import wraps
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..brand import DISPLAY_NAME, brand_icon
from ..config import ConfigStore, default_data_dir, validate_webhook_url
from ..content import CONTENT_MODES
from ..diagnostics import close_logging, configure_logging, title_fingerprint
from ..health import IncidentTracker
from ..health_queue import HealthEventWriter
from ..metrics import OcrStageRecorder, TimingRecorder
from ..models import (
    HealthSnapshot,
    MatchEvent,
    MessageCandidate,
    MessageDecision,
    RegionConfig,
    RegionStatus,
)
from ..normalize import content_digest
from ..notify import FeishuClient, NotificationDispatcher, NotifyMetrics
from ..ocr import LocalOcr
from ..readiness import readiness_summary, region_readiness_problems
from ..recognition import RecognitionStore
from ..region_save import (
    ACTION_LABELS,
    RegionSaveResult,
    apply_region_change,
    commit_config_change,
    commit_region_change,
)
from ..rules import RuleEngine
from ..security import CredentialStore
from ..service import MonitorService
from ..storage import Storage
from ..window_capture import WindowFrameCapturer
from .icons import icon
from .pages import RecordDetailDialog, build_keywords_page, build_records_page, build_settings_page
from .region import (
    PreviewDialog,
    RegionEditDialog,
    available_screen_geometry,
    close_region_dialogs,
    wait_ui_workers,
)
from .region_list import RegionColumn, RegionRole, RegionTable
from .shell import AppShell, PageId, RecordView
from .theme import ThemeManager

# 识别记录“处理结果”列：只把已知的运行时判定值翻成中文，其他保持原值。
DECISION_LABELS: dict[str, str] = {
    "not_matched": "未命中",
    "excluded_by_rule": "被排除条件过滤",
    "rule_configuration_error": "规则配置无效",
    "rule_input_too_large": "文本超出规则预算",
    "queued": "判断应通知·已入队",
    "duplicate": "判断应通知·重复合并",
    "already_processed": "同一消息已处理",
    "boundary_uncertain": "边界不确定，未判断",
    "unsupported_message_type": "非普通文字，跳过",
}

# 通知记录“送达状态”列：平台回执与本地判断分列显示，不混在同一列。
DELIVERY_LABELS: dict[str, str] = {
    "local": "已本地记录",
    "pending": "等待发送",
    "sent": "飞书已接受",
    "failed": "发送失败",
    "unknown": "结果未知",
    "expired": "过期未补发",
    "duplicate": "已合并重复",
}

_WORKER_STATE_LABELS = {
    "ready": "组件就绪",
    "working": "正在采集",
    "busy": "正在采集",
    "starting": "等待组件就绪",
    "warming": "等待组件就绪",
    "waiting": "等待组件就绪",
    "reaping": "正在回收",
    "recovering": "正在回收",
    "reaped": "异常组件已回收",
    "unavailable": "等待采集组件恢复",
    "blocked": "回收未完成",
    "manual_action": "需人工处理",
    "closed": "采集组件已停止",
}


def health_status_text(health: HealthSnapshot) -> str:
    """区域行显示可操作的中文摘要，技术编号只在详情保留。"""
    labels = {
        RegionStatus.DISABLED: "已停用", RegionStatus.BASELINING: "建立基线",
        RegionStatus.RUNNING: "监控中", RegionStatus.PAUSED: "已暂停",
        RegionStatus.ERROR: "监控故障",
    }
    label = labels.get(health.status, health.status.value)
    if health.status is RegionStatus.ERROR:
        detail = health.capture_error_summary or "处理失败，详见提示"
        worker = _WORKER_STATE_LABELS.get(health.capture_worker_state, "")
        lines = [f"{label} · {worker}" if worker else label, detail]
        if health.next_retry_at:
            retry = datetime.fromtimestamp(health.next_retry_at).strftime("%H:%M:%S")
            lines.append(f"下次重试 {retry}")
        return "\n".join(lines)
    if health.status is RegionStatus.RUNNING:
        return f"{label}\n处理延迟" if health.overloaded else label
    detail = health.detail
    for prefix in (f"{label}：", f"{label}；", f"{label}: "):
        if detail.startswith(prefix):
            detail = detail[len(prefix):]
            break
    lines = [label]
    if detail and detail != label:
        lines.append(detail)
    if health.overloaded:
        lines.append("处理延迟")
    return "\n".join(lines)


def health_performance_text(health: HealthSnapshot) -> str:
    """三行直接显示新鲜度与当前耗时；完整日期和分位数留在详情。"""
    latest = (
        datetime.fromtimestamp(health.last_success_at).strftime("%H:%M:%S")
        if health.last_success_at else "尚无"
    )
    actual = f"{health.actual_interval_ms / 1000:.1f}s" if health.actual_interval_ms else "尚无"
    return (
        f"最近成功 {latest}\n"
        f"OCR {health.last_ocr_ms:.0f}ms\n"
        f"实际间隔 {actual}"
    )


def health_diagnostics_text(health: HealthSnapshot) -> str:
    """健康快照已包含后台采集的数值；渲染不查询进程或磁盘。"""
    parts = [health.detail, f"本次运行故障事件：{health.capture_fault_count}"]
    if health.capture_recovery_ms:
        parts.append(f"最近恢复耗时：{health.capture_recovery_ms / 1000:.2f} 秒")
    if health.capture_error_stage:
        parts.append(f"故障阶段：{health.capture_error_stage}")
    if health.stages_summary:
        parts.append(f"分段统计（毫秒）：{health.stages_summary}")
    stats = health.capture_process_stats
    for key, label in (
        ("process_starts", "组件启动次数"), ("failures", "组件失败次数"),
        ("active_processes", "占用进程额度"), ("orphan_processes", "待回收进程"),
    ):
        if key in stats:
            parts.append(f"{label}：{stats[key]}")
    for process in stats.get("processes", [])[:2]:
        if not isinstance(process, dict):
            continue
        metrics = []
        for key, label in (("private_bytes", "专用内存"), ("working_set_bytes", "工作集")):
            value = process.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                metrics.append(f"{label} {value / 1024 / 1024:.1f} MiB")
        for key, label in (("handles", "句柄"), ("threads", "线程")):
            if key in process:
                metrics.append(f"{label} {process[key]}")
        parts.append(f"PID {process.get('pid', '未知')}：{' / '.join(metrics)}")
    return "\n".join(parts)

# 两张表各自只显示最近 N 条；查询耗时与重填成本都跟 N 成正比。
RECORDS_LIMIT = 200
RECOGNITION_PAGE_SIZE = 200

# 界面心跳周期（毫秒）。数据刷新与通知健康检查各自独立定时，周期相同但互不依赖。
UI_HEARTBEAT_MS = 1500

# 区域配置变更被拒绝时的统一提示（0.2.2 设计 3.1 T1.3）。
PAUSE_FIRST_HINT = "请先全部暂停并等待停止完成"
SAVING_HINT = "正在保存区域配置，请稍候再试"
SAVED_NOT_CONFIRMED_HINT = "已保存，待确认"

# 保存失败阶段里“原样再提交一次就可能有救”的那些（0.2.2 D1）：
# 全部是环境性故障（同步盘/杀软占用、磁盘满、回读被改写、后台线程兜底异常），
# 换一次时机会成功。``validate``/``apply`` 属于用户输入或数据问题，重试同一份
# 内容必然再失败，所以不提供“重试保存”，而是提示用户修正。
RETRYABLE_SAVE_STAGES = frozenset({"mkdir", "write", "replace", "verify", "io", "internal"})

# 失败提示里对“重试保存”出口的说明；措辞必须与实际能力一致。
RETRY_PENDING_HINT = "点“重试保存”即可原样再写一次，不必重新采集窗口或重新圈选"


def decision_label(value: str) -> str:
    """把消息判定代码翻成中文；未知值原样返回，避免隐藏新状态。"""
    return DECISION_LABELS.get(value, value)


def delivery_label(value: str) -> str:
    """把投递状态代码翻成中文；未知值原样返回。"""
    return DELIVERY_LABELS.get(value, value)


def config_slot(title: str):
    """配置类槽函数的保护壳：异常必须留痕，并弹出可理解的中文提示。

    Qt 槽函数里逃逸的异常在打包为窗口程序后只进不可见的 stderr，用户看到的
    就是“点了确定没反应”。这里统一兜住：写日志 + 弹 `critical`，进程不退出。
    """

    def decorator(method: Callable) -> Callable:
        @wraps(method)
        def wrapper(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except Exception as error:  # noqa: BLE001 - 槽函数不得让异常逃逸
                logger = getattr(self, "logger", None) or logging.getLogger("deal_watch")
                logger.exception("%s 失败：%s", title, type(error).__name__)
                QMessageBox.critical(
                    self,
                    f"{title}失败",
                    f"{title}未完成：{type(error).__name__}: {error}\n\n"
                    "已写入日志文件。区域配置没有被写入，请按提示修正后重试。",
                )
                return None

        return wrapper

    return decorator


def _noop(_config) -> None:
    """启动前快照用的空变更：只写盘，不改任何字段。"""


def wrapped_label(text: str, *, object_name: str = "") -> QLabel:
    """长说明/长路径用的标签：换行 + 不参与主窗口最小宽度计算。

    0.2.2 之前这些说明都是单行 ``QLabel``，它们的 ``minimumSizeHint`` 就是整行
    文字的宽度，于是隐藏页签也能把主窗口最小宽度顶到 2000 像素以上——用户看到
    的现象就是“窗口缩不小”。仅换行还不够：``sizeHint`` 仍会参与布局，必须把
    水平策略设为 ``Ignored`` 并显式放开最小宽度，主窗口才能真正缩到 760。
    """
    label = QLabel(text)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if object_name:
        label.setObjectName(object_name)
    return label


class Bridge(QObject):
    events = Signal(object)
    health = Signal(object)
    test_result = Signal(str)
    monitor_stopped = Signal(object)
    shutdown_finished = Signal()
    export_finished = Signal(str)
    recognition_prepared = Signal(str)
    settings_saved = Signal(object)


class RegionSaveCoordinator(QObject):
    """把区域配置写盘挪到后台线程，结果经信号回到 UI 线程。

    事务顺序由 :mod:`deal_watch.region_save` 保证：校验 → 写临时文件 → 回读校验
    临时文件 → 原子替换。内存提交**必须**留在 UI 线程（``commit_memory=False``）：
    跨线程改 ``config.regions`` 会让表格与服务读到半成品，跨线程 ``setItem`` 更是
    禁止的。
    """

    saved = Signal(object)
    failed = Signal(object)

    def __init__(self, config_store: ConfigStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config_store = config_store
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def request_save(
        self,
        config,
        action: str,
        region: RegionConfig | None = None,
        index: int | None = None,
        *,
        windows=None,
    ) -> bool:
        """发起一次后台保存；保存中重复提交返回 ``False``（不排队、不叠加）。"""
        if self._busy:
            return False
        self._busy = True

        def run() -> None:
            try:
                result = commit_region_change(
                    self.config_store,
                    config,
                    action,
                    region,
                    index,
                    windows=windows,
                    commit_memory=False,
                )
            except Exception as error:  # noqa: BLE001 - 后台线程不得静默死亡
                result = RegionSaveResult(
                    ok=False,
                    region_id=str(getattr(region, "id", "") or ""),
                    action=action,
                    error=f"保存失败（{type(error).__name__}: {error}）。",
                    error_stage="internal",
                )
            self._busy = False
            if result.ok:
                self.saved.emit(result)
            else:
                self.failed.emit(result)

        threading.Thread(target=run, name="region-save", daemon=True).start()
        return True


class RecordsRefreshCoordinator(QObject):
    """决定"要不要重查一次表格"，并在后台线程执行查询。

    0.2.2 之前是每 1.5 秒无条件重查两张表并整表重填（200 行 × 8 列 = 1600 个
    ``QTableWidgetItem``）。三条规则各挡掉一类无用功，缺一条就退回去：

    1. **页面不可见不查** —— 窗口最小化到托盘/被收起时没人看，查了也是白查；
    2. **数据版本号没变不查** —— 没有新数据时，查回来的必然与现在显示的一样；
    3. **已有一次在途就不发第二次** —— 慢查询会叠罗汉：越卡越查、越查越卡。

    查询必须在后台线程：``list_events`` 要拿 :class:`Storage` 的锁，而服务线程
    正在同一把锁上写事件；在 UI 线程等这把锁会把界面卡成假死。

    结果经 ``rows_ready`` 信号回主线程——``MainWindow`` 是信号接收者，跨线程
    发射由 Qt 自动排队，``setItem`` 因此一定发生在 UI 线程。

    两条收尾规则同样重要：

    * **过期结果丢弃** —— 查询期间版本号又变了，这批结果已经落后于最新写入，
      直接丢掉且**不标记成已送达**，下一拍自然重查；否则用户会看到一闪而过的
      旧状态，还要再等一拍才被纠正。
    * **数据没变不重绘** —— 版本号变了但可见字段完全一样时，由界面侧比对后跳过
      整表重建（见 ``MainWindow._apply_records``）。
    """

    rows_ready = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        fetch: Callable[[], list[dict]],
        parent: QObject | None = None,
        *,
        name: str = "records-refresh",
        revision_source: Callable[[], int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._fetch = fetch
        self._name = name
        # 版本号的**现读**入口（内存短锁，不查库）：只用来判断结果是否已过期。
        # 为 ``None`` 时不做过期判断，保持 0.2.1 行为（供不关心版本号的调用方使用）。
        self._revision_source = revision_source
        self._busy = False
        self._closed = False
        self._rerun = False
        # 最近一次**成功送达**的数据版本号；``None`` 表示还没查过。
        self._delivered_revision: int | None = None
        self._thread: threading.Thread | None = None
        # 状态机跨两个线程（UI 心跳 + 查询线程），"在途"判断必须是原子的：
        # 靠 GIL 保证不了"读 _busy → 决定起线程"这一段不被打断。
        self._state_lock = threading.Lock()
        # 只增不减的计数器，供离线用例断言"到底查了几次"。
        self.query_count = 0
        self.failure_count = 0
        self.delivered_count = 0
        self.stale_count = 0

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._busy

    @property
    def delivered_revision(self) -> int | None:
        with self._state_lock:
            return self._delivered_revision

    def tick(self, visible: bool, revision: int) -> bool:
        """心跳入口：可见性 + 版本号都通过才发起查询；返回是否真的发起。"""
        if not visible:
            return False
        return self.request(revision)

    def request(self, revision: int | None = None, *, force: bool = False) -> bool:
        """发起一次查询。``force`` 用于用户点"刷新"：忽略版本号，但不打破在途保护。

        在途时收到 ``force`` 不丢：记下来，当前查询结束后补一次，
        否则用户点了"刷新"却什么都没发生。
        """
        with self._state_lock:
            if self._closed:
                return False
            if not force and revision is not None and revision == self._delivered_revision:
                return False
            if self._busy:
                if force:
                    self._rerun = True
                return False
            self._busy = True
            self.query_count += 1
            thread = threading.Thread(
                target=self._run, args=(revision,), name=self._name, daemon=True
            )
            self._thread = thread
        thread.start()
        return True

    def shutdown(self, timeout: float = 2.0) -> bool:
        """停止接收新请求，并等在途线程收尾；返回**是否已经空闲**。

        等线程退出是必要的：线程里若在窗口销毁后才 ``emit``，就是往一个已经
        析构的 QObject 发信号。
        """
        with self._state_lock:
            self._closed = True
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        return not (thread is not None and thread.is_alive())

    def _run(self, revision: int | None) -> None:
        """查询线程主体。用户强制刷新时在**同一线程**里补跑一次，不再起新线程。"""
        while True:
            rows: list[dict] = []
            error = ""
            try:
                rows = self._fetch()
            except Exception as exc:  # noqa: BLE001 - 后台线程不得静默死亡
                error = f"{type(exc).__name__}: {exc}"

            with self._state_lock:
                self._busy = False
                if self._closed:
                    return
                # 查询期间版本号又变了：这批结果已过期，丢掉且不记版本号。
                # 判断放在锁内是为了与 request() 的“读已送达版本号”互斥。
                stale = bool(
                    not error
                    and self._revision_source is not None
                    and revision is not None
                    and self._revision_source() != revision
                )
                # 先把结果状态记好再发信号：处理函数里若再 tick，看到的必须是"已完成"。
                if error:
                    self.failure_count += 1
                elif stale:
                    self.stale_count += 1
                else:
                    self._delivered_revision = revision
                    self.delivered_count += 1
                rerun = self._rerun
                self._rerun = False
                if rerun:
                    self._busy = True
                    self.query_count += 1

            if error:
                # 不记版本号：这一版数据没送达，下一拍还要重试。
                self.failed.emit(error)
            elif not stale:
                self.rows_ready.emit(rows)
            if not rerun:
                return


def _delete_dialog_later(dialog) -> None:
    # The result/draft is copied before deletion; detached QThreads have their own owner.
    delete = getattr(dialog, "deleteLater", None)
    if callable(delete):
        delete()


class MainWindow(QMainWindow):
    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.data_dir = default_data_dir()
        self.theme_manager = ThemeManager.for_app(QApplication.instance(), self.data_dir)
        self.health_snapshots: dict[str, HealthSnapshot] = {}
        self._notification_records: dict[str, dict] = {}
        self._recognition_records: dict[str, dict] = {}
        self.logger = configure_logging(self.data_dir)
        self.config_store = ConfigStore(self.data_dir)
        self.config = self.config_store.load()
        self.logger.info(
            "startup version=%s config_version=%s interval_ms=%s configured=%s record_limit=%s",
            __version__, self.config.version, self.config.poll_interval_ms,
            len(self.config.regions), self.config.recognition_record_limit,
        )
        self.credentials = CredentialStore(self.data_dir)
        try:
            self.webhook, self.secret = self.credentials.load()
        except Exception:
            self.logger.exception("Failed to load DPAPI credentials")
            self.webhook, self.secret = "", ""
        self._credential_snapshot = (self.webhook, self.secret)
        self.storage = Storage(self.data_dir / "deal-watch.db")
        self.storage.cleanup()
        self.health_writer = HealthEventWriter(self.storage, self.config.dedupe_seconds, logger=self.logger)
        self.capture_incidents = IncidentTracker()
        self._announced_health_ids = []
        self._record_kind = "all"
        self._event_view_revision = 0
        self.recognition = RecognitionStore(self.data_dir, self.config.recognition_record_limit)
        self._recognition_ready = False
        self._recognition_thread: threading.Thread | None = None
        self._settings_thread: threading.Thread | None = None
        self._settings_busy = False
        self._recognition_page = 0
        self._recognition_total = 0
        self._recognition_view_revision = 0
        # 全链路共用一个分段耗时记录器：OCR 子段、服务调度段、通知段写进同一份
        # 环形缓冲，界面的健康快照与验收探针因此读到的是同一组样本，而不是三个
        # 互不相干的计数器。只在进程内累积，环形容量固定，不会随时间增长。
        self.metrics = TimingRecorder()
        self.bridge = Bridge()
        self.bridge.events.connect(self.on_events)
        self.bridge.health.connect(self.on_health)
        self.bridge.test_result.connect(self.on_test_result)
        self.bridge.monitor_stopped.connect(self._on_monitor_stopped)
        self.bridge.shutdown_finished.connect(self._finish_shutdown)
        self.bridge.export_finished.connect(self._on_export_finished)
        self.bridge.recognition_prepared.connect(self._on_recognition_prepared)
        self.bridge.settings_saved.connect(self._on_settings_saved)
        # 区域配置写盘一律走后台协调器；内存提交仍在本线程（见 _on_region_saved）。
        self.save_coordinator = RegionSaveCoordinator(self.config_store, self)
        self.save_coordinator.saved.connect(self._on_region_saved)
        self.save_coordinator.failed.connect(self._on_region_save_failed)
        self._saving = False
        # ``_pending_save`` 有两种含义，靠 ``_failed_save_stage`` 区分（0.2.2 D1）：
        #   1. 保存进行中：本次提交的 (action, region, index)；
        #   2. 保存失败后：**留在内存里的待重试草稿**，环境恢复后点“重试保存”即可
        #      重发，用户不必重新采集窗口、重新拖选区。成功或用户改为其他变更时清空。
        self._pending_save: tuple[str, RegionConfig | None, int | None] | None = None
        self._failed_save_stage = ""
        self._failed_save_message = ""
        self._pending_after_stop: Callable[[], None] | None = None
        self.service: MonitorService | None = None
        self.dispatcher = NotificationDispatcher(
            self.storage,
            lambda: FeishuClient(*self._credential_snapshot),
            ttl_seconds=self.config.event_ttl_seconds,
            metrics=NotifyMetrics(self.metrics),
            delivery_enabled=lambda: bool(self._credential_snapshot[0]) and self.config.notification_mode != "local",
        )
        self.dispatcher.start()
        self.quitting = False
        self._shutdown_done = False
        self._shutdown_started = False
        self._stopping_service: MonitorService | None = None
        # 故障事件化：region_id → 最近一次故障详情。只有“进入故障”时才入队一次，
        # 持续失败期间 BASELINING↔ERROR 反复上报不得重复推送飞书。
        self.active_faults: dict[str, str] = {}
        self._export_thread: threading.Thread | None = None
        self.region_rows: dict[str, int] = {}
        self.health_states: dict[str, str] = {}
        self._monitor_status_message: str | None = None
        # 最近一次 health 的 detail：状态栏/托盘需要区分“未就绪”“用户暂停”“最小化”。
        self.health_details: dict[str, str] = {}
        self.network_failed = False
        # 两张表最近一次真正填进去的内容签名：数据没变就跳过整表重建。
        self._records_signature: tuple | None = None
        self._candidates_signature: tuple | None = None
        # 通知健康检查的最近一次执行时刻；独立定时，不依赖记录表是否在刷新。
        self.notification_health_checked_at = 0.0
        self.setWindowTitle(f"{DISPLAY_NAME} {__version__}")
        self.setWindowIcon(brand_icon())
        # 0.2.3：默认 1000×720 逻辑像素，最小 760×520；首次显示再按所在屏幕可用区收缩。
        # 之前没有设最小尺寸，实际最小宽度由各页签里不换行的长文本撑到 2000 像素以上。
        self.resize(1000, 720)
        self.setMinimumSize(760, 520)
        self._initial_geometry_applied = False
        self._build_ui()
        self._build_tray()
        self._load_regions()
        self._load_settings()
        # 两张表各有自己的协调器：分别用 records_revision / records_revision
        # 判断要不要重查，且各自最多一个在途查询（互不阻塞）。``revision_source``
        # 让协调器在结果回主线程前判断“这批数据是否已被更新的写入超越”。
        self.records_refresh = RecordsRefreshCoordinator(
            lambda: self.storage.list_events(RECORDS_LIMIT, event_kind=self._record_kind),
            self,
            name="records-refresh",
            revision_source=self._events_revision,
        )
        self.records_refresh.rows_ready.connect(self._apply_records)
        self.records_refresh.failed.connect(self._on_records_failed)
        self.recognition_refresh = RecordsRefreshCoordinator(
            self._fetch_recognition_page,
            self,
            name="recognition-refresh",
            revision_source=self._recognition_revision,
        )
        self.recognition_refresh.rows_ready.connect(self._apply_candidates)
        self.recognition_refresh.failed.connect(self._on_candidates_failed)
        # 启动时窗口还没显示、也不在记录页：这里不会产生查询（可见性闸门），
        # 真正第一次查询发生在用户切到对应页签时（见 _on_tab_changed）。
        self.refresh_records()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_runtime_views)
        self.timer.start(UI_HEARTBEAT_MS)
        # 通知健康检查单独一条定时：用户切到别的页签、或窗口被收起时，记录表可以
        # 不刷新，但“飞书是否还在报错”必须照常更新，否则出问题看不见。
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.check_notification_health)
        self.health_timer.start(UI_HEARTBEAT_MS)
        # 状态栏只放短状态：完整路径在“设置”页，避免长路径把窗口最小宽度顶大。
        self.statusBar().messageChanged.connect(lambda text: self.statusBar().setVisible(bool(text)))
        self.statusBar().hide()
        self._begin_recognition_prepare()

    def _begin_recognition_prepare(self) -> None:
        if self._recognition_thread and self._recognition_thread.is_alive():
            return
        self.start_button.setEnabled(False)
        self.recognition_page_label.setText("正在准备识别记录……")

        def prepare() -> None:
            error = ""
            try:
                report = self.recognition.prepare()
                self.logger.info("recognition prepared retained=%s", report.get("retained", "ready"))
            except Exception as exc:
                self.logger.exception("识别记录准备失败：%s", type(exc).__name__)
                error = f"识别记录准备失败（{type(exc).__name__}），原记录保留，请检查数据目录后重试。"
            self.bridge.recognition_prepared.emit(error)

        self._recognition_thread = threading.Thread(
            target=prepare, name="recognition-prepare", daemon=True
        )
        self._recognition_thread.start()

    def _on_recognition_prepared(self, error: str) -> None:
        if self._shutdown_started:
            return
        self._recognition_ready = not error
        self.recognition_retry.setVisible(bool(error))
        self.start_button.setEnabled(not error and self.service is None)
        if error:
            self.recognition_page_label.setText(error)
            self.statusBar().showMessage(error)
        else:
            self.recognition_page_label.setText("识别记录已就绪")
            self.refresh_recognition(force=True)
        self._sync_header()

    def _recognition_revision(self) -> tuple[int, int]:
        return (self.recognition.records_revision, self._recognition_view_revision)

    def _fetch_recognition_page(self) -> list[dict]:
        page = self._recognition_page
        total = self.recognition.count()
        rows = self.recognition.list_records(RECOGNITION_PAGE_SIZE, page * RECOGNITION_PAGE_SIZE)
        self._recognition_total = total
        return rows

    def _change_recognition_page(self, direction: int) -> None:
        pages = max(1, (self._recognition_total + RECOGNITION_PAGE_SIZE - 1) // RECOGNITION_PAGE_SIZE)
        self._recognition_page = max(0, min(pages - 1, self._recognition_page + direction))
        self._recognition_view_revision += 1
        self.refresh_recognition(force=True)

    def _build_ui(self) -> None:
        self.shell = AppShell(__version__, self)
        self.setCentralWidget(self.shell)
        self.pages = self.shell.pages
        self.tabs = self.pages  # compatibility for inspection; navigation uses PageId.
        self.start_button = QPushButton("开始监控")
        self.start_button.setObjectName("primary")
        self.start_button.setIcon(icon("play"))
        self.start_button.clicked.connect(self.start_monitoring)
        self.stop_button = QPushButton("全部暂停")
        self.stop_button.setIcon(icon("pause"))
        self.stop_button.clicked.connect(self.stop_monitoring)
        self.stop_button.setEnabled(False)
        self.shell.controls.addWidget(self.start_button)
        self.shell.controls.addWidget(self.stop_button)
        self.pages.addWidget(self._build_monitor_page())
        self.pages.addWidget(build_keywords_page(self))
        self.pages.addWidget(build_records_page(self))
        self.pages.addWidget(build_settings_page(self))
        self.records_tab_index = RecordView.NOTIFICATIONS
        self.candidates_tab_index = RecordView.RECOGNITION
        self.records_tabs.currentChanged.connect(self._on_record_view_changed)
        self.shell.page_changed.connect(self._on_tab_changed)
        self.shell.help_requested.connect(self.show_help)
        self.theme_manager.changed.connect(self._on_theme_changed)
        self.theme_manager.save_failed.connect(self._on_theme_save_failed)
        self.shell.set_page(PageId.MONITOR)
        self._sync_header()

    def _build_monitor_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(4)
        title = QLabel("监测任务")
        title.setObjectName("pageTitle")
        title_block.addWidget(title)
        self.region_summary = QLabel()
        self.region_summary.setObjectName("muted")
        self.region_summary.setMinimumWidth(0)
        self.region_summary.setWordWrap(True)
        title_block.addWidget(self.region_summary)
        heading.addLayout(title_block, 1)
        self.add_region_button = QPushButton("新建任务")
        self.add_region_button.setIcon(icon("add"))
        self.add_region_button.setToolTip("添加监控区域；运行期间请先暂停监控")
        self.add_region_button.clicked.connect(self.add_region)
        heading.addWidget(self.add_region_button)
        layout.addLayout(heading)
        health_row = QHBoxLayout()
        self.health_notice = wrapped_label("")
        self.health_notice.setObjectName("warning")
        self.health_notice.hide()
        self.health_retry_button = QPushButton("重试记录")
        self.health_retry_button.clicked.connect(self.health_writer.retry_failed)
        self.health_retry_button.hide()
        health_row.addWidget(self.health_notice, 1)
        health_row.addWidget(self.health_retry_button)
        layout.addLayout(health_row)
        self.configuration_buttons = [self.add_region_button]
        self.region_table = RegionTable()
        self.region_table.action_requested.connect(self._region_row_action)
        self.region_table.itemDoubleClicked.connect(lambda item: self.show_region_detail())
        layout.addWidget(self.region_table, 1)
        self.empty_regions = QLabel("为没有接口的软件添加文字提醒。\n先创建规则组，再选择窗口和关注区域。")
        self.empty_regions.setObjectName("pageSubtitle")
        self.empty_regions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_regions.setWordWrap(True)
        layout.addWidget(self.empty_regions, 1)
        footer = QHBoxLayout()
        self.save_status = QLabel("")
        self.save_status.setObjectName("muted")
        self.save_status.setMinimumWidth(0)
        self.save_status.setWordWrap(True)
        footer.addWidget(self.save_status, 1)
        self.retry_button = QPushButton("重试保存")
        self.retry_button.clicked.connect(self.retry_region_save)
        self.retry_button.setEnabled(False)
        self.retry_button.hide()
        footer.addWidget(self.retry_button)
        layout.addLayout(footer)
        return page

    def _region_row_action(self, action: str, region_id: str) -> None:
        if self._select_region_row(region_id) is None:
            return
        if action == "preview":
            self.preview_region()
            return
        region = self.selected_region()
        if region is None:
            return
        menu = QMenu(self)
        menu.addAction("查看详情", self.show_region_detail)
        pause = menu.addAction("暂停此区域" if region.enabled else "恢复此区域", self.toggle_region)
        calibrate = menu.addAction("重新校准", self.edit_region)
        menu.addSeparator()
        remove = menu.addAction("删除区域", self.delete_region)
        pause.setEnabled(not self._saving and not self._shutdown_started)
        for item in (calibrate, remove):
            item.setEnabled(not self._saving and not self._shutdown_started
                            and self.service is None and self._stopping_service is None)
            item.setToolTip("请先暂停监控" if self.service is not None else "")
        menu.setToolTipsVisible(True)
        try:
            menu.exec(QCursor.pos())
        finally:
            menu.deleteLater()

    def show_region_detail(self) -> None:
        region = self.selected_region()
        if region is None:
            return
        health = self.health_snapshots.get(region.id)
        fields = [("区域", region.alias), ("绑定窗口", region.window_title),
                  ("内容模式", CONTENT_MODES[region.content_mode]),
                  ("规则组", next((g.name for g in self.config.rule_groups if g.id == region.rule_group_id), "兼容关键词")),
                  ("采样间隔", f"{(region.poll_interval_ms or self.config.poll_interval_ms) // 1000} 秒"),
                  ("去重范围", {"task": "当前任务", "group": "指定组：" + region.dedupe_group, "global": "全部任务"}[region.dedupe_scope]),
                  ("窗口跟随", "固定标题" if region.title_policy == "strict" else "此窗口当前内容"),
                  ("采集后端", region.capture_backend),
                  ("物理像素区域", f"({region.rect.x}, {region.rect.y}) · {region.rect.width}×{region.rect.height}"),
                  ("参考画面", f"{region.reference_frame_width}×{region.reference_frame_height}"),
                  ("窗口句柄 / 进程", f"{region.window_hwnd} / {region.process_id}")]
        if health is not None:
            fields.extend([
                ("当前状态", health_status_text(health)),
                ("最近成功识别", datetime.fromtimestamp(health.last_success_at).strftime("%Y-%m-%d %H:%M:%S")
                 if health.last_success_at else "尚无"),
                ("诊断", health_diagnostics_text(health)),
            ])
        dialog = RecordDetailDialog("区域详情", fields, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def show_help(self) -> None:
        fields = [
            ("开始使用", "创建规则组 → 新建任务 → 选择窗口和文字区域 → 预览确认 → 开始监测。"),
            ("采集条件", "电脑保持唤醒和解锁，目标窗口不最小化。窗口可被遮挡；移动不影响选区，大小或采集后端改变后需重新校准。"),
            ("匹配规则", "同一内容单位内支持短语包含、整条相等、字符组合、任一/全部和排除条件。微信预设按气泡判断；通用逐行不跨行拼字。预览不发送通知。"),
            ("恢复与记录", "首次启动与恢复先建立基线，不补发历史。记录中的时间是识别时间；飞书已接受不代表手机已提醒。"),
        ]
        dialog = RecordDetailDialog("使用帮助", fields, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _on_theme_changed(self, _theme: str) -> None:
        self.shell.refresh_icons()
        self.start_button.setIcon(icon("play"))
        self.stop_button.setIcon(icon("pause"))
        self.add_region_button.setIcon(icon("add"))
        self.region_table.viewport().update()
        self._sync_header()

    def _on_theme_save_failed(self, text: str) -> None:
        if not self._shutdown_started:
            self.statusBar().showMessage(text, 6000)

    def _sync_header(self) -> None:
        if not hasattr(self, "shell"):
            return
        summary = self._monitor_state_summary()
        if not self.config.regions and self.service is None:
            summary = "尚未添加区域"
        self.shell.status.setText(summary)
        self.shell.status.setToolTip(summary)
        tone = ("danger" if self.active_faults else "success" if summary.startswith("监控中")
                else "neutral" if self.service is None or summary == "已暂停" else "info")
        if self.shell.status.property("tone") != tone:
            self.shell.status.setProperty("tone", tone)
            self.shell.status.style().unpolish(self.shell.status)
            self.shell.status.style().polish(self.shell.status)
        self.shell.controls.setVisible(bool(self.config.regions) or self.service is not None)
        self.shell.controls.setCurrentWidget(
            self.stop_button if self.service is not None or self._stopping_service is not None else self.start_button
        )
        primary = self.pages.currentIndex() == PageId.MONITOR
        for button in (self.start_button, self.stop_button):
            name = "primary" if primary else ""
            if button.objectName() != name:
                button.setObjectName(name)
                button.style().unpolish(button)
                button.style().polish(button)
        color = self.theme_manager.colors["on_accent" if primary else "text"]
        self.start_button.setIcon(icon("play", color=color))
        self.stop_button.setIcon(icon("pause", color=color))

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(brand_icon(), self)
        self.tray.setToolTip("CueScope｜未启动")
        menu = QMenu()
        open_action = QAction("打开主窗口", self)
        open_action.triggered.connect(self.show_normal)
        start_action = QAction("开始监控", self)
        start_action.triggered.connect(self.start_monitoring)
        stop_action = QAction("全部暂停", self)
        stop_action.triggered.connect(self.stop_monitoring)
        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.exit_app)
        for action in (open_action, start_action, stop_action, exit_action):
            menu.addAction(action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_normal() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        self.tray.show()

    def show_normal(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """首次显示时把默认尺寸收进所在屏幕的可用区域（不动用户显示设置）。"""
        super().showEvent(event)
        if self._initial_geometry_applied:
            return
        self._initial_geometry_applied = True
        available = available_screen_geometry(self)
        if available is None or available.isEmpty():
            return
        width = min(self.width(), max(self.minimumWidth(), int(available.width() * 0.9)))
        height = min(self.height(), max(self.minimumHeight(), int(available.height() * 0.9)))
        self.resize(width, height)
        self.move(
            available.x() + max(0, (available.width() - width) // 2),
            available.y() + max(0, (available.height() - height) // 2),
        )

    def selected_region(self) -> RegionConfig | None:
        row = self.region_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "请选择区域", "请先在表格中选择一个监控区域。")
            return None
        item = self.region_table.item(row, 0)
        if item is None:
            return None
        region_id = item.data(Qt.ItemDataRole.UserRole)
        return next((item for item in self.config.regions if item.id == region_id), None)

    # ------------------------------------------------------------------
    # 区域配置：统一闸门、事务化保存、成功后提交内存
    # ------------------------------------------------------------------

    def _config_action_gate(self, *, allow_auto_stop: bool) -> str:
        """配置变更前的统一闸门；返回空字符串表示放行。

        ``allow_auto_stop=True`` 的入口（重新校准/删除）会先停监控再继续，
        因此只有“正在停止”时才拒绝；``allow_auto_stop=False`` 的入口（添加）
        在监控运行期间直接拒绝，提示用户先全部暂停。
        """
        if self._shutdown_started:
            return "程序正在退出，无法修改区域配置"
        if self._saving or self._settings_busy:
            return SAVING_HINT
        if self._stopping_service is not None:
            return PAUSE_FIRST_HINT
        if self.service is not None and not allow_auto_stop:
            return PAUSE_FIRST_HINT
        return ""

    def _after_stop(self, action: Callable[[], None]) -> None:
        """监控运行时先停监控，完全停止后再执行配置变更。

        后台采集线程与 UI 线程共用同一份 ``config``；在停止未完成时改配置
        会让服务读到半成品（0.2.2 假设 H6）。
        """
        if self.service is not None or self._stopping_service is not None:
            self._pending_after_stop = action
            self.stop_monitoring()
            self.statusBar().showMessage(
                "正在停止采集与 OCR，停止完成后会继续保存区域配置……"
            )
            return
        action()

    def _set_saving(self, saving: bool) -> None:
        """保存期间禁用全部配置按钮，并把开始监控按钮按真实状态复位。"""
        self._saving = saving
        running = self.service is not None or self._stopping_service is not None
        for button in getattr(self, "configuration_buttons", []):
            # 监控运行期间配置按钮本来就该禁用，保存结束不能把它们放开。
            button.setEnabled(not saving and not running)
        # “重试保存”只在有可重试草稿时可用；保存中/监控中都禁用（0.2.2 D1）。
        retry_button = getattr(self, "retry_button", None)
        if retry_button is not None:
            retry_button.setEnabled(not saving and not running and self._retry_ready())
            retry_button.setVisible(self._retry_ready())
        if not hasattr(self, "start_button"):
            return
        self.start_button.setEnabled(
            not saving and not running and not self._shutdown_started
        )

    def _retry_ready(self) -> bool:
        """是否存在“可以直接重发一次”的失败草稿（0.2.2 D1）。"""
        return (
            self._pending_save is not None
            and self._failed_save_stage in RETRYABLE_SAVE_STAGES
        )

    def _clear_failed_save(self) -> None:
        """清掉失败态：草稿已成功写入，或用户改为发起别的变更。"""
        self._failed_save_stage = ""
        self._failed_save_message = ""

    def _request_region_save(
        self, action: str, region: RegionConfig | None, index: int | None = None
    ) -> bool:
        """把一次区域变更交给后台协调器；保存中重复提交直接忽略。"""
        if self._saving or self.save_coordinator.busy:
            QMessageBox.information(
                self, "正在保存", "上一次区域配置还在写入，请稍候再试。"
            )
            self.statusBar().showMessage("正在保存区域配置，请稍候……")
            return False
        self._log_region_change(action, region)
        # 新的变更请求会覆盖上一条失败草稿（用户主动改做别的事 = 放弃旧草稿）。
        self._clear_failed_save()
        self._pending_save = (action, region, index)
        self._set_saving(True)
        started = self.save_coordinator.request_save(self.config, action, region, index)
        if not started:
            self._pending_save = None
            self._set_saving(False)
            QMessageBox.information(
                self, "正在保存", "上一次区域配置还在写入，请稍候再试。"
            )
            return False
        self.statusBar().showMessage(
            f"{ACTION_LABELS.get(action, action)}：正在写入配置文件……"
        )
        self.save_status.setText(f"{ACTION_LABELS.get(action, action)}：正在写入配置文件……")
        return True

    def _log_region_change(self, action: str, region: RegionConfig | None) -> None:
        """脱敏记录一次区域变更：标题只写哈希前缀（0.2.2 决策 Q1）。"""
        if region is None:
            return
        previous = next(
            (item for item in self.config.regions if item.id == region.id), None
        )
        self.logger.info(
            "区域配置变更 action=%s region_id=%s alias=%s title_fp=%s previous_title_fp=%s "
            "rect=(%d,%d,%d,%d) frame=%dx%d backend=%s enabled=%s",
            action,
            region.id,
            region.alias,
            title_fingerprint(region.window_title),
            title_fingerprint(previous.window_title) if previous else "",
            region.rect.x,
            region.rect.y,
            region.rect.width,
            region.rect.height,
            region.reference_frame_width,
            region.reference_frame_height,
            region.capture_backend,
            region.enabled,
        )

    def _on_region_saved(self, result: RegionSaveResult) -> None:
        """写临时文件、回读校验与原子替换都成功：此刻才把变更提交到内存与表格。"""
        if self._shutdown_started:
            return
        action, region, index = self._pending_save or (result.action, None, None)
        self._pending_save = None
        self._clear_failed_save()
        self._set_saving(False)
        if action != "config":
            try:
                apply_region_change(self.config, action, region, index)
            except (ValueError, KeyError) as error:
                # 磁盘已写入但内存定位失败：必须留痕并提示，不能静默留半套状态。
                self.logger.exception("区域配置已写入但内存提交失败：%s", error)
                QMessageBox.critical(
                    self,
                    "请重新打开程序",
                    f"配置已写入文件，但界面提交失败：{error}\n"
                    f"请关闭并重新打开程序让表格与配置文件重新同步。",
                )
                self._load_regions()
                return
        self._load_regions()
        alias = region.alias if region is not None else ""
        if action == "delete":
            self.statusBar().showMessage(f"“{alias}”已删除；历史通知记录保留。", 8000)
            self.save_status.setText(f"“{alias}”已删除；历史通知记录保留。")
            return
        row = self._select_region_row(result.region_id)
        if action == "toggle":
            state = "已启用" if region is not None and region.enabled else "已暂停"
            self.statusBar().showMessage(
                f"“{alias}”{state}；只有检查确认过的区域才会进入自动监控。", 8000
            )
            self.save_status.setText(f"“{alias}”{state}")
            return
        if region is not None and region.previewed:
            self.statusBar().showMessage(
                f"“{alias}”已确认可用于自动监控；采样帧重建基线后才会通知。", 8000
            )
            self.save_status.setText(f"“{alias}”已确认可用于自动监控。")
            return
        self.statusBar().showMessage(f"“{alias}”{SAVED_NOT_CONFIRMED_HINT}。", 10000)
        self.save_status.setText(f"“{alias}”{SAVED_NOT_CONFIRMED_HINT}。")
        if row is None:
            self.region_table.setFocus()

    def _on_region_save_failed(self, result: RegionSaveResult) -> None:
        """写盘或回读校验失败：内存与表格保持旧快照，且**不丢待保存的内容**。

        0.2.2 D1：写盘类失败（占用/磁盘满/回读被改写）换个时机就能成功，所以
        ``_pending_save`` 必须保留成草稿、并让界面上的“重试保存”可用；只有校验
        类失败（别名撞车、区域过小等）重发同一份内容毫无意义，才丢弃草稿并提示
        用户修正。文案只承诺真实存在的能力。
        """
        if self._shutdown_started:
            return
        action, region, index = self._pending_save or (result.action, None, None)
        self.logger.warning(
            "区域配置保存失败 action=%s stage=%s region_alias=%s error=%s",
            action,
            result.error_stage,
            getattr(region, "alias", ""),
            result.error,
        )
        retryable = result.error_stage in RETRYABLE_SAVE_STAGES and region is not None
        self._failed_save_stage = result.error_stage if retryable else ""
        self._failed_save_message = result.message
        # 可重试 → 草稿留下；不可重试 → 丢弃，避免“重试”按钮指向必然失败的输入。
        self._pending_save = (action, region, index) if retryable else None
        self._set_saving(False)
        alias = getattr(region, "alias", "") or "该区域"
        if retryable:
            detail = (
                f"区域配置没有写入，界面与配置文件都保持原样：\n{result.message}\n\n"
                f"待保存的内容（“{alias}”）已保留在程序里，环境恢复后"
                f"{RETRY_PENDING_HINT}。"
            )
        else:
            detail = (
                f"区域配置没有写入，界面与配置文件都保持原样：\n{result.message}\n\n"
                "请按上面的说明修正后重新操作。"
            )
        QMessageBox.critical(self, "保存失败", detail)
        if retryable:
            self.statusBar().showMessage(
                f"保存失败（阶段 {result.error_stage or '未知'}）：配置未写入，"
                f"“{alias}”的修改已保留，可点“重试保存”直接重试。",
                15000,
            )
            self.save_status.setText(
                f"保存失败（阶段 {result.error_stage or '未知'}）：配置未写入，"
                f"“{alias}”的修改已保留，可点“重试保存”。"
            )
            return
        self.statusBar().showMessage(
            f"保存失败（阶段 {result.error_stage or '未知'}）：请按提示修正后重新操作。",
            10000,
        )
        self.save_status.setText(
            f"保存失败（阶段 {result.error_stage or '未知'}）：配置未写入，请按提示修正后重新操作。"
        )

    @config_slot("重试保存区域配置")
    def retry_region_save(self) -> None:
        """把上次没写进配置文件的变更原样再提交一次（0.2.2 D1）。

        与重新走一遍“添加 / 重新校准”的区别：不需要重新采集窗口、不需要重新拖动
        选区——待保存的 ``RegionConfig`` 一直在内存里。
        """
        if self._shutdown_started:
            return
        pending = self._pending_save
        if pending is None or self._failed_save_stage not in RETRYABLE_SAVE_STAGES:
            QMessageBox.information(
                self, "没有待重试的修改", "现在没有保存失败、可重试的区域配置。"
            )
            return
        reason = self._config_action_gate(allow_auto_stop=True)
        if reason:
            QMessageBox.information(self, "暂时不能重试保存", reason)
            return
        action, region, index = pending
        if region is not None and action in {"update", "toggle", "delete"}:
            # 用 id 重新定位：上次失败后配置可能已被外部改动，按下标可能写错行。
            index = next(
                (
                    position
                    for position, item in enumerate(self.config.regions)
                    if item.id == region.id
                ),
                index,
            )
        failed_stage = self._failed_save_stage
        failed_message = self._failed_save_message

        def proceed() -> None:
            if self._request_region_save(action, region, index):
                return
            # 极小的竞态：后台协调器还在写上一次。把草稿放回去，按钮保持可用，
            # 用户稍后再点一次不会丢内容。
            self._pending_save = (action, region, index)
            self._failed_save_stage = failed_stage
            self._failed_save_message = failed_message
            self._set_saving(False)

        self._after_stop(proceed)

    def _select_region_row(self, region_id: str) -> int | None:
        """选中指定区域并滚动到可见位置；返回行号（找不到返回 ``None``）。"""
        row = self.region_rows.get(region_id)
        if row is None:
            return None
        self.region_table.selectRow(row)
        item = self.region_table.item(row, 0)
        if item is not None:
            self.region_table.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter
            )
        return row

    @config_slot("添加区域")
    def add_region(self) -> None:
        reason = self._config_action_gate(allow_auto_stop=False)
        if reason:
            QMessageBox.information(self, "暂时不能添加区域", reason)
            return
        dialog = RegionEditDialog(self)
        # 已知占用的别名交给对话框在保存前拦下：改个名字即可，不必重新圈选。
        dialog.taken_aliases = {item.alias.strip() for item in self.config.regions}
        self.logger.info("区域添加：打开对话框 hwnd_count=%s", len(self.config.regions))
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.logger.info("区域添加：用户取消，未写盘")
                return
            region = dialog.region()
        finally:
            _delete_dialog_later(dialog)
        # 校验由 region_save.validate_region_config 在后台复核；这里只负责提交。
        self.logger.info(
            "区域添加：提交保存 alias=%s hwnd=%s rect=(%d,%d,%d,%d) frame=%dx%d backend=%s",
            region.alias,
            region.window_hwnd,
            region.rect.x,
            region.rect.y,
            region.rect.width,
            region.rect.height,
            region.reference_frame_width,
            region.reference_frame_height,
            region.capture_backend,
        )
        if self._request_region_save("add", region):
            self.save_status.setText(f"正在添加“{region.alias}”……")
        else:
            self.save_status.setText("上一次区域配置还在写入，请稍后再试一次。")

    @config_slot("重新校准区域")
    def edit_region(self) -> None:
        reason = self._config_action_gate(allow_auto_stop=True)
        if reason:
            QMessageBox.information(self, "暂时不能重新校准", reason)
            return

        def proceed() -> None:
            region = self.selected_region()
            if not region:
                return
            index = self.config.regions.index(region)
            dialog = RegionEditDialog(self, region)
            dialog.taken_aliases = {
                item.alias.strip() for item in self.config.regions if item.id != region.id
            }
            self.logger.info("区域重新校准：打开对话框 region_id=%s", region.id)
            try:
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    self.logger.info("区域重新校准：用户取消 region_id=%s", region.id)
                    return
                candidate = dialog.region()
            finally:
                _delete_dialog_later(dialog)
            self._request_region_save("update", candidate, index)

        # 先停监控、后保存：圈选要抓目标窗口整帧，不能与运行中的采集并发。
        self._after_stop(proceed)

    @config_slot("暂停 / 启用区域")
    def toggle_region(self) -> None:
        reason = self._config_action_gate(allow_auto_stop=False)
        if reason:
            QMessageBox.information(self, "暂时不能修改区域状态", reason)
            return
        region = self.selected_region()
        if not region:
            return
        index = self.config.regions.index(region)
        if region.enabled:
            # 传副本而不是直接改内存：写盘失败时 enabled 自然保持原值（即“回滚”）。
            self._request_region_save("toggle", replace(region, enabled=False), index)
            return
        # 启用分支必须先过就绪门禁：不能把未确认/已失效的区域恢复起来。
        problems = self.readiness_problems(region)
        if problems:
            self._load_regions()
            QMessageBox.warning(
                self,
                "区域未就绪",
                f"“{region.alias}”暂时不能启用：\n{readiness_summary(problems)}\n\n"
                "请先选中该区域并点“预览”，在预览窗口里点“确认此区域”后再启用。",
            )
            return
        self._request_region_save("toggle", replace(region, enabled=True), index)

    def readiness_problems(self, region: RegionConfig) -> list[str]:
        """界面侧的就绪判定；服务在运行时复用它的采集器做窗口身份校验。"""
        service = self.service
        windows = getattr(service.capturer, "windows", None) if service else None
        return region_readiness_problems(region, windows)

    @config_slot("删除区域")
    def delete_region(self) -> None:
        reason = self._config_action_gate(allow_auto_stop=True)
        if reason:
            QMessageBox.information(self, "暂时不能删除区域", reason)
            return
        region = self.selected_region()
        if not region:
            return
        if QMessageBox.question(self, "删除区域", f"确定删除“{region.alias}”的采集配置吗？历史通知记录会保留。") != QMessageBox.StandardButton.Yes:
            return
        index = self.config.regions.index(region)
        # 先停监控，写盘成功后才从内存删除（失败时区域仍在，可直接重试）。
        self._after_stop(lambda: self._request_region_save("delete", region, index))

    @config_slot("预览")
    def preview_region(self) -> None:
        if self._shutdown_started:
            return
        if self._saving or self._settings_busy:
            QMessageBox.information(self, "正在保存", "正在保存任务或规则配置，请稍候再检查。")
            return
        region = self.selected_region()
        if not region:
            return
        if region.needs_calibration:
            QMessageBox.warning(self, "需要重新圈选", "旧版坐标不能用于窗口采集，请先重新校准。")
            return
        rules = self._decision_rules()
        index = self.config.regions.index(region)
        was_previewed = region.previewed
        preview_options = {"rules": rules}
        client = getattr(getattr(self.service, "capturer", None), "client", None)
        if getattr(client, "persistent", False) is True:
            preview_options["capture_client"] = client
        dialog = PreviewDialog(
            region, self.config.model, self.base_dir, self.config.keywords,
            self.manual_send, self, **preview_options,
        )
        try:
            dialog.exec()
        finally:
            _delete_dialog_later(dialog)
        if not region.previewed or was_previewed:
            return
        # 预览对话框只改了内存对象。写盘成功之前内存必须保持旧值，
        # 失败时这里已经把 previewed 回滚成 False。
        candidate = replace(region, previewed=True)
        region.previewed = False
        self._request_region_save("update", candidate, index)

    def _delivery_mode(self) -> str:
        return "local" if self.config.notification_mode == "local" or (self.config.notification_mode == "auto" and not self.webhook) else "feishu"

    def _events_revision(self):
        return (self.storage.records_revision, self._event_view_revision)

    def _change_event_filter(self, kind: str) -> None:
        self._record_kind = kind
        self._event_view_revision += 1
        self._records_signature = None
        self.refresh_records(force=True)

    def _decision_rules(self) -> RuleEngine:
        return RuleEngine(
            self.storage,
            self.config.keywords,
            self.config.dedupe_seconds,
            match_mode=self.config.match_mode,
            regions=self.config.regions,
            rule_groups=self.config.rule_groups,
            delivery_mode=self._delivery_mode(),
        )

    def _load_regions(self) -> None:
        selected = self.region_table.currentRow()
        selected_id = (self.region_table.item(selected, 0).data(RegionRole.ID)
                       if selected >= 0 and self.region_table.item(selected, 0) else None)
        self.region_table.setRowCount(len(self.config.regions))
        self.region_rows.clear()
        for row, region in enumerate(self.config.regions):
            self.region_rows[region.id] = row
            if not region.enabled:
                state, kind = "已暂停", "paused"
            elif region.needs_calibration:
                state, kind = "待校准", "pending"
            elif not region.previewed:
                state, kind = "待确认", "pending"
            else:
                state, kind = "已就绪", "idle"
            name = QTableWidgetItem(region.alias)
            name.setData(RegionRole.ID, region.id)
            name.setData(RegionRole.SUBTITLE, f"{CONTENT_MODES[region.content_mode]} · {region.window_title}")
            name.setData(Qt.ItemDataRole.AccessibleDescriptionRole, region.window_title)
            name.setToolTip(region.alias + "\n" + region.window_title)
            self.region_table.setItem(row, RegionColumn.REGION, name)
            status = QTableWidgetItem(state)
            status.setData(RegionRole.STATE, kind)
            if kind == "idle":
                status.setToolTip("已预览确认；开始监控时会重新核对窗口。")
            self.region_table.setItem(row, RegionColumn.STATUS, status)
            self.region_table.setItem(row, RegionColumn.SUCCESS, QTableWidgetItem("—"))
            action = QTableWidgetItem("预览 / 更多")
            action.setToolTip("回车预览；Shift+F10 打开更多操作")
            self.region_table.setItem(row, RegionColumn.ACTIONS, action)
            self.region_table.setRowHeight(row, 76)
            if self.service is not None and region.id in self.health_snapshots:
                self._render_region_health(row, self.health_snapshots[region.id])
        self.empty_regions.setVisible(not self.config.regions)
        self.region_table.setVisible(bool(self.config.regions))
        self.add_region_button.setObjectName("primary" if not self.config.regions else "")
        self.add_region_button.style().unpolish(self.add_region_button)
        self.add_region_button.style().polish(self.add_region_button)
        self._refresh_region_summary()
        self.region_table.layout_columns()
        if selected_id:
            self._select_region_row(selected_id)

    def _refresh_region_summary(self) -> None:
        """区域数量与待办数量放在固定位置，不依赖会被临时消息覆盖的状态栏。"""
        regions = self.config.regions
        pending = sum(1 for item in regions if not item.previewed and item.enabled)
        paused = sum(1 for item in regions if not item.enabled)
        stale = sum(1 for item in regions if item.needs_calibration)
        parts = [f"已配置 {len(regions)}", f"已启用 {len(regions) - paused}"]
        if pending:
            parts.append(f"待确认 {pending}")
        if paused:
            parts.append(f"已暂停 {paused}")
        if stale:
            parts.append(f"需重新圈选 {stale}")
        summary = self.region_summary
        summary.setText("｜".join(parts))
        self._sync_header()

    def save_rule_groups(self, groups) -> bool | None:
        if self.service or self._stopping_service or self._settings_busy or self._saving or self._shutdown_started:
            self.statusBar().showMessage("请先暂停监测，并等待当前保存完成。", 5000)
            return False
        candidate = copy.deepcopy(self.config)
        candidate.rule_groups = copy.deepcopy(groups)
        candidate.__post_init__()
        self._settings_busy = True
        self.rule_workspace.set_busy(True, "正在保存规则组…")
        def save():
            error = ""
            try:
                self.config_store.save(candidate, verify=True)
            except Exception as exc:
                error = f"规则未保存（{type(exc).__name__}），草稿已保留。"
            self.bridge.settings_saved.emit({"kind": "rules", "config": None if error else candidate, "error": error})
        self._settings_thread = threading.Thread(target=save, name="rule-save", daemon=True)
        self._settings_thread.start()

    @config_slot("保存关键词")
    def save_keywords(self) -> None:
        if self._settings_busy or self._saving or self._shutdown_started:
            self.statusBar().showMessage("正在保存设置，请稍候保存关键词。", 5000)
            return
        keywords = [line.strip() for line in self.keywords_edit.toPlainText().splitlines() if line.strip()]
        deduped = list(dict.fromkeys(keywords))
        result = commit_config_change(
            self.config_store,
            self.config,
            lambda candidate: setattr(candidate, "keywords", list(deduped)),
            action="config",
        )
        if not result.ok:
            self.logger.warning("关键词保存失败 stage=%s error=%s", result.error_stage, result.error)
            QMessageBox.critical(self, "保存失败", f"关键词没有保存：{result.message}")
            return
        if self.service:
            self.service.update_keywords(self.config.keywords)
        if hasattr(self.keywords_edit, "setModified"):
            self.keywords_edit.setModified(False)
        self.statusBar().showMessage("关键词已保存", 3500)

    def _load_settings(self) -> None:
        self.notification_mode.setCurrentIndex(max(0, self.notification_mode.findData(self.config.notification_mode)))
        self.webhook_edit.setText(self.webhook)
        self.secret_edit.setText(self.secret)
        self.save_images.setChecked(self.config.save_hit_images)
        self.auto_start.setChecked(self.config.auto_start)
        self.poll_seconds.setValue(self.config.poll_interval_ms // 1000)
        self.record_limit.setValue(self.config.recognition_record_limit)

    @config_slot("保存设置")
    def save_settings(self) -> None:
        if self._settings_busy or self._saving or self._shutdown_started:
            return
        if not self._recognition_ready:
            self.statusBar().showMessage("正在准备识别记录，请稍候保存设置。", 5000)
            return
        webhook = self.webhook_edit.text().strip()
        secret = self.secret_edit.text().strip()
        if webhook:
            validate_webhook_url(webhook)
        candidate = copy.deepcopy(self.config)
        candidate.notification_mode = self.notification_mode.currentData()
        if self.service and (candidate.notification_mode != self.config.notification_mode or (webhook, secret) != self._credential_snapshot):
            self.statusBar().showMessage("更换通知方式前请先暂停监测。", 5000)
            return
        if candidate.notification_mode == "feishu" and not webhook:
            raise ValueError("选择飞书通知时请配置 Webhook")
        candidate.save_hit_images = self.save_images.isChecked()
        candidate.auto_start = self.auto_start.isChecked()
        candidate.poll_interval_ms = self.poll_seconds.value() * 1000
        candidate.recognition_record_limit = self.record_limit.value()
        self._settings_busy = True
        self.settings_save_button.setEnabled(False)
        self.statusBar().showMessage("正在保存设置并整理识别记录……")

        def save() -> None:
            saved = False
            credentials_saved = False
            error = ""
            try:
                self.config_store.save(candidate, verify=True)
                saved = True
                self.credentials.save(webhook, secret)
                credentials_saved = True
                set_windows_autostart(candidate.auto_start)
                self.recognition.set_limit(candidate.recognition_record_limit)
            except Exception as exc:
                self.logger.exception("保存设置未完成：%s", type(exc).__name__)
                error = f"保存设置未完成（{type(exc).__name__}），请检查数据目录和系统权限后重试。"
            self.bridge.settings_saved.emit({
                "config": candidate if saved else None,
                "credentials": (webhook, secret) if credentials_saved else None,
                "error": error,
            })

        self._settings_thread = threading.Thread(target=save, name="settings-save", daemon=True)
        self._settings_thread.start()

    def _on_settings_saved(self, result: dict) -> None:
        self._settings_busy = False
        if self._shutdown_started:
            return
        self.settings_save_button.setEnabled(True)
        if result.get("kind") == "rules":
            self.rule_workspace.save_button.setEnabled(True)
            if result["config"] is not None:
                self.config.rule_groups = result["config"].rule_groups
                self.rule_workspace.set_groups(self.config.rule_groups)
                self.rule_workspace.show_save_result(True, "已保存")
                self.statusBar().showMessage("规则组已保存；下次启动先建立基线。", 5000)
            else:
                self.rule_workspace.show_save_result(False, result["error"])
                self.statusBar().showMessage(result["error"], 8000)
            return
        candidate = result["config"]
        if candidate is not None:
            for key in ("save_hit_images", "auto_start", "poll_interval_ms", "recognition_record_limit", "notification_mode"):
                setattr(self.config, key, getattr(candidate, key))
            if self.service:
                self.service.hit_images.enabled = self.config.save_hit_images
                self.service.update_poll_interval(self.config.poll_interval_ms)
            self._recognition_page = 0
            self._recognition_view_revision += 1
            self.refresh_recognition(force=True)
        if result["credentials"] is not None:
            self.webhook, self.secret = result["credentials"]
            self._credential_snapshot = (self.webhook, self.secret)
        if result["error"]:
            self.statusBar().showMessage(result["error"])
            QMessageBox.warning(self, "设置保存未完成", result["error"])
        else:
            self.statusBar().showMessage(
                f"设置已生效：每 {self.config.poll_interval_ms // 1000} 秒采样，"
                f"识别记录保留最新 {self.config.recognition_record_limit:,} 条。", 8000
            )

    def send_test(self) -> None:
        """仅由此按钮触发真实试发；网络请求在后台线程。"""
        self.test_button.setEnabled(False)
        webhook = self.webhook_edit.text().strip()
        secret = self.secret_edit.text().strip()
        payload = {
            "event_id": f"test-{int(time.time())}",
            "region_alias": "设置页测试",
            "keyword": "测试",
            "observed_at": time.time(),
            "text": "这是一条由用户点击“发送测试消息”触发的测试消息。",
            "boundary_uncertain": False,
        }

        def run() -> None:
            try:
                FeishuClient(webhook, secret).send(payload)
            except Exception as error:
                self.bridge.test_result.emit(f"测试失败：{error}")
            else:
                self.bridge.test_result.emit("飞书已明确接受测试消息；这不代表手机已响铃或已读。")

        threading.Thread(target=run, name="user-requested-feishu-test", daemon=True).start()

    def on_test_result(self, message: str) -> None:
        self.test_button.setEnabled(True)
        QMessageBox.information(self, "测试结果", message)

    def manual_send(
        self,
        region: RegionConfig,
        messages: list[MessageCandidate],
        decisions: dict[str, MessageDecision] | None = None,
    ) -> int:
        if getattr(self, "_settings_busy", False) or getattr(self, "_saving", False) or getattr(self, "_shutdown_started", False):
            return 0
        # Preview decisions are explanatory snapshots, never authority to bypass a
        # rule which has changed since that snapshot was produced.
        del decisions
        rules = self._decision_rules()
        events = rules.process(
            messages, region.alias, now=time.time()
        )
        self.refresh_records()
        return len(events)

    @config_slot("启动监控")
    def start_monitoring(self) -> None:
        if self.service or self._stopping_service or self._shutdown_started:
            return
        if not self._recognition_ready:
            self.statusBar().showMessage("识别记录尚未就绪，请等待准备完成或在识别记录页重试。")
            return
        if self._settings_busy:
            self.statusBar().showMessage("正在保存设置，请稍候再启动监控。")
            return
        if self._saving:
            QMessageBox.information(self, "正在保存", "正在保存区域配置，请稍候再启动监控。")
            return
        enabled = [region for region in self.config.regions if region.enabled]
        if not enabled:
            QMessageBox.warning(self, "无法启动", "请至少添加并启用一个文字监测任务。")
            return
        if not self.config.keywords and not any(g.enabled for g in self.config.rule_groups):
            QMessageBox.warning(self, "无法启动", "请先在规则页保存一个规则组或兼容关键词。")
            return
        if self._delivery_mode() == "feishu" and not self.webhook:
            QMessageBox.warning(self, "无法启动", "请先在设置页保存飞书 Webhook 和签名密钥。")
            return
        groups = {group.id: group for group in self.config.rule_groups if group.enabled}
        invalid_rules = [region.alias for region in enabled if
                         (region.rule_group_id and region.rule_group_id not in groups)
                         or (not region.rule_group_id and not self.config.keywords)]
        if invalid_rules:
            QMessageBox.warning(self, "规则未就绪", "请为以下任务选择有效规则：" + "、".join(invalid_rules))
            return
        stale = [region.alias for region in enabled if region.needs_calibration]
        if stale:
            QMessageBox.warning(
                self,
                "需要重新圈选",
                "以下区域来自旧版桌面坐标，不能猜测转换：" + "、".join(stale),
            )
            return
        unconfirmed = [region.alias for region in enabled if not region.previewed]
        if unconfirmed:
            QMessageBox.warning(
                self,
                "需要先预览",
                "以下区域还没有确认过识别结果，请先选中区域并点“预览”，"
                "在预览窗口里点“确认此区域”后再启动：" + "、".join(unconfirmed),
            )
            return
        blocked = [
            (region.alias, self.readiness_problems(region)) for region in enabled
        ]
        blocked = [(alias, problems) for alias, problems in blocked if problems]
        if blocked:
            detail = "\n".join(
                f"· {alias}：{'；'.join(problems)}" for alias, problems in blocked
            )
            QMessageBox.warning(
                self,
                "区域未就绪",
                f"以下区域还不满足自动监控条件：\n{detail}\n\n请重新圈选或先预览确认。",
            )
            return
        # 启动前快照必须成功写盘：失败就阻止启动，否则界面会显示“监控中”而
        # config.json 里还是上一次的状态。快照放在清空视图状态之前，失败无需回滚。
        snapshot = commit_config_change(
            self.config_store, self.config, _noop, action="config"
        )
        if not snapshot.ok:
            self.logger.warning(
                "启动前配置快照保存失败 stage=%s error=%s", snapshot.error_stage, snapshot.error
            )
            QMessageBox.critical(
                self,
                "保存失败",
                f"启动前无法写入配置快照，为避免把区域状态写丢，本次启动已取消：\n{snapshot.message}",
            )
            return
        # 视图状态必须在构造新会话之前清空：MonitorService.start() 会**同步**为未就绪
        # 区域发出 health（PAUSED + “区域未就绪…”），若在 start() 之后再清空，会把刚
        # 写入的暂停状态抹掉，界面就会显示“监控中”而实际一次采集都没发生。
        self.active_faults.clear()
        self.capture_incidents = IncidentTracker()
        self.health_states.clear()
        self.health_details.clear()
        self.health_snapshots.clear()
        try:
            ocr = LocalOcr(
                self.config.model,
                self.base_dir,
                observer=OcrStageRecorder(self.metrics),
            )
            self.service = MonitorService(
                self.config,
                self.storage,
                ocr,
                capturer=WindowFrameCapturer(capture_roi_mode=self.config.capture_roi_mode),
                data_dir=self.data_dir,
                on_events=self.bridge.events.emit,
                on_health=self.bridge.health.emit,
                recognition_store=self.recognition,
                metrics=self.metrics,
                delivery_mode=self._delivery_mode(),
            )
            self.service.start()
        except Exception as error:
            self.service = None
            QMessageBox.critical(self, "启动失败", str(error))
            return
        self.rule_workspace.set_busy(True, "监测运行中，暂停后可修改规则。")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        for button in self.configuration_buttons:
            button.setEnabled(False)
        self._monitor_status_message = self._monitor_status_text()
        self.statusBar().showMessage(self._monitor_status_message, 3500)
        self.refresh_tray_tooltip()

    def unready_region_aliases(self) -> list[str]:
        """启动后仍处于“区域未就绪”暂停的启用区域别名（按服务上报的 detail 判定）。"""
        aliases: list[str] = []
        for region in self.config.regions:
            if not region.enabled:
                continue
            detail = self.health_details.get(region.id, "")
            if detail.startswith("区域未就绪"):
                aliases.append(region.alias)
        return aliases

    def stop_monitoring(self) -> None:
        if self._stopping_service or self._shutdown_started:
            return
        service = self.service
        self.service = None
        if service:
            service.request_stop()
            self._stopping_service = service
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.statusBar().showMessage("正在停止采集与 OCR；窗口仍可操作……")
            self.refresh_tray_tooltip()

            def stop() -> None:
                service.stop()
                while service.is_alive():
                    service.stop()
                self.bridge.monitor_stopped.emit(service)

            threading.Thread(target=stop, name="stop-monitor", daemon=True).start()
            return
        self._on_monitor_stopped(None)

    def _on_monitor_stopped(self, service) -> None:
        if self._shutdown_started:
            return
        if service is not None and service is not self._stopping_service:
            return
        self._stopping_service = None
        self.rule_workspace.set_busy(False)
        self.start_button.setEnabled(not self._saving)
        self.stop_button.setEnabled(False)
        for button in self.configuration_buttons:
            button.setEnabled(not self._saving)
        self.active_faults.clear()
        self.capture_incidents = IncidentTracker()
        self.health_states.clear()
        self.health_details.clear()
        self.health_snapshots.clear()
        self._monitor_status_message = None
        self._load_regions()
        self.statusBar().showMessage("采集已暂停；持久通知队列仍会继续处理未过期事件。")
        self.save_status.clear()
        self.tray.setToolTip("CueScope｜已暂停")
        # 停止完成后继续此前被推迟的配置变更（重新校准 / 删除区域）。
        pending, self._pending_after_stop = self._pending_after_stop, None
        if pending is not None:
            try:
                pending()
            except Exception:  # noqa: BLE001 - 推迟动作在槽函数里执行，异常必须留痕
                self.logger.exception("停止后继续执行的配置变更失败")

    def on_events(self, events: list[MatchEvent]) -> None:
        if events:
            self.tray.showMessage(
                "已本地记录" if self._delivery_mode() == "local" else "提醒已提交飞书队列",
                "；".join(f"{event.region_alias} / {event.keyword}" for event in events[:3]),
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
        self.refresh_records()

    def on_health(self, health: HealthSnapshot) -> None:
        if not self.service or self._shutdown_started:
            return
        row = self.region_rows.get(health.region_id)
        if row is None:
            return
        current = health.status.value
        previous = self.health_states.get(health.region_id, "")
        region = next((item for item in self.config.regions if item.id == health.region_id), None)
        alias = region.alias if region else health.region_id
        if current == RegionStatus.ERROR.value:
            if health.region_id not in self.active_faults:
                # 进入故障：整轮故障只推一条飞书；后续 ERROR 只刷新详情。
                self.active_faults[health.region_id] = health.detail
                self.queue_system_message(
                    "监控故障", f"{alias}：{health.capture_error_summary or health.detail}",
                    event_id=self.capture_incidents.begin(health.region_id, health.detail) + ".failed"
                )
            else:
                self.active_faults[health.region_id] = health.detail
        elif current == RegionStatus.RUNNING.value:
            if health.region_id in self.active_faults:
                self.active_faults.pop(health.region_id, None)
                self.queue_system_message(
                    "监控恢复", f"{alias} 已恢复并完成新帧基线处理；故障期间历史不补发。",
                    event_id=(self.capture_incidents.recover(health.region_id) or content_digest(health.region_id + str(time.time()))) + ".recovered"
                )
        elif current == RegionStatus.DISABLED.value:
            # 用户停用：结束故障事件，但这不是“恢复成功”，不发恢复通知。
            self.active_faults.pop(health.region_id, None)
            self.capture_incidents.recover(health.region_id)
        elif current == RegionStatus.PAUSED.value and previous != current:
            # 窗口最小化 / 桌面锁定 / 未就绪属于可恢复暂停：只提示托盘，
            # 不结束故障事件，也不写通知队列。
            self.tray.showMessage(
                "监控已暂停",
                f"{alias}：{health.detail}",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        # BASELINING（含故障/暂停后的重新建立基线）既不是新故障也不是恢复：不通知。
        self.health_snapshots[health.region_id] = health
        self.health_states[health.region_id] = current
        self.health_details[health.region_id] = health.detail
        self.refresh_tray_tooltip()
        self._render_region_health(row, health)

    def _render_region_health(self, row: int, health: HealthSnapshot) -> None:
        labels = {RegionStatus.RUNNING: "监控中", RegionStatus.PAUSED: "已暂停",
                  RegionStatus.ERROR: "监控故障", RegionStatus.BASELINING: "建立基线",
                  RegionStatus.DISABLED: "已停用"}
        label = labels[health.status]
        detail = ""
        retry = ""
        if health.status is RegionStatus.ERROR:
            detail = health.capture_error_summary or "查看详情以处理问题"
            if health.next_retry_at:
                retry = "重试 " + datetime.fromtimestamp(health.next_retry_at).strftime("%H:%M:%S")
        elif health.status is RegionStatus.BASELINING:
            label = "准备中" if "预热" in health.detail else "建立基线"
        elif health.status is RegionStatus.PAUSED:
            detail = health.detail.replace("已暂停：", "").replace("已暂停:", "")
        elif health.overloaded:
            detail = f"处理延迟 +{health.schedule_delay_ms / 1000:.1f}s"
        item = QTableWidgetItem("\n".join(x for x in (label, detail, retry) if x))
        item.setData(RegionRole.STATE, health.status.value)
        item.setData(RegionRole.DETAIL, detail)
        item.setData(RegionRole.RETRY, retry)
        item.setToolTip(health_diagnostics_text(health))
        self.region_table.setItem(row, RegionColumn.STATUS, item)
        recent = datetime.fromtimestamp(health.last_success_at).strftime("%H:%M:%S") if health.last_success_at else "—"
        success_item = QTableWidgetItem(recent)
        success_item.setToolTip(health_diagnostics_text(health))
        self.region_table.setItem(row, RegionColumn.SUCCESS, success_item)

    def _monitor_state_summary(self) -> str:
        """Only confirmed RUNNING regions count as sampling; startup is not success."""
        if self._stopping_service is not None:
            return "正在停止"
        enabled = [region for region in self.config.regions if region.enabled]
        if self.service is None or not enabled:
            return "已暂停"
        counts = {"异常": 0, "未就绪": 0, "采样": 0, "建立基线": 0, "准备": 0, "暂停": 0}
        for region in enabled:
            state = self.health_states.get(region.id)
            if state == RegionStatus.RUNNING.value:
                key = "采样"
            elif state == RegionStatus.ERROR.value:
                key = "异常"
            elif state in {RegionStatus.PAUSED.value, RegionStatus.DISABLED.value}:
                key = "未就绪" if "未就绪" in self.health_details.get(region.id, "") else "暂停"
            elif state == RegionStatus.BASELINING.value:
                key = "准备" if "预热" in self.health_details.get(region.id, "") else "建立基线"
            else:
                key = "准备"
            counts[key] += 1
        if counts["采样"] == len(enabled):
            return f"监控中（{len(enabled)} 个区域）"
        if counts["暂停"] == len(enabled):
            return "已暂停"
        if counts["准备"] == len(enabled):
            return f"正在准备（{len(enabled)} 个区域）"
        return "｜".join(f"{label} {count}" for label, count in counts.items() if count)

    def _monitor_status_text(self) -> str:
        summary = self._monitor_state_summary()
        pending = self.unready_region_aliases()
        if pending:
            names = "、".join(pending[:2])
            if len(pending) > 2:
                names += f"等 {len(pending)} 个区域"
            return f"{summary} · {names}不会被采集"
        return summary

    def tray_summary(self) -> str:
        return f"CueScope｜{self._monitor_state_summary()}"

    def refresh_tray_tooltip(self) -> None:
        self.tray.setToolTip(self.tray_summary())
        self._sync_header()
        # Configuration writes own the fixed save label until they complete (or
        # their retained draft is resolved). Health updates must not erase them.
        if (
            self._shutdown_started or not (self.service or self._stopping_service)
            or self._saving or self._pending_save is not None or self._failed_save_stage
            or getattr(self, "_settings_busy", False)
        ):
            return
        # Only replace our own previous monitoring message, never a temporary
        # preview/export/settings message installed by another UI action.
        if (
            self.service is not None and self._monitor_status_message is not None
            and self.statusBar().currentMessage() == self._monitor_status_message
        ):
            self._monitor_status_message = self._monitor_status_text()
            self.statusBar().showMessage(self._monitor_status_message, 3500)

    def queue_system_message(self, keyword: str, text: str, *, event_id: str = "", on_saved=None) -> bool:
        now = time.time()
        event_id = event_id or content_digest(f"{keyword}|{text}|{now}")
        if event_id in self._announced_health_ids:
            return True
        event = MatchEvent(
            id=event_id, region_id="system", region_alias=keyword,
            keyword=keyword, text=text, dedupe_key=content_digest(event_id),
            observed_at=now, delivery_mode=self._delivery_mode(),
        )
        if not self.health_writer.submit(event, on_saved=on_saved):
            self.health_notice.setText("运行事件暂未保存，请检查本地存储并重试。")
            self.health_notice.show()
            return False
        self._announced_health_ids.append(event_id)
        self._announced_health_ids = self._announced_health_ids[-128:]
        self.tray.showMessage(keyword, text, QSystemTrayIcon.MessageIcon.Warning, 3500)
        return True

    def check_notification_health(self) -> None:
        self.notification_health_checked_at = time.time()
        for transition in self.dispatcher.poll_delivery_health_events():
            self.queue_system_message(
                "通知连接异常" if transition.kind == "failed" else "通知连接恢复",
                transition.detail, event_id=transition.event_id,
                on_saved=lambda event: self.dispatcher.ack_delivery_health_event(event.id),
            )
        snapshot = self.dispatcher.delivery_health
        self.network_failed = snapshot.state == "failed"
        text = ""
        if self.health_writer.last_error:
            text = "运行事件写入异常，部分记录等待重试。"
        elif self.network_failed:
            text = "通知通道异常；文字监测仍可继续，详情见活动记录。"
        self.health_notice.setText(text)
        self.health_notice.setVisible(bool(text))
        self.health_retry_button.setVisible(bool(self.health_writer.failed_count))

    def records_visible(self) -> bool:
        """窗口可见性：真的在屏幕上，且没有最小化/收起到托盘。

        最小化到托盘走的是 ``hide()``（``isVisible()`` 为 False），任务栏最小化走
        ``isMinimized()``；两种都算"没人看"。
        """
        return self.isVisible() and not self.isMinimized()

    def table_visible(self, view: RecordView) -> bool:
        return (self.records_visible() and self.pages.currentIndex() == PageId.RECORDS
                and self.records_tabs.currentIndex() == int(view))

    def navigate(self, page: PageId, record_view: RecordView | None = None) -> None:
        if record_view is not None:
            self.records_tabs.setCurrentIndex(int(record_view))
        self.shell.set_page(page)

    def _on_tab_changed(self, index: int) -> None:
        self._sync_header()
        if index == PageId.RECORDS:
            self._on_record_view_changed(self.records_tabs.currentIndex())

    def _on_record_view_changed(self, _index: int) -> None:
        if not hasattr(self, "records_refresh") or not self.records_visible():
            return
        if self.table_visible(RecordView.NOTIFICATIONS):
            self.refresh_records(force=True)
        elif self.table_visible(RecordView.RECOGNITION):
            self.refresh_recognition(force=True)

    def _record_query_failed(self, view: RecordView, error: str) -> None:
        if self._shutdown_started:
            return
        notification = view == RecordView.NOTIFICATIONS
        table = self.record_table if notification else self.candidate_table
        label = self.records_count_label if notification else self.recognition_page_label
        retry = self.records_retry_button if notification else self.recognition_query_retry
        message = "数据库正忙，可重试" if "locked" in error.lower() else "读取失败，可重试"
        label.setText(message)
        retry.show()
        if not table.rowCount():
            table.empty_text = message
            table.viewport().update()

    def _on_records_failed(self, error: str) -> None:
        self._record_query_failed(RecordView.NOTIFICATIONS, error)

    def _on_candidates_failed(self, error: str) -> None:
        self._record_query_failed(RecordView.RECOGNITION, error)

    def refresh_records(self, force: bool = True) -> None:
        """请求刷新通知记录表。查询在后台线程跑，UI 线程不碰数据库。

        ``force=True``（默认，按钮与启动时用）忽略版本号；定时器走
        :meth:`refresh_runtime_views`，那里按版本号与可见性决定。**表不可见时
        一律不发查询**——包括服务事件回调与手动发送后的刷新。
        """
        if not self.table_visible(self.records_tab_index):
            return
        if not self.record_table.rowCount():
            self.record_table.empty_text = "正在读取记录……"
            self.record_table.viewport().update()
        self.records_refresh.request(self._events_revision(), force=force)

    def refresh_runtime_views(self) -> None:
        self.records_refresh.tick(self.table_visible(RecordView.NOTIFICATIONS), self._events_revision())
        self.recognition_refresh.tick(
            self._recognition_ready and self.table_visible(RecordView.RECOGNITION),
            self._recognition_revision(),
        )

    def refresh_recognition(self, force: bool = True) -> None:
        """请求刷新识别记录表；同样后台查询、按版本号收敛，不可见时不查。"""
        if not self._recognition_ready or not self.table_visible(self.candidates_tab_index):
            return
        if not self.candidate_table.rowCount():
            self.candidate_table.empty_text = "正在读取记录……"
            self.candidate_table.viewport().update()
        self.recognition_refresh.request(self._recognition_revision(), force=force)

    @staticmethod
    def _fill_record_table(table, rows, identities):
        selected = table.currentRow()
        selected_id = (table.item(selected, 0).data(Qt.ItemDataRole.UserRole)
                       if selected >= 0 and table.item(selected, 0) else None)
        vertical, horizontal = table.verticalScrollBar().value(), table.horizontalScrollBar().value()
        top_row = table.rowAt(0)
        anchor = table.item(top_row, 0) if top_row >= 0 else None
        anchor_id = str(anchor.data(Qt.ItemDataRole.UserRole)) if anchor is not None else None
        anchor_offset = table.visualItemRect(anchor).top() if anchor is not None else 0
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(rows))
            for row, (identity, values) in enumerate(zip(identities, rows, strict=True)):
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.ItemDataRole.UserRole, str(identity))
                    table.setItem(row, column, item)
                table.setRowHeight(row, 58)
            table.clearSelection()
            table.setCurrentCell(-1, -1)
            if selected_id is not None and str(selected_id) in identities:
                table.setCurrentCell(identities.index(str(selected_id)), 0)
                table.selectRow(identities.index(str(selected_id)))
            if anchor_id is not None and anchor_id in identities:
                table.scrollToItem(table.item(identities.index(anchor_id), 0), QAbstractItemView.ScrollHint.PositionAtTop)
                if table.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel:
                    table.verticalScrollBar().setValue(table.verticalScrollBar().value() - anchor_offset)
            else:
                table.verticalScrollBar().setValue(vertical)
            table.horizontalScrollBar().setValue(horizontal)
        finally:
            table.setUpdatesEnabled(True)
        table.viewport().update()

    def _apply_records(self, records: list[dict]) -> None:
        if not self.table_visible(RecordView.NOTIFICATIONS):
            self._records_signature = None
            return
        self.record_table.empty_text = "还没有通知记录"
        if not self.record_table.rowCount():
            self.record_table.viewport().update()
        self.records_retry_button.hide()
        self.records_count_label.setText(f"最近 {len(records)} 条")
        self._notification_records = {str(r["id"]): r for r in records}
        identities = [str(r["id"]) for r in records]
        rows = [(
            datetime.fromtimestamp(r["observed_at"]).strftime("%m-%d %H:%M:%S"),
            r["region_alias"], r["text"][:180], r["keyword"], delivery_label(r["state"]),
        ) for r in records]
        signature = tuple(zip(identities, rows, strict=True))
        if signature == self._records_signature:
            return
        self._records_signature = signature
        self._fill_record_table(self.record_table, rows, identities)
        self.records_count_label.setText(f"最近 {len(records)} 条")

    def _apply_candidates(self, records: list[dict]) -> None:
        if not self.table_visible(RecordView.RECOGNITION):
            self._candidates_signature = None
            return
        pages = max(1, (self._recognition_total + RECOGNITION_PAGE_SIZE - 1) // RECOGNITION_PAGE_SIZE)
        if self._recognition_page >= pages:
            self._recognition_page = pages - 1
            self._recognition_view_revision += 1
            self.refresh_recognition(force=True)
            return
        self.recognition_previous.setEnabled(self._recognition_page > 0)
        self.recognition_next.setEnabled(self._recognition_page + 1 < pages)
        self.recognition_page_label.setText(f"{self._recognition_page + 1} / {pages} 页 · {self._recognition_total:,} 条")
        self.candidate_table.empty_text = "还没有识别记录"
        if not self.candidate_table.rowCount():
            self.candidate_table.viewport().update()
        self.recognition_query_retry.hide()
        self._recognition_records = {str(r["id"]): r for r in records}
        identities = [str(r["id"]) for r in records]
        rows = [(
            datetime.fromtimestamp(r["observed_at"]).strftime("%m-%d %H:%M:%S"),
            r["region_alias"], r["text"][:180], ", ".join(json.loads(r["matched_keywords"])),
            decision_label(r["decision"]),
        ) for r in records]
        signature = tuple(zip(identities, rows, strict=True))
        if signature == self._candidates_signature:
            return
        self._candidates_signature = signature
        self._fill_record_table(self.candidate_table, rows, identities)

    def show_record_detail(self, kind: str) -> None:
        table = self.record_table if kind == "notifications" else self.candidate_table
        records = self._notification_records if kind == "notifications" else self._recognition_records
        row = table.currentRow()
        if row < 0 or table.item(row, 0) is None:
            return
        record = records.get(str(table.item(row, 0).data(Qt.ItemDataRole.UserRole)))
        if record is None:
            return
        fields = [("识别时间", datetime.fromtimestamp(record["observed_at"]).strftime("%Y-%m-%d %H:%M:%S")),
                  ("来源", record["region_alias"]), ("正文", record["text"])]
        if kind == "notifications":
            fields.extend([
                ("命中词", record["keyword"]), ("本地判断", "应通知"),
                ("投递状态", delivery_label(record["state"])),
                ("尝试次数", str(record.get("attempts") or 0)),
                ("事件编号", str(record["id"])),
            ])
            if record.get("last_error"):
                fields.append(("最近错误", str(record["last_error"])))
        else:
            fields.extend([("命中词", ", ".join(json.loads(record["matched_keywords"])) or "—"),
                           ("处理结果", decision_label(record["decision"]))])
        dialog = RecordDetailDialog("通知详情" if kind == "notifications" else "识别详情", fields, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def export_records(self) -> None:
        if self._export_thread and self._export_thread.is_alive():
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出通知记录", "cuescope-records.csv", "CSV (*.csv)")
        if not path:
            return
        self.export_button.setEnabled(False)

        def run() -> None:
            try:
                records = self.storage.list_events(100000)
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["事件编号", "识别时间", "来源", "命中词", "消息文字", "状态", "尝试次数", "最后错误"])
                    for item in records:
                        writer.writerow([
                            item["id"], item["observed_at"], item["region_alias"], item["keyword"],
                            item["text"], item["state"], item["attempts"], item["last_error"],
                        ])
            except OSError:
                self.bridge.export_finished.emit("导出失败，请检查文件是否被占用及目录权限。")
            else:
                self.bridge.export_finished.emit("记录已导出；文件不包含 Webhook 或签名密钥。")

        self._export_thread = threading.Thread(target=run, name="export-records", daemon=True)
        self._export_thread.start()

    def _on_export_finished(self, message: str) -> None:
        self.export_button.setEnabled(True)
        self.statusBar().showMessage(message, 5000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.quitting:
            event.accept()
        else:
            event.ignore()
            self.hide()
            self.tray.showMessage("仍在后台运行", "关闭主窗口不会停止采集；从托盘选择“退出”才会停止。", QSystemTrayIcon.MessageIcon.Information, 2500)

    def exit_app(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        pending_preview_workers = close_region_dialogs(self)
        self.quitting = True
        self.timer.stop()
        self.health_timer.stop()
        # 表格查询线程必须在 storage.close() 之前收尾：否则线程里可能对着已关闭的
        # 数据库连接查表，也可能在窗口析构之后才发信号。查询本身的耗时是一次
        # 带索引的 SELECT，join 很快；给 2 秒上限只是为了不阻塞退出。
        self.records_refresh.shutdown()
        self.recognition_refresh.shutdown()
        self.centralWidget().setEnabled(False)
        self.statusBar().showMessage("正在安全停止后台任务并保存队列……")
        service = self.service or self._stopping_service
        self.service = None
        if service:
            service.request_stop()
        self.dispatcher.request_stop()

        def shutdown() -> None:
            if service:
                service.stop()
                while service.is_alive():
                    service.stop()
            self.health_writer.close(timeout=1.0)
            while self.health_writer.is_alive():
                self.health_writer.close(timeout=1.0)
            if self.health_writer.pending_count:
                self.logger.error("health records not persisted at exit count=%s", self.health_writer.pending_count)
            self.dispatcher.stop()
            while self.dispatcher.is_alive():
                self.dispatcher.stop()
            if self._export_thread:
                self._export_thread.join()
            if self._recognition_thread:
                self._recognition_thread.join()
            if self._settings_thread:
                self._settings_thread.join()
            wait_ui_workers(pending_preview_workers)
            self.recognition.close()
            self.bridge.shutdown_finished.emit()

        threading.Thread(target=shutdown, name="shutdown-workers", daemon=True).start()

    def _finish_shutdown(self) -> None:
        self.theme_manager.close()
        self._shutdown_done = True
        self.storage.close()
        self.tray.hide()
        close_logging()
        QApplication.quit()


def set_windows_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        if enabled:
            raise RuntimeError("登录自启仅支持 Windows")
        return
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    with key:
        if enabled:
            if getattr(sys, "frozen", False):
                command = f'"{sys.executable}" --minimized'
            else:
                command = f'"{sys.executable}" -m deal_watch --minimized'
            winreg.SetValueEx(key, "DealWatch", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "DealWatch")
            except FileNotFoundError:
                pass
