"""出站送达记录与被动回复预算。

落实 data-model.md：``(app_id,source_msg_id,msg_seq)`` 唯一、按来源消息串行预留、
预算不超过 4、保守 deadline = 事件时间 + 300 秒；网络结果不明记 uncertain，
不换 msg_id/msg_seq 绕过限制。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.enums import DeliveryStatus, OutboundKind
from sana_bot.errors import SanaError
from sana_bot.storage.database import Database, Migration
from sana_bot.timeutil import parse_utc, to_utc_iso, utc_now

__all__ = ["DeliveryRepository", "DeliveryRow", "BudgetDecision", "migrations", "sequence_for"]


def migrations(config: RuntimeConfig | None = None) -> tuple[Migration, ...]:
    return (
        Migration(
            name="0020_delivery",
            statements=(
                """
                CREATE TABLE IF NOT EXISTS outbound_delivery (
                    id                  TEXT PRIMARY KEY,
                    turn_id             TEXT,
                    opportunity_id      TEXT,
                    app_id              TEXT NOT NULL,
                    user_key            TEXT NOT NULL,
                    kind                TEXT NOT NULL
                                        CHECK (kind IN ('normal','fallback','error','monologue','status')),
                    source_msg_id       TEXT NOT NULL,
                    msg_seq             INTEGER NOT NULL,
                    content             TEXT NOT NULL,
                    status              TEXT NOT NULL
                                        CHECK (status IN ('prepared','sending','confirmed',
                                                          'rejected','uncertain')),
                    platform_message_id TEXT,
                    attempted_at        TEXT,
                    submit_at           REAL,
                    confirmed_at        TEXT,
                    error               TEXT,
                    correlation_id      TEXT,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL,
                    UNIQUE (app_id, source_msg_id, msg_seq)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_turn ON outbound_delivery(turn_id)
                """,
                """
                CREATE TABLE IF NOT EXISTS reply_budget (
                    app_id           TEXT NOT NULL,
                    source_msg_id    TEXT NOT NULL,
                    event_time       TEXT NOT NULL,
                    deadline         TEXT NOT NULL,
                    reserved_seq     INTEGER NOT NULL DEFAULT 0,
                    attempts         INTEGER NOT NULL DEFAULT 0,
                    remaining_budget INTEGER NOT NULL,
                    PRIMARY KEY (app_id, source_msg_id)
                )
                """,
            ),
        ),
    )


@dataclass(frozen=True)
class DeliveryRow:
    id: str
    turn_id: str | None
    opportunity_id: str | None
    app_id: str
    user_key: str
    kind: OutboundKind
    source_msg_id: str
    msg_seq: int
    content: str
    status: DeliveryStatus
    platform_message_id: str | None
    submit_at: float | None
    error: str | None
    correlation_id: str | None
    created_at: str


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    msg_seq: int
    remaining_budget: int
    deadline: datetime
    reason_code: str | None = None
    details: dict[str, Any] | None = None


class DeliveryRepository:
    """送达记录与预算的唯一入口。"""

    def __init__(self, database: Database, *, config: RuntimeConfig | None = None) -> None:
        self.database = database
        self.config = config or RuntimeConfig()

    # ------------------------------------------------------------------ 预算

    def ensure_budget(
        self, *, app_id: str, source_msg_id: str, event_time: datetime
    ) -> BudgetDecision:
        """建立或读取该来源消息的预算；deadline 取事件时间 + 保守窗口。"""
        deadline = event_time + timedelta(seconds=self.config.qq_passive_window_seconds)
        row = self.database.query_one(
            "SELECT * FROM reply_budget WHERE app_id = ? AND source_msg_id = ?",
            (app_id, source_msg_id),
        )
        if row is None:
            self.database.execute(
                """
                INSERT INTO reply_budget
                    (app_id, source_msg_id, event_time, deadline, reserved_seq, attempts,
                     remaining_budget)
                VALUES (?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    app_id,
                    source_msg_id,
                    to_utc_iso(event_time),
                    to_utc_iso(deadline),
                    self.config.qq_max_replies_per_source,
                ),
            )
            return BudgetDecision(
                allowed=True,
                msg_seq=1,
                remaining_budget=self.config.qq_max_replies_per_source,
                deadline=deadline,
            )
        remaining = int(row["remaining_budget"])
        return BudgetDecision(
            allowed=remaining > 0,
            msg_seq=int(row["reserved_seq"]) + 1,
            remaining_budget=remaining,
            deadline=parse_utc(row["deadline"]),
            reason_code=None if remaining > 0 else "budget_exhausted",
        )

    def eligible(
        self,
        *,
        app_id: str,
        source_msg_id: str,
        event_time: datetime,
        now: datetime | None = None,
    ) -> BudgetDecision:
        """发送前资格检查：剩余预算与 10 秒余量（不换来源 msg_id 绕过）。"""
        decision = self.ensure_budget(
            app_id=app_id, source_msg_id=source_msg_id, event_time=event_time
        )
        moment = now or utc_now()
        margin = self.config.qq_safety_margin_seconds
        if decision.deadline - moment < timedelta(seconds=margin):
            return BudgetDecision(
                allowed=False,
                msg_seq=decision.msg_seq,
                remaining_budget=decision.remaining_budget,
                deadline=decision.deadline,
                reason_code="passive_window_exhausted",
                details={"margin_seconds": margin},
            )
        if decision.remaining_budget <= 0:
            return BudgetDecision(
                allowed=False,
                msg_seq=decision.msg_seq,
                remaining_budget=0,
                deadline=decision.deadline,
                reason_code="budget_exhausted",
            )
        return decision

    def reserve(self, *, app_id: str, source_msg_id: str) -> int:
        """按来源消息串行预留一个稳定序号。"""
        self.database.execute(
            """
            UPDATE reply_budget
            SET reserved_seq = reserved_seq + 1,
                attempts = attempts + 1,
                remaining_budget = remaining_budget - 1
            WHERE app_id = ? AND source_msg_id = ?
            """,
            (app_id, source_msg_id),
        )
        row = self.database.query_one(
            "SELECT reserved_seq FROM reply_budget WHERE app_id = ? AND source_msg_id = ?",
            (app_id, source_msg_id),
        )
        if row is None:  # pragma: no cover - 预留前必然已建立
            raise SanaError("回复预算缺失", reason_code="budget_missing")
        return int(row["reserved_seq"])

    # ------------------------------------------------------------------ 记录

    def prepare(
        self,
        *,
        delivery_id: str,
        app_id: str,
        user_key: str,
        kind: OutboundKind,
        source_msg_id: str,
        content: str,
        msg_seq: int,
        turn_id: str | None = None,
        opportunity_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        now = to_utc_iso(utc_now())
        self.database.execute(
            """
            INSERT INTO outbound_delivery
                (id, turn_id, opportunity_id, app_id, user_key, kind, source_msg_id, msg_seq,
                 content, status, correlation_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)
            """,
            (
                delivery_id,
                turn_id,
                opportunity_id,
                app_id,
                user_key,
                kind.value,
                source_msg_id,
                msg_seq,
                content,
                correlation_id or delivery_id,
                now,
                now,
            ),
        )

    def mark_sending(self, delivery_id: str, *, submit_at: float | None = None) -> None:
        now = to_utc_iso(utc_now())
        self.database.execute(
            """
            UPDATE outbound_delivery
            SET status = 'sending', attempted_at = ?, submit_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, submit_at, now, delivery_id),
        )

    def settle(
        self,
        delivery_id: str,
        *,
        status: DeliveryStatus,
        platform_message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        now = to_utc_iso(utc_now())
        confirmed_at = now if status is DeliveryStatus.CONFIRMED else None
        self.database.execute(
            """
            UPDATE outbound_delivery
            SET status = ?, platform_message_id = ?, confirmed_at = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status.value, platform_message_id, confirmed_at, error, now, delivery_id),
        )

    def delivery(self, delivery_id: str) -> DeliveryRow | None:
        row = self.database.query_one(
            "SELECT * FROM outbound_delivery WHERE id = ?", (delivery_id,)
        )
        return self._row(row) if row else None

    def confirmed_normal(self, turn_id: str) -> DeliveryRow | None:
        row = self.database.query_one(
            """
            SELECT * FROM outbound_delivery
            WHERE turn_id = ? AND status = 'confirmed' AND kind = 'normal'
            LIMIT 1
            """,
            (turn_id,),
        )
        return self._row(row) if row else None

    def reply_text(self, turn_id: str) -> str | None:
        row = self.database.query_one(
            """
            SELECT content FROM outbound_delivery
            WHERE turn_id = ? AND status = 'confirmed'
            ORDER BY msg_seq ASC LIMIT 1
            """,
            (turn_id,),
        )
        return row["content"] if row else None

    def recent_replies(self, user_key: str, *, limit: int = 3) -> list[DeliveryRow]:
        rows = self.database.query(
            """
            SELECT * FROM outbound_delivery
            WHERE user_key = ? AND status = 'confirmed'
            ORDER BY created_at DESC LIMIT ?
            """,
            (user_key, limit),
        )
        return [self._row(row) for row in rows]

    def uncertain(self, *, limit: int = 50) -> list[DeliveryRow]:
        rows = self.database.query(
            """
            SELECT * FROM outbound_delivery WHERE status = 'uncertain'
            ORDER BY created_at ASC LIMIT ?
            """,
            (limit,),
        )
        return [self._row(row) for row in rows]

    def recover_interrupted(self) -> int:
        """重启恢复：sending 视为 uncertain，不假定成功也不自动重发。"""
        now = to_utc_iso(utc_now())
        cursor = self.database.execute(
            """
            UPDATE outbound_delivery
            SET status = 'uncertain', error = COALESCE(error, 'process_interrupted'), updated_at = ?
            WHERE status = 'sending'
            """,
            (now,),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _row(row: Any) -> DeliveryRow:
        return DeliveryRow(
            id=row["id"],
            turn_id=row["turn_id"],
            opportunity_id=row["opportunity_id"],
            app_id=row["app_id"],
            user_key=row["user_key"],
            kind=OutboundKind(row["kind"]),
            source_msg_id=row["source_msg_id"],
            msg_seq=int(row["msg_seq"]),
            content=row["content"],
            status=DeliveryStatus(row["status"]),
            platform_message_id=row["platform_message_id"],
            submit_at=row["submit_at"],
            error=row["error"],
            correlation_id=row["correlation_id"],
            created_at=row["created_at"],
        )


def sequence_for(kind: OutboundKind) -> int:
    """稳定序号：正常回复 1、独白 2、兜底与提示 1。"""
    return {
        OutboundKind.NORMAL: 1,
        OutboundKind.MONOLOGUE: 2,
        OutboundKind.FALLBACK: 1,
        OutboundKind.ERROR: 1,
        OutboundKind.STATUS: 1,
    }[kind]


def kinds_in_order(rows: Sequence[DeliveryRow]) -> list[str]:
    return [row.kind.value for row in rows]
