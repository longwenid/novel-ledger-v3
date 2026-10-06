"""卷合同与扩纲契约的回归测试（现场审计 + 千章验证双源缺陷）。

覆盖四组机制：
1. 卷号归一解析——hatch 旧键 "vol-01" / 目录名 "vol-0002" / 整数 / 中文序号互相等价，
   根治「正文目录、卷脊、记忆卷层」三套命名静默分叉；
2. `plan extend` 签卷合同——volumes payload 合并语义、纯签卷模式、scale 合同硬校验；
3. 卷合同覆盖率左移告警——acts[].volumes 字符串形态复活 `book_outline_missing_volume`，
   Σ卷预算与全书目标失衡在签出当时可见（现场事故：8 卷 × 80 章配 400 章全书）；
4. 无人值守恢复面——`run resume` 重置未兑现的低水位提示标记（「扩纲优先」不被吞）。
"""
from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.autopilot import (
    _reset_unhonored_nudge,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    _creative_advisory,
    _outline_warnings,
    chapter_next,
    status,
    validate_plan,
)
from novel_ledger_core.infra.store import BookStore, volume_entry_for
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json


def _plan(*, chapters: int = 3, volume_field: bool = False) -> dict:
    items = []
    for n in range(1, chapters + 1):
        ch = {
            "chapter": n,
            "volume": "vol-0001",
            "location": "市集",
            "present": ["主角"],
            "beats": [{"id": f"b{n}b1", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b2", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b3", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b4", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b5", "required": True, "text": f"第{n}章推进", "must": "推进"}],
        }
        if not volume_field:
            ch.pop("volume")
        items.append(ch)
    # 手工小项目口径（无 schema/无 book_outline）：卷机制不依赖 v2 合同；需要 v2 的用例自己构造
    return {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：长跑验证。",
        "chapters": items,
    }


def _init(tmp_path: Path, plan: dict, name: str = "proj") -> BookStore:
    plan_path = tmp_path / f"{name}-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / name
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def _codes(warnings: list[dict]) -> list[str]:
    return [w["code"] for w in warnings]


# ---------------------------------------------------------------- 1. 卷号归一


def test_volume_entry_for_unifies_alternate_volume_keys():
    volumes = {
        "vol-01": {"spine": "旧 hatch 键", "word_budget": 200000, "chapters_budget": 80},
        "vol-0002": {"spine": "目录名键", "word_budget": 200000, "chapters_budget": 80},
        "第三卷": {"spine": "中文序号键"},
    }
    assert volume_entry_for(volumes, 1)["spine"] == "旧 hatch 键"
    assert volume_entry_for(volumes, "vol-0001")["spine"] == "旧 hatch 键"
    assert volume_entry_for(volumes, "第一卷")["spine"] == "旧 hatch 键"
    assert volume_entry_for(volumes, 2)["spine"] == "目录名键"
    assert volume_entry_for(volumes, "vol-02")["spine"] == "目录名键"
    assert volume_entry_for(volumes, "第二卷")["spine"] == "目录名键"
    assert volume_entry_for(volumes, 3)["spine"] == "中文序号键"


def test_volume_meta_resolves_short_directory_key(tmp_path: Path):
    """现场事故复现：plan.volumes 键 "vol-01" 而章拍无 volume 字段（hatch 归一化产物）。

    旧解析 str(1)="1" 与 _volume_label(1)="vol-0001" 都不命中 "vol-01"，
    volume_meta 恒为 {}，卷脊静默退化为全局；归一后必须命中。
    """
    plan = _plan()
    plan["volumes"] = {"vol-01": {"spine": "从试药杂役到走出药圃", "goal": "走出药圃"}}
    store = _init(tmp_path, plan, name="legacy-key")
    assert store.volume_meta(1).get("spine") == "从试药杂役到走出药圃"
    assert store.volume_meta(1).get("goal") == "走出药圃"


