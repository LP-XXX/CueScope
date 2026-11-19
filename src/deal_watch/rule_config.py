"""Versioned, data-only rule definitions; no desktop, OCR or notification imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

RULE_MODES = frozenset({"contains", "exact", "char_multiset"})
WHITESPACE_MODES = frozenset({"collapse", "preserve", "remove"})
MAX_CONDITIONS_PER_GROUP = 128
MAX_RULE_TEXT_LENGTH = 4096
MAX_RULE_INPUT_LENGTH = 131072


def _identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 128
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label}必须是 1–128 个可见字符")


@dataclass(slots=True)
class RuleCondition:
    """One comparison against one content unit. ``text`` is always literal data."""

    text: str
    mode: str = "contains"
    case_sensitive: bool = False
    whitespace: str = "collapse"
    id: str = field(default_factory=lambda: uuid4().hex)
    label: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        _identifier(self.id, "条件编号")
        if not isinstance(self.mode, str) or self.mode not in RULE_MODES:
            raise ValueError("规则类型必须是 contains、exact 或 char_multiset")
        if not isinstance(self.whitespace, str) or self.whitespace not in WHITESPACE_MODES:
            raise ValueError("空白处理必须是 collapse、preserve 或 remove")
        if not isinstance(self.case_sensitive, bool) or not isinstance(self.enabled, bool):
            raise ValueError("规则开关必须是布尔值")
        if (
            not isinstance(self.text, str) or not self.text.strip()
            or len(self.text) > MAX_RULE_TEXT_LENGTH
        ):
            raise ValueError(f"条件文本须为 1–{MAX_RULE_TEXT_LENGTH} 个字符且不能全为空白")
        if not isinstance(self.label, str) or len(self.label) > 160:
            raise ValueError("条件名称不能超过 160 个字符")
        # A punctuation-only character rule would otherwise match every input.
        if self.mode == "char_multiset":
            import unicodedata

            if not any(
                unicodedata.category(character)[0] in {"L", "N", "M"}
                for character in unicodedata.normalize("NFKC", self.text)
            ):
                raise ValueError("字符组合条件必须包含文字或数字")

    @property
    def display_label(self) -> str:
        return self.label.strip() or self.text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleCondition:
        if not isinstance(data, dict):
            raise ValueError("规则条件必须是对象")
        try:
            return cls(**data)
        except TypeError as error:
            raise ValueError("规则条件包含未知字段或缺少文本") from error


@dataclass(slots=True)
class RuleGroup:
    """Exclusions win, then enabled positive conditions use ANY or ALL."""

    name: str
    conditions: list[RuleCondition]
    combination: str = "any"
    exclusions: list[RuleCondition] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)
    enabled: bool = True

    def __post_init__(self) -> None:
        _identifier(self.id, "规则组编号")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 160:
            raise ValueError("规则组名称须为 1–160 个字符")
        if not isinstance(self.combination, str) or self.combination not in {"any", "all"}:
            raise ValueError("规则组合必须是 any 或 all")
        if not isinstance(self.enabled, bool):
            raise ValueError("规则组开关必须是布尔值")
        if not isinstance(self.conditions, list) or not isinstance(self.exclusions, list):
            raise ValueError("规则条件和排除条件必须是列表")
        if len(self.conditions) + len(self.exclusions) > MAX_CONDITIONS_PER_GROUP:
            raise ValueError(f"每组最多 {MAX_CONDITIONS_PER_GROUP} 个条件（含排除条件）")
        conditions = [*self.conditions, *self.exclusions]
        if any(not isinstance(item, RuleCondition) for item in conditions):
            raise ValueError("规则组只能包含 RuleCondition 条件")
        for item in conditions:
            item.__post_init__()
        ids = [item.id for item in conditions]
        if len(ids) != len(set(ids)):
            raise ValueError("同一规则组的条件编号不能重复")
        if self.enabled and not any(item.enabled for item in self.conditions):
            raise ValueError("启用的规则组至少需要一个启用的正向条件")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleGroup:
        if not isinstance(data, dict):
            raise ValueError("规则组必须是对象")
        values = dict(data)
        for key in ("conditions", "exclusions"):
            raw = values.get(key, [])
            if not isinstance(raw, list):
                raise ValueError("规则条件和排除条件必须是列表")
            values[key] = [RuleCondition.from_dict(item) for item in raw]
        try:
            return cls(**values)
        except TypeError as error:
            raise ValueError("规则组包含未知字段或缺少名称") from error
