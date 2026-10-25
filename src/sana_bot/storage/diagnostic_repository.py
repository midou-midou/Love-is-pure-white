"""诊断事件仓储：低匹配与失败路径的可追溯记录。

默认保留 30 天正文；清理只删除诊断正文，不删除关系账本、去重凭据与轮次标识。
记录失败不阻塞用户可见回复，也不能伪报成功。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.diagnostics import DiagnosticOutcome, DiagnosticRecord, sanitize_payload
from sana_bot.enums import DiagnosticCategory
from sana_bot.storage.database import Database, Migration
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = ["DiagnosticRepository", "LowMatchReport", "migrations", "summarise"]


def migrations(config: RuntimeConfig | None = None) -> tuple[Migration, ...]:
    return (
        Migration(
            name="0030_diagnostics",
            statements=(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_event (
                    id              TEXT PRIMARY KEY,
                    type            TEXT NOT NULL,
                    occurred_at     TEXT NOT NULL,
                    user_id         TEXT,
                    conversation_id TEXT,
                    turn_id         TEXT,
                    snapshot_id     TEXT,
                    reason          TEXT NOT NULL DEFAULT '',
                    payload         TEXT NOT NULL DEFAULT '{}',
                    correlation_id  TEXT,
                    created_at      TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_diagnostic_lookup
                    ON diagnostic_event(type, occurred_at)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_diagnostic_turn
                    ON diagnostic_event(turn_id)
                """,
            ),
        ),
    )


@dataclass(frozen=True)
class LowMatchReport:
    """规定字段的低匹配记录（无候选时保留空数组）。"""

    correlation_id: str
    stage: str
    input_text: str
    primary_mood: str
    candidate_sources: tuple[Mapping[str, Any], ...] = ()
    threshold: float | None = None
    index_version: str | None = None
    model_version: str | None = None
    config_version: int = 0
    reason: str = ""
    decision: str = ""
    delivery_result: str | None = None
    before_turns: tuple[str, ...] = ()
    after_turns: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "input": self.input_text,
            "primary_mood": self.primary_mood,
            "candidates": [dict(item) for item in self.candidate_sources],
            "threshold": self.threshold,
            "index_version": self.index_version,
            "model_version": self.model_version,
            "config_version": self.config_version,
            "reason": self.reason,
            "decision": self.decision,
            "delivery_result": self.delivery_result,
            "before_turns": list(self.before_turns),
            "after_turns": list(self.after_turns),
        }


