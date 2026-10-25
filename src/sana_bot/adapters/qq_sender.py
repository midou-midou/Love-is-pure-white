"""QQ C2C 发送适配器。

发前检查 10 秒余量、权限、拒收与额度；HTTP 成功但业务失败不算 confirmed；
网络不明标 uncertain；绝不更换 msg_id/msg_seq 绕过限制；不带有任何加分逻辑。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from sana_bot.adapters.qq_auth import QQAuthClient
from sana_bot.enums import DeliveryStatus
from sana_bot.errors import ExternalServiceError
from sana_bot.ports import DeliveryResult
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.timeutil import utc_now

__all__ = ["QQTextTransport", "SendGuardResult"]

TOKEN_RETRY_LIMIT = 1

logger = logging.getLogger("sana_bot.qq_sender")


@dataclass(frozen=True)
class SendGuardResult:
    """发送前资格判断结果。"""

    allowed: bool
    reason_code: str | None = None
    msg_seq: int = 0
    deadline_seconds: float = 0.0

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass
class QQTextTransport:
    """实现 :class:`sana_bot.ports.QQTransport`。"""

    auth: QQAuthClient
    deliveries: DeliveryRepository
    client: httpx.AsyncClient | None = None
    app_id: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def guard(
        self,
        *,
        source_msg_id: str,
        event_time,
        user_key: str = "",
        deliverable: bool = True,
    ) -> SendGuardResult:
        """发送前资格：预算、10 秒余量、权限与拒收。"""
        decision = self.deliveries.eligible(
            app_id=self.app_id, source_msg_id=source_msg_id, event_time=event_time
        )
        if not deliverable:
            return SendGuardResult(allowed=False, reason_code="user_not_deliverable", msg_seq=decision.msg_seq)
        if not decision.allowed:
            return SendGuardResult(
                allowed=False,
                reason_code=decision.reason_code,
                msg_seq=decision.msg_seq,
            )
        return SendGuardResult(
            allowed=True,
            msg_seq=decision.msg_seq,
            deadline_seconds=(decision.deadline - utc_now()).total_seconds(),
        )

    async def send(
        self,
        *,
        content: str,
        source_msg_id: str,
        msg_seq: int,
        user_key: str,
        platform_msg_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DeliveryResult:
        payload: dict[str, Any] = {
            "content": content,
            "msg_type": 0,
            "msg_seq": msg_seq,
        }
        # 被动回复只认平台消息 id（ROBOT1.0_...）；内部 inbound id 会被判 400。
        # 没有平台 id 视为主动消息，不传 msg_id。
        if platform_msg_id:
            payload["msg_id"] = platform_msg_id
        response: httpx.Response | None = None
        last_error: ExternalServiceError | None = None
        for attempt in range(TOKEN_RETRY_LIMIT + 1):
            token = await self._token()
            response = await self._post(user_key, payload, token)
            if response is None:
                return DeliveryResult(
                    status=DeliveryStatus.UNCERTAIN,
                    msg_seq=msg_seq,
                    reason_code="delivery_unknown",
                    correlation_id=correlation_id,
                )
            if response.status_code in (401, 403) and attempt < TOKEN_RETRY_LIMIT:
                self.auth.invalidate()
                continue
            if response.status_code == 429:
                return DeliveryResult(
                    status=DeliveryStatus.REJECTED,
                    msg_seq=msg_seq,
                    reason_code="rate_limited",
                    correlation_id=correlation_id,
                    details={"retry_after": response.headers.get("Retry-After")},
                )
            last_error = None
            break
        assert response is not None
        if last_error is not None:  # pragma: no cover - 保留诊断路径
            raise last_error

        if response.status_code >= 500:
            return DeliveryResult(
                status=DeliveryStatus.UNCERTAIN,
                msg_seq=msg_seq,
                reason_code="platform_error",
                correlation_id=correlation_id,
                details={"http_status": response.status_code},
            )
        if response.status_code >= 400:
            body = _json_body(response) or {}
            details = {
                "http_status": response.status_code,
                "platform_code": body.get("code"),
                "platform_message": str(body.get("message", ""))[:200],
                "sent_msg_id_kind": _msg_id_kind(platform_msg_id),
            }
            logger.warning(
                "qq_send_rejected http=%s code=%s message=%s msg_id_kind=%s",
                response.status_code,
                details["platform_code"],
                details["platform_message"] or "-",
                details["sent_msg_id_kind"],
            )
            return DeliveryResult(
                status=DeliveryStatus.REJECTED,
                msg_seq=msg_seq,
                reason_code=_http_reason(response),
                correlation_id=correlation_id,
                details=details,
            )
        body = _json_body(response)
        if body is None:
            return DeliveryResult(
                status=DeliveryStatus.UNCERTAIN,
                msg_seq=msg_seq,
                reason_code="delivery_bad_response",
                correlation_id=correlation_id,
            )
        message_id = body.get("id") or body.get("message_id")
        code = body.get("code", 0)
        if code not in (0, None) or not message_id:
            return DeliveryResult(
                status=DeliveryStatus.REJECTED,
                msg_seq=msg_seq,
                reason_code="business_error",
                correlation_id=correlation_id,
                details={"code": code, "message": str(body.get("message", ""))[:120]},
            )
        return DeliveryResult(
            status=DeliveryStatus.CONFIRMED,
            platform_message_id=str(message_id),
            msg_seq=msg_seq,
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------ 内部

    async def _token(self) -> str:
        return await self.auth.access_token()

    async def _post(
        self, user_key: str, payload: dict[str, Any], token: str
    ) -> httpx.Response | None:
        url = f"{self.auth.credentials.base_url}/v2/users/{user_key}/messages"
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=10.0)
        try:
            return await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError:
            # 可能已发出：结果不明，标记 uncertain 而不是失败
            return None
        finally:
            if owns_client:
                await client.aclose()


def _json_body(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _msg_id_kind(platform_msg_id: str | None) -> str:
    """只记录 id 的类别，便于定位 400 又不泄露内容。"""
    if not platform_msg_id:
        return "absent"
    if platform_msg_id.startswith("ROBOT1.0_"):
        return "platform"
    return "unexpected"


def _http_reason(response: httpx.Response) -> str:
    if response.status_code == 403:
        return "no_permission"
    if response.status_code == 429:
        return "rate_limited"
    return "http_rejected"
