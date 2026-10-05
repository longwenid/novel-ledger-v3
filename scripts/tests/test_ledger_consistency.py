"""账本内容一致性：引文完整性、身份卫生、正典漂移与 patch 断锚。

这些闸门补上"重放 vs 快照"看不见的一类漂移：账本结构与真源完全一致，但账本里的**值**
（金额、状态）已经和正文对不上——正文改尺后账本没跟着改，错值会顺着 NOW 卡灌进后续每一章。
"""

from __future__ import annotations

import json
from pathlib import Path


from novel_ledger_core.control.pipeline import _canon_drift, audit_book
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_text
from novel_ledger_core.ledger.ledger import (
    commit_event,
    ledger_hygiene_issues,
    ledger_quote_issues,
    ledger_quote_orphans,
    verify_ledger,
)

PLAN = {
    "title": "",
    "protagonist": "主角",
    "volume_spine": "第一卷。",
    "chapters": [
        {"chapter": 1, "location": "市集", "present": ["主角"], "tags": [], "beats": [{"id": "b1", "text": "开场拒收", "must": "拒收"}]},
        {"chapter": 2, "location": "货栈", "present": ["主角"], "tags": [], "beats": [{"id": "b2", "text": "续翻账", "must": "翻账"}]},
    ],
}

PROSE_1 = "主角在市集拒收改期的凭据。赵屠那笔十二块，利钱月二，本利十四块七。"
PROSE_2 = "主角夜里翻账。他把中人钱三百文当面销了。"


def _make_project(tmp_path: Path) -> BookStore:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    return BookStore(proj)


def _write_chapter(store: BookStore, chapter: int, prose: str) -> None:
    atomic_text(store.chapter_md_path(chapter), prose + "\n")


def test_verify_flags_quote_missing_from_prose(tmp_path: Path):
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    commit_event(
        store,
        1,
        {
            "debts": [
                # 正确引文：逐字来自 ch-0001
                {"id": "debt_ok", "who": "主角", "text": "赵屠那笔", "status": "open", "quote": "赵屠那笔十二块，利钱月二，本利十四块七"},
                # 幻觉引文：正文里没有（模拟正文改尺后账本停在旧值）
                {"id": "debt_bad", "who": "主角", "text": "赵屠那笔一百二十块", "status": "open", "quote": "你去年腊月借的一百二十块下品灵石"},
            ]
        },
    )

    res = verify_ledger(store)
    assert res["consistent"] is True  # 结构仍自洽：重放 == 快照
    assert res["quote_consistent"] is False
    bad = {q["id"] for q in res["quote_invalid"]}
    assert bad == {"debt_bad"}
    assert ledger_quote_issues(store) == res["quote_invalid"]


def test_hygiene_flags_duplicate_id_stem(tmp_path: Path):
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    _write_chapter(store, 2, PROSE_2)
    # 同一笔中人钱先挂一个 id，后面换了新 id 再当新账处理
    commit_event(store, 1, {"debts": [{"id": "d_zhongren", "who": "主角", "text": "中人钱", "status": "open"}]})
    commit_event(store, 2, {"debts": [{"id": "debt_zhongren_300", "who": "主角", "text": "中人钱", "status": "cancelled"}]})

    issues = ledger_hygiene_issues(store)
    stems = [i for i in issues if i["code"] == "duplicate_id_stem"]
    assert stems and stems[0]["ids"] == ["d_zhongren", "debt_zhongren_300"]


def test_hygiene_flags_relation_conflict_and_offers_supersedes(tmp_path: Path):
    """同一对角色并存多条 open 关系必须能被收敛。

    现场曾出现：关系以 (who, target, kind) 为键、kind 是自由文本，
    同一段关系每章换一个说法就多一条 open 条目——5 章后积出 2 条 relation_pair_conflict，
    其中一对挂了 3 条，而报警信息当时只教"把 status 改成 closed"，没给出可执行路径
    （旧 kind 原文很难逐字复现）。现在给 `supersedes` 这个显式动作。
    """
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    _write_chapter(store, 2, PROSE_2)
    commit_event(store, 1, {"relations": [{"who": "主角", "target": "掌柜", "kind": "交换条件结成的同盟", "status": "open"}]})
    commit_event(store, 2, {"relations": [{"who": "主角", "target": "掌柜", "kind": "谁也不许单独换取的合谋关系", "status": "open"}]})

    issues = ledger_hygiene_issues(store)
    conflicts = [i for i in issues if i["code"] == "relation_pair_conflict"]
    assert conflicts and conflicts[0]["pair"] == ["主角", "掌柜"]
    assert len(conflicts[0]["relations"]) == 2
    # 报警必须给出可执行动作，而不是"请把旧条目改掉"这种需要逐字复现 kind 的空话
    assert "supersedes" in conflicts[0]["hint"]


