"""book audit 的节奏与重复 advisory（吸收 novel-forge integrity-and-pacing 检查思想）。

两条检查都只出 findings、不做 blocker：节奏的最终裁量在总编辑/作者，机器提供的是
「期待管理停摆」与「卷间复读」的可见信号。
"""

from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    _cross_volume_repetition,
    _pacing_flat_run,
    audit_book,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, atomic_text
from novel_ledger_core.ledger.ledger import commit_event


def _make_store(tmp_path: Path, volumes: dict[int, int]) -> BookStore:
    """volumes: {卷号: 章数}，全部章编入对应卷。"""
    chapters = []
    n = 1
    for volume, count in sorted(volumes.items()):
        for _ in range(count):
            chapters.append(
                {
                    "chapter": n,
                    "volume": volume,
                    "location": "市集",
                    "present": ["主角"],
                    "beats": [{"id": f"b{n}", "required": True, "text": f"第{n}章推进", "must": "推进"}],
                }
            )
            n += 1
    plan = {"title": "", "protagonist": "主角", "volume_spine": "长卷。", "chapters": chapters}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    return BookStore(proj)


def _commit(store: BookStore, n: int, *, hooks: list | None = None, debts: list | None = None) -> None:
    atomic_text(store.chapter_md_path(n), f"第{n}章正文，剧情推进。\n")
    commit_event(
        store,
        n,
        {
            "moves": [],
            "facts": [{"who": "主角", "text": f"第{n}章的事实"}],
            "debts": debts or [],
            "hooks": hooks or [],
            "relations": [],
            "named": ["主角"],
            "new_names": [],
            "deaths": [],
        },
    )
    atomic_json(store.summary_path(n), {"chapter": n, "l1_summary": f"第{n}章：剧情推进。"})


def test_flat_run_reports_long_stretch_without_hooks_or_settlements(tmp_path: Path):
    store = _make_store(tmp_path, {1: 14})
    _commit(store, 1, hooks=[{"id": "h1", "text": "开篇钩子", "due": 5, "status": "open"}])
    _commit(store, 2, debts=[{"id": "d1", "who": "主角", "text": "旧债", "status": "paid"}])
    for n in range(3, 15):
        _commit(store, n)  # 12 章无埋无收

    report = audit_book(store)
    flat = report["pacing"]["flat_run"]
    assert flat is not None
    assert flat["length"] == 12
    assert flat["from_chapter"] == 3 and flat["to_chapter"] == 14


def test_flat_run_stays_quiet_when_beats_keep_flowing(tmp_path: Path):
    store = _make_store(tmp_path, {1: 14})
    for n in range(1, 15):
        hooks = (
            [{"id": f"h{n}", "text": f"第{n}章钩子", "due": n + 9, "status": "open"}]
            if n % 3 == 1
            else []
        )
        _commit(store, n, hooks=hooks)

    report = audit_book(store)
    assert report["pacing"]["flat_run"] is None, report["pacing"]


def test_repeating_unchanged_open_hook_does_not_hide_flat_run(tmp_path: Path):
    store = _make_store(tmp_path, {1: 12})
    for n in range(1, 13):
        _commit(store, n, hooks=[{
            "id": "same", "text": "同一个未变的问题", "due": 20, "status": "open",
        }])
    flat = audit_book(store)["pacing"]["flat_run"]
    assert flat is not None
    assert flat["from_chapter"] == 2
    assert flat["length"] == 11


def test_cross_volume_repetition_flags_near_identical_summaries(tmp_path: Path):
    store = _make_store(tmp_path, {1: 4, 2: 4})
    for n in range(1, 9):
        _commit(store, n)
    # 两卷摘要改成近似同文（复读机信号）
    for n in range(1, 9):
        volume = 1 if n <= 4 else 2
        text = f"第{volume}卷的同一套剧情：主角在市集反复拒收改期，掌柜反复压账，日复一日没有新事。"
        atomic_json(store.summary_path(n), {"chapter": n, "l1_summary": text})

    findings = _cross_volume_repetition(store, store.load_plan())
    assert findings, "两卷近似同文必须报卷间重复"
    assert findings[0]["volumes"] == [1, 2]
    assert findings[0]["overlap"] >= 0.5

    report = audit_book(store)
    assert report["pacing"]["cross_volume_repetition"]


def test_cross_volume_repetition_stays_quiet_on_distinct_volumes(tmp_path: Path):
    store = _make_store(tmp_path, {1: 4, 2: 4})
    for n in range(1, 9):
        _commit(store, n)
    for n in range(1, 9):
        volume = 1 if n <= 4 else 2
        theme = "市集周旋与旧债" if volume == 1 else "北上山道寻访故人，旧案翻出新线索"
        atomic_json(
            store.summary_path(n),
            {"chapter": n, "l1_summary": f"第{volume}卷：{theme}。节奏各异，事件不同。"},
        )

    assert _cross_volume_repetition(store, store.load_plan()) == []


def test_cross_volume_repetition_accepts_string_volume_labels(tmp_path: Path):
    """卷号是字符串（hatch 的 "vol-01"、或 "第二卷"）时不得把 book audit 炸成 internal error。

    store.volume_meta 明文支持字符串卷号；卷间重复检测的分组键必须能吃字符串。
    """
    store = _make_store(tmp_path, {1: 4, 2: 4})
    # 把计划里的整型卷号换成 hatch 风格字符串卷号
    plan = store.load_plan()
    for item in plan["chapters"]:
        item["volume"] = f"vol-{item['volume']:02d}"
    from novel_ledger_core.infra.util import atomic_json as _aj

    _aj(store.plan_path, plan)
    for n in range(1, 9):
        _commit(store, n)
    for n in range(1, 9):
        volume = 1 if n <= 4 else 2
        text = f"第{volume}卷的同一套剧情：主角在市集反复拒收改期，掌柜反复压账，日复一日没有新事。"
        atomic_json(store.summary_path(n), {"chapter": n, "l1_summary": text})

    findings = _cross_volume_repetition(store, store.load_plan())
    assert findings, "字符串卷号的卷间重复同样必须报出"
    assert findings[0]["volumes"] == ["vol-01", "vol-02"]

    report = audit_book(store)
    assert report["pacing"]["cross_volume_repetition"]
