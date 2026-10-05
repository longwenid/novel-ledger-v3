# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：usage
from __future__ import annotations

from typing import Any
from ...ledger.ledger import (read_json_line)
from ...infra.store import (PHASE_AWAIT_ACK, PHASE_IDLE, BookStore)
from ...infra.util import (LedgerError, canonical_json, now_ts, ok)

from ._common import (
    _REWORK_EVENTS,
    _append_voice_concept,
    _log_quality,
    _require_active,
    _touch,
)

def _merge_voice_memory(store: BookStore, memory: dict[str, Any]) -> list[str]:
    """写者 `memory.voice_concepts` → `voice.json` session_notes，返回被丢弃的条目。

    与 `ack --voice-note` 同一条过滤规则（否定式不入库），所以**同样必须把丢弃告诉调用方**：
    写者以为说过的"不要…"若被静默吞掉，会直接造成"说了但没生效"的返工。
    """
    extras = memory.get("voice_concepts") or []
    if not isinstance(extras, list):
        return []
    dropped: list[str] = []
    for item in extras:
        text = str(item).strip()
        if text and not _append_voice_concept(store, text):
            dropped.append(text)
    return dropped


def _normalize_usage_sample(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize one explicit per-request token sample.

    两种合法形状，不许混报：
    - 分量形状（usage.v3）：宿主能看见缓存分量时逐项报，effective=三分量和；
    - 总量形状（usage.total.v1）：stage-agent 子代理宿主只回传整单总量时如实报
      total_tokens。它不参与 effective/cache 口径，但计入章节总量并驱动
      stop_total_per_chapter 熔断——total-only 不再是"记不了账"的豁免通道。
    """
    payload = usage or {}
    forbidden = ("input_tokens", "cached_input_tokens")
    if any(key in payload and payload.get(key) is not None for key in forbidden):
        raise LedgerError(
            "invalid_usage",
            "input_tokens/cached_input_tokens are unsupported; report explicit components",
        )
    known = (
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
    )
    has_components = any(key in payload and payload.get(key) is not None for key in known)
    has_total = payload.get("total_tokens") is not None
    if has_components and has_total:
        raise LedgerError(
            "invalid_usage",
            "report either the four components or total_tokens, never both",
        )
    if not has_components and not has_total:
        return None

    def number(key: str) -> int:
        try:
            value = int(payload.get(key) or 0)
        except (TypeError, ValueError) as exc:
            raise LedgerError("invalid_usage", f"usage.{key} must be an integer") from exc
        if value < 0:
            raise LedgerError("invalid_usage", f"usage.{key} must be non-negative")
        return value

    if not has_components:
        total = number("total_tokens")
        if total <= 0:
            raise LedgerError(
                "invalid_usage",
                "total_tokens must be positive (zero/negative means no sample was taken)",
            )
        return {
            "schema": "novel-ledger.usage.total.v1",
            "total_tokens": total,
        }

    if payload.get("uncached_input_tokens") is None:
        raise LedgerError(
            "invalid_usage",
            "usage requires uncached_input_tokens (use 0 when there is none)",
        )
    uncached = number("uncached_input_tokens")
    cache_read = number("cache_read_input_tokens")
    cache_write = number("cache_write_input_tokens")
    output = number("output_tokens")
    effective = uncached + cache_read + cache_write
    return {
        "schema": "novel-ledger.usage.v3",
        "effective_input_tokens": effective,
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
    }


def _append_usage(
    store: BookStore,
    chapter: Any,
    usage: dict[str, Any] | None,
    *,
    stage: str,
    request_id: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    normalized = _normalize_usage_sample(usage)
    if normalized is None:
        # Missing telemetry is unknown, never a fake zero sample that dilutes averages.
        return None
    event = {
        **normalized,
        "chapter": chapter,
        "stage": str(stage or "unknown"),
        "request_id": request_id,
        "session_id": session_id,
        "ts": now_ts(),
    }
    store.usage_path.parent.mkdir(parents=True, exist_ok=True)
    from ...infra.util import append_bytes
    append_bytes(store.usage_path, canonical_json(event))
    return event


def _find_usage_request(store: BookStore, request_id: str) -> dict[str, Any] | None:
    import json

    if not request_id or not store.usage_path.exists():
        return None
    for line in store.usage_path.read_text(encoding="utf-8").splitlines():
        if request_id not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict) and event.get("request_id") == request_id:
            return event
    return None


def _find_usage_void(store: BookStore, request_id: str) -> dict[str, Any] | None:
    import json

    if not request_id or not store.usage_path.exists():
        return None
    for line in store.usage_path.read_text(encoding="utf-8").splitlines():
        if request_id not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            isinstance(event, dict)
            and event.get("schema") == "novel-ledger.usage.void.v1"
            and event.get("voids_request_id") == request_id
        ):
            return event
    return None


def record_usage(
    store: BookStore,
    *,
    stage: str,
    usage: dict[str, Any],
    chapter: int | None = None,
    request_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Record one incremental host sample and return an immediate budget decision."""
    request_id = str(request_id or "").strip()
    stage = str(stage or "").strip()
    session_id = str(session_id or "").strip() or None
    if not request_id:
        raise LedgerError("missing_request_id", "usage-record requires a non-empty request-id")
    if not stage:
        raise LedgerError("missing_stage", "usage-record requires a non-empty stage")
    head = store.read_head()
    if chapter is None:
        raise LedgerError(
            "missing_chapter",
            "usage-record requires --chapter copied from the action being accounted",
        )
    resolved = int(chapter)
    if resolved <= 0:
        raise LedgerError("missing_chapter", "usage-record needs an active or explicit chapter")
    existing = _find_usage_request(store, request_id)
    duplicate = existing is not None
    if existing is not None:
        normalized = _normalize_usage_sample(usage)
        # 幂等比较按「归一后样本自带的键」逐项对齐：分量形状比五键，总量形状比
        # total_tokens。不能拿固定键表硬比——total-only 事件缺分量键，缺键双零
        # 会被误判成"相同样本"，不同总量的重报就绕过了冲突检测。
        metadata_conflict = any(
            (
                ("" if existing.get(key) is None else str(existing.get(key)))
                != ("" if value is None else str(value))
            )
            for key, value in (
                ("chapter", resolved),
                ("stage", stage),
                ("session_id", session_id),
            )
        )
        counters_conflict = bool(
            normalized is None
            or str(existing.get("schema")) != str(normalized.get("schema"))
            or any(
                (existing.get(key) is None) != (normalized.get(key) is None)
                or (
                    normalized.get(key) is not None
                    and int(existing.get(key) or 0) != int(normalized.get(key))
                )
                for key in (
                    "effective_input_tokens",
                    "uncached_input_tokens",
                    "cache_read_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            )
        )
        if normalized is None or metadata_conflict or counters_conflict:
            raise LedgerError(
                "usage_request_conflict",
                "request-id was already recorded with different usage counters or metadata",
                {"request_id": request_id},
            )
        event = existing
    else:
        phase = head.get("phase") or PHASE_IDLE
        expected_chapter = (
            int(head.get("last_committed_ch") or 0) + 1
            if phase == PHASE_IDLE
            else int(head.get("chapter") or 0)
        )
        # Some hosts expose usage only after the response has fully ended.  For an ack request,
        # that means HEAD is already idle and last_acked_ch is the action just accounted.  Accept
        # that one closed boundary in addition to the candidate next chapter; request-id remains
        # idempotent, so this cannot double-count a retry.
        just_closed_chapter = int(head.get("last_acked_ch") or 0) if phase == PHASE_IDLE else 0
        if resolved not in {expected_chapter, just_closed_chapter}:
            raise LedgerError(
                "usage_chapter_mismatch",
                "usage-record chapter must match the action chapter for the current state",
                {
                    "phase": phase,
                    "expected_chapter": expected_chapter,
                    "just_closed_chapter": just_closed_chapter or None,
                    "got_chapter": resolved,
                },
            )
        event = _append_usage(
            store,
            resolved,
            usage,
            stage=stage,
            request_id=request_id,
            session_id=session_id,
        )
    if event is None:
        raise LedgerError(
            "missing_usage",
            "usage-record needs a sample in one of the two legal shapes: the four component "
            "counters (--uncached-input-tokens …) or --total-tokens when the host only sees a "
            "whole-job total",
        )
    summary = _usage_summary(store)
    chapter_usage = (summary.get("by_chapter") or {}).get(str(resolved)) or {}
    effective = int(chapter_usage.get("effective_input_tokens") or 0)
    total_reported = int(chapter_usage.get("total_tokens") or 0)
    budget = store.load_config().get("usage_budget") or {}
    warn_at = int(budget.get("warn_input_per_chapter") or 0)
    stop_at = int(budget.get("stop_input_per_chapter") or 0)
    stop_total_at = int(budget.get("stop_total_per_chapter") or 0)
    reasons = []
    if stop_at > 0 and effective >= stop_at:
        reasons.append("chapter_input_tokens")
    if stop_total_at > 0 and total_reported >= stop_total_at:
        reasons.append("chapter_total_tokens")
    stop = bool(reasons)
    warn = bool(not stop and warn_at > 0 and effective >= warn_at)
    return ok(
        action="usage_recorded",
        stop=stop,
        warning=warn,
        chapter=resolved,
        stage=stage,
        duplicate=duplicate,
        reasons=reasons,
        sample=event,
        chapter_usage=chapter_usage,
        budget={
            "warn_input_per_chapter": warn_at,
            "stop_input_per_chapter": stop_at,
            "stop_total_per_chapter": stop_total_at,
        },
        hint=(
            "token budget exceeded: stop model work and inspect session history before continuing"
            if stop
            else "token usage is near the chapter limit; finish only the current bounded step and inspect"
            if warn
            else "usage recorded"
        ),
    )


def void_usage(
    store: BookStore,
    *,
    request_id: str,
    reason: str,
) -> dict[str, Any]:
    """作废一条记错的 usage 样本（零模型更正通道；append-only，不改历史行）。

    request-id 幂等只能拦「同 ID 重放」，拦不住「换了 ID 重复记账」——宿主把同一笔
    消费用错误 request-id 再记一次时，预算被凭空多记（实测一章多记约 15.7 万 tokens
    噪音，且无更正通道只能看着）。作废是唯一合法的更正动作：按 request_id 定位原
    样本，追加一条 void 事件，聚合口径（_usage_summary）把被作废样本全部剔除。
    历史行不重写，JSONL 契约与哈希审计不变。
    """
    request_id = str(request_id or "").strip()
    reason = str(reason or "").strip()
    if not request_id:
        raise LedgerError("missing_request_id", "usage-void requires the request-id of the sample to void")
    if not reason:
        raise LedgerError("missing_reason", "usage-void requires a reason (audit trail)")
    existing = _find_usage_request(store, request_id)
    if existing is None:
        raise LedgerError(
            "usage_request_not_found",
            "no usage sample with this request-id; nothing to void",
            {"request_id": request_id},
        )
    if _find_usage_void(store, request_id) is not None:
        return ok(
            action="usage_voided",
            duplicate=True,
            request_id=request_id,
            chapter=existing.get("chapter"),
            hint="this request-id was already voided; nothing changed",
        )
    event = {
        "schema": "novel-ledger.usage.void.v1",
        "voids_request_id": request_id,
        "chapter": existing.get("chapter"),
        "stage": existing.get("stage"),
        "reason": reason,
        "ts": now_ts(),
    }
    store.usage_path.parent.mkdir(parents=True, exist_ok=True)
    from ...infra.util import append_bytes

    append_bytes(store.usage_path, canonical_json(event))
    summary = _usage_summary(store)
    chapter_usage = (summary.get("by_chapter") or {}).get(str(existing.get("chapter"))) or {}
    voided_sample = {
        key: existing.get(key)
        for key in ("schema", "chapter", "stage", "total_tokens", "effective_input_tokens", "output_tokens")
        if existing.get(key) is not None
    }
    return ok(
        action="usage_voided",
        duplicate=False,
        request_id=request_id,
        voided_sample=voided_sample,
        chapter_usage=chapter_usage,
        hint=(
            "the sample is excluded from all usage aggregates going forward; the JSONL line "
            "itself is kept (append-only audit). Do NOT re-record the same spend with a new "
            "request-id — void first, then record the correct sample if one is actually missing"
        ),
    )


def authorize_usage_resume(
    store: BookStore,
    *,
    chapter: int | None,
    action: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Authorize only the bounded ack action after a chapter token stop.

    This is deliberately narrower than changing the global threshold: it cannot reopen drafting
    or carry into the next chapter. It is consumed when one inline ack action is
    issued; a lost/retried model response requires a new explicit authorization.
    """
    head = store.read_head()
    _require_active(head)
    resolved = int(chapter or head.get("chapter") or 0)
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    action = str(action or "").strip().lower()
    if action != "ack":
        raise LedgerError("invalid_usage_authorization", "only action=ack can be authorized")
    if not actor or not reason:
        raise LedgerError("invalid_usage_authorization", "usage authorization requires actor and reason")
    if head.get("phase") != PHASE_AWAIT_ACK or resolved != int(head.get("chapter") or 0):
        raise LedgerError(
            "wrong_phase",
            "usage authorization is only valid for the current chapter in await_ack",
        )
    cfg = store.load_config()
    stop_at = int((cfg.get("usage_budget") or {}).get("stop_input_per_chapter") or 0)
    chapter_usage = (_usage_summary(store).get("by_chapter") or {}).get(str(resolved)) or {}
    effective = int(chapter_usage.get("effective_input_tokens") or 0)
    if stop_at <= 0 or effective < stop_at:
        raise LedgerError(
            "usage_not_blocked",
            "current chapter has not reached the token stop threshold",
            {"chapter": resolved, "effective_input_tokens": effective, "stop_input_per_chapter": stop_at},
        )
    authorization = {
        "chapter": resolved,
        "action": "ack",
        "max_requests": 1,
        "actor": actor,
        "reason": reason,
        "authorized_at": now_ts(),
    }
    head["usage_authorization"] = authorization
    store.write_head(_touch(head))
    _log_quality(store, resolved, "usage_authorized", action="ack", actor=actor, reason=reason)
    return ok(
        action="usage_authorized",
        stop=False,
        chapter=resolved,
        authorized_action="ack",
        authorization=authorization,
        hint=(
            "run chapter next once to issue one bounded inline final review; "
            "the authorization is consumed when that action is returned, so a retry needs a new "
            "human authorization. actor is audit metadata, not an authentication mechanism"
        ),
    )


def _quality_summary(store: BookStore) -> dict[str, Any]:
    """从 quality.jsonl 汇总每章机检/提交结果与返工率，供书级审计。"""
    if not store.quality_log_path.exists():
        return {
            "skipped": True,
            "events": 0,
        }
    stats: dict[str, Any] = {
        "begins": 0,
        "style_machine_pass": 0,
        "style_machine_fail": 0,
        "submit_accepted": 0,
        "submit_rewrite": 0,
        "ack_pass": 0,
    }
    chapters: set[int] = set()
    rework: set[int] = set()
    total = 0
    with store.quality_log_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            ev = read_json_line(line)
            total += 1
            chapter = int(ev.get("chapter") or 0)
            event = str(ev.get("event") or "")
            verdict = str(ev.get("verdict") or "")
            if event == "begin":
                stats["begins"] += 1
            elif event == "style_machine":
                key = f"style_machine_{verdict}"
                if key in stats:
                    stats[key] += 1
            elif event == "submit":
                key = f"submit_{verdict}"
                if key in stats:
                    stats[key] += 1
            elif event == "ack" and verdict == "pass":
                stats["ack_pass"] += 1
            chapters.add(chapter)
            if (event, verdict) in _REWORK_EVENTS:
                rework.add(chapter)
    sample = len(chapters)
    stats["sample_chapters"] = sample
    stats["rework_chapters"] = len(rework)
    stats["rework_rate"] = round(len(rework) / sample, 3) if sample else 0.0
    return {
        "skipped": False,
        "events": total,
        **stats,
    }


# 用全量；只有 stdout 边界截到最近 12 章，防止长跑中随章数线性膨胀的重复 token。
_USAGE_DETAIL_WINDOW = 12


def _usage_summary(store: BookStore, *, detail_window: int | None = None) -> dict[str, Any]:
    """Aggregate explicit per-request usage-v3 deltas.

    ``detail_window``：只用于 **stdout 输出边界**（status / book audit）——把
    `by_chapter` / `chapters` 截到最近 N 章，加 `chapters_total` 标记总量。全量明细
    会随章数线性膨胀（千章实测 status 223KB、book audit 237KB），而 worker 简报要求
    每章先读 status，长跑里每个 worker 会话都要吞整坨并随请求反复重发，是隐藏的
    二次方 token 浪费。进程内消费方（usage_guard / usage-record 的单章查询）必须
    带全量窗口调用——截断只发生在输出边界，真源仍是 `book/usage.jsonl`。
    """
    import json

    fields = (
        "effective_input_tokens",
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "total_only_tokens",
    )
    by_chapter: dict[int, dict[str, int]] = {}
    records = 0
    bad_lines = 0
    voided = 0
    events: list[dict[str, Any]] = []
    if store.usage_path.exists():
        for line in store.usage_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                bad_lines += 1
                continue
            if not isinstance(ev, dict):
                bad_lines += 1
                continue
            events.append(ev)
        # usage-void 作废样本：void 事件本身不是样本（不计数），但它点名的
        # request_id 从全部聚合口径中剔除——记错的账要能更正，而不是永久污染预算。
        voided_ids = {
            str(ev.get("voids_request_id") or "")
            for ev in events
            if ev.get("schema") == "novel-ledger.usage.void.v1"
        }
        voided_ids.discard("")
        for ev in events:
            if ev.get("schema") == "novel-ledger.usage.void.v1":
                continue
            if str(ev.get("request_id") or "") in voided_ids:
                voided += 1
                continue
            try:
                chapter = int(ev.get("chapter"))
            except (TypeError, ValueError):
                bad_lines += 1
                pass
                continue
            try:
                if ev.get("schema") == "novel-ledger.usage.total.v1":
                    # total-only 样本（stage-agent 宿主只见整单总量）：没有分量可对账，
                    # 只验非负非零；计入章节总量驱动 stop_total_per_chapter。
                    total_only = int(ev["total_tokens"])
                    if total_only <= 0:
                        raise ValueError("empty total-only usage")
                    sample = {"total_only_tokens": total_only}
                elif ev.get("schema") == "novel-ledger.usage.v3":
                    effective = int(ev.get("effective_input_tokens") or 0)
                    uncached = int(ev["uncached_input_tokens"])
                    cache_read = int(ev.get("cache_read_input_tokens") or 0)
                    cache_write = int(ev.get("cache_write_input_tokens") or 0)
                    output = max(int(ev.get("output_tokens") or 0), 0)
                    if min(effective, uncached, cache_read, cache_write) < 0:
                        raise ValueError("negative usage")
                    if effective != uncached + cache_read + cache_write:
                        raise ValueError("inconsistent effective input")
                    sample = {
                        "effective_input_tokens": effective,
                        "uncached_input_tokens": uncached,
                        "cache_read_input_tokens": cache_read,
                        "cache_write_input_tokens": cache_write,
                        "output_tokens": output,
                    }
                else:
                    raise ValueError("unsupported usage schema")
            except (TypeError, ValueError):
                bad_lines += 1
                continue
            records += 1
            bucket = by_chapter.setdefault(chapter, {key: 0 for key in fields})
            for key, value in sample.items():
                bucket[key] += value

    def finish(bucket: dict[str, int]) -> dict[str, Any]:
        effective = bucket["effective_input_tokens"]
        cached = bucket["cache_read_input_tokens"] + bucket["cache_write_input_tokens"]
        return {
            **bucket,
            "cache_hit_ratio": round(cached / effective, 3) if effective > 0 else 0.0,
            "total_tokens": effective + bucket["output_tokens"] + bucket["total_only_tokens"],
        }

    chapter_view = {}
    for ch, bucket in sorted(by_chapter.items()):
        chapter_view[str(ch)] = finish(bucket)
    totals = {key: sum(bucket[key] for bucket in by_chapter.values()) for key in fields}
    aggregate = finish(totals)
    chapters = sorted(by_chapter)
    inputs = [int(chapter_view[str(ch)]["effective_input_tokens"]) for ch in chapters]
    summary = {
        "telemetry": "available" if records else "missing",
        "records": records,
        "voided": voided,
        **aggregate,
        "input_per_chapter": round(aggregate["effective_input_tokens"] / len(chapters), 1) if chapters else 0,
        "max_input_per_chapter": max(inputs) if inputs else 0,
        "latest_chapter_input": inputs[-1] if inputs else 0,
        "by_chapter": chapter_view,
        "chapters": chapters,
        "bad_lines": bad_lines,
    }
    if detail_window is not None and len(chapters) > detail_window:
        # 汇总量（totals / input_per_chapter / max）保持全书口径；只截明细窗口
        summary["chapters_total"] = len(chapters)
        summary["detail_window"] = detail_window
        summary["by_chapter"] = {str(c): chapter_view[str(c)] for c in chapters[-detail_window:]}
        summary["chapters"] = chapters[-detail_window:]
    return summary


__all__ = [
    '_merge_voice_memory',
    '_normalize_usage_sample',
    '_append_usage',
    '_find_usage_request',
    '_find_usage_void',
    'record_usage',
    'void_usage',
    'authorize_usage_resume',
    '_quality_summary',
    '_USAGE_DETAIL_WINDOW',
    '_usage_summary',
]
