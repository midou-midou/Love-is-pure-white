"""独白机会的创建与调度。

仅当活动快照 ``monologue_status=enabled`` 时创建；默认等待 60 秒、概率 30%；
禁用不创建机会且给出具体原因，启用也不补建历史轮次。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.enums import MonologueStatus, RetrievalStage
from sana_bot.errors import SanaError
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.monologue_repository import MonologueRepository, OpportunityRow
from sana_bot.timeutil import utc_now

__all__ = ["MonologueScheduler", "OpportunityCreation"]

#: 每个用户同一时刻只允许一个待发独白
SINGLE_PENDING_REASON = "another_opportunity_pending"


@dataclass(frozen=True)
class OpportunityCreation:
    created: bool
    opportunity_id: str | None = None
    reason_code: str | None = None
    details: dict[str, Any] | None = None


class MonologueScheduler:
    """创建机会的唯一入口。"""

    def __init__(
        self,
        *,
        repository: MonologueRepository,
        structures: StructureRepository,
        deliveries: DeliveryRepository,
        inbound: InboundRepository,
        config: RuntimeConfig | None = None,
        clock: Any | None = None,
    ) -> None:
        self.repository = repository
        self.structures = structures
        self.deliveries = deliveries
        self.inbound = inbound
        self.config = config or RuntimeConfig()
        self.clock = clock

    def _now(self) -> datetime:
        if self.clock is None:
            return utc_now()
        from sana_bot.timeutil import from_epoch

        return from_epoch(self.clock.wall())

    def status(self) -> tuple[MonologueStatus, float | None]:
        """当前活动索引的独白能力状态与门槛（禁用时门槛为 None）。"""
        snapshot = self.structures.active_snapshot()
        if snapshot is None:
            return MonologueStatus.DISABLED_NO_MATERIAL, None
        if snapshot.monologue_status is not MonologueStatus.ENABLED:
            return snapshot.monologue_status, None
        calibration = self.structures.calibration(snapshot.id, RetrievalStage.MONOLOGUE)
        if calibration is None or calibration.threshold is None:
            return MonologueStatus.DISABLED_UNCALIBRATED, None
        return MonologueStatus.ENABLED, calibration.threshold

    def create_for_turn(self, turn_id: str, delivery_id: str) -> OpportunityCreation:
        """正常回复确认送达后调用；只从 confirmed normal delivery 创建。"""
        delivery = self.deliveries.delivery(delivery_id)
        if delivery is None or delivery.turn_id != turn_id:
            return OpportunityCreation(created=False, reason_code="delivery_mismatch")
        from sana_bot.enums import DeliveryStatus, OutboundKind

        if delivery.status is not DeliveryStatus.CONFIRMED or delivery.kind is not OutboundKind.NORMAL:
            return OpportunityCreation(created=False, reason_code="delivery_not_normal")
        state, threshold = self.status()
        if state is not MonologueStatus.ENABLED:
            return OpportunityCreation(
                created=False,
                reason_code=state.value,
                details={"threshold": threshold},
            )
        turn = self.inbound.turn_row(turn_id)
        if turn is None:
            return OpportunityCreation(created=False, reason_code="turn_missing")
        conversation = self.inbound.conversation_row(turn.conversation_id)
        generation = int(conversation["generation"]) if conversation else 0
        pending = self.repository.due(now=self._now(), limit=50)
        if any(row.conversation_id == turn.conversation_id for row in pending):
            return OpportunityCreation(created=False, reason_code=SINGLE_PENDING_REASON)
        row = self.repository.create(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            generation=generation,
            source_msg_id=turn.inbound_id,
            now=self._now(),
        )
        return OpportunityCreation(created=True, opportunity_id=row.id)

    def cancel(self, conversation_id: str, *, reason: str = "user_continued") -> int:
        with self.repository.database.write():
            return self.repository.cancel_pending(conversation_id, reason=reason)

    def cancel_all(self, *, reason: str = "service_restart") -> int:
        with self.repository.database.write():
            return self.repository.cancel_all_pending(reason=reason)

    def due_now(self) -> Sequence[OpportunityRow]:
        return self.repository.due(now=self._now())

    def validate_configuration(self) -> None:
        if self.config.monologue_wait_seconds <= 0:
            raise SanaError("独白等待时长必须为正数", reason_code="config_invalid")
        if not 0.0 <= self.config.monologue_probability <= 1.0:
            raise SanaError("独白概率必须在 0 与 1 之间", reason_code="config_invalid")


def hook_for(scheduler: MonologueScheduler) -> Callable[[str, str, object], None]:
    """把创建机会接到轮次服务的确认送达钩子上。"""

    def hook(turn_id: str, delivery_id: str, kind: object) -> None:
        from sana_bot.enums import OutboundKind

        if kind is not OutboundKind.NORMAL:
            return
        scheduler.create_for_turn(turn_id, delivery_id)

    return hook