def test_relation_supersedes_retires_the_previous_open_entry(tmp_path: Path):
    """`supersedes: true` 把该对角色其它 open 条目置为 closed，冲突随之消失。"""
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    _write_chapter(store, 2, PROSE_2)
    commit_event(store, 1, {"relations": [{"who": "主角", "target": "掌柜", "kind": "交换条件结成的同盟", "status": "open"}]})
    commit_event(
        store,
        2,
        {
            "relations": [
                {
                    "who": "主角",
                    "target": "掌柜",
                    "kind": "翻脸后互相提防的债主关系",
                    "status": "open",
                    "supersedes": True,
                }
            ]
        },
    )

    from novel_ledger_core.ledger.ledger import load_snapshot

    stored = load_snapshot(store)["relations"]
    old = [r for r in stored if r["kind"] == "交换条件结成的同盟"][0]
    new = [r for r in stored if r["kind"] == "翻脸后互相提防的债主关系"][0]
    assert old["status"] == "closed"
    assert old["closed_chapter"] == 2
    assert new["status"] == "open"
    # 冲突不再上报
    assert not [i for i in ledger_hygiene_issues(store) if i["code"] == "relation_pair_conflict"]


def test_hygiene_flags_relation_name_variant(tmp_path: Path):
    """关系表里互相包含的两个名字＝疑似同一人物的两种叫法（现场：老药工 / 药圃老药工）。"""
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    _write_chapter(store, 2, PROSE_2)
    commit_event(store, 1, {"relations": [{"who": "主角", "target": "药圃老药工", "kind": "前辈提醒", "status": "open"}]})
    commit_event(store, 2, {"relations": [{"who": "主角", "target": "老药工", "kind": "前辈提醒", "status": "open"}]})

    issues = [i for i in ledger_hygiene_issues(store) if i["code"] == "relation_name_variant_suspected"]
    assert issues
    assert issues[0]["shorter"] == "老药工"
    assert issues[0]["longer"] == "药圃老药工"


def test_hygiene_flags_debt_cancelled_twice(tmp_path: Path):
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    _write_chapter(store, 2, PROSE_2)
    commit_event(store, 1, {"debts": [{"id": "d_z", "who": "主角", "text": "同一笔账", "status": "cancelled"}]})
    commit_event(store, 2, {"debts": [{"id": "d_z", "who": "主角", "text": "同一笔账", "status": "cancelled"}]})

    issues = ledger_hygiene_issues(store)
    repeat = [i for i in issues if i["code"] == "terminal_state_repeated"]
    assert repeat and repeat[0]["id"] == "d_z"
    assert [e["chapter"] for e in repeat[0]["events"]] == [1, 2]


def test_ledger_quote_orphans_detects_patch_removing_sole_evidence(tmp_path: Path):
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    commit_event(store, 1, {"debts": [{"id": "debt_ok", "who": "主角", "text": "赵屠那笔", "status": "open", "quote": "赵屠那笔十二块，利钱月二，本利十四块七"}]})

    after = PROSE_1.replace("赵屠那笔十二块，利钱月二，本利十四块七", "赵屠那笔一百二十块，利钱月二")
    orphans = ledger_quote_orphans(store, PROSE_1, after, chapter=1)
    assert [o["id"] for o in orphans] == ["debt_ok"]
    # 只在别处也补不回来时才报：若别的章正含同一句，则不算断锚
    _write_chapter(store, 2, "他把赵屠那笔十二块，利钱月二，本利十四块七 记在心上。")
    assert ledger_quote_orphans(store, PROSE_1, after, chapter=1) == []


