from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from ..ledger.ledger import (
    IRREVERSIBLE_CONDITIONS_PACK_MAX,
    facts_for_pack,
    get_character_continuity_records,
    load_snapshot,
    ledger_history_fingerprint,
    lifecycle_names,
    render_now_card,
    select_debts,
    select_hooks,
    select_knowledge,
    select_conditions,
    select_items,
    select_relations,
    state_near,
)
from ..infra.store import BookStore, CHARACTER_PROFILE_FIELDS, validate_kb_cards, validate_knowledge_refs
from ..infra.story_map import story_focus
from ..infra.util import LedgerError, canonical_json, pack_hash_digest, read_json, sha256_bytes, sha256_text
from .hierarchical_memory import memory_for_pack
from .recall import memory_for_chapter
from ..voice.anchor import build_voice_anchor
from ..voice.voice_manual import DEFAULT_VOICE_ID, load_content_manual, load_live_manual, load_writing_manual
from ..voice.voice_pack import content_for_pack, voice_for_pack

# 每个直接参演者都会得到人物卡和连续性档案。超过此数时显式停线，
# 不能悄悄截掉直接行动的人；围观人群无需逐个列在 chapter.present。
_PRESENT_CHARACTER_CAP = 16
_KNOWLEDGE_PACK_CAP = 16


