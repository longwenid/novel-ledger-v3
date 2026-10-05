# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：_common
from __future__ import annotations

from pathlib import Path
from typing import Any
from ...ledger.ledger import (coerce_due, load_snapshot)
from ...content.style_check import (check_style_text)
from ...infra.store import (BookStore, _volume_key_number, _volume_label, volume_entry_for)
from ...infra.scale import (COMPLETION_MIN_RATIO, derive_chapter_word_target)
from ...content.views import (make_assemble_view, make_draft_view, make_polish_view, render_assemble_brief)
from ...infra.util import (LedgerError, atomic_json_ordered, atomic_text, canonical_json, chinese_word_count, now_ts, read_json, sha256_text)

# 以及本次改动有没有引入新破坏。它不整轮重审（superpowers 的 scoped re-review）。
# 只属于组装阶段、无需重写正文的机检失败。命中这些 code 时只重跑 `assemble`，
# 保留草稿与润色终稿；其余失败（beat/must/字数/连续性/术语等）才整链重写正文。
_ASSEMBLY_ONLY_ISSUES = frozenset(
    {
        "missing_field",
        "empty_prose",
        "empty_l1_summary",
        "state_delta_not_object",
        "memory_not_object",
        "pack_hash_mismatch",
        "beats_hit_not_list",
        "beat_missed",
        "state_delta_moves_not_list",
        "state_delta_facts_not_list",
        "state_delta_debts_not_list",
        "state_delta_hooks_not_list",
        "state_delta_relations_not_list",
        "state_delta_named_not_list",
        "state_delta_new_names_not_list",
        "state_delta_deaths_not_list",
        "state_delta_revivals_not_list",
        "state_delta_nonliving_not_list",
        "state_delta_conditions_not_list",
        "state_delta_items_not_list",
        "state_delta_knowledge_not_list",
        "lifecycle_name_invalid",
        "lifecycle_duplicate_name",
        "condition_not_object",
        "condition_missing_who",
        "condition_missing_text",
        "item_not_object",
        "item_missing_id_or_name",
        "knowledge_not_object",
        "knowledge_missing_who",
        "knowledge_missing_topic_id",
        "knowledge_missing_claim",
        "knowledge_missing_source",
        "knowledge_missing_quote",
        "knowledge_invalid_stance",
        "knowledge_duplicate_topic",
        "unnamed_in_pack",
        "move_missing_who",
        "fact_not_object",
        "fact_missing_who",
        "fact_missing_text",
        "debt_not_object",
        "debt_missing_id",
        "debt_missing_text",
        "hook_not_object",
        "hook_missing_id",
        "hook_missing_text",
        # due 类型毒化在提交端就地拒收：改成整数章号是纯 delta 编辑，不回正文。
        "hook_due_not_int",
        "debt_due_not_int",
        "relation_not_object",
        "relation_missing_fields",
        "death_missing_who",
        "location_not_object",
        "location_missing_id_or_name",
        "location_attributes_not_object",
        "location_missing_quote",
        "location_invalid_status",
        "location_replaces_not_list",
        "state_delta_locations_not_list",
        # 场景地图冲突：出路之一是组装侧加 replaces 留痕（字段级修复），先回组装；
        # 正文真的写漂了再由总编辑按 hint 升级正文返工。
        "location_attribute_conflict",
        "delta_quote_not_in_prose",
        "delta_quote_too_short",
        "expected_delta_quote_missing",
        # 守卫认不出兑现≠写者没兑现：delta 里有身份一致的近失条目、字段对不上，
        # 组装侧按 diffs 对齐即可（零正文改动）；正文真没演出由 hint 升级正文返工。
        "expected_delta_shape_mismatch",
        # 情节自检字段的形状/证据问题：属组装契约，回组装重跑，不误伤正文。
        "plot_findings_not_list",
        "plot_findings_missing",
        "plot_finding_not_object",
        "plot_finding_severity_invalid",
        "plot_finding_missing_hint",
        "plot_finding_quote_missing",
        "plot_finding_quote_too_short",
        "plot_finding_quote_not_in_prose",
    }
)


