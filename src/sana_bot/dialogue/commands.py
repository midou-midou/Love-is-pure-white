"""QQ 普通用户命令：只读的 ``/关系`` 查询。

查询不触发模型、不改变情绪与计分、不创建独白机会；管理命令不能被 QQ 消息执行。
查询文案同样通过统一输出规范。
"""

from __future__ import annotations

from dataclasses import dataclass

from sana_bot.ports import DialogueDraft

__all__ = ["CommandKind", "parse_command", "RELATIONSHIP_COMMAND", "MANAGEMENT_COMMANDS"]

RELATIONSHIP_COMMAND = "/关系"

#: 这些命令只能由本地管理 CLI 执行，QQ 消息输入一律拒绝
MANAGEMENT_COMMANDS: tuple[str, ...] = (
    "/状态",
    "/设置心情",
    "/好感度设置",
    "/配置",
    "/审核",
    "/激活",
)


@dataclass(frozen=True)
class CommandKind:
    name: str
    allowed_from_qq: bool
    read_only: bool


def parse_command(text: str) -> CommandKind | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    name = stripped.split()[0]
    if name == RELATIONSHIP_COMMAND:
        return CommandKind(name=name, allowed_from_qq=True, read_only=True)
    if name in MANAGEMENT_COMMANDS:
        return CommandKind(name=name, allowed_from_qq=False, read_only=False)
    return CommandKind(name=name, allowed_from_qq=False, read_only=False)


def relationship_draft(payload: dict[str, object]) -> DialogueDraft:
    """把关系查询结果写成一句角色化只读回复（句末无句号）。"""
    score = payload.get("score", 0)
    status = payload.get("romance_status", "non_romantic")
    label = "已经在交往了" if status == "romantic" else "还只是普通相处"
    text = f"（抱臂扭过头）……好感度是 {int(score)} 点，{label}，问这个做什么"
    return DialogueDraft(
        kind="status",
        text=text,
        reason_code="relationship_query",
        details={"read_only": True, "scored": False},
    )


def rejection_draft(name: str) -> DialogueDraft:
    """QQ 用户不能执行管理命令时的合规模糊拒绝。"""
    return DialogueDraft(
        kind="status",
        text="（皱起眉）这个我这边做不了，你直接跟我说就好",
        reason_code="management_command_rejected",
        details={"command": name, "allowed_from_qq": False},
    )
