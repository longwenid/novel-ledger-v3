from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control.pipeline import (
    _creative_advisory,
    audit_book,
    chapter_next,
    validate_plan,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.content.views import _render_story_focus
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.story_map import plan_story_map_errors, story_focus
from novel_ledger_core.infra.util import LedgerError


def _plan(**extra) -> dict:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：主角要回那张被扣的凭据。",
        "volumes": {
            "1": {"spine": "第一卷：拿回凭据。", "goal": "拿回凭据。", "word_budget": 100000},
        },
        "chapters": [
            {
                "chapter": 1,
                "volume": 1,
                "location": "市集",
                "present": ["主角", "掌柜"],
                "tags": ["市集", "凭据"],
                "beats": [{"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"}],
            }
        ],
    }
    plan.update(extra)
    outline = plan.get("book_outline")
    if isinstance(outline, dict) and isinstance(outline.get("event_spine"), dict):
        plan["schema"] = "novel-ledger.plan.v2"
    return plan


def _init(tmp_path: Path, plan: dict, name: str = "proj", **kwargs) -> BookStore:
    plan_path = tmp_path / f"{name}-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / name
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", **kwargs)
    return BookStore(proj)


def _codes(result: dict) -> set[str]:
    return {w["code"] for w in result["warnings"]}


def _story_map_outline() -> dict:
    acts = [
        {"name": "起局", "volumes": "1", "arc": "取证", "stakes": "生存"},
        {"name": "破局", "volumes": "2", "arc": "公开", "stakes": "秩序"},
    ]
    return {
        "acts": acts,
        "event_spine": {
            "schema": "novel-ledger.event-spine.v1",
            "mainline": {
                "id": "main", "name": "凭据主线", "purpose": "拿回并公开凭据",
                "mainline_link": "直接承载终局", "open_stage": "起局", "payoff_stage": "破局",
                "participants": ["主角"], "events": [
                    {"id": "main-open", "stage": "起局", "event": "找到残页", "change": "获得线索"},
                    {"id": "main-payoff", "stage": "破局", "event": "公开原件", "change": "改变秩序"},
                ],
            },
            "subplots": [{
                "id": "ally", "name": "同盟支线", "purpose": "检验信任",
                "mainline_link": "同盟提供原件入口", "open_stage": "起局", "payoff_stage": "破局",
                "participants": ["主角", "盟友"], "events": [
                    {"id": "ally-open", "stage": "起局", "event": "交换半份线索", "change": "有限合作"},
                    {"id": "ally-payoff", "stage": "破局", "event": "共同作证", "change": "形成互信"},
                ],
            }],
            "timelines": [
                {
                    "id": f"clock-{kind}", "kind": kind, "name": f"{kind}时钟", "start_state": "倒计时开始",
                    "events": [
                        {"id": f"{kind}-open", "order": 1, "stage": "起局", "event": "窗口收窄", "deadline": "封锁前", "consequence": "线索断裂"},
                        {"id": f"{kind}-payoff", "order": 2, "stage": "破局", "event": "抵达终点", "deadline": "会审前", "consequence": "无法公开"},
                    ],
                }
                for kind in ("protagonist", "antagonist", "world")
            ],
            "tension_curve": [
                {"stage": "起局", "level": 2, "mode": "build", "pressure": "封锁", "turn": "发现残页", "payoff": "脱身", "next_imbalance": "追捕"},
                {"stage": "破局", "level": 5, "mode": "climax", "pressure": "会审", "turn": "取得原件", "payoff": "公开", "next_imbalance": "重建"},
            ],
        },
    }


def _phase(ident: str, start: int, end: int, stage: str = "起局") -> dict:
    suffix = "open" if stage == "起局" else "payoff"
    event_refs = [f"main-{suffix}", f"ally-{suffix}", f"protagonist-{suffix}"]
    return {
        "id": ident, "name": f"阶段{ident}", "story_stage": stage,
        "chapter_start": start, "chapter_end": end, "objective": "推进凭据链", "climax": "取得阶段证据",
        "mainline_event_ref": f"main-{suffix}", "subplot_event_refs": [f"ally-{suffix}"],
        "timeline_event_refs": [f"protagonist-{suffix}"], "tension_stage": stage,
        "entry_state": "凭据链尚未闭合", "exit_state": "取得阶段证据",
        "event_changes": {ref: f"推进{ref}" for ref in event_refs},
        "tension_change": "压力升级并在阶段高潮形成兑现",
    }


