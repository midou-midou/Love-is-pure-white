"""好感度计分：最长非重叠匹配、权重、聊天日、开心次数与门槛边界。"""

from __future__ import annotations

import pytest

from sana_bot.config import AffectionWeights, RuntimeConfig
from sana_bot.enums import PrimaryMood
from sana_bot.state.affection import KeywordMatcher, normalize_text
from sana_bot.state.relationship import RelationshipService
from sana_bot.storage.database import Database
from sana_bot.storage.relationship_repository import RelationshipRepository


def test_longest_non_overlapping_match_wins() -> None:
    matcher = KeywordMatcher(("谢谢", "谢谢你"))
    assert matcher.count("谢谢你") == 1
    assert matcher.hits("谢谢你") == [(0, 3)]


def test_repeated_keyword_counts_each_position() -> None:
    matcher = KeywordMatcher(("谢谢",))
    assert matcher.count("谢谢，真的谢谢") == 2


def test_empty_keyword_list_is_legal_and_matches_nothing() -> None:
    matcher = KeywordMatcher(())
    assert matcher.count("谢谢谢谢") == 0
    assert RuntimeConfig(keywords=()).issues() == []


def test_normalization_handles_width_and_case() -> None:
    matcher = KeywordMatcher(("ABC",))
    assert matcher.count("ａｂｃ") == 1
    assert normalize_text("ＡＢＣ") == "abc"


def test_weights_default_and_validation() -> None:
    weights = AffectionWeights()
    assert (weights.keyword, weights.day, weights.happy) == (1.0, 2.0, 5.0)
    assert weights.issues() == []
    assert AffectionWeights(happy=3, keyword=3).issues()


def test_manual_scoring_example_from_quickstart(database: Database) -> None:
    """第一天 happy 含一次关键词：8 分；同日再一轮 happy：13 分；次日两次关键词：22 分。"""
    repository = RelationshipRepository(database)
    service = RelationshipService(repository, config=RuntimeConfig(keywords=("谢谢",)))
    user = "u1"

    with database.write():
        repository.ensure(user)
        repository.register_dialogue_day(user_id=user, local_date="2026-10-16", turn_id="t1")
        repository.update_score(
            user_id=user,
            delta=1 * 1 + 2 + 5,
            keyword_hits=1,
            new_day=True,
            happy_delta=True,
            romance_status=service.decide(user, score_after=8).after,
            config_version=1,
        )
    assert repository.get(user).score == pytest.approx(8.0)

    with database.write():
        repository.update_score(
            user_id=user,
            delta=5,
            keyword_hits=0,
            new_day=False,
            happy_delta=True,
            romance_status=service.decide(user, score_after=13).after,
            config_version=1,
        )
    assert repository.get(user).score == pytest.approx(13.0)

    with database.write():
        repository.register_dialogue_day(user_id=user, local_date="2026-10-17", turn_id="t3")
        repository.update_score(
            user_id=user,
            delta=2 * 1 + 2 + 5,
            keyword_hits=2,
            new_day=True,
            happy_delta=True,
            romance_status=service.decide(user, score_after=22).after,
            config_version=1,
        )
    final = repository.get(user)
    assert final.score == pytest.approx(22.0)
    assert final.dialogue_days == 2
    assert final.keyword_count == 3
    assert final.happy_count == 3


def test_same_day_counts_once(database: Database) -> None:
    repository = RelationshipRepository(database)
    with database.write():
        assert repository.register_dialogue_day(user_id="u1", local_date="2026-10-16", turn_id="t1")
        assert not repository.register_dialogue_day(user_id="u1", local_date="2026-10-16", turn_id="t2")
    assert repository.days_for("u1") == ["2026-10-16"]


def test_threshold_equality_enters_romance_and_never_reverts(database: Database) -> None:
    repository = RelationshipRepository(database)
    service = RelationshipService(repository, config=RuntimeConfig(romance_threshold=100))
    with database.write():
        repository.ensure("u1")
    before = service.decide("u1", score_after=99.9)
    assert before.after.value == "non_romantic"
    at_threshold = service.decide("u1", score_after=100)
    assert at_threshold.after.value == "romantic"
    with database.write():
        repository.update_score(
            user_id="u1",
            delta=100,
            keyword_hits=0,
            new_day=False,
            happy_delta=False,
            romance_status=at_threshold.after,
            config_version=1,
        )
    # 门槛提高后不回退
    stricter = RelationshipService(repository, config=RuntimeConfig(romance_threshold=500))
    assert stricter.decide("u1", score_after=100).after.value == "romantic"


def test_history_is_not_recomputed_when_weights_change(database: Database) -> None:
    repository = RelationshipRepository(database)
    with database.write():
        repository.ensure("u1")
        repository.insert_event(
            delivery_id="d1",
            turn_id="t1",
            user_id="u1",
            day="2026-10-16",
            keyword_hits=1,
            new_day=True,
            happy_delta=True,
            weights=AffectionWeights().to_payload(),
            delta=8,
            score_before=0,
            score_after=8,
            romance_before="non_romantic",
            romance_after="non_romantic",
            config_version=1,
        )
        repository.update_score(
            user_id="u1",
            delta=8,
            keyword_hits=1,
            new_day=True,
            happy_delta=True,
            romance_status="non_romantic",
            config_version=1,
        )
    events = repository.events_for("u1")
    assert events[0]["delta"] == pytest.approx(8.0)
    assert repository.get("u1").score == pytest.approx(8.0)


def test_happy_delta_requires_happy_snapshot() -> None:
    from sana_bot.state.models import empty_snapshot

    happy = empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c", mood=PrimaryMood.HAPPY)
    sad = empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c", mood=PrimaryMood.SAD)
    assert happy.is_happy is True
    assert sad.is_happy is False
