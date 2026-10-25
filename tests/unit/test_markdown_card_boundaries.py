"""T010：完整卡边界、吃醋恢复同块、常驻全文与示例说话者。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sana_bot.ingest.markdown_package import ensure_package_complete, parse_markdown_package

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config" / "character-package.json"


@pytest.fixture(scope="module")
def package():
    parsed = parse_markdown_package(CONFIG, project_root=REPO_ROOT)
    ensure_package_complete(parsed)
    return parsed


def test_core_and_output_are_complete_residents(package):
    core = package.residents[0]
    output = package.residents[1]
    general = package.residents[2]
    assert core.resident_id == "sana_core"
    assert output.resident_id == "sana_output"
    assert general.resident_id == "sana_emotion_general"
    # 01/02 全文常驻：包含标题与全部章节，不被 H2 打散
    assert core.raw_text.startswith("# 乾纱凪核心设定")
    assert "## " in core.raw_text
    assert "行为边界" in core.raw_text
    assert output.raw_text.startswith("# 乾纱凪角色对话输出规范")
    assert "## " in output.raw_text
    assert len(core.raw_text) > 1000


def test_emotion_general_is_resident_policy_only(package):
    general = package.card_by_id("sana_emotion_general")
    assert general.type.value == "emotion_policy"
    assert general.resident_only
    assert general.encoded is False
    assert general.raw_text.startswith("状态可以叠加")
    encoded_ids = {card.card_id for card in package.encoded_cards}
    assert "sana_emotion_general" not in encoded_ids


def test_jealous_card_keeps_three_levels_and_recovery(package):
    card = package.card_by_id("sana_emotion_jealous")
    assert card is not None
    assert "轻度" in card.raw_text
    assert "中度" in card.raw_text
    assert "重度" in card.raw_text
    assert "恢复" in card.raw_text
    # 三级与恢复必须在同一完整卡内，未被拆散
    assert card.raw_text.count("## ") == 0
    assert "避免把恢复写成争辩输赢" in card.raw_text


def test_every_card_keeps_full_body(package):
    for card in package.cards:
        assert card.raw_text.strip()
        assert not card.raw_text.startswith("## ")
        assert card.source_ranges
        source = next(item for item in package.sources if item.source_id == card.source_id)
        raw = source.raw_text
        for source_range in card.source_ranges:
            assert raw[source_range.char_start : source_range.char_end] == card.raw_text
            assert source_range.line_start >= 1
            assert source_range.line_end >= source_range.line_start


def test_unicode_positions_use_code_points(package):
    card = package.card_by_id("sana_lore_appearance")
    source = next(item for item in package.sources if item.source_id == card.source_id)
    source_range = card.source_ranges[0]
    text = source.raw_text[source_range.char_start : source_range.char_end]
    assert text == card.raw_text
    # 原文不做 Unicode 归一化：定位直接使用原字符
    assert "酒红色长发" in text


def test_multiline_example_turns_and_continuations(package):
    card = package.card_by_id("sana_example_jealous")
    speakers = [turn.speaker for turn in card.speaker_turns]
    assert speakers == ["user", "sana", "user", "sana"]
    assert card.speaker_turns[0].text == "刚才和别的女生聊得久了点"
    assert "（我松开抱着的手臂）" in card.speaker_turns[-1].text
    # 续行属于上一说话者，不是新一轮
    assert "我在旁边站了好一会儿呢" in card.speaker_turns[1].text


def test_example_role_line_only_examples(package):
    card = package.card_by_id("sana_example_share")
    assert [turn.speaker for turn in card.speaker_turns] == ["sana", "user", "sana"]
    assert card.provenance.value == "generated"
    assert any("示例" in ref for ref in card.constraint_refs)


def test_example_provenance_distinguishes_adapted_and_generated(package):
    assert package.card_by_id("sana_example_care").provenance.value == "adapted"
    assert package.card_by_id("sana_example_gift").provenance.value == "generated"
    assert package.card_by_id("sana_lore_family").provenance.value == "source"


def test_no_card_is_inferred_as_monologue(package):
    """任何无说话者内容都不得被当成心理独白素材。"""
    for card in package.cards:
        if card.type.value != "dialogue_example":
            assert card.speaker_turns == ()
    for resident in package.residents:
        assert resident.resident_id in {
            "sana_core",
            "sana_output",
            "sana_emotion_general",
        }


def test_card_counts_match_current_sources(package):
    counts: dict[str, int] = {}
    for card in package.cards:
        counts[card.type.value] = counts.get(card.type.value, 0) + 1
    assert counts["emotion_policy"] == 1
    assert counts["emotion_rule"] == 6
    assert counts["scene_rule"] == 6
    assert counts["dialogue_example"] == 7
    assert counts["background"] + counts["appearance"] + counts["profile"] == 6
    assert len(package.cards) == 26
    assert len(package.encoded_cards) == 25


def test_excluded_files_are_recorded_but_not_loaded(package):
    excluded = {source.relative_path for source in package.sources if not source.included}
    assert excluded == {"00_加载说明.md", "07_整理记录与待配置.md"}
    for source in package.sources:
        if not source.included:
            assert source.raw_text == ""


def test_embedding_input_is_complete_and_stable(package):
    for card in package.encoded_cards:
        assert card.embedding_input
        assert card.raw_text in card.embedding_input
        assert f"类型：{card.type.value}" in card.embedding_input
        assert f"scope：{card.scope_raw}" in card.embedding_input


def test_repeated_parse_is_consistent(package):
    again = parse_markdown_package(CONFIG, project_root=REPO_ROOT)
    assert again.structure_hash == package.structure_hash
    assert again.source_set_hash == package.source_set_hash
    assert [card.card_id for card in again.cards] == [card.card_id for card in package.cards]
    assert [card.raw_text for card in again.cards] == [card.raw_text for card in package.cards]
