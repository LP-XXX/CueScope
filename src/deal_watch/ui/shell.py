"""Navigation and shared application chrome, independent of monitoring services."""

from enum import IntEnum

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..brand import DISPLAY_NAME, brand_icon
from .icons import icon


class PageId(IntEnum):
    MONITOR = 0
    KEYWORDS = 1
    RECORDS = 2
    SETTINGS = 3


class RecordView(IntEnum):
    NOTIFICATIONS = 0
    RECOGNITION = 1


PAGE_NAMES = {
    PageId.MONITOR: ("监测任务", "monitor"),
    PageId.KEYWORDS: ("规则", "keywords"),
    PageId.RECORDS: ("活动记录", "records"),
    PageId.SETTINGS: ("设置", "settings"),
}


class PageStack(QStackedWidget):
    """Named pages; tabText is retained for existing accessibility inspection tools."""

    def tabText(self, index):  # noqa: N802
        return PAGE_NAMES[PageId(index)][0]


class AppShell(QWidget):
    page_changed = Signal(int)
    help_requested = Signal()

    def __init__(self, version, parent=None):
        super().__init__(parent)
        self.setObjectName("appShell")
        self._compact = False
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.rail = QFrame()
        self.rail.setObjectName("navigationRail")
        rail = QVBoxLayout(self.rail)
        rail.setContentsMargins(12, 24, 12, 16)
        rail.setSpacing(8)
        self.brand = QLabel(DISPLAY_NAME)
        self.brand.setObjectName("brandTitle")
        self.brand.setFixedHeight(36)
        self.brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row = QHBoxLayout()
        self.brand_symbol = QLabel()
        self.brand_symbol.setPixmap(brand_icon(28).pixmap(28, 28))
        self.brand_symbol.setFixedWidth(28)
        brand_row.addWidget(self.brand_symbol)
        brand_row.addWidget(self.brand, 1)
        rail.addLayout(brand_row)
        rail.addSpacing(24)
        self.nav_buttons = {}
        for page, (title, _icon_name) in PAGE_NAMES.items():
            button = QPushButton(title)
            button.setObjectName("navButton")
            button.setAccessibleName(title)
            button.setToolTip(title)
            button.setCheckable(True)
            button.setMinimumHeight(44)
            button.clicked.connect(lambda _checked=False, p=page: self.set_page(p))
            self.nav_buttons[page] = button
            rail.addWidget(button)
        rail.addStretch()
        self.help_button = QPushButton("帮助")
        self.help_button.setObjectName("navButton")
        self.help_button.setToolTip("使用帮助")
        self.help_button.setAccessibleName("使用帮助")
        self.help_button.clicked.connect(self.help_requested)
        rail.addWidget(self.help_button)
        self.version = QLabel(f"v{version}")
        self.version.setObjectName("muted")
        self.version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rail.addWidget(self.version)
        root.addWidget(self.rail)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(20)
        self.header = QHBoxLayout()
        self.header.setSpacing(12)
        self.status = QLabel("正在准备")
        self.status.setObjectName("statusBadge")
        self.status.setMinimumWidth(0)
        self.status.setMaximumWidth(400)
        self.status.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.header.addWidget(self.status)
        self.header.addStretch(1)
        self.controls = QStackedWidget()
        self.controls.setFixedSize(116, 38)
        self.header.addWidget(self.controls)
        layout.addLayout(self.header)
        self.pages = PageStack()
        layout.addWidget(self.pages, 1)
        root.addWidget(content, 1)
        self.pages.currentChanged.connect(self._page_changed)
        self._set_compact(False)
        self.refresh_icons()

    def set_page(self, page):
        self.pages.setCurrentIndex(int(page))
        self._update_selection()

    def _page_changed(self, index):
        self._update_selection()
        self.page_changed.emit(index)

    def _update_selection(self):
        for page, button in self.nav_buttons.items():
            button.setChecked(int(page) == self.pages.currentIndex())
        self.refresh_icons()

    def refresh_icons(self):
        self.brand_symbol.setPixmap(brand_icon(28, color=self.palette().color(QPalette.ColorRole.Link)).pixmap(28, 28))
        for page, (_title, name) in PAGE_NAMES.items():
            role = QPalette.ColorRole.Link if self.nav_buttons[page].isChecked() else QPalette.ColorRole.WindowText
            self.nav_buttons[page].setIcon(icon(name, color=self.palette().color(role).name()))
        self.help_button.setIcon(icon("help"))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._set_compact(self.width() < 900)

    def _set_compact(self, compact):
        self._compact = compact
        self.rail.setFixedWidth(64 if compact else 160)
        self.brand.setVisible(not compact)
        self.brand.setText(DISPLAY_NAME)
        for page, button in self.nav_buttons.items():
            button.setText("" if compact else PAGE_NAMES[page][0])
        self.help_button.setText("" if compact else "帮助")
        self.version.setVisible(not compact)
