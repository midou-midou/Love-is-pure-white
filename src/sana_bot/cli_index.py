"""``index`` 命令实现：解析、核对、批准、切分校准、构建、门槛校准与激活。

未就绪的步骤返回明确失败（退出码 3），不伪造成功，也不自动启用未经核对的版本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from sana_bot.enums import ReviewStatus
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.service import IngestService
from sana_bot.runtime import open_context

__all__ = [
    "run_parse",
    "run_review",
    "run_approve",
    "run_calibrate_split",
    "run_build",
    "run_calibrate",
    "run_activate",
    "run_evaluate",
    "run_approve_evaluation",
    "run_rollback",
]


def run_parse(args: argparse.Namespace) -> dict[str, Any]:
    if str(getattr(args, "format", "docx") or "docx") == "markdown-package":
        from sana_bot.cli_package import persist_package_structure

        context = open_context(args)
        try:
            return persist_package_structure(
                context, Path(args.source), output_dir=Path(args.output), project_root=None
            )
        finally:
            context.close()
    context = open_context(args)
    try:
        service = IngestService(context.database, layout=context.layout, config=context.config)
        result = service.parse(Path(args.source), Path(args.output))
        return {
            "structure_id": result.structure_id,
            "structure_hash": result.details.get("structure_hash"),
            "structure_path": str(result.structure_path),
            "report_path": str(result.report_path),
            "node_count": result.node_count,
            "span_count": result.details.get("span_count"),
            "source_hash": result.source_hash,
            "review_status": result.details.get("review_status"),
            "speaker_counts": result.details.get("speaker_counts"),
            "issues": [
                {"reason_code": issue.reason_code, "message": issue.message}
                for issue in result.issues
            ],
            "note": "结构未启用；需先人工核对并执行 index approve",
        }
    finally:
        context.close()


def run_review(args: argparse.Namespace) -> dict[str, Any]:
    structure_arg = getattr(args, "structure", None)
    index_arg = getattr(args, "index", None)
    if not structure_arg and not index_arg:
        raise ConfigError("index review 需要 --structure 或 --index")
    context = open_context(args)
    try:
        from sana_bot.cli_package import index_is_package, run_review_package, structure_is_package

        if structure_is_package(context, structure_arg) or index_is_package(context, index_arg):
            return run_review_package(context, args)
        structure_id = structure_arg
        index_payload: dict[str, Any] = {}
        if not structure_id:
            snapshot = context.structures.snapshot(index_arg)
            index_payload = snapshot.to_payload()
            matches = [
                item
                for item in context.structures.list_structures()
                if item["structure_hash"] == snapshot.structure_hash
            ]
            if not matches:
                raise NotReadyError(
                    f"索引 {args.index} 的结构不可用", reason_code="structure_missing"
                )
            structure_id = matches[0]["id"]
        service = IngestService(context.database, layout=context.layout, config=context.config)
        report = service.review(structure_id)
        payload = dict(report.payload)
        if index_payload:
            payload["index"] = index_payload
        approval = context.structures.approval(structure_id)
        payload["approval"] = approval
        return payload
    finally:
        context.close()


def run_approve(args: argparse.Namespace) -> dict[str, Any]:
    if not str(args.catalog or "").strip():
        raise ConfigError("必须提供 --catalog")
    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        raise ConfigError(f"catalog 文件不存在：{catalog_path}")
    if not args.reviewer.strip():
        raise ConfigError("必须提供 --reviewer")
    raw = catalog_path.read_bytes()
    catalog_hash = hashlib.sha256(raw).hexdigest()
    try:
        catalog = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"catalog 不是合法 JSON：{exc}") from exc
    if not isinstance(catalog, dict):
        raise ConfigError("catalog 必须是对象")

    context = open_context(args)
    try:
        if catalog.get("format_version") == "structural_v2":
            from sana_bot.cli_package import run_approve_package

            return run_approve_package(context, args)
        document = context.structures.load_structure(args.structure)
        declared = catalog.get("structure_hash")
        if declared and str(declared) != document.structure_hash:
            raise NotReadyError(
                "catalog 声明的 structure_hash 与目标结构不一致，拒绝登记批准",
                reason_code="catalog_structure_mismatch",
                details={
                    "catalog_structure_hash": str(declared),
                    "structure_hash": document.structure_hash,
                },
            )
        pending = _pending_derived_rules(catalog)
        context.structures.record_approval(
            structure_id=args.structure,
            structure_hash=document.structure_hash,
            catalog_hash=catalog_hash,
            catalog_path=str(catalog_path),
            reviewer=args.reviewer,
        )
        with context.database.write():
            context.database.execute(
                "UPDATE structured_document SET catalog_hash = ?, review_status = ? WHERE id = ?",
                (catalog_hash, ReviewStatus.APPROVED.value, args.structure),
            )
        return {
            "structure_id": args.structure,
            "structure_hash": document.structure_hash,
            "catalog_hash": catalog_hash,
            "reviewer": args.reviewer,
            "approved_count": len(catalog.get("rules", [])),
            "still_unapproved": pending,
            "note": "批准不等于校准通过；检索门槛仍需 index calibrate",
        }
    finally:
        context.close()


def run_calibrate_split(args: argparse.Namespace) -> dict[str, Any]:
    """在确认样本上校准切分参数并固定 profile；不修改结构或旧索引。"""
    from sana_bot.ingest.models import SPLITTER_VERSION
    from sana_bot.ingest.semantic_splitter import SplitterParameters
    from sana_bot.retrieval.calibration import calibrate_split_parameters, load_samples
    from sana_bot.runtime import build_embedding_backend
    from sana_bot.storage.index_repository import (
        SplitCalibrationProfileRecord,
        new_id,
    )
    from sana_bot.timeutil import to_utc_iso, utc_now

    context = open_context(args)
    try:
        document = context.structures.load_structure(args.structure)
        backend = build_embedding_backend(context, allow_download=False)
        backend.ensure_available()
        split_samples, _ = load_samples(Path(args.samples))
        outcome = calibrate_split_parameters(split_samples, embed=backend.embed_documents)
        parameters = SplitterParameters(
            window_sentences=outcome.parameters.window_sentences,
            boundary_similarity_threshold=outcome.parameters.boundary_similarity_threshold,
            max_unit_tokens=outcome.parameters.max_unit_tokens,
            min_unit_tokens=outcome.parameters.min_unit_tokens,
            splitter_version=SPLITTER_VERSION,
            calibrated=True,
        )
        payload = parameters.to_payload()
        profile_hash = hashlib.sha256(
            json.dumps(
                {
                    "structure_hash": document.structure_hash,
                    "model_revision": backend.spec.effective_revision,
                    "sample_set_hash": outcome.sample_set_hash,
                    "parameters": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        profile_id = new_id()
        context.structures.insert_split_profile(
            SplitCalibrationProfileRecord(
                id=profile_id,
                structure_hash=document.structure_hash,
                model_revision=backend.spec.effective_revision,
                sample_set_hash=outcome.sample_set_hash,
                splitter_version=SPLITTER_VERSION,
                parameters=payload,
                profile_hash=profile_hash,
                metrics=dict(outcome.metrics),
                approved_at=to_utc_iso(utc_now()),
                created_at=to_utc_iso(utc_now()),
            )
        )
        return {
            "split_profile_id": profile_id,
            "profile_hash": profile_hash,
            "structure_hash": document.structure_hash,
            "model_revision": backend.spec.effective_revision,
            "sample_set_hash": outcome.sample_set_hash,
            "parameters": payload,
            "metrics": dict(outcome.metrics),
            "note": "参数已固定；改参数必须重新校准并重建快照",
        }
    finally:
        context.close()


def run_build(args: argparse.Namespace) -> dict[str, Any]:
    """按固定 profile 重新生成片段与向量；不自动启用。"""
    from sana_bot.ingest.service import build_snapshot
    from sana_bot.runtime import build_embedding_backend

    context = open_context(args)
    try:
        from sana_bot.cli_package import run_build_package, structure_is_package

        if structure_is_package(context, args.structure):
            return run_build_package(context, args)
        if not str(getattr(args, "split_profile", "") or "").strip():
            raise ConfigError(
                "semantic 构建需要 --split-profile", reason_code="split_profile_required"
            )
        profile = context.structures.split_profile(args.split_profile)
        backend = build_embedding_backend(context, allow_download=True)
        outcome = build_snapshot(
            database=context.database,
            repository=context.structures,
            layout=context.layout,
            structure_id=args.structure,
            split_profile=profile,
            embedding_backend=backend,
            output_dir=Path(args.output),
            config=context.config,
        )
        return {
            "index_id": outcome.index_id,
            "snapshot_hash": outcome.snapshot_hash,
            "manifest_hash": outcome.manifest_hash,
            "artifact_path": str(outcome.artifact_path),
            "unit_count": outcome.unit_count,
            "monologue_status": outcome.monologue_status,
            "monologue_material_count": outcome.monologue_material_count,
            "metrics": dict(outcome.metrics),
            "note": "快照尚未启用；reply 阶段校准完成后执行 index activate",
        }
    finally:
        context.close()


def run_calibrate(args: argparse.Namespace) -> dict[str, Any]:
    """在已构建快照上校准 reply/monologue 门槛，报告绑定实际 snapshot_hash。"""
    import numpy as np

    from sana_bot.enums import MonologueStatus, RetrievalStage
    from sana_bot.ingest.semantic_splitter import cosine_similarity
    from sana_bot.retrieval.calibration import (
        RetrievalSample,
        calibrate_threshold,
        load_samples,
    )
    from sana_bot.runtime import build_embedding_backend
    from sana_bot.storage.index_repository import CalibrationProfileRecord, new_id
    from sana_bot.timeutil import to_utc_iso, utc_now

    stage = RetrievalStage(args.stage)
    context = open_context(args)
    try:
        from sana_bot.cli_package import index_is_package, run_calibrate_package

        if index_is_package(context, getattr(args, "index", None)):
            return run_calibrate_package(context, args)
        snapshot = context.structures.snapshot(args.index)
        if (
            stage is RetrievalStage.MONOLOGUE
            and snapshot.monologue_status is MonologueStatus.DISABLED_NO_MATERIAL
        ):
            raise NotReadyError(
                "索引中没有真正的独白素材，独白能力保持 disabled_no_material",
                reason_code="monologue_material_absent",
            )
        _, retrieval_samples = load_samples(Path(args.samples))
        backend = build_embedding_backend(context, allow_download=False)
        units = context.structures.load_semantic_units(snapshot.id)
        records = context.structures.load_embedding_records(snapshot.id)
        matrix = np.load(Path(snapshot.artifact_path) / "vectors.npy")

        def score(sample: RetrievalSample) -> tuple[float, ...]:
            query_vector = backend.embed_query(sample.query)
            by_id = {record.unit_id: record.vector_offset for record in records}
            if sample.expected_unit_ids:
                offsets = [by_id[item] for item in sample.expected_unit_ids if item in by_id]
                if not offsets:
                    raise NotReadyError(
                        f"样本期望片段不在本快照内：{sample.query}",
                        reason_code="calibration_sample_not_in_snapshot",
                    )
                return tuple(
                    cosine_similarity(query_vector, list(matrix[offset])) for offset in offsets
                )
            return tuple(
                cosine_similarity(query_vector, list(row)) for row in matrix
            )

        outcome = calibrate_threshold(retrieval_samples, stage=stage, scored=score)
        metrics = dict(outcome.metrics)
        metrics["filters_applied"] = bool(
            all(sample.expected_unit_ids for sample in retrieval_samples if sample.stage is stage)
        )
        metrics["unit_count"] = len(units)
        profile_id = new_id()
        context.structures.insert_calibration(
            CalibrationProfileRecord(
                id=profile_id,
                index_id=snapshot.id,
                snapshot_hash=snapshot.snapshot_hash,
                split_profile_hash=snapshot.split_profile_hash,
                stage=stage,
                sample_set_hash=outcome.sample_set_hash,
                threshold=outcome.threshold,
                score_metric="cosine",
                metrics=metrics,
                approved_at=to_utc_iso(utc_now()),
                created_at=to_utc_iso(utc_now()),
            )
        )
        monologue_status = snapshot.monologue_status
        if stage is RetrievalStage.MONOLOGUE:
            monologue_status = MonologueStatus.ENABLED
        with context.database.write():
            context.database.execute(
                """
                UPDATE index_snapshot
                SET reply_calibration_id = COALESCE(?, reply_calibration_id),
                    monologue_calibration_id = COALESCE(?, monologue_calibration_id),
                    monologue_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    profile_id if stage is RetrievalStage.REPLY else None,
                    profile_id if stage is RetrievalStage.MONOLOGUE else None,
                    monologue_status.value,
                    to_utc_iso(utc_now()),
                    snapshot.id,
                ),
            )
        return {
            "calibration_id": profile_id,
            "index_id": snapshot.id,
            "snapshot_hash": snapshot.snapshot_hash,
            "stage": stage.value,
            "threshold": outcome.threshold,
            "sample_set_hash": outcome.sample_set_hash,
            "metrics": metrics,
            "monologue_status": monologue_status.value,
        }
    finally:
        context.close()


