# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：flow
from __future__ import annotations

from typing import Any
from ...ledger.ledger import (coerce_due, load_snapshot)
from ...content.pack import (assemble_pack, scene_budget, word_targets)
from ...infra.store import (PHASE_AWAIT_ACK, PHASE_AWAIT_ASSEMBLY, PHASE_AWAIT_DRAFT, PHASE_AWAIT_POLISH, PHASE_BLOCKED, PHASE_COMPLETE, PHASE_IDLE, PHASE_SUBMITTED, BookStore)
from ...infra.scale import (COMPLETION_MIN_RATIO, scale_contract_errors)
from ...infra.story_map import (plan_story_map_errors)
from ...content.views import (render_assemble_brief)
from ...infra.util import (LedgerError, atomic_json, atomic_json_ordered, atomic_text, chinese_word_count, ok, read_json)

from ._common import (
    _PLOT_SELF_CHECK,
    _ack_hint,
    _attach_prose_hash,
    _book_words_written,
    _creative_advisory,
    _head_transition,
    _log_quality,
    _plan_extend_through,
    _refresh_stage_views,
    _require_active,
    _touch,
    _volume_watermark,
)
from ._commitack import (
    _commit,
)
from ._dispatch import (
    _ACTION_ROLES,
    _dispatch_card,
    _with_execution_contract,
)
from ._planning import (
    _write_plan_extend_brief,
)
from ._submit import (
    _quote_requirements,
)
from ._usage import (
    _usage_summary,
)

def require_story_write_allowed(store: BookStore) -> None:
    """共享写路守卫：书已完结则拒绝改故事状态。

    项目**未初始化**时放行——`kb sync` 这类命令只需 canon 目录，历史上就能在没跑过
    `init` 的目录上工作；守卫只该新增"完结后拒绝"，不该顺手把这条老路堵死。
    """
    try:
        head = store.read_head()
    except LedgerError as exc:
        if exc.code == "not_initialized":
            return
        raise
    _require_active(head)


def chapter_next(store: BookStore, *, card: bool = False) -> dict[str, Any]:
    before_head = store.read_head()
    payload = _chapter_next(store)
    transition = _head_transition(before_head, store.read_head())
    if transition is not None:
        payload["head_transition"] = transition
    if card:
        return _dispatch_card(payload, store=store)
    return payload


