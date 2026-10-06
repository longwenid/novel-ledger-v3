"""扩纲批次候选头脑风暴：候选工件校验、择优治理事件、检查点回看与豁免。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    PLAN_CANDIDATES_SCHEMA,
    _plan_extend_context,
    select_batch,
    validate_plan,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, atomic_text, read_json
from novel_ledger_core.ledger.ledger import (
    commit_event,
    load_snapshot,
    read_events,
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
                "tags": ["对峙"],
                "beats": [{"id": f"b{ch}", "text": "推进成交", "must": "成交"}],
            }
            for ch in range(1, 9)
        ],
    }
    for chapter in plan["chapters"][3:]:
        chapter.update(goal="拿回凭据", conflict="当面拒收", outcome="约定明日答复")
        if chapter["chapter"] == 4:
            chapter["beats"][0]["effects"] = {"hooks": [{"id": "h-due", "status": "paid"}]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角")
    return BookStore(project)


def _commit(store: BookStore, chapter: int, delta: dict) -> None:
    atomic_text(store.chapter_md_path(chapter), f"第{chapter}章正文，故事继续。\n")
    commit_event(store, chapter, delta)


def _seed_committed(store: BookStore, through: int = 3) -> None:
    for chapter in range(1, through + 1):
        delta: dict = {}
        if chapter == through:
            delta = {
                "hooks": [
                    {"id": "h-due", "text": "对方约定期限", "status": "open", "due": 6},
                ]
            }
        _commit(store, chapter, delta)


def _candidates_doc(*, settle_a: bool = True, same_skin: bool = False) -> dict:
    candidate_b_tags = ["对峙"] if same_skin else ["暗线"]
    return {
        "schema": PLAN_CANDIDATES_SCHEMA,
        "batch_from": 4,
        "batch_to": 8,
        "candidates": [
            {
                "id": "A",
                "theme": "正面硬冲突逐步升级",
                "differentiator": "压力来源：外部期限步步紧逼",
                "failure_mode": "连续对抗无喘息，读者疲劳",
                "chapters": [
                    {
                        "chapter": chapter,
                        "goal": "拿回凭据",
                        "conflict": "当面拒收",
                        "outcome": "约定明日答复",
                        "tags": ["对峙"],
                        "settles": [{"id": "h-due"}] if settle_a and chapter == 4 else [],
                    } for chapter in range(4, 9)
                ],
            },
            {
                "id": "B",
                "theme": "绕开正面、暗线周旋",
                "differentiator": "章型排序：先暗线铺垫后集中爆发",
                "failure_mode": "铺垫期回报不足，前期平淡",
                "chapters": [
                    {
                        "chapter": chapter,
                        "goal": "另寻门路",
                        "conflict": "旧交不肯出面",
                        "outcome": "留下口信",
                        "tags": candidate_b_tags,
                        "settles": [],
                    } for chapter in range(4, 9)
                ],
            },
        ],
        "verdict": {
            "selected_id": "A",
            "rationale": "先清到期承诺，且与近章章型的重合可接受",
            "losers": [{"id": "B", "why": "把到期承诺再拖一批，逾期闸门会先停线"}],
        },
    }


def _write_candidates(store: BookStore, doc: dict) -> Path:
    path = store.editorial_dir / "plan-batch-candidates-4-8.json"
    atomic_json(path, doc)
    return path


def test_select_batch_seals_governance_event_and_writes_checks(tmp_path: Path, store: BookStore):
    _seed_committed(store)
    snap_before = load_snapshot(store)
    path = _write_candidates(store, _candidates_doc())

    result = select_batch(store, path=str(path), actor="planning-editor")

    assert result["action"] == "plan_batch_selected"
    assert result["selected_id"] == "A"
    events = read_events(store)
    sealed = [e for e in events if e.get("type") == "governance" and e.get("action") == "plan.batch_select"]
    assert len(sealed) == 1
    assert sealed[0]["selected_id"] == "A"
    assert sealed[0]["batch_from"] == 4 and sealed[0]["batch_to"] == 8
    # 无状态裁决：快照逐字段不变，verify 重放绿。
    assert load_snapshot(store) == snap_before
    assert verify_ledger(store)["consistent"] is True
    # 机器事实断言回写工件：A 覆盖到期 hook，B 未覆盖；A 与近章 tags 重合更高。
    doc = read_json(path)
    assert doc["recorded"] is True
    checks = doc["machine_checks"]
    assert checks["A"]["hooks_due_in_batch"] == 1
    assert checks["A"]["hook_settlement_missing"] == []
    assert checks["B"]["hook_settlement_missing"] == ["h-due"]
    assert checks["A"]["recent_tag_overlap"] == 1.0
    assert checks["B"]["recent_tag_overlap"] == 0.0


def test_select_batch_is_idempotent_guard(tmp_path: Path, store: BookStore):
    _seed_committed(store)
    path = _write_candidates(store, _candidates_doc())
    select_batch(store, path=str(path), actor="planning-editor")
    # 账本治理事件只追加一次的语义由调用纪律承担；重复记录至少不能破坏链。
    select_batch(store, path=str(path), actor="planning-editor")
    assert verify_ledger(store)["consistent"] is True


@pytest.mark.parametrize(
    "mutate, code",
    [
        pytest.param(
            lambda doc: doc["candidates"].__delitem__(1),
            "plan_candidates_too_few",
            id="too-few",
        ),
        pytest.param(
            lambda doc: doc["candidates"][0].__delitem__("failure_mode"),
            "plan_candidates_missing_failure_mode",
            id="missing-failure-mode",
        ),
        pytest.param(
            lambda doc: (
                doc["candidates"][0]["chapters"][0].__setitem__("settles", []),
                [row.__setitem__("tags", ["对峙"]) for row in doc["candidates"][1]["chapters"]],
            ),
            "plan_candidates_same_skin",
            id="same-skin",
        ),
        pytest.param(
            lambda doc: doc["verdict"].__setitem__("selected_id", "C"),
            "plan_candidates_selected_unknown",
            id="selected-unknown",
        ),
        pytest.param(
            lambda doc: doc["verdict"].__setitem__("losers", []),
            "plan_candidates_missing_loser_reason",
            id="missing-losers",
        ),
        pytest.param(
            lambda doc: doc["verdict"]["losers"][0].__setitem__("why", ""),
            "plan_candidates_missing_loser_reason",
            id="missing-loser-why",
        ),
    ],
)
def test_select_batch_rejects_walkthrough_shapes(tmp_path: Path, store: BookStore, mutate, code):
    _seed_committed(store)
    doc = _candidates_doc()
    mutate(doc)
    path = _write_candidates(store, doc)

    with pytest.raises(LedgerError) as excinfo:
        select_batch(store, path=str(path), actor="planning-editor")

    assert excinfo.value.code == code
    # 拒绝路径不追加任何治理事件。
    sealed = [
        e for e in read_events(store)
        if e.get("type") == "governance" and e.get("action") == "plan.batch_select"
    ]
    assert sealed == []


def test_batch_selection_event_rolls_back_with_interval(tmp_path: Path, store: BookStore):
    _seed_committed(store)
    path = _write_candidates(store, _candidates_doc())
    select_batch(store, path=str(path), actor="planning-editor")

    # 回滚到第 3 章：effective_chapter=3 的治理事件随区间一并撤销（批章拍重排时裁决作废重来）。
    rollback_ledger_to(store, 3)
    sealed = [
        e for e in read_events(store)
        if e.get("type") == "governance" and e.get("action") == "plan.batch_select"
    ]
    assert sealed == []
    assert verify_ledger(store)["consistent"] is True


def test_plan_brief_carries_batch_design(tmp_path: Path, store: BookStore):
    _seed_committed(store)

    ctx = _plan_extend_context(store, {"suggest_from": 4})

    design = ctx["batch_design"]
    assert "plan select-batch" in design["design_directive"]
    assert "plan-batch-candidates-" in design["design_directive"]
    assert [p["chapter"] for p in design["recent_patterns"]] == [1, 2, 3]
    assert design["recent_patterns"][0]["tags"] == ["对峙"]


def test_checkpoint_blocks_unwritten_batch_without_selection(tmp_path: Path, store: BookStore):
    from novel_ledger_core.control import autopilot

    _seed_committed(store)

    report = autopilot._checkpoint_report(store, chapter=3, previous=1, kinds=("batch",))

    codes = [item["code"] for item in report["blockers"]]
    assert "plan_batch_selection_missing" in codes
    assert report["batch_plan_review"]["selection_missing_from"] == 4
    assert report["review_required"] is True


def test_checkpoint_exempts_hatch_seed_batch(tmp_path: Path, store: BookStore):
    from novel_ledger_core.control import autopilot

    # 仅起手三章豁免；同时存在的后续扩纲批仍须择优。
    plan = store.load_plan()
    plan["chapters"] = plan["chapters"][:3]
    store.save_plan(plan)
    _seed_committed(store, through=2)

    report = autopilot._checkpoint_report(store, chapter=2, previous=1, kinds=("batch",))

    codes = [item["code"] for item in report["blockers"]]
    assert "plan_batch_selection_missing" not in codes


def test_validate_plan_flags_missing_selection_until_recorded(tmp_path: Path, store: BookStore):
    _seed_committed(store)

    flagged = validate_plan(store)
    codes = [item["code"] for item in flagged["warnings"]]
    assert "plan_batch_no_selection" in codes

    path = _write_candidates(store, _candidates_doc())
    select_batch(store, path=str(path), actor="planning-editor")

    resolved = validate_plan(store)
    codes = [item["code"] for item in resolved["warnings"]]
    assert "plan_batch_no_selection" not in codes


def test_select_batch_rejects_uncovered_due_hooks_without_waiver(tmp_path: Path, store: BookStore):
    """封存口硬拒：胜选候选留下批内到期 hook 未结算且无豁免——此前只是留痕事实，
    批中撞出「due 未排拍」就要改章拍再重封（r2→r3→r4 实测）。"""
    _seed_committed(store)
    doc = _candidates_doc(settle_a=False)
    path = _write_candidates(store, doc)

    with pytest.raises(LedgerError) as excinfo:
        select_batch(store, path=str(path), actor="planning-editor")

    assert excinfo.value.code == "plan_hook_settlement_uncovered"
    assert excinfo.value.details["uncovered"] == ["h-due"]
    sealed = [
        e for e in read_events(store)
        if e.get("type") == "governance" and e.get("action") == "plan.batch_select"
    ]
    assert sealed == []


def test_select_batch_waiver_seals_with_deferred_hooks_trail(tmp_path: Path, store: BookStore):
    _seed_committed(store)
    doc = _candidates_doc(settle_a=False)
    doc["verdict"]["deferred_hooks"] = [{"id": "h-due", "why": "作者明示延到下一批兑现"}]
    path = _write_candidates(store, doc)

    result = select_batch(store, path=str(path), actor="planning-editor")

    assert result["action"] == "plan_batch_selected"
    sealed = [
        e for e in read_events(store)
        if e.get("type") == "governance" and e.get("action") == "plan.batch_select"
    ]
    assert sealed[0]["deferred_hooks"] == [{"id": "h-due", "why": "作者明示延到下一批兑现"}]
    assert verify_ledger(store)["consistent"] is True


def test_select_batch_waiver_requires_explicit_why(tmp_path: Path, store: BookStore):
    _seed_committed(store)
    doc = _candidates_doc(settle_a=False)
    doc["verdict"]["deferred_hooks"] = [{"id": "h-due"}]
    path = _write_candidates(store, doc)

    with pytest.raises(LedgerError) as excinfo:
        select_batch(store, path=str(path), actor="planning-editor")

    assert excinfo.value.code == "invalid_args"


def test_validate_plan_warns_when_beats_cannot_carry_word_band(tmp_path: Path, store: BookStore):
    """拍数 × 字数带一致性：未写章拍数折算低于目标字数是 word_count_low 返工的结构性源头。"""
    _seed_committed(store)
    config = store.load_config()
    config["word_band"] = {"min": 2500, "max": 8000}
    store.save_config(config)

    report = validate_plan(store)

    codes = {(w.get("code"), w.get("chapter")) for w in report["warnings"]}
    # 默认带 aim=3200 → 需 5 场；夹具每章只有 1 拍，未写章（4–8）都该亮结构性偏瘦。
    thin = {chapter for code, chapter in codes if code == "scene_count_below_budget"}
    assert thin == {4, 5, 6, 7, 8}
    # 已写章（1–3）不警：拍数是历史事实。
    assert all(chapter not in thin for chapter in (1, 2, 3))