def run_activate(args: argparse.Namespace) -> dict[str, Any]:
    """校验结构/切分批准、reply 校准与所有 enabled 能力报告的 hash 后原子激活。"""
    from sana_bot.enums import IndexStatus, MonologueStatus, RetrievalStage
    from sana_bot.timeutil import to_utc_iso, utc_now

    context = open_context(args)
    try:
        from sana_bot.cli_package import activate_package, index_is_package

        if index_is_package(context, getattr(args, "index", None)):
            return activate_package(context, args)
        snapshot = context.structures.snapshot(args.index)
        if snapshot.status is IndexStatus.ACTIVE:
            return {"index_id": snapshot.id, "status": "active", "note": "已是活动版本"}
        approvals = [
            item
            for item in context.structures.list_structures()
            if item["structure_hash"] == snapshot.structure_hash
        ]
        if not approvals:
            raise NotReadyError("找不到该快照对应的结构", reason_code="structure_missing")
        approval = context.structures.approval(approvals[0]["id"])
        if approval is None:
            raise NotReadyError("结构尚未批准，不能激活", reason_code="structure_not_approved")
        reply = context.structures.calibration(snapshot.id, RetrievalStage.REPLY)
        if reply is None or reply.threshold is None:
            raise NotReadyError(
                "缺少 reply 阶段校准报告，不能激活",
                reason_code="reply_calibration_missing",
            )
        if reply.snapshot_hash != snapshot.snapshot_hash:
            raise NotReadyError(
                "reply 校准报告与本快照 hash 不一致",
                reason_code="calibration_hash_mismatch",
            )
        if snapshot.monologue_status is MonologueStatus.ENABLED:
            monologue = context.structures.calibration(snapshot.id, RetrievalStage.MONOLOGUE)
            if monologue is None or monologue.threshold is None:
                raise NotReadyError(
                    "独白已启用但缺少匹配报告",
                    reason_code="monologue_calibration_missing",
                )
            if monologue.snapshot_hash != snapshot.snapshot_hash:
                raise NotReadyError(
                    "独白校准报告与本快照 hash 不一致",
                    reason_code="calibration_hash_mismatch",
                )
        elif snapshot.monologue_calibration_id:
            raise NotReadyError(
                "独白被禁用时不应保留独白校准引用",
                reason_code="monologue_reference_inconsistent",
            )

        now = to_utc_iso(utc_now())
        with context.database.write():
            context.database.execute(
                "UPDATE index_snapshot SET status = 'reviewed', updated_at = ? WHERE status = 'active'",
                (now,),
            )
            context.database.execute(
                "UPDATE index_snapshot SET status = 'active', updated_at = ? WHERE id = ?",
                (now, snapshot.id),
            )
        return {
            "index_id": snapshot.id,
            "status": "active",
            "snapshot_hash": snapshot.snapshot_hash,
            "reply_threshold": reply.threshold,
            "monologue_status": snapshot.monologue_status.value,
            "note": "在途轮次继续使用其固定 index_id",
        }
    finally:
        context.close()



def run_evaluate(args: argparse.Namespace) -> dict[str, Any]:
    from sana_bot.cli_package import run_evaluate_package

    context = open_context(args)
    try:
        return run_evaluate_package(context, args)
    finally:
        context.close()


def run_approve_evaluation(args: argparse.Namespace) -> dict[str, Any]:
    from sana_bot.cli_package import run_approve_evaluation as approve_evaluation

    context = open_context(args)
    try:
        return approve_evaluation(context, args)
    finally:
        context.close()


def run_rollback(args: argparse.Namespace) -> dict[str, Any]:
    from sana_bot.cli_package import rollback_package

    context = open_context(args)
    try:
        return rollback_package(context, args)
    finally:
        context.close()


def _pending_derived_rules(catalog: dict[str, Any]) -> list[str]:
    rules = catalog.get("rules")
    if not isinstance(rules, list):
        return []
    return [
        str(rule.get("id", f"rule-{index}"))
        for index, rule in enumerate(rules)
        if isinstance(rule, dict) and rule.get("approved") is not True
    ]