def _chapter_next(store: BookStore) -> dict[str, Any]:
    head = store.read_head()
    if (head.get("status") or "active") == "completed" or head.get("phase") == PHASE_COMPLETE:
        return ok(
            action="complete",
            stop=True,
            phase=PHASE_COMPLETE,
            chapter=head.get("chapter"),
            hint="write path is closed; human ends the book",
        )
    phase = head.get("phase") or PHASE_IDLE
    if phase == PHASE_BLOCKED:
        return ok(
            action="blocked",
            stop=True,
            phase=PHASE_BLOCKED,
            chapter=head.get("chapter"),
            blocked=head.get("blocked"),
            hint="human must retry-authorize, change direction, or complete",
        )
    # Commit is deterministic disk/ledger work and may finish even after the model budget trips.
    # The guard applies before every action that would require another model response.
    if phase == PHASE_SUBMITTED:
        committed = _commit(store, head)
        if committed.get("action") != "ack":
            return _with_execution_contract(committed, store)
        post_commit_head = store.read_head()
        post_commit_guard = _usage_guard_action(store, post_commit_head)
        if post_commit_guard is not None:
            post_commit_guard["commit_completed"] = True
            post_commit_guard["resume_phase"] = PHASE_AWAIT_ACK
            post_commit_guard["chapter_path"] = committed.get("chapter_path")
            post_commit_guard["hint"] = (
                "chapter commit completed, but reported usage exceeded the continuation budget; "
                "do not start final-review model work until the session cause is inspected. "
                "The chapter remains in await_ack and can resume without rewriting"
            )
            return post_commit_guard
        return _with_execution_contract(committed, store)
    usage_guard = _usage_guard_action(store, head)
    if usage_guard is not None:
        return usage_guard
    if phase == PHASE_AWAIT_ACK:
        action = _ack_action(store, head)
        authorization = _consume_ack_usage_authorization(store, head)
        if authorization is not None:
            action["usage_authorization_consumed"] = True
            action["usage_authorization_scope"] = "ack_once_inline"
        return _with_execution_contract(action, store)
    if phase == PHASE_AWAIT_DRAFT:
        return _with_execution_contract(_draft_action(store, head), store)
    if phase == PHASE_AWAIT_POLISH:
        return _with_execution_contract(_polish_action(store, head), store)
    if phase == PHASE_AWAIT_ASSEMBLY:
        return _with_execution_contract(_assembly_action(store, head), store)
    plan = store.load_plan()
    scale_errors = scale_contract_errors(plan, store.load_config(), written_words=_book_words_written(store))
    if scale_errors:
        raise LedgerError(
            "book_scale_contract_violation",
            "book scale contract is inconsistent; repair the outline/volume/chapter budgets before continuing",
            {"errors": scale_errors},
        )
    story_errors = plan_story_map_errors(plan, required=store.hatch_manifest_path.exists(), volume_outline_required=store.load_config().get("volume_outline_schema") == "novel-ledger.volume-outlines.v1")
    if story_errors:
        raise LedgerError(
            "event_spine_contract_violation",
            "story spine or signed volume outline is inconsistent; repair the recorded obligations before continuing",
            {"errors": story_errors},
        )
    from ...content.story_review import at_volume_boundary, pending_review, prepare_review
    if at_volume_boundary(store):
        review = pending_review(store)
        if review is not None:
            return _with_execution_contract(prepare_review(store, review), store)
    begun = _begin_next(store, head)
    if begun.get("action") == "complete":
        review = pending_review(store, completing=True)
        if review is not None:
            begun = prepare_review(store, review)
    if begun.get("action") in _ACTION_ROLES:
        # PHASE_IDLE normally means the previous chapter is closed and its budget must not
        # poison the next one.  A model action can nevertheless be *prepared from idle*
        # (`extend_plan`, then the first `draft`).  Re-check against that action's candidate
        # chapter so an ignored usage-record stop cannot buy one extra model request.
        prepared_guard = _usage_guard_action(
            store,
            store.read_head(),
            candidate_chapter=int(begun.get("chapter") or 0),
        )
        if prepared_guard is not None:
            prepared_guard["prepared_action"] = begun.get("action")
            return prepared_guard
    return _with_execution_contract(begun, store)


