"""T045：索引管理 CLI 契约（参数、JSON 与 0/2/3/4 退出语义）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from sana_bot.cli import build_parser, main, run
from sana_bot.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_NOT_READY,
    EXIT_OK,
    ConfigError,
    NotReadyError,
)


def _parse(argv):
    return build_parser().parse_args(argv)


def test_parse_command_accepts_markdown_package_format():
    args = _parse(
        [
            "index",
            "parse",
            "--format",
            "markdown-package",
            "--source",
            "config/character-package.json",
            "--output",
            "runtime/structures",
        ]
    )
    assert args.format == "markdown-package"
    assert args.source.endswith("character-package.json")


def test_parse_command_defaults_to_docx():
    args = _parse(["index", "parse", "--source", "a.docx", "--output", "out"])
    assert args.format == "docx"


def test_build_split_profile_is_optional_on_cli():
    args = _parse(["index", "build", "--structure", "s1", "--output", "out"])
    assert args.split_profile is None


def test_new_commands_are_registered():
    for argv in (
        ["index", "evaluate", "--index", "i", "--samples", "s.jsonl", "--mode", "offline", "--output", "o"],
        ["index", "approve-evaluation", "--index", "i", "--report", "r.json", "--reviewer", "m"],
        ["index", "rollback", "--index", "i", "--expected-active", "x", "--reason", "why"],
        ["index", "approve-calibration", "--calibration", "c", "--reviewer", "m"],
    ):
        parsed = _parse(argv)
        assert parsed is not None


def test_evaluate_mode_is_restricted():
    with pytest.raises(SystemExit):
        _parse(
            [
                "index",
                "evaluate",
                "--index",
                "i",
                "--samples",
                "s",
                "--mode",
                "fake",
                "--output",
                "o",
            ]
        )


def test_monologue_calibration_is_rejected(tmp_path, monkeypatch):
    from sana_bot import cli_package

    args = argparse.Namespace(
        index="idx",
        stage="monologue",
        samples=str(tmp_path / "samples.jsonl"),
        runtime_dir=str(tmp_path / "runtime"),
    )
    with pytest.raises(NotReadyError) as excinfo:
        cli_package.run_calibrate_package(object(), args)  # type: ignore[arg-type]
    assert excinfo.value.reason_code == "monologue_material_absent"
    assert excinfo.value.exit_code == EXIT_NOT_READY


def test_parse_of_missing_config_exits_with_config_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        run(
            [
                "index",
                "parse",
                "--format",
                "markdown-package",
                "--source",
                str(tmp_path / "missing.json"),
                "--output",
                str(tmp_path / "out"),
                "--runtime-dir",
                str(tmp_path / "runtime"),
            ]
        )
    assert excinfo.value.exit_code == EXIT_CONFIG_ERROR
    assert excinfo.value.reason_code == "package_file_missing"


def test_parse_emits_structured_json(tmp_path, capsys):
    runtime = tmp_path / "runtime"
    code = main(
        [
            "index",
            "parse",
            "--format",
            "markdown-package",
            "--source",
            str(Path(__file__).resolve().parents[2] / "config" / "character-package.json"),
            "--output",
            str(tmp_path / "out"),
            "--runtime-dir",
            str(runtime),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["format_version"] == "structural_v2"
    assert payload["resident_count"] == 3
    assert payload["approval_status"] == "pending"
    assert "structure_id" in payload
    assert "report_path" in payload


def test_main_maps_errors_to_exit_codes(tmp_path, capsys):
    code = main(
        [
            "index",
            "review",
            "--structure",
            "missing",
            "--runtime-dir",
            str(tmp_path / "runtime"),
        ]
    )
    assert code == EXIT_NOT_READY
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason_code"]
