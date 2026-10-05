"""返工轮转（B 缺口）：plot_fix/rewrite 换的是底稿，不是从零重写。

实测教训（修真 B03）：plot_fix 清 staging 导致整稿重写 1.9M token；宿主手工
cp 备份后单句定点修 0.15M。本文件锁定流水线语义：rewrite 前旧稿轮转为
`.revK`（真源托管），返工 action 附 prior_draft_path 与定点修复指令；机检闸门
数量与严格度不变。
"""

from __future__ import annotations

from pathlib import Path

from novel_ledger_core.control.pipeline import chapter_next, submit_output
from novel_ledger_core.infra.store import BookStore
from tests.test_decoupled_pipeline import (
    _assemble_output,
    _drive_to_assembly,
    _polished,
    _project,
)


def _blocker(quote: str) -> list[dict]:
    return [{"code": "beat_missing", "severity": "BLOCKER", "hint": "拍点未兑现", "quote": quote}]


def test_plot_fix_rotates_prior_text_instead_of_destroying(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["rewrite_limit"] = 3  # 允许两轮 rewrite，验证 rev 号递增
    store.save_config(cfg)

    draft = "主角在市集，对掌柜说拒收改期。凭据还压在柜台那头，改天再来取。"
    _drive_to_assembly(store, draft)
    prose = _polished(store)

    out = _assemble_output(store, prose)
    out["plot_findings"] = _blocker("凭据还压在柜台那头")
    res = submit_output(store, out)

    assert res["verdict"] == "plot_fix"
    assert res["phase"] == "await_draft"
    rev1 = store.stage_revision_path(1, 1)
    assert rev1.exists(), "旧稿必须轮转保留（真源托管），不得销毁"
    assert rev1.read_text(encoding="utf-8") == prose
    assert res["preserved_draft_path"] == str(rev1)
    # 原路径清空（防旧文配新包的既定语义不变）
    assert not store.polished_text_path(1).exists()
    assert not store.draft_text_path(1).exists()

    nxt = chapter_next(store)
    assert nxt["action"] == "draft"
    assert nxt["prior_draft_path"] == str(rev1)
    assert "REWORK ROUND" in (nxt["prior_draft_hint"] or "")


def test_second_rewrite_rotates_to_rev2(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["rewrite_limit"] = 3
    store.save_config(cfg)

    _drive_to_assembly(store, "主角在市集，对掌柜说拒收改期。凭据还压在柜台那头，改天再来取。")
    prose = _polished(store)
    out = _assemble_output(store, prose)
    out["plot_findings"] = _blocker("凭据还压在柜台那头")
    assert submit_output(store, out)["verdict"] == "plot_fix"

    # 返工轮：定点修复后重走，再撞一次 BLOCKER → rev2
    nxt = chapter_next(store)
    fixed = prose.replace("凭据还压在柜台那头", "凭据当着掌柜的面取回")
    Path(nxt["draft_output_path"]).write_text(fixed, encoding="utf-8")
    from novel_ledger_core.control.pipeline import stage_draft_submit

    stage_draft_submit(store)
    from tests.decoupled_helpers import advance_to_assembly

    advance_to_assembly(store, prose=fixed)
    out2 = _assemble_output(store, fixed)
    out2["plot_findings"] = _blocker("凭据当着掌柜的面取回")
    res2 = submit_output(store, out2)
    assert res2["verdict"] == "plot_fix"
    rev2 = store.stage_revision_path(1, 2)
    assert rev2.exists() and rev2.read_text(encoding="utf-8") == fixed
    assert store.latest_stage_revision(1) == rev2
    assert chapter_next(store)["prior_draft_path"] == str(rev2)


def test_clean_chapter_has_no_revision_noise(tmp_path: Path):
    """正常一次过的章不产生轮转档——轮转只在返工路径触发。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    r1 = chapter_next(store)
    assert r1["action"] == "draft"
    assert r1["prior_draft_path"] is None and r1["prior_draft_hint"] is None
    _drive_to_assembly(store, "主角在市集，对掌柜说拒收改期。凭据还压在柜台那头，改天再来取。")
    assert store.latest_stage_revision(1) is None