def _usage_guard_action(
    store: BookStore,
    head: dict[str, Any],
    *,
    candidate_chapter: int | None = None,
) -> dict[str, Any] | None:
    """Pause before the next model-required action when reported usage exceeds a budget."""
    phase = head.get("phase") or PHASE_IDLE
    # Budgets are chapter-scoped. Once ack has closed a chapter, the next fresh session starts
    # with a clean budget; judging PHASE_IDLE against head.chapter would permanently poison all
    # later chapters with the previous chapter's counters and even hide plan_exhausted/complete.
    if phase == PHASE_COMPLETE or (phase == PHASE_IDLE and not candidate_chapter):
        return None
    chapter = int(candidate_chapter or head.get("chapter") or head.get("last_acked_ch") or 0)
    if chapter <= 0:
        return None
    budget = store.load_config().get("usage_budget") or {}
    stop_at = int(budget.get("stop_input_per_chapter") or 0)
    stop_total_at = int(budget.get("stop_total_per_chapter") or 0)
    summary = _usage_summary(store)
    chapter_usage = (summary.get("by_chapter") or {}).get(str(chapter))
    if not chapter_usage:
        return None
    effective = int(chapter_usage.get("effective_input_tokens") or 0)
    total_reported = int(chapter_usage.get("total_tokens") or 0)
    authorization = head.get("usage_authorization") or {}
    authorization_matches = bool(
        phase == PHASE_AWAIT_ACK
        and int(authorization.get("chapter") or 0) == chapter
        and authorization.get("action") == "ack"
    )
    token_authorized = authorization_matches
    reasons = []
    if stop_at > 0 and effective >= stop_at and not token_authorized:
        reasons.append("chapter_input_tokens")
    # total-only 记账通道的熔断：分量未见时按整单总量拦，授权语义与分量口径共用。
    if stop_total_at > 0 and total_reported >= stop_total_at and not token_authorized:
        reasons.append("chapter_total_tokens")
    if not reasons:
        return None
    return ok(
        action="usage_guard",
        stop=True,
        phase=head.get("phase") or PHASE_IDLE,
        chapter=chapter,
        chapter_usage=chapter_usage,
        reasons=reasons,
        budget={"stop_input_per_chapter": stop_at, "stop_total_per_chapter": stop_total_at},
        hint=(
            "reported usage exceeded the continuation policy. Stop new model work and inspect session "
            "history. If commit already completed and only bounded final review remains, a human may use "
            "`chapter usage-authorize --action ack --actor ... --reason ...`; otherwise adjust the budget only "
            "after the cause is resolved"
        ),
    )


def _consume_ack_usage_authorization(
    store: BookStore,
    head: dict[str, Any],
) -> dict[str, Any] | None:
    """Consume the one-shot over-budget ack authorization when the action is issued.

    CLI callers hold the project write lock around `chapter next`, so clearing HEAD here makes
    a second request fail closed even if the first final-review response is lost.  `ack-read`
    remains deterministic and does not need another authorization.
    """
    authorization = head.get("usage_authorization") or {}
    chapter = int(head.get("chapter") or 0)
    if (
        head.get("phase") != PHASE_AWAIT_ACK
        or int(authorization.get("chapter") or 0) != chapter
        or authorization.get("action") != "ack"
    ):
        return None
    head.pop("usage_authorization", None)
    store.write_head(_touch(head))
    _log_quality(
        store,
        chapter,
        "usage_authorization_consumed",
        action="ack",
        actor=authorization.get("actor"),
    )
    return authorization


