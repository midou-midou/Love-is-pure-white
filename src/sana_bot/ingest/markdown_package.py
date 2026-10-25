"""受限 Markdown 资料包解析：显式清单、完整 H2 卡边界与示例说话者。

解析只做确定性文本处理：不归一化原文、不切分正文、不推断独白。
错误带文件、标题或卡 ID、行范围与期望/实际，绝不静默跳过必需卡。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sana_bot.ingest.package_models import (
    CHUNKING_STRATEGY_MARKDOWN,
    CARD_PROVENANCES,
    CARD_TYPES,
    PACKAGE_FORMAT_VERSION,
    STATE_TAGS,
    SOURCE_ROLES,
    CardCondition,
    CardProvenance,
    CardType,
    CharacterPackage,
    PackageConfig,
    PackageError,
    PackageIssue,
    ReferenceCard,
    ResidentMaterial,
    SourceFileVersion,
    SourceRange,
    SourceRole,
    SpeakerTurn,
    card_embedding_input,
    canonical_hash,
)

__all__ = [
    "METADATA_KEYS",
    "load_package_config",
    "parse_markdown_package",
    "parse_example_turns",
    "detect_provenance",
]

METADATA_KEYS: tuple[str, ...] = ("id", "type", "character", "source", "scope")

_CONFIG_KEYS = frozenset(
    {
        "format_version",
        "chunking_strategy",
        "character",
        "story_stage",
        "required_relationship",
        "source_root",
        "profile_version",
        "policy_version",
        "catalog_version",
        "included_files",
        "excluded_files",
        "resident_ids",
        "resident_sources",
        "expected_counts",
        "card_conditions",
        "state_catalog",
        "budgets",
        "retrieval_policy",
    }
)

#: 元数据行：``key: value``；只认受控键名，正文里的冒号不受影响
_METADATA_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)[:：][ \t]*(?P<value>.*)$")
_H2 = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_H1 = re.compile(r"^#\s+(?P<title>.+?)\s*$")
_SPEAKER_LINE = re.compile(r"^(?P<label>用户|角色)[：:][ \t]?(?P<text>.*)$")

_EXPECTED_TYPES: Mapping[SourceRole, tuple[CardType, ...]] = {
    SourceRole.CORE: (),
    SourceRole.OUTPUT: (),
    SourceRole.EMOTION: (CardType.EMOTION_POLICY, CardType.EMOTION_RULE),
    SourceRole.SCENE: (CardType.SCENE_RULE,),
    SourceRole.BACKGROUND: (
        CardType.BACKGROUND,
        CardType.APPEARANCE,
        CardType.PROFILE,
    ),
    SourceRole.EXAMPLE: (CardType.DIALOGUE_EXAMPLE,),
}


# --------------------------------------------------------------------------- 配置


def _fail(reason_code: str, message: str, **details: Any) -> PackageError:
    return PackageError(message, reason_code=reason_code, details=details)


def _require_mapping(payload: Mapping[str, Any], key: str, *, file: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise _fail(
            "package_metadata_invalid",
            f"配置字段 {key} 必须是对象（{file}）",
            field=key,
            expected="object",
            actual=type(value).__name__,
        )
    return value


def load_package_config(path: Path) -> PackageConfig:
    """读取并校验显式包配置；未知字段直接报错，不猜测意图。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail("package_file_missing", f"配置文件不可读：{path}（{exc}）", file=str(path)) from exc
    except json.JSONDecodeError as exc:
        raise _fail(
            "package_metadata_invalid",
            f"配置文件不是合法 JSON：{path}（{exc}）",
            file=str(path),
        ) from exc
    if not isinstance(raw, Mapping):
        raise _fail("package_metadata_invalid", "包配置必须是对象", file=str(path))
    unknown = sorted(set(raw) - _CONFIG_KEYS)
    if unknown:
        raise _fail(
            "package_metadata_invalid",
            f"包配置包含未知字段：{unknown}",
            file=str(path),
            expected=sorted(_CONFIG_KEYS),
            actual=unknown,
        )
    missing = sorted(
        {
            "format_version",
            "chunking_strategy",
            "character",
            "source_root",
            "included_files",
            "resident_ids",
        }
        - set(raw)
    )
    if missing:
        raise _fail(
            "package_metadata_invalid",
            f"包配置缺少必需字段：{missing}",
            file=str(path),
            expected=sorted(_CONFIG_KEYS),
            actual=missing,
        )
    if raw["format_version"] != PACKAGE_FORMAT_VERSION:
        raise _fail(
            "package_metadata_invalid",
            f"包配置 format_version 必须是 {PACKAGE_FORMAT_VERSION}",
            expected=PACKAGE_FORMAT_VERSION,
            actual=raw["format_version"],
        )
    if raw["chunking_strategy"] != CHUNKING_STRATEGY_MARKDOWN:
        raise _fail(
            "package_metadata_invalid",
            f"包配置 chunking_strategy 必须是 {CHUNKING_STRATEGY_MARKDOWN}",
            expected=CHUNKING_STRATEGY_MARKDOWN,
            actual=raw["chunking_strategy"],
        )

    included: list[tuple[str, SourceRole]] = []
    for ordinal, item in enumerate(raw["included_files"]):
        if not isinstance(item, Mapping) or set(item) != {"path", "role"}:
            raise _fail(
                "package_metadata_invalid",
                "included_files 的每一项必须只有 path 与 role",
                index=ordinal,
                actual=dict(item) if isinstance(item, Mapping) else item,
            )
        role_raw = str(item["role"])
        if role_raw not in SOURCE_ROLES:
            raise _fail(
                "package_metadata_invalid",
                f"未知来源角色：{role_raw}",
                expected=list(SOURCE_ROLES),
                actual=role_raw,
            )
        included.append((str(item["path"]), SourceRole(role_raw)))
    paths = [path for path, _role in included]
    if len(set(paths)) != len(paths):
        raise _fail("package_metadata_invalid", "included_files 中路径重复", actual=paths)

    excluded: list[str] = []
    for item in raw.get("excluded_files", ()):
        if not isinstance(item, str):
            raise _fail(
                "package_metadata_invalid",
                "excluded_files 的每一项必须是相对路径字符串",
                actual=item,
            )
        excluded.append(item)

    conditions: dict[str, CardCondition] = {}
    for card_id, payload in _require_mapping(raw, "card_conditions", file=str(path)).items():
        if not isinstance(payload, Mapping):
            raise _fail(
                "package_scope_unmapped",
                f"卡 {card_id} 的条件必须是对象",
                card_id=str(card_id),
            )
        unknown_condition = sorted(set(payload) - {"relationship", "scene", "medium", "state_tags", "card_states", "resident"})
        if unknown_condition:
            raise _fail(
                "package_scope_unmapped",
                f"卡 {card_id} 包含未知条件字段：{unknown_condition}",
                card_id=str(card_id),
                actual=unknown_condition,
            )
        condition = CardCondition(
            card_id=str(card_id),
            relationship=str(payload.get("relationship", "any")),
            scene=str(payload.get("scene", "any")),
            medium=str(payload.get("medium", "any")),
            state_tags=tuple(str(item) for item in payload.get("state_tags", ())),
            card_states=tuple(str(item) for item in payload.get("card_states", ())),
            resident=bool(payload.get("resident", False)),
        )
        condition.validate()
        conditions[condition.card_id] = condition

    residents = tuple(str(item) for item in raw.get("resident_ids", ()))
    if len(residents) != len(set(residents)):
        raise _fail("package_metadata_invalid", "resident_ids 重复", actual=list(residents))

    state_catalog = _require_mapping(raw, "state_catalog", file=str(path))
    for state in state_catalog.get("states", ()):
        if not isinstance(state, Mapping):
            raise _fail("package_scope_unmapped", "state_catalog.states 项必须是对象")
        for tag in state.get("tags", ()):
            if str(tag) not in STATE_TAGS:
                raise _fail(
                    "package_scope_unmapped",
                    f"状态目录包含未批准标签：{tag}",
                    expected=list(STATE_TAGS),
                    actual=str(tag),
                )

    config = PackageConfig(
        format_version=str(raw["format_version"]),
        chunking_strategy=str(raw["chunking_strategy"]),
        character=str(raw["character"]),
        story_stage=str(raw.get("story_stage", "")),
        required_relationship=str(raw.get("required_relationship", "romantic")),
        source_root=str(raw["source_root"]),
        profile_version=str(raw.get("profile_version", "package-profile-1")),
        policy_version=str(raw.get("policy_version", "package-policy-1")),
        catalog_version=str(raw.get("catalog_version", "sana-catalog")),
        included_files=tuple(included),
        excluded_files=tuple(excluded),
        resident_ids=residents,
        resident_sources={
            str(key): str(value)
            for key, value in _require_mapping(raw, "resident_sources", file=str(path)).items()
        },
        card_conditions=conditions,
        state_catalog=dict(state_catalog),
        budgets=dict(_require_mapping(raw, "budgets", file=str(path))),
        retrieval_policy=dict(_require_mapping(raw, "retrieval_policy", file=str(path))),
        expected_counts=dict(raw.get("expected_counts", {})),
    )
    config_hash = canonical_hash(config.to_payload())
    return PackageConfig(**{**config.__dict__, "config_hash": config_hash})