_PLOT_SELF_CHECK = (
    "assembly_plot_self_check",
    (
        "1. 拍点兑现：本章 beats 每一场戏是否都在终稿里当场发生，required beat 的事件、动作、关键信息齐全。",
        "2. 世界规则与人物边界：出场人的能力、资源、身份和社会位置是否符合已给正典与当前状态；"
        "区分人物能够做什么、通常会做什么与正典明确禁止什么，不凭题材标签补规则。",
        "3. 时间线与因果：时间是否单调向前、因果链是否成立，不改断章内与章间时序。",
        "4. 账目口径与算式（必须动手算，不许凭印象）："
        "①逐笔回算——凡正文出现『A、B、C……拢共/一共/合/共 D』的枚举求和，逐项相加核对 D 是否等于各项之和；"
        "②比例回算——凡『抽 N 成 / 扣 N 成，实到 M』『成交额 X 抽成后剩 M』，按 pack 的 kb_slice / world_spine 口径重算 M；"
        "③量价回算——凡『数量 × 单价＝总额』『整批价 ÷ 件数＝单价』，重算一遍；"
        "④同章不许两值——同一笔账/同一单价在本章出现两个互斥的数即报；"
        "⑤口径一致——金额、租额、利率、里程、方位与 pack 的 kb_slice / world_spine 是否一致。",
        "5. 连续性：已知人物不失忆、不写成初见，已死角色不以活人身份行动。",
        "6. 四层事件脊柱：若 verification_brief 含本章阶段、故事线路与时间线，核对终稿是否实际推进对应事件，"
        "是否遵守当前幕张力方向且没有提前兑现后续幕；不能只因章拍填了引用 ID 就判通过。",
        "7. 首读定位与转场：读者能否从正文辨认当前人物、地点、时点与眼前目标；换场、换时或换视角时，"
        "是否有足够线索接上前一场。允许有意保留谜团，不要求旁白解释一切。",
        "8. 章内局部变化与人物反应：关键事件是否改变人物可用的信息、关系、资源或下一步行动，"
        "受影响的人是否在行动、对白或选择中作出可见反应；休整章也可用小变化推进，不强求每场戏反转。",
        "9. 人设与行为一致性：逐场核对人物已确立的经历、欲望、能力、认知、关系和本场目标，"
        "是否支撑其选择、言语、动作与情绪；偏离既有模式时，正文是否留有可追索的变化线索与后果；"
        "动机暂时隐藏时是否有后续解释安排，不凭身份标签或动作词判错。",
        "把发现写进 submit JSON 的 plot_findings 数组，每条 {code, severity, hint, quote}；"
        "severity 必须是 BLOCKER/WARNING/NIT/UNVERIFIABLE，只有 BLOCKER 会回草稿重写；"
        "判据在工作包里看不见的（跨章事实、被上限截断的候选卡）报 UNVERIFIABLE 并写清"
        "需要什么才能验证，不得脑补放行；quote 逐字抄自终稿且 ≥6 字。"
        "首读定位、转场、局部变化和人设一致性的读感疑问默认报 WARNING；"
        "人物行为直接违背已给正典、明确的人物状态或正文事实，才标 BLOCKER；"
        "缺少关键判据则报 UNVERIFIABLE，不以类型惯例补证。没有发现时写空数组 []。",
    ),
)


def _style_gate_enabled(store: BookStore) -> bool:
    """config.style_check 是否对当前 voice 生效（auto 只在 shijing 启用）。"""
    mode = store.load_config().get("style_check", "auto")
    voice_id = str((store.load_voice_profile() or {}).get("voice_id") or "")
    return mode is True or (mode == "auto" and voice_id == "shijing")


def _polish_disabled(store: BookStore) -> bool:
    """config.polish = "off" → 只写作模式：草稿即终稿，跳过润色相位。"""
    value = store.load_config().get("polish", "on")
    return str(value).strip().lower() in ("off", "false", "none", "0")


def _style_structure_args(store: BookStore) -> dict[str, Any]:
    """结构层 advisory 的书级调节旋钮（参考带可按书调整）。

    - config.style_structure = "off" → 关闭句长、对白段型与句式形状（短句排队/同头排比）提示；
    - config.style_structure_limits = {指标名: 限值} → 按指标覆盖提示参考带
      （指标名见 style_check._STRUCTURE_SPEC：句长_中位数 / 句长_P90 /
      长句占比%(>=50字) / 短句占比%(<=10字) / 句长CV）；
    - config.style_contrast_limit = int → 对照句提示的参考计数（默认 1）。
    """
    cfg = store.load_config()
    kwargs: dict[str, Any] = {}
    if str(cfg.get("style_structure") or "").strip().lower() in ("off", "false", "none", "0"):
        kwargs["structure_off"] = True
    limits = cfg.get("style_structure_limits")
    if isinstance(limits, dict) and limits:
        kwargs["structure_limits"] = limits
    if cfg.get("style_contrast_limit") is not None:
        try:
            kwargs["contrast_limit"] = int(cfg.get("style_contrast_limit"))
        except (TypeError, ValueError):
            pass
    return kwargs


