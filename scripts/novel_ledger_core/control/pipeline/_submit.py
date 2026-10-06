# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：submit
from __future__ import annotations

from pathlib import Path
from typing import Any
from ...content.gates import (blocking_plot_findings, expected_delta_duplicate_warnings, collect_plot_findings, foreign_fragment_issues, format_defect_issues, plot_findings_issues, recheck_beat_anchor_blockers, validate_prose_anchors, validate_write_output, word_count_warnings)
from ...content.pack import (inputs_fingerprint)
from ...content.style_check import (check_style_hard, style_direction)
from ...infra.store import (PHASE_AWAIT_ASSEMBLY, PHASE_AWAIT_DRAFT, PHASE_AWAIT_POLISH, PHASE_BLOCKED, PHASE_SUBMITTED, BookStore)
from ...infra.util import (LedgerError, atomic_json, atomic_text, chinese_word_count, ok, read_json, stable_read_json, stable_read_text)

from ._common import (
    _ASSEMBLY_ONLY_ISSUES,
    _log_quality,
    _one_pass_expansion_hint,
    _polish_disabled,
    _require_active,
    _style_gate_enabled,
    _style_structure_args,
    _touch,
)
from ._quality import (
    _close_rechecked_findings,
    _gate_fail,
    _plot_self_check_fail,
    _polish_anchor_gate_failed,
    _reassemble_if_stale,
    _stale_plot_self_check,
    _style_metrics_gate_failed,
)

ACK_QUOTE_MIN = 6


QUOTE_DISTRIBUTION_MIN_CHARS = 800


def _word_gate_result(store: BookStore, text: str) -> dict[str, Any] | None:
    """字数带前置闸（与 submit 机检同源口径：只数汉字）。

    未启用 word_band_enforce 或达标返回 None；越带返回拒绝载荷。
    放在 draft-submit / polish-submit 收口执行，是因为字数带此前只在最终 submit 硬拦——
    那是整条链最贵的位置（submit 失败按正文问题回 draft 整链重写）。
    """
    cfg = store.load_config()
    if not bool(cfg.get("word_band_enforce", False)):
        return None
    band = dict(cfg.get("word_band") or {})
    lo = int(band.get("min") or 0)
    hi = int(band.get("max") or 0)
    chars = chinese_word_count(text)
    if lo <= chars <= hi:
        return None
    if chars < lo:
        return {
            "code": "word_count_low",
            "chars": chars,
            "word_band": band or None,
            "chars_to_min": lo - chars,
            "detail": (
                f"hanzi count {chars} is {lo - chars} below the {lo} floor — "
                + _one_pass_expansion_hint(lo - chars)
            ),
        }
    return {
        "code": "word_count_high",
        "chars": chars,
        "word_band": band or None,
        "chars_to_min": 0,
        "detail": (
            f"hanzi count {chars} exceeds the {hi} cap; tighten by cutting whole redundant "
            "beats/exchanges, not by compressing sentences (compression trips style gates); "
            "re-run the stage submit"
        ),
    }


