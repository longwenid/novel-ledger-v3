"""事件哈希链：真源防篡改。

events.jsonl 是唯一真源；重放 vs 快照对"两边一起被改"是盲的。链让单点篡改
（改中段、删中段）在 verify 阶段即可被发现，且 repair 拒绝从断裂的真源重建——
否则 repair 会把篡改内容洗白进 snapshot。
"""

from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_text
from novel_ledger_core.ledger.ledger import (
    commit_event,
    read_events,
    repair_ledger,
    rollback_ledger_to,
    verify_ledger,
)

PLAN = {
    "title": "",
    "protagonist": "主角",
    "volume_spine": "第一卷。",
    "chapters": [
        {"chapter": c, "location": "市集", "present": ["主角"], "tags": [], "beats": [{"id": f"b{c}", "text": "推进成交", "must": "成交"}]}
        for c in (1, 2, 3, 4)
    ],
}


def _make_project(tmp_path: Path) -> BookStore:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    return BookStore(proj)


def _commit(store: BookStore, chapter: int, text: str) -> None:
    atomic_text(store.chapter_md_path(chapter), f"第{chapter}章正文，{text}\n")
    commit_event(store, chapter, {"facts": [{"who": "主角", "text": text}]})


def _rewrite_events(store: BookStore, events: list[dict]) -> None:
    body = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events)
    atomic_text(store.events_path, body)


def test_chain_is_valid_after_serial_commits(tmp_path: Path):
    store = _make_project(tmp_path)
    for chapter in (1, 2, 3):
        _commit(store, chapter, f"第{chapter}章的事实")

    events = read_events(store)
    assert len(events) == 3
    assert "hash" in events[0] and "prev_hash" not in events[0], "创世事件不带 prev_hash"
    assert events[1]["prev_hash"] == events[0]["hash"]
    assert events[2]["prev_hash"] == events[1]["hash"]

    report = verify_ledger(store)
    assert report["event_chain_ok"], report
    assert report["consistent"], report["diffs"]


def test_tampered_event_content_breaks_hash(tmp_path: Path):
    store = _make_project(tmp_path)
    for chapter in (1, 2, 3):
        _commit(store, chapter, f"第{chapter}章的事实")

    events = read_events(store)
    events[1]["state_delta"]["facts"][0]["text"] = "被篡改的事实"
    _rewrite_events(store, events)

    report = verify_ledger(store)
    assert not report["event_chain_ok"]
    codes = {issue["code"] for issue in report["event_chain_issues"]}
    assert "event_hash_mismatch" in codes
    assert not report["consistent"], "篡改事件后 verify 必须整体红"


def test_deleted_event_breaks_chain_link(tmp_path: Path):
    store = _make_project(tmp_path)
    for chapter in (1, 2, 3):
        _commit(store, chapter, f"第{chapter}章的事实")

    events = read_events(store)
    del events[1]
    _rewrite_events(store, events)

    report = verify_ledger(store)
    assert not report["event_chain_ok"]
    codes = {issue["code"] for issue in report["event_chain_issues"]}
    assert "event_chain_broken" in codes, "删除中段事件必须断链"


def test_repair_refuses_broken_chain(tmp_path: Path):
    store = _make_project(tmp_path)
    for chapter in (1, 2, 3):
        _commit(store, chapter, f"第{chapter}章的事实")

    events = read_events(store)
    events[0]["state_delta"]["facts"][0]["text"] = "洗白尝试"
    _rewrite_events(store, events)

    try:
        repair_ledger(store)
    except LedgerError as exc:
        assert exc.code == "event_chain_broken"
    else:
        raise AssertionError("链断时 repair 必须拒绝，否则篡改被洗白进 snapshot")


def test_unsealed_events_fail_chain_verification(tmp_path: Path):
    """无哈希事件即断链：本 skill 不兼容升级前的裸 JSONL 账本。"""
    store = _make_project(tmp_path)
    raw = [
        {"chapter": 1, "ts": "2026-09-01T00:00:00", "state_delta": {"facts": [{"who": "主角", "text": "旧事实一"}]}},
        {"chapter": 2, "ts": "2026-09-01T00:01:00", "state_delta": {"facts": [{"who": "主角", "text": "旧事实二"}]}},
    ]
    _rewrite_events(store, raw)
    atomic_text(store.chapter_md_path(1), "旧一\n")
    atomic_text(store.chapter_md_path(2), "旧二\n")

    report = verify_ledger(store)
    assert not report["event_chain_ok"], report
    codes = {issue["code"] for issue in report["event_chain_issues"]}
    assert codes == {"event_hash_missing"}, codes


def test_rollback_then_commit_keeps_chain_green(tmp_path: Path):
    store = _make_project(tmp_path)
    for chapter in (1, 2, 3):
        _commit(store, chapter, f"第{chapter}章的事实")

    rollback_ledger_to(store, 3)
    _commit(store, 3, "重写后的第三章事实")

    report = verify_ledger(store)
    assert report["event_chain_ok"], report
    assert report["consistent"], report["diffs"]
    events = read_events(store)
    assert events[-1]["state_delta"]["facts"][0]["text"] == "重写后的第三章事实"