def _log_quality(store: BookStore, chapter: int, event: str, **fields: Any) -> None:
    """质量留痕：每章审校/机检/提交结果追加到 quality.jsonl，供 book audit 汇总。"""
    record = {
        "schema": "novel-ledger.quality.v1",
        "ts": now_ts(),
        "chapter": chapter,
        "event": event,
        **fields,
    }
    store.quality_log_path.parent.mkdir(parents=True, exist_ok=True)
    from ...infra.util import append_bytes
    append_bytes(store.quality_log_path, canonical_json(record))


def _corpus_style_fingerprint(
    store: BookStore,
    *,
    min_chapters: int | None = None,
) -> dict[str, Any]:
    """累计节奏指纹：把已提交章节拼成一个样本，用同一套人写基线跑完整画像。

    逐章不拦分布指标，统计上只有累计到足够样本（默认 ≥5 章）才有意义。
    """
    cfg = store.load_config()
    minimum = int(min_chapters if min_chapters is not None else (cfg.get("style_fingerprint_min") or 5))
    chapters = sorted(store.chapters_dir.glob("*/ch-*.md"))
    if not _style_gate_enabled(store):
        return {"skipped": True, "reason": "style_check_off"}
    if len(chapters) < minimum:
        return {
            "skipped": True,
            "reason": "not_enough_chapters",
            "committed": len(chapters),
            "min_chapters": minimum,
        }
    texts = [cf.read_text(encoding="utf-8") for cf in chapters]
    result = check_style_text("\n\n".join(texts), **_style_structure_args(store))
    return {
        "skipped": False,
        "chapters": len(chapters),
        "chars": sum(len(t) for t in texts),
        "words": sum(chinese_word_count(t) for t in texts),
        "ok": result["ok"],
        "hard_fails": result["hard_fails"],
        "warnings": result["warnings"],
        "distribution_fails": result["distribution_fails"],
        "fails": result["fails"],
        "hint": (
            "cumulative voice fingerprint: distribution deviations are advisory; "
            "read the scenes before revising prose"
        ),
    }


def _require_active(head: dict[str, Any]) -> None:
    if (head.get("status") or "active") == "completed":
        raise LedgerError("book_completed", "book is completed; write path is closed")


def _touch(head: dict[str, Any]) -> dict[str, Any]:
    head["updated_at"] = now_ts()
    return head


def _book_words_written(store: BookStore) -> int:
    total = 0
    for meta_path in store.chapters_dir.glob("*/ch-*.meta.json"):
        try:
            total += int(read_json(meta_path).get("word_count") or 0)
        except (OSError, ValueError, TypeError):
            continue
    return total


def _plan_extend_through(
    store: BookStore, cfg: dict[str, Any], max_planned: int, last_committed: int
) -> tuple[int, Any]:
    """本次扩纲必须覆盖到的章号：plan_extend_span=int → 扩到 max+N；"volume" → 扩满一卷。

    实测教训：水位提示不带跨度，worker 每轮贴线补 ~5 章，63 章烧了 13 轮扩纲
    ≈109 万 token（每轮的钱≈写一章）。跨度由此处统一计算，回执与简报以
    extend_through_ch 显式下达。volume 模式按 plan.volumes 的 chapters_budget
    签满当前卷（当前卷已签满则整签下一卷）；无卷合同回退 20 章。
    """
    raw = cfg.get("plan_extend_span")
    if raw is None or raw == "":
        raw = 20
    if isinstance(raw, str) and raw.strip().lower() == "volume":
        plan = store.load_plan()
        volumes = plan.get("volumes")
        volume_of: dict[int, Any] = {}
        for item in plan.get("chapters") or []:
            try:
                num = int(item.get("chapter") or 0)
            except (TypeError, ValueError):
                continue
            if num > 0:
                volume_of[num] = item.get("volume", 1)
        cur = volume_of.get(max_planned + 1, volume_of.get(max_planned, 1))
        cur_num = _volume_key_number(cur) or 1
        entry = volume_entry_for(volumes, cur_num) or {}
        budget = int(entry.get("chapters_budget") or 0)
        if budget <= 0:
            return max_planned + 20, "volume(fallback:20)"
        written_in_vol = sum(
            1
            for c, v in volume_of.items()
            if 0 < c <= last_committed and (_volume_key_number(v) or 1) == cur_num
        )
        vol_end = last_committed + (budget - written_in_vol)
        if max_planned >= vol_end:
            nxt = volume_entry_for(volumes, cur_num + 1) or {}
            vol_end += int(nxt.get("chapters_budget") or 0) or budget
        return max(vol_end, max_planned + 1), "volume"
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 20
    return max_planned + max(1, n), n


