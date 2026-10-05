"""计划层缺陷通道回归：同章重复 knowledge 死锁 + plan patch-chapter。

背景（实测事故）：章拍 effects 声明同章同 (who, topic_id) 的两条 knowledge 后，
提交端 knowledge_duplicate_topic 硬禁第二条 delta，expected_delta_missing 又要求
两条逐条回填——互斥构成死锁，任何提交都无法通过；而 extend 只能向后追加、
rework-patch 只动正文，计划层没有任何修补通道，只能手改真源。

锁定的不变量：
- plan_shape_reject 在章拍入真源前硬拒同章重复 knowledge（extend/init/patch 共用）；
- 提交端对重复 expected 只收口第一条（死锁解锁），重复进 warnings 指路 patch；
- plan patch-chapter 只改未写章的 beats/location/present，治理事件留痕，
  批次封存哈希同步刷新；已提交章与越界字段硬拒。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import chapter_next, submit_output
from novel_ledger_core.content.gates import (
    _expected_delta_issues,
    expected_delta_duplicate_warnings,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json
from novel_ledger_core.infra.story_map import plan_shape_reject
from tests.decoupled_helpers import advance_to_assembly, make_chapter, make_plan


def _knowledge(who: str, topic: str, claim: str) -> dict:
    return {"who": who, "topic_id": topic, "claim": claim, "stance": "knows", "source": "亲见"}


def _beat(beat_id: str, text: str, must: str, effects: dict) -> dict:
    return {"id": beat_id, "required": True, "text": text, "must": must, "effects": effects}


def _proj(tmp_path: Path, chapters: list[dict] | None = None) -> BookStore:
    plan = make_plan(chapters or [make_chapter(1), make_chapter(2)])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(proj)


def test_plan_side_guard_rejects_duplicate_knowledge_effects() -> None:
    dup = make_chapter(
        3,
        beats=[
            _beat("b1", "主角认出旧印缺口是旧友刻的", "旧印", {"knowledge": [_knowledge("主角", "t1", "旧印缺口是旧友刻的")]}),
            _beat("b2", "主角再次确认缺口出自旧友之手", "缺口", {"knowledge": [_knowledge("主角", "t1", "第二次确认同一认知")]}),
        ],
    )
    with pytest.raises(LedgerError) as excinfo:
        plan_shape_reject([dup], origin="test")
    assert "duplicate knowledge" in excinfo.value.message
    assert excinfo.value.details["issues"][0]["topic_id"] == "t1"


def test_plan_extend_rejects_duplicate_knowledge(tmp_path: Path) -> None:
    store = _proj(tmp_path)
    dup = make_chapter(
        3,
        beats=[
            _beat("b1", "主角认出旧印缺口是旧友刻的", "旧印", {"knowledge": [_knowledge("主角", "t1", "第一次确认")]}),
            _beat("b2", "主角再次确认缺口出自旧友之手", "缺口", {"knowledge": [_knowledge("主角", "t1", "第二次确认")]}),
        ],
    )
    with pytest.raises(LedgerError) as excinfo:
        store.extend_plan([dup])
    assert "duplicate knowledge" in excinfo.value.message


def test_expected_delta_dedupes_duplicate_knowledge_and_warns() -> None:
    planned = [_knowledge("主角", "t1", "缺口是旧友刻的"), _knowledge("主角", "t1", "同一认知的第二次表述")]
    actual = [{**_knowledge("主角", "t1", "缺口是旧友刻的"), "quote": "正文中逐字摘录的证据"}]
    # 第二条不可满足声明不再判 missing：收口只对第一条生效。
    assert _expected_delta_issues({"knowledge": actual}, {"expected_delta": {"knowledge": planned}}) == []
    warnings = expected_delta_duplicate_warnings({"expected_delta": {"knowledge": planned}})
    assert warnings and warnings[0]["code"] == "plan_effects_duplicate_topic"
    assert "patch-chapter" in warnings[0]["hint"]


def test_duplicate_expected_submits_with_warning(tmp_path: Path) -> None:
    """遗留带毒计划（守卫生效前已签）在提交端解锁：合并 delta 即通过 + 告警指路。"""
    beats = [
        _beat("b1", "主角认出旧印缺口是旧友刻的", "旧印", {"knowledge": [_knowledge("主角", "t1", "缺口是旧友刻的")]}),
        _beat("b2", "主角再次确认缺口出自旧友之手", "缺口", {"knowledge": [_knowledge("主角", "t1", "同一认知的第二次表述")]}),
    ]
    plan = make_plan([make_chapter(1, beats=beats)])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(make_plan([make_chapter(1)]), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    # 模拟守卫生效前的遗留带毒计划：save_plan 直写入库（新防线会拒它在入口出现）。
    store = BookStore(proj)
    store.save_plan(plan)
    prose = "主角端详旧印，认出缺口出自旧友之手，又对着火光再次确认了一遍。"
    advance_to_assembly(store, prose=prose, chapter=1)
    pack = read_json(store.current_pack_path)
    assert len(pack["expected_delta"]["knowledge"]) == 2, "前置：计划确带重复声明"
    output = {
        "prose": prose,
        "l1_summary": "主角认出旧印缺口出自旧友。",
        "state_delta": {
            "knowledge": [{**_knowledge("主角", "t1", "缺口是旧友刻的"), "quote": "认出缺口出自旧友之手"}],
            "named": ["主角"],
            "new_names": [],
        },
        "memory": {},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1", "b2"],
    }
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"
    assert any(w["code"] == "plan_effects_duplicate_topic" for w in accepted.get("warnings", []))


def test_patch_chapter_merges_duplicate_and_audits(tmp_path: Path) -> None:
    beats = [
        _beat("b1", "主角认出旧印缺口是旧友刻的", "旧印", {"knowledge": [_knowledge("主角", "t1", "缺口是旧友刻的")]}),
        _beat("b2", "主角再次确认缺口出自旧友之手", "缺口", {"knowledge": [_knowledge("主角", "t1", "同一认知的第二次表述")]}),
    ]
    plan = make_plan([make_chapter(1, beats=beats)])
    proj = tmp_path / "bookproj"
    proj.mkdir()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(make_plan([make_chapter(1)]), ensure_ascii=False), encoding="utf-8")
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    # 模拟守卫生效前的遗留带毒计划：save_plan 直写入库。
    store = BookStore(proj)
    store.save_plan(plan)
    merged = [
        _beat("b1", "主角认出旧印缺口是旧友刻的", "旧印", {"knowledge": [_knowledge("主角", "t1", "缺口是旧友刻的")]}),
        _beat("b2", "主角对着火光细看缺口纹路", "缺口", {}),
    ]
    from novel_ledger_core.control.pipeline import plan_patch_chapter

    result = plan_patch_chapter(
        store, chapter=1,
        patch={"beats": merged},
        actor="managing-editor", reason="merge duplicate knowledge effects (plan-level deadlock)",
    )
    assert result["action"] == "plan_chapter_patch" and result["changed_fields"] == ["beats"]
    assert store.chapter_plan(1)["beats"][1]["effects"] == {}
    events = [json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    patches = [e for e in events if e.get("action") == "plan.chapter_patch" or e.get("type") == "governance" and e.get("action") == "plan.chapter_patch"]
    assert patches, "governance event must be appended"

    # 修补后的章必须可写：pack 重建、提交通过、无重复告警。
    chapter_next(store)
    prose = "主角端详旧印，认出缺口出自旧友之手，又对着火光细看缺口纹路。"
    advance_to_assembly(store, prose=prose, chapter=1)
    pack = read_json(store.current_pack_path)
    assert len(pack["expected_delta"]["knowledge"]) == 1
    output = {
        "prose": prose,
        "l1_summary": "主角认出旧印缺口出自旧友。",
        "state_delta": {
            "knowledge": [{**_knowledge("主角", "t1", "缺口是旧友刻的"), "quote": "认出缺口出自旧友之手"}],
            "named": ["主角"],
            "new_names": [],
        },
        "memory": {},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1", "b2"],
    }
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"
    assert not [w for w in accepted.get("warnings", []) if w.get("code") == "plan_effects_duplicate_topic"]


def test_patch_chapter_rejects_committed_and_forbidden_fields(tmp_path: Path) -> None:
    from novel_ledger_core.control.pipeline import plan_patch_chapter

    store = _proj(tmp_path)
    head = store.read_head()
    head.update({"phase": "idle", "chapter": 2, "last_committed_ch": 1, "last_acked_ch": 1})
    store.write_head(head)
    with pytest.raises(LedgerError) as committed_err:
        plan_patch_chapter(store, chapter=1, patch={"beats": [_beat("b1", "补一场戏", "戏", {})]},
                           actor="m", reason="r")
    assert committed_err.value.code == "chapter_already_committed"
    with pytest.raises(LedgerError) as field_err:
        plan_patch_chapter(store, chapter=2, patch={"goal": "换目标"}, actor="m", reason="r")
    assert field_err.value.code == "plan_patch_field_forbidden"
    with pytest.raises(LedgerError) as empty_err:
        plan_patch_chapter(store, chapter=2, patch={"beats": []}, actor="m", reason="r")
    assert empty_err.value.code == "missing_beats"
