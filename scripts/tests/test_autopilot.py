"""无人值守会话宿主面：交接卡、协议版本单源与恢复语义。

无人值守只有一种形态——宿主会话主线程内循环唤起子 agent（见
references/unattended.md）。本文件锁定内循环依赖的三个机器面：
- `run handoff`：交接卡机器段（HEAD/下一动作/临期钩子/开放 findings）零考古恢复入口；
- `WORKER_PROMPT_PROTOCOL`：宿主派发提示词与 `chapter next` worker 简报共用同一常量；
- `run resume`：清 paused 状态并重置未兑现的扩纲提示标记（「扩纲优先」不被吞掉）。

检查点行为见 test_autopilot_checkpoints.py。
"""

from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.control.autopilot import handoff_report, resume_run
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import chapter_next
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import read_json
from novel_ledger_core.ledger.ledger import commit_event


def _store(tmp_path: Path, *, chapters: int = 45) -> BookStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = {
        "protagonist": "主角",
        "chapters": [
            {
                "chapter": number,
                "location": "试炼场",
                "present": ["主角"],
                "beats": [
                    {
                        "id": "b1",
                        "required": True,
                        "text": f"主角完成第{number}步试炼",
                        "must": "试炼",
                    }
                ],
            }
            for number in range(1, chapters + 1)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book-project"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["style_check"] = "off"
    store.save_config(cfg)
    return store


def test_worker_protocol_version_is_single_sourced(tmp_path: Path):
    """宿主派发提示词与 `chapter next` 的 worker 简报共用同一协议版本常量。

    版本行是"宿主装了新旧两个 skill 实例混跑"的唯一可见信号（纪律见
    references/unattended.md「协议版本纪律」）：两侧各写一份字面量会静默分叉，
    所以必须同源，且此处钉住简报输出真的携带它。
    """
    from novel_ledger_core.control.pipeline import WORKER_PROMPT_PROTOCOL, _worker_brief

    store = _store(tmp_path, chapters=2)
    nxt = chapter_next(store)
    chapter_brief = _worker_brief(store, nxt)
    assert chapter_brief["worker_protocol"] == WORKER_PROMPT_PROTOCOL
    plan_brief = _worker_brief(store, {"action": "extend_plan"})
    assert plan_brief["worker_protocol"] == WORKER_PROMPT_PROTOCOL


def test_run_handoff_writes_machine_section(tmp_path: Path) -> None:
    """交接卡是恢复会话的零考古入口：HEAD/下一动作/临期钩子/开放 findings 全进一张卡。"""
    store = _store(tmp_path, chapters=6)
    commit_event(
        store,
        chapter=1,
        state_delta={
            "named": ["主角"],
            "facts": [],
            "hooks": [
                {"id": "h-soon", "text": "临期约定", "due": 2, "status": "open"},
                {"id": "h-far", "text": "远期约定", "due": 90, "status": "open"},
            ],
        },
    )
    head_before = store.read_head()
    result = handoff_report(store)
    assert result["ok"] is True and result["action"] == "run_handoff"
    assert result["due_hook_count"] == 1
    text = (store.run_dir / "handoff.md").read_text(encoding="utf-8")
    assert "下一动作" in text
    assert "临期约定" in text, "due ≤ chapter+3 的 open 钩子必须进卡"
    assert "远期约定" not in text, "远期钩子不进卡，防止卡片膨胀"
    assert "开放 findings" in text
    assert "unattended-log.md 尚未创建" in text
    # 交接只读：HEAD 相位与章号不得被推进。
    assert store.read_head() == head_before


def test_run_resume_resets_unhonored_plan_nudge(tmp_path: Path) -> None:
    """恢复清 paused 状态并重置未兑现的低水位标记：plan worker 失败后「扩纲优先」不被吞。"""
    store = _store(tmp_path, chapters=20)
    head = store.read_head()
    head["plan_low_water_nudged_at"] = 20
    store.write_head(head)

    result = resume_run(store)
    assert result["ok"] is True and result["resumed"] is True
    state = read_json(store.autopilot_state_path)
    assert state["status"] == "ready" and state["pause_reason"] is None
    assert "plan_low_water_nudged_at" not in store.read_head()
    events = [
        json.loads(line)
        for line in store.autopilot_events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "plan_nudge_marker_reset" for e in events)
    assert any(e.get("event") == "resumed" for e in events)
    # 计划已增长过的旧标记（提示已被兑现）保持原语义：不清。
    head = store.read_head()
    head["plan_low_water_nudged_at"] = 20
    store.write_head(head)
    plan = store.load_plan()
    plan["chapters"].append({
        "chapter": 30, "location": "试炼场", "present": ["主角"],
        "beats": [{"id": "b1", "required": True, "text": "主角完成第30步试炼", "must": "试炼"}],
    })
    store.save_plan(plan)
    resume_run(store)
    assert store.read_head().get("plan_low_water_nudged_at") == 20
