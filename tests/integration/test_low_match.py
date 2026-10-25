"""低匹配与调优日志：分支正确、字段完整、前后文可关联且不阻塞兜底。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.match_decision import decide_match
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import (
    DiagnosticCategory,
    MatchDecision,
    RetrievalStage,
)
from sana_bot.errors import ConfigError
from sana_bot.ports import ModelBudget, RetrievalResult
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.diagnostic_repository import DiagnosticRepository, LowMatchReport
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from tests.support import FakeModel, FakeQQTransport, make_event
from tests.support.index_fixture import build_indexed_fixture
from sana_bot.cli_logs import run_prune, run_query
from datetime import UTC, datetime

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- 决策分支


def _result(decision: MatchDecision, *, scores=(), threshold=0.5, reason=None):
    from sana_bot.ports import RetrievedCandidate

    return RetrievalResult(
        stage=RetrievalStage.REPLY,
        decision=decision,
        candidates=tuple(
            RetrievedCandidate(unit_id=f"u{index}", score=score)
            for index, score in enumerate(scores)
        ),
        threshold=threshold,
        reason_code=reason,
    )


def test_below_threshold_triggers_confused_fallback() -> None:
    outcome = decide_match(_result(MatchDecision.BELOW_THRESHOLD, scores=(0.2, 0.1)))
    assert not outcome.usable
    assert outcome.fallback_kind == "confused"
    assert outcome.reason_code == "below_threshold"


def test_empty_candidates_are_low_match() -> None:
    outcome = decide_match(_result(MatchDecision.EMPTY_CANDIDATES))
    assert outcome.reason_code == "empty_candidates"
    assert outcome.fallback_kind == "confused"


def test_equal_threshold_is_a_match() -> None:
    outcome = decide_match(_result(MatchDecision.MATCHED, scores=(0.5,), threshold=0.5))
    assert outcome.usable
    assert outcome.decision is MatchDecision.MATCHED


def test_no_material_is_not_a_low_match() -> None:
    outcome = decide_match(_result(MatchDecision.NO_MATERIAL, threshold=None))
    assert outcome.decision is MatchDecision.NO_MATERIAL
    assert outcome.reason_code == "no_material"


def test_processing_error_keeps_its_own_reason() -> None:
    outcome = decide_match(_result(MatchDecision.PROCESSING_ERROR, reason="retrieval_exception"))
    assert outcome.decision is MatchDecision.PROCESSING_ERROR
    assert outcome.reason_code == "retrieval_exception"
    assert outcome.fallback_kind is None


def test_monologue_low_match_stays_silent() -> None:
    outcome = decide_match(
        _result(MatchDecision.BELOW_THRESHOLD, scores=(0.1,)), stage=RetrievalStage.MONOLOGUE
    )
    assert outcome.silent
    assert outcome.fallback_kind is None


# --------------------------------------------------------------------------- 诊断字段


def test_low_match_record_keeps_required_fields(database: Database) -> None:
    repository = DiagnosticRepository(database)
    report = LowMatchReport(
        correlation_id="c-1",
        stage="reply",
        input_text="帮我写代码",
        primary_mood="neutral",
        candidate_sources=(),
        threshold=0.55,
        index_version="index-1",
        model_version="fake/model@rev",
        config_version=3,
        reason="below_threshold",
        decision="fallback",
        delivery_result="confirmed",
    )
    outcome = repository.record_low_match(
        correlation_id="c-1", report=report, user_id="u1", conversation_id="c1", turn_id="t1"
    )
    assert outcome.recorded
    events = repository.query(user_id="u1")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["candidates"] == []
    assert payload["threshold"] == pytest.approx(0.55)
    assert payload["input"] == "帮我写代码"
    assert payload["delivery_result"] == "confirmed"
    assert events[0]["type"] == DiagnosticCategory.LOW_MATCH.value


def test_events_are_isolated_per_user_and_reason(database: Database) -> None:
    repository = DiagnosticRepository(database)
    for user in ("u1", "u2"):
        repository.record_low_match(
            correlation_id=f"c-{user}",
            report=LowMatchReport(
                correlation_id=f"c-{user}",
                stage="reply",
                input_text="x",
                primary_mood="neutral",
                reason="below_threshold",
            ),
            user_id=user,
        )
    assert len(repository.query(user_id="u1")) == 1
    assert len(repository.query(reason="below_threshold")) == 2
    assert repository.count(category=DiagnosticCategory.LOW_MATCH) == 2


def test_context_links_use_turn_ids(database: Database) -> None:
    repository = DiagnosticRepository(database)
    inbound = InboundRepository(database)
    with database.write():
        outcome = inbound.persist_inbound(make_event(message_id="m-1", text="第一轮"))
        conversation_id = outcome.conversation_id
    first_turn = None
    with database.write():
        first_turn = inbound.create_turn(
            conversation_id=conversation_id,
            inbound_id=outcome.inbound_id or "",
            input_text="第一轮",
            chat_day_value=outcome.chat_day,
        )
        for index in range(2, 5):
            event = make_event(message_id=f"m-{index}", text=f"第{index}轮")
            follow = inbound.persist_inbound(event)
            inbound.create_turn(
                conversation_id=conversation_id,
                inbound_id=follow.inbound_id or "",
                input_text=f"第{index}轮",
                chat_day_value=follow.chat_day,
            )
    repository.record_low_match(
        correlation_id="c-1",
        report=LowMatchReport(
            correlation_id="c-1",
            stage="reply",
            input_text="第一轮",
            primary_mood="neutral",
            reason="below_threshold",
        ),
        user_id="u1",
        conversation_id=conversation_id,
        turn_id=first_turn,
    )
    events = repository.with_context(user_id="u1")
    assert len(events) == 1
    assert [item["input"] for item in events[0]["after"]] == ["第2轮", "第3轮", "第4轮"]
    assert events[0]["before"] == []


def test_prune_keeps_identifiers_and_relations(database: Database) -> None:
    repository = DiagnosticRepository(database, config=RuntimeConfig())
    repository.record_low_match(
        correlation_id="c-old",
        report=LowMatchReport(
            correlation_id="c-old",
            stage="reply",
            input_text="旧内容",
            primary_mood="neutral",
            reason="below_threshold",
        ),
        user_id="u1",
        turn_id="t-old",
    )
    database.execute(
        "UPDATE diagnostic_event SET occurred_at = ? WHERE user_id = 'u1'",
        ("2020-01-01T00:00:00+00:00",),
    )
    pruned = repository.prune(older_than_days=30)
    assert pruned == 1
    events = repository.query(user_id="u1")
    assert len(events) == 1
    assert events[0]["turn_id"] == "t-old"
    assert events[0]["payload"] == {"pruned": True}


def test_logging_failure_does_not_block_fallback(database: Database) -> None:
    from sana_bot.diagnostics import ResilientRecorder
    from tests.support import FailingDiagnostics

    recorder = ResilientRecorder(FailingDiagnostics(), secrets=["sk-secret"])
    primary = DiagnosticRepository(database)
    outcome = primary.record_low_match(
        correlation_id="c-1",
        report=LowMatchReport(
            correlation_id="c-1",
            stage="reply",
            input_text="x",
            primary_mood="neutral",
            reason="below_threshold",
        ),
    )
    assert outcome.recorded
    failing = recorder.record(
        __import__("sana_bot.diagnostics", fromlist=["DiagnosticRecord"]).DiagnosticRecord(
            category=DiagnosticCategory.LOW_MATCH, correlation_id="c-2"
        )
    )
    assert failing.recorded is False
    assert failing.fallback_signal is True


# --------------------------------------------------------------------------- CLI


def test_logs_query_requires_a_filter(tmp_path: Path) -> None:
    args = argparse.Namespace(runtime_dir=str(tmp_path / "runtime"), user=None, event=None, since=None, reason=None)
    with pytest.raises(ConfigError):
        run_query(args)


def test_logs_query_and_prune_via_cli(tmp_path: Path) -> None:
    from sana_bot.storage.database import open_runtime_database

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = open_runtime_database(runtime / "sana-bot.sqlite3")
    repository = DiagnosticRepository(database)
    repository.record_low_match(
        correlation_id="c-1",
        report=LowMatchReport(
            correlation_id="c-1",
            stage="reply",
            input_text="帮我写代码",
            primary_mood="happy",
            reason="below_threshold",
        ),
        user_id="u1",
    )
    database.close()

    payload = run_query(
        argparse.Namespace(runtime_dir=str(runtime), user="u1", event=None, since=None, reason=None)
    )
    assert payload["count"] == 1
    assert payload["events"][0]["payload"]["primary_mood"] == "happy"
    pruned = run_prune(argparse.Namespace(runtime_dir=str(runtime), older_than_days=30))
    assert pruned["pruned_events"] == 0
    assert pruned["note"].startswith("只清理诊断正文")


def test_negative_older_than_days_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        run_prune(argparse.Namespace(runtime_dir=str(tmp_path / "runtime"), older_than_days=0))
