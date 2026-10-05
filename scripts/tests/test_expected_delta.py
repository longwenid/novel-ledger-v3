"""章拍 effects → expected_delta 的回归测试。

锁定不变量：
- beats[].effects 装配时展平成 pack.expected_delta；执笔收到完整句式，剧情审校/组装收到结构化账本；
- 提交 state_delta 必须原样回填计划 effects（expected_delta_missing → 回草稿）；
- 计划语义条目命中后必须带正文 quote（expected_delta_quote_missing → 只回组装）。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import chapter_next, check_submit_output, submit_output
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.content.gates import _expected_delta_issues
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, read_json
from tests.decoupled_helpers import advance_to_assembly, make_chapter, make_plan


def _plan() -> dict:
    return make_plan(
        [
            make_chapter(
                1,
                beats=[
                    {
                        "id": "b1",
                        "required": True,
                        "text": "主角当面拒收改期，掌柜扣着凭据，答应明早答复",
                        "must": "凭据",
                        "effects": {
                            "moves": [{"who": "主角", "to": "市集"}],
                            "facts": [
                                {
                                    "who": "主角",
                                    "text": "主角在柜台当面拒绝改期",
                                    "pin": False,
                                }
                            ],
                            "debts": [
                                {
                                    "id": "d1",
                                    "who": "掌柜",
                                    "text": "欠主角一个明早的答复",
                                    "status": "open",
                                    "due": 2,
                                }
                            ],
                            "hooks": [
                                {
                                    "id": "h1",
                                    "text": "掌柜明早的答复是否兑现",
                                    "due": 2,
                                    "status": "open",
                                }
                            ],
                            "relations": [
                                {
                                    "who": "主角",
                                    "target": "掌柜",
                                    "kind": "旧交",
                                    "status": "open",
                                }
                            ],
                        },
                    }
                ],
            )
        ],
        volume_spine="第一卷：主角要回凭据。",
    )


def _prose() -> str:
    return "主角站在市集，对掌柜说拒收改期。凭据还压在柜台。掌柜说明早答复。"


def _make_project(tmp_path: Path) -> Path:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return proj


def _output(store: BookStore, *, prose: str, include_fact: bool = True, fact_quote: bool = True) -> dict:
    pack = read_json(store.current_pack_path)
    facts = [
        {
            "who": "主角",
            "text": "主角在柜台当面拒绝改期",
            "pin": False,
        }
    ]
    if fact_quote:
        facts[0]["quote"] = "对掌柜说拒收改期"
    state_delta = {
        "moves": [{"who": "主角", "to": "市集"}],
        "debts": [
            {
                "id": "d1",
                "who": "掌柜",
                "text": "欠主角一个明早的答复",
                "status": "open",
                "due": 2,
                "quote": "掌柜说明早答复",
            }
        ],
        "hooks": [
            {
                "id": "h1",
                "text": "掌柜明早的答复是否兑现",
                "due": 2,
                "status": "open",
                "quote": "掌柜说明早答复",
            }
        ],
        "relations": [
            {
                "who": "主角",
                "target": "掌柜",
                "kind": "旧交",
                "status": "open",
                "quote": "对掌柜说拒收改期",
            }
        ],
        "named": ["主角", "掌柜"],
        "new_names": [],
        "deaths": [],
    }
    if include_fact:
        state_delta["facts"] = facts
    else:
        state_delta["facts"] = []
    return {
        "prose": prose,
        "l1_summary": "主角拒收改期，凭据仍被扣。",
        "state_delta": state_delta,
        "memory": {},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1"],
    }


def test_expected_delta_reaches_draft_and_assemble_in_stage_appropriate_shapes(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    canon = read_json(store.current_pack_path)
    assert canon["expected_delta"]["facts"] == [
        {"who": "主角", "text": "主角在柜台当面拒绝改期", "pin": False}
    ]
    assert canon["expected_delta"]["debts"][0]["id"] == "d1"

    draft = read_json(store.draft_pack_path(1))
    assert "expected_delta" not in draft
    assert "计划结果（必须在正文中实际发生）" in draft["writing_brief"]
    assert "主角在柜台当面拒绝改期" in draft["writing_brief"]
    assemble = read_json(store.assemble_pack_path(1))
    assert assemble["expected_delta"] == canon["expected_delta"]


def test_planned_effects_accepted_with_quotes(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    accepted = submit_output(store, _output(store, prose=prose))
    assert accepted["verdict"] == "accepted"


def test_missing_planned_fact_rewrites_from_draft(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    rewritten = submit_output(store, _output(store, prose=prose, include_fact=False))
    assert rewritten["verdict"] == "rewrite"
    assert rewritten["phase"] == "await_draft"
    codes = {v["code"] for v in rewritten["violations"]}
    assert "expected_delta_missing" in codes


def test_planned_fact_without_quote_returns_to_assembly(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    rewritten = submit_output(
        store,
        _output(store, prose=prose, include_fact=True, fact_quote=False),
    )
    assert rewritten["verdict"] == "fix_assembly"
    assert rewritten["phase"] == "await_assembly"
    codes = {v["code"] for v in rewritten["violations"]}
    assert "expected_delta_quote_missing" in codes


def test_check_submit_is_read_only_and_routes_content_fix_to_draft(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    head_before = read_json(store.head_path)

    checked = check_submit_output(
        store,
        _output(store, prose=prose, include_fact=False),
    )

    assert checked["verdict"] == "fix_draft"
    assert checked["ready"] is False
    assert "expected_delta_missing" in {v["code"] for v in checked["violations"]}
    assert read_json(store.head_path) == head_before
    assert store.draft_text_path(1).exists()
    assert store.polished_text_path(1).exists()


def test_check_submit_rejects_wrong_planned_debt_status(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    output = _output(store, prose=prose)
    output["state_delta"]["debts"][0]["status"] = "paid"

    checked = check_submit_output(store, output)

    assert checked["verdict"] == "fix_draft"
    assert any(
        issue["code"] == "expected_delta_missing" and issue["kind"] == "debts"
        for issue in checked["violations"]
    )


def test_check_submit_ready_then_formal_submit_accepts(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    output = _output(store, prose=prose)

    checked = check_submit_output(store, output)
    assert checked["verdict"] == "ready"
    assert checked["ready"] is True
    assert read_json(store.head_path)["phase"] == "await_assembly"

    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"


def test_check_submit_diagnoses_during_blocked_phase(tmp_path: Path):
    """返工耗尽停线后，只读诊断仍可用：wrong_phase 不再拦 blocked（实战 ch21 排障需要）。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    head = read_json(store.head_path)
    head["phase"] = "blocked"
    head["blocked"] = {"reason": "rewrite_limit"}
    atomic_json(store.head_path, head)

    checked = check_submit_output(store, _output(store, prose=prose))

    assert checked["verdict"] != "not_ready"
    assert "wrong_phase" not in {v["code"] for v in checked.get("violations") or []}
    assert read_json(store.head_path) == head


