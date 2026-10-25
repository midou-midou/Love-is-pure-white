"""好感度结算与发送：确认后事务、幂等、uncertain 不计分与重启恢复。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import DeliveryStatus, OutboundKind, PrimaryMood, RelationshipStatus
from sana_bot.errors import ConfigError
from sana_bot.ports import ModelBudget
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.affection import AffectionService
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.state.relationship import RelationshipService
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.diagnostic_repository import DiagnosticRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.relationship_repository import RelationshipRepository
from sana_bot.storage.state_repository import StateRepository
from sana_bot.cli_delivery import run_review
from tests.support import FakeModel, FakeQQTransport, make_event

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
KEYWORDS = ("谢谢",)


class Harness:
    def __init__(self, database: Database, *, model: FakeModel, transport: FakeQQTransport, clock):
        self.database = database
        self.config = RuntimeConfig(keywords=KEYWORDS)
        self.model = model
        self.transport = transport
        self.clock = clock
        self.inbound = InboundRepository(database)
        self.deliveries = DeliveryRepository(database, config=self.config)
        self.structures = StructureRepository(database)
        self.history = HistoryService(
            structures=self.structures, inbound=self.inbound, deliveries=self.deliveries
        )
        self.snapshots = StateRepository(database)
        self.relationships = RelationshipRepository(database)
        self.affection = AffectionService(
            database=database,
            config=self.config,
            deliveries=self.deliveries,
            inbound=self.inbound,
            relationships=self.relationships,
            snapshots=self.snapshots,
            relationship_service=RelationshipService(self.relationships, config=self.config),
        )
        self.settlements: list[str] = []
        self.service = DialogueTurnService(
            database=database,
            config=self.config,
            structures=self.structures,
            inbound=self.inbound,
            deliveries=self.deliveries,
            history=self.history,
            policy=ConstrainedOutputPolicy(constraints=OutputConstraints(), model=model),
            reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
            catalog=RuleCatalog(version="empty"),
            extractor=LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0)),
            model=model,
            transport=transport,
            diagnostics=DiagnosticRepository(database, config=self.config),
            post_delivery_hooks=(self._hook,),
            clock=clock,
        )

    def _hook(self, turn_id: str, delivery_id: str, kind) -> None:
        if kind is not OutboundKind.NORMAL:
            return
        turn = self.inbound.turn_row(turn_id)
        snapshot_id = turn.snapshot_id if turn else None
        # 快照已在处理轮次时冻结并写入
        del snapshot_id
        result = self.affection.settle_delivery(delivery_id=delivery_id)
        if result.applied:
            self.settlements.append(delivery_id)

    def submit(self, *, message_id: str, text: str, user_key: str = "openid-1", offset: int = 0):
        event = make_event(
            message_id=message_id, user_key=user_key, text=text, event_time=BASE_TIME + timedelta(seconds=offset)
        )
        with self.database.write():
            outcome = self.inbound.persist_inbound(event)
        row = self.inbound.inbound_row(outcome.inbound_id or "")
        return QueueItem(
            inbound_id=row.id,
            user_id=row.user_id,
            conversation_id=row.conversation_id,
            generation=outcome.generation,
            text=row.text,
            chat_day=row.chat_day,
            received_at=row.received_at,
            event_time=row.event_time,
            correlation_id=row.id,
            app_id=row.app_id,
            user_key=user_key,
            message_id=row.message_id,
        )


@pytest.fixture
def harness(database: Database, clock) -> Harness:
    model = FakeModel()
    model.default_text = "（别开视线）……不用谢我"
    transport = FakeQQTransport(clock=clock)
    instance = Harness(database, model=model, transport=transport, clock=clock)
    # 让快照写入 state_snapshot，便于结算读取主情绪
    instance.service.snapshot_sink = lambda snapshot: _persist(instance, snapshot)
    # 让 CLI 路径读到同一份配置（关键词与门槛）
    from sana_bot.config import ConfigStore

    ConfigStore(database.path.parent / "config.json").apply_payload(
        {"keywords": list(KEYWORDS), "romance_threshold": 100.0}, source="test"
    )
    return instance


def _persist(instance: Harness, snapshot) -> None:
    with instance.database.write():
        instance.snapshots.save_snapshot(snapshot)


def _confirmed_turn(harness: Harness, text: str, *, message_id: str = "m-1", snapshot_mood=None):
    turn = harness.submit(message_id=message_id, text=text)
    return turn


async def test_confirmed_normal_delivery_is_settled(harness: Harness) -> None:
    item = harness.submit(message_id="m-1", text="谢谢你来陪我")
    outcome = await harness.service.handle(item)
    assert outcome.status == DeliveryStatus.CONFIRMED.value
    assert harness.settlements == [outcome.delivery_id]
    row = harness.relationships.get(item.user_id)
    assert row.keyword_count == 1
    assert row.dialogue_days == 1
    assert row.score == pytest.approx(1 + 2)  # 关键词 1 + 新聊天日 2，本轮非 happy


async def test_duplicate_settlement_is_idempotent(harness: Harness) -> None:
    item = harness.submit(message_id="m-1", text="谢谢你来陪我")
    outcome = await harness.service.handle(item)
    again = harness.affection.settle_delivery(delivery_id=outcome.delivery_id or "")
    assert again.reason_code == "already_settled"
    assert harness.relationships.get(item.user_id).score == pytest.approx(3)


async def test_fallback_and_monologue_are_not_scored(harness: Harness) -> None:
    harness.transport.queue(DeliveryStatus.CONFIRMED)
    harness.model.queue_text("哼，我讨厌你。")
    harness.model.queue_text("总之我们分手吧。")
    item = harness.submit(message_id="m-f", text="谢谢")
    outcome = await harness.service.handle(item)
    # 拦截后改用预审兜底：发送成功但不得计分
    assert outcome.kind == OutboundKind.FALLBACK.value
    assert harness.relationships.get(item.user_id).score == 0
    assert harness.relationships.get(item.user_id).keyword_count == 0


async def test_uncertain_is_not_scored(harness: Harness) -> None:
    harness.model.queue_text("（别开视线）……随便你")
    harness.transport.queue(DeliveryStatus.UNCERTAIN)
    item = harness.submit(message_id="m-u", text="谢谢")
    outcome = await harness.service.handle(item)
    assert outcome.status == DeliveryStatus.UNCERTAIN.value
    assert harness.relationships.event_for_delivery(outcome.delivery_id or "") is None
    assert harness.relationships.get(item.user_id).score == 0


async def test_interrupted_sending_recovers_as_uncertain(harness: Harness) -> None:
    """进程在 sending 中断后必须标 uncertain，且不得假定成功计分。"""
    item = harness.submit(message_id="m-i", text="谢谢")
    with harness.database.write():
        harness.deliveries.prepare(
            delivery_id="interrupted",
            app_id="app-1",
            user_key="openid-1",
            kind=OutboundKind.NORMAL,
            source_msg_id=item.inbound_id,
            content="（别开视线）……随便你",
            msg_seq=1,
            turn_id=None,
        )
        harness.deliveries.mark_sending("interrupted")
    recovered = harness.deliveries.recover_interrupted()
    assert recovered == 1
    assert harness.deliveries.delivery("interrupted").status is DeliveryStatus.UNCERTAIN
    assert harness.relationships.get(item.user_id).score == 0


async def test_delivery_review_requires_evidence_and_settles_idempotently(
    harness: Harness, tmp_path: Path
) -> None:
    harness.model.queue_text("（别开视线）……随便你")
    harness.transport.queue(DeliveryStatus.UNCERTAIN)
    item = harness.submit(message_id="m-r", text="谢谢")
    outcome = await harness.service.handle(item)
    assert outcome.delivery_id

    evidence = tmp_path / "evidence.txt"
    evidence.write_text("官方后台记录：消息已送达", encoding="utf-8")
    args = argparse.Namespace(
        runtime_dir=str(harness.database.path.parent),
        id=outcome.delivery_id,
        result="confirmed",
        evidence=str(evidence),
    )
    payload = run_review(args)
    assert payload["status"] == "confirmed"
    assert payload["settlement"]["applied"] is True
    assert harness.relationships.get(item.user_id).score == pytest.approx(3)

    with pytest.raises(ConfigError):
        run_review(
            argparse.Namespace(
                runtime_dir=str(harness.database.path.parent),
                id=outcome.delivery_id,
                result="confirmed",
                evidence=str(tmp_path / "missing.txt"),
            )
        )


async def test_missing_evidence_file_is_rejected(harness: Harness, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        run_review(
            argparse.Namespace(
                runtime_dir=str(harness.database.path.parent),
                id="any",
                result="confirmed",
                evidence=str(tmp_path / "nope.txt"),
            )
        )


async def test_romance_transition_takes_effect_next_turn(database: Database) -> None:
    """达到门槛后进入恋爱中，并从下一轮生效。"""
    config = RuntimeConfig(keywords=("谢谢",), romance_threshold=3)
    repository = RelationshipRepository(database)
    service = RelationshipService(repository, config=config)
    with database.write():
        repository.ensure("u1")
        decision = service.apply("u1", score_after=3)
        assert decision.entered_romance
        repository.update_score(
            user_id="u1",
            delta=3,
            keyword_hits=1,
            new_day=True,
            happy_delta=False,
            romance_status=decision.after,
            config_version=1,
        )
    assert repository.get("u1").romance_status is RelationshipStatus.ROMANTIC
    assert service.snapshot_payload("u1")["romance_status"] == "romantic"
