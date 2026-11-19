"""窗口选择、物理像素区域圈选与后台 OCR 预览。"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, QThread, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..capture import WindowInfo, WindowsWindows
from ..config import ModelConfig
from ..diagnostics import title_fingerprint
from ..models import (
    FrameSample,
    MessageCandidate,
    MessageDecision,
    Rect,
    RegionConfig,
    new_id,
)
from ..ocr import LocalOcr
from ..readiness import MIN_REGION_SIDE
from ..region_save import validate_region_config
from ..rules import RuleEngine
from ..tracking import blocks_to_messages
from ..window_capture import WindowCaptureClient, WindowFrameCapturer
from .task_options import TaskOptions

# 鼠标拖动小于该值（控件像素）视为“点击”而不是圈选，必须给出明确提示，
# 不能像 0.2.1 那样静默把空矩形当成选区（缺陷 D3）。
MIN_DRAG_PIXELS = 4

logger = logging.getLogger("deal_watch.ui.region")

# 0.2.3：对话框关闭后仍在运行的采集线程。QThread 不能在运行中被销毁，
# 但对话框也不该为了等它而卡住界面，所以把线程从对话框生命周期里摘出来挂在这里，
# 由 ``finished`` 自己回收（见 :func:`_detach_capture`）。
_DETACHED_CAPTURES: set = set()
# A cancelled UI preview may still be returning from native OCR. Do not create
# another unbounded model/worker queue while it drains.
_PREVIEW_SLOT = threading.BoundedSemaphore(1)


def _detach_capture(worker) -> None:
    """让在途采集线程脱离对话框：不阻塞关闭，也不因控件析构而崩溃。"""
    try:
        worker.setParent(None)
    except RuntimeError:  # 底层对象已经没了，交给垃圾回收
        return
    _DETACHED_CAPTURES.add(worker)
    finished = getattr(worker, "finished", None)
    if finished is None:
        # 替身/非 QThread 实现：没有 finished 信号，直接放弃引用。
        _DETACHED_CAPTURES.discard(worker)
        return

    def _release() -> None:
        _DETACHED_CAPTURES.discard(worker)
        worker.deleteLater()

    finished.connect(_release)


def array_to_qimage(image: np.ndarray) -> QImage:
    height, width = image.shape[:2]
    return QImage(
        image.data, width, height, int(image.strides[0]), QImage.Format.Format_BGR888
    ).copy()


def screen_for_widget(widget) -> QScreen | None:
    r"""返回控件**实际所在**的屏幕，而不是无条件用 Qt 主屏。

    多显示器下的失败很直观：主窗口被拖到副屏，圈选弹窗却按主屏尺寸缩放并居中，
    于是出现在另一块屏幕上（用户以为程序没响应），或者被主屏的可用区尺寸压扁。
    Qt 的 ``primaryScreen()`` 是“主屏”，并不等于 Windows 的 ``\\.\DISPLAY1``，
    两者不能互相替代。

    顺序：父窗口所在屏幕 → 控件自身所在屏幕（按窗口中心兜底）→ 主屏。
    """
    parent = widget.parentWidget() if widget is not None else None
    screen = parent.screen() if parent is not None else None
    if screen is None and parent is not None:
        screen = QGuiApplication.screenAt(parent.frameGeometry().center())
    if screen is None and widget is not None:
        screen = widget.screen()
    if screen is None and widget is not None:
        screen = QGuiApplication.screenAt(widget.frameGeometry().center())
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        screens = QGuiApplication.screens()
        screen = screens[0] if screens else None
    return screen


def available_screen_geometry(widget=None) -> QRect | None:
    """返回控件所在屏幕的可用区域（不含任务栏）；无屏幕时返回 None。"""
    screen = screen_for_widget(widget) if widget is not None else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        screens = QGuiApplication.screens()
        screen = screens[0] if screens else None
    return screen.availableGeometry() if screen is not None else None


def fit_dialog_to_screen(dialog: QDialog, width: int, height: int) -> None:
    """按**父窗口所在屏幕**可用区域的 90% 限制对话框尺寸并居中（修缺陷 D4）。

    0.2.1 硬编码 1100×760 / 1000×720，在 1366×768 或 150% DPI 下会把确认按钮
    挤到屏幕外，用户看不到“检查当前可见内容”这句关键指引。

    0.2.2 起不再无条件用 Qt 主屏：窗口在副屏时，弹窗必须出现在同一块屏幕上。
    """
    available = available_screen_geometry(dialog)
    if available is None or available.isEmpty():
        dialog.resize(width, height)
        return
    target_width = max(240, min(width, int(available.width() * 0.9)))
    target_height = max(180, min(height, int(available.height() * 0.9)))
    dialog.resize(target_width, target_height)
    dialog.move(
        available.x() + (available.width() - target_width) // 2,
        available.y() + (available.height() - target_height) // 2,
    )


@dataclass(frozen=True)
class SelectionState:
    """一次选区状态快照（帧内物理像素）；供对话框显示坐标、宽高与有效性。"""

    rect: Rect | None = None
    hint: str = ""
    dragging: bool = False

    @property
    def valid(self) -> bool:
        return self.rect is not None

    def describe(self) -> str:
        if self.rect is None:
            return self.hint or "尚未圈选"
        return (
            f"帧内 ({self.rect.x}, {self.rect.y}) "
            f"{self.rect.width}×{self.rect.height}"
        )


class SelectionLabel(QLabel):
    """圈选画布：真值是**采集帧内物理像素**的 :class:`Rect`。

    与 0.2.1 的三点关键差别：

    1. 选区由 :meth:`paintEvent` 自己绘制（边框 + 半透明填充 + 尺寸标签），
       松手后**保留**；只有重新拖动才替换（修 D1：松手即消失）。
    2. 真值 ``_image_rect`` 存帧内坐标，``resizeEvent`` 只重绘并重新投影，
       不改变真值（修 D2：拉伸对话框后同一选区映射出不同 ROI）。
    3. ``mousePressEvent`` 重置选区，松手时小于 ``MIN_DRAG_PIXELS`` 一律取消
       并给出提示（修 D3：按下即松手会静默清空选区）。
    """

    #: 选区变化时发出 :class:`SelectionState`；对话框据此更新坐标/宽高/有效性。
    selection_changed = Signal(object)

    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__()
        self.original_size = pixmap.size()
        self._source_pixmap = pixmap
        # 最小尺寸必须足够小：否则对话框无法缩到小屏可用区内（D4）。
        self.setMinimumSize(240, 180)
        self.setMouseTracking(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_rect: Rect | None = None
        self._drag_origin: QPoint | None = None
        self._drag_current: QPoint | None = None
        self._hint = ""

    # ------------------------------------------------------------------
    # 坐标投影：控件坐标 <-> 帧内物理像素
    # ------------------------------------------------------------------

    def _target(self) -> QRect:
        """当前控件里实际绘制图像的区域（保持宽高比并居中）。"""
        available = self.size()
        scaled = self.original_size.scaled(available, Qt.AspectRatioMode.KeepAspectRatio)
        return QRect(
            (available.width() - scaled.width()) // 2,
            (available.height() - scaled.height()) // 2,
            scaled.width(),
            scaled.height(),
        )

    def _to_frame_rect(self, widget_rect: QRect) -> Rect:
        """控件坐标 → 帧内物理像素；结果被限制在图像范围内。"""
        target = self._target()
        rect = QRect(widget_rect).normalized().intersected(target)
        frame_width = max(1, self.original_size.width())
        frame_height = max(1, self.original_size.height())
        sx = frame_width / max(1, target.width())
        sy = frame_height / max(1, target.height())
        x = round((rect.x() - target.x()) * sx)
        y = round((rect.y() - target.y()) * sy)
        width = max(1, round(rect.width() * sx))
        height = max(1, round(rect.height() * sy))
        x = max(0, min(x, frame_width - 1))
        y = max(0, min(y, frame_height - 1))
        width = max(1, min(width, frame_width - x))
        height = max(1, min(height, frame_height - y))
        return Rect(x, y, width, height)

    def _to_widget_rect(self, rect: Rect | None) -> QRect:
        """帧内物理像素 → 控件坐标；``None`` 返回空矩形。"""
        if rect is None:
            return QRect()
        target = self._target()
        sx = target.width() / max(1, self.original_size.width())
        sy = target.height() / max(1, self.original_size.height())
        return QRect(
            target.x() + round(rect.x * sx),
            target.y() + round(rect.y * sy),
            max(1, round(rect.width * sx)),
            max(1, round(rect.height * sy)),
        )

    def _clamp(self, point: QPoint) -> QPoint:
        target = self._target()
        return QPoint(
            max(target.left(), min(point.x(), target.right())),
            max(target.top(), min(point.y(), target.bottom())),
        )

    @staticmethod
    def _rect_between(origin: QPoint, current: QPoint) -> QRect:
        """按“左上点 + 宽高”重建矩形，保证各方向拖动得到同一块区域。

        不能用 ``QRect(QPoint, QPoint).normalized()``：Qt 对反向矩形做 +1 修正，
        会让“右下→左上”比“左上→右下”少 2 像素——同一块画面给出两个不同 ROI。
        """
        return QRect(
            min(origin.x(), current.x()),
            min(origin.y(), current.y()),
            abs(origin.x() - current.x()),
            abs(origin.y() - current.y()),
        )

    # ------------------------------------------------------------------
    # 选区读写
    # ------------------------------------------------------------------

    def selection_image_rect(self) -> Rect | None:
        """帧内物理像素选区（新代码用这个）。"""
        return self._image_rect

    def image_rect(self) -> QRect:
        """0.2.1 兼容接口：帧内物理像素选区，无选区时返回空 ``QRect``。"""
        rect = self._image_rect
        if rect is None:
            return QRect()
        return QRect(rect.x, rect.y, rect.width, rect.height)

    @property
    def selected(self) -> QRect:
        """0.2.1 兼容属性：**控件坐标**下的选区矩形。"""
        return self._to_widget_rect(self._image_rect)

    @selected.setter
    def selected(self, value: QRect) -> None:
        """兼容旧调用点：把控件坐标选区反算成帧内坐标存入真值。"""
        self.set_widget_selection(value)

    def set_widget_selection(self, widget_rect: QRect) -> None:
        rect = QRect(widget_rect).normalized().intersected(self._target())
        if rect.width() <= 0 or rect.height() <= 0:
            self.clear_selection(notify=True)
            return
        self._image_rect = self._to_frame_rect(rect)
        self._hint = ""
        self.update()
        self._emit_state()

    def clear_selection(self, *, notify: bool = True, hint: str = "") -> None:
        self._image_rect = None
        self._drag_origin = None
        self._drag_current = None
        self._hint = hint
        self.update()
        if notify:
            self._emit_state()

    def hint(self) -> str:
        return self._hint

    def set_hint(self, hint: str) -> None:
        self._hint = hint
        self.update()

    def _emit_state(self, *, live: bool = False) -> None:
        state = SelectionState(
            rect=self._image_rect,
            hint=self._hint,
            dragging=live or self._drag_origin is not None,
        )
        self.selection_changed.emit(state)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _widget_selection(self) -> QRect:
        """当前应绘制的选区（拖动中为实时预览，松手后为已确定选区）。"""
        if self._drag_origin is not None:
            current = self._drag_current or self._drag_origin
            return self._rect_between(self._drag_origin, current).intersected(self._target())
        return self._to_widget_rect(self._image_rect)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        target = self._target()
        if not self._source_pixmap.isNull() and target.width() > 0 and target.height() > 0:
            painter.drawPixmap(target, self._source_pixmap)
        if target.width() <= 0 or target.height() <= 0:
            return
        rect = self._widget_selection()
        if rect.isNull() or rect.isEmpty():
            self._paint_hint(painter, target)
            return
        painter.fillRect(rect, QColor(23, 92, 211, 64))
        pen = QPen(QColor("#175cd3"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        self._paint_size_label(painter, rect)

    def _paint_size_label(self, painter: QPainter, rect: QRect) -> None:
        frame = (
            self._to_frame_rect(rect) if self._drag_origin is not None else self._image_rect
        )
        if frame is None:
            return
        text = f"帧内 ({frame.x},{frame.y}) {frame.width}×{frame.height}"
        box = painter.fontMetrics().boundingRect(text).adjusted(-6, -3, 6, 3)
        top = rect.top() - box.height()
        if top < 0:
            top = rect.top() + 2
        box.moveTopLeft(QPoint(rect.left(), top))
        painter.fillRect(box, QColor(23, 92, 211, 205))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_hint(self, painter: QPainter, target: QRect) -> None:
        if not self._hint:
            return
        box = painter.fontMetrics().boundingRect(self._hint).adjusted(-8, -4, 8, 4)
        box.moveTopLeft(QPoint(target.left() + 8, target.top() + 8))
        painter.fillRect(box, QColor(255, 247, 237, 230))
        painter.setPen(QColor("#9a3412"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._hint)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        # 只重绘：真值是帧内坐标，控件尺寸变化不得改变选区本身（修 D2）。
        self.update()

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = event.position().toPoint()
        if not self._target().contains(point):
            self.clear_selection(
                hint="请在画面范围内拖动；画面以外不生成选区"
            )
            return
        self._drag_origin = self._clamp(point)
        self._drag_current = self._drag_origin
        # 按下即重置：旧选区不会和新拖动混在一起（修 D3 的静默错乱）。
        self._image_rect = None
        self._hint = "按住左键拖动出消息区域，松手后选区保留"
        self.update()
        self._emit_state(live=True)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        if self._drag_origin is not None:
            self._drag_current = self._clamp(event.position().toPoint())
            self.update()
            self._emit_state(live=True)
            return
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        if event.button() != Qt.MouseButton.LeftButton or self._drag_origin is None:
            super().mouseReleaseEvent(event)
            return
        self._drag_current = self._clamp(event.position().toPoint())
        origin = self._drag_origin
        current = self._drag_current
        self._drag_origin = None
        self._drag_current = None
        widget_rect = self._rect_between(origin, current).intersected(self._target())
        if widget_rect.width() < MIN_DRAG_PIXELS or widget_rect.height() < MIN_DRAG_PIXELS:
            # 点击（含抖动）不是圈选：显式取消并说明原因，不再静默。
            self._image_rect = None
            self._hint = f"拖动距离过小（需大于 {MIN_DRAG_PIXELS} 像素），已取消选区"
            self.update()
            self._emit_state()
            return
        self._image_rect = self._to_frame_rect(widget_rect)
        self._hint = ""
        self.update()
        self._emit_state()


class RegionSelectionDialog(QDialog):
    """整帧预览 + 圈选；只负责展示与取值，写入配置由外层负责。"""

    #: 可接受的最小区域边长（帧内物理像素），与 readiness.MIN_REGION_SIDE 一致。
    MIN_ACCEPT_SIDE = MIN_REGION_SIDE

    def __init__(
        self,
        window: WindowInfo,
        image: np.ndarray,
        frame_width: int,
        frame_height: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.setWindowTitle(f"圈选消息显示区域｜{window.title}")
        self.image = array_to_qimage(image)
        layout = QVBoxLayout(self)
        description = QLabel(
            "只圈选聊天消息文字区域：上边界取第一条可见消息顶部（避开标题栏与公告），"
            "下边界取最后一条消息底部（避开输入框与工具栏），左右不要包进头像列。"
            "窗口可以被其它窗口覆盖，但不能最小化；松手后选区会保留在画面上，"
            "确认无误后点“使用此区域”，再用“检查当前可见内容”核对识别结果。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.selector = SelectionLabel(QPixmap.fromImage(self.image))
        self.selector.selection_changed.connect(self._on_selection_changed)
        layout.addWidget(self.selector, 1)
        self.coordinates = QLabel()
        self.coordinates.setWordWrap(True)
        layout.addWidget(self.coordinates)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("使用此区域")
        self.ok_button.setObjectName("primary")
        self.ok_button.setEnabled(False)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons
        # 尺寸适配放在布局建好之后：否则控件最小尺寸会把对话框反向撑出可用区。
        fit_dialog_to_screen(self, 1100, 760)
        self._on_selection_changed(SelectionState())

    # ------------------------------------------------------------------
    # 选区状态显示与接受条件
    # ------------------------------------------------------------------

    def _on_selection_changed(self, state: SelectionState) -> None:
        rect = state.rect
        if rect is None:
            self.coordinates.setText(self._missing_reason(state.hint))
            self.ok_button.setEnabled(False)
            return
        problems = self.rect_problems(rect)
        prefix = state.describe()
        if problems:
            self.coordinates.setText(f"{prefix}；" + "；".join(problems) + "，请重新圈选")
            self.ok_button.setEnabled(False)
            return
        self.coordinates.setText(f"{prefix}；坐标与尺寸有效，点“使用此区域”确认")
        self.ok_button.setEnabled(True)

    def _missing_reason(self, hint: str) -> str:
        """未选择 / 过小 / 超出帧三类原因必须能区分。"""
        if hint:
            return f"{hint}；当前没有有效选区。"
        return "尚未选择区域：按住鼠标左键在画面上拖动出消息区域，松手后选区会保留。"

    def rect_problems(self, rect: Rect) -> list[str]:
        problems: list[str] = []
        if rect.width < self.MIN_ACCEPT_SIDE or rect.height < self.MIN_ACCEPT_SIDE:
            problems.append(
                f"区域过小（至少 {self.MIN_ACCEPT_SIDE}×{self.MIN_ACCEPT_SIDE} 像素）"
            )
        if self.frame_width > 0 and self.frame_height > 0 and (
            rect.x + rect.width > self.frame_width
            or rect.y + rect.height > self.frame_height
        ):
            problems.append("选区超出采集帧")
        return problems

    def accept(self) -> None:
        rect = self.selector.selection_image_rect()
        if rect is None:
            QMessageBox.warning(
                self, "尚未选择区域", self._missing_reason(self.selector.hint())
            )
            return
        problems = self.rect_problems(rect)
        if problems:
            QMessageBox.warning(
                self,
                "区域不可用",
                "；".join(problems) + "。请重新圈选完整的消息显示区域。",
            )
            return
        super().accept()

    def frame_rect(self) -> Rect:
        rect = self.selector.selection_image_rect()
        if rect is None:
            raise RuntimeError("尚未圈选消息显示区域，不能生成采集配置")
        return rect


class SelectionCaptureWorker(QThread):
    completed = Signal(object, object, int, int, str)

    def __init__(self, hwnd: int, backend: str, parent=None) -> None:
        super().__init__(parent)
        self.hwnd = hwnd
        self.backend = backend
        self.client = None

    def run(self) -> None:
        try:
            current = WindowsWindows().window_from_hwnd(self.hwnd)
            if current is None:
                raise RuntimeError("所选窗口已关闭，请重新选择。")
            if current.minimized:
                raise RuntimeError("窗口已最小化，请先恢复。")
            client = self.client or WindowCaptureClient()
            frame = client.capture(self.hwnd, self.backend)
            self.completed.emit(current, frame.image, frame.width, frame.height, "")
        except Exception as error:
            self.completed.emit(None, None, 0, 0, f"{type(error).__name__}: {error}")


def preview_unit_lines(messages, rules):
    lines = []
    for number, message in enumerate(messages, 1):
        reason = "未选择规则"
        if rules:
            result = rules.explain(message)
            if result.excluded_by:
                reason = "排除条件 · " + "、".join(result.excluded_by)
            elif result.should_notify:
                reason = "命中 · " + "、".join(result.matched_labels)
            elif getattr(result, "error", ""):
                reason = result.error
            elif message.boundary_uncertain:
                reason = "文字边界不确定"
            else:
                reason = "未满足所选规则"
        lines.append(f"{number}. {message.text}\n    {reason}")
    return "\n".join(lines)


class PreviewImageLabel(QLabel):
    """Read-only image canvas whose pixels never become a minimum-width hint."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preview_pixmap = QPixmap()
        self.setMinimumSize(240, 160)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

    def set_preview_image(self, image: QImage | None) -> None:
        self._preview_pixmap = QPixmap.fromImage(image) if image is not None else QPixmap()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if self._preview_pixmap.isNull():
            return
        size = self._preview_pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                       size.width(), size.height())
        painter.drawPixmap(target, self._preview_pixmap)