def test_volume_meta_resolves_directory_key_for_volume_two(tmp_path: Path):
    """跨卷场景：第 81 章 volume=2，plan.volumes 只有目录名键——卷脊必须切到第二卷。"""
    plan = _plan(chapters=2)
    plan["chapters"].append({
        "chapter": 81, "volume": 2, "location": "坊市", "present": ["主角"],
        "beats": [{"id": "b81", "required": True, "text": "主角立身", "must": "立身"}],
    })
    plan["volumes"] = {
        "vol-0001": {"spine": "第一卷卷脊"},
        "vol-0002": {"spine": "第二卷卷脊"},
    }
    store = _init(tmp_path, plan, name="cross-volume")
    assert store.volume_meta(1).get("spine") == "第一卷卷脊"
    assert store.volume_meta(2).get("spine") == "第一卷卷脊"
    assert store.volume_meta(81).get("spine") == "第二卷卷脊"
    assert store._volume_folder(81) == "vol-0002"


# ---------------------------------------------------------------- 2. plan extend 签卷合同


def test_extend_plan_signs_volumes_alone_without_chapters(tmp_path: Path):
    """纯签卷：跨卷长跑开工前把后续卷预算/卷脊签成机器可校验的事实（现场操作收编入契约）。"""
    plan = _plan(volume_field=True)
    plan["volumes"] = {"vol-0001": {"spine": "第一卷：长跑验证。", "word_budget": 250000, "chapters_budget": 100}}
    store = _init(tmp_path, plan, name="sign-volumes")
    before = read_json(store.plan_path)
    result = store.extend_plan([], volumes={
        "vol-0002": {"title": "第二卷", "spine": "第二卷卷脊", "word_budget": 250000, "chapters_budget": 100},
        "vol-0003": {"title": "第三卷", "spine": "第三卷卷脊", "word_budget": 250000, "chapters_budget": 100},
    })
    assert result["added"] == []
    assert result["volumes_updated"] == ["vol-0002", "vol-0003"]
    after = read_json(store.plan_path)
    assert after["chapters"] == before["chapters"], "纯签卷不得动章拍"
    assert set(after["volumes"]) == {"vol-0001", "vol-0002", "vol-0003"}, "merge 语义不得丢既有卷"
    assert after["volumes"]["vol-0002"]["spine"] == "第二卷卷脊"
    assert store.volume_meta(1).get("spine") == "第一卷：长跑验证。"


def test_extend_plan_rejects_inconsistent_volume_budget_atomically(tmp_path: Path):
    store = _init(tmp_path, _plan(volume_field=True), name="bad-vol")
    before = read_json(store.plan_path)
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([], volumes={
            "vol-0002": {"spine": "坏预算", "word_budget": 250000, "chapters_budget": 99},
        })
    assert exc.value.code == "book_scale_contract_violation"
    assert read_json(store.plan_path) == before, "卷合同校验失败必须整体不落盘"


def test_extend_plan_rejects_malformed_volume_entries(tmp_path: Path):
    store = _init(tmp_path, _plan(volume_field=True), name="malformed-vol")
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([], volumes={"vol-0002": "not-an-object"})
    assert exc.value.code == "invalid_plan"
    with pytest.raises(LedgerError):
        store.extend_plan([], volumes={"": {"spine": "x"}})
    with pytest.raises(LedgerError):
        store.extend_plan([], volumes={})


def test_extend_plan_appends_chapters_with_volumes_in_one_batch(tmp_path: Path):
    """扩纲 + 签卷同一原子操作：新卷先于跨卷章存在，phase 不跨卷。"""
    plan = {
        "schema": "novel-ledger.plan.v2",
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：起。",
        "chapters": [_ch(1, "起", "open", "phase-01"), _ch(2, "起", "open", "phase-01")],
        "phases": [_mini_phase("phase-01", 1, 2, "起")],
        "book_outline": {
            "book_words": 750000,
            "chapter_words_target": 2500,
            "total_chapters": 300,
            "acts": [{"name": "起", "volumes": "1"}, {"name": "承", "volumes": "2"}],
            "event_spine": _mini_spine(),
        },
        "volumes": {"vol-0001": {"spine": "第一卷卷脊", "word_budget": 250000, "chapters_budget": 100}},
    }
    store = _init(tmp_path, plan, name="batch-vol")
    cfg = store.load_config()
    cfg["book_words"] = 750000
    store.save_config(cfg)
    result = store.extend_plan(
        [_ch(3, "承", "payoff", "phase-02")],
        phase_brief=_mini_phase("phase-02", 3, 3, "承"),
        volumes={"vol-0002": {"spine": "第二卷卷脊", "word_budget": 250000, "chapters_budget": 100}},
    )
    assert result["added"] == [3]
    after = read_json(store.plan_path)
    assert after["volumes"]["vol-0002"]["spine"] == "第二卷卷脊"
    assert store.volume_meta(3).get("spine") == "第二卷卷脊"
    assert store.volume_meta(1).get("spine") == "第一卷卷脊"


