"""T047：评估样本冻结、逐轮产物、人工评审门槛与报告不可变。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sana_bot.dialogue.output_policy import check_text, constraints_from_config
from sana_bot.dialogue.prompts import (
    build_generation_messages,
    package_extra_rules,
    package_reference_units,
)
from sana_bot.dialogue.substitute import SubstituteChatModel
from sana_bot.errors import ConfigError, NotReadyError
from sana_bot.ingest.package_evaluation import (
    cases_hash,
    evaluate_package,
    load_evaluation_cases,
)
from sana_bot.retrieval.package_calibration import load_calibration_samples
from sana_bot.ports import ModelBudget, ModelRequest
from sana_bot.storage.package_repository import PackageRegressionReport
from tests.support.package_harness import (
    PackageHarness,
    build_package_harness,
    required_results,
    stable_report_artifacts,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "character_package"
EVALUATION = FIXTURES / "evaluation.jsonl"
RETRIEVAL = FIXTURES / "retrieval.jsonl"


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> PackageHarness:
    built = build_package_harness(tmp_path_factory.mktemp("evaluation"))
    try:
        yield built
    finally:
        built.close()


def test_evaluation_fixture_is_frozen_and_complete():
    cases = load_evaluation_cases(EVALUATION)
    daily = [case for case in cases if case.kind == "daily"]
    behaviour = [case for case in cases if case.kind == "behaviour"]
    assert len(daily) == 24
    assert len(behaviour) == 24
    assert len({case.category for case in behaviour}) == 12
    for case in behaviour:
        assert len(case.turns) >= 3
    assert cases_hash(cases) == cases_hash(load_evaluation_cases(EVALUATION))


def test_rubric_exists_and_defines_thresholds():
    rubric = (FIXTURES / "rubric.md").read_text(encoding="utf-8")
    assert "至少 23 例" in rubric
    assert "至少 22 段" in rubric
    assert "严重错误" in rubric


def _generate_fn(harness: PackageHarness, constraints, model):
    async def generate(*, message, history, snapshot, selection):
        messages = build_generation_messages(
            message=message,
            history=tuple(
                {"role": "user" if index % 2 == 0 else "assistant", "content": text}
                for index, text in enumerate(history)
            ),
            snapshot=snapshot,
            constraints=constraints,
            references=package_reference_units(selection),
            extra_rules=package_extra_rules(
                residents=harness.index().residents, rule_cards=selection.rule_cards
            ),
        )
        result = await model.complete_text(
            ModelRequest(messages=messages), ModelBudget(30.0)
        )
        return result.text if result.ok else ""

    return generate


def _check(constraints):
    def check(text: str) -> tuple[str, ...]:
        return tuple(item.code for item in check_text(text, constraints))

    return check


@pytest.mark.asyncio
async def test_offline_evaluation_produces_turns_and_never_claims_live(harness, tmp_path):
    constraints = replace(
        constraints_from_config(160, 3), strict_package_rules=True
    )
    model = SubstituteChatModel()
    cases = load_evaluation_cases(EVALUATION)
    outcome = await evaluate_package(
        index=harness.index(),
        view=harness.package,
        embed_query=harness.backend.embed_query,
        generate=_generate_fn(harness, constraints, model),
        check_output=_check(constraints),
        evaluation_cases=cases,
        calibration_samples=load_calibration_samples(RETRIEVAL),
        mode="offline",
        count_tokens=harness.backend.count_tokens,
        substitutions=("chat_model=substitute-offline",),
    )
    assert outcome.execution_mode == "offline"
    assert outcome.results["required"]["integrity"] == "passed"
    assert outcome.results["required"]["selection"] == "passed"
    assert len(outcome.artifacts["turns"]) == sum(len(case.turns) for case in cases)
    assert all(turn["execution_mode"] == "offline" for turn in outcome.artifacts["turns"])
    # 运维演练未执行，不计为通过
    assert outcome.results["required"]["operations"] == "not_run"
    assert outcome.results["live_required"] is True
    assert "QQ" in " ".join(outcome.notes)


@pytest.mark.asyncio
async def test_evaluation_records_per_turn_detail(harness):
    constraints = replace(constraints_from_config(160, 3), strict_package_rules=True)
    outcome = await evaluate_package(
        index=harness.index(),
        view=harness.package,
        embed_query=harness.backend.embed_query,
        generate=_generate_fn(harness, constraints, SubstituteChatModel()),
        check_output=_check(constraints),
        evaluation_cases=load_evaluation_cases(EVALUATION)[:4],
        calibration_samples=None,
        mode="offline",
        count_tokens=harness.backend.count_tokens,
    )
    turn = outcome.artifacts["turns"][0]
    assert {"case_id", "turn_index", "input", "reply", "decision", "selected", "violations"} <= set(turn)


def test_approve_requires_artifacts_and_passed_thresholds(harness):
    reports = harness.reports
    failing = PackageRegressionReport(
        report_id="failing",
        index_id=harness.index_id,
        snapshot_hash=harness.snapshot_hash,
        bundle_hash=harness.bundle_hash,
        structural_profile_hash="",
        retrieval_policy_hash="",
        sample_set_hash="s",
        results={"required": {"integrity": "passed", "behaviour": "failed"}, "execution_mode": "live"},
        business_data_checks={},
        report_path="",
        approved_by="",
        approved_at=None,
        created_at="2026-10-25T00:00:00+00:00",
        execution_mode="live",
        artifacts={"turns": [{"case_id": "x"}]},
    )
    reports.insert_regression(failing)
    with pytest.raises(NotReadyError) as excinfo:
        reports.approve_regression("failing", reviewer="maintainer")
    assert excinfo.value.reason_code == "evaluation_incomplete"

    no_artifacts = PackageRegressionReport(
        report_id="no-artifacts",
        index_id=harness.index_id,
        snapshot_hash=harness.snapshot_hash,
        bundle_hash=harness.bundle_hash,
        structural_profile_hash="",
        retrieval_policy_hash="",
        sample_set_hash="s",
        results=required_results(mode="live"),
        business_data_checks={},
        report_path="",
        approved_by="",
        approved_at=None,
        created_at="2026-10-25T00:00:00+00:00",
        execution_mode="live",
        artifacts={},
    )
    reports.insert_regression(no_artifacts)
    with pytest.raises(NotReadyError) as excinfo:
        reports.approve_regression("no-artifacts", reviewer="maintainer")
    assert excinfo.value.reason_code == "evaluation_incomplete"


def test_report_revisions_are_immutable(harness):
    reports = harness.reports
    first = PackageRegressionReport(
        report_id="immutable-1",
        index_id=harness.index_id,
        snapshot_hash=harness.snapshot_hash,
        bundle_hash=harness.bundle_hash,
        structural_profile_hash="",
        retrieval_policy_hash="",
        sample_set_hash="s",
        results=required_results(mode="live"),
        business_data_checks={},
        report_path="",
        approved_by="",
        approved_at=None,
        created_at="2026-10-25T00:00:00+00:00",
        execution_mode="live",
        artifacts=stable_report_artifacts(mode="live"),
    )
    reports.insert_regression(first)
    with pytest.raises(ConfigError):
        reports.insert_regression(first)
    revision = replace(first, report_id="immutable-2", sample_set_hash="s2")
    reports.insert_regression(revision)
    assert len(reports.regressions(harness.index_id)) >= 2


def test_threshold_does_not_change_bundle(harness):
    before = harness.structures.snapshot(harness.index_id)
    from tests.support.package_harness import insert_calibration_report

    insert_calibration_report(harness, threshold=0.35, approved=True)
    after = harness.structures.snapshot(harness.index_id)
    assert before.snapshot_hash == after.snapshot_hash
    assert before.bundle_hash == after.bundle_hash


def test_evaluation_rejects_unknown_mode(harness):
    with pytest.raises(ConfigError) as excinfo:
        __import__("asyncio").run(
            evaluate_package(
                index=harness.index(),
                view=harness.package,
                embed_query=harness.backend.embed_query,
                generate=lambda **kwargs: None,
                check_output=lambda text: (),
                evaluation_cases=load_evaluation_cases(EVALUATION)[:1],
                mode="pretend",
            )
        )
    assert excinfo.value.reason_code == "invalid_report_mode"


def test_evaluation_fails_when_integrity_is_broken(harness):
    index = harness.index()
    broken = replace(index, residents=index.residents[:2])
    outcome = __import__("asyncio").run(
        evaluate_package(
            index=broken,
            view=harness.package,
            embed_query=harness.backend.embed_query,
            generate=lambda **kwargs: None,
            check_output=lambda text: (),
            evaluation_cases=load_evaluation_cases(EVALUATION)[:1],
            mode="offline",
        )
    )
    assert outcome.results["required"]["integrity"] == "failed"
    assert outcome.results["required"]["behaviour"] == "not_run"
