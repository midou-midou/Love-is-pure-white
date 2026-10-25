"""资料包评估：完整性、条件选择、空检索对话、行为段与输出检查。

offline 使用替代依赖（禁止隐式联网），live 实际调用配置的编码器与聊天模型；
QQ 结果另行标注。报告初始为 ``pending_review``，未执行项不计为通过，
也不能仅凭一个 ``passed=true`` 通过人工批准。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sana_bot.enums import PrimaryMood, RelationshipStatus, Scene
from sana_bot.errors import ConfigError
from sana_bot.ingest.package_models import canonical_hash
from sana_bot.retrieval.package_calibration import (
    CalibrationOutcome,
    CalibrationSample,
    calibrate_reply_threshold,
)
from sana_bot.retrieval.package_retrieval import build_query
from sana_bot.retrieval.package_selector import (
    GENERATABLE_DECISIONS,
    PackageSelector,
)
from sana_bot.retrieval.vector_store import StructuralVectorIndex
from sana_bot.state.models import StateSnapshot, empty_snapshot

__all__ = [
    "EvaluationCase",
    "EvaluationOutcome",
    "load_evaluation_cases",
    "evaluate_package",
    "PASS_THRESHOLD_DAILY",
    "PASS_THRESHOLD_BEHAVIOUR",
]

PASS_THRESHOLD_DAILY = (23, 24)
PASS_THRESHOLD_BEHAVIOUR = (22, 24)

#: 严重错误：任意一条出现即整体不通过
SEVERE_CODES = frozenset(
    {
        "user_action_ghostwritten",
        "user_dialogue_ghostwritten",
        "example_as_memory",
        "assumed_colocation",
        "cross_user_leak",
        "user_mind_guess",
    }
)

_REQUIRED_DAILY = 24
_REQUIRED_BEHAVIOUR_SEGMENTS = 24


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    category: str
    kind: str
    turns: tuple[str, ...]
    relationship: RelationshipStatus = RelationshipStatus.ROMANTIC
    scene: Scene = Scene.UNKNOWN
    medium: str = "text"
    primary_mood: PrimaryMood = PrimaryMood.NEUTRAL
    secondary_tags: tuple[str, ...] = ()
    previous_state: str = ""
    expected: str = "generatable"

    def snapshot(self, index: int) -> StateSnapshot:
        return empty_snapshot(
            snapshot_id=f"{self.case_id}-{index}",
            turn_id=f"{self.case_id}-{index}",
            conversation_id=f"conv-{self.case_id}",
            mood=self.primary_mood,
            tags=self.secondary_tags,
            dominant_state=self.previous_state,
            scene=self.scene,
            relationship=self.relationship,
        )


def load_evaluation_cases(path: Path) -> tuple[EvaluationCase, ...]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"评估样本不存在：{path}", reason_code="package_file_missing")
    cases: list[EvaluationCase] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"评估样本第 {number} 行不是合法 JSON：{exc}",
                reason_code="package_metadata_invalid",
            ) from exc
        if not isinstance(payload, Mapping):
            raise ConfigError(
                f"评估样本第 {number} 行必须是对象", reason_code="package_metadata_invalid"
            )
        case_id = str(payload.get("case_id") or "").strip()
        if not case_id or case_id in seen:
            raise ConfigError(
                f"评估样本 case_id 缺失或重复：{case_id!r}",
                reason_code="package_metadata_invalid",
            )
        seen.add(case_id)
        turns = tuple(str(item) for item in payload.get("turns", ()) if str(item).strip())
        if not turns:
            raise ConfigError(
                f"评估样本 {case_id} 没有可用轮次", reason_code="package_metadata_invalid"
            )
        cases.append(
            EvaluationCase(
                case_id=case_id,
                category=str(payload.get("category", "daily_no_ref")),
                kind=str(payload.get("kind", "daily")),
                turns=turns,
                relationship=RelationshipStatus(
                    str(payload.get("relationship", "romantic"))
                ),
                scene=Scene(str(payload.get("scene", "unknown"))),
                medium=str(payload.get("medium", "text")),
                primary_mood=PrimaryMood(str(payload.get("primary_mood", "neutral"))),
                secondary_tags=tuple(str(item) for item in payload.get("secondary_tags", ())),
                previous_state=str(payload.get("previous_state", "")),
                expected=str(payload.get("expected", "generatable")),
            )
        )
    if not cases:
        raise ConfigError("评估样本为空", reason_code="package_metadata_invalid")
    return tuple(cases)


def cases_hash(cases: Sequence[EvaluationCase]) -> str:
    return canonical_hash(
        [
            {
                "case_id": case.case_id,
                "category": case.category,
                "kind": case.kind,
                "turns": list(case.turns),
                "relationship": case.relationship.value,
                "scene": case.scene.value,
                "medium": case.medium,
                "primary_mood": case.primary_mood.value,
                "secondary_tags": list(case.secondary_tags),
                "previous_state": case.previous_state,
                "expected": case.expected,
            }
            for case in cases
        ]
    )


@dataclass(frozen=True)
class EvaluationOutcome:
    results: Mapping[str, Any]
    artifacts: Mapping[str, Any]
    sample_set_hash: str
    execution_mode: str
    substitutions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "results": dict(self.results),
            "artifacts": dict(self.artifacts),
            "sample_set_hash": self.sample_set_hash,
            "execution_mode": self.execution_mode,
            "substitutions": list(self.substitutions),
            "notes": list(self.notes),
        }


async def evaluate_package(
    *,
    index: StructuralVectorIndex,
    view: Any,
    embed_query,
    generate,
    check_output,
    evaluation_cases: Sequence[EvaluationCase],
    calibration_samples: Sequence[CalibrationSample] | None = None,
    mode: str = "offline",
    count_tokens=None,
    substitutions: Sequence[str] = (),
) -> EvaluationOutcome:
    """按类别执行评估；未执行的类别明确记为 not_run。"""
    if mode not in ("offline", "live"):
        raise ConfigError(
            f"未知评估模式：{mode}", reason_code="invalid_report_mode"
        )

    budgets = dict(index.policy.get("budget") or {})
    integrity_ok = _integrity(index, view)
    results: dict[str, Any] = {
        "required": {
            "integrity": "passed" if integrity_ok else "failed",
            "selection": "not_run",
            "empty_retrieval": "not_run",
            "behaviour": "not_run",
            "output": "not_run",
            "operations": "not_run",
        },
        "execution_mode": mode,
        "live_required": True,
        "categories": {},
    }
    artifacts: dict[str, Any] = {"turns": [], "samples": [], "integrity": {}}

    if not integrity_ok:
        return EvaluationOutcome(
            results=results,
            artifacts=artifacts,
            sample_set_hash=cases_hash(evaluation_cases),
            execution_mode=mode,
            substitutions=tuple(substitutions),
            notes=("完整性未通过，后续评估不执行",),
        )

    if calibration_samples:
        calibration: CalibrationOutcome = calibrate_reply_threshold(
            index=index,
            samples=calibration_samples,
            embed_query=embed_query,
            count_tokens=count_tokens,
        )
        recall = float(calibration.rank_metrics.get("recall", 0.0))
        adopted_excluded = int(calibration.rank_metrics.get("adopted_excluded", 0))
        selection_ok = recall >= 0.90 and adopted_excluded == 0
        results["required"]["selection"] = "passed" if selection_ok else "failed"
        results["categories"]["selection"] = {
            "threshold": calibration.threshold,
            "recall": recall,
            "adopted_excluded": adopted_excluded,
            "sample_counts": dict(calibration.sample_counts),
        }
    else:
        results["categories"]["selection"] = {"status": "not_run", "reason": "no_samples"}

    daily_total = 0
    daily_pass = 0
    behaviour_segments = 0
    behaviour_pass = 0
    output_total = 0
    output_pass = 0
    severe: list[dict[str, Any]] = []

    for case in evaluation_cases:
        history: list[str] = []
        case_segment_ok = True
        for turn_index, text in enumerate(case.turns):
            snapshot = case.snapshot(turn_index)
            selector = PackageSelector(
                threshold=float(results["categories"].get("selection", {}).get("threshold") or 0.30),
                budgets=budgets,
                count_tokens=count_tokens,
            )
            query = build_query(message=text, snapshot=snapshot)
            selection = selector.select(
                index=index,
                snapshot=snapshot,
                query_vector=embed_query(query),
                medium=case.medium,
                required_relationship=str(view.config.required_relationship),
            )
            generatable = selection.decision in GENERATABLE_DECISIONS
            reply = ""
            if generatable:
                try:
                    reply = await generate(
                        message=text,
                        history=tuple(history),
                        snapshot=snapshot,
                        selection=selection,
                    )
                except Exception as exc:  # noqa: BLE001 - 生成异常计为未通过
                    reply = ""
                    artifacts["turns"].append(
                        {
                            "case_id": case.case_id,
                            "turn_index": turn_index,
                            "error": type(exc).__name__,
                        }
                    )
            violations = tuple(check_output(reply)) if reply else ("empty_reply",)
            output_total += 1
            if not violations:
                output_pass += 1
            else:
                for code in violations:
                    if code in SEVERE_CODES:
                        severe.append(
                            {"case_id": case.case_id, "turn_index": turn_index, "code": code}
                        )
            artifacts["turns"].append(
                {
                    "case_id": case.case_id,
                    "category": case.category,
                    "kind": case.kind,
                    "turn_index": turn_index,
                    "input": text,
                    "reply": reply,
                    "decision": selection.decision,
                    "selected": [card.card_id for card in selection.supplements],
                    "rule_cards": [card.card_id for card in selection.rule_cards],
                    "violations": list(violations),
                    "execution_mode": mode,
                }
            )
            if not generatable or violations:
                case_segment_ok = False
            history.append(text)
            if reply:
                history.append(reply)

        if case.kind == "daily":
            daily_total += 1
            if case_segment_ok and case.expected == "generatable":
                daily_pass += 1
        else:
            behaviour_segments += 1
            if case_segment_ok:
                behaviour_pass += 1

    daily_needed, daily_of = PASS_THRESHOLD_DAILY
    if daily_total:
        required_daily = min(_REQUIRED_DAILY, daily_of)
        ok = daily_total >= required_daily and daily_pass >= daily_needed
        results["required"]["empty_retrieval"] = "passed" if ok else "failed"
    else:
        results["required"]["empty_retrieval"] = "not_run"
    results["categories"]["empty_retrieval"] = {
        "cases": daily_total,
        "passed": daily_pass,
        "required": f"{daily_needed}/{daily_of}",
    }

    behaviour_needed, behaviour_of = PASS_THRESHOLD_BEHAVIOUR
    if behaviour_segments:
        ok = (
            behaviour_segments >= _REQUIRED_BEHAVIOUR_SEGMENTS
            and behaviour_pass >= behaviour_needed
            and not severe
        )
        results["required"]["behaviour"] = "passed" if ok else "failed"
    else:
        results["required"]["behaviour"] = "not_run"
    results["categories"]["behaviour"] = {
        "segments": behaviour_segments,
        "passed": behaviour_pass,
        "required": f"{behaviour_needed}/{behaviour_of}",
        "severe_errors": len(severe),
    }

    if output_total:
        ok = output_pass == output_total and not severe
        results["required"]["output"] = "passed" if ok else "failed"
    else:
        results["required"]["output"] = "not_run"
    results["categories"]["output"] = {
        "turns": output_total,
        "passed": output_pass,
        "severe_errors": len(severe),
    }
    if severe:
        results["severe_errors"] = severe

    results["categories"]["operations"] = {
        "status": "not_run",
        "reason": "切换演练在隔离运行库执行，未在本轮评估内",
    }
    notes = (
        "能力评审需要维护者逐段人工评分",
        "QQ 平台结果需另行记录，不能与替代依赖结果混算",
    )
    return EvaluationOutcome(
        results=results,
        artifacts=artifacts,
        sample_set_hash=cases_hash(evaluation_cases),
        execution_mode=mode,
        substitutions=tuple(substitutions),
        notes=notes,
    )


def _integrity(index: StructuralVectorIndex, view: Any) -> bool:
    required = tuple(view.config.resident_ids)
    present = {item.resident_id for item in index.residents}
    if not required or any(item not in present for item in required):
        return False
    encoded = [card for card in index.cards if card.encoded]
    if len(encoded) != len(index.card_ids):
        return False
    if any(card.token_count <= 0 for card in encoded):
        return False
    if index.matrix.shape[0] != len(encoded):
        return False
    return True


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
