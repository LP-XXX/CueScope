"""Pure basic-rule evaluation and explanations, shared by preview and monitoring."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from .rule_config import MAX_RULE_INPUT_LENGTH, RuleCondition, RuleGroup


@dataclass(slots=True)
class ConditionResult:
    group_id: str
    condition_id: str
    mode: str
    label: str
    is_exclusion: bool
    matched: bool
    normalized_text: str
    normalized_expected: str


@dataclass(slots=True)
class RuleEvaluation:
    should_notify: bool = False
    reason: str = "not_matched"
    matched_labels: list[str] = field(default_factory=list)
    matched_group_ids: list[str] = field(default_factory=list)
    excluded_by: list[str] = field(default_factory=list)
    condition_results: list[ConditionResult] = field(default_factory=list)
    error: str = ""


def normalize_for_condition(text: str, condition: RuleCondition) -> str:
    """NFKC, optional case folding, then the explicitly selected space policy."""
    value = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    if not condition.case_sensitive:
        value = value.casefold()
    if condition.mode == "char_multiset":
        return "".join(
            character for character in value
            if unicodedata.category(character)[0] in {"L", "N", "M"}
        )
    if condition.whitespace == "collapse":
        return " ".join(value.split())
    if condition.whitespace == "remove":
        return "".join(character for character in value if not character.isspace())
    return value


def evaluate_groups(text: str, groups: Iterable[RuleGroup]) -> RuleEvaluation:
    """Task exclusions win across selected groups; positive groups are OR-ed.

    Each call sees exactly one content unit, never an entire collection of messages.
    Disabled groups and conditions are not evaluated. No network, regex or code execution.
    """
    result = RuleEvaluation()
    if not isinstance(text, str) or len(text) > MAX_RULE_INPUT_LENGTH:
        result.reason = "rule_input_too_large"
        result.error = f"单条规则输入不能超过 {MAX_RULE_INPUT_LENGTH} 个字符"
        return result
    enabled = [group for group in groups if group.enabled]
    normalized: dict[tuple[str, bool, str], str] = {}
    counters: dict[tuple[str, bool, str], Counter[str]] = {}

    def check(group: RuleGroup, condition: RuleCondition, exclusion: bool) -> ConditionResult:
        key = (condition.mode, condition.case_sensitive, condition.whitespace)
        if key not in normalized:
            normalized[key] = normalize_for_condition(text, condition)
        actual = normalized[key]
        expected = normalize_for_condition(condition.text, condition)
        if condition.mode == "char_multiset":
            if key not in counters:
                counters[key] = Counter(actual)
            matched = bool(expected) and all(
                counters[key][character] >= count
                for character, count in Counter(expected).items()
            )
        elif condition.mode == "exact":
            matched = actual == expected
        else:
            matched = bool(expected) and expected in actual
        return ConditionResult(
            group.id, condition.id, condition.mode, condition.display_label,
            exclusion, matched, actual, expected,
        )

    # Exclusion is a task-level veto. It cannot be bypassed by another matching group.
    for group in enabled:
        for condition in group.exclusions:
            if not condition.enabled:
                continue
            checked = check(group, condition, True)
            result.condition_results.append(checked)
            if checked.matched and checked.label not in result.excluded_by:
                result.excluded_by.append(checked.label)
    if result.excluded_by:
        result.reason = "excluded_by_rule"
        return result

    for group in enabled:
        checked = [check(group, condition, False) for condition in group.conditions if condition.enabled]
        result.condition_results.extend(checked)
        passed = bool(checked) and (
            all(item.matched for item in checked) if group.combination == "all"
            else any(item.matched for item in checked)
        )
        if not passed:
            continue
        result.matched_group_ids.append(group.id)
        for item in checked:
            if item.matched and item.label not in result.matched_labels:
                result.matched_labels.append(item.label)
    result.should_notify = bool(result.matched_group_ids)
    result.reason = "should_notify" if result.should_notify else "not_matched"
    return result
