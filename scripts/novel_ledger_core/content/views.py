"""阶段视图层：同一 canonical pack 按阶段切成互不重叠的只读输入。

目标（完全解耦的三个阶段输入）：
- draft view：只给内联执笔角色。canonical 结构由脚本确定性渲染为 `writing_brief`
  完整句式，**不把同一份拍点 / NOW / KB 再以 JSON 重发**；不含 pack_hash 与机检负担。
- polish view：只给内联润色角色。含草稿阶段的正文 + 文风手册（voice_writing_text；内置文风为总手册）
  与输出契约；内容字段只作“不许改情节”的锚点。

canonical pack（book/pack/current.json）继续作为提交/哈希/归档/ack 的唯一权威；
视图文件全部由 canonical 派生，两者 pack_hash 一致，任何阶段角色都不需要再看 canonical。

依赖单向：views（纯函数）→ pack.py / pipeline.py 写盘。本模块不碰磁盘、不 import store。
"""

from __future__ import annotations

import json
from typing import Any

from .pack import allowed_names, beat_word_allocation, scene_budget, word_targets
from ..infra.util import coerce_due

_CONTENT_KEYS = (
    "now_card",
    "beats",
    "kb_slice",
    "kb_slice_meta",
    "state_near",
    "previous_chapter_tail",
    "present_cards",
    "character_continuity",
    "character_profiles",
    "knowledge",
    "knowledge_refs",
    "debts",
    "hooks",
    "relations",
    "items",
    "conditions",
    "omitted",
    "conditions_directive",
    "timeline",
    "near_summaries",
    "memory_layers",
    "historical_recall",
    "story_focus",
    "volume_spine",
    "world_spine",
    "glossary",
    "recap",
)

# 组装视图（事实编辑）只保留本阶段真正会用到的内容字段：
# - 机检名字/拍点：now_card / present_cards / state_near / beats；
# - compact 档情节自检：kb_slice / world_spine / character_continuity /
#   在场 character_profiles / glossary。
# 未结责任/伏笔/关系不直接投喂原列表，而由 ledger_refs 做有界投影；
# compact 才额外获得 verification_brief，避免 full 档重复投喂前情。
_ASSEMBLE_CONTENT_KEYS = (
    "historical_recall",
    "beats",
    "now_card",
    "present_cards",
    "state_near",
    "locations",
    "kb_slice",
    "world_spine",
    "character_continuity",
    "character_profiles",
    "knowledge",
    "knowledge_refs",
    "glossary",
    "items",
    "conditions",
    "omitted",
    "conditions_directive",
)

# 润色视图（文风编辑）在内容上只需"不许改什么"的锚点、台词语气指引与规范术语。
# 正典切片、近章摘要、全书债务/伏笔/人际关系账本、连续性记录与世界观脊柱不进这一格：
# 该阶段契约是"内容冻结、只改文风"，记账与宏观设定既不是它的判据，反而会诱导它去推理剧情（越界）。
# 保留 beats（must 锚点）、now_card 与 present_cards、
# 上章尾（跨章语感衔接），以及 glossary（术语规范表，防止违禁词被误留或引入）。
# `items` 与个人认知退出本视图；文风编辑只留在场人物的表达基线，
# 不读取会引导它补写秘密或重判剧情的知情账。
_POLISH_DROP_KEYS = (
    "historical_recall",
    "kb_slice",
    "kb_slice_meta",
    "near_summaries",
    "memory_layers",
    "story_focus",
    "debts",
    "hooks",
    "relations",
    "character_continuity",
    "volume_spine",
    "world_spine",
    "recap",
    "state_near",
    "conditions",
    "omitted",
    "omitted_hint",
    "conditions_directive",
    "items",
    "knowledge",
    "knowledge_refs",
)


_CONTENT_GUIDE_KEYS = (
    "voice_content_text",
    "voice_content_instruction",
    "voice_content_manual",
)

DRAFT_SCHEMA = "novel-ledger.view.draft.v2"
POLISH_SCHEMA = "novel-ledger.view.polish.v1"
ASSEMBLE_SCHEMA = "novel-ledger.view.assemble.v2"

DRAFT_GATES = (
    "先完整阅读 writing_brief；它是 canonical JSON 经脚本确定性生成的本章写作任务书，"
    "其中所有场次、事实、正典与连续性句子都必须遵守。",
    "把简报规定的几场戏写成内容草稿：情节、冲突、动作、对话写清楚写顺，承接上一章尾部。",
    "动笔前先读本视图已注入的内容层手册（若有），根据本章情境选择开场与场景写法；世界事实以项目正典为准，不从手册示例移入人物、情节或规则。",
    "写出饱满、连贯、自然的正文内容。",
    "字数口径与目标：只数汉字（标点/数字/字母不算），word_band 是硬闸、aim_chars 是安全目标"
    "（下限+缓冲）。按场次写而不是按字数注水：一场有来往、有后果的戏约 500-700 汉字，"
    "场次数以 writing_brief 定位段的场次预算为准；章拍场次少于预算时拆厚场或补过场戏，"
    "全部 must 词必须覆盖。draft-submit 会检查字数，不足会当场拒收（draft_rejected）；"
    "补足必须一次性加整场戏，严禁每轮挤几十字。",
    "writing_brief 的“知识边界”若说明候选经过有界截断，不代表可自行补设定；"
    "若章拍依赖但简报未给出关键规则，立即向总编辑澄清，由章拍 kb_refs 显式钉入。",
    "只把纯文本草稿正文写入 draft_output_path，不要输出 JSON 或结构化字段。",
)

POLISH_GATES = (
    "通读 voice_writing_text（润色文风手册），依其写法与自检要求润色；不把风格示例当作剧情设定。",
    "保留草稿的事件、冲突因果、人物动机、已知信息与 beats 的 must 词，调整句式、叙述节奏和对白表达，不擅自改写情节。",
    "若本视图带 voice_anchor_text（人写节奏锚），观察它如何随场景变化安排叙述与停顿；"
    "不得借用其中的人名、地名、组织、设定、情节或原句，也不按句长比例或某种句式凑数。",
    "字数以 word_band 为准（只数汉字，标点、数字、字母不算），以 aim_chars 为安全目标。"
    "篇幅不足时，只在现有事实和场景内补足必要的行动、反应与衔接；若无法补足，交还总编辑处理。"
    "polish-submit 会检查字数；启用文风机检时还会检查明确出戏的话术。短稿会被拒收，文件保留供原位修订。",
    "本视图**不含正典切片**：不得依据一般常识或“别的书的写法”改写任何世界细节（数值、称谓、器物、规则）。"
    "你的职责是重塑语感，不是核对世界；对拿不准的地方一律原样保留。",
    "把润色重构后的纯文本终稿写入 polished_output_path，只提交正文文本。",
)

