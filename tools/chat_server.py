"""本地开发聊天台：在浏览器里直接与 Bot 对话（不属于生产服务）。

生产入口 `sana-bot serve` 不加载本模块。本工具只做两件事：

1. 在服务端用 `QQ_APP_SECRET` 签名，把消息投给本机已运行的 `sana-bot serve` 的 `/qq/webhook`；
2. 只读查询运行目录的 SQLite，把本轮生成结果与检索证据交给页面。

密钥不下发到浏览器。回复按真实发送结果显示：openid 不是真实沙箱用户时会被平台拒绝，
页面明确标注「未送达」，不伪装成成功。

用法::

    uv run --env-file .env sana-bot serve --port 8000        # 另一个终端
    uv run --env-file .env python tools/chat_server.py       # 打开 http://127.0.0.1:8100

环境变量：`SANA_SERVE_URL`（默认 http://127.0.0.1:8000）、`CHAT_PORT`（默认 8100）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
import uvicorn

from sana_bot.adapters.qq_auth import sign_payload
from sana_bot.config import Environment
from sana_bot.timeutil import utc_now

HTML_PATH = Path(__file__).with_name("chat.html")
SERVE_URL = os.environ.get("SANA_SERVE_URL", "http://127.0.0.1:8000").rstrip("/")
CHAT_PORT = int(os.environ.get("CHAT_PORT", "8100"))

app = FastAPI(title="sana-bot 本地聊天台")


# --------------------------------------------------------------------------- 存储


def _environment() -> Environment:
    return Environment.from_process(os.environ)


def _connect() -> sqlite3.Connection:
    path = _environment().runtime_dir / "sana-bot.sqlite3"
    if not path.exists():
        raise FileNotFoundError(f"运行库不存在：{path}（先执行 sana-bot index build 然后 index activate）")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params)]


def _active_threshold(connection: sqlite3.Connection) -> float | None:
    rows = _rows(
        connection,
        """
        SELECT c.threshold FROM calibration_profile c
        JOIN index_snapshot s ON s.id = c.index_id
        WHERE s.status = 'active' AND c.stage = 'reply'
        """,
        (),
    )
    return rows[0]["threshold"] if rows else None


def _turn_view(connection: sqlite3.Connection, msg_id: str) -> dict[str, Any]:
    inbound = _rows(
        connection,
        "SELECT id, text FROM inbound_event WHERE message_id = ?",
        (msg_id,),
    )
    if not inbound:
        return {"msg_id": msg_id, "state": "unknown", "note": "消息尚未入站"}

    row = inbound[0]
    correlation_id = row["id"]
    turns = _rows(
        connection,
        "SELECT status FROM turn WHERE inbound_id = ? ORDER BY created_at DESC LIMIT 1",
        (correlation_id,),
    )
    deliveries = _rows(
        connection,
        "SELECT status, content, error, msg_seq FROM outbound_delivery"
        " WHERE correlation_id = ? ORDER BY created_at DESC LIMIT 1",
        (correlation_id,),
    )
    diagnostics = _rows(
        connection,
        "SELECT type, reason, payload FROM diagnostic_event WHERE correlation_id = ?",
        (correlation_id,),
    )

    retrieval: dict[str, Any] | None = None
    notes: list[str] = []
    for item in diagnostics:
        payload = json.loads(item["payload"] or "{}")
        if item["type"] == "low_match":
            retrieval = {
                "matched": False,
                "threshold": payload.get("threshold"),
                "candidate_count": payload.get("candidate_count"),
                "fallback_kind": payload.get("fallback_kind"),
                "candidates": payload.get("candidates") or [],
            }
        elif item["type"] == "delivery_error":
            notes.append(str(item["reason"]))
    if retrieval is None:
        retrieval = {"matched": True, "threshold": _active_threshold(connection), "candidates": []}

    base = {
        "msg_id": msg_id,
        "input_text": row["text"],
        "turn_status": turns[0]["status"] if turns else None,
        "retrieval": retrieval,
        "notes": notes,
    }
    if deliveries:
        delivery = deliveries[0]
        terminal = delivery["status"] not in ("prepared", "sending")
        if terminal:
            return {
                **base,
                "state": "done",
                "reply": delivery["content"],
                "delivery": {
                    "status": delivery["status"],
                    "error": delivery["error"],
                    "msg_seq": delivery["msg_seq"],
                },
            }
        return {**base, "state": "pending", "delivery": {"status": delivery["status"]}}
    return {**base, "state": "pending"}


# --------------------------------------------------------------------------- 投递


def _inject(*, openid: str, text: str) -> str:
    environment = _environment()
    if not environment.qq_app_secret:
        raise RuntimeError("缺少 QQ_APP_SECRET，无法签名投递")
    msg_id = f"web-{uuid.uuid4().hex[:16]}"
    body = json.dumps(
        {
            "op": 0,
            "s": int(time.time()),
            "t": "C2C_MESSAGE_CREATE",
            "id": f"evt-{uuid.uuid4().hex[:16]}",
            "appid": environment.qq_app_id or "app-1",
            "d": {
                "id": msg_id,
                "content": text,
                "timestamp": utc_now().isoformat(),
                "author": {"user_openid": openid},
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        f"{SERVE_URL}/qq/webhook",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Signature-Ed25519": sign_payload(environment.qq_app_secret, timestamp.encode() + body),
            "X-Signature-Timestamp": timestamp,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"服务返回 HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"服务拒绝入站：HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 {SERVE_URL}，请先运行 sana-bot serve（{exc.reason}）") from exc
    return msg_id


# --------------------------------------------------------------------------- 路由


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HTML_PATH, media_type="text/html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    environment = _environment()
    payload: dict[str, Any] = {
        "serve_url": SERVE_URL,
        "runtime_dir": str(environment.runtime_dir),
        "model": environment.deepseek_model,
        "embedding": (
            f"{environment.embedding_model} / {environment.embedding_kind} / "
            f"{environment.embedding_dimension}维"
        ),
    }
    try:
        connection = _connect()
        try:
            snapshots = _rows(
                connection,
                "SELECT id, snapshot_hash FROM index_snapshot WHERE status = 'active'",
                (),
            )
            payload["active_index"] = snapshots[0] if snapshots else None
            payload["threshold"] = _active_threshold(connection)
        finally:
            connection.close()
    except FileNotFoundError as exc:
        payload["error"] = str(exc)
    return payload


@app.post("/api/chat")
def chat(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", "")).strip()
    openid = str(payload.get("openid", "")).strip() or "openid-web-test"
    if not text:
        return {"error": "消息为空"}
    try:
        msg_id = _inject(openid=openid, text=text)
    except RuntimeError as exc:
        return {"error": str(exc)}
    return {"msg_id": msg_id, "openid": openid}


@app.get("/api/turn/{msg_id}")
def turn(msg_id: str) -> dict[str, Any]:
    connection = _connect()
    try:
        return _turn_view(connection, msg_id)
    finally:
        connection.close()


@app.post("/api/inspect")
async def inspect(payload: dict[str, Any]) -> dict[str, Any]:
    """对任意文本运行真实检索（只读），把命中的语义单元与分数摊开给页面看。"""
    import argparse

    from sana_bot.enums import RetrievalStage
    from sana_bot.retrieval.service import VectorRetriever
    from sana_bot.retrieval.vector_store import VectorStore
    from sana_bot.runtime import build_embedding_backend, open_context
    from sana_bot.state.models import StateSnapshot

    text = str(payload.get("text", "")).strip()
    if not text:
        return {"error": "文本为空"}

    context = open_context(argparse.Namespace(runtime_dir=str(_environment().runtime_dir)))
    try:
        snapshot = context.structures.active_snapshot()
        if snapshot is None:
            return {"error": "没有已激活的索引"}
        retriever = VectorRetriever(
            structures=context.structures,
            embeddings=build_embedding_backend(context, allow_download=False),
            store=VectorStore(structures=context.structures),
        )
        result = await retriever.retrieve(
            query=text,
            snapshot=StateSnapshot(id="inspect", turn_id="inspect", conversation_id="inspect"),
            index_id=snapshot.id,
            stage=RetrievalStage.REPLY,
            correlation_id="inspect",
        )
        return {
            "decision": result.decision.value,
            "threshold": result.threshold,
            "reason_code": result.reason_code,
            "notes": list(result.notes),
            "candidates": [
                {
                    "unit_id": item.unit_id,
                    "score": item.score,
                    "topic": item.topic,
                    "text": item.text,
                    "content_types": list(item.content_types),
                    "speaker_labels": list(item.speaker_labels),
                }
                for item in result.candidates
            ],
        }
    finally:
        context.close()


@app.get("/api/history")
def history(openid: str, limit: int = 30) -> dict[str, Any]:
    connection = _connect()
    try:
        users = _rows(
            connection,
            "SELECT id FROM user_identity WHERE app_id = ? AND user_openid = ?",
            (_environment().qq_app_id or "app-1", openid),
        )
        if not users:
            return {"openid": openid, "items": []}
        rows = _rows(
            connection,
            """
            SELECT i.message_id, i.text, i.created_at, d.content, d.status, d.error
            FROM inbound_event i
            LEFT JOIN outbound_delivery d ON d.correlation_id = i.id
            WHERE i.user_id = ?
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (users[0]["id"], max(1, min(limit, 100))),
        )
        items = [
            {
                "msg_id": row["message_id"],
                "text": row["text"],
                "created_at": row["created_at"],
                "reply": row["content"],
                "status": row["status"],
                "error": row["error"],
            }
            for row in reversed(rows)
        ]
        return {"openid": openid, "items": items}
    finally:
        connection.close()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=CHAT_PORT, log_level="warning")
