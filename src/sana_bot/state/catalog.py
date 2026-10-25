"""状态规则目录：从不可变结构提取 StateRule，并区分原文与推导。

- 恋人分支来源是第四章；非恋爱二级状态与主情绪映射属于推导，默认未批准。
- ``approved=false`` 的规则不能被 reducer 执行；文档明确的恋人限定硬限制始终生效。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sana_bot.enums import ContentType, PrimaryMood, Provenance, RelationshipStatus, Scene
from sana_bot.errors import ConfigError
from sana_bot.ingest.models import RuleNode, StructuredDocument, canonical_json
from sana_bot.ingest.package_models import STATE_TAGS
from sana_bot.state.event_models import ApprovedEventCatalog

__all__ = [
    "StateRule",
    "RecoveryRule",
    "RuleCatalog",
    "CATALOG_VERSION",
    "DEFAULT_EVENT_TAGS",
    "DERIVED_NON_ROMANTIC_STATES",
    "build_draft_catalog",
    "load_catalog",
    "catalog_from_package",
    "catalog_hash_of",
    "ROMANTIC_HARD_CONSTRAINTS",
]

CATALOG_VERSION = "state-catalog-1"

#: 首期批准的事件标签枚举；模型只能在这些标签内提议
DEFAULT_EVENT_TAGS: tuple[str, ...] = (
    "direct_praise",
    "gentle_gaze",
    "sudden_closeness",
    "sweet_talk",
    "teasing",
    "small_quarrel",
    "ignore_user",
    "user_with_other",
    "cold_treatment",
    "being_comforted",
    "misunderstanding",
    "being_scolded",
    "club_pressure",
    "family_pressure",
    "late_night",
    "relief",
    "steady_daily",
)

#: 非恋爱分支（依据第二章与输出规则推导；必须由提供者确认后才能执行）
DERIVED_NON_ROMANTIC_STATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("礼貌愉快（非恋爱）", PrimaryMood.HAPPY, ("polite_cheerful",)),
    ("动物话题温柔（非恋爱）", PrimaryMood.HAPPY, ("animal_gentle",)),
    ("被夸别扭害羞（非恋爱）", PrimaryMood.HAPPY, ("awkward_shy",)),
    ("不满直率（非恋爱）", PrimaryMood.ANGRY, ("blunt_dissatisfaction",)),
    ("被调侃羞恼（非恋爱）", PrimaryMood.ANGRY, ("teased_annoyance", "not_genuine_anger")),
    ("排斥轻浮言行（非恋爱）", PrimaryMood.ANGRY, ("reject_flirt",)),
    ("受挫沉默（非恋爱）", PrimaryMood.SAD, ("setback_silence",)),
    ("压力低落（非恋爱）", PrimaryMood.SAD, ("pressure_low",)),
    ("自尊防备（非恋爱）", PrimaryMood.SAD, ("pride_defense",)),
)

#: 文档明确、必须始终生效的恋人限定硬限制
ROMANTIC_HARD_CONSTRAINTS: tuple[str, ...] = (
    "不得命令用户不能与异性交流",
    "不得以小矛盾提分手",
    "不得无理由辱骂用户",
    "不得因恋爱就失去害羞与嘴硬",
)

#: 状态标题 → 主情绪与二级标签（映射属于推导，需核对）
_STATE_MOOD_MAP: tuple[tuple[str, PrimaryMood, tuple[str, ...]], ...] = (
    ("平稳常态", PrimaryMood.NEUTRAL, ("平稳常态",)),
    ("害羞局促", PrimaryMood.HAPPY, ("害羞局促",)),
    ("傲娇炸毛", PrimaryMood.ANGRY, ("傲娇炸毛", "not_genuine_anger")),
    ("轻度吃醋", PrimaryMood.ANGRY, ("轻度吃醋",)),
    ("中度吃醋", PrimaryMood.ANGRY, ("中度吃醋",)),
    ("重度吃醋", PrimaryMood.SAD, ("重度吃醋", "自卑失落")),
    ("委屈脆弱", PrimaryMood.SAD, ("委屈脆弱",)),
    ("依赖软萌", PrimaryMood.HAPPY, ("依赖软萌",)),
)

#: 触发标签推测：把原文触发词映射到有限事件标签（推导，需核对）
_TRIGGER_TAG_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("被直白夸奖", "被夸", "夸奖"), ("direct_praise",)),
    (("温柔凝视", "温柔地看"), ("gentle_gaze",)),
    (("被突然靠近", "靠近", "贴近"), ("sudden_closeness",)),
    (("肉麻情话", "情话", "告白"), ("sweet_talk",)),
    (("被过度调侃", "捉弄", "调侃"), ("teasing",)),
    (("轻微拌嘴", "拌嘴", "抬杠"), ("small_quarrel",)),
    (("忽略", "敷衍", "无视"), ("ignore_user",)),
    (("关注其他异性", "和别的异性", "对别人态度温和"), ("user_with_other",)),
    (("长期被冷落", "冷处理", "冷暴力"), ("cold_treatment",)),
    (("安抚", "偏爱", "陪伴"), ("being_comforted",)),
    (("被误会", "误会"), ("misunderstanding",)),
    (("被凶", "被骂"), ("being_scolded",)),
    (("社团", "社团压力"), ("club_pressure",)),
    (("家庭", "弟弟", "父亲"), ("family_pressure",)),
    (("深夜", "夜晚"), ("late_night",)),
    (("安全感", "安心"), ("relief",)),
    (("日常相处", "日常"), ("steady_daily",)),
)


@dataclass(frozen=True)
class StateRule:
    """一条经核对的状态转换规则。"""

    id: str
    label: str
    source_node_ids: tuple[str, ...]
    provenance: Provenance
    approved: bool
    relationship: str = "any"
    scene: str = "any"
    from_states: tuple[str, ...] = ()
    event_tags: tuple[str, ...] = ()
    to_primary: PrimaryMood = PrimaryMood.NEUTRAL
    to_tags: tuple[str, ...] = ()
    dominant_priority: int = 0
    exit_conditions: tuple[str, ...] = ()
    hard_constraints: tuple[str, ...] = ()
    evidence_texts: tuple[str, ...] = ()

    def qualifies(self, *, relationship: RelationshipStatus, scene: Scene) -> bool:
        """关系与场景先过滤：只要有限制就必须全部满足。"""
        if self.relationship == "romantic" and relationship is not RelationshipStatus.ROMANTIC:
            return False
        if self.relationship == "non_romantic" and relationship is not RelationshipStatus.NON_ROMANTIC:
            return False
        if self.scene == "private" and scene is not Scene.PRIVATE:
            return False
        if self.scene == "public" and scene is Scene.PRIVATE:
            # unknown 与 public 采用同样的保守表达
            return False
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "source_node_ids": list(self.source_node_ids),
            "provenance": self.provenance.value,
            "approved": self.approved,
            "relationship": self.relationship,
            "scene": self.scene,
            "from_states": list(self.from_states),
            "event_tags": list(self.event_tags),
            "to_primary": self.to_primary.value,
            "to_tags": list(self.to_tags),
            "dominant_priority": self.dominant_priority,
            "exit_conditions": list(self.exit_conditions),
            "hard_constraints": list(self.hard_constraints),
            "evidence_texts": list(self.evidence_texts),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> StateRule:
        return cls(
            id=str(payload["id"]),
            label=str(payload.get("label", "")),
            source_node_ids=tuple(payload.get("source_node_ids", ())),
            provenance=Provenance(payload.get("provenance", Provenance.SOURCE.value)),
            approved=bool(payload.get("approved", False)),
            relationship=str(payload.get("relationship", "any")),
            scene=str(payload.get("scene", "any")),
            from_states=tuple(str(item) for item in payload.get("from_states", ())),
            event_tags=tuple(str(item) for item in payload.get("event_tags", ())),
            to_primary=PrimaryMood(payload.get("to_primary", PrimaryMood.NEUTRAL.value)),
            to_tags=tuple(str(item) for item in payload.get("to_tags", ())),
            dominant_priority=int(payload.get("dominant_priority", 0)),
            exit_conditions=tuple(str(item) for item in payload.get("exit_conditions", ())),
            hard_constraints=tuple(str(item) for item in payload.get("hard_constraints", ())),
            evidence_texts=tuple(str(item) for item in payload.get("evidence_texts", ())),
        )


@dataclass(frozen=True)
class RecoveryRule:
    """已批准的恢复出口：安抚或安心事件让状态逐步缓和。"""

    event_tag: str
    from_states: tuple[str, ...]
    to_state: str
    note: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_tag": self.event_tag,
            "from_states": list(self.from_states),
            "to_state": self.to_state,
            "note": self.note,
        }


@dataclass(frozen=True)
class RuleCatalog:
    """规则目录；只有 ``approved`` 规则可被执行。"""

    version: str
    rules: tuple[StateRule, ...] = ()
    event_catalog: ApprovedEventCatalog = field(
        default_factory=lambda: ApprovedEventCatalog(version="events-v1", tags=DEFAULT_EVENT_TAGS)
    )
    structure_hash: str = ""
    recoveries: tuple[RecoveryRule, ...] = ()

    @property
    def approved_rules(self) -> tuple[StateRule, ...]:
        return tuple(rule for rule in self.rules if rule.approved)

    @property
    def pending_rules(self) -> tuple[StateRule, ...]:
        return tuple(rule for rule in self.rules if not rule.approved)

    @property
    def catalog_hash(self) -> str:
        return catalog_hash_of(self)

    @property
    def hard_constraints(self) -> tuple[str, ...]:
        """无论映射是否批准都必须生效的硬限制。"""
        collected: list[str] = list(ROMANTIC_HARD_CONSTRAINTS)
        for rule in self.rules:
            collected.extend(rule.hard_constraints)
        return tuple(dict.fromkeys(collected))

    def rules_for(
        self,
        *,
        relationship: RelationshipStatus,
        scene: Scene,
        include_unapproved: bool = False,
    ) -> tuple[StateRule, ...]:
        pool = self.rules if include_unapproved else self.approved_rules
        return tuple(rule for rule in pool if rule.qualifies(relationship=relationship, scene=scene))

    def labels(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(rule.label for rule in self.rules))

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "structure_hash": self.structure_hash,
            "event_catalog": self.event_catalog.to_payload(),
            "rules": [rule.to_payload() for rule in self.rules],
            "recoveries": [item.to_payload() for item in self.recoveries],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RuleCatalog:
        event_payload = payload.get("event_catalog") or {}
        tags = tuple(event_payload.get("tags") or DEFAULT_EVENT_TAGS)
        return cls(
            version=str(payload.get("version", CATALOG_VERSION)),
            rules=tuple(StateRule.from_payload(item) for item in payload.get("rules", ())),
            event_catalog=ApprovedEventCatalog(
                version=str(event_payload.get("version", "events-v1")),
                tags=tags,
                description=dict(event_payload.get("description", {})),
            ),
            structure_hash=str(payload.get("structure_hash", "")),
            recoveries=tuple(
                RecoveryRule(
                    event_tag=str(item["event_tag"]),
                    from_states=tuple(str(state) for state in item.get("from_states", ())),
                    to_state=str(item["to_state"]),
                    note=str(item.get("note", "")),
                )
                for item in payload.get("recoveries", ())
            ),
        )

    def with_approvals(self, approved_ids: Iterable[str]) -> RuleCatalog:
        """按维护者确认结果落地批准；未列出的保持未批准。"""
        wanted = set(approved_ids)
        rules = tuple(
            StateRule(
                id=rule.id,
                label=rule.label,
                source_node_ids=rule.source_node_ids,
                provenance=rule.provenance,
                approved=rule.id in wanted,
                relationship=rule.relationship,
                scene=rule.scene,
                from_states=rule.from_states,
                event_tags=rule.event_tags,
                to_primary=rule.to_primary,
                to_tags=rule.to_tags,
                dominant_priority=rule.dominant_priority,
                exit_conditions=rule.exit_conditions,
                hard_constraints=rule.hard_constraints,
                evidence_texts=rule.evidence_texts,
            )
            for rule in self.rules
        )
        return RuleCatalog(
            version=self.version,
            rules=rules,
            event_catalog=self.event_catalog,
            structure_hash=self.structure_hash,
            recoveries=self.recoveries,
        )


def catalog_hash_of(catalog: RuleCatalog) -> str:
    return hashlib.sha256(canonical_json(catalog.to_payload()).encode("utf-8")).hexdigest()


def load_catalog(path: Path) -> RuleCatalog:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"catalog 不可读：{path}（{exc}）") from exc
    if not isinstance(payload, Mapping):
        raise ConfigError("catalog 必须是对象")
    return RuleCatalog.from_payload(payload)


def build_draft_catalog(document: StructuredDocument, *, version: str = CATALOG_VERSION) -> RuleCatalog:
    """从不可变结构提取待审 catalog；推导映射默认未批准。"""
    from sana_bot.ingest.role_splitter import is_heading_node

    state_nodes = [
        node
        for node in document.typed_nodes
        if node.content_type is ContentType.STATE and node.heading_path and not is_heading_node(node)
    ]
    groups: dict[str, list[RuleNode]] = {}
    for node in state_nodes:
        title = node.heading_path[-1]
        if not any(needle in title for needle, _mood, _tags in _STATE_MOOD_MAP):
            # 章节标题与强度分级不是状态规则，不进入状态目录
            continue
        groups.setdefault(title, []).append(node)

    rules: list[StateRule] = []
    for title, nodes in groups.items():
        mood, tags, derived = _mood_for(title)
        triggers: list[str] = []
        exits: list[str] = []
        texts: list[str] = []
        for node in nodes:
            texts.append(node.raw_text)
            if node.field_type is not None and node.field_type.value == "trigger":
                triggers.extend(_hint_tags(node.raw_text))
            if node.field_type is not None and node.field_type.value == "recovery":
                exits.append(node.raw_text)
        relationship = "romantic"
        scene = "private" if any("独处" in node.raw_text or "独处" in title for node in nodes) else "any"
        rules.append(
            StateRule(
                id=f"state::{title}",
                label=title,
                source_node_ids=tuple(node.id for node in nodes),
                provenance=Provenance.DERIVED if derived else Provenance.SOURCE,
                approved=False,
                relationship=relationship,
                scene=scene,
                from_states=(),
                event_tags=tuple(dict.fromkeys(triggers)),
                to_primary=mood,
                to_tags=tags,
                dominant_priority=_priority_for(title),
                exit_conditions=tuple(exits),
                hard_constraints=(),
                evidence_texts=tuple(texts),
            )
        )

    chapter_two = [
        node
        for node in document.typed_nodes
        if node.heading_path and node.heading_path[0].startswith("二、")
    ]
    for label, mood, tags in DERIVED_NON_ROMANTIC_STATES:
        rules.append(
            StateRule(
                id=f"derived::{label}",
                label=label,
                source_node_ids=tuple(node.id for node in chapter_two[:1]),
                provenance=Provenance.DERIVED,
                approved=False,
                relationship="non_romantic",
                scene="any",
                event_tags=(),
                to_primary=mood,
                to_tags=tags,
                dominant_priority=10,
                exit_conditions=(),
                hard_constraints=(),
                evidence_texts=tuple(node.raw_text for node in chapter_two[:1]),
            )
        )

    return RuleCatalog(
        version=version,
        rules=tuple(rules),
        event_catalog=ApprovedEventCatalog(version="events-v1", tags=DEFAULT_EVENT_TAGS),
        structure_hash=document.structure_hash,
    )


def _mood_for(title: str) -> tuple[PrimaryMood, tuple[str, ...], bool]:
    for needle, mood, tags in _STATE_MOOD_MAP:
        if needle in title:
            return mood, tags, True
    return PrimaryMood.NEUTRAL, (), True


#: 主导表达优先级：越具体/越强烈的状态优先级越高
_PRIORITY_MAP: tuple[tuple[str, int], ...] = (
    ("平稳常态", 1),
    ("害羞局促", 5),
    ("傲娇炸毛", 6),
    ("轻度吃醋", 6),
    ("中度吃醋", 7),
    ("重度吃醋", 8),
    ("委屈脆弱", 8),
    ("依赖软萌", 9),
)


def _priority_for(title: str) -> int:
    for needle, priority in _PRIORITY_MAP:
        if needle in title:
            return priority
    return 5


def _hint_tags(text: str) -> list[str]:
    found: list[str] = []
    for needles, tags in _TRIGGER_TAG_HINTS:
        if any(needle in text for needle in needles):
            found.extend(tags)
    return found


def rules_by_event(
    catalog: RuleCatalog,
    event_tags: Sequence[str],
    *,
    relationship: RelationshipStatus,
    scene: Scene,
) -> tuple[StateRule, ...]:
    """按事件标签筛选合格规则；事件标签必须非空且全部命中。"""
    if not event_tags:
        return ()
    wanted = set(event_tags)
    candidates: list[StateRule] = []
    for rule in catalog.rules_for(relationship=relationship, scene=scene):
        if not rule.event_tags:
            continue
        if set(rule.event_tags) <= wanted:
            candidates.append(rule)
    return tuple(candidates)


def catalog_from_package(payload: Mapping[str, Any], *, version: str) -> RuleCatalog:
    """从批准的资料包状态目录构建可执行目录。

    四个主心情不变；二级标签表达六状态与吃醋三级，恢复出口显式列出。
    该目录由维护者核对的配置产生，因此规则视为已批准。
    """
    moods = tuple(str(item) for item in payload.get("primary_moods", ()))
    if moods and set(moods) != {mood.value for mood in PrimaryMood}:
        raise ConfigError(
            f"状态目录的主心情必须是四个固定值，实际：{sorted(moods)}",
            reason_code="package_scope_unmapped",
        )
    tags = tuple(str(item) for item in payload.get("event_tags", ())) or DEFAULT_EVENT_TAGS
    recoveries = tuple(
        RecoveryRule(
            event_tag=str(item["event_tag"]),
            from_states=tuple(str(state) for state in item.get("from_states", ())),
            to_state=str(item["to_state"]),
            note=str(item.get("note", "")),
        )
        for item in payload.get("recoveries", ())
    )
    exit_tags: dict[str, list[str]] = {}
    for recovery in recoveries:
        for state in recovery.from_states:
            exit_tags.setdefault(state, []).append(recovery.event_tag)

    rules: list[StateRule] = []
    for state in payload.get("states", ()):
        if not isinstance(state, Mapping):
            raise ConfigError("状态目录项必须是对象", reason_code="package_scope_unmapped")
        label = str(state.get("label", ""))
        tags_for_state = tuple(str(item) for item in state.get("tags", ()))
        unknown = [tag for tag in tags_for_state if tag not in STATE_TAGS]
        if unknown:
            raise ConfigError(
                f"状态 {label} 含未批准标签：{unknown}",
                reason_code="package_scope_unmapped",
                details={"state": label, "actual": unknown},
            )
        rules.append(
            StateRule(
                id=str(state.get("id") or label),
                label=label,
                source_node_ids=(str(state.get("card_id") or ""),),
                provenance=Provenance.SOURCE,
                approved=True,
                relationship=str(state.get("relationship", "romantic")),
                scene=str(state.get("scene", "any")),
                from_states=tuple(str(item) for item in state.get("from_states", ())),
                event_tags=tuple(str(item) for item in state.get("event_tags", ())),
                to_primary=PrimaryMood(str(state.get("primary", PrimaryMood.NEUTRAL.value))),
                to_tags=tags_for_state,
                dominant_priority=int(state.get("priority", 0)),
                exit_conditions=tuple(exit_tags.get(label, ())),
                hard_constraints=(),
                evidence_texts=(),
            )
        )
    unknown_tags = [tag for rule in rules for tag in rule.event_tags if tag not in tags]
    if unknown_tags:
        raise ConfigError(
            f"状态规则引用了未在事件目录登记的标签：{sorted(set(unknown_tags))}",
            reason_code="package_scope_unmapped",
            details={"actual": sorted(set(unknown_tags))},
        )
    return RuleCatalog(
        version=version,
        rules=tuple(rules),
        event_catalog=ApprovedEventCatalog(version=f"{version}-events", tags=tags),
        recoveries=recoveries,
    )
