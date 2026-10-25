"""离线替代聊天模型：只在显式 ``offline`` 评估中使用。

它不联网、不调用真实模型，也不参与生产回复；产物必须标注 execution_mode=offline，
不能作为生产启用证据。用固定规则保证同一输入得到同一输出，便于逐轮断言。
"""

from __future__ import annotations

from collections.abc import Mapping

from sana_bot.ports import ModelBudget, ModelJsonResult, ModelRequest, ModelTextResult

__all__ = ["SubstituteChatModel"]


class SubstituteChatModel:
    """实现 :class:`sana_bot.ports.ModelPort` 的确定性替身。"""

    model_name = "substitute-offline"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_text(self, request: ModelRequest, budget: ModelBudget) -> ModelTextResult:
        self.requests.append(request)
        message = _last_user_message(request.messages)
        topic = _topic_of(message)
        text = f"嗯，我在听{topic}，你慢慢说"
        return ModelTextResult(
            ok=True, text=text, model=self.model_name, correlation_id=request.correlation_id
        )

    async def complete_json(self, request: ModelRequest, budget: ModelBudget) -> ModelJsonResult:
        self.requests.append(request)
        if request.json_schema_name == "output_review":
            payload: Mapping[str, object] = {"approved": True, "violations": []}
        else:
            payload = {"events": [], "no_event_reason": "substitute_offline"}
        return ModelJsonResult(
            ok=True,
            payload=payload,
            model=self.model_name,
            correlation_id=request.correlation_id,
        )


def _last_user_message(messages) -> str:
    for item in reversed(tuple(messages)):
        if str(item.get("role")) == "user":
            return str(item.get("content", ""))
    return ""


def _topic_of(message: str) -> str:
    for needle, topic in (
        ("社团", "社团的事"),
        ("猫", "那只猫"),
        ("吃醋", "这点事"),
        ("点心", "点心的事"),
        ("礼物", "礼物"),
        ("累", "今天的事"),
    ):
        if needle in message:
            return topic
    return ""
