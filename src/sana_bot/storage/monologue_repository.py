"""沉默独白机会仓储。

``turn_id`` 唯一；状态迁移受限：pending → cancelled/skipped/evaluating →
sending → sent/failed/uncertain。禁用能力时不创建机会，启用也不补建历史轮次。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.enums import OpportunityStatus
from sana_bot.errors import ConfigError
from sana_bot.storage.database import Database, Migration
from sana_bot.timeutil import parse_utc, to_utc_iso

__all__ = ["MonologueRepository", "OpportunityRow", "migrations", "new_id", "ALLOWED_TRANSITIONS"]


def new_id() -> str:
    return uuid.uuid4().hex


ALLOWED_TRANSITIONS: dict[OpportunityStatus, frozenset[OpportunityStatus]] = {
    OpportunityStatus.PENDING: frozenset(
        {
            OpportunityStatus.EVALUATING,
            OpportunityStatus.CANCELLED,
            OpportunityStatus.SKIPPED,
        }
    ),
    OpportunityStatus.EVALUATING: frozenset(
        {
            OpportunityStatus.EVALUATING,  # 记录抽取结果与候选
            OpportunityStatus.SENDING,
            OpportunityStatus.SKIPPED,
            OpportunityStatus.CANCELLED,
            OpportunityStatus.FAILED,
        }
    ),
    OpportunityStatus.SENDING: frozenset(
        {
            OpportunityStatus.SENT,
            OpportunityStatus.FAILED,
            OpportunityStatus.UNCERTAIN,
        }
    ),
    OpportunityStatus.SENT: frozenset(),
    OpportunityStatus.FAILED: frozenset(),
    OpportunityStatus.UNCERTAIN: frozenset(),
    OpportunityStatus.CANCELLED: frozenset(),
    OpportunityStatus.SKIPPED: frozenset(),
}


def migrations(config: RuntimeConfig | None = None) -> tuple[Migration, ...]:
    return (
        Migration(
            name="0060_monologue",
            statements=(
                """
                CREATE TABLE IF NOT EXISTS monologue_opportunity (
                    id              TEXT PRIMARY KEY,
                    turn_id         TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL,
                    generation      INTEGER NOT NULL DEFAULT 0,
                    source_msg_id   TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    due_at          TEXT NOT NULL,
                    probability     REAL NOT NULL,
                    random_draw     REAL,
                    status          TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending','evaluating','sending','sent',
                                                      'failed','uncertain','cancelled','skipped')),
                    candidate_id    TEXT,
                    latest_snapshot TEXT,
                    skip_reason     TEXT,
                    delivery_id     TEXT
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_monologue_due
                    ON monologue_opportunity(status, due_at)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_monologue_conversation
                    ON monologue_opportunity(conversation_id, status)
                """,
            ),
        ),
    )


@dataclass(frozen=True)
class OpportunityRow:
    id: str
    turn_id: str
    conversation_id: str
    generation: int
    source_msg_id: str
    created_at: datetime
    due_at: datetime
    probability: float
    random_draw: float | None
    status: OpportunityStatus
    candidate_id: str | None
    latest_snapshot: str | None
    skip_reason: str | None
    delivery_id: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "generation": self.generation,
            "source_msg_id": self.source_msg_id,
            "created_at": to_utc_iso(self.created_at),
            "due_at": to_utc_iso(self.due_at),
            "probability": self.probability,
            "random_draw": self.random_draw,
            "status": self.status.value,
            "candidate_id": self.candidate_id,
            "latest_snapshot": self.latest_snapshot,
            "skip_reason": self.skip_reason,
            "delivery_id": self.delivery_id,
        }


class MonologueRepository:
    def __init__(self, database: Database, *, config: RuntimeConfig | None = None) -> None:
        self.database = database
        self.config = config or RuntimeConfig()

    # ------------------------------------------------------------------ 写入

    def create(
        self,
        *,
        turn_id: str,
        conversation_id: str,
        generation: int,
        source_msg_id: str,
        now: datetime,
        probability: float | None = None,
        wait_seconds: float | None = None,
    ) -> OpportunityRow:
        """正常回复确认成功后创建一次机会；每轮至多一条。"""
        if probability is None:
            probability = self.config.monologue_probability
        if not 0.0 <= probability <= 1.0:
            raise ConfigError("独白概率必须在 0 与 1 之间")
        seconds = self.config.monologue_wait_seconds if wait_seconds is None else wait_seconds
        if seconds <= 0:
            raise ConfigError("独白等待时长必须为正数")
        existing = self.for_turn(turn_id)
        if existing is not None:
            return existing
        opportunity_id = new_id()
        self.database.execute(
            """
            INSERT INTO monologue_opportunity
                (id, turn_id, conversation_id, generation, source_msg_id, created_at, due_at,
                 probability, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                opportunity_id,
                turn_id,
                conversation_id,
                generation,
                source_msg_id,
                to_utc_iso(now),
                to_utc_iso(now + timedelta(seconds=seconds)),
                probability,
            ),
        )
        return self.for_turn(turn_id)  # type: ignore[return-value]

    def transition(
        self,
        opportunity_id: str,
        target: OpportunityStatus,
        *,
        random_draw: float | None = None,
        candidate_id: str | None = None,
        latest_snapshot: str | None = None,
        skip_reason: str | None = None,
        delivery_id: str | None = None,
    ) -> bool:
        row = self.get(opportunity_id)
        if row is None:
            return False
        if target not in ALLOWED_TRANSITIONS[row.status]:
            return False
        self.database.execute(
            """
            UPDATE monologue_opportunity
            SET status = ?, random_draw = COALESCE(?, random_draw),
                candidate_id = COALESCE(?, candidate_id),
                latest_snapshot = COALESCE(?, latest_snapshot),
                skip_reason = COALESCE(?, skip_reason),
                delivery_id = COALESCE(?, delivery_id)
            WHERE id = ?
            """,
            (
                target.value,
                random_draw,
                candidate_id,
                latest_snapshot,
                skip_reason,
                delivery_id,
                opportunity_id,
            ),
        )
        return True

    def cancel_pending(self, conversation_id: str, *, reason: str = "user_continued") -> int:
        """新入站、会话结束或重启时取消待发机会；不补发。"""
        cursor = self.database.execute(
            """
            UPDATE monologue_opportunity
            SET status = 'cancelled', skip_reason = ?
            WHERE conversation_id = ? AND status IN ('pending', 'evaluating')
            """,
            (reason, conversation_id),
        )
        return int(cursor.rowcount or 0)

    def cancel_all_pending(self, *, reason: str = "service_restart") -> int:
        cursor = self.database.execute(
            """
            UPDATE monologue_opportunity
            SET status = 'cancelled', skip_reason = ?
            WHERE status IN ('pending', 'evaluating')
            """,
            (reason,),
        )
        return int(cursor.rowcount or 0)

    def mark_interrupted_uncertain(self) -> int:
        cursor = self.database.execute(
            """
            UPDATE monologue_opportunity
            SET status = 'uncertain', skip_reason = COALESCE(skip_reason, 'process_interrupted')
            WHERE status = 'sending'
            """
        )
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ 读取

    def get(self, opportunity_id: str) -> OpportunityRow | None:
        row = self.database.query_one(
            "SELECT * FROM monologue_opportunity WHERE id = ?", (opportunity_id,)
        )
        return self._row(row) if row else None

    def for_turn(self, turn_id: str) -> OpportunityRow | None:
        row = self.database.query_one(
            "SELECT * FROM monologue_opportunity WHERE turn_id = ?", (turn_id,)
        )
        return self._row(row) if row else None

    def due(self, *, now: datetime, limit: int = 20) -> list[OpportunityRow]:
        rows = self.database.query(
            """
            SELECT * FROM monologue_opportunity
            WHERE status = 'pending' AND due_at <= ?
            ORDER BY due_at ASC LIMIT ?
            """,
            (to_utc_iso(now), limit),
        )
        return [self._row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.database.query(
            "SELECT status, COUNT(*) AS n FROM monologue_opportunity GROUP BY status"
        )
        return {row["status"]: int(row["n"]) for row in rows}

    @staticmethod
    def _row(row: Any) -> OpportunityRow:
        return OpportunityRow(
            id=row["id"],
            turn_id=row["turn_id"],
            conversation_id=row["conversation_id"],
            generation=int(row["generation"]),
            source_msg_id=row["source_msg_id"],
            created_at=parse_utc(row["created_at"]),
            due_at=parse_utc(row["due_at"]),
            probability=float(row["probability"]),
            random_draw=row["random_draw"],
            status=OpportunityStatus(row["status"]),
            candidate_id=row["candidate_id"],
            latest_snapshot=row["latest_snapshot"],
            skip_reason=row["skip_reason"],
            delivery_id=row["delivery_id"],
        )
