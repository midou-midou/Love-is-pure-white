"""长连接事件与 webhook 共用同一条受理链路：去重、落盘、串行入队。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sana_bot.adapters.qq import QQWebhookHandler
from sana_bot.adapters.qq_gateway import GatewayOptions, QQGatewayClient
from sana_bot.app import ApplicationServices, build_application
from sana_bot.config import ConfigStore
from sana_bot.runtime import open_context
from sana_bot.scheduling.serial_queue import InboundQueueService, UserSerialQueue
from sana_bot.storage.inbound_repository import InboundRepository

APP_ID = "123456"


class FakeConnection:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise ConnectionResetError("连接已断开")
        return json.dumps(item, ensure_ascii=False)

    async def close(self) -> None:
        self.closed = True

    def push(self, payload: dict) -> None:
        self.inbox.put_nowait(payload)

    def disconnect(self) -> None:
        self.inbox.put_nowait(None)


class FakeAuth:
    credentials = type("C", (), {"base_url": "https://api.bot.qq.com"})()

    async def access_token(self) -> str:
        return "tok-1"

    async def refresh(self) -> str:
        return "tok-1"


def _c2c(message_id: str, *, seq: int) -> dict:
    return {
        "op": 0,
        "s": seq,
        "t": "C2C_MESSAGE_CREATE",
        "id": f"evt-{message_id}",
        "appid": APP_ID,
        "d": {
            "id": message_id,
            "content": "你的弟弟是谁",
            "timestamp": "2026-10-16T12:00:00+00:00",
            "author": {"user_openid": "openid-1"},
        },
    }


async def test_duplicate_dispatch_is_deduped_and_enqueued_once(database) -> None:
    queue = UserSerialQueue(capacity_per_user=8, item_timeout_seconds=1.0)
    handled: list[str] = []

    async def consume(item) -> None:
        handled.append(item.inbound_id)

    await queue.start(consume)
    service = InboundQueueService(
        database=database, repository=InboundRepository(database), queue=queue
    )
    handler = QQWebhookHandler(queue_service=service, secret=None, app_id=APP_ID)
    outcomes: list = []

    async def accept(payload: dict) -> None:
        outcomes.append(await handler.accept_dispatch(payload))

    conn = FakeConnection()

    async def connector(_url: str) -> FakeConnection:
        return conn

    client = QQGatewayClient(
        auth=FakeAuth(),
        accept=accept,
        options=GatewayOptions(gateway_url="wss://gw/x"),
        connector=connector,
        sleep=_noop,
    )
    conn.push({"op": 10, "d": {"heartbeat_interval": 45000}})
    conn.push({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sess-1"}})
    conn.push(_c2c("m-1", seq=2))
    conn.push(_c2c("m-1", seq=3))  # 重连补发的重复事件

    task = asyncio.create_task(client._run_session())
    deadline = asyncio.get_running_loop().time() + 2.0
    while len(outcomes) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    conn.disconnect()
    with pytest.raises(Exception):
        await task
    await queue.stop()

    assert len(outcomes) == 2
    first, second = outcomes
    assert first.status_code == 200
    assert first.enqueue is not None and first.enqueue.accepted is True
    assert second.enqueue is not None
    assert second.enqueue.duplicate is True, "重复事件必须被去重，不能二次入队"
    assert handled == [first.enqueue.inbound_id], "只应消费一次"


async def test_unsupported_event_type_is_ignored_without_error(database) -> None:
    queue = UserSerialQueue(capacity_per_user=8, item_timeout_seconds=1.0)

    async def consume(item) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("不支持的事件不应入队")

    await queue.start(consume)
    service = InboundQueueService(
        database=database, repository=InboundRepository(database), queue=queue
    )
    handler = QQWebhookHandler(queue_service=service, secret=None, app_id=APP_ID)
    decision = await handler.accept_dispatch(
        {"op": 0, "s": 9, "t": "GROUP_ADD_ROBOT", "d": {}}
    )
    await queue.stop()
    assert decision.status_code == 200
    assert decision.reason_code == "ignored_unsupported_type"


def _runtime_context(tmp_path: Path):
    import argparse

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    ConfigStore(runtime / "config.json").apply_payload(
        {"keywords": ["谢谢"], "romance_threshold": 100.0}, source="test"
    )
    return open_context(argparse.Namespace(runtime_dir=str(runtime)))


class _DummyWorker:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def recover(self) -> list[str]:
        return []


def test_webhook_route_is_disabled_in_websocket_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("QQ_TRANSPORT", "websocket")
    context = _runtime_context(tmp_path)
    try:
        services = ApplicationServices(context=context, webhook=None, worker=_DummyWorker())
        app = build_application(services)
        with TestClient(app) as client:
            response = client.post("/qq/webhook", content=b"{}")
        assert response.status_code == 409
        assert response.json()["reason_code"] == "webhook_disabled"
    finally:
        context.close()


def test_webhook_route_stays_available_in_webhook_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.delenv("QQ_TRANSPORT", raising=False)
    context = _runtime_context(tmp_path)
    try:
        services = ApplicationServices(context=context, webhook=None, worker=_DummyWorker())
        app = build_application(services)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/qq/webhook", content=b"{}")
        # webhook=None 会 500，但关键是没有被 409 拦掉：说明路由仍走回调分支
        assert response.status_code == 500
    finally:
        context.close()


async def _noop(_seconds: float) -> None:
    await asyncio.sleep(0)