def select_character_profiles(raw: Any, names: list[str]) -> dict[str, dict[str, str]]:
    """从书级人物卡只取本章在场者；作者层备注不随卡片泄露。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LedgerError("invalid_plan", "character_profiles must be an object keyed by canonical name")
    selected: dict[str, dict[str, str]] = {}
    for name in names:
        record = raw.get(name)
        if record is None:
            continue
        if not isinstance(record, dict):
            raise LedgerError("invalid_plan", f"character_profiles[{name!r}] must be an object")
        unknown = set(record) - CHARACTER_PROFILE_FIELDS
        if unknown:
            raise LedgerError("invalid_plan", f"unknown character profile fields for {name!r}: {sorted(unknown)}")
        profile: dict[str, str] = {}
        for field in (
            "background_register", "speech_habits", "under_pressure",
            "desire_and_mask", "sample_quote",
        ):
            if field not in record:
                continue
            value = record[field]
            if not isinstance(value, str):
                raise LedgerError(
                    "invalid_plan",
                    f"character_profiles[{name!r}].{field} must be a string",
                )
            if value.strip():
                profile[field] = value.strip()
        if profile:
            selected[name] = profile
    return selected

# 视角结构与平台尺度：manifest 可选字段，hatch 声明后随 plan 落盘、
# 每章随包渲染进 writing_brief 定位段。枚举与渲染住在这里（content 层），control/hatch.py
# 只做形状校验并复用——领域层不得反向依赖 control（test_infra_layer_is_not_bypassed）。
POV_STRUCTURES = ("single_limited", "dual_alternating", "ensemble")
POV_STRUCTURE_LABELS = {
    "single_limited": "单视角限知",
    "dual_alternating": "双主角交替",
    "ensemble": "多线群像",
}
POV_GRANULARITIES = ("chapter", "volume")
POV_GRANULARITY_LABELS = {"chapter": "章", "volume": "卷"}
FENCE_TIERS = ("strict", "standard", "split", "free")
FENCE_TIER_LABELS = {
    "strict": "严审平台安全线",
    "standard": "中等尺度",
    "split": "分区分层（正文安全线，高强度走支线/番外）",
    "free": "自由尺度（非平台发布）",
}


def render_narrative_pov(pov: Any) -> str:
    """把视角结构渲染成写手一句话纪律；非法形状返回空串（pack 侧静默省略）。"""
    if not isinstance(pov, dict):
        return ""
    structure = str(pov.get("structure") or "").strip()
    label = POV_STRUCTURE_LABELS.get(structure)
    if not label:
        return ""
    parts = [label]
    anchor = str(pov.get("anchor") or "").strip()
    if anchor:
        parts.append(f"叙述锚点是{anchor}")
    granularity = str(pov.get("switch_granularity") or "").strip()
    granularity_label = POV_GRANULARITY_LABELS.get(granularity)
    if structure == "single_limited":
        parts.append("读者所知不得超出视角人物所知")
    elif granularity_label:
        parts.append(f"视角切换只发生在{granularity_label}边界")
    return "本书视角结构为" + "；".join(parts)


def render_content_fence(fence: Any) -> str:
    """把平台尺度档位渲染成写手一句话约束；非法形状返回空串。"""
    if not isinstance(fence, dict):
        return ""
    tier = str(fence.get("tier") or "").strip()
    label = FENCE_TIER_LABELS.get(tier)
    if not label:
        return ""
    parts = [f"本书平台尺度档位为{label}"]
    notes = str(fence.get("notes") or "").strip()
    if notes:
        parts.append(notes)
    return "；".join(parts) + "；越档内容不得进入正文"


def _assert_glossary_consistent(
    glossary: dict[str, Any],
    *,
    beats: list[dict[str, Any]],
    volume_spine: str,
    world_spine: Any,
) -> None:
    """禁用串不得命中同一份工作包要求的词（must/拍点文本/卷脊/世界脊柱）。

    命中即装配失败：写手无法同时满足"must 是正文字串"与"禁用串不得出现"，
    卷脊同理（被禁的正是本卷要产出的东西）。glossary 的合法形状是
    「禁用旧写法 → 规范写法」，填成「词条 → 释义」就会撞上这道闸。
    """
    collisions: dict[str, list[str]] = {}
    for banned in glossary:
        banned = str(banned or "").strip()
        if not banned:
            continue
        where: list[str] = []
        for beat in beats:
            if banned in str(beat.get("must") or ""):
                where.append(f"beat[{beat.get('id') or '?'}].must")
            if banned in str(beat.get("text") or ""):
                where.append(f"beat[{beat.get('id') or '?'}].text")
        if banned in str(volume_spine or ""):
            where.append("volume_spine")
        if isinstance(world_spine, dict):
            spine_text = " ".join(str(v) for v in world_spine.values())
        else:
            spine_text = str(world_spine or "")
        if spine_text and banned in spine_text:
            where.append("world_spine")
        if where:
            collisions[banned] = sorted(set(where))
    if collisions:
        detail = "; ".join(f"'{term}' ← {','.join(spots)}" for term, spots in collisions.items())
        raise LedgerError(
            "glossary_self_conflict",
            "glossary bans terms that this writing pack itself requires: "
            + detail
            + ". glossary must be {禁用旧写法: 规范写法} pairs — a term→definition entry "
            "bans a canon word by mistake; fix config.glossary",
            {"collisions": collisions},
        )

_SPLIT_RE = re.compile(r"[\s,，。；;、：:！!？?/\\|（）()【】\[\]《》<>「」『』]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_KIND_RANK = {
    "character": 0,
    "geography": 1,
    "world": 2,
    "rule": 3,
    "power_system": 4,
}
_GENERIC_STRUCT = frozenset(
    {
        "world",
        "rule",
        "power_system",
        "geography",
        "character",
        "硬",
        "软",
    }
)
_NEEDLE_STOP = frozenset(
    {
        "一个",
        "这个",
        "那个",
        "没有",
        "还有",
        "不能",
        "不是",
        "只是",
        "可以",
        "已经",
        "然后",
        "以及",
        "或者",
        "因为",
        "所以",
        "自己",
        "什么",
        "我们",
        "他们",
        "它们",
        "本章",
        "必须",
        "立刻",
        "还在",
        "还没",
        "下来",
        "上来",
        "先落",
        "落到",
        "身上",
    }
)


def inputs_fingerprint(store: BookStore, chapter: int) -> str:
    """pack 装配所依赖的上游真源指纹（章拍 + 分层记忆 + KB + voice + caps + 阶段手册）。

    装配后任何上游被改都会使指纹失配 → 旧 pack 过期，写者必须按新 pack 重写。
    """
    plan = store.load_plan()
    memory_layers = memory_for_pack(store, chapter)
    profile = store.load_voice_profile()
    vid = str(profile.get("voice_id") or DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID
    # 总手册、润色手册和内容手册是 draft/polish 视图的上游真源；内置文风的
    # 总手册与润色手册是同一文件。它们曾不在指纹里：
    # 章中改手册不触发 stale 重建，重建后的视图其实已变。手册以内容摘要入指纹
    # （只参与 hash，不把全文塞进载荷）；voice profile 覆盖完整手册的解析产物。
    manual_digests = {
        layer: sha256_text(text)
        for layer, (_, text) in (
            ("live", load_live_manual(vid)),
            ("writing", load_writing_manual(vid)),
            ("content", load_content_manual(vid)),
        )
    }
    payload = {
        "chapter": chapter,
        "plan_chapter": store.chapter_plan(chapter),
        "plan_shared": {
            key: plan.get(key)
            for key in ("title", "volume_spine", "world_spine", "book_outline", "volumes", "character_profiles")
        },
        # 只指纹化目标章之前的有界记忆视图；commit 写入本章摘要后，
        # 本章 pack 仍应保持有效，崩溃重放才能抵达 ledger_replay_conflict。
        "hierarchical_memory": memory_layers,
        "ledger_history": ledger_history_fingerprint(store, chapter),
        "historical_recall": memory_for_chapter(store, chapter, store.chapter_plan(chapter),
            list(dict.fromkeys([str(plan.get("protagonist") or ""), *(store.chapter_plan(chapter).get("present") or [])]))),
        "kb": store.load_kb(),
        "voice": profile,
        "voice_manual_digests": manual_digests,
        # 节奏锚文件以内容哈希入指纹：参考文本被替换/修订 → 全部在途 pack 过期重建
        "voice_anchor_digest": _anchor_digest(store),
        "voice_anchor_selection": {key: store.load_config().get(key) for key in
                                   ("voice_anchor_slices", "voice_anchor_query", "voice_anchor_paragraphs")},
        "pack_caps": store.load_config().get("pack_caps") or {},
        "review_contract_version": store.load_config().get("review_contract_version", 2),
    }
    return "sha256:" + sha256_bytes(canonical_json(payload))


def _anchor_digest(store: BookStore) -> str:
    """config.voice_anchor_file 指向文件的内容指纹；未配置/不可读给占位（不炸指纹）。"""
    file_ref = str(store.load_config().get("voice_anchor_file") or "").strip()
    if not file_ref:
        return ""
    path = Path(file_ref)
    if not path.is_absolute():
        path = store.project / path
    try:
        path = store.book / path.resolve().relative_to(Path(str(store.book)))
        return "sha256:" + sha256_bytes(path.read_bytes())
    except (OSError, ValueError):
        return "unreadable"


def _bound_story_focus(focus: Any, *, cap: int) -> Any:
    """Compatibility entry point: story_focus is already selected by chapter refs.

    The former character budget no longer removes selected changes or touchpoints.
    """
    return copy.deepcopy(focus)


def assemble_pack(store: BookStore, chapter: int) -> dict[str, Any]:
    cfg = store.load_config()
    caps = cfg["pack_caps"]
    plan = store.load_plan()
    ch_plan = store.chapter_plan(chapter)
    beats = _normalize_beats(ch_plan.get("beats"))
    if not beats:
        raise LedgerError("missing_beats", f"chapter {chapter} has no beats in plan JSON")
    fact_focus = "；".join(
        str(part).strip()
        for beat in beats
        for part in (beat.get("must"), beat.get("text"))
        if str(part or "").strip()
    )
    expected_delta = collect_expected_delta(ch_plan.get("beats"))
    protagonist = (cfg.get("protagonist") or plan.get("protagonist") or "").strip()
    # 快照只加载一次，往下传给各选择函数（render_now_card/select_*/state_near/_present_cards），
    # 避免每章 pack 装配重复读盘解析 6 次同一 JSON。
    snapshot = load_snapshot(store)
    volume_meta = store.volume_meta(chapter)
    global_spine = str(plan.get("volume_spine") or "").strip()
    volume_spine = str(volume_meta.get("spine") or global_spine).strip()
    volume_goal = str(
        volume_meta.get("goal")
        or volume_meta.get("spine")
        or global_spine
    ).strip()
    now_card = render_now_card(
        store,
        protagonist=protagonist,
        chapter_plan=ch_plan,
        volume_spine=volume_goal,
        cap_chars=int(caps["now_card_chars"]),
        snapshot=snapshot,
        fact_focus=fact_focus,
    )
    profile = store.load_voice_profile() or {}
    vid = str(profile.get("voice_id") or DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID
    live_path, live_text = load_live_manual(vid)
    write_path, write_text = load_writing_manual(vid)
    content_path, content_text = load_content_manual(vid)
    voice = voice_for_pack(
        store.load_voice_concepts(),
        session_notes=store.load_voice_session_notes(),
        cap=int(caps["voice_concepts"]),
        profile=profile,
        skill_manual=live_path,
        manual_text=live_text,
    )
    content = content_for_pack(
        profile=profile,
        skill_manual=content_path,
        content_text=content_text,
    )
    # 文风单轨：概念的唯一来源是项目所选的 voices/<id>.md（`init` 自动写入，`voice apply` 刷新）。
    # 装配不出概念说明 voice.json 被清空或改坏了，宁可不开章，
    # 也不能让写者在没有文风的情况下写一章出来。
    if not voice["concepts"]:
        raise LedgerError(
            "missing_voice",
            "pack refused: 项目没有文风概念；跑 voice apply --project <PROJECT> "
            "重新从 references/voices/ 注入所选手册",
            {"voice_path": str(store.voice_memory_path)},
        )

    present = _present_names(ch_plan, protagonist)
    character_profiles = select_character_profiles(plan.get("character_profiles"), present)
    knowledge_refs = validate_knowledge_refs(ch_plan.get("knowledge_refs"), chapter=chapter)
    if knowledge_refs:
        referenced = [
            item for item in (snapshot.get("knowledge") or [])
            if isinstance(item, dict)
            and item.get("who") in present
            and item.get("topic_id") in knowledge_refs
        ]
    else:
        referenced = []
    knowledge, knowledge_omitted = select_knowledge(
        snapshot=snapshot, names=present, topic_ids=knowledge_refs,
        cap=max(_KNOWLEDGE_PACK_CAP, len(referenced)),
    )
    location = str(ch_plan.get("location") or now_card.get("location") or "")
    tags = [str(t) for t in (ch_plan.get("tags") or []) if str(t).strip()]
    raw_kb_refs = ch_plan.get("kb_refs")
    if raw_kb_refs is None:
        raw_kb_refs = []
    if not isinstance(raw_kb_refs, list):
        raise LedgerError("invalid_plan", f"chapter {chapter} kb_refs must be a list of card ids")
    if any(not isinstance(item, str) or not item.strip() for item in raw_kb_refs):
        raise LedgerError(
            "invalid_plan",
            f"chapter {chapter} kb_refs must contain only non-empty card-id strings",
        )
    kb_refs = [item.strip() for item in raw_kb_refs]
    kb_slice, kb_slice_meta = slice_kb(
        store.load_kb(),
        location=location,
        present=present,
        tags=tags,
        beats=beats,
        cap=int(caps["kb_slice"]),
        excerpt_chars=int(caps["kb_excerpt_chars"]),
        protagonist=protagonist,
        kb_refs=kb_refs,
    )
    near, near_omitted = near_summaries(store, chapter, cap=int(caps["near_summaries"]))
    debts, debts_omitted = select_debts(
        store,
        location=location,
        present=present,
        cap=int(caps["debts"]),
        snapshot=snapshot,
    )
    planned_hook_ids = [
        str(item.get("id"))
        for item in expected_delta.get("hooks") or []
        if isinstance(item, dict) and item.get("id")
    ]
    hooks, hooks_omitted = select_hooks(
        store, chapter=chapter, cap=int(caps["hooks"]), snapshot=snapshot,
        priority_ids=planned_hook_ids,
    )
    relations, relations_omitted = select_relations(
        store, names=present, cap=int(caps["relations"]), snapshot=snapshot
    )
    items, items_omitted = select_items(
        store, names=present, cap=int(caps.get("items", 8)), snapshot=snapshot
    )
    # 有界视野必须自报边界：所有切片被截断的条数汇总成一处，写者才知道自己看的不是全集。
    # 早前 12 个 cap 里只有 kb_slice / voice_concepts 会报省略，其余的写者无从得知。
    omitted: dict[str, int] = {}
    for label, count in (
        ("debts", debts_omitted),
        ("hooks", hooks_omitted),
        ("relations", relations_omitted),
        ("items", items_omitted),
        ("near_summaries", near_omitted),
        ("knowledge", knowledge_omitted),
    ):
        if count:
            omitted[label] = count
    present_cards = _present_cards(
        store, present, protagonist, snapshot, chapter, ch_plan,
        fact_focus=fact_focus, selected_relations=relations,
    )
    character_continuity = get_character_continuity_records(
        snapshot, protagonist, present, chapter,
        fact_focus=fact_focus, selected_relations=relations,
    )
    # 占用表只切本章相关地点（本章地点 + 在场者所在地），防止随全书实体数线性膨胀。
    occupancy_locations = _occupancy_locations(location, present_cards)
    state = state_near(
        store,
        pinned_names=present,
        pinned_cap=int(caps["state_pinned"]),
        recent_cap=int(caps["state_recent"]),
        occupancy_locations=occupancy_locations,
        occupancy_per_location=int(caps["occupancy_per_location"]),
        snapshot=snapshot,
        fact_focus=fact_focus,
    )
    recap = str(ch_plan.get("recap") or "").strip()
    prev_tail = previous_chapter_tail(store, chapter)
    glossary = cfg.get("glossary") or {}
    world_spine = str(plan.get("world_spine") or "").strip()
    memory_layers = memory_for_pack(store, chapter)
    chapter_story_focus = _bound_story_focus(
        story_focus(
            plan.get("book_outline"),
            ch_plan,
            plan.get("phases"),
            protagonist=str(plan.get("protagonist") or ""),
        ),
        cap=int(caps.get("story_focus_chars") or 0),
    )
    # 场景地图切片：本章会用到的地点卡（章地点/拍点文本/在场者所在地/占用表命中），
    # 最近使用优先补满；属性值摊平成字符串供简报渲染与冲突闸比较。
    location_cards = select_location_cards(
        snapshot.get("locations") or [],
        location=location,
        beats=beats,
        present_locations=[
            str((card or {}).get("location") or "").strip()
            for card in present_cards
        ],
        occupancy_locations=occupancy_locations,
        cap=int(caps.get("locations", 8)),
    )

    body: dict[str, Any] = {
        "schema": "novel-ledger.pack.v1",
        "chapter": chapter,
        "instruction": voice["instruction"],
        "voice_concepts": voice["concepts"],
        "now_card": now_card,
        "beats": beats,
        "kb_slice": kb_slice,
        "kb_slice_meta": kb_slice_meta,
        "state_near": state,
        "locations": location_cards,
        "previous_chapter_tail": prev_tail,
        "historical_recall": memory_for_chapter(store, chapter, ch_plan, present),
        "word_band": dict(cfg["word_band"]),
        # 日志体开场策略随包走（写作简报与提交探针共用同一口径）。
        "dateline_openings": str(cfg.get("dateline_openings") or "discourage"),
        # This flag describes the host requirement, not an isolation guarantee.
        "context_isolation_required": cfg.get("execution_mode") == "stage-agent",
        "read_only": True,
        "forbid_library_browse": True,
        "write_contract": _WRITE_CONTRACT,
        "inputs_fingerprint": inputs_fingerprint(store, chapter),
    }
    if int(cfg.get("review_contract_version", 2)) >= 2:
        body["write_contract"] = {**_WRITE_CONTRACT, "keys": [*_WRITE_CONTRACT["keys"], "plot_findings"], "optional_keys": [], "review_contract_version": 2}
    if any(expected_delta.values()):
        body["expected_delta"] = expected_delta
    # 阶段字段保持稳定：内置文风直接用总手册指导润色，外部文风若有
    # .writer.md companion 则用该文件。手写旧 pack 的 voice_manual_text
    # 回退由 make_polish_view 单独兼容。
    body["voice_writing_text"] = write_text
    body["voice_writing_manual"] = write_path
    # 内容层手册（给 draft 视图用）
    if content.get("content_text"):
        body["voice_content_text"] = content["content_text"]
        body["voice_content_instruction"] = content["content_instruction"]
        if content.get("content_skill_manual"):
            body["voice_content_manual"] = content["content_skill_manual"]
    # 人写节奏锚（给 polish 视图用）：config.voice_anchor_file 未配置时静默缺席
    anchor_text = build_voice_anchor(store.project, cfg, chapter, focus=fact_focus)
    if anchor_text:
        body["voice_anchor_text"] = anchor_text
        body["voice_anchor_file"] = str(cfg.get("voice_anchor_file"))
    # 按需加载：仅当字段有实际内容时才注入 Pack，避免空结构占用上下文
    if present_cards:
        body["present_cards"] = present_cards
    if character_profiles:
        body["character_profiles"] = character_profiles
    if knowledge:
        body["knowledge"] = knowledge
    if knowledge_refs:
        body["knowledge_refs"] = knowledge_refs
    if character_continuity:
        body["character_continuity"] = character_continuity
    if debts:
        body["debts"] = debts
    if hooks:
        body["hooks"] = hooks
    if relations:
        body["relations"] = relations
    if items:
        body["items"] = items
    conditions, conditions_omitted = select_conditions(
        store,
        names=present,
        snapshot=snapshot,
        cap=int(caps.get("conditions", 16)),
        irreversible_cap=int(caps.get("irreversible_conditions", IRREVERSIBLE_CONDITIONS_PACK_MAX)),
    )
    if conditions_omitted:
        omitted["conditions"] = conditions_omitted
    if conditions:
        body["conditions"] = conditions
        if any(c.get("irreversible") for c in conditions):
            body["conditions_directive"] = (
                "【不可逆代价】以下状态标了 `irreversible`：它们是**累加且不可自愈**的，"
                "正文不得出现与之矛盾的动作或能力（断肢不能握剑、毁容不能以貌识人、"
                "吊销的资格不能照旧行使、清零的本钱不能凭空回来），除非本章账本显式申报了对应的状态变更。"
            )
    if omitted:
        body["omitted"] = omitted
        body["omitted_hint"] = (
            "上列字段是**有界切片**，数字是没装进本包的条数。"
            "若某条正是本章要用的（尤其最旧的长线关系/债务），用 `ledger` 侧命令查全量，"
            "不要凭印象补写。"
        )
    timeline = ch_plan.get("timeline") or ch_plan.get("story_time")
    if timeline:
        body["timeline"] = timeline
    if near:
        body["near_summaries"] = near
    if memory_layers:
        body["memory_layers"] = memory_layers
    if chapter_story_focus:
        body["story_focus"] = chapter_story_focus
    if volume_spine:
        body["volume_spine"] = volume_spine
    if world_spine:
        body["world_spine"] = world_spine
    if glossary:
        # 自相矛盾硬拒：禁用串命中同一份包里的拍点 must/文本、
        # 卷脊或世界脊柱 = 写手被要求"既要产出这个词、又不得使用它"。must 是机检子串、
        # 禁用串是机检禁区，两者互斥必然返工循环——这是 config.glossary 填成了
        # 「词条→释义」的典型症状，必须在装配期拦下，不能让它损伤正文。
        _assert_glossary_consistent(glossary, beats=beats, volume_spine=volume_spine, world_spine=world_spine)
        body["glossary"] = glossary
    if voice.get("skill_manual"):
        body["voice_skill_manual"] = voice["skill_manual"]
    if voice.get("style_formula"):
        body["style_formula"] = voice["style_formula"]
    if voice.get("checklist"):
        body["voice_checklist"] = voice["checklist"]
    if recap:
        body["recap"] = recap
    body["context_sources"] = {
        "plan": str(store.plan_path), "kb": str(store.kb_path),
        "canon_directory": str(store.canon_dir),
        "hierarchical_memory": str(store.hierarchical_memory_path),
        "history_query": {"before_chapter": chapter, "people": present,
                          "event_ids": ch_plan.get("memory_refs") or [],
                          "topics": knowledge_refs},
    }
    if chapter > 1:
        body["context_sources"]["previous_chapter"] = str(store.chapter_md_path(chapter - 1))
    # 视角结构与平台尺度（plan 顶层携带，hatch 门 8 声明后每章随包；进 pack_hash）
    pov_sentence = render_narrative_pov(plan.get("narrative_pov"))
    if pov_sentence:
        body["narrative_pov"] = pov_sentence
    fence_sentence = render_content_fence(plan.get("content_fence"))
    if fence_sentence:
        body["content_fence"] = fence_sentence
    pack_hash = pack_hash_digest(body)
    pack = dict(body)
    pack["pack_hash"] = pack_hash
    return pack


def previous_chapter_tail(store: BookStore, chapter: int) -> str:
    """Select the previous chapter's final four complete paragraphs for continuity."""
    if chapter <= 1:
        return ""
    prev_path = store.chapter_md_path(chapter - 1)
    if not prev_path.exists():
        return ""
    try:
        lines = [
            l.strip()
            for l in prev_path.read_text(encoding="utf-8").split("\n")
            if l.strip() and not l.startswith("#")
        ]
        if not lines:
            return ""
        tail_lines = lines[-4:]
        tail_text = "\n".join(tail_lines)
        return tail_text
    except Exception:
        return ""


