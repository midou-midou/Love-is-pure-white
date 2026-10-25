"""WebSocket 长连接协议：Hello/Identify/心跳/Resume/op7/op9 与事件分发。

全程离线：连接对象、睡眠、HTTP 客户端都注入替身，不建立真实连接。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from sana_bot.adapters.qq_gateway import (
    GatewayOptions,
    QQGatewayClient,
    fetch_gateway_url,
)
from sana_bot.errors import ExternalServiceError

CLOSE = object()


class FakeConnection:
    """按脚本投递帧的连接；``sent`` 记录客户端发出的帧。"""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self.inbox.get()
        if item is CLOSE:
            raise ConnectionResetError("连接已断开")
        return json.dumps(item, ensure_ascii=False)

    async def close(self) -> None:
        self.closed = True

    def push(self, payload: dict[str, Any]) -> None:
        self.inbox.put_nowait(payload)

    def disconnect(self) -> None:
        self.inbox.put_nowait(CLOSE)


class FakeAuth:
    """access_token 替身；refresh 只记录次数。"""

    def __init__(self, token: str = "tok-1") -> None:
        self.token = token
        self.refresh_calls = 0
        self.credentials = type("C", (), {"base_url": "https://api.bot.qq.com"})()

    async def access_token(self) -> str:
        return self.token

    async def refresh(self) -> str:
        self.refresh_calls += 1
        self.token = f"{self.token}-r{self.refresh_calls}"
        return self.token


async def _noop_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _client(
    connections: list[FakeConnection],
    *,
    accept: Any | None = None,
    auth: FakeAuth | None = None,
    options: GatewayOptions | None = None,
) -> tuple[QQGatewayClient, list[dict[str, Any]], FakeAuth]:
    accepted: list[dict[str, Any]] = []

    async def default_accept(payload: dict[str, Any]) -> None:
        accepted.append(payload)

    pending = list(connections)

    async def connector(_url: str) -> FakeConnection:
        return pending.pop(0)

    client = QQGatewayClient(
        auth=auth or FakeAuth(),
        accept=accept or default_accept,
        options=options or GatewayOptions(gateway_url="wss://api.bot.qq.com/websocket/"),
        connector=connector,
        sleep=_noop_sleep,
    )
    return client, accepted, client.auth


async def _wait_sent(conn: FakeConnection, count: int, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while len(conn.sent) < count:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"等待第 {count} 帧超时，实际 {conn.sent}")
        await asyncio.sleep(0)


def _hello(interval: int = 45000) -> dict[str, Any]:
    return {"op": 10, "d": {"heartbeat_interval": interval}}


def _ready(session_id: str = "sess-1", seq: int = 1) -> dict[str, Any]:
    return {"op": 0, "s": seq, "t": "READY", "d": {"session_id": session_id}}


def _c2c(message_id: str = "m-1", seq: int = 2) -> dict[str, Any]:
    return {
        "op": 0,
        "s": seq,
        "t": "C2C_MESSAGE_CREATE",
        "id": f"evt-{message_id}",
        "d": {
            "id": message_id,
            "content": "在吗",
            "timestamp": "2026-10-16T12:00:00+00:00",
            "author": {"user_openid": "openid-1"},
        },
    }


async def _drive(client: QQGatewayClient, conn: FakeConnection) -> None:
    """跑到连接断开为止（__run_session 的异常即"断线"）。"""
    task = asyncio.create_task(client._run_session())
    while not task.done():
        await asyncio.sleep(0)
        if conn.closed:
            break
    conn.disconnect()
    with pytest.raises(Exception):
        await task


# --------------------------------------------------------------------------- Identify


async def test_identify_uses_official_token_prefix_and_intents() -> None:
    conn = FakeConnection()
    client, _accepted, _auth = _client([conn])
    conn.push(_hello())

    task = asyncio.create_task(client._run_session())
    await _wait_sent(conn, 1)
    conn.disconnect()
    with pytest.raises(Exception):
        await task

    identify = conn.sent[0]
    assert identify["op"] == 2
    assert identify["d"]["token"] == "QQBot tok-1"
    assert identify["d"]["intents"] == 1 << 25
    assert identify["d"]["shard"] == [0, 1]
    assert client.identify_count == 1
    assert client.resume_count == 0
    assert conn.closed is True


async def test_custom_intents_and_shard_are_forwarded() -> None:
    conn = FakeConnection()
    client, _accepted, _auth = _client(
        [conn], options=GatewayOptions(intents=0, shard=(2, 4), gateway_url="wss://gw/x")
    )
    conn.push(_hello())
    task = asyncio.create_task(client._run_session())
    await _wait_sent(conn, 1)
    conn.disconnect()
    with pytest.raises(Exception):
        await task
    assert conn.sent[0]["d"]["intents"] == 0
    assert conn.sent[0]["d"]["shard"] == [2, 4]


# --------------------------------------------------------------------------- Ready/心跳


async def test_ready_stores_session_and_tracks_seq() -> None:
    conn = FakeConnection()
    client, accepted, _auth = _client([conn])
    conn.push(_hello())
    conn.push(_ready(session_id="sess-abc", seq=1))
    conn.push(_c2c("m-1", seq=7))

    task = asyncio.create_task(client._run_session())
    await _wait_sent(conn, 1)
    deadline = asyncio.get_running_loop().time() + 1.0
    while client.seq != 7 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    conn.disconnect()
    with pytest.raises(Exception):
        await task

    assert client.session_id == "sess-abc"
    assert client.seq == 7
    assert len(accepted) == 1
    assert accepted[0]["t"] == "C2C_MESSAGE_CREATE"
    assert client.dispatched == 1


async def test_heartbeat_carries_last_seq() -> None:
    conn = FakeConnection()
    client, _accepted, _auth = _client([conn])
    client._seq = 42

    task = asyncio.create_task(client._heartbeat(conn, 0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn.sent, "心跳必须至少发出一次"
    assert conn.sent[0] == {"op": 1, "d": 42}


async def test_heartbeat_first_frame_sends_null_seq() -> None:
    conn = FakeConnection()
    client, _accepted, _auth = _client([conn])
    task = asyncio.create_task(client._heartbeat(conn, 0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.sent[0] == {"op": 1, "d": None}


async def test_bad_hello_interval_is_rejected() -> None:
    conn = FakeConnection()
    client, _accepted, _auth = _client([conn])
    conn.push({"op": 10, "d": {"heartbeat_interval": 0}})

    with pytest.raises(ExternalServiceError) as info:
        await client._run_session()
    assert info.value.reason_code == "qq_gateway_bad_hello"


async def test_server_requested_heartbeat_is_answered() -> None:
    conn = FakeConnection()
    client, _accepted, _auth = _client([conn])
    client._seq = 9
    await client._handle(conn, {"op": 1})
    assert conn.sent[-1] == {"op": 1, "d": 9}


# --------------------------------------------------------------------------- Resume


async def test_reconnect_resumes_with_session_and_seq() -> None:
    first, second = FakeConnection(), FakeConnection()
    client, _accepted, _auth = _client([first, second])

    first.push(_hello())
    first.push(_ready(session_id="sess-1", seq=3))
    task = asyncio.create_task(client._run_session())
    await _wait_sent(first, 1)
    deadline = asyncio.get_running_loop().time() + 1.0
    while client.session_id is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    first.disconnect()
    with pytest.raises(Exception):
        await task

    second.push(_hello())
    task2 = asyncio.create_task(client._run_session())
    await _wait_sent(second, 1)
    second.disconnect()
    with pytest.raises(Exception):
        await task2

    resume = second.sent[0]
    assert resume["op"] == 6
    assert resume["d"]["session_id"] == "sess-1"
    assert resume["d"]["seq"] == 3
    assert resume["d"]["token"] == "QQBot tok-1"
    assert client.resume_count == 1


async def test_op7_reconnect_reuses_session() -> None:
    first, second = FakeConnection(), FakeConnection()
    client, _accepted, _auth = _client([first, second])

    first.push(_hello())
    first.push(_ready(session_id="sess-7", seq=5))
    first.push({"op": 7})
    second.push(_hello())
    second.push({"op": 0, "s": 6, "t": "RESUMED", "d": ""})

    async def stop_after_first_resume() -> None:
        await _wait_sent(second, 1)
        client.stop()
        second.disconnect()

    stopper = asyncio.create_task(stop_after_first_resume())
    await asyncio.wait_for(client.run_forever(), timeout=2.0)
    stopper.cancel()

    assert second.sent[0]["op"] == 6, "op7 之后应带 session 续传而不是重新 Identify"
    assert client.resume_count == 1
    assert client.identify_count == 1


async def test_op9_clears_session_and_refreshes_token() -> None:
    conn = FakeConnection()
    auth = FakeAuth()
    client, _accepted, _auth = _client([conn], auth=auth)
    conn.push(_hello())
    conn.push(_ready(session_id="sess-9", seq=2))
    conn.push({"op": 9, "d": False})

    task = asyncio.create_task(client._run_session())
    await _wait_sent(conn, 1)
    with pytest.raises(Exception):
        await asyncio.wait_for(task, timeout=2.0)

    assert client.session_id is None
    assert client.seq is None
    assert auth.refresh_calls == 1


# --------------------------------------------------------------------------- 分发容错


async def test_dispatch_failure_keeps_connection_alive() -> None:
    conn = FakeConnection()
    calls: list[int] = []

    async def flaky(payload: dict[str, Any]) -> None:
        calls.append(int(payload.get("s") or 0))
        raise RuntimeError("受理失败")

    client, _accepted, _auth = _client([conn], accept=flaky)
    conn.push(_hello())
    conn.push(_c2c("m-1", seq=2))
    conn.push(_c2c("m-2", seq=3))

    task = asyncio.create_task(client._run_session())
    await _wait_sent(conn, 1)
    deadline = asyncio.get_running_loop().time() + 1.0
    while len(calls) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    still_open = not conn.closed
    conn.disconnect()
    with pytest.raises(Exception):
        await task

    assert calls == [2, 3], "单条事件失败不得中断后续事件"
    assert still_open is True
    assert client.seq == 3


async def test_unknown_op_is_ignored() -> None:
    conn = FakeConnection()
    client, _accepted, _auth = _client([conn])
    await client._handle(conn, {"op": 99})
    assert conn.sent == []


# --------------------------------------------------------------------------- 退避


def test_backoff_is_jittered_and_bounded() -> None:
    client = QQGatewayClient(
        auth=FakeAuth(),
        accept=lambda payload: None,  # type: ignore[arg-type]
        connector=lambda url: None,  # type: ignore[arg-type]
        options=GatewayOptions(reconnect_base_seconds=1.0, reconnect_max_seconds=8.0),
        random_source=lambda: 0.0,
    )
    assert client._backoff(1) == pytest.approx(0.5)
    assert client._backoff(4) == pytest.approx(4.0)
    assert client._backoff(50) <= 8.0


# --------------------------------------------------------------------------- /gateway


async def test_fetch_gateway_url_returns_wss_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "QQBot tok-1"
        return httpx.Response(200, json={"url": "wss://api.bot.qq.com/websocket/"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    url = await fetch_gateway_url(
        base_url="https://api.bot.qq.com", access_token="tok-1", client=client
    )
    assert url == "wss://api.bot.qq.com/websocket/"
    await client.aclose()


async def test_fetch_gateway_url_rejects_non_wss() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": "https://api.bot.qq.com/websocket/"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExternalServiceError) as info:
        await fetch_gateway_url(
            base_url="https://api.bot.qq.com", access_token="tok-1", client=client
        )
    assert info.value.reason_code == "qq_gateway_bad_response"
    await client.aclose()


async def test_fetch_gateway_url_reports_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExternalServiceError) as info:
        await fetch_gateway_url(
            base_url="https://api.bot.qq.com", access_token="tok-1", client=client
        )
    assert info.value.reason_code == "qq_gateway_http_error"
    await client.aclose()