def stage_draft_submit(store: BookStore) -> dict[str, Any]:
    """接受阶段一草稿：直接推进到 polish。"""
    head = store.read_head()
    _require_active(head)
    if head.get("phase") != PHASE_AWAIT_DRAFT:
        raise LedgerError(
            "wrong_phase",
            f"draft-submit only from {PHASE_AWAIT_DRAFT}; got {head.get('phase')}",
            {
                "phase": head.get("phase"),
                "hint": (
                    "from blocked: run `chapter next` FIRST — it moves HEAD to await_draft and"
                    " returns the draft action; draft-submit never runs from blocked"
                ),
            },
        )
    chapter = int(head["chapter"])
    path = store.draft_text_path(chapter)
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        # 保稿重提的零模型通道：rewrite/plot_fix 收口会把 staging 草稿轮转成 .revK
        # 并删除原件，文档化的「按 preserved_draft_path 保稿直接 draft-submit」曾被
        # missing_draft 挡死（实测宿主在这里空转多轮）。最新轮转稿在盘上，原样恢复
        # 后走正常闸门——机检一个不少，不算绕过。
        preserved = store.latest_stage_revision(chapter)
        if preserved is not None and Path(str(preserved)).exists():
            store.accept_stage_text(path, Path(str(preserved)).read_text(encoding="utf-8"))
            _log_quality(store, chapter, "draft_restored", source=str(preserved))
        else:
            raise LedgerError("missing_draft", f"content draft not found or empty: {path}", {"path": str(path)})
    draft_text = stable_read_text(path)
    # 字数带前置闸：短草稿在这里就地补齐（不耗任何配额），而不是养到 submit 才回整链。
    word_fail = _word_gate_result(store, draft_text)
    if word_fail is not None:
        _log_quality(
            store,
            chapter,
            "draft_words",
            verdict="fail",
            code=word_fail["code"],
            chars=word_fail["chars"],
        )
        return ok(
            verdict="draft_rejected",
            phase=PHASE_AWAIT_DRAFT,
            chapter=chapter,
            **word_fail,
            hint=(
                "the draft failed the word band at the CHEAPEST point of the chain; phase stays "
                "await_draft — expand/trim the same staging file in place (it is kept), then run "
                "`chapter draft-submit` again. " + word_fail["detail"]
            ),
        )
    # 外文残片前置闸：与字数带同 philosophy——在最便宜的 draft 点就地拦下（不耗任何
    # 配额），而不是养到 submit 才回整链。staging 文件保留，原位删除/改写后重提。
    if str(store.load_config().get("foreign_fragment_gate") or "block") != "allow":
        fragment_issues = foreign_fragment_issues(draft_text)
        if fragment_issues:
            _log_quality(store, chapter, "draft_foreign_fragment", verdict="fail", hits=fragment_issues[0].get("count"))
            return ok(
                verdict="draft_rejected",
                phase=PHASE_AWAIT_DRAFT,
                chapter=chapter,
                violations=fragment_issues,
                hint=(
                    "the draft contains foreign/garbled letter runs (deterministic gate); phase "
                    "stays await_draft — edit the same staging file in place (it is kept) to "
                    "delete or rewrite them as Chinese, then run `chapter draft-submit` again. "
                    "Books that legitimately need Latin text: config set foreign_fragment_gate=allow"
                ),
            )
    store.accept_stage_text(path, draft_text)
    # 成稿格式前置闸：与字数带、外文残片同 philosophy——在最便宜的 draft 点就地拦下。
    # 只写作模式（polish=off）下草稿即终稿，这道闸是格式残留唯一还能被拦住的地方，
    # 因此不能只挂在 assemble 的 submit 上。
    format_issues = format_defect_issues(
        draft_text,
        chapter=chapter,
        quote_style=str(store.load_config().get("quote_style") or "auto"),
    )
    if format_issues:
        _log_quality(store, chapter, "draft_format", verdict="fail", issues=format_issues[:5])
        return ok(
            verdict="draft_rejected",
            phase=PHASE_AWAIT_DRAFT,
            chapter=chapter,
            violations=format_issues,
            hint=(
                "the draft carries deterministic format/typography defects (duplicated chapter "
                "header, writing-stage markers, unpaired or mixed quote systems); phase stays "
                "await_draft — fix the same staging file in place (it is kept), then run "
                "`chapter draft-submit` again. Lock the book's quote system with "
                "`config set --key quote_style --value <cn_double|cn_corner|zh_book|ascii>`."
            ),
        )
    if _polish_disabled(store):
        # 只写作模式（config.polish=off）：草稿即终稿。字数带是章合同仍在 draft 收口；
        # 文风机检整体退出写链（要自查用 `chapter precheck`，只读不拦线），润色相位跳过，
        # 草稿落位 polished 路径后组装/入账/ack 链路零改动。
        atomic_text(store.polished_text_path(chapter), draft_text)
        store.accept_stage_text(store.polished_text_path(chapter), draft_text)
        head["phase"] = PHASE_AWAIT_ASSEMBLY
        head["blocked"] = None
        store.write_head(_touch(head))
        return ok(
            verdict="draft_accepted",
            phase=head["phase"],
            chapter=chapter,
            hint=(
                "polish disabled (draft-only mode): the draft is the final prose; "
                "run chapter next to get the assembly task"
            ),
        )
    head["phase"] = PHASE_AWAIT_POLISH
    head["blocked"] = None
    store.write_head(_touch(head))
    return ok(
        verdict="draft_accepted",
        phase=head["phase"],
        chapter=chapter,
        hint=(
            "run chapter next to get the polish task (beats/continuity gates run at submit)"
        ),
    )