def test_missing_book_outline_is_silent(tmp_path: Path):
    """老项目没有 book_outline：不产生任何大纲相关告警，passed 照常。"""
    store = _init(tmp_path, _plan())
    result = validate_plan(store)
    assert result["passed"] is True
    assert not {c for c in _codes(result) if c.startswith("book_outline")}


def test_outline_missing_volume_warns(tmp_path: Path):
    """acts 引用的卷在 plan.volumes 没有 spine/goal → 软告警，不拦 passed。"""
    plan = _plan(book_outline={"acts": [{"act": 1, "volumes": [1, 9]}]})
    store = _init(tmp_path, plan)
    result = validate_plan(store)
    assert result["passed"] is True
    missing = [w for w in result["warnings"] if w["code"] == "book_outline_missing_volume"]
    assert len(missing) == 1
    assert missing[0]["volume"] == 9


def test_outline_budget_mismatch_warns(tmp_path: Path):
    """卷预算之和 ≠ 全书目标 → 软告警。"""
    plan = _plan(
        book_outline={
            "book_words": 500000,
            "acts": [{"act": 1, "volumes": [1]}],
        },
    )
    store = _init(tmp_path, plan, book_words=500000)
    result = validate_plan(store)
    assert result["passed"] is True
    mismatch = [w for w in result["warnings"] if w["code"] == "book_outline_budget_mismatch"]
    # 卷预算 100000 vs book_outline.book_words 500000
    assert any(w.get("sum_volume_budgets") == 100000 and w.get("book_words") == 500000 for w in mismatch)


def test_outline_budget_matches_no_warning(tmp_path: Path):
    """卷预算之和 = 全书目标（且与 config 一致）→ 无预算告警。"""
    plan = _plan(
        book_outline={"book_words": 100000, "acts": [{"act": 1, "volumes": [1]}]},
    )
    store = _init(tmp_path, plan, book_words=100000)
    result = validate_plan(store)
    assert result["passed"] is True
    assert "book_outline_budget_mismatch" not in _codes(result)


def test_outline_bad_kind_warns(tmp_path: Path):
    plan = _plan(
        book_outline={
            "acts": [{"act": 1, "volumes": [1]}],
            "milestones": [{"chapter": 1, "kind": "nonsense"}],
        }
    )
    store = _init(tmp_path, plan)
    result = validate_plan(store)
    assert result["passed"] is True
    assert "book_outline_bad_kind" in _codes(result)


def test_outline_milestone_out_of_range_warns(tmp_path: Path):
    plan = _plan(
        book_outline={
            "acts": [{"act": 1, "volumes": [1]}],
            "total_chapters": 10,
            "milestones": [{"chapter": 99, "kind": "book_climax"}, {"chapter": -1, "kind": "turning_point"}],
        }
    )
    store = _init(tmp_path, plan)
    result = validate_plan(store)
    assert result["passed"] is True
    oob = [w for w in result["warnings"] if w["code"] == "book_outline_milestone_out_of_range"]
    assert len(oob) == 2


def test_outline_invalid_type_warns(tmp_path: Path):
    plan = _plan(book_outline="not-an-object")
    store = _init(tmp_path, plan)
    result = validate_plan(store)
    assert result["passed"] is True
    assert "book_outline_invalid" in _codes(result)


def test_outline_warnings_do_not_affect_passed(tmp_path: Path):
    """所有大纲告警都是 warning：errors 为 0 时 passed 必为 True。"""
    plan = _plan(
        book_outline={
            "book_words": 9,
            "acts": [{"act": 1, "volumes": [1, 42]}],
            "milestones": [{"chapter": 4242, "kind": "bad"}],
        }
    )
    store = _init(tmp_path, plan, book_words=9)
    result = validate_plan(store)
    assert result["error_count"] == 0
    assert result["passed"] is True
    assert result["warning_count"] >= 1


def test_plan_extend_preserves_book_outline(tmp_path: Path):
    """plan extend 只动 chapters，顶层 book_outline 必须原样保留。"""
    outline = {"book_words": 100000, "acts": [{"act": 1, "volumes": [1]}]}
    store = _init(tmp_path, _plan(book_outline=outline), book_words=100000)
    store.extend_plan(
        [
            {
                "chapter": 2,
                "volume": 1,
                "location": "市集",
                "present": ["主角"],
                "beats": [{"id": "b1", "required": True, "text": "主角登门", "must": "登门"}, {"id": "b1", "required": True, "text": "主角登门", "must": "登门"}, {"id": "b1", "required": True, "text": "主角登门", "must": "登门"}, {"id": "b1", "required": True, "text": "主角登门", "must": "登门"}, {"id": "b1", "required": True, "text": "主角登门", "must": "登门"}],
            }
        ]
    )
    assert store.load_plan()["book_outline"] == outline


