"""Continuity fixes exercised through submission, commit and crash recovery."""
from __future__ import annotations

import copy

import pytest

from tests.conftest import patch_pipeline_name

from novel_ledger_core.content.gates import _delta_issues, _expected_delta_issues
from novel_ledger_core.content.pack import collect_expected_delta, inputs_fingerprint
from novel_ledger_core.content.recall import recall
from novel_ledger_core.content.views import make_draft_view
from novel_ledger_core.control import pipeline
from novel_ledger_core.control.pipeline import (
    ack_read, chapter_next, close_relation, defer_hook, submit_output,
)
from novel_ledger_core.infra.story_map import plan_effects_key_issues
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json
from novel_ledger_core.ledger.ledger import (
    EMPTY_SNAPSHOT, apply_delta_conflicts, commit_event, load_snapshot,
    read_events, replay_events, verify_ledger,
)
from tests.decoupled_helpers import advance_to_assembly
from tests.test_autopilot import _store

DEATH_PROSE = "主角与掌柜走进试炼场。掌柜被落石击中当场身亡。主角把他的遗体安放在墙边。"
REVIVAL_PROSE = "主角回到试炼场寻找掌柜。掌柜从死亡中苏醒重新活了过来。掌柜走入店铺与主角继续商议。"
PACT_PROSE = "主角与掌柜在试炼场相遇。两人在众人面前缔结了盟约。掌柜承诺明天归还祖传铜印。"


def _book(tmp_path, effects=None):
    store = _store(tmp_path, chapters=3)
    cfg = store.load_config()
    cfg["word_band_enforce"] = False
    store.save_config(cfg)
    plan = store.load_plan()
    for chapter in plan["chapters"]:
        chapter["present"] = ["主角", "掌柜"]
    if effects:
        for chapter, value in effects.items():
            plan["chapters"][chapter - 1]["beats"][0]["effects"] = value
    atomic_json(store.plan_path, plan)
    return store


def _output(store, delta):
    pack = read_json(store.current_pack_path)
    return {
        "l1_summary": "主角与掌柜在本章完成试炼并形成新的局面。",
        "state_delta": delta,
        "memory": {"voice_concepts": []},
        "plot_findings": [],
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1"],
    }


def _submit(store, prose, delta):
    advance_to_assembly(store, prose=prose)
    result = submit_output(store, _output(store, delta))
    assert result["verdict"] == "accepted", result


def _commit_and_ack(store, prose):
    result = chapter_next(store)
    assert result["action"] == "ack", result
    quotes = [part for part in prose.split("。") if part]
    ack_read(store, quotes=quotes)


def _dead_chapter(store):
    _submit(store, DEATH_PROSE, {
        "named": ["主角", "掌柜"],
        "deaths": [{"who": "掌柜", "quote": "掌柜被落石击中当场身亡"}],
    })
    _commit_and_ack(store, DEATH_PROSE)


def test_revival_commits_normal_presence_and_move_and_replays(tmp_path):
    store = _book(tmp_path, {2: {"revivals": ["掌柜"]}})
    _dead_chapter(store)
    _submit(store, REVIVAL_PROSE, {
        "named": ["主角", "掌柜"], "revivals": [{"who": "掌柜"}],
        "moves": [{"who": "掌柜", "to": "店铺"}],
    })
    _commit_and_ack(store, REVIVAL_PROSE)
    snapshot = load_snapshot(store)
    assert snapshot["entities"]["掌柜"]["dead"] is False
    assert snapshot["entities"]["掌柜"]["location"] == "店铺"
    assert snapshot["entities"]["掌柜"]["revived_chapter"] == 2
    assert replay_events(store) == snapshot
    assert verify_ledger(store)["consistent"]


