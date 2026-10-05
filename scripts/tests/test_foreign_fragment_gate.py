"""外文残片闸（foreign_fragment）：长拉丁字母串混入中文正文的确定性机检。

实测缺陷：condensedcondensed 这类模型输出事故曾只靠 draft prompt 自查项兜底，
自查是概率性的；残片是确定性的，直接机检。与字数带同 philosophy——最便宜的
draft-submit 点就地拦，零配额；组装 submit 复检；恢复路由按单点零模型处理。
"""

from __future__ import annotations

from pathlib import Path

from novel_ledger_core.content.gates import foreign_fragment_issues
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import _rewrite_recovery, chapter_next, stage_draft_submit
from novel_ledger_core.infra.store import BookStore

from tests.decoupled_helpers import FALLBACK_PROSE, make_plan, write_plan


def _project(tmp_path: Path, *, foreign_fragment_gate: str | None = None) -> Path:
    plan_path = write_plan(tmp_path, make_plan(1))
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    if foreign_fragment_gate is not None:
        cfg["foreign_fragment_gate"] = foreign_fragment_gate
    store.save_config(cfg)
    return proj


def test_detects_garbled_latin_run():
    issues = foreign_fragment_issues("他低声念完，condensedcondensed然后转身离开市集。")
    assert len(issues) == 1
    issue = issues[0]
    assert issue["code"] == "foreign_fragment"
    assert issue["hits"] and issue["hits"][0]["run"] == "condensedcondensed"
    assert "condensedcondensed" in issue["hits"][0]["excerpt"]


def test_allows_short_occasional_latin():
    assert foreign_fragment_issues("他调出AI面板看了一眼，又抬手测量距离。") == []
    assert foreign_fragment_issues("墙上有GPS标记，还有A型支架。") == []
    assert foreign_fragment_issues(FALLBACK_PROSE) == []


def test_flags_repeated_medium_runs():
    prose = "第一行写着AlphaBeta，第二行写着GammaDelta，第三行写着EpsilonZeta。"
    issues = foreign_fragment_issues(prose)
    assert len(issues) == 1 and issues[0]["count"] >= 3


def test_draft_submit_rejects_fragment_at_cheapest_point(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    action = chapter_next(store)
    draft_path = Path(action["draft_output_path"])
    draft_path.write_text(FALLBACK_PROSE + "\ncondensedcondensed他又看了一遍。", encoding="utf-8")

    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_rejected"
    assert store.read_head()["phase"] == "await_draft"
    codes = {v.get("code") for v in result["violations"]}
    assert "foreign_fragment" in codes
    assert draft_path.exists(), "staging 草稿保留，原位修复后重提"

    draft_path.write_text(FALLBACK_PROSE + "\n他又把那行字看了一遍。", encoding="utf-8")
    accepted = stage_draft_submit(store)
    assert accepted["verdict"] == "draft_accepted"


def test_gate_allow_exemption(tmp_path: Path):
    proj = _project(tmp_path, foreign_fragment_gate="allow")
    store = BookStore(proj)
    action = chapter_next(store)
    Path(action["draft_output_path"]).write_text(
        FALLBACK_PROSE + "\ncondensedcondensed他又看了一遍。", encoding="utf-8"
    )
    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_accepted"


def test_recovery_routes_fragment_as_one_point_prose():
    recovery = _rewrite_recovery([{"code": "foreign_fragment", "hits": []}])
    assert recovery["route"] == "one_point_prose"
    assert "rework-patch" in recovery["steps"][0]
