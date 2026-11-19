"""Task-focused secondary pages. Persistence and monitoring remain owned by MainWindow."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__


def _label(text: str, role: str = "muted") -> QLabel:
    result = QLabel(text)
    result.setObjectName(role)
    result.setWordWrap(True)
    result.setMinimumWidth(0)
    result.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return result


def _button(text: str, callback=None, *, primary=False) -> QPushButton:
    result = QPushButton(text)
    if primary:
        result.setObjectName("primary")
    if callback is not None:
        result.clicked.connect(callback)
    return result


def _page(title: str):
    page = QWidget()
    page.setMinimumWidth(0)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(16)
    header = QHBoxLayout()
    header.setSpacing(8)
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    header.addWidget(heading)
    header.addStretch(1)
    layout.addLayout(header)
    return page, layout, header


class KeywordInput(QLineEdit):
    """Multiline paste adds whole entries without joining them into one keyword."""

    paste_lines = Signal(str)

    def keyPressEvent(self, event):  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Paste):
            text = QApplication.clipboard().text()
            if "\n" in text or "\r" in text:
                self.paste_lines.emit(text)
                event.accept()
                return
        super().keyPressEvent(event)


class KeywordList(QListWidget):
    delete_requested = Signal()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Delete and self.state() != QAbstractItemView.State.EditingState:
            self.delete_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class KeywordEditor(QWidget):
    """Visible editable entries with the text API used by the existing save transaction."""

    textChanged = Signal()
    modificationChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._baseline: list[str] = []
        self._updating = False
        self._modified = False
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        entry = QHBoxLayout()
        entry.setSpacing(8)
        self.input = KeywordInput()
        self.input.setPlaceholderText("输入关键词，按 Enter 添加")
        self.input.setAccessibleName("添加关键词")
        self.add_button = _button("添加", self._add_input)
        entry.addWidget(self.input, 1)
        entry.addWidget(self.add_button)
        layout.addLayout(entry)
        self.items = KeywordList()
        self.items.setAccessibleName("关键词词条")
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.items.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.items.setSpacing(3)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.items.setTextElideMode(Qt.TextElideMode.ElideRight)
        layout.addWidget(self.items, 1)
        self.empty_label = _label("添加需要关注的关键词。")
        layout.addWidget(self.empty_label)
        actions = QHBoxLayout()
        self.count_label = _label("0 个关键词")
        actions.addWidget(self.count_label, 1)
        self.edit_button = _button("编辑", self.edit_current)
        self.delete_button = _button("删除所选", self.delete_selected)
        self.batch_button = _button("批量编辑", self.edit_batch)
        for button in (self.edit_button, self.delete_button, self.batch_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.input.returnPressed.connect(self._add_input)
        self.input.paste_lines.connect(self.add_text)
        self.items.itemChanged.connect(self._item_changed)
        self.items.itemSelectionChanged.connect(self._selection_changed)
        self.items.delete_requested.connect(self.delete_selected)
        self._selection_changed()

    @staticmethod
    def normalized(text: str) -> list[str]:
        return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))

    def values(self) -> list[str]:
        return [self.items.item(i).text() for i in range(self.items.count())]

    def toPlainText(self) -> str:  # noqa: N802
        return "\n".join(self.values())

    def setPlainText(self, text: str) -> None:  # noqa: N802
        self._baseline = self.normalized(text)
        self._replace(self._baseline)

    def setPlaceholderText(self, text: str) -> None:  # noqa: N802
        self.input.setPlaceholderText(text)

    def isModified(self) -> bool:  # noqa: N802
        return self._modified

    def setModified(self, value: bool) -> None:  # noqa: N802
        if not value:
            self._baseline = self.values()
        changed = self._modified != bool(value)
        self._modified = bool(value)
        if changed:
            self.modificationChanged.emit(self._modified)

    def _replace(self, values: list[str]) -> None:
        selected = {item.text() for item in self.items.selectedItems()}
        scroll = self.items.verticalScrollBar().value()
        self._updating = True
        self.items.clear()
        for value in values:
            item = QListWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(value)
            item.setSizeHint(item.sizeHint().expandedTo(self._row_size()))
            self.items.addItem(item)
            item.setSelected(value in selected)
        self._updating = False
        self.items.verticalScrollBar().setValue(scroll)
        self.empty_label.setVisible(not values)
        self.count_label.setText(f"{len(values)} 个关键词")
        self.setModified(values != self._baseline)
        self._selection_changed()
        self.textChanged.emit()

    @staticmethod
    def _row_size():
        from PySide6.QtCore import QSize
        return QSize(0, 40)

    def add_text(self, text: str) -> None:
        values = list(dict.fromkeys([*self.values(), *self.normalized(text)]))
        if values != self.values():
            self._replace(values)

    def _add_input(self) -> None:
        self.add_text(self.input.text())
        self.input.clear()
        self.input.setFocus()

    def edit_current(self) -> None:
        item = self.items.currentItem()
        if item is not None:
            self.items.editItem(item)

    def _item_changed(self, _item) -> None:
        if not self._updating:
            self._replace(self.normalized(self.toPlainText()))

    def delete_selected(self) -> None:
        selected = {self.items.row(item) for item in self.items.selectedItems()}
        if selected:
            self._replace([value for index, value in enumerate(self.values()) if index not in selected])

    def _selection_changed(self) -> None:
        count = len(self.items.selectedItems())
        self.edit_button.setEnabled(count == 1)
        self.delete_button.setEnabled(count > 0)

    def edit_batch(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("批量编辑关键词")
        dialog.resize(520, 420)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_label("每行一个关键词。"))
        editor = QPlainTextEdit(self.toPlainText())
        editor.setAccessibleName("批量关键词，每行一个")
        layout.addWidget(editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用到草稿")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._replace(self.normalized(editor.toPlainText()))


class RecordTable(QTableWidget):
    """Records stay dense; message text gets most of the available width."""

    open_requested = Signal()

    def __init__(self, labels, empty_text: str, parent=None):
        super().__init__(0, len(labels), parent)
        self.empty_text = empty_text
        self.setHorizontalHeaderLabels(labels)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(46)
        self.horizontalHeader().setMinimumSectionSize(35)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.doubleClicked.connect(lambda _index: self.open_requested.emit())
        self.setToolTip("双击或按 Enter 查看完整记录。时间表示本机识别时间。")

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_columns()

    def viewportEvent(self, event):  # noqa: N802
        result = super().viewportEvent(event)
        if event.type() == QEvent.Type.Resize and self.columnCount() == 5:
            self._fit_columns()
        return result

    def _fit_columns(self):
        width = max(1, self.viewport().width())
        if getattr(self, "_last_fit_width", None) == width:
            return
        self._last_fit_width = width
        weights = (0.20, 0.14, 0.36, 0.12, 0.18)
        used = 0
        for column, weight in enumerate(weights):
            value = width - used if column == 4 else int(width * weight)
            self.setColumnWidth(column, max(35, value))
            used += value

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentRow() >= 0:
            self.open_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if not self.rowCount():
            painter = QPainter(self.viewport())
            painter.setPen(self.palette().placeholderText().color())
            painter.drawText(self.viewport().rect().adjusted(24, 20, -24, -20),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty_text)


class RecordDetailDialog(QDialog):
    """One themed detail surface; clipboard writes require an explicit button click."""

    def __init__(self, title: str, fields: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(680, 520)
        self.setMinimumSize(420, 320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setAccessibleName("完整记录详情")
        self.body.setPlainText("\n\n".join(f"{key}\n{value or '—'}" for key, value in fields))
        layout.addWidget(self.body, 1)
        self.message = next((value for key, value in fields if key in {"正文", "消息文字", "完整正文", "消息正文"}), "")
        actions = QHBoxLayout()
        self.copy_status = _label("")
        actions.addWidget(self.copy_status, 1)
        self.copy_button = _button("复制正文", self._copy)
        self.copy_button.setEnabled(bool(self.message))
        self.copy_button.setVisible(bool(self.message))
        actions.addWidget(self.copy_button)
        actions.addWidget(_button("关闭", self.accept))
        layout.addLayout(actions)

    def _copy(self):
        QApplication.clipboard().setText(self.message)
        self.copy_status.setText("已复制")


def build_keywords_page(host) -> QWidget:
    from .rule_workspace import RuleWorkspace
    page, layout, _header = _page("规则")
    tabs = QTabWidget()
    host.rule_workspace = RuleWorkspace(host)
    tabs.addTab(host.rule_workspace, "规则组")
    tabs.addTab(build_legacy_keywords_page(host), "兼容关键词")
    tabs.setCurrentIndex(0 if host.config.rule_groups or not host.config.keywords else 1)
    layout.addWidget(tabs, 1)
    return page


def build_legacy_keywords_page(host) -> QWidget:
    page, layout, header = _page("兼容关键词")
    host.keywords_dirty_label = _label("")
    host.keywords_dirty_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    host.keywords_save_button = _button("保存关键词", host.save_keywords, primary=True)
    header.addWidget(host.keywords_dirty_label)
    header.addWidget(host.keywords_save_button)
    help_row = QHBoxLayout()
    help_row.addWidget(_label("同条消息内按字符匹配。"), 1)
    help_button = _button("规则说明")
    help_button.setObjectName("textButton")
    help_button.setCheckable(True)
    help_row.addWidget(help_button)
    layout.addLayout(help_row)
    details = _label(
        "在同一条普通文字消息内按字符计数匹配，重复字符需满足数量。"
        "不要求相邻或同序，不跨消息拼字。全半角、英文大小写和气泡内换行会统一处理。"
        "保存只影响后续新增内容。"
    )
    details.hide()
    help_button.toggled.connect(details.setVisible)
    help_button.toggled.connect(lambda checked: help_button.setText("收起说明" if checked else "规则说明"))
    layout.addWidget(details)
    host.keywords_edit = KeywordEditor()
    host.keywords_edit.setPlainText("\n".join(host.config.keywords))
    host.keywords_edit.modificationChanged.connect(
        lambda changed: host.keywords_dirty_label.setText("未保存" if changed else "")
    )
    host.keywords_edit.modificationChanged.connect(host.keywords_save_button.setEnabled)
    host.keywords_save_button.setEnabled(False)
    layout.addWidget(host.keywords_edit, 1)
    return page


def build_records_page(host) -> QWidget:
    page, layout, _header = _page("活动记录")
    search = QHBoxLayout()
    host.activity_task = QLineEdit()
    host.activity_task.setPlaceholderText("任务名称")
    host.activity_text = QLineEdit()
    host.activity_text.setPlaceholderText("搜索识别文字")
    host.activity_time = QComboBox()
    for label, hours in (("全部时间", 0), ("最近 1 小时", 1), ("最近 24 小时", 24), ("最近 7 天", 168)):
        host.activity_time.addItem(label, hours)
    for control, name in ((host.activity_task, "按任务名称筛选"), (host.activity_text, "按正文筛选"),
                          (host.activity_time, "按识别时间筛选")):
        control.setAccessibleName(name)
        search.addWidget(control)
    def apply_search():
        host.set_activity_search(host.activity_task.text(), host.activity_text.text(), host.activity_time.currentData())

    host.activity_task.returnPressed.connect(apply_search)
    host.activity_text.returnPressed.connect(apply_search)
    search.addWidget(_button("筛选", apply_search))
    layout.addLayout(search)
    host.records_tabs = QTabWidget()
    host.records_tabs.setObjectName("recordsTabs")
    notifications = QWidget()
    notify_layout = QVBoxLayout(notifications)
    notify_layout.setContentsMargins(0, 16, 0, 0)
    filters = QHBoxLayout()
    host.event_filter = QComboBox()
    host.event_filter.setAccessibleName("活动类型")
    for label, key in (("全部事件", "all"), ("命中通知", "matches"), ("运行事件", "system")):
        host.event_filter.addItem(label, key)
    host.event_filter.currentIndexChanged.connect(lambda _index: host._change_event_filter(host.event_filter.currentData()))
    filters.addWidget(host.event_filter)
    filters.addStretch(1)
    notify_layout.addLayout(filters)
    host.record_table = RecordTable(
        ["识别时间", "来源", "消息摘要", "命中词", "投递状态"],
        "暂无通知记录\n命中后的投递结果会显示在这里。",
    )
    host.record_table.open_requested.connect(lambda: host.show_record_detail("notifications"))
    host.record_table.horizontalHeaderItem(4).setToolTip("飞书已接受不等于手机送达或已读；结果未知表示未取得明确回执。")
    notify_layout.addWidget(host.record_table, 1)
    footer = QHBoxLayout()
    host.records_count_label = _label("显示最近 200 条")
    footer.addWidget(host.records_count_label, 1)
    host.records_retry_button = _button("重试读取", lambda: host.refresh_records(force=True))
    host.records_retry_button.setAccessibleName("重试读取通知记录")
    host.records_retry_button.hide()
    footer.addWidget(host.records_retry_button)
    host.export_button = _button("导出记录", host.export_records)
    footer.addWidget(host.export_button)
    notify_layout.addLayout(footer)
    host.records_tabs.addTab(notifications, "事件与通知")
    recognition = QWidget()
    rec_layout = QVBoxLayout(recognition)
    rec_layout.setContentsMargins(0, 16, 0, 0)
    host.candidate_table = RecordTable(
        ["识别时间", "来源", "消息文字", "命中词", "处理结果"],
        "暂无识别记录\n开始监控后，普通文字的识别结果会显示在这里。",
    )
    host.candidate_table.open_requested.connect(lambda: host.show_record_detail("recognition"))
    rec_layout.addWidget(host.candidate_table, 1)
    row = QHBoxLayout()
    host.recognition_page_label = _label("正在准备记录…")
    row.addWidget(host.recognition_page_label, 1)
    host.recognition_query_retry = _button("重试读取", lambda: host.refresh_recognition(force=True))
    host.recognition_query_retry.setAccessibleName("重试读取识别记录")
    host.recognition_query_retry.hide()
    row.addWidget(host.recognition_query_retry)
    host.recognition_retry = _button("重试", host._begin_recognition_prepare)
    host.recognition_retry.hide()
    row.addWidget(host.recognition_retry)
    host.recognition_previous = _button("上一页", lambda: host._change_recognition_page(-1))
    host.recognition_next = _button("下一页", lambda: host._change_recognition_page(1))
    row.addWidget(host.recognition_previous)
    row.addWidget(host.recognition_next)
    rec_layout.addLayout(row)
    host.records_tabs.addTab(recognition, "全部识别")
    layout.addWidget(host.records_tabs, 1)
    return page


def _section(layout, title):
    frame = QFrame()
    frame.setObjectName("sectionFrame")
    frame.setMinimumWidth(0)
    body = QVBoxLayout(frame)
    body.setContentsMargins(16, 16, 16, 16)
    body.setSpacing(12)
    label = QLabel(title)
    label.setObjectName("sectionTitle")
    body.addWidget(label)
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setHorizontalSpacing(20)
    form.setVerticalSpacing(12)
    body.addLayout(form)
    layout.addWidget(frame)
    return body, form


class ThemeControls(QObject):
    """The application-wide theme signals must not retain destroyed page widgets."""

    def __init__(self, manager, combo, status, parent):
        super().__init__(parent)
        self.manager, self.combo, self.status = manager, combo, status
        combo.currentIndexChanged.connect(self.choose)
        manager.changed.connect(self.sync)
        manager.save_failed.connect(self.failed)

    @Slot(int)
    def choose(self, _index):
        self.manager.set_mode(self.combo.currentData())

    @Slot(str)
    def sync(self, _effective):
        old = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(max(0, self.combo.findData(str(self.manager.mode))))
        self.combo.blockSignals(old)
        self.status.hide()

    @Slot(str)
    def failed(self, message):
        self.status.setText(message)
        self.status.show()


def build_settings_page(host) -> QWidget:
    page, layout, header = _page("设置")
    host.settings_save_button = _button("保存设置", host.save_settings, primary=True)
    header.addWidget(host.settings_save_button)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    content.setMinimumWidth(0)
    body = QVBoxLayout(content)
    body.setContentsMargins(0, 0, 4, 0)
    body.setSpacing(16)
    notification, form = _section(body, "通知")
    notification_form = form
    host.notification_mode = QComboBox()
    host.notification_mode.setAccessibleName("通知方式")
    for label, key in (("发送到已启用的接收端", "auto"), ("仅本地记录与提示", "local"), ("仅飞书机器人", "feishu")):
        host.notification_mode.addItem(label, key)
    host.notification_mode.setMinimumWidth(0)
    host.notification_mode.setMinimumContentsLength(12)
    host.notification_mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    form.addRow("通知方式", host.notification_mode)
    host.webhook_edit = QLineEdit()
    host.webhook_edit.setEchoMode(QLineEdit.EchoMode.Password)
    host.webhook_edit.setAccessibleName("飞书 Webhook")
    host.webhook_edit.setPlaceholderText("输入飞书机器人 Webhook")
    host.secret_edit = QLineEdit()
    host.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
    host.secret_edit.setAccessibleName("签名密钥")
    host.secret_edit.setPlaceholderText("选填")
    form.addRow("飞书 Webhook", host.webhook_edit)
    form.addRow("签名密钥", host.secret_edit)
    host.manage_destinations = _button("配置 QQ 与任务接收端", host.edit_destinations)
    form.addRow("QQ 提醒", host.manage_destinations)
    host.test_target = QComboBox()
    host.test_target.setAccessibleName("试发接收端")
    form.addRow("试发接收端", host.test_target)
    host.test_button = _button("发送测试消息", host.send_test)
    host.test_button.setToolTip("仅点击此按钮才发送测试通知。")
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(host.test_button)
    notification.addLayout(row)
    monitor, form = _section(body, "监控")
    host.poll_seconds = QSpinBox()
    host.poll_seconds.setRange(10, 300)
    host.poll_seconds.setSuffix(" 秒")
    host.poll_seconds.setAccessibleName("默认采样间隔")
    form.addRow("默认间隔", host.poll_seconds)
    monitor.addWidget(_label("任务可单独设置间隔；实际延迟在任务详情查看。"))
    storage, form = _section(body, "存储")
    host.record_limit = QSpinBox()
    from .notification_preferences import NotificationTools
    host.notification_tools = NotificationTools(host, page, notification_form, storage)
    host.record_limit.setRange(1, 2147483647)
    host.record_limit.setSingleStep(1000)
    host.record_limit.setGroupSeparatorShown(True)
    host.record_limit.setAccessibleName("识别记录保留条数")
    form.addRow("保留识别记录", host.record_limit)
    host.save_images = QCheckBox("保存命中截图")
    host.save_images.setToolTip("仅保存命中文字区域裁剪，最多 200 MB，保留三天。")
    storage.addWidget(host.save_images)
    storage.addWidget(_label("超出条数时删除最旧记录。"))
    appearance, form = _section(body, "外观与启动")
    host.theme_mode_combo = QComboBox()
    host.theme_mode_combo.setAccessibleName("界面主题")
    for text, value in (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")):
        host.theme_mode_combo.addItem(text, value)
    manager = host.theme_manager
    host.theme_mode_combo.setCurrentIndex(max(0, host.theme_mode_combo.findData(str(manager.mode))))
    form.addRow("主题", host.theme_mode_combo)
    appearance.addWidget(_label("切换后自动保存。"))
    host.theme_save_status = _label("")
    host.theme_save_status.hide()
    page._theme_binding = ThemeControls(manager, host.theme_mode_combo, host.theme_save_status, page)
    appearance.addWidget(host.theme_save_status)
    host.auto_start = QCheckBox("登录后启动应用")
    host.auto_start.setToolTip("启动应用不等于自动开始监控，也不支持锁屏持续采集。")
    appearance.addWidget(host.auto_start)
    about_toggle = QToolButton()
    about_toggle.setText("关于与诊断")
    about_toggle.setCheckable(True)
    about_toggle.setArrowType(Qt.ArrowType.RightArrow)
    about_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    body.addWidget(about_toggle)
    about = QWidget()
    about_layout = QVBoxLayout(about)
    about_layout.setContentsMargins(16, 4, 16, 16)
    about_layout.addWidget(_label(f"版本 {__version__} · 本地 OCR · WGC"))
    paths = _label(f"配置：{host.config_store.path}\n数据：{host.data_dir}")
    paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    about_layout.addWidget(paths)
    open_folder = _button("打开数据目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(host.data_dir)))))
    about_layout.addWidget(open_folder, 0, Qt.AlignmentFlag.AlignLeft)
    about.hide()
    about_toggle.toggled.connect(about.setVisible)
    about_toggle.toggled.connect(lambda checked: about_toggle.setArrowType(
        Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
    body.addWidget(about)
    body.addStretch(1)
    scroll.setWidget(content)
    layout.addWidget(scroll, 1)
    return page
