"""``sana-bot`` 命令入口。

默认输出 JSON（服务日志除外）。退出码：0 成功、2 输入/配置错误、3 资料未就绪、
4 外部服务失败。命令实现分散在 ``cli_*.py``，本模块只负责解析、分发与结果封装，
未知功能返回明确失败而不是伪成功。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sana_bot.errors import (
    EXIT_EXTERNAL_FAILURE,
    EXIT_OK,
    ConfigError,
    SanaError,
)

__all__ = ["main", "build_parser", "COMMANDS", "run"]


@dataclass(frozen=True)
class Option:
    """单个命令行选项。"""

    dest: str
    kind: str  # str | int | flag | choice
    flags: tuple[str, ...]
    required: bool = False
    choices: tuple[str, ...] = ()
    default: Any = None
    help: str = ""
    multiple: bool = False


@dataclass(frozen=True)
class Command:
    """一条命令：路径、选项与处理函数位置。"""

    path: tuple[str, ...]
    summary: str
    handler: str  # "module:function"
    options: tuple[Option, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


_RUNTIME_DIR = Option(
    dest="runtime_dir",
    kind="str",
    flags=("--runtime-dir",),
    default=None,
    help="运行目录，默认取配置 RUNTIME_DIR",
)

_USER = Option(
    dest="user",
    kind="str",
    flags=("--user",),
    required=True,
    help="内部 UserIdentity.id（不是 openid）",
)

COMMANDS: tuple[Command, ...] = (
    Command(
        path=("doctor",),
        summary="配置、存储、编码与可选真实连接检查，绝不打印凭据",
        handler="sana_bot.cli_doctor:run_doctor",
        options=(
            Option("offline", "flag", ("--offline",), help="仅离线检查，不调用 DeepSeek/QQ"),
            Option("live_model", "flag", ("--live-model",), help="真实 DeepSeek 小规模 smoke"),
            Option("qq", "flag", ("--qq",), help="QQ 账号与权限条件检查"),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("serve",),
        summary="单实例服务与任务处理器，不向公网暴露管理接口",
        handler="sana_bot.cli_doctor:run_serve",
        options=(
            Option("host", "str", ("--host",), default="127.0.0.1"),
            Option("port", "int", ("--port",), default=8000),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "parse"),
        summary="解析 DOCX 或资料包配置为不可变结构与类型节点",
        handler="sana_bot.cli_index:run_parse",
        options=(
            Option("source", "str", ("--source",), required=True, help="正式资料路径或资料包配置"),
            Option("output", "str", ("--output",), required=True, help="结构输出目录"),
            Option(
                "format",
                "choice",
                ("--format",),
                choices=("docx", "markdown-package"),
                default="docx",
                help="docx 为旧语义资料；markdown-package 为整理版资料包",
            ),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "review"),
        summary="输出标题树、字段归属、缺失、状态与负例清单",
        handler="sana_bot.cli_index:run_review",
        options=(
            Option("structure", "str", ("--structure",), help="STRUCTURE_ID"),
            Option("index", "str", ("--index",), help="INDEX_ID"),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "approve"),
        summary="登记核对过的结构与 catalog 批准记录（不等于校准通过）",
        handler="sana_bot.cli_index:run_approve",
        options=(
            Option("structure", "str", ("--structure",), required=True),
            Option(
                "catalog",
                "str",
                ("--catalog",),
                required=True,
                help="已核对 catalog 路径；资料包格式传包配置",
            ),
            Option("reviewer", "str", ("--reviewer",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "calibrate-split"),
        summary="在已确认样本上校准切分参数，产生固定 profile",
        handler="sana_bot.cli_index:run_calibrate_split",
        options=(
            Option("structure", "str", ("--structure",), required=True),
            Option("samples", "str", ("--samples",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "build"),
        summary="按批准配置构建快照，返回 INDEX_ID、snapshot_hash 与 bundle_hash",
        handler="sana_bot.cli_index:run_build",
        options=(
            Option("structure", "str", ("--structure",), required=True),
            Option(
                "split_profile",
                "str",
                ("--split-profile",),
                help="旧 semantic 格式的固定切分 profile ID；资料包格式禁用",
            ),
            Option("output", "str", ("--output",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "calibrate"),
        summary="校准 reply 门槛，报告绑定实际 snapshot_hash 与样本哈希",
        handler="sana_bot.cli_index:run_calibrate",
        options=(
            Option("index", "str", ("--index",), required=True),
            Option("stage", "choice", ("--stage",), required=True, choices=("reply", "monologue")),
            Option("samples", "str", ("--samples",), required=True),
            Option("reviewer", "str", ("--reviewer",), help="结构校准的人工复核者"),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "activate"),
        summary="校验批准、校准、报告与封存产物后原子激活版本",
        handler="sana_bot.cli_index:run_activate",
        options=(
            Option("index", "str", ("--index",), required=True),
            Option("validation_report", "str", ("--validation-report",), help="验收报告路径"),
            Option("expected_active", "str", ("--expected-active",), required=True, help="期望的当前 active；无 active 用 none"),
            Option("reason", "str", ("--reason",), help="切换原因，写入审计"),
            Option(
                "allow_offline_rehearsal",
                "flag",
                ("--allow-offline-rehearsal",),
                help="仅隔离演练库允许 offline 报告完成切换",
            ),
            Option("actor", "str", ("--actor",), help="操作者标识，写入审计"),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "rollback"),
        summary="重启校验后恢复到仍合格的旧版本，业务数据不变",
        handler="sana_bot.cli_index:run_rollback",
        options=(
            Option("index", "str", ("--index",), required=True),
            Option("expected_active", "str", ("--expected-active",), required=True),
            Option("reason", "str", ("--reason",), required=True),
            Option(
                "allow_offline_rehearsal",
                "flag",
                ("--allow-offline-rehearsal",),
                help="仅隔离演练库允许 offline 报告完成切换",
            ),
            Option("actor", "str", ("--actor",), help="操作者标识，写入审计"),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "evaluate"),
        summary="执行完整性、选择、空检索、行为段与输出检查，产物供人工评审",
        handler="sana_bot.cli_index:run_evaluate",
        options=(
            Option("index", "str", ("--index",), required=True),
            Option("samples", "str", ("--samples",), required=True),
            Option("mode", "choice", ("--mode",), required=True, choices=("offline", "live")),
            Option("output", "str", ("--output",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "approve-evaluation"),
        summary="按实际逐轮产物与必要门槛批准报告；拒绝伪 passed",
        handler="sana_bot.cli_index:run_approve_evaluation",
        options=(
            Option("index", "str", ("--index",), required=True),
            Option("report", "str", ("--report",), required=True),
            Option("reviewer", "str", ("--reviewer",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("index", "approve-calibration"),
        summary="批准与本快照绑定的 reply 校准报告",
        handler="sana_bot.cli_package:run_approve_calibration_command",
        options=(
            Option("calibration", "str", ("--calibration",), required=True),
            Option("reviewer", "str", ("--reviewer",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("state", "show"),
        summary="查看该用户活动会话的合法状态",
        handler="sana_bot.cli_state:run_show",
        options=(_USER, _RUNTIME_DIR),
    ),
    Command(
        path=("state", "set"),
        summary="设置该用户活动会话的合法状态，不改好感度",
        handler="sana_bot.cli_state:run_set",
        options=(
            _USER,
            Option("mood", "str", ("--mood",)),
            Option("tag", "str", ("--tag",), multiple=True),
            Option("scene", "str", ("--scene",)),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("relationship", "show"),
        summary="分数、状态、三项累计与配置版本",
        handler="sana_bot.cli_relationship:run_show",
        options=(_USER, _RUNTIME_DIR),
    ),
    Command(
        path=("config", "validate"),
        summary="检查权重、词表、门槛、概率与时长的合法性",
        handler="sana_bot.cli_config:run_validate",
        options=(
            Option("file", "str", ("--file",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("config", "apply"),
        summary="原子应用新配置版本，非法配置保留旧版本",
        handler="sana_bot.cli_config:run_apply",
        options=(
            Option("file", "str", ("--file",), required=True),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("logs", "query"),
        summary="按用户/时间/事件/原因检索有限前后文诊断",
        handler="sana_bot.cli_logs:run_query",
        options=(
            Option("user", "str", ("--user",)),
            Option("event", "str", ("--event",)),
            Option("since", "str", ("--since",)),
            Option("reason", "str", ("--reason",)),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("logs", "prune"),
        summary="清理诊断正文，保留关系与必要去重信息",
        handler="sana_bot.cli_logs:run_prune",
        options=(
            Option("older_than_days", "int", ("--older-than-days",), default=30),
            _RUNTIME_DIR,
        ),
    ),
    Command(
        path=("delivery", "review"),
        summary="仅处理 uncertain 发送，凭官方证据转 confirmed/rejected",
        handler="sana_bot.cli_delivery:run_review",
        options=(
            Option("id", "str", ("--id",), required=True),
            Option("result", "choice", ("--result",), required=True, choices=("confirmed", "rejected")),
            Option("evidence", "str", ("--evidence",), required=True),
            _RUNTIME_DIR,
        ),
    ),
)


def build_parser() -> argparse.ArgumentParser:
    """按 :data:`COMMANDS` 生成解析器。"""
    parser = argparse.ArgumentParser(
        prog="sana-bot",
        description="乾纱凪资料驱动与心情感知聊天管理入口",
    )
    parser.add_argument("--version", action="store_true", help="输出版本并退出")
    root = parser.add_subparsers(dest="command", metavar="COMMAND")

    grouped: dict[str, list[Command]] = {}
    order: list[str] = []
    for command in COMMANDS:
        head = command.path[0]
        if head not in grouped:
            grouped[head] = []
            order.append(head)
        grouped[head].append(command)

    for head in order:
        entries = grouped[head]
        if len(entries) == 1 and len(entries[0].path) == 1:
            target = root.add_parser(head, help=entries[0].summary, description=entries[0].summary)
            _add_options(target, entries[0])
            continue
        group = root.add_parser(head, help=_group_summary(head), description=_group_summary(head))
        sub = group.add_subparsers(dest=f"action_{head}", metavar="ACTION")
        for command in entries:
            target = sub.add_parser(
                command.path[1],
                help=command.summary,
                description=command.summary,
            )
            _add_options(target, command)
    return parser


def _add_options(target: argparse.ArgumentParser, command: Command) -> None:
    target.set_defaults(_handler=command.handler, _path=" ".join(command.path))
    for option in command.options:
        kwargs: dict[str, Any] = {
            "dest": option.dest,
            "required": option.required,
            "help": option.help or None,
            "action": _action_for(option),
        }
        if option.kind == "int":
            kwargs["type"] = int
        if option.choices:
            kwargs["choices"] = option.choices
        if option.kind != "flag":
            kwargs["default"] = option.default
        target.add_argument(*option.flags, **kwargs)


def _group_summary(head: str) -> str:
    summaries = {
        "index": "资料结构、切分与检索门槛的核对、校准与启用",
        "state": "合法状态查看与设置",
        "relationship": "长期好感度与恋爱状态",
        "config": "运行配置版本校验与应用",
        "logs": "诊断日志检索与清理",
        "delivery": "发送结果核对",
    }
    return summaries.get(head, head)


def _action_for(option: Option) -> str:
    if option.kind == "flag":
        return "store_true"
    if option.kind == "int":
        return "append" if option.multiple else "store"
    return "append" if option.multiple else "store"


def resolve_handler(spec: str) -> Callable[[argparse.Namespace], dict[str, Any]]:
    """解析 ``module:function``；缺失时返回明确未实现错误。"""
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:  # pragma: no cover - 仅在裁剪安装中出现
        raise SanaError(
            f"命令实现不可用：{module_name}（{exc}）",
            reason_code="not_implemented",
        ) from exc
    handler = getattr(module, attr, None)
    if handler is None:
        raise SanaError(
            f"命令实现缺失：{spec}",
            reason_code="not_implemented",
        )
    return handler


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    """执行一次命令并返回 JSON 载荷（不负责打印）。"""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if getattr(args, "version", False):
        from sana_bot import __version__

        return {"status": "ok", "version": __version__}
    handler_spec = getattr(args, "_handler", None)
    if handler_spec is None:
        parser.print_help(sys.stderr)
        raise ConfigError("未指定命令", reason_code="missing_command")
    handler = resolve_handler(handler_spec)
    payload = handler(args)
    if payload is None:
        payload = {}
    result = {"status": "ok", "command": args._path}
    result.update(payload)
    return result


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    """控制台入口。"""
    correlation_id = uuid.uuid4().hex
    try:
        payload = run(argv)
    except SanaError as exc:
        error = {
            "status": "error",
            "reason_code": exc.reason_code,
            "message": exc.message,
            "correlation_id": exc.correlation_id or correlation_id,
        }
        if exc.details:
            error["details"] = exc.details
        _emit(error)
        print(f"sana-bot: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        _emit({"status": "error", "reason_code": "interrupted", "correlation_id": correlation_id})
        return 130
    except Exception as exc:  # noqa: BLE001 - 未预期失败也要给出可诊断结果
        error = {
            "status": "error",
            "reason_code": "unexpected_failure",
            "message": f"{type(exc).__name__}: {exc}",
            "correlation_id": correlation_id,
        }
        if os.environ.get("SANABOT_DEBUG"):
            error["traceback"] = traceback.format_exc()
        _emit(error)
        print(f"sana-bot: {error['message']}", file=sys.stderr)
        return EXIT_EXTERNAL_FAILURE
    _emit(payload)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
