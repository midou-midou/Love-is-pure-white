"""FastAPI 应用装配：QQ 回调、健康与就绪端点，以及任务处理器的生命周期。

不向公网暴露管理 HTTP；``/readyz`` 反映存储、完整索引与配置是否就绪，
不返回密钥或用户内容。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response

from sana_bot.adapters.qq import QQWebhookHandler
from sana_bot.enums import InboundStatus
from sana_bot.runtime import RuntimeContext

__all__ = ["ApplicationServices", "build_application", "health_payload", "readiness_payload"]


@dataclass
class ApplicationServices:
    """运行期依赖集合；由 :func:`sana_bot.cli_doctor.build_services` 组装。"""

    context: RuntimeContext
    webhook: QQWebhookHandler
    worker: Any
    maintenance: Any | None = None
    #: WebSocket 长连接客户端；仅当 ``QQ_TRANSPORT=websocket`` 时注入
    gateway: Any | None = None
    _gateway_task: Any = field(default=None, init=False, repr=False)

    async def startup(self) -> dict[str, Any]:
        if self.maintenance is not None:
            recovered_deliveries = self.maintenance.recover_interrupted_deliveries()
        else:  # pragma: no cover - 仅在裁剪装配时走这条路
            with self.context.database.write():
                recovered_deliveries = self.context.deliveries.recover_interrupted()
        # 重启：短期状态回到 neutral，待发独白取消（不补发）
        cleared = self.maintenance.clear_overrides() if self.maintenance else 0
        cancelled = self.maintenance.cancel_monologues() if self.maintenance else 0
        await self.worker.start()
        requeued = await self.worker.recover()
        if self.gateway is not None:
            # 先起 worker 再接事件：连上就可能立刻下发消息
            self._gateway_task = asyncio.create_task(self.gateway.run_forever())
        return {
            "recovered_deliveries": recovered_deliveries,
            "requeued_inbound": len(requeued),
            "cleared_state_overrides": cleared,
            "cancelled_monologues": cancelled,
        }

    async def shutdown(self) -> None:
        if self._gateway_task is not None:
            self._gateway_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._gateway_task
            self._gateway_task = None
        await self.worker.stop()
        if self.maintenance is not None:
            self.maintenance.stop()
        self.context.close()


def health_payload() -> dict[str, str]:
    return {"status": "ok"}


def readiness_payload(context: RuntimeContext) -> tuple[int, dict[str, Any]]:
    """就绪状态：存储可读写、配置合法、存在已激活的完整索引。"""
    problems: list[str] = []
    try:
        context.database.query_one("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"storage_unavailable:{type(exc).__name__}")
    config_issues = context.config.issues()
    problems.extend(config_issues)
    snapshot = None
    try:
        snapshot = context.structures.active_snapshot()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"index_unavailable:{type(exc).__name__}")
    if snapshot is None:
        problems.append("no_active_index")
    ready = not problems
    payload = {
        "ready": ready,
        "problems": problems,
        "index_id": snapshot.id if snapshot else None,
        "snapshot_hash": snapshot.snapshot_hash if snapshot else None,
        "monologue_status": snapshot.monologue_status.value if snapshot else None,
        "config_version": context.config.version,
    }
    return (200 if ready else 503), payload


def build_application(services: ApplicationServices, *, title: str = "sana-bot") -> FastAPI:
    """构造服务实例；管理接口只走本地 CLI。"""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        services.startup_result = await services.startup()  # type: ignore[attr-defined]
        try:
            yield
        finally:
            await services.shutdown()

    app = FastAPI(title=title, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return health_payload()

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, Any]:
        status, payload = readiness_payload(services.context)
        response.status_code = status
        return payload

    @app.post("/qq/webhook")
    async def webhook(request: Request) -> Response:
        if services.context.environment.qq_transport != "webhook":
            # 长连接模式下回调入口不参与收发，避免同一条消息被受理两次
            return Response(
                content='{"status": "error", "reason_code": "webhook_disabled"}',
                status_code=409,
                media_type="application/json",
            )
        raw_body = await request.body()
        signature = request.headers.get("X-Signature-Ed25519")
        timestamp = request.headers.get("X-Signature-Timestamp")
        decision = await services.webhook.handle(
            raw_body=raw_body,
            signature=signature,
            timestamp=timestamp,
        )
        import json

        return Response(
            content=json.dumps(decision.to_response(), ensure_ascii=False),
            status_code=decision.status_code,
            media_type="application/json",
        )

    return app


def ignored_statuses() -> tuple[InboundStatus, ...]:
    return (InboundStatus.IGNORED,)
