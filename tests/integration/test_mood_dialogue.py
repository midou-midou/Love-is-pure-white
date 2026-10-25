"""心情管理：默认平静、合法/非法设置、快照一致与关系/场景限制。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sana_bot.cli_state import run_set, run_show
from sana_bot.config import RuntimeConfig
from sana_bot.dialogue.history import HistoryService
from sana_bot.dialogue.output_policy import ConstrainedOutputPolicy, OutputConstraints
from sana_bot.dialogue.service import DialogueTurnService
from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.errors import ConfigError
from sana_bot.ports import ModelBudget
from sana_bot.scheduling.serial_queue import QueueItem
from sana_bot.state.catalog import RuleCatalog, StateRule
from sana_bot.state.event_models import ApprovedEventCatalog, StateEvent
from sana_bot.state.event_extractor import LangChainStateEventExtractor
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer
from sana_bot.state.service import StateService
from sana_bot.storage.database import Database
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.inbound_repository import InboundRepository
from sana_bot.storage.index_repository import StructureRepository
from sana_bot.storage.state_repository import StateRepository
from tests.support import FakeModel, FakeQQTransport, make_event
from sana_bot.enums import Provenance

BASE_TIME = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _catalog() -> RuleCatalog:
    return RuleCatalog(
        version="mood-test",
        event_catalog=ApprovedEventCatalog(version="events-v1", tags=("steady_daily",)),
        rules=(
            StateRule(
                id="state::平稳常态",
                label="平稳常态",
                source_node_ids=("n1",),
                provenance=Provenance.SOURCE,
                approved=True,
                relationship="any",
                scene="any",
                event_tags=("steady_daily",),
                to_primary=PrimaryMood.NEUTRAL,
                to_tags=("平稳常态",),
                dominant_priority=1,
            ),
        ),
    )


@pytest.fixture
def state_service(database: Database) -> StateService:
    return StateService(
        repository=StateRepository(database),
        reducer=DeterministicStateReducer(_catalog()),
        catalog=_catalog(),
        inbound=InboundRepository(database),
        config=RuntimeConfig(),
    )


def test_default_state_is_neutral(database: Database, state_service: StateService) -> None:
    with database.write():
        user = InboundRepository(database).ensure_user("app-1", "openid-1")
    snapshot = state_service.current(user, "")
    assert snapshot.primary_mood is PrimaryMood.NEUTRAL
    assert snapshot.scene is Scene.UNKNOWN


def test_legal_mood_and_tag_are_applied(state_service: StateService) -> None:
    override = state_service.set_state("u1", mood="高兴", tags=["平稳常态"], scene="private")
    assert override.mood is PrimaryMood.HAPPY
    assert override.tags == ("平稳常态",)
    assert override.scene is Scene.PRIVATE
    snapshot = state_service.current("u1", "")
    assert snapshot.primary_mood is PrimaryMood.HAPPY


def test_invalid_settings_are_rejected_and_keep_current(state_service: StateService) -> None:
    state_service.set_state("u1", mood="happy")
    with pytest.raises(ConfigError):
        state_service.set_state("u1", mood="狂喜")
    with pytest.raises(ConfigError):
        state_service.set_state("u1", tags=["不存在的状态"])
    with pytest.raises(ConfigError):
        state_service.set_state("u1", scene="rooftop")
    with pytest.raises(ConfigError):
        state_service.set_state("u1")
    assert state_service.current("u1", "").primary_mood is PrimaryMood.HAPPY


def test_set_requires_explicit_command(database: Database, state_service: StateService) -> None:
    """普通 QQ 消息不能执行管理命令。"""
    from sana_bot.dialogue.commands import parse_command

    kind = parse_command("/状态 happy")
    assert kind is not None
    assert kind.allowed_from_qq is False
    assert state_service.current("u1", "").primary_mood is PrimaryMood.NEUTRAL


def test_management_changes_are_audited(database: Database, state_service: StateService) -> None:
    state_service.set_state("u1", mood="sad", actor="maintainer")
    audits = state_service.repository.audits("u1")
    assert audits and audits[0]["action"] == "state.set"
    assert "sad" in audits[0]["after_state"]


def test_private_rule_does_not_apply_in_public(database: Database) -> None:
    catalog = RuleCatalog(
        version="mood-test",
        event_catalog=ApprovedEventCatalog(version="events-v1", tags=("being_comforted",)),
        rules=(
            StateRule(
                id="state::依赖软萌",
                label="依赖软萌",
                source_node_ids=("n1",),
                provenance=Provenance.SOURCE,
                approved=True,
                relationship="romantic",
                scene="private",
                event_tags=("being_comforted",),
                to_primary=PrimaryMood.HAPPY,
                to_tags=("依赖软萌",),
                dominant_priority=9,
            ),
        ),
    )
    reducer = DeterministicStateReducer(catalog)
    event = StateEvent(
        tag="being_comforted", source_message_id="m1", evidence_start=0, evidence_end=2, quote="安慰"
    )
    public = reducer.reduce(
        current=empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c"),
        events=(event,),
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PUBLIC,
    )
    private = reducer.reduce(
        current=empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c"),
        events=(event,),
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert public.applied_rule_ids == ()
    assert private.applied_rule_ids == ("state::依赖软萌",)


def test_snapshot_is_frozen_into_storage(database: Database) -> None:
    repository = StateRepository(database)
    inbound = InboundRepository(database)
    with database.write():
        outcome = inbound.persist_inbound(make_event(message_id="m-1", text="你好"))
        turn_id = inbound.create_turn(
            conversation_id=outcome.conversation_id,
            inbound_id=outcome.inbound_id or "",
            input_text="你好",
            chat_day_value=outcome.chat_day,
        )
        snapshot = empty_snapshot(
            snapshot_id="s1",
            turn_id=turn_id,
            conversation_id=outcome.conversation_id,
            mood=PrimaryMood.HAPPY,
            tags=("害羞局促",),
        )
        repository.save_snapshot(snapshot)
    loaded = repository.snapshot_for_turn(turn_id)
    assert loaded is not None
    assert loaded.primary_mood is PrimaryMood.HAPPY
    assert loaded.secondary_tags == ("害羞局促",)


def test_state_cli_round_trip(database: Database, tmp_path: Path) -> None:
    runtime = database.path.parent
    with database.write():
        user = InboundRepository(database).ensure_user("app-1", "openid-1")
    set_payload = run_set(
        argparse.Namespace(runtime_dir=str(runtime), user=user, mood="难过", tag=[], scene=None)
    )
    assert set_payload["applied"]["primary_mood"] == "sad"
    shown = run_show(argparse.Namespace(runtime_dir=str(runtime), user=user))
    assert shown["effective"]["primary_mood"] == "sad"
    assert shown["audits"]


def test_state_cli_requires_user(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        run_show(argparse.Namespace(runtime_dir=str(tmp_path / "runtime"), user=None))