def _mini_spine() -> dict:
    return {
        "schema": "novel-ledger.event-spine.v1",
        "mainline": {
            "id": "main", "name": "主线", "purpose": "推进", "mainline_link": "直接承载终局",
            "open_stage": "起", "payoff_stage": "承", "participants": ["主角"],
            "events": [
                {"id": "main-open", "stage": "起", "event": "起因", "change": "入局"},
                {"id": "main-payoff", "stage": "承", "event": "承转", "change": "升局"},
            ],
        },
        "subplots": [{
            "id": "ally", "name": "同盟", "purpose": "检验", "mainline_link": "提供入口",
            "open_stage": "起", "payoff_stage": "承", "participants": ["主角"],
            "events": [
                {"id": "ally-open", "stage": "起", "event": "结盟", "change": "合作"},
                {"id": "ally-payoff", "stage": "承", "event": "互证", "change": "互信"},
            ],
        }],
        "timelines": [
            {
                "id": f"clock-{kind}", "kind": kind, "name": f"{kind}时钟", "start_state": "倒计时",
                "events": [
                    {"id": f"{kind}-open", "order": 1, "stage": "起", "event": "收窄", "deadline": "月末", "consequence": "断线"},
                    {"id": f"{kind}-payoff", "order": 2, "stage": "承", "event": "抵达", "deadline": "季末", "consequence": "定局"},
                ],
            }
            for kind in ("protagonist", "antagonist", "world")
        ],
        "tension_curve": [
            {"stage": "起", "level": 2, "mode": "build", "pressure": "压", "turn": "转", "payoff": "收", "next_imbalance": "续"},
            {"stage": "承", "level": 4, "mode": "climax", "pressure": "压", "turn": "转", "payoff": "收", "next_imbalance": "续"},
        ],
    }


def _mini_phase(pid: str, start: int, end: int, stage: str) -> dict:
    suffix = "open" if stage == "起" else "payoff"
    refs = [f"main-{suffix}", f"ally-{suffix}", f"protagonist-{suffix}"]
    return {
        "id": pid, "name": f"阶段{pid}", "story_stage": stage,
        "chapter_start": start, "chapter_end": end, "objective": "推进", "climax": "收口",
        "mainline_event_ref": f"main-{suffix}",
        "subplot_event_refs": [f"ally-{suffix}"],
        "timeline_event_refs": [f"protagonist-{suffix}"],
        "tension_stage": stage,
        "entry_state": "入", "exit_state": "出",
        "event_changes": {ref: f"{ref} 推进一场" for ref in refs},
        "tension_change": "升级",
    }


