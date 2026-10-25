"""基础阶段验收：配置原子生效、数据库约束与回滚、日志脱敏、队列隔离。

并发与超时使用事件屏障与极小超时，不用真实 sleep 制造竞态。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from sana_bot.config import AffectionWeights, ConfigStore, RuntimeConfig
from sana_bot.diagnostics import (
    DiagnosticRecord,
    LoggingRecorder,
    ResilientRecorder,
    redact,
    sanitize_payload,
)
from sana_bot.enums import DiagnosticCategory, InboundStatus
from sana_bot.errors import ConfigError
from sana_bot.scheduling.serial_queue import (
    CapacityExceeded,
    InboundQueueService,
    ItemTimeout,
    QueueItem,
    UserSerialQueue,
)
from sana_bot.storage.database import Database, WriteTransactionActive
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.timeutil import parse_utc
from tests.support import FailingDiagnostics, make_event


# --------------------------------------------------------------------------- 配置


def test_default_config_is_valid() -> None:
    config = RuntimeConfig()
    assert config.issues() == []
    assert config.validate() is config
    assert config.keywords == ()
    assert config.monologue_probability == pytest.approx(0.30)
    assert config.qq_passive_window_seconds == 300
    assert config.qq_safety_margin_seconds == 10


@pytest.mark.parametrize(
    "weights",
    [
        AffectionWeights(keyword=0, day=2, happy=5),
        AffectionWeights(keyword=1, day=0, happy=5),
        AffectionWeights(keyword=1, day=2, happy=0),
        AffectionWeights(keyword=5, day=2, happy=5),
        AffectionWeights(keyword=1, day=6, happy=5),
        AffectionWeights(keyword=6, day=6, happy=5),
        AffectionWeights(keyword=-1, day=2, happy=5),
    ],
)
def test_weights_must_be_positive_and_happy_largest(weights: AffectionWeights) -> None:
    assert weights.issues()


def test_invalid_updates_keep_previous_version(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    applied = store.apply_payload(
        {"romance_threshold": 120, "keywords": ["谢谢"]},
        source="test",
    )
    assert applied.version == 1
    assert applied.romance_threshold == 120

    for invalid in (
        {"monologue_probability": 1.5},
        {"monologue_wait_seconds": 0},
        {"affection_weights": {"happy": 1}},
    ):
        with pytest.raises(ConfigError):
            store.apply_payload(invalid, source="test")

    reloaded = store.load()
    assert reloaded.version == 1
    assert reloaded.romance_threshold == 120
    assert reloaded.monologue_probability == pytest.approx(0.30)


def test_empty_keyword_list_is_legal_and_duplicates_rejected() -> None:
    assert RuntimeConfig(keywords=()).issues() == []
    assert RuntimeConfig(keywords=("a", "b")).issues() == []
    assert RuntimeConfig(keywords=("a", "a")).issues()


def test_unknown_config_field_rejected() -> None:
    with pytest.raises(ConfigError):
        RuntimeConfig.from_payload({"unknown_field": 1})


def test_config_store_partial_update_preserves_other_fields(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.apply_payload({"keywords": ["谢谢"], "romance_threshold": 50}, source="t")
    updated = store.apply_payload({"monologue_probability": 0.5}, source="t")
    assert updated.version == 2
    assert updated.keywords == ("谢谢",)
    assert updated.romance_threshold == 50
    assert updated.monologue_probability == pytest.approx(0.5)


# --------------------------------------------------------------------------- 数据库


def test_user_identity_is_unique_per_app(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    with base_database.write():
        first = repository.ensure_user("app-a", "openid-1")
        second = repository.ensure_user("app-a", "openid-1")
    assert first == second

    with pytest.raises(sqlite3.IntegrityError):
        base_database.execute(
            "INSERT INTO user_identity (id, app_id, user_openid, created_at) VALUES (?, ?, ?, ?)",
            ("dup", "app-a", "openid-1", "2026-10-16T00:00:00+00:00"),
        )


def test_only_one_active_conversation_per_user(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    with base_database.write():
        user_id = repository.ensure_user("app-a", "openid-1")
        repository.ensure_conversation(user_id)
        assert repository.active_conversation_id(user_id) is not None
        with pytest.raises(sqlite3.IntegrityError):
            base_database.execute(
                """
                INSERT INTO conversation (id, user_id, generation, scene, session_status,
                                          created_at, updated_at)
                VALUES (?, ?, 0, 'unknown', 'active', ?, ?)
                """,
                ("second", user_id, "2026-10-16T00:00:00+00:00", "2026-10-16T00:00:00+00:00"),
            )


def test_failed_transaction_rolls_back(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    with pytest.raises(sqlite3.IntegrityError):
        with base_database.write():
            repository.ensure_user("app-a", "openid-1")
            base_database.execute(
                "INSERT INTO user_identity (id, app_id, user_openid, created_at) VALUES (?, ?, ?, ?)",
                ("dup", "app-a", "openid-1", "2026-10-16T00:00:00+00:00"),
            )
    assert base_database.query("SELECT * FROM user_identity") == []


def test_inbound_dedup_by_event_key_and_message_id(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    event = make_event(message_id="m-1", event_key="e-1")
    with base_database.write():
        first = repository.persist_inbound(event)
        replay = repository.persist_inbound(event)
    assert first.accepted and not first.duplicate
    assert replay.duplicate and not replay.accepted

    with base_database.write():
        same_message = repository.persist_inbound(make_event(message_id="m-1", event_key="e-2"))
    assert same_message.duplicate
    assert base_database.query("SELECT COUNT(*) AS n FROM inbound_event")[0]["n"] == 1
    assert base_database.query("SELECT COUNT(*) AS n FROM turn")[0]["n"] == 0


def test_generation_increments_only_for_new_events(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    with base_database.write():
        first = repository.persist_inbound(make_event(message_id="m-1", event_key="e-1"))
        repository.persist_inbound(make_event(message_id="m-1", event_key="e-1"))
        second = repository.persist_inbound(make_event(message_id="m-2", event_key="e-2"))
    assert first.generation == 1
    assert second.generation == 2


def test_idempotency_key_reuse_with_different_content_is_flagged(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    with base_database.write():
        repository.persist_inbound(make_event(message_id="m-1", event_key="e-1", text="你好"))
        conflict = repository.persist_inbound(
            make_event(message_id="m-1", event_key="e-1", text="不同内容")
        )
    assert conflict.idempotency_conflict
    assert conflict.reason_code == "idempotency_conflict"


def test_chat_day_follows_received_time_in_shanghai(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    late = make_event(
        message_id="m-9",
        event_time=parse_utc("2026-10-16T15:59:59+00:00"),
        received_at=parse_utc("2026-10-16T15:59:59+00:00"),
    )
    with base_database.write():
        outcome = repository.persist_inbound(late)
    # 15:59:59Z 已是 Asia/Shanghai 的次日
    assert outcome.chat_day == "2026-10-16"
    assert outcome.chat_day == repository.inbound_row(outcome.inbound_id or "").chat_day  # type: ignore[union-attr]


def test_external_calls_rejected_inside_write_transaction(base_database: Database) -> None:
    with base_database.write():
        with pytest.raises(WriteTransactionActive):
            base_database.assert_outside_write_transaction(what="模型调用")


# --------------------------------------------------------------------------- 诊断


def test_redaction_removes_secrets_and_credential_patterns() -> None:
    secret = "sk-abcdef1234567890"
    text = f"key={secret} token=access_token:xyz Authorization: QQBot abcdefgh"
    masked = redact(text, secrets=[secret, "abcdefgh"])
    assert secret not in masked
    assert "xyz" not in masked
    assert masked.count("[已脱敏]") >= 3


def test_forbidden_keys_never_reach_log_payload() -> None:
    payload = sanitize_payload(
        {
            "prompt": "完整提示词",
            "reasoning": "推理内容",
            "api_key": "sk-secret",
            "nested": {"access_token": "t", "score": 0.4},
            "items": [{"reasoning": "x"}],
        }
    )
    assert payload["prompt"] == "[已脱敏]"
    assert payload["reasoning"] == "[已脱敏]"
    assert payload["api_key"] == "[已脱敏]"
    assert payload["nested"]["access_token"] == "[已脱敏]"
    assert payload["nested"]["score"] == pytest.approx(0.4)
    assert payload["items"][0]["reasoning"] == "[已脱敏]"


def test_logging_fallback_emits_signal(caplog: pytest.LogCaptureFixture) -> None:
    recorder = LoggingRecorder(secrets=["sk-secret"])
    record = DiagnosticRecord(
        category=DiagnosticCategory.LOW_MATCH,
        correlation_id="c-1",
        reason="低于门槛",
        payload={"threshold": 0.5},
    )
    with caplog.at_level(logging.WARNING, logger="sana_bot.diagnostics"):
        outcome = recorder.record(record)
    assert outcome.recorded is False
    assert outcome.fallback_signal is True
    assert "low_match" in caplog.text


def test_resilient_recorder_reports_failure_without_raising(caplog: pytest.LogCaptureFixture) -> None:
    recorder = ResilientRecorder(FailingDiagnostics(), secrets=["sk-secret"])
    record = DiagnosticRecord(category=DiagnosticCategory.LOGGING_ERROR, correlation_id="c-9")
    with caplog.at_level(logging.WARNING, logger="sana_bot.diagnostics"):
        outcome = recorder.record(record)
    assert outcome.recorded is False
    assert outcome.fallback_signal is True
    assert outcome.reason_code == DiagnosticCategory.LOGGING_ERROR.value
    assert "diagnostic_fallback" in caplog.text


# --------------------------------------------------------------------------- 串行队列


def _item(user_id: str, inbound_id: str = "i-1") -> QueueItem:
    stamp = parse_utc("2026-10-16T12:00:00+00:00")
    return QueueItem(
        inbound_id=inbound_id,
        user_id=user_id,
        conversation_id=f"c-{user_id}",
        generation=1,
        text="你好",
        chat_day="2026-10-16",
        received_at=stamp,
        event_time=stamp,
        correlation_id=inbound_id,
    )


async def test_same_user_is_serialized_and_ordered() -> None:
    order: list[str] = []
    queue = UserSerialQueue(capacity_per_user=8, item_timeout_seconds=1.0)
    release = asyncio.Event()

    async def handler(item: QueueItem) -> str:
        order.append(f"start:{item.inbound_id}")
        await release.wait()
        order.append(f"end:{item.inbound_id}")
        return item.inbound_id

    await queue.start(handler)
    first = asyncio.create_task(queue.put(_item("u1", "i-1"), follow=True))
    second = asyncio.create_task(queue.put(_item("u1", "i-2"), follow=True))
    await asyncio.wait_for(asyncio.sleep(0), timeout=1)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert results == ["i-1", "i-2"]
    assert order[:2] == ["start:i-1", "end:i-1"]
    await queue.stop()


async def test_different_users_run_concurrently() -> None:
    """u1 必须等 u2 的处理器运行后才能结束：串行实现会在此超时。"""
    queue = UserSerialQueue(capacity_per_user=4, item_timeout_seconds=1.0)
    barrier = asyncio.Event()

    async def handler(item: QueueItem) -> str:
        if item.user_id == "u1":
            await asyncio.wait_for(barrier.wait(), timeout=1)
        else:
            barrier.set()
        return item.user_id

    await queue.start(handler)
    first = asyncio.create_task(queue.put(_item("u1"), follow=True))
    await asyncio.sleep(0)
    second = asyncio.create_task(queue.put(_item("u2"), follow=True))
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert results == ["u1", "u2"]
    await queue.stop()


async def test_capacity_exceeded_is_explicit() -> None:
    queue = UserSerialQueue(capacity_per_user=1, item_timeout_seconds=1.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(item: QueueItem) -> str:
        started.set()
        await release.wait()
        return item.inbound_id

    await queue.start(handler)
    await queue.put(_item("u1", "i-1"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await queue.put(_item("u1", "i-2"))
    with pytest.raises(CapacityExceeded):
        await queue.put(_item("u1", "i-3"))
    release.set()
    await asyncio.wait_for(queue.join("u1"), timeout=1)
    await queue.stop()


async def test_item_timeout_is_reported() -> None:
    queue = UserSerialQueue(capacity_per_user=2, item_timeout_seconds=0.05)
    never = asyncio.Event()

    async def handler(item: QueueItem) -> str:
        await never.wait()
        return item.inbound_id

    await queue.start(handler)
    with pytest.raises(ItemTimeout):
        await queue.put(_item("u1"), follow=True)
    await queue.stop()


async def test_duplicate_and_unsupported_events_create_no_turn(
    base_database: Database,
) -> None:
    repository = InboundRepository(base_database)
    queue = UserSerialQueue(capacity_per_user=4, item_timeout_seconds=1.0)
    handled: list[str] = []

    async def handler(item: QueueItem) -> None:
        handled.append(item.inbound_id)

    await queue.start(handler)
    service = InboundQueueService(database=base_database, repository=repository, queue=queue)

    first = await service.accept(make_event(message_id="m-1", event_key="e-1"))
    assert first.queued and first.accepted
    replay = await service.accept(make_event(message_id="m-1", event_key="e-1"))
    assert replay.duplicate and not replay.queued
    unsupported = await service.accept(
        make_event(message_id="m-2", event_key="e-2", supported=False)
    )
    assert unsupported.ignored and not unsupported.queued
    await asyncio.wait_for(queue.join(), timeout=1)

    assert handled == [first.inbound_id]
    rows = base_database.query("SELECT status FROM inbound_event")
    statuses = sorted(row["status"] for row in rows)
    assert statuses == [InboundStatus.IGNORED.value, InboundStatus.QUEUED.value]
    await queue.stop()


async def test_capacity_deferral_keeps_event_recoverable(base_database: Database) -> None:
    repository = InboundRepository(base_database)
    queue = UserSerialQueue(capacity_per_user=1, item_timeout_seconds=1.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(item: QueueItem) -> None:
        started.set()
        await release.wait()

    await queue.start(handler)
    service = InboundQueueService(database=base_database, repository=repository, queue=queue)
    first = await service.accept(make_event(message_id="m-1", event_key="e-1"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await service.accept(make_event(message_id="m-2", event_key="e-2"))
    deferred = await service.accept(make_event(message_id="m-3", event_key="e-3"))
    assert deferred.accepted and not deferred.queued
    assert deferred.reason_code == CapacityExceeded.reason_code
    assert deferred.details["deferred"] is True

    release.set()
    await asyncio.wait_for(queue.join("u1"), timeout=1)
    await queue.stop()

    rows = {row["message_id"]: row["status"] for row in base_database.query("SELECT * FROM inbound_event")}
    assert rows["m-3"] == InboundStatus.RECEIVED.value
    assert first.inbound_id is not None


def _iter_nothing() -> Iterator[None]:  # pragma: no cover - 保持导入整洁
    return iter(())
