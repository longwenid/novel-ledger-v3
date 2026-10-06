"""扩纲跨度契约（plan_extend_span）回归。

实测：水位提示不带跨度，worker 每轮贴线补 ~5 章，63 章烧 13 轮扩纲 ≈109 万 token
（每轮的钱≈写一章）。修复：config `plan_extend_span`（默认 20；"volume"=按卷合同
chapters_budget 签满当前卷，已签满则整签下一卷，无卷合同回退 20），extend_plan 回执
与 plan worker 简报以 `extend_through_ch` 显式下达覆盖契约；协议卡规定 >20 章时
同一 worker 会话内拆 ≤20 章候选批连续 select-batch → plan extend（省 spawn 与重读，
per-batch 机检不变）。
"""

from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    ack_read,
    chapter_next,
    stage_draft_submit,
    stage_polish_submit,
    submit_output,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import read_json

from scripts.tests.decoupled_helpers import write_plan

# 对准计划 must 锚词「试炼」，含白名单名字与地点；引文取自正文逐字子串
_PROSE = "主角在市集完成试炼。拒收改期，凭据还压在柜台那头，改天再来取。"
_ACK_QUOTES = ["主角在市集完成试炼", "拒收改期，凭据还压在柜台那头", "凭据还压在柜台那头，改天再来取"]


def _chapter(store: BookStore) -> int:
    return int(store.read_head().get("chapter") or 1)


def _commit_one(store: BookStore) -> None:
    """完整链提交当前章并完成 ack；账本与 head 由流水线自身推进。"""
    for _ in range(8):
        r = chapter_next(store)
        action = r.get("action")
        if action == "draft":
            Path(r["draft_output_path"]).write_text(_PROSE, encoding="utf-8")
            stage_draft_submit(store)
        elif action == "polish":
            Path(r["polished_output_path"]).write_text(_PROSE, encoding="utf-8")
            stage_polish_submit(store)
        elif action == "assemble":
            view = read_json(store.assemble_pack_path(_chapter(store)))
            res = submit_output(
                store,
                {
                    "prose": _PROSE,
                    "l1_summary": "主角完成试炼，拒收改期。",
                    "state_delta": {
                        "moves": [{"who": "主角", "to": "市集"}],
                        "facts": [],
                        "named": ["主角", "掌柜"],
                    },
                    "memory": {"voice_concepts": []},
                    "pack_hash": view["pack_hash"],
                    "beats_hit": ["b1"],
                    "plot_findings": [],
                },
            )
            assert res["verdict"] == "accepted", res
        elif action == "ack":
            acked = ack_read(store, quotes=list(_ACK_QUOTES), verdict="pass")
            assert acked["verdict"] == "acked", acked
            return
        elif action == "extend_plan":
            raise AssertionError(f"extend_plan fired too early: remaining={r.get('remaining_in_plan')}")
        else:
            raise AssertionError(f"unexpected action {action}: {r}")
    raise AssertionError("did not reach ack")


def _pass_story_review(store: BookStore) -> None:
    """卷界剧情复核按设计优先于扩纲提示；显式通过它再验 extend_plan。"""
    from novel_ledger_core.content.story_review import pending_review, prepare_review, submit_review

    spec = pending_review(store)
    assert spec is not None
    prepare_review(store, spec)
    view = read_json(store.staging_dir / "story-review-view.json")
    checks = [
        {
            "id": key,
            "status": "pass",
            "reason": "拒收改期与凭据扣留构成实际代价与承诺对证的成对证据。",
            "evidence": [
                {"chapter": view["first"], "quote": "拒收改期，凭据还压在柜台那头"},
                {"chapter": view["through"], "quote": "凭据还压在柜台那头，改天再来取"},
            ],
        }
        for key in view["required_checks"]
    ]
    res = submit_review(
        store,
        {
            "review_id": view["review_id"],
            "input_hash": view["input_hash"],
            # 复核必须显式声明看不到的证据维度；逐维核过就写空表。
            "unverifiable_dimensions": [],
            "checks": checks,
        },
    )
    assert res["verdict"] == "pass", res