def _ch(n: int, stage: str, suffix: str, pid: str) -> dict:
    return {
        "chapter": n, "volume": "vol-0001" if n <= 2 else "vol-0002",
        "location": "市集", "present": ["主角"],
        "phase_id": pid, "story_stage": stage,
        "thread_refs": ["main", "ally"], "clock_refs": ["clock-protagonist"],
        "beats": [{"id": f"b{n}b1", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b2", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b3", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b4", "required": True, "text": f"第{n}章推进", "must": "推进"}, {"id": f"b{n}b5", "required": True, "text": f"第{n}章推进", "must": "推进"}],
    }


# ---------------------------------------------------------------- 3. 覆盖率左移告警


def _outline_plan(volumes: dict, acts_volumes: str | list, *, total: int = 400, words: int = 1000000) -> dict:
    plan = _plan(chapters=1, volume_field=True)
    plan["book_outline"] = {
        "book_words": words,
        "chapter_words_target": 2500,
        "total_chapters": total,
        "acts": [{"name": "第一幕", "volumes": acts_volumes, "arc": "起", "stakes": "存"}],
        "milestones": [],
        "event_spine": {},
    }
    plan["volumes"] = volumes
    return plan


def test_outline_missing_volume_fires_for_string_act_volumes(tmp_path: Path):
    """字符串形态 acts[].volumes（hatch 实际产出）此前是死检查，现在必须报警。"""
    store = _init(tmp_path, _outline_plan(
        {"vol-0001": {"spine": "卷脊", "word_budget": 200000, "chapters_budget": 80}},
        "2",
    ), name="str-acts")
    codes = _codes(_outline_warnings(store, store.load_plan()))
    assert "book_outline_missing_volume" in codes


def test_outline_missing_volume_silent_when_string_refs_covered(tmp_path: Path):
    store = _init(tmp_path, _outline_plan(
        {
            "vol-0001": {"spine": "一", "word_budget": 200000, "chapters_budget": 80},
            "vol-0002": {"spine": "二", "word_budget": 200000, "chapters_budget": 80},
        },
        "1-2",
    ), name="str-covered")
    codes = _codes(_outline_warnings(store, store.load_plan()))
    assert "book_outline_missing_volume" not in codes


def test_volume_contract_coverage_gap_warns_before_terminal_language(tmp_path: Path):
    """现场事故：Σ卷章预算 640 vs total_chapters 400，签出当时必须可见。"""
    volumes = {f"vol-{n:04d}": {"spine": f"卷{n}", "word_budget": 200000, "chapters_budget": 80}
               for n in range(1, 9)}
    store = _init(tmp_path, _outline_plan(volumes, "1-8"), name="coverage")
    codes = _codes(_outline_warnings(store, store.load_plan()))
    assert "volume_contract_chapters_gap" in codes
    assert "volume_contract_coverage_gap" not in codes  # Σ字 1,000,000 == book_words，字侧不缺

    # 字侧缺额：只签了第一卷
    store2 = _init(tmp_path, _outline_plan(
        {"vol-0001": {"spine": "一", "word_budget": 200000, "chapters_budget": 80}}, "1",
    ), name="coverage2")
    codes2 = _codes(_outline_warnings(store2, store2.load_plan()))
    assert "volume_contract_coverage_gap" in codes2


def test_volume_contract_complete_plan_has_no_coverage_warnings(tmp_path: Path):
    """5 卷 × 80 章 = 400 章 / 100 万字：合同完整，零覆盖率告警。"""
    volumes = {f"vol-{n:04d}": {"spine": f"卷{n}", "word_budget": 200000, "chapters_budget": 80}
               for n in range(1, 6)}
    store = _init(tmp_path, _outline_plan(volumes, "1-5"), name="complete")
    codes = _codes(_outline_warnings(store, store.load_plan()))
    assert "volume_contract_coverage_gap" not in codes
    assert "volume_contract_chapters_gap" not in codes
    assert "book_outline_missing_volume" not in codes


def test_no_coverage_warnings_without_volumes_table(tmp_path: Path):
    """没有卷表的老项目/小项目保持静默（告警只对已开始签卷的书生效）。"""
    plan = _plan(chapters=1)
    plan["book_outline"] = {"book_words": 100000, "total_chapters": 40, "acts": []}
    store = _init(tmp_path, plan, name="no-volumes")
    assert "volume_contract_coverage_gap" not in _codes(_outline_warnings(store, store.load_plan()))


# ---------------------------------------------------------------- 3b. 卷水位左移


def _watermark_book(tmp_path: Path, *, name: str, vol2_signed: bool) -> BookStore:
    """100 章预算的 vol-01、写到第 85 章（剩 15 章 ≤ 预警线 20）。"""
    plan = _plan(chapters=90, volume_field=True)
    plan["volumes"] = {
        "vol-0001": {"spine": "第一卷卷脊", "word_budget": 250000, "chapters_budget": 100},
    }
    if vol2_signed:
        plan["volumes"]["vol-0002"] = {
            "spine": "第二卷卷脊", "word_budget": 250000, "chapters_budget": 100,
        }
    store = _init(tmp_path, plan, name=name)
    head = store.read_head()
    head.update({"phase": "idle", "last_committed_ch": 85, "last_acked_ch": 85})
    store.write_head(head)
    return store


def test_status_volume_watermark_warns_when_next_spine_unsigned(tmp_path: Path):
    store = _watermark_book(tmp_path, name="wm-unsigned", vol2_signed=False)
    wm = status(store)["volume_watermark"]
    assert wm["current_volume"] == 1
    assert wm["chapters_left_in_volume"] == 15
    assert wm["next_volume"] == 2
    assert wm["next_volume_signed"] is False
    assert wm["warn"] is True
    assert "volumes 载荷" in wm["hint"]


def test_status_volume_watermark_quiet_when_next_spine_signed(tmp_path: Path):
    store = _watermark_book(tmp_path, name="wm-signed", vol2_signed=True)
    wm = status(store)["volume_watermark"]
    assert wm["next_volume_signed"] is True
    assert wm["warn"] is False
    assert wm["hint"] is None


def test_draft_action_carries_volume_watermark(tmp_path: Path):
    store = _watermark_book(tmp_path, name="wm-draft", vol2_signed=False)
    action = chapter_next(store)
    assert action["action"] == "draft"
    assert action["volume_watermark"]["warn"] is True


def test_creative_advisory_flags_unsigned_volume_at_boundary(tmp_path: Path):
    """跨卷边界提示必须真查 plan.volumes 条目，不能只说「核对卷脊」。"""
    plan = _plan(chapters=2, volume_field=True)
    plan["book_outline"] = {"book_words": 500000, "total_chapters": 200, "acts": []}
    plan["chapters"].append(
        {
            "chapter": 3, "volume": 2, "location": "新卷", "present": ["主角"],
            "beats": [{"id": "b3", "required": True, "text": "新卷开篇", "must": "开篇"}],
        }
    )
    plan["volumes"] = {"vol-0001": {"spine": "第一卷卷脊"}}
    store = _init(tmp_path, plan, name="adv-unsigned")
    advisory = _creative_advisory(store, [3])
    assert advisory is not None
    assert advisory["reason_code"] == "volume_boundary_outline_review"
    assert advisory["next_volume_signed"] is False
    assert "尚无卷脊条目" in advisory["hint"]

    plan["volumes"]["vol-0002"] = {"spine": "第二卷卷脊"}
    atomic_json(store.plan_path, plan)
    advisory2 = _creative_advisory(store, [3])
    assert advisory2["next_volume_signed"] is True
    assert "尚无卷脊条目" not in advisory2["hint"]


# ---------------------------------------------------------------- 4. 无人值守恢复面


def test_reset_unhonored_nudge_only_when_marker_equals_max_planned(tmp_path: Path):
    store = _init(tmp_path, _plan(chapters=20, volume_field=True), name="nudge")
    head = store.read_head()
    head["plan_low_water_nudged_at"] = 20
    store.write_head(head)

    _reset_unhonored_nudge(store)
    assert "plan_low_water_nudged_at" not in store.read_head()
    events = [json.loads(l) for l in store.autopilot_events_path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "plan_nudge_marker_reset" for e in events)

    # 计划已增长过的旧标记（20 → 现在 max=30）保持原语义：不清
    plan = read_json(store.plan_path)
    plan["chapters"].append({
        "chapter": 30, "volume": "vol-0001", "location": "市集", "present": ["主角"],
        "beats": [{"id": "b30", "required": True, "text": "第30章推进", "must": "推进"}],
    })
    atomic_json(store.plan_path, plan)
    head = store.read_head()
    head["plan_low_water_nudged_at"] = 20
    store.write_head(head)
    _reset_unhonored_nudge(store)
    assert store.read_head().get("plan_low_water_nudged_at") == 20
