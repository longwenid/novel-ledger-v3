"""Knowledge-base fail-closed and deterministic-recall regression tests."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.content.extract import extract_cards, extract_to_file
from novel_ledger_core.content.pack import slice_kb
from novel_ledger_core.content.views import (
    make_assemble_view,
    make_draft_view,
    make_polish_view,
)
from novel_ledger_core.control.pipeline import _canon_source_drift, validate_plan
from novel_ledger_core.infra.store import DEFAULT_CONFIG, BookStore, validate_kb_cards
from novel_ledger_core.infra.util import LedgerError


def _assert_code(exc: pytest.ExceptionInfo[LedgerError], code: str) -> None:
    assert exc.value.code == code


def _card(ident: str, *, body: str = "御使飞剑需要先炼化剑器。", **extra):
    return {
        "id": ident,
        "kind": "power_system",
        "title": f"规则 {ident}",
        "body": body,
        "tags": ["仙门"],
        **extra,
    }


def test_malformed_structured_source_fails_without_overwriting_last_good_output(tmp_path: Path):
    source = tmp_path / "canon"
    source.mkdir()
    (source / "broken.json").write_text('{"cards": [', encoding="utf-8")
    out = tmp_path / "cards.json"
    previous = b'{"cards":[{"id":"last-good"}]}\n'
    out.write_bytes(previous)

    with pytest.raises(LedgerError) as exc:
        extract_to_file(source, out)

    _assert_code(exc, "invalid_kb_source")
    assert out.read_bytes() == previous


def test_zero_hard_cards_fails_without_overwriting_last_good_output(tmp_path: Path):
    source = tmp_path / "canon"
    source.mkdir()
    (source / "notes.md").write_text("# 随笔\n\n【软】只是一段氛围建议。\n", encoding="utf-8")
    out = tmp_path / "cards.json"
    previous = b'{"cards":[{"id":"last-good"}]}\n'
    out.write_bytes(previous)

    with pytest.raises(LedgerError) as exc:
        extract_to_file(source, out)

    _assert_code(exc, "empty_kb")
    assert out.read_bytes() == previous


@pytest.mark.parametrize(
    "cards",
    [
        ["not-a-card"],
        [{"id": "same", "body": "甲"}, {"id": "same", "body": "乙"}],
        [{"id": "x", "body": "规则", "aliases": "不是列表"}],
        [{"id": 7, "body": "规则"}],
        [{"id": "x", "body": "规则", "tags": [""]}],
    ],
)
def test_invalid_compiled_cards_raise_domain_error(cards):
    with pytest.raises(LedgerError) as exc:
        validate_kb_cards(cards)
    _assert_code(exc, "invalid_kb")


def test_canon_drift_uses_database_source_instead_of_export_directory(tmp_path: Path):
    store = BookStore(tmp_path / "project")
    store.ensure_layout()
    source = store.canon_dir / "规则.md"
    source.write_text("# 规则\n\n【硬】城门日落即闭。\n", encoding="utf-8")
    extract_to_file(store.canon_dir, store.kb_path)
    shutil.rmtree(store.canon_dir)

    drift = _canon_source_drift(store)
    assert drift["available"] is True
    assert drift["changed"] is False
    assert drift["missing_source"] is False

    source.unlink()  # Removing the authoritative canon is still detected.
    drift = _canon_source_drift(store)
    assert drift["changed"] is True
    assert drift["missing_source"] is True


def test_structured_aliases_and_priority_survive_compilation_and_retrieve(tmp_path: Path):
    source = tmp_path / "canon"
    source.mkdir()
    (source / "rules.json").write_text(
        json.dumps(
            {
                "cards": [
                    {
                        "id": "flight",
                        "title": "御剑规则",
                        "kind": "power_system",
                        "body": "御使飞剑需要先炼化剑器。",
                        "aliases": ["踩剑飞行"],
                        "always": True,
                        "priority": "core",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    card = extract_cards(source)["cards"][0]
    assert card["aliases"] == ["踩剑飞行"]
    assert card["always"] is True
    assert card["priority"] == "core"

    selected, meta = slice_kb(
        [card],
        location="",
        present=[],
        tags=[],
        beats=[{"text": "少年第一次踩剑飞行", "must": ""}],
        cap=4,
        excerpt_chars=400,
    )
    assert [item["id"] for item in selected] == [card["id"]]
    assert meta["matched_cards"] == 1


def test_explicit_refs_recall_semantically_important_card_without_lexical_overlap():
    card = _card("flight")
    args = {
        "location": "码头",
        "present": ["少年"],
        "tags": [],
        "beats": [{"text": "少年尝试新的出行办法", "must": ""}],
        "cap": 6,
        "excerpt_chars": 400,
    }
    selected, _ = slice_kb([card], **args)
    assert selected == []

    selected, meta = slice_kb([card], kb_refs=["flight"], **args)
    assert [item["id"] for item in selected] == ["flight"]
    assert meta["explicit_refs"] == ["flight"]
    assert meta["match_breakdown"]["explicit"] == 1


def test_explicit_refs_are_strict_and_can_use_more_than_default_four_slots():
    cards = [_card(f"c{i}") for i in range(1, 7)]
    refs = [card["id"] for card in cards[:5]]
    selected, meta = slice_kb(
        cards,
        location="",
        present=[],
        tags=[],
        beats=[{"text": "无关拍点", "must": ""}],
        cap=6,
        excerpt_chars=400,
        kb_refs=refs,
    )
    assert {item["id"] for item in selected} == set(refs)
    assert meta["effective_cap"] == 5

    with pytest.raises(LedgerError) as exc:
        slice_kb(
            cards,
            location="",
            present=[],
            tags=[],
            beats=[],
            cap=6,
            excerpt_chars=400,
            kb_refs=["missing"],
        )
    _assert_code(exc, "missing_kb_ref")


def test_plan_validation_rejects_missing_duplicate_refs_without_context_count_budget(tmp_path: Path):
    store = BookStore(tmp_path / "project")
    store.ensure_layout()
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["pack_caps"]["kb_slice"] = 1
    store.save_config(config)
    store.save_kb([_card("c1"), _card("c2")])
    store.save_plan(
        {
            "protagonist": "主角",
            "chapters": [
                {
                    "chapter": 1,
                    "location": "仙门",
                    "present": ["主角"],
                    "kb_refs": ["c1", "c1", "missing"],
                    "beats": [
                        {"text": "起", "must": ""},
                        {"text": "承", "must": ""},
                        {"text": "转", "must": ""},
                    ],
                }
            ],
        }
    )

    result = validate_plan(store)
    codes = {item["code"] for item in result["errors"]}
    assert {"duplicate_kb_refs", "missing_kb_refs"} <= codes
    assert "kb_refs_over_cap" not in codes


def test_slice_metadata_reports_omissions_and_only_reaches_content_views():
    cards = [_card(f"c{i}") for i in range(6)]
    selected, meta = slice_kb(
        cards,
        location="",
        present=[],
        tags=["仙门"],
        beats=[],
        cap=2,
        excerpt_chars=400,
    )
    assert len(selected) == 2
    assert meta["matched_cards"] == 6
    assert meta["omitted_cards"] == 4

    pack = {
        "chapter": 1,
        "kb_slice": selected,
        "kb_slice_meta": meta,
        "pack_hash": "sha256:test",
        "inputs_fingerprint": "sha256:inputs",
    }
    draft = make_draft_view(pack)
    assert "kb_slice_meta" not in draft
    assert "另有4张相关候选因工作包上限未注入" in draft["writing_brief"]
    assert "kb_slice_meta" not in make_polish_view(pack)
    assert "kb_slice_meta" not in make_assemble_view(pack)
