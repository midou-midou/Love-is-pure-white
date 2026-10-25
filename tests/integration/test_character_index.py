"""正式设定索引与分层约束：启用门槛、全局约束与分支 guard。

覆盖 SC-019–021 的可离线验证部分：违规草稿全部拦截、未批准映射不可执行、
原文逐字保留而输出不机械照搬。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sana_bot.dialogue.output_policy import (
    ConstrainedOutputPolicy,
    check_text,
    load_output_constraints,
)
from sana_bot.enums import ContentType, PrimaryMood, Provenance, RelationshipStatus, Scene
from sana_bot.errors import NotReadyError
from sana_bot.ingest.docx_reader import read_docx
from sana_bot.ingest.role_splitter import split_roles
from sana_bot.ingest.rule_parser import build_nodes
from sana_bot.ports import DialogueDraft
from sana_bot.state.catalog import RuleCatalog, StateRule, rules_by_event
from sana_bot.state.event_models import ApprovedEventCatalog
from sana_bot.state.models import empty_snapshot
from sana_bot.state.reducer import DeterministicStateReducer

SOURCE = Path("doc/乾纱凪角色设定.docx")


@pytest.fixture(scope="module")
def document(repo_root: Path):
    path = repo_root / SOURCE
    if not path.exists():  # pragma: no cover
        pytest.skip("正式资料不在仓库中")
    read = read_docx(path)
    parsed = build_nodes(read.blocks, document_id="doc-index")
    from sana_bot.ingest.models import SourceDocument, StructuredDocument

    return StructuredDocument(
        id="doc-index",
        source=SourceDocument(
            id="src", relative_path=str(SOURCE), file_hash=read.file_hash, imported_at="2026-10-16"
        ),
        structure_hash=parsed.structure_hash,
        typed_nodes=parsed.nodes,
        heading_tree=parsed.heading_tree,
        body_text=read.body_text,
    )


def test_global_constraints_load_without_recall(document) -> None:
    """禁区与输出规则固定加载，不依赖是否被语义检索命中。"""
    constraints = load_output_constraints(document)
    assert "我讨厌你" in constraints.forbidden_phrases
    assert "分手" in constraints.forbidden_phrases
    assert constraints.forbidden_patterns, "文档禁止的越界管束必须成为固定模式"
    assert constraints.source_version == document.structure_hash
    assert constraints.to_payload()["source_version"] == document.structure_hash


@pytest.mark.parametrize(
    "text",
    [
        "哼，我讨厌你",
        "你是不是不喜欢我了",
        "你为什么不理我",
        "总之我们分手吧",
        "不许和异性说话",
        "你应该早点睡觉",
        "你一定觉得很麻烦吧",
        "纱凪才不会说这种话",
    ],
)
def test_known_violations_are_blocked(document, text: str) -> None:
    constraints = load_output_constraints(document)
    assert check_text(text, constraints), f"必须拦截：{text}"


async def test_blocked_draft_uses_verified_fallback(document) -> None:
    from sana_bot.dialogue.fallback_catalog import DEFAULT_FALLBACK_CATALOG

    constraints = load_output_constraints(document)
    policy = ConstrainedOutputPolicy(constraints=constraints)
    draft = DialogueDraft(kind="normal", text="哼，我讨厌你。")
    decision = await policy.review(draft=draft, snapshot=empty_snapshot(
        snapshot_id="s", turn_id="t", conversation_id="c"
    ))
    assert not decision.approved
    fallback = DEFAULT_FALLBACK_CATALOG.draft_for(
        "confused", mood=PrimaryMood.NEUTRAL, reason_code="output_blocked"
    )
    fallback_decision = await policy.review(draft=fallback, snapshot=empty_snapshot(
        snapshot_id="s", turn_id="t", conversation_id="c"
    ))
    assert fallback_decision.approved, "预审兜底必须能通过同一规范"


def test_source_text_is_preserved_verbatim(document) -> None:
    roles = split_roles(document.typed_nodes)
    for span in roles.spans:
        assert document.body_text[span.source_start : span.source_end] == span.raw_text


def test_output_must_not_copy_example_lines_verbatim(document) -> None:
    roles = split_roles(document.typed_nodes)
    examples = [
        span.raw_text.split("：", 1)[-1].strip()
        for span in roles.spans
        if span.kind.value == "dialogue" and span.speaker == "sana"
    ]
    assert examples
    constraints = load_output_constraints(document, reference_texts=examples)
    blocked = {violation.code for violation in check_text(examples[0], constraints)}
    assert "verbatim_copy" in blocked


def test_negative_material_is_marked_for_isolation(document) -> None:
    from sana_bot.ingest.role_splitter import is_heading_node

    negatives = [
        node
        for node in document.typed_nodes
        if node.content_type is ContentType.NEGATIVE and not is_heading_node(node)
    ]
    assert negatives
    for node in negatives:
        assert node.field_type is not None
        assert node.field_type.value == "negative"
        assert node.conditions.get("derived") is False or isinstance(
            node.conditions.get("derived"), bool
        )


# --------------------------------------------------------------------------- 六类分支 guard


def _catalog() -> RuleCatalog:
    return RuleCatalog(
        version="test",
        event_catalog=ApprovedEventCatalog(version="events-v1", tags=("sweet_talk", "cold_treatment")),
        rules=(
            StateRule(
                id="state::害羞局促",
                label="害羞局促",
                source_node_ids=("n1",),
                provenance=Provenance.SOURCE,
                approved=True,
                relationship="romantic",
                scene="any",
                event_tags=("sweet_talk",),
                to_primary=PrimaryMood.HAPPY,
                to_tags=("害羞局促",),
                dominant_priority=5,
            ),
            StateRule(
                id="state::委屈脆弱",
                label="委屈脆弱",
                source_node_ids=("n2",),
                provenance=Provenance.SOURCE,
                approved=True,
                relationship="romantic",
                scene="private",
                event_tags=("cold_treatment",),
                to_primary=PrimaryMood.SAD,
                to_tags=("委屈脆弱",),
                dominant_priority=8,
            ),
            StateRule(
                id="derived::礼貌愉快",
                label="礼貌愉快（非恋爱）",
                source_node_ids=("n3",),
                provenance=Provenance.DERIVED,
                approved=False,
                relationship="non_romantic",
                scene="any",
                event_tags=("sweet_talk",),
                to_primary=PrimaryMood.HAPPY,
                to_tags=("礼貌愉快",),
                dominant_priority=5,
            ),
        ),
    )


def test_unapproved_rules_cannot_execute() -> None:
    catalog = _catalog()
    assert [rule.id for rule in catalog.approved_rules] == ["state::害羞局促", "state::委屈脆弱"]
    candidates = rules_by_event(
        catalog,
        ["sweet_talk"],
        relationship=RelationshipStatus.NON_ROMANTIC,
        scene=Scene.PUBLIC,
    )
    assert candidates == (), "未批准的非恋爱分支不得参与转换"


def test_romantic_rules_do_not_leak_into_non_romantic() -> None:
    catalog = _catalog()
    reducer = DeterministicStateReducer(catalog)
    from sana_bot.state.event_models import StateEvent

    event = StateEvent(
        tag="sweet_talk", source_message_id="m1", evidence_start=0, evidence_end=2, quote="情话"
    )
    result = reducer.reduce(
        current=empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c"),
        events=(event,),
        relationship=RelationshipStatus.NON_ROMANTIC,
        scene=Scene.PUBLIC,
    )
    assert result.applied_rule_ids == ()
    assert result.reason_code == "no_matching_rule"
    assert result.primary_mood is PrimaryMood.NEUTRAL


def test_private_scene_rules_do_not_apply_in_public_or_unknown() -> None:
    catalog = _catalog()
    reducer = DeterministicStateReducer(catalog)
    from sana_bot.state.event_models import StateEvent

    event = StateEvent(
        tag="cold_treatment",
        source_message_id="m1",
        evidence_start=0,
        evidence_end=2,
        quote="冷落",
    )
    for scene in (Scene.PUBLIC, Scene.UNKNOWN):
        result = reducer.reduce(
            current=empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c"),
            events=(event,),
            relationship=RelationshipStatus.ROMANTIC,
            scene=scene,
        )
        assert result.applied_rule_ids == (), f"独处限定规则不得在 {scene.value} 生效"


def test_romantic_rule_applies_in_private_scene() -> None:
    from sana_bot.state.event_models import StateEvent

    reducer = DeterministicStateReducer(_catalog())
    event = StateEvent(
        tag="cold_treatment",
        source_message_id="m1",
        evidence_start=0,
        evidence_end=2,
        quote="冷落",
    )
    result = reducer.reduce(
        current=empty_snapshot(snapshot_id="s", turn_id="t", conversation_id="c"),
        events=(event,),
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.applied_rule_ids == ("state::委屈脆弱",)
    assert result.primary_mood is PrimaryMood.SAD
    assert result.transition_evidence[0]["quote"] == "冷落"


def test_conflicting_rules_keep_previous_state() -> None:
    from sana_bot.state.event_models import StateEvent

    catalog = RuleCatalog(
        version="conflict",
        event_catalog=ApprovedEventCatalog(version="events-v1", tags=("teasing",)),
        rules=tuple(
            StateRule(
                id=f"state::candidate-{index}",
                label=label,
                source_node_ids=(f"n{index}",),
                provenance=Provenance.SOURCE,
                approved=True,
                relationship="any",
                scene="any",
                event_tags=("teasing",),
                to_primary=mood,
                to_tags=(label,),
                dominant_priority=6,
            )
            for index, (label, mood) in enumerate(
                (("傲娇炸毛", PrimaryMood.ANGRY), ("不满直率", PrimaryMood.SAD))
            )
        ),
    )
    reducer = DeterministicStateReducer(catalog)
    current = empty_snapshot(
        snapshot_id="s",
        turn_id="t",
        conversation_id="c",
        mood=PrimaryMood.HAPPY,
        tags=("害羞局促",),
        dominant_state="害羞局促",
    )
    result = reducer.reduce(
        current=current,
        events=(
            StateEvent(
                tag="teasing",
                source_message_id="m1",
                evidence_start=0,
                evidence_end=2,
                quote="调侃",
            ),
        ),
        relationship=RelationshipStatus.ROMANTIC,
        scene=Scene.PRIVATE,
    )
    assert result.conflict
    assert result.primary_mood is PrimaryMood.HAPPY
    assert result.secondary_tags == ("害羞局促",)
    assert result.reason_code == "conflict"


def test_hard_constraints_always_present(document) -> None:
    from sana_bot.state.catalog import build_draft_catalog

    catalog = build_draft_catalog(document)
    assert "不得命令用户不能与异性交流" in catalog.hard_constraints
    assert "无论映射是否批准都必须生效" not in catalog.hard_constraints


def test_draft_catalog_matches_committed_file(document) -> None:
    """提交的待审 catalog 必须与当前解析结果一致，避免文档悄悄漂移。"""
    from sana_bot.state.catalog import build_draft_catalog

    committed = Path(__file__).resolve().parents[2] / "config" / "state-catalog.draft.json"
    if not committed.exists():  # pragma: no cover
        pytest.skip("尚未生成待审 catalog")
    payload = json.loads(committed.read_text(encoding="utf-8"))
    catalog = build_draft_catalog(document)
    assert payload["version"] == catalog.version
    assert [rule["id"] for rule in payload["rules"]] == [rule.id for rule in catalog.rules]
    assert all(rule["approved"] is False for rule in payload["rules"])


def test_incomplete_version_cannot_activate(tmp_path: Path) -> None:
    """不完整版本不能启用：没有校准报告时必须明确失败。"""
    import argparse

    from sana_bot.cli_index import run_activate
    from sana_bot.enums import IndexStatus, MonologueStatus
    from sana_bot.storage.database import open_runtime_database
    from sana_bot.storage.index_repository import SnapshotRecord, StructureRepository
    from sana_bot.timeutil import to_utc_iso, utc_now

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = open_runtime_database(runtime / "sana-bot.sqlite3")
    repository = StructureRepository(database)
    now = to_utc_iso(utc_now())
    try:
        with database.write():
            repository.insert_snapshot(
                SnapshotRecord(
                    id="incomplete",
                    source_hash="src",
                    structure_hash="struct",
                    split_profile_hash="split",
                    snapshot_hash="snapshot",
                    manifest_hash="manifest",
                    parser_version="p",
                    splitter_version="s",
                    embedding_model_id="m",
                    embedding_revision="r",
                    dimension=512,
                    rule_catalog_version="c",
                    reply_calibration_id=None,
                    monologue_calibration_id=None,
                    monologue_status=MonologueStatus.DISABLED_NO_MATERIAL,
                    status=IndexStatus.STAGED,
                    artifact_path=str(tmp_path / "none"),
                    manifest={},
                    created_at=now,
                    updated_at=now,
                )
            )
        args = argparse.Namespace(index="incomplete", runtime_dir=str(runtime))
        with pytest.raises(NotReadyError) as info:
            run_activate(args)
        assert info.value.reason_code == "structure_missing"
    finally:
        database.close()
