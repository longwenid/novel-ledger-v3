"""事实一致性在四种收口点的接线：提交闸、book audit、book reconcile、检查点/完本。

判据本身由 `test_consistency_neutral.py` 覆盖；本文件只测**接线**——
一份声明了取值域的书稿，必须在每个该拦住它的地方被点到名。
默认（`fact_keys` 为空）不产生任何发现，否则会误伤存量项目。
"""

from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.control.autopilot import _completion_audit_blockers, _checkpoint_report
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import audit_book, book_facts, calibrate_book, reconcile_book
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, atomic_text
from novel_ledger_core.ledger.ledger import commit_event

# 抽象夹具：与任何具体作品无关。
_NUMERIC_A = "他当年十一岁，跟着驮队进山。"
_NUMERIC_B = "后来的一处追述却写他十六岁那年才进山。"


def _store(tmp_path: Path, *, chapters: int = 3) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "甲",
        "volume_spine": "追查。",
        "chapters": [
            {
                "chapter": number,
                "volume": 1,
                "location": "市集",
                "present": ["甲"],
                "beats": [{"id": f"b{number}", "required": True, "text": "推进", "must": "推进"}],
            }
            for number in range(1, chapters + 1)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="甲")
    return BookStore(project)


def _commit(store: BookStore, number: int, prose: str) -> None:
    atomic_text(store.chapter_md_path(number), prose)
    commit_event(
        store,
        number,
        {"moves": [], "facts": [{"who": "甲", "text": f"第{number}章的事实"}], "debts": [],
         "hooks": [], "relations": [], "named": ["甲"], "new_names": [], "deaths": []},
    )
    atomic_json(store.summary_path(number), {"chapter": number, "l1_summary": f"第{number}章：推进。"})


def _declare(store: BookStore, key: str, spec: dict) -> None:
    cfg = store.load_config()
    cfg["fact_keys"] = {**(cfg.get("fact_keys") or {}), key: spec}
    store.save_config(cfg)


def test_empty_fact_keys_produce_no_findings(tmp_path: Path):
    """默认（未声明取值域）必须零发现：存量项目不该因为这条改动凭空报错。"""
    store = _store(tmp_path)
    _commit(store, 1, _NUMERIC_A)
    _commit(store, 2, _NUMERIC_B)
    _commit(store, 3, "第3章正文，剧情推进。\n")
    report = audit_book(store)
    assert report["fact_issues"] == []
    assert report["chapter_format"] == []
    assert report["fact_keys_declared"] == []
    assert report["fact_declarations_missing"]["code"] == "fact_declarations_missing"
    assert book_facts(store)["hits"] == []


def test_declared_fact_key_surfaces_in_audit_facts_and_reconcile(tmp_path: Path):
    store = _store(tmp_path)
    _commit(store, 1, _NUMERIC_A)
    _commit(store, 2, _NUMERIC_B)
    _commit(store, 3, "第3章正文，剧情推进。\n")
    _declare(store, "k_age", {"kind": "number", "observe": "岁", "canonical": 11})

    audit = audit_book(store)
    assert audit["fact_keys_declared"] == ["k_age"]
    assert audit["fact_issues"], "已声明的取值域命中必须进 book audit"
    assert audit["fact_issues"][0]["observed"] == 16
    assert audit["fact_declarations_missing"] is None

    facts = book_facts(store)
    assert facts["action"] == "book_facts"
    assert facts["summary"]["keys_with_hits"] == ["k_age"]

    reconcile = reconcile_book(store)
    assert reconcile["counts"]["fact"] >= 1
    assert "k_age" in json.dumps(reconcile["fact"], ensure_ascii=False)
    # 待办索引应指向命中章
    assert any(key.startswith("ch") for key in reconcile["chapters_to_fix"])


def test_reconcile_stays_clean_without_declarations(tmp_path: Path):
    store = _store(tmp_path)
    _commit(store, 1, "第1章正文，剧情推进。\n")
    reconcile = reconcile_book(store)
    assert reconcile["counts"]["fact"] == 0
    assert reconcile["clean"] is True


