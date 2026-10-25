"""状态事件的模型与校验。

模型只能提议有限标签 + 原文证据；不得返回任意状态写入、计分或配置指令。
证据必须能在本轮用户原文中回定位，标签必须在已批准目录内。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.errors import ConfigError

__all__ = [
    "StateEvent",
    "StateEventProposal",
    "ApprovedEventCatalog",
    "FORBIDDEN_EVENT_FIELDS",
    "parse_proposal",
]

#: 模型不得输出的越权字段（出现即拒绝整个提案）
FORBIDDEN_EVENT_FIELDS = frozenset(
    {
        "to_state",
        "state",
        "score",
        "affection",
        "affection_delta",
        "relationship",
        "romance_status",
        "config",
        "config_update",
        "command",
        "action",
        "tool",
        "execute",
        "set_mood",
    }
)


@dataclass(frozen=True)
class StateEvent:
    """一个带原文证据的有限标签事件。"""

    tag: str
    source_message_id: str
    evidence_start: int
    evidence_end: int
    quote: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "source_message_id": self.source_message_id,
            "evidence_start": self.evidence_start,
            "evidence_end": self.evidence_end,
            "quote": self.quote,
        }


@dataclass(frozen=True)
class ApprovedEventCatalog:
    """已批准的事件标签枚举；模型只能在此集合内提议。"""

    version: str
    tags: tuple[str, ...]
    description: Mapping[str, str] = field(default_factory=dict)

    def contains(self, tag: str) -> bool:
        return tag in self.tags

    def to_payload(self) -> dict[str, Any]:
        return {"version": self.version, "tags": list(self.tags), "description": dict(self.description)}


@dataclass(frozen=True)
class StateEventProposal:
    """一次提取的结果；区分 no_event / parse_error / conflict。"""

    events: tuple[StateEvent, ...] = ()
    unknown_tags: tuple[str, ...] = ()
    forbidden_fields: tuple[str, ...] = ()
    no_event_reason: str | None = None
    parse_error: str | None = None
    conflict: str | None = None

    @property
    def ok(self) -> bool:
        return not self.unknown_tags and not self.forbidden_fields and not self.parse_error

    def reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.unknown_tags:
            reasons.append("unknown_tag")
        if self.forbidden_fields:
            reasons.append("forbidden_field")
        if self.parse_error:
            reasons.append("parse_error")
        if self.conflict:
            reasons.append("conflict")
        if self.no_event_reason:
            reasons.append(self.no_event_reason)
        return tuple(reasons)


def _is_forbidden_field(key: str) -> bool:
    """越权字段判定：精确命中或带有越权前缀（例如 score_delta）。"""
    lowered = key.strip().lower()
    if lowered in FORBIDDEN_EVENT_FIELDS:
        return True
    return lowered.startswith(
        ("score", "affection", "relationship", "romance", "to_state", "config", "command", "set_", "execute")
    )


def parse_proposal(
    payload: Mapping[str, Any] | None,
    *,
    source_message_id: str,
    source_text: str,
    catalog: ApprovedEventCatalog,
    sources: Mapping[str, str] | None = None,
) -> StateEventProposal:
    """把模型的 JSON 结果转成受约束的事件提案；任何越权或证据不符都拒绝。

    ``sources`` 给出本会话内可引用的历史消息原文；证据必须来自其中一条，
    模型不能凭空引用其它会话或用户的内容。
    """
    if payload is None:
        return StateEventProposal(parse_error="empty_payload")
    raw_events = payload.get("events")
    if raw_events is None:
        reason = str(payload.get("no_event_reason") or "no_event")
        return StateEventProposal(no_event_reason=reason)
    if isinstance(raw_events, (str, bytes)) or not isinstance(raw_events, Sequence):
        return StateEventProposal(parse_error="events_not_a_list")

    forbidden: list[str] = []
    unknown: list[str] = []
    events: list[StateEvent] = []
    for item in raw_events:
        if not isinstance(item, Mapping):
            return StateEventProposal(parse_error="event_not_an_object")
        forbidden.extend(sorted(key for key in item if _is_forbidden_field(str(key))))
        tag = item.get("tag")
        if not isinstance(tag, str) or not tag:
            return StateEventProposal(parse_error="missing_tag")
        if not catalog.contains(tag):
            unknown.append(tag)
            continue
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote:
            return StateEventProposal(parse_error="missing_quote")
        start = item.get("evidence_start")
        end = item.get("evidence_end")
        if not isinstance(start, int) or not isinstance(end, int):
            return StateEventProposal(parse_error="missing_evidence_range")
        message_id = str(item.get("source_message_id") or source_message_id)
        if sources is not None:
            if message_id not in sources:
                # 证据必须来自当前会话可见的消息，不能跨用户或伪造历史
                return StateEventProposal(parse_error="evidence_source_unknown")
            evidence_text = sources[message_id]
        else:
            evidence_text = source_text
        if start < 0 or end > len(evidence_text) or end <= start:
            return StateEventProposal(parse_error="evidence_out_of_range")
        if evidence_text[start:end] != quote:
            return StateEventProposal(parse_error="evidence_quote_mismatch")
        events.append(
            StateEvent(
                tag=tag,
                source_message_id=message_id,
                evidence_start=start,
                evidence_end=end,
                quote=quote,
            )
        )
    if forbidden:
        return StateEventProposal(forbidden_fields=tuple(sorted(set(forbidden))))
    if unknown:
        return StateEventProposal(unknown_tags=tuple(sorted(set(unknown))))
    if not events:
        return StateEventProposal(no_event_reason="no_event")
    return StateEventProposal(events=tuple(events))


def validate_catalog(tags: Sequence[str]) -> ApprovedEventCatalog:
    """构造标签目录；空标签目录不合法（无法表达任何事件）。"""
    cleaned = tuple(dict.fromkeys(tag.strip() for tag in tags if tag and tag.strip()))
    if not cleaned:
        raise ConfigError("事件标签目录不能为空")
    return ApprovedEventCatalog(version="events-v1", tags=cleaned)
