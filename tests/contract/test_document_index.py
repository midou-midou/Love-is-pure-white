"""资料结构契约测试：字段、角色、无名对话与说明的区别、来源区间。

覆盖长句、短答句、重复文本与部分失败；不依赖 QQ 或真实模型。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sana_bot.enums import ContentType, FieldType, ReviewStatus, SpanKind
from sana_bot.ingest.docx_reader import detect_heading, read_docx
from sana_bot.ingest.models import RuleNode, SourceSpan
from sana_bot.ingest.role_splitter import LONG_SPAN_CHARS, split_roles
from sana_bot.ingest.rule_parser import build_nodes
from sana_bot.storage.database import Database
from tests.support.docx_builder import Block, blank, build_docx, heading, list_item, paragraph


def _structure(tmp_path: Path, blocks: list[Block], name: str = "fixture.docx"):
    source = build_docx(tmp_path / name, blocks)
    read = read_docx(source)
    parsed = build_nodes(read.blocks, document_id="doc-fixture")
    roles = split_roles(parsed.nodes)
    return read, parsed, roles


def _nodes_of(parsed, content_type: ContentType) -> list[RuleNode]:
    return [node for node in parsed.nodes if node.content_type is content_type]


def test_headings_are_detected_without_heading_styles(tmp_path: Path) -> None:
    read, parsed, _ = _structure(
        tmp_path,
        [
            heading("角色设定"),
            heading("一、基础档案"),
            paragraph("外貌细节"),
            heading("外貌细节加粗"),
            paragraph("正文内容在这里"),
            heading("八、输出规则"),
        ],
    )
    titles = [item["title"] for item in parsed.heading_tree]
    assert titles[0] == "角色设定"
    assert "一、基础档案" in titles
    assert "八、输出规则" in titles
    levels = {item["title"]: item["level"] for item in parsed.heading_tree}
    assert levels["一、基础档案"] == 1
    assert levels["外貌细节加粗"] == 2
    # 未加粗且带句读的正文不会升格为标题
    assert "正文内容在这里" not in titles
    assert read.blocks[0].heading is not None
    assert detect_heading(read.blocks[4]) is None


def test_colon_fields_are_classified_and_ranges_preserved(tmp_path: Path) -> None:
    trigger = "触发条件：被直白夸奖、被温柔凝视。"
    overall = "整体表现：手足无措，拼命掩饰慌乱。"
    action = "肢体细节：脸颊发烫，眼神躲闪。"
    read, parsed, _ = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            heading("四、专属情绪状态系统（恋人限定）"),
            heading("2. 害羞局促状态"),
            paragraph(f"{trigger} {overall} {action}"),
        ],
    )
    nodes = _nodes_of(parsed, ContentType.STATE)
    fields = {node.field_type: node for node in nodes if node.field_type}
    assert fields[FieldType.TRIGGER].raw_text == trigger
    assert fields[FieldType.OVERALL].raw_text == overall
    assert fields[FieldType.ACTION].raw_text == action
    body = read.body_text
    for node in nodes:
        assert body[node.char_start : node.char_end] == node.raw_text


def test_multiple_fields_keep_their_own_source_span(tmp_path: Path) -> None:
    _, parsed, _ = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph("喜欢：小猫；和风料理\n讨厌：蟑螂"),
        ],
    )
    attributes = [
        node
        for node in parsed.nodes
        if node.field_type is FieldType.ATTRIBUTE and node.content_type is ContentType.PROFILE
    ]
    assert len(attributes) == 2
    assert attributes[0].raw_text.startswith("喜欢：")
    assert attributes[1].raw_text.startswith("讨厌：")
    assert attributes[0].char_end <= attributes[1].char_start


def test_named_dialogue_and_unnamed_monologue_are_distinguished(tmp_path: Path) -> None:
    _, _, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph(
                "示例\n"
                "你：今天放学一起走吗？\n"
                "纱凪：好呀，刚好可以和你多待一会。\n"
                "（其实心里已经很高兴了，只是嘴上不承认。）"
            ),
        ],
    )
    by_kind = {}
    for span in roles.spans:
        by_kind.setdefault(span.kind, []).append(span)
    assert [span.speaker for span in by_kind[SpanKind.DIALOGUE]] == ["user", "sana"]
    monologues = by_kind.get(SpanKind.MONOLOGUE, [])
    assert len(monologues) == 1
    assert monologues[0].speaker == "sana"
    assert "嘴上不承认" in monologues[0].raw_text


def test_annotations_and_rules_never_become_monologue(tmp_path: Path) -> None:
    _, _, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph("说明：全部状态仅对你生效；面对外人维持体面形象。"),
            heading("七、行为禁区（绝对不会出现的行为）"),
            list_item("不会无缘无故对你长时间冷暴力"),
            heading("八、输出规则"),
            list_item("句末不使用句号，可使用感叹号、问号"),
        ],
    )
    assert all(span.kind is not SpanKind.MONOLOGUE for span in roles.spans)
    assert all(
        span.kind is SpanKind.STATEMENT
        for span in roles.spans
        if span.speaker is None
    )
    assert all(span.speaker != "sana" for span in roles.spans if span.kind is SpanKind.STATEMENT)


def test_other_character_lines_are_not_attributed_to_sana(tmp_path: Path) -> None:
    _, _, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph(
                "示例\n"
                "你：理央又在闹了\n"
                "理央：才没有，是姐姐太吵\n"
                "纱凪：你别乱说"
            ),
        ],
    )
    speakers = [span.speaker for span in roles.spans if span.kind is SpanKind.DIALOGUE]
    assert "rio" in speakers
    sana_lines = [span for span in roles.spans if span.speaker == "sana" and span.kind is SpanKind.DIALOGUE]
    assert [span.raw_text for span in sana_lines] == ["纱凪：你别乱说"]


def test_long_dialogue_is_split_into_child_spans(tmp_path: Path) -> None:
    long_line = "纱凪：" + "这是一段很长的说明。" * (LONG_SPAN_CHARS // 6)
    _, _, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph(f"示例\n你：说吧\n{long_line}"),
        ],
    )
    parents = [span for span in roles.spans if span.raw_text.startswith("纱凪：")]
    children = [span for span in roles.spans if span.parent_span_id]
    assert len(parents) == 1
    assert children, "超长发言应拆成子片段"
    assert all(child.parent_span_id == parents[0].id for child in children)
    joined = "".join(child.raw_text for child in children)
    assert joined == parents[0].raw_text[len("纱凪：") :]


def test_short_answer_keeps_previous_link(tmp_path: Path) -> None:
    _, _, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph("示例\n你：今天累吗\n纱凪：嗯。\n你：那早点休息"),
        ],
    )
    dialogue = [span for span in roles.spans if span.kind is SpanKind.DIALOGUE]
    short = next(span for span in dialogue if span.raw_text.endswith("嗯。"))
    assert short.previous_id == dialogue[0].id
    assert dialogue[0].next_id == short.id


def test_duplicate_text_keeps_distinct_sources(tmp_path: Path) -> None:
    _, _, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph("示例\n你：在吗\n纱凪：在的。"),
            heading("二、核心性格（通用原生性格）"),
            paragraph("示例\n你：在吗\n纱凪：在的。"),
        ],
    )
    duplicates = [span for span in roles.spans if span.raw_text == "纱凪：在的。"]
    assert len(duplicates) == 2
    assert duplicates[0].source_start != duplicates[1].source_start
    assert duplicates[0].node_id != duplicates[1].node_id


def test_source_coverage_is_traceable(tmp_path: Path) -> None:
    read, parsed, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            paragraph("外貌细节正文一。"),
            blank(),
            heading("二、核心性格（通用原生性格）"),
            paragraph("通用性格正文二。"),
        ],
    )
    assert parsed.coverage["unmapped_char_count"] == 0
    assert roles.coverage["ratio"] > 0.9
    for span in roles.spans:
        assert read.body_text[span.source_start : span.source_end] == span.raw_text


def test_unclassifiable_input_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    read, parsed, roles = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            blank(),
        ],
    )
    assert roles.spans == ()
    assert roles.issues
    assert roles.issues[0]["reason_code"] == "no_source_spans"
    assert read.issues and read.issues[0]["reason_code"] == "blank_blocks_ignored"


def test_scope_does_not_cross_same_level_heading(tmp_path: Path) -> None:
    _, parsed, _ = _structure(
        tmp_path,
        [
            heading("一、基础档案"),
            heading("外貌细节"),
            paragraph("第一段内容"),
            heading("背景故事"),
            paragraph("第二段内容"),
            heading("二、核心性格（通用原生性格）"),
            paragraph("第三段内容"),
        ],
    )
    by_text = {node.raw_text: node for node in parsed.nodes}
    assert by_text["第一段内容"].heading_path == ("一、基础档案", "外貌细节")
    assert by_text["第二段内容"].heading_path == ("一、基础档案", "背景故事")
    assert by_text["第三段内容"].heading_path == ("二、核心性格（通用原生性格）",)


def test_list_items_inherit_scope_and_are_not_headings(tmp_path: Path) -> None:
    _, parsed, _ = _structure(
        tmp_path,
        [
            heading("七、行为禁区（绝对不会出现的行为・严格原版）"),
            list_item("不会无理由辱骂你"),
            list_item("不会一直保持炸毛状态"),
        ],
    )
    from sana_bot.ingest.role_splitter import is_heading_node

    negatives = [node for node in _nodes_of(parsed, ContentType.NEGATIVE) if not is_heading_node(node)]
    assert len(negatives) == 2
    assert all(node.list_level == 0 for node in negatives)
    assert all(node.field_type is FieldType.NEGATIVE for node in negatives)
    assert all(node.heading_path[-1].startswith("七、") for node in negatives)
    assert not any(item["title"] == "不会无理由辱骂你" for item in parsed.heading_tree)


def test_structure_hash_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    blocks = [heading("一、基础档案"), paragraph("外貌细节正文")]
    _, first, _ = _structure(tmp_path, blocks, name="a.docx")
    _, second, _ = _structure(tmp_path, blocks, name="a.docx")
    _, changed, _ = _structure(
        tmp_path, [heading("一、基础档案"), paragraph("不同的正文")], name="a.docx"
    )
    assert first.structure_hash == second.structure_hash
    assert first.structure_hash != changed.structure_hash


def test_ingest_service_persists_reviewable_structure(tmp_path: Path, database: Database) -> None:
    from sana_bot.config import RuntimeLayout
    from sana_bot.ingest.service import IngestService

    layout = RuntimeLayout(tmp_path / "runtime").ensure()
    source = build_docx(
        tmp_path / "fixture.docx",
        [
            heading("角色设定"),
            heading("一、基础档案"),
            heading("外貌细节"),
            paragraph("身形娇小玲珑。"),
            heading("八、输出规则"),
            list_item("句末不使用句号"),
        ],
    )
    service = IngestService(database, layout=layout)
    result = service.parse(source, tmp_path / "structures")
    assert result.ok
    assert result.structure_path is not None and result.structure_path.exists()
    assert result.report_path is not None and result.report_path.exists()

    document = service.load(result.structure_id or "")
    assert document.structure_hash == result.details["structure_hash"]
    assert document.review_status is ReviewStatus.PENDING

    report = service.review(result.structure_id or "")
    assert report.payload["roundtrip"]["ok"] is True
    assert report.payload["missing_chapters"], "缺少的章节必须显式列出"
    assert report.payload["chapter_coverage"]["一"] > 0
    assert report.payload["approval"] if "approval" in report.payload else True

    spans: tuple[SourceSpan, ...] = service.repository.load_spans(result.structure_id or "")
    assert spans


def test_approval_records_hashes(tmp_path: Path, database: Database) -> None:
    import json

    from sana_bot.config import RuntimeLayout
    from sana_bot.ingest.service import IngestService

    layout = RuntimeLayout(tmp_path / "runtime").ensure()
    source = build_docx(tmp_path / "fixture.docx", [heading("一、基础档案"), paragraph("正文")])
    service = IngestService(database, layout=layout)
    result = service.parse(source, tmp_path / "structures")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"rules": [{"id": "r1", "approved": False}]}), encoding="utf-8")

    args = type(
        "Args",
        (),
        {
            "structure": result.structure_id,
            "catalog": str(catalog),
            "reviewer": "maintainer",
            "runtime_dir": str(layout.root),
        },
    )()
    from sana_bot.cli_index import run_approve

    payload = run_approve(args)
    assert payload["structure_hash"] == result.details["structure_hash"]
    assert payload["still_unapproved"] == ["r1"]
    assert payload["catalog_hash"]

    approval = service.repository.approval(result.structure_id or "")
    assert approval is not None
    assert approval["reviewer"] == "maintainer"


@pytest.mark.parametrize("missing", ["source", "catalog", "reviewer"])
def test_approve_requires_inputs(tmp_path: Path, database: Database, missing: str) -> None:
    from sana_bot.errors import ConfigError

    args = type(
        "Args",
        (),
        {
            "structure": "s" if missing != "source" else "",
            "catalog": "" if missing == "catalog" else str(tmp_path / "missing.json"),
            "reviewer": "" if missing == "reviewer" else "maintainer",
            "runtime_dir": str(tmp_path / "runtime"),
        },
    )()
    from sana_bot.cli_index import run_approve

    with pytest.raises((ConfigError,)):
        run_approve(args)


def test_approve_refuses_catalog_for_other_structure(tmp_path: Path, database: Database) -> None:
    """批准必须绑定目标结构：catalog 声明的 structure_hash 不符时拒绝登记。"""
    import json

    from sana_bot.config import RuntimeLayout
    from sana_bot.errors import NotReadyError
    from sana_bot.ingest.service import IngestService

    layout = RuntimeLayout(tmp_path / "runtime").ensure()
    source = build_docx(tmp_path / "fixture.docx", [heading("一、基础档案"), paragraph("正文")])
    service = IngestService(database, layout=layout)
    result = service.parse(source, tmp_path / "structures")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {"structure_hash": "f" * 64, "rules": [{"id": "r1", "approved": True}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from sana_bot.cli_index import run_approve

    args = type(
        "Args",
        (),
        {
            "structure": result.structure_id,
            "catalog": str(catalog),
            "reviewer": "maintainer",
            "runtime_dir": str(layout.root),
        },
    )()
    with pytest.raises(NotReadyError) as info:
        run_approve(args)
    assert info.value.reason_code == "catalog_structure_mismatch"
    assert service.repository.approval(result.structure_id or "") is None


def test_approve_accepts_catalog_without_structure_hash(tmp_path: Path, database: Database) -> None:
    """catalog 未声明 structure_hash 时按目标结构登记，不因缺字段拒绝。"""
    import json

    from sana_bot.config import RuntimeLayout
    from sana_bot.ingest.service import IngestService

    layout = RuntimeLayout(tmp_path / "runtime").ensure()
    source = build_docx(tmp_path / "fixture.docx", [heading("一、基础档案"), paragraph("正文")])
    service = IngestService(database, layout=layout)
    result = service.parse(source, tmp_path / "structures")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"rules": []}, ensure_ascii=False), encoding="utf-8")

    from sana_bot.cli_index import run_approve

    payload = run_approve(
        type(
            "Args",
            (),
            {
                "structure": result.structure_id,
                "catalog": str(catalog),
                "reviewer": "maintainer",
                "runtime_dir": str(layout.root),
            },
        )()
    )
    assert payload["structure_hash"] == result.details["structure_hash"]
