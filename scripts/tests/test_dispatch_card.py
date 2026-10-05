"""调度卡（--card）回归测试：总编辑调度层的上下文预算。

背景：stage-agent 宿主会话是唯一长生命周期对话，每阶段 `chapter next` 的全量信封
（hint/场次预算/钩子清单/自检清单，2–5KB）都进宿主上下文，章章叠加、每轮重播——
实测宿主主会话转录 6 章即 ~900KB。调度卡只收动作/相位/停止位与派发指针，
信封全量留在盘上由 worker 自取。

锁定的不变量：
- stage-agent 宿主：card 输出只含白名单字段，信封文件在盘上含全量载荷；
- 执行者会话（inline/worker-agent，无信封落盘）：card 自动回退全量并注明原因；
- status 卡只留相位/进度水位，不带诊断体量字段。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import chapter_next, status
from novel_ledger_core.infra.store import BookStore


@pytest.fixture()
def store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：调度卡验证。",
        "chapters": [
            {
                "chapter": n,
                "volume": "vol-0001",
                "location": "账房",
                "present": ["主角"],
                "beats": [{"id": f"b{n}", "required": True, "text": f"第{n}章对账", "must": "对账"}],
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


def _as_stage_agent(store: BookStore) -> None:
    cfg = store.load_config()
    cfg["execution_mode"] = "stage-agent"
    store.save_config(cfg)


def test_card_strips_envelope_and_keeps_dispatch_pointers(store: BookStore) -> None:
    _as_stage_agent(store)
    card = chapter_next(store, card=True)
    assert card["action"] == "draft" and card["chapter"] == 1
    # 白名单之外的字段（信封体量）一律不进卡。
    for banned in ("hint", "scene_budget", "hooks_due_unplanned", "plot_self_check", "execution"):
        assert banned not in card, f"dispatcher card must not carry {banned}"
    # 派发指针与记账句柄必须在：宿主据此建 spawn prompt 与 usage-record。
    worker = card["worker"]
    assert worker["action_path"] == card["envelope_path"]
    assert worker["role_card"].endswith("drafting-editor.md")
    assert worker["usage_request"]["request_id"]
    assert worker["stop_on"]
    # 信封全量在盘上（hint 等只住在文件里，不进宿主上下文）。
    envelope = json.loads(Path(card["envelope_path"]).read_text(encoding="utf-8"))
    assert envelope.get("hint")
    assert envelope.get("draft_output_path")
    assert card["card_note"]


def test_card_falls_back_to_full_payload_for_executor_sessions(store: BookStore) -> None:
    # 默认 inline：本会话就是执行者，信封不落盘，裁掉等于丢载荷——必须回退全量。
    full = chapter_next(store)
    assert full["action"] == "draft"
    store2 = store
    card = chapter_next(store2, card=True)
    assert "card_fallback" in card
    assert card.get("hint"), "fallback must return the full payload"


def test_card_preserves_stop_and_transition_semantics(store: BookStore) -> None:
    _as_stage_agent(store)
    first = chapter_next(store, card=True)
    assert first["action"] == "draft"
    # 卡不改变命令语义：head_transition 这类机器可读差异必须保留。
    assert "head_transition" in first and first["head_transition"]["changed"]


def test_status_card_keeps_phase_and_progress_only(store: BookStore) -> None:
    full = status(store)
    card = status(store, card=True)
    assert card["phase"] == full["phase"] and card["chapter"] == full["chapter"]
    for banned in ("storage", "config_shadow", "document_shadow", "usage", "runtime", "editorial_review"):
        assert banned not in card, f"status card must not carry {banned}"
    assert card["plan_remaining_count"] == full["plan_remaining_count"]
    assert card["book_words_written"] == full["book_words_written"]