ASSEMBLE_GATES = (
    "只需产出账本字段（l1_summary / state_delta / memory / pack_hash / beats_hit）；"
    "正文真源是阶段二终稿 polished_output_path，脚本会在 submit 时读盘注入，"
    "不要在本阶段回显或改写正文。",
    "state_delta / beats_hit / l1_summary 严格按终稿正文精确填写；facts/debts/hooks/relations/deaths/knowledge "
    "每一条都应附 quote（从终稿逐字抄出支持该增量的原句）：逐字＝与 polished_output_path 完全一致的"
    "子串（≥6 字，标点也要一致），凭印象转述必然被机检打回（delta_quote_not_in_prose）；"
    "没有逐字证据就不要申报该条。",
    "allowed_delta_names 是 state_delta.named、moves/facts/conditions.who 当前允许使用的既有名字；"
    "不在表内的新登场角色必须先列入 new_names，绝不能靠记忆补名字。",
    "relations 条目以 (who, target, kind) 为键：同一对角色**换了说法**就是新条目，不会覆盖旧的。"
    "若本章这条是既有关系的演变或取代（盟友→仇敌、试探→定盟），必须加 \"supersedes\": true"
    "（只想退掉指定的一条时再加 \"supersedes_kind\": \"<旧 kind 原文>\"），账本会把该对角色其它 "
    "open 条目置为 closed；关系真正结束而不写新条目时，直接写 status=\"closed\"。"
    "确实同时成立的多重身份（师徒＋姻亲）不要加 supersedes。",
    "expected_delta 的每个 planned entry 必须原样回填到同名 state_delta 列表（不要改写 who/text/id/kind 等稳定键），"
    "再补逐字 quote；终稿确实没兑现时报告 expected_delta_missing，不得自造状态。",
    "plot_findings 每条必须带 severity、hint、逐字 quote；只有 BLOCKER 才申请回草稿，"
    "WARNING/NIT 只记录、不阻断。",
    "输出合法 submit JSON（账本字段见 write_contract；正文由脚本注入，不必回显），pack_hash 必须逐字回显 canonical pack_hash。",
    "若 action 响应带 plot_self_check（本角色兼做情节自检）：通读终稿逐项核对，"
    "发现真问题写进 plot_findings 数组，每条 {code, severity, hint(必填), quote(逐字来自终稿、≥6 字)}；"
    "全过则写空数组 []。只有客观 BLOCKER 才回草稿重写，不报口味偏好。",
    "写完后直接运行 `chapter submit`；组装字段有误会返回 fix_assembly，"
    "在原文件修正并重交，不消耗正文返工次数。复杂问题可用只读 `chapter check-submit` 诊断。",
)


