"""状态快照与审计仓储。

快照按轮冻结（``turn_id`` 唯一）；管理操作留下审计记录，短期状态不写入长期关系。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sana_bot.config import RuntimeConfig
from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.ingest.models import canonical_json
from sana_bot.state.models import StateSnapshot
from sana_bot.storage.database import Database, Migration
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = ["StateRepository", "migrations", "new_id"]


def new_id() -> str:
    return uuid.uuid4().hex


def migrations(config: RuntimeConfig | None = None) -> tuple[Migration, ...]:
    return (
        Migration(
            name="0040_state",
            statements=(
                """
                CREATE TABLE IF NOT EXISTS state_snapshot (
                    id                  TEXT PRIMARY KEY,
                    turn_id             TEXT NOT NULL,
                    conversation_id     TEXT NOT NULL,
                    relationship_status TEXT NOT NULL DEFAULT 'non_romantic',
                    affection_before    REAL NOT NULL DEFAULT 0,
                    primary_mood        TEXT NOT NULL DEFAULT 'neutral'
                                        CHECK (primary_mood IN ('neutral','happy','angry','sad')),
                    secondary_tags      TEXT NOT NULL DEFAULT '[]',
                    dominant_state      TEXT NOT NULL DEFAULT '',
                    scene               TEXT NOT NULL DEFAULT 'unknown'
                                        CHECK (scene IN ('unknown','public','private')),
                    catalog_version     TEXT NOT NULL DEFAULT '',
                    transition_evidence TEXT NOT NULL DEFAULT '[]',
                    source              TEXT NOT NULL DEFAULT 'turn',
                    created_at          TEXT NOT NULL,
                    UNIQUE (turn_id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS state_override (
                    user_id    TEXT PRIMARY KEY,
                    mood       TEXT NOT NULL,
                    tags       TEXT NOT NULL DEFAULT '[]',
                    scene      TEXT,
                    actor      TEXT NOT NULL DEFAULT 'maintainer',
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS state_audit (
                    id           TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    action       TEXT NOT NULL,
                    before_state TEXT NOT NULL DEFAULT '{}',
                    after_state  TEXT NOT NULL DEFAULT '{}',
                    actor        TEXT NOT NULL DEFAULT 'maintainer',
                    reason       TEXT NOT NULL DEFAULT '',
                    created_at   TEXT NOT NULL
                )
                """,
            ),
        ),
    )


@dataclass(frozen=True)
class SnapshotRow:
    id: str
    turn_id: str
    conversation_id: str
    primary_mood: str
    secondary_tags: tuple[str, ...]
    dominant_state: str
    scene: str
    created_at: str


class StateRepository:
    """状态快照与审计记录。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def save_snapshot(self, snapshot: StateSnapshot) -> None:
        self.database.execute(
            """
            INSERT OR REPLACE INTO state_snapshot
                (id, turn_id, conversation_id, relationship_status, affection_before,
                 primary_mood, secondary_tags, dominant_state, scene, catalog_version,
                 transition_evidence, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.id,
                snapshot.turn_id,
                snapshot.conversation_id,
                snapshot.relationship_status.value,
                snapshot.affection_before,
                snapshot.primary_mood.value,
                json.dumps(list(snapshot.secondary_tags), ensure_ascii=False),
                snapshot.dominant_state,
                snapshot.scene.value,
                snapshot.catalog_version,
                json.dumps(
                    [dict(item) for item in snapshot.transition_evidence], ensure_ascii=False
                ),
                snapshot.source,
                snapshot.created_at or to_utc_iso(utc_now()),
            ),
        )

    def snapshot_for_turn(self, turn_id: str) -> StateSnapshot | None:
        row = self.database.query_one("SELECT * FROM state_snapshot WHERE turn_id = ?", (turn_id,))
        return self._snapshot(row) if row else None

    def latest_snapshot(self, conversation_id: str) -> StateSnapshot | None:
        row = self.database.query_one(
            """
            SELECT * FROM state_snapshot WHERE conversation_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (conversation_id,),
        )
        return self._snapshot(row) if row else None

    def recent_snapshots(self, conversation_id: str, *, limit: int = 3) -> list[SnapshotRow]:
        rows = self.database.query(
            """
            SELECT * FROM state_snapshot WHERE conversation_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT ?
            """,
            (conversation_id, limit),
        )
        return [
            SnapshotRow(
                id=row["id"],
                turn_id=row["turn_id"],
                conversation_id=row["conversation_id"],
                primary_mood=row["primary_mood"],
                secondary_tags=tuple(json.loads(row["secondary_tags"])),
                dominant_state=row["dominant_state"],
                scene=row["scene"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ 管理设置

    def set_override(
        self,
        *,
        user_id: str,
        mood: str,
        tags: tuple[str, ...],
        scene: str | None,
        actor: str = "maintainer",
    ) -> None:
        self.database.execute(
            """
            INSERT OR REPLACE INTO state_override (user_id, mood, tags, scene, actor, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                mood,
                json.dumps(list(tags), ensure_ascii=False),
                scene,
                actor,
                to_utc_iso(utc_now()),
            ),
        )

    def get_override(self, user_id: str) -> dict[str, Any] | None:
        row = self.database.query_one("SELECT * FROM state_override WHERE user_id = ?", (user_id,))
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "mood": row["mood"],
            "tags": tuple(json.loads(row["tags"])),
            "scene": row["scene"],
            "actor": row["actor"],
            "updated_at": row["updated_at"],
        }

    def clear_overrides(self) -> int:
        """服务重启后短期状态回到 neutral：清空全部管理覆盖。"""
        cursor = self.database.execute("DELETE FROM state_override")
        return int(cursor.rowcount or 0)

    def record_change(
        self,
        *,
        user_id: str,
        action: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        actor: str = "maintainer",
        reason: str = "",
    ) -> None:
        self.database.execute(
            """
            INSERT INTO state_audit
                (id, user_id, action, before_state, after_state, actor, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                user_id,
                action,
                canonical_json(before),
                canonical_json(after),
                actor,
                reason,
                to_utc_iso(utc_now()),
            ),
        )

    def audits(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.database.query(
            "SELECT * FROM state_audit WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _snapshot(row: Any) -> StateSnapshot:
        return StateSnapshot(
            id=row["id"],
            turn_id=row["turn_id"],
            conversation_id=row["conversation_id"],
            relationship_status=RelationshipStatus(row["relationship_status"]),
            affection_before=float(row["affection_before"]),
            primary_mood=PrimaryMood(row["primary_mood"]),
            secondary_tags=tuple(json.loads(row["secondary_tags"])),
            dominant_state=row["dominant_state"],
            scene=Scene(row["scene"]),
            catalog_version=row["catalog_version"],
            transition_evidence=tuple(json.loads(row["transition_evidence"])),
            created_at=row["created_at"],
            source=row["source"],
        )
