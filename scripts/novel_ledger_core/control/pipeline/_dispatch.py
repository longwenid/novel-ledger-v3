# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：dispatch
from __future__ import annotations

from pathlib import Path
from typing import Any
from ...infra.store import (BookStore)
from ...infra.util import (atomic_json)

# `chapter next` 的 worker 简报携带本版本；宿主派发提示词带同一行 `协议版本：<版本>`。
# 两侧版本不一致 = 新旧两个 skill 实例混跑，必须停止并如实报告（纪律见
# references/unattended.md）。改派发协议时先升此版本。
# v7：assemble action 新增 assemble_brief_path（一次读全的线性任务书），读取契约从
# 「polished + assemble pack」改为「brief + polished（pack 退为定向补读）」，并钉死
# 「一次写成 / 引文交机检 / 禁考古」三条省 token 纪律（实测组装单章 0.9–1.2M tok）。
WORKER_PROMPT_PROTOCOL = "worker-prompt.v7"


_ACTION_ROLES = {
    "draft": "drafting",
    "polish": "voice_edit",
    "assemble": "ledger",
    "ack": "final_review",
    "extend_plan": "planning",
    "story_review": "story_review",
}

_ACTION_CONTEXT_ISOLATION = {
    "extend_plan": "editorial_state",
}


_ACTION_ROLE_CARDS = {
    "draft": "drafting-editor.md",
    "polish": "voice-editor.md",
    "assemble": "ledger-editor.md",
    "ack": "final-reviewer.md",
    "extend_plan": "planning-editor.md",
    "story_review": "story-reviewer.md",
}


def _worker_brief(store: BookStore, payload: dict[str, Any]) -> dict[str, Any]:
    """Path-only handoff: fresh single-stage jobs by default, explicit chapter compatibility."""
    action = str(payload.get("action") or "")
    skill_root = Path(__file__).resolve().parents[4]  # pipeline包比单文件深一级
    base = {
        "worker_protocol": WORKER_PROMPT_PROTOCOL,
        "protocol_card": str(skill_root / "agents" / "roles" / "worker-protocol.md"),
        "protocol_hint": (
            "loop/hygiene/stop rules live in the protocol_card — read it FIRST "
            "(batch with the current role card + stage pack), follow the section named by 'protocol'"
        ),
        "project": str(store.project),
        "skill_root": str(skill_root),
        "cli": str(skill_root / "scripts" / "novel_ledger.py"),
        "role_cards_dir": str(skill_root / "agents" / "roles"),
    }
    if action == "extend_plan":
        return {
            **base,
            "spawn": "one disposable worker subagent for THIS plan extension only",
            "protocol": "plan",
            "suggest_from": payload.get("suggest_from"),
            "brief_path": str(store.staging_dir / "plan-extend-brief.json"),
            "brief_note": (
                "当次扩纲的机器载荷（suggest_from/extend_through_ch/overdue_hooks/volume_watermark）在"
                " brief_path；本次扩纲必须覆盖到 extend_through_ch，不贴水位线补章；"
                "职责以协议卡 §三 + 该文件为准，不依赖派发 prompt 的记忆"
            ),
            "role_cards": {"creative": "creative-editor.md", "planning": "planning-editor.md"},
            "stop_on": ["plan extend failure"],
        }
    if store.load_config().get("execution_mode") == "stage-agent" or action == "story_review":
        import uuid

        action_path = store.staging_dir / f"stage-action-{int(payload['chapter']):04d}-{action}.json"
        # 记账句柄随信封走：宿主不再手造 request-id（stage-agent 实战里因"信封缺
        # job_id/session_id"整程 telemetry=unknown）。信封每次派发重写、句柄随写随新，
        # 宿主用 worker 实际读到的这份信封回声即天然幂等。
        usage_request = {
            "request_id": f"{action}-ch{int(payload['chapter']):04d}-{uuid.uuid4().hex[:12]}",
            "session_id": None,
            "shape": "components_or_total",
            "record_hint": (
                "after this stage's model response settles, the HOST records it: chapter "
                "usage-record --request-id <request_id> --stage <stage> --chapter N, with "
                "either the four component counters or --total-tokens when the host only sees "
                "a whole-job total (both shapes are legal); re-echoing the same request-id "
                "dedupes instead of double-counting"
            ),
        }
        envelope = {k: v for k, v in payload.items() if k not in {"worker", "execution"}}
        envelope["usage_request"] = usage_request
        atomic_json(action_path, envelope)
        return {
            **base, "spawn": "one fresh, empty-context worker for THIS stage only; never fork history",
            "protocol": "stage", "chapter": payload.get("chapter"),
            "action": action, "action_path": str(action_path),
            "usage_request": usage_request,
            "role_card": str(skill_root / "agents" / "roles" / _ACTION_ROLE_CARDS[action]),
            "resume_from": {"action": action, "phase": payload.get("phase")},
            "stop_on": ["stage submitted", "phase changed", "blocked", "usage_guard"],
            "context_origin": "empty",
        }
    return {
        **base, "spawn": "one disposable worker subagent for THIS chapter only",
        "protocol": "chapter",
        "chapter": payload.get("chapter"),
        "resume_from": {"action": action, "phase": payload.get("phase")},
        "role_cards": dict(_ACTION_ROLE_CARDS),
        "stop_on": ["blocked", "usage_guard"],
    }


