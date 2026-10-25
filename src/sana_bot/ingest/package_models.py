"""整理版角色资料包的领域模型（``structural_v2`` / ``markdown_cards_v1``）。

一个包固定六份来源文件、三项常驻材料与 26 张完整参考卡：状态总则常驻，
其余 25 张按条件引用。模型只描述数据，不做解析或 IO；哈希一律走规范 JSON，
不含创建时间、运行 ID 等非内容属性。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sana_bot.errors import ConfigError

__all__ = [
    "PACKAGE_FORMAT_VERSION",
    "CHUNKING_STRATEGY_MARKDOWN",
    "PACKAGE_PARSER_VERSION",
    "PACKAGE_PROFILE_VERSION",
    "PACKAGE_POLICY_VERSION",
    "CARD_TYPES",
    "STATE_TAGS",
    "CARD_PROVENANCES",
    "CardType",
    "SourceRole",
    "CardProvenance",
    "SourceRange",
    "SourceFileVersion",
    "ResidentMaterial",
    "SpeakerTurn",
    "ReferenceCard",
    "CardCondition",
    "ApprovedPackagePolicy",
    "PackageConfig",
    "CharacterPackage",
    "PackageIssue",
    "PackageError",
    "canonical_hash",
    "card_embedding_input",
    "is_reference_card",
]

PACKAGE_FORMAT_VERSION = "structural_v2"
CHUNKING_STRATEGY_MARKDOWN = "markdown_cards_v1"
PACKAGE_PARSER_VERSION = "sana-package-1.0.0"
PACKAGE_PROFILE_VERSION = "package-profile-1"
PACKAGE_POLICY_VERSION = "package-policy-1"

#: 允许的卡类型；``emotion_policy`` 只用于状态总则且仅常驻
CARD_TYPES: tuple[str, ...] = (
    "emotion_policy",
    "emotion_rule",
    "scene_rule",
    "background",
    "appearance",
    "profile",
    "dialogue_example",
)

#: 允许的来源角色
SOURCE_ROLES: tuple[str, ...] = (
    "core",
    "output",
    "emotion",
    "scene",
    "background",
    "example",
    "maintenance",
)

#: 卡性质的来源标记；示例不是已发生事件
CARD_PROVENANCES: tuple[str, ...] = ("source", "adapted", "generated")

#: 二级状态标签（批准目录的六状态与吃醋三级）
STATE_TAGS: tuple[str, ...] = (
    "平稳常态",
    "害羞局促",
    "傲娇炸毛",
    "轻度吃醋",
    "中度吃醋",
    "重度吃醋",
    "自卑失落",
    "委屈脆弱",
    "依赖软萌",
    "not_genuine_anger",
)

_CONDITION_ENUMS: Mapping[str, tuple[str, ...]] = {
    "relationship": ("any", "romantic", "non_romantic"),
    "scene": ("any", "public", "private"),
    "medium": ("any", "text", "co_present"),
}


class PackageError(ConfigError):
    """资料包内容或配置错误；位置与期望/实际一并给出，不泄露凭据。"""

    reason_code = "package_invalid"


class CardType(StrEnum):
    EMOTION_POLICY = "emotion_policy"
    EMOTION_RULE = "emotion_rule"
    SCENE_RULE = "scene_rule"
    BACKGROUND = "background"
    APPEARANCE = "appearance"
    PROFILE = "profile"
    DIALOGUE_EXAMPLE = "dialogue_example"


class SourceRole(StrEnum):
    CORE = "core"
    OUTPUT = "output"
    EMOTION = "emotion"
    SCENE = "scene"
    BACKGROUND = "background"
    EXAMPLE = "example"
    MAINTENANCE = "maintenance"


class CardProvenance(StrEnum):
    SOURCE = "source"
    ADAPTED = "adapted"
    GENERATED = "generated"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(payload: Any) -> str:
    """内容哈希：键排序、UTF-8、稳定列表顺序。"""
    import json

    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class PackageIssue:
    """一条可核对的资料问题；位置用文件、标题或卡 ID 与字符区间表达。"""

    reason_code: str
    message: str
    file: str = ""
    heading: str = ""
    card_id: str = ""
    line_start: int | None = None
    line_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    expected: Any = None
    actual: Any = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "file": self.file,
            "heading": self.heading,
            "card_id": self.card_id,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class SourceRange:
    """原文定位：1-based 行首尾 + Unicode 码点半开字符区间。"""

    file: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    char_start: int
    char_end: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "heading_path": list(self.heading_path),
            "line_start": self.line_start,
            "line_end": self.line_end,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass(frozen=True)
class SourceFileVersion:
    """一份来源文件的版本；原文不做 Unicode 归一化后再定位。"""

    source_id: str
    relative_path: str
    role: SourceRole
    byte_hash: str
    raw_text: str
    included: bool = True
    excluded_reason: str = ""
    source_note: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "role": self.role.value,
            "byte_hash": self.byte_hash,
            "raw_text": self.raw_text,
            "included": self.included,
            "excluded_reason": self.excluded_reason,
            "source_note": self.source_note,
        }


@dataclass(frozen=True)
class ResidentMaterial:
    """每轮完整加载的常驻材料；不参与向量竞争。"""

    resident_id: str
    ordinal: int
    title: str
    raw_text: str
    source_id: str
    source_ranges: tuple[SourceRange, ...] = ()
    required: bool = True
    content_hash: str = ""
    token_count: int = 0

    def compute_hash(self) -> str:
        return canonical_hash(
            {
                "resident_id": self.resident_id,
                "ordinal": self.ordinal,
                "title": self.title,
                "raw_text": self.raw_text,
                "source_id": self.source_id,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "resident_id": self.resident_id,
            "ordinal": self.ordinal,
            "title": self.title,
            "raw_text": self.raw_text,
            "source_id": self.source_id,
            "source_ranges": [item.to_payload() for item in self.source_ranges],
            "required": self.required,
            "content_hash": self.content_hash,
            "token_count": self.token_count,
        }


@dataclass(frozen=True)
class SpeakerTurn:
    """示例中的一轮发言；续行沿用前一说话者。"""

    speaker: str
    text: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "text": self.text,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass(frozen=True)
class ReferenceCard:
    """一张完整参考卡：标识、类型、来源、条件与完整正文，不拆散。"""

    card_id: str
    type: CardType
    character: str
    heading_path: tuple[str, ...]
    source_label: str
    scope_raw: str
    raw_text: str
    source_id: str
    source_ranges: tuple[SourceRange, ...] = ()
    provenance: CardProvenance = CardProvenance.SOURCE
    speaker_turns: tuple[SpeakerTurn, ...] = ()
    constraint_refs: tuple[str, ...] = ()
    effective_conditions: Mapping[str, Any] = field(default_factory=dict)
    embedding_input: str = ""
    input_hash: str = ""
    token_count: int = 0
    resident_only: bool = False

    @property
    def encoded(self) -> bool:
        return is_reference_card(self)

    def to_payload(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "type": self.type.value,
            "character": self.character,
            "heading_path": list(self.heading_path),
            "source_label": self.source_label,
            "scope_raw": self.scope_raw,
            "raw_text": self.raw_text,
            "source_id": self.source_id,
            "source_ranges": [item.to_payload() for item in self.source_ranges],
            "provenance": self.provenance.value,
            "speaker_turns": [turn.to_payload() for turn in self.speaker_turns],
            "constraint_refs": list(self.constraint_refs),
            "effective_conditions": dict(self.effective_conditions),
            "embedding_input": self.embedding_input,
            "input_hash": self.input_hash,
            "token_count": self.token_count,
            "resident_only": self.resident_only,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReferenceCard:
        return cls(
            card_id=str(payload["card_id"]),
            type=CardType(payload["type"]),
            character=str(payload.get("character", "")),
            heading_path=tuple(payload.get("heading_path", ())),
            source_label=str(payload.get("source_label", "")),
            scope_raw=str(payload.get("scope_raw", "")),
            raw_text=str(payload.get("raw_text", "")),
            source_id=str(payload.get("source_id", "")),
            source_ranges=tuple(
                SourceRange(
                    file=str(item["file"]),
                    heading_path=tuple(item.get("heading_path", ())),
                    line_start=int(item["line_start"]),
                    line_end=int(item["line_end"]),
                    char_start=int(item["char_start"]),
                    char_end=int(item["char_end"]),
                )
                for item in payload.get("source_ranges", ())
            ),
            provenance=CardProvenance(payload.get("provenance", "source")),
            speaker_turns=tuple(
                SpeakerTurn(
                    speaker=str(turn["speaker"]),
                    text=str(turn["text"]),
                    line_start=int(turn["line_start"]),
                    line_end=int(turn["line_end"]),
                    char_start=int(turn["char_start"]),
                    char_end=int(turn["char_end"]),
                )
                for turn in payload.get("speaker_turns", ())
            ),
            constraint_refs=tuple(payload.get("constraint_refs", ())),
            effective_conditions=dict(payload.get("effective_conditions", {})),
            embedding_input=str(payload.get("embedding_input", "")),
            input_hash=str(payload.get("input_hash", "")),
            token_count=int(payload.get("token_count", 0)),
            resident_only=bool(payload.get("resident_only", False)),
        )


def is_reference_card(card: ReferenceCard) -> bool:
    """状态总则只常驻、不编码；其余卡都是可检索的完整参考卡。"""
    return not card.resident_only and card.type is not CardType.EMOTION_POLICY


@dataclass(frozen=True)
class CardCondition:
    """一张卡已核对的适用条件；值必须来自受控枚举。"""

    card_id: str
    relationship: str = "any"
    scene: str = "any"
    medium: str = "any"
    state_tags: tuple[str, ...] = ()
    card_states: tuple[str, ...] = ()
    resident: bool = False

    def validate(self) -> None:
        for name, allowed in _CONDITION_ENUMS.items():
            value = getattr(self, name)
            if value not in allowed:
                raise ConfigError(
                    f"卡 {self.card_id} 的 {name} 取值非法：{value}",
                    reason_code="package_scope_unmapped",
                    details={"card_id": self.card_id, "field": name, "expected": list(allowed), "actual": value},
                )
        unknown = [tag for tag in self.state_tags if tag not in STATE_TAGS]
        if unknown:
            raise ConfigError(
                f"卡 {self.card_id} 的二级状态标签未在批准目录内：{sorted(unknown)}",
                reason_code="package_scope_unmapped",
                details={"card_id": self.card_id, "actual": sorted(unknown)},
            )

    def accepts(
        self,
        *,
        relationship: str,
        scene: str,
        medium: str,
        state_tags: Sequence[str] = (),
    ) -> bool:
        """硬条件：只要有限制就必须全部满足；unknown 不满足任何明确限制。"""
        if self.relationship != "any" and self.relationship != relationship:
            return False
        if self.scene != "any":
            # public-only 只接受明确 public；private-only 只接受明确 private
            if scene not in ("public", "private") or self.scene != scene:
                return False
        if self.medium != "any" and self.medium != medium:
            return False
        if self.state_tags:
            return bool(set(self.state_tags) & set(state_tags))
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "relationship": self.relationship,
            "scene": self.scene,
            "medium": self.medium,
            "state_tags": list(self.state_tags),
            "card_states": list(self.card_states),
            "resident": self.resident,
        }


@dataclass(frozen=True)
class ApprovedPackagePolicy:
    """维护者核对的逐卡条件、状态目录与预算；批准后冻结。"""

    profile_version: str
    policy_version: str
    catalog_version: str
    card_conditions: Mapping[str, CardCondition]
    state_catalog: Mapping[str, Any]
    budget: Mapping[str, Any]
    retrieval_policy: Mapping[str, Any]
    approved_by: str = ""
    approved_at: str | None = None

    def condition_for(self, card_id: str) -> CardCondition | None:
        return self.card_conditions.get(card_id)

    def to_payload(self) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "policy_version": self.policy_version,
            "catalog_version": self.catalog_version,
            "card_conditions": {
                card_id: condition.to_payload()
                for card_id, condition in sorted(self.card_conditions.items())
            },
            "state_catalog": dict(self.state_catalog),
            "budget": dict(self.budget),
            "retrieval_policy": dict(self.retrieval_policy),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }

    @property
    def policy_hash(self) -> str:
        payload = self.to_payload()
        payload.pop("approved_by", None)
        payload.pop("approved_at", None)
        payload.pop("catalog_version", None)
        return canonical_hash(payload)

    @property
    def catalog_hash(self) -> str:
        return canonical_hash(
            {"version": self.catalog_version, "state_catalog": dict(self.state_catalog)}
        )


@dataclass(frozen=True)
class PackageConfig:
    """显式资料包配置；未知字段报错，不自动扩展文件清单。"""

    format_version: str
    chunking_strategy: str
    character: str
    source_root: str
    included_files: tuple[tuple[str, SourceRole], ...]
    excluded_files: tuple[str, ...]
    resident_ids: tuple[str, ...]
    resident_sources: Mapping[str, str]
    card_conditions: Mapping[str, CardCondition]
    state_catalog: Mapping[str, Any]
    budgets: Mapping[str, Any]
    retrieval_policy: Mapping[str, Any]
    expected_counts: Mapping[str, Any] = field(default_factory=dict)
    story_stage: str = ""
    required_relationship: str = "romantic"
    profile_version: str = PACKAGE_PROFILE_VERSION
    policy_version: str = PACKAGE_POLICY_VERSION
    catalog_version: str = "sana-state-catalog-v2"
    config_hash: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "chunking_strategy": self.chunking_strategy,
            "character": self.character,
            "story_stage": self.story_stage,
            "required_relationship": self.required_relationship,
            "source_root": self.source_root,
            "profile_version": self.profile_version,
            "policy_version": self.policy_version,
            "catalog_version": self.catalog_version,
            "included_files": [
                {"path": path, "role": role.value} for path, role in self.included_files
            ],
            "excluded_files": list(self.excluded_files),
            "resident_ids": list(self.resident_ids),
            "resident_sources": dict(sorted(self.resident_sources.items())),
            "card_conditions": {
                card_id: condition.to_payload()
                for card_id, condition in sorted(self.card_conditions.items())
            },
            "state_catalog": dict(self.state_catalog),
            "expected_counts": dict(self.expected_counts),
            "budgets": dict(self.budgets),
            "retrieval_policy": dict(self.retrieval_policy),
        }

    def policy(self, *, approved_by: str = "", approved_at: str | None = None) -> ApprovedPackagePolicy:
        return ApprovedPackagePolicy(
            profile_version=self.profile_version,
            policy_version=self.policy_version,
            catalog_version=self.catalog_version,
            card_conditions=self.card_conditions,
            state_catalog=self.state_catalog,
            budget=self.budgets,
            retrieval_policy=self.retrieval_policy,
            approved_by=approved_by,
            approved_at=approved_at,
        )


@dataclass(frozen=True)
class CharacterPackage:
    """一次解析/构建产出的完整资料包；不可变且可重算哈希。"""

    config: PackageConfig
    sources: tuple[SourceFileVersion, ...]
    residents: tuple[ResidentMaterial, ...]
    cards: tuple[ReferenceCard, ...]
    issues: tuple[PackageIssue, ...] = ()
    structure_hash: str = ""
    source_set_hash: str = ""

    @property
    def encoded_cards(self) -> tuple[ReferenceCard, ...]:
        return tuple(card for card in self.cards if card.encoded)

    @property
    def resident_ids(self) -> tuple[str, ...]:
        return tuple(item.resident_id for item in self.residents)

    def card_by_id(self, card_id: str) -> ReferenceCard | None:
        for card in self.cards:
            if card.card_id == card_id:
                return card
        return None

    def included_sources(self) -> tuple[SourceFileVersion, ...]:
        return tuple(source for source in self.sources if source.included)

    def compute_source_set_hash(self) -> str:
        return canonical_hash(
            [
                {"path": source.relative_path, "role": source.role.value, "byte_hash": source.byte_hash}
                for source in self.included_sources()
            ]
        )

    def compute_structure_hash(self) -> str:
        return canonical_hash(
            {
                "character": self.config.character,
                "format_version": self.config.format_version,
                "chunking_strategy": self.config.chunking_strategy,
                "residents": [
                    {
                        "resident_id": item.resident_id,
                        "ordinal": item.ordinal,
                        "content_hash": item.content_hash,
                    }
                    for item in self.residents
                ],
                "cards": [
                    {
                        "card_id": card.card_id,
                        "type": card.type.value,
                        "heading_path": list(card.heading_path),
                        "raw_text": card.raw_text,
                        "source_id": card.source_id,
                        "provenance": card.provenance.value,
                        "resident_only": card.resident_only,
                    }
                    for card in self.cards
                ],
                "excluded": sorted(self.config.excluded_files),
            }
        )

    def compute_profile_hash(self) -> str:
        return canonical_hash(
            {
                "profile_version": self.config.profile_version,
                "parser_version": PACKAGE_PARSER_VERSION,
                "included_files": [
                    {"path": path, "role": role.value} for path, role in self.config.included_files
                ],
                "expected_counts": dict(self.config.expected_counts),
                "embedding_input_format": "title+type+scope+body",
            }
        )


def card_embedding_input(card: ReferenceCard) -> str:
    """固定编码输入：标题、类型、scope 与完整正文，不截断。"""
    return "\n".join(
        [
            f"标题：{card.heading_path[-1] if card.heading_path else card.card_id}",
            f"类型：{card.type.value}",
            f"scope：{card.scope_raw}",
            "",
            card.raw_text,
        ]
    )


def iter_payloads(items: Iterable[Any]) -> list[dict[str, Any]]:
    return [item.to_payload() for item in items]
