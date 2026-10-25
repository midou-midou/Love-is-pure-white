"""T033：六状态映射、吃醋三级、恢复出口与保守归约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.state.catalog import RecoveryRule, RuleCatalog, catalog_from_package
from sana_bot.state.event_models import StateEvent
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer

CONFIG = Path(__file__).resolve().parents[2] / "config" / "character-package.json"


@pytest.fixture(scope="module")
def catalog() -> RuleCatalog:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))["state_catalog"]
    return catalog_from_package(payload, version="catalog-v2")


@pytest.fixture(scope="module")
def reducer(catalog: RuleCatalog) -> DeterministicStateReducer:
    return DeterministicStateReducer(catalog)


def event(tag: str, *, quote: str | None = None, message_id: str = "m1") -> StateEvent:
    text = quote or tag
    return StateEvent(
        tag=tag,
        source_message_id=message_id,
        evidence_start=0,
        evidence_end=len(text),
        quote=text,
    )


_MOOD_FOR_STATE = {
    "平稳常态": PrimaryMood.NEUTRAL,
    "害羞局促": PrimaryMood.HAPPY,
    "傲娇炸毛": PrimaryMood.ANGRY,
    "轻度吃醋": PrimaryMood.ANGRY,
    "中度吃醋": PrimaryMood.ANGRY,
    "重度吃醋": PrimaryMood.SAD,
    "委屈脆弱": PrimaryMood.SAD,
    "依赖软萌": PrimaryMood.HAPPY,
}


def snapshot(state: str = "平稳常态", **kwargs):
    kwargs.setdefault("mood", _MOOD_FOR_STATE.get(state, PrimaryMood.NEUTRAL))
    return empty_snapshot(
        snapshot_id="s",
        turn_id="t",
        conversation_id="c",
        dominant_state=state,
        relationship=kwargs.pop("relationship", RelationshipStatus.ROMANTIC),
        scene=kwargs.pop("scene", Scene.PRIVATE),
        **kwargs,
    )


def test_catalog_maps_four_moods_and_eight_states(catalog):
    assert {rule.to_primary for rule in catalog.rules} <= {
        PrimaryMood.NEUTRAL,
        PrimaryMood.HAPPY,
        PrimaryMood.ANGRY,
        PrimaryMood.SAD,
    }
    labels = {rule.label for rule in catalog.rules}
    assert labels == {
        "平稳常态",
        "害羞局促",
        "傲娇炸毛",
        "轻度吃醋",
        "中度吃醋",
        "重度吃醋",
        "委屈脆弱",
        "依赖软萌",
    }
    jealousy = [rule for rule in catalog.rules if "吃醋" in rule.label]
    assert {rule.card_id if hasattr(rule, "card_id") else rule.source_node_ids[0] for rule in jealousy} == {
        "sana_emotion_jealous"
    }
    assert all(rule.approved for rule in catalog.rules)


def test_direct_praise_moves_to_shy(reducer):
    result = reducer.reduce(
        current=snapshot(),
        events=[event("direct_praise", quote="你今天真好看")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.primary_mood is PrimaryMood.HAPPY
    assert "害羞局促" in result.secondary_tags
    assert result.dominant_state == "害羞局促"


def test_light_jealousy_escalates_to_medium_only_from_light(reducer):
    from_light = reducer.reduce(
        current=snapshot("轻度吃醋"),
        events=[event("user_with_other")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert from_light.dominant_state == "中度吃醋"
    from_calm = reducer.reduce(
        current=snapshot("平稳常态"),
        events=[event("user_with_other")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert from_calm.dominant_state == "轻度吃醋"


def test_heavy_jealousy_needs_cold_treatment_and_becomes_quiet(reducer):
    result = reducer.reduce(
        current=snapshot("中度吃醋"),
        events=[event("cold_treatment")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.dominant_state == "重度吃醋"
    assert result.primary_mood is PrimaryMood.SAD
    assert "自卑失落" in result.secondary_tags


def test_real_hurt_does_not_escalate_to_sharp_anger(reducer):
    result = reducer.reduce(
        current=snapshot("委屈脆弱"),
        events=[event("user_with_other")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.UNKNOWN,
    )
    # 委屈状态下不因“吃醋”关键词重新升级为争吵
    assert result.dominant_state == "委屈脆弱"
    assert result.primary_mood is PrimaryMood.SAD


def test_private_only_state_not_applied_in_unknown_scene(reducer):
    result = reducer.reduce(
        current=snapshot("平稳常态", scene=Scene.UNKNOWN),
        events=[event("misunderstanding")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.UNKNOWN,
    )
    assert result.dominant_state != "委屈脆弱"


def test_comfort_recovers_from_light_jealousy(reducer):
    result = reducer.reduce(
        current=snapshot("轻度吃醋"),
        events=[event("being_comforted", quote="我在这里听着")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.dominant_state == "平稳常态"
    assert result.reason_code == "recovery_applied"
    assert result.transition_evidence[0]["previous_state"] == "轻度吃醋"


def test_relief_moves_from_hurt_to_secure(reducer):
    result = reducer.reduce(
        current=snapshot("委屈脆弱"),
        events=[event("relief", quote="别担心，我在")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.dominant_state == "依赖软萌"


def test_recovery_does_not_repeat_from_calm(reducer):
    result = reducer.reduce(
        current=snapshot("平稳常态"),
        events=[event("being_comforted")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.reason_code != "recovery_applied"


def test_conflicting_recoveries_keep_previous_state(catalog):
    conflicted = RuleCatalog(
        version=catalog.version,
        rules=catalog.rules,
        event_catalog=catalog.event_catalog,
        recoveries=(
            RecoveryRule("being_comforted", ("轻度吃醋",), "sana_emotion_calm"),
            RecoveryRule("being_comforted", ("轻度吃醋",), "sana_emotion_secure"),
        ),
    )
    result = DeterministicStateReducer(conflicted).reduce(
        current=snapshot("轻度吃醋"),
        events=[event("being_comforted")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.dominant_state == "轻度吃醋"
    assert result.conflict == "ambiguous_recovery"


def test_unknown_previous_state_does_not_escalate(reducer):
    result = reducer.reduce(
        current=snapshot(""),
        events=[event("cold_treatment")],
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.dominant_state in ("", "平稳常态")


def test_catalog_rejects_unknown_tags():
    from sana_bot.errors import ConfigError

    payload = {
        "primary_moods": ["neutral", "happy", "angry", "sad"],
        "event_tags": ["steady_daily"],
        "states": [
            {
                "id": "x",
                "label": "未知状态",
                "primary": "neutral",
                "tags": ["未批准标签"],
                "event_tags": ["steady_daily"],
            }
        ],
    }
    with pytest.raises(ConfigError) as excinfo:
        catalog_from_package(payload, version="v")
    assert excinfo.value.reason_code == "package_scope_unmapped"
