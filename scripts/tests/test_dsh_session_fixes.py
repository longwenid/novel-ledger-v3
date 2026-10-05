"""DSH 会话（sess 243a6dfd，90年代探案 ch353+）暴露缺陷的修复回归。

五个缺陷同源是「回执不可诊/通道断裂」：
1. resync_baseline 只重盖 meta/ack 不重盖 summary → checkpoint 的
   prose_hash_mismatch 死循环，宿主被迫手改 DB 里的 summary 行才解锁；
2. checkpoint 的 mismatch 阻断项不点名哪份 artifact 陈旧、不指路 resync；
3. CLI 输出面不强制 UTF-8，Windows 控制台把中文路径/引文打成乱码；
4. rewrite 收口轮转并删除 staging 草稿后，「保稿直接 draft-submit」被
   missing_draft 挡死（宿主空转多轮）；
5. blocked 回执只有「human must intervene」，宿主连吃 wrong_phase 试错后
   才翻 recovery.md 找到 retry-authorize。
另有调度卡 cli_invocation（防 .dsh/.zcode 跨宿主路径漂移）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from novel_ledger_core.control import autopilot
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.cli import _force_utf8_stdio
from novel_ledger_core.control.pipeline import (
    _gate_fail,
    chapter_next,
    resync_baseline,
    stage_draft_submit,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, read_json, sha256_text

from scripts.tests.decoupled_helpers import FALLBACK_PROSE, make_plan, write_plan
from scripts.tests.test_autopilot import _store
from scripts.tests.test_autopilot_checkpoints import (
    _chapter_artifacts,
    _record_batch_selection,
)


def _checkpoint_clean(store: BookStore, *, chapter: int) -> dict:
    return autopilot._checkpoint_report(store, chapter=chapter, previous=0, kinds=("batch",))


def test_resync_restamps_summary_and_breaks_checkpoint_deadloop(tmp_path: Path) -> None:
    """checkpoint 查 meta/summary/ack 三份哈希，resync 曾只修 meta/ack：
    patch 过的章 summary 哈希永远陈旧 → 「resync 说修好 → checkpoint 依旧
    mismatch」死循环，宿主被迫手改 DB summary 行（实测）。"""
    store = _store(tmp_path, chapters=6)
    for chapter in range(1, 6):
        _chapter_artifacts(store, chapter)
    snap = read_json(store.snapshot_path)
    snap["chapter"] = 5
    atomic_json(store.snapshot_path, snap)
    _record_batch_selection(store, batch_from=1, batch_to=6)
    assert _checkpoint_clean(store, chapter=5)["blockers"] == []

    # 外部改写正文（走权威写，等价 chapter patch / 授权工具改稿）
    new_prose = "第1章，主角完成试炼，代价是当众认下漏登的账。\n"
    store.chapter_md_path(1).write_text(new_prose, encoding="utf-8")
    fresh = "sha256:" + sha256_text(new_prose)

    report = _checkpoint_clean(store, chapter=5)
    blocker = next(b for b in report["blockers"] if b["code"] == "prose_hash_mismatch")
    assert blocker["chapter"] == 1
    assert blocker["stale_artifacts"] == ["meta", "summary", "ack"]
    assert blocker["disk_hash"] == fresh
    assert "resync-baseline" in blocker["hint"]

    # 旧 resync 行为复现：只修 meta/ack、不修 summary → 死循环
    for path in (store.meta_path(1), store.ack_path(1)):
        data = read_json(path)
        data["prose_hash"] = fresh
        atomic_json(path, data)
    report = _checkpoint_clean(store, chapter=5)
    blocker = next(b for b in report["blockers"] if b["code"] == "prose_hash_mismatch")
    assert blocker["stale_artifacts"] == ["summary"], "summary 未被旧 resync 覆盖，正是死循环现场"

    result = resync_baseline(store)
    assert result["summaries_fixed"] >= 1
    assert _checkpoint_clean(store, chapter=5)["blockers"] == []


def test_draft_submit_restores_latest_preserved_revision(tmp_path: Path) -> None:
    """rewrite 收口把 staging 草稿轮转成 .revK 并删除原件后，文档化的
    「保稿直接 draft-submit」曾被 missing_draft 挡死；现在自动恢复最新轮转稿，
    机检照常全跑。"""
    plan_path = write_plan(tmp_path, make_plan(1))
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    store.save_config(cfg)

    action = chapter_next(store)
    live = Path(action["draft_output_path"])
    preserved = store.stage_revision_path(1, 1)
    store.accept_stage_text(preserved, FALLBACK_PROSE + "\n")

    result = stage_draft_submit(store)
    assert result["verdict"] == "draft_accepted"
    assert store.read_head()["phase"] == "await_assembly"
    assert live.exists(), "live draft 由最新轮转稿原样恢复"
    assert "主角站在市集" in live.read_text(encoding="utf-8")


def test_blocked_mechanical_hint_names_retry_authorize(tmp_path: Path) -> None:
    """未入账 + 全部违规皆机械单点时，blocked 回执直接给 retry-authorize 解锁命令，
    宿主不必翻 recovery.md 连吃 wrong_phase 试错（实测）。结构类违规仍走人工裁决。"""
    store = _store(tmp_path, chapters=6)
    head = store.read_head()
    head["chapter"] = 1
    head["phase"] = "await_assembly"
    head["rewrite_count"] = 1
    head["last_committed_ch"] = 0
    store.write_head(head)

    mechanical = [{"code": "beat_token_missing", "beat_id": "b1", "must": "拒收"}]
    resp = _gate_fail(store, head, mechanical)
    assert resp["verdict"] == "blocked" and resp["stop"] is True
    assert "retry-authorize" in resp["hint"]
    assert "await_draft" in resp["hint"]

    structural = [{"code": "word_count_low", "words": 100, "min": 2500}]
    store.write_head(head)
    resp = _gate_fail(store, head, structural)
    assert resp["verdict"] == "blocked"
    assert "human must intervene" in resp["hint"], "需要真实内容工作的违规不得暗示零门槛解锁"


def test_dispatch_card_carries_canonical_cli_invocation(tmp_path: Path) -> None:
    """调度卡带运行实例自己的绝对调用行：宿主曾中途在 .dsh/.zcode 安装路径间漂移。"""
    plan_path = write_plan(tmp_path, make_plan(2))
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["execution_mode"] = "stage-agent"
    store.save_config(cfg)
    card = chapter_next(store, card=True)
    assert "card_fallback" not in card
    assert card["cli_invocation"].startswith('py -3 "')
    assert 'novel_ledger.py" --project "' in card["cli_invocation"]
    assert str(store.project) in card["cli_invocation"]
    assert "cli_invocation" in card["card_note"]


def test_cli_force_utf8_stdio_reconfigures_and_tolerates_plain_streams(monkeypatch) -> None:
    class _Fake:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def reconfigure(self, **kwargs) -> None:
            self.calls.append(kwargs)

    out, err = _Fake(), _Fake()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    _force_utf8_stdio()
    assert out.calls == [{"encoding": "utf-8"}]
    assert err.calls == [{"encoding": "utf-8"}]

    class _Plain:  # pytest capsys 等替换流没有 reconfigure
        pass

    monkeypatch.setattr(sys, "stdout", _Plain())
    monkeypatch.setattr(sys, "stderr", _Plain())
    _force_utf8_stdio()  # 不抛即通过
