"""宿主批界（调度卡 host_batch 节）回归测试。

背景：会话宿主的上下文单调增长，每阶段 `chapter next` 全量重读一次——实测 50 章单会话
的主会话转录吃掉全 session 60% 的 token 体量。批界硬停（boundary=true → run handoff
收口本会话、接力新会话从盘上 HEAD 续跑）是宿主上下文唯一的生命周期闸门。

实测教训（23 章连跑会话）：宿主跑了 3 次批中 handoff 却不结束会话，而 handoff 事件
会重置基线——计数器每次清零自缴械，boundary 零点亮。锁定的不变量：
- 基线只认真会话起点：run_started/resumed，或带 --host-session-start 的 run_handoff；
- 批中普通 handoff 不重置基线、不解除闩锁：acks ≥ bound 后 boundary 持续点亮；
- boundary 只在 draft/extend_plan 决策点亮 note，但 ack/assemble 回退卡也带 host_batch 节；
- config.host_batch_chapters=0 时整节消失（supervisor 进程形态不受影响）；
- run checkpoint 的 CLI 响应不带完整报告（报告在盘上，回执只有结论与路径）。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.autopilot import (
    _append_event,
    handoff_report,
    host_batch_view,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import chapter_next
from novel_ledger_core.infra.store import BookStore


@pytest.fixture()
def store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：批界验证。",
        "chapters": [
            {
                "chapter": n,
                "volume": "vol-0001",
                "location": "账房",
                "present": ["主角"],
                "beats": [{"id": f"b{n}", "required": True, "text": f"第{n}章对账", "must": "对账"}],
            }
            for n in range(1, 31)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "bookproj"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    cfg = store.load_config()
    cfg["execution_mode"] = "stage-agent"
    # 批界默认已关（作者裁决=主线程循环连写，host_batch_chapters=0）；本文件测的就是
    # 批界特性本身，按书配置显式开回（托管分批场景的真实开法）。
    cfg["host_batch_chapters"] = 5
    store.save_config(cfg)
    return store


def _set_acked(store: BookStore, chapter: int) -> None:
    head = store.read_head()
    head.update({"phase": "idle", "chapter": chapter + 1, "last_committed_ch": chapter, "last_acked_ch": chapter})
    store.write_head(head)


def _events(store: BookStore) -> list[dict]:
    if not store.autopilot_events_path.exists():
        return []
    return [json.loads(line) for line in store.autopilot_events_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_session_start_handoff_establishes_baseline(store: BookStore) -> None:
    _set_acked(store, 3)
    result = handoff_report(store, host_session_start=True)
    assert result["action"] == "run_handoff" and result["chapter"] == 3
    assert result["host_session_start"] is True
    last = _events(store)[-1]
    assert last["event"] == "run_handoff" and last["chapter"] == 3 and last["host_session_start"] is True
    card = chapter_next(store, card=True)
    assert card["host_batch"] == {"acks_since_boundary": 0, "bound": 5, "boundary": False}


def test_card_flags_missing_baseline(store: BookStore) -> None:
    card = chapter_next(store, card=True)
    assert card["host_batch"]["baseline_missing"] is True
    assert "host-session-start" in card["host_batch"]["note"]


def test_boundary_latches_and_plain_handoff_cannot_clear_it(store: BookStore) -> None:
    handoff_report(store, host_session_start=True)  # 基线 0
    for ch in (1, 2, 3, 4, 5):
        _set_acked(store, ch)
    card = chapter_next(store, card=True)
    assert card["host_batch"]["boundary"] is True

    # 批中收口的普通 handoff：不重置基线、不解除闩锁。
    handoff_report(store)
    card = chapter_next(store, card=True)
    assert card["host_batch"]["boundary"] is True
    assert card["host_batch"]["acks_since_boundary"] == 5, "plain handoff must not reset the baseline"
    assert "LATCHED" in card["host_batch"]["note"]

    _set_acked(store, 6)
    assert host_batch_view(store, action="draft")["boundary"] is True
    assert host_batch_view(store, action="assemble")["boundary"] is False, "mid-chapter actions stay quiet"
    assert host_batch_view(store, action="extend_plan")["boundary"] is True


def test_new_session_resumed_resets_baseline(store: BookStore) -> None:
    handoff_report(store, host_session_start=True)
    for ch in (1, 2, 3, 4, 5):
        _set_acked(store, ch)
    assert host_batch_view(store, action="draft")["boundary"] is True
    # 接力新会话：run resume 事件携带章号 → 新基线，闩锁解除。
    _append_event(store, "resumed", chapter=5)
    view = host_batch_view(store, action="draft")
    assert view == {"acks_since_boundary": 0, "bound": 5, "boundary": False}


def test_baseline_takes_latest_session_start_event(store: BookStore) -> None:
    _set_acked(store, 3)
    handoff_report(store, host_session_start=True)  # 基线 3
    _append_event(store, "resumed", chapter=6)  # 接力会话起点：基线 6 覆盖 3
    _set_acked(store, 8)
    # 若误用旧 handoff 基线（3），since=5 会点亮 boundary；正确基线是 resumed 的 6。
    view = host_batch_view(store, action="draft")
    assert view["acks_since_boundary"] == 2 and view["boundary"] is False


def test_config_zero_disables_host_batch_node(store: BookStore) -> None:
    cfg = store.load_config()
    cfg["host_batch_chapters"] = 0
    store.save_config(cfg)
    handoff_report(store, host_session_start=True)
    _set_acked(store, 9)
    card = chapter_next(store, card=True)
    assert "host_batch" not in card
    assert host_batch_view(store, action="draft") is None


def test_fallback_card_carries_host_batch(store: BookStore) -> None:
    # inline/执行者会话走 card_fallback（信封不落盘）：批界节必须仍然可见。
    cfg = store.load_config()
    cfg["execution_mode"] = "inline"
    store.save_config(cfg)
    card = chapter_next(store, card=True)
    assert "card_fallback" in card
    assert card["host_batch"]["baseline_missing"] is True
