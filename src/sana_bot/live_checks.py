"""真实服务 smoke 检查：DeepSeek 与 QQ 凭据。

只做小规模、可复现的连通性验证；输出中不含密钥、access token 或模型推理内容，
未取得条件的检查明确标记为「未执行」而不是通过。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sana_bot.config import Environment
from sana_bot.dialogue.deepseek import ChatDeepSeekModel
from sana_bot.errors import ConfigError, ExternalServiceError
from sana_bot.ports import ModelBudget, ModelRequest

__all__ = [
    "LiveCheck",
    "model_smoke",
    "qq_auth_smoke",
    "qq_gateway_smoke",
    "SMOKE_PROMPT",
    "SMOKE_JSON_PROMPT",
]

SMOKE_PROMPT = "用一句不超过十五字的短句，以乾纱凪的口吻回应「今天社团有点忙」"
SMOKE_JSON_PROMPT = (
    '只输出 JSON 对象：{"mood": "neutral", "reason": "简短理由"}。不要输出其它文字。'
)

#: smoke 使用的模型处理预算；与运行期共享预算的量级一致
SMOKE_BUDGET_SECONDS = 30.0


@dataclass(frozen=True)
class LiveCheck:
    """一次真实检查的结果；``executed`` 为假时表示缺少条件、未验证。"""

    name: str
    executed: bool
    ok: bool | None = None
    reason_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "executed": self.executed,
            "ok": self.ok,
            "details": dict(self.details),
        }
        if self.reason_code:
            payload["reason_code"] = self.reason_code
        return payload


def _host_of(url: str) -> str:
    """诊断只保留 host，避免把完整地址写进报告。"""
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url
    except Exception:  # noqa: BLE001
        return "unknown"


def _client(environment: Environment) -> ChatDeepSeekModel:
    return ChatDeepSeekModel(
        api_key=environment.deepseek_api_key,
        model=environment.deepseek_model,
        base_url=environment.deepseek_base_url,
        total_budget_seconds=SMOKE_BUDGET_SECONDS,
    )


async def qq_gateway_smoke(
    environment: Environment,
    *,
    client: Any | None = None,
    http_client: Any | None = None,
) -> LiveCheck:
    """真实网关检查：取 access_token 后调用 ``GET /gateway``。

    只验证「能拿到 WSS 接入点」，不建立长连接（长连接由服务进程负责），
    因此不会消耗事件配额，也不会因 intents 权限问题误报。
    """
    from sana_bot.adapters.qq_auth import QQAuthClient, QQCredentials
    from sana_bot.adapters.qq_gateway import fetch_gateway_url

    missing = [
        name
        for name, present in (
            ("QQ_APP_ID", environment.qq_app_id),
            ("QQ_APP_SECRET", environment.qq_app_secret),
        )
        if not present
    ]
    if missing:
        return LiveCheck(
            name="qq_gateway_smoke",
            executed=False,
            reason_code="qq_credentials_missing",
            details={"missing": missing, "note": "缺少凭据，检查未执行"},
        )
    auth = QQAuthClient(
        credentials=QQCredentials(
            app_id=environment.qq_app_id,
            app_secret=environment.qq_app_secret,
            base_url=environment.qq_api_base_url,
        ),
        client=client,
    )
    details: dict[str, Any] = {
        "transport": environment.qq_transport,
        "intents": environment.qq_intents,
        "shard": list(environment.qq_shard),
    }
    started = time.perf_counter()
    try:
        token = await auth.access_token()
        url = await fetch_gateway_url(
            base_url=environment.qq_api_base_url,
            access_token=token,
            client=http_client,
        )
    except (ExternalServiceError, ConfigError) as exc:
        return LiveCheck(
            name="qq_gateway_smoke",
            executed=True,
            ok=False,
            reason_code=exc.reason_code,
            details={**details, "message": exc.message[:160], **(exc.details or {})},
        )
    details["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    details["url_host"] = _host_of(url)
    return LiveCheck(name="qq_gateway_smoke", executed=True, ok=True, details=details)


async def model_smoke(environment: Environment, *, client: Any | None = None) -> LiveCheck:
    """真实 DeepSeek 小规模 smoke：文本生成 + 结构化 JSON 解析。"""
    if not environment.deepseek_api_key:
        return LiveCheck(
            name="deepseek_smoke",
            executed=False,
            reason_code="deepseek_credentials_missing",
            details={"note": "未注入 DEEPSEEK_API_KEY，检查未执行"},
        )
    model = client or _client(environment)
    budget = ModelBudget(total_seconds=SMOKE_BUDGET_SECONDS)
    details: dict[str, Any] = {
        "model": environment.deepseek_model,
        "base_url": environment.deepseek_base_url,
    }

    started = time.perf_counter()
    try:
        text_result = await model.complete_text(
            ModelRequest(
                messages=(
                    {"role": "system", "content": "你是乾纱凪，只用第一人称短句回答。"},
                    {"role": "user", "content": SMOKE_PROMPT},
                ),
                temperature=0.8,
                correlation_id="doctor-live-model-text",
            ),
            budget,
        )
    except Exception as exc:  # noqa: BLE001 - 外部失败必须显式分类
        return LiveCheck(
            name="deepseek_smoke",
            executed=True,
            ok=False,
            reason_code="model_call_failed",
            details={**details, "error": type(exc).__name__},
        )
    details["text_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    details["text_ok"] = text_result.ok
    details["text_char_count"] = len(text_result.text)
    details["text_ends_with_full_stop"] = text_result.text.rstrip().endswith("。")
    details["reported_model"] = text_result.model
    if not text_result.ok:
        return LiveCheck(
            name="deepseek_smoke",
            executed=True,
            ok=False,
            reason_code=text_result.reason_code or "model_call_failed",
            details=details,
        )

    started = time.perf_counter()
    json_result = await model.complete_json(
        ModelRequest(
            messages=(
                {"role": "system", "content": SMOKE_JSON_PROMPT},
                {"role": "user", "content": "今天社团忙完了，心情还平稳。"},
            ),
            temperature=0.0,
            json_schema_name="doctor_probe",
            correlation_id="doctor-live-model-json",
        ),
        budget,
    )
    details["json_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    details["json_ok"] = json_result.ok
    details["json_keys"] = sorted(str(key) for key in (json_result.payload or {}))
    details["json_reason_code"] = json_result.reason_code
    details["not_executed"] = ["真实人物风格评分", "多轮上下文质量判定"]
    ok = bool(json_result.ok)
    return LiveCheck(
        name="deepseek_smoke",
        executed=True,
        ok=ok,
        reason_code=None if ok else (json_result.reason_code or "model_json_invalid"),
        details=details,
    )


async def qq_auth_smoke(
    environment: Environment,
    *,
    client: Any | None = None,
) -> LiveCheck:
    """真实 QQ 凭据检查：按官方接口取 access_token 并核对缓存刷新依据。"""
    from sana_bot.adapters.qq_auth import QQAuthClient, QQCredentials

    missing = [
        name
        for name, present in (
            ("QQ_APP_ID", environment.qq_app_id),
            ("QQ_APP_SECRET", environment.qq_app_secret),
        )
        if not present
    ]
    if missing:
        return LiveCheck(
            name="qq_auth_smoke",
            executed=False,
            reason_code="qq_credentials_missing",
            details={"missing": missing, "note": "缺少凭据，检查未执行"},
        )
    client_instance = QQAuthClient(
        credentials=QQCredentials(
            app_id=environment.qq_app_id,
            app_secret=environment.qq_app_secret,
            base_url=environment.qq_api_base_url,
        ),
        client=client,
    )
    details: dict[str, Any] = {
        "base_url": environment.qq_api_base_url,
        "app_id_present": True,
        "app_secret_present": True,
    }
    started = time.perf_counter()
    try:
        token = await client_instance.access_token()
    except ExternalServiceError as exc:
        return LiveCheck(
            name="qq_auth_smoke",
            executed=True,
            ok=False,
            reason_code=exc.reason_code,
            details={**details, "http_or_business": exc.details, "message": exc.message[:160]},
        )
    except ConfigError as exc:
        return LiveCheck(
            name="qq_auth_smoke",
            executed=True,
            ok=False,
            reason_code=exc.reason_code,
            details={**details, "message": exc.message[:160]},
        )
    details["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    # 只记录不可逆短指纹，便于多次运行比对，不泄露 token 本身
    details["token_fingerprint"] = client_instance.fingerprint(token)
    token_state = client_instance._token
    details["expires_in_recorded"] = bool(token_state and token_state.expires_at > 0)
    details["not_executed"] = [
        "沙箱回调验签（需 HTTPS 回调地址）",
        "C2C 被动回复发送（需沙箱用户与用户 openid）",
        "权限/拒收/限流实测",
    ]
    return LiveCheck(name="qq_auth_smoke", executed=True, ok=True, details=details)
