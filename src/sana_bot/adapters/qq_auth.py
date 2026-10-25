"""QQ 官方鉴权与 Ed25519 签名。

官方规则：凭据接口取 access_token 并按 expires_in 缓存刷新；Bot Secret 先按 32 字节
补齐/截取生成 Ed25519 seed（不得把 access_token 当 seed）；dispatch 校验
``X-Signature-Ed25519`` 与 ``X-Signature-Timestamp``（timestamp 原文 + 原始 body）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sana_bot.errors import ConfigError, ExternalServiceError

__all__ = [
    "QQCredentials",
    "QQAuthClient",
    "bot_secret_seed",
    "sign_payload",
    "verify_dispatch_signature",
    "challenge_response",
    "AccessToken",
]

TOKEN_PATH = "/app/getAppAccessToken"
#: 官方文档与既有实现出现过两个 host，均指向凭证接口；默认用 api-v2 文档中的 host
KNOWN_TOKEN_HOSTS = ("https://api.bot.qq.com", "https://bots.qq.com")
TOKEN_SAFETY_MARGIN_SECONDS = 60.0


def bot_secret_seed(secret: str) -> bytes:
    """按官方规则把 Bot Secret 处理为 32 字节种子。"""
    if not secret:
        raise ConfigError("Bot Secret 不能为空")
    raw = secret.encode("utf-8")
    if len(raw) < 32:
        return (raw * ((32 // len(raw)) + 1))[:32]
    return raw[:32]


def _private_key(secret: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bot_secret_seed(secret))


def sign_payload(secret: str, payload: bytes) -> str:
    """返回 Ed25519 签名 hex。"""
    return _private_key(secret).sign(payload).hex()


def verify_dispatch_signature(
    *,
    secret: str,
    signature_hex: str,
    timestamp: str,
    raw_body: bytes,
) -> bool:
    """校验 dispatch 回调签名；任何格式问题都视为无效。"""
    if not secret or not signature_hex or not timestamp:
        return False
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    public: Ed25519PublicKey = _private_key(secret).public_key()
    message = timestamp.encode("utf-8") + raw_body
    try:
        public.verify(signature, message)
    except InvalidSignature:
        return False
    return True


def challenge_response(*, secret: str, plain_token: str, event_ts: str) -> dict[str, str]:
    """op=13 地址验证应答：返回同一 plain_token 与拼接后的签名。"""
    signature = sign_payload(secret, (event_ts + plain_token).encode("utf-8"))
    return {"plain_token": plain_token, "signature": signature}


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: float

    def expired(self, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return moment >= self.expires_at - TOKEN_SAFETY_MARGIN_SECONDS


@dataclass
class QQCredentials:
    """凭据只保存在内存；不写日志、不入库。"""

    app_id: str | None = None
    app_secret: str | None = None
    base_url: str = "https://api.bot.qq.com"

    def require(self) -> tuple[str, str]:
        if not self.app_id or not self.app_secret:
            raise ConfigError(
                "缺少 QQ_APP_ID 或 QQ_APP_SECRET",
                reason_code="qq_credentials_missing",
            )
        return self.app_id, self.app_secret

    def redacted(self) -> dict[str, Any]:
        return {
            "app_id_present": bool(self.app_id),
            "app_secret_present": bool(self.app_secret),
            "base_url": self.base_url,
        }


@dataclass
class QQAuthClient:
    """access_token 获取与缓存；同时检查 HTTP 状态与业务 code。"""

    credentials: QQCredentials
    client: httpx.AsyncClient | None = None
    _token: AccessToken | None = field(default=None, init=False)
    _clock: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._clock = time.monotonic

    def invalidate(self) -> None:
        self._token = None

    async def access_token(self) -> str:
        if self._token is not None and not self._token.expired(now=self._clock()):
            return self._token.value
        await self._fetch()
        assert self._token is not None
        return self._token.value

    async def refresh(self) -> str:
        self.invalidate()
        return await self.access_token()

    async def _fetch(self) -> None:
        app_id, app_secret = self.credentials.require()
        payload = {"appId": app_id, "clientSecret": app_secret}
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.post(self.credentials.base_url + TOKEN_PATH, json=payload)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                f"获取 access_token 网络失败：{type(exc).__name__}",
                reason_code="qq_token_network_error",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"获取 access_token 失败：HTTP {response.status_code}",
                reason_code="qq_token_http_error",
                details={
                    "http_status": response.status_code,
                    "response_keys": _body_keys(response),
                    "url_host": _host_of(self.credentials.base_url),
                },
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "access_token 响应不是 JSON",
                reason_code="qq_token_bad_response",
                details={
                    "http_status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "url_host": _host_of(self.credentials.base_url),
                },
            ) from exc
        code = body.get("code")
        token = body.get("access_token")
        expires_in = _coerce_seconds(body.get("expires_in"))
        if code not in (0, None):
            raise ExternalServiceError(
                f"access_token 业务失败：code={code}",
                reason_code="qq_token_business_error",
                details={
                    "http_status": response.status_code,
                    "response_keys": sorted(str(key) for key in body),
                    "code": code,
                    "url_host": _host_of(self.credentials.base_url),
                },
            )
        if not isinstance(token, str) or not token.strip() or expires_in is None:
            raise ExternalServiceError(
                "access_token 响应缺少 token 或 expires_in",
                reason_code="qq_token_business_error",
                details={
                    "http_status": response.status_code,
                    "response_keys": sorted(str(key) for key in body),
                    "code": code,
                    "expires_in_type": type(body.get("expires_in")).__name__,
                    "url_host": _host_of(self.credentials.base_url),
                },
            )
        if expires_in <= 0:
            raise ExternalServiceError(
                "access_token 的 expires_in 非正数，不能安全缓存",
                reason_code="qq_token_business_error",
                details={"expires_in": expires_in},
            )
        self._token = AccessToken(
            value=token.strip(),
            expires_at=self._clock() + expires_in,
        )

    @staticmethod
    def fingerprint(token: str) -> str:
        """可用于日志的短指纹；不泄露 token 本身。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _body_keys(response: httpx.Response) -> list[str]:
    """只记录响应字段名，不记录取值，便于诊断而不泄露内容。"""
    try:
        body = response.json()
    except ValueError:
        return []
    return sorted(str(key) for key in body) if isinstance(body, dict) else []


def _host_of(base_url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(base_url).netloc or base_url
    except Exception:  # noqa: BLE001 - 诊断辅助不得抛错
        return "unknown"


def _coerce_seconds(value: Any) -> float | None:
    """官方响应可能把 expires_in 返回为字符串数字，这里统一转成秒数。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None
