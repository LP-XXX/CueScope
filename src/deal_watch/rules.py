"""统一消息决策、字面关键词匹配与跨群去重。规则层不依赖 UI、桌面或网络。"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .models import MatchEvent, MessageCandidate, MessageDecision
from .normalize import (
    _transform_outside_urls,
    char_rule_normalize,
    contains_all_chars,
    content_digest,
    match_normalize,
    meaningful_normalize,
)
from .rule_config import RuleGroup
from .rule_evaluation import RuleEvaluation, evaluate_groups
from .storage import Storage

# "最近判定"与"已通知消息"两张表的上限。两者都是 LRU，只保留最近的一批：
# 键是 tracking_id，一条消息一个键，不设上限就是一处随运行时长线性增长的内存。
_MAX_REMEMBERED_DECISIONS = 4096
_MAX_REMEMBERED_NOTIFIED = 4096


@dataclass(frozen=True, slots=True)
class _RegionRulePolicy:
    group_ids: tuple[str, ...] = ()
    dedupe_scope: str = "global"
    dedupe_group: str = ""
    error: str = ""


def _setting(region: Any, name: str, default: Any) -> Any:
    return region.get(name, default) if isinstance(region, dict) else getattr(region, name, default)


class RuleEngine:
    def __init__(
        self,
        storage: Storage,
        keywords: list[str],
        cooldown_seconds: int = 300,
        *,
        match_mode: str = "char_multiset",
        regions: list[Any] | None = None,
        rule_groups: list[RuleGroup] | None = None,
        delivery_mode: str = "feishu",
    ) -> None:
        self.storage = storage
        self.cooldown_seconds = cooldown_seconds
        self.match_mode = match_mode
        if delivery_mode not in {"feishu", "local"}:
            raise ValueError("通知方式必须是 feishu 或 local")
        self.delivery_mode = delivery_mode
        self._region_policies: dict[str, _RegionRulePolicy] = {}
        self._rule_groups: dict[str, RuleGroup] = {}
        self._notified: OrderedDict[str, str] = OrderedDict()
        self.last_decisions: OrderedDict[str, MessageDecision] = OrderedDict()
        self.set_keywords(keywords)
        self.configure_regions(regions or [], rule_groups or [])

    def configure_regions(
        self, regions: list[Any], rule_groups: list[RuleGroup],
    ) -> set[str]:
        """Snapshot data-only settings; return tasks whose rule semantics changed.

        The caller owns task generations and must establish fresh baselines before
        resuming changed tasks. Missing group references fail closed in ``explain``;
        they never silently fall back to the legacy global keywords.
        """
        groups: dict[str, RuleGroup] = {}
        for raw in rule_groups:
            group = RuleGroup.from_dict(raw.to_dict() if isinstance(raw, RuleGroup) else raw)
            if group.id in groups:
                raise ValueError("规则组编号不能重复")
            groups[group.id] = group
        policies: dict[str, _RegionRulePolicy] = {}
        for region in regions:
            region_id = _setting(region, "id", "")
            if not isinstance(region_id, str) or not region_id:
                raise ValueError("监测任务必须有有效编号")
            if region_id in policies:
                raise ValueError("监测任务编号不能重复")
            selected = _setting(region, "rule_group_ids", ())
            single = _setting(region, "rule_group_id", "")
            if not selected:
                selected = (single,) if single else ()
            error = ""
            if (
                not isinstance(selected, list | tuple)
                or any(not isinstance(item, str) or not item for item in selected)
            ):
                selected = ()
                error = "任务的规则组引用无效"
            ids = tuple(dict.fromkeys(selected))
            if any(item not in groups for item in ids):
                error = "任务引用的规则组不存在，请重新选择"
            scope = _setting(region, "dedupe_scope", "global")
            group_name = _setting(region, "dedupe_group", "")
            if not isinstance(scope, str) or scope not in {"global", "task", "group"}:
                error = "去重范围必须是 global、task 或 group"
            if not isinstance(group_name, str):
                group_name = ""
                error = "去重组名称必须是文本"
            group_name = group_name.strip()
            if scope == "group" and not group_name:
                error = "指定去重组时必须填写组名称"
            policies[region_id] = _RegionRulePolicy(ids, scope, group_name, error)
        changed = {
            region_id for region_id in self._region_policies.keys() | policies.keys()
            if self._region_policies.get(region_id) != policies.get(region_id)
            or any(
                self._rule_groups.get(group_id) != groups.get(group_id)
                for group_id in policies.get(region_id, _RegionRulePolicy()).group_ids
            )
        }
        self._rule_groups = groups
        self._region_policies = policies
        return changed

    def has_custom_rules(self, region_id: str) -> bool:
        return bool(self._region_policies.get(region_id, _RegionRulePolicy()).group_ids)

    def explain(self, message: MessageCandidate) -> RuleEvaluation:
        """Return the same decision as runtime with per-condition preview evidence."""
        if message.kind != "normal_text":
            return RuleEvaluation(reason="unsupported_message_type")
        if message.boundary_uncertain:
            return RuleEvaluation(reason="boundary_uncertain")
        policy = self._region_policies.get(message.region_id, _RegionRulePolicy())
        if policy.error:
            return RuleEvaluation(reason="rule_configuration_error", error=policy.error)
        if policy.group_ids:
            return evaluate_groups(message.text, [self._rule_groups[key] for key in policy.group_ids])
        matched = self.matching_keywords(message.text)
        return RuleEvaluation(
            should_notify=bool(matched),
            reason="should_notify" if matched else "not_matched",
            matched_labels=matched,
        )

    def _event_text(self, message: MessageCandidate) -> str:
        """Preserve details required by exact/case-sensitive rules in new tasks."""
        policy = self._region_policies.get(message.region_id, _RegionRulePolicy())
        conditions = [
            condition for group_id in policy.group_ids
            for group in (self._rule_groups[group_id],) if group.enabled
            for condition in [*group.conditions, *group.exclusions] if condition.enabled
        ]
        value = unicodedata.normalize("NFKC", message.text).replace("\r\n", "\n").replace("\r", "\n")
        if not any(condition.case_sensitive for condition in conditions):
            value = _transform_outside_urls(value, str.casefold)
        if not any(condition.mode != "char_multiset" and condition.whitespace == "preserve" for condition in conditions):
            value = " ".join(value.split())
        return value

    def _notification_identity(self, message: MessageCandidate) -> str:
        return self._event_text(message) if self.has_custom_rules(message.region_id) else meaningful_normalize(message.text)

    def _dedupe_key(self, message: MessageCandidate) -> str:
        policy = self._region_policies.get(message.region_id, _RegionRulePolicy())
        if policy.dedupe_scope == "global" and not policy.group_ids:
            return content_digest(message.text)  # Preserve existing persisted cooldowns.
        from .normalize import dedupe_normalize

        canonical = self._event_text(message) if policy.group_ids else dedupe_normalize(message.text)
        if policy.dedupe_scope == "global":
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        namespace = message.region_id if policy.dedupe_scope == "task" else policy.dedupe_group
        payload = json.dumps(
            ["cuescope-text-v1", policy.dedupe_scope, namespace, canonical],
            ensure_ascii=False, separators=(",", ":"),
        )
        # Rule IDs deliberately do not split one content event into several jobs.
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _remember_decision(
        self, tracking_id: str, decision: MessageDecision
    ) -> MessageDecision:
        """记住"最近一次判定"，供调试与界面回看。

        **有界**是硬要求：键是 ``tracking_id``，一条消息一个键，长跑期间只增不减
        就是一处无界内存。这里沿用 :attr:`_notified` 的既有写法（LRU 淘汰），
        语义上它也只是"最近判定"而不是历史归档，有界比无界更贴合本意。
        """
        self.last_decisions[tracking_id] = decision
        self.last_decisions.move_to_end(tracking_id)
        while len(self.last_decisions) > _MAX_REMEMBERED_DECISIONS:
            self.last_decisions.popitem(last=False)
        return decision

    def set_keywords(self, keywords: list[str]) -> None:
        if self.match_mode == "char_multiset":
            normalized = [
                (word, char_rule_normalize(word)) for word in keywords if word.strip()
            ]
        else:
            normalized = [(word, match_normalize(word)) for word in keywords if word.strip()]
        self._keywords = [(original, value) for original, value in normalized if value]

    def matching_keywords(self, text: str) -> list[str]:
        if self.match_mode == "char_multiset":
            return [word for word, _ in self._keywords if contains_all_chars(text, word)]
        normalized = match_normalize(text)
        return [word for word, key in self._keywords if key in normalized]

    def decide(self, message: MessageCandidate) -> MessageDecision:
        """预览、手动发送、回放和自动监控共用的唯一消息判断入口。"""
        evaluation = self.explain(message)
        decision = MessageDecision(
            tracking_id=message.tracking_id,
            char_matches=(
                list(evaluation.matched_labels) if self.has_custom_rules(message.region_id)
                else self.matching_keywords(message.text)
            ),
            matched_keywords=list(evaluation.matched_labels),
            should_notify=evaluation.should_notify,
            reason=evaluation.reason,
        )
        self._remember_decision(message.tracking_id, decision)
        return decision

    def process(
        self,
        messages: list[MessageCandidate],
        region_alias: str,
        *,
        now: float | None = None,
        decisions: dict[str, MessageDecision] | None = None,
    ) -> list[MatchEvent]:
        """把“应通知”决策转换为持久事件；返回仅包含实际新入队的事件。"""
        events: list[MatchEvent] = []
        for message in messages:
            if (
                message.is_update and message.previous_text
                and not self.has_custom_rules(message.region_id)
                and meaningful_normalize(message.text) == meaningful_normalize(message.previous_text)
            ):
                continue
            decision = decisions.get(message.tracking_id) if decisions else None
            policy = self._region_policies.get(message.region_id, _RegionRulePolicy())
            if policy.error or message.kind != "normal_text" or message.boundary_uncertain:
                decision = None
            decision = decision or self.decide(message)
            self._remember_decision(message.tracking_id, decision)
            if not decision.should_notify:
                continue
            meaningful = self._notification_identity(message)
            if self._notified.get(message.tracking_id) == meaningful:
                decision.queue_state = "already_processed"
                continue
            event = MatchEvent(
                region_id=message.region_id,
                region_alias=region_alias,
                keyword="、".join(decision.matched_keywords),
                text=message.text,
                dedupe_key=self._dedupe_key(message),
                observed_at=message.last_seen,
                tracking_id=message.tracking_id,
                matched_keywords=decision.matched_keywords,
                boundary_uncertain=message.boundary_uncertain,
                delivery_mode=self.delivery_mode,
            )
            should_send = self.storage.record_match(
                event, cooldown_seconds=self.cooldown_seconds, now=time.time() if now is None else now
            )
            decision.event_id = event.id
            decision.queue_state = "queued" if should_send else "duplicate"
            self._notified[message.tracking_id] = meaningful
            self._notified.move_to_end(message.tracking_id)
            while len(self._notified) > _MAX_REMEMBERED_NOTIFIED:
                self._notified.popitem(last=False)
            if should_send:
                events.append(event)
        return events
