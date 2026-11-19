"""Editable rule groups with local explanations; persistence belongs to MainWindow."""

from __future__ import annotations

import copy
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..rule_config import MAX_CONDITIONS_PER_GROUP, RuleGroup
from ..rule_evaluation import evaluate_groups

_MODES = {"contains": "包含短语", "exact": "整条相等", "char_multiset": "字符组合"}
_SPACES = {"collapse": "合并连续空白", "preserve": "保留全部空白", "remove": "忽略全部空白"}


def _label(text="", role="muted"):
    label = QLabel(text)
    label.setObjectName(role)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


def _combo(values):
    result = QComboBox()
    result.setMinimumWidth(0)
    result.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    result.setMinimumContentsLength(6)
    for key, text in values.items():
        result.addItem(text, key)
    return result


class RuleWorkspace(QWidget):
    """Group/condition drafts survive selection and theme changes.

    ``host.save_rule_groups(groups)`` accepts validated detached RuleGroup objects.
    An asynchronous owner calls ``set_groups`` and/or ``show_save_result`` on completion.
    ``set_busy`` locks the editor while saving or while runtime disallows rule changes.
    """

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self._drafts: list[dict] = []
        self._committed: list[dict] = []
        self._current_group_id = ""
        self._current_condition_id = ""
        self._updating = False
        self._busy = False
        self._dirty = False
        self._saving = False
        self._pending_groups = None
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.editor = QWidget()
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(12)
        toolbar = QHBoxLayout()
        self.group_combo = _combo({})
        self.group_combo.setAccessibleName("选择规则组")
        self.new_button = QPushButton("新建组")
        self.delete_button = QPushButton("删除组")
        self.test_button = QPushButton("试测")
        toolbar.addWidget(self.group_combo, 1)
        toolbar.addWidget(self.new_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addWidget(self.test_button)
        editor_layout.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setMinimumWidth(0)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setMinimumWidth(0)
        self.body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(12)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(160)
        self.name_edit.setAccessibleName("规则组名称")
        self.combination_combo = _combo({"any": "任一条件满足", "all": "全部条件满足"})
        self.group_enabled = QCheckBox("启用此规则组")
        form.addRow("名称", self.name_edit)
        form.addRow("判断方式", self.combination_combo)
        form.addRow("", self.group_enabled)
        body_layout.addLayout(form)
        self.references_label = _label()
        body_layout.addWidget(self.references_label)
        body_layout.addWidget(_label("条件", "sectionTitle"))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["用途", "内容", "匹配方式"])
        self.table.setAccessibleName("规则条件列表")
        self.table.setToolTip("双击条件或点击编辑所选，调整匹配方式与文本。")
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumWidth(0)
        self.table.setMinimumHeight(130)
        self.table.setMaximumHeight(220)
        self.table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 64)
        self.table.setColumnWidth(2, 104)
        body_layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.add_button = QPushButton("添加条件")
        self.exclude_button = QPushButton("添加排除")
        self.remove_condition_button = QPushButton("删除条件")
        self.edit_condition_button = QPushButton("编辑所选")
        for button in (self.add_button, self.exclude_button, self.edit_condition_button, self.remove_condition_button):
            actions.addWidget(button)
        actions.addStretch(1)
        body_layout.addLayout(actions)

        self.condition_editor = QWidget()
        detail = QFormLayout(self.condition_editor)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        detail.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.condition_text = QPlainTextEdit()
        self.condition_text.setPlaceholderText("输入要匹配的短语或文字")
        self.condition_text.setAccessibleName("条件文本")
        self.condition_text.setFixedHeight(66)
        self.condition_text.setMinimumWidth(0)
        self.condition_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.mode_combo = _combo(_MODES)
        self.case_sensitive = QCheckBox("区分英文大小写")
        self.whitespace_combo = _combo(_SPACES)
        self.condition_enabled = QCheckBox("启用此条件")
        detail.addRow("文本", self.condition_text)
        detail.addRow("匹配", self.mode_combo)
        detail.addRow("空白", self.whitespace_combo)
        detail.addRow("", self.case_sensitive)
        detail.addRow("", self.condition_enabled)
        body_layout.addWidget(self.condition_editor)
        body_layout.addWidget(_label("排除条件优先；所有条件只判断同一条内容。"))

        body_layout.addWidget(_label("试一下", "sectionTitle"))
        self.sample_edit = QPlainTextEdit()
        self.sample_edit.setPlaceholderText("粘贴一条示例文字，在本地检查结果；不会发送通知。")
        self.sample_edit.setAccessibleName("规则测试样例")
        self.sample_edit.setMinimumWidth(0)
        self.sample_edit.setFixedHeight(80)
        self.sample_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        body_layout.addWidget(self.sample_edit)
        self.preview_status = _label("输入示例文字查看判断。", "statusBadge")
        self.preview_status.setProperty("tone", "neutral")
        body_layout.addWidget(self.preview_status)
        self.preview_details = QPlainTextEdit()
        self.preview_details.setAccessibleName("逐条件判断说明")
        self.preview_details.setReadOnly(True)
        self.preview_details.setMinimumWidth(0)
        self.preview_details.setFixedHeight(110)
        self.preview_details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.preview_details.hide()
        body_layout.addWidget(self.preview_details)
        body_layout.addStretch(1)
        self.scroll.setWidget(self.body)
        editor_layout.addWidget(self.scroll, 1)
        layout.addWidget(self.editor, 1)
        footer = QHBoxLayout()
        self.status_label = _label("")
        self.save_button = QPushButton("保存规则组")
        self.save_button.setObjectName("primary")
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(100)
        self._preview_timer.timeout.connect(self._preview)
        self.group_combo.currentIndexChanged.connect(self._select_group)
        self.new_button.clicked.connect(self._new_group)
        self.delete_button.clicked.connect(self._delete_group)
        self.name_edit.textChanged.connect(self._update_group)
        self.combination_combo.currentIndexChanged.connect(self._update_group)
        self.group_enabled.toggled.connect(self._update_group)
        self.table.currentCellChanged.connect(self._select_condition)
        self.add_button.clicked.connect(lambda: self._add_condition(False))
        self.exclude_button.clicked.connect(lambda: self._add_condition(True))
        self.remove_condition_button.clicked.connect(self._remove_condition)
        self.condition_text.textChanged.connect(self._update_condition)
        self.mode_combo.currentIndexChanged.connect(self._update_condition)
        self.case_sensitive.toggled.connect(self._update_condition)
        self.whitespace_combo.currentIndexChanged.connect(self._update_condition)
        self.condition_enabled.toggled.connect(self._update_condition)
        self.sample_edit.textChanged.connect(self._schedule_preview)
        self.save_button.clicked.connect(self._save)
        self.test_button.clicked.connect(self._focus_sample)
        self.edit_condition_button.clicked.connect(self._focus_condition)
        self.table.itemDoubleClicked.connect(self._focus_condition)
        self.set_groups(getattr(host.config, "rule_groups", []))

    def _focus_condition(self, *_):
        self.scroll.ensureWidgetVisible(self.condition_editor)
        self.condition_text.setFocus()

    def _focus_sample(self):
        self.scroll.ensureWidgetVisible(self.sample_edit)
        self.sample_edit.setFocus()

    def minimumSizeHint(self):  # noqa: N802
        return QSize(320, 260)

    @property
    def modified(self):
        return self._dirty

    def set_groups(self, groups, selected_id=""):
        incoming = [copy.deepcopy(item.to_dict() if isinstance(item, RuleGroup) else item) for item in groups]
        # Ordinary page refreshes must not throw away edits or the selected condition.
        if self._dirty and not self._saving and incoming == self._committed:
            return
        current = selected_id or self._current_group_id
        self._drafts = copy.deepcopy(incoming)
        self._committed = incoming
        self._dirty = False
        self._refresh_groups(current)
        self.status_label.setText("" if self._drafts else "尚无规则组。点击“新建组”开始。")

    def add_committed_group(self, group):
        raw = group.to_dict()
        self._committed.append(copy.deepcopy(raw))
        self._drafts.append(raw)
        self._refresh_groups(self._current_group_id or group.id)

    def set_busy(self, busy, message=""):
        self._busy = bool(busy)
        self.editor.setEnabled(not self._busy)
        self.save_button.setEnabled(not self._busy)
        if message:
            self.status_label.setText(message)

    def show_save_result(self, success, message=""):
        if success and self._pending_groups is not None:
            self.set_groups(self._pending_groups, self._current_group_id)
        self._pending_groups = None
        self._saving = False
        self.set_busy(False)
        self.status_label.setText(message or ("规则组已保存。" if success else "保存失败，草稿已保留。"))

    def _current_group(self):
        return next((item for item in self._drafts if item["id"] == self._current_group_id), None)

    def _all_conditions(self):
        group = self._current_group()
        return [] if group is None else [
            (key, item) for key in ("conditions", "exclusions") for item in group[key]
        ]

    def _current_condition(self):
        return next((item for _, item in self._all_conditions() if item["id"] == self._current_condition_id), None)

    def _mark_dirty(self):
        self._dirty = True
        self.status_label.setText("有未保存的更改。")
        self._schedule_preview()

    def _refresh_groups(self, selected_id=""):
        self._updating = True
        self.group_combo.clear()
        for group in self._drafts:
            self.group_combo.addItem(group["name"] or "未命名规则组", group["id"])
        index = self.group_combo.findData(selected_id)
        self.group_combo.setCurrentIndex(index if index >= 0 else 0)
        self._updating = False
        self._select_group()

    def _select_group(self, *_):
        if self._updating:
            return
        self._current_group_id = self.group_combo.currentData() or ""
        self._current_condition_id = ""
        group = self._current_group()
        self.body.setEnabled(group is not None)
        self.delete_button.setEnabled(group is not None)
        self._updating = True
        self.name_edit.setText(group["name"] if group else "")
        self.combination_combo.setCurrentIndex(self.combination_combo.findData(group["combination"] if group else "any"))
        self.group_enabled.setChecked(bool(group and group["enabled"]))
        self._updating = False
        self._refresh_conditions()
        count = self._reference_count()
        self.references_label.setText(f"用于 {count} 个监测任务" if count else "保存后可在监测任务中选择此规则组。")
        if not group:
            self.status_label.setText("尚无规则组。点击“新建组”开始。")
        self._schedule_preview()

    def _update_group(self, *_):
        if self._updating or self._busy:
            return
        group = self._current_group()
        if group is None:
            return
        group.update(name=self.name_edit.text(), combination=self.combination_combo.currentData(), enabled=self.group_enabled.isChecked())
        self.group_combo.setItemText(self.group_combo.currentIndex(), group["name"] or "未命名规则组")
        self._mark_dirty()

    def _new_group(self):
        if self._busy:
            return
        group = dict(id=uuid4().hex, name=f"规则组 {len(self._drafts) + 1}", conditions=[], exclusions=[], combination="any", enabled=True)
        self._drafts.append(group)
        self._refresh_groups(group["id"])
        self._add_condition(False)
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _reference_count(self):
        return sum(
            getattr(region, "rule_group_id", "") == self._current_group_id
            or self._current_group_id in getattr(region, "rule_group_ids", ())
            for region in getattr(self.host.config, "regions", [])
        ) if self._current_group_id else 0

    def _delete_group(self):
        if self._busy or self._current_group() is None:
            return
        count = self._reference_count()
        if count:
            self.status_label.setText(f"此规则组用于 {count} 个任务，请先为这些任务更换规则组。")
            return
        self._drafts = [item for item in self._drafts if item["id"] != self._current_group_id]
        self._refresh_groups()
        self._mark_dirty()

    def _refresh_conditions(self, selected_id=""):
        target = selected_id or self._current_condition_id
        conditions = self._all_conditions()
        self._updating = True
        self.table.setRowCount(len(conditions))
        selected = -1
        for row, (purpose, item) in enumerate(conditions):
            texts = ["排除" if purpose == "exclusions" else "满足", item["text"] or "填写条件…", _MODES.get(item["mode"], item["mode"])]
            if not item["enabled"]:
                texts[0] = "已停用"
            for column, text in enumerate(texts):
                cell = QTableWidgetItem(text.replace("\n", " "))
                cell.setData(Qt.ItemDataRole.UserRole, item["id"])
                cell.setToolTip(text)
                self.table.setItem(row, column, cell)
            if item["id"] == target:
                selected = row
        if conditions:
            self.table.setCurrentCell(max(0, selected), 0)
        self._updating = False
        self._select_condition()

    def _select_condition(self, *_):
        if self._updating:
            return
        cell = self.table.item(self.table.currentRow(), 0)
        self._current_condition_id = cell.data(Qt.ItemDataRole.UserRole) if cell else ""
        item = self._current_condition()
        self.condition_editor.setEnabled(item is not None)
        self.remove_condition_button.setEnabled(item is not None)
        self._updating = True
        self.condition_text.setPlainText(item["text"] if item else "")
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(item["mode"] if item else "contains"))
        self.case_sensitive.setChecked(bool(item and item["case_sensitive"]))
        self.whitespace_combo.setCurrentIndex(self.whitespace_combo.findData(item["whitespace"] if item else "collapse"))
        self.whitespace_combo.setEnabled(bool(item and item["mode"] != "char_multiset"))
        self.condition_enabled.setChecked(bool(item and item["enabled"]))
        self._updating = False

    def _update_condition(self, *_):
        if self._updating or self._busy:
            return
        item = self._current_condition()
        if item is None:
            return
        item.update(text=self.condition_text.toPlainText(), mode=self.mode_combo.currentData(), case_sensitive=self.case_sensitive.isChecked(), whitespace=self.whitespace_combo.currentData(), enabled=self.condition_enabled.isChecked())
        self.whitespace_combo.setEnabled(item["mode"] != "char_multiset")
        row = self.table.currentRow()
        if row >= 0:
            cell = self.table.item(row, 1)
            cell.setText(item["text"].replace("\n", " ") or "填写条件…")
            cell.setToolTip(item["text"])
            self.table.item(row, 2).setText(_MODES[item["mode"]])
            purpose = next(key for key, candidate in self._all_conditions() if candidate is item)
            self.table.item(row, 0).setText(("排除" if purpose == "exclusions" else "满足") if item["enabled"] else "已停用")
        self._mark_dirty()

    def _add_condition(self, exclusion):
        group = self._current_group()
        if self._busy or group is None:
            return
        if len(self._all_conditions()) >= MAX_CONDITIONS_PER_GROUP:
            self.status_label.setText(f"每组最多 {MAX_CONDITIONS_PER_GROUP} 个条件。")
            return
        item = dict(id=uuid4().hex, text="", mode="contains", case_sensitive=False, whitespace="collapse", label="", enabled=True)
        group["exclusions" if exclusion else "conditions"].append(item)
        self._refresh_conditions(item["id"])
        self._mark_dirty()
        self.condition_text.setFocus()

    def _remove_condition(self):
        group = self._current_group()
        if self._busy or group is None:
            return
        for key in ("conditions", "exclusions"):
            group[key] = [item for item in group[key] if item["id"] != self._current_condition_id]
        self._refresh_conditions()
        self._mark_dirty()

    def _schedule_preview(self):
        if not self._updating:
            self._preview_timer.start()

    def _preview(self):
        raw = self._current_group()
        self.preview_details.clear()
        self.preview_details.hide()
        if raw is None:
            self._preview_result("先创建或选择一个规则组。", "neutral")
            return
        try:
            group = RuleGroup.from_dict(raw)
        except ValueError as error:
            self._preview_result(str(error), "warning")
            return
        sample = self.sample_edit.toPlainText()
        if not sample:
            self._preview_result("输入一条示例文字查看判断。", "neutral")
            return
        result = evaluate_groups(sample, [group])
        if result.error:
            self._preview_result(result.error, "warning")
        elif result.excluded_by:
            self._preview_result("已排除：" + "、".join(result.excluded_by), "neutral")
        elif result.should_notify:
            self._preview_result("条件满足 · 运行时仍需符合新内容与去重规则", "success")
        else:
            self._preview_result("条件未满足" if group.enabled else "此规则组已停用", "neutral")
        details = []
        for item in result.condition_results:
            purpose = "排除" if item.is_exclusion else "正向"
            details.append(f"{'满足' if item.matched else '未满足'} · {purpose} · {_MODES[item.mode]}：{item.label}")
            details.append(f"比较文本：{item.normalized_text!r}\n条件文本：{item.normalized_expected!r}")
        if details:
            self.preview_details.setPlainText("\n\n".join(details))
            self.preview_details.show()

    def _preview_result(self, text, tone):
        self.preview_status.setText(text)
        self.preview_status.setProperty("tone", tone)
        self.preview_status.style().unpolish(self.preview_status)
        self.preview_status.style().polish(self.preview_status)

    def _save(self):
        if self._busy:
            return
        groups = []
        for raw in self._drafts:
            try:
                groups.append(RuleGroup.from_dict(raw))
            except ValueError as error:
                self._refresh_groups(raw["id"])
                self.status_label.setText(f"{raw['name'] or '未命名规则组'}：{error}")
                return
        self._pending_groups = groups
        self._saving = True
        self.set_busy(True, "正在保存规则组…")
        try:
            result = self.host.save_rule_groups(groups)
        except Exception as error:
            self.show_save_result(False, f"保存失败：{error}；草稿已保留。")
            return
        if isinstance(result, bool):
            self.show_save_result(result)
