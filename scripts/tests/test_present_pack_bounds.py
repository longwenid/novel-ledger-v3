"""在场者保真与关系切片的上下文边界。"""

import json

import pytest

from novel_ledger_core.content.pack import _present_cards, _present_names, assemble_pack
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json


def test_present_cast_overflow_fails_instead_of_dropping_actors():
    cast = [f"角色{i}" for i in range(15)]
    assert len(_present_names({"present": cast}, "主角")) == 16

    with pytest.raises(LedgerError) as exc:
        _present_names({"present": [*cast, "第十六个配角"]}, "主角")
    assert exc.value.code == "present_cast_overflow"


def test_present_card_relationships_use_bounded_recent_slice():
    relations = [
        {
            "who": "主角",
            "target": "掌柜",
            "kind": f"关系{i}",
            "status": "open",
            "updated_chapter": i,
        }
        for i in range(1, 101)
    ]
    snapshot = {"entities": {"掌柜": {}}, "relations": relations}
    card = _present_cards(None, ["主角", "掌柜"], "主角", snapshot, chapter=101)[0]

    assert card["relations_with_protagonist"] == [f"关系{i}" for i in range(100, 94, -1)]
    assert "关系100" in card["rendered"]
    assert "关系1、" not in card["rendered"]


def test_present_card_uses_the_same_relation_slice_as_pack():
    snapshot = {"entities": {"掌柜": {}}, "relations": [
        {"who": "主角", "target": "掌柜", "kind": "旧交", "status": "open"},
        {"who": "主角", "target": "掌柜", "kind": "新仇", "status": "open"},
    ]}
    selected = [{"who": "主角", "target": "掌柜", "kind": "新仇", "status": "open"}]

    card = _present_cards(
        None, ["主角", "掌柜"], "主角", snapshot,
        selected_relations=selected,
    )[0]
    assert card["relations_with_protagonist"] == ["新仇"]


def test_pack_reports_omitted_relations_and_cards_share_slice(tmp_path):
    plan = {
        "title": "关系切片",
        "protagonist": "主角",
        "volume_spine": "主角与掌柜对质。",
        "chapters": [{
            "chapter": 1, "location": "市集", "present": ["主角", "掌柜"],
            "beats": [{"id": "b1", "required": True, "text": "主角与掌柜对质", "must": "对质"}],
        }],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角")
    store = BookStore(project)
    snapshot = read_json(store.snapshot_path)
    snapshot["relations"] = [
        {"who": "主角", "target": "掌柜", "kind": f"关系{i}", "status": "open", "updated_chapter": i}
        for i in range(1, 101)
    ]
    atomic_json(store.snapshot_path, snapshot)

    pack = assemble_pack(store, 1)
    expected = [item["kind"] for item in pack["relations"]]
    assert len(expected) == 6
    assert pack["omitted"]["relations"] == 94
    assert pack["present_cards"][0]["relations_with_protagonist"] == expected
    continuity = next(item for item in pack["character_continuity"] if item["name"] == "掌柜")
    assert continuity["relations_with_protagonist"] == expected