def _begin_next(store: BookStore, head: dict[str, Any]) -> dict[str, Any]:
    _require_active(head)
    cfg = store.load_config()
    last_c = int(head.get("last_committed_ch") or 0)
    last_a = int(head.get("last_acked_ch") or 0)
    if bool(cfg.get("require_ack", True)) and last_c > last_a:
        raise LedgerError(
            "ack_required",
            f"chapter {last_c} is committed but not ack-read; cannot begin the next chapter",
        )
    chapter = last_c + 1
    planned = {
        int(item.get("chapter") or 0)
        for item in (store.load_plan().get("chapters") or [])
        if int(item.get("chapter") or 0) > 0
    }
    max_planned = max(planned) if planned else 0
    remaining_chapters = sorted(c for c in planned if c > last_c)
    remaining = len(remaining_chapters)
    _plw = cfg.get("plan_low_water")
    low_water = 5 if _plw is None else int(_plw)
    # last_c > 0（已开写）才提示：v3 hatch 按契约恰好签 3 章种子，新书首跑必然 remaining ≤ 5，
    # 若在第一章之前提示扩拍，就违反开书验收承诺「首次 next 直接返回 draft」
    # （SKILL.md / hatch-wizard.md §四）。第一章提交后库存若仍低于水位，提示照常触发。
    if (
        low_water > 0
        and last_c > 0
        and chapter in planned
        and remaining <= low_water
        and remaining > 0
        and last_c < max_planned
    ):
        nudged_at = head.get("plan_low_water_nudged_at")
        if nudged_at != max_planned:
            head["plan_low_water_nudged_at"] = max_planned
            store.write_head(_touch(head))
            # 逾期伏笔随扩纲提示回流：plan 侧闭环——策划编辑把它们排进
            # 新章拍的 effects.hooks 后，提交机检（expected_delta）会强制逐条兑现，
            # 伏笔回收从“写者顺手”变成“计划内机器可验”。
            from ...ledger.ledger import select_hooks

            picked, _omitted = select_hooks(store, chapter=chapter, cap=5)
            overdue_hooks = [
                {
                    "id": h.get("id"),
                    "text": str(h.get("text") or ""),
                    "due": h.get("due"),
                    "overdue_by": chapter - (coerce_due(h.get("due")) or 0),
                }
                for h in picked
                if (coerce_due(h.get("due")) or 0) < chapter
            ]
            extend_through, span_mode = _plan_extend_through(store, cfg, max_planned, last_c)
            return ok(
                action="extend_plan",
                stop=False,
                phase=head.get("phase") or PHASE_IDLE,
                chapter=chapter,
                remaining_in_plan=remaining,
                max_planned_ch=max_planned,
                suggest_from=max_planned + 1,
                plan_extend_span=span_mode,
                extend_through_ch=extend_through,
                creative_advisory=_creative_advisory(store, remaining_chapters),
                volume_watermark=_volume_watermark(store, head),
                overdue_hooks=overdue_hooks or None,
                plan_worker_brief=_write_plan_extend_brief(
                    store,
                    {
                        "chapter": chapter,
                        "suggest_from": max_planned + 1,
                        "extend_through_ch": extend_through,
                        "plan_extend_span": span_mode,
                        "remaining_in_plan": remaining,
                        "max_planned_ch": max_planned,
                        "overdue_hooks": overdue_hooks or None,
                        "volume_watermark": _volume_watermark(store, head),
                    },
                ),
                hint=(
                    f"plan low water: {remaining} chapter(s) left (≤{low_water}); "
                    f"this ONE extension must COVER chapters {max_planned + 1} THROUGH {extend_through}"
                    f" (plan_extend_span={span_mode}) — topping up just above the waterline burns one"
                    " whole plan-worker round every few chapters (measured: 13 rounds ≈1.1M tokens"
                    " per 63 chapters). "
                    "扩纲是单发独立任务：宿主应另派一个一次性 plan worker（独立会话）执行本 action，"
                    "禁止并入章节 worker 会话；若宿主能为 spawn 指定模型，按派发手册 §9 用 strongest 档。"
                    "每次扩充都必须先按创意编辑（creative）角色签出结构化阶段小纲（Phase Brief）：逐项引用"
                    "主线事件、支线事件、时间线事件与起伏曲线阶段；再由策划编辑（planning）按该阶段编拍。"
                    "每章 5 个事件，每个事件是一场戏（当场发生、有来往、有未完），"
                    "不是拍点标签；字数是结果，偏短先加事件不注水。"
                    + (
                        "overdue_hooks 列出的是逾期未收伏笔：编拍时应优先安排回收，"
                        "把对应 hook id 写进新章拍的 effects.hooks（提交机检按 expected_delta 强制兑现）。"
                        if overdue_hooks
                        else ""
                    )
                ),
            )
    if chapter not in planned:
        if last_c <= 0:
            raise LedgerError(
                "empty_plan" if not planned else "missing_chapter_plan",
                "plan has no chapter to begin; add chapter beats before writing",
                {"chapter": chapter, "planned": sorted(planned)},
            )
        if last_c < max_planned:
            raise LedgerError(
                "missing_chapter_plan",
                f"plan has no chapter {chapter}; gap before max planned chapter {max_planned}",
                {"chapter": chapter, "planned": sorted(planned), "max_planned_ch": max_planned},
            )
        written = _book_words_written(store)
        target = int(cfg.get("book_words") or 0)
        progress = written / target if target > 0 else 0.0
        if target > 0 and progress < COMPLETION_MIN_RATIO:
            extend_through, span_mode = _plan_extend_through(store, cfg, max_planned, last_c)
            return ok(
                action="extend_plan",
                stop=False,
                reason="plan_exhausted_before_book_target",
                phase=head.get("phase") or PHASE_IDLE,
                chapter=last_c,
                last_committed_ch=last_c,
                last_acked_ch=last_a,
                book_words=target,
                book_words_written=written,
                book_words_progress=round(progress, 4),
                suggest_from=max_planned + 1,
                plan_extend_span=span_mode,
                extend_through_ch=extend_through,
                plan_worker_brief=_write_plan_extend_brief(
                    store,
                    {
                        "chapter": last_c,
                        "suggest_from": max_planned + 1,
                        "extend_through_ch": extend_through,
                        "plan_extend_span": span_mode,
                        "remaining_in_plan": 0,
                        "max_planned_ch": max_planned,
                    },
                ),
                hint=(
                    f"plan is exhausted while the signed book target is still incomplete; this ONE"
                    f" extension must COVER chapters {max_planned + 1} THROUGH {extend_through}"
                    f" (plan_extend_span={span_mode}). "
                    "create a signed phase brief aligned to mainline/subplot/timeline/tension event-spine layers, "
                    "then extend chapter beats. Do not close the book or write a whole-book ending. "
                    "Run this extension as a DEDICATED single-shot session — the host spawns a separate "
                    "plan worker (strongest tier if the host can pick a model per dispatch manual §9); "
                    "never inline it into a chapter worker session"
                ),
            )
        return ok(
            action="complete",
            stop=True,
            reason="plan_exhausted_near_book_target",
            phase=head.get("phase") or PHASE_IDLE,
            chapter=last_c,
            last_committed_ch=last_c,
            last_acked_ch=last_a,
            book_words_progress=round(progress, 4),
            hint="plan is exhausted near the signed target; run the full-book audit before author completion",
        )
    pack = assemble_pack(store, chapter)
    atomic_json(store.current_pack_path, pack)
    atomic_json(store.pack_archive_path(chapter), pack)
    _refresh_stage_views(store, pack, chapter)
    if store.output_path.exists():
        store.output_path.unlink()
    head.update(
        {
            "phase": PHASE_AWAIT_DRAFT,
            "chapter": chapter,
            "pack_hash": pack["pack_hash"],
            "prose_hash": None,
            "rewrite_count": 0,
            "style_metrics_retry": 0,
            "style_metrics_pending": False,
            "polish_anchor_retry": 0,
            "polish_anchor_pending": False,
            "blocked": None,
        }
    )
    store.write_head(_touch(head))
    _log_quality(store, chapter, "begin")
    return _draft_action(store, head)


