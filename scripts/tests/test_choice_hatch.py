"""Tests for the low-input, guided-v3 book hatching contract."""

from __future__ import annotations

import json

import copy
import json

import pytest

from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import validate_plan
from novel_ledger_core.control.hatch import (
    assemble_plan_from_manifest,
    hatch_project,
    render_canon_markdowns,
    render_intent_markdown,
    render_outline_markdown,
    validate_hatch_manifest,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.story_map import plan_story_map_errors
from novel_ledger_core.infra.util import LedgerError, read_json


from novel_ledger_core.control.hatch_sample import (  # noqa: E402
    _decision,
    _extra_decision,
    _voice_decision,
    add_volume_outline_fixture,
    build_sample_manifest,
)


@pytest.fixture
def guided_manifest() -> dict:
    return build_sample_manifest()


def test_guided_v3_validates_renders_and_assembles(guided_manifest):
    assert validate_hatch_manifest(guided_manifest) is guided_manifest
    rendered = "\n".join([render_intent_markdown(guided_manifest), render_outline_markdown(guided_manifest), *render_canon_markdowns(guided_manifest).values()])
    assert "核心关系结构" in rendered
    assert "天星宗配额制度" in rendered
    assert "下一卷接口" in rendered
    assert "主线与支线事件链" in rendered
    assert "主角／对手／世界三线时钟" in rendered
    assert "幕级情节起伏曲线" in rendered
    assert "女主角：" not in rendered
    assert "后宫/多女主" not in rendered
    plan = assemble_plan_from_manifest(guided_manifest)
    assert plan["schema"] == "novel-ledger.plan.v2"
    assert plan_story_map_errors(plan) == []
    assert [item["chapter"] for item in plan["chapters"]] == [1, 2, 3]
    assert plan["book_outline"]["chapter_words_target"] == 3200
    assert plan["book_outline"]["total_chapters"] == 625
    assert plan["book_outline"]["event_spine"]["schema"] == "novel-ledger.event-spine.v1"
    assert plan["phases"][0]["mainline_event_ref"] == "main-ledger-fragment"
    assert plan["phases"][0]["subplot_event_refs"] == ["subplot-trust-exchange"]
    assert plan["phases"][0]["timeline_event_refs"] == [
        "timeline-protagonist-search",
        "timeline-antagonist-list",
        "timeline-world-close",
    ]
    assert plan["phases"][0]["tension_stage"] == "矿场破局"
    assert plan["phases"][0]["chapter_start"] == 1
    assert plan["phases"][0]["chapter_end"] == 3
    assert plan["phases"][0]["entry_state"]
    assert plan["phases"][0]["exit_state"]
    assert set(plan["phases"][0]["event_changes"]) == {
        "main-ledger-fragment",
        "subplot-trust-exchange",
        "timeline-protagonist-search",
        "timeline-antagonist-list",
        "timeline-world-close",
    }
    assert plan["chapters"][0]["phase_id"] == "phase-opening"
    assert plan["chapters"][0]["story_stage"] == "矿场破局"
    assert plan["chapters"][0]["thread_refs"] == ["thread-main", "thread-trust"]
    assert plan["chapters"][0]["clock_refs"] == ["clock-protagonist"]
    assert "long_term_commitments" not in plan["book_outline"]


def test_guided_v3_accepts_optional_character_profiles_and_opening_knowledge_refs(guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    manifest["character_profiles"] = {
        "韩立": {
            "background_register": "采药童子，熟悉药草和矿账",
            "speech_habits": "先问凭据，再追问期限",
            "under_pressure": "不轻易抬高声量，转而核对细节",
            "desire_and_mask": "想救母亲，对外只说要补账",
            "sample_quote": "药草在这儿，账得重新算。",
        }
    }
    manifest["step6_beats"]["chapters"][0]["knowledge_refs"] = ["ledger-fragment"]
    assert validate_hatch_manifest(manifest) is manifest
    plan = assemble_plan_from_manifest(manifest)
    assert plan["character_profiles"]["韩立"]["speech_habits"] == "先问凭据，再追问期限"
    assert plan["chapters"][0]["knowledge_refs"] == ["ledger-fragment"]


def test_guided_v3_persists_normalized_long_term_commitments(tmp_path, guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    manifest["step4_outline"]["long_term_commitments"] = [
        {
            "promise": "残页的缺口终将指向总账原件",
            "planted_volume": 1,
            "resolved_volume": 3,
            "description": "旧线索在公开质证时成为证据",
            "arc_ref": "thread-main",
            "seed_use": "第一卷先作为求生线索使用",
            "final_condition": "原件公开后主角必须付出代价",
        },
        {
            "id": "trust-ledger",
            "promise": "同盟交出的半份证据是否可信",
            "planted_volume": 2,
            "resolved_volume": 3,
            "description": "互信改变取得原件的路径",
            "arc_ref": "thread-trust",
        },
    ]
    assert validate_hatch_manifest(manifest) is manifest
    expected = [
        {
            "id": "commitment-0001",
            "promise": "残页的缺口终将指向总账原件",
            "planted_volume": 1,
            "resolved_volume": 3,
            "description": "旧线索在公开质证时成为证据",
            "arc_ref": "thread-main",
            "seed_use": "第一卷先作为求生线索使用",
            "final_condition": "原件公开后主角必须付出代价",
        },
        {
            "id": "trust-ledger",
            "promise": "同盟交出的半份证据是否可信",
            "planted_volume": 2,
            "resolved_volume": 3,
            "description": "互信改变取得原件的路径",
            "arc_ref": "thread-trust",
        },
    ]
    plan = assemble_plan_from_manifest(manifest)
    assert plan["schema"] == "novel-ledger.plan.v2"
    assert plan["book_outline"]["long_term_commitments"] == expected
    assert "id" not in manifest["step4_outline"]["long_term_commitments"][0]
    outline = render_outline_markdown(manifest)
    assert "| ID | 承诺 | 埋设卷 | 计划回收卷 | 说明 |" in outline
    assert "| commitment-0001 | 残页的缺口终将指向总账原件 | 1 | 3 | 旧线索在公开质证时成为证据 |" in outline
    assert "| trust-ledger | 同盟交出的半份证据是否可信 | 2 | 3 | 互信改变取得原件的路径 |" in outline

    project = tmp_path / "long-commitments"
    hatch_project(project, manifest)
    assert BookStore(project).load_plan()["book_outline"]["long_term_commitments"] == expected


def test_long_arc_surface_belief_stays_editorial_not_hard_world_rule(tmp_path, guided_manifest):
    from novel_ledger_core.content.pack import assemble_pack
    manifest = copy.deepcopy(guided_manifest)
    manifest["long_arc_question"] = {
        "mode": "present",
        "core_mystery": "残页缺失的一角去了哪里",
        "surface_illusion": "韩立以为是矿工偷走了缺角",
        "decryption_ladder": [
            {"stage": "矿场破局", "truth": "缺角并非矿工所取"},
            {"stage": "宗门追索", "truth": "缺角进入宗门总账"},
        ],
    }
    validate_hatch_manifest(manifest)
    outline = assemble_plan_from_manifest(manifest)["book_outline"]
    assert outline["long_arc_question"]["surface_illusion"] == "韩立以为是矿工偷走了缺角"
    assert len(outline["long_arc_question"]["decryption_ladder"]) == 2
    assert "韩立以为是矿工偷走了缺角" in render_outline_markdown(manifest)
    assert "韩立以为是矿工偷走了缺角" not in render_canon_markdowns(manifest)["world_rules.md"]
    project = tmp_path / "mystery-boundary"
    hatch_project(project, manifest)
    pack = assemble_pack(BookStore(project), 1)
    assert "韩立以为是矿工偷走了缺角" not in json.dumps(pack, ensure_ascii=False)


def test_hatch_preserves_opening_scene_anchors_for_foreshadowing(tmp_path, guided_manifest):
    from novel_ledger_core.content.pack import assemble_pack

    manifest = copy.deepcopy(guided_manifest)
    first = manifest["step6_beats"]["chapters"][0]
    first.update({
        "location": "矿场账房",
        "present": ["韩立", "南宫婉"],
        "tags": ["缺角印信"],
        "goal": "借缺角印信识破假令",
    })
    plan = assemble_plan_from_manifest(manifest)
    assert {key: plan["chapters"][0][key] for key in ("location", "present", "tags", "goal")} == {
        "location": "矿场账房", "present": ["韩立", "南宫婉"],
        "tags": ["缺角印信"], "goal": "借缺角印信识破假令",
    }

    project = tmp_path / "seed-anchors"
    hatch_project(project, manifest)
    pack = assemble_pack(BookStore(project), 1)
    assert any(card["name"] == "南宫婉" for card in pack["present_cards"])
    assert pack["now_card"]["goal"] == "借缺角印信识破假令"


@pytest.mark.parametrize(
    ("commitments", "field"),
    [
        ([{"promise": " ", "planted_volume": 1, "resolved_volume": 3}], ".promise"),
        ([{"promise": "旧约", "planted_volume": 0, "resolved_volume": 3}], ".planted_volume"),
        ([{"promise": "旧约", "planted_volume": 1, "resolved_volume": True}], ".resolved_volume"),
        ([{"promise": "旧约", "planted_volume": 4, "resolved_volume": 3}], "long_term_commitments[0]"),
        ([
            {"id": "same", "promise": "旧约", "planted_volume": 1, "resolved_volume": 3},
            {"id": "same", "promise": "再记", "planted_volume": 2, "resolved_volume": 3},
        ], ".id"),
        ([
            {"id": "commitment-0002", "promise": "旧约", "planted_volume": 1, "resolved_volume": 3},
            {"promise": "再记", "planted_volume": 2, "resolved_volume": 3},
        ], ".id"),
        ([{"promise": "旧约", "planted_volume": 1, "resolved_volume": 3, "arc_ref": "missing-thread"}], ".arc_ref"),
    ],
)
def test_guided_v3_rejects_invalid_long_term_commitments(guided_manifest, commitments, field):
    manifest = copy.deepcopy(guided_manifest)
    manifest["step4_outline"]["long_term_commitments"] = commitments
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"
    assert field in exc.value.details["field"]


@pytest.mark.parametrize("mutation", ["missing_subplot", "missing_clock", "curve_order", "flat_curve", "bad_touchpoint_stage"])
def test_guided_v3_rejects_invalid_story_map(guided_manifest, mutation):
    manifest = copy.deepcopy(guided_manifest)
    outline = manifest["step4_outline"]
    spine = outline["event_spine"]
    if mutation == "missing_subplot":
        spine["subplots"] = []
    elif mutation == "missing_clock":
        spine["timelines"] = spine["timelines"][:2]
    elif mutation == "curve_order":
        spine["tension_curve"].reverse()
    elif mutation == "flat_curve":
        for point in spine["tension_curve"]:
            point.update({"level": 3, "mode": "build"})
    else:
        spine["mainline"]["events"][0]["stage"] = "不存在的幕"
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"


def test_guided_v3_rejects_implausible_volume_pace(guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    manifest["step5_volume1"]["word_budget"] = 18_000
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"


def test_protagonist_name_cliche_is_advisory_only():
    """反套路主角名：潇洒姓×玄乎单字的两字名警；俗字/三字/双字名不警。

    纯 advisory：不进 validate_hatch_manifest 的错误路径（名字是创作选择，只警不拦），
    只随 book hatch 响应回 name_warnings，作者坚持以作者为准。
    """
    from novel_ledger_core.control.hatch import protagonist_name_cliche_warnings

    for cliche in ("叶辰", "林轩", "苏尘", "沈夜", "楚枫"):
        warn = protagonist_name_cliche_warnings(cliche)
        assert [w["code"] for w in warn] == ["protagonist_name_cliche"], cliche
        assert "称呼链" in warn[0]["hint"]
    for clean in ("张铁", "赵显", "韩立", "苏九", "沈青临", "李有粮", "陈守拙", "裴听雨"):
        assert protagonist_name_cliche_warnings(clean) == [], clean


def test_hatch_project_persists_confirmed_trace_and_starts_next(tmp_path, guided_manifest):
    project = tmp_path / "guided"
    result = hatch_project(project, guided_manifest)
    assert result["action"] == "book_hatch"
    assert result["schema"] == "novel-ledger.hatch.v3"
    # 反套路主角名 advisory：随响应回 name_warnings；"韩立"是俗字名，干净
    assert result["name_warnings"] == []
    store = BookStore(project)
    persisted = read_json(store.hatch_manifest_path)
    assert persisted["wizard"]["final_confirmation"] is True
    assert len(persisted["wizard"]["decisions"]) == 9
    assert persisted["wizard"]["decisions"][-1]["voice_id"] == "shijing"
    # 显式回到出厂低水位（conftest 全局把 plan_low_water 置 0 会掩盖默认配置下的首跑行为）：
    # 起手恰 3 章是 v3 契约，首跑必须直接进 draft 而不是 extend_plan
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    store.save_config(cfg)
    assert main(["chapter", "next", "--project", str(project)]) == 0
    assert store.read_head()["phase"] == "await_draft"
    canonical = read_json(store.current_pack_path)
    draft = read_json(store.draft_pack_path(1))
    polish = read_json(store.polish_pack_path(1))
    assemble = read_json(store.assemble_pack_path(1))
    assert canonical["story_focus"]["stage"] == "矿场破局"
    assert [item["id"] for item in canonical["story_focus"]["threads"]] == ["thread-main", "thread-trust"]
    assert "本章故事线路与时钟" in draft["writing_brief"]
    assert "资源垄断证据链" in draft["writing_brief"]
    assert "主角生存窗口" in draft["writing_brief"]
    assert "thread-main" not in draft["writing_brief"]
    assert "clock-protagonist" not in draft["writing_brief"]
    assert "phase-opening" not in draft["writing_brief"]
    assert "矿场破局" in assemble["verification_brief"]
    assert "story_focus" not in polish
    assert "story_focus" not in assemble
    assert "资源垄断证据链" in assemble["verification_brief"]


def test_hatch_project_cannot_bypass_spine_by_removing_plan_schema(tmp_path, guided_manifest):
    project = tmp_path / "schema-locked"
    hatch_project(project, guided_manifest)
    store = BookStore(project)
    plan = store.load_plan()
    plan.pop("schema")
    store.save_plan(plan)

    result = validate_plan(store)

    assert result["passed"] is False
    assert "event_spine_plan_schema_missing" in {item["code"] for item in result["errors"]}


def test_hatch_can_install_xianxia_mortal_as_an_inline_choice(tmp_path, guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    manifest["background"] = "xianxia-mortal"
    project = tmp_path / "xianxia"

    result = hatch_project(project, manifest)

    assert result["background"] == "xianxia-mortal"
    assert "境界.md" in result["background_files"]
    assert "灵石.md" in result["background_files"]
    assert "README.md" not in result["background_files"]
    canon = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((project / "book" / "kb" / "canon").glob("*.md"))
    )
    cards = "\n".join(
        str(card.get("body") or "")
        for card in read_json(project / "book" / "kb" / "cards.json")["cards"]
    )
    for token in ("炼气", "筑基", "金丹", "灵压", "妖兽等阶"):
        assert token in canon
        assert token in cards

    invalid = copy.deepcopy(guided_manifest)
    invalid["background"] = "not-a-real-pack"
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(invalid)
    assert exc.value.code == "unknown_background"


def test_cli_check_only_is_read_only_and_hatch_uses_same_contract(tmp_path, guided_manifest):
    manifest_path = tmp_path / "guided.json"
    manifest_path.write_text(json.dumps(guided_manifest, ensure_ascii=False), encoding="utf-8")
    check_project = tmp_path / "must-not-exist"
    assert main(["book", "hatch", "--project", str(check_project), "--manifest", str(manifest_path), "--check-only"]) == 0
    assert not check_project.exists()
    project = tmp_path / "initialized"
    assert main(["book", "hatch", "--project", str(project), "--manifest", str(manifest_path)]) == 0
    assert BookStore(project).hatch_manifest_path.exists()
    assert main(["book", "hatch", "--manifest", str(manifest_path), "--voice", "cinematic"]) == 1


def test_old_or_missing_schema_is_rejected(guided_manifest):
    for schema in (None, "novel-ledger.hatch.v1", "novel-ledger.hatch.v2"):
        manifest = copy.deepcopy(guided_manifest)
        manifest.pop("schema") if schema is None else manifest.update({"schema": schema})
        with pytest.raises(LedgerError) as exc:
            validate_hatch_manifest(manifest)
        assert exc.value.code == "invalid_hatch_schema"


def test_guided_v3_rejects_unconfirmed_or_incomplete_choices(guided_manifest):
    unconfirmed = copy.deepcopy(guided_manifest)
    unconfirmed["wizard"]["final_confirmation"] = False
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(unconfirmed)
    assert exc.value.code == "hatch_unresolved"
    incomplete = copy.deepcopy(guided_manifest)
    incomplete["wizard"]["decisions"][0]["options"][0].pop("risk")
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(incomplete)
    assert exc.value.code == "invalid_hatch_v3"


@pytest.mark.parametrize("mutation", ["order", "duplicate_outcome", "bad_default"])
def test_option_rationality_and_dependency_order_are_enforced(guided_manifest, mutation):
    manifest = copy.deepcopy(guided_manifest)
    decisions = manifest["wizard"]["decisions"]
    if mutation == "order":
        decisions[0], decisions[1] = decisions[1], decisions[0]
    elif mutation == "duplicate_outcome":
        decisions[0]["options"][1]["outcome"] = decisions[0]["options"][0]["outcome"]
    else:
        decisions[0].update({"source": "default", "recommended_id": "A", "selected": ["B"]})
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"


def test_none_decision_rejects_hidden_payload(guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    decision = next(item for item in manifest["wizard"]["decisions"] if item["id"] == "story_mechanism")
    decision.update({"status": "none", "source": "none", "selected": []})
    manifest["story_mechanism"] = {"mode": "none", "name": "不应保留的机制"}
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"


def test_author_supplied_decision_can_skip_candidate_generation(guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    decision = manifest["wizard"]["decisions"][0]
    decision.update({"source": "author_supplied", "options": [], "selected": [], "summary": "沿用作者给定的读者承诺"})
    decision.pop("recommended_id")
    decision.pop("recommendation_reason")
    assert validate_hatch_manifest(manifest) is manifest


def test_voice_choice_is_independent_of_genre_and_persists_selected_manual(tmp_path, guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    manifest["step1_hook"]["genre_tags"] = ["科幻", "家庭"]
    manifest["step1_hook"]["narrative_tone"] = "温暖、幽默"
    voice_decision = manifest["wizard"]["decisions"][-1]
    voice_decision.update({"selected": ["B"], "voice_id": "cinematic", "summary": "选择电影镜头文风"})
    manifest["voice"] = "cinematic"

    assert validate_hatch_manifest(manifest) is manifest
    project = tmp_path / "cinematic"
    hatch_project(project, manifest)
    store = BookStore(project)
    assert store.load_voice_profile()["voice_id"] == "cinematic"
    persisted = read_json(store.hatch_manifest_path)
    assert persisted["wizard"]["decisions"][-1]["voice_id"] == "cinematic"
    assert persisted["step1_hook"]["genre_tags"] == ["科幻", "家庭"]


def test_author_supplied_voice_records_an_independent_id(guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    voice_decision = manifest["wizard"]["decisions"][-1]
    voice_decision.update({
        "source": "author_supplied", "options": [], "selected": [],
        "voice_id": "lyrical", "summary": "沿用作者明确指定的细腻抒情文风",
    })
    voice_decision.pop("recommended_id")
    voice_decision.pop("recommendation_reason")
    manifest["voice"] = "lyrical"
    assert validate_hatch_manifest(manifest) is manifest


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_voice_id", "invalid_hatch_v3"),
        ("top_level_mismatch", "invalid_hatch_v3"),
        ("candidate_mismatch", "invalid_hatch_v3"),
        ("duplicate_candidate_voice", "invalid_hatch_v3"),
        ("unknown_voice", "unknown_voice"),
    ],
)
def test_voice_decision_must_match_an_available_selected_voice(guided_manifest, mutation, expected_code):
    manifest = copy.deepcopy(guided_manifest)
    voice_decision = manifest["wizard"]["decisions"][-1]
    if mutation == "missing_voice_id":
        voice_decision.pop("voice_id")
    elif mutation == "top_level_mismatch":
        manifest["voice"] = "cinematic"
    elif mutation == "candidate_mismatch":
        voice_decision["selected"] = ["B"]
    elif mutation == "duplicate_candidate_voice":
        voice_decision["options"][1]["voice_id"] = "shijing"
    else:
        voice_decision["voice_id"] = "not-a-real-voice"
        voice_decision["options"][0]["voice_id"] = "not-a-real-voice"
        manifest["voice"] = "not-a-real-voice"
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == expected_code


def test_confirmed_eight_gate_v3_manifest_remains_compatible(guided_manifest):
    legacy = copy.deepcopy(guided_manifest)
    legacy["wizard"]["decisions"] = legacy["wizard"]["decisions"][:-2] + [_decision("scale_voice")]
    assert validate_hatch_manifest(legacy) is legacy


@pytest.mark.parametrize(
    "placements",
    [
        [("extra_cost_boundary", "protagonist_engine")],
        [("extra_cost_boundary", "reader_promise"), ("extra_opening_constraint", "voice_style")],
    ],
)
def test_additional_hatch_steps_validate_and_persist(tmp_path, guided_manifest, placements):
    manifest = copy.deepcopy(guided_manifest)
    decisions = manifest["wizard"]["decisions"]
    extra_cost = "催熟一次便失去一夜精气，异香会留下可追踪的线索"
    manifest["story_mechanism"]["cost_and_backlash"] = extra_cost
    for decision_id, after in placements:
        position = next(index for index, item in enumerate(decisions) if item["id"] == after) + 1
        extra = _extra_decision(decision_id.removeprefix("extra_"), after)
        if decision_id == "extra_cost_boundary":
            extra["summary"] = f"确认机制代价：{extra_cost}"
        if decision_id == "extra_opening_constraint":
            extra.update({"none_allowed": True, "status": "none", "source": "none", "selected": [],
                          "summary": "此书暂不设额外开篇限制"})
            manifest["hard_constraints_mode"] = "none"
            manifest["hard_constraints"] = []
        decisions.insert(position, extra)

    assert validate_hatch_manifest(manifest) is manifest
    project = tmp_path / f"guided-{len(decisions)}"
    hatch_project(project, manifest)
    store = BookStore(project)
    persisted = read_json(store.hatch_manifest_path)
    assert [item["id"] for item in persisted["wizard"]["decisions"]] == [item["id"] for item in decisions]
    assert len(persisted["wizard"]["decisions"]) == len(decisions)
    assert all(item["reason"] for item in persisted["wizard"]["decisions"] if item["id"].startswith("extra_"))
    assert persisted["story_mechanism"]["cost_and_backlash"] == extra_cost
    intent = store.intent_path.read_text(encoding="utf-8")
    assert extra_cost in intent
    if len(placements) == 2:
        assert persisted["hard_constraints_mode"] == "none"
        assert persisted["hard_constraints"] == []
        assert "未声明；不得由系统替作者创建红线" in intent


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("duplicate_extra_id", "invalid_hatch_v3"),
        ("duplicate_axis", "invalid_hatch_v3"),
        ("reordered_base", "invalid_hatch_v3"),
        ("extra_before_base", "invalid_hatch_v3"),
        ("wrong_after", "invalid_hatch_v3"),
        ("missing_reason", "invalid_hatch_v3"),
        ("unconfirmed_extra", "hatch_unresolved"),
        ("none_without_permission", "invalid_hatch_v3"),
        ("non_boolean_none_allowed", "invalid_hatch_v3"),
        ("invalid_extra_id", "invalid_hatch_v3"),
        ("missing_base", "invalid_hatch_v3"),
        ("legacy_with_extra", "invalid_hatch_v3"),
    ],
)
def test_additional_hatch_steps_reject_invalid_contract(guided_manifest, mutation, expected_code):
    manifest = copy.deepcopy(guided_manifest)
    decisions = manifest["wizard"]["decisions"]
    extra = _extra_decision("cost_boundary", "reader_promise")
    decisions.insert(1, extra)
    if mutation == "duplicate_extra_id":
        decisions.insert(2, _extra_decision("cost_boundary", "reader_promise"))
    elif mutation == "duplicate_axis":
        extra["axis"] = decisions[0]["axis"]
    elif mutation == "reordered_base":
        decisions[2], decisions[3] = decisions[3], decisions[2]
    elif mutation == "extra_before_base":
        decisions.insert(0, decisions.pop(1))
    elif mutation == "wrong_after":
        extra["after"] = "plot_architecture"
    elif mutation == "missing_reason":
        extra.pop("reason")
    elif mutation == "unconfirmed_extra":
        extra["status"] = "unresolved"
    elif mutation == "none_without_permission":
        extra.update({"status": "none", "source": "none", "selected": []})
    elif mutation == "non_boolean_none_allowed":
        extra["none_allowed"] = "false"
    elif mutation == "invalid_extra_id":
        extra["id"] = "extra_Bad"
    elif mutation == "missing_base":
        decisions.pop(2)
    elif mutation == "legacy_with_extra":
        decisions[:] = [_decision(item) for item in (
            "reader_promise", "plot_architecture", "protagonist_engine", "core_relationships",
            "world_opposition", "story_mechanism", "main_arc", "scale_voice",
        )]
        decisions.insert(1, extra)
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == expected_code


def test_extra_hatch_step_requires_final_confirmation(guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    manifest["wizard"]["decisions"].insert(1, _extra_decision("cost_boundary", "reader_promise"))
    manifest["wizard"]["final_confirmation"] = False
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "hatch_unresolved"


@pytest.mark.parametrize("mutation", ["chapter_number", "empty_beats", "must_anchor", "unknown_thread_ref", "bad_story_stage"])
def test_seed_plan_is_executable_and_precise(guided_manifest, mutation):
    manifest = copy.deepcopy(guided_manifest)
    if mutation == "chapter_number":
        manifest["step6_beats"]["chapters"][0]["chapter"] = 2
    elif mutation == "empty_beats":
        manifest["step6_beats"]["chapters"][0]["beats"] = []
    elif mutation == "unknown_thread_ref":
        manifest["step6_beats"]["chapters"][0]["thread_refs"] = ["thread-missing"]
    elif mutation == "bad_story_stage":
        manifest["step6_beats"]["chapters"][0]["story_stage"] = "不存在的幕"
    else:
        manifest["step6_beats"]["chapters"][0]["beats"][0]["must"] = "正文中不存在"
    with pytest.raises(LedgerError):
        validate_hatch_manifest(manifest)


def test_seed_chapter_fewer_than_five_scenes_is_allowed(guided_manifest, tmp_path):
    manifest = copy.deepcopy(guided_manifest)
    manifest["step6_beats"]["chapters"][0]["beats"] = manifest["step6_beats"]["chapters"][0]["beats"][:2]
    assert validate_hatch_manifest(manifest) is manifest
    result = hatch_project(tmp_path / "short-opening", manifest)
    assert any(item["code"] == "beats_count_off_band" and item["chapter"] == 1 for item in result["planning_warnings"])


def test_invalid_voice_is_prevalidated_without_project_shell(tmp_path, guided_manifest):
    manifest = copy.deepcopy(guided_manifest)
    manifest["voice"] = "not-a-real-voice"
    manifest_path = tmp_path / "bad-voice.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "must-not-exist"
    assert main(["book", "hatch", "--project", str(project), "--manifest", str(manifest_path)]) == 1
    assert not project.exists()


def test_pov_and_fence_are_optional_but_shape_checked(guided_manifest):
    """视角结构与平台尺度：可选（旧 manifest 不带仍合法），声明即校验形状并随 plan/intent 落盘。"""
    validate_hatch_manifest(guided_manifest)  # 基线不带这两个字段：合法

    manifest = copy.deepcopy(guided_manifest)
    manifest["narrative_pov"] = {
        "structure": "dual_alternating",
        "anchor": "韩立与南宫婉",
        "switch_granularity": "volume",
    }
    manifest["content_fence"] = {"tier": "split", "notes": "主线走平台安全线"}
    validate_hatch_manifest(manifest)

    plan = assemble_plan_from_manifest(manifest)
    assert plan["narrative_pov"]["structure"] == "dual_alternating"
    assert plan["content_fence"]["tier"] == "split"
    # 基线 manifest 组装出的 plan 不带这两个键（未声明 = 不落盘）
    assert "narrative_pov" not in assemble_plan_from_manifest(guided_manifest)

    rendered = render_intent_markdown(manifest)
    assert "视角结构" in rendered and "双主角交替" in rendered
    assert "平台尺度" in rendered and "分区分层" in rendered


@pytest.mark.parametrize(
    "mutation",
    [
        {"narrative_pov": {"structure": "omniscient"}},  # 非法结构枚举
        {"narrative_pov": {"structure": "ensemble"}},  # 群像缺切换粒度
        {"content_fence": {"tier": "loose"}},  # 非法档位
        {"narrative_pov": "单视角"},  # 不是对象
    ],
)
def test_pov_and_fence_reject_illegal_shapes(guided_manifest, mutation):
    manifest = copy.deepcopy(guided_manifest)
    manifest.update(mutation)
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"


def test_hatched_pack_carries_pov_and_fence_into_writing_brief(tmp_path, guided_manifest):
    """端到端：声明过的 manifest 经 hatch 落 plan，pack 带渲染句并进 pack_hash，writing_brief 定位段可见。"""
    from novel_ledger_core.content.pack import assemble_pack
    from novel_ledger_core.content.views import render_writing_brief
    from novel_ledger_core.infra.store import BookStore

    manifest = copy.deepcopy(guided_manifest)
    manifest["narrative_pov"] = {"structure": "single_limited", "anchor": "韩立"}
    manifest["content_fence"] = {"tier": "standard"}
    project = tmp_path / "guided"
    hatch_project(project, manifest)

    store = BookStore(project)
    pack = assemble_pack(store, 1)
    assert "单视角限知" in pack["narrative_pov"]
    assert "中等尺度" in pack["content_fence"]
    # 渲染句必须被 pack_hash 覆盖：篡改视角字段会使 hash 比对失效
    tampered = dict(pack)
    tampered["narrative_pov"] = pack["narrative_pov"] + "；篡改"
    from novel_ledger_core.infra.util import pack_hash_digest
    assert pack_hash_digest({k: v for k, v in tampered.items() if k != "pack_hash"}) != pack["pack_hash"]

    brief = render_writing_brief(pack)
    assert "单视角限知" in brief and "中等尺度" in brief
    # 未声明视角的包不渲染该行（回退兼容）
    assert "视角结构" not in render_writing_brief({"chapter": 4})


def test_first_next_returns_draft_under_default_low_water(tmp_path, guided_manifest):
    """出厂 plan_low_water=5 下，新书首跑必须直接 draft；第一章提交后低水位提示恢复。

    旧缺陷：v3 起手恰 3 章 < 默认水位 5，首跑曾返回 extend_plan，
    违反「首次 next 直接返回 draft」的开书验收承诺（SKILL.md / hatch-wizard.md §四）。
    此前测试没发现，是因为 conftest 全局把 plan_low_water 置 0——默认配置下的
    首跑行为从未被覆盖。低水位提示本身保留，只是不再在新书第一章之前触发。
    """
    from novel_ledger_core.control.pipeline import chapter_next

    project = tmp_path / "lowwater"
    hatch_project(project, guided_manifest)
    store = BookStore(project)
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    store.save_config(cfg)

    first = chapter_next(store)
    assert first["action"] == "draft", first.get("action")

    # 预置第 1 章已提交并 ack：库存剩 2 章 ≤ 5，提示恢复触发（去重后同批只提示一次）
    head = store.read_head()
    head.update({"phase": "idle", "chapter": 1, "last_committed_ch": 1, "last_acked_ch": 1})
    store.write_head(head)
    second = chapter_next(store)
    assert second["action"] == "extend_plan", second.get("action")
    assert second["remaining_in_plan"] == 2
    third = chapter_next(store)
    assert third["action"] == "draft", third.get("action")


def test_sample_manifest_roundtrips_through_check_only(tmp_path, capsys):
    """`--sample-manifest` 导出的样例必须原样通过 `--check-only` 全量校验。

    样例、校验器、导出通道三处同源；任何一边漂移这里立刻红——
    跨环境开书的宿主从此拿模板起步，不必读校验器源码反推形状。
    """
    from novel_ledger_core.control.cli import main

    assert main(["book", "hatch", "--sample-manifest"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["action"] == "hatch_sample_manifest"
    sample = payload["manifest"]
    assert sample["schema"] == "novel-ledger.hatch.v3"
    out = tmp_path / "sample.json"
    out.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert main(["book", "hatch", "--check-only", "--manifest", str(out)]) == 0
    check = json.loads(capsys.readouterr().out)
    assert check["ok"] is True and check["guided"] is True


def test_hatch_without_manifest_and_without_sample_is_rejected(capsys):
    from novel_ledger_core.control.cli import main

    assert main(["book", "hatch"]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "missing_manifest"
