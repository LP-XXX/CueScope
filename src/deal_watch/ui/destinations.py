"""Edit QQ endpoints and per-task routing on a detached draft."""

from __future__ import annotations

import copy
from dataclasses import asdict

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ..qq_notify import QQTarget
from .qq_binding import BindingThread, start_binding


def field(password=False):
    box = QLineEdit()
    if password:
        box.setEchoMode(QLineEdit.EchoMode.Password)
    box.setMaxLength(512)
    return box


class TargetEditor(QDialog):
    def __init__(self, current=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QQ 提醒接收端")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.scope = QComboBox()
        self.scope.addItem("发到我的 QQ", "user")
        self.scope.addItem("发到 QQ 群", "group")
        self.kind = QComboBox()
        self.kind.addItem("简易绑定 · 经 PushPlus", "pushplus")
        self.kind.addItem("直连腾讯 · 自有机器人", "qq")
        self.alias = field()
        self.alias.setText("我的 QQ")
        self.token, self.query_key = field(True), field(True)
        self.app_id, self.secret, self.option = field(), field(True), field()
        self.bind_button = QPushButton("生成一次性绑定码")
        for title, control in (("收件位置", self.scope), ("接入方式", self.kind), ("接收端名称", self.alias),
                               ("用户 Token", self.token), ("查询密钥（可选）", self.query_key),
                               ("群配置编码", self.option), ("AppID", self.app_id),
                               ("AppSecret", self.secret), ("目标会话", self.bind_button)):
            form.addRow(title, control)
        self.help = QLabel()
        self.help.setTextFormat(Qt.TextFormat.RichText)
        self.help.setOpenExternalLinks(True)
        self.help.setWordWrap(True)
        layout.addWidget(self.help)
        self.full_text = QCheckBox("随提醒发送来源、命中词和识别正文")
        self.enabled = QCheckBox("启用此接收端")
        self.enabled.setChecked(True)
        layout.addWidget(self.full_text)
        layout.addWidget(self.enabled)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.target = None
        self._thread = None
        self._bound = ""
        self._signature = ()
        self._form = form
        if current:
            self.scope.setCurrentIndex(self.scope.findData(current.scope))
            self.kind.setCurrentIndex(self.kind.findData(current.kind))
            for box, value in ((self.alias, current.alias), (self.token, current.token),
                               (self.query_key, current.query_key), (self.app_id, current.app_id),
                               (self.secret, current.secret), (self.option, current.target if current.kind == "pushplus" else "")):
                box.setText(value)
            self.full_text.setChecked(current.full_text)
            self.enabled.setChecked(current.enabled)
            if current.kind == "qq":
                self._bound = current.target
                self._signature = self.signature()
        self.scope.currentIndexChanged.connect(self._refresh)
        self.kind.currentIndexChanged.connect(self._refresh)
        self.app_id.textChanged.connect(self._refresh)
        self.secret.textChanged.connect(self._refresh)
        self.bind_button.clicked.connect(self._bind)
        self._refresh()

    def signature(self):
        return self.kind.currentData(), self.scope.currentData(), self.app_id.text().strip(), self.secret.text().strip()

    def _refresh(self, *_):
        direct = self.kind.currentData() == "qq"
        for control in (self.app_id, self.secret, self.bind_button):
            self._form.setRowVisible(control, direct)
        for control in (self.token, self.query_key):
            self._form.setRowVisible(control, not direct)
        self._form.setRowVisible(self.option, not direct and self.scope.currentData() == "group")
        self.help.setText(
            '在<a href="https://bot.q.qq.com/wiki/">腾讯开放平台</a>创建机器人，选择 WebSocket 并订阅单聊或群 @ 消息。将机器人加为好友或加入目标群，允许主动消息。'
            if direct else
            '先按<a href="https://www.pushplus.plus/doc/channel/qq.html">PushPlus 指引</a>实名并绑定 QQ。提醒经该服务中转，普通档在 QQ 显示标题摘要。启用开放接口并填写查询密钥后可查询投递结果。'
        )
        if self.signature() != self._signature:
            self._bound = ""
            if self._thread:
                self._thread.cancelled.set()
        self.status.setText("已绑定会话 · " + self._bound[-6:] if self._bound else "")

    def _bind(self):
        signature = self.signature()
        if not signature[2] or not signature[3]:
            self.status.setText("请先填写 AppID 和 AppSecret")
            return
        if self._thread:
            return
        self._signature = signature
        self._bound = ""
        thread = BindingThread(signature[2], signature[3], signature[1])
        self._thread = thread
        self.bind_button.setEnabled(False)
        self.status.setText("正在连接腾讯，绑定码有效期为两分钟。")

        thread.ready.connect(self._show_code)
        thread.matched.connect(self._matched)
        thread.failed.connect(self._binding_failed)
        thread.finished.connect(self._binding_done)
        start_binding(thread)

    def _current_binding(self):
        return (self.isVisible() and self._thread is not None and not self._thread.cancelled.is_set()
                and self.signature() == self._signature)

    @Slot(str)
    def _show_code(self, code):
        if self._current_binding():
            where = "在目标群 @机器人并发送" if self._signature[1] == "group" else "私聊机器人并发送"
            self.status.setText(f"{where}：{code}")

    @Slot(str)
    def _matched(self, target):
        if self._current_binding():
            self._bound = target
            self.status.setText("匹配到指定会话 · " + target[-6:] + "。确认无误后保存。")

    @Slot(str)
    def _binding_failed(self, detail):
        if self._current_binding():
            self.status.setText(detail)

    @Slot()
    def _binding_done(self):
        self.bind_button.setEnabled(True)
        self._thread = None

    def done(self, result):
        if self._thread:
            self._thread.cancelled.set()
        super().done(result)

    def accept(self):
        direct = self.kind.currentData() == "qq"
        try:
            self.target = QQTarget(
                self.kind.currentData(), self.scope.currentData(), self.alias.text().strip(),
                self._bound if direct else self.option.text().strip() if self.scope.currentData() == "group" else "",
                self.app_id.text().strip() if direct else "", self.secret.text().strip() if direct else "",
                self.token.text().strip() if not direct else "", self.query_key.text().strip() if not direct else "",
                self.full_text.isChecked(), self.enabled.isChecked(),
            )
        except ValueError as error:
            self.status.setText(str(error))
            return
        super().accept()


class DestinationsDialog(QDialog):
    def __init__(self, settings, regions, feishu_id, parent):
        super().__init__(parent)
        self.setWindowTitle("QQ 接收端与任务投递")
        self.settings = copy.deepcopy(settings)
        self.targets = [QQTarget(**item) for item in settings.get("targets", [])]
        self.tasks = copy.deepcopy(settings.get("tasks", {}))
        self.feishu_id = feishu_id
        self._task = None
        layout = QVBoxLayout(self)
        self.endpoints = QListWidget()
        self.endpoints.setAccessibleName("QQ 接收端")
        layout.addWidget(self.endpoints)
        row = QHBoxLayout()
        for label, fn in (("添加 QQ 接收端", self._add), ("编辑", self._edit), ("移除", self._remove)):
            button = QPushButton(label)
            button.clicked.connect(fn)
            row.addWidget(button)
        layout.addLayout(row)
        self.task = QComboBox()
        self.task.addItem("选择任务以指定接收端", "")
        for region in regions:
            self.task.addItem(region.alias, region.id)
        layout.addWidget(self.task)
        self.inherit = QCheckBox("使用全局启用的接收端")
        self.choices = QListWidget()
        layout.addWidget(self.inherit)
        layout.addWidget(self.choices)
        hint = QLabel("任务可单独勾选收件位置；取消所有勾选时仅本地记录。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.task.currentIndexChanged.connect(self._switch)
        self.inherit.toggled.connect(lambda inherit: self.choices.setEnabled(bool(self._task) and not inherit))
        self._draw()

    def _save_task(self):
        if self._task:
            if self.inherit.isChecked():
                self.tasks.pop(self._task, None)
            else:
                self.tasks[self._task] = [
                    self.choices.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.choices.count())
                    if self.choices.item(i).checkState() == Qt.CheckState.Checked
                ]

    def _switch(self, *_):
        self._save_task()
        self._draw()

    def _draw(self):
        self.endpoints.clear()
        self.choices.clear()
        available = [(self.feishu_id, "飞书")] if self.feishu_id else []
        for t in self.targets:
            title = f"{t.alias} · {'腾讯直连' if t.kind == 'qq' else 'PushPlus'} · {'启用' if t.enabled else '暂停'}"
            self.endpoints.addItem(title)
            available.append((t.id, t.alias))
        self._task = self.task.currentData()
        selected = self.tasks.get(self._task)
        self.inherit.setChecked(selected is None)
        self.inherit.setEnabled(bool(self._task))
        self.choices.setEnabled(bool(self._task) and selected is not None)
        for key, name in available:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setCheckState(Qt.CheckState.Checked if selected is None or key in selected else Qt.CheckState.Unchecked)
            self.choices.addItem(item)

    def _open(self, row):
        dialog = TargetEditor(self.targets[row] if row is not None else None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target = dialog.target
        self._save_task()
        if row is not None:
            self.targets.pop(row)
        self.targets = [t for t in self.targets if t.id != target.id]
        self.targets.append(target)
        self._draw()

    def _add(self):
        self._open(None)

    def _edit(self):
        row = self.endpoints.currentRow()
        if row >= 0:
            self._open(row)

    def _remove(self):
        row = self.endpoints.currentRow()
        if row >= 0:
            self._save_task()
            self.targets.pop(row)
            self._draw()

    def accept(self):
        self._save_task()
        self.settings = {"targets": [asdict(item) for item in self.targets], "tasks": self.tasks}
        super().accept()
