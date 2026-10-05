"""状态模型（不可逆代价）、切片省略计数、正典同步守卫 —— 第三轮完善项的回归测试。

三条都对应「文档/设计声称有，机器通道却没有」的同一类缺陷：

- 伤势/能力/债务这类状态在正典里写得最硬，账本里却没有任何结构模型——单章看不出来，
  是长篇最常见的崩盘点（断肢下一章又握剑）；
- 12 个 pack cap 里只有 2 个会报省略，写者拿着有界视野却以为它是全集；
- `canon_fingerprint()` 读的是编译产物 cards.json，所以改了 `book/kb/canon/`
  而没跑 `kb sync` 时，一切指纹都不变、一切闸门都不响，运行时静默用旧设定。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.content.extract import canon_source_fingerprint, propose_glossary_candidates
from novel_ledger_core.content.gates import _delta_issues, _QUOTE_EVIDENCE_KINDS
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    _canon_source_drift,
    _glossary_coverage,
    calibrate_book,
    status,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json
from novel_ledger_core.ledger.ledger import (
    CONDITIONS_PER_ENTITY,
    EMPTY_SNAPSHOT,
    apply_event,
    load_snapshot,
    verify_ledger,
    _evict_conditions,
    select_conditions,
    select_debts,
    select_hooks,
    select_items,
    select_relations,
)

PLAN = {
    "title": "",
    "protagonist": "主角",
    "chapters": [
        {
            "chapter": 1,
            "volume": 1,
            "location": "市集",
            "present": ["主角", "掌柜"],
            "beats": [
                {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
                {"id": "b2", "required": True, "text": "凭据仍被扣", "must": "凭据"},
            ],
        }
    ],
}


def _project(tmp_path: Path, **kwargs) -> Path:
    proj = tmp_path / "proj"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
    init_project(proj, plan_path=plan_path, protagonist="主角", **kwargs)
    return proj


# --------------------------------------------------------------------------- #
# ① 不可逆代价：全量注入、永不驱逐
# --------------------------------------------------------------------------- #


def test_irreversible_conditions_survive_eviction():
    """断肢/毁容/资格吊销/破产清零的定义就是"它不会自己好"——不能被条数上限挤掉。"""
    conditions = [
        {"who": "主角", "kind": "伤势", "text": f"旧伤{i}", "irreversible": True}
        for i in range(CONDITIONS_PER_ENTITY + 5)
    ]
    conditions += [
        {"who": "主角", "kind": "体力", "text": f"疲惫{i}"} for i in range(CONDITIONS_PER_ENTITY)
    ]
    kept = _evict_conditions(conditions)
    kept_texts = {c["text"] for c in kept}
    for i in range(CONDITIONS_PER_ENTITY + 5):
        assert f"旧伤{i}" in kept_texts, "不可逆代价被驱逐了"


def test_select_conditions_never_truncates_irreversible_but_counts_the_rest():
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap,
        {
            "chapter": 1,
            "state_delta": {
                "conditions": [
                    {"who": "主角", "kind": "伤势", "text": "右臂齐肩斩断", "irreversible": True},
                    {"who": "主角", "kind": "体力", "text": "力竭"},
                    {"who": "主角", "kind": "体力", "text": "饥饿"},
                ]
            },
        },
    )
    shown, omitted = select_conditions(None, names=["主角"], snapshot=snap, cap=2)
    assert [c["text"] for c in shown] == ["右臂齐肩斩断", "力竭", "饥饿"]
    assert omitted == 0


def test_resolved_conditions_drop_out_of_the_slice():
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap,
        {"chapter": 1, "state_delta": {"conditions": [{"who": "主角", "kind": "伤势", "text": "肋骨断了"}]}},
    )
    assert select_conditions(None, names=["主角"], snapshot=snap)[0]
    snap = apply_event(
        snap,
        {
            "chapter": 9,
            "state_delta": {
                "conditions": [
                    {"who": "主角", "kind": "伤势", "text": "肋骨断了", "status": "resolved"}
                ]
            },
        },
    )
    assert select_conditions(None, names=["主角"], snapshot=snap)[0] == []


def test_condition_shape_and_quote_are_validated():
    pack = {
        "present_cards": [{"name": "主角", "is_first_appearance": False}],
        "now_card": {"name": "主角"},
    }
    issues = _delta_issues({"conditions": [{"kind": "伤势", "text": "断臂"}]}, pack)
    assert any(i["code"] == "condition_missing_who" for i in issues)
    issues = _delta_issues({"conditions": [{"who": "主角"}]}, pack)
    assert any(i["code"] == "condition_missing_text" for i in issues)
    issues = _delta_issues({"conditions": [{"who": "外人", "text": "断臂"}]}, pack)
    assert any(i["code"] == "unnamed_in_pack" for i in issues)
    assert "conditions" in _QUOTE_EVIDENCE_KINDS


def test_conditions_participate_in_ledger_verification():
    """新维度必须进 verify 的比对面，否则快照被改也看不出来。"""
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap, {"chapter": 1, "state_delta": {"conditions": [{"who": "主角", "kind": "修为", "text": "练气三层"}]}}
    )
    assert snap["conditions"]


# --------------------------------------------------------------------------- #
# ① 端到端：不可逆代价必须出现在后续每一章的 pack 里
# --------------------------------------------------------------------------- #

_CH1_PROSE = (
    "山道上刀光一闪，韩立右臂齐肩而断。掌柜被绳索一缠，当场擒住。"
    "两人隔着三丈对峙，谁也没有退让，血顺着石阶往下流。"
)
_TWO_CHAPTER_PLAN = {
    "title": "",
    "protagonist": "韩立",
    "chapters": [
        {
            "chapter": 1,
            "volume": 1,
            "location": "山道",
            "present": ["韩立", "掌柜"],
            "beats": [
                {"id": "b1", "required": True, "text": "韩立断臂，齐肩而断", "must": "齐肩"},
                {"id": "b2", "required": True, "text": "掌柜被擒，当场擒住", "must": "擒住"},
            ],
        },
        {
            "chapter": 2,
            "volume": 1,
            "location": "洞府",
            "present": ["韩立"],
            "beats": [
                {"id": "b1", "required": True, "text": "韩立疗伤", "must": "疗伤"},
                {"id": "b2", "required": True, "text": "清点所得", "must": "清点"},
            ],
        },
    ],
}


def _commit_chapter_one_with_a_severed_arm(tmp_path: Path) -> BookStore:
    from tests.decoupled_helpers import advance_to_assembly

    from novel_ledger_core.control.pipeline import ack_read, chapter_next, submit_output

    proj = tmp_path / "proj"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_TWO_CHAPTER_PLAN, ensure_ascii=False), encoding="utf-8")
    init_project(proj, plan_path=plan_path, protagonist="韩立")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["word_band_enforce"] = False
    store.save_config(cfg)

    advance_to_assembly(store, prose=_CH1_PROSE, chapter=1)
    pack = read_json(store.assemble_pack_path(1))
    submitted = submit_output(
        store,
        {
            "prose": _CH1_PROSE,
            "l1_summary": "韩立断臂，掌柜被擒。",
            "state_delta": {
                "moves": [],
                "facts": [{"who": "韩立", "text": "右臂已断。"}],
                "debts": [],
                "hooks": [],
                "relations": [],
                "named": ["韩立", "掌柜"],
                "new_names": [],
                "deaths": [],
                "conditions": [
                    {
                        "who": "韩立",
                        "kind": "伤势",
                        "text": "右臂齐肩斩断",
                        "irreversible": True,
                        "quote": "韩立右臂齐肩而断",
                    }
                ],
            },
            "memory": {"voice_concepts": []},
            "pack_hash": pack["pack_hash"],
            "beats_hit": ["b1", "b2"],
        },
    )
    assert submitted["verdict"] == "accepted", submitted
    assert chapter_next(store)["action"] == "ack"
    ack_read(
        store,
        quotes=[
            "山道上刀光一闪，韩立右臂齐肩而断",
            "掌柜被绳索一缠，当场擒住",
            "两人隔着三丈对峙，谁也没有退让",
        ],
        verdict="pass",
    )
    return store


def test_irreversible_cost_reaches_every_later_chapter_pack(tmp_path: Path):
    """断掉的手臂必须出现在后续每一章的工作包里，并附硬约束——

    这正是"单章看不出来、长篇必崩"的那类漂移的机器化对策。
    """
    store = _commit_chapter_one_with_a_severed_arm(tmp_path)
    from novel_ledger_core.control.pipeline import chapter_next

    assert chapter_next(store)["action"] == "draft"
    pack = read_json(store.assemble_pack_path(2))
    texts = [c["text"] for c in pack.get("conditions") or []]
    assert "右臂齐肩斩断" in texts
    assert pack.get("conditions_directive"), "缺了硬约束提示，写者仍可能让断手去握剑"


def test_irreversible_cost_shows_up_in_the_book_audit_inventory(tmp_path: Path):
    store = _commit_chapter_one_with_a_severed_arm(tmp_path)
    from novel_ledger_core.control.pipeline import audit_book

    inventory = audit_book(store)["irreversible_conditions"]
    assert inventory["count"] == 1
    assert inventory["items"][0]["text"] == "右臂齐肩斩断"


def test_pack_assembly_reports_irreversible_overflow_and_accepts_explicit_cap(tmp_path: Path):
    from novel_ledger_core.content.pack import assemble_pack

    store = BookStore(_project(tmp_path))
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot["conditions"] = [
        {"who": "主角", "kind": "伤势", "text": f"永久代价{i}", "irreversible": True}
        for i in range(33)
    ]
    atomic_json(store.snapshot_path, snapshot)

    with pytest.raises(LedgerError) as raised:
        assemble_pack(store, 1)
    assert raised.value.code == "irreversible_conditions_overflow"

    cfg = store.load_config()
    cfg["pack_caps"]["irreversible_conditions"] = 33
    store.save_config(cfg)
    pack = assemble_pack(store, 1)
    assert len(pack["conditions"]) == 33


# --------------------------------------------------------------------------- #
# ② 切片省略计数：有界视野必须自报边界
# --------------------------------------------------------------------------- #


def test_every_ledger_selector_reports_how_many_it_dropped():
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap,
        {
            "chapter": 1,
            "state_delta": {
                "named": ["主角"] + [f"角色{i}" for i in range(20)],
                "debts": [
                    {"id": f"d{i}", "who": "主角", "text": "欠", "status": "open", "location": "市集"}
                    for i in range(20)
                ],
                "hooks": [{"id": f"h{i}", "text": "钩", "status": "open", "due": 99} for i in range(20)],
                "relations": [
                    {"who": "主角", "target": f"角色{i}", "kind": "盟友", "status": "open"}
                    for i in range(20)
                ],
                "items": [
                    {"id": f"it{i}", "name": f"物{i}", "holder": "主角", "status": "held"}
                    for i in range(20)
                ],
            },
        },
    )
    debts, debts_dropped = select_debts(
        None, location="市集", present=["主角"], cap=8, snapshot=snap
    )
    hooks, hooks_dropped = select_hooks(None, chapter=99, cap=5, snapshot=snap)
    relations, relations_dropped = select_relations(None, names=["主角"], cap=6, snapshot=snap)
    items, items_dropped = select_items(None, names=["主角"], cap=8, snapshot=snap)

    assert (len(debts), debts_dropped) == (8, 12)
    assert (len(hooks), hooks_dropped) == (5, 15)
    assert (len(relations), relations_dropped) == (6, 14)
    assert (len(items), items_dropped) == (8, 12)


def test_nothing_is_reported_as_dropped_when_the_view_is_complete():
    """零省略时不许报数，否则告警会变成噪音。"""
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap,
        {"chapter": 1, "state_delta": {"items": [{"id": "a", "name": "甲", "holder": "主角"}]}},
    )
    _, dropped = select_items(None, names=["主角"], cap=8, snapshot=snap)
    assert dropped == 0


# --------------------------------------------------------------------------- #
# ④ canon 源 ↔ cards.json 同步守卫
# --------------------------------------------------------------------------- #


def test_canon_source_fingerprint_changes_with_the_source(tmp_path: Path):
    canon = tmp_path / "canon"
    canon.mkdir()
    card = canon / "设定.md"
    card.write_text("# 设定\n\n一斤米三文。\n", encoding="utf-8")
    before = canon_source_fingerprint(canon)
    assert before.startswith("sha256:")
    card.write_text("# 设定\n\n一斤米三十文。\n", encoding="utf-8")
    assert canon_source_fingerprint(canon) != before


def test_editing_canon_without_sync_is_detected(tmp_path: Path):
    """这是 canon_drift 的盲区：cards.json 没变 → 指纹不变 → 一切闸门静默。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    store.canon_dir.mkdir(parents=True, exist_ok=True)
    card = store.canon_dir / "物价.md"
    card.write_text("# 物价\n\n【硬】一斤米三文。\n", encoding="utf-8")
    # 模拟一次正常的 kb sync
    from novel_ledger_core.content.extract import extract_to_file

    extract_to_file(store.canon_dir, store.kb_path, excerpt_max=4000)
    assert _canon_source_drift(store)["changed"] is False

    card.write_text("# 物价\n\n【硬】一斤米三十文。\n", encoding="utf-8")
    drifted = _canon_source_drift(store)
    assert drifted["changed"] is True
    assert "kb sync" in drifted["hint"]
    # 关键对照：老的 canon_drift 对这次改动**完全无感**（它读的是没变的 cards.json）
    from novel_ledger_core.control.pipeline import _canon_drift

    assert _canon_drift(store)["available"] is False


