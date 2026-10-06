# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：audit
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from ...content.reviews import (review_summary)
from ...content.consistency import (run_consistency_audit)
from ...ledger.ledger import (coerce_due, load_snapshot, read_events)
from ...infra.store import (PHASE_IDLE, BookStore)
from ...infra.util import (LedgerError, atomic_json, chinese_word_count, ok, read_json, sha256_text)

from ._common import (
    _corpus_style_fingerprint,
    _creative_advisory,
    _unresolved_long_term_commitments,
)
from ._planning import (
    _outline_conformance,
    _quant_coverage_warnings,
)
from ._usage import (
    _USAGE_DETAIL_WINDOW,
    _quality_summary,
    _usage_summary,
)

def _content_numeric_audit(
    store: BookStore, chapters: list[Path], cfg: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只看数字的跨章巡检（advisory，不挡 next）：
    - enum_sum_mismatch：句内「A、B、C……拢共 D」加了不等于 D（纯算术，可定罪）；
    - same_key_conflict：项目 config.quant_keys 声明的量化键，同章出现两个互斥数值；
    - derived_numeric_drift：meta/summaries 的「数字+单位」在正文里缺席（旧值残留）。
    口径词全部来自项目（glossary / quant_keys / 正典），本函数不内置题材判据。
    """
    from ...content.numeric_audit import (
        algebraic_flow_mismatches,
        derived_numeric_drift,
        enum_sum_mismatches,
        same_key_conflicts,
    )

    quant_keys = [str(k) for k in (cfg.get("quant_keys") or []) if str(k).strip()]
    numeric_issues: list[dict[str, Any]] = []
    derived_drift: list[dict[str, Any]] = []
    for cf in chapters:
        ch_num = int(cf.stem.split("-")[1])
        prose = cf.read_text(encoding="utf-8")
        for hit in enum_sum_mismatches(prose):
            numeric_issues.append({"chapter": ch_num, **hit})
        for hit in algebraic_flow_mismatches(prose):
            numeric_issues.append({"chapter": ch_num, **hit})
        for hit in same_key_conflicts(prose, quant_keys):
            numeric_issues.append({"chapter": ch_num, **hit})
        derived: dict[str, str] = {}
        meta_path = store.meta_path(ch_num)
        if meta_path.exists():
            try:
                derived["meta.l1_summary"] = str(read_json(meta_path).get("l1_summary") or "")
            except Exception:
                pass
        summary_path = store.summary_path(ch_num)
        if summary_path.exists():
            try:
                derived["summaries.l1"] = str(read_json(summary_path).get("l1_summary") or "")
            except Exception:
                pass
        for hit in derived_numeric_drift(prose, derived):
            derived_drift.append({"chapter": ch_num, **hit})
    return numeric_issues, derived_drift


def reconcile_book(store: BookStore) -> dict[str, Any]:
    """改口径 / 改尺 / 批量重写后的**一次收口对账**（只读，不挡 next）。

    把散在各闸里的"值漂移"信号聚成一张待办表，给总编辑按图索骥：
    - ledger_quotes：debts/hooks/relations/facts/deaths 引文断锚（正文改了账本没跟）；
    - numeric：句内枚举求和不等（可定的算术错）、项目 quant_keys 同键双值；
    - derived：meta/summaries 里的银价数词在正文缺席（旧值残留在派生件）；
    - canon：正典在某章 commit 后被改（canon_drift）；
    - glossary：禁用旧写法仍在正文中；
    - fact：取值域声明（`fact_keys`）命中、跨章同键双值、章节格式与体例、跨章近重复段落。

    只报事实与位置，不判定返工范围；裁决权在总编辑。全部类别为空即 `clean=true`。
    """
    from ...content.numeric_audit import (
        algebraic_flow_mismatches,
        derived_numeric_drift,
        enum_sum_mismatches,
        same_key_conflicts,
    )
    from ...ledger.ledger import verify_ledger

    cfg = store.load_config()
    quant_keys = [str(k) for k in (cfg.get("quant_keys") or []) if str(k).strip()]
    glossary = cfg.get("glossary") or {}
    chapters = sorted(store.chapters_dir.glob("*/ch-*.md"))

    ledger_res = verify_ledger(store)
    quote_orphans = ledger_res.get("quote_invalid") or []

    numeric: list[dict[str, Any]] = []
    derived: list[dict[str, Any]] = []
    glossary_issues: list[dict[str, Any]] = []
    for cf in chapters:
        ch_num = int(cf.stem.split("-")[1])
        prose = cf.read_text(encoding="utf-8")
        for hit in enum_sum_mismatches(prose):
            numeric.append({"chapter": ch_num, **hit})
        for hit in algebraic_flow_mismatches(prose):
            numeric.append({"chapter": ch_num, **hit})
        for hit in same_key_conflicts(prose, quant_keys):
            numeric.append({"chapter": ch_num, **hit})
        derived_sources: dict[str, str] = {}
        meta_path = store.meta_path(ch_num)
        if meta_path.exists():
            try:
                derived_sources["meta.l1_summary"] = str(read_json(meta_path).get("l1_summary") or "")
            except Exception:
                pass
        summary_path = store.summary_path(ch_num)
        if summary_path.exists():
            try:
                derived_sources["summaries.l1"] = str(read_json(summary_path).get("l1_summary") or "")
            except Exception:
                pass
        for hit in derived_numeric_drift(prose, derived_sources):
            derived.append({"chapter": ch_num, **hit})
        hits = [
            f"{wrong}->{pref}"
            for wrong, pref in glossary.items()
            if wrong and pref and wrong != pref and wrong in prose
        ]
        if hits:
            glossary_issues.append({"chapter": ch_num, "violations": hits})

    canon = _canon_drift(store)
    # 场景地图兜底：注册表申报与提交闸是主防线；这里对全部已提交正文做注册表
    # 无关的漂移扫描（同一地点互斥楼层/门牌），旧书没建注册表也能点名。
    from ...content.numeric_audit import spatial_attribute_drift
    from ...ledger.ledger import load_snapshot as _load_snap_for_locations

    spatial_keys = list(dict.fromkeys(
        [str(k) for k in (cfg.get("spatial_keys") or []) if str(k).strip()]
        + [
            name
            for loc in (_load_snap_for_locations(store).get("locations") or [])
            if isinstance(loc, dict)
            for name in [str(loc.get("name") or ""), str(loc.get("id") or ""), *(str(a) for a in (loc.get("aliases") or []))]
            if name.strip()
        ]
    ))
    spatial = spatial_attribute_drift(
        [(int(cf.stem.split("-")[1]), cf.read_text(encoding="utf-8")) for cf in chapters],
        spatial_keys,
    )
    # 事实取值域与跨章形态：取值全部来自项目声明，未声明时恒空（默认零误报）。
    consistency = run_consistency_audit(store)
    fact_issues = list(consistency["hits"]) + list(consistency["cross_chapter"])
    categories = {
        "ledger_quotes": quote_orphans,
        "numeric": numeric,
        "derived": derived,
        "canon": [canon] if canon.get("changed") else [],
        "glossary": glossary_issues,
        "spatial": spatial,
        "fact": fact_issues,
        "chapter_format": consistency["chapter_format"],
        "near_duplicates": consistency["near_duplicates"],
    }
    counts = {name: len(items) for name, items in categories.items()}
    clean = all(count == 0 for count in counts.values())
    # 逐章聚合的待办索引，便于一次性回改
    by_chapter: dict[str, list[str]] = {}
    for name, items in categories.items():
        for item in items:
            ch = item.get("chapter")
            key = f"ch{int(ch)}" if isinstance(ch, int) else name
            by_chapter.setdefault(key, []).append(name)
    return ok(
        action="book_reconcile",
        clean=clean,
        counts=counts,
        ledger_quotes=quote_orphans,
        numeric=numeric,
        derived=derived,
        canon=canon,
        glossary=glossary_issues,
        spatial=spatial,
        fact=fact_issues,
        chapter_format=consistency["chapter_format"],
        near_duplicates=consistency["near_duplicates"],
        repeated_phrases=consistency["repeated_phrases"],
        weak_declarations=consistency["weak_declarations"],
        chapters_to_fix=sorted(by_chapter, key=lambda k: (not k.startswith("ch"), k)),
        quant_keys=quant_keys,
        hint=(
            "全部为空：正文、账本、派生件与正典一致。"
            "否则按 chapters_to_fix 逐章回改（先账本后正文），改完重跑 book reconcile 至 clean=true。"
            "spatial 是场景地图兜底扫描：同一地点出现互斥楼层/门牌；"
            "正文为准则用 chapter patch 修旧章并在组装申报 replaces，注册表为准则修当章正文。"
            "fact 是取值域声明（config.fact_keys）命中与跨章同键双值；"
            "chapter_format 是章头/残留标记/引号体例；near_duplicates 是跨章近重复叙述（改一处或留裁决）。"
        ),
    )


def book_facts(store: BookStore) -> dict[str, Any]:
    """书级事实对账（只读）：取值域命中、跨章同键双值、章节格式、近重复、高频片段。

    与 `book audit` / `book reconcile` 共用同一内核（`content/consistency.py`），
    但把结果单独摆出来，供总编辑在批级/卷级/终局收口时按 key 逐条裁决。
    单章阶段禁用（与 `book reconcile` 同规）：章内写者不需要整书视野，也不该为它付扫描成本。
    """
    result = run_consistency_audit(store)
    hint = (
        "fact_keys 为空时全部事实探针恒空——先按 references/fact-registry.md 声明取值域。"
        "每条 hit 的 key/canonical/observed/chapter/excerpt 可直接定位到原文；"
        "定权威值后用 chapter patch 定点替换，改完重跑本命令至 hits 为空。"
    )
    if not result["fact_keys"]:
        hint = "config.fact_keys 是空的：事实一致性闸门全部空转。先跑 book calibrate 取候选。" + hint
    elif result["weak_declarations"]:
        hint = "有条目缺少 canonical 或观测模式（observe/key/suffixes），已在 weak_declarations 点名。" + hint
    return ok(action="book_facts", hint=hint, **result)


def calibrate_book(store: BookStore) -> dict[str, Any]:
    """口径标定助手（只读）：从正典派生候选 `quant_keys` 与 `glossary` 摆给总编辑。

    开书时这两个键都是空白的，靠人记得填；本命令把候选清单摆出来
    （量化口径卡的章节标题 + 加粗口径词；硬禁则里被点名成具体词串的条目），
    总编辑据此手工把「每章应当单一取值」的键写进 `config.quant_keys`、
    把够限定的禁用串写进 `config.glossary`。不写任何文件——
    取舍是编辑判断，自动写入会把不合适的键（一章两笔的「本利」）或会误伤真值的短词放进去。
    """
    from ...content.extract import propose_glossary_candidates
    from ...content.numeric_audit import propose_fact_keys, propose_quant_keys

    cards = store.load_kb()
    proposal = propose_quant_keys(cards)
    glossary = propose_glossary_candidates(cards)
    cfg = store.load_config()
    facts = propose_fact_keys(cards)
    return ok(
        action="book_calibrate",
        current_quant_keys=[str(k) for k in (cfg.get("quant_keys") or [])],
        candidates=proposal["candidates"],
        bold_terms=proposal["bold_terms"],
        hint=proposal["hint"],
        current_glossary=sorted(str(k) for k in (cfg.get("glossary") or {})),
        glossary_candidates=glossary["candidates"],
        glossary_ban_bullets=glossary["ban_bullets"],
        glossary_hint=glossary["hint"],
        current_fact_keys=sorted(str(k) for k in (cfg.get("fact_keys") or {})),
        fact_keys_candidates=facts["candidates"],
        fact_keys_hint=facts["hint"],
    )


def _derived_name_drift(
    store: BookStore, chapters: list[Path], snap: dict[str, Any]
) -> list[dict[str, Any]]:
    """跨章叙事记忆回检（advisory）：摘要点名的账本实体必须在该章正文里出现过。

    `l1_summary` 是组装阶段由模型改写、又是**唯一会喂给后续章节**的"前情"叙述
    （`near_summaries`）。数字漂移已有 `derived_numeric_drift` 兜底，事实与人物没有：
    摘要里凭空多出一个没在本章出场的人，会先进下一章写者的上下文，写者一照写就变成真 canon。
    这里只做零误报的一条：**账本已知实体名出现在摘要、却不在本章正文** → 报给总编辑确认。
    """
    names = [
        str(n)
        for n in (snap.get("entities") or {})
        if str(n).strip() and len(str(n).strip()) >= 2
    ]
    if not names:
        return []
    out: list[dict[str, Any]] = []
    for cf in chapters:
        ch_num = int(cf.stem.split("-")[1])
        summary = ""
        summary_path = store.summary_path(ch_num)
        meta_path = store.chapter_meta_path(ch_num)
        if summary_path.exists():
            summary = str(read_json(summary_path).get("l1_summary") or "")
        elif meta_path.exists():
            summary = str(read_json(meta_path).get("l1_summary") or "")
        if not summary:
            continue
        prose = cf.read_text(encoding="utf-8")
        missing = sorted(n for n in names if n in summary and n not in prose)
        if missing:
            out.append(
                {
                    "chapter": ch_num,
                    "code": "derived_name_drift",
                    "names": missing[:8],
                    "hint": "摘要里点名的角色在本章正文中没出现：确认是摘要凭空补写"
                    "（改摘要或重跑组装），还是正文漏写了这个人物（回改正文与账本）。",
                }
            )
    return out


def audit_book(store: BookStore) -> dict[str, Any]:
    """全书整体一致性审查：字数分布、引号配对、术语归一化（glossary）、哈希基线、失效 quotes、
    章首尾复读、伏笔债务及账本一致性。题材无关，不内置任何具体设定判据。"""
    from ...ledger.ledger import load_snapshot, verify_ledger

    chapters = sorted(store.chapters_dir.glob("*/ch-*.md"))
    cfg = store.load_config()
    glossary = cfg.get("glossary") or {}

    chapter_reports: list[dict[str, Any]] = []
    total_words = 0
    words_list: list[int] = []
    quote_issues: list[dict[str, Any]] = []
    glossary_issues: list[dict[str, Any]] = []
    hash_mismatch_chapters: list[int] = []
    quote_invalid_chapters: list[dict[str, Any]] = []

    for cf in chapters:
        ch_num = int(cf.stem.split("-")[1])
        prose = cf.read_text(encoding="utf-8")
        words = chinese_word_count(prose)
        total_words += words
        words_list.append(words)

        open_sq = prose.count("“")
        close_sq = prose.count("”")
        ascii_q = prose.count("\"")
        # 四套体例（“”/「」/『』/直引号）逐一查配对：只查弯引号会漏掉「」与『』，
        # 而多稿缝合的典型形态正是不同稿各用一套、谁也不闭合。
        unpaired_styles = [
            name
            for name, (left, right) in (
                ("cn_double", ("“", "”")),
                ("cn_corner", ("「", "」")),
                ("zh_book", ("『", "』")),
            )
            if prose.count(left) != prose.count(right)
        ]
        if open_sq != close_sq or ascii_q % 2 != 0 or unpaired_styles:
            quote_issues.append({
                "chapter": ch_num,
                "open_smart": open_sq,
                "close_smart": close_sq,
                "ascii_quotes": ascii_q,
                "unpaired_styles": unpaired_styles,
            })

        # 术语表检查（仅当项目配置了 glossary 才生效）
        ch_glossary_hits = []
        for wrong_term, pref in glossary.items():
            if wrong_term and pref and wrong_term != pref and wrong_term in prose:
                ch_glossary_hits.append(f"{wrong_term} -> {pref} ({prose.count(wrong_term)}处)")
        if ch_glossary_hits:
            glossary_issues.append({
                "chapter": ch_num,
                "violations": ch_glossary_hits,
            })

        # 哈希与 ack quotes 验证
        prose_clean = prose[:-1] if prose.endswith("\n") else prose
        h1 = "sha256:" + sha256_text(prose_clean)
        h2 = "sha256:" + sha256_text(prose)
        ack_file = store.ack_path(ch_num)
        if ack_file.exists():
            ack_data = read_json(ack_file)
            ack_h = ack_data.get("prose_hash")
            if ack_h not in (h1, h2):
                hash_mismatch_chapters.append(ch_num)
            for q in ack_data.get("quotes") or []:
                if q not in prose:
                    quote_invalid_chapters.append({"chapter": ch_num, "quote": q})

        chapter_reports.append({
            "chapter": ch_num,
            "words": words,
        })

    seam_issues: list[dict[str, Any]] = []
    for i in range(len(chapters) - 1):
        ch1_num = int(chapters[i].stem.split("-")[1])
        ch2_num = int(chapters[i + 1].stem.split("-")[1])
        p1 = chapters[i].read_text(encoding="utf-8")
        p2 = chapters[i + 1].read_text(encoding="utf-8")

        lines1 = [l.strip() for l in p1.split("\n") if l.strip() and not l.startswith("#")]
        lines2 = [l.strip() for l in p2.split("\n") if l.strip() and not l.startswith("#")]

        if lines1 and lines2:
            tail = lines1[-1]
            head = lines2[0]
            if len(tail) >= 12 and (tail in head or head in tail):
                seam_issues.append({
                    "from_chapter": ch1_num,
                    "to_chapter": ch2_num,
                    "type": "duplicate_sentence",
                    "tail": tail[:50],
                    "head": head[:50],
                })

    ledger_res = verify_ledger(store)
    snap = load_snapshot(store)
    plan = store.load_plan()
    numeric_issues, derived_drift = _content_numeric_audit(store, chapters, cfg)
    consistency = run_consistency_audit(store)
    fact_issues = list(consistency["hits"]) + list(consistency["cross_chapter"])
    fact_keys = consistency["fact_keys"]
    fact_declarations_missing = (
        {
            "code": "fact_declarations_missing",
            "advisory": (
                "`config.fact_keys` 是空的：书级事实一致性闸门（同一事实两套取值、"
                "跨章同键双值）**全部空转**。先跑 `book calibrate` 取候选，按 "
                "references/fact-registry.md 声明取值域。"
            ),
        }
        if not fact_keys
        else None
    )
    derived_name_drift = _derived_name_drift(store, chapters, snap)
    active_hooks = [
        h for h in (snap.get("hooks") or [])
        if isinstance(h, dict) and str(h.get("status") or "open") not in ("paid", "closed", "abandoned")
    ]
    cur_ch = int(snap.get("chapter") or 0)
    overdue_hooks = [h for h in active_hooks if (coerce_due(h.get("due")) or 999999) < cur_ch]

    book_words_target = int(cfg.get("book_words") or 0)
    stats = {
        "total_chapters": len(chapters),
        "total_words": total_words,
        "avg_words": round(total_words / len(chapters), 1) if chapters else 0,
        "min_words": min(words_list) if words_list else 0,
        "max_words": max(words_list) if words_list else 0,
        "book_words": book_words_target,
        "book_words_remaining": max(book_words_target - total_words, 0),
        "book_words_progress": (
            round(total_words / book_words_target, 4) if book_words_target > 0 else 0.0
        ),
    }
    style_fingerprint = _corpus_style_fingerprint(
        store,
        min_chapters=int(cfg.get("style_fingerprint_min") or 5),
    )

    return ok(
        action="book_audit",
        editorial_review=review_summary(store),
        patch_review_pending=[int(p.stem.split("-")[-1]) for p in store.acks_dir.glob("**/ch-*.json") if read_json(p).get("review_status") == "needs_review"],
        stats=stats,
        style_fingerprint=style_fingerprint,
        quote_issues=quote_issues,
        glossary_issues=glossary_issues,
        hash_mismatch_chapters=hash_mismatch_chapters,
        quote_invalid_count=len(quote_invalid_chapters),
        seam_issues=seam_issues,
        ledger_consistent=ledger_res.get("consistent", False),
        ledger_diffs=ledger_res.get("diffs", []),
        ledger_quote_invalid=ledger_res.get("quote_invalid") or [],
        ledger_quote_invalid_count=len(ledger_res.get("quote_invalid") or []),
        ledger_quote_consistent=ledger_res.get("quote_consistent", True),
        ledger_hygiene_issues=ledger_res.get("hygiene_issues") or [],
        numeric_issues=numeric_issues,
        numeric_issue_count=len(numeric_issues),
        fact_issues=fact_issues,
        fact_issue_count=len(fact_issues),
        fact_keys_declared=sorted(fact_keys),
        fact_declarations_missing=fact_declarations_missing,
        chapter_format=consistency["chapter_format"],
        chapter_format_count=len(consistency["chapter_format"]),
        near_duplicate_passages=consistency["near_duplicates"],
        near_duplicate_count=len(consistency["near_duplicates"]),
        repeated_phrases=consistency["repeated_phrases"],
        derived_drift=derived_drift,
        derived_drift_count=len(derived_drift),
        derived_name_drift=derived_name_drift,
        derived_name_drift_count=len(derived_name_drift),
        canon_drift=_canon_drift(store),
        canon_source_drift=_canon_source_drift(store),
        irreversible_conditions=_irreversible_conditions(snap),
        glossary_coverage=_glossary_coverage(store),
        glossary_misuse=_glossary_misuse(store),
        active_entities_count=len(snap.get("entities") or {}),
        active_hooks_count=len(active_hooks),
        overdue_hooks_count=len(overdue_hooks),
        unresolved_long_term_commitments=_unresolved_long_term_commitments(plan, snap),
        open_debts_count=len([d for d in (snap.get("debts") or []) if d.get("status") == "open"]),
        quality_summary=_quality_summary(store),
        pacing={
            "flat_run": _pacing_flat_run(read_events(store)),
            "cross_volume_repetition": _cross_volume_repetition(store, plan),
        },
        outline_conformance=_outline_conformance(store, plan, len(chapters), total_words),
        creative_advisory=_creative_advisory(store, [cur_ch + 1]),
        usage_summary=_usage_summary(store, detail_window=_USAGE_DETAIL_WINDOW),
        hint="Book consistency audit completed; style_fingerprint is the cumulative voice baseline check.",
    )


# 只出 findings 不做 blocker——节奏判断的最终裁量在总编辑/作者，这里提供机器可见的信号。
_PACING_FLAT_THRESHOLD = 8


# 只出 findings 不做 blocker——节奏判断的最终裁量在总编辑/作者，这里提供机器可见的信号。
_PACING_FLAT_THRESHOLD = 8
_TERMINAL_DEBT_STATUSES = frozenset({"paid", "cancelled", "closed", "resolved", "done", "settled"})


_VOLUME_REPETITION_OVERLAP = 0.5


def _pacing_flat_run(events: list[dict[str, Any]], threshold: int = _PACING_FLAT_THRESHOLD) -> dict[str, Any] | None:
    """最长「无埋无收」连跑：重复申报同一条 open hook 不算新进展。

    张力曲线平坦的机器信号：埋收全停意味着期待管理停摆（量控政策见
    references/craft/tension-payoff.md）。达到阈值才报，返回区间与长度。
    """
    beat: dict[int, bool] = {}
    hook_state: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.get("type") == "governance":
            continue
        chapter = int(event.get("chapter") or 0)
        if chapter <= 0:
            continue
        delta = event.get("state_delta") or {}
        has_hook = False
        for hook in delta.get("hooks") or []:
            if not isinstance(hook, dict) or not hook.get("id"):
                continue
            ident = str(hook["id"])
            previous = hook_state.get(ident)
            current = (
                str(hook.get("text") if hook.get("text") is not None else (previous or ("", ""))[0]),
                str(hook.get("status") if hook.get("status") is not None else (previous or ("", "open"))[1]),
            )
            if previous != current:
                has_hook = True
            hook_state[ident] = current
        debt_settled = any(
            str(d.get("status") or "") in _TERMINAL_DEBT_STATUSES for d in delta.get("debts") or []
        )
        beat[chapter] = beat.get(chapter, False) or has_hook or debt_settled
    if not beat:
        return None
    best_start, best_len = 0, 0
    run_start: int | None = None
    for chapter in sorted(beat):
        if beat[chapter]:
            run_start = None
            continue
        if run_start is None:
            run_start = chapter
        length = chapter - run_start + 1
        if length > best_len:
            best_len, best_start = length, run_start
    if best_len < threshold:
        return None
    return {"from_chapter": best_start, "to_chapter": best_start + best_len - 1, "length": best_len}


def _shingles(text: str, size: int = 8) -> frozenset[str]:
    cleaned = re.sub(r"\s+", "", text)
    if len(cleaned) < size:
        return frozenset({cleaned} if cleaned else ())
    return frozenset(cleaned[i : i + size] for i in range(len(cleaned) - size + 1))


def _cross_volume_repetition(store: BookStore, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """卷间近似重复：两卷 L1 摘要的 8-gram 重叠度过高（退化期复读机的信号）。

    按较小集合的重叠率计（一卷摘要很短时不会被另一卷的长文本淹没）。
    """
    volume_summaries: dict[int, list[str]] = {}
    for chapter in plan.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        number = int(chapter.get("chapter") or 0)
        if number <= 0:
            continue
        path = store.summary_path(number)
        if not path.exists():
            continue
        try:
            summary = str(read_json(path).get("l1_summary") or "")
        except Exception:
            continue
        if summary:
            # 卷号可能是字符串（"vol-01"/"第二卷"，store.volume_meta 明文支持），
            # int() 会在 book audit 的卷间重复检测上整本炸掉。整型卷号保持原样
            # （兼容旧输出与既有测试），字符串卷号原样分组；排序一律按字符串。
            raw_volume = chapter.get("volume")
            if isinstance(raw_volume, int) and not isinstance(raw_volume, bool):
                volume_key: Any = raw_volume
            elif raw_volume in (None, ""):
                volume_key = 1
            else:
                volume_key = str(raw_volume).strip()
            volume_summaries.setdefault(volume_key, []).append(summary)
    volumes = sorted(volume_summaries, key=lambda key: str(key))
    out: list[dict[str, Any]] = []
    for i in range(len(volumes)):
        for j in range(i + 1, len(volumes)):
            left = _shingles("".join(volume_summaries[volumes[i]]))
            right = _shingles("".join(volume_summaries[volumes[j]]))
            if not left or not right:
                continue
            overlap = len(left & right) / min(len(left), len(right))
            if overlap >= _VOLUME_REPETITION_OVERLAP:
                out.append({"volumes": [volumes[i], volumes[j]], "overlap": round(overlap, 3)})
    return out


def _irreversible_conditions(snap: dict[str, Any]) -> dict[str, Any]:
    """不可逆代价全量清单（供复核角色核账，不进 pack 的完整副本）。

    这些是最不该漂的东西：单章看不出来，一旦被写反就是硬伤。
    汇总一处让总编辑能一眼核出"断了的手怎么又握上剑了"。
    """
    items: list[dict[str, Any]] = []
    for raw in snap.get("conditions") or []:
        if not isinstance(raw, dict) or not raw.get("irreversible"):
            continue
        if str(raw.get("status") or "active").lower() in ("resolved", "healed", "closed"):
            continue
        items.append(
            {
                "who": str(raw.get("who") or ""),
                "kind": str(raw.get("kind") or ""),
                "text": str(raw.get("text") or ""),
                "value": raw.get("value"),
                "unit": raw.get("unit"),
            }
        )
    return {"count": len(items), "items": items}


def _glossary_misuse(store: BookStore) -> list[dict[str, Any]]:
    """glossary 配置误用扫描：把它填成「词条→释义」词典是最常见的误用，
    键（往往是正典词）会被机检当违禁词全书误拦。机检侧已对疑似误用条目
    跳过拦截（gates._glossary_entry_misused），这里在总编台面把误用条目
    一条条点名，指路修正：清掉释义，换成「笔误变体→规范写法」。"""
    from ...content.gates import value_looks_like_definition

    glossary = store.load_config().get("glossary") or {}
    if not isinstance(glossary, dict) or not glossary:
        return []
    canon_terms = ""
    for card in store.load_kb():
        if isinstance(card, dict):
            canon_terms += str(card.get("id") or "") + str(card.get("title") or "")
    issues: list[dict[str, Any]] = []
    for wrong, preferred in glossary.items():
        reasons = []
        if value_looks_like_definition(str(preferred)):
            reasons.append("值像释义句而非可直接替换的规范词串")
        if wrong and wrong in canon_terms:
            reasons.append("键本身是知识库/正典里的词，被当违禁词自相矛盾")
        if reasons:
            issues.append({
                "wrong_term": wrong,
                "preferred_term": preferred,
                "reasons": reasons,
                "hint": (
                    "glossary 是『旧写法→规范写法』的归一化映射，不是释义词典。"
                    "请清掉这条释义，如确有笔误变体需要收编，写成 {\"笔误变体\": \"规范写法\"}。"
                ),
            })
    return issues


def _glossary_coverage(store: BookStore) -> dict[str, Any]:
    """术语闸门覆盖度：正典点名了禁用项、config.glossary 却是空的 → advisory。

    `_glossary_issues` 只拦"已登记的禁用串"；机制接线完整，但**开箱无人填**时
    三处闸门（submit / patch / audit）全部空转，且没有任何提示说它空转。
    这里把"建了表没人填"变成一条可见的 advisory，并指路 `book calibrate` 的候选清单。
    """
    cfg = store.load_config()
    glossary = cfg.get("glossary") or {}
    entries = len(glossary) if isinstance(glossary, dict) else 0
    ban_bullets = 0
    ban_cards: list[str] = []
    for card in store.load_kb():
        if not isinstance(card, dict):
            continue
        body = str(card.get("body") or "")
        n = sum(
            1
            for line in body.splitlines()
            if line.strip().startswith(("-", "*")) and ("勿" in line or "禁" in line)
        )
        if n:
            ban_bullets += n
            ident = str(card.get("id") or "")
            if ident:
                ban_cards.append(ident)
    out: dict[str, Any] = {
        "glossary_entries": entries,
        "canon_ban_bullets": ban_bullets,
        "canon_ban_cards": ban_cards[:8],
    }
    if ban_bullets and entries == 0:
        out["advisory"] = (
            f"正典里有 {ban_bullets} 条硬禁条目，但 `config.glossary` 是空的："
            "submit / patch / audit 三处术语闸门因此**全部空转**。"
            "跑 `book calibrate` 取候选清单，由总编辑裁决哪些钉成禁用串。"
        )
    return out


def _canon_source_drift(store: BookStore) -> dict[str, Any]:
    """「改了正典却忘了 kb sync」：cards.json 记录的源指纹 vs 当前 canon 源指纹。

    这是 `canon_drift` 的**盲区**：`canon_fingerprint()` 读的是编译产物 cards.json，
    所以改 `book/kb/canon/**` 而没跑 `kb sync` 时，cards.json 一字未变 →
    指纹不变 → `canon_drift` 不响 → 运行时一直用旧设定，全程静默。
    cards.json 没有 `source_fingerprint` 时返回 available=False，不误报。
    """
    from ...content.extract import canon_source_fingerprint

    recorded = ""
    if store.kb_path.exists():
        try:
            data = read_json(store.kb_path)
        except LedgerError:
            data = None
        if isinstance(data, dict):
            recorded = str(data.get("source_fingerprint") or "")
    current = canon_source_fingerprint(store.canon_dir)
    if not recorded:
        return {
            "available": False,
            "hint": "cards.json 未记录正典源指纹：跑一次 `kb sync` 后即可检测。",
        }
    # 源目录整体丢失时 fingerprint 为空，也必须判漂移。旧逻辑用 bool(current)
    # 把最严重的“整库被删”误报成 changed=false，运行时会继续静默使用旧 cards。
    missing_source = not store.canon_dir.is_dir()
    changed = missing_source or recorded != current
    out: dict[str, Any] = {
        "available": True,
        "changed": changed,
        "missing_source": missing_source,
        "recorded": recorded,
        "current": current,
    }
    if changed:
        out["hint"] = (
            "`book/kb/canon/` 不存在，已编译 cards 失去可核验源；先恢复正典目录再运行 `kb sync`。"
            if missing_source
            else (
                "`book/kb/canon/` 已被改动，但 `book/kb/cards.json` 还是旧版编译产物："
                "运行时（pack / glossary / 机检）读的都是 cards.json，所以现在全在按旧正典工作。"
                "跑 `kb sync` 重新编译；若改动会影响已写章节，再跑一次 `book audit` 做一致性复核。"
            )
        )
    return out


def _canon_drift(store: BookStore) -> dict[str, Any]:
    """正典漂移：当前正典指纹 vs 最近一次 commit 记录的正典指纹。

    只报事实（changed + 自哪一章起），不判定哪一章受影响——那是复核的活。
    旧事件没有 canon_sha 时返回 available=False，不误报。
    """
    from ...ledger.ledger import read_events

    current = store.canon_fingerprint()
    last_sha = ""
    last_chapter = 0
    for event in read_events(store):
        sha = str(event.get("canon_sha") or "").strip()
        if sha:
            last_sha = sha
            last_chapter = int(event.get("chapter") or event.get("effective_chapter") or 0)
    if not last_sha:
        return {
            "available": False,
            "current": current,
            "hint": "历史事件未记录正典指纹；下一次 commit 起可检测正典漂移。",
        }
    changed = last_sha != current
    return {
        "available": True,
        "changed": changed,
        "since_chapter": last_chapter,
        "current": current,
        "committed": last_sha,
        "hint": (
            f"正典在第 {last_chapter} 章 commit 后被改过：该章及之前的正文/账本可能仍按旧正典，"
            "须做一次一致性复核（价格、等级、硬禁、称谓等）。"
            if changed
            else "正典与最近 commit 一致。"
        ),
    }


def resync_baseline(
    store: BookStore, *, fix_quotes: bool = True, restamp_canon: bool = False
) -> dict[str, Any]:
    """基线自愈系统：当外部手改或批量格式化导致哈希脱节时，全量对齐 meta/acks 哈希基线并智能自愈失效 quotes。

    `restamp_canon=False`（默认）**不动正典指纹**：正典改过后 `canon_drift` 是"老章节可能
    仍按旧正典写"的告警，一次常规对齐不该把它无声抹掉。确实复盘确认无需回改正文时，
    显式传 `restamp_canon=True`（CLI `--restamp-canon`）才重盖章。
    """
    chapters = sorted(store.chapters_dir.glob("*/ch-*.md"))
    meta_fixed = 0
    acks_fixed = 0
    quotes_refreshed = 0
    summaries_fixed = 0

    last_ch = 0
    last_prose_hash = ""

    for cf in chapters:
        ch_num = int(cf.stem.split("-")[1])
        prose = cf.read_text(encoding="utf-8")
        cur_hash = "sha256:" + sha256_text(prose)
        words = chinese_word_count(prose)
        last_ch = max(last_ch, ch_num)
        last_prose_hash = cur_hash

        # 1. 更新 meta.json
        meta_file = store.meta_path(ch_num)
        meta_data = {}
        if meta_file.exists():
            try:
                meta_data = read_json(meta_file)
            except Exception:
                pass
        meta_data["chapter"] = ch_num
        meta_data["prose_hash"] = cur_hash
        # 键名必须是 `word_count`：commit 写的是它，`status` 读的也是它。
        # 早前这里写 `words`，而全仓没有任何读者——外部手改/格式化后跑完 resync，
        # 进度读数仍停在旧值，`book_words_written` 静默失真。
        meta_data["word_count"] = words
        atomic_json(meta_file, meta_data)
        meta_fixed += 1

        # 2. 更新 ack.json
        ack_file = store.ack_path(ch_num)
        if ack_file.exists():
            ack_data = {}
            try:
                ack_data = read_json(ack_file)
            except Exception:
                pass
            ack_data["chapter"] = ch_num
            ack_data["prose_hash"] = cur_hash
            if "verdict" not in ack_data:
                ack_data["verdict"] = "pass"

            if fix_quotes:
                quotes = ack_data.get("quotes") or []
                valid_quotes = [q for q in quotes if isinstance(q, str) and q in prose]
                before = len(valid_quotes)
                if len(valid_quotes) < 3:
                    lines = [l.strip() for l in prose.splitlines() if len(l.strip()) >= 10 and not l.strip().startswith("#")]
                    if len(lines) >= 3:
                        cands = [lines[0], lines[len(lines)//2], lines[-1]]
                    elif lines:
                        cands = lines
                    else:
                        cands = []
                    for cq in cands:
                        if cq and cq not in valid_quotes and cq in prose:
                            valid_quotes.append(cq)
                        if len(valid_quotes) >= 3:
                            break
                # 只保留真实正文子串：正文太短凑不足 3 条就保留现有的，绝不伪造引用
                valid_quotes = [q for q in valid_quotes if q in prose][:3]
                if len(valid_quotes) > before:
                    quotes_refreshed += 1
                ack_data["quotes"] = valid_quotes
                if len(valid_quotes) < 3:
                    ack_data["quotes_warning"] = (
                        "prose has fewer than 3 substring quotes available; "
                        "only real prose substrings were kept"
                    )
            atomic_json(ack_file, ack_data)
            acks_fixed += 1

        # 3. 重盖 summary.json：批级 checkpoint 对 meta/summary/ack 三份 artifact 都查
        # prose_hash，而这里曾只修 meta/ack——patch/外部手改过的章，summary 哈希永远
        # 陈旧，「resync 说全部修好 → checkpoint 依旧 prose_hash_mismatch」死循环
        # （实测宿主被迫手改 DB 里的 summary 行才解锁）。l1_summary 是内容不动，
        # 只对齐基线字段。
        summary_file = store.summary_path(ch_num)
        if summary_file.exists():
            summary_data = {}
            try:
                summary_data = read_json(summary_file)
            except Exception:
                pass
            if isinstance(summary_data, dict):
                summary_data["chapter"] = ch_num
                summary_data["prose_hash"] = cur_hash
                atomic_json(summary_file, summary_data)
                summaries_fixed += 1

    # 3. 同步更新 HEAD 与 status
    head = store.read_head()
    if head and int(head.get("chapter") or 0) == last_ch:
        head["prose_hash"] = last_prose_hash
        store.write_head(head)

    # 4. 正典指纹重盖章（**显式 opt-in**）：正典被复盘后改过、且已确认无需回改正文时，
    #    才追加正典基线裁决、清掉 canon_drift。默认不动——否则一次常规哈希
    #    对齐就会把"老章节可能按旧正典写"的告警无声抹掉，真漂移信号被钝化。
    canon_restamped = _restamp_canon_fingerprint(store) if restamp_canon else False

    return ok(
        action="resync_baseline",
        total_chapters=len(chapters),
        meta_fixed=meta_fixed,
        acks_fixed=acks_fixed,
        summaries_fixed=summaries_fixed,
        quotes_refreshed=quotes_refreshed,
        canon_restamped=canon_restamped,
        hint="All chapter hashes and quotes are now resynced to disk prose.",
    )


def _restamp_canon_fingerprint(store: BookStore) -> bool:
    """显式接受当前正典：追加基线裁决，保留原章节指纹。"""
    from ...ledger.ledger import append_governance_event

    current = store.canon_fingerprint()
    last_sha = next(
        (str(ev.get("canon_sha") or "").strip() for ev in reversed(read_events(store))
         if str(ev.get("canon_sha") or "").strip()),
        "",
    )
    if not last_sha or last_sha == current:
        return False
    append_governance_event(
        store,
        action="canon.restamp",
        actor="cli",
        reason="explicit --restamp-canon after canonical baseline review",
        fields={"canon_sha": current},
    )
    return True


def audit_hooks(store: BookStore) -> dict[str, Any]:
    """伏笔健康度审计：统计全书活跃伏笔、逾期分布及长线埋点。"""
    # 未初始化项目不能报"0 条活跃伏笔、健康"——那是假绿。
    store.read_head()
    snap = load_snapshot(store)
    cur_ch = int(snap.get("chapter") or 0)
    all_hooks = list(snap.get("hooks") or [])
    # deferred 计入活跃（写者曾自创 status=deferred，旧过滤只认 open/active 时
    # 改期钩会成为审计盲区——已兑现的也没人看见）。
    # 词表纪律见 ledger-editor 卡：改期只改 due，status 不发明新值。
    active = [h for h in all_hooks if h.get("status") in ("open", "active", "deferred")]
    overdue = [h for h in active if (coerce_due(h.get("due")) or 999999) < cur_ch]
    healthy = [h for h in active if (coerce_due(h.get("due")) or 999999) >= cur_ch]
    closed = [h for h in all_hooks if h.get("status") in ("closed", "paid")]

    return ok(
        action="hooks_audit",
        current_chapter=cur_ch,
        total_hooks=len(all_hooks),
        active_count=len(active),
        overdue_count=len(overdue),
        healthy_count=len(healthy),
        closed_count=len(closed),
        active_hooks=active,
        overdue_hooks=overdue,
        hint="Hooks health audit completed.",
    )


def close_hook(
    store: BookStore, *, hook_id: str, reason: str, actor: str = "unspecified"
) -> dict[str, Any]:
    """Append a dated closing ruling; original chapters remain intact."""
    from ...ledger.ledger import append_governance_event

    append_governance_event(
        store, action="hook.close", actor=actor, reason=reason, fields={"hook_id": hook_id}
    )
    return ok(
        action="hook_closed",
        hook_id=hook_id,
        reason=reason,
        hint=f"Hook {hook_id} closed by an append-only governance event.",
    )


def defer_hook(
    store: BookStore,
    *,
    hook_id: str,
    new_due: int,
    actor: str = "unspecified",
    reason: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Append a dated due-date ruling without changing the original promise.

    防线：已签章拍的 effects.hooks 是组装机检的期望值真源。若未写章还钉着旧
    due，只改账本必然在组装时打 expected_delta_missing；再回正账本又会改包
    内容触发 stale_pack（无人值守单书实测双重往返）。三条正路：先改计划
    （并重封批次）、让正文按钉住的 due 兑现、或 --force 显式豁免留痕。
    """
    from ...ledger.ledger import append_governance_event

    new_due = int(new_due)
    hook_id = str(hook_id or "").strip()
    try:
        committed = int(store.read_head().get("last_committed_ch") or 0)
    except LedgerError:
        committed = 0
    pinned: list[dict[str, Any]] = []
    for item in store.load_plan().get("chapters") or []:
        try:
            n = int(item.get("chapter") or 0)
        except (TypeError, ValueError):
            continue
        if n <= committed:
            continue  # 已写章的拍是历史事实，不构成未来期望
        for beat in item.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            for entry in (beat.get("effects") or {}).get("hooks") or []:
                if not isinstance(entry, dict) or str(entry.get("id") or "") != hook_id:
                    continue
                pinned_due = entry.get("due")
                if pinned_due is not None and int(pinned_due) != new_due:
                    pinned.append({
                        "chapter": n,
                        "beat": str(beat.get("id") or ""),
                        "pinned_due": int(pinned_due),
                    })
    if pinned and not force:
        raise LedgerError(
            "plan_hook_due_pinned",
            f"signed plan beats still pin hook {hook_id!r} at a different due; deferring the ledger "
            "alone guarantees expected_delta_missing at assembly (and reverting later triggers "
            "stale_pack). Amend the plan and re-seal the batch, let the prose pay the hook at its "
            "pinned due, or pass --force to waive with an audit trail",
            {"hook_id": hook_id, "new_due": new_due, "pinned": pinned[:8]},
        )
    append_governance_event(
        store,
        action="hook.defer",
        actor=actor,
        reason=reason or f"due moved to chapter {new_due}",
        fields={
            "hook_id": hook_id,
            "new_due": new_due,
            "forced_over_pinned": bool(pinned) or None,
        },
    )
    return ok(
        action="hook_deferred",
        hook_id=hook_id,
        new_due=new_due,
        pinned_due_overridden=pinned or None,
        hint=(
            f"Hook {hook_id} deferred to chapter {new_due} by an append-only governance event."
            + (
                " WARNING: signed beats still pin the old due (see pinned_due_overridden) — "
                "amend the plan and re-seal before those chapters assemble, or they will fail "
                "expected_delta checks."
                if pinned
                else ""
            )
        ),
    )


def set_world_spine(
    store: BookStore,
    *,
    text: str,
    actor: str = "unspecified",
    reason: str = "",
) -> dict[str, Any]:
    """world_spine 的受支持写入口（全书世界状态段，每章 pack 必注入的口径通道）。

    此前 spine 只被读、无人写——hatch 与 plan 子命令都不落 world_spine，于是
    quant_key_not_in_world_spine 在新构建上是**无法消除的警告**（无人值守
    单书实测只能绕道正典卡承接口径）。本命令把口径治理收回正规通道：save_plan 落
    真源、治理事件留痕、落盘后回验 quant 覆盖、在途草稿给 stale 预警。
    """
    import hashlib

    spine = str(text or "").strip()
    actor = str(actor or "").strip() or "unspecified"
    reason = str(reason or "").strip()
    if not spine:
        raise LedgerError("invalid_args", "set-spine requires non-empty spine text (--file or --text)")
    if not reason:
        raise LedgerError("invalid_args", "set-spine requires an explicit --reason (book-level contract change)")
    if len(spine) > 4000:
        raise LedgerError(
            "invalid_args",
            f"world_spine is injected into EVERY chapter pack; keep it under 4000 chars (got {len(spine)}). "
            "Compact world constants only — long canon belongs in kb cards (sliced on demand).",
        )
    plan = store.load_plan()
    old = str(plan.get("world_spine") or "").strip()
    plan["world_spine"] = spine
    store.save_plan(plan)
    digest = hashlib.sha256(spine.encode("utf-8")).hexdigest()[:12]
    from ...ledger.ledger import append_governance_event

    append_governance_event(
        store,
        action="plan.spine_set",
        actor=actor,
        reason=reason,
        fields={
            "length": len(spine),
            "sha256_prefix": digest,
            "prev_sha256_prefix": hashlib.sha256(old.encode("utf-8")).hexdigest()[:12] if old else None,
        },
    )
    quant_warnings = _quant_coverage_warnings(store, plan)
    midflight_note = None
    try:
        phase = str(store.read_head().get("phase") or PHASE_IDLE)
    except LedgerError:
        phase = PHASE_IDLE
    if phase != PHASE_IDLE:
        midflight_note = (
            "HEAD is mid-chapter: any in-flight draft was packed against the old spine and will hit "
            "stale_pack. Prefer spine edits at chapter boundaries; otherwise run `chapter next` to "
            "rebuild the pack before dispatching."
        )
    return ok(
        action="plan_spine_set",
        length=len(spine),
        sha256_prefix=digest,
        quant_keys_covered=not quant_warnings,
        quant_warnings=quant_warnings or None,
        midflight_warning=midflight_note,
        hint=(
            "world_spine saved to the plan truth store (every chapter pack injects it verbatim); "
            "governance event plan.spine_set appended"
            + (" — quant keys still missing from the spine, see quant_warnings" if quant_warnings else "")
        ),
    )


def rename_relation(
    store: BookStore,
    *,
    from_name: str,
    to_name: str,
    actor: str = "unspecified",
    reason: str = "",
) -> dict[str, Any]:
    """关系实体改名（治 relation_name_variant_suspected：两种叫法各累积一条关系）。

    who/target 任一命中 from_name 即归一为 to_name；原始事件与正文保留。
    """
    from ...ledger.ledger import append_governance_event

    from_name = str(from_name).strip()
    to_name = str(to_name).strip()
    if not from_name or not to_name or from_name == to_name:
        raise LedgerError("invalid_args", "--from and --to must differ and be non-empty")
    hits = sum(
        rel.get("who") == from_name or rel.get("target") == from_name
        for rel in load_snapshot(store).get("relations") or []
    )
    if not hits:
        raise LedgerError(
            "relation_not_found", f"no relation references the name {from_name!r}"
        )
    append_governance_event(
        store,
        action="relation.rename",
        actor=actor,
        reason=reason or f"canonicalize relation name {from_name!r} to {to_name!r}",
        fields={"from_name": from_name, "to_name": to_name},
    )
    return ok(
        action="relation_renamed",
        from_name=from_name,
        to_name=to_name,
        entries=hits,
        hint=(
            f"renamed {hits} current relation entries {from_name!r} -> {to_name!r} "
            "by an append-only governance event."
        ),
    )


def close_relation(
    store: BookStore, *, who: str, target: str, kind_substring: str, reason: str,
    actor: str = "unspecified",
) -> dict[str, Any]:
    """收敛同 pair 多条 open 关系（治 relation_pair_conflict）：按 who+target+kind 子串
    定位当前条目置 closed 并记裁决理由。kind_substring 为空则匹配该 pair 全部条目。"""
    from ...ledger.ledger import append_governance_event

    who = str(who).strip()
    target = str(target).strip()
    kind_substring = str(kind_substring or "").strip()
    if not who or not target or not reason.strip():
        raise LedgerError("invalid_args", "--who/--target/--reason are required")

    def _match(rel: dict[str, Any]) -> bool:
        if rel.get("who") != who or rel.get("target") != target:
            return False
        return not kind_substring or kind_substring in str(rel.get("kind") or "")
    hits = sum(_match(rel) for rel in load_snapshot(store).get("relations") or [])
    if not hits:
        raise LedgerError(
            "relation_not_found",
            f"no relation matches {who!r}->{target!r}"
            + (f" with kind containing {kind_substring!r}" if kind_substring else ""),
        )
    append_governance_event(
        store,
        action="relation.close",
        actor=actor,
        reason=reason,
        fields={"who": who, "target": target, "kind_substring": kind_substring},
    )
    return ok(
        action="relation_closed",
        who=who,
        target=target,
        kind_substring=kind_substring or None,
        entries=hits,
        reason=reason,
        hint=f"closed {hits} current relation entries ({who}->{target}) by governance event.",
    )


def merge_hook(
    store: BookStore,
    *,
    from_id: str,
    into_id: str,
    actor: str = "unspecified",
    reason: str = "",
) -> dict[str, Any]:
    """合并同主体重复登记的伏笔（duplicate_id_stem 治理入口）。

    from_id 的旧事件保留；新治理事件声明快照中由 into_id 幸存。
    """
    from ...ledger.ledger import append_governance_event

    if from_id == into_id:
        raise LedgerError("invalid_args", "--from and --into must be different hook ids")
    existing_ids = {
        str(h.get("id") or "") for h in load_snapshot(store).get("hooks") or []
    }
    if into_id not in existing_ids:
        raise LedgerError("hook_not_found", f"merge target {into_id} not found in snapshot hooks")
    if from_id not in existing_ids:
        raise LedgerError("hook_not_found", f"merge source {from_id} not found in snapshot hooks")
    append_governance_event(
        store,
        action="hook.merge",
        actor=actor,
        reason=reason or f"merge duplicate hook {from_id!r} into {into_id!r}",
        fields={"hook_id": from_id, "into_id": into_id},
    )
    return ok(
        action="hook_merged",
        from_id=from_id,
        into_id=into_id,
        hint=(
            f"Hook {from_id} merged into {into_id} by an append-only governance event."
        ),
    )


__all__ = [
    '_content_numeric_audit',
    'reconcile_book',
    'calibrate_book',
    'book_facts',
    '_derived_name_drift',
    'audit_book',
    '_PACING_FLAT_THRESHOLD',
    '_TERMINAL_DEBT_STATUSES',
    '_VOLUME_REPETITION_OVERLAP',
    '_pacing_flat_run',
    '_shingles',
    '_cross_volume_repetition',
    '_irreversible_conditions',
    '_glossary_misuse',
    '_glossary_coverage',
    '_canon_source_drift',
    '_canon_drift',
    'resync_baseline',
    '_restamp_canon_fingerprint',
    'audit_hooks',
    'close_hook',
    'defer_hook',
    'set_world_spine',
    'rename_relation',
    'close_relation',
    'merge_hook',
]
