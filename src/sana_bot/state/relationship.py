"""关系状态维护：按配置门槛判定恋爱，状态不回退、历史不重算。"""

from __future__ import annotations

from dataclasses import dataclass

from sana_bot.config import RuntimeConfig
from sana_bot.enums import RelationshipStatus
from sana_bot.errors import ConfigError
from sana_bot.storage.relationship_repository import RelationshipRepository, RelationshipRow

__all__ = ["RelationshipService", "RomanceDecision"]


@dataclass(frozen=True)
class RomanceDecision:
    before: RelationshipStatus
    after: RelationshipStatus
    threshold: float
    score_after: float

    @property
    def entered_romance(self) -> bool:
        return (
            self.before is RelationshipStatus.NON_ROMANTIC
            and self.after is RelationshipStatus.ROMANTIC
        )


class RelationshipService:
    """只负责状态判定与展示；不改分、不重算历史。"""

    def __init__(
        self, repository: RelationshipRepository, *, config: RuntimeConfig | None = None
    ) -> None:
        self.repository = repository
        self.config = config or RuntimeConfig()

    def get(self, user_id: str) -> RelationshipRow:
        return self.repository.ensure(user_id)

    def decide(self, user_id: str, *, score_after: float) -> RomanceDecision:
        """达到或超过门槛即进入恋爱中；已是恋爱不因门槛提高而回退。"""
        current = self.get(user_id)
        threshold = self.config.romance_threshold
        if threshold <= 0:
            raise ConfigError("恋爱门槛必须为正数", reason_code="config_invalid")
        after = current.romance_status
        if after is not RelationshipStatus.ROMANTIC and score_after >= threshold:
            after = RelationshipStatus.ROMANTIC
        return RomanceDecision(
            before=current.romance_status,
            after=after,
            threshold=threshold,
            score_after=score_after,
        )

    def apply(self, user_id: str, *, score_after: float) -> RomanceDecision:
        decision = self.decide(user_id, score_after=score_after)
        if decision.after is not decision.before:
            self.repository.database.execute(
                "UPDATE relationship SET romance_status = ? WHERE user_id = ?",
                (decision.after.value, user_id),
            )
        return decision

    def snapshot_payload(self, user_id: str) -> dict[str, object]:
        row = self.get(user_id)
        return {
            "user_id": row.user_id,
            "score": row.score,
            "romance_status": row.romance_status.value,
            "keyword_count": row.keyword_count,
            "dialogue_days": row.dialogue_days,
            "happy_count": row.happy_count,
            "config_version": row.version,
            "romance_threshold": self.config.romance_threshold,
        }
