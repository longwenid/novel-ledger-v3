from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..content.hierarchical_memory import load_hierarchical_memory
from ..infra.scale import CHAPTER_WORD_TARGET, chapter_word_target_for_band, derive_chapter_word_target
from ..infra.store import BookStore, _volume_label, volume_entry_for
from ..infra.util import (
    LedgerError, atomic_json, atomic_text, canonical_json,
    chinese_word_count, now_ts, ok, read_json, sha256_text,
)
from ..ledger.ledger import _event_digest, coerce_due, load_snapshot
from .pipeline import (
    WORKER_PROMPT_PROTOCOL,
    audit_book,
    book_complete,
    chapter_next,
    record_usage,
    status as book_status,
)
from .stage_jobs import stage_progressed, record_session_receipt
from ..content.reviews import review_summary


DRIVER_SCHEMA = "novel-ledger.driver.v1"
AUTOPILOT_SCHEMA = "novel-ledger.autopilot.v1"
DEFAULT_LIMITS = {
    "chapter_timeout_seconds": 2700,
    # plan job 默认与无进度下限取现场实证值：一个 plan job 要在新会话里
    # 产出 phase brief + 最多 20 章章拍并跑通四层脊柱与规模合同，strongest 档常超
    # 1200s；且 no_progress 对 plan job 同样生效——plan worker 只在最后 plan extend
    # 落盘时 HEAD 才变化，"静默思考"超窗会被先杀。旧值只能跑中救火手改。
    "plan_timeout_seconds": 3600,
    # 无进展窗口按「可观察工件变化」计（HEAD/plan/quality/staging，见 _snapshot）。
    # 单章实测耗时随书变厚从早期 5–9 分钟涨到 ~15 分钟——草稿在写作末尾才落盘，
    # 900s 会在草稿写完的同一分钟杀 worker（双书实测后放宽），现取 1800。
    "no_progress_seconds": 1800,
    "max_infra_retries": 3,
    "retry_backoff_seconds": [30, 120, 600],
}
_PLACEHOLDERS = {"{prompt_file}", "{prompt}", "{project}", "{job_id}", "{result_file}"}
# validate-config 的限制告警阈值：显式配得比这更紧时亮告警（默认值已按实测放宽）。
PLAN_TIMEOUT_FLOOR_SECONDS = 3600
NO_PROGRESS_FLOOR_SECONDS = 1800
_MODEL_ACTIONS = {"draft", "polish", "assemble", "ack"}
_TERMINAL_ACTIONS = {"blocked", "usage_guard", "complete", "story_review_blocked"}
_IMMEDIATE_PAUSE_REASONS = {"blocked", "usage_guard", "contract_violation"}
_CONTRACT_ERROR_CODES = {
    "stage_session_unverified", "stage_session_reused", "stage_driver_not_isolated", "invalid_findings_journal",
    "book_scale_contract_violation", "volume_outline_contract_violation",
    "empty_plan",
    "event_spine_contract_violation",
    "invalid_autopilot_action",
    "invalid_head",
    "invalid_kb",
    "invalid_plan",
    "missing_chapter_plan",
    "invalid_story_reviews", "story_review_missing_chapter", "story_review_unacked_revision",
    "story_review_job_mismatch",
    "narrative_contract_source_drift", "narrative_contract_source_unbound", "narrative_contract_invalid",
    "narrative_contract_canon_unsynced", "narrative_contract_audit_invalid",
    "story_review_canon_unsynced", "story_review_required",
    "book_not_finished", "book_promises_unresolved", "editorial_review_pending", "completion_audit_failed", "book_target_not_reached",
}
HEARTBEAT_SECONDS = 10
LEASE_STALE_SECONDS = 90
PROCESS_TERM_GRACE_SECONDS = 10
WORKER_EXIT_GRACE_SECONDS = 10
POLL_SECONDS = 0.1
CHECKPOINT_CHAPTERS = 10
CHECKPOINT_ASSET_IDS = 12


def _worker_may_still_run(job: dict[str, Any]) -> bool:
    """孤儿检查：supervisor 未亲历退出且 pid 仍存活，才视为 worker 可能仍在跑。

    裸 pid 存活会被 Windows 的 pid 复用误报成"孤儿 worker 还活着"，把崩溃恢复
    卡在 orphan_worker_alive（满负荷机器上必现）。supervisor 亲历过退出
    （driver_handle.exited，由 _command_attempt 落盘）即不再信任同号 pid。
    """
    handle = job.get("driver_handle") or {}
    if handle.get("exited"):
        return False
    return _pid_alive(int(handle.get("pid") or 0))