def test_kb_sync_clears_the_canon_source_drift(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    store.canon_dir.mkdir(parents=True, exist_ok=True)
    card = store.canon_dir / "物价.md"
    card.write_text("# 物价\n\n【硬】一斤米三文。\n", encoding="utf-8")
    from novel_ledger_core.content.extract import extract_to_file

    extract_to_file(store.canon_dir, store.kb_path, excerpt_max=4000)
    card.write_text("# 物价\n\n【硬】一斤米三十文。\n", encoding="utf-8")
    assert _canon_source_drift(store)["changed"] is True

    extract_to_file(store.canon_dir, store.kb_path, excerpt_max=4000)
    assert _canon_source_drift(store)["changed"] is False


def test_save_kb_does_not_erase_the_recorded_fingerprint(tmp_path: Path):
    """init 只做搬运，不能顺手抹掉同步凭证（否则守卫永远报 available=False）。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    store.canon_dir.mkdir(parents=True, exist_ok=True)
    (store.canon_dir / "物价.md").write_text("# 物价\n\n【硬】一斤米三文。\n", encoding="utf-8")
    from novel_ledger_core.content.extract import extract_to_file

    extract_to_file(store.canon_dir, store.kb_path, excerpt_max=4000)
    recorded = read_json(store.kb_path)["source_fingerprint"]

    store.save_kb(store.load_kb())
    assert read_json(store.kb_path).get("source_fingerprint") == recorded


# --------------------------------------------------------------------------- #
# ④ glossary 产出方：把空白页变成候选清单，交总编裁决
# --------------------------------------------------------------------------- #


def test_glossary_coverage_advisory_fires_when_canon_declares_bans(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    store.canon_dir.mkdir(parents=True, exist_ok=True)
    (store.canon_dir / "硬禁.md").write_text(
        "# 术语\n\n## 硬禁则\n\n- 勿写「验阵抽两成」，应作「验阵抽一成」。\n- 勿把旧称说成官名。\n",
        encoding="utf-8",
    )
    from novel_ledger_core.content.extract import extract_to_file

    extract_to_file(store.canon_dir, store.kb_path, excerpt_max=4000)

    coverage = _glossary_coverage(store)
    assert coverage["canon_ban_bullets"] >= 2
    assert coverage["glossary_entries"] == 0
    assert "空转" in coverage["advisory"]

    # 填上一条之后就不再催
    cfg = store.load_config()
    cfg["glossary"] = {"验阵抽两成": "验阵抽一成"}
    store.save_config(cfg)
    assert "advisory" not in _glossary_coverage(store)


def test_calibrate_offers_glossary_candidates_to_the_managing_editor(tmp_path: Path):
    """总编不必凭空想：`book calibrate` 把正典硬禁里点名过的具体词串摆出来。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    store.canon_dir.mkdir(parents=True, exist_ok=True)
    (store.canon_dir / "硬禁.md").write_text(
        "# 术语\n\n## 硬禁则\n\n- 勿写「验阵抽两成」，应作「验阵抽一成」。\n",
        encoding="utf-8",
    )
    from novel_ledger_core.content.extract import extract_to_file

    extract_to_file(store.canon_dir, store.kb_path, excerpt_max=4000)

    payload = calibrate_book(store)
    assert payload["current_glossary"] == []
    assert "验阵抽两成" in payload["glossary_candidates"]
    assert payload["glossary_ban_bullets"]
    # 只做建议，绝不自动写入
    assert store.load_config().get("glossary") == {}


def test_propose_glossary_candidates_is_read_only_on_claims():
    cards = [
        {
            "id": "硬禁",
            "body": "## 硬禁则\n\n- 勿写「抄本等于完整真传」。\n- 勿写渡劫返老。\n",
        }
    ]
    out = propose_glossary_candidates(cards)
    assert "抄本" in out["candidates"] or "抄本等于完整真传" in out["candidates"]
    assert len(out["ban_bullets"]) == 2
    assert out["hint"]


def test_conditions_survive_unrelated_later_events():
    """conditions 必须跨事件结转：不带 conditions 的后续事件不得清空历史角色状态。

    缺陷形态：apply_event 重建快照时漏带 conditions 键——每个事件应用后只剩当章
    新增条件，历史全部清零；增量提交与重放同病，单章测试永远看不见。
    """
    base = {"chapter": 0, "entities": {}, "debts": [], "hooks": [], "relations": [],
            "occupancy": {}, "items": [], "conditions": []}
    e1 = {"chapter": 1, "state_delta": {"conditions": [
        {"who": "主角", "kind": "伤势", "text": "右臂齐肩斩断", "irreversible": True},
        {"who": "主角", "kind": "体力", "text": "连日赶路，体力透支"},
    ]}}
    e2 = {"chapter": 2, "state_delta": {"moves": [{"who": "主角", "to": "码头"}]}}
    snap = apply_event(apply_event(base, e1), e2)
    conds = {(c["who"], c["kind"], c["text"]) for c in snap["conditions"]}
    assert ("主角", "伤势", "右臂齐肩斩断") in conds
    assert ("主角", "体力", "连日赶路，体力透支") in conds
    irrev = [c for c in snap["conditions"] if c.get("irreversible")]
    assert irrev and irrev[0]["text"] == "右臂齐肩斩断"
