"""T006：资料包错误分类、退出码与脱敏。"""

from __future__ import annotations

import pytest

from sana_bot.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_EXTERNAL_FAILURE,
    EXIT_NOT_READY,
    ConfigError,
    ExternalServiceError,
    NotReadyError,
    PackageExternalFailure,
    PackageFailure,
    PackageNotReady,
    exit_code_for,
    redact_details,
)
from sana_bot.ingest.package_models import PackageError


@pytest.mark.parametrize(
    ("error", "code"),
    (
        (PackageError("x"), EXIT_CONFIG_ERROR),
        (PackageFailure("x"), EXIT_CONFIG_ERROR),
        (PackageNotReady("x"), EXIT_NOT_READY),
        (PackageExternalFailure("x"), EXIT_EXTERNAL_FAILURE),
        (ConfigError("x"), EXIT_CONFIG_ERROR),
        (NotReadyError("x"), EXIT_NOT_READY),
        (ExternalServiceError("x"), EXIT_EXTERNAL_FAILURE),
    ),
)
def test_error_classes_map_to_exit_codes(error, code):
    assert error.exit_code == code


@pytest.mark.parametrize(
    ("reason", "code"),
    (
        ("package_file_missing", EXIT_CONFIG_ERROR),
        ("package_metadata_invalid", EXIT_CONFIG_ERROR),
        ("source_path_escape", EXIT_CONFIG_ERROR),
        ("duplicate_card_id", EXIT_CONFIG_ERROR),
        ("resident_missing", EXIT_NOT_READY),
        ("card_input_over_limit", EXIT_NOT_READY),
        ("bundle_hash_mismatch", EXIT_NOT_READY),
        ("package_relationship_mismatch", EXIT_NOT_READY),
        ("active_version_conflict", EXIT_NOT_READY),
        ("model_failure", EXIT_EXTERNAL_FAILURE),
    ),
)
def test_reason_codes_have_exit_semantics(reason, code):
    assert exit_code_for(reason) == code


def test_unknown_reason_defaults_to_external_failure():
    assert exit_code_for("some_unmapped_reason") == EXIT_EXTERNAL_FAILURE
    assert exit_code_for("some_unmapped_reason", default=EXIT_CONFIG_ERROR) == EXIT_CONFIG_ERROR


def test_error_details_are_redacted():
    details = {
        "api_key": "sk-live-123",
        "authorization": "Bearer xyz",
        "embedding_token": "abc",
        "file": "03_情绪状态.md",
        "long": "字" * 500,
        "nested": {"client_secret": "s", "reason": "ok"},
    }
    redacted = redact_details(details)
    assert redacted["api_key"] == "[redacted]"
    assert redacted["authorization"] == "[redacted]"
    assert redacted["embedding_token"] == "[redacted]"
    assert redacted["nested"]["client_secret"] == "[redacted]"
    assert redacted["nested"]["reason"] == "ok"
    assert redacted["file"] == "03_情绪状态.md"
    assert redacted["long"].endswith("…")
    assert len(redacted["long"]) <= 201


def test_package_error_carries_position_and_expectation():
    error = PackageError(
        "卡元数据缺字段：scope",
        reason_code="package_metadata_invalid",
        details={"file": "03_情绪状态.md", "line_start": 5, "expected": ["id", "type"]},
    )
    assert error.reason_code == "package_metadata_invalid"
    assert error.details["line_start"] == 5
    assert "scope" in error.message
