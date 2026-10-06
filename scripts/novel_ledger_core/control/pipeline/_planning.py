# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：planning
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from ...content.narrative_contract import (narrative_contract_view)
from ...ledger.ledger import (coerce_due, load_snapshot, read_events)
from ...content.pack import (SCENE_CHARS, scene_budget, select_character_profiles, word_targets)
from ...infra.store import (OUTLINE_MILESTONE_KINDS, PHASE_IDLE, BookStore, _volume_key_number, beats_count_warning, outline_budget_for_volume, validate_knowledge_refs, volume_entry_for)
from ...infra.scale import (COMPLETION_MIN_RATIO, ENDGAME_MIN_RATIO, derive_chapter_word_target, scale_contract_errors)
from ...infra.story_map import (plan_story_map_errors)
from ...infra.util import (LedgerError, atomic_json, canonical_json, ok, read_json, sha256_text)

from ._common import (
    _book_words_written,
    _require_active,
    hooks_schedule_gaps,
    overdue_hooks_undeclared,
)
from ._dispatch import (
    WORKER_PROMPT_PROTOCOL,
)

PLAN_CANDIDATES_SCHEMA = "novel-ledger.plan-candidates.v1"


# （实战中 batch_from/failure_mode/verdict.selected_id 曾各需 3–4 次试错）。
_CANDIDATES_SKELETON = (
    'minimal shape: {"schema":"novel-ledger.plan-candidates.v1","batch_from":21,"batch_to":21,'
    '"candidates":[{"id":"A","theme":"…","differentiator":"…","failure_mode":"…",'
    '"chapters":[{"chapter":21,"goal":"…","conflict":"…","outcome":"…","tags":["对峙"],'
    '"settles":[{"id":"h1"}]}]},'
    '{"id":"B","theme":"…","differentiator":"…","failure_mode":"…",'
    '"chapters":[{"chapter":21,"goal":"…","conflict":"…","outcome":"…","tags":["暗访"],"settles":[]}]}],'
    '"verdict":{"selected_id":"A","rationale":"…","losers":[{"id":"B","why":"…"}]}}'
    ' — every candidate must list the full batch range; references/extend-contract.md 批次候选与择优'
)


