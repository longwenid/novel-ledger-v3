from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..content.hierarchical_memory import load_hierarchical_memory
from ..content.consistency import run_consistency_audit
from ..infra.store import BookStore, _volume_label, volume_entry_for
from ..infra.util import (
    LedgerError, atomic_json, atomic_text, canonical_json,
    chinese_word_count, now_ts, ok, read_json, sha256_text,
)
from ..ledger.ledger import _event_digest, coerce_due, load_snapshot
from .pipeline import status as book_status
from ..content.reviews import review_summary


AUTOPILOT_SCHEMA = "novel-ledger.autopilot.v1"
CHECKPOINT_CHAPTERS = 10
CHECKPOINT_ASSET_IDS = 12


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


def handoff_report(store: BookStore) -> dict[str, Any]:
    """生成无人值守交接卡机器段并落盘 book/run/handoff.md。

    会话宿主（子 agent 形态）会被轮数/时长/配额中断，且中断点任意；恢复靠盘上 HEAD，
    但「下一步做什么、还有什么没结」散在多个文件里。本命令把机器可判定的事实收进
    一份 markdown（HEAD/下一动作/逾期与临期钩子/开放 findings/进度日志尾部），
    只读故事状态、不推进 HEAD；批目标与剩余轮预算等批次上下文由宿主
    追加到 book/run/unattended-log.md 的「## 交接」节。
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
    if state and state.get("status") == "paused":
        lines.append(
            f"- 运行态：paused（pause_reason={state.get('pause_reason')}）；处置后 run resume 续跑"
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
        handoff_path=str(path),
    )
    return ok(
        action="run_handoff",
        handoff_path=str(path),
        chapter=int(book.get("last_acked_ch") or 0),
        next_step=_HANDOFF_NEXT_STEP.get(phase, "unknown-phase"),
        blocked=blocked,
        due_hook_count=len(due_hooks),
        open_findings=reviews.get("pending_count"),
    )


def resume_run(store: BookStore) -> dict[str, Any]:
    """处置完暂停原因后恢复长跑：清 paused 状态并重置未兑现的扩纲提示标记。

    `plan_low_water_nudged_at` 记录上次提示时的 max_planned，只在计划增长到新值时才
    再次提示。plan worker 失败后若标记仍等于当前 max_planned（提示没被任何一次成功
    的扩纲兑现），后续 `chapter next` 会跳过提示直接开写下一章，「扩纲优先」意图
    静默丢失。恢复时在写锁内重置标记，下一次 next 重新提示扩纲。
    """
    state = _read_optional_json(store.autopilot_state_path) or {}
    previous_reason = state.get("pause_reason")
    state.update({"status": "ready", "pause_reason": None})
    _write_state(store, state)
    _reset_unhonored_nudge(store)
    _append_event(store, "resumed", chapter=int(store.read_head().get("last_acked_ch") or 0), previous_pause=previous_reason)
    return ok(action="run_resume", resumed=True, previous_pause=previous_reason)


def _reset_unhonored_nudge(store: BookStore) -> None:
    """清掉「未兑现」的低水位提示标记。

    只在「标记 == 当前 max_planned」时清——计划已增长过的旧标记保持原语义
    （表示老提示已被兑现，计划后来又落回低水位属于新提示周期）。
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


def run_checkpoint(store: BookStore) -> dict[str, Any]:
    """会话宿主形态的独立批窗口检查点：确定性检查路，零模型调用。

    宿主在章界（每 10 章/卷界）直接调用——报告落盘 checkpoints/，
    宿主只读结论（review_required / blockers / 路径）；仅 review_required 才派 triage
    子代理处置阻断项。非到期章返回 skipped 不出报告（last_checkpoint_ch 游标幂等）。
    响应里不带完整报告——报告可能带整窗摘要与证据（实测单份 110K），
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
    # 跨章事实一致性：硬事实冲突（同一键出现互斥取值）进 blockers 停线等人裁决；
    # 形态类（近重复段落、高频片段、格式体例）进 advisories——它们是判断项不是错误项，
    # 由总编辑按 references/fact-registry.md 决定删一处、改写法还是留裁决。
    # 事实登记表为空时本段恒空，不影响既有项目。
    try:
        consistency = run_consistency_audit(store)
        hard_facts = list(consistency["hits"]) + list(consistency["cross_chapter"])
        consistency_view = {
            "fact_keys": len(consistency["fact_keys"]),
            "hits": hard_facts[:CHECKPOINT_ASSET_IDS],
            "hit_count": len(hard_facts),
            "chapter_format": consistency["chapter_format"][:CHECKPOINT_ASSET_IDS],
            "chapter_format_count": len(consistency["chapter_format"]),
            "near_duplicates": consistency["near_duplicates"][:CHECKPOINT_ASSET_IDS],
            "near_duplicate_count": len(consistency["near_duplicates"]),
            "repeated_phrases": consistency["repeated_phrases"][:CHECKPOINT_ASSET_IDS],
        }
        report["continuity"] = consistency_view
        if hard_facts:
            blockers.append(
                {
                    "code": "fact_value_conflict",
                    "count": len(hard_facts),
                    "keys": sorted({str(item.get("key") or "") for item in hard_facts}),
                    "chapters": sorted(
                        {
                            int(item["chapter"])
                            for item in hard_facts
                            if isinstance(item.get("chapter"), int)
                        }
                    )[:CHECKPOINT_ASSET_IDS],
                }
            )
        if consistency["chapter_format"]:
            advisories.append(
                {"code": "chapter_format_drift", "count": len(consistency["chapter_format"])}
            )
        if consistency["near_duplicates"]:
            advisories.append(
                {"code": "near_duplicate_passages", "count": len(consistency["near_duplicates"])}
            )
        if consistency["repeated_phrases"]:
            advisories.append(
                {"code": "repeated_phrases", "count": len(consistency["repeated_phrases"])}
            )
        if not consistency["fact_keys"]:
            advisories.append({"code": "fact_declarations_missing"})
    except LedgerError as exc:
        advisories.append({"code": "consistency_scan_failed", "detail": str(exc)[:160]})
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
        "fact_issues", "chapter_format",
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
