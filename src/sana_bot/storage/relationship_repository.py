"""长期关系与好感度账本仓储。

不变量：``user_id`` 唯一、``score >= 0``、``(user_id, local_date)`` 唯一、
``delivery_id`` 与 ``turn_id`` 各自唯一、``new_day``/``happy_delta`` ∈ {0,1}。
配置只影响后续事件，不重算历史。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.enums import RelationshipStatus
from sana_bot.storage.database import Database, Migration
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = ["RelationshipRepository", "RelationshipRow", "migrations", "new_id"]


def new_id() -> str:
    return uuid.uuid4().hex


def migrations(config: RuntimeConfig | None = None) -> tuple[Migration, ...]:
    return (
        Migration(
            name="0050_relationship",
            statements=(
                """
                CREATE TABLE IF NOT EXISTS relationship (
                    user_id         TEXT PRIMARY KEY,
                    score           REAL NOT NULL DEFAULT 0 CHECK (score >= 0),
                    keyword_count   INTEGER NOT NULL DEFAULT 0,
                    dialogue_days   INTEGER NOT NULL DEFAULT 0,
                    happy_count     INTEGER NOT NULL DEFAULT 0,
                    romance_status  TEXT NOT NULL DEFAULT 'non_romantic'
                                    CHECK (romance_status IN ('non_romantic','romantic')),
                    version         INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS dialogue_day (
                    user_id      TEXT NOT NULL,
                    local_date   TEXT NOT NULL,
                    first_turn_id TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    PRIMARY KEY (user_id, local_date)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS affection_event (
                    id              TEXT PRIMARY KEY,
                    delivery_id     TEXT NOT NULL UNIQUE,
                    turn_id         TEXT NOT NULL UNIQUE,
                    user_id         TEXT NOT NULL,
                    day             TEXT NOT NULL,
                    keyword_hits    INTEGER NOT NULL DEFAULT 0,
                    new_day         INTEGER NOT NULL DEFAULT 0 CHECK (new_day IN (0, 1)),
                    happy_delta     INTEGER NOT NULL DEFAULT 0 CHECK (happy_delta IN (0, 1)),
                    weights         TEXT NOT NULL DEFAULT '{}',
                    delta           REAL NOT NULL DEFAULT 0,
                    score_before    REAL NOT NULL DEFAULT 0,
                    score_after     REAL NOT NULL DEFAULT 0,
                    romance_before  TEXT NOT NULL DEFAULT 'non_romantic',
                    romance_after   TEXT NOT NULL DEFAULT 'non_romantic',
                    config_version  INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS relationship_config (
                    version              INTEGER PRIMARY KEY,
                    keyword_rules        TEXT NOT NULL DEFAULT '[]',
                    weights              TEXT NOT NULL DEFAULT '{}',
                    romance_threshold    REAL NOT NULL,
                    normalization_version TEXT NOT NULL DEFAULT 'v1',
                    effective_at         TEXT NOT NULL
                )
                """,
            ),
        ),
    )


@dataclass(frozen=True)
class RelationshipRow:
    user_id: str
    score: float
    keyword_count: int
    dialogue_days: int
    happy_count: int
    romance_status: RelationshipStatus
    version: int


class RelationshipRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------ 读取

    def get(self, user_id: str) -> RelationshipRow:
        row = self.database.query_one("SELECT * FROM relationship WHERE user_id = ?", (user_id,))
        if row is None:
            return RelationshipRow(
                user_id=user_id,
                score=0.0,
                keyword_count=0,
                dialogue_days=0,
                happy_count=0,
                romance_status=RelationshipStatus.NON_ROMANTIC,
                version=0,
            )
        return RelationshipRow(
            user_id=row["user_id"],
            score=float(row["score"]),
            keyword_count=int(row["keyword_count"]),
            dialogue_days=int(row["dialogue_days"]),
            happy_count=int(row["happy_count"]),
            romance_status=RelationshipStatus(row["romance_status"]),
            version=int(row["version"]),
        )

    def ensure(self, user_id: str) -> RelationshipRow:
        """建立档案（初始 0 与未恋爱），已存在则原样返回。"""
        existing = self.database.query_one(
            "SELECT user_id FROM relationship WHERE user_id = ?", (user_id,)
        )
        if existing is None:
            now = to_utc_iso(utc_now())
            self.database.execute(
                """
                INSERT INTO relationship
                    (user_id, score, keyword_count, dialogue_days, happy_count,
                     romance_status, version, created_at, updated_at)
                VALUES (?, 0, 0, 0, 0, 'non_romantic', 0, ?, ?)
                """,
                (user_id, now, now),
            )
        return self.get(user_id)

    # ------------------------------------------------------------------ 写入

    def register_dialogue_day(self, *, user_id: str, local_date: str, turn_id: str) -> bool:
        """插入当日去重记录；已存在返回 False（同日只计一次）。"""
        cursor = self.database.execute(
            """
            INSERT OR IGNORE INTO dialogue_day (user_id, local_date, first_turn_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, local_date, turn_id, to_utc_iso(utc_now())),
        )
        return bool(cursor.rowcount)

    def update_score(
        self,
        *,
        user_id: str,
        delta: float,
        keyword_hits: int,
        new_day: bool,
        happy_delta: bool,
        romance_status: RelationshipStatus,
        config_version: int,
    ) -> RelationshipRow:
        current = self.ensure(user_id)
        score_after = max(0.0, current.score + delta)
        romance = (
            RelationshipStatus.ROMANTIC
            if current.romance_status is RelationshipStatus.ROMANTIC or romance_status is RelationshipStatus.ROMANTIC
            else RelationshipStatus.NON_ROMANTIC
        )
        self.database.execute(
            """
            UPDATE relationship
            SET score = ?, keyword_count = keyword_count + ?, dialogue_days = dialogue_days + ?,
                happy_count = happy_count + ?, romance_status = ?, version = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                score_after,
                keyword_hits,
                1 if new_day else 0,
                1 if happy_delta else 0,
                romance.value,
                config_version,
                to_utc_iso(utc_now()),
                user_id,
            ),
        )
        return self.get(user_id)

    def insert_event(
        self,
        *,
        delivery_id: str,
        turn_id: str,
        user_id: str,
        day: str,
        keyword_hits: int,
        new_day: bool,
        happy_delta: bool,
        weights: Mapping[str, float],
        delta: float,
        score_before: float,
        score_after: float,
        romance_before: RelationshipStatus,
        romance_after: RelationshipStatus,
        config_version: int,
    ) -> str:
        event_id = new_id()
        self.database.execute(
            """
            INSERT INTO affection_event
                (id, delivery_id, turn_id, user_id, day, keyword_hits, new_day, happy_delta,
                 weights, delta, score_before, score_after, romance_before, romance_after,
                 config_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                delivery_id,
                turn_id,
                user_id,
                day,
                keyword_hits,
                1 if new_day else 0,
                1 if happy_delta else 0,
                json.dumps(dict(weights), ensure_ascii=False),
                delta,
                score_before,
                score_after,
                RelationshipStatus(romance_before).value,
                RelationshipStatus(romance_after).value,
                config_version,
                to_utc_iso(utc_now()),
            ),
        )
        return event_id

    def event_for_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT * FROM affection_event WHERE delivery_id = ?", (delivery_id,)
        )
        return dict(row) if row else None

    def events_for(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM affection_event WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    def days_for(self, user_id: str) -> list[str]:
        rows = self.database.query(
            "SELECT local_date FROM dialogue_day WHERE user_id = ? ORDER BY local_date", (user_id,)
        )
        return [row["local_date"] for row in rows]

    def store_config(self, config: RuntimeConfig, *, effective_at: str) -> int:
        version = 1 + int(
            (
                self.database.query_one("SELECT COALESCE(MAX(version), 0) AS v FROM relationship_config")
                or {"v": 0}
            )["v"]
        )
        self.database.execute(
            """
            INSERT INTO relationship_config
                (version, keyword_rules, weights, romance_threshold, normalization_version, effective_at)
            VALUES (?, ?, ?, ?, 'v1', ?)
            """,
            (
                version,
                json.dumps(list(config.keywords), ensure_ascii=False),
                json.dumps(config.affection_weights.to_payload(), ensure_ascii=False),
                config.romance_threshold,
                effective_at,
            ),
        )
        return version

    def latest_config(self) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT * FROM relationship_config ORDER BY version DESC LIMIT 1"
        )
        return dict(row) if row else None
