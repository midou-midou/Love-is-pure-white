"""平台预算：双时钟边界、10 秒余量、四次额度、拒收与跨用户隔离。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.enums import DeliveryStatus, MonologueStatus, OpportunityStatus, RetrievalStage
from sana_bot.storage.index_repository import CalibrationProfileRecord
from tests.support import FakeModel, FakeQQTransport
from tests.support.index_fixture import build_indexed_fixture
from tests.support.monologue_harness import BASE_TIME, MonologueHarness


def _harness(database, tmp_path: Path, clock, *, probability: float = 1.0, config=None):
    fixture = build_indexed_fixture(
        database,
        artifact_root=tmp_path / "indexes",
        threshold=0.05,
        monologue_status=MonologueStatus.ENABLED,
    )
    with database.write():
        fixture.repository.insert_calibration(
            CalibrationProfileRecord(
                id="calibration-monologue",
                index_id=fixture.index_id,
                snapshot_hash=fixture.snapshot_hash,
                split_profile_hash="s",
                stage=RetrievalStage.MONOLOGUE,
                sample_set_hash="s",
                threshold=0.05,
                score_metric="cosine",
                metrics={},
                approved_at=None,
                created_at="2026-10-16T00:00:00+00:00",
            )
        )
    model = FakeModel()
    model.default_text = "（低头）……其实有点高兴"
    instance = MonologueHarness(
        database,
        model=model,
        transport=FakeQQTransport(clock=clock),
        clock=clock,
        config=config or RuntimeConfig(monologue_probability=probability, monologue_wait_seconds=60.0),
    )
    instance.attach_index(fixture)
    return instance, fixture


async def test_monologue_blocked_when_event_window_expired(database, tmp_path: Path, clock) -> None:
    harness, _ = _harness(database, tmp_path, clock)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    # 事件时间 + 300 秒已过，即使 60 秒等待早已满足也不能发
    harness.clock.advance(400)
    results = await harness.run_due()
    assert results[0].outcome in {"skipped", "failed"}
    assert results[0].skip_reason in {"passive_window_exhausted", "generation_advanced"}
    status = harness.monologues.get(opportunity.id).status
    assert status in {OpportunityStatus.CANCELLED, OpportunityStatus.SKIPPED, OpportunityStatus.FAILED}
    assert harness.transport.sent[-1].msg_seq == 1


async def test_safety_margin_is_enforced(database, tmp_path: Path, clock) -> None:
    harness, _ = _harness(database, tmp_path, clock)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    decision = harness.deliveries.eligible(
        app_id="app-1",
        source_msg_id=outcome.turn_id,
        event_time=BASE_TIME,
        now=BASE_TIME + timedelta(seconds=292),
    )
    assert decision.allowed is False
    assert decision.reason_code == "passive_window_exhausted"
    assert decision.details["margin_seconds"] == 10


async def test_budget_allows_four_messages_then_blocks(database, tmp_path: Path, clock) -> None:
    harness, _ = _harness(database, tmp_path, clock)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    source = harness.inbound.turn_row(outcome.turn_id).inbound_id
    # 正常回复已占用 1 次；再预留 2 次后仅剩 1 次额度
    for _ in range(2):
        with database.write():
            harness.deliveries.reserve(app_id="app-1", source_msg_id=source)
    decision = harness.deliveries.eligible(
        app_id="app-1",
        source_msg_id=source,
        event_time=clock.dt(),
        now=clock.dt(),
    )
    assert decision.allowed
    with database.write():
        harness.deliveries.reserve(app_id="app-1", source_msg_id=source)
    blocked = harness.deliveries.eligible(
        app_id="app-1", source_msg_id=source, event_time=clock.dt(), now=clock.dt()
    )
    assert blocked.allowed is False
    assert blocked.reason_code == "budget_exhausted"


async def test_rejected_monologue_is_failed_without_retry(database, tmp_path: Path, clock) -> None:
    harness, _ = _harness(database, tmp_path, clock)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（低头）……其实有点高兴")
    harness.transport.queue(DeliveryStatus.CONFIRMED, DeliveryStatus.REJECTED)
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    harness.clock.advance(61)
    results = await harness.run_due()
    assert results[0].outcome == "failed"
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.FAILED
    harness.clock.advance(600)
    assert await harness.run_due() == [], "失败后不得自动重发"


async def test_unknown_result_is_uncertain(database, tmp_path: Path, clock) -> None:
    harness, _ = _harness(database, tmp_path, clock)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（低头）……其实有点高兴")
    harness.transport.queue(DeliveryStatus.CONFIRMED, DeliveryStatus.UNCERTAIN)
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    harness.clock.advance(61)
    results = await harness.run_due()
    assert results[0].outcome == "failed"
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.UNCERTAIN


async def test_users_are_isolated(database, tmp_path: Path, clock) -> None:
    harness, _ = _harness(database, tmp_path, clock)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（歪头）嗯")
    first = await harness.reply(message_id="m-1", text="社团的猫咪", user_key="openid-a")
    second = await harness.reply(message_id="m-2", text="社团的猫咪", user_key="openid-b")
    assert first.turn_id != second.turn_id
    assert harness.monologues.for_turn(first.turn_id).conversation_id != harness.monologues.for_turn(
        second.turn_id
    ).conversation_id
    harness.clock.advance(61)
    results = await harness.run_due()
    assert len(results) == 2


async def test_restart_cancels_pending_opportunities(database, tmp_path: Path, clock) -> None:
    harness, _ = _harness(database, tmp_path, clock)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    cancelled = harness.scheduler.cancel_all(reason="service_restart")
    assert cancelled == 1
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.CANCELLED
    assert harness.monologues.get(opportunity.id).skip_reason == "service_restart"
    harness.clock.advance(61)
    assert await harness.run_due() == []
