"""Actual chapter production and candidate selection remain enforceable at book scale."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import chapter_next, plan_rebudget, select_batch
from novel_ledger_core.content.planning import batch_selection_coverage
from novel_ledger_core.infra.scale import scale_contract_errors
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, atomic_text
from novel_ledger_core.ledger.ledger import commit_event, read_events, verify_ledger
from tests.test_book_outline import _phase, _story_map_outline


def _book(tmp_path: Path, *, through: int = 25) -> BookStore:
    outline = _story_map_outline()
    outline.update(book_words=80000, chapter_words_target=3200, total_chapters=25,
                   milestones=[{"kind": "book_climax", "chapter": 25}])
    chapters = []
    phases = []
    for start, end in ((1, 20), (21, 25)):
        ident = f"phase-{start}"
        phases.append(_phase(ident, start, end))
        for number in range(start, end + 1):
            chapters.append({"chapter": number, "volume": 1, "location": "市集", "present": ["主角"],
                             "phase_id": ident, "story_stage": "起局", "thread_refs": ["main", "ally"],
                             "clock_refs": ["clock-protagonist"], "beats": [{"id": "b1", "text": "找线索", "must": "线索"}]})
    source = tmp_path / "plan.json"
    source.write_text(json.dumps({"schema": "novel-ledger.plan.v2", "protagonist": "主角",
        "book_outline": outline, "phases": phases, "chapters": chapters,
        "volumes": {"1": {"spine": "追查凭据", "word_budget": 80000, "chapters_budget": 25}, "2": {"spine": "公开真相"}}}, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    init_project(project, plan_path=source, protagonist="主角", book_words=80000)
    store = BookStore(project)
    cfg = store.load_config()
    cfg.update(execution_mode="stage-agent", review_contract_version=2, plan_low_water=0)
    store.save_config(cfg)
    with store.transaction():
        for number in range(1, through + 1):
            atomic_text(store.chapter_md_path(number), "主角线索" + "主" * 2496)
            atomic_json(store.meta_path(number), {"chapter": number, "word_count": 2500})
            commit_event(store, number, {"named": ["主角"]})
        head = store.read_head()
        head.update(phase="idle", chapter=through, last_committed_ch=through, last_acked_ch=through)
        store.write_head(head)
    return store


def _select(store: BookStore, start: int, end: int) -> list[dict]:
    rows = [{"chapter": n, "goal": "取证", "conflict": "封锁", "outcome": "拿到线索", "tags": ["追索"], "settles": []} for n in range(start, end + 1)]
    other = copy.deepcopy(rows)
    for row in other:
        row["tags"] = ["潜入"]
    doc = {"schema": "novel-ledger.plan-candidates.v1", "batch_from": start, "batch_to": end,
           "candidates": [{"id": ident, "differentiator": ident, "failure_mode": "无回报", "chapters": skeleton}
                          for ident, skeleton in (("A", rows), ("B", other))],
           "verdict": {"selected_id": "A", "rationale": "有回报", "losers": [{"id": "B", "why": "风险更高"}]}}
    path = store.editorial_dir / "candidates.json"
    atomic_json(path, doc)
    select_batch(store, path=str(path))
    return rows


def _expanded(store: BookStore, rows: list[dict]) -> list[dict]:
    previous = store.load_plan()["chapters"][-1]
    result = []
    for row in rows:
        chapter = copy.deepcopy(previous)
        chapter.update({key: value for key, value in row.items() if key != "settles"})
        chapter["phase_id"] = "phase-extra"
        result.append(chapter)
    return result


def test_legal_short_chapters_can_rebudget_then_extend_without_changing_spine(tmp_path):
    store = _book(tmp_path)
    before = copy.deepcopy(store.load_plan()["book_outline"]["event_spine"])
    with store.transaction():
        assert chapter_next(store)["action"] == "extend_plan"
        revised = plan_rebudget(store, actor="planning-editor", reason="按实写字数续签章数")
        assert revised["total_chapters"] == 31
        rows = _select(store, 26, 31)
        result = store.extend_plan(_expanded(store, rows), phase_brief=_phase("phase-extra", 26, 31))
    assert result["added"] == list(range(26, 32))
    plan = store.load_plan()
    assert plan["book_outline"]["book_words"] == store.load_config()["book_words"] == 80000
    assert plan["book_outline"]["event_spine"] == before
    assert plan["volumes"]["1"]["word_budget"] == 80000
    assert plan["volumes"]["1"]["chapters_budget"] == 31
    assert scale_contract_errors(plan, store.load_config(), written_words=62500) == []
    assert verify_ledger(store)["consistent"]
    assert batch_selection_coverage(store, after_chapter=25)["issues"] == []
    assert any(event.get("action") == "plan.rebudget" for event in read_events(store))


def test_rebudget_shifts_only_unwritten_milestones_and_is_idempotent(tmp_path):
    store = _book(tmp_path, through=20)
    with store.transaction():
        revised = plan_rebudget(store, actor="planning-editor", reason="提前校准剩余容量")
        assert revised["total_chapters"] == 30
        assert revised["milestone_shifts"] == [{"chapter_before": 25, "chapter_after": 30, "kind": "book_climax"}]
        again = plan_rebudget(store, actor="planning-editor", reason="重复请求")
    assert again["changed"] is False
    plan = store.load_plan()
    plan["book_outline"]["total_chapters"] += 10
    assert any(error["code"] == "book_scale_total_chapters_mismatch" for error in scale_contract_errors(plan, store.load_config()))


@pytest.mark.parametrize("change", ["goal", "tags", "paid_hook"])
def test_extension_cannot_depart_from_the_selected_candidate(tmp_path, change):
    store = _book(tmp_path)
    with store.transaction():
        plan_rebudget(store, actor="planning-editor", reason="续签")
        rows = _select(store, 26, 27)
        chapters = _expanded(store, rows)
        if change == "paid_hook":
            chapters[0]["beats"][0]["effects"] = {"hooks": [{"id": "invented", "status": "paid"}]}
        elif change == "tags":
            chapters[0]["tags"] = ["潜入"]
        else:
            chapters[0]["goal"] = "别的方向"
        with pytest.raises(LedgerError, match="selected") as rejected:
            store.extend_plan(chapters, phase_brief=_phase("phase-extra", 26, 27))
    assert rejected.value.code == "plan_batch_selection_mismatch"
    assert max(c["chapter"] for c in store.load_plan()["chapters"]) == 25


def test_candidate_range_and_length_are_hard_boundaries(tmp_path):
    store = _book(tmp_path)
    with store.transaction():
        _select(store, 26, 27)
        path = store.editorial_dir / "candidates.json"
        doc = json.loads(path.read_text())
        for candidate in doc["candidates"]:
            candidate["chapters"] = candidate["chapters"][:1]
        atomic_json(path, doc)
        with pytest.raises(LedgerError) as mismatch:
            select_batch(store, path=str(path))
        assert mismatch.value.code == "plan_candidates_chapter_range"
        doc["batch_to"] = 1000000
        atomic_json(path, doc)
        with pytest.raises(LedgerError) as oversize:
            select_batch(store, path=str(path))
        assert oversize.value.code == "plan_candidates_batch_too_long"


def test_rebudget_keeps_a_future_book_climax_inside_the_revised_endgame(tmp_path):
    store = _book(tmp_path, through=20)
    with store.transaction():
        plan = store.load_plan()
        plan["book_outline"]["milestones"][0]["chapter"] = 22
        store.save_plan(plan)
        revised = plan_rebudget(store, actor="planning-editor", reason="保护未写终局位置")
    assert revised["total_chapters"] == 30
    assert revised["milestone_shifts"] == [{"chapter_before": 22, "chapter_after": 26, "kind": "book_climax"}]
    assert scale_contract_errors(store.load_plan(), store.load_config(), written_words=50000) == []


def test_checkpoint_detects_changed_expansion_after_selection(tmp_path):
    store = _book(tmp_path)
    with store.transaction():
        plan_rebudget(store, actor="planning-editor", reason="续签")
        rows = _select(store, 26, 27)
        store.extend_plan(_expanded(store, rows), phase_brief=_phase("phase-extra", 26, 27))
        plan = store.load_plan()
        plan["chapters"][-1]["beats"][0]["text"] = "未经复核换了一场戏"
        store.save_plan(plan)
    coverage = batch_selection_coverage(store, after_chapter=25)
    assert coverage["issues"] == [{"code": "plan_batch_selection_mismatch", "from_chapter": 26, "through_chapter": 27}]