def test_chapter_format_defect_surfaces_in_audit(tmp_path: Path):
    store = _store(tmp_path)
    _commit(store, 1, "第1章 起\n正文推进。\n第1章 起\n（本章完）\n")
    report = audit_book(store)
    codes = {item["code"] for item in report["chapter_format"]}
    assert {"duplicated_chapter_header", "story_marker_residue"} <= codes


def test_unpaired_corner_quotes_surface_in_audit_quote_issues(tmp_path: Path):
    """引号配对必须查四套体例：只查弯引号会漏掉「」与『』。"""
    store = _store(tmp_path)
    _commit(store, 1, "他说：「走。」接着又说：「来。\n")
    report = audit_book(store)
    assert any(
        "cn_corner" in (item.get("unpaired_styles") or [])
        for item in report["quote_issues"]
    )


def test_near_duplicate_passages_surface_in_audit(tmp_path: Path):
    store = _store(tmp_path)
    prose = (
        "他推开那扇掉了漆的木门走进院子，看见堂屋的桌上摊着一本旧册子，"
        "封皮已经磨得发了白，边角卷起来露出一截发黄的纸芯。他伸手把册子翻开，"
        "一笔一笔记下当日的开销，又抬头看了看窗外渐渐暗下来的天色。"
    )
    _commit(store, 1, prose)
    _commit(store, 2, prose.replace("当日的开销", "昨日的开销"))
    _commit(store, 3, "她蹲在灶前添柴，锅里的水响了，屋里飘起米香。")
    report = audit_book(store)
    assert report["near_duplicate_passages"], "跨章近重复叙述必须进 book audit"
    assert report["near_duplicate_passages"][0]["chapters"] == [1, 2]


def test_checkpoint_blocks_on_hard_fact_conflict(tmp_path: Path):
    store = _store(tmp_path, chapters=3)
    _commit(store, 1, _NUMERIC_A)
    _commit(store, 2, _NUMERIC_B)
    _commit(store, 3, "第3章正文，剧情推进。\n")
    _declare(store, "k_age", {"kind": "number", "observe": "岁", "canonical": 11})

    report = _checkpoint_report(store, chapter=3, previous=0, kinds=("batch",))
    assert report["continuity"]["hit_count"] >= 1
    codes = {item.get("code") for item in report["blockers"]}
    assert "fact_value_conflict" in codes
    assert report["review_required"] is True


def test_checkpoint_is_advisory_only_for_shape_findings(tmp_path: Path):
    """形态类（近重复、格式、高频片段）只出 advisory，不把检查点拖成停线。"""
    store = _store(tmp_path, chapters=2)
    _commit(store, 1, "第1章 起\n正文推进。\n第1章 起\n")
    _commit(store, 2, "第2章 承\n正文继续推进。\n")
    report = _checkpoint_report(store, chapter=2, previous=0, kinds=())
    advisories = {item.get("code") for item in report["advisories"]}
    assert "chapter_format_drift" in advisories
    assert "fact_value_conflict" not in {item.get("code") for item in report["blockers"]}


def test_completion_audit_blocks_on_fact_and_format_findings():
    blockers = _completion_audit_blockers(
        {
            "ok": True,
            "action": "book_audit",
            "stats": {"total_chapters": 3},
            "fact_issues": [{"code": "fact_value_conflict"}],
            "chapter_format": [{"code": "scene_break_marker"}],
        }
    )
    codes = {item.get("code") for item in blockers}
    assert {"fact_issues", "chapter_format"} <= codes


def test_calibrate_proposes_fact_key_candidates_from_canon(tmp_path: Path):
    store = _store(tmp_path)
    kb_path = store.kb_path
    atomic_json(
        kb_path,
        [
            {"id": "口径卡--硬-时点与人数", "title": "口径卡", "body": "- 案发时点：八八年冬\n- 驮队共五人"},
        ],
    )
    out = calibrate_book(store)
    assert out["current_fact_keys"] == []
    kinds = {item["kind"] for item in out["fact_keys_candidates"]}
    assert {"date", "number"} <= kinds
    date_item = next(item for item in out["fact_keys_candidates"] if item["kind"] == "date")
    assert date_item["canonical"] == 1988
    assert "fact-registry" in out["fact_keys_hint"]