def test_assembly_shape_errors_carry_minimal_examples(tmp_path: Path):
    """高频组装形状错误的 hint 必须带最小合法示例（memory 字符串/beats_hit 对象曾每章复发）。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    output = _output(store, prose=prose)
    output["memory"] = "笔记字符串"

    checked = check_submit_output(store, output)

    by_code = {v["code"]: v for v in checked.get("violations") or []}
    assert "memory_not_object" in by_code
    assert "voice_concepts" in by_code["memory_not_object"].get("hint", "")


def test_check_submit_cli_is_read_only(tmp_path: Path, capsys):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    output_path = tmp_path / "submit.json"
    output_path.write_text(
        json.dumps(_output(store, prose=prose), ensure_ascii=False),
        encoding="utf-8",
    )
    head_before = read_json(store.head_path)

    assert main(
        ["chapter", "check-submit", "--project", str(proj), "--output", str(output_path)]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["action"] == "submit_check"
    assert payload["verdict"] == "ready"
    assert read_json(store.head_path) == head_before


# --- conditions 分支回归 -------------------------------------------------------
# 缺 `_find_actual` 的 conditions 分支时，章拍只要在 effects 里声明 conditions，
# 组装端无论怎么写都会被判 expected_delta_missing（守卫认不出兑现，不是写者没兑现）。
# 下面两条同时锁住两个方向：兑现的必须放过，真没兑现的仍必须拦住。

def _condition_plan() -> dict:
    return {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：主角扛伤赶路。",
        "chapters": [
            {
                "chapter": 1,
                "location": "石桥",
                "present": ["主角"],
                "beats": [
                    {
                        "id": "b1",
                        "required": True,
                        "text": "主角在桥上滑倒，左肩锁骨断了，他缠上布继续赶路",
                        "must": "锁骨",
                        "effects": {
                            "conditions": [
                                {
                                    "who": "主角",
                                    "kind": "伤势",
                                    "text": "左肩锁骨断裂，短期不能负重",
                                    "irreversible": False,
                                    "status": "active",
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }


_CONDITION_PROSE = "主角在石桥上滑了一跤，左肩锁骨断了。他用布把肩膀缠住，忍痛继续赶路。"


def _make_condition_project(tmp_path: Path) -> Path:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_condition_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return proj


def _condition_output(store: BookStore, *, include_condition: bool = True) -> dict:
    pack = read_json(store.current_pack_path)
    conditions = [
        {
            "who": "主角",
            "kind": "伤势",
            "text": "左肩锁骨断裂，短期不能负重",
            "irreversible": False,
            "status": "active",
            "quote": "左肩锁骨断了。他用布把肩膀缠住",
        }
    ]
    return {
        "prose": _CONDITION_PROSE,
        "l1_summary": "主角断锁骨赶路。",
        "state_delta": {
            "named": ["主角"],
            "new_names": [],
            "deaths": [],
            "conditions": conditions if include_condition else [],
        },
        "memory": {},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1"],
    }


def test_planned_condition_is_matched_not_reported_missing(tmp_path: Path):
    proj = _make_condition_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    assemble = read_json(store.assemble_pack_path(1))
    assert assemble["expected_delta"]["conditions"][0]["text"] == "左肩锁骨断裂，短期不能负重"

    advance_to_assembly(store, prose=_CONDITION_PROSE, chapter=1)
    accepted = submit_output(store, _condition_output(store))
    assert accepted["verdict"] == "accepted"


def test_planned_condition_still_required_when_absent(tmp_path: Path):
    proj = _make_condition_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    advance_to_assembly(store, prose=_CONDITION_PROSE, chapter=1)
    rewritten = submit_output(store, _condition_output(store, include_condition=False))
    assert rewritten["verdict"] == "rewrite"
    codes = {v["code"] for v in rewritten["violations"]}
    assert "expected_delta_missing" in codes


def test_matching_stable_key_cannot_hide_changed_planned_state():
    cases = [
        ("facts", {"who": "主角", "text": "知道暗号", "pin": True}, {"pin": False}),
        ("debts", {"id": "d1", "who": "掌柜", "text": "明日还钱", "status": "open", "due": 2}, {"status": "paid"}),
        ("hooks", {"id": "h1", "text": "明日揭晓", "status": "open", "due": 2}, {"due": 9}),
        ("relations", {"who": "主角", "target": "掌柜", "kind": "盟友", "status": "open"}, {"status": "closed"}),
        ("items", {"id": "seal", "name": "铜印", "holder": "主角", "quantity": 1, "status": "held"}, {"holder": "掌柜"}),
        ("items", {"id": "seal", "quantity": 1, "status": "held"}, {"quantity": True}),
        ("conditions", {"who": "主角", "kind": "伤势", "text": "锁骨断裂", "value": 2, "unit": "级", "irreversible": True, "status": "active"}, {"irreversible": False}),
        ("conditions", {"who": "主角", "text": "锁骨断裂", "status": "active"}, {"status": "resolved"}),
    ]
    for kind, planned, changed in cases:
        actual = {**planned, **changed, "quote": "正文中逐字摘录的证据"}
        issues = _expected_delta_issues({kind: [actual]}, {"expected_delta": {kind: [planned]}})
        assert [issue["code"] for issue in issues] == ["expected_delta_missing"], (kind, issues)


def test_only_declared_fields_are_required_and_quote_is_extra_evidence():
    planned = {"id": "h1", "status": "open"}
    actual = {"id": "h1", "status": "open", "due": 2, "text": "明日揭晓", "quote": "正文中逐字摘录的证据"}
    assert _expected_delta_issues({"hooks": [actual]}, {"expected_delta": {"hooks": [planned]}}) == []
    condition = {"who": "主角", "kind": "伤势", "text": "锁骨断裂", "quote": "正文中逐字摘录的证据"}
    planned_condition = {"who": "主角", "kind": "", "text": "锁骨断裂"}
    assert _expected_delta_issues(
        {"conditions": [condition]}, {"expected_delta": {"conditions": [planned_condition]}}
    ) == []


def test_second_entry_can_satisfy_plan_when_first_shares_id_but_not_state():
    planned = {"id": "h1", "status": "paid"}
    actual = [{"id": "h1", "status": "open"}, {"id": "h1", "status": "paid", "quote": "正文中逐字摘录的证据"}]
    assert _expected_delta_issues({"hooks": actual}, {"expected_delta": {"hooks": [planned]}}) == []


def test_item_plan_by_name_accepts_added_id_with_same_name():
    planned = {"name": "铜印", "holder": "主角", "status": "held"}
    actual = {"id": "seal", "name": "铜印", "holder": "主角", "status": "held", "quote": "主角接过那枚铜印"}
    assert _expected_delta_issues({"items": [actual]}, {"expected_delta": {"items": [planned]}}) == []


def test_item_plan_with_explicit_id_rejects_different_id_despite_same_name():
    planned = {"id": "seal-a", "name": "铜印", "holder": "主角"}
    actual = {"id": "seal-b", "name": "铜印", "holder": "主角", "quote": "主角接过那枚铜印"}
    issues = _expected_delta_issues({"items": [actual]}, {"expected_delta": {"items": [planned]}})
    assert [issue["code"] for issue in issues] == ["expected_delta_missing"]


def test_near_miss_fact_routes_to_assembly_with_field_diff(tmp_path: Path):
    """守卫认不出兑现≠写者没兑现：同 who 近失条目判 shape_mismatch（回组装、带 diff）。

    实测教训（ch252）：facts 文本略异被报 expected_delta_missing 回草稿，宿主为拨回相位
    派了一个什么都不改的 draft worker，还翻了 skill 源码手写 diff——近失对照进回执后，
    组装侧照抄对齐即可。
    """
    planned = {"who": "主角", "text": "主角在柜台当面拒绝改期", "pin": False}
    actual = [{"who": "主角", "text": "主角在柜台当面拒了改期", "pin": False, "quote": "对掌柜说拒收改期"}]
    issues = _expected_delta_issues({"facts": actual}, {"expected_delta": {"facts": [planned]}})
    assert [issue["code"] for issue in issues] == ["expected_delta_shape_mismatch"]
    assert issues[0]["diffs"][0]["field"] == "text"
    assert "拒" in issues[0]["diffs"][0]["expected"]
    assert "assembly" in issues[0]["hint"] or "组装" in issues[0]["hint"]


def test_knowledge_text_claim_shape_mismatch_routes_to_assembly():
    planned = {"who": "主角", "topic_id": "t1", "text": "凭据从未归还"}
    actual = [{"who": "主角", "topic_id": "t1", "claim": "凭据从未归还", "stance": "knows", "source": "亲见", "quote": "证据句"}]
    issues = _expected_delta_issues({"knowledge": actual}, {"expected_delta": {"knowledge": [planned]}})
    assert [issue["code"] for issue in issues] == ["expected_delta_shape_mismatch"]
    assert issues[0]["diffs"][0]["field"] == "text"
    assert issues[0]["diffs"][0]["actual"] == "<missing>"


def test_truly_absent_fact_still_rewrites_from_draft(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    rewritten = submit_output(store, _output(store, prose=prose, include_fact=False))
    assert rewritten["verdict"] == "rewrite"
    assert rewritten["phase"] == "await_draft"
    codes = {v["code"] for v in rewritten["violations"]}
    assert "expected_delta_missing" in codes
    # rewrite 回执带恢复路由：单点问题给零模型宿主路径，不再让宿主猜。
    recovery = rewritten["recovery"]
    assert recovery["route"] == "one_point_prose"
    assert "rework-patch" in recovery["steps"][0]
    assert "--target" in recovery["steps"][0]
    assert any("chapter next" in step for step in recovery["steps"])


def test_unnamed_in_pack_hint_carries_remedy(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    output = _output(store, prose=prose)
    output["state_delta"]["named"] = ["主角", "掌柜", "曹母"]
    rejected = submit_output(store, output)
    assert rejected["verdict"] == "fix_assembly"
    assert rejected["phase"] == "await_assembly"
    unnamed = [v for v in rejected["violations"] if v["code"] == "unnamed_in_pack"]
    assert unnamed and "new_names" in unnamed[0]["hint"]


def test_expansion_diff_and_selection_mismatch_carry_details():
    from novel_ledger_core.infra.planning import expansion_diff, expansion_selection, rows_hash
    from novel_ledger_core.infra.util import LedgerError

    rows = [{"chapter": 4, "goal": "核对登记本", "conflict": "卷宗不外借", "outcome": "找到改登记的笔",
             "tags": ["卷宗复核"], "settles": ["h4"]}]
    chapters = [{
        "chapter": 4, "goal": "核对登记本", "conflict": "卷宗外借受阻", "outcome": "找到改登记的笔",
        "tags": ["卷宗复核"],
        "beats": [{"id": "b1", "effects": {"hooks": []}}],
    }]
    diffs = expansion_diff(rows, chapters)
    kinds = {d["kind"] for d in diffs}
    assert "field_mismatch" in kinds and "settles_mismatch" in kinds
    settles = next(d for d in diffs if d["kind"] == "settles_mismatch")
    assert settles["unpaid"] == ["h4"] and settles["extra_paid"] == []

    event = {
        "type": "governance", "action": "plan.batch_select",
        "batch_from": 4, "batch_to": 4, "selected_id": "A",
        "selected_chapters": rows, "candidate_hash": rows_hash(rows), "hash": "x",
    }
    try:
        expansion_selection([event], chapters, required=True)
        raise AssertionError("expected plan_batch_selection_mismatch")
    except LedgerError as exc:
        assert exc.code == "plan_batch_selection_mismatch"
        assert exc.details and exc.details["diff"]
        assert "align" in exc.details["hint"]


def test_shape_mismatch_routes_to_assembly_end_to_end(tmp_path: Path):
    """expected_delta_shape_mismatch ∈ _ASSEMBLY_ONLY_ISSUES：相位不动、不耗返工预算。"""
    from novel_ledger_core.control.pipeline import _ASSEMBLY_ONLY_ISSUES

    assert "expected_delta_shape_mismatch" in _ASSEMBLY_ONLY_ISSUES
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    prose = _prose()
    advance_to_assembly(store, prose=prose, chapter=1)
    output = _output(store, prose=prose)
    output["state_delta"]["facts"][0]["text"] = "主角在柜台当面拒了改期"
    rejected = submit_output(store, output)
    assert rejected["verdict"] == "fix_assembly"
    assert rejected["phase"] == "await_assembly"
    head = read_json(store.head_path)
    assert head["rewrite_count"] == 0
    shape = [v for v in rejected["violations"] if v["code"] == "expected_delta_shape_mismatch"]
    assert shape and shape[0]["diffs"]
