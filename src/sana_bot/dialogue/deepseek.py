"""LangChain + DeepSeek 适配：异步生成、结构化 JSON 校验与共享模型预算。

只取回复 content：不输出推理内容、工具结果或流式草稿；所有事件提取/生成/审核/改写
共用同一模型预算，超预算走合规失败路径。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from sana_bot.errors import ConfigError
from sana_bot.ports import (
    ModelBudget,
    ModelJsonResult,
    ModelRequest,
    ModelTextResult,
)

__all__ = ["ChatDeepSeekModel", "extract_json_object", "UNSUPPORTED_PARAM_MARKERS"]


#: 厂商拒绝 response_format 时的典型错误标记
UNSUPPORTED_PARAM_MARKERS = (
    "response_format",
    "unexpected keyword",
    "extra_forbidden",
    "unrecognized request argument",
    "does not support",
)


def _looks_like_unsupported_response_format(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in UNSUPPORTED_PARAM_MARKERS)


def extract_json_object(text: str) -> Mapping[str, Any] | None:
    """从模型输出中取出唯一 JSON 对象；夹杂说明文字时也能解析，其它情况返回 None。"""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, Mapping) else None


class ChatDeepSeekModel:
    """实现 :class:`sana_bot.ports.ModelPort`。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        total_budget_seconds: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_name = model
        self.base_url = base_url
        self.budget = ModelBudget(total_seconds=total_budget_seconds)
        self._client = client
        #: 记录本次结构化调用实际使用的 JSON 模式，便于诊断不必猜测
        self.last_json_mode = "json_object"

    # ------------------------------------------------------------------ 生命周期

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ConfigError(
                "缺少 DEEPSEEK_API_KEY，不能调用对话模型",
                reason_code="deepseek_credentials_missing",
            )
        from langchain_deepseek import ChatDeepSeek

        self._client = ChatDeepSeek(
            model=self.model_name,
            api_key=self.api_key,
            api_base=self.base_url,
            temperature=0.8,
        )
        return self._client

    # ------------------------------------------------------------------ 调用

    async def complete_text(self, request: ModelRequest, budget: ModelBudget) -> ModelTextResult:
        return await self._invoke(request, budget, as_json=False)  # type: ignore[return-value]

    async def complete_json(self, request: ModelRequest, budget: ModelBudget) -> ModelJsonResult:
        return await self._invoke(request, budget, as_json=True)  # type: ignore[return-value]

    async def _invoke(
        self, request: ModelRequest, budget: ModelBudget, *, as_json: bool
    ) -> ModelTextResult | ModelJsonResult:
        import time as _time

        now = _time.monotonic()
        budget.start(now)
        if budget.expired(now):
            return self._failure("model_budget_exhausted", as_json=as_json)
        timeout = budget.timeout_for(now)
        try:
            client = self._ensure_client()
        except ConfigError as exc:
            return self._failure(exc.reason_code, as_json=as_json, message=exc.message)

        messages = [dict(item) for item in request.messages]
        kwargs: dict[str, Any] = {}
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if as_json:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await asyncio.wait_for(
                client.ainvoke(messages, **kwargs), timeout=max(0.001, timeout)
            )
        except asyncio.TimeoutError:
            return self._failure("model_budget_exhausted", as_json=as_json, message="timeout")
        except Exception as exc:  # noqa: BLE001 - 外部失败必须显式分类
            if as_json and _looks_like_unsupported_response_format(exc):
                # 厂商不支持 json_object 时回退为纯提示词，仍由本地解析与校验把关
                kwargs.pop("response_format", None)
                try:
                    response = await asyncio.wait_for(
                        client.ainvoke(messages, **kwargs), timeout=max(0.001, timeout)
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    return self._failure(
                        "model_call_failed", as_json=as_json, message=type(retry_exc).__name__
                    )
                self.last_json_mode = "prompt_only_fallback"
            else:
                return self._failure(
                    "model_call_failed", as_json=as_json, message=f"{type(exc).__name__}"
                )
        content = getattr(response, "content", "")
        if isinstance(content, list):  # 兼容多段 content
            content = "".join(str(part) for part in content if isinstance(part, str))
        text = str(content or "")
        if not text.strip():
            return self._failure("model_empty_content", as_json=as_json)
        if not as_json:
            return ModelTextResult(
                ok=True,
                text=text.strip(),
                model=self.model_name,
                correlation_id=request.correlation_id,
            )
        payload = extract_json_object(text)
        if payload is None:
            return ModelJsonResult(
                ok=False,
                raw_text=text[:500],
                model=self.model_name,
                reason_code="model_json_invalid",
                correlation_id=request.correlation_id,
            )
        return ModelJsonResult(
            ok=True,
            payload=payload,
            raw_text=text[:500],
            model=self.model_name,
            correlation_id=request.correlation_id,
        )

    def _failure(
        self,
        reason_code: str,
        *,
        as_json: bool,
        message: str = "",
    ) -> ModelTextResult | ModelJsonResult:
        if as_json:
            return ModelJsonResult(ok=False, reason_code=reason_code, model=self.model_name, details={"message": message})
        return ModelTextResult(ok=False, reason_code=reason_code, model=self.model_name, details={"message": message})