# --------------------------------------------------------------------------- 路径


def _resolve_source_root(config_path: Path, config: PackageConfig, project_root: Path | None) -> Path:
    if project_root is not None:
        root = Path(project_root).resolve()
    else:
        candidate = Path(config_path).resolve().parent.parent
        root = candidate if (candidate / config.source_root).exists() else Path.cwd().resolve()
    source_root = (root / config.source_root).resolve()
    if not source_root.is_dir():
        raise _fail(
            "package_file_missing",
            f"来源目录不存在：{source_root}",
            file=str(source_root),
            expected="directory",
        )
    return source_root


def _safe_relative(root: Path, relative: str) -> Path:
    """清单内路径不得逃逸来源根目录，也不接受绝对路径。"""
    candidate = Path(relative)
    if candidate.is_absolute():
        raise _fail(
            "source_path_escape",
            f"来源路径不允许是绝对路径：{relative}",
            actual=relative,
        )
    if any(part == ".." for part in candidate.parts):
        raise _fail("source_path_escape", f"来源路径不允许包含 ..：{relative}", actual=relative)
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _fail(
            "source_path_escape",
            f"来源路径逃逸出资料根目录：{relative}",
            expected=str(root),
            actual=str(resolved),
        ) from exc
    if resolved.is_symlink():
        raise _fail(
            "source_path_escape",
            f"来源路径不允许是符号链接：{relative}",
            actual=str(resolved),
        )
    return resolved


