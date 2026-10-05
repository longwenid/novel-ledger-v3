"""角色状态的快照配额按人物独立计算，长跑中互不挤占。"""

from __future__ import annotations

import pytest

from novel_ledger_core.infra.util import LedgerError
from novel_ledger_core.ledger.ledger import (
    CONDITIONS_PER_ENTITY,
    IRREVERSIBLE_CONDITIONS_PACK_MAX,
    EMPTY_SNAPSHOT,
    _evict_conditions,
    apply_event,
    select_conditions,
)


def _state(who: str, number: int, *, irreversible: bool = False, status: str = "active") -> dict:
    return {
        "who": who,
        "kind": "伤势",
        "text": f"{who}状态{number}",
        "irreversible": irreversible,
        "status": status,
    }


def test_each_character_keeps_its_own_condition_quota():
    conditions = [
        _state(who, number)
        for who in ("甲", "乙", "丙")
        for number in range(CONDITIONS_PER_ENTITY)
    ]

    kept = _evict_conditions(conditions)

    assert len(kept) == 3 * CONDITIONS_PER_ENTITY
    for who in ("甲", "乙", "丙"):
        assert [c["text"] for c in kept if c["who"] == who] == [
            f"{who}状态{number}" for number in range(CONDITIONS_PER_ENTITY)
        ]


def test_one_character_irreversible_overflow_does_not_evict_another_characters_state():
    conditions = [_state("甲", number, irreversible=True) for number in range(CONDITIONS_PER_ENTITY + 4)]
    conditions.extend(_state("乙", number) for number in range(4))

    kept = _evict_conditions(conditions)

    assert len([c for c in kept if c["who"] == "甲"]) == CONDITIONS_PER_ENTITY + 4
    assert [c["text"] for c in kept if c["who"] == "乙"] == [f"乙状态{n}" for n in range(4)]


def test_irreversible_conditions_do_not_use_up_their_characters_reversible_quota():
    conditions = [_state("甲", number, irreversible=True) for number in range(CONDITIONS_PER_ENTITY + 4)]
    conditions.extend(_state("甲", number + 100) for number in range(CONDITIONS_PER_ENTITY))

    kept = _evict_conditions(conditions)

    assert len(kept) == 2 * CONDITIONS_PER_ENTITY + 4
    assert [c["text"] for c in kept if not c.get("irreversible")] == [
        f"甲状态{number + 100}" for number in range(CONDITIONS_PER_ENTITY)
    ]


def test_present_characters_share_pack_capacity_fairly():
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot["conditions"] = [
        *(_state("乙", number) for number in range(2)),
        *(_state("甲", number) for number in range(8)),
    ]

    shown, omitted = select_conditions(None, names=["乙", "甲"], snapshot=snapshot, cap=4)

    assert [c["text"] for c in shown] == ["乙状态0", "乙状态1", "甲状态6", "甲状态7"]
    assert omitted == 6


def test_irreversible_conditions_do_not_use_pack_capacity():
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot["conditions"] = [
        _state("甲", 0, irreversible=True),
        _state("甲", 1),
        _state("甲", 2),
    ]

    shown, omitted = select_conditions(None, names=["甲"], snapshot=snapshot, cap=2)

    assert [c["text"] for c in shown] == ["甲状态0", "甲状态1", "甲状态2"]
    assert omitted == 0


def test_irreversible_overflow_stops_pack_without_dropping_permanent_costs():
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot["conditions"] = [
        _state("甲", number, irreversible=True)
        for number in range(IRREVERSIBLE_CONDITIONS_PACK_MAX + 1)
    ]

    with pytest.raises(LedgerError) as raised:
        select_conditions(None, names=["甲"], snapshot=snapshot)

    assert raised.value.code == "irreversible_conditions_overflow"
    assert raised.value.details["count"] == IRREVERSIBLE_CONDITIONS_PACK_MAX + 1
    assert raised.value.details["cap"] == IRREVERSIBLE_CONDITIONS_PACK_MAX
    assert "pack_caps.irreversible_conditions" in raised.value.message