def _occupancy_locations(location: str, present_cards: list[dict[str, Any]]) -> list[str]:
    """Select the scene location and the known locations of its named actors."""
    out: list[str] = []
    for loc in [location, *(str(c.get("location") or "").strip() for c in present_cards)]:
        if loc and loc not in out:
            out.append(loc)
    return out


# 写者输出契约随 pack 注入，避免内联角色另读文档才知道输出格式。
# 抽象化通用协议：零特定情节/人名/术语绑定，跨题材通用。
_WRITE_CONTRACT: dict[str, Any] = {
    # prose 不在必填键里：正文真源是阶段二终稿（polished_output_path），
    # 脚本在 submit 时读盘注入 canonical output，事实编辑不必逐字回显整章。
    "keys": ["l1_summary", "state_delta", "memory", "pack_hash", "beats_hit"],
    # Legacy v1 shape; v2 makes plot_findings mandatory during pack assembly.
    "optional_keys": ["plot_findings"],
    "delta_schema": {
        "moves": [{"who": "str", "to": "str（必填：缺了会把该角色 location 清空）"}],
        "facts": [{"who": "str", "text": "str", "pin": "bool", "quote": "str（正文证据，要求）"}],
        "debts": [{"id": "str", "who": "str", "text": "str", "status": "open|paid|cancelled", "due": "int（章号，不收「第 N 章」文本）", "quote": "str（正文证据，要求）"}],
        "hooks": [{"id": "str", "text": "str", "due": "int（章号，不收「第 N 章」文本）", "status": "open|paid|closed|deferred", "quote": "str（正文证据，要求）"}],
        "relations": [{"who": "str", "target": "str", "kind": "str", "status": "open|closed（缺省 open；结束关系用 closed）", "supersedes": "bool（可选：本章这条是该对角色关系的演变/取代，账本把其它 open 条目置 closed）", "supersedes_kind": "str（可选：只退掉 kind 等于此值的旧条目）", "quote": "str（正文证据，要求）"}],
        "named": ["在场工作集人名"],
        "new_names": ["新登场申报人名"],
        "deaths": [{"who": "阵亡人名", "quote": "str（正文证据，要求）"}],
        "revivals": ["复活/夺舍归来的已故角色（撤销死亡标记）"],
        "nonliving": ["已故角色以遗体/遗物/鬼魂/闪回身份在场时在此声明，否则 named/moves 会以 dead_speaking 停线"],
        "items": [{"id": "str", "name": "str", "holder": "str", "quantity": "int", "status": "held|used|damaged|lost|transferred", "quote": "str（正文证据，要求）"}],
        "conditions": [{"who": "str", "kind": "伤势|体力|欠债|职务|…（项目自定）", "text": "str", "value": "int|float（可选，可量化状态的数值）", "unit": "str（可选）", "irreversible": "bool（不可逆代价：永不驱逐；活跃项在 pack 上限内全量注入，超限停线）", "status": "active|resolved", "quote": "str（正文证据，要求）"}],
        "knowledge": [{"who": "规范角色名", "topic_id": "稳定话题 id", "claim": "此人当前知道/误信/怀疑的说法", "stance": "knows|believes|suspects|refuted", "source": "获知途径", "quote": "str（本章逐字证据，必填）"}],
        "locations": [{"id": "str（或以 name 为 id）", "name": "str", "aliases": ["别名"], "attributes": {"楼层": "四楼", "门牌": "101", "方位": "北"}, "replaces": ["属性键（正文演出翻修/搬迁时显式留痕，未标记的同键改值按漂移拦截）"], "status": "open|closed", "quote": "str（正文证据，带 attributes 时必填）"}],
    },
    "gates": [
        "只从终稿正文提取账本字段；不要回显正文，也不要改动正文。",
        "pack_hash 必须逐字回显。",
        "expected_delta 中的 planned entry 必须原样回填到同名 state_delta 列表，再补逐字 quote。",
        "`chapter submit` 校验名字、引文、expected_delta、字段形状和 plot_findings 证据；组装字段错误原位修正，不消耗正文返工预算。",
        "plot_findings 必须显式给出（无问题为 []）；每条 severity=BLOCKER|WARNING|NIT|UNVERIFIABLE。除 UNVERIFIABLE 外必须有逐字 quote；只有 BLOCKER 会回草稿重写。",
    ],
}


