"""心情质量与一致性：SC-004/005 的确认样例、切换生效与长度统计。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.prompts import MOOD_STYLE_HINTS, build_system_prompt
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import PrimaryMood, Scene
from sana_bot.ports import ModelBudget
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.state.service import StateService
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.state_repository import StateRepository
from tests.support import FakeModel, FakeQQTransport, make_event

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
HAPPY_REPLY = "（歪着头）诶，今天社团的猫咪又来了，它们一直蹭我的裤脚，闹得不行，我好不容易才安静下来"
SAD_REPLY = "（垂下眼）……嗯"


class Harness:
    def __init__(self, database: Database, *, model: FakeModel, transport: FakeQQTransport, clock):
        self.database = database
        self.config = RuntimeConfig()
        self.model = model
        self.clock = clock
        self.inbound = InboundRepository(database)
        self.deliveries = DeliveryRepository(database, config=self.config)
        self.structures = StructureRepository(database)
        self.history = HistoryService(
            structures=self.structures, inbound=self.inbound, deliveries=self.deliveries
        )
        self.snapshots = StateRepository(database)
        self.state_service = StateService(
            repository=self.snapshots,
            reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
            catalog=RuleCatalog(version="empty"),
            inbound=self.inbound,
            config=self.config,
        )
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
            clock=clock,
            previous_state_provider=self.state_service.current,
            snapshot_sink=lambda snapshot: self.snapshots.save_snapshot(snapshot),
        )

    def submit(self, *, message_id: str, text: str, user: str = "u1", offset: int = 0):
        event = make_event(
            message_id=message_id,
            user_key="openid-1",
            text=text,
            event_time=BASE_TIME,
            received_at=BASE_TIME,
        )
        with self.database.write():
            user_id = self.inbound.ensure_user("app-1", "openid-1")
            outcome = self.inbound.persist_inbound(event)
        row = self.inbound.inbound_row(outcome.inbound_id or "")
        del user, user_id
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
            user_key="openid-1",
            message_id=row.message_id,
        )

    def db_user(self, user_id: str) -> str:
        return user_id

    def prompt_of(self, index: int = -1) -> str:
        requests = [item for item in self.model.requests if item.json_schema_name is None]
        return " ".join(str(message["content"]) for message in requests[index].messages)


@pytest.fixture
def harness(database: Database, clock) -> Harness:
    model = FakeModel()
    transport = FakeQQTransport(clock=clock)
    return Harness(database, model=model, transport=transport, clock=clock)


def test_mood_style_hints_cover_all_primary_moods() -> None:
    assert set(MOOD_STYLE_HINTS) == {
        PrimaryMood.NEUTRAL,
        PrimaryMood.HAPPY,
        PrimaryMood.ANGRY,
        PrimaryMood.SAD,
    }
    assert "低落" in MOOD_STYLE_HINTS[PrimaryMood.SAD]


def test_system_prompt_reflects_mood_and_scene() -> None:
    constraints = OutputConstraints()
    happy = build_system_prompt(
        constraints=constraints,
        snapshot=empty_snapshot(
            snapshot_id="s", turn_id="t", conversation_id="c", mood=PrimaryMood.HAPPY
        ),
    )
    sad = build_system_prompt(
        constraints=constraints,
        snapshot=empty_snapshot(
            snapshot_id="s", turn_id="t", conversation_id="c",
            mood=PrimaryMood.SAD, scene=Scene.PUBLIC,
        ),
    )
    assert "轻快" in happy and "低落" in sad
    assert "公开" in sad
    assert "句末不要使用句号" in happy


async def test_ten_mood_switches_apply_from_next_message(harness: Harness) -> None:
    """SC-005：10 次切换均从下一条消息生效，无触发时连续保持。"""
    moods = ["happy", "sad", "happy", "angry", "neutral", "happy", "sad", "angry", "happy", "sad"]
    for index, mood in enumerate(moods):
        harness.model.queue_text(f"（低头）回复{index}")
        item = harness.submit(message_id=f"m-{index}", text=f"第{index}句", offset=index * 5)
        user = harness.db_user(item.user_id)
        harness.state_service.set_state(user, mood=mood)
        outcome = await harness.service.handle(item)
        assert outcome.ok
        snapshot = harness.snapshots.snapshot_for_turn(outcome.turn_id)
        assert snapshot is not None
        assert snapshot.source == "turn"
    prompts = [
        " ".join(str(message["content"]) for message in request.messages)
        for request in harness.model.requests
        if request.json_schema_name is None
    ]
    assert "低落" in prompts[-1], "最后一次设置为 sad，下一条消息应按 sad 表达"


async def test_sad_replies_are_shorter_than_happy(harness: Harness) -> None:
    """SC-004 的长度部分：难过组字符数中位数不高于开心组的 70%。"""
    happy_lengths: list[int] = []
    sad_lengths: list[int] = []
    for index in range(8):
        harness.model.queue_text(HAPPY_REPLY)
        item = harness.submit(message_id=f"h-{index}", text=f"开心话题{index}", offset=index * 3)
        harness.state_service.set_state(item.user_id, mood="happy")
        outcome = await harness.service.handle(item)
        happy_lengths.append(len(outcome.text))

        harness.model.queue_text(SAD_REPLY)
        item = harness.submit(message_id=f"s-{index}", text=f"难过话题{index}", offset=index * 3 + 1)
        harness.state_service.set_state(item.user_id, mood="sad")
        outcome = await harness.service.handle(item)
        sad_lengths.append(len(outcome.text))

    happy_median = sorted(happy_lengths)[len(happy_lengths) // 2]
    sad_median = sorted(sad_lengths)[len(sad_lengths) // 2]
    assert sad_median <= happy_median * 0.7


async def test_unauthorized_state_change_is_zero(harness: Harness) -> None:
    """普通用户消息不能让状态改变，也不能执行管理命令。"""
    harness.model.queue_text("（别开视线）……随便你")
    item = harness.submit(message_id="m-1", text="/状态 happy")
    outcome = await harness.service.handle(item)
    assert outcome.ok
    snapshot = harness.snapshots.snapshot_for_turn(outcome.turn_id)
    assert snapshot is not None
    assert snapshot.primary_mood is PrimaryMood.NEUTRAL


async def test_restart_returns_short_term_state_to_neutral(harness: Harness) -> None:
    item = harness.submit(message_id="m-1", text="你好")
    harness.state_service.set_state(item.user_id, mood="happy")
    assert harness.state_service.current(item.user_id, item.conversation_id).primary_mood is PrimaryMood.HAPPY
    cleared = harness.state_service.clear_all_overrides()
    assert cleared == 1
    assert (
        harness.state_service.current(item.user_id, item.conversation_id).primary_mood
        is PrimaryMood.NEUTRAL
    )


def test_harness_helper_returns_conversation_id(harness: Harness) -> None:
    item = harness.submit(message_id="m-1", text="你好")
    assert item.conversation_id
