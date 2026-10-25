"""状态管理：合法的初始心情与二级状态，冻结到下一轮生效。

管理设置只影响后续轮次，不改好感度、不改配置；短期状态可在重启回到 neutral，
长期关系不受影响。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.errors import ConfigError
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.models import StateSnapshot, normalize_mood
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.state_repository import StateRepository
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = ["StateService", "StateOverride"]


@dataclass(frozen=True)
class StateOverride:
    user_id: str
    mood: PrimaryMood
    tags: tuple[str, ...]
    scene: Scene | None
    actor: str = "maintainer"

    def to_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "primary_mood": self.mood.value,
            "secondary_tags": list(self.tags),
            "scene": self.scene.value if self.scene else None,
            "actor": self.actor,
        }


@dataclass
class StateService:
    """管理设置的唯一入口；短期状态保存在内存，不写长期档案。"""

    repository: StateRepository
    reducer: DeterministicStateReducer
    catalog: RuleCatalog
    inbound: InboundRepository
    config: RuntimeConfig = field(default_factory=RuntimeConfig)

    # ------------------------------------------------------------------ 管理

    def allowed_tags(self) -> tuple[str, ...]:
        tags: list[str] = []
        for rule in self.catalog.rules:
            tags.extend(rule.to_tags)
        return tuple(dict.fromkeys(tag for tag in tags if tag))

    def validate(self, *, mood: str | None, tags: Sequence[str], scene: str | None) -> tuple[PrimaryMood | None, tuple[str, ...], Scene | None]:
        parsed_mood: PrimaryMood | None = None
        if mood is not None:
            try:
                parsed_mood = normalize_mood(mood)
            except ValueError as exc:
                raise ConfigError(f"无效主情绪：{mood}", reason_code="invalid_mood") from exc
        allowed = set(self.allowed_tags())
        unknown = [tag for tag in tags if tag not in allowed]
        if unknown:
            raise ConfigError(
                f"无效二级状态：{sorted(unknown)}（合法取值：{sorted(allowed)}）",
                reason_code="invalid_tag",
            )
        parsed_scene: Scene | None = None
        if scene is not None:
            try:
                parsed_scene = Scene(scene)
            except ValueError as exc:
                raise ConfigError(f"无效场景：{scene}", reason_code="invalid_scene") from exc
        return parsed_mood, tuple(tags), parsed_scene

    def set_state(
        self,
        user_id: str,
        *,
        mood: str | None = None,
        tags: Sequence[str] = (),
        scene: str | None = None,
        actor: str = "maintainer",
    ) -> StateOverride:
        if mood is None and not tags and scene is None:
            raise ConfigError("state set 至少需要 --mood/--tag/--scene 之一")
        parsed_mood, parsed_tags, parsed_scene = self.validate(mood=mood, tags=tags, scene=scene)
        previous = self.override_for(user_id)
        override = StateOverride(
            user_id=user_id,
            mood=parsed_mood or (previous.mood if previous else PrimaryMood.NEUTRAL),
            tags=parsed_tags or (previous.tags if previous else ()),
            scene=parsed_scene if parsed_scene is not None else (previous.scene if previous else None),
            actor=actor,
        )
        with self.repository.database.write():
            self.repository.set_override(
                user_id=user_id,
                mood=override.mood.value,
                tags=override.tags,
                scene=override.scene.value if override.scene else None,
                actor=actor,
            )
            self.repository.record_change(
                user_id=user_id,
                action="state.set",
                before=previous.to_payload() if previous else {},
                after=override.to_payload(),
                actor=actor,
            )
        return override

    def override_for(self, user_id: str) -> StateOverride | None:
        row = self.repository.get_override(user_id)
        if row is None:
            return None
        return StateOverride(
            user_id=user_id,
            mood=PrimaryMood(row["mood"]),
            tags=tuple(row["tags"]),
            scene=Scene(row["scene"]) if row["scene"] else None,
            actor=row["actor"],
        )

    def clear(self, user_id: str) -> None:
        with self.repository.database.write():
            self.repository.database.execute(
                "DELETE FROM state_override WHERE user_id = ?", (user_id,)
            )

    def clear_all_overrides(self) -> int:
        """服务启动时清空：重启后短期状态回到 neutral。"""
        with self.repository.database.write():
            return self.repository.clear_overrides()

    # ------------------------------------------------------------------ 读取

    def current(self, user_id: str, conversation_id: str) -> StateSnapshot:
        """下一轮使用的起始状态：管理设置优先，其次上一轮冻结的快照。"""
        override = self.override_for(user_id)
        latest = self.repository.latest_snapshot(conversation_id) if conversation_id else None
        mood = override.mood if override else (latest.primary_mood if latest else PrimaryMood.NEUTRAL)
        tags = override.tags if override else (latest.secondary_tags if latest else ())
        scene = (
            override.scene
            if override and override.scene is not None
            else (latest.scene if latest else Scene.UNKNOWN)
        )
        return StateSnapshot(
            id=uuid.uuid4().hex,
            turn_id=latest.turn_id if latest else "",
            conversation_id=conversation_id,
            relationship_status=(
                latest.relationship_status if latest else RelationshipStatus.NON_ROMANTIC
            ),
            affection_before=latest.affection_before if latest else 0.0,
            primary_mood=mood,
            secondary_tags=tuple(tags),
            dominant_state=latest.dominant_state if latest else "",
            scene=scene,
            catalog_version=self.catalog.version,
            created_at=to_utc_iso(utc_now()),
            source="override" if override else "previous_turn",
        )

    def show(self, user_id: str, conversation_id: str | None = None) -> dict[str, Any]:
        override = self.override_for(user_id)
        latest = (
            self.repository.latest_snapshot(conversation_id) if conversation_id else None
        )
        return {
            "user_id": user_id,
            "override": override.to_payload() if override else None,
            "effective": {
                "primary_mood": (override.mood if override else (latest.primary_mood if latest else PrimaryMood.NEUTRAL)).value,
                "secondary_tags": list(override.tags if override else (latest.secondary_tags if latest else ())),
                "scene": (override.scene.value if override and override.scene else (latest.scene.value if latest else Scene.UNKNOWN.value)),
                "dominant_state": latest.dominant_state if latest else "",
            },
            "last_snapshot": (
                {
                    "id": latest.id,
                    "turn_id": latest.turn_id,
                    "primary_mood": latest.primary_mood.value,
                    "secondary_tags": list(latest.secondary_tags),
                    "scene": latest.scene.value,
                    "created_at": latest.created_at,
                }
                if latest
                else None
            ),
            "catalog_version": self.catalog.version,
            "note": "管理设置不改好感度；重启后短期状态回到 neutral",
        }

    def recent_transitions(self, conversation_id: str, *, limit: int = 3) -> list[Mapping[str, Any]]:
        rows = self.repository.recent_snapshots(conversation_id, limit=limit)
        return [
            {
                "turn_id": row.turn_id,
                "primary_mood": row.primary_mood,
                "secondary_tags": list(row.secondary_tags),
                "created_at": row.created_at,
            }
            for row in rows
        ]