_EFFECT_KINDS = ("moves", "facts", "debts", "hooks", "relations", "new_names", "deaths", "revivals", "nonliving", "items", "conditions", "knowledge", "locations")


def select_location_cards(
    registry: list[Any],
    *,
    location: str,
    beats: list[Any],
    present_locations: list[str],
    occupancy_locations: list[str],
    cap: int,
) -> list[dict[str, Any]]:
    """从地点簿切本章相关的地点卡（场景地图的工作包投影）。

    命中来源（按优先级）：章地点字段、拍点文本、在场者所在地、占用表地点；
    不足 cap 时按最近使用（updated_chapter 降序）补满——常去地点即使本章
    没被点名也带着，防「没点名就当不存在」。属性摊平为 {键: 值字符串}，
    quote/章号留在账本，简报只报值，冲突闸按值比较。
    """
    entries: list[tuple[int, dict[str, Any]]] = []
    for raw in registry or []:
        if isinstance(raw, dict):
            entries.append((int(raw.get("updated_chapter") or raw.get("established_chapter") or 0), raw))
    if cap <= 0 or not entries:
        return []
    beat_text = " ".join(
        str((beat or {}).get("text") or "") for beat in (beats or []) if isinstance(beat, dict)
    )
    haystacks = [
        str(location or ""),
        beat_text,
        " ".join(str(x) for x in present_locations if x),
        " ".join(str(x) for x in occupancy_locations if x),
    ]

    def _matched(card: dict[str, Any]) -> bool:
        names = [str(card.get("id") or ""), str(card.get("name") or ""), *(str(a) for a in (card.get("aliases") or []))]
        for name in names:
            if not name:
                continue
            if any(name in h for h in haystacks):
                return True
        return False

    def _card(card: dict[str, Any], rank: int) -> dict[str, Any]:
        attributes = {
            str(key): str((attr or {}).get("value") if isinstance(attr, dict) else attr or "")
            for key, attr in (card.get("attributes") or {}).items()
        }
        return {
            "id": str(card.get("id") or ""),
            "name": str(card.get("name") or card.get("id") or ""),
            "aliases": [str(a) for a in (card.get("aliases") or [])],
            "attributes": {k: v for k, v in attributes.items() if v},
            "established_chapter": int(card.get("established_chapter") or 0),
            "status": str(card.get("status") or "open"),
            "_rank": rank,
        }

    matched = [_card(card, 0) for _, card in entries if _matched(card)]
    if len(matched) < cap:
        chosen_ids = {c["id"] for c in matched}
        for _, card in sorted(entries, key=lambda e: -e[0]):
            if len(matched) >= cap:
                break
            if str(card.get("id") or "") in chosen_ids:
                continue
            matched.append(_card(card, 1))
    for card in matched:
        card.pop("_rank", None)
    return matched[:cap]