# --------------------------------------------------------------------------- 解析


@dataclass(frozen=True)
class _Block:
    heading: str
    heading_path: tuple[str, ...]
    metadata_start_line: int
    body_start_line: int
    body_end_line: int
    metadata: Mapping[str, str]
    body: str
    char_start: int
    char_end: int


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def detect_provenance(source_label: str) -> CardProvenance:
    """按来源说明判定性质：整理新增 = generated，改写 = adapted，其余 = source。"""
    label = source_label.strip()
    if "整理新增" in label:
        return CardProvenance.GENERATED
    if "整理改写" in label or ("整理" in label and "改写" in label):
        return CardProvenance.ADAPTED
    if label in CARD_PROVENANCES:
        return CardProvenance(label)
    if not label:
        return CardProvenance.SOURCE
    return CardProvenance.SOURCE


def parse_example_turns(
    body: str,
    *,
    body_start_line: int,
    char_start: int,
    file: str,
    heading: str,
) -> tuple[tuple[SpeakerTurn, ...], PackageIssue | None]:
    """按 ``用户：`` / ``角色：`` 切轮；续行沿用前一说话者，开头无说话者报歧义。"""
    lines = body.split("\n")
    offsets: list[int] = []
    cursor = char_start
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1

    turns: list[SpeakerTurn] = []
    current: dict[str, Any] | None = None
    saw_unlabelled_head = False

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        text = "\n".join(current["lines"])
        if not text.strip():
            current = None
            return
        turns.append(
            SpeakerTurn(
                speaker=current["speaker"],
                text=text,
                line_start=current["line_start"],
                line_end=current["line_end"],
                char_start=current["char_start"],
                char_end=current["char_end"],
            )
        )
        current = None

    for index, line in enumerate(lines):
        absolute_line = body_start_line + index
        match = _SPEAKER_LINE.match(line)
        if match is not None:
            flush()
            label = match.group("label")
            text = match.group("text")
            current = {
                "speaker": "user" if label == "用户" else "sana",
                "label": label,
                "lines": [text],
                "line_start": absolute_line,
                "line_end": absolute_line,
                "char_start": offsets[index] + len(label) + 1,
                "char_end": offsets[index] + len(line),
            }
            continue
        if current is None:
            if line.strip():
                saw_unlabelled_head = True
            continue
        current["lines"].append(line)
        if line.strip():
            current["line_end"] = absolute_line
            current["char_end"] = offsets[index] + len(line)
    flush()

    issue = None
    if saw_unlabelled_head:
        issue = PackageIssue(
            reason_code="example_speaker_ambiguous",
            message="示例正文开头存在无法确认说话者的内容，不猜为独白",
            file=file,
            heading=heading,
            line_start=body_start_line,
        )
    return tuple(turns), issue


