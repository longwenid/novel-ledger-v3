"""长跑有界性证明（吸收 novel-forge test_long_run 模式，适配 ledger 架构）。

百万字承诺的直接证据：合成 1000 章提交后——
1. `ledger verify` 全绿（重放一致 + 哈希链完整）；
2. 快照可由事件真源逐字段重建；
3. 工作包只加载当前阶段、近章与定向召回；旧历史保留索引，不逐章全文注入。

合成路径只走确定性 API（commit_event / update_hierarchical_memory），
不发起任何模型请求——与 execution-architecture.md「验证 1000 章用合成 L1 本地重建」
的口径一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.content.hierarchical_memory import (
    memory_for_pack,
    update_hierarchical_memory,
)
from novel_ledger_core.content.pack import assemble_pack
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, atomic_text, canonical_json
from novel_ledger_core.ledger.ledger import (
    commit_event,
    load_snapshot,
    replay_events,
    select_conditions,
    select_items,
    verify_ledger,
)

CHAPTERS = 1000
ENTITIES = ["主角", "掌柜", "老周", "阿禾", "说书人", "镖头", "药婆", "账房"]
LOCATIONS = ["市集", "货栈", "码头", "城南", "山道"]


def _plan() -> dict:
    return {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：长跑验证。",
        "chapters": [
            {
                "chapter": n,
                "volume": (n - 1) // 100 + 1,
                "location": LOCATIONS[n % len(LOCATIONS)],
                "present": [ENTITIES[n % len(ENTITIES)], "主角"],
                "beats": [
                    {"id": f"b{n}a", "required": True, "text": f"第{n}章推进", "must": "推进"},
                    {"id": f"b{n}b", "required": True, "text": f"第{n}章转折", "must": "转折"},
                ],
            }
            for n in range(1, CHAPTERS + 1)
        ],
    }


def _make_store(tmp_path: Path) -> BookStore:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["word_band_enforce"] = False
    store.save_config(cfg)
    return store


def _synth_chapter(store: BookStore, n: int) -> None:
    who = ENTITIES[n % len(ENTITIES)]
    hooks = [{"id": f"h{n}", "text": f"第{n}章埋下的钩子", "due": n + 9, "status": "open"}]
    if n > 10 and n % 3 == 0:
        # 收掉旧钩子：让 hooks 账本在长跑里保持流动，而不是只增不减
        hooks.append({"id": f"h{n - 9}", "text": f"第{n - 9}章埋下的钩子", "due": n, "status": "paid"})
    delta = {
        "moves": [{"who": "主角", "to": LOCATIONS[n % len(LOCATIONS)]}] if n % 2 else [],
        "facts": [{"who": who, "text": f"第{n}章的事实：{who}在场。"}],
        "debts": [] if n % 4 else [{"id": f"d{n}", "who": who, "text": f"第{n}章的债", "status": "open"}],
        "hooks": hooks,
        "relations": [] if n % 5 else [{"who": "主角", "target": who, "kind": "旧交", "status": "open"}],
        "named": [who, "主角"],
        "new_names": [who] if n < len(ENTITIES) else [],
        "deaths": [],
        "conditions": [] if n % 6 else [{"who": who, "kind": "伤势", "text": f"第{n}章落下的伤", "status": "open"}],
    }
    # Sparse lifecycle events exercise the same id across distant volumes. The
    # working set stays small; a 1000-chapter book need not inject old prose.
    if n == 1:
        delta["facts"].append({"who": "主角", "text": "左腕永久伤痕", "pin": True})
        delta["debts"].append({"id": "long_debt", "who": "主角", "text": "欠账待清", "status": "open"})
        delta["hooks"].append({"id": "long_hook", "text": "旧案真相", "due": 800, "status": "open"})
        delta["relations"].append({"who": "主角", "target": "掌柜", "kind": "旧盟", "status": "open"})
        delta["items"] = [{"id": "heirloom", "name": "旧剑", "holder": "主角", "status": "held", "quantity": 1}]
        delta["conditions"].append({"who": "主角", "kind": "伤势", "text": "左腕残缺", "irreversible": True})
        delta["new_names"].append("失踪人")
    if n == 400:
        delta["items"] = [{"id": "heirloom", "holder": "掌柜", "status": "transferred"}]
    if n == 450:
        delta["deaths"] = ["失踪人"]
    if n == 500:
        delta["conditions"].append({"who": "主角", "kind": "伤势", "text": "新伤未愈", "status": "active"})
    if n == 600:
        delta["debts"].append({"id": "long_debt", "status": "paid"})
        delta["relations"].append({"who": "主角", "target": "掌柜", "kind": "旧盟", "status": "closed"})
    if n == 700:
        delta["conditions"].append({"who": "主角", "kind": "伤势", "text": "新伤未愈", "status": "resolved"})
    if n == 800:
        delta["hooks"].append({"id": "long_hook", "status": "paid"})
        delta["items"] = [{"id": "heirloom", "holder": "主角", "status": "transferred"}]
    if n == 900:
        delta["revivals"] = ["失踪人"]
    if n == 1000:
        delta["items"] = [{"id": "heirloom", "status": "used", "quantity": 0}]
    atomic_text(
        store.chapter_md_path(n),
        f"第{n}章正文。{who}在{LOCATIONS[n % len(LOCATIONS)]}推进了一段长跑验证剧情，并完成转折。\n",
    )
    commit_event(store, n, delta)
    summary = f"第{n}章：{who}在场，剧情推进并转折。"
    atomic_json(store.summary_path(n), {"chapter": n, "l1_summary": summary})
    update_hierarchical_memory(store, n, summary)


def _pack_chars(store: BookStore, chapter: int) -> int:
    return len(canonical_json(assemble_pack(store, chapter)).decode("utf-8"))


@pytest.mark.slow
def test_long_run_loads_relevant_history_and_keeps_ledger_rebuildable(tmp_path: Path):
    store = _make_store(tmp_path)
    for n in range(1, CHAPTERS + 1):
        with store.transaction():
            _synth_chapter(store, n)
        if n == 400:
            assert [item["id"] for item in select_items(store, names=["掌柜"])[0]] == ["heirloom"]
            assert select_items(store, names=["主角"])[0] == []
        if n == 800:
            assert [item["id"] for item in select_items(store, names=["主角"])[0]] == ["heirloom"]
            assert select_items(store, names=["掌柜"])[0] == []

    head = store.read_head()
    head.update(last_committed_ch=CHAPTERS, last_acked_ch=CHAPTERS)
    store.write_head(head)
    historical = assemble_pack(store, CHAPTERS)["historical_recall"]
    assert historical["records"]
    assert historical["chars"] == sum(len(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) for row in historical["records"])
    assert all(row["prose_path"] == str(store.chapter_md_path(row["chapter"])) for row in historical["records"])
    assert all(row["chapter"] < CHAPTERS for row in historical["records"])

    # 1) 账本在长跑末端仍然可验证：重放一致 + 哈希链完整
    report = verify_ledger(store)
    assert report["consistent"], report["diffs"]
    assert report["event_chain_ok"], report["event_chain_issues"]
    assert report["events"] == CHAPTERS

    # 2) 快照可由事件真源逐字段重建（不是"大致像"，是逐字段相等）
    rebuilt = replay_events(store)
    current = load_snapshot(store)
    for field in ("chapter", "entities", "debts", "hooks", "relations", "occupancy", "items", "conditions"):
        assert rebuilt.get(field) == current.get(field), f"rebuild diverges on {field}"

    # Long lived assets survive through chapter 1000, while settled or consumed
    # assets stop appearing in the next writer's working set.
    assert current["entities"]["失踪人"]["dead"] is False
    assert current["entities"]["失踪人"]["revived_chapter"] == 900
    assert current["entities"]["主角"]["facts"][0]["text"] == "左腕永久伤痕"
    assert next(item for item in current["debts"] if item["id"] == "long_debt")["status"] == "paid"
    assert next(item for item in current["hooks"] if item["id"] == "long_hook")["status"] == "paid"
    assert next(item for item in current["relations"] if item["kind"] == "旧盟")["status"] == "closed"
    assert select_items(store, names=["主角", "掌柜"])[0] == []
    shown_conditions, _ = select_conditions(store, names=["主角"])
    assert any(item["text"] == "左腕残缺" for item in shown_conditions)
    assert all(item["text"] != "新伤未愈" for item in shown_conditions)

    # 3) Later chapters select their own completed phase, not all old prose.
    mid = CHAPTERS // 2
    for chapter in (mid, mid + 60, CHAPTERS):
        pack = assemble_pack(store, chapter)
        selected = pack["memory_layers"]
        assert int(selected["phase_summary"]["end_chapter"]) < chapter
        assert "第1章：" not in selected["phase_summary"]["summary"]
        assert selected["volume_summary"]["loaded_phase_ids"] == [selected["phase_summary"]["id"]]
        assert all(row["chapter"] < chapter for row in pack["near_summaries"])

    # 4) The full hierarchy remains available through explicit source indexes.
    layers = memory_for_pack(store, CHAPTERS + 1)
    assert layers["book_spine"]["source_path"] == str(store.hierarchical_memory_path)
    assert layers["phase_summary"]["end_chapter"] == CHAPTERS
    assert f"第{CHAPTERS}章：" in layers["phase_summary"]["summary"]
    assert "第1章：" not in layers["phase_summary"]["summary"]
