"""状态事件提取测试：有限标签、原文证据一致、越权字段与失败路径。

使用 fake 结构化模型端口，不依赖真实 DeepSeek。
"""

from __future__ import annotations

import pytest

from sana_bot.ports import ModelBudget
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.event_models import (
    ApprovedEventCatalog,
    parse_proposal,
    validate_catalog,
)
from sana_bot.state.models import normalize_mood
from tests.support import FakeModel

CATALOG = ApprovedEventCatalog(version="events-v1", tags=("direct_praise", "teasing", "steady_daily"))
MESSAGE = "你怎么突然夸我，我才没有很开心"
SOURCE_ID = "m-1"


def _event(tag: str, quote: str, start: int) -> dict[str, object]:
    return {
        "tag": tag,
        "source_message_id": SOURCE_ID,
        "evidence_start": start,
        "evidence_end": start + len(quote),
        "quote": quote,
    }


def test_catalog_rejects_empty_tags() -> None:
    from sana_bot.errors import ConfigError

    with pytest.raises(ConfigError):
        validate_catalog([])


def test_valid_event_is_accepted() -> None:
    payload = {"events": [_event("direct_praise", "突然夸我", MESSAGE.index("突然夸我"))]}
    proposal = parse_proposal(
        payload, source_message_id=SOURCE_ID, source_text=MESSAGE, catalog=CATALOG
    )
    assert proposal.ok
    assert proposal.events[0].tag == "direct_praise"
    assert proposal.events[0].quote == "突然夸我"


def test_unknown_tag_is_rejected() -> None:
    payload = {"events": [_event("set_affection", "突然夸我", MESSAGE.index("突然夸我"))]}
    proposal = parse_proposal(
        payload, source_message_id=SOURCE_ID, source_text=MESSAGE, catalog=CATALOG
    )
    assert not proposal.ok
    assert proposal.unknown_tags == ("set_affection",)
    assert "unknown_tag" in proposal.reasons()


def test_forbidden_field_is_rejected() -> None:
    item = _event("teasing", "突然夸我", MESSAGE.index("突然夸我"))
    item["score_delta"] = 5
    item["state"] = "happy"
    proposal = parse_proposal(
        {"events": [item]}, source_message_id=SOURCE_ID, source_text=MESSAGE, catalog=CATALOG
    )
    assert not proposal.ok
    assert "score_delta" in proposal.forbidden_fields
    assert "state" in proposal.forbidden_fields


@pytest.mark.parametrize(
    "quote,start,expected",
    [
        ("不存在的原文", 0, "evidence_quote_mismatch"),
        ("突然夸我", 999, "evidence_out_of_range"),
        ("突然夸我", 6, "evidence_quote_mismatch"),
    ],
)
def test_evidence_must_match_source(quote: str, start: int, expected: str) -> None:
    payload = {"events": [_event("direct_praise", quote, start)]}
    proposal = parse_proposal(
        payload, source_message_id=SOURCE_ID, source_text=MESSAGE, catalog=CATALOG
    )
    assert not proposal.ok
    assert proposal.parse_error == expected


def test_malformed_payload_reports_parse_error() -> None:
    assert parse_proposal(
        {"events": "not-a-list"},
        source_message_id=SOURCE_ID,
        source_text=MESSAGE,
        catalog=CATALOG,
    ).parse_error == "events_not_a_list"
    assert parse_proposal(
        {"events": [{"tag": "teasing"}]},
        source_message_id=SOURCE_ID,
        source_text=MESSAGE,
        catalog=CATALOG,
    ).parse_error == "missing_quote"
    assert parse_proposal(
        {"events": []}, source_message_id=SOURCE_ID, source_text=MESSAGE, catalog=CATALOG
    ).no_event_reason == "no_event"
    assert parse_proposal(
        None, source_message_id=SOURCE_ID, source_text=MESSAGE, catalog=CATALOG
    ).parse_error == "empty_payload"


# --------------------------------------------------------------------------- 提取器


async def test_extractor_returns_events_from_model() -> None:
    model = FakeModel()
    model.queue_json({"events": [_event("direct_praise", "突然夸我", MESSAGE.index("突然夸我"))]})
    extractor = LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0))
    result = await extractor.extract(
        message=MESSAGE,
        source_message_id=SOURCE_ID,
        catalog=CATALOG,
        correlation_id="c-1",
    )
    assert result.ok
    assert result.events[0].tag == "direct_praise"
    assert result.correlation_id == "c-1"
    prompt = model.requests[0].messages[1]["content"]
    assert "direct_praise" in prompt
    assert "禁止输出状态名" in model.requests[0].messages[0]["content"]


async def test_extractor_no_event_is_not_an_error() -> None:
    model = FakeModel()
    model.queue_json({"events": [], "no_event_reason": "no_evidence"})
    extractor = LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0))
    result = await extractor.extract(message=MESSAGE, source_message_id=SOURCE_ID, catalog=CATALOG)
    assert not result.ok
    assert result.reason_code == "no_event"
    assert result.events == ()


async def test_extractor_reports_unknown_tag_without_applying() -> None:
    model = FakeModel()
    model.queue_json({"events": [_event("relationship_up", "突然夸我", MESSAGE.index("突然夸我"))]})
    extractor = LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0))
    result = await extractor.extract(message=MESSAGE, source_message_id=SOURCE_ID, catalog=CATALOG)
    assert not result.ok
    assert result.reason_code == "unknown_tag"
    assert result.events == ()


async def test_extractor_reports_model_failure() -> None:
    model = FakeModel()
    model.queue_failure(json=True, reason="model_timeout")
    extractor = LangChainStateEventExtractor(model=model, budget=ModelBudget(30.0))
    result = await extractor.extract(message=MESSAGE, source_message_id=SOURCE_ID, catalog=CATALOG)
    assert not result.ok
    assert result.reason_code == "model_timeout"


async def test_extractor_respects_shared_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel()
    budget = ModelBudget(total_seconds=0.0)
    extractor = LangChainStateEventExtractor(model=model, budget=budget)
    result = await extractor.extract(message=MESSAGE, source_message_id=SOURCE_ID, catalog=CATALOG)
    assert not result.ok
    assert result.reason_code == "model_budget_exhausted"
    assert model.requests == []


def test_mood_aliases_are_normalized() -> None:
    from sana_bot.enums import PrimaryMood

    assert normalize_mood("高兴") is PrimaryMood.HAPPY
    assert normalize_mood("开心") is PrimaryMood.HAPPY
    assert normalize_mood("high") is PrimaryMood.HAPPY
    assert normalize_mood("难过") is PrimaryMood.SAD
    assert normalize_mood("平静") is PrimaryMood.NEUTRAL
    with pytest.raises(ValueError):
        normalize_mood("狂喜")
