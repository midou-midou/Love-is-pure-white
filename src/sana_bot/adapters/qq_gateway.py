"""QQ 官方 WebSocket 长连接接入。

与 webhook 模式互斥：进程出网连 ``wss://api.bot.qq.com/websocket/``，因此**不需要**
公网域名、证书或备案。事件仍然走与 webhook 完全相同的受理链路（去重落盘 + 每用户
串行队列），回复依旧通过 OpenAPI 发送。

官方契约（dev-prepare/interface-framework/event-emit.html）：

- 建连后先收 ``op=10 Hello``，``d.heartbeat_interval`` 为毫秒心跳周期
- ``op=2 Identify``：``d.token`` 必须是 ``"QQBot {access_token}"``，另带 ``intents``/``shard``
- 鉴权成功后 ``op=0 t=READY`` 下发 ``d.session_id``
- ``op=1`` 心跳，``d`` 为最近一次收到事件的 ``s``（首连为 ``null``），回 ``op=11``
- 断线重连先发 ``op=6 Resume``（token/session_id/seq），补发完成后收 ``RESUMED``
- ``op=7`` 服务端要求重连；``op=9`` 鉴权参数有误（token 可能过期，需重新 Identify）

设计约束：连接对象、睡眠、HTTP 客户端都可注入，测试全程离线；断线一律退避重连，
任何异常都不允许把进程带走。
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from sana_bot.enums import DiagnosticCategory
from sana_bot.errors import ExternalServiceError

__all__ = [
    "GatewayOptions",
    "GatewayConnection",
    "QQGatewayClient",
    "fetch_gateway_url",
    "HELLO_TIMEOUT_SECONDS",
]

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

EVENT_READY = "READY"
EVENT_RESUMED = "RESUMED"

GATEWAY_PATH = "/gateway"
HELLO_TIMEOUT_SECONDS = 30.0


@runtime_checkable
class GatewayConnection(Protocol):
    """只用到官方协议所需的最小连接能力。"""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[GatewayConnection]]
Sleeper = Callable[[float], Awaitable[None]]
AcceptCallable = Callable[[Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class GatewayOptions:
    """连接参数；默认订阅单聊/群事件（1 << 25）。"""

    intents: int = 1 << 25
    shard: tuple[int, int] = (0, 1)
    properties: Mapping[str, str] = field(
        default_factory=lambda: {
            "$os": "linux",
            "$browser": "sana-bot",
            "$device": "sana-bot",
        }
    )
    gateway_url: str | None = None
    reconnect_base_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    heartbeat_timeout_seconds: float = HELLO_TIMEOUT_SECONDS


async def _connect_default(url: str) -> GatewayConnection:
    from websockets.asyncio.client import connect

    # 官方网关自己发心跳；不要用 websockets 的 ping 以免与 op 1 混淆
    return await connect(url, ping_interval=None, close_timeout=5, max_size=16 * 1024 * 1024)


async def fetch_gateway_url(
    *,
    base_url: str,
    access_token: str,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> str:
    """``GET /gateway`` 取 WSS 接入点；只回报 host，不打印 token。"""
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    headers = {"Authorization": f"QQBot {access_token}"}
    try:
        response = await http.get(base_url.rstrip("/") + GATEWAY_PATH, headers=headers)
    except httpx.HTTPError as exc:
        raise ExternalServiceError(
            f"获取网关地址网络失败：{type(exc).__name__}",
            reason_code="qq_gateway_network_error",
        ) from exc
    finally:
        if owns_client:
            await http.aclose()
    if response.status_code >= 400:
        raise ExternalServiceError(
            f"获取网关地址失败：HTTP {response.status_code}",
            reason_code="qq_gateway_http_error",
            details={"http_status": response.status_code},
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ExternalServiceError(
            "获取网关地址响应不是 JSON",
            reason_code="qq_gateway_bad_response",
        ) from exc
    url = body.get("url") if isinstance(body, dict) else None
    if not isinstance(url, str) or not url.startswith("wss://"):
        raise ExternalServiceError(
            "获取网关地址响应缺少合法 wss 地址",
            reason_code="qq_gateway_bad_response",
            details={"response_keys": sorted(map(str, body)) if isinstance(body, dict) else []},
        )
    return url


class _Reconnect(Exception):
    """服务端要求重连（op=7）：保留 session 以便 Resume。"""


class _ResumeInvalid(Exception):
    """session 失效（op=9）：清空 session，重新 Identify。"""


class QQGatewayClient:
    """一条长连接的完整生命周期：鉴权、心跳、分发、断线续传。"""

    def __init__(
        self,
        *,
        auth: Any,
        accept: AcceptCallable,
        options: GatewayOptions | None = None,
        connector: Connector | None = None,
        http_client: httpx.AsyncClient | None = None,
        diagnostics: Any | None = None,
        sleep: Sleeper | None = None,
        logger: Callable[[str], None] | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> None:
        self.auth = auth
        self.accept = accept
        self.options = options or GatewayOptions()
        self._connector = connector or _connect_default
        self._http_client = http_client
        self._diagnostics = diagnostics
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._log = logger or (lambda message: None)
        self._rand = random_source or random.random
        self._session_id: str | None = None
        self._seq: int | None = None
        self._stopped = False
        self.attempts = 0
        self.identify_count = 0
        self.resume_count = 0
        self.dispatched = 0

    # ------------------------------------------------------------------ 对外

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def seq(self) -> int | None:
        return self._seq

    def stop(self) -> None:
        self._stopped = True

    async def run_forever(self) -> None:
        """永不返回（除非被取消）；每次失败都退避重连，不向上抛异常。"""
        while not self._stopped:
            try:
                await self._run_session()
                self.attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 长连接不得带走进程
                self.attempts += 1
                delay = self._backoff(self.attempts)
                self._record(
                    "gateway_disconnected",
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:200],
                        "attempt": self.attempts,
                        "retry_in_seconds": round(delay, 2),
                        "has_session": bool(self._session_id),
                    },
                )
                await self._sleep(delay)

    # ------------------------------------------------------------------ 单次连接

    async def _run_session(self) -> None:
        token = await self.auth.access_token()
        url = self.options.gateway_url or await fetch_gateway_url(
            base_url=self._base_url(), access_token=token, client=self._http_client
        )
        connection = await self._connector(url)
        heartbeat: asyncio.Task[None] | None = None
        try:
            interval = await self._await_hello(connection)
            if self._session_id and self._seq is not None:
                await connection.send(
                    _dumps(
                        {
                            "op": OP_RESUME,
                            "d": {
                                "token": f"QQBot {token}",
                                "session_id": self._session_id,
                                "seq": self._seq,
                            },
                        }
                    )
                )
                self.resume_count += 1
                self._record(
                    "gateway_resume", {"seq": self._seq, "url_host": _host_of(url)}
                )
            else:
                await connection.send(
                    _dumps(
                        {
                            "op": OP_IDENTIFY,
                            "d": {
                                "token": f"QQBot {token}",
                                "intents": self.options.intents,
                                "shard": list(self.options.shard),
                                "properties": dict(self.options.properties),
                            },
                        }
                    )
                )
                self.identify_count += 1
                self._record(
                    "gateway_identify",
                    {
                        "intents": self.options.intents,
                        "shard": list(self.options.shard),
                        "url_host": _host_of(url),
                    },
                )
            heartbeat = asyncio.create_task(self._heartbeat(connection, interval))
            self._record("gateway_connected", {"url_host": _host_of(url), "shard": list(self.options.shard)})
            await self._receive_loop(connection)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await _safe_close(connection)

    async def _await_hello(self, connection: GatewayConnection) -> float:
        while True:
            payload = await asyncio.wait_for(
                _recv_json(connection), timeout=self.options.heartbeat_timeout_seconds
            )
            if payload.get("op") == OP_HELLO:
                data = payload.get("d")
                interval_ms = data.get("heartbeat_interval") if isinstance(data, dict) else None
                if not isinstance(interval_ms, (int, float)) or interval_ms <= 0:
                    raise ExternalServiceError(
                        "Hello 未给出合法 heartbeat_interval",
                        reason_code="qq_gateway_bad_hello",
                    )
                return float(interval_ms) / 1000.0
            # Hello 之前收到其它消息不改变流程，继续等

    async def _heartbeat(self, connection: GatewayConnection, interval: float) -> None:
        try:
            while True:
                await self._sleep(interval)
                await connection.send(_dumps({"op": OP_HEARTBEAT, "d": self._seq}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 心跳失败等于连接已坏，主动断开让主循环重连
            await _safe_close(connection)

    async def _receive_loop(self, connection: GatewayConnection) -> None:
        while True:
            payload = await _recv_json(connection)
            await self._handle(connection, payload)

    async def _handle(self, connection: GatewayConnection, payload: Mapping[str, Any]) -> None:
        op = payload.get("op")
        if op == OP_DISPATCH:
            await self._handle_dispatch(payload)
            return
        if op == OP_HEARTBEAT:
            # 服务端主动要心跳
            await connection.send(_dumps({"op": OP_HEARTBEAT, "d": self._seq}))
            return
        if op == OP_HEARTBEAT_ACK:
            return
        if op == OP_RECONNECT:
            raise _Reconnect()
        if op == OP_INVALID_SESSION:
            self._session_id = None
            self._seq = None
            await self._refresh_token()
            self._record("gateway_session_invalid", {"op": op})
            raise _ResumeInvalid()
        if op == OP_HELLO:
            return
        self._record("gateway_unknown_op", {"op": op})

    async def _handle_dispatch(self, payload: Mapping[str, Any]) -> None:
        seq = payload.get("s")
        if isinstance(seq, int):
            self._seq = seq
        event_type = payload.get("t")
        if event_type == EVENT_READY:
            data = payload.get("d")
            session_id = data.get("session_id") if isinstance(data, dict) else None
            if isinstance(session_id, str) and session_id:
                self._session_id = session_id
            self._record("gateway_ready", {"session_present": bool(self._session_id)})
            return
        if event_type == EVENT_RESUMED:
            self._record("gateway_resumed", {"seq": self._seq})
            return
        self.dispatched += 1
        try:
            await self.accept(payload)
        except Exception as exc:  # noqa: BLE001 - 单条事件失败不得断开长连接
            self._record(
                "gateway_dispatch_failed",
                {"event_type": str(event_type), "error_type": type(exc).__name__},
            )

    # ------------------------------------------------------------------ 辅助

    async def _refresh_token(self) -> None:
        refresh = getattr(self.auth, "refresh", None)
        if refresh is None:
            return
        try:
            await refresh()
        except Exception as exc:  # noqa: BLE001 - 刷新失败沿用旧 token，下一轮重连再试
            self._record("gateway_token_refresh_failed", {"error_type": type(exc).__name__})

    def _base_url(self) -> str:
        credentials = getattr(self.auth, "credentials", None)
        return getattr(credentials, "base_url", "") or "https://api.bot.qq.com"

    def _backoff(self, attempt: int) -> float:
        base = min(
            self.options.reconnect_max_seconds,
            self.options.reconnect_base_seconds * (2 ** max(0, attempt - 1)),
        )
        # 抖动避免多实例同时重连打爆网关
        return base * (0.5 + self._rand() * 0.5)

    def _record(self, reason: str, payload: Mapping[str, Any]) -> None:
        self._log(f"{reason} {dict(payload)}")
        if self._diagnostics is None:
            return
        try:
            from sana_bot.diagnostics import DiagnosticRecord

            self._diagnostics.record(
                DiagnosticRecord(
                    category=DiagnosticCategory.TRANSPORT,
                    correlation_id=f"gateway-{reason}",
                    reason=reason,
                    payload=dict(payload),
                    occurred_at=datetime.now(UTC).isoformat(),
                )
            )
        except Exception:  # noqa: BLE001 - 诊断失败不影响长连接
            pass


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def _recv_json(connection: GatewayConnection) -> dict[str, Any]:
    raw = await connection.recv()
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExternalServiceError(
            "网关消息不是合法 JSON", reason_code="qq_gateway_bad_frame"
        ) from exc
    if not isinstance(payload, dict):
        raise ExternalServiceError("网关消息不是 JSON 对象", reason_code="qq_gateway_bad_frame")
    return payload


async def _safe_close(connection: GatewayConnection) -> None:
    try:
        await connection.close()
    except Exception:  # noqa: BLE001
        pass


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url
    except Exception:  # noqa: BLE001
        return "unknown"