def _with_execution_contract(payload: dict[str, Any], store: BookStore) -> dict[str, Any]:
    """Declare the host session contract, without claiming to create a model session.

    The host owns creation of empty model conversations. Fresh-session context and
    filesystem access are separate boundaries; inline/worker-agent only isolate
    logical views.
    """
    role = _ACTION_ROLES.get(str(payload.get("action") or ""))
    if role is None:
        return payload
    if payload.get("action") in {"draft", "assemble"}:
        review_path = store.staging_dir / f"review-findings-{int(payload['chapter']):04d}.json"
        if review_path.exists():
            payload["review_findings_path"] = str(review_path)
    configured = str(store.load_config().get("execution_mode") or "inline")
    fresh_required = configured == "stage-agent" or payload.get("action") == "story_review"
    mode = configured
    execution: dict[str, Any] = {
        "mode": mode,
        "role": role,
        "spawn_allowed": fresh_required or mode in {"worker-agent", "stage-agent"},
        "context_isolation": "fresh_session" if fresh_required else _ACTION_CONTEXT_ISOLATION.get(str(payload.get("action") or ""), "stage_pack"),
        "history_inheritance_allowed": not fresh_required,
        "session_scope": "stage" if fresh_required else "chapter",
        "filesystem_isolation": "host_sandbox_required",
        "preferred_tier": "standard",
    }
    if str(payload.get("action") or "") in {"extend_plan", "ack", "story_review"}:
        # plan job 的档位要求随信封走：宿主派发路径上这个字段是机器可读的档位缺口
        # ——宿主不能按 spawn 切模型时它如实可见（继承当前会话模型并在状态卡记录），
        # 而不是只活在派发手册 §9 的纪律里。
        execution["preferred_tier"] = "strongest"
    payload["execution"] = execution
    payload["context_isolation_required"] = fresh_required
    if fresh_required or mode in {"worker-agent", "stage-agent"}:
        payload["worker"] = _worker_brief(store, payload)
    return payload


# 钩子清单/自检清单）进宿主上下文就是「调度层上下文一直叠加」的主源头（实测单阶段
# 信封 2–5KB，每章 ×4 阶段，章章累加且每轮重播）。信封全量已在 stage-action 文件里，
# worker 按路径自取；宿主只需要动作、相位、停止位与派发指针。
_DISPATCH_CARD_KEYS = ("ok", "action", "chapter", "phase", "stop", "blocked", "pack_hash", "verdict")
_WORKER_CARD_KEYS = (
    "spawn", "protocol", "chapter", "action", "action_path", "role_card",
    "brief_path", "brief_note", "usage_request", "stop_on", "context_origin",
)


def _dispatch_card(payload: dict[str, Any], *, store: "BookStore | None" = None) -> dict[str, Any]:
    """宿主调度视图：只留决策与派发要用的字段，信封全量留在盘上。

    兜底：信封没落盘（inline/worker-agent 会话自己执行、无 stage-action 文件）时
    不能裁——那正是执行者本人的载荷；原样返回并注明回退原因。
    """
    worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
    envelope_path = worker.get("action_path")
    if not envelope_path or not Path(str(envelope_path)).exists():
        return {**payload, "card_fallback": "envelope not on disk (executor session); full payload returned"}
    card = {key: payload[key] for key in _DISPATCH_CARD_KEYS if key in payload}
    if "head_transition" in payload:
        card["head_transition"] = payload["head_transition"]
    if "review_findings_path" in payload:
        card["review_findings_path"] = payload["review_findings_path"]
    if worker:
        card["worker"] = {key: worker[key] for key in _WORKER_CARD_KEYS if key in worker}
    card["envelope_path"] = envelope_path
    # 规范化调用行：宿主曾中途在 .dsh / .zcode 两个安装路径间漂移（junction 同内容，
    # 但跨宿主解析不稳定）。卡上给出运行中实例自己的绝对调用行，宿主逐字复制，
    # 不再手写解析 SKILL_ROOT。
    cli_path = Path(__file__).resolve().parents[3] / "novel_ledger.py"
    card["cli_invocation"] = f'py -3 "{cli_path}" --project "{store.project}"'
    card["card_note"] = (
        "full envelope lives at envelope_path (the worker reads it from disk); "
        "dispatcher: paste only card paths into the spawn prompt, never the envelope; "
        "run host-side commands with cli_invocation verbatim"
    )
    return card


__all__ = [
    'WORKER_PROMPT_PROTOCOL',
    '_ACTION_ROLES',
    '_ACTION_CONTEXT_ISOLATION',
    '_ACTION_ROLE_CARDS',
    '_worker_brief',
    '_with_execution_contract',
    '_DISPATCH_CARD_KEYS',
    '_WORKER_CARD_KEYS',
    '_dispatch_card',
]
