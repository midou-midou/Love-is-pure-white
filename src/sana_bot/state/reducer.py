"""确定性状态归约：已批准事件 + 关系/场景 → 新的主情绪与二级标签。

模型只能提议有限标签；转换是否成立由目录中的已批准规则决定。
冲突保持上一个合法状态并记录 ambiguity，不随意制造人物心事。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.ports import StateReducerResult
from sana_bot.state.catalog import RuleCatalog, StateRule
from sana_bot.state.event_models import StateEvent
from sana_bot.state.models import StateSnapshot

__all__ = ["DeterministicStateReducer", "RuleSelection"]


@dataclass(frozen=True)
class RuleSelection:
    """一次规则选择的结果：唯一 / 无匹配 / 冲突。"""

    rules: tuple[StateRule, ...] = ()
    conflict: str | None = None
    priority: int = 0

    @property
    def unique(self) -> StateRule | None:
        return self.rules[0] if len(self.rules) == 1 else None


class DeterministicStateReducer:
    """实现 :class:`sana_bot.ports.StateReducer`。"""

    def __init__(self, catalog: RuleCatalog) -> None:
        self.catalog = catalog

    def reduce(
        self,
        *,
        current: StateSnapshot,
        events: Sequence[StateEvent],
        relationship: RelationshipStatus,
        scene: Scene,
        correlation_id: str | None = None,
    ) -> StateReducerResult:
        usable = self._usable_events(events)
        if not usable:
            return self._keep(
                current,
                reason_code="no_event",
                correlation_id=correlation_id,
                detail="没有可用事件，保持当前状态",
            )

        recovery = self._recovery(current, usable, relationship=relationship, scene=scene)
        if recovery is not None:
            return recovery

        selection = self.select(
            usable, relationship=relationship, scene=scene, current=current
        )
        if selection.conflict is not None:
            return self._keep(
                current,
                reason_code="conflict",
                correlation_id=correlation_id,
                detail=selection.conflict,
                conflict=selection.conflict,
            )
        rule = selection.unique
        if rule is None:
            return self._keep(
                current,
                reason_code="no_matching_rule",
                correlation_id=correlation_id,
                detail="事件存在但没有已批准规则匹配，保持当前状态",
            )

        evidence = tuple(
            {
                "event_tag": event.tag,
                "source_message_id": event.source_message_id,
                "evidence_start": event.evidence_start,
                "evidence_end": event.evidence_end,
                "quote": event.quote,
            }
            for event in usable
        )
        return StateReducerResult(
            ok=True,
            primary_mood=rule.to_primary,
            secondary_tags=rule.to_tags,
            dominant_state=rule.label,
            applied_rule_ids=(rule.id,),
            transition_evidence=evidence,
            reason_code="transition_applied",
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------ 选择

    def _recovery(
        self,
        current: StateSnapshot,
        events: Sequence[StateEvent],
        *,
        relationship: RelationshipStatus,
        scene: Scene,
    ) -> StateReducerResult | None:
        """安抚后按批准出口逐步缓和；多个出口指向不同目标时保留前态并记歧义。"""
        tags = {event.tag for event in events}
        previous = current.dominant_state
        matched = [
            rule
            for rule in self.catalog.recoveries
            if rule.event_tag in tags and previous in rule.from_states
        ]
        if not matched:
            return None
        targets = {rule.to_state for rule in matched}
        if len(targets) > 1:
            return self._keep(
                current,
                reason_code="conflict",
                correlation_id=None,
                detail=(
                    "多个恢复出口指向不同状态，保留上一个合法状态："
                    + "、".join(sorted(item.event_tag for item in matched))
                ),
                conflict="ambiguous_recovery",
            )
        target_id = matched[0].to_state
        state = next((rule for rule in self.catalog.rules if rule.id == target_id), None)
        if state is None or not state.qualifies(relationship=relationship, scene=scene):
            return self._keep(
                current,
                reason_code="recovery_target_unavailable",
                correlation_id=None,
                detail=f"恢复目标 {target_id} 在当前条件不可用，保留上一个合法状态",
            )
        evidence = tuple(
            {
                "event_tag": event.tag,
                "source_message_id": event.source_message_id,
                "evidence_start": event.evidence_start,
                "evidence_end": event.evidence_end,
                "quote": event.quote,
                "recovery": target_id,
                "previous_state": previous,
            }
            for event in events
            if event.tag in {rule.event_tag for rule in matched}
        )
        return StateReducerResult(
            ok=True,
            primary_mood=state.to_primary,
            secondary_tags=state.to_tags,
            dominant_state=state.label,
            applied_rule_ids=(state.id,),
            transition_evidence=evidence,
            reason_code="recovery_applied",
        )

    def select(
        self,
        events: Sequence[StateEvent],
        *,
        relationship: RelationshipStatus,
        scene: Scene,
        current: StateSnapshot | None = None,
    ) -> RuleSelection:
        tags = {event.tag for event in events}
        previous = current.dominant_state if current is not None else ""
        candidates = [
            rule
            for rule in self.catalog.rules_for(relationship=relationship, scene=scene)
            if rule.event_tags
            and set(rule.event_tags) & tags
            and _from_state_allows(rule.from_states, previous)
        ]
        if not candidates:
            return RuleSelection()
        top = max(rule.dominant_priority for rule in candidates)
        best = [rule for rule in candidates if rule.dominant_priority == top]
        outcomes = {(rule.to_primary, tuple(rule.to_tags)) for rule in best}
        if len(outcomes) > 1:
            return RuleSelection(
                rules=tuple(best),
                conflict=(
                    "同优先级规则给出不同目标状态，保持上一个合法状态："
                    + "、".join(sorted(rule.label for rule in best))
                ),
                priority=top,
            )
        return RuleSelection(rules=(best[0],), priority=top)

    def _usable_events(self, events: Sequence[StateEvent]) -> tuple[StateEvent, ...]:
        """只有批准目录内的标签且证据非空的评论才能参与转换。"""
        return tuple(
            event
            for event in events
            if self.catalog.event_catalog.contains(event.tag) and event.quote
        )

    def _keep(
        self,
        current: StateSnapshot,
        *,
        reason_code: str,
        correlation_id: str | None,
        detail: str = "",
        conflict: str | None = None,
    ) -> StateReducerResult:
        return StateReducerResult(
            ok=True,
            primary_mood=current.primary_mood,
            secondary_tags=current.secondary_tags,
            dominant_state=current.dominant_state,
            applied_rule_ids=(),
            transition_evidence=(),
            conflict=conflict,
            reason_code=reason_code,
            correlation_id=correlation_id,
        )


def describe_transition(result: StateReducerResult) -> Mapping[str, Any]:
    """供审计与诊断使用的简短描述。"""
    return {
        "reason_code": result.reason_code,
        "primary_mood": result.primary_mood.value,
        "secondary_tags": list(result.secondary_tags),
        "dominant_state": result.dominant_state,
        "applied_rule_ids": list(result.applied_rule_ids),
        "conflict": result.conflict,
    }


def mood_rank(mood: PrimaryMood) -> int:
    order = {
        PrimaryMood.NEUTRAL: 0,
        PrimaryMood.HAPPY: 1,
        PrimaryMood.ANGRY: 2,
        PrimaryMood.SAD: 3,
    }
    return order[mood]


def _from_state_allows(from_states: Sequence[str], previous: str) -> bool:
    """空或通配表示任意前态；否则前态必须在批准列表内。

    前态未知时不因缺少依据而随便升级到强烈状态。
    """
    if not from_states or "*" in from_states:
        return True
    if not previous:
        return False
    return previous in from_states