class RegionEditDialog(QDialog):
    """One modal lifecycle: select a window, draw ROI, then explicitly confirm OCR."""

    def __init__(self, parent=None, existing: RegionConfig | None = None, *,
                 model: ModelConfig | None = None, base_dir: Path | None = None,
                 rules: RuleEngine | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑监测任务" if existing else "新建监测任务")
        self.setMinimumSize(600, 460)
        self.existing = existing
        self._draft_id = existing.id if existing else new_id()
        config = getattr(parent, "config", None)
        self.model = model or getattr(config, "model", None) or ModelConfig()
        self.base_dir = base_dir or getattr(parent, "base_dir", None) or Path(__file__).resolve().parents[3]
        decision_factory = getattr(parent, "_decision_rules", None)
        self.rules = rules or (decision_factory() if callable(decision_factory) else None)
        self.windows = WindowsWindows().list_windows()
        self.selected_window: WindowInfo | None = None
        self.selected_rect: Rect | None = None
        self.selected_frame_size: tuple[int, int] | None = None
        self._selection_generation = 0
        self._preview_generation = 0
        self._selection: tuple[int, str, int, int] | None = None
        self._selection_worker = None
        self._selection_worker_generation = None
        self._preview_worker = None
        self._frame_image = None
        self._capture_identity = None
        self._captured_identity = None
        self._captured_outer_size = None
        self._preview_verified = False
        self._preview_evidence = None
        self._save_confirmed = False
        self._closed = False
        self.taken_aliases: set[str] = set()
        self.selector: SelectionLabel | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        header = QHBoxLayout()
        self.step_labels = []
        for text in ("1 选择窗口", "2 圈选区域", "3 预览确认"):
            label = QLabel(text)
            header.addWidget(label, 1)
            self.step_labels.append(label)
        layout.addLayout(header)
        self.capture_status = self._label("选择目标窗口，配置它的文字处理方式。")
        self.capture_status.setObjectName("muted")
        layout.addWidget(self.capture_status)
        self.steps = QStackedWidget()
        self.steps.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.steps, 1)

        choose_page = QWidget()
        choose_layout = QVBoxLayout(choose_page)
        choose_layout.setContentsMargins(0, 0, 0, 0)
        choose_layout.setSpacing(16)
        form = QFormLayout()
        form.setSpacing(12)
        self.alias = QLineEdit(existing.alias if existing else "")
        self.alias.setPlaceholderText("为这个区域起个名字")
        self.window_combo = QComboBox()
        self.window_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.window_combo.setMinimumContentsLength(20)
        self.window_combo.setMinimumWidth(0)
        self._populate_window_combo()
        form.addRow("区域名称", self.alias)
        window_row = QHBoxLayout()
        window_row.addWidget(self.window_combo, 1)
        self.refresh_windows_button = QPushButton("刷新")
        self.refresh_windows_button.setAccessibleName("刷新可见窗口列表")
        self.refresh_windows_button.setAutoDefault(False)
        self.refresh_windows_button.clicked.connect(self.refresh_windows)
        window_row.addWidget(self.refresh_windows_button)
        form.addRow("目标窗口", window_row)
        choose_layout.addLayout(form)
        self.task_options = TaskOptions(config, existing, self)
        self._options_title_policy = self.task_options.title_policy.currentData()
        choose_layout.addWidget(self.task_options)
        self.advanced_button = QPushButton("高级选项")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setAccessibleName("展开采集后端选项")
        choose_layout.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced_panel = QWidget()
        advanced_form = QFormLayout(self.advanced_panel)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("WGC（推荐）", "wgc")
        self.backend_combo.addItem("PrintWindow（兼容）", "print-window")
        advanced_form.addRow("采集后端", self.backend_combo)
        self.advanced_panel.hide()
        self.advanced_button.toggled.connect(self.advanced_panel.setVisible)
        choose_layout.addWidget(self.advanced_panel)
        choose_layout.addStretch(1)
        self.choose_button = QPushButton("截取窗口")
        self.choose_button.setObjectName("primary")
        self.choose_button.clicked.connect(self.choose_region)
        from PySide6.QtWidgets import QScrollArea
        choose_scroll = QScrollArea()
        choose_scroll.setWidgetResizable(True)
        choose_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        choose_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        choose_scroll.setWidget(choose_page)
        self.steps.addWidget(choose_scroll)

        crop_page = QWidget()
        crop_layout = QVBoxLayout(crop_page)
        crop_layout.setContentsMargins(0, 0, 0, 0)
        crop_layout.setSpacing(12)
        crop_layout.addWidget(self._label("圈选需要判断的文字，避开无关区域；内容单位将在预览中标出。"))
        self.crop_layout = QVBoxLayout()
        self.crop_placeholder = self._label("正在准备画面……")
        self.crop_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_layout.addWidget(self.crop_placeholder, 1)
        crop_layout.addLayout(self.crop_layout, 1)
        self.selection_label = self._label("尚未圈选")
        self.selection_label.setObjectName("muted")
        crop_layout.addWidget(self.selection_label)
        self.recapture_button = QPushButton("重新截取")
        self.recapture_button.clicked.connect(self.choose_region)
        crop_layout.addWidget(self.recapture_button, 0, Qt.AlignmentFlag.AlignRight)
        self.steps.addWidget(crop_page)

        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(12)
        self.preview_image = PreviewImageLabel()
        preview_layout.addWidget(self.preview_image, 1)
        self.preview_result = self._label("确认圈选内容可读后保存。")
        self.preview_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.preview_result)
        detail_scroll.setAccessibleName("逐条识别文字和判断理由")
        detail_scroll.setMinimumHeight(72)
        detail_scroll.setMaximumHeight(150)
        preview_layout.addWidget(detail_scroll)
        self.steps.addWidget(preview_page)

        footer = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.back_button.clicked.connect(self.go_back)
        footer.addWidget(self.back_button)
        footer.addStretch(1)
        self.pending_button = QPushButton("保存为待确认")
        self.pending_button.setToolTip("保存当前圈选，稍后预览确认；未确认区域不能开始监控。")
        self.pending_button.clicked.connect(self.accept_pending)
        footer.addWidget(self.pending_button)
        footer.addWidget(self.choose_button)
        self.preview_button = QPushButton("预览识别")
        self.preview_button.setObjectName("primary")
        self.preview_button.clicked.connect(self.start_preview)
        footer.addWidget(self.preview_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("确认并保存")
        self.ok_button.setObjectName("primary")
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        layout.addLayout(footer)
        for button in (self.back_button, self.pending_button, self.cancel_button, self.recapture_button):
            button.setAutoDefault(False)
        if existing:
            self.backend_combo.setCurrentIndex(1 if existing.capture_backend == "print-window" else 0)
            for index, window in enumerate(self.windows):
                if window.hwnd == existing.window_hwnd:
                    self.window_combo.setCurrentIndex(index)
                    break
        self.window_combo.currentIndexChanged.connect(self._invalidate_selection)
        self.backend_combo.currentIndexChanged.connect(self._invalidate_selection)
        self.task_options.changed.connect(self._task_options_changed)
        self._show_step(0)
        fit_dialog_to_screen(self, 900, 680)

    def _task_options_changed(self):
        policy = self.task_options.title_policy.currentData()
        if policy != self._options_title_policy:
            self._options_title_policy = policy
            self._invalidate_selection()
        self._cancel_preview()
        if self.steps.currentIndex() == 2:
            self._show_step(1)
        self.capture_status.setText("任务条件已改变，请重新预览确认。")
        self._update_buttons()

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def _identity(self, window):
        from ..window_identity import canonical_process_path
        title = getattr(window, "title", None) if self.task_options.title_policy.currentData() == "strict" else ""
        return (getattr(window, "hwnd", None), getattr(window, "process_id", None),
                canonical_process_path(getattr(window, "process_path", "")),
                getattr(window, "class_name", None), title,
                getattr(window, "process_start_id", None), getattr(window, "dpi", None))

    @staticmethod
    def _outer_size(window):
        rect = getattr(window, "rect", None)
        return (getattr(rect, "width", None), getattr(rect, "height", None))

    def _show_step(self, step):
        self.steps.setCurrentIndex(step)
        for index, label in enumerate(self.step_labels):
            label.setObjectName("sectionTitle" if index == step else "muted")
            label.style().unpolish(label)
            label.style().polish(label)
        self._update_buttons()

    def _preview_running(self):
        return self._preview_worker is not None and self._preview_worker.isRunning()

    def _update_buttons(self):
        step = self.steps.currentIndex()
        selected = self._selection is not None and self.selected_rect is not None
        capturing = self._capture_running()
        previewing = self._preview_running()
        self.back_button.setVisible(step > 0)
        self.pending_button.setVisible(step > 0)
        self.pending_button.setEnabled(selected and not capturing and not previewing)
        self.preview_button.setVisible(step == 1 or (step == 2 and not self._preview_verified))
        self.preview_button.setEnabled(selected and not capturing and not previewing)
        self.preview_button.setText("正在识别……" if previewing else "预览识别" if step == 1 else "重试识别")
        self.ok_button.setVisible(step == 2 and self._preview_verified)
        self.ok_button.setEnabled(self._preview_verified and selected and not capturing)
        self.choose_button.setVisible(step == 0)
        self.choose_button.setEnabled(not capturing)
        self.choose_button.setText("正在截取……" if capturing else "截取窗口")
        self.recapture_button.setEnabled(not capturing)
        self.choose_button.setDefault(step == 0)
        self.preview_button.setDefault(step == 1 or (step == 2 and not self._preview_verified))
        self.ok_button.setDefault(step == 2 and self._preview_verified)

    def _cancel_preview(self):
        self._preview_generation += 1
        self._preview_verified = False
        self._preview_evidence = None
        self._save_confirmed = False
        worker, self._preview_worker = self._preview_worker, None
        if worker is not None and worker.isRunning():
            _detach_capture(worker)
        self.preview_image.set_preview_image(None)
        self.preview_result.setText("确认圈选内容可读后保存。")

    def _clear_canvas(self):
        self._frame_image = None
        if self.selector is not None:
            self.crop_layout.removeWidget(self.selector)
            self.selector.deleteLater()
            self.selector = None
        self.crop_placeholder.show()

    def _invalidate_selection(self, *_args):
        self._selection_generation += 1
        self._selection = None
        self.selected_window = None
        self.selected_rect = None
        self.selected_frame_size = None
        self._capture_identity = None
        self._captured_identity = None
        self._captured_outer_size = None
        self._cancel_preview()
        self._clear_canvas()
        self.selection_label.setText("尚未圈选，请重新圈选消息区域")
        self.capture_status.setText("选择窗口后重新截取。")
        self._show_step(0)

    def _populate_window_combo(self):
        titles = Counter(window.title for window in self.windows)
        for window in self.windows:
            title = window.title
            if titles[title] > 1:
                process = PureWindowsPath(window.process_path).name or "进程未知"
                title = f"{title} · {process} · 窗口 {window.hwnd:X}"
            self.window_combo.addItem(title, window)

    def refresh_windows(self):
        previous = getattr(self.window_combo.currentData(), "hwnd", None)
        self.windows = WindowsWindows().list_windows()
        self.window_combo.blockSignals(True)
        self.window_combo.clear()
        self._populate_window_combo()
        for index, window in enumerate(self.windows):
            if window.hwnd == previous:
                self.window_combo.setCurrentIndex(index)
                break
        self.window_combo.blockSignals(False)
        self._invalidate_selection()

    def _capture_running(self):
        return self._selection_worker is not None and self._selection_worker.isRunning()

    def _set_capturing(self, capturing):
        if capturing:
            self.ok_button.setEnabled(False)
            self.pending_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.choose_button.setEnabled(False)
            self.recapture_button.setEnabled(False)
            self.choose_button.setText("正在截取……")
        else:
            self._update_buttons()

    def choose_region(self):
        window = self.window_combo.currentData()
        backend = str(self.backend_combo.currentData())
        if window is None or window.minimized:
            self.capture_status.setText("请先打开或恢复所选窗口。")
            return
        if self._capture_running():
            return
        if not self.alias.text().strip():
            self.alias.setText(str(window.title).strip())
        if self.alias.text().strip() in self.taken_aliases:
            self.capture_status.setText("区域名称已存在，请换一个名称。")
            self.alias.setFocus()
            self.alias.selectAll()
            return
        self._invalidate_selection()
        generation = self._selection_generation
        hwnd = int(window.hwnd)
        self._capture_identity = self._identity(window)
        self._show_step(1)
        self.capture_status.setText("正在截取窗口……")
        self.crop_placeholder.setText("正在准备画面……")
        logger.info("区域截取开始 generation=%s hwnd=%s backend=%s title_fp=%s",
                    generation, hwnd, backend, title_fingerprint(window.title))
        worker = SelectionCaptureWorker(hwnd, backend, self)
        worker.client = getattr(self, "capture_client", None)
        self._selection_worker = worker
        self._selection_worker_generation = generation
        worker.completed.connect(lambda current, image, width, height, error:
            self._on_selection_completed(generation, hwnd, backend, current, image, width, height, error))
        finished = getattr(worker, "finished", None)
        if finished is not None:
            finished.connect(lambda: self._capture_finished(worker))
        self._set_capturing(True)
        worker.start()

    def _capture_finished(self, worker):
        if self._selection_worker is worker:
            self._selection_worker = None
            if not self._closed:
                self._update_buttons()
        worker.deleteLater()

    def _on_selection_completed(self, generation, hwnd, backend, current, image,
                                width, height, error, visible=()):
        del visible
        if self._closed or generation != self._selection_generation:
            logger.info("区域截取结果作废 generation=%s current=%s", generation, self._selection_generation)
            if not self._closed and self._selection_worker_generation == generation:
                self._selection_worker = None
                self._update_buttons()
            return
        if error:
            self.capture_status.setText(f"截取失败：{error}；可重试。")
            self.crop_placeholder.setText("未取得画面")
            self._update_buttons()
            return
        fresh = WindowsWindows().window_from_hwnd(hwnd)
        if (current is None or int(getattr(current, "hwnd", 0)) != hwnd
                or backend != str(self.backend_combo.currentData())
                or getattr(self.window_combo.currentData(), "hwnd", None) != hwnd
                or fresh is None or self._identity(fresh) != self._identity(current)
                or self._outer_size(fresh) != self._outer_size(current)
                or (self._capture_identity is not None and self._capture_identity != self._identity(current))):
            self.capture_status.setText("窗口或后端已变化，请重新截取。")
            self.selection_label.setText("圈选已失效，请重新圈选")
            self._update_buttons()
            return
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                or image.ndim != 3 or image.shape != (height, width, 3)):
            self.capture_status.setText("画面格式无效，请重新截取。")
            self._update_buttons()
            return
        self._clear_canvas()
        self._frame_image = image
        self.selected_window = current
        self.selected_frame_size = (int(width), int(height))
        self._selection = (hwnd, backend, int(width), int(height))
        self._captured_identity = self._identity(current)
        self._captured_outer_size = self._outer_size(current)
        self.selector = SelectionLabel(QPixmap.fromImage(array_to_qimage(image)))
        self.selector.selection_changed.connect(self._on_inline_selection)
        self.crop_placeholder.hide()
        self.crop_layout.addWidget(self.selector, 1)
        self.capture_status.setText("画面已就绪，请圈选消息文字。")
        self._show_step(1)

    def _on_inline_selection(self, state):
        if self._frame_image is None or self._closed:
            return
        self._cancel_preview()
        rect = state.rect
        valid = (rect is not None and not state.dragging
                 and rect.width >= MIN_REGION_SIDE and rect.height >= MIN_REGION_SIDE)
        self.selected_rect = rect if valid else None
        self.selection_label.setText(
            f"已选 {rect.width} × {rect.height} 像素" if valid else
            f"请圈选至少 {MIN_REGION_SIDE} × {MIN_REGION_SIDE} 像素的消息区域"
        )
        self._update_buttons()

    def go_back(self):
        if self.steps.currentIndex() == 2:
            self._cancel_preview()
            self.capture_status.setText("调整圈选后重新预览。")
            self._show_step(1)
        else:
            self._invalidate_selection()

    def start_preview(self):
        if self._preview_running():
            return
        problems = self.selection_problems()
        if problems or self._frame_image is None:
            self.capture_status.setText("；".join(problems) or "请重新截取窗口。")
            return
        self._cancel_preview()
        self._show_step(2)
        generation = self._preview_generation
        selection_generation = self._selection_generation
        evidence = (self._selection, self.selected_rect)
        draft = self._build_region()
        if self.rules is not None:
            groups = getattr(getattr(self.parent(), "config", None), "rule_groups", [])
            quick_rule = self.task_options.draft_rule()
            self.rules.configure_regions([draft], [*groups, quick_rule] if quick_rule else groups)
        rect = self.selected_rect
        image = self._frame_image[rect.y:rect.y + rect.height, rect.x:rect.x + rect.width].copy()
        frame = FrameSample(draft.id, 1, time.time(), image, rect)
        worker = PreviewWorker(draft, self.model, self.base_dir, frame)
        self._preview_worker = worker
        worker.completed.connect(lambda image, observation, error:
            self._on_preview_completed(generation, selection_generation, evidence, image, observation, error))
        finished = getattr(worker, "finished", None)
        if finished is not None:
            finished.connect(lambda: self._preview_finished(worker))
        self.capture_status.setText("正在识别圈选内容……")
        self.preview_result.setText("正在准备识别结果")
        worker.start()
        self._update_buttons()

    def _preview_finished(self, worker):
        if self._preview_worker is worker:
            self._preview_worker = None
            if not self._closed:
                self._update_buttons()
        worker.deleteLater()

    def _on_preview_completed(self, generation, selection_generation, evidence, image, observation, error):
        if (self._closed or generation != self._preview_generation
                or selection_generation != self._selection_generation
                or evidence != (self._selection, self.selected_rect)):
            return
        self._preview_verified = False
        if error:
            self.capture_status.setText(f"识别失败：{error}；可重试或保存为待确认。")
        elif observation is None or not observation.blocks:
            self.capture_status.setText("未识别到文字，请调整圈选或保存为待确认。")
            self.preview_result.setText("当前画面没有可确认的文字。")
        else:
            problems = self.selection_problems()
            if problems:
                self.capture_status.setText("；".join(problems))
            else:
                messages = blocks_to_messages(observation)
                hits = [m for m in messages if self.rules and self.rules.decide(m).matched_keywords]
                self._preview_verified = True
                self._preview_evidence = (generation, selection_generation, evidence)
                self.capture_status.setText("选区可以保存。当前没有提醒条件；请返回第一步填写短语，或稍后配置规则。"
                                           if self.task_options.reminder_missing() else "请确认文字区域清晰、完整。")
                self.preview_result.setText(f"{len(messages)} 个内容单位 · 命中 {len(hits)} 条 · 预览不发送提醒\n"
                                            + preview_unit_lines(messages, self.rules))
        if image is not None and observation is not None:
            image = image.copy()
            painter = QPainter(image)
            painter.setPen(QPen(QColor("#087C69"), 2))
            for block in observation.blocks:
                box = block.box
                painter.drawRect(box.x, box.y, box.width, box.height)
            for number, message in enumerate(blocks_to_messages(observation), 1):
                painter.drawText(message.box.x, max(12, message.box.y), str(number))
            painter.setPen(QPen(QColor("#C74E39"), 2, Qt.PenStyle.DashLine))
            for box in observation.uncovered_boxes:
                painter.drawRect(box.x, box.y, box.width, box.height)
            painter.end()
        self.preview_image.set_preview_image(image)
        self._update_buttons()

    def accept_pending(self):
        if self._capture_running() or self._preview_running():
            return
        self._save_confirmed = False
        self._finish_save()

    def done(self, result):
        self._closed = True
        self._selection_generation += 1
        self._preview_generation += 1
        for worker in (self._selection_worker, self._preview_worker):
            if worker is not None and worker.isRunning():
                _detach_capture(worker)
        self._selection_worker = self._preview_worker = None
        self._clear_canvas()
        self.preview_image.set_preview_image(None)
        if result != QDialog.DialogCode.Accepted:
            self._save_confirmed = False
        super().done(result)

    def selection_problems(self) -> list[str]:
        """保存前的复核；返回问题清单，空列表表示可以保存。"""
        problems: list[str] = []
        try:
            self.task_options.values()
        except ValueError as error:
            return [str(error)]
        if not self.alias.text().strip():
            problems.append("请填写来源别名")
        if self._selection is None or self.selected_window is None or self.selected_rect is None:
            problems.append("请先圈选消息显示区域")
            return problems
        hwnd, backend, width, height = self._selection
        if int(getattr(self.selected_window, "hwnd", 0)) != hwnd:
            problems.append("圈选窗口与当前所选窗口不一致，请重新圈选")
        if backend != str(self.backend_combo.currentData()):
            problems.append("圈选时的采集后端与当前选择不一致，请重新圈选")
        if self.selected_frame_size != (int(width), int(height)):
            # 参考帧尺寸必须与圈选那一刻一致；切换窗口/后端后旧尺寸已清空。
            problems.append("参考帧尺寸与圈选时不一致，请重新圈选")
        current = WindowsWindows().window_from_hwnd(hwnd)
        if current is None:
            problems.append("所选窗口已关闭，请重新选择窗口并圈选")
            return problems
        if current.minimized:
            problems.append("窗口已最小化，请恢复后重新圈选")
        if not getattr(current, "process_id", 0) or not getattr(current, "process_path", ""):
            problems.append("无法确认目标进程身份，请检查窗口权限后重试")
        if self._window_identity_changed(current):
            problems.append("窗口身份已变化，请重新圈选")
        # 结构性校验统一交给 region_save.validate_region_config，避免两套口径。
        # 别名重复由主界面在提交时用真实区域列表复核（这里看不到其他区域）。
        problems.extend(
            validate_region_config(
                self._build_region(),
                existing=(),
                windows=None,
                max_regions=0,
                check_duplicate_alias=False,
            )
        )
        return problems

    def _window_identity_changed(self, current) -> bool:
        if self.selected_window is None:
            return True
        identity = self._captured_identity or self._identity(self.selected_window)
        size = self._captured_outer_size or self._outer_size(self.selected_window)
        return self._identity(current) != identity or self._outer_size(current) != size

    def accept(self) -> None:
        evidence = (self._preview_generation, self._selection_generation,
                    (self._selection, self.selected_rect))
        if not self._preview_verified or self._preview_evidence != evidence:
            self.capture_status.setText("请先预览识别；也可保存为待确认。")
            return
        self._save_confirmed = True
        self._finish_save()

    def _finish_save(self) -> None:
        problems = self.selection_problems()
        if problems:
            self._cancel_preview()
            self._show_step(1)
            self.capture_status.setText("；".join(problems))
            return
        alias = self.alias.text().strip()
        if alias and alias in self.taken_aliases:
            self._save_confirmed = False
            self.capture_status.setText("区域名称已存在，请返回上一步修改。")
            return
        super().accept()

    def _build_region(self) -> RegionConfig:
        if self._selection is None or self.selected_window is None or self.selected_rect is None:
            raise RuntimeError("尚未完成圈选，不能生成采集配置")
        hwnd, backend, width, height = self._selection
        return RegionConfig(
            id=self._draft_id,
            alias=self.alias.text().strip(),
            window_title=self.selected_window.title,
            window_class=self.selected_window.class_name,
            window_rect=self.selected_window.rect,
            window_hwnd=hwnd,
            process_id=self.selected_window.process_id,
            process_path=self.selected_window.process_path,
            capture_backend=backend,
            coordinate_space="frame",
            reference_frame_width=width,
            reference_frame_height=height,
            needs_calibration=False,
            rect=self.selected_rect,
            enabled=self.existing.enabled if self.existing else True,
            # Only explicit confirmation of this calibration's OCR evidence can set true.
            previewed=self._save_confirmed,
            process_start_id=getattr(self.selected_window, "process_start_id", ""),
            reference_dpi=getattr(self.selected_window, "dpi", 0),
            **self.task_options.values(),
        )

    def region(self) -> RegionConfig:
        return self._build_region()


