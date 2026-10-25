"""SQLite 连接与短事务管理。

规则：WAL 模式、外键开启、迁移按注册顺序单事务应用；网络与模型调用不得位于
写事务内（由 :meth:`Database.assert_outside_write_transaction` 在调用点强制）。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sana_bot.errors import SanaError
from sana_bot.timeutil import to_utc_iso, utc_now

__all__ = ["Database", "Migration", "WriteTransactionActive", "open_runtime_database"]


class WriteTransactionActive(RuntimeError):
    """在写事务内执行外部调用（模型/网络）时抛出。"""


@dataclass(frozen=True)
class Migration:
    """一条迁移；按注册顺序在单个事务中应用。

    ``foreign_keys_off`` 用于 SQLite 官方 12 步表重建配方：约束检查必须在
    事务外关闭，否则父表被重建期间子表外键会立刻报错。
    """

    name: str
    statements: tuple[str, ...]
    foreign_keys_off: bool = False


class Database:
    """单实例 SQLite；每线程独立连接，写事务串行。"""

    def __init__(
        self,
        path: Path | str,
        *,
        migrations: Sequence[Migration] = (),
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.migrations = tuple(migrations)
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._depth = threading.local()

    # ------------------------------------------------------------------ 连接

    @property
    def _is_memory(self) -> bool:
        return str(self.path) == ":memory:"

    def connection(self) -> sqlite3.Connection:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        if self._is_memory:
            # 内存库共享单连接，保证同一进程内可见
            with self._lock:
                shared = getattr(self, "_shared_connection", None)
                if shared is None:
                    shared = self._new_connection()
                    self._shared_connection = shared
            return shared
        connection = self._new_connection()
        self._local.connection = connection
        return connection

    def _new_connection(self) -> sqlite3.Connection:
        if not self._is_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        if not self._is_memory:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        with self._lock:
            self._connections.append(connection)
        return connection

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            self._local.connection = None
        with self._lock:
            pending = list(self._connections)
            self._connections.clear()
            self._shared_connection = None
        for item in pending:
            try:
                item.close()
            except sqlite3.Error:  # pragma: no cover - 关闭失败无补救动作
                pass

    # ------------------------------------------------------------------ 事务

    @property
    def in_write_transaction(self) -> bool:
        return getattr(self._depth, "write", 0) > 0

    def assert_outside_write_transaction(self, *, what: str) -> None:
        if self.in_write_transaction:
            raise WriteTransactionActive(
                f"{what} 不得在数据库写事务内执行；请先提交事务再调用外部服务"
            )

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """短写事务：BEGIN IMMEDIATE，异常回滚。"""
        connection = self.connection()
        nested = self.in_write_transaction
        if not nested:
            connection.execute("BEGIN IMMEDIATE")
        self._depth.write = getattr(self._depth, "write", 0) + 1
        try:
            yield connection
        except BaseException:
            self._depth.write -= 1
            if not nested:
                connection.execute("ROLLBACK")
            raise
        else:
            self._depth.write -= 1
            if not nested:
                connection.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self.connection()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.connection().execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.connection().execute(sql, tuple(params)).fetchone()

    # ------------------------------------------------------------------ 迁移

    def migrate(self) -> tuple[str, ...]:
        """应用未执行的迁移，返回本次应用的名称。"""
        connection = self.connection()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            row["name"] for row in connection.execute("SELECT name FROM schema_migrations").fetchall()
        }
        newly: list[str] = []
        for migration in self.migrations:
            if migration.name in applied:
                continue
            foreign_keys_were_on = False
            if migration.foreign_keys_off:
                foreign_keys_were_on = bool(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0]
                )
                connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                        (migration.name, to_utc_iso(utc_now())),
                    )
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                else:
                    connection.execute("COMMIT")
            finally:
                if migration.foreign_keys_off:
                    connection.execute(
                        f"PRAGMA foreign_keys = {'ON' if foreign_keys_were_on else 'OFF'}"
                    )
            newly.append(migration.name)
        return tuple(newly)


def open_runtime_database(
    path: Path | str,
    *,
    extra_migrations: Sequence[Migration] = (),
) -> Database:
    """按运行目录打开数据库并应用全部迁移。"""
    from sana_bot.storage.schema import all_migrations

    migrations = (*all_migrations(), *extra_migrations)
    database = Database(path, migrations=migrations)
    database.migrate()
    return database


def describe_error(exc: BaseException) -> SanaError:
    """把 sqlite 错误转成可诊断的统一错误。"""
    if isinstance(exc, sqlite3.IntegrityError):
        return SanaError(f"数据库唯一性或外键约束失败：{exc}", reason_code="storage_conflict")
    return SanaError(f"存储失败：{exc}", reason_code="storage_failure")
