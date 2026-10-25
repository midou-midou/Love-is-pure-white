"""独白时机：起算、p=0/1、续聊取消、最新状态重匹配与无材料静默。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.enums import MonologueStatus, OpportunityStatus, RetrievalStage
from sana_bot.ports import ModelBudget
from sana_bot.storage.index_repository import CalibrationProfileRecord
from tests.support import FakeModel, FakeQQTransport, InlineRandom
from tests.support.index_fixture import build_indexed_fixture
from tests.support.monologue_harness import BASE_TIME, MonologueHarness


def _build(
    database, tmp_path: Path, clock, *, probability: float = 0.30, threshold: float = 0.05
) -> MonologueHarness:
    fixture = build_indexed_fixture(
        database,
        artifact_root=tmp_path / "indexes",
        threshold=threshold,
        monologue_status=MonologueStatus.ENABLED,
    )
    with database.write():
        fixture.repository.insert_calibration(
            CalibrationProfileRecord(
                id="calibration-monologue",
                index_id=fixture.index_id,
                snapshot_hash=fixture.snapshot_hash,
                split_profile_hash="fixture-split",
                stage=RetrievalStage.MONOLOGUE,
                sample_set_hash="s",
                threshold=0.05,
                score_metric="cosine",
                metrics={},
                approved_at=None,
                created_at="2026-10-16T00:00:00+00:00",
            )
        )
    config = RuntimeConfig(monologue_wait_seconds=60.0, monologue_probability=probability)
    model = FakeModel()
    model.default_text = "（低头）……其实有点高兴"
    transport = FakeQQTransport(clock=clock)
    instance = MonologueHarness(
        database, model=model, transport=transport, clock=clock, config=config
    )
    instance.attach_index(fixture)
    return instance


@pytest.fixture
def harness(database, tmp_path: Path, clock) -> MonologueHarness:
    return _build(database, tmp_path, clock)


def build_with_probability(database, tmp_path: Path, clock, probability: float):
    return _build(database, tmp_path, clock, probability=probability)


async def test_not_due_is_skipped(harness: MonologueHarness) -> None:
    harness.model.queue_text("（别开视线）……随便你")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    assert outcome.ok
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    assert opportunity is not None
    results = await harness.run_due()
    assert results == []
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.PENDING


async def test_waiting_window_then_single_monologue(harness: MonologueHarness) -> None:
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    harness.rng = InlineRandom([0.1])
    harness.evaluator.rng = harness.rng
    harness.clock.advance(61)
    results = await harness.run_due()
    assert results and results[0].outcome == "sent"
    assert results[0].text.startswith("【心理独白】")
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.SENT
    assert len(harness.transport.sent) == 2
    assert harness.transport.sent[-1].msg_seq == 2


async def test_p_zero_never_sends(database, tmp_path: Path, clock) -> None:
    harness = build_with_probability(database, tmp_path, clock, 0.0)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    harness.clock.advance(61)
    results = await harness.run_due()
    assert results[0].outcome == "skipped"
    assert results[0].skip_reason == "probability_miss"
    assert len(harness.transport.sent) == 1, "只发过正常回复"
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.SKIPPED


async def test_p_one_sends_once_each_opportunity(database, tmp_path: Path, clock) -> None:
    harness = build_with_probability(database, tmp_path, clock, 1.0)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（低头）……其实有点高兴")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    harness.clock.advance(61)
    first = await harness.run_due()
    assert first[0].outcome == "sent"
    second = await harness.run_due()
    assert second == [], "同一机会不得重复抽取"
    assert harness.monologues.for_turn(outcome.turn_id) is not None


async def test_new_message_cancels_pending_opportunity(harness: MonologueHarness) -> None:
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（低头）……嗯，我听着")
    harness.model.queue_text("（歪头）嗯，还在")
    first = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(first.turn_id)
    assert opportunity.status is OpportunityStatus.PENDING

    cancelled = harness.scheduler.cancel(opportunity.conversation_id, reason="user_continued")
    assert cancelled == 1
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.CANCELLED

    second = await harness.reply(message_id="m-2", text="社团的猫咪还乖吗", offset=30)
    assert second.ok and second.kind == "normal"
    new_opportunity = harness.monologues.for_turn(second.turn_id)
    assert new_opportunity is not None and new_opportunity.id != opportunity.id


async def test_latest_state_is_used_when_sending(harness: MonologueHarness) -> None:
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（低声）……嗯")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    harness.clock.advance(61)
    harness.rng = InlineRandom([0.2])
    harness.evaluator.rng = harness.rng
    results = await harness.run_due()
    assert results[0].outcome == "sent"
    # 发送前重新读取了冻结快照
    assert harness.monologues.get(opportunity.id).latest_snapshot is not None


async def test_low_match_stays_silent(database, tmp_path: Path, clock) -> None:
    harness = _build(database, tmp_path, clock, threshold=0.0)
    harness.model.queue_text("（别开视线）……今天天气确实不错")
    outcome = await harness.reply(message_id="m-1", text="今天天气不错")
    opportunity = harness.monologues.for_turn(outcome.turn_id)
    with harness.database.write():
        harness.database.execute(
            "UPDATE calibration_profile SET threshold = 0.99 WHERE stage = 'monologue'"
        )
    harness.clock.advance(61)
    results = await harness.run_due()
    assert results[0].outcome == "skipped"
    assert results[0].skip_reason == "below_threshold"
    assert len(harness.transport.sent) == 1, "独白低匹配必须保持安静"
    assert harness.monologues.get(opportunity.id).status is OpportunityStatus.SKIPPED


async def test_no_material_creates_no_opportunity(database, tmp_path: Path, clock) -> None:
    fixture = build_indexed_fixture(
        database,
        artifact_root=tmp_path / "indexes",
        threshold=0.05,
        monologue_status=MonologueStatus.DISABLED_NO_MATERIAL,
    )
    model = FakeModel()
    model.default_text = "（别开视线）……随便你"
    transport = FakeQQTransport(clock=clock)
    instance = MonologueHarness(
        database,
        model=model,
        transport=transport,
        clock=clock,
        config=RuntimeConfig(),
    )
    instance.attach_index(fixture)
    harness = instance
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    assert harness.monologues.for_turn(outcome.turn_id) is None
    harness.clock.advance(61)
    assert await harness.run_due() == []
    assert harness.scheduler.status()[0] is MonologueStatus.DISABLED_NO_MATERIAL
    assert harness.scheduler.status()[1] is None


async def test_generation_during_wait_is_not_sent(harness: MonologueHarness) -> None:
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（低头）……嗯")
    harness.model.queue_text("（歪头）嗯")
    first = await harness.reply(message_id="m-1", text="社团的猫咪")
    opportunity = harness.monologues.for_turn(first.turn_id)
    # 到期前用户续聊：generation 递增
    second = await harness.reply(message_id="m-2", text="社团的猫咪还乖吗", offset=30)
    assert second.ok
    harness.clock.advance(61)
    results = await harness.run_due()
    statuses = {result.outcome for result in results}
    assert "sent" not in statuses or harness.monologues.get(opportunity.id).status is not OpportunityStatus.SENT
    assert harness.deliveries.eligible(
        app_id="app-1",
        source_msg_id=opportunity.source_msg_id,
        event_time=BASE_TIME,
        now=BASE_TIME + timedelta(seconds=400),
    ).allowed is False
