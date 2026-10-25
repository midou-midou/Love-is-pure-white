"""调优追踪：低匹配可回溯字段、错误类型区分、跨会话不串用与发送结果回填。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sana_bot.cli_logs import run_query
from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.fallback_catalog import DEFAULT_FALLBACK_CATALOG
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints, check_text
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import DiagnosticCategory, PrimaryMood, RetrievalStage
from sana_bot.ports import ModelBudget
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.diagnostic_repository import DiagnosticRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from tests.support import FakeModel, FakeQQTransport, make_event
from tests.support.index_fixture import build_indexed_fixture

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class TuningHarness:
    def __init__(self, database: Database, *, model: FakeModel, transport: FakeQQTransport, clock):
        self.database = database
        self.model = model
        self.clock = clock
        self.inbound = InboundRepository(database)
        self.deliveries = DeliveryRepository(database, config=RuntimeConfig())
        self.structures = StructureRepository(database)
        self.history = HistoryService(
            structures=self.structures, inbound=self.inbound, deliveries=self.deliveries
        )
        self.diagnostics = DiagnosticRepository(database)
        self.service = DialogueTurnService(
            database=database,
            config=RuntimeConfig(),
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
            diagnostics=self.diagnostics,
            clock=clock,
        )

    def attach(self, *, index_id: str, embeddings, store: VectorStore) -> None:
        self.service.retriever = VectorRetriever(
            structures=self.structures, embeddings=embeddings, store=store
        )

    def submit(self, *, message_id: str, text: str, user_key: str = "openid-1", offset: int = 0):
        event = make_event(
            message_id=message_id,
            user_key=user_key,
            text=text,
            event_time=BASE_TIME + timedelta(seconds=offset),
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
def harness(database: Database, tmp_path: Path, clock) -> TuningHarness:
    fixture = build_indexed_fixture(database, artifact_root=tmp_path / "indexes")
    database.execute("UPDATE calibration_profile SET threshold = 0.99 WHERE stage = 'reply'")
    model = FakeModel()
    transport = FakeQQTransport(clock=clock)
    instance = TuningHarness(database, model=model, transport=transport, clock=clock)
    instance.attach(
        index_id=fixture.index_id,
        embeddings=fixture.embeddings,
        store=VectorStore(structures=fixture.repository),
    )
    return instance


async def test_low_match_event_has_required_fields(harness: TuningHarness) -> None:
    for index in range(3):
        harness.model.queue_text(f"（歪头）回复{index}")
        await harness.service.handle(
            harness.submit(message_id=f"m-{index}", text="帮我写代码", offset=index * 5)
        )
    events = harness.diagnostics.query(category=DiagnosticCategory.LOW_MATCH)
    assert len(events) == 3
    payload = events[0]["payload"]
    for key in (
        "stage",
        "input",
        "primary_mood",
        "candidates",
        "threshold",
        "index_version",
        "model_version",
        "config_version",
        "decision",
    ):
        assert key in payload, key
    assert payload["candidates"] == [] or payload["candidate_count"] >= 0
    assert payload["decision"]["fallback_kind"] == "confused"


async def test_cli_trace_links_neighbours(harness: TuningHarness) -> None:
    for index in range(4):
        harness.model.queue_text(f"（歪头）回复{index}")
        await harness.service.handle(
            harness.submit(message_id=f"m-{index}", text=f"第{index}句话", offset=index * 5)
        )
    payload = run_query(
        argparse.Namespace(
            runtime_dir=str(harness.database.path.parent),
            user=None,
            event=None,
            since=None,
            reason="below_threshold",
        )
    )
    assert payload["count"] >= 1
    event = payload["events"][0]
    assert len(event["after"]) <= 3
    assert all("turn_id" in item and "input" in item for item in event["after"])


async def test_error_types_stay_separate(harness: TuningHarness) -> None:
    """输出拦截与低匹配必须分开统计。"""
    harness.model.queue_text("哼，我讨厌你。")
    # 改写仍然违规：最终必须拦截并单独记录
    harness.model.queue_text("总之我们分手吧。")
    await harness.service.handle(harness.submit(message_id="m-1", text="社团的猫咪"))

    harness.database.execute("UPDATE calibration_profile SET threshold = 0.99")
    harness.model.queue_text("（歪头）我不知道该怎么回答")
    await harness.service.handle(harness.submit(message_id="m-2", text="帮我写代码", offset=5))

    blocked = harness.diagnostics.query(category=DiagnosticCategory.OUTPUT_BLOCKED)
    low = harness.diagnostics.query(category=DiagnosticCategory.LOW_MATCH)
    assert blocked, "违规草稿必须单独记录为 output_blocked"
    assert low, "低匹配必须单独记录"
    assert {item["type"] for item in blocked} == {DiagnosticCategory.OUTPUT_BLOCKED.value}


async def test_no_cross_conversation_contamination(harness: TuningHarness) -> None:
    harness.model.queue_text("（歪头）回复甲")
    await harness.service.handle(
        harness.submit(message_id="m-a", text="用户甲的话", user_key="openid-a")
    )
    harness.model.queue_text("（歪头）回复乙")
    await harness.service.handle(
        harness.submit(message_id="m-b", text="用户乙的话", user_key="openid-b", offset=5)
    )
    first = harness.inbound.get_user(
        harness.database.query_one("SELECT user_id FROM inbound_event WHERE message_id = 'm-a'")["user_id"]
    )
    events_a = harness.diagnostics.query(user_id=first["id"])
    events_b = harness.diagnostics.query(
        user_id=harness.database.query_one(
            "SELECT user_id FROM inbound_event WHERE message_id = 'm-b'"
        )["user_id"]
    )
    assert events_a and events_b
    assert {item["user_id"] for item in events_a} == {first["id"]}


def test_fallback_mood_variants_follow_state() -> None:
    neutral = DEFAULT_FALLBACK_CATALOG.text_for("confused", PrimaryMood.NEUTRAL)
    happy = DEFAULT_FALLBACK_CATALOG.text_for("confused", PrimaryMood.HAPPY)
    sad = DEFAULT_FALLBACK_CATALOG.text_for("confused", PrimaryMood.SAD)
    assert len({neutral, happy, sad}) == 3
    assert len(sad) <= len(neutral) + 6
    constraints = OutputConstraints()
    for text in (neutral, happy, sad):
        assert check_text(text, constraints) == (), text


def test_snapshot_defaults_are_neutral() -> None:
    snapshot = empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c")
    assert snapshot.primary_mood is PrimaryMood.NEUTRAL
    assert snapshot.secondary_tags == ()