def _parse_blocks(
    text: str,
    *,
    file: str,
    role: SourceRole,
    h1: str,
) -> tuple[list[_Block], list[PackageIssue], str]:
    """按 H2 切块并解析五字段元数据；返回块、问题与前言。"""
    lines = text.split("\n")
    offsets = _line_offsets(text)
    blocks: list[_Block] = []
    issues: list[PackageIssue] = []

    h2_positions = [
        (index, match.group("title"))
        for index, line in enumerate(lines)
        if (match := _H2.match(line)) is not None
    ]
    preamble_end = h2_positions[0][0] if h2_positions else len(lines)
    preamble = "\n".join(lines[1:preamble_end]).strip()

    for position, (line_index, title) in enumerate(h2_positions):
        next_index = h2_positions[position + 1][0] if position + 1 < len(h2_positions) else len(lines)
        cursor = line_index + 1
        while cursor < next_index and not lines[cursor].strip():
            cursor += 1
        metadata: dict[str, str] = {}
        metadata_start = cursor
        duplicates: list[str] = []
        while cursor < next_index:
            match = _METADATA_LINE.match(lines[cursor])
            if match is None:
                break
            key = match.group("key")
            if key not in METADATA_KEYS:
                issues.append(
                    PackageIssue(
                        reason_code="package_metadata_invalid",
                        message=f"未知元数据字段：{key}",
                        file=file,
                        heading=title,
                        line_start=cursor + 1,
                        expected=list(METADATA_KEYS),
                        actual=key,
                    )
                )
                cursor += 1
                continue
            if key in metadata:
                duplicates.append(key)
            metadata[key] = match.group("value").strip()
            cursor += 1

        missing = [key for key in METADATA_KEYS if key not in metadata]
        empty = [key for key, value in metadata.items() if not value]
        for problem, code, detail in (
            (missing, "缺字段", "missing"),
            (sorted(set(duplicates)), "重复字段", "duplicate"),
            (empty, "空字段", "empty"),
        ):
            if problem:
                issues.append(
                    PackageIssue(
                        reason_code="repeated_metadata_field" if detail == "duplicate" else "package_metadata_invalid",
                        message=f"卡元数据{code}：{problem}",
                        file=file,
                        heading=title,
                        line_start=metadata_start + 1,
                        char_start=offsets[min(metadata_start, len(lines) - 1)],
                        char_end=offsets[min(cursor, len(lines) - 1)],
                        expected=list(METADATA_KEYS),
                        actual=problem,
                    )
                )

        body_start = cursor
        while body_start < next_index and not lines[body_start].strip():
            body_start += 1
        body_end = next_index
        while body_end > body_start and not lines[body_end - 1].strip():
            body_end -= 1
        body = "\n".join(lines[body_start:body_end])
        blocks.append(
            _Block(
                heading=title,
                heading_path=(h1, title),
                metadata_start_line=metadata_start + 1,
                body_start_line=body_start + 1,
                body_end_line=body_end,
                metadata=dict(metadata),
                body=body,
                char_start=offsets[body_start] if body_start < len(lines) else len(text),
                char_end=offsets[body_end - 1] + len(lines[body_end - 1]) if body_end > body_start else offsets[body_start] if body_start < len(lines) else len(text),
            )
        )
    return blocks, issues, preamble