class PreviewWorker(QThread):
    completed = Signal(object, object, str)

    def __init__(
        self, region: RegionConfig, model: ModelConfig, base_dir: Path,
        frame: FrameSample | None = None,
        *, capture_client: WindowCaptureClient | None = None,
    ) -> None:
        super().__init__()
        self.region = region
        self.model = model
        self.base_dir = base_dir
        self.frame = frame
        # 监控中的预览借用已预热客户端，不另占全局两个进程额度；生命周期
        # 仍由 MonitorService 持有。预览只使用独立适配器的帧序号与诊断状态。
        self.capture_client = capture_client

    def run(self) -> None:
        if not _PREVIEW_SLOT.acquire(blocking=False):
            self.completed.emit(None, None, "上一轮预览正在结束，请稍后重试。")
            return
        try:
            frame = self.frame
            if frame is None:
                client = self.capture_client if self.capture_client is not None else WindowCaptureClient()
                frame = WindowFrameCapturer(client=client).capture(self.region, force=True)
            frame.content_mode = self.region.content_mode
            observation = LocalOcr(self.model, self.base_dir).recognize(frame)
            self.completed.emit(array_to_qimage(frame.image), observation, "")
        except Exception as error:
            self.completed.emit(None, None, f"{type(error).__name__}: {error}")
        finally:
            _PREVIEW_SLOT.release()


