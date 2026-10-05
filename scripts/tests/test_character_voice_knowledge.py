"""Character-specific dialogue and epistemic state stay distinct from book-wide style.

These tests cover deterministic context delivery and ledger continuity. Whether a
character feels alive in prose remains an editorial judgment, not a machine gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.content.gates import _delta_issues, _delta_quote_issues, _expected_delta_issues
from novel_ledger_core.content.pack import assemble_pack, collect_expected_delta, inputs_fingerprint
from novel_ledger_core.content.views import make_assemble_view, make_draft_view, make_polish_view
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import validate_plan
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json
from novel_ledger_core.ledger.ledger import commit_event, load_snapshot, replay_events, select_knowledge, verify_ledger


def _profile(register: str, habit: str, pressure: str, mask: str, quote: str) -> dict[str, str]:
    return {
        "background_register": register,
        "speech_habits": habit,
        "under_pressure": pressure,
        "desire_and_mask": mask,
        "sample_quote": quote,
    }


def _store(tmp_path: Path, *, with_profiles: bool = True) -> BookStore:
    plan: dict = {
        "title": "角色声音与认知测试",
        "protagonist": "主角",
        "volume_spine": "主角查清旧印来历。",
        "chapters": [
            {
                "chapter": 1,
                "location": "账房",
                "present": ["主角", "掌柜"],
                "beats": [{"id": "b1", "text": "主角向掌柜查问旧印", "must": "旧印"}],
            },
            {
                "chapter": 621,
                "location": "账房",
                "present": ["主角", "掌柜"],
                "knowledge_refs": ["seal-origin"],
                "beats": [{"id": "b621", "text": "掌柜在旧印面前改口", "must": "旧印"}],
            },
        ],
    }
    if with_profiles:
        plan["character_profiles"] = {
            "主角": _profile(
                "行商出身，先问价再问人", "短句收尾，总把问题落到物件",
                "焦急时追问凭据", "想查真相，却装作只关心生意", "把旧印拿来，我先看缺口。",
            ),
            "掌柜": _profile(
                "老账房，按辈分称呼", "先铺条件，再给答案",
                "受逼问时转而报出具体数目", "想护住旧友，却装作只在对账", "这账，得从三年前算。",
            ),
            "使者": _profile(
                "宫中来人", "只说结果", "受压时沉默", "想保命却装镇定", "诏令已到。",
            ),
        }
    source = tmp_path / "plan.json"
    source.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    project.mkdir()
    init_project(project, plan_path=source, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def _knowledge(who: str, topic: str, claim: str, stance: str, source: str) -> dict[str, str]:
    return {
        "who": who,
        "topic_id": topic,
        "claim": claim,
        "stance": stance,
        "source": source,
        "quote": f"{who}得知：{claim}",
    }


def test_present_character_profiles_reach_draft_and_polish_without_offstage_voice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pack = assemble_pack(store, 1)
    assert set(pack["character_profiles"]) == {"主角", "掌柜"}
    assert pack["character_profiles"]["主角"]["speech_habits"] == "短句收尾，总把问题落到物件"
    assert pack["character_profiles"]["掌柜"]["under_pressure"] == "受逼问时转而报出具体数目"

    draft = make_draft_view(pack)
    brief = draft["writing_brief"]
    assert "短句收尾，总把问题落到物件" in brief
    assert "先铺条件，再给答案" in brief
    assert "诏令已到" not in brief
    assert "character_profiles" not in draft  # draft has one rendered brief, not duplicate JSON

    polish = make_polish_view(pack)
    assert set(polish["character_profiles"]) == {"主角", "掌柜"}
    assert set(polish["character_profiles"]["掌柜"]) == {"background_register", "speech_habits", "under_pressure"}
    assert "想护住旧友" not in json.dumps(polish, ensure_ascii=False)
    assert "knowledge" not in polish  # voice editor gets dialogue anchors, not character secrets

    assemble = make_assemble_view(pack)
    assert set(assemble["character_profiles"]) == {"主角", "掌柜"}
    assert assemble["character_profiles"]["掌柜"]["desire_and_mask"] == "想护住旧友，却装作只在对账"
    assert "使者" not in assemble["character_profiles"]


def test_knowledge_revision_survives_hundreds_of_chapters_and_reference_wins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    commit_event(store, 1, {"named": ["主角", "掌柜", "使者"], "knowledge": [
        _knowledge("掌柜", "seal-origin", "旧印缺口是敌人刻的", "believes", "坊间传言"),
        _knowledge("使者", "gate-secret", "北门的钥匙藏在旧印里", "knows", "亲眼看见"),
    ]})
    commit_event(store, 500, {"knowledge": [
        _knowledge("掌柜", "seal-origin", "旧印缺口是旧友刻的", "knows", "旧友亲口承认"),
    ]})
    commit_event(store, 620, {"knowledge": [
        _knowledge("掌柜", "market-news", "市集明日不开门", "knows", "告示"),
    ]})

    snapshot = load_snapshot(store)
    assert replay_events(store)["knowledge"] == snapshot["knowledge"]
    seal_rows = [row for row in snapshot["knowledge"] if row["who"] == "掌柜" and row["topic_id"] == "seal-origin"]
    assert len(seal_rows) == 1  # same actor/topic revises, rather than preserving contradictory current claims
    assert seal_rows[0]["claim"] == "旧印缺口是旧友刻的"
    assert seal_rows[0]["stance"] == "knows"

    pack = assemble_pack(store, 621)
    selected = pack["knowledge"]
    assert selected[0]["topic_id"] == "seal-origin", "explicit knowledge_refs beat newer unrelated news"
    assert {row["who"] for row in selected} == {"掌柜"}
    assert all(row["topic_id"] != "gate-secret" for row in selected), "offstage secret must not leak into writer context"
    brief = make_draft_view(pack)["writing_brief"]
    assert "旧印缺口是旧友刻的" in brief
    assert "旧印缺口是敌人刻的" not in brief
    assert "北门的钥匙藏在旧印里" not in brief
    assert "knowledge" not in make_polish_view(pack)


def test_multiple_explicit_knowledge_refs_each_get_a_slot() -> None:
    names = [f"角色{i}" for i in range(16)]
    knowledge = [dict(_knowledge(name, "old-promise", "他们记得旧约", "knows", "在场"), updated_chapter=10)
                 for name in names]
    knowledge.append(dict(_knowledge(names[0], "new-clue", "已见新线索", "suspects", "亲眼看到"), updated_chapter=1))
    selected, omitted = select_knowledge(
        snapshot={"knowledge": knowledge}, names=names,
        topic_ids=["old-promise", "new-clue"], cap=16,
    )
    assert len(selected) == 16 and omitted == 1
    assert {item["topic_id"] for item in selected} == {"old-promise", "new-clue"}


def test_pack_keeps_all_explicit_in_scene_knowledge_beyond_legacy_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    names = ["主角", *(f"角色{i}" for i in range(8))]
    plan = store.load_plan()
    plan["chapters"][1]["present"] = names
    plan["chapters"][1]["knowledge_refs"] = ["old-promise", "new-clue"]
    atomic_json(store.plan_path, plan)
    updates = [
        _knowledge(name, topic, f"{name}记住了{topic}", "knows", "亲眼看到")
        for name in names for topic in ("old-promise", "new-clue")
    ]
    commit_event(store, 620, {"knowledge": updates})
    pack = assemble_pack(store, 621)
    assert len(pack["knowledge"]) == len(updates)
    assert {(row["who"], row["topic_id"]) for row in pack["knowledge"]} == {(row["who"], row["topic_id"]) for row in updates}
    assert all(row["claim"] == f"{row['who']}记住了{row['topic_id']}" for row in pack["knowledge"])


def test_profile_edit_changes_pack_input_fingerprint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before = inputs_fingerprint(store, 1)
    plan = store.load_plan()
    plan["character_profiles"]["掌柜"]["speech_habits"] = "改用断句，回答前先重复对方的问题"
    atomic_json(store.plan_path, plan)
    assert inputs_fingerprint(store, 1) != before


def test_plan_extend_updates_character_profile_without_appending_chapter(tmp_path: Path, capsys) -> None:
    store = _store(tmp_path)
    before = inputs_fingerprint(store, 1)
    payload = tmp_path / "profiles.json"
    payload.write_text(json.dumps({
        "character_profiles": {
            "掌柜": _profile(
                "老账房，仍熟悉人情账", "改用断句，回答前先复述问题",
                "受逼问时先沉默", "已决定保护旧友，仍装作公事公办", "这账，我来认。",
            ),
        },
    }, ensure_ascii=False), encoding="utf-8")
    assert main(["plan", "extend", "--project", str(store.project), "--chapters", str(payload)]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["character_profiles_updated"] == ["掌柜"]
    assert response["added"] == []
    assert store.load_plan()["character_profiles"]["主角"]["speech_habits"] == "短句收尾，总把问题落到物件"
    assert store.load_plan()["character_profiles"]["掌柜"]["speech_habits"] == "改用断句，回答前先复述问题"
    assert inputs_fingerprint(store, 1) != before


def test_knowledge_delta_requires_actor_topic_claim_stance_source_and_quote() -> None:
    pack = {"now_card": {"name": "主角"}}
    good = _knowledge("主角", "seal-origin", "旧印缺口是旧友刻的", "knows", "旧友亲口承认")
    assert _delta_issues({"knowledge": [good]}, pack) == []
    for field in ("who", "topic_id", "claim", "stance", "source", "quote"):
        broken = dict(good)
        broken[field] = ""
        issues = _delta_issues({"knowledge": [broken]}, pack)
        assert issues, f"missing knowledge.{field} must be rejected"
    broken = dict(good)
    broken["stance"] = "guessed"
    assert _delta_issues({"knowledge": [broken]}, pack), "unknown stance must not become a silent ledger state"
    assert _delta_issues({"knowledge": "not-a-list"}, pack), "knowledge must be a list"


def test_knowledge_quote_requires_exact_prose_evidence() -> None:
    good = _knowledge("主角", "seal-origin", "旧印缺口是旧友刻的", "knows", "旧友亲口承认")
    prose = good["quote"] + "，掌柜终于认了。"
    assert _delta_quote_issues({"knowledge": [good]}, prose) == []
    invented = dict(good, quote="主角得知：旧印缺口是敌人刻的")
    issues = _delta_quote_issues({"knowledge": [invented]}, prose)
    assert any(issue["code"] == "delta_quote_not_in_prose" for issue in issues)


def test_direct_knowledge_commit_rejects_malformed_state_before_event_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broken = _knowledge("掌柜", "seal-origin", "旧印有缺口", "knows", "亲眼看见")
    broken["stance"] = "perhaps"
    with pytest.raises(LedgerError, match="stance is invalid"):
        commit_event(store, 1, {"knowledge": [broken]})
    assert replay_events(store)["knowledge"] == []


def test_planned_knowledge_effect_requires_matching_delta_and_quote(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = store.load_plan()
    planned = {
        "who": "掌柜",
        "topic_id": "seal-origin",
        "claim": "旧印缺口是旧友刻的",
        "stance": "knows",
        "source": "旧友亲口承认",
    }
    plan["chapters"][0]["beats"][0]["effects"] = {"knowledge": [planned]}
    atomic_json(store.plan_path, plan)

    expected = collect_expected_delta(plan["chapters"][0]["beats"])
    assert expected["knowledge"] == [planned]
    pack = assemble_pack(store, 1)
    assert pack["expected_delta"]["knowledge"] == [planned]
    assert planned["claim"] in make_draft_view(pack)["writing_brief"]

    missing = _expected_delta_issues({"knowledge": []}, pack)
    assert any(issue["code"] == "expected_delta_missing" and issue["kind"] == "knowledge" for issue in missing)
    unquoted = _expected_delta_issues({"knowledge": [planned]}, pack)
    assert any(issue["code"] == "expected_delta_quote_missing" and issue["kind"] == "knowledge" for issue in unquoted)
    quoted = dict(planned, quote="掌柜得知：旧印缺口是旧友刻的")
    assert _expected_delta_issues({"knowledge": [quoted]}, pack) == []
    for field, wrong in (("claim", "缺口是敌人刻的"), ("stance", "suspects"), ("source", "路人传言")):
        changed = dict(quoted, **{field: wrong})
        issues = _expected_delta_issues({"knowledge": [changed]}, pack)
        assert any(issue["code"] == "expected_delta_missing" and issue["kind"] == "knowledge" for issue in issues)


def test_legacy_snapshot_without_knowledge_loads_and_verifies(tmp_path: Path) -> None:
    store = _store(tmp_path, with_profiles=False)
    old_snapshot = read_json(store.snapshot_path)
    old_snapshot.pop("knowledge", None)
    atomic_json(store.snapshot_path, old_snapshot)
    assert load_snapshot(store)["knowledge"] == []
    report = verify_ledger(store)
    assert report["consistent"], report["diffs"]
    assert report["event_chain_ok"]


def test_legacy_plan_without_profiles_still_assembles(tmp_path: Path) -> None:
    store = _store(tmp_path, with_profiles=False)
    pack = assemble_pack(store, 1)
    assert not pack.get("character_profiles")
    assert make_draft_view(pack)["writing_brief"]


def test_empty_plan_still_validates_character_profiles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = store.load_plan()
    plan["chapters"] = []
    plan["character_profiles"]["主角"]["speech_habits"] = 123
    atomic_json(store.plan_path, plan)
    report = validate_plan(store)
    assert report["passed"] is False
    assert report["error_count"] == 1
    assert report["errors"][0]["code"] == "invalid_character_profiles"
