from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.content.hierarchical_memory import (
    load_hierarchical_memory,
    memory_for_pack,
    rebuild_hierarchical_memory,
    update_hierarchical_memory,
)
from novel_ledger_core.content.pack import assemble_pack
from novel_ledger_core.content.views import make_assemble_view, make_draft_view, make_polish_view
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, canonical_json


def _chapter(number: int, *, volume: int = 1, phase: str = "") -> dict:
    item = {
        "chapter": number,
        "volume": volume,
        "location": "城中",
        "present": ["主角"],
        "beats": [
            {"id": f"b{number}", "required": True, "text": f"主角推进第{number}章事件", "must": "推进"}
        ],
    }
    if phase:
        item["phase_id"] = phase
    return item


def _store(tmp_path: Path, chapters: list[dict], *, phases: list[dict] | None = None) -> BookStore:
    plan = {
        "title": "长篇压力书",
        "protagonist": "主角",
        "world_spine": "所有力量增长都必须付出代价。",
        "volume_spine": "主角沿四卷主线追查旧案。",
        "chapters": chapters,
    }
    if phases is not None:
        plan["phases"] = phases
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "project"
    init_project(project, plan_path=plan_path, protagonist="主角")
    return BookStore(project)


def _write_l1(store: BookStore, chapter: int, text: str) -> None:
    atomic_json(store.summary_path(chapter), {"chapter": chapter, "l1_summary": text})


def test_hierarchy_rolls_chapters_into_phase_volume_and_book_layers(tmp_path: Path):
    chapters = [
        _chapter(
            number,
            volume=1 if number <= 30 else 2,
            phase="phase-pursuit" if number <= 10 else ("phase-siege-1" if number <= 30 else "phase-siege-2"),
        )
        for number in range(1, 41)
    ]
    store = _store(tmp_path, chapters, phases=[
        {"id": "phase-pursuit", "name": "追索"},
        {"id": "phase-siege-1", "name": "围城"},
        {"id": "phase-siege-2", "name": "围城"},
    ])
    initial = load_hierarchical_memory(store)
    assert initial["updated_chapter"] == 0

    for chapter in range(1, 36):
        summary = f"主角在第{chapter}章取得线索，并承担第{chapter}次代价。"
        _write_l1(store, chapter, summary)
        update_hierarchical_memory(store, chapter, summary)

    hierarchy = load_hierarchical_memory(store)
    assert hierarchy["updated_chapter"] == 35
    assert len(hierarchy["volumes"]) == 2
    assert [phase["label"] for phase in hierarchy["phases"]] == ["追索", "围城", "围城"]
    assert hierarchy["book"]["chapter_count"] == 35

    layers = memory_for_pack(store, 36)
    assert layers["phase_summary"]["relation"] == "current"
    assert layers["volume_summary"]["id"] == "vol-0002"
    assert "世界脊柱" in layers["book_spine"]["summary"]
    assert all(f"第{number}章取得线索" in layers["phase_summary"]["summary"] for number in range(31, 36))
    assert "第1章取得线索" not in layers["volume_summary"]["summary"]
    assert layers["volume_summary"]["source_path"] == str(store.hierarchical_memory_path)

    pack = assemble_pack(store, 36)
    assert pack["memory_layers"] == layers
    draft = make_draft_view(pack)
    assert "memory_layers" not in draft
    assert layers["phase_summary"]["summary"] in draft["writing_brief"]
    assert layers["volume_summary"]["summary"] in draft["writing_brief"]
    assert layers["book_spine"]["summary"] in draft["writing_brief"]
    assert "memory_layers" not in make_polish_view(pack)
    assert "memory_layers" not in make_assemble_view(pack)


def test_rebuild_after_rollback_removes_discarded_chapter(tmp_path: Path):
    store = _store(tmp_path, [_chapter(number) for number in range(1, 5)])
    for chapter in range(1, 5):
        _write_l1(store, chapter, f"第{chapter}章留下事实{chapter}。")
    rebuild_hierarchical_memory(store, 4)
    assert load_hierarchical_memory(store)["updated_chapter"] == 4

    store.summary_path(4).unlink()
    rebuilt = rebuild_hierarchical_memory(store, 3)
    assert rebuilt["updated_chapter"] == 3
    assert "事实4" not in canonical_json(rebuilt).decode("utf-8")


@pytest.mark.slow
def test_one_thousand_chapter_hierarchy_loads_current_phase_without_model_calls(tmp_path: Path):
    """1000 章只写确定性 L1 JSON 并本地滚层，不触发模型、usage 或正文生成。"""
    chapters = [
        _chapter(number, volume=(number - 1) // 250 + 1)
        for number in range(1, 1001)
    ]
    store = _store(tmp_path, chapters)
    for chapter in range(1, 1001):
        _write_l1(store, chapter, f"第{chapter}章推进旧案线索，状态锚点为编号{chapter}。")

    hierarchy = rebuild_hierarchical_memory(store, 1000)
    assert hierarchy["updated_chapter"] == 1000
    assert hierarchy["book"]["chapter_count"] == 1000
    assert len(hierarchy["volumes"]) == 4
    assert len(hierarchy["phases"]) == 52  # 每卷 250 章，按 20 章自动分成 13 段
    stored = canonical_json(hierarchy).decode("utf-8")
    assert "状态锚点为编号1。" in stored and "状态锚点为编号1000。" in stored
    assert not store.usage_path.exists()

    layers = memory_for_pack(store, 1000)
    selected = canonical_json(layers).decode("utf-8")
    assert "状态锚点为编号999。" in selected
    assert "状态锚点为编号1。" not in selected and "状态锚点为编号751。" not in selected
    assert "状态锚点为编号1000。" not in selected  # no own-chapter input
    assert len(layers["book_spine"]["volume_index"]) == 4
    assert layers["volume_summary"]["loaded_phase_ids"] == [layers["phase_summary"]["id"]]
