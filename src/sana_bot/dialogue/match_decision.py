"""低匹配决策：即时对话与独白分别采用各自已批准门槛。

``below_threshold`` / ``empty_candidates`` 在即时对话走疑惑兜底、在独白保持安静；
``no_material`` 与 ``processing_error`` 单独记录，不能混入真实低分案例。
"""

from __future__ import annotations

from dataclasses import dataclass

from sana_bot.enums import MatchDecision, RetrievalStage
from sana_bot.ports import RetrievalResult

__all__ = ["MatchOutcome", "decide_match"]


@dataclass(frozen=True)
class MatchOutcome:
    decision: MatchDecision
    usable: bool
    reason_code: str
    matched_score: float | None = None
    fallback_kind: str | None = None
    silent: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reason_code": self.reason_code,
            "matched_score": self.matched_score,
            "fallback_kind": self.fallback_kind,
            "silent": self.silent,
        }


def decide_match(result: RetrievalResult, *, stage: RetrievalStage = RetrievalStage.REPLY) -> MatchOutcome:
    """按阶段决定分支；等于门槛算命中，异常分数不会被伪装成低分。"""
    if result.decision is MatchDecision.PROCESSING_ERROR:
        return MatchOutcome(
            decision=MatchDecision.PROCESSING_ERROR,
            usable=False,
            reason_code=result.reason_code or "processing_error",
            silent=stage is RetrievalStage.MONOLOGUE,
        )
    if result.decision is MatchDecision.NO_MATERIAL or not result.candidates:
        decision = MatchDecision.EMPTY_CANDIDATES if result.candidates == () else result.decision
        if result.decision is MatchDecision.NO_MATERIAL and not result.candidates:
            decision = MatchDecision.NO_MATERIAL
        return MatchOutcome(
            decision=decision,
            usable=False,
            reason_code=(
                "no_material"
                if decision is MatchDecision.NO_MATERIAL
                else "empty_candidates"
            ),
            fallback_kind="confused" if stage is RetrievalStage.REPLY else None,
            silent=stage is RetrievalStage.MONOLOGUE,
        )
    threshold = result.threshold
    best = max(candidate.score for candidate in result.candidates)
    if threshold is None:
        # 未校准就不得启用该能力
        return MatchOutcome(
            decision=MatchDecision.PROCESSING_ERROR,
            usable=False,
            reason_code="threshold_missing",
            matched_score=best,
            silent=stage is RetrievalStage.MONOLOGUE,
        )
    if best < threshold:
        return MatchOutcome(
            decision=MatchDecision.BELOW_THRESHOLD,
            usable=False,
            reason_code="below_threshold",
            matched_score=best,
            fallback_kind="confused" if stage is RetrievalStage.REPLY else None,
            silent=stage is RetrievalStage.MONOLOGUE,
        )
    return MatchOutcome(
        decision=MatchDecision.MATCHED,
        usable=True,
        reason_code="matched",
        matched_score=best,
    )
