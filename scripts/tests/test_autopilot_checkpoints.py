from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control import autopilot
from novel_ledger_core.control.autopilot import resume_run, run_checkpoint
from novel_ledger_core.infra.util import (
    atomic_json, canonical_json, chinese_word_count, read_json, sha256_text,
)
from novel_ledger_core.ledger.ledger import _event_digest

from scripts.tests.test_autopilot import _store


def _chapter_artifacts(store, chapter: int) -> None:
    prose = f"第{chapter}章，主角完成试炼并付出了代价。\n"
    digest = "sha256:" + sha256_text(prose.rstrip("\n"))
    md = store.chapter_md_path(chapter)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(prose, encoding="utf-8")
    atomic_json(store.meta_path(chapter), {
        "chapter": chapter, "prose_hash": digest,
        "l1_summary": f"第{chapter}章完成试炼。", "word_count": chinese_word_count(prose),
    })
    atomic_json(store.summary_path(chapter), {
        "chapter": chapter, "prose_hash": digest,
        "l1_summary": f"第{chapter}章完成试炼。",
    })
    atomic_json(store.ack_path(chapter), {
        "chapter": chapter, "prose_hash": digest, "verdict": "pass", "quotes": [],
    })
    with store.quality_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"chapter": chapter, "event": "ack", "verdict": "pass"}) + "\n")
    previous = ""
    if store.events_path.exists():
        lines = store.events_path.read_text(encoding="utf-8").splitlines()
        if lines:
            previous = str(json.loads(lines[-1]).get("hash") or "")
    event = {"chapter": chapter, "state_delta": {}}
    if previous:
        event["prev_hash"] = previous
    event["hash"] = _event_digest(event)
    with store.events_path.open("ab") as handle:
        handle.write(canonical_json(event))


def test_checkpoint_reads_latest_ten_chapters_and_complete_quality_records(tmp_path: Path):
    store = _store(tmp_path, chapters=25)
    for chapter in range(1, 21):
        _chapter_artifacts(store, chapter)
    snap = read_json(store.snapshot_path)
    snap["chapter"] = 20
    atomic_json(store.snapshot_path, snap)
    _record_batch_selection(store, batch_from=21, batch_to=25)

    report = autopilot._checkpoint_report(store, chapter=20, previous=10, kinds=("batch",))

    assert report["range"] == {"from_chapter": 11, "through_chapter": 20}
    assert len(report["recent_summaries"]) == 10
    assert report["recent_summaries"][0]["chapter"] == 11
    assert report["quality"]["ack_pass"] == 10
    assert report["ledger"]["chapters"] == list(range(11, 21))
    assert report["review_required"] is False
    assert report["blockers"] == []
    # 有留痕时批次回看节存在且指向该批（缺留痕的阻断由 test_plan_brainstorm 覆盖）。
    review = report["batch_plan_review"]
    assert review["next_unwritten"] == 21
    assert review["selection_missing_from"] is None
    assert review["selections_in_window"][0]["batch_from"] == 21


def _record_batch_selection(store, *, batch_from: int, batch_to: int, selected_id: str = "A") -> None:
    """Create a real, exact candidate selection matching the actual planned batch."""
    from novel_ledger_core.control.pipeline import select_batch

    plan = store.load_plan()
    rows = []
    for chapter in plan["chapters"]:
        if batch_from <= chapter["chapter"] <= batch_to:
            chapter.update(goal="完成试炼", conflict="试炼代价", outcome="取得证据", tags=["试炼"])
            rows.append({key: chapter[key] for key in ("chapter", "goal", "conflict", "outcome", "tags")})
    store.save_plan(plan)
    alternative = [{**row, "tags": ["揭示"]} for row in rows]
    path = store.editorial_dir / f"plan-batch-candidates-{batch_from}-{batch_to}.json"
    atomic_json(path, {"schema": "novel-ledger.plan-candidates.v1", "batch_from": batch_from, "batch_to": batch_to,
        "candidates": [
            {"id": "A", "theme": "试炼", "differentiator": "行动", "failure_mode": "代价不足", "chapters": rows},
            {"id": "B", "theme": "揭示", "differentiator": "秘密", "failure_mode": "过早揭秘", "chapters": alternative}],
        "verdict": {"selected_id": selected_id, "rationale": "承接现有试炼方向",
                    "losers": [{"id": "B" if selected_id == "A" else "A", "why": "不合本批目标"}]}})
    select_batch(store, path=path, actor="planning-editor", reason="准确匹配实际扩纲范围")


