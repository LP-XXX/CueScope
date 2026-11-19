"""Compact region presentation; rows never own a tree of interactive widgets."""

from enum import IntEnum

from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
)

from .icons import icon


class RegionColumn(IntEnum):
    REGION = 0
    STATUS = 1
    SUCCESS = 2
    ACTIONS = 3


class RegionRole(IntEnum):
    ID = int(Qt.ItemDataRole.UserRole)
    SUBTITLE = ID + 1
    STATE = ID + 2
    DETAIL = ID + 3
    RETRY = ID + 4


class RegionDelegate(QStyledItemDelegate):
    action_requested = Signal(str, str)

    @staticmethod
    def action_rects(rect):
        y = rect.center().y() - 16
        return QRect(rect.x() + 6, y, 50, 32), QRect(rect.x() + 64, y, 30, 32)

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(80, 76)

    def paint(self, painter, option, index):
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        prepared.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, prepared, painter, option.widget)
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        text_color = option.palette.color(QPalette.ColorRole.Text)
        muted = option.palette.color(QPalette.ColorRole.PlaceholderText)
        accent = option.palette.color(QPalette.ColorRole.Link)
        area = option.rect.adjusted(12, 8, -10, -8)
        # Native styles can pass a system-default option font while QSS has
        # already resolved the view's UI font. Keep custom-painted rows aligned
        # with labels, headers and buttons in the same theme.
        font = QFont(option.widget.font() if option.widget else prepared.font)
        font.setPixelSize(14)
        painter.setFont(font)
        painter.setPen(text_color)
        value = str(index.data() or "")

        def line(text, rect, color, size=14, bold=False):
            face = QFont(font)
            face.setPixelSize(size)
            face.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
            painter.setFont(face)
            painter.setPen(color)
            clipped = painter.fontMetrics().elidedText(str(text), Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, clipped)

        if index.column() == RegionColumn.REGION:
            line(value, QRect(area.x(), area.y() + 7, area.width(), 22), text_color, bold=True)
            line(index.data(RegionRole.SUBTITLE) or "", QRect(area.x(), area.y() + 31, area.width(), 20), muted, 12)
        elif index.column() == RegionColumn.STATUS:
            state = str(index.data(RegionRole.STATE) or "idle")
            dark = option.palette.color(QPalette.ColorRole.Base).lightness() < 128
            colors = {
                "running": "#81D7A8" if dark else "#16754A",
                "error": "#FFA29A" if dark else "#B22C25",
                "pending": "#EBC876" if dark else "#825B08",
                "baselining": accent.name(),
            }
            color = QColor(colors.get(state, muted.name()))
            dot = QRect(area.x(), area.y() + 8, 6, 6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(dot)
            line(value.split("\n")[0], QRect(area.x() + 13, area.y(), area.width() - 13, 22), color, 13, True)
            detail = index.data(RegionRole.DETAIL) or ""
            retry = index.data(RegionRole.RETRY) or ""
            line(detail, QRect(area.x(), area.y() + 22, area.width(), 18), text_color, 12)
            line(retry, QRect(area.x(), area.y() + 40, area.width(), 18), muted, 12)
        elif index.column() == RegionColumn.ACTIONS:
            preview, more = self.action_rects(option.rect)
            if selected or option.state & QStyle.StateFlag.State_MouseOver:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(option.palette.color(QPalette.ColorRole.AlternateBase))
                painter.drawRoundedRect(preview, 6, 6)
                painter.drawRoundedRect(more, 6, 6)
            line("预览", preview.adjusted(8, 0, -4, 0), accent, 13)
            icon("more", color=muted.name(), size=18).paint(painter, more.adjusted(6, 7, -6, -7))
        else:
            line(value, QRect(area.x(), area.y() + 17, area.width(), 24), text_color, 13)
        painter.restore()

    def editorEvent(self, event, model, option, index):  # noqa: N802
        if (index.column() == RegionColumn.ACTIONS and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton):
            preview, more = self.action_rects(option.rect)
            point = event.position().toPoint()
            action = "preview" if preview.contains(point) else "more" if more.contains(point) else None
            if action:
                region_id = index.siblingAtColumn(RegionColumn.REGION).data(RegionRole.ID)
                self.action_requested.emit(action, str(region_id))
                return True
        return super().editorEvent(event, model, option, index)


class RegionTable(QTableWidget):
    action_requested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["区域", "状态", "最近成功", "操作"])
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setMouseTracking(True)
        self.verticalHeader().setDefaultSectionSize(76)
        self.setWordWrap(False)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.delegate = RegionDelegate(self)
        self.delegate.action_requested.connect(self.action_requested)
        self.setItemDelegate(self.delegate)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.setAccessibleName("监控区域，回车预览，Shift+F10打开区域操作")

    def _context_menu(self, pos):
        index = self.indexAt(pos)
        if index.isValid():
            self.selectRow(index.row())
            self.action_requested.emit("more", str(self.item(index.row(), 0).data(RegionRole.ID)))

    def keyPressEvent(self, event):  # noqa: N802
        row = self.currentRow()
        if row >= 0 and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Menu):
            self.action_requested.emit("more" if event.key() == Qt.Key.Key_Menu else "preview",
                                       str(self.item(row, 0).data(RegionRole.ID)))
            return
        if row >= 0 and event.key() == Qt.Key.Key_F10 and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.action_requested.emit("more", str(self.item(row, 0).data(RegionRole.ID)))
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.layout_columns()

    def layout_columns(self):
        available = max(1, self.viewport().width())
        action = 104
        latest = 110 if available >= 640 else 94
        status = max(170, int(available * .31))
        widths = [max(100, available - action - latest - status), status, latest, action]
        for column, width in enumerate(widths):
            if self.columnWidth(column) != width:
                self.setColumnWidth(column, width)
