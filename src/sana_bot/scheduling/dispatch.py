"""最终发送临界区：generation 核对、预算与稳定序号。

提交前的新入站会取消该机会；HTTP 已提交视为已发生，之后不可撤回但会记录 submit 时间。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sana_bot.enums import DeliveryStatus, OutboundKind, OpportunityStatus
from sana_bot.ports import QQTransport
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository, sequence_for
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.monologue_repository import MonologueRepository
from sana_bot.timeutil import from_epoch, utc_now

__all__ = ["MonologueDispatcher", "DispatchOutcome"]


@dataclass(frozen=True)
class DispatchOutcome:
    sent: bool
    status: DeliveryStatus | None = None
    delivery_id: str | None = None
    reason_code: str | None = None
    submit_at: float | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "status": self.status.value if self.status else None,
            "delivery_id": self.delivery_id,
            "reason_code": self.reason_code,
            "submit_at": self.submit_at,
        }


class MonologueDispatcher:
    """独白发送的唯一路径；普通回复也可复用同一预算规则。"""

    def __init__(
        self,
        *,
        database: Database,
        deliveries: DeliveryRepository,
        monologues: MonologueRepository,
        inbound: InboundRepository,
        transport: QQTransport | None,
        clock: Any | None = None,
    ) -> None:
        self.database = database
        self.deliveries = deliveries
        self.monologues = monologues
        self.inbound = inbound
        self.transport = transport
        self.clock = clock

    def _now(self) -> Any:
        if self.clock is None:
            return utc_now()
        return from_epoch(self.clock.wall())

    async def send_monologue(
        self,
        *,
        opportunity_id: str,
        content: str,
        user_key: str,
        app_id: str,
        correlation_id: str | None = None,
    ) -> DispatchOutcome:
        opportunity = self.monologues.get(opportunity_id)
        if opportunity is None:
            return DispatchOutcome(sent=False, reason_code="opportunity_missing")
        if opportunity.status is not OpportunityStatus.EVALUATING:
            return DispatchOutcome(
                sent=False,
                reason_code="opportunity_not_evaluating",
                submit_at=None,
            )
        conversation = self.inbound.conversation_row(opportunity.conversation_id)
        current_generation = int(conversation["generation"]) if conversation else 0
        if current_generation != opportunity.generation:
            self._cancel(opportunity_id, "generation_advanced")
            return DispatchOutcome(sent=False, reason_code="generation_advanced")
        if self.transport is None:
            self._cancel(opportunity_id, "transport_unavailable")
            return DispatchOutcome(sent=False, reason_code="transport_unavailable")
        turn = self.inbound.turn_row(opportunity.turn_id)
        event_time = turn.created_at if turn else self._now()
        budget = self.deliveries.eligible(
            app_id=app_id,
            source_msg_id=opportunity.source_msg_id,
            event_time=event_time,
            now=self._now(),
        )
        if not budget.allowed:
            self._cancel(opportunity_id, budget.reason_code or "budget_exhausted")
            return DispatchOutcome(sent=False, reason_code=budget.reason_code)

        msg_seq = sequence_for(OutboundKind.MONOLOGUE)
        delivery_id = f"{opportunity_id}-monologue"
        with self.database.write():
            next_seq = self.deliveries.reserve(
                app_id=app_id, source_msg_id=opportunity.source_msg_id
            )
            msg_seq = max(msg_seq, next_seq)
            self.deliveries.prepare(
                delivery_id=delivery_id,
                app_id=app_id,
                user_key=user_key,
                kind=OutboundKind.MONOLOGUE,
                source_msg_id=opportunity.source_msg_id,
                content=content,
                msg_seq=msg_seq,
                turn_id=opportunity.turn_id,
                opportunity_id=opportunity_id,
                correlation_id=correlation_id,
            )
            self.deliveries.mark_sending(delivery_id, submit_at=self._submit_stamp())
            self.monologues.transition(
                opportunity_id, OpportunityStatus.SENDING, delivery_id=delivery_id
            )
        try:
            result = await self.transport.send(
                content=content,
                source_msg_id=opportunity.source_msg_id,
                msg_seq=msg_seq,
                user_key=user_key,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - 结果不明不得重发
            with self.database.write():
                self.deliveries.settle(
                    delivery_id, status=DeliveryStatus.UNCERTAIN, error=type(exc).__name__
                )
                self.monologues.transition(
                    opportunity_id, OpportunityStatus.UNCERTAIN, skip_reason="delivery_exception"
                )
            return DispatchOutcome(
                sent=False,
                status=DeliveryStatus.UNCERTAIN,
                delivery_id=delivery_id,
                reason_code="delivery_exception",
                submit_at=self._submit_stamp(),
            )
        with self.database.write():
            self.deliveries.settle(
                delivery_id,
                status=result.status,
                platform_message_id=result.platform_message_id,
                error=result.reason_code,
            )
            target = {
                DeliveryStatus.CONFIRMED: OpportunityStatus.SENT,
                DeliveryStatus.REJECTED: OpportunityStatus.FAILED,
                DeliveryStatus.UNCERTAIN: OpportunityStatus.UNCERTAIN,
            }[result.status]
            self.monologues.transition(
                opportunity_id,
                target,
                skip_reason=None if result.status is DeliveryStatus.CONFIRMED else result.reason_code,
            )
        return DispatchOutcome(
            sent=result.status is DeliveryStatus.CONFIRMED,
            status=result.status,
            delivery_id=delivery_id,
            reason_code=result.reason_code,
            submit_at=self._submit_stamp(),
        )

    def _cancel(self, opportunity_id: str, reason: str) -> None:
        with self.database.write():
            self.monologues.transition(
                opportunity_id, OpportunityStatus.CANCELLED, skip_reason=reason
            )

    def _submit_stamp(self) -> float:
        return float(self.clock.now()) if self.clock is not None else 0.0
