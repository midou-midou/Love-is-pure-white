"""正式设定的结构契约：八章、补充约束、字段归属、负例与示例角色。

使用仓库内正式资料与人工核对 fixture；未通过核对的推导映射不得当作已确认。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sana_bot.enums import ContentType, FieldType
from sana_bot.ingest.docx_reader import read_docx
from sana_bot.ingest.role_splitter import is_heading_node, split_roles
from sana_bot.ingest.rule_parser import build_nodes
from sana_bot.state.catalog import build_draft_catalog
from sana_bot.ingest.models import SPLITTER_VERSION  # noqa: F401  (import 稳定性检查)

SOURCE = Path("doc/乾纱凪角色设定.docx")


@pytest.fixture(scope="module")
def reviewed(fixtures_dir: Path) -> dict:
    return json.loads((fixtures_dir / "character_structure.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def parsed(repo_root: Path):
    source = repo_root / SOURCE
    if not source.exists():  # pragma: no cover - 资料缺失时明确跳过
        pytest.skip("正式资料不在仓库中")
    read = read_docx(source)
    return read, build_nodes(read.blocks, document_id="doc-contract")


def test_source_hash_matches_reviewed_fixture(parsed, reviewed) -> None:
    read, _ = parsed
    assert read.file_hash == reviewed["source_sha256"]


def test_all_eight_chapters_and_supplement_are_recognized(parsed, reviewed) -> None:
    _, result = parsed
    titles = {item["title"]: item["content_type"] for item in result.heading_tree}
    for chapter, content_type in reviewed["expected_chapters"].items():
        assert chapter in titles, f"缺少章节：{chapter}"
        assert titles[chapter] == content_type


def test_subheadings_are_recognized(parsed, reviewed) -> None:
    _, result = parsed
    titles = {item["title"] for item in result.heading_tree}
    for subheading in reviewed["expected_subheadings"]:
        assert subheading in titles, f"缺少子标题：{subheading}"


def test_field_expectations(parsed, reviewed) -> None:
    _, result = parsed
    for expectation in reviewed["field_expectations"]:
        scope = expectation["scope_contains"]
        scoped = [
            node
            for node in result.nodes
            if scope in " | ".join(node.heading_path) and not is_heading_node(node)
        ]
        assert scoped, f"缺少作用域：{scope}"
        found_fields = {node.field_type.value for node in scoped if node.field_type}
        for field in expectation["fields"]:
            assert field in found_fields, f"{scope} 缺少字段：{field}"
        assert any(
            expectation["raw_text_contains"] in node.raw_text for node in scoped
        ), f"{scope} 缺少原文：{expectation['raw_text_contains']}"


def test_negative_examples_are_not_candidates(parsed, reviewed) -> None:
    _, result = parsed
    chapter_seven = [
        node
        for node in result.nodes
        if any(part.startswith("七、") for part in node.heading_path)
        and not is_heading_node(node)
        and not node.conditions.get("annotation")
    ]
    annotations = [
        node
        for node in result.nodes
        if any(part.startswith("七、") for part in node.heading_path)
        and node.conditions.get("annotation")
    ]
    assert annotations, "章节说明应作为标注单独归类，而不是标为可发送候选"
    assert chapter_seven
    for node in chapter_seven:
        assert node.content_type is ContentType.NEGATIVE
        assert node.field_type is FieldType.NEGATIVE
        assert "candidate" not in node.conditions
    joined = "".join(node.raw_text for node in chapter_seven)
    assert "我讨厌你" in joined
    negatives = [
        node
        for node in result.nodes
        if node.content_type is ContentType.NEGATIVE and not is_heading_node(node)
    ]
    for expectation in reviewed["negative_example_phrases"]:
        scoped = [
            node
            for node in negatives
            if expectation["scope_contains"] in " | ".join(node.heading_path)
        ]
        assert scoped, f"缺少负例作用域：{expectation['scope_contains']}"
        assert any(
            expectation["phrase"] in node.raw_text for node in scoped
        ), f"负例短语缺失：{expectation['phrase']}"


def test_example_dialogue_roles_are_attributed(parsed, reviewed) -> None:
    read, result = parsed
    roles = split_roles(result.nodes)
    speakers = roles.speaker_counts
    for label, expected in reviewed["expected_dialogue_turns"].items():
        canonical = {"你": "user", "纱凪": "sana"}[label]
        assert speakers.get(canonical) == expected
    assert not roles.speaker_counts.get("说明")
    assert not any(span.kind.value == "monologue" for span in roles.spans)


def test_no_monologue_material_is_reported_honestly(parsed, reviewed) -> None:
    _, result = parsed
    roles = split_roles(result.nodes)
    has_monologue = any(span.kind.value == "monologue" for span in roles.spans)
    assert has_monologue is reviewed["monologue_material_present"]


def test_every_source_range_round_trips(parsed) -> None:
    read, result = parsed
    for node in result.nodes:
        assert read.body_text[node.char_start : node.char_end] == node.raw_text


def test_draft_catalog_marks_derived_rules_unapproved(parsed, reviewed) -> None:
    _, result = parsed
    from sana_bot.ingest.models import StructuredDocument, SourceDocument

    document = StructuredDocument(
        id="doc-contract",
        source=SourceDocument(
            id="s1", relative_path=str(SOURCE), file_hash="hash", imported_at="2026-10-16"
        ),
        structure_hash=result.structure_hash,
        typed_nodes=result.nodes,
        heading_tree=result.heading_tree,
    )
    catalog = build_draft_catalog(document)
    assert catalog.approved_rules == ()
    assert catalog.pending_rules, "推导映射必须保持待审"
    romantic = [rule for rule in catalog.rules if rule.relationship == "romantic"]
    non_romantic = [rule for rule in catalog.rules if rule.relationship == "non_romantic"]
    assert romantic, "恋人分支来源应为第四章"
    assert non_romantic, "非恋爱分支应作为推导项列出"
    assert all(rule.provenance.value == "derived" for rule in non_romantic)
    assert catalog.hard_constraints, "恋人限定硬限制必须始终生效"
