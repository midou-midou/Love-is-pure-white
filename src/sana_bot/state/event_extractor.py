"""状态事件提取器：模型只提议有限标签与原文证据。

先以可注入的 LangChain 调用端口独立验证；US1 接入真实 DeepSeek 适配。
解析失败、证据不符或标签越界一律返回 no_event 并记录原因。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sana_bot.errors import ConfigError
from sana_bot.ports import (
    ModelBudget,
    ModelPort,
    ModelRequest,
    StateEventResult,
)
from sana_bot.state.event_models import (
    ApprovedEventCatalog,
    parse_proposal,
)

__all__ = ["LangChainStateEventExtractor", "build_extraction_messages", "SYSTEM_PROMPT"]

SYSTEM_PROMPT = (
    "你是状态事件标注器。只输出 JSON 对象，形如 {\"events\": [...]} 或 {\"events\": [], \"no_event_reason\": \"...\"}。\n"
    "每个事件只能包含 tag、source_message_id、evidence_start、evidence_end、quote 五个字段。\n"
    "tag 必须来自给定的允许标签列表；quote 必须是用户原文的精确子串，"
    "evidence_start / evidence_end 是该子串在用户原文中的字符下标（左闭右开）。\n"
    "不确定时返回空事件列表，不要猜测。禁止输出状态名、分数、关系、配置或任何操作指令。"
)


def build_extraction_messages(
    *,
    message: str,
    source_message_id: str,
    tags: tuple[str, ...],
    history: Sequence[Mapping[str, str]] = (),
    previous_state: str = "",
) -> tuple[Mapping[str, str], ...]:
    allowed = "、".join(tags)
    lines = [f"允许标签：{allowed}"]
    if previous_state:
        lines.append(f"上一轮有效状态：{previous_state}")
    if history:
        lines.append("最近上下文（只能引用下列消息 id）：")
        for item in history:
            lines.append(
                f"- id={item.get('message_id', '')} role={item.get('role', '')} "
                f"text={item.get('content', '')}"
            )
    lines.append(f"source_message_id：{source_message_id}")
    lines.append(f"用户原文（长度 {len(message)}）：")
    user = "\n".join(lines) + message
    return (
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    )


class LangChainStateEventExtractor:
    """实现 :class:`sana_bot.ports.StateEventExtractor`。"""

    def __init__(
        self,
        *,
        model: ModelPort,
        budget: ModelBudget | None = None,
    ) -> None:
        self.model = model
        self.budget = budget or ModelBudget(total_seconds=30.0)

    async def extract(
        self,
        *,
        message: str,
        source_message_id: str,
        catalog: ApprovedEventCatalog,
        correlation_id: str | None = None,
        history: Sequence[Mapping[str, str]] = (),
        previous_state: str = "",
    ) -> StateEventResult:
        if not isinstance(catalog, ApprovedEventCatalog) or not catalog.tags:
            raise ConfigError("事件提取需要非空的批准标签目录")
        clock = getattr(self.budget, "clock", None)
        now = clock.now() if clock is not None else _monotonic()
        self.budget.start(now)
        if self.budget.expired(now):
            return StateEventResult(
                ok=False,
                reason_code="model_budget_exhausted",
                correlation_id=correlation_id,
            )
        request = ModelRequest(
            messages=build_extraction_messages(
                message=message,
                source_message_id=source_message_id,
                tags=catalog.tags,
                history=history,
                previous_state=previous_state,
            ),
            temperature=0.0,
            json_schema_name="state_events",
            correlation_id=correlation_id,
        )
        result = await self.model.complete_json(request, self.budget)
        if not result.ok:
            return StateEventResult(
                ok=False,
                reason_code=result.reason_code or "model_failure",
                correlation_id=correlation_id,
                details={"model": result.model},
            )
        sources = {
            str(item.get("message_id") or (source_message_id if item.get("role") == "user" and item.get("content") == message else "")): str(
                item.get("content", "")
            )
            for item in history
            if item.get("message_id")
        }
        sources[source_message_id] = message
        proposal = parse_proposal(
            result.payload,
            source_message_id=source_message_id,
            source_text=message,
            catalog=catalog,
            sources=sources,
        )
        if not proposal.ok:
            return StateEventResult(
                ok=False,
                proposal=proposal,
                reason_code=proposal.reasons()[0] if proposal.reasons() else "parse_error",
                correlation_id=correlation_id,
                details={"reasons": list(proposal.reasons())},
            )
        return StateEventResult(
            ok=bool(proposal.events),
            events=proposal.events,
            proposal=proposal,
            reason_code=None if proposal.events else "no_event",
            correlation_id=correlation_id,
            details={"reason": proposal.no_event_reason} if proposal.no_event_reason else {},
        )


def _monotonic() -> float:
    import time

    return time.monotonic()
