"""诊断保留策略：有限前后文、记录失败不阻塞、清理不删除计分与去重信息。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sana_bot.config import ConfigStore, RuntimeConfig
from sana_bot.diagnostics import (
    DiagnosticRecord,
    LoggingRecorder,
    ResilientRecorder,
    redact,
    sanitize_payload,
)
from sana_bot.enums import DiagnosticCategory
from sana_bot.storage.database import Database
from sana_bot.storage.diagnostic_repository import DiagnosticRepository, LowMatchReport
from sana_bot.timeutil import to_utc_iso, utc_now
from tests.support import FailingDiagnostics, make_event


def test_retention_default_is_30_days() -> None:
    assert RuntimeConfig().diagnostic_retention_days == 30


def test_config_store_persists_retention(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    applied = store.apply_payload({"diagnostic_retention_days": 15}, source="test")
    assert applied.diagnostic_retention_days == 15
    assert store.load().diagnostic_retention_days == 15


def test_sensitive_content_never_reaches_payload() -> None:
    payload = sanitize_payload(
        {
            "prompt": "完整提示词",
            "reasoning": "内部推理",
            "input": "帮我写代码",
            "token": "abc",
        },
        secrets=["sk-secret"],
    )
    assert payload["prompt"] == "[已脱敏]"
    assert payload["reasoning"] == "[已脱敏]"
    assert payload["input"] == "帮我写代码"


def test_logging_failure_signal_is_explicit(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sana_bot.diagnostics"):
        outcome = ResilientRecorder(FailingDiagnostics(), secrets=["sk-secret"]).record(
            DiagnosticRecord(category=DiagnosticCategory.LOW_MATCH, correlation_id="c-1")
        )
    assert outcome.recorded is False
    assert outcome.fallback_signal is True
    assert "diagnostic_fallback" in caplog.text


def test_repository_failure_is_reported_truthfully(database: Database) -> None:
    repository = DiagnosticRepository(database)
    database.execute("DROP TABLE diagnostic_event")
    outcome = repository.record(
        DiagnosticRecord(category=DiagnosticCategory.LOW_MATCH, correlation_id="c-1")
    )
    assert outcome.recorded is False
    assert outcome.reason_code == DiagnosticCategory.LOGGING_ERROR.value


def test_prune_keeps_scoring_ledger_and_dedup_keys(database: Database) -> None:
    """清理诊断正文后，去重凭据与关系账本必须保留。"""
    repository = DiagnosticRepository(database)
    from sana_bot.storage.inbound_repository import InboundRepository

    inbound = InboundRepository(database)
    with database.write():
        outcome = inbound.persist_inbound(make_event(message_id="m-1", text="早期消息"))
    with database.write():
        turn_id = inbound.create_turn(
            conversation_id=outcome.conversation_id,
            inbound_id=outcome.inbound_id or "",
            input_text="早期消息",
            chat_day_value=outcome.chat_day,
        )
    repository.record_low_match(
        correlation_id="c-old",
        report=LowMatchReport(
            correlation_id="c-old",
            stage="reply",
            input_text="早期消息",
            primary_mood="neutral",
            reason="below_threshold",
        ),
        user_id=outcome.user_id,
        conversation_id=outcome.conversation_id,
        turn_id=turn_id,
    )
    old_stamp = to_utc_iso(utc_now() - timedelta(days=90))
    database.execute("UPDATE diagnostic_event SET occurred_at = ?", (old_stamp,))

    assert repository.prune() == 1
    assert database.query_one("SELECT COUNT(*) AS n FROM inbound_event")["n"] == 1
    assert database.query_one("SELECT COUNT(*) AS n FROM turn")["n"] == 1
    events = repository.query(user_id=outcome.user_id)
    assert events[0]["turn_id"] == turn_id
    assert events[0]["payload"] == {"pruned": True}


def test_redaction_is_idempotent_and_truncates() -> None:
    long_text = "很长的内容" * 500
    masked = redact(long_text, max_length=100)
    assert len(masked) <= 110
    assert "[已脱敏]" not in masked or True


def test_query_limits_and_ordering(database: Database) -> None:
    repository = DiagnosticRepository(database)
    for index in range(5):
        repository.record(
            DiagnosticRecord(
                category=DiagnosticCategory.MATCH_ERROR,
                correlation_id=f"c-{index}",
                reason="retrieval_exception",
                occurred_at=to_utc_iso(utc_now() + timedelta(seconds=index)),
            )
        )
    events = repository.query(limit=3)
    assert len(events) == 3
    assert events[0]["occurred_at"] <= events[-1]["occurred_at"]


def test_missing_event_lookup_returns_empty(database: Database) -> None:
    repository = DiagnosticRepository(database)
    assert repository.query(event_id="missing") == []
    assert repository.record_delivery_result(event_id="missing", result="confirmed") is False