# （views.render_writing_brief）必须用同一个 aim 与同一个场次尺度，否则又会漂成
# "任务书说 2800 字、简报说 2500 字"两套说法。
_scene_budget = scene_budget


def _hooks_due_unplanned(store: BookStore, chapter: int) -> list[dict[str, Any]] | None:
    """章纲层防线：到期未排拍的伏笔在开写前亮出来，而不是写完才发现——正文里带时点的
    承诺若章拍未排，蒸发只能靠单章情节自检事后抓。

    本预检在 draft action 上报 advisory：快照里 due ≤ 本章且仍
    open、而本章章拍 effects.hooks 未引用的条目。宿主可回炉章拍（排出回收场景）或
    让正文明确改期；软提示不硬拦，与 governance「逾期不硬拦」口径一致。
    """
    snap = load_snapshot(store)
    due_open = [
        h
        for h in snap.get("hooks") or []
        if str(h.get("status") or "open") not in ("paid", "closed", "abandoned")
        and 0 < (coerce_due(h.get("due")) or 0) <= chapter
    ]
    if not due_open:
        return None
    try:
        beats = store.chapter_plan(chapter).get("beats") or []
    except LedgerError:
        beats = []
    planned_ids: set[str] = set()
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        for entry in (beat.get("effects") or {}).get("hooks") or []:
            if isinstance(entry, dict) and entry.get("id"):
                planned_ids.add(str(entry["id"]))
    unplanned = [
        {"id": h.get("id"), "text": str(h.get("text") or ""), "due": h.get("due")}
        for h in due_open
        if str(h.get("id")) not in planned_ids
    ]
    return unplanned[:8] or None


