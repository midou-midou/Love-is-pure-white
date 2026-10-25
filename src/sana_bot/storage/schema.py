"""基础表结构（用户、会话、入站事件、轮次）。

唯一键与检查约束直接落实 data-model.md 的逐字不变量：
``(app_id,user_openid)`` 唯一且不假设 openid 等于 QQ 号；每用户一个活动会话；
外层事件 id 与消息 id 分开且各自去重；``inbound_id`` 唯一。
"""

from __future__ import annotations

from sana_bot.config import RuntimeConfig
from sana_bot.storage.database import Migration

__all__ = ["Migration", "base_migrations", "all_migrations"]


BASE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        name="0001_base_entities",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS user_identity (
                id            TEXT PRIMARY KEY,
                app_id        TEXT NOT NULL,
                user_openid   TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                UNIQUE (app_id, user_openid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS conversation (
                id              TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL REFERENCES user_identity(id),
                generation      INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
                scene           TEXT NOT NULL DEFAULT 'unknown'
                                CHECK (scene IN ('unknown', 'public', 'private')),
                session_status  TEXT NOT NULL DEFAULT 'active'
                                CHECK (session_status IN ('active', 'closed')),
                last_inbound_at TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """,
            # 每用户只有一个活动会话
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_active_user
                ON conversation(user_id) WHERE session_status = 'active'
            """,
            """
            CREATE TABLE IF NOT EXISTS inbound_event (
                id                TEXT PRIMARY KEY,
                app_id            TEXT NOT NULL,
                event_key         TEXT NOT NULL,
                platform_event_id TEXT,
                message_id        TEXT NOT NULL,
                user_id           TEXT NOT NULL REFERENCES user_identity(id),
                conversation_id   TEXT NOT NULL REFERENCES conversation(id),
                text              TEXT NOT NULL DEFAULT '',
                payload_hash      TEXT NOT NULL,
                received_at       TEXT NOT NULL,
                event_time        TEXT NOT NULL,
                chat_day          TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'received'
                                  CHECK (status IN ('received','queued','processing',
                                                    'completed','failed','expired','ignored')),
                correlation_id    TEXT,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                UNIQUE (event_key),
                UNIQUE (app_id, message_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inbound_status
                ON inbound_event(status, received_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS turn (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversation(id),
                inbound_id      TEXT NOT NULL REFERENCES inbound_event(id),
                input_text      TEXT NOT NULL,
                snapshot_id     TEXT,
                index_id        TEXT,
                status          TEXT NOT NULL DEFAULT 'processing',
                chat_day        TEXT NOT NULL,
                correlation_id  TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                UNIQUE (inbound_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_config (
                version      INTEGER PRIMARY KEY,
                effective_at TEXT NOT NULL,
                source       TEXT NOT NULL,
                payload      TEXT NOT NULL
            )
            """,
        ),
    ),
)


def base_migrations() -> tuple[Migration, ...]:
    """基础表迁移；不含资料索引与业务实体表。"""
    return BASE_MIGRATIONS


def all_migrations(config: RuntimeConfig | None = None) -> tuple[Migration, ...]:
    """全部迁移（基础表 + 各仓储拥有的业务表），按固定顺序返回。"""
    from sana_bot.storage import (
        delivery_repository,
        diagnostic_repository,
        index_schema,
        monologue_repository,
        relationship_repository,
        state_repository,
    )
    ordered = [
        base_migrations(),
        index_schema.migrations(),
        state_repository.migrations(),
        delivery_repository.migrations(),
        diagnostic_repository.migrations(),
        relationship_repository.migrations(),
        monologue_repository.migrations(config),
    ]
    return tuple(migration for group in ordered for migration in group)
