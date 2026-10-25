"""每用户串行队列与入站受理。

同一用户严格串行处理，不同用户并行；容量超限时明确拒绝/延后（不静默丢消息）；
重复或不支持的事件不生成新轮次。重启后重新装载未处理且未过期的入站事件。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sana_bot.enums import InboundStatus
from sana_bot.errors import SanaError
from sana_bot.ports import NormalizedEvent
from sana_bot.storage.database import Database
from sana_bot.storage.inbound_repository import InboundRepository, PersistOutcome

__all__ = [
    "QueueItem",
    "EnqueueOutcome",
    "CapacityExceeded",
    "ItemTimeout",
    "UserSerialQueue",
    "InboundQueueService",
]


class CapacityExceeded(SanaError):
    """该用户队列已满：明确拒绝或延后，不静默丢弃。"""

    exit_code = 4
    reason_code = "queue_capacity_exceeded"


class ItemTimeout(SanaError):
    exit_code = 4
    reason_code = "queue_item_timeout"


@dataclass(frozen=True)
class QueueItem:
    """一次待处理的入站轮次。"""

    inbound_id: str
    user_id: str
    conversation_id: str
    generation: int
    text: str
    chat_day: str
    received_at: datetime
    event_time: datetime
    correlation_id: str
    app_id: str = ""
    user_key: str = ""
    message_id: str = ""
    supported: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnqueueOutcome:
    """受理结果；重复与不支持事件在这里被拦下。"""

    accepted: bool
    duplicate: bool = False
    ignored: bool = False
    idempotency_conflict: bool = False
    inbound_id: str | None = None
    conversation_id: str = ""
    generation: int = 0
    queued: bool = False
    reason_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


Handler = Callable[[QueueItem], Awaitable[Any]]
ErrorHook = Callable[[QueueItem, BaseException], None]


class UserSerialQueue:
    """按用户隔离的串行执行器。"""

    def __init__(
        self,
        *,
        capacity_per_user: int = 100,
        item_timeout_seconds: float = 60.0,
        on_error: ErrorHook | None = None,
    ) -> None:
        if capacity_per_user <= 0:
            raise SanaError("每用户队列容量必须为正数", reason_code="config_invalid")
        if item_timeout_seconds <= 0:
            raise SanaError("队列处理超时必须为正数", reason_code="config_invalid")
        self.capacity_per_user = capacity_per_user
        self.item_timeout_seconds = item_timeout_seconds
        self._on_error = on_error
        self._lanes: dict[str, asyncio.Queue[QueueItem]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._flushes: dict[str, list[asyncio.Future[Any]]] = {}
        self._handler: Handler | None = None
        self._running = False
        self._logger = logging.getLogger("sana_bot.scheduling.queue")
        self.last_error: BaseException | None = None

    # ------------------------------------------------------------------ 生命周期

    async def start(self, handler: Handler) -> None:
        self._handler = handler
        self._running = True

    async def stop(self) -> None:
        self._running = False
        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        for task in workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停止时吞掉派发中的异常
                pass
        self._workers.clear()

    @property
    def running(self) -> bool:
        return self._running

    def depth_for(self, user_id: str) -> int:
        lane = self._lanes.get(user_id)
        return lane.qsize() if lane else 0

    @property
    def depth(self) -> int:
        return sum(lane.qsize() for lane in self._lanes.values())

    def users(self) -> list[str]:
        return sorted(self._lanes)

    # ------------------------------------------------------------------ 投递

    async def put(self, item: QueueItem, *, follow: bool = False) -> Any | None:
        """入队；``follow=True`` 时等待该条处理完成并返回结果（供测试与本地核对）。"""
        if not self._running or self._handler is None:
            raise SanaError("队列尚未启动", reason_code="queue_not_started")
        lane = self._lanes.get(item.user_id)
        if lane is None:
            lane = asyncio.Queue(maxsize=self.capacity_per_user)
            self._lanes[item.user_id] = lane
        if lane.full():
            raise CapacityExceeded(
                f"用户 {item.user_id} 队列已满（{self.capacity_per_user}）",
                details={"user_id": item.user_id, "inbound_id": item.inbound_id},
            )
        future: asyncio.Future[Any] | None = asyncio.get_running_loop().create_future() if follow else None
        await lane.put(item)
        if follow:
            assert future is not None
            self._flushes.setdefault(item.user_id, []).append(future)
        self._ensure_worker(item.user_id)
        if future is None:
            return None
        return await future

    def _ensure_worker(self, user_id: str) -> None:
        task = self._workers.get(user_id)
        if task is None or task.done():
            self._workers[user_id] = asyncio.create_task(self._run_lane(user_id))

    async def _run_lane(self, user_id: str) -> None:
        lane = self._lanes[user_id]
        while True:
            try:
                item = await lane.get()
            except asyncio.CancelledError:
                raise
            try:
                result = await asyncio.wait_for(self._invoke(item), timeout=self.item_timeout_seconds)
            except asyncio.TimeoutError:
                error = ItemTimeout(
                    f"处理超时：{item.inbound_id}",
                    correlation_id=item.correlation_id,
                )
                self._handle_error(item, error)
                self._resolve(user_id, error)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 单条失败不能拖垮该用户通道
                self._handle_error(item, exc)
                self._resolve(user_id, exc)
            else:
                self._resolve(user_id, result)
            finally:
                lane.task_done()

    async def _invoke(self, item: QueueItem) -> Any:
        assert self._handler is not None
        return await self._handler(item)

    def _handle_error(self, item: QueueItem, exc: BaseException) -> None:
        self.last_error = exc
        self._logger.error(
            "queue_item_failed inbound_id=%s correlation_id=%s error=%s",
            item.inbound_id,
            item.correlation_id,
            type(exc).__name__,
        )
        if self._on_error is not None:
            try:
                self._on_error(item, exc)
            except Exception:  # noqa: BLE001 - 错误钩子自身失败不再放大
                self._logger.exception("queue_error_hook_failed inbound_id=%s", item.inbound_id)

    def _resolve(self, user_id: str, result: Any) -> None:
        pending = self._flushes.get(user_id)
        if not pending:
            return
        future = pending.pop(0)
        if not future.done():
            if isinstance(result, BaseException):
                future.set_exception(result)
            else:
                future.set_result(result)

    async def join(self, user_id: str | None = None) -> None:
        """等待队列中的任务处理完毕（不含等待新任务）。"""
        targets = [user_id] if user_id else list(self._lanes)
        for target in targets:
            lane = self._lanes.get(target)
            if lane is not None:
                await lane.join()


class InboundQueueService:
    """入站受理：去重落盘 → generation 递增 → 串行入队。"""

    def __init__(
        self,
        *,
        database: Database,
        repository: InboundRepository,
        queue: UserSerialQueue,
    ) -> None:
        self.database = database
        self.repository = repository
        self.queue = queue

    def persist(self, event: NormalizedEvent) -> PersistOutcome:
        with self.database.write():
            return self.repository.persist_inbound(event)

    async def accept(self, event: NormalizedEvent) -> EnqueueOutcome:
        outcome = self.persist(event)
        if not outcome.accepted:
            return EnqueueOutcome(
                accepted=False,
                duplicate=outcome.duplicate,
                idempotency_conflict=outcome.idempotency_conflict,
                inbound_id=outcome.inbound_id,
                conversation_id=outcome.conversation_id,
                generation=outcome.generation,
                reason_code=outcome.reason_code,
            )
        if outcome.ignored:
            with self.database.write():
                self.repository.set_inbound_status(outcome.inbound_id or "", InboundStatus.IGNORED)
            return EnqueueOutcome(
                accepted=False,
                ignored=True,
                inbound_id=outcome.inbound_id,
                conversation_id=outcome.conversation_id,
                generation=outcome.generation,
                reason_code="unsupported_event",
            )
        row = self.repository.inbound_row(outcome.inbound_id or "")
        if row is None:  # pragma: no cover - 落盘后立即读取必然存在
            return EnqueueOutcome(
                accepted=False,
                reason_code="storage_failure",
                inbound_id=outcome.inbound_id,
            )
        item = QueueItem(
            inbound_id=row.id,
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            generation=outcome.generation,
            text=row.text,
            chat_day=row.chat_day,
            received_at=row.received_at,
            event_time=row.event_time,
            correlation_id=row.id,
            app_id=row.app_id,
            user_key=self.repository.user_openid(row.user_id) or "",
            message_id=row.message_id,
        )
        try:
            await self.queue.put(item)
        except CapacityExceeded as exc:
            # 留在 received 状态，交由恢复扫描延后处理，不静默丢弃
            return EnqueueOutcome(
                accepted=True,
                inbound_id=row.id,
                conversation_id=row.conversation_id,
                generation=outcome.generation,
                queued=False,
                reason_code=exc.reason_code,
                details={"deferred": True},
            )
        with self.database.write():
            self.repository.set_inbound_status(row.id, InboundStatus.QUEUED)
        return EnqueueOutcome(
            accepted=True,
            inbound_id=row.id,
            conversation_id=row.conversation_id,
            generation=outcome.generation,
            queued=True,
        )

    async def recover_pending(self, *, window_seconds: float = 300.0, limit: int = 500) -> list[str]:
        """重启后重新装载合法未过期的入站；超窗口的标记 expired。"""
        with self.database.write():
            self.repository.expire_stale_inbound(window_seconds=window_seconds)
        rows = self.repository.recoverable_inbound(limit=limit)
        requeued: list[str] = []
        for row in rows:
            conversation = self.repository.conversation_row(row.conversation_id)
            generation = int(conversation["generation"]) if conversation else 0
            item = QueueItem(
                inbound_id=row.id,
                user_id=row.user_id,
                conversation_id=row.conversation_id,
                generation=generation,
                text=row.text,
                chat_day=row.chat_day,
                received_at=row.received_at,
                event_time=row.event_time,
                correlation_id=row.id,
                app_id=row.app_id,
                user_key=self.repository.user_openid(row.user_id) or "",
                message_id=row.message_id,
            )
            try:
                await self.queue.put(item)
            except CapacityExceeded:
                break
            with self.database.write():
                self.repository.set_inbound_status(row.id, InboundStatus.QUEUED)
            requeued.append(row.id)
        return requeued