def _h1_title(text: str, fallback: str) -> str:
    for line in text.split("\n"):
        match = _H1.match(line)
        if match is not None:
            return match.group("title")
    return fallback


def parse_markdown_package(
    config_path: Path,
    *,
    project_root: Path | None = None,
    for_build: bool = False,
) -> CharacterPackage:
    """解析显式清单中的六份内容文件，产出常驻、完整卡与可核对问题清单。"""
    config_path = Path(config_path)
    config = load_package_config(config_path)
    root = _resolve_source_root(config_path, config, project_root)

    sources: list[SourceFileVersion] = []
    cards: list[ReferenceCard] = []
    residents: list[ResidentMaterial] = []
    issues: list[PackageIssue] = []
    text_by_path: dict[str, str] = {}

    for relative, role in config.included_files:
        path = _safe_relative(root, relative)
        if not path.is_file():
            raise _fail(
                "package_file_missing",
                f"必需文件缺失：{relative}",
                file=str(path),
                expected="file",
            )
        raw_bytes = path.read_bytes()
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail(
                "package_metadata_invalid",
                f"来源文件不是 UTF-8：{relative}",
                file=str(path),
                actual=str(exc),
            ) from exc
        text_by_path[relative] = text
        import hashlib

        byte_hash = hashlib.sha256(raw_bytes).hexdigest()
        source = SourceFileVersion(
            source_id=f"src::{relative}",
            relative_path=relative,
            role=role,
            byte_hash=byte_hash,
            raw_text=text,
            included=True,
            source_note=f"整理版 {relative}",
        )
        sources.append(source)
        h1 = _h1_title(text, role.value)

        if role in (SourceRole.CORE, SourceRole.OUTPUT):
            resident_id = "sana_core" if role is SourceRole.CORE else "sana_output"
            residents.append(
                ResidentMaterial(
                    resident_id=resident_id,
                    ordinal=0 if role is SourceRole.CORE else 1,
                    title=h1,
                    raw_text=text.strip("\n"),
                    source_id=source.source_id,
                    source_ranges=(
                        SourceRange(
                            file=relative,
                            heading_path=(h1,),
                            line_start=1,
                            line_end=len(text.rstrip("\n").split("\n")),
                            char_start=0,
                            char_end=len(text.rstrip("\n")),
                        ),
                    ),
                    content_hash=canonical_hash({"resident_id": resident_id, "raw_text": text.strip("\n")}),
                )
            )
            continue

        blocks, block_issues, preamble = _parse_blocks(text, file=relative, role=role, h1=h1)
        issues.extend(block_issues)
        for block in blocks:
            meta = block.metadata
            card_id = meta.get("id", "")
            if not card_id:
                # 元数据缺失已记录，无法形成可核对卡，不能静默跳过
                continue
            type_raw = meta.get("type", "")
            if type_raw not in CARD_TYPES:
                issues.append(
                    PackageIssue(
                        reason_code="unknown_card_type",
                        message=f"未知卡类型：{type_raw}",
                        file=relative,
                        heading=block.heading,
                        card_id=card_id,
                        expected=list(CARD_TYPES),
                        actual=type_raw,
                    )
                )
                continue
            card_type = CardType(type_raw)
            expected_types = _EXPECTED_TYPES[role]
            if expected_types and card_type not in expected_types:
                issues.append(
                    PackageIssue(
                        reason_code="package_metadata_invalid",
                        message=(
                            f"卡 {card_id} 的类型 {type_raw} 与来源用途 {role.value} 不符"
                        ),
                        file=relative,
                        heading=block.heading,
                        card_id=card_id,
                        expected=[item.value for item in expected_types],
                        actual=type_raw,
                    )
                )
                continue
            if not block.body.strip():
                issues.append(
                    PackageIssue(
                        reason_code="package_metadata_invalid",
                        message=f"卡 {card_id} 缺少正文",
                        file=relative,
                        heading=block.heading,
                        card_id=card_id,
                        line_start=block.body_start_line,
                    )
                )
                continue

            turns: tuple[SpeakerTurn, ...] = ()
            preface_refs: tuple[str, ...] = ()
            if card_type is CardType.DIALOGUE_EXAMPLE:
                turns, turn_issue = parse_example_turns(
                    block.body,
                    body_start_line=block.body_start_line,
                    char_start=block.char_start,
                    file=relative,
                    heading=block.heading,
                )
                if turn_issue is not None:
                    issues.append(turn_issue)
                preface_refs = ("示例仅供表达参考，不是已经发生的事件",)
                if preamble:
                    preface_refs = (*preface_refs, preamble.replace("\n", " ")[:200])

            resident_only = card_type is CardType.EMOTION_POLICY
            condition = config.card_conditions.get(card_id)
            effective = (
                condition.to_payload()
                if condition is not None
                else {"relationship": "any", "scene": "any", "medium": "any", "state_tags": []}
            )
            card = ReferenceCard(
                card_id=card_id,
                type=card_type,
                character=meta.get("character", ""),
                heading_path=block.heading_path,
                source_label=meta.get("source", ""),
                scope_raw=meta.get("scope", ""),
                raw_text=block.body,
                source_id=source.source_id,
                source_ranges=(
                    SourceRange(
                        file=relative,
                        heading_path=block.heading_path,
                        line_start=block.body_start_line,
                        line_end=block.body_end_line,
                        char_start=block.char_start,
                        char_end=block.char_end,
                    ),
                ),
                provenance=detect_provenance(meta.get("source", "")),
                speaker_turns=turns,
                constraint_refs=preface_refs,
                effective_conditions=effective,
                resident_only=resident_only,
            )
            card = replace(card, embedding_input=card_embedding_input(card))
            cards.append(card)
            if resident_only and card_id == "sana_emotion_general":
                residents.append(
                    ResidentMaterial(
                        resident_id="sana_emotion_general",
                        ordinal=2,
                        title=block.heading,
                        raw_text=block.body,
                        source_id=source.source_id,
                        source_ranges=card.source_ranges,
                        content_hash=canonical_hash(
                            {"resident_id": "sana_emotion_general", "raw_text": block.body}
                        ),
                    )
                )

    for relative in config.excluded_files:
        path = _safe_relative(root, relative)
        if not path.is_file():
            issues.append(
                PackageIssue(
                    reason_code="package_file_missing",
                    message=f"声明的排除文件不存在：{relative}",
                    file=str(path),
                )
            )
            continue
        import hashlib

        sources.append(
            SourceFileVersion(
                source_id=f"src::{relative}",
                relative_path=relative,
                role=SourceRole.MAINTENANCE,
                byte_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                raw_text="",
                included=False,
                excluded_reason="加载说明与维护记录，不进入角色生成内容",
                source_note=f"维护记录 {relative}",
            )
        )

    # 目录内未声明的文件：明确列为未纳入，绝不自动加载
    declared = {path for path, _role in config.included_files} | set(config.excluded_files)
    for path in sorted(root.glob("*.md")):
        if path.name not in declared:
            issues.append(
                PackageIssue(
                    reason_code="package_file_unlisted",
                    message=f"目录内存在未声明文件，未纳入本版本：{path.name}",
                    file=str(path),
                    actual=path.name,
                )
            )

    # 卡 ID 唯一性
    seen: dict[str, ReferenceCard] = {}
    duplicates: set[str] = set()
    for card in cards:
        if card.card_id in seen:
            duplicates.add(card.card_id)
        seen[card.card_id] = card
    for card_id in sorted(duplicates):
        issues.append(
            PackageIssue(
                reason_code="duplicate_card_id",
                message=f"同一包内卡 ID 重复：{card_id}",
                card_id=card_id,
                actual=card_id,
            )
        )

    # 每个卡都必须有批准条件映射
    for card in cards:
        if card.card_id not in config.card_conditions and not card.resident_only:
            issues.append(
                PackageIssue(
                    reason_code="package_scope_unmapped",
                    message=f"卡 {card.card_id} 缺少已批准的条件映射",
                    card_id=card.card_id,
                )
            )
    for card_id in sorted(set(config.card_conditions) - {card.card_id for card in cards}):
        issues.append(
            PackageIssue(
                reason_code="package_scope_unmapped",
                message=f"条件映射引用了不存在的卡：{card_id}",
                card_id=card_id,
            )
        )

    # 常驻材料必须齐全
    present = {item.resident_id: item for item in residents}
    missing_residents = [item for item in config.resident_ids if item not in present]
    if missing_residents:
        issues.append(
            PackageIssue(
                reason_code="resident_missing",
                message=f"必需常驻材料缺失：{missing_residents}",
                expected=list(config.resident_ids),
                actual=sorted(present),
            )
        )

    package = CharacterPackage(
        config=config,
        sources=tuple(sources),
        residents=tuple(sorted(residents, key=lambda item: item.ordinal)),
        cards=tuple(cards),
        issues=tuple(issues),
    )
    package = replace(
        package,
        structure_hash=package.compute_structure_hash(),
        source_set_hash=package.compute_source_set_hash(),
    )
    if for_build:
        ensure_package_complete(package)
    return package