def _draft_action(store: BookStore, head: dict[str, Any]) -> dict[str, Any]:
    """阶段一：执笔编辑只读 draft 视图，写纯文本草稿。"""
    chapter = int(head.get("chapter") or 0)
    # 字数带前置给到任务书：草稿就奔着下限以上写（建议留 100 字余量），
    # 不要等 submit 机检才发现差百来字。
    band = dict(store.load_config().get("word_band") or {})
    targets = word_targets(band)
    band_min = targets["min"]
    band_max = targets["max"]
    aim_chars = targets["aim_chars"]
    budget = _scene_budget(aim_chars, floor=band_min)
    prior = store.latest_stage_revision(chapter)
    return ok(
        action="draft",
        stop=False,
        phase=PHASE_AWAIT_DRAFT,
        chapter=chapter,
        draft_pack_path=str(store.draft_pack_path(chapter)),
        draft_output_path=str(store.draft_text_path(chapter)),
        # 返工轮：上一稿已存为轮转档——复制回 draft_output_path 后只修
        # review_findings_path 所引 BLOCKER 的最小定点，其余逐字保留；
        # 字数带/must/state_delta 机检照常全跑，不因保留旧稿降门槛。
        prior_draft_path=str(prior) if prior else None,
        prior_draft_hint=(
            "REWORK ROUND: copy prior_draft_path to draft_output_path, then apply ONLY the minimal "
            "targeted fix for the BLOCKER quoted in review_findings_path; keep every other sentence "
            "verbatim. All machine gates re-run unchanged. For a one-sentence fix the HOST may skip "
            "this worker entirely: `chapter rework-patch --target ... --replacement ...` restores and "
            "patches the preserved text deterministically (zero model rounds), then `chapter "
            "draft-submit` re-runs the same gates."
        )
        if prior
        else None,
        word_band=band or None,
        aim_chars=aim_chars,
        scene_budget=budget,
        # 进入新卷的首章：给宿主一行“该考虑采用创意编辑角色了”的 advisory（不挡流程）。
        creative_advisory=_creative_advisory(store, [chapter]),
        # 卷合同水位（紧凑版）：warn=True 时无人值守宿主应在跨卷前安排签卷。
        volume_watermark=_volume_watermark(store, head),
        # 章纲层防线：到期未排拍的伏笔在开写前亮出来（软提示，宿主回炉章拍或让正文改期）。
        hooks_due_unplanned=_hooks_due_unplanned(store, chapter),
        hint=(
            "execute the DRAFTING-EDITOR role under the execution contract; read ONLY draft_pack_path "
            "(with voice_content_text guide); write raw prose text to draft_output_path without echoing it; "
            f"word band {band_min}-{band_max} counts hanzi only (no punctuation/digits/latin) — aim for "
            f"aim_chars {aim_chars}: undershooting the floor is the #1 historical cause of rework loops; "
            f"plan roughly {budget['scenes']} scene(s) of ~{budget['chars_per_scene']} hanzi each "
            "(scene_budget) rather than writing to a word quota. `chapter draft-submit` checks the word band; "
            "a short draft is rejected on the spot "
            f"(draft_rejected) and must be expanded in ONE pass by whole scene(s) (~{budget['chars_per_scene']} "
            "hanzi each), never by sprinkling filler words. NB: `draft-submit` reads draft_output_path "
            "itself and takes NO path argument"
        ),
    )


