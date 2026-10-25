"""好感度结算：仅 confirmed normal delivery 计入，按 delivery_id 幂等。

三类贡献可叠加：关键词最长非重叠命中次数、亚洲/上海接收日期的新聊天日、本轮
快照主情绪为高兴。计分事务失败可按 delivery_id 重做，不重复发送也不重复加分。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sana_bot.config import RuntimeConfig
from sana_bot.enums import DeliveryStatus, OutboundKind, PrimaryMood, RelationshipStatus
from sana_bot.ports import SettlementResult
from sana_bot.state.relationship import RelationshipService
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.relationship_repository import RelationshipRepository
from sana_bot.storage.state_repository import StateRepository
from sana_bot.timeutil import chat_day, to_utc_iso, utc_now

__all__ = ["AffectionService", "KeywordMatcher", "normalize_text"]


def normalize_text(text: str) -> str:
    """归一化：全角转半角、大小写统一、去掉多余空白（保留字符位置对齐信息由调用方处理）。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return normalized


@dataclass(frozen=True)
class KeywordMatcher:
    """最长非重叠匹配；重复词在不同位置分别计数。"""

    keywords: tuple[str, ...]

    def hits(self, text: str) -> list[tuple[int, int]]:
        if not self.keywords or not text:
            return []
        normalized = normalize_text(text)
        candidates: list[tuple[int, int]] = []
        for keyword in self.keywords:
            needle = normalize_text(keyword)
            if not needle:
                continue
            for match in re.finditer(re.escape(needle), normalized):
                candidates.append((match.start(), match.end()))
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        accepted: list[tuple[int, int]] = []
        occupied: list[tuple[int, int]] = []
        for start, end in sorted(candidates, key=lambda item: (-(item[1] - item[0]), item[0])):
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            accepted.append((start, end))
            occupied.append((start, end))
        return sorted(accepted)

    def count(self, text: str) -> int:
        return len(self.hits(text))


class AffectionService:
    """实现 :class:`sana_bot.ports.AffectionService`。"""

    def __init__(
        self,
        *,
        database: Database,
        config: RuntimeConfig,
        deliveries: DeliveryRepository,
        inbound: InboundRepository,
        relationships: RelationshipRepository,
        snapshots: StateRepository,
        relationship_service: RelationshipService | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.deliveries = deliveries
        self.inbound = inbound
        self.relationships = relationships
        self.snapshots = snapshots
        self.relationship_service = relationship_service or RelationshipService(
            relationships, config=config
        )
        self.matcher = KeywordMatcher(config.keywords)

    def settle_delivery(self, *, delivery_id: str, correlation_id: str | None = None) -> SettlementResult:
        delivery = self.deliveries.delivery(delivery_id)
        if delivery is None:
            return SettlementResult(
                applied=False, reason_code="delivery_missing", correlation_id=correlation_id
            )
        existing = self.relationships.event_for_delivery(delivery_id)
        if existing is not None:
            # 幂等：同一 delivery 只结算一次
            return SettlementResult(
                applied=True,
                delta=float(existing["delta"]),
                score_after=float(existing["score_after"]),
                relationship_after=RelationshipStatus(str(existing["romance_after"])),
                reason_code="already_settled",
                correlation_id=correlation_id,
            )
        if delivery.status is not DeliveryStatus.CONFIRMED or delivery.kind is not OutboundKind.NORMAL:
            return SettlementResult(
                applied=False,
                reason_code="not_eligible",
                correlation_id=correlation_id,
                details={"status": delivery.status.value, "kind": delivery.kind.value},
            )
        turn_id = delivery.turn_id
        if turn_id is None:
            return SettlementResult(
                applied=False, reason_code="turn_missing", correlation_id=correlation_id
            )
        turn = self.inbound.turn_row(turn_id)
        if turn is None:
            return SettlementResult(
                applied=False, reason_code="turn_missing", correlation_id=correlation_id
            )
        inbound_row = self.inbound.inbound_row(turn.inbound_id)
        if inbound_row is None:
            return SettlementResult(
                applied=False, reason_code="inbound_missing", correlation_id=correlation_id
            )
        user_id = inbound_row.user_id
        day = inbound_row.chat_day or chat_day(inbound_row.received_at).isoformat()
        snapshot = self.snapshots.snapshot_for_turn(turn_id)
        hits = self.matcher.count(turn.input_text)
        happy = bool(snapshot and snapshot.primary_mood is PrimaryMood.HAPPY)

        with self.database.write():
            self.relationships.ensure(user_id)
            current = self.relationships.get(user_id)
            new_day = self.relationships.register_dialogue_day(
                user_id=user_id, local_date=day, turn_id=turn_id
            )
            weights = self.config.affection_weights
            delta = (
                hits * weights.keyword
                + (weights.day if new_day else 0.0)
                + (weights.happy if happy else 0.0)
            )
            decision = self.relationship_service.decide(user_id, score_after=current.score + delta)
            updated = self.relationships.update_score(
                user_id=user_id,
                delta=delta,
                keyword_hits=hits,
                new_day=new_day,
                happy_delta=happy,
                romance_status=decision.after,
                config_version=self.config.version,
            )
            self.relationships.insert_event(
                delivery_id=delivery_id,
                turn_id=turn_id,
                user_id=user_id,
                day=day,
                keyword_hits=hits,
                new_day=new_day,
                happy_delta=happy,
                weights=weights.to_payload(),
                delta=delta,
                score_before=current.score,
                score_after=updated.score,
                romance_before=current.romance_status,
                romance_after=updated.romance_status,
                config_version=self.config.version,
            )
        return SettlementResult(
            applied=True,
            delta=delta,
            score_after=updated.score,
            relationship_after=updated.romance_status,
            reason_code="settled",
            correlation_id=correlation_id,
            details={
                "keyword_hits": hits,
                "new_day": new_day,
                "happy": happy,
                "day": day,
                "weights": weights.to_payload(),
                "config_version": self.config.version,
            },
        )


def settlement_payload(result: SettlementResult) -> dict[str, object]:
    return {
        "applied": result.applied,
        "delta": result.delta,
        "score_after": result.score_after,
        "relationship_after": result.relationship_after.value,
        "reason_code": result.reason_code,
        "details": dict(result.details),
    }


def now_stamp() -> str:
    return to_utc_iso(utc_now())