def test_checkpoint_detects_recent_event_tampering_without_full_book_audit(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    _chapter_artifacts(store, 1)
    snap = read_json(store.snapshot_path)
    snap["chapter"] = 1
    atomic_json(store.snapshot_path, snap)
    event = json.loads(store.events_path.read_text(encoding="utf-8"))
    event["state_delta"] = {"facts": [{"text": "被篡改"}]}
    store.events_path.write_bytes(canonical_json(event))

    report = autopilot._checkpoint_report(store, chapter=1, previous=0, kinds=("volume",))
    assert "ledger_window_invalid" in {item["code"] for item in report["blockers"]}


def test_checkpoint_reads_large_selected_artifacts_and_full_summaries(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    _chapter_artifacts(store, 1)
    snap = read_json(store.snapshot_path)
    snap["chapter"] = 1
    atomic_json(store.snapshot_path, snap)
    prose = "当前章完整正文。" * 45_000 + "章末完整标记。\n"
    summary = "当前章摘要完整因果。" * 15_000 + "摘要末尾标记。"
    store.chapter_md_path(1).write_text(prose, encoding="utf-8")
    digest = "sha256:" + sha256_text(prose.rstrip("\n"))
    atomic_json(store.meta_path(1), {"chapter": 1, "prose_hash": digest, "l1_summary": summary, "word_count": chinese_word_count(prose)})
    atomic_json(store.summary_path(1), {"chapter": 1, "prose_hash": digest, "l1_summary": summary})
    atomic_json(store.ack_path(1), {"chapter": 1, "prose_hash": digest, "verdict": "pass", "quotes": ["章末完整标记"]})
    report = autopilot._checkpoint_report(store, chapter=1, previous=0, kinds=("batch",))
    assert report["blockers"] == []
    assert report["recent_summaries"] == [{"chapter": 1, "summary": summary}]


def test_checkpoint_large_quality_and_ledger_events_do_not_hide_range(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    _chapter_artifacts(store, 1)
    snap = read_json(store.snapshot_path)
    snap["chapter"] = 1
    atomic_json(store.snapshot_path, snap)
    event = json.loads(store.events_path.read_text(encoding="utf-8"))
    event["state_delta"] = {"facts": [{"text": "相关依据。" * 240_000 + "末尾事件证据"}]}
    event["hash"] = _event_digest(event)
    store.events_path.write_bytes(canonical_json(event))
    with store.quality_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"chapter": 1, "event": "submit", "verdict": "pass", "detail": "完整质量证据。" * 20_000}) + "\n")
    report = autopilot._checkpoint_report(store, chapter=1, previous=0, kinds=("batch",))
    assert report["quality"]["ack_pass"] == 1
    assert report["ledger"]["chapters"] == [1]
    assert report["blockers"] == []
    assert not report["quality"]["truncated"]
    assert not report["ledger"]["truncated"]


def test_volume_checkpoint_loads_relevant_review_and_flags_overdue_hook(tmp_path: Path):
    store = _store(tmp_path, chapters=3)
    plan = store.load_plan()
    plan["chapters"][0]["volume"] = 1
    plan["chapters"][1]["volume"] = 1
    plan["chapters"][2]["volume"] = 2
    plan["volumes"] = {
        "vol-0001": {"spine": "试炼与代价", "goal": "通过初试"},
        "vol-0002": {"spine": "代价追索", "goal": "找到真相"},
    }
    store.save_plan(plan)
    _chapter_artifacts(store, 2)
    snap = read_json(store.snapshot_path)
    snap["chapter"] = 2
    snap["hooks"] = [{"id": "h-old", "status": "deferred", "due": 1, "text": "试炼代价"}]
    atomic_json(store.snapshot_path, snap)

    report = autopilot._checkpoint_report(store, chapter=2, previous=1, kinds=("volume",))

    assert report["volume_review"]["current_volume"] == "vol-0001"
    assert report["volume_review"]["next_volume"] == "vol-0002"
    assert report["volume_review"]["current_spine"] == "试炼与代价"
    assert len(report["recent_summaries"]) == 1
    assert report["review_required"] is True
    assert {issue["code"] for issue in report["blockers"]} == {"overdue_hooks"}


def test_dirty_batch_checkpoint_pauses_and_rechecks_after_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """会话宿主在章界跑 run checkpoint：脏报告以 review_required 暂停，处置后 resume 重查同一章。"""
    store = _store(tmp_path, chapters=12)
    head = store.read_head()
    head.update({"chapter": 10, "phase": "idle", "last_committed_ch": 10, "last_acked_ch": 10})
    store.write_head(head)
    calls: list[int] = []

    def dirty_report(store, *, chapter, previous, kinds):
        calls.append(chapter)
        return {
            "chapter": chapter, "kinds": list(kinds), "review_required": True,
            "blockers": [{"code": "missing_ack", "chapters": [chapter]}],
        }

    monkeypatch.setattr(autopilot, "_checkpoint_report", dirty_report)
    result = run_checkpoint(store)
    assert result["action"] == "run_paused"
    assert result["reason"] == "review_required"
    assert calls == [10]
    state = read_json(store.autopilot_state_path)
    assert state["status"] == "paused"
    assert state["last_checkpoint"]["chapter"] == 10
    assert state.get("last_checkpoint_ch", 0) < 10

    resume_run(store)
    again = run_checkpoint(store)
    assert again["reason"] == "review_required"
    assert calls == [10, 10]
    assert read_json(store.autopilot_state_path).get("last_checkpoint_ch", 0) < 10