class DiagnosticRepository:
    """实现 :class:`sana_bot.diagnostics.DiagnosticRecorder`。"""

    def __init__(
        self,
        database: Database,
        *,
        config: RuntimeConfig | None = None,
        inbound: InboundRepository | None = None,
    ) -> None:
        self.database = database
        self.config = config or RuntimeConfig()
        self.inbound = inbound or InboundRepository(database)

    # ------------------------------------------------------------------ 写入

    def record(self, record: DiagnosticRecord) -> DiagnosticOutcome:
        identifier = uuid.uuid4().hex
        payload = sanitize_payload(record.payload)
        try:
            with self.database.write():
                self.database.execute(
                    """
                    INSERT INTO diagnostic_event
                        (id, type, occurred_at, user_id, conversation_id, turn_id, snapshot_id,
                         reason, payload, correlation_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        record.category.value,
                        record.occurred_at or to_utc_iso(utc_now()),
                        record.user_id,
                        record.conversation_id,
                        record.turn_id,
                        record.snapshot_id,
                        record.reason,
                        json.dumps(payload, ensure_ascii=False),
                        record.correlation_id,
                        to_utc_iso(utc_now()),
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - 记录失败必须如实上报
            return DiagnosticOutcome(
                recorded=False,
                reason_code=DiagnosticCategory.LOGGING_ERROR.value,
                correlation_id=record.correlation_id,
                message=f"{type(exc).__name__}",
            )
        return DiagnosticOutcome(recorded=True, correlation_id=record.correlation_id)

    def record_low_match(
        self,
        *,
        correlation_id: str,
        report: LowMatchReport,
        user_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> DiagnosticOutcome:
        """记录低匹配或跳过原因；前后文只关联实际发生的轮次。"""
        return self.record(
            DiagnosticRecord(
                category=DiagnosticCategory.LOW_MATCH,
                correlation_id=correlation_id,
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                reason=report.reason,
                payload=report.to_payload(),
                occurred_at=to_utc_iso(utc_now()),
            )
        )

    # ------------------------------------------------------------------ 查询

    def query(
        self,
        *,
        user_id: str | None = None,
        event_id: str | None = None,
        since: str | None = None,
        reason: str | None = None,
        category: DiagnosticCategory | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_id:
            clauses.append("(id = ? OR turn_id = ? OR correlation_id = ?)")
            params.extend([event_id, event_id, event_id])
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if since:
            clauses.append("occurred_at >= ?")
            params.append(since)
        if reason:
            clauses.append("reason = ?")
            params.append(reason)
        if category is not None:
            clauses.append("type = ?")
            params.append(category.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.database.query(
            f"SELECT * FROM diagnostic_event {where} ORDER BY occurred_at ASC LIMIT ?",
            (*params, limit),
        )
        return [self._render(row) for row in rows]

    def with_context(
        self,
        *,
        event_id: str | None = None,
        user_id: str | None = None,
        since: str | None = None,
        reason: str | None = None,
        limit: int = 20,
        context_turns: int = 3,
    ) -> list[dict[str, Any]]:
        """按事件聚合，附带最多三轮前文与后文（通过 ID 关联，不复制对话）。"""
        events = self.query(
            user_id=user_id, event_id=event_id, since=since, reason=reason, limit=limit
        )
        enriched: list[dict[str, Any]] = []
        for event in events:
            item = dict(event)
            turn_id = item.get("turn_id")
            item["before"] = []
            item["after"] = []
            if turn_id:
                item["before"] = [
                    {"turn_id": turn.id, "input": turn.input_text}
                    for turn in self.inbound.messages_before(turn_id, limit=context_turns)
                ]
                item["after"] = [
                    {"turn_id": turn.id, "input": turn.input_text}
                    for turn in self.inbound.messages_after(turn_id, limit=context_turns)
                ]
            enriched.append(item)
        return enriched

    def count(self, *, category: DiagnosticCategory | None = None) -> int:
        if category is None:
            row = self.database.query_one("SELECT COUNT(*) AS n FROM diagnostic_event")
        else:
            row = self.database.query_one(
                "SELECT COUNT(*) AS n FROM diagnostic_event WHERE type = ?", (category.value,)
            )
        return int(row["n"]) if row else 0

    def record_delivery_result(self, *, event_id: str, result: str) -> bool:
        """回填实际发送结果，不新造事件。"""
        row = self.database.query_one(
            "SELECT payload FROM diagnostic_event WHERE id = ?", (event_id,)
        )
        if row is None:
            return False
        payload = json.loads(row["payload"])
        payload["delivery_result"] = result
        with self.database.write():
            self.database.execute(
                "UPDATE diagnostic_event SET payload = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), event_id),
            )
        return True

    # ------------------------------------------------------------------ 清理

    def prune(self, *, older_than_days: int | None = None) -> int:
        """清理诊断正文；保留类型、时间与关联标识。"""
        days = older_than_days or self.config.diagnostic_retention_days
        cutoff = to_utc_iso(utc_now() - timedelta(days=days))
        with self.database.write():
            cursor = self.database.execute(
                """
                UPDATE diagnostic_event
                SET payload = '{"pruned": true}', reason = ''
                WHERE occurred_at < ? AND payload != '{"pruned": true}'
                """,
                (cutoff,),
            )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _render(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": row["type"],
            "occurred_at": row["occurred_at"],
            "user_id": row["user_id"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "snapshot_id": row["snapshot_id"],
            "correlation_id": row["correlation_id"],
            "reason": row["reason"],
            "payload": json.loads(row["payload"]),
        }


def summarise(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for event in events:
        key = str(event.get("type", "unknown"))
        summary[key] = summary.get(key, 0) + 1
    return summary
