"""运行配置：环境密钥与版本化非敏感配置。

密钥只从环境注入，永不写入仓库、日志或 JSON 输出；非敏感配置有版本与生效时间，
非法更新保持旧版本。检索门槛不在此处设默认值：reply/monologue 门槛来自绑定当前
``snapshot_hash`` 的 :class:`CalibrationProfile`。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sana_bot.errors import ConfigError

__all__ = [
    "Environment",
    "AffectionWeights",
    "RuntimeConfig",
    "RuntimeLayout",
    "ConfigStore",
    "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_int_env(environ: Mapping[str, str], name: str, default: int) -> int:
    """从传入的环境映射读取整数，避免绕过注入、误读真实进程环境。"""
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        parsed = int(str(raw).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，得到 {raw!r}") from exc
    return parsed


#: 单聊/群事件订阅位（GROUP_AND_C2C_EVENT）：C2C_MESSAGE_CREATE 等
DEFAULT_QQ_INTENTS = 1 << 25
QQ_TRANSPORTS = ("webhook", "websocket")


def _qq_transport_env(environ: Mapping[str, str]) -> str:
    raw = (environ.get("QQ_TRANSPORT") or "webhook").strip().lower()
    if raw not in QQ_TRANSPORTS:
        raise ConfigError(
            f"QQ_TRANSPORT 只能是 {'/'.join(QQ_TRANSPORTS)}，得到 {raw!r}",
            reason_code="qq_transport_invalid",
        )
    return raw


def _qq_shard_env(environ: Mapping[str, str]) -> tuple[int, int]:
    raw = (environ.get("QQ_SHARD") or "0,1").strip()
    parts = [item.strip() for item in raw.split(",")]
    try:
        shard, total = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError) as exc:
        raise ConfigError(
            f"QQ_SHARD 必须是「当前分片,分片总数」，得到 {raw!r}",
            reason_code="qq_shard_invalid",
        ) from exc
    if total <= 0 or shard < 0 or shard >= total:
        raise ConfigError(
            f"QQ_SHARD 超出范围：分片 {shard} / 总数 {total}",
            reason_code="qq_shard_invalid",
        )
    return shard, total


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url
    except Exception:  # noqa: BLE001 - 仅用于展示
        return "unknown"


@dataclass(frozen=True)
class RuntimeLayout:
    """运行目录布局；除仓库模板外均不提交。"""

    root: Path

    @property
    def database_path(self) -> Path:
        return self.root / "sana-bot.sqlite3"

    @property
    def structures_dir(self) -> Path:
        return self.root / "structures"

    @property
    def indexes_dir(self) -> Path:
        return self.root / "indexes"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    def ensure(self) -> RuntimeLayout:
        for path in (self.root, self.structures_dir, self.indexes_dir, self.reports_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class Environment:
    """进程环境中的密钥与地址配置。"""

    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    qq_app_id: str | None = None
    qq_app_secret: str | None = None
    qq_api_base_url: str = "https://api.bot.qq.com"
    qq_transport: str = "webhook"
    qq_intents: int = DEFAULT_QQ_INTENTS
    qq_shard: tuple[int, int] = (0, 1)
    qq_gateway_url: str | None = None
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_revision: str | None = None
    embedding_cache_dir: Path | None = None
    embedding_kind: str = "local"
    embedding_api_key: str | None = None
    embedding_base_url: str = ""
    embedding_dimension: int = 512
    embedding_max_tokens: int = 512
    runtime_dir: Path = Path("runtime")
    tracing_enabled: bool = False

    @classmethod
    def from_mapping(cls, environ: Mapping[str, str]) -> Environment:
        def value(name: str, default: str = "") -> str:
            raw = environ.get(name, default)
            return raw.strip() if raw else default

        revision = value("EMBEDDING_REVISION") or None
        cache = value("EMBEDDING_CACHE_DIR") or None
        api_key = value("EMBEDDING_API_KEY") or value("SILICONFLOW_API_KEY") or None
        return cls(
            deepseek_api_key=value("DEEPSEEK_API_KEY") or None,
            deepseek_model=value("DEEPSEEK_MODEL", "deepseek-flash"),
            deepseek_base_url=value("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            qq_app_id=value("QQ_APP_ID") or None,
            qq_app_secret=value("QQ_APP_SECRET") or None,
            qq_api_base_url=value("QQ_API_BASE_URL", "https://api.bot.qq.com"),
            qq_transport=_qq_transport_env(environ),
            qq_intents=_as_int_env(environ, "QQ_INTENTS", DEFAULT_QQ_INTENTS),
            qq_shard=_qq_shard_env(environ),
            qq_gateway_url=value("QQ_GATEWAY_URL") or None,
            embedding_model=value("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
            embedding_revision=revision,
            embedding_cache_dir=Path(cache).expanduser() if cache else None,
            embedding_kind=value("EMBEDDING_KIND", "local").strip().lower() or "local",
            embedding_api_key=api_key,
            embedding_base_url=value("EMBEDDING_BASE_URL").rstrip("/"),
            embedding_dimension=_as_int_env(environ, "EMBEDDING_DIMENSION", 512),
            embedding_max_tokens=_as_int_env(environ, "EMBEDDING_MAX_TOKENS", 512),
            runtime_dir=Path(value("RUNTIME_DIR", "runtime")).expanduser(),
            tracing_enabled=value("LANGCHAIN_TRACING_V2", "false").lower() == "true",
        )

    @classmethod
    def from_process(cls, environ: Mapping[str, str] | None = None) -> Environment:
        return cls.from_mapping(os.environ if environ is None else environ)

    @property
    def layout(self) -> RuntimeLayout:
        return RuntimeLayout(self.runtime_dir)

    def secret_names(self) -> tuple[str, ...]:
        return (
            "DEEPSEEK_API_KEY",
            "QQ_APP_ID",
            "QQ_APP_SECRET",
            "EMBEDDING_API_KEY",
            "SILICONFLOW_API_KEY",
        )

    def credential_status(self) -> dict[str, bool]:
        """只报告是否注入，不返回值本身。"""
        return {
            "DEEPSEEK_API_KEY": bool(self.deepseek_api_key),
            "QQ_APP_ID": bool(self.qq_app_id),
            "QQ_APP_SECRET": bool(self.qq_app_secret),
            "EMBEDDING_API_KEY": bool(self.embedding_api_key),
        }

    def as_public_payload(self) -> dict[str, Any]:
        return {
            "deepseek_model": self.deepseek_model,
            "deepseek_base_url": self.deepseek_base_url,
            "qq_api_base_url": self.qq_api_base_url,
            "qq_transport": self.qq_transport,
            "qq_intents": self.qq_intents,
            "qq_shard": list(self.qq_shard),
            "qq_gateway_url_host": _host_of(self.qq_gateway_url or ""),
            "embedding_kind": self.embedding_kind,
            "embedding_model": self.embedding_model,
            "embedding_revision": self.embedding_revision,
            "embedding_base_url_host": _host_of(self.embedding_base_url),
            "embedding_dimension": self.embedding_dimension,
            "embedding_max_tokens": self.embedding_max_tokens,
            "runtime_dir": str(self.runtime_dir),
            "tracing_enabled": self.tracing_enabled,
            "credentials": self.credential_status(),
        }


@dataclass(frozen=True)
class AffectionWeights:
    """好感度权重：均为正数且开心权重严格最大。"""

    keyword: float = 1.0
    day: float = 2.0
    happy: float = 5.0

    def issues(self) -> list[str]:
        problems: list[str] = []
        for name, value in (("keyword", self.keyword), ("day", self.day), ("happy", self.happy)):
            if value <= 0:
                problems.append(f"权重 {name} 必须为正数，当前 {value!r}")
        if self.happy <= self.keyword:
            problems.append("开心的权重必须严格大于关键词权重")
        if self.happy <= self.day:
            problems.append("开心的权重必须严格大于聊天日权重")
        return problems

    def to_payload(self) -> dict[str, float]:
        return {"keyword": self.keyword, "day": self.day, "happy": self.happy}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AffectionWeights:
        unknown = set(payload) - {"keyword", "day", "happy"}
        if unknown:
            raise ConfigError(f"未知权重字段：{sorted(unknown)}")
        try:
            return cls(
                keyword=float(payload.get("keyword", 1.0)),
                day=float(payload.get("day", 2.0)),
                happy=float(payload.get("happy", 5.0)),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"权重取值非法：{exc}") from exc


@dataclass(frozen=True)
class RuntimeConfig:
    """版本化非敏感配置；构造后不可变，更新产生新版本。"""

    version: int = 0
    effective_at: datetime = field(default_factory=utcnow)
    source: str = "default"
    affection_weights: AffectionWeights = field(default_factory=AffectionWeights)
    romance_threshold: float = 100.0
    keywords: tuple[str, ...] = ()
    monologue_wait_seconds: float = 60.0
    monologue_probability: float = 0.30
    qq_passive_window_seconds: float = 300.0
    qq_safety_margin_seconds: float = 10.0
    qq_max_replies_per_source: int = 4
    diagnostic_retention_days: int = 30
    max_reply_chars: int = 160
    max_reply_sentences: int = 3

    def issues(self) -> list[str]:
        """返回空列表表示合法；任何一条都足以拒绝应用。"""
        problems = list(self.affection_weights.issues())
        if self.romance_threshold <= 0:
            problems.append("恋爱门槛必须大于 0")
        if self.monologue_wait_seconds <= 0:
            problems.append("独白等待时长必须为正数")
        if not 0.0 <= self.monologue_probability <= 1.0:
            problems.append("独白概率必须在 0 与 1 之间")
        if self.qq_passive_window_seconds <= 0:
            problems.append("被动回复窗口必须为正数")
        if self.qq_safety_margin_seconds < 0:
            problems.append("发送余量不能为负数")
        if self.qq_safety_margin_seconds >= self.qq_passive_window_seconds:
            problems.append("发送余量必须小于被动回复窗口")
        if not 1 <= self.qq_max_replies_per_source <= 4:
            problems.append("每条来源消息的回复预算必须在 1 与 4 之间")
        if self.diagnostic_retention_days <= 0:
            problems.append("诊断保留天数必须为正数")
        if self.max_reply_chars <= 0 or self.max_reply_sentences <= 0:
            problems.append("回复长度与句数上限必须为正数")
        duplicated = _duplicate_keywords(self.keywords)
        if duplicated:
            problems.append(f"关键词重复：{sorted(duplicated)}")
        return problems

    def validate(self) -> RuntimeConfig:
        problems = self.issues()
        if problems:
            raise ConfigError("；".join(problems), details={"issues": problems})
        return self

    def next_version(self, *, source: str, at: datetime | None = None) -> RuntimeConfig:
        return replace(self, version=self.version + 1, effective_at=at or utcnow(), source=source)

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "effective_at": self.effective_at.isoformat(),
            "source": self.source,
            "affection_weights": self.affection_weights.to_payload(),
            "romance_threshold": self.romance_threshold,
            "keywords": list(self.keywords),
            "monologue_wait_seconds": self.monologue_wait_seconds,
            "monologue_probability": self.monologue_probability,
            "qq_passive_window_seconds": self.qq_passive_window_seconds,
            "qq_safety_margin_seconds": self.qq_safety_margin_seconds,
            "qq_max_replies_per_source": self.qq_max_replies_per_source,
            "diagnostic_retention_days": self.diagnostic_retention_days,
            "max_reply_chars": self.max_reply_chars,
            "max_reply_sentences": self.max_reply_sentences,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        base: RuntimeConfig | None = None,
        at: datetime | None = None,
    ) -> RuntimeConfig:
        """在 ``base``（默认出厂值）之上应用部分字段，保持未提及字段不变。"""
        base = base or cls()
        allowed = set(base.to_payload())
        unknown = set(payload) - allowed
        if unknown:
            raise ConfigError(f"未知配置字段：{sorted(unknown)}")
        data: dict[str, Any] = {
            "version": int(payload.get("version", base.version)),
            "source": str(payload.get("source", base.source)),
            "effective_at": at or _parse_time(payload.get("effective_at")) or base.effective_at,
        }
        if "affection_weights" in payload:
            weights = payload["affection_weights"]
            if not isinstance(weights, Mapping):
                raise ConfigError("affection_weights 必须是对象")
            data["affection_weights"] = AffectionWeights.from_payload(weights)
        for name in (
            "romance_threshold",
            "monologue_wait_seconds",
            "monologue_probability",
            "qq_passive_window_seconds",
            "qq_safety_margin_seconds",
        ):
            if name in payload:
                data[name] = _as_float(name, payload[name])
        for name in (
            "qq_max_replies_per_source",
            "diagnostic_retention_days",
            "max_reply_chars",
            "max_reply_sentences",
        ):
            if name in payload:
                data[name] = _as_int(name, payload[name])
        if "keywords" in payload:
            keywords = payload["keywords"]
            if isinstance(keywords, str) or not isinstance(keywords, (list, tuple)):
                raise ConfigError("keywords 必须是字符串数组")
            data["keywords"] = tuple(str(item) for item in keywords)
        return replace(base, **data)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(f"effective_at 不是合法时间：{value}") from exc
    return None


def _as_float(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是数值，得到布尔值")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是数值，得到 {value!r}") from exc


def _as_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是整数，得到布尔值")
    if isinstance(value, float) and not value.is_integer():
        raise ConfigError(f"{name} 必须是整数，得到 {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数，得到 {value!r}") from exc


def _duplicate_keywords(keywords: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    duplicated: set[str] = set()
    for keyword in keywords:
        if keyword in seen:
            duplicated.add(keyword)
        seen.add(keyword)
    return duplicated


class ConfigStore:
    """配置文件读写；非法更新不改动文件，原子替换合法版本。"""

    def __init__(self, path: Path, *, default: RuntimeConfig | None = None) -> None:
        self.path = path
        self._default = default or RuntimeConfig()

    def load(self) -> RuntimeConfig:
        if not self.path.exists():
            return self._default
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"配置文件不可读：{self.path}（{exc}）") from exc
        if not isinstance(payload, Mapping):
            raise ConfigError(f"配置文件必须是对象：{self.path}")
        return RuntimeConfig.from_payload(payload).validate()

    def validate_payload(self, payload: Mapping[str, Any]) -> RuntimeConfig:
        """校验候选配置；未应用任何变更。"""
        candidate = RuntimeConfig.from_payload(payload, base=self.load())
        return candidate.validate()

    def apply_payload(self, payload: Mapping[str, Any], *, source: str) -> RuntimeConfig:
        """在现有版本之上应用部分字段；非法配置抛错且不动文件。"""
        candidate = RuntimeConfig.from_payload(payload, base=self.load()).validate()
        return self._write(candidate, source=source)

    def apply(self, candidate: RuntimeConfig, *, source: str) -> RuntimeConfig:
        """应用完整候选配置；非法配置保持旧版本并抛错。"""
        candidate.validate()
        return self._write(candidate, source=source)

    def _write(self, candidate: RuntimeConfig, *, source: str) -> RuntimeConfig:
        current = self.load()
        applied = replace(
            candidate,
            version=current.version + 1,
            effective_at=utcnow(),
            source=source,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".config-", suffix=".json")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(applied.to_payload(), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return applied
