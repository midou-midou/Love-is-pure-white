"""条件优先的完整卡选择：常驻 → 状态/场景规则 → 背景与示例 → 预算。

硬条件先于相似度：不满足关系/场景/媒介/状态条件的卡一律排除，未知不等于独处。
向量分数只用于背景与示例排序，绝不决定状态卡是否存在。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.enums import RelationshipStatus
from sana_bot.ingest.package_models import CardCondition, CardType, ReferenceCard
from sana_bot.ports import SelectionEvidence
from sana_bot.retrieval.vector_store import StructuralVectorIndex, cosine_scores
from sana_bot.state.models import StateSnapshot

__all__ = [
    "DECISION_READY_WITH_REFERENCES",
    "DECISION_READY_WITHOUT_REFERENCES",
    "DECISION_AMBIGUOUS_REFERENCE",
    "DECISION_PROCESSING_ERROR",
    "DECISION_PACKAGE_UNAVAILABLE",
    "DECISION_RELATIONSHIP_MISMATCH",
    "DECISION_CONTEXT_OVER_LIMIT",
    "PackageSelection",
    "PackageSelector",
    "condition_allows",
    "state_card_ids",
]

DECISION_READY_WITH_REFERENCES = "ready_with_references"
DECISION_READY_WITHOUT_REFERENCES = "ready_without_references"
DECISION_AMBIGUOUS_REFERENCE = "ambiguous_reference"
DECISION_PROCESSING_ERROR = "processing_error"
DECISION_PACKAGE_UNAVAILABLE = "package_unavailable"
DECISION_RELATIONSHIP_MISMATCH = "package_relationship_mismatch"
DECISION_CONTEXT_OVER_LIMIT = "context_over_limit"

#: 只有这些决策可以继续生成
GENERATABLE_DECISIONS = frozenset(
    {
        DECISION_READY_WITH_REFERENCES,
        DECISION_READY_WITHOUT_REFERENCES,
        DECISION_AMBIGUOUS_REFERENCE,
    }
)

#: 背景先于示例：示例整卡最多 2 张
_TYPE_ORDER: Mapping[CardType, int] = {
    CardType.SCENE_RULE: 0,
    CardType.BACKGROUND: 1,
    CardType.APPEARANCE: 1,
    CardType.PROFILE: 1,
    CardType.DIALOGUE_EXAMPLE: 2,
}


@dataclass(frozen=True)
class PackageSelection:
    decision: str
    reason_code: str | None = None
    residents: tuple[str, ...] = ()
    rule_cards: tuple[ReferenceCard, ...] = ()
    supplements: tuple[ReferenceCard, ...] = ()
    scores: Mapping[str, float] = field(default_factory=dict)
    evidence: tuple[SelectionEvidence, ...] = ()
    ambiguity: Mapping[str, Any] | None = None
    effective_state: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def generatable(self) -> bool:
        return self.decision in GENERATABLE_DECISIONS

    @property
    def example_cards(self) -> tuple[ReferenceCard, ...]:
        return tuple(
            card for card in self.supplements if card.type is CardType.DIALOGUE_EXAMPLE
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "residents": list(self.residents),
            "rule_cards": [card.card_id for card in self.rule_cards],
            "supplements": [card.card_id for card in self.supplements],
            "scores": {key: round(value, 4) for key, value in self.scores.items()},
            "evidence": [item.to_payload() for item in self.evidence],
            "ambiguity": dict(self.ambiguity) if self.ambiguity else None,
            "effective_state": self.effective_state,
            "details": dict(self.details),
        }


def condition_allows(
    condition: CardCondition | None,
    *,
    snapshot: StateSnapshot,
    medium: str,
) -> bool:
    """条件判断：unknown 不满足任何明确限制；通用规则仍可保守使用。"""
    if condition is None:
        return True
    return condition.accepts(
        relationship=snapshot.relationship_status.value,
        scene=snapshot.scene.value,
        medium=medium,
        state_tags=tuple(snapshot.secondary_tags),
    )


def state_card_ids(
    catalog: Mapping[str, Any], *, label: str, tags: Sequence[str]
) -> tuple[str, ...]:
    """把当前有效状态（标签或二级标签）映射到批准目录中的状态与卡。"""
    states = catalog.get("states") or (catalog.get("state_catalog") or {}).get("states") or ()
    matched: list[str] = []
    label = (label or "").strip()
    tag_set = {str(tag) for tag in tags}
    for state in states:
        state_label = str(state.get("label", ""))
        state_tags = {str(tag) for tag in state.get("tags", ())}
        if label and state_label == label:
            matched.append(str(state.get("card_id") or state.get("id")))
            continue
        if tag_set and state_tags & tag_set:
            matched.append(str(state.get("card_id") or state.get("id")))
    return tuple(dict.fromkeys(item for item in matched if item))


class PackageSelector:
    """在新格式索引上执行条件优先选择；所有决定都留下证据。"""

    def __init__(
        self,
        *,
        threshold: float | None = None,
        budgets: Mapping[str, Any] | None = None,
        count_tokens: Callable[[str], int] | None = None,
    ) -> None:
        self.threshold = threshold
        self.budgets = dict(budgets or {})
        self.count_tokens = count_tokens or (lambda text: len(text))

    # ------------------------------------------------------------------ 入口

    def select(
        self,
        *,
        index: StructuralVectorIndex,
        snapshot: StateSnapshot,
        query_vector: Sequence[float] | None = None,
        medium: str = "text",
        required_relationship: str = "romantic",
        history_tokens: int = 0,
        query_tokens: int = 0,
    ) -> PackageSelection:
        residents = index.residents
        required_resident_ids = tuple(index.policy.get("resident_ids") or ()) or tuple(
            item.resident_id for item in residents
        )
        present = {item.resident_id for item in residents}
        missing = [item for item in required_resident_ids if item not in present]
        if missing:
            return PackageSelection(
                decision=DECISION_PACKAGE_UNAVAILABLE,
                reason_code="resident_missing",
                details={"missing_residents": missing},
            )

        if (
            required_relationship
            and required_relationship != "any"
            and snapshot.relationship_status is not RelationshipStatus.ROMANTIC
            and required_relationship == "romantic"
        ):
            return PackageSelection(
                decision=DECISION_RELATIONSHIP_MISMATCH,
                reason_code="package_relationship_mismatch",
                residents=tuple(item.resident_id for item in residents),
                details={
                    "required_relationship": required_relationship,
                    "actual": snapshot.relationship_status.value,
                },
            )

        conditions: Mapping[str, CardCondition] = index.policy.get("card_conditions") or {}
        scores = self._scores(index, query_vector)
        evidence: list[SelectionEvidence] = []
        effective_state = snapshot.dominant_state or ""
        wanted_states = state_card_ids(
            index.catalog, label=effective_state, tags=snapshot.secondary_tags
        )

        rule_cards: list[ReferenceCard] = []
        excluded_state: list[str] = []
        for card in index.cards:
            if card.type is CardType.EMOTION_POLICY:
                continue
            if card.type is not CardType.EMOTION_RULE:
                continue
            condition_ok = condition_allows(
                conditions.get(card.card_id), snapshot=snapshot, medium=medium
            )
            state_match = card.card_id in wanted_states
            if not condition_ok:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=False,
                        selected=False,
                        score=scores.get(card.card_id),
                        reason="condition_rejected",
                        state_match=state_match,
                    )
                )
                if state_match:
                    excluded_state.append(card.card_id)
                continue
            if not state_match and card.card_id != "sana_emotion_calm":
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=scores.get(card.card_id),
                        reason="state_not_effective",
                    )
                )
                continue
            rule_cards.append(card)
            evidence.append(
                SelectionEvidence(
                    card_id=card.card_id,
                    card_type=card.type.value,
                    condition_ok=True,
                    selected=True,
                    score=scores.get(card.card_id),
                    reason="state_card_selected" if state_match else "baseline_state_selected",
                    state_match=state_match,
                )
            )

        scene_candidates: list[tuple[ReferenceCard, float]] = []
        background_candidates: list[tuple[ReferenceCard, float]] = []
        example_candidates: list[tuple[ReferenceCard, float]] = []
        for card in index.cards:
            if card.type not in _TYPE_ORDER:
                continue
            condition_ok = condition_allows(
                conditions.get(card.card_id), snapshot=snapshot, medium=medium
            )
            score = scores.get(card.card_id, 0.0)
            if not condition_ok:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=False,
                        selected=False,
                        score=score,
                        reason="condition_rejected",
                    )
                )
                continue
            if card.type is CardType.SCENE_RULE:
                scene_candidates.append((card, score))
            elif card.type is CardType.DIALOGUE_EXAMPLE:
                example_candidates.append((card, score))
            else:
                background_candidates.append((card, score))

        threshold = self.threshold
        max_examples = int(self.budgets.get("max_example_cards", 2))
        max_supplements = int(self.budgets.get("max_supplements", 8))

        selected: list[ReferenceCard] = []
        ambiguity: dict[str, Any] | None = None

        ranked_scenes = sorted(scene_candidates, key=lambda item: (-item[1], item[0].card_id))
        scene_taken = 0
        for card, score in ranked_scenes:
            if threshold is not None and score < threshold:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=score,
                        reason="below_threshold",
                    )
                )
                continue
            if len(selected) >= max_supplements:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=score,
                        reason="budget_excluded",
                        budget_excluded=True,
                    )
                )
                continue
            selected.append(card)
            scene_taken += 1
            evidence.append(
                SelectionEvidence(
                    card_id=card.card_id,
                    card_type=card.type.value,
                    condition_ok=True,
                    selected=True,
                    score=score,
                    reason="scene_rule_selected",
                )
            )

        ranked_background = sorted(
            background_candidates, key=lambda item: (-item[1], item[0].card_id)
        )
        for card, score in ranked_background:
            if threshold is None or score < threshold:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=score,
                        reason="below_threshold",
                    )
                )
                continue
            if len(selected) >= max_supplements:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=score,
                        reason="budget_excluded",
                        budget_excluded=True,
                    )
                )
                continue
            selected.append(card)
            evidence.append(
                SelectionEvidence(
                    card_id=card.card_id,
                    card_type=card.type.value,
                    condition_ok=True,
                    selected=True,
                    score=score,
                    reason="background_selected",
                )
            )

        ranked_examples = sorted(
            example_candidates, key=lambda item: (-item[1], item[0].card_id)
        )
        examples_taken = 0
        for card, score in ranked_examples:
            if threshold is None or score < threshold:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=score,
                        reason="below_threshold",
                    )
                )
                continue
            if examples_taken >= max_examples:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=score,
                        reason="budget_excluded",
                        budget_excluded=True,
                    )
                )
                continue
            if len(selected) >= max_supplements:
                evidence.append(
                    SelectionEvidence(
                        card_id=card.card_id,
                        card_type=card.type.value,
                        condition_ok=True,
                        selected=False,
                        score=score,
                        reason="budget_excluded",
                        budget_excluded=True,
                    )
                )
                continue
            selected.append(card)
            examples_taken += 1
            evidence.append(
                SelectionEvidence(
                    card_id=card.card_id,
                    card_type=card.type.value,
                    condition_ok=True,
                    selected=True,
                    score=score,
                    reason="example_selected",
                )
            )

        if excluded_state:
            ambiguity = {
                "code": "state_card_condition_conflict",
                "message": "当前有效状态卡的条件与场景不符，改用常驻规则的保守表达",
                "card_ids": excluded_state,
            }

        # 生成预算：常驻、状态卡、本轮消息不可裁剪；先移除示例，再依次移除背景与场景
        selected, budget_overflow = self._apply_budget(
            index=index,
            residents=tuple(item.raw_text for item in residents),
            rule_cards=rule_cards,
            supplements=selected,
            evidence=evidence,
            history_tokens=history_tokens,
            query_tokens=query_tokens,
        )
        if budget_overflow:
            return PackageSelection(
                decision=DECISION_CONTEXT_OVER_LIMIT,
                reason_code="context_over_limit",
                residents=tuple(item.resident_id for item in residents),
                rule_cards=tuple(rule_cards),
                evidence=tuple(evidence),
                effective_state=effective_state,
                details={"limit": self.budgets.get("max_prompt_tokens")},
            )

        supplements = tuple(
            sorted(selected, key=lambda card: (_TYPE_ORDER.get(card.type, 9), -scores.get(card.card_id, 0.0)))
        )
        if ambiguity is not None:
            decision = DECISION_AMBIGUOUS_REFERENCE
        elif supplements:
            decision = DECISION_READY_WITH_REFERENCES
        else:
            decision = DECISION_READY_WITHOUT_REFERENCES
        return PackageSelection(
            decision=decision,
            residents=tuple(item.resident_id for item in residents),
            rule_cards=tuple(rule_cards),
            supplements=supplements,
            scores={key: scores[key] for key in scores},
            evidence=tuple(evidence),
            ambiguity=ambiguity,
            effective_state=effective_state,
            details={
                "threshold": threshold,
                "scene_rules": scene_taken,
                "background": sum(
                    1 for card in supplements if card.type is not CardType.DIALOGUE_EXAMPLE
                ),
                "examples": examples_taken,
                "budget_excluded": [
                    item.card_id for item in evidence if item.budget_excluded
                ],
            },
        )

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _scores(
        index: StructuralVectorIndex, query_vector: Sequence[float] | None
    ) -> dict[str, float]:
        if query_vector is None:
            return {card_id: 0.0 for card_id in index.card_ids}
        raw = cosine_scores(query_vector, index.matrix)
        return {
            card_id: float(raw[position])
            for position, card_id in enumerate(index.card_ids)
        }

    def _apply_budget(
        self,
        *,
        index: StructuralVectorIndex,
        residents: Sequence[str],
        rule_cards: Sequence[ReferenceCard],
        supplements: Sequence[ReferenceCard],
        evidence: list[SelectionEvidence],
        history_tokens: int,
        query_tokens: int,
    ) -> tuple[list[ReferenceCard], bool]:
        limit = int(self.budgets.get("max_prompt_tokens", 4096))
        reserved = int(self.budgets.get("reserved_output_tokens", 320))
        fixed = sum(self.count_tokens(text) for text in residents)
        fixed += sum(self.count_tokens(card.raw_text) for card in rule_cards)
        fixed += history_tokens + query_tokens + reserved
        if fixed > limit:
            return list(supplements), True
        remaining = limit - fixed
        kept: list[ReferenceCard] = []
        # 逆序尝试：先放弃示例，再背景，最后场景
        priority = sorted(
            supplements,
            key=lambda card: (_TYPE_ORDER.get(card.type, 9), card.card_id),
        )
        for card in priority:
            cost = self.count_tokens(card.raw_text)
            if cost <= remaining:
                kept.append(card)
                remaining -= cost
                continue
            for position, item in enumerate(evidence):
                if item.card_id == card.card_id and item.selected:
                    evidence[position] = SelectionEvidence(
                        card_id=item.card_id,
                        card_type=item.card_type,
                        condition_ok=item.condition_ok,
                        selected=False,
                        score=item.score,
                        reason="budget_excluded",
                        state_match=item.state_match,
                        budget_excluded=True,
                    )
                    break
        return kept, False