def _volume_watermark(store: BookStore, head: dict[str, Any]) -> dict[str, Any] | None:
    """卷合同水位：当前卷剩余章数 + 下一卷卷脊是否已签（左移预警）。

    背景：hatch 只签第一卷，覆盖率左移告警只活在 plan validate / book audit 这两个
    离线巡检里，不在章节主循环上。水位进 status / chapter next，
    无人值守每章可见。未启用卷合同（plan.volumes 为空）的书返回 None 不打扰。
    """
    plan = store.load_plan()
    volumes = plan.get("volumes")
    if not isinstance(volumes, dict) or not volumes:
        return None
    last_c = int(head.get("last_committed_ch") or 0)
    next_ch = last_c + 1
    volume_of: dict[int, Any] = {}
    for item in plan.get("chapters") or []:
        try:
            num = int(item.get("chapter") or 0)
        except (TypeError, ValueError):
            continue
        if num > 0:
            volume_of[num] = item.get("volume", 1)
    cur_num = _volume_key_number(volume_of.get(next_ch, volume_of.get(last_c, 1))) or 1
    cur_entry = volume_entry_for(volumes, cur_num) or {}
    budget = int(cur_entry.get("chapters_budget") or 0)
    written_in_vol = sum(
        1
        for c, v in volume_of.items()
        if 0 < c <= last_c and (_volume_key_number(v) or 1) == cur_num
    )
    next_num = cur_num + 1
    next_entry = volume_entry_for(volumes, next_num) or {}
    next_signed = bool(str(next_entry.get("spine") or "").strip())
    cfg = store.load_config()
    _gap = cfg.get("volume_spine_warn_gap")
    warn_gap = 20 if _gap is None else int(_gap)
    target = int(cfg.get("book_words") or 0)
    progress = _book_words_written(store) / target if target > 0 else 0.0
    left = budget - written_in_vol if budget > 0 else None
    warn = bool(
        left is not None
        and left <= warn_gap
        and not next_signed
        and progress < COMPLETION_MIN_RATIO
    )
    return {
        "current_volume": cur_num,
        "chapters_budget": budget or None,
        "chapters_written_in_volume": written_in_vol,
        "chapters_left_in_volume": left,
        "next_volume": next_num,
        "next_volume_signed": next_signed,
        "warn": warn,
        "warn_gap": warn_gap,
        "hint": (
            f"vol-{cur_num:02d} 剩约 {left} 章，下一卷（vol-{next_num:02d}）卷脊未签："
            "在跨入下一卷之前用 `plan extend` 的 volumes 载荷签卷（可与扩纲同批原子完成），"
            "否则卷脊静默退化为全局兜底，终局措辞进章拍时 scale 合同会硬拒整批扩纲。"
        )
        if warn
        else None,
    }


_HEAD_TRANSITION_FIELDS = ("chapter", "phase", "status", "last_committed_ch", "last_acked_ch")