def test_nonliving_effect_commits_ghost_presence_without_reviving(tmp_path):
    store = _book(tmp_path, {2: {"nonliving": ["掌柜"]}})
    _dead_chapter(store)
    prose = "主角重新走进试炼场。掌柜的鬼魂站在遗体旁留下口信。主角听完口信便独自走出门口。"
    _submit(store, prose, {"named": ["主角", "掌柜"], "nonliving": ["掌柜"]})
    _commit_and_ack(store, prose)
    assert load_snapshot(store)["entities"]["掌柜"]["dead"] is True
    assert verify_ledger(store)["consistent"]


@pytest.mark.parametrize("kind", ["deaths", "revivals", "nonliving"])
@pytest.mark.parametrize("entry", [None, True, 4, "", "  ", {}, {"who": 3}])
def test_lifecycle_rejects_invalid_entries_without_changing_history(tmp_path, kind, entry):
    store = _book(tmp_path)
    with pytest.raises(LedgerError) as raised:
        commit_event(store, 1, {kind: [entry]})
    assert raised.value.code == "lifecycle_name_invalid"
    assert read_events(store) == []
    issues = _delta_issues({kind: [entry]}, {"now_card": {"name": "主角"}})
    assert any(issue["code"] == "lifecycle_name_invalid" for issue in issues)


def test_unknown_or_living_character_cannot_be_invented_as_revived():
    snapshot = copy.deepcopy(EMPTY_SNAPSHOT)
    snapshot["entities"]["主角"] = {"dead": False}
    assert apply_delta_conflicts(snapshot, {"revivals": ["主角", "外人"]}) == [
        {"code": "revival_not_dead", "who": "主角"},
        {"code": "revival_not_dead", "who": "外人"},
    ]


def test_same_chapter_death_then_revival_finishes_alive(tmp_path):
    store = _book(tmp_path, {1: {"deaths": ["掌柜"], "revivals": ["掌柜"]}})
    prose = "主角陪掌柜走进试炼场。掌柜心脏停止被众人确认身亡。掌柜随后重新活过来走向门口。"
    _submit(store, prose, {
        "named": ["主角", "掌柜"],
        "deaths": [{"who": "掌柜", "quote": "掌柜心脏停止被众人确认身亡"}],
        "revivals": ["掌柜"], "moves": [{"who": "掌柜", "to": "门口"}],
    })
    _commit_and_ack(store, prose)
    entity = load_snapshot(store)["entities"]["掌柜"]
    assert entity["dead"] is False
    assert entity["dead_chapter"] == entity["revived_chapter"] == 1
    assert verify_ledger(store)["consistent"]


def test_lifecycle_effects_are_rendered_and_enforced_not_silently_dropped(tmp_path):
    effects = {"revivals": ["掌柜"], "nonliving": [{"who": "祖师"}]}
    expected = collect_expected_delta([{"effects": effects}])
    assert expected["revivals"] == [{"who": "掌柜"}]
    assert expected["nonliving"] == [{"who": "祖师"}]
    assert {issue["kind"] for issue in _expected_delta_issues({}, {"expected_delta": expected})} == {"revivals", "nonliving"}
    assert _expected_delta_issues(effects, {"expected_delta": expected}) == []
    rendered = make_draft_view({"expected_delta": expected})["writing_brief"]
    assert "复活结果" in rendered and "非活人出场" in rendered
    assert plan_effects_key_issues([{"chapter": 2, "beats": [{"effects": {"revivals": [None]}}]}])
    assert plan_effects_key_issues([{"chapter": 2, "beats": [{"effects": {"revivals": ["掌柜", " 掌柜 "]}}]}])
    with pytest.raises(LedgerError):
        collect_expected_delta([{"effects": {"nonliving": [True]}}])


def test_planned_condition_requires_prose_evidence_in_actual_submission(tmp_path):
    condition = {"who": "主角", "kind": "伤势", "text": "左手断失", "irreversible": True}
    store = _book(tmp_path, {1: {"conditions": [condition]}})
    prose = "主角与掌柜一同参加试炼。主角在机关下失去了整只左手。掌柜扶住主角带他走出了场地。"
    advance_to_assembly(store, prose=prose)
    output = _output(store, {"named": ["主角", "掌柜"], "conditions": [condition]})
    rejected = submit_output(store, output)
    assert rejected["verdict"] == "fix_assembly", rejected
    assert any(issue["code"] == "expected_delta_quote_missing" for issue in rejected["violations"])
    assert store.read_head()["rewrite_count"] == 0
    output["state_delta"]["conditions"] = [{**condition, "quote": "主角在机关下失去了整只左手"}]
    assert submit_output(store, output)["verdict"] == "accepted"
    _commit_and_ack(store, prose)
    assert load_snapshot(store)["conditions"][0]["irreversible"] is True