def _small_store(tmp_path: Path, chapters: int = 8, volumes: dict | None = None, **cfg_extra):
    chapters_list = [
        {
            "chapter": n,
            "location": "市集",
            "present": ["主角", "掌柜"],
            "beats": [{"id": "b1", "required": True, "text": "主角完成试炼", "must": "试炼"}],
        }
        for n in range(1, chapters + 1)
    ]
    if volumes and chapters_list:
        # 按 volumes 预算给章标卷号
        vol_keys = sorted(volumes)
        idx = 0
        for key in vol_keys:
            budget = int(volumes[key].get("chapters_budget") or 0)
            for _ in range(budget):
                if idx < len(chapters_list):
                    chapters_list[idx]["volume"] = key
                    idx += 1
    plan: dict = {"protagonist": "主角", "chapters": chapters_list}
    if volumes:
        plan["volumes"] = volumes
    plan_path = write_plan(tmp_path, plan)
    proj = tmp_path / "proj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = "off"
    # conftest 会话级把 DEFAULT_CONFIG.plan_low_water 置 0（既有管线用例不受扩纲打扰）；
    # 本文件测的就是水位触发，按书配置显式开回。
    cfg["plan_low_water"] = 5
    cfg.update(cfg_extra)
    store.save_config(cfg)
    return store


def test_default_span_twenty(tmp_path: Path):
    """默认 plan_extend_span=20：低水位回执与简报都要求覆盖到 max+20。"""
    store = _small_store(tmp_path, chapters=8)
    for _ in range(3):  # 提交 3 章 → 剩 5 ≤ low_water
        _commit_one(store)
    r = chapter_next(store)
    assert r["action"] == "extend_plan", r.get("action")
    assert r["plan_extend_span"] == 20
    assert r["extend_through_ch"] == r["max_planned_ch"] + 20
    assert r["suggest_from"] == r["max_planned_ch"] + 1
    brief = json.loads(Path(r["plan_worker_brief"]["path"]).read_text(encoding="utf-8"))
    assert brief["extend_through_ch"] == r["extend_through_ch"]
    assert brief["plan_extend_span"] == 20
    assert "COVER" in r["hint"] and "waterline" in r["hint"]


def test_span_volume_signs_full_current_volume(tmp_path: Path):
    """volume 模式：按当前卷 chapters_budget 扩满到卷末章。"""
    store = _small_store(
        tmp_path,
        chapters=8,
        volumes={"1": {"title": "卷一", "chapters_budget": 10}},
        plan_extend_span="volume",
    )
    for _ in range(3):
        _commit_one(store)
    r = chapter_next(store)
    assert r["action"] == "extend_plan"
    assert r["plan_extend_span"] == "volume"
    # 卷一预算 10、已写 3 → 卷末 = 3 + (10-3) = 10（max_planned=5 尚未到卷末）
    assert r["extend_through_ch"] == 10


def test_span_volume_full_volume_signs_next_volume(tmp_path: Path):
    """volume 模式：当前卷已全部签批 → 整签下一卷。"""
    store = _small_store(
        tmp_path,
        chapters=8,
        volumes={
            "1": {"title": "卷一", "chapters_budget": 3},
            "2": {"title": "卷二", "chapters_budget": 7},
        },
        plan_extend_span="volume",
    )
    for _ in range(3):
        _commit_one(store)
    r = chapter_next(store)
    if r["action"] == "story_review":  # 卷一正好写满：卷界复核先于扩纲提示
        _pass_story_review(store)
        r = chapter_next(store)
    assert r["action"] == "extend_plan", r.get("action")
    # 下一未签章在卷二：卷二末 = last_c(3) + 卷二预算 7 = 10
    assert r["plan_extend_span"] == "volume"
    assert r["extend_through_ch"] == 10


def test_span_volume_without_volume_contract_falls_back(tmp_path: Path):
    """volume 模式但无卷合同 → 回退 20 章（回执标注 fallback）。"""
    store = _small_store(tmp_path, chapters=8, plan_extend_span="volume")
    for _ in range(3):
        _commit_one(store)
    r = chapter_next(store)
    assert r["action"] == "extend_plan"
    assert r["plan_extend_span"] == "volume(fallback:20)"
    assert r["extend_through_ch"] == r["max_planned_ch"] + 20


def test_custom_span_integer(tmp_path: Path):
    """整数跨度可调：plan_extend_span=8 → 扩到 max+8。"""
    store = _small_store(tmp_path, chapters=8, plan_extend_span=8)
    for _ in range(3):
        _commit_one(store)
    r = chapter_next(store)
    assert r["action"] == "extend_plan"
    assert r["extend_through_ch"] == r["max_planned_ch"] + 8
