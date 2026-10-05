"""只写作模式（config.polish=off）：draft-submit 直通组装，草稿即终稿。

润色相位与文风机检退出写链；字数带（章合同）仍在 draft-submit 收口。
"""

from __future__ import annotations

from pathlib import Path

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    chapter_next,
    stage_draft_submit,
    submit_output,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import read_json

from tests.decoupled_helpers import FALLBACK_PROSE, make_plan, write_plan


def _project(tmp_path: Path, polish: str | None = "off") -> Path:
    plan_path = write_plan(tmp_path, make_plan(1))
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    if polish is not None:
        cfg["polish"] = polish
    store.save_config(cfg)
    return proj


def _output(store: BookStore, prose: str, chapter: int = 1) -> dict:
    view = read_json(store.assemble_pack_path(chapter))
    return {
        "prose": prose,
        "l1_summary": "主角在市集拒收改期。",
        "state_delta": {
            "moves": [{"who": "主角", "to": "市集"}],
            "facts": [{"who": "主角", "text": "主角拒收改期。"}],
            "debts": [],
            "hooks": [],
            "relations": [],
            "named": ["主角", "掌柜"],
            "new_names": [],
            "deaths": [],
        },
        "memory": {"voice_concepts": []},
        "pack_hash": view["pack_hash"],
        "beats_hit": ["b1"],
    }


def test_draft_only_mode_skips_polish_and_accepts_draft_as_final(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    store = BookStore(proj)

    r1 = chapter_next(store)
    assert r1["action"] == "draft" and r1["phase"] == "await_draft"
    Path(r1["draft_output_path"]).write_text(FALLBACK_PROSE, encoding="utf-8")

    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_accepted"
    assert result["phase"] == "await_assembly"
    # 草稿即终稿：polished 落位为草稿原文，下游组装/入账链路零改动
    polished = store.polished_text_path(1).read_text(encoding="utf-8")
    assert polished == FALLBACK_PROSE

    r2 = chapter_next(store)
    assert r2["action"] == "assemble" and r2["phase"] == "await_assembly"
    accepted = submit_output(store, _output(store, polished.rstrip("\n")))
    assert accepted["verdict"] == "accepted"


def test_draft_only_mode_keeps_word_band_contract(tmp_path: Path) -> None:
    """只写作不等于只写几个字：章字数合同仍在最便宜的 draft 收口。"""
    proj = _project(tmp_path)
    store = BookStore(proj)

    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text("主角拒收。", encoding="utf-8")
    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_rejected"
    assert store.read_head()["phase"] == "await_draft"


def test_draft_only_mode_ignores_style_hard_lines(tmp_path: Path) -> None:
    """文风机检退出写链：硬红线话术在 draft-only 下照常入账（自查走 precheck）。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True  # 即使机检开着，draft-only 也不拦线
    store.save_config(cfg)

    r1 = chapter_next(store)
    prose = FALLBACK_PROSE + "值得注意的是，这买卖还没完。"
    Path(r1["draft_output_path"]).write_text(prose, encoding="utf-8")
    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_accepted"
    assert result["phase"] == "await_assembly"


def test_skill_default_is_draft_only(tmp_path: Path) -> None:
    """2026-10-01 起 skill 默认只写作：不设 polish 时 draft 直通组装，草稿即终稿。"""
    proj = _project(tmp_path, polish=None)
    store = BookStore(proj)

    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(FALLBACK_PROSE, encoding="utf-8")
    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_accepted"
    assert result["phase"] == "await_assembly"
    assert store.polished_text_path(1).exists()  # 草稿原文落位 polished 路径
    assert chapter_next(store)["action"] == "assemble"


def test_polish_opt_in_routes_through_polish(tmp_path: Path) -> None:
    """显式 polish=on 仍走润色泳线：draft → await_polish，polished 不提前落位。"""
    proj = _project(tmp_path, polish="on")
    store = BookStore(proj)

    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(FALLBACK_PROSE, encoding="utf-8")
    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_accepted"
    assert result["phase"] == "await_polish"
    assert not store.polished_text_path(1).exists()
    assert chapter_next(store)["action"] == "polish"
