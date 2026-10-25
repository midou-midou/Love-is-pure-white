"""T009：资料包输入契约（显式清单、元数据、路径与示例说话者）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sana_bot.errors import ConfigError
from sana_bot.ingest.markdown_package import (
    ensure_package_complete,
    load_package_config,
    parse_markdown_package,
    parse_example_turns,
)
from sana_bot.ingest.package_models import PackageError

MINIMAL = Path(__file__).resolve().parents[1] / "fixtures" / "character_package" / "minimal"


def _copy_minimal(tmp_path: Path) -> Path:
    target = tmp_path / "minimal"
    shutil.copytree(MINIMAL, target)
    return target


def _parse(root: Path):
    return parse_markdown_package(root / "config.json", project_root=root)


def test_config_declares_six_sources_and_three_residents():
    config = load_package_config(MINIMAL / "config.json")
    assert [path for path, _role in config.included_files] == [
        "01_core.md",
        "02_output.md",
        "03_emotion.md",
        "04_scene.md",
        "05_lore.md",
        "06_examples.md",
    ]
    assert config.resident_ids == ("sana_core", "sana_output", "sana_emotion_general")
    assert config.format_version == "structural_v2"
    assert config.chunking_strategy == "markdown_cards_v1"


def test_config_rejects_unknown_field(tmp_path):
    root = _copy_minimal(tmp_path)
    payload = json.loads((root / "config.json").read_text(encoding="utf-8"))
    payload["unexpected"] = True
    (root / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PackageError) as excinfo:
        _parse(root)
    assert excinfo.value.reason_code == "package_metadata_invalid"


def test_metadata_order_is_free_but_values_must_be_present(tmp_path):
    root = _copy_minimal(tmp_path)
    scene = root / "docs" / "04_scene.md"
    scene.write_text(
        "# 测试场景行为\n\n## 分享日常\n\n"
        "scope: 恋人日常\nsource: 原文第三节\ncharacter: 乾纱凪\n"
        "type: scene_rule\nid: sana_scene_share\n\n正文\n",
        encoding="utf-8",
    )
    package = _parse(root)
    card = package.card_by_id("sana_scene_share")
    assert card is not None
    assert card.scope_raw == "恋人日常"
    assert card.source_label == "原文第三节"


def test_missing_metadata_field_blocks_enablement(tmp_path):
    root = _copy_minimal(tmp_path)
    scene = root / "docs" / "04_scene.md"
    scene.write_text(
        "# 测试场景行为\n\n## 分享日常\n\n"
        "id: sana_scene_share\ntype: scene_rule\ncharacter: 乾纱凪\nsource: 原文\n\n正文\n",
        encoding="utf-8",
    )
    package = _parse(root)
    codes = {issue.reason_code for issue in package.issues}
    assert "package_metadata_invalid" in codes
    with pytest.raises(PackageError) as excinfo:
        ensure_package_complete(package)
    assert excinfo.value.reason_code == "package_metadata_invalid"


def test_duplicate_metadata_field_is_reported(tmp_path):
    root = _copy_minimal(tmp_path)
    scene = root / "docs" / "04_scene.md"
    scene.write_text(
        "# 测试场景行为\n\n## 分享日常\n\n"
        "id: sana_scene_share\ntype: scene_rule\ncharacter: 乾纱凪\n"
        "source: 原文\nscope: a\nscope: b\n\n正文\n",
        encoding="utf-8",
    )
    package = _parse(root)
    assert "repeated_metadata_field" in {issue.reason_code for issue in package.issues}


def test_unknown_metadata_field_is_reported(tmp_path):
    root = _copy_minimal(tmp_path)
    scene = root / "docs" / "04_scene.md"
    scene.write_text(
        "# 测试场景行为\n\n## 分享日常\n\n"
        "id: sana_scene_share\ntype: scene_rule\ncharacter: 乾纱凪\n"
        "source: 原文\nscope: 恋人\nextra: 不该出现\n\n正文\n",
        encoding="utf-8",
    )
    package = _parse(root)
    assert "package_metadata_invalid" in {issue.reason_code for issue in package.issues}


def test_unknown_card_type_is_rejected(tmp_path):
    root = _copy_minimal(tmp_path)
    scene = root / "docs" / "04_scene.md"
    scene.write_text(
        "# 测试场景行为\n\n## 分享日常\n\n"
        "id: sana_scene_share\ntype: monologue_hint\ncharacter: 乾纱凪\n"
        "source: 原文\nscope: 恋人\n\n正文\n",
        encoding="utf-8",
    )
    package = _parse(root)
    assert "unknown_card_type" in {issue.reason_code for issue in package.issues}


def test_card_type_must_match_source_role(tmp_path):
    root = _copy_minimal(tmp_path)
    scene = root / "docs" / "04_scene.md"
    scene.write_text(
        "# 测试场景行为\n\n## 分享日常\n\n"
        "id: sana_scene_share\ntype: background\ncharacter: 乾纱凪\n"
        "source: 原文\nscope: 恋人\n\n正文\n",
        encoding="utf-8",
    )
    package = _parse(root)
    assert "package_metadata_invalid" in {issue.reason_code for issue in package.issues}


def test_duplicate_card_id_is_rejected(tmp_path):
    root = _copy_minimal(tmp_path)
    lore = root / "docs" / "05_lore.md"
    lore.write_text(
        (MINIMAL / "docs" / "05_lore.md").read_text(encoding="utf-8")
        + "\n## 重复卡\n\nid: sana_lore_family\ntype: background\ncharacter: 乾纱凪\n"
        "source: 原文\nscope: 家庭\n\n重复正文\n",
        encoding="utf-8",
    )
    package = _parse(root)
    assert "duplicate_card_id" in {issue.reason_code for issue in package.issues}


def test_missing_included_file_is_reported(tmp_path):
    root = _copy_minimal(tmp_path)
    (root / "docs" / "05_lore.md").unlink()
    with pytest.raises(PackageError) as excinfo:
        _parse(root)
    assert excinfo.value.reason_code == "package_file_missing"


def test_path_escape_is_rejected(tmp_path):
    root = _copy_minimal(tmp_path)
    payload = json.loads((root / "config.json").read_text(encoding="utf-8"))
    payload["included_files"][0]["path"] = "../outside.md"
    (root / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PackageError) as excinfo:
        _parse(root)
    assert excinfo.value.reason_code == "source_path_escape"


def test_absolute_source_path_is_rejected(tmp_path):
    root = _copy_minimal(tmp_path)
    payload = json.loads((root / "config.json").read_text(encoding="utf-8"))
    payload["included_files"][0]["path"] = str(root / "docs" / "01_core.md")
    (root / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PackageError) as excinfo:
        _parse(root)
    assert excinfo.value.reason_code == "source_path_escape"


def test_symlink_outside_root_is_rejected(tmp_path):
    root = _copy_minimal(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# 外部\n\n不应被加载\n", encoding="utf-8")
    target = root / "docs" / "05_lore.md"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(PackageError) as excinfo:
        _parse(root)
    assert excinfo.value.reason_code == "source_path_escape"


def test_unlisted_file_is_reported_but_not_loaded(tmp_path):
    root = _copy_minimal(tmp_path)
    (root / "docs" / "99_extra.md").write_text("# 额外\n\n## 额外卡\n", encoding="utf-8")
    package = _parse(root)
    assert any(
        issue.reason_code == "package_file_unlisted" for issue in package.issues
    )
    assert package.card_by_id("sana_lore_family") is not None
    assert len(package.cards) == 6


def test_example_turns_preserve_continuation_and_actions():
    body = "用户：今天放学一起走吗？\n角色：好呀\n（我收拾了一下书包）\n\n用户：那说好了"
    turns, issue = parse_example_turns(
        body, body_start_line=10, char_start=0, file="06.md", heading="示例"
    )
    assert issue is None
    assert [turn.speaker for turn in turns] == ["user", "sana", "user"]
    assert "（我收拾了一下书包）" in turns[1].text
    assert turns[0].text == "今天放学一起走吗？"


def test_example_half_width_colon_is_supported():
    turns, issue = parse_example_turns(
        "用户: 在吗\n角色: 在的", body_start_line=1, char_start=0, file="06.md", heading="示例"
    )
    assert issue is None
    assert [turn.speaker for turn in turns] == ["user", "sana"]


def test_example_head_without_speaker_is_ambiguous():
    turns, issue = parse_example_turns(
        "这是没有说话者的开头\n用户：在吗",
        body_start_line=1,
        char_start=0,
        file="06.md",
        heading="示例",
    )
    assert issue is not None
    assert issue.reason_code == "example_speaker_ambiguous"
    # 不猜成独白，也不把它算进任何一轮
    assert [turn.speaker for turn in turns] == ["user"]


def test_emotion_policy_card_is_resident_only(tmp_path):
    package = _parse(_copy_minimal(tmp_path))
    general = package.card_by_id("sana_emotion_general")
    assert general is not None
    assert general.type.value == "emotion_policy"
    assert general.resident_only is True
    assert general not in package.encoded_cards
    assert "sana_emotion_general" in package.resident_ids


def test_no_speaker_content_is_not_treated_as_monologue(tmp_path):
    package = _parse(_copy_minimal(tmp_path))
    for card in package.cards:
        for turn in card.speaker_turns:
            assert turn.speaker in {"user", "sana"}


def test_config_requires_matching_policy_hash(tmp_path):
    root = _copy_minimal(tmp_path)
    payload = json.loads((root / "config.json").read_text(encoding="utf-8"))
    del payload["card_conditions"]["sana_scene_share"]
    (root / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    package = _parse(root)
    assert "package_scope_unmapped" in {issue.reason_code for issue in package.issues}
    with pytest.raises(PackageError):
        ensure_package_complete(package)


def test_condition_enum_values_are_validated(tmp_path):
    root = _copy_minimal(tmp_path)
    payload = json.loads((root / "config.json").read_text(encoding="utf-8"))
    payload["card_conditions"]["sana_scene_share"]["scene"] = "somewhere"
    (root / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_package_config(root / "config.json")
    assert excinfo.value.reason_code == "package_scope_unmapped"