class PreviewDialog(QDialog):
    def __init__(
        self,
        region: RegionConfig,
        model: ModelConfig,
        base_dir: Path,
        keywords: list[str],
        send_callback,
        parent=None,
        frame: FrameSample | None = None,
        rules: RuleEngine | None = None,
        capture_client: WindowCaptureClient | None = None,
    ) -> None:
        super().__init__(parent)
        self._closed = False
        self.setWindowTitle(f"当前可见内容检查｜{region.alias}")
        self.region = region
        self.keywords = keywords
        self.send_callback = send_callback
        self.rules = rules
        self.decisions: dict[str, MessageDecision] = {}
        self.messages: list[MessageCandidate] = []
        # 最近一次成功识别的文字块数；0 表示没有证据，禁止“确认此区域”。
        self._last_block_count = 0
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(24, 20, 24, 20)
        self.layout.setSpacing(12)
        self.status = QLabel("正在预览识别……")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.layout.addWidget(self.status)
        self.image_label = PreviewImageLabel()
        self.layout.addWidget(self.image_label, 1)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.results_scroll = QScrollArea()
        self.results_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_scroll.setWidget(self.result_label)
        self.results_scroll.setFixedHeight(32)
        self.layout.addWidget(self.results_scroll)
        self.confirm_button = QPushButton("确认此区域")
        self.confirm_button.setToolTip(
            "确认这块区域能稳定识别到聊天文字后，才可以启动自动监控。"
        )
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.confirm_region)
        self.send_button = QPushButton("发送当前命中内容")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self.send_hits)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.confirm_button)
        row.addWidget(self.send_button)
        row.addWidget(close)
        self.layout.addLayout(row)
        # 与圈选对话框同一策略：按可用屏幕 90% 限制并居中（修 D4）。
        fit_dialog_to_screen(self, 1000, 720)
        worker_options = {"capture_client": capture_client} if capture_client is not None else {}
        self.worker = PreviewWorker(region, model, base_dir, frame, **worker_options)
        self.worker.completed.connect(self.show_result)
        worker = self.worker
        finished = getattr(worker, "finished", None)
        if finished is not None:
            finished.connect(lambda: self._worker_finished(worker))
        self.worker.start()

    def _worker_finished(self, worker):
        worker.frame = None
        if self._closed:
            worker.deleteLater()

    def done(self, result: int) -> None:
        self._closed = True
        worker, self.worker = getattr(self, "worker", None), None
        if worker is not None:
            if worker.isRunning():
                _detach_capture(worker)
            else:
                worker.frame = None
                worker.deleteLater()
        self.messages = []
        self.decisions = {}
        result_label = getattr(self, "result_label", None)
        if result_label is not None:
            result_label.clear()
        image = getattr(self, "image_label", None)
        if isinstance(image, PreviewImageLabel):
            image.set_preview_image(None)
        super().done(result)

    def show_result(self, image: QImage | None, observation, error: str) -> None:
        if self._closed:
            return
        if error:
            self.status.setText(f"检查失败：{error}")
            return
        self.messages = blocks_to_messages(observation)
        # 人工门禁的证据：只有真正识别出文字块才允许“确认此区域”。
        # 否则 0 块 + 满屏未覆盖也能点确认，门禁等于空转。
        self._last_block_count = len(observation.blocks)
        self.decisions = {
            message.tracking_id: self.rules.decide(message)
            for message in self.messages
        } if self.rules else {}
        hits = self.hit_messages()
        if self._last_block_count:
            self.status.setText(
                f"已识别 {self._last_block_count} 个文字块 · {observation.inference_ms:.0f} ms"
                + (f" · {len(observation.uncovered_boxes)} 处未覆盖" if observation.uncovered_boxes else "")
            )
        else:
            self.status.setText(
                "未识别到任何文字块，不能确认此区域。请调整圈选范围后重试。"
            )
        self.result_label.setText(preview_unit_lines(self.messages, self.rules) or "当前没有可读文字。")
        self.results_scroll.setFixedHeight(104 if self.messages else 32)
        pixmap = QPixmap.fromImage(image)
        if observation.uncovered_boxes:
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor("#d32f2f"), 3))
            for box in observation.uncovered_boxes:
                painter.drawRect(box.x, box.y, box.width, box.height)
            painter.end()
        self.image_label.set_preview_image(pixmap.toImage())
        self.send_button.setEnabled(bool(hits))
        self.confirm_button.setEnabled(self._last_block_count > 0)

    def confirm_region(self) -> None:
        """用户确认区域可用：写入 RegionConfig.previewed，才允许启动自动监控。

        入口二次复核：没有识别到任何文字块时禁止确认（否则人工门禁空转）。
        注意：这里只改内存对象，落盘由主界面的事务入口负责；落盘失败时主界面
        会把 ``previewed`` 回滚成 ``False``。
        """
        if getattr(self, "_last_block_count", 0) <= 0:
            QMessageBox.warning(
                self,
                "无法确认此区域",
                "这次检查没有识别到任何文字块，说明圈选区域可能不含可识别文字。\n"
                "请调整圈选范围后重新检查，识别到聊天文字才能确认。",
            )
            self.confirm_button.setEnabled(False)
            return
        self.region.previewed = True
        self.confirm_button.setEnabled(False)
        QMessageBox.information(
            self,
            "已确认此区域",
            f"“{self.region.alias}”已确认可用于自动监控；未覆盖区域（图片/卡片或版式无法判定，红框）不会参与通知。",
        )
        self.accept()

    def hit_messages(self) -> list[MessageCandidate]:
        if self.rules is not None:
            return [
                message
                for message in self.messages
                if self.decisions[message.tracking_id].should_notify
            ]
        from ..normalize import contains_all_chars

        return [
            message
            for message in self.messages
            if any(contains_all_chars(message.text, word) for word in self.keywords if word.strip())
        ]

    def send_hits(self) -> None:
        count = self.send_callback(self.region, self.hit_messages(), self.decisions)
        QMessageBox.information(self, "已处理", f"已写入 {count} 条通知队列；跨群去重仍生效。")
        self.send_button.setEnabled(False)


def pending_ui_workers() -> list[QThread]:
    """Snapshot detached UI work without waiting or touching any target window."""
    pending = []
    for worker in tuple(_DETACHED_CAPTURES):
        try:
            if worker.isRunning():
                pending.append(worker)
        except RuntimeError:
            # QObject deletion may already have reached its deferred-delete event.
            continue
    return pending


def close_region_dialogs(parent) -> list[QThread]:
    """Cancel owned region dialogs on the UI thread before application shutdown."""
    for dialog in tuple(parent.findChildren(QDialog)):
        if isinstance(dialog, RegionEditDialog | PreviewDialog):
            dialog.reject()
    return pending_ui_workers()


def wait_ui_workers(workers) -> None:
    """Background shutdown only: wait in short slices, never terminate native OCR."""
    for worker in workers:
        try:
            while worker.isRunning():
                worker.wait(100)
        except RuntimeError:
            # A finished worker can be destroyed by its UI-thread completion slot.
            continue