def _plan_extend_context(store: BookStore, payload: dict[str, Any]) -> dict[str, Any]:
    """Load current planning state and full selected evidence, with source indexes."""
    from ...content.hierarchical_memory import memory_for_pack
    from ...ledger.ledger import facts_for_pack, select_conditions, select_debts, select_items, select_knowledge, select_relations
    from ...content.pack import select_character_profiles

    plan = store.load_plan()
    snapshot = load_snapshot(store)
    chapters = [item for item in plan.get("chapters") or [] if isinstance(item, dict)]
    chapters.sort(key=lambda item: int(item.get("chapter") or 0))
    recent_raw = chapters[-3:]
    committed = max(int(store.read_head().get("last_committed_ch") or 0), int(snapshot.get("chapter") or 0))
    planned_hooks = [
        {
            "chapter": item.get("chapter"),
            "id": hook.get("id"),
            "status": hook.get("status"),
            "due": hook.get("due"),
        }
        for item in chapters
        if int(item.get("chapter") or 0) > committed
        for beat in (item.get("beats") or [])
        if isinstance(beat, dict)
        for hook in ((beat.get("effects") or {}).get("hooks") or [])
        if isinstance(hook, dict)
    ]
    next_ch = max(1, int(payload.get("suggest_from") or (int(payload.get("chapter") or 0) + 1)))

    def compact(value: Any, cap: int = 140) -> str:
        # cap is a compatibility argument; complete selected wording is retained.
        return str(value or "")

    recent = [
        {
            "chapter": item.get("chapter"),
            "volume": item.get("volume"),
            "phase_id": item.get("phase_id"),
            "story_stage": item.get("story_stage"),
            "title": compact(item.get("title"), 80),
            "goal": compact(item.get("goal"), 160),
            "location": compact(item.get("location"), 60),
            "present": [compact(name, 40) for name in (item.get("present") or [])],
            "beats": [
                {
                    "id": beat.get("id"),
                    "text": compact(beat.get("text"), 180),
                    "hooks": [
                        {"id": hook.get("id"), "status": hook.get("status"), "due": hook.get("due")}
                        for hook in ((beat.get("effects") or {}).get("hooks") or [])
                        if isinstance(hook, dict)
                    ],
                }
                if isinstance(beat, dict) else {"text": compact(beat, 180)}
                for beat in (item.get("beats") or [])
            ],
            "beats_omitted": 0,
        }
        for item in recent_raw
    ]
    # 批次候选头脑风暴的机评底座：近 20 章章型直方图（紧凑统计，不重复章拍正文），
    # 供候选自评对照重复度；比 recent_chapters（3 章全文投影）更长的窗口只给计数形态。
    pattern_raw = [item for item in chapters if int(item.get("chapter") or 0) <= committed][-20:]
    recent_patterns = [
        {
            "chapter": item.get("chapter"),
            "tags": [compact(tag, 24) for tag in (item.get("tags") or [])],
            "threads": [compact(ref, 32) for ref in (item.get("thread_refs") or [])],
            "settled": any(
                isinstance(hook, dict) and str(hook.get("status") or "") == "paid"
                for beat in (item.get("beats") or [])
                if isinstance(beat, dict)
                for hook in ((beat.get("effects") or {}).get("hooks") or [])
            ),
        }
        for item in pattern_raw
    ]
    phases = [item for item in plan.get("phases") or [] if isinstance(item, dict)]
    latest_phase = phases[-1] if phases else {}
    phase = {
        key: compact(latest_phase.get(key), 240)
        for key in ("id", "name", "story_stage", "objective", "climax", "entry_state", "exit_state", "tension_change")
        if latest_phase.get(key)
    }
    phase.update({
        "chapter_start": latest_phase.get("chapter_start"),
        "chapter_end": latest_phase.get("chapter_end"),
        "mainline_event_ref": latest_phase.get("mainline_event_ref"),
        "subplot_event_refs": (latest_phase.get("subplot_event_refs") or []),
        "timeline_event_refs": (latest_phase.get("timeline_event_refs") or []),
        "event_changes": {
            str(key): compact(value, 180)
            for key, value in (latest_phase.get("event_changes") or {}).items()
        },
    })
    outline = plan.get("book_outline") if isinstance(plan.get("book_outline"), dict) else {}
    acts = [item for item in outline.get("acts") or [] if isinstance(item, dict)][:5]
    act_names = [str(item.get("name") or "") for item in acts]
    current_stage = str(latest_phase.get("story_stage") or (recent_raw[-1].get("story_stage") if recent_raw else "") or "")
    stage_index = act_names.index(current_stage) if current_stage in act_names else 0
    focus_stages = set(act_names[stage_index : stage_index + 2])
    spine = outline.get("event_spine") if isinstance(outline.get("event_spine"), dict) else {}
    # 当前幕的张力曲线项：候选批次自评"张力是否单调递进"时对照的机读锚。
    tension_now: dict[str, Any] = {}
    for entry in spine.get("tension_curve") or []:
        if isinstance(entry, dict) and current_stage and str(entry.get("stage") or "") == current_stage:
            tension_now = {
                "stage": entry.get("stage"),
                "level": entry.get("level"),
                "mode": entry.get("mode"),
            }
            break

    def owner_view(owner: Any) -> dict[str, Any]:
        if not isinstance(owner, dict):
            return {}
        events = [
            {
                "id": event.get("id"),
                "stage": event.get("stage"),
                "event": compact(event.get("event"), 150),
                "change": compact(event.get("change"), 120),
                "deadline": compact(event.get("deadline"), 80),
                "consequence": compact(event.get("consequence"), 100),
            }
            for event in (owner.get("events") or [])
            if isinstance(event, dict) and (not focus_stages or str(event.get("stage") or "") in focus_stages)
        ]
        return {
            "id": owner.get("id"),
            "name": compact(owner.get("name"), 80),
            "kind": owner.get("kind"),
            "purpose": compact(owner.get("purpose"), 120),
            "open_stage": owner.get("open_stage"),
            "payoff_stage": owner.get("payoff_stage"),
            "start_state": compact(owner.get("start_state"), 120),
            "participants": [compact(name, 40) for name in (owner.get("participants") or [])],
            "events": events,
        }

    spine_view = {
        "focus_stages": sorted(focus_stages),
        "mainline": owner_view(spine.get("mainline")),
        "subplots": [owner_view(item) for item in (spine.get("subplots") or [])],
        "timelines": [owner_view(item) for item in (spine.get("timelines") or [])],
        "tension_curve": [
            {key: compact(item.get(key), 120) for key in ("stage", "level", "mode", "pressure", "turn", "payoff") if item.get(key) is not None}
            for item in (spine.get("tension_curve") or [])
            if isinstance(item, dict) and (not focus_stages or str(item.get("stage") or "") in focus_stages)
        ],
    }
    names = list(dict.fromkeys([
        str(plan.get("protagonist") or "").strip(),
        *(str(name).strip() for item in recent_raw for name in (item.get("present") or [])),
        *(str(name).strip() for owner in [spine_view["mainline"], *spine_view["subplots"]] for name in owner.get("participants") or []),
    ]))
    names = [name for name in names if name]
    shown_names = names
    entities = snapshot.get("entities") or {}
    character_states = [
        {
            "name": name,
            "location": compact((entities.get(name) or {}).get("location"), 60),
            "dead": bool((entities.get(name) or {}).get("dead")),
            "facts": [compact(fact, 160) for fact in facts_for_pack((entities.get(name) or {}).get("facts"), cap=2, who=name)],
        }
        for name in shown_names
        if isinstance(entities.get(name), dict)
    ]
    character_profiles = {
        name: {key: compact(value, 150) for key, value in record.items()}
        for name, record in select_character_profiles(plan.get("character_profiles"), shown_names).items()
    }
    recent_topics = list(dict.fromkeys(
        str(topic).strip()
        for item in recent_raw
        for topic in (item.get("knowledge_refs") or [])
        if str(topic).strip()
    ))
    location = str(recent_raw[-1].get("location") or "") if recent_raw else ""
    debts, debts_omitted = select_debts(store, location=location, present=shown_names, cap=8, snapshot=snapshot)
    relations, relations_omitted = select_relations(store, names=shown_names, cap=8, snapshot=snapshot)
    items, items_omitted = select_items(store, names=shown_names, cap=8, snapshot=snapshot)
    conditions, conditions_omitted = select_conditions(store, names=shown_names, cap=12, snapshot=snapshot)
    # due 统一走 coerce_due（宽容归一）：isdigit+int 的双重转换是同一逻辑写两遍
    due_ranked = []
    for hook in snapshot.get("hooks") or []:
        if not isinstance(hook, dict):
            continue
        if str(hook.get("status") or "open") in ("paid", "closed", "abandoned"):
            continue
        due = coerce_due(hook.get("due")) or 0
        if 0 < due <= next_ch + 19:
            due_ranked.append((abs(due - next_ch), due, hook))
    due_ranked.sort(key=lambda item: (item[0], item[1]))
    hooks = [hook for _, _, hook in due_ranked]
    due_hooks = [
        {"id": hook.get("id"), "text": compact(hook.get("text"), 140), "due": hook.get("due")}
        for hook in hooks[:24]
    ]
    all_undated = [
        hook for hook in snapshot.get("hooks") or []
        if isinstance(hook, dict)
        and str(hook.get("status") or "open") not in ("paid", "closed", "abandoned")
        and not hook.get("due")
    ]
    undated_hooks = [
        {"id": hook.get("id"), "text": compact(hook.get("text"), 140)}
        for hook in all_undated[-4:]
    ]
    volume_number = _volume_key_number(recent_raw[-1].get("volume")) if recent_raw else 1
    volumes = plan.get("volumes") or {}
    from ...infra.volume_outline import volume_outline_view
    whole_book_volumes = volume_outline_view(plan)
    volume_view = {
        str(number): dict(volume_entry_for(volumes, number) or {})
        for number in (volume_number or 1, (volume_number or 1) + 1)
        if volume_entry_for(volumes, number)
    }
    # 书级长线承诺只向策划投影当前/下一卷仍待处理的少数条目；逐章写者不看
    # 最终答案。ledger_hook 给出首见证据和当前解释，events 给出最近两次显形，
    # 让几百章后的回收仍可检查因果，而不必重读旧正文或整张账本。
    raw_commitments = [
        item for item in (outline.get("long_term_commitments") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    hook_index = {
        str(item.get("id")): item
        for item in snapshot.get("hooks") or []
        if isinstance(item, dict) and item.get("id")
    }
    phase_event_ids = {
        str(latest_phase.get("mainline_event_ref") or ""),
        *(str(ref) for ref in (latest_phase.get("subplot_event_refs") or [])),
    }
    phase_arc_ids = {
        str(owner.get("id"))
        for owner in [spine.get("mainline"), *(spine.get("subplots") or [])]
        if isinstance(owner, dict)
        and any(str(event.get("id")) in phase_event_ids for event in (owner.get("events") or []) if isinstance(event, dict))
    }

    def commitment_volume(item: dict[str, Any], key: str, default: int) -> int:
        try:
            return int(item.get(key) or default)
        except (TypeError, ValueError):
            return default

    active_commitments = [
        item for item in raw_commitments
        if commitment_volume(item, "planted_volume", 1) <= (volume_number or 1) + 1
        and (
            commitment_volume(item, "resolved_volume", 999999) >= (volume_number or 1)
            or str((hook_index.get(str(item.get("id"))) or {}).get("status") or "open") not in ("paid", "closed")
        )
    ]
    active_commitments.sort(key=lambda item: (
        0 if str(item.get("arc_ref") or "") in phase_arc_ids else 1,
        abs(commitment_volume(item, "resolved_volume", 999999) - (volume_number or 1)),
        commitment_volume(item, "planted_volume", 1),
        str(item.get("id")),
    ))
    selected_commitments = active_commitments[:6]
    selected_ids = {str(item["id"]) for item in selected_commitments}
    touch_history: dict[str, dict[str, Any]] = {
        ident: {"count": 0, "first": None, "recent": []} for ident in selected_ids
    }
    if selected_ids:
        for event in read_events(store):
            if event.get("type") == "governance":
                continue
            for hook in (event.get("state_delta") or {}).get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                ident = str(hook.get("id") or "")
                if ident not in touch_history:
                    continue
                info = touch_history[ident]
                touch = {
                    "chapter": event.get("chapter"),
                    "text": compact(hook.get("text"), 120),
                    "status": hook.get("status") or "open",
                    "quote": compact(hook.get("quote"), 100),
                }
                info["count"] += 1
                if info["first"] is None:
                    info["first"] = touch
                info["recent"].append(touch)
                info["recent"] = info["recent"][-2:]
    commitment_view = []
    for item in selected_commitments:
        ident = str(item["id"])
        hook = hook_index.get(ident) or {}
        history = touch_history[ident]
        first = history["first"] or {}
        commitment_view.append({
            "id": ident,
            "promise": compact(item.get("promise"), 160),
            "planted_volume": item.get("planted_volume"),
            "resolved_volume": item.get("resolved_volume"),
            "arc_ref": item.get("arc_ref"),
            "seed_use": compact(item.get("seed_use"), 120),
            "final_condition": compact(item.get("final_condition"), 120),
            "description": compact(item.get("description"), 120),
            "overdue_volume": commitment_volume(item, "resolved_volume", 999999) < (volume_number or 1)
                and str(hook.get("status") or "open") not in ("paid", "closed"),
            "ledger_hook": {
                "status": hook.get("status") or "unplanted",
                "opened_chapter": hook.get("opened_chapter") or first.get("chapter"),
                "seed_text": compact(hook.get("seed_text") or first.get("text"), 120),
                "seed_quote": compact(hook.get("seed_quote") or first.get("quote"), 100),
                "updated_chapter": hook.get("updated_chapter"),
                "text": compact(hook.get("text"), 120),
                "due": hook.get("due"),
                "touch_count": history["count"],
                "recent_touches": history["recent"],
            },
        })
    # 同一长线 id 若也被用作认知话题，给数百章前的知情版本定向优先级。
    knowledge_priority = list(dict.fromkeys([
        *recent_topics, *(str(item["id"]) for item in selected_commitments),
    ]))
    character_knowledge, knowledge_omitted = select_knowledge(
        snapshot=snapshot, names=shown_names, topic_ids=knowledge_priority, cap=16,
    )
    knowledge_view = [
        {
            "who": entry.get("who"),
            "topic_id": entry.get("topic_id"),
            "claim": compact(entry.get("claim"), 160),
            "stance": entry.get("stance"),
            "source": compact(entry.get("source"), 100),
            "first_recorded_chapter": entry.get("first_recorded_chapter"),
            "updated_chapter": entry.get("updated_chapter"),
            "quote": compact(entry.get("quote"), 100),
        }
        for entry in character_knowledge
    ]
    long_question = outline.get("long_arc_question") if isinstance(outline.get("long_arc_question"), dict) else {}
    long_question_view = {
        "core_mystery": compact(long_question.get("core_mystery"), 160),
        "surface_illusion": compact(long_question.get("surface_illusion"), 160),
        "decryption_ladder": [
            {"stage": compact(step.get("stage"), 60), "truth": compact(step.get("truth"), 140)}
            for step in (long_question.get("decryption_ladder") or [])
            if isinstance(step, dict) and (not focus_stages or str(step.get("stage") or "") in focus_stages)
        ],
    } if long_question else None
    book_scale = {key: outline.get(key) for key in ("book_words", "chapter_words_target", "total_chapters") if outline.get(key) is not None}
    budget_basis = outline.get("chapter_rebudget")
    if isinstance(budget_basis, dict):
        book_scale["chapter_rebudget"] = {
            key: budget_basis.get(key)
            for key in ("schema", "book_words", "chapter_words_target", "signed_total_chapters", "through_chapter", "written_words", "total_chapters", "additional_chapters")
            if key in budget_basis
        }
        book_scale["chapter_rebudget"]["continuation_volume"] = compact(budget_basis.get("continuation_volume"), 60)
        shifts = [item for item in budget_basis.get("milestone_shifts") or [] if isinstance(item, dict)]
        book_scale["chapter_rebudget"]["milestone_shifts"] = [
            {key: item.get(key) for key in ("chapter_before", "chapter_after", "kind")}
            for item in shifts
        ]
        book_scale["chapter_rebudget"]["milestone_shifts_omitted"] = 0
    cfg = store.load_config()
    target = int(cfg.get("book_words") or 0)
    written = _book_words_written(store)
    acked = int(store.read_head().get("last_acked_ch") or 0)
    pace = derive_chapter_word_target(plan, cfg)
    total = int(outline.get("total_chapters") or 0)
    max_planned = max((int(item.get("chapter") or 0) for item in chapters), default=0)
    needed_total = acked + -(-(max(target - written, 0)) // pace) if pace else total
    book_scale.update({
        "book_words_written": written,
        "book_words_progress": round(written / target, 4) if target > 0 else 0.0,
        "rebudget_required": bool(total and max_planned >= total and acked > 0 and target > 0 and written / target < COMPLETION_MIN_RATIO and needed_total > total),
    })
    if book_scale["rebudget_required"]:
        book_scale["rebudget_hint"] = (
            "已规划到签约章数上限，但实际正文仍不足目标 90%；本次先运行 plan rebudget "
            "按已写字数重算剩余章数，再生成候选、select-batch 和 extend。保持作者目标字数与主线/红线不变。"
        )
    return {
        "schema": "novel-ledger.planning-context.v1",
        "narrative_contract": narrative_contract_view(store, plan=plan),
        "book_scale": book_scale,
        "acts": [{key: compact(act.get(key), 150) for key in ("name", "volumes", "arc", "stakes") if act.get(key)} for act in acts],
        "near_milestones": [
            {"chapter": item.get("chapter"), "kind": item.get("kind"), "note": compact(item.get("note") or item.get("description"), 140)}
            for item in (outline.get("milestones") or [])
            if isinstance(item, dict)
            and str(item.get("chapter") or "").isdigit()
            and next_ch - 5 <= int(item.get("chapter")) <= next_ch + 40
        ],
        "volumes": volume_view,
        "volume_outlines": whole_book_volumes,
        "available_sources": {"plan": str(store.plan_path), "snapshot": str(store.snapshot_path),
                              "load_volume": "plan volume-outline --volume N --project <project>",
                              "load_source": "context read --source intent|outline|canon --project <project>"},
        "latest_phase": phase,
        "event_spine": spine_view,
        "recent_chapters": recent,
        "memory": memory_for_pack(store, next_ch),
        "characters": character_states,
        "character_profiles": character_profiles,
        "knowledge": knowledge_view,
        "knowledge_refs": knowledge_priority,
        "relations": relations,
        "items": items,
        "conditions": conditions,
        "debts": debts,
        "due_hooks": due_hooks,
        "undated_hooks": undated_hooks,
        "planned_hooks": [hook for hook in planned_hooks if committed < int(hook.get("chapter") or 0) <= next_ch + 19],
        "long_term_commitments": commitment_view,
        "long_arc_question": long_question_view,
        "batch_design": {
            "recent_patterns": recent_patterns,
            "tension_now": tension_now,
            "design_directive": (
                "扩纲先做批次候选头脑风暴：产出至少 2 个结构性不同的批次走势候选"
                "（每候选=批次主题一句+每章一行『目标→冲突→后果』+章型分布+hook 兑现安排+"
                "声明差异轴+最坏失效模式），落盘 book/editorial/plan-batch-candidates-<起>-<止>.json，"
                "运行 plan select-batch 记录机器事实断言并把择优裁决封进账本，"
                "再把胜选候选展开为逐场 beats 跑 plan extend。细则见协议卡 §三。"
            ),
        },
        "omitted": {
            "characters": max(0, len(names) - len(shown_names)),
            "character_profiles": max(0, len(select_character_profiles(plan.get("character_profiles"), shown_names)) - len(character_profiles)),
            "knowledge": knowledge_omitted,
            "relations": relations_omitted,
            "items": items_omitted,
            "conditions": conditions_omitted,
            "debts": debts_omitted,
            "due_hooks": max(0, len(hooks) - len(due_hooks)),
            "undated_hooks": max(0, len(all_undated) - len(undated_hooks)),
            "planned_hooks": sum(1 for hook in planned_hooks if not committed < int(hook.get("chapter") or 0) <= next_ch + 19),
            "long_term_commitments": max(0, len(active_commitments) - len(commitment_view)),
            "long_arc_ladder": max(0, len(long_question.get("decryption_ladder") or []) - len((long_question_view or {}).get("decryption_ladder") or [])),
        },
    }


def _write_plan_extend_brief(
    store: BookStore,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """把当次 extend_plan 的机器载荷落盘成 plan worker 简报（指针纪律）。

    根因：扩纲职责写在派发会话的 prompt 模板里——写笔被旧上下文会话接手时，
    plan worker 拿到旧职责，新纪律（overdue_hooks 排进 effects.hooks、跨卷签卷）全部漏执行。
    落盘后职责真源=本文件+协议卡 §三（都是当前 skill 版本），派发 prompt 只需指路，
    旧会话上下文无法再腐蚀职责。
    """
    brief = {
        "schema": "novel-ledger.plan-brief.v1",
        "action": "extend_plan",
        "worker_protocol": WORKER_PROMPT_PROTOCOL,
        "chapter": payload.get("chapter"),
        "suggest_from": payload.get("suggest_from"),
        "extend_through_ch": payload.get("extend_through_ch"),
        "plan_extend_span": payload.get("plan_extend_span"),
        "remaining_in_plan": payload.get("remaining_in_plan"),
        "max_planned_ch": payload.get("max_planned_ch"),
        "overdue_hooks": payload.get("overdue_hooks"),
        "volume_watermark": payload.get("volume_watermark"),
        "planning_context": _plan_extend_context(store, payload),
        "protocol_card": str(Path(__file__).resolve().parents[4] / "agents" / "roles" / "worker-protocol.md"),
        "protocol_section": "三、扩纲执行循环（简报 protocol=plan）",
        "ts": int(time.time()),
    }
    scale = brief["planning_context"]["book_scale"]
    if scale.get("rebudget_required"):
        brief["budget_preflight"] = {"action": "plan rebudget", "required": True, "hint": scale["rebudget_hint"]}
    path = store.staging_dir / "plan-extend-brief.json"
    atomic_json(path, brief)
    return {
        "path": str(path),
        "protocol": "plan",
        "worker_protocol": WORKER_PROMPT_PROTOCOL,
        "note": (
            "spawn one disposable plan worker; its prompt only points at this brief file + "
            "the protocol card — duties live on disk, not in the host prompt"
        ),
    }


def plan_rebudget(store: BookStore, *, actor: str, reason: str) -> dict[str, Any]:
    """Extend chapter capacity from actual production without changing the book's words or spine."""
    import copy
    import math
    from ...ledger.ledger import append_governance_event

    if not str(actor or "").strip() or not str(reason or "").strip():
        raise LedgerError("invalid_actor", "actor and reason are required")
    head = store.read_head()
    _require_active(head)
    through = int(head.get("last_acked_ch") or 0)
    if head.get("phase") != PHASE_IDLE or through <= 0 or through != int(head.get("last_committed_ch") or 0):
        raise LedgerError("plan_rebudget_not_due", "rebudget only at an acknowledged chapter boundary")
    plan = store.load_plan()
    cfg = store.load_config()
    outline = plan.get("book_outline")
    pace = derive_chapter_word_target(plan, cfg)
    if not isinstance(outline, dict) or not pace or not outline.get("total_chapters"):
        raise LedgerError("plan_rebudget_unsigned", "rebudget requires a signed word/chapter scale")
    target = int(cfg.get("book_words") or 0)
    written = _book_words_written(store)
    current_total = int(outline["total_chapters"])
    chapters = [c for c in plan.get("chapters") or [] if isinstance(c, dict)]
    max_planned = max((int(c.get("chapter") or 0) for c in chapters), default=0)
    required = max(current_total, through + math.ceil(max(target - written, 0) / pace))
    if max_planned < current_total or required <= current_total:
        return ok(action="plan_rebudget", changed=False, total_chapters=current_total,
                  hint="the signed chapter budget still has sufficient capacity")
    if target <= 0 or int(outline.get("book_words") or 0) != target:
        raise LedgerError("book_scale_contract_violation", "rebudget cannot change the signed word target")
    previous = outline.get("chapter_rebudget") or {}
    signed_total = int(previous.get("signed_total_chapters") or current_total)
    current_chapter = next((c for c in chapters if int(c.get("chapter") or 0) == through), None)
    if current_chapter is None:
        raise LedgerError("plan_rebudget_not_due", "committed chapter is absent from the signed plan")
    volumes = plan.get("volumes") or {}
    current_entry = volume_entry_for(volumes, current_chapter.get("volume", 1))
    continuation = str(previous.get("continuation_volume") or next(
        (key for key, value in volumes.items() if value is current_entry), ""))
    if not continuation or continuation not in volumes:
        raise LedgerError("plan_rebudget_unsigned", "rebudget requires the continuation volume's signed budget")
    volume_contract = plan.get("volume_outline_contract") or {}
    rounding_surplus = int(volume_contract.get("signed_volume_chapters") or signed_total) - int(volume_contract.get("signed_total_chapters") or signed_total)
    if (sum(int(v.get("word_budget") or 0) for v in volumes.values()) != target
            or sum(int(v.get("chapters_budget") or 0) for v in volumes.values()) != current_total + rounding_surplus):
        raise LedgerError("plan_rebudget_unsigned", "sign complete, consistent volume budgets before rebudgeting")
    before_fingerprint = sha256_text(canonical_json(plan).decode("utf-8"))
    candidate = copy.deepcopy(plan)
    shifts = []
    for milestone in candidate["book_outline"].get("milestones") or []:
        old = int(milestone.get("chapter") or 0)
        if old > through and current_total > through:
            new = through + math.ceil((old - through) * (required - through) / (current_total - through))
            if milestone.get("kind") == "book_climax":
                new = max(new, math.ceil(required * ENDGAME_MIN_RATIO))
            if new != old:
                shifts.append({"chapter_before": old, "chapter_after": new, "kind": milestone.get("kind")})
                milestone["chapter"] = new
    additional = required - signed_total
    candidate["book_outline"]["total_chapters"] = required
    candidate["book_outline"]["chapter_rebudget"] = {
        "schema": "novel-ledger.chapter-rebudget.v1", "book_words": target,
        "chapter_words_target": pace, "signed_total_chapters": signed_total,
        "through_chapter": through, "written_words": written, "total_chapters": required,
        "additional_chapters": additional, "continuation_volume": continuation,
        "milestone_shifts": shifts, "actor": actor, "reason": reason,
    }
    old_additional = int(previous.get("additional_chapters") or 0)
    candidate["volumes"][continuation]["chapters_budget"] += additional - old_additional
    errors = scale_contract_errors(candidate, cfg, written_words=written) + plan_story_map_errors(candidate)
    if errors:
        raise LedgerError("book_scale_contract_violation", "rebudget does not preserve the signed contracts", {"errors": errors})
    event = append_governance_event(store, action="plan.rebudget", actor=actor, reason=reason,
                                   fields={"basis": candidate["book_outline"]["chapter_rebudget"],
                                           "previous_total_chapters": current_total})
    store.save_plan(candidate)
    after_fingerprint = sha256_text(canonical_json(candidate).decode("utf-8"))
    return ok(action="plan_rebudget", changed=True, previous_total_chapters=current_total,
              total_chapters=required, additional_chapters=additional, through_chapter=through,
              written_words=written, milestone_shifts=shifts, governance_hash=event["hash"],
              plan_fingerprint=after_fingerprint)


def plan_patch_chapter(
    store: BookStore,
    *,
    chapter: int,
    patch: dict[str, Any],
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """计划层定点修补通道：只改**未提交章**的 beats/location/present，治理事件留痕。

    背景：expected 与提交闸门可能构成计划层死锁（如同章同 (who,topic) 的重复
    knowledge——提交端禁止第二条、expected 又要求逐条回填，任何提交都无法通过），
    而 extend 只能向后追加、rework-patch 只动正文——过去只能手改计划真源。
    本通道把这类修补变成机器校验、审计留痕的正规操作：

    - 只允许未提交章（chapter > last_committed）：已提交章是历史事实，回改破坏账本；
    - 只允许 beats/location/present 三个字段：goal/conflict/outcome/tags 被批次候选
      封存绑定，动了等于改约，走候选重选；
    - beats 整组替换并过 plan_shape_reject 全量形状闸（稳定键/must 锚词/同章
      knowledge 唯一——死锁类缺陷在落盘前被点名）；
    - 覆盖批次封存的章会同步刷新该批的 chapters_hash（封存语义=候选绑定字段不变，
      拍级修订经治理事件审计后反映进证明）。
    """
    if not str(actor or "").strip() or not str(reason or "").strip():
        raise LedgerError("invalid_args", "plan patch-chapter requires actor and reason")
    allowed_fields = {"beats", "location", "present"}
    if not isinstance(patch, dict) or not patch:
        raise LedgerError("invalid_args", "patch must be a non-empty JSON object")
    unknown = sorted(set(map(str, patch)) - allowed_fields)
    if unknown:
        raise LedgerError(
            "plan_patch_field_forbidden",
            "patch may only modify beats/location/present; candidate-bound or identity fields need the candidate/contract channels",
            {"unknown_fields": unknown, "allowed": sorted(allowed_fields)},
        )
    head = store.read_head()
    committed = int(head.get("last_committed_ch") or 0)
    if chapter <= committed:
        raise LedgerError(
            "chapter_already_committed",
            "plan patch-chapter only applies to uncommitted chapters; committed beats are history",
            {"chapter": chapter, "last_committed_ch": committed},
        )
    plan = store.load_plan()
    index = next(
        (i for i, item in enumerate(plan.get("chapters") or []) if int(item.get("chapter") or 0) == chapter),
        None,
    )
    if index is None:
        raise LedgerError("missing_chapter_plan", f"plan has no chapter {chapter}", {"chapter": chapter})
    if "beats" in patch:
        beats = patch["beats"]
        if not isinstance(beats, list) or not beats:
            raise LedgerError("missing_beats", f"chapter {chapter} patch requires a non-empty beats list")
    if "present" in patch and not isinstance(patch["present"], list):
        raise LedgerError("invalid_plan", "present must be a list of names")
    if "location" in patch and not str(patch.get("location") or "").strip():
        raise LedgerError("invalid_plan", "location must be a non-empty string")

    from ...infra.planning import rows_hash
    from ...infra.story_map import plan_shape_reject

    chapters = [dict(item) for item in plan.get("chapters") or []]
    before = chapters[index]
    beats_count_before = len(before.get("beats") or [])
    patched = dict(before)
    patched.update({k: v for k, v in patch.items()})
    chapters[index] = patched
    plan_shape_reject([patched], origin="plan patch-chapter")
    candidate = dict(plan)
    candidate["chapters"] = chapters
    story_errors = plan_story_map_errors(candidate)
    if story_errors:
        raise LedgerError(
            "event_spine_contract_violation",
            "plan patch breaks the signed event spine (thread/clock/phase references)",
            {"errors": story_errors[:5]},
        )
    refreshed_batches: list[int] = []
    batches = candidate.get("expansion_batches")
    if isinstance(batches, list):
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            batch_from = int(batch.get("batch_from") or 0)
            batch_to = int(batch.get("batch_to") or 0)
            if not (batch_from <= chapter <= batch_to):
                continue
            batch_chapters = [c for c in chapters if batch_from <= int(c.get("chapter") or 0) <= batch_to]
            if batch_chapters:
                batch["chapters_hash"] = rows_hash(batch_chapters)
                refreshed_batches.append(chapter)
    from ...ledger.ledger import append_governance_event

    event = append_governance_event(
        store,
        action="plan.chapter_patch",
        actor=actor,
        reason=reason,
        fields={
            "target_chapter": chapter,
            "changed_fields": sorted(map(str, patch)),
            "beats_count_before": beats_count_before,
            "beats_count_after": len(patched.get("beats") or []),
            "expansion_hash_refreshed": bool(refreshed_batches),
        },
    )
    store.save_plan(candidate)
    return ok(
        action="plan_chapter_patch",
        chapter=chapter,
        changed_fields=sorted(map(str, patch)),
        expansion_hash_refreshed=bool(refreshed_batches),
        governance_hash=event["hash"],
        hint="run chapter next to regenerate this chapter's pack/brief from the patched beats",
    )


def _candidate_fingerprint(candidate: dict[str, Any]) -> tuple:
    """同皮守卫的判据：章型序列 + hook 兑现安排完全相同的候选视作同一个。

    保守判定（宁漏勿误伤）：tags 与兑现 id 逐章完全一致才判同皮——只换措辞不换
    结构的假候选会被拦，真正的方向差异不会被拦。
    """
    chapters = [
        item for item in candidate.get("chapters") or [] if isinstance(item, dict)
    ]
    return tuple(
        (
            tuple(str(tag) for tag in (item.get("tags") or [])),
            tuple(sorted(str(hook.get("id")) for hook in item.get("settles") or [] if isinstance(hook, dict))),
        )
        for item in chapters
    )


def select_batch(
    store: BookStore,
    *,
    path: str,
    actor: str = "unspecified",
    reason: str = "",
) -> dict[str, Any]:
    """校验批次候选工件、写回机器事实断言，并把择优裁决封进账本治理事件。

    头脑风暴的"选择最优解"必须可审计：候选全档在 editorial 永久工件里，账本
    通过 plan.batch_select 治理事件封印"已择优"这一事实（哈希链可验）；重放
    不依赖工件文件本身。CLI 拒绝码是走过场的机器底线：候选不足、负项/败因
    缺失、同皮候选、胜选 id 未知都拒绝记录。
    """
    from ...ledger.ledger import append_governance_event

    file_path = Path(path)
    try:
        relative = file_path.resolve().relative_to(Path(str(store.book)))
    except ValueError:
        pass
    else:
        file_path = store.book / relative
    if not file_path.is_file() and Path(path).is_file():
        file_path = Path(path)  # Explicit candidate submission is an input boundary.
    if not file_path.is_file():
        raise LedgerError("invalid_args", f"candidates file not found: {path}")
    doc = read_json(file_path)
    if not isinstance(doc, dict) or doc.get("schema") != PLAN_CANDIDATES_SCHEMA:
        raise LedgerError(
            "invalid_plan", f"candidates file schema must be {PLAN_CANDIDATES_SCHEMA}"
        )
    if any(isinstance(doc.get(key), bool) or not isinstance(doc.get(key), int) for key in ("batch_from", "batch_to")):
        raise LedgerError("invalid_plan", "candidates file requires integer batch_from/batch_to")
    batch_from = doc["batch_from"]
    batch_to = doc["batch_to"]
    if batch_from <= 0 or batch_to < batch_from:
        raise LedgerError("invalid_plan", "candidates batch range must be positive and ordered")
    from ...infra.planning import MAX_BATCH_CHAPTERS, candidate_rows, rows_hash
    if batch_to - batch_from + 1 > MAX_BATCH_CHAPTERS:
        raise LedgerError("plan_candidates_batch_too_long", "one candidate batch must match one phase of at most 20 chapters")

    candidates = [c for c in doc.get("candidates") or [] if isinstance(c, dict)]
    if len(candidates) < 2:
        raise LedgerError(
            "plan_candidates_too_few",
            "batch brainstorm requires at least 2 structurally different candidates",
            _CANDIDATES_SKELETON,
        )
    by_id: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        cid = str(cand.get("id") or "").strip()
        if not cid:
            raise LedgerError("invalid_plan", "every candidate needs a non-empty id")
        if cid in by_id:
            raise LedgerError("invalid_plan", f"duplicate candidate id {cid!r}")
        if not str(cand.get("differentiator") or "").strip():
            raise LedgerError(
                "invalid_plan", f"candidate {cid!r} needs a declared differentiator axis"
            )
        if not str(cand.get("failure_mode") or "").strip():
            raise LedgerError(
                "plan_candidates_missing_failure_mode",
                f"candidate {cid!r} needs its worst failure mode (anti-sycophancy)",
                _CANDIDATES_SKELETON,
            )
        if not isinstance(cand.get("chapters"), list) or not cand.get("chapters"):
            raise LedgerError(
                "invalid_plan", f"candidate {cid!r} needs a per-chapter outline skeleton"
            )
        candidate_rows(cand, batch_from, batch_to)
        by_id[cid] = cand
    if len({_candidate_fingerprint(cand) for cand in candidates}) < len(candidates):
        raise LedgerError(
            "plan_candidates_same_skin",
            "candidates share the same chapter-type sequence and settlement plan; "
            "vary a differentiator axis instead of rewording one direction",
        )

    verdict = doc.get("verdict") if isinstance(doc.get("verdict"), dict) else {}
    selected_id = str(verdict.get("selected_id") or "").strip()
    rationale = str(verdict.get("rationale") or "").strip()
    if not selected_id:
        raise LedgerError("invalid_args", "verdict.selected_id is required")
    if selected_id not in by_id:
        raise LedgerError(
            "plan_candidates_selected_unknown",
            f"selected_id {selected_id!r} is not among the candidates",
            _CANDIDATES_SKELETON,
        )
    if not rationale:
        raise LedgerError("invalid_args", "verdict.rationale is required")
    losers_raw = verdict.get("losers") or []
    if not isinstance(losers_raw, list) or not losers_raw:
        raise LedgerError(
            "plan_candidates_missing_loser_reason",
            "verdict.losers must list every non-selected candidate with its loss reason",
            _CANDIDATES_SKELETON,
        )
    loser_ids: set[str] = set()
    for item in losers_raw:
        if not isinstance(item, dict):
            raise LedgerError("invalid_args", "verdict.losers entries must be objects")
        lid = str(item.get("id") or "").strip()
        why = str(item.get("why") or "").strip()
        if not lid or lid not in by_id or lid == selected_id:
            raise LedgerError("invalid_args", f"verdict loser id {lid!r} is invalid")
        if not why:
            raise LedgerError(
                "plan_candidates_missing_loser_reason",
                f"loser {lid!r} needs an explicit reason it lost (anti-sycophancy)",
            )
        loser_ids.add(lid)
    if loser_ids != set(by_id) - {selected_id}:
        raise LedgerError(
            "plan_candidates_missing_loser_reason",
            "every non-selected candidate needs an explicit loss reason",
        )
    # 显式豁免清单：胜选候选不结算某条批内到期 hook 时，必须逐条给 why——
    # 与 losers 的反谄媚纪律同构：机器不判价值，但「知道没安顿却沉默」不行。
    deferred_hooks_raw = verdict.get("deferred_hooks") or []
    if not isinstance(deferred_hooks_raw, list):
        raise LedgerError("invalid_args", "verdict.deferred_hooks must be a list of {id, why}")
    deferred_ids: set[str] = set()
    for item in deferred_hooks_raw:
        if not isinstance(item, dict):
            raise LedgerError("invalid_args", "verdict.deferred_hooks entries must be objects")
        hid = str(item.get("id") or "").strip()
        why = str(item.get("why") or "").strip()
        if not hid or not why:
            raise LedgerError(
                "invalid_args",
                "verdict.deferred_hooks entries need both id and why (why the selected candidate "
                "deliberately leaves this due hook unsettled in this batch)",
            )
        deferred_ids.add(hid)

    # 机器事实断言（只陈述事实，不打综合分——价值判断归模型裁决）：
    # ① hook 覆盖：due≤批末 的 open hooks 里，该候选兑现安排覆盖几条；
    # ② 重复距离：候选章型 tags 与近 20 已写章 tags 的重合率（越低越新颖）。
    snapshot = load_snapshot(store)
    committed = max(
        int(store.read_head().get("last_committed_ch") or 0), int(snapshot.get("chapter") or 0)
    )
    due_soon = [
        str(hook.get("id") or "")
        for hook in snapshot.get("hooks") or []
        if isinstance(hook, dict)
        and str(hook.get("status") or "open") == "open"
        and hook.get("due") is not None
        and committed < (coerce_due(hook.get("due")) or 0) <= batch_to
        and str(hook.get("id") or "")
    ]
    plan = store.load_plan()
    chapters = [item for item in plan.get("chapters") or [] if isinstance(item, dict)]
    recent_tags: list[str] = []
    for item in chapters:
        if committed >= int(item.get("chapter") or 0) > committed - 20:
            recent_tags.extend(str(tag) for tag in (item.get("tags") or [])[:2])

    machine_checks: dict[str, Any] = {}
    for cid, cand in by_id.items():
        settles = {
            str(hook.get("id") or "")
            for item in cand.get("chapters") or []
            if isinstance(item, dict)
            for hook in item.get("settles") or []
            if isinstance(hook, dict)
        }
        cand_tags = [
            str(tag)
            for item in cand.get("chapters") or []
            if isinstance(item, dict)
            for tag in (item.get("tags") or [])[:2]
        ]
        overlap = (
            sum(1 for tag in cand_tags if tag in recent_tags) / len(cand_tags)
            if cand_tags
            else 0.0
        )
        uncovered = sorted(hook_id for hook_id in due_soon if hook_id not in settles)
        machine_checks[cid] = {
            "hooks_due_in_batch": len(due_soon),
            "hook_settlement_missing": uncovered,
            "recent_tag_overlap": round(overlap, 2),
        }

    # 封存口硬拒：胜选候选必须安顿好每一条批内到期 hook。此前 hook_settlement_missing
    # 只是随记录留痕的事实，裁决可带洞封存——批中每撞一次「due 未排拍」就要改章拍
    # 再重封一次（实测 r2→r3→r4 三连封）。三条正路：候选 settles 补结算、封存前
    # `hooks defer` 改期、verdict.deferred_hooks 逐条带 why 豁免。
    selected_uncovered = list(
        (machine_checks.get(selected_id) or {}).get("hook_settlement_missing") or []
    )
    unexplained = [hid for hid in selected_uncovered if hid not in deferred_ids]
    if unexplained:
        raise LedgerError(
            "plan_hook_settlement_uncovered",
            "selected candidate leaves due-in-batch hooks unsettled without a waiver: settle them "
            "in the candidate's chapters[].settles, run `hooks defer` before sealing, or list them "
            "in verdict.deferred_hooks with an explicit why per hook",
            {"uncovered": unexplained, "deferred": sorted(deferred_ids)},
        )

    try:
        relative = Path(path).resolve().relative_to(Path(str(store.editorial_dir)))
    except ValueError:
        file_path = store.editorial_dir / f"plan-batch-candidates-{batch_from}-{batch_to}.json"
    else:
        file_path = store.editorial_dir / relative
    append_governance_event(
        store,
        action="plan.batch_select",
        actor=actor,
        reason=reason or f"batch {batch_from}-{batch_to}: selected candidate {selected_id}",
        fields={
            "batch_from": batch_from,
            "batch_to": batch_to,
            "selected_id": selected_id,
            "selected_chapters": candidate_rows(by_id[selected_id], batch_from, batch_to),
            "candidate_hash": rows_hash(candidate_rows(by_id[selected_id], batch_from, batch_to)),
            "candidates_path": str(file_path.resolve()),
            "rationale": rationale,
            "deferred_hooks": deferred_hooks_raw,
            "losers": [
                {"id": str(item.get("id")), "why": str(item.get("why"))}
                for item in losers_raw
                if isinstance(item, dict)
            ],
        },
    )

    doc["machine_checks"] = machine_checks
    doc["machine_checks_ts"] = int(time.time())
    doc.setdefault("verdict", {})
    doc["verdict"]["selected_id"] = selected_id
    doc["verdict"]["rationale"] = rationale
    doc["verdict"]["losers"] = [
        {"id": str(item.get("id")), "why": str(item.get("why"))}
        for item in losers_raw
        if isinstance(item, dict)
    ]
    doc["recorded"] = True
    atomic_json(file_path, doc)
    return ok(
        action="plan_batch_selected",
        batch_from=batch_from,
        batch_to=batch_to,
        selected_id=selected_id,
        candidates_path=str(file_path),
        machine_checks=machine_checks,
        hint=(
            "selection sealed as a governance event (ledger verify replays it); "
            "expand the winning candidate into per-scene beats and run plan extend"
        ),
    )


def _outline_volume_entry(volumes: Any, volume_key: Any) -> dict[str, Any] | None:
    """按卷号归一解析在 plan.volumes 里找一卷（兼容原始键/目录名/中文序号），找不到返回 None。"""
    return volume_entry_for(volumes, volume_key)


def _act_volume_refs(value: Any) -> list[tuple[Any, int]]:
    """解析 acts[].volumes 的两种合法形态，返回 (原始值, 卷号) 列表。

    list 型（["1","2-4"]）与字符串型（"2-4"——hatch/向导实际产出的形态）都认。
    解析不出卷号的片段记为 (raw, 0)，由调用方按"未知引用"告警。此前只处理 list 型，
    字符串型直接 continue，`book_outline_missing_volume` 对 hatch v3 项目是死检查
    。
    """
    items: list[Any] = []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str) and value.strip():
        items = [value]
    out: list[tuple[Any, int]] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        parts = text.split("-") if "-" in text else [text]
        numbers: list[int] = []
        for part in parts:
            n = _volume_key_number(part.strip())
            if n is None:
                numbers = []
                break
            numbers.append(n)
        if len(numbers) == 1:
            out.append((item, numbers[0]))
        elif len(numbers) == 2 and numbers[0] <= numbers[1]:
            out.extend((item, n) for n in range(numbers[0], numbers[1] + 1))
        else:
            out.append((item, 0))
    return out


def _outline_warnings(store: BookStore, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """书级大纲（plan 顶层 `book_outline`）的叙事与引用软诊断。

    这里只返回 warning；字数、章数、卷预算与提前终局等规模硬错误由
    ``scale_contract_errors`` 单独处理。
    """
    outline = plan.get("book_outline")
    if outline is None:
        return []
    if not isinstance(outline, dict):
        return [{
            "code": "book_outline_invalid",
            "message": "book_outline must be an object; it is ignored until fixed",
        }]

    warnings: list[dict[str, Any]] = []
    cfg = store.load_config()
    volumes = plan.get("volumes")
    target = outline.get("book_words")
    try:
        target_int = int(target) if target is not None else None
    except (TypeError, ValueError):
        target_int = None
    if target_int is None:
        target_int = int(cfg.get("book_words") or 0) or None
    total_chapters = outline.get("total_chapters")
    try:
        total_chapters_int = int(total_chapters) if total_chapters is not None else None
    except (TypeError, ValueError):
        total_chapters_int = None

    # 1. acts[].volumes 指向的卷须在 plan.volumes 有 spine/goal（list 与 "2-4" 字符串形态都认）
    act_volume_keys: list[Any] = []
    acts = outline.get("acts")
    if isinstance(acts, list):
        for a_idx, act in enumerate(acts):
            if not isinstance(act, dict):
                continue
            for v, v_num in _act_volume_refs(act.get("volumes")):
                act_volume_keys.append(v)
                # 按展开后的卷号解析（原始片段可能是 "2-4" 这种区间写法）
                entry = _outline_volume_entry(volumes, v_num) if v_num > 0 else None
                has_spine = bool(entry and (str(entry.get("spine") or "").strip() or str(entry.get("goal") or "").strip()))
                if not has_spine:
                    warnings.append({
                        "code": "book_outline_missing_volume",
                        "act": a_idx,
                        "volume": v,
                        "message": f"book_outline.acts[{a_idx}] references volume {v!r} with no spine/goal in plan.volumes",
                    })

    # 1.5 卷合同覆盖率（左移告警）：终局措辞一旦出现在任何章拍文本里，scale 合同会要求
    #     Σ卷字预算 ≈ 全书目标（±10%）、Σ卷章预算 ≈ total_chapters（±20%）；等到写终局
    #     才炸就太晚了。卷表非空且目标可读时，缺额在此可见。
    if isinstance(volumes, dict) and volumes:
        sum_words = 0
        sum_chapters = 0
        for _key, item in volumes.items():
            if not isinstance(item, dict):
                continue
            try:
                sum_words += int(item.get("word_budget") or 0)
            except (TypeError, ValueError):
                pass
            try:
                sum_chapters += int(item.get("chapters_budget") or 0)
            except (TypeError, ValueError):
                pass
        if target_int is not None and 0 < sum_words < target_int:
            warnings.append({
                "code": "volume_contract_coverage_gap",
                "book_words": target_int,
                "sum_volume_budgets": sum_words,
                "coverage": round(sum_words / target_int, 4),
                "message": (
                    f"signed volume word budgets cover {sum_words}/{target_int} words; "
                    "sign the remaining volumes via plan extend before chapters reach them "
                    "(whole-book ending language hard-fails the scale contract until then)"
                ),
            })
        if total_chapters_int is not None and sum_chapters > 0 and sum_chapters != total_chapters_int:
            warnings.append({
                "code": "volume_contract_chapters_gap",
                "total_chapters": total_chapters_int,
                "sum_volume_chapter_budgets": sum_chapters,
                "message": (
                    f"signed volume chapter budgets sum to {sum_chapters}, "
                    f"but book_outline.total_chapters is {total_chapters_int}; "
                    "this mismatch becomes a hard error the moment any chapter uses whole-book ending language"
                ),
            })

    # 2. 卷预算之和 与 全书目标 一致性。只有被 act 引用的卷**全部**声明 word_budget 时才比对，
    #    避免"只声明了一半预算"这种正常过程产生噪声告警。
    budgets: list[int] | None = []
    for v in act_volume_keys:
        b = outline_budget_for_volume(volumes, v)
        if b is None:
            budgets = None
            break
        budgets.append(b)
    if budgets and target_int is not None:
        total = sum(budgets)
        if total != target_int:
            warnings.append({
                "code": "book_outline_budget_mismatch",
                "book_words": target_int,
                "sum_volume_budgets": total,
                "message": f"sum of volume word_budget ({total}) != book target ({target_int})",
            })
    cfg_words = int(cfg.get("book_words") or 0)
    if isinstance(target, int) and cfg_words > 0 and target != cfg_words:
        warnings.append({
            "code": "book_outline_budget_mismatch",
            "book_words": target,
            "config_book_words": cfg_words,
            "message": f"book_outline.book_words ({target}) != config.book_words ({cfg_words})",
        })

    # 3. milestones：kind 在枚举内、chapter 为正且不超 total_chapters（声明了才校验）
    milestones = outline.get("milestones")
    if not milestones:
        warnings.append({
            "code": "book_outline_character_arc_unspecified",
            "message": (
                "book_outline.milestones is empty: character development or a deliberately steadfast arc has no declared checkpoints; "
                "state the intended choices, costs and ending evidence in the author contract/outline before literary review. "
                "No fixed milestone count is required."
            ),
        })
    if isinstance(milestones, list):
        for m_idx, m in enumerate(milestones):
            if not isinstance(m, dict):
                warnings.append({
                    "code": "book_outline_milestone_out_of_range",
                    "milestone_index": m_idx,
                    "message": "milestone must be an object with a positive chapter",
                })
                continue
            kind = m.get("kind")
            if kind is not None and kind not in OUTLINE_MILESTONE_KINDS:
                warnings.append({
                    "code": "book_outline_bad_kind",
                    "milestone_index": m_idx,
                    "kind": kind,
                    "allowed": list(OUTLINE_MILESTONE_KINDS),
                    "message": f"milestone kind {kind!r} is not one of {list(OUTLINE_MILESTONE_KINDS)}",
                })
            try:
                ch = int(m.get("chapter"))
            except (TypeError, ValueError):
                warnings.append({
                    "code": "book_outline_milestone_out_of_range",
                    "milestone_index": m_idx,
                    "message": "milestone.chapter must be a positive integer",
                })
                continue
            if ch <= 0 or (total_chapters_int is not None and ch > total_chapters_int):
                warnings.append({
                    "code": "book_outline_milestone_out_of_range",
                    "milestone_index": m_idx,
                    "chapter": ch,
                    "total_chapters": total_chapters_int,
                    "message": f"milestone chapter {ch} is out of range (total_chapters={total_chapters_int})",
                })
    return warnings


def _outline_conformance(
    store: BookStore,
    plan: dict[str, Any],
    committed_chapters: int,
    total_words: int,
) -> dict[str, Any]:
    """`book audit` 的书级大纲符合度：进度、里程碑达成数、问题数。未声明则 `declared=False`。"""
    outline = plan.get("book_outline")
    if not isinstance(outline, dict):
        return {"declared": False}
    target = outline.get("book_words")
    try:
        target_int = int(target)
    except (TypeError, ValueError):
        target_int = int(store.load_config().get("book_words") or 0)
    milestones = outline.get("milestones")
    reached = 0
    if isinstance(milestones, list):
        for m in milestones:
            if not isinstance(m, dict):
                continue
            try:
                if int(m.get("chapter")) <= committed_chapters:
                    reached += 1
            except (TypeError, ValueError):
                continue
    issues = _outline_warnings(store, plan) + scale_contract_errors(
        plan,
        store.load_config(),
        written_words=total_words,
    ) + plan_story_map_errors(plan, required=store.hatch_manifest_path.exists(), volume_outline_required=store.load_config().get("volume_outline_schema") == "novel-ledger.volume-outlines.v1")
    return {
        "declared": True,
        "book_words": target_int,
        "book_words_written": total_words,
        "book_words_progress": round(total_words / target_int, 4) if target_int > 0 else 0.0,
        "total_chapters": outline.get("total_chapters"),
        "milestones_total": len(milestones) if isinstance(milestones, list) else 0,
        "milestones_reached": reached,
        "issue_count": len(issues),
        "issues": issues,
    }


def _quant_coverage_warnings(store: BookStore, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """口径覆盖软告警：项目声明的量化键（`config.quant_keys`）必须在 world_spine 里出现。

    `world_spine` 是每章 pack **必注入**的唯一通道，`kb_slice` 是按需检索、不保证命中；
    口径项只写在正典卡里而阶段视图不可见时，跨章数字就会各自发挥。
    本检查只做「声明了却没进必达通道」的提示，不判定数值对错；未声明 quant_keys 时恒空。
    """
    keys = [str(k).strip() for k in (store.load_config().get("quant_keys") or []) if str(k).strip()]
    if not keys:
        return []
    spine = str(plan.get("world_spine") or "")
    missing = [k for k in keys if k and k not in spine]
    if not missing:
        return []
    return [
        {
            "chapter": 0,
            "code": "quant_key_not_in_world_spine",
            "keys": missing,
            "message": (
                "以下量化口径键未出现在 plan.world_spine（每章 pack 必注入的通道），"
                "写者可能看不到口径而各自发挥：" + "、".join(missing)
            ),
        }
    ]


def validate_plan(store: BookStore) -> dict[str, Any]:
    """验证章拍质量：beats条数、must词合理性、location/present完整性。

    目标：在plan extend后或人工编辑章拍后，诊断可能影响KB切片和写作质量的问题。
    """
    from ...content.pack import _NEEDLE_STOP

    plan = store.load_plan()
    chapters = plan.get("chapters") or []

    from ...infra.volume_outline import volume_outline_warnings
    warnings: list[dict[str, Any]] = volume_outline_warnings(plan)
    errors: list[dict[str, Any]] = []
    raw_profiles = plan.get("character_profiles")
    if raw_profiles is not None:
        try:
            if not isinstance(raw_profiles, dict) or any(
                not isinstance(name, str) or not name.strip() for name in raw_profiles
            ):
                raise LedgerError("invalid_plan", "character_profiles must map non-empty canonical names to objects")
            select_character_profiles(raw_profiles, list(raw_profiles))
        except LedgerError as exc:
            errors.append({"chapter": 0, "code": "invalid_character_profiles", "message": exc.message})
    if not chapters:
        return ok(
            action="plan_validate",
            total_chapters=0,
            warnings=warnings,
            warning_count=len(warnings),
            errors=errors,
            error_count=len(errors),
            passed=not errors,
            hint="plan has no chapters to validate" if not errors else "plan contains invalid character profiles",
        )
    kb_ids = {str(card.get("id") or "") for card in store.load_kb()}
    # 场数建议带只对未写章生效（已写章拍数成历史事实，警告只剩噪音）。
    # 已写边界与 select_batch 同判据：HEAD 与账本快照取大——夹具/恢复路径直接
    # commit_event 时 HEAD 可能滞后，只看 HEAD 会把已写章当未写章重复报警。
    committed = 0
    try:
        committed = int(store.read_head().get("last_committed_ch") or 0)
    except LedgerError:
        pass
    try:
        committed = max(committed, int(load_snapshot(store).get("chapter") or 0))
    except LedgerError:
        pass
    unwritten_from = committed + 1
    # 字数带口径（与写作简报同源）：拍数折算覆盖不了目标字数的章，是 word_count_low
    # 返工的结构性源头——在编拍层亮出来，别等执笔拆场/补场硬凑。
    _word_band = store.load_config().get("word_band") or {}
    band_targets = (
        word_targets(_word_band)
        if isinstance(_word_band, dict)
        and int(_word_band.get("min") or 0) > 0
        and int(_word_band.get("max") or 0) > int(_word_band.get("min") or 0)
        else None
    )

    # must 词 × glossary 死锁校验：must 是机检硬门禁（终稿必须含该词），
    # glossary 键是被禁旧写法（终稿必须不含）——must 命中 glossary 键时
    # 同一个词既要出现又不能出现，章内无解。编拍时 must 须用规范术语。
    glossary = store.load_config().get("glossary") or {}
    if isinstance(glossary, dict) and glossary:
        for ch_data in chapters:
            ch_num = int(ch_data.get("chapter") or 0)
            for beat in ch_data.get("beats") or []:
                must = str(beat.get("must") or "").strip()
                if must and must in glossary:
                    warnings.append({
                        "chapter": ch_num,
                        "code": "must_word_is_banned_term",
                        "beat": str(beat.get("id") or ""),
                        "message": (
                            f"must 词「{must}」同时是 glossary 被禁旧写法（规范写法「{glossary[must]}」）："
                            "终稿必须含 must 又必须不含被禁词，章内无解。改 must 为规范写法。"
                        ),
                    })
    # effects 稳定键审计：缺键条目在组装提交时无法对账，必然 blocked 停线
    # （plan extend 落盘前已拒收新坏键；这里兜住历史存量与手工编辑）。
    from ...infra.story_map import plan_effects_key_issues

    for issue in plan_effects_key_issues(chapters):
        errors.append({
            "chapter": issue.get("chapter"),
            "code": "effects_entry_missing_keys",
            "message": (
                f"beat {issue.get('beat')} 的 effects.{issue.get('kind')}[{issue.get('index')}] "
                f"缺稳定键 {issue.get('missing_keys')}（现有键 {issue.get('entry_keys')}）："
                "组装对账永远失败，该章只能 blocked。改键后重跑 plan validate。"
            ),
        })

    # 验证每章
    for ch_data in chapters:
        ch_num = int(ch_data.get("chapter") or 0)
        if ch_num <= 0:
            errors.append({
                "chapter": ch_num,
                "code": "invalid_chapter_number",
                "message": "chapter number must be a positive integer",
            })
            continue

        beats = ch_data.get("beats") or []
        location = str(ch_data.get("location") or "").strip()
        present = ch_data.get("present") or []
        raw_kb_refs = ch_data.get("kb_refs")
        try:
            validate_knowledge_refs(ch_data.get("knowledge_refs"), chapter=ch_num)
        except LedgerError as exc:
            errors.append({
                "chapter": ch_num,
                "code": "invalid_knowledge_refs",
                "message": exc.message,
            })

        # 显式正典引用是关键规则的确定性通道：声明了就必须存在且装得进 pack。
        if raw_kb_refs is not None:
            if not isinstance(raw_kb_refs, list) or any(
                not isinstance(item, str) or not item.strip() for item in raw_kb_refs
            ):
                errors.append({
                    "chapter": ch_num,
                    "code": "invalid_kb_refs",
                    "message": "kb_refs must be a list of non-empty KB card id strings",
                })
            else:
                refs = [item.strip() for item in raw_kb_refs]
                duplicates = sorted({item for item in refs if refs.count(item) > 1})
                missing = [item for item in refs if item not in kb_ids]
                if duplicates:
                    errors.append({
                        "chapter": ch_num,
                        "code": "duplicate_kb_refs",
                        "refs": duplicates,
                        "message": "kb_refs contains duplicate card ids",
                    })
                if missing:
                    errors.append({
                        "chapter": ch_num,
                        "code": "missing_kb_refs",
                        "refs": missing,
                        "message": "kb_refs points to card ids missing from book/kb/cards.json",
                    })


        # 1. beats条数检查：与扩纲共用同一建议带（store.beats_count_warning，5–5）。
        # 旧实现残留 3–4 硬编码——按新纪律写 5 场的章反而被 validate 误报 off-band。
        # 只警未写章：已写章的拍数成历史事实，重排不可能，警告只剩噪音。
        beats_count = len(beats)
        if beats_count == 0:
            errors.append({
                "chapter": ch_num,
                "code": "missing_beats",
                "message": "chapter has no beats",
            })
        elif ch_num >= unwritten_from:
            band_warning = beats_count_warning(ch_num, beats)
            if band_warning:
                warnings.append(band_warning)
            if band_targets is not None:
                scenes_needed = scene_budget(band_targets["aim_chars"], floor=band_targets["min"])["scenes"]
                if beats_count < scenes_needed:
                    warnings.append({
                        "chapter": ch_num,
                        "code": "scene_count_below_budget",
                        "beats": beats_count,
                        "scenes_needed": scenes_needed,
                        "message": (
                            f"拍数 {beats_count} 场按每场约{SCENE_CHARS}字折算低于本章目标 "
                            f"{band_targets['aim_chars']} 汉字（需约 {scenes_needed} 场）：执笔只能靠"
                            "拆场/补场凑字数，是 word_count_low 返工的结构性源头。"
                            "编拍时排足场次，或显式下调 word_band。"
                        ),
                    })

        # 2. beats质量检查：功能清单式检测
        checklist_keywords = ["要", "必须", "需要", "应该", "得", "应当", "完成"]
        checklist_beats = []
        for idx, beat in enumerate(beats):
            beat_text = str(beat.get("text") if isinstance(beat, dict) else beat).strip()
            if not beat_text:
                warnings.append({
                    "chapter": ch_num,
                    "code": "empty_beat",
                    "beat_index": idx,
                    "message": f"beat[{idx}] is empty",
                })
                continue

            # 检测是否为功能清单式（大量"XX要YY"模式）
            keyword_count = sum(1 for kw in checklist_keywords if kw in beat_text)
            if keyword_count >= 2 and len(beat_text) < 30:
                checklist_beats.append(idx)

        if checklist_beats and len(checklist_beats) >= len(beats) * 0.75:
            warnings.append({
                "chapter": ch_num,
                "code": "checklist_style_beats",
                "message": "most beats seem checklist-style; beats should describe scenes (what happens on stage), not task lists",
                "affected_beats": checklist_beats,
            })

        # 3. must词合理性检查
        for idx, beat in enumerate(beats):
            if not isinstance(beat, dict):
                continue
            beat_text = str(beat.get("text") or "").strip()
            must_word = str(beat.get("must") or "").strip()

            if must_word:
                # must词应该在beat文本中出现
                if must_word not in beat_text:
                    warnings.append({
                        "chapter": ch_num,
                        "code": "must_not_in_beat",
                        "beat_index": idx,
                        "must": must_word,
                        "message": f"beat[{idx}] must word '{must_word}' does not appear in beat text",
                    })

                # must词不应该是停用词
                if must_word.lower() in _NEEDLE_STOP:
                    warnings.append({
                        "chapter": ch_num,
                        "code": "must_is_stopword",
                        "beat_index": idx,
                        "must": must_word,
                        "message": f"beat[{idx}] must word '{must_word}' is a common stopword; choose a more specific keyword",
                    })

        # 4. location/present完整性检查（影响KB切片和连续性）
        if not location:
            warnings.append({
                "chapter": ch_num,
                "code": "missing_location",
                "message": "chapter has no location; KB slice and debt scoring will degrade",
            })

        if not present or len(present) == 0:
            warnings.append({
                "chapter": ch_num,
                "code": "missing_present",
                "message": "chapter has no present characters; relation slice and continuity tracking will fail",
            })

    # 5. 书级大纲：叙事字段保留软告警；规模合同是硬错误，防止百万字目标被压成几十章。
    warnings.extend(_outline_warnings(store, plan))
    errors.extend(scale_contract_errors(plan, store.load_config(), written_words=_book_words_written(store)))
    errors.extend(plan_story_map_errors(plan, required=store.hatch_manifest_path.exists(), volume_outline_required=store.load_config().get("volume_outline_schema") == "novel-ledger.volume-outlines.v1"))
    warnings.extend(_quant_coverage_warnings(store, plan))

    # 6. due 对排期对账：due 抢在 effects.hooks 引用章之前的，提示 defer 对齐，
    # 消掉 C 章前每章一次的预检/自检噪音 WARNING。
    for gap in hooks_schedule_gaps(store):
        warnings.append(
            {
                "code": "hook_due_precedes_schedule",
                "id": gap["id"],
                "due": gap["due"],
                "scheduled_at": gap["scheduled_at"],
                "message": (
                    f"hook {gap['id']} due {gap['due']} but first scheduled (effects.hooks) at chapter "
                    f"{gap['scheduled_at']}: defer the due to match the schedule, or move the payoff earlier"
                ),
            }
        )
    # 7. 逾期未声明：已过 due 仍 open、且没有任何未写章 effects.hooks 引用——
    # 两条正路（声明兑现 / defer 改期）之外不允许静默。
    for item in overdue_hooks_undeclared(store):
        warnings.append(
            {
                "code": "overdue_hook_not_declared",
                "id": item["id"],
                "due": item["due"],
                "message": (
                    f"hook {item['id']} is overdue (due {item['due']}) with no effects.hooks reference "
                    "in any unwritten chapter: declare its payoff in beats or defer it explicitly"
                ),
            }
        )

    # 8. 批次候选留痕（advisory）：待写章区间缺少 plan.batch_select 治理事件 =
    # 该批扩纲没做候选择优。无人值守的硬闸在批级检查点（review_required），
    # 这里给巡检与 attended 场景一个离线可见信号。hatch 起手批（≤3）豁免；
    # 未初始化（无 HEAD/快照）的项目没有批次语义，直接跳过。
    try:
        committed_val = max(
            int(store.read_head().get("last_committed_ch") or 0),
            int((load_snapshot(store) or {}).get("chapter") or 0),
        )
    except LedgerError:
        committed_val = None
    if committed_val is not None:
        unwritten_nums = sorted(
            num for num in (int(item.get("chapter") or 0) for item in chapters)
            if num > committed_val
        )
        if unwritten_nums and unwritten_nums[0] > 3:
            first_unwritten = unwritten_nums[0]
            try:
                from ...content.planning import batch_selection_coverage
                covered = not batch_selection_coverage(store, after_chapter=committed_val)["issues"]
            except LedgerError:
                covered = False
            if not covered:
                warnings.append(
                    {
                        "code": "plan_batch_no_selection",
                        "from_chapter": first_unwritten,
                        "message": (
                            f"unwritten chapters from {first_unwritten} have no plan.batch_select "
                            "governance event: run the batch candidate brainstorm and record it with "
                            "plan select-batch before expanding beats"
                        ),
                    }
                )

    # 统计
    error_count = len(errors)
    warning_count = len(warnings)

    return ok(
        action="plan_validate",
        total_chapters=len(chapters),
        errors=errors,
        error_count=error_count,
        warnings=warnings,
        warning_count=warning_count,
        passed=error_count == 0,
        hint=(
            "plan validation passed with no errors"
            if error_count == 0
            else f"plan validation found {error_count} error(s) and {warning_count} warning(s)"
        ),
    )


__all__ = [
    'PLAN_CANDIDATES_SCHEMA',
    '_CANDIDATES_SKELETON',
    '_plan_extend_context',
    '_write_plan_extend_brief',
    'plan_rebudget',
    'plan_patch_chapter',
    '_candidate_fingerprint',
    'select_batch',
    '_outline_volume_entry',
    '_act_volume_refs',
    '_outline_warnings',
    '_outline_conformance',
    '_quant_coverage_warnings',
    'validate_plan',
]
