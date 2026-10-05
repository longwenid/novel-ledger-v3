"""Append-only hook/relation governance must preserve chapter history and replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    _canon_drift,
    close_hook,
    close_relation,
    defer_hook,
    merge_hook,
    rename_relation,
    resync_baseline,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, atomic_text
from novel_ledger_core.ledger.ledger import (
    commit_event,
    load_snapshot,
    read_events,
    repair_ledger,
    replay_events,
    rollback_ledger_to,
    verify_ledger,
)


@pytest.fixture()
def store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷。",
        "chapters": [
            {
                "chapter": ch,
                "location": "市集",
                "present": ["主角"],
                "beats": [{"id": f"b{ch}", "text": "推进成交", "must": "成交"}],
            }
            for ch in range(1, 5)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角")
    return BookStore(project)


def _commit(store: BookStore, chapter: int, delta: dict) -> None:
    atomic_text(store.chapter_md_path(chapter), f"第{chapter}章正文，故事继续。\n")
    commit_event(store, chapter, delta)


def test_governance_appends_without_changing_chapter_events(store: BookStore):
    _commit(
        store,
        1,
        {
            "hooks": [
                {"id": "h-old", "text": "旧伏笔", "due": 2, "status": "open"},
                {"id": "h-new", "text": "新伏笔", "due": 3, "status": "open"},
            ],
            "relations": [
                {"who": "张铁", "target": "执事孙典", "kind": "借剑", "status": "open"},
                {"who": "张铁", "target": "孙典", "kind": "结盟", "status": "open"},
            ],
        },
    )
    _commit(store, 2, {"facts": [{"who": "主角", "text": "第二章已发生"}]})
    original = store.events_path.read_text(encoding="utf-8").splitlines(keepends=True)
    original_hashes = [event["hash"] for event in read_events(store)]

    defer_hook(store, hook_id="h-old", new_due=10, actor="总编", reason="第六卷回收")
    close_hook(store, hook_id="h-old", actor="总编", reason="第二章取消了承诺")
    merge_hook(store, from_id="h-old", into_id="h-new", actor="总编", reason="确认同一伏笔")
    rename_relation(store, from_name="执事孙典", to_name="孙典", actor="总编", reason="统一别名")
    close_relation(
        store, who="张铁", target="孙典", kind_substring="借剑", actor="总编", reason="剑已归还"
    )

    events = read_events(store)
    assert store.events_path.read_text(encoding="utf-8").splitlines(keepends=True)[:2] == original
    assert [event["hash"] for event in events[:2]] == original_hashes
    assert len(events) == 7
    assert all(event["type"] == "governance" for event in events[2:])
    assert [event["action"] for event in events[2:]] == [
        "hook.defer", "hook.close", "hook.merge", "relation.rename", "relation.close"
    ]
    assert all(event["actor"] == "总编" and event["reason"] for event in events[2:])
    assert all(event["effective_chapter"] == 2 and "chapter" not in event for event in events[2:])
    snap = load_snapshot(store)
    assert snap == replay_events(store)
    assert snap["chapter"] == 2
    assert {h["id"] for h in snap["hooks"]} == {"h-new"}
    assert {r["target"] for r in snap["relations"]} == {"孙典"}
    assert next(r for r in snap["relations"] if r["kind"] == "借剑")["status"] == "closed"
    assert verify_ledger(store)["consistent"]
    assert verify_ledger(store)["events"] == 2  # governance is not another chapter

    # A later chapter can still commit; governance does not consume a chapter number.
    _commit(store, 3, {"facts": [{"who": "主角", "text": "第三章已发生"}]})
    assert verify_ledger(store)["consistent"]


def test_governance_repair_and_rollback_respect_effective_chapter(store: BookStore):
    _commit(store, 1, {"hooks": [{"id": "h", "text": "线索", "due": 2}]})
    _commit(store, 2, {})
    defer_hook(store, hook_id="h", new_due=9, actor="总编", reason="排期改变")
    original_first = read_events(store)[0]

    damaged = load_snapshot(store)
    damaged["hooks"][0]["due"] = 999
    atomic_json(store.snapshot_path, damaged)
    assert not verify_ledger(store)["consistent"]
    assert repair_ledger(store)["repaired"]
    assert load_snapshot(store)["hooks"][0]["due"] == 9
    assert read_events(store)[0] == original_first

    _commit(store, 3, {})
    rollback_ledger_to(store, 3)
    assert len(read_events(store)) == 3
    assert load_snapshot(store)["hooks"][0]["due"] == 9
    rollback_ledger_to(store, 2)
    assert len(read_events(store)) == 1
    assert load_snapshot(store)["hooks"][0]["due"] == 2
    assert verify_ledger(store)["event_chain_ok"]


def test_tampered_governance_event_blocks_repair(store: BookStore):
    _commit(store, 1, {"hooks": [{"id": "h", "text": "线索", "due": 2}]})
    defer_hook(store, hook_id="h", new_due=9, actor="总编", reason="排期改变")
    events = read_events(store)
    events[-1]["new_due"] = 999
    atomic_text(store.events_path, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    assert not verify_ledger(store)["event_chain_ok"]
    with pytest.raises(LedgerError) as exc:
        repair_ledger(store)
    assert exc.value.code == "event_chain_broken"


def test_canon_restamp_appends_after_governance_tail(store: BookStore):
    atomic_text(store.chapter_md_path(1), "第一章正文，故事继续。\n")
    commit_event(
        store, 1,
        {"hooks": [{"id": "h", "text": "线索", "due": 2}]},
        {"canon_sha": store.canon_fingerprint()},
    )
    defer_hook(store, hook_id="h", new_due=9, actor="总编", reason="排期改变")
    originals = store.events_path.read_text(encoding="utf-8").splitlines(keepends=True)
    cards = store.load_kb()
    cards.append({"id": "canon-added", "kind": "world", "title": "新规", "body": "新规则。"})
    store.save_kb(cards)
    assert _canon_drift(store)["changed"]

    assert resync_baseline(store, restamp_canon=True)["canon_restamped"]
    assert store.events_path.read_text(encoding="utf-8").splitlines(keepends=True)[:2] == originals
    events = read_events(store)
    assert events[-1]["type"] == "governance"
    assert events[-1]["action"] == "canon.restamp"
    assert events[-1]["canon_sha"] == store.canon_fingerprint()
    assert _canon_drift(store)["changed"] is False
    assert _canon_drift(store)["since_chapter"] == 1
    assert verify_ledger(store)["consistent"]
    assert resync_baseline(store, restamp_canon=True)["canon_restamped"] is False


def test_merged_hook_id_cannot_be_reintroduced_by_later_chapter(store: BookStore):
    _commit(store, 1, {"hooks": [{"id": "old", "text": "线索", "due": 2}, {"id": "new", "text": "同一线索", "due": 3}]})
    merge_hook(store, from_id="old", into_id="new", actor="总编", reason="重复登记")
    before = store.events_path.read_bytes()
    with pytest.raises(LedgerError) as exc:
        commit_event(store, 2, {"hooks": [{"id": "old", "status": "paid"}]})
    assert exc.value.code == "retired_hook_id"
    assert store.events_path.read_bytes() == before


def test_renamed_relation_name_cannot_reappear_in_later_delta(store: BookStore):
    _commit(store, 1, {"relations": [{"who": "张铁", "target": "执事孙典", "kind": "盟约"}]})
    rename_relation(store, from_name="执事孙典", to_name="孙典", actor="总编", reason="统一别名")
    before = store.events_path.read_bytes()
    with pytest.raises(LedgerError) as exc:
        commit_event(store, 2, {"relations": [{"who": "张铁", "target": "执事孙典", "kind": "借剑"}]})
    assert exc.value.code == "retired_relation_name"
    assert store.events_path.read_bytes() == before


def test_hook_merge_keeps_latest_lifecycle_state_under_surviving_id(store: BookStore):
    _commit(store, 1, {"hooks": [{"id": "new", "text": "原线索", "due": 10}, {"id": "old", "text": "重复线索", "due": 3}]})
    _commit(store, 2, {"hooks": [{"id": "old", "status": "paid", "text": "已兑现"}]})
    merge_hook(store, from_id="old", into_id="new", actor="总编", reason="同一线索已兑现")
    hook = load_snapshot(store)["hooks"][0]
    assert hook["id"] == "new"
    assert hook["status"] == "paid"
    assert hook["text"] == "已兑现"
    assert verify_ledger(store)["consistent"]
