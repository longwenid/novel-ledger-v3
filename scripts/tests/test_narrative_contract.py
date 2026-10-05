"""Author-approved narrative inputs survive empty planning/review sessions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.content.narrative_contract import (
    NARRATIVE_CONTRACT_SCHEMA,
    amend_narrative_contract,
    narrative_contract_view,
    read_narrative_source,
)
from novel_ledger_core.content.extract import canon_source_fingerprint, extract_cards
from novel_ledger_core.control.hatch import hatch_project, validate_hatch_manifest
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import _write_plan_extend_brief, validate_plan
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, atomic_text, read_json
from scripts.tests.test_autopilot import _store
from scripts.tests.test_choice_hatch import guided_manifest, add_volume_outline_fixture


def _manifest() -> dict:
    return guided_manifest.__wrapped__()


def test_real_hatch_retains_author_markers_in_empty_planning_input(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["main_plotline"]["core_quest"] += "：主线唯一标记甲"
    manifest["main_plotline"]["endgame_condition"] += "：终局唯一标记乙"
    manifest["step1_hook"]["selling_points"] += "：读者承诺唯一标记丙"
    manifest["step2_protagonist"]["desire_deep"] += "：深层欲望唯一标记丁"
    manifest["hard_constraints"].append("作者红线唯一标记戊：不以失忆抹去选择后果")
    manifest["step3_world"]["iron_rules"].append("世界铁律唯一标记己：能力不能无代价回滚")
    manifest["relationship_structure"]["relationships"][0]["independent_goal"] += "：关系唯一标记庚"
    manifest["theme"] = {"question": "主题唯一标记辛：自由选择与共同责任如何共存"}
    project = tmp_path / "hatched"
    hatch_project(project, manifest)
    store = BookStore(project)
    contract = store.load_plan()["narrative_contract"]
    assert contract["schema"] == NARRATIVE_CONTRACT_SCHEMA
    assert contract["hard_constraints"] == manifest["hard_constraints"]
    assert contract["main_arc"] == manifest["main_plotline"]
    brief = _write_plan_extend_brief(store, {"chapter": 3, "suggest_from": 4})
    context = read_json(Path(brief["path"]))["planning_context"]
    view = context["narrative_contract"]
    assert view["mode"] == "structured"
    assert view["contract"] == contract
    assert view["omitted"] == {"source_chars": 0, "canon_cards": 0}
    encoded = json.dumps(view, ensure_ascii=False)
    for marker in ("主线唯一标记甲", "终局唯一标记乙", "读者承诺唯一标记丙", "深层欲望唯一标记丁", "作者红线唯一标记戊", "世界铁律唯一标记己", "关系唯一标记庚", "主题唯一标记辛"):
        assert marker in encoded
    assert view == narrative_contract_view(store)
    assert view["sources"][0]["field"] == "narrative_contract"


def test_theme_is_optional_and_relationship_none_is_preserved(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["relationship_structure"] = {"mode": "none"}
    decision = next(d for d in manifest["wizard"]["decisions"] if d["id"] == "core_relationships")
    decision.update({"status": "none", "source": "none", "selected": []})
    project = tmp_path / "none"
    hatch_project(project, manifest)
    contract = BookStore(project).load_plan()["narrative_contract"]
    assert "theme" not in contract
    assert contract["core_relationships"] == {"mode": "none"}


def test_long_signed_contract_survives_hatch_and_empty_context_in_full(tmp_path: Path) -> None:
    manifest = _manifest()
    redline = "作者约定不能因篇幅删除。" * 30_000 + "末尾必须保留标记"
    manifest["hard_constraints"] = [redline]
    validate_hatch_manifest(manifest)
    project = tmp_path / "long-contract"
    hatch_project(project, manifest)
    store = BookStore(project)
    view = narrative_contract_view(store)
    assert view["contract"]["hard_constraints"] == [redline]
    brief = _write_plan_extend_brief(store, {"suggest_from": 4})
    assert read_json(Path(brief["path"]))["planning_context"]["narrative_contract"] == view


def test_corrupt_stored_contract_does_not_silently_fall_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = store.load_plan()
    plan["narrative_contract"] = {"schema": "unknown"}
    store.save_plan(plan)
    with pytest.raises(LedgerError) as exc:
        narrative_contract_view(store)
    assert exc.value.code == "narrative_contract_invalid"


@pytest.mark.parametrize("source", ["intent", "outline", "canon"])
def test_signed_source_changes_stop_planning_until_explicit_full_amendment(tmp_path: Path, source: str) -> None:
    hatch_project(tmp_path / source, _manifest())
    store = BookStore(tmp_path / source)
    before_plan = store.load_plan()
    before_view = narrative_contract_view(store)
    path = {"intent": store.intent_path, "outline": store.outline_path,
            "canon": store.canon_dir / "added-rule.md"}[source]
    atomic_text(path, (path.read_text(encoding="utf-8") if path.exists() else "") + "\n## 明确修订\n作者确认的新约定唯一标记。\n")
    for read in (lambda: narrative_contract_view(store),
                 lambda: _write_plan_extend_brief(store, {"chapter": 3, "suggest_from": 4})):
        with pytest.raises(LedgerError) as exc:
            read()
        assert exc.value.code == "narrative_contract_source_drift"
        assert exc.value.details["changed_sources"] == [source]
    if source == "canon":
        with pytest.raises(LedgerError) as exc:
            amend_narrative_contract(store, before_plan["narrative_contract"], actor="作者", reason="重签明确修订")
        assert exc.value.code == "narrative_contract_canon_unsynced"
        store.save_kb(extract_cards(store.canon_dir)["cards"], source_fingerprint=canon_source_fingerprint(store.canon_dir))
        # kb sync fixes the compiled source only; it does not silently re-sign.
        with pytest.raises(LedgerError) as exc:
            narrative_contract_view(store)
        assert exc.value.code == "narrative_contract_source_drift"
    contract = before_plan["narrative_contract"]
    contract["main_arc"]["endgame_condition"] += "：明确重签后的终局唯一标记"
    result = amend_narrative_contract(store, contract, actor="作者", reason="重签明确修订")
    current = store.load_plan()
    assert current["book_outline"] == before_plan["book_outline"]
    assert current["chapters"] == before_plan["chapters"]
    assert current["narrative_contract"]["hard_constraints"] == before_plan["narrative_contract"]["hard_constraints"]
    assert narrative_contract_view(store)["fingerprint"] != before_view["fingerprint"]
    audit = read_json(store.editorial_dir / "narrative-contract-amendments.json")
    assert audit[-1]["actor"] == "作者"
    assert audit[-1]["reason"] == "重签明确修订"
    assert audit[-1]["previous_contract"]["main_arc"]["endgame_condition"] != current["narrative_contract"]["main_arc"]["endgame_condition"]
    assert result["contract_fingerprint"] == audit[-1]["after_fingerprint"]


def test_legacy_contract_upgrade_and_missing_baseline_require_explicit_signing(tmp_path: Path) -> None:
    hatch_project(tmp_path / "upgrade", _manifest())
    store = BookStore(tmp_path / "upgrade")
    plan = store.load_plan()
    contract = plan.pop("narrative_contract")
    store.save_plan(plan)
    assert narrative_contract_view(store)["mode"] == "legacy_unstructured"
    contract.pop("source_baselines")
    plan["narrative_contract"] = contract
    store.save_plan(plan)
    with pytest.raises(LedgerError) as exc:
        narrative_contract_view(store)
    assert exc.value.code == "narrative_contract_source_unbound"
    amend_narrative_contract(store, contract, actor="作者", reason="旧合同显式绑定来源")
    assert narrative_contract_view(store)["mode"] == "structured"


def test_explicit_contract_binds_long_original_outline_without_clipping(tmp_path: Path) -> None:
    hatch_project(tmp_path / "long-source", _manifest())
    store = BookStore(tmp_path / "long-source")
    contract = store.load_plan()["narrative_contract"]
    atomic_text(store.outline_path, "历史大纲原文" * 60_000 + "末尾不能被剪去的作者红线")
    amend_narrative_contract(store, contract, actor="作者", reason="明确以完整合同约束长历史大纲")
    view = narrative_contract_view(store)
    assert view["mode"] == "structured"
    before = view["fingerprint"]
    atomic_text(store.outline_path, store.outline_path.read_text(encoding="utf-8") + "尾部新红线")
    with pytest.raises(LedgerError) as exc:
        narrative_contract_view(store)
    assert exc.value.code == "narrative_contract_source_drift"
    amend_narrative_contract(store, contract, actor="作者", reason="再次确认完整新来源")
    assert narrative_contract_view(store)["fingerprint"] != before


def test_contract_amend_requires_complete_json_and_idle_boundary(tmp_path: Path) -> None:
    hatch_project(tmp_path / "amend", _manifest())
    store = BookStore(tmp_path / "amend")
    contract = store.load_plan()["narrative_contract"]
    for invalid in ({"schema": NARRATIVE_CONTRACT_SCHEMA}, {**contract, "main_arc": {}}):
        with pytest.raises(LedgerError) as exc:
            amend_narrative_contract(store, invalid, actor="作者", reason="不得空签")
        assert exc.value.code == "narrative_contract_invalid"
    with pytest.raises(LedgerError) as exc:
        amend_narrative_contract(store, contract, actor="", reason="缺签发者")
    assert exc.value.code == "invalid_actor"
    head = store.read_head()
    head["phase"] = "await_draft"
    store.write_head(head)
    with pytest.raises(LedgerError) as exc:
        amend_narrative_contract(store, contract, actor="作者", reason="章中不能换约定")
    assert exc.value.code == "narrative_contract_amend_inflight"
    assert not (store.editorial_dir / "narrative-contract-amendments.json").exists()


def test_amend_contract_cli_signs_complete_file_and_records_author(tmp_path: Path, capsys) -> None:
    hatch_project(tmp_path / "cli", _manifest())
    store = BookStore(tmp_path / "cli")
    contract = store.load_plan()["narrative_contract"]
    contract["reader_promise"]["selling_points"] += "：作者确认的读者承诺补充"
    source = tmp_path / "complete-contract.json"
    atomic_json(source, contract)
    assert main(["plan", "amend-contract", "--project", str(store.project), "--file", str(source),
                 "--actor", "作者", "--reason", "明确补充读者承诺"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "plan_amend_contract"
    assert store.load_plan()["narrative_contract"]["reader_promise"] == contract["reader_promise"]
    audit = read_json(store.editorial_dir / "narrative-contract-amendments.json")
    assert audit[-1]["actor"] == "作者"
    assert audit[-1]["reason"] == "明确补充读者承诺"


@pytest.mark.parametrize("state,error", [("await_draft", "narrative_contract_amend_inflight"), ("completed", "book_completed")])
def test_amend_contract_cli_rejects_inflight_or_closed_book(tmp_path: Path, capsys, state: str, error: str) -> None:
    hatch_project(tmp_path / "cli-reject", _manifest())
    store = BookStore(tmp_path / "cli-reject")
    before = store.load_plan()
    source = tmp_path / "complete-contract.json"
    atomic_json(source, before["narrative_contract"])
    head = store.read_head()
    if state == "completed":
        head["status"] = state
    else:
        head["phase"] = state
    store.write_head(head)
    assert main(["plan", "amend-contract", "--project", str(store.project), "--file", str(source),
                 "--actor", "作者", "--reason", "不能在章中或封笔后重签"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == error
    assert store.load_plan() == before


def test_legacy_inputs_are_verbatim_unstructured_and_red_lines_never_clipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = "作者原始意图，不要从摘录猜新约定。\n" + "旧主线与人物选择。" * 700 + "\n作者红线：后段唯一红线标记。\n"
    atomic_text(store.intent_path, intent)
    outline = "## 主线与终局\n旧终局标记，不代表已兑现。\n\n## 大段场景史\n" + "旧场景史。" * 900 + "\n## 作者红线\n不能舍弃尾部红线。\n"
    atomic_text(store.outline_path, outline)
    store.save_kb([{"id": "rule-old", "kind": "rule", "hardness": "hard", "title": "世界铁律", "body": "旧世界铁律标记：交易均有代价。"}])
    view = narrative_contract_view(store)
    assert view["mode"] == "legacy_unstructured"
    assert view["contract"] is None
    intent_source = next(s for s in view["sources"] if s["kind"] == "intent")
    assert intent_source["text"] == intent
    assert intent_source["complete"] is True
    assert "不能舍弃尾部红线。" in "\n".join(s.get("text", "") for s in view["sources"])
    assert "旧终局标记" in "\n".join(s.get("text", "") for s in view["sources"])
    assert "旧世界铁律标记" in "\n".join(s.get("text", "") for s in view["sources"])
    assert view["omitted"]["source_chars"] > 0
    assert view["limitations"]
    before = view["fingerprint"]
    atomic_text(store.intent_path, intent + "已明确补充的读者承诺。")
    assert narrative_contract_view(store)["fingerprint"] != before


def test_long_legacy_intent_is_preserved_without_prefix_trim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = "正文方向" * 100_000 + "作者红线：末尾标记"
    atomic_text(store.intent_path, intent)
    view = narrative_contract_view(store)
    assert next(source["text"] for source in view["sources"] if source["kind"] == "intent") == intent



@pytest.mark.parametrize("source", ["outline", "canon"])
def test_legacy_fingerprint_binds_omitted_original_source_text(tmp_path: Path, source: str) -> None:
    store = _store(tmp_path)
    if source == "outline":
        path = store.outline_path
        atomic_text(path, "## 主线与终局\n保留的主线依据。\n## 历史场景\n" + "省略场景正文。" * 700 + "省略尾部旧标记。")
    else:
        path = store.canon_dir / "world.md"
        atomic_text(path, "## 世界规则\n" + "原始世界规则正文。" * 700 + "原始正典尾部旧标记。")
        store.save_kb([{"id": "legacy-world", "kind": "rule", "hardness": "hard", "title": "世界规则",
                       "source": "world.md", "body": "保持未变的旧编译卡片摘录。"}])
    before = narrative_contract_view(store)
    atomic_text(path, path.read_text(encoding="utf-8").replace("尾部旧标记", "尾部新标记"))
    after = narrative_contract_view(store)
    assert before["mode"] == after["mode"] == "legacy_unstructured"
    assert before["sources"] == after["sources"]
    assert before["omitted"] == after["omitted"]
    assert before["source_baselines"][source] != after["source_baselines"][source]
    assert before["fingerprint"] != after["fingerprint"]
    assert after["sources"] == before["sources"]


def test_legacy_canon_constraints_use_original_source_not_compiled_prefix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = "## 作者红线\n" + "规则正文。" * 900 + "原始正典末尾红线标记。\n"
    atomic_text(store.canon_dir / "constraints.md", original)
    store.save_kb([{"id": "constraints-old", "kind": "rule", "hardness": "hard", "title": "作者红线", "source": "constraints.md", "body": original[:1000]}])
    view = narrative_contract_view(store)
    constraint = next(s for s in view["sources"] if s["kind"] == "author_constraints")
    assert constraint["text"] == original
    assert view["omitted"]["canon_cards"] == 0


def test_real_hatch_tension_anchor_uses_canonical_spine(tmp_path: Path) -> None:
    manifest = _manifest()
    hatch_project(tmp_path / "tension", manifest)
    store = BookStore(tmp_path / "tension")
    brief = _write_plan_extend_brief(store, {"chapter": 3, "suggest_from": 4})
    context = read_json(Path(brief["path"]))["planning_context"]
    first_curve = manifest["step4_outline"]["event_spine"]["tension_curve"][0]
    assert context["batch_design"]["tension_now"] == {key: first_curve[key] for key in ("stage", "level", "mode")}


def test_missing_character_arc_is_explicit_diagnostic_without_fixed_quota(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["step4_outline"].pop("milestones")
    hatch_project(tmp_path / "arc", manifest)
    result = validate_plan(BookStore(tmp_path / "arc"))
    assert result["passed"] is True
    warnings = [w for w in result["warnings"] if w["code"] == "book_outline_character_arc_unspecified"]
    assert len(warnings) == 1
    assert "No fixed milestone count" in warnings[0]["message"]


def test_empty_planning_input_explains_exhausted_budget_and_keeps_rebudget_basis(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["book_words"] = 9600
    manifest["step5_volume1"].update({"word_budget": 9600, "chapters_budget": 3})
    add_volume_outline_fixture(manifest, count=1)
    # This test targets the complete projection; avoid scheduling a book-level
    # climax in the tiny seed batch merely to exercise the budget arithmetic.
    manifest["step4_outline"]["milestones"] = []
    hatch_project(tmp_path / "budget", manifest)
    store = BookStore(tmp_path / "budget")
    for number in range(1, 4):
        atomic_json(store.meta_path(number), {"chapter": number, "word_count": 2500})
    head = store.read_head()
    head.update({"chapter": 3, "phase": "idle", "last_committed_ch": 3, "last_acked_ch": 3})
    store.write_head(head)
    brief = _write_plan_extend_brief(store, {"chapter": 4, "suggest_from": 4, "max_planned_ch": 3})
    data = read_json(Path(brief["path"]))
    assert data["planning_context"]["book_scale"]["book_words_written"] == 7500
    assert data["planning_context"]["book_scale"]["rebudget_required"] is True
    assert data["budget_preflight"]["action"] == "plan rebudget"
    plan = store.load_plan()
    plan["book_outline"]["total_chapters"] = 4
    plan["book_outline"]["chapter_rebudget"] = {
        "schema": "novel-ledger.chapter-rebudget.v1", "book_words": 9600,
        "chapter_words_target": 3200, "signed_total_chapters": 3,
        "through_chapter": 3, "written_words": 7500, "total_chapters": 4,
        "additional_chapters": 1, "continuation_volume": "vol-0001",
        "milestone_shifts": [{"chapter_before": n, "chapter_after": n + 1, "kind": "turning_point"} for n in range(20)],
    }
    plan["volumes"]["vol-0001"]["chapters_budget"] = 4
    store.save_plan(plan)
    projected = read_json(Path(_write_plan_extend_brief(store, {"suggest_from": 4})["path"]))["planning_context"]["book_scale"]
    assert projected["rebudget_required"] is False
    basis = projected["chapter_rebudget"]
    assert basis["signed_total_chapters"] == 3
    assert basis["through_chapter"] == 3
    assert basis["written_words"] == 7500
    assert basis["total_chapters"] == 4
    assert len(basis["milestone_shifts"]) == 20
    assert basis["milestone_shifts_omitted"] == 0


def test_source_section_loads_complete_large_subtree_and_defers_unrelated_history(tmp_path: Path, capsys) -> None:
    store = _store(tmp_path)
    direction = "## 主线与终局\n" + "主角决定必须承担完整后果。" * 25_000 + "末尾主线依据。\n### 因果补充\n完整子节依据。\n"
    history = "## 历史场景\n" + "不属于默认方向上下文。" * 1000 + "历史末尾标记。\n"
    atomic_text(store.outline_path, direction + history)
    view = narrative_contract_view(store)
    loaded = "".join(source.get("text", "") for source in view["sources"])
    assert direction in loaded
    assert "历史末尾标记" not in loaded
    assert view["omitted"]["source_chars"] == len(history)
    assert main(["context", "read", "--project", str(store.project), "--source", "outline", "--section", "历史场景"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["text"] == history
    assert result["complete"] is True
    assert read_narrative_source(store, "outline", section="主线与终局")["text"] == direction


def test_context_canon_source_cannot_escape_project(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LedgerError) as exc:
        read_narrative_source(store, "canon", name="../../outside.md")
    assert exc.value.code == "invalid_context_source"


def test_volume_outline_cli_reads_selected_full_outline_and_index_only_by_default(tmp_path: Path, capsys) -> None:
    source = _manifest()
    source["volume_outline_contract"]["volumes"][2]["outline"] += "完整卷纲不能剪去。" * 6000 + "卷纲末尾标记"
    hatch_project(tmp_path / "cli-volume", source)
    store = BookStore(tmp_path / "cli-volume")
    assert main(["plan", "volume-outline", "--project", str(store.project)]) == 0
    index = json.loads(capsys.readouterr().out)
    assert index["selected"] is None
    assert len(index["index"]) == 3
    assert "outline" not in index["index"][2]
    assert main(["plan", "volume-outline", "--project", str(store.project), "--volume", "3"]) == 0
    loaded = json.loads(capsys.readouterr().out)
    assert loaded["selected"]["outline"] == source["volume_outline_contract"]["volumes"][2]["outline"]
    assert main(["plan", "volume-outline", "--project", str(store.project), "--volume", "99"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "volume_outline_missing"


def test_planning_loads_current_and_next_full_volume_but_only_indexes_later_prose(tmp_path: Path) -> None:
    manifest = _manifest()
    hatch_project(tmp_path / "volume-plan", manifest)
    store = BookStore(tmp_path / "volume-plan")
    brief = _write_plan_extend_brief(store, {"suggest_from": 4})
    view = read_json(Path(brief["path"]))["planning_context"]
    assert view["volumes"]["1"]["outline"] == manifest["volume_outline_contract"]["volumes"][0]["outline"]
    assert view["volumes"]["2"]["outline"] == manifest["volume_outline_contract"]["volumes"][1]["outline"]
    assert "3" not in view["volumes"]
    assert len(view["volume_outlines"]["index"]) == 3
    assert manifest["volume_outline_contract"]["volumes"][2]["outline"] not in json.dumps(view, ensure_ascii=False)


def test_import_signed_volume_plan_preserves_required_version_and_word_target(tmp_path: Path) -> None:
    from novel_ledger_core.control.bootstrap import init_project
    from novel_ledger_core.control.pipeline import chapter_next
    original = _manifest()
    hatch_project(tmp_path / "source-book", original)
    plan = BookStore(tmp_path / "source-book").load_plan()
    source = tmp_path / "signed-plan.json"
    atomic_json(source, plan)
    project = tmp_path / "import-book"
    init_project(project, plan_path=source)
    store = BookStore(project)
    assert store.load_config()["volume_outline_schema"] == "novel-ledger.volume-outlines.v1"
    assert store.load_config()["book_words"] == original["book_words"]
    plan.pop("volume_outline_contract")
    plan["book_outline"].pop("volume_outline_schema")
    store.save_plan(plan)
    with pytest.raises(LedgerError) as exc:
        chapter_next(store)
    assert exc.value.code == "event_spine_contract_violation"
    assert any(error["code"] == "volume_outline_contract_missing" for error in exc.value.details["errors"])


def test_invalid_signed_volume_import_rejects_before_layout_write(tmp_path: Path) -> None:
    from novel_ledger_core.control.bootstrap import init_project
    hatch_project(tmp_path / "source-book", _manifest())
    plan = BookStore(tmp_path / "source-book").load_plan()
    plan["volumes"]["vol-0002"]["outline"] = "不够五百字的未签完整卷纲"
    source = tmp_path / "bad-plan.json"
    atomic_json(source, plan)
    project = tmp_path / "bad-import"
    with pytest.raises(LedgerError) as exc:
        init_project(project, plan_path=source)
    assert exc.value.code == "volume_outline_contract_violation"
    assert not (project / "book").exists()
