"""任务处理器：把入站队列交给轮次服务，并维护独白机会的取消边界。

新入站先持久化并递增 generation，随后取消该会话待发独白；不同用户并行、同一用户串行。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sana_bot.scheduling.serial_queue import InboundQueueService, QueueItem, UserSerialQueue

__all__ = ["TurnWorker", "WorkerStats"]

CancelMonologue = Callable[[str], int]


@dataclass
class WorkerStats:
    handled: int = 0
    failed: int = 0
    cancelled_opportunities: int = 0
    last_reason_code: str | None = None
    last_error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "failed": self.failed,
            "cancelled_opportunities": self.cancelled_opportunities,
            "last_reason_code": self.last_reason_code,
            "last_error": self.last_error,
        }


@dataclass
class TurnWorker:
    """队列消费者；把每条入站交给轮次服务。"""

    queue: UserSerialQueue
    service: Any
    inbound_queue: InboundQueueService | None = None
    cancel_monologue: CancelMonologue | None = None
    stats: WorkerStats = field(default_factory=WorkerStats)

    async def start(self) -> None:
        await self.queue.start(self.handle)
        self._logger = logging.getLogger("sana_bot.scheduling.worker")

    async def stop(self) -> None:
        await self.queue.stop()

    async def handle(self, item: QueueItem) -> Any:
        if self.cancel_monologue is not None:
            try:
                cancelled = self.cancel_monologue(item.conversation_id)
                self.stats.cancelled_opportunities += int(cancelled or 0)
            except Exception as exc:  # noqa: BLE001 - 取消失败不得阻塞正常回复
                logging.getLogger("sana_bot.scheduling.worker").error(
                    "monologue_cancel_failed conversation=%s error=%s",
                    item.conversation_id,
                    type(exc).__name__,
                )
        outcome = await self.service.handle(item)
        self.stats.handled += 1
        if not outcome.ok:
            self.stats.failed += 1
            self.stats.last_reason_code = outcome.reason_code
        return outcome

    async def recover(self, *, window_seconds: float = 300.0) -> list[str]:
        """重启恢复：重新装载未处理且未过期的入站。"""
        if self.inbound_queue is None:
            return []
        return await self.inbound_queue.recover_pending(window_seconds=window_seconds)

    async def drain(self, user_id: str | None = None) -> None:
        await self.queue.join(user_id)