def test_volume_boundary_emits_review_signal_before_tenth_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = _store(tmp_path, chapters=3)
    plan = store.load_plan()
    for index, chapter in enumerate(plan["chapters"]):
        chapter["volume"] = 1 if index < 2 else 2
    store.save_plan(plan)
    head = store.read_head()
    head.update({"chapter": 2, "phase": "idle", "last_committed_ch": 2, "last_acked_ch": 2})
    store.write_head(head)
    monkeypatch.setattr(autopilot, "_checkpoint_report", lambda store, *, chapter, previous, kinds: {
        "chapter": chapter, "kinds": list(kinds), "review_required": False, "blockers": [],
    })

    result = run_checkpoint(store)
    assert result["action"] == "run_checkpoint" and result["stop"] is False
    events = [json.loads(line) for line in store.autopilot_events_path.read_text().splitlines()]
    kinds = [tuple(e["kinds"]) for e in events if e["event"] == "quality_checkpoint"]
    assert kinds == [("volume",)]
    assert len([event for event in events if event["event"] == "volume_review"]) == 1


def test_completion_audit_flags_content_errors_even_with_ok_true():
    blockers = autopilot._completion_audit_blockers({
        "ok": True, "action": "book_audit", "ledger_consistent": True,
        "quote_issues": [{"chapter": 2}], "numeric_issue_count": 1,
    })
    assert {item["code"] for item in blockers} == {"quote_issues", "numeric_issue_count"}


def test_completion_audit_pauses_for_unpaid_book_level_promise():
    blockers = autopilot._completion_audit_blockers({
        "ok": True, "action": "book_audit", "ledger_consistent": True,
        "unresolved_long_term_commitments": [{"id": "commitment-seal"}],
    })
    assert blockers == [{"code": "unresolved_long_term_commitments", "count": 1}]


def test_run_checkpoint_standalone_writes_report_and_is_idempotent(tmp_path: Path):
    """会话宿主形态的独立检查点：机器部分零模型调用，幂等（游标共用）。"""
    store = _store(tmp_path, chapters=25)
    for chapter in range(1, 21):
        _chapter_artifacts(store, chapter)
    snap = read_json(store.snapshot_path)
    snap["chapter"] = 20
    atomic_json(store.snapshot_path, snap)
    head = store.read_head()
    head.update({"chapter": 21, "phase": "idle", "last_committed_ch": 20, "last_acked_ch": 20})
    store.write_head(head)
    _record_batch_selection(store, batch_from=21, batch_to=25)

    first = autopilot.run_checkpoint(store)
    assert first["action"] == "run_checkpoint" and first["stop"] is False
    report_path = Path(first["checkpoint_path"])
    assert report_path.exists()
    assert read_json(store.autopilot_state_path)["last_checkpoint_ch"] == 20

    # 幂等：同章重跑不出第二份报告，也不推进游标。
    second = autopilot.run_checkpoint(store)
    assert second["action"] == "run_checkpoint" and second["skipped"] == "not_due"
    assert second["last_checkpoint_ch"] == 20
    reports = list((store.run_dir / "checkpoints").glob("ch-0020-*.json"))
    assert len(reports) == 1


def test_run_checkpoint_standalone_pauses_on_review_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = _store(tmp_path, chapters=12)
    head = store.read_head()
    head.update({"chapter": 11, "phase": "idle", "last_committed_ch": 10, "last_acked_ch": 10})
    store.write_head(head)

    def dirty_report(store, *, chapter, previous, kinds):
        return {
            "chapter": chapter, "kinds": list(kinds), "review_required": True,
            "blockers": [{"code": "missing_ack", "chapters": [chapter]}],
            "recent_summaries": [{"chapter": chapter, "l1_summary": "very long window dump"}],
        }

    monkeypatch.setattr(autopilot, "_checkpoint_report", dirty_report)
    result = autopilot.run_checkpoint(store)
    assert result["action"] == "run_paused" and result["stop"] is True
    assert result["reason"] == "review_required"
    # CLI 响应瘦身：报告全文留盘上，回执只带结论与路径——整窗摘要不得回显进宿主上下文。
    slim = result["checkpoint"]
    assert slim["review_required"] is True and slim["blockers"]
    assert "recent_summaries" not in slim
    assert "checkpoint_path" in result
    state = read_json(store.autopilot_state_path)
    assert state["status"] == "paused" and state["last_checkpoint"]["chapter"] == 10
    events = [json.loads(line) for line in store.autopilot_events_path.read_text().splitlines()]
    assert any(event["event"] == "paused" and event.get("reason") == "review_required" for event in events)
