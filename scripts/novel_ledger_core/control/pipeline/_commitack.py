# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：commitack
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from ...content.gates import (blocking_plot_findings, collect_plot_findings, plot_findings_issues, validate_prose_anchors)
from ...content.hierarchical_memory import (rebuild_hierarchical_memory, update_hierarchical_memory)
from ...content.reviews import (review_summary)
from ...ledger.ledger import (commit_event, load_snapshot, max_event_chapter, read_events, rollback_ledger_to)
from ...content.pack import (assemble_pack, inputs_fingerprint)
from ...content.style_check import (check_style_hard)
from ...infra.store import (PHASE_AWAIT_ACK, PHASE_AWAIT_DRAFT, PHASE_AWAIT_POLISH, PHASE_BLOCKED, PHASE_COMPLETE, PHASE_IDLE, BookStore)
from ...infra.scale import (COMPLETION_MIN_RATIO)
from ...infra.util import (LedgerError, atomic_json, atomic_text, canonical_json, chinese_word_count, now_ts, ok, read_json, sha256_text, stable_read_text)

from ._common import (
    _ack_hint,
    _append_voice_concept,
    _book_words_written,
    _corpus_style_fingerprint,
    _log_quality,
    _refresh_stage_views,
    _require_active,
    _style_gate_enabled,
    _style_structure_args,
    _touch,
    _unresolved_long_term_commitments,
)
from ._audit import (
    audit_book,
    resync_baseline,
)
from ._submit import (
    ACK_QUOTE_MIN,
    _quote_requirements,
    _quotes_all_in_first_half,
)
from ._usage import (
    _merge_voice_memory,
)

def _commit(store: BookStore, head: dict[str, Any]) -> dict[str, Any]:
    if not store.output_path.exists():
        raise LedgerError("missing_output", "no accepted output to commit")
    output = read_json(store.output_path)
    pack = read_json(store.current_pack_path)
    chapter = int(head["chapter"])
    if pack.get("inputs_fingerprint") != inputs_fingerprint(store, chapter):
        # 防御：submit 通过后、commit 前上游又被改。旧 pack 入账会污染账本 → 停线等人。
        blocked = {
            "chapter": chapter,
            "reason": "stale_inputs",
            "violations": [{"code": "inputs_fingerprint_mismatch"}],
        }
        head["phase"] = PHASE_BLOCKED
        head["blocked"] = blocked
        store.write_head(_touch(head))
        return ok(
            action="blocked",
            stop=True,
            phase=PHASE_BLOCKED,
            chapter=chapter,
            blocked=blocked,
            hint="upstream inputs changed after submit; retry-authorize after reconciling plan/kb/voice",
        )
    # submit 已把终稿正文注入 output.json；这里再兜一层：万一读到的是旧版/手改过的
    # output.json（无 prose），回落到阶段二终稿，避免 commit 抛 KeyError 留下半个章节。
    prose = str(output.get("prose") or "")
    if not prose:
        polished_path = store.polished_text_path(chapter)
        if polished_path.exists():
            prose = polished_path.read_text(encoding="utf-8").rstrip("\n")
    if not prose:
        raise LedgerError("empty_prose", "commit has no prose (output.json missing prose and polished text)")
    prose_hash = "sha256:" + sha256_text(prose)
    delta = dict(output.get("state_delta") or {})
    try:
        snap = commit_event(
            store,
            chapter,
            delta,
            {"canon_sha": store.canon_fingerprint()},
        )
    except LedgerError as exc:
        if exc.code == "ledger_replay_conflict" and _resumable_commit(store, chapter, delta, prose_hash):
            # 崩溃前滚：事件已入账且
            # 与本次已验收的提交完全一致 → 幂等尾部（md/meta/summary/滚层/文风/HEAD 全是
            # 原子覆盖）直接续跑并进入 ack。旧语义会把已写完的章整章回滚重写，成本极高。
            snap = load_snapshot(store)
        elif exc.code not in ("ledger_conflict", "ledger_replay_conflict"):
            raise
        else:
            blocked = {
                "chapter": chapter,
                "reason": exc.code,
                "violations": exc.details if isinstance(exc.details, list) else [exc.details],
            }
            head["phase"] = PHASE_BLOCKED
            head["blocked"] = blocked
            store.write_head(_touch(head))
            hint = (
                "ledger conflict; human must retry-authorize, change direction, or complete"
                if exc.code == "ledger_conflict"
                # 本章已在账本里（commit 中途崩溃后重跑）：不要盲目重跑，先 ledger verify 看清，
                # 再 retry-authorize（会撤销本章事件后重写）或 ledger repair --from-events
                else "chapter is already in the ledger (crashed mid-commit?); run ledger verify, "
                "then retry-authorize to roll the chapter back and rewrite it"
            )
            return ok(
                action="blocked",
                stop=True,
                phase=PHASE_BLOCKED,
                chapter=chapter,
                blocked=blocked,
                hint=hint,
            )
    return _commit_tail(
        store,
        head,
        chapter=chapter,
        prose=prose,
        prose_hash=prose_hash,
        pack=pack,
        output=output,
        snap=snap,
    )


def _resumable_commit(store: BookStore, chapter: int, delta: dict[str, Any], prose_hash: str) -> bool:
    """commit 中途崩溃后的前滚条件（保守判定）。

    本章恰好一条事件、其 state_delta 与本次已验收提交逐字节一致、已落盘的 meta
    （若有）与本次正文哈希一致。任一不满足都回退到 blocked 保守路径——前滚的
    安全边界是"同一份提交的续跑"，不是"同章号就续跑"。
    """
    matches = [e for e in read_events(store) if int(e.get("chapter") or 0) == int(chapter)]
    if len(matches) != 1:
        return False
    if canonical_json(matches[0].get("state_delta") or {}) != canonical_json(delta):
        return False
    meta_path = store.chapter_meta_path(chapter)
    if meta_path.exists():
        try:
            existing = read_json(meta_path)
        except Exception:
            return False
        if existing.get("prose_hash") != prose_hash:
            return False
    return True


