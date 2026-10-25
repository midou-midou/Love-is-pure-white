"""独白概率：每个机会一次抽样、1000 次统计、未命中不重抽、独白不链式触发。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.enums import MonologueStatus, OpportunityStatus, RetrievalStage
from sana_bot.storage.index_repository import CalibrationProfileRecord
from tests.support import FakeClock, FakeModel, FakeQQTransport, InlineRandom, SeededRandom
from tests.support.index_fixture import build_indexed_fixture
from tests.support.monologue_harness import MonologueHarness


def _harness(database, tmp_path: Path, clock, *, probability: float, threshold: float = 0.05):
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
                split_profile_hash="s",
                stage=RetrievalStage.MONOLOGUE,
                sample_set_hash="s",
                threshold=threshold,
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
        config=RuntimeConfig(monologue_probability=probability),
    )
    instance.attach_index(fixture)
    return instance


async def test_each_opportunity_draws_exactly_once(database, tmp_path: Path, clock) -> None:
    harness = _harness(database, tmp_path, clock, probability=0.5)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    harness.rng = InlineRandom([0.4])  # 只允许一次抽样
    harness.evaluator.rng = harness.rng
    harness.clock.advance(61)
    results = await harness.run_due()
    assert results[0].outcome == "sent"
    assert harness.rng.calls == 1
    assert await harness.run_due() == []


async def test_miss_does_not_redraw(database, tmp_path: Path, clock) -> None:
    harness = _harness(database, tmp_path, clock, probability=0.1)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    harness.rng = InlineRandom([0.9])
    harness.evaluator.rng = harness.rng
    harness.clock.advance(61)
    results = await harness.run_due()
    assert results[0].skip_reason == "probability_miss"
    assert harness.rng.calls == 1
    harness.clock.advance(600)
    assert await harness.run_due() == []


async def test_monologue_does_not_create_new_opportunity(database, tmp_path: Path, clock) -> None:
    harness = _harness(database, tmp_path, clock, probability=1.0)
    harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
    harness.model.queue_text("（低头）……其实有点高兴")
    outcome = await harness.reply(message_id="m-1", text="社团的猫咪")
    harness.clock.advance(61)
    await harness.run_due()
    rows = database.query("SELECT COUNT(*) AS n FROM monologue_opportunity")
    assert rows[0]["n"] == 1
    assert harness.monologues.for_turn(outcome.turn_id).status is OpportunityStatus.SENT


async def test_seeded_probability_matches_configured_rate(database, tmp_path: Path, clock) -> None:
    """1000 次独立合格机会、30% 概率：实际触发比例应落在 25%–35%。"""
    harness = _harness(database, tmp_path, clock, probability=0.30)
    rng = SeededRandom(20261016)
    harness.rng = rng
    harness.evaluator.rng = rng
    samples = 1000
    sent = 0
    misses = 0
    for index in range(samples):
        harness.model.queue_text("（别开视线）……社团的猫咪今天很黏人")
        harness.model.queue_text("（低头）……其实有点高兴")
        outcome = await harness.reply(message_id=f"m-{index}", text="社团的猫咪")
        if outcome.kind != "normal":
            continue
        harness.clock.advance(61)
        results = await harness.run_due()
        if results and results[0].outcome == "sent":
            sent += 1
        elif results and results[0].skip_reason == "probability_miss":
            misses += 1
    evaluated = sent + misses
    assert evaluated >= samples * 0.9, f"合格机会不足：{evaluated}"
    ratio = sent / evaluated
    assert 0.25 <= ratio <= 0.35, f"30% 概率的实际触发比例异常：{ratio:.3f}"