def _pact_chapter(store):
    _submit(store, PACT_PROSE, {
        "named": ["主角", "掌柜"],
        "relations": [{"who": "主角", "target": "掌柜", "kind": "盟约", "status": "open", "quote": "两人在众人面前缔结了盟约"}],
        "hooks": [{"id": "old-promise", "text": "掌柜明天归还铜印", "status": "open", "due": 2, "quote": "掌柜承诺明天归还祖传铜印"}],
    })
    _commit_and_ack(store, PACT_PROSE)


@pytest.mark.parametrize("mutation", ["relation", "hook"])
@pytest.mark.parametrize("phase", ["assembly", "submitted"])
def test_governance_invalidates_inflight_inputs_and_blocks_old_commit(tmp_path, mutation, phase):
    store = _book(tmp_path)
    _pact_chapter(store)
    advance_to_assembly(store, prose=PACT_PROSE)
    output = _output(store, {"named": ["主角", "掌柜"]})
    previous = inputs_fingerprint(store, 2)
    if phase == "submitted":
        assert submit_output(store, output)["verdict"] == "accepted"
    if mutation == "relation":
        close_relation(store, who="主角", target="掌柜", kind_substring="盟约", reason="盟约已经解除", actor="test")
    else:
        defer_hook(store, hook_id="old-promise", new_due=3, reason="期限改为后日", actor="test")
    assert inputs_fingerprint(store, 2) != previous
    records = recall(store, before_chapter=2, people=["主角"])["records"]
    assert any(record["kind"] == "governance" for record in records)
    if mutation == "hook":
        assert any(record["kind"] == "governance" for record in recall(store, before_chapter=2, assets=["old-promise"])["records"])
    if phase == "assembly":
        rejected = submit_output(store, output)
        assert rejected["verdict"] == "stale_pack", rejected
    else:
        rejected = chapter_next(store)
        assert rejected["action"] == "blocked", rejected
        assert rejected["blocked"]["reason"] == "stale_inputs"
    assert len([event for event in read_events(store) if event.get("type") != "governance"]) == 1


@pytest.mark.parametrize("kind", ["deaths", "revivals"])
def test_lifecycle_commit_recovers_after_event_write_without_invalidating_own_inputs(tmp_path, monkeypatch, kind):
    store = _book(tmp_path)
    if kind == "revivals":
        _dead_chapter(store)
        prose = REVIVAL_PROSE
        delta = {"named": ["主角", "掌柜"], "revivals": ["掌柜"], "moves": [{"who": "掌柜", "to": "店铺"}]}
    else:
        prose = DEATH_PROSE
        delta = {"named": ["主角", "掌柜"], "deaths": [{"who": "掌柜", "quote": "掌柜被落石击中当场身亡"}]}
    _submit(store, prose, delta)
    chapter = int(store.read_head()["chapter"])
    before = inputs_fingerprint(store, chapter)
    def crash(*args, **kwargs):
        raise RuntimeError("crash after event and snapshot")
    patch_pipeline_name(monkeypatch, "atomic_text", crash)
    with pytest.raises(RuntimeError):
        chapter_next(store)
    monkeypatch.undo()
    assert inputs_fingerprint(store, chapter) == before
    recovered = chapter_next(store)
    assert recovered["action"] == "ack", recovered
    assert len([event for event in read_events(store) if event.get("type") != "governance"]) == chapter
    assert verify_ledger(store)["consistent"]
    assert replay_events(store) == load_snapshot(store)
