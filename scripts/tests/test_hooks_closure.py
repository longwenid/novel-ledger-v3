"""伏笔回流闭环回归（活跃伏笔大量逾期的系统性根因）。

覆盖四条回流线：
1. 计划侧闭环——低水位 extend_plan 提示附 overdue_hooks，策划编辑排进新章拍
   effects.hooks 后由提交机检（expected_delta）强制兑现；
2. 草稿简报逾期指令——逾期伏笔不再只报「期限为第X章」，明确本章应回收或 defer；
3. hooks merge——duplicate_id_stem 治理入口，追加裁决后快照按 id 收敛；
4. 新建 hook 默认 status=open——audit / select_hooks / apply_event 三处口径统一。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    audit_hooks,
    chapter_next,
    defer_hook,
    merge_hook,
    status,
)
from novel_ledger_core.content.views import make_draft_view
from novel_ledger_core.content.pack import assemble_pack
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json
from novel_ledger_core.ledger.ledger import (
    commit_event,
    load_snapshot,
    verify_ledger,
)


def _plan(chapters: int = 8) -> dict:
    items = []
    for n in range(1, chapters + 1):
        items.append(
            {
                "chapter": n,
                "volume": "vol-0001",
                "location": "账房",
                "present": ["主角"],
                "beats": [{"id": f"b{n}", "required": True, "text": f"第{n}章对账", "must": "对账"}],
            }
        )
    return {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：伏笔回流验证。",
        "chapters": items,
    }


@pytest.fixture()
def store(tmp_path: Path) -> BookStore:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "bookproj"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def _commit_with_hooks(store: BookStore, chapter: int, hooks: list[dict]) -> None:
    commit_event(
        store,
        chapter=chapter,
        state_delta={"named": ["主角"], "facts": [], "hooks": hooks},
    )


def _set_head(store: BookStore, **fields) -> None:
    head = store.read_head()
    head.update(fields)
    store.write_head(head)


def test_chapter_pack_prioritizes_planned_long_hook_over_near_due_hooks(store: BookStore) -> None:
    hooks = [
        {"id": f"near-{i}", "text": f"近期约定{i}", "due": 3, "status": "open"}
        for i in range(8)
    ]
    hooks.append({"id": "long-seal", "text": "缺角印信", "due": 620, "status": "open"})
    _commit_with_hooks(store, 1, hooks)
    plan = store.load_plan()
    plan["chapters"][1]["beats"][0]["effects"] = {"hooks": [{
        "id": "long-seal", "text": "缺角印信帮主角识破假令", "status": "open", "due": 620,
    }]}
    atomic_json(store.plan_path, plan)

    pack = assemble_pack(store, 2)
    assert pack["hooks"][0]["id"] == "long-seal"
    assert len(pack["hooks"]) <= store.load_config()["pack_caps"]["hooks"]
    assert pack["omitted"]["hooks"] == 4


# ---------------------------------------------------------------- 1. 扩纲提示回流


def test_low_water_extend_plan_carries_overdue_hooks(store: BookStore):
    _commit_with_hooks(
        store,
        1,
        [{"id": "h-old", "due": 1, "status": "open", "text": "评定卷宗缴贡栏被描改"}],
    )
    # 已写到第 1 章、库存 7 章恰好压线低水位 5——不会触发；把库存砍到 5 章内
    plan = store.load_plan()
    plan["chapters"] = plan["chapters"][:6]
    atomic_json(store.plan_path, plan)
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    atomic_json(store.config_path, cfg)
    _set_head(store, phase="idle", last_committed_ch=1, last_acked_ch=1)

    action = chapter_next(store)
    assert action["action"] == "extend_plan"
    payload_ids = [h["id"] for h in action.get("overdue_hooks") or []]
    assert "h-old" in payload_ids
    assert action["overdue_hooks"][0]["overdue_by"] >= 1
    assert "effects.hooks" in action["hint"], "提示必须教策划编辑把逾期伏笔排进章拍"


def test_extend_plan_payload_omits_overdue_hooks_when_none(store: BookStore):
    _commit_with_hooks(store, 1, [])
    plan = store.load_plan()
    plan["chapters"] = plan["chapters"][:6]
    atomic_json(store.plan_path, plan)
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    atomic_json(store.config_path, cfg)
    _set_head(store, phase="idle", last_committed_ch=1, last_acked_ch=1)

    action = chapter_next(store)
    assert action["action"] == "extend_plan"
    assert not action.get("overdue_hooks")


# ---------------------------------------------------------------- 2. 草稿简报逾期指令


def test_draft_view_marks_overdue_hooks_with_directive():
    pack = {
        "chapter": 5,
        "hooks": [
            {"id": "h-late", "text": "明日去溪口", "status": "open", "due": 3},
            {"id": "h-future", "text": "腊月初三赎押契", "status": "open", "due": 40},
        ],
    }
    view = make_draft_view(pack)
    blob = json.dumps(view, ensure_ascii=False)
    assert "已逾期 2 章" in blob
    # 处置口径要写清阶段归属：演出在正文、结钩申报在组装步
    assert "把兑现演进正文" in blob
    assert "state_delta.hooks" in blob and "置 paid" in blob
    # 未逾期的伏笔不得误报
    late_marker = blob.count("已逾期")
    assert late_marker == 1, "只有逾期伏笔带回收指令"


# ---------------------------------------------------------------- 3. hooks merge


def test_merge_hook_collapses_duplicate_and_keeps_chain_green(store: BookStore):
    _commit_with_hooks(
        store,
        1,
        [{"id": "hook-c21-fu", "due": 25, "status": "open", "text": "福线伏笔"}],
    )
    _commit_with_hooks(
        store,
        2,
        [{"id": "hook-c23-fu", "due": 30, "status": "open", "text": "福线伏笔续"}],
    )
    _commit_with_hooks(store, 3, [])

    result = merge_hook(store, from_id="hook-c21-fu", into_id="hook-c23-fu")
    assert result["action"] == "hook_merged"
    ids = [h["id"] for h in load_snapshot(store)["hooks"]]
    assert "hook-c21-fu" not in ids
    assert ids.count("hook-c23-fu") == 1, "快照按 id 合并语义收敛成一条"
    verified = verify_ledger(store)
    assert verified["event_chain_ok"], verified.get("event_chain_issues")


def test_merge_hook_rejects_unknown_target(store: BookStore):
    _commit_with_hooks(store, 1, [{"id": "h-a", "due": 5, "status": "open", "text": "a"}])
    with pytest.raises(LedgerError):
        merge_hook(store, from_id="h-a", into_id="h-ghost")
    with pytest.raises(LedgerError):
        merge_hook(store, from_id="h-a", into_id="h-a")


# ---------------------------------------------------------------- 4. 默认 status 口径


def test_new_hook_defaults_to_open_status(store: BookStore):
    _commit_with_hooks(store, 1, [{"id": "h-bare", "due": 9, "text": "无状态伏笔"}])
    snap_hook = {h["id"]: h for h in load_snapshot(store)["hooks"]}["h-bare"]
    assert snap_hook.get("status") == "open"
    # audit 口径：无 status 的 hook 现在算活跃，不再与 pack 注入口径分叉
    audit = audit_hooks(store)
    assert any(h["id"] == "h-bare" for h in audit["active_hooks"])


# ---------------------------------------------------------------- 5. 章纲层预检


def test_draft_action_flags_hooks_due_unplanned(store: BookStore):
    """章纲漏排：due ≤ 本章且未进本章 effects.hooks 的伏笔，开写前在 draft action 亮出。"""
    _commit_with_hooks(
        store,
        1,
        [{"id": "h-trip", "due": 2, "status": "open", "text": "溪口明日我自己去一趟"}],
    )
    _set_head(store, phase="idle", last_committed_ch=1, last_acked_ch=1)

    action = chapter_next(store)
    assert action["action"] == "draft"
    flagged = [h["id"] for h in action.get("hooks_due_unplanned") or []]
    assert "h-trip" in flagged


def test_draft_action_quiet_when_beats_cover_due_hook(store: BookStore):
    """章拍排了回收：该章 beats 的 effects.hooks 引用了到期伏笔，预检不再点名。"""
    _commit_with_hooks(
        store,
        1,
        [{"id": "h-trip", "due": 2, "status": "open", "text": "溪口明日我自己去一趟"}],
    )
    plan = store.load_plan()
    for ch in plan["chapters"]:
        if ch.get("chapter") == 2:
            ch["beats"][0]["effects"] = {
                "hooks": [{"id": "h-trip", "status": "paid", "text": "溪口行程演出"}]
            }
    atomic_json(store.plan_path, plan)
    _set_head(store, phase="idle", last_committed_ch=1, last_acked_ch=1)

    action = chapter_next(store)
    assert action["action"] == "draft"
    flagged = [h["id"] for h in action.get("hooks_due_unplanned") or []]
    assert "h-trip" not in flagged


def test_draft_action_ignores_paid_and_future_hooks(store: BookStore):
    _commit_with_hooks(
        store,
        1,
        [
            {"id": "h-done", "due": 1, "status": "paid", "text": "已兑现"},
            {"id": "h-later", "due": 9, "status": "open", "text": "远期伏笔"},
        ],
    )
    _set_head(store, phase="idle", last_committed_ch=1, last_acked_ch=1)

    action = chapter_next(store)
    assert action["action"] == "draft"
    assert not action.get("hooks_due_unplanned")


# ---------------------------------------------------------------- 6. 扩纲 brief 落盘 + due 对排期


def test_low_water_extend_plan_writes_brief_file(store: BookStore):
    """扩纲机器载荷落盘：职责真源在盘上，旧派发上下文不再能腐蚀（第2批实战事故）。"""
    _commit_with_hooks(
        store,
        1,
        [{"id": "h-old", "due": 1, "status": "open", "text": "逾期伏笔"}],
    )
    plan = store.load_plan()
    plan["chapters"] = plan["chapters"][:6]
    atomic_json(store.plan_path, plan)
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    atomic_json(store.config_path, cfg)
    _set_head(store, phase="idle", last_committed_ch=1, last_acked_ch=1)

    action = chapter_next(store)
    assert action["action"] == "extend_plan"
    brief_path = Path(action["plan_worker_brief"]["path"])
    assert brief_path.exists()
    assert brief_path.name == "plan-extend-brief.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    assert brief["schema"] == "novel-ledger.plan-brief.v1"
    assert brief["suggest_from"] == 7
    assert any(h["id"] == "h-old" for h in brief["overdue_hooks"] or [])
    assert "worker-protocol" in brief["protocol_card"]


def test_hooks_schedule_gaps_flags_due_before_reference(store: BookStore):
    """due 抢在 effects.hooks 首次引用章之前的，对账点名；对齐后消失。"""
    from novel_ledger_core.control.pipeline import hooks_schedule_gaps, validate_plan

    _commit_with_hooks(
        store,
        1,
        [{"id": "h-sched", "due": 3, "status": "open", "text": "银柜夹层"}],
    )
    plan = store.load_plan()
    for ch in plan["chapters"]:
        if ch.get("chapter") == 5:
            ch["beats"][0]["effects"] = {
                "hooks": [{"id": "h-sched", "status": "paid", "text": "开柜"}]
            }
    atomic_json(store.plan_path, plan)

    gaps = hooks_schedule_gaps(store)
    assert gaps == [{"id": "h-sched", "due": 3, "scheduled_at": 5}]
    codes = [w.get("code") for w in validate_plan(store).get("warnings") or []]
    assert "hook_due_precedes_schedule" in codes

    defer_hook(store, hook_id="h-sched", new_due=5)
    assert hooks_schedule_gaps(store) == []
    codes = [w.get("code") for w in validate_plan(store).get("warnings") or []]
    assert "hook_due_precedes_schedule" not in codes


def test_hooks_schedule_gaps_ignores_unreferenced_and_paid(store: BookStore):
    """无 effects.hooks 引用的钩不猜文本（由逐章预检兜底）；paid/closed 不参与对账。"""
    from novel_ledger_core.control.pipeline import hooks_schedule_gaps

    _commit_with_hooks(
        store,
        1,
        [
            {"id": "h-textonly", "due": 2, "status": "open", "text": "只在拍点文本里"},
            {"id": "h-done", "due": 1, "status": "paid", "text": "已结"},
        ],
    )
    plan = store.load_plan()
    for ch in plan["chapters"]:
        if ch.get("chapter") == 4:
            ch["beats"][0]["effects"] = {
                "hooks": [{"id": "h-done", "status": "paid", "text": "x"}]
            }
    atomic_json(store.plan_path, plan)
    assert hooks_schedule_gaps(store) == []


# ---------------------------------------------------------------- 7. 逾期未声明检查


def test_overdue_hooks_undeclared_flags_declares_and_defers(store: BookStore):
    """已过 due 仍 open 且无未写章 effects.hooks 引用 → 点名；声明或 defer 后消失。"""
    from novel_ledger_core.control.pipeline import overdue_hooks_undeclared, validate_plan

    _commit_with_hooks(store, 1, [{"id": "h-od", "due": 1, "status": "open", "text": "逾期未排"}])
    _set_head(store, phase="idle", last_committed_ch=1, last_acked_ch=1)

    items = overdue_hooks_undeclared(store)
    assert [i["id"] for i in items] == ["h-od"]
    codes = [w.get("code") for w in validate_plan(store).get("warnings") or []]
    assert "overdue_hook_not_declared" in codes

    plan = store.load_plan()
    for ch in plan["chapters"]:
        if ch.get("chapter") == 2:
            ch["beats"][0]["effects"] = {"hooks": [{"id": "h-od", "status": "paid", "text": "回收"}]}
    atomic_json(store.plan_path, plan)
    assert overdue_hooks_undeclared(store) == []
    codes = [w.get("code") for w in validate_plan(store).get("warnings") or []]
    assert "overdue_hook_not_declared" not in codes

    defer_hook(store, hook_id="h-od", new_due=9)
    assert overdue_hooks_undeclared(store) == []


def test_audit_counts_deferred_status_as_active(store: BookStore):
    """防御性归一：写者自创 status=deferred（改期）时不得从 audit 活跃口径里消失。

    词表纪律已入 ledger-editor 卡（改期只改 due），这里是读层兜底。
    """
    _commit_with_hooks(
        store,
        1,
        [
            {"id": "h-ghost", "due": 9, "status": "deferred", "text": "写者自创状态"},
            {"id": "h-norm", "due": 9, "status": "open", "text": "正常"},
        ],
    )
    audit = audit_hooks(store)
    ids = {h["id"] for h in audit["active_hooks"]}
    assert {"h-ghost", "h-norm"} <= ids
    assert audit["active_count"] == 2


# ---------------------------------------------------------------- 8. relations 治理 + style 轻解锁


def test_relations_rename_unifies_variant_and_reseals(store: BookStore):
    from novel_ledger_core.control.pipeline import rename_relation
    from novel_ledger_core.ledger.ledger import verify_ledger, ledger_hygiene_issues, load_snapshot

    commit_event(
        store,
        chapter=1,
        state_delta={
            "named": ["主角"],
            "facts": [],
            "relations": [
                {"who": "张铁", "target": "盟会执事孙典", "kind": "催查", "status": "open"},
                {"who": "张铁", "target": "孙典", "kind": "对质", "status": "open"},
            ],
        },
    )
    snap = load_snapshot(store)
    variants = [i for i in ledger_hygiene_issues(store, snap) if i["code"] == "relation_name_variant_suspected"]
    assert variants, "改名前应报变体"

    result = rename_relation(store, from_name="盟会执事孙典", to_name="孙典")
    assert result["entries"] >= 1
    snap2 = load_snapshot(store)
    pairs = {(r["who"], r["target"]) for r in snap2["relations"]}
    assert ("张铁", "盟会执事孙典") not in pairs
    assert ("张铁", "孙典") in pairs
    verified = verify_ledger(store)
    assert verified["event_chain_ok"], verified.get("event_chain_issues")
    assert not [
        i for i in ledger_hygiene_issues(store, snap2)
        if i["code"] == "relation_name_variant_suspected"
    ]
    with pytest.raises(LedgerError):
        rename_relation(store, from_name="不存在的名字", to_name="孙典")


def test_relations_close_converges_pair_with_ruling(store: BookStore):
    from novel_ledger_core.control.pipeline import close_relation
    from novel_ledger_core.ledger.ledger import load_snapshot, verify_ledger

    commit_event(
        store,
        chapter=1,
        state_delta={
            "named": ["主角"],
            "facts": [],
            "relations": [
                {"who": "张铁", "target": "贾文茂", "kind": "总库名目之约", "status": "open"},
                {"who": "张铁", "target": "贾文茂", "kind": "改籍保批之约", "status": "open"},
            ],
        },
    )
    result = close_relation(
        store, who="张铁", target="贾文茂", kind_substring="总库名目", reason="名目已用讫（测试裁决）"
    )
    assert result["entries"] == 1
    snap = load_snapshot(store)
    by_kind = {r["kind"]: r for r in snap["relations"]}
    assert by_kind["总库名目之约"]["status"] == "closed"
    assert "测试裁决" in by_kind["总库名目之约"]["close_reason"]
    assert by_kind["改籍保批之约"]["status"] == "open"
    assert verify_ledger(store)["event_chain_ok"]
    with pytest.raises(LedgerError):
        close_relation(store, who="张铁", target="贾文茂", kind_substring="不存在", reason="x")


def test_retry_authorize_style_unlocks_in_place(store: BookStore):
    """style 耗尽 blocked 的轻解锁：回 await_polish、重置计数、零回滚、留痕。"""
    from novel_ledger_core.control.pipeline import retry_authorize
    from novel_ledger_core.ledger.ledger import read_events

    commit_event(store, chapter=1, state_delta={"named": ["主角"], "facts": []})
    events_before = len(read_events(store))
    _set_head(
        store,
        phase="blocked",
        chapter=2,
        last_committed_ch=1,
        last_acked_ch=1,
        blocked={
            "chapter": 2,
            "reason": "style_metrics_failed",
            "style_metrics_retry": 2,
            "style_metrics_limit": 2,
            "fails": ["全角分号×2"],
            "metrics_path": "book/staging/style-metrics-2.json",
        },
        style_metrics_retry=2,
        style_metrics_pending=False,
    )

    result = retry_authorize(store, actor="总编辑", reason="两个分号定点修", action="style")
    assert result["verdict"] == "authorized"
    head = store.read_head()
    assert head["phase"] == "await_polish"
    assert head["style_metrics_retry"] == 0
    assert head["style_metrics_pending"] is True
    assert head["blocked"] is None
    # 零回滚：账本事件一条没少（对照 rewrite 路径的整章回滚）
    assert len(read_events(store)) == events_before

    # 非法入口：非 style-blocked 拒绝；已入账拒绝（须走 rewrite）
    _set_head(store, phase="blocked", blocked={"chapter": 2, "reason": "plot_blocker"})
    with pytest.raises(LedgerError):
        retry_authorize(store, actor="总编辑", reason="x", action="style")
    _set_head(
        store,
        phase="blocked",
        blocked={"chapter": 2, "reason": "style_metrics_failed"},
        last_committed_ch=2,
        last_acked_ch=1,
    )
    with pytest.raises(LedgerError):
        retry_authorize(store, actor="总编辑", reason="x", action="style")


def test_defer_then_status_reflects_new_due(store: BookStore):
    _commit_with_hooks(store, 1, [{"id": "h-d", "due": 2, "status": "open", "text": "d"}])
    defer_hook(store, hook_id="h-d", new_due=15)
    assert {h["id"]: h for h in load_snapshot(store)["hooks"]}["h-d"]["due"] == 15
    assert verify_ledger(store)["event_chain_ok"]
    wm = status(store)
    assert wm["ok"] is True
