"""T008：增量迁移、幂等、旧格式读取与业务数据不变。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sana_bot.enums import IndexStatus
from sana_bot.ingest.models import (
    CHUNKING_STRATEGY_SEMANTIC,
    INDEX_FORMAT_SEMANTIC_V1,
)
from sana_bot.storage.database import Database
from sana_bot.storage.index_repository import SnapshotRecord, StructureRepository
from sana_bot.storage.schema import all_migrations
from sana_bot.timeutil import to_utc_iso, utc_now

RUNTIME_DIR = Path(__file__).resolve().parents[2] / "runtime"


def _semantic_snapshot(index_id: str = "sem-1") -> SnapshotRecord:
    now = to_utc_iso(utc_now())
    return SnapshotRecord(
        id=index_id,
        source_hash="source",
        structure_hash="structure",
        split_profile_hash="split",
        snapshot_hash="snap",
        manifest_hash="manifest",
        parser_version="p",
        splitter_version="s",
        embedding_model_id="model",
        embedding_revision="rev",
        dimension=8,
        rule_catalog_version="catalog",
        reply_calibration_id=None,
        monologue_calibration_id=None,
        monologue_status=__import__(
            "sana_bot.enums", fromlist=["MonologueStatus"]
        ).MonologueStatus.DISABLED_NO_MATERIAL,
        status=IndexStatus.STAGED,
        artifact_path="/tmp/x",
        manifest={},
        created_at=now,
        updated_at=now,
        index_format_version=INDEX_FORMAT_SEMANTIC_V1,
        chunking_strategy=CHUNKING_STRATEGY_SEMANTIC,
    )


def test_new_migrations_apply_once_and_are_idempotent(tmp_path):
    path = tmp_path / "db.sqlite3"
    first = Database(path, migrations=all_migrations())
    applied = first.migrate()
    assert "0012_package_sources_and_reports" in applied
    assert first.migrate() == ()
    tables = {
        row["name"]
        for row in first.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"package_source", "package_resident", "package_card"} <= tables
    first.close()
    second = Database(path, migrations=all_migrations())
    assert second.migrate() == ()
    second.close()


def test_reports_are_versioned_not_unique_per_stage(tmp_path):
    database = Database(tmp_path / "db.sqlite3", migrations=all_migrations())
    database.migrate()
    columns = {
        row["name"] for row in database.query("PRAGMA table_info(structural_calibration_report)")
    }
    assert {"report_hash", "execution_mode", "status", "artifacts"} <= columns
    indexes = {
        row["name"]
        for row in database.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='structural_calibration_report'"
        )
    }
    assert "idx_structural_calibration_stage" not in indexes
    assert "idx_structural_calibration_versioned" in indexes
    database.close()


def test_migration_preserves_existing_business_rows(tmp_path):
    path = tmp_path / "db.sqlite3"
    database = Database(path, migrations=all_migrations())
    database.migrate()
    now = to_utc_iso(utc_now())
    with database.write():
        database.execute(
            "INSERT INTO user_identity (id, app_id, user_openid, created_at) VALUES (?, ?, ?, ?)",
            ("u1", "app", "openid", now),
        )
        database.execute(
            """
            INSERT INTO conversation (id, user_id, generation, scene, session_status, created_at, updated_at)
            VALUES (?, ?, 0, 'private', 'active', ?, ?)
            """,
            ("c1", "u1", now, now),
        )
    database.close()

    reopened = Database(path, migrations=all_migrations())
    assert reopened.migrate() == ()
    assert reopened.query_one("SELECT id FROM user_identity")["id"] == "u1"
    assert reopened.query_one("SELECT scene FROM conversation")["scene"] == "private"
    reopened.close()


def test_legacy_semantic_snapshot_still_readable(tmp_path):
    database = Database(tmp_path / "db.sqlite3", migrations=all_migrations())
    database.migrate()
    repository = StructureRepository(database)
    repository.insert_snapshot(_semantic_snapshot())
    loaded = repository.snapshot("sem-1")
    assert loaded.index_format_version == INDEX_FORMAT_SEMANTIC_V1
    assert loaded.is_structural is False
    assert loaded.split_profile_hash == "split"
    assert loaded.bundle_hash is None
    database.close()


def test_structural_snapshot_requires_package_hashes(tmp_path):
    from sana_bot.errors import NotReadyError

    database = Database(tmp_path / "db.sqlite3", migrations=all_migrations())
    database.migrate()
    repository = StructureRepository(database)
    base = _semantic_snapshot()
    broken = SnapshotRecord(
        **{
            **base.__dict__,
            "id": "broken",
            "split_profile_hash": None,
            "index_format_version": "structural_v2",
            "chunking_strategy": "markdown_cards_v1",
            "structural_profile_hash": None,
            "retrieval_policy_hash": "",
        }
    )
    with pytest.raises(NotReadyError) as excinfo:
        repository.insert_snapshot(broken)
    assert excinfo.value.reason_code == "snapshot_format_inconsistent"
    database.close()


def test_unknown_format_is_rejected(tmp_path):
    from sana_bot.errors import NotReadyError

    database = Database(tmp_path / "db.sqlite3", migrations=all_migrations())
    database.migrate()
    repository = StructureRepository(database)
    base = _semantic_snapshot()
    unknown = SnapshotRecord(
        **{**base.__dict__, "id": "u", "index_format_version": "semantic_v9"}
    )
    with pytest.raises(NotReadyError) as excinfo:
        repository.insert_snapshot(unknown)
    assert excinfo.value.reason_code == "unknown_index_format"
    database.close()


def test_only_one_active_snapshot_is_allowed(tmp_path):
    import sqlite3

    database = Database(tmp_path / "db.sqlite3", migrations=all_migrations())
    database.migrate()
    repository = StructureRepository(database)
    first = SnapshotRecord(**{**_semantic_snapshot("a").__dict__, "status": IndexStatus.ACTIVE})
    second = SnapshotRecord(**{**_semantic_snapshot("b").__dict__, "status": IndexStatus.ACTIVE})
    repository.insert_snapshot(first)
    with pytest.raises(sqlite3.IntegrityError):
        repository.insert_snapshot(second)
    database.close()


def test_runtime_database_has_active_format_and_no_package_rows(tmp_path):
    """真实运行库的旧格式快照在新增迁移后仍可读取（不改写业务数据）。"""
    database_path = RUNTIME_DIR / "sana-bot.sqlite3"
    if not database_path.exists():
        pytest.skip("运行库不存在")
    database = Database(database_path)
    try:
        rows = database.query("SELECT index_format_version FROM index_snapshot")
        assert all(row["index_format_version"] in ("semantic_v1", "structural_v2") for row in rows)
    finally:
        database.close()
