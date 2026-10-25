"""``index`` 的新格式命令：资料包导入、核对、批准、构建、校准、评估、启用与恢复。

所有命令沿用既有退出码（0/2/3/4）；管理操作仅本地。启用与恢复共用同一个
目标验证器：先只读重算封存与报告，再在短事务内比较 expected-active 并写审计。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sana_bot.dialogue.substitute import SubstituteChatModel
from sana_bot.enums import IndexStatus, RetrievalStage
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.package_evaluation import evaluate_package, load_evaluation_cases
from sana_bot.ingest.package_service import (
    build_package,
    parse_package,
    verify_sealed_bundle,
)
from sana_bot.retrieval.package_calibration import (
    calibrate_reply_threshold,
    load_calibration_samples,
)
from sana_bot.retrieval.vector_store import VectorStore
from sana_bot.runtime import RuntimeContext, build_embedding_backend, open_context
from sana_bot.storage.index_repository import new_id
from sana_bot.storage.package_repository import (
    PackageCalibrationReport,
    PackageRegressionReport,
    PackageRepository,
    PackageReportRepository,
)
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = [
    "run_parse_package",
    "run_review_package",
    "run_approve_package",
    "run_build_package",
    "run_calibrate_package",
    "run_approve_calibration",
    "run_approve_calibration_command",
    "run_evaluate_package",
    "run_approve_evaluation",
    "activate_package",
    "rollback_package",
    "structure_is_package",
    "index_is_package",
]


def _packages(context: RuntimeContext) -> PackageRepository:
    return PackageRepository(context.database)


def _reports(context: RuntimeContext) -> PackageReportRepository:
    return PackageReportRepository(context.database)


def _store(context: RuntimeContext) -> VectorStore:
    return VectorStore(structures=context.structures)


def structure_is_package(context: RuntimeContext, structure_id: str | None) -> bool:
    if not structure_id:
        return False
    return any(
        item["id"] == structure_id for item in _packages(context).list_structures()
    )


def index_is_package(context: RuntimeContext, index_id: str | None) -> bool:
    if not index_id:
        return False
    try:
        return context.structures.snapshot(index_id).is_structural
    except NotReadyError:
        return False


# --------------------------------------------------------------------------- 导入


def run_parse_package(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(getattr(args, "source", "") or "")
    if not str(config_path):
        raise ConfigError("index parse --format markdown-package 需要 --source CONFIG")
    context = open_context(args)
    try:
        outcome = parse_package(
            config_path,
            output_dir=Path(args.output),
            project_root=None,
        )
        return {
            "structure_id": outcome.structure_id,
            "format_version": "structural_v2",
            "structure_hash": outcome.structure_hash,
            "source_set_hash": outcome.source_set_hash,
            "profile_hash": outcome.profile_hash,
            "policy_hash": outcome.policy_hash,
            "catalog_hash": outcome.catalog_hash,
            "resident_count": outcome.resident_count,
            "card_counts": dict(outcome.card_counts),
            "excluded_files": list(outcome.excluded_files),
            "issues": list(outcome.issues),
            "structure_path": str(outcome.structure_path),
            "report_path": str(outcome.report_path),
            "cards_path": str(outcome.cards_path),
            "approval_status": "pending",
            "note": "解析成功只表示结构可核对；需人工核对并执行 index approve",
        }
    finally:
        context.close()


def _blocking_issues(issues: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    blocking = {
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
    return [issue for issue in issues if str(issue.get("reason_code")) in blocking]


def persist_package_structure(
    context: RuntimeContext, config_path: Path, *, output_dir: Path, project_root: Path | None = None
) -> dict[str, Any]:
    """解析并登记结构（供 approve/build 使用）；阻断问题直接失败。"""
    from sana_bot.ingest.markdown_package import parse_markdown_package

    outcome = parse_package(
        config_path, output_dir=output_dir, project_root=project_root
    )
    blocking = _blocking_issues(outcome.issues)
    if blocking:
        raise NotReadyError(
            f"资料包存在 {len(blocking)} 个阻断问题，不能登记为可用结构",
            reason_code=str(blocking[0].get("reason_code") or "package_metadata_invalid"),
            details={"issues": blocking[:10]},
        )
    package = parse_markdown_package(config_path, project_root=project_root)
    _packages(context).save_package(package, structure_id=outcome.structure_id)
    return {
        "structure_id": outcome.structure_id,
        "format_version": "structural_v2",
        "structure_hash": outcome.structure_hash,
        "source_set_hash": outcome.source_set_hash,
        "profile_hash": outcome.profile_hash,
        "policy_hash": outcome.policy_hash,
        "catalog_hash": outcome.catalog_hash,
        "resident_count": outcome.resident_count,
        "card_counts": dict(outcome.card_counts),
        "excluded_files": list(outcome.excluded_files),
        "issues": list(outcome.issues),
        "structure_path": str(outcome.structure_path),
        "report_path": str(outcome.report_path),
        "cards_path": str(outcome.cards_path),
        "approval_status": "pending",
        "note": "解析成功只表示结构可核对；需人工核对并执行 index approve",
    }


# --------------------------------------------------------------------------- 核对/批准


def run_review_package(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    packages = _packages(context)
    structure_id = getattr(args, "structure", None)
    index_payload: dict[str, Any] = {}
    if not structure_id:
        snapshot = context.structures.snapshot(args.index)
        structure_id = str(snapshot.manifest.get("structure_id") or "")
        index_payload = snapshot.to_payload()
    view = packages.view(structure_id)
    approval = packages.approval(structure_id)
    return {
        "structure_id": structure_id,
        "format_version": "structural_v2",
        "structure_hash": view.structure_hash,
        "source_set_hash": view.source_set_hash,
        "profile_hash": view.profile_hash,
        "policy_hash": view.policy_hash,
        "catalog_hash": view.catalog_hash,
        "review_status": view.review_status,
        "approval": approval,
        "sources": [
            {
                "path": source.relative_path,
                "role": source.role.value,
                "byte_hash": source.byte_hash,
                "included": source.included,
                "excluded_reason": source.excluded_reason,
            }
            for source in view.sources
        ],
        "residents": [
            {
                "resident_id": item.resident_id,
                "ordinal": item.ordinal,
                "title": item.title,
                "content_hash": item.content_hash,
                "char_count": len(item.raw_text),
            }
            for item in view.residents
        ],
        "cards": [
            {
                "card_id": card.card_id,
                "type": card.type.value,
                "provenance": card.provenance.value,
                "resident_only": card.resident_only,
                "scope": card.scope_raw,
                "conditions": dict(card.effective_conditions),
                "char_count": len(card.raw_text),
                "topic": card.heading_path[-1] if card.heading_path else "",
            }
            for card in view.cards
        ],
        "encoded_count": len(view.encoded_cards),
        "index": index_payload,
        "note": "新格式的容量证明在 build 时逐卡记录",
    }


def run_approve_package(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    from sana_bot.ingest.markdown_package import load_package_config

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        raise ConfigError(f"配置不存在：{catalog_path}")
    config = load_package_config(catalog_path)
    policy = config.policy()
    packages = _packages(context)
    view = packages.view(args.structure)
    mismatches: dict[str, Any] = {}
    if view.policy_hash != policy.policy_hash:
        mismatches["policy_hash"] = {
            "expected": view.policy_hash,
            "actual": policy.policy_hash,
        }
    if view.catalog_hash != policy.catalog_hash:
        mismatches["catalog_hash"] = {
            "expected": view.catalog_hash,
            "actual": policy.catalog_hash,
        }
    if mismatches:
        raise NotReadyError(
            "配置与已登记结构不一致，拒绝登记批准；请重新解析后再批准",
            reason_code="report_version_mismatch",
            details={"mismatches": mismatches},
        )
    condition_conflicts = [
        card.card_id
        for card in view.cards
        if not card.resident_only and card.card_id not in config.card_conditions
    ]
    if condition_conflicts:
        raise NotReadyError(
            f"存在未映射条件的卡：{condition_conflicts}",
            reason_code="package_scope_unmapped",
        )
    packages.record_approval(
        structure_id=args.structure,
        structure_hash=view.structure_hash,
        catalog_hash=policy.catalog_hash,
        policy_hash=policy.policy_hash,
        profile_hash=view.profile_hash,
        catalog_path=str(catalog_path),
        reviewer=args.reviewer,
    )
    return {
        "structure_id": args.structure,
        "structure_hash": view.structure_hash,
        "policy_hash": policy.policy_hash,
        "catalog_hash": policy.catalog_hash,
        "profile_hash": view.profile_hash,
        "reviewer": args.reviewer,
        "approved_card_count": len(config.card_conditions),
        "still_required": ["index build", "index calibrate", "index evaluate", "index approve-evaluation"],
        "note": "批准不等于启用；构建与验收门槛仍需完成",
    }


# --------------------------------------------------------------------------- 构建


def run_build_package(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    if str(getattr(args, "split_profile", "") or "").strip():
        raise ConfigError(
            "新格式构建不允许 semantic --split-profile",
            reason_code="split_profile_forbidden",
        )
    backend = build_embedding_backend(context, allow_download=True)
    outcome = build_package(
        database=context.database,
        structures=context.structures,
        packages=_packages(context),
        structure_id=args.structure,
        backend=backend,
        output_dir=Path(args.output),
        project_root=None,
    )
    return {
        "index_id": outcome.index_id,
        "format_version": "structural_v2",
        "structure_id": outcome.structure_id,
        "snapshot_hash": outcome.snapshot_hash,
        "bundle_hash": outcome.bundle_hash,
        "manifest_hash": outcome.manifest_hash,
        "artifact_path": str(outcome.artifact_path),
        "resident_count": outcome.resident_count,
        "card_count": outcome.card_count,
        "encoded_count": outcome.encoded_count,
        "dimension": outcome.dimension,
        "token_counts": dict(outcome.token_counts),
        "encoder_config_hash": outcome.encoder_config_hash,
        "monologue_status": outcome.monologue_status,
        "metrics": dict(outcome.metrics),
        "note": "快照为 staged；需完成校准与验收后才能启用",
    }


# --------------------------------------------------------------------------- 校准


def run_calibrate_package(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    stage = RetrievalStage(args.stage)
    if stage is RetrievalStage.MONOLOGUE:
        raise NotReadyError(
            "资料包没有独白素材，不能运行 monologue 校准",
            reason_code="monologue_material_absent",
        )
    snapshot = context.structures.snapshot(args.index)
    store = _store(context)
    index = store.load_structural(snapshot)
    samples = load_calibration_samples(Path(args.samples))
    backend = build_embedding_backend(context, allow_download=False)
    outcome = calibrate_reply_threshold(
        index=index,
        samples=samples,
        embed_query=backend.embed_query,
        count_tokens=backend.count_tokens,
    )
    report = PackageCalibrationReport(
        id=new_id(),
        index_id=snapshot.id,
        stage=stage.value,
        snapshot_hash=snapshot.snapshot_hash,
        structural_profile_hash=snapshot.structural_profile_hash or "",
        retrieval_policy_hash=snapshot.retrieval_policy_hash,
        sample_set_hash=outcome.sample_set_hash,
        model_id=snapshot.embedding_model_id,
        encoder_config_hash=str(snapshot.manifest.get("encoder_config_hash", "")),
        threshold=outcome.threshold,
        sample_counts=dict(outcome.sample_counts),
        rank_metrics=dict(outcome.rank_metrics),
        approved_by="",
        approved_at=None,
        created_at=to_utc_iso(utc_now()),
        execution_mode="live",
        artifacts={"thresholds": outcome.rank_metrics.get("thresholds", {})},
        status="pending_review",
    )
    _reports(context).insert_calibration(report)
    return {
        "calibration_id": report.id,
        "index_id": snapshot.id,
        "snapshot_hash": snapshot.snapshot_hash,
        "stage": stage.value,
        "threshold": report.threshold,
        "report_hash": report.compute_hash(),
        "sample_set_hash": report.sample_set_hash,
        "sample_counts": dict(report.sample_counts),
        "rank_metrics": dict(report.rank_metrics),
        "status": report.status,
        "note": "校准报告未改变封存哈希；需维护者批准后用于启用",
    }


def run_approve_calibration(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    reports = _reports(context)
    reviewer = str(getattr(args, "reviewer", "") or "").strip()
    if not reviewer:
        raise ConfigError("必须提供 --reviewer")
    report = reports.approve_calibration(args.calibration, reviewer=reviewer)
    with context.database.write():
        context.database.execute(
            """
            UPDATE index_snapshot
            SET reply_calibration_id = ?, active_report_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (report.id, report.compute_hash(), to_utc_iso(utc_now()), report.index_id),
        )
    return {
        "calibration_id": report.id,
        "status": report.status,
        "approved_by": report.approved_by,
        "threshold": report.threshold,
        "report_hash": report.compute_hash(),
    }