def _commit_tail(
    store: BookStore,
    head: dict[str, Any],
    *,
    chapter: int,
    prose: str,
    prose_hash: str,
    pack: dict[str, Any],
    output: dict[str, Any],
    snap: dict[str, Any],
) -> dict[str, Any]:
    """commit 的幂等尾部：从章 md 落盘到 HEAD 推进。

    每一步都是原子覆盖且不依赖"未写过"的前提，因此正常路径与崩溃前滚路径
    共用同一份尾部——这也是 failpoint 收敛测试（test_commit_failpoints.py）
    逐点证明的对象。
    """
    atomic_text(store.chapter_md_path(chapter), prose if prose.endswith("\n") else prose + "\n")
    atomic_json(
        store.chapter_meta_path(chapter),
        {
            "chapter": chapter,
            "pack_hash": pack["pack_hash"],
            "prose_hash": prose_hash,
            "word_count": chinese_word_count(prose),
            "l1_summary": output["l1_summary"],
            "committed_at": now_ts(),
        },
    )
    atomic_json(
        store.summary_path(chapter),
        {"chapter": chapter, "l1_summary": output["l1_summary"], "prose_hash": prose_hash},
    )
    update_hierarchical_memory(store, chapter, str(output["l1_summary"]))
    dropped_notes = _merge_voice_memory(store, output.get("memory") or {})
    head.update(
        {
            "phase": PHASE_AWAIT_ACK,
            "last_committed_ch": chapter,
            "prose_hash": prose_hash,
            "blocked": None,
        }
    )
    store.write_head(_touch(head))
    quote_requirements = _quote_requirements(store)
    payload: dict[str, Any] = {
        "action": "ack",
        "stop": False,
        "phase": PHASE_AWAIT_ACK,
        "chapter": chapter,
        "pack_hash": pack["pack_hash"],
        "prose_hash": prose_hash,
        "chapter_path": str(store.chapter_md_path(chapter)),
        "ledger_chapter": snap.get("chapter"),
        "quote_requirements": quote_requirements,
        "hint": _ack_hint(store, head, quote_requirements),
    }
    if dropped_notes:
        payload["voice_note_dropped"] = {
            "notes": dropped_notes,
            "reason": "note starts with 不要/禁止",
            "hint": _VOICE_NOTE_DROP_HINT,
        }
    return ok(**payload)


def _validated_read_quotes(store: BookStore, prose: str, quotes: list[str]) -> list[str]:
    min_quotes = _quote_requirements(store)["minimum"]
    cleaned = [q.strip() for q in quotes if str(q).strip()]
    if len(cleaned) < min_quotes:
        raise LedgerError(
            "ack_insufficient_quotes",
            f"ack-read requires at least {min_quotes} quotes from the prose (substring, ≥{ACK_QUOTE_MIN} chars each)",
            {"required": min_quotes, "got": len(cleaned)},
        )
    for quote in cleaned:
        if len(quote) < ACK_QUOTE_MIN:
            raise LedgerError(
                "ack_quote_too_short",
                f"ack quote must be at least {ACK_QUOTE_MIN} characters (a sentence, not a token)",
                quote,
            )
        if quote not in prose:
            raise LedgerError("ack_quote_not_in_prose", "quote is not in the committed chapter", quote)
    if _quotes_all_in_first_half(cleaned, prose):
        raise LedgerError(
            "ack_quotes_not_distributed",
            "ack quotes all come from the first half of the chapter; "
            "at least one quote must cover the latter half; quotes verify coverage, not full-chapter reading",
            {"quote_count": len(cleaned)},
        )
    return cleaned


def ack_patched_chapter(store: BookStore, *, chapter: int, quotes: list[str]) -> dict[str, Any]:
    path = store.chapter_md_path(chapter)
    if not path.exists() or not store.ack_path(chapter).exists():
        raise LedgerError("missing_chapter", "patch review requires a committed, previously reviewed chapter")
    prose = path.read_text(encoding="utf-8")
    receipt = read_json(store.ack_path(chapter))
    actual = "sha256:" + sha256_text(prose)
    if receipt.get("prose_hash") not in {actual, "sha256:" + sha256_text(prose.rstrip("\n"))}:
        raise LedgerError("prose_hash_mismatch", "patch review cannot restamp untracked edits")
    receipt.update({"quotes": _validated_read_quotes(store, prose, quotes), "review_status": "reviewed", "reviewed_prose_hash": actual, "reviewed_at": now_ts()})
    atomic_json(store.ack_path(chapter), receipt)
    _log_quality(store, chapter, "patch_review", verdict="pass", prose_hash=actual)
    return ok(action="patch_reviewed", chapter=chapter, prose_hash=actual)


