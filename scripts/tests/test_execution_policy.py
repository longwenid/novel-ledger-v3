from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    ack_read,
    authorize_usage_resume,
    chapter_next,
    record_usage,
    stage_draft_submit,
    stage_polish_submit,
    submit_output,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json


PROSE = (
    "主角站在市集门口拒收改期。"
    "掌柜把凭据压回柜台抽屉。"
    "围观的人群散去后主角仍未离开。"
)
QUOTES = ["主角站在市集门口", "掌柜把凭据压回柜台", "围观的人群散去后"]


def _store(tmp_path: Path, *, name: str = "execution") -> BookStore:
    plan = {
        "protagonist": "主角",
        "chapters": [
            {
                "chapter": 1,
                "location": "市集",
                "present": ["主角", "掌柜"],
                "beats": [
                    {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
                    {"id": "b2", "required": True, "text": "掌柜扣住凭据", "must": "凭据"},
                ],
            }
        ],
    }
    plan_path = tmp_path / f"{name}-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / name
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["style_check"] = "off"
    cfg["polish"] = "on"  # 本文件驱动润色泳线；skill 默认只写作（polish=off）
    store.save_config(cfg)
    return store


def _assert_safe_action(payload: dict, expected: str) -> None:
    assert payload["action"] == expected
    assert payload["execution"]["mode"] == "inline"
    assert payload["execution"]["spawn_allowed"] is False
    assert payload["execution"]["context_isolation"] == "stage_pack"
    hint = str(payload.get("hint") or "").lower()
    assert "spawn a fresh" not in hint
    assert "fresh sub-agent" not in hint


def _advance_to_submitted(store: BookStore) -> dict:
    draft = chapter_next(store)
    _assert_safe_action(draft, "draft")
    Path(draft["draft_output_path"]).write_text(PROSE, encoding="utf-8")
    stage_draft_submit(store)

    polish = chapter_next(store)
    _assert_safe_action(polish, "polish")
    Path(polish["polished_output_path"]).write_text(PROSE, encoding="utf-8")
    stage_polish_submit(store)

    assemble = chapter_next(store)
    _assert_safe_action(assemble, "assemble")
    output = _assembly_output(store)
    submitted = submit_output(store, output)
    assert submitted["verdict"] == "accepted"
    return submitted


def _assembly_output(store: BookStore) -> dict:
    pack = read_json(store.current_pack_path)
    return {
        "l1_summary": "主角拒收改期，掌柜仍扣着凭据。",
        "state_delta": {
            "moves": [{"who": "主角", "to": "市集"}],
            "facts": [{"who": "主角", "text": "拒收改期"}],
            "debts": [],
            "hooks": [],
            "relations": [],
            "named": ["主角", "掌柜"],
            "new_names": [],
            "deaths": [],
        },
        "memory": {},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1", "b2"],
    }


def test_submitted_next_returns_safe_ack_action(tmp_path: Path):
    store = _store(tmp_path)
    _advance_to_submitted(store)
    ack = chapter_next(store)
    _assert_safe_action(ack, "ack")


def test_usage_guard_does_not_block_deterministic_commit(tmp_path: Path):
    store = _store(tmp_path, name="commit-after-budget")
    cfg = store.load_config()
    cfg["usage_budget"]["stop_input_per_chapter"] = 10
    store.save_config(cfg)
    _advance_to_submitted(store)
    usage = record_usage(
        store,
        chapter=1,
        stage="assemble",
        request_id="req-over-after-submit",
        usage={"uncached_input_tokens": 11, "output_tokens": 1},
    )
    assert usage["stop"] is True

    # HEAD is submitted: commit is pure disk/ledger work and finishes, but the following
    # final-review model action is still blocked. The editorial phase remains resumable.
    guarded = chapter_next(store)
    assert guarded["action"] == "usage_guard"
    assert guarded["commit_completed"] is True
    assert guarded["resume_phase"] == "await_ack"
    assert store.chapter_md_path(1).exists()
    assert store.read_head()["phase"] == "await_ack"

    authorized = authorize_usage_resume(
        store,
        chapter=1,
        action="ack",
        actor="总编辑",
        reason="正文已确定性提交，仅放行一次有界终审以完成质量闭环",
    )
    assert authorized["authorized_action"] == "ack"
    assert store.load_config()["usage_budget"]["stop_input_per_chapter"] == 10

    ack = chapter_next(store)
    _assert_safe_action(ack, "ack")
    assert ack["usage_authorization_consumed"] is True
    assert not store.read_head().get("usage_authorization")

    # Issuing the action consumes the grant. A runner retry cannot start a second model request;
    # the deterministic ack-read below is still allowed to finish the chapter.
    replay = chapter_next(store)
    assert replay["action"] == "usage_guard"
    closed = ack_read(store, quotes=QUOTES, verdict="pass")
    assert closed["verdict"] == "acked"
    assert not store.read_head().get("usage_authorization")


def test_acked_chapter_budget_does_not_poison_next_chapter(tmp_path: Path):
    """章预算在 ack 边界结束；新会话下一章不能被上一章用量永久锁死。"""
    store = _store(tmp_path, name="budget-resets-after-ack")
    store.extend_plan([
        {
            "chapter": 2,
            "location": "车站",
            "present": ["主角"],
            "beats": [
                {"id": "b1", "required": True, "text": "主角进入车站", "must": "车站"},
                {"id": "b2", "required": True, "text": "车站点名没他", "must": "车站"},
                {"id": "b3", "required": True, "text": "票根数目对不上", "must": "票根"},
                {"id": "b4", "required": True, "text": "站长来车站圆场", "must": "车站"},
                {"id": "b5", "required": True, "text": "主角在车站候补明早", "must": "车站"},
            ],
        }
    ])
    cfg = store.load_config()
    cfg["usage_budget"]["stop_input_per_chapter"] = 10
    store.save_config(cfg)

    _advance_to_submitted(store)
    ack_action = chapter_next(store)
    _assert_safe_action(ack_action, "ack")
    usage = record_usage(
        store,
        chapter=1,
        stage="ack",
        request_id="req-ack-over-budget",
        usage={"uncached_input_tokens": 11, "output_tokens": 1},
    )
    assert usage["stop"] is True
    closed = ack_read(store, quotes=QUOTES, verdict="pass")
    assert closed["requires_new_session"] is True

    next_chapter = chapter_next(store)
    _assert_safe_action(next_chapter, "draft")
    assert next_chapter["chapter"] == 2



def _draft_only_store(tmp_path: Path, *, name: str = "rework") -> BookStore:
    """只写作泳线（polish=off）的返工定点修夹具：draft-submit 直落 await_assembly。"""
    plan = {
        "protagonist": "主角",
        "chapters": [
            {
                "chapter": 1,
                "location": "市集",
                "present": ["主角", "掌柜"],
                "beats": [
                    {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
                    {"id": "b2", "required": True, "text": "掌柜扣住凭据", "must": "凭据"},
                ],
            }
        ],
    }
    plan_path = tmp_path / f"{name}-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / name
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["style_check"] = "off"
    cfg["polish"] = "off"
    store.save_config(cfg)
    return store


def _force_plot_fix(store: BookStore) -> None:
    """把 HEAD 推进 plot_fix 语义：轮转旧稿、清 staging、回 await_draft、rewrite+1。"""
    from novel_ledger_core.control.pipeline import _plot_self_check_fail

    verdict = _plot_self_check_fail(store, store.read_head(), [{"severity": "BLOCKER", "hint": "口径矛盾"}])
    assert verdict["verdict"] == "plot_fix"


def test_rework_patch_targets_sentence_without_redraft_round(tmp_path: Path):
    """返工定点修：单句 BLOCKER 修复零模型轮次——恢复轮转稿+锚定替换，
    draft-submit 阶段闸一个不少照跑，phase 回组装位。"""
    from novel_ledger_core.control.pipeline import rework_patch

    store = _draft_only_store(tmp_path)
    action = chapter_next(store)
    Path(action["draft_output_path"]).write_text(PROSE, encoding="utf-8")
    stage_draft_submit(store)  # polish=off → await_assembly，polished 已落位
    _force_plot_fix(store)
    assert not store.draft_text_path(1).exists()  # staging 已被轮转清空

    result = rework_patch(store, patches=[("拒收改期", "拒收改期并立下字据")])

    assert result["action"] == "rework_patch"
    assert result["applied_patches"] == 1
    restored = store.draft_text_path(1).read_text(encoding="utf-8")
    assert "拒收改期并立下字据" in restored
    assert "拒收改期。" not in restored  # 原句已替换，其余逐字保留
    assert "掌柜把凭据压回柜台抽屉。" in restored

    accepted = stage_draft_submit(store)
    assert accepted["verdict"] == "draft_accepted"
    assert store.read_head()["phase"] == "await_assembly"
    # 质量日志留痕：返工定点修有据可查
    quality = store.quality_log_path.read_text(encoding="utf-8")
    assert "rework_patch" in quality


def test_rework_patch_guards(tmp_path: Path):
    from novel_ledger_core.control.pipeline import rework_patch

    store = _draft_only_store(tmp_path)
    chapter_next(store)
    # 新章（rewrite_count=0）不得绕过执笔 worker
    with pytest.raises(LedgerError) as fresh:
        rework_patch(store, patches=[("拒收", "改口")])
    assert fresh.value.code == "invalid_args"
    with pytest.raises(LedgerError) as no_patches:
        rework_patch(store, patches=[])
    assert no_patches.value.code == "empty_patches"

    action = chapter_next(store)
    Path(action["draft_output_path"]).write_text(PROSE, encoding="utf-8")
    stage_draft_submit(store)
    _force_plot_fix(store)

    with pytest.raises(LedgerError) as missing:
        rework_patch(store, patches=[("不存在的句子", "新句")])
    assert missing.value.code == "patch_target_not_found"
    with pytest.raises(LedgerError) as ambiguous:
        rework_patch(store, patches=[("主角", "某人")])  # PROSE 中出现两次
    assert ambiguous.value.code == "patch_target_ambiguous"

    rework_patch(store, patches=[("拒收改期", "拒收改期并立下字据")])
    stage_draft_submit(store)
    # 相位已离开 await_draft：不得再当返工轮用
    with pytest.raises(LedgerError) as wrong_phase:
        rework_patch(store, patches=[("字据", "契书")])
    assert wrong_phase.value.code == "wrong_phase"
