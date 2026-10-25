"""DeepSeek 适配的失败分类与共享预算行为。

这些分支此前没有测试，导致预算耗尽时会抛 ``TypeError`` 而不是走合规失败路径。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from sana_bot.dialogue.deepseek import ChatDeepSeekModel, extract_json_object
from sana_bot.ports import ModelBudget, ModelRequest

REQUEST = ModelRequest(
    messages=({"role": "user", "content": "社团的小猫怎么样"},),
    temperature=0.8,
    correlation_id="unit-test",
)


class FakeClient:
    """记录调用并按脚本返回；可指定抛错或延迟。"""

    def __init__(self, *, content: str = "嗯", raises: BaseException | None = None, delay: float = 0.0) -> None:
        self.content = content
        self.raises = raises
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(content=self.content)


def _model(**kwargs: Any) -> ChatDeepSeekModel:
    return ChatDeepSeekModel(api_key="test-key", model="deepseek-test", **kwargs)


async def test_exhausted_budget_returns_failure_not_exception() -> None:
    client = FakeClient()
    model = _model(client=client)
    budget = ModelBudget(total_seconds=0.0)

    result = await model.complete_text(REQUEST, budget)

    assert result.ok is False
    assert result.reason_code == "model_budget_exhausted"
    assert client.calls == [], "预算耗尽不得发起调用"


async def test_exhausted_budget_returns_failure_for_json_too() -> None:
    model = _model(client=FakeClient())
    result = await model.complete_json(REQUEST, ModelBudget(total_seconds=0.0))
    assert result.ok is False
    assert result.reason_code == "model_budget_exhausted"


async def test_timeout_is_classified_as_budget_exhausted() -> None:
    model = _model(client=FakeClient(delay=0.5))
    result = await model.complete_text(REQUEST, ModelBudget(total_seconds=0.01))
    assert result.ok is False
    assert result.reason_code == "model_budget_exhausted"


async def test_successful_text_and_json_calls() -> None:
    model = _model(client=FakeClient(content="  今天挺精神的  "))
    budget = ModelBudget(total_seconds=5.0)
    text = await model.complete_text(REQUEST, budget)
    assert text.ok is True and text.text == "今天挺精神的"

    model_json = _model(client=FakeClient(content='说明如下：{"mood":"happy"}'))
    payload = await model_json.complete_json(REQUEST, ModelBudget(total_seconds=5.0))
    assert payload.ok is True
    assert payload.payload == {"mood": "happy"}


async def test_empty_and_invalid_content_are_classified() -> None:
    empty = await _model(client=FakeClient(content="   ")).complete_text(
        REQUEST, ModelBudget(total_seconds=5.0)
    )
    assert empty.ok is False and empty.reason_code == "model_empty_content"

    invalid = await _model(client=FakeClient(content="没有 JSON")).complete_json(
        REQUEST, ModelBudget(total_seconds=5.0)
    )
    assert invalid.ok is False and invalid.reason_code == "model_json_invalid"


async def test_call_failure_is_classified_and_reported() -> None:
    model = _model(client=FakeClient(raises=RuntimeError("boom")))
    result = await model.complete_text(REQUEST, ModelBudget(total_seconds=5.0))
    assert result.ok is False
    assert result.reason_code == "model_call_failed"
    assert result.details["message"] == "RuntimeError"


async def test_unsupported_response_format_falls_back_to_prompt_only() -> None:
    class FallbackClient(FakeClient):
        async def ainvoke(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
            self.calls.append({"messages": messages, **kwargs})
            if "response_format" in kwargs:
                raise ValueError("unrecognized request argument: response_format")
            return SimpleNamespace(content='{"mood":"happy"}')

    client = FallbackClient()
    model = _model(client=client)
    result = await model.complete_json(REQUEST, ModelBudget(total_seconds=5.0))

    assert result.ok is True
    assert model.last_json_mode == "prompt_only_fallback"
    assert len(client.calls) == 2


def test_budget_reset_restarts_accumulation() -> None:
    budget = ModelBudget(total_seconds=30.0)
    budget.start(1000.0)
    assert budget.remaining(1010.0) == pytest.approx(20.0)
    budget.reset()
    assert budget.remaining(1010.0) == pytest.approx(30.0), "复位后按新一轮重新计时"
    budget.start(1010.0)
    assert budget.remaining(1015.0) == pytest.approx(25.0)


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a":1}', {"a": 1}),
        ('前言 {"a":1} 后记', {"a": 1}),
        ("不是对象", None),
        ("", None),
        ("[1,2]", None),
    ],
)
def test_extract_json_object(text: str, expected: Any) -> None:
    assert extract_json_object(text) == expected