def _effect_entries(effects: Any, kind: str) -> list[Any]:
    """把 beat.effects[kind] 归一成 {who/text/...} 形状，供 expected_delta 使用。"""
    if not isinstance(effects, dict):
        return []
    entries = effects.get(kind)
    if kind in ("revivals", "nonliving"):
        return [{"who": name} for name in lifecycle_names(entries, field=f"effects.{kind}")]
    if not isinstance(entries, list):
        return []
    out: list[Any] = []
    for entry in entries:
        if kind in ("new_names", "deaths") and isinstance(entry, str):
            name = str(entry).strip()
            if name:
                out.append(name if kind == "new_names" else {"who": name})
            continue
        if not isinstance(entry, dict):
            continue
        clean = {k: v for k, v in entry.items() if k != "quote"}
        if kind == "deaths":
            who = str(clean.get("who") or "").strip()
            if who:
                out.append({"who": who})
        elif kind == "new_names":
            who = str(clean.get("who") or clean.get("name") or "").strip()
            if who:
                out.append(who)
        else:
            out.append(clean)
    return out


def collect_expected_delta(raw_beats: Any) -> dict[str, list[Any]]:
    """把各 beat 的 effects 展平成整章 expected_delta（组装阶段的“计划账本”）。

    计划只声明“这章该留下什么状态”，不做正文抽取；事实编辑拿它逐条核对终稿，
    命中后原样回填并附正文 quote，未命中则报 expected_delta_missing。
    """
    expected: dict[str, list[Any]] = {kind: [] for kind in _EFFECT_KINDS}
    if not isinstance(raw_beats, list):
        return expected
    for beat in raw_beats:
        if not isinstance(beat, dict):
            continue
        effects = beat.get("effects")
        if not isinstance(effects, dict):
            continue
        for kind in _EFFECT_KINDS:
            expected[kind].extend(_effect_entries(effects, kind))
    return expected


