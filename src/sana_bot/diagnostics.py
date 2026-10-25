"""诊断分类、脱敏与失败降级。

诊断正文不得包含密钥、完整 prompt 或模型推理；记录失败时必须发出备用信号，
但绝不向调用方伪报"记录成功"。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sana_bot.enums import DiagnosticCategory

__all__ = [
    "DiagnosticCategory",
    "REDACTED",
    "FORBIDDEN_KEYS",
    "redact",
    "sanitize_payload",
    "DiagnosticRecord",
    "DiagnosticOutcome",
    "DiagnosticRecorder",
    "LoggingRecorder",
    "ResilientRecorder",
    "diagnostic_logger",
]

REDACTED = "[已脱敏]"
_LOGGER_NAME = "sana_bot.diagnostics"

#: 这些键即使出现也不写入诊断，避免泄露 prompt、推理与凭据。
FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "messages",
        "reasoning",
        "reasoning_content",
        "chain_of_thought",
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "app_secret",
        "client_secret",
        "secret",
        "password",
        "raw_body",
        "raw_request",
        "raw_response",
    }
)

_SECRET_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "QQ_APP_SECRET",
    "QQ_APP_ID",
    "LANGCHAIN_API_KEY",
)

# 常见凭据样式；即使环境未注入也能拦住明显泄露
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\bQQBot\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE), f"QQBot {REDACTED}"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|app[_-]?secret)\b\s*[:=]\s*\S+"), r"\1: " + REDACTED),
    (re.compile(r"(?i)\bAuthorization\s*[:=]\s*\S+"), f"Authorization: {REDACTED}"),
)


def diagnostic_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def redact(text: str, *, secrets: Sequence[str | None] = (), max_length: int = 2000) -> str:
    """脱敏并截断；空串安全。"""
    if not text:
        return ""
    result = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            result = result.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    if len(result) > max_length:
        result = result[:max_length] + "…[截断]"
    return result


def sanitize_payload(
    payload: Mapping[str, Any] | None,
    *,
    secrets: Sequence[str | None] = (),
    depth: int = 0,
) -> dict[str, Any]:
    """递归脱敏字典；禁用键被替换为占位说明而不是原值。"""
    if not payload:
        return {}
    if depth > 4:
        return {"_": "[层级过深已截断]"}
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = str(key).lower()
        if lowered in FORBIDDEN_KEYS:
            clean[str(key)] = REDACTED
            continue
        if isinstance(value, Mapping):
            clean[str(key)] = sanitize_payload(value, secrets=secrets, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            clean[str(key)] = [
                sanitize_payload(item, secrets=secrets, depth=depth + 1)
                if isinstance(item, Mapping)
                else redact(str(item), secrets=secrets)
                for item in value[:32]
            ]
        elif isinstance(value, str):
            clean[str(key)] = redact(value, secrets=secrets)
        elif isinstance(value, (int, float, bool)) or value is None:
            clean[str(key)] = value
        else:
            clean[str(key)] = redact(repr(value), secrets=secrets)
    return clean


@dataclass(frozen=True)
class DiagnosticRecord:
    """一条待记录的诊断事件。"""

    category: DiagnosticCategory
    correlation_id: str
    user_id: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    snapshot_id: str | None = None
    reason: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: str | None = None


@dataclass(frozen=True)
class DiagnosticOutcome:
    """记录结果；``fallback_signal`` 表示已发出备用信号。"""

    recorded: bool
    fallback_signal: bool = False
    reason_code: str | None = None
    correlation_id: str | None = None
    message: str = ""


@runtime_checkable
class DiagnosticRecorder(Protocol):
    def record(self, record: DiagnosticRecord) -> DiagnosticOutcome: ...


class LoggingRecorder:
    """备用信号：写日志（已脱敏），不依赖数据库可写。"""

    def __init__(self, *, secrets: Sequence[str | None] = ()) -> None:
        self._secrets = tuple(secrets)
        self._logger = diagnostic_logger()

    def record(self, record: DiagnosticRecord) -> DiagnosticOutcome:
        self._logger.warning(
            "diagnostic_fallback category=%s correlation_id=%s user_id=%s reason=%s payload=%s",
            record.category.value,
            record.correlation_id,
            record.user_id,
            redact(record.reason, secrets=self._secrets),
            sanitize_payload(record.payload, secrets=self._secrets),
        )
        return DiagnosticOutcome(recorded=False, fallback_signal=True, correlation_id=record.correlation_id)


class ResilientRecorder:
    """先写主记录（数据库），失败时发出备用信号且如实报告未记录。"""

    def __init__(
        self,
        primary: DiagnosticRecorder,
        *,
        fallback: DiagnosticRecorder | None = None,
        secrets: Iterable[str | None] = (),
    ) -> None:
        self._primary = primary
        self._fallback = fallback or LoggingRecorder(secrets=tuple(secrets))

    def record(self, record: DiagnosticRecord) -> DiagnosticOutcome:
        try:
            outcome = self._primary.record(record)
        except Exception as exc:  # noqa: BLE001 - 记录失败不得影响用户可见回复
            degraded = self._fallback.record(record)
            return DiagnosticOutcome(
                recorded=False,
                fallback_signal=degraded.fallback_signal,
                reason_code=DiagnosticCategory.LOGGING_ERROR.value,
                correlation_id=record.correlation_id,
                message=f"诊断写入失败：{type(exc).__name__}",
            )
        if not outcome.recorded:
            degraded = self._fallback.record(record)
            return DiagnosticOutcome(
                recorded=False,
                fallback_signal=degraded.fallback_signal or outcome.fallback_signal,
                reason_code=outcome.reason_code or DiagnosticCategory.LOGGING_ERROR.value,
                correlation_id=record.correlation_id,
                message=outcome.message or "诊断未落库",
            )
        return outcome