def _pid_alive(pid: int) -> bool:
    """该 pid 是否仍在运行。

    Windows 上 `os.kill(pid, 0)` 对**已不存在**的 pid 抛的是 WinError 87
    （ERROR_INVALID_PARAMETER），而不是 POSIX 的 ProcessLookupError；不显式处理会让
    supervisor 的崩溃恢复与僵死租约回收直接抛错停摆。无权限错误（WinError 5）说明
    进程确实存在，按存活处理，避免误回收仍在写的另一个 supervisor。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # Windows：WinError 87 = ERROR_INVALID_PARAMETER，即 pid 不存在/已退出。
        if getattr(exc, "winerror", None) == 87:
            return False
        # 其余未知错误按"存活"保守处理：宁可暂停等人核对，也不并发写同一本书。
        return True
    return True


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = read_json(path)
    if not isinstance(value, dict):
        raise LedgerError("invalid_autopilot_state", f"expected a JSON object: {path}")
    return value


def _append_event(store: BookStore, event: str, **details: Any) -> None:
    payload = {"schema": AUTOPILOT_SCHEMA, "event": event, "ts": now_ts(), **details}
    store.autopilot_events_path.parent.mkdir(parents=True, exist_ok=True)
    from ..infra.util import append_bytes
    append_bytes(store.autopilot_events_path, canonical_json(payload))


def _write_state(store: BookStore, state: dict[str, Any]) -> dict[str, Any]:
    state = {**state, "schema": AUTOPILOT_SCHEMA, "updated_at": now_ts()}
    atomic_json(store.autopilot_state_path, state)
    return state


def _activate_job(store: BookStore, job: dict[str, Any], driver_kind: str) -> None:
    """Fence story mutations to one dispatched job under the normal write lock."""
    with store.exclusive_lock(), store.transaction():
        existing = _read_optional_json(store.autopilot_active_job_path)
        if existing and existing.get("job_id") != job["job_id"]:
            raise LedgerError(
                "autopilot_fence",
                "another unattended job still owns the story write fence",
                {"active_job": existing},
            )
        atomic_json(
            store.autopilot_active_job_path,
            {
                "schema": AUTOPILOT_SCHEMA,
                "job_id": job["job_id"],
                "kind": job["kind"],
                "target": job["target"],
                "initial_action": job.get("initial_action"),
                "isolation_required": job.get("isolation_required", False),
                "plan_fingerprint": sha256_text(canonical_json(store.load_plan()).decode("utf-8")),
                "output_path": job.get("output_path"),
                "review_id": job.get("review_id"),
                "review_input_hash": job.get("review_input_hash"),
                "driver_kind": driver_kind,
                "activated_at": now_ts(),
            },
        )


def _clear_active_job(store: BookStore, job_id: str) -> None:
    """Release only the caller's fence; never erase a newer worker's claim."""
    with store.exclusive_lock(), store.transaction():
        active = _read_optional_json(store.autopilot_active_job_path)
        if active is None:
            return
        if str(active.get("job_id") or "") != str(job_id):
            raise LedgerError(
                "autopilot_fence",
                "refusing to clear a different unattended job fence",
                {"expected_job_id": job_id, "active_job": active},
            )
        store.autopilot_active_job_path.unlink()


def validate_driver_config(path: Path) -> dict[str, Any]:
    raw = read_json(path)
    if not isinstance(raw, dict) or raw.get("schema") != DRIVER_SCHEMA:
        raise LedgerError(
            "invalid_driver_config",
            f"driver config schema must be {DRIVER_SCHEMA}",
        )
    driver = raw.get("driver")
    if not isinstance(driver, dict):
        raise LedgerError("invalid_driver_config", "driver must be an object")
    kind = str(driver.get("kind") or "").strip()
    if kind not in ("command", "antigravity-agentapi"):
        raise LedgerError(
            "invalid_driver_config",
            "driver.kind must be command or antigravity-agentapi",
        )
    normalized_driver: dict[str, Any] = {"kind": kind}
    policy = driver.get("session_policy")
    if policy not in (None, "fresh"):
        raise LedgerError("invalid_driver_config", "driver.session_policy must be fresh")
    if policy:
        normalized_driver["session_policy"] = policy
    if kind == "command":
        if driver.get("shell") not in (None, False):
            raise LedgerError("invalid_driver_config", "command driver never permits shell=true")
        argv = driver.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
            raise LedgerError("invalid_driver_config", "command driver.argv must be a non-empty string list")
        unknown = sorted(
            token
            for arg in argv
            for token in _extract_placeholders(arg)
            if token not in _PLACEHOLDERS
        )
        if unknown:
            raise LedgerError("invalid_driver_config", "unknown argv placeholder", unknown)
        normalized_driver["argv"] = list(argv)
        cwd = str(driver.get("cwd") or "{project}")
        unknown_cwd = [token for token in _extract_placeholders(cwd) if token != "{project}"]
        if unknown_cwd:
            raise LedgerError("invalid_driver_config", "driver.cwd only supports {project}")
        normalized_driver["cwd"] = cwd
    else:
        executable = str(driver.get("executable") or "agentapi").strip()
        if not executable:
            raise LedgerError("invalid_driver_config", "agentapi executable cannot be empty")
        normalized_driver["executable"] = executable

    limits_raw = raw.get("limits") or {}
    if not isinstance(limits_raw, dict):
        raise LedgerError("invalid_driver_config", "limits must be an object")
    limits = dict(DEFAULT_LIMITS)
    limits.update(limits_raw)
    for key in ("chapter_timeout_seconds", "plan_timeout_seconds", "no_progress_seconds"):
        value = _integer(limits.get(key), key, minimum=1)
        limits[key] = value
    limits["max_infra_retries"] = _integer(limits.get("max_infra_retries"), "max_infra_retries", minimum=0)
    backoff = limits.get("retry_backoff_seconds")
    if not isinstance(backoff, list) or not backoff:
        raise LedgerError("invalid_driver_config", "retry_backoff_seconds must be a non-empty list")
    limits["retry_backoff_seconds"] = [
        _integer(value, "retry_backoff_seconds", minimum=0) for value in backoff
    ]
    return {
        "schema": DRIVER_SCHEMA,
        "driver": normalized_driver,
        "limits": limits,
    }






def _integer(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise LedgerError("invalid_driver_config", f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LedgerError("invalid_driver_config", f"{field} must be an integer") from exc
    if parsed < minimum:
        raise LedgerError("invalid_driver_config", f"{field} must be >= {minimum}")
    return parsed


def _extract_placeholders(text: str) -> list[str]:
    import re

    return re.findall(r"\{[a-z_]+\}", text)


def build_worker_prompt(*, project: Path, job_id: str, kind: str, chapter: int, action: str | None = None) -> str:
    try:
        isolated_plan = kind == "plan" and BookStore(project).load_config().get("execution_mode") == "stage-agent"
    except LedgerError:
        isolated_plan = False
    if kind in {"stage", "review"} or isolated_plan:
        action = "extend_plan" if isolated_plan else action
        skill_root = Path(__file__).resolve().parents[3]
        return (
            f"使用 novel-ledger 完成第{chapter}章的唯一阶段 {action}。协议 {WORKER_PROMPT_PROTOCOL}。\n"
            f"PROJECT={project}\nJOB_ID={job_id}\n"
            f"只读 {project}/book/run/autopilot-current.action.json 中的 action 路径与对应角色卡。"
            f"协议卡：{skill_root}/agents/roles/worker-protocol.md {'§三' if isolated_plan else '§六' if kind == 'review' else '§二'}。"
            "宿主必须创建无历史继承的新模型会话；禁止 fork、resume、摘要注入、创建或派发子 agent。"
            "禁止运行 chapter next。按 action 写本阶段 staging 并直接提交；阶段通过、phase 改变、blocked 或 usage_guard 后："
            "先写上方的 result 文件（这是退出动作的必备部分，不写就走=本 job 失败），写完立即退出。"
            + ("plan job 先读 action 的 plan_worker_brief.path；仅在已签脊柱与预算授权内选批、起草 phase 并 plan extend，成功即退出。" if isolated_plan else "")
            + ("独立剧情复核只读 action.review_pack_path 与选定正文；补证据用 review story-evidence。逐项产出checks及正文引用，review story-submit后退出；不得修改正文/账本。" if kind == "review" else "")
            + f"CLI 一律带 --terse 和 NOVEL_LEDGER_JOB_ID={job_id}。逐请求记录 usage delta，request-id 必须唯一，禁止汇总重复计量。"
            f"结束时写 {project}/book/run/autopilot-current.result.json："
            f'{{"job_id":"{job_id}","action":"{action}","session_id":"宿主真实新会话ID","context_origin":"empty","status":"success"}}。'
            "session_id 必须由宿主提供，不得编造；不能证明新会话则停止。正文只在文件中，不回显。"
        )
    common = (
        f"使用 novel-ledger skill 执行一个有界无人值守任务。\n"
        f"协议版本：{WORKER_PROMPT_PROTOCOL}\n"
        f"PROJECT={project}\nJOB_ID={job_id}\n"
        "开工第一步核对：`chapter next` 返回的 worker 简报 worker_protocol 与本版本一致；"
        "不一致说明宿主装了新旧两个 skill 实例混跑，立即停止并如实报告，不要继续写作。"
        "先以 `chapter next` 恢复台面——状态真源在盘上，不做巡检式阅读；"
        "只读返回 action 指定的阶段视图。"
        "所有编辑角色必须在当前会话内按 execution 契约串行执行，禁止创建、派发或 fork 任何物理子 agent。"
        "不得浏览历史章节，不得把正文全文回显到对话。"
        "不要使用长期目标类 slash 命令。"
        f"所有会写入 novel-ledger 的命令必须以前缀 NOVEL_LEDGER_JOB_ID={job_id} 执行；"
        "没有该 job 凭据时不得写入。"
        "所有 novel-ledger CLI 调用一律加 `--terse`：剥掉的 worker/execution 编排信封是给"
        "派发方看的，你已持有本简报与角色卡，全量信封只会在你的上下文里每阶段重复堆积"
        "（实测每章 ~10K 字符的纯重复）。路径、verdict、hint 等工作字段在 terse 下全部保留。"
    )
    if kind == "plan":
        try:
            story = BookStore(project)
            config = story.load_config()
            pace = derive_chapter_word_target(story.load_plan(), config) or chapter_word_target_for_band(config.get("word_band"))
        except LedgerError:
            pace = CHAPTER_WORD_TARGET
        return common + (
            "本任务只完成当前 extend_plan：先读 book/staging/plan-extend-brief.json（当次扩纲的"
            "机器载荷：suggest_from/overdue_hooks/volume_watermark，含 batch_design 批次设计底座）。"
            "落拍前先做批次候选头脑风暴：按协议卡 §三 产出至少 2 个结构性不同的批次走势候选，"
            "落盘 book/editorial/plan-batch-candidates-<起>-<止>.json，运行 plan select-batch"
            "记录机器事实断言并把择优裁决封进账本（败因必填），再把胜选候选展开成逐场章拍；"
            "职责以协议卡 §三 与该文件为准。"
            "按四层事件脊柱签发一个最多"
            "20章的阶段并执行 plan extend；"
            "扩纲批跨入新卷时必须同批携带 volumes 载荷签卷（title/spine/goal/word_budget/"
            f"chapters_budget，chapters_budget == ceil(word_budget/{pace})）——brief 的"
            " volume_watermark.warn=true 或 creative_advisory 指出卷脊未签时尤其如此；"
            "brief 里的 overdue_hooks 是逾期伏笔，应优先排进新章拍的 effects.hooks 安排回收。"
            "plan extend 成功后立即结束会话；禁止启动或撰写下一章。"
        )
    return common + (
        f"本任务只处理第{chapter}章，从当前落盘 phase 恢复并完成 compact 流水线，直到 ack-read 成功。"
        "ack-read 返回 requires_new_session=true 后立即结束整个会话；禁止再次调用 chapter next，禁止进入下一章。"
        "若项目配置 session_mode=inline-compact，ack-read 会返回 requires_new_session=false："
        "此时可先压缩上下文（丢弃本章正文、只留 HEAD/账本指针）再继续 chapter next，"
        "但严禁把上一章正文回显进新任务。"
        "遇到 blocked 或 usage_guard 立即停止并如实报告，不得自行越权解锁。"
    )


def _snapshot(store: BookStore) -> dict[str, Any]:
    """无进展判定用的「可观察工件」快照。

    进度不只是状态机迁移：staging 里草稿/终稿/提交稿的落盘与更新同样是
    worker 活着的证据——写作中的 worker 不是挂死的 worker。快照键多一项
    不影响 `_job_succeeded`（它只读特定字段）。
    """
    head = store.read_head()
    plan = store.load_plan()
    chapters = [
        int(item.get("chapter") or 0)
        for item in (plan.get("chapters") or [])
        if isinstance(item, dict)
    ]
    quality_size = store.quality_log_path.stat().st_size if store.quality_log_path.exists() else 0
    staging: list[tuple[str, int, int]] = []
    if store.staging_dir.exists():
        for item in store.staging_dir.iterdir():
            if item.is_file():
                stat = item.stat()
                staging.append((item.name, stat.st_size, stat.st_mtime_ns))
        staging.sort()
    return {
        "status": head.get("status"),
        "phase": head.get("phase"),
        "chapter": int(head.get("chapter") or 0),
        "last_committed_ch": int(head.get("last_committed_ch") or 0),
        "last_acked_ch": int(head.get("last_acked_ch") or 0),
        "pack_hash": head.get("pack_hash"),
        "prose_hash": head.get("prose_hash"),
        "plan_max_chapter": max(chapters) if chapters else 0,
        "quality_size": quality_size,
        "staging": staging,
        "last_story_review": head.get("last_story_review"),
    }


def _job_succeeded(kind: str, target: int, before: dict[str, Any], current: dict[str, Any], action: str | None = None) -> bool:
    if kind == "review":
        marker = current.get("last_story_review") or {}
        return (marker.get("through") == target and marker.get("review_id") == before.get("expected_review_id")
                and marker.get("input_hash") == before.get("expected_review_input_hash")
                and marker != before.get("last_story_review"))
    if kind == "plan":
        return current["plan_max_chapter"] > before["plan_max_chapter"]
    if kind == "stage":
        return stage_progressed(str(action), target, current)
    return current["last_acked_ch"] >= target


def _pause_request(store: BookStore) -> dict[str, Any] | None:
    return _read_optional_json(store.autopilot_pause_path)


class SupervisorLease:
    def __init__(self, store: BookStore):
        self.store = store
        self.run_id = str(uuid.uuid4())
        self.owner_path = store.autopilot_lease_dir / "owner.json"
        self.started_at = now_ts()
        self._last_heartbeat = 0.0

    def __enter__(self) -> "SupervisorLease":
        self.store.run_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.store.autopilot_lease_dir.mkdir()
            except FileExistsError:
                owner = _read_optional_json(self.owner_path) or {}
                directory_mtime = int(self.store.autopilot_lease_dir.stat().st_mtime)
                heartbeat = int(owner.get("heartbeat_at") or owner.get("started_at") or directory_mtime)
                pid = int(owner.get("pid") or 0)
                if now_ts() - heartbeat > LEASE_STALE_SECONDS and not _pid_alive(pid):
                    shutil.rmtree(self.store.autopilot_lease_dir, ignore_errors=True)
                    continue
                raise LedgerError(
                    "autopilot_locked",
                    "another unattended supervisor owns this book",
                    {"lock": str(self.store.autopilot_lease_dir), "owner": owner},
                )
            self.heartbeat(force=True)
            return self
        raise LedgerError("autopilot_locked", "could not reclaim stale unattended supervisor lease")

    def heartbeat(self, *, force: bool = False) -> None:
        current = time.monotonic()
        if not force and current - self._last_heartbeat < HEARTBEAT_SECONDS:
            return
        self._last_heartbeat = current
        atomic_json(
            self.owner_path,
            {
                "schema": AUTOPILOT_SCHEMA,
                "run_id": self.run_id,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "heartbeat_at": now_ts(),
            },
        )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        owner = _read_optional_json(self.owner_path)
        if owner and owner.get("run_id") == self.run_id:
            shutil.rmtree(self.store.autopilot_lease_dir, ignore_errors=True)


def autopilot_status(store: BookStore) -> dict[str, Any]:
    state = _read_optional_json(store.autopilot_state_path) or {
        "schema": AUTOPILOT_SCHEMA,
        "status": "stopped",
        "current_job": None,
        "telemetry": "unknown",
    }
    return ok(
        action="run_status",
        autopilot=state,
        pause_requested=_pause_request(store),
        active_job=_read_optional_json(store.autopilot_active_job_path),
        lease_owner=_read_optional_json(store.autopilot_lease_dir / "owner.json"),
        book=book_status(store),
    )


# 交接卡的「下一动作」按 HEAD 相位推导：恢复会话读完即可执行，不做聊天考古。
_HANDOFF_NEXT_STEP = {
    "await_draft": "HEAD 已在 await_draft：读 book/staging/stage-action-*（或 chapter next）派发 draft worker",
    "await_polish": "HEAD 已在 await_polish：派发 polish worker（只写作模式不会走到此相位）",
    "await_assembly": "HEAD 已在 await_assembly：派发 assemble worker（先读 assemble_brief_path）",
    "await_ack": "HEAD 已在 await_ack：派发 ack 终审 worker（通读 chapter_path 后 ack-read）",
    "submitted": "chapter next 推进本章 commit 并取 ack action",
    "idle": "chapter next 取下一章 draft action；返回 extend_plan 时先派独立 plan worker 扩纲",
    "complete": "全书已 complete：按完本流程跑全书审计与终局复核",
}


def handoff_report(
    store: BookStore,
    *,
    host_session_start: bool = False,
) -> dict[str, Any]:
    """生成无人值守交接卡机器段并落盘 book/run/handoff.md。

    会话宿主（子 agent 形态）会被轮数/时长/配额中断，且中断点任意；恢复靠盘上 HEAD，
    但「下一步做什么、还有什么没结」散在多个文件里。本命令把机器可判定的事实收进
    一份 markdown（HEAD/下一动作/逾期与临期钩子/开放 findings/supervisor 状态/进度
    日志尾部），只读故事状态、不推进 HEAD；批目标与剩余轮预算等批次上下文由宿主
    追加到 book/run/unattended-log.md 的「## 交接」节。

    host_session_start：宿主**新会话**的第一件事。带它跑的 handoff 才重置宿主批界基线；
    批中收口的普通 handoff 不重置——否则「handoff 了但没结束会话」会把 boundary
    计数器自缴械（实测：23 章里宿主跑了 3 次批中 handoff，一次 boundary 都没点亮）。
    """
    book = book_status(store)
    chapter = int(book.get("chapter") or 0)
    phase = str(book.get("phase") or "")
    blocked = book.get("blocked")
    snap = load_snapshot(store)
    due_hooks: list[tuple[int, int, str, str]] = []
    for hook in snap.get("hooks") or []:
        if not isinstance(hook, dict):
            continue
        if str(hook.get("status") or "open").lower() not in {"open", "deferred"}:
            continue
        due = coerce_due(hook.get("due")) or 0
        if due and due <= chapter + 3:
            due_hooks.append((due if due >= chapter else 0, due, str(hook.get("id") or ""), str(hook.get("text") or "")))
    due_hooks.sort()
    reviews = review_summary(store)
    lines: list[str] = [
        "# 无人值守交接卡（机器段）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_ts()))}",
        f"- HEAD：chapter={chapter} phase={phase} last_acked={book.get('last_acked_ch')} "
        f"last_committed={book.get('last_committed_ch')} status={book.get('status')}"
        + (f" **blocked={blocked}**（先排查 blocked，再继续写作）" if blocked else ""),
        f"- 进度：计划剩余 {book.get('plan_remaining_count')} 章（计划至第 {book.get('plan_max_chapter')} 章）；"
        f"全书字数 {book.get('book_words_written')}/{book.get('book_words')}"
        f"（{int(float(book.get('book_words_progress') or 0) * 100)}%）",
        f"- 下一动作：{_HANDOFF_NEXT_STEP.get(phase, f'未知相位 {phase}：先跑 status 核对')}",
    ]
    if due_hooks:
        lines.append("- 逾期/临期钩子（open/deferred，due ≤ 第%d章＋3）：" % chapter)
        for _, due, ident, text in due_hooks[:20]:
            mark = "已逾期" if due < chapter else "临期"
            gap = f"{chapter - due} 章" if due < chapter else f"{due - chapter} 章后到期"
            lines.append(f"  - [{mark}·{gap}] {ident}：{text}（due 第{due}章）")
        if len(due_hooks) > 20:
            lines.append(f"  - 另有 {len(due_hooks) - 20} 条；用 hooks audit 查全量")
    else:
        lines.append(f"- 逾期/临期钩子：无（open/deferred 且 due ≤ 第{chapter + 3}章）")
    by_sev = reviews.get("by_severity") or {}
    lines.append(
        "- 开放 findings：BLOCKER {b} / WARNING {w} / NIT {n} / UNVERIFIABLE {u}"
        "（UNVERIFIABLE 必须逐条处置；用 review list 查明细）".format(
            b=by_sev.get("BLOCKER", 0), w=by_sev.get("WARNING", 0),
            n=by_sev.get("NIT", 0), u=by_sev.get("UNVERIFIABLE", 0),
        )
    )
    state = _read_optional_json(store.autopilot_state_path)
    if state:
        lines.append(
            f"- supervisor：{state.get('status')}"
            + (f"（pause_reason={state.get('pause_reason')}）" if state.get("pause_reason") else "")
            + "；恢复用 run resume"
        )
    log_path = store.run_dir / "unattended-log.md"
    if log_path.exists():
        tail = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if tail:
            lines.append(f"- 进度日志尾部（{log_path}）：")
            lines.extend(f"  > {l}" for l in tail[-10:])
    else:
        lines.append("- 进度日志：book/run/unattended-log.md 尚未创建（宿主形态长跑应每章 ack 后追加一行）")
    path = store.run_dir / "handoff.md"
    atomic_text(path, "\n".join(lines).strip() + "\n")
    _append_event(
        store, "run_handoff",
        chapter=int(book.get("last_acked_ch") or 0),
        host_session_start=host_session_start,
        handoff_path=str(path),
    )
    return ok(
        action="run_handoff",
        handoff_path=str(path),
        chapter=int(book.get("last_acked_ch") or 0),
        host_session_start=host_session_start,
        next_step=_HANDOFF_NEXT_STEP.get(phase, "unknown-phase"),
        blocked=blocked,
        due_hook_count=len(due_hooks),
        open_findings=reviews.get("pending_count"),
    )


def relay_prompt(store: BookStore, *, every_hours: int = 4) -> dict[str, Any]:
    """生成接力自动化的创建素材：定时器 prompt（自包含）+ 排程建议。

    平台铁律（实测）：定时任务拉起的会话**不能**再创建定时任务（报错
    "Cannot create a scheduled task inside a session that already belongs to a
    scheduled task"）。一次性「自续命」接力链因此不成立——被拉起的会话批末试图
    CronCreate 下一棒必然被拒，链条必断。唯一成立的结构是：作者主会话只建
    **一个** recurring 定时器，每次触发跑**一批**（批目标有界），从盘上 HEAD 续跑；
    会话内永不建自动化。

    **归属语义（实测）**：定时器的每次触发回到**创建它的那个会话**，不是新会话。
    因此「在哪个会话建自动化」决定了写作会话落在哪个工作区——书接力自动化必须
    由书工作区的会话创建，skill 工程/其他工程会话不得代建（否则 fire 会把写作
    带进错误的工作区）。每火之间靠批界闩锁（handoff 基线/acks_since_boundary）
    控制工作量与上下文增长，会话长期存在由宿主的上下文压缩兜底。

    本命令把接力 prompt 变成运行态的确定性产物：HEAD/相位/计划余量/批大小/路径
    全部现场取值，不靠宿主手写——手写模板曾混入非法的自建下一棒步骤，正是断链
    根因。同时把被拉起会话的禁用清单（CronCreate/CronList）与重叠护栏（locked 即退）
    写死在 prompt 里。
    """
    book = book_status(store)
    cfg = store.load_config()
    batch = int(cfg.get("host_batch_chapters") or 0) or 5
    every_hours = max(1, int(every_hours or 4))
    cli = Path(__file__).resolve().parents[2] / "novel_ledger.py"
    project = store.project
    title = str(store.load_plan().get("title") or "") or "未命名书"
    phase = str(book.get("phase") or "")
    run_dir = store.run_dir
    text = "\n".join([
        f"《{title}》无人值守批界接力——本会话由定时任务拉起，每次触发只跑一批（≤{batch} 章）。",
        "",
        "## 硬约束（先读；违反即断链或烧钱）",
        "1. 本会话属于定时任务：**禁止调用 CronCreate**（平台会拒绝：定时任务会话不能再创建"
        "定时任务）；**禁止调用 CronList**（只读轮询无意义且烧上下文）。你的「下一棒」已经"
        "存在——就是本定时器的下一次触发（触发会回到本会话）。批末收口后停止写作即可。",
        "2. 同书同时刻只允许一个宿主会话。开工先核对：若上一棒仍在写作（status 显示 locked"
        " 或活跃 job 未清），立即原样退出，不写任何文件。",
        "3. 到批界就收口停线，不要试图在本会话里一口气写完全书。",
        "",
        "## 步骤",
        f"1. 读交接卡：{run_dir / 'handoff.md'}（HEAD、下一动作、开放 findings、计划余量、进度日志尾部）。",
        f'2. 立批界基线（新会话第一件事）：py -3 "{cli}" --project "{project}" run handoff --host-session-start',
        "3. 章节循环直到批界，每阶段派一次性空上下文 worker 执行：",
        f'   NOVEL_LEDGER_JOB_ID=host-loop py -3 "{cli}" --project "{project}" chapter next --card',
        "   - draft/polish/assemble/ack 各派独立空上下文 worker（不继承本会话历史），只给 "
        "stage-action 文件路径、worker-protocol.md 与对应角色卡；worker 不回显正文、不运行 chapter next。",
        "   - 宿主记账：按信封 usage_request.request_id 执行 chapter usage-record --stage <stage>"
        " --chapter <n> --request-id <rid> --total-tokens <tokens>；记错用 chapter usage-void 作废，不重记。",
        "   - 提交返工按回执 recovery.route：delta_only / one_point_prose 走宿主零模型路径"
        "（rework-patch / 保稿重提），不派返工 worker；redraft_worker 才派新会话；"
        "blocked / usage_guard / story_review fix / review_required 即停并报告。",
        "   - chapter next 返回 extend_plan：派独立 plan worker（worker-protocol.md §三 + "
        "planning/creative-editor 角色卡 + staging 里的 plan-extend-brief.json），"
        "先 plan select-batch 择优再 plan extend。",
        "   - 每 10 章章界由宿主直接跑 run checkpoint（零模型）。",
        f"4. 批界收口（acks_since_boundary ≥ {batch}）：run handoff → 把批目标与裁决摘要追加到 "
        f"{run_dir / 'unattended-log.md'} 的「## 交接」节 → **停止写作**（下一次触发会回到本会话"
        "续跑下一批）。禁止在 boundary 下再开新章。",
        "5. 每章 ack 后在 unattended-log.md 追加一行进度（章号/字数/异常），不要攒到批末。",
        "6. 到 complete：跑 book audit 全书审计，报告「可完本」或 completion_audit_failed；"
        "此后定时触发的会话读到 completed 即空转退出，作者可删除本定时器。",
        "",
        "## 当前状态",
        f"HEAD：第 {book.get('chapter')} 章 phase={phase}；计划至第 {book.get('plan_max_chapter')} 章"
        f"（剩 {book.get('plan_remaining_count')} 章）；全书 {book.get('book_words_written')}/"
        f"{book.get('book_words')} 字。批大小 {batch} 章。",
        _HANDOFF_NEXT_STEP.get(phase, f"未知相位 {phase}：先跑 status 核对"),
    ])
    path = run_dir / "relay-prompt.md"
    atomic_text(path, text + "\n")
    _append_event(
        store, "run_relay_prompt",
        chapter=int(book.get("last_acked_ch") or 0),
        relay_prompt_path=str(path),
        every_hours=every_hours,
    )
    return ok(
        action="run_relay_prompt",
        relay_prompt_path=str(path),
        prompt=text,
        prompt_chars=len(text),
        creator=(
            "author session in the BOOK's workspace only（触发回到创建定时器的会话——"
            "在哪个会话建，写作就落在哪个工作区；skill/其他工程会话不得代建，"
            "被拉起的会话不得再建自动化）"
        ),
        schedule={
            "kind": "recurring",
            "every_hours": every_hours,
            "cron": "37 * * * *",
            "interval_unit": "hourly",
            "interval": every_hours,
        },
        constraints=[
            "one_recurring_automation_not_self_renewing_chain",
            "create_in_book_workspace_fires_return_to_owning_session",
            "fired_sessions_never_create_automations",
            "bounded_batch_per_fire",
            "skip_fire_when_previous_host_still_alive",
            "completed_book_fires_noop_then_author_deletes",
        ],
        chapter=int(book.get("last_acked_ch") or 0),
        next_step=_HANDOFF_NEXT_STEP.get(phase, "unknown-phase"),
    )


def request_pause(store: BookStore, reason: str) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise LedgerError("invalid_pause", "run pause requires a reason")
    payload = {"schema": AUTOPILOT_SCHEMA, "reason": reason, "requested_at": now_ts()}
    atomic_json(store.autopilot_pause_path, payload)
    _append_event(store, "pause_requested", reason=reason)
    return ok(action="run_pause", paused=True, reason=reason)


def resume_run(store: BookStore) -> dict[str, Any]:
    previous = _pause_request(store)
    with contextlib.suppress(FileNotFoundError):
        store.autopilot_pause_path.unlink()
    state = _read_optional_json(store.autopilot_state_path) or {}
    state.update({"status": "ready", "pause_reason": None})
    _write_state(store, state)
    _append_event(store, "resumed", chapter=int(store.read_head().get("last_acked_ch") or 0), previous_pause=previous)
    return ok(action="run_resume", resumed=True, previous_pause=previous)


# —— 会话宿主批界视图 ——
# 宿主长跑会话的上下文单调增长，每阶段 chapter next 都全量重读一次：50 章单会话实测
# 烧掉全 session 60% 的 token 体量。批界硬停（run handoff 收口本会话、接力新会话从盘上
# HEAD 续跑）是宿主上下文唯一的生命周期闸门。基线只认**真会话起点**：
# run_started / resumed（含接力），或带 host_session_start 的 run_handoff（新会话第一件事）。
# 批中收口的普通 handoff 不重置基线也不解除 boundary——boundary 是闩锁：
# handoff 只是收口动作，只有新会话才开始新计数；否则「handoff 了但继续跑」会让
# 计数器每次清零自缴械（实测 23 章连跑、boundary 零点亮）。
_HOST_BATCH_BASELINE_EVENTS = ("run_started", "resumed")
_HOST_BATCH_BOUNDARY_ACTIONS = {"draft", "extend_plan"}
_HOST_BATCH_TAIL_BYTES = 262_144


def _host_batch_baseline(store: BookStore) -> int | None:
    path = store.autopilot_events_path
    if not path.exists():
        return None
    baseline: int | None = None
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - _HOST_BATCH_TAIL_BYTES))
        blob = handle.read()
    for raw in blob.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # 尾窗切在半行上：跳过残行，前面的完整行仍可解
        if not isinstance(event, dict):
            continue
        name = event.get("event")
        if name in _HOST_BATCH_BASELINE_EVENTS:
            pass
        elif name == "run_handoff" and event.get("host_session_start"):
            pass
        else:
            continue
        chapter = event.get("chapter")
        if isinstance(chapter, int) and not isinstance(chapter, bool) and chapter >= 0:
            baseline = chapter
    return baseline


def host_batch_view(store: BookStore, *, action: str) -> dict[str, Any] | None:
    """调度卡的宿主批界节：bound 来自 config.host_batch_chapters（0=关闭）。

    boundary 是闩锁：acks_since_boundary ≥ bound 后即保持点亮（普通 handoff 不解除，
    只有新会话的 run_started/resumed/host_session_start handoff 重置基线）；
    只在 draft/extend_plan 决策点（即将投入新一章）置 boundary=true；
    没有基线事件时返回 baseline_missing，提示宿主先 run handoff --host-session-start 立基线。
    """
    try:
        bound = int(store.load_config().get("host_batch_chapters") or 0)
    except (TypeError, ValueError):
        bound = 0
    if bound <= 0:
        return None
    baseline = _host_batch_baseline(store)
    if baseline is None:
        return {
            "baseline_missing": True,
            "note": (
                "no session-start event carries a chapter yet; run "
                "`run handoff --host-session-start` once at host-session start to "
                "establish the batch boundary baseline"
            ),
        }
    acked = int(store.read_head().get("last_acked_ch") or 0)
    since = max(0, acked - baseline)
    view: dict[str, Any] = {"acks_since_boundary": since, "bound": bound}
    # 闩锁只在决策点（即将投入新一章）亮 boundary：章中相位先跑完当前章、在章界收口。
    if since >= bound and action in _HOST_BATCH_BOUNDARY_ACTIONS:
        view["boundary"] = True
        view["note"] = (
            "host batch boundary LATCHED: run `run handoff`, then END this host "
            "session — the relay session continues from disk HEAD. A plain handoff "
            "does NOT clear this flag; only a new session does. Do not start "
            "another chapter in this session."
        )
    else:
        view["boundary"] = False
    return view


def run_checkpoint(store: BookStore) -> dict[str, Any]:
    """会话宿主形态的独立批窗口检查点：与 supervisor 环内同一条确定性检查路。

    宿主在章界（每 10 章/卷界）直接调用——机器部分零模型调用，报告落盘 checkpoints/，
    宿主只读结论（review_required / blockers / 路径）；仅 review_required 才派 triage
    子代理处置阻断项。非到期章返回 skipped 不出报告（与环内 last_checkpoint_ch 游标
    共用，幂等）。响应里不带完整报告——报告可能带整窗摘要与证据（实测单份 110K），
    回显进宿主上下文就是检查点版的上下文税；全文在 checkpoint_path，triage 按需自取。
    """
    state = _read_optional_json(store.autopilot_state_path) or {}
    result = _run_due_checkpoint(store, state)
    if result is None:
        return ok(
            action="run_checkpoint",
            stop=False,
            skipped="not_due",
            last_checkpoint_ch=int(state.get("last_checkpoint_ch") or 0),
        )
    report = result.get("checkpoint")
    if isinstance(report, dict):
        result["checkpoint"] = {
            "chapter": report.get("chapter"),
            "kinds": report.get("kinds"),
            "review_required": report.get("review_required"),
            "blockers": report.get("blockers"),
            "note": "full report on disk at checkpoint_path; read it (or dispatch triage) by path",
        }
    return result


def _expand(value: str, values: dict[str, str]) -> str:
    result = value
    for token, replacement in values.items():
        result = result.replace(token, replacement)
    return result


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows CI not available here
            proc.terminate()
    try:
        proc.wait(timeout=PROCESS_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover
                proc.kill()
        proc.wait()


def _command_attempt(
    store: BookStore,
    config: dict[str, Any],
    *,
    job: dict[str, Any],
    prompt_path: Path,
    result_path: Path,
    before: dict[str, Any],
    heartbeat: Callable[[], None],
    on_handle: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    driver = config["driver"]
    values = {
        "{prompt_file}": str(prompt_path),
        "{prompt}": prompt_path.read_text(encoding="utf-8"),
        "{project}": str(store.project),
        "{job_id}": job["job_id"],
        "{result_file}": str(result_path),
    }
    argv = [_expand(arg, values) for arg in driver["argv"]]
    cwd = Path(_expand(driver.get("cwd", "{project}"), values))
    env = dict(os.environ)
    env.update(
        {
            "NOVEL_LEDGER_PROJECT": str(store.project),
            "NOVEL_LEDGER_JOB_ID": job["job_id"],
            "NOVEL_LEDGER_PROMPT_FILE": str(prompt_path),
            "NOVEL_LEDGER_RESULT_FILE": str(result_path),
        }
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_handle, tempfile.TemporaryFile(
        mode="w+", encoding="utf-8"
    ) as stderr_handle:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                text=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            failure = {
                "success": False,
                "dispatched": False,
                "reason": "dispatch_failed",
                "detail": str(exc),
                "argv_head": argv[0] if argv else "",
            }
            hint = _spawn_failure_hint(argv[0]) if argv else ""
            if hint:
                failure["hint"] = hint
            return failure
        try:
            on_handle({"pid": proc.pid})
        except Exception:
            _terminate(proc)
            raise
        exit_reported = False

        def _report_exit() -> None:
            # supervisor 亲历 worker 退出（terminate 收尸或 poll 判死）后如实落盘：
            # 之后同号 pid 的"存活"只能是复用，孤儿检查据此放行，不再误停机。
            nonlocal exit_reported
            if not exit_reported:
                exit_reported = True
                with contextlib.suppress(Exception):
                    on_handle({"pid": proc.pid, "exited": True})

        started = time.monotonic()
        last_progress = started
        completed_at: float | None = None
        latest = before
        timeout = config["limits"][
            "plan_timeout_seconds" if job["kind"] == "plan" else "chapter_timeout_seconds"
        ]
        no_progress = config["limits"]["no_progress_seconds"]
        while proc.poll() is None:
            heartbeat()
            pause = _pause_request(store)
            current = _snapshot(store)
            if current != latest:
                latest = current
                last_progress = time.monotonic()
            if _job_succeeded(job["kind"], job["target"], before, current, job.get("initial_action")):
                if completed_at is None:
                    completed_at = time.monotonic()
                if time.monotonic() - completed_at >= WORKER_EXIT_GRACE_SECONDS:
                    _terminate(proc)
                    _report_exit()
                    return {
                        "success": True,
                        "dispatched": True,
                        "reason": "state_completed",
                        "terminated": True,
                    }
                time.sleep(POLL_SECONDS)
                continue
            if current.get("phase") == "blocked":
                _terminate(proc)
                _report_exit()
                return {"success": False, "dispatched": True, "reason": "blocked", "terminated": True}
            if pause:
                _terminate(proc)
                _report_exit()
                return {"success": False, "dispatched": True, "reason": "pause_requested", "detail": pause}
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                _terminate(proc)
                _report_exit()
                return {"success": False, "dispatched": True, "reason": "timeout", "terminated": True}
            if time.monotonic() - last_progress >= no_progress:
                _terminate(proc)
                _report_exit()
                return {"success": False, "dispatched": True, "reason": "no_progress", "terminated": True}
            time.sleep(POLL_SECONDS)
        _report_exit()
        stdout_handle.seek(0)
        stderr_handle.seek(0)
        stdout = stdout_handle.read()
        stderr = stderr_handle.read()
        current = _snapshot(store)
        success = proc.returncode == 0 and _job_succeeded(job["kind"], job["target"], before, current, job.get("initial_action"))
        return {
            "success": success,
            "dispatched": True,
            "reason": "completed" if success else "no_progress",
            "exit_code": proc.returncode,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        }


def _agentapi_attempt(
    store: BookStore,
    config: dict[str, Any],
    *,
    job: dict[str, Any],
    prompt_path: Path,
    before: dict[str, Any],
    heartbeat: Callable[[], None],
    on_handle: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    executable = config["driver"]["executable"]
    env = dict(os.environ)
    try:
        dispatched = subprocess.run(
            [executable, "new-conversation", prompt_path.read_text(encoding="utf-8")],
            cwd=str(store.project),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "dispatched": False, "reason": "dispatch_failed", "detail": str(exc)}
    if dispatched.returncode != 0:
        return {
            "success": False,
            "dispatched": False,
            "reason": "dispatch_failed",
            "exit_code": dispatched.returncode,
            "stderr_tail": dispatched.stderr[-2000:],
        }
    handle = {"agentapi_output": dispatched.stdout.strip()[-1000:]}
    on_handle(handle)
    started = time.monotonic()
    last_progress = started
    latest = before
    timeout = config["limits"]["plan_timeout_seconds" if job["kind"] == "plan" else "chapter_timeout_seconds"]
    no_progress = config["limits"]["no_progress_seconds"]
    while True:
        heartbeat()
        pause = _pause_request(store)
        current = _snapshot(store)
        if current != latest:
            latest = current
            last_progress = time.monotonic()
        if _job_succeeded(job["kind"], job["target"], before, current, job.get("initial_action")):
            return {"success": True, "dispatched": True, "reason": "state_completed", **handle}
        if current.get("phase") == "blocked":
            return {"success": False, "dispatched": True, "reason": "blocked", "uncancellable": True}
        if pause:
            return {
                "success": False,
                "dispatched": True,
                "reason": "pause_requested",
                "uncancellable": True,
                "detail": pause,
            }
        if time.monotonic() - started >= timeout:
            return {"success": False, "dispatched": True, "reason": "timeout", "uncancellable": True}
        if time.monotonic() - last_progress >= no_progress:
            return {"success": False, "dispatched": True, "reason": "no_progress", "uncancellable": True}
        time.sleep(POLL_SECONDS)


def _load_worker_result(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = read_json(path)
    if not isinstance(value, dict):
        raise LedgerError("invalid_worker_result", "worker result must be a JSON object")
    return value


def _record_result_usage(
    store: BookStore,
    *,
    job: dict[str, Any],
    worker_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if job.get("kind") == "stage" or job.get("isolation_required"):
        result = worker_result or {}
        if "usage" in result:
            raise LedgerError("stage_usage_aggregate_forbidden", "stage receipts accept per-request usage_records only, never a session total")
        records = result.get("usage_records")
        if records is None:
            return None
        if not isinstance(records, list):
            raise LedgerError("invalid_worker_result", "usage_records must be a list of request deltas")
        latest = None
        for entry in records:
            if not isinstance(entry, dict) or not str(entry.get("request_id") or "").startswith(job["job_id"] + ":"):
                raise LedgerError("invalid_worker_result", "request_id must be job_id:request-sequence")
            latest = record_usage(store, stage=job["initial_action"], chapter=job["target"], usage=entry.get("usage") or {}, request_id=entry["request_id"], session_id=result.get("session_id"))
        return latest
    usage = (worker_result or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    return record_usage(
        store,
        stage=f"autopilot_{job['kind']}",
        usage=usage,
        chapter=job["target"],
        request_id=f"autopilot:{job['job_id']}",
        session_id=str((worker_result or {}).get("session_id") or "") or None,
    )


def _prepare_job(store: BookStore) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    with store.exclusive_lock(), store.transaction():
        action = chapter_next(store)
    name = str(action.get("action") or "")
    if name in _TERMINAL_ACTIONS:
        return None, action
    if name == "extend_plan":
        kind = "plan"
    elif name == "story_review":
        kind = "review"
    elif name in _MODEL_ACTIONS:
        kind = "stage" if store.load_config().get("execution_mode") == "stage-agent" else "chapter"
    else:
        raise LedgerError("invalid_autopilot_action", f"unsupported unattended action: {name}", action)
    target = int(action.get("chapter") or store.read_head().get("chapter") or 0)
    if target <= 0:
        raise LedgerError("invalid_autopilot_action", "unattended action has no target chapter", action)
    before = _snapshot(store)
    if kind == "review":
        before.update(expected_review_id=action["review_id"], expected_review_input_hash=action["input_hash"])
    return {
        "job_id": str(uuid.uuid4()),
        "kind": kind,
        "target": target,
        "initial_action": name,
        "output_path": action.get("submit_output_path"),
        "review_id": action.get("review_id"),
        "review_input_hash": action.get("input_hash"),
        "isolation_required": name == "story_review" or store.load_config().get("execution_mode") == "stage-agent",
        "before": before,
        "attempt": 0,
    }, action


def _backoff(config: dict[str, Any], retry_number: int) -> int:
    values = config["limits"]["retry_backoff_seconds"]
    return int(values[min(max(retry_number - 1, 0), len(values) - 1)])


def _jsonl_range(path: Path, first: int, last: int, *, chain: bool = False) -> tuple[list[dict[str, Any]], bool, int] | None:
    """Select complete range records; retain the predecessor for hash checking.

    The file is streamed so historical records do not become model context. A
    selected event is never lost because its text exceeds a byte-tail window.
    """
    if not path.exists():
        return None
    events: list[dict[str, Any]] = []
    invalid = 0
    previous = None
    started = False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                invalid += 1
                continue
            if not isinstance(event, dict):
                invalid += 1
                continue
            number = _checkpoint_int(event.get("chapter") or event.get("effective_chapter")) or 0
            relevant = first <= number <= last
            if chain and relevant and not started:
                if previous is not None:
                    events.append(previous)
                started = True
            if relevant or chain and started:
                events.append(event)
            previous = event
    return events, False, invalid


def _quality_tail(store: BookStore, first: int, last: int) -> dict[str, Any]:
    """Read the checkpoint range without clipping complete quality records."""
    loaded = _jsonl_range(store.quality_log_path, first, last)
    if loaded is None:
        return {"available": False, "truncated": False, "ack_pass": 0, "rework_chapters": 0}
    events, truncated, invalid = loaded
    acked: set[int] = set()
    reworked: set[int] = set()
    for event in events:
        number = _checkpoint_int(event.get("chapter")) or 0
        if not first <= number <= last:
            continue
        name = str(event.get("event") or "")
        verdict = str(event.get("verdict") or "")
        if name == "ack" and verdict == "pass":
            acked.add(number)
        if (name, verdict) in {
            ("style_machine", "fail"), ("submit", "rewrite"),
            ("polish_anchor", "fail"), ("plot_self_check", "fix"),
        }:
            reworked.add(number)
    return {
        "available": True,
        "truncated": truncated,
        "invalid_lines": invalid,
        "ack_pass": len(acked),
        "ack_chapters": sorted(acked),
        "rework_chapters": len(reworked),
    }


def _checkpoint_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hook_active(hook: dict[str, Any]) -> bool:
    return str(hook.get("status") or "open").lower() not in {"paid", "closed", "abandoned"}


def _hook_debt_state(snap: dict[str, Any], chapter: int) -> dict[str, Any]:
    overdue: list[str] = []
    overdue_count = 0
    open_hooks = 0
    for hook in snap.get("hooks") or []:
        if not isinstance(hook, dict) or not _hook_active(hook):
            continue
        open_hooks += 1
        due = _checkpoint_int(hook.get("due")) or 0
        if 0 < due < chapter:
            overdue_count += 1
            if len(overdue) < CHECKPOINT_ASSET_IDS:
                overdue.append(str(hook.get("id") or ""))
    return {
        "open_hooks": open_hooks,
        "overdue_hooks": overdue_count,
        "overdue_hook_ids": overdue,
        "open_debts": sum(
            1 for item in snap.get("debts") or []
            if isinstance(item, dict) and str(item.get("status") or "open").lower() == "open"
        ),
    }


def _ledger_tail(store: BookStore, first: int, last: int) -> dict[str, Any]:
    """Check new chapter events and their local hash links without replaying the book."""
    loaded = _jsonl_range(store.events_path, first, last, chain=True)
    if loaded is None:
        return {"available": False, "truncated": False, "chapters": [], "issues": []}
    events, truncated, invalid = loaded
    issues: list[dict[str, Any]] = []
    if invalid:
        issues.append({"code": "invalid_event_json", "count": invalid})
    chapters: dict[int, int] = {}
    for index, event in enumerate(events):
        number = _checkpoint_int(event.get("chapter")) or 0
        if first <= number <= last:
            chapters[number] = chapters.get(number, 0) + 1
        stored = str(event.get("hash") or "")
        if not stored or stored != _event_digest(event):
            issues.append({"code": "event_hash_invalid", "chapter": number})
        if index and str(event.get("prev_hash") or "") != str(events[index - 1].get("hash") or ""):
            issues.append({"code": "event_chain_broken", "chapter": number})
    for number in range(first, last + 1):
        if chapters.get(number) != 1:
            issues.append({"code": "chapter_event_count", "chapter": number, "count": chapters.get(number, 0)})
    return {
        "available": True,
        "truncated": truncated,
        "chapters": sorted(chapters),
        "issues": issues,
    }


def _checkpoint_report(
    store: BookStore, *, chapter: int, previous: int, kinds: tuple[str, ...]
) -> dict[str, Any]:
    """A chapter-range checkpoint; volume review is a signal for editorial judgment."""
    plan = store.load_plan()
    planned = {
        number: item for item in plan.get("chapters") or []
        if isinstance(item, dict) and (number := _checkpoint_int(item.get("chapter"))) is not None
    }
    first = max(previous + 1, chapter - CHECKPOINT_CHAPTERS + 1)
    blockers: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    if previous < first - 1:
        advisories.append({"code": "prior_chapters_unchecked", "count": first - previous - 1})
    recent_summaries: list[dict[str, Any]] = []
    for number in range(first, chapter + 1):
        folder = _volume_label((planned.get(number) or {}).get("volume", 1))
        stem = f"ch-{number:04d}"
        paths = {
            "prose": store.chapters_dir / folder / f"{stem}.md",
            "meta": store.chapters_dir / folder / f"{stem}.meta.json",
            "summary": store.summaries_dir / folder / f"{stem}.json",
            "ack": store.acks_dir / folder / f"{stem}.json",
        }
        missing = [name for name, path in paths.items() if not path.exists()]
        if missing:
            blockers.append({"code": "missing_chapter_artifacts", "chapter": number, "missing": missing})
            continue
        try:
            prose = paths["prose"].read_text(encoding="utf-8")
            meta = read_json(paths["meta"])
            summary = read_json(paths["summary"])
            ack = read_json(paths["ack"])
            if not all(isinstance(value, dict) for value in (meta, summary, ack)):
                raise ValueError("meta, summary and ack must be JSON objects")
        except (OSError, UnicodeError, LedgerError, ValueError) as exc:
            blockers.append({"code": "unreadable_chapter_artifacts", "chapter": number, "detail": str(exc)[:160]})
            continue
        hashes = {
            "sha256:" + sha256_text(prose),
            "sha256:" + sha256_text(prose[:-1] if prose.endswith("\n") else prose),
        }
        if (
            meta.get("prose_hash") not in hashes
            or summary.get("prose_hash") not in hashes
            or ack.get("prose_hash") not in hashes
        ):
            # 诊断回执：点名哪几份 artifact 陈旧 + 指路唯一合法修复（resync 全量重盖），
            # 不让宿主翻库挖哈希（实测：宿主手改 DB 里的 summary 行才解锁）。
            stale = [
                name
                for name, value in (
                    ("meta", meta.get("prose_hash")),
                    ("summary", summary.get("prose_hash")),
                    ("ack", ack.get("prose_hash")),
                )
                if value not in hashes
            ]
            blockers.append({
                "code": "prose_hash_mismatch",
                "chapter": number,
                "stale_artifacts": stale,
                "disk_hash": "sha256:" + sha256_text(prose),
                "hint": "prose was edited after these artifacts were written; run "
                        "`book resync-baseline` to restamp meta/summary/ack hashes from disk "
                        "prose, then re-run `run checkpoint`",
            })
        if (
            _checkpoint_int(meta.get("chapter")) != number
            or _checkpoint_int(summary.get("chapter")) != number
            or _checkpoint_int(ack.get("chapter")) != number
            or ack.get("verdict") != "pass"
            or ack.get("review_status") == "needs_review"
        ):
            blockers.append({"code": "chapter_receipt_mismatch", "chapter": number})
        if str(meta.get("l1_summary") or "") != str(summary.get("l1_summary") or ""):
            blockers.append({"code": "summary_mismatch", "chapter": number})
        if not str(summary.get("l1_summary") or "").strip():
            blockers.append({"code": "empty_summary", "chapter": number})
        if _checkpoint_int(meta.get("word_count")) != chinese_word_count(prose):
            blockers.append({"code": "word_count_mismatch", "chapter": number})
        quotes = ack.get("quotes")
        if not isinstance(quotes, list) or any(
            not isinstance(quote, str) or quote not in prose for quote in quotes
        ):
            blockers.append({"code": "ack_quote_missing", "chapter": number})
        recent_summaries.append({
            "chapter": number,
            "summary": str(summary.get("l1_summary") or ""),
        })

    quality = _quality_tail(store, first, chapter)
    editorial = review_summary(store)
    if editorial["by_severity"]["UNVERIFIABLE"] or editorial["by_severity"]["BLOCKER"]:
        blockers.append({"code": "editorial_findings_unresolved", **editorial})
    if editorial["by_severity"]["WARNING"] or editorial["by_severity"]["NIT"]:
        advisories.append({"code": "editorial_findings_triage", **editorial})
    if not quality["available"]:
        blockers.append({"code": "missing_quality_trace"})
    elif quality["invalid_lines"]:
        blockers.append({"code": "quality_log_invalid", "count": quality["invalid_lines"]})
    else:
        missing_acks = sorted(set(range(first, chapter + 1)) - set(quality["ack_chapters"]))
        if missing_acks:
            blockers.append({"code": "quality_ack_missing", "chapters": missing_acks})
    if quality["rework_chapters"]:
        advisories.append({"code": "rework_in_window", "chapters": quality["rework_chapters"]})

    ledger = _ledger_tail(store, first, chapter)
    if not ledger["available"]:
        blockers.append({"code": "missing_ledger_events"})
    if ledger["issues"]:
        blockers.append({"code": "ledger_window_invalid", "issues": ledger["issues"]})

    assets: dict[str, Any] = {}
    try:
        snap = load_snapshot(store)
        if int(snap.get("chapter") or 0) != chapter:
            blockers.append({"code": "snapshot_chapter_mismatch", "snapshot_chapter": snap.get("chapter")})
        hook_debt = _hook_debt_state(snap, chapter)
        assets = {
            "open_hooks": hook_debt["open_hooks"],
            "overdue_hooks": hook_debt["overdue_hooks"],
            "open_debts": hook_debt["open_debts"],
            "items": len(snap.get("items") or []),
            "conditions": len(snap.get("conditions") or []),
            "relations": len(snap.get("relations") or []),
        }
        if hook_debt["overdue_hooks"]:
            blockers.append({"code": "overdue_hooks", "count": hook_debt["overdue_hooks"], "ids": hook_debt["overdue_hook_ids"]})
    except LedgerError as exc:
        blockers.append({"code": "snapshot_unreadable", "detail": str(exc)[:160]})

    # 批次头脑风暴回看（每 10 章的 batch 检查点）：本窗口内的 plan.batch_select
    # 治理事件 + 待写批次的留痕覆盖核对。缺留痕 = 该批扩纲没做候选择优，停线等人。
    batch_plan_review: dict[str, Any] = {}
    if "batch" in kinds:
        from ..ledger.ledger import read_events

        selections: list[dict[str, Any]] = []
        try:
            for event in read_events(store):
                if not isinstance(event, dict) or event.get("type") != "governance":
                    continue
                if event.get("action") != "plan.batch_select":
                    continue
                selections.append({
                    "effective_chapter": int(event.get("effective_chapter") or 0),
                    "batch_from": int(event.get("batch_from") or 0),
                    "batch_to": int(event.get("batch_to") or 0),
                    "selected_id": str(event.get("selected_id") or ""),
                    "rationale": str(event.get("rationale") or ""),
                    "candidates_path": str(event.get("candidates_path") or ""),
                })
        except LedgerError:
            selections = []
        window = [s for s in selections if first <= s["effective_chapter"] <= chapter]
        unwritten = sorted(num for num in planned if num > chapter)
        first_unwritten = unwritten[0] if unwritten else None
        missing_from = None
        from ..content.planning import batch_selection_coverage
        selection_coverage = batch_selection_coverage(store, after_chapter=chapter)
        missing_from = selection_coverage.get("selection_missing_from")
        blockers.extend(selection_coverage["issues"])
        missing_artifacts = [
            {"batch_from": s["batch_from"], "batch_to": s["batch_to"]}
            for s in window
            if s["candidates_path"] and not _stored_artifact_exists(store, s["candidates_path"])
        ]
        if missing_artifacts:
            advisories.append({
                "code": "plan_candidates_artifact_missing",
                "batches": missing_artifacts,
            })
        batch_plan_review = {
            "selections_in_window": [
                {key: s[key] for key in ("batch_from", "batch_to", "selected_id", "rationale")}
                for s in window
            ],
            "next_unwritten": first_unwritten,
            "selection_missing_from": missing_from,
        }

    report: dict[str, Any] = {
        "schema": AUTOPILOT_SCHEMA,
        "chapter": chapter,
        "kinds": list(kinds),
        "range": {"from_chapter": first, "through_chapter": chapter},
        "recent_summaries": recent_summaries,
        "quality": quality,
        "editorial_review": editorial,
        "ledger": ledger,
        "assets": assets,
        "blockers": blockers,
        "advisories": advisories,
    }
    if "volume" in kinds:
        current_volume = _volume_label((planned.get(chapter) or {}).get("volume", 1))
        next_volume = _volume_label((planned.get(chapter + 1) or {}).get("volume", 1))
        current_meta = volume_entry_for(plan.get("volumes"), current_volume) or {}
        next_meta = volume_entry_for(plan.get("volumes"), next_volume) or {}
        rolled = ""
        try:
            hierarchy = load_hierarchical_memory(store)
            volume = next(
                (item for item in reversed(hierarchy.get("volumes") or []) if item.get("id") == current_volume),
                None,
            )
            if isinstance(volume, dict):
                rolled = str(volume.get("summary") or "")
        except LedgerError as exc:
            blockers.append({"code": "volume_memory_unreadable", "detail": str(exc)[:160]})
        from ..infra.volume_outline import volume_outline_view
        report["volume_review"] = {
            "volume_outlines": volume_outline_view(plan, current_volume),
            "current_volume": current_volume,
            "next_volume": next_volume,
            "current_spine": str(current_meta.get("spine") or ""),
            "current_goal": str(current_meta.get("goal") or ""),
            "next_spine": str(next_meta.get("spine") or ""),
            "rolled_summary": rolled,
            "review_topics": ["character choices and costs", "main and side plot payoffs", "clocks and world rules"],
            "mandatory_story_review": {"required": True, "dispatch": "chapter next", "role": "story_review"},
        }
        if not next_meta.get("spine") and (plan.get("volumes") or {}):
            blockers.append({"code": "next_volume_unsigned", "volume": next_volume})
    if batch_plan_review:
        report["batch_plan_review"] = batch_plan_review
    report["review_required"] = bool(blockers)
    return report


def _checkpoint_kinds(store: BookStore, chapter: int) -> tuple[str, ...]:
    if chapter <= 0:
        return ()
    kinds = ["batch"] if chapter % CHECKPOINT_CHAPTERS == 0 else []
    plan = store.load_plan()
    adjacent = {
        number: item for item in plan.get("chapters") or []
        if isinstance(item, dict)
        and (number := _checkpoint_int(item.get("chapter"))) in {chapter, chapter + 1}
    }
    if chapter + 1 in adjacent and _volume_label((adjacent.get(chapter) or {}).get("volume", 1)) != _volume_label(adjacent[chapter + 1].get("volume", 1)):
        kinds.append("volume")
    return tuple(kinds)


def _stored_artifact_exists(store: BookStore, address: str) -> bool:
    path = Path(address)
    try:
        relative = path.resolve().relative_to(Path(str(store.book)))
    except ValueError:
        return path.is_file()
    return (store.book / relative).is_file()


def _run_due_checkpoint(store: BookStore, state: dict[str, Any]) -> dict[str, Any] | None:
    chapter = int(store.read_head().get("last_acked_ch") or 0)
    previous = int(state.get("last_checkpoint_ch") or 0)
    kinds = _checkpoint_kinds(store, chapter)
    if chapter <= previous or not kinds:
        return None
    report = _checkpoint_report(store, chapter=chapter, previous=previous, kinds=kinds)
    path = store.run_dir / "checkpoints" / f"ch-{chapter:04d}-{uuid.uuid4().hex[:8]}.json"
    atomic_json(path, report)
    state["last_checkpoint"] = {
        "chapter": chapter, "path": str(path), "kinds": list(kinds),
        "review_required": report["review_required"], "blockers": report["blockers"],
    }
    _append_event(store, "quality_checkpoint", chapter=chapter, kinds=kinds, review_required=report["review_required"], path=str(path))
    if "volume" in kinds:
        _append_event(store, "volume_review", chapter=chapter, review_required=report["review_required"], path=str(path))
    if report["review_required"]:
        state.update({"status": "paused", "pause_reason": "review_required", "current_job": None})
        _write_state(store, state)
        _append_event(store, "paused", reason="review_required", checkpoint=str(path), blockers=report["blockers"])
        return ok(action="run_paused", stop=True, reason="review_required", checkpoint=report, checkpoint_path=str(path))
    state["last_checkpoint_ch"] = chapter
    _write_state(store, state)
    return ok(action="run_checkpoint", stop=False, checkpoint_path=str(path))


def _completion_audit_blockers(audit: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    review = audit.get("editorial_review") or {}
    if review.get("pending_count"):
        blockers.append({"code": "editorial_findings_unresolved", "count": review["pending_count"]})
    if not audit.get("ok") or audit.get("action") != "book_audit":
        blockers.append({"code": "audit_command_failed"})
    stats = audit.get("stats")
    if isinstance(stats, dict) and (_checkpoint_int(stats.get("total_chapters")) or 0) <= 0:
        blockers.append({"code": "empty_book"})
    if not audit.get("ledger_consistent"):
        blockers.append({"code": "ledger_inconsistent", "diffs": (audit.get("ledger_diffs") or [])[:CHECKPOINT_ASSET_IDS]})
    if audit.get("ledger_quote_consistent") is False:
        blockers.append({"code": "ledger_quotes_invalid"})
    for field in (
        "quote_issues", "glossary_issues", "hash_mismatch_chapters", "seam_issues",
        "ledger_hygiene_issues", "ledger_quote_invalid", "numeric_issues",
        "derived_drift", "derived_name_drift", "unresolved_long_term_commitments", "patch_review_pending",
    ):
        values = audit.get(field) or []
        if values:
            blockers.append({"code": field, "count": len(values)})
    for field in (
        "quote_invalid_count", "ledger_quote_invalid_count", "numeric_issue_count",
        "derived_drift_count", "derived_name_drift_count", "overdue_hooks_count",
    ):
        count = _checkpoint_int(audit.get(field)) or 0
        if count:
            blockers.append({"code": field, "count": count})
    for field in ("canon_drift", "canon_source_drift"):
        value = audit.get(field) or {}
        if isinstance(value, dict) and value.get("changed"):
            blockers.append({"code": field})
    outline = audit.get("outline_conformance") or {}
    issue_count = _checkpoint_int(outline.get("issue_count")) if isinstance(outline, dict) else None
    if issue_count:
        blockers.append({"code": "outline_conformance", "count": issue_count})
    return blockers


def run_one_job(
    store: BookStore,
    config: dict[str, Any],
    *,
    lease: SupervisorLease,
    state: dict[str, Any],
) -> dict[str, Any]:
    job, action = _prepare_job(store)
    if job is not None and job["kind"] == "review" and config["driver"].get("session_policy") != "fresh":
        raise LedgerError("stage_driver_not_isolated", "independent story review requires driver.session_policy=fresh in every execution mode")
    if job is None:
        name = action["action"]
        if name == "complete":
            audit = audit_book(store)
            blockers = _completion_audit_blockers(audit)
            try:
                snapshot = load_snapshot(store)
                overdue = _hook_debt_state(snapshot, int(snapshot.get("chapter") or 0))
                if overdue["overdue_hooks"]:
                    blockers.append({
                        "code": "overdue_hooks_snapshot",
                        "count": overdue["overdue_hooks"],
                        "ids": overdue["overdue_hook_ids"],
                    })
            except LedgerError as exc:
                blockers.append({"code": "snapshot_unreadable", "detail": str(exc)[:160]})
            audit_path = store.run_dir / f"completion-audit-{uuid.uuid4().hex[:8]}.json"
            atomic_json(audit_path, audit)
            state["completion_audit"] = {
                "path": str(audit_path), "passed": not blockers, "blockers": blockers,
            }
            if blockers:
                state.update({"status": "paused", "pause_reason": "completion_audit_failed", "current_job": None})
                _write_state(store, state)
                _append_event(store, "paused", reason="completion_audit_failed", audit_path=str(audit_path), blockers=blockers)
                return ok(
                    action="run_paused", stop=True, reason="completion_audit_failed",
                    audit_path=str(audit_path), blockers=blockers,
                )
            with store.exclusive_lock(), store.transaction():
                completion_head = store.read_head()
                if completion_head.get("status") != "completed" or completion_head.get("completion_kind") == "normal":
                    author_override = (completion_head.get("completion_kind") == "normal"
                                       and completion_head.get("completion_target_override") == int(store.load_config().get("book_words") or 0))
                    book_complete(store, actor="autopilot", reason="signed plan, narrative reviews and completion audit passed", override_target=author_override)
            state.update({"status": "completed", "pause_reason": None, "current_job": None})
            _write_state(store, state)
            _append_event(store, "completed", action=action, audit_passed=True, audit_path=str(audit_path))
            return ok(action="run_complete", stop=True, reason="complete", audit_path=str(audit_path))
        state.update({"status": "paused", "current_job": None, "pause_reason": name})
        _write_state(store, state)
        _append_event(store, "paused", reason=name, action=action)
        return ok(action="run_paused", stop=True, reason=name, detail=action)


    max_retries = int(config["limits"]["max_infra_retries"])
    for attempt in range(max_retries + 1):
        if _pause_request(store):
            state.update({"status": "paused", "pause_reason": "requested", "current_job": None})
            _write_state(store, state)
            return ok(action="run_paused", stop=True, reason="requested")
        if attempt:
            delay = _backoff(config, attempt)
            _append_event(store, "retry_wait", job_id=job["job_id"], attempt=attempt, seconds=delay)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                lease.heartbeat()
                if _pause_request(store):
                    state.update({"status": "paused", "pause_reason": "requested", "current_job": None})
                    _write_state(store, state)
                    return ok(action="run_paused", stop=True, reason="requested")
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            job = {**job, "job_id": str(uuid.uuid4()), "attempt": attempt}

        prompt = build_worker_prompt(
            project=store.project,
            job_id=job["job_id"],
            kind=job["kind"],
            chapter=job["target"],
            action=job.get("initial_action"),
        )
        atomic_json(store.run_dir / "autopilot-current.action.json", action)
        prompt_path = store.run_dir / "autopilot-current.prompt.md"
        result_path = store.run_dir / "autopilot-current.result.json"
        atomic_text(prompt_path, prompt)
        with contextlib.suppress(FileNotFoundError):
            result_path.unlink()
        state.update(
            {
                "status": "running",
                "current_job": {**job, "driver_kind": config["driver"]["kind"], "started_at": now_ts()},
                "pause_reason": None,
            }
        )
        _write_state(store, state)
        _activate_job(store, job, config["driver"]["kind"])
        _append_event(store, "job_started", job=state["current_job"])

        def on_handle(handle: dict[str, Any]) -> None:
            state["current_job"] = {**(state.get("current_job") or {}), "driver_handle": handle}
            _write_state(store, state)

        if config["driver"]["kind"] == "command":
            outcome = _command_attempt(
                store,
                config,
                job=job,
                prompt_path=prompt_path,
                result_path=result_path,
                before=job["before"],
                heartbeat=lease.heartbeat,
                on_handle=on_handle,
            )
        else:
            outcome = _agentapi_attempt(
                store,
                config,
                job=job,
                prompt_path=prompt_path,
                before=job["before"],
                heartbeat=lease.heartbeat,
                on_handle=on_handle,
            )
        worker_result = _load_worker_result(result_path)
        worker_status = str((worker_result or {}).get("status") or "")
        if not outcome.get("success") and worker_status in ("blocked", "usage_guard"):
            outcome = {**outcome, "reason": worker_status}
        _append_event(store, "job_finished", job_id=job["job_id"], attempt=attempt, outcome=outcome)
        if outcome.get("success"):
            try:
                record_session_receipt(store, job, worker_result)
                usage_result = _record_result_usage(store, job=job, worker_result=worker_result)
            except LedgerError as exc:
                outcome = {**outcome, "success": False, "reason": "contract_violation", "error": exc.as_dict()["error"]}
        if outcome.get("success"):
            _clear_active_job(store, job["job_id"])
            telemetry = "reported" if usage_result is not None else "unknown"
            state.update(
                {
                    "status": "ready",
                    "current_job": None,
                    "telemetry": telemetry,
                    "last_job": {**job, "finished_at": now_ts(), "outcome": outcome},
                    "jobs_completed": int(state.get("jobs_completed") or 0) + 1,
                }
            )
            if job["kind"] == "chapter" or (job["kind"] == "stage" and job["initial_action"] == "ack" and store.read_head().get("phase") == "idle"):
                state["chapters_completed"] = int(state.get("chapters_completed") or 0) + 1
            _write_state(store, state)
            if usage_result and usage_result.get("stop"):
                state.update({"status": "paused", "pause_reason": "usage_guard"})
                _write_state(store, state)
                _append_event(store, "paused", reason="usage_guard", usage=usage_result)
                return ok(action="run_paused", stop=True, reason="usage_guard", usage=usage_result)
            return ok(
                action="run_job_complete",
                stop=False,
                job=job,
                telemetry=telemetry,
                worker_result=worker_result,
            )
        if outcome.get("uncancellable") and outcome.get("dispatched"):
            state.update(
                {
                    "status": "paused",
                    "pause_reason": outcome.get("reason"),
                    "current_job": {**(state.get("current_job") or {}), "outcome": outcome},
                }
            )
            _write_state(store, state)
            _append_event(store, "paused", reason=outcome.get("reason"), uncancellable=True)
            return ok(action="run_paused", stop=True, reason=outcome.get("reason"), detail=outcome)
        if job.get("isolation_required") and _job_succeeded(job["kind"], job["target"], job["before"], _snapshot(store), job["initial_action"]):
            # A phase transition without a verified session/result is not a retry
            # of the old stage. Keep the fence and recovery record until the host
            # supplies its missing receipt, rather than silently advancing.
            state.update({"status": "paused", "pause_reason": "contract_violation", "current_job": {**(state.get("current_job") or {}), "outcome": outcome}})
            _write_state(store, state)
            return ok(action="run_paused", stop=True, reason="contract_violation", detail=outcome)
        _clear_active_job(store, job["job_id"])
        if outcome.get("reason") in _IMMEDIATE_PAUSE_REASONS:
            state.update(
                {"status": "paused", "pause_reason": outcome.get("reason"), "current_job": None}
            )
            _write_state(store, state)
            _append_event(store, "paused", reason=outcome.get("reason"), detail=outcome)
            return ok(action="run_paused", stop=True, reason=outcome.get("reason"), detail=outcome)
    state.update({"status": "paused", "pause_reason": outcome.get("reason"), "current_job": None})
    _write_state(store, state)
    _append_event(store, "paused", reason=outcome.get("reason"), retries_exhausted=True)
    return ok(action="run_paused", stop=True, reason=outcome.get("reason"), retries_exhausted=True)


def _recover_previous_job(store: BookStore, state: dict[str, Any]) -> dict[str, Any] | None:
    job = state.get("current_job")
    if not isinstance(job, dict):
        active = _read_optional_json(store.autopilot_active_job_path)
        if active is not None:
            return ok(action="run_paused", stop=True, reason="active_job_state_unknown", job=active)
        return None
    before = job.get("before")
    if job.get("isolation_required") and job.get("driver_kind") == "command" and _worker_may_still_run(job):
        return ok(action="run_paused", stop=True, reason="orphan_worker_alive", job=job)
    if isinstance(before, dict) and _job_succeeded(
        str(job.get("kind")), int(job.get("target") or 0), before, _snapshot(store), job.get("initial_action")
    ):
        worker_result = _load_worker_result(store.run_dir / "autopilot-current.result.json")
        try:
            record_session_receipt(store, job, worker_result)
            usage_result = _record_result_usage(store, job=job, worker_result=worker_result)
        except LedgerError as exc:
            return ok(action="run_paused", stop=True, reason=exc.code, error=exc.as_dict()["error"])
        _clear_active_job(store, str(job.get("job_id") or ""))
        telemetry = "reported" if usage_result is not None else "unknown"
        state.update(
            {
                "status": "ready",
                "current_job": None,
                "recovered_job": job.get("job_id"),
                "telemetry": telemetry,
                "jobs_completed": int(state.get("jobs_completed") or 0) + 1,
            }
        )
        if job.get("kind") == "chapter" or (job.get("kind") == "stage" and job.get("initial_action") == "ack" and store.read_head().get("phase") == "idle"):
            state["chapters_completed"] = int(state.get("chapters_completed") or 0) + 1
        if usage_result and usage_result.get("stop"):
            state.update({"status": "paused", "pause_reason": "usage_guard"})
            _write_state(store, state)
            _append_event(store, "paused", reason="usage_guard", usage=usage_result, recovered=True)
            return ok(action="run_paused", stop=True, reason="usage_guard", usage=usage_result)
        _write_state(store, state)
        _append_event(store, "job_recovered_complete", job_id=job.get("job_id"))
        return None
    if job.get("driver_kind") == "command" and _worker_may_still_run(job):
        return ok(action="run_paused", stop=True, reason="orphan_worker_alive", job=job)
    if job.get("driver_kind") == "antigravity-agentapi":
        return ok(action="run_paused", stop=True, reason="sidecar_worker_state_unknown", job=job)
    _clear_active_job(store, str(job.get("job_id") or ""))
    state.update({"status": "ready", "current_job": None, "recovered_from": job.get("job_id")})
    _write_state(store, state)
    _append_event(store, "job_recovered_resume", job_id=job.get("job_id"))
    return None


def _reset_unhonored_nudge(store: BookStore) -> None:
    """run 启动/续跑时清掉「未兑现」的低水位提示标记。

    `plan_low_water_nudged_at` 记录的是上次提示时的 max_planned，且只在计划增长到
    新值时才会再次提示。plan job 失败 → pause → run start 续跑后，若标记仍等于当前
    max_planned（提示没被任何一次成功的 plan job 兑现），chapter next 会跳过提示
    直接开写下一章，「扩纲优先」意图静默丢失，总编辑只能手改 HEAD 解卡
    。
    supervisor 是确定性写者，在写锁内重置标记是合法路径；
    只在「标记 == 当前 max_planned」时清——计划已增长过的旧标记保持原语义。
    """
    try:
        chapters = store.load_plan().get("chapters") or []
        max_planned = max(
            (int(i.get("chapter") or 0) for i in chapters if int(i.get("chapter") or 0) > 0),
            default=0,
        )
    except LedgerError:
        return
    if max_planned <= 0:
        return
    with store.exclusive_lock(), store.transaction():
        head = store.read_head()
        marker = head.get("plan_low_water_nudged_at")
        if marker is not None and int(marker or 0) == max_planned:
            head.pop("plan_low_water_nudged_at", None)
            head["updated_at"] = now_ts()
            store.write_head(head)
            _append_event(store, "plan_nudge_marker_reset", marker=max_planned)


def run_supervisor(
    store: BookStore,
    config: dict[str, Any],
    *,
    once: bool,
    max_chapters: int = 0,
) -> dict[str, Any]:
    if max_chapters < 0:
        raise LedgerError("invalid_args", "--max-chapters must be >= 0")
    if store.load_config().get("execution_mode") == "stage-agent" and config["driver"].get("session_policy") != "fresh":
        raise LedgerError("stage_driver_not_isolated", "stage-agent requires driver.session_policy=fresh; driver must create empty model conversations")
    with SupervisorLease(store) as lease:
        state = _read_optional_json(store.autopilot_state_path) or {
            "schema": AUTOPILOT_SCHEMA,
            "status": "ready",
            "telemetry": "unknown",
            "jobs_completed": 0,
            "chapters_completed": 0,
        }
        recovered = _recover_previous_job(store, state)
        if recovered is not None:
            state.update({"status": "paused", "pause_reason": recovered.get("reason")})
            _write_state(store, state)
            return recovered
        if store.read_head().get("completion_kind") == "early_close":
            state.update({"status": "stopped", "pause_reason": "author_early_close", "current_job": None})
            _write_state(store, state)
            _append_event(store, "run_stopped", reason="author_early_close", normal_completion=False)
            return ok(action="run_stopped", stop=True, reason="author_early_close", normal_completion=False)
        started_acked = int(store.read_head().get("last_acked_ch") or 0)
        _reset_unhonored_nudge(store)
        _append_event(store, "run_started", run_id=lease.run_id, once=once, max_chapters=max_chapters,
                      chapter=started_acked)
        pending_checkpoint = _run_due_checkpoint(store, state)
        if pending_checkpoint and pending_checkpoint.get("stop"):
            return pending_checkpoint
        while True:
            if _pause_request(store):
                state.update({"status": "paused", "pause_reason": "requested"})
                _write_state(store, state)
                return ok(action="run_paused", stop=True, reason="requested")
            try:
                result = run_one_job(store, config, lease=lease, state=state)
            except LedgerError as exc:
                if exc.code not in _CONTRACT_ERROR_CODES:
                    raise
                state.update(
                    {
                        "status": "paused",
                        "pause_reason": "contract_violation",
                        "current_job": None,
                        "contract_error": exc.as_dict()["error"],
                    }
                )
                _write_state(store, state)
                _append_event(store, "paused", reason="contract_violation", error=exc.as_dict()["error"])
                return ok(
                    action="run_paused",
                    stop=True,
                    reason="contract_violation",
                    error=exc.as_dict()["error"],
                )
            if result.get("action") == "run_job_complete" and (result.get("job", {}).get("kind") == "chapter" or result.get("job", {}).get("initial_action") == "ack"):
                checkpoint = _run_due_checkpoint(store, state)
                if checkpoint is not None:
                    if checkpoint.get("stop"):
                        return checkpoint
                    result["checkpoint_path"] = checkpoint["checkpoint_path"]
            if result.get("stop") or once:
                return result
            completed_this_run = int(store.read_head().get("last_acked_ch") or 0) - started_acked
            if max_chapters and completed_this_run >= max_chapters:
                state.update({"status": "stopped", "pause_reason": "max_chapters"})
                _write_state(store, state)
                _append_event(store, "run_stopped", reason="max_chapters", chapters=completed_this_run)
                return ok(
                    action="run_stopped",
                    stop=True,
                    reason="max_chapters",
                    chapters_completed=completed_this_run,
                )


def validate_config_response(path: Path) -> dict[str, Any]:
    config = validate_driver_config(path)
    warnings = executable_resolution_warnings(config["driver"])
    warnings.extend(_limit_fit_warnings(config))
    payload = ok(action="run_validate_config", config=config)
    if warnings:
        codes = {str(item.get("code") or "") for item in warnings}
        hints = []
        if codes & {"driver_executable_not_found", "driver_executable_is_shell_shim"}:
            hints.append(
                "argv[0] cannot be launched as written on this host; run start would fail at "
                "dispatch time with dispatch_failed"
            )
        if codes & {"plan_job_timeout_tight", "plan_job_no_progress_tight"}:
            hints.append(
                "plan-job limits look tighter than field-proven floors; expect repeated "
                "plan-job timeouts/no_progress kills"
            )
        payload["executable_warnings"] = warnings
        payload["hint"] = "driver config is schema-valid, but some settings are unlikely to hold: " + "; ".join(hints)
    return payload


def _limit_fit_warnings(config: dict[str, Any]) -> list[dict[str, Any]]:
    """配置语义上立得住、运行期却大概率不够用的限制项，在校验期亮出来。

    plan job 要在一个新会话里产出完整 phase brief（10 个必填
    字段 + 事件引用）+ 最多 20 章 × 5 场戏的章拍，并跑通 plan extend 的四层脊柱与
    规模合同校验，strongest 档 1200s 偏紧；且 no_progress 对 plan job 同样生效，
    plan worker 落盘前 HEAD 不变，静默超时会被先杀。总编辑只能跑中救火改 3600/900。
    这类「纯配置决定」应该在开跑前可见。只提示，不阻断。
    """
    warnings: list[dict[str, Any]] = []
    if (config.get("driver") or {}).get("kind") == "command":
        warnings.append({
            "code": "external_driver_model_unmetered",
            "message": (
                "a command driver spawns external processes whose model and billing follow "
                "that CLI's own account defaults — the skill no longer feeds model parameters. "
                "Prefer the session-subagent unattended form (references/unattended-subagent.md): "
                "workers inherit the session model and usage is metered by the host session"
            ),
        })
    limits = config.get("limits") or {}

    def _int(key: str) -> int:
        try:
            return int(limits.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    plan_timeout = _int("plan_timeout_seconds")
    if plan_timeout and plan_timeout < PLAN_TIMEOUT_FLOOR_SECONDS:
        warnings.append({
            "code": "plan_job_timeout_tight",
            "plan_timeout_seconds": plan_timeout,
            "recommended_floor_seconds": PLAN_TIMEOUT_FLOOR_SECONDS,
            "message": (
                "a plan job signs one phase brief plus up to 20 chapters of beats and passes the "
                "four-layer spine + scale contract in one session; strongest-tier models have "
                f"repeatedly needed more than {plan_timeout}s — consider raising "
                "limits.plan_timeout_seconds"
            ),
        })
    no_progress = _int("no_progress_seconds")
    if no_progress and no_progress < NO_PROGRESS_FLOOR_SECONDS:
        warnings.append({
            "code": "plan_job_no_progress_tight",
            "no_progress_seconds": no_progress,
            "recommended_floor_seconds": NO_PROGRESS_FLOOR_SECONDS,
            "message": (
                "no_progress only resets on observable artifacts (HEAD/plan/quality/staging). "
                "Chapter latency grows with the book (drafts land near the end of composing, "
                "~15 min in the field); a plan worker touches HEAD only when plan extend lands. "
                f"Windows tighter than {NO_PROGRESS_FLOOR_SECONDS}s kill working workers of both "
                "kinds mid-flight — consider raising limits.no_progress_seconds"
            ),
        })
    return warnings


def executable_resolution_warnings(driver: dict[str, Any]) -> list[dict[str, Any]]:
    """接线前先看这条 argv 在本机到底起不起来——把 spawn 失败从运行期挪到校验期。

    driver argv 若用裸 `dsh`，而它在 Windows 上是 `.CMD` 垫片。
    `subprocess` 不走 shell、解析裸名只认 `.exe`，于是运行期抛 FileNotFoundError，
    被吞成 `dispatch_failed`，排查方向被带到"PATH/环境变量"上；而
    `run validate-config` 只校验 schema 与占位符，对此一无所知，接线看起来是"通过"的。

    这里只做两件确定性检查，**不阻断**（校验器不该替宿主决定装什么）：
    - 目标在本机解析不到 → `driver_executable_not_found`；
    - 目标只解析到 `.cmd`/`.bat` 垫片 → `driver_executable_is_shell_shim`
      （顺带提醒多行 prompt 不要经 cmd.exe）。
    argv 里还剩占位符时无法在校验期定论，交给运行期。
    """
    if driver.get("kind") == "command":
        argv = driver.get("argv") or []
        target = str(argv[0]) if argv else ""
    else:
        target = str(driver.get("executable") or "")
    target = target.strip()
    if not target or "{" in target:
        return []

    looks_like_path = any(sep in target for sep in ("/", "\\"))
    if looks_like_path:
        if Path(target).exists():
            return []
        return [
            {
                "code": "driver_executable_not_found",
                "target": target,
                "hint": (
                    "这个路径在本机不存在；运行期会以 dispatch_failed 结束。"
                    "Windows 上建议给出可执行文件的完整路径，或直接用 node 启动入口脚本"
                ),
            }
        ]

    resolved = shutil.which(target)
    if not resolved:
        return [
            {
                "code": "driver_executable_not_found",
                "target": target,
                "hint": (
                    "裸命令名在 PATH 上解析不到；运行期会以 dispatch_failed 结束。"
                    "先跑 `command -v <cli>` 确认，或改成完整路径"
                ),
            }
        ]
    if os.name == "nt" and Path(resolved).suffix.lower() in (".cmd", ".bat"):
        return [
            {
                "code": "driver_executable_is_shell_shim",
                "target": target,
                "resolved": resolved,
                "hint": (
                    "只解析到 .cmd/.bat 垫片：subprocess 不走 shell，无法直接启动它"
                    "（裸名只匹配 .exe）。请显式调用解释器（如 node <bin.js>）或给出 .exe 路径；"
                    "另外多行/中文 prompt 不要经 cmd.exe 传参，会被搅碎"
                ),
            }
        ]
    return []


def _spawn_failure_hint(executable: str) -> str:
    """dispatch_failed 时补一句可直接照做的排查话术。"""
    resolved = "" if any(sep in executable for sep in ("/", "\\")) else (shutil.which(executable) or "")
    if os.name == "nt" and Path(resolved).suffix.lower() in (".cmd", ".bat"):
        return (
            "argv[0] 在本机只解析到 .cmd/.bat 垫片，subprocess 无法直接启动；"
            "改用 node <bin.js> 或 .exe 完整路径"
        )
    if not resolved and executable:
        return "argv[0] 在本机解析不到；先跑 `command -v <cli>`，或改成完整路径"
    return ""