def test_canon_fingerprint_and_drift(tmp_path: Path):
    store = _make_project(tmp_path)
    before = store.canon_fingerprint()
    _write_chapter(store, 1, PROSE_1)
    commit_event(store, 1, {}, {"canon_sha": before})

    # 与最近 commit 一致 → 不报漂移
    assert _canon_drift(store)["changed"] is False

    # 改正典（新增一张卡）→ 指纹变化，漂移报警
    cards = store.load_kb()
    cards.append({"id": "new--硬", "kind": "world", "title": "新规", "body": "验阵两三贯起。"})
    store.save_kb(cards)
    drift = _canon_drift(store)
    assert drift["available"] is True
    assert drift["changed"] is True
    assert drift["since_chapter"] == 1


def test_audit_surfaces_ledger_quote_and_drift(tmp_path: Path):
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    commit_event(store, 1, {"debts": [{"id": "debt_bad", "who": "主角", "text": "错值", "status": "open", "quote": "正文里没有的句子"}]})
    res = audit_book(store)
    assert res["ok"] is True
    assert res["ledger_quote_consistent"] is False
    assert any(q["id"] == "debt_bad" for q in res["ledger_quote_invalid"])
    assert "canon_drift" in res


def test_facts_and_deaths_quotes_are_gated_from_events(tmp_path: Path):
    """facts/deaths 的 quote 不在快照里（只存 text/who），必须回事件源校验。

    回归：曾只覆盖 debts/hooks/relations，整章重写可静默改断 facts 引文而 verify 报绿。
    """
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    commit_event(
        store,
        1,
        {
            "facts": [
                {"who": "主角", "text": "正确事实", "quote": "主角在市集拒收改期的凭据"},
                {"who": "主角", "text": "错值事实", "quote": "正文里没有的旧值句子"},
            ],
            "deaths": [{"who": "配角", "quote": "配角倒在雪里，再没起来"}],
        },
    )

    res = verify_ledger(store)
    assert res["consistent"] is True  # 结构仍自洽
    assert res["quote_consistent"] is False
    kinds = {(q["kind"], q["quote"]) for q in res["quote_invalid"]}
    assert ("facts", "正文里没有的旧值句子") in kinds
    assert ("deaths", "配角倒在雪里，再没起来") in kinds
    assert ("facts", "主角在市集拒收改期的凭据") not in kinds


def test_facts_quote_orphan_blocks_patch_that_drops_it(tmp_path: Path):
    """patch 改断 facts 引文（且别处补不回）时 fail-closed。"""
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    commit_event(store, 1, {"facts": [{"who": "主角", "text": "拒收", "quote": "主角在市集拒收改期的凭据"}]})

    after = PROSE_1.replace("主角在市集拒收改期的凭据", "主角在市集收下了改期的凭据")
    orphans = ledger_quote_orphans(store, PROSE_1, after, chapter=1)
    assert [(o["kind"], o["quote"]) for o in orphans] == [("facts", "主角在市集拒收改期的凭据")]


def test_occupancy_replay_matches_sorted_snapshot_order(tmp_path: Path):
    """两个具名角色同处一地时 verify 不得误报 occupancy 分歧。

    snapshot.json 以 canonical_json（sort_keys）落盘，实体顺序是字典序；重放按事件
    创建序展开实体。occupancy 列表必须与两者都无关（就地按名排序），否则只要创建序
    ≠ 字典序（几乎必然），verify_ledger 就在正常长跑里假红。
    """
    store = _make_project(tmp_path)
    _write_chapter(store, 1, PROSE_1)
    commit_event(
        store,
        1,
        {
            # 创建序：阿三(U+963F) 先于 卜二(U+535C)；字典序恰好相反
            "moves": [{"who": "阿三", "to": "市集"}, {"who": "卜二", "to": "市集"}],
            "named": ["阿三", "卜二"],
        },
    )
    res = verify_ledger(store)
    assert res["consistent"] is True, res["diffs"]
    assert "occupancy" not in res["diffs"]
    from novel_ledger_core.ledger.ledger import load_snapshot, replay_events

    assert replay_events(store)["occupancy"] == load_snapshot(store)["occupancy"]
    assert load_snapshot(store)["occupancy"]["市集"] == ["卜二", "阿三"]
