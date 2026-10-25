"""身份、会话、入站消息与轮次的仓储。

落实 data-model.md 的不变量：``(app_id,user_openid)`` 唯一、每用户一个活动会话、
``generation`` 每次新消息递增、事件幂等键与 ``(app_id,message_id)`` 双重去重、
``inbound_id`` 唯一。聊天日在导入时按接收时间固定，回复处理不再改写。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sana_bot.enums import ConversationStatus, InboundStatus, Scene
from sana_bot.ports import NormalizedEvent
from sana_bot.storage.database import Database
from sana_bot.timeutil import chat_day, parse_utc, to_utc_iso, utc_now

__all__ = ["InboundRepository", "PersistOutcome", "InboundRow", "TurnRow"]


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class PersistOutcome:
    """落盘结果；重复与不支持的事件不会生成新轮次。"""

    accepted: bool
    duplicate: bool = False
    idempotency_conflict: bool = False
    ignored: bool = False
    inbound_id: str | None = None
    conversation_id: str = ""
    user_id: str = ""
    generation: int = 0
    chat_day: str = ""
    status: InboundStatus = InboundStatus.RECEIVED
    reason_code: str | None = None


@dataclass(frozen=True)
class InboundRow:
    id: str
    app_id: str
    event_key: str
    platform_event_id: str | None
    message_id: str
    user_id: str
    conversation_id: str
    text: str
    received_at: datetime
    event_time: datetime
    chat_day: str
    status: InboundStatus


@dataclass(frozen=True)
class TurnRow:
    id: str
    conversation_id: str
    inbound_id: str
    input_text: str
    snapshot_id: str | None
    index_id: str | None
    status: str
    chat_day: str
    created_at: datetime


class InboundRepository:
    """身份/会话/入站/轮次的读写；事务边界由调用方通过 :meth:`Database.write` 控制。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------ 身份

    def ensure_user(self, app_id: str, user_openid: str) -> str:
        """返回内部 UserIdentity.id；openid 不假定等于 QQ 号，也不跨应用复用。"""
        row = self.database.query_one(
            "SELECT id FROM user_identity WHERE app_id = ? AND user_openid = ?",
            (app_id, user_openid),
        )
        if row is not None:
            return row["id"]
        user_id = _new_id()
        self.database.execute(
            "INSERT INTO user_identity (id, app_id, user_openid, created_at) VALUES (?, ?, ?, ?)",
            (user_id, app_id, user_openid, to_utc_iso(utc_now())),
        )
        return user_id

    def get_user(self, user_id: str) -> sqlite3.Row | None:
        return self.database.query_one("SELECT * FROM user_identity WHERE id = ?", (user_id,))

    def user_openid(self, user_id: str) -> str | None:
        row = self.database.query_one(
            "SELECT user_openid FROM user_identity WHERE id = ?", (user_id,)
        )
        return row["user_openid"] if row else None

    # ------------------------------------------------------------------ 会话

    def active_conversation_id(self, user_id: str) -> str | None:
        row = self.database.query_one(
            """
            SELECT id FROM conversation
            WHERE user_id = ? AND session_status = 'active'
            """,
            (user_id,),
        )
        return row["id"] if row else None

    def ensure_conversation(self, user_id: str) -> str:
        existing = self.active_conversation_id(user_id)
        if existing is not None:
            return existing
        now = to_utc_iso(utc_now())
        conversation_id = _new_id()
        self.database.execute(
            """
            INSERT INTO conversation
                (id, user_id, generation, scene, session_status, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?, ?, ?)
            """,
            (conversation_id, user_id, Scene.UNKNOWN.value, ConversationStatus.ACTIVE.value, now, now),
        )
        return conversation_id

    def close_conversation(self, conversation_id: str) -> None:
        self.database.execute(
            "UPDATE conversation SET session_status = ?, updated_at = ? WHERE id = ?",
            (ConversationStatus.CLOSED.value, to_utc_iso(utc_now()), conversation_id),
        )

    def conversation_row(self, conversation_id: str) -> sqlite3.Row | None:
        return self.database.query_one("SELECT * FROM conversation WHERE id = ?", (conversation_id,))

    def bump_generation(self, conversation_id: str, *, at: datetime | None = None) -> int:
        """新消息到达：generation 递增，并记录最后入站时间。"""
        stamp = to_utc_iso(at or utc_now())
        self.database.execute(
            """
            UPDATE conversation
            SET generation = generation + 1, last_inbound_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (stamp, stamp, conversation_id),
        )
        row = self.database.query_one("SELECT generation FROM conversation WHERE id = ?", (conversation_id,))
        return int(row["generation"]) if row else 0

    def set_scene(self, conversation_id: str, scene: Scene) -> None:
        self.database.execute(
            "UPDATE conversation SET scene = ?, updated_at = ? WHERE id = ?",
            (scene.value, to_utc_iso(utc_now()), conversation_id),
        )

    # ------------------------------------------------------------------ 入站

    def persist_inbound(self, event: NormalizedEvent) -> PersistOutcome:
        """去重落盘并使旧独白失效前置条件成立（generation 递增）。

        必须在 :meth:`Database.write` 事务内调用。
        """
        existing_event = self.database.query_one(
            "SELECT id, payload_hash, status FROM inbound_event WHERE event_key = ?",
            (event.event_key,),
        )
        if existing_event is not None:
            conflict = existing_event["payload_hash"] != event.payload_hash
            return PersistOutcome(
                accepted=False,
                duplicate=not conflict,
                idempotency_conflict=conflict,
                inbound_id=existing_event["id"],
                reason_code="idempotency_conflict" if conflict else "duplicate_event",
                status=InboundStatus(existing_event["status"]),
            )
        existing_message = self.database.query_one(
            "SELECT id, payload_hash, status FROM inbound_event WHERE app_id = ? AND message_id = ?",
            (event.app_id, event.message_id),
        )
        if existing_message is not None:
            conflict = existing_message["payload_hash"] != event.payload_hash
            return PersistOutcome(
                accepted=False,
                duplicate=not conflict,
                idempotency_conflict=conflict,
                inbound_id=existing_message["id"],
                reason_code="idempotency_conflict" if conflict else "duplicate_message",
                status=InboundStatus(existing_message["status"]),
            )

        user_id = self.ensure_user(event.app_id, event.user_key)
        conversation_id = self.ensure_conversation(user_id)
        generation = self.bump_generation(conversation_id, at=event.received_at)
        day = chat_day(event.received_at).isoformat()
        status = InboundStatus.RECEIVED if event.supported else InboundStatus.IGNORED
        inbound_id = _new_id()
        now = to_utc_iso(utc_now())
        self.database.execute(
            """
            INSERT INTO inbound_event
                (id, app_id, event_key, platform_event_id, message_id, user_id, conversation_id,
                 text, payload_hash, received_at, event_time, chat_day, status, correlation_id,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inbound_id,
                event.app_id,
                event.event_key,
                event.platform_event_id,
                event.message_id,
                user_id,
                conversation_id,
                event.text,
                event.payload_hash,
                to_utc_iso(event.received_at),
                to_utc_iso(event.event_time),
                day,
                status.value,
                inbound_id,
                now,
                now,
            ),
        )
        return PersistOutcome(
            accepted=True,
            ignored=not event.supported,
            inbound_id=inbound_id,
            conversation_id=conversation_id,
            user_id=user_id,
            generation=generation,
            chat_day=day,
            status=status,
        )

    def inbound_row(self, inbound_id: str) -> InboundRow | None:
        row = self.database.query_one("SELECT * FROM inbound_event WHERE id = ?", (inbound_id,))
        return self._to_inbound(row) if row else None

    def set_inbound_status(self, inbound_id: str, status: InboundStatus) -> None:
        self.database.execute(
            "UPDATE inbound_event SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, to_utc_iso(utc_now()), inbound_id),
        )

    def recoverable_inbound(self, *, now: datetime | None = None, limit: int = 500) -> list[InboundRow]:
        """重启恢复：未处理且未过期的入站事件，按接收顺序。"""
        stamp = now or utc_now()
        rows = self.database.query(
            """
            SELECT * FROM inbound_event
            WHERE status IN ('received', 'queued', 'processing')
              AND datetime(received_at) > datetime(?, ?)
            ORDER BY received_at ASC
            LIMIT ?
            """,
            (to_utc_iso(stamp), f"-{int(_recovery_window_seconds())} seconds", limit),
        )
        return [self._to_inbound(row) for row in rows]

    def expire_stale_inbound(self, *, now: datetime | None = None, window_seconds: float = 300.0) -> int:
        stamp = to_utc_iso(now or utc_now())
        cursor = self.database.execute(
            """
            UPDATE inbound_event
            SET status = 'expired', updated_at = ?
            WHERE status IN ('received', 'queued') AND datetime(received_at) <= datetime(?, ?)
            """,
            (stamp, stamp, f"-{int(window_seconds)} seconds"),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _to_inbound(row: sqlite3.Row) -> InboundRow:
        return InboundRow(
            id=row["id"],
            app_id=row["app_id"],
            event_key=row["event_key"],
            platform_event_id=row["platform_event_id"],
            message_id=row["message_id"],
            user_id=row["user_id"],
            conversation_id=row["conversation_id"],
            text=row["text"],
            received_at=parse_utc(row["received_at"]),
            event_time=parse_utc(row["event_time"]),
            chat_day=row["chat_day"],
            status=InboundStatus(row["status"]),
        )

    # ------------------------------------------------------------------ 轮次

    def create_turn(
        self,
        *,
        conversation_id: str,
        inbound_id: str,
        input_text: str,
        chat_day_value: str,
        index_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        turn_id = _new_id()
        now = to_utc_iso(utc_now())
        self.database.execute(
            """
            INSERT INTO turn
                (id, conversation_id, inbound_id, input_text, snapshot_id, index_id, status,
                 chat_day, correlation_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?, 'processing', ?, ?, ?, ?)
            """,
            (
                turn_id,
                conversation_id,
                inbound_id,
                input_text,
                index_id,
                chat_day_value,
                correlation_id or turn_id,
                now,
                now,
            ),
        )
        return turn_id

    def attach_snapshot(self, turn_id: str, snapshot_id: str) -> None:
        self.database.execute(
            "UPDATE turn SET snapshot_id = ?, updated_at = ? WHERE id = ?",
            (snapshot_id, to_utc_iso(utc_now()), turn_id),
        )

    def set_turn_status(self, turn_id: str, status: str) -> None:
        self.database.execute(
            "UPDATE turn SET status = ?, updated_at = ? WHERE id = ?",
            (status, to_utc_iso(utc_now()), turn_id),
        )

    def turn_row(self, turn_id: str) -> TurnRow | None:
        row = self.database.query_one("SELECT * FROM turn WHERE id = ?", (turn_id,))
        if row is None:
            return None
        return TurnRow(
            id=row["id"],
            conversation_id=row["conversation_id"],
            inbound_id=row["inbound_id"],
            input_text=row["input_text"],
            snapshot_id=row["snapshot_id"],
            index_id=row["index_id"],
            status=row["status"],
            chat_day=row["chat_day"],
            created_at=parse_utc(row["created_at"]),
        )

    def turn_for_inbound(self, inbound_id: str) -> TurnRow | None:
        row = self.database.query_one("SELECT * FROM turn WHERE inbound_id = ?", (inbound_id,))
        if row is None:
            return None
        return TurnRow(
            id=row["id"],
            conversation_id=row["conversation_id"],
            inbound_id=row["inbound_id"],
            input_text=row["input_text"],
            snapshot_id=row["snapshot_id"],
            index_id=row["index_id"],
            status=row["status"],
            chat_day=row["chat_day"],
            created_at=parse_utc(row["created_at"]),
        )

    def recent_turns(self, conversation_id: str, *, limit: int = 6) -> list[TurnRow]:
        """最近轮次，按时间正序返回（含截止轮次）。"""
        rows = self.database.query(
            """
            SELECT * FROM turn WHERE conversation_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (conversation_id, limit),
        )
        turns = [
            TurnRow(
                id=row["id"],
                conversation_id=row["conversation_id"],
                inbound_id=row["inbound_id"],
                input_text=row["input_text"],
                snapshot_id=row["snapshot_id"],
                index_id=row["index_id"],
                status=row["status"],
                chat_day=row["chat_day"],
                created_at=parse_utc(row["created_at"]),
            )
            for row in rows
        ]
        turns.reverse()
        return turns

    def messages_before(self, turn_id: str, *, limit: int = 3) -> list[TurnRow]:
        """诊断用：该轮之前最多三轮（不复制无限对话）。"""
        row = self.database.query_one("SELECT conversation_id, created_at FROM turn WHERE id = ?", (turn_id,))
        if row is None:
            return []
        rows = self.database.query(
            """
            SELECT * FROM turn WHERE conversation_id = ? AND created_at < ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (row["conversation_id"], row["created_at"], limit),
        )
        turns = [
            TurnRow(
                id=item["id"],
                conversation_id=item["conversation_id"],
                inbound_id=item["inbound_id"],
                input_text=item["input_text"],
                snapshot_id=item["snapshot_id"],
                index_id=item["index_id"],
                status=item["status"],
                chat_day=item["chat_day"],
                created_at=parse_utc(item["created_at"]),
            )
            for item in rows
        ]
        turns.reverse()
        return turns

    def messages_after(self, turn_id: str, *, limit: int = 3) -> list[TurnRow]:
        """诊断用：该轮之后最多三轮（只关联实际发生的消息）。"""
        row = self.database.query_one(
            "SELECT conversation_id, created_at FROM turn WHERE id = ?", (turn_id,)
        )
        if row is None:
            return []
        rows = self.database.query(
            """
            SELECT * FROM turn WHERE conversation_id = ? AND created_at > ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (row["conversation_id"], row["created_at"], limit),
        )
        turns = [
            TurnRow(
                id=item["id"],
                conversation_id=item["conversation_id"],
                inbound_id=item["inbound_id"],
                input_text=item["input_text"],
                snapshot_id=item["snapshot_id"],
                index_id=item["index_id"],
                status=item["status"],
                chat_day=item["chat_day"],
                created_at=parse_utc(item["created_at"]),
            )
            for item in rows
        ]
        return turns

    def conversations_without_turn_for(self, inbound_id: str) -> bool:
        row = self.database.query_one("SELECT 1 FROM turn WHERE inbound_id = ?", (inbound_id,))
        return row is None


def payload_hash_of(payload: object) -> str:
    """稳定载荷指纹；用于识别幂等键被复用于不同内容的情况。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _recovery_window_seconds() -> float:
    return 300.0


def summarize_statuses(rows: Iterable[InboundRow]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for row in rows:
        summary[row.status.value] = summary.get(row.status.value, 0) + 1
    return summary


def ordered_inbound_ids(rows: Sequence[InboundRow]) -> list[str]:
    return [row.id for row in rows]
