"""T030/T055：资料包就绪检查与独白能力禁用。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sana_bot.cli_doctor import ensure_ready, package_readiness
from sana_bot.enums import IndexStatus, MonologueStatus
from sana_bot.errors import NotReadyError
from sana_bot.scheduling.monologue import MonologueScheduler
from tests.support.package_harness import PackageHarness, build_package_harness


@pytest.fixture
def harness(tmp_path) -> PackageHarness:
    built = build_package_harness(tmp_path / "ready", with_calibration=True)
    try:
        yield built
    finally:
        built.close()


def test_ready_when_residents_and_report_present(harness):
    context = SimpleNamespace(
        database=harness.database,
        structures=harness.structures,
        environment=SimpleNamespace(),
    )
    payload = package_readiness(context)
    assert payload["format"] == "structural_v2"
    assert payload["ready"] is True
    assert payload["problems"] == []
    assert payload["encoded_cards"] == 25
    assert payload["bundle_hash"] == harness.bundle_hash
    assert payload["calibration_id"] == harness.calibration_id


def test_monologue_disabled_does_not_block_reply(harness):
    context = SimpleNamespace(
        database=harness.database, structures=harness.structures, environment=SimpleNamespace()
    )
    payload = package_readiness(context)
    assert payload["monologue_status"] == MonologueStatus.DISABLED_NO_MATERIAL.value
    assert payload["monologue_blocks_reply"] is False
    assert payload["ready"] is True


def test_missing_calibration_is_not_ready(tmp_path):
    built = build_package_harness(tmp_path / "no-cal", with_calibration=False)
    try:
        context = SimpleNamespace(
            database=built.database, structures=built.structures, environment=SimpleNamespace()
        )
        payload = package_readiness(context)
        assert payload["ready"] is False
        assert "package_calibration_missing" in payload["problems"]
        with pytest.raises(NotReadyError) as excinfo:
            ensure_ready(SimpleNamespace(context=context))
        assert excinfo.value.reason_code == "package_calibration_missing"
    finally:
        built.close()


def test_missing_resident_is_not_ready(harness):
    with harness.database.write():
        harness.database.execute(
            "DELETE FROM package_resident WHERE resident_id = 'sana_output'"
        )
    context = SimpleNamespace(
        database=harness.database, structures=harness.structures, environment=SimpleNamespace()
    )
    payload = package_readiness(context)
    assert payload["ready"] is False
    assert "resident_missing" in payload["problems"]


def test_tampered_artifacts_are_not_ready(harness):
    import numpy as np

    path = Path(harness.artifact_dir) / "vectors.npy"
    matrix = np.load(path)
    matrix[0][0] += 1.0
    np.save(path, matrix)
    context = SimpleNamespace(
        database=harness.database, structures=harness.structures, environment=SimpleNamespace()
    )
    payload = package_readiness(context)
    assert payload["ready"] is False
    assert "bundle_hash_mismatch" in payload["problems"]


def test_no_active_index_is_not_ready(tmp_path):
    built = build_package_harness(tmp_path / "staged")
    try:
        built.structures.set_snapshot_status(built.index_id, IndexStatus.STAGED)
        context = SimpleNamespace(
            database=built.database, structures=built.structures, environment=SimpleNamespace()
        )
        payload = package_readiness(context)
        assert payload["ready"] is False
        assert "no_active_index" in payload["problems"]
    finally:
        built.close()


def test_monologue_opportunity_skipped_when_disabled(harness):
    from sana_bot.config import RuntimeConfig
    from sana_bot.storage.delivery_repository import DeliveryRepository
    from sana_bot.storage.inbound_repository import InboundRepository
    from sana_bot.storage.monologue_repository import MonologueRepository
    from tests.support import FakeClock

    clock = FakeClock()
    scheduler = MonologueScheduler(
        repository=MonologueRepository(harness.database),
        structures=harness.structures,
        deliveries=DeliveryRepository(harness.database, config=RuntimeConfig()),
        inbound=InboundRepository(harness.database),
        config=RuntimeConfig(),
        clock=clock,
    )
    status, threshold = scheduler.status()
    assert status is MonologueStatus.DISABLED_NO_MATERIAL
    assert threshold is None