def _pick(pack: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        if key in pack:
            out[key] = pack[key]
    return out


# 视图落盘用保序序列化（见 util.atomic_json_ordered）。这里把**每章一字不差的稳定块**
# （手册全文 + 阶段契约 + 常量开关）排到最前，逐章变化的章拍/正文锚点排到后面，
# 让每章视图形成尽可能长的**稳定前缀**供宿主 prompt cache 命中。chapter 不放最前——
# 它逐章递增，放前面会把前缀截断成只剩 schema/view 两个字段。
_DRAFT_STABLE = (
    "schema",
    "view",
    "word_band",
    "voice_content_text",
    "voice_content_instruction",
    "voice_content_manual",
    "draft_contract",
    "read_only",
    "forbid_library_browse",
)
_POLISH_STABLE = (
    "schema",
    "view",
    "word_band",
    "voice_writing_text",
    "voice_writing_manual",
    "polish_contract",
    "read_only",
    "forbid_library_browse",
)
# 组装视图的稳定块＝输出契约（delta_schema 等，逐章不变）；pack_hash/fingerprint 逐章变，归到后面。
_ASSEMBLE_STABLE = (
    "schema",
    "view",
    "write_contract",
    "read_only",
    "forbid_library_browse",
)
def _stable_first(view: dict[str, Any], stable_keys: tuple[str, ...]) -> dict[str, Any]:
    """把稳定字段按声明顺序提到最前，其余（逐章变化）保持原顺序跟在后面。"""
    out: dict[str, Any] = {}
    for key in stable_keys:
        if key in view:
            out[key] = view[key]
    for key, value in view.items():
        if key not in out:
            out[key] = value
    return out


_TERMINAL_PUNCTUATION = ("。", "！", "？", "；", ".", "!", "?", ";")


def _sentence(value: Any) -> str:
    """把单条已裁剪事实变成完整句子，不改写其中的专名与数值。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.endswith(_TERMINAL_PUNCTUATION) else text + "。"


def _joined(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    return "、".join(str(item).strip() for item in values if str(item).strip())


def _human_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if key == "status":
        status_labels = {
            "open": "未结",
            "paid": "已偿还",
            "cancelled": "已取消",
            "closed": "已关闭",
            "deferred": "已延期",
            "held": "持有中",
            "used": "已使用",
            "damaged": "已损坏",
            "lost": "已遗失",
            "transferred": "已转移",
            "active": "生效中",
            "resolved": "已解除",
        }
        return status_labels.get(str(value).strip().lower(), str(value).strip())
    if key == "due" and isinstance(value, int):
        return f"第{value}章"
    if isinstance(value, list):
        return _joined(value)
    if isinstance(value, dict):
        return "、".join(f"{k}={v}" for k, v in value.items() if str(v or "").strip() != "")
    return str(value).strip()


def _inline_excerpt(value: Any) -> str:
    """把正典卡里的 Markdown 行转成可直接阅读的一组分句。"""
    lines: list[str] = []
    for raw in str(value or "").splitlines():
        line = raw.strip().lstrip("-*#> ").strip()
        if line and line not in {"|", "---"}:
            lines.append(line.rstrip("。；"))
    return "；".join(lines)


def _append_section(parts: list[str], title: str, lines: list[str]) -> None:
    clean = [_sentence(line) for line in lines if str(line or "").strip()]
    if not clean:
        return
    if parts:
        parts.append("")
    parts.append(f"## {title}")
    parts.extend(f"- {line}" for line in clean)


def _record_details(record: dict[str, Any], fields: tuple[tuple[str, str], ...]) -> str:
    details: list[str] = []
    for key, label in fields:
        value = record.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = _human_value(key, value)
        if rendered:
            details.append(f"{label}为{rendered}")
    return "，".join(details)


def _render_beats(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    valid_beats = [
        beat
        for beat in pack.get("beats") or []
        if isinstance(beat, dict) and str(beat.get("text") or beat.get("rendered") or "").strip()
    ]
    # 拍级配速：字数带已知时把目标摊到每场，写短当场可见，不等 draft-submit 总判。
    allocation: list[int] = []
    word_band = pack.get("word_band") or {}
    if isinstance(word_band, dict) and int(word_band.get("min") or 0) and int(word_band.get("max") or 0):
        allocation = beat_word_allocation(word_targets(word_band)["aim_chars"], len(valid_beats))
    for index, beat in enumerate(valid_beats, start=1):
        text = str(beat.get("text") or beat.get("rendered") or "").strip()
        required = beat.get("required", True) is not False
        prefix = f"第{index}场必须完整演出" if required else f"第{index}场可按节奏处理"
        line = f"{prefix}：{text}"
        target = allocation[index - 1] if index - 1 < len(allocation) else 0
        if target > 0:
            line += f"（本场目标约{target}汉字）"
        must = str(beat.get("must") or "").strip()
        if must:
            line += f"；正文中必须自然出现“{must}”"
        lines.append(line)
    return lines


_EXPECTED_DELTA_LABELS = {
    "moves": "人物移动",
    "facts": "确定事实",
    "debts": "责任与债务",
    "hooks": "伏笔",
    "relations": "关系变化",
    "new_names": "新登场人物",
    "deaths": "死亡结果",
    "revivals": "复活结果",
    "nonliving": "非活人出场",
    "items": "物品变化",
    "conditions": "持续状态",
    "knowledge": "人物认知变化",
    "locations": "场景地点口径",
}


def _render_expected_delta(pack: dict[str, Any]) -> list[str]:
    """把计划账本变成执笔能直接执行的完整句子，不暴露第二份 JSON。"""
    expected = pack.get("expected_delta") or {}
    if not isinstance(expected, dict):
        return []
    lines: list[str] = []
    preferred = (
        "id", "who", "target", "name", "to", "text", "kind", "status", "due",
        "topic_id", "claim", "stance", "source",
        "holder", "quantity", "value", "unit", "pin", "irreversible", "attributes",
    )
    field_labels = {
        "id": "稳定 id", "who": "人物", "target": "对象", "name": "名称", "to": "去向",
        "text": "内容", "kind": "类别", "status": "状态", "due": "期限", "holder": "持有人",
        "quantity": "数量", "value": "数值", "unit": "单位", "pin": "长期钉住",
        "irreversible": "不可逆", "attributes": "空间口径",
        "topic_id": "话题 id", "claim": "此人所知/所信", "stance": "认知状态", "source": "消息来源",
    }
    for category, label in _EXPECTED_DELTA_LABELS.items():
        entries = expected.get(category) or []
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries, start=1):
            if isinstance(entry, dict):
                ordered = [key for key in preferred if key in entry]
                ordered.extend(key for key in entry if key not in ordered and key != "quote")
                details = "，".join(
                    f"{field_labels.get(key, key)}为{_human_value(key, entry[key])}"
                    for key in ordered
                    if entry.get(key) not in (None, "", [], {})
                )
            else:
                details = str(entry or "").strip()
            if details:
                lines.append(f"本章必须实际发生的{label}第{index}项是：{details}")
    return lines


def _render_characters(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    now = pack.get("now_card") or {}
    if isinstance(now, dict) and now:
        details = _record_details(
            now,
            (
                ("name", "姓名"),
                ("goal", "当前目标"),
                ("location", "所在地点"),
                ("wound", "伤势或限制"),
                ("owes", "未偿责任"),
            ),
        )
        if details:
            lines.append(f"主角当前状态如下：{details}")
        facts = _joined(now.get("facts"))
        if facts:
            lines.append(f"主角已经确认的事实包括：{facts}")
        speech = _joined(now.get("speech"))
        if speech:
            lines.append(f"主角本章可沿用的说话习惯包括：{speech}")
        if now.get("dead") is True:
            lines.append("主角已被账本标记为死亡，本章不得把其写成正常存活状态")

    for card in pack.get("present_cards") or []:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "未命名角色").strip()
        details = _record_details(
            card,
            (
                ("location", "当前位置"),
                ("relations_with_protagonist", "与主角的关系"),
                ("facts", "既有事实"),
            ),
        )
        lines.append(f"角色{name}本章在场" + (f"，{details}" if details else ""))

    for record in pack.get("character_continuity") or []:
        if not isinstance(record, dict):
            continue
        directive = str(record.get("continuity_directive") or "").strip()
        if directive:
            lines.append(directive)
    profiles = pack.get("character_profiles") or {}
    labels = (
        ("background_register", "生活和职业带来的词汇范围"),
        ("speech_habits", "惯用句法与说话节奏"),
        ("under_pressure", "受压时的变调"),
        ("desire_and_mask", "当下争取与遮掩的底色"),
        ("sample_quote", "语感样例"),
    )
    if isinstance(profiles, dict):
        for name, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            details = "；".join(
                f"{label}：{profile[key]}" for key, label in labels if profile.get(key)
            )
            if details:
                lines.append(
                    f"角色{name}的个人表达基线是：{details}；"
                    "据此写其对白与行动，不能把被遮掩的动机直接写成限知视角已知的事实"
                )
    return lines


def _render_knowledge(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    refs = pack.get("knowledge_refs") or []
    if isinstance(refs, list) and refs:
        lines.append(
            f"本章重点核对个人认知话题：{_joined(refs)}；未入账只表示未追踪，"
            "不能推断某人已经知道或必然不知道"
        )
    stance_labels = {
        "knows": "已确认", "believes": "相信但未证实",
        "suspects": "仅怀疑", "refuted": "已推翻旧说法",
    }
    for item in pack.get("knowledge") or []:
        if not isinstance(item, dict):
            continue
        who = str(item.get("who") or "").strip()
        topic = str(item.get("topic_id") or "").strip()
        claim = str(item.get("claim") or "").strip()
        if not who or not topic or not claim:
            continue
        stance = stance_labels.get(str(item.get("stance") or ""), "尚未定性")
        source = str(item.get("source") or "").strip()
        chapter = int(item.get("updated_chapter") or 0)
        evidence = str(item.get("quote") or "").strip()
        line = f"第{chapter}章后，{who}对话题{topic}的个人认知为{stance}：{claim}"
        if source:
            line += f"；来源是{source}"
        if evidence:
            line += f"；最近正文证据是“{evidence}”"
        lines.append(line)
    if lines:
        lines.append("个人认知逐人有效：同场在场不等于都听见；误信不得当成世界客观规则")
    return lines


def _render_kb(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    cards = pack.get("kb_slice") or []
    for card in cards:
        if not isinstance(card, dict):
            continue
        ident = str(card.get("id") or "未编号").strip()
        title = str(card.get("title") or ident).strip()
        excerpt = _inline_excerpt(card.get("excerpt") or card.get("body") or "")
        if excerpt:
            lines.append(f"必须遵守正典卡“{title}”（卡片 id 为 {ident}）：{excerpt}")

    meta = pack.get("kb_slice_meta") or {}
    if isinstance(meta, dict):
        selected = int(meta.get("selected_cards") or len(cards))
        matched = int(meta.get("matched_cards") or selected)
        omitted = int(meta.get("omitted_cards") or 0)
        explicit = _joined(meta.get("explicit_refs"))
        if cards or matched or omitted or explicit:
            lines.append(f"本章正典检索命中{matched}张卡片，实际注入{selected}张")
        if explicit:
            lines.append(f"章拍已显式钉住这些关键正典卡：{explicit}")
        if omitted:
            lines.append(
                f"另有{omitted}张相关候选因工作包上限未注入；若本章依赖未出现的规则，"
                "必须请总编辑补充 kb_refs，不得自行猜测"
            )
    if not cards:
        lines.append("本章工作包没有注入正典卡片；不得据其他作品惯例自行补造世界规则")
    return lines


def _render_story_state(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    formats: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
        ("debts", "未结责任", (("who", "责任人"), ("text", "内容"), ("status", "状态"), ("due", "期限"))),
        ("hooks", "未结伏笔", (("text", "内容"), ("status", "状态"), ("due", "期限"))),
        ("relations", "人物关系", (("who", "人物"), ("target", "对象"), ("kind", "关系"), ("status", "状态"))),
        ("items", "关键物品", (("name", "名称"), ("holder", "持有人"), ("quantity", "数量"), ("status", "状态"))),
        ("conditions", "持续状态", (("who", "人物"), ("kind", "类别"), ("text", "内容"), ("value", "数值"), ("unit", "单位"), ("status", "状态"), ("irreversible", "不可逆"))),
    )
    for key, noun, fields in formats:
        for record in pack.get(key) or []:
            if isinstance(record, dict):
                details = _record_details(record, fields)
                if details:
                    lines.append(f"{noun}需要保持一致：{details}")
                # 逾期伏笔追加回收指令：只报「期限为第X章」不标逾期，
                # 写者无从知道该收了。对齐 conditions_directive 的指令句先例（pack.py）。
                if key == "hooks":
                    chapter = int(pack.get("chapter") or 0)
                    due = coerce_due(record.get("due")) or 0
                    if chapter > 0 and 0 < due < chapter:
                        lines.append(
                            f"注意：该伏笔已逾期 {chapter - due} 章（期限第{due}章，本章第{chapter}章）——"
                            "本章应优先把兑现演进正文（对白/动作演出，不写结案旁白）；结钩在组装步"
                            "由 `state_delta.hooks` 同 id 置 paid 附逐字 quote 申报，"
                            "确实无法回收的用 hooks defer 明确改期，不要无限顺延。"
                        )

    state = pack.get("state_near") or {}
    if isinstance(state, dict):
        # now_card / present_cards 已在「人物与连续性」完整呈现；这里只补差量，
        # 避免同一条长期事实被人物卡、pinned、recent 连续复述。
        seen: dict[str, dict[str, Any]] = {}
        for card, name_key in ((pack.get("now_card"), "name"),):
            if isinstance(card, dict) and card.get(name_key):
                seen[str(card[name_key])] = card
        for card in pack.get("present_cards") or []:
            if isinstance(card, dict) and card.get("name"):
                seen[str(card["name"])] = card
        for group, label in (("pinned", "本章钉住状态"), ("recent", "最近状态")):
            for record in state.get(group) or []:
                if not isinstance(record, dict):
                    continue
                original = record
                record = dict(record)
                name = str(record.get("id") or "")
                prior = seen.get(name)
                if isinstance(prior, dict):
                    for key in ("location", "dead"):
                        if record.get(key) == prior.get(key):
                            record.pop(key, None)
                    if isinstance(record.get("facts"), list):
                        remaining = [fact for fact in record["facts"] if fact not in (prior.get("facts") or [])]
                        if remaining:
                            record["facts"] = remaining
                        else:
                            record.pop("facts", None)
                seen[name] = {
                    **(prior or {}),
                    **original,
                    "facts": list(dict.fromkeys([*(prior or {}).get("facts", []), *original.get("facts", [])])),
                }
                if not any(key in record for key in ("location", "facts", "dead")):
                    continue
                details = _record_details(
                    record,
                    (("id", "人物"), ("location", "位置"), ("facts", "事实"), ("dead", "死亡标记")),
                )
                if details:
                    lines.append(f"{label}如下：{details}")
        occupancy = state.get("occupancy") or {}
        if isinstance(occupancy, dict):
            for location, names in occupancy.items():
                occupants = _joined(names)
                if occupants:
                    lines.append(f"地点“{location}”当前记录的在场者为：{occupants}")
    directive = str(pack.get("conditions_directive") or "").strip()
    if directive:
        lines.append(directive)
    return lines


def _render_memory(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    tail = str(pack.get("previous_chapter_tail") or "").strip()
    if tail:
        lines.append(f"上一章结尾原文如下，开场必须直接承接，不得倒带或复写：{tail}")
    for item in pack.get("near_summaries") or []:
        if not isinstance(item, dict):
            continue
        chapter = int(item.get("chapter") or 0)
        summary = str(item.get("l1_summary") or "").strip()
        if summary:
            lines.append(f"第{chapter}章已经发生：{summary}")
    layers = pack.get("memory_layers") or {}
    if isinstance(layers, dict):
        labels = {
            "phase_summary": "当前阶段摘要",
            "volume_summary": "当前卷摘要",
            "book_spine": "全书脊柱",
        }
        for key, label in labels.items():
            value = layers.get(key) or {}
            summary = str(value.get("summary") or "").strip() if isinstance(value, dict) else str(value).strip()
            if summary:
                lines.append(f"{label}是：{summary}")
            if isinstance(value, dict) and value.get("source_path"):
                index = value.get("phase_index") or value.get("volume_index") or []
                lines.append(f"{label}的按需来源：{value['source_path']}；范围索引={json.dumps(index, ensure_ascii=False)}。需要旧事时按phase/volume id精确读取，未加载的历史不能视为不存在。")
    recall = pack.get("historical_recall") or {}
    for record in recall.get("records") or []:
        evidence = str(record.get("quote") or "").strip()
        detail = f"原文证据：{evidence}" if evidence else "此项仅为账本记录，未核验当前正文原句"
        if record.get("kind") == "knowledge":
            detail += f"；当时认知={record.get('stance')}，来源={record.get('source')}，是否为截止本章的最新认知={record.get('latest_knowledge')}"
        lines.append(f"历史召回[{record.get('id')}]（第{record.get('chapter')}章，{record.get('kind')}）：{record.get('text')}；{detail}。人物是否知情仍以个人认知记录为准。")
    if recall.get("omitted"):
        lines.append(f"历史召回有 {recall['omitted']} 条候选未进入简报；缺关键证据请总编辑定向查询，不得据此断言旧事不存在。")
    sources = pack.get("context_sources") or {}
    if sources:
        lines.append(f"按需加载来源索引：{json.dumps(sources, ensure_ascii=False)}。仅沿当前人物、地点、卷、章拍引用定向读取；不要打开整书历史。")
    return lines


def _render_story_focus(pack: dict[str, Any], *, include_ids: bool = True) -> list[str]:
    """Render only this chapter's referenced story-map slice, never the whole map."""
    focus = pack.get("story_focus") or {}
    if not isinstance(focus, dict):
        return []
    lines: list[str] = []
    stage = str(focus.get("stage") or "").strip()
    if stage:
        lines.append(f"本章属于“{stage}”幕，不能提前兑现后续幕的结果")
    phase = focus.get("phase") or {}
    if isinstance(phase, dict) and phase:
        phase_label = f"“{phase.get('name', '-')}”"
        if include_ids:
            phase_label += f"（id={phase.get('id', '-')}）"
        lines.append(
            f"本章属于已签发阶段{phase_label}：阶段目标是{phase.get('objective', '-')}；"
            f"阶段高潮是{phase.get('climax', '-')}"
        )
        lines.append(
            f"本阶段从“{phase.get('entry_state', '-')}”推进到“{phase.get('exit_state', '-')}”；"
            f"起伏变化是{phase.get('tension_change', '-')}"
        )
        if phase.get("is_phase_end"):
            lines.append("本章是阶段末章，必须在正文中兑现阶段退出态和阶段高潮，不能只停在准备动作")
        if include_ids:
            lines.append(
                f"本阶段四层对齐为：主线事件{phase.get('mainline_event_ref', '-')}；"
                f"支线事件{_joined(phase.get('subplot_event_refs')) or '-'}；"
                f"时间线事件{_joined(phase.get('timeline_event_refs')) or '-'}；"
                f"起伏曲线阶段{phase.get('tension_stage', '-')}"
            )
    for thread in focus.get("threads") or []:
        if not isinstance(thread, dict):
            continue
        thread_label = f"“{thread.get('name', '-')}”（{thread.get('kind', '-')}"
        if include_ids:
            thread_label += f"，id={thread.get('id', '-')}"
        thread_label += "）"
        lines.append(
            f"本章推进故事线{thread_label}：作用是{thread.get('purpose', '-')}；"
            f"与主线的因果关系是{thread.get('mainline_link', '-')}"
        )
        if thread.get("beats_support") is False:
            # 本线在本章拍点里没有任何具名参与者：视图仍展示，但必须把"以 beats 为准"说清楚，
            # 否则执笔编辑会把 advisory 当成"必须给它一场戏"。口径见 story_map._thread_has_beat_support。
            lines.append(
                f"注意：{thread_label}在本章拍点与在场名单里都没有具名参与者——"
                "本章以 beats 为准，只可在既有场次内自然带出这条线，"
                "不得为它另起场次或硬塞人物；确需其出场请先回策划改章拍"
            )
        for point in thread.get("stage_touchpoints") or []:
            if isinstance(point, dict):
                lines.append(f"本幕触点是：{point.get('event', '-')}；应形成的变化是{point.get('change', '-')}")
    for clock in focus.get("clocks") or []:
        if not isinstance(clock, dict):
            continue
        clock_label = f"“{clock.get('name', '-')}”（{clock.get('kind', '-')}"
        if include_ids:
            clock_label += f"，id={clock.get('id', '-')}"
        clock_label += "）"
        lines.append(f"本章推进时间线{clock_label}；起始状态是{clock.get('start_state', '-')}")
        for event in clock.get("stage_events") or []:
            if isinstance(event, dict):
                lines.append(
                    f"本幕时钟事件是：{event.get('event', '-')}；截止条件为{event.get('deadline', '-')}；"
                    f"错失后果为{event.get('consequence', '-')}"
                )
    tension = focus.get("tension") or {}
    if isinstance(tension, dict) and tension:
        lines.append(
            f"本幕张力强度为{tension.get('level', '-')}，模式为{tension.get('mode', '-')}；"
            f"压力是{tension.get('pressure', '-')}，转折是{tension.get('turn', '-')}，"
            f"阶段兑现是{tension.get('payoff', '-')}，之后留下的新失衡是{tension.get('next_imbalance', '-')}"
        )
    return lines


def _render_constraints(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, label in (
        ("world_spine", "全书世界状态"),
        ("volume_spine", "本卷推进方向"),
        ("recap", "本章前情提要"),
        ("timeline", "本章时间定位"),
    ):
        value = pack.get(key)
        if isinstance(value, dict):
            rendered = "；".join(f"{name}为{item}" for name, item in value.items() if item not in (None, ""))
        else:
            rendered = str(value or "").strip()
        if rendered:
            lines.append(f"{label}是：{rendered}")
    glossary = pack.get("glossary") or {}
    if isinstance(glossary, dict):
        # glossary 被填成「词条→释义」时，机检侧按误用条目豁免
        # （gates._glossary_entry_misused），任务书渲染却无条件照抄——写手被自己的
        # 门禁教唆避开正典核心词（医案/药痕/配额堂 全部 0 命中）。渲染必须与机检
        # 同规：误用条目不进写作指令，留给 book audit 点名修 config。
        from .gates import _glossary_entry_misused

        for banned, canonical in glossary.items():
            banned = str(banned or "").strip()
            canonical = str(canonical or "").strip()
            if not banned or not canonical or banned == canonical:
                continue
            if _glossary_entry_misused(banned, canonical, pack):
                continue
            lines.append(f"正文不得使用“{banned}”，必须统一写作“{canonical}”")
    omitted = pack.get("omitted") or {}
    if isinstance(omitted, dict) and omitted:
        omitted_labels = {
            "near_summaries": "近章摘要",
            "debts": "未结责任",
            "hooks": "伏笔",
            "relations": "人物关系",
            "items": "关键物品",
            "conditions": "持续状态",
            "knowledge": "个人认知",
        }
        summary = "、".join(
            f"{omitted_labels.get(name, str(name))}省略{count}项"
            for name, count in omitted.items()
            if int(count or 0) > 0
        )
        if summary:
            lines.append(f"工作包还有受上限控制的状态未展开：{summary}；不得把未展示误判为不存在")
    return lines


def _omitted_policy(pack: dict[str, Any], *, stage: str) -> str:
    omitted = pack.get("omitted") or {}
    if not isinstance(omitted, dict) or not any(int(value or 0) > 0 for value in omitted.values()):
        return ""
    return (
        "部分候选状态因工作包上限未展开；若终稿涉及未展示条目，向总编辑报告缺失类别，"
        "不得浏览整库、猜测或补造账本。"
    )


def make_ledger_refs(pack: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """事实编辑所需的最小账本参照；不复制与本章无关的完整记录。"""
    fields = {
        "debts": ("id", "who", "text", "status", "due"),
        "hooks": ("id", "text", "status", "due"),
        "relations": ("who", "target", "kind", "status"),
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for category, allowed in fields.items():
        records: list[dict[str, Any]] = []
        for raw in pack.get(category) or []:
            if not isinstance(raw, dict):
                continue
            record = {key: raw[key] for key in allowed if raw.get(key) not in (None, "", [], {})}
            if record:
                records.append(record)
        if records:
            result[category] = records
    return result


def _open_relation_conflicts(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一对角色并存多条 open 关系——在**写之前**就把这件事摆给事实编辑看。

    账本侧（`ledger verify`）只能在写完之后报警；到那时新条目已经落盘。
    这里用同一口径做前置提示：命中就说明"要么这次加 supersedes，要么确属多重身份"。
    """
    from ..ledger.ledger import relation_is_closed

    pairs: dict[tuple[str, str], list[str]] = {}
    for rel in relations:
        if not isinstance(rel, dict) or relation_is_closed(rel):
            continue
        who = str(rel.get("who") or "").strip()
        target = str(rel.get("target") or "").strip()
        kind = str(rel.get("kind") or "").strip()
        if not who or not target or not kind:
            continue
        pairs.setdefault(tuple(sorted((who, target))), []).append(kind)
    return [
        {"pair": list(pair), "open_kinds": kinds}
        for pair, kinds in pairs.items()
        if len(set(kinds)) > 1
    ]


def render_verification_brief(pack: dict[str, Any]) -> str:
    """给 compact 事实编辑的有限前情证据，用于同一遍完成情节自检。"""
    lines: list[str] = []
    tail = str(pack.get("previous_chapter_tail") or "").strip()
    if tail:
        lines.append(f"上一章结尾原文是：{tail}")
    for item in pack.get("near_summaries") or []:
        if not isinstance(item, dict):
            continue
        chapter = int(item.get("chapter") or 0)
        summary = str(item.get("l1_summary") or "").strip()
        if summary:
            lines.append(f"第{chapter}章已经发生：{summary}")
    for record in pack.get("character_continuity") or []:
        if not isinstance(record, dict):
            continue
        directive = str(record.get("continuity_directive") or "").strip()
        if directive:
            lines.append(f"人物连续性要求：{directive}")
    lines.extend(_render_story_focus(pack, include_ids=True))
    return "\n".join(f"- {_sentence(line)}" for line in lines)


def _render_locations(pack: dict[str, Any]) -> list[str]:
    """场景地图（地点簿）渲染：空间属性是章间最脆的连续性，逐地点成句。"""
    lines: list[str] = []
    for card in pack.get("locations") or []:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or card.get("id") or "").strip()
        if not name:
            continue
        attrs = card.get("attributes") or {}
        parts = [f"{key}为{value}" for key, value in attrs.items() if str(value or "").strip()]
        aliases = _joined(card.get("aliases"))
        established = int(card.get("established_chapter") or 0)
        head = f"地点「{name}」"
        if aliases:
            head += f"（又称{aliases}）"
        if established:
            head += f"（第{established}章确立）"
        if parts:
            lines.append(f"{head}的空间口径：{'，'.join(parts)}")
        if str(card.get("status") or "open").lower() == "closed":
            lines.append(f"地点「{name}」已登记为退出/不可用状态，不得再当作现成场景使用")
    if lines:
        lines.append(
            "以上空间口径是已入账事实：正文提及这些地点时属性必须一致；"
            "剧情确实需要搬迁/翻修时，先在正文演出变化，组装申报 locations 并对变化的属性键加 replaces 留痕"
        )
    return lines


def render_writing_brief(pack: dict[str, Any]) -> str:
    """把 canonical pack 单向渲染为执笔可直接执行的完整句式任务书。"""
    chapter = int(pack.get("chapter") or 0)
    parts = [f"# 第{chapter}章写作简报"]
    word_band = pack.get("word_band") or {}
    positioning: list[str] = []
    if isinstance(word_band, dict):
        minimum = int(word_band.get("min") or 0)
        maximum = int(word_band.get("max") or 0)
        if minimum and maximum:
            # 只写区间会被写成下限：现场实测第 5/6/8 章首轮草稿 1789–2309 汉字，都要被
            # 阶段机检打回再补整场戏。把安全目标与场次尺度写进简报本身——执笔读的是
            # writing_brief，看不见 `chapter next` 的行动载荷。
            targets = word_targets(word_band)
            budget = scene_budget(targets["aim_chars"], floor=minimum)
            positioning.append(
                f"本章正文目标长度为{targets['aim_chars']}个中文字（硬闸{minimum}至{maximum}，只数汉字）："
                f"共 {budget['scenes']} 场戏，每场约 {budget['chars_per_scene']} 汉字。"
                f"写不足{minimum}会被 `chapter draft-submit` 当场拒收（draft_rejected）；"
                "补足只允许一次性补整场戏，严禁每轮挤几十字。"
                "场次预算是本章的场数下限：章拍列出的场次少于预算时，把信息最厚的场拆成两场"
                "或补一场当口发生的过场戏，全部 must 词必须覆盖，严禁注水凑数。"
            )
    # 视角结构与平台尺度是全书级纪律（hatch 门 8 声明、随 plan 落盘），每章定位段重申
    for key in ("narrative_pov", "content_fence"):
        sentence = str(pack.get(key) or "").strip()
        if sentence:
            positioning.append(sentence)
    if str(pack.get("dateline_openings") or "discourage") != "allow":
        positioning.append(
            "时间呈现纪律：简报与正典里的绝对日期、钟点供你对齐口径，不供照抄进正文——"
            "本章不用「X月X号早上X点」式的日志句开场，日期与时刻只在跨日、跨周或读者需要对表时"
            "出现（全章至多一处显式日期），优先从车次、饭点、挂历、会议点名这类场景事物自然带出；"
            "承接上一章靠场景与动作，不靠复述日期。连续多章日期句开场会被 dateline_opening 机检点名。"
        )
    _append_section(parts, "本章定位", positioning)
    _append_section(parts, "场次与硬锚点", _render_beats(pack))
    _append_section(parts, "场景地点与空间口径（正文中这些地点的属性必须一致）", _render_locations(pack))
    _append_section(parts, "本章故事线路与时钟", _render_story_focus(pack, include_ids=False))
    _append_section(parts, "计划结果（必须在正文中实际发生）", _render_expected_delta(pack))
    _append_section(parts, "人物与连续性", _render_characters(pack))
    _append_section(parts, "在场者的知情与误信", _render_knowledge(pack))
    _append_section(parts, "前情与长线记忆", _render_memory(pack))
    _append_section(parts, "正典规则", _render_kb(pack))
    _append_section(parts, "当前剧情状态", _render_story_state(pack))
    _append_section(parts, "宏观约束与知识边界", _render_constraints(pack))
    return "\n".join(parts).strip() + "\n"


def make_draft_view(pack: dict[str, Any]) -> dict[str, Any]:
    """执笔输入：机器结构只留在 canonical，视图只给确定性自然语言简报。"""
    view: dict[str, Any] = {
        "schema": DRAFT_SCHEMA,
        "view": "draft",
        "chapter": int(pack.get("chapter") or 0),
        "writing_brief": render_writing_brief(pack),
    }
    # 注入内容层手册，指导场景与对白；世界事实仍以项目正典为准。
    view.update(_pick(pack, _CONTENT_GUIDE_KEYS))
    view["draft_contract"] = {"gates": list(DRAFT_GATES)}
    view["read_only"] = True
    view["forbid_library_browse"] = True
    return _stable_first(view, _DRAFT_STABLE)


def make_polish_view(pack: dict[str, Any]) -> dict[str, Any]:
    """润色阶段的只读输入：只留润色手册 + 内容锚点；不带审校清单/配额表。"""
    view: dict[str, Any] = {
        "schema": POLISH_SCHEMA,
        "view": "polish",
        "chapter": int(pack.get("chapter") or 0),
    }
    if "voice_writing_text" in pack:
        # 文风编辑只看润色手册；判据级硬红线由 polish-submit 机检执行。
        # 保持 canonical pack 的 voice_writing_text 键名，不重复拷贝总手册。
        view["voice_writing_text"] = pack["voice_writing_text"]
        if "voice_writing_manual" in pack:
            view["voice_writing_manual"] = pack["voice_writing_manual"]
    elif "voice_manual_text" in pack:
        # 兼容手写旧 pack（只有 voice_manual_text）：转到润色阶段使用的键。
        view["voice_writing_text"] = pack["voice_manual_text"]
    # 人写节奏锚（可选，config.voice_anchor_file 配置后随包注入）：只学节奏不学内容。
    # 不进 _POLISH_STABLE——切片按章号滚动，逐章不同，排进稳定前缀会截断 prompt cache。
    if "voice_anchor_text" in pack:
        view["voice_anchor_text"] = pack["voice_anchor_text"]
    view.update(
        _pick(pack, tuple(k for k in _CONTENT_KEYS if k not in _POLISH_DROP_KEYS))
    )
    # 在场人物卡只留 name/关系；角色表达基线另由 character_profiles 给出。
    # 卡里的 facts / continuity_directive / rendered / 出场元数据是 draft 与 assemble
    # 视图逐字重复的副本（单卡实测 938 字，判据以外约九成），且会诱导它推理剧情。
    if isinstance(view.get("present_cards"), list):
        view["present_cards"] = [
            {
                key: card[key]
                for key in ("name", "relations_with_protagonist")
                if isinstance(card, dict) and key in card
            }
            for card in view["present_cards"]
        ]
    # 润色只需可观察的说话方式。隐藏动机与样句可能包含作者层秘密或旧情节，
    # 不应在内容冻结的文风阶段重新暴露给编辑。
    if isinstance(view.get("character_profiles"), dict):
        view["character_profiles"] = {
            name: {key: profile[key] for key in ("background_register", "speech_habits", "under_pressure") if key in profile}
            for name, profile in view["character_profiles"].items()
            if isinstance(profile, dict)
        }
    if "word_band" in pack:
        view["word_band"] = pack["word_band"]
    view["polish_contract"] = {"gates": list(POLISH_GATES)}
    view["read_only"] = True
    view["forbid_library_browse"] = True
    return _stable_first(view, _POLISH_STABLE)


def make_assemble_view(pack: dict[str, Any]) -> dict[str, Any]:
    """组装阶段的只读输入：终稿正文 + 结构锚点 + 输出契约；零 voice_* 字段。

    单一 compact 泳线没有独立剧情审校，恒注入有界验证简报（verification_brief），
    供事实编辑的情节自检（plot_self_check）使用。
    """
    view: dict[str, Any] = {
        "schema": ASSEMBLE_SCHEMA,
        "view": "assemble",
        "chapter": int(pack.get("chapter") or 0),
        "pack_hash": str(pack.get("pack_hash") or ""),
        "inputs_fingerprint": str(pack.get("inputs_fingerprint") or ""),
    }
    view.update(_pick(pack, _ASSEMBLE_CONTENT_KEYS))
    # 同一个人物事实只在最先出现的卡片中保留；其他结构字段供归因和名单机检使用。
    seen_facts: dict[str, set[str]] = {}

    def _trim_facts(record: dict[str, Any], name_key: str) -> dict[str, Any]:
        clean = dict(record)
        # canonical 的 rendered 文本重复了本卡结构字段；组装角色只需结构值。
        clean.pop("rendered", None)
        name = str(clean.get(name_key) or "")
        raw = clean.get("facts")
        if name and isinstance(raw, list):
            prior = seen_facts.setdefault(name, set())
            remaining = [fact for fact in raw if isinstance(fact, str) and fact not in prior]
            prior.update(remaining)
            if remaining:
                clean["facts"] = remaining
            else:
                clean.pop("facts", None)
        return clean

    if isinstance(view.get("now_card"), dict):
        view["now_card"] = _trim_facts(view["now_card"], "name")
    if isinstance(view.get("present_cards"), list):
        view["present_cards"] = [
            _trim_facts(card, "name") if isinstance(card, dict) else card
            for card in view["present_cards"]
        ]
    if isinstance(view.get("state_near"), dict):
        state = dict(view["state_near"])
        for group in ("pinned", "recent"):
            if isinstance(state.get(group), list):
                state[group] = [
                    _trim_facts(record, "id") if isinstance(record, dict) else record
                    for record in state[group]
                ]
        view["state_near"] = state
    # 连续性指令句只保留一份：verification_brief 已把 character_continuity 的
    # continuity_directive 渲染成完整句子（情节自检的判据形态）；原始记录里再逐字
    # 带一份就是同文二发。结构字段（name/首末章/关系/死亡标记）留给归因与核对。
    if isinstance(view.get("character_continuity"), list):
        stripped = []
        for record in view["character_continuity"]:
            if not isinstance(record, dict):
                stripped.append(record)
                continue
            clean = _trim_facts(
                {k: v for k, v in record.items() if k != "continuity_directive"}, "name"
            )
            if clean:
                stripped.append(clean)
        if stripped:
            view["character_continuity"] = stripped
        else:
            view.pop("character_continuity", None)
    ledger_refs = make_ledger_refs(pack)
    if ledger_refs:
        view["ledger_refs"] = ledger_refs
        conflicts = _open_relation_conflicts(ledger_refs.get("relations") or [])
        if conflicts:
            view["relation_conflicts"] = conflicts
            view["relation_conflict_policy"] = (
                "上面这些角色对已经挂着多条 open 关系。本章若写的是同一段关系的演变/取代，"
                '请在新增的 relations 条目上加 "supersedes": true（必要时用 "supersedes_kind" 指定退掉哪条），'
                "账本会把其它 open 条目置为 closed；确实是同时成立的多重身份（师徒＋姻亲）则不加。"
            )
    verification_brief = render_verification_brief(pack)
    if verification_brief:
        view["verification_brief"] = verification_brief
    omitted_policy = _omitted_policy(pack, stage="assemble")
    if omitted_policy:
        view["omitted_policy"] = omitted_policy
    view["allowed_delta_names"] = sorted(allowed_names(pack))
    view["allowed_name_policy"] = (
        "state_delta.named 与 moves/facts/conditions.who 只能使用 allowed_delta_names 中的既有名字；"
        "本章首次出现且正文确有演出的人物，先写入 new_names，再在同一提交的其他字段中使用。"
    )
    if "expected_delta" in pack:
        view["expected_delta"] = pack["expected_delta"]
    if "write_contract" in pack:
        contract = pack["write_contract"]
        if isinstance(contract, dict):
            contract = dict(contract)
            contract["gates"] = list(ASSEMBLE_GATES)
            view["write_contract"] = contract
        else:
            view["write_contract"] = contract
    view["read_only"] = True
    view["forbid_library_browse"] = True
    return _stable_first(view, _ASSEMBLE_STABLE)


# —— 组装简报：一次读全的线性任务书 ——
# 现场实测（样本书第 5/6 章会话复盘）：组装 worker 面对单行 JSON 包只能用脚本分片转储
# （每章 4–5 遍 ≈35K 字符进上下文），还要翻 skill 安装目录找输出契约（≈25K）；
# 单章累计 0.9–1.2M token。简报把核对输入与提交模板压成一份多行文本，worker
# 一次 read 全进；assemble pack JSON 退为机器真源与定向补读目标，不再是阅读面。

_LEDGER_REF_FIELDS = {
    "debts": (("id", "编号"), ("who", "责任人"), ("text", "内容"), ("status", "状态"), ("due", "期限")),
    "hooks": (("id", "编号"), ("text", "内容"), ("status", "状态"), ("due", "期限")),
    "relations": (("who", "人物"), ("target", "对象"), ("kind", "关系"), ("status", "状态")),
}
_LEDGER_REF_LABELS = {
    "debts": "未结责任", "hooks": "未结伏笔", "relations": "人物关系",
}


def _brief_delta_shapes(contract: dict[str, Any]) -> list[str]:
    """把 write_contract.delta_schema 渲染成一行一字的条目形状说明。"""
    schema = contract.get("delta_schema") or {}
    lines: list[str] = []
    for field, shape in schema.items():
        if isinstance(shape, list) and shape and isinstance(shape[0], dict):
            parts = [f"{key}={value}" for key, value in shape[0].items()]
            lines.append(f'"{field}": [{{{"，".join(parts)}}}]')
        else:
            lines.append(f'"{field}": {json.dumps(shape, ensure_ascii=False)}')
    return lines


def render_assemble_brief(view: dict[str, Any]) -> str:
    """把 assemble 视图单向渲染为一次可读完的线性任务书（token 瘦身的读取形态）。

    只读视图、不改正文：本函数是视图→文本的单向确定性渲染，与 writing_brief 同构；
    机器校验仍以 assemble pack JSON 与 submit 机检为准。
    """
    chapter = int(view.get("chapter") or 0)
    pack_hash = str(view.get("pack_hash") or "")
    parts: list[str] = [f"# 第{chapter}章组装任务书", ""]
    parts.append(
        "本简报是组装阶段唯一必读任务书：一次读完即可开工。不要用脚本分片转储 "
        "assemble pack JSON，也不要翻 skill 安装目录或协议文档找输出格式——"
        "提交模板与字段形状都在下面。终稿正文另按 action 指到的 polished_output_path 全文阅读。"
    )
    parts.append(
        "quote（引文）由 `chapter submit` 机检逐字校验：不要手工核对引文字符，"
        "也不要写核验脚本；被拒就按诊断定点修改。submit JSON 一次写成，"
        "修订只对报错条目做定点替换，不整文件重写、不读回全文。"
    )

    # 提交模板：固定字段预填（pack_hash/beats_hit），变量字段给形状。
    contract = view.get("write_contract") or {}
    beats = [beat for beat in view.get("beats") or [] if isinstance(beat, dict)]
    required_ids = [
        str(beat.get("id") or "").strip()
        for beat in beats
        if str(beat.get("id") or "").strip() and beat.get("required", True) is not False
    ]
    prose_hash = str(view.get("prose_hash") or "")
    template = [
        "{",
        '  "l1_summary": "<本章一段话摘要>",',
        '  "state_delta": {<各列表见下方字段形状；空列表保留键名>},',
        '  "memory": {"voice_concepts": []},',
        f'  "pack_hash": "{pack_hash}",',
        "  \"beats_hit\": " + (json.dumps(required_ids, ensure_ascii=False) if required_ids else "[]") + ",",
        '  "plot_findings": []',
        "}",
        "（beats_hit 逐场核对终稿确实演出后保留；required 拍缺失要在 plot_findings 里报告。",
        "plot_findings 无发现写 []，不得省略；有发现时每条 {code, severity, hint, quote}；",
        "beat 锚词缺失用 code=beat_anchor_missing 并带 beat_id。",
        "plot_findings 是对本次所读正文的一次性判断：正文若被返工，判断作废，须重跑本阶段。）",
    ]
    if prose_hash:
        # 新鲜度凭证：submit 比对该字段与现行终稿哈希，不一致拒收（prose 返工后旧
        # plot_findings 不得重提）。模板预填即「逐字回显」的机器形态。
        template.insert(5, f'  "prose_hash": "{prose_hash}",')
    parts.append("")
    parts.append("## 提交模板（写到 action 的 submit_output_path）")
    parts.extend(template)
    shapes = _brief_delta_shapes(contract)
    if shapes:
        parts.append("state_delta 字段形状（真源 write_contract.delta_schema）：")
        parts.extend(f"- {shape}" for shape in shapes)
    for gate in contract.get("gates") or []:
        parts.append(f"- 契约：{gate}")

    _append_section(parts, "拍点与硬锚点（逐场核对终稿）", _render_beats(view))
    _append_section(parts, "计划增量（expected_delta 原样回填并补 quote）", _render_expected_delta(view))
    _append_section(
        parts,
        "场景地点簿（正文提及这些地点时属性必须一致；正文确立新空间属性时申报 locations）",
        _render_locations(view),
    )

    verification = str(view.get("verification_brief") or "").strip()
    if verification:
        parts.append("")
        parts.append("## 前情验证简报（同一遍做情节自检）")
        parts.append(verification)

    _append_section(parts, "人物与连续性", _render_characters(view))
    _append_section(parts, "在场者的知情与误信", _render_knowledge(view))

    refs_lines: list[str] = []
    refs = view.get("ledger_refs") or {}
    for category, records in (refs.items() if isinstance(refs, dict) else []):
        if not isinstance(records, list):
            continue
        label = _LEDGER_REF_LABELS.get(str(category), str(category))
        for record in records[:24]:
            if isinstance(record, dict):
                details = _record_details(record, _LEDGER_REF_FIELDS.get(str(category), (("id", "编号"),)))
                if details:
                    refs_lines.append(f"{label}：{details}")
        if len(records) > 24:
            refs_lines.append(f"{label}另有 {len(records) - 24} 条未列出；需要时按 id 定向查询，不得凭印象续写")
    for conflict in view.get("relation_conflicts") or []:
        if isinstance(conflict, dict):
            pair = _joined(conflict.get("pair"))
            kinds = _joined(conflict.get("open_kinds"))
            if pair and kinds:
                refs_lines.append(f"角色对（{pair}）并存多条 open 关系：{kinds}")
    policy = str(view.get("relation_conflict_policy") or "").strip()
    if policy:
        refs_lines.append(policy)
    _append_section(parts, "账本投影（结合终稿判断续建、关闭或更新）", refs_lines)

    _append_section(parts, "当前剧情状态", _render_story_state(view))
    _append_section(parts, "正典规则", _render_kb(view))
    _append_section(parts, "长线记忆与历史召回", _render_memory(view))

    names_lines: list[str] = []
    allowed = view.get("allowed_delta_names") or []
    if allowed:
        names_lines.append(f"named 与 moves/facts/conditions.who 只能使用：{_joined(allowed)}")
    names_policy = str(view.get("allowed_name_policy") or "").strip()
    if names_policy:
        names_lines.append(names_policy)
    _append_section(parts, "名字白名单", names_lines)

    omitted_policy = str(view.get("omitted_policy") or "").strip()
    if omitted_policy:
        parts.append("")
        parts.append(omitted_policy)
    return "\n".join(parts).strip() + "\n"