def _normalize_beats(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            beat = {"id": f"b{i+1}", "required": True, "text": item, "must": ""}
            beat["rendered"] = _render_beat(beat, i + 1)
            out.append(beat)
            continue
        if not isinstance(item, dict):
            continue
        beat_id = str(item.get("id") or f"b{i+1}")
        beat = {
            "id": beat_id,
            "required": bool(item.get("required", True)),
            "text": str(item.get("text") or ""),
            "must": str(item.get("must") or ""),
        }
        beat["rendered"] = _render_beat(beat, i + 1)
        out.append(beat)
    return out


def _render_beat(beat: dict[str, Any], index: int) -> str:
    """将beat渲染成自然语言场景描述。"""
    text = beat.get("text", "")
    must = beat.get("must", "")

    if must:
        return f"第{index}场：{text}（关键词须带出：{must}）"
    else:
        return f"第{index}场：{text}"



def _present_names(ch_plan: dict[str, Any], protagonist: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in [protagonist, *(ch_plan.get("present") or [])]:
        n = str(name).strip()
        if not n or n in seen:
            continue
        seen.add(n)
        names.append(n)
    if len(names) > _PRESENT_CHARACTER_CAP:
        raise LedgerError(
            "present_cast_overflow",
            f"chapter present has {len(names)} named actors; max {_PRESENT_CHARACTER_CAP} "
            "including the protagonist. List only characters who actually act or speak "
            "in this chapter; move other onlookers into scene prose or split the scene.",
            {"count": len(names), "cap": _PRESENT_CHARACTER_CAP},
        )
    return names



def _present_cards(
    store: BookStore,
    present: list[str],
    protagonist: str,
    snapshot: dict[str, Any],
    chapter: int = 1,
    ch_plan: dict[str, Any] | None = None,
    *,
    fact_focus: str = "",
    selected_relations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    entities = snapshot.get("entities") or {}
    # 直接调用时也沿用账本关系切片；assemble_pack 传入它已经选好的那一份，
    # 避免人物卡绕过全局 cap、重复把上百条历史关系塞回工作包。
    relations = selected_relations
    if relations is None:
        relations, _omitted = select_relations(
            store, names=[protagonist, *present], cap=6, snapshot=snapshot
        )
    cards = []
    for name in present:
        if name == protagonist:
            continue
        ent = entities.get(name) or {}
        first_seen = int(ent.get("first_seen_chapter") or ent.get("updated_chapter") or 0)
        last_seen = int(ent.get("last_seen_chapter") or ent.get("updated_chapter") or 0)
        is_first_appearance = (first_seen == 0 or first_seen >= chapter)
        
        rel_kinds = []
        for r in relations:
            if str(r.get("status") or "open") == "closed":
                continue
            who = str(r.get("who") or "")
            target = str(r.get("target") or "")
            if (who == name and target == protagonist) or (who == protagonist and target == name):
                kind = str(r.get("kind") or "")
                if kind:
                    rel_kinds.append(kind)
                    
        card: dict[str, Any] = {
            "name": name,
            "location": ent.get("location") or "",
            "is_first_appearance": is_first_appearance,
            "first_seen_chapter": first_seen if first_seen > 0 else None,
            "last_seen_chapter": last_seen if last_seen > 0 else None,
            "relations_with_protagonist": rel_kinds,
            "facts": facts_for_pack(ent.get("facts"), cap=3, focus=fact_focus, who=name),
            "dead": bool(ent.get("dead")),
        }

        if bool(ent.get("dead")):
            card["continuity_directive"] = (
                f"【死亡警告】角色【{name}】在账本中已标记死亡："
                "除非本章章拍/项目正典明确安排其作为回忆、遗物、鬼魂等非活人存在，"
                "否则严禁让其说话、行动或以活人身份参与本章。"
            )
        elif not is_first_appearance:
            rel_str = f"【{'/'.join(rel_kinds)}】" if rel_kinds else "【已有交集】"
            card["continuity_directive"] = (
                f"【连续性警告】：角色【{name}】在第 {first_seen} 章已登场（关系：{rel_str}），"
                f"双方已相识，严禁写成陌生人或初次见面！"
            )

        # 渲染角色小传（将结构化数据转换成自然语言）
        card["rendered"] = _render_character_card(card, protagonist)

        cards.append(card)
    return cards


def _render_character_card(card: dict[str, Any], protagonist: str) -> str:
    """将角色卡片渲染成自然语言小传。"""
    name = card.get("name", "")
    location = card.get("location", "")
    facts = card.get("facts", [])
    relations = card.get("relations_with_protagonist", [])

    parts = [f"角色【{name}】"]

    if card.get("dead"):
        parts.append("账本已标记死亡")

    # 地点
    if location:
        parts.append(f"现在{location}")

    # 关系
    if relations:
        rel_text = "、".join(relations)
        parts.append(f"与{protagonist}的关系：{rel_text}")

    # 事实
    if facts:
        facts_text = "；".join(facts)
        parts.append(f"{facts_text}")

    return "。".join(parts) + "。"


def _add_kb_token(bucket: list[str], seen: set[str], token: str) -> None:
    text = (token or "").strip().lower()
    if len(text) < 2 or text in seen or text in _NEEDLE_STOP:
        return
    seen.add(text)
    bucket.append(text)


def _add_kb_needle(
    body_needles: list[str],
    struct_needles: list[str],
    seen: set[str],
    text: str,
    *,
    ngrams: bool = False,
    body_ok: bool = True,
) -> None:
    raw = (text or "").strip().lower()
    if len(raw) < 2:
        return
    target = body_needles if body_ok else struct_needles
    _add_kb_token(target, seen, raw)
    for part in _SPLIT_RE.split(raw):
        _add_kb_token(target, seen, part)
    if not ngrams:
        return
    for run in _CJK_RUN_RE.findall(raw):
        if len(run) < 3:
            continue
        for size in (3, 4):
            if len(run) < size:
                continue
            for i in range(len(run) - size + 1):
                gram = run[i : i + size]
                if gram in seen or gram in _NEEDLE_STOP:
                    continue
                seen.add(gram)
                # 3 字只打 id/title/tags；4 字才允许打正文，避免「世界」一类开篇词拖进说明书
                (body_needles if size >= 4 else struct_needles).append(gram)


def _kb_needles(
    *,
    location: str,
    present: list[str],
    tags: list[str],
    beats: list[dict[str, Any]],
    protagonist: str = "",
    extra_text: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """本章检索词。按需：拍点 / must / 配角人名 / 地点可打正文；主角名、tags 与 extra_text 只打结构化字段。

    不把 volume_spine 当默认 extra_text——卷脊里的世界观大词会把整套说明书拖进来。
    """
    body_needles: list[str] = []
    struct_needles: list[str] = []
    must_needles: list[str] = []
    seen: set[str] = set()
    seen_must: set[str] = set()
    _add_kb_needle(body_needles, struct_needles, seen, location, ngrams=True)
    for name in present:
        is_protag = bool(protagonist and name == protagonist)
        _add_kb_needle(body_needles, struct_needles, seen, str(name), body_ok=not is_protag)
    for beat in beats:
        must = str(beat.get("must") or "")
        _add_kb_needle(body_needles, struct_needles, seen, must)
        # must 是拍点的硬门禁词：命中卡片正文一次即可纳入，不能因「正文命中不足 2 处」的
        # 通用阈值被丢掉——否则拍点点名术语时核心卡永远装不上。
        if must:
            _add_kb_needle(must_needles, [], seen_must, must)
        _add_kb_needle(
            body_needles,
            struct_needles,
            seen,
            str(beat.get("text") or ""),
            ngrams=True,
        )
    for tag in tags:
        _add_kb_needle(body_needles, struct_needles, seen, str(tag), body_ok=False)
    _add_kb_needle(body_needles, struct_needles, seen, extra_text, body_ok=False)
    return body_needles, struct_needles, must_needles


def _is_core_card(card: dict[str, Any]) -> bool:
    if card.get("always") is True:
        return True
    return str(card.get("priority") or "").lower() == "core"


def _format_kb_card(card: dict[str, Any], excerpt_chars: int) -> dict[str, Any]:
    body = str(card.get("body") or card.get("excerpt") or "")
    return {
        "id": card.get("id") or card.get("title"),
        "kind": card.get("kind") or "world",
        "title": card.get("title") or "",
        "excerpt": body,
        "tags": list(card.get("tags") or []),
        "source": card.get("source") or "",
    }


def _card_structured(card: dict[str, Any]) -> str:
    tags = [str(t) for t in (card.get("tags") or []) if str(t).strip() and str(t) not in _GENERIC_STRUCT]
    return " ".join(
        [
            str(card.get("id") or ""),
            str(card.get("title") or ""),
            str(card.get("source") or ""),
            " ".join(tags),
            " ".join(str(a) for a in (card.get("aliases") or [])),
        ]
    ).lower()


def slice_kb(
    cards: list[dict[str, Any]],
    *,
    location: str,
    present: list[str],
    tags: list[str],
    beats: list[dict[str, Any]],
    cap: int,
    excerpt_chars: int,
    protagonist: str = "",
    extra_text: str = "",
    kb_refs: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按本章拍点检索卡片；``kb_refs`` 可钉住不能漏的卡片 id。

    返回：(切片卡片列表, 切片元数据)
    元数据包含命中统计，用于诊断章拍质量对KB切片的影响。
    """
    cards = validate_kb_cards(cards)
    if kb_refs is None:
        refs = []
    elif not isinstance(kb_refs, list) or any(
        not isinstance(item, str) or not item.strip() for item in kb_refs
    ):
        raise LedgerError(
            "invalid_kb_ref",
            "kb_refs must be a list of non-empty card-id strings",
        )
    else:
        refs = [item.strip() for item in kb_refs]
    if len(refs) != len(set(refs)):
        raise LedgerError("invalid_kb_ref", "kb_refs must not contain duplicate card ids", {"kb_refs": refs})
    card_ids = {str(card.get("id") or "") for card in cards}
    missing_refs = [item for item in refs if item not in card_ids]
    if missing_refs:
        raise LedgerError(
            "missing_kb_ref",
            "chapter references KB card ids that do not exist",
            {"missing": missing_refs},
        )
    ref_set = set(refs)
    meta = {
        "matched_cards": 0,
        "selected_cards": 0,
        "omitted_cards": 0,
        "effective_cap": 0,
        "explicit_refs": refs,
        "match_breakdown": {
            "explicit": 0,
            "location": 0,
            "present": 0,
            "tags": 0,
            "must": 0,
        },
    }

    if cap <= 0 and not refs:
        return [], meta

    body_needles, struct_needles, must_needles = _kb_needles(
        location=location,
        present=present,
        tags=tags,
        beats=beats,
        protagonist=protagonist,
        extra_text=extra_text,
    )

    # 统计needle来源（用于诊断）
    location_needles = set()
    present_needles = set()
    tags_needles = set()
    # 重新构建needles并标记来源
    if location:
        for token in _SPLIT_RE.split(location):
            if token and len(token) >= 2 and token not in _NEEDLE_STOP:
                location_needles.add(token.lower())

    for name in present:
        if name:
            present_needles.add(name.lower())

    for tag in tags:
        for token in _SPLIT_RE.split(tag):
            if token and len(token) >= 2 and token not in _NEEDLE_STOP:
                tags_needles.add(token.lower())

    scored: list[tuple[int, int, int, int, int, dict[str, Any]]] = []
    for card in cards:
        card_id = str(card.get("id") or "")
        ref_bonus = 1 if card_id in ref_set else 0
        structured = _card_structured(card)
        body = str(card.get("body") or card.get("excerpt") or "").lower()
        struct_score = 0
        body_score = 0

        # 记录命中来源
        hit_location = False
        hit_present = False
        hit_tags = False
        hit_must = False

        for needle in body_needles:
            if needle in structured:
                struct_score += 2
            if needle in body:
                body_score += 1
            # 判断来源
            if needle in location_needles:
                hit_location = True
            if needle in present_needles:
                hit_present = True
            if needle in tags_needles:
                hit_tags = True

        for needle in struct_needles:
            if needle in structured:
                struct_score += 1

        for needle in must_needles:
            if needle in body:
                body_score += 2
                hit_must = True

        # 强相关度阈值：要求结构化命中、must 命中正文，或至少 2 处正文强相关，
        # 过滤单字单词弱关联噪声
        total_score = struct_score + body_score
        if not ref_bonus and total_score < 2 and struct_score <= 0:
            continue

        # 统计命中来源
        if hit_location:
            meta["match_breakdown"]["location"] += 1
        if hit_present:
            meta["match_breakdown"]["present"] += 1
        if hit_tags:
            meta["match_breakdown"]["tags"] += 1
        if hit_must:
            meta["match_breakdown"]["must"] += 1
        if ref_bonus:
            meta["match_breakdown"]["explicit"] += 1

        core_bonus = 1 if _is_core_card(card) else 0
        kind_rank = _KIND_RANK.get(str(card.get("kind") or "world"), 9)
        scored.append((ref_bonus, struct_score, body_score, core_bonus, -kind_rank, card))

    meta["matched_cards"] = len(scored)
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]), reverse=True)

    # 动态按需截断：单章通常最多需要 3–4 张高相关设定卡，避免灌入过多弱相关背景
    effective_cap = max(len(refs), min(cap, 4))
    meta["effective_cap"] = effective_cap

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, _, _, _, card in scored:
        if len(out) >= effective_cap:
            break
        cid = str(card.get("id") or card.get("title") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(_format_kb_card(card, excerpt_chars))

    meta["selected_cards"] = len(out)
    meta["omitted_cards"] = max(0, len(scored) - len(out))
    return out, meta


def near_summaries(store: BookStore, chapter: int, *, cap: int) -> tuple[list[dict[str, Any]], int]:
    """近章摘要（最近 cap 章）。返回 `(条目, 被省略条数)`。

    近章摘要是写者唯一的"前情"叙述来源，缺了它写者会开始凭印象补前情。
    所以超过 cap 的旧摘要要**报数**，而不是静默 `break`。
    """
    available_by_chapter = {}
    for path in store.summaries_dir.glob("**/ch-*.json"):
        try:
            number = int(path.stem.split("-")[-1])
        except ValueError:
            continue
        if 0 < number < chapter:
            expected = store.summary_path(number)
            if number not in available_by_chapter or path == expected:
                available_by_chapter[number] = path
    available = list(available_by_chapter.items())
    keep = max(0, int(cap))
    chosen = sorted(available, reverse=True)[:keep]
    collected: list[dict[str, Any]] = []
    # Discover indexes without reading every old summary's full body.
    for n, path in reversed(chosen):
        data = read_json(path)
        collected.append(
            {
                "chapter": n,
                "l1_summary": str(data.get("l1_summary") or ""),
                "source_path": str(path),
            }
        )
    return collected, max(0, len(available) - len(collected))


def allowed_names(pack: dict[str, Any]) -> set[str]:
    """state_delta / moves / facts 里允许使用的**既有**名字。

    判据是"账本已经认识它"，而不是"本章刚好在场"：名字表若只取 now_card + present_cards +
    state_near.pinned，那么一个前几章登过场、本章只在近况或关系表里出现的角色会被判成
    "新名字"，事实编辑只能把他塞进 new_names —— 而 new_names 的语义是"本章首次登场"，
    用它兜底会污染首次登场记录。

    现场曾出现：某角色在前几章都出过场，本章只在 state_near.recent 里出现，
    assemble 校验连报 unused_in_pack（unnamed_in_pack），worker 只能改道 new_names
    才过检。
    """
    names: set[str] = set()
    now = pack.get("now_card") or {}
    if now.get("name"):
        names.add(str(now["name"]))
    for card in pack.get("present_cards") or []:
        if card.get("name"):
            names.add(str(card["name"]))
    state_near = pack.get("state_near") or {}
    for item in state_near.get("pinned") or []:
        if isinstance(item, dict) and item.get("id"):
            names.add(str(item["id"]))
    # 近况里的人：可能正是本章要复用的旧角色（只差不在场）。
    for item in state_near.get("recent") or []:
        if isinstance(item, dict) and item.get("id"):
            names.add(str(item["id"]))
    # 关系表两端：账本已经登记过这层关系，名字当然不是新的。
    for rel in (pack.get("ledger_refs") or {}).get("relations") or []:
        if not isinstance(rel, dict):
            continue
        for key in ("who", "target"):
            if rel.get(key):
                names.add(str(rel[key]))
    # 连续性档案里的人。
    for item in pack.get("character_continuity") or []:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    return names


# 目标字数的缓冲：草稿就奔着下限以上写，别等 submit 机检才发现差百来字。
# 生产书实测：aim=min+300=2800 时初稿稳定落在
# 2050–2500（欠写 15–25%），几乎每章都吃一次"补整场戏"返工；缓冲抬到 700，
# aim=3200（≈5 场戏）让首轮草稿直接进带。下限 2500 不动——那是闸门，不是目标。
WORD_BAND_AIM_BUFFER = 700
# 一场戏的规模特写：现场实测"补字数"唯一被允许的动作就是补整场戏，所以场次预算必须
# 用同一个尺度折算，不能一处写 500-700、另一处写别的。
SCENE_CHARS = 700


def word_targets(word_band: Any) -> dict[str, int]:
    """把 word_band 折算成 (下限, 上限, 安全目标)。总编辑任务书与执笔简报共用同一口径。"""
    band = word_band if isinstance(word_band, dict) else {}
    minimum = int(band.get("min") or 0)
    maximum = int(band.get("max") or 0)
    aim = min(minimum + WORD_BAND_AIM_BUFFER, maximum) if maximum > minimum else minimum
    return {"min": minimum, "max": maximum, "aim_chars": aim}


def scene_budget(target_chars: int, *, floor: int = 0, gap_chars: int = 0) -> dict[str, int]:
    """把"目标字数"折算成"几场戏 × 每场多少字"，让字数缺口在落笔前就是可执行步长。

    草稿首轮曾低于下限 2500，`chapter precheck` 当场打回，只能一次性补一整场戏再交——
    这一轮等于白写一次全文。且把预算只放进 `chapter next` 的行动载荷不够——执笔编辑
    读的是 pack 里的 `writing_brief`，看不见载荷。所以预算必须渲染进 `writing_brief`
    本身（见 views.render_writing_brief）。
    """
    band = max(1, int(target_chars))
    scenes = max(1, -(-band // SCENE_CHARS))
    budget = {
        "target_chars": band,
        "scenes": scenes,
        "chars_per_scene": SCENE_CHARS,
    }
    if floor:
        budget["floor_chars"] = int(floor)
    if gap_chars > 0:
        budget["gap_chars"] = int(gap_chars)
        budget["scenes_to_floor"] = max(1, -(-int(gap_chars) // SCENE_CHARS))
    return budget


def beat_word_allocation(aim_chars: int, beat_count: int) -> list[int]:
    """把目标字数摊到各拍：等分、余数给前几拍，给执笔一个逐场配速锚。

    场次预算（scene_budget）只回答"共几场、每场约多少字"；实测单场写短时
    没有当场参照，整章欠载要到 draft-submit 才被发现（word_count_low 返工）。
    拍级分配把总目标拆成逐场小目标并渲染进 writing_brief，哪场写得薄当场可见。
    """
    count = max(0, int(beat_count))
    aim = max(0, int(aim_chars))
    if count == 0 or aim == 0:
        return []
    base, rest = divmod(aim, count)
    return [base + (1 if i < rest else 0) for i in range(count)]
