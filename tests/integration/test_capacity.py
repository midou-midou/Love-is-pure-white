"""容量与预算：100 关系档案、10 活跃用户、5 入站/秒、延迟目标与共享模型预算。

真实网络/模型测量与 fake 预算证据分开记录；这里只断言本服务在 fake 依赖下的目标。
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import MonologueStatus, RetrievalStage
from sana_bot.ports import ModelBudget
from sana_bot.retrieval.service import VectorRetriever
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.scheduling.serial_queue import InboundQueueService, UserSerialQueue
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.relationship_repository import RelationshipRepository
from tests.support import FakeClock, FakeModel, FakeQQTransport, make_event
from tests.support.index_fixture import build_indexed_fixture

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
RELATIONSHIP_PROFILES = 100
ACTIVE_USERS = 10
INBOUND_RATE_PER_SECOND = 5


@pytest.fixture
def harness(database: Database, tmp_path: Path, clock: FakeClock):
    fixture = build_indexed_fixture(database, artifact_root=tmp_path / "indexes", threshold=0.05)
    model = FakeModel()
    model.default_text = "（别开视线）……社团的猫咪今天很黏人"
    transport = FakeQQTransport(clock=clock)
    inbound = InboundRepository(database)
    deliveries = DeliveryRepository(database, config=RuntimeConfig())
    structures = StructureRepository(database)
    history = HistoryService(structures=structures, inbound=inbound, deliveries=deliveries)
    retriever = VectorRetriever(
        structures=structures, embeddings=fixture.embeddings, store=VectorStore(structures=structures)
    )
    service = DialogueTurnService(
        database=database,
        config=RuntimeConfig(),
        structures=structures,
        inbound=inbound,
        deliveries=deliveries,
        history=history,
        policy=ConstrainedOutputPolicy(constraints=OutputConstraints(), model=model),
        reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
        catalog=RuleCatalog(version="empty"),
        extractor=LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0)),
        model=model,
        transport=transport,
        retriever=retriever,
        clock=clock,
    )
    queue = UserSerialQueue(capacity_per_user=64, item_timeout_seconds=5.0)
    inbound_queue = InboundQueueService(database=database, repository=inbound, queue=queue)
    return {
        "database": database,
        "model": model,
        "transport": transport,
        "inbound": inbound,
        "service": service,
        "queue": queue,
        "inbound_queue": inbound_queue,
        "retriever": retriever,
        "clock": clock,
        "fixture": fixture,
    }


def test_retrieval_p95_stays_under_200ms(harness) -> None:
    """热模型情况下本地检索 p95 ≤ 200ms。"""
    retriever = harness["retriever"]
    fixture = harness["fixture"]
    from sana_bot.state.models import empty_snapshot

    snapshot = empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c")
    samples: list[float] = []

    async def measure() -> None:
        for index in range(40):
            start = time.perf_counter()
            await retriever.retrieve(
                query=f"社团的猫咪 {index}",
                snapshot=snapshot,
                index_id=fixture.index_id,
                stage=RetrievalStage.REPLY,
            )
            samples.append((time.perf_counter() - start) * 1000)

    asyncio.run(measure())
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 <= 200, f"检索 p95={p95:.1f}ms 超出目标"


def test_hundred_relationship_profiles_are_supported(harness) -> None:
    database = harness["database"]
    relationships = RelationshipRepository(database)
    with database.write():
        for index in range(RELATIONSHIP_PROFILES):
            user_id = f"user-{index:03d}"
            relationships.ensure(user_id)
            relationships.update_score(
                user_id=user_id,
                delta=float(index % 10),
                keyword_hits=index % 3,
                new_day=index % 2 == 0,
                happy_delta=index % 5 == 0,
                romance_status="non_romantic",
                config_version=1,
            )
    assert database.query_one("SELECT COUNT(*) AS n FROM relationship")["n"] == RELATIONSHIP_PROFILES
    assert relationships.get("user-099").score >= 0


async def test_ten_active_users_and_five_inbound_per_second(harness) -> None:
    """10 个活跃用户、每秒 5 条入站：全部处理完成且每用户串行。"""
    database = harness["database"]
    model = harness["model"]
    queue = harness["queue"]
    inbound_queue = harness["inbound_queue"]
    service = harness["service"]
    model.default_text = "（别开视线）……社团的猫咪今天很黏人"
    processed: list[str] = []

    async def consume(item) -> None:
        outcome = await service.handle(item)
        processed.append(item.user_id)
        assert outcome.turn_id

    await queue.start(consume)
    try:
        total = 0
        for second in range(2):
            for index in range(INBOUND_RATE_PER_SECOND):
                user_key = f"openid-{index % ACTIVE_USERS}"
                event = make_event(
                    message_id=f"m-{second}-{index}",
                    user_key=user_key,
                    text="社团的猫咪",
                    event_time=BASE_TIME,
                )
                outcome = await inbound_queue.accept(event)
                assert outcome.accepted and outcome.queued
                total += 1
        await queue.join()
        assert len(processed) == total
        assert database.query_one("SELECT COUNT(*) AS n FROM turn")["n"] == total
    finally:
        await queue.stop()


async def test_ack_latency_stays_under_one_second(harness) -> None:
    """回调落盘 ACK 目标 p95 ≤ 1s（不等待模型）。"""
    inbound_queue = harness["inbound_queue"]
    queue = harness["queue"]

    async def consume(item) -> None:
        return None

    await queue.start(consume)
    try:
        samples: list[float] = []
        for index in range(10):
            event = make_event(message_id=f"ack-{index}", text="社团的猫咪", event_time=BASE_TIME)
            start = time.perf_counter()
            outcome = await inbound_queue.accept(event)
            samples.append((time.perf_counter() - start) * 1000)
            assert outcome.queued
        assert max(samples) <= 1000
    finally:
        await queue.stop()


async def test_end_to_end_latency_under_twenty_seconds(harness) -> None:
    """入站持久化到确认发送的端到端目标 p95 ≤ 20s（fake 依赖下的服务端预算）。"""
    model = harness["model"]
    queue = harness["queue"]
    inbound_queue = harness["inbound_queue"]
    service = harness["service"]
    model.default_text = "（别开视线）……社团的猫咪今天很黏人"
    durations: list[float] = []

    async def consume(item) -> None:
        start = time.perf_counter()
        await service.handle(item)
        durations.append(time.perf_counter() - start)

    await queue.start(consume)
    try:
        for index in range(10):
            event = make_event(message_id=f"e2e-{index}", text="社团的猫咪", event_time=BASE_TIME)
            await inbound_queue.accept(event)
        await queue.join()
        assert len(durations) == 10
        assert max(durations) <= 20, f"端到端最慢 {max(durations):.2f}s"
    finally:
        await queue.stop()


async def test_shared_model_budget_is_enforced() -> None:
    """事件提取/生成/审核/改写共享同一预算；耗尽后走合规失败路径。"""
    budget = ModelBudget(total_seconds=0.0)
    model = FakeModel()
    extractor = LangChainStateEventExtractor(model=model, budget=budget)
    from sana_bot.state.event_models import ApprovedEventCatalog

    result = await extractor.extract(
        message="你好",
        source_message_id="m-1",
        catalog=ApprovedEventCatalog(version="v1", tags=("steady_daily",)),
    )
    assert not result.ok
    assert result.reason_code == "model_budget_exhausted"
    assert model.requests == []


def test_overload_is_reported_not_silently_dropped(harness) -> None:
    """队列满时明确拒绝或延后，不静默丢消息。"""
    database = harness["database"]
    queue = UserSerialQueue(capacity_per_user=1, item_timeout_seconds=5.0)
    inbound = harness["inbound"]
    service = harness["inbound_queue"]
    del service
    blocking = InboundQueueService(database=database, repository=inbound, queue=queue)

    started = asyncio.Event()
    release = asyncio.Event()

    async def consume(item) -> None:
        started.set()
        await release.wait()

    async def run() -> None:
        await queue.start(consume)
        first = await blocking.accept(
            make_event(message_id="o-1", text="a", event_time=BASE_TIME)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await blocking.accept(make_event(message_id="o-2", text="b", event_time=BASE_TIME))
        deferred = await blocking.accept(
            make_event(message_id="o-3", text="c", event_time=BASE_TIME)
        )
        assert deferred.accepted and not deferred.queued
        assert deferred.reason_code == "queue_capacity_exceeded"
        release.set()
        await queue.join()
        await queue.stop()
        assert first.accepted

    asyncio.run(run())
    statuses = {
        row["message_id"]: row["status"]
        for row in database.query("SELECT * FROM inbound_event")
    }
    assert statuses["o-3"] == "received", "被延后的事件必须保留为可恢复状态"
