"""候选硬过滤：关系/场景/状态/角色/类型先筛，再打分。

禁区（negative）与标注型说明不参与召回；过滤后最多保留 8 个语义单元，
并保留必要的问答关联与来源位置。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.ingest.models import SemanticUnit
from sana_bot.ports import RetrievedCandidate
from sana_bot.state.models import StateSnapshot

__all__ = [
    "MAX_CANDIDATES",
    "EXCLUDED_CONTENT_TYPES",
    "eligible_units",
    "to_candidates",
    "specificity_of",
]

#: 召回上限：过滤后最多 8 个语义单元
MAX_CANDIDATES = 8

#: 禁区与标注不得作为回复召回内容
EXCLUDED_CONTENT_TYPES = frozenset({"negative", "output_rule"})


def _conditions(unit: SemanticUnit) -> dict[str, Any]:
    return dict(unit.conditions)


def _matches_relationship(conditions: dict[str, Any], relationship: RelationshipStatus) -> bool:
    wanted = str(conditions.get("relationship", "any"))
    if wanted in ("any", ""):
        return True
    if wanted == "romantic":
        return relationship is RelationshipStatus.ROMANTIC
    if wanted == "non_romantic":
        return relationship is RelationshipStatus.NON_ROMANTIC
    return False


def _matches_scene(conditions: dict[str, Any], scene: Scene) -> bool:
    wanted = str(conditions.get("scene", "any"))
    if wanted in ("any", ""):
        return True
    if wanted == "private":
        # 独处限定只在明确私密场景生效
        return scene is Scene.PRIVATE
    if wanted == "public":
        return scene is not Scene.PRIVATE
    return True


def _matches_moods(conditions: dict[str, Any], mood: PrimaryMood) -> bool:
    moods = conditions.get("primary_moods") or ()
    if not moods:
        return True
    return mood.value in {str(item) for item in moods}


def _matches_tags(conditions: dict[str, Any], tags: Sequence[str]) -> bool:
    wanted = {str(item) for item in (conditions.get("secondary_tags") or ())}
    if not wanted:
        return True
    return bool(wanted & set(tags))


def eligible_units(
    units: Sequence[SemanticUnit],
    *,
    snapshot: StateSnapshot,
    speaker_only: bool = False,
) -> tuple[SemanticUnit, ...]:
    """按适用条件硬过滤；只要有明确限制就必须全部满足。"""
    eligible: list[SemanticUnit] = []
    for unit in units:
        types = {str(item) for item in unit.content_types}
        if types & EXCLUDED_CONTENT_TYPES:
            continue
        conditions = _conditions(unit)
        if conditions.get("annotation"):
            continue
        if not _matches_relationship(conditions, snapshot.relationship_status):
            continue
        if not _matches_scene(conditions, snapshot.scene):
            continue
        if not _matches_moods(conditions, snapshot.primary_mood):
            continue
        if not _matches_tags(conditions, snapshot.secondary_tags):
            continue
        if speaker_only and "sana" not in set(unit.speaker_labels) and unit.speaker_labels:
            continue
        eligible.append(unit)
    return tuple(eligible)


def specificity_of(unit: SemanticUnit) -> int:
    """更具体 = 明确限制更多（关系、场景、心情、二级标签）。"""
    conditions = _conditions(unit)
    score = 0
    if str(conditions.get("relationship", "any")) not in ("any", ""):
        score += 1
    if str(conditions.get("scene", "any")) not in ("any", ""):
        score += 1
    if conditions.get("primary_moods"):
        score += 1
    if conditions.get("secondary_tags"):
        score += 1
    return score


def to_candidates(
    scored: Sequence[tuple[SemanticUnit, float]],
    *,
    limit: int = MAX_CANDIDATES,
    include_context_links: bool = True,
) -> tuple[RetrievedCandidate, ...]:
    """按分数排序并补齐必要的上下文关联；保留来源位置。"""
    ordered = sorted(scored, key=lambda item: (-item[1], item[0].id))
    selected = ordered[:limit]
    selected_ids = {unit.id for unit, _score in selected}
    candidates: list[RetrievedCandidate] = []
    for unit, score in selected:
        links = tuple(
            link
            for link in unit.context_links
            if include_context_links
        )
        candidates.append(
            RetrievedCandidate(
                unit_id=unit.id,
                score=float(score),
                source_span_ids=unit.source_span_ids,
                topic=unit.topic,
                context_links=links,
                speaker_labels=unit.speaker_labels,
                primary_moods=tuple(str(item) for item in unit.conditions.get("primary_moods", ())),
                secondary_tags=tuple(
                    str(item) for item in unit.conditions.get("secondary_tags", ())
                ),
                relationship=str(unit.conditions.get("relationship", "any")),
                scene=str(unit.conditions.get("scene", "any")),
                content_types=tuple(str(item) for item in unit.content_types),
                text=unit.text,
                source_node_ids=unit.source_node_ids,
                conditions={
                    **dict(unit.conditions),
                    "specificity": specificity_of(unit),
                    "linked_units": list(links),
                    "linked_selected": [item for item in links if item in selected_ids],
                },
            )
        )
    return tuple(candidates)
