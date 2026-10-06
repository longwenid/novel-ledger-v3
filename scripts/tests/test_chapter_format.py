"""成稿格式与体例闸门：重复章头、错章号、写作期残留标记、引号体例。

这些是**确定性**缺陷（小说正文不会自然出现「第三场」或同章两遍相同章头），
所以直接挂在提交闸上，判 `fix_draft` 回正文返工；判据本身在
`test_consistency_neutral.py`，本文件只测它在流水线里的落点与分流。
"""

from __future__ import annotations

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    chapter_next,
    stage_draft_submit,
    stage_polish_submit,
)
from novel_ledger_core.infra.store import BookStore

from tests.decoupled_helpers import make_plan, write_plan

# 干净的场景正文：字数足够过 word_band，顺序与标点都正常。
_BODY = (
    "他推门进来，屋里没人，桌上的册子摊开着，边角卷起一截。"
    "他把册子合上，又抬手把窗栓拨正，回头看了一眼门框上的划痕。"
)
# 干净正文：章头正确、引号成对、无标记。
_CLEAN = "第1章 起局\n\n" + _BODY + "\n"
# 多稿缝合的典型残留：重复章头 + 分场标记 + 平台话术。
_DIRTY = "第1章 起局\n\n" + _BODY + "\n第1章 起局\n第二场 夜审\n（本章完）\n"


def _store(tmp_path, *, polish: str = "off") -> BookStore:
    project = tmp_path / "proj"
    project.mkdir()
    init_project(project, plan_path=write_plan(tmp_path, make_plan(1)), protagonist="主角")
    store = BookStore(project)
    cfg = store.load_config()
    cfg.update(polish=polish, word_band={"min": 20, "max": 8000})
    store.save_config(cfg)
    return store


def _submit_draft(store: BookStore, prose: str) -> dict:
    action = chapter_next(store)
    assert action["action"] == "draft"
    from pathlib import Path

    Path(action["draft_output_path"]).write_text(prose, encoding="utf-8")
    return stage_draft_submit(store)


def test_dirty_draft_is_rejected_at_the_draft_stage(tmp_path):
    store = _store(tmp_path)
    result = _submit_draft(store, _DIRTY)
    assert result["verdict"] == "draft_rejected"
    codes = {item["code"] for item in result["violations"]}
    assert {"duplicated_chapter_header", "scene_break_marker", "story_marker_residue"} <= codes
    assert store.read_head()["phase"] == "await_draft"


def test_clean_draft_passes_the_format_gate(tmp_path):
    store = _store(tmp_path)
    result = _submit_draft(store, _CLEAN)
    assert result["verdict"] == "draft_accepted"
    assert store.read_head()["phase"] == "await_assembly"


def test_wrong_chapter_number_is_rejected(tmp_path):
    store = _store(tmp_path)
    result = _submit_draft(store, "第7章 起局\n\n" + _BODY + "\n")
    assert result["verdict"] == "draft_rejected", result
    assert [item["code"] for item in result["violations"]] == ["chapter_header_mismatch"], result
    assert result["violations"][0]["header_number"] == 7


def test_mixed_quote_systems_are_rejected(tmp_path):
    store = _store(tmp_path)
    prose = "第1章 起局\n\n" + _BODY + '他说：「走吧。」隔了一会儿又说："行。"\n'
    result = _submit_draft(store, prose)
    assert result["verdict"] == "draft_rejected", result
    assert [item["code"] for item in result["violations"]] == ["quote_style_mixed"], result


def test_project_can_lock_a_quote_system(tmp_path):
    store = _store(tmp_path)
    cfg = store.load_config()
    cfg["quote_style"] = "cn_corner"
    store.save_config(cfg)
    assert _submit_draft(store, "第1章 起局\n\n" + _BODY + "他说：「走吧。」\n")["verdict"] == "draft_accepted"


def test_polish_stage_rejects_format_defects_it_introduced(tmp_path):
    """草稿干净、润色阶段改坏了引号 → 只回 polish 重润，不整链重写。"""
    store = _store(tmp_path, polish="on")
    assert _submit_draft(store, _CLEAN)["verdict"] == "draft_accepted"
    action = chapter_next(store)
    assert action["action"] == "polish"
    from pathlib import Path

    Path(action["polished_output_path"]).write_text(
        "第1章 起局\n\n" + _BODY + '他说：「走吧。」隔了一会儿又说："行。"\n',
        encoding="utf-8",
    )
    result = stage_polish_submit(store)
    assert result["verdict"] == "polish_rejected", result
    assert {item["code"] for item in result["violations"]} == {"quote_style_mixed"}, result
    assert result["polished_kept"] is True
    assert store.read_head()["phase"] == "await_polish"


# —— v2 锚定纪律：fixed 取值域与时间锚在提交点硬拒 ——

def _declare(store: BookStore, key: str, spec: dict) -> None:
    cfg = store.load_config()
    cfg["fact_keys"] = {**(cfg.get("fact_keys") or {}), key: spec}
    store.save_config(cfg)


def test_draft_with_fixed_fact_conflict_is_rejected(tmp_path):
    """声明「十一岁」后草稿写十六 → draft-submit 当场拒，不养到检查点。"""
    store = _store(tmp_path)
    _declare(store, "k_age", {"kind": "number", "observe": "岁", "canonical": 11})
    result = _submit_draft(store, "第1章 起局\n\n" + _BODY + "他十六岁那年进了山。\n")
    assert result["verdict"] == "draft_rejected", result
    assert [item["code"] for item in result["violations"]] == ["fact_value_conflict"], result
    assert result["violations"][0]["observed"] == 16
    assert store.read_head()["phase"] == "await_draft"


def test_eventful_fact_conflict_does_not_block_draft(tmp_path):
    """eventful 声明的合法变动只记录，不挡线（回执带 fact_eventful_count）。"""
    store = _store(tmp_path)
    _declare(store, "k_post", {"kind": "set", "observe": "职务", "canonical": "股长",
                               "allow": ["副股长"], "policy": "eventful"})
    result = _submit_draft(store, "第1章 起局\n\n" + _BODY + "他的职务是副股长。\n")
    assert result["verdict"] == "draft_accepted", result
    assert result.get("fact_eventful_count") == 1, result


def test_draft_with_age_anchor_conflict_is_rejected(tmp_path):
    """出生年+故事年声明后，年龄是算出来的：算术矛盾当场拒。"""
    store = _store(tmp_path)
    cfg = store.load_config()
    cfg["timeline_anchors"] = {"chapter_years": {"1": 1993}, "birth_years": {"张甲": 1987}}
    store.save_config(cfg)
    result = _submit_draft(store, "第1章 起局\n\n" + _BODY + "张甲十六岁那年进了城。\n")
    assert result["verdict"] == "draft_rejected", result
    assert [item["code"] for item in result["violations"]] == ["age_anchor_conflict"], result
    assert result["violations"][0]["canonical"] == 6


def test_draft_matching_all_anchors_passes(tmp_path):
    store = _store(tmp_path)
    _declare(store, "k_age", {"kind": "number", "observe": "岁", "canonical": 11})
    cfg = store.load_config()
    cfg["timeline_anchors"] = {"chapter_years": {"1": 1993}, "birth_years": {"张甲": 1982}}
    store.save_config(cfg)
    # 1993 − 1982 = 11，与 fact_keys 声明一致
    result = _submit_draft(store, "第1章 起局\n\n" + _BODY + "张甲十一岁那年进了城。\n")
    assert result["verdict"] == "draft_accepted", result
