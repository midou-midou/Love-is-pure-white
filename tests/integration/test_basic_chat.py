"""基础聊天链路：fake 模型与传输下的多用户三轮对话、失败路径与去重。

不绑定真实资料审批，也不依赖网络或等待。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import DeliveryStatus, OutboundKind
from sana_bot.ports import ModelBudget
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.catalog import RuleCatalog
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from tests.support import FakeModel, FakeQQTransport, make_event

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class _Harness:
    def __init__(
        self, database: Database, *, model: FakeModel, transport: FakeQQTransport, clock=None
    ) -> None:
        self.database = database
        self.clock = clock
        self.config = RuntimeConfig()
        self.model = model
        self.transport = transport
        self.inbound = InboundRepository(database)
        self.deliveries = DeliveryRepository(database, config=self.config)
        self.structures = StructureRepository(database)
        self.history = HistoryService(
            structures=self.structures, inbound=self.inbound, deliveries=self.deliveries
        )
        self.settlements: list[tuple[str, str]] = []
        self.service = DialogueTurnService(
            database=database,
            config=self.config,
            structures=self.structures,
            inbound=self.inbound,
            deliveries=self.deliveries,
            history=self.history,
            policy=ConstrainedOutputPolicy(
                constraints=OutputConstraints(), model=model, budget=ModelBudget(30.0)
            ),
            reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
            catalog=RuleCatalog(version="empty"),
            extractor=LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0)),
            model=model,
            transport=transport,
            post_delivery_hooks=(self._hook,),
            clock=clock,
        )

    def _hook(self, turn_id: str, delivery_id: str, kind) -> None:
        if kind is OutboundKind.NORMAL:
            self.settlements.append((turn_id, delivery_id))

    def generation_requests(self):
        """只看生成调用，排除事件提取与语义审核。"""
        return [request for request in self.model.requests if request.json_schema_name is None]

    def submit(self, *, message_id: str, text: str, user_key: str = "openid-1", offset: int = 0):
        event = make_event(
            message_id=message_id,
            user_key=user_key,
            text=text,
            event_time=BASE_TIME + timedelta(seconds=offset),
            received_at=BASE_TIME + timedelta(seconds=offset, milliseconds=10),
        )
        with self.database.write():
            outcome = self.inbound.persist_inbound(event)
        row = self.inbound.inbound_row(outcome.inbound_id or "")
        assert row is not None
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
def harness(database: Database, clock) -> _Harness:
    model = FakeModel()
    model.default_text = "（别开视线）……我才没有很高兴"
    transport = FakeQQTransport(clock=clock)
    return _Harness(database, model=model, transport=transport, clock=clock)


async def test_three_turn_conversation_keeps_context(harness: _Harness) -> None:
    harness.model.queue_text(
        "（歪头）嗯？你今天怎么这么安静",
        "（抱臂）社团的事情我才不想说",
        "（小声）……明天陪我一起去吧",
    )
    texts = []
    for index, message in enumerate(["你好呀", "刚才那件事", "明天有空吗"], start=1):
        item = harness.submit(message_id=f"m-{index}", text=message, offset=index * 10)
        outcome = await harness.service.handle(item)
        assert outcome.ok, outcome
        assert outcome.status == DeliveryStatus.CONFIRMED.value
        texts.append(outcome.text)

    assert texts[0].startswith("（歪头）")
    assert len(harness.transport.sent) == 3
    assert [message.msg_seq for message in harness.transport.sent] == [1, 1, 1]
    # 第三轮必须带上前两轮的上下文
    third_request = harness.generation_requests()[-1]
    joined = " ".join(str(item["content"]) for item in third_request.messages)
    assert "你好呀" in joined and "刚才那件事" in joined
    assert len(harness.settlements) == 3


async def test_users_are_isolated(harness: _Harness) -> None:
    harness.model.queue_text("回复甲")
    first = harness.submit(message_id="m-1", text="我是用户甲的秘密", user_key="openid-a")
    await harness.service.handle(first)

    harness.model.queue_text("回复乙")
    second = harness.submit(message_id="m-2", text="我是用户乙的话题", user_key="openid-b", offset=5)
    await harness.service.handle(second)

    second_request = harness.generation_requests()[-1]
    joined = " ".join(str(item["content"]) for item in second_request.messages)
    assert "用户甲的秘密" not in joined
    assert "用户乙的话题" in joined


async def test_model_failure_sends_explicit_failure_message(harness: _Harness) -> None:
    harness.model.queue_failure(text=True, reason="model_timeout")
    item = harness.submit(message_id="m-1", text="在吗")
    outcome = await harness.service.handle(item)
    assert outcome.kind == OutboundKind.FALLBACK.value
    assert outcome.status == DeliveryStatus.CONFIRMED.value
    assert "你再说一次" in outcome.text
    assert harness.settlements == [], "兜底不计好感度"


async def test_rejected_delivery_is_not_settled(harness: _Harness) -> None:
    harness.model.queue_text("（别开视线）……随便你")
    harness.transport.queue(DeliveryStatus.REJECTED)
    item = harness.submit(message_id="m-1", text="在吗")
    outcome = await harness.service.handle(item)
    assert outcome.status == DeliveryStatus.REJECTED.value
    assert not outcome.ok
    assert harness.settlements == []
    row = harness.database.query_one("SELECT status, error FROM outbound_delivery")
    assert row["status"] == "rejected"


async def test_unknown_delivery_result_is_uncertain_and_unscored(harness: _Harness) -> None:
    harness.model.queue_text("（别开视线）……随便你")
    harness.transport.queue(DeliveryStatus.UNCERTAIN)
    item = harness.submit(message_id="m-1", text="在吗")
    outcome = await harness.service.handle(item)
    assert outcome.status == DeliveryStatus.UNCERTAIN.value
    assert harness.settlements == []
    assert harness.deliveries.uncertain()


async def test_transport_exception_is_recorded_as_uncertain(harness: _Harness) -> None:
    harness.model.queue_text("（别开视线）……随便你")
    harness.transport.exc = TimeoutError("socket timeout")
    item = harness.submit(message_id="m-1", text="在吗")
    outcome = await harness.service.handle(item)
    assert outcome.status == DeliveryStatus.UNCERTAIN.value
    assert outcome.reason_code == "delivery_exception"
    assert harness.settlements == []


async def test_duplicate_inbound_does_not_create_second_turn(harness: _Harness) -> None:
    harness.model.queue_text("第一次回复")
    item = harness.submit(message_id="m-1", text="在吗")
    first = await harness.service.handle(item)
    second = await harness.service.handle(item)
    assert first.ok
    assert second.status == "duplicate"
    assert harness.database.query("SELECT COUNT(*) AS n FROM turn")[0]["n"] == 1
    assert len(harness.transport.sent) == 1


async def test_budget_exhaustion_blocks_send(harness: _Harness) -> None:
    harness.model.queue_text("（别开视线）……随便你")
    # 时钟推进到窗口之外：事件时间 + 300 秒已过去
    harness.clock.advance(400)
    item = harness.submit(message_id="m-late", text="在吗")
    result = await harness.service.handle(item)
    assert not result.ok
    assert result.reason_code == "passive_window_exhausted"
    assert harness.transport.sent == []


async def test_confirmed_reply_is_reused_as_history(harness: _Harness) -> None:
    harness.model.queue_text("（抱臂）……我知道了")
    first = harness.submit(message_id="m-1", text="今天很累")
    await harness.service.handle(first)
    harness.model.queue_text("（小声）……那早点休息")
    second = harness.submit(message_id="m-2", text="我先走了", offset=30)
    await harness.service.handle(second)

    request = harness.generation_requests()[-1]
    joined = " ".join(str(item["content"]) for item in request.messages)
    assert "（抱臂）……我知道了" in joined


async def test_shared_model_budget_restarts_each_turn(database: Database, clock) -> None:
    """生产装配让提取/生成/审核共用同一个模型预算；它必须每轮复位。

    不复位时，进程启动 30 秒后的每一轮都会直接判定预算耗尽：状态提取永远失败、
    回复退化为兜底。这里用真实的 ChatDeepSeekModel（配假客户端）才带预算判断，
    FakeModel 不检查预算，抓不到该缺陷。
    """
    from types import SimpleNamespace

    from sana_bot.dialogue.deepseek import ChatDeepSeekModel

    class _Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.schemas = ['{"events": []}', '{"approved": true, "violations": []}']
            self.schemas_used: list[str] = []

        async def ainvoke(self, messages, **kwargs):
            self.calls.append(kwargs)
            if "response_format" in kwargs:
                schema = self.schemas.pop(0) if self.schemas else '{"approved": true}'
                self.schemas_used.append(schema)
                return SimpleNamespace(content=schema)
            return SimpleNamespace(content="（抱臂）社团的事我才不想说")

    client = _Client()
    model = ChatDeepSeekModel(
        api_key="test-key", model="deepseek-test", total_budget_seconds=30.0, client=client
    )
    shared = model.budget
    shared.start(0.0)
    assert shared.expired(10_000.0), "先构造出与生产运行 30 秒后等价的已过期预算"

    config = RuntimeConfig()
    inbound = InboundRepository(database)
    service = DialogueTurnService(
        database=database,
        config=config,
        structures=StructureRepository(database),
        inbound=inbound,
        deliveries=DeliveryRepository(database, config=config),
        history=HistoryService(
            structures=StructureRepository(database),
            inbound=inbound,
            deliveries=DeliveryRepository(database, config=config),
        ),
        policy=ConstrainedOutputPolicy(
            constraints=OutputConstraints(), model=model, budget=shared
        ),
        reducer=DeterministicStateReducer(RuleCatalog(version="empty")),
        catalog=RuleCatalog(version="empty"),
        extractor=LangChainStateEventExtractor(model=model, budget=shared),
        model=model,
        transport=FakeQQTransport(clock=clock),
        clock=clock,
    )

    event = make_event(
        message_id="m-budget",
        user_key="openid-budget",
        text="社团的小猫今天怎么样",
        event_time=BASE_TIME,
        received_at=BASE_TIME,
    )
    with database.write():
        outcome = inbound.persist_inbound(event)
    row = inbound.inbound_row(outcome.inbound_id or "")
    assert row is not None
    item = QueueItem(
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
        user_key="openid-budget",
        message_id=row.message_id,
    )

    result = await service.handle(item)

    assert result.ok is True, result
    assert result.reason_code != "turn_exception"
    assert result.text == "（抱臂）社团的事我才不想说", "预算已复位，应真正调用生成"
    assert len(client.calls) == 3, "事件提取、生成与语义审核都应实际发起调用"
    assert shared.started_at not in (None, 0.0), "本轮重新计时，而不是沿用进程启动时的旧起点"