def run_approve_calibration_command(args: argparse.Namespace) -> dict[str, Any]:
    context = open_context(args)
    try:
        return run_approve_calibration(context, args)
    finally:
        context.close()


# --------------------------------------------------------------------------- 评估


def run_evaluate_package(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    import asyncio

    mode = str(args.mode)
    if mode not in ("offline", "live"):
        raise ConfigError(f"未知评估模式：{mode}", reason_code="invalid_report_mode")
    snapshot = context.structures.snapshot(args.index)
    index = _store(context).load_structural(snapshot)
    view = _packages(context).view(str(snapshot.manifest.get("structure_id") or ""))
    cases = load_evaluation_cases(Path(args.samples))
    backend = build_embedding_backend(context, allow_download=(mode == "live"))

    if mode == "live":
        from sana_bot.dialogue.deepseek import ChatDeepSeekModel

        model = ChatDeepSeekModel(
            api_key=context.environment.deepseek_api_key,
            model=context.environment.deepseek_model,
            base_url=context.environment.deepseek_base_url,
        )
        substitutions: tuple[str, ...] = ()
    else:
        model = SubstituteChatModel()
        substitutions = ("chat_model=substitute-offline", "embeddings=configured")

    from dataclasses import replace as _replace

    from sana_bot.dialogue.output_policy import (
        ConstrainedOutputPolicy,
        check_text,
        constraints_from_config,
    )
    from sana_bot.dialogue.prompts import (
        build_generation_messages,
        package_extra_rules,
        package_reference_units,
    )

    constraints = _replace(
        constraints_from_config(
            context.config.max_reply_chars, context.config.max_reply_sentences
        ),
        strict_package_rules=True,
    )
    policy = ConstrainedOutputPolicy(constraints=constraints, model=model)

    threshold = None
    reports = _reports(context).calibrations(snapshot.id, stage="reply")
    approved = [item for item in reports if item.approved_at and item.snapshot_hash == snapshot.snapshot_hash]
    if approved:
        threshold = approved[-1].threshold

    async def generate(*, message, history, snapshot, selection):
        extra = package_extra_rules(residents=index.residents, rule_cards=selection.rule_cards)
        messages = build_generation_messages(
            message=message,
            history=tuple({"role": "user" if i % 2 == 0 else "assistant", "content": text} for i, text in enumerate(history)),
            snapshot=snapshot,
            constraints=constraints,
            references=package_reference_units(selection),
            extra_rules=extra,
        )
        from sana_bot.ports import ModelBudget, ModelRequest

        result = await model.complete_text(
            ModelRequest(messages=messages, correlation_id=None),
            ModelBudget(total_seconds=30.0),
        )
        return result.text if result.ok else ""

    def check_output(text: str) -> tuple[str, ...]:
        return tuple(item.code for item in check_text(text, constraints))

    evaluation = asyncio.run(
        evaluate_package(
            index=index,
            view=view,
            embed_query=backend.embed_query,
            generate=generate,
            check_output=check_output,
            evaluation_cases=cases,
            calibration_samples=None,
            mode=mode,
            count_tokens=backend.count_tokens,
            substitutions=substitutions,
        )
    )
    # selection 类别引用已批准校准报告的实际指标，不只看“有报告”
    if threshold is not None and approved:
        metrics = approved[-1].rank_metrics or {}
        recall = float(metrics.get("recall", 0.0))
        adopted = int(metrics.get("adopted_excluded", 1))
        passed = recall >= 0.90 and adopted == 0
        evaluation.results["categories"]["selection"] = {
            "threshold": threshold,
            "source": "approved_calibration_report",
            "calibration_id": approved[-1].id,
            "recall": recall,
            "adopted_excluded": adopted,
        }
        if evaluation.results["required"]["selection"] == "not_run":
            evaluation.results["required"]["selection"] = "passed" if passed else "failed"

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_id = new_id()
    report_path = output_dir / f"evaluation-{snapshot.id}-{report_id[:8]}.json"
    payload = {
        "report_id": report_id,
        "kind": "behaviour",
        "index_id": snapshot.id,
        "snapshot_hash": snapshot.snapshot_hash,
        "bundle_hash": snapshot.bundle_hash,
        "structural_profile_hash": snapshot.structural_profile_hash,
        "retrieval_policy_hash": snapshot.retrieval_policy_hash,
        "catalog_hash": evaluation.results.get("catalog_hash", view.catalog_hash),
        "encoder_config_hash": str(snapshot.manifest.get("encoder_config_hash", "")),
        "sample_set_hash": evaluation.sample_set_hash,
        "execution_mode": mode,
        **evaluation.to_payload(),
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = PackageRegressionReport(
        report_id=report_id,
        index_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        bundle_hash=snapshot.bundle_hash or "",
        structural_profile_hash=snapshot.structural_profile_hash or "",
        retrieval_policy_hash=snapshot.retrieval_policy_hash,
        sample_set_hash=evaluation.sample_set_hash,
        results=dict(evaluation.results),
        business_data_checks={},
        report_path=str(report_path),
        approved_by="",
        approved_at=None,
        created_at=to_utc_iso(utc_now()),
        kind="behaviour",
        execution_mode=mode,
        artifacts=dict(evaluation.artifacts),
        catalog_hash=view.catalog_hash,
        encoder_config_hash=str(snapshot.manifest.get("encoder_config_hash", "")),
    )
    _reports(context).insert_regression(report)
    return {
        "report_id": report.report_id,
        "report_path": str(report_path),
        "execution_mode": mode,
        "results": dict(evaluation.results),
        "substitutions": list(evaluation.substitutions),
        "status": report.status,
        "note": "报告初始为 pending_review；未执行项不计为通过",
    }


def run_approve_evaluation(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    reviewer = str(getattr(args, "reviewer", "") or "").strip()
    if not reviewer:
        raise ConfigError("必须提供 --reviewer")
    payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
    report_id = str(payload.get("report_id") or "")
    if not report_id:
        raise ConfigError(
            "报告缺少 report_id，不能只凭通过标记批准",
            reason_code="evaluation_incomplete",
        )
    reports = _reports(context)
    stored = reports.regression(report_id)
    mismatches: dict[str, Any] = {}
    for key in (
        "snapshot_hash",
        "bundle_hash",
        "structural_profile_hash",
        "retrieval_policy_hash",
        "sample_set_hash",
        "execution_mode",
    ):
        declared = payload.get(key)
        actual = getattr(stored, key, None)
        if declared is not None and str(declared) != str(actual):
            mismatches[key] = {"report": declared, "stored": actual}
    if mismatches:
        raise NotReadyError(
            "报告文件与已登记版本不一致，拒绝批准",
            reason_code="report_version_mismatch",
            details={"mismatches": mismatches},
        )
    if not (payload.get("artifacts") or {}).get("turns"):
        raise NotReadyError(
            "报告文件缺少逐轮产物，不能批准",
            reason_code="evaluation_incomplete",
        )
    reasons = tuple(str(item) for item in payload.get("reviewer_reasons", ()))
    report = reports.approve_regression(report_id, reviewer=reviewer, reasons=reasons)
    return {
        "report_id": report.report_id,
        "status": report.status,
        "approved_by": report.approved_by,
        "report_hash": report.compute_hash(),
        "execution_mode": report.execution_mode,
        "note": "offline 报告仅可用于演练库验证，不能作为正常运行启用证据",
    }


# --------------------------------------------------------------------------- 启用/恢复


def _approved_calibration(
    snapshot, reports: PackageReportRepository
) -> Any:
    rows = [
        item
        for item in reports.calibrations(snapshot.id, stage="reply")
        if item.snapshot_hash == snapshot.snapshot_hash
    ]
    approved = [item for item in rows if item.approved_at and item.threshold is not None]
    if not approved:
        raise NotReadyError(
            "缺少已批准的 reply 校准报告，不能启用",
            reason_code="package_calibration_missing",
        )
    return approved[-1]


def _approved_regression(snapshot, reports: PackageReportRepository, *, allow_offline: bool) -> Any:
    rows = reports.regressions(snapshot.id)
    approved = [
        item
        for item in rows
        if item.approved_at
        and item.snapshot_hash == snapshot.snapshot_hash
        and item.bundle_hash == (snapshot.bundle_hash or "")
        and item.kind == "behaviour"
    ]
    if not approved:
        raise NotReadyError(
            "缺少与当前快照绑定的已批准行为报告，不能启用",
            reason_code="package_report_missing",
        )
    live = [item for item in approved if item.execution_mode == "live"]
    if not live and not allow_offline:
        raise NotReadyError(
            "只有 offline 报告，不能用于正常运行的启用",
            reason_code="package_offline_only",
        )
    return live[-1] if live else approved[-1]


def validate_activation_target(
    context: RuntimeContext,
    *,
    index_id: str,
    allow_offline: bool,
) -> dict[str, Any]:
    """只读验证：重算封存产物、来源与报告绑定；任何一项不符都拒绝。"""
    snapshot = context.structures.snapshot(index_id)
    if snapshot.status is IndexStatus.FAILED:
        raise NotReadyError(
            "失败版本不能启用", reason_code="index_artifact_missing"
        )
    if not snapshot.is_structural:
        return {"index_id": index_id, "format": "semantic_v1", "snapshot": snapshot}
    actual_files = verify_sealed_bundle(snapshot)
    if not actual_files:
        raise NotReadyError(
            "封存目录为空，不能启用", reason_code="bundle_hash_mismatch"
        )
    packages = _packages(context)
    view = packages.view(str(snapshot.manifest.get("structure_id") or ""))
    if view.structure_hash != snapshot.structure_hash:
        raise NotReadyError(
            "结构 hash 与快照记录不一致", reason_code="report_version_mismatch"
        )
    if view.policy_hash != snapshot.retrieval_policy_hash:
        raise NotReadyError(
            "策略 hash 与快照记录不一致", reason_code="report_version_mismatch"
        )
    approval = packages.approval(view.structure_id)
    if approval is None:
        raise NotReadyError(
            "资料包尚未批准", reason_code="index_not_reviewed"
        )
    reports = _reports(context)
    calibration = _approved_calibration(snapshot, reports)
    regression = _approved_regression(snapshot, reports, allow_offline=allow_offline)
    return {
        "index_id": index_id,
        "format": "structural_v2",
        "snapshot": snapshot,
        "calibration": calibration,
        "regression": regression,
    }


def _apply_activation(
    context: RuntimeContext,
    *,
    index_id: str,
    expected_active: str | None,
    action: str,
    actor: str,
    reason: str,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot = validation["snapshot"]
    calibration = validation.get("calibration")
    regression = validation.get("regression")
    now = to_utc_iso(utc_now())
    expected = None if expected_active in (None, "", "none") else str(expected_active)
    with context.database.write():
        row = context.database.query_one(
            "SELECT id FROM index_snapshot WHERE status = 'active' LIMIT 1"
        )
        current = str(row["id"]) if row is not None else None
        if current != expected:
            raise NotReadyError(
                f"active 版本与期望不一致：期望 {expected or 'none'}，实际 {current or 'none'}",
                reason_code="active_version_conflict",
                details={"expected": expected, "actual": current},
            )
        if current != index_id:
            if current is not None:
                context.database.execute(
                    "UPDATE index_snapshot SET status = 'reviewed', updated_at = ? WHERE id = ?",
                    (now, current),
                )
            context.database.execute(
                """
                UPDATE index_snapshot
                SET status = 'active', updated_at = ?, active_report_hash = ?, approved_report_id = ?
                WHERE id = ?
                """,
                (
                    now,
                    getattr(calibration, "compute_hash", lambda: "")(),
                    getattr(regression, "report_id", None),
                    index_id,
                ),
            )
        report_ids = tuple(
            item
            for item in (
                getattr(calibration, "id", None),
                getattr(regression, "report_id", None),
            )
            if item
        )
        report_hashes = tuple(
            item
            for item in (
                getattr(calibration, "compute_hash", lambda: "")(),
                getattr(regression, "compute_hash", lambda: "")(),
            )
            if item
        )
        context.database.execute(
            """
            INSERT INTO activation_audit
                (id, action, previous_index_id, target_index_id, mode, snapshot_hash,
                 bundle_hash, validation_report_ids, actor, reason, occurred_at, result,
                 expected_active_index_id, calibration_id, report_hash,
                 validation_report_hashes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                action,
                current,
                index_id,
                "structural_v2",
                snapshot.snapshot_hash,
                snapshot.bundle_hash,
                json.dumps(list(report_ids), ensure_ascii=False),
                actor,
                reason,
                now,
                "ok",
                expected,
                getattr(calibration, "id", None),
                getattr(regression, "report_hash", ""),
                json.dumps(list(report_hashes), ensure_ascii=False),
            ),
        )
    return {
        "index_id": index_id,
        "action": action,
        "status": "active",
        "previous_index_id": current,
        "expected_active": expected,
        "snapshot_hash": snapshot.snapshot_hash,
        "bundle_hash": snapshot.bundle_hash,
        "calibration_id": getattr(calibration, "id", None),
        "report_id": getattr(regression, "report_id", None),
        "note": "在途轮次继续使用其固定 index_id；业务数据未修改",
    }


def _actor(args: argparse.Namespace) -> str:
    return str(getattr(args, "actor", "") or getattr(args, "reviewer", "") or "maintainer")


def activate_package(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    snapshot = context.structures.snapshot(args.index)
    allow_offline = bool(getattr(args, "allow_offline_rehearsal", False))
    if not snapshot.is_structural:
        return _activate_semantic(context, args)
    validation = validate_activation_target(
        context, index_id=args.index, allow_offline=allow_offline
    )
    return _apply_activation(
        context,
        index_id=args.index,
        expected_active=getattr(args, "expected_active", None),
        action="activate",
        actor=_actor(args),
        reason=str(getattr(args, "reason", "") or ""),
        validation=validation,
    )


def rollback_package(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    snapshot = context.structures.snapshot(args.index)
    if not snapshot.is_structural:
        return _rollback_semantic(context, args, snapshot)
    validation = validate_activation_target(
        context,
        index_id=args.index,
        allow_offline=bool(getattr(args, "allow_offline_rehearsal", False)),
    )
    return _apply_activation(
        context,
        index_id=args.index,
        expected_active=args.expected_active,
        action="rollback",
        actor=_actor(args),
        reason=args.reason,
        validation=validation,
    )


def _rollback_semantic(
    context: RuntimeContext, args: argparse.Namespace, snapshot: Any
) -> dict[str, Any]:
    """旧格式恢复：仍按旧格式的完整性、批准与校准规则重新验证。"""
    from sana_bot.enums import RetrievalStage

    known = list(context.structures.list_structures())
    known.extend(_packages(context).list_structures())
    candidates = [
        item for item in known if item["structure_hash"] == snapshot.structure_hash
    ]
    if not candidates:
        raise NotReadyError(
            "找不到该快照对应的结构", reason_code="structure_missing"
        )
    approvals = [
        context.structures.approval(item["id"]) or _packages(context).approval(item["id"])
        for item in candidates
    ]
    if not any(approval is not None for approval in approvals):
        raise NotReadyError(
            "结构尚未批准，不能恢复", reason_code="structure_not_approved"
        )
    reply = context.structures.calibration(snapshot.id, RetrievalStage.REPLY)
    if reply is None or reply.threshold is None:
        raise NotReadyError(
            "缺少 reply 阶段校准报告，不能恢复",
            reason_code="reply_calibration_missing",
        )
    if reply.snapshot_hash != snapshot.snapshot_hash:
        raise NotReadyError(
            "reply 校准报告与本快照 hash 不一致",
            reason_code="calibration_hash_mismatch",
        )
    return _apply_activation(
        context,
        index_id=snapshot.id,
        expected_active=args.expected_active,
        action="rollback",
        actor=_actor(args),
        reason=args.reason,
        validation={"snapshot": snapshot, "format": "semantic_v1"},
    )


def _activate_semantic(context: RuntimeContext, args: argparse.Namespace) -> dict[str, Any]:
    """旧 semantic_v1：沿用原有批准、校准与 hash 校验规则。"""
    from sana_bot.cli_index import run_activate as legacy_activate

    return legacy_activate(args)