def stage_polish_submit(store: BookStore) -> dict[str, Any]:
    """接受阶段二润色终稿：跑机器硬红线后进组装。"""
    head = store.read_head()
    _require_active(head)
    if head.get("phase") != PHASE_AWAIT_POLISH:
        raise LedgerError(
            "wrong_phase",
            f"polish-submit only from {PHASE_AWAIT_POLISH}; got {head.get('phase')}",
            {"phase": head.get("phase")},
        )
    chapter = int(head["chapter"])
    path = store.polished_text_path(chapter)
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        raise LedgerError("missing_polished", f"polished prose not found or empty: {path}", {"path": str(path)})
    head["blocked"] = None
    polished_text = stable_read_text(path)

    # 字数带前置闸（两档都跑）：终稿是入账文本的真源，短稿在这里回 polish 原地补，
    # 比等到 submit 按正文问题回 draft 整链重写便宜一整条链。
    word_fail = _word_gate_result(store, polished_text)
    if word_fail is not None:
        _log_quality(
            store,
            chapter,
            "polish_words",
            verdict="fail",
            code=word_fail["code"],
            chars=word_fail["chars"],
        )
        return ok(
            verdict="polish_rejected",
            phase=PHASE_AWAIT_POLISH,
            chapter=chapter,
            polished_output_path=str(path),
            polished_kept=True,
            **word_fail,
            hint=(
                "the polished draft failed the word band BEFORE assembly (the formal submit would send "
                "it all the way back to draft); the file is KEPT at polished_output_path — fix it in "
                "place, then run `chapter polish-submit` again. " + word_fail["detail"]
            ),
        )

    # 内容锚点收口（**两档都跑**）：beats/must 兑现、连续性防失忆、glossary 术语归一化。
    # 这三样此前只在 `chapter submit` 才被机检，而 submit 对正文类问题按约定回 `draft` 整链重写。
    # 润色终稿是入账前最后一个动正文的产物，在这里拦下就能只回 polish 重润——
    # 省掉「组装 → 机检失败 → 回 draft 重写 → 重润 → 重组装」的整条返工链。
    # 注意判据取 canonical pack（视图是它的派生切片，视图裁剪不影响本机检）。
    canon_pack = stable_read_json(store.current_pack_path)
    # 格式/体例闸同理：润色会在场景级重构里重排标点与引号，本相位就地拦、只回重润。
    format_issues = format_defect_issues(
        polished_text,
        chapter=chapter,
        quote_style=str(store.load_config().get("quote_style") or "auto"),
    )
    if format_issues:
        draft_path = store.draft_text_path(chapter)
        draft_text = draft_path.read_text(encoding="utf-8") if draft_path.exists() else ""
        draft_codes = {
            str(issue.get("code") or "")
            for issue in format_defect_issues(
                draft_text,
                chapter=chapter,
                quote_style=str(store.load_config().get("quote_style") or "auto"),
            )
        }
        polish_only = [
            issue for issue in format_issues if str(issue.get("code") or "") not in draft_codes
        ]
        if polish_only:
            _log_quality(store, chapter, "polish_format", verdict="fail", issues=polish_only[:5])
            return ok(
                verdict="polish_rejected",
                phase=PHASE_AWAIT_POLISH,
                chapter=chapter,
                polished_output_path=str(path),
                polished_kept=True,
                violations=polish_only,
                hint=(
                    "the polished prose introduced format/typography defects (mixed or unpaired "
                    "quote systems, duplicated chapter header, writing-stage markers); the file is "
                    "KEPT at polished_output_path — fix it in place, then run `chapter "
                    "polish-submit` again."
                ),
            )
    anchor_issues = validate_prose_anchors(polished_text, canon_pack)
    if anchor_issues:
        # 只拦**润色自己改坏的**锚点，按条归属、不越权替 draft 收口：
        # - 草稿本来就有这问题（少 must 词、草稿就写成了陌生人）→ 源头在草稿，
        #   这里不抢先处理，交给既有 `submit` 机检按原语义回 draft 整链重写；
        # - 草稿是干净的、只有终稿破了锚点 → 是文风编辑在场景级重构里改丢/引入的，
        #   回 polish 重润即可，比「组装→机检失败→回 draft」省一整条链。
        # 这样本闸门是**纯增量**：不改变既有分流结论，只多接住一类此前要等到 submit 才发现的问题。
        draft_path = store.draft_text_path(chapter)
        draft_text = draft_path.read_text(encoding="utf-8") if draft_path.exists() else ""
        draft_codes = {
            str(issue.get("code") or "")
            for issue in validate_prose_anchors(draft_text, canon_pack)
        }
        polish_only = [
            issue for issue in anchor_issues if str(issue.get("code") or "") not in draft_codes
        ]
        if polish_only:
            return _polish_anchor_gate_failed(store, head, chapter, polish_only)
    head["polish_anchor_pending"] = False
    anchor_path = store.polish_anchor_path(chapter)
    if anchor_path.exists():
        anchor_path.unlink()

    style_warnings: list[dict[str, Any]] = []
    metrics_path = store.style_metrics_path(chapter)
    if _style_gate_enabled(store):
        hard = check_style_hard(polished_text, **_style_structure_args(store))
        # 数值偏离与原书常见写法都只计作提示；按指标去重，避免方向画像重复计数。
        advisory_items = [*(hard.get("warnings") or []), *style_direction(polished_text)]
        style_warnings = list({
            str(item["metric"]): {
                "metric": item["metric"],
                "verdict": item.get("verdict", "high"),
            }
            for item in advisory_items
        }.values())
        if not hard["ok"]:
            return _style_metrics_gate_failed(store, head, chapter, hard, style_warnings)
    head["style_metrics_pending"] = False
    if metrics_path.exists():
        metrics_path.unlink()
    head["phase"] = PHASE_AWAIT_ASSEMBLY
    store.accept_stage_text(path, polished_text)
    store.write_head(_touch(head))
    _log_quality(
        store,
        chapter,
        "style_machine",
        verdict="pass",
        warnings_count=len(style_warnings),
    )
    return ok(
        verdict="polish_accepted",
        phase=PHASE_AWAIT_ASSEMBLY,
        chapter=chapter,
        # 节奏画像明细是 advisory、逐章形态雷同，不逐条回灌会话（省每章注入）；
        # 方向性结论看 style_warnings_count，累计漂移由指纹基线在批级闸门比对。
        style_warnings_count=len(style_warnings),
        hint=(
            "machine red-line hard gate passed (when enabled); "
            f"rhythm advisories: {len(style_warnings)} (directional only; fingerprint at batch gate); "
            "run chapter next to get the assembly task"
        ),
    )


