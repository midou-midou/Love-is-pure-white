"""状态快照模型：一轮冻结同一快照，短句与二级标签保持一致。

高兴/开心/high 统一为 ``happy``；主情绪仅 neutral/happy/angry/sad。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene

__all__ = ["StateSnapshot", "normalize_mood", "MOOD_ALIASES", "empty_snapshot"]

#: 别名统一：高兴/开心/高 → happy，不引入额外枚举
MOOD_ALIASES: Mapping[str, PrimaryMood] = {
    "neutral": PrimaryMood.NEUTRAL,
    "平静": PrimaryMood.NEUTRAL,
    "常态": PrimaryMood.NEUTRAL,
    "happy": PrimaryMood.HAPPY,
    "高兴": PrimaryMood.HAPPY,
    "开心": PrimaryMood.HAPPY,
    "high": PrimaryMood.HAPPY,
    "angry": PrimaryMood.ANGRY,
    "生气": PrimaryMood.ANGRY,
    "恼怒": PrimaryMood.ANGRY,
    "sad": PrimaryMood.SAD,
    "难过": PrimaryMood.SAD,
    "伤心": PrimaryMood.SAD,
    "低落": PrimaryMood.SAD,
}


def normalize_mood(value: str | PrimaryMood) -> PrimaryMood:
    if isinstance(value, PrimaryMood):
        return value
    key = str(value).strip().lower()
    if key in MOOD_ALIASES:
        return MOOD_ALIASES[key]
    raise ValueError(f"无法识别的主情绪：{value!r}")


@dataclass(frozen=True)
class StateSnapshot:
    """一轮使用的冻结状态；回复与开心计分共用同一快照。"""

    id: str
    turn_id: str
    conversation_id: str
    relationship_status: RelationshipStatus = RelationshipStatus.NON_ROMANTIC
    affection_before: float = 0.0
    primary_mood: PrimaryMood = PrimaryMood.NEUTRAL
    secondary_tags: tuple[str, ...] = ()
    dominant_state: str = "平稳常态"
    scene: Scene = Scene.UNKNOWN
    catalog_version: str = ""
    transition_evidence: tuple[Mapping[str, Any], ...] = ()
    created_at: str = ""
    source: str = "initial"

    @property
    def is_happy(self) -> bool:
        return self.primary_mood is PrimaryMood.HAPPY

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "relationship_status": self.relationship_status.value,
            "affection_before": self.affection_before,
            "primary_mood": self.primary_mood.value,
            "secondary_tags": list(self.secondary_tags),
            "dominant_state": self.dominant_state,
            "scene": self.scene.value,
            "catalog_version": self.catalog_version,
            "transition_evidence": [dict(item) for item in self.transition_evidence],
            "created_at": self.created_at,
            "source": self.source,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> StateSnapshot:
        return cls(
            id=str(payload["id"]),
            turn_id=str(payload["turn_id"]),
            conversation_id=str(payload["conversation_id"]),
            relationship_status=RelationshipStatus(
                payload.get("relationship_status", RelationshipStatus.NON_ROMANTIC.value)
            ),
            affection_before=float(payload.get("affection_before", 0.0)),
            primary_mood=PrimaryMood(payload.get("primary_mood", PrimaryMood.NEUTRAL.value)),
            secondary_tags=tuple(payload.get("secondary_tags", ())),
            dominant_state=str(payload.get("dominant_state", "平稳常态")),
            scene=Scene(payload.get("scene", Scene.UNKNOWN.value)),
            catalog_version=str(payload.get("catalog_version", "")),
            transition_evidence=tuple(payload.get("transition_evidence", ())),
            created_at=str(payload.get("created_at", "")),
            source=str(payload.get("source", "initial")),
        )


def empty_snapshot(
    *,
    snapshot_id: str,
    turn_id: str,
    conversation_id: str,
    mood: PrimaryMood = PrimaryMood.NEUTRAL,
    tags: Sequence[str] = (),
    dominant_state: str = "平稳常态",
    scene: Scene = Scene.UNKNOWN,
    relationship: RelationshipStatus = RelationshipStatus.NON_ROMANTIC,
    affection_before: float = 0.0,
    catalog_version: str = "",
    source: str = "initial",
) -> StateSnapshot:
    return StateSnapshot(
        id=snapshot_id,
        turn_id=turn_id,
        conversation_id=conversation_id,
        relationship_status=relationship,
        affection_before=affection_before,
        primary_mood=mood,
        secondary_tags=tuple(tags),
        dominant_state=dominant_state,
        scene=scene,
        catalog_version=catalog_version,
        source=source,
    )
