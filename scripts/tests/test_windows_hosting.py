"""DSH/Windows 宿主实战缺口回归：BOM 容错、hooks defer 钉住防线、set-spine 写入口。

源自无人值守开书会话（2026-10-01）：PowerShell BOM 炸 quotes-file 一轮、
defer 与已签章拍 due 口径冲突双重往返、quant_key_not_in_world_spine 无命令可消。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import defer_hook, set_world_spine, validate_plan
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, read_json, stable_read_text
from novel_ledger_core.ledger.ledger import commit_event, read_events


def _store(tmp_path: Path, *, name: str = "hosting") -> BookStore:
    plan = {
        "protagonist": "主角",
        "chapters": [
            {
                "chapter": ch,
                "location": "市集",
                "present": ["主角"],
                "beats": [
                    {
                        "id": f"b{ch}",
                        "required": True,
                        "text": "推进成交",
                        "must": "成交",
                    }
                ],
            }
            for ch in range(1, 5)
        ],
    }
    # 第 4 章章拍钉住 h1 的 due=4：组装机检的期望值真源
    plan["chapters"][3]["beats"][0]["effects"] = {
        "hooks": [{"id": "h1", "status": "paid", "due": 4}]
    }
    plan_path = tmp_path / f"{name}-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / name
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def _seed_hook(store: BookStore, chapter: int = 1, *, hook_id: str = "h1", due: int = 4) -> None:
    store.chapter_md_path(chapter).write_text(f"第{chapter}章正文，主角市集成交。\n", encoding="utf-8")
    store.accept_stage_text(store.chapter_md_path(chapter), store.chapter_md_path(chapter).read_text(encoding="utf-8"))
    commit_event(
        store,
        chapter,
        {"hooks": [{"id": hook_id, "text": "对方约定期限", "status": "open", "due": due}]},
    )


# ---------- P3：Windows BOM 容错 ----------

def test_read_json_and_stable_read_tolerate_utf8_bom(tmp_path: Path):
    bom_json = tmp_path / "quotes.json"
    bom_json.write_bytes(b"\xef\xbb\xbf" + json.dumps(["第一句", "第二句"], ensure_ascii=False).encode("utf-8"))
    assert read_json(bom_json) == ["第一句", "第二句"]

    bom_text = tmp_path / "draft.txt"
    bom_text.write_bytes(b"\xef\xbb\xbf" + "正文开头".encode("utf-8"))
    assert stable_read_text(bom_text) == "正文开头"


def test_cli_set_spine_reads_bom_file(tmp_path: Path, capsys):
    store = _store(tmp_path)
    spine_file = tmp_path / "spine.txt"
    spine_file.write_bytes(b"\xef\xbb\xbf" + "历差为一日，廪米三斗。".encode("utf-8"))
    code = main([
        "plan", "set-spine", "--project", str(store.project),
        "--file", str(spine_file), "--reason", "补口径",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert store.load_plan()["world_spine"] == "历差为一日，廪米三斗。"


# ---------- P1：hooks defer 钉住防线 ----------

def test_defer_refuses_when_signed_beat_pins_different_due(tmp_path: Path):
    store = _store(tmp_path)
    _seed_hook(store, due=4)

    with pytest.raises(LedgerError) as exc:
        defer_hook(store, hook_id="h1", new_due=6)

    assert exc.value.code == "plan_hook_due_pinned"
    assert exc.value.details["pinned"] == [{"chapter": 4, "beat": "b4", "pinned_due": 4}]
    # 拒绝路径不落治理事件
    assert not [e for e in read_events(store) if e.get("action") == "hook.defer"]


def test_defer_passes_when_no_conflict_and_force_waives(tmp_path: Path):
    store = _store(tmp_path)
    prose = "第1章正文，主角市集成交。\n"
    store.chapter_md_path(1).write_text(prose, encoding="utf-8")
    store.accept_stage_text(store.chapter_md_path(1), prose)
    commit_event(
        store,
        1,
        {"hooks": [
            {"id": "h1", "text": "对方约定期限", "status": "open", "due": 4},
            {"id": "h2", "text": "另一承诺", "status": "open", "due": 2},
        ]},
    )

    # 未被章拍引用的 hook：自由 defer
    free = defer_hook(store, hook_id="h2", new_due=9)
    assert free["action"] == "hook_deferred"
    assert free["pinned_due_overridden"] is None

    # 钉住同值：不是冲突
    same = defer_hook(store, hook_id="h1", new_due=4)
    assert same["action"] == "hook_deferred"

    # --force 豁免留痕
    forced = defer_hook(store, hook_id="h1", new_due=6, force=True)
    assert forced["pinned_due_overridden"] == [{"chapter": 4, "beat": "b4", "pinned_due": 4}]
    events = [e for e in read_events(store) if e.get("action") == "hook.defer"]
    last = [e for e in events if e.get("hook_id") == "h1" and e.get("new_due") == 6]
    assert last and last[-1].get("forced_over_pinned") is True


# ---------- P2：set-spine 受支持写入口 ----------

def test_set_world_spine_writes_truth_and_clears_quant_warning(tmp_path: Path):
    store = _store(tmp_path)
    cfg = store.load_config()
    cfg["quant_keys"] = ["历差", "廪米"]
    store.save_config(cfg)
    codes_before = {w["code"] for w in validate_plan(store)["warnings"]}
    assert "quant_key_not_in_world_spine" in codes_before

    result = set_world_spine(store, text="历差为一日；廪米三斗。", reason="把口径收进必达通道")

    assert result["action"] == "plan_spine_set"
    assert result["quant_keys_covered"] is True
    assert store.load_plan()["world_spine"] == "历差为一日；廪米三斗。"
    codes_after = {w["code"] for w in validate_plan(store)["warnings"]}
    assert "quant_key_not_in_world_spine" not in codes_after
    sealed = [e for e in read_events(store) if e.get("action") == "plan.spine_set"]
    assert len(sealed) == 1 and sealed[0]["length"] == len("历差为一日；廪米三斗。")


def test_set_world_spine_guards(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(LedgerError) as no_reason:
        set_world_spine(store, text="有内容", reason="")
    assert no_reason.value.code == "invalid_args"
    with pytest.raises(LedgerError) as empty:
        set_world_spine(store, text="  ", reason="空白")
    assert empty.value.code == "invalid_args"
    with pytest.raises(LedgerError) as too_long:
        set_world_spine(store, text="字" * 4001, reason="超长")
    assert too_long.value.code == "invalid_args"
