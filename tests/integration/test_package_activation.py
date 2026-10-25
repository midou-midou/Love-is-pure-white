"""T046：校准/行为报告与启用、并发切换、恢复及业务数据不变。"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sana_bot.cli_package import activate_package, rollback_package
from sana_bot.enums import IndexStatus
from sana_bot.errors import NotReadyError
from sana_bot.ingest.package_service import build_package
from sana_bot.storage.package_repository import PackageRegressionReport
from tests.support.package_harness import (
    PackageHarness,
    build_package_harness,
    insert_regression_report,
    required_results,
    stable_report_artifacts,
)


@pytest.fixture
def harness(tmp_path) -> PackageHarness:
    built = build_package_harness(tmp_path / "activation", with_calibration=True)
    try:
        # 默认回到 staged，模拟尚未启用的新版本
        built.structures.set_snapshot_status(built.index_id, IndexStatus.STAGED)
        yield built
    finally:
        built.close()


def _context(harness: PackageHarness) -> SimpleNamespace:
    return SimpleNamespace(database=harness.database, structures=harness.structures)


def _args(index_id: str, *, expected: str, reason: str = "test", rehearsal: bool = False):
    return argparse.Namespace(
        index=index_id,
        expected_active=expected,
        reason=reason,
        actor="tester",
        allow_offline_rehearsal=rehearsal,
    )


def _second_index(harness: PackageHarness, tmp_path: Path) -> str:
    outcome = build_package(
        database=harness.database,
        structures=harness.structures,
        packages=harness.packages,
        structure_id=harness.structure_id,
        backend=harness.backend,
        output_dir=tmp_path / "second",
    )
    harness.reports.insert_calibration(
        __import__(
            "sana_bot.storage.package_repository", fromlist=["PackageCalibrationReport"]
        ).PackageCalibrationReport(
            id="cal-second",
            index_id=outcome.index_id,
            stage="reply",
            snapshot_hash=outcome.snapshot_hash,
            structural_profile_hash=harness.structures.snapshot(outcome.index_id).structural_profile_hash
            or "",
            retrieval_policy_hash=harness.structures.snapshot(outcome.index_id).retrieval_policy_hash,
            sample_set_hash="fixture",
            model_id=harness.structures.snapshot(outcome.index_id).embedding_model_id,
            encoder_config_hash="enc",
            threshold=0.25,
            sample_counts={},
            rank_metrics={},
            approved_by="tester",
            approved_at="2026-10-25T00:00:00+00:00",
            created_at="2026-10-25T00:00:00+00:00",
        )
    )
    snapshot = harness.structures.snapshot(outcome.index_id)
    harness.reports.insert_regression(
        PackageRegressionReport(
            report_id="reg-second",
            index_id=outcome.index_id,
            snapshot_hash=outcome.snapshot_hash,
            bundle_hash=outcome.bundle_hash,
            structural_profile_hash=snapshot.structural_profile_hash or "",
            retrieval_policy_hash=snapshot.retrieval_policy_hash,
            sample_set_hash="fixture",
            results=required_results(mode="live"),
            business_data_checks={},
            report_path="",
            approved_by="tester",
            approved_at="2026-10-25T00:00:00+00:00",
            created_at="2026-10-25T00:00:00+00:00",
            execution_mode="live",
            artifacts=stable_report_artifacts(mode="live"),
        )
    )
    return outcome.index_id


def test_activation_requires_behaviour_report(harness):
    with pytest.raises(NotReadyError) as excinfo:
        activate_package(_context(harness), _args(harness.index_id, expected="none"))
    assert excinfo.value.reason_code == "package_report_missing"


def test_offline_only_report_cannot_activate_normally(harness):
    insert_regression_report(harness, execution_mode="offline")
    with pytest.raises(NotReadyError) as excinfo:
        activate_package(_context(harness), _args(harness.index_id, expected="none"))
    assert excinfo.value.reason_code == "package_offline_only"


def test_offline_report_allowed_in_isolated_rehearsal(harness):
    insert_regression_report(harness, execution_mode="offline")
    result = activate_package(
        _context(harness), _args(harness.index_id, expected="none", rehearsal=True)
    )
    assert result["status"] == "active"
    assert result["expected_active"] is None


def test_successful_activation_writes_audit(harness):
    insert_regression_report(harness, execution_mode="live")
    result = activate_package(_context(harness), _args(harness.index_id, expected="none"))
    assert result["status"] == "active"
    snapshot = harness.structures.snapshot(harness.index_id)
    assert snapshot.status is IndexStatus.ACTIVE
    audits = harness.database.query("SELECT * FROM activation_audit ORDER BY occurred_at")
    assert audits and audits[-1]["action"] == "activate"
    assert audits[-1]["expected_active_index_id"] is None
    assert audits[-1]["calibration_id"] == harness.calibration_id
    assert audits[-1]["report_hash"]
    assert snapshot.approved_report_id


def test_expected_active_conflict_keeps_current_version(harness):
    insert_regression_report(harness, execution_mode="live")
    activate_package(_context(harness), _args(harness.index_id, expected="none"))
    second = _second_index(harness, harness.directory / "tmp")
    with pytest.raises(NotReadyError) as excinfo:
        activate_package(_context(harness), _args(second, expected="none"))
    assert excinfo.value.reason_code == "active_version_conflict"
    assert harness.structures.active_snapshot().id == harness.index_id


def test_activation_is_idempotent_but_still_checks_expected(harness):
    insert_regression_report(harness, execution_mode="live")
    activate_package(_context(harness), _args(harness.index_id, expected="none"))
    again = activate_package(_context(harness), _args(harness.index_id, expected=harness.index_id))
    assert again["status"] == "active"
    with pytest.raises(NotReadyError) as excinfo:
        activate_package(_context(harness), _args(harness.index_id, expected="none"))
    assert excinfo.value.reason_code == "active_version_conflict"


def test_tampered_bundle_blocks_activation(harness):
    insert_regression_report(harness, execution_mode="live")
    snapshot = harness.structures.snapshot(harness.index_id)
    path = Path(snapshot.artifact_path) / "vectors.npy"
    matrix = np.load(path)
    matrix[0][0] += 1.0
    np.save(path, matrix)
    with pytest.raises(NotReadyError) as excinfo:
        activate_package(_context(harness), _args(harness.index_id, expected="none"))
    assert excinfo.value.reason_code == "bundle_hash_mismatch"


def test_report_bound_to_other_snapshot_is_rejected(tmp_path):
    built = build_package_harness(tmp_path / "bound", with_calibration=True)
    try:
        built.structures.set_snapshot_status(built.index_id, IndexStatus.STAGED)
        snapshot = built.structures.snapshot(built.index_id)
        built.reports.insert_regression(
            PackageRegressionReport(
                report_id="wrong-snapshot",
                index_id=built.index_id,
                snapshot_hash="other-snapshot",
                bundle_hash=snapshot.bundle_hash or "",
                structural_profile_hash=snapshot.structural_profile_hash or "",
                retrieval_policy_hash=snapshot.retrieval_policy_hash,
                sample_set_hash="fixture",
                results=required_results(mode="live"),
                business_data_checks={},
                report_path="",
                approved_by="tester",
                approved_at="2026-10-25T00:00:00+00:00",
                created_at="2026-10-25T00:00:00+00:00",
                execution_mode="live",
                artifacts=stable_report_artifacts(mode="live"),
            )
        )
        with pytest.raises(NotReadyError) as excinfo:
            activate_package(_context(built), _args(built.index_id, expected="none"))
        assert excinfo.value.reason_code == "package_report_missing"
    finally:
        built.close()


def test_unapproved_calibration_blocks_activation(tmp_path):
    built = build_package_harness(tmp_path / "unapproved", with_calibration=False)
    try:
        built.structures.set_snapshot_status(built.index_id, IndexStatus.STAGED)
        insert_regression_report(built, execution_mode="live")
        with pytest.raises(NotReadyError) as excinfo:
            activate_package(_context(built), _args(built.index_id, expected="none"))
        assert excinfo.value.reason_code == "package_calibration_missing"
    finally:
        built.close()


def test_business_data_unchanged_after_switch(harness):
    from tests.support import make_event

    from sana_bot.storage.inbound_repository import InboundRepository

    insert_regression_report(harness, execution_mode="live")
    inbound = InboundRepository(harness.database)
    with harness.database.write():
        persisted = inbound.persist_inbound(
            make_event(
                message_id="m-1",
                user_key="openid-a",
                text="今天社团的小猫",
                event_time=__import__("datetime").datetime(
                    2026, 10, 25, 12, 0, tzinfo=__import__("datetime").UTC
                ),
                received_at=__import__("datetime").datetime(
                    2026, 10, 25, 12, 0, tzinfo=__import__("datetime").UTC
                ),
            )
        )
    before = {
        "users": len(harness.database.query("SELECT * FROM user_identity")),
        "turns": len(harness.database.query("SELECT * FROM turn")),
        "inbound": len(harness.database.query("SELECT * FROM inbound_event")),
    }
    activate_package(_context(harness), _args(harness.index_id, expected="none"))
    after = {
        "users": len(harness.database.query("SELECT * FROM user_identity")),
        "turns": len(harness.database.query("SELECT * FROM turn")),
        "inbound": len(harness.database.query("SELECT * FROM inbound_event")),
    }
    assert before == after
    assert harness.database.query("SELECT * FROM relationship") == []
    assert persisted.inbound_id


def test_switch_keeps_in_flight_reference_valid(harness):
    insert_regression_report(harness, execution_mode="live")
    frozen = harness.store.load_structural(harness.structures.snapshot(harness.index_id))
    second = _second_index(harness, harness.directory / "tmp2")
    activate_package(_context(harness), _args(harness.index_id, expected="none"))
    activate_package(_context(harness), _args(second, expected=harness.index_id))
    # 缓存被清理后，旧引用仍可读且版本一致
    harness.store.forget(harness.index_id)
    assert frozen.index_id == harness.index_id
    assert frozen.matrix.shape[0] == 25
    assert harness.structures.active_snapshot().id == second


def test_rollback_restores_previous_version(harness):
    insert_regression_report(harness, execution_mode="live")
    second = _second_index(harness, harness.directory / "tmp3")
    activate_package(_context(harness), _args(harness.index_id, expected="none"))
    activate_package(_context(harness), _args(second, expected=harness.index_id))
    assert harness.structures.active_snapshot().id == second
    result = rollback_package(
        _context(harness),
        argparse.Namespace(
            index=harness.index_id,
            expected_active=second,
            reason="性能回退",
            actor="tester",
            allow_offline_rehearsal=False,
        ),
    )
    assert result["action"] == "rollback"
    assert harness.structures.active_snapshot().id == harness.index_id
    audits = harness.database.query(
        "SELECT * FROM activation_audit WHERE action = 'rollback'"
    )
    assert audits and audits[-1]["reason"] == "性能回退"


def test_new_package_cannot_reuse_old_report(harness):
    insert_regression_report(harness, execution_mode="live")
    second = _second_index(harness, harness.directory / "tmp4")
    activate_package(_context(harness), _args(harness.index_id, expected="none"))
    # 删除新索引自己的报告，把旧索引的报告挂过来：快照/bundle 不匹配，必须拒绝
    with harness.database.write():
        harness.database.execute(
            "DELETE FROM regression_report WHERE report_id = 'reg-second'"
        )
        harness.database.execute(
            "UPDATE regression_report SET index_id = ? WHERE report_id = 'reg-live-fixture'",
            (second,),
        )
    with pytest.raises(NotReadyError) as excinfo:
        activate_package(_context(harness), _args(second, expected=harness.index_id))
    assert excinfo.value.reason_code == "package_report_missing"


def test_semantic_rollback_uses_legacy_validation(tmp_path):
    """旧格式恢复仍按旧格式的批准与校准规则重新验证。"""
    from sana_bot.enums import MonologueStatus, RetrievalStage
    from sana_bot.storage.database import open_runtime_database
    from sana_bot.storage.index_repository import (
        CalibrationProfileRecord,
        SnapshotRecord,
        StructureRepository,
    )
    from sana_bot.storage.package_repository import PackageRepository
    from sana_bot.timeutil import to_utc_iso, utc_now
    from sana_bot.ingest.markdown_package import parse_markdown_package

    database = open_runtime_database(tmp_path / "semantic.sqlite3")
    structures = StructureRepository(database)
    packages = PackageRepository(database)
    package = parse_markdown_package(
        Path(__file__).resolve().parents[2] / "config" / "character-package.json",
        project_root=Path(__file__).resolve().parents[2],
    )
    packages.save_package(package, structure_id="sem-struct")
    policy = package.config.policy()
    packages.record_approval(
        structure_id="sem-struct",
        structure_hash=package.structure_hash,
        catalog_hash=policy.catalog_hash,
        policy_hash=policy.policy_hash,
        profile_hash=package.compute_profile_hash(),
        catalog_path="config/character-package.json",
        reviewer="tester",
    )
    # 结构登记为 package 行，但快照故意写成 semantic_v1 以验证旧格式分支
    now = to_utc_iso(utc_now())
    snapshot = SnapshotRecord(
        id="sem-1",
        source_hash="s",
        structure_hash=package.structure_hash,
        split_profile_hash="split-1",
        snapshot_hash="snap-1",
        manifest_hash="m",
        parser_version="p",
        splitter_version="splitter",
        embedding_model_id="model",
        embedding_revision="rev",
        dimension=8,
        rule_catalog_version="catalog",
        reply_calibration_id=None,
        monologue_calibration_id=None,
        monologue_status=MonologueStatus.DISABLED_NO_MATERIAL,
        status=IndexStatus.STAGED,
        artifact_path=str(tmp_path / "art"),
        manifest={},
        created_at=now,
        updated_at=now,
        index_format_version="semantic_v1",
        chunking_strategy="semantic",
    )
    structures.insert_snapshot(snapshot)
    structures.insert_calibration(
        CalibrationProfileRecord(
            id="cal-1",
            index_id="sem-1",
            snapshot_hash="snap-1",
            split_profile_hash="split-1",
            stage=RetrievalStage.REPLY,
            sample_set_hash="s",
            threshold=0.5,
            score_metric="cosine",
            metrics={},
            approved_at=now,
            created_at=now,
        )
    )
    context = SimpleNamespace(database=database, structures=structures)
    result = rollback_package(
        context,
        argparse.Namespace(
            index="sem-1",
            expected_active="none",
            reason="旧版恢复",
            actor="tester",
            allow_offline_rehearsal=False,
        ),
    )
    assert result["action"] == "rollback"
    assert structures.snapshot("sem-1").status is IndexStatus.ACTIVE
    database.close()
