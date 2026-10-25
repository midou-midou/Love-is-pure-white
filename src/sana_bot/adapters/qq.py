"""QQ C2C 入站适配：原始 body 验签、op13 挑战、去重落盘与快速 ACK。

回调必须：验签 → 持久化/去重 → 迅速 ACK；不阻塞等待模型。签名无效 401、
结构非法 400、无法持久化 503，绝不先 ACK 后丢事件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sana_bot.adapters.qq_auth import challenge_response, verify_dispatch_signature
from sana_bot.enums import InboundStatus
from sana_bot.ports import NormalizedEvent
from sana_bot.scheduling.serial_queue import EnqueueOutcome, InboundQueueService
from sana_bot.storage.inbound_repository import payload_hash_of

__all__ = [
    "WebhookDecision",
    "UnsupportedEvent",
    "normalize_c2c_event",
    "parse_webhook_body",
    "QQWebhookHandler",
    "C2C_EVENT_TYPE",
]

C2C_EVENT_TYPE = "C2C_MESSAGE_CREATE"
OP_DISPATCH = 0
OP_CHALLENGE = 13


@dataclass(frozen=True)
class WebhookDecision:
    status_code: int
    payload: dict[str, Any]
    reason_code: str | None = None
    enqueue: EnqueueOutcome | None = None
    event: NormalizedEvent | None = None

    def to_response(self) -> dict[str, Any]:
        return self.payload


@dataclass(frozen=True)
class UnsupportedEvent:
    """有效但不支持的事件：ACK 并标 ignored，不调用模型、不计分。"""

    event_key: str
    message_id: str
    app_id: str
    user_key: str
    event_time: datetime
    reason_code: str
    hint: str


def parse_webhook_body(raw_body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _event_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return fallback
    return fallback


def normalize_c2c_event(
    payload: dict[str, Any],
    *,
    received_at: datetime,
) -> NormalizedEvent | UnsupportedEvent | None:
    """把 op=0 的 C2C 事件归一化为应用事件；结构非法返回 None。"""
    data = payload.get("d")
    if not isinstance(data, dict):
        return None
    message_id = data.get("id")
    author = data.get("author") or {}
    user_openid = author.get("user_openid") if isinstance(author, dict) else None
    if not isinstance(message_id, str) or not message_id or not isinstance(user_openid, str):
        return None
    outer_id = payload.get("id")
    event_key = str(outer_id) if outer_id else message_id
    app_id = str(payload.get("appid") or payload.get("app_id") or "")
    event_time = _event_time(data.get("timestamp"), received_at)
    content = data.get("content")
    attachments = data.get("attachments") or []
    if not isinstance(content, str) or not content.strip():
        if attachments:
            return UnsupportedEvent(
                event_key=event_key,
                message_id=message_id,
                app_id=app_id,
                user_key=user_openid,
                event_time=event_time,
                reason_code="unsupported_media",
                hint="这个我看不了，发文字给我",
            )
        return NormalizedEvent(
            event_key=event_key,
            app_id=app_id,
            user_key=user_openid,
            message_id=message_id,
            event_time=event_time,
            received_at=received_at,
            text="",
            payload_hash=payload_hash_of(data),
            platform_event_id=str(outer_id) if outer_id else None,
            supported=False,
        )
    return NormalizedEvent(
        event_key=event_key,
        app_id=app_id,
        user_key=user_openid,
        message_id=message_id,
        event_time=event_time,
        received_at=received_at,
        text=content.strip(),
        payload_hash=payload_hash_of(data),
        platform_event_id=str(outer_id) if outer_id else None,
        supported=True,
    )


class QQWebhookHandler:
    """验签、挑战应答与入站受理。"""

    def __init__(
        self,
        *,
        queue_service: InboundQueueService,
        secret: str | None,
        app_id: str | None,
    ) -> None:
        self.queue_service = queue_service
        self.secret = secret
        self.app_id = app_id

    # ------------------------------------------------------------------ 入站

    async def handle(
        self,
        *,
        raw_body: bytes,
        signature: str | None,
        timestamp: str | None,
        received_at: datetime | None = None,
    ) -> WebhookDecision:
        payload = parse_webhook_body(raw_body)
        if payload is None:
            return WebhookDecision(
                status_code=400,
                payload={"status": "error", "reason_code": "invalid_body"},
                reason_code="invalid_body",
            )
        op = payload.get("op")
        if op == OP_CHALLENGE:
            return self._challenge(payload)
        if op != OP_DISPATCH:
            return WebhookDecision(
                status_code=400,
                payload={"status": "error", "reason_code": "unsupported_op"},
                reason_code="unsupported_op",
            )
        if not self._verify(payload, signature=signature, timestamp=timestamp, raw_body=raw_body):
            return WebhookDecision(
                status_code=401,
                payload={"status": "error", "reason_code": "bad_signature"},
                reason_code="bad_signature",
            )
        return await self._accept(payload, received_at=received_at or datetime.now(UTC))

    def _verify(
        self,
        payload: dict[str, Any],
        *,
        signature: str | None,
        timestamp: str | None,
        raw_body: bytes,
    ) -> bool:
        if payload.get("op") == OP_CHALLENGE:
            return False
        if not self.secret:
            return False
        appid = payload.get("appid") or payload.get("app_id")
        if self.app_id and appid and str(appid) != str(self.app_id):
            return False
        return verify_dispatch_signature(
            secret=self.secret,
            signature_hex=signature or "",
            timestamp=timestamp or "",
            raw_body=raw_body,
        )

    def _challenge(self, payload: dict[str, Any]) -> WebhookDecision:
        data = payload.get("d") or {}
        if not isinstance(data, dict):
            return WebhookDecision(
                status_code=400,
                payload={"status": "error", "reason_code": "invalid_challenge"},
                reason_code="invalid_challenge",
            )
        plain_token = data.get("plain_token")
        event_ts = data.get("event_ts")
        if not isinstance(plain_token, str) or not isinstance(event_ts, str) or not self.secret:
            return WebhookDecision(
                status_code=400,
                payload={"status": "error", "reason_code": "invalid_challenge"},
                reason_code="invalid_challenge",
            )
        return WebhookDecision(
            status_code=200,
            payload=dict(challenge_response(secret=self.secret, plain_token=plain_token, event_ts=event_ts)),
            reason_code="challenge_signed",
        )

    async def _accept(
        self, payload: dict[str, Any], *, received_at: datetime
    ) -> WebhookDecision:
        event_type = payload.get("t")
        if event_type != C2C_EVENT_TYPE:
            return WebhookDecision(
                status_code=200,
                payload={"op": 12},
                reason_code="ignored_unsupported_type",
            )
        normalized = normalize_c2c_event(payload, received_at=received_at)
        if normalized is None:
            return WebhookDecision(
                status_code=400,
                payload={"status": "error", "reason_code": "invalid_event_structure"},
                reason_code="invalid_event_structure",
            )
        if isinstance(normalized, UnsupportedEvent):
            event = NormalizedEvent(
                event_key=normalized.event_key,
                app_id=normalized.app_id,
                user_key=normalized.user_key,
                message_id=normalized.message_id,
                event_time=normalized.event_time,
                received_at=received_at,
                text="",
                payload_hash=payload_hash_of(payload.get("d") or {}),
                supported=False,
            )
            outcome = await self.queue_service.accept(event)
            if not outcome.accepted and outcome.reason_code == "storage_failure":
                return WebhookDecision(
                    status_code=503,
                    payload={"status": "error", "reason_code": "storage_failure"},
                    reason_code="storage_failure",
                )
            return WebhookDecision(
                status_code=200,
                payload={"op": 12},
                reason_code=normalized.reason_code,
                enqueue=outcome,
            )
        try:
            outcome = await self.queue_service.accept(normalized)
        except Exception:  # noqa: BLE001 - 无法持久化时不能先 ACK
            return WebhookDecision(
                status_code=503,
                payload={"status": "error", "reason_code": "persist_failed"},
                reason_code="persist_failed",
            )
        if not outcome.accepted and not outcome.duplicate and outcome.reason_code == "storage_failure":
            return WebhookDecision(
                status_code=503,
                payload={"status": "error", "reason_code": "storage_failure"},
                reason_code="storage_failure",
            )
        return WebhookDecision(
            status_code=200,
            payload={"op": 12},
            reason_code=outcome.reason_code,
            enqueue=outcome,
            event=normalized,
        )


def ignored_status() -> InboundStatus:
    return InboundStatus.IGNORED
