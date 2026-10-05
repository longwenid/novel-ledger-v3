"""场景地图（地点簿 locations）回归测试。

动机：worker 每章都是空上下文会话，空间属性（楼层/门牌/方位）没有注册表就只能靠
运气——ch1「局长办公室在四楼北边101」，ch20 写成「三楼505」。地点簿给空间口径
一个跨章记忆与三道防线：
1. 登记：state_delta.locations 申报（attributes + 逐字 quote），apply_event 入账；
2. 注入：本章相关地点卡进 draft 简报与 assemble 视图/简报（写前就知道口径）；
3. 对账：申报与注册表互斥 → location_attribute_conflict 拦下（replaces 显式翻修豁免）；
   已提交正文做注册表无关的漂移扫描（book reconcile 的 spatial 类）。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.content.gates import validate_write_output
from novel_ledger_core.content.numeric_audit import spatial_attribute_drift
from novel_ledger_core.content.views import make_assemble_view, render_assemble_brief, render_writing_brief
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.ledger.ledger import (
    apply_event,
    commit_event,
    load_snapshot,
    norm_spatial_value,
    verify_ledger,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import read_json
from tests.decoupled_helpers import advance_to_assembly, decoupled_submit


@pytest.fixture()
def store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：场景地图验证。",
        "chapters": [
            {
                "chapter": n,
                "volume": "vol-0001",
                "location": "局长办公室",
                "present": ["主角", "局长"],
                "beats": [{"id": f"b{n}", "required": True, "text": f"第{n}章对峙", "must": "对峙"}],
            }
            for n in range(1, 3)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "bookproj"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def _commit_location(store: BookStore, chapter: int, entry: dict) -> None:
    # 直连 commit 需要章文件与引文自洽（verify 的 missing_chapter_files/quote 检查）。
    path = store.chapter_md_path(chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            f"第{chapter}章。局长办公室在四楼北边101号。韩局的办公室搬到了五楼。\n",
            encoding="utf-8",
        )
    commit_event(store, chapter=chapter, state_delta={"named": ["主角"], "locations": [entry]})


def test_norm_spatial_value_normalizes_chinese_numerals() -> None:
    assert norm_spatial_value("四楼") == norm_spatial_value("4楼") == "4楼"
    assert norm_spatial_value("十二层") == "12层"
    assert norm_spatial_value("北边 101") == "北边101"


def test_locations_registry_roundtrip_and_alias_merge(store: BookStore) -> None:
    _commit_location(
        store,
        1,
        {"id": "局长办公室", "attributes": {"楼层": "四楼", "门牌": "101号", "方位": "北"},
         "quote": "局长办公室在四楼北边101号"},
    )
    _commit_location(
        store,
        2,
        {"id": "局长办公室", "name": "韩局的办公室", "aliases": ["局长室"],
         "attributes": {"楼层": "五楼"}, "replaces": ["楼层"], "quote": "韩局的办公室搬到了五楼"},
    )
    snap = load_snapshot(store)
    locs = snap["locations"]
    assert len(locs) == 1
    merged = locs[0]
    assert merged["name"] == "韩局的办公室"
    assert "局长办公室" in merged["aliases"] and "局长室" in merged["aliases"]
    assert merged["attributes"]["楼层"]["value"] == "五楼" and merged["attributes"]["楼层"]["chapter"] == 2
    assert merged["attributes"]["门牌"]["value"] == "101号" and merged["attributes"]["门牌"]["chapter"] == 1
    assert verify_ledger(store)["consistent"] is True


def _drive_to_assembly_and_submit(store: BookStore, output: dict, prose: str, chapter: int):
    from novel_ledger_core.control.pipeline import ack_read, chapter_next

    result = decoupled_submit(store, output, prose=prose, chapter=chapter)
    return result, ack_read, chapter_next


def test_submit_blocks_conflicting_attribute_and_replaces_escapes(store: BookStore) -> None:
    from novel_ledger_core.control.pipeline import ack_read, chapter_next, submit_output

    # 第 1 章建档：申报四楼，提交通过、commit、ack，推进到第 2 章。
    prose1 = "主角第一次走进局长办公室，局长指了指对面的椅子，两人在四楼对峙。"
    advance_to_assembly(store, prose=prose1, chapter=1)
    out1 = {
        "l1_summary": "主角在局长办公室对峙。",
        "state_delta": {"named": ["主角"], "locations": [
            {"id": "局长办公室", "attributes": {"楼层": "四楼", "门牌": "101号"},
             "quote": "两人在四楼对峙"}]},
        "memory": {"voice_concepts": []},
        "pack_hash": read_json(store.current_pack_path)["pack_hash"],
        "beats_hit": ["b1"],
        "plot_findings": [],
    }
    assert submit_output(store, out1)["verdict"] == "accepted"
    assert chapter_next(store)["action"] == "ack"
    ack_read(store, quotes=["两人在四楼对峙", "第一次走进局长办公室", "指了指对面的椅子"])
    assert chapter_next(store)["action"] == "draft"
    # 第 2 章正文写漂成三楼且未声明翻修：拦截并给两条出路。
    prose2 = "主角推开局长办公室的门，三楼的风从走廊灌进来，一切照旧对峙。"
    advance_to_assembly(store, prose=prose2, chapter=2)
    base2 = {
        "l1_summary": "主角再到局长办公室对峙。",
        "state_delta": {"named": ["主角"]},
        "memory": {"voice_concepts": []},
        "pack_hash": read_json(store.current_pack_path)["pack_hash"],
        "beats_hit": ["b2"],
        "plot_findings": [],
    }
    conflict = submit_output(
        store,
        {**base2, "state_delta": {"named": ["主角"], "locations": [
            {"id": "局长办公室", "attributes": {"楼层": "三楼"}, "quote": "三楼的风从走廊灌进来"}]}},
    )
    assert conflict["verdict"] == "fix_assembly"
    assert any(v.get("code") == "location_attribute_conflict" for v in conflict.get("violations", []))
    # replaces 显式留痕后放行：注册表按最新声明覆盖。
    fixed = submit_output(
        store,
        {**base2, "state_delta": {"named": ["主角"], "locations": [
            {"id": "局长办公室", "attributes": {"楼层": "三楼"}, "replaces": ["楼层"],
             "quote": "三楼的风从走廊灌进来"}]}},
    )
    assert fixed["verdict"] == "accepted"
    assert chapter_next(store)["action"] == "ack"  # commit 落账（事件在 commit 时入账本）
    snap = load_snapshot(store)
    assert snap["locations"][0]["attributes"]["楼层"]["value"] == "三楼"


def test_location_declaration_requires_verbatim_quote(store: BookStore) -> None:
    action = advance_to_assembly(store, chapter=1)
    pack = json.loads(Path(action["assemble_pack_path"]).read_text(encoding="utf-8"))
    output = {
        "l1_summary": "主角在局长办公室对峙。",
        "state_delta": {"named": ["主角"], "locations": [
            {"id": "局长办公室", "attributes": {"楼层": "四楼"}, "quote": "这句不在正文里凭空出现"}]},
        "memory": {"voice_concepts": []},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1"],
        "plot_findings": [],
    }
    issues = validate_write_output(output, pack, prose="主角在局长办公室对峙。")
    assert any(i["code"] == "delta_quote_not_in_prose" and i["kind"] == "locations" for i in issues)


def test_pack_and_briefs_carry_location_cards(store: BookStore) -> None:
    _commit_location(
        store, 1,
        {"id": "局长办公室", "attributes": {"楼层": "四楼", "门牌": "101号"}, "quote": "局长办公室在四楼北边101号"},
    )
    action = advance_to_assembly(store, chapter=1)
    pack = json.loads(Path(action["assemble_pack_path"]).read_text(encoding="utf-8"))
    cards = pack.get("locations") or []
    assert cards and cards[0]["name"] == "局长办公室"
    assert cards[0]["attributes"] == {"楼层": "四楼", "门牌": "101号"}
    brief = Path(action["assemble_brief_path"]).read_text(encoding="utf-8")
    assert "局长办公室" in brief and "四楼" in brief and "replaces" in brief
    # draft 简报同样带场景口径：写者在动笔前就知道楼层门牌。
    draft_view = json.loads(Path(store.pack_dir / "draft-0001.json").read_text(encoding="utf-8"))
    assert "局长办公室" in draft_view["writing_brief"] and "四楼" in draft_view["writing_brief"]
    assert "locations" not in draft_view, "draft 视图走简报渲染，不重复携带结构化 locations"


def test_spatial_drift_scan_catches_legacy_books_without_registry() -> None:
    chapters = [
        (1, "局长办公室在四楼北边101号，李有粮推门进去。"),
        (20, "他爬上三楼，505室的局长办公室门开着。"),
    ]
    hits = spatial_attribute_drift(chapters, ["局长办公室"])
    by_attr = {h["attribute"]: h["values"] for h in hits}
    assert by_attr.get("楼层") == ["3", "4"]
    assert by_attr.get("门牌") == ["101", "505"]
    assert spatial_attribute_drift(chapters, []) == []