def ack_read(
    store: BookStore,
    *,
    quotes: list[str],
    verdict: str = "pass",
    voice_note: str | None = None,
    prose_hash: str | None = None,
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    head = store.read_head()
    _require_active(head)
    if head.get("phase") != PHASE_AWAIT_ACK:
        raise LedgerError("wrong_phase", f"ack-read not allowed in phase {head.get('phase')}")
    chapter = int(head["chapter"])
    path = store.chapter_md_path(chapter)
    if not path.exists():
        raise LedgerError("missing_chapter", f"committed chapter file missing: {path}")
    prose = path.read_text(encoding="utf-8")
    actual = "sha256:" + sha256_text(prose[:-1] if prose.endswith("\n") else prose)
    # file may have trailing newline added at commit
    alt = "sha256:" + sha256_text(prose)
    expected = head.get("prose_hash")
    if actual != expected and alt != expected:
        raise LedgerError("prose_hash_mismatch", "committed prose hash does not match HEAD")
    if prose_hash and prose_hash not in (expected, actual, alt):
        raise LedgerError("prose_hash_mismatch", "ack prose_hash does not match committed chapter")
    cfg = store.load_config()
    cleaned = _validated_read_quotes(store, prose, quotes)
    reported = {"plot_findings": findings if findings is not None else []}
    finding_issues = plot_findings_issues(reported, prose)
    if finding_issues:
        raise LedgerError("ack_findings_invalid", "final reviewer findings require valid severity and evidence", finding_issues)
    final_findings = collect_plot_findings(reported)
    if blocking_plot_findings(final_findings) and verdict != "p0":
        raise LedgerError("ack_verdict_conflict", "objective BLOCKER must use p0, never pass")
    if final_findings:
        from ...content.reviews import record_findings
        record_findings(store, chapter, prose.rstrip("\n"), final_findings)
    record = {
        "chapter": chapter,
        "prose_hash": expected,
        "quotes": cleaned,
        "verdict": verdict,
        "voice_note": (voice_note or "").strip() or None,
        "findings": final_findings,
        "acked_at": now_ts(),
    }
    atomic_json(store.ack_path(chapter), record)
    if verdict == "p0":
        head.pop("usage_authorization", None)
        return _reopen_p0(store, head, voice_note)
    voice_note_stored = True
    if voice_note and voice_note.strip():
        voice_note_stored = _append_voice_concept(store, voice_note.strip())
    head.update(
        {
            "phase": PHASE_IDLE,
            "last_acked_ch": chapter,
            "rewrite_count": 0,
            "reopen_count": 0,
            "style_metrics_retry": 0,
            "style_metrics_pending": False,
            "polish_anchor_retry": 0,
            "polish_anchor_pending": False,
            "usage_authorization": None,
        }
    )
    store.write_head(_touch(head))
    # 成功确认入账后清理暂存区中间产物，保证 staging 状态卫生
    for stale in (
        store.draft_text_path(chapter),
        store.polished_text_path(chapter),
        store.submit_check_receipt_path(chapter),
        store.output_path,
    ):
        if stale.exists():
            with contextlib.suppress(OSError):
                stale.unlink()
    _log_quality(store, chapter, "ack", verdict="pass", quotes=len(cleaned))
    inline_mode = str(cfg.get("session_mode") or "strict") == "inline-compact"
    payload = ok(
        verdict="acked",
        stop=True,
        session_boundary="advisory" if inline_mode else "required",
        requires_new_session=not inline_mode,
        session_mode="inline-compact" if inline_mode else "strict",
        phase=PHASE_IDLE,
        chapter=chapter,
        prose_hash=expected,
        last_acked_ch=chapter,
        resume_command="chapter next",
        hint=(
            "chapter is closed; inline-compact mode allows continuing the NEXT chapter in THIS session, "
            "but only after compacting context: drop the finished chapter's prose from the conversation, "
            "keep only HEAD/plan/ledger pointers, never re-echo old prose into the new task; "
            "then run `chapter next`"
            if inline_mode
            else "chapter is closed; end the current model session now. Begin the next chapter by running "
            "`chapter next` from a new session so prior chapter context is not retransmitted"
        ),
    )
    if voice_note and voice_note.strip() and not voice_note_stored:
        payload["voice_note_dropped"] = {
            "reason": "note starts with 不要/禁止",
            "hint": _VOICE_NOTE_DROP_HINT,
        }
    every = int(cfg.get("style_fingerprint_every") or 0)
    if every > 0 and chapter % every == 0:
        # 每 N 章自动跑一次累计节奏指纹；只报方向，不挡下一章。
        payload["style_fingerprint"] = _corpus_style_fingerprint(store, min_chapters=every)
    return payload


def _reopen_p0(store: BookStore, head: dict[str, Any], voice_note: str | None) -> dict[str, Any]:
    dropped = False
    if voice_note and voice_note.strip():
        dropped = not _append_voice_concept(store, voice_note.strip())
    payload = _reopen_chapter(
        store,
        head,
        reason="voice_p0_exhausted",
        verdict_hint="P0 voice: rewrite the same chapter; still automatic",
    )
    if dropped:
        payload["voice_note_dropped"] = {
            "reason": "note starts with 不要/禁止",
            "hint": _VOICE_NOTE_DROP_HINT,
        }
    return payload


def _reopen_chapter(
    store: BookStore,
    head: dict[str, Any],
    *,
    reason: str,
    verdict_hint: str,
) -> dict[str, Any]:
    """同章重写（新 pack、删旧稿、回退 last_committed_ch）。共用 reopen_limit 配额。"""
    cfg = store.load_config()
    limit = int(cfg.get("reopen_limit") or 1)
    count = int(head.get("reopen_count") or 0)
    if count >= limit:
        blocked = {
            "chapter": head.get("chapter"),
            "reason": reason,
            "reopen_count": count,
        }
        head["phase"] = PHASE_BLOCKED
        head["blocked"] = blocked
        store.write_head(_touch(head))
        return ok(verdict="blocked", stop=True, phase=PHASE_BLOCKED, blocked=blocked)
    chapter = int(head["chapter"])
    if store.output_path.exists():
        store.output_path.unlink()
    rollback = _discard_committed_chapter(store, chapter)
    pack = assemble_pack(store, chapter)
    atomic_json(store.current_pack_path, pack)
    atomic_json(store.pack_archive_path(chapter), pack)
    _refresh_stage_views(store, pack, chapter)
    head.update(
        {
            "phase": PHASE_AWAIT_DRAFT,
            "pack_hash": pack["pack_hash"],
            "prose_hash": None,
            "last_committed_ch": int(head.get("last_committed_ch") or chapter) - 1
            if int(head.get("last_committed_ch") or 0) == chapter
            else head.get("last_committed_ch"),
            "rewrite_count": 0,
            "reopen_count": count + 1,
            "style_metrics_retry": 0,
            "style_metrics_pending": False,
            "polish_anchor_retry": 0,
            "polish_anchor_pending": False,
            "blocked": None,
        }
    )
    store.write_head(_touch(head))
    _log_quality(store, chapter, "begin", reason="reopen")
    return ok(
        verdict="reopen",
        phase=head["phase"],
        chapter=chapter,
        pack_hash=pack["pack_hash"],
        pack_path=str(store.current_pack_path),
        ledger_rollback=rollback,
        hint=verdict_hint + " — run chapter next to restart the chapter",
    )


def _discard_committed_chapter(store: BookStore, chapter: int) -> dict[str, Any] | None:
    """废掉第 chapter 章的已入账内容：先撤账本事件，再删章文件与派生记录。

    账本必须先回滚：只删章文件会让废稿申报的 facts/debts/hooks/relations（以及不可逆的
    deaths）永久留在 events/snapshot 里，重写稿提交后同一实体下出现两稿并存的事实，
    而废稿正文已删、人无从排查。返回 None 表示账本里本来就没有这一章。
    """
    rollback: dict[str, Any] | None = None
    if max_event_chapter(store) >= int(chapter):
        rollback = rollback_ledger_to(store, chapter)
    for stale_path in (
        store.chapter_md_path(chapter),
        store.chapter_meta_path(chapter),
        store.summary_path(chapter),
        store.ack_path(chapter),
    ):
        if stale_path.exists():
            stale_path.unlink()
    rebuild_hierarchical_memory(store, chapter - 1)
    # 账本空了＝废稿的 ack/写者笔记没有更早章可挂靠。不清就会把「用鼻血写靠近高境」
    # 这类旧稿 session_notes 再灌进重写 pack 的 voice_concepts 剩余槽。
    if rollback and int(rollback.get("kept_events") or 0) == 0:
        store.save_voice_session_notes([])
    return rollback


def retry_authorize(
    store: BookStore, *, actor: str, reason: str, action: str = "rewrite"
) -> dict[str, Any]:
    head = store.read_head()
    _require_active(head)
    if not actor.strip() or not reason.strip():
        raise LedgerError("invalid_actor", "actor and reason are required")
    phase = head.get("phase")
    last_c = int(head.get("last_committed_ch") or 0)
    last_a = int(head.get("last_acked_ch") or 0)
    chapter = int(head.get("chapter") or 0)
    if action == "style":
        # 轻解锁。
        # 只适用于 polish 段 style 耗尽 blocked：正文未入账、无需回滚——重置重试计数、
        # 带着失败清单回 await_polish，写者按 metrics_path 定点修润色稿后重交，
        # polish-submit 每次都重跑逐字机检，不存在绕过。人因留痕进 quality 日志。
        blocked = head.get("blocked") or {}
        if phase != PHASE_BLOCKED or blocked.get("reason") != "style_metrics_failed":
            raise LedgerError(
                "wrong_phase",
                "retry-authorize --action style only from blocked(reason=style_metrics_failed)",
            )
        if last_c >= chapter:
            raise LedgerError(
                "chapter_committed",
                "chapter already committed; use the default rewrite path (full rollback)",
            )
        if store.output_path.exists():
            store.output_path.unlink()
        head.update(
            {
                "phase": PHASE_AWAIT_POLISH,
                "style_metrics_retry": 0,
                "style_metrics_pending": True,
                "blocked": None,
            }
        )
        store.write_head(_touch(head))
        _log_quality(
            store,
            chapter,
            "style_retry_authorized",
            action="style",
            actor=actor,
            reason=reason,
        )
        return ok(
            verdict="authorized",
            phase=head["phase"],
            chapter=chapter,
            action="style",
            actor=actor,
            polished_output_path=str(store.polished_text_path(chapter)),
            style_metrics_path=blocked.get("metrics_path"),
            hint=(
                "style retry budget restored IN PLACE (no rollback): patch the polished file "
                "per the failed-metrics list (or chapter patch), then re-run polish-submit — "
                "every machine gate reruns on submit"
            ),
        )
    # 已 ack 的最后一章整章重写：recovery「本章已入账但要重写 → retry-authorize」。
    # 只认 idle 且 last_committed == last_acked == chapter，避免跳过未 ack 章或开下一章。
    idle_rewrite_last = (
        phase == PHASE_IDLE
        and last_c > 0
        and last_c == last_a
        and chapter == last_c
    )
    if phase != PHASE_BLOCKED and not idle_rewrite_last:
        raise LedgerError(
            "wrong_phase",
            "retry-authorize only from blocked, or idle after the last committed chapter is acked",
        )
    if store.output_path.exists():
        store.output_path.unlink()
    # 先回滚再装配：否则 pack 会带上废稿 facts / session_notes，submit 还会 stale_pack。
    rollback = _discard_committed_chapter(store, chapter)
    pack = assemble_pack(store, chapter)
    atomic_json(store.current_pack_path, pack)
    atomic_json(store.pack_archive_path(chapter), pack)
    _refresh_stage_views(store, pack, chapter)
    last_c = int(head.get("last_committed_ch") or 0)
    last_a = int(head.get("last_acked_ch") or 0)
    head.update(
        {
            "phase": PHASE_AWAIT_DRAFT,
            "pack_hash": pack["pack_hash"],
            "prose_hash": None,
            "last_committed_ch": min(last_c, chapter - 1) if rollback else last_c,
            "last_acked_ch": min(last_a, chapter - 1) if rollback else last_a,
            "rewrite_count": 0,
            "reopen_count": 0,
            "style_metrics_retry": 0,
            "style_metrics_pending": False,
            "polish_anchor_retry": 0,
            "polish_anchor_pending": False,
            "blocked": None,
        }
    )
    store.write_head(_touch(head))
    _log_quality(store, chapter, "begin", reason="retry_authorize")
    return ok(
        verdict="authorized",
        phase=head["phase"],
        chapter=chapter,
        pack_hash=pack["pack_hash"],
        actor=actor,
        reason=reason,
        ledger_rollback=rollback,
        hint="human unlocked; run chapter next to restart the chapter",
    )


def book_complete(
    store: BookStore,
    *,
    actor: str,
    reason: str,
    override_target: bool = False,
) -> dict[str, Any]:
    head = store.read_head()
    if not actor.strip() or not reason.strip():
        raise LedgerError("invalid_actor", "actor and reason are required")
    pending_review = review_summary(store)
    pending_patches = [str(p) for p in store.acks_dir.glob("**/ch-*.json") if read_json(p).get("review_status") == "needs_review"]
    if pending_review["pending_count"] or pending_patches:
        raise LedgerError("editorial_review_pending", "resolve findings and review patched revisions before completing the book", {"findings": pending_review, "patches": pending_patches})
    cfg = store.load_config()
    target_words = int(cfg.get("book_words") or 0)
    written_words = _book_words_written(store)
    progress = written_words / target_words if target_words > 0 else 0.0
    if target_words > 0 and progress < COMPLETION_MIN_RATIO and not override_target:
        raise LedgerError(
            "book_target_not_reached",
            "book completion is blocked because the signed word target is far from complete",
            {
                "book_words": target_words,
                "book_words_written": written_words,
                "book_words_progress": round(progress, 4),
                "completion_min_ratio": COMPLETION_MIN_RATIO,
                "hint": "continue with plan extend; an author may explicitly use --override-target to shorten the book",
            },
        )
    planned = sorted(
        int(c.get("chapter") or 0)
        for c in (store.load_plan().get("chapters") or [])
        if int(c.get("chapter") or 0) > 0
    )
    last_acked = int(head.get("last_acked_ch") or 0)
    if not planned or last_acked <= 0 or last_acked < planned[-1] or last_acked != int(head.get("last_committed_ch") or 0) or head.get("phase") not in (PHASE_IDLE, PHASE_COMPLETE):
        raise LedgerError("book_not_finished", "normal completion requires all planned chapters committed, acknowledged and no unfinished stage", {"phase": head.get("phase"), "last_acked_ch": last_acked, "last_planned_ch": planned[-1] if planned else None, "hint": "continue writing/reviewing; author-approved early closure uses book close-early"})
    unresolved_commitments = _unresolved_long_term_commitments(
        store.load_plan(), load_snapshot(store)
    )
    if unresolved_commitments:
        raise LedgerError("book_promises_unresolved", "resolve required book promises or explicitly amend the signed outline before completing", {"commitments": unresolved_commitments})
    from ...content.story_review import pending_review
    pending = pending_review(store, completing=True)
    if pending is not None:
        raise LedgerError("story_review_required", "normal completion requires current passing volume and endgame review receipts", {"review_id": pending["review_id"], "hint": "run chapter next, or review story-next --complete for an explicitly shortened word target"})
    from ..autopilot import _completion_audit_blockers
    audit = audit_book(store)
    blockers = _completion_audit_blockers(audit)
    if blockers:
        raise LedgerError("completion_audit_failed", "normal completion requires a clean book audit", {"blockers": blockers})
    head["status"] = "completed"
    head["phase"] = PHASE_COMPLETE
    head["blocked"] = None
    head["completion_kind"] = "normal"
    head["completion_target_override"] = target_words if override_target and target_words > 0 and progress < COMPLETION_MIN_RATIO else None
    store.write_head(_touch(head))
    payload: dict[str, Any] = {
        "action": "complete",
        "stop": True,
        "phase": PHASE_COMPLETE,
        "actor": actor,
        "reason": reason,
        "chapters_acked": last_acked,
        "chapters_planned": len(planned),
        "book_words": target_words,
        "book_words_written": written_words,
        "book_words_progress": round(progress, 4),
        "target_overridden": bool(override_target and target_words > 0 and progress < COMPLETION_MIN_RATIO),
        "unresolved_long_term_commitments": unresolved_commitments,
    }
    return ok(**payload)


def book_close_early(store: BookStore, *, actor: str, reason: str, author_confirmed: bool) -> dict[str, Any]:
    """Explicit author closure is recorded separately from successful completion."""
    if not author_confirmed or not actor.strip() or not reason.strip():
        raise LedgerError("author_confirmation_required", "early closure requires --author-confirmed, actor and reason")
    head = store.read_head()
    if head.get("status") == "completed":
        raise LedgerError("book_completed", "reopen the closed book before changing its closure")
    head["early_close_resume"] = {"phase": head.get("phase") or PHASE_IDLE, "blocked": head.get("blocked")}
    head.update(status="completed", phase=PHASE_COMPLETE, blocked=None, completion_kind="early_close")
    store.write_head(_touch(head))
    from ...infra.util import append_bytes
    append_bytes(store.editorial_dir / "decisions.jsonl", canonical_json({"ts": now_ts(), "action": "book.close_early", "actor": actor, "reason": reason, "author_confirmed": True, "normal_completion": False, "last_acked_ch": head.get("last_acked_ch"), "resume": head["early_close_resume"]}))
    return ok(action="book_closed_early", stop=True, phase=PHASE_COMPLETE, completion_kind="early_close", normal_completion=False, actor=actor, reason=reason)


def book_reopen(store: BookStore, *, actor: str, reason: str) -> dict[str, Any]:
    """撤销 `book complete`：重新打开写路（完结的逃生口）。

    没有这条命令时，误封笔只能手改 `book/run/HEAD.json`——而那正是文档反复禁止的动作。
    这里只复位 status/phase，不碰已入账章节与账本一行。
    """
    head = store.read_head()
    if not actor.strip() or not reason.strip():
        raise LedgerError("invalid_actor", "actor and reason are required")
    if (head.get("status") or "active") != "completed":
        raise LedgerError("not_completed", "book is not completed; nothing to reopen")
    head["status"] = "active"
    resume = head.pop("early_close_resume", {})
    head["phase"] = resume.get("phase") or PHASE_IDLE
    head["blocked"] = resume.get("blocked")
    head.pop("completion_kind", None)
    head.pop("completion_target_override", None)
    store.write_head(_touch(head))
    return ok(
        action="book_reopen",
        status="active",
        phase=head["phase"],
        chapter=int(head.get("chapter") or 0),
        last_committed_ch=int(head.get("last_committed_ch") or 0),
        last_acked_ch=int(head.get("last_acked_ch") or 0),
        actor=actor,
        reason=reason,
        hint="write path reopened; run chapter next to continue",
    )


_VOICE_NOTE_DROP_HINT = (
    "voice 笔记只收正向写法（如「对白再磕一点」）；以「不要/禁止」开头的否定式一律不入库"
    "（防止长跑里长出一张会漂移的禁词表）。禁写项请写进 book/editorial/intent.md 的红线，"
    "由总编辑在立项与派发时约束。"
)


def patch_prose(
    store: BookStore,
    *,
    patches: list[tuple[str, str]],
    chapter: int | None = None,
    bypass_style_check: bool = False,
) -> dict[str, Any]:
    """段落级精准修润与自愈沉淀：支持暂存区（staging）与已入账正文（committed）就地热修补。"""
    if not patches:
        raise LedgerError("empty_patches", "at least one (target, replacement) patch pair is required")

    head = store.read_head()
    # 封笔后不得再改正文：`patch` 会重写已入卷的章文件并顺手 resync 哈希基线，
    # 一次越权修润就能把"完结"这件事变成假的，且不留任何可审的痕迹。
    _require_active(head)
    target_ch = int(chapter if chapter is not None else (head.get("chapter") or 1))

    # 1. 确定目标文件与模式
    committed_path = store.chapter_md_path(target_ch)
    polished_path = store.polished_text_path(target_ch)
    draft_path = store.draft_text_path(target_ch)

    mode = "committed"
    target_file = committed_path

    # 判断模式优先顺位：
    # 1. 显式指定 chapter 时：若 committed 存在优先走 committed；否则才看 staging；
    # 2. 未指定 chapter 时：若该章已入账（last_committed_ch >= target_ch 且 committed 存在），
    #    绝不命中 staging 残渣，必须走 committed；
    # 3. 只有尚未入账（创作中）的章节才优先命中 staging 暂存区。
    is_committed = (int(head.get("last_committed_ch") or 0) >= target_ch) and committed_path.exists()
    if chapter is not None and committed_path.exists():
        mode = "committed"
        target_file = committed_path
    elif is_committed:
        mode = "committed"
        target_file = committed_path
    elif polished_path.exists():
        mode = "staging"
        target_file = polished_path
    elif committed_path.exists():
        mode = "committed"
        target_file = committed_path
    elif draft_path.exists():
        mode = "staging"
        target_file = draft_path
    else:
        raise LedgerError(
            "missing_prose_for_patch",
            f"no editable prose found for chapter {target_ch}",
            {"chapter": target_ch},
        )

    text = target_file.read_text(encoding="utf-8")
    original_text = text  # 备份原文，用于回滚和字数变化检测
    original_word_count = chinese_word_count(original_text)

    # 2. 校验并执行精确替换
    for idx, (tgt, rep) in enumerate(patches):
        if not tgt:
            raise LedgerError("empty_patch_target", f"patch[{idx}] target is empty")
        count = text.count(tgt)
        if count == 0:
            raise LedgerError(
                "patch_target_not_found",
                f"patch[{idx}] target substring not found in {target_file.name}"
                + (
                    "（修历史章须显式 --chapter N：patch 默认作用于当前活动章节）"
                    if mode == "committed" and int(head.get("chapter") or 0) != target_ch
                    else ""
                ),
                {"target_preview": tgt[:60] if len(tgt) > 60 else tgt, "chapter": target_ch},
            )
        if count > 1:
            raise LedgerError(
                "patch_target_ambiguous",
                f"patch[{idx}] target matched {count} times in {target_file.name}; must be unique",
                {"target_preview": tgt[:60] if len(tgt) > 60 else tgt, "count": count},
            )
        text = text.replace(tgt, rep, 1)

    if mode == "committed":
        pack_path = store.pack_archive_path(target_ch)
        if pack_path.exists():
            pack = read_json(pack_path)
            # 归档 pack 里的 glossary 是**该章写作时**的快照，项目后续新增的禁用写法不在其中。
            # 就地修润老章节时必须以**当前 config.glossary** 为准，否则新守卫护不住老章节
            # （提交与 patch 都会漏，只剩 book audit 事后能抓）。
            current_glossary = store.load_config().get("glossary") or {}
            if isinstance(current_glossary, dict) and current_glossary:
                merged = dict(pack.get("glossary") or {})
                merged.update(current_glossary)
                pack = {**pack, "glossary": merged}
            anchor_issues = validate_prose_anchors(text, pack)
            # 只拦**本次 patch 新引入**的术语违规：老章节在上闸前就含的禁用词属存量债，
            # 由 book audit / book reconcile 记 advisory，不该让任何无关修补都被它卡死
            # （与润色收口的"按来源分流"同一原则）。
            anchor_issues = [
                issue
                for issue in anchor_issues
                if issue.get("code") != "glossary_term_banned"
                or str(issue.get("wrong_term") or "") not in original_text
            ]
            if anchor_issues:
                raise LedgerError(
                    "patch_anchor_failed",
                    "patch broke a prose anchor (beats/must, continuity or glossary); "
                    "the replacement must not erase the chapter's required events or rewrite known characters",
                    {"issues": anchor_issues},
                )
        # 账本引文闸：就地修润若改断了账本 quote 的正文支撑（且别处补不回来），
        # fail-closed。否则改了数字/措辞、账本却停在旧值，值会顺着 NOW 卡灌进后续章。
        from ...ledger.ledger import ledger_quote_orphans

        orphans = ledger_quote_orphans(store, original_text, text, chapter=target_ch)
        if orphans:
            raise LedgerError(
                "patch_orphaned_ledger_quote",
                "patch removed prose that a ledger quote depends on; reconcile the ledger first, then re-patch",
                {
                    "orphans": orphans,
                    "hint": "该段正文是账本某条 debts/hooks/relations 的唯一引文来源。"
                    "若改的是数字或事实，请先按新正文修正账本对应条目，再重跑 patch；"
                    "若只是改错字、引文仍应保留，请把引文原句一并保留在替换文本里。",
                },
            )

    # 3. 质量防护快检
    warnings: list[dict[str, Any]] = []

    # 3.1 字数变化幅度检测（±20%）
    new_word_count = chinese_word_count(text)
    if original_word_count > 0:
        ratio = abs(new_word_count - original_word_count) / original_word_count
        if ratio > 0.20:
            warnings.append({
                "code": "patch_word_count_large_change",
                "old_words": original_word_count,
                "new_words": new_word_count,
                "change_ratio": round(ratio, 3),
                "hint": "patch changed word count by >20%; consider full rewrite if scope is large",
            })

    # 3.2 账本一致性验证（committed模式）
    if mode == "committed":
        from ...ledger.ledger import verify_ledger

        # 临时写入 patched 文本以验证账本一致性。这次写入**只是只读探针**，真正落盘在步骤 4，
        # 因此无论校验结果如何都必须还原：否则后续任何一条闸（如文风硬红线）拒绝时，
        # 文件已被探针改成了未经验证的稿子，形成"patch 被拒、正文却已改"的假阴性。
        temp_backup = target_file.read_text(encoding="utf-8")
        pending: LedgerError | None = None
        try:
            atomic_text(target_file, text if text.endswith("\n") else text + "\n")
            verify_result = verify_ledger(store)
            if not verify_result.get("consistent", False):
                pending = LedgerError(
                    "patch_broke_ledger",
                    "patch caused ledger inconsistency; reverted to pre-patch state",
                    {"ledger_issues": verify_result.get("diffs", [])},
                )
        except LedgerError as exc:
            pending = exc
        except Exception:
            # 验证过程出错，不因验证本身失败阻止 patch；正文仍以步骤 4 的落盘为准。
            pending = None
        finally:
            atomic_text(target_file, temp_backup)
        if pending is not None:
            raise pending

    if not bypass_style_check and _style_gate_enabled(store):
        hard = check_style_hard(text, **_style_structure_args(store))
        fails = hard.get("fails", [])
        if fails:
            raise LedgerError(
                "patch_style_hard_failed",
                f"patch resulted in {len(fails)} style hard-gate violation(s)",
                {"fails": fails},
            )

    cfg = store.load_config()
    if mode == "committed" and cfg.get("word_band_enforce", True):
        band = cfg.get("word_band") or {}
        if not int(band.get("min") or 0) <= new_word_count <= int(band.get("max") or 10**9):
            raise LedgerError("patch_word_band_failed", "committed patch must preserve the signed chapter word band", {"words": new_word_count, "word_band": band})

    # 4. 落盘与沉淀自愈
    atomic_text(target_file, text if text.endswith("\n") else text + "\n")
    words = chinese_word_count(text)

    if mode == "staging":
        # 若 output.json 已存在，同步更新 output 中的 prose
        if store.output_path.exists():
            try:
                out = read_json(store.output_path)
                if isinstance(out, dict) and "prose" in out:
                    out["prose"] = text
                    atomic_json(store.output_path, out)
            except Exception:
                pass
        return ok(
            action="patch",
            mode="staging",
            chapter=target_ch,
            file=str(target_file),
            applied_patches=len(patches),
            words=words,
            warnings=warnings,
            hint="staging prose patched; run chapter next or chapter polish-submit/submit",
        )

    # committed 模式：一键基线自愈
    previous_ack = read_json(store.ack_path(target_ch)) if store.ack_path(target_ch).exists() else {}
    resync_info = resync_baseline(store, fix_quotes=True)
    ack_path = store.ack_path(target_ch)
    if ack_path.exists():
        receipt = read_json(ack_path)
        receipt.update({"review_status": "needs_review", "patched_at": now_ts(), "reviewed_prose_hash": previous_ack.get("reviewed_prose_hash") or previous_ack.get("prose_hash")})
        atomic_json(ack_path, receipt)
    return ok(
        action="patch",
        mode="committed",
        chapter=target_ch,
        file=str(target_file),
        applied_patches=len(patches),
        words=words,
        warnings=warnings,
        resynced=True,
        review_required=True,
        resync_details=resync_info,
        hint="hash baseline synchronized; fresh final reviewer must run review ack-patch before this revision is considered reviewed",
    )


def rework_patch(
    store: BookStore,
    *,
    patches: list[tuple[str, str]],
    bypass_style_check: bool = False,
) -> dict[str, Any]:
    """返工定点修：plot_fix/rewrite 轮转出的旧稿就地单点修复，不整章回炉。

    现状链路：组装 BLOCKER → plot_fix 轮转旧稿、清空 staging、phase 回 await_draft
    → 派新 draft worker「复制底稿+只修一句」（轮转后实测仍 ~0.15M token 一个会话）。
    本通道把这一步变成确定性宿主操作：恢复轮转稿 → 锚定替换 → 写回 draft 落位，
    之后的 `chapter draft-submit` 一个闸不少地照跑。修文本身零模型轮次；
    终稿仍要过组装 submit 与 ack 独立终审，安全网不变。
    """
    if not patches:
        raise LedgerError("empty_patches", "at least one (target, replacement) patch pair is required")
    head = store.read_head()
    _require_active(head)
    if head.get("phase") != PHASE_AWAIT_DRAFT:
        raise LedgerError(
            "wrong_phase",
            f"rework-patch only from a rework round ({PHASE_AWAIT_DRAFT} after plot_fix/rewrite); "
            f"got {head.get('phase')}",
            {"phase": head.get("phase")},
        )
    chapter = int(head.get("chapter") or 0)
    if int(head.get("rewrite_count") or 0) < 1:
        raise LedgerError(
            "invalid_args",
            "rework-patch is only for rework rounds (rewrite_count >= 1); a fresh chapter must go "
            "through the drafting worker",
            {"rewrite_count": int(head.get("rewrite_count") or 0)},
        )
    rotation = store.latest_stage_revision(chapter)
    if rotation is None:
        raise LedgerError(
            "missing_prose_for_patch",
            f"no preserved rework text (.revK rotation) for chapter {chapter}; use the rework draft worker instead",
            {"chapter": chapter},
        )
    text = stable_read_text(rotation)
    original_words = chinese_word_count(text)
    for idx, (tgt, rep) in enumerate(patches):
        if not tgt:
            raise LedgerError("empty_patch_target", f"patch[{idx}] target is empty")
        count = text.count(tgt)
        if count == 0:
            raise LedgerError(
                "patch_target_not_found",
                f"patch[{idx}] target substring not found in {Path(rotation).name}",
                {"target_preview": tgt[:60] if len(tgt) > 60 else tgt, "chapter": chapter},
            )
        if count > 1:
            raise LedgerError(
                "patch_target_ambiguous",
                f"patch[{idx}] target matched {count} times in {Path(rotation).name}; must be unique",
                {"target_preview": tgt[:60] if len(tgt) > 60 else tgt, "count": count},
            )
        text = text.replace(tgt, rep, 1)
    warnings: list[dict[str, Any]] = []
    new_words = chinese_word_count(text)
    if original_words > 0:
        ratio = abs(new_words - original_words) / original_words
        if ratio > 0.20:
            warnings.append({
                "code": "patch_word_count_large_change",
                "old_words": original_words,
                "new_words": new_words,
                "change_ratio": round(ratio, 3),
                "hint": "patch changed word count by >20%; a rework this large should go through the drafting worker instead",
            })
    if not bypass_style_check and _style_gate_enabled(store):
        hard = check_style_hard(text, **_style_structure_args(store))
        fails = hard.get("fails", [])
        if fails:
            raise LedgerError(
                "patch_style_hard_failed",
                f"patch resulted in {len(fails)} style hard-gate violation(s)",
                {"fails": fails},
            )
    draft_path = store.draft_text_path(chapter)
    store.accept_stage_text(draft_path, text)
    _log_quality(
        store,
        chapter,
        "rework_patch",
        verdict="patched",
        patches=len(patches),
        words=new_words,
        restored_from=Path(rotation).name,
    )
    return ok(
        action="rework_patch",
        chapter=chapter,
        restored_from=str(rotation),
        applied_patches=len(patches),
        words=new_words,
        warnings=warnings,
        hint=(
            "deterministic targeted fix on the preserved rework text; now run `chapter draft-submit` "
            "(word band / must gates re-run unchanged — the staging file is kept on rejection), then "
            "`chapter next` to reach assembly"
        ),
    )


__all__ = [
    '_commit',
    '_resumable_commit',
    '_commit_tail',
    '_validated_read_quotes',
    'ack_patched_chapter',
    'ack_read',
    '_reopen_p0',
    '_reopen_chapter',
    '_discard_committed_chapter',
    'retry_authorize',
    'book_complete',
    'book_close_early',
    'book_reopen',
    '_VOICE_NOTE_DROP_HINT',
    'patch_prose',
    'rework_patch',
]