def ensure_package_complete(package: CharacterPackage) -> None:
    """存在任何阻断问题时拒绝继续构建；错误带位置，不静默跳过。"""
    blocking = [
        issue
        for issue in package.issues
        if issue.reason_code
        in {
            "package_file_missing",
            "package_metadata_invalid",
            "repeated_metadata_field",
            "duplicate_card_id",
            "unknown_card_type",
            "package_scope_unmapped",
            "source_path_escape",
            "resident_missing",
            "example_speaker_ambiguous",
        }
    ]
    if blocking:
        first = blocking[0]
        raise PackageError(
            f"资料包存在 {len(blocking)} 个阻断问题，首个：{first.message}",
            reason_code=first.reason_code,
            details={
                "issues": [item.to_payload() for item in blocking],
                "file": first.file,
                "card_id": first.card_id,
                "line_start": first.line_start,
            },
        )

    counts = package.config.expected_counts
    if counts:
        by_type: dict[str, int] = {}
        for card in package.cards:
            if card.type in (CardType.BACKGROUND, CardType.APPEARANCE, CardType.PROFILE):
                key = "lore"
            elif card.type in (CardType.EMOTION_POLICY, CardType.EMOTION_RULE):
                key = "emotion"
            elif card.type is CardType.DIALOGUE_EXAMPLE:
                key = "example"
            elif card.type is CardType.SCENE_RULE:
                key = "scene"
            else:
                key = card.type.value
            by_type[key] = by_type.get(key, 0) + 1
        actual = {
            "resident": len(package.residents),
            "cards": len(package.cards),
            "encoded": len(package.encoded_cards),
            **by_type,
        }
        mismatches = {
            key: {"expected": value, "actual": actual.get(key)}
            for key, value in counts.items()
            if actual.get(key) != value
        }
        if mismatches:
            raise PackageError(
                f"资料计数与显式配置不一致：{mismatches}",
                reason_code="package_count_mismatch",
                details={"mismatches": mismatches, "actual": actual},
            )
