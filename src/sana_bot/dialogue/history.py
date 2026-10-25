"""对话历史与资料可用性：按会话读取上下文、隔离用户、固定本轮 index_id。

资料不可用时只走明确的受控降级，不假称 ready 或资料命中；降级原因必须可诊断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.enums import RetrievalStage
from sana_bot.storage.delivery_repository import DeliveryRepository
from sana_bot.storage.index_repository import SnapshotRecord, StructureRepository
from sana_bot.storage.inbound_repository import InboundRepository

__all__ = ["ConversationHistory", "HistoryEntry", "MaterialAvailability"]

#: 单轮上下文最多带入的轮数（不含本轮）
DEFAULT_HISTORY_TURNS = 6
#: 供模型参考的最近资料片段上限
MAX_REFERENCE_UNITS = 8


@dataclass(frozen=True)
class HistoryEntry:
    role: str  # user / assistant
    text: str
    turn_id: str


@dataclass(frozen=True)
class ConversationHistory:
    entries: tuple[HistoryEntry, ...]
    conversation_id: str
    generation: int

    def as_messages(self) -> tuple[Mapping[str, str], ...]:
        return tuple({"role": entry.role, "content": entry.text} for entry in self.entries)

    def recent_user_texts(self, limit: int = 3) -> tuple[str, ...]:
        return tuple(
            entry.text for entry in self.entries if entry.role == "user"
        )[-limit:]


@dataclass(frozen=True)
class MaterialAvailability:
    """本轮资料可用性；``index_id`` 固定后本轮不再切换版本。"""

    available: bool
    index_id: str | None = None
    snapshot: SnapshotRecord | None = None
    reason_code: str | None = None
    notes: tuple[str, ...] = ()
    reply_threshold: float | None = None
    monologue_threshold: float | None = None
    degraded: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    #: 双格式标识；结构包额外冻结常驻、卡、策略与报告引用
    format_version: str = "semantic_v1"
    bundle_hash: str = ""
    policy_hash: str = ""
    catalog_hash: str = ""
    calibration_id: str | None = None
    report_hash: str = ""
    index: Any | None = None
    view: Any | None = None


class HistoryService:
    """读取上下文与资料可用性。"""

    def __init__(
        self,
        *,
        structures: StructureRepository,
        inbound: InboundRepository,
        deliveries: DeliveryRepository,
        packages: Any | None = None,
        store: Any | None = None,
        reports: Any | None = None,
    ) -> None:
        self.structures = structures
        self.inbound = inbound
        self.deliveries = deliveries
        self.packages = packages
        self.store = store
        self.reports = reports

    def history_for(
        self,
        conversation_id: str,
        *,
        limit: int = DEFAULT_HISTORY_TURNS,
        generation: int = 0,
    ) -> ConversationHistory:
        """按会话读取上下文；不同会话不会互相引用。"""
        turns = self.inbound.recent_turns(conversation_id, limit=limit)
        entries: list[HistoryEntry] = []
        for turn in turns:
            entries.append(HistoryEntry(role="user", text=turn.input_text, turn_id=turn.id))
            reply = self.deliveries.reply_text(turn.id)
            if reply:
                entries.append(HistoryEntry(role="assistant", text=reply, turn_id=turn.id))
        return ConversationHistory(
            entries=tuple(entries), conversation_id=conversation_id, generation=generation
        )

    def material(
        self,
        *,
        allow_degraded: bool = False,
    ) -> MaterialAvailability:
        """当前活动索引的可用性；不完整版本不能启用。"""
        snapshot = self.structures.active_snapshot()
        if snapshot is None:
            degraded = MaterialAvailability(
                available=False,
                reason_code="no_active_index",
                degraded=True,
                notes=("没有已激活的完整索引，本轮走受控降级",),
            )
            return degraded if allow_degraded else degraded
        if snapshot.is_structural:
            return self._structural_material(snapshot)
        reply = self.structures.calibration(snapshot.id, RetrievalStage.REPLY)
        if reply is None or reply.threshold is None:
            return MaterialAvailability(
                available=False,
                index_id=snapshot.id,
                snapshot=snapshot,
                reason_code="reply_calibration_missing",
                degraded=True,
                notes=("缺少 reply 阶段校准报告，资料暂不可用",),
            )
        monologue = self.structures.calibration(snapshot.id, RetrievalStage.MONOLOGUE)
        if snapshot.monologue_status.value != "enabled" or monologue is None:
            monologue_threshold = None
        else:
            monologue_threshold = monologue.threshold
        return MaterialAvailability(
            available=True,
            index_id=snapshot.id,
            snapshot=snapshot,
            reply_threshold=reply.threshold,
            monologue_threshold=monologue_threshold,
            notes=("本轮固定该 index_id",),
            details={
                "snapshot_hash": snapshot.snapshot_hash,
                "monologue_status": snapshot.monologue_status.value,
            },
        )

    def _structural_material(self, snapshot: SnapshotRecord) -> MaterialAvailability:
        """新格式：冻结常驻、完整卡、策略与已批准报告；缺任一项即不可用。"""
        if self.packages is None or self.store is None or self.reports is None:
            return MaterialAvailability(
                available=False,
                index_id=snapshot.id,
                snapshot=snapshot,
                reason_code="package_unavailable",
                degraded=True,
                format_version=snapshot.index_format_version,
                notes=("运行期缺少资料包仓储或向量装载器",),
            )
        structure_id = str(snapshot.manifest.get("structure_id") or "")
        try:
            view = self.packages.view(structure_id)
            index = self.store.load_structural(snapshot)
        except Exception as exc:  # noqa: BLE001 - 产物或绑定损坏必须受控失败
            return MaterialAvailability(
                available=False,
                index_id=snapshot.id,
                snapshot=snapshot,
                reason_code=getattr(exc, "reason_code", "package_unavailable"),
                degraded=True,
                format_version=snapshot.index_format_version,
                notes=(str(getattr(exc, "message", exc)),),
            )
        present = {item.resident_id for item in index.residents}
        missing = [item for item in view.config.resident_ids if item not in present]
        if missing:
            return MaterialAvailability(
                available=False,
                index_id=snapshot.id,
                snapshot=snapshot,
                reason_code="resident_missing",
                degraded=True,
                format_version=snapshot.index_format_version,
                notes=(f"缺少常驻材料：{missing}",),
            )
        reports = [
            item
            for item in self.reports.calibrations(snapshot.id, stage="reply")
            if item.snapshot_hash == snapshot.snapshot_hash
        ]
        approved = [item for item in reports if item.approved_at]
        calibration = approved[-1] if approved else (reports[-1] if reports else None)
        if calibration is None:
            return MaterialAvailability(
                available=False,
                index_id=snapshot.id,
                snapshot=snapshot,
                reason_code="package_calibration_missing",
                degraded=True,
                format_version=snapshot.index_format_version,
                notes=("缺少与本快照绑定的校准报告",),
            )
        return MaterialAvailability(
            available=True,
            index_id=snapshot.id,
            snapshot=snapshot,
            reply_threshold=calibration.threshold,
            monologue_threshold=None,
            notes=("本轮固定该 index_id 与 bundle_hash",),
            format_version=snapshot.index_format_version,
            bundle_hash=snapshot.bundle_hash or "",
            policy_hash=snapshot.retrieval_policy_hash,
            catalog_hash=str(index.catalog.get("catalog_hash", "")),
            calibration_id=calibration.id,
            report_hash=calibration.compute_hash(),
            index=index,
            view=view,
            details={
                "snapshot_hash": snapshot.snapshot_hash,
                "bundle_hash": snapshot.bundle_hash,
                "resident_ids": [item.resident_id for item in index.residents],
                "monologue_status": snapshot.monologue_status.value,
                "calibration_id": calibration.id,
                "report_hash": calibration.compute_hash(),
            },
        )

    def confirmed_context(self, turn_id: str) -> Sequence[str]:
        """该轮之前已确认送达的回复，用于诊断与上下文。"""
        row = self.inbound.turn_row(turn_id)
        if row is None:
            return ()
        return tuple(
            entry.text for entry in self.history_for(row.conversation_id).entries if entry.role == "assistant"
        )
