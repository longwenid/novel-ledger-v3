"""Signed chapter pace and demand-loaded planning input for long serials."""

from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import _write_plan_extend_brief
from novel_ledger_core.content.pack import word_targets
from novel_ledger_core.infra.scale import chapter_word_target_for_band, derive_chapter_word_target, scale_contract_errors
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, read_json
from novel_ledger_core.ledger.ledger import commit_event


def test_signed_scale_uses_declared_pace_instead_of_fixed_floor() -> None:
    plan = {
        "book_outline": {
            "book_words": 320_000,
            "chapter_words_target": 3200,
            "total_chapters": 100,
        },
        "volumes": {"vol-01": {"word_budget": 160_000, "chapters_budget": 50}},
        "chapters": [],
    }
    assert derive_chapter_word_target(plan) == 3200
    assert scale_contract_errors(plan, {"book_words": 320_000}) == []


def test_planning_pace_matches_writing_aim_for_configured_bands() -> None:
    for band in ({"min": 2500, "max": 8000}, {"min": 20, "max": 5000}, {"min": 3000, "max": 3400}):
        assert chapter_word_target_for_band(band) == word_targets(band)["aim_chars"]


def test_legacy_signed_pace_still_valid_when_self_consistent() -> None:
    plan = {
        "book_outline": {
            "book_words": 250_000,
            "chapter_words_target": 2500,
            "total_chapters": 100,
        },
        "volumes": {"vol-01": {"word_budget": 125_000, "chapters_budget": 50}},
        "chapters": [],
    }
    assert derive_chapter_word_target(plan) == 2500
    assert scale_contract_errors(plan, {"book_words": 250_000}) == []


def test_unsigned_volume_budget_uses_configured_writing_aim(tmp_path: Path) -> None:
    source = tmp_path / "plan.json"
    source.write_text(json.dumps({
        "protagonist": "主角",
        "chapters": [{"chapter": 1, "present": ["主角"], "beats": [{"id": "b1", "text": "启程"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "small-book"
    init_project(project, plan_path=source, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    result = store.extend_plan([], volumes={"vol-01": {"word_budget": 7200, "chapters_budget": 10}})
    assert result["added"] == []


def test_plan_brief_loads_recent_scope_in_full_at_one_thousand_chapters(tmp_path: Path) -> None:
    source = tmp_path / "plan.json"
    source.write_text(json.dumps({
        "protagonist": "主角",
        "chapters": [{
            "chapter": number,
            "volume": "vol-01",
            "location": "市集",
            "present": ["主角"],
            "beats": [{
                "id": "b1", "text": ("主角与掌柜谈完旧账并换来新的通行线索" * 1000 + "当前章末完整章拍") if number == 1000 else "千章以前的历史依据" + str(number),
                "effects": {"hooks": [{"id": "h-future", "status": "open", "due": 1004}]} if number == 1000 else {},
            }],
        } for number in range(1, 1001)],
    }, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    init_project(project, plan_path=source, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    commit_event(store, chapter=999, state_delta={
        "named": ["主角"],
        "facts": [{"who": "主角", "text": "旧账已经付清", "pin": True}],
        "hooks": [{"id": "h-next", "text": "千章旧约", "due": 1000, "status": "open"}],
    })
    brief = _write_plan_extend_brief(store, {"chapter": 1000, "suggest_from": 1001})
    data = read_json(Path(brief["path"]))
    context = data["planning_context"]
    assert len(context["recent_chapters"]) <= 3
    assert context["recent_chapters"][-1]["chapter"] == 1000
    assert any(item["id"] == "h-next" for item in context["due_hooks"])
    assert any(item["id"] == "h-future" for item in context["planned_hooks"])
    assert "旧账已经付清" in context["characters"][0]["facts"]
    assert context["recent_chapters"][-1]["beats"][0]["text"].endswith("当前章末完整章拍")
    assert "千章以前的历史依据1" not in json.dumps(context, ensure_ascii=False)
    assert context["available_sources"]["plan"] == str(store.plan_path)


def test_long_commitment_recalled_with_seed_and_recent_touches_hundreds_of_chapters_later(tmp_path: Path) -> None:
    source = tmp_path / "plan.json"
    source.write_text(json.dumps({
        "protagonist": "主角",
        "book_outline": {"long_term_commitments": [{
            "id": "commitment-seal",
            "promise": "缺角印信的来历终将改变主角的盟约",
            "planted_volume": 1,
            "resolved_volume": 10,
            "arc_ref": "thread-main",
            "seed_use": "初次露面时排除假令",
            "final_condition": "主角凭印信作证并承担失去盟友的代价",
        }], "long_arc_question": {
            "core_mystery": "缺角为何出现",
            "surface_illusion": "众人以为缺角出自敌手",
            "decryption_ladder": [{"stage": "第四卷", "truth": "旧盟友也曾持有印信"}],
        }},
        "chapters": [{
            "chapter": number,
            "volume": "vol-04" if number >= 160 else "vol-01",
            "present": ["主角"],
            "beats": [{"id": "b1", "text": "主角追查旧令"}],
        } for number in range(1, 202)],
    }, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    init_project(project, plan_path=source, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    for number, meaning in ((1, "缺角印信排除假令"), (80, "缺角印信换来一次信任"), (200, "缺角印信使旧盟友生疑")):
        commit_event(store, chapter=number, state_delta={"named": ["主角"], "hooks": [{
            "id": "commitment-seal", "text": meaning, "status": "open",
            "quote": meaning, "arc_ref": "thread-main",
        }]})

    brief = _write_plan_extend_brief(store, {"chapter": 201, "suggest_from": 202})
    context = read_json(Path(brief["path"]))["planning_context"]
    assert context["due_hooks"] == []  # 最终回收尚远，短期到期队列不会带它
    entry = context["long_term_commitments"][0]
    assert entry["id"] == "commitment-seal"
    assert entry["ledger_hook"]["opened_chapter"] == 1
    assert entry["ledger_hook"]["seed_text"] == "缺角印信排除假令"
    assert entry["ledger_hook"]["touch_count"] == 3
    assert [touch["chapter"] for touch in entry["ledger_hook"]["recent_touches"]] == [80, 200]
    assert context["long_arc_question"]["surface_illusion"] == "众人以为缺角出自敌手"
    assert context["long_term_commitments"][0]["final_condition"] == "主角凭印信作证并承担失去盟友的代价"
