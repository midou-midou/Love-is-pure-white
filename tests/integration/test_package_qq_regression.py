"""T057：新资料包下的 QQ 链路回归（去重、失败路径、隔离与预算）。"""

from __future__ import annotations

import pytest

from sana_bot.enums import DeliveryStatus
from tests.support.package_harness import build_package_dialogue


@pytest.fixture
def dialogue(tmp_path):
    built = build_package_dialogue(tmp_path / "qq")
    try:
        yield built
    finally:
        built.close()


@pytest.mark.asyncio
async def test_duplicate_inbound_does_not_send_twice(dialogue):
    item = dialogue.submit(message_id="m-1", text="今天社团的小猫很粘人")
    first = await dialogue.service.handle(item)
    second = await dialogue.service.handle(item)
    assert first.ok is True
    assert second.status == "duplicate"
    assert len(dialogue.transport.sent) == 1


@pytest.mark.asyncio
async def test_rejected_delivery_is_reported_without_retry(dialogue):
    dialogue.transport.queue(DeliveryStatus.REJECTED)
    outcome = await dialogue.service.handle(
        dialogue.submit(message_id="m-1", text="今天社团的小猫很粘人")
    )
    assert outcome.ok is False
    assert outcome.delivery_status == DeliveryStatus.REJECTED.value
    assert len(dialogue.transport.sent) == 1


@pytest.mark.asyncio
async def test_transport_exception_marks_uncertain(dialogue):
    dialogue.transport.exc = RuntimeError("network down")
    outcome = await dialogue.service.handle(
        dialogue.submit(message_id="m-1", text="今天社团的小猫很粘人")
    )
    assert outcome.delivery_status == DeliveryStatus.UNCERTAIN.value
    assert outcome.reason_code == "delivery_exception"
    rows = dialogue.database.query("SELECT status FROM outbound_delivery")
    assert rows and rows[-1]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_sessions_remain_isolated(dialogue):
    dialogue.model.queue_text("回复甲")
    await dialogue.service.handle(
        dialogue.submit(message_id="m-1", text="甲的社团话题", user_key="openid-a")
    )
    dialogue.model.queue_text("回复乙")
    await dialogue.service.handle(
        dialogue.submit(message_id="m-2", text="乙的社团话题", user_key="openid-b", offset=5)
    )
    assert len(dialogue.transport.sent) == 2
    assert dialogue.transport.sent[0].user_key == "openid-a"
    assert dialogue.transport.sent[1].user_key == "openid-b"
    latest = dialogue.generation_requests()[-1]
    joined = " ".join(str(item["content"]) for item in latest.messages)
    assert "甲的社团话题" not in joined


@pytest.mark.asyncio
async def test_msg_seq_stays_within_conservative_budget(dialogue):
    for index in range(5):
        dialogue.model.queue_text(f"第{index}次回复")
        outcome = await dialogue.service.handle(
            dialogue.submit(message_id=f"m-{index}", text=f"第{index}次提问", offset=index * 5)
        )
        if index < 4:
            assert outcome.ok, outcome
    seqs = [message.msg_seq for message in dialogue.transport.sent]
    assert seqs, "至少有一次尝试发送"
    assert max(seqs) <= 4
    assert len(dialogue.transport.sent) >= 4


@pytest.mark.asyncio
async def test_output_violation_blocks_sending(dialogue):
    dialogue.model.queue_text("我在听。")
    outcome = await dialogue.service.handle(
        dialogue.submit(message_id="m-1", text="今天社团的小猫很粘人")
    )
    # 句末句号与改写结果都要受控；不得把违规文本发出去
    if outcome.ok:
        assert not outcome.text.endswith("。")
    else:
        assert outcome.reason_code in {"output_blocked", "output_blocked_after_rewrite"}
    assert len(dialogue.transport.sent) <= 1


@pytest.mark.asyncio
async def test_relationship_ledger_not_touched_on_failure(tmp_path):
    from sana_bot.enums import RelationshipStatus

    built = build_package_dialogue(
        tmp_path / "ledger", relationship=RelationshipStatus.NON_ROMANTIC
    )
    try:
        before = built.database.query("SELECT * FROM relationship")
        await built.service.handle(
            built.submit(message_id="m-1", text="今天社团的小猫很粘人")
        )
        after = built.database.query("SELECT * FROM relationship")
        assert before == after == []
    finally:
        built.close()
