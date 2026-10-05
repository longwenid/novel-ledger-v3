"""Opening signs all volume obligations before rolling chapter refinement."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from novel_ledger_core.control.cli import main
from novel_ledger_core.control.hatch import hatch_project, validate_hatch_manifest
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import plan_rebudget, chapter_next, validate_plan, audit_book
from novel_ledger_core.content.story_review import _spec
from novel_ledger_core.infra.scale import scale_contract_errors
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.story_map import plan_story_map_errors
from novel_ledger_core.infra.volume_outline import (VOLUME_OUTLINE_SCHEMA, outline_han_count,
    volume_outline_errors, volume_outline_view, volume_outline_warnings)
from novel_ledger_core.infra.util import LedgerError, atomic_json, atomic_text, read_json, sha256_text
from novel_ledger_core.ledger.ledger import commit_event, verify_ledger
from scripts.tests.test_choice_hatch import guided_manifest, add_volume_outline_fixture
from scripts.tests.test_autopilot import _store


def manifest() -> dict:
    return guided_manifest.__wrapped__()


def book(tmp_path: Path) -> BookStore:
    hatch_project(tmp_path / "novel", manifest())
    return BookStore(tmp_path / "novel")


def test_hatch_signs_all_volume_originals_and_persists_complete_source(tmp_path: Path) -> None:
    source = manifest()
    hatch_project(tmp_path / "all", source)
    store = BookStore(tmp_path / "all")
    plan = store.load_plan()
    assert plan["volume_outline_contract"]["volume_count"] == 3
    assert list(plan["volumes"]) == ["vol-0001", "vol-0002", "vol-0003"]
    assert store.load_config()["volume_outline_schema"] == VOLUME_OUTLINE_SCHEMA
    assert sum(v["word_budget"] for v in plan["volumes"].values()) == source["book_words"]
    for signed in source["volume_outline_contract"]["volumes"]:
        persisted = plan["volumes"][f"vol-{signed['volume']:04d}"]
        assert persisted == signed
        assert signed["outline"] in store.outline_path.read_text(encoding="utf-8")
        assert outline_han_count(signed["outline"]) >= 500
    assert volume_outline_errors(plan) == plan_story_map_errors(plan) == []


@pytest.mark.parametrize("han,accepted", [(499, False), (500, True), (501, True)])
def test_each_volume_minimum_counts_han_characters_only(han: int, accepted: bool) -> None:
    source = manifest()
    source["volume_outline_contract"]["volumes"][1]["outline"] = "汉" * han + "abc123，。!" * 800
    if accepted:
        validate_hatch_manifest(source)
    else:
        with pytest.raises(LedgerError) as exc:
            validate_hatch_manifest(source)
        assert any(e["code"] == "volume_outline_too_short" and e["han_count"] == 499 for e in exc.value.details["errors"])


@pytest.mark.parametrize("failure", ["missing_contract", "missing_last", "missing_role", "budget", "duplicate", "act_coverage", "seed_mismatch"])
def test_incomplete_book_outlines_reject_before_hatch_writes(tmp_path: Path, failure: str) -> None:
    source = manifest()
    if failure == "missing_contract": source.pop("volume_outline_contract")
    elif failure == "missing_last": source["volume_outline_contract"]["volumes"].pop()
    elif failure == "missing_role": source["volume_outline_contract"]["volumes"][1].pop("advances")
    elif failure == "budget": source["volume_outline_contract"]["volumes"][1]["word_budget"] -= 100
    elif failure == "duplicate": source["volume_outline_contract"]["volumes"][1]["outline"] = source["volume_outline_contract"]["volumes"][0]["outline"]
    elif failure == "act_coverage": source["step4_outline"]["acts"][1]["volumes"] = "2"
    elif failure == "seed_mismatch": source["step5_volume1"]["title"] += "不应另签首卷"
    with pytest.raises(LedgerError): validate_hatch_manifest(source)
    project = tmp_path / failure
    with pytest.raises(LedgerError): hatch_project(project, source)
    assert not (project / "book").exists()


def test_check_only_rejects_missing_volume_without_initializing(tmp_path: Path, capsys) -> None:
    source = manifest()
    source["volume_outline_contract"]["volumes"].pop()
    path = tmp_path / "manifest.json"
    atomic_json(path, source)
    project = tmp_path / "readonly"
    assert main(["book", "hatch", "--project", str(project), "--manifest", str(path), "--check-only"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_hatch_v3"
    assert not (project / "book").exists()


@pytest.mark.parametrize("field", ["outline", "plot_role", "inherits", "advances", "ending_direction", "next_handoff", "word_budget"])
def test_extend_cannot_change_signed_volume_obligations(tmp_path: Path, field: str) -> None:
    store = book(tmp_path)
    before = store.load_plan()
    old = before["volumes"]["vol-0002"][field]
    value = old + 1 if isinstance(old, int) else old + "改约"
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([], volumes={"vol-0002": {field: value}})
    assert exc.value.code == "volume_outline_contract_violation"
    assert store.load_plan() == before


def test_extend_refines_existing_volume_but_cannot_add_unknown_or_downgrade(tmp_path: Path) -> None:
    store = book(tmp_path)
    original = copy.deepcopy(store.load_plan()["volumes"]["vol-0002"])
    store.extend_plan([], volumes={"vol-0002": {"detail_outline": "下一阶段先比对出入库记录，再让证人在有第三方的条件下作证。"}})
    current = store.load_plan()
    assert {k: current["volumes"]["vol-0002"][k] for k in original} == original
    assert current["volumes"]["vol-0002"]["detail_outline"]
    with pytest.raises(LedgerError): store.extend_plan([], volumes={"vol-0004": {"detail_outline": "不能偷偷增加未知后续卷"}})
    current.pop("volume_outline_contract")
    current["book_outline"].pop("volume_outline_schema")
    store.save_plan(current)
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([], volumes={"vol-0002": {"recap": "不能删版本降级"}})
    assert exc.value.code == "volume_outline_contract_violation"


def test_tampered_deleted_or_unknown_volume_is_diagnosed_by_plan_validator(tmp_path: Path) -> None:
    plan = book(tmp_path).load_plan()
    plan["volumes"]["vol-0002"]["outline"] += "作者没有改签的漂移"
    assert any(e["code"] == "volume_outline_contract_drift" for e in plan_story_map_errors(plan))
    plan["volumes"].pop("vol-0003")
    assert any(e["code"] == "volume_outline_registry_incomplete" for e in plan_story_map_errors(plan))


def test_volume_view_gives_complete_direction_index_and_only_selected_prose(tmp_path: Path) -> None:
    plan = book(tmp_path).load_plan()
    view = volume_outline_view(plan, 2)
    assert view["selected"]["outline"] == plan["volumes"]["vol-0002"]["outline"]
    assert "outline" not in view["index"][0]
    assert view["index"][1]["field_paths"]["outline"] == "plan.volumes.vol-0002.outline"
    assert view["index"][0]["chapter_start"] == 1
    assert view["index"][1]["chapter_start"] == plan["volumes"]["vol-0001"]["chapters_budget"] + 1
    assert view["index"][-1]["chapter_end"] == plan["volume_outline_contract"]["signed_volume_chapters"]
    assert all(view["index"][0][key] == plan["volumes"]["vol-0001"][key] for key in ("inherits", "advances", "ending_direction", "next_handoff", "goal", "spine"))


def test_legacy_book_is_compatible_with_explicit_missing_outline_diagnostic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert volume_outline_errors(store.load_plan()) == []
    assert volume_outline_warnings(store.load_plan())[0]["code"] == "volume_outline_legacy_unsigned"
    assert volume_outline_view(store.load_plan())["mode"] == "legacy_unsigned"


@pytest.mark.parametrize("key", ["vol-01", "1", "第一卷"])
def test_legacy_volume_view_reads_aliases_by_volume_number(key: str) -> None:
    entry = {"spine": "旧编号卷脊", "outline": "原卷完整文字"}
    view = volume_outline_view({"volumes": {key: entry}}, 1)
    assert view["mode"] == "legacy_unsigned"
    assert view["selected"]["id"] == key
    assert view["selected"]["outline"] == entry["outline"]


def test_huge_invalid_volume_count_is_shape_error_without_constructing_registry() -> None:
    plan = {"volume_outline_contract": {"schema": VOLUME_OUTLINE_SCHEMA, "volume_count": 10 ** 30, "volume_ids": []}}
    assert volume_outline_errors(plan)[0]["code"] == "volume_outline_registry_invalid"


def test_signed_volume_marker_survives_initial_plan_migration(tmp_path: Path) -> None:
    source = book(tmp_path).load_plan()
    path = tmp_path / "migrated-plan.json"
    atomic_json(path, source)
    project = tmp_path / "migrated"
    init_project(project, plan_path=path, protagonist=source["protagonist"], book_words=source["book_outline"]["book_words"])
    migrated = BookStore(project)
    assert migrated.load_config()["volume_outline_schema"] == VOLUME_OUTLINE_SCHEMA
    bad = copy.deepcopy(source)
    bad["volumes"]["vol-0003"]["outline"] = "删掉卷纲"
    atomic_json(path, bad)
    with pytest.raises(LedgerError): init_project(tmp_path / "bad-migration", plan_path=path, protagonist=source["protagonist"])
    assert not (tmp_path / "bad-migration" / "book").exists()


def test_deleted_volume_markers_stop_next_and_are_visible_in_validate_and_audit(tmp_path: Path) -> None:
    store = book(tmp_path)
    plan = store.load_plan()
    plan.pop("volume_outline_contract")
    plan["book_outline"].pop("volume_outline_schema")
    store.save_plan(plan)
    assert store.load_plan() == plan  # Damaged projects remain inspectable.
    with pytest.raises(LedgerError) as rejected:
        chapter_next(store)
    assert any(e["code"] == "volume_outline_contract_missing" for e in rejected.value.details["errors"])
    assert any(e["code"] == "volume_outline_contract_missing" for e in validate_plan(store)["errors"])
    assert "volume_outline_contract_missing" in json.dumps(audit_book(store), ensure_ascii=False)


def test_new_book_volume_and_book_review_require_original_outline_obligations(tmp_path: Path) -> None:
    store = book(tmp_path)
    for number in range(1, 4):
        prose = f"第{number}章：韩立在矿场查验账目，并保留取得证据的代价。"
        atomic_text(store.chapter_md_path(number), prose)
        atomic_json(store.ack_path(number), {"verdict": "pass", "prose_hash": "sha256:" + sha256_text(prose)})
    volume = _spec(store, "volume", 1, 3, "vol-0001")
    assert "volume_plot_and_handoff" in volume["required_checks"]
    assert volume["volume_outlines"]["selected"]["outline"] == store.load_plan()["volumes"]["vol-0001"]["outline"]
    endgame = _spec(store, "book", 1, 3)
    assert "volume_chain_endgame" in endgame["required_checks"]
    assert len(endgame["volume_outlines"]["index"]) == 3


def test_multi_volume_rounding_rebudget_preserves_all_signed_directions_and_full_outlines(tmp_path: Path) -> None:
    source = manifest()
    source["book_words"] = 80_000
    source["step5_volume1"].update(word_budget=25_001, chapters_budget=8)
    add_volume_outline_fixture(source)
    hatch_project(tmp_path / "short-chapters", source)
    store = BookStore(tmp_path / "short-chapters")
    plan = store.load_plan()
    original = copy.deepcopy(plan["volumes"])
    seal = copy.deepcopy(plan["volume_outline_contract"])
    assert seal["signed_total_chapters"] == 25
    assert seal["signed_volume_chapters"] == 26
    # Explicitly prepare an otherwise valid 25-chapter plan; this fixture tests
    # accounting and immutability, not the literary quality of these placeholders.
    opening = copy.deepcopy(plan["phases"][0])
    seed = copy.deepcopy(plan["chapters"][0])
    phases, chapters = [], []
    for start, end, volume in ((1, 8, 1), (9, 17, 2), (18, 25, 3)):
        phase = copy.deepcopy(opening)
        phase.update(id=f"phase-volume-{volume}", chapter_start=start, chapter_end=end)
        if volume > 1:
            phase.update(story_stage="宗门追索", tension_stage="宗门追索", mainline_event_ref="main-ledger-proof",
                         subplot_event_refs=["subplot-trust-cost"],
                         timeline_event_refs=["timeline-protagonist-hearing", "timeline-antagonist-forgery", "timeline-world-merge"])
            refs = [phase["mainline_event_ref"], *phase["subplot_event_refs"], *phase["timeline_event_refs"]]
            phase["event_changes"] = {ref: "既有证据改变下一步质证条件" for ref in refs}
        phases.append(phase)
        for number in range(start, end + 1):
            chapter = copy.deepcopy(seed)
            chapter.update(chapter=number, volume=volume, phase_id=phase["id"], story_stage=phase["story_stage"])
            chapter["clock_refs"] = [["clock-protagonist"], ["clock-antagonist"], ["clock-world"]][(number - start) % 3]
            chapters.append(chapter)
    plan.update(chapters=chapters, phases=phases)
    assert plan_story_map_errors(plan) == []
    store.save_plan(plan)
    with store.transaction():
        for number in range(1, 26):
            atomic_text(store.chapter_md_path(number), "韩立查账" + "主" * 2496)
            atomic_json(store.meta_path(number), {"chapter": number, "word_count": 2500})
            commit_event(store, number, {"named": ["韩立"]})
        head = store.read_head()
        head.update(phase="idle", chapter=25, last_committed_ch=25, last_acked_ch=25)
        store.write_head(head)
        revised = plan_rebudget(store, actor="planning-editor", reason="25章合法短章后按实写字数保留终局容量")
    after = store.load_plan()
    assert revised["total_chapters"] == 31 and revised["additional_chapters"] == 6
    assert after["volume_outline_contract"] == seal
    assert sum(v["chapters_budget"] for v in after["volumes"].values()) == 32
    assert after["volumes"]["vol-0003"]["chapters_budget"] == original["vol-0003"]["chapters_budget"] + 6
    for ident, entry in original.items():
        assert outline_han_count(after["volumes"][ident]["outline"]) >= 500
        assert {key: value for key, value in after["volumes"][ident].items() if key != "chapters_budget"} == {key: value for key, value in entry.items() if key != "chapters_budget"}
    assert volume_outline_errors(after) == plan_story_map_errors(after) == []
    assert scale_contract_errors(after, store.load_config(), written_words=62_500) == []
    assert verify_ledger(store)["consistent"]


def test_long_volume_and_hatch_text_is_retained_without_upper_character_limit(tmp_path: Path) -> None:
    source = manifest()
    source["volume_outline_contract"]["volumes"][2]["outline"] += "终局完整卷纲保留" * 5000 + "结尾原样标记"
    source["narrative_pov"] = {"structure": "single_limited", "anchor": "人物认知边界" * 1000 + "锚点尾部"}
    source["content_fence"] = {"tier": "free", "notes": "作者具体平台约束" * 1000 + "尺度尾部"}
    validate_hatch_manifest(source)
    hatch_project(tmp_path / "long", source)
    store = BookStore(tmp_path / "long")
    assert store.load_plan()["volumes"]["vol-0003"]["outline"] == source["volume_outline_contract"]["volumes"][2]["outline"]
    assert "结尾原样标记" in store.outline_path.read_text(encoding="utf-8")
