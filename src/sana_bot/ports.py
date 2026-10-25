"""服务端口（协议）与统一结果类型。

领域流程只依赖这里的协议，因此可以用 fake clock/RNG/model/transport 独立验证。
所有结果都带 ``reason_code`` 与 ``correlation_id``，便于关联诊断记录；
失败结果不伪装成空结果。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sana_bot.enums import (
    DeliveryStatus,
    MatchDecision,
    PrimaryMood,
    RelationshipStatus,
    RetrievalStage,
    Scene,
)

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期循环导入
    from sana_bot.state.event_models import ApprovedEventCatalog, StateEvent, StateEventProposal
    from sana_bot.state.models import StateSnapshot

__all__ = [
    "ServiceIssue",
    "NormalizedEvent",
    "Clock",
    "RandomSource",
    "ModelBudget",
    "ModelRequest",
    "ModelTextResult",
    "ModelJsonResult",
    "ModelPort",
    "IngestResult",
    "RetrievedCandidate",
    "RetrievalResult",
    "SelectionEvidence",
    "FrozenTurnContext",
    "StateEventResult",
    "StateReducerResult",
    "DialogueDraft",
    "OutputDecision",
    "DeliveryResult",
    "SettlementResult",
    "MonologueResult",
    "IngestService",
    "Retriever",
    "StateEventExtractor",
    "StateReducer",
    "DialogueService",
    "OutputPolicy",
    "QQTransport",
    "AffectionService",
    "MonologueService",
]


@dataclass(frozen=True)
class NormalizedEvent:
    """归一化后的应用事件；外层事件 id 与消息 id 分开保存。"""

    event_key: str
    app_id: str
    user_key: str
    message_id: str
    event_time: datetime
    received_at: datetime
    text: str = ""
    payload_hash: str = ""
    platform_event_id: str | None = None
    supported: bool = True
    scene_hint: str | None = None


@dataclass(frozen=True)
class ServiceIssue:
    """可诊断的问题条目。"""

    reason_code: str
    message: str
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- 基础端口


@runtime_checkable
class Clock(Protocol):
    """可注入时钟；fake clock 让等待与到期判定可重现。"""

    def now(self) -> float:
        """单调递增秒数，用于超时与预算。"""

    def wall(self) -> float:
        """墙上时间秒数（UTC epoch），用于事件时间与聊天日。"""


@runtime_checkable
class RandomSource(Protocol):
    """可注入随机源；每个机会只抽一次。"""

    def random(self) -> float:
        """返回 [0, 1) 的浮点数。"""


@dataclass
class ModelBudget:
    """共享的模型处理预算；事件提取/生成/审核/改写共用同一预算。"""

    total_seconds: float
    started_at: float | None = None

    def start(self, now: float) -> None:
        if self.started_at is None:
            self.started_at = now

    def reset(self) -> None:
        """开始新一轮时复位：预算按轮共享，不跨轮累计。"""
        self.started_at = None

    def remaining(self, now: float) -> float:
        if self.started_at is None:
            return self.total_seconds
        return max(0.0, self.total_seconds - (now - self.started_at))

    def expired(self, now: float) -> bool:
        return self.remaining(now) <= 0.0

    def timeout_for(self, now: float) -> float:
        return self.remaining(now)


@dataclass(frozen=True)
class ModelRequest:
    """一次模型调用的输入；只包含已校验文本，不含凭据。"""

    messages: tuple[Mapping[str, str], ...]
    temperature: float | None = None
    max_tokens: int | None = None
    json_schema_name: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ModelTextResult:
    ok: bool
    text: str = ""
    model: str = ""
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelJsonResult:
    ok: bool
    payload: Mapping[str, Any] | None = None
    raw_text: str = ""
    model: str = ""
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelPort(Protocol):
    """聊天模型端口；只返回 content，不透传推理、工具结果或流式草稿。"""

    async def complete_text(self, request: ModelRequest, budget: ModelBudget) -> ModelTextResult: ...

    async def complete_json(self, request: ModelRequest, budget: ModelBudget) -> ModelJsonResult: ...


# --------------------------------------------------------------------------- 资料导入


@dataclass(frozen=True)
class IngestResult:
    """``index parse`` 的结果：不可变结构与核对材料的位置。"""

    ok: bool
    structure_id: str | None = None
    structure_path: Path | None = None
    report_path: Path | None = None
    node_count: int = 0
    source_hash: str = ""
    issues: tuple[ServiceIssue, ...] = ()
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class IngestService(Protocol):
    def parse(self, source: Path, output_dir: Path) -> IngestResult: ...


# --------------------------------------------------------------------------- 检索


@dataclass(frozen=True)
class RetrievedCandidate:
    """合格的语义单元候选；不含被硬过滤掉的片段。"""

    unit_id: str
    score: float
    source_span_ids: tuple[str, ...] = ()
    topic: str = ""
    context_links: tuple[str, ...] = ()
    speaker_labels: tuple[str, ...] = ()
    primary_moods: tuple[str, ...] = ()
    secondary_tags: tuple[str, ...] = ()
    relationship: str = "any"
    scene: str = "any"
    content_types: tuple[str, ...] = ()
    text: str = ""
    source_node_ids: tuple[str, ...] = ()
    conditions: Mapping[str, Any] = field(default_factory=dict)
    #: 检索候选的类型/来源键；语义路径留空，绝不把 parent_unit_id 冒充组键
    group_key: str = ""
    matched_field_keys: tuple[str, ...] = ()
    matched_leaf_keys: tuple[str, ...] = ()
    hydrated_fields: tuple[str, ...] = ()
    ancestor_constraints: tuple[Mapping[str, Any], ...] = ()
    effective_conditions: Mapping[str, Any] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    omitted_optional_fields: tuple[str, ...] = ()
    context_status: str = "ok"
    snapshot_hash: str = ""


@dataclass(frozen=True)
class RetrievalResult:
    stage: RetrievalStage
    decision: MatchDecision
    candidates: tuple[RetrievedCandidate, ...] = ()
    threshold: float | None = None
    index_id: str | None = None
    snapshot_hash: str = ""
    reason_code: str | None = None
    correlation_id: str | None = None
    notes: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Retriever(Protocol):
    async def retrieve(
        self,
        *,
        query: str,
        snapshot: StateSnapshot,
        index_id: str,
        stage: RetrievalStage,
        correlation_id: str | None = None,
    ) -> RetrievalResult: ...


# --------------------------------------------------------------------------- 包冻结上下文


@dataclass(frozen=True)
class SelectionEvidence:
    """一张候选卡的选取证据：条件、分数、选中/排除原因。"""

    card_id: str
    card_type: str
    condition_ok: bool
    selected: bool
    score: float | None = None
    reason: str = ""
    state_match: bool = False
    budget_excluded: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "card_type": self.card_type,
            "condition_ok": self.condition_ok,
            "selected": self.selected,
            "score": self.score,
            "reason": self.reason,
            "state_match": self.state_match,
            "budget_excluded": self.budget_excluded,
        }


@dataclass(frozen=True)
class FrozenTurnContext:
    """本轮固定的完整包引用；一经冻结不得再读 active 覆盖版本。"""

    turn_id: str
    user_id: str
    conversation_id: str
    generation: int = 0
    index_id: str = ""
    snapshot_hash: str = ""
    bundle_hash: str = ""
    policy_hash: str = ""
    catalog_hash: str = ""
    calibration_id: str | None = None
    report_hash: str = ""
    resident_ids: tuple[str, ...] = ()
    relationship_status: RelationshipStatus = RelationshipStatus.NON_ROMANTIC
    scene: Scene = Scene.UNKNOWN
    medium: str = "text"
    location: str = "unknown"
    cohabitation: str = "unknown"
    nickname: str | None = None
    previous_state: str = ""
    effective_state: str = ""
    transition_evidence: tuple[Mapping[str, Any], ...] = ()
    selections: tuple[SelectionEvidence, ...] = ()
    decision: str = ""
    reason_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "generation": self.generation,
            "index_id": self.index_id,
            "snapshot_hash": self.snapshot_hash,
            "bundle_hash": self.bundle_hash,
            "policy_hash": self.policy_hash,
            "catalog_hash": self.catalog_hash,
            "calibration_id": self.calibration_id,
            "report_hash": self.report_hash,
            "resident_ids": list(self.resident_ids),
            "relationship_status": self.relationship_status.value,
            "scene": self.scene.value,
            "medium": self.medium,
            "location": self.location,
            "cohabitation": self.cohabitation,
            "nickname": self.nickname,
            "previous_state": self.previous_state,
            "effective_state": self.effective_state,
            "transition_evidence": [dict(item) for item in self.transition_evidence],
            "selections": [item.to_payload() for item in self.selections],
            "decision": self.decision,
            "reason_code": self.reason_code,
        }


# --------------------------------------------------------------------------- 状态事件与归约


@dataclass(frozen=True)
class StateEventResult:
    """事件提取结果：只允许有限标签与原文证据。"""

    ok: bool
    events: tuple[StateEvent, ...] = ()
    proposal: StateEventProposal | None = None
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class StateEventExtractor(Protocol):
    async def extract(
        self,
        *,
        message: str,
        source_message_id: str,
        catalog: ApprovedEventCatalog,
        correlation_id: str | None = None,
    ) -> StateEventResult: ...


@dataclass(frozen=True)
class StateReducerResult:
    ok: bool
    primary_mood: PrimaryMood = PrimaryMood.NEUTRAL
    secondary_tags: tuple[str, ...] = ()
    dominant_state: str = ""
    applied_rule_ids: tuple[str, ...] = ()
    transition_evidence: tuple[Mapping[str, Any], ...] = ()
    conflict: str | None = None
    reason_code: str | None = None
    correlation_id: str | None = None


@runtime_checkable
class StateReducer(Protocol):
    def reduce(
        self,
        *,
        current: StateSnapshot,
        events: Sequence[StateEvent],
        relationship: RelationshipStatus,
        scene: Scene,
        correlation_id: str | None = None,
    ) -> StateReducerResult: ...


# --------------------------------------------------------------------------- 对话与输出


@dataclass(frozen=True)
class DialogueDraft:
    """待发送草稿；绝不直接流给 QQ。"""

    kind: str  # normal / fallback / error
    text: str
    source_candidates: tuple[str, ...] = ()
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class DialogueService(Protocol):
    async def respond(
        self,
        *,
        message: str,
        history: Sequence[Mapping[str, Any]],
        snapshot: StateSnapshot,
        index_id: str | None,
        correlation_id: str | None = None,
    ) -> DialogueDraft: ...


@dataclass(frozen=True)
class OutputDecision:
    approved: bool
    text: str = ""
    violations: tuple[str, ...] = ()
    rewritten: bool = False
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class OutputPolicy(Protocol):
    async def review(
        self,
        *,
        draft: DialogueDraft,
        snapshot: StateSnapshot,
        stage: RetrievalStage | None = None,
        correlation_id: str | None = None,
    ) -> OutputDecision: ...


# --------------------------------------------------------------------------- 发送


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    platform_message_id: str | None = None
    msg_seq: int | None = None
    submit_at: float | None = None
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class QQTransport(Protocol):
    async def send(
        self,
        *,
        content: str,
        source_msg_id: str,
        msg_seq: int,
        user_key: str,
        correlation_id: str | None = None,
    ) -> DeliveryResult: ...


# --------------------------------------------------------------------------- 好感度与独白


@dataclass(frozen=True)
class SettlementResult:
    applied: bool
    delta: float = 0.0
    score_after: float = 0.0
    relationship_after: RelationshipStatus = RelationshipStatus.NON_ROMANTIC
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class AffectionService(Protocol):
    def settle_delivery(self, *, delivery_id: str, correlation_id: str | None = None) -> SettlementResult: ...


@dataclass(frozen=True)
class MonologueResult:
    outcome: str  # sent / skipped / cancelled / failed / uncertain
    text: str = ""
    skip_reason: str | None = None
    delivery_id: str | None = None
    reason_code: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class MonologueService(Protocol):
    async def evaluate(
        self,
        *,
        opportunity_id: str,
        clock: Clock | None = None,
        rng: RandomSource | None = None,
        correlation_id: str | None = None,
    ) -> MonologueResult: ...


# --------------------------------------------------------------------------- 类型别名


Handler = Callable[..., Awaitable[None]]
