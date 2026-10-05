"""glossary 三层防线。

事故：向导把 config.glossary 填成「词条→释义」（医案/药痕/配额堂），
机检侧本有误用豁免（gates._glossary_entry_misused 跳过拦截、audit 点名），
但任务书渲染无条件照抄——写手被自己的门禁教唆避开正典核心词，
第 1 章三个核心术语 0 命中。三层防线：
  1. 渲染层：views 与机检同规，误用条目不进写作指令；
  2. 装配层：禁用串命中同包拍点/卷脊/世界脊柱 → 装配硬拒（写手不可能同时满足）；
  3. 源头层：hatch 拒绝「词条→释义」形状与主线/卷脊碰撞，坏配置不给出生机会。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.content.pack import assemble_pack
from novel_ledger_core.content.views import render_writing_brief
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.hatch import validate_hatch_manifest
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError
from tests.test_choice_hatch import guided_manifest

DEFINITION_ENTRY = {"医案": "记录症状、施治与结果的对照文本，本作中兼具证据与账目两重身份"}
LEGIT_ENTRY = {"验阵抽两成": "验阵取两成"}


# ---------- 层 1：任务书渲染与机检同规 ----------


def test_writer_brief_skips_definition_shaped_glossary():
    """值像释义句的条目不得进写作指令；合法条目照常渲染。"""
    pack = {"chapter": 1, "glossary": {**DEFINITION_ENTRY, **LEGIT_ENTRY}}
    brief = render_writing_brief(pack)
    assert "正文不得使用“医案”" not in brief, "释义式条目被渲染成了禁词指令"
    assert "正文不得使用“验阵抽两成”，必须统一写作“验阵取两成”" in brief


def test_writer_brief_skips_canon_term_banned_by_kb_title():
    """键命中知识库卡 id/title（正典词被当违禁词）同样跳过——与机检豁免完全同规。"""
    pack = {
        "chapter": 1,
        "glossary": {"药圃": "灵田"},
        "kb_slice": [{"id": "药圃", "kind": "world", "title": "药圃", "excerpt": "…"}],
    }
    brief = render_writing_brief(pack)
    assert "正文不得使用“药圃”" not in brief


def test_writer_brief_skips_empty_and_self_mapped_entries():
    pack = {"chapter": 1, "glossary": {"空值": "", "同词": "同词"}}
    brief = render_writing_brief(pack)
    assert "不得使用" not in brief


# ---------- 层 2：装配期自相矛盾硬拒 ----------


def _store_with_glossary(tmp_path: Path, *, glossary: dict, volume_spine: str, must: str) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": volume_spine,
        "chapters": [
            {
                "chapter": 1,
                "location": "药庐",
                "present": ["主角"],
                "beats": [{"id": "b1", "required": True, "text": f"主角处理{must}并收档", "must": must}],
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["glossary"] = glossary
    store.save_config(cfg)
    return store


def test_assembly_rejects_banned_term_required_by_volume_spine(tmp_path: Path):
    """禁用串撞卷脊（事故原形）：卷脊要求产出"第一份完整医案"，glossary 却禁"医案"。"""
    store = _store_with_glossary(
        tmp_path,
        glossary={"医案": "诊疗档案"},  # 形状合法（短、无标点），撞卷脊才是问题
        volume_spine="第一卷：主角完成第一份完整医案。",
        must="收档",
    )
    with pytest.raises(LedgerError) as exc:
        assemble_pack(store, 1)
    assert exc.value.code == "glossary_self_conflict"
    assert "医案" in str(exc.value.message) and "volume_spine" in str(exc.value.details)


def test_assembly_rejects_banned_term_required_by_beat_must(tmp_path: Path):
    """禁用串撞拍点 must：must 要求是正文字串、禁用串要求不出现——机检互斥，必然返工循环。"""
    store = _store_with_glossary(
        tmp_path,
        glossary={"医案": "诊疗档案"},
        volume_spine="第一卷。",
        must="医案",
    )
    with pytest.raises(LedgerError) as exc:
        assemble_pack(store, 1)
    assert exc.value.code == "glossary_self_conflict"
    assert "beat[b1].must" in str(exc.value.details) or "beat" in str(exc.value.details)


def test_assembly_accepts_legit_glossary(tmp_path: Path):
    """合法「旧写法→规范写法」不撞任何要求：照常装配并进 pack。"""
    store = _store_with_glossary(
        tmp_path,
        glossary=LEGIT_ENTRY,
        volume_spine="第一卷：主角完成第一份完整医案。",
        must="收档",
    )
    pack = assemble_pack(store, 1)
    assert pack["glossary"] == LEGIT_ENTRY
    brief = render_writing_brief(pack)
    assert "正文不得使用“验阵抽两成”" in brief
    assert "医案" in pack["volume_spine"]


# ---------- 层 3：hatch 源头校验 ----------


def test_hatch_rejects_definition_shaped_glossary(guided_manifest):
    manifest = dict(guided_manifest)
    manifest["glossary"] = DEFINITION_ENTRY
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"
    assert "definition" in str(exc.value.message)


def test_hatch_rejects_banned_term_required_by_mainline(guided_manifest):
    """guided 样例的 core_quest 是"打破修炼资源垄断…"：禁"资源垄断"就是禁主线本身。"""
    manifest = dict(guided_manifest)
    manifest["glossary"] = {"资源垄断": "资源寡占"}
    with pytest.raises(LedgerError) as exc:
        validate_hatch_manifest(manifest)
    assert exc.value.code == "invalid_hatch_v3"
    assert "资源垄断" in str(exc.value.message)


def test_hatch_accepts_legit_glossary(guided_manifest):
    manifest = dict(guided_manifest)
    manifest["glossary"] = LEGIT_ENTRY
    assert validate_hatch_manifest(manifest) is manifest
