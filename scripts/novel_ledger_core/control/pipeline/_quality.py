# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：quality
from __future__ import annotations

from typing import Any
from ...content.gates import (recheck_beat_anchor_blockers)
from ...content.pack import (assemble_pack, inputs_fingerprint)
from ...infra.store import (PHASE_AWAIT_ASSEMBLY, PHASE_AWAIT_DRAFT, PHASE_AWAIT_POLISH, PHASE_BLOCKED, BookStore)
from ...content.views import (make_draft_view, make_polish_view)
from ...infra.util import (LedgerError, atomic_json, canonical_json, now_ts, ok, read_json, sha256_text)

from ._common import (
    _ASSEMBLY_ONLY_ISSUES,
    _log_quality,
    _polish_disabled,
    _preserve_rework_text,
    _refresh_stage_views,
    _touch,
)

def _style_metrics_gate_failed(
    store: BookStore,
    head: dict[str, Any],
    chapter: int,
    hard: dict[str, Any],
    style_warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    """文风硬红线失败：✗ 清单落盘；可重试则回 polish，满额停线 blocked。"""
    metrics_path = store.style_metrics_path(chapter)
    atomic_json(
        metrics_path,
        {
            "schema": "novel-ledger.style-metrics.v1",
            "chapter": chapter,
            "ok": False,
            "pack_hash": head.get("pack_hash"),
            "generated_at": now_ts(),
            "fails": hard["fails"],
            "style_direction": style_warnings,
        },
    )
    mlimit = int(store.load_config().get("style_metrics_limit") or 2)
    mcount = int(head.get("style_metrics_retry") or 0) + 1
    head["style_metrics_retry"] = mcount
    head["blocked"] = None
    _log_quality(
        store,
        chapter,
        "style_machine",
        verdict="fail",
        retry=mcount,
        limit=mlimit,
        fail_count=len(hard.get("fails") or []),
    )
    if mcount >= mlimit:
        blocked = {
            "chapter": chapter,
            "reason": "style_metrics_failed",
            "style_metrics_retry": mcount,
            "style_metrics_limit": mlimit,
            "fails": hard["fails"],
            "metrics_path": str(metrics_path),
        }
        head["phase"] = PHASE_BLOCKED
        head["blocked"] = blocked
        head["style_metrics_pending"] = False
        store.write_head(_touch(head))
        return ok(
            verdict="blocked",
            stop=True,
            phase=PHASE_BLOCKED,
            chapter=chapter,
            blocked=blocked,
            fails=hard["fails"],
            style_metrics_path=str(metrics_path),
            hint=(
                "style hard-gate failed after the retry limit: human must decide — "
                "patch wording in place then `chapter retry-authorize --action style` (light unlock, "
                "no rollback), raise style_metrics_limit, or full `retry-authorize` rewrite; "
                "do not silently loop"
            ),
        )
    head["style_metrics_pending"] = True
    head["phase"] = PHASE_AWAIT_POLISH
    store.write_head(_touch(head))
    # 与 `_polish_anchor_gate_failed` 同构：保留上一版润色稿。明显出戏的话术
    # 多半改词即可消除，写者可原地修复后重交；polish-submit 每次都重跑逐字机检，
    # 不存在绕过空间。此前这里直接删除润色稿，失败一次就整篇从零重写。
    if store.output_path.exists():
        store.output_path.unlink()
    return ok(
        verdict="style_metrics_failed",
        phase=PHASE_AWAIT_POLISH,
        chapter=chapter,
        fails=hard["fails"],
        fail_count=len(hard["fails"]),
        style_metrics_retry=mcount,
        style_metrics_limit=mlimit,
        style_metrics_path=str(metrics_path),
        polished_output_path=str(store.polished_text_path(chapter)),
        polished_kept=True,
        hint=(
            "style hard-gate failed: the previous polished draft is KEPT at polished_output_path — "
            "fix only the listed red-line items in place (or re-polish fully if needed), then run "
            "`chapter polish-submit` again; the hard gate re-runs on every submit "
            f"(retry {mcount}/{mlimit})"
        ),
    )


def _polish_anchor_gate_failed(
    store: BookStore,
    head: dict[str, Any],
    chapter: int,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """润色收口的内容锚点失败：问题清单落盘；可重润则回 polish，满额停线 blocked。

    与 `_style_metrics_gate_failed` 同构（落盘 → 计数 → 回 polish → 满额 blocked），
    区别只在判据来源：那个查文风硬红线，这个查**内容锚点**
    （beats/must 兑现、连续性防失忆、glossary 术语归一化）。

    为什么要在这里提前拦：这三样原本只在 `chapter submit` 才查，而 submit 对正文类问题
    按约定回 `draft` 整链重写。锚点问题多半是**润色时改丢的**（少带一个 must、把已相识的
    人物写成初见、引入了非规范术语），那些都该由文风编辑自己修——回 polish 比回 draft 便宜得多。
    满额仍不过则停线：锚点缺失也可能源自草稿没兑现，那种情况下重润多少次都不会好，
    必须由总编辑裁决（从 blocked 走 `retry-authorize` 回 draft 重写）。
    """
    anchor_path = store.polish_anchor_path(chapter)
    atomic_json(
        anchor_path,
        {
            "schema": "novel-ledger.polish-anchors.v1",
            "chapter": chapter,
            "ok": False,
            "pack_hash": head.get("pack_hash"),
            "generated_at": now_ts(),
            "issues": issues,
            "issue_count": len(issues),
        },
    )
    limit = int(store.load_config().get("polish_anchor_limit") or 2)
    count = int(head.get("polish_anchor_retry") or 0) + 1
    head["polish_anchor_retry"] = count
    head["blocked"] = None
    _log_quality(
        store,
        chapter,
        "polish_anchor",
        verdict="fail",
        retry=count,
        limit=limit,
        issue_count=len(issues),
    )
    if count >= limit:
        blocked = {
            "chapter": chapter,
            "reason": "polish_anchor_failed",
            "polish_anchor_retry": count,
            "polish_anchor_limit": limit,
            "issues": issues,
            "polish_anchor_path": str(anchor_path),
        }
        head["phase"] = PHASE_BLOCKED
        head["blocked"] = blocked
        head["polish_anchor_pending"] = False
        store.write_head(_touch(head))
        return ok(
            verdict="blocked",
            stop=True,
            phase=PHASE_BLOCKED,
            chapter=chapter,
            blocked=blocked,
            issues=issues,
            polish_anchor_path=str(anchor_path),
            hint=(
                "prose anchors still broken after the re-polish limit: human must decide. "
                "If the anchor was never realized in the draft (missing must word, plot fact), "
                "this is a draft-side defect — `retry-authorize` from blocked goes back to draft; "
                "if it is a polish-side slip (dropped a must word, non-canonical term), "
                "fix the manual/adjust the limit. Do not silently loop"
            ),
        )
    head["polish_anchor_pending"] = True
    head["phase"] = PHASE_AWAIT_POLISH
    store.write_head(_touch(head))
    # 保留上一版润色稿：must 锚点缺失通常只是丢词，写者可原地补锚点后重交，
    # polish-submit 会重新逐字校验，不存在绕过机检的空间。
    if store.output_path.exists():
        store.output_path.unlink()
    return ok(
        verdict="polish_anchor_failed",
        phase=PHASE_AWAIT_POLISH,
        chapter=chapter,
        issues=issues,
        issue_count=len(issues),
        polish_anchor_retry=count,
        polish_anchor_limit=limit,
        polish_anchor_path=str(anchor_path),
        polished_output_path=str(store.polished_text_path(chapter)),
        hint=(
            "prose anchors failed at polish closing; the previous polished draft is KEPT at "
            "polished_output_path — fix only the listed anchor items in place (or re-polish "
            "fully if the prose needs it), then run chapter polish-submit again; anchors are "
            f"re-verified verbatim on every submit (retry {count}/{limit})"
        ),
    )


def _stale_plot_self_check(
    store: BookStore,
    head: dict[str, Any],
    pack: dict[str, Any],
    output: dict[str, Any],
    polished: str,
) -> dict[str, Any] | None:
    """组装件自带的新鲜度凭证与现行终稿不符 → 拒收并指路重跑组装。

    实测死锁形态：prose 返工后宿主补齐旧 assembly JSON 的 quote 再重提，
    里面过期的 beat 锚词 BLOCKER 被无限回收。缺凭证（旧流程产物）放行——确定性
    复检（recheck_beat_anchor_blockers）兜底；凭证不符则不再机检，直接回路由。
    """
    declared = str(output.get("prose_hash") or "").strip()
    if not declared:
        return None
    current = "sha256:" + sha256_text(polished)
    if declared == current:
        return None
    return ok(
        verdict="stale_plot_findings",
        stop=False,
        phase=head.get("phase"),
        chapter=head.get("chapter"),
        pack_hash=pack.get("pack_hash"),
        declared_prose_hash=declared,
        current_prose_hash=current,
        hint=(
            "assembly output's plot_findings were judged against a DIFFERENT prose revision"
            " (prose_hash mismatch; the prose was reworked after this assembly was built)."
            " The old submit JSON is void — do NOT patch quotes in it and resubmit:"
            " run `chapter next` and dispatch a FRESH assemble stage worker so plot_findings"
            " are re-judged against the current prose, then submit"
        ),
    )


def _close_rechecked_findings(store: BookStore, chapter: int, pack: dict[str, Any], prose: str) -> None:
    """兑现即闭合：本章 journal 里仍 open 的 beat 锚词类发现，现行正文已兑现的就地闭合。

    plot_fix 轮按设计把 BLOCKER 记成 open（返工 worker 要看着它修）；prose 修好、
    本章过闸后这些发现若继续挂 open，会永远留在 review list，还可能被下一轮组装
    worker 当现役发现再次吞进 plot_findings。以确定性复检为准闭合，非锚词类不动。
    """
    from ...content.gates import _BEAT_ANCHOR_FINDING_CODES
    from ...content.reviews import findings_state, resolve_finding

    state = findings_state(store)
    for identity, item in state.items():
        if item.get("chapter") != chapter or item.get("disposition") != "open":
            continue
        if str(item.get("code") or "") not in _BEAT_ANCHOR_FINDING_CODES:
            continue
        remaining, _cleared = recheck_beat_anchor_blockers(pack, prose, [dict(item)])
        if remaining:
            continue
        resolve_finding(
            store,
            identity,
            disposition="closed",
            actor="submit-gate",
            reason="beat anchor deterministically rechecked present in current prose",
        )


def _reassemble_if_stale(
    store: BookStore,
    head: dict[str, Any],
    pack: dict[str, Any],
) -> dict[str, Any] | None:
    """pack 装配后上游（plan 章拍/kb/voice/pack_caps）被改 → 自动重装配。

    回退深度由**新旧视图内容**决定，不由上游全量指纹决定——指纹只是触发器：
    - draft 视图变 → 回 draft、清两份 staging（旧文确实配不上新包）；
    - 仅后续视图变 → 回 polish、保留草稿；
    - 本章视图全部一致（kb 加无关卡、别章 plan 调整等）→ 只重建视图与 canonical
      pack，staging 与 phase 全部保持，submit 按新 pack 继续走机检（组装件重对
      hash 即可，正文零重写）。
    不消耗 rewrite 配额（不是写者的错）。
    """
    from ...content.pack import assemble_pack, inputs_fingerprint

    chapter = int(head["chapter"])
    expected = inputs_fingerprint(store, chapter)
    if pack.get("inputs_fingerprint") == expected:
        return None
    new_pack = assemble_pack(store, chapter)

    def _view_changed(path, make) -> bool:
        try:
            old_view = read_json(path)
        except LedgerError:
            return True
        return canonical_json(old_view) != canonical_json(make(new_pack))

    draft_changed = _view_changed(store.draft_pack_path(chapter), make_draft_view)
    polish_changed = _view_changed(store.polish_pack_path(chapter), make_polish_view)
    if draft_changed:
        scope, new_phase = "draft", PHASE_AWAIT_DRAFT
    elif polish_changed:
        # 只写作模式没有润色相位：视图漂移一律按草稿重写收口。
        scope, new_phase = (
            ("draft", PHASE_AWAIT_DRAFT)
            if _polish_disabled(store)
            else ("polish", PHASE_AWAIT_POLISH)
        )
    else:
        scope, new_phase = "none", None

    atomic_json(store.current_pack_path, new_pack)
    atomic_json(store.pack_archive_path(chapter), new_pack)
    _refresh_stage_views(store, new_pack, chapter, scope=scope)
    head.update({"pack_hash": new_pack["pack_hash"]})
    if scope == "draft":
        head.update(
            {
                "prose_hash": None,
                "rewrite_count": 0,
                "style_metrics_retry": 0,
                "style_metrics_pending": False,
                "polish_anchor_retry": 0,
                "polish_anchor_pending": False,
                "blocked": None,
            }
        )
    elif scope == "polish":
        # 草稿仍有效：只重置润色层计数与终稿指纹；rewrite_count 是 submit 级预算，保留
        head.update(
            {
                "prose_hash": None,
                "style_metrics_retry": 0,
                "style_metrics_pending": False,
                "polish_anchor_retry": 0,
                "polish_anchor_pending": False,
                "blocked": None,
            }
        )
    if new_phase is not None:
        head["phase"] = new_phase
    store.write_head(_touch(head))
    _log_quality(store, chapter, "begin", reason="stale_pack")
    if scope == "none":
        # 视图未变：不算 stale，本次 submit 换新 pack 继续走（组装件补对 hash 即可）
        return None
    return ok(
        verdict="stale_pack",
        phase=head["phase"],
        chapter=chapter,
        pack_hash=new_pack["pack_hash"],
        kept_draft=(scope == "polish"),
        hint=(
            "plan/kb/voice changed after pack assembly; the {} view changed — rerun from {} "
            "(upstream inputs outside this chapter's views did NOT invalidate the staged prose)"
        ).format("draft" if scope == "draft" else "polish", head["phase"]),
    )


def _gate_fail(
    store: BookStore,
    head: dict[str, Any],
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cfg = store.load_config()
    limit = int(cfg.get("rewrite_limit") or 1)
    count = int(head.get("rewrite_count") or 0)
    chapter = int(head.get("chapter") or 0)
    codes = {str(issue.get("code") or "") for issue in issues}
    if codes and codes <= _ASSEMBLY_ONLY_ISSUES:
        # 账本字段填错不属于正文返工；保留终稿和 rewrite 预算。
        if store.output_path.exists():
            store.output_path.unlink()
        _log_quality(store, chapter, "submit", verdict="fix_assembly", codes=sorted(codes))
        return ok(
            verdict="fix_assembly",
            phase=PHASE_AWAIT_ASSEMBLY,
            chapter=chapter,
            pack_hash=head.get("pack_hash"),
            rewrite_count=count,
            violations=issues,
            warnings=warnings or [],
            hint="correct the reported assembly fields and submit again",
        )
    if count < limit:
        head["rewrite_count"] = count + 1
        head["phase"] = PHASE_AWAIT_DRAFT
        preserved = _preserve_rework_text(store, chapter)
        stale_paths = [store.output_path, store.draft_text_path(chapter), store.polished_text_path(chapter)]
        for stale in stale_paths:
            if stale.exists():
                stale.unlink()
        store.write_head(_touch(head))
        _log_quality(
            store,
            int(head["chapter"]),
            "submit",
            verdict="rewrite",
            codes=sorted({str(i.get("code") or "") for i in issues}),
        )
        _log_quality(store, chapter, "begin", reason="submit_gate_rewrite")
        return ok(
            verdict="rewrite",
            phase=head["phase"],
            chapter=head["chapter"],
            pack_hash=head.get("pack_hash"),
            rewrite_count=head["rewrite_count"],
            preserved_draft_path=str(preserved) if preserved else None,
            violations=issues,
            warnings=warnings or [],
            recovery=_rewrite_recovery(issues),
            hint="same canonical pack; the preserved prior text is at preserved_draft_path — "
            "targeted minimal fix on it, not a rewrite from scratch",
        )
    blocked = {
        "chapter": head.get("chapter"),
        "reason": "gate_failed",
        "violations": issues,
        "rewrite_count": count,
    }
    head["phase"] = PHASE_BLOCKED
    head["blocked"] = blocked
    store.write_head(_touch(head))
    # 机械单点 + 未入账：解锁命令写进回执，宿主不必翻 recovery.md 试错
    # （实测 rework-patch/draft-submit 连吃两个 wrong_phase 后才找到 retry-authorize）。
    # 集合保持保守：beat_missed/字数带等需要真实内容工作的不在此列，仍走人工裁决。
    hint = "human must intervene; do not skip the chapter"
    if (
        codes
        and codes
        <= {
            "beat_token_missing",
            "expected_delta_missing",
            "expected_delta_shape_mismatch",
            "unnamed_in_pack",
            "foreign_fragment",
        }
        and int(head.get("last_committed_ch") or 0) < chapter
    ):
        hint = (
            "nothing committed and every violation is single-point mechanical: "
            "`retry-authorize --actor <role> --reason <why>` restarts the chapter from "
            "await_draft; dispatch a rework worker on the latest .revK preserved draft "
            "with these violations, then draft-submit → next → submit"
        )
    return ok(
        verdict="blocked",
        stop=True,
        phase=PHASE_BLOCKED,
        chapter=head["chapter"],
        blocked=blocked,
        warnings=warnings or [],
        hint=hint,
    )


def _rewrite_recovery(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """rewrite 回执的恢复路由：机械单点走零模型宿主路径，结构性才派返工 worker。

    实测教训：rewrite 判决只给 violations 不给恢复路径，宿主先手改 staging 再吃
    wrong_phase、最后派一个什么都不改的 draft worker 只为把相位拨回 await_assembly——
    三笔浪费都源于「下一步该跑什么」不在判决回执里。
    """
    codes = {str(issue.get("code") or "") for issue in issues}
    prose_rooted = sorted(
        str(i.get("code") or "") for i in issues if str(i.get("code") or "") not in _ASSEMBLY_ONLY_ISSUES
    )
    single_point = codes <= {
        "expected_delta_missing",
        "expected_delta_shape_mismatch",
        "unnamed_in_pack",
        # 外文残片是定点删除/替换：零模型 rework-patch 可表达，不值得派返工 worker。
        "foreign_fragment",
    }
    steps = [
        "prose one-point fix: `chapter rework-patch --project <P> --target <原句> --replacement <新句>`"
        " (zero model; anchored replacement on the preserved draft; chapter comes from HEAD)",
        "`chapter draft-submit` (stage gates re-run) → `chapter next` (back to await_assembly)",
        "prose UNCHANGED only: revise the assembly delta fields per each violation's hint, then"
        " `chapter submit` again. prose CHANGED above: the old assembly JSON is VOID (its"
        " plot_findings were judged against the pre-fix prose) — dispatch a fresh assemble stage"
        " worker to rebuild it; patching quotes in the old JSON and resubmitting re-raises the"
        " stale BLOCKERs forever (measured deadlock)",
    ]
    if single_point and not prose_rooted:
        route = "delta_only"
        note = (
            "violations are assembly-delta-shape only: restore/resubmit the preserved draft unchanged"
            " (rework-patch or direct draft-submit), then align the delta fields per hints —"
            " do NOT dispatch a rework worker for this"
        )
    elif single_point:
        route = "one_point_prose"
        note = (
            "single-point issues: use the zero-model host path below; dispatch a rework worker"
            " only if the fix cannot be expressed as one anchored replacement"
        )
    else:
        route = "redraft_worker"
        note = "structural: dispatch a fresh rework worker with review findings and evidence paths"
    return {"route": route, "note": note, "steps": steps}


def _plot_self_check_fail(
    store: BookStore,
    head: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """事实编辑在组装阶段的情节自检发现真问题：回草稿重写（正文问题，非组装契约）。

    与 `_gate_fail` 共用 `rewrite_limit` 与 `PHASE_AWAIT_DRAFT` 目标，但单独计数与留痕
    （quality 记 plot_self_check）。
    """
    cfg = store.load_config()
    limit = int(cfg.get("rewrite_limit") or 1)
    count = int(head.get("rewrite_count") or 0)
    chapter = int(head.get("chapter") or 0)
    if count < limit:
        head["rewrite_count"] = count + 1
        head["phase"] = PHASE_AWAIT_DRAFT
        preserved = _preserve_rework_text(store, chapter)
        for stale in (store.output_path, store.draft_text_path(chapter), store.polished_text_path(chapter)):
            if stale.exists():
                stale.unlink()
        head["blocked"] = None
        store.write_head(_touch(head))
        _log_quality(store, chapter, "plot_self_check", verdict="fix", finding_count=len(findings))
        _log_quality(store, chapter, "begin", reason="plot_self_check_rewrite")
        return ok(
            verdict="plot_fix",
            phase=PHASE_AWAIT_DRAFT,
            chapter=chapter,
            rewrite_count=head["rewrite_count"],
            preserved_draft_path=str(preserved) if preserved else None,
            findings=findings,
            recovery={
                "route": "plot_fix_then_reassemble",
                "steps": [
                    "fix the prose: one-point `chapter rework-patch --target <原句> --replacement <新句>`"
                    " (zero model) or a redraft worker per the findings",
                    "`chapter draft-submit` → `chapter next`",
                    "dispatch a FRESH assemble stage worker — the deleted assembly JSON stays deleted;"
                    " its plot_findings were judged against the pre-fix prose and restoring/patching it"
                    " re-raises the same BLOCKER forever (measured deadlock)",
                    "`chapter submit`",
                ],
            },
            hint=(
                "assembler's plot self-check found正文级问题：run chapter next to redraft content with the findings; "
                "同 pack，自动一次；不要开下一章；组装件须重跑（见 recovery.steps）"
            ),
        )
    blocked = {
        "chapter": chapter,
        "reason": "plot_self_check_failed",
        "findings": findings,
        "rewrite_count": count,
    }
    head["phase"] = PHASE_BLOCKED
    head["blocked"] = blocked
    store.write_head(_touch(head))
    _log_quality(store, chapter, "plot_self_check", verdict="fix", finding_count=len(findings), blocked=True)
    return ok(
        verdict="blocked",
        stop=True,
        phase=PHASE_BLOCKED,
        chapter=chapter,
        blocked=blocked,
        recovery={
            "route": "human_adjudicate_plot_fix",
            "steps": [
                "verify each finding against the CURRENT prose: beat-anchor findings may already be"
                " satisfied by a rework that happened after the assembler judged (they re-raise on"
                " every resubmit otherwise)",
                "if the prose needs the fix: `chapter next` → fix per findings → `chapter draft-submit`"
                " → `chapter next` → dispatch a FRESH assemble stage worker (never restore the old"
                " assembly JSON — its plot_findings are void once prose changed)",
                "`chapter submit`; if the same findings re-raise with prose_hash matching the current"
                " prose, the finding is fresh — adjudicate (change the beat anchor via plan"
                " patch-chapter, or `retry-authorize` to force-restart the chapter)",
            ],
        },
        hint="human must intervene; do not skip the chapter",
    )


__all__ = [
    '_style_metrics_gate_failed',
    '_polish_anchor_gate_failed',
    '_stale_plot_self_check',
    '_close_rechecked_findings',
    '_reassemble_if_stale',
    '_gate_fail',
    '_rewrite_recovery',
    '_plot_self_check_fail',
]
