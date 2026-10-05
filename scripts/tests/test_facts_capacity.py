from __future__ import annotations

from novel_ledger_core.ledger.ledger import EMPTY_SNAPSHOT, apply_event, facts_for_pack


def test_pinned_facts_fill_capacity_without_retaining_unpinned_history():
    """A full pinned set must not turn the ordinary fact pool into an unbounded log."""
    snap = dict(EMPTY_SNAPSHOT)
    for chapter in range(1, 101):
        facts = [{"who": "主角", "text": f"关键事实{i}", "pin": True} for i in range(1, 13)] if chapter == 1 else []
        facts.append({"who": "主角", "text": f"第{chapter}章临时事实"})
        snap = apply_event(snap, {"chapter": chapter, "state_delta": {"facts": facts}})

    stored = snap["entities"]["主角"]["facts"]
    assert len(stored) == 12
    assert [fact["text"] for fact in stored] == [f"关键事实{i}" for i in range(1, 13)]
    assert facts_for_pack(stored, cap=3) == ["关键事实10", "关键事实11", "关键事实12"]


def test_pack_prioritizes_pinned_when_they_fill_visible_slots():
    facts = [
        {"text": "关键事实一", "pin": True},
        {"text": "关键事实二", "pin": True},
        {"text": "关键事实三", "pin": True},
        {"text": "最新临时事实", "pin": False},
    ]
    assert facts_for_pack(facts, cap=3) == ["关键事实一", "关键事实二", "关键事实三"]


def test_repeated_pinned_fact_does_not_grow_or_fill_pack_with_duplicates():
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(snap, {"chapter": 1, "state_delta": {"facts": [
        {"who": "主角", "text": "左腕永久伤痕", "pin": True},
        {"who": "主角", "text": "真名另有来历", "pin": True},
    ]}})
    for chapter in range(2, 1001):
        snap = apply_event(snap, {"chapter": chapter, "state_delta": {"facts": [
            {"who": "主角", "text": "左腕永久伤痕", "pin": chapter % 2 == 0},
        ]}})

    stored = snap["entities"]["主角"]["facts"]
    assert len(stored) == 2
    assert {fact["text"] for fact in stored} == {"左腕永久伤痕", "真名另有来历"}
    assert all(fact["pin"] for fact in stored)
    assert set(facts_for_pack(stored, cap=2)) == {"左腕永久伤痕", "真名另有来历"}


def test_repeated_fact_can_be_promoted_to_pinned():
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(snap, {"chapter": 1, "state_delta": {"facts": [
        {"who": "主角", "text": "身世尚未查明"},
    ]}})
    snap = apply_event(snap, {"chapter": 300, "state_delta": {"facts": [
        {"who": "主角", "text": "身世尚未查明", "pin": True},
    ]}})
    assert snap["entities"]["主角"]["facts"] == [{"text": "身世尚未查明", "pin": True}]