def test_irreversible_overflow_has_explicit_reconciliation_path():
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot["conditions"] = [
        _state("甲", number, irreversible=True)
        for number in range(IRREVERSIBLE_CONDITIONS_PACK_MAX + 1)
    ]

    shown, omitted = select_conditions(
        None,
        names=["甲"],
        snapshot=snapshot,
        irreversible_cap=IRREVERSIBLE_CONDITIONS_PACK_MAX + 1,
    )
    assert len(shown) == IRREVERSIBLE_CONDITIONS_PACK_MAX + 1
    assert omitted == 0

    snapshot["conditions"][-1]["status"] = "closed"
    shown, omitted = select_conditions(None, names=["甲"], snapshot=snapshot)
    assert len(shown) == IRREVERSIBLE_CONDITIONS_PACK_MAX
    assert omitted == 0


def test_present_order_breaks_ties_when_pack_capacity_is_smaller_than_cast():
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot["conditions"] = [_state("甲", 0), _state("乙", 0), _state("丙", 0)]

    shown, omitted = select_conditions(None, names=["丙", "甲", "乙"], snapshot=snapshot, cap=2)

    assert [c["who"] for c in shown] == ["甲", "丙"]
    assert omitted == 1


def test_multiple_characters_conditions_survive_across_chapters_until_own_quota_is_full():
    snapshot = dict(EMPTY_SNAPSHOT)
    chapter = 0
    for number in range(10):
        for who in ("甲", "乙", "丙"):
            chapter += 1
            snapshot = apply_event(
                snapshot,
                {"chapter": chapter, "state_delta": {"conditions": [_state(who, number)]}},
            )

    for who in ("甲", "乙", "丙"):
        shown, omitted = select_conditions(None, names=[who], snapshot=snapshot)
        assert omitted == 0
        assert [c["text"] for c in shown] == [f"{who}状态{number}" for number in range(10)]

    for number in range(10, 20):
        chapter += 1
        snapshot = apply_event(
            snapshot,
            {"chapter": chapter, "state_delta": {"conditions": [_state("甲", number)]}},
        )

    assert [c["text"] for c in snapshot["conditions"] if c["who"] == "甲"] == [
        f"甲状态{number}" for number in range(20 - CONDITIONS_PER_ENTITY, 20)
    ]
    for who in ("乙", "丙"):
        shown, omitted = select_conditions(None, names=[who], snapshot=snapshot)
        assert omitted == 0
        assert [c["text"] for c in shown] == [f"{who}状态{number}" for number in range(10)]


def test_resolved_history_never_evicts_an_older_active_condition():
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot = apply_event(
        snapshot,
        {"chapter": 1, "state_delta": {"conditions": [_state("甲", 0), _state("乙", 0)]}},
    )
    chapter = 1
    for number in range(1, CONDITIONS_PER_ENTITY + 10):
        chapter += 1
        snapshot = apply_event(
            snapshot,
            {"chapter": chapter, "state_delta": {"conditions": [_state("甲", number)]}},
        )
        chapter += 1
        terminal = ("resolved", "healed", "closed")[(number - 1) % 3]
        snapshot = apply_event(
            snapshot,
            {"chapter": chapter, "state_delta": {"conditions": [_state("甲", number, status=terminal)]}},
        )

    shown, omitted = select_conditions(None, names=["甲", "乙"], snapshot=snapshot)
    assert [c["text"] for c in shown] == ["甲状态0", "乙状态0"]
    assert omitted == 0
    assert len([c for c in snapshot["conditions"] if c["who"] == "甲"]) <= CONDITIONS_PER_ENTITY


def test_updating_an_old_condition_refreshes_its_position_before_eviction():
    snapshot = dict(EMPTY_SNAPSHOT)
    snapshot = apply_event(
        snapshot,
        {
            "chapter": 1,
            "state_delta": {
                "conditions": [_state("甲", number) for number in range(CONDITIONS_PER_ENTITY)]
            },
        },
    )
    snapshot = apply_event(
        snapshot,
        {
            "chapter": 2,
            "state_delta": {"conditions": [{**_state("甲", 0), "value": 2}]},
        },
    )
    snapshot = apply_event(
        snapshot,
        {
            "chapter": 3,
            "state_delta": {"conditions": [_state("甲", CONDITIONS_PER_ENTITY)]},
        },
    )

    by_text = {condition["text"]: condition for condition in snapshot["conditions"]}
    assert len(by_text) == CONDITIONS_PER_ENTITY
    assert by_text["甲状态0"]["value"] == 2
    assert "甲状态1" not in by_text