def _polish_action(store: BookStore, head: dict[str, Any]) -> dict[str, Any]:
    """阶段二：文风编辑只读草稿 + polish 视图，写纯文本终稿。"""
    chapter = int(head.get("chapter") or 0)
    metrics_path = None
    if head.get("style_metrics_pending"):
        candidate = store.style_metrics_path(chapter)
        if candidate.exists():
            metrics_path = str(candidate)
    anchors_path = None
    if head.get("polish_anchor_pending"):
        candidate = store.polish_anchor_path(chapter)
        if candidate.exists():
            anchors_path = str(candidate)
    # 字数缺口前置：把下限与当前草稿汉字数直接算给写者，而不是等 submit 机检才打回
    # （现场实测会连续 5–6 轮差 100–200 字，每轮都白写一次全文）。
    cfg = store.load_config()
    band = dict(cfg.get("word_band") or {})
    draft_path = store.draft_text_path(chapter)
    draft_chars = chinese_word_count(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else 0
    band_min = int(band.get("min") or 0)
    band_max = int(band.get("max") or 0)
    aim_chars = word_targets(band)["aim_chars"]
    # 章首 begin/retry 会清 staging，润色稿存在即说明上一轮已写过一版：允许原位修而非从草稿重写。
    rerun = store.polished_text_path(chapter).exists()
    chars_to_min = max(0, band_min - draft_chars)
    budget = _scene_budget(aim_chars, floor=band_min, gap_chars=chars_to_min)
    return ok(
        action="polish",
        stop=False,
        phase=PHASE_AWAIT_POLISH,
        chapter=chapter,
        draft_output_path=str(store.draft_text_path(chapter)),
        polish_pack_path=str(store.polish_pack_path(chapter)),
        polished_output_path=str(store.polished_text_path(chapter)),
        style_metrics_path=metrics_path,
        polish_anchor_path=anchors_path,
        word_band=band or None,
        aim_chars=aim_chars,
        draft_chars=draft_chars,
        chars_to_min=chars_to_min,
        scene_budget=budget,
        hint=(
            "execute the VOICE-EDITOR role under the execution contract; read ONLY draft_output_path + polish_pack_path "
            "(voice_writing_text = 润色文风手册；内置文风取总手册，自定义文风可取独立 writer companion) "
            "and style_metrics_path IF present "
            "(machine ✗ list; fix only those listed items) and polish_anchor_path IF present "
            "(prose anchors broken at last polish closing: beats/must, continuity or glossary; "
            "fix exactly those, keep everything else)"
            + (
                "; polished_output_path already holds your previous attempt — you may fix the listed "
                "items in place instead of rewriting from the draft"
                if rerun
                else ""
            )
            + f"; word band {band_min}-{band_max} counts hanzi only (no punctuation/digits/latin) — "
            f"aim for aim_chars {aim_chars}; draft_chars is the current draft count, chars_to_min how "
            "many more you need"
            + "; if short, develop meaningful action and reaction within the draft's existing scenes; "
            "if the established events cannot support the word band, report the gap to the managing editor "
            "rather than inventing events; a short polished draft is rejected on the spot "
            "(polish_rejected, file kept); `chapter polish-submit` checks the word band and, when enabled, style "
            "(it reads polished_output_path itself and takes NO path argument; "
            "if you already got polish_accepted, do NOT submit again — the phase has moved on); "
            "scene-level rewrite with voice manual; "
            "write polished prose text to polished_output_path without echoing it; then run `chapter polish-submit`"
        ),
    )


def _assembly_action(store: BookStore, head: dict[str, Any]) -> dict[str, Any]:
    """阶段三：事实编辑 只读终稿 + assemble 视图，产出 submit JSON。

    compact 档没有独立剧情审校，由事实编辑在组装时兼做情节自检（`_PLOT_SELF_CHECK`）：
    它本就通读终稿并核对 beats/账本，把同一遍阅读对准情节与能力边界，省一整个阶段。
    """
    chapter = int(head.get("chapter") or 0)
    # 旧书的 assemble 包先于简报特性存在（升级/接续场景）：派发前按盘上视图补渲染一次，
    # 保证 action 指到的 assemble_brief_path 永远可读。
    brief_path = store.assemble_brief_path(chapter)
    pack_path = store.assemble_pack_path(chapter)
    if not brief_path.exists() and pack_path.exists():
        stale_view = read_json(pack_path)
        if isinstance(stale_view, dict) and stale_view:
            atomic_text(brief_path, render_assemble_brief(stale_view))
    # 新鲜度凭证补烙：视图可能建于终稿落盘之前（draft-only 直通）。派发组装 worker
    # 前按现行终稿重烙 prose_hash 并重渲染简报，模板才预填得到正确凭证。
    if pack_path.exists():
        stamped_view = read_json(pack_path)
        if isinstance(stamped_view, dict) and stamped_view:
            before = stamped_view.get("prose_hash")
            _attach_prose_hash(store, chapter, stamped_view)
            if stamped_view.get("prose_hash") != before:
                atomic_json_ordered(pack_path, stamped_view)
                atomic_text(brief_path, render_assemble_brief(stamped_view))
    payload = ok(
        action="assemble",
        stop=False,
        phase=PHASE_AWAIT_ASSEMBLY,
        chapter=chapter,
        pack_hash=head.get("pack_hash"),
        polished_output_path=str(store.polished_text_path(chapter)),
        assemble_brief_path=str(store.assemble_brief_path(chapter)),
        assemble_pack_path=str(store.assemble_pack_path(chapter)),
        submit_output_path=str(store.staging_dir / f"assembly-{chapter:04d}.json"),
        hint=(
            "execute the LEDGER-EDITOR role under the execution contract. Read assemble_brief_path FIRST and IN FULL — "
            "it is the one-pass linear taskbook (beats, expected_delta, continuity, ledger refs, name whitelist AND the "
            "submit-JSON template with pack_hash prefilled); do NOT dump the pack JSON field-by-field with scripts and "
            "do NOT browse skill docs for the output format. Then read polished_output_path in full. "
            "Write the submit JSON to submit_output_path ONCE (targeted edits only for revisions; never rewrite the whole "
            "file, never read it back) and STOP — the HOST executes `chapter submit` (deterministic gate; worker "
            "self-check text is advisory evidence, never a substitute). quotes are machine-verified verbatim at submit "
            "— do not hand-check characters; fix per diagnostics when rejected. fix_assembly keeps the final prose "
            "and rewrite budget. assemble_pack_path stays the machine truth for targeted lookups only"
            " (exact pack_hash echo, delta_schema), not the reading surface"
        ),
    )
    payload["plot_self_check"] = list(_PLOT_SELF_CHECK[1])
    return payload


def _ack_action(store: BookStore, head: dict[str, Any]) -> dict[str, Any]:
    chapter = int(head["chapter"])
    quote_requirements = _quote_requirements(store)
    payload: dict[str, Any] = {
        "action": "ack",
        "stop": False,
        "phase": PHASE_AWAIT_ACK,
        "chapter": chapter,
        "pack_hash": head.get("pack_hash"),
        "prose_hash": head.get("prose_hash"),
        "chapter_path": str(store.chapter_md_path(chapter)),
        "quote_requirements": quote_requirements,
        "hint": _ack_hint(store, head, quote_requirements),
    }
    return ok(**payload)


__all__ = [
    'require_story_write_allowed',
    'chapter_next',
    '_chapter_next',
    '_usage_guard_action',
    '_consume_ack_usage_authorization',
    '_begin_next',
    '_scene_budget',
    '_hooks_due_unplanned',
    '_draft_action',
    '_polish_action',
    '_assembly_action',
    '_ack_action',
]