def _quote_requirements(store: BookStore) -> dict[str, int]:
    """返回与 ack 机检完全同源的引用契约。"""
    minimum = int(store.load_config().get("ack_quotes_min") or 3)
    return {
        "minimum": minimum,
        "minimum_chars": ACK_QUOTE_MIN,
        "require_latter_half_when_chars_at_least": QUOTE_DISTRIBUTION_MIN_CHARS,
    }


def _quotes_all_in_first_half(
    quotes: list[str],
    prose: str,
    *,
    min_chars: int = QUOTE_DISTRIBUTION_MIN_CHARS,
) -> bool:
    """长稿引用分布闸：正文足够长时，至少一条引用必须来自后半段。

    只要求若干条短子串并不能证明“通读了全文”——全摘开头也能 pass。
    短文（如测试/极短章）不做分布要求，避免误伤。
    """
    if len(prose) < min_chars:
        return False
    half = len(prose) / 2
    return not any(q in prose and prose.rfind(q) >= half for q in quotes)


def _submission_review(
    store: BookStore,
    output: dict[str, Any],
    pack: dict[str, Any],
    polished: str,
    *,
    plot_self_check_enabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """运行提交前的只读校验；正式 submit 与 check-submit 共用同一判据。"""
    cfg = store.load_config()
    enforce_word_band = bool(cfg.get("word_band_enforce", False))
    issues = validate_write_output(
        output, pack, enforce_word_band=enforce_word_band, prose=polished
    )
    # 外文残片是确定性缺陷，机检直拦；prompt 自查项只是概率兜底（实测 condensedcondensed
    # 混进正文只能靠 BLOCKER 偶然暴露）。确需拉丁文的书 config set foreign_fragment_gate=allow。
    if str(cfg.get("foreign_fragment_gate") or "block") != "allow":
        issues.extend(foreign_fragment_issues(polished))
    # 成稿格式与体例：章头重复/错号、写作期残留标记、引号体例混用或未闭合。
    # 同样是确定性缺陷，进正文返工路（不在 _ASSEMBLY_ONLY_ISSUES 里，故判 fix_draft）。
    issues.extend(
        format_defect_issues(
            polished,
            chapter=int(store.read_head().get("chapter") or 0),
            quote_style=str(cfg.get("quote_style") or "auto"),
        )
    )
    issues.extend(plot_findings_issues(output, polished, required=int(cfg.get("review_contract_version", 2)) >= 2))
    plot_issues = collect_plot_findings(output) if plot_self_check_enabled else []
    warnings = word_count_warnings(output, pack, prose=polished)
    warnings.extend(_dateline_warnings(store, polished, cfg))
    # 同章重复 knowledge 声明是计划层缺陷：提交端已去重放行（死锁解锁），
    # 告警跟随回执，指路 plan patch-chapter 在计划层合并。
    warnings.extend(expected_delta_duplicate_warnings(pack))
    if enforce_word_band:
        issue_codes = {i.get("code") for i in issues}
        warnings = [w for w in warnings if w.get("code") not in issue_codes]
    return issues, plot_issues, warnings


def _dateline_warnings(
    store: BookStore, polished: str, cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """日志体开场探针（advisory，不阻断）：单章 NIT，连续三章起升级 streak。

    探因：简报/正典灌满绝对日期 + 机检盯时间线，写者的安全牌收敛成每章
    「X月X号早上X点」开场；单章合法（正文确有对表需求），连续才是读感问题。
    日记体/报告体书用 config dateline_openings=allow 豁免。
    """
    if str(cfg.get("dateline_openings") or "discourage") == "allow":
        return []
    from ...content.style_check import dateline_opening

    hit = dateline_opening(polished)
    if hit is None:
        return []
    chapter = int(store.read_head().get("chapter") or 0)
    streak = 1
    for back in range(1, 4):
        path = store.chapter_md_path(chapter - back)
        if not path.exists() or dateline_opening(path.read_text(encoding="utf-8")) is None:
            break
        streak += 1
    if streak >= 3:
        return [
            {
                "code": "dateline_opening_streak",
                "streak": streak,
                "opening": hit["opening"],
                "hint": (
                    f"本章已连续第 {streak} 章以日志式日期/时刻句开场——读感从小说滑向工作日志。"
                    "按写作简报的时间呈现纪律改开场：承接靠场景与动作，日期留在场景中段自然带出。"
                ),
            }
        ]
    return [
        {
            "code": "dateline_opening",
            "opening": hit["opening"],
            "hint": (
                "本章以日志式日期/时刻句开场。单章合法（确有对表需求时可用），"
                "但不要连续多章同款开场（streak 探针会升级点名）。"
            ),
        }
    ]


def precheck_prose(store: BookStore, path: Path, *, chapter: int | None = None) -> dict[str, Any]:
    """可选的正文诊断：只读，不改变阶段或消耗返工配额。"""
    if not path.exists():
        raise LedgerError("missing_prose", f"file not found: {path}", {"path": str(path)})
    text = path.read_text(encoding="utf-8")
    band = dict(store.load_config().get("word_band") or {})
    band_min = int(band.get("min") or 0)
    band_max = int(band.get("max") or 0)
    chars = chinese_word_count(text)
    word_band_ok = band_min <= chars <= band_max
    style_enabled = _style_gate_enabled(store)
    hard = check_style_hard(text, **_style_structure_args(store)) if style_enabled else None
    style_ok = bool(hard["ok"]) if hard is not None else True
    passed = word_band_ok and style_ok
    problems: list[str] = []
    if chars < band_min:
        problems.append(f"hanzi count {chars} < word_band.min {band_min} (need {band_min - chars} more)")
    if chars > band_max:
        problems.append(f"hanzi count {chars} > word_band.max {band_max}")
    if hard is not None and not style_ok:
        problems.extend(f"style red line: {item}" for item in (hard.get("fails") or []))
    hint = (
        "local diagnostic passed; proceed to the stage submit"
        if passed
        else "fix the listed problems in place; the stage submit also runs its own checks"
    )
    if chars < band_min:
        # 字数不足是唯一给出换算的项：它就是历史上 5-6 轮返工的那个缺口。
        hint = f"short by {band_min - chars} hanzi — {_one_pass_expansion_hint(band_min - chars)}"
    # 日志体开场探针：advisory，不进 problems（problems 非空会被当成 fail 信号），
    # 独立字段暴露；写者改不改由它按简报纪律判断。
    dateline = None
    if str(store.load_config().get("dateline_openings") or "discourage") != "allow":
        from ...content.style_check import dateline_opening

        dateline = dateline_opening(text)
    return ok(
        action="precheck",
        verdict="pass" if passed else "fail",
        chapter=chapter,
        path=str(path),
        chars=chars,
        word_band=band or None,
        word_band_ok=word_band_ok,
        style_gate_enabled=style_enabled,
        style_ok=style_ok,
        style_fails=(hard or {}).get("fails") or [],
        dateline_opening=dateline,
        problems=problems,
        hint=hint,
    )


def check_submit_output(store: BookStore, output: dict[str, Any]) -> dict[str, Any]:
    """可选的只读提交诊断；正式 submit 独立运行同一校验。"""
    head = store.read_head()
    phase = head.get("phase")
    if not isinstance(output, dict):
        raise LedgerError("invalid_output", "output must be a JSON object")
    # blocked 相位同样放行：返工耗尽停线后，只读诊断是定位提交稿问题的第一入口
    # （诊断不改变阶段、不消耗任何预算）；其余相位仍报 not_ready。
    if phase not in (PHASE_AWAIT_ASSEMBLY, PHASE_BLOCKED):
        return ok(
            action="submit_check",
            verdict="not_ready",
            ready=False,
            phase=phase,
            violations=[{"code": "wrong_phase", "phase": phase}],
            hint=f"submit check only in {PHASE_AWAIT_ASSEMBLY} (or blocked, for read-only diagnosis); got {phase}",
        )
    if not store.current_pack_path.exists():
        raise LedgerError("missing_pack", "no current pack; run chapter next first")
    pack = read_json(store.current_pack_path)
    chapter = int(head.get("chapter") or 0)
    if str(pack.get("inputs_fingerprint") or "") != inputs_fingerprint(store, chapter):
        return ok(
            action="submit_check",
            verdict="stale_pack",
            ready=False,
            phase=phase,
            chapter=chapter,
            pack_hash=pack.get("pack_hash"),
            violations=[{"code": "stale_pack"}],
            hint="plan/kb/voice changed after pack assembly; run chapter next to rebuild before checking",
        )
    polished_path = store.polished_text_path(chapter)
    if not polished_path.exists():
        raise LedgerError(
            "missing_polished",
            f"submit check requires polished prose first: {polished_path}",
            {"path": str(polished_path)},
        )
    polished = polished_path.read_text(encoding="utf-8").rstrip("\n")
    refused = _stale_plot_self_check(store, head, pack, output, polished)
    if refused is not None:
        refused["action"] = "submit_check"
        return refused
    issues, plot_issues, warnings = _submission_review(
        store,
        output,
        pack,
        polished,
        plot_self_check_enabled=True,
    )
    codes = {str(issue.get("code") or "") for issue in issues}
    blockers = blocking_plot_findings(plot_issues)
    non_blocking_plot = [item for item in plot_issues if item not in blockers]
    if issues:
        verdict = "fix_assembly" if codes and codes <= _ASSEMBLY_ONLY_ISSUES else "fix_draft"
    elif blockers:
        verdict = "fix_draft"
    else:
        verdict = "ready"
    return ok(
        action="submit_check",
        verdict=verdict,
        ready=verdict == "ready",
        phase=phase,
        chapter=chapter,
        pack_hash=pack.get("pack_hash"),
        violations=issues,
        plot_blockers=blockers,
        plot_warnings=non_blocking_plot,
        warnings=warnings,
        hint=(
            "read-only diagnostic passed; run chapter submit"
            if verdict == "ready"
            else "fix the reported assembly fields; chapter submit will check them again"
            if verdict == "fix_assembly"
            else "reported issue needs draft/content handling"
        ),
    )


def submit_output(store: BookStore, output: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    head = store.read_head()
    _require_active(head)
    phase = head.get("phase")
    if phase != PHASE_AWAIT_ASSEMBLY:
        raise LedgerError(
            "wrong_phase",
            f"submit not allowed in phase {phase}",
            {
                "phase": phase,
                "hint": (
                    "a previous rewrite verdict moved HEAD back to await_draft — re-submitting the"
                    " assembly now is refused; recovery IN THIS ORDER: `chapter next` FIRST (blocked →"
                    " await_draft, returns the draft action — draft-submit itself refuses from blocked),"
                    " then `chapter draft-submit` the preserved draft, `chapter next` back to"
                    " await_assembly, re-run the assemble stage worker (the old assembly JSON's"
                    " plot_findings are void once prose changed), then submit"
                ),
            },
        )
    if not store.current_pack_path.exists():
        raise LedgerError("missing_pack", "no current pack; run chapter next first")
    pack = read_json(store.current_pack_path)
    if not isinstance(output, dict):
        raise LedgerError("invalid_output", "output must be a JSON object")
    polished_path = store.polished_text_path(int(head["chapter"]))
    if not polished_path.exists():
        raise LedgerError(
            "missing_polished",
            f"submit requires polished prose first (stage order draft→polish→assemble): {polished_path}",
            {"path": str(polished_path)},
        )
    stale = _reassemble_if_stale(store, head, pack)
    if stale is not None:
        return stale
    # stale 检测在「视图未变、仅指纹变」时会静默换新 canonical pack（返回 None），
    # 这里必须重读，后续机检/hash 比对才是新包
    pack = read_json(store.current_pack_path)
    # 正文真源＝阶段二终稿。事实编辑只需产出账本字段，正文由脚本读盘注入 canonical output：
    # 省一次全章回显，且不接受事实编辑提供另一份正文真源。
    polished = polished_path.read_text(encoding="utf-8").rstrip("\n")
    refused = _stale_plot_self_check(store, head, pack, output, polished)
    if refused is not None:
        return refused
    issues, plot_issues, warnings = _submission_review(
        store,
        output,
        pack,
        polished,
        plot_self_check_enabled=True,
    )
    blockers = blocking_plot_findings(plot_issues)
    # 过期发现确定性复检：plot_findings 是组装 worker 对「它当时读到的正文」的一次性
    # 判断，prose 返工后旧 BLOCKER 会阴魂不散（实测锚词死锁：补词后闸门依旧报
    # 「全篇未出现该词」，回执 quote 里就带着该词）。beat 锚词类发现可确定性
    # 复核——现行正文已兑现的不再阻断，留痕放行；非机械发现仍走原路由。
    blockers, rechecked = recheck_beat_anchor_blockers(pack, polished, blockers)
    rechecked_set = {id(f) for f in rechecked}
    if rechecked:
        _log_quality(
            store,
            int(head["chapter"]),
            "plot_self_check_recheck",
            verdict="cleared",
            finding_count=len(rechecked),
            findings=rechecked,
        )
        warnings.append(
            {
                "code": "stale_plot_finding_cleared",
                "cleared": len(rechecked),
                "hint": (
                    "组装件携带的 beat 锚词类 BLOCKER 已对现行正文确定性复检通过（prose 返工后"
                    "的过期发现），本次不再阻断；完整判断见 quality 日志 plot_self_check_recheck"
                ),
            }
        )
    non_blocking_plot = [item for item in plot_issues if item not in blockers]
    if not issues:
        from ...content.reviews import record_findings
        # 复检已兑现的过期发现不再入账本 findings（否则会以 open 态喂给下一轮
        # 组装 worker，重新毒化其 plot_findings）。
        record_findings(store, int(head["chapter"]), polished, [f for f in plot_issues if id(f) not in rechecked_set])
        _close_rechecked_findings(store, int(head["chapter"]), pack, polished)
    if blockers and not issues:
        return _plot_self_check_fail(store, head, blockers)
    if not issues:
        # 唯有通过的组装件才成为 canonical output。
        output = {**output, "prose": polished}
        atomic_json(store.output_path, output)
        head["phase"] = PHASE_SUBMITTED
        head["blocked"] = None
        store.write_head(_touch(head))
        if non_blocking_plot:
            _log_quality(
                store,
                int(head["chapter"]),
                "plot_self_check",
                verdict="warn",
                finding_count=len(non_blocking_plot),
                findings=non_blocking_plot,
            )
            warnings.extend({**item, "source": "plot_self_check"} for item in non_blocking_plot)
        _log_quality(
            store,
            int(head["chapter"]),
            "submit",
            verdict="accepted",
            warnings_count=len(warnings),
        )
        return ok(
            verdict="accepted",
            phase=PHASE_SUBMITTED,
            chapter=head["chapter"],
            pack_hash=pack["pack_hash"],
            words=chinese_word_count(str(output.get("prose") or "")),
            warnings=warnings,
            hint="run chapter next to commit, then ack-read",
        )
    # 组装契约错误就地修正；正文问题仍按章节级返工处理。
    return _gate_fail(store, head, issues, warnings)


__all__ = [
    'ACK_QUOTE_MIN',
    'QUOTE_DISTRIBUTION_MIN_CHARS',
    '_word_gate_result',
    'stage_draft_submit',
    'stage_polish_submit',
    '_quote_requirements',
    '_quotes_all_in_first_half',
    '_submission_review',
    '_dateline_warnings',
    'precheck_prose',
    'check_submit_output',
    'submit_output',
]