def _head_transition(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    """`chapter next` 不是只读动作——它会落盘推进 HEAD。把这次推进显式回报出来。

    有人只是"想看看下一章是什么"就跑了一次 `chapter next`，
    HEAD 从 `chapter=3/phase=idle` 变成 `chapter=4/phase=await_draft`、
    `quality.jsonl` 多出一条 `chapter 4 begin`、pack 也换了一份——但输出里没有任何一处
    说明"状态已被本命令推进"。事后审计很容易把它读成"第 4 章已经开工"。
    只读台面请用 `status`；这里给出机器可读的差异，免得靠人去比对。
    """
    changed = {
        key: [before.get(key), after.get(key)]
        for key in _HEAD_TRANSITION_FIELDS
        if before.get(key) != after.get(key)
    }
    if not changed:
        return None
    return {
        "changed": changed,
        "note": "this command advanced on-disk state; use `status` for a read-only view",
    }


def _preserve_rework_text(store: BookStore, chapter: int) -> Path | None:
    """销毁前轮转：把现有终稿（无则草稿）存为 .revK 轮转档（真源托管）。

    机检只验终稿是否过闸（must/引文/state_delta 与旧稿无关），流水线没有理由
    销毁已过字数带、场景结构完整的旧稿——实测整稿重写 1.9M token，宿主备份后
    单句定点修 0.15M，差一个数量级。轮转档经 _draft_action 交给返工 worker
    作定点修复底稿；闸门一个不少。
    """
    source = None
    if store.polished_text_path(chapter).exists():
        source = store.polished_text_path(chapter)
    elif store.draft_text_path(chapter).exists():
        source = store.draft_text_path(chapter)
    if source is None:
        return None
    text = source.read_text(encoding="utf-8")
    rev = 1
    latest = store.latest_stage_revision(chapter)
    if latest is not None:
        name = Path(str(latest)).name
        tail = name.split(".rev", 1)[1].split(".txt", 1)[0] if ".rev" in name else ""
        if tail.isdigit():
            rev = int(tail) + 1
    target = store.stage_revision_path(chapter, rev)
    store.accept_stage_text(target, text)
    return target


def _refresh_stage_views(
    store: BookStore,
    pack: dict[str, Any],
    chapter: int,
    *,
    scope: str = "draft",
) -> None:
    """canonical pack 变化后同步重建各阶段视图，并按影响层清掉过期 staging。

    视图走保序序列化（`atomic_json_ordered`）：把每章一字不差的手册/契约排在文件最前，
    形成稳定前缀供宿主 prompt cache 命中。canonical pack（current.json / 归档）仍走
    `atomic_json`（排序键，字节确定）——它是 hash 权威，顺序不能动。

    scope 决定清理深度（防旧文配新包，但不误删仍有效的过闸文本）：
    - "draft"：整章重写语义——草稿/终稿与全部重试产物清空（begin/retry 的既定行为）；
    - "polish"：只回润色——保留草稿文本，清终稿与其后的重试产物；
    - "none"：本章视图内容未变（仅上游全量指纹变化）——只重建视图文件，staging 全部保留。
    """
    atomic_json_ordered(store.draft_pack_path(chapter), make_draft_view(pack))
    atomic_json_ordered(store.polish_pack_path(chapter), make_polish_view(pack))
    assemble_view = make_assemble_view(pack)
    _attach_prose_hash(store, chapter, assemble_view)
    atomic_json_ordered(store.assemble_pack_path(chapter), assemble_view)
    # 组装简报与视图同源同生：视图重建即重渲染，worker 永远读到与包一致的任务书。
    atomic_text(
        store.assemble_brief_path(chapter),
        render_assemble_brief(assemble_view),
    )
    protected: set[Any] = set()
    if scope == "polish":
        protected = {store.draft_text_path(chapter)}
    elif scope == "draft":
        # 整章重写语义也只轮转不销毁：stale_pack 作废的是包，不是已写好的正文。
        _preserve_rework_text(store, chapter)
    elif scope == "none":
        protected = {
            store.draft_text_path(chapter),
            store.polished_text_path(chapter),
            store.style_metrics_path(chapter),
            store.polish_anchor_path(chapter),
        }
    for stale in (
        store.draft_text_path(chapter),
        store.polished_text_path(chapter),
        store.style_metrics_path(chapter),
        store.polish_anchor_path(chapter),
    ):
        if stale in protected:
            continue
        if stale.exists():
            stale.unlink()


def hooks_schedule_gaps(store: BookStore) -> list[dict[str, Any]]:
    """due 对排期对账：仍 open 的 hook 若已被某章 effects.hooks 引用
    （最早引用章 C）而账本 due < C，报 gap——due 抢在排期前面，会在 C 之前的每章
    被预检/自检点名制造噪音 WARNING。处置：`hooks defer --due C` 对齐，或改排期。

    只对「有引用」的钩子判矛盾（文本排场无声明时由逐章预检兜底，这里不猜文本）。
    """
    snap = load_snapshot(store)
    open_hooks = {
        str(h.get("id")): h
        for h in snap.get("hooks") or []
        if str(h.get("status") or "open") not in ("paid", "closed", "abandoned")
    }
    if not open_hooks:
        return []
    first_ref: dict[str, int] = {}
    for item in store.load_plan().get("chapters") or []:
        try:
            n = int(item.get("chapter") or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        for beat in item.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            for entry in (beat.get("effects") or {}).get("hooks") or []:
                hid = str(entry.get("id")) if isinstance(entry, dict) else ""
                if hid and hid in open_hooks:
                    cur = first_ref.get(hid)
                    if cur is None or n < cur:
                        first_ref[hid] = n
    gaps = []
    for hid, hook in open_hooks.items():
        due = coerce_due(hook.get("due")) or 0
        ref = first_ref.get(hid)
        if due > 0 and ref is not None and due < ref:
            gaps.append({"id": hid, "due": due, "scheduled_at": ref})
    return sorted(gaps, key=lambda g: (g["due"], g["id"]))


def overdue_hooks_undeclared(store: BookStore) -> list[dict[str, Any]]:
    """逾期伏笔未声明检查：已过 due 且仍 open 的钩，若没有任何未写章 effects.hooks
    引用，签批/校验时报出。

    实测里 planner 常只在拍点文本里排回收、effects.hooks 声明数为 0——「排进
    effects.hooks」不能只靠纪律文案。
    报出≠阻断：处置有两条正路（声明进章拍 / hooks defer 改期），由总编辑裁决。
    """
    snap = load_snapshot(store)
    try:
        next_ch = int(store.read_head().get("last_committed_ch") or 0) + 1
    except LedgerError:
        next_ch = 1  # 未初始化项目（裸 plan 校验）：全部章按未写算
    overdue = [
        h
        for h in snap.get("hooks") or []
        if str(h.get("status") or "open") not in ("paid", "closed", "abandoned")
        and 0 < (coerce_due(h.get("due")) or 0) < next_ch
    ]
    if not overdue:
        return []
    declared: set[str] = set()
    for item in store.load_plan().get("chapters") or []:
        try:
            n = int(item.get("chapter") or 0)
        except (TypeError, ValueError):
            continue
        if n < next_ch:
            continue
        for beat in item.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            for entry in (beat.get("effects") or {}).get("hooks") or []:
                if isinstance(entry, dict) and entry.get("id"):
                    declared.add(str(entry["id"]))
    return [
        {"id": h.get("id"), "due": h.get("due"), "text": str(h.get("text") or "")}
        for h in overdue
        if str(h.get("id")) not in declared
    ]


def _one_pass_expansion_hint(need: int) -> str:
    """把「还差 N 字」换算成一次性补足指令（英文 hint 用，中文版在 views/dispatch 手册）。

    现场复盘（第 6 章 6 轮 precheck：2237→2485→2509）：写者每轮只挤 50-100 字，
    闭一个 250 字缺口要 5-6 轮。指令必须落到结构粒度——整场戏——并显式超额，
    因为写者补写普遍欠量。
    """
    scenes = max(1, -(-need // 600))
    return (
        f"close it in ONE pass: add at least {need + max(50, need // 8)} more hanzi "
        f"≈ {scenes} full scene(s) of dialogue-with-consequence "
        "(a full scene runs 500-700 hanzi; ~30 hanzi per line, 4-6 lines per paragraph); "
        "do NOT sprinkle filler words — padding trips the AI-flavor quotas at the next gate; "
        "add events and exchanges instead, then re-run the stage submit"
    )


def _ack_hint(store: BookStore, head: dict[str, Any], requirements: dict[str, int]) -> str:
    """ack 只做最终完整性核对：全文真读 + 引用。

    正文/文风判断已由上游闸门（组装自检 + polish 机检）完成，ack 不重复判，
    也不需要手册或账本。
    """
    upstream = "assembly plot self-check plus polish machine/content gates"
    return (
        "execute the FINAL-REVIEW role under the execution contract; read chapter_path in full without "
        f"echoing the prose, then run ack-read with ≥{requirements['minimum']} verbatim quotes "
        f"(≥{requirements['minimum_chars']} chars each; for chapters with at least "
        f"{requirements['require_latter_half_when_chars_at_least']} chars, include one from the latter half). "
        f"Upstream closure used {upstream}; do NOT re-judge plot or style here. "
        "The reviewer must read end-to-end; machine checks establish quote validity only, not proof of reading. "
        "Exception: report verdict p0 only for an objective defect visible from this chapter alone: "
        "truncation/corruption, a duplicated large block, or an internal self-contradiction. "
        "Never use p0 for taste, preference, or facts unavailable in chapter_path."
    )


def _attach_prose_hash(store: BookStore, chapter: int, assemble_view: dict[str, Any]) -> None:
    """把现行终稿哈希烙进组装视图（随简报预填进提交模板）。

    plot_findings 是组装 worker 对其所读正文的一次性判断；prose_hash 是 submit
    判断「这份判断属于哪版正文」的凭证——缺失（旧书/旧包）只降级为不检查，
    不匹配则整份组装件拒收（_stale_plot_self_check）。
    """
    polished_path = store.polished_text_path(chapter)
    if not polished_path.exists():
        return
    assemble_view["prose_hash"] = "sha256:" + sha256_text(
        polished_path.read_text(encoding="utf-8").rstrip("\n")
    )


def _unresolved_long_term_commitments(
    plan: dict[str, Any], snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """Signed book-level promises need an actual paid hook with the same stable id.

    This is an evidence signal, not a literary verdict: an author can revise the
    outline if a planted clue no longer belongs in the ending.
    """
    outline = plan.get("book_outline") if isinstance(plan.get("book_outline"), dict) else {}
    commitments = outline.get("long_term_commitments") or []
    hooks = {
        str(hook.get("id")): hook
        for hook in snapshot.get("hooks") or []
        if isinstance(hook, dict) and hook.get("id")
    }
    unresolved = []
    for index, item in enumerate(commitments):
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id") or f"commitment-{index + 1:04d}")
        hook = hooks.get(ident) or {}
        if str(hook.get("status") or "") == "paid":
            continue
        unresolved.append({
            "id": ident,
            "promise": str(item.get("promise") or ""),
            "resolved_volume": item.get("resolved_volume"),
            "hook_status": hook.get("status") or "unplanted",
            "last_touch_chapter": hook.get("updated_chapter"),
        })
    return unresolved


def _append_voice_concept(store: BookStore, text: str) -> bool:
    """ack `--voice-note` / 写者 `memory.voice_concepts` → `voice.json` 的 session_notes。

    以「不要」「禁止」开头的条目直接丢弃：这是"不建禁词表"这条设计在代码里的落点
    （见 references/write-output.md），不是文案偏好。

    返回是否真的落盘。调用方**必须**把 False 告诉用户：静默吞掉一条作者指令，
    比直接报错更难发现（作者以为自己说过了）。

    只写 session_notes，绝不碰 concepts。早前两者共用一个 `concepts[-16:]` 滑窗、
    而 pack cap 是 12，于是**一次** `--voice-note` 就能把首条「硬禁对举句」挤出 pack ——
    那条概念在写者契约里无处可查、也没有任何机检兜得住，挤掉即彻底失传。
    """
    text = text.strip()
    if not text or text.startswith("不要") or text.startswith("禁止"):
        return False
    notes = store.load_voice_session_notes()
    if text not in notes:
        notes.append(text)
    store.save_voice_session_notes(notes)
    return True


# 与"重写一次就够贵"的实际代价对齐；正常重跑（stale_pack）不计入。
_REWORK_EVENTS = frozenset(
    {
        ("style_machine", "fail"),
        ("polish_anchor", "fail"),
        ("submit", "rewrite"),
    }
)


#
# 创意编辑（creative）是开书之外的**按需**角色：跨卷设计、全书大纲修订、剧情破局都由
# 总编辑裁量是否采用。脚本永不自动创建物理 agent——产物是"给人拍板的证据"，必须有人在环。
# 缺的从来不是能力而是**触发信号**：入口文档只在开书场景提它，于是跨卷/缺大纲这些时刻
# 全靠宿主自己想起来，长跑里就成了死角。这里把"该想起它的时刻"（跨入新卷）变成机器
# 可读的一行 advisory，是否采用该逻辑角色仍归总编辑。
def _creative_advisory(store: BookStore, chapters: list[int]) -> dict[str, Any] | None:
    """本批章节若跨入新卷或卷内新阶段，返回创意编辑介入建议；否则 None。

    触发点包括：
    1. 跨卷（Volume Boundary）：覆盖已声明 book_outline 的核对与未声明时的补纲建议；
    2. 卷内阶段跃迁（Mid-Volume Phase Transition）：若章拍中声明了 phase_id 且在同卷内
       发生阶段变更，提示按创意编辑角色出具阶段小纲（Phase Brief）。
    """
    plan = store.load_plan()
    chapter_target = derive_chapter_word_target(plan, store.load_config())
    volume_of: dict[int, str] = {}
    phase_of: dict[int, str] = {}
    for item in plan.get("chapters") or []:
        try:
            num = int(item.get("chapter") or 0)
        except (TypeError, ValueError):
            continue
        if num > 0:
            volume_of[num] = _volume_label(item.get("volume", 1))
            p_val = item.get("phase_id")
            if p_val is not None and str(p_val).strip():
                phase_of[num] = str(p_val).strip()

    # 1. 优先检测跨卷边界
    vol_boundaries = [
        ch
        for ch in sorted(chapters)
        if ch > 1 and ch in volume_of and (ch - 1) in volume_of
        and volume_of[ch] != volume_of[ch - 1]
    ]
    if vol_boundaries:
        first = vol_boundaries[0]
        if isinstance(plan.get("book_outline"), dict):
            boundary_volume = volume_of[first]
            boundary_entry = volume_entry_for(plan.get("volumes"), boundary_volume) or {}
            unsigned = not bool(str(boundary_entry.get("spine") or "").strip())
            hint = (
                f"第 {first} 章起进入新卷：对照 plan.book_outline 与 book/editorial/outline.md "
                "核对本卷收口与下一卷卷脊；与意图/大纲冲突时，按创意编辑（creative）角色起草修订，"
                "再由总编辑签发。"
            )
            if unsigned:
                # 真查 plan.volumes 有无本卷条目：只说“核对卷脊”不查条目，
                # 跨卷批可以在不签卷的情况下落库，卷脊静默退化为全局兜底。
                hint += (
                    f" 本批跨入的卷（{boundary_volume}）在 plan.volumes 尚无卷脊条目："
                    "扩纲批必须同批携带 volumes 载荷签卷（plan extend 的 merge 语义，"
                    f"预算需满足 chapters_budget == ceil(word_budget/{chapter_target or '签约章幅'})）。"
                )
            return {
                "suggested_role": "creative",
                "reason_code": "volume_boundary_outline_review",
                "chapters": vol_boundaries,
                "next_volume_signed": not unsigned,
                "hint": hint,
            }
        return {
            "suggested_role": "creative",
            "reason_code": "book_outline_undeclared",
            "chapters": vol_boundaries,
            "hint": (
                f"第 {first} 章起进入新卷，但未声明 book_outline：逐卷设计容易升级塌缩、套路重复、"
                "跨卷长线悬空。建议按创意编辑（creative）角色做跨卷设计并补全书大纲，"
                "由总编辑签发进 plan 后再按策划编辑角色编拍。"
            ),
        }

    # 2. 检测卷内阶段跃迁（同卷但 phase_id 发生跃迁）
    phase_boundaries = [
        ch
        for ch in sorted(chapters)
        if ch > 1 and ch in phase_of and (ch - 1) in phase_of
        and ch in volume_of and (ch - 1) in volume_of
        and volume_of[ch] == volume_of[ch - 1]
        and phase_of[ch] != phase_of[ch - 1]
    ]
    if phase_boundaries:
        first_p = phase_boundaries[0]
        curr_p = phase_of[first_p]
        prev_p = phase_of[first_p - 1]
        return {
            "suggested_role": "creative",
            "reason_code": "mid_volume_phase_transition",
            "chapters": phase_boundaries,
            "phase": curr_p,
            "previous_phase": prev_p,
            "hint": (
                f"第 {first_p} 章起进入卷内新阶段（阶段 {curr_p}，上一阶段为 {prev_p}）："
                "建议按创意编辑（creative）角色出具卷内阶段小纲（Phase Brief），"
                "明确本阶段核心矛盾、新对手/盟友生态与阶段小高潮，"
                "由总编辑签发后再按策划编辑（planning）角色细化章拍。"
            ),
        }

    return None


__all__ = [
    '_ASSEMBLY_ONLY_ISSUES',
    '_PLOT_SELF_CHECK',
    '_style_gate_enabled',
    '_polish_disabled',
    '_style_structure_args',
    '_log_quality',
    '_corpus_style_fingerprint',
    '_require_active',
    '_touch',
    '_book_words_written',
    '_plan_extend_through',
    '_volume_watermark',
    '_HEAD_TRANSITION_FIELDS',
    '_head_transition',
    '_preserve_rework_text',
    '_refresh_stage_views',
    'hooks_schedule_gaps',
    'overdue_hooks_undeclared',
    '_one_pass_expansion_hint',
    '_ack_hint',
    '_attach_prose_hash',
    '_unresolved_long_term_commitments',
    '_append_voice_concept',
    '_REWORK_EVENTS',
    '_creative_advisory',
]
