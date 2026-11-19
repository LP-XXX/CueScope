"""Task semantics shared by calibration and previews; no desktop access."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QWidget,
)

from ..content import CONTENT_MODES
from ..models import new_id
from ..rule_config import RuleCondition, RuleGroup


class TaskOptions(QWidget):
    changed = Signal()

    def __init__(self, config=None, existing=None, parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        self.mode = QComboBox()
        for key, label in CONTENT_MODES.items():
            self.mode.addItem(label, key)
        self.rules = QComboBox()
        self._has_keywords = bool(getattr(config, "keywords", []))
        self.rules.addItem("兼容关键词（旧任务）" if self._has_keywords or existing else "暂不设规则，先保存选区", "")
        self._active_groups = {g.id for g in getattr(config, "rule_groups", []) if g.enabled}
        self._known_config = config is not None
        for group in getattr(config, "rule_groups", []):
            self.rules.addItem(group.name, group.id)
        self.quick_phrase = QLineEdit()
        self.quick_phrase.setAccessibleName("直接输入提醒短语")
        self.quick_phrase.setPlaceholderText("例如：任务完成；留空则使用规则组")
        self.quick_phrase.setMaxLength(160)
        self._group_id, self._condition_id = new_id(), new_id()
        self.title_policy = QComboBox()
        self.title_policy.addItem("固定标题，变化后确认", "strict")
        self.title_policy.addItem("跟随此窗口当前内容", "window")
        self.interval = QSpinBox()
        self.interval.setRange(0, 300)
        self.interval.setSpecialValueText("使用默认间隔")
        self.interval.setSuffix(" 秒")
        self.interval.setToolTip("任务可设 10–300 秒；0 表示使用设置页默认值")
        self.dedupe = QComboBox()
        for title, key in (("仅当前任务", "task"), ("指定去重组", "group"), ("全部任务（旧行为）", "global")):
            self.dedupe.addItem(title, key)
        self.group = QLineEdit()
        self.group.setPlaceholderText("内容相同的来源使用同一个组名")
        self.group.setMaxLength(128)
        for title, widget in (("内容模式", self.mode), ("提醒短语", self.quick_phrase), ("规则组", self.rules), ("窗口跟随", self.title_policy),
                              ("采样间隔", self.interval), ("去重范围", self.dedupe), ("去重组", self.group)):
            widget.setAccessibleName(title)
            widget.setMinimumWidth(0)
            if isinstance(widget, QComboBox):
                widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                widget.setMinimumContentsLength(12)
            form.addRow(title, widget)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        self.hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        form.addRow(self.hint)
        for widget, value in ((self.mode, getattr(existing, "content_mode", "generic_line")),
                              (self.rules, getattr(existing, "rule_group_id", self.rules.itemData(1) if self.rules.count() > 1 else "")),
                              (self.title_policy, getattr(existing, "title_policy", "strict")),
                              (self.dedupe, getattr(existing, "dedupe_scope", "task"))):
            index = widget.findData(value)
            if index < 0:
                widget.addItem("配置引用不可用，请重新选择", value)
                index = widget.count() - 1
            widget.setCurrentIndex(index)
        self.interval.setValue(getattr(existing, "poll_interval_ms", 0) // 1000)
        self.group.setText(getattr(existing, "dedupe_group", ""))
        for widget in (self.mode, self.rules, self.title_policy, self.dedupe):
            widget.currentIndexChanged.connect(self._changed)
        self.interval.valueChanged.connect(self._changed)
        self.group.textChanged.connect(self._changed)
        self.quick_phrase.textChanged.connect(self._changed)
        self._refresh()

    def _refresh(self):
        self.rules.setEnabled(not self.quick_phrase.text().strip())
        self.group.setEnabled(self.dedupe.currentData() == "group")
        text = {
            "generic_line": "每个文字行独立判断；不会跨行拼出关键词。",
            "text_block": "只圈一个逻辑文本块，多行会合并判断；请勿把独立条目放在同一区域。",
            "wechat_chat": "按聊天气泡判断，排除图片、卡片等内容。",
        }[self.mode.currentData()]
        if self.title_policy.currentData() == "window":
            text += " 切换页面后也会监测此窗口新显示的内容。"
        if self.reminder_missing():
            text += " 当前还没有有效提醒条件；填写上方短语或选择已启用的规则组。"
        self.hint.setText(text)

    def _changed(self, *_):
        self._refresh()
        self.changed.emit()

    def values(self):
        seconds = self.interval.value()
        if 0 < seconds < 10:
            raise ValueError("任务采样间隔须为 10–300 秒，或使用默认值")
        if self.dedupe.currentData() == "group" and not self.group.text().strip():
            raise ValueError("请填写去重组名称")
        group_id = self._group_id if self.quick_phrase.text().strip() else self.rules.currentData()
        return dict(content_mode=self.mode.currentData(), rule_group_id=group_id,
                    title_policy=self.title_policy.currentData(), poll_interval_ms=seconds * 1000,
                    dedupe_scope=self.dedupe.currentData(), dedupe_group=self.group.text().strip())

    def draft_rule(self):
        text = self.quick_phrase.text().strip()
        if not text:
            return None
        return RuleGroup(name=text, id=self._group_id,
                         conditions=[RuleCondition(text, id=self._condition_id)])

    def reminder_missing(self):
        if not self._known_config or self.quick_phrase.text().strip():
            return False
        selected = self.rules.currentData()
        return selected not in self._active_groups if selected else not self._has_keywords