def test_story_map_requires_chapter_thread_and_clock_refs(tmp_path: Path):
    plan = _plan(book_outline=_story_map_outline(), phases=[_phase("phase-1", 1, 1)])
    plan["chapters"][0].update({"phase_id": "phase-1", "story_stage": "起局"})
    store = _init(tmp_path, plan, name="story_refs")
    result = validate_plan(store)
    assert result["passed"] is False
    errors = {item["code"]: item for item in result["errors"]}
    assert "story_map_missing_thread_refs" in errors
    assert "story_map_missing_clock_refs" in errors
    # 错误消息必须点名正确字段：worker 自造 threads/clocks 时靠这句话改对（实战曾连拒停线）。
    assert "`thread_refs`" in errors["story_map_missing_thread_refs"]["message"]
    assert "`clock_refs`" in errors["story_map_missing_clock_refs"]["message"]


def test_plan_extend_enforces_story_map_atomically(tmp_path: Path):
    plan = _plan(book_outline=_story_map_outline(), phases=[_phase("phase-1", 1, 1)])
    plan["chapters"][0].update({
        "phase_id": "phase-1",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
    })
    store = _init(tmp_path, plan, name="story_extend")
    before = store.load_plan()
    invalid = {
        "chapter": 2, "location": "市集", "present": ["主角"],
        "beats": [{"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}],
    }
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([invalid])
    assert exc.value.code == "event_spine_phase_required"
    assert store.load_plan() == before

    valid = {
        **invalid,
        "phase_id": "phase-2",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
    }
    result = store.extend_plan([valid], phase_brief=_phase("phase-2", 2, 2))
    assert result["added"] == [2]
    assert result["phase_id"] == "phase-2"
    saved = store.load_plan()
    assert saved["phases"][-1]["id"] == "phase-2"


@pytest.mark.parametrize(
    ("mutation", "expected_detail_code"),
    [
        ("missing_subplot", "event_spine_phase_refs"),
        ("wrong_timeline_layer", "event_spine_phase_refs"),
        ("cross_stage_tension", "event_spine_phase_tension_mismatch"),
        ("cross_stage_mainline", "event_spine_phase_stage_mismatch"),
    ],
)
def test_plan_extend_requires_all_four_event_spine_layers_atomically(
    tmp_path: Path,
    mutation: str,
    expected_detail_code: str,
):
    plan = _plan(book_outline=_story_map_outline(), phases=[_phase("phase-1", 1, 1)])
    plan["chapters"][0].update({
        "phase_id": "phase-1",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
    })
    store = _init(tmp_path, plan, name=f"four_layers_{mutation}")
    before = store.load_plan()
    phase = _phase("phase-2", 2, 2)
    if mutation == "missing_subplot":
        phase["subplot_event_refs"] = []
    elif mutation == "wrong_timeline_layer":
        phase["timeline_event_refs"] = ["main-open"]
    elif mutation == "cross_stage_tension":
        phase["tension_stage"] = "破局"
    else:
        phase["mainline_event_ref"] = "main-payoff"
    chapter = {
        "chapter": 2,
        "location": "市集",
        "present": ["主角"],
        "phase_id": "phase-2",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
        "beats": [{"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}],
    }

    with pytest.raises(LedgerError) as exc:
        store.extend_plan([chapter], phase_brief=phase)

    assert exc.value.code == "event_spine_contract_violation"
    assert expected_detail_code in {item["code"] for item in exc.value.details["errors"]}
    assert store.load_plan() == before


def test_plan_extend_phase_range_must_exactly_match_contiguous_batch(tmp_path: Path):
    plan = _plan(book_outline=_story_map_outline(), phases=[_phase("phase-1", 1, 1)])
    plan["chapters"][0].update({
        "phase_id": "phase-1",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
    })
    store = _init(tmp_path, plan, name="phase_range")
    before = store.load_plan()
    chapter = {
        "chapter": 2,
        "location": "市集",
        "present": ["主角"],
        "phase_id": "phase-2",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
        "beats": [{"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}, {"id": "b2", "text": "主角带走原件", "must": "原件"}],
    }

    with pytest.raises(LedgerError) as exc:
        store.extend_plan([chapter], phase_brief=_phase("phase-2", 2, 3))

    assert exc.value.code == "event_spine_phase_range_mismatch"
    assert store.load_plan() == before


def test_plan_v2_cannot_bypass_contract_by_removing_event_spine(tmp_path: Path):
    outline = _story_map_outline()
    outline.pop("event_spine")
    plan = _plan(schema="novel-ledger.plan.v2", book_outline=outline, phases=[])
    store = _init(tmp_path, plan, name="missing_spine")

    result = validate_plan(store)

    assert result["passed"] is False
    assert "event_spine_missing" in {item["code"] for item in result["errors"]}


def test_phase_chapters_must_collectively_cover_every_selected_owner(tmp_path: Path):
    plan = _plan(book_outline=_story_map_outline(), phases=[_phase("phase-1", 1, 1)])
    plan["chapters"][0].update({
        "phase_id": "phase-1",
        "story_stage": "起局",
        "thread_refs": ["main"],
        "clock_refs": ["clock-protagonist"],
    })
    store = _init(tmp_path, plan, name="missing_phase_coverage")

    result = validate_plan(store)

    coverage = [item for item in result["errors"] if item["code"] == "event_spine_phase_coverage"]
    assert coverage
    assert coverage[0]["missing_threads"] == ["ally"]


def test_story_focus_projects_only_phase_selected_event_within_same_stage():
    outline = _story_map_outline()
    outline["event_spine"]["mainline"]["events"].insert(1, {
        "id": "main-open-later",
        "stage": "起局",
        "event": "后续阶段才取得完整抄本",
        "change": "证据升级",
    })
    phase = _phase("phase-1", 1, 1)
    chapter = {
        "chapter": 1,
        "phase_id": "phase-1",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
    }

    focus = story_focus(outline, chapter, [phase])

    main = next(item for item in focus["threads"] if item["id"] == "main")
    assert [item["event"] for item in main["stage_touchpoints"]] == ["找到残页"]
    assert "后续阶段才取得完整抄本" not in str(focus)


def test_selected_story_focus_keeps_all_changes_despite_legacy_character_cap():
    """Chapter-selected changes remain complete; old char caps cannot remove them."""
    from novel_ledger_core.content.pack import _bound_story_focus
    from novel_ledger_core.infra.util import canonical_json

    small = {"stage": "起局", "phase": {"id": "p1", "name": "起", "objective": "立住"}}
    assert _bound_story_focus(small, cap=1200) == small

    focus = {
        "stage": "起局",
        "phase": {
            "id": "p1",
            "name": "phase",
            "objective": "目标",
            "event_changes": [{"change": f"变化{i}" + "长" * 60} for i in range(20)],
            "chapter_start": 1,
            "chapter_end": 20,
            "entry_state": "入",
            "exit_state": "出",
        },
    }
    assert len(canonical_json(focus)) > 1200
    bounded = _bound_story_focus(focus, cap=1200)
    assert bounded == focus
    assert len(canonical_json(bounded)) > 1200
    # 调用方对象不被就地修改
    assert len(focus["phase"]["event_changes"]) == 20


def test_story_focus_flags_threads_the_chapter_beats_cannot_support():
    """thread_refs 与本章 beats 对不上时必须留痕，而不是让视图去指挥执笔硬塞人物。

    现场曾整卷出现：章节挂着某条支线的 ref，但本章拍点与在场
    名单里根本没有这条线的具名参与者；pack 却把 story_focus 渲染成"本章推进故事线 X"，
    执笔编辑照做就会为没有场次的人物另起一场戏。当时靠总编辑每章手写禁令挡住。
    """
    outline = _story_map_outline()
    phase = _phase("phase-1", 1, 1)
    base = {
        "chapter": 1,
        "phase_id": "phase-1",
        "story_stage": "起局",
        "thread_refs": ["main", "ally"],
        "clock_refs": ["clock-protagonist"],
    }

    # 1) 拍点里没有盟友 → ally 线没有本章场次支撑；主线永远算有支撑。
    unsupported = story_focus(
        outline,
        {
            **base,
            "present": ["主角"],
            "beats": [{"id": "b1", "text": "主角在市集翻出残页"}],
        },
        [phase],
        protagonist="主角",
    )
    ally = next(item for item in unsupported["threads"] if item["id"] == "ally")
    main = next(item for item in unsupported["threads"] if item["id"] == "main")
    assert ally["beats_support"] is False
    assert "不得为它另起场次" in ally["beats_support_note"]
    assert main["beats_support"] is True
    assert "beats_support_note" not in main

    # 2) 盟友出现在拍点正文里 → 有支撑，不得误报。
    supported = story_focus(
        outline,
        {
            **base,
            "present": ["主角"],
            "beats": [{"id": "b1", "text": "主角把残页给盟友看"}],
        },
        [phase],
        protagonist="主角",
    )
    ally_ok = next(item for item in supported["threads"] if item["id"] == "ally")
    assert ally_ok["beats_support"] is True

    # 3) 在场名单也算支撑（不必写进拍点文本）。
    in_present = story_focus(
        outline,
        {
            **base,
            "present": ["主角", "盟友"],
            "beats": [{"id": "b1", "text": "主角翻出残页"}],
        },
        [phase],
        protagonist="主角",
    )
    assert next(item for item in in_present["threads"] if item["id"] == "ally")["beats_support"] is True

    # 4) 渲染侧必须把"以 beats 为准"写进给执笔编辑看的同一段文字里。
    rendered = "\n".join(_render_story_focus({"story_focus": unsupported}, include_ids=False))
    assert "在本章拍点与在场名单里都没有具名参与者" in rendered
    assert "本章以 beats 为准" in rendered
    # 有支撑时不得平白多出这条禁令
    clean = "\n".join(_render_story_focus({"story_focus": supported}, include_ids=False))
    assert "本章以 beats 为准" not in clean


def test_phase_and_event_order_cannot_regress():
    first = _phase("phase-1", 1, 1, stage="破局")
    second = _phase("phase-2", 2, 2, stage="起局")
    plan = _plan(
        book_outline=_story_map_outline(),
        phases=[first, second],
        chapters=[
            {
                "chapter": 1, "volume": 1, "phase_id": "phase-1", "story_stage": "破局",
                "thread_refs": ["main", "ally"], "clock_refs": ["clock-protagonist"],
                "beats": [{"id": "b1", "text": "公开证据", "must": "证据"}],
            },
            {
                "chapter": 2, "volume": 1, "phase_id": "phase-2", "story_stage": "起局",
                "thread_refs": ["main", "ally"], "clock_refs": ["clock-protagonist"],
                "beats": [{"id": "b2", "text": "重新寻找残页", "must": "残页"}],
            },
        ],
    )

    codes = {item["code"] for item in plan_story_map_errors(plan)}

    assert "event_spine_phase_stage_regression" in codes
    assert "event_spine_event_regression" in codes


def test_phase_length_is_bounded_to_twenty_chapters():
    phase = _phase("phase-long", 1, 21)
    chapters = [
        {
            "chapter": number, "volume": 1, "phase_id": "phase-long", "story_stage": "起局",
            "thread_refs": ["main", "ally"], "clock_refs": ["clock-protagonist"],
            "beats": [{"id": f"b{number}", "text": f"推进第{number}章", "must": "推进"}],
        }
        for number in range(1, 22)
    ]
    plan = _plan(book_outline=_story_map_outline(), phases=[phase], chapters=chapters)

    codes = {item["code"] for item in plan_story_map_errors(plan)}

    assert "event_spine_phase_too_long" in codes


def test_scale_contract_rejects_million_word_book_collapsed_to_sixty_chapters(tmp_path: Path):
    plan = _plan(
        book_outline={
            "book_words": 2_000_000,
            "chapter_words_target": 2500,
            "total_chapters": 60,
            "acts": [],
        },
    )
    store = _init(tmp_path, plan, name="collapsed", book_words=2_000_000)
    result = validate_plan(store)
    assert result["passed"] is False
    assert "book_scale_total_chapters_mismatch" in {item["code"] for item in result["errors"]}


def test_plan_extend_rejects_early_whole_book_ending_atomically(tmp_path: Path):
    plan = _plan(
        book_outline={
            "book_words": 2_000_000,
            "chapter_words_target": 2500,
            "total_chapters": 800,
            "acts": [],
        },
        volumes={
            "1": {
                "spine": "第一卷",
                "word_budget": 200_000,
                "chapters_budget": 80,
            }
        },
    )
    store = _init(tmp_path, plan, name="early_end", book_words=2_000_000)
    before = store.load_plan()
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([
            {
                "chapter": 2,
                "volume": 1,
                "title": "全书大结局",
                "location": "故乡",
                "present": ["主角"],
                "beats": [
                    {"id": "b1", "text": "主角合上账册，全书圆满收官", "must": "账册"},
                    {"id": "b2", "text": "众人回到故乡", "must": "故乡"},
                    {"id": "b3", "text": "晨光照进屋内", "must": "晨光"},
                    {"id": "b4", "text": "主角把账册交给后来人", "must": "账册"},
                    {"id": "b5", "text": "故乡的钟声响起", "must": "故乡"},
                ],
            }
        ])
    assert exc.value.code == "book_scale_contract_violation"
    assert store.load_plan() == before


def test_audit_book_reports_outline_conformance(tmp_path: Path):
    """book audit 输出 outline_conformance 汇总；未声明大纲时 declared=False。"""
    store = _init(
        tmp_path,
        _plan(
            book_outline={
                "book_words": 100000,
                "total_chapters": 50,
                "acts": [{"act": 1, "volumes": [1]}],
                "milestones": [{"chapter": 1, "kind": "turning_point"}, {"chapter": 50, "kind": "book_climax"}],
            }
        ),
        name="with_outline",
        book_words=100000,
    )
    res = audit_book(store)
    conf = res["outline_conformance"]
    assert conf["declared"] is True
    assert conf["book_words"] == 100000
    assert conf["total_chapters"] == 50
    assert conf["milestones_total"] == 2
    # 尚无已提交章，达成 0
    assert conf["milestones_reached"] == 0

    store2 = _init(tmp_path, _plan(), name="no_outline")
    assert audit_book(store2)["outline_conformance"] == {"declared": False}


# --- 创意编辑介入 advisory（跨卷触发，不挡流程） ---------------------------------
#
# 创意编辑是开书之外的按需角色（跨卷/大纲修订/破局），派发权归总编辑。脚本不自动派它，
# 只把"该想起它的时刻"（跨入新卷）变成机器可读的一行建议——否则入口文档只在开书语境
# 提它，跨卷时全靠宿主记忆，长跑里必成死角。


def _two_volume_plan(**extra) -> dict:
    """两卷各两章：第 3 章跨入第二卷，是唯一的卷边界。"""
    plan = {
        "title": "",
        "protagonist": "主角",
        "overview": "两卷骨架",
        "volumes": {
            "1": {"spine": "第一卷：拿回凭据。", "goal": "拿回凭据。", "word_budget": 50000},
            "2": {"spine": "第二卷：出镇。", "goal": "出镇立足。", "word_budget": 50000},
        },
        "chapters": [
            {
                "chapter": n,
                "volume": 1 if n <= 2 else 2,
                "location": "市集",
                "present": ["主角"],
                "beats": [{"id": "b1", "required": True, "text": f"第{n}章凭据事件", "must": "凭据"}],
            }
            for n in range(1, 5)
        ],
    }
    plan.update(extra)
    return plan


def _codes_or_empty(advisory: dict | None) -> str:
    return (advisory or {}).get("reason_code", "")


def test_creative_advisory_silent_within_one_volume(tmp_path: Path):
    """卷内推进（无卷边界）不产生任何创意编辑建议——单卷项目永不触发。"""
    store = _init(tmp_path, _plan(), name="within_vol")
    assert _creative_advisory(store, [1]) is None


def test_creative_advisory_fires_on_volume_boundary_with_outline(tmp_path: Path):
    """已声明 book_outline 时跨卷：提示按大纲核对收口，走 creative 起草。"""
    outline = {"book_words": 100000, "acts": [{"act": 1, "volumes": [1, 2]}]}
    store = _init(
        tmp_path,
        _two_volume_plan(book_outline=outline),
        name="boundary_outline",
        book_words=100000,
    )
    advisory = _creative_advisory(store, [3, 4])
    assert advisory is not None
    assert advisory["suggested_role"] == "creative"
    assert advisory["reason_code"] == "volume_boundary_outline_review"
    assert advisory["chapters"] == [3]


def test_creative_advisory_flags_missing_outline_at_volume_boundary(tmp_path: Path):
    """未声明 book_outline 时跨卷：提示补全书大纲（逐卷设计的塌缩风险）。"""
    store = _init(tmp_path, _two_volume_plan(), name="boundary_no_outline")
    advisory = _creative_advisory(store, [3])
    assert _codes_or_empty(advisory) == "book_outline_undeclared"
    assert advisory["suggested_role"] == "creative"


def test_draft_action_carries_creative_advisory_on_new_volume(tmp_path: Path):
    """draft action 真带这个字段：卷内章为空、跨卷首章非空。"""
    store = _init(tmp_path, _two_volume_plan(), name="draft_boundary")
    # 第 1 章：仍在第一卷，字段在但为空
    first = chapter_next(store)
    assert first["action"] == "draft"
    assert "creative_advisory" in first
    assert first["creative_advisory"] is None
    # 第 3 章（跨卷首章）：直接走 _draft_action 的装配行
    from novel_ledger_core.control.pipeline import _draft_action

    deep = _draft_action(store, {"chapter": 3, "phase": "await_draft"})
    assert deep["action"] == "draft"
    assert deep["creative_advisory"]["reason_code"] == "book_outline_undeclared"


def test_creative_advisory_ignores_chapters_outside_plan(tmp_path: Path):
    """计划耗尽/断档不得误报跨卷：缺章的卷归属会静默回退第一卷，用它比较会假阳性。

    末章在第 2 卷时，问"下一章"（不在计划里）不能报跨卷——那只是没有下一章了。
    """
    store = _init(
        tmp_path, _two_volume_plan(), name="exhausted", word_min=20, word_max=5000
    )
    # 第 4 章在第 2 卷；第 5 章不在计划里 → 不触发
    assert _creative_advisory(store, [5]) is None
    # 断档：只问第 3 章而第 2 章也不在计划里时，不比较缺章
    plan = _two_volume_plan()
    plan["chapters"] = [c for c in plan["chapters"] if c["chapter"] != 2]
    store2 = _init(tmp_path, plan, name="gap", word_min=20, word_max=5000)
    assert _creative_advisory(store2, [3]) is None


def test_audit_book_carries_creative_advisory_key(tmp_path: Path):
    """book audit 也带这个 advisory 键（尚无已提交章时静默，但字段存在）。"""
    store = _init(tmp_path, _two_volume_plan(), name="audit_boundary")
    res = audit_book(store)
    assert "creative_advisory" in res
    assert res["creative_advisory"] is None


def test_extend_plan_payload_carries_creative_advisory(tmp_path: Path):
    """低水位 extend_plan 的 hint 之外，另给一行创意编辑建议（跨卷时才非空）。"""
    store = _init(
        tmp_path,
        _two_volume_plan(),
        name="extend_boundary",
        word_min=20,
        word_max=5000,
    )
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    store.save_config(cfg)
    # 低水位提示只在已开写后触发（首跑直接 draft）：预置第 1 章已提交 ack
    head = store.read_head()
    head.update({"phase": "idle", "chapter": 1, "last_committed_ch": 1, "last_acked_ch": 1})
    store.write_head(head)
    nudge = chapter_next(store)
    assert nudge["action"] == "extend_plan"
    # 剩余章跨第 3 章的卷边界
    assert nudge["creative_advisory"]["reason_code"] == "book_outline_undeclared"


def test_creative_advisory_fires_on_mid_volume_phase_transition(tmp_path: Path):
    """同卷内发生 phase_id 阶段跃迁：提示派创意编辑出具阶段小纲（Phase Brief）。"""
    plan = {
        "protagonist": "李长生",
        "volumes": [{"volume": 1, "spine": "首卷卷脊"}],
        "chapters": [
            {
                "chapter": 1,
                "volume": 1,
                "phase_id": "phase-1",
                "beats": [{"id": "b1", "text": "第1章戏", "must": ["锚点"]}],
            },
            {
                "chapter": 2,
                "volume": 1,
                "phase_id": "phase-1",
                "beats": [{"id": "b2", "text": "第2章戏", "must": ["锚点"]}],
            },
            {
                "chapter": 3,
                "volume": 1,
                "phase_id": "phase-2",
                "beats": [{"id": "b3", "text": "第3章戏", "must": ["锚点"]}],
            },
        ],
    }
    store = _init(tmp_path, plan, name="phase_trans", word_min=20, word_max=5000)
    advisory = _creative_advisory(store, [3])
    assert advisory is not None
    assert advisory["suggested_role"] == "creative"
    assert advisory["reason_code"] == "mid_volume_phase_transition"
    assert advisory["phase"] == "phase-2"
    assert advisory["previous_phase"] == "phase-1"
    assert advisory["chapters"] == [3]
    assert "Phase Brief" in advisory["hint"]


def test_creative_advisory_silent_within_same_phase(tmp_path: Path):
    """同卷同阶段推进：不触发阶段跃迁建议。"""
    plan = {
        "protagonist": "李长生",
        "volumes": [{"volume": 1, "spine": "首卷卷脊"}],
        "chapters": [
            {
                "chapter": 1,
                "volume": 1,
                "phase_id": "phase-1",
                "beats": [{"id": "b1", "text": "第1章戏", "must": ["锚点"]}],
            },
            {
                "chapter": 2,
                "volume": 1,
                "phase_id": "phase-1",
                "beats": [{"id": "b2", "text": "第2章戏", "must": ["锚点"]}],
            },
        ],
    }
    store = _init(tmp_path, plan, name="same_phase", word_min=20, word_max=5000)
    assert _creative_advisory(store, [2]) is None


def test_extend_plan_hint_carries_phase_brief_guidance(tmp_path: Path):
    """低水位 extend_plan 的 hint 包含卷内新阶段派发创意编辑的指导信息。"""
    plan = _plan()
    plan["chapters"].append(
        {
            "chapter": 2,
            "volume": 1,
            "location": "账房",
            "present": ["主角", "掌柜"],
            "tags": ["账房"],
            "beats": [{"id": "b2", "required": True, "text": "主角夜里翻账", "must": "翻账"}],
        }
    )
    store = _init(tmp_path, plan, name="extend_hint", word_min=20, word_max=5000)
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    store.save_config(cfg)
    # 低水位提示只在已开写后触发（首跑直接 draft）：预置第 1 章已提交 ack
    head = store.read_head()
    head.update({"phase": "idle", "chapter": 1, "last_committed_ch": 1, "last_acked_ch": 1})
    store.write_head(head)
    nudge = chapter_next(store)
    assert nudge["action"] == "extend_plan"
    assert "阶段小纲（Phase Brief）" in nudge["hint"]


def test_plan_extend_rejects_effects_with_wrong_stable_keys(tmp_path: Path):
    """effects.facts 用 `fact` 键（应为 `text`）在 extend 落盘前拒收。

    这类坏键在组装提交时无论怎么写都无法对账（_find_actual 按稳定键匹配），
    必然 blocked 停线——度牒项目曾因此停机在 ch5 组装。"""
    store = _init(tmp_path, _plan(), name="fx_keys")
    before = store.load_plan()
    beat = {
        "id": "b1",
        "required": True,
        "text": "主角递状取回执",
        "must": "回执",
        "effects": {"facts": [{"who": "主角", "fact": "申诉状已递入公所"}]},
    }
    bad = {
        "chapter": 2,
        "volume": 1,
        "location": "公所",
        "present": ["主角"],
        "beats": [dict(beat) for _ in range(5)],
    }
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([bad])
    assert exc.value.code == "invalid_plan"
    detail = exc.value.detail if hasattr(exc.value, "detail") else exc.value.args
    assert "fact" in str(detail)
    assert store.load_plan() == before

    good_beat = {
        **beat,
        "effects": {"facts": [{"who": "主角", "text": "申诉状已递入公所"}]},
    }
    good = {**bad, "beats": [dict(good_beat) for _ in range(5)]}
    result = store.extend_plan([good])
    assert result["added"] == [2]


def test_validate_plan_flags_effects_entries_missing_stable_keys(tmp_path: Path):
    """存量坏键（手工编辑或历史数据）由 validate_plan 兜底点名。

    入口防线（init/hatch 与 plan extend）现在直接拒收坏键，构造存量只能走
    save_plan 直接写真源——这正是 validate 兜底要覆盖的「手工编辑」场景。
    """
    store = _init(tmp_path, _plan(), name="fx_audit")
    plan = _plan()
    plan["chapters"][0]["beats"][0]["effects"] = {
        "facts": [{"who": "主角", "fact": "凭据被扣"}],
        "relations": [{"who": "主角", "target": "掌柜"}],
    }
    store.save_plan(plan)
    result = validate_plan(store)
    assert result["passed"] is False
    codes = {item["code"] for item in result["errors"]}
    assert "effects_entry_missing_keys" in codes
