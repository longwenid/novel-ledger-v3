from __future__ import annotations

from novel_ledger_core.ledger.ledger import (
    EMPTY_SNAPSHOT,
    apply_event,
    render_now_card,
    select_debts,
    state_near,
)


def test_cancelled_debt_stops_reaching_the_writer():
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(snap, {"chapter": 1, "state_delta": {"debts": [
        {"id": "d1", "who": "主角", "text": "欠掌柜十两", "status": "open"},
    ]}})
    snap = apply_event(snap, {"chapter": 800, "state_delta": {"debts": [
        {"id": "d1", "status": "cancelled"},
    ]}})

    assert select_debts(None, location="市集", present=["主角"], cap=8, snapshot=snap)[0] == []
    now = render_now_card(
        None, protagonist="主角", chapter_plan={"location": "市集"},
        volume_spine="继续查旧账", snapshot=snap,
    )
    assert now["owes"] == ""
    near = state_near(None, pinned_names=["主角"], pinned_cap=1, recent_cap=0, snapshot=snap)
    assert near["open_debt_ids"] == []


def test_new_debt_wins_when_equal_priority_debts_exceed_pack_cap():
    snap = dict(EMPTY_SNAPSHOT)
    for chapter in range(1, 10):
        snap = apply_event(snap, {"chapter": chapter, "state_delta": {"debts": [
            {"id": f"d{chapter}", "who": "主角", "text": f"欠账{chapter}", "status": "open"},
        ]}})

    shown, omitted = select_debts(None, location="", present=["主角"], cap=8, snapshot=snap)
    assert omitted == 1
    assert "d9" in [debt["id"] for debt in shown]
    assert "d1" not in [debt["id"] for debt in shown]
    now = render_now_card(None, protagonist="主角", chapter_plan={}, volume_spine="旧账", snapshot=snap)
    assert "欠账9" in now["owes"]
    near = state_near(None, pinned_names=["主角"], pinned_cap=1, recent_cap=0, snapshot=snap)
    assert "d9" in near["open_debt_ids"]
    assert "d1" not in near["open_debt_ids"]
