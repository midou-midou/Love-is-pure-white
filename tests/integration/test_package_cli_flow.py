"""CLI 端到端：parse → approve → build → calibrate → evaluate → activate（替代编码器）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from sana_bot import cli_package
from sana_bot.enums import IndexStatus
from sana_bot.errors import NotReadyError
from sana_bot.runtime import open_context
from tests.support.package_harness import package_backend

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "character_package"


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_package,
        "build_embedding_backend",
        lambda _context, allow_download=False: package_backend(),
    )
    runtime_dir = tmp_path / "runtime"
    args = argparse.Namespace(runtime_dir=str(runtime_dir))
    opened = open_context(args)
    try:
        yield opened, tmp_path
    finally:
        opened.close()


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


def test_full_offline_cli_flow(context, tmp_path):
    ctx, base = context
    parsed = cli_package.persist_package_structure(
        ctx,
        REPO_ROOT / "config" / "character-package.json",
        output_dir=base / "structures",
        project_root=REPO_ROOT,
    )
    structure_id = parsed["structure_id"]
    assert parsed["resident_count"] == 3

    approved = cli_package.run_approve_package(
        ctx,
        _args(
            structure=structure_id,
            catalog=str(REPO_ROOT / "config" / "character-package.json"),
            reviewer="maintainer",
        ),
    )
    assert approved["approved_card_count"] == 26

    built = cli_package.run_build_package(
        ctx,
        _args(structure=structure_id, output=str(base / "indexes"), split_profile=None),
    )
    assert built["encoded_count"] == 25
    assert built["resident_count"] == 3
    assert built["monologue_status"] == "disabled_no_material"
    index_id = built["index_id"]

    calibrated = cli_package.run_calibrate_package(
        ctx,
        _args(
            index=index_id,
            stage="reply",
            samples=str(FIXTURES / "retrieval.jsonl"),
        ),
    )
    assert calibrated["rank_metrics"]["recall"] >= 0.90
    assert calibrated["rank_metrics"]["adopted_excluded"] == 0
    assert calibrated["status"] == "pending_review"

    approved_calibration = cli_package.run_approve_calibration(
        ctx, _args(calibration=calibrated["calibration_id"], reviewer="maintainer")
    )
    assert approved_calibration["status"] == "approved"

    evaluated = cli_package.run_evaluate_package(
        ctx,
        _args(
            index=index_id,
            samples=str(FIXTURES / "evaluation.jsonl"),
            mode="offline",
            output=str(base / "evaluation"),
        ),
    )
    assert evaluated["execution_mode"] == "offline"
    assert evaluated["status"] == "pending_review"
    assert evaluated["results"]["required"]["integrity"] == "passed"
    assert evaluated["results"]["required"]["operations"] == "not_run"
    report_path = Path(evaluated["report_path"])
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["report_id"] == evaluated["report_id"]
    assert payload["artifacts"]["turns"]

    approved_report = cli_package.run_approve_evaluation(
        ctx, _args(index=index_id, report=str(report_path), reviewer="maintainer")
    )
    assert approved_report["status"] == "approved"

    with pytest.raises(NotReadyError) as excinfo:
        cli_package.activate_package(ctx, _args(
            index=index_id,
            expected_active="none",
            reason="offline",
            actor="maintainer",
            allow_offline_rehearsal=False,
        ))
    assert excinfo.value.reason_code == "package_offline_only"

    activated = cli_package.activate_package(
        ctx,
        _args(
            index=index_id,
            expected_active="none",
            reason="隔离演练",
            actor="maintainer",
            allow_offline_rehearsal=True,
        ),
    )
    assert activated["status"] == "active"
    assert ctx.structures.snapshot(index_id).status is IndexStatus.ACTIVE

    readiness = cli_package  # ensure module import surface stays usable
    assert readiness is not None


def test_build_rejects_split_profile_for_package(context, tmp_path):
    ctx, base = context
    parsed = cli_package.persist_package_structure(
        ctx,
        REPO_ROOT / "config" / "character-package.json",
        output_dir=base / "structures",
        project_root=REPO_ROOT,
    )
    with pytest.raises(Exception) as excinfo:
        cli_package.run_build_package(
            ctx,
            _args(
                structure=parsed["structure_id"],
                output=str(base / "indexes"),
                split_profile="sp-1",
            ),
        )
    assert getattr(excinfo.value, "reason_code", "") == "split_profile_forbidden"


def test_blocking_issue_prevents_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_package,
        "build_embedding_backend",
        lambda _context, allow_download=False: package_backend(),
    )
    ctx = open_context(argparse.Namespace(runtime_dir=str(tmp_path / "runtime")))
    try:
        broken = tmp_path / "broken"
        (broken / "docs").mkdir(parents=True)
        config = json.loads(
            (FIXTURES / "minimal" / "config.json").read_text(encoding="utf-8")
        )
        config["source_root"] = "docs"
        (broken / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        for name in ("01_core.md", "02_output.md", "03_emotion.md", "04_scene.md", "05_lore.md"):
            source = FIXTURES / "minimal" / "docs" / name
            (broken / "docs" / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        # 缺少 06 示例文件 → 必需文件缺失，不得登记
        with pytest.raises(Exception) as excinfo:
            cli_package.persist_package_structure(
                ctx, broken / "config.json", output_dir=tmp_path / "out", project_root=broken
            )
        assert getattr(excinfo.value, "reason_code", "") == "package_file_missing"
        assert ctx.database.query("SELECT * FROM structural_document") == []
    finally:
        ctx.close()
